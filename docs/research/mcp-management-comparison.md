# MCP management: Addled vs other agents

Research note, 2026-10-14. **No code was changed.** Everything below is either
read from the tree on disk, queried from the running app, or cited to a primary
source. Unverified claims are marked.

---

## 1. How Addled does it today

### Shape

Addled is **GUI-first**. There is no CLI and no hand-edited config file as the
primary path — Settings → MCP is the whole surface. That is the opposite of
Claude Code (config-first) and closer to Cursor/VS Code (both, GUI leading).

| Piece | Where |
|---|---|
| UI | `dashboard/src/app/settings/page.tsx:271-660` (`McpSection`) |
| Manager | `backend/mcp_client/manager.py` (25.7 KB, 594 lines) |
| Transports | `stdio.py`, `http.py` |
| Protocol | `protocol.py` (JSON-RPC + SSE *response* parsing) |
| Approval | `approval.py` |
| Credentials | `credentials.py` |
| Market | `market.py` (32.5 KB — the largest file in the module) |
| Tests | `scripts/check_mcp.py`, `scripts/check_mcp_market.py` |

### What the page offers

Seven global settings (enable, connect-at-startup, browse-market,
auto-acquire, idle-timeout, plus a reconnect-all button and a
`N/M connected · K tools` counter), a warning banner about third-party
programs, an add-server form (stdio or HTTP, with a `KEY=VALUE` env textarea
that parses the way a README block is written), a per-server card, and a market
search.

Per-server card: state badge (`ready` / `connecting` / `error` /
`disconnected`), transport, resolved command line or URL, tool count,
`last_error`, and five controls — Enabled, Trusted, Connect, Disconnect,
Remove. Below that, **one row per tool** with an "Always allow" toggle.

### Design choices worth naming

**Tool naming — `mcp__{server}__{tool}`.** `manager.py:37`. Sanitised to
`[A-Za-z0-9_]`, 48 chars per segment. This is *the same convention* Claude Code
and VS Code use — so an MCP tool reads identically in Addled's prompt as in
those products. Good convergence, whether by design or by following the spec.

**Approval via a `confirm` argument on the tool itself.** `approval.py:143`.
Rather than a separate permission layer, Addled *injects* a `confirm: boolean`
property into every MCP tool's JSON Schema, strips it before forwarding, and
returns a `requires_approval` result whose `message` tells the model in plain
language: *"call this exact tool again with the same arguments plus
confirm=true."* Two tiers: session approval (in-memory, dies on restart) and
standing approval (persisted, per-tool). A tool can be marked `grantable:
false`, in which case it always asks.

This is unusual and, I think, the most interesting thing in the module. It puts
the approval conversation *inside* the tool-call loop, where the model can
retry with the flag, instead of in a side-channel the model has to be taught
about.

**Credentials are name-keyed and reused.** `credentials.py`. Values are keyed
by variable name (`HASDATA_API_KEY`), not by server — so a key entered once is
reused when another listing asks for the same name, *including* during an
automatic install. The UI can never read a value back; it is told which names
are known. Removing one means writing an empty string.

**Two registries.** The official MCP registry
(`registry.modelcontextprotocol.io`) and Smithery
(`registry.smithery.ai`). Listings are pre-flighted: a card knows whether the
entry is `runnable`, and if not, *which variable* is missing versus *which
runtime* is missing versus *hosted-only* — and the button stays disabled rather
than promising something it cannot do.

**Auto-acquire + idle sweep.** `manager.py:280`. The agent can find and start a
server by itself, but only ones needing no user setup. Those get an `auto` badge
and are disconnected after `auto_deactivate_minutes` (default 30). Servers the
user added by hand are never swept. Configuration survives, so reconnect is
cheap.

**Live state (queried from the running app):** `enabled: true`,
`autoconnect: true`, `servers: []`, `tool_count: 0`. Nothing configured.

---

## 2. Feature comparison

Legend: ✅ shipped · ⚠️ partial/limited · ❌ absent · `?` unverified

