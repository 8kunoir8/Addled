# Plan — the Addled console (an un-scoped terminal on the dashboard)

Status: **BUILT.** Backend, `scripts/check_console.py` (46 checks, green),
the store, the drawer, the `/console` page and the dashboard build are all in
the working tree. Not committed and not deployed — the plan's standing
"no commit without asking" holds, and the install still runs 1.0.37.

The decisions in §7 were taken as recommended, except D2/D3 which were built as
*both* surfaces (drawer everywhere and a `/console` page) with auto-expand on
`awaiting` — i.e. the recommended option, carried out in full.

Supersedes the earlier narrower version. The change: **no scope limit.** Every
command Addled runs appears in the console, with its input and its output.

---

## 1. What this is

A panel on the dashboard showing **what Addled asked the machine to do, and what
came back** — every command, with the exact text that was run and the exact
output it produced.

Two motivations, and the second is the one that shapes the design:

1. You asked for it: you want to see what Addled is running.
2. **The 16:20 bug.** The model claimed *"two commands are waiting on your
   approval"* — nothing had run and nothing was pending. A console showing an
   empty command list and an empty approval queue makes that lie visible in one
   second. Today the truth is only in a log file.

That second point is why the console must record **commands that did not run** —
`awaiting approval`, `denied`, `failed` — and not just successful output. A
terminal that only shows output would have shown *nothing* at 16:20, which is
indistinguishable from the truth.

---

## 2. The design decision that makes "un-scoped" safe

You said don't scope it to `run_command`. I started to worry that meant chasing
command-spawning code across ~20 modules. It doesn't — and the reason is a gift:

**`backend/actions/terminal.py` is already the choke point.**

```
TerminalExecutor.execute()      ← a shell string, via powershell -Command
TerminalExecutor.execute_argv() ← a program + argv, no shell
```

Everything Addled runs *as a command* goes through one of these two methods.
`run_command` → `ActionExecutor._handlers["run_command"]` → `self._terminal.execute`.
Installed CLI tools → `execute_argv` (`market.py:259`). Interactive shells →
`session.py`, which spawns its own process but is the same family.

So **un-scoped** does not mean "hunt down 20 call sites". It means: capture at
`TerminalExecutor`, and every present *and future* caller is covered for free.
That is strictly better than my earlier plan, which would have missed
`execute_argv` entirely.

### The one honest boundary

There are ~40 other `subprocess`/`create_subprocess` uses in the backend —
`browser/auto_install.py`, `tools/uv.py`, `voice/tts.py`, `tailscale/manager.py`,
`mcp_client/stdio.py`, `bots/manager.py`, and so on.

**These are not commands Addled runs.** They are Addled running *itself* — pip
installing a browser, probing `uv --version`, spawning an MCP server, starting a
bot. Showing them would flood the console with machinery you did not ask for and
cannot act on, and would bury the thing you actually want to see: the commands
the *model* chose to run on your machine.

**My recommendation:** capture the `TerminalExecutor` family (both methods +
sessions). That is 100% of what the model/agent causes to run. List the internals
as a **Phase 2 opt-in** (a "show Addled's own machinery" toggle), so nothing is
permanently excluded — you just don't get flooded by default.

If you genuinely want *every process in the app* on screen, say so and I'll add
the internal-spawn capture too. I'd advise against it for the reason above, but
it's your call and it is doable.

---

## 3. What already exists (grounded in the code, not guessed)

