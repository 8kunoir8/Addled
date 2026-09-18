# Addled

> An AI desktop companion with a floating animated character, full web dashboard,  
> voice interaction (wake word + speech), 30 built-in skills, self-extending  
> capability forge, 10 AI provider backends, live screen awareness, long-term  
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
> The **local AI model** (llamafile + Qwen3-4B, ~2.4 GB) is downloaded on demand
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
│  │  10 AI Providers • Goals • Code    │  │
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
| Communication | WebSocket JSON-RPC 2.0 (126 handlers) | ✅ Built |

---

## Features

### 🎭 Floating Character
12 animation states (idle, listening, observing, thinking, has_suggestion, acting, speaking, sleeping, blocked, error, working, dreaming) — all fully wired to real agent activity (thinking while the LLM works, speaking during TTS, acting during action execution, error on failures, blocked under privacy guard). 6 vector shapes, cursor gaze tracking, breathing/stretch animations, glow effects, mood tinting, progress ring, 7 particle types. **Chat-bubble replies** pop above the character when you click it.

### 🦊 Sprite Skins (codex-pet style)
Upload any animated GIF or a ZIP of per-state GIFs in Settings → Character and the floating character becomes that pet — all 12 agent states keep working on top (thinking/error/sleeping effects included). State clips map by file name (`idle.gif`, `thinking.gif`, …); skipped states fall back to `idle.gif`. Ships with the **Neon Panda** starter skin out of the box.

### 💬 AI Chat
Full chat UI with streaming responses, markdown rendering, conversation history. **Attachments**: images are analyzed by the visual model (local Florence-2 or provider vision) and described to the main model; text files are inlined; drag & drop supported. Proactive insights arrive as 💡 messages in chat and bubbles on the character. Works with any configured AI provider. Supports skill-based tool calling across all providers.

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

**Local by default**: with no API key configured, Addled runs **Qwen3-4B-Instruct-2507**
(3-bit GGUF) through **llamafile** on `127.0.0.1` — fully offline, no account. The
model is **not** bundled with the installer: Addled asks once (setup wizard and a
dashboard prompt) before downloading ~2.4 GB, and the download resumes if it is
interrupted. Decline and Addled falls back to **OpenRouter**
(`nvidia/nemotron-3-ultra-550b-a55b:free`) — paste a key in Settings → Providers.
`Hugging Face (Local)` runs any Hugging Face chat model in-process with
`transformers` (optional install from Settings).

**Runs only when you use it**: the local server never preloads at launch. It starts
when *Addled Local* is the selected provider (Settings → Providers → *Start
automatically when selected provider*) or when *Keep local AI running* is switched
on — and any message routed to it starts it on demand (~3 s). While it is not
selected or kept, it stays stopped and frees its RAM after `local_llm.idle_unload_min`
(15 min) of inactivity.

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

### 🛠️ 56 Built-in Skills (Provider-Agnostic)
All 10 AI providers can invoke any skill — no provider lock-in.

| Category | Skills |
|----------|--------|
| **System** | `run_command`, `get_screen_size`, `screenshot`, `get_clipboard`, `set_clipboard`, `set_volume`, `set_brightness`, `lock_screen` |
| **Files** | `read_file`, `write_file`, `list_dir`, `search_files`, `delete_file`, `create_dir`, `file_info` |
| **Windows** | `list_windows`, `focus_window`, `resize_window`, `close_window` |
| **Browser** | `browser_navigate`, `browser_extract`, `browser_click`, `browser_type` |
| **Code** | `code_read`, `code_edit` |
| **Calendar** | `calendar_add`, `calendar_list` |
| **Web** | `web_search`, `web_fetch` (auto-falls back to search discovery when sites block bots) |
| **Memory relations** | `memory_get`, `memory_set`, `memory_link`, `memory_unlink`, `memory_related`, `memory_files`, `memory_graph` |
| **Wiki** | `wiki_search`, `wiki_read`, `wiki_write`, `wiki_ingest`, `wiki_links`, `wiki_lint` |
| **Meta** | `forge_skill`, `list_forged`, `guidelines_status`, `ponytail_review` |

