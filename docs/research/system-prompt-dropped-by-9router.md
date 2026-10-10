# The system prompt is dropped by 9router — root cause (2026-10-09)

## Symptom

Every system-prompt feature in Addled silently does nothing on this machine:
meeting recall, and the long-standing "no tool is ever called live" behaviour.
The model behaves as if it never received instructions or context.

## Root cause

**9router (v0.5.95) discards the `system` role before forwarding upstream.**
It is a local Node proxy on `localhost:20128` fronting the model. Not an Addled
bug, not a provider-model bug.

## Evidence

### 1. Addled sends it correctly

Intercepted the outbound request at a fake OpenAI-compatible server. Addled's
`openai_provider.chat_stream` posts the body verbatim:

```json
{"model": "Voxagent",
 "messages": [{"role": "system", "content": "SYSTEM-MARKER-ALPHA you must obey"},
              {"role": "user",   "content": "USER-MARKER-BETA hello"}],
 "max_tokens": 50, "temperature": 0.7, "stream": true}
```

`role: system` is present and intact on the wire. The drop is downstream.

### 2. 9router drops it — proven by token accounting

`prompt_tokens` for a 200-word message, by role:

| request | prompt_tokens |
|---|---|
| user message only | **15** |
| + 200-word `system` message | **15**  ← *unchanged: dropped* |
| + 200-word `developer` message | **15**  ← *also dropped* |
| + 200-word *user* message | **418** ← counted |

A 200-word system message adds **exactly zero** tokens. The same text in a user
turn adds 403. Stable across repeats. `developer` is dropped too.

### 3. The code

From the shipped bundle
(`%APPDATA%/npm/node_modules/9router/app/.next-cli-build/server/chunks/5330.js`):

```js
for (let d of a) {
  let a = String(d.role || "user");
  "developer" === a && (a = "system");
  let e = "";
  ... // flatten content
  e.trim() && ("system" === a ? b += e + "\n"        // <- hoisted into `b`
              : ("user" === a || "assistant" === a) && c.push({role:a, content:e}))
}
```

The `system` branch **accumulates into `b`** and never pushes into the forwarded
message list `c`. And the downstream builder that consumes the split:

```js
{systemMsg: b, history: c, currentMsg: d}(C)

F = function(a, b, c) {
  if (b) return a.currentMsg;                        // <- early return
  let d = {}, e = [];
  a.systemMsg.trim() && e.push(a.systemMsg.trim());  // unreachable when b is set
  ...
}
```

When `b` is set the function returns **only the current message** and the
collected `systemMsg` is never re-attached. That is the drop. It is a bug in
9router's token-saving message-splitting path, not a documented mode.

### 4. Working controls

- "Reply with exactly BANANA" in the system prompt → "Hello! How can I help you today?"
- Planted secret `ZEBRA-7741` in the system prompt → "there isn't one"
- Same secret in the **user** turn → `ZEBRA-7741` (exact)
- Meeting block in the system prompt → ignored; in the user turn → answered correctly

Conversation **history is kept** (a number planted in an earlier turn was
recalled), so this is specific to the system role, not to context in general.

## Consequences

- **Every system-prompt feature is inert on this configuration.** That includes
  meetings recall, memory/facts/profile/wiki/SOP blocks, and the tool catalogue
  that makes tool-calling work — which explains "no tool is ever called live".
- **Do not read this as a defect in any of those features.** They build correct
  prompts; the proxy discards them.
- A provider that honours the `system` role would use all of it unchanged. The
  `local_llm` path is declined on this machine, so there is no such provider
  available here today.

## Options

1. **Fix/upgrade 9router** — report upstream (`github.com/decolua/9router`); the
   bug is in their message splitter. Upgrading past 0.5.95 may or may not fix it.
2. **Toggle RTK off** — **TESTED, DOES NOT HELP.** `rtkEnabled` and
   `headroomEnabled` were both set to false in turn (authenticating with
   the package's own CLI client, and confirming the toggle really flips:
   `true -> false -> true`). The system role was dropped in every combination.
   **The drop is unconditional** — it is in the always-on message-splitting
   path, not behind either token-saver flag.
3. **Addled-side fallback** — **BUILT.** `backend/providers/system_role.py`
   detects the drop from `tokens_in` (no extra request — the usage is already in
   every response) and, only for a provider KNOWN to drop it, folds the system
   text into the first user turn. The verdict is per provider and persisted to
   settings, so it is learned once rather than re-learned (and lost) every
   launch. Providers that honour the system role are untouched.

   Tested by `scripts/check_system_role.py` (43 checks), revert-verified.

## The detection, and why it is the token count

Not a canary: asking the model to echo a marker costs a call and assumes the
model obeys — and on this endpoint the model never received the instructions to
obey. The endpoint's own `usage.prompt_tokens` cannot be talked out of it.

A system prompt of N characters needs at least N/6 tokens even for a
token-hungry tokeniser. If a request's whole `prompt_tokens` comes in under
that, the system text was not in the request. Measured stable: delta exactly 0
across every run.

Unknown stays "honours it": folding when it is not needed rewrites the prompt
for no reason, so only a positive detection flips the behaviour.

## The fold broke the reply-language rule, and the suite caught it

Worth recording because the failure was invisible until the whole suite ran.

**What happened.** The fold first *prepended* the system text to the user turn —
the obvious placement. That pushed the user's own question down, and
`check_reply_language.py` asserts:

```
sent.startswith(INDONESIAN)          # the user's question leads the turn
"read_file(" not in sent             # the catalogue is a separate message
id_directive in sent                 # the language line rides the turn
```

It passed `check_system_role.py` (presence, not position) and failed
`check_reply_language.py` — which had not run since the change. **The full suite
is what caught it**, and it would have shipped otherwise.

**Why position matters, not just presence.** The turn is thousands of tokens of
English tool catalogue. An instruction that pins the reply language only works
from the front of the turn; buried, the model answers in English — the exact bug
`backend/language.py` exists to fix.

**The fix.** The fold now APPENDS after the user's words (`_append_instructions`).
Nothing needs to be first: the catalogue sets the format, the question states the
task, the language line pins the language, and instructions arriving after all
three are still instructions.

**Also fixed in passing:** an empty user turn now still carries the instructions
instead of producing a blank first message.

**The lesson.** A change that alters where text sits in a prompt is a change to
every rule that depends on position — and those rules live in other suites. Run
the whole suite, not the one you were editing.