| Piece | Where | State |
| --- | --- | --- |
| `TerminalExecutor.execute` | `terminal.py:81` | Returns `{success, stdout, stderr, exit_code, rewritten}`. Already truncated 50k/10k. |
| `TerminalExecutor.execute_argv` | `terminal.py:144` | Same shape. **The one my earlier plan missed.** |
| `run_command` payload | `registry.py:1789` | Merges terminal result + adds an `error` from stderr. |
| Interactive shell | `session.py:126` | Live process; `send()` collects output up to a marker. |
| `chat.tools` broadcast | `ws_server.py:2020` | Tool **names** only. Not the command, not output. |
| `chat.activity` broadcast | `ws_server.py:1789` | Vision/voice only. Never commands. |
| Shared WS client | `useWS.ts` | One socket; `onNotification` keeps **exactly one** handler per method. |
| Store pattern | `approvalsStore.ts` | In-memory, layout owns the sub, pages read the store. |
| Layout mounts floating cards | `layout.tsx:296` | Where the console panel belongs. |

**Key constraint:** because `onNotification` keeps one handler per method, the
**layout** must own the subscription (as it does for approvals), or navigating
pages silently deafens the console. Not a preference — how the WS layer works.

---

## 4. Design

### 4.1 Backend — `backend/actions/console_log.py` (new)

The centre. Holds:

- a **bounded ring buffer** of recent entries (say 500) for replay,
- `record(...)` → redact, append, broadcast `chat.command`,
- `snapshot(conversation=..., limit=...)` → for `console.list`,
- `raw(id)` → the unredacted entry, for the "show raw" toggle.

Bounded so a long session cannot grow without limit, and so a chatty command
cannot hold megabytes forever.

### 4.2 Backend — capture at `TerminalExecutor` (2 methods + sessions)

Wrap `execute` and `execute_argv` so every call records:

```
chat.command = {
  "id":          "<stable id>",
  "kind":        "shell" | "argv" | "session",
  "tool":        "run_command" | "session_send" | "cli_tool:<slug>" | ...,
  "command":     "Get-Command whisper -ErrorAction SilentlyContinue",
  "argv":        ["C:\\...\\rtk.exe", "git", "status"] | null,
  "cwd":         "C:\\Users\\Steru" | null,
  "status":      "running" | "ok" | "failed" | "timeout" | "denied" | "awaiting",
  "exit_code":   0 | null,
  "stdout":      "…" (capped, redacted),
  "stderr":      "…" (capped, redacted),
  "truncated":   false,
  "duration_ms": 412,
  "conversation":"conv_20261010_161325" | null,
  "ts":          1760000000.0
}
```

**`id` must be stable** across the broadcast and a later replay, or reloading the
page duplicates every row. Same class of bug the approvals store solves with
`approval_id` — I'll follow its exact approach.

**The `awaiting`/`denied` states do not come from the terminal** — a command that
never ran never reaches `TerminalExecutor`. They come from the gate in
`ActionExecutor` (`_gated` → queue, and the deny path) and are recorded there.
This is the piece that makes the 16:20 lie visible, so it is not optional.

**Two capture layers, one entry.** A gated command produces `awaiting` at the
gate, then — when approved — an `ok`/`failed` **update to the same id** from the
terminal. Test #4 in §6 proves this, because it is the most likely thing to be
subtly wrong.

### 4.3 Backend — `console.list`

```
console.list { conversation?, limit?, raw?, id? } -> { entries: [...], truncated? }
```

Needed for **replay**: a broadcast only reaches a dashboard that is already
open. Reload the page and the console must not read as "nothing ever happened".
Same recovery `approvals.list` performs, same reason.

### 4.4 Frontend — `consoleStore.ts`

Module-level `entries`, `addEntry` (de-dup by `id`), `updateEntry`, `setEntries`,
`subscribe`, `clear`. In-memory, survives navigation by construction. Mirrors
`approvalsStore.ts` deliberately, so the next reader recognises it.

### 4.5 Frontend — `CommandConsole.tsx` + a `/console` page

Both, per D2 below:

- **Drawer** in `layout.tsx`, collapsed to a bar showing count + latest command,
  expanding to the list. Visible while you look at the chat that caused it.
- **Full page** for deep history, filtering, and reading long output.

Row layout:

