"""The loopback half of an OAuth authorization-code flow.

Every desktop OAuth client needs the same three things, and the Google Calendar
integration here had none of them:

* **A redirect that actually lands.** It pointed the browser at
  `http://localhost:9876/oauth/callback`, and 9876 is the WebSocket listener —
  a browser arriving there is told "Failed to open a WebSocket connection".
  The HTTP API is on 9877 and served only `/api/nav` and `/api/ping`, so even
  at the right port the callback 404'd. Measured against the running app:
  `/oauth/callback` on 9877 gave 404 while `/api/ping` gave 200, and 9876 gave
  426 Upgrade Required.

* **PKCE (RFC 7636).** A desktop client cannot keep a client secret, so the
  authorization code alone must not be enough to redeem a token. Any process on
  the machine can read a loopback redirect; PKCE makes the stolen code useless
  without the verifier, which never leaves this process.

* **`state`.** Without it, anything that can reach the loopback port can hand
  this app a code for an account it controls, or replay an old one. The state
  is compared in constant time and consumed once.

The callback server is deliberately short-lived and bound to 127.0.0.1 only: it
exists while one authorization is in flight and shuts down as soon as it has
the answer. It does not reuse the main HTTP API, because that one runs for the
life of the app and its routes are the Electron shell's; this one is opened per
attempt so a flow can never wait on a port another flow left bound.

The returned page is written for a person looking at a browser tab, not for a
caller reading JSON — the JSON goes out on the wire to whoever is waiting on
`wait()`, not to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("addled.oauth")

# How long a flow may wait for the user to finish in the browser. A login that
# involves a password manager, a 2FA prompt and a consent screen is not fast;
# five minutes is long enough not to time out on a careful user and short
# enough that an abandoned tab is not holding a port.
DEFAULT_TIMEOUT = 300.0


def _b64url(raw: bytes) -> str:
    """Base64url with no padding, which is what RFC 7636 specifies."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_verifier() -> str:
    """A fresh PKCE code verifier.

    32 random bytes, base64url encoded — 43 characters, inside the 43–128 the
    RFC allows, and drawn from `secrets` rather than `random`.
    """
    return _b64url(secrets.token_bytes(32))


def challenge_for(verifier: str, method: str = "S256") -> str:
    """The `code_challenge` for a verifier.

    S256 is the only method worth offering: `plain` sends the verifier itself,
    which defeats the point of having one. Providers that accept only `plain`
    exist but are required by the RFC to support S256, so asking for it is
    safe.
    """
    if method != "S256":
        raise ValueError(f"unsupported PKCE method: {method!r}")
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def make_state() -> str:
    """A fresh `state` value, long enough not to be guessable."""
    return secrets.token_urlsafe(32)


def _success_page() -> bytes:
    return (b"<!doctype html><meta charset='utf-8'>"
            b"<title>Addled</title>"
            b"<body style=\"font:15px system-ui;padding:2.5rem;color:#222\">"
            b"<h2 style='margin:0 0 .5rem'>Connected</h2>"
            b"<p>Addled can close this tab and carry on.</p>"
            b"</body>")


def _error_page(message: str) -> bytes:
    import html
    safe = html.escape(str(message or "The request was refused."))
    return (b"<!doctype html><meta charset='utf-8'>"
            b"<title>Addled</title>"
            b"<body style=\"font:15px system-ui;padding:2.5rem;color:#222\">"
            b"<h2 style='margin:0 0 .5rem'>Not connected</h2>"
            b"<p>" + safe.encode("utf-8") + b"</p>"
            b"</body>")


