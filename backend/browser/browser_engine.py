"""
Playwright browser automation — real web browsing for Addled.

Replaces the 8 browser WS handler stubs with real Playwright integration.
Install: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
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
            log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
            return False
        except Exception as e:
            log.error("Browser launch failed: %s", e)
            return False

    async def navigate(self, url: str) -> dict:
        """Navigate to a URL. Returns page info."""
        if not await self._ensure_browser():
            return {"success": False, "error": "Playwright not available"}
        try:
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

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
        """Close the browser."""
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            self._browser = None
            self._context = None
            self._page = None
            self._playwright = None
            self._state = BrowserState()
            log.info("Browser closed")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_state(self) -> dict:
        """Get current browser state."""
        return {
            "url": self._state.url,
            "title": self._state.title,
            "hasScreenshot": bool(self._state.screenshot_b64),
            "screenshot": self._state.screenshot_b64[:200] + "..." if self._state.screenshot_b64 else "",
            "historyLength": len(self._state.history),
        }


# Singleton
browser = PlaywrightBrowser()
