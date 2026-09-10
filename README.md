# Addled

> An AI desktop companion with a floating animated character, full web dashboard,  
> voice interaction (wake word + speech), 30 built-in skills, self-extending  
> capability forge, 7 AI provider backends, live screen awareness, long-term  
> memory, and a full safety suite.  
> Built with Python 3.14 + PyQt6 + Electron 28 + Next.js 16.
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

Or use the one-click launcher:
```bash
launch.bat
```

> **Self-contained installer**: `build.bat` bundles Python 3.14.7 + all core
> dependencies — no Python install needed on the target PC.
> Optional extras: local vision (`torch` + `transformers`, ~400 MB) and
> browser automation (`pip install playwright && playwright install chromium`).
> The voice model (faster-whisper tiny, ~75 MB) downloads on first use.

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
| Desktop Shell | Electron 28, system tray, auto-updater, backend auto-respawn | ✅ Built |
| Bot Bridges | Node.js (grammY, Baileys, discord.js) | ✅ Built |
| Communication | WebSocket JSON-RPC 2.0 (48 handlers) | ✅ Built |

---

## Features

### 🎭 Floating Character
12 animation states (idle, listening, observing, thinking, has_suggestion, acting, speaking, sleeping, blocked, error, working, dreaming) — all fully wired to real agent activity (thinking while the LLM works, speaking during TTS, acting during action execution, error on failures, blocked under privacy guard). 6 vector shapes, cursor gaze tracking, breathing/stretch animations, glow effects, mood tinting, progress ring, 7 particle types. **Chat-bubble replies** pop above the character when you click it.

### 🦊 Sprite Skins (codex-pet style)
Upload any animated GIF or a ZIP of per-state GIFs in Settings → Character and the floating character becomes that pet — all 12 agent states keep working on top (thinking/error/sleeping effects included). State clips map by file name (`idle.gif`, `thinking.gif`, …); skipped states fall back to `idle.gif`. Ships with the **Neon Panda** starter skin out of the box.

### 💬 AI Chat
Full chat UI with streaming responses, markdown rendering, conversation history. **Attachments**: images are analyzed by the visual model (local Florence-2 or provider vision) and described to the main model; text files are inlined; drag & drop supported. Proactive insights arrive as 💡 messages in chat and bubbles on the character. Works with any configured AI provider. Supports skill-based tool calling across all providers.

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
| **Web** | `web_search`, `web_fetch` (auto-falls back to search discovery when sites block bots) |
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
Playwright-powered Chromium browser when installed — navigate, click, type, extract text, screenshot, history navigation. **Without Playwright, navigation automatically falls back to a lightweight HTTP fetch** (browser-like headers, HTML→text), and blocked sites are re-discovered through search results. Web search uses DuckDuckGo with automatic Bing fallback (some networks block DDG) and snippet extraction.

### 📅 Calendar & Email
Local calendar with Google Calendar OAuth sync. IMAP/SMTP email — fetch unread, send, search. Dashboard integration for both.

### 🤖 Bot Bridges
Telegram (grammY with 6 commands), WhatsApp (Baileys multi-device with QR pairing), Discord (discord.js with 5 slash commands). All forward messages to Addled's chat.

### 🛡️ Safety
Prompt guard (15 injection + 5 exfiltration patterns), presence guard (meeting/gaming/away auto-sleep), rate limiter, destruction gate with approval workflow (`action.approve` / `action.deny`), **global kill-switch hotkey** (Ctrl+Shift+Alt+K, configurable), **clipboard secret filter** (API keys, tokens, passwords, private keys redacted before the agent sees them), **egress monitor** (logs + scrubs every outbound payload), and **privacy zones** (screen blackout regions + excluded apps actually applied to screenshots).

### 🧠 Memory
**Local semantic memory** — a fully offline, layered memory system: chat history (JSON, cross-session), **semantic long-term recall** (local ONNX MiniLM embeddings + BM25 hybrid search — every turn remembered; relevant past turns auto-injected into new chats, with hash fallback when the model is missing), **rolling compaction** (long conversations auto-summarize their oldest turns so early context survives), **core facts** (durable user facts the agent saves/reads via `memory_get`/`memory_set` tools — shown on the Memory page), **temporal knowledge graph** (subject → relation → object triples with semantic + time-bounded lookup), and **rolling screenshot memory** (last 30 privacy-masked screenshots, 24h auto-purge — enables "what was I doing 20 minutes ago?"). Idle-time maintenance re-embeds legacy rows, dedups and prunes in the background. All controllable in Settings → Memory.

