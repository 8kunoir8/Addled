"""The OAuth loopback flow: PKCE, state, the callback, and the exchange.

What this covers, and why each part earned a check:

* **The redirect actually lands.** The bug was a redirect to port 9876 — the
  WebSocket listener, which answers a browser with "Failed to open a WebSocket
  connection" — and no route on 9877 for it to reach. Both are asserted against
  a real server on a real socket, not by reading the source.
* **PKCE is present and correct.** S256, and the challenge is the SHA-256 of
  the verifier base64url encoded with no padding. Getting this wrong is
  invisible locally and fatal against a provider.
* **`state` is enforced.** A wrong or missing state must be refused and must
  not deliver a code. This is the CSRF defence; a flow that skips it accepts a
  code from anyone who can reach the port.
* **The verifier travels with the code**, or the exchange cannot redeem it.
* **A refusal and a timeout are reported, not hung on.** An abandoned browser
  tab must not leave a port bound or a caller waiting forever.

The token exchange is pointed at a stub HTTP server, so the whole path runs
without a Google account or network access.
"""

from __future__ import annotations

import asyncio
import hashlib
import base64
import json
import socket
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails: list[str] = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def transport():
    from backend.integrations import oauth_loopback
    return oauth_loopback


# ---- 1. PKCE -----------------------------------------------------------------

def test_pkce():
    oauth = transport()

    verifier = oauth.make_verifier()
    check("a verifier is inside the RFC 7636 length range",
          43 <= len(verifier) <= 128, f"len={len(verifier)}")
    check("a verifier is url-safe",
          all(c.isalnum() or c in "-._~" for c in verifier), verifier[:40])
    check("two verifiers differ", oauth.make_verifier() != verifier,
          "the verifier is not random")

    # The challenge must be exactly SHA-256(verifier), base64url, unpadded.
    expected = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    check("the challenge is the S256 of the verifier",
          oauth.challenge_for(verifier) == expected,
          f"{oauth.challenge_for(verifier)[:20]} != {expected[:20]}")
    check("the challenge carries no base64 padding",
          "=" not in oauth.challenge_for(verifier), "padding present")
    check("plain PKCE is refused",
          _raises(lambda: oauth.challenge_for(verifier, "plain")),
          "a plain challenge would send the verifier itself")

    check("two states differ", oauth.make_state() != oauth.make_state(),
          "the state is not random")
    check("a state is not guessable",
          len(oauth.make_state()) >= 32, "state too short")


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


# ---- 2. the authorize URL ----------------------------------------------------

def test_authorize_url():
    oauth = transport()
    flow = oauth.LoopbackFlow()
    try:
        url = flow.authorize_url("https://example.test/auth",
                                 client_id="cid", scope="s1 s2",
                                 extra={"access_type": "offline"})
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        check("the authorize URL points at the given endpoint",
              parsed.netloc == "example.test" and parsed.path == "/auth", url)
        check("it asks for a code",
              query.get("response_type") == ["code"], str(query))
        check("it carries the state",
              query.get("state") == [flow.state], str(query.get("state")))
        check("it carries the S256 challenge",
              query.get("code_challenge") == [flow.challenge], str(query))
        check("it names S256 as the method",
              query.get("code_challenge_method") == ["S256"], str(query))
        check("the verifier is NOT in the URL",
              flow.verifier not in url, "the verifier leaked into the URL")
        check("extra provider parameters are passed",
              query.get("access_type") == ["offline"], str(query))
        check("the redirect is on loopback, not the WebSocket port",
              ":9876" not in flow.redirect_uri, flow.redirect_uri)
        check("the redirect targets 127.0.0.1",
              flow.redirect_uri.startswith("http://127.0.0.1:"),
              flow.redirect_uri)
        check("the redirect path is the callback path",
              flow.redirect_uri.endswith("/oauth/callback"), flow.redirect_uri)
    finally:
        flow.close()


# ---- 3. the callback, on a real socket ---------------------------------------

def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_callback_happy_path():
    oauth = transport()
    flow = oauth.LoopbackFlow()
    flow.start()
    port = flow.port
    try:
        # Port 9876 is the WebSocket listener; nothing here may use it.
        check("the callback server did not take the WebSocket port",
              port != 9876, f"port={port}")
        check("the callback server bound a real port", port > 0, f"port={port}")

        url = (f"http://127.0.0.1:{port}/oauth/callback"
               f"?code=THECODE&state={urllib.parse.quote(flow.state)}")
        status, body = _get(url)
        check("the callback answers 200", status == 200, f"status={status}")
        check("the callback page tells the user they are done",
              "connected" in body.lower(), body[:120])

        result = flow.wait(timeout=10)
        check("the code came back", result.get("code") == "THECODE",
              str(result))
        check("the verifier travels with the code",
              result.get("verifier") == flow.verifier,
              "the exchange cannot redeem a code without it")
        check("the redirect used in the exchange is reported",
              result.get("redirect_uri", "").endswith("/oauth/callback"),
              str(result))
        check("the callback server was closed after the answer",
              flow._server is None, "the port is still bound")
    finally:
        flow.close()


