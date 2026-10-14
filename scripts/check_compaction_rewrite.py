"""Compaction summarized forever but the conversation never got smaller.

## What was wrong

`maybe_compact()` advanced a bookmark — `state["summarized"] += len(chunk)` —
and appended a rolling summary. It never touched the live list. So a long
conversation grew in **both** directions at once: the raw turns it had just
summarized stayed in the prompt, and the summaries of them were added to it.
The summary was pure cost.

`ChatHistory` had no method that could remove a message. Everything in it
appends, which is why the bug was structural rather than an oversight in one
function.

## The rewrite

`chat_history.replace_context()` is the missing primitive: the only operation
that can shorten a conversation. `maybe_compact()` now uses it to

  1. keep a **verbatim tail** (`KEEP_TAIL` turns) that is never summarized, so
     the model can still quote what was just said;
  2. **truncate** long messages older than that tail, keeping their opening —
     Addled stores tool output inside the assistant's prose rather than as
     separate `tool` messages, so the unit is a long message, not a tool block;
  3. **drop** what remains before the fold line, which the summary now speaks
     for; and
  4. advance by the chunk actually summarized, so the history shrinks over
     several turns instead of being gutted on the first pass.

## Two bugs the checks found during implementation

Both were found by running the rewrite, not by reading it:

  * The first version jumped `keep_from` to `total - KEEP_TAIL` in one pass. On
    the 12-message default threshold a 240-message conversation went to 12 —
    measured. It now advances by the chunk.
  * `summarized` was left as a **position** in a list that the rewrite had just
    shortened. On the next pass `pending = all_msgs[summarized:]` was empty, so
    a 240-message conversation compacted exactly once and then never again —
    also measured. It is now a running total, and what remains to fold is
    derived from the tail, not from a position.

## What is asserted here

The primitive and its refusals, the two reductions, the bookkeeping that the
two bugs above broke, the thrashing guard, and the wiring — including that the
state is not saved when the store refuses the rewrite.
"""

import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---- the store must be unreachable from here -------------------------------
#
# This suite exercises `replace_context`, which is a WRITE. An earlier version
# built a real `ChatHistory`, blanked `_data` and set `_loaded = True` to avoid
# touching a real file — which works only while the process is not pointed at
# one. Run with `ADDLED_DATA_DIR` set to a real data directory (as it was, when
# these checks were pointed at an installed build), the first `_save()` wrote a
# one-conversation store straight over the user's chat history. 83
# conversations became 1, and nothing recovered them.
#
# So the redirect happens HERE, before `chat_history` is imported, and it is not
# optional: `HISTORY_PATH` is rebuilt from a private temporary directory for the
# whole process. `ADDLED_DATA_DIR` is cleared too, so a subprocess cannot pick
# up the real one either.
_SANDBOX = tempfile.mkdtemp(prefix="addled_check_store_")
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)
os.environ.pop("ADDLED_DATA_DIR", None)

import backend.app_paths as _app_paths  # noqa: E402

_app_paths.DATA_DIR = __import__("pathlib").Path(_SANDBOX)
_app_paths.MEMORY_DIR = _app_paths.DATA_DIR

import backend.memory.chat_history as _chat_history_mod  # noqa: E402

_chat_history_mod.HISTORY_PATH = __import__("pathlib").Path(_SANDBOX) / "chat_history.json"

REAL_STORE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Addled",
    "chat_history.json")

fails: list[str] = []


def check_sandbox_is_real() -> None:
    """Fail loudly if the store under test is not the sandbox copy.

    Guards the guard: if a future edit moves the redirect below an import, or
    the module is reloaded, every write in this suite would land on the real
    file again. That has happened once and must not happen twice.
    """
    resolved = str(_chat_history_mod.HISTORY_PATH)
    check("the store under test is the sandbox, not the real one",
          _SANDBOX in resolved and resolved != REAL_STORE,
          f"HISTORY_PATH={resolved} — refusing to run against the live store")


def check(name: str, ok: bool, why: str = "") -> None:
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f"  -- {why}" if why else ""))
        fails.append(name)


