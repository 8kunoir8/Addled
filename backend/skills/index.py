"""In-memory BM25 retrieval index for skills.

Zero dependencies, standard library only.
Designed for the live SkillRegistry:
- Tokenizer splits on non-alphanumeric (hyphens, underscores, dots, whitespace).
- Suffix stemmer handles regular inflections (ing, ed, es, s, ment, etc.) with memoization.
- Dynamic document frequency (DF) cutoff suppresses ubiquitous terms without static stopword lists.
- Exact slug matches receive a 2.0x weight boost.
- Thread-safe or simple invalidation on skill registration / mutation.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.skills.registry import SkillDefinition

_WORD_RE = re.compile(r"[a-z0-9]+")
_SUFFIXES = ("ing", "ers", "er", "ions", "ion", "ed", "es", "s", "ly", "ment")
_EXACT_SLUG_BOOST = 2.0
_DEFAULT_DF_CUT = 0.20
_STEM_CACHE: dict[str, str] = {}


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _stem(word: str) -> str:
    cached = _STEM_CACHE.get(word)
    if cached is not None:
        return cached
    res = word
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 3 and word.endswith(suf):
            res = word[:-len(suf)]
            break
    _STEM_CACHE[word] = res
    return res


class SkillIndex:
    """In-memory BM25 index over a snapshot of skills."""

    def __init__(self, skills: list[SkillDefinition], k1: float = 1.5, b: float = 0.5,
                 df_cut: float = _DEFAULT_DF_CUT):
        self.k1 = k1
        self.b = b
        self.df_cut = df_cut
        self.slugs: set[str] = set()
        self.names: list[str] = []
        self.skills_by_name: dict[str, SkillDefinition] = {}

        doc_tokens: list[list[str]] = []
        for s in skills:
            self.names.append(s.name)
            self.slugs.add(s.name.lower())
            self.skills_by_name[s.name] = s
            # Index name, description, and body text
            body_txt = str(getattr(s, "body", "") or "")
            full_txt = f"{s.name} {s.description or ''} {body_txt}"
            ws = [_stem(w) for w in _tokenize(full_txt)]
            doc_tokens.append(ws)

        self.n = len(self.names)
        self.dl = [len(d) for d in doc_tokens]
        self.avgdl = (sum(self.dl) / max(self.n, 1)) if self.n else 0.0

        self.tf = [Counter(d) for d in doc_tokens]
        df: Counter[str] = Counter()
        for t in self.tf:
            df.update(t.keys())
        self.df = df
        self.idf = {
            t: math.log(1 + (self.n - c + 0.5) / (c + 0.5))
            for t, c in df.items()
        }

    def search(self, query: str, limit: int = 12, floor: float = 0.0) -> list[tuple[float, SkillDefinition]]:
        """Rank skills for query, best first.

        Returns list of (score, SkillDefinition).
        """
        if limit <= 0 or not self.n:
            return []

        raw_tokens = [w for w in _tokenize(query) if len(w) > 2]
        if not raw_tokens:
            return []

        q_stemmed = [_stem(w) for w in raw_tokens]
        exact_slug_matches = {w for w in raw_tokens if w in self.slugs}

        scored: list[tuple[float, SkillDefinition]] = []
        df_threshold = self.df_cut * self.n if self.df_cut else 0.0

        for i, tf in enumerate(self.tf):
            s = 0.0
            content_hits = 0
            for t in q_stemmed:
                if t not in tf:
                    continue
                # Suppress ubiquitous stopwords that appear in > df_cut of all docs
                if df_threshold and self.df[t] > df_threshold:
                    continue
                f = tf[t]
                idf_val = self.idf.get(t, 0.0)
                denom = f + self.k1 * (1 - self.b + self.b * self.dl[i] / max(self.avgdl, 0.001))
                s += idf_val * (f * (self.k1 + 1) / denom)
                content_hits += 1

            if s > floor and content_hits > 0:
                skill_obj = self.skills_by_name[self.names[i]]
                if skill_obj.name.lower() in exact_slug_matches:
                    s *= _EXACT_SLUG_BOOST
                scored.append((s, skill_obj))

        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return scored[:limit]
