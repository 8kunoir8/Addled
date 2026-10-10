"""Decide which emotion a moment calls for.

`mood.py` owns the slow background feeling: valence and energy, decayed over
minutes, driven by eight coarse events. That is the right shape for a mood and
the wrong shape for an expression. "task_success" nudges valence by +0.08
whether the user just said "this is brilliant" or the assistant merely finished
reading a file, so a mood alone cannot tell joy from satisfaction.

This module is the foreground decision: given a specific moment — a chat turn,
the user clicking the character, an observation the assistant just made — pick
an emotion to show.

Two ways in, following the two designs that real projects actually ship:

1. `parse_tag` reads an emotion the model already chose. The model is handed the
   emotion vocabulary in its system prompt (see `emotion_menu`) and annotates
   its own reply with a tag like `[joy]`. Its language understanding has already
   read the conversation, so this costs no extra call and no extra model. This
   is how Open-LLM-VTuber works, and it is the reason it needs no
   emotion-detection model at all.

2. `classify_text` is the offline fallback. A weighted emoji/keyword/punctuation
   score with an argmax, in the manner of prometheus-avatar's EmotionAnalyzer.
   It is shallow on purpose: it exists so that a local model with no tag
   support, or a reply that forgot its tag, still produces something sensible
   rather than nothing.

Neither path ever raises and neither returns a name outside
`emotions.emotion_names()`, because the result is handed to the 30fps paint
loop.
"""

from __future__ import annotations

import re

from backend.character.emotions import resolve_emotion

# ---------------------------------------------------------------------------
# 1. The tag the model writes
# ---------------------------------------------------------------------------

# A tag is a bare emotion word in square brackets. Deliberately strict: it must
# be one of the words we advertise, matched case-insensitively, and it must not
# contain whitespace. A loose pattern is dangerous here because square brackets
# are ordinary punctuation in a chat reply — "[see the docs]" or a markdown
# link would otherwise be read as an emotion and silently swallowed.
_TAG_RE = re.compile(r"\[([a-z_]{2,20})\]", re.IGNORECASE)


def emotion_menu(names: list[str] | None = None) -> str:
    """The tag list to put in a system prompt, e.g. ``[joy] [sadness] ...``.

    Handed to the model verbatim so it can only choose words the avatar can
    actually draw. A model told to "express an emotion" invents synonyms;
    a model shown the exact vocabulary reuses it.
    """
    from backend.character.emotions import emotion_names

    words = names if names is not None else emotion_names()
    return " ".join(f"[{w}]" for w in words)


def _is_emotion_word(word: str) -> bool:
    """Whether a bracketed word names an emotion the avatar can draw.

    Checks the aliases as well as the canonical names, so a model that writes
    the natural `[sad]` is understood rather than ignored. It deliberately does
    NOT go through `resolve_emotion`, which answers NEUTRAL for anything
    unknown — that would make every bracket in every reply look like a valid
    tag, and "[see the docs]" would be read as an emotion and deleted.
    """
    from backend.character.emotions import EMOTION_ALIASES, EMOTIONS

    key = word.strip().lower()
    return key in EMOTIONS or key in EMOTION_ALIASES


def parse_tag(text: str) -> tuple[str, str]:
    """Split an emotion tag out of a reply.

    Returns ``(emotion_name, remaining_text)``. When there is no usable tag the
    emotion is ``"neutral"`` and the text comes back untouched.
    """
    if not text:
        return "neutral", text or ""

    found = "neutral"
    for match in _TAG_RE.finditer(text):
        word = match.group(1).strip().lower()
        # Only a word we can draw counts. Anything else is punctuation that
        # happened to be bracketed, and must stay in the reply.
        if _is_emotion_word(word):
            found = resolve_emotion(word).name
            break

    # Strip every bracket-tag that names a drawable emotion, so no tag is ever
    # spoken aloud or shown. Bracketed text that is NOT an emotion is left
    # exactly where it was.
    def _drop(match: re.Match) -> str:
        return "" if _is_emotion_word(match.group(1)) else match.group(0)

    cleaned = _TAG_RE.sub(_drop, text)
    # The tag usually sits at the front, so removing it leaves a leading space.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return found, cleaned


# ---------------------------------------------------------------------------
# 2. The offline fallback
# ---------------------------------------------------------------------------

# Each entry is (emotion, weight, words). Words are matched as whole words so
# "great" does not fire inside "greatest hits" style false positives, and the
# match is on a lowercased copy.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("joy", ("thank", "thanks", "thank you", "appreciate", "love it",
             "perfect", "brilliant", "awesome", "amazing", "wonderful",
             "delighted", "yay", "hooray", "excellent")),
    ("happy", ("good", "great", "nice", "glad", "happy", "pleased",
               "works", "worked", "done", "fixed")),
    ("sadness", ("sorry", "unfortunately", "sad", "gutted", "devastated",
                 "upset", "miss", "lost", "gone", "regret")),
    ("anger", ("angry", "furious", "unacceptable", "ridiculous", "hate",
               "annoyed", "frustrating", "frustrated", "why did you")),
    ("fear", ("worried", "worry", "scared", "afraid", "danger", "risky",
              "unsafe", "might break", "could fail")),
    ("confusion", ("confused", "unclear", "not sure", "don't understand",
                   "dont understand", "ambiguous", "hmm", "which one")),
    ("concern", ("careful", "caution", "warning", "issue", "problem",
                 "trouble", "are you sure")),
    ("pride", ("nailed it", "accomplished", "completed", "shipped",
               "finished", "built", "achieved")),
    ("sleepy", ("tired", "exhausted", "sleepy", "late", "bed", "rest")),
    ("excitement", ("can't wait", "cant wait", "exciting", "let's go",
                    "lets go", "finally")),
    ("boredom", ("boring", "bored", "nothing to do", "dull")),
)

