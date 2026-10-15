"""A new question arrived carrying the old subject, and the model answered the
old subject.

## What was reported

On the chat page:

    [user] test connection
    [assistant] Connection's good - I'm here. Quick note: I never actually ran
                the skill_read you asked about...
    [user] bro one off admin management itu maksudnya yg gimana ?
    [assistant] Hasilnya bersih: ... turn ini cuma satu panggilan baca
                (`skill_read`)

The third line asks about admin management. The fourth answers about
`skill_read` again. The model never saw the new subject.

## What was measured

Against the live store at the time (`%LOCALAPPDATA%\\Addled\\chat_history.json`):

    history window: 8 msgs, 4270 chars total
    new user message: 54 chars
    => the request is 98.8% OLD topic by volume

Every *memory* block in the prompt is relevance-gated. The conversation window
was the one block that was not - and it was the largest. 7.6% of the 210 real
user turns in the store at the time are topic shifts by a plain overlap test.

That store no longer exists, so the figure cannot be reproduced. It was
destroyed on 2026-10-14, when `check_compaction_rewrite.py` was run with
`ADDLED_DATA_DIR` pointing at the real data directory and wrote a blank store
over it -- 83 conversations became 1 (see the INCIDENT section of
docs/research/addled-self-improvement-plan.md). The file kept as
`chat_history.DAMAGED-20261014.json` is that aftermath, not a copy. So
`covered_continuations()` below asserts the cases that mattered against fixed
windows, and this suite reads no live data at all.

## The signal

Not length, and not similarity. It is whether the new message carries any
content word of its own:

  * no content words ("continue", "why?", "kenapa?", "lanjutkan") -> the turn
    has no topic to stand on, so it is *about* the previous one. Keep history.
  * content words, sharing nothing with the window -> it can stand alone. It is
    a candidate new topic; withhold the stale window.

This is Arguello & Rose's "Content Contribution" feature -- introduced exactly
because term-overlap methods "wrongly classify all of these as starts of new
topics" -- and Kumar & Callan's Cat1 ("self-contained query ... demarcates a
contextual shift") vs Cat2-imp split.

## Why a plain overlap rule was rejected

`drop history when lexical overlap < 0.34` was tried first. Against the store as
it stood then, it dropped 42 of 210 turns, including:

    'The delete_file you asked for was APPROVED and ran successfully...'
    'Teach Scout10807 a standing rule: keep every review under three sentences'

The first is a permission-approval continuation, meaningless without the turn
before it. Two further checks were needed:

  1. continuation words that are shaped like content ("continue", "explain",
     "lanjutkan" all survive the stopword filter as ordinary verbs);
  2. a dependency held in the ASSISTANT's last turn -- it asked a question or is
     waiting on approval, so a terse reply answers it whatever words it uses.

With both, the same store dropped 21 turns and lost no must-keep turn.

## What is asserted here

The primitives, the four cases the design must get right, the two
false-positive classes above, the continuations the design was actually built
for (against fixed windows), and the wiring in `ws_server.run_chat_pipeline`:
that the gate is called, that it runs AFTER the recall-query expansion (which
reads the window), and that a shift keeps the last turn rather than nothing.

This suite used to replay the live store as well. That went, because the store
is the user's own file: the app rewrites it, and the verification harnesses
write their turns into it too, so an assertion reading it says as much about
what was run last as about the rule. It was the only suite here that tested
against live data -- `check_compaction_rewrite.py` sandboxes the store and
asserts it is *not* read -- and the data it depended on was destroyed.
"""

import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails: list[str] = []


def check(label: str, ok: object, detail: object = "") -> None:
    if ok:
        print(f"  ok    {label}")
    else:
        fails.append(label)
        print(f"  FAIL  {label}")
        if detail:
            print(f"        {detail}")


def src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# The window from the reported conversation, verbatim in shape.
WINDOW = [
    {"role": "user", "content": "test connection"},
    {"role": "assistant",
     "content": "Connection's good - I'm here. Quick note on the last few "
                "turns: I never actually ran the skill_read you asked about."},
    {"role": "user", "content": "Call skill_read for the skill named see"},
]

REPORTED = "bro one off admin management itu maksudnya yg gimana ?"


