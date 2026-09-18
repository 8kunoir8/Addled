"""Auth checks: passwords, sessions, throttling, and the refusals.

The failure modes matter more than the happy path here, so most of this is
about what must be *rejected*: a malformed stored hash, a wrong password, an
expired session, a superceded session after a password change, and a brute-force
attempt. Config is mutated in memory and restored in a finally.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_remote.py
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def run():
    from backend.config import config
    from backend.remote import auth

    config._ensure_loaded()
    original = dict(config._data.get("remote") or {})
    config._data["remote"] = {
        "enabled": True, "port": 9878, "password_hash": "",
        "session_hours": 12, "idle_timeout_minutes": 60, "max_sessions": 8,
        "allow_shell": False, "allow_desktop_input": False,
        "allow_funnel": False, "trusted_origins": [],
    }

    auth.sessions.reset()
    auth.login_limiter.reset()

    try:
        # ---- 1. hashing round-trip ----------------------------------------
        stored = auth.hash_password("correct horse battery staple")
        check("a hash is scrypt", stored.startswith("scrypt$"), stored[:20])
        check("the parameters travel with the hash",
              stored.count("$") == 5, stored)
        check("the raw password is not in the stored value",
              "correct horse" not in stored, "the password leaked into the hash")
        check("the right password verifies",
              auth.verify_password("correct horse battery staple", stored) is True, "")
        check("a wrong password does not",
              auth.verify_password("correct horse battery stapl", stored) is False, "")
        check("an empty password does not",
              auth.verify_password("", stored) is False, "")
        check("the same password hashes differently each time",
              auth.hash_password("x" * 12) != auth.hash_password("x" * 12),
              "no per-hash salt")

        # ---- 2. a malformed stored hash must refuse, not raise ------------
        for bad in ("", "garbage", "scrypt$notanint$8$1$aa$bb",
                    "bcrypt$16384$8$1$aa$bb", "scrypt$16384$8$1$zz$bb"):
            try:
                result = auth.verify_password("anything", bad)
                check(f"malformed hash refused ({bad[:18]!r})", result is False,
                      f"returned {result!r}")
            except Exception as e:  # noqa: BLE001
                check(f"malformed hash did not raise ({bad[:18]!r})", False,
                      f"{type(e).__name__}: {e}")

        # ---- 3. constant-time comparison is actually used -----------------
        source = open(os.path.join(ROOT, "backend", "remote", "auth.py"),
                      encoding="utf-8").read()
        check("verification uses compare_digest",
              "hmac.compare_digest" in source,
              "a plain == on a secret leaks timing")

        # ---- 4. generated passwords are strong and typable ----------------
        pw = auth.generate_password()
        check("a generated password is long enough", len(pw) >= 20, pw)
        check("no lookalike characters", not (set(pw) & set("O0Il1")), pw)
        check("two generated passwords differ",
              auth.generate_password() != auth.generate_password(), "")

        # ---- 5. password_is_set / set_password ---------------------------
        check("no password is reported before one is set",
              auth.password_is_set() is False, "")
        too_short = auth.set_password("short")
        check("a short password is refused", too_short.get("success") is False, str(too_short))
        check("still no password", auth.password_is_set() is False, "")
        ok = auth.set_password("a-good-enough-password")
        check("a real password is accepted", ok.get("success") is True, str(ok))
        check("and is reported as set", auth.password_is_set() is True, "")
        check("verify_login accepts it",
              auth.verify_login("a-good-enough-password") is True, "")
        check("verify_login rejects a wrong one",
              auth.verify_login("a-good-enough-passworD") is False, "")

        # ---- 6. sessions -------------------------------------------------
        auth.sessions.reset()
        s1 = auth.sessions.create(remote_addr="100.64.0.9", user_agent="Mozilla/5.0 (iPhone)",
                                  tailscale_user="me@example.com",
                                  tailscale_device="phone")
        check("a fresh session validates", auth.sessions.get(s1.token) is not None, "")
        check("an unknown token does not", auth.sessions.get("nope") is None, "")
        check("an empty token does not", auth.sessions.get(None) is None, "")
        check("the token is not a trivial value", len(s1.token) >= 32, str(len(s1.token)))

        public = auth.sessions.list()[0]
        check("the public view never contains the token",
              "token" not in public, str(public.keys()))
        check("but it does identify the session", public.get("id") == s1.id, str(public))
        check("and carries the tailscale identity",
              public.get("tailscale_user") == "me@example.com", str(public))

        # idle expiry
        config._data["remote"]["idle_timeout_minutes"] = 60
        s1.last_seen = time.time() - 3601
        check("an idle session lapses", auth.sessions.get(s1.token) is None, "")
        check("and is removed from the list", auth.sessions.count() == 0, "")
        config._data["remote"]["idle_timeout_minutes"] = 60

        # absolute expiry, even with a fresh last_seen
        s2 = auth.sessions.create(remote_addr="100.64.0.10")
        config._data["remote"]["session_hours"] = 1
        s2.created = time.time() - 3601
        s2.last_seen = time.time()
        check("an old session lapses even if it was just used",
              auth.sessions.get(s2.token) is None,
              "absolute expiry is not enforced independently of idle")
        config._data["remote"]["session_hours"] = 12

        # revoke
        s3 = auth.sessions.create(remote_addr="100.64.0.11")
        check("revoke reports success", auth.sessions.revoke(s3.id) is True, "")
        check("the revoked token stops working", auth.sessions.get(s3.token) is None, "")
        check("revoking a missing id is False", auth.sessions.revoke("nope") is False, "")

        # cap
        auth.sessions.reset()
        config._data["remote"]["max_sessions"] = 3
        made = [auth.sessions.create(remote_addr=f"100.64.0.{i}") for i in range(5)]
        check("the session cap is enforced", auth.sessions.count() == 3,
              f"{auth.sessions.count()} sessions")
        check("the newest sessions are the ones kept",
              auth.sessions.get(made[-1].token) is not None,
              "the cap evicted the session the user was actively using")
        config._data["remote"]["max_sessions"] = 8

        # ---- 7. changing the password ends existing sessions --------------
        auth.sessions.reset()
        auth.sessions.create(remote_addr="100.64.0.20")
        auth.sessions.create(remote_addr="100.64.0.21")
        changed = auth.set_password("another-good-password")
        check("changing the password drops sessions",
              changed.get("sessionsDropped") == 2 and auth.sessions.count() == 0,
              str(changed))

        # ---- 8. login throttling ------------------------------------------
        limiter = auth.LoginLimiter(max_failures=3, window_s=900, lockout_s=300)
        check("a clean address is allowed", limiter.retry_after("1.2.3.4") == 0, "")
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        check("two failures are not yet a lockout",
              limiter.retry_after("1.2.3.4") == 0, "")
        limiter.record_failure("1.2.3.4")
        wait = limiter.retry_after("1.2.3.4")
        check("the third failure locks out", wait > 0, f"retry_after={wait}")
        check("the lockout is per address",
              limiter.retry_after("5.6.7.8") == 0,
              "one attacker's failures locked out everyone")
        limiter.record_success("1.2.3.4")
        check("a success clears the failures", limiter.retry_after("1.2.3.4") == 0, "")

        # ---- 9. the exposure guard ----------------------------------------
        # Remote must never open without a credential; the gateway and the
        # serve toggle both rely on this one predicate.
        config._data["remote"]["password_hash"] = ""
        check("no password reads as not set", auth.password_is_set() is False, "")
        config._data["remote"]["password_hash"] = stored
        check("a password reads as set", auth.password_is_set() is True, "")
    finally:
        config._data["remote"] = original
        auth.sessions.reset()
        auth.login_limiter.reset()


run()
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
