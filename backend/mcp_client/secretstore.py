"""Credentials at rest, encrypted with the OS, not readable from the file.

MCP credentials were written into `settings.json` as plain text. Measured: a
saved value appeared verbatim in that file, so anything that could read a
config file — a backup, a synced folder, a file-share, another process running
as the same user — could read the key. `settings.json` is a file people copy
and paste into bug reports.

This encrypts them instead, with Windows DPAPI (`CryptProtectData`), which
binds the ciphertext to the current user's login. The encrypted bytes are
useless on another machine and useless to another account on this one. No new
dependency: `ctypes` against the documented API, the same approach
`backend/actions/audio.py` takes for Core Audio.

Scope is deliberately local-user, not local-machine: `CRYPTPROTECT_LOCCRED`
would let any process on the machine decrypt, which is the property that makes
this worth doing at all.

What this does **not** protect against, stated plainly so nobody over-reads it:
a process already running as this user can call `unprotect` and read the
value. This is protection at rest — against the file being read, copied or
synced — not against malware running with the user's own rights. Doing better
needs a separate secret prompt, which is a different project.

If DPAPI is unavailable (not Windows, or the API refuses), the store reports
that it cannot encrypt and the caller decides. It never silently falls back to
plaintext: a credential that is quietly written in clear after a failure is
worse than one that is refused, because the failure is invisible and the
person believes it was protected.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger("addled.mcp.secrets")

# One setting holds every protected value, so clearing MCP credentials clears
# all of them. `v1` names the format: a future change that has to re-encrypt
# old values has somewhere to notice they are old.
_CONFIG_NS = "mcp"
_CONFIG_KEY = "secrets_v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01

# A prefix that cannot occur in base64, so an encrypted record is
# distinguishable from a value somebody pasted. Both are read, for the
# migration in `_stored` below.
_PREFIX = "dpapi:"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def available() -> bool:
    """Whether this process can encrypt and decrypt on this machine."""
    return sys.platform == "win32" and hasattr(ctypes, "windll") \
        and bool(getattr(ctypes.windll, "crypt32", None))


def _blob_of(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _DataBlob) -> bytes:
    """Copy a returned blob out and free the buffer the API allocated.

    The API allocates with `LocalAlloc`; not freeing it leaks a little on every
    call, and this runs on every credential read.
    """
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(text: str) -> str:
    """Encrypt for this user. Returns a `dpapi:...` string, or raises.

    Raises rather than returning the input: a caller that cannot encrypt must
    not be handed something that looks like it worked.
    """
    if not available():
        raise RuntimeError("DPAPI is not available on this platform")
    # UTF-16-LE because the API is defined in terms of wide characters; a plain
    # UTF-8 byte string would round-trip but is not what the API documents.
    raw = str(text).encode("utf-16-le")
    in_blob = _blob_of(raw)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        raise OSError(ctypes.get_last_error() or 0,
                      "CryptProtectData refused the value")
    cipher = _take(out_blob)
    return _PREFIX + base64.b64encode(cipher).decode("ascii")


def unprotect(stored: str) -> str:
    """Decrypt a value produced by `protect`.

    A value without the prefix is returned unchanged, so a credential saved
    before this existed (or by hand) still works — see `_stored`.
    """
    text = str(stored or "")
    if not text.startswith(_PREFIX):
        return text
    if not available():
        raise RuntimeError("DPAPI is not available on this platform")
    try:
        cipher = base64.b64decode(text[len(_PREFIX):], validate=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"the stored secret is not valid base64: {e}") from e
    in_blob = _blob_of(cipher)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        # The realistic causes, in order: the file was copied from another
        # machine or account, or the Windows profile was rebuilt. Saying so is
        # more useful than an errno.
        raise OSError(
            ctypes.get_last_error() or 0,
            "the stored secret cannot be decrypted by this user on this "
            "machine — it was most likely saved by a different account or on "
            "a different computer; re-enter it")
    raw = _take(out_blob)
    return raw.decode("utf-16-le")


def is_protected(value: str) -> bool:
    return str(value or "").startswith(_PREFIX)
