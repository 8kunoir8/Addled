# Addled

> An AI desktop companion with a floating animated character, full web dashboard,  
> 30 built-in skills, self-extending capability forge, and 7 AI provider backends.  
> Built with Python 3.14 + PyQt6 + Electron 28 + Next.js 16.
>
> **Repository**: [github.com/8kunoir8/Addled](https://github.com/8kunoir8/Addled)

## Quick Start (Development)

```bash
# 1. Install Python dependencies
cd backend
pip install -r requirements.txt

# 2. Start the backend (Python + WebSocket server + character widget)
python main.py

# 3. In another terminal, start the dashboard
cd dashboard
npm install
npm run dev

# 4. In a third terminal, start the Electron shell (optional)
cd ..
npx electron .
```

Or use the one-click launcher:
```bash
scripts\dev.bat
```

---

## Architecture

```
┌─────────────────────────────────────────┐
│           Electron Shell                │
│  ┌───────────────┐  ┌────────────────┐  │
│  │Next.js Dashboard│  │ Bot Bridges    │  │
│  │10 pages        │  │Telegram/WA/    │  │
│  │Chat/Goals/Code │  │ Discord        │  │
│  │Swarm/Browser/  │  │                │  │
│  │Calendar/Bots/  │  │                │  │
│  │Settings        │  │                │  │
│  └───────┬───────┘  └───────┬────────┘  │
│          │ WebSocket         │ HTTP      │
├──────────┼───────────────────┼───────────┤
│          ▼                   ▼           │
│  ┌────────────────────────────────────┐  │
│  │      Python Backend Service        │  │
│  │  Engine • 36 WS Handlers           │  │
│  │  30 Skills • Skill Forge           │  │
│  │  7 AI Providers • Goals • Code     │  │
│  │  Swarm • Calendar • Email • Browser│  │
│  │  Character • Safety • Memory •     │  │
│  │  Observer • Voice • Onboarding     │  │
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
| Desktop Shell | Electron 28, system tray, auto-updater | ✅ Built |
| Bot Bridges | Node.js (grammY, Baileys, discord.js) | ✅ Built |
| Communication | WebSocket JSON-RPC 2.0 (36 handlers) | ✅ Built |

---

## Features

### 🎭 Floating Character
12 animation states (idle, listening, observing, thinking, has_suggestion, acting, speaking, sleeping, blocked, error, working, dreaming), 6 vector shapes (triangle, circle, diamond, hexagon, star, square), cursor gaze tracking, breathing/stretch animations, glow effects, mood tinting, progress ring, 7 particle types (zzz, sparkle, gear, glow_burst, trail, lightbulb).

### 💬 AI Chat
Full chat UI with streaming responses, markdown rendering, conversation history. Works with any configured AI provider. Supports skill-based tool calling across all providers.

### 🔌 7 AI Providers
| Provider | Type | Vision | Streaming |
|----------|------|--------|-----------|
| DeepSeek | Cloud | — | ✅ |
| OpenAI (GPT-4o) | Cloud | ✅ | ✅ |
| Claude | Cloud | ✅ | ✅ |
| Gemini | Cloud | ✅ | ✅ |
| GitHub Copilot | Cloud | — | ✅ |
| Ollama | Local | — | ✅ |
| LM Studio | Local | — | ✅ |

### 🛠️ 30 Built-in Skills (Provider-Agnostic)
All 7 AI providers can invoke any skill — no provider lock-in.

| Category | Skills |
|----------|--------|
| **System** | `run_command`, `get_screen_size`, `screenshot`, `get_clipboard`, `set_clipboard`, `set_volume`, `set_brightness`, `lock_screen` |
| **Files** | `read_file`, `write_file`, `list_dir`, `search_files`, `delete_file`, `create_dir`, `file_info` |
| **Windows** | `list_windows`, `focus_window`, `resize_window`, `close_window` |
| **Browser** | `browser_navigate`, `browser_extract`, `browser_click`, `browser_type` |
| **Code** | `code_read`, `code_edit` |
| **Calendar** | `calendar_add`, `calendar_list` |
| **Web** | `web_search` |
| **Meta** | `forge_skill`, `list_forged` |

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
Workspace folder binding with file tree, language detection (40+ languages), unified diff generation/apply/revert, LLM-powered code editing with diff preview.

### 🐝 Agent Swarm
7 agent types (coder, writer, analyst, planner, researcher, devops, general) with unique system prompts and tool access. Parallel execution via asyncio. Dashboard spawn/stop/task controls.

### 🌐 Browser Automation
Playwright-powered Chromium browser — navigate, click, type, extract text, screenshot, history navigation. 8 WS handlers for full web automation.

### 📅 Calendar & Email
Local calendar with Google Calendar OAuth sync. IMAP/SMTP email — fetch unread, send, search. Dashboard integration for both.

### 🤖 Bot Bridges
Telegram (grammY with 6 commands), WhatsApp (Baileys multi-device with QR pairing), Discord (discord.js with 5 slash commands). All forward messages to Addled's chat.

### 🛡️ Safety
Prompt guard (14 injection + 5 exfiltration patterns), presence guard (meeting/gaming/away detection), rate limiter, destruction gate with approval workflow, privacy guard, clipboard filter.

### 🧠 Memory
Chat history (JSON), session context tracking, vector store (SQLite + numpy cosine similarity) for semantic search.

### 🎤 Voice & Perception
Edge TTS with 5 voice options, 3-tier observer (light pHash / medium window classification / deep vision model analysis). Wired into engine tick for autonomous context awareness.

### 📦 Installer & Auto-Update
Windows NSIS + portable installer via electron-builder. GitHub Releases auto-updater with 4-hour check interval, download progress, restart prompt. First-run PyQt6 onboarding wizard (6 steps).

---

## WebSocket API (36 handlers)

### Core
`chat.send` `action.execute` `voice.speak` `character.setState` `observer.status` `system.status` `system.getProviders` `settings.get` `settings.set`

### Goals
`goal.create` `goal.list` `goal.start` `goal.cancel`

### Code
`code.bind` `code.read` `code.edit`

### Swarm
`swarm.spawn` `swarm.list` `swarm.run` `swarm.stop`

### Calendar & Email
`calendar.add` `calendar.list` `calendar.delete` `email.fetch` `email.send` `email.search`

### Browser
`browser.navigate` `browser.go_back` `browser.go_forward` `browser.click` `browser.type` `browser.screenshot` `browser.extract` `browser.close`

### Skill Forge
`forge.create` `forge.list`

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
│   ├── main.py              # Entry point + onboarding + single-instance lock
│   ├── config.py            # Portable JSON settings
│   ├── engine.py            # Async event loop + observer + goal tick
│   ├── ws_server.py         # 36 JSON-RPC 2.0 handlers
│   ├── providers/           # 7 AI providers (base + registry)
│   ├── skills/              # Skill registry + tool loop + forge
│   ├── character/           # States, shapes, movement, animation, avatar, particles
│   ├── actions/             # 55+ action executor (input, windows, files, system, terminal)
│   ├── safety/              # Prompt guard, presence guard, rate limiter, privacy
│   ├── perception/          # 3-tier observer (light/medium/deep)
│   ├── voice/               # Edge TTS
│   ├── memory/              # Chat history, session context, vector store, forged skills
│   ├── browser/             # Playwright browser automation
│   ├── goals/               # Planner, executor, store
│   ├── code/                # Diff engine, language detection
│   ├── swarm/               # Agent orchestrator
│   ├── integrations/        # Calendar (Google OAuth), Email (IMAP/SMTP)
│   ├── onboarding/          # PyQt6 setup wizard (6 pages)
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
│       └── settings/page.tsx# 9-section config + live WS save
├── electron/
│   ├── main.js              # BrowserWindow, tray, Python/Next.js spawn
│   ├── updater.js           # GitHub Releases auto-updater
│   └── preload.js           # Context bridge
├── bots/
│   ├── telegram-bot.js      # GrammY with 6 commands
│   ├── whatsapp-bot.js      # Baileys multi-device
│   └── discord-bot.js       # discord.js 5 slash commands
├── scripts/
│   ├── dev.bat              # 3-terminal launcher
│   └── build.bat            # Full build pipeline → Windows installer
└── electron-builder.yml     # NSIS + portable packaging config
```

## Build (Windows Installer)

```bash
scripts\build.bat
# → dist/Addled Setup x.y.z.exe (NSIS installer)
# → dist/Addled x.y.z-portable.exe (portable)
```

---

## License

MIT

