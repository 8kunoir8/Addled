# Addled

**An AI companion that lives on your desktop.**

Not a chat window you visit — a small animated character that sits on your screen,
watches what you're working on, remembers what you told it last week, and can
actually *do* things: run commands, edit your code, read your documents, control
your browser, and reach you on your phone.

Runs **entirely offline** by default. No account, no API key, no data leaving your
machine unless you choose a cloud provider.

> **Repository**: [github.com/8kunoir8/Addled](https://github.com/8kunoir8/Addled)
> **Download**: [latest release](https://github.com/8kunoir8/Addled/releases/latest)

---

## Why it's different

| | |
|---|---|
| 🎭 **It has a face** | A floating character with 12 animation states, mood tinting, particles and speech bubbles. Click it to chat; it answers on screen and out loud. |
| 🔒 **Offline first** | Ships with a local 8B model and local speech/vision. Works with no internet and no account. Add cloud providers only if you want them. |
| 🧠 **It remembers** | Long-term semantic memory, a knowledge graph, a personal wiki, and a learned model of you — injected into every conversation. |
| 🔨 **It extends itself** | Ask for something it can't do and it searches, installs, writes the skill, tests it, and keeps it. |
| 🐝 **It has a team** | Save named agents with their own roles, skills and models. Name one in chat and it takes the job. |
| 🛡️ **It asks first** | Anything destructive stops for your approval. Nothing installs or sends without you. |

---

## Get started

**Download the installer** from the [latest release](https://github.com/8kunoir8/Addled/releases/latest).
No Python required — the installer bundles its own runtime.

1. Run `Addled-x.y.z-x64.exe` and follow the 8-step setup wizard.
2. Addled asks before downloading the local model (~5 GB, resumable). Decline and
   it falls back to OpenRouter — paste a free key instead.
3. Click the character and start talking.

Optional extras install on demand from Settings: **local vision** (~400 MB) and
**browser automation** (~150 MB). Neither is bundled, so you only pay for what
you use.

---

## Run from source

```bash
# 1. Python dependencies
pip install -r requirements.txt

# 2. Backend + WebSocket server + character
cd backend
python main.py

# 3. Dashboard (in a second terminal) — must be up before Electron
cd dashboard
npm install
npm run dev

# 4. Electron shell (in a third terminal, optional)
npx electron .
```

Or use the one-click launcher: `launch.bat`

> **The dashboard must be running before Electron.** In development the shell loads
> `http://localhost:3000` and shows a hidden window until it answers, so a missing
> dashboard looks like a crash.

To build the self-contained installer: `build.bat`

---

## Features

### 🎭 A character, not a chat box
Twelve animation states wired to real activity — thinking while the model works,
speaking during TTS, sleeping when you're away, erroring on failures. Six vector
shapes, cursor tracking, breathing, glow and particles. **Replies appear in a
bubble you can click**, and you can answer permission prompts and questions
directly on it rather than hunting for the dashboard.

Swap it for any animated pet: drop a GIF or a ZIP of per-state GIFs into
Settings → Character and all twelve states keep working on top.

### 💬 Chat that does things
Streaming replies, markdown, history — plus **attachments**: paste a screenshot,
drop a PDF, or attach a selection. Images are described by the vision model, text
is inlined, documents are read. Proactive suggestions arrive as 💡 messages when
you look stuck.

### 🐝 A team of agents you can name
Nine agent types (coder, reviewer, analyst, researcher, writer, planner, devops,
QA, general), five ready out of the box. Each keeps a **name, role, brief, learned
rules, skill set and optional model** — so a routine desk runs on the local model
while the reasoning desk uses a cloud one.

- **Name one in chat** — *"ask Scout to review this"* — and it takes the job.
- **Create and rename them by talking**: *"make me an agent called Scout"*.
- **They learn from corrections**: send work back with a fix and it's remembered.
- Flows support dependencies, parallel work, retries and branching.

### 💻 Code engine
Bind a folder and get a real editor (tabs, syntax highlighting, search), with all
reads and writes **confined to that folder**.

- **Plan first** — a multi-file request searches the project and names the files
  before proposing anything.
- **Verified** — after applying, it runs *your* project's own check and reports
  honestly; a project with no tests says so rather than showing a green tick.
- **Anchored edits** — find the exact text, replace it. Untouched lines can't
  drift, and an ambiguous match is refused rather than guessed.
- **Undo is git** — a repo gets a commit describing the change; a plain folder
  gets `.bak` files.
- **Self-modification** — Addled can edit its own source, but only as a diff you
  approve, with the safety files excluded.

### 🧠 Memory that actually accumulates
Local semantic recall (offline embeddings + BM25), rolling summarization, core
facts, a **temporal knowledge graph**, and a **memory relations graph** linking
facts, files, wiki pages and journal entries — which is what lets it volunteer
*"and the file for that is…"*. Nothing is uploaded.

### 📖 A personal wiki
A Karpathy-style markdown wiki maintained from your own sources. Ingest a second
document about the same topic and it **updates the existing page** rather than
creating a rival one — and keeps the citation. Relevant pages are injected into
chat as evidence.

### 📄 Documents, for real
Read and write **Word, Excel, PowerPoint and PDF** directly. Merge, split,
convert between formats, and **redact properly** — `pdf_redact` removes the text
rather than covering it, then re-reads the file to confirm it's gone.

### 🌐 Browser & desktop
Four browser backends with automatic routing: your own Chrome over CDP, Playwright
Chromium, the browser-use framework for open-ended tasks, and an always-available
HTTP fallback so navigation never simply fails. **Desktop control** (mouse and
keyboard) is available but off by default, with session grants and a kill switch.

### 🎤 Voice, both ways
Wake word → command → spoken reply. **Silero VAD** for real turn detection,
**faster-whisper** transcribing locally, **Kokoro** neural voices fully offline
with an edge-tts fallback. Interrupt it mid-sentence and it stops. Voice pickers
list what's actually installed and follow your language setting.

### 📡 Reach it from anywhere
Sign in to Tailscale and reach Addled from your phone or another machine in a
browser. Remote access goes through a **separate authenticated gateway** — scrypt
password, in-memory sessions, rate limiting, `Origin` checks — and a logged-in
session is **not** automatically root: shell and input control stay off unless you
enable them on the machine itself.

### 🔌 Ten providers, local by default
**Addled Local** (llamafile + Qwen3-8B, offline) · OpenRouter · Hugging Face
(Local) · DeepSeek · OpenAI · Claude · Gemini · GitHub Copilot · Ollama ·
LM Studio.

Requests are **routed by what they need** — chat, reasoning, vision, long-form —
so a quick question doesn't go to an expensive model. The local model starts only
when used and **frees its RAM while you game**.

**Use Addled as a model for other tools.** A stable OpenAI-compatible endpoint at
`http://127.0.0.1:8099/v1` lets Copilot, Codex or anything else point at your
local model.

### 🛡️ Safety you can see
Prompt-injection guard, egress scrubbing, clipboard secret filter, privacy zones
that black out screen regions, privacy-excluded apps, a **global kill switch**
(`Ctrl+Shift+Alt+K`), and an approval workflow for anything destructive.
**Standing permission is per-kind and revocable** — and destructive names like
`delete_file` can never be made standing, no matter which surface offers the button.

### 🧰 Skills, and a market for more
**109 built-in skills** across system, files, documents, windows, browser, code,
calendar, tasks, email, web, memory, wiki and more. Toggle any of them; delete
ones you installed. It speaks **MCP**, so third-party tool servers plug straight in
as ordinary skills.

Missing something? Ask. Addled searches for a skill, installs it, or **forges one**
— generating, testing and registering it on the spot.

**Build your own tools.** Settings → CLI Tools writes a real command-line
program for a capability Addled does not have. You read the code before it is
saved, edit it if you want, and test it with one button — then it is a skill
like any other, callable from chat, the Code page and your swarm agents. Tools
you build are preferred over anything downloaded, and they live in
`%LOCALAPPDATA%\Addled\cli_tools\` so they survive upgrades.

### 🤖 Bots
Telegram, WhatsApp and Discord — all two-way. Send text, photos or voice notes and
the reply comes back in the same chat. The agent can also reach *out*:
`send_message` to a contact, `chat_history` to read a thread, and scheduled
messages for later.

### 📅 Calendar, tasks and email
Local calendar with Google sync, a tick-driven scheduler for one-shot and
recurring reminders (spoken or as a prompt for later), and IMAP/SMTP email.
Reading is open; **sending asks first**, because mail can't be recalled.

### 💗 It feels alive
A mood engine (valence + energy) that decays over time and tints the character,
its movement and even its speech speed. Nightly journal summaries, weekly rhythm
suggestions, a reflection loop, and return greetings when you come back.

---

## Architecture

```
┌──────────────────────────────────────────────┐
│              Electron Shell                  │
│  Next.js dashboard (13 pages)  · Bot bridges │
└───────────────┬──────────────────┬───────────┘
                │ WebSocket        │ HTTP
┌───────────────▼──────────────────▼───────────┐
│           Python Backend Service             │
│  Engine · 206 WS handlers · 109 built-in     │
│  skills · 10 providers · Goals · Code ·      │
│  Swarm · Memory · Wiki · Voice · Observer ·  │
│  Safety · Bots                               │
└───────────────┬──────────────────────────────┘
                │ Qt signals
┌───────────────▼──────────────────────────────┐
│     Floating character (PyQt6, 30 fps)       │
└──────────────────────────────────────────────┘
```

| Layer | Stack |
|---|---|
| Backend | Python 3.14, asyncio, PyQt6, websockets |
| Character | PyQt6 QWidget, QPainter, 30 fps |
| Dashboard | Next.js 16, TypeScript, Tailwind |
| Shell | Electron 28, tray, auto-updater |
| Bots | Node.js (grammY, Baileys, discord.js) |
| Protocol | WebSocket JSON-RPC 2.0 |

---

## Your data stays yours

Settings, memory, skills, MCP servers and the swarm roster live beside the install,
and the installer **ships no copy of any of them** — so an upgrade adds program
files and has nothing to overwrite your data with. This is enforced by a check
that reads the paths the backend writes and fails if any could be packaged.

Settings are saved atomically, so an upgrade mid-write can't leave a truncated
config behind.

---

## Development

```bash
python -m scripts.check_all            # the full suite (75 checks)
python scripts/verify_python.py        # syntax check
cd dashboard && npm run build          # build the dashboard
```

`build.bat` produces the self-contained installer: Python 3.14.7 and all core
dependencies bundled, nothing to install on the target machine.

---

## License

See [LICENSE](LICENSE).
