"""
Password and session handling for remote access.

Deliberately boring, and deliberately dependency-free: scrypt from the standard
library for the password, a comparison that cannot leak timing, and sessions
held in memory only. That last part is a security property rather than an
oversight — restarting Addled logs every remote device out.

Two things here are easy to get wrong and are handled explicitly:

* The session **token** is never handed back to the dashboard. The Remote page
  lists sessions by a separate public id, so reading that list over the API
  cannot be turned into hijacking one.
* Login attempts are throttled per address, because a password on a network
  endpoint with unlimited guesses is not a password.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger("addled.remote.auth")

# scrypt: n=2**14, r=8 is ~16 MB and tens of milliseconds on a desktop CPU.
# Expensive enough that offline guessing hurts, cheap enough that a login does
# not feel slow. The parameters are stored alongside the hash so raising them
# later does not lock anyone out.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
SCHEME = "scrypt"

# Unambiguous alphabet: no O/0, I/l/1, so a password read off a screen can be
# typed back correctly.
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_GROUPS = 5
PASSWORD_GROUP_LEN = 4

COOKIE_NAME = "addled_session"


# -- passwords ----------------------------------------------------------------


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt_hex$hash_hex`."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM)
    return (f"{SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
            f"{salt.hex()}${digest.hex()}")


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. Anything malformed is a plain False, never a raise.

    A stored value we cannot parse must not become a crash on the login path,
    and it must not become an accidental accept either.
    """
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = str(stored).split("$")
        if scheme != SCHEME:
            log.warning("Stored password uses an unknown scheme: %r", scheme)
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=SCRYPT_MAXMEM)
    except (ValueError, TypeError, MemoryError) as e:
        log.warning("Stored password is unusable (%s) — refusing the login", e)
        return False
    return hmac.compare_digest(digest, expected)


def generate_password() -> str:
    """A strong password the user can read off a screen and retype.

    ~103 bits from an alphabet with the lookalike characters removed.
    """
    groups = [
        "".join(secrets.choice(PASSWORD_ALPHABET)
                for _ in range(PASSWORD_GROUP_LEN))
        for _ in range(PASSWORD_GROUPS)
    ]
    return "-".join(groups)


def _setting(key: str, default):
    try:
        from backend.config import config
        return config.get("remote", key, default=default)
    except Exception:
        return default


def password_is_set() -> bool:
    """Whether a password exists. Remote access must never open without one."""
    return bool(str(_setting("password_hash", "") or "").strip())


def set_password(password: str) -> dict:
    """Store a new password, and drop every existing session.

    Changing the password has to end the sessions: otherwise a device that is
    already logged in stays logged in, and the change means nothing to whoever
    had access.
    """
    text = str(password or "")
    if len(text) < 8:
        return {"success": False,
                "error": "Use at least 8 characters — this guards your whole "
                         "machine, not just a screen."}
    try:
        from backend.config import config
        config.set("remote", "password_hash", value=hash_password(text))
    except Exception as e:
        return {"success": False, "error": f"Could not save the password: {e}"}
    dropped = sessions.revoke_all()
    log.info("Remote password changed; %d session(s) dropped", dropped)
    return {"success": True, "sessionsDropped": dropped}


def verify_login(password: str) -> bool:
    stored = str(_setting("password_hash", "") or "")
    return verify_password(str(password or ""), stored)


# -- sessions -----------------------------------------------------------------


@dataclass
class Session:
    id: str
    token: str
    created: float
    last_seen: float
    remote_addr: str = ""
    user_agent: str = ""
    tailscale_user: str = ""
    tailscale_device: str = ""

    def absolute_expiry(self) -> float:
        return self.created + float(_setting("session_hours", 12) or 12) * 3600

    def idle_expiry(self) -> float:
        minutes = float(_setting("idle_timeout_minutes", 60) or 60)
        return self.last_seen + minutes * 60

    def expired(self, now: float | None = None) -> str | None:
        """None if valid, else why it lapsed."""
        now = now if now is not None else time.time()
        if now >= self.absolute_expiry():
            return "expired"
        if now >= self.idle_expiry():
            return "idle"
        return None

    def brief(self) -> str:
        return self.user_agent.split(")")[0].split("(")[-1].strip()[:40]

    def public(self) -> dict:
        """What the dashboard may see. Never the token."""
        now = time.time()
        return {
            "id": self.id,
            "created": self.created,
            "last_seen": self.last_seen,
            "age_s": int(now - self.created),
            "idle_s": int(now - self.last_seen),
            "expires_in_s": max(0, int(self.absolute_expiry() - now)),
            "remote_addr": self.remote_addr,
            "user_agent": self.user_agent[:160],
            "browser": self.brief(),
            "tailscale_user": self.tailscale_user,
            "tailscale_device": self.tailscale_device,
        }


