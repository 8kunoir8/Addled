"""
Addled — Proactive Desktop Agent
Entry point. Boots Qt, spawns character, engine, and WebSocket server.
"""

import ctypes
import os
import sys
import time
from pathlib import Path

# ---- stdlib shadow guard ----------------------------------------------------
# `backend/` is on sys.path (it is the script directory, and main.py inserts it
# below), so any sub-directory whose name matches a standard-library module wins
# the import for that name. A package called `backend/code/` used to shadow the
# stdlib `code` module here, and sympy — which arrives with torch/transformers
# on the local-model path — does `from code import InteractiveConsole` while
# importing, so the first local-model tool call failed with "module 'code' has
# no attribute 'InteractiveConsole'". The package is now `backend/codemode/`,
# which removes that collision. Importing the stdlib modules a name collision
# would most likely hit, before any sub-directory can claim them, keeps a
# future package from reintroducing the same failure silently.
import code as _stdlib_code          # noqa: F401  (backend/codemode no longer collides)
import types as _stdlib_types        # noqa: F401
import platform as _stdlib_platform  # noqa: F401

# ---- single-instance lock ---------------------------------------------------

try:
    import win32event
    import win32api
    import winerror

    _MUTEX_NAME = "Global\\Addled_SingleInstance"
    _mutex_handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        ctypes.windll.user32.MessageBoxW(
            0, "Addled is already running.\nCheck your system tray.", "Addled", 0x40
        )
        sys.exit(0)
except ImportError:
    pass  # Non-Windows: skip single-instance lock

# ---- path setup -------------------------------------------------------------

_BACKEND_ROOT = Path(__file__).parent
_PROJECT_ROOT = _BACKEND_ROOT.parent  # addled/
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

os.environ["ADDLED_DATA_DIR"] = str(_BACKEND_ROOT / "memory")

if getattr(sys, "frozen", False):
    _EXE_DIR = Path(sys.executable).parent
    if str(_EXE_DIR) not in sys.path:
        sys.path.insert(0, str(_EXE_DIR))


# ---- logging ----------------------------------------------------------------

def _setup_logging():
    import logging
    from logging.handlers import RotatingFileHandler
    log_dir = _BACKEND_ROOT / "memory"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            # Rotating handler — keeps the log from growing unbounded
            # (e.g. repeated paint errors once filled a 167 MB file).
            RotatingFileHandler(log_dir / "addled.log", encoding="utf-8",
                                maxBytes=5 * 1024 * 1024, backupCount=2),
            logging.StreamHandler(sys.stderr),
        ],
    )
    # Keep httpx quiet
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---- main -------------------------------------------------------------------