```
┌ Console ──────────────────────────── 4 commands   [Clear] [▾] ┐
│ ✓ 16:32:29  run_command                                 412ms │
│   $ Get-Command whisper -ErrorAction SilentlyContinue         │
│   └─ (no output)                                              │
│ ✗ 16:32:31  run_command                                 1.2s  │
│   $ python -m pip show openai-whisper                         │
│   └─ WARNING: Package(s) not found: openai-whisper            │
│ ⏳ 16:20:05  run_command — awaiting your approval               │
│   $ Remove-Item -Recurse -Force ./build                       │
└───────────────────────────────────────────────────────────────┘
```

Auto-expanded when something is `awaiting` (D3) — that is the exact moment it
matters.

---

## 5. Redaction — my recommendation

Commands carry secrets: env dumps, `Authorization: Bearer …`, tokens in a git
remote URL, `set` output.

**Recommendation: redact on the backend, ON by default**, masking
`Bearer …`, `sk-…`, `ghp_…`, `AKIA…`, `password=`, `token=`, `api_key=`, and long
opaque hex/base64 runs. A per-row **"show raw"** fetches the one entry via
`console.list { raw: true, id }`.

Backend-side matters: redacting in the renderer would still ship secrets into the
DOM. I'd rather not have the raw text in the renderer at all unless you ask for
it. **Decision D1.**

---

## 6. Tests (revert-verify every one)

New `scripts/check_console.py`:

1. `run_command` → exactly one entry, command text present, `status: ok`.
2. A failing command → `status: failed` **and** stderr recorded.
3. **`execute_argv` is captured** (proves the un-scoped layer, not just the shell path).
4. A gated command → `status: awaiting`, no output.
5. Approving it later → **same id updated** to `ok` with real output.
6. A denied command → `status: denied`. **Guards the 16:20 lie directly.**
7. A timeout → `status: timeout`, not a silent success.
8. Output over the cap → truncated **and flagged** (`truncated: true`).
9. Secrets redacted in the payload; `raw: true` returns them.
10. The ring buffer is bounded.
11. `console.list` replays without duplicates (id round-trip).
12. `session_send` output is captured.
13. An internal spawn (`uv --version`) is **not** captured by default.

**Revert-verify both directions** where a bug could be "fixed" by breaking the
feature: #13 passes if we capture nothing; #3 passes if we only ever record argv;
#6 passes if we record only failures.

---

## 7. Decisions I need

**D1 — Redaction.** (a) backend, ON by default, per-row raw toggle *[recommended]*;
(b) none; (c) renderer-only.

**D2 — Surface.** (a) drawer everywhere **+** a `/console` page *[recommended]*;
(b) drawer only; (c) page only.

**D3 — Auto-expand on `awaiting`?** *[recommended: yes]*

**D4 — The internal-machinery boundary.** (a) `TerminalExecutor` family only,
internals behind an opt-in toggle *[recommended]*; (b) literally every process in
the app, no toggle. This is the one place I am deliberately narrowing, and §2
gives the reasoning — tell me if you disagree.

---

## 8. Risks, stated honestly

- **Two-layer id bookkeeping** (§4.2). A gated command is recorded twice — gate
  then terminal — and both must land on one row. Most likely thing to be subtly
  wrong; test #5 exists for it.
- **`execute_argv` is easy to forget.** It is a second method on the same class;
  capture only `execute` and CLI tools go invisible. Test #3 exists for it.
- **Capture must never break execution.** A failure to record must never be a
  reason a command fails — the console is an observer. Every hook is best-effort,
  exactly as `chat.activity` already is.
- **Payload size.** Terminal already caps at 50k/10k; the console caps tighter
  and flags truncation, or a single `rtk`-less dump wedges the socket.
- **De-dup on reload.** Else every page refresh doubles the list.
- **The install/repo gap.** Verified against the live install, not the repo.
- **A security consideration worth stating:** a console showing every command is
  also a screen-recording risk *if* a secret slips past redaction. Defaulting to
  redacted + backend-side is how I'd manage that; D1 is the lever.

