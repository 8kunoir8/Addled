> ## ⚠️ CORRECTED 2026-10-10 — the central claim below is WRONG
>
> **The live app does not use the prompt-tools path this document describes.**
> It uses native function calling, and that works correctly.
>
> `chat_with_tools` picks the path at `tool_loop.py:825`:
>
> ```python
> uses_native = (
>     provider_id in NATIVE_TOOL_PROVIDERS            # 9router is NOT in this set
>     or type(provider).__name__ == "OpenAIProvider"  # …but 9router IS one
>     or getattr(provider, "has_native_tools", False)
> )
> ```
>
> 9router is absent from `NATIVE_TOOL_PROVIDERS` but *is* an `OpenAIProvider`,
> so the second clause wins and every live turn takes **`_call_native_tools`**.
> The fenced-JSON catalogue is sent only on `_call_prompt_tools`, which this
> provider never enters.
>
> Measured both ways against the live endpoint:
>
> | call | result |
> |---|---|
> | `chat(tools=[…])` — **the live path** | proper `tool_calls`, `list_dir`, correct args, executed, multi-round |
> | `chat()` with no `tools=` — **what this doc tested** | invents `list_files`, **fabricates an entire directory tree**, presents it as a tool result |
>
> The formats below — `<run_command>`, `[[list_dir]]` — are what the model
> writes when it has **not** been given `tools=`. That is real and worth
> knowing, but it is not what the user experiences.
>
> **Corrected conclusion:** "the model invents a format every turn" is an
> artifact of testing a path the app does not take. Given the native API, this
> model calls tools correctly. Kept rather than deleted — "I tested the wrong
> path and believed it" is the failure worth remembering.
>
> A live turn trace that settled it:
>
> ```
> >>> _call_native_tools   provider.chat tools=YES -> tool_calls=[list_dir {path:.}]
> >>> _call_native_tools   (round 2, refining) -> tool_calls=[run_command …]
> final: "needs your approval before it can run"   (the approval gate works)
> ```
>
> Addendum 2 (below) is a **different** symptom and is *not* explained away:
> there the model had `tools=` and still claimed the tool did not exist.
>
# The model invents a new tool-call format every turn (2026-10-09)

## What was expected

The catalogue tells the model exactly how to call a tool:

```
Call one with: ```tool
{"tool": "name", "params": {}}
```
```

`_parse_tool_response` reads three shapes: fenced JSON, fenced `tool`, and
Python-shaped `name("arg")`.

## What the model actually emits

Once the system-prompt fold-in (`system_role.py`) let the model SEE the
catalogue for the first time, it began trying to call tools — and invented a
different syntax on each of four consecutive turns:

| turn | what it wrote | parsed? |
|---|---|---|
| 1 | `<run_command><command>dir</command></run_command>` | no |
| 2 | `<run_command>` again, told explicitly to use fenced JSON | no |
| 3 | `[[list_dir]] E:\Kunoir\Codeground` | no |
| 4 | (same model, XML again) | no |

Asked directly to use the fenced form, it **refused** — "I can't emit a call for
a tool I don't have, so a fenced block like that would just be pretend" — and
used its own shape anyway. The format is this model's habit, not a mistake to
correct with a stronger instruction.

## Why it mattered so much

`_parse_tool_response` returned **nothing at all** for these — not even
`malformed`. So `_stream_round` (which gates on a parseable call) treated the
text as a final answer, and the tool never ran. The user saw the model narrate
an action it had not taken.

## What was fixed

XML-shaped calls are now parsed (`_call_from_xml`), and an unparseable
angle-bracket tag naming a real skill is reported as `malformed` rather than
vanishing. That covers the first two turns.

## What is still open

`[[list_dir]] args` is not parsed. More importantly, **this is a per-format
chase and the model keeps changing format** — patching each one is whack-a-mole.

Tested a general approach: detect a real skill name inside a call-shaped
wrapper, rather than matching one format. It separates cleanly:

```
PROSE                                        CALLS
'I could use list_dir for that.'   -> None   '[[list_dir]] E:\Kunoir'  -> list_dir
'The list_dir tool would help.'    -> None   '[list_dir] E:\x'         -> list_dir
'You can call run_command.'        -> None   'list_dir(E:\x)'          -> list_dir
```

