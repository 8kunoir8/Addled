"""Line-anchored passage chunking and retrieval for code and documents.

Pure stdlib, zero external dependencies.
Splits text files into overlapping line-anchored passages so coding agents
can read the relevant function or block (~200 tokens) rather than swallowing
an entire 80,000-token source file.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9]+")
_EXACT_MATCH_BOOST = 10.0


def tokenize(text: str) -> list[str]:
    """Tokenize query or passage into lowercase alphanumeric words."""
    return _WORD_RE.findall((text or "").lower())


def chunk_lines(text: str, window: int = 30, stride: int = 15) -> list[tuple[int, int, str]]:
    """Split text into overlapping line-anchored windows.

    Returns list of (start_line, end_line, passage_text), 1-indexed.
    """
    if not text:
        return []
    lines = text.splitlines()
    total_lines = len(lines)
    if total_lines <= window:
        return [(1, total_lines, text)]

    passages = []
    for i in range(0, total_lines, stride):
        chunk = lines[i:i + window]
        if not chunk:
            break
        start_line = i + 1
        end_line = min(i + window, total_lines)
        passages.append((start_line, end_line, "\n".join(chunk)))
        if end_line >= total_lines:
            break
    return passages


class PassageIndex:
    """In-memory BM25 index over line-anchored passages from multiple files."""

    def __init__(self, passages: list[tuple[str, int, int, str]], k1: float = 1.5, b: float = 0.75):
        """Passages is list of (file_path, start_line, end_line, text)."""
        self.k1 = k1
        self.b = b
        self.passages = passages
        self.n = len(passages)

        doc_toks = [tokenize(p[3]) for p in passages]
        self.tf = [Counter(d) for d in doc_toks]
        self.dl = [len(d) for d in doc_toks]
        self.avgdl = (sum(self.dl) / max(self.n, 1)) if self.n else 0.0

        df: Counter[str] = Counter()
        for t in self.tf:
            df.update(t.keys())
        self.df = df
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
            for t, c in df.items()
        }

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Find best matching passages, best first.

        Returns list of {filePath, startLine, endLine, snippet, score}.
        """
        if limit <= 0 or not self.n:
            return []

        raw_query = query.strip()
        q_tokens = [w for w in tokenize(raw_query) if len(w) > 1]
        if not q_tokens:
            return []

        q_lower = raw_query.lower()
        scored: list[tuple[float, tuple[str, int, int, str]]] = []

        for i, (fp, sl, el, chunk) in enumerate(self.passages):
            tf_map = self.tf[i]
            s = 0.0
            hits = 0
            for t in q_tokens:
                if t in tf_map:
                    f = tf_map[t]
                    idf_val = self.idf.get(t, 0.0)
                    denom = f + self.k1 * (1 - self.b + self.b * self.dl[i] / max(self.avgdl, 0.001))
                    s += idf_val * (f * (self.k1 + 1) / denom)
                    hits += 1

            if hits > 0:
                # Boost for exact multi-token substring or exact symbol hits
                chunk_l = chunk.lower()
                if len(q_lower) >= 3 and q_lower in chunk_l:
                    s += _EXACT_MATCH_BOOST
                else:
                    # Check if any significant token is an exact identifier
                    exact_symbols = sum(1 for t in q_tokens if len(t) >= 4 and t in chunk_l)
                    if exact_symbols:
                        s += min(exact_symbols * 2.0, _EXACT_MATCH_BOOST)
                scored.append((s, (fp, sl, el, chunk)))

        scored.sort(key=lambda pair: (-pair[0], pair[1][0], pair[1][1]))
        return [
            {
                "filePath": fp,
                "startLine": sl,
                "endLine": el,
                "snippet": chunk,
                "score": round(score, 2),
            }
            for score, (fp, sl, el, chunk) in scored[:limit]
        ]
