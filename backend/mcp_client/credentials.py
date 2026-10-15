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

from backend.mcp_client import secretstore

log = logging.getLogger("addled.mcp.credentials")

# All three live in one setting so a reset cannot keep the keys and drop the
# rest, which would leave a server half-configured.
_KIND_ENV = "env"
_KIND_HEADERS = "headers"
# Values that travel in the URL. Smithery's gateway reads its key as `api_key`,
# and sending it as a bearer token is refused outright - so a credential whose
# destination is the query string needs its own bucket rather than being bent
# into a header.
_KIND_PARAMS = "params"


def _stored() -> dict:
    """Every stored value, decrypted, for this process to use.

    The single place values are read, which is why decryption lives here
    rather than in each accessor: `env()`, `headers()`, `params()` and every
    `subset_*` call in the add path all come through this function, so none of
    them has to know the values are encrypted on disk.
    """
    from backend.config import config
    value = config.get("mcp", "credentials", default={}) or {}
    if not isinstance(value, dict):
        return {_KIND_ENV: {}, _KIND_HEADERS: {}, _KIND_PARAMS: {}}
    out: dict[str, dict[str, str]] = {}
    for kind in (_KIND_ENV, _KIND_HEADERS, _KIND_PARAMS):
        bucket = value.get(kind) or {}
        out[kind] = {str(k): _readable(v) for k, v in bucket.items()}
    return out


def _readable(value: object) -> str:
    """One stored value in the clear, or the raw text if it is not encrypted.

    A value with no `dpapi:` prefix is returned unchanged so a credential
    saved before encryption existed keeps working. A value that *is* prefixed
    but cannot be decrypted returns empty rather than raising: this runs while
    listing what is configured, and one unreadable string must not take the
    whole panel down. `have_*` then answers honestly — the name is not
    satisfied, and the user is asked for it again.
    """
    text = str(value if value is not None else "")
    if not secretstore.is_protected(text):
        return text
    try:
        return secretstore.unprotect(text)
    except Exception as e:  # noqa: BLE001
        log.warning("A stored MCP credential could not be decrypted: %s", e)
        return ""


def _encrypted(current: dict) -> dict:
    """The on-disk form of the in-memory values: every one of them protected.

    All three buckets are protected, not just `headers`. A credential that
    travels in the URL (`params`) or is handed to a process (`env`) is no less
    a secret than one in a header, and encrypting only the bucket that looked
    secret would leave the rest readable — which is the failure this replaces.

    If DPAPI is unavailable the values are written as they are, and the log
    says so. That is deliberate: this is Windows-only code and the store must
    still work on a platform without it. The alternative — refusing to save —
    trades a real feature for a guarantee the host cannot provide either way.
    A user who needs the encryption gets it wherever DPAPI exists, which is
    wherever the packaged app runs.
    """
    protected: dict[str, dict[str, str]] = {}
    for kind in (_KIND_ENV, _KIND_HEADERS, _KIND_PARAMS):
        bucket = current.get(kind) or {}
        out: dict[str, str] = {}
        for key, value in bucket.items():
            text = str(value if value is not None else "")
            if not text:
                continue
            # Already-protected text is kept as it is: re-encrypting an
            # encrypted string would write a `dpapi:` prefix inside another.
            if secretstore.is_protected(text):
                out[str(key)] = text
                continue
            try:
                out[str(key)] = secretstore.protect(text)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not encrypt the MCP credential '%s' (%s); "
                            "it is stored unencrypted", key, e)
                out[str(key)] = text
        protected[kind] = out
    return protected


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


def params() -> dict[str, str]:
    return _stored()[_KIND_PARAMS]


def have_env(name: str) -> bool:
    return bool(env().get(str(name or "").strip()))


def have_header(name: str) -> bool:
    return bool(headers().get(str(name or "").strip()))


def have_param(name: str) -> bool:
    return bool(params().get(str(name or "").strip()))


def missing_env(names: object) -> list[str]:
    """The declared names we hold no value for, in the order given."""
    return [str(n) for n in (names or []) if not have_env(n)]


def missing_headers(names: object) -> list[str]:
    return [str(n) for n in (names or []) if not have_header(n)]


def missing_params(names: object) -> list[str]:
    return [str(n) for n in (names or []) if not have_param(n)]


def subset_env(names: object) -> dict[str, str]:
    """The values we hold for these names — what a spec should be given."""
    held = env()
    return {str(n): held[str(n)] for n in (names or []) if held.get(str(n))}


def subset_headers(names: object) -> dict[str, str]:
    held = headers()
    return {str(n): held[str(n)] for n in (names or []) if held.get(str(n))}


def subset_params(names: object) -> dict[str, str]:
    held = params()
    return {str(n): held[str(n)] for n in (names or []) if held.get(str(n))}


def save(env_values: object = None, header_values: object = None,
         param_values: object = None) -> dict:
    """Remember values the user just typed. Returns the names now known.

    An empty value for a name that is already stored clears it, which is how the
    UI removes one — the only way to unset a key without editing settings.json.
    """
    from backend.config import config

    current = _stored()
    for kind, incoming in ((_KIND_ENV, env_values),
                           (_KIND_HEADERS, header_values),
                           (_KIND_PARAMS, param_values)):
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

    # Compared in the clear, before encrypting: DPAPI is randomised, so
    # encrypting first would make every save look like a change and rewrite
    # settings.json on every keystroke of the credentials form.
    if current != _stored():
        stored = _encrypted(current)
        try:
            config.set("mcp", "credentials", value=stored)
            log.info("MCP credentials updated (env: %d, headers: %d, params: %d)",
                     len(current[_KIND_ENV]), len(current[_KIND_HEADERS]),
                     len(current[_KIND_PARAMS]))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not save MCP credentials: %s", e)
    return known()


def known() -> dict:
    """Which names have a value — names only, never the values."""
    stored = _stored()
    return {"env": sorted(stored[_KIND_ENV]),
            "headers": sorted(stored[_KIND_HEADERS]),
            "params": sorted(stored[_KIND_PARAMS])}


def clear(env_name: str = "", header_name: str = "",
          param_name: str = "") -> dict:
    """Forget one stored value."""
    current = _stored()
    if env_name:
        current[_KIND_ENV].pop(env_name, None)
    if header_name:
        current[_KIND_HEADERS].pop(header_name, None)
    if param_name:
        current[_KIND_PARAMS].pop(param_name, None)
    from backend.config import config
    config.set("mcp", "credentials", value=current)
    return known()