def primitives() -> None:
    """The signal itself."""
    from backend.memory import relevance as R

    check("content_words() is the shared tokenizer",
          R.content_words("admin management") == {"admin", "management"},
          R.content_words("admin management"))
    check("content_words() drops stopwords",
          R.content_words("the one off") == {"one", "off"},
          R.content_words("the one off"))
    check("a message with no content words is a bare follow-up",
          R.is_bare_followup("and then") and R.is_bare_followup("why?"))
    check("an empty message is a bare follow-up",
          R.is_bare_followup("") and R.is_bare_followup("   "))
    check("a message with content words is not",
          not R.is_bare_followup(REPORTED))


def the_four_cases() -> None:
    """The cases the design is specified against."""
    from backend.memory import relevance as R

    async def keep(msg: str) -> bool:
        return await R.continues_topic(msg, WINDOW)

    # (i) bare follow-ups must keep history
    for msg in ("continue", "why?", "kenapa?", "lanjutkan", "jelaskan",
                "mengapa", "and then", "?", ""):
        check(f"a bare follow-up keeps history: {msg!r}", asyncio.run(keep(msg)))

    # (ii) the reported bug: content words, new subject -> withhold
    check("the reported question does not inherit the old subject",
          asyncio.run(keep(REPORTED)) is False)

    # (iii) a long message on the same subject keeps history
    same = ("anyway the skill_read output was truncated, what did it say "
            "about see")
    check("a same-subject follow-up keeps history", asyncio.run(keep(same)))

    # (iv) a short standalone question is handled on its own merits
    check("a short standalone question does not inherit",
          asyncio.run(keep("What colour is this image?")) is False)


def the_false_positives() -> None:
    """The two classes a plain overlap rule got wrong in the real store."""
    from backend.memory import relevance as R

    # Continuation words that survive the stopword filter as ordinary verbs.
    for msg in ("continue", "explain", "lanjutkan", "jelaskan", "mengapa"):
        check(f"a continuation verb keeps history: {msg!r}",
              R.is_bare_followup(msg),
              f"content_words={R.content_words(msg)} - it looks like content "
              f"to the tokenizer, so it must be named")

    # The dependency held in the assistant's own last turn.
    approving = [{"role": "assistant",
                  "content": "Not yet - I asked to run two commands and "
                             "they're still waiting on your approval."}]
    check("a reply to an approval request keeps history",
          asyncio.run(R.continues_topic("well, i have approved it, check again",
                                        approving)) is True)

    asking = [{"role": "assistant",
               "content": "Do you want me to carry on with the second file?"}]
    check("a reply to the assistant's question keeps history",
          asyncio.run(R.continues_topic("yes", asking)) is True)

    # And a fresh question after a plain statement does NOT.
    plain = [{"role": "assistant", "content": "PONG-GAMMA"}]
    check("a fresh question after an unrelated reply does not keep history",
          asyncio.run(R.continues_topic(
              "List the files in the etc directory", plain)) is False)

    # An OFFER is not a blocked approval. This is the reported conversation: the
    # prior turn ended by offering to run something and by asking a question,
    # and the user then moved to a different subject. Treating the offer as a
    # dependency kept 3336 stale chars in the request and the bug came back.
    offering = [{"role": "assistant", "content": (
        "[neutral] Connection's good - I'm here. Quick note on the last few "
        "turns: I never actually ran the skill_read you asked for. Want me to "
        "go ahead and read references/seedance-prompt-template.md now and give "
        "you the first two lines in a code block as asked? And \"test "
        "connection\" on its own - is there something specific you'd like me to "
        "check, or was that just pinging me to see if I'd reply?")}]
    check("an unanswered OFFER does not count as a blocked approval",
          asyncio.run(R.continues_topic(REPORTED, offering)) is False,
          "this is the reported turn: the fix has to survive a prior reply "
          "that ended in a question")

    # But a real approval block still does, at the same low overlap.
    blocked = [{"role": "assistant", "content": (
        "This needs your approval before it can run: run_command. Approve it in "
        "the dashboard and I will pick up where I left off.")}]
    check("a real approval block still keeps history",
          asyncio.run(R.continues_topic(REPORTED, blocked)) is True)


