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

# Sources where a person is present and can answer a question mid-flow.
#
# This is the authority on "may the model ask something and expect an answer?".
# It exists because asking is only meaningful when somebody is looking: the
# chat page, the character and the phone bridges all have a human on the other
# end, and `code` does too (the Code page's composer is right there).
#
# The set is written out rather than derived as "everything except UNATTENDED"
# on purpose. A new source added to SOURCES and forgotten here is then treated
# as unattended — the model states its assumption and carries on — instead of
# silently gaining the ability to park a question in front of nobody. Failing
# towards "decide for yourself" costs an answer the user has to correct; the
# other direction leaves a turn waiting on a card that will never be read.
ATTENDED = frozenset({
    # The fallback source, and the most common one in practice: the floating
    # character and anything that did not declare where it came from. It is
    # attended because an unlabelled turn is the user typing into their own
    # machine — the app's own default. Leaving it out made every turn that
    # forgot to pass `source` unable to ask, which is most of them.
    "addled",
    "dashboard",   # the chat page itself
    "telegram",    # a phone in someone's hand
    "discord",
    "whatsapp",
    "character",   # the floating widget: the user is looking at the screen
    "code",        # the Code page composer
    "remote",      # an SSH-style session someone is driving
})

# Sources with nobody to answer. These run from a schedule, a queue, or another
# agent's plan, so a question would sit unanswered while the work it blocks
# never finishes.
#
# A real list, and the authority for the *unattended* side. It used to be prose
# with the effective set derived as "everything not in ATTENDED" — which made
# the gap check tautological: `known - ATTENDED - unattended()` is empty by
# construction, so the guard that was supposed to catch an unclassified source
# could never fire. Two lists with a check that compares them is what makes a
# forgotten source visible.
UNATTENDED = frozenset({
    "task",        # a scheduled task — fired by the clock
    "swarm",       # another agent's desk
    "approval",    # the deferred run of an earlier approval
    "voice",       # wake-word capture: the reply is spoken, not answered
})

def normalise(source: object) -> str:
    """A known source id, or the default. Never raises, never returns junk."""
    name = str(source or "").strip().lower()
    return name if name in SOURCES else DEFAULT

def is_attended(source: object) -> bool:
    """Is there a person who could answer a question raised by this turn?

    Unknown sources fall back to `DEFAULT`, which is attended — an unlabelled
    turn is the user in their own app. A *typo* is therefore treated as
    attended too, and that is the right way round: the cost of asking someone
    who is there is one card, and the cost of refusing to ask someone who is
    there is a turn that guesses wrong and explains itself badly.
    """
    return normalise(source) in ATTENDED

def unattended_sources() -> frozenset[str]:
    """Sources with nobody watching.

    The declared list, not a derived complement. Deriving it made the gap check
    below unable to fail: "known minus ATTENDED minus unattended" is empty by
    definition if `unattended` is itself derived from ATTENDED, so a source
    added to SOURCES and forgotten in ATTENDED was silently treated as
    **attended** at runtime while the guard reported everything fine.
    """
    return UNATTENDED

def attendance_gaps() -> dict:
    """Sources classified wrongly, for a check to report by name.

    Every `SOURCES` entry must be in exactly one of ATTENDED / UNATTENDED, and
    neither set may name something that is not a source. An empty result is the
    healthy state.

    The previous version could not fail: it subtracted the complement of
    ATTENDED from ATTENDED's own complement. With a declared UNATTENDED list
    the comparison is between two independent statements, so a forgotten source
    shows up in `unclassified`.
    """
    known = set(SOURCES)
    return {
        "unclassified": sorted(known - ATTENDED - UNATTENDED),
        "unknown_id": sorted((ATTENDED | UNATTENDED) - known),
        "both": sorted(ATTENDED & UNATTENDED),
    }

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
