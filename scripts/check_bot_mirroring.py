"""Checks for bot traffic appearing on the chat page.

A message sent from Telegram reached Addled and got an answer the chat page
never saw, because `chat_send` returned to its caller and told nobody. The two
conversations then drifted: the app looked as if it had ignored the phone, and
a reply the user read in Telegram was invisible in the app.

What is worth testing here is the pair of directions that would be silently
wrong rather than merely broken:

* a turn the chat page sent itself must **not** be announced, or the whole
  exchange appears twice — the page already drew both bubbles; and
* a turn from an unknown or absent source must still render plainly, rather
  than erroring or printing a raw id at the user.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bot_mirroring.py
"""

from __future__ import annotations

import builtins
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import chat_sources
import backend.ws_server as ws

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

class _Recorder:
    """Stands in for the live server so nothing is actually broadcast."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def broadcast_nowait(self, method, params=None):
        self.sent.append((method, params or {}))

    def pushes(self):
        return [p for m, p in self.sent if m == "chat.push"]

def run():
    # ---- the source registry ------------------------------------------------
    for platform in ("telegram", "discord", "whatsapp"):
        entry = chat_sources.describe(platform)
        check(f"{platform} has a label", bool(entry["source_label"]),
              str(entry))
        check(f"{platform} has an icon", bool(entry["source_icon"]),
              str(entry))
        check(f"{platform} keeps its own id",
              entry["source"] == platform, str(entry))

    check("an unknown source is treated as the default, not passed through",
          chat_sources.normalise("myspace") == chat_sources.DEFAULT,
          chat_sources.normalise("myspace"))
    check("an empty source is the default",
          chat_sources.normalise("") == chat_sources.DEFAULT, "")
    check("a None source is the default",
          chat_sources.normalise(None) == chat_sources.DEFAULT, "")
    check("a source differing only in case still resolves",
          chat_sources.normalise("TELEGRAM") == "telegram",
          chat_sources.normalise("TELEGRAM"))
    check("surrounding whitespace is tolerated",
          chat_sources.normalise("  discord  ") == "discord",
          chat_sources.normalise("  discord  "))
    check("the default has a human label, so no bubble shows a raw id",
          chat_sources.label(chat_sources.DEFAULT).strip() != "",
          chat_sources.label(chat_sources.DEFAULT))

    # ---- what gets announced ------------------------------------------------
    rec = _Recorder()
    real_get_server = ws.get_server
    ws.get_server = lambda: rec
    try:
        # A bot's turn announces both bubbles, labelled.
        rec.sent.clear()
        ws._announce_turn({"source": "telegram"}, "hello from a phone",
                          "hello back")
        pushes = rec.pushes()
        check("a bot turn announces two bubbles", len(pushes) == 2,
              f"{len(pushes)} push(es)")
        roles = [p.get("role") for p in pushes]
        check("the first bubble is the user's",
              roles[:1] == ["user"], str(roles))
        check("the second bubble is the reply",
              roles[1:2] == ["assistant"], str(roles))
        check("both carry the platform label",
              all(p.get("source_label") == "Telegram" for p in pushes),
              str([p.get("source_label") for p in pushes]))
        check("both carry the same timestamp, so they read as one turn",
              len({p.get("timestamp") for p in pushes}) == 1,
              str([p.get("timestamp") for p in pushes]))
        check("the user's words are forwarded verbatim",
              pushes[0].get("content") == "hello from a phone",
              str(pushes[0].get("content")))
        check("the reply is forwarded verbatim",
              pushes[1].get("content") == "hello back",
              str(pushes[1].get("content")))

        # The chat page's own turn must not come back to it.
        for local in ("dashboard", "DASHBOARD", " dashboard "):
            rec.sent.clear()
            ws._announce_turn({"source": local}, "typed here", "answered here")
            check(f"a turn from {local!r} is not announced",
                  rec.pushes() == [], str(rec.pushes()))

        # An undeclared source still shows, plainly labelled.
        rec.sent.clear()
        ws._announce_turn({}, "no source declared", "reply")
        pushes = rec.pushes()
        check("a turn with no source is still announced", len(pushes) == 2,
              f"{len(pushes)} push(es)")
        check("and is labelled with the default rather than a blank",
              pushes and bool(pushes[0].get("source_label")),
              str(pushes[:1]))

        # A failed turn announces the question, not the error text.
        rec.sent.clear()
        ws._announce_turn({"source": "discord"}, "ask something",
                          "[Provider error: no key]")
        pushes = rec.pushes()
        check("a provider failure does not post the error into the chat",
              len(pushes) == 1 and pushes[0].get("role") == "user",
              str([(p.get("role"), p.get("content")) for p in pushes]))

        rec.sent.clear()
        ws._announce_turn({"source": "discord"}, "ask something", "")
        check("an empty reply posts only the question",
              len(rec.pushes()) == 1, str(len(rec.pushes())))

        rec.sent.clear()
        ws._announce_turn({"source": "discord"}, "ask something", "   ")
        check("a whitespace-only reply posts only the question",
              len(rec.pushes()) == 1, str(len(rec.pushes())))

        # No server yet must not raise: the announcement is a courtesy.
        ws.get_server = lambda: None
        try:
            ws._announce_turn({"source": "telegram"}, "x", "y")
            check("announcing with no server running does not raise", True)
        except Exception as e:  # noqa: BLE001
            check("announcing with no server running does not raise", False,
                  repr(e))

        # A broken registry must not turn a working turn into an error.
        ws.get_server = lambda: rec
        real_import = builtins.__import__

        def _explode(name, *args, **kwargs):
            if name == "backend" or name.endswith("chat_sources"):
                raise ImportError("simulated failure")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = _explode
        try:
            ws._announce_turn({"source": "telegram"}, "x", "y")
            check("a registry that cannot be imported does not raise", True)
        except Exception as e:  # noqa: BLE001
            check("a registry that cannot be imported does not raise", False,
                  repr(e))
        finally:
            builtins.__import__ = real_import
    finally:
        ws.get_server = real_get_server

    # ---- the broadcast actually reaches a connected client ------------------
    # This is the defect that made the feature look implemented and do nothing:
    # `broadcast_nowait` reported a live connection and delivered the frame to
    # nobody, because the connection was removed from the broadcast set as soon
    # as its first request finished. Each request is its own task, so the
    # quickest one to complete unsubscribed a client that was still connected.
    import asyncio
    import json as _json

    try:
        import websockets
    except ImportError:
        websockets = None

    if websockets is None:
        check("websockets is available to test the transport", False,
              "the broadcast cannot be exercised without it")
    else:
        async def _delivery():
            received: list[str] = []

            async def handler(sock):
                try:
                    async for raw in sock:
                        msg = _json.loads(raw)
                        if msg.get("method"):
                            received.append(msg["method"])
                        else:
                            await sock.send(_json.dumps(
                                {"jsonrpc": "2.0", "id": msg.get("id"),
                                 "result": {"ok": True}}))
                except Exception:  # noqa: BLE001
                    return

            server = await websockets.serve(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            srv = ws.WSServer()

            async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
                # Stand in for a live connection, the way the handler registers
                # one, and for a server that has recorded its loop.
                class _Conn:
                    async def send(self, payload):
                        await client.send(payload)

                srv._loop = asyncio.get_running_loop()
                srv._connections.add(_Conn())  # type: ignore[arg-type]

                async def listen():
                    try:
                        while True:
                            received.append(_json.loads(
                                await asyncio.wait_for(client.recv(), 5))
                                .get("method", "?"))
                    except Exception:  # noqa: BLE001
                        return

                task = asyncio.create_task(listen())
                # A plain notification must reach the client.
                srv.broadcast_nowait("chat.push", {"role": "user",
                                                   "content": "x"})
                await asyncio.sleep(1.5)
                task.cancel()

            server.close()
            await server.wait_closed()
            return received

        try:
            seen = asyncio.run(_delivery())
        except Exception as e:  # noqa: BLE001
            seen = []
            check("the delivery probe runs", False, repr(e))
        check("a broadcast from inside the loop reaches a connected client",
              "chat.push" in seen, f"received {seen}")

    # ---- the connection is dropped with the socket, not with the request ----
    # The removal used to live in `_serve`, which runs once per *request*. With
    # one task per request, the quickest one to finish unsubscribed a client
    # that was still connected: the reply still went out, because that goes to
    # the socket directly, while every later broadcast reached nobody.
    #
    # Read as text, and located by brace-free slicing that does not depend on
    # the two functions appearing in a particular order in the file.
    root_ws = os.path.join(ROOT, "backend", "ws_server.py")
    try:
        with open(root_ws, encoding="utf-8") as f:
            server_src = f.read()
    except OSError as e:
        check("backend/ws_server.py is readable", False, str(e))
        server_src = ""

    if server_src:
        def body_of(name: str) -> str:
            """The source of one method, up to the next method at that indent."""
            start = server_src.find(f"    async def {name}(")
            if start < 0:
                start = server_src.find(f"    def {name}(")
            if start < 0:
                return ""
            tail = server_src[start + 1:]
            nxt = tail.find("\n    async def ")
            nxt2 = tail.find("\n    def ")
            ends = [n for n in (nxt, nxt2) if n > 0]
            return tail[:min(ends)] if ends else tail

        serve_body = body_of("_serve")
        check("_serve was found, so the next check is not vacuous",
              bool(serve_body), "could not locate `_serve`")
        check("a finished request does not unsubscribe its connection",
              "_connections" not in serve_body,
              "_serve still touches the broadcast set, so the first request "
              "to finish unsubscribes a live client")
        check("the socket's own teardown unsubscribes it",
              "self._connections.discard(ws)" in body_of("_handle_connection"),
              "nothing removes a closed connection, so the set only grows")

    # ---- the bot scripts declare their platform -----------------------------
    #
    # Matched across the whole call rather than line by line: the arguments are
    # formatted over several lines now, and a check that only understood the
    # one-line spelling would fail on correct code — which is worse than not
    # checking, because it teaches you to ignore the failure.
    import re

    bots = os.path.join(ROOT, "bots")
    for platform in ("telegram", "discord", "whatsapp"):
        path = os.path.join(bots, f"{platform}-bot.js")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            check(f"{platform}-bot.js is readable", False, str(e))
            continue
        # Every `ws.send('chat.send', { ... })` call, however it is wrapped.
        calls = re.findall(r"ws\.send\(\s*'chat\.send'\s*,\s*\{(.*?)\}\s*\)",
                           text, re.S)
        check(f"{platform}-bot.js has at least one chat.send to tag",
              bool(calls), "no chat.send found — did the bridge change?")
        for index, body in enumerate(calls):
            check(f"{platform}-bot.js chat.send #{index + 1} declares its source",
                  f"source: '{platform}'" in body,
                  "this call would mirror with no platform label")
            check(f"{platform}-bot.js chat.send #{index + 1} names its "
                  f"conversation",
                  "conversation:" in body,
                  "an approval raised by this call could not be answered here")

    # ---- the dashboard declares its own turns ------------------------------
    page = os.path.join(ROOT, "dashboard", "src", "app", "chat", "page.tsx")
    try:
        with open(page, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        check("the chat page is readable", False, str(e))
        src = ""
    if src:
        check("the chat page marks its own turns as coming from the dashboard",
              "source: 'dashboard'" in src, "the send does not declare a source")
        check("the chat page reads the source fields off a push",
              "source_label" in src and "source_icon" in src,
              "a pushed message would render with no badge")
        check("the chat page renders a badge",
              "via {msg.sourceLabel}" in src,
              "the source fields are read but never shown")

    # ---- the proactive paths are labelled too ------------------------------
    for rel, needle in (
        ("backend/tasks/actions.py", 'chat_sources.describe("task")'),
        ("backend/engine.py", 'chat_sources.describe("character")'),
    ):
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            check(f"{rel} is readable", False, str(e))
            continue
        check(f"{rel} labels its proactive messages", needle in text,
              f"expected {needle!r}")

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: bot mirroring — a message from Telegram, Discord or WhatsApp "
          "appears on the chat page, labelled, and exactly once")
    return 0

if __name__ == "__main__":
    sys.exit(main())
