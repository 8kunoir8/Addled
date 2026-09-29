"""Scheduled-task actions — what happens when a task fires.

Registered actions receive (task) and run fire-and-forget. The scheduler
(scheduler.py) holds the engine reference for character bubbles.
"""

from __future__ import annotations

import logging

from backend import chat_sources

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

def _notify_platforms() -> list[str]:
    """Which bot platforms a reminder should reach. Empty means any.

    Read every time rather than cached: the setting is changed from the
    dashboard while the daemon is running, and a cached list would keep sending
    reminders to a bot the user had just turned off.
    """
    try:
        from backend.config import config
        values = config.get("bots", "notify_platforms", default=[]) or []
        if isinstance(values, str):
            values = [v.strip() for v in values.split(",")]
        return [str(v).strip().lower() for v in values if str(v).strip()]
    except Exception as e:  # noqa: BLE001
        log.debug("could not read notify_platforms: %s", e)
        return []


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
            # Remote companion: the bot bridges deliver this to a phone.
            #
            # `platforms` scopes it. The broadcast reaches every connected
            # client, and each bot subscribes on its own behalf — so without a
            # destination list a reminder would be sent once per running bot and
            # the user would get the same message two or three times. Empty
            # means "whatever is running", which is the behaviour a single-bot
            # setup wants and the only sane default.
            server.broadcast_nowait("bot.notify", {
                "text": message,
                "platforms": _notify_platforms(),
            })
            # Recorded so "did my reminder go out" is answerable. The actual
            # delivery is reported by each bridge and lands here only if it
            # resolves a destination — see `record_notify_delivery`.
            try:
                from backend.memory import bot_history
                bot_history.record_outbound(
                    "reminder", f"task:{task.id}", message, True)
            except Exception as e:  # noqa: BLE001
                log.debug("could not record the reminder: %s", e)
            server.broadcast_nowait("chat.push", {
                "role": "assistant",
                "content": message,
                "insight": True,
                # A reminder was not typed on the chat page, so it says where
                # it came from. Without this the bubble looks like the user
                # said it, or like Addled spoke unprompted for no reason.
                **chat_sources.describe("task"),
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
                **chat_sources.describe("task"),
            })
        return {"ok": True, "reply": reply[:200]}
    except Exception as e:
        log.warning("chat task failed: %s", e)
        return {"ok": False, "error": str(e)}


register_action("notify", _run_notify)
register_action("chat", _run_chat)
