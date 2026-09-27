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

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: browser install — status shape, backoff reset, button wired")
    return 0

if __name__ == "__main__":
    sys.exit(main())