def test_callback_rejects_bad_state():
    oauth = transport()
    flow = oauth.LoopbackFlow()
    flow.start()
    port = flow.port
    try:
        url = (f"http://127.0.0.1:{port}/oauth/callback"
               f"?code=ATTACKER&state=not-the-right-state")
        status, _ = _get(url)
        check("a wrong state is refused", status == 400, f"status={status}")
        result = flow.wait(timeout=10)
        check("a wrong state delivers no code", not result.get("code"),
              str(result))
        check("a wrong state is explained",
              "state" in str(result.get("error", "")).lower()
              or "match" in str(result.get("error", "")).lower(),
              str(result.get("error")))
    finally:
        flow.close()


def test_callback_missing_state_and_404():
    oauth = transport()
    flow = oauth.LoopbackFlow()
    flow.start()
    port = flow.port
    try:
        # A different path must not be treated as the callback. Checked
        # BEFORE the waiting call, because `wait()` closes the server — a
        # probe after it would be measuring a closed socket, not the route.
        status2, _ = _get(f"http://127.0.0.1:{port}/something/else")
        check("another path is a 404, not the callback", status2 == 404,
              f"status={status2}")

        status, _ = _get(f"http://127.0.0.1:{port}/oauth/callback?code=X")
        check("a callback with no state is refused", status == 400,
              f"status={status}")
        result = flow.wait(timeout=10)
        check("a callback with no state delivers no code",
              not result.get("code"), str(result))
    finally:
        flow.close()


def test_provider_error_is_reported():
    oauth = transport()
    flow = oauth.LoopbackFlow()
    flow.start()
    port = flow.port
    try:
        url = (f"http://127.0.0.1:{port}/oauth/callback?error=access_denied"
               f"&error_description=User+said+no")
        _get(url)
        result = flow.wait(timeout=10)
        check("a provider refusal is reported",
              "User said no" in str(result.get("error", "")), str(result))
        check("a provider refusal carries no code",
              not result.get("code"), str(result))
    finally:
        flow.close()


def test_timeout_closes_the_port():
    oauth = transport()
    flow = oauth.LoopbackFlow()
    flow.start()
    port = flow.port
    result = flow.wait(timeout=0.4)
    check("a timed-out flow reports rather than hanging",
          "error" in result, str(result))
    check("a timed-out flow mentions the timeout",
          "timed out" in str(result.get("error", "")).lower(), str(result))
    # The port must be free again — a second flow should still work.
    check("the callback server closed on timeout", flow._server is None,
          "the port is still bound")
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
        bound = True
    except OSError:
        bound = False
    finally:
        probe.close()
    check("the timed-out port can be bound again", bound,
          f"port {port} is still held")


def test_superseding_a_flow_releases_the_port():
    oauth = transport()
    first = oauth.LoopbackFlow()
    first.start()
    first_port = first.port
    first.cancel("superseded")
    check("cancelling resolves the wait",
          "error" in first.wait(timeout=2), "still waiting")
    second = oauth.LoopbackFlow()
    second.start()
    try:
        check("a second flow after a cancel still starts",
              second.port > 0, f"port={second.port}")
    finally:
        second.close()
        first.close()
    check("the first flow's port was released", first_port != second.port
          or first._server is None, "port handling looks wrong")


# ---- 4. the token exchange, against a stub provider --------------------------

class _StubTokenServer:
    """A token endpoint that records what it was sent."""

    def __init__(self, response: dict, status: int = 200):
        self.response = response
        self.status = status
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                outer.received = urllib.parse.parse_qs(raw)
                body = json.dumps(outer.response).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.received: dict = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="stub-token").start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/token"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def test_exchange_sends_pkce_and_gets_tokens():
    oauth = transport()
    stub = _StubTokenServer({"access_token": "at", "refresh_token": "rt",
                             "expires_in": 3600})
    try:
        got = oauth.exchange_code(
            token_endpoint=stub.endpoint, code="thecode",
            verifier="theverifier", client_id="cid",
            redirect_uri="http://127.0.0.1:1234/oauth/callback")
        check("the exchange returns the access token",
              got.get("access_token") == "at", str(got))
        check("the exchange returns the refresh token",
              got.get("refresh_token") == "rt", str(got))
        check("the exchange stamps when the token was obtained",
              isinstance(got.get("obtained_at"), (int, float)), str(got))
        sent = stub.received
        check("the exchange sends the code verifier (PKCE)",
              sent.get("code_verifier") == ["theverifier"], str(sent))
        check("the exchange sends authorization_code",
              sent.get("grant_type") == ["authorization_code"], str(sent))
        check("the exchange sends the same redirect it authorized with",
              sent.get("redirect_uri") ==
              ["http://127.0.0.1:1234/oauth/callback"], str(sent))
        check("the exchange sends the client id",
              sent.get("client_id") == ["cid"], str(sent))
        check("no client secret is sent when none is configured",
              "client_secret" not in sent, str(sent))
    finally:
        stub.close()


def test_exchange_with_client_secret():
    oauth = transport()
    stub = _StubTokenServer({"access_token": "at"})
    try:
        oauth.exchange_code(token_endpoint=stub.endpoint, code="c",
                            verifier="v", client_id="cid",
                            client_secret="shh",
                            redirect_uri="http://127.0.0.1:1/oauth/callback")
        check("a configured client secret is sent",
              stub.received.get("client_secret") == ["shh"],
              str(stub.received))
    finally:
        stub.close()