### 🎤 Voice & Perception
**Voice input**: wake word → command → chat → spoken reply (local faster-whisper, offline). Edge TTS output. **Auto TTS toggle** (Settings → Voice): dashboard chat and character prompts speak replies out loud. **3-tier observer**: light hash (5s) / window-title classification (~15s) / deep Florence-2 vision (5 min, local), with a **Deep vision toggle + interval** in Settings → Observation (turn off to keep the vision model out of RAM). **Live screen awareness**: chat automatically receives the current activity context + latest vision description; asking "what do you see?" triggers a fresh capture. **Proactive insights**: the agent suggests help when you've been stuck on a task — delivered as chat messages + character bubbles (optionally spoken).

### 📦 Installer & Auto-Update
Windows NSIS + portable installer via electron-builder. **Weekly auto-update** against the latest GitHub release (version discovery via the GitHub API; delta updates via latest.yml + blockmap). If the repo is private, the check degrades gracefully and resumes automatically once it's public. Manual "Check for Updates" in the tray. First-run PyQt6 onboarding wizard (6 steps).

---

## WebSocket API (62 handlers)

### Core
`chat.send` `action.execute` `action.approve` `action.deny` `action.pending` `voice.speak` `character.setState` `observer.status` `system.status` `system.getProviders` `settings.get` `settings.set`

### Goals
`goal.create` `goal.list` `goal.start` `goal.cancel`

### Code
`code.bind` `code.read` `code.edit` `code.apply`

### Excel
`excel.read` `excel.write`

### Swarm
`swarm.spawn` `swarm.list` `swarm.run` `swarm.stop`

### Calendar & Email
`calendar.add` `calendar.list` `calendar.delete` `email.fetch` `email.send` `email.search`

### Browser
`browser.navigate` `browser.go_back` `browser.go_forward` `browser.click` `browser.type` `browser.screenshot` `browser.extract` `browser.close`

### Privacy & Monitoring
`privacy.setZones` `privacy.list` `privacy.excludeApp` `privacy.removeApp` `egress.list` `snapshot.list`

### Memory
`chat.history` `memory.list` `memory.deleteSummary` `memory.deleteMemory` `memory.clear` `memory.getFacts` `memory.setFact` `memory.deleteFact` `memory.listTriples` `memory.deleteTriple`

### Skill Forge
`forge.create` `forge.list`

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
│   ├── ws_server.py         # 48 JSON-RPC 2.0 handlers + server pushes
│   ├── providers/           # 7 AI providers (base + registry) + local Florence-2 vision
│   ├── skills/              # Skill registry + tool loop + forge
│   ├── character/           # States, shapes, movement, animation, avatar, particles
│   ├── actions/             # 55+ action executor (input, windows, files, system, terminal, excel)
│   ├── safety/              # Prompt guard, presence guard, destruction gate, kill switch,
│   │                       #   clipboard filter, egress monitor, privacy zones
│   ├── perception/          # 3-tier observer (light/medium/deep) + snapshot hook
│   ├── voice/               # Edge TTS + wake-word STT (faster-whisper)
│   ├── memory/              # Chat history, semantic recall (ONNX MiniLM + BM25), compaction,
│   │                       #   facts, knowledge graph, snapshot store, maintenance
│   ├── browser/             # Playwright browser automation
│   ├── goals/               # Planner, executor, store
│   ├── code/                # Diff engine (apply with backup), language detection
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
│   └── verify_python.py     # Python environment check
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
> The auto-updater polls GitHub Releases every 4 hours.

> **Self-contained**: the installer bundles Python 3.14.7 + all core
> dependencies — no Python install needed on the target PC.
> Optional extras on the target PC: local vision (`torch` + `transformers`)
> and browser automation (`playwright install chromium`).

---

## License

MIT

