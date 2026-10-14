# How Addled builds context, prompts, and converses with providers — compared to Claude Code, DeepSeek, OpenAI, Hermes, and Gemini/ADK

Research date: 2026-10-14. Addled findings are from this repository's source, read and
**measured** (numbers below are from running the code, not estimates). The comparison
systems are from primary sources with inline URLs; anything unverifiable is marked
UNVERIFIED.

---

# Part 1 — Addled, as it actually is

## 1.1 The turn, end to end

`run_chat_pipeline` in `backend/ws_server.py` (~line 1540 onward) does, in order:

1. **Egress scrub** (`backend/safety/egress.py`) — secrets stripped before the message
   leaves the machine, if `safety.egress_guard` is on (default true).
2. **Resolve the provider** (`providers.get_provider()`).
3. **Build `sys_prompt` as one string**, by repeated `+=` of independent blocks
   (§1.2).
4. **Take history**: `chat_history.get_context(max_messages=context_len)` — a plain
   slice, `messages[-max_messages:]`. Default **20** messages for cloud, **6** for
   `local` (`ws_server.py:1604-1609`).
5. **Query expansion for recall**: if the message is short (<5 words or <25 chars) or
   is a bare indexical ("why", "continue", "lanjutkan", "mengapa"), the *previous user
   turn* is prefixed to form the recall query (`ws_server.py:1611-1624`).
6. **Memory retrieval**, gated per block (§1.3).
7. **Hand to the tool loop** (`chat_with_tools` in `backend/skills/tool_loop.py`).

## 1.2 The system prompt is one concatenated string

There is no prompt object, no sections, no template. It is a Python `str` built by
sequential concatenation, and every block is wrapped in `try/except` that logs at
`debug` and continues:

```python
sys_prompt = config.get("chat", "system_prompt", default="") or ""   # base persona
sys_prompt = sys_prompt + "\n\n" + caps                # capabilities, from the tool list
sys_prompt = sys_prompt + "\n\n" + emotion_menu        # character face tags
sys_prompt = sys_prompt + "\n\n" + facts_ctx           # relevance-gated
sys_prompt = sys_prompt + "\n\n" + profile_ctx         # always on
sys_prompt = sys_prompt + "\n\n" + timeline_ctx        # relevance-gated
sys_prompt = sys_prompt + "\n\n" + wiki_ctx            # relevance-ranked
sys_prompt = sys_prompt + "\n\n" + sop_ctx             # relevance-gated
sys_prompt = sys_prompt + "\n\n" + session_ctx         # relevance-gated
sys_prompt = sys_prompt + "\n\n" + meeting_ctx         # relevance-gated
sys_prompt = sys_prompt + "\n\n" + rolling_ctx         # always on
sys_prompt = sys_prompt + "\n\n" + memory_ctx          # semantic recall
sys_prompt += "\n\n[Saved facts] …"                    # KG triples, keyword-gated
sys_prompt += "\n\n[Related] …"                        # link graph
sys_prompt += "\n\nIf a saved fact …"                  # grounding instruction
sys_prompt += "\n\nAlways reply in the same language …"
```

The base persona is 659 characters (`backend/config.py:308`). The **capabilities block
is generated from the actual tool list** (`backend/tool_brief.py:capabilities_block`) —
this is the single best design decision in the file, and the comments record why: a
hand-written inventory drifted from the real tool set, so a model holding five
`meeting_*` schemas answered "there is no list meetings function wired up". It is
derived now, so the prose and the schema cannot disagree.

### Measured sizes (live install's own store)

| Block | Chars |
|---|---|
| Base persona | 659 |
| `capabilities_block` (all tools) | 2,592 |
| `wiki_ctx` | **5,553** |
| `long_term_recall` | 1,493 |
| `user_profile` | 185 |
| facts / timeline / summaries / meetings / sop / rolling | 0 (gated out for an unrelated query) |

The relevance gates **do** work: asked about an unrelated topic, six of the eleven
blocks correctly returned nothing. The wiki block, however, was the largest single
contributor — larger than the tool catalogue itself.

## 1.3 Per-turn retrieval gating (Addled's most distinctive feature)

