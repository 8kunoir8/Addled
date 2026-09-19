"""
Streamable-HTTP transport.

One POST per JSON-RPC message; the reply comes back either as a plain JSON body
or as a ``text/event-stream`` frame. Servers may hand out a session id in the
``Mcp-Session-Id`` response header, which must be echoed on later requests.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

from backend.mcp_client.protocol import (
    McpError,
    initialize_params,
    parse_sse,
)

log = logging.getLogger("addled.mcp.http")

DEFAULT_TIMEOUT = 30.0

# A failed request has to say what went wrong in words. The raw body is either a
# JSON object nobody reads at a glance or, as a 530 produced here, a Cloudflare
# HTML page whose first 200 characters are a doctype - which is what the server
# card ended up showing.
_WHAT_IT_MEANS = {
    401: "the server rejected the credential (HTTP 401) - replace it with "
         "Forget on the market card, or check the server wants that key",
    402: "this server bills per request and the charge was refused "
         "(HTTP 402 Payment required) - it cannot be used on this account",
    403: "the server refused the credential (HTTP 403)",
    404: "there is no server at this address any more (HTTP 404)",
    429: "the server is rate limiting requests (HTTP 429) - try again later",
}


def _body_note(body: str, already: str = "") -> str:
    """The useful part of an error body, or nothing.

    A JSON message is worth keeping; an HTML page is not - it is written for a
    browser, not for a program, and says nothing the status code did not.
    """
    text = " ".join(str(body or "").split())
    if not text or text.lstrip().startswith("<"):
        return ""
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return "" if message.lower() in already.lower() \
                    else f" - {message}"
        elif isinstance(error, str) and error.strip():
            description = str(payload.get("error_description") or "").strip()
            return (f" - {error.strip()}"
                    + (f" ({description})" if description else ""))
    return f" - {text[:120]}"


def _explain(status: int, body: str = "") -> str:
    """What a failed response means, for somebody who did not write the server.

    A status we can explain gets our sentence and nothing else: the server's own
    wording ("invalid_token") adds nothing to it. Anything else keeps whatever
    the body said, because then it is the only clue there is.
    """
    known = _WHAT_IT_MEANS.get(status)
    if known is not None:
        return known
    if status >= 500:
        return (f"the server's own host failed (HTTP {status}) - this is "
                "on their side, not a problem with the address or the key")
    return f"the server answered HTTP {status}{_body_note(body)}"


class McpHttpClient:
    """A remote MCP server reached over streamable HTTP."""

    def __init__(self, server_id: str, spec: dict,
                 timeout: float = DEFAULT_TIMEOUT):
        self.server_id = server_id
        self.spec = spec or {}
        self.timeout = float(spec.get("timeout_s") or timeout)
        self.url = str(spec.get("url") or "").strip()
        self.server_info: dict = {}
        self.protocol_version: str = ""
        self.notifications: list[dict] = []
        self._client = None
        self._session_id = ""
        self._counter = 0

    @property
    def alive(self) -> bool:
        return self._client is not None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> dict:
        if not self.url:
            raise McpError(-32602, "no url configured for this server")
        import httpx
        self._client = httpx.AsyncClient(timeout=self.timeout,
                                         follow_redirects=True)
        result = await self.request("initialize", initialize_params())
        self.protocol_version = str(result.get("protocolVersion") or "")
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        await self.notify("notifications/initialized")
        log.info("MCP server '%s' ready over HTTP: %s", self.server_id,
                 self.server_info.get("name", "?"))
        return result

    async def close(self, grace: float = 3.0) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    # -- io ----------------------------------------------------------------

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        extra = self.spec.get("headers") or {}
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _target(self) -> str:
        """The URL to POST to, with any configured query parameters.

        A credential whose destination is the query string cannot be a header:
        Smithery's gateway reads its key as `api_key` and refuses the same value
        as a bearer token. The parameter is appended per request rather than
        stored in `url`, so the key is not written into the spec's address and
        never appears in a card or a log line that prints one.
        """
        extra = self.spec.get("params") or {}
        if not isinstance(extra, dict) or not extra:
            return self.url
        query = urllib.parse.urlencode(
            {str(k): str(v) for k, v in extra.items() if str(v)})
        if not query:
            return self.url
        return f"{self.url}{'&' if '?' in self.url else '?'}{query}"

    async def _post(self, message: dict,
                    timeout: float | None = None) -> list[dict]:
        if self._client is None:
            raise McpError(-32000, "client is closed")
        limit = self.timeout if timeout is None else timeout
        try:
            resp = await self._client.post(
                self._target(), json=message, headers=self._headers(), timeout=limit)
        except Exception as e:
            raise McpError(-32000, f"request failed: {type(e).__name__}: {e}")

        session_id = (resp.headers.get("Mcp-Session-Id")
                      or resp.headers.get("mcp-session-id"))
        if session_id:
            self._session_id = session_id

        if resp.status_code == 202:
            return []
        if resp.status_code >= 400:
            raise McpError(-32000, _explain(resp.status_code, resp.text))
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            return parse_sse(resp.text)
        try:
            data = resp.json()
        except Exception:
            return []
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    # -- requests ----------------------------------------------------------

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> dict:
        self._counter += 1
        req_id = self._counter
        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        limit = self.timeout if timeout is None else timeout
        messages = await self._post(message, timeout=limit)

        for msg in messages:
            if "method" in msg and "id" not in msg:
                self.notifications.append(msg)
                continue
            if msg.get("id") != req_id or "method" in msg:
                continue
            error = msg.get("error")
            if error:
                raise McpError(int(error.get("code") or -32603),
                               str(error.get("message") or "server error"),
                               error.get("data"))
            result = msg.get("result")
            return result if isinstance(result, dict) else {"value": result}
        raise McpError(-32000, f"no response to '{method}'")

    async def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._post(message)
        except McpError as e:
            # Notifications are fire-and-forget; a server that rejects them
            # (or is already gone) must not fail the caller.
            log.debug("MCP '%s' notification %s failed: %s",
                      self.server_id, method, e)
