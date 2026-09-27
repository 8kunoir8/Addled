"""Bug-hunt regression checks — defects found by audit, pinned so they stay fixed.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bugfixes.py

Each check here corresponds to a real defect found by reading the code rather
than by a test. They are grouped by the failure they prevent, and every one is
written so it FAILS against the old code — a regression test that passes either
way is not a regression test.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def _asyncio_run(coro):
    """Run one coroutine to completion on a fresh loop."""
    return asyncio.run(coro)

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def run():
    # ── 1. a one-shot task's next_run is its anchor, not a no-op ternary ───
    import datetime
    from backend.tasks import recurrence

    class T:
        date = "2020-01-01"
        time = "09:00"
        recurrence = {"type": "none"}

    # The old code was `return ts if ts > after else ts` — provably `return ts`.
    # The behaviour that matters is that a PAST anchor comes back unchanged
    # (not rolled forward), because `expand_month` projects a one-shot onto its
    # calendar day and a rolled-forward value would move every one-shot to the
    # day it was asked about.
    now = time.time()
    ts = recurrence.next_run(T(), after=now)
    expected = datetime.datetime(2020, 1, 1, 9, 0).timestamp()
    check("a past one-shot returns its own anchor, not a rolled-forward time",
          abs(ts - expected) < 1, f"got {ts}, expected {expected}")
    check("and that anchor is genuinely in the past",
          ts < now, "the anchor moved into the future")

    # A future one-shot returns its anchor too.
    T2 = type("T2", (), {"date": "2099-01-01", "time": "09:00",
                         "recurrence": {"type": "none"}})
    ts2 = recurrence.next_run(T2(), after=now)
    check("a future one-shot returns its anchor",
          ts2 > now, f"got {ts2}")

    # ── 2. recall has no unreachable code after its return ────────────────
    src = Path(ROOT, "backend", "memory", "recall.py").read_text(
        encoding="utf-8", errors="replace")
    import ast
    tree = ast.parse(src)
    dead = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body[:-1]):
            # A Return that is not the last statement in its block makes
            # everything after it unreachable.
            if isinstance(stmt, ast.Return):
                dead.append(getattr(stmt, "lineno", 0))
    check("no unreachable statement follows a return in recall.py",
          not dead, f"return at line(s) {dead} is not last in its block")

    # ── 3. the swarm's branch loop has no always-true guard ───────────────
    orch = Path(ROOT, "backend", "swarm", "orchestrator.py").read_text(
        encoding="utf-8", errors="replace")
    check("the always-true 'position in outcome[finished]' guard is gone",
          'if position not in outcome["finished"]:' not in orch,
          "the dead guard is back — it can never be false")

    # ── 4. SOP trim drops by the SOP's id, not by object id() ─────────────
    sopv = Path(ROOT, "backend", "sop", "store.py").read_text(
        encoding="utf-8", errors="replace")
    check("_trim does not use id() for identity",
          "id(s) not in drop" not in sopv and "id(s) for s in" not in sopv,
          "id() is back in _trim; use the SOP's own 'id' field")

    # Behave it: over the cap, the least-used entry is the one dropped.
    from backend.sop import store as sop_store
    real_path = sop_store.path
    tmp = Path(tempfile.mkdtemp(prefix="soptrim_"))
    try:
        sop_store.path = lambda: tmp / "sops.json"
        sops = [{"id": f"s{i}", "category": "files", "uses": i,
                 "updated": f"2026-01-{i + 1:02d}", "title": f"t{i}"}
                for i in range(6)]
        sop_store._trim(sops, "files")  # cap is 40 by default → no drop
        check("a category under the cap is left alone", len(sops) == 6,
              f"{len(sops)} entries")

        # Force a small cap and check the least-used go first.
        orig_setting = sop_store._setting
        sop_store._setting = lambda k, d=None: 3 if k == "max_per_category" else d
        try:
            sops = [{"id": f"s{i}", "category": "files", "uses": i,
                     "updated": f"2026-01-{i + 1:02d}", "title": f"t{i}"}
                    for i in range(6)]
            sop_store._trim(sops, "files")
            check("over the cap, it trims to the cap", len(sops) == 3,
                  f"{len(sops)} entries")
            remaining = {s["id"] for s in sops}
            check("and keeps the most-used entries",
                  remaining == {"s3", "s4", "s5"}, str(sorted(remaining)))
        finally:
            sop_store._setting = orig_setting
    finally:
        sop_store.path = real_path
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 5. budget keeps the system prompt by role, not by identity ────────
    from backend.providers import budget
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old " * 200},
        {"role": "assistant", "content": "old " * 200},
        {"role": "user", "content": "middle " * 200},
        {"role": "assistant", "content": "middle " * 200},
        {"role": "user", "content": "LATEST"},
    ]
    kept, dropped = budget.fit_messages(msgs, limit=4000, reserve=500)
    check("a plain conversation keeps its system prompt",
          any(m.get("role") == "system" for m in kept), str(kept)[:200])
    check("and its latest turn",
          any(m.get("content") == "LATEST" for m in kept), str(kept)[:200])

    # A COPY of the system dict must survive too — the old identity test
    # (`m is not head[0]`) only worked when the same object was passed.
    copied = [dict(msgs[0])] + msgs[1:]
    kept2, _ = budget.fit_messages(copied, limit=4000, reserve=500)
    check("the system prompt survives when passed as a copy",
          any(m.get("role") == "system" for m in kept2), str(kept2)[:200])

    # ── 6. fire-and-forget tasks hold a reference ─────────────────────────
    ws = Path(ROOT, "backend", "ws_server.py").read_text(
        encoding="utf-8", errors="replace")
    check("ws_server has a task-keeping spawn helper",
          "def _spawn(" in ws and "_background_tasks" in ws,
          "the strong-reference helper is gone; tasks can be collected "
          "mid-run")
    check("the auto-speak path goes through it",
          "_spawn(_speak_reply(reply))" in ws,
          "the spoken reply can be garbage-collected before it plays")
    sched = Path(ROOT, "backend", "tasks", "scheduler.py").read_text(
        encoding="utf-8", errors="replace")
    check("the scheduler keeps its tasks alive",
          "def _spawn(" in sched and "add_done_callback" in sched,
          "a scheduled task can be collected mid-run")

    # ── 8. the CDP client must not leak a pending future on timeout ────────
    cdp_src = Path(ROOT, "backend", "browser", "cdp_client.py").read_text(
        encoding="utf-8", errors="replace")
    check("the CDP call poisons its own timeout path",
          "except (asyncio.TimeoutError, asyncio.CancelledError):" in cdp_src
          and "self._pending.pop(rid, None)" in cdp_src,
          "a timed-out CDP call leaks its future into _pending forever")
    check("the CDP reader task is stored, not fire-and-forget",
          "_reader_task" in cdp_src,
          "the reader can be garbage-collected mid-flight")
    check("the CDP reader snapshots the socket",
          "ws = self._ws" in cdp_src,
          "a concurrent close() makes the reader die on a None socket")
    check("the CDP reader does not swallow its own exit",
          "CDP reader stopped" in cdp_src,
          "a protocol error looks identical to a closed socket")

    # ── 9. web_fetch's search fallback obeys the engine gate ──────────────
    reg = Path(ROOT, "backend", "skills", "registry.py").read_text(
        encoding="utf-8", errors="replace")
    fetch_start = reg.find("async def web_fetch")
    check("web_fetch exists", fetch_start != -1, "not found")
    if fetch_start != -1:
        # The fallback block runs to the end of the function.
        fetch_body = reg[fetch_start:fetch_start + 4200]
        check("web_fetch's fallback checks the engine is not cooling down",
              "_engine_ready" in fetch_body,
              "a cooling-down engine is still hit from this path")
        check("web_fetch's fallback relevance-checks its results",
              "_looks_relevant" in fetch_body,
              "the off-topic page set is handed to the model")
        check("web_fetch's fallback records engine failures",
              "_record_engine" in fetch_body,
              "this path never contributed to the cooldown counter")

    # ── 10. the WORKING ring is not frozen at zero ────────────────────────
    from backend.character.animation import Animator, CharacterState
    an = Animator()
    an.update(CharacterState.WORKING, 0.016)
    a1 = an.progress_angle
    an.update(CharacterState.WORKING, 0.016)
    a2 = an.progress_angle
    check("the WORKING ring moves when no progress is reported",
          a1 != a2,
          f"frozen at {a1} — the ring is invisible")
    an.set_progress(0.5)
    an.update(CharacterState.WORKING, 0.016)
    check("and it is exactly determinate when progress IS reported",
          abs(an.progress_angle - 180.0) < 0.001, str(an.progress_angle))

    # ── 11. knowledge-graph subject matching is Unicode-aware ─────────────
    import sqlite3
    from backend.memory.knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph()
    kg._conn = sqlite3.connect(":memory:")
    kg._conn.execute("CREATE TABLE triples (id INTEGER PRIMARY KEY, "
                     "subject TEXT, relation TEXT, object TEXT, ts REAL)")
    kg._conn.executemany(
        "INSERT INTO triples (subject, relation, object, ts) VALUES (?,?,?,?)",
        [("\u0130stanbul plan\u0131", "is", "a city", 1.0),
         ("API_KEY", "is", "a secret", 2.0),
         ("CAF\u00c9 list", "is", "a list", 3.0)])
    kg._conn.commit()
    check("an ASCII subject still matches regardless of case",
          len(kg.lookup(subject="api_key")) == 1, "ascii match broke")
    check("a non-ASCII subject matches (SQL LIKE/lower cannot do this)",
          len(kg.lookup(subject="\u0130stanbul")) == 1,
          "Unicode subject unfindable — match must happen in Python")
    check("an accented subject matches case-insensitively",
          len(kg.lookup(subject="caf\u00e9")) == 1,
          "accented ASCII-uppercase subject unfindable")
    check("a non-matching subject still returns nothing",
          kg.lookup(subject="nothing-like-this") == [],
          "the filter stopped filtering")

    # ── 12. a destructive skill cannot run without approval ───────────────
    from backend.skills.registry import skill_registry as _reg
    _reg._register_all()

    d_skill = _reg.get("delete_file")
    w_skill = _reg.get("write_file")
    r_skill = _reg.get("read_file")
    check("delete_file is registered as needing approval",
          bool(d_skill and d_skill.requires_approval), "flag missing")
    check("write_file is registered as needing approval",
          bool(w_skill and w_skill.requires_approval), "flag missing")
    check("a safe skill does not need approval",
          bool(r_skill) and not r_skill.requires_approval,
          "read_file should not be gated")

    # The flag must actually be ENFORCED, not just declared. With no approval
    # UI answering, a gated skill must refuse and leave the target untouched.
    import backend.actions.executor as _ex
    _saved_wait = _ex.APPROVAL_WAIT_S
    _ex.APPROVAL_WAIT_S = 0.3
    _tmp = Path(tempfile.mkdtemp(prefix="gate_"))
    try:
        _victim = _tmp / "keep-me.txt"
        _victim.write_text("important", encoding="utf-8")
        _res = _asyncio_run(_reg.execute("delete_file", {"path": str(_victim)}))
        check("a gated delete does not run without approval",
              _res.success is False, str(_res)[:200])
        check("and the file is still there", _victim.exists(),
              "delete_file ran ungated")
        check("and the refusal says approval is needed",
              "approval" in str(_res.error).lower()
              or (isinstance(_res.data, dict)
                  and _res.data.get("requires_approval")),
              str(_res)[:200])
    finally:
        _ex.APPROVAL_WAIT_S = _saved_wait
        import shutil as _sh
        _sh.rmtree(_tmp, ignore_errors=True)

    # ── 13. verify_code refuses a destructive command ─────────────────────
    from backend.codemode import verify as _verify
    _vres = _asyncio_run(_verify.run_verification(
        str(Path(ROOT)), command="format C:"))
    check("verify_code refuses a destructive command",
          _vres.get("ran") is False and _vres.get("ok") is False,
          str(_vres)[:200])
    check("and says why", "destructive" in (_vres.get("reason") or "").lower(),
          str(_vres)[:200])

    # ── 14. fact ids are never reused after the cap rotates ───────────────
    from backend.memory import facts as _facts
    _saved_paths = (_facts.FACTS_PATH, _facts._COUNTER_PATH)
    _ftmp = Path(tempfile.mkdtemp(prefix="facts_"))
    try:
        _facts.FACTS_PATH = _ftmp / "facts.json"
        _facts._COUNTER_PATH = _ftmp / "facts_counter.json"

        # Write past a tiny cap and prove no id is handed out twice.
        import backend.config as _cfg
        _real_get = _cfg.config.get
        def _fake_get(section, key=None, default=None):
            if section == "memory" and key == "facts_max":
                return 3
            return _real_get(section, key, default) if key is not None \
                else _real_get(section, default=default)
        _cfg.config.get = _fake_get
        try:
            ids = []
            for i in range(8):
                f = _facts.add_fact(f"fact number {i} unique text")
                if f:
                    ids.append(f["id"])
            check("every fact got an id", len(ids) == 8, str(ids))
            check("no fact id was ever reused",
                  len(set(ids)) == len(ids), f"ids={ids}")
            check("ids are monotonic",
                  ids == sorted(ids), f"ids={ids}")
            # The surviving store is capped, so the ids kept are the newest.
            live = {f["id"] for f in _facts.get_facts(limit=100)}
            check("the newest facts are the ones kept",
                  live == set(ids[-3:]), f"live={sorted(live)}")
        finally:
            _cfg.config.get = _real_get
    finally:
        _facts.FACTS_PATH, _facts._COUNTER_PATH = _saved_paths
        import shutil as _sh
        _sh.rmtree(_ftmp, ignore_errors=True)

    # ── 15. copy/move refuse to clobber by default ───────────────────────
    from backend.actions.file_ops import FileOps
    import backend.workspace as _ws
    _wtmp = Path(tempfile.mkdtemp(prefix="fops_"))
    _saved_resolve = _ws.resolve
    try:
        # Bind the guard to the temp folder so the probe stays contained.
        _ws.resolve = lambda p: (Path(p).resolve(), "")
        fo = FileOps()
        src = _wtmp / "src.txt"
        dst = _wtmp / "dst.txt"
        src.write_text("new", encoding="utf-8")
        dst.write_text("PRECIOUS", encoding="utf-8")

        res = _asyncio_run(fo.copy(str(src), str(dst)))
        check("copy refuses an existing destination by default",
              res.get("success") is False, str(res)[:200])
        check("and the existing file is untouched",
              dst.read_text(encoding="utf-8") == "PRECIOUS",
              "the destination was overwritten")

        res2 = _asyncio_run(fo.copy(str(src), str(dst), overwrite=True))
        check("copy overwrites when explicitly asked",
              res2.get("success") is True, str(res2)[:200])
        check("and keeps a .bak of what it replaced",
              (dst.parent / "dst.txt.bak").exists(),
              "an overwrite left no way back")

        m_src = _wtmp / "m.txt"
        m_dst = _wtmp / "m_dst.txt"
        m_src.write_text("move me", encoding="utf-8")
        m_dst.write_text("KEEP", encoding="utf-8")
        mres = _asyncio_run(fo.move(str(m_src), str(m_dst)))
        check("move refuses an existing destination by default",
              mres.get("success") is False, str(mres)[:200])
        check("and both files are still where they were",
              m_src.exists() and m_dst.read_text(encoding="utf-8") == "KEEP",
              "move clobbered the destination")
    finally:
        _ws.resolve = _saved_resolve
        import shutil as _sh
        _sh.rmtree(_wtmp, ignore_errors=True)

    # ── 16. a gated skill's approval path is actually awaited ─────────────
    reg_src = Path(ROOT, "backend", "skills", "registry.py").read_text(
        encoding="utf-8", errors="replace")
    check("request_approval is awaited, not left as a bare coroutine",
          "await _exec.request_approval(" in reg_src,
          "a missing await makes every gated skill fall through to 'waiting'")

    # ── 17. market script args cannot inject a command ────────────────────
    from backend.actions.terminal import TerminalExecutor as _TE
    check("TerminalExecutor has a shell-free argv runner",
          hasattr(_TE, "execute_argv"),
          "script skills build a command string, so an argument becomes code")
    mk_src = Path(ROOT, "backend", "skills", "market.py").read_text(
        encoding="utf-8", errors="replace")
    check("the script handler uses the argv runner",
          "execute_argv(" in mk_src,
          "the script path still interpolates arguments into a shell string")
    # Strip comments before looking for the dangerous pattern: the comment that
    # explains WHY it was removed quotes the old code, and a naive substring
    # check matches the explanation.
    import re as _re
    mk_code = "\n".join(
        line for line in mk_src.splitlines()
        if not line.strip().startswith("#"))
    check("and no longer builds a quoted command line",
          'f\'python "{script_path}"' not in mk_code,
          "the injectable f-string is back")
    check("the script handler does not call execute() with a built string",
          'execute(cmd' not in mk_code,
          "the old string-based execution is back")

    # Behave it: a metacharacter must arrive as DATA, not run.
    import sys as _sys
    _itmp = Path(tempfile.mkdtemp(prefix="argv_"))
    try:
        _script = _itmp / "echoargs.py"
        _script.write_text("import sys\nprint('ARGV:', sys.argv[1:])\n",
                           encoding="utf-8")
        _witness = _itmp / "PWNED.txt"
        _payload = f"$(New-Item -ItemType File -Path '{_witness}')"
        _out = _asyncio_run(_TE().execute_argv(
            [_sys.executable, str(_script), _payload],
            cwd=str(_itmp), timeout=30))
        check("a shell metacharacter is passed through as a literal argument",
              "$(New-Item" in (_out.get("stdout") or ""),
              str(_out)[:200])
        check("and it did NOT execute",
              not _witness.exists(),
              "the injected command ran — arbitrary code execution")
    finally:
        import shutil as _sh
        _sh.rmtree(_itmp, ignore_errors=True)

    # ── 18. a GitHub file name cannot escape the skill folder ─────────────
    check("market sanitises the filenames it writes",
          "outside its folder" in mk_src and "suspicious skill file name" in mk_src,
          "f['name'] from the API is used as a path with no containment")

    # ── 19. session summaries are addressed by a stable id ────────────────
    from backend.memory import session_summary as _ss
    _saved_sum_path = _ss.SUMMARIES_PATH
    _stmp = Path(tempfile.mkdtemp(prefix="summ_"))
    try:
        _ss.SUMMARIES_PATH = _stmp / "session_summaries.json"
        _ids = []
        for i in range(5):
            _ss.save_session_summary(f"summary number {i} distinct")
            _ids.append(_ss.get_recent_summaries(limit=1)[0].get("id"))
        check("each summary gets a stable id",
              all(isinstance(i, int) for i in _ids), str(_ids))
        check("ids are unique and monotonic",
              len(set(_ids)) == len(_ids) and _ids == sorted(_ids), str(_ids))
        check("deleting by id removes THAT summary",
              _ss.delete_summary(_ids[1]) is True, "delete by id failed")
        _left = [s.get("summary") for s in _ss.get_recent_summaries(limit=50)]
        check("and leaves the others in place",
              not any("number 1 " in (s or "") for s in _left)
              and len(_left) == 4, str(_left))
    finally:
        _ss.SUMMARIES_PATH = _saved_sum_path
        import shutil as _sh
        _sh.rmtree(_stmp, ignore_errors=True)

    # ── 20. the embedder does not monopolise the default executor ─────────
    emb_src = Path(ROOT, "backend", "memory", "embedding.py").read_text(
        encoding="utf-8", errors="replace")
    check("embedding uses a dedicated thread pool",
          "_EMBED_POOL" in emb_src,
          "embedding queues on the loop's shared pool and can starve it")
    check("and does not pass None to run_in_executor",
          "run_in_executor(\n        None, embed_text" not in emb_src,
          "the default executor is back")

    # ── 21. concurrent journal writes must not lose an entry ──────────────
    from backend.memory import journal as _j
    import threading as _th
    _saved_jdir = _j.JOURNAL_DIR
    _jtmp = Path(tempfile.mkdtemp(prefix="journal_"))
    try:
        _j.JOURNAL_DIR = _jtmp
        # Record from several threads at once. Without the lock each call
        # re-reads the day file, so writers clobber each other and entries go
        # missing — which is exactly what swarm agents finishing together do.
        _N = 40
        _threads = [
            _th.Thread(target=_j.record, args=("user", f"turn {i}"))
            for i in range(_N)
        ]
        for t in _threads:
            t.start()
        for t in _threads:
            t.join()
        _day = _j.get_day()
        _n = len(_day.get("entries") or [])
        check(f"all {_N} concurrent journal writes survive", _n == _N,
              f"only {_n} of {_N} entries were written — a writer was lost")
        _texts = {e.get("text") for e in (_day.get("entries") or [])}
        check("and every distinct turn is present",
              len(_texts) == _N, f"{len(_texts)} distinct of {_N}")
    finally:
        _j.JOURNAL_DIR = _saved_jdir
        import shutil as _sh
        _sh.rmtree(_jtmp, ignore_errors=True)

    check("journal.record serialises its read-modify-write",
          "_JOURNAL_LOCK" in Path(ROOT, "backend", "memory",
                                  "journal.py").read_text(
                                      encoding="utf-8", errors="replace"),
          "the lock is gone; concurrent writers will lose turns again")

    # ── 7. no bare except anywhere in the backend ─────────────────────────
    import ast as _ast
    bare = []
    for p in Path(ROOT, "backend").rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        try:
            tree = _ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in _ast.walk(tree):
            if isinstance(n, _ast.ExceptHandler) and n.type is None:
                bare.append(f"{p.name}:{n.lineno}")
    check("no bare 'except:' swallows KeyboardInterrupt/SystemExit",
          not bare, str(bare))

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: bug fixes — recall, recurrence, swarm branch, SOP trim, "
          "budget, task lifetime, bare excepts, CDP leak, web_fetch gate, "
          "progress ring, Unicode lookup, tool approval, verify_code gate, "
          "fact id reuse, copy/move clobber, script-arg injection, "
          "market file containment, summary ids, embed pool, "
          "concurrent journal writes")
    return 0

if __name__ == "__main__":
    sys.exit(main())
