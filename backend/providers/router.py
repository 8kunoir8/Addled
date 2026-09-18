"""
Task-aware model routing.

A single ``default_model`` cannot be right for every request: a one-line question
does not need the reasoning model, and an image does not need the chat model.
This module classifies a request into a small set of *roles* and resolves which
model to send for that role.

The contract is deliberately conservative:

* ``providers.<id>.default_model`` stays the single baseline model. Every role
  falls back to it, so with no role overrides configured the resolved model is
  exactly what Addled sent before routing existed.
* Classification is pure heuristics — no extra LLM call, no network, no latency.
* ``providers.auto_route`` (default true) switches routing off entirely; callers
  then receive ``None`` and behave as they always did.

Roles
-----
``chat``       quick conversational replies (the default)
``reasoning``  analysis, debugging, planning, maths, architecture
``vision``     image/screenshot input
``long``       very large inputs (whole file / document / repo)
``utility``    small background extractions (facts, titles, summaries)
"""

from __future__ import annotations

import logging
import re

from backend.config import config

log = logging.getLogger("addled.providers.router")

ROLES = ("chat", "reasoning", "vision", "long", "utility")
DEFAULT_ROLE = "chat"

# Messages longer than this are treated as "long context" work.
_LONG_CHARS = 1200
# Messages longer than this are treated as substantive enough to reason about.
_REASON_CHARS = 400
# Code work is worth reasoning about at a much lower bar: "rename x to y" is
# trivial, "update this function to handle the new payload shape" is not.
_CODE_REASON_CHARS = 80
# A fenced block with at least this many lines implies real code work.
_CODE_BLOCK_LINES = 12

# --- heuristics ---------------------------------------------------------------

_EXPLICIT_LONG = re.compile(
    r"\b("
    r"whole\s+(?:file|files|repo|repository|codebase|project|document|pdf|log|transcript)"
    r"|entire\s+(?:file|files|repo|repository|codebase|project|document|pdf|log|transcript)"
    r"|read\s+(?:all|everything|the\s+whole)"
    r"|all\s+(?:of\s+)?the\s+(?:files|code|documents)"
    r"|summari[sz]e\s+(?:this|the)\s+(?:\w+\s+)?(?:document|file|pdf|transcript|conversation|log)"
    r"|full\s+(?:file|context|document|transcript)"
    r"|go\s+through\s+(?:the\s+)?(?:whole|entire)"
    r")\b",
    re.IGNORECASE,
)

_REASONING = re.compile(
    r"\b("
    r"prove|proof|why\s+(?:does|do|is|are|did|would|won't|wont)"
    r"|root\s+cause|debug|diagnose|troubleshoot"
    r"|analy[sz]e|analys[ei]s|assess|evaluate"
    r"|architect(?:ure)?|design\s+(?:a|an|the|me)|system\s+design"
    r"|refactor|restructure|migrat(?:e|ion)"
    r"|algorithm|data\s+structure|complexity|big[\s-]?o"
    r"|trade[\s-]?offs?|pros\s+and\s+cons|implications?|downsides?|edge\s+cases?"
    r"|compare\b[\s\S]{0,40}\bvs\b|versus|which\s+is\s+better|should\s+i\s+(?:use|choose|pick)"
    r"|step[\s-]by[\s-]step|walk\s+me\s+through|think\s+through|reason\s+about"
    r"|strateg(?:y|ise|ize|ic)|plan\s+(?:for|out|the)|roadmap|figure\s+out"
    r"|optimi[sz]e|improve\s+performance|scale|bottleneck"
    r"|estimat(?:e|ion)|calculate|compute|derive|solve"
    r"|explain\s+why|how\s+does\b[\s\S]{0,40}\bwork|what\s+happens\s+if"
    r")\b",
    re.IGNORECASE,
)

_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

_ROLE_TAG = re.compile(
    r"^\s*@(" + "|".join(ROLES) + r")\b[:,]?[ \t]*",
    re.IGNORECASE,
)


def split_role_tag(message: str) -> tuple[str | None, str]:
    """Pop an explicit ``@role`` prefix from a message.

    Returns ``(role | None, message_without_tag)``. Lets a user (or a test) force
    a role without touching settings.
    """
    if not message:
        return None, message
    match = _ROLE_TAG.match(message)
    if not match:
        return None, message
    return match.group(1).lower(), message[match.end():]


def _code_block_lines(message: str) -> int:
    longest = 0
    for block in _CODE_BLOCK.finditer(message or ""):
        longest = max(longest, block.group(1).count("\n") + 1)
    return longest


