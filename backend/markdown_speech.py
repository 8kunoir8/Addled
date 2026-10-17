"""Turn a markdown reply into the words a voice should say.

The model writes markdown - `**Full path:**`, `- bullets`, `# headings`, and
fenced code - and the chat page renders it. TTS was handed the reply string
untouched, so the character read the markup aloud: "asterisk asterisk full
path colon asterisk asterisk". The user hears the formatting instead of the
sentence.

This is the Python half of `dashboard/src/lib/markdown.ts`. The two exist
because the surfaces are in different languages, but they implement the same
grammar deliberately, and `scripts/check_markdown_speech.py` runs the same
corpus through both and fails if they disagree - a voice that speaks a
different reply from the one on screen is its own bug, and an easy one to
introduce by editing only one side.

Not a general markdown parser, and deliberately so: it handles what the model
actually emits, which was measured against the real chat store (bold, inline
code, fenced blocks, headings, bullets, links, rules). Anything it does not
recognise passes through as text rather than being deleted - losing a sentence
to a formatting rule is worse than reading a stray asterisk.
"""

from __future__ import annotations

import re

# Order matters, and it is the same order as the TypeScript: code first,
# because `**` inside backticks is code, not emphasis; bold before italic,
# because `**` is longer than `*` and a lazy italic rule would consume half of
# a bold pair and leave a stray asterisk behind.
_CODE = re.compile(r"`([^`]+)`")
_FENCE = re.compile(r"^\s*```")
_BOLD_STAR = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_UNDER = re.compile(r"__([^_]+)__")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+")
_RULE = re.compile(r"^\s*(---|\*\*\*|___)\s*$")
_WS = re.compile(r"[ \t]+")

# The tone tag the character opens a reply with, e.g. "[happy]". It is the
# app's protocol, not markdown: spoken, it becomes "open bracket happy close
# bracket", and it is not part of the sentence.
_EMOTION = re.compile(
    r"^\s*\[(neutral|happy|concern|confusion|thoughtful|content|excited|sad|"
    r"angry|curious|calm|playful)\]\s*",
    re.IGNORECASE,
)


def _strip_inline(text: str) -> str:
    """Remove inline markup, keeping the words.

    Code spans are protected first - their contents are literal, and a `*` in
    them is an operator rather than emphasis. They are put back afterwards so
    `Length` or `35%` still reads inside the sentence.
    """
    held: list[str] = []

    def hold(match: re.Match) -> str:
        held.append(match.group(1))
        return f"\x00{len(held) - 1}\x00"

    protected = _CODE.sub(hold, text)
    protected = _LINK.sub(r"\1", protected)      # link text, not the URL
    protected = _BOLD_STAR.sub(r"\1", protected)
    protected = _BOLD_UNDER.sub(r"\1", protected)
    protected = _ITALIC.sub(r"\1", protected)

    for i, value in enumerate(held):
        protected = protected.replace(f"\x00{i}\x00", value)
    return protected


def strip_emotion(text: str) -> str:
    """Drop a leading `[happy]`-style tone tag."""
    return _EMOTION.sub("", str(text or ""), count=1)


def markdown_to_speech(text: str) -> str:
    """The words to speak, from a reply written in markdown.

    Fenced code blocks are DROPPED rather than read. A shell command or a
    PowerShell pipeline read aloud is noise the user cannot follow, and it is
    on screen if they want it. Inline code is kept: `Length` or `35%` in the
    middle of a sentence is part of that sentence.
    """
    source = strip_emotion(str(text or ""))
    if not source.strip():
        return ""

    out: list[str] = []
    in_fence = False

    for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        line = _RULE.sub("", line)
        line = _HEADING.sub("", line)
        # A bullet is a pause, not a character. Keeping the words and dropping
        # the dash is what makes a list read as a list.
        line = _BULLET.sub("", line)
        line = _ORDERED.sub("", line)
        line = _strip_inline(line)
        line = _WS.sub(" ", line).strip()

        if line:
            out.append(line)

    # Each line is a fragment; a stop between them gives the engine a breath.
    #
    # Only append a stop when the line does not already end in punctuation, and
    # only tidy the ". ." that appending can create - never a "..." the author
    # wrote. Collapsing every run of stops edited the reply instead of
    # formatting it: `ReadAllBytes(...)` became `ReadAllBytes(..)` and
    # `sebentar...` became `sebentar.`. The TypeScript half had the same bug,
    # which is why the parity check compares them rather than each being
    # trusted on its own.
    joined = " ".join(
        line if re.search(r"[.!?…,;:—-]$", line) else line + "."
        for line in out
    )
    return re.sub(r"\.\s+\.", ".", joined).strip()
