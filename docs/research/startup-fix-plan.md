# Plan: make "start with Windows" launch the whole app

> **Superseded, in part.** This plan assumed the broken Startup entry was
> hand-written and that the app had no autostart support, so it proposed adding
> some. Both were wrong: the app writes that entry itself, and the feature
> already exists end to end (`system.setAutostart`, `set_autostart()`,
> `autostart_enabled()`, and a working dashboard toggle). Only the program the
> entry names was wrong. Superseded by the correction in
> `startup-dashboard-not-showing.md` and by commit `5c7073d`, which fixes the
> writer instead of adding a mechanism. Steps 2 and 3 below were not built.
> Fault B in step 1 was correct and shipped in `820006b`.

Follows the research in `startup-dashboard-not-showing.md`. That document
established the cause; this one decides what to change and in what order.

## What is actually wrong — two independent faults

**Fault A — the startup entry starts the wrong half.**
`%APPDATA%\...\Startup\Addled.bat` (hand-made, not in the repo) runs
`python.exe backend/main.py` directly. Electron never starts, so the tray icon
and the dashboard window — both owned by Electron — never exist. The avatar
appears because the backend draws it.

**Fault B — Settings silently does nothing, even with the backend alone.**
`CharacterWidget._default_settings_dialog` (`backend/character/avatar.py`) has
three steps: set a nav intent, focus the Addled window, then **fall back to
opening the dashboard in a browser**. The fallback is unreachable:

```python
try:
    asyncio.run(_focus())
    return                      # reached whenever _focus() does not raise
except Exception:
    pass
# step 3: webbrowser.open(url)  -- dead code
```

`WindowManager.focus()` returns `{"success": False, "error": "No open window
matched 'Addled'."}` when nothing matches. It **swallows its own errors** and
returns a dict, so it essentially never raises. The `except` therefore never
fires, the unconditional `return` runs, and step 3 is never reached.

Verified live:

```
focus('NoSuchWindowTitleXYZ123') -> {'success': False,
                                     'error': "No open window matched ..."}
```

So with no Electron window, clicking Settings does nothing at all instead of
opening the browser — which is why the symptom is "nothing happens" rather than
"it opened in a tab".

These are separate: fixing A alone would hide B (the window would exist), but B
would come back the moment anything else made the window unavailable — and it
is a plain bug regardless of how the app was started. **Both should be fixed.**

## The fix

### 1. `backend/character/avatar.py` — act on what `focus` returned (small, first)

The bug is a discarded result. Step 2 should fall through when focus fails:

```python
# 2) bring the Addled window to the front
focused = False
try:
    import asyncio
    from backend.actions.window_manager import WindowManager

    async def _focus():
        return await WindowManager().focus("Addled")

    focused = bool((asyncio.run(_focus()) or {}).get("success"))
except Exception:
    focused = False

if focused:
    return

# 3) fallback: open in the default browser
try:
    webbrowser.open(url)
except Exception:
    ...
```

Note `asyncio.run` raises if a loop is already running. That is a real risk in
the backend's own thread, and the `except` already covers it — but the existing
`except: pass` currently *falls through to the return*, so today it also
suppresses the fallback. The rewrite keeps the same protection without losing
step 3.

**Test:** a check that with no window present, the settings dialog reaches
`webbrowser.open` (patch it and assert it was called), and that with a
successful focus it does not. This is testable without Qt by calling the
focus/branch logic if it is factored out; if it is not, the check patches
`WindowManager.focus` to return a failure and asserts the intent/fallback path.

### 2. Autostart the whole app, owned by the app (the real fix for A)

The app has no autostart support at all — no `setLoginItemSettings` anywhere in
`electron/`, and the installer only makes a Start Menu shortcut
(`electron-builder.yml: createStartMenuShortcut`). A user had to hand-write the
`.bat`, and starting the backend is the natural mistake.

So give the app the switch and let it write the entry:

- `electron/main.js` — a small module or pair of IPC handlers:
  `startup-get` / `startup-set`, wrapping `app.getLoginItemSettings()` and
  `app.setLoginItemSettings({ openAtLogin, path: app.getPath('exe'), args })`.
  Electron writes the correct registry entry itself, so the hand-made `.bat`
  is no longer needed.
- On Windows the entry must launch **`Addled.exe`**, which then either starts
  the backend or attaches to one already running (see "already true" below).
- A settings toggle in the dashboard, beside the other app-level switches.

**Why this is the right shape for this repo:** the installer and
`deploy_to_install.ps1` both already treat "what the app owns" as something the
app should manage; the current situation is a user-maintained file that
diverges on every reinstall.

### 3. Migrate the existing hand-made entry

The user already has `Addled.bat` in Startup. After (2), it must go, or both
will run and the backend will be started twice by two owners. The plan:

- Detect it (a `.bat` named `Addled.bat` in the Startup folder containing
  `backend\main.py`) and offer to remove it, or remove it as part of turning
  the new toggle on.
- **Do not delete it silently.** Repo rule and common sense: an explicit,
  user-visible action, named file only.

## What is already true, and must not be "fixed"

Checked before planning, because each of these could look like a missing piece:

- **One app instance already.** `app.requestSingleInstanceLock()` at
  `main.js:19` — a second `Addled.exe` quits, and `second-instance` shows and
  focuses the existing window (`main.js:24`). A user double-clicking the
  shortcut twice does not get two windows.
- **Attaching to a running backend already.** `main.js:618` checks
  `isPortFree(WS_PORT)` and only spawns Python if the port is free. So if the
  backend is already up (today's `.bat`, or a leftover), starting `Addled.exe`
  attaches instead of double-starting.
- **The nav-intent channel works.** `http_api.py` serves `/api/nav` on 9877;
  `electron/main.js:270 startNavPolling()` consumes it every 1.5s. Verified
  live (`{"path": null}` — nothing pending). Fault B is in the *producer's*
  control flow, not this channel.
- **The deployed dashboard is intact** (128 files; `index.html` matches the
  repo build). Not a build problem.

So the fix is genuinely just: act on `focus()`'s result, and start `Addled.exe`
instead of `main.py`.

## Order and risk

| Step | Size | Risk | Notes |
|---|---|---|---|
| 1. Respect `focus()`'s result | small | low | A real bug; testable; no behaviour change when a window exists |
| 2. App-owned autostart | medium | medium | New IPC + a settings toggle; writes to the registry via Electron |
| 3. Retire the hand-made `.bat` | small | low | Explicit and named; never silent |

Step 1 first: it is small, it is a bug on its own merits, and it makes the
backend-only case degrade honestly (open the browser) even before autostart
changes.

## Verification

- Step 1: revert the fix → the fallback assertion fails by name.
- Step 2: exercise get/set through the real IPC (the pattern used for the
  other `window-*` handlers), assert the registry entry points at
  `Addled.exe`, and assert a second run does not double-start the backend
  (relying on `isPortFree`, already covered).
- End to end, the only test that proves the report is fixed: with the new
  entry in place, sign out and back in (or run the entry directly), and confirm
  the **tray icon** and the **dashboard window** both appear — the two things
  missing today.

## Open question for the user

Should the toggle default **on** or **off**? Off matches how most desktop apps
ship and means nobody gets an autostart entry they did not ask for; on matches
what this user clearly wants, given they hand-made the `.bat`. The plan
assumes **off by default with the toggle in Settings**, since it is trivially
turned on and an entry that appears uninvited is the worse surprise.
