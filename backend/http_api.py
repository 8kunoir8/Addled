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

        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.startswith("/api/nav"):
                server = get_server()
                intent = server.consume_nav_intent() if server else None
                self._json({"path": intent})
            elif self.path.startswith("/api/ping"):
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, status=404)

        def log_message(self, *args):  # silence request logging
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="http-api").start()
    log.info("HTTP API on http://127.0.0.1:%d", port)