A skill name in **prose** must stay prose; a skill name in a **wrapper**
(`[[ ]]`, `[ ]`, `< >`, `name(...)`) is an attempt. That is the shape of a real
fix, and it needs its own measurement before it is trusted: a false positive
turns an ordinary answer into a tool call, which is worse than a missed call.

## Recommendation

This is a **provider-behaviour workstream of its own**, not part of the meetings
plan. Two candidate directions, neither yet chosen:

1. **Wrapper-anchored general parsing** — the approach above, with a written
   set of cases and revert-verified checks. Handles new formats by construction.
2. **Make the model obey** — a stronger, more explicit catalogue instruction, or
   few-shot examples in the message itself. Cheap if it works; this model
   refused once already, so it is not obviously going to.

The meetings work does **not** depend on this: `meeting_summarise` and the
recall path are called through `skill_registry.execute(...)` and the WS
handlers, both of which work (verified live — transcribe, summarise, index,
recall, delete). This only affects the model *choosing* to call a tool in chat.

## Addendum 2026-10-10 — the catalogue budget was measuring a string nothing sends

`check_tools.py` asserted the tool catalogue stays under 60% of the local
model's usable window. Adding `meeting_actions` took the figure to 60.4% and
failed it, which looked like a real regression.

It was a **measurement that had drifted from the code**. The check called
`registry.to_prompt_tools()` with no argument — the whole ~120-skill catalogue.
Nothing in the app sends that:

```
backend/skills/tool_loop.py:1451
    if only is None:
        query = _last_user_text(messages)
        only = skill_registry.filter_for_query(query)
    tools_text = skill_registry.to_prompt_tools(only)
```

The only runtime caller **always** goes through `filter_for_query` first. The
unfiltered string is rendered by checks (`check_tools`, `check_mcp`,
`check_skill_bodies`) and by nothing else.

Measured, the two differ by an order of magnitude:

| | tools | tokens | % of window |
|---|---|---|---|
| unfiltered `to_prompt_tools()` | ~120 | 4,790 | **60.4%** |
| worst real turn (gated) | 16 | 740 | **9.3%** |
| short Indonesian greeting | 7 | 296 | **3.7%** |

So the guard failed on work the app does not do and would have **passed a
genuinely broken gate** — sending all 120 tools every turn is exactly the bug it
exists to catch, and at 60.4% it was one skill away from being waved through.

**Changed to measure the real path:** the check now runs `filter_for_query` over
a spread of intents (meeting, files, scheduling, greeting, empty) and bounds the
**worst** gated result at 60%. Two new assertions keep the diagnostic honest —
gating must narrow the catalogue, and the full figure must stay under 2× the
window, which fails if the gate stops filtering. Both revert-verified: a gate
returning `None` (everything) fails, and a trebled catalogue fails.

**Not fixed, found while measuring:** `filter_for_query` is keyword-based, and
"tugas dari hasil rapat" (Indonesian for "tasks from the meeting") matches
nothing — the meeting skills do not reach the catalogue for an Indonesian
meeting request. Pre-existing, affects every tool equally, and out of scope
here; recorded because it is the kind of gap that only shows up when the gate
is measured rather than assumed.

## Addendum 2 — the model denied having a tool it had been handed

Same root, sharper edge. In the live action gate the model answered:

> I don't see a meetings tool in what I have available here — I can do
> calendars, scheduled tasks, files, browser, and desktop, but there's no
> "list meetings" function wired up.

Measured for the exact question it was answering:

```
filter_for_query("…Phoenix migration review… list the meetings first")
-> 16 tools, including:
   meeting_actions, meeting_get, meeting_list, meeting_save, meeting_summarise
```

**All five were in the catalogue it received.** `meeting_list` was there by
name. The statement was false, and confidently false — it named the categories
it believed it had ("calendars, scheduled tasks, files, browser, desktop") and
those are not what it was sent either.

This is the same failure as the invented tool-call formats, one level up: the
model does not reliably read its own catalogue, so it does not merely *format*
calls its own way, it **reports its capabilities its own way** — and a user
asking "can you handle meetings?" gets a wrong answer with no way to tell.

**Why it matters more than the format issue.** A badly formatted call fails
loudly and can be caught and retried. A confident capability denial reads as a
finished answer: the user is told the feature does not exist, has no reason to
doubt it, and never learns the tool was one message away. It is the same class of
harm as the dropped system prompt — the work is done, and the model says it
wasn't.

