"""The meeting summariser: blocking, map-reduce, and honest degradation.

The logic worth guarding, and why each part is asserted rather than assumed:

  * **Blocking keeps timestamps.** The timestamps are the only anchor a reader
    has for checking a summary against the transcript. If a block loses them the
    summary can still read well and be unverifiable, which is the failure mode
    that looks like success.
  * **"The summariser did not answer" is not "nothing was found".** Conflating
    them turns a dead provider into a confident empty result. This suite keeps
    them apart because that conflation is what produced the wrong conclusion
    about the model earlier in this workstream.
  * **A partial result is reported as partial.** The plan requires it: say which
    part was summarised rather than quietly dropping the rest.

The suite uses fake providers, so it needs no model and cannot fail for reasons
that have nothing to do with the code under test.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_meeting_summarise.py
"""

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="addled_summarise_check_")
os.environ["ADDLED_DATA_DIR"] = _TMP

fails = []

def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        fails.append(label)


class _Result:
    def __init__(self, text, ok=True, error=None):
        self.ok = ok
        self.response = text
        self.error = error


class FakeProvider:
    """Answers with whatever `fn(call_number, prompt)` returns."""

    provider_id = "fake"

    def __init__(self, fn):
        self._fn = fn
        self.calls = []
        self.n = 0

    def chat(self, messages, **kwargs):
        self.n += 1
        prompt = messages[0]["content"]
        self.calls.append(prompt)

        async def go():
            return self._fn(self.n, prompt)
        return go()


def _json_part(index, prompt):
    if "PART SUMMARIES" in prompt:
        return _Result(json.dumps({
            "summary": "REDUCED.", "decisions": ["d1"],
            "actions": [{"what": "a1", "who": "Bob"}],
            "open_questions": ["q1"]}))
    return _Result(json.dumps({
        "summary": f"part {index}", "decisions": [f"d{index}"],
        "actions": [], "open_questions": []}))


_SEGS = [{"at": f"{i:02d}:00", "start": i * 60, "text": f"line {i} " + "x" * 400}
         for i in range(12)]
_TR = "\n".join(s["text"] for s in _SEGS)


