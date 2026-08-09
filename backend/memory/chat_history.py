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

HISTORY_PATH = Path(__file__).parent / "chat_history.json"


class ChatHistory:
    """Manages conversation history as JSON."""

    def __init__(self):
        self._data: dict = {"conversations": {}, "current_conversation": None}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if HISTORY_PATH.exists():
            try:
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {"conversations": {}, "current_conversation": None}
        self._loaded = True

    def _save(self):
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

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
        """Get recent messages for LLM context."""
        self._load()
        conv_id = conversation_id or self._data.get("current_conversation")
        if not conv_id or conv_id not in self._data["conversations"]:
            return []

        messages = self._data["conversations"][conv_id]["messages"]
        return messages[-max_messages:]

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
