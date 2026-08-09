# Addled

> An AI desktop companion with a floating animated character and full web dashboard.  
> Built with Python + PyQt6 + Electron + Next.js.
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

# 4. In a third terminal, start the Electron shell
cd ..
npm run dev
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
│  │10 pages        │  │(Telegram/WA/   │  │
│  │Chat/Goals/Code │  │ Discord stubs) │  │
│  │Swarm/Browser/  │  │                │  │
│  │Calendar/Bots/  │  │                │  │
│  │Settings        │  │                │  │
│  └───────┬───────┘  └───────┬────────┘  │
│          │ WebSocket         │ HTTP      │
├──────────┼───────────────────┼───────────┤
│          ▼                   ▼           │
│  ┌────────────────────────────────────┐  │
│  │      Python Backend Service        │  │
│  │  Engine • 7 AI Providers           │  │
│  │  18 WS Handlers • Chat/Code/Goals  │  │
│  │  Character System • Safety • Memory│  │
│  └────────────────┬───────────────────┘  │
│                   │ Qt Signals           │
│  ┌────────────────▼───────────────────┐  │
│  │   Floating Character (PyQt6)       │  │
│  │   12 animation states • 6 shapes   │  │
│  │   particles • mood tint • drag     │  │
│  └────────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

| Layer | Stack | Status |
|-------|-------|--------|
| AI Backend | Python 3.11+, asyncio, PyQt6, websockets, httpx | ✅ Built |
| Character Engine | PyQt6 QWidget, QPainter, 30fps animation loop | ✅ Built |
| Dashboard | Next.js 16, TypeScript, Tailwind CSS | ✅ Built |
| Desktop Shell | Electron 28+, system tray, auto-updater | ✅ Built |
| Bot Bridges | Node.js (grammY, Baileys, discord.js) | ✅ Built |
| Communication | WebSocket JSON-RPC 2.0 (18 handlers) | ✅ Built |

---

## Features

### ✅ Implemented
| Feature | Details |
|---------|---------|
| 🎭 **Floating Character** | 12 animation states (idle/listen/think/speak/act/sleep/error/work/dream/observe/suggest/blocked), 6 vector shapes, cursor gaze, breathing/stretch/blink, glow effects, mood tinting, progress ring, zzz/sparkle/gear/glow particles |
| 💬 **Chat** | Full chat UI with streaming responses, markdown, conversation history, works with any configured AI provider |
| 🔌 **7 AI Providers** | DeepSeek, OpenAI, Claude, Gemini, GitHub Copilot, Ollama (local), LM Studio (local) — all with streaming |
| ⚙️ **Settings** | 9-section settings page (providers, character, voice, safety, notifications, memory, integrations, appearance, about) with live WebSocket save |
| 🎯 **Goals** | Create goals with priority, filter by status, expand/collapse, Start/Pause/Cancel state management |
| 💻 **Code Mode** | Bind workspace folders, file tree with language-colored dots, file viewer, AI edit instruction input |
| 🐝 **Agent Swarm** | 7 built-in agent types (Coder, Writer, Analyst, Planner, Researcher, DevOps, General), spawn/stop controls |
| 🌐 **Browser** | URL bar with nav controls, screenshot viewport, action log, session management |
| 📅 **Calendar** | Full month grid, prev/next navigation, today highlight, date selection with event detail |
| 🤖 **Bot Bridges** | Telegram, WhatsApp, Discord — full implementations with command forwarding |
| 🛡️ **Safety** | Presence guard (meeting/gaming/quiet-hours detection), prompt guard, clipboard filter, kill switch config |
| 🧠 **Memory** | Chat history (JSON), session context, vector store (SQLite + numpy cosine similarity) |
| 📡 **WebSocket API** | 18 JSON-RPC methods: system, settings, chat, code, goals, swarm, browser |

### ⏳ Coming (Phases 3-7)
| Feature | Phase |
|---------|-------|
| Screen observation (OCR + vision) | Phase 3 |
| Action execution (55+ commands) | Phase 3 |
| Voice (TTS + STT + wake word) | Phase 3 |
| Full browser automation (Playwright) | Phase 3 |
| Background goal executor | Phase 5 |
| Code diff engine | Phase 5 |
| Agent swarm execution | Phase 5 |
| Calendar + Email integration | Phase 5 |
| Installer + auto-updater | Phase 7 |
| Onboarding wizard | Phase 7 |

---

## Development

```bash
# Terminal 1 — Backend
cd backend && python main.py
# → Character appears on desktop, WS on ws://127.0.0.1:9876

# Terminal 2 — Dashboard
cd dashboard && npm run dev
# → Dashboard on http://localhost:3000

# Terminal 3 — Electron (optional)
npx electron .
# → Desktop window loading dashboard
```

### Backend Modules
```
backend/
├── config.py          # Portable JSON settings
├── engine.py          # Async event loop + state machine
├── ws_server.py       # 18 JSON-RPC 2.0 handlers
├── providers/         # 7 AI providers (base + registry)
├── character/         # States, shapes, movement, animation, avatar, particles
├── safety/            # Presence guard
├── cognition/         # Decision engine
├── memory/            # Chat history, session context, vector store
├── perception/        # (Phase 3)
├── actions/           # (Phase 3)
├── voice/             # (Phase 3)
├── goals/             # (Phase 5)
├── code/              # (Phase 5)
└── swarm/             # (Phase 5)
```

### Dashboard Pages
```
dashboard/src/app/
├── chat/page.tsx       # Chat with streaming + markdown
├── goals/page.tsx      # Goal creation + progress
├── code/page.tsx       # Workspace binding + file viewer
├── swarm/page.tsx      # 7 agent cards + spawn
├── browser/page.tsx    # URL bar + screenshot + logs
├── calendar/page.tsx   # Month grid + events
├── bots/page.tsx       # Bot connection UI
└── settings/page.tsx   # 9-section config UI
```

## License

MIT
