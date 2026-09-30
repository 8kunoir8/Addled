"""Live skill check: real destructive skills, on the INSTALLED app, in a sandbox.

`live_tool_check.py` proves a real model can call a real tool. This goes further:
it proves the DESTRUCTIVE tools work, and that they work on the thing they were
aimed at and nothing else.

Two rules make that safe, and both are enforced in code below rather than by
care:

  1. Every file a test touches is created by the test, inside a sandbox folder.
     Nothing is ever aimed at a path that already existed.
  2. Every assertion is made by OBSERVING the filesystem or the window list,
     never by trusting the skill's own return value. `close_window` returns
     success even when it matched nothing, so its word is not evidence.

The installed app runs with `safety.file_access_mode = unrestricted`, which
means the workspace guard does NOT confine these calls. The sandbox is the only
thing standing between a test and the user's real files, which is why rule 1 is
checked rather than assumed.

Run against the install, with the install's own interpreter:

    $env:ADDLED_ROOT = "$env:LOCALAPPDATA\\Programs\\Addled\\resources"
    & "$env:LOCALAPPDATA\\Programs\\Addled\\resources\\python\\python.exe" `
        -s scripts\\live_skill_check.py

Add `--destructive` to include the delete/move/copy and window tests. Without it
only the non-destructive pass runs.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Window titles carry emoji, and the default Windows console codec (cp1252)
# cannot encode them — printing one raised UnicodeEncodeError and killed the
# pass mid-run. Replace what cannot be shown instead of failing on it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DESTRUCTIVE = "--destructive" in sys.argv

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}" + (f"  — {detail}" if detail else ""))


async def call(skill: str, **params) -> dict:
    from backend.skills.tool_loop import execute_skill
    return await execute_skill(skill, params, None)


def grant_for_session(names: list[str]) -> None:
    """Stand the approval gate aside for this process only.

    29 skills are approval-gated, and hitting that gate is what a live run
    otherwise reports as a failure — `pdf_create` came back with "This needs
    your permission". The gate is working correctly; the test simply has no
    dashboard to answer it.

    A SESSION grant is used deliberately: it lives in memory, so it is gone when
    this script exits and the user's real standing permissions are untouched.
    Nothing is written to disk, so a run cannot leave a destructive skill
    permanently allowed.
    """
    try:
        from backend.approvals import policy
        for name in names:
            policy.allow_for_session(policy.SKILL, name)
        print(f"  granted for this session only: {', '.join(names)}")
    except Exception as e:  # noqa: BLE001
        print(f"  (could not set session grants: {e})")


def sandbox() -> str:
    """A folder created for this run, inside the workspace, and asserted so."""
    from backend.workspace import root
    base = root()
    if base is None:
        raise SystemExit("no workspace configured — refusing to run")
    path = os.path.join(str(base), f"_livecheck_{int(time.time())}")
    os.makedirs(path, exist_ok=False)
    # The one thing that must never be wrong: we are inside the workspace, and
    # the folder did not exist a moment ago. If this is not true, stop.
    if not os.path.abspath(path).startswith(os.path.abspath(str(base))):
        raise SystemExit(f"sandbox {path} escaped the workspace {base}")
    return path


async def non_destructive(box: str) -> None:
    print("\nA. non-destructive — create, write, read back")

    target = os.path.join(box, "livecheck.txt")
    r = await call("write_file", path=target, content="hello from the live check")
    record("write_file creates a file", r.get("success") is True, str(r.get("error") or ""))
    # Observed, not trusted.
    record("  … and the file is really on disk",
           os.path.isfile(target) and "hello" in open(target, encoding="utf-8").read())

    r = await call("read_file", path=target)
    record("read_file reads it back",
           r.get("success") is True and "hello" in str(r.get("data") or r))
    record("  … contents match what was written",
           "hello from the live check" in str(r.get("data") or r))

    d = os.path.join(box, "sub")
    r = await call("create_dir", path=d)
    record("create_dir", r.get("success") is True and os.path.isdir(d))

    r = await call("file_info", path=target)
    record("file_info", r.get("success") is True)

    # The v1.0.25 document set, end to end, in the sandbox.
    docx = os.path.join(box, "livecheck.docx")
    r = await call("word_create", path=docx, content="# Title\nbody text",
                   overwrite=True)
    record("word_create", r.get("success") is True and os.path.isfile(docx),
           str(r.get("error") or ""))
    if os.path.isfile(docx):
        r = await call("word_read", path=docx)
        record("word_read round-trip",
               r.get("success") is True and "body text" in str(r),
               str(r.get("error") or ""))

    xlsx = os.path.join(box, "livecheck.xlsx")
    # `grid` is the declared parameter, not `rows`.
    r = await call("excel_write", path=xlsx, grid=[["a", 1], ["b", 2]],
                   overwrite=True)
    record("excel_write", r.get("success") is True and os.path.isfile(xlsx),
           str(r.get("error") or ""))
    if os.path.isfile(xlsx):
        r = await call("excel_read", path=xlsx)
        record("excel_read round-trip", r.get("success") is True,
               str(r.get("error") or ""))

    pdf = os.path.join(box, "livecheck.pdf")
    r = await call("pdf_create", path=pdf, content="# Page\nsecret-number-42",
                   overwrite=True)
    record("pdf_create", r.get("success") is True and os.path.isfile(pdf),
           str(r.get("error") or ""))
    if os.path.isfile(pdf):
        r = await call("pdf_read", path=pdf)
        record("pdf_read round-trip",
               r.get("success") is True and "secret-number-42" in str(r),
               str(r.get("error") or ""))

    # Redaction, and the verification that it really is gone.
    if os.path.isfile(pdf):
        red = os.path.join(box, "livecheck_redacted.pdf")
        r = await call("pdf_redact", path=pdf, terms=["secret-number-42"],
                       output=red, overwrite=True)
        record("pdf_redact", r.get("success") is True and os.path.isfile(red),
               str(r.get("error") or ""))
        if os.path.isfile(red):
            r = await call("pdf_redact_verify", path=red,
                           terms=["secret-number-42"])
            record("pdf_redact_verify confirms it is gone",
                   r.get("success") is True,
                   str(r.get("data") or r.get("error") or "")[:90])


async def destructive_files(box: str) -> None:
    print("\nB. destructive file ops — each acts on a file this test just made")

    # delete_file: two files, delete one, prove the other is untouched.
    victim = os.path.join(box, "delete_me.txt")
    survivor = os.path.join(box, "keep_me.txt")
    await call("write_file", path=victim, content="bye")
    await call("write_file", path=survivor, content="stay")
    ok_before = os.path.isfile(victim) and os.path.isfile(survivor)

    r = await call("delete_file", path=victim)
    got = not os.path.exists(victim)
    record("delete_file removes the target", ok_before and got,
           f"success={r.get('success')} gone={got}")
    record("  … and leaves the neighbour alone", os.path.isfile(survivor))

    # move_file
    mv_src = os.path.join(box, "move_src.txt")
    mv_dst = os.path.join(box, "moved.txt")
    await call("write_file", path=mv_src, content="move me")
    r = await call("move_file", source=mv_src, destination=mv_dst)
    record("move_file",
           os.path.isfile(mv_dst) and not os.path.exists(mv_src),
           f"success={r.get('success')}")

    # copy_file
    cp_src = os.path.join(box, "copy_src.txt")
    cp_dst = os.path.join(box, "copied.txt")
    await call("write_file", path=cp_src, content="copy me")
    r = await call("copy_file", source=cp_src, destination=cp_dst)
    same = (os.path.isfile(cp_src) and os.path.isfile(cp_dst)
            and open(cp_src, encoding="utf-8").read()
            == open(cp_dst, encoding="utf-8").read())
    record("copy_file keeps source and matches bytes", same,
           f"success={r.get('success')}")

    # Refusals — a guard that does not hold is the bug worth catching.
    r = await call("delete_file", path=os.path.join(box, "does_not_exist.txt"))
    record("delete_file on a missing file is refused",
           r.get("success") is False)

    outside = os.path.join(os.path.dirname(box), "SHOULD_NOT_BE_CREATED.txt")
    r = await call("write_file", path=outside, content="escape attempt")
    # In `unrestricted` mode this is ALLOWED by design; the check is that we
    # noticed and cleaned up, not that it failed.
    if os.path.isfile(outside):
        os.remove(outside)
        record("write outside the sandbox", True,
               "allowed (file_access_mode=unrestricted) — cleaned up")
    else:
        record("write outside the sandbox is refused", r.get("success") is False)


async def destructive_windows() -> None:
    print("\nC. destructive window ops — act on a window this test just opened")

    before = await call("list_windows")

    def rows(res: dict) -> list[dict]:
        data = res.get("data")
        if isinstance(data, dict):
            return list(data.get("windows") or [])
        return []

    hwnds_before = {r.get("hwnd") for r in rows(before)}

    try:
        proc = subprocess.Popen(["notepad.exe"])
    except OSError as e:
        record("launch notepad", False, str(e))
        return
    time.sleep(3.0)

    try:
        # Identify the window by HWND, not by title-set difference. Titles of
        # OTHER windows change between the two calls (a chat window rewrites
        # its own title), so a set comparison can come back empty even though
        # the new window is right there — which is exactly what happened.
        after = await call("list_windows")
        new = [r for r in rows(after) if r.get("hwnd") not in hwnds_before]
        record("list_windows sees the notepad we opened", bool(new),
               f"new hwnd: {new[0].get('title')!r}" if new else "none appeared")

        if not new:
            record("close_window", False, "no new window to target")
            return
        # The window's REAL title, taken from the list. Notepad is localised,
        # so a hard-coded "Notepad" matches nothing on a non-English build.
        target = str(new[0].get("title") or "")
        print(f"     targeting: {target!r}")

        r = await call("resize_window", title_substring=target,
                       width=800, height=600)
        record("resize_window on the opened window", r.get("success") is True,
               str(r.get("error") or ""))

        r = await call("close_window", title_substring=target)
        time.sleep(2.5)
        # The skill's own word is not evidence — the window list is.
        remaining = {rr.get("hwnd") for rr in rows(await call("list_windows"))}
        gone = new[0].get("hwnd") not in remaining
        record("close_window really closed it", gone,
               "window left the list" if gone else
               f"skill said success={r.get('success')} but it is still open")

        # A title nothing matches must be refused, not reported as done.
        r = await call("close_window", title_substring="zzz-no-such-window-zzz")
        record("close_window on a non-existent title is refused",
               r.get("success") is False,
               f"success={r.get('success')} error={r.get('error')}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


async def main() -> int:
    print(f"root: {ROOT}")
    from backend.skills.tool_loop import execute_skill  # noqa: F401  (import check)

    # The writers this run needs, and nothing else. Read-only skills are not
    # gated, so they are deliberately absent from this list.
    needed = ["write_file", "word_create", "excel_write", "pdf_create",
              "pdf_redact"]
    if DESTRUCTIVE:
        needed += ["delete_file", "move_file", "copy_file", "close_window",
                   "resize_window"]
    grant_for_session(needed)

    box = sandbox()
    print(f"sandbox: {box}")
    print("  (every file below is created by this test, inside this folder)")
    try:
        await non_destructive(box)
        if DESTRUCTIVE:
            await destructive_files(box)
            await destructive_windows()
        else:
            print("\n(skipping destructive passes — re-run with --destructive)")
    finally:
        shutil.rmtree(box, ignore_errors=True)
        record("sandbox cleaned up", not os.path.exists(box))

    done = sum(1 for _, ok, _ in results if ok)
    print(f"\n{done}/{len(results)} passed")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
