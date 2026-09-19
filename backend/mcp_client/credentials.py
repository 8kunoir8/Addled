"""Values a market server needs before it can run — held, never echoed back.

Half the MCP registry declares required environment variables, and a further set
declare required headers. The market was already honest about it — *"needs
HASDATA_API_KEY"*, *"needs an API key for Authorization"* — but there was nowhere
to put the value, so those entries were permanently unaddable and the reason was
a dead end. This is where the value goes.

They are keyed by variable name, because that is what a listing declares:
`WORKSPACE_ROOT` is the same requirement wherever it is asked for, and a value
already given is reused the next time a listing wants it — including when the
agent finds a server on its own, which is what makes an automatic install of a
keyed server possible at all.

Nothing here returns a value to the dashboard. It reports which names are known,
the same way the Tailscale auth key is reported: the UI has no business reading
back a credential it just stored.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.mcp.credentials")

# Both halves live in one setting so a reset cannot keep the keys and drop the
# headers, which would leave a server half-configured.
_KIND_ENV = "env"
_KIND_HEADERS = "headers"


def _stored() -> dict:
    from backend.config import config
    value = config.get("mcp", "credentials", default={}) or {}
    if not isinstance(value, dict):
        return {_KIND_ENV: {}, _KIND_HEADERS: {}}
    return {
        _KIND_ENV: dict(value.get(_KIND_ENV) or {}),
        _KIND_HEADERS: dict(value.get(_KIND_HEADERS) or {}),
    }


def _clean(mapping: object) -> dict[str, str]:
    """Only named, non-empty string values are worth keeping."""
    if not isinstance(mapping, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in mapping.items():
        name = str(key or "").strip()
        text = "" if value is None else str(value).strip()
        if name and text:
            out[name] = text
    return out


def env() -> dict[str, str]:
    return _stored()[_KIND_ENV]


def headers() -> dict[str, str]:
    return _stored()[_KIND_HEADERS]


def have_env(name: str) -> bool:
    return bool(env().get(str(name or "").strip()))


def have_header(name: str) -> bool:
    return bool(headers().get(str(name or "").strip()))


def missing_env(names: object) -> list[str]:
    """The declared names we hold no value for, in the order given."""
    return [str(n) for n in (names or []) if not have_env(n)]


def missing_headers(names: object) -> list[str]:
    return [str(n) for n in (names or []) if not have_header(n)]


def subset_env(names: object) -> dict[str, str]:
    """The values we hold for these names — what a spec should be given."""
    held = env()
    return {str(n): held[str(n)] for n in (names or []) if held.get(str(n))}


def subset_headers(names: object) -> dict[str, str]:
    held = headers()
    return {str(n): held[str(n)] for n in (names or []) if held.get(str(n))}


def save(env_values: object = None, header_values: object = None) -> dict:
    """Remember values the user just typed. Returns the names now known.

    An empty value for a name that is already stored clears it, which is how the
    UI removes one — the only way to unset a key without editing settings.json.
    """
    from backend.config import config

    current = _stored()
    for kind, incoming in ((_KIND_ENV, env_values), (_KIND_HEADERS, header_values)):
        if incoming is None:
            continue
        bucket = current[kind]
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                name = str(key or "").strip()
                if not name:
                    continue
                text = "" if value is None else str(value).strip()
                if text:
                    bucket[name] = text
                else:
                    bucket.pop(name, None)
        else:
            bucket.update(_clean(incoming))

    if current != _stored():
        try:
            config.set("mcp", "credentials", value=current)
            log.info("MCP credentials updated (env: %d, headers: %d)",
                     len(current[_KIND_ENV]), len(current[_KIND_HEADERS]))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not save MCP credentials: %s", e)
    return known()


def known() -> dict:
    """Which names have a value — names only, never the values."""
    stored = _stored()
    return {"env": sorted(stored[_KIND_ENV]), "headers": sorted(stored[_KIND_HEADERS])}


def clear(env_name: str = "", header_name: str = "") -> dict:
    """Forget one stored value."""
    current = _stored()
    if env_name:
        current[_KIND_ENV].pop(env_name, None)
    if header_name:
        current[_KIND_HEADERS].pop(header_name, None)
    from backend.config import config
    config.set("mcp", "credentials", value=current)
    return known()