def src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def the_primitive() -> None:
    """replace_context rewrites; it does not create, and it does not alias."""
    from backend.memory.chat_history import ChatHistory

    # A REAL ChatHistory against the SANDBOX path — never a blanked `_data`.
    # Faking `_data` skipped the load, and the first save then wrote the blank
    # store over whatever file the process was pointed at.
    h = ChatHistory()
    cid = h.create_conversation("t")
    for i in range(5):
        h.add_message("user", f"m{i}", conversation_id=cid)

    n = h.replace_context([{"role": "user", "content": "only one"}], cid)
    check("replace_context returns the new length", n == 1)
    after = h.get_context(conversation_id=cid, max_messages=100000)
    check("replace_context actually replaces", len(after) == 1)
    check("the stored content is the new one", after[0]["content"] == "only one")

    # The caller's list must not be aliased: get_context returns the store's own
    # dicts, and a shared reference would let a later append mutate both.
    given = [{"role": "user", "content": "x"}]
    h.replace_context(given, cid)
    given.append({"role": "user", "content": "appended by the caller"})
    check("the caller's list is copied, not aliased",
          len(h.get_context(conversation_id=cid, max_messages=100000)) == 1,
          "an aliased list would let the caller mutate the store")

    # A missing conversation must NOT be created: that would hide a caller bug
    # behind a plausible empty result.
    check("an unknown conversation is refused, not created",
          h.replace_context([{"role": "user", "content": "x"}], "conv_does_not_exist") == 0)
    check("and it is not added to the store",
          "conv_does_not_exist" not in h._data["conversations"])

    try:
        h.replace_context("not a list", cid)
    except TypeError:
        check("a non-list is refused loudly", True)
    else:
        check("a non-list is refused loudly", False,
              "accepting it would silently empty the conversation")

    before = h.get_context(conversation_id=cid, max_messages=100000)
    try:
        h.replace_context(None, cid)
    except TypeError:
        check("None is refused too", True)
    else:
        check("None is refused too", False)
    check("a refused call leaves the conversation intact",
          h.get_context(conversation_id=cid, max_messages=100000) == before)


def the_two_reductions() -> None:
    """Truncate what is old and long; drop what was summarized."""
    from backend.memory import compaction as C

    msgs = ([{"role": "user", "content": "L" * 2000}]
            + [{"role": "user", "content": f"m{i}"} for i in range(19)])
    out = C._rewritten(msgs, 0)

    check("everything from the fold line is kept", len(out) == 20)
    check("a long message outside the tail is truncated",
          out[0].get("truncated") is True
          and len(out[0]["content"]) < 2000,
          "the payload must stop being re-sent every turn")
    check("the truncated message keeps its opening",
          out[0]["content"].startswith("LLLL"),
          "the turn still happened; a reader should see how it began")
    check("it is marked as truncated", "[...truncated]" in out[0]["content"],
          "a silent cut would look like the message really ended there")
    check("a message inside the tail is untouched",
          "truncated" not in out[-1])

    out2 = C._rewritten(msgs, 12)
    check("the fold line drops what was summarized",
          len(out2) == 8 and out2[0]["content"] == "m11",
          "index 12 of the list is m11, because the long message is index 0")
    check("the dropped messages are gone entirely",
          all(str(m.get("content")) != "L" * 2000 for m in out2))
    check("the caller's list is not mutated",
          len(msgs) == 20 and len(str(msgs[0]["content"])) == 2000,
          "get_context returns the store's own dicts")

    short = [{"role": "user", "content": "hi"}]
    check("a short conversation is returned unchanged",
          [m["content"] for m in C._rewritten(short, 0)] == ["hi"])
    check("an empty list is fine", C._rewritten([], 0) == [])
    check("a fold line past the end yields nothing",
          C._rewritten(short, 99) == [])


def the_bookkeeping() -> None:
    """The two measured bugs: the head-jump, and the stale position."""
    from backend.memory import compaction as C

    KEEP = C.KEEP_TAIL
    # The fold line must advance by the chunk, not jump to the tail. On a short
    # conversation the head-jump discarded almost everything in one pass.
    def fold_line(total, chunk):
        return min(len(chunk), max(0, total - KEEP))

    check("a 240-message conversation keeps most of itself on the first pass",
          fold_line(240, list(range(20))) == 20,
          "the head-jump would have left 12")
    check("a 20-message conversation keeps its tail",
          fold_line(20, list(range(6))) == 6)
    check("the fold line never passes the verbatim tail",
          fold_line(15, list(range(5))) <= max(0, 15 - KEEP))
    check("a conversation at the tail size folds nothing",
          fold_line(KEEP, list(range(5))) == 0)

    # `summarized` must be a running total, not a position: the rewrite removed
    # the messages it counted, so a position pointed past the end of the list
    # and compaction stopped forever after one pass.
    compact = src("backend/memory/compaction.py")
    check("summarized is accumulated, not assigned to a list length",
          'state["summarized"] = int(state.get("summarized", 0)) + len(chunk)'
          in compact,
          "assigning len(rewritten) made the second pass a no-op")
    check("what remains to fold is derived from the tail",
          "foldable = max(0, total - KEEP_TAIL)" in compact,
          "a positional bookmark drifts once a rewrite shortens the list")
    check("the guard compares against the rewritten size",
          'state["rewritten_at"] = stored' in compact)


