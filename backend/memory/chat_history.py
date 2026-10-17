"""
Chat history — persistent conversation storage.

Stores conversations as JSON. Supports multi-turn context,
cross-session persistence, and auto-titling.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("addled.chat_history")

from backend import app_paths

HISTORY_PATH = app_paths.MEMORY_DIR / "chat_history.json"


class ChatHistory:
    """Manages conversation history as JSON."""

    def __init__(self):
        self._data: dict = {"conversations": {}, "current_conversation": None}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        from backend.memory._atomic import read_json
        default = {"conversations": {}, "current_conversation": None}
        data = read_json(HISTORY_PATH, default=None)
        self._data = data if isinstance(data, dict) else default
        self._loaded = True

    def _save(self):
        from backend.memory._atomic import write_json_safe
        write_json_safe(HISTORY_PATH, self._data)

    def create_conversation(self, title: str = "") -> str:
        """Create a new conversation, return its ID."""
        self._load()
        conv_id = f"conv_{time.strftime('%Y%m%d_%H%M%S')}"
        self._data["conversations"][conv_id] = {
            "id": conv_id,
            "created": time.time(),
            "updated": time.time(),
            "title": title or "New conversation",
            "messages": [],
        }
        self._data["current_conversation"] = conv_id
        self._save()
        return conv_id

    def add_message(self, role: str, content: str, conversation_id: str | None = None,
                    tokens: dict | None = None):
        """Add a message to a conversation."""
        self._load()
        conv_id = conversation_id or self._data.get("current_conversation")
        if not conv_id:
            conv_id = self.create_conversation()

        conv = self._data["conversations"].get(conv_id)
        if not conv:
            conv_id = self.create_conversation()
            conv = self._data["conversations"][conv_id]

        msg = {
            "role": role,
            "content": content,
            "timestamp": time.time(),
        }
        if tokens:
            msg["tokens"] = tokens

        conv["messages"].append(msg)
        conv["updated"] = time.time()

        # Auto-title from first user message
        if role == "user" and len(conv["messages"]) == 1:
            conv["title"] = content[:60]

        self._save()

    def get_context(self, conversation_id: str | None = None,
                    max_messages: int = 20) -> list[dict]:
        """Get recent messages for LLM context.

        Deliberately answers `[]` when there is no current conversation.

        `start_session` clears `current_conversation` on every launch so a new
        run does not silently continue a thread from weeks ago, and this
        function is what the MODEL is fed through - `check_chat_scope` asserts
        that a fresh session sees no history, because leaking the old thread
        back into the prompt is exactly the bug that clearing prevents.

        The dashboard needs the opposite answer ("show me the last thing we
        said"), and it asks through `chat.history`. That handler does the
        falling back; a fallback here would feed last week's conversation to a
        brand-new session to make a UI feel better.
        """
        self._load()
        conv_id = conversation_id or self._data.get("current_conversation")
        if not conv_id or conv_id not in self._data["conversations"]:
            return []

        messages = self._data["conversations"][conv_id]["messages"]
        return messages[-max_messages:]

    def latest_context(self, max_messages: int = 60) -> list[dict]:
        """Recent turns for a reader that wants to SEE the conversation.

        The counterpart to `get_context`, and separate from it on purpose.

        `get_context` answers the model and must be empty for a session that
        has said nothing yet; this answers the chat page, which wants the last
        thread it was in. Collapsing the two into one function means either the
        model gets fed a stale conversation or the page shows nothing, and the
        page showing nothing is the bug being fixed: it loads this at mount and
        REPLACES its whole message list with the result, so an empty answer
        deleted every reply that had arrived by push - a bot's or voice's
        reply, the only copy the page had, since the page did not send that
        turn. The user's own bubble survived (the page keeps its own list) and
        the answer did not, which is the "I heard it speak but there is no
        reply in the chat" report.

        Ordered by `updated`, the field `add_message` maintains, so this is the
        thread the user last spoke in rather than the newest file on disk. A
        conversation with no messages is skipped: an empty thread is not
        something to fall back to.
        """
        self._load()
        current = self._data.get("current_conversation")
        conv = self._data["conversations"].get(current) if current else None
        if conv is None or not conv.get("messages"):
            candidates = [c for c in self._data["conversations"].values()
                          if c.get("messages")]
            conv = max(candidates, key=lambda c: c.get("updated") or 0) \
                if candidates else None
        if conv is None:
            return []
        return conv["messages"][-max_messages:]

    def list_conversations(self) -> list[dict]:
        """List all conversations, newest first."""
        self._load()
        convs = list(self._data["conversations"].values())
        convs.sort(key=lambda c: c["updated"], reverse=True)
        return [
            {"id": c["id"], "title": c["title"], "created": c["created"],
             "updated": c["updated"], "message_count": len(c["messages"])}
            for c in convs
        ]

    @property
    def current_conversation_id(self) -> str | None:
        """Get the current conversation ID."""
        self._load()
        return self._data.get("current_conversation")

    def start_session(self) -> str | None:
        """Begin a new conversation for this run, returning the one that was open.

        `current_conversation` used to survive a restart, so a new launch quietly
        carried on whatever thread was left open — on this machine a conversation
        from 2026-08-16, whose last twenty messages went to the model as context
        for a bare "yoo apakabar ?" while the dashboard, reading only its own
        cache, showed a blank chat. Dropping the pointer makes the first message
        of the run open a fresh conversation; the older ones stay on disk for the
        conversations list, the Memory page and semantic recall.
        """
        self._load()
        previous = self._data.get("current_conversation")
        if previous is not None:
            self._data["current_conversation"] = None
            self._save()
        return previous

    def replace_context(self, messages: list[dict],
                        conversation_id: str | None = None) -> int:
        """Rewrite a conversation's message list. Returns the new length.

        The only operation here that can REMOVE turns. Everything else in this
        class appends, which is why compaction could summarize forever without
        the prompt getting smaller: the rolling summary was added while the raw
        turns it summarized stayed in the list, so a long conversation grew
        both ways at once.

        Callers pass the message list they want stored, and are responsible for
        what it contains. This method does not decide what is safe to drop — it
        only makes the rewrite possible. Refuses a non-list so a bad call cannot
        silently empty a conversation.

        The conversation is NOT re-created when it is missing: rewriting a
        conversation that does not exist is a caller bug, and creating one would
        hide it behind a plausible-looking empty result.
        """
        self._load()
        if not isinstance(messages, list):
            raise TypeError(
                f"replace_context needs a list of messages, got {type(messages).__name__}")
        conv_id = conversation_id or self._data.get("current_conversation")
        conv = self._data["conversations"].get(conv_id) if conv_id else None
        if not conv:
            log.warning("replace_context: no conversation %r to rewrite", conv_id)
            return 0

        # Copied, not aliased: the caller's list is often the one it read from
        # here, and storing that reference would let a later `append` on either
        # side mutate the other.
        conv["messages"] = [dict(m) for m in messages]
        conv["updated"] = time.time()
        self._save()
        return len(conv["messages"])

    def clear(self, conversation_id: str | None = None):
        """Clear a conversation or all conversations."""
        self._load()
        if conversation_id:
            self._data["conversations"].pop(conversation_id, None)
            if self._data["current_conversation"] == conversation_id:
                self._data["current_conversation"] = None
        else:
            self._data["conversations"] = {}
            self._data["current_conversation"] = None
        self._save()


# Singleton
chat_history = ChatHistory()