Plus the MCP-bridged skills from any connected server, registered dynamically as `mcp__<server>__<tool>`.

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

### 📅 Calendar, Tasks & Scheduling
Local calendar with Google Calendar OAuth sync — and a **tick-driven task scheduler** (no cron/APScheduler): one-shot and recurring tasks (daily / weekly / monthly) with `notify` (reminder: bubble + spoken TTS) and `chat` (run a prompt later) actions. Calendar events fire reminders before they start; relative dates like "tomorrow 3pm" are normalized on add. Tasks are created from the **Calendar page** (day-click sidebar with edit/pause/delete), from **chat** via the `task_schedule` skill, and from **voice** — including a heuristic parser fallback that works when the LLM provider is down. Memory-maintenance housekeeping jobs run through the same scheduler. IMAP/SMTP email — fetch unread, send, search.

### 🤖 Bot Bridges
Telegram (grammY with 6 commands), WhatsApp (Baileys multi-device with QR pairing), Discord (discord.js with 5 slash commands). All forward messages to Addled's chat. Scheduler notifications broadcast as `bot.notify` events for bridge push integration.

### 💗 Lifelike companion (mood, timeline, presence)
A persistent **mood & emotion engine** (valence + energy, decays over time) drives the character's visual tint, movement energy and **voice emotion** (Kokoro speech speed follows the mood). **Barge-in**: start talking while Addled speaks and it stops mid-sentence. **Initiative cadence**: return greetings + a daily check-in. An **episodic timeline** journals every day (nightly summaries) and a learned **user model** (preferences, rituals, hours) is injected into every chat — the agent references its own past naturally. **Project awareness**: index your code workspace (semantic search, chat injection). **Predictive proactivity**: weekly rhythms mined from tasks/calendar → gentle suggestions. **Reflection loop**: per-skill telemetry + weekly self-review.

### � Remote access (Tailscale)
Reach Addled from your phone or another machine, in a browser, over Tailscale — it manages an existing Tailscale install (status, sign-in, `tailscale serve` sharing) and never installs Tailscale itself.

The WebSocket API has **no authentication of its own**, and several of its 138 methods can run shell commands or synthesise input. Rather than spread credential checks across all of them, remote access goes through a separate **gateway** that owns the whole remote surface:

- It serves the login page and the dashboard, and only bridges a WebSocket to `127.0.0.1:9876` **after** validating a session. It binds loopback only; Tailscale terminates TLS and proxies to it, so the browser gets a real `https://<machine>.<tailnet>.ts.net` URL and `wss://` works without Addled handling a certificate.
- The **password is scrypt-hashed** (`settings.json`), sessions are **in-memory only** (a restart logs everyone out), login attempts are **rate-limited per address**, and the session cookie is `HttpOnly` + `SameSite=Lax` (+ `Secure` over HTTPS).
- **The gateway refuses to start without a password**, and sharing refuses too. There is no way to publish an unauthenticated dashboard.
- A logged-in remote session is **not automatically root**: running commands and controlling the mouse/keyboard are blocked for it unless you turn them on in Settings → Remote, on the machine itself. `mcp.add`/`mcp.connect` and `skills.installFrom` stay blocked regardless — they spawn processes or install code, which no browser session needs.
- Secrets are **redacted from `settings.get`** for remote connections, so one request can no longer return every API key and refresh token.
- The WebSocket handshake now checks the **`Origin`** header. Before this, any page in any browser on the machine could open `ws://127.0.0.1:9876` and drive the API — loopback stops the network, not the user's own browser.
- Tailscale **Funnel** (public internet) is off by default and needs an explicit opt-in.

Run it: **Remote** page → generate a password → sign in to Tailscale → share to your tailnet. Defaults the whole feature off.

### �🛡️ Safety
Prompt guard (15 injection + 5 exfiltration patterns), presence guard (meeting/gaming/away auto-sleep), rate limiter, destruction gate with approval workflow (`action.approve` / `action.deny`), **global kill-switch hotkey** (Ctrl+Shift+Alt+K, configurable), **clipboard secret filter** (API keys, tokens, passwords, private keys redacted before the agent sees them), **egress monitor** (logs + scrubs every outbound payload), and **privacy zones** (screen blackout regions + excluded apps actually applied to screenshots).