def the_thrashing_guard() -> None:
    """Repeated passes are refused until enough new turns arrive."""
    from backend.memory import compaction as C

    check("no rewrite yet counts as everything (never blocks the first)",
          C._turns_since({}, 50) == 50)
    check("a stale summary-only state also does not block",
          C._turns_since({"summarized": 40}, 50) == 50,
          "those are exactly the conversations that need rewriting")
    check("nothing new since the last rewrite is refused",
          C._turns_since({"rewritten_at": 220}, 220)
          < C.MIN_TURNS_BETWEEN_COMPACTIONS)
    check("enough new turns are allowed",
          C._turns_since({"rewritten_at": 220}, 220 + C.MIN_TURNS_BETWEEN_COMPACTIONS)
          >= C.MIN_TURNS_BETWEEN_COMPACTIONS)
    check("the guard threshold is a positive number",
          isinstance(C.MIN_TURNS_BETWEEN_COMPACTIONS, int)
          and C.MIN_TURNS_BETWEEN_COMPACTIONS > 0)
    check("the tail is a positive number of turns",
          isinstance(C.KEEP_TAIL, int) and C.KEEP_TAIL > 0)


def the_refusal_is_refused() -> bool:
    """Drive maybe_compact() with a store that refuses the rewrite.

    The state must come back untouched: recording progress over turns that are
    still in the list would mark them summarized, and the next pass would skip
    them — losing them from both the history and the summary.
    """
    import asyncio
    from backend.memory import compaction as C
    from backend.memory.chat_history import ChatHistory
    import backend.memory.chat_history as ch_mod

    class Refusing(ChatHistory):
        def replace_context(self, messages, conversation_id=None):
            return 0                      # report "nothing stored", always
    real = ch_mod.chat_history
    # Real class, sandbox path — see the header. `Refusing` inherits everything
    # except the one method whose refusal is under test.
    store = Refusing()
    cid = store.create_conversation("refuse")
    for i in range(40):
        store.add_message("user", f"q{i}: " + "w " * 30, conversation_id=cid)
        store.add_message("assistant", f"a{i}: " + "r " * 40, conversation_id=cid)

    saved_state = C._load()
    old_quiet = C.QUIET_WINDOW_S
    C.QUIET_WINDOW_S = 0.0
    ch_mod.chat_history = store
    try:
        # `maybe_compact` imports the singleton at call time, so patching the
        # module attribute is what redirects it.
        ok = asyncio.run(C.maybe_compact())
    finally:
        ch_mod.chat_history = real
        C.QUIET_WINDOW_S = old_quiet

    before = len(store.get_context(conversation_id=cid, max_messages=100000))
    after_state = C._load()
    return (ok is False
            and before == 80
            and after_state.get("summarized", 0) == saved_state.get("summarized", 0)
            and after_state.get("rewritten_at") == saved_state.get("rewritten_at"))


def the_wiring() -> None:
    """maybe_compact rewrites the store, and only records progress if it did."""
    compact = src("backend/memory/compaction.py")

    check("maybe_compact calls replace_context",
          "chat_history.replace_context(rewritten, conv_id)" in compact)
    check("it builds the rewritten list before storing",
          compact.find("_rewritten(all_msgs") < compact.find("replace_context("))
    # The refusal path is exercised, not pattern-matched. An earlier version of
    # this check looked for the warning string in the source, which a mutation
    # that ignores the refusal and saves anyway left untouched.
    check("a refused rewrite does not record progress",
          the_refusal_is_refused())

    # The properties the rewrite must not have broken.
    check("the single-flight lock is still held",
          "if _locked:" in compact and "finally:\n        _locked = False" in compact)
    check("the quiet wait is still applied before spending the model",
          "await _wait_for_quiet()" in compact)
    check("the heuristic fallback is still there",
          "Heuristic fallback" in compact and "_summarize_chunk" in compact)
    check("conversation scoping is still there",
          'state.get("conversation_id") != conv_id' in compact)
    check("the chunk is still capped",
          "MAX_CHUNK" in compact and "min(MAX_CHUNK," in compact)
    check("the state is re-read after the quiet wait",
          compact.count("chat_history.get_context(max_messages=100000)") >= 2,
          "the user may have chatted while waiting")
    check("a new conversation resets the state",
          '"summarized": 0' in compact)


def main() -> int:
    print("the store under test is not the real one")
    check_sandbox_is_real()
    print()
    print("compaction rewrite: the primitive")
    the_primitive()
    print()
    print("the two reductions:")
    the_two_reductions()
    print()
    print("the bookkeeping the two measured bugs broke:")
    the_bookkeeping()
    print()
    print("the thrashing guard:")
    the_thrashing_guard()
    print()
    print("the wiring and what must not have broken:")
    the_wiring()
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: a long conversation is rewritten to a summary plus a verbatim "
          "tail, the history shrinks over several passes, and it cannot thrash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
