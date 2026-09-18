"""
Tailscale management.

Addled manages an **existing** Tailscale install: it reads status, starts a
login, and configures `tailscale serve` so the dashboard is reachable from the
tailnet. It deliberately does not install Tailscale — that is a system-level
change involving UAC and a background service, and doing it silently would be a
worse outcome than telling the user to run the official installer.

Two things shape the whole module:

* **Tailscale may not be installed.** That is the normal case on a fresh
  machine, so `status()` reports `installed: False` with a blocker rather than
  raising, and every action returns a readable error instead of a traceback.
* **The CLI's own output is the truth.** `serve` syntax has changed across
  versions, so failures report the exact command and its stderr. An earlier
  round of work in this project lost a day to error bodies being thrown away;
  not repeating that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

log = logging.getLogger("addled.tailscale")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEFAULT_TIMEOUT = 20
LOGIN_TIMEOUT = 300

DOWNLOAD_URL = "https://tailscale.com/download/windows"
HTTPS_DOCS = "https://login.tailscale.com/admin/dns"

# Candidate locations, checked in order. PATH first so a user-managed install
# wins, then the standard installer locations.
COMMON_PATHS = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
]


def _cfg(key: str, default=None):
    try:
        from backend.config import config
        return config.get("tailscale", key, default=default)
    except Exception:
        return default


def cli() -> list[str] | None:
    """The argv prefix for the Tailscale CLI, or None if there isn't one.

    `ADDLED_TAILSCALE_EXE` overrides the lookup, either as a path or as a JSON
    argv array. That covers a non-standard install location, and it is what
    lets the test suite drive this module against a stand-in CLI.
    """
    override = (os.environ.get("ADDLED_TAILSCALE_EXE") or "").strip()
    if override:
        if override.startswith("["):
            try:
                parsed = json.loads(override)
                if isinstance(parsed, list) and parsed:
                    return [str(x) for x in parsed]
            except json.JSONDecodeError:
                log.warning("ADDLED_TAILSCALE_EXE is not valid JSON; ignoring it")
        else:
            candidate = Path(override)
            if candidate.is_file():
                return [str(candidate)]

    found = shutil.which("tailscale")
    if found:
        return [found]
    for path in COMMON_PATHS:
        try:
            if Path(path).is_file():
                return [path]
        except OSError:
            continue
    return None


def installed() -> bool:
    return cli() is not None


async def run(args: list[str], timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Run the CLI. Never raises; always reports what happened.

    `stderr` is included in every failure path on purpose — a bare exit code
    tells the user nothing about why `serve` refused.
    """
    argv = cli()
    if argv is None:
        return {"success": False, "code": None, "stdout": "", "stderr": "",
                "error": "Tailscale is not installed."}
    command = argv + [str(a) for a in args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, ValueError) as e:
        return {"success": False, "code": None, "stdout": "", "stderr": "",
                "error": f"Could not run {command[0]}: {e}",
                "command": " ".join(command)}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"success": False, "code": None, "stdout": "", "stderr": "",
                "error": f"`{args[0]}` timed out after {int(timeout)}s",
                "command": " ".join(command)}
    text_out = out.decode("utf-8", errors="replace")
    text_err = err.decode("utf-8", errors="replace")
    result = {
        "success": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": text_out,
        "stderr": text_err,
        "command": " ".join(command),
    }
    if proc.returncode != 0:
        detail = (text_err or text_out).strip().splitlines()
        result["error"] = (detail[-1] if detail else "") or \
            f"`{' '.join(args)}` exited with {proc.returncode}"
    return result


# -- parsing ------------------------------------------------------------------
# Pure functions, so they can be tested against fixtures without a real
# Tailscale anywhere near the machine.


