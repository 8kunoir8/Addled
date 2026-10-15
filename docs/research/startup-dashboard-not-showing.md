# Why the dashboard (and tray) is missing when Addled starts with Windows

## The report

On Windows startup, Addled's backend starts and the floating avatar appears,
but the dashboard does not. There is no system-tray icon. Right-clicking the
avatar and choosing Settings does nothing.

## Cause

**The startup shortcut launches the backend only, not the app.**

`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Addled.bat` contains:

```bat
@echo off
"C:\Program Files\Addled\resources\python\python.exe" "C:\Program Files\Addled\resources\backend\main.py"
```

`backend/main.py` is the **PyQt** process. Its own docstring says what it boots:
"Boots Qt, spawns character, engine, and WebSocket server." It draws the avatar
(`CharacterWidget`) and serves the WebSocket and HTTP APIs. It does **not**
launch `Addled.exe`, and it contains no reference to Electron at all.

The tray icon and the dashboard window are both owned by the **Electron** main
process (`electron/main.js` — `createTray()` and the single `BrowserWindow`).
Nothing starts it, so neither exists. All three symptoms are one cause:

| Symptom | Why |
|---|---|
| Avatar appears | Drawn by `backend/main.py` via PyQt, which *is* running |
| No tray icon | Tray is created by `electron/main.js:createTray()`, which never runs |
| Settings does nothing | The click sets a nav *intent* that only Electron consumes (below) |

## Why Settings silently does nothing

The avatar's menu is built in the **backend**, and it does not open a window
itself — it cannot. It records an intent, and the Electron shell is expected to
notice:

- `backend/http_api.py` serves `GET /api/nav` on port **9877**, and its
  docstring states the contract: *"The Electron main process polls GET
  /api/nav every ~1.5s and, when a path is present, shows/focuses the Addled
  window and loads that dashboard page. This is the reliable channel for
  'character menu → open GUI at /settings' because a hidden Electron window
  throttles the renderer's WebSocket."*
- `electron/main.js:startNavPolling()` (line 270) is the only consumer. Its
  guard is `if (mainWindow && !mainWindow.isDestroyed())` (line 281) — so with
  no Electron process there is no `mainWindow`, and the intent is never read.

The intent is a single-shot value that is cleared when read
(`consume_nav_intent()`), but with nobody polling it simply sits there. The
backend is not broken; the half that acts on it was never started.

Verified on the running machine: the backend is up and the channel is live and
waiting —

```
GET http://127.0.0.1:9877/api/nav  -> 200 {"path": null}
GET http://127.0.0.1:9877/api/ping -> 200 {"ok": true}
```

`{"path": null}` is the intent nobody has consumed.

## Is this a bug in the code, or in how it was started?

Both, in different places, and the distinction matters for the fix:

1. **The startup entry is wrong.** The `.bat` in the Startup folder runs the
   backend directly. A normal launch runs `Addled.exe`, which starts the
   backend *as a child* and keeps the Electron shell — that is why launching
   from the Start menu gives a tray icon and a dashboard, and this does not.
   The `.bat` was written by hand (dated after the last install) rather than
   produced by the app.
2. **The app never manages its own autostart.** There is no
   `setLoginItemSettings`/`openAtLogin` anywhere in `electron/`, so the app has
   no supported way to start with Windows. A user wanting this had to write a
   shortcut themselves, and picking the backend is the easy mistake: it is the
   process that "does the work".

So the dashboard-not-showing is a *consequence* of an autostart entry that
starts the wrong half of the app.

## What a fix would look like

Stated as options, not yet chosen:

- **Point the startup entry at `Addled.exe`.** One line; the shell starts the
  backend itself and both windows and tray appear. Minimal, but leaves the
  startup entry as something a user maintains by hand.
- **Give the app real autostart support** — a `setLoginItemSettings` call
  behind a setting, so "start with Windows" is a switch in the UI and the app
  writes the correct entry itself. This is what removes the class of mistake;
  it is more work and needs a UI surface.
- **Make the backend able to start the shell** if it is not already running,
  so a backend-only launch heals itself. This is the most defensive and the
  most surprising: a backend that spawns a GUI is a side effect nobody expects,
  and it would fight a deliberate backend-only run (headless, testing).

Given the repo's existing habits (`scripts/deploy_to_install.ps1`, and the
installer in `electron/installer-precompile.nsh`), the second option is the one
that fits: the app owns its own startup entry rather than relying on a
hand-made file.

## Evidence gathered

- Read the Startup folder: `Addled.bat` (backend only), plus unrelated
  `9router.vbs`, `Ollama.lnk`.
- Read `backend/main.py`: boots Qt + character + engine + WS; no Electron.
- Read `electron/main.js`: single `BrowserWindow` (line 301), `createTray()`
  (line 481), `startNavPolling()` (line 270), `ready-to-show` → `show()`
  (line 462).
- Read `backend/http_api.py`: the `/api/nav` intent contract.
- Probed the live backend: `/api/nav` returns `{"path": null}`, `/api/ping`
  returns `{"ok": true}`.
- Confirmed the deployed dashboard is intact (128 files, `index.html` matches
  the repo build), so the missing dashboard is not a broken build.

## Correction: the startup entry is written by the app, not by hand

This document first said the `Addled.bat` had been hand-written because the
file is not in the repo. That was wrong, and the fix depended on knowing
otherwise, so it is corrected here rather than quietly edited above.

The app writes it. `backend/onboarding/wizard.py` has `_autostart_shortcut()`,
resolving to exactly:

```
C:\Users\<user>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\Addled.bat
```

and `_apply_autostart()` wrote:

```python
exe = sys.executable                              # the bundled python.exe
main_script = Path(__file__).parent.parent / "main.py"
f.write(f'@echo off\n"{exe}" "{main_script}"\n')
```

Same path, same format, same `main.py` target as the file on disk. So Fault A
is not a user mistake: **the app's own "Start with Windows" option creates the
broken entry.** A user who ticks that box once gets, at every future sign-in,
the character with no tray and no dashboard.

That also removes the reason for a new mechanism. The feature already existed
end to end — `system.setAutostart` over the WebSocket, `set_autostart()`,
`autostart_enabled()`, surfaced in `system.status`, with a working toggle in
the dashboard at `dashboard/src/app/settings/page.tsx` — and only the program
it named was wrong. The fix is the writer, not the feature.
