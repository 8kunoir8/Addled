"""Where a chat turn came from, and how to label it.

A turn can arrive from the dashboard, a bot bridge, the floating character,
voice, a scheduled task or a swarm agent. All of them go through
`run_chat_pipeline`, so all of them can be *shown* in one conversation — which
is the point: a message sent from a phone should appear on the chat page beside
the ones typed there, and a reply the user reads on Telegram should be visible
in the app too.

Only the label lives here. The decision about *whether* to announce a turn is
the pipeline's, and the rendering is the dashboard's; this module exists so
those two do not each invent their own spelling of "whatsapp".
"""

from __future__ import annotations

# The fallback for a turn whose origin nobody declared. Deliberately a real
# entry rather than None: an unlabelled message should render plainly, not
# break the badge lookup or print "unknown".
DEFAULT = "addled"

# id -> (label, icon). The icons match the ones on Settings -> Bots, so a
# message is marked with the same symbol as the page that configured it.
SOURCES: dict[str, tuple[str, str]] = {
    "addled": ("Addled", "🤖"),
    "dashboard": ("this app", "💬"),
    "telegram": ("Telegram", "✈️"),
    "discord": ("Discord", "🎮"),
    "whatsapp": ("WhatsApp", "💬"),
    "voice": ("voice", "🎤"),
    "character": ("the character", "🎭"),
    "task": ("a scheduled task", "⏰"),
    "swarm": ("a swarm agent", "🐝"),
    "code": ("Code mode", "💻"),
    "remote": ("a remote session", "📡"),
    # An action the user approved. It runs after the turn that asked has ended,
    # so the outcome has to be announced like any other out-of-turn message.
    "approval": ("an approved action", "✅"),
}

# Sources that are the user talking *in this app*. A message from one of these
# is already on the chat page — the page drew it — so announcing it as well
# would show it twice. Kept as a set rather than a flag so the rule is visible
# in one place instead of implied by whichever caller happens to pass it.
LOCAL = frozenset({"dashboard"})

def normalise(source: object) -> str:
    """A known source id, or the default. Never raises, never returns junk."""
    name = str(source or "").strip().lower()
    return name if name in SOURCES else DEFAULT

def label(source: object) -> str:
    """Human wording, e.g. "Telegram" — for a tooltip or a log line."""
    return SOURCES[normalise(source)][0]

def icon(source: object) -> str:
    """The badge symbol for a source, e.g. "✈️"."""
    return SOURCES[normalise(source)][1]

def describe(source: object) -> dict:
    """Everything the dashboard needs to draw the badge, in one payload."""
    name = normalise(source)
    text, symbol = SOURCES[name]
    return {"source": name, "source_label": text, "source_icon": symbol}

def should_announce(source: object) -> bool:
    """Is this a turn the chat page has not already drawn for itself?"""
    return normalise(source) not in LOCAL

def catalogue() -> list[dict]:
    """Every known source, for a settings list or a check."""
    return [{"id": key, "label": text, "icon": symbol}
            for key, (text, symbol) in SOURCES.items()]
