# CLI Tool Builder — Detailed Implementation Plan (All Phases)

Companion to `PLAN-cli-toolbuilder.md`. That file is the *why*; this is the *how*,
phase by phase, with exact files, signatures, and acceptance criteria.

Status: **SHIPPED in 1.0.37.** Phases 0–6 are implemented and green;
Phase 7 (MCP export) was deliberately not built — see "Where the
implementation diverged" at the end of this file.

Conventions taken from the codebase (verified, not assumed):

- **Skill model** — `SkillDefinition(name, description, parameters: dict,
  handler, category, requires_approval, aliases)`. Handler is
  `async (params: dict) -> dict` returning at least `{"success": bool}`.
  (`backend/skills/registry.py:57`)
- **Single execution seam** — `skill_registry.execute(name, params)` enforces
  alias normalisation, enable/disable, and the approval gate. Never bypass it.
  (`backend/skills/registry.py:924`)
- **Registration is global** — one `skill_registry` singleton; MCP tools already
  register into it under `mcp__<server>__<tool>` with category `mcp`. A CLI tool
  does the same with category `cli`. (`backend/mcp_client/manager.py:342`)
- **Durable data** lives in `ADDLED_DATA_DIR` (`backend/app_paths.py:120`), never
  in the install tree; `check_packaging.py` fails if a writable path could be
  packaged.
- **Tests** are `scripts/check_*.py`, listed in `scripts/check_all.py:18`, using
  a local `check(label, cond, detail)` helper and a `fails` list.
- **RPC** methods are registered with `_server.register("name", handler)`;
  `check_ws_methods.py` cross-references dashboard calls against them.

---

## Phase 0 — Groundwork (prerequisite, no user-visible change)

Establishes the package, the storage root, and the data-dir contract before any
behaviour changes.

### Files
- **new** `backend/cli_tools/__init__.py`
- **edit** `backend/app_paths.py`
- **edit** `scripts/check_packaging.py`
- **edit** `.gitignore`

### Work

1. `app_paths.py` — add, beside `PYLIBS_DIR = DATA_DIR / "pylibs"` (line 208):

   ```python
   # User-built CLI tools. Deliberately under DATA_DIR and not the install
   # tree: a tool is the user's work, and an upgrade replaces program files.
   CLI_TOOLS_DIR = DATA_DIR / "cli_tools"
   ```

   Create it lazily on first write, not at import (matches how `MEMORY_DIR`
   subdirs are handled).

2. `__init__.py` — facade only, mirroring `backend/skills/__init__.py`:

   ```python
   from backend.cli_tools.registry import (CliTool, CliToolRegistry, cli_tools)
   from backend.cli_tools.runner import run_cli
   from backend.cli_tools.adapter import as_skill
   __all__ = ["CliTool", "CliToolRegistry", "cli_tools", "run_cli", "as_skill"]
   ```

3. `check_packaging.py` — extend the writable-path detection so
   `CLI_TOOLS_DIR` is recognised as data (the file already special-cases
   `app_paths.MEMORY_DIR` at lines 152/198/570; add the parallel case).

4. `.gitignore` — add `cli_tools/` and `**/cli_tools/` so a dev run never
   commits a built tool.

### Acceptance
- `python -m scripts.check_packaging` green.
- `from backend.cli_tools import cli_tools` imports without error.

---

## Phase 1 — Registry + Runner + Adapter (backend-only)

Outcome: a **hand-written** CLI tool, dropped into `cli_tools/<slug>/`, is
callable from chat. No builder, no page yet.

### Files
- **new** `backend/cli_tools/registry.py`
- **new** `backend/cli_tools/runner.py`
- **new** `backend/cli_tools/adapter.py`
- **new** `backend/cli_tools/spec.py`
- **edit** `backend/main.py` (boot-time load)
- **new** `scripts/check_cli_tools.py`

### 1.1 `spec.py` — the manifest

```python
@dataclass
class CliToolSpec:
    slug: str
    name: str                 # skill name the model sees
    description: str
    keywords: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)   # param -> flag
    params: dict = field(default_factory=dict)              # JSON Schema
    argv_template: list[str] = field(default_factory=list)  # ["--url","{url}"]
    entry: list[str] = field(default_factory=lambda: ["python"])
    requires_approval: bool = False
    dry_run_args: list[str] | None = None                   # for the smoke test
    timeout_s: int = 60

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "CliToolSpec": ...
    def validate(self) -> str:   # "" when fine, else why not
        ...
```

`validate()` rules (each is a real failure mode, not ceremony):

- `slug` matches `^[a-z0-9][a-z0-9_-]{0,39}$`.
- `slug` is not a stdlib module name — reuse the shadow-guard concern in
  `backend/main.py:12`. Reject against `sys.stdlib_module_names`.
- `name` matches `^[a-z][a-z0-9_]{0,39}$` (skill names never contain
  whitespace — `tool_loop.py:910`).
- `name` does not collide with an existing skill (`skill_registry.get(name)`).
- Every `{placeholder}` in `argv_template` is a declared param, and every
  required param appears in the template.
- `params` is a valid JSON Schema object with `properties`.

### 1.2 `registry.py` — discovery, matching, persistence

```python
@dataclass
class CliTool:
    spec: CliToolSpec
    source_dir: Path
    bin_path: Path
    created_at: float
    built_by: str = "user"          # "user" | "agent"
    run_count: int = 0
    last_run: float = 0.0
    last_error: str | None = None
    preferred: bool = True          # outranks MCP when matched

class CliToolRegistry:
    def __init__(self): self._tools: dict[str, CliTool] = {}
    def load(self) -> int: ...          # scan CLI_TOOLS_DIR/*/tool.json
    def add(self, tool, *, register_skill=True) -> dict
    def remove(self, slug) -> dict      # unregister skill FIRST, then unlink
    def get(self, slug) -> CliTool | None
    def list_all(self) -> list[CliTool]
    def find_match(self, name: str, request_text: str = "",
                   threshold: float | None = None) -> CliTool | None
    def bump_run(self, slug, ok: bool, error: str | None = None) -> None

cli_tools = CliToolRegistry()
```

`find_match` scoring (mirrors `market_search.find_match` so the two are
interchangeable in the chain):