### 🧠 Memory
**Local semantic memory** — a fully offline, layered memory system: chat history (JSON, cross-session), **semantic long-term recall** (local ONNX MiniLM embeddings + BM25 hybrid search — every turn remembered; relevant past turns auto-injected into new chats, with hash fallback when the model is missing), **rolling compaction** (long conversations auto-summarize their oldest turns so early context survives), **core facts** (durable user facts the agent saves/reads via `memory_get`/`memory_set` tools — shown on the Memory page), **temporal knowledge graph** (subject → relation → object triples with semantic + time-bounded lookup), and **rolling screenshot memory** (last 30 privacy-masked screenshots, 24h auto-purge — enables "what was I doing 20 minutes ago?"). Idle-time maintenance re-embeds legacy rows, dedups and prunes in the background. All controllable in Settings → Memory.

### 🔗 Memory Relations (one graph over everything)
Every memory store above is an independent flat collection, so nothing knew that a fact and a file were about the same thing. **`links.db`** is a single edge table across all of them — `fact`, `triple`, `memory`, `summary`, `journal`, `wiki`, `file` — with a closed relation vocabulary (`relates_to`, `same_as`, `supersedes`, `contradicts`, `derived_from`, `mentions`, `part_of`, `sourced_from`, `documents`, `links_to`). Relations are recorded automatically as memories are written: **file mentions** are scanned out of a fact/triple/memory and linked to the path on disk, and **provenance** links each derived item back to whatever produced it (a triple to its conversation, a summary to its journal day). Related items are injected into chat as a `[Related]` block, which is what lets an answer volunteer *"and the file for that is …"* instead of stopping at the fact. Idle maintenance drops edges whose target no longer exists and flags facts that say the same thing in different words as `same_as`. Tools: `memory_link` `memory_unlink` `memory_related` `memory_files` `memory_graph`. Dashboard: Memory page → Relations (trace any item, list referenced files, prune).

### 📖 Wiki (LLM Wiki, local-first)
A **Karpathy-pattern wiki** instead of another chat transcript: markdown pages under `backend/memory/wiki/pages/` with YAML frontmatter, maintained *incrementally* from your own sources. Ingesting a document first pulls the pages that already look related and asks the model for **merged page bodies**, so a second document about the same topic updates the page and appends its citation rather than creating a rival page — and the citation is preserved on every later rewrite. `[[wiki-links]]` between pages are mirrored into the relation graph (`links_to`), and non-URL sources become `sourced_from` edges to the real files, so the wiki also answers "which file said this?". Relevant pages are injected into chat with their citations as a `[Wiki]` block, since pre-distilled pages are better evidence than a guess. **`auto_ingest` is off by default** — Addled never reads your files into the wiki unless you ask. Tools: `wiki_search` `wiki_read` `wiki_write` `wiki_ingest` `wiki_links` `wiki_lint`. Dashboard: Wiki page (list, search, edit, ingest, lint).

### 🎤 Voice & Perception
**Voice input**: wake word → command → chat → spoken reply. **Silero VAD** segments real speech for turn detection (RMS fallback). STT: **faster-whisper** (local, offline; tiny/small selectable) with **SenseVoice** auto-activating when funasr is available. **TTS**: **Kokoro** neural voices fully offline (local 82M ONNX) with **edge-tts** online fallback — engine selectable in Settings → Voice, with an **Auto TTS toggle** so dashboard chat and character prompts speak replies out loud. **Voice pickers** list what is actually available rather than asking you to type a voice name: the installed Kokoro pack is read straight out of `voices-v1.0.bin` (54 voices, offline, without loading the model) and the Edge voices come from the service, cached for a day with a curated offline fallback. Both are grouped by language and follow the **Language** setting, and where an engine simply cannot speak the chosen language — Kokoro has no Indonesian voice — Addled says so and routes to one that can. Speech input is local & offline; Edge TTS is the only network-dependent part. **3-tier observer**: light hash (5s) / window-title classification (~15s) / deep Florence-2 vision (5 min, local), with a **Deep vision toggle + interval** in Settings → Observation (turn off to keep the vision model out of RAM). **Live screen awareness**: chat automatically receives the current activity context + latest vision description; asking "what do you see?" triggers a fresh capture. **Proactive insights**: the agent suggests help when you've been stuck on a task — delivered as chat messages + character bubbles (optionally spoken).

