"""Live end-to-end exercise of the gateway: login, cookies, static, WS gating.

This is the proof that the design works against a real socket, not just in
theory. It stands up the gateway on a free port with a temporary dashboard and
a stub upstream API, then does exactly what a remote browser would do.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_remote_gateway.py
"""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import websockets  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, method: str = "GET", headers: dict | None = None,
         cookie: str = ""):
    """Returns (status, body, set_cookie)."""
    hdr = dict(headers or {})
    if cookie:
        hdr["Cookie"] = cookie
    req = urllib.request.Request(url, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers.get("Set-Cookie") or ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Set-Cookie") or ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_noredirect(url: str, cookie: str = ""):
    """(status, location, body) without following redirects.

    `urlopen` follows 3xx silently, so a redirect to the login page would look
    like a 200 with the login page in the body — which is precisely the thing
    this suite needs to tell apart.
    """
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.headers.get("Location") or "", r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        location = (e.headers.get("Location") or "") if e.headers else ""
        return e.code, location, e.read().decode("utf-8", "replace")


async def main():
    from backend.config import config
    from backend.remote import auth, gateway as gw

    config._ensure_loaded()
    original = dict(config._data.get("remote") or {})

    tmp = Path(tempfile.mkdtemp())
    build = tmp / "out"
    (build / "_next" / "static").mkdir(parents=True)
    (build / "index.html").write_text(
        "<html><head><title>App</title></head><body>dashboard shell</body></html>",
        encoding="utf-8")
    # Next's static export writes a route as BOTH `chat/` and `chat.html`, with
    # the directory holding only RSC payloads — no index.html. The first version
    # of this fixture had only `chat.html`, so it could not catch the bug where
    # the directory branch returned None and every route fell back to the shell.
    (build / "chat").mkdir()
    (build / "chat" / "payload.txt").write_text("rsc payload", encoding="utf-8")
    (build / "chat.html").write_text("<html><body>chat page</body></html>",
                                     encoding="utf-8")
    # The other shape, where the directory does have an index.
    (build / "direct").mkdir()
    (build / "direct" / "index.html").write_text(
        "<html><body>direct page</body></html>", encoding="utf-8")
    (build / "_next" / "static" / "app.js").write_text("console.log(1)",
                                                       encoding="utf-8")
    # A secret sitting outside the build, to prove containment.
    (tmp / "secret.txt").write_text("TOP SECRET", encoding="utf-8")

    port = free_port()
    upstream_port = free_port()

    beacon: dict = {}

    async def upstream_handler(conn):
        beacon["remote"] = conn.request.headers.get("X-Addled-Remote")
        beacon["session"] = conn.request.headers.get("X-Addled-Session")
        beacon["identity"] = conn.request.headers.get("Tailscale-User-Login")
        async for message in conn:
            if message == "ping":
                await conn.send("pong-from-api")
            else:
                await conn.send(f"echo:{message}")

    config._data["remote"] = {
        "enabled": True, "port": port, "password_hash": "",
        "session_hours": 12, "idle_timeout_minutes": 60, "max_sessions": 8,
        "allow_shell": False, "allow_desktop_input": False,
        "allow_funnel": False, "trusted_origins": [],
    }
    auth.sessions.reset()
    auth.login_limiter.reset()
    auth.set_password("a-good-enough-password")

    async with serve(upstream_handler, "127.0.0.1", upstream_port):
        g = gw.RemoteGateway()
        g._dashboard = build.resolve()
        g._upstream = f"ws://127.0.0.1:{upstream_port}"
        ok, error = await g.start()
        check("the gateway starts", ok, error)
        if not ok:
            return

        base = f"http://127.0.0.1:{port}"
        try:
            # ---- 1. the login page -----------------------------------------
            status, body, _ = await asyncio.to_thread(http, base + "/login")
            check("GET /login serves the page", status == 200, f"{status} {body[:80]}")
            check("the login page is HTML", "<!DOCTYPE html>" in body, body[:60])
            check("it asks for a password", 'type="password"' in body, "")
            check("no password is echoed into the page",
                  "a-good-enough-password" not in body, "the password leaked")

            # ---- 2. nothing is served without a session --------------------
            # This used to assert the opposite: that the shell loaded with no
            # cookie. That is exactly the bug the user hit — the dashboard was
            # readable by anyone who could reach the port.
            for path in ("/", "/chat", "/_next/static/app.js"):
                status, location, body = await asyncio.to_thread(
                    http_noredirect, base + path)
                check(f"{path} is not served without a session",
                      status in (301, 302, 303, 307, 308),
                      f"status {status} — content served to an anonymous caller")
                check(f"{path} redirects to the login page",
                      location.endswith("/login"), f"Location: {location!r}")

            # ---- 3. the session API ----------------------------------------
            status, body, _ = await asyncio.to_thread(
                http, base + "/api/session", "POST", {"X-Addled-Password": "wrong"})
            check("a wrong password is refused", status == 401, f"{status} {body[:80]}")

            status, body, cookie_header = await asyncio.to_thread(
                http, base + "/api/session", "POST",
                {"X-Addled-Password": "a-good-enough-password",
                 "X-Forwarded-Proto": "https",
                 "Tailscale-User-Login": "me@example.com",
                 "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"})
            check("the right password returns 200", status == 200, f"{status} {body[:120]}")
            check("a cookie is set", "addled_session=" in cookie_header, cookie_header)
            check("the cookie is HttpOnly", "HttpOnly" in cookie_header, cookie_header)
            check("the cookie is SameSite", "SameSite=Lax" in cookie_header, cookie_header)
            check("the cookie is Secure over https",
                  "Secure" in cookie_header, cookie_header)
            check("the response body does not contain the token",
                  cookie_header.split("=")[1].split(";")[0] not in body,
                  "the session token was echoed back in JSON")

            cookie = cookie_header.split(";")[0]
            token = cookie.split("=", 1)[1]
            session_id = json.loads(body)["session"]["id"]

            # ---- 4. the session is real ------------------------------------
            status, body, _ = await asyncio.to_thread(
                http, base + "/api/session", "GET", None, cookie)
            check("the session validates", status == 200, f"{status} {body[:80]}")
            check("the session reports the tailscale user",
                  "me@example.com" in body, body[:160])

            # A Secure cookie is not sent over http by a real browser, but this
            # client sends it explicitly, which is what we want to test here.
            status, _, _ = await asyncio.to_thread(
                http, base + "/api/session", "GET", None, "addled_session=nonsense")
            check("an invented token is refused", status == 401, str(status))

            # ---- 5. static serving and containment -------------------------
            # Note the cookie: everything except /login and /api/* now needs a
            # session. The earlier version of this test asserted that the shell
            # loaded with no session — it encoded the bug as expected behaviour,
            # which is why the dashboard was readable by anyone who reached the
            # port.
            status, body, _ = await asyncio.to_thread(
                http, base + "/_next/static/app.js", "GET", None, cookie)
            check("a hashed asset is served once signed in",
                  status == 200 and "console.log" in body, f"{status} {body[:60]}")

            status, body, _ = await asyncio.to_thread(
                http, base + "/chat", "GET", None, cookie)
            check("a route serves its own page, not the app shell",
                  status == 200 and "chat page" in body, f"{status} {body[:60]}")
            check("and the socket URL is injected into it",
                  "__ADDLED_WS_URL__" in body, body[:200])

            # Without a cookie, and NOT following the redirect — urlopen would
            # happily fetch /login and report 200, which is how the earlier
            # version of this suite managed to assert the opposite.
            status, location, body = await asyncio.to_thread(
                http_noredirect, base + "/_next/static/app.js")
            check("an asset is NOT served without a session",
                  status in (301, 302, 303, 307, 308),
                  f"status {status} — the UI is readable by anyone who reaches the port")
            check("the anonymous asset request is sent to the login page",
                  location.endswith("/login"), f"Location: {location!r}")

            for attack in ("/../secret.txt", "/../../secret.txt",
                           "/%2e%2e/secret.txt", "/..%2fsecret.txt",
                           "/_next/../../secret.txt"):
                status, body, _ = await asyncio.to_thread(
                    http, base + attack, "GET", None, cookie)
                leaked = "TOP SECRET" in body
                check(f"traversal refused: {attack}", not leaked,
                      f"status={status} body={body[:80]}")

            status, body, _ = await asyncio.to_thread(
                http, base + "/missing.js", "GET", None, cookie)
            check("a missing asset is a 404, not the shell",
                  status == 404, f"{status} {body[:60]} — the Electron server returns 200 here")

            # ---- 6. the WebSocket gate -------------------------------------
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    await ws.recv()
                check("the socket refuses an unauthenticated upgrade", False,
                      "connected with no cookie")
            except websockets.exceptions.InvalidStatus as e:
                check("the socket refuses an unauthenticated upgrade",
                      e.response.status_code == 401,
                      f"status {e.response.status_code}")
            except Exception as e:  # noqa: BLE001
                check("the socket refuses an unauthenticated upgrade", False,
                      f"{type(e).__name__}: {e}")

            try:
                async with websockets.connect(
                        f"ws://127.0.0.1:{port}/ws",
                        additional_headers={"Cookie": "addled_session=madeup"}) as ws:
                    await ws.recv()
                check("the socket refuses a fabricated cookie", False, "connected")
            except websockets.exceptions.InvalidStatus as e:
                check("the socket refuses a fabricated cookie",
                      e.response.status_code == 401, f"status {e.response.status_code}")
            except Exception as e:  # noqa: BLE001
                check("the socket refuses a fabricated cookie", False,
                      f"{type(e).__name__}: {e}")

            # ---- 7. the bridge actually works ------------------------------
            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/ws",
                    additional_headers={"Cookie": cookie}) as ws:
                await ws.send("ping")
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                check("a message reaches the API and comes back",
                      reply == "pong-from-api", reply)
                await ws.send("hello")
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                check("the bridge is bidirectional", reply == "echo:hello", reply)

            check("the API is told this connection is remote",
                  beacon.get("remote") == "1", str(beacon))
            check("the API is told which session it is",
                  beacon.get("session") == session_id, str(beacon))
            check("the session id is not the session token",
                  session_id != token,
                  "the public id matches the secret token, so listing sessions would leak them")
            check("the tailscale identity is forwarded",
                  beacon.get("identity") == "me@example.com", str(beacon))

            # ---- 8. an Origin that is not allowed -------------------------
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws",
                                              origin="https://evil.example",
                                              additional_headers={"Cookie": cookie}) as ws:
                    await ws.recv()
                check("a foreign Origin is refused", False, "connected")
            except websockets.exceptions.InvalidStatus as e:
                check("a foreign Origin is refused",
                      e.response.status_code in (401, 403),
                      f"status {e.response.status_code}")
            except Exception as e:  # noqa: BLE001
                check("a foreign Origin is refused", False,
                      f"{type(e).__name__}: {e}")

            # ---- 9. signing out -------------------------------------------
            status, _, cleared = await asyncio.to_thread(
                http, base + "/api/session", "DELETE", None, cookie)
            check("signing out succeeds", status == 200, str(status))
            check("the cookie is cleared", "Max-Age=0" in cleared, cleared)
            status, _, _ = await asyncio.to_thread(
                http, base + "/api/session", "GET", None, cookie)
            check("the revoked session no longer validates", status == 401, str(status))

            # ---- 10. status -------------------------------------------------
            st = g.status()
            check("status reports it is running", st["running"] is True, str(st))
            check("status has no blockers once healthy", not st["blockers"], str(st["blockers"]))
            check("status never leaks the token",
                  token not in json.dumps(st), "the token is in the status payload")
            check("status does not expose the password hash",
                  "scrypt$" not in json.dumps(st), "the hash is in the status payload")
        finally:
            await g.stop()
            check("stopping the gateway revokes sessions",
                  auth.sessions.count() == 0,
                  f"{auth.sessions.count()} session(s) survived")

    config._data["remote"] = original
    auth.sessions.reset()
    auth.login_limiter.reset()
    shutil.rmtree(tmp, ignore_errors=True)

    # -- the request-method shape ------------------------------------------
    # `websockets.asyncio.server.Request` carries a `method` field in 17.0.1 but
    # NOT in 15.0.1, which is the version this app pins (`google_genai` needs
    # `<17`). The handler used to read `request.method` unconditionally, so on
    # the shipped version every request raised AttributeError and was swallowed.
    #
    # Both shapes are simulated here, because the one that fails cannot be
    # reached over this machine's own socket.
    class _ReqNoMethod:
        """15.0.1: path and headers only."""
        def __init__(self, path="/"):
            self.path, self.headers = path, {}

    class _ReqWithMethod:
        """17.0.1: also has `method`."""
        def __init__(self, method, path="/"):
            self.path, self.headers, self.method = path, {}, method

    # An unknown method must NOT be reported as a method. `getattr(req,
    # "method", "GET")` would fail this, which is why the code returns None.
    check("an absent request method reads as unknown, not as GET",
          gw.gateway._request_method(_ReqNoMethod()) is None,
          f"got {gw.gateway._request_method(_ReqNoMethod())!r}")

    check("a present request method is read and upper-cased",
          gw.gateway._request_method(_ReqWithMethod("post")) == "POST",
          f"got {gw.gateway._request_method(_ReqWithMethod('post'))!r}")

    # With no method to read, the request is refused rather than assumed to be
    # a read -- otherwise a write would pass straight through the 405 gates.
    r = await gw.gateway._process_request(None, _ReqNoMethod("/dashboard"))
    check("an unreadable method is refused, not treated as a read",
          r is not None and r.status_code == 501,
          f"status {getattr(r, 'status_code', None)}")

    # And the gates that depend on the method still refuse writes when the
    # method IS readable -- the regression `assuming GET` would have caused.
    r = await gw.gateway._process_request(None, _ReqWithMethod("POST", "/login"))
    check("a POST to the login page is still refused",
          r is not None and r.status_code == 405,
          f"status {getattr(r, 'status_code', None)}")


try:
    asyncio.run(main())
finally:
    pass

print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
