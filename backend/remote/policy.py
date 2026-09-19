"""
What a remote caller may see and may do.

Kept in one module on purpose. The WebSocket API has 138 handlers and several
of them are effectively arbitrary-code primitives written on the assumption that
only this machine could reach them. Spreading "is this remote?" checks across
the handlers would mean trusting 138 call sites to remember, so instead there is
a single gate that `_dispatch` consults once, before any handler runs, plus a
single redactor for outbound settings.

Nothing here trusts the Tailscale identity headers. They are shown to the user
in the session list, but a local process can send the same headers, so they are
never the thing that grants access.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("addled.remote.policy")

# A value is replaced with this when it must not reach a remote caller. Kept
# truthy-looking so "is it configured?" is still answerable without the secret.
REDACTED = "(hidden)"

# Exact key names that hold credentials. Deliberately a list rather than a
# broad regex: redacting `max_tokens` because it ends in "tokens" would break
# the Settings page for no security gain.
SECRET_KEYS = {
    "api_key", "apikey", "auth_key", "password", "password_hash",
    "secret", "client_secret", "access_token", "refresh_token",
    "private_key", "tokens", "credential", "credentials",
    "email_password", "imap_password", "smtp_password",
}

# Suffixes that make a key a secret whatever its prefix (`deepseek_api_key`,
# `google_client_secret`, `telegram_bot_token`).
SECRET_SUFFIXES = ("_api_key", "_password", "_secret", "_token", "_auth_key",
                   "_private_key")

# Methods a remote session may never call, regardless of settings. Each is a
# way to run code or install it, and none of them is a dashboard feature — they
# exist so the local agent can extend itself.
REMOTE_FORBIDDEN_METHODS = {
    "mcp.add", "mcp.connect", "mcp.update", "mcp.remove", "mcp.reload",
    "skills.installFrom",
    "forge.create",
    # Binding decides which folder the code.* writes are contained to. A remote
    # caller could bind `C:\\` and then write anywhere, so the workspace choice
    # stays a local decision. The contained methods remain available remotely.
    "code.bind",
    # Downloading an executable onto the machine Addled runs on is not something
    # a browser session should be able to ask for, even though the user themself
    # clicks the button locally. Being in this set also means the generic reason
    # below describes it correctly.
    "system.rtkInstall",
    "system.uvInstall",
}

# Action types held back from remote sessions unless deliberately opened.
SHELL_ACTIONS = {"run_command"}
INPUT_ACTIONS = {"click", "double_click", "type", "key_press", "scroll",
                 "drag", "move_mouse", "set_clipboard"}


def _flag(key: str, default: bool = False) -> bool:
    try:
        from backend.config import config
        return bool(config.get("remote", key, default=default))
    except Exception:
        return default


def context_of(ws) -> dict:
    """The remote context attached at handshake time, if any."""
    return getattr(ws, "addled", None) or {}


def is_remote(ws) -> bool:
    """Whether this connection came through the gateway rather than being local."""
    return bool(context_of(ws).get("remote"))


def session_id(ws) -> str:
    return str(context_of(ws).get("session") or "")


def client_addr(ws) -> str:
    return str(context_of(ws).get("addr") or "")


def identity(ws) -> str:
    return str(context_of(ws).get("tailscale_user") or "")


# -- what a remote caller may do ----------------------------------------------


# A more accurate reason for the methods above whose generic wording would
# misdescribe them.
REMOTE_FORBIDDEN_REASONS = {
    "code.bind": ("Binding a folder is done on the machine running Addled, "
                   "because it decides which folder the code tools may then "
                   "write to. Once it is bound there, the contained code.* "
                   "calls work normally over remote access."),
}


def remote_refusal(method: str, params: dict | None = None) -> str | None:
    """Why a remote session may not use this, or None if it may.

    Called once from `_dispatch`, so a handler added later is covered by
    default rather than by remembering to add a check.
    """
    if method in REMOTE_FORBIDDEN_METHODS:
        specific = REMOTE_FORBIDDEN_REASONS.get(method)
        if specific:
            return specific
        return (f"{method} is not available over remote access — it can start "
                f"a process or install code, which is not something a browser "
                f"session should be able to do.")

    if method == "action.execute":
        action = str((params or {}).get("type") or "")
        if action in SHELL_ACTIONS and not _flag("allow_shell"):
            return ("Running commands is blocked for remote sessions. To allow "
                    "it, turn on 'Allow remote shell' in Addled under Settings "
                    "→ Remote, on that machine.")
        if action in INPUT_ACTIONS and not _flag("allow_desktop_input"):
            return ("Controlling the mouse and keyboard is blocked for remote "
                    "sessions. To allow it, turn on 'Allow remote input' in "
                    "Addled under Settings → Remote, on that machine.")

    return None


# -- what a remote caller may see ---------------------------------------------


def is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    if lowered in SECRET_KEYS:
        return True
    return lowered.endswith(SECRET_SUFFIXES)


def redact_settings(value, _depth: int = 0):
    """Replace credential values with REDACTED, recursively.

    Only truthy values are replaced, so "not configured" still looks like "not
    configured" and the Settings page keeps making sense.
    """
    if _depth > 12:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_secret_key(key) and item:
                if isinstance(item, dict):
                    # e.g. bots.tokens — report which entries exist, not what
                    # they contain.
                    out[key] = {k: REDACTED for k in item}
                else:
                    out[key] = REDACTED
            else:
                out[key] = redact_settings(item, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact_settings(item, _depth + 1) for item in value]
    return value


# -- origins ------------------------------------------------------------------

# The tailnet's own HTTPS name is what Tailscale Serve serves, and it is not
# known until the node is up, so it has to be a pattern.
TAILNET_ORIGIN = re.compile(r"https?://([a-z0-9-]+\.)+ts\.net(:\d+)?$")
LOCAL_ORIGINS = [
    re.compile(r"https?://127\.0\.0\.1(:\d+)?$"),
    re.compile(r"https?://localhost(:\d+)?$"),
    re.compile(r"https?://\[::1\](:\d+)?$"),
]


def allowed_origins(trusted=None) -> list:
    """Accepted `Origin` values for a WebSocket handshake.

    `None` in the list means "no Origin header at all", which is what the Node
    bot bridges and other non-browser clients send. A browser always sends one,
    so this is the check that stops another site's JavaScript from driving
    Addled — a hole that exists today, with no Tailscale involved, because the
    handshake was never checked at all.
    """
    items = list(trusted or [])
    if isinstance(items, (str, bytes)):
        items = [items]
    patterns: list = [None]
    patterns += [re.compile(str(p)) for p in items if str(p).strip()]
    patterns.append(TAILNET_ORIGIN)
    patterns += LOCAL_ORIGINS
    return patterns