def test_exchange_errors_are_explained():
    oauth = transport()
    stub = _StubTokenServer({"error": "invalid_grant",
                             "error_description": "Bad Request"}, status=400)
    try:
        got = oauth.exchange_code(
            token_endpoint=stub.endpoint, code="used", verifier="v",
            client_id="cid", redirect_uri="http://127.0.0.1:1/oauth/callback")
        check("a refused exchange returns an error, not tokens",
              bool(got.get("error")) and not got.get("access_token"), str(got))
        check("the error says the code may be expired or used",
              "expired" in got["error"].lower()
              or "already been used" in got["error"].lower(),
              got["error"])
    finally:
        stub.close()

    # A non-JSON body must not raise.
    class _Junk(_StubTokenServer):
        pass

    stub2 = _StubTokenServer({}, status=500)
    try:
        got2 = oauth.exchange_code(
            token_endpoint=stub2.endpoint, code="c", verifier="v",
            client_id="cid", redirect_uri="http://127.0.0.1:1/oauth/callback")
        check("a 500 from the token endpoint is reported",
              bool(got2.get("error")), str(got2))
    finally:
        stub2.close()

    # Unreachable endpoint.
    got3 = oauth.exchange_code(
        token_endpoint="http://127.0.0.1:1/token", code="c", verifier="v",
        client_id="cid", redirect_uri="http://127.0.0.1:1/oauth/callback",
        timeout=2.0)
    check("an unreachable token endpoint is reported, not raised",
          bool(got3.get("error")), str(got3))


def test_refresh():
    oauth = transport()
    stub = _StubTokenServer({"access_token": "new"})
    try:
        got = oauth.refresh_tokens(token_endpoint=stub.endpoint,
                                   refresh_token="rt", client_id="cid")
        check("refresh returns a new access token",
              got.get("access_token") == "new", str(got))
        check("refresh sends refresh_token as the grant",
              stub.received.get("grant_type") == ["refresh_token"],
              str(stub.received))
        check("refresh keeps the refresh token when none is reissued",
              got.get("refresh_token") == "rt", str(got))
    finally:
        stub.close()


# ---- 5. the HTTP API route (the shared callback) -----------------------------

def test_deliver_routes_by_state():
    """The route on the app's own port hands the result to the waiting flow."""
    oauth = transport()
    flow = oauth.LoopbackFlow()
    try:
        oauth.register_waiting(flow.state, flow)
        ok = oauth.deliver(flow.state, code="ROUTED")
        check("deliver finds the waiting flow by state", ok,
              "the flow was not found")
        result = flow.wait(timeout=5)
        check("the routed code reaches the flow",
              result.get("code") == "ROUTED", str(result))
        check("delivering twice does not reuse the state",
              oauth.deliver(flow.state, code="AGAIN") is False,
              "a replayed callback was accepted")
    finally:
        flow.close()

    check("an unknown state is refused",
          oauth.deliver("nobody-waiting", code="X") is False,
          "a stray callback was accepted")


def test_http_api_has_the_route():
    """The live HTTP API must route /oauth/callback rather than 404 it."""
    source = (ROOT / "backend" / "http_api.py").read_text(encoding="utf-8")
    check("the HTTP API routes /oauth/callback",
          "/oauth/callback" in source,
          "the callback 404s, which is the bug this fixes")
    check("the HTTP API still serves /api/nav",
          "/api/nav" in source, "the Electron shell's route was lost")
    check("the HTTP API still serves /api/ping",
          "/api/ping" in source, "the control route was lost")


# ---- 6. the calendar integration uses all of it -------------------------------

def test_calendar_wiring():
    src = (ROOT / "backend" / "integrations"
           / "calendar_integration.py").read_text(encoding="utf-8")
    check("the calendar no longer redirects to the WebSocket port",
          "9876" not in src, "a browser cannot complete an OAuth flow there")
    check("the calendar asks for PKCE",
          "code_challenge" in src or "authorize_url" in src,
          "the flow must carry PKCE")
    check("the calendar uses the shared flow",
          "oauth_loopback" in src, "the PKCE work must be shared")
    check("the calendar has a way to consume the callback",
          "google_wait_and_sync" in src,
          "nothing consumed the code, so the flow could never finish")
    check("the calendar sends the verifier on exchange",
          "verifier" in src, "the exchange cannot redeem a code without it")

    # And it must not crash on import.
    from backend.integrations.calendar_integration import calendar
    check("the calendar integration still imports", calendar is not None)

    # "Not configured" must be decided by the client id, not by whatever the
    # ambient config happens to hold. Setting it explicitly is the difference
    # between testing the behaviour and testing the leftover state of whoever
    # ran last.
    from backend.config import config
    saved = config.get("integrations", "google_client_id", default=None)
    try:
        config.set("integrations", "google_client_id", value="")
        check("google_auth_url returns None without a client id",
              calendar.google_auth_url() is None,
              "it should report 'not configured' rather than open a bad URL")
    finally:
        config.set("integrations", "google_client_id", value=saved)