| Signal | Score |
|---|---|
| exact slug == name | 1.00 (short-circuit) |
| exact `name` == name | 0.95 |
| manifest alias contains name | 0.85 |
| keyword ∩ (name + request_text tokens) | `0.15 × overlap`, capped 0.75 |
| description token overlap | `0.05 × overlap`, capped 0.30 |

Return the best above `config.get("cli_tools", "match_threshold", default=0.5)`.
Scoring is pure and deterministic so `check_cli_priority.py` can assert it
without a model.

`bump_run` persists `run_count` / `last_run` back into `tool.json` — but only
the *stats* block, written with the same atomic tmp+replace pattern as
`config.save()`, so a crashed run cannot corrupt a manifest.

### 1.3 `runner.py` — execution

```python
async def run_cli(tool: CliTool, params: dict) -> dict
def render_argv(template: list[str], params: dict,
                aliases: dict[str, str]) -> list[str]
```

- `render_argv` maps model params to flags via the manifest, appending any
  parameter the template did not pin:
  - bool `True` → `--flag`; `False` → omitted.
  - str/int/float → `--key value` as **two list slots** (never concatenated).
  - list → repeat the flag per item.
  - Unknown extra params are dropped and named in the returned warnings.
- Execution via `loop.run_in_executor(None, ...)` +
  `subprocess.run(argv, shell=False, capture_output=True, cwd=tool.source_dir,
  timeout=tool.spec.timeout_s)`.
- Caps: stdout 50 KB, stderr 10 KB — same numbers as
  `backend/actions/terminal.py`, so logs are recognisable.
- Parse: try `json.loads(stdout)`; on failure return
  `{"success": True, "stdout": ..., "parsed": False}`. A CLI that prints JSON
  gets structured results; one that does not is still usable.
- Nonzero exit → `{"success": False, "error": stderr or exit code, "stdout": ...}`.
- Timeout → `{"success": False, "error": "timed out after Ns"}`.
- **Gate**: before running, if `spec.requires_approval`, defer to the standard
  approval path (`executor.request_approval`) exactly as
  `registry._execute_gated` does — return a `requires_approval` result rather
  than executing.

### 1.4 `adapter.py` — the bridge

```python
def as_skill(tool: CliTool) -> SkillDefinition:
    async def handler(params: dict) -> dict:
        return await run_cli(tool, params)
    return SkillDefinition(
        name=tool.spec.name,
        description=tool.spec.description,
        parameters=tool.spec.params,
        handler=handler,
        category="cli",
        requires_approval=tool.spec.requires_approval,
        aliases={k: (v, ) for k, v in ...},   # reverse of spec.aliases, as tuples
    )
```

Registration goes through `skill_registry.register(...)`. Because that is the
same singleton every consumer reads, **chat, code, swarm, bots and voice all get
the tool with zero further changes** — this is the Phase-5 acceptance test.

### 1.5 Boot wiring

`backend/main.py` — after the skill registry is built and before the WS server
starts, add one call:

```python
from backend.cli_tools.registry import cli_tools
n = cli_tools.load()      # logs "loaded N CLI tool(s)"
```

`load()` must be safe when `CLI_TOOLS_DIR` does not exist (return 0) and must
soft-fail per tool: a manifest that fails `validate()` is skipped and logged,
never fatal — a broken user tool must not stop the app booting.

### 1.6 `scripts/check_cli_tools.py`

Suites (local `check()`/`fails` harness, run via `check_all.py`):

1. schema round-trip: `to_dict` → `from_dict` → equal.
2. `validate()` rejects bad slug, stdlib slug (`code`, `json`), colliding skill
   name, undeclared placeholder, missing required param.
3. `find_match`: exact, alias, keyword, below-threshold, no-match.
4. runner: `render_argv` for str/bool/list/unknown; a temp script printing JSON
   parses; nonzero exit is a failure; timeout is a failure and does not hang the
   loop; output truncation.
5. adapter: registers, appears in `enabled_list_all()`, `execute()` routes
   through it, and `unregister` removes it.
6. persistence: write a tool, `load()` re-registers it; `remove()` unregisters
   skill **before** deleting files (assert `skill_registry.get(name) is None`
   after).

### Acceptance
- A tool placed in `ADDLED_DATA_DIR/cli_tools/demo/` is callable via
  `skill_registry.execute("demo", {...})`.
- `check_cli_tools.py` green; registered in `check_all.py`.

---

## Phase 2 — Resolution Order + "build one" Fallback

Outcome: user-authored CLI tools are preferred over MCP; a missing capability
ends by pointing at Settings instead of failing.

### Files
- **edit** `backend/skills/tool_loop.py` (`_execute_skill_inner`, line 178)
- **edit** `backend/config.py` (new defaults)
- **edit** `backend/tool_brief.py` — a new `_cli_tools_note()`, NOT a `_FACTS`
  entry. `_FACTS` is keyed by skill NAME and CLI tool names are chosen by the
  user, so no static row can ever match one. See "Where the implementation
  diverged".
- **new** `scripts/check_cli_priority.py`
- **new** `scripts/check_cli_needs_tool.py`

### 2.1 The chain

Rewrite `_execute_skill_inner` to:

```
1. skill_registry.get(name)              → execute           (built-in)
2. cli_tools.find_match(name, request)   → execute           (NEW)
3. registry now has the skill?           → (MCP already connected registers there,
                                            so it is caught by step 1 — nothing to add)
4. market search + consent + install     → execute           (existing, unchanged)
5. forge + consent                       → execute           (existing, unchanged)
6. nothing → needs_tool result           (NEW)
```

Ordering note that matters: **MCP tools that are already connected are skills in
the registry, so they are reached at step 1 only if the CLI registry did not
claim the name.** That is exactly the intended priority — CLI wins. If they
collide, the CLI tool is used and an INFO log records the shadowed name.

### 2.2 Config

`DEFAULT_SETTINGS` gains:

```python
"cli_tools": {
    "enabled": True,
    "match_threshold": 0.5,
    "prefer_over_mcp": True,     # the priority switch; False restores old order
    "timeout_s": 60,
    "ask_before_build": True,    # the "want me to build one?" question
},
```

`prefer_over_mcp = False` must genuinely restore the old behaviour, so the
change is reversible by a user who disagrees with the default.

