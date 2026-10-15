# MCP: remediation plan

Derived from `mcp-deep-audit.md` and `mcp-management-comparison.md`. Phases 1
and 3.2 are implemented (see Progress below); the rest is ordered so the
highest-severity, lowest-risk work lands first.

## Progress

Branch `mcp-remediation`, based on `ea05fad`.

| Commit | What | State |
|---|---|---|
| `d37c888` | Carried-forward work from earlier sessions | done |
| `9d21fdf` | **1.1** SSE framing (F1) | done, verified with teeth |
| `c6a418a` | **1.2** Environment isolation (F2) | done, verified with teeth, opt-out in Settings |
| `e86be6c` | **3.2** Model cannot self-approve (F5) | done, verified with teeth, 11 assertions rewritten |
| `d975246` | Retargeted a stale `check_tools` assertion | done |
| `337b755` | **Phase 6** loopback callback + PKCE + state | done, verified with teeth, shared with 5.1a |
| `a5ca11e` | plan: record Phase 6, and the failures that are not ours | done |
| `07683cb` | **3.1** Approval does not survive a repoint (F4) | done, verified with teeth |
| `f228fb6` | **Phase 2** colliding tool names all stay reachable (F3) | done, verified with teeth, end to end |
| `e0fa82b` | plan: record Phase 2 (F3) done | done |
| `6ff8581` | **Phase 4** HTTP transport conformance (F6/F8/F9/F10) | done, verified with teeth |
| `7335400` | plan: record Phase 4 done | done |
| `f29cbfc` | **5.2** Credentials encrypted at rest (F7, DPAPI) | done, verified with teeth |
| `4336e6a` | **5.1a** OAuth client: discovery, DCR, PKCE, exchange | done, verified with teeth |
| `811f990` | **5.1b** 401 interceptor, refresh-once, manager surface | done, verified with teeth |
| `23cb984` | **5.1c** Authorize button; `oauth` market blocker | done, verified with teeth |
| `dca55ab` | **5.3a** per-tool disable | done, verified with teeth |
| `e6e9f29` | **5.3b** `last_error` redaction | done, verified with teeth |
| `696cbd3` | **5.3c** config import from other tools | done, verified with teeth |
| `874703c` | **F7** per-server credential keying | done, verified with teeth |

**Every finding is now closed.** F1–F11 are fixed and verified with teeth, and
5.1 (OAuth a/b/c), 5.2 (DPAPI) and 5.3 (a/b/c) are done. Nothing in this plan
is outstanding.

### Known suite failures, and which are not ours

Two suites fail on this branch. Neither is caused by this work, and both are
recorded here so a later run does not re-investigate them:

- `check_topic_gate.py` — "the store was large enough to be evidence". A
  data-size precondition; failed on `ea05fad` too.
- `check_gaming_pause.py` — "the real desktop is not misread as gaming". This
  reads the **live foreground window** and deliberately refuses to assert when
  a frameless one is in front (its own comment explains why). The agent's
  terminal is that window during an automated run, so it fails when run from
  the tooling and passes in a normal shell. Measured: the foreground window
  was this session's terminal at 1096x1888 with no caption. No file this
  branch touches relates to gaming detection.

**F5 note for whoever picks this up.** The fix is spread over five files on
purpose, and each seam is load-bearing — reverting any one of them re-opens
the hole or breaks the approve path:

- `approval.py` — the refusal no longer says how to get past the gate.
- `manager.py` — untrusted tools register with `requires_approval`; the
  handler refuses and no longer calls `approve()` on a flag; the
  skill→tool map lives here.
- `registry.py` — `_needs_approval` asks for `mcp__*` unconditionally (the
  DestructionGate cannot know dynamic names); `_is_granted` reads the MCP
  store.
- `tool_loop.py` — the turn ends on a refusal instead of nudging a retry.
- `executor.py` — `approve()` records the MCP grant, or the user's Allow runs
  nothing.

Every phase is independently shippable and independently revertible. Phases 1–3
are bug fixes; 4–5 are trust-model design decisions; 6+ are the competitive gaps.

