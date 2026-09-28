"""An OpenAI-compatible front door to Addled's local model.

Agent tools (GitHub Copilot, Codex, and anything else speaking the OpenAI
protocol) can point at this instead of at the local runner, because the local
runner's own address is unusable for them in three ways:

1. **Its port moves.** The runner is handed 8090, but takes the next free port
   when that one is busy, so a tool configured once breaks on the next launch.
   This facade keeps one fixed port and finds the runner wherever it is.
2. **Nothing is listening until something wakes it.** The local model is never
   started eagerly — it boots when Addled itself would use it. An outside tool
   asking first would meet a closed port. This facade starts it on demand.
3. **It advertises a file path as the model id.** ``/v1/models`` on the runner
   reports the absolute path of the GGUF file, and the runner ignores whatever
   ``model`` you send, echoing that path back. Tools that validate or display
   the model name get a filesystem path. This facade gives them a real name.

What it deliberately does NOT do is translate between protocols, rewrite
sampling parameters, or second-guess tool calls. The runner was measured to
implement the OpenAI shape correctly — including ``tools`` and SSE streaming —
so the facade passes those through untouched. Anything it rewrote would be a
place for the two to disagree.

Anthropic-shaped clients (Claude Code) speak a different protocol and are not
served here.

Loopback only. This exposes a language model, not a sandbox: anything able to
reach the port can use the machine's GPU.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("addled.model_api")

# The name clients are told to ask for. A path is not an id, and the runner
# accepts any id anyway, so one clean name is enough.
MODEL_ID = "addled-local"

# A turn on the local 8B model takes minutes, and an agent may open with a long
# prompt. Generous on purpose: timing out mid-answer is worse than waiting.
UPSTREAM_TIMEOUT_S = 600


class _Token:
    """The bearer token this facade requires.

    Generated once and kept in config so it survives restarts — a token that
    changed every launch would mean re-configuring every tool each time.
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self._lock = threading.Lock()

    def _read(self) -> str:
        try:
            return str(self._cfg.get("model_api", "token", default="") or "")
        except Exception:  # noqa: BLE001
            return ""

    def value(self) -> str:
        existing = self._read()
        if existing:
            return existing
        with self._lock:
            # Re-read inside the lock: two threads may have raced here, and a
            # second token would silently invalidate the first.
            existing = self._read()
            if existing:
                return existing
            token = "sk-addled-" + secrets.token_hex(16)
            try:
                self._cfg.set("model_api", "token", value=token)
            except Exception as e:  # noqa: BLE001
                log.warning("could not persist the model API token: %s", e)
            return token


def _openai_error(status: int, message: str,
                  kind: str = "invalid_request_error") -> dict:
    """An error body shaped the way OpenAI clients expect.

    A bare string would leave a client parsing JSON and failing with a decode
    error instead of showing the user what actually went wrong.
    """
    return {"error": {"message": message, "type": kind,
                      "param": None, "code": None}}


class ModelApiServer:
    """Owns the fixed-port HTTP surface in front of the local model."""

    def __init__(self):
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._token: _Token | None = None

    # -- config --------------------------------------------------------------

    def _cfg(self, *keys, default=None):
        from backend.config import config
        return config.get("model_api", *keys, default=default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enabled", default=False))

    @property
    def host(self) -> str:
        # Loopback only. A model endpoint on a LAN is a real decision, not a
        # default, and silently binding wide would be the wrong one.
        return "127.0.0.1"

    @property
    def port(self) -> int:
        try:
            return int(self._cfg("port", default=8099) or 8099)
        except (TypeError, ValueError):
            return 8099

    def token(self) -> str:
        if self._token is None:
            from backend.config import config
            self._token = _Token(config)
        return self._token.value()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def is_running(self) -> bool:
        return self._httpd is not None

    # -- lifecycle -----------------------------------------------------------

    def status(self) -> dict:
        model_running = False
        model_port = None
        model_installed = False
        try:
            from backend.local_llm.manager import local_llm
            model_running = bool(local_llm.is_running())
            model_port = local_llm.port()
        except Exception:  # noqa: BLE001
            pass
        try:
            from backend.local_models import paths
            model_installed = bool(paths.installed())
        except Exception:  # noqa: BLE001
            pass

        running = self.is_running()
        return {
            "enabled": self.enabled,
            "running": running,
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url,
            # Shown so the user can paste it into a tool. It is a convenience
            # for the person who owns the machine, not a hidden secret.
            "token": self.token() if running else "",
            "model_id": MODEL_ID,
            "model_running": model_running,
            "model_port": model_port,
            "model_installed": model_installed,
            # Reachable on this machine only, so a remote tool cannot use it.
            "loopback_only": True,
        }

    def start(self) -> tuple[bool, str]:
        if self._httpd is not None:
            return True, "already running"
        if not self.enabled:
            return False, "the model API is disabled in Settings"

        handler = _handler_for(self)
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            return False, f"could not listen on {self.host}:{self.port}: {e}"

        # Threaded on purpose: an agent may hold one long streaming request
        # while a second connection asks /v1/models, and a single-threaded
        # server would make the second wait for the first to finish.
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                        name="model-api")
        self._thread.start()
        log.info("Model API on %s (model: %s)", self.base_url, MODEL_ID)
        return True, self.base_url

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception as e:  # noqa: BLE001
                log.debug("could not stop the model API cleanly: %s", e)


