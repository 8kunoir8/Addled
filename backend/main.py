"""
Addled — Proactive Desktop Agent
Entry point. Boots Qt, spawns character, engine, and WebSocket server.
"""

import ctypes
import os
import sys
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
    log_dir = _BACKEND_ROOT / "memory"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "addled.log", encoding="utf-8"),
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

    # Save mark first-run as complete (happens after wizard in main flow)
    if config.is_first_run:
        config.set("first_run_complete", value=False)

    log.info("Active provider: %s", config.active_provider)
    app.setApplicationDisplayName("Addled - " + config.agent_name)

    # ---- start WebSocket server (runs in background) --------------------------
    from backend.ws_server import start_ws_server
    import asyncio
    import threading

    _ws_loop = asyncio.new_event_loop()

    def _run_ws():
        asyncio.set_event_loop(_ws_loop)
        _ws_loop.run_until_complete(start_ws_server(host="127.0.0.1", port=9876))

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

    # ---- event loop ----------------------------------------------------------
    exit_code = app.exec()

    # ---- cleanup -------------------------------------------------------------
    log.info("Shutting down...")
    engine.stop()
    _ws_loop.call_soon_threadsafe(_ws_loop.stop)
    _ws_thread.join(timeout=3)
    config.save()
    log.info("=== Addled stopped ===")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
