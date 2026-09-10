"""
Playwright browser automation — real web browsing for Addled.

Replaces the 8 browser WS handler stubs with real Playwright integration.
Install: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("addled.browser")


@dataclass
class BrowserState:
    url: str = ""
    title: str = ""
    screenshot_b64: str = ""
    history: list[str] = field(default_factory=list)
    history_index: int = -1


class PlaywrightBrowser:
    """Manages a Chromium browser instance via Playwright."""

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._state = BrowserState()
        # Lightweight HTTP fallback (used when Playwright isn't installed)
        self._http_text = ""
        self._http_title = ""
        self._using_http = False
        # CDP attach to the USER'S browser (opt-in, read-only by default)
        self._cdp = None

    async def _ensure_browser(self) -> bool:
        """Lazy-init Playwright browser. Returns True if ready."""
        if self._page and not self._page.is_closed():
            return True
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            log.info("Playwright browser launched")
            return True
        except ImportError:
            log.info("Playwright not installed — using HTTP fallback for navigation")
            return False
        except Exception as e:
            log.error("Browser launch failed: %s", e)
            return False

    # ---- CDP (user's own browser) -------------------------------------------

    async def _ensure_cdp(self) -> bool:
        """Attach to the user's running Chrome/Edge when enabled."""
        from backend.config import config
        if not config.get("browser", "user_browser", default=False):
            return False
        if self._cdp is None:
            from backend.browser.cdp_client import CDPClient
            self._cdp = CDPClient(int(config.get("browser", "cdp_port",
                                                  default=9222)))
        if self._cdp.connected:
            return True
        return await self._cdp.connect()

    @property
    def mode(self) -> str:
        if self._cdp is not None and self._cdp.connected:
            return "cdp"
        if self._page and not self._page.is_closed():
            return "playwright"
        if self._using_http:
            return "http"
        return "off"

    def _cdp_guard(self, action: str) -> dict | None:
        """Block mutating actions when the user-browser is read-only."""
        from backend.config import config
        if (config.get("browser", "readonly", default=True)
                and action in ("click", "type", "navigate")):
            return {"success": False,
                    "error": "User-browser is in read-only mode "
                             "(Settings → Browser)"}
        return None

    def _engine_allows(self, name: str) -> bool:
        """True when the configured engine (auto or explicit) permits `name`."""
        from backend.config import config
        engine = config.get("browser", "engine", default="auto")
        return engine in ("auto", name)

    @staticmethod
    def _playwright_available() -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _cdp_port_reachable(port: int) -> bool:
        try:
            from backend.browser.cdp_client import CDPClient
            return bool(CDPClient(port).list_targets())
        except Exception:
            return False

    @staticmethod
    def _looks_open_ended(task: str) -> bool:
        from backend.browser.framework_agent import OPEN_ENDED_HINTS
        t = (task or "").lower()
        return len(t.split()) > 8 or any(k in t for k in OPEN_ENDED_HINTS)

    INTERACTIVE_HINTS = ("fill", "click", "form", "submit", "login",
                         "sign in", "cart", "button", "order", "type into")

    @staticmethod
    def _looks_interactive(task: str) -> bool:
        t = (task or "").lower()
        return any(k in t for k in PlaywrightBrowser.INTERACTIVE_HINTS)

    @staticmethod
    def _maybe_trigger_install(backend: str) -> str:
        try:
            from backend.browser.auto_install import maybe_trigger
            return maybe_trigger(backend)
        except Exception:
            return "off"

    async def status(self) -> dict:
        """Health snapshot for the dashboard + router."""
        from backend.config import config
        from backend.browser import framework_agent
        from backend.browser import auto_install
        return {
            "mode": self.mode,
            "engine": config.get("browser", "engine", default="auto"),
            "task_mode": config.get("browser", "task_mode", default="auto"),
            "auto_install": config.get("browser", "auto_install",
                                      default="ask"),
            "playwright_available": self._playwright_available(),
            "cdp_available": self._cdp_port_reachable(
                int(config.get("browser", "cdp_port", default=9222))),
            "framework_available": framework_agent.available(),
            "llm_available": framework_agent.llm_available(),
            "installing_playwright": auto_install.installing("playwright"),
            "installing_framework": auto_install.installing("framework"),
        }

    async def _fetch_http(self, url: str) -> dict:
        """Fetch a page with stdlib urllib and reduce it to readable text.
        Used when Playwright is unavailable — covers navigate/extract for
        most agent needs (search results, articles, docs)."""
        import html as _html
        import re
        import urllib.request

        def _fetch() -> str:
            req = urllib.request.Request(url, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/125.0.0.0 Safari/537.36"),
                "Accept": ("text/html,application/xhtml+xml,"
                           "application/xml;q=0.9,image/avif,image/webp,"
                           "*/*;q=0.8"),
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                return body.decode("utf-8", errors="ignore")

        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _fetch)
        title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        title = _html.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip() if title_m else url
        # Crude but effective HTML→text: drop scripts/styles, then tags
        page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", page)
        text = _html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        self._http_text = text.strip()[:50000]
        self._http_title = title
        self._using_http = True
        self._state.url = url
        self._state.title = title
        self._state.history.append(url)
        self._state.history_index = len(self._state.history) - 1
        return {"success": True, "url": url, "title": title, "engine": "http"}

    async def navigate(self, url: str) -> dict:
        """Navigate to a URL. Returns page info.

        Falls back from Playwright → plain HTTP fetch, so navigation works
        even without a browser installed. Bot-guarded sites that refuse
        automated access come back with a hint to use web_fetch/web_search.
        """
        if not url:
            return {"success": False, "error": "No URL provided"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        from backend.config import config
        engine = config.get("browser", "engine", default="auto")

        # 1) User's own browser via CDP (opt-in)
        if engine in ("auto", "cdp") and await self._ensure_cdp():
            err = self._cdp_guard("navigate")
            if err:
                return err
            try:
                await self._cdp.navigate(url)
                self._state.url = await self._cdp.url()
                self._state.title = await self._cdp.title()
                self._state.history.append(self._state.url)
                self._state.history_index = len(self._state.history) - 1
                shot = await self._cdp.screenshot_b64()
                self._state.screenshot_b64 = shot
                return {"success": True, "url": self._state.url,
                        "title": self._state.title, "screenshot": shot,
                        "engine": "cdp"}
            except Exception as e:
                return {"success": False, "error": f"CDP navigate failed: {e}"}

        # 2) Own Playwright Chromium
        if engine in ("auto", "playwright") and await self._ensure_browser():
            try:
                await self._page.goto(url, wait_until="domcontentloaded",
                                      timeout=30000)
                self._state.url = self._page.url
                self._state.title = await self._page.title()
                self._state.history.append(self._state.url)
                self._state.history_index = len(self._state.history) - 1

                screenshot = await self._page.screenshot(type="jpeg", quality=70)
                self._state.screenshot_b64 = base64.b64encode(screenshot).decode()

                return {
                    "success": True,
                    "url": self._state.url,
                    "title": self._state.title,
                    "screenshot": self._state.screenshot_b64,
                    "engine": "playwright",
                }
            except Exception as e:
                if engine == "playwright":
                    return {"success": False,
                            "error": f"Playwright navigate failed: {e}"}
                log.warning("Playwright navigate failed (%s) — falling back to HTTP", e)

        # Explicit engine unavailable → clear error, no silent fallback
        if engine in ("cdp", "playwright"):
            return {"success": False,
                    "error": f"Browser engine '{engine}' is not available"}

        # 3) HTTP fallback (always available)
        log.info("Using lightweight HTTP fetch")
        try:
            return await self._fetch_http(url)
        except Exception as e:
            hint = ("Automated access may be blocked. Try the web_fetch "
                    "or web_search tool instead.")
            return {"success": False, "error": f"{e} — {hint}"}

    async def go_back(self) -> dict:
        """Go back in browser history."""
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            await self._page.go_back()
            self._state.url = self._page.url
            self._state.title = await self._page.title()
            screenshot = await self._page.screenshot(type="jpeg", quality=70)
            self._state.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": True, "url": self._state.url, "title": self._state.title,
                    "screenshot": self._state.screenshot_b64}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def go_forward(self) -> dict:
        """Go forward in browser history."""
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            await self._page.go_forward()
            self._state.url = self._page.url
            self._state.title = await self._page.title()
            screenshot = await self._page.screenshot(type="jpeg", quality=70)
            self._state.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": True, "url": self._state.url, "title": self._state.title,
                    "screenshot": self._state.screenshot_b64}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def click(self, selector: str | None = None, x: int | None = None,
                    y: int | None = None) -> dict:
        """Click on an element by CSS selector or coordinates."""
        if self._engine_allows("cdp") and await self._ensure_cdp():
            err = self._cdp_guard("click")
            if err:
                return err
            try:
                if selector:
                    await self._cdp.click_selector(selector)
                elif x is not None and y is not None:
                    await self._cdp.click_xy(int(x), int(y))
                else:
                    return {"success": False,
                            "error": "Provide selector or x,y coordinates"}
                await asyncio.sleep(0.5)
                shot = await self._cdp.screenshot_b64()
                self._state.screenshot_b64 = shot
                return {"success": True, "url": await self._cdp.url(),
                        "screenshot": shot, "engine": "cdp"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            if selector:
                await self._page.click(selector, timeout=10000)
            elif x is not None and y is not None:
                await self._page.mouse.click(x, y)
            else:
                return {"success": False, "error": "Provide selector or x,y coordinates"}

            await asyncio.sleep(0.5)
            screenshot = await self._page.screenshot(type="jpeg", quality=70)
            self._state.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": True, "url": self._page.url,
                    "screenshot": self._state.screenshot_b64}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def type_text(self, selector: str, text: str) -> dict:
        """Type text into an input element."""
        if self._engine_allows("cdp") and await self._ensure_cdp():
            err = self._cdp_guard("type")
            if err:
                return err
            try:
                await self._cdp.type_text(selector, str(text))
                shot = await self._cdp.screenshot_b64()
                self._state.screenshot_b64 = shot
                return {"success": True, "screenshot": shot, "engine": "cdp"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            await self._page.fill(selector, text, timeout=10000)
            screenshot = await self._page.screenshot(type="jpeg", quality=70)
            self._state.screenshot_b64 = base64.b64encode(screenshot).decode()
            return {"success": True, "screenshot": self._state.screenshot_b64}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def screenshot(self, full_page: bool = False) -> dict:
        """Take a screenshot of the current page."""
        if self._engine_allows("cdp") and await self._ensure_cdp():
            try:
                shot = await self._cdp.screenshot_b64()
                self._state.screenshot_b64 = shot
                return {"success": True, "url": await self._cdp.url(),
                        "title": await self._cdp.title(),
                        "screenshot": shot, "engine": "cdp"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            img = await self._page.screenshot(type="jpeg", quality=80, full_page=full_page)
            self._state.screenshot_b64 = base64.b64encode(img).decode()
            return {"success": True, "url": self._page.url, "title": await self._page.title(),
                    "screenshot": self._state.screenshot_b64}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def extract(self, selector: str | None = None) -> dict:
        """Extract page content: text, all text, or specific element."""
        if self._engine_allows("cdp") and await self._ensure_cdp():
            try:
                if selector:
                    text = await self._cdp.evaluate(
                        "(() => { const el = document.querySelector(%s); "
                        "return el ? el.innerText : ''; })()"
                        % json.dumps(selector))
                else:
                    text = await self._cdp.text()
                return {"success": True, "url": await self._cdp.url(),
                        "title": await self._cdp.title(),
                        "text": str(text or "")[:10000], "engine": "cdp"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        if self._page is None and self._using_http:
            # HTTP fallback: return the page text captured at navigate time
            return {"success": True, "url": self._state.url,
                    "title": self._http_title,
                    "text": self._http_text[:10000], "engine": "http"}
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            if selector:
                text = await self._page.text_content(selector, timeout=5000)
            else:
                text = await self._page.inner_text("body")
            title = await self._page.title()
            return {"success": True, "url": self._page.url, "title": title,
                    "text": text[:10000] if text else ""}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self) -> dict:
        """Close the browser (own instance or CDP connection — never the
        user's browser itself, just the debug attachment)."""
        try:
            if self._cdp is not None:
                await self._cdp.close()
                self._cdp = None
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            self._browser = None
            self._context = None
            self._page = None
            self._playwright = None
            self._state = BrowserState()
            self._http_text = ""
            self._http_title = ""
            self._using_http = False
            log.info("Browser closed")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_state(self) -> dict:
        """Get current browser state."""
        return {
            "url": self._state.url,
            "title": self._state.title,
            "mode": self.mode,
            "hasScreenshot": bool(self._state.screenshot_b64),
            "screenshot": self._state.screenshot_b64[:200] + "..." if self._state.screenshot_b64 else "",
            "historyLength": len(self._state.history),
        }

    # ---- Task routing (conditional framework use) ---------------------------

    async def _run_deterministic(self, task: str, engine: str) -> dict:
        """Best-effort deterministic handling: search-results page via the
        chosen backend. engine: auto | cdp | playwright | http."""
        import urllib.parse
        q = urllib.parse.quote((task or "")[:200])
        url = f"https://www.bing.com/search?q={q}"
        if engine == "http":
            try:
                res = await self._fetch_http(url)
                res["engine"] = "http"
                return res
            except Exception as e:
                return {"success": False, "engine": "http", "error": str(e)}
        if engine == "cdp":
            if not await self._ensure_cdp():
                return {"success": False, "engine": "cdp",
                        "error": "CDP not available — launch the browser "
                                 "with --remote-debugging-port"}
            try:
                await self._cdp.navigate(url)
                return {"success": True, "engine": "cdp", "url": url,
                        "title": await self._cdp.title(),
                        "text": (await self._cdp.text())[:10000]}
            except Exception as e:
                return {"success": False, "engine": "cdp", "error": str(e)}
        if engine == "playwright":
            if not await self._ensure_browser():
                return {"success": False, "engine": "playwright",
                        "error": "Playwright not installed"}
            try:
                await self._page.goto(url, wait_until="domcontentloaded",
                                      timeout=30000)
                text = await self._page.inner_text("body")
                return {"success": True, "engine": "playwright", "url": url,
                        "title": await self._page.title(),
                        "text": (text or "")[:10000]}
            except Exception as e:
                return {"success": False, "engine": "playwright",
                        "error": str(e)}
        # auto: best-fit deterministic
        if await self._ensure_cdp():
            return await self._run_deterministic(task, "cdp")
        if self._playwright_available():
            return await self._run_deterministic(task, "playwright")
        # interactive task with no engine at all → offer to auto-install
        if self._looks_interactive(task):
            self._maybe_trigger_install("playwright")
        return await self._run_deterministic(task, "http")

    async def route_task(self, task: str, max_steps: int | None = None) -> dict:
        """Route an open-ended browsing task to the best backend.

        The browser-use framework runs ONLY when installed AND an LLM is
        available AND the task looks open-ended; otherwise the best-fit
        deterministic backend handles it and the result is marked degraded."""
        from backend.config import config
        task = (task or "").strip()
        if not task:
            return {"success": False, "error": "No task provided"}
        engine = config.get("browser", "engine", default="auto")
        task_mode = config.get("browser", "task_mode", default="auto")
        max_steps = int(max_steps or config.get("browser",
                                                "framework_max_steps",
                                                default=10))

        # 1) explicit deterministic engine override
        if engine in ("cdp", "playwright", "http"):
            return await self._run_deterministic(task, engine)

        # 2) framework decision (conditional)
        from backend.browser.framework_agent import available as fw_available
        from backend.browser.framework_agent import llm_available
        use_framework = False
        reason = ""
        if task_mode == "always":
            if fw_available():
                use_framework = True
            else:
                reason = "browser-use not installed"
        elif task_mode == "off":
            reason = "task_mode is off"
        else:  # auto
            if fw_available() and llm_available() and \
                    self._looks_open_ended(task):
                use_framework = True
            elif not llm_available():
                reason = "LLM unavailable"
            elif not fw_available():
                reason = "browser-use not installed"
                # offer to auto-install the framework when an LLM exists
                if llm_available():
                    self._maybe_trigger_install("framework")
            else:
                reason = "task not open-ended"

        if use_framework:
            from backend.browser.framework_agent import run_task
            res = await run_task(task, max_steps=max_steps)
            if res.get("success"):
                return {**res, "engine": "framework", "degraded": False}
            reason = f"framework failed: {res.get('error')}"

        # 3) best-fit deterministic fallback
        result = await self._run_deterministic(task, "auto")
        result["degraded"] = True
        result["fallback_reason"] = reason
        return result


# Singleton
browser = PlaywrightBrowser()