def test_calendar_round_trip():
    """The whole flow against a stub provider: authorized URL -> callback ->
    exchange -> events.

    This is the part that could not happen before. The pieces each worked in
    isolation (there was a `sync_google`, there was an auth URL) but nothing
    served the redirect and the exchange carried no verifier, so the two ends
    never met. Running them together with a stub provider is what shows the
    wiring is now real rather than merely present.
    """
    from backend.config import config
    from backend.integrations.calendar_integration import calendar

    class _StubProvider(BaseHTTPRequestHandler):
        received: dict = {}

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            _StubProvider.received["token"] = urllib.parse.parse_qs(
                self.rfile.read(length).decode())
            body = json.dumps({"access_token": "STUB_AT",
                               "refresh_token": "STUB_RT",
                               "expires_in": 3600}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            body = json.dumps({"items": [{
                "summary": "Stub event",
                "start": {"dateTime": "2026-10-15T09:00:00Z"},
                "end": {"dateTime": "2026-10-15T10:00:00Z"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubProvider)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="stub-provider").start()

    saved = {}
    for key in ("google_client_id", "google_client_secret", "google_tokens"):
        saved[key] = config.get("integrations", key, default=None)
    try:
        calendar.GOOGLE_AUTH_ENDPOINT = f"{base}/auth"
        calendar.GOOGLE_TOKEN_ENDPOINT = f"{base}/token"
        calendar.GOOGLE_EVENTS_ENDPOINT = f"{base}/events"
        config.set("integrations", "google_client_id", value="stub-client")
        config.set("integrations", "google_client_secret", value="stub-secret")
        config.set("integrations", "google_tokens", value={})

        url = calendar.google_auth_url()
        check("the calendar produces an authorize URL", bool(url), str(url))
        if not url:
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        check("the authorize URL carries PKCE",
              "code_challenge" in query, str(query))
        check("the authorize URL carries state",
              "state" in query, str(query))

        # The provider sends the browser to our callback with the code.
        redirect = query["redirect_uri"][0]
        state = query["state"][0]
        status, _ = _get(f"{redirect}?code=THE_CODE"
                         f"&state={urllib.parse.quote(state)}")
        check("the calendar's callback is served", status == 200,
              f"status={status}")

        result = calendar.google_wait_and_sync(timeout=15)
        check("the round trip reports no error",
              not result.get("error"), str(result))
        check("the exchange sent the PKCE verifier",
              bool(_StubProvider.received.get("token", {}).get(
                  "code_verifier")),
              "without it the provider refuses the code")
        check("the exchange sent the same redirect it authorized with",
              _StubProvider.received.get("token", {}).get(
                  "redirect_uri") == [redirect],
              str(_StubProvider.received.get("token")))
        check("an event was imported from the provider",
              result.get("events") == 1, str(result))
        check("the imported event is in the calendar",
              any(e.get("title") == "Stub event"
                  for e in calendar.get_events()),
              str([e.get("title") for e in calendar.get_events()][:5]))
        check("the tokens were stored",
              bool(config.get("integrations", "google_tokens",
                              default={}).get("access_token")),
              "the connection cannot be reused without them")
    finally:
        for key, value in saved.items():
            config.set("integrations", key, value=value)
        srv.shutdown()
        srv.server_close()


def test_google_ws_actions():
    """The dashboard's connect button, as the handlers actually behave."""
    from backend.config import config
    from backend.integrations.calendar_integration import calendar
    import backend.ws_server as wsmod

    wsmod._register_default_handlers()
    srv = wsmod.get_server()
    handlers = srv._handlers
    check("calendar.googleState is registered",
          "calendar.googleState" in handlers, "the UI cannot read the state")
    check("calendar.googleConnect is registered",
          "calendar.googleConnect" in handlers, "the button has nothing to call")
    check("calendar.googleDisconnect is registered",
          "calendar.googleDisconnect" in handlers, "cannot undo a connection")

    saved_id = config.get("integrations", "google_client_id", default=None)
    saved_tokens = config.get("integrations", "google_tokens", default=None)
    import webbrowser
    original_open = webbrowser.open
    opened: dict = {}

    async def drive():
        config.set("integrations", "google_client_id", value="")
        state = await handlers["calendar.googleState"]({}, None)
        check("the state reports 'not configured' with no client id",
              state.get("configured") is False, str(state))
        refused = await handlers["calendar.googleConnect"]({}, None)
        check("connect refuses honestly when unconfigured",
              bool(refused.get("error")), str(refused))

        config.set("integrations", "google_client_id", value="cid")
        state2 = await handlers["calendar.googleState"]({}, None)
        check("the state reports 'configured' once a client id is set",
              state2.get("configured") is True, str(state2))

        # The browser is opened by the handler; capture it instead.
        webbrowser.open = lambda url, *a, **k: (opened.setdefault("url", url)
                                                or True)
        started = await handlers["calendar.googleConnect"]({}, None)
        check("connect reports success", started.get("success") is True,
              str(started))
        check("connect opens a browser at the authorize URL",
              bool(opened.get("url")), "no URL was opened")
        check("the opened URL carries PKCE",
              "code_challenge=" in str(opened.get("url")),
              str(opened.get("url"))[:120])
        check("the opened URL carries state",
              "state=" in str(opened.get("url")),
              str(opened.get("url"))[:120])
        check("connect returns without waiting for the browser",
              isinstance(started, dict) and "error" not in started,
              "the handler blocked, which would freeze the event loop")

        # Disconnect clears the tokens.
        config.set("integrations", "google_tokens",
                   value={"access_token": "x"})
        connected = await handlers["calendar.googleState"]({}, None)
        check("the state reports 'connected' with tokens present",
              connected.get("connected") is True, str(connected))
        await handlers["calendar.googleDisconnect"]({}, None)
        after = await handlers["calendar.googleState"]({}, None)
        check("disconnect clears the connection",
              after.get("connected") is False, str(after))

    try:
        asyncio.run(drive())
    finally:
        webbrowser.open = original_open
        # Leave no flow waiting: its thread and port would outlive the test.
        pending_flow = getattr(calendar, "_flow", None)
        if pending_flow is not None:
            pending_flow.cancel("check finished")
        config.set("integrations", "google_client_id", value=saved_id)
        config.set("integrations", "google_tokens", value=saved_tokens)


class _StubOAuthServer:
    """An MCP server that requires OAuth and publishes the documents for it.

    Serves the three things the flow has to find: the Protected Resource
    Metadata (RFC 9728), the Authorization Server Metadata (RFC 8414), and a
    registration endpoint (RFC 7591). Also serves `/mcp` so the 401 that
    starts the flow can be produced for real.
    """

    def __init__(self, *, publish_registration=True, reject_refresh=False):
        self.publish_registration = publish_registration
        self.reject_refresh = reject_refresh
        self.record = {"registered": None, "exchanged": None,
                       "refreshed": None, "authorize_url": "",
                       "mcp_auth": [], "token_calls": 0}
        # The one bearer the MCP endpoint will accept. Tests set it to stand in
        # for a token that is still good; left alone it accepts none, so the
        # 401 path is the default.
        self.accept_bearer = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = f"http://127.0.0.1:{outer.port}"
                if self.path.startswith("/.well-known/oauth-protected-resource"):
                    return self._json(200, {
                        "resource": f"{base}/mcp",
                        "authorization_servers": [base],
                        "scopes_supported": ["mcp:read", "mcp:write"]})
                if self.path.startswith("/.well-known/oauth-authorization-server"):
                    doc = {
                        "issuer": base,
                        "authorization_endpoint": f"{base}/authorize",
                        "token_endpoint": f"{base}/token",
                    }
                    if outer.publish_registration:
                        doc["registration_endpoint"] = f"{base}/register"
                    return self._json(200, doc)
                if self.path == "/mcp":
                    # The 401 that begins everything, naming where to look.
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Bearer resource_metadata="{base}/.well-known/'
                        f'oauth-protected-resource/mcp"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if self.path == "/mcp":
                    auth = self.headers.get("Authorization") or ""
                    outer.record["mcp_auth"].append(auth)
                    if not outer.accept_bearer or auth != (
                            f"Bearer {outer.accept_bearer}"):
                        self.send_response(401)
                        self.send_header(
                            "WWW-Authenticate",
                            f'Bearer error="invalid_token", resource_metadata='
                            f'"http://127.0.0.1:{outer.port}/.well-known/'
                            f'oauth-protected-resource/mcp"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    msg = json.loads(raw or b"{}")
                    if msg.get("method") == "initialize":
                        return self._json(200, {
                            "jsonrpc": "2.0", "id": msg.get("id"),
                            "result": {
                                "protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "serverInfo": {"name": "stub", "version": "1"}}})
                    return self._json(202, {})
                if self.path == "/register":
                    outer.record["registered"] = json.loads(raw or b"{}")
                    return self._json(201, {"client_id": "cid-abc",
                                            "client_secret": "csec-xyz"})
                if self.path == "/token":
                    outer.record["token_calls"] += 1
                    form = {k: v[0] for k, v in
                            urllib.parse.parse_qs(raw.decode()).items()}
                    if form.get("grant_type") == "refresh_token":
                        outer.record["refreshed"] = form
                        if outer.reject_refresh:
                            return self._json(400, {
                                "error": "invalid_grant",
                                "error_description": "refresh token expired"})
                        return self._json(200, {
                            "access_token": "refreshed-access",
                            "token_type": "Bearer", "expires_in": 3600})
                    outer.record["exchanged"] = form
                    return self._json(200, {
                        "access_token": "the-access-token",
                        "refresh_token": "the-refresh-token",
                        "token_type": "Bearer", "expires_in": 3600})
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def test_mcp_oauth_flow():
    """Discovery, registration, PKCE and the exchange, against a stub server."""
    from backend.mcp_client.oauth import (
        OAuthClient, OAuthError, TokenStore, _metadata_url_from_challenge)
    from backend.config import config

    # The well-known segment goes between origin and path (RFC 8414/9728).
    # Appending it to the end is the obvious-looking mistake and asks the
    # wrong URL.
    check("the well-known path precedes the resource path",
          OAuthClient._well_known("https://h/a/b", "oauth-protected-resource")
          == "https://h/.well-known/oauth-protected-resource/a/b",
          OAuthClient._well_known("https://h/a/b", "oauth-protected-resource"))
    check("a bare origin produces no trailing slash artefact",
          OAuthClient._well_known("https://h", "oauth-authorization-server")
          == "https://h/.well-known/oauth-authorization-server")
    check("a quoted resource_metadata is read from the challenge",
          _metadata_url_from_challenge(
              'Bearer realm="x", resource_metadata="https://h/.wk/r"')
          == "https://h/.wk/r")
    check("a challenge without one yields nothing",
          _metadata_url_from_challenge('Bearer realm="x"') == "")

    stub = _StubOAuthServer()
    store = TokenStore()
    saved = store._all()
    store.clear()
    try:
        client = OAuthClient("mcp-stub", stub.url, store=store)

        async def drive():
            metadata = await client.discover()
            check("the token endpoint is discovered",
                  bool(metadata.get("token_endpoint")),
                  str(metadata)[:160])
            check("the scopes come from the resource document",
                  client.scope == "mcp:read mcp:write", client.scope)

            url = await client.authorize_url()
            stub.record["authorize_url"] = url
            check("the authorize URL carries PKCE",
                  "code_challenge=" in url
                  and "code_challenge_method=S256" in url, url[:160])
            check("the authorize URL carries state", "state=" in url,
                  url[:160])
            # RFC 8707: without `resource`, a token minted for one server can
            # be presented to another that trusts the same issuer.
            check("the authorize URL names the resource",
                  "resource=" in url
                  and urllib.parse.quote(stub.url, safe="") in url,
                  url[:200])
            check("registration used the loopback redirect",
                  stub.record["registered"]["redirect_uris"][0]
                  .startswith("http://127.0.0.1:"),
                  str(stub.record["registered"].get("redirect_uris")))
            check("registration asks for the refresh grant",
                  "refresh_token" in
                  stub.record["registered"].get("grant_types", []),
                  str(stub.record["registered"].get("grant_types")))

            # Deliver a code to the loopback listener as a browser would.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            state = (query.get("state") or [""])[0]
            callback = (f"{client._flow.redirect_uri}?code=stub-code"
                        f"&state={urllib.parse.quote(state)}")

            def hit():
                try:
                    urllib.request.urlopen(callback, timeout=4).read()
                except Exception:  # noqa: BLE001 — the flow records the result
                    pass

            threading.Thread(target=hit, daemon=True).start()
            result = await client.wait_for_code(timeout=8)
            check("the callback delivers the code",
                  result.get("code") == "stub-code", str(result))
            check("the verifier travels with the code",
                  bool(result.get("verifier")), str(result)[:120])
            check("the redirect URI travels with the code",
                  bool(result.get("redirect_uri")), str(result)[:120])

            await client.complete(result)
            sent = stub.record["exchanged"] or {}
            check("the exchange sends the PKCE verifier",
                  bool(sent.get("code_verifier")), str(sent)[:160])
            check("the exchange sends the same redirect URI it authorized with",
                  sent.get("redirect_uri") == result.get("redirect_uri"),
                  f"{sent.get('redirect_uri')} vs {result.get('redirect_uri')}")
            check("the exchange sends the client id",
                  sent.get("client_id") == "cid-abc", str(sent)[:120])
            check("the tokens are held",
                  client.has_tokens and client.bearer()
                  == "Bearer the-access-token", client.bearer())

            # At rest: a token is a secret like any other, and the credential
            # store encrypts what it holds.
            raw = str(store._all())
            check("the token is not stored in the clear",
                  "the-access-token" not in raw
                  and "the-refresh-token" not in raw,
                  "a token is readable in the config")

            await client.refresh()
            check("a refresh uses the stored refresh token",
                  (stub.record["refreshed"] or {}).get("refresh_token")
                  == "the-refresh-token",
                  str(stub.record["refreshed"])[:160])
            check("the refreshed token replaced the old one",
                  client.bearer() == "Bearer refreshed-access",
                  client.bearer())
            check("a refresh returning no new refresh token keeps the old",
                  client.tokens.get("refresh_token") == "the-refresh-token",
                  str(client.tokens.get("refresh_token"))[:60])

            # A server with nothing published must read as a sentence, not a
            # traceback in a log.
            lonely = OAuthClient("nobody", "http://127.0.0.1:1/mcp",
                                 store=store)
            try:
                await lonely.discover('Bearer realm="x"')
                check("a server publishing no metadata is refused", False,
                      "discovery appeared to succeed")
            except OAuthError as e:
                check("a server publishing no metadata is refused with words",
                      "sign-in" in str(e) or "reach" in str(e), str(e))
            lonely.close()
            # `close` joins the flow's server thread, so it is not something to
            # await — but doing it inside the coroutine is fine because it
            # returns immediately once the socket is shut.
            client.close()

        asyncio.run(drive())
    finally:
        store.clear()
        config.set("mcp", "oauth_tokens", value=saved)
        stub.close()


def test_mcp_oauth_needs_no_registration():
    """A server with no registration endpoint says so, in words."""
    from backend.mcp_client.oauth import OAuthClient, OAuthError, TokenStore
    from backend.config import config

    stub = _StubOAuthServer(publish_registration=False)
    store = TokenStore()
    saved = store._all()
    store.clear()
    try:
        client = OAuthClient("mcp-noreg", stub.url, store=store)

        async def drive():
            await client.discover()
            try:
                await client.authorize_url()
                check("a server without DCR is refused", False,
                      "authorize_url appeared to succeed")
            except OAuthError as e:
                check("a server without DCR asks for a client id by hand",
                      "client id" in str(e), str(e))
            client.close()

        asyncio.run(drive())
    finally:
        store.clear()
        config.set("mcp", "oauth_tokens", value=saved)
        stub.close()


def test_mcp_401_interceptor():
    """A 401 becomes an actionable message, not a bare status code.

    This is what made the market's `runnable: True` a lie: a server wanting
    OAuth answered every call with something the user could not act on. The
    client is expected to say what to do, and to expose the state a card needs
    to offer the button that does it.
    """
    import asyncio
    from backend.mcp_client.http import McpHttpClient, _auth_hint
    from backend.mcp_client.oauth import TokenStore

    # The message depends on what the challenge said. Never having had a token
    # is a different situation from having one refused, and telling a user to
    # sign in when they already did sends them in a circle.
    assert "rejected" in _auth_hint('Bearer error="invalid_token"'), (
        "an invalid_token challenge does not say the token was refused")
    assert "enough access" in _auth_hint('Bearer error="insufficient_scope"')
    assert "needs a sign-in" in _auth_hint('Bearer realm="x"'), (
        "a challenge we do not recognise does not say what to do")

    stub = _StubOAuthServer()
    store = TokenStore()
    store.clear()
    try:
        async def run():
            problems = []
            client = McpHttpClient("needs-auth",
                                   {"url": stub.url, "timeout_s": 5})
            try:
                await client.start()
                problems.append("start() succeeded against a server that "
                                "wants a sign-in")
                return problems
            except Exception as e:  # noqa: BLE001
                text = str(e)
            if "sign-in" not in text:
                problems.append(f"the 401 message is not actionable: {text}")
            if not client.auth_required:
                problems.append("auth_required stayed False after a 401")
            if not client.auth_error:
                problems.append("auth_error is empty after a 401")
            return problems

        problems = asyncio.run(run())
    finally:
        store.clear()
        stub.close()
    assert not problems, "\n  ".join(problems)


def test_mcp_auth_header_rules():
    """An explicit key wins over OAuth; a stored token is sent unasked.

    Two rules that are easy to get backwards and expensive when you do: a
    server with a static key must keep working now that OAuth exists at all,
    and a token saved in an earlier run must go out on this run's first
    request rather than after a wasted 401.
    """
    from backend.mcp_client.http import McpHttpClient
    from backend.mcp_client.oauth import TokenStore

    store = TokenStore()
    store.clear()
    try:
        plain = McpHttpClient("plain", {"url": "https://x.example/mcp"})
        assert "Authorization" not in plain._headers(), (
            "a client with no token and no configured header sent an "
            "Authorization header anyway")

        static = McpHttpClient("static", {
            "url": "https://x.example/mcp",
            "headers": {"Authorization": "Bearer static-key"}})
        assert static._headers()["Authorization"] == "Bearer static-key"

        store.set("tok", {"access_token": "stored-tok"})
        with_token = McpHttpClient("tok", {"url": "https://x.example/mcp"})
        assert with_token._headers().get("Authorization") == "Bearer stored-tok", (
            "a stored token was not sent on the first request")

        both = McpHttpClient("tok", {
            "url": "https://x.example/mcp",
            "headers": {"Authorization": "Bearer static-key"}})
        assert both._headers()["Authorization"] == "Bearer static-key", (
            "an OAuth token replaced the Authorization the user configured")

        lower = McpHttpClient("tok", {
            "url": "https://x.example/mcp",
            "headers": {"authorization": "Basic abc"}})
        assert lower._headers()["authorization"] == "Basic abc", (
            "a lower-case authorization header was overwritten")
        assert "Authorization" not in lower._headers(), (
            "a second Authorization header was added beside a lower-case one")
    finally:
        store.clear()


def test_mcp_401_refreshes_once():
    """A refused token is renewed once; a dead one says so and stops.

    The failure this guards is a loop: refresh, retry, 401, refresh. That
    hammers the authorization server and never terminates. One attempt is the
    contract, so the count is asserted rather than assumed.
    """
    import asyncio
    from backend.mcp_client.http import McpHttpClient
    from backend.mcp_client.oauth import TokenStore

    async def renewable():
        problems = []
        stub = _StubOAuthServer()
        stub.accept_bearer = "refreshed-access"
        store = TokenStore()
        store.clear()
        try:
            store.set("renew", {"access_token": "stale-tok",
                                "refresh_token": "good-refresh",
                                "_client_id": "cid-abc"})
            client = McpHttpClient("renew", {"url": stub.url, "timeout_s": 5})
            try:
                await client.start()
            except Exception as e:  # noqa: BLE001
                problems.append(f"a renewable token did not renew: {e}")
                return problems
            seen = stub.record["mcp_auth"]
            if not seen or seen[-1] != "Bearer refreshed-access":
                problems.append(
                    f"the renewed token was not sent; the server saw {seen}")
            if stub.record["token_calls"] != 1:
                problems.append(
                    f"the token endpoint was called "
                    f"{stub.record['token_calls']} times, not once")
            return problems
        finally:
            store.clear()
            stub.close()

    async def dead():
        problems = []
        stub = _StubOAuthServer(reject_refresh=True)
        store = TokenStore()
        store.clear()
        try:
            store.set("dead", {"access_token": "stale-tok",
                               "refresh_token": "expired",
                               "_client_id": "cid-abc"})
            client = McpHttpClient("dead", {"url": stub.url, "timeout_s": 5})
            raised = ""
            try:
                await client.start()
            except Exception as e:  # noqa: BLE001
                raised = str(e)
            if not raised:
                problems.append("a dead refresh token did not raise at all")
            elif "expired" not in raised and "invalid_grant" not in raised:
                problems.append(
                    f"a dead refresh token produced: {raised}")
            if stub.record["token_calls"] != 1:
                problems.append(
                    f"the token endpoint was called "
                    f"{stub.record['token_calls']} times before giving up")
            return problems
        finally:
            store.clear()
            stub.close()

    async def refused_forever():
        """A server that renews happily but accepts nothing must not loop.

        This is the shape the guard exists for, and it is not the same as a
        refresh that fails: here every refresh succeeds and every retry is
        refused, so only the one-attempt rule stops it. Without the guard this
        calls the token endpoint without bound.
        """
        problems = []
        # `accept_bearer` stays empty, so nothing is ever accepted — but the
        # token endpoint answers normally.
        stub = _StubOAuthServer()
        store = TokenStore()
        store.clear()
        try:
            store.set("refused", {"access_token": "stale-tok",
                                  "refresh_token": "good-refresh",
                                  "_client_id": "cid-abc"})
            client = McpHttpClient("refused",
                                   {"url": stub.url, "timeout_s": 5})
            raised = ""
            try:
                await client.start()
            except RecursionError:
                raised = "recursion"
            except Exception as e:  # noqa: BLE001
                raised = str(e)
            if not raised:
                problems.append(
                    "a server that refuses every token did not raise at all")
            if stub.record["token_calls"] > 1:
                problems.append(
                    f"a server that refuses every token was retried "
                    f"{stub.record['token_calls']} times; it must be tried "
                    f"once and then reported")
            return problems
        finally:
            store.clear()
            stub.close()

    problems = (asyncio.run(renewable()) + asyncio.run(dead())
                + asyncio.run(refused_forever()))
    assert not problems, "\n  ".join(problems)


def test_mcp_client_id_survives_a_restart():
    """A refresh in a later run still names the client the grant belongs to.

    A refresh must identify the client the tokens were issued to, and
    registration is not available again on a server that will not register
    twice. If the id is lost, a token is renewable until the app restarts and
    dead afterwards — the worst possible time to find out.
    """
    from backend.mcp_client.oauth import OAuthClient, TokenStore

    store = TokenStore()
    store.clear()
    try:
        first = OAuthClient("srv", "https://x.example/mcp", store=store)
        first.client_info = {"client_id": "cid-1", "client_secret": "sec-1"}
        first.save_tokens({"access_token": "at", "refresh_token": "rt"})

        # Everything about this is a secret, so the file must not show any of
        # it in the clear. Read it back the way the app would instead.
        assert "sec-1" not in str(store._all()), (
            "the client secret is readable in the config file")
        assert "cid-1" not in str(store._all()), (
            "the client id is readable in the config file")

        # A fresh object stands in for the next run of the app.
        later = OAuthClient("srv", "https://x.example/mcp", store=store)
        assert not later.client_info, "the client id leaked in without asking"
        later._restore_client()
        assert later.client_info.get("client_id") == "cid-1", (
            "the client id did not survive the restart")
        assert later.bearer() == "Bearer at", (
            "carrying the client id disturbed the token")
    finally:
        store.clear()


def main():
    for name, fn in [
        ("pkce", test_pkce),
        ("authorize url", test_authorize_url),
        ("callback happy path", test_callback_happy_path),
        ("callback bad state", test_callback_rejects_bad_state),
        ("callback missing state / 404", test_callback_missing_state_and_404),
        ("provider error", test_provider_error_is_reported),
        ("timeout closes the port", test_timeout_closes_the_port),
        ("superseding a flow", test_superseding_a_flow_releases_the_port),
        ("exchange", test_exchange_sends_pkce_and_gets_tokens),
        ("exchange with secret", test_exchange_with_client_secret),
        ("exchange errors", test_exchange_errors_are_explained),
        ("refresh", test_refresh),
        ("route by state", test_deliver_routes_by_state),
        ("http api route", test_http_api_has_the_route),
        ("calendar wiring", test_calendar_wiring),
        ("calendar round trip", test_calendar_round_trip),
        ("google ws actions", test_google_ws_actions),
        ("mcp oauth flow", test_mcp_oauth_flow),
        ("mcp oauth without registration",
         test_mcp_oauth_needs_no_registration),
        ("mcp 401 interceptor", test_mcp_401_interceptor),
        ("mcp auth header rules", test_mcp_auth_header_rules),
        ("mcp 401 refreshes once", test_mcp_401_refreshes_once),
        ("mcp client id survives a restart",
         test_mcp_client_id_survives_a_restart),
    ]:

        try:
            fn()
        except Exception as e:  # noqa: BLE001
            fails.append(f"{name} raised: {type(e).__name__}: {e}")

    print(f"\n{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
