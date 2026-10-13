"""Trajectory foundations — per-tool timing, and token accounting that is true.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_trajectory.py

This pins the first two phases of `PLAN-trajectory-view.md`: what a tool call
cost, and where a turn's tokens went. Both were previously thrown away — the
timing never captured, and the token split collapsed to one number that was
then stored with `out` hardcoded to 0.

The interesting checks here are the guards, not the features. Adding fields to
a shared dict is easy; the risk is the fields *around* them. `toolResults` is
read as an integer in five places, `chat_history.json` is rewritten whole on
every message, and a fabricated token count is worse than a missing one. Each
of those has a check, so a later change that breaks the invariant fails here
rather than in the product.

Behaviour is exercised for real: a tool is actually executed through
`chat_with_tools` with a stub provider, so the timing is measured rather than
searched for in the source.
"""

import asyncio
import json
import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8",
              errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Per-tool timing, measured against a real tool call.
# ---------------------------------------------------------------------------

class _StubProvider:
    """Two rounds: ask for one real command, then answer.

    `provider_id` is in NATIVE_TOOL_PROVIDERS so the loop takes the native
    function-calling path, which is where the timing lines live.
    """
    provider_id = "openai"

    def __init__(self):
        self.round = 0

    async def chat(self, messages, model=None, **kw):
        self.round += 1
        if self.round == 1:
            return type("R", (), {
                "ok": True, "response": "", "tokens_in": 100, "tokens_out": 20,
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_command",
                    "arguments": json.dumps({"command": "echo TRAJECTORY-PROBE"})}}],
                "reasoning_content": "", "error": ""})()
        return type("R", (), {
            "ok": True, "response": "done", "tokens_in": 150, "tokens_out": 40,
            "tool_calls": [], "reasoning_content": "", "error": ""})()


async def run_tool_call():
    from backend.skills import tool_loop

    res = await tool_loop.chat_with_tools(
        _StubProvider(), [{"role": "user", "content": "run it"}], "sys",
        tools={"run_command"})
    tr = res.get("tool_results") or []

    check("a tool call is recorded", len(tr) == 1,
          f"expected 1 entry, got {len(tr)}")
    if not tr:
        return res
    entry = tr[0]

    check("the tool actually ran",
          "TRAJECTORY-PROBE" in ((entry.get("result") or {}).get("stdout") or ""),
          str(entry.get("result"))[:120])

    ms = entry.get("duration_ms")
    check("the call is timed", isinstance(ms, int),
          f"duration_ms is {ms!r} — the timing capture is missing")
    check("the timing is the call's own, not a constant",
          isinstance(ms, int) and ms > 0,
          f"duration_ms={ms!r} — zero or negative means it is not measured")

    # The pre-existing keys must survive: this is an additive change.
    for key in ("tool", "success", "result", "error"):
        check(f"the existing '{key}' key is still present", key in entry,
              "adding fields must not drop the ones already read")
    return res


class _SequentialCallProvider:
    """A tool call in EACH of two rounds, then an answer.

    Two rounds is what makes this catch the `seq` bug. The loop resets
    `tool_results` to `[]` at the top of every round and drains it into the
    message, so anything derived from its length is 0 for the first call of
    EVERY round — two calls in different rounds both got `seq=0` and the order
    of the whole turn was lost. Observed live.

    A single round does NOT reproduce it: within one round the list grows, so
    `len(tool_results)` happens to give 0 then 1. The bug is specifically a
    cross-round collision, which is why this asks for one call at a time and
    why the earlier version of this test — one round, two calls — passed
    against the broken code.
    """
    provider_id = "openai"

    def __init__(self):
        self.round = 0

    def _call(self, cid: str, command: str):
        return type("R", (), {
            "ok": True, "response": "", "tokens_in": 10, "tokens_out": 5,
            "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "run_command",
                "arguments": json.dumps({"command": command})}}],
            "reasoning_content": "", "error": ""})()

    async def chat(self, messages, model=None, **kw):
        self.round += 1
        if self.round == 1:
            return self._call("c1", "echo FIRST")
        if self.round == 2:
            return self._call("c2", "echo SECOND")
        return type("R", (), {
            "ok": True, "response": "done", "tokens_in": 20, "tokens_out": 8,
            "tool_calls": [], "reasoning_content": "", "error": ""})()


