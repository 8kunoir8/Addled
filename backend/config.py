"""
Addled — portable JSON settings facade.

Reads/writes backend/memory/settings.json.
No registry, no system paths. Fully portable.

Default provider: DeepSeek (https://api.deepseek.com).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---- root resolution ---------------------------------------------------------

if getattr(sys, "frozen", False):
    _ROOT = Path(sys.executable).parent
else:
    _ROOT = Path(__file__).parent  # backend/

_MEMORY = _ROOT / "memory"
_MEMORY.mkdir(parents=True, exist_ok=True)

SETTINGS_PATH = _MEMORY / "settings.json"


# ---- default settings --------------------------------------------------------

DEFAULT_SETTINGS: dict = {
    "agent_name": "Addled",
    "version": 1,
    "first_run_complete": False,
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
    },
    "providers": {
        "active": "deepseek",
        "priority": ["deepseek", "claude", "openai", "copilot", "gemini", "ollama", "lmstudio"],
        "builtin": {
            "deepseek": {
                "name": "DeepSeek",
                "base_url": "https://api.deepseek.com",
                "api_key": "",
                "default_model": "deepseek-v4-pro",
                "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"],
                "vision": True,
                "vision_mode": "local",
                "vision_model": "deepseek-ai/deepseek-vl2-tiny",
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
                "default_model": "minicpm-v:8b",
                "models": ["minicpm-v:8b", "llama3.1:8b", "deepseek-r1:8b", "qwen2.5:7b"],
                "vision": True,
            },
            "lmstudio": {
                "name": "LM Studio (Local)",
                "base_url": "http://localhost:1234/v1",
                "api_key": "",
                "default_model": "local-model",
                "models": ["local-model"],
                "vision": False,
            },
        },
        "custom": [],
    },
    "chat": {
        "max_tokens": 4096,
        "temperature": 0.7,
        "system_prompt": (
            "You are Addled, a helpful AI desktop companion. "
            "You can see the user's screen, execute actions, and help proactively. "
            "Be concise, friendly, and practical."
        ),
        "context_messages": 20,
    },
    "voice": {
        "tts_engine": "edge",
        "tts_voice": "en-US-JennyNeural",
        "stt_engine": "sensevoice",
        "wake_word": "hey addled",
        "wake_sensitivity": 0.7,
        "auto_tts": True,
        "language": "en",
    },
    "safety": {
        "file_access_mode": "workspace_only",
        "allowed_folders": [],
        "kill_switch_hotkey": "ctrl+shift+alt+k",
        "clipboard_filter": True,
        "prompt_guard": True,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "meeting_auto_sleep": True,
        "gaming_auto_sleep": True,
        "away_timeout_minutes": 5,
    },
    "observation": {
        "light_interval_s": 5,
        "medium_cycles": 3,
        "deep_interval_s": 300,
        "monitors": "all",
        "privacy_zones": [],
    },
    "vision": {
        "fallback_enabled": True,
        "hf_model": "deepseek-ai/deepseek-vl2-tiny",
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
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Deep merge with defaults to fill any missing keys
                self._data = _deep_merge(DEFAULT_SETTINGS, loaded)
            except (json.JSONDecodeError, OSError):
                self._data = dict(DEFAULT_SETTINGS)
        else:
            self._data = dict(DEFAULT_SETTINGS)
            self.save()

    def save(self):
        """Persist settings to disk."""
        _MEMORY.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
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
        return self.get("providers", "active", default="deepseek")

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
    """Recursively merge override into base. Returns new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---- module-level singleton --------------------------------------------------

config = _Config()
