# Addled — CLI Tool Builder

**Feature plan.** A Settings page where the user asks Addled to build a dedicated
CLI for a capability it will need later. Every tool built there becomes a real
skill — callable from chat, code, and swarm — and takes **priority over market
search and MCP** when the agent is missing a tool.

Status: **SHIPPED in 1.0.37** · See `PLAN-cli-toolbuilder-phases.md` for what was
actually built, and the notes at the end of that file for where this plan and the
implementation diverged.

---

## 1. Problem

Today, when the model calls a skill that does not exist, `_execute_skill_inner`
(`backend/skills/tool_loop.py:178`) resolves in this order:

```
1. built-in registry   → 2. MCP market search + install → 3. LLM forge
```

Two consequences:

- **MCP is tried first and costs the most.** A market install fetches and runs
  third-party code; it is the heaviest and least-predictable path, yet it is the
  agent's first instinct when anything is missing.
- **There is no place to pre-build a tool deliberately.** The forge is only
  reachable *reactively*, mid-turn, when the model has already failed once. The
  user cannot say "build me a tool for X, I will need it later" and have it sit
  ready, reviewed, and named.

The user wants the inverse: **user-authored CLI tools are first-class, built on
purpose, and preferred over anything downloaded.**

## 2. What "CLI tool" means here

A **CLI tool** is a self-contained command-line program that Addled builds and
owns:

- an executable entry point (`tools/bin/<slug>.py` or `.cmd` shim on Windows),
- a `--help` interface the user can run by hand,
- JSON on stdout when given `--json`, so the skill layer can parse it,
- a manifest (`tools/<slug>/tool.json`) describing name, description, params,
  and the argv template.

It is **not** a chat-turn skill the model calls directly. The skill layer is a
thin adapter: `SkillDefinition.handler` shells out to the CLI
(`subprocess.run(["python", bin_path, ...])`) and returns the parsed JSON.

Why a CLI instead of a raw async function (what the forge produces)?

| | Forge skill | CLI tool |
|---|---|---|
| User can run it | no | yes, from a terminal |
| Debuggable | only through chat | same interface as the agent |
| Composable | no | pipes, scripts, cron |
| Reusable by other agents/MCP | only via MCP wrapper | directly |
| Reviewable before use | file appears mid-turn | built on request, reviewed on the page |

CLI as the execution substrate; skill as the adapter; MCP as an optional export.

## 3. Resolution order change (the priority requireh)

`_execute_skill_inner` becomes:

```
1. built-in registry
2. CLI tool registry        ← NEW, user-authored, preferred
3. MCP tools already connected
4. MCP market search + install
5. LLM forge
6. ask the user to build one from Settings  ← NEW, last resort
```

Rationale: user-built tools are reviewed, local, and known-good. A downloaded MCP
server is none of those. Preferring the user's own is both safer and faster.

### 3.1 Matching

`backend/cli_tools/registry.py` exposes `find_match(name, params)`:

1. exact slug match,
2. alias match (manifest `aliases`),
3. keyword overlap between the requested skill name + the turn's request text
   (`config.get("_forge", "request")`) and each manifest's `keywords`,
   scored; return the best if above `cli_tools.match_threshold` (default 0.5).

Reuse the pattern from `backend/skills/market_search.py:find_match` — same shape,
same threshold semantics, so the two are interchangeable in the chain.

### 3.2 The tool-call rewrite

If the model called `scrape_prices` and the CLI registry matches slug
`price-scraper`, the adapter does **not** rewrite the name. It executes the CLI
with the params the model sent, mapped through the manifest's `argv_template`.
Unknown params are passed as `--key value`; `argv_template` pins the rest.

### 3.3 Telling the agent

When the chain reaches step 6 — nothing built-in, no CLI tool, no MCP match,
forge failed or was declined — the return is not a dead end. It is an
`ask_user`-shaped result:

```
{
  "success": False,
  "error": "No tool for this. I can build one — open Settings → CLI Tools
            and ask for '<capability>'.",
  "needs_tool": {"capability": "...", "suggested_slug": "..."},
  "requires_answer": True,
}
```