def parse_status(raw: dict) -> dict:
    """Turn `tailscale status --json` into the fields the dashboard shows."""
    raw = raw if isinstance(raw, dict) else {}
    self_node = raw.get("Self") or {}
    peers = raw.get("Peer") or {}
    tailnet = raw.get("CurrentTailnet") or {}

    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or raw.get("TailscaleIPs") or []
    online_peers = sum(1 for p in peers.values()
                       if isinstance(p, dict) and p.get("Online"))

    backend = str(raw.get("BackendState") or "") or "NoState"
    return {
        "backend_state": backend,
        "running": backend == "Running",
        "logged_in": backend in ("Running", "Starting"),
        "needs_login": backend == "NeedsLogin",
        "stopped": backend in ("Stopped", "NoState"),
        "dns_name": dns_name,
        "host_name": str(self_node.get("HostName") or ""),
        "ips": [str(ip) for ip in ips],
        "online": bool(self_node.get("Online")),
        "tailnet": str(tailnet.get("Name") or raw.get("MagicDNSSuffix") or ""),
        "magic_dns_suffix": str(raw.get("MagicDNSSuffix")
                               or tailnet.get("MagicDNSSuffix") or "").rstrip("."),
        "peer_count": len(peers),
        "peers_online": online_peers,
        "has_ipv4": bool(raw.get("TUN")),
    }


def parse_serve(raw, port: int | None = None) -> dict:
    """Work out whether our gateway is the thing being served.

    `tailscale serve status` has changed shape more than once, so this reads
    defensively and keeps the raw payload: if the shape is unfamiliar the user
    and I can still see what the CLI actually said.
    """
    result = {"configured": False, "funnel": False, "url": "", "target": "",
              "hosts": [], "raw": raw}
    if isinstance(raw, str):
        # Plain-text fallback. Good enough to answer "is anything served?".
        text = raw.strip()
        result["configured"] = bool(text) and "http" in text.lower()
        result["raw"] = text
        if port:
            result["target_hit"] = str(port) in text
        return result
    if not isinstance(raw, dict):
        return result

    web = raw.get("Web") or {}
    allow_funnel = raw.get("AllowFunnel") or {}
    for host, spec in (web.items() if isinstance(web, dict) else []):
        handlers = (spec or {}).get("Handlers") or {}
        for path, handler in (handlers.items() if isinstance(handlers, dict) else []):
            proxy = str((handler or {}).get("Proxy") or "")
            entry = {"host": str(host), "path": str(path), "proxy": proxy,
                     "funnel": bool(allow_funnel.get(host))}
            result["hosts"].append(entry)
            if not result["target"]:
                result["target"] = proxy
            if not result["url"]:
                scheme = "https"
                result["url"] = f"{scheme}://{str(host).split(':')[0]}"
            if allow_funnel.get(host):
                result["funnel"] = True
            if port and str(port) in proxy:
                result["target_hit"] = True
    result["configured"] = bool(result["hosts"])
    return result


def login_url_from(text: str) -> str:
    """Pull the browser login URL out of `tailscale up` output."""
    for token in str(text or "").replace("\r", " ").split():
        cleaned = token.strip().strip(".,;\"'()")
        if cleaned.startswith("https://login.tailscale.com/"):
            return cleaned
    return ""


# -- manager ------------------------------------------------------------------


