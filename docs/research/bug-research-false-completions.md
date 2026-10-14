# Bug Research — False Completions, Broken System Tools, and the Failing Provider

Date: 2026-10-14.
Live provider: **`9router`** (`http://localhost:20128/v1`, `default_model: Voxagent`)
→ routed to `deepseek-v4.1-flash` via OpenRouter.
Workspace: `E:\Kunoir\Codeground`. **Nothing here is committed.**

---

## 0. The finding that reframes everything: the provider was failing

Every conclusion below was reached while the live provider was **intermittently
unavailable**, which is why the symptoms were erratic rather than repeatable.

Measured, in order:

| Test | Result |
|---|---|
| Raw `httpx` POST, `model=Voxagent`, `max_tokens=100` | **Works** — returns `PONG` |
| Raw `httpx` POST, `model=deepseek-v4.1-flash` | **402** — "This request requires more credits… You requested up to 300 tokens, but can only afford 49" |
| `provider.chat(...)` × 12 | **12/12 empty** (`ok=True`, no content, no error) |
| Same, 3 minutes later × 18 | **18/18 OK** |

The gateway **floors every request to 300 tokens** and the account had ~49
affordable. The 402 is surfaced as **HTTP 503** by the proxy, and when it hits
mid-stream the provider returns `ok=True` with **empty content**.

**Consequence for this investigation:** the live turns that "fabricated" were
running on a provider that returned nothing, intermittently. That is a genuine
application defect (see §3 — an empty round must not pass as an answer), but it
is *not* the model inventing results in a healthy system. Some turns were
flawless in the same run for exactly this reason.

Also observed: the gateway sometimes returns **garbled text**
(`"Hi! How can#include <stdio.h>"`), another sign of a degraded route.

**Recommended user action:** switch the active provider (Settings → Providers)
to something with credit, or point `9router` at a model it can actually fund.
Nothing in the code can fix an unfunded account.

---

## 1. BUG — `chat_stream` never received the tool catalogue  (FIXED, unverified)

### Evidence

`openai_provider.chat_stream` (was line 300) built its payload from
`model, messages, max_tokens, temperature, stream` — **no `tools` key**, and the
signature had **no `tools` parameter**. Its batch counterpart `chat` takes and
sends `tools` normally.

`tool_loop._streamed_answer` called it as:

```python
async for piece in provider.chat_stream(
        messages, model=model, max_tokens=max_tokens, temperature=0.7):
```

The live app **always** takes the streaming path — `ws_server.py:2177` passes
`on_delta=_on_delta` on every turn.

Asked a tool-shaped question **with no tools offered**, the model's reply is
text-only, and `_stream_round` accepts it as the final answer because there is
no call syntax to detect. Measured: **1/20 turns** leaked, with the reply
captured verbatim —

> *"I don't have a `delete_file` tool available. The tools I have for
> filesystem work are `run_command`, `read_file`, `write_file`…"*

The model was **correct** — the tools genuinely were not in the request.

### Fix applied (in the repo, NOT deployed)

- `providers/base.py` — `chat_stream` gains `tools: list[dict] | None = None`.
- `providers/openai_provider.py` — `chat_stream` builds a `payload` dict and adds
  `tools` (plus `tool_choice: "auto"`) **only when non-empty**, so a provider
  that does not understand the key keeps the exact request it had. It already
  yielded `STREAM_TOOL_CALL` when a `delta.tool_calls` appeared.
- `tool_loop._streamed_answer` — new `tools` parameter, passed to `chat_stream`
  behind a `TypeError` guard so an un-updated provider falls back to the old call.
- `tool_loop._stream_round` — passes `skill_registry.to_openai_tools(only)`.

Verified in isolation against the repo code: `_stream_round` now returns **None**
(falls back to the batch path, where tools work) instead of leaking text.
**Not yet verified end-to-end** — the provider was failing during the window.

### Risk

Low. Absent tools ⇒ identical payload. A provider that rejects the keyword
raises `TypeError`, which is caught and retried without it. Worst case is the
previous behaviour.

---