**Not yet diagnosed.** Whether the model skipped the catalogue, or read a
truncated view, or pattern-matched from the prose reply is unknown. What is
established is that the catalogue contained the tool and the model said it did
not — so any fix that only improves *parsing* will not touch this.

### Addendum 2 — CORRECTED 2026-10-10: this also does not reproduce

The denial above was measured on the **prompt path**, which the live app never
takes. Re-asked with `tools=` — the live path — the same question behaves
correctly:

| question | tools sent | result |
|---|---|---|
| the exact gate question | 16, incl. `meeting_list` | **calls `meeting_list`** |
| "Do you have a way to list my meetings?" | `meeting_list` present | "Yes — the tool is `meeting_list`." |
| "What tools do you have for meetings?" | `meeting_list` present | names it, with an accurate description |

So the confident denial was, like the invented formats, a **wrong-path
artifact**. Given the native API this model reads its tool list correctly and
says so. Addendum 2's "why it matters" section is left standing as a
description of a *class* of harm, but the instance itself is retracted.

### The lesson, which cost two sessions

Two separate conclusions — "the model invents tool-call formats" and "the model
denies tools it has" — were both drawn from a code path the application does
not use, and both were written up as defects. Neither survived being tested on
the path a user actually takes.

The prompt path is a real fallback for providers without native support, and
`_call_prompt_tools` is genuinely reached when `chat(tools=…)` raises
`TypeError` (`tool_loop.py:~1355`). But **9router never raises it**, so for this
user the path is dead code. Anything measured there describes a hypothetical.

**The rule this earns:** before writing up a provider-behaviour defect, confirm
which branch the request actually takes. `uses_native` is computed in one
place — read it, or trace it, but do not assume the path you can drive most
easily is the path that runs. Both of these were easy to drive precisely
because they bypass the machinery that would have contradicted them.

## Addendum 3 — 2026-10-10: the REAL cause of the denials, found and fixed

Addendum 2 concluded that both the invented formats and the capability denial
were wrong-path artifacts. The formats were. **The denials were not** — they
reproduced on the live native path, and the cause is ours.

### The bug

`backend/tool_brief.py` appends a prose block naming the capabilities a turn
has, built from a hand-written `_FACTS` table. That table had **no meetings
entry**. So a turn holding five `meeting_*` schemas received this:

```
You are deeply integrated into the Addled desktop app with real tools…
- Run Windows PowerShell 5.1 commands… (`run_command`)
- Read, write and search files with `read_file`, `write_file`…
…
When asked what you can do, or asked to check or manage the calendar,
schedule, tasks, reminders, desktop, files, browser or memory, acknowledge
these capabilities and call the appropriate tool.
```

`grep -i meeting` over the whole block: **no match**. The closing sentence —
also hand-written — listed seven domains and **omitted meetings too**:

```
mentions meeting?: False
tools offered    : 16, including meeting_list, meeting_list, meeting_get,
                   meeting_save, meeting_summarise, meeting_actions
```

The model was handed `meeting_list` in its schema and told, in prose, an
inventory of its abilities that did not include meetings. It believed the prose:
*"there's no 'list meetings' function wired up."*

**This is the exact failure `tool_brief.py` was written to prevent.** Its own
module docstring describes it: *"an agent holding a tool schema but no prose
that the tools exist answers 'those tools aren't accessible in this environment'
and refuses work it could have done."* The fix for the original instance was a
prose block; the block then went stale for a capability added later.

### Why the existing check missed it

`check_tool_brief.py` proved **table → registry**: every name in `_FACTS` is a
real skill (that is how the stale `browser_open`/`remember`/`recall` names were
caught). Nothing proved **registry → table**: that a capability the app has is
described. So five skills could ship with no prose and no check would notice.

### The fix

1. A `meeting_save` fact, naming all five tools and saying plainly *"You DO have
   these tools; never tell the user you cannot access their meetings."*
2. The closing sentence is now **derived** from a `_DOMAINS` table rather than
   written out, so it cannot omit a capability the facts describe.
