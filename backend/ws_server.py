"""
WebSocket JSON-RPC 2.0 server.

Handles all communication between Dashboard/Bots and the Python backend.
Runs on ws://127.0.0.1:9876.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger("addled.ws")

# Reference to the engine instance (set by main.py after engine is created)
_engine_ref = None

# Pending code edits awaiting user approval: {workspaceId::filePath: content}
_pending_edits: dict[str, str] = {}

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
        """Thread-safe fire-and-forget broadcast. Callable from any thread
        (e.g. the engine loop, which runs on its own event loop)."""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast(method, params), self._loop)

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
        """Handle a single WebSocket connection."""
        self._connections.add(ws)
        context = self._note_remote(ws)
        peer = ws.remote_address
        if context["remote"]:
            log.info("Remote client connected: session=%s via %s from %s",
                     context["session"] or "?", peer,
                     context["addr"] or "?")
        else:
            log.info("Client connected: %s", peer)

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    response = await self._dispatch(msg, ws)
                    if response is not None:
                        await ws.send(json.dumps(response))
                except json.JSONDecodeError:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }))
                except Exception:
                    log.exception("Error handling message from %s", peer)
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": "Internal error"},
                    }))
        except websockets.ConnectionClosed:
            log.info("Client disconnected: %s", peer)
        finally:
            self._connections.discard(ws)

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


async def run_chat_pipeline(message: str, params: dict | None = None) -> dict:
    """Shared chat pipeline — used by chat.send and the voice listener."""
    params = params or {}
    from backend.config import config

    # Compaction shares the provider with chat. Mark the turn so that
    # background summarization yields instead of making the user's next message
    # queue behind it on a single-generation local model.
    try:
        from backend.memory.compaction import note_activity
        note_activity()
    except Exception:
        pass

    # Character shows the THINKING animation while the LLM works
    if _engine_ref is not None:
        _engine_ref._chat_busy = True
        try:
            _engine_ref.sig_agent_state.emit("thinking")
        except Exception:
            pass
    try:
        response = await _run_chat_pipeline_inner(message, params)
        # Surface provider/connection failures as the ERROR character state
        text = response.get("response", "") if isinstance(response, dict) else ""
        try:
            from backend.character.mood import mood_engine
            if text.startswith(("[Not connected:", "[Provider")):
                mood_engine.event("task_failure")
            else:
                mood_engine.event("chat_reply")
        except Exception:
            pass
        # Episodic timeline: journal every real turn
        try:
            from backend.memory.journal import record
            record("user", message)
            if not text.startswith(("[Not connected:", "[Provider")):
                record("assistant", text[:500])
        except Exception:
            pass
        if text.startswith(("[Not connected:", "[Provider")) and _engine_ref is not None:
            try:
                _engine_ref.sig_agent_state.emit("error")
            except Exception:
                pass
        return response
    finally:
        if _engine_ref is not None:
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


async def _run_chat_pipeline_inner(message: str, params: dict | None = None) -> dict:
    from backend.config import config

    # params is optional; several paths below read from it unconditionally.
    params = params or {}

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

        provider = get_provider()
        egress.record("chat.send", {
            "provider": getattr(provider, "provider_id", "?"),
            "payload_chars": len(message),
        })
        agent_name = config.agent_name
        sys_prompt = config.get("chat", "system_prompt", default="") or ""
        sys_prompt = sys_prompt.replace("{agent_name}", agent_name)
        if agent_name != "Addled":
            # also fix persisted prompts that hardcode the old name
            sys_prompt = sys_prompt.replace("Addled", agent_name)
        if not sys_prompt.strip():
            sys_prompt = (f"You are {agent_name}, a helpful AI desktop "
                          "companion with access to system tools.")
        # Core memory: durable facts the agent saved about the user
        from backend.memory.facts import build_facts_context
        facts_ctx = build_facts_context()
        if facts_ctx:
            sys_prompt = sys_prompt + "\n\n" + facts_ctx

        # User model: learned profile (preferences, rituals, hours, tone)
        from backend.memory.user_profile import build_profile_context
        profile_ctx = build_profile_context()
        if profile_ctx:
            sys_prompt = sys_prompt + "\n\n" + profile_ctx

        # Episodic timeline: recent day summaries (persistent identity)
        from backend.memory.journal import build_timeline_context
        timeline_ctx = build_timeline_context()
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

        # Memory-grounded conversation: volunteer relevant past organically
        sys_prompt += ("\n\nIf a saved fact, a previous conversation, or a "
                       "recent day's summary is clearly relevant to this "
                       "conversation, mention it naturally (e.g. 'last time "
                       "we...', 'you mentioned before that...'). Do not force "
                       "it when nothing fits.")

        # Language mirroring: answer in the user's language
        sys_prompt += ("\n\nAlways reply in the same language the user "
                       "writes or speaks in.")

        context = chat_history.get_context(max_messages=config.get("chat", "context_messages", default=20))

        # Long-term recall: inject relevant past conversation turns
        # (semantic + hybrid when enabled; legacy hash path otherwise)
        from backend.memory.recall import (build_memory_context,
                                           build_memory_context_hybrid)
        if config.get("memory", "semantic_embeddings", default=True):
            memory_ctx = await build_memory_context_hybrid(message)
        else:
            memory_ctx = build_memory_context(message)
        user_messages = [
            {"role": m["role"], "content": m["content"]} for m in context
        ]
        if memory_ctx:
            user_messages.insert(0, {"role": "user", "content": memory_ctx})

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
            if anchors:
                get_server().broadcast_nowait("memory.anchors",
                                               {"anchors": anchors})
        except Exception:
            pass

        # Session continuity: recent session summaries (long-run memory)
        from backend.memory.session_summary import build_session_context
        session_ctx = build_session_context()
        if session_ctx:
            user_messages.insert(0, {"role": "user", "content": session_ctx})

        # Rolling conversation continuity (long chats — compaction summaries)
        from backend.memory.compaction import build_rolling_context
        rolling_ctx = build_rolling_context()
        if rolling_ctx:
            user_messages.insert(0, {"role": "user", "content": rolling_ctx})

        # Temporal fact triples (Graphiti-lite) — relational questions only
        rel_kw = ("decide", "decided", "prefer", "preference", "favorite",
                  "project", "working on", "what is my", "name of", "use for",
                  "remember", "know about", "related", "context", "history",
                  "last time", "why did", "who is", "where is", "which file",
                  "document", "notes", "note about")
        triples = []
        if any(k in message.lower() for k in rel_kw):
            try:
                from backend.memory.knowledge_graph import kg
                triples = kg.semantic_search(message, top_k=8)
                if triples:
                    lines = "\n".join(
                        f"- {t['subject']} {t['relation']} {t['object']}"
                        for t in triples)
                    user_messages.insert(0, {"role": "user", "content":
                        "[Saved facts] Things on record about the user:\n" +
                        lines + "\nUse them when relevant."})
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
                    user_messages.insert(0, {"role": "user", "content":
                        "[Related] Connected in the user's memory graph:\n" +
                        "\n".join(related_lines) +
                        "\nMention these only when they help the answer."})
        except Exception as e:
            log.debug("related lookup failed: %s", e)

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
                force_role=explicit_role,
            )
            log.debug("Chat route: role=%s model=%s provider=%s",
                      role, route_model,
                      getattr(provider, "provider_id", "?"))
        except Exception as e:
            log.debug("Model routing failed, using provider default: %s", e)

        # Use the provider-agnostic tool-use loop
        result = await chat_with_tools(
            provider=provider,
            messages=user_messages,
            system_prompt=sys_prompt,
            max_tool_rounds=params.get("maxToolRounds", 5),
            model=route_model,
        )

        response_text = result.get("response", "")
        tool_rounds = result.get("tool_rounds", 0)
        tool_results = result.get("tool_results", [])

        if response_text and not response_text.startswith("[Provider:") and not response_text.startswith("[Not connected:"):
            chat_history.add_message("user", message)
            chat_history.add_message("assistant", response_text,
                tokens={"in": result.get("tokens", 0), "out": 0})
            # Remember for the long term
            from backend.memory.recall import remember_async
            await remember_async("user", message)
            await remember_async("assistant", response_text)
            return {
                "response": response_text,
                "tokens": result.get("tokens", 0),
                "conversationId": chat_history.current_conversation_id,
                "toolRounds": tool_rounds,
                "toolResults": len(tool_results),
                "memoryRecall": bool(memory_ctx),
                "role": role,
                "model": route_model or "",
            }
        return {"response": response_text or "I couldn't process that request.",
                "tokens": result.get("tokens", 0), "conversationId": None,
                "role": role, "model": route_model or ""}
    except Exception as e:
        log.exception("Chat failed")
        return {"response": f"[Not connected: {e}] Configure an AI provider in Settings.", "tokens": 0, "conversationId": None}


async def start_ws_server(host: str = "127.0.0.1", port: int = 9876):
    """Start the singleton WebSocket server."""
    _register_default_handlers()
    await _server.start(host, port)


def _register_default_handlers():
    """Register built-in JSON-RPC handlers."""

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
        """Whether the RTK token-saver binary is available."""
        from backend.actions.terminal import _find_rtk
        from backend.config import config
        path = _find_rtk()
        return {"available": path is not None,
                "path": path,
                "enabled": bool(config.get("tools", "rtk_enabled",
                                            default=True))}

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
        """Add a server from the registry and connect it."""
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
            )
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
        return {"success": True, **describe()}

    # ---- Phase 3: Chat with prompt guard + provider integration ---------------

    async def chat_send(params: dict, ws) -> dict:
        message = params.get("message", "")
        if not message:
            return {"response": "I didn't catch that.", "tokens": 0, "conversationId": None}
        result = await run_chat_pipeline(message, params)
        # Rolling compaction: summarize the oldest turns in the background
        # once the conversation outgrows the context window.
        try:
            reply = result.get("response", "") if isinstance(result, dict) else ""
            if reply and not reply.startswith(("[Not connected:", "[Provider")):
                from backend.memory.compaction import maybe_compact
                asyncio.create_task(maybe_compact())
        except Exception:
            log.debug("compaction dispatch failed", exc_info=True)
        # Auto-speak replies when voice.auto_tts is enabled (Settings → Voice)
        try:
            from backend.config import config
            if config.get("voice", "auto_tts", default=True):
                reply = result.get("response", "")
                if reply and not reply.startswith(("[Not connected:", "[Provider")):
                    asyncio.create_task(_speak_reply(reply))
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

    async def action_approve(params: dict, ws) -> dict:
        """Approve a pending destructive action by its approval_id."""
        from backend.actions.executor import executor
        approval_id = params.get("approvalId", "")
        if not approval_id:
            return {"success": False, "error": "approvalId is required"}
        result = await executor.approve(approval_id)
        return {"success": result.success, "action_type": result.action_type,
                "summary": result.summary, "error": result.error, "data": result.data}

    async def action_deny(params: dict, ws) -> dict:
        """Deny a pending destructive action."""
        from backend.actions.executor import executor
        approval_id = params.get("approvalId", "")
        if not approval_id:
            return {"success": False, "error": "approvalId is required"}
        result = executor.deny(approval_id)
        return {"success": result.success, "summary": result.summary, "error": result.error}

    async def action_pending(params: dict, ws) -> dict:
        """List actions waiting for approval."""
        from backend.actions.executor import executor
        return {"pending": executor.pending_approvals()}

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
        from backend.memory.session_summary import delete_summary
        idx = params.get("index")
        if not isinstance(idx, int) or idx < 0:
            return {"success": False, "error": "index is required"}
        ok = delete_summary(idx)
        if ok:
            from backend.memory import autolink
            autolink.forget_ref("summary", idx)
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

    async def goal_start(params: dict, ws) -> dict:
        from backend.goals.store import goal_store
        from backend.goals.executor import goal_executor
        from backend.actions.executor import executor as action_exec
        goal_id = params.get("goalId", "")
        if not goal_id:
            return {"success": False, "error": "No goalId provided"}

        goal_executor.set_executor(action_exec)
        goal_executor.set_store(goal_store)

        # Fire and forget — run in background
        import asyncio
        asyncio.create_task(goal_executor.run_goal(goal_id))
        return {"success": True, "goalId": goal_id, "status": "started"}

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
    _SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.next',
                  'dist', 'build', 'out', 'venv', '.venv', 'target', 'vendor',
                  'site-packages', '.mypy_cache', '.pytest_cache', '.ruff_cache',
                  '.idea', '.vs', '.gradle'}

    async def code_bind(params: dict, ws) -> dict:
        folder = params.get("folderPath", "")
        if not folder:
            return {"workspaceId": None, "files": [], "error": "No folder path provided"}
        import os
        from backend.code.lang_detect import detect
        if not os.path.isdir(folder):
            return {"workspaceId": folder, "files": [], "error": f"Folder not found: {folder}"}
        limit = 400
        show_all = bool(params.get("includeIgnored"))
        files = []
        truncated = False
        try:
            for root, dirs, filenames in os.walk(folder):
                if show_all:
                    dirs[:] = [d for d in dirs if d != '.git']
                else:
                    dirs[:] = [d for d in dirs if not d.startswith('.')
                               and d not in _SKIP_DIRS]
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
                "truncated": truncated, "limit": limit}

    async def code_grep(params: dict, ws) -> dict:
        """Literal (case-insensitive) search across the bound workspace."""
        import os
        from backend.code import OutsideWorkspace, resolve_in_workspace
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
        from backend.code import OutsideWorkspace, resolve_in_workspace
        from backend.code.lang_detect import detect
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

    async def code_edit(params: dict, ws) -> dict:
        from backend.code.diff_engine import generate_diff
        wp = params.get("workspaceId", "")
        instruction = params.get("instruction", "")
        # Generate a diff from the instruction using LLM
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
            # Read the target file if specified
            fp = params.get("filePath", "")
            original = ""
            if fp and wp:
                from backend.code import (OutsideWorkspace,
                                          resolve_in_workspace)
                try:
                    full = resolve_in_workspace(wp, fp)
                except OutsideWorkspace as e:
                    return {"diffs": [], "status": "refused",
                            "message": str(e)}
                if full.is_file():
                    original = full.read_text(encoding='utf-8',
                                              errors='replace')
            prompt = f"Given this instruction: '{instruction}'\n\n"
            if original:
                prompt += f"And this original code:\n```\n{original[:3000]}\n```\n\n"
            prompt += "Return ONLY the complete modified code. No explanations."
            # Working guidelines apply to code generation too. The strict
            # output contract still comes last, and the reply is parsed
            # tolerantly below, so a chatty model cannot corrupt the diff.
            try:
                from backend.guidelines import inject as guidelines
                gblock = guidelines.system_block(code_task=True)
                if gblock:
                    prompt = f"{gblock}\n\n{prompt}"
            except Exception as e:
                log.debug("code.edit guideline injection failed: %s", e)
            # Editing code is the canonical reasoning-role task.
            try:
                from backend.providers import router
                _role, _model = router.pick(
                    getattr(provider, "provider_id", "unknown"),
                    instruction, force_role="reasoning")
            except Exception:
                _model = None
            result = await provider.chat(
                [{"role": "user", "content": prompt}],
                model=_model, max_tokens=4000, temperature=0.3)
            if result.ok and original:
                modified = _extract_code(result.response)
                if modified:
                    diff = generate_diff(original, modified, fp)
                    _pending_edits[f"{wp}::{fp}"] = modified
                    return {"diffs": [diff], "status": "pending",
                            "editId": f"{wp}::{fp}",
                            "message": "Edit ready for review. Approve with code.apply."}
        except Exception:
            pass
        return {"diffs": [{"instruction": instruction, "status": "pending",
                "message": "Edit ready for review. Apply to see changes."}]}

    async def code_apply(params: dict, ws) -> dict:
        """Apply a reviewed code edit. Requires explicit content OR a pending
        edit id created by code.edit."""
        from backend.code.diff_engine import apply_content
        from backend.code import OutsideWorkspace, resolve_in_workspace
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
        from backend.code import (MAX_EDIT_BYTES, OutsideWorkspace,
                                  resolve_in_workspace)
        from backend.code.diff_engine import apply_content
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

    # ---- Local model (llamafile) — ask-then-download flow -------------------

    async def localllm_status(params: dict, ws) -> dict:
        from backend.local_llm.manager import local_llm
        data = await asyncio.to_thread(local_llm.status)
        try:
            data["hf"] = await asyncio.to_thread(local_llm.hf_status)
        except Exception as exc:
            data["hf"] = {"deps_ready": False, "error": str(exc)}
        return data

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
                "deletable": s.category in ("forged", "market"),
                "source": s.category,
            })
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
            if url:
                meta = market.install_from_url(url)
            else:
                meta = market.install_from_github(
                    repo, str(params.get("path", "")).strip())
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
    _server.register("voice.speak", voice_speak)
    _server.register("voice.voices", voice_voices)
    _server.register("character.setState", character_set_state)
    _server.register("observer.status", observer_status)
    _server.register("system.status", system_status)
    _server.register("system.getProviders", system_get_providers)
    _server.register("system.rtkStatus", system_rtk_status)
    _server.register("desktop.status", desktop_status)
    _server.register("desktop.grant", desktop_grant)
    _server.register("desktop.revoke", desktop_revoke)
    _server.register("settings.get", settings_get)
    _server.register("settings.set", settings_set)
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
    _server.register("mcp.sweep", mcp_sweep)

    # Phase 5 Goals engine
    _server.register("goal.create", goal_create)
    _server.register("goal.list", goal_list)
    _server.register("goal.start", goal_start)
    _server.register("goal.cancel", goal_cancel)

    # Phase 5 Code engine
    _server.register("code.bind", code_bind)
    _server.register("code.read", code_read)
    _server.register("code.edit", code_edit)
    _server.register("code.apply", code_apply)
    _server.register("code.write", code_write)
    _server.register("code.grep", code_grep)

    # Phase 5 Swarm orchestrator
    _server.register("swarm.spawn", swarm_spawn)
    _server.register("swarm.list", swarm_list)
    _server.register("swarm.run", swarm_run)
    _server.register("swarm.stop", swarm_stop)

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
    _server.register("localLlm.status", localllm_status)
    _server.register("localLlm.installApprove", localllm_install)
    _server.register("localLlm.installDecline", localllm_decline)
    _server.register("localLlm.start", localllm_start)
    _server.register("localLlm.stop", localllm_stop)
    _server.register("localLlm.remove", localllm_remove)
    _server.register("localLlm.setOption", localllm_set_option)
    _server.register("localLlm.installHfDeps", localllm_hf_install)
    _server.register("hf.unload", hf_unload)

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