And when nothing is missing but the user asks "why can't you do X", the system
prompt fact block (`backend/tool_brief.py`) gains a line naming the CLI Tools
page as the place to add capability. See §6.

## 4. Backend design

### 4.1 New package: `backend/cli_tools/`

```
backend/cli_tools/
  __init__.py        facade: cli_tools, CliToolRegistry, CliTool
  registry.py        CliTool dataclass, load/save, find_match, list_all
  builder.py         build(spec) → generate CLI source, write, validate, register
  runner.py          subprocess execution, timeout, JSON parse, output cap
  adapter.py         CliTool → SkillDefinition (the bridge into tool_loop)
  spec.py            CliToolSpec schema + argparse→JSON-Schema inference
```

Data model (`CliTool`):

```python
@dataclass
class CliTool:
    slug: str                      # "price-scraper"
    name: str                      # skill name exposed to the model
    description: str
    keywords: list[str]            # for find_match
    aliases: dict[str, str]        # model param name → CLI flag
    params: dict                   # JSON Schema (same shape as SkillDefinition)
    argv_template: list[str]       # ["--url", "{url}", "--json"]
    entry: str                     # "python" | "node" | path to exe
    bin_path: str                  # tools/bin/<slug>.py
    source_dir: str                # tools/<slug>/
    requires_approval: bool = False
    category: str = "cli"
    created_at: float
    built_by: str                  # "user" | "agent"
```

Storage: `ADDLED_DATA_DIR/tools/<slug>/` — **user data, beside the install**,
per the README's data-ownership rule and `backend/app_paths.py`. Not in the repo
tree, so an upgrade cannot clobber it. The packaging check in
`scripts/check_packaging.py` must be taught to exclude it.

### 4.2 Builder (`builder.py`)

The forge already knows how to do this — reuse it rather than reimplement.

```
build(spec, provider) →
  1. validate slug (unique, filesystem-safe, not a reserved stdlib module name
     — see the shadow-guard comment in backend/main.py)
  2. discover(): reuse SkillForge.discover() for library/approach
  3. generate(): LLM writes a standalone CLI program that
       - defines main(argv) and argparse with --json
       - prints {"success": bool, ...} JSON to stdout
       - has a docstring that doubles as --help
  4. AST-validate, infer params → JSON Schema (reuse SkillForge._infer_params)
  5. write tools/<slug>/tool.py + tool.json manifest + shim
  6. smoke-test: run `<entry> tool.py --help` then a dry-run if the manifest
     declares one; failure → do not register, report why
  7. register the adapter skill
```

Key difference from the forge: **the user sees the source before it is
registered.** The builder returns the generated code and the page shows it in a
CodeMirror panel with Apply / Regenerate / Discard. This is the whole point of
doing it on a page rather than mid-turn.

### 4.3 Runner (`runner.py`)

- `subprocess.run(argv, timeout=config.get("cli_tools","timeout_s",default=60),
  capture_output=True, cwd=tool.source_dir, shell=False)`.
- Cap stdout 50 KB / stderr 10 KB (mirror `backend/actions/terminal.py`).
- Prefer `--json`; if absent, wrap the text as `{"success": True, "stdout": ...}`.
- Every run goes through `ActionExecutor`'s gate pipeline for the destructive
  check — a CLI tool is just a command, and `run_command` is already gated.
  Non-destructive reads skip the gate for speed; anything the manifest marks
  `requires_approval` always stops.

### 4.4 Adapter (`adapter.py`)

```python
def as_skill(tool: CliTool) -> SkillDefinition:
    async def handler(params: dict) -> dict:
        argv = render_argv(tool.argv_template, params, tool.aliases)
        return await run_cli(tool, argv)
    return SkillDefinition(
        name=tool.name, description=tool.description, parameters=tool.params,
        handler=handler, category="cli",
        requires_approval=tool.requires_approval,
    )
```

Registration into the same singleton `skill_registry`, so every existing
consumer (chat, code, swarm, bots, voice) picks it up with no change. Same trick
`mcp_client/manager.py:_register_tools` already uses.

### 4.5 RPC surface (in `ws_server.py`)

