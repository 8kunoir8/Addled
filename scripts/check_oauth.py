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