---

## Assumptions I verified before planning

| Assumption | Verified how | Result |
|---|---|---|
| `sse-starlette` is what MCP servers actually use | read the SDK's `streamable_http.py` | ✅ **line 39: `from sse_starlette import EventSourceResponse`** |
| CRLF is that library's default | read `sse_starlette/event.py` | ✅ `DEFAULT_SEPARATOR = "\r\n"` |
| OAuth needs no new dependencies | imported in the bundled Python | ✅ `httpx`, `secrets`, `hashlib`, `base64`, `webbrowser`, `http.server` all present |
| A callback route exists to extend | read `http_api.py` | ❌ **`/oauth/callback` returns 404 — must be built** |
| An existing OAuth flow is correct enough to copy | read `calendar_integration.py` | ❌ **it is broken in two ways (see Phase 6)** |
| There is a secret store to reuse | grep for keyring/DPAPI | ❌ none exists; API keys are plaintext config too |

The last three change the shape of Phases 6–7: OAuth is **new work**, not
"copy the calendar module", and a keychain is new infrastructure that would
affect more than MCP.

---

## Phase 1 — the two critical bugs

### 1.1 SSE framing (F1)

**Change:** normalise line endings before splitting, in `protocol.py:142`.

```python
def parse_sse(body: str) -> list[dict]:
    # SSE permits CRLF, LF, or CR as the line terminator (WHATWG HTML §9.2.4).
    # sse-starlette — the streamable-HTTP layer of the official MCP SDK —
    # defaults to CRLF, so a body framed with it contained no "\n\n" and the
    # whole stream collapsed into one block, whose joined data: lines failed to
    # parse and were dropped. Normalising first makes the split correct for all
    # three terminators instead of only the one we happened to develop against.
    body = body.replace("\r\n", "\n").replace("\r", "\n")
```

**Risk: very low.** Pure normalisation; LF bodies are unaffected.

**Test:** extend `check_mcp.py` with a CRLF-framed two-event stream that must
parse to 2 messages. Add the exact bytes `sse_starlette` emits (captured from its
`encode()` logic) as a fixture, so the test tracks the real library.

**Acceptance:** a two-event CRLF stream yields 2 messages; the existing LF tests
still pass.

### 1.2 Environment inheritance (F2)

**Change:** stop passing the whole parent environment to stdio children
(`stdio.py:66`).

```python
# Variables a process needs to function, and nothing else. The previous
# `dict(os.environ)` handed every stdio server — third-party code, some of it
# auto-acquired on the model's initiative — the parent's ANTHROPIC_API_KEY,
# OPENAI_API_KEY, cloud credentials and the values entered for OTHER MCP
# servers. A server needs a PATH and a temp directory; it does not need the
# user's keys. Anything a specific server genuinely needs is declared in its
# own `env`, which is merged below.
_PASSTHROUGH = ("PATH", "Path", "PATHEXT", "SystemRoot", "windir",
                "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA",
                "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
                "PROGRAMDATA", "COMSPEC", "COMSPEC", "NUMBER_OF_PROCESSORS",
                "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "PYTHONIOENCODING")
```

**Risk: medium — this is the one change that can break working servers.**
A server relying on an inherited variable stops working. Mitigations:
1. Default to the allowlist, but add an opt-out (`mcp.inherit_env`, default
   `false`) so a user whose server breaks can restore the old behaviour without
   a code change.
2. Surface it: when a server fails to start with the allowlist on, the error
   should *name the likely cause* rather than reporting a bare exit code.
3. Ship it behind its own commit so it can be reverted alone.

**Test:** assert `PROBE_CANARY` in the parent does **not** appear in `_env()`;
assert `PATH` **does**; assert a declared `env` value **does**.

**Acceptance:** the four MCP test servers still start; no parent-only variable
reaches a child.

---

## Phase 2 — tool-name collisions (F3)

**Change:** detect collisions at registration and be honest about them.

