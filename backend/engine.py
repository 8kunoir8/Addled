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
        from backend.perception.observer import Observer
        from backend.goals.executor import goal_executor
        from backend.goals.store import goal_store
        from backend.actions.executor import executor as action_exec

        self._presence_guard = PresenceGuard()
        self._decision = DecisionEngine()
        self._observer = Observer(decision=self._decision)

        # Wire goal executor with action executor + store
        goal_executor.set_executor(action_exec)
        goal_executor.set_store(goal_store)
        self._goal_executor = goal_executor

        log.info("Engine subsystems initialized (observer, presence, decision, goals)")

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

        # 2. Light observation — screen hash + context classification
        if self._observer:
            try:
                obs = await self._observer.tick()
                if obs and obs.context != "unknown" and obs.changed:
                    log.debug("Observer: context=%s tier=%s", obs.context, obs.tier)
                    # Emit observing state if screen changed
                    if obs.tier == "light":
                        self.sig_agent_state.emit("observing")
                    elif obs.tier == "deep" and obs.decision:
                        self.sig_agent_state.emit(obs.decision)
            except Exception as e:
                log.warning("Observer tick failed: %s", e)

        # 3. Process pending goal steps — one step per tick
        if self._goal_executor:
            try:
                from backend.goals.store import goal_store
                in_progress = goal_store.list_all(status="in_progress")
                if in_progress:
                    goal = in_progress[0]
                    # Fire-and-forget step processing (don't block tick)
                    if goal["id"] not in self._goal_executor._running:
                        self.sig_agent_state.emit("goal_executing")
                        asyncio.ensure_future(self._goal_executor.run_goal(goal["id"]))
            except Exception as e:
                log.warning("Goal tick failed: %s", e)

        # 4. Idle state if no activity detected
        if self._state == EngineState.RUNNING:
            self.sig_agent_state.emit("idle")

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
