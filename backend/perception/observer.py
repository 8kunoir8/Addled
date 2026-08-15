"""
Observer — 3-tier screen observation system.

Light (free):   perceptual hash diff, runs every 5s
Medium (cheap): OCR + window title classification
Deep (costly):  vision model full analysis
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("addled.observer")


@dataclass
class ObservationResult:
    tier: str
    screen_hash: str = ""
    changed: bool = False
    context: str = "unknown"
    detail: str | None = None
    tokens: int = 0
    decision: str = "stay_quiet"


class Observer:
    """Three-tier observation engine."""

    def __init__(self, hasher=None, decision=None, provider=None,
                 medium_cycles: int = 3, deep_interval_s: int = 300):
        self._hasher = hasher
        self._decision = decision
        self._provider = provider
        self._light_counter: int = 0
        self._last_context: str = "unknown"
        self._medium_cycles = medium_cycles
        self._deep_interval_s = deep_interval_s
        self._last_deep_time: float = 0.0
        self._last_hash: str = ""

    async def tick(self) -> ObservationResult | None:
        """Called every ~5s. Returns None if nothing to report."""
        self._light_counter += 1

        # ---- LIGHT TIER (always, free) ---------------------------------------
        try:
            import hashlib
            from backend.actions.system_controls import SystemControls
            sc = SystemControls()
            result = await sc.screenshot()
            if result.get("success") and result.get("image_b64"):
                raw = result["image_b64"][:1000]  # Hash first part for speed
                new_hash = hashlib.md5(raw.encode()).hexdigest()
                changed = new_hash != self._last_hash
                self._last_hash = new_hash
            else:
                changed = True  # Can't hash, assume changed
        except Exception:
            changed = True
            new_hash = "error"

        if not changed:
            return ObservationResult(tier="light", screen_hash=new_hash, changed=False, context="unchanged")

        # ---- MEDIUM TIER (every Nth cycle) -----------------------------------
        if self._light_counter % self._medium_cycles == 0:
            context = await self._classify_context()
            self._last_context = context
            return ObservationResult(tier="medium", screen_hash=new_hash, changed=True, context=context)

        # ---- DEEP TIER (every deep_interval_s) -------------------------------
        now = time.time()
        if now - self._last_deep_time >= self._deep_interval_s:
            self._last_deep_time = now
            detail = await self._deep_analyze()
            return ObservationResult(tier="deep", screen_hash=new_hash, changed=True,
                                     context=self._last_context, detail=detail, tokens=500)

        return ObservationResult(tier="light", screen_hash=new_hash, changed=True, context=self._last_context)

    async def _classify_context(self) -> str:
        """Classify what the user is doing from window titles."""
        try:
            from backend.actions.window_manager import WindowManager
            wm = WindowManager()
            result = await wm.list_windows()
            if result.get("success") and result.get("windows"):
                titles = " | ".join(w["title"] for w in result["windows"][:5])
                title_lower = result["windows"][0]["title"].lower() if result["windows"] else ""

                # Simple heuristic classification
                if any(kw in title_lower for kw in ["visual studio", "vscode", "pycharm", "intellij", "cursor"]):
                    return "coding"
                if any(kw in title_lower for kw in ["chrome", "firefox", "edge", "brave", "browser"]):
                    return "browsing"
                if any(kw in title_lower for kw in ["word", "docs", "notion", "obsidian", "notepad"]):
                    return "writing"
                if any(kw in title_lower for kw in ["terminal", "powershell", "cmd", "bash", "wsl"]):
                    return "terminal"
                if any(kw in title_lower for kw in ["zoom", "teams", "meet", "discord", "slack"]):
                    return "meeting"
                if any(kw in title_lower for kw in ["steam", "game", "league", "valorant", "minecraft"]):
                    return "gaming"
                if any(kw in title_lower for kw in ["figma", "photoshop", "illustrator", "blender"]):
                    return "design"
                if any(kw in title_lower for kw in ["explorer", "finder", "files"]):
                    return "file_management"
                return "other"
        except Exception as e:
            log.debug("Context classification failed: %s", e)
        return "unknown"

    async def _deep_analyze(self) -> str | None:
        """Run vision model analysis. Returns detail string or None."""
        if not self._provider:
            return None
        try:
            from backend.actions.system_controls import SystemControls
            sc = SystemControls()
            result = await sc.screenshot()
            if result.get("success") and result.get("image_b64"):
                prompt = ("Describe what's on this screen in detail. Focus on: "
                          "what application, what the user is working on, "
                          "any errors visible. Be concise.")
                vision_result = await self._provider.vision(
                    result["image_b64"], prompt)

                # ── Global fallback: local DeepSeek-VL2-tiny via HF ────────
                if not vision_result.ok:
                    log.debug("Provider vision failed (%s) — trying HF fallback",
                              vision_result.error)
                    from backend.providers.hf_vision import hf_vision
                    vision_result = await hf_vision.analyze(
                        result["image_b64"], prompt)

                if vision_result.ok:
                    return vision_result.response
        except Exception as e:
            log.debug("Deep analysis failed: %s", e)
        return None