### � Skills & Market
33 built-in skills with a **Skills dashboard page** — every skill can be toggled on/off, market/forged skills deleted. **SKILL.md market support**: install skills from Claude Code/Copilot/opencode ecosystems via URL or GitHub repo, and **market search** (GitHub `claude-skills` topic, ranked with the local embedder) — when the agent calls a missing skill, Addled auto-finds, installs and runs the best market match, falling back to LLM code-generation (Skill Forge).

### 🌐 Browser & Desktop
Four browser backends with **task-based routing**: your running Chrome/Edge via CDP (read-only by default, opt-in), Playwright's own Chromium, the **browser-use** AI framework for open-ended tasks (used only when installed AND an LLM is available), and an always-on HTTP fallback. Missing backends can **auto-install on demand** (`ask` / `off` / `auto` policy with a dashboard approval banner). **Desktop control** (mouse/keyboard) is first-class but off by default: session grants via dashboard prompt, screen-bounds checks, typing caps, hotkey whitelist, pyautogui FAILSAFE, and egress logging.

### �📦 Installer & Auto-Update
Windows NSIS + portable installer via electron-builder. **Weekly auto-update** against the latest GitHub release (version discovery via the GitHub API; delta updates via latest.yml + blockmap). If the repo is private, the check degrades gracefully and resumes automatically once it's public. Manual "Check for Updates" in the tray. First-run PyQt6 onboarding wizard (6 steps).

---

## WebSocket API (126 handlers)

### Core
`chat.send` `action.execute` `action.approve` `action.deny` `action.pending` `voice.speak` `voice.voices` `character.setState` `observer.status` `system.status` `system.getProviders` `settings.get` `settings.set` `models.routes` `models.catalog` `models.refresh` `guidelines.state` `guidelines.refresh` `mcp.list` `mcp.add` `mcp.update` `mcp.remove` `mcp.connect` `mcp.disconnect` `mcp.reload` `mcp.tools` `localLlm.status` `localLlm.installApprove` `localLlm.installDecline` `localLlm.start` `localLlm.stop` `localLlm.remove` `localLlm.installHfDeps` `hf.unload`

### Goals
`goal.create` `goal.list` `goal.start` `goal.cancel`

### Code
`code.bind` `code.read` `code.edit` `code.apply`

### Excel
`excel.read` `excel.write`

### Swarm
`swarm.spawn` `swarm.list` `swarm.run` `swarm.stop`

### Calendar & Email
`calendar.add` `calendar.list` `calendar.delete` `calendar.update` `tasks.schedule` `tasks.list` `tasks.update` `tasks.pause` `tasks.resume` `tasks.cancel` `tasks.month` `email.fetch` `email.send` `email.search`

### Browser
`browser.navigate` `browser.go_back` `browser.go_forward` `browser.click` `browser.type` `browser.screenshot` `browser.extract` `browser.close` `browser.status` `browser.task` `browser.installApprove`

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
│   ├── ws_server.py         # 126 JSON-RPC 2.0 handlers + server pushes
│   ├── providers/           # 10 AI providers (base + registry + selector + router)
│   │                        #   + live model catalog + local Florence-2 vision
│   ├── mcp_client/          # MCP client (stdio + streamable HTTP) → tools as skills
│   ├── guidelines/          # External rulesets (ponytail, Karpathy): fetch, cache, inject
│   ├── local_llm/           # llamafile runtime manager (ask-first download, start/stop, idle unload)
│   ├── local_models/        # Resumable model downloads + storage paths
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
> The auto-updater polls GitHub Releases every 4 hours.

> **Self-contained**: the installer bundles Python 3.14.7 + all core
> dependencies — no Python install needed on the target PC.
> Optional extras on the target PC: local vision (`torch` + `transformers`)
> and browser automation (`playwright install chromium`).

---

## License

MIT

