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
]

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
    text_lower = text.lower()

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            log.warning("Prompt injection blocked: matched pattern '%s'", pattern)
            return "[Blocked by prompt guard]", True

    for pattern in EXFIL_PATTERNS:
        if re.search(pattern, text_lower):
            log.warning("Data exfiltration blocked: matched pattern '%s'", pattern)
            return "[Blocked by prompt guard]", True

    return text, False