def _handler_for(server: ModelApiServer):
    """Build the handler class, bound to one server instance."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # -- helpers ---------------------------------------------------------

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError):
                pass

        def _error(self, status: int, message: str,
                   kind: str = "invalid_request_error") -> None:
            self._json(_openai_error(status, message, kind), status=status)

        def _authorized(self) -> bool:
            """Check the bearer token.

            Compared with a constant-time equality so the check does not leak
            the token one character at a time to something probing the port.
            """
            expected = server.token()
            if not expected:
                return True
            header = self.headers.get("Authorization", "") or ""
            if not header.lower().startswith("bearer "):
                return False
            return secrets.compare_digest(header[7:].strip(), expected)

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        # -- routing ---------------------------------------------------------

        def do_GET(self):  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/v1/models", "/models"):
                if not self._authorized():
                    return self._error(401, "Incorrect API key provided.",
                                       "authentication_error")
                return self._json(self._models())
            if path in ("/v1/health", "/health"):
                # No auth: this is what a user or a tool's "test connection"
                # button hits, and it exposes nothing but liveness.
                return self._json({"ok": True, "model": MODEL_ID,
                                   "upstream": _upstream_running()})
            return self._error(404, f"Unknown path: {path}")

        def do_POST(self):  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path not in ("/v1/chat/completions", "/chat/completions"):
                return self._error(404, f"Unknown path: {path}")
            if not self._authorized():
                return self._error(401, "Incorrect API key provided.",
                                   "authentication_error")
            payload = self._read_json()
            if payload is None:
                return self._error(400, "The request body was not valid JSON.")
            if not payload.get("messages"):
                return self._error(400, "'messages' is required.")
            self._chat(payload)

        # -- the two real endpoints -----------------------------------------

        def _models(self) -> dict:
            return {
                "object": "list",
                "data": [{
                    "id": MODEL_ID,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "addled",
                }],
            }

        def _chat(self, payload: dict) -> None:
            """Forward a completion, streaming the reply back if asked.

            The upstream is woken BEFORE any header is sent, so a failure to
            reach the model is still reportable as a proper JSON error with a
            status code, rather than as a truncated 200.
            """
            problem = _ensure_upstream()
            if problem:
                return self._error(503, problem, "service_unavailable")

            body = dict(payload)
            # Whatever the client asked for, it means our local model. The
            # upstream ignores this field, but a client reading it back should
            # see the name it used, not a file path.
            body["model"] = MODEL_ID
            stream = bool(body.get("stream"))

            req = urllib.request.Request(
                _upstream_base() + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer sk-local"})
            try:
                resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass
                return self._error(
                    e.code or 502,
                    f"the local model refused the request: "
                    f"{detail or e.reason}")
            except Exception as e:  # noqa: BLE001
                return self._error(502, f"could not reach the local model: {e}",
                                   "api_error")

            if stream:
                self._stream(resp)
            else:
                self._buffered(resp)

        def _buffered(self, resp) -> None:
            try:
                raw = resp.read()
            except Exception as e:  # noqa: BLE001
                return self._error(
                    502, f"the local model stopped mid-reply: {e}", "api_error")
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                # Not JSON: hand it back as-is rather than pretending.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionAbortedError):
                    pass
                return
            if isinstance(parsed, dict):
                # The upstream echoes its model path; report the name instead.
                parsed["model"] = MODEL_ID
                self._json(parsed)
            else:
                self._error(502, "the local model returned an unexpected reply",
                            "api_error")

        def _stream(self, resp) -> None:
            """Relay SSE, flushing every chunk.

            Chunks are written as they arrive because a client reading a stream
            wants tokens as they are produced; buffering would quietly turn a
            stream into a slow non-streaming reply.
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for raw in resp:
                    self.wfile.write(_repoint_model(raw))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError):
                # The client hung up — normal when a user cancels a turn.
                pass
            except Exception as e:  # noqa: BLE001
                log.debug("stream ended early: %s", e)
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
            self.close_connection = True

        def log_message(self, *args):  # silence per-request logging
            pass

        # A client that hangs up mid-request (a cancelled turn, a closed tool)
        # otherwise makes http.server print a full traceback from its own error
        # path. That is normal traffic for a model endpoint, not a fault, and
        # the noise would bury a real error.
        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError,
                    BrokenPipeError, TimeoutError):
                self.close_connection = True
            except ValueError:
                # A malformed request line; the client is not speaking HTTP.
                self.close_connection = True

    return Handler


