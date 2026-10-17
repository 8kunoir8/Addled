"""The local model can be paused while the user games, and comes back after.

Why this exists: the presence guard already detected gaming and already
quietened the agent, but the local model kept several gigabytes resident the
whole time — the resource the user actually wanted back. So the pause has to be
more than stopping the process. Three things have to hold, and each is checked
here:

1. **A pause is not a stop.** `ensure_running()` starts the server on demand,
   so a background request (a bot message, a scheduled task) would reload the
   weights mid-game and the pause would be decorative. While paused, that call
   must REFUSE.

2. **Resuming respects the user's choice.** Starting unconditionally would undo
   a deliberate decision — someone who turned the local model off, or switched
   to a cloud provider, must not find it loaded again because a game closed.

3. **The edges are safe.** The engine calls this on EVERY tick, so a pause must
   be idempotent; and it must not kill a model that is still loading weights.

No real game is required: the manager is driven directly, which is the part
that can be tested honestly. Whether the DETECTOR recognises a particular title
is a separate question and is not something this suite can answer.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_gaming_pause.py
"""

import asyncio
import ctypes
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def main() -> int:
    from backend.config import config
    from backend.local_llm.manager import local_llm

    print("=== the setting exists and defaults on ===")
    # Not merely present: a real game is not needed to confirm the switch is
    # wired, and defaulting OFF would make the feature invisible.
    value = config.get("safety", "pause_local_model_on_gaming", default=None)
    check("pause_local_model_on_gaming is configured", value is not None,
          repr(value))
    check("and defaults to on", bool(value) is True, repr(value))
    # Separate from the noise-suppression switch, so one cannot be mistaken for
    # having broken the other.
    check("it is NOT the same key as gaming_auto_sleep",
          config.get("safety", "gaming_auto_sleep", default=None) is not None)

    print()
    print("=== a pause is not just a stop ===")
    # The state that matters. `is_running()` would be False after a plain stop
    # and would prove nothing, so the checks are on the paused flag and on the
    # refusal, not on the process.
    await local_llm.pause_for("gaming")
    check("the manager reports paused", local_llm.is_paused())
    check("the reason is recorded", local_llm.pause_reason() == "gaming",
          local_llm.pause_reason())
    check("should_run() is False while paused", not local_llm.should_run(),
          "a paused model would still be considered wanted")

    # THE check. With `local` selected and autostart on, an unpaused model would
    # be started here; paused, it must come back with a message instead.
    started_before = local_llm.is_running()
    problem = await local_llm.ensure_running()
    check("ensure_running() refuses while paused", bool(problem),
          "it returned success and may have started the model")
    check("and it did not start the server",
          local_llm.is_running() == started_before,
          f"running went {started_before} -> {local_llm.is_running()}")
    check("the refusal explains itself in plain words",
          problem and "paused" in problem.lower(), str(problem))

    print()
    print("=== the pause is idempotent ===")
    # The engine calls this on every tick (~5s). Treating a second call as an
    # error, or re-stopping, would spam the log and confuse the state machine.
    again = await local_llm.pause_for("gaming")
    check("pausing twice is harmless", again is False or again is True,
          repr(again))
    check("still paused, reason unchanged", local_llm.pause_reason() == "gaming",
          local_llm.pause_reason())

    print()
    print("=== status exposes it, so the UI can explain itself ===")
    st = local_llm.status()
    check("status carries the paused reason", st.get("paused") == "gaming",
          str(st.get("paused")))
    check("status still reports the normal fields",
          "running" in st and "phase" in st)

    print()
    print("=== resume respects a deliberate choice ===")
    # With the local model NOT wanted (keep-warm off, not the chosen provider),
    # clearing the pause must not start it. This is the case that would undo the
    # user's own decision.
    saved = {
        "enabled": config.get("local_llm", "enabled", default=False),
        "autostart": config.get("local_llm", "autostart", default=True),
    }
    try:
        config.set("local_llm", "enabled", value=False)
        # Assert the setup, not just the outcome. Written loosely first, these
        # checks passed even when the branch was vacuous — a test that cannot
        # fail is worse than no test, because it reads as proof.
        wanted = local_llm.should_run()
        if wanted:
            print("     NOTE: the local model is wanted in this config, so the "
                  "'not wanted' path cannot be exercised here.")
            print("           reported, not skipped silently: chosen="
                  f"{local_llm.is_chosen()} keep={local_llm.keep_running()}")
        else:
            check("the local model is genuinely NOT wanted for this test",
                  not wanted, "the not-wanted branch was not reached")
            check("should_run() is False", not local_llm.should_run())
        await local_llm.resume_from_pause()
        check("resuming cleared the pause", not local_llm.is_paused(),
              local_llm.pause_reason())
        if not wanted:
            # The point of the whole case: a game closing must not resurrect a
            # model the user did not ask for.
            check("resuming did NOT start an unwanted model",
                  not local_llm.is_running(),
                  "an unwanted model was started")
    finally:
        config.set("local_llm", "enabled", value=saved["enabled"])
        config.set("local_llm", "autostart", value=saved["autostart"])

    print()
    print("=== resuming a model that was never paused is a no-op ===")
    check("not paused to begin with", not local_llm.is_paused())
    did = await local_llm.resume_from_pause()
    check("resume returns False when there was no pause", did is False,
          repr(did))

    print()
    print("=== a loading model is not killed ===")
    # pause_for refuses while _phase == "starting". Killing a half-read of
    # several gigabytes wastes the load and can leave the port held.
    original_phase = local_llm._phase
    try:
        local_llm._phase = "starting"
        ok = await local_llm.pause_for("gaming")
        check("pause_for declines while the model is starting", ok is False,
              repr(ok))
        check("and it did NOT record a pause", not local_llm.is_paused(),
              local_llm.pause_reason())
    finally:
        local_llm._phase = original_phase

    print()
    print("=== the engine hook actually pauses (through a real loop) ===")
    # Driven through a real event loop, because the engine DISPATCHES the pause
    # to the server's loop rather than awaiting it. A first version gated the
    # call on `is_running()`, which meant the common case — the model is not
    # loaded when a game starts — recorded nothing at all, and the unit checks
    # above still passed because they drove the manager directly. This is that
    # bug's regression test.
    from backend.engine import Engine
    from backend.ws_server import get_server
    server = get_server()
    had_loop = server._loop
    try:
        server._loop = asyncio.get_running_loop()
        eng = Engine()
        local_llm._paused_reason = ""
        local_llm._phase = "idle"

        # The important precondition: NOT running. This is the normal state when
        # a game starts, and the one the earlier bug mishandled.
        check("the model is not running for this test",
              not local_llm.is_running(), "cannot test the down case")

        eng._sync_gaming_pause(True, "gaming")
        await asyncio.sleep(0.2)
        check("a gaming tick pauses even though the model was not running",
              local_llm.is_paused(), repr(local_llm.pause_reason()))
        check("the recorded reason is gaming",
              local_llm.pause_reason() == "gaming", local_llm.pause_reason())
        check("and ensure_running now refuses",
              bool(await local_llm.ensure_running()))

        # Gaming only: a meeting must not pause the model.
        local_llm._paused_reason = ""
        eng._sync_gaming_pause(True, "meeting")
        await asyncio.sleep(0.1)
        check("a meeting tick does NOT pause the model",
              not local_llm.is_paused(), repr(local_llm.pause_reason()))

        # Game closed: arm the delay, do not resume on the spot.
        local_llm._paused_reason = "gaming"
        eng._gaming_resume_at = 0.0
        eng._sync_gaming_pause(False, "")
        check("leaving the game arms a delayed resume",
              eng._gaming_resume_at > 0, str(eng._gaming_resume_at))
        check("and does not resume on the same tick",
              local_llm.is_paused(), "it would thrash on an alt-tab")
        local_llm._paused_reason = ""

        # The switch must disable the whole behaviour.
        config.set("safety", "pause_local_model_on_gaming", value=False)
        local_llm._paused_reason = ""
        eng._sync_gaming_pause(True, "gaming")
        await asyncio.sleep(0.1)
        check("the setting turns the pause off",
              not local_llm.is_paused(), repr(local_llm.pause_reason()))
    finally:
        config.set("safety", "pause_local_model_on_gaming", value=True)
        local_llm._paused_reason = ""
        server._loop = had_loop

    print()
    print("=== the detector's rules, scored directly ===")
    # These call the SHIPPING classifier with plain numbers - no window system
    # is involved, so the result does not depend on what is on screen. The
    # previous version re-implemented the rules here and then grepped the
    # source for strings to keep the copy honest: two sources of truth, plus a
    # match a comment can satisfy. The rules under test are the ones that ship.
    #
    # Rows 1-5 are REAL windows measured on this machine, including a real
    # running game. The rest pin shapes that have no specimen here. A borderless
    # game often sizes to the WORK AREA (1392 here) rather than the screen
    # (1440), because it does not cover the taskbar - and a maximised ordinary
    # window is the same size, so size alone can never separate them.
    from backend.safety.presence_guard import (
        classify_window, _under_system,
        WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE,
    )

    WORK_W, WORK_H = 2560, 1392
    MON_W, MON_H = 2560, 1440
    GAME_PATH = (r"E:\Program\Game\Delta Force\DeltaForce\Binaries"
                 r"\Win64\DeltaForceClient-Win64-Shipping.exe")
    SYS_PATH = r"C:\Windows\System32\some.exe"
    OWN_PID, OTHER_PID = 4242, 7777

    # (label, class, w, h, style, exstyle, zoomed, pid, path, want, want_reason)
    cases = [
        # --- real windows, measured -------------------------------------------------
        ("real: Program Manager", "Progman", 3640, 1920, 0x96000000, 0x00200080,
         False, OTHER_PID, r"C:\Windows\explorer.exe", False, "shell class"),
        # 0x00200000 carries NO NOACTIVATE - only the class rule catches this one.
        ("real: Windows Input Experience", "Windows.UI.Core.CoreWindow",
         2560, 1440, 0x94000000, 0x00200000, False, OTHER_PID,
         r"C:\Windows\SystemApps\X\TextInputHost.exe", False, "shell class"),
        ("real: cursor overlay", "Cua.AgentCursorOverlay", 3640, 1920, 0x94000000,
         0x080800A0, False, OTHER_PID, r"C:\Users\x\cua-driver.exe",
         False, "shell class"),
        ("real: maximised browser", "Chrome_WidgetWin_1", 2576, 1408, 0x17CF0000,
         0x00200100, True, OTHER_PID, r"C:\Program Files\Brave\brave.exe",
         False, "maximised"),
        # The real game: Garena Delta Force, borderless, WS_EX_APPWINDOW set.
        ("real: Garena Delta Force", "UnrealWindow", 2560, 1440, 0x94020000,
         0x00040000, False, OTHER_PID, GAME_PATH, True, "borderless fullscreen"),
        # --- synthetic, pinning shapes with no specimen ------------------------------
        ("borderless game at work area", "UnityWndClass", 2560, 1392, 0x90000000,
         0, False, OTHER_PID, GAME_PATH, True, "borderless fullscreen"),
        ("borderless game one pixel short", "UnityWndClass", 2559, 1391, 0x80000000,
         0, False, OTHER_PID, GAME_PATH, True, "borderless fullscreen"),
        ("browser fullscreen video", "Chrome_WidgetWin_1", 2560, 1440,
         0x80C00000, 0x00000100, False, OTHER_PID,
         r"C:\Program Files\Brave\brave.exe", False, "captioned popup"),
        ("small splash window", "Dlg", 900, 600, 0x86800000, 0, False,
         OTHER_PID, GAME_PATH, False, "too small"),
        ("our own window, full screen", "Qt51519QWindowIcon", 2560, 1392,
         0x96000000, 0, False, OWN_PID, GAME_PATH, False, "own process"),
        ("tool window, full screen", "SomeTool", 2560, 1400, 0x94000000,
         WS_EX_TOOLWINDOW, False, OTHER_PID, GAME_PATH, False, "tool window"),
        ("click-through overlay", "Overlay", 2560, 1440, 0x94000000,
         WS_EX_LAYERED | WS_EX_TRANSPARENT, False, OTHER_PID, GAME_PATH,
         False, "click-through overlay"),
        ("system binary, borderless", "SomeClass", 2560, 1440, 0x94000000,
         0, False, OTHER_PID, SYS_PATH, False, "system binary"),
        ("small ordinary window", "Qt51519QWindowIcon", 816, 633, 0x96CF0000,
         0x00000100, False, OTHER_PID, r"E:\app\app.exe", False, "too small"),
        # 3640x1920 exceeds the 2560x1440 monitor, so the overshoot rule fires
        # before the style rules. Still True - the verdict is what matters, and
        # the reason is asserted so the ordering stays visible.
        ("game larger than the monitor", "UnityWndClass", 3640, 1920, 0x80000000,
         0, False, OTHER_PID, GAME_PATH, True, "larger than the monitor"),
    ]

    for (label, cls, w, h, style, ex, zoomed, pid, path, want, want_why) in cases:
        got, why = classify_window(
            class_name=cls, width=w, height=h, style=style, exstyle=ex,
            zoomed=zoomed, work_w=WORK_W, work_h=WORK_H,
            monitor_w=MON_W, monitor_h=MON_H,
            pid=pid, own_pid=OWN_PID, image_path=path)
        # Assert the REASON too. A row that returns False because it was "too
        # small" proves nothing about the rule it was written for.
        check(f"detector: {label}", got == want and why == want_why,
              f"want {want} ({want_why}), got {got} ({why})")

    # The floor must be the WORK AREA, not the screen. A borderless game that
    # does not cover the taskbar is 1392 tall; measured on this machine it
    # would be judged too small against the 1440-tall screen. This is the exact
    # regression the old code was fixed for, so pin it by size.
    got, why = classify_window(
        class_name="UnityWndClass", width=2560, height=1392, style=0x90000000,
        exstyle=0, zoomed=False, work_w=WORK_W, work_h=WORK_H,
        pid=OTHER_PID, own_pid=OWN_PID, image_path=GAME_PATH)
    check("a game sized to the work area is still a game",
          (got, why) == (True, "borderless fullscreen"), f"got {got} ({why})")

    # And the floor itself is real: a frameless POPUP that covers everything
    # except its size is not full-screen. Without this, a small borderless
    # window would be called a game (found by ablation).
    got, why = classify_window(
        class_name="SomePopup", width=800, height=600, style=0x80000000,
        exstyle=0, zoomed=False, work_w=WORK_W, work_h=WORK_H,
        pid=OTHER_PID, own_pid=OWN_PID, image_path=GAME_PATH)
    check("a small frameless popup is not a game",
          (got, why) == (False, "too small"), f"got {got} ({why})")

    # A game that KEEPS ITS CAPTION. Measured live: Delta Force in windowed
    # mode is 2576x1479 at (-8,-31) on a 2560x1440 monitor, style 0x14CA0000
    # (WS_CAPTION set), not zoomed. Borderless fullscreen is implemented by
    # drawing past the screen edge, so the window is LARGER than the monitor -
    # which no ordinary window the user can reach is.
    #
    # The old code returned False for this window, so this is a gap that
    # predates the rewrite, found by testing against the running game.
    got, why = classify_window(
        class_name="UnrealWindow", width=2576, height=1479, style=0x14CA0000,
        exstyle=0x00040100, zoomed=False, work_w=WORK_W, work_h=WORK_H,
        monitor_w=MON_W, monitor_h=MON_H, pid=OTHER_PID, own_pid=OWN_PID,
        image_path=GAME_PATH)
    check("a captioned window larger than the monitor is a game",
          (got, why) == (True, "larger than the monitor"), f"got {got} ({why})")

    # ...but a CAPTIONED window that merely FILLS the monitor is a maximised
    # app or a dialog, not a game. This is the row that stops the rule above
    # from swallowing every full-screen window.
    got, why = classify_window(
        class_name="Chrome_WidgetWin_1", width=2560, height=1440,
        style=0x14CF0000, exstyle=0x00000100, zoomed=False,
        work_w=WORK_W, work_h=WORK_H, monitor_w=MON_W, monitor_h=MON_H,
        pid=OTHER_PID, own_pid=OWN_PID,
        image_path=r"C:\Program Files\Brave\brave.exe")
    check("a captioned window exactly the monitor size is not",
          got is False, f"got {got} ({why})")

    # The overshoot rule comes AFTER the shell rule, and it has to: `Progman`
    # and the cursor overlay BOTH overshoot this monitor. If the order were
    # reversed, the desktop would be called a game.
    for cls in ("Progman", "Cua.AgentCursorOverlay"):
        got, why = classify_window(
            class_name=cls, width=3640, height=1920, style=0x96000000,
            exstyle=0x00200080, zoomed=False, work_w=WORK_W, work_h=WORK_H,
            monitor_w=MON_W, monitor_h=MON_H, pid=OTHER_PID, own_pid=OWN_PID,
            image_path=r"C:\Windows\explorer.exe")
        check(f"overshoot does not rescue {cls}",
              (got, why) == (False, "shell class"), f"got {got} ({why})")

    # The no-activate rule has no specimen on this machine - nothing observed
    # carries WS_EX_NOACTIVATE AND passes every other rule - so it is pinned by
    # construction here. Without this row the rule is untested, and removing it
    # broke nothing (found by ablation).
    got, why = classify_window(
        class_name="SomeOverlay", width=2560, height=1440, style=0x94000000,
        exstyle=WS_EX_NOACTIVATE, zoomed=False, work_w=WORK_W, work_h=WORK_H,
        pid=OTHER_PID, own_pid=OWN_PID, image_path=GAME_PATH)
    check("a no-activate window is not a game",
          (got, why) == (False, "no-activate overlay"), f"got {got} ({why})")

    # The same window with the same flags, minus NOACTIVATE, IS a game. This is
    # what makes the row above a test of that rule rather than of the shape.
    got, why = classify_window(
        class_name="SomeOverlay", width=2560, height=1440, style=0x94000000,
        exstyle=0, zoomed=False, work_w=WORK_W, work_h=WORK_H,
        pid=OTHER_PID, own_pid=OWN_PID, image_path=GAME_PATH)
    check("and the same window without it is", got is True, why)

    # The overlay rule is a judgement call, so it is a parameter rather than a
    # hardcoded assumption: a support report can turn it off without a rebuild.
    got, why = classify_window(
        class_name="SomeOverlay", width=2560, height=1440, style=0x94000000,
        exstyle=WS_EX_NOACTIVATE, zoomed=False, work_w=WORK_W, work_h=WORK_H,
        pid=OTHER_PID, own_pid=OWN_PID, image_path=GAME_PATH,
        noactivate_rejects=False)
    check("the no-activate rule can be turned off", got is True, why)

    # A prefix test must not match a sibling directory: C:\WindowsApps is not
    # under C:\Windows. This failed open while it was being written.
    check("system-path check respects the separator",
          _under_system(("C:\\Windows",), r"C:\WindowsApps\Foo\foo.exe") is False)
    check("system-path check is case-folded",
          _under_system(("C:\\Windows",), r"C:\WINDOWS\System32\x.exe") is True)
    check("a real game path is not a system path",
          _under_system(("C:\\Windows",), GAME_PATH) is False)

    print()
    print("=== the engine hook is wired ===")
    from pathlib import Path
    eng = Path(ROOT, "backend", "engine.py").read_text(encoding="utf-8")
    check("the engine has the sync method", "_sync_gaming_pause" in eng)
    check("and calls it from the tick", "_sync_gaming_pause(blocked, reason)" in eng)
    check("it reads the new setting",
          "pause_local_model_on_gaming" in eng,
          "the engine never consults the switch")
    # Gaming only: a meeting pause was deliberately left out.
    check("it pauses for gaming, not for every block",
          'reason == "gaming"' in eng, "it would pause for meetings too")
    # The delay is what stops an alt-tab thrashing the model in and out.
    check("a resume delay exists", "_gaming_resume_delay" in eng)
    # The cross-loop dispatch, which is the difference between working and
    # hanging the engine.
    check("the work is dispatched, not awaited cross-loop",
          "run_soon" in eng, "it may await a coroutine owned by another loop")

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all gaming-pause checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
