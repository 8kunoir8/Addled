"""
Start, stop and report on the Node bot bridges.

The bots are separate Node processes under ``bots/`` that connect back to this
WebSocket server. They were previously instructions on a dashboard page and
nothing more — and they could not have run anyway, because every one of them
required ``../shared/ws-client`` from inside ``bots/``, which resolves to
``<root>/shared`` and does not exist.

This manager only ever *reports* facts it can check (is node present, is the
script on disk, are its dependencies installed) so the page can say why a bot
will not start rather than showing a light that never turns green.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections import deque
from pathlib import Path

log = logging.getLogger("addled.bots")

# platform -> how to run it
PLATFORMS: dict[str, dict] = {
    "telegram": {
        "name": "Telegram Bot",
        "label": "Telegram",
        "script": "telegram-bot.js",
        "token_env": "TELEGRAM_BOT_TOKEN",
        "deps": ["grammy"],
        "icon": "✈️",
        "setup": ("1. Create a bot with @BotFather on Telegram\n"
                  "2. Paste the token below and press Start\n\n"
                  "Commands: /chat, /goal, /status, /screenshot, /sleep, /wake"),
    },
    "discord": {
        "name": "Discord Bot",
        "label": "Discord",
        "script": "discord-bot.js",
        "token_env": "DISCORD_BOT_TOKEN",
        "deps": ["discord.js"],
        "icon": "🎮",
        "setup": ("1. Create an app at discord.com/developers\n"
                  "2. Paste the bot token below and press Start\n"
                  "3. Invite it with the applications.commands scope\n\n"
                  "Slash commands: /chat, /goal, /status, /sleep, /wake"),
    },
    "whatsapp": {
        "name": "WhatsApp Bot",
        "label": "WhatsApp",
        "script": "whatsapp-bot.js",
        "token_env": "",            # QR-paired, no token
        "deps": ["@whiskeysockets/baileys", "@hapi/boom"],
        "icon": "💬",
        "setup": ("1. Press Start\n"
                  "2. Scan the QR code from the log below with WhatsApp\n"
                  "3. Keep the phone connected"),
    },
}

LOG_LINES = 40
_procs: dict[str, asyncio.subprocess.Process] = {}
_logs: dict[str, deque] = {p: deque(maxlen=LOG_LINES) for p in PLATFORMS}
_tasks: dict[str, asyncio.Task] = {}


def _root() -> Path | None:
    """The directory that contains ``bots/``.

    In a dev run that is the repo root. In a packaged build the app lives in
    app.asar, which a plain ``node`` process cannot read, so the scripts have to
    be unpacked next to it (see asarUnpack in electron-builder.yml).
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2],                                   # <root>/backend/bots/
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent)
    resources = Path(os.environ.get("ADDLED_RESOURCES", "") or ".")
    if resources != Path("."):
        candidates.append(resources / "app.asar.unpacked")
    for base in candidates:
        try:
            if (base / "bots").is_dir():
                return base
        except OSError:
            continue
    return None


def _node() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def tokens() -> dict:
    try:
        from backend.config import config
        return dict(config.get("bots", "tokens", default={}) or {})
    except Exception:
        return {}


def set_token(platform: str, token: str) -> bool:
    if platform not in PLATFORMS:
        return False
    try:
        from backend.config import config
        current = tokens()
        if token:
            current[platform] = token
        else:
            current.pop(platform, None)
        config.set("bots", "tokens", value=current)
        return True
    except Exception as e:
        log.debug("could not save the %s token: %s", platform, e)
        return False


def status() -> dict:
    """Everything the page needs to explain itself, checked not guessed."""
    root = _root()
    node = _node()
    saved = tokens()
    out = {}
    for platform, spec in PLATFORMS.items():
        script = (root / "bots" / spec["script"]) if root else None
        token = saved.get(platform, "")
        deps_present = None
        if root:
            modules = root / "node_modules"
            deps_present = all((modules / d).exists() for d in spec["deps"])
        proc = _procs.get(platform)
        running = bool(proc and proc.returncode is None)
        if not running:
            _procs.pop(platform, None)

        blockers = []
        if root is None:
            blockers.append("the bot scripts are not on disk — a packaged build "
                            "needs them unpacked from app.asar")
        if node is None:
            blockers.append("Node.js was not found on PATH")
        if deps_present is False:
            blockers.append("the bot's Node dependencies are not installed "
                            "(run npm install in the app folder)")
        if spec["token_env"] and not token:
            blockers.append(f"no {spec['token_env']} saved yet")

        out[platform] = {
            "id": platform,
            "name": spec["name"],
            "label": spec["label"],
            "icon": spec["icon"],
            "setup": spec["setup"],
            "token_env": spec["token_env"],
            "has_token": bool(token),
            "running": running,
            "pid": proc.pid if running and proc else None,
            "script_found": bool(script and script.is_file()),
            "script_path": str(script) if script else "",
            "deps_installed": deps_present,
            "node": node or "",
            "ready": not blockers,
            "blockers": blockers,
            "log": list(_logs[platform])[-LOG_LINES:],
        }
    return {"platforms": out, "node": node or "", "root": str(root) if root else ""}


async def _drain(platform: str, stream) -> None:
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                _logs[platform].append(text)
    except Exception:
        pass


async def start(platform: str) -> dict:
    if platform not in PLATFORMS:
        return {"success": False,
                "error": f"Unknown bot '{platform}'. Known: "
                         + ", ".join(PLATFORMS)}
    info = status()["platforms"][platform]
    if info["running"]:
        return {"success": True, "already": True, "pid": info["pid"]}
    if not info["ready"]:
        return {"success": False,
                "error": "Cannot start yet — " + "; ".join(info["blockers"])}

    spec = PLATFORMS[platform]
    root = _root()
    env = dict(os.environ)
    token = tokens().get(platform, "")
    if spec["token_env"] and token:
        env[spec["token_env"]] = token

    script = root / "bots" / spec["script"]
    try:
        proc = await asyncio.create_subprocess_exec(
            _node(), str(script),
            cwd=str(root), env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        return {"success": False, "error": f"Could not start it: {e}"}

    _procs[platform] = proc
    _logs[platform].append(f"[addled] started {script.name} (pid {proc.pid})")
    _tasks[platform] = asyncio.create_task(_drain(platform, proc.stdout))
    # Give it a moment so an immediate crash is reported rather than claimed as
    # a successful start.
    await asyncio.sleep(1.5)
    if proc.returncode is not None:
        tail = " | ".join(list(_logs[platform])[-3:]) or "no output"
        _procs.pop(platform, None)
        return {"success": False,
                "error": f"It exited immediately (code {proc.returncode}): {tail}"}
    return {"success": True, "pid": proc.pid}


async def stop(platform: str) -> dict:
    proc = _procs.get(platform)
    if not proc or proc.returncode is not None:
        _procs.pop(platform, None)
        return {"success": True, "already": True}
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception as e:
        return {"success": False, "error": str(e)}
    _procs.pop(platform, None)
    task = _tasks.pop(platform, None)
    if task:
        task.cancel()
    _logs[platform].append("[addled] stopped")
    return {"success": True}