# Emoji carry more per glyph than a word does, so they weigh more. Grouped by
_EMOJI: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("joy", ("\U0001F60A", "\U0001F604", "\U0001F602", "\U0001F389",
             "\u2764", "\U0001F60D", "\U0001F44D", "\U0001F64C")),
    ("sadness", ("\U0001F622", "\U0001F62D", "\U0001F494", "\U0001F614")),
    ("anger", ("\U0001F620", "\U0001F621", "\U0001F92C", "\U0001F4A2")),
    ("surprise", ("\U0001F632", "\U0001F62E", "\U0001F631", "\U0001F633")),
    ("confusion", ("\U0001F615", "\U0001F914", "\U0001F643")),
    ("sleepy", ("\U0001F634", "\U0001F62A", "\U0001F4A4")),
)

_WEIGHT_EMOJI = 0.5
_WEIGHT_WORD = 0.3

_PUNCTUATION_ONLY = frozenset({"thoughtful", "confusion", "joy", "surprise"})


def classify_text(text: str) -> tuple[str, float, list[str]]:
    """Score a piece of text into an emotion.

    Returns ``(emotion, confidence, triggers)``. `triggers` names what fired, so
    a wrong answer can be explained rather than merely observed — the same
    contract prometheus-avatar's analyzer returns, and the reason it is
    debuggable at all.
    """
    if not text:
        return "neutral", 0.0, []

    low = text.lower()
    scores: dict[str, float] = {}
    # Which emotions a WORD or EMOJI argued for. This is the set that decides
    # whether there is any real evidence: punctuation may add to a score but
    # must never create a candidate on its own, or an ordinary question mark
    # would put an expression on the character's face.
    grounded: set[str] = set()
    triggers: list[str] = []

    for emotion, glyphs in _EMOJI:
        hits = [g for g in glyphs if g in text]
        if hits:
            scores[emotion] = scores.get(emotion, 0.0) + _WEIGHT_EMOJI * len(hits)
            grounded.add(emotion)
            triggers.extend(hits)

    for emotion, words in _KEYWORDS:
        hits = [w for w in words if w in low]
        if hits:
            scores[emotion] = scores.get(emotion, 0.0) + _WEIGHT_WORD * len(hits)
            grounded.add(emotion)
            triggers.extend(hits)

    # Punctuation. Weighted below a single word, so it can only sharpen a
    # reading that a word already supports, never invent one.
    if "!!" in text:
        scores["surprise"] = scores.get("surprise", 0.0) + 0.3
        triggers.append("!!")
    elif "!" in text:
        scores["joy"] = scores.get("joy", 0.0) + 0.15
        triggers.append("!")
    if "?" in text:
        scores["confusion"] = scores.get("confusion", 0.0) + 0.2
        triggers.append("?")
    if "..." in text:
        scores["thoughtful"] = scores.get("thoughtful", 0.0) + 0.15
        triggers.append("...")

    # Shouting. Needs enough letters to be meaningful: "OK" is not anger. This
    # counts as evidence in its own right — a shout is a deliberate, readable
    # signal in a way a question mark is not — so it must clear the same bar a
    # word does. At a smaller weight "WHAT WERE YOU THINKING" scored 0.2 and
    # was thrown away as noise, which is the opposite of what shouting means.
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 8:
        caps = sum(1 for c in letters if c.isupper()) / len(letters)
        if caps > 0.6:
            scores["anger"] = scores.get("anger", 0.0) + _WEIGHT_WORD
            grounded.add("anger")
            triggers.append("SHOUTING")
            # Surprise rides along but is NOT grounded by shouting alone, so a
            # shout cannot come out as surprise — only as anger, or as anger
            # reinforced by words that were already there.
            scores["surprise"] = scores.get("surprise", 0.0) + 0.15

    # An emotion qualifies only if a word, an emoji, or shouting argued for it,
    # and only if that argument reached a full word's weight. The `grounded`
    # test runs first because it is the load-bearing one: without it "Are you
    # sure?" reads as confusion on the strength of its question mark alone.
    candidates = {name: scores[name] for name in grounded
                  if scores.get(name, 0.0) >= _WEIGHT_WORD}
    if not candidates:
        return "neutral", 0.0, triggers

    # Ties break toward the first-seen emotion in the table order above, which
    # is stable. `max` on a dict would otherwise depend on insertion order in a
    # way that is easy to break from a distance.
    name, score = max(candidates.items(), key=lambda kv: kv[1])
    return resolve_emotion(name).name, min(1.0, score), triggers


# ---------------------------------------------------------------------------
# 3. Deciding, given a moment
# ---------------------------------------------------------------------------

def decide(
    text: str,
    *,
    allow_tag: bool = True,
    allow_classifier: bool = True,
) -> tuple[str, str]:
    """The emotion for a reply, and the reply with any tag removed.

    The tag wins when present: the model read the conversation and this reads a
    string, so the model's answer is the better one. The classifier is consulted
    only when there is no tag to trust.
    """
    if allow_tag:
        emotion, cleaned = parse_tag(text)
        if emotion != "neutral":
            return emotion, cleaned
        if not allow_classifier:
            return "neutral", cleaned
        guess, confidence, _ = classify_text(cleaned)
        # The classifier is a fallback, so only take it when it is actually
        # confident. A weak guess would put an expression on a neutral turn and
        # make the character look like it is reacting to nothing.
        if confidence >= _WEIGHT_WORD:
            return guess, cleaned
        return "neutral", cleaned

    if not allow_classifier:
        return "neutral", text
    guess, confidence, _ = classify_text(text)
    if confidence >= _WEIGHT_WORD:
        return guess, text
    return "neutral", text
