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
