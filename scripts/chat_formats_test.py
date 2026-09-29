"""create / edit / rename / move / copy / delete across real-world file formats,
driven through the RUNNING app's chat, like the chat page does.

Formats are the ones an office and a development workspace actually contain:

  office      .txt .md .csv .json .xml .yaml .html .log
  desktop     .cs .cpp .h .rc .sln .bat .ps1 .ini
  web         .ts .tsx .css .scss .vue .sql .env
  mobile      .dart .kt .swift .gradle .plist .java

Each case runs the same six steps on ONE file, so a failure names both the
operation and the format. The filesystem is the judge, not the reply text.

Approvals are answered the way the card's "Allow once" would, using the id taken
from the event (not from `approvals.list`, whose head may be stale).

Usage (the app must be running):
    python-bundle\\python.exe -s scripts\\chat_formats_test.py [--only .dart]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

URL = "ws://127.0.0.1:9876"
WS_ROOT = r"E:\Kunoir\Codeground\AgentWorkSpace"
FOLDER = "format_lab"

_id = [0]

def nid() -> int:
    _id[0] += 1
    return _id[0]

def safe(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")

# (extension, one-line content, human label)
CASES = [
    # ---- office ---------------------------------------------------------
    (".txt",    "Q3 review notes",                          "plain text memo"),
    (".md",     "# Release Notes\n\n- item one",            "markdown doc"),
    (".csv",    "name,role,total\nAna,Lead,120",            "spreadsheet export"),
    (".json",   '{"client":"ACME","total":4200}',           "config / data"),
    (".xml",    "<invoice><total>99</total></invoice>",     "structured doc"),
    (".yaml",   "service: billing\nreplicas: 3",            "config file"),
    (".html",   "<h1>Report</h1>",                          "web page"),
    (".log",    "2026-09-28 INFO start",                    "log file"),
    # ---- desktop ---------------------------------------------------------
    (".cs",     "public class Order {}"                     , "C# class"),
    (".cpp",    "#include <vector>\nint main(){return 0;}",  "C++ source"),
    (".h",      "#pragma once\nvoid Run();",                 "C/C++ header"),
    (".sln",    "Microsoft Visual Studio Solution File",     "VS solution"),
    (".bat",    "@echo off\r\necho build",                  "batch script"),
    (".ps1",    "Write-Host 'deploy'",                      "PowerShell script"),
    (".ini",    "[server]\nport=8080",                      "INI settings"),
    # ---- web -------------------------------------------------------------
    (".ts",     "export const total = (n: number) => n * 2;", "TypeScript"),
    (".tsx",    "export const Card = () => <div/>;",        "React component"),
    (".css",    ".btn { color: #3380FF; }",                 "stylesheet"),
    (".scss",    "$brand: #3380FF;\n.btn { color: $brand; }", "Sass"),
    (".vue",    "<template><div/></template>",              "Vue SFC"),
    (".sql",    "SELECT id, total FROM orders;",            "SQL script"),
    (".env",    "API_URL=http://localhost:9876",            "env file"),
    # ---- mobile ----------------------------------------------------------
    (".dart",   "class Order { final int total; }",         "Flutter/Dart"),
    (".kt",     "data class Order(val total: Int)",         "Kotlin/Android"),
    (".swift",  "struct Order { let total: Int }",          "Swift/iOS"),
    (".gradle", "dependencies { implementation 'x:y:1' }",  "Gradle build"),
    (".plist",  "<plist><dict/></plist>",                   "iOS plist"),
    (".java",   "public class Order { int total; }",        "Java/Android"),
]

def path_of(ext: str, name: str) -> str:
    return f"{FOLDER}/{name}{ext}"

def abs_of(rel: str) -> str:
    return os.path.join(WS_ROOT, rel.replace("/", os.sep))

def exists(rel: str) -> bool:
    return os.path.exists(abs_of(rel))

def read(rel: str):
    try:
        with open(abs_of(rel), "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None

async def rpc(ws, method, params, timeout=420.0):
    rid = nid()
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "method": method, "params": params}))
    events = []
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("id") == rid:
            return msg, events
        if msg.get("method"):
            events.append(msg)

async def ask(ws, text, approve=True):
    """One chat turn, then answer every approval it raised.

    A gated turn ENDS when it asks, so answering is a separate call. The turn
    can raise more than one request (the model retries a tool inside the same
    turn), and each has its own id — answering only the first left the rest
    pending and a working step looked like a failure.

    Ids come from the event, never from the head of `approvals.list`, whose
    oldest entry may be a stale request from an earlier case.
    """
    msg, events = await rpc(ws, "chat.send", {"message": text})
    aids, tools = [], []
    for ev in events:
        p = ev.get("params") or {}
        if ev.get("method") == "action.approvalRequest" and p.get("approval_id"):
            aids.append(p["approval_id"])
        elif ev.get("method") in ("chat.toolCall", "tool.call"):
            tools.append(p)

    if approve:
        for aid in aids:
            try:
                await rpc(ws, "action.approve", {"approvalId": aid})
            except Exception as e:  # noqa: BLE001
                print(f"    (could not approve {aid}: {safe(e)[:60]})")

    res = msg.get("result") or {}
    return (res.get("response") or ""), tools, aids

def reset_folder(folder_abs: str):
    import shutil
    shutil.rmtree(folder_abs, ignore_errors=True)
    os.makedirs(folder_abs, exist_ok=True)

async def ask_until(ws, text, done, retries=2):
    """Ask, approve, and re-ask if the filesystem still disagrees.

    A gated action runs when it is approved, but the model may only realise that
    on a later turn. Without this a correct approval is scored as a failure
    purely because the test looked too early.
    """
    for attempt in range(retries + 1):
        reply, tools, aids = await ask(ws, text if attempt == 0 else
                                       f"{text} (do it now, it is approved)")
        if done():
            return True
    return False

async def run_case(ws, ext, content, label, results):
    name = f"sample_{ext.lstrip('.')}"
    orig = path_of(ext, name)
    renamed = path_of(ext, name + "_renamed")
    # The file carries the RENAMED name by the time it is moved, so the moved
    # and copied paths have to be built from that name, not the original. The
    # first version expected the pre-rename name and reported a working move as
    # a failure.
    moved = f"{FOLDER}/moved/{name}_renamed{ext}"
    copied = f"{FOLDER}/copied/{name}_renamed_copy{ext}"

    row = {"ext": ext, "label": label, "steps": {}}
    print(f"\n=== {ext:<8} {label}")

    # 1. CREATE -- write the file with real content for the format.
    ok = await ask_until(
        ws,
        f"Create a folder named {FOLDER} if needed, then write a file at {orig} "
        f"with exactly this content:\n{content}",
        lambda: exists(orig))
    row["steps"]["create"] = ok
    print(f"    create  {'ok ' if ok else 'FAIL'}  {orig}")

    # 2. EDIT -- change the content of the existing file.
    marker = "EDITED-MARKER-42"
    if ok:
        ok2 = await ask_until(
            ws, f"Overwrite the file {orig} so its content is exactly: {marker}",
            lambda: marker in (read(orig) or ""))
        row["steps"]["edit"] = ok2
        print(f"    edit    {'ok ' if ok2 else 'FAIL'}  "
              f"content now {safe((read(orig) or '')[:40])!r}")
    else:
        row["steps"]["edit"] = False

    # 3. RENAME -- same folder, new filename.
    if exists(orig):
        ok3 = await ask_until(
            ws, f"Rename the file {orig} to {renamed}",
            lambda: exists(renamed) and not exists(orig))
        row["steps"]["rename"] = ok3
        print(f"    rename  {'ok ' if ok3 else 'FAIL'}  -> {renamed}")
    else:
        row["steps"]["rename"] = False

    # 4. MOVE -- a different folder, same filename (already renamed).
    if exists(renamed):
        ok4 = await ask_until(
            ws,
            f"Create the folder {FOLDER}/moved/ if needed, then move the file "
            f"{renamed} into it keeping its current filename.",
            lambda: exists(moved) and not exists(renamed))
        row["steps"]["move"] = ok4
        print(f"    move    {'ok ' if ok4 else 'FAIL'}  -> {moved}")
    else:
        row["steps"]["move"] = False

    # 5. COPY -- original must survive.
    if exists(moved):
        ok5 = await ask_until(
            ws,
            f"Create the folder {FOLDER}/copied/ if needed, then copy the file "
            f"{moved} to {copied}.",
            lambda: exists(copied) and exists(moved))
        row["steps"]["copy"] = ok5
        print(f"    copy    {'ok ' if ok5 else 'FAIL'}  -> {copied} "
              f"(source kept: {exists(moved)})")
    else:
        row["steps"]["copy"] = False

    # 6. DELETE -- remove both remaining files.
    if exists(copied) or exists(moved):
        ok6 = await ask_until(
            ws, f"Delete the file {copied}. Then delete the file {moved}.",
            lambda: not exists(copied) and not exists(moved))
        row["steps"]["delete"] = ok6
        print(f"    delete  {'ok ' if ok6 else 'FAIL'}  both removed")
    else:
        row["steps"]["delete"] = False

    results.append(row)
    return row

async def main():
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    workspace_abs = os.path.join(WS_ROOT, FOLDER)
    reset_folder(workspace_abs)
    os.makedirs(os.path.join(workspace_abs, "moved"), exist_ok=True)
    os.makedirs(os.path.join(workspace_abs, "copied"), exist_ok=True)

    results = []
    async with websockets.connect(URL, max_size=64 * 1024 * 1024) as ws:
        print(f"workspace: {WS_ROOT}")
        print(f"cases: {len(CASES)} formats")
        for ext, content, label in CASES:
            if only and ext != only:
                continue
            try:
                await run_case(ws, ext, content, label, results)
            except Exception as e:  # noqa: BLE001
                print(f"    ERROR {type(e).__name__}: {safe(e)[:160]}")
                results.append({"ext": ext, "label": label, "steps": {},
                                "error": str(e)[:200]})

    # ---- summary ---------------------------------------------------------
    print(f"\n{'=' * 74}\nSUMMARY\n")
    ops = ["create", "edit", "rename", "move", "copy", "delete"]
    header = f"  {'fmt':<8} {'label':<20} " + " ".join(f"{o[:6]:<6}" for o in ops)
    print(header)
    print("  " + "-" * (len(header) - 2))
    totals = {o: 0 for o in ops}
    for r in results:
        cells = []
        for o in ops:
            v = r["steps"].get(o)
            cells.append(f"{'ok' if v else 'FAIL':<6}")
            totals[o] += 1 if v else 0
        print(f"  {r['ext']:<8} {safe(r['label'])[:20]:<20} " + " ".join(cells))
    n = len(results)
    print(f"\n  per-operation: " +
          ", ".join(f"{o} {totals[o]}/{n}" for o in ops))
    allok = all(totals[o] == n for o in ops)
    print("  RESULT:", "all formats passed all six steps"
          if allok else "SOME STEPS FAILED (see FAIL cells above)")

asyncio.run(main())