`backend/memory/relevance.py` is a **relevance gate** that decides whether a memory
block is injected *at all*. Its docstring records the bug it exists to fix:

> Injecting them into every prompt regardless of what was asked is how a bare "yoo
> apakabar ?" came back as a screen resolution: the model was handed a wall of stale
> context and no question.

Two thresholds, either passes:

- **Lexical**: share of the query's content words present in the memory ≥ **0.34**
  (stop-word filtered).
- **Semantic**: cosine ≥ **0.35** — but skipped entirely when the embedder is the
  "hash" fallback, because those vectors carry no meaning to compare.

There is a deliberate **two-tier** design worth noting: a *retrieval floor*
(`retrieval_min_similarity: 0.25`) that admits candidates into the ranking, and the
*relevance gate* (`min_similarity: 0.35`) that decides what is used. The config comment
explains the gap was measured — English-centric MiniLM scores Indonesian and
code-mixed turns at 0.23–0.33, so a 0.35 floor admitted **zero** semantic candidates and
hybrid search silently degraded to keyword-only.

The stated bias: *"dropping a memory that would have helped costs the agent a little
context, while keeping one that does not fit changes the answer."* Every comparison
system below does retrieval; **Addled is the only one that gates injection on relevance
as a first-class rule.**

## 1.4 Model routing: roles, not a router model

`backend/providers/router.py` classifies each message into one of seven roles —
`chat, reasoning, vision, long, utility, plan, judge` — using **pure regex heuristics**
and length thresholds:

| Signal | Role |
|---|---|
| Attachments, image-only | `vision` |
| > 1,200 chars, or "whole file"/"entire repo"/"summarize this document" | `long` |
| "prove/debug/refactor/architect/trade-offs/step-by-step" | `reasoning` |
| ≥ 12-line fenced code block, or ≥ 2 question marks | `reasoning` |
| > 400 chars (non-code), or > 80 chars with a code hint | `reasoning` |
| otherwise | `chat` |

An explicit `@role` prefix overrides everything (used by tests and by the user). Each
role maps to a per-provider model; only `deepseek` ships builtin defaults
(`deepseek-v4.1-flash` for chat/utility, `deepseek-v4-pro` for reasoning).

This is **heuristic classification, not learned routing.** Claude Code and OpenAI route
by *delegating to a subagent with a different model*; Addled routes by regexing the
user's own words.

## 1.5 Conversing with the provider

`backend/providers/openai_provider.py` is the base for OpenAI, LM Studio, and any
OpenAI-compatible endpoint. It posts to `/chat/completions` — **Chat Completions, not
the Responses API**. Key handling, each of which was a measured bug:

- **`content: null` with `finish_reason: "tool_calls"` is treated as a complete
  answer**, not an empty one. The comment records the live evidence:
  `RAW: finish='tool_calls' content=None tools=2`, and that the wrong ordering "killed
  2 of 8 live runs at round 2, after the model had already called meeting_list".
- **`reasoning_content` is captured and echoed back** on the follow-up request. The
  comment names the two failure modes: the model repeats its reasoning, or it spends the
  whole token budget thinking and returns no content at all (`qwen3-8b` at 2000 tokens:
  9063 chars of reasoning, 0 of content).
- **Timeouts are per-provider**: 600s for local/ollama/lmstudio, 120s otherwise, because
  a local 8B "reliably exceeded 60s" and `httpx.ReadTimeout` has an empty `str()`.
- **SSE is handled even when not requested**, because some routers always stream
  (`_parse_sse_as_completion`).

### Two tool-call mechanisms

`tool_loop.py` picks by provider (`NATIVE_TOOL_PROVIDERS = {"openai","deepseek","gemini","openrouter"}`):

- **Native** (`_call_native_tools`): real `tools` schemas, `tool_calls` in the reply.
- **Prompt-based** (`_call_prompt_tools`): for models without native tool support, the
  catalogue is written into the conversation as **its own message immediately before the
  user's**, and the model replies with a text call. The comment records the measurement:
  glued onto the user's message, a 20-character question became ~6,600 characters of
  English with the question buried at the top, and the model "wrote the list out again
  (three samples: 516, 564 and 1920 characters, up to 32 items repeated)"; separated,
  "the same turn answers in ~200 characters and repeats nothing."

