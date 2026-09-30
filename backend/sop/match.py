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
               "document", "txt", "pdf", "csv", "json", "rewrite", "overwrite",
               "update", "replace", "edit")),
    ("code", ("code", "function", "class", "bug", "refactor", "compile",
              "test", "tests", "script", "python", "typescript", "javascript",
              "import", "error", "traceback", "diff", "repository", "repo",
              "git", "commit", "edit", "parser", "fix", "debug", "rewrite",
              "implement", "module", "syntax")),
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
    ("vision", ("image", "picture", "photo", "diagram", "chart",
                "describe", "visual")),
]

DEFAULT_CATEGORY = "general"


def words(text: str) -> set[str]:
    """The significant words in a piece of text, with verbs normalised.

    `_VERB_SYNONYMS` is applied here rather than in one scorer, so every signal
    that reads these words — lexical coverage, the shared-term count, tool
    affinity and category agreement — is comparing the same vocabulary. Doing it
    per-scorer would let one see "rewrite" and another "overwrite" and quietly
    disagree about whether two texts are about the same thing.
    """
    return {_VERB_SYNONYMS.get(w, w)
            for w in WORD_RE.findall(str(text or "").lower())
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

# Tool categories that do not exist in this module's taxonomy, mapped to the
# procedure category that covers them.
#
# The two classifiers disagreed, and it was not theoretical. LEARNING files a
# procedure under `category_for(tools)`, whose values come from the skill
# registry (`files`, `system`, `windows`, `integrations`, `meta`, `general`).
# LOOKUP searches with `guess_category(message)`, which can only ever produce the
# CATEGORY_HINTS keys (`files`, `code`, `web`, `browser`, `calendar`, `system`,
# `desktop`, `memory`, `vision`).
#
# So `windows`, `integrations`, `meta` and `general` were write-only: a learned
# procedure filed under one of them could never be found, because no message is
# ever classified into a category the lookup does not know. The recipe was
# saved, counted, and permanently unreachable.
#
# Mapping rather than adding new categories on purpose: making `windows` its own
# bucket would split window tasks across two — filed under `windows` when they
# ran, searched under `desktop` when asked about — so the same work would
# produce two procedures that never reinforce each other.
TOOL_CATEGORY_ALIASES = {
    "windows": "desktop",
    "integrations": "calendar",
    "mcp": "system",
    "market": "system",
    "forged": "system",
    "meta": "system",
}


def category_for(tool_names) -> str:
    """The category most of these tools belong to.

    Tools are the best evidence available: they are what actually ran, so the
    category reflects what the task turned out to be rather than what the
    wording suggested.

    The result is translated through TOOL_CATEGORY_ALIASES, so it is always a
    category the LOOKUP side can produce. A category only one side knows about
    is a procedure nobody will ever be offered.
    """
    names = [str(t or "").strip() for t in (tool_names or []) if str(t or "").strip()]
    if not names:
        return DEFAULT_CATEGORY
    counts: dict[str, int] = {}
    for name in names:
        category = _skill_category(name)
        if category:
            category = TOOL_CATEGORY_ALIASES.get(category, category)
            counts[category] = counts.get(category, 0) + 1
    if not counts:
        return DEFAULT_CATEGORY
    return max(counts.items(), key=lambda kv: kv[1])[0]


def guess_category(message: str) -> str:
    """Best guess from the wording, for tasks with no tool history.

    A category must lead outright; a tie is reported as `general`. The hint
    lists themselves are kept honest instead of requiring several hits here:
    demanding two got "book a meeting" wrong (only "meeting" appears) and
    "look up the population" wrong (only "look"), which are exactly the cases
    this is meant to catch.
    """
    text_words = words(message)
    if not text_words:
        return DEFAULT_CATEGORY
    hits: list[tuple[str, int]] = []
    for category, hints in CATEGORY_HINTS:
        count = len(text_words & set(hints))
        if count:
            hits.append((category, count))
    if not hits:
        return DEFAULT_CATEGORY
    hits.sort(key=lambda kv: -kv[1])
    if len(hits) > 1 and hits[1][1] == hits[0][1]:
        # Two categories equally implicated: no decision to make.
        return DEFAULT_CATEGORY
    return hits[0][0]


def _sop_text(sop: dict) -> str:
    return " ".join([str(sop.get("title") or ""),
                     str(sop.get("category") or ""),
                     " ".join(str(s) for s in (sop.get("steps") or [])),
                     " ".join(str(t) for t in (sop.get("tools") or []))])


def _stem(word: str) -> str:
    """A crude English stem, enough to join a verb to its -ing/-ed form.

    `_overlap`'s prefix rule cannot bridge a stem change: "rewrite" and
    "rewriting" differ at the seventh character (the final `e` is dropped before
    `ing`), so `"rewriting".startswith("rewrite")` is False and the two were
    treated as unrelated words. That is how "rewrite the config file" matched no
    procedure at all while "overwrite the config file" matched the right one.

    Deliberately not a real stemmer. It handles the two endings that actually
    appear in tasks and recipes — -ing and -ed, including the dropped `e` — and
    leaves everything else alone, because over-stemming merges words that mean
    different things ("adding" and "addiction") and a false merge is worse than
    a missed one.
    """
    w = str(word or "").lower()
    if len(w) > 5 and w.endswith("ing"):
        base = w[:-3]
        # "rewriting" -> "rewrit" -> restoring the `e` gives "rewrite".
        if len(base) > 3 and base[-1] not in "aeiou":
            return base + "e"
        return base
    if len(w) > 4 and w.endswith("ed"):
        base = w[:-2]
        if len(base) > 3 and base[-1] not in "aeiou":
            return base + "e"
        return base
    return w

def _overlap(query: set[str], candidate: set[str]) -> int:
    """How many of the query's words the candidate accounts for.

    A prefix counts as a match, so "overwrite" finds "overwriting" and "read"
    finds "read_file". Four characters is the floor, which is long enough that
    "rea" does not match half the language. Words are also compared through
    `_stem`, so a verb finds its own -ing/-ed form.
    """
    hits = 0
    stems = {_stem(w) for w in candidate}
    for word in query:
        for other in candidate:
            if (word == other
                    or (len(word) >= 4 and other.startswith(word))
                    or (len(other) >= 4 and word.startswith(other))):
                hits += 1
                break
        else:
            # No literal match; try the stem of the query word against the
            # stems of the candidates.
            if len(word) >= 4 and _stem(word) in stems:
                hits += 1
    return hits


def _lexical(query_words: set[str], sop: dict,
             idf: dict[str, float] | None = None) -> float:
    """Share of the query's words the procedure accounts for, weighted.

    Measuring what the query covers, rather than what the candidate contains,
    is what lets a short task find a longer procedure. Title words count for
    most of it, since a procedure's title is its subject.

    A word that names the thing being worked ON is discounted. "write the config
    file again" and "Find a file before creating one" share the word "file" —
    but "file" is the task's OBJECT, not its subject; the task is about writing.
    Counting it at full weight let the procedure whose title happens to name the
    object outscore the one whose tools actually perform the verb, which is how
    a task about overwriting matched a recipe about searching.
    """
    if not query_words:
        return 0.0
    total = len(query_words)

    def weight(word: str) -> float:
        base = idf.get(word, 1.0) if idf else 1.0
        # The object of the task, not its topic. Weighted down hard rather than
        # removed: a task that mentions NO subject is still better matched by a
        # procedure that handles the same kind of thing ("read the file" against
        # a file procedure), so the word is not worthless — it is just very weak
        # evidence about WHICH procedure, which is the question being asked.
        return base * _OBJECT_NOUN_WEIGHT if word in _OBJECT_NOUNS else base

    def coverage(candidate: set[str]) -> float:
        """Sum of weights of the query's words this candidate accounts for."""
        got = 0.0
        for word in query_words:
            for other in candidate:
                if (word == other
                        or (len(word) >= 4 and other.startswith(word))
                        or (len(other) >= 4 and word.startswith(other))):
                    got += weight(word)
                    break
        return got

    denom = sum(weight(w) for w in query_words) or float(total)
    title = coverage(words(sop.get("title") or "")) / denom
    body = coverage(words(
        " ".join(str(s) for s in (sop.get("steps") or [])))) / denom
    tools = coverage(words(
        " ".join(str(t) for t in (sop.get("tools") or [])))) / denom
    return min(0.5 * title + 0.3 * body + 0.2 * tools, 1.0)

# Words that name WHAT a task acts on rather than what it is about. They appear
# in a task as the object ("write the config FILE") and in a procedure's title
# as the thing it handles ("Find a FILE before creating one"), so sharing one is
# much weaker evidence than sharing a verb.
#
# Deliberately a short, closed list of the objects that actually caused a
# mis-match in the seed set, not an attempt at a general taxonomy: `file` and
# `page` are both in seed titles and both decided a match they should not have.
_OBJECT_NOUNS = {
    "file", "files", "document", "documents", "page", "pages", "text",
    "data", "content", "thing", "things", "stuff", "item", "items",
}

# How much an object noun is worth as evidence about WHICH procedure applies.
# Low because the word says what the task touches, not what it is doing; not
# zero because it is still something when no better signal exists.
_OBJECT_NOUN_WEIGHT = 0.2

# Verbs that mean the same job, mapped to one spelling.
#
# No amount of ranking fixes a word that appears in NEITHER the task nor the
# recipe: "rewrite the config file" and "update the config file" both scored 0
# against a procedure about overwriting, because the recipe said "overwriting"
# and neither request used that word. BM25 cannot help either — it weights terms
# that exist, it cannot invent one that does not (see the note about it in
# `backend/memory/bm25.py`, which solves the related but different problem of
# exact-name lookup).
#
# Normalising the verb is the cheap, deterministic answer, and the same trick
# `CATEGORY_HINTS` already uses for nouns (it knows "edit" means code and "book"
# means calendar). Applied at tokenisation so EVERY signal sees the same words —
# lexical, shared-term count, tool affinity and category agreement all agree.
#
# Deliberately narrow: these are the rewrites that actually occur in file and
# document work, not an attempt at a thesaurus. A wrong merge is worse than a
# missed one, so only unambiguous equivalences are listed.
_VERB_SYNONYMS = {
    "rewrite": "overwrite", "rewriting": "overwrite", "overwrite": "overwrite",
    "overwriting": "overwrite", "rewrote": "overwrite", "update": "overwrite",
    "updating": "overwrite", "amend": "overwrite", "amending": "overwrite",
    "modify": "change", "modifying": "change", "change": "change",
    "changing": "change", "edit": "change", "editing": "change",
    "alter": "change", "adjust": "change",
    "remove": "delete", "removing": "delete", "delete": "delete",
    "deleting": "delete", "erase": "delete", "erasing": "delete",
    "make": "create", "making": "create", "create": "create",
    "creating": "create", "generate": "create", "generating": "create",
    "find": "search", "finding": "search", "search": "search",
    "searching": "search", "locate": "search", "look": "search",
    "summarise": "summarize", "summarize": "summarize",
    "summarizing": "summarize", "summarising": "summarize",
    "show": "read", "display": "read", "view": "read",
}

# Removed: `build_idf`. It computed inverse-document-frequency weights over the
# procedures on offer, and an IDF weighting WAS tried in this scorer -- it was
# reverted because it lowered every score (legitimate matches fell under the
# threshold: "what do you know about the user's setup" scored 0.209 against the
# procedure that plainly answers it) and nothing else used it afterwards. Left
# as a note rather than deleted silently, because the idea is reasonable and the
# reason it failed is specific: it rescales every score without telling the
# threshold, so the bar stops meaning what it was calibrated to mean.

# Verbs that mean the task destroys something, so it cannot be the same task as
# a procedure that only reads.
_DESTRUCTIVE_VERBS = {"delete", "remove", "erase", "drop", "wipe", "format",
                      "uninstall", "kill", "clear", "purge", "destroy"}

def _tool_affinity(task: str, sop: dict) -> float:
    """How much the task's verbs agree with this procedure's tools.

    Words alone got this wrong on a case the code had already flagged: "delete a
    file" shares the token "file" with "Find a file before creating one" and won
    on it, while sharing nothing with the procedure that actually covers
    removing something. The verb is what distinguishes them.

    So a matching tool stem is evidence for the procedure, and a DESTRUCTIVE
    verb the procedure has no tool for is evidence against it — the half that
    was missing entirely.
    """
    sop_tools = {str(t).lower() for t in (sop.get("tools") or [])}
    if not sop_tools:
        return 0.0
    task_words = words(task)
    score = 0.0
    for tool in sop_tools:
        stem = tool.split("_")[0]
        if len(stem) < 3 or stem not in task_words:
            continue
        # A tool shared by several procedures in a category says little about
        # WHICH one this is. `list` is in most of them, so sharing it decides
        # nothing; the tool only one procedure has is the informative one.
        #
        # `write` used to be in this set and should not be: it is the task's
        # own VERB in "write the config file again". Discounting it meant a
        # procedure whose tools include `write_file` gained almost nothing for
        # matching what the user actually asked to do, while the procedure whose
        # TITLE happened to contain the file's name gained full marks for the
        # noun. The verb is the part that says which job this is; the noun just
        # says what it is done to.
        score += 0.5 if stem in _UBIQUITOUS_TOOL_STEMS else 1.0
    for verb in _DESTRUCTIVE_VERBS & task_words:
        if not any(verb in t for t in sop_tools):
            # The task destroys something and no tool here does that.
            score -= 2.0
    return max(-2.0, min(2.0, score))

# Tool stems that appear across many procedures, so sharing one is weak
# evidence. Not a general taxonomy — these are the ones that actually overlap in
# the seed set and made two procedures look equally likely. `write` is
# deliberately absent: it is a common VERB in a task, and the procedure that
# performs it is the one being looked for.
_UBIQUITOUS_TOOL_STEMS = {"list", "read", "search"}

def _reliability(sop: dict) -> float:
    """How well this procedure has worked, shrunk for a small sample.

    `uses` and `successes` are recorded by `store.record_use` and were then read
    by nothing — the counter went up and no decision consulted it. A recipe that
    has worked nine times in ten is better evidence than one that worked once,
    and that was information already on hand.

    Shrunk toward 0.5 with a prior weight of 2, so one lucky use gives 0.75
    rather than 1.0, and one-of-two gives 0.5. A procedure has to earn its
    record over several runs before the score moves much: a raw ratio lets a
    single success outrank a well-established recipe.
    """
    uses = int(sop.get("uses") or 0)
    successes = int(sop.get("successes") or 0)
    if uses <= 0:
        return 0.5
    prior = 2.0
    return (successes + prior / 2) / (uses + prior)


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


def _category_agreement(task: str, sop: dict) -> float:
    """Whether the task and the procedure are about the same kind of work.

    This is the synonym bridge the words alone cannot build. `CATEGORY_HINTS`
    already knows that "edit" and "refactor" are code words and that "book" and
    "meeting" are calendar words, and the procedure carries its own category —
    but nothing compared the two. So "edit the parser" scored 0.075 against
    "Change code safely" (it shares only the word "handles") and "book a meeting
    for tuesday" scored 0.000 against the calendar procedure.

    Returns -1, 0 or 1: agreement, silence, or disagreement. Silence is the
    common and correct answer when either side has no category to compare.
    """
    sop_category = str(sop.get("category") or "").strip().lower()
    if not sop_category or sop_category == DEFAULT_CATEGORY:
        return 0.0
    task_category = guess_category(task)
    if task_category == DEFAULT_CATEGORY:
        # The wording said nothing decisive (only ONE hint word, or none).
        # Returning 0 here rather than -1 matters: a task whose category is a
        # guess is not evidence against a procedure.
        return 0.0
    if task_category == sop_category:
        return 1.0
    if TOOL_CATEGORY_ALIASES.get(sop_category, sop_category) == task_category:
        return 1.0
    return -1.0

def combined_score(task: str, sop: dict,
                   idf: dict[str, float] | None = None) -> float:
    """The one number the ranking uses, before the field gate.

    Words decide, because on this data they were right in every case tested —
    including both negatives. The terms that were missing are added on top:

    `tool_affinity` carries the verb signal words cannot see ("delete a file"
    must not win a procedure that only reads), and `reliability` carries the
    record the store had been keeping and nothing had been reading.

    Weights: lexical keeps the majority so existing behaviour is preserved and
    only sharpened. Affinity is scaled small because it is a correction, not a
    ranking of its own; reliability is a small tiebreak between procedures that
    are otherwise equally plausible.
    """
    base = _lexical(words(task), sop, idf)
    affinity = _tool_affinity(task, sop)
    reliability = _reliability(sop)
    category = _category_agreement(task, sop)
    score = (base
             + 0.15 * affinity
             + 0.10 * (reliability - 0.5)
             + 0.30 * category)
    return max(0.0, min(1.0, score))

def shared_terms(task: str, sop: dict) -> int:
    """How many DISTINCT task words the procedure accounts for.

    `_lexical` is a share of the query, which flatters a short question: one
    shared word out of three is 0.33 and clears a 0.25 bar, so
    "how do i change a tyre" matched a procedure about code because of the word
    "change", and "what is 17 times 3" matched a calendar procedure on "time".
    A share cannot tell a coincidence from a match, because with few words a
    coincidence IS a large share.

    Counting distinct words can. One shared word is one coincidence whatever the
    query length; two is a topic.

    Two kinds of word are not counted, because neither says which procedure this
    is. An OBJECT noun ("file", "page") is what the task acts on rather than what
    it is about, and generic filler ("new", "only") is not a topic at all —
    measured on the real mis-match, "Only create a NEW one if nothing suitable
    exists" supplied the word "new", which was the entire difference between the
    right procedure and the wrong one. The same discount is applied in
    `_lexical`, so the two agree about what counts as evidence.
    """
    q = {w for w in words(task)
         if w not in _OBJECT_NOUNS and w not in _FILLER_WORDS}
    if not q:
        return 0
    candidate = words(" ".join([
        str(sop.get("title") or ""),
        " ".join(str(s) for s in (sop.get("steps") or [])),
        " ".join(str(t) for t in (sop.get("tools") or [])),
    ]))
    return _overlap(q, candidate)

# Words that appear in a task or a recipe as ordinary filler and carry no
# evidence about which procedure applies. Short and closed on purpose: these are
# the ones observed to decide a real match, not a general stopword list (the
# tokeniser already removes those — "again" never reaches here).
_FILLER_WORDS = {
    "new", "only", "one", "own", "same", "other", "another", "first",
    "last", "next", "actual", "actually", "suitable", "thing", "stuff",
    "again", "already", "still", "just", "even", "really", "quite",
}

def find_among(task: str, candidates: list[dict],
               limit: int = 1) -> dict | None:
    """The best procedure for a task from a given list, or None.

    Separate from `find()` so the decision can be tested against a known set
    without a store on disk — and so the RULE is in one place. The rule is:

      * score every candidate,
      * refuse unless the winner clears its bar, AND
      * refuse when the runner-up is too close to call.

    The second condition is the one that was missing. A floor alone cannot tell
    a match from a near-miss when the field is crowded, which is how a task
    about deleting won a procedure about creating.
    """
    text = str(task or "").strip()
    if not text or not candidates:
        return None

    # Weights from the field, so a word every procedure shares counts for less
    # than one only this procedure uses.
    scored = []
    for sop in candidates:
        # Category agreement only counts as EVIDENCE when the verb does not
        # contradict it. "delete a file" is filed under `files`, the same as a
        # procedure about creating files — but one destroys and the other
        # creates, and the shared category must not be allowed to paper over
        # that. `_tool_affinity` is what knows the difference, so it decides
        # whether the agreement is usable.
        affinity = _tool_affinity(text, sop)
        agrees = _category_agreement(text, sop) > 0 and affinity >= 0
        # The lexical share is kept alongside the combined score because the
        # two are different scales. `combined_score` adds up to +0.30 for a
        # matching category, +0.15 for a matching verb and +0.10 for a good
        # record, and the stored threshold was calibrated against WORDS ALONE —
        # so applying it to the combined number let a procedure clear the bar
        # on its category while sharing almost no words with the task.
        scored.append({"sop": sop, "score": combined_score(text, sop),
                       "lexical": _lexical(words(text), sop),
                       "shared": shared_terms(text, sop),
                       "affinity": affinity,
                       "category_agrees": agrees})
    scored.sort(key=lambda item: (-item["score"], -item["shared"]))

    top = scored[0]

    # Two independent kinds of evidence, either of which is enough:
    #   * two or more distinct words in common, or
    #   * the same category, with a verb that does not contradict it.
    # A single shared word alone is a coincidence, and a share cannot see that:
    # with a three-word question one coincidence is 0.33 and clears the 0.25
    # bar, which is how "how do i change a tyre" matched a procedure about
    # editing code on the word "change".
    #
    # This runs BEFORE the threshold on purpose. It used to run after, so a
    # candidate that satisfied the category evidence but scored below the bar
    # was refused by the floor before the rule that would have accepted it was
    # reached — the two conditions were written as alternatives but behaved as
    # a sequence.
    enough_words = top["shared"] >= MIN_SHARED_TERMS
    enough_category = bool(top.get("category_agrees"))
    # A verb that maps onto a tool this procedure alone has is evidence in its
    # own right, and the two conditions above both miss it. "search the web for
    # the release notes" shares one word and names no category, but `search` is
    # the verb and the procedure's tools are `web_search` and `web_fetch` — the
    # only procedure in the set those verbs point at. Measured against ten
    # negatives, none reached 1.0: a coincidence is a shared NOUN, and this
    # signal cannot fire on one. Negative scores mean the opposite (a destructive
    # verb with no tool for it), so the bar is one-sided.
    enough_verb = top.get("affinity", 0.0) >= 1.0
    if not (enough_words or enough_category or enough_verb):
        log.debug("Refusing a one-word procedure match: %r (shared=%d)",
                  text[:40], top["shared"])
        return None

    # The floor then judges the combined score, which is the scale the stored
    # threshold was tuned against in practice.
    #
    # A separate floor on the lexical SHARE was tried and reverted: a short
    # question legitimately has a low share even when the match is right
    # ("book a meeting for tuesday" scores 0.000 on words alone because every
    # word it uses is a category hint, and "rewrite the config file" scores
    # 0.164 against the procedure that is plainly correct). The share measures
    # coverage of a short query, so a fixed bar on it refuses correct matches.
    if top["score"] < threshold("lexical"):
        log.debug("Refusing a weak procedure match: %r (%.3f)",
                  text[:40], top["score"])
        return None

    # The winner must beat the field. When two procedures are this close, the
    # honest answer is neither: a wrong recipe is worse than no recipe, and the
    # model will follow whichever it is handed.
    if len(scored) > 1:
        runner_up = scored[1]["score"]
        if (top["score"] - runner_up) < AMBIGUITY_MARGIN and runner_up > 0:
            log.debug("Refusing an ambiguous procedure match: %.3f vs %.3f",
                      top["score"], runner_up)
            return None
    return {"sop": top["sop"], "score": round(top["score"], 4)}

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

    The method matters: an embedding cosine and a word-overlap share are not the
    same units, so they cannot be compared against one number.

    Both are computed and the LEXICAL answer wins when it clears its own merge
    bar. That is not a preference for words, it is a correction: `score()`
    returns the embedding whenever one is available, and an embedding cosine
    cannot clear the merge bar even for a string compared against itself —
    measured on this machine, an identical title scored 0.7665 against a bar of
    0.82, while the lexical score for the same pair was 0.80 against a bar of
    0.55.

    The consequence was that merging never happened while embeddings were
    working, so every repeat of a task created another near-identical procedure
    — the exact duplication the merge exists to prevent, and the reason a
    long-lived store fills with clones instead of sharpening one recipe.

    Merging is a decision about near-identity, and words answer it well: shared
    vocabulary is strong evidence two runs did the same thing. `best()` still
    ranks with the embedding, because "is this relevant" is a different question
    with a different bar.
    """
    lexical = _lexical(words(task), sop)
    if lexical >= threshold("lexical", "merge"):
        return lexical, "lexical"

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
    """The bar a LEXICAL score must clear.

    It is a share of the query's words the procedure accounts for, and it is
    applied to that share alone — not to `combined_score`, which adds a category
    term (+0.30), a verb term (+0.15) and a reliability term (+0.10) on top.
    Judging the combined number with this bar let a procedure clear it on its
    category while sharing almost no words with the task.

    The fallback here is 0.30, but the live value comes from
    `sop.lexical_min_similarity` in settings and is currently **0.25** — read it
    rather than trusting the default, and note that a docstring once claimed
    0.30 as though it were the value in use.

    Embedding scores are deliberately NOT gated by an absolute number here. The
    values this used to return (0.35 to offer, 0.82 to merge) were calibrated
    against vectors that turned out to be hashed n-grams, because the ONNX
    embedder was silently falling back to hashing. Real sentence embeddings do
    not spread over 0..1: measured on this machine a matching procedure scores
    0.97 and an unrelated one 0.94, a gap of about 0.04. No absolute floor in
    that range means "related" - it either admits everything or refuses
    everything. An embedding match is judged by `gate_embedding()` instead,
    which looks at how the winner compares to the field. The merge bar stays,
    because deciding two procedures are the same procedure is a different
    question that genuinely warrants a high number.
    """
    from backend.sop import store

    if method == "embedding":
        if kind != "merge":
            return 0.0
        key, fallback = "merge_similarity", 0.82
    else:
        key, fallback = ("lexical_merge_similarity", 0.55) if kind == "merge" \
            else ("lexical_min_similarity", 0.30)
    try:
        return float(store._setting(key, fallback) or fallback)
    except (TypeError, ValueError):
        return fallback

# How far above the field an embedding match must stand to be offered.
#
# An absolute cosine cannot do this job — measured on this machine, every
# candidate against every task scored 0.86-0.95, and an unrelated task
# ("how tall is mount everest") scored 0.872 against "Search then read the
# source". There is no absolute floor that means "related".
#
# A relative margin was tried too, and it also failed: a correct match led the
# runner-up by 0.013 while two non-matches led by 0.017 and 0.009. The bands
# overlap, so no margin separates them. That is a statement about the signal,
# not about the number — the vectors this model produces do not carry enough
# task-to-procedure discrimination to make this decision.
#
# So the embedding path no longer decides on its own. See `gate_embedding`.
EMBEDDING_MARGIN = 0.03
EMBEDDING_FLOOR = 0.90

# How far the winner must lead the runner-up for a match to be offered at all.
#
# A floor answers "is this relevant?" but not "is this THE one?". When two
# procedures both clear the bar and sit this close together, the evidence does
# not distinguish them, and handing the model the higher of two near-equal
# recipes is how a confident wrong answer is produced. Refusing costs nothing —
# no procedure is the normal case for a task done the first time.
AMBIGUITY_MARGIN = 0.05

# A procedure is only offered when the evidence for it is more than one shared
# word. Two independent kinds of evidence count:
#
#   * two or more DISTINCT words in common, or
#   * the task and the procedure are about the same CATEGORY — and the verb does
#     not contradict it (a task that deletes never "agrees" with a procedure
#     that only reads, however well the nouns line up).
#
# The share-based scorer cannot make this call alone: with a three-word question
# one coincidence is 0.33 and clears the 0.25 bar, so "how do i change a tyre"
# matched a procedure about editing code on the single word "change". Counting
# words, and letting agreement substitute for the second word, is what turns
# "a word in common" into "about the same thing" — which is exactly how
# "book a meeting" reaches the calendar procedure without sharing a single word
# with it.
MIN_SHARED_TERMS = 2

def gate_embedding(scored: list[dict], task: str,
                   candidates: list[dict]) -> bool:
    """Whether to offer an embedding match.

    The embedding decides *nothing* on its own, because measurement showed it
    cannot: it ranked a "delete a file" procedure above a "read a file" one for
    a read-a-file task, and it found a confident 0.87 match for "what is the
    capital of Peru", where the honest answer is no procedure at all.

    What it is kept for is the job words genuinely cannot do — recognising a
    paraphrase with no shared vocabulary. So an embedding match is offered only
    when it *also* clears a relative margin over the field, which is what an
    unrelated task fails to do. Where words do have something to say, the
    lexical scorer decides, because on this data it was right in every case
    tested including both negatives.
    """
    if not scored:
        return False
    top = scored[0]
    if top.get("method") == "lexical":
        return float(top["score"]) >= threshold("lexical")

    # Words got nothing; this is where only an embedding can help. Require it to
    # beat the field clearly, because a wrong procedure is worse than none.
    score = float(top["score"])
    if len(scored) == 1:
        return score >= EMBEDDING_FLOOR
    return (score - float(scored[1]["score"])) >= EMBEDDING_MARGIN

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

    # One decision, in `find_among`, so the store path and the tested path
    # cannot drift apart.
    best = find_among(text, candidates, limit=limit)
    if best is None:
        return []
    return [best]


def best(task: str, category: str | None = None) -> dict | None:
    """The single best procedure worth offering, or None.

    Delegates to `find_among`, so there is ONE rule. This function used to rank
    with `_lexical` on its own, which meant the live path (this is what
    `build_sop_context` calls into the system prompt) and the path the tests
    exercised had drifted apart: the evidence rule, the ambiguity margin, tool
    affinity and the reliability term were all present in `find_among` and
    absent here, so the running app never saw any of them.

    Two rankers for one question will always diverge, and the untested one is
    the one that ships. `find()` was already routed through `find_among` for
    that reason; this closes the same hole.

    The embedding is still NOT consulted, and that is a deliberate decision kept
    from the version this replaces. Measured on this machine its margins are
    *inverted*: the unrelated task "what is the capital of Peru" led its
    runner-up by 0.126, while the correct match for "read the existing file
    before I overwrite it" led by only 0.008. An embedding match that is more
    confident when it is wrong cannot be gated, and offering a procedure for the
    wrong task is worse than offering none. So a paraphrase with no shared words
    is not matched — a real capability lost, stated rather than hidden behind a
    constant that does not work.
    """
    from backend.sop import store

    text = str(task or "").strip()
    if not text:
        return None
    candidates = store.list_all(category) if category else store.list_all()
    if not candidates:
        return None

    hit = find_among(text, candidates, limit=1)
    if hit is None:
        return None
    return {"sop": hit["sop"], "score": hit["score"], "method": "lexical"}


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

    # Offering counts as a use.
    #
    # It did not, and that is why every procedure in a long-lived store read
    # "0/0 successful": `record_use` was only reached on the LEARNING path, when
    # a repeat run merged into an existing recipe. A seed that was correctly
    # matched and injected twenty times still showed zero. Two consequences, both
    # bad — the block told the model "0/0 successful", which argues AGAINST
    # following it, and there was no way to see whether lookup ever fired at all,
    # or which seeds are dead weight.
    #
    # Counted here rather than in `best()` because this is the point at which a
    # procedure is actually handed to the model. A lookup that is computed and
    # then discarded has not been used.
    try:
        store.record_use(sop.get("id"), success=True)
        # Reflect the increment in what is about to be printed. `record_use`
        # wrote to disk; `sop` is the dict read before it, so without this the
        # block would still say "0/1 successful" on the very turn it counted.
        sop["uses"] = int(sop.get("uses") or 0) + 1
        sop["successes"] = int(sop.get("successes") or 0) + 1
    except Exception as e:  # noqa: BLE001
        log.debug("could not record the procedure offer: %s", e)

    lines = [
        "[Procedure]",
        f"A procedure for '{sop.get('category')}' tasks worked before "
        f"({sop.get('successes') or 0}/{sop.get('uses') or 0} successful): "
        f"{sop.get('title')}.",
    ]
    # The reason, when one was recorded. It is the part that carries WHY the
    # route was chosen — a step list alone reads as arbitrary ritual, and a
    # procedure followed without understanding is one applied to the wrong task.
    reason = str(sop.get("reason") or "").strip()
    if reason:
        lines.append(f"Because: {reason}")
    lines += [
        *(f"{i}. {step}" for i, step in enumerate(steps, 1)),
        "Follow it when it fits. If the task differs, say so and do what the "
        "task actually needs.",
    ]
    return "\n".join(lines)
