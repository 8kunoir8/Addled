# Addled

> An AI desktop companion with a floating animated character and full dashboard.
> Combines the best of Vox's PyQt6 character engine with Skales' rich capabilities.
>
> **Repository**: [github.com/8kunoir8/Addled](https://github.com/8kunoir8/Addled)

## Quick Start (Development)

```bash
# 1. Install Python dependencies
cd backend
pip install -r requirements.txt

# 2. Start the backend
python main.py

# 3. In another terminal, start the dashboard
cd dashboard
npm install
npm run dev

# 4. In a third terminal, start the Electron shell
npm run dev
```

Or use the dev launcher:
```bash
scripts\dev.bat
```

## Architecture

- **Python Backend** — AI engine, character rendering (PyQt6), screen observation, action execution, voice, safety
- **Next.js Dashboard** — Chat, goals, code mode, swarm, settings, bot management
- **Electron Shell** — Desktop window, system tray, auto-updater
- **WebSocket Bridge** — JSON-RPC 2.0 between backend and frontend

## Features

- 🎭 **Floating Character** — 12 animation states reflecting AI model activity
- 💬 **Chat** — Full chat with streaming responses and markdown
- 🎯 **Goals** — Background autonomous task execution
- 💻 **Code Mode** — Inline diffs with Monaco Editor
- 🐝 **Agent Swarm** — Multiple AI agents working in parallel
- 🌐 **Browser Control** — Playwright-based web automation
- 🤖 **Bot Bridges** — Telegram, WhatsApp, Discord
- 📅 **Calendar + Email** — Google Calendar, IMAP/SMTP
- 🛡️ **Safety** — 8 security guards protecting your data
- 🎤 **Voice** — TTS + STT with wake word

## License

MIT
