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

SEEDS: list[dict] = [
    {
        "category": "files",
        "title": "Inspect before overwriting",
        "steps": [
            "List or read the target first so you know what is already there.",
            "Read the existing file before rewriting it — an overwrite that "
            "drops content the user wanted is the expensive mistake here.",
            "Write the change.",
            "Read it back to confirm what is actually on disk.",
        ],
        "tools": ["list_dir", "read_file", "write_file"],
    },
    {
        "category": "files",
        "title": "Find a file before creating one",
        "steps": [
            "Search the workspace for an existing file that already covers it.",
            "Only create a new one if nothing suitable exists.",
            "Put it in the workspace folder rather than the process directory.",
        ],
        "tools": ["search_files", "list_dir", "write_file"],
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
    },
]


def seed() -> int:
    """Insert the seeds. Returns how many are new. Never raises."""
    try:
        from backend.sop import store

        added = 0
        for entry in SEEDS:
            existing = store.find_by_title(entry["category"], entry["title"])
            if existing:
                continue
            out = store.upsert({**entry, "source": "seed"})
            if out.get("success"):
                added += 1
        if added:
            log.info("Seeded %d procedure(s)", added)
        return added
    except Exception as e:
        log.debug("Seeding procedures failed: %s", e)
        return 0


def ensure_seeded() -> bool:
    """Seed once, and only when there is nothing at all yet.

    Deliberately not "top up missing seeds": once the user deletes a seeded
    procedure it should stay deleted, not come back on the next start.
    """
    try:
        from backend.sop import store
        if store.load()["sops"]:
            return False
        return seed() > 0
    except Exception as e:
        log.debug("Could not check the procedure seeds: %s", e)
        return False
