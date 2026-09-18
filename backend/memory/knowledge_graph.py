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

import numpy as np

log = logging.getLogger("addled.knowledge_graph")

DB_PATH = Path(__file__).parent / "triples.db"


def _triple_text(sub: str, rel: str, obj: str) -> str:
    return f"{sub} {rel} {obj}"


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
            self._ensure_embedding_column()
        except Exception as e:
            log.warning("Knowledge graph unavailable: %s", e)
            self._conn = None

    def _ensure_embedding_column(self):
        """Lazily add the embedding column (older DBs lack it)."""
        try:
            cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(triples)").fetchall()]
            if "embedding" not in cols:
                self._conn.execute(
                    "ALTER TABLE triples ADD COLUMN embedding BLOB")
                self._conn.commit()
        except Exception as e:
            log.debug("embedding column migration failed: %s", e)

    @property
    def available(self) -> bool:
        return self._conn is not None

    @staticmethod
    def _embed(text: str) -> bytes | None:
        """384-dim vector for a triple (hash fallback when model missing)."""
        try:
            from backend.memory.embedding import embed_text
            return embed_text(text).astype(np.float32).tobytes()
        except Exception as e:
            log.debug("triple embed failed: %s", e)
            return None

    def add_triples(self, triples: list[dict], source: str = "agent",
                    origin: tuple[str, str] | None = None) -> int:
        """Add [{subject, relation, object}] rows. Returns count added.

        ``origin`` is an optional ``(kind, id)`` reference the triples were
        derived from, recorded as a ``derived_from`` relation so provenance is
        a query rather than a guess.
        """
        if not self.available:
            return 0
        added = 0
        inserted: list[tuple[int, str]] = []
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
                blob = self._embed(_triple_text(sub, rel, obj))
                cur = self._conn.execute(
                    "INSERT INTO triples (subject, relation, object, ts, "
                    "source, embedding) VALUES (?,?,?,?,?,?)",
                    (sub, rel, obj, time.time(), source, blob))
                inserted.append((int(cur.lastrowid), f"{sub} {rel} {obj}"))
                added += 1
            self._conn.commit()
        except Exception as e:
            log.debug("add_triples failed: %s", e)
        # Relate the new triples to the files they name and to their origin.
        try:
            from backend.memory.autolink import link_provenance, link_text
            for row_id, text in inserted:
                link_text("triple", row_id, text, source="auto",
                          extra_note=text[:80])
                if origin:
                    link_provenance("triple", row_id, origin[0], origin[1],
                                    note=text[:80])
        except Exception as e:
            log.debug("triple auto-link failed: %s", e)
        return added

    def lookup(self, subject: str | None = None, relation: str | None = None,
               since: float | None = None, until: float | None = None,
               limit: int = 20) -> list[dict]:
        """Time-bounded graph lookup.
        Returns [{id, subject, relation, object, ts}] newest first."""
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
                "SELECT id, subject, relation, object, ts FROM triples " +
                where + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
            return [{"id": r[0], "subject": r[1], "relation": r[2],
                     "object": r[3], "ts": r[4]} for r in rows]
        except Exception as e:
            log.debug("lookup failed: %s", e)
            return []

    def semantic_search(self, query: str, top_k: int = 8,
                        since: float | None = None,
                        until: float | None = None) -> list[dict]:
        """Cosine-similarity search over embedded triple texts.
        Falls back to lexical lookup when no embeddings exist yet."""
        if not self.available:
            return []
        from backend.memory.embedding import embed_text
        try:
            qvec = embed_text(query)
            qnorm = float(np.linalg.norm(qvec))
            if qnorm == 0:
                return self.lookup(limit=top_k, since=since, until=until)
            conds, args = [], []
            if since is not None:
                conds.append("ts >= ?")
                args.append(float(since))
            if until is not None:
                conds.append("ts <= ?")
                args.append(float(until))
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            rows = self._conn.execute(
                "SELECT id, subject, relation, object, ts, embedding "
                "FROM triples " + where +
                " ORDER BY ts DESC LIMIT 1000", tuple(args)).fetchall()
        except Exception as e:
            log.debug("semantic_search failed (%s) — lexical fallback", e)
            return self.lookup(limit=top_k, since=since, until=until)

        scored: list[tuple[float, dict]] = []
        for rid, sub, rel, obj, ts, blob in rows:
            if not blob:
                continue
            try:
                vec = np.frombuffer(blob, dtype=np.float32)
                vn = float(np.linalg.norm(vec))
                if vn == 0:
                    continue
                sim = float(np.dot(qvec, vec) / (qnorm * vn))
                scored.append((sim, {"id": rid, "subject": sub,
                                   "relation": rel, "object": obj, "ts": ts,
                                   "similarity": sim}))
            except Exception:
                continue
        scored.sort(key=lambda x: -x[0])
        top = [t for _, t in scored[:top_k]]
        if top:
            return top
        return self.lookup(limit=top_k, since=since, until=until)

    def delete(self, triple_id: int) -> bool:
        if not self.available:
            return False
        try:
            cur = self._conn.execute("DELETE FROM triples WHERE id = ?",
                                     (int(triple_id),))
            self._conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False

    def count(self) -> int:
        if not self.available:
            return 0
        try:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM triples").fetchone()[0])
        except Exception:
            return 0

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
    from backend.providers import router
    try:
        result = await provider.chat(
            [{"role": "user", "content": prompt}],
            model=router.for_provider(provider, "utility"),
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
