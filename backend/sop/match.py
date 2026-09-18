"""
Finding the procedure that fits the task in front of us.

Two questions, in order: which category is this task, and which procedure in
that category is closest to it? The category filter matters more than it looks
— it keeps a code recipe from being offered for a web search, which is exactly
the kind of confident-but-wrong suggestion that makes a feature like this
annoying rather than useful.

Embeddings are used when they are available and lexical overlap when they are
not, so a missing model degrades the ranking instead of breaking the lookup.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("addled.sop.match")

WORD_RE = re.compile(r"[a-z0-9_]{3,}")

# Words that carry no signal about which procedure applies.
STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "please",
    "can", "you", "your", "him", "her", "its", "our", "are", "was", "were",
    "have", "has", "had", "will", "would", "should", "could", "then", "than",
    "them", "they", "there", "here", "what", "when", "where", "which", "how",
    "why", "all", "any", "some", "not", "but", "out", "get", "got", "let",
    "use", "using", "used", "make", "made", "need", "want", "like", "just",
    "about", "over", "again", "also", "very", "much", "more", "most", "now",
    "addled",
}

# Task words that point at a category. Checked against the message, not the
# tools, so a task with no tool history yet can still be placed.
CATEGORY_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("files", ("file", "files", "folder", "directory", "write", "read",
               "append", "rename", "copy", "move", "delete", "save", "notes",
               "document", "txt", "pdf", "csv", "json")),
    ("code", ("code", "function", "class", "bug", "refactor", "compile",
              "test", "tests", "script", "python", "typescript", "javascript",
              "import", "error", "traceback", "diff", "repository", "repo",
              "git", "commit")),
    ("web", ("search", "google", "look", "up", "internet", "online", "wikipedia",
             "article", "news", "research", "source", "sources")),
    ("browser", ("browser", "page", "website", "site", "url", "click", "form",
                 "webpage", "tab", "link")),
    ("calendar", ("calendar", "event", "meeting", "schedule", "appointment",
                  "reminder", "tomorrow", "monday", "today")),
    ("system", ("command", "shell", "terminal", "process", "install",
                "uninstall", "system", "windows", "registry", "service")),
    ("desktop", ("window", "screen", "desktop", "mouse", "keyboard", "click",
                 "type", "screenshot", "app")),
    ("memory", ("remember", "recall", "memory", "forget", "note", "notes",
                "wiki", "knowledge", "preference")),
    ("vision", ("image", "picture", "photo", "screenshot", "see", "look",
                "screen", "diagram", "chart")),
]

DEFAULT_CATEGORY = "general"


def words(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(str(text or "").lower())
            if w not in STOPWORDS}


def _skill_category(tool_name: str) -> str | None:
    try:
        from backend.skills.registry import skill_registry
        skill = skill_registry.get(tool_name)
        if skill is not None:
            return getattr(skill, "category", None)
    except Exception as e:
        log.debug("Could not resolve category for %s: %s", tool_name, e)
    return None


def category_for(tool_names) -> str:
    """The category most of these tools belong to.

    Tools are the best evidence available: they are what actually ran, so the
    category reflects what the task turned out to be rather than what the
    wording suggested.
    """
    names = [str(t or "").strip() for t in (tool_names or []) if str(t or "").strip()]
    if not names:
        return DEFAULT_CATEGORY
    counts: dict[str, int] = {}
    for name in names:
        category = _skill_category(name)
        if category:
            counts[category] = counts.get(category, 0) + 1
    if not counts:
        return DEFAULT_CATEGORY
    return max(counts.items(), key=lambda kv: kv[1])[0]


def guess_category(message: str) -> str:
    """Best guess from the wording, for tasks with no tool history."""
    text_words = words(message)
    if not text_words:
        return DEFAULT_CATEGORY
    best, best_hits = DEFAULT_CATEGORY, 0
    for category, hints in CATEGORY_HINTS:
        hits = len(text_words & set(hints))
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def _sop_text(sop: dict) -> str:
    return " ".join([str(sop.get("title") or ""),
                     str(sop.get("category") or ""),
                     " ".join(str(s) for s in (sop.get("steps") or [])),
                     " ".join(str(t) for t in (sop.get("tools") or []))])


def _overlap(query: set[str], candidate: set[str]) -> int:
    """How many of the query's words the candidate accounts for.

    A prefix counts as a match, so "overwrite" finds "overwriting" and "read"
    finds "read_file". Four characters is the floor, which is long enough that
    "rea" does not match half the language.
    """
    hits = 0
    for word in query:
        for other in candidate:
            if (word == other
                    or (len(word) >= 4 and other.startswith(word))
                    or (len(other) >= 4 and word.startswith(other))):
                hits += 1
                break
    return hits


def _lexical(query_words: set[str], sop: dict) -> float:
    """Share of the query's words the procedure accounts for, weighted.

    Measuring what the query covers, rather than what the candidate contains,
    is what lets a short task find a longer procedure. Title words count for
    most of it, since a procedure's title is its subject.
    """
    if not query_words:
        return 0.0
    total = len(query_words)
    title = _overlap(query_words, words(sop.get("title") or "")) / total
    body = _overlap(query_words, words(
        " ".join(str(s) for s in (sop.get("steps") or [])))) / total
    tools = _overlap(query_words, words(
        " ".join(str(t) for t in (sop.get("tools") or [])))) / total
    return min(0.5 * title + 0.3 * body + 0.2 * tools, 1.0)


def _cosine(a, b) -> float:
    try:
        import numpy as np
        a = np.asarray(a, dtype="float32").ravel()
        b = np.asarray(b, dtype="float32").ravel()
        na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
    except Exception as e:
        log.debug("cosine failed: %s", e)
        return 0.0


def _embeddings_available() -> bool:
    try:
        from backend.memory import embedding
        return embedding.embedder_kind() != "hash"
    except Exception:
        return False


def score(task: str, sop: dict, task_vector=None) -> tuple[float, str]:
    """(score, how it was scored). Embedding score when possible, else lexical."""
    lexical = _lexical(words(task), sop)
    if task_vector is None or not _embeddings_available():
        return lexical, "lexical"
    try:
        from backend.memory.embedding import embed_text
        vector = embed_text(_sop_text(sop))
        return _cosine(task_vector, vector), "embedding"
    except Exception as e:
        log.debug("Embedding comparison failed, using words: %s", e)
        return lexical, "lexical"


def similarity_pair(task: str, sop: dict) -> tuple[float, str]:
    """(score, method) against a task string, for merge decisions.

    The method matters: an embedding cosine and a word-overlap share are not
    the same units, so they cannot be compared against one number.
    """
    task_vector = None
    try:
        from backend.memory.embedding import embed_text
        task_vector = embed_text(task)
    except Exception:
        task_vector = None
    return score(task, sop, task_vector)


def similarity(task: str, sop: dict) -> float:
    """Best available score against a task string, ignoring how it was scored."""
    return similarity_pair(task, sop)[0]


def threshold(method: str, kind: str = "min") -> float:
    """The bar for this scoring method. Measured, not guessed.

    On the sample of real procedures, a matching task scores about 0.45 by
    embedding and 0.42 by words, while an unrelated task scores 0.20 and a
    procedure from the wrong category 0.28. The bars sit in the gap.
    """
    from backend.sop import store

    if kind == "merge":
        key, fallback = ("merge_similarity", 0.82) if method == "embedding" \
            else ("lexical_merge_similarity", 0.55)
    else:
        key, fallback = ("min_similarity", 0.35) if method == "embedding" \
            else ("lexical_min_similarity", 0.30)
    try:
        return float(store._setting(key, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback


def find(task: str, category: str | None = None, limit: int | None = None) -> list[dict]:
    """Procedures for this task, best first: [{'sop':…, 'score':…}]."""
    from backend.sop import store

    text = str(task or "").strip()
    if not text:
        return []
    if limit is None:
        try:
            limit = int(store._setting("inject_top_k", 1) or 1)
        except Exception:
            limit = 1
    limit = max(1, min(int(limit), 10))

    if category:
        candidates = store.list_all(category)
    else:
        candidates = store.list_all()
    if not candidates:
        return []

    task_vector = None
    try:
        from backend.memory.embedding import embed_text
        task_vector = embed_text(text)
    except Exception as e:
        log.debug("No embedding for the task, ranking by words: %s", e)

    scored = []
    for sop in candidates:
        value, method = score(text, sop, task_vector)
        scored.append({"sop": sop, "score": round(value, 4), "method": method})
    scored.sort(key=lambda item: -item["score"])
    return scored[:limit]


def best(task: str, category: str | None = None) -> dict | None:
    """The single best procedure above the injection threshold, or None."""
    for item in find(task, category=category, limit=1):
        if item["score"] >= threshold(item["method"]):
            return item
    return None


def build_sop_context(task: str) -> str | None:
    """The [Procedure] block for the system prompt, or None if nothing fits.

    Returning None rather than an empty block matters: a header with no
    procedure under it reads to the model like an instruction it failed to
    receive.
    """
    from backend.sop import store

    if not store.enabled():
        return None
    try:
        category = guess_category(task)
        hit = best(task, category=category) or best(task)
    except Exception as e:
        log.debug("procedure lookup failed: %s", e)
        return None
    if not hit:
        return None

    sop = hit["sop"]
    steps = [str(s).strip() for s in (sop.get("steps") or []) if str(s).strip()]
    if not steps:
        return None

    lines = [
        "[Procedure]",
        f"A procedure for '{sop.get('category')}' tasks worked before "
        f"({sop.get('successes') or 0}/{sop.get('uses') or 0} successful): "
        f"{sop.get('title')}.",
        *(f"{i}. {step}" for i, step in enumerate(steps, 1)),
        "Follow it when it fits. If the task differs, say so and do what the "
        "task actually needs.",
    ]
    return "\n".join(lines)
