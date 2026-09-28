"""Checks for the OpenAI-compatible endpoint in front of the local model.

Agent tools can point at this endpoint instead of at the local runner, because
the runner's own address is unusable for them:

* its port moves when 8090 is taken, so a configured tool breaks later;
* it advertises the GGUF file's absolute path as the model id, and ignores the
  `model` a client sends, echoing that path back;
* nothing listens until Addled itself wakes it, so an outside tool's first
  request meets a closed port; and
* it has no auth, so anything on the machine can use the GPU.

The directions that matter here are the ones that would be silently wrong:

* the model id must be a name, never a filesystem path;
* a token, once required, must actually be enforced;
* errors must be OpenAI-shaped JSON with a real status, not a hang or a
  truncated 200; and
* the endpoint must keep serving whatever the client asked for — tools,
  streaming, sampling — rather than quietly rewriting it.

The suite talks to the real facade over a real socket, but stubs the local
model, so it does not need a multi-gigabyte model installed or minutes of
inference. Run from the project root:

    .\\python-bundle\\python.exe -s .\\scripts\\check_model_api.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import model_api as mod  # noqa: E402

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

# -- a stub upstream, standing in for llamafile -----------------------------
#
# The facade only ever calls /chat/completions and /models on the upstream, so
# a tiny server that answers those two faithfully is enough to exercise every
# path the facade owns.

class _Upstream:
    """A minimal stand-in for the local model server."""

    def __init__(self):
        self.port = _free_port()
        self.seen: list[dict] = []
        self.available = True
        self._httpd = None

    def start(self):
        outer = self

        class H(mod.BaseHTTPRequestHandler if hasattr(mod, "BaseHTTPRequestHandler")
                else object):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if not outer.available:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # Mirrors the real runner: the id IS the file path.
                body = json.dumps({"object": "list", "data": [{
                    "id": "/models/Qwen3-8B-Q4_K_M.gguf",
                    "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
                outer.seen.append(payload)
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    # The real runner echoes the model path in each chunk.
                    chunk = ('data: {"choices":[{"delta":{"content":"hi"}}],'
                             '"model":"/models/Qwen3-8B-Q4_K_M.gguf"}\n\n')
                    self.wfile.write(chunk.encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                body = json.dumps({
                    "id": "chatcmpl-x", "object": "chat.completion",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": "PONG"}}],
                    # The upstream reports itself by file path.
                    "model": "/models/Qwen3-8B-Q4_K_M.gguf",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        from http.server import ThreadingHTTPServer
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _request(port, method, path, payload=None, token=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(payload).encode() if payload is not None else None
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", "replace")
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, ctype, raw


upstream = _Upstream()
upstream.start()

# Point the facade at the stub, and give it a token so auth is exercised.
from backend.config import config  # noqa: E402

# Remember the user's real settings so the suite cannot leave the endpoint
# switched on, or a test port or test token behind. It writes to the same
# settings file the app uses.
_SAVED = {k: config.get("model_api", k, default=None)
          for k in ("enabled", "port", "token")}

config.set("model_api", "enabled", value=True)
config.set("model_api", "port", value=_free_port())
config.set("model_api", "token", value="sk-addled-testtoken")

real_base = mod._upstream_base
mod._upstream_base = lambda: f"http://127.0.0.1:{upstream.port}/v1"

server = mod.ModelApiServer()
# The token helper caches on the instance; make sure it reads the config value.
server._token = mod._Token(config)

ok, detail = server.start()
check("the endpoint starts", ok, detail)
port = server.port
token = server.token()

try:
    # -- the model id is a name, not a path --------------------------------
    st, _, body = _request(port, "GET", "/v1/models", token=token)
    check("GET /v1/models succeeds", st == 200, f"status {st}")
    ids = [m["id"] for m in json.loads(body).get("data", [])]
    check("the model id is not a filesystem path",
          ids and all("/" not in i and "\\" not in i and not i.endswith(".gguf")
                      for i in ids),
          f"ids: {ids}")
    check("the model id is the documented name", ids == [mod.MODEL_ID],
          f"got {ids}")

    # -- auth is enforced --------------------------------------------------
    st, _, body = _request(port, "GET", "/v1/models")
    check("no token is refused", st == 401, f"status {st}")
    check("the refusal is OpenAI-shaped",
          json.loads(body).get("error", {}).get("type") == "authentication_error",
          f"body: {body[:120]}")

    st, _, _ = _request(port, "GET", "/v1/models", token="sk-addled-wrong")
    check("a wrong token is refused", st == 401, f"status {st}")

    st, _, _ = _request(port, "POST", "/v1/chat/completions",
                        payload={"messages": [{"role": "user", "content": "x"}]},
                        token="sk-addled-wrong")
    check("a wrong token cannot reach the model", st == 401, f"status {st}")
    check("a refused request never reached the upstream",
          all(p.get("messages") != [{"role": "user", "content": "x"}]
              for p in upstream.seen),
          "the upstream was called despite the bad token")

    # -- health is reachable without a token, and says little --------------
    st, _, body = _request(port, "GET", "/v1/health")
    check("health needs no token", st == 200, f"status {st}")
    health = json.loads(body)
    check("health does not leak the token", "token" not in health,
          f"keys: {sorted(health)}")

    # -- errors are shaped, with real statuses -----------------------------
    st, _, body = _request(port, "GET", "/v1/nope", token=token)
    check("an unknown path is 404", st == 404, f"status {st}")
    check("the 404 body is OpenAI-shaped", "error" in json.loads(body),
          f"body: {body[:120]}")

    st, _, body = _request(port, "POST", "/v1/chat/completions",
                           payload={}, token=token)
    check("a missing 'messages' is 400", st == 400, f"status {st}")

    # A body that is not JSON must be a 400, not an exception or a hang.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    conn.request("POST", "/v1/chat/completions", body=b"{not json",
                 headers={"Content-Type": "application/json",
                          "Authorization": "Bearer " + token})
    resp = conn.getresponse()
    check("a malformed body is 400", resp.status == 400, f"status {resp.status}")
    resp.read()
    conn.close()

    # -- the request is passed through, not rewritten ----------------------
    tools = [{"type": "function",
              "function": {"name": "t", "description": "d", "parameters": {}}}]
    st, _, _ = _request(port, "POST", "/v1/chat/completions", token=token,
                        payload={"model": "whatever-the-client-said",
                                 "messages": [{"role": "user", "content": "hi"}],
                                 "tools": tools, "tool_choice": "auto",
                                 "temperature": 0.25, "max_tokens": 77})
    check("a normal completion succeeds", st == 200, f"status {st}")
    sent = upstream.seen[-1] if upstream.seen else {}
    check("tools are forwarded untouched", sent.get("tools") == tools,
          f"tools came through as {sent.get('tools')!r}")
    check("tool_choice is forwarded", sent.get("tool_choice") == "auto",
          f"got {sent.get('tool_choice')!r}")
    check("sampling parameters are not rewritten",
          sent.get("temperature") == 0.25 and sent.get("max_tokens") == 77,
          f"temperature={sent.get('temperature')} max_tokens={sent.get('max_tokens')}")
    check("the client's model id is replaced with ours",
          sent.get("model") == mod.MODEL_ID,
          f"upstream saw model={sent.get('model')!r}")

    # -- the reply never shows the upstream's path -------------------------
    st, _, body = _request(port, "POST", "/v1/chat/completions", token=token,
                           payload={"model": "x",
                                    "messages": [{"role": "user", "content": "hi"}]})
    d = json.loads(body)
    check("the reply reports our model name", d.get("model") == mod.MODEL_ID,
          f"model was {d.get('model')!r}")
    check("the reply body is otherwise intact",
          d["choices"][0]["message"]["content"] == "PONG",
          f"content: {d['choices'][0]['message'].get('content')!r}")

    # -- streaming is relayed, and also repointed --------------------------
    st, ctype, body = _request(port, "POST", "/v1/chat/completions", token=token,
                               payload={"model": "x", "stream": True,
                                        "messages": [{"role": "user",
                                                      "content": "hi"}]})
    check("streaming returns SSE",
          st == 200 and "text/event-stream" in ctype,
          f"status {st} ctype {ctype!r}")
    check("the stream terminates with [DONE]",
          body.strip().endswith("data: [DONE]"), f"tail: {body[-60:]!r}")
    check("no stream chunk exposes the upstream path",
          "/models/" not in body and ".gguf" not in body,
          "the GGUF path leaked into the stream")

    # -- a stream frame stays valid whatever the key order ----------------
    #
    # This was a real bug: the model field was rewritten by text surgery that
    # assumed something followed it. When "model" was the LAST key the closing
    # brace was dropped, producing a malformed frame mid-stream — after the 200
    # and the headers, so no error could be reported and a strict client just
    # failed. Every frame below must stay parseable.
    frames = [
        ("model last", b'data: {"choices":[],"model":"/a/b.gguf"}\n\n'),
        ("model first", b'data: {"model":"/a/b.gguf","choices":[]}\n\n'),
        ("model middle",
         b'data: {"a":1,"model":"/a/b.gguf","b":2}\n\n'),
        ("escaped win path",
         b'data: {"model":"C:\\\\m\\\\x.gguf","c":[]}\n\n'),
        ("crlf frame", b'data: {"model":"/x.gguf","a":1}\r\n\r\n'),
    ]
    for label, frame in frames:
        out = mod._repoint_model(frame)
        try:
            json.loads(out.split(b"data: ", 1)[1].decode().strip())
            parses = True
        except Exception:
            parses = False
        check(f"a stream frame survives rewriting ({label})", parses,
              f"became invalid JSON: {out!r}")
        check(f"no path leaks from a stream frame ({label})",
              b".gguf" not in out, f"still held a path: {out!r}")

    # Non-payload lines must pass through untouched, or a stream is corrupted.
    for label, frame in (("done sentinel", b"data: [DONE]\n\n"),
                         ("keepalive", b": ping\n\n"),
                         ("no model field",
                          b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')):
        check(f"a frame without a model field is untouched ({label})",
              mod._repoint_model(frame) == frame,
              f"was altered to {mod._repoint_model(frame)!r}")

    # -- an unreachable model is a clear 503, not a hang -------------------
    # A closed port, rather than flipping the stub's flag: `_ensure_upstream`
    # deliberately prefers any live upstream, so a real local model running on
    # this machine would satisfy the stub's 503 and this would pass for the
    # wrong reason.
    upstream.available = False
    mod._upstream_base = lambda: f"http://127.0.0.1:{_free_port()}/v1"
    st, _, body = _request(port, "POST", "/v1/chat/completions", token=token,
                           payload={"model": "x",
                                    "messages": [{"role": "user", "content": "hi"}]},
                           timeout=30)
    check("an unreachable model is reported, not hung",
          st in (502, 503), f"status {st}")
    check("the unreachable error is explanatory",
          "error" in json.loads(body)
          and len(json.loads(body)["error"]["message"]) > 20,
          f"body: {body[:160]}")
    upstream.available = True
    mod._upstream_base = lambda: f"http://127.0.0.1:{upstream.port}/v1"

    # -- config gating -----------------------------------------------------
    server.stop()
    check("stop() releases the port", not server.is_running(),
          "still running after stop")
    config.set("model_api", "enabled", value=False)
    fresh = mod.ModelApiServer()
    fresh._token = mod._Token(config)
    ok, detail = fresh.start()
    check("a disabled endpoint refuses to start", not ok,
          f"started anyway: {detail}")
    check("the refusal says why", "disabled" in detail.lower(),
          f"detail: {detail!r}")

finally:
    server.stop()
    upstream.stop()
    mod._upstream_base = real_base
    # Put the user's settings back exactly as they were, so running the checks
    # never turns the endpoint on or leaves a test token behind.
    for _k, _v in _SAVED.items():
        config.set("model_api", _k, value=_v)
    _after = config.get("model_api", default={})
    if (_after.get("enabled") or _after.get("token")
            or _after.get("port") != _SAVED.get("port")):
        fails.append("the suite left model_api settings changed: "
                     f"{_after}")

if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
    print(f"\n{len(fails)} failure(s)")
    sys.exit(1)

print("PASS: model API -- a fixed address in front of the local model, with a "
      "real model id, an enforced token, OpenAI-shaped errors and untouched "
      "tools/streaming")
