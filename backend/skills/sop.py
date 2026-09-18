"""
Skills for the procedure library.

These let the model pull up the recipe for a task it is about to do, and write
one down when the user asks for a procedure to be kept. The automatic lookup
already happens before the turn starts; these are for the cases where the model
knows it needs to go looking, or where the user says "remember how we do this".
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.skills.sop")


def _store():
    from backend.sop import store
    return store


async def _sop_lookup(params: dict) -> dict:
    """Find the procedure for a task."""
    from backend.sop import match

    task = str(params.get("task") or params.get("query") or "").strip()
    if not task:
        return {"success": False, "error": "Pass a 'task' to look up."}
    store = _store()
    if not store.enabled():
        return {"success": False, "error": "Procedures are turned off in settings."}

    try:
        limit = max(1, min(int(params.get("limit") or 3), 10))
    except (TypeError, ValueError):
        limit = 3

    category = str(params.get("category") or "").strip() or None
    hits = match.find(task, category=category, limit=limit)
    if not hits:
        return {
            "success": True,
            "count": 0,
            "summary": (f"No procedure matches that yet — nothing recorded for "
                        f"'{task[:60]}'. Do the task the way that makes sense and "
                        f"it will be kept for next time."),
        }
    return {
        "success": True,
        "count": len(hits),
        "procedures": [
            {"id": h["sop"].get("id"),
             "category": h["sop"].get("category"),
             "title": h["sop"].get("title"),
             "steps": h["sop"].get("steps"),
             "score": h["score"]}
            for h in hits
        ],
        "summary": "Closest procedure: " + str(hits[0]["sop"].get("title")),
    }


async def _sop_save(params: dict) -> dict:
    """Create or update a procedure."""
    store = _store()
    if not store.enabled():
        return {"success": False, "error": "Procedures are turned off in settings."}

    steps = params.get("steps")
    if isinstance(steps, str):
        steps = steps.split("\n")
    if not steps and not params.get("id"):
        return {"success": False,
                "error": "Pass 'steps' (a list, or one per line) to record."}

    out = store.upsert({
        "id": params.get("id"),
        "category": params.get("category") or "general",
        "title": params.get("title"),
        "steps": steps,
        "tools": params.get("tools"),
        "source": "manual",
    })
    if not out.get("success"):
        return out
    sop = out["sop"]
    return {
        "success": True,
        "sop": sop,
        "summary": (f"Saved the '{sop['category']}' procedure \"{sop['title']}\" "
                    f"with {len(sop['steps'])} step(s)."),
    }


async def _sop_list(params: dict) -> dict:
    """List the stored procedures, optionally for one category."""
    store = _store()
    if not store.enabled():
        return {"success": False, "error": "Procedures are turned off in settings."}
    category = str(params.get("category") or "").strip() or None
    sops = store.list_all(category)
    return {
        "success": True,
        "count": len(sops),
        "categories": store.categories(),
        "procedures": [{"id": s.get("id"), "category": s.get("category"),
                        "title": s.get("title"), "steps": s.get("steps")}
                       for s in sops],
        "summary": f"{len(sops)} procedure(s) stored.",
    }


def register(registry) -> None:
    """Attach the procedure skills to a SkillRegistry."""
    from backend.skills.registry import SkillDefinition

    try:
        from backend.sop import learn, match, store  # noqa: F401
    except Exception as e:
        log.debug("procedure skills unavailable: %s", e)
        return

    registry.register(SkillDefinition(
        "sop_lookup",
        "Look up the standard procedure for a task category — the steps that "
        "worked previously for this kind of task. Use it when starting "
        "something that has been done before (editing files, changing code, "
        "research, filling in a calendar) to follow the same route.",
        {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "The task to find a procedure for."},
                "category": {"type": "string",
                             "description": "Optional category to restrict to, "
                                            "e.g. files, code, web."},
                "limit": {"type": "integer",
                          "description": "Maximum procedures to return."},
            },
            "required": ["task"],
        },
        _sop_lookup,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "sop_save",
        "Record or update a standard procedure for a task category, so the "
        "same route is followed next time. Use it when the user says to "
        "remember how something is done, or asks for a procedure to be saved.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "Short name for the procedure."},
                "category": {"type": "string",
                             "description": "Task category, e.g. files, code, web."},
                "steps": {"type": "array", "items": {"type": "string"},
                          "description": "The ordered steps."},
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "Tools this procedure uses."},
                "id": {"type": "string",
                       "description": "Existing procedure id, to update one."},
            },
            "required": ["title", "steps"],
        },
        _sop_save,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "sop_list",
        "List the stored standard procedures, optionally for one category. "
        "Use it when the user asks what procedures Addled knows.",
        {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "description": "Optional category to list."},
            },
        },
        _sop_list,
        category="memory",
    ))