3. `check_tool_brief.py` gained the missing direction: every capability family
   must have a fact, and every fact must have a domain. It immediately caught a
   **second** instance of the same bug — `run_command` had a fact but no domain,
   so the sentence never mentioned the PC either. Fixed.

Revert-verified three ways: meetings fact removed, meetings domain removed, and
the old hand-written sentence restored.

**The third revert initially PASSED**, which is the useful part. My assertion
was "the block mentions meetings" — but the `_FACTS` prose also says
`meeting_save`, so a hardcoded sentence omitting meetings still passed. Tightened
to read the **closing sentence itself** and require every `_DOMAINS` entry in it.
It now fails on the restored old sentence, as it should.

### The pattern, which is the actual lesson

Three separate bugs this session had one shape: **a hand-maintained list that
must stay in sync with the code, checked in only one direction.**

| list | checked | result |
|---|---|---|
| `check_tools.py` catalogue budget | measured a path never taken | passed a broken gate |
| `_FACTS` capability prose | names are real, not that all are named | 5 skills denied |
| closing sentence domains | nothing — it was a string literal | meetings + PC omitted |

Each passed its own check while being wrong. The fix in every case was to
**derive** the thing rather than maintain it, and to check the direction nobody
was checking. Where a list cannot be derived, check it against its source in
both directions.

## Addendum 4 — the model NARRATES a tool call and ends the turn

With the prose block fixed, the final live gate passes cleanly:

```
You recorded one meeting that matches — "Phoenix migration review" on 2026-10-10…
Its action items were:
1. Send the migration plan to the platform team — Tom
2. Sign off the pricing cap — Steru

Before I create tasks, let me check your existing task list so I don't duplicate
anything:

**→ Check `task_list`**

Give me a moment and I'll report back, then turn those two into scheduled tasks
(unless you'd rather I just create them now — say the word and I'll go straight
ahead).
```

`**→ Check \`task_list\`**` is a **narration**, not a call. `tools used: (none)` —
no tool ran, and the turn ended. The user is told to wait for a follow-up that
never comes.

**This is distinct from every earlier symptom, and none of the fixes above touch
it.** The formats issue was parsing (and turned out to be a wrong-path artifact).
The denial was the `tool_brief` prose gap (fixed). This is the model *choosing
not to call* while writing as though it had.

**Why it likely happens, from the same reply:** the model already had the answer.
The recall block supplied the meeting's decisions and action items in the system
prompt, so no read was needed — and `task_list` was a genuine next step it
described instead of taking. A small model writing "→ Check `task_list`" has
produced a plausible *description* of an action, which is cheaper than emitting
a real call, and nothing in the prompt makes the difference observable to it.

**Not diagnosed, and not claimed to be.** Whether it is laziness, a formatting
habit, or the end-of-turn heuristic firing early needs its own measurement:
what happens when the turn *does* need a tool the recall block cannot supply.
Any fix here is speculative until then, and the failure is quiet — a narrated
call looks like progress to the user.

Recorded rather than fixed: this is the third distinct cause behind one visible
symptom ("the model doesn't use its tools"), and conflating them is how the
first two got written up wrongly.

---

## Addendum 4b — 2026-10-10: MEASURED, and it is not what Addendum 4 guessed

Addendum 4 asked for its own measurement before any fix, and said the guessing
should stop there. Here is the measurement, and it revises the guess.

**The narration is real, and it reproduces.** `chat_with_tools` on the live
provider, four identical runs of the Phoenix question:

| run | rounds | tools called | outcome |
|-----|--------|--------------|---------|
| 1 | 4 | `meeting_list`, `meeting_get`, `meeting_actions` | ok |
| 2 | 3 | `meeting_list`, `meeting_get`, `meeting_actions` | ok |
| 3 | 3 | `ask_user` | ok |
| 4 | 2 | `meeting_list` | **provider error: empty response** |

So the honest headline is the opposite of a total failure: **the tool path
works, most of the time.** Three of four runs found the meeting, read it, and
produced the right proposal — including the owner-is-a-third-party case that
the whole feature exists to protect.

**Why Addendum 4's guess was wrong.** It reasoned that the model narrated
because "the recall block already had the answer, so no read was needed". It
did not: in these runs the model *did* call `meeting_list` and `meeting_get`,
and the question was answerable only through them. Narration was not a
substitute for a call; it was preamble *around* one. `deepseek-v4.1-flash`
routinely emits "I'll list your meetings now" **and** the tool call in the same
message, so seeing narration is not by itself evidence of the bug.