def main():
    _setup_logging()
    import logging
    log = logging.getLogger("addled")

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt

    from backend.config import config

    log.info("=== Addled starting (python %s) ===", sys.version.split()[0])
    log.info("Root: %s", _BACKEND_ROOT)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Addled")

    log.info("Active provider: %s", config.active_provider)
    app.setApplicationDisplayName("Addled - " + config.agent_name)

    # ---- first-run onboarding wizard -----------------------------------------
    from backend.onboarding.wizard import is_first_run, show_onboarding
    if is_first_run():
        log.info("First run detected — launching onboarding wizard")
        completed = show_onboarding()
        if not completed:
            log.info("Onboarding cancelled by user")
            sys.exit(0)
        log.info("Onboarding complete")
        # Reload config after wizard saves it
        config._dirty = False
    else:
        log.info("Already onboarded — skipping wizard")

    # ---- session boundary ----------------------------------------------------
    # A run starts a fresh conversation. The pointer a previous run left behind
    # used to continue an old thread invisibly: the dashboard showed a blank chat
    # while the model was handed the last twenty messages of a month-old one.
    from backend.memory.chat_history import chat_history
    _previous = chat_history.start_session()
    if _previous:
        log.info("Fresh session — previous conversation was %s", _previous)

    # ---- start WebSocket server (runs in background) --------------------------
    from backend.ws_server import start_ws_server, get_server
    import asyncio
    import threading

    _ws_loop = asyncio.new_event_loop()

    def _run_ws():
        asyncio.set_event_loop(_ws_loop)
        _ws_loop.run_until_complete(start_ws_server(host="127.0.0.1", port=9876))
        # CRITICAL: keep the loop running — without this the thread exits and
        # the listening socket never accepts connections (handshakes hang).
        _ws_loop.run_forever()

    _ws_thread = threading.Thread(target=_run_ws, daemon=True, name="ws-server")
    _ws_thread.start()
    log.info("WebSocket server starting on ws://127.0.0.1:9876")

    # ---- tiny HTTP API for the Electron shell (window show / navigation) -----
    from backend.http_api import start_http_api
    start_http_api(port=9877)

    # ---- OpenAI-compatible endpoint in front of the local model --------------
    # Off unless the user turned it on. Started here rather than lazily so the
    # Address a tool was configured with is live from launch; if it cannot
    # bind, that is logged and the app carries on.
    try:
        from backend.model_api import model_api
        _ok, _detail = model_api.start()
        if _ok:
            log.info("Model API ready at %s", _detail)
        else:
            log.info("Model API not started: %s", _detail)
    except Exception as e:  # noqa: BLE001
        log.warning("Model API unavailable: %s", e)

    # ---- character widget ----------------------------------------------------
    from backend.character.states import CharacterState, StateMachine, AGENT_TO_CHARACTER
    from backend.character.avatar import CharacterWidget

    char_settings = config.get("character", default={})
    char_widget = CharacterWidget(char_settings)
    char_widget.show()
    log.info("Character widget shown")

    # ---- engine (starts in Qt-managed thread) --------------------------------
    from backend.engine import Engine

    engine = Engine(char_widget=char_widget)
    engine.sig_agent_state.connect(char_widget.set_agent_state)

    # Mood → character visual tint (warmth, brightness) + movement energy
    try:
        def _apply_mood(w, b):
            char_widget._animator.set_mood(w, b)
            try:
                char_widget._mover.set_energy(0.4 + b * 1.2)
            except Exception:
                pass

        engine.sig_mood.connect(_apply_mood)
    except Exception:
        pass

    # Mirror every engine state change to dashboard clients
    def _broadcast_agent_state(state: str):
        try:
            get_server().broadcast_nowait("state.changed", {"state": state})
        except Exception:
            pass

    engine.sig_agent_state.connect(_broadcast_agent_state)
    engine.start()
    log.info("Engine started")

    # Wire engine to WS server so handlers can query real state
    from backend.ws_server import set_engine
    set_engine(engine)

    # ---- local model (llamafile) — ask-then-download + lazy boot -------------
    try:
        from backend.local_llm.manager import local_llm

        def _background(coro, name: str):
            def _done(fut):
                try:
                    err = fut.exception()
                except Exception:
                    return
                if err:
                    log.warning("%s failed: %s", name, err)

            handle = asyncio.run_coroutine_threadsafe(coro, _ws_loop)
            handle.add_done_callback(_done)
            return handle

        _background(local_llm.boot(), "local model boot")
        _background(local_llm.watchdog_loop(), "local model watchdog")
        log.info("Local model manager armed (port %s)", local_llm.port())
    except Exception as e:
        log.warning("Local model manager unavailable: %s", e)

    # ---- remote access gateway + Tailscale -----------------------------------
    try:
        from backend.remote.gateway import gateway
        from backend.tailscale.manager import tailscale

        def _remote_background(coro, name: str):
            def _done(fut):
                try:
                    err = fut.exception()
                except Exception:
                    return
                if err:
                    log.warning("%s failed: %s", name, err)

            handle = asyncio.run_coroutine_threadsafe(coro, _ws_loop)
            handle.add_done_callback(_done)
            return handle

        # Neither opens anything on its own: the gateway refuses to start
        # without a password, and Tailscale is only reconciled with what the
        # user already chose. A fresh install therefore stays closed.
        _remote_background(gateway.boot(), "remote gateway boot")
        _remote_background(tailscale.boot(), "tailscale boot")
        _remote_background(tailscale.watchdog_loop(), "tailscale watchdog")
        log.info("Remote access armed (gateway %s)",
                 "on" if gateway.enabled() else "off")
    except Exception as e:
        log.warning("Remote access manager unavailable: %s", e)

    # ---- MCP servers (third-party tool servers) ------------------------------
    try:
        from backend.mcp_client.manager import mcp_manager

        def _mcp_started(fut):
            try:
                err = fut.exception()
            except Exception:
                return
            if err:
                log.debug("MCP startup failed: %s", err)

        # Fire and forget: a slow or missing server must never delay startup.
        handle = asyncio.run_coroutine_threadsafe(
            mcp_manager.start(), _ws_loop)
        handle.add_done_callback(_mcp_started)

        # Periodic switch-off for servers the agent added and then stopped
        # using. Only servers marked 'auto' are ever disconnected, so nothing
        # the user configured by hand is touched.
        async def _mcp_idle_loop():
            from backend.config import config as _config
            while True:
                try:
                    minutes = float(_config.get(
                        "mcp", "auto_deactivate_minutes", default=30) or 30)
                    await asyncio.sleep(max(60.0, minutes * 30))
                    await mcp_manager.sweep_idle()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.debug("MCP idle sweep failed: %s", e)

        idle = asyncio.run_coroutine_threadsafe(_mcp_idle_loop(), _ws_loop)
        idle.add_done_callback(_mcp_started)
        log.info("MCP manager armed")
    except Exception as e:
        log.warning("MCP client unavailable: %s", e)

    # ---- scheduler (tasks, calendar reminders, housekeeping) ------------------
    try:
        from backend.tasks.scheduler import scheduler
        scheduler.set_engine_emit(engine.sig_insight.emit)

        def _calendar_reminders():
            from backend.integrations.calendar_integration import calendar
            for ev in calendar.due_reminders():
                try:
                    import asyncio
                    from backend.tasks.actions import ACTION_REGISTRY
                    from backend.tasks.store import ScheduledTask
                    notify = ACTION_REGISTRY["notify"]
                    task = ScheduledTask(
                        id=f"cal_{ev.get('id', '')}",
                        title=ev.get("title", "Reminder"),
                        kind="reminder",
                        payload=f"{ev.get('title', 'Reminder')} at "
                                f"{ev.get('start', '')}",
                        time=ev.get("start", "09:00")[-5:] or "09:00",
                        source="calendar",
                    )
                    asyncio.create_task(notify(task))
                    calendar.mark_reminded(ev.get("id", ""))
                except Exception as e:
                    log.warning("calendar reminder failed: %s", e)

        scheduler.register_housekeeping("calendar_reminders",
                                        _calendar_reminders, interval_s=30)
        from backend.memory.maintenance import run_maintenance
        scheduler.register_housekeeping("memory_maintenance",
                                        run_maintenance, interval_s=60)

        def _initiative_checkin():
            """Daily check-in: one templated question per day, in-window."""
            from backend.config import config
            if not config.get("initiative", "enabled", default=True):
                return
            if not config.get("initiative", "checkin_enabled", default=True):
                return
            import asyncio
            import json as _json
            import time as _t
            try:
                target = config.get("initiative", "checkin_time",
                                    default="09:00")
                th, tm = map(int, target.split(":"))
                target_min = th * 60 + tm
            except (ValueError, AttributeError):
                return
            now = _t.localtime()
            cur = now.tm_hour * 60 + now.tm_min
            if not (target_min - 30 <= cur <= target_min + 30):
                return
            from backend.tasks.actions import in_quiet_hours
            if in_quiet_hours():
                return
            state_path = (
                Path(__file__).resolve().parent / "memory" / "integrations"
                / "initiative_state.json")
            today = _t.strftime("%Y-%m-%d")
            try:
                state = _json.loads(state_path.read_text(
                    encoding="utf-8")) if state_path.exists() else {}
                if state.get("last_checkin") == today:
                    return
            except (_json.JSONDecodeError, OSError):
                state = {}
            text = "How's your day going?"
            try:
                engine.sig_insight.emit(text)
                from backend.voice.tts import speak
                asyncio.create_task(speak(text))
            except Exception:
                pass
            state["last_checkin"] = today
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(_json.dumps(state), encoding="utf-8")
            except OSError:
                pass

        scheduler.register_housekeeping("initiative_checkin",
                                        _initiative_checkin, interval_s=60)

        def _journal_nightly():
            """Summarize finished days + refresh the learned user profile."""
            import asyncio
            try:
                from backend.memory.journal import nightly_summarize
                asyncio.create_task(nightly_summarize())
            except Exception:
                pass
            try:
                from backend.memory.user_profile import auto_learn
                auto_learn()
            except Exception:
                pass

        scheduler.register_housekeeping("journal_nightly",
                                        _journal_nightly, interval_s=3600)

        def _project_index():
            from backend.project.indexer import index_roots
            index_roots()

        scheduler.register_housekeeping("project_index",
                                        _project_index, interval_s=600)

        def _patterns_suggest():
            from backend.project.patterns import maybe_suggest_patterns
            maybe_suggest_patterns(engine.sig_insight.emit)

        scheduler.register_housekeeping("patterns_suggest",
                                        _patterns_suggest, interval_s=1800)

        def _weekly_reflection():
            from backend.reflection.weekly_review import weekly_review
            weekly_review(engine.sig_insight.emit)

        scheduler.register_housekeeping("weekly_reflection",
                                        _weekly_reflection, interval_s=21600)

        async def _weekly_refresh():
            """Refresh slow-moving remote data held in the local cache.

            Currently the model catalog (which models each provider offers) and
            the guideline packs (ponytail, Karpathy). The job also fires at
            startup, so the staleness checks are what keep that first pass a
            no-op.
            """
            try:
                from backend.providers import model_catalog
                await model_catalog.refresh_if_stale()
            except Exception as e:
                log.debug("model catalog refresh failed: %s", e)
            try:
                from backend.guidelines import store as guidelines_store
                await guidelines_store.refresh_if_stale()
            except Exception as e:
                log.debug("guideline pack refresh failed: %s", e)

        scheduler.register_housekeeping("weekly_refresh",
                                        _weekly_refresh, interval_s=604800)
        log.info("Scheduler wired (calendar reminders + memory maintenance "
                 "+ daily check-in + journal + project index + patterns "
                 "+ reflection + weekly refresh)")
    except Exception as e:
        log.warning("Scheduler wiring failed: %s", e)

    # ---- restore active sprite skin (codex-pet style) ------------------------
    try:
        from backend.character import sprite_skin
        sprite_skin.install_default_skins()  # starter skins on first run
        active_skin = sprite_skin.get_active_skin_id()
        if active_skin:
            char_widget.apply_skin(active_skin)
    except Exception:
        log.exception("Skin restore failed (ignored)")

    # ---- standard operating procedures (starter set on first run) -------------
    try:
        from backend.sop import seeds as sop_seeds
        sop_seeds.ensure_seeded()
    except Exception as e:
        log.debug("Procedure seeding skipped: %s", e)

    # ---- kill switch (global hotkey: stops everything immediately) -----------
    from backend.safety.kill_switch import kill_switch

    def _on_kill():
        log.warning("KILL SWITCH: halting engine and all actions")
        engine.stop()
        from backend.actions.executor import executor
        executor.cancel_current()
        from backend.ws_server import get_server
        get_server().broadcast_nowait("kill.activated", {"ts": time.time()})
        engine.sig_agent_state.emit("sleeping")

    hotkey = config.get("safety", "kill_switch_hotkey", default="ctrl+shift+alt+k")
    kill_switch.on_activated(_on_kill)
    kill_switch.start(hotkey)

    # ---- voice input (wake word → command → chat → speak) --------------------
    from backend.voice.stt import voice_listener
    from backend.ws_server import run_chat_pipeline, get_server

    async def _handle_voice_command(text: str, lang: str | None = None):
        get_server().broadcast_nowait("voice.command",
                                      {"text": text, "lang": lang})
        try:
            from backend.character.mood import mood_engine
            mood_engine.event("chat_user")
        except Exception:
            pass
        result = await run_chat_pipeline(text)
        reply = result.get("response", "")
        get_server().broadcast_nowait("voice.reply", {"text": reply})

        # Heuristic scheduling fallback: provider down (e.g. 402) but the
        # command reads like "remind me to X at 3pm" → schedule it anyway.
        scheduled_reply = ""
        if reply.startswith("[Not connected:") or reply.startswith("[Provider"):
            try:
                from backend.tasks.parse import parse_natural_task
                from backend.tasks.recurrence import next_run
                from backend.tasks.store import ScheduledTask, task_store
                parsed = parse_natural_task(text)
                if parsed:
                    task = ScheduledTask(
                        id="", title=parsed["title"],
                        action="notify", payload=parsed["payload"],
                        time=parsed["time"], date=parsed["date"],
                        recurrence=parsed["recurrence"], source="voice",
                    )
                    task.next_run = next_run(task)
                    added, err = task_store.add(task)
                    if added is not None:
                        scheduled_reply = (
                            f"Scheduled: remind you to {added.payload} "
                            f"at {added.time}"
                            + ("" if added.recurrence["type"] == "none"
                               else " (recurring)"))
            except Exception as e:
                log.warning("voice task parse failed: %s", e)

        spoken = scheduled_reply or reply
        if spoken and not spoken.startswith("[Not connected:") \
                and not spoken.startswith("[Provider"):
            try:
                engine.sig_agent_state.emit("speaking")
                engine._voice_busy = True
                from backend.voice.tts import pick_voice, speak
                voice, tts_engine = pick_voice(lang)
                await speak(spoken, voice=voice, engine=tts_engine)
            except Exception as e:
                log.warning("Voice reply TTS failed: %s", e)
            finally:
                engine._voice_busy = False
                engine.sig_agent_state.emit("idle")

    def _on_voice_command(text: str, lang: str | None = None):
        try:
            asyncio.run_coroutine_threadsafe(
                _handle_voice_command(text, lang), _ws_loop)
        except Exception as e:
            log.warning("Voice dispatch failed: %s", e)

    wake_word = config.get("voice", "wake_word", default="hey addled")
    voice_listener.on_command(_on_voice_command)
    voice_listener.on_wake(lambda: engine.sig_agent_state.emit("listening"))
    if config.get("voice", "mic_enabled", default=True):
        voice_listener.start()
        log.info("Voice input: %s (wake word '%s')",
                 "enabled" if voice_listener.enabled else "disabled", wake_word)

    # ---- floating-character prompt (left click → ask → answer) ----------------
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QInputDialog
    from backend.character.chat_bubble import ChatBubble

    class _UIBridge(QObject):
        reply = pyqtSignal(str)
        # A decision, emitted from the WS loop and rendered on the Qt thread.
        # `object` rather than a typed payload because Qt needs the type at
        # class-definition time and this is a plain dict of label/callback.
        decision = pyqtSignal(object)

    bridge = _UIBridge()

    # Chat-bubble popup anchored above the character (replaces QMessageBox)
    bubble = ChatBubble(agent_name=config.agent_name)

    # Proactive insights also pop as bubbles on the floating character
    engine.sig_insight.connect(
        lambda text: bubble.show_message(text, char_widget.frameGeometry().center()))

    def _show_reply(text: str):
        bubble.show_message(text, char_widget.frameGeometry().center())

    bridge.reply.connect(_show_reply)  # thread-safe: emit from WS loop → Qt queue

    def _show_decision(payload: dict):
        """Put a prompt on the bubble, with buttons that answer it.

        A permission request or a question used to reach the character as plain
        text, and the user had to go and find the dashboard to act on it. The
        anchor is re-read here rather than captured, so the bubble lands on the
        character wherever it has been dragged to since.

        `record` rides alongside the display fields: it is what the character
        prompt needs to ANSWER the question rather than only show it. Without
        it, clicking the character and typing sent a brand-new request and the
        question expired unanswered.
        """
        try:
            bubble.show_decision(
                str(payload.get("title") or "Addled"),
                str(payload.get("text") or ""),
                payload.get("buttons") or [],
                char_widget.frameGeometry().center(),
                closable=bool(payload.get("closable", True)),
                payload=payload.get("record") or {},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not show a decision on the bubble: %s", e)

    bridge.decision.connect(_show_decision)

    # What the floating character should do when a decision is raised.
    #
    # Two broadcast methods carry a decision, and they are handled here rather
    # than each call site reaching for the bubble: the character is not a
    # WebSocket client, so this notifier is its only way to hear one, and a
    # prompt that appears on the chat page but not on the character is the gap
    # this closes.
    def _on_ui_notification(method: str, params: dict) -> None:
        try:
            if method == "action.approvalRequest":
                approval_id = str(params.get("approval_id") or "")
                if not approval_id:
                    return
                name = str(params.get("name") or params.get("action_type")
                           or "an action")
                kind = str(params.get("kind") or "action")
                command = str(params.get("command") or "")

                def answer(method: str):
                    """Answer on the character's behalf, on the WS loop."""
                    def _go():
                        async def _send():
                            r = await get_server().call(method,
                                                        {"approvalId": approval_id})
                            if not r.get("success"):
                                log.warning("bubble approval failed: %s",
                                            r.get("error"))
                        asyncio.run_coroutine_threadsafe(_send(), _ws_loop)
                    return _go

                text = f'The {kind} "{name}" needs your permission before it runs.'
                if command:
                    text += f"\n\n{command[:200]}"
                buttons = [
                    {"label": "Allow once", "callback": answer("action.approve")},
                    {"label": "Deny", "callback": answer("action.deny"),
                     "danger": True},
                ]
                bridge.decision.emit({
                    "title": "🔐 Permission needed",
                    "text": text,
                    "buttons": buttons,
                    # A gated action can be answered from the dashboard later,
                    # so leaving it is legitimate — the request stays queued.
                    "closable": True,
                })
            elif method == "question.ask":
                question_id = str(params.get("question_id") or "")
                options = params.get("options") or []
                if not question_id:
                    return
                question_text = str(params.get("question") or "")
                text = question_text
                if params.get("context"):
                    text += f"\n\n{params['context']}"

                # The record the answer needs, carried with the card so both
                # ways of answering — a button here and typing at the character
                # — send the same thing.
                record = {
                    "question_id": question_id,
                    "question": question_text,
                    "source": str(params.get("source") or ""),
                    "conversation": str(params.get("conversation") or ""),
                    "options": list(options),
                }
                buttons = []
                # Only the first three choices get a button: four plus the way
                # out is the most that stays readable above a small character.
                for option in options[:3]:
                    buttons.append({
                        "label": str(option)[:32],
                        "callback": (lambda choice=option: asyncio
                                     .run_coroutine_threadsafe(
                                         _answer_question(record, str(choice)),
                                         _ws_loop)),
                    })
                bridge.decision.emit({
                    "title": "❓ Addled needs to know",
                    "text": text,
                    "buttons": buttons,
                    "record": record,
                    # An open-ended question has no buttons, but it is still
                    # answerable from here: click the character and type. The
                    # prompt names the question so it is clear what is being
                    # answered.
                    "closable": True,
                })
        except Exception as e:  # noqa: BLE001
            log.debug("could not raise a bubble decision: %s", e)

    async def _answer_question(record: dict, answer: str) -> dict:
        """Settle a question and resume the work it was blocking.

        One helper for both ways of answering on this surface — a bubble button
        and typing at the character. Sending only `question.answer` settles the
        question and tells the model nothing: it asked, the user answered, and
        the turn it was blocking never continues. Keeping both calls here is
        what stops the two paths drifting into that.
        """
        settled = await get_server().call("question.answer", {
            "question_id": record.get("question_id"),
            "answer": answer,
            "source": record.get("source") or "character",
            "conversation": record.get("conversation") or "",
        })
        if not settled.get("success"):
            log.warning("character question not answered: %s",
                        settled.get("error"))
            return settled
        asked = (f'"{record.get("question")}"' if record.get("question")
                 else "a question")
        await get_server().call("chat.send", {
            "message":
                f"Answering your question {asked}: {answer}\n\n"
                "This is my answer to the question you asked. Continue the task "
                "you were working on when you asked it, using this.",
            "source": record.get("source") or "character",
            "conversation": record.get("conversation") or None,
        })
        return settled

    try:
        get_server().set_ui_notifier(_on_ui_notification)
    except Exception as e:  # noqa: BLE001
        log.warning("could not attach the bubble notifier: %s", e)

    async def _handle_prompt(text: str):
        get_server().broadcast_nowait("chat.push", {"role": "user", "content": text})
        result = await run_chat_pipeline(text)
        reply = result.get("response", "")
        get_server().broadcast_nowait("chat.push", {"role": "assistant", "content": reply})
        bridge.reply.emit(reply)
        if reply and not reply.startswith("[Not connected:") and not reply.startswith("[Provider"):
            # Spoken replies follow the same Auto TTS toggle as the dashboard
            if config.get("voice", "auto_tts", default=True):
                try:
                    engine.sig_agent_state.emit("speaking")
                    engine._voice_busy = True
                    from backend.voice.tts import speak
                    await speak(reply)
                except Exception as e:
                    log.warning("Prompt TTS failed: %s", e)
                finally:
                    engine._voice_busy = False
                    engine.sig_agent_state.emit("idle")

    def _on_prompt():
        """Left-click on the character: type something at Addled.

        **When a question is open, this is the answer to it.** Clicking the
        character and typing is the obvious thing to try while the bubble is
        asking something, and it used to send a brand-new request instead: the
        text went to a fresh turn, the question stayed open, and it expired
        while the user believed they had answered it — a message that goes
        nowhere with nothing saying so.

        The open question is read from the bubble rather than from the queue,
        because the bubble is what the user is looking at: if it is showing a
        question, that is the one being answered.
        """
        held = bubble.open_question()
        if held:
            title = "Answer Addled"
            # The question itself is the prompt, so what is being answered is
            # visible while typing rather than remembered from a card that has
            # since been covered.
            prompt = (held.get("question") or "Your answer?").strip()
            if len(prompt) > 300:
                prompt = prompt[:300] + "…"
        else:
            title = "Ask Addled"
            prompt = "What would you like to know?"

        text, ok = QInputDialog.getText(char_widget, title, prompt)
        if not (ok and text.strip()):
            return
        answer = text.strip()

        bubble.show_thinking(char_widget.frameGeometry().center())
        try:
            if held:
                asyncio.run_coroutine_threadsafe(
                    _answer_question(held, answer), _ws_loop)
            else:
                asyncio.run_coroutine_threadsafe(
                    _handle_prompt(answer), _ws_loop)
        except Exception as e:
            log.warning("Prompt dispatch failed: %s", e)

    char_widget.set_interaction_callbacks(on_ask=_on_prompt)

    # ---- event loop ----------------------------------------------------------
    exit_code = app.exec()

    # ---- cleanup -------------------------------------------------------------
    log.info("Shutting down...")
    voice_listener.stop()
    kill_switch.stop()
    engine.stop()

    # Stop the local model server so it does not outlive the app
    try:
        from backend.local_llm.manager import local_llm
        fut = asyncio.run_coroutine_threadsafe(local_llm.stop(), _ws_loop)
        fut.result(timeout=15)
    except Exception as e:
        log.debug("Local model shutdown: %s", e)

    # Close MCP servers so no child process outlives the app either
    try:
        from backend.mcp_client.manager import mcp_manager
        fut = asyncio.run_coroutine_threadsafe(mcp_manager.stop(), _ws_loop)
        fut.result(timeout=15)
    except Exception as e:
        log.debug("MCP shutdown: %s", e)

    # Stop the remote gateway. This also drops every remote session, so closing
    # Addled logs remote devices out rather than leaving a live cookie behind.
    try:
        from backend.remote.gateway import gateway
        fut = asyncio.run_coroutine_threadsafe(gateway.stop(), _ws_loop)
        fut.result(timeout=10)
    except Exception as e:
        log.debug("Remote gateway shutdown: %s", e)

    # Cancel any sign-in still waiting on a browser
    try:
        from backend.tailscale.manager import tailscale
        fut = asyncio.run_coroutine_threadsafe(tailscale.stop(), _ws_loop)
        fut.result(timeout=10)
    except Exception as e:
        log.debug("Tailscale shutdown: %s", e)

    # Stop a half-finished Tailscale install from outliving the app
    try:
        from backend.tailscale.installer import installer
        fut = asyncio.run_coroutine_threadsafe(installer.stop(), _ws_loop)
        fut.result(timeout=10)
    except Exception as e:
        log.debug("Tailscale installer shutdown: %s", e)

    # Summarize the session into long-term memory before the loops stop
    try:
        from backend.memory.session_summary import summarize_session
        fut = asyncio.run_coroutine_threadsafe(summarize_session(), _ws_loop)
        fut.result(timeout=25)
        log.info("Session summary saved")
    except Exception as e:
        log.warning("Session summary failed: %s", e)

    _ws_loop.call_soon_threadsafe(_ws_loop.stop)
    _ws_thread.join(timeout=3)
    config.save()
    log.info("=== Addled stopped ===")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
