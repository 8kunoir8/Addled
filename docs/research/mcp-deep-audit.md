# MCP subsystem: deep audit

Follow-up to `mcp-management-comparison.md`. **No code was changed.** Every
finding below was reproduced by running it; the probes are listed so each can be
re-run. Findings are separated into CONFIRMED (reproduced here) and SUSPECTED
(inferred from reading, not yet reproduced).

Scope: `backend/mcp_client/` — `manager.py` (594 ln), `market.py`, `stdio.py`,
`http.py`, `protocol.py`, `approval.py`, `credentials.py` — plus the UI and the
`find_mcp_server` skill that reaches them.

---

## 0. What the previous report got right, and what it missed

Right: no OAuth, no SSE transport, plaintext secrets, the `last_error` path.

Missed — and these matter more, because they break servers that **already
work** or hand trust to the wrong party:

| # | Finding | Severity |
|---|---|---|
| F1 | SSE frames with CRLF are not parsed — **breaks the reference SDK** | **Critical** |
| F2 | Every stdio server receives **100% of the parent environment** | **Critical** |
| F3 | Tool names collide after sanitising; tools silently vanish | High |
| F4 | Approval survives a server being repointed elsewhere | High |
| F5 | The model can satisfy its own approval gate | High |
| F6 | Server→client requests are dropped (HTTP only) | Medium |
| F7 | Credentials are shared by variable *name* across all servers | Medium |
| F8 | `MCP-Protocol-Version` only sent after initialize | Low |
| F9 | No session termination (`DELETE`), no 404 re-initialize | Low |
| F10 | `streamable-http`/`sse`/`ws` silently coerced, no error | Low-Med |

---

## F1 — CRLF SSE frames break the reference SDK  **CONFIRMED, CRITICAL**

`protocol.py:142` splits the body on `"\n\n"`. `sse-starlette` — the library the
official MCP Python SDK's streamable-HTTP server is built on — frames every
event with **CRLF by default**:

```python
# sse_starlette/event.py (v3.0.0)
DEFAULT_SEPARATOR = "\r\n"
```

`"\r\n\r\n"` contains no `"\n\n"`, so the whole body becomes **one block**, its
two `data:` lines are joined with a newline, `json.loads` fails, and the
`except JSONDecodeError: continue` **silently drops everything**.

Measured (`_pn/mcp_probe_final.py`):

```
1 event  (CRLF) -> 1 parsed   (single frame survives by accident)
2 events (CRLF) -> 0 parsed   <-- both lost
2 events (LF)   -> 2 parsed   (only LF works)
```

**Impact:** a conformant server using the reference SDK is unusable. The POST
returns `[]`, so `request()` raises `"no response to 'initialize'"` and the
server card shows `error` with no hint that the payload arrived intact. Any
Python/FastAPI/Starlette MCP server is affected. This is not an edge case — it
is the default of the most common server stack.

