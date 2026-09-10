"""
Temporal fact triples — a lightweight local knowledge graph (Graphiti-lite).

Extracts (subject, relation, object) triples from conversations and answers
relational questions ("what did we decide...", "what do I prefer...") via
SQL lookups. LLM extraction is opt-in (memory.graph_extract) and never runs
without a working provider. SQLite only — no Neo4j.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("addled.knowledge_graph")

DB_PATH = Path(__file__).parent / "triples.db"


class KnowledgeGraph:
    def __init__(self):
        self._conn = None
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS triples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    object TEXT NOT NULL,
                    ts REAL NOT NULL,
                    source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
                CREATE INDEX IF NOT EXISTS idx_triples_ts ON triples(ts);
            """)
            self._conn.commit()
        except Exception as e:
            log.warning("Knowledge graph unavailable: %s", e)
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def add_triples(self, triples: list[dict], source: str = "agent") -> int:
        """Add [{subject, relation, object}] rows. Returns count added."""
        if not self.available:
            return 0
        added = 0
        try:
            for t in triples:
                sub = str(t.get("subject", "")).strip()
                rel = str(t.get("relation", "")).strip()
                obj = str(t.get("object", "")).strip()
                if not (sub and rel and obj):
                    continue
                # dedupe: skip exact (subject, relation, object) repeats
                exists = self._conn.execute(
                    "SELECT 1 FROM triples WHERE subject = ? AND relation = ? "
                    "AND object = ? LIMIT 1", (sub, rel, obj)).fetchone()
                if exists:
                    continue
                self._conn.execute(
                    "INSERT INTO triples (subject, relation, object, ts, source) "
                    "VALUES (?,?,?,?,?)",
                    (sub, rel, obj, time.time(), source))
                added += 1
            self._conn.commit()
        except Exception as e:
            log.debug("add_triples failed: %s", e)
        return added

    def lookup(self, subject: str | None = None, relation: str | None = None,
               since: float | None = None, until: float | None = None,
               limit: int = 20) -> list[dict]:
        """Time-bounded graph lookup. Returns [{subject, relation, object, ts}]."""
        if not self.available:
            return []
        conds, args = [], []
        if subject:
            conds.append("subject LIKE ?")
            args.append(f"%{subject.lower()}%")
        if relation:
            conds.append("relation = ?")
            args.append(relation)
        if since is not None:
            conds.append("ts >= ?")
            args.append(float(since))
        if until is not None:
            conds.append("ts <= ?")
            args.append(float(until))
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        try:
            rows = self._conn.execute(
                "SELECT subject, relation, object, ts FROM triples " + where +
                " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
            return [{"subject": r[0], "relation": r[1], "object": r[2],
                     "ts": r[3]} for r in rows]
        except Exception as e:
            log.debug("lookup failed: %s", e)
            return []

    def list_recent(self, limit: int = 50) -> list[dict]:
        return self.lookup(limit=limit)


async def extract_triples() -> int:
    """LLM extraction from recent turns. Returns count added (0 when off
    or provider unavailable)."""
    from backend.config import config
    if not config.get("memory", "graph_extract", default=False):
        return 0

    from backend.memory.chat_history import chat_history
    messages = chat_history.get_context(max_messages=30)
    if not messages:
        return 0

    from backend.providers.registry import get_provider
    try:
        provider = get_provider()
    except Exception:
        return 0

    transcript = "\n".join(
        f"{m['role']}: {str(m['content'])[:200]}" for m in messages[-20:])
    prompt = ("Extract up to 8 knowledge-graph triples from this conversation. "
              "Each triple: {\"subject\": person/entity, \"relation\": short "
              "verb phrase, \"object\": entity/thing}. Return ONLY a JSON "
              "array, or [] if nothing worth saving.\n\n" + transcript)
    try:
        result = await provider.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=400, temperature=0.2)
        if not result.ok or not result.response or \
                result.response.startswith(("[Provider", "[Not connected")):
            return 0
        match = re.search(r"\[.*\]", result.response, re.S)
        if not match:
            return 0
        items = json.loads(match.group(0))
        return kg.add_triples([i for i in items if isinstance(i, dict)],
                              source="agent")
    except Exception as e:
        log.debug("triple extraction failed: %s", e)
        return 0


# Singleton
kg = KnowledgeGraph()
