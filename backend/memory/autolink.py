"""
Automatic relations.

Two things Addled can work out for itself, so the graph is useful before the
user ever links anything by hand:

  * **Files a memory mentions.** When a fact, triple or conversation memory names
    a path, it is linked to that file with ``mentions``. This is what makes
    "which memories are about this file?" answerable.
  * **Where a memory came from.** A triple extracted from a conversation, or a
    session summary written at the end of one, is linked back to its origin with
    ``derived_from``, so provenance is a query rather than a guess.

Everything here is best-effort: a failure must never break the memory write that
called it, so every public function swallows its own errors.
"""

from __future__ import annotations

import logging
import os
import re

import numpy as np

from backend.memory.links import link_store

log = logging.getLogger("addled.memory.autolink")

MAX_PATHS_PER_TEXT = 20
MAX_NOTE = 160

# Quoted candidates come first: a quoted path may legitimately contain spaces,
# and the unquoted patterns deliberately refuse whitespace so they cannot run on
# into the prose that follows a path.
_PATTERNS = (
    re.compile(r"[\"']([^\"'\r\n]{3,400})[\"']"),
    re.compile(r"[A-Za-z]:[\\/](?:[^\s\\/:*?\"<>|]+[\\/])*[^\s\\/:*?\"<>|]*"),
    re.compile(r"\\\\[^\s\\/]+[\\/][^\s\"'`;)]+"),
    re.compile(r"(?<![\w:/])~[\\/][^\s\"'`;)]+"),
    re.compile(r"(?<![\w:])/(?:[\w.@+-]+/)+[\w.@+-]+"),
    # Relative paths such as backend/config.py. The lookbehind refuses a
    # preceding separator or word character, so it cannot re-match the tail of
    # an absolute path that an earlier pattern already captured.
    re.compile(r"(?<![\w.\\/])(?:[\w.@+-]+[\\/])+[\w.@+-]+\.[A-Za-z]\w{1,7}"),
)

# A line reference immediately after a match: ``path.py:42`` or ``path.py#L42``.
_LINE_AFTER = re.compile(r"(?::|#L)(\d+)")

_TRAILING_JUNK = ".,;:)]}\"'`"
_URL = re.compile(r"^\w+://")
# URLs contain path-like segments (``/example.com/a.txt``) that would otherwise
# be picked up as real files, so they are blanked out before scanning.
_URL_STRIP = re.compile(r"\b\w+://\S+")
# path.py:42 / path.py#L42 — keep the file, remember the line.
_LINE_SUFFIX = re.compile(r"[#:]L?(\d+)$")


def _clean(raw: str) -> tuple[str, int | None]:
    """Trim punctuation and split a trailing ``:line`` off a candidate."""
    text = raw.strip().rstrip(_TRAILING_JUNK)
    line = None
    match = _LINE_SUFFIX.search(text)
    if match:
        line = int(match.group(1))
        text = text[:match.start()]
    return text.rstrip(_TRAILING_JUNK), line


def _looks_like_file(path: str) -> bool:
    """Accept a candidate that exists, or that is convincingly path-shaped.

    Requiring a real extension (two or more characters) is what keeps version
    strings like ``v2.0``, prose and things like ``and/or`` out of the graph; if
    the file genuinely exists it is always accepted.
    """
    if len(path) < 3 or len(path) > 400:
        return False
    if _URL.match(path):
        return False
    try:
        if os.path.exists(os.path.expanduser(path)):
            return True
    except (OSError, ValueError):
        return False
    name = os.path.basename(path.replace("\\", "/"))
    if "." not in name:
        return False
    stem, _, ext = name.rpartition(".")
    if not (2 <= len(ext) <= 8 and ext.isalnum()):
        return False
    # A space in the name is only trusted when the path already has a separator,
    # which is what distinguishes a real "My Documents\\notes.md" from a phrase.
    if " " in stem and not re.search(r"[\\/]", path):
        return False
    return True