def _repoint_model(raw: bytes) -> bytes:
    """Rewrite the ``model`` field in one SSE frame to our public name.

    Parsed as JSON rather than edited as text. Text surgery on a JSON object is
    only correct while the field happens to be followed by a comma: when
    ``model`` is the LAST key, there is nothing after it to keep, and the frame
    was rewritten without its closing brace — a malformed SSE frame that a
    strict client rejects, in the middle of a stream that had already started
    so no error could be reported.

    Anything not a JSON data frame is passed through untouched, so comments,
    keep-alives and the ``[DONE]`` sentinel are never altered.
    """
    if b'"model"' not in raw:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw

    # Only "data: {...}" frames carry a payload. Keep the exact trailing
    # whitespace so frame boundaries survive the round trip.
    if not text.startswith("data: {"):
        return raw
    newline = ""
    body = text
    for ending in ("\r\n\r\n", "\n\n", "\r\n", "\n"):
        if body.endswith(ending):
            newline = ending
            body = body[: -len(ending)]
            break
    payload = body[len("data: "):]
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        # Not something we understand; leave it exactly as it came.
        return raw
    if not isinstance(parsed, dict) or "model" not in parsed:
        return raw
    parsed["model"] = MODEL_ID
    return ("data: " + json.dumps(parsed) + newline).encode("utf-8")


def _upstream_base() -> str:
    """Where the local model is listening *right now*.

    Re-read on every request because the port changes when 8090 is taken.
    """
    from backend.local_llm.manager import local_llm
    return local_llm.api_base()


def _upstream_running() -> bool:
    """Whether something is actually answering at the local model's address.

    Asks the server rather than reading a flag: a process can be alive while
    still loading weights, and a flag can be set with nothing listening. This
    is what makes the check honest about what a client will experience.
    """
    try:
        req = urllib.request.Request(_upstream_base() + "/models",
                                     headers={"Authorization": "Bearer sk-local"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False

def _ensure_upstream() -> str:
    """Make sure the local model is up. Returns "" or the reason it is not.

    This is the point of the facade: an outside tool cannot start the model
    itself, and reaching a closed port is the experience Addled would otherwise
    be handing it.
    """
    # If the model is already answering, use it. This matters because the file
    # check below is relative to *this* installation, and a model may be live
    # from another one (or started by hand) — refusing in that case would be
    # wrong when the thing the caller needs is demonstrably working.
    if _upstream_running():
        return ""

    try:
        from backend.local_llm.manager import local_llm
    except Exception as e:  # noqa: BLE001
        return f"the local model is unavailable in this build: {e}"
    try:
        from backend.local_models import paths
        if not paths.installed():
            return ("the local model is not installed yet - open Addled and "
                    "approve the download in Settings, then try again")
    except Exception as e:  # noqa: BLE001
        log.debug("could not check the local model install: %s", e)

    # ensure_running() is a coroutine owned by the app's loop, and this handler
    # runs on an HTTP worker thread. Scheduling it back onto the loop and
    # waiting is what lets an external request boot the model.
    try:
        problem = _run_on_loop(local_llm.ensure_running())
    except Exception as e:  # noqa: BLE001
        return f"could not start the local model: {e}"
    if problem:
        return str(problem)
    return ""


def _run_on_loop(coro) -> str | None:
    """Await a coroutine on the application's event loop, from a worker thread.

    The model manager lives on the loop that started it; calling into it from
    an HTTP thread without hopping back would raise. If there is no loop
    (tests, a headless import) the coroutine is run on its own loop instead,
    which keeps this callable outside the full app.
    """
    import asyncio

    loop = None
    try:
        from backend.ws_server import get_server
        server = get_server()
        # The model manager was started on the WebSocket server's loop, so that
        # is the loop a coroutine touching it has to run on.
        loop = getattr(server, "_loop", None) if server is not None else None
    except Exception:  # noqa: BLE001
        loop = None

    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=UPSTREAM_TIMEOUT_S)
    return asyncio.run(coro)


# Singleton, matching the rest of the backend.
model_api = ModelApiServer()