def main() -> int:
    from backend.meetings import summarise as S

    print("Blocking a transcript")
    bl = S.blocks(_TR, _SEGS, block_chars=1200)
    check("a long transcript becomes several blocks", len(bl) > 1, str(len(bl)))
    check("every block carries its timestamps",
          all("[" in b["text"] for b in bl),
          "a summary from these blocks could not be checked against the transcript")
    check("each block records the span it covers",
          all(b["from"] and b["to"] for b in bl), str(bl[0]))
    check("blocks are numbered and counted",
          [b["index"] for b in bl] == list(range(1, len(bl) + 1))
          and all(b["total"] == len(bl) for b in bl))
    check("no block starts mid-word after a split",
          all(not b["text"].startswith(" ") for b in bl))

    # A single block must not be split, or a short meeting pays for a reduce it
    # does not need.
    check("a short transcript is one block",
          len(S.blocks("hello there", None)) == 1)
    check("an empty transcript is no blocks", S.blocks("", None) == [])

    # Without segments there are no timestamps to keep, but it still has to
    # split, or a wall of text becomes one oversized prompt.
    plain = S.blocks("para one. " * 400, None, block_chars=500)
    check("a transcript with no segments is still blocked", len(plain) > 1,
          str(len(plain)))

    print("\nMap-reduce")
    p = FakeProvider(_json_part)
    r = asyncio.run(S.summarise(_TR, provider=p, segments=_SEGS,
                                block_chars=1200))
    check("a multi-block transcript summarises", r["success"] is True,
          str(r.get("error")))
    check("it counts the blocks it made", r["blocks"] == len(bl), str(r["blocks"]))
    check("it counts the parts that succeeded",
          r["parts_ok"] == r["blocks"], f"{r['parts_ok']}/{r['blocks']}")
    check("nothing is marked partial when everything worked",
          r["partial"] is False)
    check("one call per block, plus one to reduce",
          p.n == len(bl) + 1, f"{p.n} calls for {len(bl)} blocks")
    check("the reduce result is what comes back", r["summary"] == "REDUCED.",
          r["summary"])

    # A single block is the common short-meeting case and must not pay for a
    # reduce stage that would only reformat it.
    p1 = FakeProvider(_json_part)
    r1 = asyncio.run(S.summarise("a short meeting", provider=p1))
    check("a short transcript uses one call, not two", p1.n == 1, str(p1.n))
    check("and is not marked partial", r1["partial"] is False)

    print("\nA dead summariser is reported, not hidden")
    # The failure that produced the earlier wrong conclusion about the model.
    dead = FakeProvider(lambda i, pr: _Result("", ok=False, error="boom"))
    rd = asyncio.run(S.summarise(_TR, provider=dead, segments=_SEGS,
                                 block_chars=1200))
    check("a provider that errors means failure", rd["success"] is False,
          str(rd))
    # Two shapes of "no reply", and both must read as failure rather than as an
    # empty meeting. The multi-block path names how much was lost; the
    # single-block path says outright that the summariser did not answer.
    check("multi-block: the error says how much could not be summarised",
          "could be summarised" in (rd["error"] or "")
          and str(len(bl)) in (rd["error"] or ""),
          str(rd.get("error")))
    rd1 = asyncio.run(S.summarise(
        "a short meeting", provider=FakeProvider(lambda i, pr: _Result("", ok=False))))
    check("single-block: the error says the summariser did not answer",
          "did not answer" in (rd1["error"] or ""), str(rd1.get("error")))
    check("and that one is a failure too", rd1["success"] is False)
    check("no summary is invented for it", rd["summary"] == "")
    check("and no decisions are invented either",
          rd["decisions"] == [] and rd["actions"] == [])

    # Prose with no JSON is a reply that said nothing usable — a different
    # failure from no reply, and worth its own message.
    prose = FakeProvider(lambda i, pr: _Result("I think it went well."))
    rp = asyncio.run(S.summarise("x", provider=prose))
    check("a reply with no JSON is a failure", rp["success"] is False, str(rp))
    check("and says it was not usable JSON",
          "usable JSON" in (rp["error"] or ""), str(rp.get("error")))
    check("which is a different message from no reply at all",
          rp["error"] != rd["error"])

    check("an empty transcript fails without calling the model",
          asyncio.run(S.summarise("", provider=FakeProvider(_json_part)))
          ["success"] is False)
    check("no provider configured is a clear failure",
          asyncio.run(S.summarise("text", provider=object()))
          ["success"] is False)

    print("\nA part that fails is reported as partial")
    def flaky(i, pr):
        if i == 2:
            return _Result("", ok=False, error="nope")
        return _json_part(i, pr)
    p7 = FakeProvider(flaky)
    r7 = asyncio.run(S.summarise(_TR, provider=p7, segments=_SEGS,
                                 block_chars=1200))
    check("the summary still succeeds with one block missing",
          r7["success"] is True, str(r7.get("error")))
    check("it is flagged partial", r7["partial"] is True)
    check("the counts say how many worked",
          r7["parts_ok"] == r7["blocks"] - 1,
          f"{r7['parts_ok']}/{r7['blocks']}")
    check("the failed block is named",
          r7.get("failed_blocks") == [2], str(r7.get("failed_blocks")))
    check("and the error says how much was missed",
          "1 of" in (r7["error"] or ""), str(r7.get("error")))

    print("\nReducing without losing the parts")
    # A reduce that drops everything the parts found is worse than no reduce:
    # the meeting's actions would vanish at the last step.
    def empty_reduce(i, pr):
        if "PART SUMMARIES" in pr:
            return _Result(json.dumps({"summary": "merged",
                                       "decisions": [], "actions": [],
                                       "open_questions": []}))
        return _json_part(i, pr)
    p8 = FakeProvider(empty_reduce)
    r8 = asyncio.run(S.summarise(_TR, provider=p8, segments=_SEGS,
                                 block_chars=1200))
    check("a reduce that finds nothing does not erase the parts' findings",
          len(r8["decisions"]) > 0, str(r8["decisions"]))

    # A failed reduce returns the merged block results rather than nothing.
    def fail_reduce(i, pr):
        if "PART SUMMARIES" in pr:
            return _Result("", ok=False, error="reduce died")
        return _json_part(i, pr)
    p9 = FakeProvider(fail_reduce)
    r9 = asyncio.run(S.summarise(_TR, provider=p9, segments=_SEGS,
                                 block_chars=1200))
    check("a failed reduce still returns the block summaries",
          r9["success"] is True and bool(r9["summary"]), str(r9)[:200])
    check("and keeps every decision the blocks found",
          len(r9["decisions"]) == len(bl), str(len(r9["decisions"])))

    print("\nParsing what models actually return")
    wrapped = FakeProvider(lambda i, pr: _Result(
        'Sure!\n```json\n{"summary":"s","decisions":["d"]}\n```\nDone.'))
    rw = asyncio.run(S.summarise("x", provider=wrapped))
    check("JSON in a fenced block is found",
          rw["success"] and rw["summary"] == "s", str(rw)[:120])

    prose_json = FakeProvider(lambda i, pr: _Result(
        'Here you go: {"summary":"s2","decisions":["a","b"]} hope that helps'))
    rj = asyncio.run(S.summarise("x", provider=prose_json))
    check("JSON inside prose is found",
          rj["success"] and rj["decisions"] == ["a", "b"], str(rj)[:120])

    bare = FakeProvider(lambda i, pr: _Result(
        '{"summary":"s","actions":["fix the thing",{"what":"ship it","who":"Sam"}]}'))
    rb = asyncio.run(S.summarise("x", provider=bare))
    check("action items given as plain strings still become actions",
          rb["actions"][0]["what"] == "fix the thing", str(rb["actions"]))
    check("and an action with a named owner keeps it",
          rb["actions"][1]["who"] == "Sam", str(rb["actions"]))

    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all summariser checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