class LoopbackFlow:
    """One in-flight authorization-code flow, with its own callback server.

    Usage:

        flow = LoopbackFlow(port=0)
        url = flow.authorize_url("https://accounts.google.com/o/oauth2/v2/auth",
                                 client_id=..., scope=..., extra={...})
        webbrowser.open(url)
        result = flow.wait()          # {"code": ...} or {"error": ...}

    `wait()` blocks on the server thread and never raises. It always closes the
    socket, including on timeout, so a failed attempt does not leave a port
    bound.
    """

    def __init__(self, port: int = 0, path: str = "/oauth/callback"):
        self.verifier = make_verifier()
        self.challenge = challenge_for(self.verifier)
        self.state = make_state()
        self.path = path
        self._result: dict | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.port = int(port or 0)
        self._thread: threading.Thread | None = None

    # -- server ----------------------------------------------------------

    def _handler_class(self):
        flow = self

        class _Handler(BaseHTTPRequestHandler):
            def _send(self, body: bytes, status: int = 200) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # The page is the whole response; nothing here should be
                # cached, framed, or referrer-leaked back to the provider.
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 (http.server API)
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != flow.path:
                    self._send(_error_page("Not found."), status=404)
                    return
                query = urllib.parse.parse_qs(parsed.query)

                def one(name: str) -> str:
                    values = query.get(name) or [""]
                    return str(values[0])

                err = one("error")
                if err:
                    detail = one("error_description") or err
                    # The provider refused. Not our state's business.
                    flow._finish({"error": f"The provider refused: {detail}"})
                    self._send(_error_page(detail))
                    return

                got_state = one("state")
                # `compare_digest` so a wrong state cannot be found by timing.
                # The check is the CSRF defence: without it any local process
                # could hand this app a code for an account it controls.
                if not got_state or not hmac.compare_digest(got_state,
                                                            flow.state):
                    flow._finish({
                        "error": ("The reply did not match this request "
                                  "(state mismatch). Nothing was connected.")})
                    self._send(_error_page(
                        "This reply did not match the request Addled made. "
                        "Nothing was connected."), status=400)
                    return

                code = one("code")
                if not code:
                    flow._finish({"error": "The provider sent no code."})
                    self._send(_error_page("The provider sent no code."),
                               status=400)
                    return

                flow._finish({"code": code})
                self._send(_success_page())

            def log_message(self, *args):  # silence request logging
                pass

        return _Handler

    def _finish(self, result: dict) -> None:
        with self._lock:
            if self._result is None:
                self._result = result
        self._done.set()

    def start(self) -> str:
        """Bind the callback server and return the redirect URI."""
        if self._server is not None:
            return self.redirect_uri
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port),
                                           self._handler_class())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="oauth-loopback")
        self._thread.start()
        log.info("oauth: waiting for the callback on %s", self.redirect_uri)
        return self.redirect_uri

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    def authorize_url(self, endpoint: str, client_id: str, scope: str,
                      extra: dict | None = None,
                      response_type: str = "code") -> str:
        """Build the URL to open in a browser.

        `extra` carries provider-specific parameters (Google wants
        `access_type=offline` to issue a refresh token, `prompt=consent` to
        issue one again on a repeat grant).
        """
        if self._server is None:
            self.start()
        params = {
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": response_type,
            "scope": scope,
            "state": self.state,
            # PKCE. Providers that require it (all of them, now) reject a
            # request without these.
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
        }
        for key, value in (extra or {}).items():
            if value is not None:
                params[key] = value
        joiner = "&" if "?" in endpoint else "?"
        return endpoint + joiner + urllib.parse.urlencode(params)

    def wait(self, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Block until the callback lands, the user gives up, or `timeout`.

        Always returns a dict; `code` on success, `error` otherwise. The
        callback server is closed either way.
        """
        try:
            got = self._done.wait(timeout)
        finally:
            self.close()
        if not got:
            return {"error": ("Timed out waiting for the browser to finish. "
                              "Nothing was connected.")}
        with self._lock:
            result = dict(self._result or {})
        if not result:
            return {"error": "The authorization did not complete."}
        # The verifier is the caller's only way to redeem the code, so it
        # travels with it rather than being read off the flow afterwards —
        # one less chance to pair the wrong two together.
        if "code" in result:
            result["verifier"] = self.verifier
            result["redirect_uri"] = self.redirect_uri
        return result

    def cancel(self, reason: str = "Cancelled.") -> None:
        """Stop waiting without an answer."""
        self._finish({"error": reason})

    def close(self) -> None:
        """Stop the callback server. Safe to call more than once."""
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 — shutting down must not raise
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def exchange_code(*, token_endpoint: str, code: str, verifier: str,
                  client_id: str, redirect_uri: str,
                  client_secret: str = "",
                  timeout: float = 30.0) -> dict:
    """Redeem an authorization code for tokens.

    Uses `httpx`, which the MCP HTTP transport already depends on, so this adds
    no import that is not already loaded in a running app.

    A public client (no secret) sends `client_id` and the PKCE `code_verifier`;
    a confidential one adds the secret. Both are included when a secret is
    configured, because Google's desktop clients are issued one and expect it.
    """
    import httpx

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(token_endpoint, data=data,
                               headers={"Accept": "application/json"})
    except Exception as e:  # noqa: BLE001 — network failures are the caller's
        return {"error": f"Could not reach the token endpoint: {e}"}
    if resp.status_code >= 400:
        return {"error": _explain_token_error(resp)}
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return {"error": "The token endpoint returned a body that is not JSON."}
    if not isinstance(payload, dict):
        return {"error": "The token endpoint returned an unexpected body."}
    if payload.get("error"):
        return {"error": _explain_token_error(resp, payload)}
    if payload.get("access_token"):
        payload["obtained_at"] = time.time()
    return payload


def refresh_tokens(*, token_endpoint: str, refresh_token: str, client_id: str,
                   client_secret: str = "", timeout: float = 30.0) -> dict:
    """Get a new access token from a refresh token.

    Same shape as `exchange_code`, so callers treat both the same way.
    """
    import httpx

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(token_endpoint, data=data,
                               headers={"Accept": "application/json"})
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not reach the token endpoint: {e}"}
    if resp.status_code >= 400:
        return {"error": _explain_token_error(resp)}
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return {"error": "The token endpoint returned a body that is not JSON."}
    if isinstance(payload, dict):
        if payload.get("error"):
            return {"error": _explain_token_error(resp, payload)}
        if payload.get("access_token"):
            payload["obtained_at"] = time.time()
            payload.setdefault("refresh_token", refresh_token)
        return payload
    return {"error": "The token endpoint returned an unexpected body."}


def _explain_token_error(resp, payload: dict | None = None) -> str:
    """Turn a failed token request into something a person can act on.

    OAuth error bodies are terse and the status alone is misleading — a 400 on
    a token exchange means the code was wrong, expired, or already used, and
    "invalid_grant" does not say which. Reading the body's own error field
    covers cases the status cannot.
    """
    detail = ""
    if payload is None:
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = None
    code = ""
    description = ""
    if isinstance(payload, dict):
        # Read `error` and `error_description` separately. Preferring the
        # description looked reasonable and was wrong: Google answers a spent
        # code with `error=invalid_grant` and
        # `error_description="Bad Request"`, and the description is the one
        # field that says nothing. The code is the machine-readable part and
        # the only one worth branching on; the description is added as colour
        # when it actually carries some.
        code = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or "").strip()
        if description.lower() in ("bad request", "badrequest"):
            description = ""
    status = getattr(resp, "status_code", 0)
    if code == "invalid_grant":
        return ("The authorization code was refused (invalid_grant). It may "
                "have expired, already have been used, or have been issued "
                "for a different redirect. Start the connection again.")
    if code == "invalid_client":
        return ("The client id or secret was refused (invalid_client). Check "
                "the credentials in Settings.")
    if code in ("invalid_request", "unsupported_grant_type"):
        return f"The token endpoint refused the request ({code})."
    if code or description:
        detail = " — ".join(part for part in (code, description) if part)
        return f"The token endpoint refused the request ({status}): {detail}"
    body = ""
    try:
        body = str(resp.text or "")[:200]
    except Exception:  # noqa: BLE001
        pass
    return f"The token endpoint refused the request ({status}). {body}".strip()


# -- the shared callback route for the long-lived HTTP API ----------------
#
# `LoopbackFlow` opens its own port per attempt, which is right for MCP (many
# servers, one at a time). Some callers want a single stable callback URL that
# the provider has already been told about, and for those the route below is
# registered on the main HTTP API instead. Both share the PKCE and state work.

_waiting: dict[str, LoopbackFlow] = {}
_waiting_lock = threading.Lock()


def register_waiting(state: str, flow: LoopbackFlow) -> None:
    with _waiting_lock:
        _waiting[state] = flow


def take_waiting(state: str) -> LoopbackFlow | None:
    with _waiting_lock:
        return _waiting.pop(state, None)


def deliver(state: str, code: str = "", error: str = "") -> bool:
    """Hand a callback result to the flow waiting on `state`.

    Returns whether a flow was found. Used by the HTTP API route, so a provider
    redirect can be answered on the app's own port.
    """
    flow = take_waiting(state)
    if flow is None:
        return False
    if error:
        flow._finish({"error": error})
    elif code:
        flow._finish({"code": code})
    else:
        flow._finish({"error": "The provider sent neither a code nor an error."})
    return True
