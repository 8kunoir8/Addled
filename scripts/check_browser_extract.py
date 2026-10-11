"""Browser extraction checks — honest failure, and the stealth launch.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_browser_extract.py

These pin a live defect, not a hypothetical one. On 2026-10-11 a marketplace
search was browsed and the extract returned `success: True, text: ""`; the model
could not tell that apart from a genuinely empty page, so it answered from
nothing and told the user the tool "went astray". The navigation had loaded a
page, the content had simply not rendered (or had been replaced by a bot check),
and nothing said so.

So the rule this file protects: an extraction that produced no text is a
FAILURE, with a reason the caller can act on. Alongside it, the launch settings
that stop a site rejecting the browser outright - the `webdriver` flag above all,
which read `true` on the live install.

No browser is downloaded here; the live-behaviour checks are skipped when
Chromium is absent so this suite still runs on a machine without it.
"""

import asyncio
import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.browser import browser_engine

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# 1. The wait/loading decision, which needs no browser.
# ---------------------------------------------------------------------------

def run_static():
    L = browser_engine.PlaywrightBrowser._looks_loaded

    check("an empty body is not 'loaded'", not L(""),
          "an empty page must not stop the content wait")
    check("a whitespace body is not 'loaded'", not L("   \n\t "),
          "whitespace is not content")
    check("a short body is not 'loaded'", not L("hello"),
          "a fragment is a loading state, not a page")
    check("a Tokopedia loading shell is not 'loaded'",
          not L("Memuat, mohon tunggu" * 20),
          "the exact phrase the live page showed while rendering")
    check("an English loading shell is not 'loaded'",
          not L("Loading, please wait..." * 20), "")
    check("a real page body is 'loaded'", L("x" * 400),
          "content above the threshold must stop the wait")

    # The reason shown when a page yields nothing must name the actionable
    # causes, because that string is what the model reads.
    b = browser_engine.PlaywrightBrowser()
    r = b._empty_extract("playwright", "body")
    check("an empty extract reports failure", r.get("success") is False,
          "empty text as success is the bug this file exists for")
    check("an empty extract names a reason", bool(r.get("error")), str(r))
    err = (r.get("error") or "").lower()
    check("the reason mentions blocking or rendering",
          "block" in err or "render" in err,
          "the caller needs to know why, not just that it failed")
    check("the reason points at another tool",
          "web_search" in err or "web_fetch" in err,
          "a dead end is not actionable")


# ---------------------------------------------------------------------------
# 2. The launch configuration: the free signals, removed.
# ---------------------------------------------------------------------------

def run_launch_config():
    opts = browser_engine._LAUNCH_OPTIONS
    args = " ".join(opts.get("args", []))
    check("the automation flag is disabled",
          "AutomationControlled" in args,
          "without this Chromium reports navigator.webdriver === true")
    check("the browser is not headless",
          opts.get("headless") is False,
          "a headless Chromium is a first-order automation signal")
    check("a locale is set", bool(opts.get("locale")), str(opts.get("locale")))
    check("a timezone is set", bool(opts.get("timezone_id")),
          "a UTC clock against an Indonesian user is a mismatch")
    check("a user agent is set", bool(opts.get("user_agent")), "")

    ua = opts.get("user_agent", "")
    check("the user agent is a current Chrome",
          bool(re.search(r"Chrome/(1[2-9]\d|9\d)\.", ua)),
          f"an outdated UA is itself a tell: {ua!r}")

    script = browser_engine._STEALTH_INIT_SCRIPT
    check("the init script clears navigator.webdriver", "webdriver" in script,
          "the flag alone is not always enough; the property is overwritten too")
    check("the init script supplies window.chrome", "chrome" in script,
          "an absent chrome object is a headless tell")

    from backend import app_paths
    check("the profile lives under the app data dir",
          str(app_paths.BROWSER_PROFILE_DIR).startswith(str(app_paths.DATA_DIR)),
          str(app_paths.BROWSER_PROFILE_DIR))
    check("a profile lock is detected by presence",
          app_paths.profile_is_locked(app_paths.BROWSER_PROFILE_DIR)
          in (True, False),
          "profile_is_locked must be callable on any path")


# ---------------------------------------------------------------------------
# 3. Live behaviour, when a browser is actually available.
# ---------------------------------------------------------------------------

async def run_live() -> int:
    from backend.browser.browser_engine import PlaywrightBrowser
    if not PlaywrightBrowser._playwright_available():
        return -1

    b = PlaywrightBrowser()
    ran = 0
    try:
        nav = await b.navigate("https://example.com")
        if nav.get("engine") != "playwright":
            return ran  # no network / blocked; nothing to assert
        check("a normal navigation uses the browser", nav.get("success") is True,
              str(nav))
        ex = await b.extract("body")
        check("a normal page extracts text", ex.get("success") is True,
              str(ex)[:120])
        check("the extracted text is non-trivial",
              len(ex.get("text", "")) > browser_engine._MEANINGFUL_TEXT_LEN,
              f"len={len(ex.get('text', ''))}")
        check("the extract names its engine", ex.get("engine") == "playwright",
              str(ex.get("engine")))
        ran += 1

        # The stealth flag, measured on the page rather than assumed.
        wd = await b._page.evaluate("navigator.webdriver")
        check("navigator.webdriver is not true", wd is not True,
              f"a site can read this: {wd!r}")
        ran += 1
    except Exception as e:  # a network failure is not a check failure
        check("the live extraction ran without raising", False, str(e)[:140])
    finally:
        await b.close()
    return ran


if __name__ == "__main__":
    run_static()
    run_launch_config()
    ran = asyncio.run(run_live())

    if ran < 0:
        print("  skip live browser behaviour (Chromium not available)")
    else:
        print(f"  live browser behaviour exercised ({ran} assertions)")

    if fails:
        print(f"{len(fails)} FAILED")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("All browser-extract checks passed.")