async def run_multi_tool_call():
    """One call per round: they must be distinguishable, and in order."""
    from backend.skills import tool_loop
    from backend.actions import trajectory_log as tlog

    tlog.reset()
    turn_id = tlog.new_turn_id()
    await tool_loop.chat_with_tools(
        _SequentialCallProvider(), [{"role": "user", "content": "run both"}],
        "sys", tools={"run_command"}, turn_id=turn_id)

    entries = tlog.snapshot(turn_id=turn_id)["entries"]
    check("a call in each of two rounds is recorded", len(entries) == 2,
          f"got {len(entries)} steps")
    if len(entries) != 2:
        return
    seqs = [e.get("seq") for e in entries]
    check("each step has a DISTINCT seq",
          len(set(seqs)) == 2,
          f"seqs={seqs} — a per-round counter collides across rounds")
    check("seq counts across the turn, not within the round",
          sorted(seqs) == [0, 1], f"seqs={seqs}")
    check("the steps keep their execution order",
          "FIRST" in str(entries[0].get("result"))
          and "SECOND" in str(entries[1].get("result")),
          "step 0 must be the call that ran first")
    tlog.reset()


def run_timing_source():
    src = _read("backend/skills/tool_loop.py")
    check("timing uses a monotonic clock",
          "time.monotonic()" in src,
          "wall-clock time can jump mid-turn and make a duration nonsense")
    check("both ends of the call are sampled",
          src.count("time.monotonic()") >= 2,
          "one sample is not a duration")
    check("`time` is imported", re.search(r"^import time$", src, re.M) is not None,
          "NameError at the first tool call otherwise")


# ---------------------------------------------------------------------------
# 2. Token accounting — the defect, and the honesty rules.
# ---------------------------------------------------------------------------

def run_tokens(res: dict):
    from backend.ws_server import _stored_tokens, _maybe_token_usage

    check("the sum is still reported",
          isinstance(res.get("tokens"), (int, float)),
          "existing readers must keep getting a number")
    check("the input count is carried out", res.get("tokens_in") == 150,
          f"tokens_in={res.get('tokens_in')!r}")
    check("the output count is carried out", res.get("tokens_out") == 40,
          f"tokens_out={res.get('tokens_out')!r}")
    check("the sum is the sum of the parts",
          res.get("tokens") == (res.get("tokens_in") or 0) + (res.get("tokens_out") or 0),
          "the split must reconcile with the number callers already read")

    # The defect: out was the constant 0, in was handed the sum.
    stored = _stored_tokens({"tokens_in": 100, "tokens_out": 20})
    check("a reported split is stored verbatim", stored == {"in": 100, "out": 20},
          f"stored={stored!r}")
    check("stored output tokens are no longer always zero",
          stored.get("out") != 0,
          "this is the bug: `out` was hardcoded 0 for every turn")
    check("stored input tokens are not the sum",
          stored.get("in") != 120,
          "`in` used to be given tokens_in + tokens_out")

    # Honesty: an estimate must be marked, and silence must not become zero.
    est = _stored_tokens({"tokens": 30, "tokens_estimated": True})
    check("an estimated count is stored with its marker",
          est == {"out": 30, "estimated": True}, f"stored={est!r}")
    check("an unmarked estimate is not stored",
          _stored_tokens({"tokens_estimated": True}) is None,
          "an estimate with no number must not become a zero")
    check("nothing reported stores nothing",
          _stored_tokens({}) is None,
          "'not reported' must be absent, not zero")

    payload = _maybe_token_usage({"tokens_in": 100, "tokens_out": 20})
    check("the reply payload carries the split", payload.get("tokenUsage") == {"in": 100, "out": 20},
          f"payload={payload!r}")
    check("the payload stays silent when nothing was reported",
          _maybe_token_usage({}) == {},
          "an empty dict, not a zero-filled one")


