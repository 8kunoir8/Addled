# Addled 1.0.34

**Chat and Code can hand work to a swarm agent.** Ask for the Reviewer to check
something and it happens, in the conversation, without visiting the Swarm page.

## What you can do now

> "have the reviewer look at the parser changes"

Addled acknowledges straight away — *"Reviewer is working on that now. The answer
will arrive as a message when it is done."* — and its turn continues. When the
agent finishes, its answer appears in the chat as a message from that agent, a
toast shows on whatever page you moved to, and it reaches your phone through the
bot bridges if you have them.

The Code page can do the same, because its planner runs through the same
pipeline as chat.

## Why it does not wait

A swarm agent runs a full reasoning turn — its own brief, its own skills, its own
model, possibly several tool rounds. That is minutes, not seconds. Holding your
chat open for it would look like a hang, and the character would sit on
"thinking" for work it is not doing. So the delegation starts the agent and
reports back; the answer arrives the same way a scheduled reminder does.

## One level deep, on purpose

A swarm agent runs its task through the same pipeline a chat turn does, which
means an agent *is* a chat turn — and holds this skill itself. Without a limit,
an agent could delegate to an agent, forever, on a local model that runs one
generation at a time so the nested work would simply queue behind itself.

So the allowance is **one level**: your chat or code turn may delegate, and the
agent it delegates to cannot delegate again. An agent that is asked to try says
so and does the work itself instead.

## Verified

- `scripts/check_swarm_delegate.py` is new and covers the skill, the depth
  brake, the refusal inside a delegation, and the wording of the error when an
  agent name is wrong (it lists the agents you actually have).
- `check_reachability.py` caught this before release: the skill was registered
  but **no realistic phrasing would ever offer it**. That is now covered.
- `scripts/check_all.py` runs **72 suites**, all green.

---

# Addled 1.0.33

**"Install Playwright" and "Install browser-use" did nothing if Addled was
installed to `C:\Program Files`.** The exact same bug that broke the vision
install in 1.0.30 broke browser automation here.

## The bug

The Settings → Browser buttons ran:

```
pip install playwright --no-warn-script-location
pip install browser-use --no-warn-script-location
```

with no destination specified. Under a per-machine install the bundled
interpreter defaults to its own `site-packages` inside `Program Files`, which is
read-only. pip downloaded the packages, then died at the final copy:

```
ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied
```

The UI returned the generic message:
*"the install did not finish. check the network and try again — the reason is in the log"*.

## The fix

- **The install targets the writable directory.** Both pip commands now pass
  `--target %LOCALAPPDATA%\Addled\pylibs`, which is added to `sys.path` so the
  packages are importable immediately without a restart.
- **The Playwright CLI sees the packages.** `playwright install chromium` runs
  through an inline runner that inserts `pylibs` into `sys.path` before
  importing, because a fresh subprocess under `-s` ignores `PYTHONPATH` and the
  embeddable Python has no user site-packages.
- **Success is verified.** The import is checked after pip finishes, before
  reporting "done" to the UI.

## Verified

