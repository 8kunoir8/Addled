"""
Minimal MCP (Model Context Protocol) client.

Addled ships its own client rather than the official ``mcp`` SDK: the bundled
Python has ``httpx`` and ``asyncio`` and nothing else is needed for the two
transports that matter (stdio and streamable HTTP), so no new dependency and no
repackaging of ``python-bundle``. The package is deliberately named
``mcp_client`` so it can never shadow the real ``mcp`` package if one is ever
installed — in that case ``protocol``/``stdio``/``http`` could be swapped for the
SDK behind the same :class:`~backend.mcp_client.manager.McpManager` API.
"""

from backend.mcp_client.manager import mcp_manager  # noqa: F401

__all__ = ["mcp_manager"]
