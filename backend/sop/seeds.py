"""
Procedures Addled starts with.

These are the handful of orderings that are simply right, and that are worth
having on day one rather than waiting to rediscover them: look before you
overwrite, bind before you read, search before you fetch. They are marked
``source: "seed"`` so the dashboard can show what was given versus what was
learned, and so they can be told apart when pruning.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.sop.seeds")

# Seeds a later version replaced or withdrew.
#
# `seed()` can only ADD: it tops up what is missing and leaves what is there, so
# a built-in that this file stops declaring has no way to leave an existing
# install. The merged file procedure proved that is a real gap — "Find a file
# before creating one" and "Inspect before overwriting" were combined into one
# recipe here, and on a machine that already had both, BOTH stayed. Two
# near-identical procedures about writing files then competed for the same
# tasks, and the matcher correctly refused the call as ambiguous, so the merge
# made matching worse on exactly the installs it was meant to improve.
#
# Listing them here retires them the same way a user deletion is recorded: the
# key is tombstoned so it is never re-added, and the stored copy is dropped.
# Only seeds are affected. A procedure the user wrote or edited is matched by
# title but is not `source: "seed"` and is left alone.
RETIRED: list[tuple[str, str]] = [
    ("files", "Find a file before creating one"),
    ("files", "Inspect before overwriting"),
]

SEEDS: list[dict] = [
    {
        # ONE file procedure, not two.
        #
        # These were separate — "Find a file before creating one" and "Inspect
        # before overwriting" — and they could not be told apart: same category,
        # both writing a file, sharing the vocabulary the matcher scores on. A
        # task like "write the config file again" fits both, and no amount of
        # ranking fixes that, because the two recipes genuinely were the same job
        # split in half.
        #
        # Anthropic's guidance on tool design is explicit: tools that overlap in
        # function make agents confused about which to use, and "if a human
        # engineer can't definitively say which tool should be used in a given
        # situation, an AI agent can't be expected to do better" — which was
        # exactly my position. Their own examples consolidate this way
        # (`list_users` + `list_events` + `create_event` become one
        # `schedule_event`). So search-then-write is one procedure with two
        # branches, which is how a person would describe it.
        "category": "files",
        "title": "Search, then write — inspecting anything already there",
        "steps": [
            "Search the workspace first: an existing file, note or document may "
            "already cover this, and creating a second one is the mistake.",
            "If the target already exists, read it BEFORE rewriting, updating or "
            "replacing it — an overwrite that drops content the user wanted is "
            "the expensive mistake.",
            "Create or write the change, putting a new file in the workspace "
            "folder rather than the process directory.",
            "Read it back to confirm what is actually on disk.",
        ],
        "tools": ["search_files", "read_file", "write_file"],
        "guards": [
            "the workspace was searched before a new file was written",
            "an existing target was read before it was overwritten",
        ],
    },
    {
        "category": "code",
        "title": "Change code safely",
        "steps": [
            "Read the file you are about to change, in full.",
            "Make the edit, keeping to the style of the code around it.",
            "Review the diff before applying it.",
            "Apply, then run the project's own checks rather than assuming.",
        ],
        "tools": ["code_read", "code_edit"],
        "guards": [
            "the file was read before it was edited",
            "the project's own check was run after the change",
        ],
    },
    {
        "category": "web",
        "title": "Search, then read the best source",
        "steps": [
            "Search for the question rather than guessing an answer.",
            "Open the most relevant result and read it.",
            "Say where the answer came from, and flag it if the sources "
            "disagree.",
        ],
        "tools": ["web_search", "web_fetch"],
        "guards": [],
    },
    {
        "category": "browser",
        "title": "Read a page before acting on it",
        "steps": [
            "Navigate to the URL.",
            "Read the page to see what is actually there.",
            "Only then click or fill anything, targeting what was observed.",
        ],
        "tools": ["browser_navigate", "browser_extract"],
        "guards": [
            "the page was read before anything on it was clicked or filled",
        ],
    },
    {
        "category": "memory",
        "title": "Check what is already known",
        "steps": [
            "Search the wiki and memory before answering a question about the "
            "user's own material.",
            "Prefer the stored page over recollection, and say when nothing "
            "was found.",
        ],
        "tools": ["wiki_search", "memory_get"],
        "guards": [],
    },
    {
        "category": "calendar",
        "title": "Confirm before writing to a calendar",
        "steps": [
            "List the existing events around the requested time first.",
            "Check the slot is actually free.",
            "Create the event, then list again to confirm it landed.",
        ],
        "tools": ["calendar_list", "calendar_add"],
        "guards": [
            "the slot was checked for a clash before the event was created",
        ],
    },
    # -- documents -----------------------------------------------------------
    #
    # ONE document procedure, not three.
    #
    # Three were tried and they fought each other: every document tool is filed
    # under `files` in the skill registry, so all three landed in the same
    # category as the two file-handling seeds and shared the vocabulary
    # ("file", "read", "document") that the matcher scores on. "read a file
    # before you rewrite it" then matched "Read before answering about a file's
    # contents" instead of "Inspect before overwriting", and "save a new
    # document" matched "Convert a document". The recipes were not wrong; there
    # were simply too many of them saying similar things.
    #
    # So this keeps the one that is genuinely a different job — a document is
    # read differently and converted differently from a text file — and folds
    # the redaction and conversion guidance into its steps rather than giving
    # each its own near-identical entry.
    {
        "category": "files",
        "title": "Handle a document as a document",
        "steps": [
            "Read it with the tool for its format (word_read, excel_read, "
            "pdf_read) rather than as plain text.",
            "Converting writes a NEW file; never convert over the original.",
            "Redacting is irreversible: say what will be removed, let the user "
            "confirm, write to a new file, then verify it is really gone.",
            "Read the result back — a conversion or redaction that silently "
            "lost content is the failure to catch.",
        ],
        "tools": ["pdf_read", "word_read", "excel_read", "pdf_redact",
                  "pdf_redact_verify", "convert_to_pdf"],
        "guards": [
            "the output is a different file from the source",
            "a redaction was verified, not assumed",
        ],
    },
    {
        "category": "system",
        "title": "Look before running a command",
        "steps": [
            "Work out what the command will actually do before running it.",
            "Prefer the least destructive form that answers the question.",
            "Read the output rather than assuming it worked.",
        ],
        "tools": ["run_command"],
        "guards": [
            "the command was read back before it was run",
        ],
    },
    {
        "category": "system",
        "title": "Undo by reverting, not by guessing",
        "steps": [
            "Find out what actually changed — the last commit, or the file's "
            "previous contents.",
            "Revert that specific thing rather than writing a replacement from "
            "memory.",
            "Confirm the state is what was expected before carrying on.",
        ],
        "tools": ["run_command", "read_file"],
        "guards": [
            "the thing being reverted was identified, not assumed",
        ],
    },
]


def seed() -> int:
    """Insert every seed that is not already present. Returns how many are new.

    Never raises. Idempotent by category+title, so calling it every start adds
    only what is genuinely missing.
    """
    try:
        from backend.sop import store

        removed = store.removed_seed_keys()
        added = 0
        retired = 0
        for category, title in RETIRED:
            key = store.seed_key(category, title)
            if key in removed:
                continue
            existing = store.find_by_title(category, title)
            if not existing:
                # Either never seeded, or already retired. Recording the key
                # anyway keeps it from coming back if a future edit to SEEDS
                # re-introduces the title by accident.
                store.tombstone_seed(category, title)
                continue
            if str(existing.get("source") or "") != "seed":
                # The user adopted the title for their own procedure. Not ours
                # to remove.
                continue
            if store.delete(existing.get("id")):
                retired += 1
        for entry in SEEDS:
            key = store.seed_key(entry["category"], entry["title"])
            if key in removed:
                # The user deleted this one. Re-adding it would be the app
                # overruling a decision it was told to make.
                continue
            existing = store.find_by_title(entry["category"], entry["title"])
            if existing:
                continue
            out = store.upsert({**entry, "source": "seed"})
            if out.get("success"):
                added += 1
        if retired:
            log.info("Retired %d superseded procedure(s)", retired)
        if added:
            log.info("Seeded %d procedure(s)", added)
        return added
    except Exception as e:
        log.debug("Seeding procedures failed: %s", e)
        return 0

def ensure_seeded() -> bool:
    """Top up the built-ins on every start. Returns whether anything was added.

    This used to seed ONLY when the store was completely empty, on the reasoning
    that a deleted seed should stay deleted. That was right about deletion and
    wrong about everything else: it also meant a NEW built-in procedure never
    reached an install that already had one, so shipping an improved recipe
    required throwing the store away.

    Both halves are honoured now. A seed missing from the store is added
    whatever else is there, and one the user deleted is not resurrected — the
    deletion is recorded as a tombstone by `store.delete`, so "the user said no"
    survives a restart while "this recipe did not exist last time" does not
    block a top-up.

    Nothing here overwrites. An existing procedure is left exactly as it is,
    whether the user edited it or learning sharpened it; improvement comes from
    use (see `store.record_use` and the reliability term in `sop/match.py`),
    not from rewriting the file on boot.
    """
    try:
        from backend.sop import store
        if not store.enabled():
            return False
        return seed() > 0
    except Exception as e:
        log.debug("Could not top up the procedure seeds: %s", e)
        return False
