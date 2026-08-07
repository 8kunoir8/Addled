"""
Core engine — asyncio-driven event loop.

Runs the observation tick, handles state transitions, and coordinates
all subsystems (observer, safety, character, actions).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from enum import Enum, auto

from PyQt6.QtCore import QObject, pyqtSignal

from backend.config import config

log = logging.getLogger("addled.engine")


class EngineState(Enum):
    STARTING = auto()
    RUNNING = auto()
    SLEEPING = auto()   # Meeting, gaming, or user away
    STOPPED = auto()
    ERROR = auto()


class Engine(QObject):
    """Central coordinator. Runs the main loop in a background asyncio thread."""

    sig_agent_state = pyqtSignal(str)  # "idle", "observing", "in_meeting", etc.
    sig_error = pyqtSignal(str)

    def __init__(self, char_widget=None):
        super().__init__()
        self._state = EngineState.STARTING
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._shutdown_requested = False
        self._char_widget = char_widget

        # Subsystems (lazy-init)
        self._observer = None
        self._presence_guard = None
        self._decision = None
        self._goal_executor = None

        # Tick intervals
        self._light_interval = config.get("observation", "light_interval_s") or 5
        self._tick_count = 0

    # ---- lifecycle -----------------------------------------------------------

    def start(self):
        """Start the engine in a background thread."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="engine")
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main_loop())

    async def _main_loop(self):
        log.info("Engine main loop started")
        try:
            await self._init_subsystems()
            self._set_state(EngineState.RUNNING)
            self.sig_agent_state.emit("idle")

            while not self._shutdown_requested:
                await self._tick()
                await asyncio.sleep(self._light_interval)
        except Exception as e:
            log.exception("Engine loop crashed: %s", e)
            self._set_state(EngineState.ERROR)
            self.sig_error.emit(str(e))

    async def _init_subsystems(self):
        """Lazy-init all subsystems in order."""
        from backend.safety.presence_guard import PresenceGuard
        from backend.cognition.decision import DecisionEngine

        self._presence_guard = PresenceGuard()
        self._decision = DecisionEngine()

        log.info("Engine subsystems initialized")

    async def _tick(self):
        """One engine tick (~5s)."""
        self._tick_count += 1

        # 1. Check presence guard — meeting / gaming / away?
        if self._presence_guard:
            blocked, reason = self._presence_guard.check()
            if blocked:
                if self._state != EngineState.SLEEPING:
                    self._set_state(EngineState.SLEEPING)
                    self.sig_agent_state.emit("in_meeting")
                return
            elif self._state == EngineState.SLEEPING:
                self._set_state(EngineState.RUNNING)
                self.sig_agent_state.emit("idle")

        # 2. Light observation (screen hash)
        # TODO: Full observer integration in Phase 3

        # 3. Idle state if no activity
        if self._state == EngineState.RUNNING:
            self.sig_agent_state.emit("idle")

        # 4. Process pending goal steps
        # TODO: Goal executor integration in Phase 5

    def stop(self):
        """Request graceful shutdown."""
        self._shutdown_requested = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._set_state(EngineState.STOPPED)

    def _set_state(self, state: EngineState):
        old = self._state
        self._state = state
        if old != state:
            log.info("Engine state: %s → %s", old.name, state.name)

    # ---- public API ----------------------------------------------------------

    @property
    def state(self) -> EngineState:
        return self._state

    def set_sleeping(self, active: bool):
        """Manually sleep/wake the engine."""
        if active:
            self._set_state(EngineState.SLEEPING)
            self.sig_agent_state.emit("sleeping")
        else:
            self._set_state(EngineState.RUNNING)
            self.sig_agent_state.emit("idle")
