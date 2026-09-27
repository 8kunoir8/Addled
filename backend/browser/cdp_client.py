"""
Chrome DevTools Protocol client — attaches to the USER'S running
Chrome/Edge via the bundled `websockets` library (no Playwright needed).

Requires the browser to be started with --remote-debugging-port=<port>.
Read-only safe by default: the caller (browser_engine) enforces the
config-level read-only gate before using mutating methods.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

log = logging.getLogger("addled.cdp")


class CDPClient:
    """Minimal CDP client over websockets."""

    def __init__(self, port: int = 9222):
        self.port = int(port)
        self._ws = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        # Strong reference to the reader task — see `_connect`.
        self._reader_task = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    # ---- transport ---------------------------------------------------------

    def _http_json(self, path: str):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"User-Agent": "Addled/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def list_targets(self) -> list[dict]:
        try:
            return [t for t in self._http_json("/json")
                    if t.get("type") == "page"]
        except Exception:
            return []

    async def connect(self) -> bool:
        """Attach to the most recently active page tab."""
        targets = self.list_targets()
        if not targets:
            return False
        try:
            import websockets
            ws = await websockets.connect(
                targets[0]["webSocketDebuggerUrl"],
                max_size=64 * 1024 * 1024)
            self._ws = ws
            self._pending = {}
            # The reader is stored, not fire-and-forget. `asyncio` keeps only a
            # WEAK reference to a running task, so `create_task(...)` with the
            # result discarded can be garbage-collected mid-flight — and a dead
            # reader means every `call()` hangs until its timeout, with nothing
            # in the log to say why.
            self._reader_task = asyncio.get_running_loop().create_task(
                self._reader())
            log.info("CDP attached to %s", targets[0].get("url", "?"))
            return True
        except Exception as e:
            log.info("CDP connect failed: %s", e)
            self._ws = None
            return False

    async def _reader(self):
        # Snapshot the socket: `async for self._ws` re-reads the attribute on
        # every iteration, so a concurrent `close()` (which sets it to None)
        # made the loop raise "NoneType is not async iterable" — swallowed by
        # the bare except below, so the reader died silently and looked exactly
        # like "the browser stopped responding".
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                rid = msg.get("id")
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(
                            msg["error"].get("message", "cdp error")))
                    else:
                        fut.set_result(msg.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Logged, not swallowed: a protocol error and a closed socket look
            # identical from the outside otherwise.
            log.debug("CDP reader stopped: %s", e)

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = 30) -> dict:
        if not self.connected:
            raise RuntimeError("CDP not connected")
        self._req_id += 1
        rid = self._req_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send(json.dumps(
            {"id": rid, "method": method, "params": params or {}}))
        try:
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Only `_reader` ever removed a pending entry, and it only does so
            # when the reply ARRIVES. On a timeout or a cancel nothing popped
            # `rid`, so every hung call leaked a future for the life of the
            # attachment and the late reply then logged "exception was never
            # retrieved". Two consequences, both fixed by dropping it here.
            self._pending.pop(rid, None)
            raise

    async def evaluate(self, expression: str):
        res = await self.call("Runtime.evaluate",
                              {"expression": expression, "returnByValue": True})
        return res.get("result", {}).get("value")

    # ---- page operations ----------------------------------------------------

    async def navigate(self, url: str) -> None:
        await self.call("Page.enable")
        await self.call("Page.navigate", {"url": url})
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                if await self.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                break

    async def url(self) -> str:
        return str(await self.evaluate("location.href") or "")

    async def title(self) -> str:
        return str(await self.evaluate("document.title") or "")

    async def text(self) -> str:
        try:
            t = await self.evaluate(
                "document.body ? document.body.innerText : ''")
            return str(t or "")
        except Exception:
            return ""

    async def screenshot_b64(self, quality: int = 70) -> str:
        res = await self.call("Page.captureScreenshot",
                              {"format": "jpeg", "quality": quality})
        return res.get("data", "")

    async def go_back(self) -> None:
        await self.evaluate("history.back()")
        await asyncio.sleep(0.5)

    async def go_forward(self) -> None:
        await self.evaluate("history.forward()")
        await asyncio.sleep(0.5)

    async def click_xy(self, x: int, y: int) -> None:
        for kwargs in ({"type": "mousePressed", "x": int(x), "y": int(y),
                        "button": "left", "clickCount": 1},
                       {"type": "mouseReleased", "x": int(x), "y": int(y),
                        "button": "left", "clickCount": 1}):
            await self.call("Input.dispatchMouseEvent", kwargs)

    async def click_selector(self, selector: str) -> None:
        js = ("(() => { const el = document.querySelector(%s); "
              "if (!el) return null; const r = el.getBoundingClientRect(); "
              "return {x: r.x + r.width/2, y: r.y + r.height/2}; })()"
              % json.dumps(selector))
        rect = await self.evaluate(js)
        if not rect:
            raise RuntimeError(f"element not found: {selector}")
        await self.click_xy(int(rect["x"]), int(rect["y"]))

    async def type_text(self, selector: str, text: str) -> None:
        js = ("(() => { const el = document.querySelector(%s); "
              "if (!el) return false; el.focus(); return true; })()"
              % json.dumps(selector))
        if not await self.evaluate(js):
            raise RuntimeError(f"element not found: {selector}")
        await self.call("Input.insertText", {"text": str(text)})

    async def close(self) -> None:
        # Cancel the reader before dropping the socket, or it wakes up on a
        # closed connection and exits through the exception path.
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._pending:
            # Any call still waiting will never get a reply now; failing them
            # here gives the caller a real error instead of a 30s timeout.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("CDP connection closed"))
            self._pending.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