class TailscaleManager:
    """Owns the Tailscale CLI calls and the cached view of them.

    `status()` reads a cache; `refresh()` is what actually shells out. That
    keeps the dashboard's polling from spawning a process a second, which is
    what a naive implementation would do.
    """

    def __init__(self):
        self._snapshot: dict = {}
        self._checked_at = 0.0
        self._login_task: asyncio.Task | None = None
        self._login_url = ""
        self._login_state = "idle"   # idle | starting | waiting | done | failed
        self._login_error = ""
        self._watchdog: asyncio.Task | None = None

    # -- config

    def enabled(self) -> bool:
        return bool(_cfg("enabled", False))

    def serve_wanted(self) -> bool:
        return bool(_cfg("serve_enabled", False))

    def funnel_allowed(self) -> bool:
        return bool(_cfg("funnel", False))

    def serve_port(self) -> int:
        try:
            return int(_cfg("serve_port", 443) or 443)
        except (TypeError, ValueError):
            return 443

    def poll_seconds(self) -> float:
        try:
            return max(5.0, float(_cfg("poll_seconds", 20) or 20))
        except (TypeError, ValueError):
            return 20.0

    def _gateway_port(self) -> int:
        try:
            from backend.config import config
            return int(config.get("remote", "port", default=9878) or 9878)
        except Exception:
            return 9878

    def _broadcast(self, method: str, params: dict) -> None:
        try:
            from backend.ws_server import get_server
            get_server().broadcast_nowait(method, params)
        except Exception as e:  # noqa: BLE001
            log.debug("Could not broadcast %s: %s", method, e)

    # -- status

    async def refresh(self) -> dict:
        """Ask the CLI what is true, and cache it."""
        snapshot = {
            "installed": installed(),
            "cli": " ".join(cli() or []),
            "checked_at": time.time(),
            "version": "",
            "backend_state": "NoState",
            "running": False,
            "logged_in": False,
            "needs_login": False,
            "dns_name": "",
            "tailnet": "",
            "ips": [],
            "peers_online": 0,
            "peer_count": 0,
            "serve": {"configured": False, "funnel": False, "url": "", "hosts": []},
            "error": "",
            "blockers": [],
        }

        if not snapshot["installed"]:
            snapshot["error"] = "Tailscale is not installed."
            snapshot["blockers"] = [
                f"Tailscale is not installed on this machine. Install it from "
                f"{DOWNLOAD_URL}, then reopen this page."
            ]
            self._snapshot = snapshot
            self._checked_at = time.time()
            return snapshot

        version = await run(["version"])
        if version["success"]:
            first = (version["stdout"] or version["stderr"]).strip().splitlines()
            snapshot["version"] = (first[0].strip() if first else "")[:80]

        state = await run(["status", "--json"])
        if state["success"]:
            try:
                snapshot.update(parse_status(json.loads(state["stdout"])))
            except json.JSONDecodeError as e:
                snapshot["error"] = f"Could not read Tailscale's status: {e}"
        else:
            # `status --json` can exit non-zero while still printing valid JSON
            # (for example when logged out), so try the output anyway.
            parsed = False
            if state["stdout"].strip().startswith("{"):
                try:
                    snapshot.update(parse_status(json.loads(state["stdout"])))
                    parsed = True
                except json.JSONDecodeError:
                    parsed = False
            if not parsed:
                snapshot["error"] = state.get("error") or "Tailscale did not report a status."

        serve_raw = await run(["serve", "status", "--json"])
        if serve_raw["success"] and serve_raw["stdout"].strip().startswith("{"):
            try:
                snapshot["serve"] = parse_serve(json.loads(serve_raw["stdout"]),
                                                port=self._gateway_port())
            except json.JSONDecodeError:
                snapshot["serve"] = parse_serve(serve_raw["stdout"],
                                                port=self._gateway_port())
        else:
            text = await run(["serve", "status"])
            snapshot["serve"] = parse_serve(text.get("stdout") or "",
                                            port=self._gateway_port())

        snapshot["blockers"] = self._blockers(snapshot)
        self._snapshot = snapshot
        self._checked_at = time.time()
        return snapshot

    def _blockers(self, snapshot: dict) -> list[str]:
        """Plain-language reasons the remote URL is not up yet."""
        out = []
        if not self.enabled():
            out.append("Tailscale management is turned off in Settings.")
        if not snapshot.get("logged_in"):
            if snapshot.get("needs_login"):
                out.append("This machine is not signed in to Tailscale.")
            elif snapshot.get("stopped"):
                out.append("The Tailscale backend is stopped.")
            else:
                out.append("Tailscale is not running.")
        if snapshot.get("logged_in") and not snapshot["serve"].get("configured"):
            out.append("The dashboard is not shared to your tailnet yet "
                       "(no `tailscale serve` mapping).")
        elif snapshot.get("logged_in") and not snapshot["serve"].get("target_hit", True):
            out.append("Another address is being served, not Addled's gateway.")
        return out

    def status(self) -> dict:
        """The cached view, plus config. Cheap — safe to poll."""
        snapshot = dict(self._snapshot) if self._snapshot else {}
        snapshot.setdefault("installed", installed())
        snapshot.setdefault("backend_state", "NoState")
        snapshot.setdefault("serve", {"configured": False, "funnel": False,
                                      "url": "", "hosts": []})
        snapshot.setdefault("blockers", [])
        snapshot.setdefault("error", "")
        snapshot["enabled"] = self.enabled()
        snapshot["serve_wanted"] = self.serve_wanted()
        snapshot["funnel_wanted"] = self.funnel_allowed()
        snapshot["serve_port"] = self.serve_port()
        snapshot["gateway_port"] = self._gateway_port()
        snapshot["age_s"] = int(time.time() - self._checked_at) if self._checked_at else 0
        snapshot["login"] = {"state": self._login_state, "url": self._login_url,
                             "error": self._login_error}
        snapshot["installed"] = bool(snapshot.get("installed"))
        return snapshot

    def url(self) -> str:
        """The URL to hand to another device, if there is one."""
        serve = (self._snapshot or {}).get("serve") or {}
        if serve.get("configured") and serve.get("url"):
            return f"{serve['url']}{'' if serve['url'].endswith('/') else '/'}"
        return ""

    # -- login

    def login_running(self) -> bool:
        return self._login_task is not None and not self._login_task.done()

    async def login(self, auth_key: str = "") -> dict:
        """Start a login. Returns immediately; the URL arrives by broadcast.

        `tailscale up` blocks until the user finishes in a browser, which would
        hold a dashboard request open for minutes, so this runs in the
        background and reports progress on `tailscale.status` /
        `tailscale.loginUrl` instead.
        """
        if not installed():
            return {"success": False,
                    "error": f"Tailscale is not installed. Get it from {DOWNLOAD_URL}."}
        if self.login_running():
            return {"success": True, "started": False, "url": self._login_url,
                    "message": "A sign-in is already in progress."}

        key = str(auth_key or _cfg("auth_key", "") or "").strip()
        args = ["up"]
        hostname = str(_cfg("hostname", "") or "").strip()
        if hostname:
            args += ["--hostname", hostname]
        if key:
            args.append(f"--auth-key={key}")

        self._login_state = "starting"
        self._login_error = ""
        self._login_url = ""
        self._broadcast("tailscale.status", self.status())
        self._login_task = asyncio.create_task(self._run_login(args))
        return {"success": True, "started": True,
                "message": ("Signing in with the stored auth key."
                            if key else "Starting sign-in; a URL will appear here.")}

    async def _run_login(self, args: list[str]) -> None:
        """Drive `tailscale up`, surfacing the login URL as soon as it appears."""
        argv = (cli() or []) + args
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, ValueError) as e:
            self._login_state = "failed"
            self._login_error = f"Could not start Tailscale: {e}"
            self._broadcast("tailscale.status", self.status())
            return

        collected: list[str] = []
        self._login_state = "waiting"
        try:
            async def pump():
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    collected.append(line)
                    url = login_url_from(line)
                    if url and url != self._login_url:
                        self._login_url = url
                        log.info("Tailscale sign-in URL ready")
                        self._broadcast("tailscale.loginUrl", {"url": url})
                        self._broadcast("tailscale.status", self.status())

            await asyncio.wait_for(pump(), timeout=LOGIN_TIMEOUT)
            code = await proc.wait()
            if code == 0:
                self._login_state = "done"
                self._login_error = ""
            else:
                self._login_state = "failed"
                tail = " ".join(collected[-3:]) or "no output"
                self._login_error = f"`tailscale up` exited with {code}: {tail}"
        except asyncio.TimeoutError:
            self._login_state = "failed"
            self._login_error = (f"Sign-in did not finish within "
                                 f"{LOGIN_TIMEOUT // 60} minutes.")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        except Exception as e:  # noqa: BLE001
            self._login_state = "failed"
            self._login_error = str(e)
        finally:
            if self._login_state == "done":
                log.info("Tailscale signed in")
            await self.refresh()
            self._broadcast("tailscale.status", self.status())

    async def logout(self) -> dict:
        result = await run(["logout"])
        if not result["success"]:
            return {"success": False, "error": result.get("error")}
        await self.refresh()
        self._broadcast("tailscale.status", self.status())
        return {"success": True}

    async def down(self) -> dict:
        result = await run(["down"])
        if not result["success"]:
            return {"success": False, "error": result.get("error")}
        await self.refresh()
        self._broadcast("tailscale.status", self.status())
        return {"success": True}

    # -- serve

    async def _serve_command(self, funnel: bool) -> dict:
        """Configure the mapping, tolerating older CLI syntax.

        The `serve` surface has changed shape across releases, so the modern
        form is tried first and the older one after it. Both errors are
        reported, because "it did not work" is not actionable.
        """
        verb = "funnel" if funnel else "serve"
        port = self.serve_port()
        target = f"http://127.0.0.1:{self._gateway_port()}"
        attempts = [
            [verb, "--bg", f"--https={port}", target],
            [verb, "--bg", target],
            [verb, f"--https={port}", "/", target],
            [verb, target],
        ]
        problems = []
        for args in attempts:
            result = await run(args, timeout=45)
            if result["success"]:
                return {"success": True, "command": result["command"]}
            problems.append(f"`{' '.join(args)}` -> {result.get('error')}")
        detail = " | ".join(problems[:3])
        # HTTPS has to be switched on for the tailnet before `serve` will issue a
        # certificate, and the CLI's own wording for that is easy to miss. Say it
        # plainly and point at the page that fixes it.
        if "https" in detail.lower() or "cert" in detail.lower():
            detail += (f". Serving HTTPS needs to be enabled for your tailnet "
                       f"first: {HTTPS_DOCS}")
        return {"success": False,
                "error": "Tailscale rejected every form of the command. " + detail,
                "attempts": problems}

    async def enable_serve(self, funnel: bool = False) -> dict:
        """Share the gateway to the tailnet (or the internet, if funnel)."""
        if not installed():
            return {"success": False,
                    "error": f"Tailscale is not installed. Get it from {DOWNLOAD_URL}."}

        # The same rule the gateway enforces, checked again here: never publish
        # a dashboard that has no password.
        try:
            from backend.remote.auth import password_is_set
            if not password_is_set():
                return {"success": False,
                        "error": "Set a remote access password first. Addled will "
                                 "not share the dashboard without one."}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"Could not check the password: {e}"}

        if funnel and not self.funnel_allowed():
            return {"success": False,
                    "error": "Public access is turned off. Enable 'Publish to the "
                             "public internet' in Settings → Remote first."}

        await self.refresh()
        if not (self._snapshot or {}).get("logged_in"):
            return {"success": False,
                    "error": "Sign in to Tailscale first — there is no tailnet to "
                             "share to yet."}

        result = await self._serve_command(funnel=funnel)
        if not result["success"]:
            log.warning("Serving the gateway failed: %s", result.get("error"))
            return result

        serve_raw = await run(["serve", "status", "--json"])
        parsed = {}
        if serve_raw["success"] and serve_raw["stdout"].strip().startswith("{"):
            try:
                parsed = parse_serve(json.loads(serve_raw["stdout"]),
                                     port=self._gateway_port())
            except json.JSONDecodeError:
                parsed = {}
        if not parsed.get("configured"):
            # Some versions do not report `Web` for a funnel-only mapping.
            parsed = parse_serve(serve_raw.get("stdout") or "",
                                 port=self._gateway_port())

        await self.refresh()
        url = self.url()
        if not url:
            url = self._snapshot.get("serve", {}).get("url", "") if self._snapshot else ""
        if funnel:
            try:
                from backend.config import config
                config.set("tailscale", "funnel", value=True)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                from backend.config import config
                config.set("tailscale", "serve_enabled", value=True)
            except Exception:  # noqa: BLE001
                pass

        self._broadcast("tailscale.status", self.status())
        return {"success": True, "url": url,
                "message": (f"Shared to the public internet at {url}"
                            if funnel else f"Shared to your tailnet at {url}"),
                "command": result.get("command", "")}

    async def disable_serve(self, funnel: bool = False) -> dict:
        verb = "funnel" if funnel else "serve"
        attempts = [[verb, "--bg", "off"], [verb, "off"]]
        problems = []
        for args in attempts:
            result = await run(args, timeout=45)
            if result["success"]:
                try:
                    from backend.config import config
                    config.set("tailscale", "funnel" if funnel else "serve_enabled",
                               value=False)
                except Exception:  # noqa: BLE001
                    pass
                await self.refresh()
                self._broadcast("tailscale.status", self.status())
                return {"success": True}
            problems.append(f"`{' '.join(args)}` -> {result.get('error')}")
        return {"success": False,
                "error": "Could not turn it off. " + " | ".join(problems[:2])}

    # -- lifecycle

    async def apply_policy(self) -> None:
        """Make reality match the settings. Called after settings change."""
        await self.refresh()
        if not self.enabled() or not installed():
            return
        if not (self._snapshot or {}).get("logged_in"):
            return
        current = (self._snapshot or {}).get("serve") or {}

        # Funnel is deliberately NOT reconciled here, and this is the important
        # line in the file. `tailscale.funnel` records what is currently
        # published; it is not an instruction to publish. Treating it as one
        # meant that merely having the value set caused the dashboard to be put
        # on the public internet at startup, with no click — which is the one
        # thing this feature promises never happens. Public exposure is only
        # ever started by an explicit action on the Remote page.
        wanted = self.serve_wanted()
        if wanted and not current.get("configured"):
            await self.enable_serve(funnel=False)
        elif not wanted and current.get("configured"):
            await self.disable_serve(funnel=current.get("funnel", False))

        # Clear a leftover flag: nothing is published, so a recorded funnel state
        # is stale, and leaving it set is what makes the confusion above possible.
        if not current.get("configured") and self.funnel_allowed():
            try:
                from backend.config import config
                config.set("tailscale", "funnel", value=False)
                log.info("Cleared a stale Tailscale funnel flag (nothing published)")
            except Exception as e:  # noqa: BLE001
                log.debug("Could not clear the funnel flag: %s", e)

    async def boot(self) -> None:
        """Reconcile on startup. Never starts a login on its own."""
        try:
            await self.refresh()
            if self.enabled() and installed():
                await self.apply_policy()
            log.info("Tailscale: %s", self._snapshot.get("backend_state",
                                                          "not installed"))
        except Exception as e:  # noqa: BLE001
            log.debug("Tailscale boot skipped: %s", e)

    async def watchdog_loop(self) -> None:
        """Keep the cached status fresh, and push changes to the dashboard."""
        while True:
            try:
                before = json.dumps({
                    "s": (self._snapshot or {}).get("backend_state"),
                    "c": ((self._snapshot or {}).get("serve") or {}).get("configured"),
                    "u": self.url(),
                }, sort_keys=True)
                await self.refresh()
                after = json.dumps({
                    "s": (self._snapshot or {}).get("backend_state"),
                    "c": ((self._snapshot or {}).get("serve") or {}).get("configured"),
                    "u": self.url(),
                }, sort_keys=True)
                if before != after:
                    self._broadcast("tailscale.status", self.status())
            except Exception as e:  # noqa: BLE001
                log.debug("Tailscale watchdog: %s", e)
            await asyncio.sleep(self.poll_seconds())

    async def stop(self) -> None:
        self._watchdog = None
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            try:
                await self._login_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


tailscale = TailscaleManager()
