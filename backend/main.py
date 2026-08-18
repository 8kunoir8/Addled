"""
Addled — Proactive Desktop Agent
Entry point. Boots Qt, spawns character, engine, and WebSocket server.
"""

import ctypes
import os
import sys
import time
from pathlib import Path

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

    # ---- start WebSocket server (runs in background) --------------------------
    from backend.ws_server import start_ws_server
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
    engine.start()
    log.info("Engine started")

    # Wire engine to WS server so handlers can query real state
    from backend.ws_server import set_engine
    set_engine(engine)

    # ---- restore active sprite skin (codex-pet style) ------------------------
    try:
        from backend.character import sprite_skin
        active_skin = sprite_skin.get_active_skin_id()
        if active_skin:
            char_widget.apply_skin(active_skin)
    except Exception:
        log.exception("Skin restore failed (ignored)")

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

    async def _handle_voice_command(text: str):
        get_server().broadcast_nowait("voice.command", {"text": text})
        result = await run_chat_pipeline(text)
        reply = result.get("response", "")
        get_server().broadcast_nowait("voice.reply", {"text": reply})
        if reply and not reply.startswith("[Not connected:"):
            try:
                from backend.voice.tts import speak
                await speak(reply)
            except Exception as e:
                log.warning("Voice reply TTS failed: %s", e)

    def _on_voice_command(text: str):
        try:
            asyncio.run_coroutine_threadsafe(_handle_voice_command(text), _ws_loop)
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

    bridge = _UIBridge()

    # Chat-bubble popup anchored above the character (replaces QMessageBox)
    bubble = ChatBubble(agent_name=config.agent_name)

    # Proactive insights also pop as bubbles on the floating character
    engine.sig_insight.connect(
        lambda text: bubble.show_message(text, char_widget.frameGeometry().center()))

    def _show_reply(text: str):
        bubble.show_message(text, char_widget.frameGeometry().center())

    bridge.reply.connect(_show_reply)  # thread-safe: emit from WS loop → Qt queue

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
                    from backend.voice.tts import speak
                    await speak(reply)
                except Exception as e:
                    log.warning("Prompt TTS failed: %s", e)

    def _on_prompt():
        text, ok = QInputDialog.getText(char_widget, "Ask Addled",
                                        "What would you like to know?")
        if ok and text.strip():
            # Show a "thinking" bubble right away, anchored to the character
            bubble.show_thinking(char_widget.frameGeometry().center())
            try:
                asyncio.run_coroutine_threadsafe(_handle_prompt(text.strip()), _ws_loop)
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
