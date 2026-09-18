"""
The authenticated gateway: one loopback port that serves the dashboard and
bridges an authenticated WebSocket to the loopback API.

Why a gateway at all, rather than authenticating the API directly: the
WebSocket server has 138 handlers written on the assumption that only this
machine can reach it, several of which would be catastrophic if exposed (it can
run shell commands and synthesise input). Adding a credential check to each
handler would mean trusting 138 call sites to remember it. Instead, nothing
remote ever talks to the API directly — it talks to this, and this only bridges
after a session is validated.

Tailscale Serve terminates TLS and proxies to this port, so the browser sees a
real `https://<machine>.<tailnet>.ts.net` URL, `wss://` works, and Addled never
handles a certificate. The process itself only ever binds loopback.

Two facts about the library this is built on, both verified by running probes
rather than assumed (websockets 17.0.1):

* `process_request` can answer a plain HTTP request *and* gate a WebSocket
  upgrade on the same port, so the login page, the dashboard and the socket
  share one listener — and one `tailscale serve` mapping.
* The handshake parser does not read request bodies, and a request that carries
  one is closed without a response. So the login endpoint takes the password in
  a header, never a body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend.remote import auth

log = logging.getLogger("addled.remote.gateway")

LOOPBACK = "127.0.0.1"
WS_UPSTREAM = "ws://127.0.0.1:9876"
WS_PATH = "/ws"
LOGIN_PATH = "/login"

# Large enough for a base64 image attachment, the same ceiling the WS server
# uses for the same reason.
MAX_MESSAGE = 64 * 1024 * 1024
MAX_ASSET = 16 * 1024 * 1024

# The Electron shell serves the dashboard from 3001 and talks to 9876 directly.
# The gateway serves the same build from its own origin, so it tells the page
# where to connect instead of shipping two builds.
WS_GLOBAL = '<script>window.__ADDLED_WS_URL__="/ws";</script>'

NO_STORE = ("Cache-Control", "no-store")

# Header names. The password goes in a header because the handshake path cannot
# read a body; Authorization is accepted too so this is curl-able.
PW_HEADER = "X-Addled-Password"
IDENTITY_HEADERS = {
    "user": "Tailscale-User-Login",
    "name": "Tailscale-User-Name",
    "device": "Tailscale-Node-Name",
}


def _cfg(key: str, default=None):
    try:
        from backend.config import config
        return config.get("remote", key, default=default)
    except Exception:
        return default


def dashboard_dir() -> Path | None:
    """Find the built dashboard.

    Dev has it at `<repo>/dashboard/out`; a packaged build has it at
    `<resources>/dashboard` (electron-builder `extraResources`). Both are
    checked by looking for `index.html`, so an empty folder is not mistaken for
    a build.
    """
    here = Path(__file__).resolve()
    # backend/remote/gateway.py -> backend/remote -> backend -> <root>
    root = here.parents[2]
    candidates = [root / "dashboard" / "out", root / "dashboard"]
    extra = os.environ.get("ADDLED_RESOURCES")
    if extra:
        candidates += [Path(extra) / "dashboard", Path(extra) / "dashboard" / "out"]
    for base in candidates:
        try:
            if (base / "index.html").is_file():
                return base
        except OSError:
            continue
    return None


def _client_ip(conn, request) -> str:
    """The real client address.

    Everything arrives from tailscaled on loopback, so the peer address is
    always 127.0.0.1 and useless for throttling. Serve sets X-Forwarded-For.
    Trusting that header is safe here because the only route to this port is
    the local proxy — a local process that can spoof it is already local.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    addr = getattr(conn, "remote_address", None)
    return str(addr[0]) if addr else "?"


def _is_secure(request) -> bool:
    """Whether the browser reached us over TLS, via the proxy's header."""
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    return proto.lower() == "https"


def _cookie_from(headers) -> str:
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == auth.COOKIE_NAME:
            return value.strip()
    return ""


def _session_from(request) -> auth.Session | None:
    token = _cookie_from(request.headers)
    # touch=False: a WebSocket handshake is not evidence of use for throttling
    # the idle clock — the socket's own traffic is.
    return auth.sessions.get(token, touch=False)


