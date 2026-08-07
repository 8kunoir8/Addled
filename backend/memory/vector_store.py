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
               top_k: int = 5, min_similarity: float = 0.0) -> list[dict]:
        """Find top-k most similar entries. Returns [{id, similarity, category, metadata, timestamp}, ...]"""
        if not self.available:
            return []
        try:
            if query.shape[0] != self._dim:
                query = self._resize(query)
            query_vec = query.astype(np.float32).flatten()
            query_norm = np.linalg.norm(query_vec)
            if query_norm == 0:
                return []

            rows = self._conn.execute(
                "SELECT id, embedding, category, metadata, timestamp FROM vectors "
                + ("WHERE category = ? " if category else "")
                + "ORDER BY timestamp DESC LIMIT 5000",
                (category,) if category else (),
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


# Singleton
vector_store = VectorStore()
