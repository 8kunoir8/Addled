"""
Tailscale integration.

Addled manages an existing Tailscale install — status, sign-in, and the
`tailscale serve` mapping that makes the dashboard reachable from the tailnet.
It does not install Tailscale itself.
"""

from backend.tailscale import manager  # noqa: F401