1. In `_register_tools`, keep a dict of sanitised-name → original tool names.
2. On collision, **do not silently overwrite.** Register the first and record
   the rest as unavailable, surfacing them in `server_status()` so the card
   shows *"2 tools could not be exposed: my.tool, my/tool (names collide after
   sanitising)"*.
3. Give the model the real names where possible: if a collision is detected,
   disambiguate with a short hash suffix (`mcp__s__my_tool_a1b2`) rather than
   dropping the tool, so nothing becomes unreachable.

**Design choice to make:** suffix-and-keep vs report-and-drop. I recommend
**suffix-and-keep** — a tool that exists but has an ugly name beats a tool that
silently does not exist. Confirm before implementing.

**Risk: low.** Only affects servers that collide today (which currently lose
tools — strictly an improvement).

**Test:** the three-name fixture from `_pn/mcp_probe_collide.py` becomes a
permanent test asserting **3 tools → 3 reachable**.

---

## Phase 3 — the trust model (F4, F5)

These are **decisions, not patches**. I recommend stopping here for a steer
before writing code.

### 3.1 Approval must not survive a repoint (F4)

**Change:** include the endpoint in the approval key, and revoke on change.

- Key becomes `server_id::endpoint::tool`, where endpoint is the URL (http) or
  the resolved command (stdio).
- `update()` revokes approvals when `url`, `command`, `args`, or `env` change.

**Risk: low.** Standing approvals for legitimately-unchanged servers keep working.

### 3.2 The model must not grant itself approval (F5)

This is the one that needs a decision, because the current design is deliberate.

**Option A — separate the two jobs (recommended).** Keep `confirm` as the
*retry* mechanism, but require a real user action for the *grant*:
- The refusal result no longer instructs the model to re-call with `confirm=true`.
- Instead it returns `requires_approval` and the turn **ends**, surfacing a
  prompt (reusing the existing `_ask_to_acquire` / `question_id` machinery).
- The user's "Allow" writes the session approval; the model never can.

**Option B — keep it, but make the bypass visible.** Allow `confirm=true` but
record every self-granted approval and show it on the card. Cheaper, weaker.

**Option C — status quo with documentation.** Cheapest, and I would not
recommend it: the flag's own description says "set to true only after the user
has agreed", which is a promise the code cannot enforce.

**Risk (Option A): medium — it changes the tool-call loop for MCP tools**, so
it touches `tool_loop` as well as `manager.py`. Needs the `check_mcp.py`
confirm-retry assertions rewritten.

---

## Phase 4 — transport conformance (F6, F8, F9, F10)

Batch these; each is small and they share a test file.

- **F6** — make `McpHttpClient` use `JsonRpcSession` (or replicate its reply
  logic), so server→client requests are answered. Also stop `return`ing before
  later messages are read, so notifications after a result are captured.
- **F8** — send `MCP-Protocol-Version` on every request after negotiation, and
  handle a 400 that names a version mismatch by retrying with the older version.
- **F9** — treat **404 on a session-bearing request as "session expired"**:
  re-initialize and retry once. Send `DELETE` on disconnect.
- **F10** — `validate()` must reject unknown transports instead of coercing.
  Accept `stdio`, `http`, `streamable-http` (alias); **reject** `sse`, `ws`,
  anything else with a message naming what *is* supported. (If Phase 5 adds SSE,
  `sse` moves to the accepted list.)

**Risk: low**, except F9's retry-once needs a guard against a loop.

---

## Phase 5 — the competitive gaps

Ordered by value-to-effort, from the comparison research.

### 5.1 OAuth for remote servers (F11) — the real gap

**Effort: large. This is the single biggest piece of work.**

What the spec requires (all MUST, per 2025-06-18): parse `WWW-Authenticate` on
401; Protected Resource Metadata (RFC 9728) discovery; Authorization Server
Metadata (RFC 8414) with fallback; Dynamic Client Registration (RFC 7591);
PKCE S256; the `resource` parameter (RFC 8707); loopback redirect; token
storage and refresh.

**What is already here to build on:**
- `httpx` is available and already used by `http.py`.
- `webbrowser` is used by `avatar.py`.
- `http.server` runs the HTTP API on **9877**.