**The actual defect is intermittent, and it is on the provider boundary.** Run 4
died the way `openai_provider.py:191` describes: `not content.strip()` was true
and there were no tool calls, so the turn was failed as an empty response. It
is not a prompt gap, not a catalogue gap, and not the model "choosing" to
narrate — the same request succeeded either side of it. Two candidate causes
are visible in the code and neither is confirmed:

**Both candidate causes were then tested, and one was wrong.**

`tool_loop.py:1066`'s `content: None` was **not** the cause: replaying that exact
follow-up 5 times with `content=None` and 5 times with `content=""` gave 0
failures in both. Measured, not assumed. It is still hardened (see below), but
it was not the bug, and "fixing" it would have been a change that looked like a
fix and changed nothing.

`9router` substituting the model is real but is **not a defect**: the gateway is
a router, `Voxagent` is its alias, and asking for `Voxagent` or for the
provider's own default both come back as `deepseek-v4.1-flash` either way. A
router routing is the job. Left alone.

**The actual cause was in the parser, and a raw-response log found it.** Patching
at the httpx layer to print every `chat/completions` body showed the failing
rounds clearly:

    RAW: finish='tool_calls' content=None tools=2

The gateway returns a **complete, valid tool call with `content: null`** and
`finish_reason: "tool_calls"`. That is legal under the OpenAI schema —
`content` is nullable and the call lives in its own field — and it is what a
model does when it has nothing to say before acting.

`openai_provider.chat` tested `if not content.strip():` **before** looking at
`message["tool_calls"]`, so it saw "no content", concluded the reply was empty,
and **threw the tool calls away** — then reported "The model returned an empty
response. Check that the model is loaded", blaming the model for a parser that
had discarded its answer.

That is a defect, not provider flakiness, and it explains the shape of the
outage exactly: the turn died at round 2 *after* `meeting_list` had already been
called once, and 2 of 8 runs failed while 6 succeeded, because whether the model
adds prose before a call varies run to run.

**Fixed and measured:**

* `openai_provider.py` now returns `ok=True` with the tool calls when content is
  empty but calls are present. The genuinely-empty branch is untouched — a
  reply with neither prose nor calls is still a failure, still with the same
  message, and `check_tool_calls.py` asserts both directions so the guard cannot
  be "fixed" by deleting it.
* `tool_loop.py` no longer drops a failed round that still carries calls. That
  ordering is what made the provider bug fatal: the loop bailed before reading
  the calls. Defensive now, but it is the reason one parser slip lost a whole
  turn.
* **8/8 live runs clean** afterwards, against 6/8 before. The three tools are
  called in order, nothing is created without asking, and the third party's
  ownership is named every time.

## The gate was scoring silence as success

`.livetest/gate_actions.py` reported **GATE: PASS** on a run whose reply was
"Let me look up the meeting and pull the action items." with `tools used:
(none)` and a 120s timeout.

The verdict was `ok = third_party_not_scheduled and (not created or
owner_kept)`. With no tool called, `created` is empty, so `not created` is
`True`, so the gate passed **because nothing happened**. A run that did nothing
scored identically to one that correctly proposed.

Fixed to require action: `acted = bool(calls)` is now part of the verdict, and
an actionless run prints `GATE: FAIL -- no tool was called, so this run
demonstrates nothing`. Re-run against the same failing behaviour, it correctly
reports FAIL. **The gate now fails when the model does nothing, which is the
point of having it.**

---

## Addendum 5 — 2026-10-10: the model wrote the call as DSML TEXT

With the parser fixed and the gate no longer scoring silence as success, the
live gate failed a third way — and this time the reply said exactly what
happened:

```
Let me pull up your recorded meetings so I can find the Phoenix migration
review and its action items.

<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="meeting_list">

</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>
```

That is DeepSeek's DSML tool-call markup, emitted as **literal text** instead of
through the API's `tool_calls` field. The model was not confused about what to
do — it wrote a precise, well-formed call naming a real skill. It just wrote it
in the wrong channel.