| | **Addled** | **Claude Code** | **Cursor** | **VS Code** | **Windsurf** |
|---|---|---|---|---|---|
| Primary surface | Settings GUI | CLI + JSON + `/mcp` panel | GUI + JSON | GUI + JSON + CLI | JSON-first (current) |
| Config file | `settings.json` (internal) | `.mcp.json`, `~/.claude.json` | `.cursor/mcp.json`, `~/.cursor/mcp.json` | `.vscode/mcp.json`, `.mcp.json` | `mcp_config.json` |
| Scopes | ❌ single global | ✅ local/project/user/managed | ✅ project/global | ✅ workspace/user | ❌ |
| Interops with other agents' configs | ❌ | ✅ `add-from-claude-desktop` | ❌ | ✅ **reads Cursor, Windsurf, Claude configs** | ❌ |
| Transport: stdio | ✅ | ✅ | ✅ | ✅ | ✅ |
| Transport: streamable HTTP | ✅ | ✅ | ✅ | ✅ | ✅ |
| Transport: SSE (standalone) | ❌ **coerced to http** | ✅ (deprecated) | ✅ | ✅ | ✅ |
| Transport: WebSocket | ❌ | ✅ (config only) | ❌ | ❌ | ❌ |
| OAuth for remote servers | ❌ **none** | ✅ DCR + metadata discovery | ✅ DCR + static | ✅ | ✅ |
| Dynamic headers / SSO | ❌ | ✅ `headersHelper` | ❌ | ❌ | ❌ |
| Env-var interpolation `${VAR}` | ❌ | ✅ incl. `${VAR:-default}` | ⚠️ | ✅ `inputs` system | ✅ `${file:...}` |
| Secret storage | ⚠️ **plaintext in settings.json** | ✅ keychain + separate creds file | ⚠️ plaintext | ✅ "securely stored" on prompt | ⚠️ plaintext |
| Browsable marketplace | ✅ **2 registries, in-app** | ❌ none (plugins fill this) | ✅ marketplace | ✅ gallery | ⚠️ vintage-dependent |
| Market pre-flight (why it can't run) | ✅ **explicit blocker reasons** | n/a | ? | ? | ? |
| Agent auto-installs a server | ✅ `auto_acquire` | ❌ | ❌ | ❌ | ❌ |
| Idle auto-shutdown | ✅ `auto_deactivate_minutes` | ⚠️ cached-connect | ❌ | ❌ | ❌ |
| Per-server enable/disable | ✅ | ✅ | ✅ | ✅ | ✅ |
| Per-tool approval | ✅ (standing toggle) | ✅ | ✅ | ✅ | ⚠️ `disabledTools` |
| Per-tool *disable* (hide from model) | ❌ | ⚠️ org-only / deny rules | ? | ✅ | ✅ `disabledTools` |
| Glob/wildcard permission rules | ❌ | ✅ `mcp__github__get_*` | ? | ? | ❌ |
| Trust prompt on new server | ✅ `trusted` flag | ✅ project approval | ✅ | ✅ | ✅ |
| Health / status surfacing | ✅ badge + `last_error` | ✅ 8 distinct states | ⚠️ logs | ✅ richest (badges, output channel) | ❌ |
| Auto-reconnect | ✅ "Reconnect all" | ✅ 5× exponential backoff | ⚠️ | ✅ | ⚠️ |
| Admin/enterprise policy | ❌ | ✅ `managed-mcp.json` | ✅ team allowlist | ✅ enterprise allowlist | ❌ |
| Tool-name namespacing | ✅ `mcp__s__t` | ✅ `mcp__s__t` | ? | ? | ? |
| Tool-count guard (>128 chars) | ⚠️ 48-char sanitise | ✅ omits >128 | ? | ? | ? |

---

## 3. Where Addled is ahead

**1. It is the only one that installs servers for you.** `auto_acquire` is
genuinely novel. Claude Code explicitly declined this ("no built-in browsable
registry"); Cursor and VS Code require a click. Addled will notice a task needs
a capability, find a server needing no setup, start it, and switch it off when
idle. Nothing else in the comparison does this. *(Risk noted below.)*

**2. Two registries, and it pre-flights them.** Claude Code has no browser at
all. Addled queries both the official registry and Smithery, and — more
usefully — resolves *why* a listing cannot run: missing variable, missing
runtime (`uvx`/`npx`), or hosted-only. Most marketplaces list an entry and let
it fail on install.

**3. Blocker-aware credential prompts.** The market says "needs
`HASDATA_API_KEY`" and puts a field right there. Keys already held are
subtracted from the requirement, so a card shows what is *still* missing rather
than what is merely declared. And the header-scheme inference (wrapping a bare
token in the declared `Bearer ` prefix) is a small, real kindness.

**4. Idle sweep with a safety boundary.** Only `auto` servers get swept. That
distinction — the agent may clean up after itself but never touch what you
configured by hand — is exactly right, and I did not find it elsewhere.

**5. The `confirm`-parameter approval flow** (discussed above). Different,
defensible, and it keeps the model in the loop rather than beside it.

---

## 4. Where Addled is behind

**1. No OAuth. At all.** This is the biggest gap and it is structural. Grepping
`backend/mcp_client/` for `oauth` returns **zero hits**. Claude Code does
Dynamic Client Registration with RFC 9728/8414 metadata discovery, static
client secrets, and scope pinning; Cursor and VS Code both do OAuth. Addled
supports static headers and env vars only — so **every OAuth-protected remote
MCP server is unusable in Addled today**, and the market will offer them.

**2. No standalone SSE transport.** `manager.py:502` accepts only `stdio` and
`http`; a spec declaring `transport: "sse"` is **silently coerced to `http`**.
Addled parses SSE *responses* (`protocol.py:142`) but cannot *connect* over the
older HTTP+SSE transport. Claude Code, Cursor, VS Code and Windsurf all can.
Servers that never migrated to streamable HTTP are unreachable.

**3. Secrets are plaintext.** `credentials.py:141` writes values via
`config.set("mcp", "credentials", ...)` — into `settings.json`. Verified: the
file has an `mcp.credentials` key. Claude Code puts OAuth tokens in the OS
keychain and a separate `~/.claude/.credentials.json` (verified locally: an
`mcpOAuth` map, 11 entries, with `accessToken` fields). VS Code prompts and
stores "securely". To be fair, **Cursor and Windsurf are also plaintext** — this
is the weakest area across the whole field, not just Addled. But Addled is at
the bottom of it, since it also has no OAuth to keep out of the file.

**4. No scopes and no config interop.** One global list. Claude Code has
local/project/user/managed with a documented precedence order; Cursor has
project-vs-global with project winning on name collision. More pointedly, **VS
Code reads other agents' config files** — Cursor's, Windsurf's, Claude
Desktop's. Addled reads none, and nothing reads Addled's. This repo contains
`.cursor/mcp.json` and `.vscode/mcp.json` (both pointing at
`agent-reasoning-mcp`); Addled's Settings shows an empty list. It cannot see
either.

**5. No permission wildcards.** Claude Code allows `mcp__github__get_*` in an
allow rule. Addled's granularity is one boolean per tool, toggled by hand. Fine
for three tools; tedious for thirty.

**6. No per-tool disable.** Addled's per-tool control is *"Always allow"* —
i.e. approval only. There is no way to say "never let the model see this tool".
VS Code and Windsurf both offer it; Claude Code offers it as org policy and via
deny rules.

**7. No health detail beyond one line.** `last_error` is a single string. Claude
Code surfaces eight distinct states (connected / needs-auth / failed /
pending-approval / rejected / disabled / not-configured / cached) with an
`Issue:` row, and redacts credential-like text from failure output. Addled's
`MCP_STATES` has four, and no redaction. **This one I traced to a concrete
leak path rather than leaving as a hunch:** `manager.py:229-235` appends the
last 400 chars of a failed stdio server's **stderr** to `last_error`
unredacted; `server_status()` (line 460) emits it into the `mcp.list` payload;
the dashboard renders it at `settings/page.tsx:501`. A server that prints its
own environment or a startup banner containing a key puts that key on screen.
Claude Code explicitly redacts credential-like text for exactly this reason.

**8. No reconnect strategy.** "Reconnect all" is a manual button. Claude Code
retries remote servers 5× with exponential backoff. Addled's `connect` is
one-shot.

---

## 5. Judgement

Addled's MCP page is **not a thin one**. It is roughly the most
feature-complete *in-app* MCP management surface of the five — a real
marketplace where Claude Code has none, auto-install where nobody else has it,
and blocker pre-flight that is better than any of them.

Its weaknesses are concentrated in one place: **remote servers**. No OAuth, no
SSE, plaintext secrets. Every competitor handles remote MCP servers with OAuth;
Addled cannot. That is not a polish gap — it is the difference between "works
with the servers I can run locally" and "works with the ones I can reach". The
market will happily surface OAuth-protected servers that will then fail on
connect.

The second theme is **isolation**: no scopes, no config import, no interop. Addled
is a silo. VS Code went the other way and now absorbs whatever its rivals have
configured.

One thing I **checked rather than assumed**, because it looked like the biggest
risk in the module: `auto_acquire` does **not** install unattended.
`registry.py:4798-4813` runs a `_ask_to_acquire` confirmation before any
install, with a comment recording that it *used to* happen silently and was
fixed. The install side is gated; the blast radius I was worried about is not
there. The `trusted` flag and per-tool approval cover the *tool* side.

The residual risk is narrower: once a server is installed and trusted, nothing
re-checks it. That is a normal trust model, not a defect.

**Unverified:** Cursor's and VS Code's tool-naming schemes (marked `?` — I did
not confirm they use `mcp__s__t`); whether Windsurf's current Devin-hosted
build still ships a marketplace (its docs contradict their own archive); and
Cursor's exact secret-storage behaviour beyond "plaintext + interpolation".
