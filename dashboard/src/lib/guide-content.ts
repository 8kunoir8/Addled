/**
 * The text behind Settings → Guide.
 *
 * Kept as data rather than JSX so the guide can be searched, reordered and
 * audited without touching layout code, and so `scripts/check_guide.py` can
 * read it to prove the guide does not reference a Settings tab, a dashboard
 * route or a status flag that does not exist.
 *
 * Two rules for anything added here:
 *
 *   1. Describe what the app actually does. Where a feature needs something
 *      that may be missing, say so in `needs` and let the badge do the talking
 *      rather than writing "may not work" into the prose.
 *   2. Do not repeat a Settings description word for word. Settings says how to
 *      change one value; the guide says what the feature is for and how to use
 *      it, then links to the setting.
 */

/** Every flag `guide.status` returns. Typed so a typo fails the build. */
export type GuideStatus = {
  any_model_ready: boolean;
  bots_ready: number;
  browser_automation: boolean;
  desktop_approval_required: boolean;
  desktop_input_allowed: boolean;
  discord_configured: boolean;
  discord_ready: boolean;
  discord_running: boolean;
  local_model_installed: boolean;
  local_model_running: boolean;
  mcp_connected: number;
  mcp_enabled: boolean;
  mcp_servers: number;
  mcp_tools: number;
  mcp_untrusted: number;
  node_available: boolean;
  npx_available: boolean;
  providers_configured: number;
  providers_total: number;
  remote_enabled: boolean;
  skills_total: number;
  smart_routing_on: boolean;
  stt_available: boolean;
  stt_sensevoice: boolean;
  tailscale_installed: boolean;
  telegram_configured: boolean;
  telegram_ready: boolean;
  telegram_running: boolean;
  tts_available: boolean;
  uvx_available: boolean;
  whatsapp_configured: boolean;
  whatsapp_ready: boolean;
  whatsapp_running: boolean;
  workspace_configured: boolean;
  workspace_enforced: boolean;
};

/**
 * A precondition the machine may not meet.
 *
 * `want` is the value that means "ready", so a flag that is better when False
 * (for example `desktop_input_allowed`) reads naturally. The badge only shows
 * when the live value is not `want`.
 */
export type GuideNeed = {
  flag: keyof GuideStatus;
  want: boolean;
  /** What is missing and where to fix it, shown only when the badge is up. */
  label: string;
};

export type GuideEntry = {
  id: string;
  title: string;
  icon: string;
  /** One line, used in search results and under the title. */
  summary: string;
  /** What the feature is and why it exists. */
  what: string;
  /** Numbered, concrete. "How to use it". */
  how: string[];
  /** Settings tab ids this feature is changed from. Must exist in Settings. */
  settingsTabs?: string[];
  /** Dashboard routes this feature is used from. Must exist in the nav. */
  routes?: { href: string; label: string }[];
  /** Skill names from the live catalogue worth pointing at. */
  skills?: string[];
  needs?: GuideNeed[];
  /** Render the full live skill catalogue inside this entry. */
  showSkillCatalogue?: boolean;
};

/** A short, human explanation of each status flag, for badges and the summary. */
export const FLAG_LABELS: Partial<Record<keyof GuideStatus, string>> = {
  browser_automation: 'Playwright is not installed',
  uvx_available: 'uvx is not on PATH',
  node_available: 'Node.js is not on PATH',
  desktop_input_allowed: 'Desktop control is switched off',
  workspace_configured: 'No workspace folder is bound',
  remote_enabled: 'Remote access is switched off',
  tailscale_installed: 'Tailscale is not installed',
  any_model_ready: 'No AI provider is ready',
};