**The parser returned nothing, and did not flag it.** `_parse_tool_response`
returned `calls: []` *and* `malformed: 0` for those bytes. So a real call was
discarded in silence: no tool ran, and the raw markup was shown to the user as
if it were prose — reading as gibberish, with no error anywhere to explain why
the answer never came.

**Fixed** with `_call_from_dsml`, a sibling of the existing `_call_from_xml` and
there for the reason that function's own comment gives: *"the format is the
model's habit, not a mistake to correct... meeting the model where it is costs
one regex; not doing so costs every tool call it ever attempts."* Both the
fullwidth bars (U+FF5C, what was actually sent) and ASCII pipes are accepted,
because which appears depends on the build and guessing wrong costs the call.
Only a real skill name is accepted — the same rule the XML parser follows, so a
bogus name cannot start the market-and-forge path.

Verified on the **exact bytes** from the live failure, end to end: the call is
recovered, `meeting_list` executes, and the markup is swallowed so it cannot
reach the user. `scripts/check_tool_calls.py` asserts all of it, revert-verified
against a discarded parse (6 checks fail without the parser, including one that
catches the raw markup reaching the reply).

**How often it happens is not settled, and I will not claim otherwise.** Ten
direct runs of the same question produced **0/10** DSML — all ten used real tool
calls. Nor did repeating the WS pipeline's exact system prompt (capabilities
block plus the meetings context) reproduce it, 0/6. It appeared once, in a live
gate run, and was captured because the gate printed the reply verbatim. The
parser is correct regardless — a call the model *did* write should not be thrown
away — but the trigger is unmeasured, and the frequency matters for knowing
whether this was one unlucky turn or a common weakness of this gateway model.

## What this episode cost, and why the first write-up was wrong