The call format is **repeated on the question** because "one message away is far enough
for a small model to forget it is allowed to call anything" — measured 4/10 with the
format only in the catalogue, **10/10** with it repeated.

**`MAX_TOOL_ROUNDS = 8`.** Above the loop there are two nudge mechanisms with their own
caps: `MAX_PROMISE_NUDGES = 2` (the model claims work is happening but called no tool)
and `MAX_VERIFY_NUDGES = 1`.

## 1.6 The system-role fold (a genuinely unusual feature)

`backend/providers/system_role.py` detects that an endpoint **silently drops the
`system` role**, using token accounting: `prompt_tokens` counts the whole request, so if
it comes in under half the minimum the system text should account for
(`len/6` tokens), the system text is not in there. A verdict is persisted per provider.

`_KNOWN_DROP = {"9router"}` — and the docstring carries the **measurement**: a 200-word
system message added exactly **0** `prompt_tokens` while the same text in a user turn
added **403**.

When a provider is known to drop it, `fold_into_user` moves the system text into the
user turn — **appended after the user's own words**, not prepended. The comment explains
that prepending broke `check_reply_language.py`, which asserts the turn starts with the
user's question.

I have not seen this anywhere else. Every other system either trusts the system role or
documents which providers are incompatible.

## 1.7 Budget

`backend/providers/budget.py` trims against an *estimated* window:

- `CHARS_PER_TOKEN = 3` — deliberately an **under**-estimate: "An UNDER-estimate is the
  unsafe direction… which is what `request (8470 tokens) exceeds the available context
  size (8192)` was, seen in a real chat."
- `fit_messages` keeps a pinned system + recent tail, drops the middle.
- `reply_budget` reserves room for the reply (`MIN_REPLY_TOKENS = 256`).