class SessionStore:
    """In-memory sessions. A restart logs everyone out, by design."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def _max(self) -> int:
        try:
            return max(1, int(_setting("max_sessions", 8) or 8))
        except (TypeError, ValueError):
            return 8

    def _sweep(self) -> int:
        stale = [t for t, s in self._sessions.items() if s.expired()]
        for token in stale:
            self._sessions.pop(token, None)
        return len(stale)

    def create(self, remote_addr: str = "", user_agent: str = "",
               tailscale_user: str = "", tailscale_device: str = "") -> Session:
        self._sweep()
        # Oldest first eviction, so the cap cannot lock the user out of their
        # own current device.
        while len(self._sessions) >= self._max():
            oldest = min(self._sessions.values(), key=lambda s: s.created)
            self._sessions.pop(oldest.token, None)
            log.info("Session cap reached; dropped session %s", oldest.id)
        now = time.time()
        session = Session(
            id=secrets.token_hex(6),
            token=secrets.token_urlsafe(32),
            created=now,
            last_seen=now,
            remote_addr=str(remote_addr or ""),
            user_agent=str(user_agent or ""),
            tailscale_user=str(tailscale_user or ""),
            tailscale_device=str(tailscale_device or ""),
        )
        self._sessions[session.token] = session
        log.info("Remote session %s opened from %s", session.id,
                 session.remote_addr or "?")
        return session

    def get(self, token: str, touch: bool = True) -> Session | None:
        """The session for a token, or None. Touches `last_seen` by default."""
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        reason = session.expired()
        if reason:
            self._sessions.pop(token, None)
            log.info("Session %s closed (%s)", session.id, reason)
            return None
        if touch:
            session.last_seen = time.time()
        return session

    def revoke(self, session_id: str) -> bool:
        for token, session in list(self._sessions.items()):
            if session.id == session_id:
                self._sessions.pop(token, None)
                log.info("Session %s revoked", session_id)
                return True
        return False

    def revoke_all(self) -> int:
        count = len(self._sessions)
        self._sessions.clear()
        return count

    def list(self) -> list[dict]:
        self._sweep()
        return [s.public() for s in
                sorted(self._sessions.values(), key=lambda s: -s.last_seen)]

    def count(self) -> int:
        return len(self._sessions)

    def reset(self) -> None:
        """For tests."""
        self._sessions.clear()


# -- login throttling ---------------------------------------------------------


class LoginLimiter:
    """Per-address backoff on failed logins.

    In-memory: a restart clears it, which is fine — an attacker cannot restart
    the app, and the alternative is persisting failure counts for no gain.
    """

    def __init__(self, max_failures: int = 5, window_s: float = 900,
                 lockout_s: float = 300):
        self.max_failures = max_failures
        self.window_s = window_s
        self.lockout_s = lockout_s
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, addr: str) -> float:
        """Seconds the caller must wait. 0 means allowed."""
        now = time.time()
        until = self._locked_until.get(addr, 0.0)
        if until > now:
            return round(until - now, 1)
        if until:
            self._locked_until.pop(addr, None)
        return 0.0

    def record_failure(self, addr: str) -> float:
        """Returns the retry-after that now applies (0 if still free)."""
        now = time.time()
        recent = [t for t in self._failures.get(addr, []) if now - t < self.window_s]
        recent.append(now)
        self._failures[addr] = recent
        if len(recent) >= self.max_failures:
            self._locked_until[addr] = now + self.lockout_s
            self._failures.pop(addr, None)
            log.warning("Login lockout for %s after %d failures (%ds)",
                        addr, len(recent), int(self.lockout_s))
            return float(self.lockout_s)
        return 0.0

    def record_success(self, addr: str) -> None:
        self._failures.pop(addr, None)
        self._locked_until.pop(addr, None)

    def reset(self) -> None:
        """For tests."""
        self._failures.clear()
        self._locked_until.clear()


sessions = SessionStore()
login_limiter = LoginLimiter()