Five distinct causes have now been attributed to one visible symptom ("the model
doesn't use its tools"), and each earlier write-up was partly wrong:

| # | claimed cause | verdict |
|---|---------------|---------|
| 1 | the model invents a tool-call format every turn | **wrong** — a path the app never takes |
| 2 | the model denies having tools it was handed | **real** — a `tool_brief` prose gap, fixed |
| 3 | the model narrates a call and stops | **real but misdiagnosed** — 3/4 runs called tools fine |
| 4 | provider flakiness on an intermittent round | **wrong** — a parser discarded valid tool calls |
| 5 | the model writes calls as DSML text | **real**, parsed now, frequency unmeasured |

The pattern worth keeping: **every wrong conclusion came from measuring a path
the app does not take, or from inferring a cause without capturing the bytes.**
`-s` measured a Python without `site-packages`; a bare `chat()` measured a call
the pipeline never makes; "provider flakiness" was inferred from a failure rate
instead of read off the wire. The fix that ended it was crude and obvious —
patch the HTTP layer, print every response body — and it took one run.

---

## Addendum 6 — 2026-10-10 (later): the model tried YET ANOTHER format

With the DSML parser deployed and the app restarted, the gate ran against the
real build and failed again -- with a fourth spelling:

```
Got it - let me pull up that meeting and turn its action items into
scheduled tasks.

I'll list the meetings first to get the id, then read the Phoenix migration
review and convert its actions.

<tool_call>meeting_list</tool_call>
```

To be precise about what this test did and did not show: the gate reports
`the turn actually used a tool: False`. What is verified is that the parser
returned nothing for those bytes -- the reply never reached the loop as a call,
so no tool ran. Whether the model also *intended* the tag to be executed is
the model's business; what is certain is that a well-formed call naming a real
skill was thrown away **without a malformed signal**, exactly as with DSML.

This is the third distinct ASCII-tag spelling seen from this model, after
`<|DSML| invoke ...>` markup and plain narration. The `<tool_call>` tag is
particularly worth supporting because it is **Anthropic's** convention, not
DeepSeek's: the model is not inventing syntax, it is cycling through the
real tool-call dialects of well-known providers. Each is a fixed, well-formed
format with its own parser at those vendors, so each is cheap to accept.

**Fixed** with `_call_from_toolcall_tag`, covering the bare name, `name()`,
and the self-closing attribute form. The JSON form inside the tag already
worked, because the fenced-JSON path finds the object first.

Revert-verified in `scripts/check_tool_calls.py`: 5 assertions fail when the
parser is pulled back out.

### The pattern, stated properly

Five separate occurrences, three formats, one root cause: **the app only
understood the call format it hoped for, and discarded anything else in
silence.** `_parse_tool_response` returned `calls: []` *and* `malformed: 0`
for DSML and for `<tool_call>` alike, so every one of these looked like a
model refusing to act rather than a parser dropping its answer.

The durable part of this work is not either regex. It is that a reply which
*looks like* a tool call and cannot be parsed should be **reported as
malformed**, never silently dropped. Both parsers now sit in the fallback
chain ahead of the generic path, and both are covered by checks pinned to the
literal bytes captured live. If a fifth format appears, the gate will catch it
the same way -- by printing the reply verbatim rather than describing it.

---

## Addendum 7 — 2026-10-10 (later still): a FAIL that was the harness, not the app

After the `<tool_call>` parser was deployed, the gate ran again and failed with
a reply that named the meeting's contents **without calling any tool**:

```
Nice — let me pull up that Phoenix migration review and get the action items
sorted.

I've got the meeting in my notes. Here's what came out of it:

**Decision:** Cut over on the 14th.

**Action items:**
1. **Send the migration plan to the platform team** — owner: Tom
2. **Sign off the pricing cap** — owner: Steru (that's you)
```

The contents were correct to the letter -- including both owners -- because the
model was not recalling the meeting it had just been asked about. It was
recalling **the previous gate run's own answer**, stored in the same conversation
memory it reads from. That answer sits there verbatim: decision, both actions,
both owners.

So the gate had planted nothing it could measure. It asked "find the meeting",
the model found a stored transcript of a *previous* answer about that meeting,
and answering from it was the correct behaviour. `the turn actually used a tool:
False` is the right verdict on the turn and the wrong verdict on the app.

**This is the same class of error as every wrong conclusion in Addenda 1-4**:
the measurement was invalid, not the thing being measured. And it is the same
specific mistake as Addendum 4's "provider flakiness" -- inferring a cause from
a symptom instead of checking whether the fixture was sound.

### Two real bugs, both in the harness

1. **The precondition could not see the pollution it existed to catch.** Its
   marker list keyed on *how one run phrased its answer* ("phoenix migration",
   "northwind"), so a later run whose answer read differently slipped past it --
   and the gate then **certified** a store it had not checked. A precondition
   that cannot detect the failure it guards against is worse than none.

2. **The gate made no attempt to clean its own leftovers.** It planted a
   meeting and stored its answer, then refused to run next time and asked a
   human to tidy up. A fixture this script wrote is this script's to remove.

### The actual root cause: the cleanup never removed the transcript

The gate's closing cleanup deleted the tasks and the planted meeting, then
printed `cleanup: done` -- but it **never removed the conversation rows**. It
drives the live app over `chat.send`, so the app stores both the question and
the model's reply in the memory the gate reads. Every run therefore left its
successor a store containing a full, correct answer to the very question it was
about to ask. The next run found it, replied from it, and reported
`the turn actually used a tool: False` -- a verdict on the app for a fault in
the harness.

This is why the failure was intermittent and hard to read: run 1 is honest,
run 2 answers from run 1's stored answer, and the symptom ("the model is
narrating instead of acting") points at the model every time.

The cleanup now snapshots the conversation row ids before the turn and deletes
whatever the turn added. The set-difference is the point: it removes exactly
this run's transcript and nothing else, so real history is untouched no matter
what the model says.

### The marker fix had its own bug

The precondition now matches the **planted meeting's own content** (`phoenix`,
`cutover`, `pricing cap`, `migration plan`, ...) rather than one run's phrasing,
and `clean_own_leftovers()` removes only rows matching those markers, then
re-verifies.

The first attempt at those markers was **too loose** -- it included `"got it"`
and `"action items"`, ordinary English that matched two unrelated rows (a
file-split chat and a policy answer). Cleaning a false positive would have
destroyed real user memory. That is the opposite error to the one being fixed,
and shows the hazard in marker-based cleaning: **a wrong marker does not merely
miss, it deletes.** The markers are now restricted to strings only this fixture
produces, and both directions are tested -- pollution is caught and cleared,
innocents are preserved.

### Worth stating plainly