def run_token_source():
    src = _read("backend/ws_server.py")
    check("output tokens are no longer hardcoded to zero",
          '"out": 0' not in src,
          "`tokens={\"in\": ..., \"out\": 0}` was the defect")

    # The estimate must come out of the REAL streamed path, not a dict this
    # test made up. A source-grep for the string would pass on a comment.
    from backend.skills.tool_loop import _streamed_answer

    class _StreamingProvider:
        provider_id = "openai"

        async def chat_stream(self, messages, model=None, **kw):
            for piece in ("A streamed ", "answer ", "with some length"):
                yield piece

    streamed = asyncio.run(_streamed_answer(
        _StreamingProvider(), [{"role": "user", "content": "hi"}]))
    check("the streamed path reports a count",
          isinstance(streamed.get("tokens"), int) and streamed["tokens"] > 0,
          f"streamed={streamed!r}")
    check("the streamed count is marked as an estimate",
          streamed.get("tokens_estimated") is True,
          "this is `len(text)//4` — a guess that must not look like a measurement")



# ---------------------------------------------------------------------------
# 3. The guards — what a later change must not break.
# ---------------------------------------------------------------------------

def run_guards():
    src = _read("backend/ws_server.py")

    # `toolResults` is read as an int in five places (two of them via `.get(..., 0)`,
    # so turning it into a list would break them silently).
    check("`toolResults` is still a count",
          re.search(r'"toolResults":\s*len\(tool_results\)', src) is not None,
          "five call sites read this as an int — see PLAN-trajectory-view.md §4.5")

    # The whole-file rewrite is why tool data must not live in chat messages.
    hist = _read("backend/memory/chat_history.py")
    check("chat history still stores only role/content/timestamp/tokens",
          'msg = {' in hist and '"content": content' in hist,
          "tool data here would be re-serialised on every message")
    check("add_message still writes a flat message",
          '"role": role' in hist and '"timestamp": time.time()' in hist,
          "the message shape is a contract the dashboard reads")

    # A tool record must not be gated on the reply succeeding.
    tl = _read("backend/skills/tool_loop.py")
    check("tool records are built at execution time",
          '"duration_ms"' in tl,
          "recording at reply time would lose a failed or timed-out call")

    # The code this change edits AROUND must still be there.
    #
    # A range edit whose boundaries are wrong does not fail loudly — it deletes
    # a neighbouring block and the file still parses and imports. That happened
    # during this change: deleting a duplicated comment took the `_forge`
    # request write with it, and only `check_tools.py` noticed. These pin the
    # neighbours, so a future edit that clips one fails here by name rather
    # than in a different suite with a puzzling message.
    for label, needle, why in (
        ("the auto-forge request write", '_forge", "request"',
         "the forge path reads this to search for the capability"),
        ("the console broadcast wiring", "_console.set_broadcast(",
         "without it the console stops updating after a reload"),
        ("the trajectory broadcast wiring", "_trajectory.set_broadcast(",
         "without it the trajectory stops updating after a reload"),
        ("the pipeline call", "run_chat_pipeline(message, params)",
         "the turn itself"),
        ("the trajectory RPC registration", 'register("trajectory.list"',
         "the dashboard reads this to replay on connect"),
    ):
        check(f"{label} survived the edit", needle in src, why)

    for label, needle in (
        ("the round-cap fallback", "_summarize_tool_results("),
        ("the token split", "_token_split(result)"),
    ):
        check(f"{label} survived the edit", needle in tl)


# ---------------------------------------------------------------------------
# 4. Against a REAL provider, when one is reachable.
# ---------------------------------------------------------------------------
#
# Everything above uses a stub, which proves the plumbing but not that a real
# provider reports a usage block in the shape this code assumes. That was the
# one load-bearing guess in the whole change: `tokens_in`/`tokens_out` are only
# populated if the adapter found a `usage` object. Here we ask the live one.
#
# Skipped, not failed, when the provider is down — a test that needs a local
# service must not turn a suite red on a machine that has none.