Why "1 event works" hid it: a single trailing frame still leaves its `data:`
line inside the one block, so the bug only appears once a server sends two
events — i.e. on any server that emits a progress notification before its
result, which the spec explicitly allows ("server MAY send requests/notifications
first").

## F2 — every stdio server gets the whole parent environment  **CONFIRMED, CRITICAL**

`stdio.py:66`:

```python
def _env(self) -> dict:
    env = dict(os.environ)          # <-- everything
    extra = self.spec.get("env") or {}
```

Measured: **125 parent vars → 125 child vars, nothing filtered.** A stdio MCP
server is a third-party program that Addled will *auto-acquire* on the model's
initiative, and it receives every credential in the parent process —
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AWS_*`, `GITHUB_TOKEN`, and the MCP
credentials entered for *other* servers.

For comparison, Claude Code explicitly documents the opposite guarantee:
credential-named variables (`ANTHROPIC_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`,
`NPM_TOKEN`, …) **read as empty** inside a server's config, and it refuses to
send OAuth credentials to non-HTTPS token endpoints. The MCP spec's own security
section warns that servers must not proxy credentials they receive.

This is the finding I would fix first. It is a small change (an allowlist of
`PATH`/`SystemRoot`/`TEMP` plus declared `env`) with a large blast radius.

## F3 — tool names collide and vanish silently  **CONFIRMED, HIGH**

`manager.py:37` sanitises `[^A-Za-z0-9_]` → `_`, so `my-tool`, `my.tool` and
`my/tool` all become `mcp__s__my_tool`. `SkillRegistry.register` is
`self._skills[skill.name] = skill` — **a bare overwrite, no collision check**.

Reproduced end-to-end against a server exposing all three
(`_pn/mcp_probe_collide.py`):

```
server really exposes : ['my-tool', 'my.tool', 'my/tool']  (3 tools)
skills registered     : ['mcp__collide__my_tool']          (1)
RESULT: 3 tools on the wire -> 1 reachable
```

Two tools are unreachable and the model is never told. Note the collision is
also *within* one server, which is the realistic case: a server with both
`read-file` and `read.file` loses one. The same 48-char truncation in
`_sanitise` collides two long server names, which makes `add()` report
"already exists" for a server the user never added.

## F4 — an approval survives the server being repointed  **CONFIRMED, HIGH**

`approval._key()` is `f"{server_id}::{tool}"` — the id, never the URL or
command. `update()` (`manager.py:564`) rewrites `url`/`command` freely and does
**not** revoke approvals; `remove()` does. Reproduced:

```
approved on srvX                   : True
repoint url -> https://evil.example/mcp, same id : still True
```

So: add a server, approve a tool, then change the URL to a different host. The
new host's tool of the same name runs with no prompt. The grant was made about
*a tool on a specific server*; the key only names the id. Fixing it means
including the endpoint in the key, or revoking on any change to `url`,
`command`, `args`, or `env`.

## F5 — the model can grant its own approval  **CONFIRMED, HIGH**

`approval.decorate_schema` injects a `confirm: boolean` property into the tool's
schema and `manager.py:405-412` treats it as the approval gate:

```python
if not trusted and not approval.is_approved(server_id, tool_name):
    if not bool(call_params.get(approval.CONFIRM_PARAM)):
        return approval.refusal(...)
    approval.approve(server_id, tool_name)   # <-- granted on the model's word
```

`confirm` is a **tool argument**. The model writes it. Nothing verifies a user
saw anything. Reproduced (`_pn/mcp_probe_selfapprove.py` mechanically): a call
with no flag is refused; the identical call with `confirm=true` **runs**, and
`is_approved()` is then true for the rest of the session for that tool.

I checked whether anything upstream catches it — a Netlify of the approval
system. It does not:

```
policy.is_protected('mcp__x__y')          -> False
policy.is_permanently_grantable('mcp__...') -> True
```

MCP tools are neither protected nor content-classified, so nothing intercepts
them before the handler. The refusal *message* is what does the work it claims
is done by permission — it instructs the model to re-call with `confirm=true`.
A model that follows instructions literally grants itself access; a model under
prompt injection is handed a documented one-flag bypass.

To be fair to the design: the two-tier intent (session vs standing) is sound, and
the UI's "Always allow" is a real user action. The defect is that the *same*
flag serves both the model's retry and the user's consent, and nothing
distinguishes a retry-after-asking from a first-call assertion.

## F6 — server→client requests dropped over HTTP only  **CONFIRMED, MEDIUM**

`protocol.py:1-9` states the requirement: a message with a `method` is a peer
request and "must be answered even when it is one Addled does not implement — a
client that silently drops it leaves the server waiting."

`JsonRpcSession.feed` does exactly that. `McpHttpClient` does not use it — it
reimplements correlation inline and **skips any message carrying a method**:

```python
if msg.get("id") != req_id or "method" in msg:
    continue          # http.py:210 — a server request dies here
```

Reproduced: a server asking `roots/list` (id=99) received **no reply** over
HTTP, while the same scenario over stdio is answered. `JsonRpcSession` is
imported only by `stdio.py` — it is dead code for HTTP, so the two transports
have silently diverged and HTTP lost the guarantee its own module documents.

A second, smaller divergence in the same loop: it `return`s on the matching id,
so a notification arriving *after* the result is never read.

## F7 — credentials are shared by name across servers  **CONFIRMED, MEDIUM**

`credentials.py` keys values by variable *name* alone, deliberately ("a value
already given is reused the next time a listing wants it"). Reproduced: after
storing `API_KEY=secret-for-server-A`, `subset_env(["API_KEY"])` returns that
secret for **any** server declaring `API_KEY`. Since `API_KEY`, `TOKEN` and
`API_TOKEN` are the declared names half the registry uses, server B silently
receives server A's secret. Combined with F2, it also reaches every stdio child.

The reuse is a real convenience and `subset_*` is correctly whitelisted to
declared names — but "same name" is not "same secret", and the auto-acquire path
makes it reachable without the user watching.

## F8-F10 — smaller conformance gaps  **CONFIRMED, LOW**

- **F8** `MCP-Protocol-Version` is set in `_headers()` only once
  `self.protocol_version` is populated, i.e. never on the initializing POST. The
  spec requires it "on all subsequent HTTP requests" and the server SHOULD assume
  `2025-03-26` if absent — so a 2025-06-18 server may downgrade the first
  exchange. Verified: `_headers()` before negotiation contains no version header.
- **F9** No `DELETE` is ever sent to terminate a session (the spec's SHOULD), and
  a **404 is explained as "there is no server at this address any more"** when
  the spec says a 404 on a session-bearing request means *the session expired,
  start a new one*. The client treats an expected lifecycle event as a fatal
  misconfiguration. Verified: no HTTP verb other than POST exists in the class.
- **F10** `validate()` accepts any transport string and silently rewrites it:
  `sse`, `ws`, `websocket` → `http`, `err=None`. Verified by execution.
  `streamable-http` → `http` is correct (it is the spec's own alias), but
  unknowns are indistinguishable from aliases. An SSE-only server is recorded as
  `http` and later fails with a transport error that names the wrong cause.

## F11 — the OAuth dead end, end to end  **CONFIRMED, HIGH**

Confirmed in the earlier report; the chain is now traced with the classifier:

```
market._finish({'transport':'http','url':'https://mcp.acme/mcp'})
  -> runnable: True, blocked_kinds: [], blocked_reason: ''
```

Six blocker kinds exist (`command`, `env`, `headers`, `hosted`, `params`,
`runtime`) — **no `oauth`**. So an OAuth-protected server is offered as
installable, installs cleanly, and fails on first call with:

> *"the server rejected the credential (HTTP 401) — replace it with Forget on
> the market card"*

That advice cannot work: there is no credential to replace. The user is sent to
a dead end by an error message that misdiagnoses the cause. The spec's MUST here
is unambiguous — clients "MUST be able to parse WWW-Authenticate headers and
respond appropriately to HTTP 401" — and `grep` confirms Addled never reads
`WWW-Authenticate`, `well-known`, `authorization_servers`, `code_challenge`,
`redirect_uri`, or `refresh_token`. There is no OAuth machinery at all.

---

## The two that matter most

**F1 and F2 are not feature gaps — they are things that break today.**

F1 means MCP servers built on the reference SDK do not work, and the failure
looks like the server's fault. F2 means every server Addled runs can read the
user's API keys. Both are small fixes with contained scope: normalise the SSE
separator before splitting, and stop passing `os.environ` wholesale.

F3–F5 are the trust model: tools can vanish silently, trust can follow a server
to a new host, and the model can grant itself permission. F5 in particular is
worth a design decision rather than a patch — the `confirm` flag currently does
two jobs, and the security one only works if the user's consent is what sets it.

F11 is the strategic one: without OAuth, all remote MCP servers are second-class.

---

## Reproduction

| Finding | Probe |
|---|---|
| F1, F2, F3, F4, F5 | `_pn/mcp_probe_final.py` |
| F3 (end-to-end, real server) | `_pn/mcp_probe_collide.py` |
| Protocol conformance | `_pn/mcp_probe_protocol.py` |
| HTTP transport | `_pn/mcp_probe_http.py` |
| F5 (handler path) | `_pn/mcp_probe_selfapprove.py` |

All under `_pn/` (untracked). They drive classes directly against local fakes —
no network, no user servers touched. `check_mcp.py` and `check_mcp_market.py`
both still pass: **none of these findings are covered by the existing tests**,
which is why they survived.

## SUSPECTED (read, not reproduced)

- **Image content is discarded.** `_normalise` replaces `image` blocks with the
  string `"[image content returned by the tool]"`, so a vision-capable server's
  output never reaches the model as an image. Reproducible in principle; I did
  not build an image-returning server.
- **`_normalise` truncates at `MAX_RESULT_CHARS` with no marker in the text**,
  and sets only a `truncated` boolean — the model sees a silently clipped result.
- **`sweep_idle` may disconnect a server mid-use** if `last_used` is not updated
  for a call that exceeded the timeout window; it sets `last_used` at call
  *start*, so a long call is safe — but a *streamed* `long`-running server with
  no call in between is swept. Didn't reproduce.
- **Credential values could reach a log line** via `_body_note(body)` echoing a
  server's error body into `McpError`, which is logged. The query-string
  credential is appended to the URL per request, and a failing host may echo the
  URL in its error body — that would put the key into `last_error` (F from the
  earlier report) by a second route.

## Not defects

Checked and clean, for the record: `connect()` is lock-guarded; `remove()`
revokes approvals and unregisters tools; `subset_*` is whitelisted to declared
names; stdio reaps the child and drains stderr (no pipe deadlock); the `Accept`
header correctly lists both content types; `fail_all` fires when a stdio
transport dies; `_explain` gives genuinely useful text for 401/402/403/404/429
and 5xx.