Three of the six "failures" chased in this document turned out to be problems
with the test, not the app: a Python missing `site-packages`, a store holding
stale hash vectors, and now a gate poisoned by its own previous answer. The app
did have real bugs -- the parser discarded valid tool calls, and two ASCII-call
formats were silently dropped -- but the first move on any new failure should be
*"is this measurement sound?"*, and that question was skipped every time.

---

## Addendum 8 — 2026-10-10: the model was reading the SCREEN, not remembering

After Addendum 7's cleanup was deployed, the gate still failed, and the model
said something that should have been caught far earlier:

```
Same message a fourth time, word for word. I'll stop narrating and just run it.

**Action items from the Phoenix migration review:**
1. **Send the migration plan to the platform team** — Tom
2. **Sign off the pricing cap** — Steru (you)
```

The vector store was verified clean (165 rows, none matching). `chat_history`
had been emptied of all six poisoned messages. `links.db` held only links to
already-deleted rows. Every memory channel had been checked and cleared, and
the content still arrived.

So the probing stopped and the prompt was dumped instead. The provider was
patched to write the exact `messages` list to a file, one real turn was sent,
and the first message was **13,031 characters** long:

```
[Live screen awareness] recent screen description: ...
```

`ws_server.py` screenshots the desktop, has a vision model describe it, and
inserts that description as a **user message ahead of every prompt**
(`screen_note`, inserted at position 0 once any recent description exists).

The description was of **my own terminal**, and it contained the planted
fixture -- the meeting title, the summary, the actions and their owners -- read
off the screen while I was working, plus 7,710 characters describing the probe
code I was writing. The model was not recalling the meeting. It was looking at
it.

### What this means

1. **The gate was never measuring what it claimed.** It plants a meeting in
   the store and asks the model to find it; the meeting text was simultaneously
   visible on the screen and injected directly. Every "the model narrated and
   stopped" failure in Addenda 3, 5 and 7 is explained: the model already had
   the answer in its context and had **no reason to call a tool**. The
   behaviour was correct given what it was told.

2. **This is a real privacy and behaviour finding, not just a test artefact.**
   Screen contents are captured, described by a model, and placed in the prompt
   on every turn -- including when the user has not asked about the screen. A
   password manager, a private message or a medical result on screen becomes
   context for an unrelated request. The injection is unconditional once a
   description exists.

3. **It also explains the ASYMMETRY that was misread as flakiness.** Runs failed
   intermittently because it depended on whether my terminal -- with the fixture
   visible -- happened to be the foreground window when the screenshot was
   taken. Restarting the app, switching windows, or running the gate from a
   different terminal changed the result. That is exactly what "provider
   flakiness" looked like from outside.

### Confirmed directly

A guard was added to the gate: query `observer.status` over the WebSocket
before sending, and refuse if the description contains the fixture. Run against
the real app it named all six leaks:

```
REFUSING TO RUN — the injected screen description contains the planted meeting:
    leaked: phoenix
    leaked: migration review
    leaked: pricing cap
    leaked: migration plan
    leaked: platform team
    leaked: cut over on the 14th
```

That is the whole explanation, stated by the app: the answer was on screen and
in the prompt before the question was asked.

Two earlier versions of this guard were useless and were caught: importing the
engine reference in the gate's own process is always `None` (the gate is a
separate program), and an exception-swallowing `try` around it hid the fact
that it never looked. A guard that cannot fail is not a guard; this one was
validated by confirming it DOES refuse.

### What was NOT wrong (again)

The parser fixes from Addenda 5 and 6 are real and independently verified: the
`content: None` + `tool_calls` discard, the DSML parser and the `<tool_call>`
parser each fix a genuine silent-discard, tested against the literal captured
bytes. None of them was the cause of *these* gate failures.

### The honest ledger for this whole document

Of the failures investigated across all eight addenda, the causes were:

| cause | count | type |
|-------|-------|------|
| parser discarded a valid call | 2 | real app bug |
| `-s` measured a Python without `site-packages` | 1 | bad measurement |
| store held stale hash vectors | 1 | bad fixture |
| gate poisoned by its own stored answer | 1 | bad fixture |
| **screen description injected the answer** | **1** | **bad fixture (and a real concern)** |

Five of six were measurement faults. Not one was found by reasoning about the
model's behaviour; every one was found by capturing raw bytes -- the HTTP body,
the parser input, the final prompt. **When a model "won't use its tools", read
the exact prompt before theorising about the model.**
