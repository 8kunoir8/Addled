"""
Addled — portable JSON settings facade.

Reads/writes the app's settings file. Which file that is comes from
`backend.app_paths`, not from this module: the app can be installed somewhere it
cannot write (a per-machine install under ``Program Files``), so "beside
ourselves" is a preference, not a guarantee.

Default provider: DeepSeek (https://api.deepseek.com).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from backend import app_paths

log = logging.getLogger("addled.config")

# ---- root resolution ---------------------------------------------------------

# The writable data directory, chosen by `app_paths` (its docstring has the
# rule). Historically this was always the install-local `memory/`, which worked
# only because the installer was per-user.
_MEMORY = app_paths.MEMORY_DIR


def memory_dir() -> Path:
    """Where Addled's own state lives, right now.

    A function as well as the module constant, so a caller can ask after a test
    has redirected `ADDLED_DATA_DIR` rather than reading a stale import.
    """
    return app_paths.MEMORY_DIR


SETTINGS_PATH = _MEMORY / "settings.json"

# Test-only redirect. `SETTINGS_PATH` is resolved at import and every write
# goes through `settings_path()`, which checks this first.
#
# Why it exists: `scripts/check_code_editor.py` sets `workspace.root` to a temp
# folder to exercise containment, and used to do that on the LIVE settings file,
# restoring the old value in a `finally`. That works until a run is interrupted
# — then the user's real workspace is left pointing at a throwaway directory
# created by a test. It happened: the app was found bound to a leftover fixture
# in %TEMP% full of test files, with ~200 more temp workspaces accumulated
# beside it. A suite must not be able to damage the settings it is testing.
_TEST_SETTINGS_PATH: Path | None = None

def settings_path() -> Path:
    """Where settings are read and written right now."""
    return _TEST_SETTINGS_PATH or SETTINGS_PATH

def use_settings_file(path) -> None:
    """Point this process at a different settings file. For tests only.

    Deliberately not a general "override the config" switch: nothing in the app
    calls it, so a running Addled cannot be redirected by anything but a test
    that has imported this explicitly.
    """
    global _TEST_SETTINGS_PATH
    _TEST_SETTINGS_PATH = Path(path) if path else None


# ---- default settings --------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "agent_name": "Addled",
    "version": 1,
    "first_run_complete": False,
    "system": {
        "onboarded": False,
        # Whether Addled was asked to start with Windows. The wizard has always
        # applied this to the Startup folder but never recorded the choice, so
        # there was no way to show the current state or to turn it back off.
        # `local_llm.autostart` is a different thing — that one is about the
        # model server, not the app.
        "autostart": False,
    },
    "character": {
        "shape": "triangle",
        "color": "#3380FF",
        "glow": "soft-halo",
        "glow_intensity": 0.6,
        "eyes": True,
        "size": 64,
        "opacity": 0.9,
        "movement_speed": "medium",
        "idle_wander_range": 300,
        "preferred_corner": "top-right",
        "skin": "",
    },
    "providers": {
        "active": "local",
        "smart_default": True,
        "priority": ["local", "openrouter", "huggingface", "deepseek", "claude", "openai", "copilot", "gemini", "ollama", "lmstudio"],
        # Task-aware model routing (see backend/providers/router.py).
        # "default_model" stays the baseline for every role; the per-provider
        # "roles" map only overrides it where the user (or these defaults) opt in.
        "auto_route": True,
        "route_validate": True,
        "route_long_chars": 1200,
        "route_reason_chars": 400,
        "route_code_reason_chars": 80,
        # Live model discovery: ask each provider what it offers and cache it.
        "catalog_auto": True,
        "catalog_ttl_days": 7,
        "builtin": {
            "deepseek": {
                "name": "DeepSeek",
                "base_url": "https://api.deepseek.com",
                "api_key": "",
                "default_model": "deepseek-flash",
                # Cheap/fast model for ordinary conversation, the stronger model
                # for analysis. Empty values fall back to "default_model".
                # `deepseek-v4-flash` and `deepseek-chat` still answer, but the
                # API resolves both to `deepseek-flash`, so the canonical id is
                # used here and the aliases are not offered.
                "roles": {
                    "chat": "deepseek-flash",
                    "reasoning": "deepseek-v4-pro",
                    "vision": "",
                    # Non-thinking model: these jobs cap output at 200-400 tokens
                    # and a reasoning model wastes part of that budget thinking.
                    "utility": "deepseek-flash",
                },
                "models": ["deepseek-flash", "deepseek-v4-pro"],
                "vision": True,
                "vision_mode": "local",
                "vision_model": "microsoft/Florence-2-base",
            },
            "openai": {
                "name": "OpenAI",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "default_model": "gpt-4o",
                "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
                "vision": True,
            },
            "claude": {
                "name": "Claude",
                "base_url": "https://api.anthropic.com",
                "api_key": "",
                "default_model": "claude-sonnet-4-20250514",
                "models": ["claude-sonnet-4-20250514", "claude-3-opus-20240229", "claude-3-haiku-20240307"],
                "vision": True,
            },
            "gemini": {
                "name": "Gemini",
                "base_url": "https://generativelanguage.googleapis.com",
                "api_key": "",
                "default_model": "gemini-2.0-flash",
                "models": ["gemini-2.0-flash", "gemini-2.0-pro", "gemini-1.5-pro"],
                "vision": True,
            },
            "copilot": {
                "name": "GitHub Copilot",
                "base_url": "",
                "api_key": "",
                "default_model": "copilot-gpt-4o",
                "models": ["copilot-gpt-4o"],
                "vision": True,
            },
            "ollama": {
                "name": "Ollama (Local)",
                "base_url": "http://localhost:11434",
                "api_key": "",
                # A local server on loopback: it needs no key, and being marked
                # otherwise made it look unavailable to the dashboard.
                "local": True,
                "default_model": "minicpm-v:8b",
                "models": ["minicpm-v:8b", "llama3.1:8b", "deepseek-r1:8b", "qwen2.5:7b"],
                "vision": True,
            },
            "lmstudio": {
                "name": "LM Studio (Local)",
                "base_url": "http://localhost:1234/v1",
                "api_key": "",
                "local": True,
                "default_model": "local-model",
                "models": ["local-model"],
                "vision": False,
            },
            "local": {
                "name": "Addled Local (llamafile)",
                "base_url": "http://127.0.0.1:8090/v1",
                "api_key": "sk-local",
                "default_model": "qwen3-8b",
                "models": ["qwen3-8b"],
                "vision": False,
                "local": True,
            },
            "openrouter": {
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "",
                # Not a ":free" model. OpenRouter refuses those for any account
                # whose privacy settings do not allow free-model training, and
                # answers 404 — which is what a fresh install used to hit on the
                # very first message. The free models stay selectable, further
                # down the list.
                "default_model": "deepseek/deepseek-v4.1-flash",
                "models": [
                    "deepseek/deepseek-v4.1-flash",
                    "openrouter/auto",
                    "deepseek/deepseek-v3.2",
                    "qwen/qwen3.8-27b:free",
                    "nvidia/nemotron-3-ultra-550b-a55b:free",
                ],
                "vision": True,
                # Also not a ":free" model: a blocked vision model means image
                # attachments and the deep-vision tier fail for accounts whose
                # privacy settings disallow free-model training, and the local
                # model cannot fill in because it is text-only.
                "vision_model": "google/gemini-3.8-flash",
                "extra_headers": {
                    "HTTP-Referer": "https://github.com/8kunoir8/Addled",
                    "X-Title": "Addled",
                },
            },
            "huggingface": {
                "name": "Hugging Face (Local)",
                "base_url": "",
                "api_key": "",
                "default_model": "Qwen/Qwen3-4B-Instruct-2507",
                "models": [
                    "Qwen/Qwen3-4B-Instruct-2507",
                    "microsoft/Phi-4-mini-instruct",
                    "Qwen/Qwen3-1.7B",
                ],
                "vision": False,
                "local": True,
                "device": "auto",
                "dtype": "auto",
                "load_in_4bit": False,
                "max_new_tokens": 1024,
                "model_root": "",
            },
        },
        "custom": [],
    },
    "local_llm": {
        "enabled": False,
        "backend": "llamafile",
        "runtime_version": "0.10.6",
        "runtime_url": "https://github.com/mozilla-ai/llamafile/releases/download/0.10.6/llamafile-0.10.6",
        "runtime_name": "llamafile.exe",
        "runtime_sha256": "d579f61dcd3a306f518e6d90e599d77793ed5f09543023d09c96ad35fcfa63f0",
        "model_repo": "unsloth/Qwen3-8B-GGUF",
        "model_file": "Qwen3-8B-Q4_K_M.gguf",
        "model_url": "https://huggingface.co/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
        "model_sha256": "",
        "model_name": "qwen3-8b",
        "size_mb": 5030,
        "host": "127.0.0.1",
        "port": 8090,
        "ctx": 8192,
        "gpu": "auto",
        "threads": 0,
        # Server slots, passed as -np. llamafile's own default is "auto", which
        # picked 4 here, and `-c` is applied PER SLOT -- so the effective context
        # (and KV cache) was 4x what `ctx` says. Nothing in the app issues
        # concurrent requests to the local model (swarm.Orchestrator.-
        # _single_generation_provider forces sequential), so one slot is enough
        # and it keeps the KV cache at 1x.
        "slots": 1,
        # KV cache precision, passed as -ctk/-ctv. f16 is the llama.cpp default;
        # q8_0 halves the cache with no measurable cost at this context size.
        # Empty means "let llamafile decide".
        "kv_type": "",
        "extra_args": [],
        "idle_unload_min": 15,
        "autostart": True,
        "prompt_on_first_run": True,
        "asked": False,
        "download_approved": False,
        "declined": False,
    },
    # An OpenAI-compatible endpoint in front of the local model, so agent tools
    # (Copilot, Codex, ...) can use it as their model. Off by default: it serves
    # the machine's GPU to whatever can reach the port, so it is a choice, not
    # something an update should switch on.
    #
    # `token` is generated on first start and kept, because a token that changed
    # every launch would mean re-configuring every tool each time.
    "model_api": {
        "enabled": False,
        "port": 8099,
        # Reserved for exposing the endpoint beyond this machine. Not honoured
        # yet: the server always binds loopback until this is implemented.
        "expose_lan": False,
        "token": "",
    },
    "chat": {
        "max_tokens": 4096,
        "temperature": 0.7,
        "system_prompt": (
            "You are Addled, a helpful AI desktop companion. "
            "You can manage calendars, schedule background tasks, inspect screens, execute actions, "
            "control desktop/browser, and help proactively. "
            "Be concise, friendly, and practical."
        ),
        "context_messages": 20,
    },
    "memory": {
        "semantic_embeddings": True,
        "hybrid_search": True,
        "recall_top_k": 3,
        # Below roughly a third of a cosine, "most similar" stops meaning
        # "related". At the 0.05 this shipped with, a greeting recalled whatever
        # memory happened to be nearest, and the model answered that instead of
        # the question.
        #
        # This is the *relevance gate* — the bar a memory must clear to be
        # injected into the prompt at all. It is deliberately strict.
        "min_similarity": 0.35,
        # The *retrieval floor*: the bar a candidate must clear to enter the
        # ranking in the first place. It is lower than the gate on purpose.
        #
        # Measured on this machine's own store, English-centric MiniLM scores
        # Indonesian and code-mixed conversation turns at 0.23–0.33, so a 0.35
        # floor admitted ~0 semantic candidates and hybrid search silently
        # became keyword-only. Admitting them here lets reciprocal rank fusion
        # weigh them against BM25; the gate above still decides what is used.
        "retrieval_min_similarity": 0.25,
        # Which embedding model to use: "minilm" (bundled, English-centric) or
        # "multilingual" (better on this user's Indonesian and code-mixed turns,
        # but ~470 MB and not bundled). Changing this requires re-embedding the
        # store — `reembed_all()` reports what is left, and mixing two models'
        # vectors is undetectable from a similarity score.
        "embedder": "minilm",
        "compaction_threshold": 12,
        "auto_facts": False,
        "graph_extract": False,
        "facts_max": 200,
        "maintenance_interval_min": 30,
        "dedup_min_sim": 0.95,
        "max_age_days": 365,
    },
    # Relations between memories, and between memories and files (links.db).
    "links": {
        "enabled": True,
        "auto_file_links": True,
        "auto_provenance": True,
        "dedup_links": True,
        "prune_interval_h": 24,
        "related_depth": 1,
        "related_inject": 5,
    },
    # LLM Wiki: an interlinked markdown knowledge base Addled maintains from
    # sources, instead of retrieving from scratch on every question.
    "wiki": {
        "enabled": True,
        "dir": "",
        "auto_ingest": False,
        "inject_top_k": 3,
        "max_chars": 4000,
    },
    # Bot bridges (the Node scripts under bots/). Addled stores their tokens and
    # can start/stop the processes; the bots themselves talk to the WS server.
    "bots": {
        "tokens": {},
        # Which bridges a scheduled reminder should reach. Empty means any that
        # is running — the right default for a single-bot setup, and the only
        # behaviour that works before anyone visits Settings. Set it when more
        # than one bridge is running, or a reminder is delivered once per bot.
        "notify_platforms": [],
        # Where the record of what each bridge exchanged lives. Empty means
        # backend/memory/bot_history.
        "history_dir": "",
    },
    "tools": {
        "rtk_enabled": True,
    },
    "skills": {
        "disabled": [],
        "pinned": [],
        "market_search": True,
        "market_sim_threshold": 0.45,
        "allow_script_skills": False,
    },
    # External rulesets Addled can follow (ponytail, Karpathy). Fetched from
    # upstream, cached under backend/memory/guidelines/, refreshed weekly.
    # "scope" is the default: "code" injects only for code-related requests.
    "guidelines": {
        "enabled": True,
        "scope": "code",
        "max_chars": 6000,
        "ttl_days": 7,
        "packs": {
            "ponytail": {"enabled": True, "level": "full", "scope": "code"},
            "karpathy": {"enabled": True, "level": "full", "scope": "code"},
        },
    },
    # MCP servers — third-party tool servers Addled can call. Each entry:
    # {id, name, transport: stdio|http, command, args[], env{}, cwd,
    #  url, headers{}, enabled, trusted, timeout_s}
    # "trusted" skips the per-tool approval prompt.
    "mcp": {
        "enabled": True,
        "autoconnect": True,
        "servers": [],
        # Values a listing declared it needs before it can run — environment
        # variables and headers, keyed by variable name. Held here rather than
        # on one server so a variable is answered once and reused wherever it is
        # asked for, including when the agent installs a server on its own.
        # Never returned to the dashboard; it reports which names are known.
        "credentials": {"env": {}, "headers": {}},
        # The official MCP registry, browsable from Settings -> MCP.
        "market_enabled": True,
        # Let the agent find and start an unconfigured server when a task needs
        # one. Only entries that need nothing from the user are taken.
        "auto_acquire": True,
        # Minutes of idleness before a server the agent added is disconnected
        # again. Its configuration stays, so reconnecting is instant.
        "auto_deactivate_minutes": 30,
    },
    "browser": {
        "user_browser": False,
        "cdp_port": 9222,
        "readonly": True,
        "engine": "auto",
        "task_mode": "auto",
        "framework_max_steps": 10,
        "auto_install": "ask",
    },
    "desktop": {
        "allow_input": False,
        "require_session_approval": True,
        "session_timeout_min": 15,
        "max_type_chars": 500,
        "allow_extended_hotkeys": False,
    },
    "scheduling": {
        "enabled": True,
        "poll_s": 5,
        "max_scheduled": 20,
        "reminder_lead_min": 10,
        "missed_policy": "catchup_once",
        "llm_actions": ["notify", "chat"],
    },
    "mood": {
        "enabled": True,
        "decay_h": 6.0,
    },
    "initiative": {
        "enabled": True,
        "greeting_enabled": True,
        "greeting_cooldown_min": 30,
        "checkin_enabled": True,
        "checkin_time": "09:00",
    },
    "project": {
        "enabled": False,
        "roots": [],
        "max_files": 500,
        "inject_into_chat": True,
    },
    # The folder Addled works in. File tools are confined to it (plus
    # extra_dirs, when safety.file_access_mode is "custom"), relative paths
    # resolve against it, and the Code page and project indexer default to it.
    "workspace": {
        "root": "",
        "extra_dirs": [],
    },
    # Standard operating procedures: what worked last time for this kind of
    # task. The similarity bars are per scoring method because an embedding
    # cosine and a word-overlap share are different units — a matching task
    # scores about 0.45 by embedding and 0.42 by words, an unrelated one 0.20,
    # and one from the wrong category 0.28.
    "sop": {
        "enabled": True,
        "learn": True,
        "inject_top_k": 1,
        "min_similarity": 0.35,
        "lexical_min_similarity": 0.25,
        "merge_similarity": 0.82,
        "lexical_merge_similarity": 0.60,
        "max_per_category": 40,
        "dir": "",
    },
    # Remote access: an authenticated gateway, so other machines can use the
    # dashboard in a browser. The gateway itself binds loopback only; Tailscale
    # Serve terminates TLS and proxies to it.
    "remote": {
        "enabled": False,
        "port": 9878,
        # scrypt hash. Empty means remote access cannot be opened at all.
        "password_hash": "",
        "session_hours": 12,
        "idle_timeout_minutes": 60,
        "max_sessions": 8,
        # The two highest-risk categories stay local unless deliberately opened.
        # A logged-in remote session is not automatically root.
        "allow_shell": False,
        "allow_desktop_input": False,
        # Public internet exposure via Tailscale Funnel. Off, and not a switch
        # to flip casually.
        "allow_funnel": False,
        "trusted_origins": [],
    },
    # Tailscale: Addled manages an existing install — status, login, serve,
    # funnel. It never installs Tailscale itself.
    "tailscale": {
        "enabled": False,
        "hostname": "",
        # Keep the dashboard shared to the user's own tailnet, and set that up
        # automatically once Tailscale is installed and signed in. This can only
        # ever reach their tailnet: it refuses without a password, and Funnel is
        # never covered by it.
        "auto_share": True,
        "serve_port": 443,
        "funnel": False,
        "poll_seconds": 20,
        # How to install Tailscale when the user asks: "auto" uses winget when it
        # is available and the official download otherwise.
        "install_method": "auto",
        # Optional pre-auth key for a headless node. Never returned to the UI.
        "auth_key": "",
    },
    "voice": {
        "tts_engine": "edge",
        "tts_voice": "en-US-JennyNeural",
        "stt_engine": "sensevoice",
        "stt_model": "small",
        "mic_enabled": True,
        "wake_word": "hey addled",
        "wake_sensitivity": 0.7,
        "auto_tts": True,
        "language": "en",
        "vad_enabled": True,
        "vad_threshold": 0.5,
        "kokoro_voice": "af_heart",
    },
    "safety": {
        "file_access_mode": "workspace_only",
        "allowed_folders": [],
        "kill_switch_hotkey": "ctrl+shift+alt+k",
        "clipboard_filter": True,
        "prompt_guard": True,
        "egress_guard": True,
        "privacy_excluded_apps": [],
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "meeting_auto_sleep": True,
        "gaming_auto_sleep": True,
        # Free the local model's RAM while the user is gaming. Separate from
        # `gaming_auto_sleep` on purpose: that one only stops the agent
        # chattering, and a user may reasonably want the quiet without giving up
        # the model, or the RAM without silencing Addled. Tying them together
        # would make one look like it had broken the other.
        "pause_local_model_on_gaming": True,
        "away_timeout_minutes": 5,
        # Names the user has granted standing permission to, so a skill or tool
        # that normally asks does not ask again. Answered once from the approval
        # card ("Always allow") or from the switch on the skill/tool card, and
        # revoked the same way. Names the destruction gate owns are refused by
        # `backend/approvals/policy.py` rather than listed here — a destructive
        # shell command and `delete_file` ask every time, by design, and no
        # amount of clicking can put them in this list.
        "always_allow": {"skills": [], "tools": []},
    },
    "observation": {
        "light_interval_s": 5,
        "medium_cycles": 3,
        "deep_vision": True,
        "deep_interval_s": 300,
        "monitors": "all",
        "privacy_zones": [],
        "snapshot_store_enabled": True,
        "snapshot_max": 30,
        "snapshot_min_interval_s": 60,
        "snapshot_max_age_h": 24,
        "voice_insights": False,
    },
    "vision": {
        "fallback_enabled": True,
        "hf_model": "microsoft/Florence-2-base",
    },
    "notifications": {
        "bubble_duration_s": 8,
        "max_bubbles": 3,
        "show_character_state": True,
    },
    "integrations": {
        "calendar_provider": None,
        "email_imap": None,
        "email_smtp": None,
        "email_address": "",
        "email_password": "",
        "imap_server": "",
        "imap_port": 993,
        "smtp_server": "",
        "smtp_port": 587,
        "google_client_id": "",
        "google_client_secret": "",
        "google_tokens": None,
        "rss_feeds": [],
    },
}


# ---- config singleton --------------------------------------------------------

@dataclass
class _Config:
    _data: dict = field(default_factory=dict)
    _dirty: bool = False

    def _ensure_loaded(self):
        if not self._data:
            self.load()

    def load(self):
        """Load settings from disk, or initialize defaults."""
        source = settings_path()
        if source.exists():
            try:
                with open(source, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Deep merge with defaults to fill any missing keys
                self._data = _deep_merge(DEFAULT_SETTINGS, loaded)
            except (json.JSONDecodeError, OSError):
                self._data = dict(DEFAULT_SETTINGS)
        else:
            self._data = dict(DEFAULT_SETTINGS)
            self.save()
        self._migrate()

    def _migrate(self) -> None:
        """Repair a stored value that an earlier default left harmful.

        The deep merge cannot do this on its own: a value already on disk always
        wins, so a bad shipped default would survive in every install that
        predates the fix. Only the exact old default is replaced, so a value the
        user chose is left alone.
        """
        if self._data.get("memory", {}).get("min_similarity") == 0.05:
            self.set("memory", "min_similarity",
                     value=DEFAULT_SETTINGS["memory"]["min_similarity"])

        llm = self._data.get("local_llm")
        providers = self._data.get("providers")
        builtin = providers.get("builtin") if isinstance(providers, dict) else None
        local = builtin.get("local") if isinstance(builtin, dict) else None
        if (isinstance(llm, dict)
                and llm.get("model_file") == "Qwen3-4B-Instruct-2507-Q3_K_M.gguf"):
            new_llm = DEFAULT_SETTINGS["local_llm"]
            for key in ("model_repo", "model_file", "model_url", "model_sha256",
                        "model_name", "size_mb"):
                llm[key] = new_llm[key]
            if isinstance(local, dict):
                new_local = DEFAULT_SETTINGS["providers"]["builtin"]["local"]
                local["default_model"] = new_local["default_model"]
                local["models"] = list(new_local["models"])
            self.save()

        # A Smithery key stored as a bearer header: the market used to ask for it
        # that way, and their gateway refuses `Authorization: Bearer <api key>`
        # - it reads the key from the `api_key` query parameter - so every server
        # added then answers "401 invalid token" while holding a perfectly good
        # key. The value only moves, and the header copy is left alone because
        # another server may legitimately hold a bearer of its own.
        stored = self._data.get("mcp", {}).get("credentials")
        if isinstance(stored, dict):
            headers = stored.get("headers")
            bearer = (str((headers or {}).get("Authorization") or "").strip()
                      if isinstance(headers, dict) else "")
            params = stored.get("params")
            params = dict(params) if isinstance(params, dict) else {}
            if bearer.lower().startswith("bearer ") and not params.get("api_key"):
                params["api_key"] = bearer[len("bearer "):].strip()
                self.set("mcp", "credentials",
                         value={**stored, "params": params})

    def save(self):
        """Persist settings to disk, atomically.

        Written to a temporary file and then moved into place, because the
        previous version opened the real path and wrote into it. A write that
        is interrupted — the process killed, the machine losing power, or the
        NSIS updater replacing files as the app closes — left `settings.json`
        truncated. The next launch then read a partial file and fell back to
        defaults, which is experienced as "my provider changed, my API keys are
        gone", with nothing in the logs to say why because the file that would
        have explained it is the one that was lost.

        `os.replace` is atomic on Windows and POSIX: a reader sees either the
        old file or the new one, never half of either.

        The path is re-read from the module each time rather than captured, so
        `use_settings_file()` can redirect this process away from the real
        settings.
        """
        target = settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as e:
            log.warning("could not write settings: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        # A copy of the settings that were replaced, so a bad save is
        # recoverable by hand rather than being simply gone.
        try:
            if target.exists():
                shutil.copy2(target, target.with_suffix(".json.bak"))
        except OSError:
            pass
        self._dirty = False

    def get(self, *keys: str, default=None):
        """Deep get: config.get('character', 'shape') → 'triangle'."""
        self._ensure_loaded()
        node = self._data
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return default
        return node

    def set(self, *keys: str, value):
        """Deep set: config.set('character', 'shape', 'circle'). Auto-saves."""
        self._ensure_loaded()
        node = self._data
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        self._dirty = True
        self.save()

    # ---- convenience properties ------------------------------------------------

    @property
    def is_first_run(self) -> bool:
        return not self.get("first_run_complete", default=False)

    @property
    def agent_name(self) -> str:
        return self.get("agent_name", default="Addled")

    @agent_name.setter
    def agent_name(self, value: str):
        self.set("agent_name", value)

    @property
    def active_provider(self) -> str:
        """Provider used at runtime — honours the smart-default policy.

        Falls back (local → OpenRouter) when the explicit choice cannot run yet,
        e.g. the local model has not been downloaded or an API key is missing.
        """
        try:
            from backend.providers.selector import resolve_default_provider
            return resolve_default_provider()
        except Exception:
            return self.get("providers", "active", default="local")

    @property
    def selected_provider(self) -> str:
        """The user's explicit choice in Settings (may be unusable right now)."""
        return self.get("providers", "active", default="local")

    @active_provider.setter
    def active_provider(self, value: str):
        self.set("providers", "active", value)

    def provider_config(self, provider_id: str) -> dict:
        """Get merged config for a provider (built-in or custom)."""
        builtin = self.get("providers", "builtin", default={})
        if provider_id in builtin:
            return dict(builtin[provider_id])
        custom = self.get("providers", "custom", default=[])
        for c in custom:
            if c.get("id") == provider_id:
                return dict(c)
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Returns new dict.

    Corruption guard: if a saved section is not a dict (e.g. a boolean
    written by an old bug) but the default is, keep the defaults.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict):
            if isinstance(value, dict):
                result[key] = _deep_merge(result[key], value)
            # else: non-dict override for a dict section → ignore, keep defaults
        else:
            result[key] = value
    return result


# ---- module-level singleton --------------------------------------------------

config = _Config()
