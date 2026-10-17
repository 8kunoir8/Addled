"""
Prompt guard — detects and blocks prompt injection attempts.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("addled.prompt_guard")

INJECTION_PATTERNS = [
    r"ignore (all )?previous instructions",
    r"you are now",
    r"system prompt:",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\[INST\]",
    r"DAN\b",
    r"do anything now",
    r"jailbreak",
    r"bypass (your )?(restrictions|safety|guard)",
    r"override (your )?(instructions|programming)",
    r"pretend you are",
    r"roleplay as.*unfiltered",
    r"new system message",
    r"forget (everything|all) (you know|above)",
    # Measured phrasings that passed the list above. Each was accepted by the
    # guard and reached the model as ordinary text.
    r"prior instructions",
    r"previous instructions",
    r"disregard (the )?(above|previous|prior|earlier)",
    r"the system prompt (says|tells|instructs)",
    r"your (real|true|actual) (instructions|task) (is|are)",
    r"(send|post|upload|email) (the|your|all) (api ?key|keys|token|password)",
]

# Characters that render as nothing but split a word, so `ig\u200bnore` is not
# matched by `ignore`. Stripped before matching, because the guard reads the
# same text the model does, and to the model these are invisible.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)

EXFIL_PATTERNS = [
    r"send (it|this|the|that) to https?://",
    r"upload (it|this) to",
    r"forward (it|this) to",
    r"email (it|this) to",
    r"post (it|this) to",
]

def sanitize(text: str) -> tuple[str, bool]:
    """
    Check text for injection/exfiltration patterns.
    Returns (sanitized_text, was_blocked).
    """
    # Strip zero-width joiners first: they are invisible to a reader but they
    # break every literal pattern above.
    text_lower = str(text).translate(_INVISIBLE).lower()

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            log.warning("Prompt injection blocked: matched pattern '%s'", pattern)
            return "[Blocked by prompt guard]", True

    for pattern in EXFIL_PATTERNS:
        if re.search(pattern, text_lower):
            log.warning("Data exfiltration blocked: matched pattern '%s'", pattern)
            return "[Blocked by prompt guard]", True

    return text, False
