"""The dashboard's static server must serve Next's RSC payloads.

`electron/main.js` serves the exported dashboard from a plain http server. Next
writes a route's RSC payload in one shape and the client ASKS for it in another:

    on disk   <route>/__next.<route>/__PAGE__.txt      (slash — sub-routes)
    on disk   __next.__PAGE__.txt                      (dots  — the root route)
    requested <route>/__next.<route>.__PAGE__.txt      (dots  — every route)

The root route therefore always worked (its on-disk name already has the dots)
and only sub-routes 404'd. That is why it went unnoticed: nothing looked broken.
The visible cost was that every sidebar prefetch failed, which silently
downgrades Next's client-side navigation to a full page load — page routes still
returned 200, so the symptom was "navigation feels slow", not an error.

The server now maps the dot form onto the directory form. This asserts that
mapping, and — just as importantly — that it does not claim names it shouldn't:
a real file containing dots must still resolve to itself.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_dashboard_server.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

MAIN_JS = os.path.join(ROOT, "electron", "main.js")

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)


def main() -> int:
    with open(MAIN_JS, encoding="utf-8") as fh:
        src = fh.read()

    # -- the server still has the containment guard ---------------------------
    # This is the security property the file documents: a request must never
    # escape the build directory, because settings.json (every API key) is one
    # level up from it.
    check("the server checks path containment", "isInside" in src,
          "a request could escape the build directory")
    check("containment is checked after the RSC mapping too",
          src.count("isInside(") >= 3,
          "the mapped path must be contained as well, not just the raw one")

    # -- the mapping exists, with the same regex ------------------------------
    m = re.search(r"^(.*/)?\(__next\\\.\[\^/\]\+\)\\\.\(__PAGE__\|_full\|_tree\)"
                  r"\\\.txt\$", src, re.M)
    check("the RSC dot-form mapping is present",
          "dotRsc" in src and "__next\\." in src,
          "sub-route RSC payloads 404 without it")

    # Extract the regex the server actually uses, so the test cannot drift from
    # it by hardcoding its own copy. The literal sits between the slashes of
    # `const dotRsc = /pattern/`, but the pattern itself contains ESCAPED slashes
    # (`\/`), so a non-greedy match stops too early — it is taken up to the last
    # slash on that line instead.
    gm = re.search(r"const dotRsc = /(.+)/\s*$", src, re.M)
    check("the regex source is readable from main.js", gm is not None,
          "could not find `const dotRsc = /.../` in electron/main.js")
    if gm is None:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    pattern_src = gm.group(1)
    check("the mapping only claims RSC-shaped names",
          "__next" in pattern_src and "txt" in pattern_src,
          f"unexpected pattern: {pattern_src}")
    dot_rsc = re.compile(pattern_src)

    root = Path(tempfile.mkdtemp(prefix="dashsrv_"))
    (root / "chat" / "__next.chat").mkdir(parents=True)
    (root / "chat" / "__next.chat" / "__PAGE__.txt").write_text("chat-rsc")
    (root / "chat" / "__next.chat" / "_full.txt").write_text("chat-full")
    (root / "__next.__PAGE__.txt").write_text("root-rsc")
    (root / "chat" / "notes.backup.txt").write_text("real")
    (root / "chat.txt").write_text("route-txt")

    def resolve(url_path: str) -> Path:
        """The server's lookup order: direct, then the dot-form mapping."""
        direct = root / url_path.lstrip("/")
        if direct.exists():
            return direct
        hit = dot_rsc.match(url_path)
        if hit:
            cand = (root / (hit.group(1) or "").lstrip("/")
                    / hit.group(2) / (hit.group(3) + ".txt"))
            if cand.exists():
                return cand
        return direct

    # -- the case that was broken --------------------------------------------
    check("a sub-route __PAGE__ request resolves",
          resolve("/chat/__next.chat.__PAGE__.txt").read_text() == "chat-rsc")
    check("a sub-route _full request resolves",
          resolve("/chat/__next.chat._full.txt").read_text() == "chat-full")

    # -- the cases that already worked ---------------------------------------
    check("the root dot form still resolves",
          resolve("/__next.__PAGE__.txt").read_text() == "root-rsc")
    check("the slash form still resolves",
          resolve("/chat/__next.chat/__PAGE__.txt").read_text() == "chat-rsc")
    check("a plain route .txt still resolves",
          resolve("/chat.txt").read_text() == "route-txt")

    # -- and the mapping must not overreach ----------------------------------
    check("a real dotted filename is not remapped",
          resolve("/chat/notes.backup.txt").read_text() == "real",
          "a genuine file whose name contains dots must resolve to itself")
    check("an unrelated missing file stays missing",
          not resolve("/chat/missing.thing.txt").exists())
    check("a non-RSC dot name is not claimed",
          dot_rsc.match("/chat/notes.backup.txt") is None)

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: the dashboard server serves RSC payloads and nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(main())
