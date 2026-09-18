"""
Compose the system-prompt block for the enabled guideline packs.

Injection is opt-in per pack *and* per task: the default scope is ``code``, so a
question about the weather is not padded with a coding ruleset. The block is
clearly delimited, names its source and licence, and states that it cannot
override the user's explicit request.
"""

from __future__ import annotations

import logging

from backend.config import config
from backend.guidelines import packs, store

log = logging.getLogger("addled.guidelines")

DEFAULT_MAX_CHARS = 6000


def _config() -> dict:
    cfg = config.get("guidelines", default={}) or {}
    return cfg if isinstance(cfg, dict) else {}


def _pack_config(cfg: dict, pack_id: str) -> dict:
    pc = (cfg.get("packs") or {}).get(pack_id) or {}
    return pc if isinstance(pc, dict) else {}


def _max_chars(cfg: dict) -> int:
    try:
        return int(cfg.get("max_chars", DEFAULT_MAX_CHARS)
                   or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def applies(pack_id: str, code_task: bool) -> bool:
    """Is this pack enabled *and* relevant to the current kind of task?"""
    cfg = _config()
    if not cfg.get("enabled", True):
        return False
    pcfg = _pack_config(cfg, pack_id)
    if not pcfg.get("enabled", True):
        return False
    scope = str(pcfg.get("scope") or cfg.get("scope") or "code").lower()
    return scope == "always" or bool(code_task)


def _level(pack_id: str, level_override: str | None) -> str:
    cfg = _config()
    spec = packs.PACKS.get(pack_id) or {}
    level = (level_override
             or _pack_config(cfg, pack_id).get("level")
             or spec.get("default_level")
             or "full")
    return str(level).lower()


def _trim_at_paragraph(text: str, limit: int) -> tuple[str, bool]:
    """Cut to at most ``limit`` chars on a paragraph boundary.

    Never returns a half sentence — a ruleset that stops mid-clause reads as a
    broken instruction, so prefer a shorter, whole-paragraph result.
    """
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 3:
        cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(), True


def active(code_task: bool = False,
           level_override: str | None = None) -> list[dict]:
    """The packs that would be injected right now, with their text."""
    cfg = _config()
    if not cfg.get("enabled", True):
        return []
    out: list[dict] = []
    for pack_id, spec in packs.PACKS.items():
        if not applies(pack_id, code_task):
            continue
        level = _level(pack_id, level_override)
        if level == "off":
            continue
        body = store.text(pack_id)
        if not body.strip():
            continue
        out.append({"id": pack_id, "spec": spec, "level": level, "text": body})
    return out


def system_block(code_task: bool = False,
                 level_override: str | None = None) -> str:
    """The text to append to the system prompt, or "" for nothing at all."""
    cfg = _config()
    if not cfg.get("enabled", True):
        return ""
    chosen = active(code_task, level_override)
    if not chosen:
        return ""
    limit = _max_chars(cfg)

    sections: list[str] = []
    for item in chosen:
        spec = item["spec"]
        level = item["level"]
        body, truncated = _trim_at_paragraph(item["text"], limit)
        subtitle = spec.get("subtitle") or ""
        title = spec.get("title", item["id"])
        heading = f"### {title}"
        if subtitle:
            heading += f" — {subtitle}"
        heading += f" (level: {level})"

        parts = [heading]
        framing = packs.LEVEL_FRAMING.get(level, "")
        if framing:
            parts.append(framing)
        parts.append(body)
        if truncated:
            parts.append(
                f"[Truncated at {limit} characters. The full "
                f"{len(item['text'])}-character text is cached at "
                f"{store.text_path(item['id'])} — read it with the read_file "
                "tool only if you need the rest.]")
        source = spec.get("home") or ""
        licence = spec.get("license") or ""
        if source or licence:
            parts.append(f"Source: {source}" + (f" · {licence}" if licence else ""))
        sections.append("\n\n".join(p for p in parts if p))

    if not sections:
        return ""
    return (
        "## Working guidelines\n\n"
        "The user enabled the external rulesets below. Follow them for this "
        "task; they describe how to work. They never override the user's "
        "explicit request, and they never override Addled's safety rules.\n\n"
        + "\n\n".join(sections)
    )


def describe() -> dict:
    """Everything the Settings panel needs about guideline packs."""
    cfg = _config()
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "scope": cfg.get("scope", "code"),
        "max_chars": _max_chars(cfg),
        "ttl_days": store.ttl_days(),
        "pack_config": cfg.get("packs") or {},
        "packs": store.docs(),
    }