def classify(
    message: str,
    *,
    has_attachments: bool = False,
    image_only: bool = False,
    code_hint: bool = False,
) -> str:
    """Pick a role for this request. Pure function — safe to unit test."""
    text = message or ""
    length = len(text.strip())

    # 1. Images win: they need a multimodal model regardless of wording.
    if has_attachments and image_only:
        return "vision"

    # 2. Huge inputs need a long-context model.
    long_chars = int(config.get("providers", "route_long_chars",
                                default=_LONG_CHARS) or _LONG_CHARS)
    if length > long_chars or _EXPLICIT_LONG.search(text):
        return "long"

    # 3. Reasoning cues.
    if _REASONING.search(text):
        return "reasoning"
    if _code_block_lines(text) >= _CODE_BLOCK_LINES:
        return "reasoning"
    if text.count("?") >= 2:
        return "reasoning"
    reason_chars = int(config.get("providers", "route_reason_chars",
                                  default=_REASON_CHARS) or _REASON_CHARS)
    # Substantive code work is reasoning; a one-line code tweak is not.
    if code_hint:
        code_chars = int(config.get("providers", "route_code_reason_chars",
                                    default=_CODE_REASON_CHARS)
                         or _CODE_REASON_CHARS)
        if length > code_chars or _code_block_lines(text) >= 3:
            return "reasoning"
    if length > reason_chars:
        return "reasoning"

    return DEFAULT_ROLE


# --- resolution ---------------------------------------------------------------


def _provider_cfg(provider_id: str) -> dict:
    try:
        return config.provider_config(provider_id) or {}
    except Exception:
        return {}


def _catalog_models(provider_id: str) -> list[str]:
    """Models discovered from the provider's API, if the catalog module exists."""
    try:
        from backend.providers import model_catalog
        return list(model_catalog.merged(provider_id) or [])
    except Exception:
        return []


def resolve_model(
    provider_id: str,
    role: str,
    cfg: dict | None = None,
) -> str | None:
    """The model to use for ``role``, or ``None`` to let the provider decide.

    ``None`` means "send no model at all" — exactly the pre-routing behaviour.
    That happens when routing is disabled, or when the provider has no model
    configured for any role.
    """
    if not bool(config.get("providers", "auto_route", default=True)):
        return None
    if role not in ROLES:
        role = DEFAULT_ROLE

    cfg = cfg if cfg is not None else _provider_cfg(provider_id)
    default_model = str(cfg.get("default_model") or "").strip()
    roles = cfg.get("roles") or {}
    if not isinstance(roles, dict):
        roles = {}
    override = str(roles.get(role) or "").strip()

    if override:
        candidate = override
    elif role == "vision":
        # Providers with a dedicated vision model keep working exactly as before.
        candidate = (str(cfg.get("vision_model") or "").strip()
                     or default_model)
    else:
        candidate = default_model

    if not candidate:
        return None

    # Guard against a stale role override that the provider no longer offers.
    if override and bool(config.get("providers", "route_validate", default=True)):
        known = _catalog_models(provider_id)
        if known and candidate not in known:
            log.warning(
                "Route '%s' for provider '%s' names model '%s', which the "
                "provider does not list — falling back to '%s'.",
                role, provider_id, candidate, default_model or "provider default")
            return default_model or None

    return candidate


def pick(
    provider_id: str,
    message: str,
    *,
    has_attachments: bool = False,
    image_only: bool = False,
    code_hint: bool = False,
    force_role: str | None = None,
    cfg: dict | None = None,
) -> tuple[str, str | None]:
    """Classify a message and resolve its model in one step.

    Returns ``(role, model | None)``. An explicit ``@role`` tag in the message
    wins over the heuristics; ``force_role`` (used by code editing) wins over both.
    """
    tagged_role, _ = split_role_tag(message or "")
    role = force_role or tagged_role or classify(
        message,
        has_attachments=has_attachments,
        image_only=image_only,
        code_hint=code_hint,
    )
    return role, resolve_model(provider_id, role, cfg=cfg)


def utility_model(provider_id: str) -> str | None:
    """Model for small background extractions, or None to use the default."""
    if not bool(config.get("providers", "auto_route", default=True)):
        return None
    cfg = _provider_cfg(provider_id)
    roles = cfg.get("roles") or {}
    if not isinstance(roles, dict):
        return None
    return str(roles.get("utility") or "").strip() or None


def describe(provider_id: str) -> dict:
    """Effective model per role — powers the Settings routing panel."""
    cfg = _provider_cfg(provider_id)
    out: dict[str, str] = {}
    for role in ROLES:
        out[role] = resolve_model(provider_id, role, cfg=cfg) or ""
    return {
        "provider": provider_id,
        "auto_route": bool(config.get("providers", "auto_route", default=True)),
        "route_validate": bool(config.get("providers", "route_validate",
                                          default=True)),
        "default_model": str(cfg.get("default_model") or ""),
        "roles": out,
    }