**What must be built:**
1. A real callback route on the HTTP API (9877) — it does not exist today.
2. `backend/mcp_client/oauth.py`: discovery, DCR, PKCE, code exchange, refresh.
3. Token storage — see 5.2.
4. A 401 interceptor in `http.py` that triggers the flow once per server.
5. UI: an "Authorize" button and a "needs authentication" state on the card.
6. A market blocker kind `oauth`, so such servers are labelled honestly rather
   than offered as runnable.

**Suggested split:** 5.1a discovery + DCR + PKCE + exchange (the happy path),
5.1b refresh and re-auth, 5.1c the UI and the market kind. Ship 5.1a behind a
flag first.

**Note:** this also fixes the broken Google Calendar flow (Phase 6).

### 5.2 Secrets at rest

**Decision needed.** Three options:
- **A — OS keychain** (`keyring`, or DPAPI on Windows). Correct, but a new
  dependency and new infrastructure that should cover *all* app secrets, not
  just MCP.
- **B — DPAPI-encrypted file**, no new dependency (`ctypes` → `CryptProtectData`),
  Windows-only. Fits the existing `audio.py` precedent of pure-ctypes Windows
  APIs.
- **C — leave plaintext, document it.** What the field mostly does.

I recommend **B** for MCP now (no new dependency, real protection at rest) and
A as a separate project. Confirm.

### 5.3 Smaller, high-value additions

Cheap and each independently shippable:
- **Per-tool disable** (`disabledTools`), so a tool can be hidden from the model,
  not just auto-approved. VS Code and Windsurf have this; Addled does not.
- **Redact `last_error`** before it reaches the UI — at minimum strip anything
  matching a declared credential name or a bearer token.
- **Config import**: read `.cursor/mcp.json`, `.vscode/mcp.json`, and Claude
  Code's `~/.claude.json` scopes. VS Code does this and it is a genuine
  switching cost remover. Low effort, no server changes.
- **Permission wildcards** (`mcp__github__get_*`) once there are enough tools to
  need them.

---

## Phase 6 — pre-existing bugs found while planning

These are **not MCP bugs**, but they are in the same feature area and were found
by the same investigation. Reporting rather than assuming scope.

**The Google Calendar OAuth flow is broken in two independent ways**
(`calendar_integration.py`):
1. The redirect is `http://localhost:9876/oauth/callback` — **9876 is the
   WebSocket port**; the HTTP API is 9877. The callback cannot be served there.
2. Even at the right port, **`/oauth/callback` is not routed** — `http_api.py`
   serves only `/api/nav` and `/api/ping`, so the route 404s.
3. It also has **no PKCE and no `state`**, so the flow is both unsafe (CSRF) and
   rejected by providers that require PKCE.

Both defects verified against the **running app**, not just read:

```
GET http://127.0.0.1:9877/oauth/callback?code=test  -> HTTP 404
GET http://127.0.0.1:9877/api/ping                  -> HTTP 200   (control)
GET http://127.0.0.1:9876/oauth/callback?code=x     -> HTTP 426
       "Failed to open a WebSocket connection: missing Connection header."
```

426 Upgrade Required confirms 9876 is the WebSocket listener — a browser
redirect there cannot deliver a code even if the route existed.

So that integration cannot currently complete an authorization. Worth fixing as
part of 5.1a, since a working callback + PKCE is shared work.

---

## Suggested order and sizing

| Phase | Work | Risk | Effort |
|---|---|---|---|
| 1.1 | SSE line-ending normalisation | very low | ~1h incl. test |
| 1.2 | stdio env allowlist + opt-out | **medium** | ~3h incl. tests |
| 2 | Tool-name collision handling | low | ~3h |
| 3.1 | Approval key includes endpoint | low | ~2h |
| 3.2 | Model cannot self-approve | **medium** | ~4h + decision |
| 4 | Transport conformance (F6/F8/F9/F10) | low | ~6h |
| 6 | Fix the calendar callback (shared with 5.1a) | low | ~2h |
| 5.3a | Per-tool disable | low | ~3h |
| 5.3b | Redact `last_error` | low | ~1h |
| 5.3c | Config import from other agents | low | ~4h |
| 5.2 | DPAPI secret storage | medium | ~6h |
| 5.1 | OAuth (a/b/c) | high | **~2-4 days** |

