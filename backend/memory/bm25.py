"""
BM25 keyword search over stored memory texts.

Pairs with semantic vector recall via reciprocal rank fusion (RRF) so
exact-name queries (apps, repos, commands) stay findable even when
embedding similarity is weak. Pure stdlib — no dependencies.
"""

from __future__ import annotations

import logging
import math
import re

log = logging.getLogger("addled.bm25")

K1 = 1.5
B = 0.75


class BM25Index:
    """Stdlib Okapi BM25 over conversation texts, with a version-stamped
    cache that rebuilds only when vectors.db changed."""

    def __init__(self):
        self._docs: list[dict] = []       # [{id, text}]
        self._version: tuple = (-1, -1)   # (max_row_id, row_count)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [t.lower() for t in re.findall(r"[\w']+", text or "")]

    def sync(self) -> bool:
        """Rebuild the index if the vector store changed. True if rebuilt."""
        from backend.memory.vector_store import vector_store
        stamp = vector_store.stamp()
        if stamp == self._version:
            return False
        try:
            rows = vector_store.list(category="conversation", limit=5000)
            docs = []
            for r in rows:
                meta = r.get("metadata") or {}
                text = str(meta.get("text", "")).strip()
                if text:
                    docs.append({"id": r["id"], "text": text})
            self._docs = docs
            self._version = stamp
        except Exception as e:
            log.debug("bm25 sync failed: %s", e)
            self._docs = []
        return True

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Return [{id, score, text}] ranked by BM25."""
        self.sync()
        tokens = self._tokenize(query)
        if not tokens or not self._docs:
            return []

        tokenized = [self._tokenize(d["text"]) for d in self._docs]
        n = len(self._docs)
        dl = [len(t) for t in tokenized]
        avgdl = sum(dl) / n if n else 0.0

        df = {t: sum(1 for toks in tokenized if t in toks) for t in tokens}

        scored: list[tuple[float, dict]] = []
        for i, toks in enumerate(tokenized):
            tf = {t: toks.count(t) for t in set(toks)}
            score = 0.0
            for t in set(tokens):
                if t not in tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                denom = tf[t] + K1 * (1 - B + B * dl[i] / avgdl)
                score += idf * (tf[t] * (K1 + 1)) / denom
            if score > 0:
                scored.append((score, self._docs[i]))
        scored.sort(key=lambda x: -x[0])
        return [{"id": d["id"], "score": round(s, 5), "text": d["text"]}
                for s, d in scored[:top_k]]


# Singleton
bm25_index = BM25Index()