def _json_response(status: int, payload: dict, headers: list | None = None) -> Response:
    body = json.dumps(payload).encode("utf-8")
    base = [("Content-Type", "application/json"), NO_STORE]
    return Response(status, _reason(status), Headers(base + list(headers or [])), body)


_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 405: "Method Not Allowed", 413: "Payload Too Large",
            429: "Too Many Requests", 500: "Internal Server Error",
            503: "Service Unavailable"}


def _reason(status: int) -> str:
    return _REASONS.get(status, "OK")


class RemoteGateway:
    """Owns the remote HTTP surface and the WebSocket bridge."""

    def __init__(self):
        self._server = None
        self._port: int | None = None
        self._dashboard: Path | None = None
        self._upstream = WS_UPSTREAM
        self._started_at = 0.0
        self._last_error = ""
        self._bridges = 0

    # -- configuration --------------------------------------------------------

    def enabled(self) -> bool:
        return bool(_cfg("enabled", False))

    def port(self) -> int:
        try:
            return int(_cfg("port", 9878) or 9878)
        except (TypeError, ValueError):
            return 9878

    def trusted_origins(self) -> list[str]:
        raw = _cfg("trusted_origins", []) or []
        if isinstance(raw, str):
            raw = [line.strip() for line in raw.splitlines()]
        return [str(x).strip() for x in raw if str(x).strip()]

    def origins(self) -> list:
        """Accepted `Origin` values for the WebSocket handshake.

        `None` in the list means "no Origin header", which is what the Node bot
        bridges and any non-browser client send. A page in a browser always
        sends one, so this is the check that stops another site's JavaScript
        from driving Addled.
        """
        patterns: list = [None]
        patterns += [re.compile(p) for p in self.trusted_origins()]
        # The tailnet's own https name, which is what Serve serves.
        patterns.append(re.compile(r"https?://([a-z0-9-]+\.)+ts\.net(:\d+)?$"))
        # This machine's own dashboard: the Electron shell and the dev server.
        patterns += [
            re.compile(r"https?://127\.0\.0\.1(:\d+)?$"),
            re.compile(r"https?://localhost(:\d+)?$"),
            re.compile(r"https?://\[::1\](:\d+)?$"),
        ]
        return patterns

    # -- lifecycle ------------------------------------------------------------

    def is_running(self) -> bool:
        return self._server is not None

    async def start(self) -> tuple[bool, str]:
        """Begin listening. Returns (ok, error)."""
        if self._server is not None:
            return True, ""
        if not self.enabled():
            return False, "Remote access is turned off."
        # The one rule that must never be relaxed: no password, no exposure.
        if not auth.password_is_set():
            return False, ("Set a remote password first — Addled will not open "
                           "remote access without one.")

        found = self._dashboard or dashboard_dir()
        if found is None:
            return False, ("The dashboard build was not found (expected "
                           "dashboard/out). Run the dashboard build first.")
        self._dashboard = Path(found).resolve()
        port = self.port()

        try:
            self._server = await serve(
                self._handle_socket,
                LOOPBACK,
                port,
                origins=self.origins(),
                process_request=self._process_request,
                max_size=MAX_MESSAGE,
            )
        except OSError as e:
            self._server = None
            self._last_error = f"Could not listen on {LOOPBACK}:{port} — {e}"
            log.warning("Gateway failed to start: %s", e)
            return False, self._last_error

        self._port = port
        self._started_at = time.time()
        self._last_error = ""
        log.info("Remote gateway listening on http://%s:%d (dashboard: %s)",
                 LOOPBACK, port, self._dashboard)
        return True, ""

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception as e:  # noqa: BLE001
                log.debug("Gateway close: %s", e)
            self._server = None
            log.info("Remote gateway stopped")
        # Turning remote access off has to end the sessions, or a device that
        # is already in stays in.
        dropped = auth.sessions.revoke_all()
        if dropped:
            log.info("Dropped %d session(s) with the gateway", dropped)

    async def boot(self) -> None:
        """Start only if the user asked for it. Never opens access on its own."""
        if not self.enabled():
            return
        ok, error = await self.start()
        if not ok:
            log.info("Remote gateway not started: %s", error)

    # -- request routing ------------------------------------------------------

    async def _process_request(self, conn, request) -> Response | None:
        """Answer HTTP, or return None to let a WebSocket upgrade proceed."""
        try:
            path = unquote((request.path or "/").split("?")[0])
            method = (request.method or "GET").upper()

            if path == WS_PATH:
                return self._authorize_socket(conn, request)

            if path == LOGIN_PATH:
                if method not in ("GET", "HEAD"):
                    return _json_response(405, {"error": "Use GET."})
                return await self._login_page()

            if path.startswith("/api/"):
                return await self._api(conn, request, path, method)

            if method not in ("GET", "HEAD"):
                return _json_response(405, {"error": f"{method} is not supported."})

            # Everything past this point needs a session. The dashboard is only a
            # static bundle, but serving it to anyone who can reach the port hands
            # over the whole UI, and gating just the socket left the shell visible
            # — which reads as a working, empty app rather than a locked door.
            if _session_from(request) is None:
                return self._redirect_to_login()

            return await self._static(path, head=(method == "HEAD"))
        except Exception as e:  # noqa: BLE001
            # A raised exception here would drop the connection with no reply,
            # which is indistinguishable from Addled being down.
            log.exception("Gateway request failed: %s", e)
            return _json_response(500, {"error": "Gateway error."})

    # -- http: the login page and the session API -----------------------------

    def _redirect_to_login(self) -> Response:
        """Send a browser to the login form.

        A redirect rather than a 401 with a body, because these are browser
        navigations: the user needs to end up looking at a form, not at an error
        page. No return-to parameter — that would be an open-redirect to keep
        safe, and the app shell sends you on to /chat anyway.
        """
        return Response(302, "Found", Headers([
            ("Location", LOGIN_PATH),
            NO_STORE,
            ("Referrer-Policy", "no-referrer"),
        ]), b"")

    async def _login_page(self) -> Response:
        page = Path(__file__).resolve().parent / "login.html"
        try:
            body = await asyncio.to_thread(page.read_bytes)
        except OSError as e:
            log.warning("Login page unreadable: %s", e)
            return _json_response(500, {"error": "The login page is missing."})
        return Response(200, "OK",
                        Headers([("Content-Type", "text/html; charset=utf-8"),
                                 NO_STORE,
                                 ("X-Frame-Options", "DENY"),
                                 ("Referrer-Policy", "no-referrer")]),
                        body)

    async def _api(self, conn, request, path: str, method: str) -> Response:
        if path == "/api/ping":
            # Deliberately unauthenticated and contentless: a probe that
            # confirms reachability without confirming anything about state.
            return _json_response(200, {"ok": True, "addled": "remote"})

        if path == "/api/session":
            if method == "POST":
                return self._login(conn, request)
            if method == "GET":
                session = _session_from(request)
                if session is None:
                    return _json_response(401, {"error": "Not signed in."})
                return _json_response(200, {"session": session.public()})
            if method == "DELETE":
                return self._logout(request)
            return _json_response(405, {"error": f"{method} is not supported."})

        return _json_response(404, {"error": "No such endpoint."})

    def _login(self, conn, request) -> Response:
        if not auth.password_is_set():
            return _json_response(503, {
                "error": "Remote access has no password set. Set one in Addled "
                         "under Remote before signing in."})

        addr = _client_ip(conn, request)
        wait = auth.login_limiter.retry_after(addr)
        if wait:
            return _json_response(
                429, {"error": f"Too many attempts. Try again in {int(wait)}s."},
                [("Retry-After", str(int(wait)))])

        password = request.headers.get(PW_HEADER) or ""
        if not password:
            bearer = request.headers.get("Authorization") or ""
            if bearer.lower().startswith("bearer "):
                password = bearer[7:].strip()

        if not auth.verify_login(password):
            lock = auth.login_limiter.record_failure(addr)
            log.warning("Failed remote login from %s", addr)
            if lock:
                return _json_response(
                    429, {"error": f"Too many attempts. Try again in {int(lock)}s."},
                    [("Retry-After", str(int(lock)))])
            return _json_response(401, {"error": "That password is not right."})

        auth.login_limiter.record_success(addr)
        session = auth.sessions.create(
            remote_addr=addr,
            user_agent=request.headers.get("User-Agent") or "",
            # Informational only. This is never the credential: a local process
            # can send the same headers, so it is displayed to the user, not
            # trusted.
            tailscale_user=request.headers.get(IDENTITY_HEADERS["user"]) or "",
            tailscale_device=request.headers.get(IDENTITY_HEADERS["device"]) or "",
        )
        try:
            max_age = int(float(_cfg("session_hours", 12) or 12) * 3600)
        except (TypeError, ValueError):
            max_age = 12 * 3600

        cookie = (f"{auth.COOKIE_NAME}={session.token}; Path=/; HttpOnly; "
                  f"SameSite=Lax; Max-Age={max_age}")
        if _is_secure(request):
            cookie += "; Secure"

        return _json_response(200, {"ok": True, "session": session.public()},
                              [("Set-Cookie", cookie)])

    def _logout(self, request) -> Response:
        token = _cookie_from(request.headers)
        if token:
            session = auth.sessions.get(token, touch=False)
            if session is not None:
                auth.sessions.revoke(session.id)
        expired = (f"{auth.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; "
                   f"Max-Age=0")
        return _json_response(200, {"ok": True}, [("Set-Cookie", expired)])

    # -- http: static dashboard ----------------------------------------------

    def _resolve_asset(self, url_path: str) -> Path | None:
        """Map a URL to a file inside the dashboard, or None.

        Containment is checked on the *resolved* path, which is what makes
        `/../backend/memory/settings.json` fail: `..` is collapsed first, and
        joining an absolute path (which `Path` allows) is caught by the same
        check. Resolving also catches a symlink pointing out of the build.
        """
        if self._dashboard is None:
            return None
        rel = url_path.lstrip("/")
        if "\0" in rel:
            return None
        if not rel:
            rel = "index.html"
        try:
            candidate = (self._dashboard / rel).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if candidate != self._dashboard and not candidate.is_relative_to(self._dashboard):
            log.warning("Refused a path outside the dashboard: %r", url_path)
            return None
        if candidate.is_dir():
            index = candidate / "index.html"
            if index.is_file():
                return index
        elif candidate.is_file():
            return candidate
        # Next's static export writes a route as BOTH `chat/` and `chat.html`,
        # and the directory holds only RSC payloads — not an index.html. Returning
        # None as soon as the directory had no index meant every route fell
        # through to the app shell, so /chat, /settings and /remote all rendered
        # the root page, which then redirected to /chat, which rendered the root
        # page again.
        if not candidate.suffix:
            sibling = candidate.with_suffix(".html")
            if sibling.is_file():
                return sibling
        return None

    async def _static(self, url_path: str, head: bool = False) -> Response:
        if self._dashboard is None:
            return _json_response(503, {"error": "The dashboard is not available."})

        asset = self._resolve_asset(url_path)
        if asset is None:
            leaf = url_path.rstrip("/").rsplit("/", 1)[-1]
            # A missing *asset* is a 404. Only a route (no file extension) falls
            # back to the app shell — the Electron server returns index.html
            # with a 200 for anything missing, which hides real 404s.
            if leaf and "." in leaf:
                return _json_response(404, {"error": "Not found."})
            asset = self._resolve_asset("index.html")
            if asset is None:
                return _json_response(404, {"error": "Not found."})

        try:
            if asset.stat().st_size > MAX_ASSET:
                return _json_response(413, {"error": "That file is too large."})
            body = await asyncio.to_thread(asset.read_bytes)
        except OSError as e:
            log.debug("Asset unreadable (%s): %s", asset, e)
            return _json_response(404, {"error": "Not found."})

        is_html = asset.suffix.lower() in (".html", ".htm")
        if is_html:
            # Tells the page where its socket lives. Done at serve time so the
            # same build works inside Electron, which needs the untouched file.
            text = body.decode("utf-8", errors="replace")
            if "__ADDLED_WS_URL__" not in text:
                text = (text.replace("</head>", WS_GLOBAL + "</head>", 1)
                        if "</head>" in text else WS_GLOBAL + text)
            body = text.encode("utf-8")

        ctype = mimetypes.guess_type(str(asset))[0] or "application/octet-stream"
        if is_html or asset.suffix.lower() in (".json", ".js", ".css"):
            if is_html:
                ctype = "text/html; charset=utf-8"
        headers = Headers([
            ("Content-Type", ctype),
            # HTML and the app shell must not be cached: they carry the
            # injected socket URL. Hashed assets may be.
            ("Cache-Control", "no-store") if is_html
            else ("Cache-Control", "public, max-age=3600"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return Response(200, "OK", headers, b"" if head else body)

    # -- websocket ------------------------------------------------------------

    def _authorize_socket(self, conn, request) -> Response | None:
        """Gate the upgrade. None lets it proceed."""
        if not self.enabled():
            return _json_response(503, {"error": "Remote access is turned off."})
        session = _session_from(request)
        if session is None:
            # 401 rather than 403 so the dashboard can tell "sign in" apart from
            # "this browser is not allowed".
            return _json_response(401, {"error": "Sign in first."})
        # Stash it for the handler and the audit log; the handler is where the
        # session proves it was validated.
        conn.addled_session = session
        conn.addled_addr = _client_ip(conn, request)
        return None

    async def _handle_socket(self, conn) -> None:
        """Bridge an authenticated socket to the loopback API."""
        session = getattr(conn, "addled_session", None)
        if session is None:
            # Unreachable while _authorize_socket gates the path, but the
            # bridge must never run unauthenticated if that ever changes.
            await conn.close(1011, "unauthenticated")
            return

        upstream = None
        self._bridges += 1
        try:
            upstream = await ws_connect(
                self._upstream,
                max_size=MAX_MESSAGE,
                # Tell the loopback server this is not a local caller. It cannot
                # tell from the peer address, because we connect from loopback.
                additional_headers={
                    "X-Addled-Remote": "1",
                    "X-Addled-Session": session.id,
                    IDENTITY_HEADERS["user"]: session.tailscale_user,
                    IDENTITY_HEADERS["device"]: session.tailscale_device,
                },
            )
        except Exception as e:  # noqa: BLE001
            self._bridges -= 1
            log.warning("Could not reach the Addled API for session %s: %s",
                        session.id, e)
            await conn.close(1011, "addled api unreachable")
            return

        log.info("Remote session %s bridged (from %s)", session.id,
                 getattr(conn, "addled_addr", "?"))

        async def client_to_api():
            async for message in conn:
                await upstream.send(message)

        async def api_to_client():
            async for message in upstream:
                await conn.send(message)

        pumpers = [asyncio.create_task(client_to_api()),
                   asyncio.create_task(api_to_client())]
        try:
            done, pending = await asyncio.wait(
                pumpers, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    log.debug("Bridge ended: %s", exc)
        finally:
            self._bridges -= 1
            for closer in (conn, upstream):
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("Remote session %s disconnected", session.id)

    # -- status ---------------------------------------------------------------

    def remote_url_hint(self) -> str:
        """The path Serve should expose. The host comes from Tailscale."""
        return "/"

    def status(self) -> dict:
        found = dashboard_dir()
        blockers = []
        if not self.enabled():
            blockers.append("Remote access is turned off.")
        if not auth.password_is_set():
            blockers.append("No password is set. Addled will not open remote "
                            "access without one.")
        if found is None:
            blockers.append("The dashboard build was not found "
                            "(dashboard/out is missing).")
        if self.enabled() and auth.password_is_set() and found is not None \
                and not self.is_running():
            blockers.append("The gateway is not running.")
        return {
            "enabled": self.enabled(),
            "running": self.is_running(),
            "port": self._port or self.port(),
            "bind": f"{LOOPBACK}:{self._port or self.port()}",
            "started_at": self._started_at,
            "uptime_s": int(time.time() - self._started_at) if self._started_at else 0,
            "dashboard_dir": str(found) if found else "",
            "dashboard_found": found is not None,
            "password_set": auth.password_is_set(),
            "sessions": auth.sessions.list(),
            "session_count": auth.sessions.count(),
            "bridges": self._bridges,
            "trusted_origins": self.trusted_origins(),
            "error": self._last_error,
            "blockers": blockers,
        }


gateway = RemoteGateway()
