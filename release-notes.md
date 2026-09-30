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