```
cli_tools.list        → all tools + build state
cli_tools.get         {slug} → manifest + source
cli_tools.build       {spec, provider?} → {source, slug, warnings} (dry, not saved)
cli_tools.apply       {slug, source} → validate + smoke-test + register
cli_tools.test        {slug, params} → run once, return output
cli_tools.update      {slug, manifest}
cli_tools.remove      {slug} → unregister + delete dir
cli_tools.suggest     {capability} → draft a spec from a capability phrase
cli_tools.exportMcp   {slug} → stdio MCP server wrapper (optional, §7)
```

`cli_tools.build` is deliberately two-phase (build → show → apply) so the page
can hold the generated source in a review buffer that never touches disk.

## 5. Frontend design

### 5.1 New Settings section: **CLI Tools**

`dashboard/src/app/settings/cli-tools-section.tsx`, registered in the
`SECTIONS` list of `settings/page.tsx` (currently 19 sections; this is the 20th)
with `SECTION_ICONS["cli-tools"] = "⌨️"`.

Layout:

```
┌─ CLI Tools ──────────────────────────────────────────────┐
│ Build a command-line tool for something Addled will need  │
│ later. It's yours, runs locally, and every page can use   │
│ it — chat, code, and swarm agents alike.                  │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  What should it do?                                 │ │
│  │  [ e.g. fetch a URL and return the main text   ]    │ │
│  │  Name (optional): [ price-scraper              ]    │ │
│  │  Provider: [ Addled Local ▾ ]      [ Build ]        │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                           │
│  ── Review (not saved yet) ────────────────────────────── │
│  ┌ CodeMirror (python) ─────────────────────────────────┐ │
│  │  #!/usr/bin/env python3                              │ │
│  │  """Fetch a URL..."""                                │ │
│  │  ...                                                 │ │
│  └──────────────────────────────────────────────────────┘ │
│  Detected params: url (string, required), json (flag)     │
│  [ Regenerate ]  [ Discard ]              [ Apply ]       │
│                                                           │
│  ── Installed ─────────────────────────────────────────── │
│  ▸ price-scraper    ★ preferred   3 runs   [Test][Edit][×]│
│  ▸ pdf-splitter                 never run  [Test][Edit][×]│
└───────────────────────────────────────────────────────────┘
```

Behavior details:

- **Build** calls `cli_tools.build`. Buttons disabled while streaming; reuse
  the SaveIndicator pattern.
- **Apply** calls `cli_tools.apply`. On success the tool moves to Installed.
- **Test** opens a small param form derived from the JSON Schema and shows
  stdout/stderr side by side — the same output a user would see in a terminal.
- **Preferred** badge marks a tool that outranks MCP in `find_match`.
- **Edit** opens the manifest + source in the existing Code page editor instead
  of duplicating CodeMirror (the dashboard already has one).
- Rows are deletable; deletion unregisters the skill immediately.

### 5.2 Chat, Code, Swarm — no page changes needed

Because registration is into the shared registry, all three already work:

- **Chat** — `chat_send` → `run_chat_pipeline` → `chat_with_tools(tools=None)`
  offers every enabled skill, CLI tools included (`tool_brief.capabilities_block`
  needs a `_FACTS` entry for the `cli` category, see §6).
- **Code** — `code_plan` / `code_edit` pass a restricted tool list; add the CLI
  tool names for the workspace scope.
- **Swarm** — `SwarmAgent.run_task` uses `tools=self.tools`; a roster entry can
  name CLI tools like any skill. The dispatch in `orchestrator.spawn` resolves
  via `roster.skill_names()`, so no change.

The only frontend change outside Settings: an **empty-state hint** in chat/code
when a turn fails with `needs_tool`. Render a card: *"I don't have a tool for
this. Build one?"* with a button that deep-links to
`/settings?section=cli-tools&prefill=<capability>`.

## 6. Prompt integration

`backend/tool_brief.py:_FACTS` gains one entry:

```
"cli": "You have user-built command-line tools (prefixed in the tool list).
        Prefer these over searching for an MCP server — they were built and
        reviewed by the user. If none fits and the task needs a new capability,
        do not silently install anything: say so and point the user at
        Settings → CLI Tools."
```

