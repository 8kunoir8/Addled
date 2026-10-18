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
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("addled.browser")

# The Chromium the app drives, configured so a normal site does not refuse it on
# sight. See `_ensure_browser` for why these are limited to the free signals.
#
# `--disable-blink-features=AutomationControlled` is the important one: without
# it Chromium reports `navigator.webdriver === true`, and that single boolean is
# the cheapest way for a site to spot Playwright. It was measured true on the
# live install before this change.
_LAUNCH_OPTIONS: dict = {
    "headless": False,
    "args": [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
    ],
    "viewport": {"width": 1280, "height": 720},
    # A current UA, matching the Chromium Playwright ships rather than an old
    # hard-coded one. A UA that disagrees with the engine is itself a tell.
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "locale": "en-US",
    # The user is in Indonesia and asks about Indonesian marketplaces; a UTC
    # clock against an Indonesian IP is a mismatch the page can read.
    "timezone_id": "Asia/Jakarta",
    "accept_downloads": False,
}

# Applied to every page in the context. Each line removes one of the properties
# a headless-automation check reads first. Kept as one string so the intent is
# reviewable in a glance.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'id'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

# Pages that hydrate after `domcontentloaded` show a loading shell first. This is
# the phrase Tokopedia used while its product grid rendered; matching it lets the
# extractor wait for real content instead of returning the shell.
_LOADING_MARKERS: tuple[str, ...] = (
    "memuat, mohon tunggu",
    "loading, please wait",
    "please wait while",
)

