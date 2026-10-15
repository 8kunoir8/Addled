# How Addled should improve itself — and the bug where a previous topic is carried into a new question

Research date: 2026-10-14. Everything about Addled is read from this repository's source
and **measured** by running it (against the live install at `%LOCALAPPDATA%\Addled` and
the live gateway on `localhost:20128`). Everything about other systems is from primary
sources with inline URLs; anything unverifiable is marked UNVERIFIED.

Companion: `docs/research/context-prompt-provider-architecture.md` is the "what Addled
does today" survey. This document is the "what to change, in what order, and why".

---

# Part 0 — The bug you reported: a new topic inherits the old one

## 0.1 Reproduced, and it is worse than it looks

Your example is in the live chat store, and it failed exactly as you described:

| Turn | Role | Content |
|---|---|---|
| 4 | user | `test connection` |
| 5 | assistant | *"Connection's good — I'm here. Quick note on the last few turns: I never actually ran the `skill_read`…"* |
| 6 | user | **`bro one off admin management itu maksudnya yg gimana ?`** |
| 7 | assistant | *"**Hasilnya bersih: Yang aku ubah: nggak ada. Turn ini cuma satu panggilan baca (`skill_read`)**"* |

Turn 6 is a fresh question about admin management. Turn 7 answers about `skill_read`
again. The model never saw the new topic.

**Measured, from your own store:**

```
history window: 8 msgs, 4270 chars total
new user message: 54 chars
=> the request is 98.8% OLD topic by volume
```

**98.8% of the input was the previous topic, and 1.2% was the actual question.** The
model was not misbehaving. It answered the only thing that was substantially there.

## 0.2 The cause, precisely

Addled **gates its memory by relevance** — facts, timeline, wiki, procedures, session
summaries and long-term recall all pass through `backend/memory/relevance.py` before
being allowed into the prompt. That is genuinely good design and rare among the systems
in the companion report.

**The conversation history is the one block that is not gated.** `ws_server.py:1821`:

```python
context = chat_history.get_context(max_messages=context_len)   # 1609: last 6/20 messages
...
user_messages = [{"role": m["role"], "content": m["content"]} for m in context]   # 1821
```

Every message in the window is copied in verbatim, on every turn, regardless of what the
new message is about. So the largest block in the prompt is the only unfiltered one.

This is not a one-off. Measured across your whole store (210 user turns with prior
context): **7.6% of turns are topic shifts** by a cheap overlap test, and the examples are
unambiguous — `'What colour is this image?'` arriving after `'OK'`, a file listing after
`'PONG-GAMMA'`.

> **Note added 2026-10-15:** this store no longer exists and the figures below cannot be
> reproduced. It was destroyed on 2026-10-14 — see *INCIDENT: a check suite destroyed the
> live chat history* at the end of this document. Every "210 turns" / "7.6%" / "35 withheld"
> number in Part 0 is a measurement of that store as it stood, and is kept as the design
> record rather than as a live claim. `scripts/check_topic_gate.py` now asserts the cases
> that mattered against fixed windows instead of replaying the store.
>
> It applies from here to the end of §0.3.

## 0.3 The right fix, and why the obvious fix is wrong

### I prototyped the obvious fix first, and it broke real workflows

The natural approach is "if the new message shares no words with recent turns, drop the
history". I tested exactly that against your real conversations. It would have dropped
**42 of 210 turns** — including these, which must *not* be dropped:

```
ov=0.33  'The delete_file you asked for was APPROVED and ran successfully…'
ov=0.27  'Teach Scout10807 a standing rule: keep every review under three sentences'
```

The first is a **permission approval continuation** — it is meaningless without the prior
turn. The second is part of an ongoing swarm task. A pure overlap test destroys both.

### The correct signal is an established, named pattern

The research found that this exact problem is solved in the field, and the signal is
**not** length and **not** semantic similarity. It is:

> **Does the new message contain any content word of its own?**

- **Zero content words** (`continue`, `why?`, `kenapa?`, `lanjutkan`, `mengapa`, `and then`,
  a bare `?`) → the message has no topic of its own → it is **recursively dependent** on
  the prior turn → **always keep history**.
- **One or more content words** (`bro one off admin management itu maksudnya yg gimana ?`
  → `{bro, one, admin, management, maksudnya, gimana}`) → it **can stand alone** → it is a
  candidate topic shift.

