"""Remote policy checks: the origin gate, secret redaction, and the one refusal gate.

The point of this suite is to prove that a remote connection *cannot* reach the
dangerous parts of the API, and that a local one is unaffected. Anything less
than a direct assertion here would be wishful thinking about a security
boundary, so this drives the real dispatcher with real connection objects.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_remote_policy.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


class FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers
        self.path = "/"
        self.method = "GET"


class FakeConn:
    """Enough of a websockets ServerConnection for the dispatcher."""

    def __init__(self, remote: bool = False, session: str = "abc123"):
        self.request = FakeRequest({})
        self.remote_address = ("127.0.0.1", 51000)
        self.sent: list[str] = []
        if remote:
            self.request.headers = {
                "X-Addled-Remote": "1",
                "X-Addled-Session": session,
                "X-Forwarded-For": "100.64.0.7",
                "Tailscale-User-Login": "me@example.com",
                "Tailscale-Node-Name": "laptop",
            }

    async def send(self, payload):
        self.sent.append(payload)


async def run():
    from backend.config import config
    from backend import ws_server
    from backend.remote import auth, policy

    config._ensure_loaded()
    original = dict(config._data.get("remote") or {})
    config._data["remote"] = {
        "enabled": True, "port": 9878, "password_hash": "x",
        "session_hours": 12, "idle_timeout_minutes": 60, "max_sessions": 8,
        "allow_shell": False, "allow_desktop_input": False,
        "allow_funnel": False, "trusted_origins": [],
    }

    try:
        # ---- 1. the remote context is read off the connection -------------
        local = FakeConn(remote=False)
        remote = FakeConn(remote=True)
        server = ws_server.WSServer()

        server._note_remote(local)
        server._note_remote(remote)
        check("a local connection is not marked remote",
              policy.is_remote(local) is False, "")
        check("a gateway connection is marked remote",
              policy.is_remote(remote) is True, "")
        check("the session id is captured",
              policy.session_id(remote) == "abc123", policy.session_id(remote))
        check("the tailscale identity is captured but is not the credential",
              policy.identity(remote) == "me@example.com", "")

        # ---- 2. the origin gate accepts only what it should ---------------
        patterns = policy.allowed_origins()
        check("no Origin is allowed (the Node bots send none)",
              None in patterns, "a non-browser client would be locked out")

        def allows(origin):
            for pattern in patterns:
                if pattern is None:
                    continue
                if hasattr(pattern, "match") and pattern.match(origin):
                    return True
                if pattern == origin:
                    return True
            return False

        for origin, expected in (
            ("https://addled.tailnet-1234.ts.net", True),
            ("https://host.tail9f.ts.net", True),
            ("http://127.0.0.1:3001", True),
            ("http://localhost:3000", True),
            ("https://evil.example", False),
            ("https://evil.example/ts.net", False),
            ("http://127.0.0.1.evil.com", False),
            ("https://ts.net.evil.com", False),
            ("null", False),
        ):
            check(f"Origin {origin} -> {'allowed' if expected else 'refused'}",
                  allows(origin) is expected, "")

        # ---- 3. redaction ------------------------------------------------
        sample = {
            "providers": {"builtin": {"deepseek": {"api_key": "sk-real-key",
                                                   "model": "deepseek-chat",
                                                   "max_tokens": 4096}}},
            "integrations": {"google_client_secret": "GOCSPX-secret",
                             "google_tokens": {"refresh_token": "1//refresh",
                                               "access_token": "ya29.x"},
                             "email_password": "hunter2"},
            "bots": {"tokens": {"telegram": "123:ABC"}},
            "remote": {"password_hash": "scrypt$1$2$3$aa$bb"},
            "tailscale": {"auth_key": "tskey-auth-xyz"},
            "chat": {"max_tokens": 2048, "system_prompt": "hi"},
            "wiki": {"enabled": True},
            "sop": {"enabled": True, "min_similarity": 0.35},
        }
        red = policy.redact_settings(sample)

        def dig(data, *keys):
            for key in keys:
                data = data[key]
            return data

        check("a nested api_key is redacted",
              dig(red, "providers", "builtin", "deepseek", "api_key") == policy.REDACTED,
              dig(red, "providers", "builtin", "deepseek", "api_key"))
        check("a *_secret is redacted",
              dig(red, "integrations", "google_client_secret") == policy.REDACTED, "")
        check("the google refresh token is redacted",
              dig(red, "integrations", "google_tokens", "refresh_token") == policy.REDACTED, "")
        check("the email password is redacted",
              dig(red, "integrations", "email_password") == policy.REDACTED, "")
        check("bot tokens are redacted but still list which exist",
              dig(red, "bots", "tokens", "telegram") == policy.REDACTED
              and "telegram" in dig(red, "bots", "tokens"), str(red["bots"]))
        check("the remote password hash is redacted",
              dig(red, "remote", "password_hash") == policy.REDACTED, "")
        check("the tailscale auth key is redacted",
              dig(red, "tailscale", "auth_key") == policy.REDACTED, "")

        # Non-secrets must survive, or the Settings page breaks.
        check("a provider model name is untouched",
              dig(red, "providers", "builtin", "deepseek", "model") == "deepseek-chat", "")
        check("chat.max_tokens is untouched",
              dig(red, "chat", "max_tokens") == 2048,
              "redacting max_tokens because it ends in 'tokens' would break settings")
        check("the system prompt is untouched",
              dig(red, "chat", "system_prompt") == "hi", "")
        check("booleans are untouched", dig(red, "wiki", "enabled") is True, "")
        check("floats are untouched", dig(red, "sop", "min_similarity") == 0.35, "")

        check("the original dict was not mutated",
              dig(sample, "providers", "builtin", "deepseek", "api_key") == "sk-real-key",
              "redaction modified the live config in place")

        # An empty value must stay empty, so "not configured" still reads as such.
        check("an unset secret stays empty, not '(hidden)'",
              policy.redact_settings({"api_key": ""})["api_key"] == "", "")

        # ---- 4. the refusal gate ------------------------------------------
        check("shell is refused for remote by default",
              policy.remote_refusal("action.execute", {"type": "run_command"}) is not None,
              "run_command was allowed remotely")
        for action in ("click", "type", "key_press", "double_click", "scroll"):
            check(f"input action '{action}' is refused for remote",
                  policy.remote_refusal("action.execute", {"type": action}) is not None, "")
        for method in ("mcp.add", "mcp.connect", "skills.installFrom", "forge.create"):
            check(f"'{method}' is refused for remote",
                  policy.remote_refusal(method, {}) is not None, "")

        # Harmless things must still work, or the dashboard is useless remotely.
        for method in ("settings.get", "memory.list", "chat.send", "wiki.list",
                       "sop.list", "project.status", "models.routes"):
            check(f"'{method}' is allowed for remote",
                  policy.remote_refusal(method, {}) is None,
                  policy.remote_refusal(method, {}))
        for action in ("screenshot", "read_file", "list_dir", "search_files"):
            check(f"action '{action}' is allowed for remote",
                  policy.remote_refusal("action.execute", {"type": action}) is None, "")

        # A non-shell action must not be caught by the shell rule.
        check("an unrelated action is not blocked by name collision",
              policy.remote_refusal("action.execute", {"type": "open_app"}) is None, "")

        # ---- 5. the flags actually open it --------------------------------
        config._data["remote"]["allow_shell"] = True
        check("allow_shell opens run_command",
              policy.remote_refusal("action.execute", {"type": "run_command"}) is None,
              "the setting has no effect")
        config._data["remote"]["allow_desktop_input"] = True
        check("allow_desktop_input opens input",
              policy.remote_refusal("action.execute", {"type": "type"}) is None, "")
        config._data["remote"]["allow_shell"] = False
        config._data["remote"]["allow_desktop_input"] = False

        # Even with everything open, the process-spawning methods stay shut.
        config._data["remote"]["allow_shell"] = True
        check("mcp.connect stays refused even with allow_shell",
              policy.remote_refusal("mcp.connect", {}) is not None, "")
        config._data["remote"]["allow_shell"] = False

        # ---- 6. the dispatcher enforces it --------------------------------
        server = ws_server.WSServer()
        calls: list[str] = []

        async def spy(params, ws):
            calls.append("called")
            return {"ok": True}

        server.register("action.execute", spy)
        server.register("settings.get", spy)
        server.register("mcp.connect", spy)

        # _dispatch *returns* its response; _handle_connection is what sends it.
        def error_of(response):
            return (response or {}).get("error") or {}

        def result_of(response):
            return (response or {}).get("result") or {}

        # A remote connection must be refused before the handler runs.
        remote = FakeConn(remote=True)
        server._note_remote(remote)
        response = await server._dispatch(
            {"id": 1, "method": "action.execute", "params": {"type": "run_command"}},
            remote)
        check("the dispatcher refuses remote run_command",
              error_of(response).get("code") == -32001, str(response))
        check("the refusal tells the user which setting to change",
              "Allow remote" in str(error_of(response).get("message") or "")
              or "Settings" in str(error_of(response).get("message") or ""),
              str(error_of(response).get("message")))
        check("and the handler never ran", calls == [], f"handler ran: {calls}")

        response = await server._dispatch(
            {"id": 2, "method": "mcp.connect", "params": {}}, remote)
        check("the dispatcher refuses remote mcp.connect",
              error_of(response).get("code") == -32001, str(response))
        check("and that handler never ran either", calls == [], str(calls))

        # A local connection must be entirely unaffected.
        local = FakeConn(remote=False)
        server._note_remote(local)
        response = await server._dispatch(
            {"id": 3, "method": "action.execute", "params": {"type": "run_command"}},
            local)
        check("the dispatcher allows local run_command",
              result_of(response).get("ok") is True, str(response))
        check("the local handler did run", calls == ["called"], str(calls))
        check("and no error was raised for local", not error_of(response), str(response))

        response = await server._dispatch(
            {"id": 4, "method": "mcp.connect", "params": {}}, local)
        check("local mcp.connect is still allowed",
              result_of(response).get("ok") is True, str(response))

        # ---- 7. redaction is wired into settings.get ----------------------
        source = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
        check("settings_get calls the redactor",
              "redact_settings" in source,
              "settings.get would return raw secrets to a remote browser")
        check("the redaction is conditional on the connection being remote",
              "if is_remote(ws):" in source, "")
        check("the socket handshake passes an origins gate",
              "origins=allowed_origins()" in source,
              "the cross-site hole this fixes would still be open")
    finally:
        config._data["remote"] = original
        auth.sessions.reset()


asyncio.run(run())
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
