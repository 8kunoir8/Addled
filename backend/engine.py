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
    sig_insight = pyqtSignal(str)  # proactive insight text → floating bubble

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
        self._last_obs = None  # most recent ObservationResult
        self._last_detail = None  # latest successful deep vision description
        self._last_context = None  # latest meaningful activity context
        self._last_insight_text = ""  # dedup: don't repeat the same insight
        self._last_insight_time = 0.0
        self._last_maintenance = 0.0  # last memory-maintenance dispatch
        self._chat_busy = False  # a chat is in flight — don't force 'idle'
        self._voice_busy = False  # TTS is speaking — don't force 'idle'

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

    def _pick_vision_provider(self):
        """
        Choose a provider for the deep vision tier.

        Priority: active provider if vision-capable → first provider in the
        configured priority list that supports vision → None (vision off).
        """
        from backend.providers.registry import get_provider

        builtin = config.get("providers", "builtin", default={}) or {}
        custom = config.get("providers", "custom", default=[]) or []
        active = config.active_provider

        def is_vision(pid: str) -> bool:
            if pid in builtin:
                return bool(builtin[pid].get("vision", False))
            for c in custom:
                if c.get("id") == pid:
                    return bool(c.get("vision", True))
            return False

        candidates = []
        if active:
            candidates.append(active)
        candidates.extend(p for p in config.get("providers", "priority", default=[]) if p != active)

        for pid in candidates:
            if not is_vision(pid):
                continue
            try:
                provider = get_provider(pid)
                if provider is not None:
                    log.info("Vision provider selected: %s", pid)
                    return provider
            except Exception as e:
                log.warning("Vision provider %s unavailable: %s", pid, e)

        log.info("No vision-capable provider available — deep vision tier disabled")
        return None

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
        self._observer = Observer(
            decision=self._decision,
            provider=self._pick_vision_provider(),
        )

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
                if obs:
                    self._last_obs = obs
                    if obs.detail:
                        self._last_detail = obs.detail
                        log.info("Observer deep analysis: %s", obs.detail[:90])
                    if obs.context not in (None, "unknown", "unchanged", "private"):
                        self._last_context = obs.context
                if obs and obs.context != "unknown" and obs.changed:
                    log.debug("Observer: context=%s tier=%s decision=%s",
                              obs.context, obs.tier, obs.decision)
                    if obs.context == "private":
                        # Privacy guard active (excluded app / zone)
                        self.sig_agent_state.emit("privacy_guard")
                    elif obs.tier == "light":
                        self.sig_agent_state.emit("observing")
                    elif obs.decision in ("nudge", "suggest", "offer_action"):
                        self.sig_agent_state.emit("has_suggestion")
                        self._push_insight(obs)
                    elif obs.decision:
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

        # 4. Idle state if no activity detected (unless a chat or TTS is
        #    in flight — keep THINKING/SPEAKING animations visible)
        if (self._state == EngineState.RUNNING
                and not self._chat_busy and not self._voice_busy):
            self.sig_agent_state.emit("idle")

        # 5. Memory maintenance (re-embed, compaction, facts, triples,
        #    dedup) — fire-and-forget; each job throttles itself internally
        try:
            if time.time() - self._last_maintenance >= 60.0:
                self._last_maintenance = time.time()
                from backend.memory.maintenance import run_maintenance
                asyncio.ensure_future(run_maintenance())
        except Exception as e:
            log.debug("maintenance dispatch failed: %s", e)

    def _push_insight(self, obs):
        """Deliver a proactive insight to dashboards/bots, and optionally
        speak it when voice_insights is enabled in settings."""
        if obs.tier == "deep" and obs.detail:
            text = obs.detail.strip()
        else:
            text = (f"You've been in '{obs.context}' for a while — "
                    "need a hand with anything?")
        now = time.time()
        # Dedup: never repeat the identical insight, and don't nag with
        # context-level suggestions more often than every 10 minutes.
        if text == self._last_insight_text:
            return
        if obs.tier != "deep" and now - self._last_insight_time < 600:
            return
        self._last_insight_text = text
        self._last_insight_time = now
        try:
            from backend.ws_server import get_server
            get_server().broadcast_nowait("observer.insight", {
                "decision": obs.decision,
                "context": obs.context,
                "tier": obs.tier,
                "text": text,
                "timestamp": now,
            })
        except Exception as e:
            log.warning("Insight broadcast failed: %s", e)
        # Also push the insight as a chat message (dashboard chat page)
        # and as a bubble on the floating character.
        try:
            from backend.ws_server import get_server
            get_server().broadcast_nowait("chat.push", {
                "role": "assistant",
                "content": f"💡 {text}",
                "insight": True,
            })
        except Exception as e:
            log.warning("Insight chat.push failed: %s", e)
        try:
            self.sig_insight.emit(text)
        except Exception as e:
            log.warning("Insight bubble emit failed: %s", e)
        # Briefly flip the character to 'has suggestion' (resets next tick)
        try:
            self.sig_agent_state.emit("has_suggestion")
        except Exception:
            pass
        if config.get("observation", "voice_insights", default=False):
            try:
                from backend.voice.tts import speak
                asyncio.ensure_future(speak(text))
            except Exception as e:
                log.warning("Insight TTS failed: %s", e)

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

    def last_screen_info(self) -> dict | None:
        """Latest observation for chat screen-awareness (keeps the most
        recent deep vision detail even after later light ticks)."""
        obs = self._last_obs
        return {
            "context": self._last_context
                or ((obs.context if obs else None)
                    if (obs and obs.context not in ("unknown", "unchanged", "private"))
                    else "unknown"),
            "detail": self._last_detail or (obs.detail if obs else None),
            "tier": obs.tier if obs else "none",
            "changed": obs.changed if obs else False,
        }

    async def fresh_screen_detail(self) -> str | None:
        """Run a deep vision analysis now (used when the user asks what the
        bot can see). Returns the description or None."""
        if not self._observer:
            return None
        try:
            return await self._observer._deep_analyze()
        except Exception as e:
            log.warning("Fresh screen analysis failed: %s", e)
            return None

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
