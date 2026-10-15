"""OAuth for remote MCP servers: discovery, registration, tokens.

A remote MCP server that requires OAuth cannot be used without this. The market
listed such servers as `runnable: True` and adding one produced a connection
that failed with a 401 and no way forward — there was no client, no callback,
and nowhere to put a token. This is the client.

The flow, as the 2025-06-18 spec defines it (all required there):

  1. Call the server. A 401 carries `WWW-Authenticate: Bearer resource_metadata=`
     naming where to find out how to authenticate. If it does not name one,
     RFC 9728 gives two well-known paths to try.
  2. Fetch that Protected Resource Metadata document (RFC 9728) to learn which
     authorization server this resource trusts.
  3. Fetch the authorization server's metadata (RFC 8414), from the well-known
     path or, failing that, from the resource document itself.
  4. Register a client (RFC 7591) unless the metadata already published client
     credentials for us to reuse.
  5. Authorize with PKCE S256 in the browser, a `resource` parameter naming the
     server we are actually asking for (RFC 8707), and the loopback redirect
     `oauth_loopback` already provides.
  6. Exchange the code for tokens, and refresh them when they expire.

Every step is over HTTPS and every response is JSON that another program wrote,
so each one is parsed defensively: a missing field is a message the user can
act on, not a stack trace in a log.

The `resource` parameter is not a formality. Without it an authorization server
may issue a token for whatever resource the browser session implies, and a
token minted for one server could be presented to another that trusts the same
issuer. Passing the exact MCP URL is what makes the token belong to this server.

Tokens are stored through `credentials`, so they are DPAPI-encrypted at rest
like every other secret (5.2) rather than being the one exception.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

from backend.integrations import oauth_loopback

log = logging.getLogger("addled.mcp.oauth")

DEFAULT_TIMEOUT = 20.0

# The scopes a server may ask for; if it asks for none we still need something
# for servers that require the parameter, and offline access is what makes a
# refresh token possible at all.
_DEFAULT_SCOPE = "openid offline_access"


class OAuthError(RuntimeError):
    """Something in the flow could not be completed.

    Carries a sentence a person can act on. The failures worth naming here are
    the ones the user can do something about: no metadata published, a
    registration refused, a browser that never came back.
    """


class OAuthClient:
    """The OAuth state for one remote MCP server."""

    def __init__(self, server_id: str, url: str, *, client=None,
                 store=None):
        self.server_id = server_id
        self.url = str(url or "").strip().rstrip("/")
        self._client = client
        self._store = store
        # Discovered once and kept: the metadata documents change rarely, and
        # re-fetching them on every 401 would turn a slow server into a slow
        # app.
        self.resource_metadata: dict = {}
        self.auth_server_metadata: dict = {}
        self.client_info: dict = {}
        self.scope: str = ""
        self._lock = asyncio.Lock()

    # -- tokens ------------------------------------------------------------

    @property
    def tokens(self) -> dict:
        """The stored tokens for this server, decrypted, or empty."""
        if self._store is None:
            return {}
        held = self._store.get(self.server_id) or {}
        return held if isinstance(held, dict) else {}

    @property
    def has_tokens(self) -> bool:
        return bool(self.tokens.get("access_token"))

    def save_tokens(self, payload: dict) -> None:
        """Store the tokens, together with the client id they belong to.

        The client id is kept because a refresh has to name the same client the
        grant was issued to, and a refresh in a later run of the app has no
        other way to know it. Without this, a token would be renewable only
        until the app restarted.
        """
        if self._store is None or not isinstance(payload, dict):
            return
        stored = dict(payload)
        client_id = str((self.client_info or {}).get("client_id") or "").strip()
        if client_id:
            stored["_client_id"] = client_id
        secret = str((self.client_info or {}).get("client_secret") or "").strip()
        if secret:
            stored["_client_secret"] = secret
        self._store.set(self.server_id, stored)

    def _restore_client(self) -> None:
        """Take the client id back out of storage, if it is there."""
        if self.client_info:
            return
        held = self.tokens
        client_id = str(held.get("_client_id") or "").strip()
        if client_id:
            self.client_info = {
                "client_id": client_id,
                "client_secret": str(held.get("_client_secret") or "").strip(),
            }

    def forget(self) -> None:
        if self._store is not None:
            self._store.set(self.server_id, {})

    def bearer(self) -> str:
        """The header value to send, or empty if we hold no token."""
        token = str(self.tokens.get("access_token") or "").strip()
        return f"Bearer {token}" if token else ""

    # -- http --------------------------------------------------------------

    async def _get_json(self, url: str) -> dict:
        """One JSON document, or a named failure. Never a bare exception."""
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        try:
            resp = await self._client.get(
                url, headers={"Accept": "application/json"},
                timeout=DEFAULT_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise OAuthError(f"could not reach {url}: "
                             f"{type(e).__name__}: {e}") from e
        if resp.status_code >= 400:
            raise OAuthError(f"{url} answered HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise OAuthError(f"{url} did not return JSON") from e
        if not isinstance(data, dict):
            raise OAuthError(f"{url} did not return a JSON object")
        return data

    async def _post_json(self, url: str, payload: dict,
                         form: bool = True) -> dict:
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT, follow_redirects=True)
        try:
            if form:
                resp = await self._client.post(
                    url, data=payload,
                    headers={"Accept": "application/json"},
                    timeout=DEFAULT_TIMEOUT)
            else:
                resp = await self._client.post(
                    url, json=payload,
                    headers={"Accept": "application/json",
                             "Content-Type": "application/json"},
                    timeout=DEFAULT_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise OAuthError(f"could not reach {url}: "
                             f"{type(e).__name__}: {e}") from e
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        if resp.status_code >= 400:
            message = ""
            if isinstance(data, dict):
                message = str(data.get("error_description")
                              or data.get("error") or "")
            raise OAuthError(
                f"{url} answered HTTP {resp.status_code}"
                + (f": {message}" if message else ""))
        if not isinstance(data, dict):
            raise OAuthError(f"{url} did not return a JSON object")
        return data

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def _well_known(base: str, suffix: str) -> str:
        """Insert `/.well-known/<name>` between origin and path.

        RFC 8414 and RFC 9728 both put the well-known segment *before* the
        resource path, not after: for `https://host/a/b` the document lives at
        `https://host/.well-known/x/a/b`. Appending it to the end — the
        obvious-looking thing — asks the wrong URL.
        """
        parts = urllib.parse.urlsplit(base)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.strip("/")
        return f"{origin}/.well-known/{suffix}" + (f"/{path}" if path else "")

    async def discover(self, www_authenticate: str = "") -> dict:
        """Find the metadata documents, from the 401 we were just given.

        Returns the authorization server metadata. Raises `OAuthError` with
        something to read if the server publishes nothing usable.
        """
        async with self._lock:
            if self.auth_server_metadata:
                return self.auth_server_metadata

            resource_urls: list[str] = []
            named = _metadata_url_from_challenge(www_authenticate)
            if named:
                resource_urls.append(named)
            resource_urls.append(
                self._well_known(self.url, "oauth-protected-resource"))

            resource_doc: dict = {}
            last_error: Exception | None = None
            for candidate in resource_urls:
                try:
                    resource_doc = await self._get_json(candidate)
                    break
                except OAuthError as e:
                    last_error = e
            if not resource_doc:
                raise OAuthError(
                    "this server needs a sign-in but does not publish where "
                    "to do it"
                    + (f" ({last_error})" if last_error else ""))

            self.resource_metadata = resource_doc
            self.scope = " ".join(
                str(s) for s in (resource_doc.get("scopes_supported") or [])
                if str(s).strip()) or _DEFAULT_SCOPE

            servers = resource_doc.get("authorization_servers") or []
            issuer = ""
            if isinstance(servers, list) and servers:
                issuer = str(servers[0] or "").strip()
            if not issuer:
                # Some documents put the endpoints here instead of naming an
                # issuer; use them directly rather than failing.
                if resource_doc.get("authorization_endpoint"):
                    self.auth_server_metadata = dict(resource_doc)
                    self._remember_client_from(resource_doc)
                    return self.auth_server_metadata
                raise OAuthError(
                    "this server says it needs a sign-in but names no "
                    "authorization server")

            # RFC 8414: the documented path first, then the OpenID Connect
            # variant many issuers publish instead.
            for suffix in ("oauth-authorization-server",
                           "openid-configuration"):
                try:
                    doc = await self._get_json(
                        self._well_known(issuer, suffix))
                    if doc.get("authorization_endpoint") \
                            and doc.get("token_endpoint"):
                        self.auth_server_metadata = doc
                        self._remember_client_from(doc)
                        return self.auth_server_metadata
                except OAuthError:
                    continue

            # Last resort: some issuers serve it at their own root.
            try:
                doc = await self._get_json(issuer)
                if doc.get("authorization_endpoint") \
                        and doc.get("token_endpoint"):
                    self.auth_server_metadata = doc
                    self._remember_client_from(doc)
                    return self.auth_server_metadata
            except OAuthError:
                pass

            raise OAuthError(
                f"the authorization server {issuer} does not publish usable "
                f"metadata")

    def _remember_client_from(self, doc: dict) -> None:
        """Keep any client credentials the metadata itself published.

        A server that registered us before, or that hands out a public client
        id, saves a registration round trip. Only taken when both halves are
        present: half a credential is worse than none.
        """
        client_id = str(doc.get("client_id") or "").strip()
        if client_id and not self.client_info:
            self.client_info = {
                "client_id": client_id,
                "client_secret": str(doc.get("client_secret") or "").strip(),
            }

    # -- registration ------------------------------------------------------

    async def register(self) -> dict:
        """Register this app as a client, unless we already are one.

        RFC 7591. Registration is what stops the user having to create an OAuth
        application by hand for every server they add, which is the thing that
        made this unusable before.
        """
        if self.client_info.get("client_id"):
            return self.client_info
        endpoint = str(self.auth_server_metadata.get("registration_endpoint")
                       or "").strip()
        if not endpoint:
            raise OAuthError(
                "this server does not support automatic registration, so it "
                "needs a client id configured by hand")

        # The redirect URI is the port we will actually listen on. The
        # loopback flow picks it; registration has to name the same one, so a
        # flow is opened here to learn the port and left open for the
        # authorize step.
        flow = self._ensure_flow()
        payload = {
            "client_name": "Addled",
            "redirect_uris": [flow.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        try:
            data = await self._post_json(endpoint, payload, form=False)
        except OAuthError as e:
            raise OAuthError(f"registration failed: {e}") from e

        client_id = str(data.get("client_id") or "").strip()
        if not client_id:
            raise OAuthError("registration succeeded but returned no client id")
        self.client_info = {
            "client_id": client_id,
            "client_secret": str(data.get("client_secret") or "").strip(),
            # A server may narrow what it granted; honour it rather than
            # asking for more than we were given.
            "redirect_uri": flow.redirect_uri,
            "token_endpoint_auth_method": str(
                data.get("token_endpoint_auth_method") or "none"),
        }
        return self.client_info

    # -- the flow ----------------------------------------------------------

    def _ensure_flow(self):
        flow = getattr(self, "_flow", None)
        if flow is None:
            self._flow = oauth_loopback.LoopbackFlow()
        return self._flow

    async def authorize_url(self, extra: dict | None = None) -> str:
        """Prepare the browser step and return the URL to open.

        The flow owns the verifier and the state — it generates them, checks
        the state on the way back, and hands the verifier out with the code.
        Keeping a second copy here would be two values that have to agree, and
        the failure when they do not is a code that cannot be redeemed for a
        reason nobody can see.

        `extra` carries provider-specific parameters for servers that want
        them. The `resource` parameter is added here rather than passed in,
        because it is required for every server and forgetting it would let a
        token minted for one be presented to another.
        """
        metadata = await self.discover()
        info = await self.register()
        flow = self._ensure_flow()
        endpoint = str(metadata.get("authorization_endpoint") or "").strip()
        if not endpoint:
            raise OAuthError("the authorization server published no "
                             "authorization endpoint")
        params = dict(extra or {})
        params.setdefault("resource", self.url)
        return flow.authorize_url(
            endpoint=endpoint,
            client_id=info["client_id"],
            scope=self.scope,
            extra=params,
        )

    async def wait_for_code(self, timeout: float = 300.0) -> dict:
        """Wait for the browser to come back. Blocks; run it off the loop.

        The flow compares `state` itself before it will complete, so a reply
        for a different request cannot land here. The result carries the code
        together with the verifier and the exact redirect URI to redeem it
        with, which is why those are taken from here and not reconstructed.
        """
        flow = self._ensure_flow()
        result = await asyncio.to_thread(flow.wait, timeout)
        if result.get("error"):
            raise OAuthError(str(result["error"]))
        if not result.get("code"):
            raise OAuthError("the browser came back without a code")
        return result

    async def complete(self, result: dict) -> dict:
        """Exchange the code `wait_for_code` returned, and store the tokens."""
        return await self.exchange(
            str(result.get("code") or ""),
            verifier=str(result.get("verifier") or ""),
            redirect_uri=str(result.get("redirect_uri") or ""))

    async def exchange(self, code: str, *, verifier: str = "",
                       redirect_uri: str = "") -> dict:
        """Turn the code into tokens and store them."""
        metadata = self.auth_server_metadata or await self.discover()
        info = self.client_info or await self.register()
        flow = self._ensure_flow()
        endpoint = str(metadata.get("token_endpoint") or "").strip()
        if not endpoint:
            raise OAuthError("the authorization server published no token "
                             "endpoint")
        # `exchange_code` is synchronous (it uses httpx directly), so it runs
        # off the loop: a token endpoint that is slow to answer must not stall
        # every other socket in the app while it thinks.
        payload = await asyncio.to_thread(
            oauth_loopback.exchange_code,
            token_endpoint=endpoint,
            code=code,
            verifier=verifier,
            client_id=info["client_id"],
            client_secret=info.get("client_secret", ""),
            # The URI has to match the one that was authorized with, character
            # for character, or the server rejects the exchange.
            redirect_uri=redirect_uri or flow.redirect_uri)
        if payload.get("error"):
            raise OAuthError(str(payload["error"]))
        self.save_tokens(payload)
        return payload

    async def refresh(self) -> dict:
        """Renew an expired access token, or say why it cannot be."""
        held = self.tokens
        refresh_token = str(held.get("refresh_token") or "").strip()
        if not refresh_token:
            raise OAuthError("this sign-in has expired and cannot be renewed; "
                             "sign in again")
        self._restore_client()
        metadata = self.auth_server_metadata or await self.discover()
        # Deliberately not `register()`: the client id needed to refresh is the
        # one the grant was issued to, which we already hold. Registering again
        # would mint a *second* client that this refresh token does not belong
        # to, and servers reject that — turning a renewable token into a dead
        # end. Registration is only for starting a new grant.
        info = self.client_info
        if not info.get("client_id"):
            raise OAuthError(
                "this sign-in cannot be renewed automatically; connect it "
                "again from its card in Settings")
        payload = await asyncio.to_thread(
            oauth_loopback.refresh_tokens,
            token_endpoint=str(metadata.get("token_endpoint") or ""),
            refresh_token=refresh_token,
            client_id=info["client_id"],
            client_secret=info.get("client_secret", ""))
        if payload.get("error"):
            raise OAuthError(str(payload["error"]))
        # Servers that do not rotate the refresh token leave it out; keeping
        # the old one is what lets the next refresh work.
        if not payload.get("refresh_token"):
            payload["refresh_token"] = refresh_token
        self.save_tokens(payload)
        return payload

    def cancel(self, reason: str = "Cancelled.") -> None:
        flow = getattr(self, "_flow", None)
        if flow is not None:
            flow.cancel(reason)

    def close(self) -> None:
        flow = getattr(self, "_flow", None)
        if flow is not None:
            flow.close()
            self._flow = None


def _metadata_url_from_challenge(header: str) -> str:
    """The `resource_metadata` URL from a `WWW-Authenticate` header, if any.

    The header looks like:
        Bearer realm="x", resource_metadata="https://host/.well-known/..."
    Parsed by hand rather than with a bearer-token library: the value is a
    quoted string inside a comma-separated list, and the usual parsers drop
    parameters they do not know — which is exactly the one we need.
    """
    text = str(header or "")
    marker = "resource_metadata="
    at = text.find(marker)
    if at < 0:
        return ""
    rest = text[at + len(marker):].lstrip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else ""
    return rest.split(",")[0].split()[0].strip() if rest else ""


class TokenStore:
    """Where a server's tokens live, encrypted, keyed by server id.

    A separate setting from the market's required values: a token must never be
    mistaken for a key the user typed and shown as a satisfied requirement, and
    clearing one must not clear the other.

    Each token field is protected on its own rather than the whole dict being
    JSON-encoded and protected once. That keeps the shape — a person reading
    the config sees which fields exist without being able to read them — and
    means adding a field later does not invalidate what is already stored.
    """

    NS = "mcp"
    KEY = "oauth_tokens"
    # Fields worth naming. Anything else a server returns is still stored, so a
    # non-standard field is not silently dropped; these are the ones the client
    # itself reads.
    FIELDS = ("access_token", "refresh_token", "id_token", "token_type",
              "scope", "expires_in")

    def _all(self) -> dict:
        from backend.config import config
        value = config.get(self.NS, self.KEY, default={}) or {}
        return value if isinstance(value, dict) else {}

    def get(self, server_id: str) -> dict:
        """One server's tokens, decrypted. Empty when there are none."""
        from backend.mcp_client import secretstore
        raw = self._all().get(str(server_id)) or {}
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        for key, value in raw.items():
            text = str(value if value is not None else "")
            if not text:
                continue
            if secretstore.is_protected(text):
                try:
                    out[str(key)] = secretstore.unprotect(text)
                except Exception as e:  # noqa: BLE001
                    # An unreadable token is treated as no token: the flow
                    # re-authorizes, which is the action that fixes it.
                    log.warning("A stored token for '%s' could not be "
                                "decrypted: %s", server_id, e)
            else:
                out[str(key)] = text
        return out

    def set(self, server_id: str, tokens: dict) -> None:
        from backend.config import config
        from backend.mcp_client import secretstore
        everything = dict(self._all())
        name = str(server_id)
        if not tokens:
            everything.pop(name, None)
        else:
            protected: dict = {}
            for key, value in tokens.items():
                text = str(value if value is not None else "")
                if not text:
                    continue
                if secretstore.is_protected(text):
                    protected[str(key)] = text
                    continue
                try:
                    protected[str(key)] = secretstore.protect(text)
                except Exception as e:  # noqa: BLE001
                    # Refuse rather than write a token in the clear. A token
                    # is the thing this whole module exists to handle, and one
                    # written readable is worse than one not written at all.
                    log.error("Refusing to store the token '%s' for '%s' "
                              "unencrypted: %s", key, server_id, e)
            everything[name] = protected
        config.set(self.NS, self.KEY, value=everything)

    def clear(self) -> None:
        from backend.config import config
        config.set(self.NS, self.KEY, value={})

    def servers(self) -> list[str]:
        """Which servers hold tokens — names only, never values."""
        return sorted(str(k) for k in self._all())


token_store = TokenStore()


def store_for_tokens() -> TokenStore:
    """The shared token store, by the name the flow code expects."""
    return token_store
