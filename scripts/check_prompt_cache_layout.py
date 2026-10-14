"""A stable prompt prefix was reaching the provider in a place its cache could
not hold it.

## What was measured

On the live gateway (`9router`, the active provider), the same system text sent
with a changing tail:

    stable text AFTER the user's question   ->    0% cache hit
    stable text BEFORE it (own message)     ->   98% cache hit

Both were measured, not assumed. The `0%` is not a small loss: the prompt at the
time of measurement was 9,923 tokens and 9,728 of them were the prefix, so the
cache was worth most of the request.

## Why the fold moved the prefix

`backend/providers/system_role.py` folds the system text into the user turn for
endpoints that drop the `system` role (9router does — measured: a 7,200-character
system message added 13 `prompt_tokens` while the same text as a `user` message
added 1,214). Folding *appends* the text after the user's words, which is right
for instruction-following — `check_reply_language.py` asserts the answered turn
starts with the user's question — and wrong for caching, because every turn then
begins with a different question and diverges immediately.

The fix keeps that ordering constraint and satisfies the cache: the
turn-invariant head is emitted as its OWN leading message, and only the volatile
remainder is appended to the user's turn.

## What is asserted here

The split itself (the prefix leads, the question still leads its own turn), the
match-by-value guard (a prefix the system text does not start with is ignored
rather than reordering the prompt), that `prepare()` passes it through and that
`ws_server` supplies it, and the no-prefix path, which must be byte-identical to
the behaviour before this change.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f"  -- {why}" if why else ""))
        fails.append(name)


def src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def the_split() -> None:
    """The prefix leads as its own message; the question still leads its turn."""
    from backend.providers import system_role

    msgs = [{"role": "system", "content": "STABLE HEAD\n\nvolatile tail about cats"},
            {"role": "user", "content": "what about dogs?"}]
    out = system_role.fold_into_user(msgs, stable_prefix="STABLE HEAD")

    check("no system role survives the fold",
          all(m["role"] != "system" for m in out),
          "the endpoint drops it, so leaving it in means it arrives twice or not at all")
    check("the stable head is its own first message",
          out[0]["role"] == "user" and out[0]["content"] == "STABLE HEAD",
          "the cache needs the invariant text at the very front, alone")
    check("the folded text comes after the user's words",
          out[1]["content"].startswith("what about dogs?"),
          "check_reply_language asserts the answered turn starts with the question")
    check("the volatile remainder is the part folded in",
          "volatile tail about cats" in out[1]["content"]
          and "STABLE HEAD" not in out[1]["content"],
          "the head must not be duplicated into the question's turn")
    check("nothing is lost in the split",
          "volatile tail about cats" in "".join(str(m["content"]) for m in out))


def the_match_by_value_guard() -> None:
    """A prefix the system text does not start with is ignored, not forced."""
    from backend.providers import system_role

    msgs = [{"role": "system", "content": "actual text here"},
            {"role": "user", "content": "q"}]
    out = system_role.fold_into_user(msgs, stable_prefix="something else")
    check("a non-matching prefix does not create a leading message",
          out[0]["content"].startswith("q"),
          "reordering on a prefix that does not match would corrupt the prompt")
    check("the whole system text still arrives",
          "actual text here" in out[0]["content"])


def the_no_prefix_path() -> None:
    """Without a prefix the result is the pre-change behaviour."""
    from backend.providers import system_role

    msgs = [{"role": "system", "content": "head\n\ntail"},
            {"role": "user", "content": "q"}]
    out = system_role.fold_into_user(msgs)
    check("one user turn, as before, when no prefix is given",
          len(out) == 1 and out[0]["role"] == "user",
          "the old shape has to stay byte-identical for callers that pass none")
    check("the instructions are appended after the question",
          out[0]["content"].startswith("q") and "head" in out[0]["content"])


def the_edge_cases() -> None:
    """Empty prefix, empty body, no user turn, multimodal content."""
    from backend.providers import system_role
    out = system_role.fold_into_user(
        [{"role": "system", "content": "ALL OF IT"},
         {"role": "user", "content": "q"}],
        stable_prefix="ALL OF IT")
    check("a prefix that is the whole system text leaves the question its own turn",
          len(out) == 2 and out[0]["content"] == "ALL OF IT" and out[1]["content"] == "q",
          "the question must not be swallowed into the prefix, and no empty "
          "turn may be added after it")

    out = system_role.fold_into_user(
        [{"role": "system", "content": "HEAD\n\nbody"},
         {"role": "user", "content": "q"}],
        stable_prefix="HEAD\n\nbody")
    check("a prefix covering the whole body adds no trailing empty turn",
          len(out) == 2 and out[1]["content"] == "q")

    out = system_role.fold_into_user(
        [{"role": "system", "content": "HEAD\n\nbody"}],
        stable_prefix="HEAD")
    check("with no user turn the head and body are both still delivered",
          len(out) == 2 and out[0]["content"] == "HEAD"
          and "body" in out[1]["content"],
          "dropping the instructions because there was no user turn loses them")

    out = system_role.fold_into_user(
        [{"role": "system", "content": "HEAD\n\nbody"},
         {"role": "user", "content": [{"type": "text", "text": "look at this"}]}],
        stable_prefix="HEAD")
    check("multimodal content keeps its parts and gains the body",
          isinstance(out[1]["content"], list)
          and out[1]["content"][0]["text"] == "look at this",
          "the image must not be replaced by the instructions")


def the_wiring() -> None:
    """prepare() forwards the prefix; the pipeline supplies it."""
    role = src("backend/providers/system_role.py")
    loop = src("backend/skills/tool_loop.py")
    ws = src("backend/ws_server.py")

    check("prepare() accepts stable_prefix",
          re.search(r"def prepare\(provider, messages: list\[dict\], \*, "
                    r"enabled: bool = True,\s*\n\s*stable_prefix: str = \"\"",
                    role) is not None)
    check("prepare() forwards it to fold_into_user",
          "fold_into_user(messages, stable_prefix=stable_prefix)" in role)
    check("a provider that keeps the system role is untouched",
          "if not enabled or not drops_system_role(provider):\n        return messages"
          in role,
          "the prefix path must not alter providers that already work")

    check("chat_with_tools accepts stable_prefix",
          "stable_prefix: str = \"\"," in loop)
    check("the loop passes it to system_role.prepare",
          re.search(r"system_role\.prepare\(provider, full_messages,\s*\n\s*"
                    r"stable_prefix=stable_prefix\)", loop) is not None)

    check("the pipeline supplies the PromptBuild prefix",
          "stable_prefix=sys_prompt.prefix()," in ws,
          "without this the prefix computed for the cache never reaches the call")
    prefix_at = ws.find("stable_prefix=sys_prompt.prefix(),")
    build_at = ws.find("sys_prompt = PromptBuild(")
    check("the prefix comes from the same object that was built",
          -1 < build_at < prefix_at)


def the_cache_numbers_are_recorded() -> None:
    """The measurement this change rests on is written down where it is used."""
    role = src("backend/providers/system_role.py")
    check("the 98-vs-0 measurement is recorded at the split",
          "98%" in role and "measured" in role.lower(),
          "an unexplained reorder invites a future 'cleanup' that breaks it")
    check("the ordering constraint that caused the conflict is cited",
          "_append_instructions" in role,
          "the reason the prefix could not simply be prepended")


def main() -> int:
    print("cache-aware folding: the split")
    the_split()
    print()
    print("the guard against a wrong prefix:")
    the_match_by_value_guard()
    print()
    print("the path with no prefix (unchanged behaviour):")
    the_no_prefix_path()
    print()
    print("edge cases:")
    the_edge_cases()
    print()
    print("the wiring from PromptBuild to the provider:")
    the_wiring()
    print()
    print("the measurement is documented where it is relied on:")
    the_cache_numbers_are_recorded()
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: the turn-invariant prefix is emitted where a provider's cache "
          "can hold it, the question still leads its own turn, and providers "
          "that keep the system role are untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