def _live_provider():
    """The 9router custom provider from the installed settings, or None."""
    cfg = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Addled",
                       "settings.json")
    if not os.path.exists(cfg):
        return None, "no installed settings.json"
    try:
        with open(cfg, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = next((e for e in (data.get("providers", {}).get("custom") or [])
                      if "20128" in str(e.get("base_url", ""))), None)
        if not entry or not entry.get("api_key"):
            return None, "no custom provider on :20128 with a key"
        return entry, None
    except Exception as e:  # noqa: BLE001
        return None, f"could not read settings: {e}"


def run_live_provider():
    entry, why = _live_provider()
    if not entry:
        print(f"  skip  live provider checks ({why})")
        return

    import urllib.error
    import urllib.request

    base = str(entry["base_url"]).rstrip("/")
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({
                "model": entry.get("default_model") or "Voxagent",
                "messages": [{"role": "user", "content": "Say exactly: ok"}],
                "max_tokens": 20,
            }).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {entry['api_key']}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"  skip  live provider checks (unreachable: {str(e)[:60]})")
        return

    usage = body.get("usage") or {}
    check("the live provider reports a usage block",
          bool(usage), f"keys={list(body.keys())}")

    # This is the assumption the whole token fix rests on: the adapter reads
    # these two keys, so the provider must actually send them.
    check("the live provider reports prompt_tokens",
          isinstance(usage.get("prompt_tokens"), int))
    check("the live provider reports completion_tokens",
          isinstance(usage.get("completion_tokens"), int),
          "if it does not, tokens_out is silently dropped")
    check("the live completion count is not zero for real output",
          (usage.get("completion_tokens") or 0) >= 1,
          "a non-empty answer must not report 0 output tokens")

    # And the adapter turns that into the fields the fix carries.
    from backend.ws_server import _stored_tokens

    p = _live_openai_provider(entry)
    if p is not None:
        r = asyncio.run(p.chat([{"role": "user", "content": "Say exactly: ok"}]))
        check("the adapter carries prompt_tokens into tokens_in",
              r.ok and r.tokens_in == usage.get("prompt_tokens"),
              f"adapter={r.tokens_in!r} raw={usage.get('prompt_tokens')!r}")
        check("the adapter carries completion_tokens into tokens_out",
              r.ok and r.tokens_out == usage.get("completion_tokens"),
              f"adapter={r.tokens_out!r} raw={usage.get('completion_tokens')!r}")
        # The end of the chain: what would be written to history.
        check("the split survives to the stored record",
              _stored_tokens({"tokens_in": r.tokens_in, "tokens_out": r.tokens_out})
              == {"in": r.tokens_in, "out": r.tokens_out})


def _live_openai_provider(entry):
    """The real adapter class, or None if the import shape changed."""
    try:
        from backend.providers.openai_provider import OpenAIProvider
        return OpenAIProvider({
            "base_url": entry["base_url"],
            "api_key": entry["api_key"],
            "default_model": entry.get("default_model") or "Voxagent",
            "provider_id": "openai",
        })
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 5. The edge cases — what the happy path does not show.
# ---------------------------------------------------------------------------