# Extract waits this long for content to appear before giving up on a page.
# Bounded on purpose: `networkidle` is unusable here - Tokopedia holds a
# long-poll open and it never fires, timing out at 45s while the content was
# ready in under three.
_CONTENT_WAIT_TOTAL_S = 10.0
_CONTENT_WAIT_STEP_S = 0.5
# A body shorter than this on a page we were told to read is treated as "not
# loaded yet" rather than "the page is empty", which is what made an unrendered
# marketplace return success with no text.
_MEANINGFUL_TEXT_LEN = 200


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
        """Lazy-init Playwright browser. Returns True if ready.

        A persistent context, not a throwaway one, for two reasons that both
        matter on the marketplaces this was failing on:

        - Cookies and storage survive a restart, so a second visit looks like a
          returning visitor. A fresh profile every launch is itself a signal.
        - Chromium keeps the profile cache, so repeat navigations are faster.

        The anti-detection work is deliberately modest and honest: it removes
        the signals that are *free* to remove (the `webdriver` flag, the absent
        `chrome` object, the wrong UA/locale) and does nothing cleverer. A site
        determined to fingerprint the browser will still detect it - and when
        that happens the caller is told, rather than handed an empty page that
        looks like a successful one.
        """
        if self._page and not self._page.is_closed():
            return True
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()

            profile_dir = self._profile_dir()
            self._context = await self._launch_with_profile(profile_dir)
            await self._context.add_init_script(_STEALTH_INIT_SCRIPT)
            self._page = (self._context.pages[0] if self._context.pages
                          else await self._context.new_page())
            log.info("Playwright browser launched (profile: %s)", profile_dir)
            return True
        except ImportError:
            log.info("Playwright not installed — using HTTP fallback for navigation")
            return False
        except Exception as e:
            log.error("Browser launch failed: %s", e)
            return False

    def _profile_dir(self) -> str:
        """The profile directory to launch against.

        Prefers this install's persistent profile; falls back to a temp one when
        it is already held. The fallback is what keeps a second concurrent browse
        - or a launch racing a previous shutdown - from failing outright.
        """
        from backend import app_paths
        preferred = app_paths.BROWSER_PROFILE_DIR
        if app_paths.profile_is_locked(preferred):
            log.info("Browser profile %s is in use — using a fresh one",
                     preferred)
            return str(app_paths._default_profile_dir())
        try:
            preferred.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("Cannot create profile dir %s (%s) — using a temp one",
                        preferred, e)
            return str(app_paths._default_profile_dir())
        return str(preferred)

    async def _launch_with_profile(self, profile_dir: str):
        """Launch a persistent context, retrying once on a busy profile.

        The lock can appear between the check in `_profile_dir` and the launch
        itself, so the first failure on a lock is retried against a temp
        profile rather than being reported as a browser that will not start.
        """
        from backend import app_paths
        try:
            return await self._playwright.chromium.launch_persistent_context(
                profile_dir, **_LAUNCH_OPTIONS)
        except Exception as e:
            if "singleton lock" not in str(e).lower():
                raise
            fallback = str(app_paths._default_profile_dir())
            if fallback == profile_dir:
                raise
            log.warning("Profile lock at launch (%s) — retrying on %s",
                        e, fallback)
            return await self._playwright.chromium.launch_persistent_context(
                fallback, **_LAUNCH_OPTIONS)


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
    def _playwright_importable() -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _browsers_path() -> str:
        """Where Playwright keeps its browser builds.

        The environment variable wins when set — that is what Playwright itself
        reads, and an operator override must be honoured. Otherwise Addled's own
        directory is used (`app_paths.BROWSERS_DIR`, under the data dir), which
        is where `configure_browsers_path()` points Playwright at startup. The
        fallback goes there rather than to Playwright's machine-wide default so
        a script or a check that never ran `main.py` looks in the same place the
        app does, and does not report a browser the app cannot see.
        """
        override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if override:
            return override
        try:
            from backend import app_paths
            return str(app_paths.BROWSERS_DIR)
        except Exception:
            if os.name == "nt":
                base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
                return os.path.join(base, "ms-playwright")
            return os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")

    @staticmethod
    def _chromium_available() -> bool:
        """Whether the Chromium build THIS Playwright wants is on disk.

        Deliberately local: it reads the installed package's own
        `browsers.json` for the revision it expects, then looks for the real
        executable. No driver is started — this is called from the status RPC
        and the auto-install gate, and spawning a Playwright driver per poll
        would be both slow and noisy.

        The revision is the point. Only looking for a `chromium-*` folder
        passes on an install where a *different* Playwright release downloaded
        its own build — which is exactly the state that made every launch fail
        while the dashboard showed a green "installed".
        """
        try:
            import playwright
            pkg = os.path.dirname(playwright.__file__)
            with open(os.path.join(pkg, "driver", "package", "browsers.json"),
                      encoding="utf-8") as fh:
                browsers = json.load(fh).get("browsers", [])
        except Exception:
            return False
        base = PlaywrightBrowser._browsers_path()
        win = os.name == "nt"
        # (browsers.json name, relative executable path, macOS .app wrapper)
        wanted = (
            ("chromium", ("chrome-win64/chrome.exe" if win
                          else "chrome-linux/chrome"),
             "chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
            ("chromium-headless-shell",
             "chrome-headless-shell-win64/chrome-headless-shell.exe",
             "chrome-headless-shell-mac/chrome-headless-shell"),
        )
        for name, rel, mac_rel in wanted:
            rev = next((b.get("revision") for b in browsers
                        if b.get("name") == name), None)
            if not rev:
                continue
            leaf = mac_rel if sys.platform == "darwin" else rel
            exe = os.path.join(base, f"{name}-{rev}", *leaf.split("/"))
            if os.path.exists(exe):
                return True
        return False

    @staticmethod
    def _playwright_available() -> bool:
        """Whether Playwright can actually launch a browser.

        Two things must be true and checking only the first is the bug this
        replaces: the `playwright` package has to import AND its Chromium
        build has to be on disk. pip installs the package separately from the
        browser it drives, so `import playwright` succeeds on an install where
        the ~150 MB download never happened or was later pruned — and the
        Settings page then draws a green "installed" for a browser that cannot
        start. The revision matters too: a Chromium fetched by a *different*
        Playwright release is the wrong revision and does not count.
        """
        if not PlaywrightBrowser._playwright_importable():
            return False
        return PlaywrightBrowser._chromium_available()

    @staticmethod
    def _playwright_unavailable_reason() -> str:
        """Why Playwright cannot be used, in words a user can act on.

        The two failure modes need different fixes, so they must not share a
        message: a missing package is installed, a missing *browser* is
        downloaded. Saying only "not available" for both leaves the user with
        no idea which.
        """
        if not PlaywrightBrowser._playwright_importable():
            return "playwright package is not installed"
        if not PlaywrightBrowser._chromium_available():
            return "the Chromium browser is not downloaded"
        return "browser engine not available"

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

    async def navigate(self, url: str, *, explicit: bool = True) -> dict:
        """Navigate to a URL. Returns page info.

        `explicit` says who asked. A browse the user requested (the
        `browser_navigate` tool, the dashboard's browser page) wants the real
        browser and gets it. A caller whose job is to GET THE CONTENT —
        `web_fetch`, the forge's research step — must not raise a window, so it
        passes `explicit=False` and the cheap HTTP path is tried first.

        The order used to be the other way round for everyone: Playwright was
        attempted before the plain fetch, so simply fetching an ordinary page
        opened Chromium. Measured 2026-10-17: a plain `web_search` did it. The
        cheap path is tried first now and the browser is kept for when the
        fetch genuinely cannot serve the page.

        Bot-guarded sites that refuse automated access come back with a hint to
        use web_fetch/web_search.
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
        #
        # `degrade_reason` records WHY a real browser was not used, so the
        # caller can say so instead of presenting a plain fetch as a browse.
        # That difference is invisible in the payload otherwise: both paths
        # return success=True, and a fetch has no screenshot, no JS, no clicks.
        degrade_reason = ""

        # A browser this call started, and should therefore close again. One
        # the caller (or an earlier turn) already had open is left alone: the
        # user's browsing session is not ours to end. Without this, a single
        # fallback left Chromium resident for the rest of the day - `close()` is
        # only ever reached from the `browser.close` RPC, so nothing collected
        # it.
        started_here = not (self._page and not self._page.is_closed())

        # An implicit fetch tries the cheap path first and only escalates to a
        # browser when the page cannot be read without one.
        probe: dict | None = None
        if not explicit:
            try:
                probe = await self._fetch_http(url)
                text = (probe.get("text") or self._http_text or "").strip()
                if probe.get("success") and self._looks_loaded(text):
                    return probe
                degrade_reason = "the plain fetch returned no usable text"
                log.info("HTTP fetch yielded no usable text — escalating to a "
                         "browser for %s", url)
            except Exception as e:
                degrade_reason = f"the plain fetch failed: {e}"
                log.info("HTTP fetch failed (%s) — escalating to a browser "
                         "for %s", e, url)

        if engine in ("auto", "playwright"):
            if await self._ensure_browser():
                try:
                    await self._page.goto(url, wait_until="domcontentloaded",
                                          timeout=30000)
                    self._state.url = self._page.url
                    self._state.title = await self._page.title()
                    self._state.history.append(self._state.url)
                    self._state.history_index = len(self._state.history) - 1

                    screenshot = await self._page.screenshot(
                        type="jpeg", quality=70)
                    self._state.screenshot_b64 = base64.b64encode(
                        screenshot).decode()

                    result = {
                        "success": True,
                        "url": self._state.url,
                        "title": self._state.title,
                        "screenshot": self._state.screenshot_b64,
                        "engine": "playwright",
                    }
                    # The navigation worked, but a bot check or a still-hydrating
                    # page can leave a body with nothing readable in it. Saying so
                    # here means the model knows before it calls extract, instead
                    # of reading an empty result as "this page is empty".
                    if not self._looks_loaded(await self._safe_body_text()):
                        result["content_warning"] = (
                            "The page loaded but no readable text was found — it "
                            "may still be rendering, or the site may be blocking "
                            "automated access. Verify before relying on it, or "
                            "try web_search / web_fetch."
                        )
                    if not explicit and started_here:
                        # This call raised the window; it takes it down. The
                        # text is already captured, so nothing is lost.
                        await self._release_implicit_browser()
                    return result
                except Exception as e:
                    if not explicit and started_here:
                        await self._release_implicit_browser()
                    if engine == "playwright":
                        return {"success": False,
                                "error": f"Playwright navigate failed: {e}"}
                    degrade_reason = f"Playwright navigate failed: {e}"
                    log.warning("Playwright navigate failed (%s) — falling "
                                "back to HTTP", e)
            elif engine == "playwright":
                return {"success": False,
                        "error": "Browser engine 'playwright' is not available"}
            else:
                degrade_reason = self._playwright_unavailable_reason()
                log.warning("Playwright unavailable (%s) — using HTTP fetch",
                            degrade_reason)

        # Explicit engine unavailable → clear error, no silent fallback
        if engine == "cdp":
            return {"success": False,
                    "error": "Browser engine 'cdp' is not available"}

        # 3) HTTP fallback (always available)
        #
        # Marked degraded: this is a plain fetch standing in for a browser, and
        # a caller that treats the two as the same shows the user a result with
        # no screenshot and no interactivity while claiming to have browsed.
        log.info("Using lightweight HTTP fetch")
        try:
            # The implicit path already fetched this URL to decide whether a
            # browser was needed at all; reuse that answer rather than hitting
            # the same server twice.
            result = probe if probe is not None else await self._fetch_http(url)
            result["degraded"] = True
            result["fallback_reason"] = (
                degrade_reason or "browser engine not available")
            self._maybe_trigger_install("playwright")
            return result
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

    async def _await_page_text(self, selector: str | None = None) -> str:
        """Read the page text, waiting briefly for it to actually render.

        Pages that hydrate after `domcontentloaded` return a loading shell or
        nothing at all if read immediately - Tokopedia answered 141 characters
        of chrome and the phrase "Memuat, mohon tunggu..." before its product
        grid existed. Reading that as the page content is what made an
        unrendered marketplace look like an empty one.

        Polls instead of using `wait_until="networkidle"`, which is unusable on
        these sites: they hold a long-poll connection open, so idle never
        arrives and the navigation times out at 45s while the content was ready
        in under three.

        Returns the text (possibly empty); callers decide what empty means.
        """
        deadline = _CONTENT_WAIT_TOTAL_S
        waited = 0.0
        text = ""
        while True:
            try:
                if selector:
                    # `inner_text`, not `text_content`: the latter also returns
                    # hidden and script text, so a page with zero visible words
                    # can still measure tens of thousands of characters.
                    text = await self._page.inner_text(selector, timeout=5000)
                else:
                    text = await self._page.inner_text("body")
            except Exception:
                text = ""
            if self._looks_loaded(text) or waited >= deadline:
                return text or ""
            await asyncio.sleep(_CONTENT_WAIT_STEP_S)
            waited += _CONTENT_WAIT_STEP_S

    @staticmethod
    def _looks_loaded(text: str) -> bool:
        """Whether this text is real content rather than a loading shell.

        Long enough to be a page, and not one of the known "waiting" phrases.
        Conservative on purpose: a false "loaded" stops the wait early on a page
        that would have rendered, and that is the bug being fixed here.
        """
        stripped = (text or "").strip()
        if len(stripped) < _MEANINGFUL_TEXT_LEN:
            return False
        low = stripped.lower()
        return not any(marker in low for marker in _LOADING_MARKERS)

    async def _safe_body_text(self) -> str:
        """The visible body text, or "" if the page cannot be read.

        Used only to decide whether a navigation produced readable content; a
        page mid-navigation can throw here, and that is not an error worth
        surfacing on its own.
        """
        if not self._page:
            return ""
        try:
            return await self._page.inner_text("body")
        except Exception:
            return ""

    async def extract(self, selector: str | None = None) -> dict:
        """Extract page content: text, all text, or specific element.

        Empty output is reported as a FAILURE, not as a successful empty page.
        That distinction is the whole point: a marketplace that served a
        bot-check shell used to come back `success: True, text: ""`, the model
        could not tell that apart from a genuinely blank page, and it answered
        from nothing. Saying "this did not work, here is why" is what lets the
        model try another route or tell the user the truth.
        """
        if self._engine_allows("cdp") and await self._ensure_cdp():
            try:
                if selector:
                    text = await self._cdp.evaluate(
                        "(() => { const el = document.querySelector(%s); "
                        "return el ? el.innerText : ''; })()"
                        % json.dumps(selector))
                else:
                    text = await self._cdp.text()
                text = str(text or "")
                if not text.strip():
                    return self._empty_extract("cdp", selector)
                return {"success": True, "url": await self._cdp.url(),
                        "title": await self._cdp.title(),
                        "text": text[:10000], "engine": "cdp"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        if self._page is None and self._using_http:
            # HTTP fallback: return the page text captured at navigate time
            text = self._http_text or ""
            if not text.strip():
                return self._empty_extract("http", selector)
            return {"success": True, "url": self._state.url,
                    "title": self._http_title,
                    "text": text[:10000], "engine": "http"}
        if not self._page:
            return {"success": False, "error": "No page loaded"}
        try:
            text = await self._await_page_text(selector)
            title = await self._page.title()
            if not text.strip():
                return self._empty_extract("playwright", selector)
            return {"success": True, "url": self._page.url, "title": title,
                    "text": text[:10000], "engine": "playwright"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _empty_extract(self, engine: str, selector: str | None) -> dict:
        """The result when a page yielded no text.

        Names the likely cause rather than guessing at one: a page that loads but
        has no text is almost always either still rendering or refusing an
        automated client, and both are actionable if the model can see them.
        """
        where = f" for selector {selector!r}" if selector else ""
        return {
            "success": False,
            "engine": engine,
            "url": self._state.url or (self._page.url if self._page else ""),
            "error": (
                f"No text was extracted{where}. The page loaded but has no "
                "readable content — it may still be rendering, or it may be "
                "blocking automated access with a bot check. Try web_search or "
                "web_fetch for this URL instead."
            ),
        }


    async def _release_implicit_browser(self) -> None:
        """Close a browser that no one asked for. Never raises.

        Only ever called for a navigation the user did not request, and only
        when this call is what started the browser. A CDP attachment is left
        alone - that is the user's own browser and closing the connection would
        not close it anyway.
        """
        if self._cdp is not None:
            return
        if not (self._page and not self._page.is_closed()):
            return
        try:
            await self.close()
            log.info("Closed the browser this call started")
        except Exception as e:  # noqa: BLE001
            log.debug("could not close the implicitly started browser: %s", e)

    async def close(self) -> dict:
        """Close the browser (own instance or CDP connection — never the
        user's browser itself, just the debug attachment)."""
        try:
            if self._cdp is not None:
                await self._cdp.close()
                self._cdp = None
            # A persistent context owns its own browser, so there is no separate
            # `_browser` to close - closing the context closes Chromium and, with
            # it, releases the profile lock. Skipping this left the lock file
            # behind and the next launch had to fall back to a temp profile.
            if self._context:
                await self._context.close()
            elif self._browser:
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