This is Kumar & Callan's **Cat1 (self-contained) vs Cat2-imp (context-dependent)**
classification from TREC 2019 CAsT — *"If the query belongs to Cat1, then it is a
self-contained query and demarcates a contextual shift"*
(https://trec.nist.gov/pubs/trec28/papers/CMU.C.pdf) — and Arguello & Rosé's **"Content
Contribution"** feature, *"Binary-valued, is there a non-stopword term in the current
contribution?"*, which was introduced specifically because *"dialogue contributions are
often terse or even contentless… TextTiling wrongly classifies all of these as starts of
new topics"* (https://aclanthology.org/W06-3407.pdf).

That second quote **is** Addled's bug, described in a 2006 paper.

Addled already half-implements this: `ws_server.py:1614` hardcodes `short_indexicals`
(`{"why","why?","explain","continue","how so","what about","and then","kenapa",…}`). The
content-word test **generalises** that list — every contentless turn is a follow-up, in
any language, for free. It does not *replace* it: see the correction below, where
`continue` and `explain` turned out to be indistinguishable from topic words and the list
had to be kept alongside. The two are complementary, not competing.

### Why not semantic similarity

Because Addled's default embedder is English-centric `all-MiniLM-L6-v2`, which — by
Addled's own measurement in `recall.py:129` — *"scores Indonesian and code-mixed turns at
0.23–0.33"*, below the 0.35 gate. **A semantic-only gate would fire constantly on your
Indonesian turns and drop history you needed.** Lexical overlap on content words is
language-independent: `admin`, `management`, `one off` survive intact in a code-mixed
sentence.

Addled **already ships the multilingual encoder** (`paraphrase-multilingual-MiniLM-L12-v2`,
which lists `id` as supported, `embedding.py:51`, 384-dim so the store is compatible) — it
is just not the default. Switching `memory.embedder` to `"multilingual"` is a config
change, not a dependency.

### Why the risk is acceptable

Published error analysis (Li, Gao et al., ACL 2022,
https://arxiv.org/html/2112.08812v2): replacing history invalidates **23% of questions**,
and the dominant cause is **unresolved coreference at 44%** — which is exactly the
bare-follow-up case, and exactly the case this design protects. Meanwhile the distractor
cost is documented too: TopiOCQA (https://aclanthology.org/2022.tacl-1.27.pdf) reports
*"non-relevant keywords in the representation act as distractors"*, and CHIQ's ablation
shows a topic-switch rule is worth **+13.2% MRR** on that benchmark while never hurting the
topic-continuation benchmark.

#### Correction: the content-word test alone is not sufficient

The research proposed the content-word test as *the* signal, replacing the hardcoded
`short_indexicals` list. **Implementing it showed that is wrong**, and this is the most
useful thing this exercise produced. Three findings, each measured:

**1. Continuation verbs are shaped like content words.** `_tokens` keeps words longer than
three characters, and `continue`, `explain`, `lanjutkan`, `jelaskan`, `mengapa` are all
ordinary verbs that survive the stopword filter:

```
'continue'  -> content_words={'continue'}   -> looks like a TOPIC
'explain'   -> content_words={'explain'}    -> looks like a TOPIC
'and then'  -> content_words=set()          -> correctly a follow-up
```

So the test is not a replacement for the list — it is the **open-ended half** of the
signal, and the list is the **closed half**. Both are needed, and they union cleanly.

**2. The dependency can live entirely in the assistant's previous turn.** Three real
turns from the store depend on what the assistant just said, not on their own words:

| User turn | Prior assistant turn |
|---|---|
| `well, i have approved it, check again` | *"still waiting on your approval"* |
| `continue what you were doing` | *"I will pick up where I left off"* |
| `yes lets do as you recommend` | a recommendation, laid out just before |

No property of the *user's* message detects these. A check on the prior turn is required.

**3. That check must distinguish an approval from an offer.** The first version treated
any hint of *"want me to" / "would you like"* as a dependency — and that **reintroduced
the exact bug being fixed**, because the reported conversation's prior turn ends with an
offer and a question. Measured on that turn: lexical `0.0`, cosine `0.12` — about as
clearly a new topic as this data gets. The corrected rule lets a *blocking approval*
override, but an *offer* only breaks a tie.

**The resulting rule**, as implemented, in order — every branch can only *keep* history,
and only the last can withhold it:

```
1. no content words at all                       -> KEEP   (bare follow-up / ellipsis)
2. only continuation words ("continue","explain")-> KEEP   (closed-class imperatives)
3. empty window                                  -> KEEP   (nothing to compare)
4. lexical overlap >= 0.34 with the recent turns -> KEEP   (same subject)
5. prior turn is blocked on an approval          -> KEEP   (gates the next action)
6. prior turn asked, and cosine >= 0.35          -> KEEP   (weak, tie-break only)
7. otherwise                                     -> WITHHOLD, keeping the last turn
```

Rule 7 keeps **one** message rather than none — CHIQ's documented topic-switch behaviour
(*"if a switch is identified, the enhanced history would only include the last turn"*) —
so the model still knows what it just said.

Measured across the live store (210 user turns with prior context): **35 withheld (16.7%),
zero must-keep continuations lost**, and the reported bug turn drops from 3,336 stale
characters to 523.


### The rule as first proposed (superseded — see the correction above)

```
1. No content words            -> KEEP full history   (protects the 44% coreference failure)
2. Content words + lexical overlap >= 0.34 with recent turns -> KEEP full history
3. Content words + no overlap  -> TOPIC SHIFT: withhold the stale window from this request
```

This is the shape the research recommended and the shape that shipped, with the two
additional guards that implementation proved necessary.

**Point 3 must not delete anything.** Suppress only the verbatim paste; `chat_history`
keeps every row, so an older topic referenced later is still reachable through the existing
`build_memory_context_hybrid` recall path. TopiOCQA shows questioners *do* go back 1–2
topics, so the information must stay recoverable.

This reuses Addled's existing primitives — `relevance._tokens`, `lexical`, `LEXICAL_MIN` —
so it adds no dependency and follows the file's existing convention.

## 0.4 A related finding: Addled's stopword list has no Indonesian words

Measured: `relevance._STOP` contains **zero** Indonesian function words (`itu`, `yang`,
`dan`, `yg`, `di`, `ke`, …) while **7% of your messages are Indonesian**. For *this* fix
that is survivable (Indonesian words surviving as "content" pushes toward standalone,
which is the safe direction for a fresh question) — but it is a real limitation of the
existing relevance gate, and worth knowing about.

---

# Part 1 — The other gaps, in priority order

Order matters. This sequence is deliberate, and the reason for it is in each section.

## 1. Context-window correctness — **do this first**

**Measured defect.** Addled's live provider is `9router` with `default_model: "Voxagent"`.
`budget.CONTEXT_LIMITS` has no entry for `9router`, so `context_limit("9router")` returns
`FALLBACK_CONTEXT = 32768`. But the gateway's own `/v1/models` publishes:

```
{'id': 'Voxagent', 'context_length': 200000, 'max_completion_tokens': 64000}
```

**Addled assumes 32,768 where the truth is 200,000 — a 6× under-estimate**, and it can be
worse: other entries on that same gateway list 1,050,000. Addled therefore trims history it
had no reason to trim, silently.

**Addled already downloads this data and discards it.** `backend/providers/model_catalog.py:185`:

```python
mid = item.get("id") if isinstance(item, dict) else item
if mid:
    models.append(str(mid))        # context_length was right there in `item`
```

The fix: capture `context_length` / `max_completion_tokens` when fetching `/models`, consult
it from `budget.context_limit()`, and make the caller pass the **resolved model** — because
one gateway serves models with windows from 200K to 1.05M, so a single per-provider number
is structurally incapable of being right.

**When the window is genuinely unknown**, the fallback policy should change:
`FALLBACK_CONTEXT = 32768` is the *smallest local-model window in the table* and has no
business being the answer for a cloud proxy. A cloud-plausible 128,000, logged at `info`
naming the provider and the assumed number, plus a user-settable
`providers.<id>.context_limit` override — that is what Claude Code ships as
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` for exactly this situation
(https://github.com/bogdanmatasaru/claude-code-guide/blob/main/docs/guides/auto-compact.md).

**Why this comes first:** a token-budget compactor (next section) built on a 32,768 guess
would summarise away history the 200,000-token gateway would have carried happily — it
would make things *worse*. This is a precondition, not a nice-to-have. It is also a
few-line change to code that already fetches the data.

## 2. Prompt caching — the largest cost and latency win

**Measured: Addled has none.** No `cache_control`, no `prompt_cache`, no `cached_tokens`
anywhere in `backend/`. It rebuilds ~10KB+ of prompt every turn and pays full price on
every token.

What that costs, per provider:
- **DeepSeek**: cache-hit input is **25–50× cheaper** than cache-miss
  (https://api-docs.deepseek.com/quick_start/pricing) — and this is what `9router` fronts.
- **OpenAI**: cache reads are **0.1×** the input rate (https://developers.openai.com/api/docs/guides/prompt-caching).
- **Anthropic**: needs explicit `cache_control` breakpoints, max 4, 5m/1h TTL.

**The real blocker is not a missing API parameter.** It is that Addled appends ~10KB of
volatile per-turn memory blocks onto the front of the prompt, so the prefix differs on
essentially every turn — and it flattens everything into one undifferentiated system
string, which makes a breakpoint impossible to place.

**Minimum viable change:** split the assembly into a **stable prefix** (persona,
capabilities block, emotion menu, static directives) and a **volatile tail** (recall,
facts, wiki, procedures, timeline, meetings, rolling summary), in that order.

**This is worth doing even on providers that cannot cache**, because:
1. It is the precondition for caching anywhere.
2. Claude Code's own documented list of cache-breaking mistakes starts with putting
   volatile content early (https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything).
3. **Static-first improves quality independently** — Addled's own code already records
   that a thousands-of-token block appended *before* the tool catalogue makes a small model
   lose the instructions above it.

A useful data point on how much this matters: Hermes on this machine keeps a **frozen**
memory snapshot per session specifically to protect the prefix cache, and orders its prompt
into three explicit tiers (`stable → context → volatile`) whose docstring says the purpose
is that *"implicit longest-prefix caches keep the unchanged scaffold."*

**Tension worth naming:** Addled's relevance gating and prefix caching pull against each
other. The better the gate works, the more the prefix changes. The resolution is that the
**stable prefix should be genuinely stable** (persona + capabilities + fixed directives)
while the gated blocks live in the tail. You get the gate's benefit and a cacheable
prefix.

**On adopting OpenAI's Responses API:** the documented gains are real (3% SWE-bench,
40–80% cache improvement — https://developers.openai.com/api/docs/guides/migrate-to-responses),
and GPT-6 Astra/Sol *require* it for tool calling. **But DeepSeek's Responses endpoint is
explicitly stateless** — `previous_response_id`, `conversation` and `store` are all
unsupported (https://api-docs.deepseek.com/guides/responses_api). Since Addled runs
DeepSeek-through-9router, adopting Responses buys nothing on the path that actually
runs. **Recommendation: keep Chat Completions, add Responses behind a per-provider
capability flag only for OpenAI-proper models, and spend the effort on prefix hygiene.**

**Status: verified against the primary source and deliberately NOT done.** The DeepSeek
docs were re-read rather than trusted from this report, and they confirm the claim
directly: `previous_response_id`, `conversation` and `store` are each marked **"Not
supported (stateless API)"**, and unsupported parameters are **silently ignored** rather
than erroring. So switching Addled's 9router path to `/v1/responses` would be the same
stateless one-shot request in a different envelope — no server-side conversation state to
buy, and the automatic caching it does offer is the same cache Addled already reaches
through Chat Completions (measured this session, see the caching status below).

No `responses` capability flag was added. Adding one for a provider Addled does not run
would be dead code, and the check suite would then have to defend a path nothing
exercises. If an OpenAI-proper model is ever configured, the flag belongs next to
`NATIVE_TOOL_PROVIDERS` in `backend/skills/tool_loop.py`, which is already the
per-provider capability table — that is where to look, not in a new mechanism.

## 3. Compaction of live history

**Addled does not compact.** `maybe_compact()` (`memory/compaction.py`) only *appends* a
rolling-summary block; nothing is ever removed from the live list, which stays a raw
`messages[-20:]` slice. On a long conversation the prompt grows **both** ways.

Claude Code's real behaviour (https://code.claude.com/docs/en/how-claude-code-works):
*"It clears older tool outputs first, then summarizes the conversation if needed."*
Its trigger is an absolute token window, not a message count — about **967K** on native-1M
models, the **200K** boundary on 4.6-generation ones
(https://code.claude.com/docs/en/model-config). And the summarization call uses
**cache-safe forking**: the same system prompt, tools and history, with the compaction
prompt appended as a final user message, so the cached prefix is reused
(https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything).

**What Addled should take from this:**
- Trigger on **estimated tokens against the real window**, not a message count (12).
- **Clear older tool outputs first** — they are the fastest-growing content and the
  cheapest to drop.
- Keep a word-for-word tail; summarize only what falls out.
- Add a **thrashing guard** (Claude Code: *"stops auto-compacting after a few attempts and
  shows an error instead of looping"*). Addled's `_locked` flag prevents *concurrency*, not
  *recurrence*.
- Preserve what already works and has measurements behind it: single-flight locking, the
  15s quiet-window yield, the heuristic fallback when the provider is down, and the
  conversation scoping. Each exists because of a recorded failure.

**Prerequisite:** requires a `chat_history.replace_context()` primitive, which does not
exist today.

## 4. Per-block token accounting — the cheapest win, and the instrument for the rest

**The problem:** the prompt is a bare `str` built by `+=` in ~20 places. Nothing knows what
any block costs, so nothing can report *"the wiki block is now 5.5KB"*. My measurement
table below had to be produced by an offline harness; the running system cannot show it:

| Block | Chars |
|---|---|
| Base persona | 659 |
| `capabilities_block` (all tools) | 2,592 |
| **`wiki_ctx`** | **5,553** |
| `long_term_recall` | 1,493 |
| `user_profile` | 185 |

Note the wiki block is the **largest single contributor — bigger than the whole tool
catalogue** — and nobody can see that.

Claude Code ships `/context` for exactly this
(https://code.claude.com/docs/en/how-claude-code-works).

**Cheapest fix:** a small `PromptBuild` wrapper that records `(label, len)` on each append
and still behaves like a `str` everywhere downstream. ~15 lines, mechanical call-site
edits, no behaviour change, and it *removes* lines at call sites (it absorbs the `if block:`
checks). Log the breakdown at `info` and it would have made both this table **and** the
32,768 window visible on turn one instead of requiring a research pass.

---

# Part 2 — What NOT to do

- **Do not gate on semantic similarity alone.** With the default English-centric embedder,
  any Indonesian or code-mixed message scores 0.23–0.33 — below every threshold — so a
  semantic-only gate would classify most of your messages as topic shifts. Lexical first;
  semantic may only *confirm*, never rescue.
- **Do not delete history on a topic shift.** Only withhold the verbatim paste. Deleting
  breaks the documented "go back 1–2 topics" behaviour and the recall path.
- **Do not drop history for a contentless turn.** This is the 44% coreference failure mode.
  It is the single most expensive mistake available here.
- **Do not raise the relevance thresholds to "fix" this.** `0.35`/`0.34` are calibrated and
  the gap between them (`retrieval_min_similarity: 0.25`) exists because of a measurement.
- **Do not adopt the Responses API for the DeepSeek path.** It is stateless there; you
  would do the work and get nothing.
- **Do not build an Indonesian stopword list.** Indonesian function words surviving as
  "content" pushes toward standalone, which is the safe direction. Doing this would make
  `'lanjutkan'`-style turns *harder* to classify correctly.
- **Do not copy Claude Code's 96.7% compaction threshold.** It compacts near the window
  edge because it caches and reserves a compaction buffer. Addled does neither and its
  estimator is deliberately pessimistic. Start around 0.75.
- **Do not do compaction before fixing the window.** You would compact against a number
  that is 6× too small.

---

# Part 3 — Recommended sequence

| Order | Change | Effort | Why here |
|---|---|---|---|
| 1 | **Topic-shift gate** (your bug) | ~50 lines in `relevance.py` + 4 lines at the call site | It is the reported bug; reuses existing primitives; no dependency |
| 2 | **Per-block accounting** | ~15 lines + mechanical edits | The instrument that makes 3 and 4 visible; zero behaviour change |
| 3 | **Window correctness** | ~30 lines in `model_catalog.py` + `budget.py` | Data is already downloaded; a few lines; unblocks 4 |
| 4 | **Prompt caching** (prefix split) | Medium — restructure the assembly | Largest cost/latency win; 2 is a prerequisite |
| 5 | **Compaction of live history** | Largest — new primitive + rewrite | Needs 3 correct first; benefits from 4's forking |

Each should ship with a `scripts/check_*.py` that fails by name when reverted, matching
the existing convention (e.g. `check_skill_files.py` carries 55 checks and is
mutation-tested).

## Status: items 1 and 3 are implemented

Implemented and verified, uncommitted, pending review:

| Item | Files | Tests |
|---|---|---|
| Topic-shift gate | `backend/memory/relevance.py` (`content_words`, `is_bare_followup`, `continues_topic`), `backend/ws_server.py` (gate + one `import`) | `scripts/check_topic_gate.py` — 35 checks |
| Window correctness | `backend/providers/model_catalog.py` (capture), `backend/providers/budget.py` (consult + fallback + override), `backend/skills/tool_loop.py` (pass the model) | `scripts/check_context_window.py` — 33 checks |

Both suites are registered in `scripts/check_all.py` and **mutation-tested**: reverting
each guard makes the relevant check fail by name (verified for the gate call, the
content-word test, the continuation-word union, the prior-turn check, the offer-versus-
approval distinction, the `default_model` preference, the fallback value, and the
discovery capture).

Two bugs were found **by the checks themselves**, during implementation:

1. `context_limit` borrowed a sibling model's window when the named model was absent from
   the discovery map — a real error on a gateway spanning 200k to 1.05M.
2. With no model named, taking `min()` over all published windows returned **16,385**
   (a legacy `gpt-3.5-turbo` on the gateway) — worse than the 32,768 being replaced. Fixed
   by preferring the provider's own `default_model`.

Live result: `context_limit("9router")` now returns **200000**, discovered from the
gateway, where it previously returned 32768.

`check_guide.py` fails in the full suite, and is **pre-existing and unrelated**: it asserts
the built `dashboard/out` carries version 1.0.41, and the built output is stale. It fails
identically with these backend changes stashed.

### End-to-end test against the live install (2026-10-14)

Run with `ADDLED_DATA_DIR` pointing at `%LOCALAPPDATA%\Addled` — the documented override —
so the **real store, real settings and the live 9router provider** were used, while the
code under test was this working tree. The request was intercepted at the HTTP boundary,
which is the only place that shows what actually leaves the machine.

The reported conversation, replayed through `run_chat_pipeline`:

| | Gate OFF (old behaviour) | Gate ON (fixed) |
|---|---|---|
| Messages in the first request | **7** | **2** |
| Prior conversation turns sent | 6 | 1 (the last turn only) |
| Turns containing `skill_read` | 4 | 0 |
| Characters of prior conversation | 3,336 | 523 |

And the replies differ in kind, not just in length:

- **Gate OFF:** *"I didn't actually run anything for that — I described what I would do,
  called no tool…"* — still prosecuting the `skill_read` topic.
- **Gate ON:** *"Nggak ada tool yang perlu gue panggil buat ini, bro. Pertanyaan lu
  'one-off admin management itu maksudnya gimana?' itu pertanyaan arti ist[ilah]…"* —
  answers the question that was actually asked, in the user's own language.

The gate fired in the live log as designed:

```
INFO addled.ws: new topic: carrying the last 1 of 2 window message(s) for this
                request; earlier history kept and reachable through recall
```

Window fix, same environment: `model_catalog.refresh()` discovered **93** windows from
the live gateway and `context_limit("9router", model)` returned **200000** where the old
code returned 32768. `local` stayed 8192 and `ollama` 32768, so the widening is confined
to providers that publish a window.

Scope of this test, stated plainly: it exercises the real pipeline, provider, store and
gateway, but **not** the packaged app. The live install at
`C:\Program Files\Addled\resources\backend` still contains none of these changes — it is
the 1.0.41 build. Confirming the fix in the shipped app needs a rebuild and reinstall.
Test conversations created during this run were removed from the live store.


## Status: item 2 (prompt caching) is implemented, and the result is narrower than predicted

The prediction in §2 above — that Addled "has none" and that the assembly order is the
blocker — is **half wrong, and the measurement is what corrected it.**

### What was built

| Piece | Where | What it does |
|---|---|---|
| Per-block accounting | `backend/providers/budget.py` (`PromptBuild`) | Names each block, its size, and which blocks are turn-invariant |
| Stable-prefix emission | `backend/providers/system_role.py` (`fold_into_user(stable_prefix=…)`) | Emits the invariant head as its own leading message, so a prefix cache can hold it |
| Plumbing | `backend/skills/tool_loop.py`, `backend/ws_server.py` | Carries `sys_prompt.prefix()` from the builder to the provider |

Checks: `scripts/check_prompt_cache_layout.py` (20 checks) and
`scripts/check_prompt_accounting.py` (29 checks), both registered and mutation-tested.

### What was measured

The first measurement is the finding that matters most, and it is a **negative** one:

| Scenario | Cache hit, prefix split | Cache hit, no split |
|---|---|---|
| **Established conversation, tail changing every turn** | 84–95% | 85–96% |
| **New conversation, first turn, question varying** | **98%** | **0%** |

Both rows are from `run_chat_pipeline` on the live provider, intercepted at the HTTP
boundary, ten consecutive real turns per row.

**The honest reading: §2's premise was wrong.** Addled was *not* paying full price on
every token. Once a conversation has history, that history sits at the front of the folded
message and is itself stable, so the prefix cache was already working — 85–96% — **before
this change**. The `stable_prefix` split does not move that number.

What it does fix is the case with no history to anchor on: a **new conversation starts at
0%** and now starts at **98%**. Every conversation in Addled begins cold, so this is a
real win, but it is a *first-turn* win, not the across-the-board one §2 predicted.

### Two corrections this measurement forced on the report

1. **"Addled has none" was too strong.** It has no *explicit* caching code, but the
   gateway caches automatically and the assembled prompt was already mostly stable. The
   absence of `cache_control` is not the same as the absence of caching.
2. **The fold was not the blocker §2 claimed.** The fold appends instructions *after* the
   user's question, which looked catastrophic and is not: the conversation history ahead
   of both of them is the stable span, and it is large.

### The one number that did hold up

On the isolated first-turn pattern the effect is stark and reproducible — same 9,935-token
prompt, same varying question, same provider, measured back to back:

```
OLD (fold only):        prompt=9935  hit=0     (0%)
NEW (prefix split):     prompt=9935  hit=9728  (98%)
```

Also verified: `9router` **is** in `_KNOWN_DROP`, so the fold fires on the live path and
this code is reachable — not inert. (`verdict_for` returns `False`, i.e. `_DROP`; a
synthetic `provider_id='9router'` object reproduces `drops_system_role → True`.)

### A measured non-issue, recorded because it cost real time

A 7,200-character message sent as `system` added **13** `prompt_tokens` while the same
text as `user` added **1,214** — which looks like proof that 9router drops the system role
today. It is not: calling `prov.chat()` directly bypasses `system_role.prepare()`. Through
the real path the message arrives folded, as one `user` turn of 5,309 chars. **Use
`chat_with_tools` to exercise the fold; `prov.chat()` does not.**


### Checks, and the full suite

Both new suites are registered in `scripts/check_all.py` and mutation-tested. Reverting
each guard makes a named check fail:

| Mutation | Caught by |
|---|---|
| fold ignores `stable_prefix` (the pre-change behaviour) | `the stable head is its own first message` |
| `_prefix_end` no longer stops at the first volatile block | 3 accounting checks |
| drop the match-by-value guard on the prefix | 2 cache-layout checks |
| `ws_server` stops supplying the prefix | 2 cache-layout checks |

Full suite: **116 suites ok**, 1 skipped (needs a configured workspace), and
`check_guide.py` failing — **pre-existing and unrelated** (it asserts the stale
`dashboard/out` build carries 1.0.41).

Two regressions the existing checks caught, both fixed: `check_system_role.py` and
`check_approvals.py` pinned the exact source text of two call sites that `PromptBuild`
legitimately reformatted. Both now assert the behaviour rather than the syntax — the
approval note is still appended byte-for-byte (verified:
`PromptBuild.add(..., newline=False)` reproduces the old `+` exactly).

**One self-inflicted deletion, recorded as a hazard:** a `rm -f *.txt` cleanup removed
`requirements.txt`, `requirements-core.txt` and `requirements-vision.txt`, which made
`check_vision_deps.py` fail. Restored from git; that suite passes again. **Do not
glob-delete `*.txt` in this repo.**


## Status: item 4 (compaction) is implemented — the conversation now gets smaller

§3 called this correctly: `maybe_compact()` only ever *appended* a rolling summary, and
the live list was never rewritten, so a long conversation grew in both directions at once.
The summary was pure cost.

### What was built

| Piece | Where | What it does |
|---|---|---|
| Rewrite primitive | `backend/memory/chat_history.py` (`replace_context`) | The one method that can remove a message; everything else appended |
| Two reductions | `backend/memory/compaction.py` (`_rewritten`) | Truncate old long messages, drop what was summarized |
| Gradual fold | `backend/memory/compaction.py` (`keep_from`) | Advances by the summarized chunk, not by a head-jump |
| Thrashing guard | `backend/memory/compaction.py` (`_turns_since`) | Refuses until `MIN_TURNS_BETWEEN_COMPACTIONS` new turns arrive |

### Measured, live, against the real store

```
seed: 120 messages
  pass 0: 130 -> 110 messages
  pass 1: 120 -> 100
  ...
  pass 5:  80 ->  60
```

Each pass adds ten turns and folds twenty, so history converges instead of collapsing.
The character effect is larger than the message effect, because the dropped turns are the
long ones: a single pass on an 80-message conversation took **26,540 → 19,920 chars**
(25%), and this was measured before the fold was made gradual — the first version reached
**85%** and that was the bug below.

### Two bugs the checks found, both by running the code rather than reading it

1. **The head-jump.** The first version set the fold line to `total - KEEP_TAIL`,
   gutting a 240-message conversation to 12 in one pass. On the 12-message default
   threshold it discarded most of a short conversation before the summary had been shown
   to be any good. It now advances by the chunk actually summarized.
2. **The stale position.** `summarized` was left as a position in a list the rewrite had
   just shortened, so `pending = all_msgs[summarized:]` came back empty and the
   conversation compacted **exactly once and then never again** — measured on 240
   messages. It is now a running total, and what remains to fold is derived from the
   tail rather than from a position.

### A third bug, found by mutation testing rather than by the checks

The first wiring check looked for the refusal's *warning string* in the source. Mutating
the code to ignore the refusal and save anyway left that string in place, so **the
mutation was not caught**. The check now drives `maybe_compact()` with a store whose
`replace_context` returns 0, and asserts the state is unchanged. That version fails on
the mutation.

This matters because the refusal is what stops turns being marked summarized while they
are still in the list — which would lose them from both the history and the summary.

Checks: `scripts/check_compaction_rewrite.py` (38 checks), registered and
mutation-tested — the stale bookmark, the tail protection, the refusal path and the
list copy are each caught by name.

### A fourth regression, caught by the suite: a test double that lost its contract

`check_bot_chat.py` failed after this change. Its `_FakeHistory` had `get_context` but no
`replace_context`, so `maybe_compact` raised `AttributeError`, the broad `except` swallowed
it, and the check saw `False` — a failure with nothing to do with what it tests. The fake
now models the rewrite and returns the new length, as the real store does.

Worth stating plainly: **this is the cost of adding a method to a dependency that test
doubles stand in for.** The code was right and the test double was incomplete, but the
suite caught it, which is the point of running the suite after every change rather than at
the end.

### Deliberately preserved

The properties that were already right and are easy to lose in a rewrite of this size:
the single-flight `_locked` guard, `_wait_for_quiet()` with its 15s window before the
model is spent, `MAX_CHUNK`, the heuristic fallback when the provider is down,
conversation scoping, and the state re-read after the wait. Each has a named check.

### Sized against the real store, not a synthetic one

The live store was read (read-only — no writes) to check the sizes compaction will
actually meet. 83 conversations, 10 at or over the 12-message threshold, 3 that would
compact on the next turn. The longest is **168 messages**, and the heaviest is a
**24-message / 65 KB** conversation — few messages, almost all of it payload.

That second shape is the case worth checking by hand, because `foldable` (12) exactly
equals the threshold (12):

```
messages: 24 -> 20          (barely moves)
chars:    61762 -> 3273     (95% smaller)
truncated: 0                tail intact: yes
```

The message count hardly changes while the token cost collapses, because the weight is in
the dropped prefix rather than in the number of turns. That is the reduction working, and
it is why message count is the wrong metric to judge this feature by — the character
column is the one that matters.

### Scope of the verification, stated plainly

| Capability | Live data / gateway | Installed app |
|---|---|---|
| Topic-shift gate | yes | **no** |
| Context-window fix | yes | **no** |
| Prompt caching (cache-hit rate) | yes | **no** |
| Compaction + `replace_context` | yes | **no** |

Every row was exercised against the real store, the real 9router gateway and the real
HTTP boundary — but by running **this working tree**, not the installed binary. The
1.0.41 install at `C:\Program Files\Addled\resources\backend` contains **none** of this
work (checked: no `PromptBuild`, no `stable_prefix`, no `replace_context` in it). It is a
built artefact, so confirming in the shipped app needs a rebuild and reinstall, which is
a ~1 GB payload with npm and pip — a different exercise from a code test cycle.

**The deferred question is now answered.** The compaction summary enters the prompt as
the `rolling_summary` block, appended *after* the stable head (`persona`, `capabilities`,
`emotion_menu`). Verified by rebuilding the append order and reading
`PromptBuild.prefix()`: the prefix is exactly `persona + capabilities + emotion_menu`,
and `rolling_summary` is **not** in it. The summary is therefore volatile, sits in the
tail, and cannot break the cached prefix as compaction rewrites history beneath it.

## INCIDENT: a check suite destroyed the live chat history (2026-10-14)

**83 conversations became 1.** The cause was a check, not the product, and it is written
down here because it is the most expensive mistake in this whole effort.

### The mechanism

`scripts/check_compaction_rewrite.py` needed a `ChatHistory` it could write to without
touching a real file, so it did this:

```python
h = ChatHistory()
h._data = {"conversations": {}, "current_conversation": None}   # blank
h._loaded = True        # tells _load() there is nothing to read
h.create_conversation("t")      # -> _save() writes the BLANK store
```

Blanking `_data` and setting `_loaded = True` skips the load, so the next `_save()`
writes whatever is in memory — an empty store — over `HISTORY_PATH`. That is safe **only
while the process is not pointed at a real data directory.** The suite was then run with
`ADDLED_DATA_DIR` set to the user's real data dir (first from the repo, then again against
an installed build), and on the first save the live store was replaced.

`create_conversation()` calls `_load()` first and therefore *merges*. The damage came from
the fake, not from the store's own logic — the product is fine.

### Why it was not caught earlier

The suite passed. It passed **because** it was writing to a blank store: the assertion
"the store was empty to begin with" held trivially. A check that fakes its input cannot
notice that it is overwriting the real one.

### Recovery

Not recovered. Checked and exhausted: no `.bak` beside it, `AddledMemoryKeep` held an
unrelated single conversation from 2026-09-19, File History is not configured, no OneDrive
copy, and VSS needs elevation. The terminal-history file that matched the search matched
only because it held the *terminal output* of an earlier session, not the data. The
damaged file was preserved as `chat_history.DAMAGED-20261014.json`.

### The fix

The redirect moved to the top of the check, **before** `chat_history` is imported, and is
no longer optional:

```python
_SANDBOX = tempfile.mkdtemp(prefix="addled_check_store_")
os.environ.pop("ADDLED_DATA_DIR", None)          # a subprocess cannot re-acquire it
_app_paths.DATA_DIR = _app_paths.MEMORY_DIR = Path(_SANDBOX)
_chat_history_mod.HISTORY_PATH = Path(_SANDBOX) / "chat_history.json"
```

`check_sandbox_is_real()` then asserts `HISTORY_PATH` is inside the sandbox and is not the
real file, as the **first** check the suite runs — so a future edit that moves the
redirect below an import fails loudly instead of silently. Verified by pointing
`ADDLED_DATA_DIR` at a decoy store seeded with 83 conversations: all 83 survive.

The lesson, stated for the next person: **a test that writes must redirect the path, not
fake the object.** `scripts/check_chat_scope.py` got this right from the start — it swaps
`HISTORY_PATH` and every sibling path into a `TemporaryDirectory` and restores them in a
`finally` — and it is the pattern the others should have copied.


## BUG 2: the promise guard rewrote an honest answer (2026-10-14, 21:41)

A user asked, in Indonesian, **"bro, kamu punya skill apa aja ?"** — *what skills do you
have?* — on a brand-new conversation. The reply shown was:

> *"I didn't actually run anything for that — I described what I would do, called no tool,
> and there is no result to report. Nothing was changed. If you still want it done, ask
> again and I'll make the call itself…"*

That is `_PROMISED_NOTHING_ANSWER`. The model **did** answer the question; the guard threw
the answer away and substituted an apology for a promise that was never made.

### Reproduced from the log

```
21:41:11,749  prompt: 9650 chars (~3216 tokens), 16 blocks; prefix 3714 chars (38%)
21:41:15,477  Promise nudge: announced action, nothing called
21:41:17,939  Promise nudge: announced action, nothing called
21:41:20,412  Promise nudge spent; replacing an unbacked promise with the honest result
```

### The defect: `_PROMISE_INTENT` fires, `_EXPLAINS_PAT` does not clear it

Asked what it can do, a model naturally opens **"Let me list my skills: …"**. `_PROMISE_INTENT`
matches `let me …` with *any* verb, and the only thing that clears it is `_EXPLAINS_PAT` —
which contains `explain, describe, summarise, clarify, outline, walk you through, note that,
mention, point out, answer, tell you about`, and **not `list`, `show`, `tell you`, `share`,
`enumerate`, `break down`, `give you`, `provide`, `cover`, `recap`, `spell out`, `lay out`,
`run through`, `start with`, `begin with`, `take a look at`.**

Measured, on the live code:

| Reply opening | Verdict |
|---|---|
| "Let me **explain** what I can do…" | ok |
| "Let me **walk you through**…" | ok |
| "Let me **list** what I can do…" | **rewritten** |
| "Let me **show** you what I can do…" | **rewritten** |
| "I'll **tell you** what I can do…" | **rewritten** |

**Measured rates.** Benign replies wrongly rewritten: **7 / 33 (21%)**. Of 36 plausible
benign verbs after "let me", **25 are misread**. Real promises missed: **0 / 14** — the
existing protection is intact; this is purely a precision defect.

### Why precision is the whole safety of this feature

The nudge gate is `tools_were_offered AND no call this round AND nothing ran yet AND
_announced_work(text)`. On any *answered question* — no tool was needed, so nothing ran and
nothing was called — **the first three are always true.** The text test is the only
discriminator standing between a normal answer and a rewrite. It is not a secondary guard,
so a 21% false-positive rate is a serious defect, not a cosmetic one.

### The failure is worse than the one it guards against

The guard was built for a real bug (live, 2026-10-10): the model said *"Firing the delete
now; expect an approval prompt"*, called nothing, and the user later asked "how is it?"
with nothing to report. Rewriting **that** is correct — the model lied.

But the rewrite asserts *"there is no result to report"* about a turn that never claimed to
act. A misfire therefore manufactures a **false confession**, which is a new falsehood
rather than a silent miss. `_history` check: the replacement has fired **once in the entire
log** — this incident. Three other nudges fired and recovered (the model went on to call
`run_command` on 2026-10-11 and `skill_read` on 2026-10-14), which is why they were
harmless. A false-positive nudge usually costs a model call; it only reaches the user when
the model repeats the flagged phrase.

### Also length-dependent

`_announced_work` returns False above 700 chars (an explanation runs long, a promise is one
line). So a *short* capability listing is rewritten and a *long* one escapes — the same
question is answered or destroyed depending only on how verbose the model happens to be.
Measured: 55-char listing → rewritten; the same text padded to 1,135 chars → passed through.

## The fix

**Extend the veto, and scope it to the clause.** Do not replace the detector, and do not
grow a verb whitelist of *actions* — measured, that does not work: "Let me list my skills:
file editing" contains `edit`, and a whitelist cannot tell a skill being *named* from an
action being *taken*.

The change is one regex plus one split, applied before the existing tests:

```python
# Verbs that describe TELLING or SHOWING, never doing. Multi-word forms are
# listed explicitly because the discourse sense lives in the phrase: "run
# through" is discourse, "run that" is not.
_DISCOURSE_VETO = re.compile(
    r"\b(?:let me|i'?ll|i will|i'?m going to)\s+(?:quickly\s+|just\s+)?"
    r"(?:list|show|tell\s+you|share|enumerate|break\s+down|give\s+you|provide|"
    r"cover|recap|run\s+through|spell\s+out|lay\s+out|start\s+with|begin\s+with|"
    r"take\s+a\s+look\s+at|walk\s+you\s+through)\b", re.IGNORECASE)

_IMMEDIACY = re.compile(
    r"\b(?:now|right now|as we speak|sekarang|langsung|bentar|sebentar)\b",
    re.IGNORECASE)

def _without_discourse_clauses(text: str) -> str:
    """Drop sentences that only announce what the reply is about to say.

    Clause-scoped on purpose. A blunt whole-reply veto is unsafe in the other
    direction: "Let me list what I'll delete. Firing the delete now." starts
    with a discourse phrase and ends with a real promise, and a reply-wide
    veto would let the promise through. A sentence is dropped only when it
    carries the discourse phrase AND no immediacy marker.
    """
    return " ".join(
        part for part in re.split(r"(?<=[.!?\n])", text)
        if not (_DISCOURSE_VETO.search(part) and not _IMMEDIACY.search(part)))
```

`_announced_work` then runs its existing frames on `_without_discourse_clauses(text)`.

### Verified

| metric | current | with fix |
|---|---|---|
| benign replies wrongly rewritten | 7 / 33 | **0 / 33** |
| real promises missed | 0 / 14 | **0 / 14** |
| benign verbs misread | 25 / 36 | **0 / 13** |
| existing check's own 14 fixtures | — | **0 regressions** |

The mixed cases are the test of the clause scoping, and all pass:
"Let me list what I'll delete. Firing the delete now." → caught;
"I'll show you the result after deleting that folder now." → caught;
"Let me run through it - actually I'm running the command now." → caught.

### The canned reply must not be English-only

The user asked in Indonesian and was answered in English, because
`_PROMISED_NOTHING_ANSWER` is a hardcoded English string and the fallback path bypasses the
reply-language directive entirely. `backend.language.detect()` already returns the user's
language (`"bro, kamu punya skill apa aja ?"` → `id`) and `_NAMES` already carries a name
per language, so the fallback can mirror the language the same way a normal reply does.

### What must NOT change

- **The guard itself.** It exists for a real, measured lie. Deleting it would restore the
  2026-10-10 bug. The fix is precision, not removal.
- **`MAX_PROMISE_NUDGES = 2`.** A single nudge cannot tell "will not call" from "needs one
  more ask", and the live evidence for two is in the comment.
- **The foreign-language frames.** They caught the 2026-10-11 Indonesian promise. The veto
  must stay English-framed (the discourse form is English) so it cannot weaken them.
- **The 700-char cap.** It is load-bearing for the intent frame; leave it.

### Known, and deliberately out of scope here

- **A false *completion* is unguarded.** "Done — it's deleted." with no call is a different
  defect (the model claims an outcome, not an announcement) and there is **no guard for it**
  anywhere in `tool_loop.py`. Worth its own change; folding it into this one would widen the
  blast radius of a fix that is currently one regex.
- **"Lanjut cek sekarang" is missed standing alone.** It matched on 2026-10-11 only because
  it rode a longer message. The Indonesian frames need the marker inside the clause; the
  bare imperative falls outside. Recorded as a measured limitation, not fixed here.
- **The 6/26 in-scope promise shapes found in the false-negative hunt** ("The command is
  running", "Invoking the shell tool", "The deletion is underway") are worth a separate
  look — all are subject-less present-tense claims, which no current frame covers.
- **Two discourse replies with an immediacy marker still fire**, and both are
  genuinely ambiguous: "Let me show you the deletion output now" and "I'll list the files
  I deleted now". The veto deliberately defers to the marker, because weakening it to clear
  these would reopen the real-promise hole the clause scoping exists to close. Both are
  outside the class this fix targets (an answer that claims no action). Left, and recorded.

### Test cases added — all now in `scripts/check_announced_not_called.py`

The suite tested refusals, plain answers, completed claims and real promises, and **had no
capability-question case at all** — which is why a 21% false-positive rate passed a green
run. Sections 7-10 were added:

1. **Capability listings must not be rewritten** — 11 phrasings of the discourse frames,
   in English and Indonesian, including the incident's own language.
2. **Every discourse verb individually** — 16 entries, because the defect was a missing
   list entry rather than a missing idea.
3. **The mixed replies must still be caught** — 4 fixtures where a discourse sentence
   shares a reply with a real promise. This is the clause-scoping guard; a reply-wide veto
   fails exactly these and nothing else.
4. **End to end** — the incident's exact question through `chat_with_tools` must return
   the model's answer, cost exactly one call, and execute nothing.
5. **The fallback's language** — Indonesian gets an Indonesian form, unclear falls back to
   English.

Mutation-tested, all three caught by name: reverting the veto (27 failures), making it
reply-wide (2 failures, both clause-scoping), and ignoring the language (2 failures).




---

# Sources

**Addled:** `backend/ws_server.py` (1540-1823, 2113), `backend/memory/relevance.py`,
`backend/memory/chat_history.py`, `backend/memory/compaction.py`,
`backend/providers/{budget,model_catalog,router,system_role}.py`, `backend/memory/embedding.py`,
`backend/skills/tool_loop.py`. Measured against `%LOCALAPPDATA%\Addled\{settings,chat_history}.json`
and `GET http://localhost:20128/v1/models`.

**Topic shift:** Arguello & Rosé, *Topic Segmentation of Dialogue* (https://aclanthology.org/W06-3407.pdf) ·
Kumar & Callan, TREC 2019 CAsT (https://trec.nist.gov/pubs/trec28/papers/CMU.C.pdf) ·
Hearst, *TextTiling* (https://aclanthology.org/J97-1003.pdf) ·
Adlakha et al., *TopiOCQA* (https://aclanthology.org/2022.tacl-1.27.pdf) ·
CHIQ (https://aclanthology.org/2024.emnlp-main.135.pdf) ·
Li, Gao et al., *Ditch the Gold Standard* (https://arxiv.org/html/2112.08812v2) ·
Mo et al., *Learning to Relate to Previous Turns* (https://arxiv.org/html/2306.02553v1) ·
Liu et al., *Lost in the Middle* (https://aclanthology.org/2024.tacl-1.9/)

**Caching / compaction / context:** https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything ·
https://code.claude.com/docs/en/{prompt-caching,context-window,model-config,how-claude-code-works,troubleshooting} ·
https://platform.claude.com/docs/en/build-with-claude/{prompt-caching,compaction-on-demand} ·
https://developers.openai.com/api/docs/guides/{prompt-caching,compaction,migrate-to-responses} ·
https://api-docs.deepseek.com/{guides/kv_cache,guides/responses_api,quick_start/pricing}

**Multilingual:** https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

**UNVERIFIED carried forward:** the "~92% of window" auto-compact figure; whether 9router's
`context_length` is static per-combo or refreshed from upstream (it is self-consistent
across the whole response and authoritative for the gateway either way, but its provenance
is not established).