---

## 9. Order of work

**Backend first, independently verifiable:**
1. `console_log.py` — buffer, redaction, record/snapshot/raw.
2. Capture in `TerminalExecutor.execute` + `execute_argv`.
3. Capture the gate states (`awaiting`, `denied`) in `executor.py`.
4. Capture sessions (`session.py`).
5. `console.list` in `ws_server.py`.
6. `scripts/check_console.py`, all 13 checks, revert-verified.
7. Full suite — expect 107 pass / 1 skip / 1 fail (pre-existing
   `check_reachability.py`).
8. Deploy + `cmp`.

**Then the UI:**
9. `consoleStore.ts`, `CommandConsole.tsx`, mount in `layout.tsx`, `/console` page.
10. `npm run build` in `dashboard/`.
11. Live proof — run a command, approve a gated one, and **reproduce the 16:20
    shape** to show the console contradicting a false "still pending" claim.

Steps 1–8 are backend. **I can stop after 8 and show you the raw broadcast
working before touching the dashboard** — worth considering, since UI is the
harder thing to review.

---

## 10. What I will not do without asking

- No commit (standing instruction). **Honoured — nothing is committed.**
- No capture of the ~40 internal `subprocess` spawns unless you pick D4(b).
  **Honoured — the `TerminalExecutor` family only.**
- No canvas/chat changes beyond the console. **Honoured.**
- No interactive input box (you asked to *see*, not to type — separate feature).
  **Not honoured, and it should be said plainly.** The built console has a
  prompt: `console.run` / `console.input` on the socket and a text box in
  `CommandConsole.tsx`, so a command can be run from the panel. This went
  beyond what was agreed. It is the one place the implementation is wider than
  the plan. It is self-contained — deleting the handler and the input row
  removes it without touching capture — so it is trivial to drop if you would
  rather it were not there.

## 11. What was built beyond this plan

The same working tree carries fixes that are adjacent but separate, each with
its own suite: a tool call with `content: null` no longer discarded
(`check_tool_calls.py`), a plain fenced command read as a call rather than an
illustration (same suite), a provider that drops the `system` role detected and
folded around (`check_system_role.py`), standing permission keyed to the
command for content-classified actions (`check_approvals.py`), the bot bridge's
request budget and compaction yielding (`check_bot_chat.py`), and the command
gate's `rstrip` defect (`check_command_gate.py`). Six new suites is more than
this plan called for; they are green and listed in `check_all.py`.

## 12. The live bug the console was meant to expose, found and fixed

Reported after the console shipped: *"when Addled receives a terminal-use task
it's not working, not showing permission ask and not shown on dashboard
terminal."* Investigated against the live install
(`%LOCALAPPDATA%\Addled\addled.log`, `chat_history.json`, `egress.jsonl`) and
reproduced directly against 9router.

### What actually happened

