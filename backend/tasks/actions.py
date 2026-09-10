"""Scheduled-task actions — what happens when a task fires.

Registered actions receive (task) and run fire-and-forget. The scheduler
(scheduler.py) holds the engine reference for character bubbles.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.tasks.actions")

ACTION_REGISTRY: dict[str, object] = {}

# Set from main.py so notify actions can pop a bubble on the character
# (mirrors ws_server._engine_ref pattern).
_engine_emit = None


def set_engine_emit(emit) -> None:
    global _engine_emit
    _engine_emit = emit


def register_action(name: str, fn) -> None:
    ACTION_REGISTRY[name] = fn


def _in_quiet_hours() -> bool:
    try:
        from backend.config import config
        start = config.get("safety", "quiet_hours_start", default="22:00")
        end = config.get("safety", "quiet_hours_end", default="07:00")
        now = __import__("datetime").datetime.now().strftime("%H:%M")
        if start <= end:
            return start <= now < end
        return now >= start or now < end  # overnight window
    except Exception:
        return False


def in_quiet_hours() -> bool:
    return _in_quiet_hours()


async def _run_notify(task) -> dict:
    """Reminder: WS broadcast + chat push + character bubble + spoken TTS."""
    text = (task.payload or task.title).strip() or task.title
    message = f"⏰ {text}"
    try:
        from backend.ws_server import get_server
        server = get_server()
        if server is not None:
            server.broadcast_nowait("tasks.notification", {
                "task_id": task.id,
                "title": task.title,
                "text": text,
                "time": task.time,
            })
            server.broadcast_nowait("chat.push", {
                "role": "assistant",
                "content": message,
                "insight": True,
            })
    except Exception as e:
        log.warning("notify broadcast failed: %s", e)
    if _engine_emit is not None:
        try:
            _engine_emit(text)
        except Exception as e:
            log.warning("notify bubble failed: %s", e)
    try:
        from backend.voice.tts import speak
        await speak(text)
    except Exception as e:
        log.warning("notify TTS failed: %s", e)
    return {"ok": True, "text": text}


async def _run_chat(task) -> dict:
    """Run a chat prompt later and push the reply to the dashboard."""
    prompt = (task.payload or task.title).strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    try:
        from backend.ws_server import run_chat_pipeline
        result = await run_chat_pipeline(prompt)
        reply = result.get("response", "")
        from backend.ws_server import get_server
        server = get_server()
        if server is not None and reply:
            server.broadcast_nowait("chat.push", {
                "role": "assistant",
                "content": f"⏰ {task.title}: {reply}",
                "insight": True,
            })
        return {"ok": True, "reply": reply[:200]}
    except Exception as e:
        log.warning("chat task failed: %s", e)
        return {"ok": False, "error": str(e)}


register_action("notify", _run_notify)
register_action("chat", _run_chat)
