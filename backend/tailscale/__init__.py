"""
Tailscale integration.

Addled manages an existing Tailscale install — status, sign-in, and the
`tailscale serve` mapping that makes the dashboard reachable from the tailnet —
and can run the official installer when the user asks it to.
"""

from backend.tailscale import installer, manager  # noqa: F401