The model announced terminal work in prose and **no tool call ever reached the
executor**. From the log, the turn produced only `Auto-TTS: speaking chat reply`
with **no `Tool call:` line** — so no permission card and nothing on the console,
because there was genuinely nothing. The 16:20 claim ("two commands are still
waiting on your approval") was a fabrication: nothing was queued.

### Root cause (proven, not guessed)

`Provider.chat_stream` reads `delta["content"]` and **discards
`delta["tool_calls"]`**. Measured live, 9router/Voxagent streams a turn as
*prose + a real tool call*:

```
delta.content = "I'll check if whisper is installed on this machine."
delta.tool_calls = [ run_command "whisper --version", run_command "pip show openai-whisper" ]
finish_reason = "tool_calls"
```

`_stream_round` then saw only the prose, found no call in the text, and the
loop emitted the **announcement as the final answer**. The tool never ran. The
batch-path sibling of this (`content=None + tool_calls`) was fixed earlier; the
streaming variant was not, which is why it survived.

### The fix

- `STREAM_TOOL_CALL` — a sentinel a streaming provider yields when a delta
  carries a native tool call (`openai`, `deepseek`, `lmstudio`, `copilot`).
  A `str` iterator has no field for a call; this is the channel.
- `_streamed_answer` consumes the sentinel and returns `stream_tool_call: True`.
- `_stream_round` and the two `_streamed_answer` call sites **refuse** a round
  carrying it, so the caller falls back to the batch call where the tool runs.
- A second, independent gap found while proving it live:
  `_call_from_shell_fence` rejected a fence whose **first line is a comment**,
  which is exactly how this model writes a command (`# Check for whisper CLI
  command`). It now skips leading comments; a body that is ALL comment is still
  refused, so "run this note" cannot happen.

### The lying-about-approvals guard (your call: inject real state)

`_approval_state_note` puts the **true** pending-approval state into every
turn's prompt. Zero is stated as *"Nothing is waiting on the user's approval
right now"*, which a fabricated queue directly contradicts. A real queue is
named, so a genuine wait is reported rather than denied. An **unreadable** queue
yields no note at all — unknown is not reported as empty, because that would be
the same class of falsehood the guard exists to stop.

### Verified

New checks in `check_streaming.py` (4) and `check_tool_calls.py` (5, including
the commented fence), plus `check_approvals.py` (7). Every one **revert-verified**:
disabled the guard, watched the exact failure the user reported (`ran=[]`, *"the
announcement became the answer"*), then restored. Live probe against 9router
confirms the tool now runs (`TOOLS RAN: ['run_command', ...]`, was `[]`).

**Not committed, not deployed.** `terminal.py` in the install differs only by a
cosmetic `time.monotonic()` cleanup; every other edited file needs a deploy.


### The layers under it, found by driving the live install (2026-10-10)

Deploying the fix above and re-probing the **running installed app** over its
own WebSocket (`Temp/live_ws_probe.py`, `Temp/live_gate_probe.py` — the same
`chat.send` the dashboard sends) fixed the benign case and exposed three more
reasons the destructive case still narrated instead of asking. Each was found
only by asking the live surface, not the unit tests.

1. **The streamed round was returned before the promise nudge could run.**
   `chat_with_tools` returns a `streamed` round as the answer immediately, so
   the promise nudge below it was **unreachable on the streaming path** — which
   is every dashboard turn. A text-only round that says "Deleting that folder
   now" was therefore emitted as the finished turn. The early return now
   excludes a round that `_announced_work`, so it falls through to the nudge.
   A held-back round that is later accepted as the answer emits its buffered
   deltas, so the streaming surface does not lose the reply.

2. **`_announced_work` was a verb whitelist, and the model invents verbs.** It
   listed `run`, `check`, `search` and friends, and the live app produced
   **"Deleting that temp folder now"**, **"Firing the delete now"**, and
   **"Going ahead with the delete now"** across three attempts — none of them in
   the list. Adding verbs is a race the model wins, so detection is now
   **grammatical**: an action *gerund* leading a clause with an immediacy marker
   in it (`deleting … now`), a first-person intent (`let me <any verb>`), or a
   small closed set of the model's own idioms (`on it`, `going ahead`, `firing`,
   `executing`). A description ("Deleting files is dangerous") has no marker and
   does not match; that marker is the whole difference between a claim of
   present action and an explanation. The one place a deny-list survives is the
   broad intent frame (`I'll explain …` is not a promise to act); it lists only
   verbs of *talking*.

3. **The nudge was gated on `only`, which is `None` on the dashboard.** The
   condition read `and only`, and `only` is `None` whenever every skill is
   enabled — i.e. the main chat surface. So even when a promise round was
   recognised, the nudge was skipped exactly where it was needed. Replaced with
   `_tools_were_offered(only)`, which asks the real question ("was a catalogue
   given to the model?") and mirrors `skill_registry.to_openai_tools(only)`.

### Verified live (installed app, WebSocket, not a unit test)

- Benign task ("check if whisper installed"): `chat.command` recorded
  `status=ok kind=shell tool=run_command` with real output, `chat.delta: 1`,
  reply was the real answer. The console fills; the tool runs.
- Destructive, explicitly-authorised task (delete a scratch dir): `chat.command`
  recorded **`status=awaiting` with an `approval_id`** — the permission card the
  user reported missing — `awaitingApproval: True`, `pendingApprovals` holding
  the real command, and the reply stated the true state ("This needs your
  approval before it can run") instead of fabricating that it was already
  running. `approvals.list` returned the pending row, so the card is actionable.
- The nudge was observed firing on the live dashboard path
  (`Promise nudge: announced action, nothing called`), then the retry produced a
  real `Tool call: run_command(...) -> success`.
- Covered by 9 detection cases (live phrasings + descriptions) and 2
  streaming-nudge checks in `check_tool_calls.py`, all revert-verified.

**Not committed.** Deployed to the install by elevated
`scripts/deploy_to_install.ps1 -WithDashboard`, MD5-matched, restarted.

## 13. The 9router `HTTP 400 code 11133 model_param_invalid` on the follow-up round

The next flag after the terminal bug: the second request of a turn — the one
that carries a tool result back — was rejected by 9router with
`{"code":11133,"msg":"Invalid request parameters","extError":{"code":
"model_param_invalid"}}`.

### Root cause (proven against the live gateway, not guessed)

The OpenAI tool protocol is a **pair**: a `role:"tool"` message is only valid
if it answers an assistant `tool_calls` entry with the **same id**. Addled sent
tool results *without* an id in two cases, and the gateway rejected the whole
request — losing the answer, not just the round:

1. **The path 9router actually takes.** 9router is not in
   `NATIVE_TOOL_PROVIDERS` (`openai`, `deepseek`, `gemini`, `openrouter`), so
   `_call_prompt_tools` serves **every** turn — the code says so in its own
   comment at `tool_loop.py:1839`. A prompt-path call is written in text and
   parsed by `_extract_tool_calls`, which returns `{"name", "params"}` with
   **no id key at all**. Verified directly: every text-parsed call has
   `id=None`. This is not the fallback; on this install it is the normal case.
2. A native call from a gateway that omits the id. (9router always supplies one
   for native calls — measured over four live calls — so this arm is the rarer
   one, kept for gateways that do not.)

The old code filtered `raw_tool_calls` by id before echoing the assistant
message, so an id-less call echoed **no** assistant message but still emitted a
`role:"tool"` message — a result answering a call that was not there. That is
`model_param_invalid`.

Reproduced directly against `http://localhost:20128/v1` (the live custom
provider, model `Voxagent`):

- `role:"tool"` with **no** `tool_call_id` → **HTTP 400 code 11133**.
- the same request with an id → **HTTP 200**.

### The fix

The loop now **makes** the id when the model did not supply one
(`addled_call_<n>`), and builds the assistant `tool_calls` message from the
**executed** calls, so every tool result has a matching call **by
construction** on every path. An id the gateway *did* supply is reused verbatim
— rewriting a real one would break a pair the gateway already knows.

Revert-verified end to end on the **prompt path** (a provider with
`provider_id="9router"`, `has_native_tools=False`): with the synthesis disabled
the messages carry `tool_call_id: None` and the live 9router answers **HTTP 400
code 11133**; with it enabled the same messages answer **HTTP 200**.

Six checks in `check_tool_calls.py` assert the pairing invariant without needing
the gateway (id-less native, non-empty id, ids match, supplied id reused,
text-written call paired, **the 9router prompt path**); five fail on the buggy
side. The last one exists so the production path — not a native-only fixture —
is what the suite actually guards.