## 2. BUG — `set_brightness` always reports success; `set_volume` cannot work

`backend/actions/system_controls.py`:

```python
subprocess.run(["nircmd", "setsysvolume", ...], capture_output=True)
return {"success": True, "level": level}          # <-- unconditional
```

- `nircmd` is **not installed** (`WinError 2`). `set_volume` only reports failure
  by accident — the missing binary raises, and the bare `except` catches it.
- **`set_brightness` is worse.** It swallows everything:

```python
subprocess.run(cmd, shell=True, capture_output=True)
return {"success": True, "level": level}          # <-- ALWAYS
```

Measured: the PowerShell returns **exit code 1**, `Get-WmiObject : Not
supported` (no DDC/CI on this monitor). The tool still returns
**`success: True, level: 50`**. It is wrong **100% of the time** here.

### Fix to apply

Check the return code and return the real error, for both. A tool must never
report success it did not achieve — that is the same lie class as §1.

---

## 3. GAP — no guard against a completion claimed but never run

`tool_loop.py:693` documents the hole in its own words:

> *"I'll run it" is a plan, "I ran it" is a false completion (a different
> defect), and "running it now" is the one this catches.*

Two guards exist, both keyed on **future tense** via `_announced_work`:

- promise nudge (`MAX_PROMISE_NUDGES = 2`) — "I'll do it"
- verify nudge (`MAX_VERIFY_NUDGES = 1`) — files changed, nothing verified

**Nothing** handles **"I did it"** in a turn where nothing ran. A past-tense
claim with zero tool calls passes straight through to the user.

This is independent of §1, and it is the guard that would have caught every
fabricated turn even with a broken provider.

### Design constraint (from bug 2 in the prior session)

A verb whitelist **fails** — measured 7/33 honest replies destroyed, because
"Let me list my skills: file editing" contains `edit`. The existing fix is a
**clause-scoped veto** (`_without_discourse_clauses`). Any new guard must be
equally conservative: a false positive here rewrites an honest answer, which is
worse than the bug.

### Fix to apply

A third nudge, mirroring the promise one, for the **past-tense completion claim
with no tool result this turn**. Bounded, and only for the shape where tools
were offered and nothing ran.

---

## 4. BUG — `write_file` cannot write an empty file

`content` is in the schema's `required` list, and `registry.py:198` treats an
empty string as missing:

```python
missing = [k for k in required if k not in given or given.get(k) in ("", None)]
```

So `write_file(content="")` is rejected before reaching `FileOps.write`, which
would happily create a 0-byte file. Confirmed the model's report was accurate.

---

## 5. NOT A BUG — guidelines reported "0 chars"

The live turn claimed the packs were *stale, 0 chars, never fetched*. Running
the real handler (`guidelines._guidelines_status`) in a fresh process:

```
active_for_code: ['ponytail', 'karpathy']
Ponytail: on, level=full, scope=code, 2721 chars, updated 2026-10-09T07:42:02
Karpathy guidelines: on, level=full, scope=code, 2345 chars, updated 2026-10-09T07:42:02
```

Both packs are cached with real text. **The live report was fabricated** — the
model invented tool output. Another instance of §3.

---

## 6. Grants work; routing is the issue

| Name | Granted | Protected | Effect |
|---|---|---|---|
| `delete_file` | **True** | False | No prompt |
| `write_file` | **True** | False | No prompt |
| `move_file` | **True** | False | No prompt |
| `run_command` | False | **True** | **Never permanently grantable** — prompts every fresh process |

The prompt the user saw was **not a broken grant**. It was the model routing a
delete through PowerShell. The capability block in the system prompt encourages
this: *"Chain commands with `';'`… safe ones run immediately, and a destructive
one … asks the user to approve it first, so say it is waiting for approval."*

---

## Fix order

1. **§1 — pass tools to the stream.** Root cause. Removes the leak.
2. **§3 — false-completion guard.** Catches what remains, including a dead provider.
3. **§2 — return real errors from `set_*`.** Small, self-contained.
4. **§4 — allow empty content.** Schema fix.

Each gets a check in `scripts/` before it is called done.
