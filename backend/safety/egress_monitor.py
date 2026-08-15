"""
Egress monitor — records and guards everything the agent sends out.

- record(): appends an entry to the in-memory ring buffer and disk log
- scrub(): redacts secrets from outbound text (reuses the clipboard filter)
- list_recent(): returns the last N entries for the dashboard
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time

log = logging.getLogger("addled.egress")

MAX_BUFFER = 200


class EgressMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._log_path = None

    def _log_file(self) -> pathlib.Path | None:
        if self._log_path is not None:
            return self._log_path
        try:
            from backend.config import config
            data_dir = config.data_dir if hasattr(config, "data_dir") else \
                pathlib.Path(__file__).resolve().parent.parent / "memory"
            path = pathlib.Path(data_dir) / "egress.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_path = path
            return path
        except Exception:
            return None

    def record(self, category: str, detail: dict | None = None) -> None:
        """Record an outbound event (chat send, provider call, file upload…)."""
        entry = {
            "ts": time.time(),
            "category": category,
            "detail": detail or {},
        }
        with self._lock:
            self._entries.append(entry)
            if len(self._entries) > MAX_BUFFER:
                self._entries = self._entries[-MAX_BUFFER:]
        path = self._log_file()
        if path:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                pass

    def list_recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(reversed(self._entries[-limit:]))

    def scrub(self, text: str) -> tuple[str, int]:
        """Redact secrets from an outbound payload. Returns (scrubbed, hits)."""
        from backend.safety.clipboard_filter import redact
        scrubbed, hits = redact(text)
        if hits:
            self.record("egress.scrub", {"hits": hits})
            log.warning("Egress guard redacted %d secret(s) from outbound payload", hits)
        return scrubbed, hits


egress = EgressMonitor()
