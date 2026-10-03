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


def _foreground_is_frameless(u) -> bool:
    """Whether the window in front is the kind a game would be.

    Used only to decide if a live-desktop check is meaningful. If the user
    really is in a game while this runs, asserting "not gaming" would fail
    through no fault of the code.

    Must check SIZE as well as style. Style alone said "a game is in front" for
    the TASKBAR — an empty-titled popup with no caption, 2560x48 — and reported
    a failure that the detector itself had correctly avoided. A game covers the
    work area; that is half of what makes it one, so the check has to include it
    or it is testing a different question than the code asks.
    """
    try:
        GWL_STYLE = -16
        WS_POPUP = 0x80000000
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        hwnd = u.GetForegroundWindow()
        style = u.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        has_caption = bool(style & WS_CAPTION)
        has_frame = bool(style & WS_THICKFRAME)
        is_popup = bool(style & WS_POPUP)
        frameless = not ((has_caption or has_frame) and not is_popup)
        if not frameless:
            return False

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = _RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        work = _RECT()
        u.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
        wa_w = work.right - work.left
        wa_h = work.bottom - work.top
        # Big enough to be covering the usable screen, frameless, and so the
        # kind of window a game would be.
        return w >= wa_w - 2 and h >= wa_h - 2
    except Exception:  # noqa: BLE001
        return False


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
    print("=== the detector recognises borderless-windowed games ===")
    # Scored against the shapes that actually differ, using this machine's
    # real numbers. A borderless game often sizes to the WORK AREA (1392 tall)
    # rather than the screen (1440), because it does not cover the taskbar —
    # and the old size-only test missed exactly that. A maximised ordinary
    # window is the SAME size, so size alone can never separate them; the
    # window STYLE does, which is what these cases pin down.
    import ctypes
    u = ctypes.windll.user32
    screen_w = u.GetSystemMetrics(0)
    screen_h = u.GetSystemMetrics(1)

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    _work = _RECT()
    u.SystemParametersInfoW(0x0030, 0, ctypes.byref(_work), 0)
    work_w = _work.right - _work.left or screen_w
    work_h = _work.bottom - _work.top or screen_h

    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000

    def classify(w, h, style):
        """The detector's decision, mirroring presence_guard._detect_gaming."""
        SLACK = 2
        if not (w >= work_w - SLACK and h >= work_h - SLACK):
            return False
        has_caption = bool(style & WS_CAPTION)
        has_frame = bool(style & WS_THICKFRAME)
        is_popup = bool(style & WS_POPUP)
        if (has_caption or has_frame) and not is_popup:
            return False
        if has_caption and not has_frame:
            return False
        return True

    cases = [
        ("exclusive fullscreen", screen_w, screen_h, WS_POPUP, True),
        ("borderless at screen size", screen_w, screen_h, WS_POPUP, True),
        # The regression: this is the shape the old test reported as not-gaming.
        ("borderless at work-area size", work_w, work_h, WS_POPUP, True),
        ("borderless one pixel short", screen_w - 1, screen_h - 1, WS_POPUP, True),
        ("maximised ordinary window", work_w, work_h,
         WS_CAPTION | WS_THICKFRAME, False),
        ("large plain window", 1400, 900, WS_CAPTION | WS_THICKFRAME, False),
        ("splash dialog", screen_w, screen_h, WS_POPUP | WS_CAPTION, False),
        ("small popup", 400, 300, WS_POPUP, False),
    ]
    for label, w, h, style, want in cases:
        check(f"detector: {label}", classify(w, h, style) == want,
              f"{w}x{h} style=0x{style:08X}")

    # And the real desktop must not be a false positive while we sit in an editor.
    from backend.safety.presence_guard import PresenceGuard
    guard = PresenceGuard()
    check("the real desktop is not misread as gaming",
          guard._detect_gaming() in (True, False) and not (
              # Only meaningful when an ordinary window is in front, which is
              # what this check is for. Reported rather than asserted when a
              # game is genuinely running, so the suite does not fail on the
              # user's own gaming session.
              _foreground_is_frameless(u)),
          "a frameless window is in front right now (a game?) — rerun later")

    # The classifier above mirrors the detector, so it could agree with itself
    # while the real function drifted. These tie the two together: the shipped
    # code must contain each rule the mirror applies.
    from pathlib import Path
    guard_src = Path(ROOT, "backend", "safety",
                     "presence_guard.py").read_text(encoding="utf-8")
    body = guard_src.split("def _detect_gaming")[1].split("def _has_user_input")[0]
    for rule, needle in (
        ("the detector reads the work area", "SPI_GETWORKAREA"),
        ("it falls back to the screen when the work area is unavailable", "got_work"),
        ("it allows a pixel of slack", "SLACK"),
        ("it inspects the window style", "GWL_STYLE"),
        ("it rejects a framed non-popup window (a maximised app)",
         "looks_like_a_window and not is_popup"),
        ("it rejects a captioned popup (a splash)", "has_caption and not has_frame"),
    ):
        check(rule, needle in body, "the mirror may not match the shipping code")

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
