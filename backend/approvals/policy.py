"""Standing permission for skills and tools the user has said yes to.

A skill or tool that needs approval asks every time. That is right for a
destructive action and wrong for the fourth time in a row in the same session,
so the approval card offers **Always allow** and the skill and tool cards each
carry a switch for the same answer. Both write here.

Three rules shape this module:

1. **It is a list of names, not a flag on the skill.** A granted name lives in
   `safety.always_allow`, so it survives a restart and can be read back and
   revoked. `SkillDefinition.requires_approval` still says the skill *can* ask;
   this says the user has already answered.

2. **Destructive actions are refused here, not merely hidden in the UI.** The
   destruction gate owns `delete_file` and the dangerous shell commands, and
   those ask every time. `always_allow()` returns False for them rather than
   writing the name, so a dashboard bug, a crafted RPC or a future caller
   cannot turn one click into permanent permission to format a disk.

3. **Failures never grant permission.** If the config cannot be read or written,
   the answer is "not allowed" and the prompt appears as it did before. The
   cost of that is one extra click; the cost of the other direction is not
   recoverable.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.approvals.policy")

# The two kinds of thing that can be granted. Kept as a tuple so an unknown
# kind is a typo that gets refused rather than a new config key created on the
# fly by a caller passing whatever string it had to hand.
SKILL = "skill"
TOOL = "tool"
KINDS = (SKILL, TOOL)

_SECTION = ("safety", "always_allow")
_MAX_NAMES = 500

def _bucket_key(kind: str) -> str:
    """`skill` -> `skills`, `tool` -> `tools` (the config dict's own keys)."""
    return f"{kind}s"

def protected_names() -> set[str]:
    """Action names the destruction gate owns — never grantable.

    Imported lazily: the gate imports `terminal`, which imports a good deal,
    and this module is imported from the executor's hot path.
    """
    names: set[str] = set()
    try:
        from backend.safety.destruction_gate import DESTRUCTIVE_ACTIONS
        names |= {str(n) for n in DESTRUCTIVE_ACTIONS}
    except Exception as e:  # noqa: BLE001
        log.debug("could not read the destruction gate's set: %s", e)
    try:
        # The gate classifies `run_command` by its content, so the command set
        # is not a list of skill names. It is included because the same names
        # must not become grantable if a skill is ever named after one.
        from backend.actions.terminal import DANGEROUS_COMMANDS
        names |= {str(c).strip().rstrip("/\\") for c in DANGEROUS_COMMANDS}
    except Exception as e:  # noqa: BLE001
        log.debug("could not read the dangerous-command set: %s", e)
    return {n for n in names if n}

def is_protected(name: str) -> bool:
    """True when this name must keep asking, whatever the user clicks."""
    clean = str(name or "").strip().lower()
    if not clean:
        return True
    for guarded in protected_names():
        if clean == guarded or clean.startswith(guarded):
            return True
    return False

def fingerprint(name: str) -> str:
    """A short digest of what an installed skill actually *is*.

    "Always allow" is keyed by name, and a name is not an identity: removing
    `pdf_tools` and installing a different `pdf_tools` from another repository
    — with a different script — would inherit the grant without anyone being
    asked. That is the one gap in treating a grant as a fact about a name.

    So the grant is stored against a digest of the skill's own files. A
    reinstall of the same code keeps its permission; a skill whose code changed
    asks again, which is the answer that costs one click rather than the one
    that cannot be taken back.

    Only installed skills (`market`) have files of their own to digest. A
    built-in or MCP skill is identified by name because its code ships with the
    app or lives in the server, and neither can be swapped underneath a grant.
    """
    clean = str(name or "").strip()
    if not clean:
        return ""
    try:
        # Imported by module path, not `from backend.skills import market`:
        # that package exports a singleton *named* `market`, which shadows the
        # module and would hand back the loader instance instead — silently
        # failing here and quietly disabling this check altogether.
        import importlib
        market_module = importlib.import_module("backend.skills.market")
        folder = market_module.installed_dir(clean)
        if folder is None:
            return ""
        return market_module.folder_digest(folder)
    except Exception as e:  # noqa: BLE001
        log.debug("could not fingerprint '%s': %s", clean, e)
        return ""

def _read() -> dict:
    """The whole `always_allow` dict, normalised to both buckets present."""
    try:
        from backend.config import config
        raw = config.get(*_SECTION, default=None)
    except Exception as e:  # noqa: BLE001
        log.debug("could not read always_allow: %s", e)
        raw = None
    if not isinstance(raw, dict):
        return {"skills": [], "tools": [], "fingerprints": {}}
    out = {}
    for kind in KINDS:
        bucket = raw.get(_bucket_key(kind))
        out[_bucket_key(kind)] = [str(x) for x in bucket] \
            if isinstance(bucket, (list, tuple)) else []
    prints = raw.get("fingerprints")
    out["fingerprints"] = {str(k): str(v) for k, v in prints.items()} \
        if isinstance(prints, dict) else {}
    return out

def _write(data: dict) -> bool:
    try:
        from backend.config import config
        config.set(*_SECTION, value=data)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not save always_allow: %s", e)
        return False

def _valid_kind(kind: str) -> str | None:
    clean = str(kind or "").strip().lower()
    return clean if clean in KINDS else None

def is_always_allowed(kind: str, name: str) -> bool:
    """Has the user granted this skill or tool standing permission?

    Never raises, and answers False when the store cannot be read. This is the
    one function every hot path calls, and it is asked immediately before a
    destructive thing runs, so a failure has to fall back to the prompt rather
    than to permission.

    For an installed skill the grant also has to match the code it was given
    for: a name that has been reinstalled from somewhere else is a different
    thing and asks again.
    """
    k = _valid_kind(kind)
    clean = str(name or "").strip()
    if k is None or not clean:
        return False
    # A name that is protected now might have been granted by an older build,
    # so the check is repeated on the way out and not only on the way in.
    if k == SKILL and is_protected(clean):
        return False
    try:
        data = _read()
        if clean not in data[_bucket_key(k)]:
            return False
        if k != SKILL:
            return True
        recorded = data["fingerprints"].get(clean, "")
        current = fingerprint(clean)
        if not current:
            # No digest to compare against, for one of two reasons. Either the
            # skill was never an installed one — a built-in, whose name is a
            # stable identity and whose grant is simply honoured — or it *was*
            # installed and its files are now gone, in which case there is
            # nothing left to run and the grant should not survive into
            # whatever takes its name next. The recorded digest is what tells
            # the two apart.
            return not recorded
        if not recorded:
            # An installed skill granted before fingerprints existed. Honouring
            # it silently would keep the gap open for every such grant already
            # on disk, so it asks once more — the cheap, reversible direction.
            return False
        return current == recorded
    except Exception as e:  # noqa: BLE001
        log.warning("could not read always_allow, so '%s' will ask: %s",
                    clean, e)
        return False

def always_allow(kind: str, name: str) -> dict:
    """Grant standing permission. Refuses a protected name.

    Returns `{"success": bool, ...}`: `protected` says the refusal was the
    guard rather than a failure, so the caller can explain it rather than
    report an error.
    """
    k = _valid_kind(kind)
    clean = str(name or "").strip()
    if k is None:
        return {"success": False, "error": f"unknown kind: {kind!r}"}
    if not clean:
        return {"success": False, "error": "a name is required"}
    if k == SKILL and is_protected(clean):
        return {"success": False, "protected": True,
                "error": (f"'{clean}' always asks first. Destructive actions "
                          "cannot be granted standing permission.")}
    data = _read()
    bucket = data[_bucket_key(k)]
    if clean not in bucket:
        if len(bucket) >= _MAX_NAMES:
            return {"success": False,
                    "error": f"too many granted {k}s ({_MAX_NAMES})"}
        bucket.append(clean)
        bucket.sort()
    # Record what was actually allowed, so a different skill installed under
    # the same name does not inherit this answer.
    if k == SKILL:
        current = fingerprint(clean)
        if current:
            data["fingerprints"][clean] = current
    if not _write(data):
        return {"success": False,
                "error": "the permission could not be saved"}
    return {"success": True, "kind": k, "name": clean, "allowed": True}

def revoke(kind: str, name: str) -> dict:
    """Take standing permission back, so the prompt returns."""
    k = _valid_kind(kind)
    clean = str(name or "").strip()
    if k is None:
        return {"success": False, "error": f"unknown kind: {kind!r}"}
    if not clean:
        return {"success": False, "error": "a name is required"}
    data = _read()
    bucket = data[_bucket_key(k)]
    if clean in bucket:
        bucket.remove(clean)
    # The fingerprint goes with it. Leaving it behind would silently re-allow
    # the skill if the same name were ever granted again by something else.
    data["fingerprints"].pop(clean, None)
    if not _write(data):
        return {"success": False, "error": "the change could not be saved"}
    return {"success": True, "kind": k, "name": clean, "allowed": False}

def list_allowed() -> dict:
    """Everything currently granted, for the settings summary and the cards.

    Never raises: an unreadable store is reported as nothing granted, which is
    the safe reading — the cards then show "not allowed" and the prompt stays.
    """
    try:
        data = _read()
    except Exception as e:  # noqa: BLE001
        log.warning("could not list always_allow: %s", e)
        return {"skills": [], "tools": []}
    return {"skills": list(data["skills"]), "tools": list(data["tools"])}

def clear() -> dict:
    """Drop every grant, fingerprints included. Used by the check suite."""
    if not _write({"skills": [], "tools": [], "fingerprints": {}}):
        return {"success": False, "error": "the change could not be saved"}
    return {"success": True, "skills": [], "tools": []}
