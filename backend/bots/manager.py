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
# The most recent pairing QR per platform, kept OUTSIDE the log ring.
#
# A QR is one very long line carrying the entire pairing payload. Putting it in
# a 40-line ring meant the dashboard's own "last 12 lines" window threw it away,
# so the code was emitted and never shown. It is state, not a log line — the
# newest one is the only one worth keeping.
_qr: dict[str, str] = {}
_tasks: dict[str, asyncio.Task] = {}

QR_PREFIX = "QR_DATA:"

# Outbound sends waiting for their bot to answer.
#
# The bot owns the WhatsApp socket, so the backend cannot send directly — it
# asks over the same WebSocket and waits for `bots.sendResult`. A request id
# rather than a bare future key: two sends can be in flight at once, and a
# result must be matched to the message it belongs to, not to whichever waiter
# happens to be first.
_send_waiters: dict[str, asyncio.Future] = {}
_send_counter = 0
# Long enough for a cold socket, short enough that a dead bot does not hold a
# chat turn open. A send that times out says so rather than failing silently.
SEND_TIMEOUT_S = 20.0


def _root() -> Path | None:
    """The directory that contains ``bots/``.

    Two layouts matter. A dev run keeps them in the repo root. A packaged build
    keeps them inside ``app.asar``, which an external ``node`` cannot read, so
    they are unpacked *beside* it — ``<resources>/app.asar.unpacked/bots`` — and
    that is also where their Node dependencies land. The build config does the
    unpacking; this has to look in both places or the packaged app reports the
    scripts as missing even when they are there.
    """
    here = Path(__file__).resolve()
    resources = here.parents[2]          # <repo> or <install>/resources
    candidates: list[Path] = [
        resources,
        resources / "app.asar.unpacked",
    ]
    if len(here.parents) > 3:
        parent = here.parents[3]
        candidates += [parent, parent / "app.asar.unpacked"]
    extra = os.environ.get("ADDLED_RESOURCES")
    if extra:
        candidates += [Path(extra), Path(extra) / "app.asar.unpacked"]

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
            # Empty unless the bot is waiting to be paired. The dashboard shows
            # it as a scannable code, which is the only way to link a phone.
            "qr": _qr.get(platform, ""),
        }
    return {"platforms": out, "node": node or "", "root": str(root) if root else ""}


async def _drain(platform: str, stream) -> None:
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            # A pairing QR is pulled out of the stream rather than left in it.
            # It is ~1500 characters on one line, which would push every other
            # log line out of a 40-line buffer, and the dashboard needs it as a
            # value to render — not as text to scroll past.
            if text.startswith(QR_PREFIX):
                payload = text[len(QR_PREFIX):].strip()
                if payload:
                    _qr[platform] = payload
                continue
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
    # Drop any QR from a previous run before the new process has said anything.
    # A stale code left on screen looks scannable and silently fails to pair.
    _qr.pop(platform, None)
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
        _qr.pop(platform, None)
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
    # A stopped bot cannot be paired, so its code must not stay on screen.
    _qr.pop(platform, None)
    _logs[platform].append("[addled] stopped")
    return {"success": True}

def resolve_send(request_id: str, result: dict) -> bool:
    """Hand a bot's answer to whoever is waiting for it.

    Called from the WebSocket handler when `bots.sendResult` arrives. Never
    raises: an unknown or already-settled id is a late answer to a request that
    timed out, which is not an error worth surfacing.
    """
    waiter = _send_waiters.pop(str(request_id or ""), None)
    if waiter is None or waiter.done():
        return False
    try:
        waiter.set_result(result or {})
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("could not settle send %s: %s", request_id, e)
        return False

async def send(platform: str, to: str, text: str) -> dict:
    """Ask a running bot to send a message, and wait for its answer.

    The bot owns the platform's connection, so this cannot be done from here —
    it is a request over the WebSocket the bot already holds. Every failure mode
    is named, because "nothing happened" is the hardest kind of bug to chase
    from the chat page:

      * unknown platform, or one that cannot send (it is not running)
      * an empty destination or body
      * the bot never answering, which means it is wedged or was closed
    """
    if platform not in PLATFORMS:
        return {"success": False,
                "error": f"Unknown bot '{platform}'. Known: "
                         + ", ".join(PLATFORMS)}
    body = str(text or "").strip()
    if not body:
        return {"success": False, "error": "The message text is empty."}
    destination = str(to or "").strip()
    if not destination:
        return {"success": False,
                "error": ("Give a destination — a phone number in international "
                          "form, e.g. 6281234567890.")}

    proc = _procs.get(platform)
    if not proc or proc.returncode is not None:
        return {"success": False,
                "error": f"The {platform} bot is not running. Start it first."}

    try:
        from backend.ws_server import get_server
        server = get_server()
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"no way to reach the bot: {e}"}
    if server is None:
        return {"success": False, "error": "the server is not running"}

    global _send_counter
    _send_counter += 1
    request_id = f"send_{_send_counter}"
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future = loop.create_future()
    _send_waiters[request_id] = waiter
    try:
        server.broadcast_nowait("bots.send", {
            "requestId": request_id,
            "platform": platform,
            "to": destination,
            "text": body,
        })
        result = await asyncio.wait_for(waiter, timeout=SEND_TIMEOUT_S)
    except asyncio.TimeoutError:
        _send_waiters.pop(request_id, None)
        return {"success": False,
                "error": (f"The {platform} bot did not answer within "
                          f"{int(SEND_TIMEOUT_S)}s. Check its log on the Bots "
                          f"page — it may need re-pairing.")}
    except Exception as e:  # noqa: BLE001
        _send_waiters.pop(request_id, None)
        return {"success": False, "error": str(e)}

    out = dict(result or {})
    out.setdefault("success", False)
    if not out.get("success") and not out.get("error"):
        out["error"] = "the bot refused the message without saying why"
    # Recorded either way. A send that failed is exactly the thing someone will
    # come back to ask about, and an unrecorded failure looks identical to a
    # message that was never attempted.
    try:
        from backend.memory import bot_history
        bot_history.record_outbound(platform, destination, body,
                                    bool(out.get("success")),
                                    str(out.get("error") or ""))
    except Exception as e:  # noqa: BLE001
        log.debug("could not record the outbound message: %s", e)
    return out