def find_paths(text: str, limit: int = MAX_PATHS_PER_TEXT) -> list[dict]:
    """File paths mentioned in text, deduplicated, with optional line numbers."""
    if not text:
        return []
    # Same length as the original, so snippet offsets stay valid.
    scan = _URL_STRIP.sub(lambda m: " " * len(m.group(0)), text)
    found: dict[str, dict] = {}
    spans: list[tuple[int, int]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(scan):
            # The quoted pattern captures the path; the others match it whole.
            span = match.span(1) if match.lastindex else match.span(0)
            # Skip anything already inside a match we accepted: the quoted
            # pattern captures a whole path, and the relative pattern would
            # otherwise re-match its tail ("Notes\\todo.md" inside
            # "C:\\My Notes\\todo.md").
            if any(s <= span[0] and span[1] <= e for s, e in spans):
                continue
            raw = match.group(1) if match.lastindex else match.group(0)
            candidate, line = _clean(raw)
            if line is None:
                # ``path.py:42`` — the reference sits just past the match.
                suffix = _LINE_AFTER.match(text[match.end():match.end() + 8])
                if suffix:
                    line = int(suffix.group(1))
            if not _looks_like_file(candidate):
                continue
            key = os.path.normcase(candidate)
            if key in found:
                if line and not found[key]["line"]:
                    found[key]["line"] = line
                continue
            found[key] = {"path": candidate, "line": line,
                          "context": _snippet(text, span[0])}
            spans.append(span)
            if len(found) >= limit:
                return list(found.values())
    return list(found.values())


def _snippet(text: str, index: int, width: int = 60) -> str:
    start = max(0, index - width)
    end = min(len(text), index + width)
    return " ".join(text[start:end].split())[:MAX_NOTE]


def link_text(kind: str, ref_id, text: str, source: str = "auto",
              extra_note: str = "") -> int:
    """Create ``mentions`` edges from a memory to every file its text names.

    Returns the number of links written. Never raises.
    """
    try:
        if not bool(_cfg("auto_file_links", True)):
            return 0
        count = 0
        for hit in find_paths(text):
            note = extra_note or hit.get("context") or ""
            if hit.get("line"):
                note = f"line {hit['line']}: {note}" if note else f"line {hit['line']}"
            if link_store.link(kind, ref_id, "mentions", "file", hit["path"],
                               source=source, confidence=0.9, note=note,
                               meta={"line": hit.get("line")} if hit.get("line")
                               else None) is not None:
                count += 1
        return count
    except Exception as e:
        log.debug("file auto-link failed for %s/%s: %s", kind, ref_id, e)
        return 0


def link_provenance(child_kind: str, child_id, parent_kind: str, parent_id,
                    rel: str = "derived_from", source: str = "auto",
                    note: str = "", meta: dict | None = None) -> bool:
    """Record that one memory was produced from another."""
    try:
        if not bool(_cfg("auto_provenance", True)):
            return False
        return link_store.link(child_kind, child_id, rel, parent_kind, parent_id,
                               source=source, confidence=1.0, note=note,
                               meta=meta) is not None
    except Exception as e:
        log.debug("provenance link failed (%s %s -> %s %s): %s",
                  child_kind, child_id, parent_kind, parent_id, e)
        return False


def link_duplicate(kind_a: str, id_a, kind_b: str, id_b, similarity: float,
                   source: str = "dedup") -> bool:
    """Mark two memories as the same thing, keeping the similarity as confidence."""
    try:
        return link_store.link(kind_a, id_a, "same_as", kind_b, id_b,
                               source=source,
                               confidence=round(float(similarity), 3)) is not None
    except Exception:
        return False


def link_near_duplicates(min_sim: float = 0.92, limit: int = 400,
                         kind: str = "fact") -> int:
    """Mark near-identical facts as ``same_as``.

    Same text added twice is already rejected by the store, but the same fact
    rephrased ("prefers dark mode" / "likes dark themes") slips through and then
    gets counted twice in every prompt. Facts are few, so a quadratic scan is
    cheaper than the bookkeeping an index would need.

    Returns the number of pairs marked.
    """
    try:
        if not bool(_cfg("dedup_links", True)):
            return 0
        from backend.memory.facts import get_facts
        facts = [f for f in (get_facts(limit) or []) if str(f.get("text"))]
        if len(facts) < 2:
            return 0
        from backend.memory.embedding import embed_text
        texts = [str(f["text"]).strip().lower() for f in facts]
        vectors = []
        for text in texts:
            vec = np.asarray(embed_text(text), dtype="float32").ravel()
            norm = float(np.linalg.norm(vec)) or 1.0
            vectors.append(vec / norm)  # so a dot product is a cosine
        seen_pairs: set[tuple] = set()
        marked = 0
        for i, vi in enumerate(vectors):
            for j in range(i + 1, len(vectors)):
                similarity = float(np.dot(vi, vectors[j]))
                if similarity < min_sim:
                    continue
                pair = (facts[i]["id"], facts[j]["id"])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if link_duplicate(kind, pair[0], kind, pair[1], similarity):
                    marked += 1
        return marked
    except Exception as e:
        log.debug("near-duplicate scan failed: %s", e)
        return 0


def forget_ref(kind: str, ref_id) -> int:
    """Drop every relation touching one memory. Call this when it is deleted.

    Lives here so that :data:`link_store` is the single seam: writes and cleanup
    both go through this module, which is what makes the behaviour testable by
    swapping one attribute.
    """
    try:
        return link_store.forget(kind, ref_id)
    except Exception as e:
        log.debug("forget failed for %s/%s: %s", kind, ref_id, e)
        return 0


def forget_kind(kind: str) -> int:
    """Drop every relation involving a whole kind of memory."""
    try:
        return link_store.forget_all(kind)
    except Exception:
        return 0


def _cfg(key: str, default):
    try:
        from backend.config import config
        return config.get("links", key, default=default)
    except Exception:
        return default