And the acquisition-consent wording (`_ask_to_acquire`) gains a prior question
variant: when a CLI build is possible but not done, ask "I can build a small
tool for this — shall I?" **before** offering a market install.

Ordered questioning, matching the ordered resolution:

1. built-in → just do it.
2. CLI tool exists → just do it.
3. no CLI tool, capability is buildable → *"I can build a tool for this. Build
   it, or should I look for an MCP server?"* → user chooses.
4. user says search → market search, then existing consent text.
5. nothing anywhere → *"I couldn't find or build this. Want to describe it on
   Settings → CLI Tools?"*

## 7. Optional: MCP export

`cli_tools.exportMcp {slug}` writes a ~60-line stdio MCP server that advertises
the CLI tool's schema and shells out to it. This makes a user-built CLI tool
consumable by *external* agents (Claude Desktop, Copilot) without a second
implementation — the adapter pattern from the original discussion, pointed the
other way.

Deferred to 1.0.38 unless requested; the CLI + skill path is the whole of the
value and this is an ecosystem nicety.

## 8. Safety

- Builds require the same consent gate as forge/market — a generated program is
  generated code. `requires_approval` on the manifest forces a prompt per run.
- The page **shows source before registration**; nothing executes before Apply.
- Slug validation rejects stdlib-shadowing names (see `backend/main.py` guard).
- `tools/` lives in `ADDLED_DATA_DIR`, outside the install, so `check_packaging`
  must exclude it — add to the check and to `.gitignore`.
- Runner uses `shell=False` and an argv array; no string interpolation into a
  shell. `argv_template` placeholders are substituted into list slots, not
  concatenated.
- Delete unregisters the skill *before* unlinking files, so a half-deleted tool
  cannot be called.

## 9. Testing

Not pytest — the project uses `scripts/check_*.py`. New suites:

| Script | Covers |
|---|---|
| `check_cli_tools.py` | registry CRUD, manifest round-trip, storage in data dir |
| `check_cli_build.py` | build → source generated → AST valid → apply → registered |
| `check_cli_priority.py` | **resolution order**: CLI tool beats MCP market; MCP unreachable when a CLI match exists |
| `check_cli_consumers.py` | a CLI skill is offered to chat, code, and a swarm agent |
| `check_cli_needs_tool.py` | the dead-end returns `needs_tool` + `requires_answer`, not a bare error |
| `check_cli_safety.py` | slug shadow rejection, `shell=False`, approval gate, packaging exclusion |

Register all six in `scripts/check_all.py`. Update `check_parity.py` (dashboard
and backend method lists must match) and `check_wiring.py`.

## 10. Rollout

| Phase | Deliverable | Gate |
|---|---|---|
| 1 | `cli_tools/` registry + runner + adapter, no builder | a hand-written CLI tool is callable from chat |
| 2 | resolution order change + `needs_tool` return | `check_cli_priority`, `check_cli_needs_tool` green |
| 3 | builder (generate → review → apply) | `check_cli_build` green |
| 4 | Settings page + empty-state deep link | build + test a tool end-to-end in the UI |
| 5 | prompt integration (`_FACTS`, ordered questions) | agent prefers CLI, asks before MCP |
| 6 | docs (`README`, `release-notes.md`, `guide-content.ts`) | — |
| 7 (optional) | MCP export | — |

## 11. Open questions

1. **Language.** Python only for v1 (bundled interpreter is guaranteed), or allow
   Node/batch when detected? *Recommend Python only, with `entry` in the manifest
   so other runtimes can be added without a schema change.*
2. **Naming collision.** If a CLI tool and an MCP tool both match, CLI wins
   silently or is announced? *Recommend silent win + a log line; the priority is
   the point.*
3. **Sharing.** Should built tools be exportable as a bundle (like a skin or a
   forged skill) so users can trade them? *Out of scope for v1.*
4. **Scope of `find_match`.** Match on tool-call name only, or also the turn's
   request text? *Recommend both — the name is often generic (`fetch_page`) while
   the request carries the intent.*
