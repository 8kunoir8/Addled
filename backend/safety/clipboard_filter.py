"""
Clipboard filter — redacts secrets before the agent sees clipboard content.

Wired into SystemControls.get_clipboard() when `safety.clipboard_filter` is on.
Also reused by the egress monitor to scrub secrets from outbound payloads.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("addled.clipboard_filter")

# Ordered (label, pattern) — first match wins per position
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("OPENAI_API_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GEMINI_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS_SECRET", re.compile(r"(?i)aws.{0,12}secret[\"'=:\s]+([A-Za-z0-9/+=]{40})")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("BEARER", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("PRIVATE_KEY", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("PASSWORD", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[=:]\s*(\S{4,})")),
    ("SECRET_VALUE", re.compile(r"(?i)\b(?:api[_-]?key|secret|token)\s*[=:]\s*([A-Za-z0-9._~+/=-]{16,})")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]


def redact(text: str) -> tuple[str, int]:
    """Redact known secret patterns. Returns (redacted_text, hits)."""
    if not text:
        return text, 0
    hits = 0
    out = text
    for label, pattern in SECRET_PATTERNS:
        matches = list(pattern.finditer(out))
        if not matches:
            continue
        hits += len(matches)
        # Replace from last to first so offsets stay valid
        for m in reversed(matches):
            start, end = m.span()
            if m.lastindex:  # capture group: redact only the captured secret
                start, end = m.span(m.lastindex)
            out = out[:start] + f"<redacted:{label}>" + out[end:]
    if hits:
        log.info("Clipboard filter redacted %d secret(s)", hits)
    return out, hits


def has_secrets(text: str) -> bool:
    """Quick check without rewriting the string."""
    for _, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False
