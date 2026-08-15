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

    def register(self, method: str, handler: HandlerFunc):
        """Register a JSON-RPC method handler."""
        self._handlers[method] = handler

    async def start(self, host: str = "127.0.0.1", port: int = 9876):
        """Start the WebSocket server."""
        self._loop = asyncio.get_running_loop()
        self._server = await websockets.serve(
            self._handle_connection, host, port,
            max_size=10 * 1024 * 1024,  # 10MB max message
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

    async def _handle_connection(self, ws: WebSocketServerProtocol):
        """Handle a single WebSocket connection."""
        self._connections.add(ws)
        peer = ws.remote_address
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
            result = await handler(params, ws)
            if is_notification:
                return None
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
        from backend.providers.registry import list_available
        return {"providers": list_available()}

    async def settings_get(params: dict, ws) -> dict:
        from backend.config import config
        section = params.get("section")
        if section:
            return {"settings": {section: config.get(section, default={})}}
        # Ensure config is loaded before accessing private _data
        config._ensure_loaded()
        return {"settings": dict(config._data)}

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
        return {"success": True}

    # ---- Phase 3: Chat with prompt guard + provider integration ---------------

    async def chat_send(params: dict, ws) -> dict:
        message = params.get("message", "")
        if not message:
            return {"response": "I didn't catch that.", "tokens": 0, "conversationId": None}

        # Prompt guard check
        from backend.config import config
        if config.get("safety", "prompt_guard", default=True):
            from backend.safety.prompt_guard import sanitize
            sanitized, blocked = sanitize(message)
            if blocked:
                return {"response": sanitized, "tokens": 0, "conversationId": None}

        try:
            from backend.providers.registry import get_provider
            from backend.memory.chat_history import chat_history
            from backend.skills.tool_loop import chat_with_tools

            provider = get_provider()
            sys_prompt = config.get("chat", "system_prompt",
                default="You are Addled, a helpful AI desktop companion with access to system tools.")
            context = chat_history.get_context(max_messages=config.get("chat", "context_messages", default=20))

            # Build user messages (without system prompt — tool_loop handles it)
            user_messages = [
                {"role": m["role"], "content": m["content"]} for m in context
            ] + [{"role": "user", "content": message}]

            # Use the provider-agnostic tool-use loop
            result = await chat_with_tools(
                provider=provider,
                messages=user_messages,
                system_prompt=sys_prompt,
                max_tool_rounds=params.get("maxToolRounds", 5),
            )

            response_text = result.get("response", "")
            tool_rounds = result.get("tool_rounds", 0)
            tool_results = result.get("tool_results", [])

            if response_text and not response_text.startswith("[Provider:") and not response_text.startswith("[Not connected:"):
                chat_history.add_message("user", message)
                chat_history.add_message("assistant", response_text,
                    tokens={"in": result.get("tokens", 0), "out": 0})
                return {
                    "response": response_text,
                    "tokens": result.get("tokens", 0),
                    "conversationId": chat_history.current_conversation_id,
                    "toolRounds": tool_rounds,
                    "toolResults": len(tool_results),
                }
            return {"response": response_text or "I couldn't process that request.",
                    "tokens": result.get("tokens", 0), "conversationId": None}
        except Exception as e:
            log.exception("Chat failed")
            return {"response": f"[Not connected: {e}] Configure an AI provider in Settings.", "tokens": 0, "conversationId": None}

    # ---- Phase 3: Action execution --------------------------------------------

    async def action_execute(params: dict, ws) -> dict:
        from backend.actions.executor import executor, ActionRequest
        action_type = params.get("type", "")
        action_params = params.get("params", {})
        if not action_type:
            return {"success": False, "error": "No action type specified"}
        result = await executor.execute(ActionRequest(action_type=action_type, params=action_params))
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

    # ---- Phase 3: Voice TTS --------------------------------------------------

    async def voice_speak(params: dict, ws) -> dict:
        from backend.voice.tts import speak
        text = params.get("text", "")
        voice = params.get("voice", "en-US-JennyNeural")
        if not text:
            return {"success": False, "error": "No text to speak"}
        result = await speak(text, voice)
        return result

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

    async def code_bind(params: dict, ws) -> dict:
        folder = params.get("folderPath", "")
        if not folder:
            return {"workspaceId": None, "files": [], "error": "No folder path provided"}
        import os
        from backend.code.lang_detect import detect
        if not os.path.isdir(folder):
            return {"workspaceId": folder, "files": [], "error": f"Folder not found: {folder}"}
        files = []
        try:
            for root, dirs, filenames in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules','__pycache__','venv','.git','.next')]
                for f in filenames[:200]:
                    fp = os.path.join(root, f)
                    files.append({"name": f, "path": os.path.relpath(fp, folder),
                                  "language": detect(fp), "size": os.path.getsize(fp)})
                if len(files) >= 200: break
        except Exception as e:
            return {"workspaceId": folder, "files": [], "error": str(e)}
        return {"workspaceId": folder, "files": files}

    async def code_read(params: dict, ws) -> dict:
        from backend.code.lang_detect import detect
        wp = params.get("workspaceId", "")
        fp = params.get("filePath", "")
        import os
        full = os.path.join(wp, fp) if wp else fp
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
                import os
                full = os.path.join(wp, fp)
                if os.path.isfile(full):
                    with open(full, 'r', encoding='utf-8', errors='replace') as f:
                        original = f.read()
            prompt = f"Given this instruction: '{instruction}'\n\n"
            if original:
                prompt += f"And this original code:\n```\n{original[:3000]}\n```\n\n"
            prompt += "Return ONLY the complete modified code. No explanations."
            result = await provider.chat([{"role": "user", "content": prompt}], max_tokens=4000, temperature=0.3)
            if result.ok and original:
                modified = result.response.strip()
                if modified.startswith("```"):
                    lines = modified.split("\n")
                    modified = "\n".join(lines[1:-1]) if len(lines) > 2 else modified
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
        wp = params.get("workspaceId", "")
        fp = params.get("filePath", "")
        edit_id = params.get("editId") or f"{wp}::{fp}"

        content = params.get("content")
        if content is None:
            content = _pending_edits.get(edit_id)
        if content is None:
            return {"success": False,
                    "error": "No pending edit or content provided. Run code.edit first or pass 'content'."}

        import os
        full = os.path.join(wp, fp) if wp else fp
        if not os.path.isfile(full):
            return {"success": False, "error": f"File not found: {full}"}
        result = apply_content(full, content, backup=params.get("backup", True))
        if result.get("success"):
            _pending_edits.pop(edit_id, None)
        return result

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

    # ---- Register all handlers -----------------------------------------------

    # Phase 1-3 core handlers
    _server.register("chat.send", chat_send)
    _server.register("action.execute", action_execute)
    _server.register("action.approve", action_approve)
    _server.register("action.deny", action_deny)
    _server.register("action.pending", action_pending)
    _server.register("voice.speak", voice_speak)
    _server.register("character.setState", character_set_state)
    _server.register("observer.status", observer_status)
    _server.register("system.status", system_status)
    _server.register("system.getProviders", system_get_providers)
    _server.register("settings.get", settings_get)
    _server.register("settings.set", settings_set)

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

    # Phase 5 Swarm orchestrator
    _server.register("swarm.spawn", swarm_spawn)
    _server.register("swarm.list", swarm_list)
    _server.register("swarm.run", swarm_run)
    _server.register("swarm.stop", swarm_stop)

    # Phase 5 Calendar & Email
    _server.register("calendar.add", calendar_add)
    _server.register("calendar.list", calendar_list)
    _server.register("calendar.delete", calendar_delete)
    _server.register("email.fetch", email_fetch)
    _server.register("email.send", email_send)
    _server.register("email.search", email_search)

    # Browser (Playwright)
    _server.register("browser.navigate", browser_navigate)
    _server.register("browser.go_back", browser_go_back)
    _server.register("browser.go_forward", browser_go_forward)
    _server.register("browser.click", browser_click)
    _server.register("browser.type", browser_type)
    _server.register("browser.screenshot", browser_screenshot)
    _server.register("browser.extract", browser_extract)
    _server.register("browser.close", browser_close)

    # Skill Forge
    _server.register("forge.create", forge_create)
    _server.register("forge.list", forge_list)

    # Excel operations
    _server.register("excel.read", excel_read)
    _server.register("excel.write", excel_write)