def covered_continuations() -> None:
    """The continuations the rule was built for, against fixed windows.

    The two must-keep messages are verbatim from the store the research
    measured; it named them as continuations a plain overlap rule would drop,
    and recorded their overlaps there as 0.33 and 0.27. The original store is
    gone (see the module docstring), so the *windows* below are reconstructed
    to hold the dependency each turn answers -- an approval block. They are
    therefore faithful to those two turns, not to the store: this asserts the
    rule on the cases the design was built for, and does not claim to reproduce
    the store's own numbers.

    The window matters as much as the message. The swarm turn is kept by the
    approval branch, not by overlap -- its overlap against this window is 0.22,
    under the 0.34 lexical gate -- so pairing it with a bare "agent is ready"
    reply would assert the wrong thing and lose the case the research found.
    """
    from backend.memory import relevance as R

    approved_agent = [
        {"role": "user", "content": "Create a Scout10807 agent for code review."},
        {"role": "assistant", "content": (
            "This needs your approval before it can run: create_agent. Approve "
            "it in the dashboard and I will continue.")},
    ]
    approved_delete = [
        {"role": "user", "content": "run delete_file on notes.txt"},
        {"role": "assistant", "content": (
            "This needs your approval before it can run: run_delete.")},
    ]

    cases = [
        # The permission-approval continuation: meaningless without the turn
        # before it, and the plain overlap rule dropped it.
        ("The delete_file you asked for was APPROVED and ran successfully",
         approved_delete, True),
        # Part of an ongoing swarm task. Its window is a blocked approval, which
        # is what keeps it; the overlap alone is under the lexical gate.
        ("Teach Scout10807 a standing rule: keep every review under three "
         "sentences", approved_agent, True),
        # And the other direction, so a rule that kept everything could not pass
        # this by returning a constant.
        ("List the files in the etc directory",
         [{"role": "assistant", "content": "PONG-GAMMA"}], False),
    ]

    kept = sum(1 for _, _, want in cases if want)
    check("the corpus covers both outcomes",
          0 < kept < len(cases),
          f"{kept} keep / {len(cases) - kept} withhold - a corpus of one kind "
          f"cannot tell a rule from a constant")

    wrong: list[str] = []
    for text, window, want in cases:
        got = asyncio.run(R.continues_topic(text, window))
        if got != want:
            wrong.append(f"{'kept' if got else 'withheld'} "
                         f"{text.splitlines()[0][:48]!r}")
    check("every named must-keep continuation is kept", not wrong, wrong)


def the_wiring() -> None:
    """The gate is actually called, in the right order, keeping the last turn."""
    body = src("backend/ws_server.py")

    check("relevance is imported before the gate uses it",
          re.search(r"from backend\.memory import relevance\n",
                    body.split("continues_topic")[0]) is not None,
          "a later import would be a NameError at runtime")

    check("the gate is called on the message and the window",
          "relevance.continues_topic(message, context)" in body)

    gate_at = body.find("relevance.continues_topic(message, context)")
    recall_at = body.find("recall_query = f\"{prev_user_text}\\n{message}\"")
    paste_at = body.find("for m in context")
    check("the recall query is built before the gate",
          -1 < recall_at < gate_at,
          "recall reads the window; gating first would blind it")
    check("the window is pasted after the gate",
          gate_at < paste_at,
          "the gate has to run before the paste to have any effect")

    check("a shift keeps the last turn rather than nothing",
          "context = context[-1:] if context else []" in body,
          "dropping the whole window loses what the model just said")

    # The window is withheld, never deleted: the gate must not touch the store.
    gate_block = body[gate_at - 400:gate_at + 900]
    check("the gate does not delete anything from the store",
          "chat_history.clear" not in gate_block
          and "chat_history.delete" not in gate_block,
          "history has to stay reachable through recall")


def main() -> int:
    print("topic-shift gate: the signal")
    primitives()
    print()
    print("the four specified cases:")
    the_four_cases()
    print()
    print("the false positives a plain overlap rule produced:")
    the_false_positives()
    print()
    print("the continuations the design was built for:")
    covered_continuations()
    print()
    print("the wiring in run_chat_pipeline:")
    the_wiring()
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: the conversation window is relevance-gated, bare follow-ups "
          "keep their history, and a new subject no longer inherits the old one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