def run_edges():
    from backend.skills.tool_loop import _token_split

    # A provider that reports nothing must yield nothing, not zeros. A zero
    # looks like a measurement; an absent key looks like what it is.
    check("absent counts stay absent",
          _token_split({}) == {}, f"{_token_split({})!r}")
    check("a reported zero is still a reported zero",
          _token_split({"tokens_in": 0, "tokens_out": 7}) == {"tokens_in": 0, "tokens_out": 7},
          "0 in vs absent in are different truths")
    check("a partial report carries only what was reported",
          _token_split({"tokens_in": 12}) == {"tokens_in": 12})
    check("junk counts are not carried",
          _token_split({"tokens_in": "many", "tokens_out": None}) == {},
          "strings must not become token counts")
    check("a float count is coerced, not dropped",
          _token_split({"tokens_in": 10.0}) == {"tokens_in": 10})
    check("the estimate marker rides along",
          _token_split({"tokens": 30, "tokens_estimated": True})
          == {"tokens_estimated": True},
          "the marker is not itself a count")

    # Very large counts must not overflow or truncate.
    big = 2 ** 31 - 1
    check("a huge count survives intact",
          _token_split({"tokens_in": big, "tokens_out": big})
          == {"tokens_in": big, "tokens_out": big})

    # Timing: the timer must bracket the CALL, not the whole turn. `find` alone
    # is not enough — `execute_skill(` appears first as its own `def`, so the
    # call site is the occurrence AFTER the timer starts.
    src = _read("backend/skills/tool_loop.py")
    i = src.find("_tool_started = time.monotonic()")
    j = src.find("_tool_ended = time.monotonic()")
    k = src.find("execute_skill(", i)  # the call, not the definition
    check("the timer brackets the tool call, not the whole turn",
          -1 < i < k < j,
          "start must precede the call and end must follow it")

    # The timing fields must be additive on the existing record. The window is
    # taken line by line until the append's own closing brace, not by a
    # character count and not by the first `})` — `exec_result.get("data", {})`
    # contains exactly that, 160 characters in.
    lines, seen_append = [], False
    for line in src.splitlines():
        if "tool_results.append({" in line:
            seen_append = True
        if seen_append:
            lines.append(line)
            if seen_append and line.strip() in ("})", "},"):
                break
    rec = "\n".join(lines)
    check("the tool record still carries its original keys",
          all(key in rec for key in ('"tool"', '"success"', '"result"', '"error"')),
          "duration_ms is added to the existing shape, not replacing it")
    check("the record is appended, and the append still has duration_ms",
          '"duration_ms"' in rec,
          "the timing must be on the record, not just computed nearby")


# ---------------------------------------------------------------------------
# 6. The store itself — the turn grouping, and the bounds that keep it honest.
# ---------------------------------------------------------------------------

