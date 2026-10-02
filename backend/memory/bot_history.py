"""What the chat bridges exchanged, kept so it can be read back.

Why a local record and not the platform's own history: WhatsApp does not serve
message history to a linked device unless the bridge configures an explicit
message store at connect time, and this one does not. So "what did that contact
say" cannot be answered by asking WhatsApp — but it can be answered from what
the bridge already passed through Addled, which is the part that matters for
"did my message send" and "what were we talking about".

Deliberately NOT the main chat history. That store is the dashboard's
conversation, keyed by conversation id, and it feeds context, compaction and
recall. A bot chat mingling into it would put a stranger's message into the
user's chat page and into semantic recall. This is a separate, small, bounded
log.

Nothing here raises: it is written from the chat turn that has already replied.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("addled.bot_history")

# Per conversation. A bridge is a chat with a person, so the useful window is
# the recent exchange, not an archive; unbounded growth here would be a leak.
MAX_PER_CONVERSATION = 200
# Conversations kept in total, oldest dropped first.
MAX_CONVERSATIONS = 200
MAX_TEXT_CHARS = 2000

_LOCK = threading.Lock()

def _dir() -> Path:
    try:
        from backend.config import config
        custom = str(config.get("bots", "history_dir", default="") or "").strip()
        if custom:
            return Path(os.path.expandvars(os.path.expanduser(custom)))
    except Exception as e:  # noqa: BLE001
        log.debug("could not read bots.history_dir: %s", e)
    from backend import app_paths
    return app_paths.subdir("bot_history")

def _file() -> Path:
    return _dir() / "history.json"

def _load() -> dict:
    file = _file()
    if not file.exists():
        return {"conversations": []}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read %s (%s) — starting from empty", file, e)
        return {"conversations": []}
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), list):
        log.warning("%s is not in the expected shape — ignoring it", file)
        return {"conversations": []}
    return {"conversations": [c for c in data["conversations"]
                              if isinstance(c, dict)]}

def _save(data: dict) -> None:
    file = _file()
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"conversations": list(data.get("conversations") or [])}
        tmp = file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, file)
    except OSError as e:
        log.warning("could not write bot history: %s", e)

def _clean(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS - 1] + "…"

def record_turn(source, conversation, user_text: str, reply_text: str) -> None:
    """Note one exchange. Never raises.

    Only a real bridge is recorded. The dashboard is already its own history,
    and copying it here would double every turn and make "what did the phone
    say" indistinguishable from "what did I type in the app".
    """
    try:
        from backend import chat_sources
        name = chat_sources.normalise(source)
    except Exception:  # noqa: BLE001
        name = str(source or "").strip().lower()
    if not name or name == "dashboard":
        return
    conv = str(conversation or "").strip()
    if not conv:
        return

    try:
        with _LOCK:
            data = _load()
            now = time.time()
            entry = next((c for c in data["conversations"]
                          if c.get("platform") == name
                          and c.get("conversation") == conv), None)
            if entry is None:
                entry = {"platform": name, "conversation": conv,
                         "created": now, "updated": now, "turns": []}
                data["conversations"].append(entry)

            if str(user_text or "").strip():
                entry["turns"].append({"role": "user",
                                       "text": _clean(user_text),
                                       "at": now})
            if str(reply_text or "").strip():
                entry["turns"].append({"role": "assistant",
                                       "text": _clean(reply_text),
                                       "at": now})
            del entry["turns"][:-MAX_PER_CONVERSATION]
            entry["updated"] = now

            # Newest first, then trim, so the cap drops the coldest chat.
            data["conversations"].sort(key=lambda c: c.get("updated") or 0,
                                       reverse=True)
            del data["conversations"][MAX_CONVERSATIONS:]
            _save(data)
    except Exception as e:  # noqa: BLE001
        log.debug("could not record a bot turn: %s", e)

def record_outbound(platform, to, text, ok: bool, error: str = "") -> None:
    """Note a message a bridge SENT that nobody had asked for.

    A reply is already recorded by `record_turn`. This covers the other two
    cases — a proactive send from the chat, and a scheduled reminder — which
    would otherwise be invisible in the history, making "did my reminder go out"
    unanswerable.
    """
    try:
        conv = str(to or "").strip()
        if not conv:
            return
        name = str(platform or "").strip().lower() or "unknown"
        with _LOCK:
            data = _load()
            now = time.time()
            entry = next((c for c in data["conversations"]
                          if c.get("platform") == name
                          and c.get("conversation") == conv), None)
            if entry is None:
                entry = {"platform": name, "conversation": conv,
                         "created": now, "updated": now, "turns": []}
                data["conversations"].append(entry)
            entry["turns"].append({
                "role": "assistant",
                "text": _clean(text),
                "at": now,
                "outbound": True,
                "delivered": bool(ok),
                **({"error": _clean(error)} if error else {}),
            })
            del entry["turns"][:-MAX_PER_CONVERSATION]
            entry["updated"] = now
            data["conversations"].sort(key=lambda c: c.get("updated") or 0,
                                       reverse=True)
            del data["conversations"][MAX_CONVERSATIONS:]
            _save(data)
    except Exception as e:  # noqa: BLE001
        log.debug("could not record an outbound message: %s", e)

def recent(platform: str | None = None, conversation: str | None = None,
           limit: int = 20) -> list[dict]:
    """Recent turns, newest last. Never raises.

    `conversation` matches loosely — a phone number may be stored as a WhatsApp
    JID (`628...@s.whatsapp.net`) and asked for as bare digits, which is how a
    person would give it.
    """
    try:
        with _LOCK:
            data = _load()
    except Exception as e:  # noqa: BLE001
        log.debug("could not read bot history: %s", e)
        return []

    convs = data.get("conversations") or []
    if platform:
        wanted = str(platform).strip().lower()
        convs = [c for c in convs if c.get("platform") == wanted]
    if conversation:
        needle = str(conversation).strip()
        digits = "".join(ch for ch in needle if ch.isdigit())

        def matches(entry: dict) -> bool:
            stored = str(entry.get("conversation") or "")
            if stored == needle:
                return True
            if digits and digits in "".join(ch for ch in stored if ch.isdigit()):
                return True
            return False
        convs = [c for c in convs if matches(c)]

    turns: list[dict] = []
    for entry in convs:
        for turn in (entry.get("turns") or []):
            turns.append({
                "platform": entry.get("platform"),
                "conversation": entry.get("conversation"),
                "role": turn.get("role"),
                "text": turn.get("text"),
                "at": turn.get("at"),
                **({"outbound": True} if turn.get("outbound") else {}),
                **({"delivered": turn.get("delivered")}
                   if "delivered" in turn else {}),
                **({"error": turn.get("error")} if turn.get("error") else {}),
            })
    turns.sort(key=lambda t: t.get("at") or 0)
    return turns[-max(1, int(limit)):]

def conversations() -> list[dict]:
    """Every chat a bridge has spoken in, newest first. Never raises."""
    try:
        with _LOCK:
            data = _load()
    except Exception as e:  # noqa: BLE001
        log.debug("could not read bot history: %s", e)
        return []
    out = []
    for entry in data.get("conversations") or []:
        if not isinstance(entry, dict):
            continue
        out.append({
            "platform": entry.get("platform"),
            "conversation": entry.get("conversation"),
            "turns": len(entry.get("turns") or []),
            "updated": entry.get("updated"),
        })
    return out