export const GUIDE: GuideEntry[] = [
  {
    id: 'getting-started',
    title: 'Getting started',
    icon: '🚀',
    summary: 'What Addled is, what runs where, and how to stop it.',
    what:
      'Addled is a desktop assistant: a character that floats on your screen, a local backend that does the work, and this dashboard for everything that needs more room than a character can offer. The character is what you talk to. The dashboard is where you configure it and where long-running jobs — code edits, swarms, goals — are watched. Your conversations, memory and settings live on your machine; the only traffic that leaves it is requests to the AI provider you pick and the model downloads you ask for.',
    how: [
      'Launch Addled. The character appears in the corner set by Settings → Character, and a tray icon is added.',
      'Click the character and type or speak. The same conversation is visible on the Chat page, and continues there if you open it.',
      'Open the dashboard from the tray menu. Every page in the left nav is described in this guide.',
      'If something goes wrong and you want it to stop immediately, press Ctrl+Shift+Alt+K. That is the kill switch (Settings → Safety → Kill switch hotkey) and it halts Addled without asking.',
      'Everything starts from one AI provider. Configure at least one in Settings → Providers before expecting a reply.',
    ],
    settingsTabs: ['providers', 'character'],
    needs: [
      {
        flag: 'any_model_ready',
        want: true,
        label: 'No provider is ready yet. Add an API key in Settings → Providers, or install a local model there.',
      },
    ],
  },

  {
    id: 'chat',
    title: 'Chat',
    icon: '💬',
    summary: 'Conversations, sessions, roles, and what one turn can actually do.',
    what:
      'Chat is the main loop: your message goes to the active model together with recalled memory, any relevant skills, and the conversation so far, and the answer streams back. Addled can also use tools during a turn — read a file, search the web, run a command if you have allowed it — and it decides which to use. A turn can therefore take a few seconds or a few minutes; the dashboard waits up to three minutes before giving up on a request.',
    how: [
      'Type in the box at the bottom of the Chat page, or talk to the character. Enter sends.',
      'Watch the tool rows: when Addled calls a tool, the call and a short result appear in the conversation rather than being hidden.',
      'Switch sessions from the list on the Chat page. Each session keeps its own history.',
      'Change the role to bias the answer — analysis, coding, quick chat — from the role selector; the routing rules decide which model each role uses.',
      'Long conversations are trimmed automatically: the oldest turns are summarised in the background once the history outgrows the model, so a long chat keeps working instead of failing.',
    ],
    routes: [{ href: '/chat', label: 'Chat' }],
    settingsTabs: ['providers', 'memory'],
    skills: ['web_search', 'web_fetch', 'run_command'],
    needs: [
      {
        flag: 'any_model_ready',
        want: true,
        label: 'Chat needs a provider. Add a key in Settings → Providers, or run a local model.',
      },
    ],
  },

  {
    id: 'providers',
    title: 'Providers and models',
    icon: '🔌',
    summary: 'Cloud APIs, local models, the model catalogue and task routing.',
    what:
      'A provider is where the answers come from. Addled ships connectors for cloud APIs (DeepSeek, OpenAI, Claude, Gemini, GitHub Copilot, OpenRouter, Hugging Face) and for models that run on your own machine (a downloaded local model, Ollama, LM Studio). Anything that can speak the OpenAI API can be added as a custom provider. You can have several configured at once and switch between them, or let routing pick per task.',
    how: [
      'Open Settings → Providers, paste the API key for a provider and press Test. Keys are stored locally in the app settings file.',
      'Set the active provider — this is the one used unless routing overrides it.',
      'For local models, use the local entry: Addled can download a model for you and start it, which is the only option that needs no key and no internet afterwards.',
      'Turn on task-aware routing to send short chats to a cheap model and long or reasoning-heavy work to a stronger one. Each provider has per-role model boxes for chat, reasoning, vision and utility.',
      'Let the catalogue refresh itself: Addled asks each configured provider which models it currently offers and caches the answer, so new models appear without an update.',
    ],
    settingsTabs: ['providers'],
    needs: [
      {
        flag: 'any_model_ready',
        want: true,
        label: 'No provider is usable yet — add a key, or install a local model, in Settings → Providers.',
      },
    ],
  },

  {
    id: 'character',
    title: 'Character and appearance',
    icon: '🎭',
    summary: 'The floating companion: name, skins, size, and how it behaves.',
    what:
      'The character is the part of Addled you see all day, and it is meant to be a companion rather than a status bar. It reacts to what Addled is doing — idle, thinking, speaking — and it can be as plain as a drawn shape or as finished as a multi-frame animated sprite. A sprite skin is what most people end up using: when one is active it replaces the drawn shape entirely.',
    how: [
      'Open Settings → Character and pick a skin. The skins section lists the built-in sprites and their frame sets.',
      'Set the name Addled answers to. This is the same name used across chat, the dashboard and the bots.',
      'Set size and movement speed — how large it is, and how briskly it drifts.',
      'Turn on starting with Windows if you want it there when you sign in.',
      'The character changes state on its own as Addled works; no configuration is needed for that.',
      'Shape, colour, glow and eyes still exist in the character itself and in settings.json, but they are not offered in the interface — the skins are the supported way to change how it looks.',
    ],
    settingsTabs: ['character', 'appearance'],
    routes: [
      { href: '/settings', label: 'Settings' },
    ],
  },

  {
    id: 'voice',
    title: 'Voice',
    icon: '🎤',
    summary: 'Speaking to Addled and having it speak back.',
    what:
      'Voice has two independent halves and either can be used alone. Speech-to-text listens through your microphone, detects where a sentence starts and ends, transcribes it and treats it as a typed message. Text-to-speech reads answers aloud with the voice you choose. Both run locally by default; nothing is uploaded to a speech service unless you pick the Edge engine, which uses Microsoft voices.',
    how: [
      'Open Settings → Voice and choose the speech-to-text engine and model size. Smaller models are faster and less accurate.',
      'Choose the text-to-speech engine and voice, then use the preview button to hear it.',
      'Turn on the ambient listener if you want Addled to react to speech without you pressing anything.',
      'Turn on/off the microphone from the character or the tray rather than the dashboard when you are mid-conversation.',
      'Leave speech recognition off when you are on a call — it listens to everything it can hear, within the volume threshold.',
    ],
    settingsTabs: ['voice'],
    needs: [
      {
        flag: 'stt_available',
        want: true,
        label: 'Speech-to-text needs sounddevice and faster-whisper in the Python environment Addled runs on.',
      },
      {
        flag: 'tts_available',
        want: true,
        label: 'The selected text-to-speech engine cannot start — install its dependency in Settings → Voice, or pick another engine.',
      },
    ],
  },

  {
    id: 'memory',
    title: 'Memory',
    icon: '🧠',
    summary: 'What Addled remembers, how it is recalled, and how to correct it.',
    what:
      'Memory is what makes a second conversation better than the first. It holds saved facts about you, notes Addled decided were worth keeping, a journal, and the conversation history itself. Recall is by meaning rather than by keyword: when you ask something, Addled searches memory for the closest matches and puts them in front of the model. You can read and edit every entry — nothing is hidden from you, and deleting something is enough to stop it being recalled.',
    how: [
      'Open the Memory page to see everything that is stored, grouped by kind.',
      'Add a fact by hand. This is the most reliable way to teach it something permanent.',
      'Turn on automatic notes if you want Addled to save durable facts it notices during conversation. It is off by default because it will occasionally save something trivial.',
      'Link two memories to each other when one only makes sense with the other; recall follows the links.',
      'Delete anything wrong. Recall is only as good as what is in here, so correcting memory is more effective than arguing with the model.',
      'The Journal is a separate, append-only record of what Addled did and noticed each day — useful for answering "what did you get done", and not used for recall.',
    ],
    settingsTabs: ['memory'],
    routes: [{ href: '/memory', label: 'Memory' }],
    skills: ['memory_set', 'memory_get', 'memory_link', 'memory_related'],
    needs: [
      {
        flag: 'any_model_ready',
        want: true,
        label: 'Memory can be read and edited without a model, but it cannot be searched by meaning until a provider is available.',
      },
    ],
  },

  {
    id: 'wiki',
    title: 'Wiki',
    icon: '📖',
    summary: 'Your own reference pages, which Addled reads before answering.',
    what:
      'The wiki is where you write things down that you want Addled to treat as true: how your project is laid out, house rules, the correct names for things. It differs from memory in kind rather than in storage — memory is what Addled noticed, the wiki is what you decided. Addled searches it when a question looks like it would be answered by your own notes, and it can be told to write or update pages itself so answers get captured instead of disappearing into a chat.',
    how: [
      'Open the Wiki page and create a page. A page is markdown text with a title.',
      'Write what you would otherwise repeat. Page names matter: they are how Addled finds and links them.',
      'Link pages to each other with backlinks — the linked pages show as pills, and you can follow them both ways.',
      'Search from the page; search is over your own text only.',
      'Ask Addled to write something up ("add this to the wiki") when a conversation produces something durable.',
    ],
    settingsTabs: ['wiki'],
    routes: [{ href: '/wiki', label: 'Wiki' }],
    skills: ['wiki_write', 'wiki_search', 'wiki_read'],
  },

  {
    id: 'procedures',
    title: 'Procedures',
    icon: '📋',
    summary: 'Repeatable multi-step jobs that run the same way every time.',
    what:
      'A procedure is a written sequence of steps for something you do regularly — a release checklist, a morning routine, a standard way to triage an inbox. Addled follows the steps in order, using its tools, instead of improvising from your description each time. Several procedures are seeded on first run as worked examples; they are ordinary records, so edit or delete them freely.',
    how: [
      'Open the Procedures page. The seeded procedures show what the format looks like.',
      'Create one: a name, when it should run, and the steps in order.',
      'Run it from the page and watch the steps tick off. Steps that use tools report their result.',
      'Edit the steps whenever the real process changes — a stale procedure is worse than none.',
    ],
    routes: [{ href: '/sop', label: 'Procedures' }],
    skills: ['sop_list', 'sop_lookup', 'sop_save'],
  },

  {
    id: 'skills',
    title: 'Skills',
    icon: '🧩',
    summary: 'The things Addled can do, and how to add more.',
    what:
      'A skill is a single named capability with a description the model reads. When you ask for something, Addled looks through its skills, decides which apply, and calls them. This is the main way it reaches beyond conversation: file reading, web search, memory, shell commands, browser control and everything from MCP arrive as skills. Every skill can be switched off individually, which is the honest way to stop Addled doing something you do not want it to do.',
    how: [
      'Open the Skills page to see everything installed, grouped by category, with each one on or off.',
      'Turn off anything you do not want used. A disabled skill is not offered to the model at all, so it cannot be called by accident.',
      'Destructive skills ask first. Deleting or overwriting a file, running a risky command and applying a change to Addled\'s own code all stop for your approval, and the request waits on the page rather than silently failing.',
      'For work that needs a terminal to keep its state — changing directory and running several commands there, setting an environment variable for later, a REPL, an open ssh session — Addled opens a live shell instead of separate commands. Type into it and the next command sees what the last one did.',
      'Search the market when you want a capability that is missing. Addled searches public skill repositories, shows what it found with its match score, and installs only what you approve.',
      'Use the forge when nothing suitable exists: describe what you want and Addled writes a new skill for it, which you then review like any other file.',
      'Ask Addled to change its own code and it proposes the change first, showing the diff, and applies nothing until you agree. The files that decide what is permitted are deliberately excluded, and the change takes effect after a restart.',
      'Skills you installed appear in the same list and can be removed from it.',
      'Install the token saver from Settings → Tools if you want long command output compressed before the model reads it — git, pip, pytest, npm, gh, docker. It is a 6 MB download of two other projects\' binaries (rtk and ripgrep) from their GitHub releases, verified against the sha256 GitHub publishes for them. It is not bundled, so a fresh copy of Addled has none until you click Install; without it, commands run unchanged.',
    ],
    routes: [{ href: '/skills', label: 'Skills' }],
    settingsTabs: ['tools'],
    skills: ['forge_skill', 'list_forged', 'find_mcp_server',
             'session_open', 'verify_code', 'self_propose'],
    showSkillCatalogue: true,
  },

  {
    id: 'mcp',
    title: 'MCP servers',
    icon: '🧰',
    summary: 'Plugging in external tool servers that Addled did not ship with.',
    what:
      'MCP (Model Context Protocol) is a common way for tools to describe themselves to an AI. An MCP server is a small program that offers a set of tools; Addled connects to it, reads that list, and registers every tool as a skill. This is what lets Addled use tools nobody wrote specifically for it — a filesystem server, a database, a service you run yourself. Servers reach Addled either as a command it launches on your machine or as a URL it calls.',
    how: [
      'Open Settings → MCP and browse the market. It lists servers published in the public registries with the version and what they need.',
      'Install one you recognise, then connect it. Addled shows each tool the server offers, and anything it could not start, with the server\'s own error.',
      'Review an unfamiliar server as untrusted. Untrusted servers have a confirmation step added to every tool they expose, so a tool cannot run without you agreeing to that specific call.',
      'Let Addled find servers for itself if you want: it can look one up when it needs a capability it does not have, install it, use it, and disconnect it again when it goes idle. Only servers it found this way are ever disconnected automatically; anything you added by hand stays connected.',
      'Remove a server you no longer want. Removing it takes its tools away immediately.',
    ],
    settingsTabs: ['mcp'],
    skills: ['find_mcp_server'],
    needs: [
      {
        flag: 'uvx_available',
        want: true,
        label: 'Some published servers are Python packages that need uvx to launch, and uvx is not on PATH. Servers shipped as Node packages still work.',
      },
      {
        flag: 'npx_available',
        want: true,
        label: 'Most published servers are Node packages launched with npx, which is not on PATH. Install Node.js to use them.',
      },
    ],
  },

  {
    id: 'goals-swarm',
    title: 'Goals, tasks and swarm',
    icon: '🎯',
    summary: 'Work that takes longer than one reply, and swarm agents that remember.',
    what:
      'A goal is an objective Addled works on across turns rather than answering once: it can be started, checked on and cancelled, and it keeps its own state between attempts. Tasks are timed jobs — something to do at a time, or on a repeat. The swarm runs several agents on the same objective at once, each with a different angle, and merges what they produce; it is slower and more expensive than a single turn and worth it when a problem genuinely benefits from parallel attempts. Every agent works through the same pipeline as the Chat page, so it has the same skills and tools: an agent reads files, searches the web and runs procedures rather than only writing prose.\n\nSwarm agents are saved, not throwaway. Addled ships five — Planner, Researcher, Coder, Reviewer and QA — and each keeps a name, a role, a brief (how you want its work done) and a set of skills. They survive a restart, so "my Reviewer" is a thing you can build up over time rather than a one-off. Alongside the brief each agent keeps standing rules learned from your corrections, which is how it gets better at your work specifically.',
    how: [
      'Open the Goals page and describe the objective. Addled plans, works, and reports back as it goes.',
      'Watch progress on the page; a goal can be cancelled at any point and keeps whatever it produced.',
      'Schedule a task for work that should happen later or repeatedly.',
      'Use the Swarm page for several agents at once. Each desk is a saved definition: edit its brief and skills on the page and they apply from the next task, with no restart.',
      'Write a brief the way you would brief a colleague: tone, red lines, where to be careful. It is read before every task that agent runs.',
      'Correct a result and it is learned: send the work back with your correction and Addled records it as a standing rule for that agent — "proposals are always one page" — so the next run starts from it. Say it is a one-off and it applies only to that piece of work.',
      'Give an agent a task that needs a tool — "what is the size of this file?" — and it will use one. The card says what it may use: "all skills & tools" unless it was deliberately given a narrower set.',
      'Point an agent at a model if you want to: a cheap local model for routine desks, a stronger cloud one for the reasoning desk. Left empty, the normal routing decides.',
      'Stop removes an agent from this session; deleting it removes the saved definition too. Delete the definition and it stays deleted — Addled will not re-add a desk you removed.',
      'Expect a goal or a swarm to take minutes, not seconds. They are local loops around your provider, and their speed is the model\'s speed.',
    ],
    routes: [
      { href: '/goals', label: 'Goals' },
      { href: '/swarm', label: 'Swarm' },
    ],
    skills: ['swarm_note', 'swarm_roster', 'swarm_learn'],
    needs: [
      {
        flag: 'any_model_ready',
        want: true,
        label: 'Goals and swarms are model loops, so they need a working provider.',
      },
    ],
  },

  {
    id: 'code',
    title: 'Code and workspace',
    icon: '💻',
    summary: 'The built-in editor, verified edits, and what confinement means.',
    what:
      'The Code page is a real editor for one folder — a workspace — with syntax highlighting, tabs and saving, plus an AI edit loop. The important part is the workspace: you bind one folder, and every read, write and edit is confined to it. Paths that try to leave it are refused, and a file outside the workspace cannot be read or written even by a mistake. That single rule is what makes it safe to let Addled edit code at all.\n\nA suggested edit is applied by finding the exact text to change and replacing it, rather than by rewriting the whole file. That matters in practice: only the lines that actually changed appear in the diff, the untouched parts cannot drift, and a long file is edited as easily as a short one. You still review and apply it — nothing is written until you press Apply.',
    how: [
      'Bind a folder on the Code page. Addled opens whatever the Workspace setting points at, so set that first if you would rather not retype it.',
      'Open a file from the tree, edit it, and press Ctrl+S to save. Ctrl+F finds inside the file.',
      'Use the search panel to find a word across the whole workspace and jump to the line.',
      'Ask for a change in the box at the bottom. Addled proposes an edit, you see the diff, and nothing is written until you press Apply. Unsaved editor text is saved first, so the diff always describes what you are looking at.',
      'For a change that spans files, ask it to plan first: Addled searches the project, names the files it believes are involved, and only then proposes edits — so a multi-file change is not guessed from one open file.',
      'When the change depends on the rest of the project, let Addled read it: the suggestions run with read-only tools, so it can look up a helper, a documented convention or a saved procedure instead of guessing. It still cannot write — approving the diff is the only way a file changes.',
      'After applying a plan, Addled runs the project\'s own check — its test script, a Makefile target, or the runner the folder layout implies — and tells you whether it passed. A change that does not pass is reported as unverified rather than done.',
      'If the workspace is a git repository, applying a plan leaves a commit naming what changed. That makes "undo the last change" a normal git revert, and your own log shows what Addled did and when.',
      'Checkpoint before large changes: an applied edit also keeps the previous version beside the file as a .bak.',
    ],
    routes: [{ href: '/code', label: 'Code' }],
    settingsTabs: ['workspace', 'safety'],
    skills: ['code_read', 'code_edit', 'read_file', 'write_file', 'verify_code'],
    needs: [
      {
        flag: 'workspace_configured',
        want: true,
        label: 'No workspace folder is bound yet. Bind one on the Code page, or set it in Settings → Workspace, so edits cannot reach anything else.',
      },
      {
        flag: 'any_model_ready',
        want: true,
        label: 'The editor and saving work without a model; only the suggested-edit button needs a provider.',
      },
    ],
  },

  {
    id: 'bots',
    title: 'Bots',
    icon: '🤖',
    summary: 'Talking to Addled from Telegram, Discord or WhatsApp.',
    what:
      'A bot bridge puts Addled in a chat app, so you can ask it something from your phone and get the same assistant that is running on your desktop — same memory, same skills, same machine. It is a bridge rather than a second copy: the bot sends your message to the backend over the local socket, so the desktop does not need to be in front of you, only running.',
    how: [
      'Open the Bots page and pick a platform. Each one shows its own setup steps.',
      'Telegram: create a bot with BotFather and paste the token. Discord: create an application in the developer portal and paste the bot token.',
      'WhatsApp: press start and scan the QR code from your phone. It pairs a device rather than using a token.',
      'Press start, then send yourself a message from the chat app. The log on the page shows what the bridge is doing and any error the platform returned.',
      'Leave the page open while a bot is starting the first time — a missing dependency is reported there and nowhere else.',
    ],
    routes: [{ href: '/bots', label: 'Bots' }],
    needs: [
      {
        flag: 'node_available',
        want: true,
        label: 'The bot bridges are Node scripts and Node.js was not found on PATH.',
      },
      {
        flag: 'telegram_configured',
        want: true,
        label: 'Telegram has no bot token saved yet.',
      },
      {
        flag: 'discord_configured',
        want: true,
        label: 'Discord has no bot token saved yet.',
      },
    ],
  },

  {
    id: 'remote',
    title: 'Remote access',
    icon: '📡',
    summary: 'Reaching this machine from another device, and what is held back.',
    what:
      'Remote access lets you use the dashboard from another device over Tailscale, your own private network — no port forwarding and nothing exposed to the public internet. A remote session gets the dashboard and the assistant, but not everything: starting processes, installing code and binding a workspace are refused for remote callers, because those are decisions that should be made at the machine itself. The refusal says why rather than failing silently.',
    how: [
      'Install Tailscale and sign in on this machine. Addled detects it and shows the state on the Remote page.',
      'Turn remote access on and set a password. The password is what authenticates, not the Tailscale identity.',
      'Open the printed address from your other device and sign in. Sessions are listed on the page with the device, and can be revoked individually.',
      'Use Funnel only if you deliberately want the address reachable from outside your tailnet. It is off by default and asks for confirmation.',
      'Read the "not available remotely" list before relying on it: it is the boundary, and it is enforced in one place on the backend.',
    ],
    routes: [{ href: '/remote', label: 'Remote' }],
    settingsTabs: ['remote'],
    needs: [
      {
        flag: 'tailscale_installed',
        want: true,
        label: 'Tailscale is not installed, so there is no private network to reach this machine over.',
      },
      {
        flag: 'remote_enabled',
        want: true,
        label: 'Remote access is switched off. Turn it on in Settings → Remote to use it.',
      },
    ],
  },

  {
    id: 'browser-desktop',
    title: 'Browser and desktop control',
    icon: '🌐',
    summary: 'Driving websites, and (carefully) the mouse and keyboard.',
    what:
      'Two related abilities with different risk. Browser automation opens pages, reads them and clicks through them — it stays inside the browser. Desktop control reaches the real mouse and keyboard, which means it can act on any window, including things that matter. Both exist so Addled can finish a job that leaves the page, and both are off or gated until you turn them on.',
    how: [
      'Use the Browser page to see where Addled is browsing and to take over when it gets stuck.',
      'Install the browser backends from Settings → Browser whenever you want them: the page shows which are present and has an Install button for each, saying what it will download before you press it. It is a one-time download into Addled\'s own Python.',
      'Let them install on demand instead if you prefer: with the ask policy (the default), a task that needs a backend stops and offers to install it rather than failing.',
      'Turn on desktop control in Settings → Desktop when you want Addled to click and type outside the browser. It is off by default.',
      'Expect a session-level grant as well as the setting: even switched on, permission is asked for per session so it cannot quietly stay granted.',
      'Watch the permission banner: while desktop control is active it stays visible, and you can revoke it from there.',
    ],
    routes: [{ href: '/browser', label: 'Browser' }],
    settingsTabs: ['browser', 'desktop'],
    skills: ['browser_navigate', 'browser_extract', 'desktop_click',
             'desktop_type', 'screenshot'],
    needs: [
      {
        flag: 'browser_automation',
        want: true,
        label: 'Playwright is not installed, so clicking and typing in pages is unavailable. Reading a URL still works through a plain HTTP fetch. Install Playwright to enable the rest.',
      },
      {
        flag: 'desktop_input_allowed',
        want: true,
        label: 'Desktop control is switched off. Turn it on in Settings → Desktop if you want it.',
      },
    ],
  },

  {
    id: 'calendar-office',
    title: 'Calendar, email and office files',
    icon: '📅',
    summary: 'Dates, mail and spreadsheets as things Addled can work with.',
    what:
      'These are the tools for ordinary office work: a calendar of items with dates and times, email through your own mail account, and spreadsheet files it can read and write. They are deliberately plain — local records and direct connections to the accounts you configure, not a sync service.',
    how: [
      'Add calendar items from the Calendar page, or just tell Addled when something is happening.',
      'Configure a mail account in Settings → Integrations to let Addled send, fetch and search mail. It talks to your mail server directly using the details you give it.',
      'Ask Addled to read or update a spreadsheet file, and point it at the path inside your workspace.',
      'Check the calendar page before asking "what is on today" — that is where the answer comes from.',
    ],
    routes: [{ href: '/calendar', label: 'Calendar' }],
    settingsTabs: ['integrations'],
  },

  {
    id: 'safety',
    title: 'Privacy and safety',
    icon: '🛡️',
    summary: 'What Addled is allowed to see, do, and send out.',
    what:
      'The safety settings are the limits you set on your own assistant. There are three kinds. File access decides which folders can be read or written at all. Desktop permissions decide whether it can drive the mouse and keyboard. Privacy rules decide what it is allowed to notice — apps and screen regions it should ignore entirely — and egress control decides where it may send data. Everything is enforced on the backend, so a limit holds even when a request comes from a bot or a remote session.',
    how: [
      'Set the file access mode. Workspace-only is the strict one: reads and writes are confined to the folder you bound.',
      'Add extra folders only if you need them. Each one widens what a mistake can reach.',
      'Set privacy zones and excluded apps so password managers, banking and personal windows are never captured.',
      'Keep desktop control off unless you are actively using it, and remember the grants are per session.',
      'Review egress if you care where data can go — it limits outbound requests independently of which provider you use.',
      'Use the kill switch hotkey if Addled is doing something you did not ask for: it stops immediately and does not ask for confirmation.',
    ],
    settingsTabs: ['safety', 'appearance', 'observation'],
    needs: [
      {
        flag: 'workspace_enforced',
        want: true,
        label: 'File access is not confined yet. Bind a workspace, or set the file access mode to workspace-only in Settings → Safety.',
      },
    ],
  },

  {
    id: 'settings-reference',
    title: 'Settings reference',
    icon: '⚙️',
    summary: 'What every Settings tab is for, in one place.',
    what:
      'Every Settings tab, and the part of Addled it controls. Each tab explains its own individual options; this is the map.',
    how: [
      'Providers — which AI the answers come from, keys, and per-task model routing.',
      'Character — the floating companion: skin, size and behaviour.',
      'Voice — speech recognition and speech output, and which engine each uses.',
      'Workspace — the one folder Addled treats as its project.',
      'Safety — file access rules, desktop permissions, privacy zones, egress, kill switch.',
      'Remote — Tailscale access, password, sessions, funnel.',
      'Notifications — which events are announced, and how.',
      'Observation — whether Addled may watch the screen, and how often.',
      'Memory — what it remembers, and whether it saves notes on its own.',
      'Wiki — your own reference pages and how they are searched.',
      'Tools — skills, the skill market, and which capabilities are offered to the model.',
      'Guidelines — external coding rulesets, downloaded once and refreshed in the background.',
      'MCP — external tool servers, the market, and trust.',
      'Browser — automation settings and the browser Addled drives.',
      'Desktop — mouse and keyboard control, and its approval rules.',
      'Integrations — mail and other accounts Addled connects to.',
      'Appearance — dashboard and interface look.',
      'Guide — this page.',
      'About — version, stack, and licence.',
    ],
    settingsTabs: [
      'providers', 'character', 'voice', 'workspace', 'safety', 'remote',
      'notifications', 'observation', 'memory', 'wiki', 'tools', 'guidelines',
      'mcp', 'browser', 'desktop', 'integrations', 'appearance', 'guide', 'about',
    ],
  },
];

/** The version the dashboard was built against, from the Electron package.json. */
export const APP_VERSION = process.env.NEXT_PUBLIC_ADDED_VERSION || '0.0.0';

/**
 * Honest limits worth stating once, on the page, rather than burying a
 * disclaimer under every feature: they are properties of running a model
 * locally, not bugs.
 */
export const LIMITS: string[] = [
  'A local model has a small context window (around 8,000 tokens). Long files, long chats and multi-file edits will run out of room, where a cloud model with a large window would not.',
  'Addled plans a multi-file change, applies it, runs your project\'s own check and reports whether it passed — but it does not yet loop on a failure by itself. When the check fails, the result says so and the next attempt is yours to ask for.',
  'Undo for an applied change is git, and only in a folder that is already a repository. Everywhere else the .bak beside each file is the way back.',
  'Changing Addled\'s own code takes effect after a restart, not immediately: reloading the running source would leave it half-loaded.',
  'A feature that needs a runtime you do not have says so with a badge instead of failing later. Playwright and uvx are the two that are commonly missing.',
];