### 2.3 Capability inference for the fallback

When step 6 is reached, build the `needs_tool` payload from the turn's own
request text (`config.get("_forge", "request")`, already published every turn —
see `tool_loop.py:142`), not from the function name:

```python
{
  "success": False,
  "error": "No tool for this, and I won't install one without asking. "
           "I can build a small tool for it — Settings → CLI Tools.",
  "needs_tool": {"capability": <request text>, "suggested_slug": <slug>, "called_name": name},
  "requires_answer": True,
}
```

`requires_answer` uses the existing mechanism (`backend/questions/pending`), so
the chat page and the character bubble already know how to surface it.

### 2.4 Ordered questioning

Before the market search runs (step 4), and only when
`cli_tools.ask_before_build` is on **and** the capability looks buildable
(a slug is derivable and the request text is non-empty), ask:

> "I don't have a tool for this. I can build one — shall I build it, or look
> for an MCP server instead?"

Reuse `_ask_to_acquire` (`tool_loop.py:104`, consent text at 1156) with a new
prior variant and extend `_ACQUIRE_QUESTION_RE` (line 175) to recognise the
build phrasing so the answer routes back correctly. Refusal still wins
(`_consented`, line 154).

### 2.5 Prompt facts

`backend/tool_brief.py` gains a `_cli_tools_note(only)` function, called from
`capabilities_block`. It lists the tools the turn actually holds, says to
prefer them over searching for an MCP server, and points at Settings → CLI
Tools when nothing fits.

It is a separate paragraph rather than a `_FACTS` row because that table is
keyed by skill *name* and CLI tool names are whatever the user chose — there is
no static entry that could match `merge_pdfs` or `shout_text`. The guidance is
identical for every built tool, so it is said once. It returns `""` when the
turn holds none, so a turn without them is told nothing about them.

### Acceptance
- `check_cli_priority.py`: with a CLI tool matching a name and the market stubbed
  to return a candidate, the CLI tool runs and the market is never touched; with
  `prefer_over_mcp=False`, the old order returns.
- `check_cli_needs_tool.py`: an unknown capability returns `needs_tool` +
  `requires_answer`, not a bare `"Unknown skill"`, and the ask is emitted only
  when `ask_before_build` is on.
- Existing `check_skill_market.py` and `check_reachability.py` stay green.

---

## Phase 3 — The Builder

Outcome: an LLM generates a CLI program, it is validated and smoke-tested, and
registration happens only after review. Reuses the forge rather than
reimplementing it.

### Files
- **new** `backend/cli_tools/builder.py`
- **edit** `backend/skills/forge.py` (expose two helpers — see below)
- **new** `scripts/check_cli_build.py`

### 3.1 Reuse points in `SkillForge`

Three helpers are already correct and should be shared, not copied:

| Need | Existing | Used as |
|---|---|---|
| find the right library/approach | `SkillForge.discover()` | `skill_forge.discover(...)` — unmodified |
| pip install into `PYLIBS_DIR` + import probe | `SkillForge.install()` | `skill_forge.install(...)` — unmodified |
| AST → JSON Schema | `SkillForge._infer_params()` | **NOT used** — see below |

No refactor was needed. `discover` and `install` were already public methods on
the `skill_forge` singleton that take no skill-specific state, so `builder.py`
calls them directly and the forge's own suites stayed green untouched.

`_infer_params` was the exception, and the plan was wrong to list it.
It reads a generated *Python function's* signature and body to infer types. A
CLI tool has no such function — its interface is an `argparse` block — so there
is nothing for it to read. `builder._params_from_source()` does the equivalent
job for the shape a program actually has: `--flag` names, `type=int`,
`default=`, `action="store_true"`, `nargs`. Reusing `_infer_params` here would
have meant either feeding it code it cannot parse or weakening its contract for
the forge's own callers.

### 3.2 `builder.py`

```python
@dataclass
class BuildResult:
    success: bool
    slug: str
    source: str            # the generated program, for review
    manifest: dict         # proposed tool.json
    detail: str = ""
    warnings: list[str] = field(default_factory=list)
    test_params: dict = field(default_factory=dict)

async def draft(capability: str, *, slug: str | None = None,
                provider=None, name_hint: str | None = None) -> BuildResult
async def apply(build: BuildResult, *, source: str | None = None) -> dict
async def smoke_test(spec: CliToolSpec, bin_path: Path) -> dict
```

**draft** (writes nothing):

1. slug: from `name_hint` or slugified capability; uniquify against the
   registry; run `CliToolSpec.validate()` on the candidate and return the reason
   if it fails.
2. `discover(capability)` → package/approach, for the prompt.
3. `generate` prompt asks for a **standalone CLI program** (not a skill handler):

   ```
   Write a self-contained command-line Python program.
   Requirements:
   - standard library only, unless discovery found a package (name it in a
     `# Package: <name>` header line)
   - argparse with a `--json` flag
   - prints a single JSON object to stdout: {"success": true, ...}
   - errors print {"success": false, "error": "..."} and exit non-zero
   - a module docstring that reads well as `--help`
   - no network calls unless the task needs them
   ```

4. AST-validate; strip code fences (the forge does the same at line 596).
5. Ask the model for an example invocation (`EXAMPLE_ARGV = [...]`, the same
   trick as the forge's `TEST_PARAMS`) and `literal_eval` it out — split off the
   source, because it is the only honest input to smoke-test with.
6. Infer the JSON Schema via `builder._params_from_source()` — read from the
   `argparse` block, not from a function signature.
7. Build the **proposed manifest** (`argv_template` derived from the example
   invocation, `keywords` and `slug` from the capability, `entry: ["python",
   "tool.py"]`). Validate it and carry any complaint as a WARNING rather than
   rejecting, because a draft is shown to a human who can fix it.
8. Return `BuildResult` with source + manifest. **Nothing on disk.**

**apply** (the only writer):

1. Re-validate: slug, collision, schema.
2. `install()` the discovered package if any (verified, as forge does).
3. Write `cli_tools/<slug>/tool.py`, `tool.json`, and a Windows shim
   `tools/bin/<slug>.cmd` containing `@python "%~dp0..\\<slug>\\tool.py" %*`.
4. `smoke_test`: run `--help` first (cheap, proves it parses), then the
   `dry_run_args` if declared, else `TEST_PARAMS`. Failure → **delete the
   directory, register nothing**, return the failure.
5. `cli_tools.add(...)` → registers the adapter skill.
6. Return `{success, slug, registered: True, test: {...}}`.

Keeping `draft` and `apply` separate is what makes the review step possible: the
page holds a `BuildResult` in memory and never touches disk until Apply.

### 3.3 Safety in the builder

- Slug shadow rejection (§1.1) runs in *both* draft and apply.
- Generated code is never `exec`'d in-process — it is only ever run as a
  subprocess. (The forge imports generated modules; the CLI path does not need
  to, and not doing it removes an obvious risk.)
- `apply` refuses to overwrite an existing slug directory; the user must
  `remove` first, so a rebuild cannot silently replace a working tool.
- The `# Package: <name>` install goes through the same verified pip path as the
  forge, and a failed install aborts the build (no half-registered tool).

