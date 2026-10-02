# Addled

> An AI desktop companion with a floating animated character, full web dashboard,  
> voice interaction (wake word + speech), **141 built-in skills**, self-extending  
> capability forge, 10 AI provider backends, live screen awareness, long-term  
> memory, **Office & PDF document handling**, and a full safety suite.  
> Built with Python 3.14 + PyQt6 + Electron + Next.js 16.
>
> **Repository**: [github.com/8kunoir8/Addled](https://github.com/8kunoir8/Addled)  
> **Releases**: [github.com/8kunoir8/Addled/releases](https://github.com/8kunoir8/Addled/releases)

## Quick Start (Development)

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Start the backend (Python + WebSocket server + character widget)
cd backend
python main.py

# 3. In another terminal, start the dashboard
cd dashboard
npm install
npm run dev

# 4. In a third terminal, start the Electron shell (optional)
cd ..
npx electron .
```

> **The dashboard must be running before Electron.** In development the Electron
> shell loads `http://localhost:3000` and shows nothing until that server answers
> — the window is created but stays hidden, so a missing dashboard looks like a
> crash. Start step 3 first, then step 4.
>
> Electron in development also spawns the backend with whatever `python` is on
> `PATH`, not the bundled interpreter. If that Python lacks the app's packages
> you will see `ModuleNotFoundError: No module named 'PyQt6'` and the backend
> will restart five times and give up; the window still opens, but the pages
> report *Backend not running*. Use the packaged build, or point `PATH` at
> `python-bundle\python.exe`, if you want the full stack in development.

Or use the one-click launcher:
```bash
launch.bat
```

> **Self-contained installer**: `build.bat` bundles Python 3.14.7 + all core
> dependencies — no Python install needed on the target PC.
> Optional extras: local vision (`torch` + `transformers` + `einops` + `timm`,
> ~400 MB) and browser automation (Playwright + Chromium, ~150 MB) — both
> installable from the app itself (Settings → Local AI for vision, Settings →
> Browser for Playwright). Vision is installed on demand rather than bundled,
> because 400 MB of torch on every download is a steep price for an offline
> fallback most setups never need. The exact package list lives in
> `requirements-vision.txt`, which the app reads so the two cannot drift.
> The **local AI model** (llamafile + Qwen3-8B-Q4_K_M, ~5.03 GB) is downloaded on demand
> after you approve it — never bundled.
> Voice models are fetched by `scripts/fetch_voice_models.py` (~650 MB:
> Silero VAD + Kokoro TTS). STT uses faster-whisper (downloads on first use;
> SenseVoice auto-activates if funasr is installed).

---

## Architecture

```
┌─────────────────────────────────────────┐
│           Electron Shell                │
│  ┌───────────────┐  ┌────────────────┐  │
│  │Next.js Dashboard│  │ Bot Bridges    │  │
│  │14 pages        │  │Telegram/WA/    │  │
│  │Chat/Goals/Code │  │ Discord        │  │
│  │Swarm/Browser/  │  │                │  │
│  │Calendar/Bots/  │  │                │  │
│  │Remote/Wiki/    │  │                │  │
│  │Memory/Skills/  │  │                │  │
│  │Procedures/     │  │                │  │
│  │Settings        │  │                │  │
│  └───────┬───────┘  └───────┬────────┘  │
│          │ WebSocket         │ HTTP      │
├──────────┼───────────────────┼───────────┤
│          ▼                   ▼           │
│  ┌────────────────────────────────────┐  │
│  │      Python Backend Service        │  │
│  │  Engine • 203 WS Handlers          │  │
│  │  141 Skills • Skill Forge          │  │
│  │  10 AI Providers • Goals • Code    │  │
│  │  Swarm • Calendar • Email • Browser│  │
│  │  Office & PDF • Bots • Memory •    │  │
│  │  Character • Safety • Voice •      │  │
│  │  Observer • Onboarding             │  │
│  └────────────────┬───────────────────┘  │
│                   │ Qt Signals           │
│  ┌────────────────▼───────────────────┐  │
│  │   Floating Character (PyQt6)       │  │
│  │   12 states • 6 shapes • particles │  │
│  └────────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

| Layer | Stack | Status |
|-------|-------|--------|
| AI Backend | Python 3.14, asyncio, PyQt6, websockets, httpx | ✅ Built |
| Character Engine | PyQt6 QWidget, QPainter, 30fps animation loop | ✅ Built |
| Skill System | Provider-agnostic function calling + auto-forge | ✅ Built |
| Dashboard | Next.js 16, TypeScript, Tailwind CSS | ✅ Built |
| Desktop Shell | Electron 28, system tray, auto-updater, backend auto-respawn | ✅ Built |
| Bot Bridges | Node.js (grammY, Baileys, discord.js) | ✅ Built |
| Communication | WebSocket JSON-RPC 2.0 (203 handlers) | ✅ Built |

---

## Features

### 🎭 Floating Character
12 animation states (idle, listening, observing, thinking, has_suggestion, acting, speaking, sleeping, blocked, error, working, dreaming) — all fully wired to real agent activity (thinking while the LLM works, speaking during TTS, acting during action execution, error on failures, blocked under privacy guard). 6 vector shapes, cursor gaze tracking, breathing/stretch animations, glow effects, mood tinting, progress ring, 7 particle types. **Chat-bubble replies** pop above the character when you click it.

**Prompts on the character are clickable.** A permission request or a question appears on the bubble with its own buttons, so the surface that is always on screen is one you can answer on rather than one that tells you to go and find the dashboard. A decision bubble takes an amber border and **does not auto-dismiss or close on a click** — a prompt that fades away on its own leaves the work behind it blocked and the reason unseen. Ordinary replies keep their usual read-then-fade behaviour.

**Ask an open question and the answer goes back to the work.** When the model needs a decision it cannot guess — which file, which reading, which of two approaches — it asks instead of assuming, and the question appears both on the character and on the dashboard. Typing at the character while a question is up **answers that question** rather than starting a new request, and the prompt box says what is being answered. Answering settles the question and resumes the turn that was waiting on it, so the work continues instead of stopping at the question. A permission prompt is deliberately *not* answerable this way: a free-text reply to "may I run this?" is not something the backend accepts, so a message typed during an approval is still an ordinary request.

### 🪟 Bubble window (frameless dashboard)

The dashboard is a **frameless, rounded window** rather than a normal application window: no OS title bar and no File/Edit/View/Window/Help menu. The shell draws its own 38-pixel header — the app name, the character-state dot, and **minimise / maximise / close** controls — and that header is the drag handle.

- **Close means hide.** The ✕ sends the window to the tray, exactly as the system close button did before the frame was removed, so the character and any running work stay alive.
- **Still resizable.** `frame: false` removes the OS chrome but keeps `WS_THICKFRAME`, so the window resizes from its edges.
- **An icon rail, not a tall sidebar.** The 13 sections sit in a 52-pixel rail that widens on hover to reveal its labels, so the page keeps the width.
- **No duplicated titles.** The header carries only what the pages cannot — the app's identity and the window controls. Each page still shows its own title and connection state, so nothing is printed twice.
- **Close still means hide.** A frameless window has no OS close button, so the header's ✕ is wired to the same path the frame used: it hides to the tray and does not quit.

### 🦊 Sprite Skins (codex-pet style)
Upload any animated GIF or a ZIP of per-state GIFs in Settings → Character and the floating character becomes that pet — all 12 agent states keep working on top (thinking/error/sleeping effects included). State clips map by file name (`idle.gif`, `thinking.gif`, …); skipped states fall back to `idle.gif`. Ships with the **Neon Panda** starter skin out of the box.

### 💬 AI Chat
Full chat UI with streaming responses, markdown rendering, conversation history. **Attachments** — paste an image straight from the clipboard, paste text, drag & drop, or pick a file: images are analyzed by the visual model (local Florence-2 or provider vision) and described to the main model, text files are inlined, and document types are read by the backend. Pasted *text* stays in the box where you expect it, and a whole-file paste is treated as a paste rather than as something you typed. Proactive insights arrive as 💡 messages in chat and bubbles on the character. Works with any configured AI provider. Supports skill-based tool calling across all providers.

### 🔌 10 AI Providers
| Provider | Type | Vision | Streaming |
|----------|------|--------|-----------|
| **Addled Local (llamafile)** | Local | — | ✅ |
| **OpenRouter** | Cloud | ✅ | ✅ |
| **Hugging Face (Local)** | Local | — | — |
| DeepSeek | Cloud | — | ✅ |
| OpenAI (GPT-4o) | Cloud | ✅ | ✅ |
| Claude | Cloud | ✅ | ✅ |
| Gemini | Cloud | ✅ | ✅ |
| GitHub Copilot | Cloud | — | ✅ |
| Ollama | Local | — | ✅ |
| LM Studio | Local | — | ✅ |

**Local by default**: with no API key configured, Addled runs **Qwen3-8B-Q4_K_M**
(4-bit GGUF) through **llamafile** on `127.0.0.1` — fully offline, no account. The
model is **not** bundled with the installer: Addled asks once (setup wizard and a
dashboard prompt) before downloading ~5.03 GB, and the download resumes if it is
interrupted. Decline and Addled falls back to **OpenRouter**
(`nvidia/nemotron-3-ultra-550b-a55b:free`) — paste a key in Settings → Providers.
`Hugging Face (Local)` runs any Hugging Face chat model in-process with
`transformers` (optional install from Settings).

**Local vision is a second, separate install.** Florence-2 captioning needs
`torch`, `transformers`, `einops` and `timm` — the last two are imported by
Florence-2's own code the moment it loads, so a torch-and-transformers-only
install fails on the first image. Settings → Providers → Local AI reports the
two capabilities apart, because they genuinely differ: *chat ready · images
need deps* means text works and only the vision stack is outstanding. The
button installs from `requirements-vision.txt` into the app's own
site-packages — the bundled interpreter runs with `-s`, so user
site-packages are invisible to it and installing there would silently do
nothing.

**Runs only when you use it**: the local server never preloads at launch. It starts
when *Addled Local* is the selected provider (Settings → Providers → *Start
automatically when selected provider*) or when *Keep local AI running* is switched
on — and any message routed to it starts it on demand (~3 s). While it is not
selected or kept, it stays stopped and frees its RAM after `local_llm.idle_unload_min`
(15 min) of inactivity.

### 🔌 Use Addled as a model (OpenAI-compatible endpoint)
Settings → Providers → **Use Addled as a model** serves the local model at a
fixed OpenAI-compatible address, so an agent tool can use Addled as *its* model:

```
Base URL    http://127.0.0.1:8099/v1
API key     sk-addled-…      (generated once, kept across restarts)
Model       addled-local
```

Point GitHub Copilot, Codex, or anything else that accepts a base URL at those
three values. Works with tool calling and streaming, both verified against the
running model.

Why a separate address rather than the model's own port: the runner takes the
next free port when 8090 is busy, so a tool configured once would break on the
next launch; it reports the **GGUF file path** as the model id and ignores the
`model` you send, echoing that path back; it has no auth; and it is stopped
until Addled itself wakes it, so an outside tool's first request would meet a
closed port. The endpoint keeps one stable address, answers with a real model
name, requires the key, and **starts the model on the first request** — so a
tool works without you opening Addled first.

*Honest limits:* it binds **loopback only**, so a tool on another machine cannot
reach it. **Claude Code is not supported** — it speaks the Anthropic protocol
(`/v1/messages`), not the OpenAI one. And the local model is an 8B on one
machine: expect seconds per turn, not the latency of a hosted API.

### 🎯 Task-Aware Model Routing
Addled picks the model by what the task needs instead of sending one model
everywhere. Each request is classified into a role — `chat`, `reasoning`,
`vision`, `long` — using pure heuristics (no extra LLM call, no network, no
latency), and every role resolves to a model:

| Role | Used for | DeepSeek default |
|------|----------|------------------|
| `chat` | quick conversational replies | `deepseek-v4-flash` |
| `reasoning` | analysis, debugging, code changes | `deepseek-v4-pro` |
| `vision` | images and screenshots | the provider's vision model |
| `long` | whole files and documents | the provider default |

`providers.<id>.default_model` stays the single baseline: any role left empty
falls back to it, so a provider with no role map behaves exactly as it did
before routing existed. Override any role in **Settings → Providers → Model
routing**, or force one for a single message with an `@role` prefix (for example
`@reasoning explain this stack trace`). A role pointing at a model the provider
no longer offers is downgraded to the default with a warning instead of failing
the request.

### 📚 Model Catalog (Live, Refreshed Weekly)
Addled asks each provider which models it actually offers — OpenAI-compatible
`GET /models`, Ollama `/api/tags`, Anthropic `/v1/models`, the Gemini SDK — and
caches the answer for a week (`backend/memory/models_catalog.json`). The
discovered models fill the Settings dropdowns and back the routing validation,
with a **Refresh models** button when you want them sooner.

The cached list is **merged** with your configured one, never replaced, so a
model you pinned by hand cannot vanish because an API response omitted it. That
matters in practice: DeepSeek reports only `deepseek-flash` and
`deepseek-v4-pro`, while Addled's chat role uses `deepseek-v4-flash`, which the
API accepts but does not list. A provider that is offline or has no key records
an error and keeps its previous list.

### 📐 Working Guidelines (ponytail + Karpathy)
Addled can follow two external coding rulesets:
[**ponytail**](https://github.com/DietrichGebert/ponytail) (a 7-rung ladder that
stops you writing code that did not need to exist, plus the things it is never
lazy about) and the
[**Karpathy guidelines**](https://github.com/multica-ai/andrej-karpathy-skills)
(think before coding, simplicity first, surgical changes, goal-driven execution).

The text is downloaded from upstream on first use, cached under
`backend/memory/guidelines/`, and refreshed by the same weekly job as the model
catalog — nothing is bundled, so upstream edits arrive on their own and each pack
shows its real source URL and licence. Injection is gated twice: per pack
(`enabled`, level `lite`/`full`/`ultra`/`off`) and by task — the default scope is
**code only**, so ordinary chat is not padded with a coding ruleset. Configure it
in **Settings → Guidelines**, or ask Addled what it is following.

The `ponytail_review` skill reviews a file or diff for over-engineering and
returns one finding per line (`delete:` / `stdlib:` / `native:` / `yagni:` /
`shrink:`) plus a `net: -N lines possible.` score.

### 🧰 MCP Servers
Addled speaks the Model Context Protocol, so third-party tool servers plug in
alongside the built-in skills. Both **stdio** (a local command) and **streamable
HTTP** transports are supported, built on the packages Addled already ships — no
extra dependency, and no rebuild of the bundled Python.

Every tool a server advertises is registered as an ordinary skill in the `MCP`
category (`mcp__<server>__<tool>`), so it shows up in **Settings → Skills**, in
every provider's tool list, and behind the existing per-skill on/off switch. Add
and manage servers in **Settings → MCP**.

MCP servers are third-party programs with real side effects, so a tool from an
untrusted server needs approval: the first call is refused and the model asks you
to confirm, and only a retry with your agreement runs it. Approval is per tool,
not per server, and a *Trusted* switch on the server skips the prompt. A server
that is missing, slow or crashed records an error and returns a readable tool
failure rather than hanging the conversation.

### 🛠️ 141 Built-in Skills (Provider-Agnostic)
All 10 AI providers can invoke any skill — no provider lock-in.

| Category | Skills |
|----------|--------|
| **System** | `run_command`, `get_screen_size`, `screenshot`, `get_clipboard`, `set_clipboard`, `set_volume`, `set_brightness`, `lock_screen` |
| **Files** | `read_file`, `write_file`, `list_dir`, `search_files`, `search_in_files`, `delete_file`, `copy_file`, `move_file`, `create_dir`, `file_info` |
| **Documents** | `word_read`, `word_create`, `word_edit`, `excel_read`, `excel_write`, `excel_sheets`, `pptx_read`, `pptx_create`, `pptx_add_slide`, `pdf_read`, `pdf_create`, `pdf_edit`, `pdf_merge`, `pdf_pages`, `pdf_extract`, `pdf_redact`, `pdf_redact_verify`, `convert_to_pdf`, `convert_from_pdf` |
| **Windows** | `list_windows`, `focus_window`, `resize_window`, `close_window` |
| **Browser** | `browser_navigate`, `browser_extract`, `browser_click`, `browser_type` |
| **Code** | `code_read`, `code_edit` |
| **Calendar & Tasks** | `calendar_add`, `calendar_list`, `calendar_delete`, `task_schedule`, `task_list`, `task_cancel` |
| **Contacts** | `send_message`, `chat_history`, `bot_status`, `email_send`, `email_search`, `email_list`, `transcribe_audio` |
| **Web** | `web_search`, `web_fetch` (auto-falls back to search discovery when sites block bots) |
| **Memory relations** | `memory_get`, `memory_set`, `memory_link`, `memory_unlink`, `memory_related`, `memory_files`, `memory_graph` |
| **Wiki** | `wiki_search`, `wiki_read`, `wiki_write`, `wiki_ingest`, `wiki_links`, `wiki_lint` |
| **Meta** | `forge_skill`, `list_forged`, `find_mcp_server`, `guidelines_status` |

Plus the MCP-bridged skills from any connected server, registered dynamically as `mcp__<server>__<tool>`.

The table above groups them by area rather than listing every one. **Settings → Guide**
lists the live catalogue — all 141, with each skill's own description and whether it is
currently switched on.

#### 📄 Office & PDF Documents
The agent works with the document formats an office day actually involves, reading and
writing them directly rather than shelling out to another application.

- **Word** (`.docx`) — read headings and paragraphs, create a document from Markdown-ish
  text, and edit it in place.
- **Excel** (`.xlsx`/`.xlsm`) — list sheets, read cells and ranges, write values and formulas.
- **PowerPoint** (`.pptx`) — read slide text, create a deck, add slides.
- **PDF** — read the text, create one from content, edit pages (rotate, reorder, keep or
  remove), merge, extract a page range into a new file, and **redact**.
- **Conversion** — `convert_to_pdf` and `convert_from_pdf` move between PDF and the Office
  formats (and plain text), so a `.docx` can be handed over as a PDF without leaving the chat.

Two behaviours are worth knowing:

- **Redaction is real.** `pdf_redact` *removes* the text rather than drawing a black box
  over it, so it cannot be recovered by copying the page or extracting the text — and
  `pdf_redact_verify` re-reads the file afterwards to confirm it is actually gone. It
  writes to a new file by default, because redaction cannot be undone.
- **Nothing is overwritten by accident.** Every operation that writes a file refuses to
  replace an existing one unless you pass `overwrite=true`, and every path must stay
  inside the bound workspace — a path that tries to leave it is refused. A file whose
  extension does not match its contents is reported as such instead of being half-read.

### 🔨 Skill Forge — Self-Extending Agent
When the agent encounters a task it can't handle, it automatically:
1. **Searches** the web for a solution
2. **Installs** required packages (pip)
3. **Generates** a Python skill wrapper (LLM-powered)
4. **Validates** the new skill with a test call
5. **Registers** it for immediate use — persists across restarts

### 🎯 Goals Engine
LLM-based goal decomposition into sequential steps, background execution with checkpointing, retry with fallback, cancellation, JSON persistence. Wired into engine tick for autonomous processing.

### 💻 Code Engine
Workspace folder binding with a real editor (tabs, syntax highlighting, save with Ctrl+S, workspace-wide search), language detection for 32 languages across 42 extensions, unified diff generation/apply/revert, and LLM-powered code editing where you review the diff before it is applied. Every read and write is **confined to the bound folder** — a path that tries to leave it is refused, including for remote callers.

Edits are proposed as **anchored changes** — find the exact existing text, replace it — rather than by rewriting the whole file. Only what changed appears in the diff, untouched lines cannot drift, and there is no file-size ceiling to hit. Matching tolerates indentation and whitespace drift, and a change that would be ambiguous (the anchor appears twice) is **refused rather than guessed**.

- **Plan first**: a multi-file request searches the project, names the files it believes are involved, and only then proposes edits, so a cross-file change is not guessed from the one open file.
- **Verified**: after a plan is applied, Addled runs the project's **own** check — a `verify`/`test` script, a Makefile target, or the runner the folder layout implies — and reports whether it passed. It never invents a command; a project with no check is reported as unverified rather than given a green tick. A project whose runner collects no tests says **"no tests found"** rather than borrowing the failure state, because "the tests did not run" and "the tests failed" are different facts.
- **Undo is git**: if the workspace is a repository, an applied plan leaves a commit describing what changed, so "undo the last change" is an ordinary `git revert` and your own log records what Addled did. It commits on top and never rewrites your history. A folder that is not a repository still gets a `.bak` beside every file it wrote, and the Undo button restores from it — so the same change is reversible either way.
- **Sessions**: work is grouped into named sessions in the sidebar, titled from your first request, pinnable and deletable. A session belongs to the folder it was started in, and binding a workspace reopens the session you were last working in there.
- **The composer takes what you have**: paste a screenshot, drop a PDF or a document, name a file with `@`, or attach the lines you have selected in the editor. An image goes to the vision model, a text file is sent as content, and a document type is read by the backend.
- **Right-click, where you already are**: on a file (open, attach to the prompt, copy path, reveal, add its folder as context), on a selection (explain, refactor, write tests, find the bug, document), and on the tree (new file, refresh, collapse, clear the filter).
- **The Changes view** answers "what did Addled change here?" — the branch, the modified and untracked files, and the uncommitted diff — and can run the project's check on demand.
- **Self-modification**: Addled can change its own source on request, but only as a staged proposal you approve after seeing the diff, with the files that decide what is permitted deliberately excluded. It takes effect after a restart, and every such change is recorded in the journal.

### 🐝 Agent Swarm
**9 agent types** (coder, reviewer, analyst, researcher, writer, planner, devops, qa, general) with role-appropriate prompts and **default skill sets** — the coder desk gets `verify_code` and the Karpathy/Ponytail guidelines, the researcher gets `web_search` + the wiki, the QA desk gets a verifier.

Agents are **saved, not throwaway**. Five ship ready to use (Planner, Researcher, Coder, Reviewer, QA) and each keeps a name, role, **brief** (standing instructions read before every task), **learned rules**, its own skill set and optionally its own model — so a routine desk can run on the local model while the reasoning desk uses a cloud one. They survive a restart, and deleting one is respected rather than re-seeded.

- **Learns from corrections**: send a result back with a correction and it is recorded as a standing rule for that agent ("proposals are always one page") or as a one-off for that task, and applied from the next run.
- **Mid-flight notes**: agents working a step in parallel can leave each other short notes — a warning, a decision — which reach the others while it can still change the outcome.
- Flows support dependencies, parallel peers with a merge step, `until` gates with retry, and pass/fail branching with a jump budget.

### 🌐 Browser Automation
Playwright-powered Chromium browser when installed — navigate, click, type, extract text, screenshot, history navigation. **Install it from Settings → Browser** with a button per backend that says what it will download (~150 MB for Chromium), or let the on-demand policy offer it when a task needs it. **Without Playwright, navigation automatically falls back to a lightweight HTTP fetch** (browser-like headers, HTML→text), and blocked sites are re-discovered through search results. Web search asks Google News RSS and the MediaWiki API first, then Bing News RSS, then scrapes Bing and DuckDuckGo with a relevance gate, and falls back to the browser — so it keeps working on networks where a single engine is unreachable.

### 📅 Calendar, Tasks & Scheduling
Local calendar with Google Calendar OAuth sync — and a **tick-driven task scheduler** (no cron/APScheduler): one-shot and recurring tasks (daily / weekly / monthly) with `notify` (reminder: bubble + spoken TTS) and `chat` (run a prompt later) actions. Calendar events fire reminders before they start; relative dates like "tomorrow 3pm" are normalized on add. Tasks are created from the **Calendar page** (day-click sidebar with edit/pause/delete), from **chat** via the `task_schedule` skill, and from **voice** — including a heuristic parser fallback that works when the LLM provider is down. Memory-maintenance housekeeping jobs run through the same scheduler. IMAP/SMTP email — fetch unread, send, search.

### 🤖 Bot Bridges
Telegram (grammY with 6 commands), WhatsApp (Baileys multi-device with QR pairing), Discord (discord.js with 5 slash commands). All forward messages to Addled's chat. Scheduler notifications broadcast as `bot.notify` events for bridge push integration.

**Text, photos and voice notes.** Send any of them and the reply comes back in the same chat. A photo goes through the visual model and a voice note is transcribed locally by whisper, so the answer is about what is *in* the media rather than an acknowledgement that a file arrived. A caption on a photo is treated as the question. Media is capped (10 MB image, 12 MB audio) with a refusal that names the actual size, and a file that cannot be read says so instead of answering as if nothing was sent.

The bots are **two-way**, not just a way to talk to Addled: the agent can also reach *out*
through them. `send_message` sends to a contact or chat on any connected bridge,
`chat_history` reads back a conversation, and `task_schedule` queues a message to be
delivered later — so "message Sam that I'll be late" and "remind the team tomorrow at 9"
are ordinary requests. Delivery is recorded, so a send that failed is reported as failed
rather than assumed to have arrived.

### 💗 Lifelike companion (mood, timeline, presence)
A persistent **mood & emotion engine** (valence + energy, decays over time) drives the character's visual tint, movement energy and **voice emotion** (Kokoro speech speed follows the mood). **Barge-in**: start talking while Addled speaks and it stops mid-sentence. **Initiative cadence**: return greetings + a daily check-in. An **episodic timeline** journals every day (nightly summaries) and a learned **user model** (preferences, rituals, hours) is injected into every chat — the agent references its own past naturally. **Project awareness**: index your code workspace (semantic search, chat injection). **Predictive proactivity**: weekly rhythms mined from tasks/calendar → gentle suggestions. **Reflection loop**: per-skill telemetry + weekly self-review.

### 📡 Remote access (Tailscale)
Reach Addled from your phone or another machine, in a browser, over Tailscale — status, sign-in and `tailscale serve` sharing are all managed from the **Remote** page, which can also run the official Tailscale installer when you ask it to (nothing installs on its own).

The WebSocket API has **no authentication of its own**, and several of its 203 methods can run shell commands or synthesise input. Rather than spread credential checks across all of them, remote access goes through a separate **gateway** that owns the whole remote surface:

- It serves the login page and the dashboard, and only bridges a WebSocket to `127.0.0.1:9876` **after** validating a session. It binds loopback only; Tailscale terminates TLS and proxies to it, so the browser gets a real `https://<machine>.<tailnet>.ts.net` URL and `wss://` works without Addled handling a certificate.
- The **password is scrypt-hashed** (`settings.json`), sessions are **in-memory only** (a restart logs everyone out), login attempts are **rate-limited per address**, and the session cookie is `HttpOnly` + `SameSite=Lax` (+ `Secure` over HTTPS).
- **The gateway refuses to start without a password**, and sharing refuses too. There is no way to publish an unauthenticated dashboard.
- A logged-in remote session is **not automatically root**: running commands and controlling the mouse/keyboard are blocked for it unless you turn them on in Settings → Remote, on the machine itself. `mcp.add`/`mcp.connect` and `skills.installFrom` stay blocked regardless — they spawn processes or install code, which no browser session needs.
- Secrets are **redacted from `settings.get`** for remote connections, so one request can no longer return every API key and refresh token.
- The WebSocket handshake now checks the **`Origin`** header. Before this, any page in any browser on the machine could open `ws://127.0.0.1:9876` and drive the API — loopback stops the network, not the user's own browser.
- Tailscale **Funnel** (public internet) is off by default and needs an explicit opt-in.

Run it: **Remote** page → generate a password → sign in to Tailscale → share to your tailnet. Defaults the whole feature off.

### 🛡️ Safety
Prompt guard (15 injection + 5 exfiltration patterns), presence guard (meeting/gaming/away auto-sleep), rate limiter, destruction gate with approval workflow (`action.approve` / `action.deny`), **global kill-switch hotkey** (Ctrl+Shift+Alt+K, configurable), **clipboard secret filter** (API keys, tokens, passwords, private keys redacted before the agent sees them), **egress monitor** (logs + scrubs every outbound payload), and **privacy zones** (screen blackout regions + excluded apps actually applied to screenshots).

### 🧠 Memory
**Local semantic memory** — a fully offline, layered memory system: chat history (JSON, cross-session), **semantic long-term recall** (local ONNX MiniLM embeddings + BM25 hybrid search — every turn remembered; relevant past turns auto-injected into new chats, with hash fallback when the model is missing), **rolling compaction** (long conversations auto-summarize their oldest turns so early context survives), **core facts** (durable user facts the agent saves/reads via `memory_get`/`memory_set` tools — shown on the Memory page), **temporal knowledge graph** (subject → relation → object triples with semantic + time-bounded lookup), and **rolling screenshot memory** (last 30 privacy-masked screenshots, 24h auto-purge — enables "what was I doing 20 minutes ago?"). Idle-time maintenance re-embeds legacy rows, dedups and prunes in the background. All controllable in Settings → Memory.

### 🔗 Memory Relations (one graph over everything)
Every memory store above is an independent flat collection, so nothing knew that a fact and a file were about the same thing. **`links.db`** is a single edge table across all of them — `fact`, `triple`, `memory`, `summary`, `journal`, `wiki`, `file` — with a closed relation vocabulary (`relates_to`, `same_as`, `supersedes`, `contradicts`, `derived_from`, `mentions`, `part_of`, `sourced_from`, `documents`, `links_to`). Relations are recorded automatically as memories are written: **file mentions** are scanned out of a fact/triple/memory and linked to the path on disk, and **provenance** links each derived item back to whatever produced it (a triple to its conversation, a summary to its journal day). Related items are injected into chat as a `[Related]` block, which is what lets an answer volunteer *"and the file for that is …"* instead of stopping at the fact. Idle maintenance drops edges whose target no longer exists and flags facts that say the same thing in different words as `same_as`. Tools: `memory_link` `memory_unlink` `memory_related` `memory_files` `memory_graph`. Dashboard: Memory page → Relations (trace any item, list referenced files, prune).

### 📖 Wiki (LLM Wiki, local-first)
A **Karpathy-pattern wiki** instead of another chat transcript: markdown pages under `backend/memory/wiki/pages/` with YAML frontmatter, maintained *incrementally* from your own sources. Ingesting a document first pulls the pages that already look related and asks the model for **merged page bodies**, so a second document about the same topic updates the page and appends its citation rather than creating a rival page — and the citation is preserved on every later rewrite. `[[wiki-links]]` between pages are mirrored into the relation graph (`links_to`), and non-URL sources become `sourced_from` edges to the real files, so the wiki also answers "which file said this?". Relevant pages are injected into chat with their citations as a `[Wiki]` block, since pre-distilled pages are better evidence than a guess. **`auto_ingest` is off by default** — Addled never reads your files into the wiki unless you ask. Tools: `wiki_search` `wiki_read` `wiki_write` `wiki_ingest` `wiki_links` `wiki_lint`. Dashboard: Wiki page (list, search, edit, ingest, lint).

### 🎤 Voice & Perception
**Voice input**: wake word → command → chat → spoken reply. **Silero VAD** segments real speech for turn detection (RMS fallback). STT: **faster-whisper** (local, offline; tiny/small selectable) with **SenseVoice** auto-activating when funasr is available. **TTS**: **Kokoro** neural voices fully offline (local 82M ONNX) with **edge-tts** online fallback — engine selectable in Settings → Voice, with an **Auto TTS toggle** so dashboard chat and character prompts speak replies out loud. **Voice pickers** list what is actually available rather than asking you to type a voice name: the installed Kokoro pack is read straight out of `voices-v1.0.bin` (54 voices, offline, without loading the model) and the Edge voices come from the service, cached for a day with a curated offline fallback. Both are grouped by language and follow the **Language** setting, and where an engine simply cannot speak the chosen language — Kokoro has no Indonesian voice — Addled says so and routes to one that can. Speech input is local & offline; Edge TTS is the only network-dependent part. **3-tier observer**: light hash (5s) / window-title classification (~15s) / deep Florence-2 vision (5 min, local), with a **Deep vision toggle + interval** in Settings → Observation (turn off to keep the vision model out of RAM). **Live screen awareness**: chat automatically receives the current activity context + latest vision description; asking "what do you see?" triggers a fresh capture. **Proactive insights**: the agent suggests help when you've been stuck on a task — delivered as chat messages + character bubbles (optionally spoken).

### 🧩 Skills & Market
141 built-in skills with a **Skills dashboard page** — every skill can be toggled on/off, market/forged skills deleted. **SKILL.md market support**: install skills from Claude Code/Copilot/opencode ecosystems via URL or GitHub repo, and **market search** (GitHub `claude-skills` topic, ranked with the local embedder) — when the agent calls a missing skill, Addled auto-finds, installs and runs the best market match, falling back to LLM code-generation (Skill Forge).

Two things worth calling out:

- **Destructive skills ask first.** `requires_approval` is enforced at the single registry seam, so `delete_file`, `write_file`, the desktop-control skills, `self_apply` and market script skills all stop for approval on the turn that asked. Previously the flag was declared and displayed but never read, and the file skills bypassed the destruction gate entirely.
- **Interactive sessions.** For work a one-shot command cannot do — a `cd` that sticks, an env var the next command reads, a REPL, an open ssh — `session_open` starts a real shell that keeps its state, and `session_send` types into it. Market script arguments are passed as an **argv list, never folded into a shell string**, so an argument containing PowerShell metacharacters is data rather than code.
- **Standing permission, granted from whichever surface asked.** An approval prompt carries its own **Always allow** button, so a skill or tool you trust stops asking from that point on without you going to Settings. Grants are per *kind* (skill vs tool), so allowing `read_file` never silently allows a forged skill by the same name. **Destructive names are refused inside the grant call itself** — not by a caller that remembers to check — so `delete_file` cannot be made standing no matter which surface offers the button. Installed market skills are bound to a **digest fingerprint**: if the script behind a granted skill later changes, the grant stops matching and Addled asks again.
- **The answer belongs to the chat that asked.** Telegram, Discord, WhatsApp and the dashboard all share one approval queue, so a bare "yes" is ambiguous. Every queued approval records its **origin** (source + conversation), and an answer must name a request raised *in that same chat* — otherwise a reply in one bot cannot release an action requested by another. Resolving an approval clears its origin, so the map tracks only what is genuinely outstanding.
- **It can ask you a question, and you can answer it wherever you are.** `ask_user` is for the case a turn genuinely cannot resolve — which of two same-named files, which account, which of three readings of a short request. It is deliberately narrow and its description says when *not* to use it: a model that finds asking cheap stops making reasonable assumptions. The question appears as a card with buttons for its choices (or a text box when there is nothing to choose between), on the chat page, the Code page, **the floating character**, and **all three bots**. Answering **resumes the work** — the turn that asked has ended, and the answer arrives as the next turn carrying what you said.
  - **The turn does not block.** A question ends it, exactly like an approval, because holding a turn open is the behaviour that was already removed once: a local model routinely outlived the wait and the app looked hung.
  - **Only where someone can answer.** Scheduled tasks, swarm desks and voice turns have nobody watching, so they are refused and told to decide, state their assumption and carry on. The rule is derived from the source list, so a new source cannot land in a gap.
  - **A question expires** (10 minutes by default) and the next turn is *told* it went unanswered rather than silently forgetting — otherwise the model just asks again. A repeated question is refused too, including one reworded, because a looping model otherwise replaces your answer with a second card.
- **Swarm agents keep their own notebooks.** Each agent in a swarm writes a per-agent **notebook** after every step rather than at the end of a flow, because a run killed mid-way is exactly the case it exists for. Entries are capped; the overflow is **folded into a standing summary** rather than dropped, so detail degrades from verbatim to compressed instead of vanishing. Agents hand each other a **digest**, not the raw log, and can search their own history with `swarm.notebookSearch` — a long-running role keeps its memory across restarts without flooding its peers.
- **Method skills, not just tools.** The desks that write code and check it carry a *method*, not only a toolset: the coder desk gets test-driven development and systematic debugging, the reviewer gets the code-review pair, and QA gets verification-before-completion plus the webapp-testing toolkit. These were installed as market skills and mapped onto each role, so an agent is told **how** to work rather than only what it may call.

### 📧 Email & transcription
Both are things the agent does itself, not just things the dashboard can do.

- **Email** — `email_list`, `email_search` and `email_send`. Reading is ungated; **sending requires approval**, because mail cannot be recalled. A mailbox that is not configured says so rather than returning an empty list, so "no mail" is never confused with "not set up" — an agent reporting a false all-clear is worse than one reporting an error.
- **Transcription** — `transcribe_audio` turns a recording into text with the **local** whisper model (nothing uploaded), so a meeting or voice note can be summarised and its action items pulled out. The model is loaded once and shared with the live voice listener rather than duplicated in RAM.

### 🎯 Goals, with the plan made visible
Clicking a goal shows **which agent ran each step**, that agent's live state, the task it is on, and a **progress bar** derived from the plan — not just a status word. The plan, the findings each desk produced, and a round counter appear together, and a **re-plan replaces the step list rather than merging into it**, so steps from an abandoned plan cannot linger looking like work still to do.

### 🌐 Browser & Desktop
Four browser backends with **task-based routing**: your running Chrome/Edge via CDP (read-only by default, opt-in), Playwright's own Chromium, the **browser-use** AI framework for open-ended tasks (used only when installed AND an LLM is available), and an always-on HTTP fallback. Backends are installed from the Settings page with a manual button (or on demand, `ask` / `off` / `auto` policy with a dashboard approval banner). **Desktop control** (mouse/keyboard) is first-class but off by default: session grants via dashboard prompt, screen-bounds checks, typing caps, hotkey whitelist, pyautogui FAILSAFE, and egress logging.

### 📦 Installer & Auto-Update
Windows NSIS + portable installer via electron-builder. **Weekly auto-update** against the latest GitHub release (version discovery via the GitHub API; delta updates via latest.yml + blockmap). If the repo is private, the check degrades gracefully and resumes automatically once it's public. Manual "Check for Updates" in the tray. First-run PyQt6 onboarding wizard (8 steps: provider, local model, name, workspace folder, voice, wake word, autostart).
**An upgrade keeps your setup.** Settings, memory, skills, tools, MCP servers and the swarm roster all live beside the install, and the installer deliberately ships **no copy** of any of them — an upgrade adds and replaces program files and has nothing to overwrite your data with. This is now enforced rather than assumed: a check reads the paths the backend *writes* out of the source and fails if any of them could be packaged, so a future store cannot start shipping by being added to one list and not another. Settings are also saved **atomically** (write to a temp file, then swap it in), so an upgrade landing mid-write cannot leave a truncated config behind.
---

## WebSocket API (203 handlers)

### Core
`chat.send` `action.execute` `action.approve` `action.deny` `action.pending` `approvals.list` `approvals.alwaysAllow` `approvals.revoke` `voice.speak` `voice.voices` `character.setState` `observer.status` `system.status` `system.getProviders` `settings.get` `settings.set` `guide.status` `models.routes` `models.catalog` `models.refresh` `guidelines.state` `guidelines.refresh` `mcp.list` `mcp.add` `mcp.update` `mcp.remove` `mcp.connect` `mcp.disconnect` `mcp.reload` `mcp.tools` `localLlm.status` `localLlm.installApprove` `localLlm.installDecline` `localLlm.start` `localLlm.stop` `localLlm.remove` `localLlm.installHfDeps` `hf.unload` `modelApi.status` `modelApi.start` `modelApi.stop`

### Goals
`goal.create` `goal.list` `goal.start` `goal.cancel`

### Code
`code.bind` `code.read` `code.edit` `code.apply` `code.applyPlan` `code.write` `code.grep` `code.plan` `code.verify` `code.git.status` `code.git.diff` `code.git.revert`

### Excel
`excel.read` `excel.write`

### Swarm
`swarm.spawn` `swarm.list` `swarm.run` `swarm.stop` `swarm.flow` `swarm.memory` `swarm.roster` `swarm.define` `swarm.forget` `swarm.restore` `swarm.revise` `swarm.notes` `swarm.notebook` `swarm.notebookSearch`

### Calendar & Email
`calendar.add` `calendar.list` `calendar.delete` `calendar.update` `tasks.schedule` `tasks.list` `tasks.update` `tasks.pause` `tasks.resume` `tasks.cancel` `tasks.month` `email.fetch` `email.send` `email.search`

### Browser
`browser.navigate` `browser.go_back` `browser.go_forward` `browser.click` `browser.type` `browser.screenshot` `browser.extract` `browser.close` `browser.status` `browser.task` `browser.installApprove` `browser.installStatus` `browser.installNow`

### System
`system.status` `system.setAutostart`

### Privacy & Monitoring
`privacy.setZones` `privacy.list` `privacy.excludeApp` `privacy.removeApp` `egress.list` `snapshot.list`

### Memory
`chat.history` `memory.list` `memory.deleteSummary` `memory.deleteMemory` `memory.clear` `memory.getFacts` `memory.setFact` `memory.deleteFact` `memory.listTriples` `memory.deleteTriple` `memory.links` `memory.addLink` `memory.deleteLink` `memory.related` `memory.files` `memory.pruneLinks`

### Wiki
`wiki.list` `wiki.get` `wiki.save` `wiki.delete` `wiki.search` `wiki.ingest` `wiki.lint` `wiki.refreshLinks`

### Skill Forge`forge.create` `forge.list`

### Skills
`skills.list` `skills.setState` `skills.delete` `skills.searchMarket` `skills.installFrom`

### Desktop control
`desktop.status` `desktop.grant` `desktop.revoke`

### Server pushes
`state.changed` (character state) · `observer.insight` (proactive suggestions) · `chat.push` (character/voice-initiated messages) · `kill.activated`

---

## Development

```bash
# Terminal 1 — Backend (Python + WS + Character)
cd backend && python main.py
# → Character appears on desktop, WS on ws://127.0.0.1:9876

# Terminal 2 — Dashboard (Next.js)
cd dashboard && npm run dev
# → Dashboard on http://localhost:3000

# Terminal 3 — Electron shell (optional)
npx electron .
# → Desktop window loading dashboard
```

### Project Structure
```
Addled/
├── backend/
│   ├── main.py              # Entry point + onboarding + single-instance lock + kill switch + voice
│   ├── config.py            # Portable JSON settings
│   ├── engine.py            # Async event loop + observer + insight pushes + goal tick
│   ├── ws_server.py         # 203 JSON-RPC 2.0 handlers + server pushes
│   ├── providers/           # 10 AI providers (base + registry + selector + router)
│   │                        #   + live model catalog + local Florence-2 vision
│   ├── mcp_client/          # MCP client (stdio + streamable HTTP) → tools as skills
│   ├── guidelines/          # External rulesets (ponytail, Karpathy): fetch, cache, inject
│   ├── local_llm/           # llamafile runtime manager (ask-first download, start/stop, idle unload)
│   ├── local_models/        # Resumable model downloads + storage paths
│   ├── skills/              # Skill registry + tool loop + forge
│   ├── character/           # States, shapes, movement, animation, avatar, particles
│   ├── actions/             # 39 actions (input, windows, files, system, terminal, excel)
│   ├── safety/              # Prompt guard, presence guard, destruction gate, kill switch,
│   │                       #   clipboard filter, egress monitor, privacy zones
│   ├── perception/          # 3-tier observer (light/medium/deep) + snapshot hook
│   ├── voice/               # Edge TTS + wake-word STT (faster-whisper)
│   ├── memory/              # Chat history, semantic recall (ONNX MiniLM + BM25), compaction,
│   │                       #   facts, knowledge graph, snapshot store, maintenance
│   ├── browser/             # Playwright browser automation
│   ├── goals/               # Planner, executor, store
│   ├── codemode/            # Diff engine, anchored edits, git safety net,
│   │                        #   verification, self-modification
│   ├── swarm/               # Agent orchestrator + the saved roster
│   ├── integrations/        # Calendar (Google OAuth), Email (IMAP/SMTP)
│   ├── onboarding/          # PyQt6 setup wizard (8 pages)
│   └── cognition/           # Decision engine
├── dashboard/
│   └── src/app/
│       ├── chat/page.tsx    # Chat with streaming + markdown
│       ├── goals/page.tsx   # Goal creation + WS-synced progress
│       ├── code/page.tsx    # Workspace binding + file viewer + edit
│       ├── swarm/page.tsx   # Agent cards + WS-synced spawn/stop
│       ├── browser/page.tsx # URL bar + screenshot + log
│       ├── calendar/page.tsx# Month grid + WS-synced events
│       ├── bots/page.tsx    # Bot connection UI
│       └── settings/page.tsx# provider/voice/memory/wiki config + live WS save
├── electron/
│   ├── main.js              # BrowserWindow, tray, Python/Next.js spawn
│   ├── updater.js           # GitHub Releases auto-updater
│   └── preload.js           # Context bridge
├── bots/
│   ├── telegram-bot.js      # GrammY with 6 commands
│   ├── whatsapp-bot.js      # Baileys multi-device
│   └── discord-bot.js       # discord.js 5 slash commands
├── scripts/
│   ├── verify_python.py     # Python environment check
│   ├── check_voice.py       # voice catalogue + voice selection
│   ├── check_tools.py       # tool catalogue, parser and provider errors
│   ├── check_links.py       # memory relation graph
│   ├── check_wiki.py        # wiki pages, ingest and skills
│   └── check_wiring.py      # model routing wiring
├── launch.bat               # One-click dev launcher
└── electron-builder.yml     # NSIS + portable packaging config
```

## Build (Windows Installer)

One-click (installs deps, builds dashboard, packages everything):
```bash
build.bat
```

Manual equivalent:
```bash
pip install -r requirements.txt
cd dashboard && npm install && npm run build && cd ..
npm run build:win
# → dist/Addled-<version>-x64.exe (NSIS installer)
# → dist/Addled-<version>-portable.exe (portable)
```

### Publish a release
```bash
# after build.bat:
git tag vX.Y.Z && git push origin vX.Y.Z
gh release create vX.Y.Z --title "Addled X.Y.Z" --notes-file release-notes.md
gh release upload vX.Y.Z dist\Addled-X.Y.Z-x64.exe.blockmap dist\latest.yml --clobber
gh release upload vX.Y.Z dist\Addled-X.Y.Z-x64.exe dist\Addled-X.Y.Z-portable.exe --clobber
gh release edit vX.Y.Z --draft=false
```
> Upload small assets first, then the two large exes (more reliable).
> The auto-updater checks GitHub Releases weekly, with a daily gate re-check and a
> manual check from the tray.

> **Self-contained**: the installer bundles Python 3.14.7 + all core
> dependencies — no Python install needed on the target PC.
> Optional extras on the target PC: local vision (`torch` + `transformers`)
> and browser automation (Playwright + Chromium, installable from
> Settings → Browser).

---

## License

MIT

