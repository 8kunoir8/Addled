"""System tools must not report success they did not achieve.

Two live defects, both found on 2026-10-14 by asking the app to change the
volume and the brightness and reading the machine back:

  * ``set_volume`` shelled out to ``nircmd`` — a third-party binary that was
    never bundled — and returned ``{"success": True}`` unconditionally, so a
    machine that had the tool and one that did not were reported identically.
    The user asked for 30% and the call said it worked.
  * ``set_brightness`` ran a PowerShell command, DISCARDED its output entirely,
    and returned ``{"success": True, "level": n}`` on every path. The command
    returned exit 1 with ``Get-WmiObject : Not supported`` (no DDC/CI on that
    display) and the tool still claimed success. It was wrong 100% of the time.

The same class as a model reporting work it did not do, and the reason the fix
is a READBACK rather than a better command: a volume setter cannot know it
worked from an exit code, only from the endpoint reporting the new level.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_system_controls.py
"""

import asyncio
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        if detail:
            print(f"       {detail}")
        fails.append(label)


def main() -> int:
    from backend.actions import audio, system_controls

    print("system controls — success must mean success")
    print()

    # ------------------------------------------------------------------
    # 1. The source-level guarantee: no unconditional success.
    # ------------------------------------------------------------------
    vol_src = inspect.getsource(system_controls.SystemControls.set_volume)
    bright_src = inspect.getsource(system_controls.SystemControls.set_brightness)

    # The docstring names `nircmd` deliberately - it records what went wrong -
    # so the test is on the BODY: a call, not a mention.
    check("set_volume no longer runs nircmd",
          "nircmd" not in vol_src.split('"""')[-1],
          "nircmd is not bundled; the call raised WinError 2 on every machine")
    check("set_volume delegates to the verified audio module",
          "audio.set_volume" in vol_src,
          "the readback lives in backend.actions.audio")
    check("set_brightness checks the command result",
          "proc.returncode" in bright_src or 'out == "OK"' in bright_src,
          "the old body discarded stdout and returned success anyway")
    check("set_brightness cannot return success without the marker",
          'out == "OK"' in bright_src,
          "success must be gated on the script's own OK, not on not-raising")
    check("the old 'Brightness control only on Windows' path is gone",
          "Brightness control only on Windows\"" not in bright_src
          or "Windows-only" in bright_src,
          "the non-Windows answer must still be an honest failure")

    # ------------------------------------------------------------------
    # 2. The audio module itself, driven for real on Windows.
    #
    #    Skipped where there is no audio endpoint (CI, a headless box) rather
    #    than asserted vacuously - a check that passes because it did nothing
    #    is the kind of green that hid this bug for as long as it did.
    # ------------------------------------------------------------------
    print()
    if sys.platform != "win32":
        print("  skip audio readback — not Windows")
    else:
        before = audio.get_volume()
        if before is None:
            print("  skip audio readback — no readable default endpoint here")
        else:
            check("the volume can be read at all", isinstance(before, int),
                  "the module exists so the level can be confirmed, not just set")
            # Ask for a value, then confirm the ENDPOINT agrees.
            target = 25 if before != 25 else 70
            res = audio.set_volume(target)
            after = audio.get_volume()
            check(f"setting the volume to {target}% reports success",
                  res.get("success") is True,
                  f"got {res!r}")
            check(f"the endpoint actually reads back {target}%",
                  after is not None and abs(after - target) <= 1,
                  f"asked for {target}%, endpoint reports {after}% — a setter "
                  f"that does not read back is how the original defect hid")
            # Boundaries: 0 and 100 are the ones a clamp gets wrong.
            for edge in (0, 100):
                edge_res = audio.set_volume(edge)
                edge_read = audio.get_volume()
                check(f"the boundary value {edge}% round-trips",
                      edge_res.get("success") is True
                      and edge_read is not None and abs(edge_read - edge) <= 1,
                      f"got {edge_res!r} then read {edge_read}")
            # Restore, so running the check does not change the user's level.
            audio.set_volume(before)
            check("the original level was restored",
                  audio.get_volume() is not None
                  and abs((audio.get_volume() or -1) - before) <= 1,
                  "a check that leaves the machine changed is a check that "
                  "gets run once")

    # ------------------------------------------------------------------
    # 3. A failure must be reported as one. Driven through the real method.
    # ------------------------------------------------------------------
    print()
    sc = system_controls.SystemControls()

    async def _brightness_fails_cleanly():
        """Brightness is unusable on most external displays — and must say so
        rather than claim success, which is the whole point."""
        if sys.platform != "win32":
            return None
        r = await sc.set_brightness(50)
        return r

    bright = asyncio.run(_brightness_fails_cleanly())
    if bright is not None:
        check("set_brightness reports the display's real answer",
              isinstance(bright, dict) and "success" in bright,
              f"got {bright!r}")
        if bright.get("success") is False:
            err = str(bright.get("error", ""))
            check("the brightness failure explains itself",
                  len(err) > 10 and "$ErrorActionPreference" not in err
                  and "Add-Type" not in err,
                  "the message must be a diagnosis, not a copy of the script: "
                  f"got {err[:120]!r}")

    # ------------------------------------------------------------------
    # 4. Empty content is a VALUE for write_file, and only for write_file.
    # ------------------------------------------------------------------
    print()
    import backend.skills.registry as reg
    d = reg.skill_registry.get("write_file")
    check("write_file declares that empty content is allowed",
          "content" in (d.parameters or {}).get("x-allow-empty", []),
          "without the declaration an empty write is refused as a missing arg")

    _given, err = d.normalise({"path": "probe.txt", "content": ""})
    check("an empty file can be requested", not err,
          f"the live turn asked for a zero-byte file and was refused: {err!r}")
    _given, err = d.normalise({"path": "", "content": "hi"})
    check("an empty PATH is still refused", bool(err),
          "an empty path is genuinely missing, and the fix must not have "
          "loosened that too")

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all system-control checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