### Acceptance
- `check_cli_build.py`: with a stubbed provider, `draft` returns source that
  `ast.parse`s and a manifest that passes `validate()`; `apply` on a temp
  `CLI_TOOLS_DIR` writes the files, smoke-tests, and registers; a deliberately
  broken generation leaves `CLI_TOOLS_DIR` empty and the registry unchanged; a
  colliding slug is refused.

---

## Phase 4 — Settings Page

Outcome: the user builds, reviews, tests and deletes tools from the dashboard.

### Files
- **new** `dashboard/src/app/settings/cli-tools-section.tsx`
- **edit** `dashboard/src/app/settings/page.tsx` (20th section)
- **edit** `dashboard/src/lib/ws-types.ts` (payload types)
- **edit** `backend/ws_server.py` (7 RPC handlers)
- **edit** `dashboard/src/app/chat/page.tsx` and `code/page.tsx` (empty-state card)
- **new** `scripts/check_cli_page.py`

### 4.1 RPC handlers (`ws_server.py`)

Registered beside the existing `skills.*` block (lines 5422-5499), so related
methods stay together:

```
cli_tools.list     → {tools: [...], dir: str, enabled: bool}
cli_tools.get      {slug}      → manifest + source (+ stats)
cli_tools.draft    {capability, slug?, provider?} → BuildResult (nothing written)
cli_tools.apply    {slug, source?, manifest?}     → registers
cli_tools.test     {slug, params}                 → run once, {stdout, stderr, parsed}
cli_tools.remove   {slug}                         → unregister + delete
cli_tools.suggest  {capability}                   → draft spec without generating code
```

`cli_tools.draft` and `cli_tools.apply` are split exactly along the
`builder.draft`/`apply` seam, so the "review before save" guarantee is enforced
by the backend, not just by the UI.

`cli_tools.remove` and `cli_tools.apply` are destructive-ish and follow the
existing convention: they are dashboard-initiated (the user clicked), so they
run directly, but `cli_tools.remove` unregisters the skill before unlinking —
the ordering Phase 1 already asserts.

### 4.2 Page structure

Follow the existing settings pattern exactly (`SECTION_ICONS` map,
`SettingRow` primitive, `updateSetting(section, key, value)`, `SaveIndicator`):

```
SECTIONS.push({ id: "cli-tools", label: "CLI Tools", icon: "⌨️" })
```

Component sections:

- **Header** — one-paragraph explainer (copy from the parent plan §5.1).
- **Build card** — capability textarea, optional name, provider dropdown
  (reuse the providers list the Settings page already loads), `Build` button.
- **Review panel** — rendered only when a draft is held:
  - CodeMirror (python) showing `result.source`. Reuse the CodeMirror setup the
    Code page uses rather than adding a second editor configuration.
  - A derived-parameters table from `result.manifest.params`.
  - `Regenerate` / `Discard` / `Apply`.
  - Warnings from `result.warnings` in a muted list.
- **Installed list** — one row per tool: slug, `preferred` badge, run count,
  last error if any, and `Test` / `Edit` / `Delete`.
- **Test dialog** — generates a small form from the JSON Schema (string →
  input, bool → checkbox, array → comma-separated), runs `cli_tools.test`, and
  shows stdout/stderr side by side.

State is local to the section; nothing about a draft is persisted, so closing
the page discards it. That is intentional and matches "must be reviewed".

### 4.3 Empty-state deep link

`chat/page.tsx` and `code/page.tsx`: when a `chat.send` / `code.plan` response
carries `needs_tool`, render a card instead of the plain error:

> **I don't have a tool for this.** *<capability>*
> [ Build one ] ← `/settings?section=cli-tools&prefill=<encoded capability>`