- `scripts/check_browser_autoinstall.py` is new (#71); `scripts/check_all.py`
  runs **71 suites**, all green.

---

# Addled 1.0.32

One more store was still writing beside the code, found by auditing the
installed app rather than the source.

## The journal

`backend/memory/journal.py` kept its daily journal in
`<install>/backend/memory/journal`. Under a per-machine install that is inside
`C:\Program Files`, so every write failed:

```
WARNING addled.journal: journal save failed: [WinError 5] Access is denied:
'C:\Program Files\Addled\resources\backend\memory\journal'
```

This one was easy to miss for two reasons. It is a **directory**, not a file, so
there was no `.json` suffix to notice; and it fails as a **warning** — the
journal is written best-effort, nothing waits on it, so chat kept working and
only the timeline silently stayed empty.

It now resolves through `app_paths` like the other thirty locations.

## Why three rounds of this

Each pass fixed a different *shape* of the same mistake, and each check only
recognised the shape it was written for:

1. `Path(__file__).parent.parent / "memory" / x` — nineteen modules.
2. `Path(__file__).parent / "x.json"` — ten state files that live *inside*
   `backend/memory/` and so never mention `"memory"` at all.
3. `Path(__file__).parent / "journal"` — a directory, with no suffix to match.

`check_data_dir.py` now recognises all three, and the ones that are correct are
named with their reason rather than merely tolerated: the shipped starter skins,
the bundled embedder and voice weights, and the tool lookups that already try the
install directory first and fall back to a per-user one. A check that flags
correct code is worse than no check, so each exception is deliberate and
reviewed.

## Verified

- Audited the **installed** 1.0.31 app: 33 RPCs across every page, all 30 state
  locations, chat (including history saving), memory, vision, and all 13
  dashboard routes.
- `scripts/check_all.py` runs **70 suites**, all green.

---

# Addled 1.0.31

"Install torch + transformers" did nothing if you installed Addled to
`C:\Program Files`, and saving a conversation failed there too. Both are fixed.

## The install button

The button ran:

```
pip install --target C:\Program Files\Addled\resources\python\Lib\site-packages torch transformers …
```

That target is inside `Program Files`, which a normal user cannot write. pip
fetched everything — several gigabytes — and then died at the final copy:

```
PermissionError: [WinError 5] Access is denied: …\site-packages\einops
```

The button returned to "Install" and the packages were absent, which reads as
*nothing happened* rather than *it failed*. Fixed:

- **The target is checked.** Addled keeps using its own `site-packages` when
  that folder is writable — so a portable or per-user install is unchanged — and
  falls back to `%LOCALAPPDATA%\Addled\pylibs`, which is added to the import
  path at startup.
- **Success is verified, not assumed.** `torch` and `transformers` are imported
  after pip finishes; if either is missing the install is reported as failed.
- **A failure now reaches the UI**, with the reason.

## Ten more files that wrote beside the code

1.0.28 moved the *directory* the app writes to, and fixed nineteen modules that
computed it. It missed ten that live **inside** that directory and so never
mentioned it by name — `chat_history.json`, `facts.json`, `vectors.db`,
`links.db`, `triples.db`, `session_context.json`, `session_summaries.json`,
`user_profile.json`, `rolling_summary.json`, `maintenance_state.json`.

Each was still built as `Path(__file__).parent / "<file>.json"`, so under
`Program Files` they kept trying to write into a read-only folder. The visible
symptom was:

```
ERROR addled.ws: Chat failed
PermissionError: [Errno 13] Permission denied: …\backend\memory\chat_history.json
```

— every message failed once the app tried to save it. All ten now resolve
through `app_paths` like the rest.

`check_data_dir.py` was only looking for a path containing `"memory"`, which is
why it passed while these were broken. It now also flags a `__file__`-relative
path ending in a file the app would write, and fails by name on the exact shape
that was invisible (verified by reintroducing one).

## Verified

- Reproduced the original `PermissionError` with the installed interpreter, then
  confirmed the fixed code chooses a writable target, puts it on `sys.path`, and
  installs a real package that then imports.
- `scripts/check_all.py` runs **70 suites**, all green.

---

# Addled 1.0.30

**"Install torch + transformers" did nothing if you installed Addled to
`C:\Program Files`.** It downloaded the whole stack, then threw it away. This
release makes the button work, and makes a failure say so instead of failing
silently.

## The bug

The button ran:

```
pip install --target C:\Program Files\Addled\resources\python\Lib\site-packages torch transformers …
```

That target is inside `Program Files`, which a normal user cannot write. pip
fetched everything — several gigabytes — and then died at the final copy:

```
PermissionError: [WinError 5] Access is denied: …\site-packages\einops
```

Two things made that invisible:

- the destination was chosen without checking whether it could be written to,
  and
- success was decided by pip's exit code alone, so a `--target` install into a
  folder the app cannot import from still counted as done.

The button simply returned to "Install" and the packages were absent, which
reads as *nothing happened* rather than *it failed*.

## The fix

- **The install target is now checked.** Addled keeps using its own
  `site-packages` when that folder is writable — so a portable or per-user
  install behaves exactly as before — and falls back to
  `%LOCALAPPDATA%\Addled\pylibs` when it is not. That folder is added to the
  import path at startup, which is what makes an install there visible to the
  app (`-s` means the interpreter only searches its own directories).
- **Success is verified, not assumed.** After pip finishes, Addled imports
  `torch` and `transformers`. If either is still missing, the install is
  reported as failed with the reason, rather than silently reported as done.
- **A failure now reaches the UI.** A failed install broadcasts its phase and
  the error text, so the page can say what went wrong.

## Verified

- Reproduced the original `PermissionError` with the installed interpreter, then
  confirmed the fixed code chooses a writable target, puts it on `sys.path`, and
  installs a real package that then imports.
- `scripts/check_vision_install.py` is new; `scripts/check_all.py` runs **70
  suites**, all green.

---

# Addled 1.0.29

A small cleanup release. The fix in 1.0.28 — starting the backend from a
read-only install directory — works, and this stops it leaving clutter behind.

## What changed

When Addled has to move its data into your user folder (because it is installed
somewhere it cannot write, such as `C:\Program Files`), it copies any existing
state across on first run. That copy was too broad: `backend/memory/` holds the
memory **modules** next to the state they use, so the module files came too.

They were inert — nothing imports from the data folder — but they left a stale
second copy of the app's own code sitting in `%LOCALAPPDATA%\Addled`, which is
misleading to anyone who looks there. The migration now copies only state:
`.py`, `.pyc`, `.pyo` and `__pycache__` are skipped.

If you installed 1.0.28 you were unaffected in every practical sense — this only
touches the first-run migration, which had already happened. It matters for a
fresh install on a machine that already has data.

## Also

- `scripts/check_data_dir.py` now asserts that only state files are migrated, so
the copy cannot quietly widen again.
- `scripts/check_all.py` runs 69 suites, all green.

## The 1.0.28 fix, for context

If you are coming from 1.0.27: installing to `Program Files` used to leave you
with a character that said *Backend not running*, because every writable path was
resolved beside the code. `backend/app_paths.py` now decides — the install
directory when it is writable, your user folder when it is not — and the startup
log names which it chose.

---

# Addled 1.0.28

**The backend could not start when Addled was installed to `C:\Program Files`.**
This release fixes that. If 1.0.27 left you with a character that says "Backend
not running", this is the fix.

## The bug

Every writable thing the app owns — its log, `settings.json`, the memory
databases, the wiki, downloaded models, learned skills — was written **beside the
code**, in `backend/memory/`. That only works when the install directory is
writable, which it was while the installer placed Addled in your own user folder.

If you chose (or the installer defaulted to) `C:\Program Files`, that is an
administrator-only location, and the backend died on its first act:

```
PermissionError: [Errno 13] Permission denied:
'C:\Program Files\Addled\resources\backend\memory\addled.log'
```

Nothing in the UI said why. The dashboard showed **Backend not running** and the
status dot went red, which looks like a broken install rather than a permissions
problem.

## The fix

One module — `backend/app_paths.py` — now decides where Addled's data lives, and
nineteen separate files that each computed their own `memory/` path use it.

- **Writable install → unchanged.** If the install directory can be written,
data stays exactly where it has always been. Existing portable and per-user
installs are not moved.
- **Read-only install → falls back to your user folder.**
`%LOCALAPPDATA%\Addled` on Windows, `~/.local/share/addled` elsewhere, and the
startup log names which one it chose:

```
Data: C:\Users\you\AppData\Local\Addled (portable=False)
```

- **Your existing data is brought across.** An install that upgrades from
"writes beside itself" finds its old `memory/` copied into the new location once,
on first run. Nothing is deleted, existing files are never overwritten, and an
interrupted copy simply runs again next time — so a downgrade still finds the
original.
- **The writability test is a real write, not `os.access`.** On Windows
`os.access` reports `Program Files` as writable because the denial comes from an
ACL it does not consult. A test write is the only answer that is true.
- **`ADDLED_DATA_DIR`** overrides the whole thing if you want Addled's state
somewhere specific.

## Verified

- Reproduced the failure by making an install directory write-denied, then
  confirmed the fixed backend starts from it and writes its log and settings to
  the fallback location.
- `scripts/check_data_dir.py` is new and covers the resolver, the real-write
  probe, the fallback, the override, migration (including "do not overwrite" and
  "run twice"), and a guard that no module starts computing its own `memory/`
  path again.
- `scripts/check_all.py` runs 69 suites, all green.

---

# Addled 1.0.27

The dashboard becomes a **frameless bubble window** — no OS title bar, no menu —
and a question the model asks can now be answered from the character instead of
only from the dashboard.

## The dashboard is a bubble now

The window no longer wears an operating-system title bar or Electron's stock
File/Edit/View/Window/Help menu. It is a rounded, framed-in-software surface with
its own header: the app name, the character-state dot at a glance, and
**minimise / maximise / close** on the right.

- **Drag by the header, resize by the edges.** With no frame there is no title
  bar to drag, so the header is the drag region; the window still resizes from
  its edges because a frameless window keeps `WS_THICKFRAME`.
- **The ✕ hides to the tray**, exactly as the old close button did. It does not
  quit, so the character and any running work survive.
- **An icon rail instead of a tall sidebar.** The 13 sections sit in a 52-pixel
  rail that widens on hover to show its labels, giving the page back its width.
- **No repeated titles.** Every page already draws its own title and connection
  state, so the window header carries only what the pages cannot — the app's
  identity, the window controls, and a warning when the backend is down.

### A note on this one

The window chrome lives in `electron/main.js`, which is packed inside
`resources/app.asar` — so this change is visible only after the installer runs,
not from a backend or dashboard deploy. That is why it arrives in a release.

## Answering a question from the character

When the model needs a decision it cannot reasonably guess, it asks rather than
silently picking. That question already reached the character's bubble; now the
character can be *answered* there.

- **Type at the character while a question is up and your words answer it**,
  instead of starting a new request. The prompt box says which question is being
  answered, so the input is never ambiguous.
- **The answer settles the question and resumes the work.** The turn that was
  waiting on the decision continues, so nothing stops at the question.
- **A permission prompt is not answerable this way, on purpose.** A free-text
  reply to "may I run this?" is not something the backend accepts, so a message
  typed during an approval is still an ordinary request — and a refused answer
  says why rather than being swallowed.

## Fixed

- **The window no longer shows a page title twice.** The first version of the
  bubble header printed each page's name and connection state above the copy the
  page already draws, so "Chat" appeared twice, stacked.
- **`package.json` advertised build targets that did not exist.** It carried
  `"build:all": "electron-builder --win --mac --linux"` while the packaging
  configuration defined only `win:`, so `--mac` and `--linux` built nothing and
  reported success. The script is gone, and `check_packaging.py` now fails if a
  build script passes a platform flag the config cannot build — and, the other
  way, if a configured platform has no script that builds it.
- **`check_packaging.py` reported a broken manifest as a stack trace.** A
  malformed `package.json` crashed the check instead of naming the problem; it now
  reports the JSON error with its line and column.
- **The character prompt can answer the question on screen**, covered by
  `check_open_question.py` so a permission prompt is never mistaken for one.

## Notes

- `check_ask_user.py`, `check_open_question.py` and `check_decision_surfaces.py`
  cover the question lifecycle; `scripts/check_all.py` runs 68 suites.
- Windows: NSIS installer and portable build, both x64.

---

# Addled 1.0.26

A large release: the Code page is rebuilt around sessions and review, the chat
composer takes the same inputs, and a set of real bugs found by testing against
the installed app are fixed.

## Code page — rebuilt

The working area is now a sidebar of **sessions** beside a transcript-or-editor
column with the composer at the bottom, and context chips showing the workspace,
the git branch and the active session.

- **Sessions**: work is grouped into named sessions, titled from your first
  request, pinnable and deletable. A session belongs to the folder it was
  started in, so binding a workspace reopens the session you were last working
  in there.
- **The composer takes what you have**: paste a screenshot, paste text, drop a
  PDF or document, name a file with `@`, or attach the lines selected in the
  editor. An image goes to the vision model, text is sent as content, and
  document types are read by the backend.
- **Right-click where you already are** — on a file (open, attach to prompt,
  copy path, reveal, add its folder as context), on a selection (explain,
  refactor, write tests, find the bug, document), and on the tree (new file,
  refresh, collapse, clear filter).
- **A Changes view**: branch, modified and untracked files, and the uncommitted
  diff — plus a button to run the project's own check on demand.

### Applied changes are now reversible from the UI

The backend was already returning a commit id and a verify verdict on every
apply, and the page was discarding both. It no longer does.

- **Undo**: an applied change shows an **Undo this change** bar. In a repository
  it runs an ordinary `git revert` — an inverse commit, never a rewrite of your
  history. In a plain folder it restores the `.bak` kept beside each file.
- **Verified, not just written**: the check the backend ran is reported — green
  when it passed, amber when it did not, and plainly *not verified* when no
  check could be found. There is no green tick over nothing.
- **No tests found** is reported as such. Most runners exit non-zero when they
  collect nothing, and calling that "the tests failed" tells you your code is
  broken when the project simply has no matching tests.

## Chat — same inputs as Code

The chat composer now accepts all four: drag & drop, a file picker (widened to
documents), and **paste** of images and files. Pasted *text* stays in the
message box. Both composers share one implementation, so they cannot drift.

## Fixed

- **The dashboard's static server 404'd every Next.js prefetch payload.** Next
  writes a route's RSC payload as `chat/__next.chat/__PAGE__.txt` but the client
  asks for `chat/__next.chat.__PAGE__.txt`. The root route's on-disk name already
  had the dots, so only sub-routes failed — pages still loaded, which is why it
  went unnoticed. The cost was that every sidebar prefetch failed, silently
  downgrading client-side navigation to a full page load.
- **`git status` reported truncated file names.** The porcelain format uses a
  fixed two-column status field, so a leading space is data. Stripping it turned
  `" M app.py"` into `"M app.py"` and reported the file as `pp.py` — first two
  characters gone, first line only.
- **Internal turns were being learned as procedures.** The Code page's planner
  makes two model calls per request that you never see, and every one was stored
  as a "learned procedure" titled after its own internal prompt. Those entries
  then competed with the real built-ins when matching.
- **A merged procedure could never replace an installed one.** Seeding could only
  add, so two near-identical file procedures survived on any existing install and
  the matcher correctly refused the tasks they both fitted.
- **The sidebar could squeeze the working column to zero width** in a narrow
  window: the drawer is fixed-width and never yielded. It now collapses below a
  breakpoint, remembers a manual toggle, and is capped at a fraction of the
  viewport.
- **The process library** now distinguishes a workspace with no check from one
  whose check failed, and the reliability counters it records are actually read
  when ranking.

## Notes

- An upgrade keeps your setup: settings, memory, skills, tools, MCP servers and
  the swarm roster all live beside the install, and the installer ships no copy
  of any of them.
- The RSC fix is in the Electron shell, so it takes effect with this release
  rather than by updating the dashboard files alone.

## Verification

61 check suites pass, and the Code and Chat pages were driven against the
installed app over its real WebSocket: a plan produces a diff, a write keeps a
backup holding the *previous* content, an escaping path is refused, and an
invalid commit id is rejected without leaving a half-finished revert behind.
