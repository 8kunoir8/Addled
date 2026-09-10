"""
Vector memory — semantic recall system for Addled.

Stores embeddings of observations, actions, and user feedback.
Enables: "I've seen this before", "last time we did X", preference learning.

Uses pure numpy cosine similarity — no external vector DB for portability.
Persistence: SQLite blob storage of float32 arrays.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("addled.vector_memory")

DB_PATH = Path(__file__).parent / "vectors.db"
DEFAULT_DIM = 384  # Compatible with all-MiniLM-L6-v2 and similar


class VectorStore:
    """Lightweight vector database with cosine similarity search."""

    def __init__(self, dim: int = DEFAULT_DIM):
        self._dim = dim
        self._conn = None
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    embedding BLOB NOT NULL,
                    category TEXT NOT NULL DEFAULT 'observation',
                    metadata TEXT,
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vec_category ON vectors(category, timestamp DESC);
            """)
            self._conn.commit()
        except Exception as e:
            log.warning("Vector store unavailable: %s", e)
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def add(self, embedding: np.ndarray, category: str = "observation",
            metadata: dict | None = None) -> int | None:
        """Store an embedding. Returns row ID or None on failure."""
        if not self.available:
            return None
        try:
            if embedding.shape[0] != self._dim:
                embedding = self._resize(embedding)
            blob = embedding.astype(np.float32).tobytes()
            meta_json = json.dumps(metadata) if metadata else None
            cursor = self._conn.execute(
                "INSERT INTO vectors (embedding, category, metadata, timestamp) VALUES (?, ?, ?, ?)",
                (blob, category, meta_json, time.time()),
            )
            self._conn.commit()
            return cursor.lastrowid
        except Exception:
            return None

    def search(self, query: np.ndarray, category: str | None = None,
               top_k: int = 5, min_similarity: float = 0.0,
               since: float | None = None,
               until: float | None = None) -> list[dict]:
        """Find top-k most similar entries, optionally time-bounded.
        Returns [{id, similarity, category, metadata, timestamp}, ...]"""
        if not self.available:
            return []
        try:
            if query.shape[0] != self._dim:
                query = self._resize(query)
            query_vec = query.astype(np.float32).flatten()
            query_norm = np.linalg.norm(query_vec)
            if query_norm == 0:
                return []

            conds, args = [], []
            if category:
                conds.append("category = ?")
                args.append(category)
            if since is not None:
                conds.append("timestamp >= ?")
                args.append(float(since))
            if until is not None:
                conds.append("timestamp <= ?")
                args.append(float(until))
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            rows = self._conn.execute(
                "SELECT id, embedding, category, metadata, timestamp FROM vectors "
                + where + " ORDER BY timestamp DESC LIMIT 5000",
                tuple(args),
            ).fetchall()

            results = []
            for row_id, blob, cat, meta_json, ts in rows:
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] != self._dim:
                    continue
                vec_norm = np.linalg.norm(vec)
                if vec_norm == 0:
                    continue
                sim = float(np.dot(query_vec, vec) / (query_norm * vec_norm))
                if sim >= min_similarity:
                    results.append({
                        "id": row_id,
                        "similarity": sim,
                        "category": cat,
                        "metadata": json.loads(meta_json) if meta_json else None,
                        "timestamp": ts,
                    })
            results.sort(key=lambda r: r["similarity"], reverse=True)
            return results[:top_k]
        except Exception:
            return []

    def _resize(self, arr: np.ndarray) -> np.ndarray:
        """Resize embedding to configured dimension (pad or truncate)."""
        if arr.shape[0] < self._dim:
            padded = np.zeros(self._dim, dtype=np.float32)
            padded[:arr.shape[0]] = arr.flatten()
            return padded
        return arr.flatten()[:self._dim]

    # ---- management (dashboard memory page) ----------------------------------

    def list(self, category: str | None = None, limit: int = 100) -> list[dict]:
        """Most recent stored entries with metadata."""
        if not self.available:
            return []
        try:
            if category:
                rows = self._conn.execute(
                    "SELECT id, category, metadata, timestamp FROM vectors "
                    "WHERE category = ? ORDER BY timestamp DESC LIMIT ?",
                    (category, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, category, metadata, timestamp FROM vectors "
                    "ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            return [{"id": r[0], "category": r[1],
                     "metadata": json.loads(r[2]) if r[2] else None,
                     "timestamp": r[3]} for r in rows]
        except Exception:
            return []

    def delete(self, row_id: int) -> bool:
        if not self.available:
            return False
        try:
            self._conn.execute("DELETE FROM vectors WHERE id = ?", (row_id,))
            self._conn.commit()
            return True
        except Exception:
            return False

    def delete_category(self, category: str) -> int:
        """Delete every entry in a category. Returns rows removed."""
        if not self.available:
            return 0
        try:
            cur = self._conn.execute("DELETE FROM vectors WHERE category = ?",
                                     (category,))
            self._conn.commit()
            return cur.rowcount
        except Exception:
            return 0

    def find_near(self, embedding: np.ndarray, category: str | None = None,
                  min_sim: float = 0.95) -> dict | None:
        """Closest existing row within min_sim (dedup helper), or None."""
        hits = self.search(embedding, category=category, top_k=1,
                           min_similarity=min_sim)
        return hits[0] if hits else None

    def stamp(self) -> tuple:
        """(max_row_id, row_count) — cheap change-detection for caches."""
        if not self.available:
            return (-1, -1)
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id),0), COUNT(*) FROM vectors").fetchone()
            return (int(row[0]), int(row[1]))
        except Exception:
            return (-1, -1)

    def reembed(self, row_id: int, embedding: np.ndarray,
                kind: str = "minilm") -> bool:
        """Replace a row's vector and tag it with the embedder used."""
        if not self.available:
            return False
        try:
            if embedding.shape[0] != self._dim:
                embedding = self._resize(embedding)
            blob = embedding.astype(np.float32).tobytes()
            row = self._conn.execute(
                "SELECT metadata FROM vectors WHERE id = ?",
                (int(row_id),)).fetchone()
            meta = json.loads(row[0]) if row and row[0] else {}
            meta["embedder"] = kind
            self._conn.execute(
                "UPDATE vectors SET embedding = ?, metadata = ? WHERE id = ?",
                (blob, json.dumps(meta), int(row_id)))
            self._conn.commit()
            return True
        except Exception:
            return False

    def dedup(self, category: str | None = None, min_sim: float = 0.95,
              scan_limit: int = 2000) -> int:
        """Drop near-duplicate rows (newest kept). Returns count removed."""
        if not self.available:
            return 0
        try:
            rows = self._conn.execute(
                "SELECT id, embedding, category FROM vectors "
                "ORDER BY timestamp DESC LIMIT ?", (scan_limit,)).fetchall()
            kept: list[tuple[np.ndarray, str]] = []
            drop_ids: list[int] = []
            for row_id, blob, cat in rows:
                vec = np.frombuffer(blob, dtype=np.float32)
                if vec.shape[0] != self._dim:
                    continue
                vn = np.linalg.norm(vec)
                if vn == 0:
                    continue
                dup = False
                for kvec, kcat in kept:
                    if category is not None and kcat != category:
                        continue
                    sim = float(np.dot(vec, kvec) / (vn * np.linalg.norm(kvec)))
                    if sim >= min_sim:
                        dup = True
                        break
                if dup:
                    drop_ids.append(row_id)
                else:
                    kept.append((vec, cat))
            for rid in drop_ids:
                self._conn.execute("DELETE FROM vectors WHERE id = ?", (rid,))
            self._conn.commit()
            return len(drop_ids)
        except Exception:
            return 0

    def expire(self, category: str | None = None,
               max_age_h: float = 24 * 365) -> int:
        """Delete rows older than max_age_h. Returns count removed."""
        if not self.available:
            return 0
        cutoff = time.time() - max_age_h * 3600
        try:
            if category:
                cur = self._conn.execute(
                    "DELETE FROM vectors WHERE category = ? AND timestamp < ?",
                    (category, cutoff))
            else:
                cur = self._conn.execute(
                    "DELETE FROM vectors WHERE timestamp < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount
        except Exception:
            return 0


# Singleton
vector_store = VectorStore()
