"""
WebSocket JSON-RPC 2.0 server.

Handles all communication between Dashboard/Bots and the Python backend.
Runs on ws://127.0.0.1:9876.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger("addled.ws")

# Reference to the engine instance (set by main.py after engine is created)
_engine_ref = None

# Pending code edits awaiting user approval: {workspaceId::filePath: content}
_pending_edits: dict[str, str] = {}

# Strong references to the background jobs this module starts.
#
# `asyncio` keeps only a WEAK reference to a running task, so a bare
# `create_task(...)` whose result is dropped can be garbage-collected mid-run —
# the job stops with no exception, no log line and no trace. That is how a
# chat reply could be silently not spoken, or a compaction silently never
# happen. Every fire-and-forget call here goes through `_spawn` instead.
_background_tasks: set = set()

def _spawn(coro):
    """Start a background job and keep it alive until it finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

def set_engine(engine):
    """Called by main.py to give WS handlers access to engine state."""
    global _engine_ref
    _engine_ref = engine

# ---- message types ----------------------------------------------------------

HandlerFunc = Callable[[dict, WebSocketServerProtocol], Awaitable[dict | None]]


class WSServer:
    """JSON-RPC 2.0 WebSocket server with method dispatch."""

    def __init__(self):
        self._handlers: dict[str, HandlerFunc] = {}
        self._connections: set[WebSocketServerProtocol] = set()
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._nav_intent: str | None = None
        # Broadcasts scheduled from inside the loop. Held here because a bare
        # `create_task` is only weakly referenced — the runtime may collect it
        # before it runs, which is the other half of how an announcement could
        # be dropped without an error.
        self._background: set[asyncio.Task] = set()

    def set_nav_intent(self, path: str):
        """Queue a GUI navigation request (consumed by the Electron shell)."""
        self._nav_intent = path

    def consume_nav_intent(self) -> str | None:
        """Return + clear the pending navigation intent (single-shot)."""
        intent, self._nav_intent = self._nav_intent, None
        return intent

    def register(self, method: str, handler: HandlerFunc):
        """Register a JSON-RPC method handler."""
        self._handlers[method] = handler

    async def start(self, host: str = "127.0.0.1", port: int = 9876):
        """Start the WebSocket server."""
        from backend.remote.policy import allowed_origins
        self._loop = asyncio.get_running_loop()
        self._server = await websockets.serve(
            self._handle_connection, host, port,
            max_size=64 * 1024 * 1024,  # 64MB — room for base64 image attachments
            # Without this, any page in any browser on this machine could open a
            # socket and drive the API. Loopback stops the network, not the
            # user's own browser.
            origins=allowed_origins(),
        )
        log.info("WebSocket server listening on ws://%s:%d", host, port)

    async def stop(self):
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("WebSocket server stopped")

    async def broadcast(self, method: str, params: dict | None = None):
        """Send a notification to all connected clients."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })
        dead = set()
        for ws in self._connections:
            try:
                await ws.send(msg)
            except websockets.ConnectionClosed:
                dead.add(ws)
        self._connections -= dead

    def broadcast_nowait(self, method: str, params: dict | None = None):
        """Fire-and-forget broadcast, callable from any thread.

        Two callers, and they need different scheduling. The engine loop and
        background jobs run on their *own* thread and must hop to this one with
        `run_coroutine_threadsafe`. An RPC handler — `chat_send` announcing a
        bot's turn — is already on this loop, and using that same call there
        silently dropped the notification: the coroutine was handed to the
        loop's thread-safe queue and, because the handler's own reply was
        awaited and sent immediately after, the announcement was never reached
        before the task was collected. The symptom was a broadcast that
        reported a live connection and delivered nothing.

        Detecting which side we are on is what makes both work.
        """
        if self._loop is None or self._loop.is_closed():
            return
        coro = self.broadcast(method, params)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            # Already here: schedule a real task so it cannot be dropped.
            self._background.add(task := self._loop.create_task(coro))
            task.add_done_callback(self._background.discard)
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception as e:  # noqa: BLE001
            log.debug("could not schedule the %s broadcast: %s", method, e)
            coro.close()

    def _note_remote(self, ws: WebSocketServerProtocol) -> dict:
        """Attach the remote context to a connection, if the gateway set one.

        The gateway connects from loopback, so the peer address cannot tell a
        remote caller from a local one — the headers it injects are the only
        signal, and they are read here once so nothing downstream has to.
        """
        context = {"remote": False, "session": "", "addr": "",
                   "tailscale_user": "", "tailscale_device": ""}
        try:
            request = getattr(ws, "request", None)
            headers = getattr(request, "headers", None)
            if headers is not None and headers.get("X-Addled-Remote"):
                context = {
                    "remote": True,
                    "session": str(headers.get("X-Addled-Session") or ""),
                    "addr": str(headers.get("X-Forwarded-For") or ""),
                    "tailscale_user": str(headers.get("Tailscale-User-Login") or ""),
                    "tailscale_device": str(headers.get("Tailscale-Node-Name") or ""),
                }
        except Exception as e:  # noqa: BLE001
            log.debug("Could not read remote context: %s", e)
        try:
            ws.addled = context
        except Exception:  # noqa: BLE001
            pass
        return context

    async def _handle_connection(self, ws: WebSocketServerProtocol):
        """Handle a single WebSocket connection.

        Each request is dispatched as its own task rather than awaited in the
        read loop. Awaiting in the loop meant one slow handler blocked every
        later message on the same socket — and the case that matters most is
        exactly the wrong one: `chat.send` runs for a long time while it waits
        for the model, so an `action.approve` sent during that turn sat unread
        in the socket buffer until the turn finished. The approval card was
        built to be clicked *while* a turn is waiting, so sequential dispatch
        made the whole approval path look broken: the button disabled itself,
        no reply ever came, and the tool never ran.
        """
        self._connections.add(ws)
        context = self._note_remote(ws)
        peer = ws.remote_address
        if context["remote"]:
            log.info("Remote client connected: session=%s via %s from %s",
                     context["session"] or "?", peer,
                     context["addr"] or "?")
        else:
            log.info("Client connected: %s", peer)

        in_flight: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }))
                    continue
                task = asyncio.create_task(self._serve(msg, ws, peer))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        except websockets.ConnectionClosed:
            log.info("Client disconnected: %s", peer)
        finally:
            # The socket's life ends here, so this is where it leaves the
            # broadcast set. Doing this in `_serve` — which is where it was —
            # removed the connection as soon as its *first* request completed:
            # each request is its own task now, so the quickest one to finish
            # unsubscribed a client that was still connected. Its reply still
            # arrived, because that goes to the socket directly, while every
            # later broadcast went nowhere — an announcement that reported a
            # live connection and delivered nothing to it.
            self._connections.discard(ws)
            # The socket is gone, so a reply can no longer be delivered.
            # Cancelling stops a handler waiting on a decision nobody can send
            # — `action.approve` waiting on a decision is the one that matters
            # — from holding the task until its own timeout.
            for task in list(in_flight):
                if not task.done():
                    task.cancel()

    async def _serve(self, msg: dict, ws: WebSocketServerProtocol,
                     peer) -> None:
        """Run one request and send its reply. Never raises."""
        try:
            response = await self._dispatch(msg, ws)
            if response is not None:
                await ws.send(json.dumps(response))
        except websockets.ConnectionClosed:
            log.debug("Client went away before the reply to %s",
                      msg.get("method") if isinstance(msg, dict) else "?")
        except Exception:
            log.exception("Error handling message from %s", peer)
            try:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32603, "message": "Internal error"},
                }))
            except Exception:  # noqa: BLE001
                log.debug("could not report the failure to %s", peer)

    async def _dispatch(self, msg: dict, ws: WebSocketServerProtocol) -> dict | None:
        """Route a JSON-RPC message to the appropriate handler."""
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        # Notifications have no id — don't respond
        is_notification = msg_id is None and method != ""

        # Read the remote flag straight off the connection so this works even
        # if the policy module cannot be imported. Failing closed matters here:
        # a broken import must not silently unlock a remote session.
        remote = bool((getattr(ws, "addled", None) or {}).get("remote"))
        sid = str((getattr(ws, "addled", None) or {}).get("session") or "")

        refuser = None
        if remote:
            try:
                from backend.remote.policy import remote_refusal
                refuser = remote_refusal
            except Exception as e:  # noqa: BLE001
                log.error("Remote policy unavailable (%s) — refusing '%s'",
                          e, method)
                if is_notification:
                    return None
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32002,
                              "message": "Remote access is misconfigured on the "
                                         "machine running Addled; this call was "
                                         "refused."},
                }

        handler = self._handlers.get(method)
        if handler is None:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        try:
            # One gate for every remote restriction. A handler added later is
            # covered by default rather than by remembering to check.
            if refuser is not None:
                refusal = refuser(method, params)
                if refusal:
                    log.warning("Remote session %s refused '%s'", sid or "?", method)
                    if is_notification:
                        return None
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32001, "message": refusal},
                    }

            result = await handler(params, ws)
            if is_notification:
                return None
            if remote:
                # Local calls stay unlogged: they are the dashboard polling
                # itself, and logging them would bury the ones that matter.
                log.info("Remote session %s: %s -> ok", sid or "?", method)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result or {},
            }
        except Exception as e:
            log.exception("Handler error for %s", method)
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": str(e)},
            }


# ---- singleton ---------------------------------------------------------------

_server = WSServer()


def get_server() -> WSServer:
    return _server


def _extract_code(text: str) -> str:
    """Pull the code out of a model reply.

    Handles the two shapes models actually return: a bare code block, and a
    fenced block preceded by commentary. Returns "" only when there is nothing
    usable, so callers can avoid diffing against an empty result.
    """
    if not text:
        return ""
    body = text.strip()
    import re
    match = re.search(r"```[^\n]*\n(.*?)```", body, re.DOTALL)
    if match:
        return match.group(1).strip("\n")
    return body

def _extract_json_object(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply, or None.

    Models wrap JSON in a fence, in prose, or both. Rather than a regex that
    would fail on a nested brace, this scans for the first balanced `{...}`
    and parses that — so a fenced block, a bare object, and "Here is the plan:
    {...}" all work. Returns None when there is no parseable object, which the
    caller treats as "not the anchored shape" and falls back from.
    """
    if not text:
        return None
    import json
    body = text
    # Prefer the contents of a fenced block when there is one.
    import re
    fence = re.search(r"```[a-zA-Z]*\n(.*?)```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(body):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        parsed = json.loads(body[start:i + 1])
                    except json.JSONDecodeError:
                        start = -1
                        continue
                    if isinstance(parsed, dict):
                        return parsed
                    start = -1
    return None


# Tools the Code page's "Ask the model" box may use. Read-only on purpose: the
# change it produces is reviewed as a diff and applied by the user, and none of
# these can write, so that review step cannot be skipped by the model itself.
#
# Wiki and procedure tools are read-only too (`wiki_search`, `wiki_read`,
# `sop_lookup`, `sop_list`), so they are safe here: the model can look up
# project documentation or coding recipes, but still cannot write to the wiki
# or save a new procedure without going through the Chat page where the user
# sees the call. `wiki_write`, `wiki_ingest`, and `sop_save` are deliberately
# excluded — a code edit must not have side effects beyond the diff.
_CODE_EDIT_TOOLS = ("code_read", "read_file", "list_dir", "search_files",
                    "file_info",
                    "wiki_search", "wiki_read", "sop_lookup", "sop_list")

# Tools the PLANNER may use. Also read-only, and deliberately the same set —
# planning must be able to look at the project without being able to change it.
#
# `search_in_files` is the important one, and getting its NAME wrong is a real
# failure mode this list already hit once: it said `code_grep`, which is a
# WebSocket method for the UI's search panel, NOT an agent skill. The model was
# therefore never offered a content search, and planned against filenames it
# guessed from the request ("registry.py", "likely contains…") instead of files
# it had found. Verified by tracing: with `code_grep` absent the planner made
# ZERO tool calls. `search_files` matches file NAMES against a glob and cannot
# substitute — only `search_in_files` looks inside them.
#
# The planner also gets `wiki_search`, `wiki_read`, `sop_lookup`, and
# `sop_list`: a good plan names the right files, but it also follows the
# project's own conventions and standard procedures. Without these, the
# planner invented a structure that contradicted a documented standard.
_PLAN_TOOLS = ("code_read", "search_in_files", "read_file", "list_dir",
               "search_files", "file_info",
               "wiki_search", "wiki_read", "sop_lookup", "sop_list")

# How many reviewed edits one `code.apply_plan` call may write. The page warns
# from 8 files, so reaching this means something is wrong rather than thorough.
_MAX_PLAN_APPLIES = 40


def _plan_concurrency() -> int:
    """How many Code-page diffs may be requested at once right now.

    Reads the active provider and asks the shared helper, so the page is told
    the truth about whether asking for several at once achieves anything. On the
    local model the answer is 1, and the UI says so rather than pretending.
    """
    try:
        from backend.providers.base import concurrency_width
        from backend.providers.registry import get_provider
        return concurrency_width(get_provider())
    except Exception as e:
        log.debug("plan concurrency probe failed: %s", e)
        return 1

# The editor rewrites a whole file in one reply, so the file has to fit with
# room to think. Above this a partial answer would diff as a mass deletion.
_CODE_EDIT_MAX_CHARS = 12000


async def _analyze_attachments(provider, attachments: list[dict],
                              vision_model: str | None = None) -> list[str]:
    """Route attachments to the right model:
    - images → visual model (provider vision or local Florence-2) → text description
    - text files → inline content
    - others → filename/size note only

    Returns context notes injected into the chat for the main model.
    """
    notes: list[str] = []
    for att in (attachments or [])[:5]:
        name = str(att.get("name") or "file")[:120]
        kind = att.get("kind", "file")
        data = att.get("data", "")

        if kind == "image" and data:
            prompt = ("Describe this image in detail: what is shown, "
                      "any visible text, and the overall context.")
            desc = None
            try:
                if getattr(provider, "supports_vision", False):
                    res = await provider.vision(data, prompt,
                                                model=vision_model)
                    if res.ok:
                        desc = res.response
            except Exception:
                desc = None
            if not desc:
                # Fallback: local Florence-2 visual model
                try:
                    from backend.providers.hf_vision import hf_vision
                    res = await hf_vision.analyze(data, prompt)
                    if res.ok:
                        desc = res.response
                except Exception:
                    desc = None
            if desc:
                notes.append(
                    f'[Attached image "{name}" — visual model analysis: '
                    f'{str(desc)[:1500]}]')
            else:
                # Both the provider and the local model failed. Say so in the
                # log as well as the chat, or the reason is lost.
                log.warning("Could not analyse attached image '%s' — vision "
                            "provider and local Florence-2 both failed", name)
                notes.append(
                    f'[Attached image "{name}" — the visual model could '
                    f'not analyze it.]')
        elif kind == "text" and data:
            text = str(data)[:8000]
            notes.append(
                f'[Attached file "{name}" — content:\n{text}\n'
                f'--- end of file ---]')
        else:
            notes.append(
                f'[Attached file "{name}" — binary or unreadable content, '
                f'not analyzed.]')
    return notes


def _tool_usage_snapshot(used: list | None = None) -> dict:
    """What the chat has to work with, and what it just used.

    The chat page shows this above the box, the way it shows the memory anchors,
    because "why did it not search the web for me" is otherwise unanswerable:
    the user cannot see whether a tool exists, which MCP servers are connected,
    or whether the model chose not to call anything. Counts for what is
    available, names for what was called.
    """
    skills = 0
    mcp_servers: list[str] = []
    mcp_tools = 0
    try:
        from backend.skills.registry import skill_registry
        enabled = skill_registry.enabled_list_all()
        skills = sum(1 for s in enabled if s.category != "mcp")
    except Exception as e:  # noqa: BLE001
        log.debug("Could not read the skill catalogue: %s", e)
    try:
        from backend.mcp_client.manager import mcp_manager
        for server in mcp_manager.status().get("servers") or []:
            if server.get("state") != "ready":
                continue
            mcp_servers.append(str(server.get("name") or server.get("id")))
            mcp_tools += int(server.get("tool_count") or 0)
    except Exception as e:  # noqa: BLE001
        log.debug("Could not read the MCP status: %s", e)
    calls: list[str] = []
    for entry in used or []:
        name = str((entry or {}).get("tool") or "").strip()
        if name and name not in calls:
            calls.append(name)
    return {"skills": skills, "mcpServers": mcp_servers,
            "mcpTools": mcp_tools, "used": calls}


def _announce_turn(params: dict, message: str, reply: str) -> None:
    """Tell the other surfaces about a chat turn. Never raises.

    Two pushes, because they are two bubbles: what the user said and what came
    back. The reply is skipped when it is a provider error, since that is a
    failure to report to the caller rather than something to post into the
    conversation.

    `source` decides both the badge and whether to announce at all. A turn the
    chat page sent is declared `dashboard`, and is not announced, because that
    page has already drawn both bubbles itself — announcing it would show the
    whole exchange twice.
    """
    try:
        from backend import chat_sources
    except Exception as e:  # noqa: BLE001
        log.debug("chat source registry unavailable: %s", e)
        return
    try:
        source = chat_sources.normalise(params.get("source"))
        if not chat_sources.should_announce(source):
            return
        server = get_server()
        if server is None:
            return
        stamp = time.time()
        base = {**chat_sources.describe(source), "timestamp": stamp}
        server.broadcast_nowait("chat.push", {
            **base, "role": "user", "content": message,
        })
        text = (reply or "").strip()
        if text and not text.startswith(("[Not connected:", "[Provider")):
            server.broadcast_nowait("chat.push", {
                **base, "role": "assistant", "content": text,
            })
    except Exception as e:  # noqa: BLE001
        # An announcement is a courtesy to the other surfaces; a failure here
        # must not turn a working turn into an error for its caller.
        log.debug("could not announce the chat turn: %s", e)


def _pending_for(params: dict) -> list[dict]:
    """Approvals this turn left waiting, described for a caller to render.

    Only the ones raised by *this* conversation, so a bot is never handed
    another chat's decision to answer — and, more to the point, is never shown
    a button that would let it.

    The shape matches what an `action.approvalRequest` broadcast carries, so a
    bridge that builds its buttons from one can build them from the other.
    """
    try:
        from backend.approvals import pending
        from backend import chat_sources, chat_context
        ambient = chat_context.origin()
        source = chat_sources.normalise(params.get("source")
                                        or ambient.get("source"))
        conversation = str(params.get("conversation")
                           or params.get("conversationId")
                           or ambient.get("conversation") or "")
        if not conversation:
            # No conversation means nothing scoped to answer, so report none
            # rather than every queued request on the machine.
            return []
        out = []
        for entry in pending.for_conversation(source, conversation):
            out.append({
                "approval_id": entry["approval_id"],
                "action_type": entry["action_type"],
                "kind": entry["kind"],
                "name": entry["action_type"],
                "grantable": entry["grantable"],
                "command": entry["command"],
            })
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("could not gather pending approvals: %s", e)
        return []


async def run_chat_pipeline(
    message: str,
    params: dict | None = None,
    *,
    persona: str | None = None,
    tools: list[str] | None = None,
    record: bool = True,
    announce: bool = True,
    max_tool_rounds: int | None = None,
    force_role: str | None = None,
    provider: object | None = None,
) -> dict:
    """The one place a turn is run, for every way in.

    The Chat page, the floating character, voice, the bots, scheduled tasks,
    swarm agents and code edits all arrive here. That is deliberate: a
    capability added to this pipeline reaches all of them, and one added
    somewhere else reaches whichever path happened to be edited. The swarm used
    to call the provider directly, so it had no tools at all.

    persona          replaces the configured chat prompt as the base. The
                     injected context is still appended, so a persona changes
                     who is answering, not what it can find out.
    tools            narrows the tool catalogue. None means every enabled skill,
                     which is what chat, the character and the bots use.
    record           False for a turn that is a task rather than a conversation:
                     skips the chat history, the journal, the memory writes and
                     the mood events.
    announce         False to leave the character alone (swarm workers).
    max_tool_rounds  overrides the caller's default of 5.
    force_role       picks the routing role directly instead of classifying the
                     message.
    provider         an already-resolved provider. None asks the registry.
    """
    params = params or {}
    from backend.config import config

    # Who is asking travels beside the call, not inside it. A tool call reaches
    # the approval gate with only its own arguments, and an approval has to be
    # answerable by the chat that raised it — so the origin is set once here,
    # for the whole turn, and read where the decision is made.
    try:
        from backend import chat_context
        chat_context.set_origin(params.get("source"),
                                params.get("conversation")
                                or params.get("conversationId"))
    except Exception as e:  # noqa: BLE001
        log.debug("could not set the turn origin: %s", e)

    # Compaction shares the provider with chat. Mark the turn so that
    # background summarization yields instead of making the user's next message
    # queue behind it on a single-generation local model.
    try:
        from backend.memory.compaction import note_activity
        note_activity()
    except Exception:
        pass

    # Character shows the THINKING animation while the LLM works
    if announce and _engine_ref is not None:
        _engine_ref._chat_busy = True
        try:
            _engine_ref.sig_agent_state.emit("thinking")
        except Exception:
            pass
    try:
        response = await _run_chat_pipeline_inner(
            message, params, persona=persona, tools=tools, record=record,
            max_tool_rounds=max_tool_rounds, force_role=force_role,
            provider=provider)
        # The inner pipeline reports what the model actually returned. Only
        # here does an empty answer become something to show the user, because
        # a caller like the code editor has to be able to tell "the model said
        # nothing" apart from "the model wrote this sentence".
        if isinstance(response, dict) and not (response.get("response") or "").strip():
            response["response"] = "I couldn't process that request."
        # Surface provider/connection failures as the ERROR character state
        text = response.get("response", "") if isinstance(response, dict) else ""
        if record:
            try:
                from backend.character.mood import mood_engine
                if text.startswith(("[Not connected:", "[Provider")):
                    mood_engine.event("task_failure")
                else:
                    mood_engine.event("chat_reply")
            except Exception:
                pass
            # The journal write moved into `_run_chat_pipeline_inner`, next to
            # the history and memory writes. It was here, which meant only turns
            # arriving through this wrapper were journaled — the swarm and the
            # code page were not — and adding it to the inner function without
            # removing it here would have journaled every chat turn TWICE, since
            # this function calls that one.
        if (announce and text.startswith(("[Not connected:", "[Provider"))
                and _engine_ref is not None):
            try:
                _engine_ref.sig_agent_state.emit("error")
            except Exception:
                pass
        return response
    finally:
        if announce and _engine_ref is not None:
            _engine_ref._chat_busy = False


async def _speak_reply(text: str, voice: str | None = None) -> dict:
    """Speak text with the SPEAKING character animation, then reset.

    voice None means "use whatever voice.tts_voice / voice.kokoro_voice is
    configured" — the previous hardcoded default meant the Edge voice setting
    was ignored on every path that reached the audio output.
    """
    if _engine_ref is not None:
        _engine_ref._voice_busy = True
        try:
            _engine_ref.sig_agent_state.emit("speaking")
        except Exception:
            pass
    try:
        from backend.voice.tts import speak
        return await speak(text, voice)
    finally:
        if _engine_ref is not None:
            _engine_ref._voice_busy = False
            try:
                _engine_ref.sig_agent_state.emit("idle")
            except Exception:
                pass


def _record_scope(record):
    """Unpack ``record`` into three booleans: (history, memory, journal).

    ``record`` can be:
    - ``True``  → write everything
    - ``False`` → write nothing
    - a set of strings like ``{"history", "memory", "journal"}`` → write only
      the named categories.  A swarm worker, for instance, passes
      ``{"memory", "journal"}`` so its chatter doesn't flood the user's
      chat history but the knowledge and timeline are still captured.
    """
    if isinstance(record, set):
        return ("history" in record,
                "memory" in record,
                "journal" in record)
    flag = bool(record)
    return flag, flag, flag


async def _run_chat_pipeline_inner(
    message: str,
    params: dict | None = None,
    *,
    persona: str | None = None,
    tools: list[str] | None = None,
    record: bool = True,
    max_tool_rounds: int | None = None,
    force_role: str | None = None,
    provider: object | None = None,
) -> dict:
    from backend.config import config

    # params is optional; several paths below read from it unconditionally.
    params = params or {}

    # The auto-forge path fires from deep inside the tool loop, where the only
    # thing in hand is a function name the model invented. Publishing the
    # user's own request here is what lets it search the web for the capability
    # instead of the name — see backend/skills/tool_loop.py.
    try:
        config.set("_forge", "request", value=str(message or "")[:600])
    except Exception:
        pass

    # Prompt guard check
    if config.get("safety", "prompt_guard", default=True):
        from backend.safety.prompt_guard import sanitize
        sanitized, blocked = sanitize(message)
        if blocked:
            return {"response": sanitized, "tokens": 0, "conversationId": None}

    try:
        from backend.providers.registry import get_provider
        from backend.memory.chat_history import chat_history
        from backend.skills.tool_loop import chat_with_tools
        from backend.safety.egress_monitor import egress

        # Egress guard: scrub secrets before anything leaves the machine
        if config.get("safety", "egress_guard", default=True):
            message, _hits = egress.scrub(message)

        provider = provider or get_provider()
        egress.record("chat.send", {
            "provider": getattr(provider, "provider_id", "?"),
            "payload_chars": len(message),
        })
        agent_name = config.agent_name
        if persona:
            # A caller with its own identity: a swarm agent's role, or the code
            # editor's output contract.
            sys_prompt = persona
        else:
            sys_prompt = config.get("chat", "system_prompt", default="") or ""
            sys_prompt = sys_prompt.replace("{agent_name}", agent_name)
            if agent_name != "Addled":
                # also fix persisted prompts that hardcode the old name
                sys_prompt = sys_prompt.replace("Addled", agent_name)
            if not sys_prompt.strip():
                sys_prompt = (f"You are {agent_name}, a helpful AI desktop "
                              "companion with access to system tools.")
            capabilities_ctx = (
                "You are deeply integrated into the Addled desktop app with real tools and background services:\n"
                "- Shell: Run Windows PowerShell 5.1 commands on the user's own PC (`run_command`) — read and write files, inspect processes, run git/pip/npm/build commands. You DO have access to the user's system through this tool; never tell the user you cannot run commands or lack system access. Chain with ';' (not '&&'). Safe commands run immediately; a destructive one (delete, format, shutdown, restart) asks the user to approve it first, so say that it is waiting for approval rather than that you are unable to do it.\n"
                "- Calendar: Add events (`calendar_add`), list/check events (`calendar_list`), delete events (`calendar_delete`).\n"
                "- Task Scheduler: Background daemon runs 24/7. Schedule tasks/reminders (`task_schedule`), list scheduled tasks (`task_list`), cancel tasks (`task_cancel`).\n"
                "- Desktop & Screen: Inspect screen (`screen_read`), click/type (`desktop_click`, `desktop_type`), scroll, manage windows, control volume/brightness.\n"
                "- Browser: Navigate web pages, click, type, extract content (`browser_open`, `browser_click`, `browser_extract`).\n"
                "- Files & Workspace: Read, write, and search workspace files (`read_file`, `write_file`, `search_files`).\n"
                "- Memory: Remember facts (`remember`), recall past notes (`recall`).\n"
                "When asked about abilities or asked to check/manage calendar, schedule, tasks, reminders, desktop, files, or to run a command, acknowledge these capabilities and call the appropriate tool.\n"
                "- Permission: a tool that guards something destructive will not run the first time. The result comes back marked `requires_approval` with a message. When that happens, say plainly what you want to do and ask the user to allow it — name the tool and what it will touch, in one or two sentences, and stop there. Do not claim you are unable to do it and do not silently try another route. How they answer depends on where they are: in this app they click Allow on the card above the composer or on a button in your message, and on a bot (Telegram, Discord, WhatsApp) they get a button or reply \"yes\". Ask, then wait. If the request is not answered in time it stays queued, so do not repeat it forever — say it is still waiting once and let them come back to it.\n"
            )
            sys_prompt = sys_prompt + "\n\n" + capabilities_ctx
        # Context window sizing: local models get a lean 6-message (3-turn) window
        # so remaining tokens belong to memory and tools; cloud gets 20 turns.
        provider_id = getattr(provider, "provider_id", "") or ""
        default_context_len = 6 if provider_id == "local" else 20
        context_len = int(config.get("chat", "context_messages", default=default_context_len))
        context = chat_history.get_context(max_messages=context_len)

        # Context-aware query expansion for long-term recall: if message is a short follow-up
        recall_query = message
        words = (message or "").split()
        short_indexicals = {"why", "why?", "explain", "continue", "how so", "what about", "and then",
                            "kenapa", "kenapa?", "jelaskan", "lanjutkan", "lalu", "mengapa", "bagaimana"}
        clean_msg = (message or "").strip().lower()
        if (len(words) < 5 and len(message or "") < 25) or clean_msg in short_indexicals:
            prev_user_text = ""
            for turn in reversed(context):
                if turn.get("role") == "user" and turn.get("content") != message:
                    prev_user_text = turn.get("content", "")
                    break
            if prev_user_text:
                recall_query = f"{prev_user_text}\n{message}"

        # Core memory: durable facts the agent saved about the user. Only the
        # ones that relate to what was just said — an unrelated fact is not
        # context, it is something the model can end up answering instead of the
        # question (see backend/memory/relevance.py).
        from backend.memory import relevance
        facts_ctx = await relevance.facts_block(message)
        if facts_ctx:
            sys_prompt = sys_prompt + "\n\n" + facts_ctx

        # User model: learned profile (preferences, rituals, hours, tone)
        from backend.memory.user_profile import build_profile_context
        profile_ctx = build_profile_context()
        if profile_ctx:
            sys_prompt = sys_prompt + "\n\n" + profile_ctx

        # Episodic timeline: recent day summaries (persistent identity), gated
        # like the facts — "yesterday we..." is worth its tokens only when the
        # day is about this.
        timeline_ctx = await relevance.timeline_block(message)
        if timeline_ctx:
            sys_prompt = sys_prompt + "\n\n" + timeline_ctx

        # Wiki: the maintained factual layer. Pages are already distilled from
        # the user's own sources, so they outrank guesswork.
        wiki_ctx = None
        if config.get("wiki", "enabled", default=True):
            try:
                from backend.wiki.query import build_wiki_context
                wiki_ctx = build_wiki_context(message)
                if wiki_ctx:
                    sys_prompt = sys_prompt + "\n\n" + wiki_ctx
            except Exception as e:
                log.debug("wiki context failed: %s", e)

        # Procedures: how this kind of task was done successfully before. Only
        # offered when it is genuinely close to the task in hand — a recipe for
        # something else is worse than no recipe.
        sop_ctx = None
        try:
            from backend.sop.match import build_sop_context
            sop_ctx = build_sop_context(message)
            if sop_ctx:
                sys_prompt = sys_prompt + "\n\n" + sop_ctx
        except Exception as e:
            log.debug("procedure context failed: %s", e)

        # Session continuity: recent session summaries (long-run memory), gated
        # the same way
        session_ctx = await relevance.summaries_block(message)
        if session_ctx:
            sys_prompt = sys_prompt + "\n\n" + session_ctx

        # Rolling conversation continuity (long chats — compaction summaries)
        from backend.memory.compaction import build_rolling_context
        rolling_ctx = build_rolling_context()
        if rolling_ctx:
            sys_prompt = sys_prompt + "\n\n" + rolling_ctx

        # Long-term recall: inject relevant past conversation turns
        # (semantic + hybrid when enabled; legacy hash path otherwise)
        from backend.memory.recall import (build_memory_context,
                                           build_memory_context_hybrid)
        if config.get("memory", "semantic_embeddings", default=True):
            memory_ctx = await build_memory_context_hybrid(recall_query)
        else:
            memory_ctx = build_memory_context(recall_query)
        if memory_ctx:
            sys_prompt = sys_prompt + "\n\n" + memory_ctx

        # Memory anchors for the chat UI: which memories were injected
        try:
            anchors = []
            if memory_ctx:
                anchors.append({"type": "recall", "text": memory_ctx[:300]})
            if timeline_ctx:
                anchors.append({"type": "timeline", "text": timeline_ctx[:300]})
            if facts_ctx:
                anchors.append({"type": "facts", "text": facts_ctx[:300]})
            if wiki_ctx:
                anchors.append({"type": "wiki", "text": wiki_ctx[:300]})
            if sop_ctx:
                anchors.append({"type": "procedure", "text": sop_ctx[:300]})
            if rolling_ctx:
                anchors.append({"type": "rolling", "text": rolling_ctx[:300]})
            if session_ctx:
                anchors.append({"type": "session", "text": session_ctx[:300]})
            if anchors:
                get_server().broadcast_nowait("memory.anchors",
                                               {"anchors": anchors})
        except Exception:
            pass

        # What this turn has to work with. Sent before the model runs so the
        # chat page can show it while the user waits, then re-sent afterwards
        # with the tools the turn actually called.
        try:
            get_server().broadcast_nowait("chat.tools", _tool_usage_snapshot())
        except Exception:
            pass

        # Temporal fact triples (Graphiti-lite) — relational questions only
        rel_kw = ("decide", "decided", "prefer", "preference", "favorite",
                  "project", "working on", "what is my", "name of", "use for",
                  "remember", "know about", "related", "context", "history",
                  "last time", "why did", "who is", "where is", "which file",
                  "document", "notes", "note about")
        triples = []
        if any(k in (recall_query or "").lower() for k in rel_kw):
            try:
                from backend.memory.knowledge_graph import kg
                triples = kg.semantic_search(recall_query, top_k=8)
                if triples:
                    lines = "\n".join(
                        f"- {t['subject']} {t['relation']} {t['object']}"
                        for t in triples)
                    sys_prompt += ("\n\n[Saved facts] Things on record about the user:\n" +
                                   lines + "\nUse them when relevant.")
            except Exception as e:
                log.debug("triple lookup failed: %s", e)

        # Relation graph: what those items are attached to. This is what lets an
        # answer say "and the file for that is ..." instead of stopping at the
        # fact, which is the whole point of recording relations.
        try:
            if triples and config.get("links", "enabled", default=True):
                from backend.memory.links import link_store
                depth = int(config.get("links", "related_depth", default=1))
                cap = int(config.get("links", "related_inject", default=5))
                seen: set[tuple] = set()
                related_lines: list[str] = []
                for seed in triples[:4]:
                    if seed.get("id") is None:
                        continue
                    for row in link_store.related("triple", seed["id"],
                                                  depth=depth, limit=6):
                        key = (row["kind"], row["ref_id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        rel = (row.get("via") or {}).get("rel", "relates_to")
                        label = f"{row['kind']}: {row['ref_id']}"
                        if row.get("label"):
                            label += f" ({row['label']})"
                        related_lines.append(f"- {label} — {rel}")
                        if len(related_lines) >= cap:
                            break
                    if len(related_lines) >= cap:
                        break
                if related_lines:
                    sys_prompt += ("\n\n[Related] Connected in the user's memory graph:\n" +
                                   "\n".join(related_lines) +
                                   "\nMention these only when they help the answer.")
        except Exception as e:
            log.debug("related lookup failed: %s", e)

        # Memory-grounded conversation: volunteer relevant past organically
        sys_prompt += ("\n\nIf a saved fact, a previous conversation, or a "
                       "recent day's summary is clearly relevant to this "
                       "conversation, mention it naturally (e.g. 'last time "
                       "we...', 'you mentioned before that...'). Do not force "
                       "it when nothing fits.")

        # Language mirroring. This line is the general statement; the binding one
        # is `reply_directive` below, which lands *after* the tool catalogue in
        # the turn being answered. Kept because a cloud model honours it and it
        # costs a line.
        sys_prompt += ("\n\nAlways reply in the same language the user "
                       "writes or speaks in.")

        # Where a file goes. Nothing used to say, so asked for a file "in the
        # workspace" the model wrote `workspace\notes.txt` — inventing a
        # subdirectory inside the root it had already been given — and then
        # reported that longer path as the location, which at least matched
        # where the file really was.
        try:
            from backend.workspace import root as _workspace_root
            folder = _workspace_root()
            if folder:
                sys_prompt += (f"\n\nFiles: the workspace root is {folder}. "
                               "Give file paths relative to it — "
                               "'notes.txt', not 'workspace/notes.txt'.")
        except Exception as e:  # noqa: BLE001
            log.debug("workspace hint skipped: %s", e)

        user_messages = [
            {"role": m["role"], "content": m["content"]} for m in context
        ]

        # Does this look like code/file work? Shared by project injection and by
        # the model router (substantive code work leans on the reasoning role).
        code_hint = any(k in message.lower() for k in
                        (".py", ".js", ".ts", "file", "function",
                         "class ", "code", "bug", "repo", "import"))

        # Project awareness: inject matching code snippets when the message
        # looks code/file-related
        if config.get("project", "inject_into_chat", default=True):
            try:
                if code_hint:
                    from backend.project.indexer import search_project
                    hits = search_project(message, top_k=4)
                    if hits:
                        lines = "\n".join(
                            f"- {h['path']}: {h['snippet'][:180]}"
                            for h in hits)
                        user_messages.insert(0, {"role": "user", "content":
                            "[Project context] Matching code in the indexed "
                            "workspace:\n" + lines +
                            "\nUse them when relevant."})
            except Exception as e:
                log.debug("project injection failed: %s", e)

        # Live screen awareness: let the model know what the observer sees
        screen_note = None
        if _engine_ref:
            info = _engine_ref.last_screen_info()
            asks_about_screen = any(
                kw in message.lower() for kw in
                ("what do you see", "what can you see", "screen", "desktop",
                 "what's on my screen", "what am i looking at"))
            detail = (info or {}).get("detail")
            if asks_about_screen and not detail:
                # User explicitly asks what it sees and there is no recent
                # vision result — capture a fresh one right now.
                detail = await _engine_ref.fresh_screen_detail()
            ctx = (info or {}).get("context")
            if detail or ctx not in (None, "unknown", "unchanged", "private"):
                parts = []
                if ctx not in (None, "unknown", "unchanged", "private"):
                    parts.append(f"the user's current activity context: {ctx}")
                if detail:
                    parts.append(f"recent screen description: {detail}")
                if parts:
                    screen_note = ("[Live screen awareness] " + "; ".join(parts) +
                                   ". Use this when the user asks what you can see "
                                   "or what they are doing.")
        if screen_note:
            user_messages.insert(0, {"role": "user", "content": screen_note})

        # Past-screen memory: "what was I doing 20 minutes ago?"
        import re as _re
        ago_m = _re.search(r"(\d+)\s*(minutes?|mins?|hours?)\s*ago", message.lower())
        asks_past = bool(ago_m) or any(
            kw in message.lower() for kw in
            ("what was i doing", "what was i working on", "what did i just do",
             "earlier", "a moment ago"))
        if asks_past:
            ago_seconds = None
            when = "earlier"
            if ago_m:
                n, unit = int(ago_m.group(1)), ago_m.group(2)
                ago_seconds = n * (3600 if unit.startswith("hour") else 60)
                when = ago_m.group(0)
            try:
                from backend.memory.snapshot_store import snapshot_store
                past_desc = await snapshot_store.describe(ago_seconds)
                if past_desc:
                    past_note = (f"[Past screen memory] Around '{when}', your screen "
                                 f"showed: {past_desc}. Use this when the user asks "
                                 "what they were doing earlier.")
                    user_messages.insert(0, {"role": "user", "content": past_note})
            except Exception as e:
                log.debug("Past-screen recall failed: %s", e)

        # Attachments: images go through the visual model, files get
        # inlined as text — the main model sees the results as context.
        attachments = params.get("attachments") or []
        if attachments:
            try:
                from backend.providers import router as _router
                _vision_model = _router.resolve_model(
                    getattr(provider, "provider_id", ""), "vision")
                att_notes = await _analyze_attachments(
                    provider, attachments, vision_model=_vision_model)
                if att_notes:
                    user_messages.insert(0, {
                        "role": "user",
                        "content": "\n".join(att_notes) + "\n\n"
                        "Use the attachment information above when answering "
                        "the user's message.",
                    })
            except Exception as e:
                log.debug("Attachment analysis failed: %s", e)

        user_messages.append({"role": "user", "content": message})

        # Optional guideline packs (ponytail / Karpathy). They apply to
        # code-shaped requests unless a pack is set to scope "always".
        try:
            from backend.guidelines import inject as guidelines
            gblock = guidelines.system_block(code_task=code_hint)
            if gblock:
                sys_prompt = f"{sys_prompt}\n\n{gblock}"
                log.debug("Injected %d chars of working guidelines",
                          len(gblock))
        except Exception as e:
            log.debug("guideline injection failed: %s", e)

        # Task-aware model routing. "default_model" stays the baseline, so an
        # unconfigured provider behaves exactly as it did before routing.
        role, route_model = "chat", None
        try:
            from backend.providers import router
            explicit_role, message = router.split_role_tag(message)
            image_only = bool(attachments) and all(
                (a or {}).get("kind") == "image" for a in attachments)
            role, route_model = router.pick(
                getattr(provider, "provider_id", "unknown"),
                message,
                has_attachments=bool(attachments),
                image_only=image_only,
                code_hint=code_hint,
                # A caller that knows the shape of its work picks the role
                # directly: a swarm worker is always reasoning, a code edit is
                # always code, even when the instruction is one short line.
                force_role=explicit_role or force_role,
            )
            # An explicit model wins over the routed one. This is how a roster
            # agent pins its own model — a grunt desk on the local model while
            # the reasoning desk runs on a cloud one — without every other
            # surface having to know about it.
            pinned = str((params or {}).get("model") or "").strip()
            if pinned:
                route_model = pinned
            log.debug("Chat route: role=%s model=%s provider=%s",
                      role, route_model,
                      getattr(provider, "provider_id", "?"))
        except Exception as e:
            log.debug("Model routing failed, using provider default: %s", e)

        # The reply language, stated where it is read last. Everything above is
        # English, and the tool catalogue that follows is thousands of tokens of
        # it appended to the user's own message — which is what a small model
        # answers in. Asked in Indonesian, Addled replied in English.
        # See backend/language.py.
        try:
            from backend.language import reply_directive
            reply_lang = reply_directive(message)
        except Exception as e:
            log.debug("reply-language detection failed: %s", e)
            reply_lang = ""

        # Use the provider-agnostic tool-use loop
        result = await chat_with_tools(
            provider=provider,
            messages=user_messages,
            system_prompt=sys_prompt,
            max_tool_rounds=(max_tool_rounds
                             or params.get("maxToolRounds", 5)),
            model=route_model,
            tools=tools,
            reply_directive=reply_lang,
        )

        response_text = result.get("response", "")
        tool_rounds = result.get("tool_rounds", 0)
        tool_results = result.get("tool_results", [])

        # Named, so the chat page can say "used web_search" rather than only
        # how many. A turn that called nothing says that too, which is the
        # answer to "why did it not use the tool I know it has".
        try:
            get_server().broadcast_nowait("chat.tools",
                                          _tool_usage_snapshot(tool_results))
        except Exception:
            pass

        if response_text and not response_text.startswith("[Provider:") and not response_text.startswith("[Not connected:"):
            if record:
                # `record` may be True/False or a set naming what to keep, so a
                # caller can say "remember this, but do not put it in my chat".
                # A swarm flow runs five agents; without that distinction their
                # chatter buries the user's own conversation, and the choice was
                # previously all-or-nothing.
                write_history, write_memory, write_journal = _record_scope(record)
                if write_history:
                    chat_history.add_message("user", message)
                    chat_history.add_message("assistant", response_text,
                        tokens={"in": result.get("tokens", 0), "out": 0})
                if write_memory:
                    # Remember for the long term. Only a real exchange: a
                    # refusal or a provider error is not knowledge, and memory
                    # full of failure strings makes recall worse, not better.
                    from backend.memory.recall import remember_async
                    await remember_async("user", message)
                    await remember_async("assistant", response_text)
                if write_journal:
                    from backend.memory.journal import record as journal_record
                    journal_record("user", message)
                    journal_record("assistant", response_text[:500])
            return {
                "response": response_text,
                "tokens": result.get("tokens", 0),
                "conversationId": (chat_history.current_conversation_id
                                   if record else None),
                "toolRounds": tool_rounds,
                "toolResults": len(tool_results),
                "memoryRecall": bool(memory_ctx),
                "role": role,
                "model": route_model or "",
            }
        return {"response": response_text,
                "tokens": result.get("tokens", 0), "conversationId": None,
                "role": role, "model": route_model or ""}
    except Exception as e:
        log.exception("Chat failed")
        return {"response": f"[Not connected: {e}] Configure an AI provider in Settings.", "tokens": 0, "conversationId": None}


async def start_ws_server(host: str = "127.0.0.1", port: int = 9876):
    """Start the singleton WebSocket server."""
    _register_default_handlers()
    await _server.start(host, port)


def _autostart_state() -> bool:
    """Whether Addled's Startup shortcut exists. Never raises."""
    try:
        from backend.onboarding.wizard import autostart_enabled
        return autostart_enabled()
    except Exception:  # noqa: BLE001
        return False

def _register_default_handlers():
    """Register built-in JSON-RPC handlers."""

    # Bring the saved swarm agents back before anything can ask for them, so
    # the Agents page shows the user's desks rather than an empty room the
    # first time it is opened after a restart. Best-effort: a broken roster
    # must not stop the server from starting.
    try:
        from backend.swarm import roster as _roster
        _roster.seed_if_empty()
        from backend.swarm.orchestrator import swarm as _swarm
        restored = _swarm.load_roster()
        if restored:
            log.info("Swarm roster restored: %d agent(s)", restored)
    except Exception as e:  # noqa: BLE001
        log.debug("could not restore the swarm roster: %s", e)

    async def system_status(params: dict, ws) -> dict:
        from backend.config import config
        import time
        engine_state = "unknown"
        char_state = "idle"
        if _engine_ref:
            engine_state = _engine_ref.state.name.lower() if hasattr(_engine_ref, 'state') else "running"
        return {
            "engineState": engine_state,
            "provider": config.active_provider,
            "agentName": config.agent_name,
            "characterState": char_state,
            # The real state of the Startup shortcut, not the stored preference:
            # the file is the truth, and it can be removed by hand.
            "autostart": _autostart_state(),
            "uptime": int(getattr(_engine_ref, '_tick_count', 0) * 5) if _engine_ref else 0,
        }

    async def system_get_providers(params: dict, ws) -> dict:
        from backend.config import config
        from backend.providers.registry import list_available
        return {
            "providers": list_available(),
            "active": config.active_provider,
            "selected": config.selected_provider,
        }

    async def system_rtk_status(params: dict, ws) -> dict:
        """Whether the token-saver binaries are present, and how far an
        install has got. The dashboard draws the Install button from this."""
        from backend.tools import rtk
        return rtk.status()

    async def system_rtk_install(params: dict, ws) -> dict:
        """Download rtk + ripgrep, on the user's explicit instruction.

        Returns as soon as the download starts; progress arrives on
        `system.rtkProgress`, and `system.rtkStatus` reports the outcome.
        """
        from backend.remote import policy
        from backend.tools import rtk
        return await rtk.install(
            force=bool(params.get("force")),
            with_rg=bool(params.get("rg", True)),
            remote=policy.is_remote(ws),
        )

    async def system_uv_status(params: dict, ws) -> dict:
        """Whether uv/uvx are available to launch PyPI-packaged MCP servers.

        The market refuses those entries with "needs 'uvx' on PATH (install
        uv)", and this is what lets the dashboard offer to act on that instead
        of leaving it as a reason with no remedy.
        """
        from backend.tools import uv
        return uv.status()

    async def system_uv_install(params: dict, ws) -> dict:
        """Download uv, on the user's explicit instruction.

        Returns as soon as the download starts; progress arrives on
        `system.uvProgress`, and `system.uvStatus` reports the outcome.
        """
        from backend.remote import policy
        from backend.tools import uv
        return await uv.install(force=bool(params.get("force")),
                                remote=policy.is_remote(ws))

    async def desktop_status(params: dict, ws) -> dict:
        from backend.actions.desktop_control import desktop_control
        from backend.config import config
        return {
            "allow_input": bool(config.get("desktop", "allow_input",
                                           default=False)),
            "require_session_approval": bool(config.get(
                "desktop", "require_session_approval", default=True)),
            "granted": desktop_control.granted(),
        }

    async def desktop_grant(params: dict, ws) -> dict:
        from backend.actions.desktop_control import desktop_control
        desktop_control.grant()
        return {"success": True, "granted": True}

    async def desktop_revoke(params: dict, ws) -> dict:
        from backend.actions.desktop_control import desktop_control
        desktop_control.revoke()
        return {"success": True, "granted": False}

    async def mcp_list(params: dict, ws) -> dict:
        """Configured MCP servers and their live connection state."""
        from backend.mcp_client.manager import mcp_manager
        try:
            return mcp_manager.status()
        except Exception as e:
            log.debug("mcp.list failed: %s", e)
            return {"enabled": False, "servers": [], "error": str(e)}

    async def mcp_add(params: dict, ws) -> dict:
        from backend.mcp_client.manager import mcp_manager
        try:
            return mcp_manager.add(params.get("server") or params)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_update(params: dict, ws) -> dict:
        from backend.mcp_client.manager import mcp_manager
        server_id = str(params.get("id") or "")
        if not server_id:
            return {"success": False, "error": "missing server id"}
        try:
            return mcp_manager.update(server_id, params.get("patch") or {})
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_remove(params: dict, ws) -> dict:
        from backend.mcp_client.manager import mcp_manager
        server_id = str(params.get("id") or "")
        if not server_id:
            return {"success": False, "error": "missing server id"}
        try:
            return await mcp_manager.remove(server_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_connect(params: dict, ws) -> dict:
        from backend.mcp_client.manager import mcp_manager
        server_id = str(params.get("id") or "")
        if not server_id:
            return {"success": False, "error": "missing server id"}
        try:
            out = await mcp_manager.connect(server_id)
            return {**out, "status": mcp_manager.status()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_disconnect(params: dict, ws) -> dict:
        from backend.mcp_client.manager import mcp_manager
        server_id = str(params.get("id") or "")
        try:
            out = await mcp_manager.disconnect(server_id)
            return {**out, "status": mcp_manager.status()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_reload(params: dict, ws) -> dict:
        """Reconnect every configured server (after editing the list)."""
        from backend.mcp_client.manager import mcp_manager
        try:
            out = await mcp_manager.reload()
            return {"success": True, **(out or {}),
                    "status": mcp_manager.status()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_tools(params: dict, ws) -> dict:
        """The tools a connected server advertises, with their schemas."""
        from backend.mcp_client.manager import mcp_manager
        server_id = str(params.get("id") or "")
        try:
            return mcp_manager.tools(server_id)
        except Exception as e:
            return {"success": False, "tools": [], "error": str(e)}

    async def mcp_set_tool_approval(params: dict, ws) -> dict:
        """Grant or revoke standing permission for one tool on one server.

        The name is stored as the MCP approval key (`server::tool`), which is
        what `is_approved` reads, so the switch and the prompt agree about
        which tool is being allowed.
        """
        from backend.mcp_client import approval as mcp_approval
        from backend.approvals import policy
        server_id = str(params.get("id") or "").strip()
        tool = str(params.get("tool") or "").strip()
        allowed = bool(params.get("allowed", True))
        if not server_id or not tool:
            return {"success": False, "error": "id and tool are required"}
        if allowed:
            result = mcp_approval.approve_always(server_id, tool)
        else:
            mcp_approval.revoke(server_id, tool)
            result = {"success": True, "kind": policy.TOOL,
                      "name": mcp_approval.key(server_id, tool),
                      "allowed": False}
        if result.get("success"):
            from backend.mcp_client.manager import mcp_manager
            result["status"] = mcp_manager.status()
        return result

    async def mcp_search_market(params: dict, ws) -> dict:
        """Search the official MCP registry for servers to add."""
        from backend.mcp_client import market
        query = str(params.get("query") or "").strip()
        if not query:
            return {"success": False, "servers": [],
                    "error": "give something to search for"}
        try:
            candidates = await market.search(
                query, limit=int(params.get("limit") or 12))
            return {"success": True, "query": query, "servers": candidates,
                    "count": len(candidates), "registry": market.REGISTRY_URL}
        except Exception as e:
            return {"success": False, "servers": [], "error": str(e)}

    async def mcp_install(params: dict, ws) -> dict:
        """Add a server from the registry and connect it.

        ``env`` and ``headers`` carry the values for the variables the listing
        declared it needs. They are stored first, so the entry is judged against
        what we now hold rather than what the listing assumes nobody has.
        """
        from backend.mcp_client import market
        name = str(params.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "missing server name"}
        try:
            return await market.install(
                name,
                trusted=bool(params.get("trusted")),
                auto=bool(params.get("auto")),
                extra_args=params.get("args"),
                env=params.get("env"),
                headers=params.get("headers"),
                params=params.get("params"),
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_credentials(params: dict, ws) -> dict:
        """Which env vars, headers and query parameters we hold a value for.

        Passing `env`/`headers`/`params` stores values; an empty string for a
        name removes it, which is how the dashboard clears one. Values are never
        returned: the UI has no business reading back a credential it stored.
        """
        from backend.mcp_client import credentials
        env = params.get("env")
        headers = params.get("headers")
        queries = params.get("params")
        if env is None and headers is None and queries is None:
            return {"success": True, **credentials.known()}
        try:
            return {"success": True, **credentials.save(env, headers, queries)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_sweep(params: dict, ws) -> dict:
        """Disconnect servers the agent added that have gone unused."""
        from backend.mcp_client.manager import mcp_manager
        try:
            out = await mcp_manager.sweep_idle()
            return {"success": True, **(out or {}),
                    "status": mcp_manager.status()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def guidelines_state(params: dict, ws) -> dict:
        """Guideline pack status for the Settings panel."""
        from backend.guidelines import inject as guidelines
        try:
            return guidelines.describe()
        except Exception as e:
            log.debug("guidelines.state failed: %s", e)
            return {"enabled": False, "packs": {}, "error": str(e)}

    async def guidelines_refresh(params: dict, ws) -> dict:
        """Re-download the enabled guideline packs from upstream."""
        from backend.guidelines import inject as guidelines
        from backend.guidelines import store as guidelines_store
        try:
            out = await guidelines_store.refresh_if_stale(force=True)
            return {"success": True, **(out or {}),
                    "state": guidelines.describe()}
        except Exception as e:
            log.debug("guidelines.refresh failed: %s", e)
            try:
                state = guidelines.describe()
            except Exception:
                state = {}
            return {"success": False, "error": str(e), "state": state}

    async def models_catalog(params: dict, ws) -> dict:
        """Cached model lists per provider (what Settings shows)."""
        from backend.providers import model_catalog
        try:
            return model_catalog.catalog()
        except Exception as e:
            log.debug("models.catalog failed: %s", e)
            return {"providers": {}, "error": str(e)}

    async def models_refresh(params: dict, ws) -> dict:
        """Query providers for their current model lists."""
        from backend.providers import model_catalog
        pid = params.get("provider")
        try:
            if pid:
                out = await model_catalog.refresh(force=True, only=[pid])
            else:
                out = await model_catalog.refresh(force=True)
            return {"success": True, **out}
        except Exception as e:
            log.debug("models.refresh failed: %s", e)
            return {"success": False, "error": str(e),
                    "catalog": model_catalog.catalog()}

    async def models_routes(params: dict, ws) -> dict:
        """Effective model per task role — powers the Settings routing panel."""
        from backend.config import config
        from backend.providers import router
        pid = params.get("provider") or config.active_provider
        try:
            return {"routes": router.describe(pid),
                    "roles": list(router.ROLES)}
        except Exception as e:
            log.debug("models.routes failed: %s", e)
            return {"routes": {}, "roles": list(router.ROLES),
                    "error": str(e)}

    async def settings_get(params: dict, ws) -> dict:
        from backend.config import config
        from backend.remote.policy import is_remote, redact_settings
        section = params.get("section")
        if section:
            data = {section: config.get(section, default={})}
        else:
            # Ensure config is loaded before accessing private _data
            config._ensure_loaded()
            data = dict(config._data)
        if is_remote(ws):
            # With no section this returns the whole settings tree, which holds
            # every provider key, the Google refresh token, the email password
            # and the bot tokens. A remote browser gets the shape of the
            # settings, not the secrets.
            data = redact_settings(data)
        return {"settings": data}

    async def system_set_autostart(params: dict, ws) -> dict:
        """Turn "start with Windows" on or off.

        Not a plain settings.set: autostart is a shortcut in the Startup folder
        as well as a stored value, and a settings-only write would show the
        toggle as on while no shortcut existed. This owns both.
        """
        from backend.onboarding import wizard as onboarding
        return onboarding.set_autostart(bool(params.get("enabled")))

    async def settings_set(params: dict, ws) -> dict:
        from backend.config import config
        section = params.get("section")
        key = params.get("key")
        value = params.get("value")
        if section:
            if key is not None and key != "":
                config.set(section, key, value=value)
            else:
                # Top-level setting (e.g. agent_name)
                config.set(section, value=value)
        # Switching to/from the local provider starts or stops its server
        if section == "providers":
            try:
                from backend.local_llm.manager import local_llm
                await local_llm.apply_policy()
            except Exception as exc:
                log.debug("Local model policy after settings change: %s", exc)
        # Remote access and Tailscale take effect immediately rather than on the
        # next restart, because the user's next action is usually to try them.
        if section in ("remote", "tailscale"):
            warning = ""
            try:
                from backend.remote.gateway import gateway
                if gateway.enabled() and not gateway.is_running():
                    _ok, _error = await gateway.start()
                    warning = _error
                elif not gateway.enabled() and gateway.is_running():
                    await gateway.stop()
            except Exception as exc:
                log.debug("Remote gateway policy failed: %s", exc)
                warning = str(exc)
            try:
                from backend.tailscale.manager import tailscale
                await tailscale.apply_policy()
            except Exception as exc:
                log.debug("Tailscale policy failed: %s", exc)
            if warning:
                # Surfaced so the Remote page can explain why nothing opened.
                return {"success": True, "warning": warning}
        return {"success": True}

    async def workspace_status(params: dict, ws) -> dict:
        """The resolved view of the workspace: what is allowed right now.

        The raw settings are just two strings; whether they confine anything
        depends on the access mode, so this reports the outcome rather than the
        inputs.
        """
        from backend.workspace import describe
        info = describe()
        try:
            from backend.memory.session_context import session_context
            session_context._load()
            info["active_workspace"] = session_context.active_workspace or info.get("root", "")
        except Exception:
            info["active_workspace"] = info.get("root", "")
        return {"success": True, **info}

    # ---- Phase 3: Chat with prompt guard + provider integration ---------------

    async def chat_send(params: dict, ws) -> dict:
        message = params.get("message", "")
        if not message:
            return {"response": "I didn't catch that.", "tokens": 0, "conversationId": None}
        result = await run_chat_pipeline(message, params)
        reply = result.get("response", "") if isinstance(result, dict) else ""
        # Show the turn in every other surface that is watching the
        # conversation. A message typed on Telegram reached Addled and got an
        # answer the chat page never saw, because this function used to return
        # to its caller and tell nobody — so the two conversations drifted and
        # the app looked like it had ignored the phone. Announcing here rather
        # than in each bot bridge means voice, tasks and the character are
        # covered by the same line of code.
        _announce_turn(params, message, reply)
        # Hand the caller any decision this turn left waiting. The reply used
        # to carry only a count, so a bot was told "approval needed" with no id
        # to answer with — it could not build a button even if it wanted to.
        if isinstance(result, dict):
            try:
                result["pendingApprovals"] = _pending_for(params)
            except Exception as e:  # noqa: BLE001
                log.debug("could not list pending approvals: %s", e)
        # Rolling compaction: summarize the oldest turns in the background
        # once the conversation outgrows the context window.
        try:
            if reply and not reply.startswith(("[Not connected:", "[Provider")):
                from backend.memory.compaction import maybe_compact
                _spawn(maybe_compact())
        except Exception:
            log.debug("compaction dispatch failed", exc_info=True)
        # Auto-speak replies when voice.auto_tts is enabled (Settings → Voice)
        try:
            from backend.config import config
            if config.get("voice", "auto_tts", default=True):
                if reply and not reply.startswith(("[Not connected:", "[Provider")):
                    _spawn(_speak_reply(reply))
                    log.info("Auto-TTS: speaking chat reply (%d chars)", len(reply))
        except Exception:
            log.debug("Auto-TTS dispatch failed", exc_info=True)
        return result

    # ---- Phase 3: Action execution --------------------------------------------

    async def action_execute(params: dict, ws) -> dict:
        from backend.actions.executor import executor, ActionRequest
        action_type = params.get("type", "")
        action_params = params.get("params", {})
        if not action_type:
            return {"success": False, "error": "No action type specified"}
        if _engine_ref is not None:
            try:
                _engine_ref.sig_agent_state.emit("executing_action")
            except Exception:
                pass
        result = await executor.execute(ActionRequest(action_type=action_type, params=action_params))
        if _engine_ref is not None:
            try:
                _engine_ref.sig_agent_state.emit("error" if not result.success else "idle")
            except Exception:
                pass
        return {"success": result.success, "action_type": result.action_type,
                "summary": result.summary, "duration_ms": result.duration_ms,
                "error": result.error, "data": result.data}

    async def _resolve_approval_target(params: dict) -> tuple[str, dict | None]:
        """Find the approval an answer refers to.

        Two ways to name it. The dashboard sends `approvalId`, because it is
        looking at one card. A bot sends the conversation it is answering in,
        because the user typed "yes" and that word carries no id — so the
        request has to be found by *who asked*, which is what stops a message
        in one chat from releasing a command another chat asked for.
        """
        from backend.actions.executor import executor
        approval_id = str(params.get("approvalId") or "").strip()
        if approval_id:
            return approval_id, None
        source = params.get("source")
        conversation = params.get("conversation") or params.get("conversationId")
        if not conversation:
            return "", None
        entry = await executor.resolve_by_conversation(source, conversation)
        if not entry:
            return "", None
        return str(entry.get("approval_id") or ""), entry

    async def action_approve(params: dict, ws) -> dict:
        """Approve a pending destructive action, by id or by conversation."""
        from backend.actions.executor import executor
        from backend.actions import approval_notice
        approval_id, entry = await _resolve_approval_target(params)
        if not approval_id:
            return {"success": False,
                    "error": ("approvalId is required, or a source and "
                              "conversation to find the request this chat "
                              "raised")}
        result = await executor.approve(approval_id)
        approval_notice.remember(approval_id)
        return {"success": result.success, "action_type": result.action_type,
                "summary": result.summary, "error": result.error,
                "data": result.data, "approvalId": approval_id,
                "resolved": entry or {}}

    async def action_deny(params: dict, ws) -> dict:
        """Deny a pending destructive action, by id or by conversation."""
        from backend.actions.executor import executor
        from backend.actions import approval_notice
        approval_id, entry = await _resolve_approval_target(params)
        if not approval_id:
            return {"success": False,
                    "error": ("approvalId is required, or a source and "
                              "conversation to find the request this chat "
                              "raised")}
        result = executor.deny(approval_id)
        approval_notice.remember(approval_id)
        return {"success": result.success, "summary": result.summary,
                "error": result.error, "approvalId": approval_id,
                "resolved": entry or {}}

    async def action_pending(params: dict, ws) -> dict:
        """Approvals still waiting, for a dashboard that connected late."""
        from backend.actions.executor import executor
        pending = executor.pending_approvals()
        return {"success": True, "pending": pending, "count": len(pending)}

    async def approvals_list(params: dict, ws) -> dict:
        """Everything granted standing permission, plus what is still queued.

        The queued half comes from the notice module rather than the executor
        because it is the one that knows a request's kind and whether it may be
        granted — the executor only holds the action type and its parameters.
        """
        from backend.approvals import policy
        from backend.actions import approval_notice
        allowed = policy.list_allowed()
        return {"success": True, **allowed,
                "pending": approval_notice.pending(),
                "protected": sorted(policy.protected_names())}

    async def approvals_always_allow(params: dict, ws) -> dict:
        """Grant standing permission for one skill or tool.

        Named two ways. The dashboard sends `kind` and `name` because its card
        is about a specific thing. A bot sends only `approvalId`, because the
        button that carried it had 64 bytes and the name would not fit — so the
        kind and name are resolved from the record of that approval. Resolving
        it here rather than trusting the caller also means the name cannot be
        altered in transit to grant something the user was never asked about.
        """
        from backend.approvals import policy
        from backend.approvals import pending as pending_origins
        from backend.actions import approval_notice

        kind = str(params.get("kind") or "").strip()
        name = str(params.get("name") or "").strip()
        approval_id = str(params.get("approvalId") or "").strip()

        if not name and approval_id:
            entry = pending_origins.get(approval_id)
            if entry:
                kind = kind or entry.get("kind") or ""
                name = str(entry.get("action_type") or "")

        if not name:
            return {"success": False,
                    "error": ("name is required, or an approvalId whose "
                              "request is still known")}

        result = policy.always_allow(kind, name)
        if not result.get("success"):
            result.setdefault("success", False)
            return result

        if approval_id:
            from backend.actions.executor import executor
            await executor.approve(approval_id)
            approval_notice.remember(approval_id)
        return {**result, "allowed": True}

    async def approvals_revoke(params: dict, ws) -> dict:
        """Take standing permission back, so the prompt returns."""
        from backend.approvals import policy
        from backend.mcp_client import approval as mcp_approval
        kind = str(params.get("kind") or "").strip()
        name = str(params.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        result = policy.revoke(kind, name)
        # An MCP tool is spelled `server::tool`; drop the session grant too so
        # revoking from the card cannot leave the fast path still saying yes.
        if result.get("success") and kind == policy.TOOL and "::" in name:
            server_id, _, tool = name.partition("::")
            try:
                mcp_approval.revoke(server_id, tool)
            except Exception as e:  # noqa: BLE001
                log.debug("could not clear the session MCP grant: %s", e)
        return result

    # ---- Excel operations -----------------------------------------------------

    async def excel_read(params: dict, ws) -> dict:
        from backend.actions.excel_ops import excel_ops
        return await excel_ops.read(
            path=params.get("path", ""),
            sheet=params.get("sheet"),
            max_rows=params.get("maxRows", 500),
        )

    async def excel_write(params: dict, ws) -> dict:
        from backend.actions.excel_ops import excel_ops
        return await excel_ops.write(
            path=params.get("path", ""),
            sheet=params.get("sheet", "Sheet1"),
            cells=params.get("cells", []),
            grid=params.get("grid"),
        )

    # ---- Privacy + egress -----------------------------------------------------

    async def privacy_set_zones(params: dict, ws) -> dict:
        """Replace the privacy blackout zones: [{x, y, w, h}, ...]."""
        from backend.config import config
        zones = params.get("zones", []) or []
        try:
            for z in zones:
                int(z.get("x", 0)), int(z.get("y", 0)), int(z.get("w", 0)), int(z.get("h", 0))
        except (TypeError, ValueError, AttributeError):
            return {"success": False, "error": "zones must be [{x, y, w, h}, ...]"}
        config.set("observation", "privacy_zones", value=zones)
        return {"success": True, "zones": zones}

    async def privacy_list(params: dict, ws) -> dict:
        from backend.config import config
        return {
            "zones": config.get("observation", "privacy_zones", default=[]) or [],
            "excludedApps": config.get("safety", "privacy_excluded_apps", default=[]) or [],
        }

    async def privacy_exclude_app(params: dict, ws) -> dict:
        from backend.config import config
        app = (params.get("app") or "").strip()
        if not app:
            return {"success": False, "error": "app name is required"}
        excluded = list(config.get("safety", "privacy_excluded_apps", default=[]) or [])
        if app not in excluded:
            excluded.append(app)
            config.set("safety", "privacy_excluded_apps", value=excluded)
        return {"success": True, "excludedApps": excluded}

    async def privacy_remove_app(params: dict, ws) -> dict:
        from backend.config import config
        app = (params.get("app") or "").strip()
        excluded = [a for a in (config.get("safety", "privacy_excluded_apps", default=[]) or [])
                    if a != app]
        config.set("safety", "privacy_excluded_apps", value=excluded)
        return {"success": True, "excludedApps": excluded}

    async def egress_list(params: dict, ws) -> dict:
        from backend.safety.egress_monitor import egress
        entries = egress.list_recent(int(params.get("limit", 50)))
        return {"entries": entries, "count": len(entries)}

    async def snapshot_list(params: dict, ws) -> dict:
        """List stored (privacy-masked) screen snapshots."""
        from backend.memory.snapshot_store import snapshot_store
        return {"snapshots": snapshot_store.list(), "count": snapshot_store.count()}

    async def memory_list(params: dict, ws) -> dict:
        """Everything the agent remembers: session summaries + conversation memories."""
        from backend.memory.session_summary import get_recent_summaries
        from backend.memory.vector_store import vector_store
        return {
            "summaries": get_recent_summaries(50),
            "memories": vector_store.list("conversation", limit=100),
        }

    async def memory_delete_summary(params: dict, ws) -> dict:
        """Delete one session summary.

        Takes the summary's `id` (what `memory.getSummaries` returns). The old
        `index` parameter is still accepted, but a position does not survive the
        50-entry rotation the store keeps — it was deleting whichever summary
        happened to sit at that slot rather than the one the user picked.
        """
        from backend.memory.session_summary import delete_summary
        ref = params.get("id")
        if ref is None:
            ref = params.get("index")
        if not isinstance(ref, int) or ref < 0:
            return {"success": False,
                    "error": "id is required (the summary's own id)"}
        ok = delete_summary(ref)
        if ok:
            # Same id, so the link graph and the summary store agree about
            # which entry is gone.
            from backend.memory import autolink
            autolink.forget_ref("summary", ref)
        return {"success": ok}

    async def memory_delete_memory(params: dict, ws) -> dict:
        from backend.memory.vector_store import vector_store
        rid = params.get("id")
        if rid is None:
            return {"success": False, "error": "id is required"}
        try:
            ok = vector_store.delete(int(rid))
        except (TypeError, ValueError):
            return {"success": False, "error": "id must be a number"}
        if ok:
            from backend.memory import autolink
            autolink.forget_ref("memory", int(rid))
        return {"success": ok}

    async def memory_clear(params: dict, ws) -> dict:
        from backend.memory.session_summary import clear_summaries
        return {"cleared": clear_summaries()}

    async def memory_get_facts(params: dict, ws) -> dict:
        from backend.memory.facts import get_facts
        return {"facts": get_facts(int(params.get("limit", 50)))}

    async def memory_set_fact(params: dict, ws) -> dict:
        from backend.memory.facts import add_fact
        fact = add_fact(str(params.get("fact", "")).strip(),
                        source=params.get("source", "manual"))
        if fact is None:
            return {"success": False, "error": "Empty or duplicate fact"}
        return {"success": True, "fact": fact}

    async def memory_delete_fact(params: dict, ws) -> dict:
        from backend.memory.facts import delete_fact
        try:
            ok = delete_fact(int(params.get("id")))
        except (TypeError, ValueError):
            return {"success": False, "error": "id must be a number"}
        return {"success": ok}

    async def memory_list_triples(params: dict, ws) -> dict:
        from backend.memory.knowledge_graph import kg
        return {"triples": kg.list_recent(int(params.get("limit", 50))),
                "count": kg.count()}

    async def memory_health(params: dict, ws) -> dict:
        """What the retrieval stack is actually doing, and what is wrong.

        Reported because the failures here are silent: no semantic embedder, a
        store with no rows, or a relevance gate that admits nothing all look
        identical from the outside — the agent just answers from the
        conversation alone. `probe` runs one real recall so that is visible.
        """
        from backend.memory.health import report, probe
        out = report()
        if params.get("probe"):
            out["probe"] = await probe(str(params.get("query") or "what did we work on"))
        return out

    async def memory_reembed(params: dict, ws) -> dict:
        """Migrate stored vectors to the current embedder.

        Needed after changing `memory.embedder`: two 384-dim models produce
        incomparable vectors, so rows left behind do not error, they rank
        wrongly. The background job does this in batches; this runs it now.
        """
        from backend.memory.reembed import reembed_all
        return await reembed_all()

    async def memory_delete_triple(params: dict, ws) -> dict:
        from backend.memory.knowledge_graph import kg
        try:
            tid = int(params.get("id"))
            ok = kg.delete(tid)
        except (TypeError, ValueError):
            return {"success": False, "error": "id must be a number"}
        if ok:
            from backend.memory import autolink
            autolink.forget_ref("triple", tid)
        return {"success": ok}

    async def chat_get_history(params: dict, ws) -> dict:
        """Recent conversation turns — used by the chat page to recover a
        reply that was generated while the dashboard was on another tab."""
        from backend.memory.chat_history import chat_history
        max_messages = int(params.get("max", 60))
        return {"messages": chat_history.get_context(max_messages=max_messages)}

    # ---- Memory relations ----------------------------------------------------

    async def memory_links(params: dict, ws) -> dict:
        """The relation graph: stats, plus edges for one item when asked."""
        from backend.memory.links import link_store
        out = {"success": True, **link_store.stats()}
        kind = (params.get("kind") or "").strip().lower()
        ref_id = params.get("id")
        if kind and ref_id not in (None, ""):
            out["neighbors"] = link_store.neighbors(
                kind, ref_id,
                direction=params.get("direction") or "both",
                relations=params.get("relations") or None,
                limit=int(params.get("limit", 100)))
            out["count"] = len(out["neighbors"])
        else:
            out["edges"] = link_store.export(int(params.get("limit", 200)))
        return out

    async def memory_add_link(params: dict, ws) -> dict:
        from backend.memory.links import RELATIONS, link_store
        rel = (params.get("relation") or "relates_to").strip().lower()
        if rel not in RELATIONS:
            return {"success": False,
                    "error": f"relation must be one of {', '.join(RELATIONS)}"}
        link_id = link_store.link(
            params.get("kind", ""), params.get("id", ""), rel,
            params.get("target_kind", ""), params.get("target_id", ""),
            source=params.get("source") or "dashboard",
            note=params.get("note") or "")
        if link_id is None:
            return {"success": False,
                    "error": "Could not link those items (check the kinds and "
                             "that they are not the same item)."}
        return {"success": True, "id": link_id}

    async def memory_delete_link(params: dict, ws) -> dict:
        from backend.memory.links import link_store
        try:
            return {"success": link_store.unlink(int(params.get("id")))}
        except (TypeError, ValueError):
            return {"success": False, "error": "id must be a number"}

    async def memory_related(params: dict, ws) -> dict:
        from backend.memory.links import link_store
        kind = (params.get("kind") or "").strip().lower()
        ref_id = params.get("id")
        if not kind or ref_id in (None, ""):
            return {"success": False, "error": "kind and id are required"}
        items = link_store.related(kind, ref_id,
                                   depth=int(params.get("depth", 1)),
                                   limit=int(params.get("limit", 50)))
        return {"success": True, "related": items, "count": len(items)}

    async def memory_files(params: dict, ws) -> dict:
        """Files the memory graph points at, most referenced first."""
        from backend.memory.links import link_store
        rows = link_store.files(int(params.get("limit", 200)))
        return {"success": True, "files": rows, "count": len(rows)}

    async def memory_prune_links(params: dict, ws) -> dict:
        """Drop edges whose target no longer exists."""
        from backend.memory.links import link_store
        report = link_store.prune()
        try:
            from backend.wiki import store as wiki_store
            wiki_store.reindex_links()
        except Exception:
            pass
        return {"success": True, **report}

    # ---- Wiki ---------------------------------------------------------------

    async def wiki_list(params: dict, ws) -> dict:
        from backend.wiki import store
        if not store.enabled():
            return {"success": False, "error": "The wiki is disabled."}
        pages = store.list_pages()
        return {"success": True, "pages": pages, "count": len(pages),
                "dir": str(store.base_dir())}

    async def wiki_get(params: dict, ws) -> dict:
        from backend.wiki import store
        slug = (params.get("slug") or "").strip()
        if not slug:
            return {"success": False, "error": "slug is required"}
        page = store.read(slug)
        if not page:
            return {"success": False, "error": f"No wiki page '{slug}'."}
        page["backlinks"] = store.backlinks(page["slug"])
        return {"success": True, "page": page}

    async def wiki_save(params: dict, ws) -> dict:
        from backend.wiki import store
        if not store.enabled():
            return {"success": False, "error": "The wiki is disabled."}
        slug = (params.get("slug") or params.get("title") or "").strip()
        if not slug:
            return {"success": False, "error": "slug or title is required"}
        body = params.get("body")
        if body is None:
            return {"success": False, "error": "body is required"}
        tags = params.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        sources = params.get("sources") or []
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.splitlines() if s.strip()]
        page = store.write(slug, title=params.get("title") or slug,
                           body=str(body), tags=list(tags),
                           sources=list(sources))
        if not page:
            return {"success": False, "error": "Could not write the page."}
        return {"success": True, "page": page}

    async def wiki_delete(params: dict, ws) -> dict:
        from backend.wiki import store
        slug = (params.get("slug") or "").strip()
        if not slug:
            return {"success": False, "error": "slug is required"}
        return {"success": store.delete(slug)}

    async def wiki_search(params: dict, ws) -> dict:
        from backend.wiki import store
        query = (params.get("query") or "").strip()
        if not query:
            return {"success": False, "error": "query is required"}
        hits = store.search(query, limit=int(params.get("limit", 20)))
        return {"success": True, "pages": hits, "count": len(hits)}

    async def wiki_ingest(params: dict, ws) -> dict:
        """Fold a file or pasted text into the wiki — the slow path, so the
        dashboard shows progress rather than blocking silently."""
        from backend.wiki import ingest
        path = (params.get("path") or "").strip()
        text = (params.get("text") or "").strip()
        if not path and not text:
            return {"success": False, "error": "path or text is required"}
        tags = params.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if path:
            return await ingest.ingest_file(path, tags=list(tags))
        return await ingest.ingest_text(text, title=params.get("title") or "",
                                        source_ref=params.get("source") or "",
                                        tags=list(tags))

    async def wiki_lint(params: dict, ws) -> dict:
        from backend.wiki import store
        if not store.enabled():
            return {"success": False, "error": "The wiki is disabled."}
        return {"success": True, **store.lint()}

    async def wiki_refresh_links(params: dict, ws) -> dict:
        from backend.wiki import store
        return {"success": True, **store.reindex_links()}

    # ---- Procedures ---------------------------------------------------------

    async def sop_status(params: dict, ws) -> dict:
        """What the Settings panel shows: on/off, and how much is stored."""
        from backend.sop import store
        try:
            return {"success": True, **store.describe()}
        except Exception as e:
            log.debug("sop.status failed: %s", e)
            return {"success": False, "error": str(e)}

    async def sop_list(params: dict, ws) -> dict:
        from backend.sop import store
        category = params.get("category") or None
        try:
            return {"success": True,
                    "procedures": store.list_all(category),
                    "categories": store.categories()}
        except Exception as e:
            log.debug("sop.list failed: %s", e)
            return {"success": False, "error": str(e), "procedures": []}

    async def sop_get(params: dict, ws) -> dict:
        from backend.sop import store
        sop = store.get(str(params.get("id") or ""))
        if not sop:
            return {"success": False, "error": f"No procedure '{params.get('id')}'."}
        return {"success": True, "procedure": sop}

    async def sop_save(params: dict, ws) -> dict:
        from backend.sop import store
        try:
            if not store.enabled():
                return {"success": False,
                        "error": "Procedures are turned off in settings."}
            out = store.upsert({
                "id": params.get("id"),
                "category": params.get("category"),
                "title": params.get("title"),
                "steps": params.get("steps"),
                "tools": params.get("tools"),
                "source": "manual",
            })
            if not out.get("success"):
                return out
            return {"success": True, "procedure": out["sop"],
                    "categories": store.categories()}
        except Exception as e:
            log.debug("sop.save failed: %s", e)
            return {"success": False, "error": str(e)}

    async def sop_delete(params: dict, ws) -> dict:
        from backend.sop import store
        try:
            removed = store.delete(str(params.get("id") or ""))
            return {"success": removed,
                    "categories": store.categories(),
                    **({"error": "No such procedure."} if not removed else {})}
        except Exception as e:
            log.debug("sop.delete failed: %s", e)
            return {"success": False, "error": str(e)}

    async def sop_reseed(params: dict, ws) -> dict:
        """Re-add the built-in procedures the user has deleted."""
        from backend.sop import seeds, store
        try:
            added = seeds.seed()
            return {"success": True, "added": added,
                    "categories": store.categories(),
                    "procedures": store.list_all()}
        except Exception as e:
            log.debug("sop.reseed failed: %s", e)
            return {"success": False, "error": str(e)}

    # ---- Remote access ------------------------------------------------------

    async def remote_status(params: dict, ws) -> dict:
        """What the Remote page needs: the gateway, the sessions, the tailnet."""
        out: dict = {"success": True}
        try:
            from backend.remote.gateway import gateway
            out.update(gateway.status())
        except Exception as e:
            log.debug("remote.status gateway part failed: %s", e)
            out.update({"enabled": False, "running": False, "sessions": [],
                        "blockers": [f"Remote access is unavailable: {e}"],
                        "error": str(e)})
        try:
            from backend.tailscale.manager import tailscale
            ts = tailscale.status()
            out["tailscale"] = ts
            # The URL to hand to another device, if the tailnet is serving us.
            out["remote_url"] = tailscale.url()
        except Exception as e:
            log.debug("remote.status tailscale part failed: %s", e)
            out["tailscale"] = {"installed": False, "blockers": [str(e)]}
            out["remote_url"] = ""
        # Whether to offer the install button. A remote session must not start
        # an elevated install on a machine nobody is sitting at.
        out["remote_session"] = bool(
            (getattr(ws, "addled", None) or {}).get("remote"))
        try:
            from backend.tailscale.installer import installer
            out["installer"] = installer.status()
        except Exception as e:
            log.debug("remote.status installer part failed: %s", e)
            out["installer"] = {"phase": "idle", "running": False,
                                "winget": False, "preferred": "download",
                                "can_install": False}
        # Never send the gateway's own bind address as if it were the URL.
        out.setdefault("remote_url", "")
        return out

    async def remote_set_password(params: dict, ws) -> dict:
        """Set the remote password. Only callable from a local connection."""
        from backend.remote import auth
        # A remote session must not be able to change the password that guards
        # it — that would turn one compromised session into permanent access.
        if getattr(ws, "addled", {}).get("remote"):
            return {"success": False,
                    "error": "The remote password can only be changed on the "
                             "machine running Addled."}
        return auth.set_password(str(params.get("password") or ""))

    async def remote_generate_password(params: dict, ws) -> dict:
        """Set a strong password and return it once, for the user to copy."""
        from backend.remote import auth
        if getattr(ws, "addled", {}).get("remote"):
            return {"success": False,
                    "error": "The remote password can only be changed on the "
                             "machine running Addled."}
        password = auth.generate_password()
        result = auth.set_password(password)
        if not result.get("success"):
            return result
        return {"success": True, "password": password,
                "sessionsDropped": result.get("sessionsDropped", 0)}

    async def remote_sessions(params: dict, ws) -> dict:
        from backend.remote import auth
        return {"success": True, "sessions": auth.sessions.list()}

    async def remote_revoke(params: dict, ws) -> dict:
        from backend.remote import auth
        session_id = str(params.get("id") or "")
        if not session_id:
            return {"success": False, "error": "Pass the session 'id'."}
        removed = auth.sessions.revoke(session_id)
        return {"success": removed, "sessions": auth.sessions.list(),
                **({} if removed else {"error": "No such session."})}

    async def remote_revoke_all(params: dict, ws) -> dict:
        from backend.remote import auth
        count = auth.sessions.revoke_all()
        return {"success": True, "revoked": count, "sessions": []}

    async def remote_start(params: dict, ws) -> dict:
        from backend.remote.gateway import gateway
        ok, error = await gateway.start()
        return {"success": ok, "error": error, "status": gateway.status()}

    async def remote_stop(params: dict, ws) -> dict:
        from backend.remote.gateway import gateway
        await gateway.stop()
        return {"success": True, "status": gateway.status()}

    # ---- Tailscale -----------------------------------------------------------

    async def tailscale_status(params: dict, ws) -> dict:
        from backend.tailscale.manager import tailscale
        if params.get("refresh"):
            try:
                await tailscale.refresh()
            except Exception as e:  # noqa: BLE001
                log.debug("tailscale refresh failed: %s", e)
        return {"success": True, **tailscale.status()}

    async def tailscale_login(params: dict, ws) -> dict:
        """Begin a sign-in. The URL arrives on `tailscale.loginUrl`."""
        from backend.tailscale.manager import tailscale
        key = str(params.get("authKey") or "").strip()
        if key:
            # Stored, never returned; `status()` deliberately omits it.
            try:
                from backend.config import config
                config.set("tailscale", "auth_key", value=key)
            except Exception as e:  # noqa: BLE001
                log.debug("could not store the auth key: %s", e)
        return await tailscale.login(auth_key=key)

    async def tailscale_logout(params: dict, ws) -> dict:
        from backend.tailscale.manager import tailscale
        return await tailscale.logout()

    async def tailscale_down(params: dict, ws) -> dict:
        from backend.tailscale.manager import tailscale
        return await tailscale.down()

    async def tailscale_enable_serve(params: dict, ws) -> dict:
        from backend.tailscale.manager import tailscale
        return await tailscale.enable_serve(funnel=bool(params.get("funnel")))

    async def tailscale_disable_serve(params: dict, ws) -> dict:
        from backend.tailscale.manager import tailscale
        return await tailscale.disable_serve(funnel=bool(params.get("funnel")))

    async def tailscale_install_status(params: dict, ws) -> dict:
        from backend.tailscale.installer import installer
        return {"success": True, **installer.status()}

    async def tailscale_install(params: dict, ws) -> dict:
        """Run the official Tailscale installer, on the user's click.

        The remote check is the important part: the install raises a UAC prompt
        on this machine, and nobody is sitting at it to accept a prompt raised
        from a remote session. This also refuses unless it is a deliberate call —
        nothing installs on its own.
        """
        from backend.tailscale.installer import installer
        remote = bool((getattr(ws, "addled", None) or {}).get("remote"))
        method = str(params.get("method") or "")
        if not method:
            try:
                from backend.config import config
                method = str(config.get("tailscale", "install_method",
                                        default="auto") or "auto")
            except Exception:  # noqa: BLE001
                method = "auto"
        return await installer.install(method=method, remote=remote)

    # ---- Phase 3: Voice TTS --------------------------------------------------

    async def voice_speak(params: dict, ws) -> dict:
        text = params.get("text", "")
        voice = params.get("voice") or None
        if not text:
            return {"success": False, "error": "No text to speak"}
        return await _speak_reply(text, voice)

    async def voice_voices(params: dict, ws) -> dict:
        """The voices the pickers can offer: installed Kokoro + Edge service.

        Slow on a cold cache, because the Edge half is a network call, so the
        caller shows a loading state rather than blocking on it. `force`
        bypasses the cache when the user asks to refresh.
        """
        try:
            from backend.voice import voices as voice_catalogue
            payload = await voice_catalogue.describe(
                force=bool(params.get("force")))
        except Exception as e:
            log.debug("voice.voices failed: %s", e)
            return {"success": False, "error": str(e),
                    "kokoro": [], "edge": []}
        return {"success": True, **payload}

    # ---- Phase 3: Character state control (used by bots) --------------------

    async def character_set_state(params: dict, ws) -> dict:
        from backend.ws_server import get_server
        state = params.get("state", "idle")
        # Tell the engine to change state (triggers actual animation)
        if _engine_ref:
            if state in ("sleeping",):
                _engine_ref.set_sleeping(True)
            elif state in ("idle", "wake"):
                _engine_ref.set_sleeping(False)
            _engine_ref.sig_agent_state.emit(state)
        # Broadcast to all WS clients
        await get_server().broadcast("state.changed", {"state": state})
        return {"success": True, "state": state}

    # ---- Phase 3: Observer status ---------------------------------------------

    async def observer_status(params: dict, ws) -> dict:
        if _engine_ref:
            info = _engine_ref.last_screen_info()
            return {
                "tier": (info or {}).get("tier", "light"),
                "active": _engine_ref.state.name.lower() == "running",
                "context": (info or {}).get("context", "unknown"),
                "detail": (info or {}).get("detail"),
                "message": "Full 3-tier perception (light/medium/deep).",
            }
        return {"tier": "light", "active": True, "context": "unknown",
                "message": "Observer engine active. Full 3-tier perception (light/medium/deep)."}

    # ---- Phase 5: Goals Engine (planner + executor + store) -------------------

    async def goal_create(params: dict, ws) -> dict:
        from backend.goals.store import goal_store
        from backend.goals.planner import plan_goal
        title = (params.get("title") or "").strip()
        if not title:
            return {"error": "title is required"}
        description = params.get("description", "")
        priority = params.get("priority", "normal")

        # Plan the goal using LLM
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
            plan = await plan_goal(title, description, provider)
        except Exception:
            plan = await plan_goal(title, description)  # Fallback without provider

        goal_id = goal_store.create(title, description, priority, plan)
        return {"goalId": goal_id, "plan": plan}

    async def goal_list(params: dict, ws) -> dict:
        from backend.goals.store import goal_store
        status = params.get("status")
        goals = goal_store.list_all(status)
        return {"goals": goals, "count": len(goals)}

    async def goal_get(params: dict, ws) -> dict:
        """One goal in full: plan, per-step status, findings and rounds.

        `goal.list` returns every goal, which is wasteful for a detail view and
        makes the page scan for what it already has an id for. An unknown id is
        reported as an explicit error rather than an empty object, so the page
        can say the goal is gone instead of rendering a blank card.
        """
        from backend.goals.store import goal_store
        goal_id = str(params.get("goalId") or "").strip()
        if not goal_id:
            return {"success": False, "error": "No goalId provided"}
        try:
            goal = goal_store.load(goal_id)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"could not read {goal_id}: {e}"}
        if not goal:
            return {"success": False, "error": f"Goal not found: {goal_id}"}
        return {"success": True, "goal": goal}

    async def goal_start(params: dict, ws) -> dict:
        from backend.goals.store import goal_store
        from backend.goals.executor import goal_executor
        from backend.actions.executor import executor as action_exec
        goal_id = params.get("goalId", "")
        if not goal_id:
            return {"success": False, "error": "No goalId provided"}

        goal_executor.set_executor(action_exec)
        goal_executor.set_store(goal_store)

        # The provider is resolved here and handed in, so a run in progress
        # keeps using the model it started with even if the user switches
        # providers mid-goal.
        provider = None
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception as e:  # noqa: BLE001
            log.debug("no provider for goal %s: %s", goal_id, e)

        # Fire and forget — run in background. Through `_spawn` so the task
        # keeps a strong reference: a goal half-run and then collected would
        # look like it simply stopped, with nothing in the log to say why.
        _spawn(goal_executor.run_goal(goal_id, provider=provider))
        return {"success": True, "goalId": goal_id, "status": "started"}

    async def goal_replan(params: dict, ws) -> dict:
        """Re-plan a goal from scratch, optionally with feedback.

        Separate from `goal.start` because a plan that was wrong should be
        fixable without the goal having to fail first — and because the loop in
        the executor re-plans *informed* by what happened, which an explicit
        call lets the user trigger by hand.
        """
        from backend.goals.store import goal_store
        from backend.goals.planner import plan_goal
        goal_id = str(params.get("goalId") or "").strip()
        if not goal_id:
            return {"success": False, "error": "No goalId provided"}
        goal = goal_store.load(goal_id)
        if not goal:
            return {"success": False, "error": f"No goal {goal_id}"}

        provider = None
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:  # noqa: BLE001
            pass

        # What the user says went wrong takes precedence over the step report;
        # they know more than the plan does about why it did not fit.
        feedback = str(params.get("feedback") or "").strip()
        if not feedback:
            from backend.goals.executor import goal_executor
            feedback = goal_executor._feedback(
                (goal.get("plan") or {}).get("steps") or [])

        plan = await plan_goal(goal.get("title", ""),
                               goal.get("description", ""), provider,
                               feedback=feedback)
        goal["plan"] = plan
        # A re-plan replaces the steps, so any status from the previous attempt
        # would be carried onto steps that no longer describe the same work.
        goal["status"] = "pending"
        goal_store.save(goal)
        return {"success": True, "goalId": goal_id, "plan": plan,
                "count": plan.get("count", 0)}

    async def goal_cancel(params: dict, ws) -> dict:
        from backend.goals.executor import goal_executor
        from backend.goals.store import goal_store
        goal_id = params.get("goalId", "")
        goal_executor.cancel(goal_id)
        goal_store.update_status(goal_id, "cancelled")
        return {"success": True}

    # ---- Phase 5: Code Engine (diff + language detect) -----------------------

    # Generated or vendored directories the file tree hides by default. The page
    # offers "show everything", so nothing is permanently unreachable.
    #
    # `vendor` and `target` are DELIBERATELY not here. They are generated in
    # some ecosystems, but they are real source in others — `vendor/` holds
    # committed dependencies in Go and PHP, and `target/` holds hand-written
    # code in Rust and Maven projects. Hiding them made a bound workspace show
    # folders simply vanish, which reads as a bug because it is one. The rest
    # below are generated in every ecosystem that uses them, so hiding those is
    # safe.
    _SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.next',
                  'dist', 'build', 'out', 'venv', '.venv',
                  'site-packages', '.mypy_cache', '.pytest_cache', '.ruff_cache',
                  '.idea', '.vs', '.gradle'}

    async def code_bind(params: dict, ws) -> dict:
        folder = params.get("folderPath", "")
        if not folder:
            return {"workspaceId": None, "files": [], "error": "No folder path provided"}
        import os
        from pathlib import Path
        from backend.codemode.lang_detect import detect
        if not os.path.isdir(folder):
            return {"workspaceId": folder, "files": [], "error": f"Folder not found: {folder}"}
        folder = str(Path(folder).resolve())
        try:
            from backend.config import config
            config.set("workspace", "root", value=folder)
            from backend.memory.session_context import session_context
            session_context._load()
            session_context.active_workspace = folder
            session_context.save()
        except Exception as e:
            log.debug("workspace scope update failed: %s", e)
        limit = 400
        show_all = bool(params.get("includeIgnored"))
        files = []
        truncated = False
        # Which directories were left out, so the page can SAY so rather than
        # presenting a partial tree as if it were the whole thing. A folder
        # quietly missing reads as a bug; a folder missing with a note does not.
        hidden_dirs: list[str] = []
        try:
            for root, dirs, filenames in os.walk(folder):
                if show_all:
                    dirs[:] = [d for d in dirs if d != '.git']
                else:
                    kept = []
                    for d in dirs:
                        if d.startswith('.') or d in _SKIP_DIRS:
                            rel = os.path.relpath(os.path.join(root, d), folder)
                            hidden_dirs.append(rel.replace(os.sep, '/'))
                        else:
                            kept.append(d)
                    dirs[:] = kept
                dirs.sort()
                for f in sorted(filenames):
                    fp = os.path.join(root, f)
                    try:
                        files.append({"name": f,
                                      "path": os.path.relpath(fp, folder).replace(os.sep, '/'),
                                      "language": detect(fp),
                                      "size": os.path.getsize(fp)})
                    except OSError:
                        continue
                    if len(files) >= limit:
                        truncated = True
                        break
                if truncated:
                    break
        except Exception as e:
            return {"workspaceId": folder, "files": [], "error": str(e)}
        return {"workspaceId": folder, "files": files,
                "truncated": truncated, "limit": limit,
                "hiddenDirs": hidden_dirs[:20],
                "hiddenDirCount": len(hidden_dirs)}

    async def code_git_status(params: dict, ws) -> dict:
        """Whether the workspace is a git repo, and what is uncommitted.

        The Code page shows this so the user can see that applying a plan will
        leave a revertible commit, and that their own uncommitted work is still
        there. A non-repo workspace is reported as such, not as an error.
        """
        from backend.codemode import gitops
        wp = str(params.get("workspaceId") or "")
        if not wp:
            return {"isRepo": False, "available": gitops.available(),
                    "error": "No workspace is bound."}
        try:
            return gitops.status(wp)
        except Exception as e:  # noqa: BLE001
            log.debug("git status failed: %s", e)
            return {"isRepo": False, "error": str(e)}

    async def code_git_diff(params: dict, ws) -> dict:
        """The uncommitted diff of the workspace, for the page's git view."""
        from backend.codemode import gitops
        wp = str(params.get("workspaceId") or "")
        if not wp:
            return {"diff": "", "error": "No workspace is bound."}
        files = params.get("files") or None
        return {"diff": gitops.diff(wp, files=files,
                                    staged=bool(params.get("staged")))}

    async def code_git_revert(params: dict, ws) -> dict:
        """Undo the last change Addled committed to the workspace.

        Only ever reverts a commit the caller names — normally the sha returned
        by `code.applyPlan` — and only via `git revert`, so it adds an inverse
        commit instead of rewriting history the user may have pushed.
        """
        from backend.codemode import gitops
        wp = str(params.get("workspaceId") or "")
        sha = str(params.get("sha") or "").strip()
        if not wp:
            return {"success": False, "error": "No workspace is bound."}
        if not sha:
            return {"success": False,
                    "error": "Name the commit to revert (the sha code.applyPlan "
                             "returned)."}
        result = gitops.revert_commit(wp, sha)
        return {"success": bool(result.get("ok")),
                "sha": result.get("sha") or "",
                "error": "" if result.get("ok") else (result.get("reason") or "")}

    async def code_verify(params: dict, ws) -> dict:
        """Run the workspace's own test/verify command.

        The Code page calls this after applying a plan so the result can say
        "verified" rather than only "written". The command is discovered from
        the project and never invented — a workspace with no check reports that
        plainly, which is more useful than a green tick over nothing.
        """
        from backend.codemode import verify as verify_mod
        wp = str(params.get("workspaceId") or "")
        if not wp:
            return {"ok": False, "ran": False,
                    "error": "No workspace is bound."}
        detected = verify_mod.detect_command(wp)
        if params.get("detectOnly"):
            return {"ok": False, "ran": False, **detected}
        result = await verify_mod.run_verification(
            wp, command=str(params.get("command") or detected.get("command") or ""),
            timeout=int(params.get("timeout") or 300))
        result["verdict"] = verify_mod.verdict_line(result)
        return result

    async def code_grep(params: dict, ws) -> dict:
        """Literal (case-insensitive) search across the bound workspace."""
        import os
        from backend.codemode import OutsideWorkspace, resolve_in_workspace
        wp = str(params.get("workspaceId") or "")
        needle = str(params.get("query") or "")
        if not wp:
            return {"matches": [], "error": "No workspace is bound."}
        if len(needle) < 2:
            return {"matches": [], "error": "Use at least two characters."}
        try:
            root = resolve_in_workspace(wp, ".")
        except OutsideWorkspace as e:
            return {"matches": [], "error": str(e)}
        limit = max(1, min(int(params.get("limit") or 200), 1000))
        needle_cf = needle.casefold()
        matches, scanned, truncated = [], 0, False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != '.git'
                           and d not in _SKIP_DIRS]
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) > 2_000_000:
                        continue
                    with open(path, 'r', encoding='utf-8',
                              errors='replace') as fh:
                        text = fh.read()
                except OSError:
                    continue
                if '\x00' in text:            # binary, skip
                    continue
                scanned += 1
                rel = os.path.relpath(path, root).replace(os.sep, '/')
                for n, line in enumerate(text.splitlines(), 1):
                    if needle_cf in line.casefold():
                        matches.append({"filePath": rel, "line": n,
                                        "text": line.strip()[:200]})
                        if len(matches) >= limit:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break
        return {"matches": matches, "scanned": scanned,
                "truncated": truncated}

    async def code_read(params: dict, ws) -> dict:
        from backend.codemode import OutsideWorkspace, resolve_in_workspace
        from backend.codemode.lang_detect import detect
        wp = params.get("workspaceId", "")
        fp = params.get("filePath", "")
        # Never join blindly: a ".." chain or an absolute path escapes the bound
        # workspace, and these methods are reachable from a remote session.
        if not wp:
            return {"content": "// No workspace is bound.", "language": "text"}
        try:
            full = resolve_in_workspace(wp, fp)
        except OutsideWorkspace as e:
            return {"content": f"// Refused: {e}", "language": "text"}
        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return {"content": content, "language": detect(fp)}
        except Exception as e:
            return {"content": f"// Error: {e}", "language": "text"}

    async def code_plan(params: dict, ws) -> dict:
        """Work out WHICH files a change touches, before editing anything.

        This exists because editing without a plan means guessing at the target.
        `code.edit` is given one file and rewrites it, so a change that really
        spans three files was made against whichever one happened to be open.

        TWO calls, on purpose, and that is the whole design:

        1. FIND — with the read-only tools, free-form. The model searches and
           reports what it found in prose.
        2. PLAN — the same request plus that transcript, with NO tools offered,
           so the only thing it can produce is the JSON plan.

        A single call that asked for both a tool call and a strict JSON document
        was tried first and does not work on a prompt-tools provider (which the
        local model is — see tool_loop.NATIVE_TOOL_PROVIDERS). Measured: the
        model made ZERO tool calls, then wrote the tool call it wished it had
        made *as a plan step*:

            {"filePath": "search_in_files", "action": "tool", ...}

        and named files that do not exist ("backend/orders/create_order.py",
        "src/order_utils.py"). Two output contracts in one turn — "call one with
        ```tool" from the catalogue and "reply with JSON only" from the brief —
        and the small model satisfies whichever it read last. Splitting them
        gave one contract per call, and the same model then searched correctly
        (`search_in_files {"query": "format_name"}`) and named the right file.

        It does not write, and it cannot: `_PLAN_TOOLS` is read-only.
        """
        import json as _json
        import re as _re

        from backend.codemode import OutsideWorkspace, resolve_in_workspace

        wp = str(params.get("workspaceId") or "")
        instruction = str(params.get("instruction") or "").strip()
        context_folders = [str(f) for f in (params.get("contextFolders") or [])]

        if not wp:
            return {"status": "error", "message": "Bind a workspace first.",
                    "plan": None}
        if not instruction:
            return {"status": "error", "message": "Describe the change first.",
                    "plan": None}
        try:
            resolve_in_workspace(wp, ".")
        except OutsideWorkspace as e:
            return {"status": "refused", "message": str(e), "plan": None}

        # A hint from the user's right-click, so the planner looks where they
        # pointed rather than scanning the whole tree blindly.
        hint = ""
        if context_folders:
            hint = ("\n\nThe user pointed at these folders — start there:\n"
                    + "\n".join(f"- {f}" for f in context_folders))

        # ---- call 1: FIND --------------------------------------------------
        # Free-form prose and tools. No JSON is asked for here, so the tool
        # catalogue is the only contract in the turn.
        #
        find_persona = (
            "You find code. You report which files matter for a change; you do "
            "NOT make the change.\n\n"
            "Search before you answer. Call `search_in_files` with the exact "
            "symbol, string or filename the request mentions, then `read_file` a "
            "candidate to confirm it. A filename that merely sounds related is "
            "not evidence — the file that needs changing is named after the "
            "concept, not after the request, and callers you have not thought of "
            "will need updating too.\n\n"
            "If a search finds nothing, try another term. If you still cannot "
            "tell, say which files you ruled out and why."
        )

        # The search report is prose, so it is written in the user's own
        # language. The pipeline pins the reply language itself from the message
        # text (see the `reply_directive` call in _run_chat_pipeline_inner), and
        # `instruction` is carried in that message, so nothing extra is needed
        # here — an earlier draft of this function computed a directive locally
        # and silently discarded it.
        find_message = (
            f"Request: {instruction}{hint}\n\n"
            "Find every file that must change, and say why each one is in "
            "scope. Report the paths exactly as the tools returned them. "
            "Start by calling `search_in_files`."
        )

        try:
            found = await _run_chat_pipeline_inner(
                find_message,
                persona=find_persona,
                tools=list(_PLAN_TOOLS),
                record=False,
                force_role="reasoning",
                # Finding the files IS the work, so this is deliberately more
                # generous than code.edit's 5.
                max_tool_rounds=8,
            )
        except Exception as e:
            log.exception("code.plan search step failed")
            return {"status": "error", "message": f"Planning failed: {e}",
                    "plan": None}

        transcript = (found or {}).get("response", "") or ""
        # `_run_chat_pipeline_inner` reports tool use as COUNTS, not a list:
        # `toolResults` is an int (how many results came back) and `toolRounds`
        # is an int (how many model turns it took). Reading `tool_results` as a
        # list silently reported "no search happened" on every plan, and the
        # guard below then refused work that had in fact been done correctly.
        tool_count = int((found or {}).get("toolResults")
                         or (found or {}).get("tool_results") or 0)
        rounds = int((found or {}).get("toolRounds")
                     or (found or {}).get("tool_rounds") or 0)

        if not transcript.strip() or transcript.startswith(("[Not connected:",
                                                            "[Provider:")):
            return {"status": "error",
                    "message": transcript or "The search step returned nothing.",
                    "plan": None}

        # If nothing was searched, say so rather than letting call 2 invent a
        # plan from the request alone — that is exactly the guessing this
        # method exists to prevent.
        #
        # The transcript counts as evidence too: a model that quotes a path it
        # could only have got from a tool result has plainly searched, and
        # refusing that would lose a good plan over bookkeeping.
        quoted_path = bool(_re.search(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,6}\b",
                                      transcript))
        if tool_count <= 0 and not quoted_path:
            return {
                "status": "error",
                "message": ("The planner did not search the project, so it "
                            "cannot say which files to change. Try naming the "
                            "file or the symbol you mean."),
                "plan": None,
                "searched": False,
            }

        # ---- call 2: PLAN --------------------------------------------------
        # The transcript plus the request, and NO tools, so JSON is the only
        # possible answer.
        #
        # `structure` says the JSON stays English on purpose. The pipeline pins
        # the reply language from the message text, and this document is parsed
        # and rendered by a machine — prose in another language inside a value
        # is fine, but a translated KEY is a plan that no longer loads. Saying so
        # beats relying on the model to guess which parts are data.
        contract = (
            "Reply with JSON only, no prose before or after:\n"
            '{"summary": "one line describing the change",\n'
            ' "steps": [{"filePath": "relative/path.py",\n'
            '            "action": "edit" | "create",\n'
            '            "instruction": "what to change in THIS file, specific '
            'enough to act on alone",\n'
            '            "reason": "why this file is in scope"}],\n'
            ' "uncertain": [{"question": "what you could not determine"}]}\n'
            "The keys above are fixed — keep them exactly as written, in "
            "English. Write the VALUES in the same language as the request. "
            "Only use file paths that appear in the search results above, "
            'except when action is "create".'
        )
        plan_message = (
            f"Request: {instruction}\n\n"
            f"What a search of the project found:\n{transcript}\n\n"
            f"Turn that into the plan. {contract}"
        )

        try:
            planned = await _run_chat_pipeline_inner(
                plan_message,
                persona=("You turn a search report into a plan. The files are "
                         "already known — do not invent new ones."),
                # Deliberately EMPTY, not None: an empty list means "no tools",
                # which is the point of this call. None would mean "every
                # enabled skill" and reopen the two-contract problem.
                tools=[],
                record=False,
                force_role="reasoning",
                max_tool_rounds=1,
            )
        except Exception as e:
            log.exception("code.plan format step failed")
            return {"status": "error", "message": f"Planning failed: {e}",
                    "plan": None}

        text = (planned or {}).get("response", "") or ""
        if not text or text.startswith(("[Not connected:", "[Provider:")):
            return {"status": "error",
                    "message": text or "The planner returned nothing.",
                    "plan": None}

        # Pull the JSON object out of whatever the model surrounded it with.
        candidate = text.strip()
        fence = _re.search(r"```(?:json)?\s*(.*?)```", candidate, _re.DOTALL)
        if fence:
            candidate = fence.group(1).strip()
        match = _re.search(r"\{.*\}", candidate, _re.DOTALL)
        if not match:
            return {"status": "error",
                    "message": "The planner did not return a plan.",
                    "plan": None}

        body = match.group(0)
        raw = None
        try:
            raw = _json.loads(body)
        except _json.JSONDecodeError as first_error:
            # A local model's JSON is often *nearly* valid: a raw newline inside
            # a string, a trailing comma, or a comment. Rejecting the whole plan
            # for one of those throws away real work, so try to repair the two
            # shapes that actually happen before giving up.
            #
            # Observed for real: "Expecting ',' delimiter: line 6 column 137" —
            # a multi-line value written as a bare string. The failure was only
            # found by driving the UI; a stubbed model always returns clean JSON.
            repaired = body
            # 1. Trailing commas before a closing brace/bracket.
            repaired = _re.sub(r",(\s*[}\]])", r"\1", repaired)
            # 2. Literal newlines/tabs inside a JSON string, which are illegal
            #    but which a model writes whenever it puts a code block in a
            #    value. Walk the text and escape control chars while inside a
            #    quoted run.
            out, in_str, escaped = [], False, False
            for ch in repaired:
                if in_str:
                    if escaped:
                        out.append(ch); escaped = False
                    elif ch == "\\":
                        out.append(ch); escaped = True
                    elif ch == '"':
                        out.append(ch); in_str = False
                    elif ch in "\n\r\t":
                        out.append({"\n": "\\n", "\r": "\\r",
                                    "\t": "\\t"}[ch])
                    else:
                        out.append(ch)
                else:
                    if ch == '"':
                        in_str = True
                    out.append(ch)
            repaired = "".join(out)
            try:
                raw = _json.loads(repaired)
                log.info("code.plan: repaired near-valid JSON (%s)",
                         first_error.msg)
            except _json.JSONDecodeError as e:
                return {"status": "error",
                        "message": (f"The plan was not valid JSON: {e.msg}. "
                                    f"The model's reply started with: "
                                    f"{body[:160]}"),
                        "plan": None}
        if not isinstance(raw, dict):
            return {"status": "error", "message": "The plan was not an object.",
                    "plan": None}

        steps, rejected = [], []
        for entry in (raw.get("steps") or []):
            if not isinstance(entry, dict):
                continue
            fp = str(entry.get("filePath") or "").strip().replace("\\", "/")
            if not fp:
                continue
            action = str(entry.get("action") or "edit").strip().lower()
            # A step must stay inside the workspace. Checking it here means an
            # out-of-scope path is reported as a plan problem, rather than
            # surfacing later as a confusing refusal from code.edit.
            if action != "create":
                try:
                    resolve_in_workspace(wp, fp)
                except OutsideWorkspace as e:
                    rejected.append({"filePath": fp, "message": str(e)})
                    continue
            steps.append({
                "filePath": fp,
                "action": action,
                "instruction": str(entry.get("instruction") or instruction).strip(),
                "reason": str(entry.get("reason") or "").strip(),
            })

        if not steps:
            return {"status": "error",
                    "message": ("The planner named no file inside the "
                                "workspace." + (f" Refused: {rejected[0]['message']}"
                                                if rejected else "")),
                    "plan": None, "rejected": rejected}

        uncertain = []
        for entry in (raw.get("uncertain") or []):
            if isinstance(entry, dict) and entry.get("question"):
                uncertain.append({"question": str(entry["question"])})
            elif isinstance(entry, str) and entry.strip():
                uncertain.append({"question": entry.strip()})

        return {
            "status": "ok",
            "plan": {
                "summary": str(raw.get("summary") or "").strip(),
                "steps": steps,
                "uncertain": uncertain,
            },
            "rejected": rejected,
            # What the search actually did, so the UI can show that the plan was
            # based on looking rather than guessing.
            "searched": True,
            "tool_calls": tool_count,
            "rounds": rounds,
            "findings": transcript[:1200],
            # How many diffs the page may request at once. 1 means "ask for them
            # one at a time" — the local model queues concurrent requests, so
            # anything wider would look parallel and behave serially. Sent with
            # the plan so the page does not have to guess or hold its own copy
            # of the provider list.
            "concurrency": _plan_concurrency(),
        }

    async def code_edit(params: dict, ws) -> dict:
        """Propose a change to one file, for the user to review as a diff.

        Runs through the shared chat pipeline with a code persona and a
        read-only tool subset, so the editor can look at the rest of the
        project before answering instead of guessing at it — the same
        capability chat has.

        It cannot write. The Code page's whole safety property is that a change
        is shown as a diff and applied by the user, so the tools below are
        read-only on purpose: write_file here would quietly retire that review
        step.
        """
        from backend.codemode.diff_engine import generate_diff
        wp = params.get("workspaceId", "")
        fp = params.get("filePath", "")
        instruction = (params.get("instruction") or "").strip()

        if not instruction:
            return {"diffs": [], "status": "error",
                    "message": "Describe the change you want first."}
        if not fp or not wp:
            return {"diffs": [], "status": "error",
                    "message": "Open a file in the workspace first."}

        original = ""
        from backend.codemode import OutsideWorkspace, resolve_in_workspace
        try:
            full = resolve_in_workspace(wp, fp)
        except OutsideWorkspace as e:
            return {"diffs": [], "status": "refused", "message": str(e)}
        if not full.is_file():
            return {"diffs": [], "status": "error",
                    "message": f"{fp} does not exist yet — save it first."}
        try:
            original = full.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"diffs": [], "status": "error",
                    "message": f"Could not read {fp}: {e}"}

        if not original.strip():
            return {"diffs": [], "status": "error",
                    "message": f"{fp} is empty — write the first version by hand, "
                               "then ask for changes."}

        # The model rewrites the file whole, so it has to fit in one reply with
        # room to think. This used to send the first 3000 characters and diff
        # the answer against the *whole* file, which meant a long file came back
        # as "everything after 3000 characters was deleted".
        #
        # Anchored edits removed the reason for this ceiling: the reply carries
        # only the change, so a 200 KB file is edited as easily as a small one.
        # The limit stays as a guard on the *prompt* — the whole file is still
        # sent so the model can anchor accurately — but a file above it is sent
        # with an explicit note to use anchors only, rather than refused.
        _oversized = len(original) > _CODE_EDIT_MAX_CHARS

        # Working guidelines apply to code generation too.
        guidelines_block = ""
        try:
            from backend.guidelines import inject as guidelines
            guidelines_block = guidelines.system_block(code_task=True) or ""
        except Exception as e:
            log.debug("code.edit guideline injection failed: %s", e)

        persona = (
            "You are the code editor inside Addled. You are given one file from "
            "the user's project and an instruction for changing it.\n\n"
            "You have read-only tools for that project. Use them when the change "
            "depends on anything you have not seen — how a helper is defined, "
            "what a module exports, which convention the neighbours follow. "
            "Reading beats assuming.\n\n"
            "You cannot write files. Your answer is a proposal that the user "
            "reviews as a diff, so never say a change has been made.\n\n"
            "You change the file by naming the exact text to replace, not by "
            "reproducing the whole file. Copy the anchor from the file above "
            "verbatim, including its indentation, and include enough "
            "surrounding lines that it appears only once."
        )
        if guidelines_block:
            persona = f"{persona}\n\n{guidelines_block}"

        # Said last, in the same turn as the file, because this decides whether
        # the reply can be parsed at all. The shape is the whole contract: an
        # anchored edit carries only the change, so a truncated reply fails
        # loudly ("anchor not found") instead of reading as a deletion.
        contract = (
            "Reply with JSON only, no prose before or after:\n"
            '{"edits": [{"anchor": "the exact existing text to replace",\n'
            '            "replacement": "what to put in its place"}],\n'
            ' "summary": "one line describing the change"}\n'
            "Copy each anchor from the file above exactly as it appears, "
            "including indentation. It must be unique in the file — include "
            "more surrounding lines until it is. To delete text, use an empty "
            "replacement. To insert, anchor on the line the new text goes "
            "after and repeat it in the replacement followed by the new text. "
            "If the change touches most of the file, anchor on the entire old "
            "body and replace it with the entire new body.\n"
            "Keep the JSON keys in English; write the summary in the language "
            "of the instruction."
        )
        size_note = ""
        if _oversized:
            size_note = (
                f"\n\nThis file is {len(original):,} characters, so it is too "
                "long to return whole. Use anchored edits only — never reply "
                "with the complete file."
            )
        message = (f"{instruction}\n\n"
                   f"File: {fp}\n```\n{original}\n```\n\n{contract}{size_note}")

        try:
            # The inner pipeline, not run_chat_pipeline: the outer function turns
            # an empty reply into "I couldn't process that request.", which is
            # right for a chat bubble and wrong here — it would be read as the
            # new contents of the file. This has to be able to see "the model
            # said nothing" as nothing.
            result = await _run_chat_pipeline_inner(
                message,
                persona=persona,
                tools=list(_CODE_EDIT_TOOLS),
                # A code edit is a task — its raw output should not appear in
                # the chat history (it is a code block, not a conversation),
                # but the instruction and outcome belong in long-term memory
                # and the episodic journal so that all surfaces can recall
                # what was changed and why.
                record={"memory", "journal"},
                # Editing code is the canonical reasoning-role task.
                force_role="reasoning",
                max_tool_rounds=5,
            )
        except Exception as e:
            log.exception("code.edit pipeline failed")
            return {"diffs": [], "status": "error",
                    "message": f"The edit failed: {e}"}

        text = (result or {}).get("response", "") or ""
        if not text or text.startswith(("[Not connected:", "[Provider:")):
            return {"diffs": [], "status": "error",
                    "message": text or "The model returned nothing."}

        # Anchored edits are the expected shape. A whole-file reply (a bare
        # fenced block) is still accepted, because a model that ignores the
        # contract and returns the file anyway should not lose the user's edit —
        # but it is the fallback, not the path.
        modified = ""
        anchors_used = 0
        edit_report: list[dict] = []
        payload = _extract_json_object(text)
        if payload is not None:
            from backend.codemode.anchored import apply_edits, parse_edit_ops
            ops, parse_error = parse_edit_ops(payload)
            if ops:
                modified, edit_report = apply_edits(original, ops)
                anchors_used = sum(1 for r in edit_report if r.get("ok"))
                failed = [r for r in edit_report if not r.get("ok")]
                if failed and anchors_used == 0:
                    # Nothing could be applied — usually the anchor was copied
                    # from memory rather than the file. Say which one, so the
                    # next attempt can be corrected instead of guessed at.
                    first = failed[0]
                    return {
                        "diffs": [], "status": "anchor_not_found",
                        "message": (first.get("error")
                                    or "The anchor was not found in the file."),
                        "anchor": first.get("anchor", ""),
                        "edits": edit_report,
                    }
                if failed and anchors_used > 0:
                    # Partial: the edits that applied are real and are offered,
                    # with a note about the one that did not. Silent partial
                    # application is the failure mode this avoids.
                    diff = generate_diff(original, modified, fp)
                    _pending_edits[f"{wp}::{fp}"] = modified
                    return {
                        "diffs": [diff], "status": "pending",
                        "editId": f"{wp}::{fp}",
                        "applied": anchors_used,
                        "edits": edit_report,
                        "warning": (failed[0].get("error")
                                    or "An edit could not be applied."),
                        "toolCalls": (result or {}).get("toolResults", 0),
                        "message": (f"{anchors_used} of {len(edit_report)} "
                                    "edits applied — review the diff, and check "
                                    "the note about the one that failed."),
                    }
            elif parse_error and _extract_code(text).strip() == "":
                # No edits and no whole file either — report why.
                return {"diffs": [], "status": "error", "message": parse_error}

        if not modified:
            modified = _extract_code(text)

        if not modified:
            return {"diffs": [], "status": "error",
                    "message": "The model returned no code to apply."}
        if modified.strip() == original.strip():
            return {"diffs": [], "status": "unchanged",
                    "message": "The model returned the file unchanged."}

        diff = generate_diff(original, modified, fp)
        # Held until the user approves it with code.apply.
        _pending_edits[f"{wp}::{fp}"] = modified
        out = {"diffs": [diff], "status": "pending",
               "editId": f"{wp}::{fp}",
               "toolCalls": (result or {}).get("toolResults", 0),
               "message": "Edit ready for review. Approve with code.apply."}
        if anchors_used:
            out["anchors"] = anchors_used
            out["edits"] = edit_report
        return out

    async def code_apply_plan(params: dict, ws) -> dict:
        """Apply several already-reviewed edits, one file at a time.

        Why this exists: "apply all" on the page. Doing it in the browser with N
        `code.apply` round trips means the page decides what to do when the third
        of five fails, and a dropped socket midway leaves a half-applied plan
        with no record of where it stopped. The backend can do better — it knows
        which edits are still pending, and it can report exactly what happened to
        each file.

        A change is applied only if the user reviewed it: every entry must name
        an `editId` that `code.edit` actually produced and that is still pending.
        Passing `content` is NOT accepted here, unlike `code.apply` — a bulk call
        carrying its own file contents would be a way to write several files
        without the review step that makes the Code page safe.

        Stops at the first failure. A plan is usually a chain (rename a function,
        then its callers), so continuing past a failed step would apply the
        callers against a definition that never changed. The result says which
        files were written, which was refused, and which were never attempted.
        """
        from backend.codemode import OutsideWorkspace, resolve_in_workspace
        from backend.codemode.diff_engine import apply_content

        wp = str(params.get("workspaceId") or "")
        entries = params.get("edits") or []
        if not wp:
            return {"applied": [], "skipped": [], "failed": None,
                    "error": "No workspace is bound; refusing to write."}
        if not isinstance(entries, list) or not entries:
            return {"applied": [], "skipped": [], "failed": None,
                    "error": "No edits to apply."}
        # A cap, so one call cannot rewrite an unbounded number of files. The
        # page warns well before this; reaching it means something is wrong.
        if len(entries) > _MAX_PLAN_APPLIES:
            return {"applied": [], "skipped": [], "failed": None,
                    "error": (f"{len(entries)} edits at once is above the "
                              f"{_MAX_PLAN_APPLIES} this will apply in one "
                              f"call. Apply them in smaller groups.")}

        backup = params.get("backup", True)
        applied, skipped = [], []

        def _not_attempted(from_index: int) -> list[dict]:
            """Everything after `from_index`, reported so the page can say what
            is still outstanding instead of implying the whole plan succeeded."""
            rest = []
            for i in range(from_index, len(entries)):
                item = entries[i]
                rest.append({
                    "index": i,
                    "filePath": (str(item.get("filePath") or "")
                                 if isinstance(item, dict) else ""),
                    "reason": "Not attempted.",
                })
            return rest

        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                skipped.append({"index": index, "filePath": "",
                                "reason": "Malformed entry."})
                continue
            fp = str(entry.get("filePath") or "")
            if not fp:
                skipped.append({"index": index, "filePath": "",
                                "reason": "No file path."})
                continue

            # Contain the path BEFORE looking up the pending edit, so a refused
            # call cannot consume an edit the user never approved.
            try:
                full = resolve_in_workspace(wp, fp)
            except OutsideWorkspace as e:
                return {"applied": applied,
                        "skipped": skipped + _not_attempted(index + 1),
                        "failed": {"index": index, "filePath": fp,
                                   "reason": str(e)},
                        "error": f"Refused: {e}"}

            # Only a real pending edit counts. No `content` fallback here, on
            # purpose — see the docstring.
            edit_id = entry.get("editId") or f"{wp}::{fp}"
            content = _pending_edits.get(edit_id)
            if content is None:
                return {"applied": applied,
                        "skipped": skipped + _not_attempted(index + 1),
                        "failed": {"index": index, "filePath": fp,
                                   "reason": ("This change was not reviewed, or "
                                              "its diff has already been "
                                              "applied. Generate it again.")},
                        "error": f"No reviewed edit pending for {fp}."}

            allow_create = bool(entry.get("create"))
            if not full.is_file() and not allow_create:
                return {"applied": applied,
                        "skipped": skipped + _not_attempted(index + 1),
                        "failed": {"index": index, "filePath": fp,
                                   "reason": "File not found."},
                        "error": f"File not found: {fp}"}

            result = apply_content(str(full), content, backup=backup)
            if not result.get("success"):
                return {"applied": applied,
                        "skipped": skipped + _not_attempted(index + 1),
                        "failed": {"index": index, "filePath": fp,
                                   "reason": result.get("error") or "Write failed."},
                        "error": (result.get("error")
                                  or f"Could not write {fp}.")}
            _pending_edits.pop(edit_id, None)
            applied.append({
                "index": index, "filePath": fp,
                "created": bool(result.get("created")),
                "backup": result.get("backup") or "",
            })

        # Git is the undo, when the workspace is a repo. The commit goes on top
        # of the user's history and names what changed, so "undo the agent's
        # last change" is `git revert` and the user's own log shows what Addled
        # did and when. Best-effort throughout: a non-repo workspace still has
        # the per-file `.bak` backups, and the result says git was not used
        # rather than failing the apply.
        git_info: dict = {}
        verify_info: dict = {}
        if applied:
            # Verify BEFORE committing, so the commit that records the change is
            # only made when the project's own check agrees it is a good change.
            # A failing check still commits (the work is the user's, and losing
            # it would be worse), but the result says plainly that it did not
            # verify — that is the difference between "finished" and "done".
            try:
                from backend.codemode import verify as verify_mod
                verify_info = await verify_mod.run_verification(
                    wp, timeout=int(params.get("verifyTimeout") or 300))
                verify_info["verdict"] = verify_mod.verdict_line(verify_info)
            except Exception as e:  # noqa: BLE001
                log.debug("verification after plan failed: %s", e)
                verify_info = {"ran": False, "ok": False,
                               "reason": str(e), "verdict": ""}

        if applied:
            try:
                from backend.codemode import gitops
                summary = str(params.get("summary") or "")
                if verify_info.get("ran"):
                    marker = "verified" if verify_info.get("ok") else "unverified"
                    summary = f"{summary} [{marker}]".strip()
                git_info = gitops.commit(
                    wp, gitops.describe_change(applied, summary),
                    files=[str(entry.get("filePath") or "")
                           for entry in applied if entry.get("filePath")])
                git_info = {"used": bool(git_info.get("ok")),
                            "sha": git_info.get("sha") or "",
                            "reason": git_info.get("reason") or ""}
            except Exception as e:  # noqa: BLE001
                log.debug("git commit after plan failed: %s", e)
                git_info = {"used": False, "reason": str(e)}

        return {"applied": applied, "skipped": skipped, "failed": None,
                "error": "", "count": len(applied), "git": git_info,
                "verify": verify_info}

    async def code_apply(params: dict, ws) -> dict:
        """Apply a reviewed code edit. Requires explicit content OR a pending
        edit id created by code.edit."""
        from backend.codemode.diff_engine import apply_content
        from backend.codemode import OutsideWorkspace, resolve_in_workspace
        wp = params.get("workspaceId", "")
        fp = params.get("filePath", "")

        # Contain the path before touching the pending-edit store: a refused
        # call should not be able to consume an edit the user never approved.
        if not wp:
            return {"success": False,
                    "error": "No workspace is bound; refusing to write."}
        try:
            full = resolve_in_workspace(wp, fp)
        except OutsideWorkspace as e:
            return {"success": False, "error": str(e)}
        if not full.is_file():
            return {"success": False, "error": f"File not found: {fp}"}

        edit_id = params.get("editId") or f"{wp}::{fp}"
        content = params.get("content")
        if content is None:
            content = _pending_edits.get(edit_id)
        if content is None:
            return {"success": False,
                    "error": "No pending edit or content provided. Run code.edit first or pass 'content'."}

        result = apply_content(str(full), content,
                               backup=params.get("backup", True))
        if result.get("success"):
            _pending_edits.pop(edit_id, None)
        return result

    async def code_write(params: dict, ws) -> dict:
        """Save editor contents to a file inside the bound workspace.

        The editor needs a plain save: code.edit goes through a model and
        code.apply only replays a reviewed edit, so neither could persist an
        edit made by hand. Creating a file is allowed here — that is what the
        page's New File button is — and the path is contained first.
        """
        from backend.codemode import (MAX_EDIT_BYTES, OutsideWorkspace,
                                      resolve_in_workspace)
        from backend.codemode.diff_engine import apply_content
        wp = str(params.get("workspaceId") or "")
        fp = str(params.get("filePath") or "")
        content = params.get("content")
        if not wp:
            return {"success": False,
                    "error": "No workspace is bound; refusing to write."}
        if not fp:
            return {"success": False, "error": "No file path given."}
        if content is None:
            return {"success": False, "error": "No content to write."}
        try:
            full = resolve_in_workspace(wp, fp)
        except OutsideWorkspace as e:
            return {"success": False, "error": str(e)}
        if len(str(content)) > MAX_EDIT_BYTES:
            return {"success": False,
                    "error": (f"That file is larger than the "
                              f"{MAX_EDIT_BYTES // 1000} kB editor limit.")}
        return apply_content(str(full), str(content), backup=True, create=True)

    async def guide_status(params: dict, ws) -> dict:
        """Which features are actually usable on this machine.

        The Guide page shows a "needs setup" badge on each feature, and a badge
        has to reflect this machine rather than a guess. Every check below is a
        real import or a real look on PATH, not a version comparison or a
        hopeful default.

        Deliberately nothing but booleans and integers, at a flat top level: no
        strings, no paths, no names. This method is reachable from a remote
        session, so it must not become another way to read configuration back
        out of the machine. `scripts/check_guide.py` asserts that property, and
        also that every flag the Guide page references exists here — so a typo
        in the dashboard fails the suite instead of silently showing a badge
        that never appears.
        """
        import importlib.util
        import shutil

        from backend.config import config

        def module_present(name: str) -> bool:
            try:
                return importlib.util.find_spec(name) is not None
            except Exception:
                return False

        def on_path(name: str) -> bool:
            try:
                return shutil.which(name) is not None
            except Exception:
                return False

        def setting(section: str, key: str, default=None):
            try:
                return config.get(section, key, default=default)
            except Exception:
                return default

        out: dict = {}

        # ---- models and providers -------------------------------------
        providers_total = 0
        providers_with_key = 0
        try:
            from backend.providers.registry import list_available
            for entry in list_available():
                providers_total += 1
                # The local runtime carries a placeholder key rather than a
                # credential, so counting it here overstates how many providers
                # are actually usable. It is reported on its own line below.
                if entry.get("has_key") and not entry.get("local"):
                    providers_with_key += 1
        except Exception:
            pass

        # Ask the local runtime's own manager. `list_available()` only annotates
        # the local entry when that provider is flagged `local` in settings, and
        # this install's is not — so reading the annotated entry reported "not
        # installed" for a model that was downloaded and serving on port 8090.
        local_installed = local_running = False
        try:
            from backend.local_llm.manager import local_llm
            view = local_llm.status() or {}
            local_installed = bool(view.get("installed"))
            local_running = bool(view.get("running"))
        except Exception:
            pass

        out["providers_total"] = providers_total
        out["providers_configured"] = providers_with_key
        out["local_model_installed"] = local_installed
        out["local_model_running"] = local_running
        out["any_model_ready"] = bool(providers_with_key or local_running)
        out["smart_routing_on"] = bool(setting("providers", "auto_route", False))

        # ---- skills --------------------------------------------------
        try:
            from backend.skills.registry import skill_registry
            out["skills_total"] = len(skill_registry.list_all())
        except Exception:
            out["skills_total"] = 0

        # ---- workspace confinement -----------------------------------
        try:
            from backend.workspace import describe
            view = describe()
            out["workspace_configured"] = bool(view.get("configured"))
            out["workspace_enforced"] = bool(view.get("enforced"))
        except Exception:
            out["workspace_configured"] = False
            out["workspace_enforced"] = False

        # ---- MCP -----------------------------------------------------
        mcp_enabled = False
        mcp_servers = mcp_connected = mcp_tools = mcp_untrusted = 0
        try:
            from backend.mcp_client.manager import mcp_manager
            view = mcp_manager.status()
            servers = view.get("servers") or []
            mcp_enabled = bool(view.get("enabled"))
            mcp_servers = len(servers)
            mcp_tools = int(view.get("tool_count") or 0)
            for server in servers:
                # "ready" is the connected state; "connected" is not one the
                # manager ever sets, so this counted zero on a working install.
                if str(server.get("state")) in ("ready", "connected", "connecting"):
                    mcp_connected += 1
                if not server.get("trusted"):
                    mcp_untrusted += 1
        except Exception:
            pass
        out["mcp_enabled"] = mcp_enabled
        out["mcp_servers"] = mcp_servers
        out["mcp_connected"] = mcp_connected
        out["mcp_tools"] = mcp_tools
        out["mcp_untrusted"] = mcp_untrusted

        # ---- desktop control -----------------------------------------
        out["desktop_input_allowed"] = bool(setting("desktop", "allow_input", False))
        out["desktop_approval_required"] = bool(
            setting("desktop", "require_session_approval", True))

        # ---- remote access -------------------------------------------
        out["remote_enabled"] = bool(setting("remote", "enabled", False))
        tailscale = False
        try:
            from backend.tailscale.manager import installed as ts_installed
            tailscale = bool(ts_installed())
        except Exception:
            pass
        out["tailscale_installed"] = tailscale

        # ---- runtimes the optional tools need ------------------------
        out["browser_automation"] = module_present("playwright")
        out["node_available"] = on_path("node")
        out["npx_available"] = on_path("npx")
        out["uvx_available"] = on_path("uvx")

        # ---- voice ---------------------------------------------------
        # Mirrors what the engines actually import, so the flag cannot claim
        # voice works when the import would fail.
        tts_engine = str(setting("voice", "tts_engine", "edge") or "edge")
        tts = False
        try:
            from backend.voice.tts import engine_available
            tts = bool(engine_available(tts_engine))
        except Exception:
            pass
        out["tts_available"] = tts
        out["stt_available"] = (module_present("sounddevice")
                                and module_present("faster_whisper"))
        out["stt_sensevoice"] = module_present("funasr")

        # ---- bots ----------------------------------------------------
        bots_ready = 0
        try:
            from backend.bots import manager as bots
            platforms = (bots.status() or {}).get("platforms") or {}
            for name in ("telegram", "discord", "whatsapp"):
                info = platforms.get(name) or {}
                ready = bool(info.get("ready"))
                out[f"{name}_ready"] = ready
                out[f"{name}_configured"] = bool(info.get("has_token")) or (
                    name == "whatsapp" and ready)
                out[f"{name}_running"] = bool(info.get("running"))
                if ready:
                    bots_ready += 1
        except Exception:
            for name in ("telegram", "discord", "whatsapp"):
                out[f"{name}_ready"] = False
                out[f"{name}_configured"] = False
                out[f"{name}_running"] = False
        out["bots_ready"] = bots_ready

        return out

    # ---- Phase 5: Swarm Orchestrator -----------------------------------------

    async def swarm_spawn(params: dict, ws) -> dict:
        from backend.swarm.orchestrator import swarm
        name = params.get("name", "unnamed")
        agent_type = params.get("agentType", "general")
        tools = params.get("tools")
        agent = swarm.spawn(name, agent_type, tools=tools)
        return {"agentId": agent.id, "name": agent.name, "type": agent.type, "status": "spawned"}

    async def swarm_list(params: dict, ws) -> dict:
        from backend.swarm.orchestrator import swarm
        return {"agents": swarm.list_agents()}

    async def swarm_run(params: dict, ws) -> dict:
        from backend.swarm.orchestrator import swarm
        agent_id = params.get("agentId", "")
        task = params.get("task", "")
        if not agent_id or not task:
            return {"success": False, "error": "agentId and task required"}
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:
            provider = None
        result = await swarm.run_agent(agent_id, task, provider)
        return result

    async def swarm_stop(params: dict, ws) -> dict:
        from backend.swarm.orchestrator import swarm
        agent_id = params.get("agentId", "")
        ok = swarm.stop(agent_id)
        return {"success": ok}

    async def swarm_flow(params: dict, ws) -> dict:
        """Run several agents in order, each seeing what came before it.

        A step may name `parallel` agents to work it at the same time, a
        `merge` agent to reconcile their answers, an `until` gate with a
        `retry` agent to send rejected work back, and `dependsOn` step numbers
        to wait for.
        """
        from backend.swarm.orchestrator import swarm
        steps = params.get("steps")
        if not isinstance(steps, list) or not steps:
            return {"success": False,
                    "error": "steps must be a list of {agentId, task}"}
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:
            provider = None
        return await swarm.run_flow(
            steps, provider=provider,
            goal=str(params.get("goal") or ""))

    async def swarm_memory(params: dict, ws) -> dict:
        """Read or clear the swarm's shared memory of finished work."""
        from backend.swarm.orchestrator import swarm
        if params.get("clear"):
            dropped = swarm.clear_memory()
            return {"success": True, "cleared": dropped, "entries": []}
        text = swarm.memory_text()
        return {"success": True, "memory": text,
                "entries": len(swarm._memory),
                "chars": len(text)}

    async def swarm_roster(params: dict, ws) -> dict:
        """The saved agent desks, and the built-in types with their skills."""
        from backend.swarm import roster
        return {"success": True,
                "agents": roster.definitions(),
                "types": roster.type_catalogue()}

    async def swarm_define(params: dict, ws) -> dict:
        """Create or update an agent on the roster.

        This is what makes an agent survive a restart. It also spawns it now,
        so defining and using are one step rather than two.
        """
        from backend.swarm.orchestrator import swarm
        from backend.swarm import roster
        entry = {
            "id": str(params.get("id") or ""),
            "name": str(params.get("name") or ""),
            "type": str(params.get("type") or "general"),
            "role": str(params.get("role") or ""),
            "does": str(params.get("does") or ""),
            "prompt": str(params.get("prompt") or ""),
            "brief": str(params.get("brief") or ""),
            "tools": params.get("tools"),
            "skills": params.get("skills"),
            "model": str(params.get("model") or ""),
            "provider": str(params.get("provider") or ""),
            "isLead": bool(params.get("isLead")),
        }
        if not entry["id"]:
            agent = swarm.spawn(
                entry["name"] or "unnamed", entry["type"],
                system_prompt=entry["prompt"], tools=entry["tools"],
                brief=entry["brief"], skills=entry["skills"],
                model=entry["model"], provider=entry["provider"],
                role=entry["role"], does=entry["does"],
                is_lead=entry["isLead"], persist=True)
            return {"success": True, "agentId": agent.id,
                    "agent": swarm.save_agent(agent.id).get("agent")}
        # An existing id: update the definition and respawn it so a running
        # session picks up a changed brief without a restart.
        saved = roster.upsert(entry)
        swarm.stop(saved["id"])
        swarm.spawn_from_roster(saved["id"])
        return {"success": True, "agentId": saved["id"], "agent": saved}

    async def swarm_forget(params: dict, ws) -> dict:
        """Remove an agent and its saved definition."""
        from backend.swarm.orchestrator import swarm
        agent_id = str(params.get("agentId") or params.get("id") or "")
        if not agent_id:
            return {"success": False, "error": "No agentId given."}
        return {"success": swarm.forget(agent_id)}

    async def swarm_restore(params: dict, ws) -> dict:
        """Bring every saved agent back into this session."""
        from backend.swarm.orchestrator import swarm
        return {"success": True, "restored": swarm.load_roster()}

    async def swarm_revise(params: dict, ws) -> dict:
        """Send a deliverable back with a correction, and learn from it.

        The correction is recorded against the agent so it applies next time —
        a standing rule unless it is clearly about just this task. The revision
        itself is a fresh run with the correction appended, so the agent reworks
        the work rather than being told about it and doing nothing.
        """
        from backend.swarm.orchestrator import swarm
        from backend.swarm import roster
        agent_id = str(params.get("agentId") or "")
        correction = str(params.get("correction") or "").strip()
        previous = str(params.get("output") or "")
        if not agent_id or not correction:
            return {"success": False,
                    "error": "Both agentId and correction are required."}
        agent = swarm.get_agent(agent_id)
        if agent is None:
            return {"success": False, "error": f"No agent {agent_id}."}

        # Record it first, so the rework already benefits from it. Default is a
        # standing rule; a caller that knows it is task-specific says so.
        if params.get("oneOff"):
            roster.add_one_off(agent_id, correction)
        else:
            roster.add_rule(agent_id, correction)

        task = ("Revise your last deliverable using this correction. Do not "
                "start over — keep what was right and change what was wrong.\n"
                f"Correction: {correction}\n\n"
                + (f"Your last deliverable:\n{previous[:4000]}\n\n" if previous
                   else ""))
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:  # noqa: BLE001
            provider = None
        result = await swarm.run_agent(agent_id, task, provider)
        return {"success": bool(result.get("success")),
                "response": result.get("response", ""),
                "learned": correction,
                "kind": "one-off" if params.get("oneOff") else "standing",
                "error": result.get("error")}

    async def swarm_notes(params: dict, ws) -> dict:
        """Read or clear the mid-flight notes between agents."""
        from backend.swarm.orchestrator import swarm
        if params.get("clear"):
            return {"success": True, "cleared": swarm.clear_notes()}
        return {"success": True, "notes": list(swarm._notes),
                "count": len(swarm._notes)}

    async def swarm_notebook(params: dict, ws) -> dict:
        """One agent's own history, or a list of every notebook.

        With no `agentId` this lists them, so the page can show which desks
        have a record without loading each one — the contents can be large and
        most of the time nobody wants them.
        """
        from backend.swarm import notebook
        agent_id = str(params.get("agentId") or "").strip()
        if not agent_id:
            return {"success": True, "notebooks": notebook.listing()}
        data = notebook.load(agent_id)
        return {
            "success": True,
            "agentId": agent_id,
            "name": data.get("name", ""),
            "summary": data.get("summary", "")[:notebook.MAX_SUMMARY_CHARS],
            "entries": data.get("entries", []),
            "flows": data.get("flows", []),
            "updated": data.get("updated", 0.0),
            "digest": notebook.digest(agent_id),
        }

    async def swarm_notebook_search(params: dict, ws) -> dict:
        """Ask one agent's notebook for something it did earlier.

        This is the half a summary cannot give: entries the digest has moved
        past are still on disk, so work from forty steps ago is answerable.
        """
        from backend.swarm import notebook
        agent_id = str(params.get("agentId") or "").strip()
        query = str(params.get("query") or "").strip()
        if not agent_id:
            return {"success": False, "error": "agentId is required"}
        if not query:
            return {"success": False, "error": "query is required"}
        hits = notebook.search(agent_id, query)
        return {"success": True, "agentId": agent_id, "query": query,
                "results": hits, "count": len(hits)}

    async def swarm_checkpoints(params: dict, ws) -> dict:
        """Flows that were interrupted and can still be carried on."""
        from backend.swarm.orchestrator import swarm
        return {"success": True, "flows": swarm.resumable_flows()}

    async def swarm_resume(params: dict, ws) -> dict:
        """Carry on an interrupted flow from where it stopped."""
        from backend.swarm.orchestrator import swarm
        flow_id = str(params.get("flowId") or "").strip()
        if not flow_id:
            return {"success": False, "error": "flowId is required"}
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:  # noqa: BLE001
            provider = None
        result = await swarm.resume_flow(flow_id, provider=provider)
        # A resumed flow is a real run, so the page should show it happening.
        try:
            get_server().broadcast_nowait("swarm.updated",
                                          {"agents": swarm.list_agents()})
        except Exception:  # noqa: BLE001
            pass
        return result

    async def swarm_discard_checkpoint(params: dict, ws) -> dict:
        """Throw an interrupted flow away instead of resuming it."""
        from backend.swarm import checkpoints
        flow_id = str(params.get("flowId") or "").strip()
        if not flow_id:
            return {"success": False, "error": "flowId is required"}
        removed = checkpoints.discard(flow_id)
        return {"success": removed, "flowId": flow_id,
                "error": None if removed else "No such checkpoint."}

    # ---- Phase 5: Calendar & Email integrations ------------------------------

    async def calendar_add(params: dict, ws) -> dict:
        from backend.integrations.calendar_integration import calendar
        title = (params.get("title") or "").strip()
        if not title:
            return {"error": "title is required"}
        event = calendar.add_event(
            title=title,
            start=params.get("start", ""),
            end=params.get("end"),
            description=params.get("description", ""),
            location=params.get("location", ""),
        )
        return {"event": event}

    async def calendar_list(params: dict, ws) -> dict:
        from backend.integrations.calendar_integration import calendar
        year = params.get("year")
        month = params.get("month")
        if year and month:
            events = calendar.get_month(int(year), int(month))
        elif params.get("range") == "week":
            events = calendar.get_week()
        elif params.get("range") == "today":
            events = calendar.get_today()
        else:
            events = calendar.get_events()
        return {"events": events, "count": len(events)}

    async def calendar_delete(params: dict, ws) -> dict:
        from backend.integrations.calendar_integration import calendar
        ok = calendar.delete_event(params.get("eventId", ""))
        return {"success": ok}

    async def calendar_update(params: dict, ws) -> dict:
        from backend.integrations.calendar_integration import calendar
        fields = {k: v for k, v in params.get("fields", {}).items()
                  if k in ("title", "start", "end", "description",
                           "location", "reminder_minutes")}
        if not fields:
            return {"error": "no editable fields"}
        event = calendar.update_event(params.get("eventId", ""), fields)
        return {"event": event} if event else {"error": "not found"}

    # ---- Tasks scheduler -------------------------------------------------------

    async def tasks_schedule(params: dict, ws) -> dict:
        from backend.tasks.recurrence import next_run
        from backend.tasks.store import ScheduledTask, task_store
        title = (params.get("title") or "").strip()
        if not title:
            return {"error": "title is required"}
        action = params.get("action", "notify")
        from backend.config import config
        allowed = config.get("scheduling", "llm_actions",
                             default=["notify", "chat"])
        if action not in ("notify", "chat") and action not in allowed:
            return {"error": f"action '{action}' not schedulable"}
        recurrence = params.get("recurrence") or {"type": "none"}
        recurrence.setdefault("weekdays", [])
        task = ScheduledTask(
            id="",
            title=title,
            kind=params.get("kind", "task"),
            action=action,
            payload=params.get("payload", ""),
            time=params.get("time", "09:00"),
            date=params.get("date", ""),
            recurrence=recurrence,
            source=params.get("source", "ui"),
        )
        task.next_run = next_run(task)
        # one-shot with no date and a time already past → roll to tomorrow
        if (task.recurrence or {}).get("type") == "none" and not task.date \
                and task.next_run <= __import__("time").time():
            from datetime import datetime, timedelta
            task.date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            task.next_run = next_run(task)
        added, err = task_store.add(task)
        if added is None:
            return {"error": err}
        from backend.tasks.recurrence import humanize
        return {"task": added.to_dict(), "rule": humanize(added)}

    async def tasks_list(params: dict, ws) -> dict:
        from backend.tasks.scheduler import scheduler
        status = scheduler.status()
        return {"tasks": status["tasks"], "housekeeping": status["housekeeping"],
                "enabled": status["enabled"]}

    async def tasks_update(params: dict, ws) -> dict:
        from backend.tasks.recurrence import next_run
        from backend.tasks.store import task_store
        task = task_store.get(params.get("taskId", ""))
        if task is None:
            return {"error": "not found"}
        fields = {k: v for k, v in params.get("fields", {}).items()
                  if k in ("title", "time", "date", "payload", "action",
                           "recurrence", "enabled")}
        if not fields:
            return {"error": "no editable fields"}
        if "recurrence" in fields:
            fields["recurrence"].setdefault("weekdays", [])
        updated = task_store.update(task.id, fields)
        if any(k in fields for k in ("time", "date", "recurrence")) \
                and updated.enabled:
            task_store.update(task.id, {"next_run": next_run(updated)})
        return {"task": task_store.get(task.id).to_dict()}

    async def tasks_pause(params: dict, ws) -> dict:
        from backend.tasks.store import task_store
        return {"task": task_store.update(params.get("taskId", ""),
                                          {"enabled": False}).to_dict()}

    async def tasks_resume(params: dict, ws) -> dict:
        from backend.tasks.recurrence import next_run
        from backend.tasks.store import task_store
        task = task_store.update(params.get("taskId", ""), {"enabled": True})
        if task is not None and task.next_run <= 0:
            task_store.update(task.id, {"next_run": next_run(task)})
        return {"task": task.to_dict()} if task else {"error": "not found"}

    async def tasks_cancel(params: dict, ws) -> dict:
        from backend.tasks.store import task_store
        return {"success": task_store.delete(params.get("taskId", ""))}

    async def tasks_month(params: dict, ws) -> dict:
        from backend.tasks.recurrence import expand_month, humanize
        from backend.tasks.store import task_store
        year = int(params.get("year", 2026))
        month = int(params.get("month", 1))
        rows = []
        for task in task_store.list_all(enabled=True):
            for day in expand_month(task, year, month):
                rows.append({"date": day, "task_id": task.id,
                             "title": task.title, "action": task.action,
                             "time": task.time, "kind": task.kind,
                             "rule": humanize(task)})
        return {"rows": rows}

    async def mood_status(params: dict, ws) -> dict:
        from backend.character.mood import mood_engine
        return {"mood": mood_engine.state()}

    async def journal_list(params: dict, ws) -> dict:
        from backend.memory.journal import list_days
        return {"days": list_days(int(params.get("limit", 14)))}

    async def journal_today(params: dict, ws) -> dict:
        from backend.memory.journal import get_day
        day = get_day()
        return {"date": day["date"], "entries": day["entries"][-100:],
                "summary": day["summary"]}

    async def profile_get(params: dict, ws) -> dict:
        from backend.memory.user_profile import get_profile
        return {"profile": get_profile()}

    async def profile_update(params: dict, ws) -> dict:
        """Edit the learned profile.

        `update_profile()` has always existed and its docstring says
        "(dashboard)", but no handler was ever registered — the Memory page
        could display the profile and never change it.
        """
        from backend.memory.user_profile import update_profile
        fields = params.get("fields")
        if not isinstance(fields, dict) or not fields:
            return {"success": False,
                    "error": "Pass 'fields' with one or more of: tone, hours, "
                             "rituals, preferences"}
        try:
            return {"success": True, "profile": update_profile(fields)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---- Bot bridges ---------------------------------------------------------

    async def bots_status(params: dict, ws) -> dict:
        """What the Bots page needs: checks, not assumed state."""
        try:
            from backend.bots import manager as bots
            return {"success": True, **bots.status()}
        except Exception as e:
            log.debug("bots.status failed: %s", e)
            return {"success": False, "error": str(e), "platforms": {}}

    async def bots_set_token(params: dict, ws) -> dict:
        from backend.bots import manager as bots
        platform = str(params.get("platform") or "").strip().lower()
        token = str(params.get("token") or "").strip()
        if platform not in bots.PLATFORMS:
            return {"success": False,
                    "error": f"Unknown bot '{platform}'. Known: "
                             + ", ".join(bots.PLATFORMS)}
        if not bots.set_token(platform, token):
            return {"success": False, "error": "Could not save the token"}
        return {"success": True, "saved": bool(token)}

    async def bots_start(params: dict, ws) -> dict:
        from backend.bots import manager as bots
        platform = str(params.get("platform") or "").strip().lower()
        return await bots.start(platform)

    async def bots_stop(params: dict, ws) -> dict:
        from backend.bots import manager as bots
        platform = str(params.get("platform") or "").strip().lower()
        return await bots.stop(platform)

    async def project_search(params: dict, ws) -> dict:
        from backend.project.indexer import search_project
        results = search_project(str(params.get("query", "")),
                                 top_k=int(params.get("top_k", 6)))
        return {"results": results, "count": len(results)}

    async def project_patterns(params: dict, ws) -> dict:
        from backend.project.patterns import detect_patterns
        return {"patterns": detect_patterns()}

    async def project_status(params: dict, ws) -> dict:
        from backend.project.indexer import status
        return status()

    async def email_fetch(params: dict, ws) -> dict:
        from backend.integrations.email_integration import email_client
        unread = email_client.fetch_unread(limit=params.get("limit", 10))
        return {"emails": unread, "count": len(unread)}

    async def email_send(params: dict, ws) -> dict:
        from backend.integrations.email_integration import email_client
        result = email_client.send(
            to=params.get("to", ""),
            subject=params.get("subject", ""),
            body=params.get("body", ""),
            html=params.get("html", False),
        )
        return result

    async def email_search(params: dict, ws) -> dict:
        from backend.integrations.email_integration import email_client
        results = email_client.search(params.get("query", ""), limit=params.get("limit", 20))
        return {"emails": results, "count": len(results)}

    # ---- Phase 8: Playwright Browser integration ----------------------------

    async def browser_navigate(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.navigate(params.get("url", ""))

    async def browser_go_back(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.go_back()

    async def browser_go_forward(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.go_forward()

    async def browser_click(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.click(
            selector=params.get("selector"),
            x=params.get("x"),
            y=params.get("y"),
        )

    async def browser_type(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.type_text(
            selector=params.get("selector", "body"),
            text=params.get("text", ""),
        )

    async def browser_screenshot(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.screenshot(full_page=params.get("fullPage", False))

    async def browser_extract(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.extract(selector=params.get("selector"))

    async def browser_close(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.close()

    async def browser_status(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        return await browser.status()

    async def browser_task(params: dict, ws) -> dict:
        from backend.browser.browser_engine import browser
        task = str(params.get("task", "")).strip()
        if not task:
            return {"success": False, "error": "task is required"}
        return await browser.route_task(task, params.get("max_steps"))

    async def browser_install_approve(params: dict, ws) -> dict:
        """Run a dashboard-approved backend install (auto_install=ask flow)."""
        from backend.browser.auto_install import approve
        return await approve(str(params.get("backend", "")))

    async def browser_install_status(params: dict, ws) -> dict:
        """What the Settings browser page draws: installed, installing, version.

        One call for both backends, so the page does not have to make two round
        trips and reconcile them.
        """
        from backend.browser.auto_install import status_all
        return status_all()

    async def browser_install_now(params: dict, ws) -> dict:
        """Install a browser backend because the user pressed the button.

        Distinct from `browser.installApprove`, which answers the ask-banner.
        This one is an explicit instruction: it clears the failure backoff so a
        press always tries, and reports why when it cannot.
        """
        from backend.browser.auto_install import approve, installing, is_installed
        backend = str(params.get("backend", "playwright") or "playwright")
        if backend not in ("playwright", "framework"):
            return {"success": False, "error": f"Unknown backend '{backend}'."}
        if is_installed(backend):
            return {"success": True, "status": "installed",
                    "message": "Already installed."}
        if installing(backend):
            return {"success": True, "status": "installing",
                    "message": "An install is already running."}
        result = await approve(backend)
        return {
            "success": bool(result.get("success")),
            "status": result.get("status", "")
            if result else "failed",
            "error": "" if result.get("success")
            else ("The install did not finish. Check the network and try "
                  "again — the reason is in the log."),
        }

    # ---- Local model (llamafile) — ask-then-download flow -------------------

    async def localllm_status(params: dict, ws) -> dict:
        from backend.local_llm.manager import local_llm
        data = await asyncio.to_thread(local_llm.status)
        try:
            data["hf"] = await asyncio.to_thread(local_llm.hf_status)
        except Exception as exc:
            data["hf"] = {"deps_ready": False, "error": str(exc)}
        return data

    # ---- the OpenAI-compatible endpoint for outside agent tools --------------

    async def modelapi_status(params: dict, ws) -> dict:
        """Everything Settings needs to show and copy the endpoint.

        `status()` reads config and the local-model manager, both of which can
        block, so it runs off the event loop.
        """
        from backend.model_api import model_api
        try:
            return await asyncio.to_thread(model_api.status)
        except Exception as e:  # noqa: BLE001
            return {"enabled": False, "running": False, "error": str(e)}

    async def modelapi_start(params: dict, ws) -> dict:
        from backend.model_api import model_api
        try:
            ok, detail = await asyncio.to_thread(model_api.start)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": str(e)}
        return {"success": ok, "detail": detail,
                "status": await asyncio.to_thread(model_api.status)}

    async def modelapi_stop(params: dict, ws) -> dict:
        from backend.model_api import model_api
        try:
            await asyncio.to_thread(model_api.stop)
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": str(e)}
        return {"success": True, "status": await asyncio.to_thread(
            model_api.status)}

    async def localllm_install(params: dict, ws) -> dict:
        """User approved the download — start it (resumable)."""
        from backend.local_llm.manager import local_llm
        local_llm.approve()
        return await local_llm.install()

    async def localllm_decline(params: dict, ws) -> dict:
        """User said no — fall back to OpenRouter and ask for an API key."""
        from backend.local_llm.manager import local_llm
        return local_llm.decline()

    async def localllm_start(params: dict, ws) -> dict:
        from backend.local_llm.manager import local_llm
        ok, problem = await local_llm.start()
        return {"success": ok, "error": problem, "status": local_llm.status()}

    async def localllm_stop(params: dict, ws) -> dict:
        from backend.local_llm.manager import local_llm
        await local_llm.stop()
        return {"success": True, "status": local_llm.status()}

    async def localllm_remove(params: dict, ws) -> dict:
        from backend.local_llm.manager import local_llm
        return await local_llm.remove()

    async def localllm_set_option(params: dict, ws) -> dict:
        """Toggle keep-running / autostart-on-select for the local model."""
        from backend.local_llm.manager import local_llm
        return await local_llm.set_option(str(params.get("key", "")),
                                          params.get("value"))

    async def localllm_hf_install(params: dict, ws) -> dict:
        """Install the optional torch + transformers pair for HF (Local)."""
        from backend.local_llm.manager import local_llm
        return await local_llm.install_hf_deps()

    async def hf_unload(params: dict, ws) -> dict:
        """Free RAM by dropping the in-process Hugging Face model."""
        from backend.providers import huggingface_local_provider as hf
        try:
            await asyncio.to_thread(hf.unload)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True}

    # ---- Skill Forge — self-extending capabilities --------------------------

    async def forge_create(params: dict, ws) -> dict:
        from backend.skills.forge import skill_forge
        from backend.providers.registry import get_provider
        task = params.get("task", params.get("description", ""))
        if not task:
            return {"success": False, "error": "No task description provided"}
        try:
            provider = get_provider()
        except Exception:
            provider = None
        result = await skill_forge.forge(task_description=task, provider=provider,
                                         auto_validate=params.get("validate", True))
        return {
            "success": result.success,
            "skillName": result.skill_name,
            "action": result.action,
            "detail": result.detail,
        }

    async def forge_list(params: dict, ws) -> dict:
        from backend.skills.forge import skill_forge
        skills = skill_forge.list_forged()
        return {"skills": skills, "count": len(skills)}

    # ---- Skill management (market + toggles) -------------------------------

    async def skills_list(params: dict, ws) -> dict:
        from backend.skills.registry import skill_registry
        out = []
        for s in sorted(skill_registry.list_all(),
                        key=lambda x: (x.category, x.name)):
            out.append({
                "name": s.name,
                "category": s.category,
                "description": s.description[:300],
                "requires_approval": bool(s.requires_approval),
                "enabled": skill_registry.is_enabled(s.name),
                # What the card's switch shows. `grantable` is False for the
                # skills the destruction gate owns, so the card can disable the
                # control instead of offering a button that would only fail.
                "always_allowed": skill_registry.is_always_allowed(s.name),
                "deletable": s.category in ("forged", "market"),
                "source": s.category,
            })
        try:
            from backend.approvals import policy
            granted = set(policy.list_allowed()["skills"])
            grantable = {s["name"]: not policy.is_protected(s["name"])
                         for s in out}
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the approval policy: %s", e)
            granted, grantable = set(), {}
        for s in out:
            s["grantable"] = grantable.get(s["name"], False)
            # A granted skill whose code no longer matches the grant asks
            # again. Saying so beats a switch that looks on and behaves off.
            s["code_changed"] = bool(
                s["category"] == "market"
                and s["name"] in granted
                and not s["always_allowed"])
        return {"skills": out, "count": len(out)}

    async def skills_set_state(params: dict, ws) -> dict:
        from backend.config import config
        name = str(params.get("name", "")).strip()
        enabled = bool(params.get("enabled", True))
        if not name:
            return {"success": False, "error": "name is required"}
        disabled = list(config.get("skills", "disabled", default=[]) or [])
        if enabled:
            disabled = [d for d in disabled if d != name]
        elif name not in disabled:
            disabled.append(name)
        config.set("skills", "disabled", value=disabled)
        return {"success": True, "name": name, "enabled": enabled}

    async def skills_delete(params: dict, ws) -> dict:
        from backend.skills.registry import skill_registry
        name = str(params.get("name", "")).strip()
        if not name:
            return {"success": False, "error": "name is required"}
        skill = skill_registry.get(name)
        if not skill:
            return {"success": False, "error": f"Skill not found: {name}"}
        if skill.category == "market":
            from backend.skills.market import market
            ok = market.delete(name)
        elif skill.category == "forged":
            from backend.skills.forge import skill_forge
            ok = skill_forge.delete(name)
        else:
            return {"success": False,
                    "error": "Built-in skills cannot be deleted — disable them instead"}
        return {"success": ok}

    async def skills_search_market(params: dict, ws) -> dict:
        from backend.skills.market_search import search
        q = str(params.get("query", "")).strip()
        if not q:
            return {"success": False, "error": "query is required"}
        return {"success": True, "results": await search(q, limit=8)}

    async def skills_install_from(params: dict, ws) -> dict:
        from backend.skills.market import market
        repo = str(params.get("repo", "")).strip()
        url = str(params.get("url", "")).strip()
        if not repo and not url:
            return {"success": False, "error": "Provide 'url' or 'repo'"}
        try:
            if url or repo.startswith(("http://", "https://")):
                meta = market.install_from_url(url or repo)
            else:
                meta = market.install_from_github(
                    repo,
                    str(params.get("path", "")).strip(),
                    ref=str(params.get("ref", "")).strip(),
                )
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "skill": meta["name"],
                "scripts": meta.get("scripts", []),
                "license": meta.get("license", "")}

    # ---- Sprite skins (codex-pet style character body) ----------------------

    async def character_skins_list(params: dict, ws) -> dict:
        from backend.character import sprite_skin
        return {"skins": sprite_skin.list_skins(),
                "active": sprite_skin.get_active_skin_id()}

    async def character_upload_skin(params: dict, ws) -> dict:
        import base64
        from backend.character import sprite_skin
        name = (params.get("name") or "skin").strip()
        filename = params.get("filename", "")
        data_b64 = params.get("data", "")
        if not data_b64:
            return {"success": False, "error": "No file data provided"}
        try:
            raw = base64.b64decode(data_b64)
        except Exception:
            return {"success": False, "error": "Invalid base64 data"}
        # Branch on file type: ZIP of per-state GIFs, or a single GIF
        if raw[:2] == b"PK":
            save_fn = sprite_skin.save_uploaded_zip
        elif raw[:6] in sprite_skin.GIF_MAGIC:
            save_fn = sprite_skin.save_uploaded_skin
        else:
            return {"success": False,
                    "error": "Only .gif or .zip files are supported"}
        try:
            skin = save_fn(name, filename, raw)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        # Apply immediately so the character switches right away
        sprite_skin.set_active_skin(skin["id"])
        if _engine_ref is not None:
            widget = getattr(_engine_ref, "_char_widget", None)
            if widget is not None:
                widget.request_apply_skin(skin["id"])  # thread-safe
        await get_server().broadcast("character.skinChanged",
                                     {"skinId": skin["id"], "active": skin["id"]})
        skin["active"] = True
        return {"success": True, "skin": skin}

    async def character_set_skin(params: dict, ws) -> dict:
        from backend.character import sprite_skin
        skin_id = params.get("id")
        if not skin_id or skin_id == "none":
            sprite_skin.set_active_skin("")
            if _engine_ref is not None:
                widget = getattr(_engine_ref, "_char_widget", None)
                if widget is not None:
                    widget.request_apply_skin(None)
            await get_server().broadcast("character.skinChanged",
                                         {"skinId": None, "active": ""})
            return {"success": True, "active": ""}
        if not sprite_skin.resolve_clip_path(skin_id, "idle"):
            return {"success": False, "error": f"Skin not found: {skin_id}"}
        sprite_skin.set_active_skin(skin_id)
        if _engine_ref is not None:
            widget = getattr(_engine_ref, "_char_widget", None)
            if widget is not None:
                widget.request_apply_skin(skin_id)
        await get_server().broadcast("character.skinChanged",
                                     {"skinId": skin_id, "active": skin_id})
        return {"success": True, "active": skin_id}

    async def character_delete_skin(params: dict, ws) -> dict:
        from backend.character import sprite_skin
        skin_id = params.get("id", "")
        ok = sprite_skin.delete_skin(skin_id)
        if not ok:
            return {"success": False, "error": f"Skin not found: {skin_id}"}
        # If the active skin was deleted, revert the character body
        if sprite_skin.get_active_skin_id() == "":
            if _engine_ref is not None:
                widget = getattr(_engine_ref, "_char_widget", None)
                if widget is not None:
                    widget.request_apply_skin(None)
        await get_server().broadcast("character.skinChanged",
                                     {"skinId": None,
                                      "active": sprite_skin.get_active_skin_id()})
        return {"success": True, "deleted": skin_id}

    # ---- Register all handlers -----------------------------------------------

    # Phase 1-3 core handlers
    _server.register("chat.send", chat_send)
    _server.register("chat.history", chat_get_history)
    _server.register("action.execute", action_execute)
    _server.register("action.approve", action_approve)
    _server.register("action.deny", action_deny)
    _server.register("action.pending", action_pending)
    _server.register("approvals.list", approvals_list)
    _server.register("approvals.alwaysAllow", approvals_always_allow)
    _server.register("approvals.revoke", approvals_revoke)
    _server.register("voice.speak", voice_speak)
    _server.register("voice.voices", voice_voices)
    _server.register("character.setState", character_set_state)
    _server.register("observer.status", observer_status)
    _server.register("system.status", system_status)
    _server.register("system.getProviders", system_get_providers)
    _server.register("system.rtkStatus", system_rtk_status)
    _server.register("system.rtkInstall", system_rtk_install)
    _server.register("system.uvStatus", system_uv_status)
    _server.register("system.uvInstall", system_uv_install)
    _server.register("desktop.status", desktop_status)
    _server.register("desktop.grant", desktop_grant)
    _server.register("desktop.revoke", desktop_revoke)
    _server.register("settings.get", settings_get)
    _server.register("settings.set", settings_set)
    _server.register("system.setAutostart", system_set_autostart)
    _server.register("workspace.status", workspace_status)
    _server.register("models.routes", models_routes)
    _server.register("models.catalog", models_catalog)
    _server.register("models.refresh", models_refresh)
    _server.register("guidelines.state", guidelines_state)
    _server.register("guidelines.refresh", guidelines_refresh)
    _server.register("mcp.list", mcp_list)
    _server.register("mcp.add", mcp_add)
    _server.register("mcp.update", mcp_update)
    _server.register("mcp.remove", mcp_remove)
    _server.register("mcp.connect", mcp_connect)
    _server.register("mcp.disconnect", mcp_disconnect)
    _server.register("mcp.reload", mcp_reload)
    _server.register("mcp.tools", mcp_tools)
    _server.register("mcp.searchMarket", mcp_search_market)
    _server.register("mcp.install", mcp_install)
    _server.register("mcp.credentials", mcp_credentials)
    _server.register("mcp.sweep", mcp_sweep)
    _server.register("mcp.setToolApproval", mcp_set_tool_approval)

    # Phase 5 Goals engine
    _server.register("goal.create", goal_create)
    _server.register("goal.list", goal_list)
    _server.register("goal.get", goal_get)
    _server.register("goal.replan", goal_replan)
    _server.register("goal.start", goal_start)
    _server.register("goal.cancel", goal_cancel)

    # Phase 5 Code engine
    _server.register("code.bind", code_bind)
    _server.register("code.read", code_read)
    _server.register("code.plan", code_plan)
    _server.register("code.edit", code_edit)
    _server.register("code.apply", code_apply)
    _server.register("code.applyPlan", code_apply_plan)
    _server.register("code.write", code_write)
    _server.register("code.grep", code_grep)
    _server.register("code.git.status", code_git_status)
    _server.register("code.git.diff", code_git_diff)
    _server.register("code.git.revert", code_git_revert)
    _server.register("code.verify", code_verify)

    # Guide page: which features are usable here (no strings, no secrets)
    _server.register("guide.status", guide_status)

    # Phase 5 Swarm orchestrator
    _server.register("swarm.spawn", swarm_spawn)
    _server.register("swarm.list", swarm_list)
    _server.register("swarm.run", swarm_run)
    _server.register("swarm.stop", swarm_stop)
    _server.register("swarm.flow", swarm_flow)
    _server.register("swarm.memory", swarm_memory)
    _server.register("swarm.roster", swarm_roster)
    _server.register("swarm.define", swarm_define)
    _server.register("swarm.forget", swarm_forget)
    _server.register("swarm.restore", swarm_restore)
    _server.register("swarm.revise", swarm_revise)
    _server.register("swarm.notes", swarm_notes)
    _server.register("swarm.notebook", swarm_notebook)
    _server.register("swarm.notebookSearch", swarm_notebook_search)
    _server.register("swarm.checkpoints", swarm_checkpoints)
    _server.register("swarm.resume", swarm_resume)
    _server.register("swarm.discardCheckpoint", swarm_discard_checkpoint)

    # Phase 5 Calendar & Email
    _server.register("calendar.add", calendar_add)
    _server.register("calendar.list", calendar_list)
    _server.register("calendar.delete", calendar_delete)
    _server.register("calendar.update", calendar_update)
    _server.register("email.fetch", email_fetch)
    _server.register("email.send", email_send)
    _server.register("email.search", email_search)

    # Tasks scheduler
    _server.register("tasks.schedule", tasks_schedule)
    _server.register("tasks.list", tasks_list)
    _server.register("tasks.update", tasks_update)
    _server.register("tasks.pause", tasks_pause)
    _server.register("tasks.resume", tasks_resume)
    _server.register("tasks.cancel", tasks_cancel)
    _server.register("tasks.month", tasks_month)
    _server.register("mood.status", mood_status)
    _server.register("journal.list", journal_list)
    _server.register("journal.today", journal_today)
    _server.register("profile.get", profile_get)
    _server.register("profile.update", profile_update)
    _server.register("bots.status", bots_status)
    _server.register("bots.setToken", bots_set_token)
    _server.register("bots.start", bots_start)
    _server.register("bots.stop", bots_stop)
    _server.register("project.search", project_search)
    _server.register("project.patterns", project_patterns)
    _server.register("project.status", project_status)

    # Browser (Playwright)
    _server.register("browser.navigate", browser_navigate)
    _server.register("browser.go_back", browser_go_back)
    _server.register("browser.go_forward", browser_go_forward)
    _server.register("browser.click", browser_click)
    _server.register("browser.type", browser_type)
    _server.register("browser.screenshot", browser_screenshot)
    _server.register("browser.extract", browser_extract)
    _server.register("browser.close", browser_close)
    _server.register("browser.status", browser_status)
    _server.register("browser.task", browser_task)
    _server.register("browser.installApprove", browser_install_approve)
    _server.register("browser.installStatus", browser_install_status)
    _server.register("browser.installNow", browser_install_now)
    _server.register("localLlm.status", localllm_status)
    _server.register("localLlm.installApprove", localllm_install)
    _server.register("localLlm.installDecline", localllm_decline)
    _server.register("localLlm.start", localllm_start)
    _server.register("localLlm.stop", localllm_stop)
    _server.register("localLlm.remove", localllm_remove)
    _server.register("localLlm.setOption", localllm_set_option)
    _server.register("localLlm.installHfDeps", localllm_hf_install)
    _server.register("hf.unload", hf_unload)
    _server.register("modelApi.status", modelapi_status)
    _server.register("modelApi.start", modelapi_start)
    _server.register("modelApi.stop", modelapi_stop)

    # Skill Forge
    _server.register("forge.create", forge_create)
    _server.register("forge.list", forge_list)

    # Skill management
    _server.register("skills.list", skills_list)
    _server.register("skills.setState", skills_set_state)
    _server.register("skills.delete", skills_delete)
    _server.register("skills.searchMarket", skills_search_market)
    _server.register("skills.installFrom", skills_install_from)

    # Excel operations
    _server.register("excel.read", excel_read)
    _server.register("excel.write", excel_write)

    # Privacy + egress
    _server.register("privacy.setZones", privacy_set_zones)
    _server.register("privacy.list", privacy_list)
    _server.register("privacy.excludeApp", privacy_exclude_app)
    _server.register("privacy.removeApp", privacy_remove_app)
    _server.register("egress.list", egress_list)
    _server.register("snapshot.list", snapshot_list)
    _server.register("memory.list", memory_list)
    _server.register("memory.deleteSummary", memory_delete_summary)
    _server.register("memory.deleteMemory", memory_delete_memory)
    _server.register("memory.clear", memory_clear)
    _server.register("memory.getFacts", memory_get_facts)
    _server.register("memory.setFact", memory_set_fact)
    _server.register("memory.deleteFact", memory_delete_fact)
    _server.register("memory.listTriples", memory_list_triples)
    _server.register("memory.deleteTriple", memory_delete_triple)
    _server.register("memory.links", memory_links)
    _server.register("memory.addLink", memory_add_link)
    _server.register("memory.deleteLink", memory_delete_link)
    _server.register("memory.related", memory_related)
    _server.register("memory.files", memory_files)
    _server.register("memory.pruneLinks", memory_prune_links)
    _server.register("memory.health", memory_health)
    _server.register("memory.reembed", memory_reembed)
    _server.register("wiki.list", wiki_list)
    _server.register("wiki.get", wiki_get)
    _server.register("wiki.save", wiki_save)
    _server.register("wiki.delete", wiki_delete)
    _server.register("wiki.search", wiki_search)
    _server.register("wiki.ingest", wiki_ingest)
    _server.register("wiki.lint", wiki_lint)
    _server.register("wiki.refreshLinks", wiki_refresh_links)
    _server.register("sop.status", sop_status)
    _server.register("sop.list", sop_list)
    _server.register("sop.get", sop_get)
    _server.register("sop.save", sop_save)
    _server.register("sop.delete", sop_delete)
    _server.register("sop.reseed", sop_reseed)
    _server.register("remote.status", remote_status)
    _server.register("remote.setPassword", remote_set_password)
    _server.register("remote.generatePassword", remote_generate_password)
    _server.register("remote.sessions", remote_sessions)
    _server.register("remote.revoke", remote_revoke)
    _server.register("remote.revokeAll", remote_revoke_all)
    _server.register("remote.start", remote_start)
    _server.register("remote.stop", remote_stop)
    _server.register("tailscale.status", tailscale_status)
    _server.register("tailscale.login", tailscale_login)
    _server.register("tailscale.logout", tailscale_logout)
    _server.register("tailscale.down", tailscale_down)
    _server.register("tailscale.enableServe", tailscale_enable_serve)
    _server.register("tailscale.disableServe", tailscale_disable_serve)
    _server.register("tailscale.installStatus", tailscale_install_status)
    _server.register("tailscale.install", tailscale_install)

    # Sprite skins
    _server.register("character.skinsList", character_skins_list)
    _server.register("character.uploadSkin", character_upload_skin)
    _server.register("character.setSkin", character_set_skin)
    _server.register("character.deleteSkin", character_delete_skin)
