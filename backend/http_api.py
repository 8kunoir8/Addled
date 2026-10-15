"""
Minimal HTTP API for the Electron shell.

The Electron main process polls GET /api/nav every ~1.5s and, when a path
is present, shows/focuses the Addled window and loads that dashboard page.
This is the reliable channel for "character menu → open GUI at /settings"
because a hidden Electron window throttles the renderer's WebSocket.

Single-shot: consume_nav_intent() clears the intent when read.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("addled.http_api")


def start_http_api(port: int = 9877) -> None:
    from backend.ws_server import get_server

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.startswith("/api/nav"):
                server = get_server()
                intent = server.consume_nav_intent() if server else None
                self._json({"path": intent})
            elif self.path.startswith("/api/ping"):
                self._json({"ok": True})
            elif self.path.startswith("/oauth/callback"):
                self._oauth_callback()
            else:
                self._json({"error": "not found"}, status=404)

        def _oauth_callback(self) -> None:
            """Receive a provider's redirect for an authorization in flight.

            This route is here rather than on the per-attempt loopback server
            because some providers are told a fixed redirect URI in advance and
            a port that changes each time cannot be pre-registered. The waiting
            flow is looked up by its `state`; an unknown state is answered
            honestly and changes nothing, so a stray or replayed request cannot
            connect anything.
            """
            from backend.integrations import oauth_loopback
            import urllib.parse
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)

            def one(name: str) -> str:
                return str((query.get(name) or [""])[0])

            state = one("state")
            error = one("error")
            code = one("code")
            if error:
                # `error_description` is the provider's own words and is more
                # use than the code.
                message = one("error_description") or error
                oauth_loopback.deliver(state, error=f"The provider refused: "
                                                    f"{message}")
                self._html(oauth_loopback._error_page(message))
                return
            if not state:
                self._html(oauth_loopback._error_page(
                    "This reply carried no state, so it could not be matched "
                    "to a request. Nothing was connected."), status=400)
                return
            if oauth_loopback.deliver(state, code=code):
                self._html(oauth_loopback._success_page())
                return
            self._html(oauth_loopback._error_page(
                "This reply did not match a connection Addled is waiting for. "
                "It may have been used already, or the attempt timed out."),
                status=400)

        def log_message(self, *args):  # silence request logging
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="http-api").start()
    log.info("HTTP API on http://127.0.0.1:%d", port)
