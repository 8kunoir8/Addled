"""
Remote access: an authenticated gateway in front of the dashboard.

The app's WebSocket API has no authentication of its own and never did — it was
written on the assumption that only this machine could reach it. Rather than
retrofit a credential onto 138 handlers, this package owns the whole remote
surface: it serves the dashboard, serves the login page, and only then bridges
an authenticated WebSocket through to the loopback server.

Tailscale is how it reaches other machines. Nothing here binds anything but
loopback.
"""

from backend.remote import auth  # noqa: F401
