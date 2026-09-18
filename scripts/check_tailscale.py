"""Tailscale manager checks, driven against a stand-in CLI.

Tailscale is not installed on this machine (and should not need to be to run a
test suite), so the manager is pointed at a fake `tailscale` that mimics the
real one's argv surface and output, including the ways it fails. That covers the
logic that would otherwise only be reachable on a machine with a working
tailnet: parsing status, extracting the login URL, the two generations of
`serve` syntax, and every absence path.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_tailscale.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


# The stand-in CLI. Reads/writes a state file next to itself so `serve` and
# `status` agree with each other, and reads a `mode` file so the failure paths
# can be exercised.
FAKE_CLI = r'''
import json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
MODE = HERE / "mode"
HOST = "addled-host.tailnet-abc.ts.net"


def mode():
    try:
        return MODE.read_text().strip()
    except OSError:
        return "ok"


def state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"logged_in": True, "serve": None}


def save(data):
    STATE.write_text(json.dumps(data))


def out(text):
    sys.stdout.write(text + "\n")


def fail(text, code=1):
    sys.stderr.write(text + "\n")
    sys.exit(code)


args = sys.argv[1:]
m = mode()

if m == "fail":
    fail("failed to connect to local tailscaled; it doesn't appear to be running")

if not args:
    fail("no command")

if args[0] == "version":
    out("1.58.2")
    out("  tailscale commit: 1234abcd")
    sys.exit(0)

if args[0] == "status":
    st = state()
    payload = {
        "Version": "1.58.2",
        "TUN": True,
        "BackendState": "Running" if st["logged_in"] else "NeedsLogin",
        "TailscaleIPs": ["100.101.102.103"],
        "MagicDNSSuffix": "tailnet-abc.ts.net",
        "CurrentTailnet": {"Name": "me@example.com",
                           "MagicDNSSuffix": "tailnet-abc.ts.net"},
        "Self": {"HostName": "addled-host",
                 "DNSName": HOST + ".",
                 "Online": True,
                 "TailscaleIPs": ["100.101.102.103"]},
        "Peer": {
            "node1": {"HostName": "phone", "Online": True},
            "node2": {"HostName": "laptop", "Online": False},
        },
    }
    if m == "broken_json":
        out("{not json at all")
        sys.exit(0)
    out(json.dumps(payload))
    sys.exit(0)

if args[0] == "serve" and "status" in args:
    st = state()
    if not st.get("serve"):
        out("{}")
        sys.exit(0)
    target = st["serve"]["target"]
    port = st["serve"]["port"]
    payload = {
        "TCP": {str(port): {"HTTPS": True}},
        "Web": {f"{HOST}:{port}": {"Handlers": {"/": {"Proxy": target}}}},
        "AllowFunnel": {f"{HOST}:{port}": bool(st["serve"].get("funnel"))},
    }
    out(json.dumps(payload))
    sys.exit(0)

if args[0] == "serve" and any(a in ("off", "--off") or a.endswith("off") for a in args):
    st = state()
    st["serve"] = None
    save(st)
    out("serve disabled")
    sys.exit(0)

if args[0] in ("serve", "funnel"):
    st = state()
    # The stand-in rejects the modern spelling in "slow_fail" mode so the
    # legacy-syntax fallback is exercised.
    if m == "slow_fail" and any(a.startswith("--https=") for a in args):
        fail("flag provided but not defined: -https")
    target = ""
    port = 443
    for a in args:
        if a.startswith("http://"):
            target = a
        if a.startswith("--https="):
            port = int(a.split("=", 1)[1])
    if not target:
        fail("no proxy target given")
    st["serve"] = {"target": target, "port": port,
                   "funnel": args[0] == "funnel"}
    save(st)
    out(f"Available on the web at https://{HOST}/")
    sys.exit(0)

if args[0] == "up":
    if any(a.startswith("--auth-key=") for a in args):
        st = state()
        st["logged_in"] = True
        save(st)
        out("Success.")
        sys.exit(0)
    out("")
    out("To authenticate, visit:")
    out("")
    out(f"\thttps://login.tailscale.com/a/deadbeefcafe")
    out("")
    st = state()
    st["logged_in"] = True
    save(st)
    sys.exit(0)

if args[0] in ("logout", "down"):
    st = state()
    st["logged_in"] = False
    st["serve"] = None
    save(st)
    out("Done.")
    sys.exit(0)

fail("unknown command: " + args[0])
'''


def write_fake(tmp: Path, mode: str = "ok") -> str:
    (tmp / "tailscale.py").write_text(FAKE_CLI, encoding="utf-8")
    (tmp / "mode").write_text(mode, encoding="utf-8")
    (tmp / "state.json").write_text(json.dumps({"logged_in": True, "serve": None}),
                                    encoding="utf-8")
    return json.dumps([sys.executable, "-s", str(tmp / "tailscale.py")])


async def run():
    from backend.config import config
    from backend import tailscale as ts_pkg

    mod = ts_pkg.manager
    manager = mod.tailscale      # the singleton: what most of this drives
    config._ensure_loaded()
    original_ts = dict(config._data.get("tailscale") or {})
    original_remote = dict(config._data.get("remote") or {})
    saved_env = os.environ.get("ADDLED_TAILSCALE_EXE")

    tmp = Path(tempfile.mkdtemp())
    config._data["tailscale"] = {
        "enabled": True, "hostname": "", "serve_enabled": False,
        "serve_port": 443, "funnel": False, "poll_seconds": 20, "auth_key": "",
    }
    config._data["remote"] = {
        "enabled": True, "port": 9878, "password_hash": "",
        "session_hours": 12, "idle_timeout_minutes": 60, "max_sessions": 8,
        "allow_shell": False, "allow_desktop_input": False,
        "allow_funnel": False, "trusted_origins": [],
    }

    try:
        # ---- 1. not installed: the state of this machine right now --------
        os.environ.pop("ADDLED_TAILSCALE_EXE", None)
        real_cli = mod.cli
        mod.cli = lambda: None
        try:
            check("absence is detected", mod.installed() is False, "")
            status = await manager.refresh()
            check("refresh survives absence", status["installed"] is False, "")
            check("absence produces a blocker", bool(status["blockers"]), str(status))
            check("the blocker says where to get it",
                  any("tailscale.com" in b for b in status["blockers"]),
                  str(status["blockers"]))
            check("absence does not raise from status()",
                  isinstance(manager.status(), dict), "")
            out = await manager.login()
            check("sign-in without Tailscale returns an error, not a raise",
                  out.get("success") is False and "not installed" in out.get("error", ""),
                  str(out))
            out = await manager.enable_serve()
            check("sharing without Tailscale returns an error",
                  out.get("success") is False and "not installed" in out.get("error", ""),
                  str(out))
        finally:
            mod.cli = real_cli

        # ---- 2. a healthy install, driven through the fake CLI ------------
        os.environ["ADDLED_TAILSCALE_EXE"] = write_fake(tmp, "ok")
        check("the stand-in CLI is found", mod.installed() is True, "")

        status = await manager.refresh()
        check("the version is read", status["version"].startswith("1.58.2"),
              status["version"])
        check("the backend state is read", status["backend_state"] == "Running",
              status["backend_state"])
        check("a running node is logged in", status["logged_in"] is True, "")
        check("the DNS name is parsed without the trailing dot",
              status["dns_name"] == "addled-host.tailnet-abc.ts.net",
              status["dns_name"])
        check("the tailnet name is parsed",
              status["tailnet"] == "me@example.com", status["tailnet"])
        check("the IPs are parsed", status["ips"] == ["100.101.102.103"],
              str(status["ips"]))
        check("peer counts are parsed",
              status["peer_count"] == 2 and status["peers_online"] == 1,
              f"{status['peer_count']}/{status['peers_online']}")
        check("no serve mapping means no URL", manager.url() == "", manager.url())
        check("and it is listed as a blocker",
              any("not shared" in b for b in status["blockers"]),
              str(status["blockers"]))

        # ---- 3. sharing refuses without a password ------------------------
        from backend.remote import auth
        config._data["remote"]["password_hash"] = ""
        out = await manager.enable_serve()
        check("sharing refuses with no password set",
              out.get("success") is False and "password" in out.get("error", "").lower(),
              str(out))

        # ---- 4. sharing with a password -----------------------------------
        auth.set_password("a-good-enough-password")
        out = await manager.enable_serve()
        check("sharing succeeds", out.get("success") is True, str(out))
        check("sharing returns the URL",
              out.get("url", "").startswith("https://addled-host"),
              str(out.get("url")))
        status = await manager.refresh()
        check("serve status reports configured",
              status["serve"]["configured"] is True, str(status["serve"]))
        check("the serve target is our gateway",
              status["serve"].get("target_hit") is True,
              str(status["serve"]))
        check("the config records that sharing is on",
              config.get("tailscale", "serve_enabled") is True,
              str(config.get("tailscale", "serve_enabled")))
        check("no more serve blockers",
              not any("not shared" in b for b in status["blockers"]),
              str(status["blockers"]))

        # ---- 5. turning it off --------------------------------------------
        out = await manager.disable_serve()
        check("sharing can be turned off", out.get("success") is True, str(out))
        status = await manager.refresh()
        check("serve status reports nothing configured",
              status["serve"]["configured"] is False, str(status["serve"]))
        check("the config records that sharing is off",
              config.get("tailscale", "serve_enabled") is False, "")

        # ---- 6. older serve syntax -----------------------------------------
        # The stand-in rejects `--https=`, so the fallback has to carry it.
        (tmp / "mode").write_text("slow_fail", encoding="utf-8")
        out = await manager.enable_serve()
        check("an older serve syntax is tried when the modern one is rejected",
              out.get("success") is True,
              str(out.get("error", ""))[:300])
        check("the fallback still records the mapping",
              (await manager.refresh())["serve"]["configured"] is True, "")
        (tmp / "mode").write_text("ok", encoding="utf-8")

        # ---- 7. failures surface the CLI's own words -----------------------
        (tmp / "mode").write_text("fail", encoding="utf-8")
        result = await mod.run(["status", "--json"])
        check("a failing CLI is reported as a failure", result["success"] is False, "")
        check("the stderr text is surfaced, not just the exit code",
              "tailscaled" in result.get("error", ""), str(result.get("error")))
        check("the exact command is recorded",
              "tailscale.py" in result.get("command", ""), str(result.get("command")))
        status = await manager.refresh()
        check("refresh survives a failing CLI",
              isinstance(status, dict) and status["installed"] is True, "")
        check("a failing CLI is reported in status", bool(status.get("error")),
              "the error was swallowed")
        (tmp / "mode").write_text("ok", encoding="utf-8")

        # ---- 8. garbage JSON -------------------------------------------------
        (tmp / "mode").write_text("broken_json", encoding="utf-8")
        status = await manager.refresh()
        check("unparseable status JSON is reported, not raised",
              isinstance(status, dict) and bool(status.get("error")),
              f"error={status.get('error')!r}")
        (tmp / "mode").write_text("ok", encoding="utf-8")

        # ---- 9. signed out ---------------------------------------------------
        await mod.run(["logout"])
        status = await manager.refresh()
        check("a signed-out node is detected", status["needs_login"] is True,
              status["backend_state"])
        check("signed out is not running", status["logged_in"] is False, "")
        check("a signed-out node produces a blocker",
              any("signed in" in b for b in status["blockers"]),
              str(status["blockers"]))
        out = await manager.enable_serve()
        check("sharing is refused while signed out",
              out.get("success") is False and "Sign in" in out.get("error", ""),
              str(out))

        # ---- 10. the sign-in URL --------------------------------------------
        await manager.login()
        task = manager._login_task
        check("sign-in starts in the background", task is not None, "")
        if task is not None:
            await asyncio.wait_for(task, timeout=30)
        check("the sign-in URL is extracted from the CLI output",
              manager._login_url == "https://login.tailscale.com/a/deadbeefcafe",
              manager._login_url)
        check("sign-in reports success", manager._login_state == "done",
              f"{manager._login_state}: {manager._login_error}")
        status = await manager.refresh()
        check("after signing in the node is running",
              status["logged_in"] is True, status["backend_state"])

        # an auth key skips the browser
        await mod.run(["logout"])
        config._data["tailscale"]["auth_key"] = "tskey-auth-test"
        out = await manager.login()
        check("sign-in with an auth key is accepted", out.get("success") is True, str(out))
        if manager._login_task is not None:
            await asyncio.wait_for(manager._login_task, timeout=30)
        check("the auth key path finishes without a URL",
              manager._login_state == "done", f"{manager._login_state}")
        config._data["tailscale"]["auth_key"] = ""

        # ---- 11. funnel is held back until explicitly allowed ---------------
        config._data["tailscale"]["funnel"] = False
        out = await manager.enable_serve(funnel=True)
        check("public sharing is refused while the setting is off",
              out.get("success") is False and "Public" in out.get("error", ""),
              str(out))
        config._data["tailscale"]["funnel"] = True
        out = await manager.enable_serve(funnel=True)
        check("public sharing works once allowed", out.get("success") is True, str(out))
        status = await manager.refresh()
        check("funnel is reported as on", status["serve"]["funnel"] is True,
              str(status["serve"]))
        config._data["tailscale"]["funnel"] = False
        await manager.disable_serve(funnel=True)

        # ---- 12. the pure parsers -------------------------------------------
        parsed = mod.parse_status({
            "BackendState": "Running",
            "MagicDNSSuffix": "x.ts.net",
            "Self": {"DNSName": "host.x.ts.net.", "TailscaleIPs": ["100.1.1.1"],
                     "Online": True, "HostName": "host"},
            "Peer": {},
        })
        check("parse_status strips the trailing dot",
              parsed["dns_name"] == "host.x.ts.net", parsed["dns_name"])
        check("parse_status reports running", parsed["running"] is True, "")
        check("parse_status tolerates an empty payload",
              isinstance(mod.parse_status({}), dict), "")
        check("parse_status tolerates a non-dict",
              isinstance(mod.parse_status(None), dict), "")

        serve = mod.parse_serve(
            {"Web": {"h.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}},
             "AllowFunnel": {"h.ts.net:443": False}}, port=9878)
        check("parse_serve notices a mapping that is not ours",
              serve["configured"] is True and serve.get("target_hit") is None,
              str(serve))
        check("parse_serve tolerates text output",
              mod.parse_serve("https://h.ts.net/ -> http://127.0.0.1:9878")["configured"]
              is True, "")
        check("parse_serve tolerates an empty payload",
              mod.parse_serve({})["configured"] is False, "")
        check("parse_serve tolerates a non-dict",
              mod.parse_serve(None)["configured"] is False, "")

        check("a login URL is found in CLI noise",
              mod.login_url_from("To authenticate, visit:\n\thttps://login.tailscale.com/a/x1")
              == "https://login.tailscale.com/a/x1", "")
        check("no login URL is invented when there is none",
              mod.login_url_from("Success.") == "", "")

        # ---- 13. status() is cheap and never leaks the auth key ------------
        config._data["tailscale"]["auth_key"] = "tskey-auth-SECRET"
        blob = json.dumps(manager.status())
        check("status() never returns the auth key",
              "tskey-auth-SECRET" not in blob,
              "the auth key is in the status payload")
        config._data["tailscale"]["auth_key"] = ""

        # ---- 14. funnel is NEVER reconciled automatically ------------------
        # This was a real bug. `apply_policy` used to treat the funnel flag as an
        # instruction, so a leftover value published the dashboard to the public
        # internet at startup with no click — while allow_shell and
        # allow_desktop_input might be on.
        calls: list[tuple] = []
        saved_methods = (manager.refresh, manager.enable_serve, manager.disable_serve)
        # apply_policy refreshes first, so the snapshot has to come from here
        # rather than being assigned before the call.
        serve_state = {"configured": False}

        async def fake_refresh():
            manager._snapshot = {"logged_in": True,
                                 "serve": {"configured": serve_state["configured"]}}
            manager._checked_at = time.time()
            return manager._snapshot

        async def fake_enable(funnel: bool = False):
            calls.append(("enable", funnel))
            return {"success": True}

        async def fake_disable(funnel: bool = False):
            calls.append(("disable", funnel))
            return {"success": True}

        manager.refresh = fake_refresh
        manager.enable_serve = fake_enable
        manager.disable_serve = fake_disable
        try:
            config._data["tailscale"]["serve_enabled"] = False
            config._data["tailscale"]["funnel"] = True
            await manager.apply_policy()
            check("a leftover funnel flag publishes nothing",
                  calls == [], f"apply_policy started {calls}")
            check("and the stale flag is cleared",
                  config.get("tailscale", "funnel") is False,
                  str(config.get("tailscale", "funnel")))

            calls.clear()
            config._data["tailscale"]["serve_enabled"] = True
            await manager.apply_policy()
            check("serve_enabled does share, to the tailnet only",
                  calls == [("enable", False)], str(calls))

            calls.clear()
            config._data["tailscale"]["serve_enabled"] = False
            serve_state["configured"] = True
            await manager.apply_policy()
            check("turning the share off disables it",
                  calls and calls[0][0] == "disable", str(calls))
        finally:
            (manager.refresh, manager.enable_serve,
             manager.disable_serve) = saved_methods
            config._data["tailscale"]["serve_enabled"] = False
            config._data["tailscale"]["funnel"] = False
    finally:
        if saved_env is None:
            os.environ.pop("ADDLED_TAILSCALE_EXE", None)
        else:
            os.environ["ADDLED_TAILSCALE_EXE"] = saved_env
        config._data["tailscale"] = original_ts
        config._data["remote"] = original_remote
        from backend.remote import auth
        auth.sessions.reset()
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(run())
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
