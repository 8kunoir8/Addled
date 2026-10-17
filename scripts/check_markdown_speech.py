#!/usr/bin/env python
"""The chat renders markdown and the voice speaks it as words.

Both are checked here because the failure mode is that ONE of them drifts.
The page and the backend implement the same grammar in different languages
(`dashboard/src/lib/markdown.ts` and `backend/markdown_speech.py`), and a voice
that speaks different text from the screen is its own bug - the two surfaces
would be telling the user different things about what the character said.

What this checks:
  1. `_speak_reply` converts through `markdown_to_speech` - the single funnel
     both auto-speak and `voice.speak` pass through.
  2. The chat renders assistant replies through `<Markdown>`, and user replies
     stay verbatim.
  3. The Python stripper removes every markdown token from a corpus.
  4. The TypeScript stripper - compiled from the real source, not a copy - AND
     the Python one produce IDENTICAL text for the whole corpus.

Step 4 is the one that matters. It is how the `[happy]` tone tag was found
being spoken aloud, and how `...(.)` and `**bold with `code` inside**` were
found disagreeing. Editing one language without the other fails here.

Run: ./python-bundle/python.exe -s scripts/check_markdown_speech.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILS.append(f"{name}{(': ' + detail) if detail else ''}")


# A corpus of the shapes a model actually writes, plus the shapes that broke
# the two implementations before: a code span inside bold, an ellipsis inside
# code, a doubled stop at the end of a line, a tone tag with nothing after it.
CORPUS = [
    "[happy] Done. **Full path:** `E:\\x\\y.txt`\n\n**Size:** 0 bytes",
    "[neutral] Volume is set to **35%** - the tool confirmed it.",
    "One caveat: that 35 is what the tool *reports it set*, not a read-back.",
    "## Heading\n\nBody text after the heading.",
    "- first item with **bold**\n- second with `code`\n- third plain",
    "1. first\n2. second",
    "```powershell\nGet-ChildItem -Recurse\nWrite-Output 'x'\n```",
    "Before the fence.\n```\necho hi\n```\nAfter the fence.",
    "See [the docs](https://example.com/page) for more.",
    "---",
    "`ReadAllBytes(...).Length` says 0.",
    "Tunggu sebentar...",
    "**bold with `code` inside**",
    "**unclosed bold",
    "`unclosed code",
    "a * b * c (multiplication, not italic)",
    "plain sentence with no markup at all",
    "[unknown_tag] is not a tone tag",
    "",
    "emoji 🦊 and ✅ keep working",
    "line one\nline two\nline three",
    "[concern] Couldn't set it. Error: Not supported // control blocked",
]

# BALANCED markup only. An unclosed `**` or backtick is not markup - there is
# no valid span to strip - and is passed through on purpose: deleting the
# character would silently edit the user's text. Checked separately below.
MARKUP = re.compile(r"\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|^#{1,6} |^\s*[-*+] ", re.MULTILINE)
# A stray marker that did NOT open a valid span, e.g. a lone `**`.
LONE = re.compile(r"\*\*|`")


def main() -> int:
    print("markdown: chat renders it, voice speaks it as words")

    # --- 1. the funnel -------------------------------------------------------
    ws = (ROOT / "backend" / "ws_server.py").read_text(encoding="utf-8", errors="replace")
    speak_fn = re.search(r"async def _speak_reply\(.*?(?=\nasync def |\ndef )", ws, re.DOTALL)
    check("_speak_reply exists", speak_fn is not None)
    if speak_fn:
        body = speak_fn.group(0)
        # A CALL, not merely the import - the import alone is a substring
        # match that passes while nothing is converted.
        check("_speak_reply converts through markdown_to_speech",
              "markdown_to_speech(text)" in body)
        # The conversion must happen BEFORE speak(), not after.
        conv = body.find("markdown_to_speech(")
        spk = body.find("await speak(")
        check("_speak_reply strips before speaking",
              conv != -1 and spk != -1 and conv < spk,
              f"convert at {conv}, speak at {spk}")

    # --- 2. the chat ---------------------------------------------------------
    page = (ROOT / "dashboard" / "src" / "app" / "chat" / "page.tsx").read_text(encoding="utf-8")
    check("chat page imports <Markdown>", "from '@/components/Markdown'" in page)
    check("assistant replies render through <Markdown",
          "<Markdown text={msg.content}" in page)
    # The user's own words must not be reformatted: if someone types `**` they
    # meant those characters.
    check("user replies stay verbatim",
          re.search(r"msg\.role === 'assistant' \?", page) is not None)

    # --- 3. python strips everything ----------------------------------------
    from backend.markdown_speech import markdown_to_speech, strip_emotion

    speech = [markdown_to_speech(t) for t in CORPUS]
    leftovers = [t for t, out in zip(CORPUS, speech) if MARKUP.search(out)]
    check("no markdown survives in spoken text", not leftovers,
          f"{len(leftovers)} cases left markup: {leftovers[:2]}")
    check("unclosed markup is preserved, not eaten",
          markdown_to_speech(CORPUS[13]).startswith("**unclosed bold")
          and markdown_to_speech(CORPUS[14]).startswith("`unclosed code"))
    check("code fences are dropped from speech",
          "Get-ChildItem" not in markdown_to_speech(CORPUS[6]))
    check("prose around a fence is kept",
          "Before the fence" in markdown_to_speech(CORPUS[7])
          and "After the fence" in markdown_to_speech(CORPUS[7]))
    check("inline code is kept as words",
          "ReadAllBytes(...)" in markdown_to_speech(CORPUS[10]))
    check("a real ellipsis is not shortened",
          markdown_to_speech(CORPUS[11]).endswith("sebentar..."))
    check("tone tag is not spoken",
          "[happy]" not in speech[0] and "[neutral]" not in speech[1])
    check("strip_emotion leaves an unknown tag alone",
          strip_emotion("[unknown_tag] x") == "[unknown_tag] x")

    # --- 4. the two implementations agree -----------------------------------
    # Compile the REAL TypeScript source rather than a copy, so this check
    # fails when someone edits the page's grammar and not the voice's.
    node = shutil.which("node")
    tsc = ROOT / "dashboard" / "node_modules" / "typescript" / "bin" / "tsc"
    if not node or not tsc.exists():
        check("typescript compiler available", False,
              "node or typescript not found; cannot verify parity")
        return report()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "md.ts").write_text(
            (ROOT / "dashboard" / "src" / "lib" / "markdown.ts").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [node, str(tsc), str(tmp_dir / "md.ts"), "--module", "commonjs",
             "--target", "es2020", "--outDir", str(tmp_dir), "--skipLibCheck"],
            capture_output=True, text=True, cwd=str(ROOT / "dashboard"),
        )
        out = tmp_dir / "md.js"
        if not out.exists():
            check("typescript compiles", False, proc.stdout[-400:] + proc.stderr[-400:])
            return report()

        (tmp_dir / "corpus.json").write_text(json.dumps(CORPUS), encoding="utf-8")
        runner = tmp_dir / "run.js"
        runner.write_text(
            "const m = require('./md.js');\n"
            "const fs = require('fs');\n"
            "const c = JSON.parse(fs.readFileSync(__dirname + '/corpus.json','utf8'));\n"
            "process.stdout.write(JSON.stringify(c.map(m.markdownToPlainText)));\n",
            encoding="utf-8",
        )
        run = subprocess.run([node, str(runner)], capture_output=True, text=True)
        if run.returncode != 0:
            check("typescript implementation runs", False, run.stderr[-400:])
            return report()
        ts_out = json.loads(run.stdout)

    diffs = [(i, ts_out[i], speech[i]) for i in range(len(CORPUS)) if ts_out[i] != speech[i]]
    check("chat grammar and voice grammar agree", not diffs,
          (f"{len(diffs)}/{len(CORPUS)} differ; first: TS={diffs[0][1][:70]!r} "
           f"PY={diffs[0][2][:70]!r}") if diffs else "")

    return report()


def report() -> int:
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("OK: markdown renders in chat and is spoken as words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