def run_store():
    from backend.actions import trajectory_log as tlog

    tlog.reset()
    turn_a, turn_b = "turn_a", "turn_b"
    s1 = tlog.record(tlog.make_step(turn_id=turn_a, tool="read_file",
                                    seq=0, arguments={"path": "x"}))
    tlog.finish(s1["id"], success=True, result={"text": "hi"}, duration_ms=10)
    s2 = tlog.record(tlog.make_step(turn_id=turn_a, tool="read_file", seq=1))
    tlog.finish(s2["id"], success=False, error="boom", duration_ms=20)
    s3 = tlog.record(tlog.make_step(turn_id=turn_b, tool="write_file", seq=0,
                                    conversation="conv-b"))
    tlog.finish(s3["id"], success=True, result="ok", duration_ms=5)

    snap = tlog.snapshot()
    check("steps group into turns",
          len(snap["turns"]) == 2, f"got {len(snap['turns'])}")
    a = next(g for g in snap["turns"] if g["turn_id"] == turn_a)
    check("a turn lists all its steps in order",
          [s["tool"] for s in a["steps"]] == ["read_file", "read_file"],
          f"{[s['tool'] for s in a['steps']]}")
    check("a turn sums the durations of its steps",
          a["duration_ms"] == 30, f"got {a['duration_ms']}")
    check("a turn counts its failures",
          a["failed"] == 1, f"got {a['failed']}")
    check("a finished step reports its status",
          snap["entries"][0]["status"] == "ok"
          and snap["entries"][1]["status"] == "failed")

    # Filtering, the two ways the dashboard asks.
    check("a snapshot can be filtered to one turn",
          len(tlog.snapshot(turn_id=turn_a)["entries"]) == 2)
    check("a snapshot can be filtered to one conversation",
          [e["tool"] for e in tlog.snapshot(conversation="conv-b")["entries"]]
          == ["write_file"])

    # The bounds. A ring that grows is the leak this is meant to avoid.
    tlog.reset()
    for i in range(tlog._MAX_ENTRIES + 40):
        tlog.record(tlog.make_step(turn_id="t", tool="read_file", seq=i))
    capped = tlog.snapshot()
    check("the store is bounded",
          capped["count"] == tlog._MAX_ENTRIES,
          f"count={capped['count']} max={tlog._MAX_ENTRIES}")
    check("the bound is reported, not silent",
          capped["capped"] is True)
    check("the OLDEST entries are the ones dropped",
          capped["entries"][0]["seq"] == 40,
          f"first seq={capped['entries'][0]['seq']} (expected 40)")

    # Redaction: a tool argument is exactly where a key gets typed.
    #
    # The fixture is assembled at runtime rather than written as a literal: a
    # real-looking key in a tracked file trips `check_secrets`, and weakening
    # THAT guard to accommodate a test would be the wrong trade. The shape is
    # what the redactor matches on, and this builds the shape.
    tlog.reset()
    fake_key = "sk-" + ("abcdefghijklmnopqrstuvwxyz"[:24])
    secret = tlog.record(tlog.make_step(
        turn_id="t", tool="web_fetch",
        arguments={"headers": {"Authorization": "Bearer " + fake_key}}))
    check("a key in the arguments is masked",
          fake_key not in str(secret.get("arguments")),
          f"{secret.get('arguments')!r}")
    real = tlog.record(tlog.make_step(
        turn_id="t", tool="web_fetch",
        arguments={"url": "https://example.com/x?token=abcdef123456"}))
    check("a token in a query string is masked",
          "abcdef123456" not in str(real.get("arguments")),
          f"{real.get('arguments')!r}")

    # A step started and never finished must remain visible as running.
    tlog.reset()
    tlog.record(tlog.make_step(turn_id="t", tool="run_command"))
    run = tlog.snapshot()["entries"][0]
    check("an unfinished step stays visible as running",
          run["status"] == "running",
          "this is the step a success-only record would lose")

    # Truncation is flagged, never silent.
    tlog.reset()
    big = tlog.record(tlog.make_step(turn_id="t", tool="read_file",
                                     arguments={"x": "A" * (tlog._FIELD_CAP + 500)}))
    check("a huge field is capped",
          len(str(big["arguments"])) <= tlog._FIELD_CAP, )
    check("a capped field says so", big["truncated"] is True)

    # One call is ONE row, however many times it is observed.
    #
    # This is the bug the console documents at length, and I reproduced it here:
    # `record` appended unconditionally, so re-recording a known id produced TWO
    # rows for one step - a tool that appears to have run twice, once stuck on
    # "running". The normal flow hid it because `finish` updates in place.
    tlog.reset()
    step = tlog.make_step(turn_id="t", tool="run_command")
    tlog.record(step)
    tlog.record(dict(step, status="ok", duration_ms=7))
    check("re-recording a known id UPDATES rather than duplicates",
          tlog.snapshot()["count"] == 1,
          f"count={tlog.snapshot()['count']} - one call must be one row")
    check("the update is visible on the stored row",
          tlog.snapshot()["entries"][0]["status"] == "ok")

    # A partial write must not blank a field an earlier write filled.
    tlog.reset()
    s = tlog.record(tlog.make_step(turn_id="t", tool="read_file",
                                   arguments={"path": "keep.txt"}))
    tlog.record({"id": s["id"], "status": "ok", "arguments": None})
    check("a later write does not blank an earlier field",
          "keep.txt" in str(tlog.snapshot()["entries"][0].get("arguments")),
          "the gate-then-terminal merge, the same rule the console has")

    # Recording must never be able to break the thing it observes. A field that
    # cannot be serialised is the realistic case; the store must swallow it and
    # return the step rather than raise into the turn.
    tlog.reset()
    class _Unserialisable:
        def __repr__(self):
            raise RuntimeError("cannot render me")

    broken = tlog.make_step(turn_id="t", tool="x")
    broken["arguments"] = _Unserialisable()
    survived = tlog.record(broken)
    check("a store that cannot render a field does not raise",
          isinstance(survived, dict), "observation must not cost the turn")

    tlog.reset()
    check("reset clears the store", tlog.snapshot()["count"] == 0)


if __name__ == "__main__":
    print("— a real tool call is timed")
    res = asyncio.run(run_tool_call())
    asyncio.run(run_multi_tool_call())
    run_timing_source()

    print("— token accounting is true, and honest about estimates")
    run_tokens(res or {})
    run_token_source()

    print("— the guards hold")
    run_guards()

    print("— the edge cases")
    run_edges()

    print("— the store groups, bounds and redacts")
    run_store()

    print("— against the live provider, if one is up")
    run_live_provider()

    if fails:
        print(f"\n{len(fails)} FAILED")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("\nAll trajectory checks passed.")