`settings/page.tsx` reads `?section=` and `?prefill=` on mount (the section
list already supports programmatic selection for the guide's deep links) and
focuses the Build textarea with the capability pre-filled.

### 4.4 Types

`ws-types.ts` gains `CliToolInfo`, `CliToolDraft`, `CliToolTestResult`, and a
`needsTool?: {capability: string; suggested_slug: string}` field on the chat and
code response types.

### Acceptance
- `check_cli_page.py`: the seven methods are registered
  (cross-checked by `check_ws_methods.py`, which will fail if the page calls a
  method that does not exist — that is the point of running it here).
- Manual: build → review → apply → test → delete a real tool end-to-end.

---

## Phase 5 — Consumer Parity

Outcome: proof that all four surfaces can use CLI tools, enforced by a check.

### Files
- **new** `scripts/check_cli_consumers.py`
- **edit** `backend/codemode/__init__.py` or the `code_plan`/`code_edit` tool
  lists in `ws_server.py`
- **edit** `scripts/check_parity.py` (extend its assertions)

### Work

1. **Chat** — `chat_send` → `run_chat_pipeline` → `chat_with_tools(tools=None)`
   already offers every enabled skill, so a CLI tool is included. Assert it.
2. **Swarm** — `SwarmAgent.run_task` uses `tools=self.tools`; `orchestrator.spawn`
   resolves names via `roster.skill_names()`. A CLI tool is reachable when named,
   and when `tools=None` it is in the full set. Assert both.
3. **Code** — `code_plan` and `code_edit` pass RESTRICTED lists
   (`_PLAN_TOOLS`, `_CODE_EDIT_TOOLS`), so a tool in the registry is NOT
   automatically callable there. This was the one real code change in the
   phase. A `_code_page_tools(base)` resolver appends the built tools to
   whichever base list the caller passes, and both call sites now use it.

   It resolves at CALL time, not import time. Appending to the constants would
   only ever include tools that existed when the backend started, so a tool
   built during this session would be invisible on the page that just built it.

   Note this deliberately widens what those lists permit: they are read-only by
   design, and a built tool is arbitrary code with no such guarantee. That was a
   decision, taken knowingly — see "Where the implementation diverged".

`check_cli_consumers.py` asserts, with a temp CLI tool registered:

- it is in `enabled_list_all()` → chat offers it;
- `to_openai_tools({tool})` returns exactly it → a narrowed consumer gets it;
- `to_prompt_tools({tool})` names it;
- `filter_for_query("what tools do you have?")` includes it → the `cli`
  category was not dropped by the turn selector's allow-list;
- `_code_page_tools(_PLAN_TOOLS)` and `_code_page_tools(_CODE_EDIT_TOOLS)` both
  contain it, while still keeping `code_read`/`read_file` → the Code page can
  call it without losing what those lists are for;
- executing it through `skill_registry` runs the real program.

Assertions call the resolver and the registry rather than grepping for
identifiers: a name that never reaches the list is the bug, and a grep cannot
tell the difference between "the code exists" and "the code is reached".

### Acceptance
- `check_cli_consumers.py` green; `check_parity.py` extended and green.

---

## Phase 6 — Docs

### Files
- **edit** `README.md` — a "CLI Tools" bullet under Features, and one line in
  the Skills section noting user-built tools outrank MCP.
- **edit** `release-notes.md` — a 1.0.37 entry in the established voice
  (what you can do now / why / verified).
- **edit** `dashboard/src/lib/guide-content.ts` — a Guide entry so the Settings
  guide covers it, with a live-status badge (`cli_tools.list` count).
- **edit** `PLAN-cli-toolbuilder.md` — mark implemented, record deviations.

### Acceptance
- `check_guide.py` green (it validates the guide content shape).

---

## Phase 7 (optional) — MCP Export

Only if requested. `cli_tools.exportMcp {slug}` writes a ~60-line stdio MCP
server that advertises the tool's schema and shells out to it — the adapter
pattern pointed outward, so external agents (Claude Desktop, Copilot) can use a
user-built tool with no second implementation.

Deferring this is safe: the CLI + skill path is the entire user-facing value, and
the manifest already holds everything an export would need, so nothing in
Phases 0-6 forecloses it.

---

## Cross-cutting

### Reversibility
Every behaviour change is behind a config key (`cli_tools.enabled`,
`prefer_over_mcp`, `ask_before_build`), all defaulting to the new behaviour and
all settable from the existing Settings → Tools section. Setting
`prefer_over_mcp = False` restores the pre-1.0.37 resolution order exactly.

### What could regress
- The resolution chain is shared by chat, code, swarm, bots and voice — a bug
  there is app-wide. `check_cli_priority.py`, `check_cli_needs_tool.py`,
  `check_parity.py` and the existing `check_skill_market.py` / `check_forge_*`
  suites are the guard; all must be green before merge.
- Boot must not break on a bad user tool → `load()` soft-fails per tool
  (Phase 1.5). Covered by a check that plants a malformed manifest and asserts
  boot-equivalent `load()` returns without raising.
- `check_packaging.py` must not start failing once tools exist on a dev machine
  → Phase 0.3.

### New check suites, in `check_all.py` order

| Script | Phase | Asserts |
|---|---|---|
| `check_cli_tools.py` | 1 | registry, manifest, runner, adapter, persistence |
| `check_cli_priority.py` | 2 | CLI beats MCP; switch restores old order |
| `check_cli_needs_tool.py` | 2 | fallback shape + ordered questioning |
| `check_cli_build.py` | 3 | draft/apply/smoke-test, safety refusals |
| `check_cli_page.py` | 4 | RPC surface + page wiring |
| `check_cli_consumers.py` | 5 | chat/code/swarm parity, no special-casing |
| `check_cli_safety.py` | 1-3 | slug shadow, `shell=False`, approval, packaging |

## Suggested sequencing

Phase 1 and 2 are the load-bearing work and can land together — after them a
hand-written CLI tool already outranks MCP. Phase 3 (builder) is the largest
single piece and depends only on Phase 1. Phase 4 depends on 3. Phase 5 depends
on 1 and closes the parity guarantee. Phases 6-7 are polish.

Estimated shape: Phase 0-1 one sitting; Phase 2 one sitting; Phase 3 the bulk;
Phase 4 the bulk; Phase 5 small; 6-7 trivial.

---

## Where the implementation diverged

Recorded because a plan that quietly disagrees with the code is worse than no
plan: the next person reads the plan and trusts it.

**`check_cli_safety.py` was not written.** Its three concerns are covered by the
suites that already exist rather than by a new one:

- *slug shadowing a real skill* — `CliToolSpec.validate(known_skills=…)` refuses
  a colliding name, and `check_cli_tools.py` asserts the refusal.
- *`shell=False`* — asserted directly in `check_cli_tools.py`, which runs a tool
  whose argument contains a shell metacharacter and checks it arrives intact.
- *packaging* — `check_packaging.py` learned the `CLI_TOOLS_DIR` anchor in
  Phase 0, so a packaged writable path fails the build there.

Splitting them into a fourth suite would have been four places to keep in step
for no extra coverage.

**The `needs_tool` offer runs through `pending.ask`, not a bespoke card.** The
plan described a new card on the chat page. In practice the existing question
mechanism already renders a card with buttons, already survives a page reload,
and already delivers the answer as the next turn — so `_needs_tool` was made
*async* and returns a question-shaped result with `needs_tool` riding alongside
it. This was found by `check_cli_needs_tool.py` failing: the original shape put
the payload in `data` and the loop, which reads `question`/`options`/
`question_id` at the TOP level, dropped it into the generic failure branch. The
offer never reached the user at all. The card needed no page change; the result
shape needed fixing.

**The forge consent question is suppressed while `ask_before_build` is on.**
Otherwise the user is asked twice about the same unmet capability — once by the
build offer and again by the forge — and the second prompt is the one that made
`needs_tool` unreachable. `_build_offer_enabled()` is the single place both call
sites read, so they cannot disagree.

**Matching needed a second corroboration path.** The plan said a prose match
counts only when a call-name word also matches. That is too strict for the case
the feature exists for: `do_the_thing` alongside "fetch me the prices off a
page" has no call-name overlap at all. The rule is now *either* a call-name word
matches, *or* two or more of the request's words do — one shared word is common
English, two is a description.


**The Code page's read-only tool lists were widened on purpose.** `_PLAN_TOOLS`
and `_CODE_EDIT_TOOLS` exist so the planner and editor can look at a project
without being able to change it. A built CLI tool is arbitrary code with no
equivalent guarantee, so adding these tools weakens that boundary — the planner
can now invoke a program that writes files.

It was raised as a decision rather than taken silently, and the call was to add
them anyway: a tool the user wrote and reviewed is one they chose to have
available, and the Code page is one of the surfaces the feature promises. The
alternative — keeping the lists closed — would have left a built tool invisible
on the page that built it.

What this does NOT do is make it silent. `_code_page_tools()` names the reason
in its docstring, and `check_cli_consumers.py` asserts both halves: the tool
reaches the lists, AND the read-only tools those lists exist for are still
there.

**A stdlib module named as a dependency killed the whole build.** This one was
found by calling the live builder against the installed app — no check suite
caught it, and a unit test would not have.

`draft` asks the model what the tool needs, and the model answered the word-count
case with `package: "collections"`. `collections` is in the standard library, so
`pip install collections` failed, and the builder treated that as fatal: it
returned before generating any code. A program needing nothing at all was
therefore unbuildable, and the error blamed a package that was already present.

The fix checks whether the module already imports before believing the install
failure. A genuinely missing dependency is still fatal — that install did need to
work — and the probe deliberately returns False when it cannot run, so it can
never invent a pass.

`check_cli_build.py` now covers it. Worth noting how the first version of that
check was WRONG: it reused the suite's shared `discover` stub, which returns an
empty package, so the install branch was skipped entirely and the check passed
whether or not the guard existed. It was only trusted after being run with the
guard removed and seen to fail.

**The model's example invocation invented a flag, and the tool was born broken.**
Found the same way as the last one — by running what had been built.

`draft` asks the model for an `EXAMPLE_ARGV` and builds the argv template from
it. The model writes that line by hand, and for the word-count tool it wrote
`["--file", "input.txt", ...]` while implementing `--input` / `-i`. The template
builder passed the unknown flag through verbatim.

The result was worse than a failed build: the tool registered, appeared in the
catalogue, validated, and then failed on every single call with
`unrecognized arguments: --file`. Nothing complained until it was used, because
`--file` was not a placeholder and so no validation looked at it.

`_params_from_source` was never wrong — it reads the real `add_argument` calls
and had the right answer all along. The bug was in `_template_from` treating the
model's hand-written example as equally authoritative. It now drops any flag the
schema does not declare, along with its value, since the schema is the only
authority on what the program accepts.

Two lessons worth keeping. The first: both bugs in this feature were found by
running the thing, not by reading it, and both had passing tests beforehand. The
second: the first version of this check also passed for the wrong reason — the
template it inspected already contained the placeholder for the flag it was
asserting about. It was only trusted after being run against the unfixed code.

**The Test button tested nothing.** `dry_run_args` was declared on the spec as
"args that exercise the tool without side effects, for the build-time smoke
test", and then never populated or read by anything — the same dead-field shape
as `test_params` beside it. `smoke_test` called every tool with no arguments, so
a tool with a required flag failed its own test with "nothing to run". The user
read that as "my tool is broken"; the tool was fine and the call was empty.

The builder now records a concrete argv on the spec, derived from the template
after the schema has had its say — so a flag the schema rejected cannot come
back in through the test. Required parameters get shaped sample values
(`--input` a path, `--url` a URL); optional ones are left to the program's own
defaults, because inventing `--top 10` changes what the test exercises. A tool
saved before the field existed derives its args on the fly rather than being
permanently untestable.

Every check in the suite passed explicit params, which is exactly why none of
them caught this. There is now one that passes none.

**Building a tool opened a browser.** `draft` reuses `skill_forge.discover`,
which navigates to DuckDuckGo in Chromium to research a library. The user
clicked "Write the tool" and got a browser window over their desktop with no
explanation. It also mostly hurt: it is where the `collections` suggestion came
from.

`discover` gained `web: bool = True`. The forge's own `forge()` keeps the
default — searching for a real third-party library is the point there — and only
the CLI builder passes `web=False`.

**There was no sign that anything was happening.** The button did grey out and
its label change to "Writing…", but the only other cue was a line of small grey
text, and a draft takes 15-45 seconds. An indeterminate bar plus a count-up
timer now run for as long as the draft does. A percentage was considered and
rejected: the backend reports no progress, and a number that crawls to 90% and
stops is worse than one that says plainly it is still working.

**The fix for the Test button did not fix the Test button, because the bug was
in the caller.** Worth recording in full, because it is the most instructive
failure in this feature.

`smoke_test` was taught to fall back to the spec's recorded args when it is
passed `None`. But its only caller — the `cliTools.test` handler — coerced a
MISSING `params` into `{}` before calling:

    call = params.get("params")
    if not isinstance(call, dict):
        call = {}

So `params is not None` was always true, the fallback was unreachable, and the
button behaved exactly as before. Deployed and confirmed still broken by the
user, with the identical error.

Two lessons, both already half-learned earlier in this session and neither
applied here:

The first: `{}` and `None` are different requests — "call it with no arguments"
against "I have no values, use the tool's own example" — and collapsing them
silently destroyed the distinction the fix depended on.

The second, and the reason the regression test was written the wrong way twice:
the check asserted against `smoke_test` DIRECTLY, the same way the broken code
called it in the author's head, so it exercised the fix and not the bug. A test
that calls the function you just changed proves the function works; it says
nothing about whether anything calls it that way. The check now drives the real
`cliTools.test` handler through the real registry with the payload the page
sends, and was confirmed to fail — with the user's exact error — before it was
trusted.

**A correct call to a working tool still failed, because the sample file did not
exist.** With the handler fixed, a bare Test finally ran the program — and the
program exited 1 with `File not found: input.txt`. The flags were right, the
values were right, and the file was a placeholder nobody had created.

`--file` is given the sample `"input.txt"` so the program can parse its
arguments. Nothing then made `input.txt`, so every tool that reads a file failed
its own smoke test. The user sees a red Test on a tool that is perfectly fine.

`smoke_test` now creates a real file for any argument that looks like a path,
in a temp directory it removes afterwards. Recognised by extension rather than
by flag name, because the flag says what the value is FOR and this needs to know
what it IS: `--input` may be a literal string, and `--source` may be a path.

This is the fourth bug in this feature found by running it, and the third that
had a passing test in front of it. The check written for the previous one
asserted only that the old error was ABSENT — which would have passed straight
through this, since `File not found` is a different string. It now asserts the
bare click reports `success`, and was confirmed to fail with the user's exact
message before being trusted.

**A flag could appear twice in the template.** Found by testing the path fix
against a tool that takes no file argument — the case I had not exercised,
because the previous three bugs were all about files.

Two separate steps add `--json`: the loop that walks the model's example, and
the loop that adds every declared flag the example did not reach. The code meant
to reconcile them tested whether `"json"` appeared AS A PLACEHOLDER — which is
never true, because `--json` is a boolean and takes no value — so the condition
was always satisfied and both steps ran. A tool whose example mentioned
`--json` produced
`["--word", "{word}", "--json", "--times", "{times}", "--json"]`.

For a `store_true` flag argparse shrugs, so this was invisible in every test
that ran. The template is now deduplicated by first appearance, which also keeps
the model's own flag order — the order it wrote to match its `--help` text. A
duplicate of a flag that TAKES A VALUE would have been a real mis-call, and
nothing would have caught it.

Worth noting how this surfaced: not from a bug report, and not from any check,
but from deliberately probing a shape the earlier fixes did not touch. The
pattern across all five bugs in this feature is the same — the code paths that
were exercised worked, and every defect lived in the ones that were not.

**`required` was inferred instead of read, in four wrong ways.** Found by
probing argument shapes the earlier fixes never touched, since every previous
bug involved a file.

The rule was "not (`default=` or `nargs=` or a boolean or an integer)". argparse
decides this itself and says so — an option is required only when it is declared
`required=True` — and all four inferences fail in one direction or the other:

- `nargs="+", required=True` was DROPPED from the schema, so a flag the program
  needs could never be set and **every call failed**.
- `add_argument("--name")` — optional to argparse — was ADDED, so the schema
  demanded a value nothing needed.
- `--count` with `type=int, required=True` was required only by accident of its
  type; the same flag with `type=str` would not have been.

Now read from the source with the one keyword argparse reads.

**A repeated flag left its value behind.** The deduplication added a moment ago
skipped the duplicated FLAG but not its placeholder, turning
`["--tags", "{tags}", "--tags", "{tags}"]` into
`["--tags", "{tags}", "{tags}"]` — a stray positional, which is exactly the
mis-call that comment predicted. The flag and its value are now dropped together.

**A positional argument was invisible, and made the tool uncallable.** The schema
is built from `add_argument("--flag")` calls, so a positional appears nowhere in
it and every call from chat or the swarm omits it. The prompt asks for
`--kebab-case` options, but nothing enforced it, and the failure was silent: the
tool registered, listed, and then refused every call.

Positionals are now REPORTED as a draft warning rather than rejected. The draft
is shown to a human who can rename the argument, and refusing outright would
throw the work away over something they may prefer to fix themselves.

Three bugs from one probe, none of them visible from the paths already tested.

**A parameter spelled differently was dropped, rendering an empty argv.** The
schema key is snake_case (`file_path`) and the flag is kebab-case
(`--file-path`), so a model reading the tool's own description may send
`file-path`. `argv_for` already accepted aliases and reported the miss, but
NOTHING EVER POPULATED `aliases`, so every near-miss was discarded. The call
then went out as `[]` — which fails as "nothing to run", blaming the tool for a
parameter the caller merely spelled differently.

Populated from the flag's own name only: `file_path` and `file-path` address the
same argument, and `path` deliberately does not, because mapping a synonym would
let a wrong value be accepted and look correct.

Two things worth keeping from this one. The map direction is
`{canonical: alias}` because that is how `spec.argv_for` reads it, and building
it the other way round does not raise — it looks for a parameter named after the
alias, finds none, and renders nothing, which is the same silently-empty argv the
fix was meant to remove.

And the check for it was wrong on the first attempt in the usual way: it
asserted `dashed == exact` rather than naming the argv each must produce. With
the map inverted BOTH lookups returned `[]`, so the two empty lists compared
equal and the check passed. Recording it because it is the fourth time in this
feature that a comparison against another computed value hid the bug, and the
fix is always the same — assert the literal expected result.

**Three bugs found only by running the real model.** Previous rounds tested
hand-written programs against the parser. Driving `cliTools.draft` on the live
app with three real capabilities produced three failures that no synthetic input
had produced, because the model writes code differently from how I imagined it
would.

**`["--json", "--json"]` — the example loop and the property loop both added
it.** A boolean never appears as a placeholder, so the property loop's "was this
parameter used?" test always reads it as unset and appends the flag; when the
model's own example already contained `--json`, the example loop had appended it
too. Harmless to argparse for a `store_true` flag, but a malformed template — and
the identical collision on a flag that takes a VALUE is a real mis-call.

Recorded with a correction: I first reported this as "the dedupe ran before the
last append" and fixed the ordering. That fix was real but addressed a different
path, and the shape I described did not reproduce. It was only when I ran the
INSTALLED builder by path — rather than the repo one, which `sys.path` kept
serving me — that the actual condition showed itself, and it depended on the
example containing the flag. Reporting the first plausible cause as the cause is
what this entry exists to warn against.

**The check for it passed against the broken code.** The first version tested
only `example == []`, where the broken implementation happened to be correct, so
it went green on code that produced the duplicate. The loop now covers
`["--json"]` as well. This is the FOURTH check this session that passed without
testing anything, and the pattern is always the same: an input chosen without
checking it reaches the bug. Every check here was subsequently run against the
installed builder and seen to fail before being trusted.

**A positional argument, again, from the model this time.** Asked to count lines
in a file, the model wrote `add_argument("file")` — clean, correct code that the
schema cannot express. The previous positional guard read the TEMPLATE, and a
positional is precisely the thing a template has no slot for, so it never fired.
Detection now reads the `add_argument` calls that do not begin with `--`.

The prompt also now says `add_argument("--file")`, never `add_argument("file")`,
with the exact error the caller would see, because "read every input from
command-line flags" described the intent without forbidding the spelling.

**`ModuleNotFoundError: No module named 'pandas'`.** Discovery suggests a
library from what the open web says about the task — a good guess at the right
library, and no evidence at all that it is installed. `_context_block` asserted
such a package "will already be installed", which is how the model came to build
on pandas. Availability is now CHECKED with `importlib.util.find_spec`, and an
absent package is named as absent with the standard library as the fallback;
telling the model only "not installed" invites it to import it anyway.

All three are the class of thing that cannot be found by testing the layer just
changed. They needed the real generator, and each cost one generation to
surface.

**The tool is now RUN before it is shown, not after it is saved.** This was the
last significant gap, and the user asked about it directly: nothing verified a
draft until the user pressed Test, by which point it had already been written
to disk, registered, and offered to chat, the swarm and the code page. A tool
that failed its first real call was indistinguishable from a working one on the
review screen.

`draft` now runs the source from a temporary directory — so it still writes
nothing permanent — and reports the outcome on the review screen. On failure the
model is asked ONCE more, with the real error fed back into the prompt, because
an error from code that actually ran is evidence rather than speculation.
A second failure is shown anyway: the point is to replace "this might work" with
"here is what happened", not to withhold source from someone who may want to fix
it by hand.

**The first version of this test passed a broken tool.** It ran the invocation
derived from the schema, and a positional argument has no schema slot — so the
program ran with the positional simply ABSENT, exited 0, and the test reported
success on a tool that would fail every call. This is the exact false-pass the
test exists to catch, produced by the test itself. Positionals are now fed a
value, taken from the model's own `EXAMPLE_ARGV` when it has one.

**And the check for that was itself worthless.** It set a stub provider, but
`draft` calls `forge_target_provider` to choose the writing model, and earlier
checks in the same file had already replaced that chooser with one returning a
different program. The check ran against `GOOD_PROGRAM` and asserted nothing
about the source it had written. Found by printing what `draft` actually
received — `['--path', '__FILE__', '--limit', '3']` instead of the positional
source. Passing a provider is not enough; the chooser has to be replaced too.

Worth noting the shape of both mistakes: the feature worked in isolation and
failed in the suite, and the difference was state left by earlier checks. A test
that runs in a dirty environment can pass or fail for reasons unrelated to the
code, which is why this one was verified in both directions.

**A folder flag was given a file, so good code failed its own test.** With the
draft-time test deployed, the first real run of "list the files in a directory"
failed twice. The tool was CORRECT — it declared `--directory`, rejected
`input.txt` with `NotADirectoryError`, and said so readably. The fault was the
sample: `_sample_for` matched `dir` in the same branch as `path`/`file` and
returned `input.txt`, and `_materialise_paths` only ever created FILES, deciding
by extension — which a directory does not have.

So the harness's own placeholder was the defect, and the smoke test accused
working code of being broken. Exactly the failure mode the materialisation step
exists to prevent, one level up: it had been fixed for files and never
considered for directories.

Both halves are now handled — the sample is directory-shaped for
`dir`/`folder`/`directory` flags, and it is created as a real directory
containing a file and a subdirectory, so a tool that lists a folder has
something to list. This one is worth recording because it is the first bug found
BY the draft-time test rather than in it, which is the test doing its job.

Note also what the two failures DID look like from the outside: identical. The
retry ran, failed the same way, and the second failure is reported. That is
correct behaviour — a retry cannot fix a bad test fixture, and the model had
nothing to correct — but it means a harness fault and a model fault are
indistinguishable from the warning text alone. Only the tool's own error message
separated them.

**A fixture fault and a code fault now report differently.** Raised by the
folders bug above: from the user's side, "the sample was wrong" and "the tool is
broken" produced identical screens, and the retry was spent asking the model to
correct a program that had never been wrong.

`smoke_test` now marks a failure as `fixture` when the operating system's own
path error names one of the samples we passed. The retry is skipped in that
case, because a second generation cannot fix a bad sample, and the message says
so.

The first version of the classifier was WRONG, in the direction that matters: it
matched on the error text alone, so a tool that hardcoded
`open("definitely-not-here.txt")` — real breakage, the kind this feature exists
to catch — was reported as a problem with the test fixture. Excusing a genuine
fault is worse than the vagueness it removes. It now requires the error to name
one of OUR sample values, compared on basename because a tool may report the
path as received or as resolved. Verified both ways: our sample → fixture, a
hardcoded path → not.

A `NameError: name 'argv' is not defined` shipped in the same change and was
caught only by running a draft that actually FAILED its test. Every earlier run
passed, so the branch referencing it was never entered — the same
unexercised-path shape as everything else in this file.


**Phase 7 (MCP export) was not built.** It was marked optional and stays that
way; nothing in 1.0.37 depends on it.

## Outcome

Seven new check suites (`check_cli_tools.py`, `check_cli_priority.py`,
`check_cli_needs_tool.py`, `check_cli_build.py`, `check_cli_page.py`,
`check_cli_consumers.py`, plus the CLI case inside `check_parity.py`), all green.
`scripts/check_all.py` runs 79 suites; the failures it reports are pre-existing
and unrelated — verified by stashing this work and re-running to get the same
set. The exact count varies with the temp data directory (10–12 of 12), because
`check_remote_gateway.py` and `check_tailscale.py` want prior state there.

**NOT yet deployed.** `scripts/deploy_to_install.ps1 -WithDashboard` was
dry-run successfully (backend, bots and dashboard stages all prepared) but
needs an ELEVATED shell to actually write into `C:\Program Files\Addled`, and
the session that built this was not elevated. Run it as Administrator, then
relaunch `Addled.exe` — the backend respawns itself, but the dashboard does not.
