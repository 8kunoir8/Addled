"""
Skills for the external guideline packs.

``guidelines_status`` reports what Addled is currently following, and
``ponytail_review`` turns the ponytail ruleset into an actual capability: point
it at a file or a diff and it lists what to cut, in upstream's own output format
(one line per finding, ending in ``net: -<N> lines possible.``).

The review instructions are written here rather than copied from upstream so
the pack text itself stays a fetched, updatable document.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.skills.guidelines")

MAX_REVIEW_CHARS = 20000

# Upstream's review output contract, restated.
_REVIEW_INSTRUCTIONS = """You are reviewing code for unnecessary complexity only. This is the ponytail \
review.

Scope: over-engineering and complexity ONLY. Correctness bugs, security holes \
and performance are explicitly out of scope — do not mention them. Do not apply \
or write out fixes; only list what to change.

Report one finding per line, in exactly this shape:
L<line>: <tag> <what to cut>. <replacement>.
Or <file>:L<line>: ... when the input spans more than one file.

Tags:
- delete: dead code, unused flexibility, speculative feature. Replacement: nothing.
- stdlib: hand-rolled thing the standard library ships. Name the function.
- native: a dependency or code doing what the platform already does. Name the feature.
- yagni: an abstraction with one implementation, config nobody sets, a layer with one caller.
- shrink: same logic, fewer lines. Show the shorter form.

End with the only metric that matters: `net: -<N> lines possible.`
If there is nothing to cut, reply exactly: `Lean already. Ship.` and stop.
A single smoke test or `assert`-based self-check is the ponytail minimum, not \
bloat — never flag it for deletion.
Be specific: name the line, the thing to cut, and what replaces it. No preamble, \
no summary of what the code does."""


async def _guidelines_status(params: dict) -> dict:
    """Report which external rulesets Addled is currently following."""
    from backend.guidelines import inject as guidelines
    info = guidelines.describe()
    pack_cfg = info.get("pack_config") or {}
    lines = []
    for pid, doc in (info.get("packs") or {}).items():
        cfg = pack_cfg.get(pid) or {}
        level = cfg.get("level") or doc.get("default_level") or "full"
        lines.append(
            f"{doc.get('title', pid)}: "
            f"{'on' if cfg.get('enabled', True) else 'off'}, "
            f"level={level}, "
            f"scope={cfg.get('scope') or info.get('scope')}, "
            f"{doc.get('chars', 0)} chars, "
            f"updated {doc.get('fetched_at') or 'never'}"
            + (f", error: {doc['error']}" if doc.get("error") else ""))
    active = [p["id"] for p in guidelines.active(code_task=True)]
    return {
        "success": True,
        "enabled": info.get("enabled"),
        "scope": info.get("scope"),
        "max_chars": info.get("max_chars"),
        "active_for_code": active,
        "packs": info.get("packs"),
        "snippet": "\n".join(lines) or "No guideline packs configured.",
        "summary": ("Guidelines active for code: " + ", ".join(active)
                    if active else "No guideline packs active."),
    }


async def _ponytail_review(params: dict) -> dict:
    """Review a file or diff for over-engineering (the ponytail ladder)."""
    code = str(params.get("code") or "")
    path = str(params.get("path") or "").strip()
    focus = str(params.get("focus") or "").strip()

    if not code and path:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                code = handle.read(MAX_REVIEW_CHARS)
        except OSError as e:
            return {"success": False,
                    "error": f"Could not read '{path}': {e}"}
    if not code.strip():
        return {"success": False,
                "error": "Nothing to review — pass 'code' or 'path'."}

    capped = code[:MAX_REVIEW_CHARS]
    truncated = len(code) > MAX_REVIEW_CHARS
    prompt = _REVIEW_INSTRUCTIONS
    if focus:
        prompt += f"\n\nPay particular attention to: {focus}"
    target = path or "the submitted code"
    prompt += f"\n\n--- BEGIN {target} ---\n{capped}\n--- END {target} ---"
    if truncated:
        prompt += (f"\n\nThe input was truncated at {MAX_REVIEW_CHARS} "
                   "characters; review what is shown.")

    from backend.providers.registry import get_provider
    provider = get_provider()
    model = None
    try:
        from backend.providers import router
        _role, model = router.pick(
            getattr(provider, "provider_id", ""),
            "review this code for over-engineering",
            force_role="reasoning")
    except Exception as e:
        log.debug("review model routing failed: %s", e)

    try:
        result = await provider.chat(
            [{"role": "user", "content": prompt}],
            model=model, max_tokens=1500, temperature=0.2)
    except Exception as e:
        return {"success": False, "error": f"Review failed: {e}"}
    if not result.ok:
        return {"success": False,
                "error": result.error or "The provider returned an error."}

    review = (result.response or "").strip()
    net = ""
    for line in reversed(review.splitlines()):
        if line.strip().lower().startswith("net:"):
            net = line.strip()
            break
    return {
        "success": True,
        "target": target,
        "review": review,
        "net": net,
        "model": result.model or model or "",
        "summary": net or (review.splitlines()[0] if review else "Review complete"),
    }


def register(registry) -> None:
    """Attach the guideline skills to a SkillRegistry."""
    from backend.skills.registry import SkillDefinition

    registry.register(SkillDefinition(
        "guidelines_status",
        "Report which external working guidelines Addled is currently following "
        "(ponytail, Karpathy guidelines): whether each is on, its level, whether "
        "it applies only to code tasks or to all chat, how large the cached text "
        "is, and when it was last refreshed. Use when the user asks what rules or "
        "guidelines Addled is following.",
        {"type": "object", "properties": {}},
        _guidelines_status,
        category="meta",
    ))

    registry.register(SkillDefinition(
        "ponytail_review",
        "Review code or a diff for unnecessary complexity only — over-"
        "engineering, reinvented standard library, unneeded dependencies, "
        "speculative abstractions, dead flexibility. Returns one line per "
        "finding in the form 'L<line>: <tag> <what to cut>. <replacement>.' "
        "plus a 'net: -<N> lines possible.' score. Correctness, security and "
        "performance are out of scope. Pass 'code' directly or 'path' to a file.",
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The code or diff to review.",
                },
                "path": {
                    "type": "string",
                    "description": ("Absolute path to a file to review instead "
                                    "of passing code."),
                },
                "focus": {
                    "type": "string",
                    "description": ("Optional note about which part to look at "
                                    "most closely."),
                },
            },
        },
        _ponytail_review,
        category="code",
    ))