**Measured gap:** on the live install the active provider is `9router`, whose
`providers.9router` config is empty, so `context_limit("9router")` returns the
`FALLBACK_CONTEXT` of **32,768**. The actual model behind it is `deepseek-v4.1-flash`,
which DeepSeek documents at a **1M-token** context
(https://api-docs.deepseek.com/quick_start/pricing). Addled therefore throws away ~97% of
the available window, and — more importantly — **trims messages that did not need
trimming**, silently.

## 1.8 What Addled does NOT have

Verified by grep across `backend/`:

- **Zero prompt caching.** No `cache_control`, no `prompt_cache`, no `cached_tokens`, no
  cache breakpoints — `hits: NONE`.
- **No Responses API / previous_response_id / conversation state.** Chat Completions
  only, client-side history.
- **No compaction on the hot path.** `maybe_compact()` exists
  (`memory/compaction.py`, threshold 12) but writes a *rolling summary block* appended
  to the prompt; the live message list is still a raw slice. Nothing summarizes the
  conversation *from the conversation's own tokens* the way Claude Code's `/compact`
  does.
- **No token-level accounting of the prompt.** It is a `str`; nothing knows what any
  block costs.

**Dead code found while reading:** `backend/providers/openai_provider.py:191-218`
contains a comment block **duplicated verbatim** (14 lines, 13 distinct comment lines
repeated). A copy-paste artifact; harmless but worth removing.

---

# Part 2 — How the others do it

## 2.1 Claude Code

**The organizing principle is prompt caching.** Anthropic's own blog is explicit:
"Design your entire system around this constraint. Get the ordering right and most of
the caching works for free"
(https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything).

**(a) System prompt.** Not one string — **four cache-ordered layers**:
1. static system prompt + tool definitions,
2. project context (CLAUDE.md, auto memory),
3. session context,
4. conversation messages.

Critically, **CLAUDE.md is NOT in the system prompt**: "CLAUDE.md content is delivered
as a user message after the system prompt, not as part of the system prompt itself"
(https://code.claude.com/docs/en/memory). Updates mid-session go through
`<system-reminder>` tags in the next user message or tool result
(https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything).

**(b) Compaction.** Trigger is an absolute token window, not a percentage — "models
running with a native 1M window compact before the window fills, at about **967K
tokens** by default"; 4.6-generation models compact "at the **200K** boundary"
(https://code.claude.com/docs/en/model-config). Note the widely-cited "~92%" figure is
**UNVERIFIED** for current versions. It "clears older tool outputs first, then
summarizes the conversation if needed"
(https://code.claude.com/docs/en/how-claude-code-works), and re-injects from disk:
project-root CLAUDE.md, auto memory, the plan, and **up to five recently-modified
files** (https://code.claude.com/docs/en/context-window). If a file keeps refilling
context it "stops auto-compacting after a few attempts and shows an error instead of
looping".

`/compact` is **cache-safe forking**: "the *exact same* system prompt, user context,
system context, and tool definitions as the parent conversation… then append the
compaction prompt as a new user message at the end" — so the cached prefix is reused and
"a mid-session `/compact` costs a fraction of what the context size suggests"
(https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything).

**(c) Provider conversation.** Messages API. Caching is exact-prefix, **up to 4
`cache_control` breakpoints**, 5m default TTL (1h available at 2× write cost); cache
reads cost 0.1× input (https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
Two consequences are documented as *deliberate*: plan mode "keep[s] *all* tools in the
request at all times and use[s] EnterPlanMode and ExitPlanMode as tools themselves"
because swapping the tool set "would break the cache"; and MCP tools use
`defer_loading: true` stubs discovered via tool search rather than being removed.

**(d) Tool loop.** Subagents (the Agent tool, ex-Task) run in **separate context
windows**: "Each subagent runs in its own context window with a custom system prompt,
specific tool access, and independent permissions"
(https://code.claude.com/docs/en/sub-agents). They "might explore extensively, using
tens of thousands of tokens or more, but return only a condensed, distilled summary
(often **1,000-2,000 tokens**)"
(https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). A
subagent notably does **not** inherit the main conversation's auto memory, and does not
read the parent's cache.

**(e) Routing.** By subagent model, not by message classification: "If you need to
switch models, the best way to do it is with subagents… we do this often with the Claude
Code's Explore agents, which use Haiku"
(https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything).

**(f) Reasoning replay.** Thinking blocks carry a `signature` and must be passed back
"complete and unmodified"; modified blocks are rejected with a **400**. "Within the
latest assistant message, the sequence of consecutive `thinking` blocks must match what
the model generated" (https://platform.claude.com/docs/en/build-with-claude/thinking).
`redacted_thinking` must be round-tripped too, and filtering on `type == "thinking"`
alone "silently drops `redacted_thinking` blocks and breaks the multi-turn protocol."

**Addled parallel:** Addled's `reasoning_content` echo is the same *idea* (the provider
requires its reasoning returned), arrived at independently and implemented more simply —
a string field on the assistant message rather than signed blocks.

## 2.2 DeepSeek

**(a) Harness.** The API is the substrate: one endpoint in three wire formats (OpenAI,
Anthropic, Responses) — https://api-docs.deepseek.com/. Open-source harness `dsh`
(https://github.com/deepseek-ai/deepseek-harness) is "everything is a plugin" on the
Cordis framework, with a cardinal invariant: **"Model-visible ⟺ logged."** The session
log is ground truth; model history is *derived* (`deriveMessages()`), never stored
separately (https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/architecture.md).

**(b) The headline finding — the reasoning pass-back contract.** Whether
`reasoning_content` must be echoed **depends on whether the request carries `tools`**
(https://api-docs.deepseek.com/guides/thinking_mode):

- **With `tools`**: the `reasoning_content` of *all previous turns* MUST be passed back,
  and is concatenated into context.
- **Without `tools`**: it need not be, and if passed it is **ignored**.

The documented failure is verbatim: *"If your code does not correctly pass back
`reasoning_content`, **the API will return a 400 error**."* This applies "even for turns
where the model did not perform a tool call."

**This is exactly what Addled implements** — `tool_loop.py:1611-1613` sets
`assistant_message["reasoning_content"] = reasoning`, and only when the provider actually
sent it, "so providers that never do are not handed an unknown field." Addled's
implementation is correct and matches the documented contract.

**(c) Caching.** Automatic, on-disk, no explicit mode
(https://api-docs.deepseek.com/guides/kv_cache). Prefix units persist at request
boundaries and detected common prefixes. Pricing makes it dramatic: on `deepseek-flash`,
cache-hit input is **$0.003–0.006** per 1M vs cache-miss **$0.15–0.30** — a **25–50×**
difference (https://api-docs.deepseek.com/quick_start/pricing). Minimum cacheable
threshold: **UNVERIFIED** (docs describe a sliding-window model, not a token floor).

**(d) State is always the client's.** Even the Responses format is **stateless**:
`previous_response_id`, `conversation`, and `store` are all unsupported and unsupported
params are *silently ignored* (https://api-docs.deepseek.com/guides/responses_api).

**Addled parallel:** Addled is client-state-only, same as DeepSeek. But because Addled
never caches, it pays the full cache-miss price on **every request** — including the
large, mostly-identical system prompt it rebuilds every turn.

## 2.3 OpenAI

**(a) Responses vs Chat Completions.** Responses is now recommended for new work;
documented advantages include a **3% SWE-bench improvement** with reasoning models and
"**40%–80% improvement**" in cache utilization
(https://developers.openai.com/api/docs/guides/migrate-to-responses). The structural
difference is **Messages vs Items**: Responses input/output is a union of typed items
(`message`, `function_call`, `function_call_output`, `reasoning`), where Chat
Completions "glues many concerns into one object."

Note the constraint: "GPT-6 Astra and GPT-6.1 Sol **require** the Responses API for tool
calling" (https://developers.openai.com/api/docs/guides/function-calling).

**(b) State.** Three options: manual client-side replay; `previous_response_id`
(requires `store: true`); or the Conversations API for a durable object
(https://developers.openai.com/api/docs/guides/conversation-state).

**(c) Reasoning replay.** "When doing function calling with a reasoning model in the
Responses API, we highly recommend you pass back any reasoning items returned with the
last function call… If the model calls multiple functions consecutively, you should pass
back all reasoning items, function call items, and function call output items, since the
last `user` message." Extra items are safe: "Our systems will smartly ignore any
reasoning items that aren't relevant"
(https://developers.openai.com/api/docs/guides/reasoning). `reasoning.context` controls
rendering (`current_turn` / `all_turns` / `auto`).

**(d) Caching.** Automatic prefix caching; **1,024-token minimum** for GPT-5.6+;
**write 1.25×, read 0.1×** of the uncached input rate. Up to four cache writes per
request. "Disable tools via `tool_choice: "none"` rather than removing definitions" — the
cache-safety rule again (https://developers.openai.com/api/docs/guides/prompt-caching).

**(e) Compaction is now an API primitive.** Responses exposes `context_management` with
type `compaction` and a `compact_threshold`, plus a `/responses/compact` endpoint.
Compaction "replaces earlier conversation content with a compacted context", trading
cache reuse for window room. The Agents API provides "automatic context compaction"
(https://developers.openai.com/api/docs/guides/agents).

**(f) A `phase` field** on assistant messages: "preserve and resend `phase` on all
assistant messages — dropping it can degrade performance"
(https://developers.openai.com/api/reference/resources/responses/methods/create).

**Addled parallel:** Addled speaks Chat Completions, which OpenAI now documents as the
*less* capable path. Addled has no analogue of reasoning items, `phase`, or
`reasoning.context` — it keeps a flat `reasoning_content` string.

## 2.4 Hermes

**"Hermes agent" is ambiguous, and I resolved it against a real install.** This
machine has a Hermes checkout at `%LOCALAPPDATA%\hermes\` with `hermes-agent/agent/`
(`prompt_builder.py`, `system_prompt.py`, `prompt_caching.py`, `prompt_cache_boundary.py`,
`hermes_state.py`), matching the documented architecture
(https://hermes-agent.nousresearch.com/docs/developer-guide/architecture). There are
three distinct things: the Hermes LLM family, the prompt format, and the **Hermes
Agent** harness (MIT, model-agnostic).

**(a) Prompt format.** Hermes 2/3 use **ChatML** (`<|im_start|>`…`<|im_end|>`); Hermes 4
switched to **Llama-3 headers** (`<|start_header_id|>`…`<|eot_id|>`) with `<think>`
reasoning — a real breaking change
(https://huggingface.co/NousResearch/Hermes-4-70B).

**(b) System prompt — three cache tiers.** *Verified in the local source*,
`build_system_prompt_parts` (agent/system_prompt.py:659) returns exactly three tiers:

- **stable** — identity (`SOUL.md`), guidance, coding brief, pinned skills;
- **context** — caller `system_message`, project context files, workspace snapshot;
- **volatile** — skills index, memory, user profile, external memory, timestamp,
  runtime hints.

The docstring states the purpose outright: tiers are ordered so **"implicit
longest-prefix caches keep the unchanged scaffold"**, and the prompt is **"Never
re-rendered mid-session."**

**(c) Tool format.** JSON inside XML tags, trained, not OpenAI's shape: tools declared
in `<tools>`, calls emitted as `<tool_call>{"name":…,"arguments":…}</tool_call>`,
results returned in a `tool` role wrapped in `<tool_response>`. The tags are **single
added tokens** "to assist with agentic capabilities in parsing while streaming"
(https://huggingface.co/NousResearch/Hermes-3-Llama-3.1-8B).

**(d) Cache boundaries — the most interesting module I read.**
`agent/prompt_cache_boundary.py` (70 lines) lets a builder **register its stable prefix**
so the cache planner places an Anthropic breakpoint at exactly that boundary. The
docstring explains why re-parsing markers is avoided: "markers can legitimately appear
inside skill bodies, and any delimiter heuristic then shrinks the cached prefix or
absorbs volatile bytes." It also enforces the tail be non-whitespace "so the split never
yields an empty text block (Anthropic rejects it with HTTP 400)."

**(e) Memory is bounded, and frozen to protect the cache.** `MEMORY.md` (2,200 chars)
and `USER.md` (1,375 chars) are injected as a **snapshot at session start** and never
change mid-session: "In-session writes persist to disk immediately but only appear in the
prompt next session" (https://hermes-agent.nousresearch.com/docs/user-guide/features/memory).
There is **no auto-compaction** — when a write would exceed the cap the tool errors and
the agent must consolidate.

**(f) Caching.** "Built-in cross-session 1-hour prefix cache for Claude on native
Anthropic, OpenRouter, and Nous Portal. Always-on; no configuration required"
(https://hermes-agent.nousresearch.com/docs/user-guide/features/overview).

**Addled parallel:** This is the sharpest contrast in the whole report. Addled and
Hermes have the **same problem shape** — a large system prompt plus per-turn memory —
and opposite solutions. Hermes freezes memory at session start and orders tiers for
longest-prefix reuse; Addled re-derives every block every turn, with retrieval gates that
change the content per message, and caches nothing. Addled's relevance gating is
*nearly incompatible* with prefix caching by design: the better the gate works, the more
the prefix changes turn to turn.

## 2.5 Gemini / ADK

**(a) Instruction assembly.** An `LlmAgent` takes a single `instruction` (string or
provider function), but the ADK **composes** it at runtime with `static_instruction`
(session-long, cache-friendly), `GlobalInstructionPlugin` (shared rules across all
agents), and `{var}`/`{artifact.var}` state templating — and rebuilds it per invocation
from the **event stream**, which is ground truth. "LLM context is an orchestrated
view… filtered and transformed from the event stream, not a dump of the log"
(https://adk.dev/agents/llm-agents/).

**(b) Sessions, state, memory, artifacts — four distinct stores.**
- **Session** = events + a `state` dict.
- **State** = serializable key-value scratchpad, **prefix-scoped**: no prefix
  (session), `user:` (all that user's sessions), `app:` (all users), `temp:` (one
  invocation) — https://adk.dev/sessions/state/.
- **Memory** = a searchable cross-session archive (`add_session_to_memory`,
  `search_memory`), surfaced via a `load_memory` tool
  (https://adk.dev/sessions/memory/).
- **Artifacts** = named, **versioned binary** data, separate service from state
  (https://adk.dev/artifacts/).

**(c) Caching.** Gemini now leads with **implicit caching, enabled by default** for 2.5+
models with `usage.total_cached_tokens` reported; minimums are model-dependent (2,048
tokens for 2.5 Flash/Pro). **Explicit** `CachedContent` objects still exist, with an
immutable `contents`/`tools`/`systemInstruction` and a `ttl`
(https://ai.google.dev/api/caching). ADK wraps this in `ContextCacheConfig(min_tokens,
ttl_seconds=1800, cache_intervals=10)`, configuring caching **at the App level**
(https://adk.dev/context/caching/).

**(d) The context hook surface is callbacks** — six of them (`before/after_agent`,
`before/after_model`, `before/after_tool`). Returning `None` allows default behavior;
returning a value **short-circuits** — a `before_model_callback` returning an
`LlmResponse` "skips the LLM call", which is how you serve a cached response
(https://adk.dev/callbacks/).

**(e) Branch isolation and compaction.** Parallel siblings are isolated via a `branch`
field (dotted path like `parent.child@1`). Compaction runs when the app sets
`events_compaction_config`, and the Runner compacts **after** the invocation. Delegation
deliberately does **not** hand the sub-agent the coordinator's transcript.

**Addled parallel:** ADK's `state` prefix scoping has a direct analogue in Addled's
memory *kinds* (facts / profile / session / wiki) — but Addled's are separate stores with
separate retrieval rules, not one scratchpad with scoping. ADK's callback short-circuit
is the cleanest analogue of Addled's `system_role.prepare` interception point.

---

# Part 3 — Cross-cutting comparison

| Dimension | **Addled** | Claude Code | DeepSeek | OpenAI | Hermes | Gemini/ADK |
|---|---|---|---|---|---|---|
| System prompt | One concatenated `str`, ~11 blocks | 4 cache-ordered layers; CLAUDE.md is *not* in it | Prompt sections (plugin) | `instructions` + developer msgs | 3 tiers: stable/context/volatile | Composed per invocation from events |
| **Prompt caching** | **NONE (measured)** | 4 breakpoints, 5m/1h | Automatic, 25–50× cheaper | Auto, 1,024-tok min, 1.25×/0.1× | 1h prefix cache, always on | Implicit by default + explicit TTL |
| Compaction | Rolling summary block; live list still raw | ~967K window; cache-safe fork | Automatic (harness) | `context_management` + API endpoint | `/compress`, no auto-compaction | `events_compaction_config` |
| Retrieval | **Relevance-gated injection (0.34 / 0.35)** | JIT glob/grep over pre-loading | — | file_search tool | Frozen snapshot + FTS5 + skills index | `search_memory` tool |
| Model routing | **Regex heuristics → 7 roles** | **Subagent with a different model** | Model per profile | Agents SDK handoffs | Model-agnostic config | `LlmAgent` per branch |
| Reasoning replay | `reasoning_content` string, echoed back | Signed blocks, 400 if altered | **Required with `tools`, 400 otherwise** | Reasoning items since last user msg | `<think>` retained | Not surfaced |
| System-role handling | **Detects silent drops, folds into user turn** | — | — | — | — | — |
| State | Client-side only | Client-side | Client-side (**even Responses**) | Server-side optional | SQLite state.db | SessionService |

## Where Addled is genuinely strong

1. **Relevance-gated retrieval.** Nobody else in this set gates *injection* on
   measured relevance with a documented false-positive cost model. Claude Code's answer
   is JIT tool calls; ADK's is an explicit `load_memory` tool. Addled's is automatic and
   costs no round trip.
2. **The `system_role` fold.** I found no equivalent anywhere. It solves a real,
   measured problem (9router adding 0 `prompt_tokens` for a 200-word system message) that
   other harnesses would simply fail on.
3. **The capabilities block derived from the tool list.** The comment records the exact
   failure it prevents — a model holding `meeting_*` schemas claiming no such function
   exists. Gemini/ADK explicitly advises "Don't just list tools; explain *when* and *why*"
   but leaves it to the author; Addled generates it from truth.
4. **Measured, recorded bug-fixing.** Almost every non-obvious decision in this codebase
   carries the measurement that produced it ("killed 2 of 8 live runs", "516, 564 and
   1920 characters", "9063 chars of reasoning, 0 of content"). That is better
   engineering documentation than any of the five comparison systems ships publicly.

## Where Addled is behind

1. **No prompt caching at all — and it is the only system here without it.** This is the
   single largest cost and latency gap. Addled rebuilds ~10KB+ of prompt every turn and
   pays full price on every token, while DeepSeek's cache hits are 25–50× cheaper and
   OpenAI's reads are 0.1×. Worse, the *static* portion (persona + capabilities, ~3.2KB)
   is identical every turn and is exactly what would cache for free.
2. **The retrieval gate and caching are in direct tension.** Hermes identified this and
   chose frozen memory. Addled chose fresh retrieval. Both are defensible, but Addled
   pays a price it has not accounted for.
3. **Chat Completions only.** No Responses API, no `previous_response_id`, no typed
   reasoning items — and OpenAI now documents Responses as both cheaper and (3%
   SWE-bench) better for reasoning models.
4. **Regex routing decisions.** Claude Code switches models by *delegating*; OpenAI uses
   SDK handoffs. Addled decides the model by counting question marks and characters.
5. **The context window is misconfigured on the live install.** Measured: `9router` →
   fallback 32,768 tokens, while the model behind it has 1M. Addled trims messages it did
   not need to trim, silently.
6. **No context-budget visibility.** The prompt is a `str`; nothing knows what any block
   costs, so nothing can report "the wiki block is now 5.5KB". Hermes and ADK both expose
   per-block accounting.

## Two concrete, low-risk wins

Both follow directly from the measurements above, and neither changes any behaviour the
user sees:

1. **Fix `context_limit("9router")`.** It resolves to 32,768 while the live model has
   1M, so history is trimmed for no reason. Adding the provider to `CONTEXT_LIMITS`, or
   reading the window from the model catalog, is a one-file change.
2. **Cache the static prefix.** The persona + capabilities block (~3.2KB) is
   byte-identical every turn. Placing it first and stable is exactly Hermes's stable tier
   and Claude Code's layer 1 — and it is the prerequisite for any caching work, since a
   cache needs a stable prefix to hit.

A third, smaller one: **delete the duplicated comment block** at
`backend/providers/openai_provider.py:191-218`.

---

# Part 4 — Sources

**Addled:** `backend/ws_server.py` (1540-1803), `backend/providers/{base,router,system_role,budget,openai_provider}.py`,
`backend/memory/relevance.py`, `backend/tool_brief.py`, `backend/skills/tool_loop.py`,
`backend/config.py` (305-362). Sizes measured by running the code against the live
install's store at `%LOCALAPPDATA%\Addled`.

**Claude Code:** https://code.claude.com/docs/en/{memory,context-window,prompt-caching,sub-agents,tools-reference,hooks,model-config,how-claude-code-works,glossary} ·
https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything ·
https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents ·
https://platform.claude.com/docs/en/build-with-claude/{prompt-caching,thinking,thinking-tool-workflows}

**DeepSeek:** https://api-docs.deepseek.com/{guides/thinking_mode,guides/kv_cache,guides/responses_api,guides/tool_calls,quick_start/pricing} ·
https://github.com/deepseek-ai/deepseek-harness (+ `docs/architecture.md`, `AGENTS.md`)

**OpenAI:** https://developers.openai.com/api/docs/guides/{migrate-to-responses,conversation-state,reasoning,function-calling,prompt-caching,agents} ·
https://developers.openai.com/api/reference/resources/responses/methods/create

**Hermes:** https://huggingface.co/NousResearch/Hermes-{2-Pro-Llama-3-8B,3-Llama-3.1-8B,4-70B} ·
https://github.com/NousResearch/{Hermes-Agent,Hermes-Function-Calling} ·
https://hermes-agent.nousresearch.com/docs/user-guide/features/{overview,memory,skills,personality} ·
**plus direct reading of the local install at `%LOCALAPPDATA%\hermes\hermes-agent\agent\`**

**Gemini/ADK:** https://ai.google.dev/{gemini-api/docs/caching,api/caching} ·
https://adk.dev/{agents/llm-agents,sessions,sessions/state,sessions/memory,artifacts,context/caching,callbacks} ·
https://github.com/google/adk-python

**UNVERIFIED items carried forward:** Claude Code's exact cache-breakpoint layout and
its thinking-block persistence on `--resume`; the "~92%" compaction figure; DeepSeek's
minimum cacheable prefix; any official DeepSeek "recommended prompt structure" doc.
