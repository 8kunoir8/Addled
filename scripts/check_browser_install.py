"""Browser backend install checks — the manual Install button's backend.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_browser_install.py

Pins the parts of the install flow the Settings page depends on: the status
report has the shape the page draws, an unknown backend is refused rather than
half-handled, a press clears the failure backoff (a person retrying is not the
app polling), and the RPCs the page calls are registered.

Nothing here performs an install — that downloads ~150 MB and is the user's
decision to make by pressing the button.
"""

import os
import sys
import time
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import app_paths
from backend.browser import auto_install

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def run():
    # ---- status shape -----------------------------------------------------
    st = auto_install.status("playwright")
    for field in ("backend", "known", "installed", "installing",
                  "retryInSeconds", "version", "package", "installs"):
        check(f"playwright status reports '{field}'", field in st, str(st))
    check("the playwright package is named", st.get("package") == "playwright",
          str(st))
    check("the install list names what will be downloaded",
          any("Chromium" in x for x in (st.get("installs") or [])),
          str(st.get("installs")))

    fw = auto_install.status("framework")
    check("framework status is known", fw.get("known") is True, str(fw))
    check("its package is browser-use", fw.get("package") == "browser-use",
          str(fw))

    unknown = auto_install.status("nonsense")
    check("an unknown backend reports known=False rather than raising",
          unknown.get("known") is False, str(unknown))

    both = auto_install.status_all()
    check("status_all returns a policy", "policy" in both, str(both))
    check("status_all covers both backends",
          len(both.get("backends") or []) == 2, str(both))
    check("each backend entry names itself",
          all(b.get("backend") for b in both["backends"]), str(both))

    # ---- backoff -----------------------------------------------------------
    auto_install._fail_until["playwright"] = time.time() + 3600
    st = auto_install.status("playwright")
    check("a failure backoff is reported as seconds remaining",
          st.get("retryInSeconds", 0) > 0, str(st))

    auto_install.reset_backoff("playwright")
    check("the backoff can be cleared on demand",
          auto_install.can_retry("playwright"), "still backing off")

    auto_install._fail_until["playwright"] = time.time() + 3600
    check("reset_backoff refuses an unknown backend",
          auto_install.reset_backoff("nope").get("success") is False,
          "unknown backend accepted")

    # `approve` is what the button reaches. It must clear the backoff itself,
    # or a press after a failed attempt returns instantly without trying — the
    # failure this pins.
    auto_install._fail_until["framework"] = time.time() + 3600
    import asyncio

    async def _probe():
        # Stop before the real work: pretend it is already installed so
        # `approve` returns early, and assert the backoff was cleared on the
        # way in.
        real_is = auto_install.is_installed
        auto_install.is_installed = lambda b: True
        try:
            res = await auto_install.approve("framework")
            return res, auto_install.can_retry("framework")
        finally:
            auto_install.is_installed = real_is

    res, can = asyncio.run(_probe())
    check("approve reports an already-installed backend",
          res.get("status") == "installed", str(res))
    check("and clears the backoff so a press always tries", can,
          "the backoff survived an explicit press")

    auto_install._fail_until.update({"playwright": 0.0, "framework": 0.0})

    # ---- the RPCs the page calls exist ------------------------------------
    import inspect
    from backend import ws_server
    src = inspect.getsource(ws_server)
    for name in ("browser.installStatus", "browser.installNow",
                 "browser.installApprove"):
        check(f"'{name}' is registered", f'"{name}"' in src,
              "the Settings button would fail with 'no such method'")

    # ---- the page has a real button, not a README pointer ------------------
    page = os.path.join(ROOT, "dashboard", "src", "app", "settings",
                        "page.tsx")
    text = open(page, encoding="utf-8", errors="replace").read()
    check("the Settings page calls browser.installNow",
          "browser.installNow" in text,
          "there is no manual install button")
    check("the page no longer tells the user to run a script",
          "fetch_playwright.py" not in text,
          "the old 'see README' row is still there")

    # ---- availability reflects the BINARY, not just the package -----------
    #
    # The bug this pins: `_playwright_available` returned True whenever the
    # `playwright` package imported, so an install missing its Chromium (or
    # holding the wrong revision, which is what a second Playwright's
    # `npx playwright install` leaves behind) reported "installed" to the
    # Settings page and the Install button never appeared.
    from backend.browser.browser_engine import PlaywrightBrowser
    check("availability is not just the package import",
          hasattr(PlaywrightBrowser, "_chromium_available"),
          "nothing checks the browser binary")
    check("importability is its own question",
          hasattr(PlaywrightBrowser, "_playwright_importable"),
          "package presence and browser presence are conflated")

    real_importable = PlaywrightBrowser._playwright_importable
    real_chromium = PlaywrightBrowser._chromium_available
    try:
        # Package present, browser missing → NOT available.
        PlaywrightBrowser._playwright_importable = staticmethod(lambda: True)
        PlaywrightBrowser._chromium_available = staticmethod(lambda: False)
        check("package without a browser is not 'available'",
              PlaywrightBrowser._playwright_available() is False,
              "the old lie: package import alone reads as installed")
        # Package missing → NOT available, regardless of a stray binary.
        PlaywrightBrowser._playwright_importable = staticmethod(lambda: False)
        PlaywrightBrowser._chromium_available = staticmethod(lambda: True)
        check("a browser without the package is not 'available'",
              PlaywrightBrowser._playwright_available() is False, "")
    finally:
        PlaywrightBrowser._playwright_importable = real_importable
        PlaywrightBrowser._chromium_available = real_chromium

    # ---- the revision is what matters, not the folder name ----------------
    #
    # A real install held chromium-1243 while the resolved package wanted
    # chromium-1234. A check for "a chromium-* directory" passed and every
    # launch failed; the executable path must be derived from browsers.json.
    needed = app_paths._playwright_needed_revisions()
    check("needed revisions come from the package's own browsers.json",
          isinstance(needed, set), str(type(needed)))
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        browsers = Path(tmp) / "ms-playwright"
        browsers.mkdir()
        wanted = sorted(needed) if needed else ["1234"]
        # Present, but the WRONG revision: must still read as unavailable.
        wrong = browsers / f"chromium-{int(wanted[0]) + 100}"
        (wrong / "chrome-win64").mkdir(parents=True)
        (wrong / "chrome-win64" / "chrome.exe").write_bytes(b"")
        saved_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
        try:
            check("a chromium of the WRONG revision does not count",
                  PlaywrightBrowser._chromium_available() is False,
                  "folder-name matching would accept it and every launch "
                  "would then fail on the missing executable")
        finally:
            if saved_env is None:
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            else:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved_env

    # ---- the browser cache is Addled's own --------------------------------
    check("Addled keeps its browsers under its own data dir",
          hasattr(app_paths, "BROWSERS_DIR"), "no isolated browser cache")
    check("the env var Playwright reads is the one that is set",
          app_paths.PLAYWRIGHT_BROWSERS_ENV == "PLAYWRIGHT_BROWSERS_PATH",
          "Playwright would not see the override")
    saved = os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    try:
        check("configure points Playwright at that directory",
              app_paths.configure_browsers_path()
              and os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
              == str(app_paths.BROWSERS_DIR),
              "Playwright still shares the machine-wide cache")
        # An operator override must survive.
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "X:/deliberate"
        app_paths.configure_browsers_path()
        check("a deliberate override is respected",
              os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "X:/deliberate",
              "the app overwrote a path the operator set")
    finally:
        if saved is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = saved

    # ---- user-site packages can no longer shadow pylibs -------------------
    #
    # The second half of the bug: the app's interpreter is not `-s`, so a
    # stray `pip install --user playwright` sat ahead of `pylibs` on sys.path
    # and the Install button's work was never the copy that got imported.
    check("the duplication test knows about the user-site",
          hasattr(app_paths, "_user_site_names"),
          "only the bundled site-packages is considered")
    check("pylibs is placed ahead of the first site-packages when a name "
          "is shared",
          "site-packages" in inspect.getsource(app_paths.add_pylibs_to_path),
          "the insertion that makes pylibs win is gone")

    # ---- degradation is reported, not hidden ------------------------------
    engine_src = inspect.getsource(PlaywrightBrowser)
    check("an HTTP fallback is marked degraded",
          "degraded" in engine_src,
          "a plain fetch is presented as a browse with no way to tell")
    check("the reason a browser was not used is carried",
          "_playwright_unavailable_reason" in engine_src,
          "the user is told nothing about why")
    nav = inspect.getsource(PlaywrightBrowser.navigate)
    check("navigate sets the degraded flag on its HTTP path",
          "degraded" in nav, "navigate hides the fallback")


def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: browser install — status shape, backoff reset, button wired, "
          "availability reflects the binary, cache is Addled's own")
    return 0

if __name__ == "__main__":
    sys.exit(main())