### Order given the decisions

1. **3.2 first, not last.** Decision 3 changes the tool-call loop, and the
   `check_mcp.py` confirm-retry tests have to be rewritten around it. Doing the
   other work first means rewriting those tests twice.
2. **Phase 6 second.** A working `/oauth/callback` route + PKCE is shared with
   5.1a, so building it here avoids doing it twice.
3. **1.1, 1.2, 2, 3.1, 4** — independent, no ordering constraints.
4. **5.2 (DPAPI) before 5.1a.** OAuth stores refresh tokens; storing them in
   cleartext first and encrypting later means a migration on top of a migration.
5. **5.1a -> 5.1b -> 5.1c**, then 5.3a/b/c.

**Ship 1.1 and 1.2 independently and immediately** — they fix what is broken
today. 1.1 breaks nothing. 1.2 carries the only regression risk in the set and
has an opt-out, so it can go out alone and be reverted alone.

Total: ~2-4 days for all of it, dominated by 5.1.

---

## Decisions — ANSWERED 2026-10-14

All five settled. These are now the plan's constraints, not open questions.

| # | Decision | Chosen | What it means in code |
|---|---|---|---|
| 1 | **Env isolation (F2)** | **Allowlist + opt-out flag** | Pass ~20 vars (`PATH`, `TEMP`, `SystemRoot`, …) to stdio children. Add `mcp.inherit_env` (default `false`); true restores today's behaviour. Ship alone, own commit. |
| 2 | **Name collisions (F3)** | **Suffix and keep all** | Collided names get a short hash suffix (`mcp__srv__my_tool_a1b2`). Nothing is dropped; all 3 of 3 reachable. |
| 3 | **Self-approval (F5)** | **Separate the two jobs** | The refusal no longer tells the model to re-call with `confirm=true`. The turn ends and a real user prompt writes the approval. `confirm=true` from the model is ignored. Touches `tool_loop.py` as well as `manager.py`. |
| 4 | **Secrets at rest (5.2)** | **DPAPI encrypted file** | `ctypes` -> `CryptProtectData`, local-user scope, no new dependency. Follows the `audio.py` precedent. Encrypt MCP credentials; leave broader app secrets for the separate keychain project. |
| 5 | **OAuth scope (5.1)** | **Do it all now, phased** | Full conformance: `WWW-Authenticate`, RFC 9728, RFC 8414, DCR, PKCE S256, `resource`, loopback redirect, refresh. Delivered as 5.1a/b/c so each lands reviewed. Phase 6's callback + PKCE work is shared. |

### Consequences worth stating plainly

- **Decision 3 is the largest behavioural change** and the only one that alters
  the tool-call loop. Every existing `check_mcp.py` assertion about confirm
  retries needs rewriting rather than extending.
- **Decision 4 is a format change.** Existing plaintext credentials in
  `settings.json` must migrate on first read (decrypt-or-plaintext fallback),
  or users silently lose their keys. This needs its own test.
- **Decision 5 reverses the earlier recommendation** ("bugs first"). This work
  is ~2-4 days and must be staged 5.1a -> 5.1b -> 5.1c, each independently
  reviewable. It also raises the value of doing Phase 6 early, since a working
  callback route is shared work.
- **Testing 5.1 needs a real OAuth-protected server and a funded provider.** The
  9router account was credit-starved during the last live session, so the OAuth
  flow cannot be verified end-to-end until that is settled.


**Also relevant:** the repo currently has ~18 modified files across several
sessions and you asked me to keep `ea05fad` as the last commit. Any of this work
should go on a fresh branch from that point, and I still have uncommitted fixes
from the earlier session (the streaming tool-catalogue fix `chat_stream` never
received) sitting in the tree alongside it.
