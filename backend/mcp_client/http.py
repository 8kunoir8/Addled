"""
Streamable-HTTP transport.

One POST per JSON-RPC message; the reply comes back either as a plain JSON body
or as a ``text/event-stream`` frame. Servers may hand out a session id in the
``Mcp-Session-Id`` response header, which must be echoed on later requests.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse

from backend.mcp_client.protocol import (
    JsonRpcSession,
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


_VERSION_IN_TEXT = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _auth_hint(challenge: str) -> str:
    """What to tell a user whose 401 means "sign in first".

    Kept separate from the transport so the message is the same whether the
    refusal came from `start()` or from a tool call. A `WWW-Authenticate`
    naming an error we recognise gets a better sentence than one that does
    not; anything else still says what to do.
    """
    text = str(challenge or "")
    if "invalid_token" in text:
        return ("this server rejected the stored sign-in; connect it again "
                "from its card in Settings")
    if "insufficient_scope" in text:
        return ("the sign-in for this server does not grant enough access; "
                "connect it again from its card in Settings")
    return ("this server needs a sign-in before it can be used. "
            "Connect it from its card in Settings.")


def _wanted_version(body: str) -> str:
    """The protocol version a server's 400 named, if it named one.

    Servers say this in several ways — a JSON `error.message`, a plain string,
    an `MCP-Protocol-Version` hint in the body — so rather than parse each
    shape, look for the one thing that is always the same: a date-shaped
    version. It has to be a date, or a server complaining about anything else
    (a bad argument, a missing header) would be read as a version request and
    the retry would send a version we invented.
    """
    match = _VERSION_IN_TEXT.search(str(body or ""))
    return match.group(1) if match else ""


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
        # Routes incoming messages. It already knows how to answer a
        # server→client request and how to keep notifications; the HTTP client
        # used to do neither, because it scanned the response body for its own
        # id and stopped. Sharing the router means one implementation of that
        # rule rather than two that disagree.
        self._session = JsonRpcSession()
        # OAuth, when this server wants it. Built lazily on the first 401 so a
        # server that needs no sign-in pays nothing for the capability, and
        # created at most once per client so two calls racing on an expired
        # token start one flow rather than two.
        self._oauth = None
        # Why the last sign-in attempt failed, in words for a card to show.
        # Empty means "no sign-in has been tried or needed".
        self._auth_failed: str = ""
        self._client = None
        self._session_id = ""
        self._counter = 0

    @property
    def notifications(self) -> list[dict]:
        """Notifications the server has sent, newest last.

        Read from the router rather than stored here, so there is exactly one
        buffer and it is the same one that decides what a server→client request
        is. A second list beside it would drift the moment one path recorded
        something the other did not.
        """
        return self._session.notifications

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
        # The protocol asks the client to DELETE a session when it is done.
        # Without it the server keeps the session — and everything it holds for
        # it — until its own timeout, which on a long-lived app is a leak the
        # server pays for. Best-effort: closing must not fail because the
        # server is already gone, and it must not hang on a server that will
        # not answer, so it gets a short deadline of its own.
        client, self._client = self._client, None
        if client is not None and self._session_id:
            try:
                await client.delete(self._target(),
                                    headers=self._headers(),
                                    timeout=min(grace, 5.0))
            except Exception as e:  # noqa: BLE001
                log.debug("could not close MCP session %s: %s",
                          self._session_id, e)
            self._session_id = ""
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _reinitialize(self) -> None:
        """Start a fresh session after the server dropped ours.

        `initialize` is sent through `request`, which will not touch `_post`'s
        session-retry branch: the id is already cleared, so there is nothing to
        expire. A failure here is not swallowed — if we cannot re-initialize,
        the caller has to hear that rather than see the original 404.
        """
        self._session_id = ""
        result = await self.request("initialize", initialize_params())
        version = str(result.get("protocolVersion") or "")
        if version:
            self.protocol_version = version
        info = result.get("serverInfo")
        if isinstance(info, dict):
            self.server_info = info
        await self.notify("notifications/initialized")


    # -- oauth -------------------------------------------------------------

    def oauth(self):
        """The OAuth state for this server, built on first use."""
        if self._oauth is None:
            from backend.mcp_client.oauth import OAuthClient, token_store
            self._oauth = OAuthClient(self.server_id, self.url,
                                      store=token_store)
        return self._oauth

    @property
    def auth_required(self) -> bool:
        """Whether this server has told us it needs a sign-in."""
        return bool(self._auth_failed) and not self.oauth().has_tokens

    @property
    def auth_error(self) -> str:
        return self._auth_failed

    def authorize(self, timeout: float = 300.0) -> dict:
        """Discover, register, open a browser, and wait for the callback.

        Synchronous on purpose: it is called from a worker thread, because
        waiting for a person to finish in a browser is minutes, and doing that
        on the event loop would freeze every other socket in the app.
        """
        import asyncio

        async def run() -> dict:
            client = self.oauth()
            url = await client.authorize_url()
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception as e:  # noqa: BLE001
                log.debug("could not open a browser for MCP auth: %s", e)
            result = await client.wait_for_code(timeout)
            await client.complete(result)
            return {"success": True, "url": url}

        try:
            return asyncio.run(run())
        except Exception as e:  # noqa: BLE001
            # The message is what the card shows, so it is the exception's own
            # words rather than a wrapper — `OAuthError` is written to be read.
            self._auth_failed = str(e)
            return {"success": False, "error": str(e)}

    def forget_auth(self) -> None:
        if self._oauth is not None:
            self._oauth.forget()
        self._auth_failed = ""

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
        # An OAuth token, when this server wants one. Placed after the
        # configured headers so an explicit `Authorization` in the spec wins:
        # a server that takes a static key should keep taking it, and a user
        # who typed one should not have it silently replaced by a token.
        # Checked case-insensitively because a spec written by hand may spell
        # the header however it likes.
        if not any(k.lower() == "authorization" for k in headers):
            # Read through `oauth()` even when no 401 has arrived yet. A token
            # stored in a previous run must be *sent* on the first request of
            # this one; waiting for a 401 first would burn a round trip on
            # every reconnect and log a refusal that was never real.
            bearer = self.oauth().bearer()
            if bearer:
                headers["Authorization"] = bearer
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
                    timeout: float | None = None,
                    retry_version: bool = False,
                    retry_session: bool = False,
                    retry_auth: bool = False) -> list[dict]:
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

        # A version the server will not speak. The spec lets a server answer a
        # request carrying the wrong `MCP-Protocol-Version` with a 400 naming
        # the one it wants; retrying once with that version turns a dead
        # server into a working one, and is what every other client does.
        # `retry_version` is set only for the retry, so this cannot loop.
        if resp.status_code == 400 and not retry_version:
            wanted = _wanted_version(resp.text)
            if wanted and wanted != self.protocol_version:
                log.info("MCP server '%s' asked for protocol version %s "
                         "(we sent %s); retrying", self.server_id, wanted,
                         self.protocol_version or "none")
                self.protocol_version = wanted
                return await self._post(message, timeout=timeout,
                                        retry_version=True)

        # 401 means this server wants an OAuth token and either we have none
        # or the one we have was refused. `WWW-Authenticate` names where to
        # learn how to sign in; that header is the only thing that tells us
        # which authorization server this resource trusts, so it is passed on
        # rather than guessed at.
        #
        # Once per client, guarded by `auth_attempted`: a server that 401s
        # everything must not send the user to a browser on every tool call.
        # The retry after a successful authorization is the one case where a
        # second POST follows, and that is guarded by `retry_auth`.
        if resp.status_code == 401 and not retry_auth:
            challenge = (resp.headers.get("WWW-Authenticate")
                         or resp.headers.get("www-authenticate") or "")
            if not self._auth_failed:
                self._auth_failed = _auth_hint(challenge)
            if self._oauth is not None and self._oauth.has_tokens:
                # We held a token and it was refused: try to renew it once.
                try:
                    await self._oauth.refresh()
                    return await self._post(message, timeout=timeout,
                                            retry_auth=True)
                except Exception as e:  # noqa: BLE001
                    self._auth_failed = (
                        f"the sign-in for this server is no longer valid and "
                        f"could not be renewed ({e}); sign in again")
                    log.info("MCP server '%s' token refresh failed: %s",
                             self.server_id, e)
            raise McpError(-32000, self._auth_failed or (
                "this server needs a sign-in before it can be used. "
                "Connect it from its card in Settings."))

        # 404 on a request that carried a session id means the session is gone,
        # not that the address is. Servers expire them routinely. Treating it
        # as "there is no server at this address any more" told the user their
        # server had disappeared when it was running perfectly, and left the
        # connection permanently broken until they reconnected by hand.
        # Re-initialize and try once; the guard keeps a server that 404s
        # everything from looping.
        if (resp.status_code == 404 and self._session_id
                and not retry_session):
            log.info("MCP session %s on '%s' expired; reinitializing",
                     self._session_id, self.server_id)
            self._session_id = ""
            await self._reinitialize()
            return await self._post(message, timeout=timeout,
                                    retry_session=True)

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

        # Every message is routed, not just the reply we are waiting for. A
        # response body can carry more than one thing: the server may ask us
        # something (`roots/list`, `sampling/createMessage`) in the same batch,
        # and it may send notifications alongside the result. Scanning for our
        # id and returning discarded both — the request went unanswered, so the
        # server waited, and the notification was never seen.
        result: dict | None = None
        error: McpError | None = None
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            reply = self._session.feed(msg)
            if reply is not None:
                # The peer asked something we do not implement. Answering with
                # a proper error is the protocol's requirement: silence leaves
                # the server holding an entry open for a client that will never
                # write back.
                await self._reply(reply)
                continue
            if msg.get("id") != req_id or "method" in msg:
                continue
            if msg.get("error"):
                err = msg["error"]
                error = McpError(int(err.get("code") or -32603),
                                 str(err.get("message") or "server error"),
                                 err.get("data"))
                continue
            payload = msg.get("result")
            result = payload if isinstance(payload, dict) else {"value": payload}

        if error is not None:
            raise error
        if result is not None:
            return result
        raise McpError(-32000, f"no response to '{method}'")

    async def _reply(self, envelope: dict) -> None:
        """Send a JSON-RPC error back for a request we do not implement.

        Best-effort: the request we are waiting on must still be able to
        return, so a failure to deliver this cannot become the caller's error.
        """
        try:
            await self._post(envelope)
        except Exception as e:  # noqa: BLE001
            log.debug("could not answer a server request: %s", e)

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
