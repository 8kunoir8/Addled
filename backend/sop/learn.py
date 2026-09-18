"""
Learning procedures from runs that actually worked.

The rule that keeps this from becoming noise: only a run that *succeeded* and
used at least two different tools is treated as a procedure. One tool call is
not a procedure, it is a tool call — and a failed run teaches the wrong lesson.
Everything learned is merged into the closest existing procedure for that
category when it is similar enough, so repeating a task sharpens one recipe
instead of cloning a dozen of them.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.sop.learn")

MIN_TOOLS = 2
MAX_TRACE_TOOLS = 8
GOAL_CHARS = 160


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _trace_steps(goal: str, tools: list[str]) -> list[str]:
    steps = []
    if goal:
        steps.append(f"Goal: {goal}")
    steps += [f"Call {tool}" for tool in tools]
    return steps


def record_run(category: str, tools_used, message: str = "",
               success: bool = True) -> dict:
    """Fold a finished task into the procedures. Never raises.

    Called after a tool-using turn: if it went well, the sequence that got
    there is worth keeping; if it did not, keeping it would train Addled to
    repeat a mistake.

    Two runs are the same procedure when they *accomplish* the same thing, so
    the comparison is made against the goal rather than the goal plus the tools
    — otherwise every task that happened to use the same tools would collapse
    into one, and the procedure would be titled after whichever came first.
    """
    try:
        from backend.sop import store, match

        if not store.enabled():
            return {"skipped": "procedures are turned off"}
        if not store.learning_enabled():
            return {"skipped": "learning is turned off"}
        if not success:
            return {"skipped": "the run did not succeed"}

        tools = store._clean_tools(tools_used)
        if len(tools) < MIN_TOOLS:
            return {"skipped": f"only {len(tools)} tool(s) — not a procedure"}

        resolved = (match.category_for(tools) if not category
                    else store.clean_category(category))
        goal = _shorten(message, GOAL_CHARS)
        trace = tools[:MAX_TRACE_TOOLS]
        steps = _trace_steps(goal, trace)
        task_text = " ".join([goal] + trace)

        comparison = goal or " ".join(trace)
        closest, best, method = None, 0.0, "lexical"
        for sop in store.list_all(resolved):
            value, how = match.similarity_pair(comparison, sop)
            if value > best:
                closest, best, method = sop, value, how

        if closest is not None and best >= match.threshold(method, "merge"):
            # Same task, done again: widen the tool set and count the use
            # rather than adding a near-duplicate nobody will ever read.
            merged_tools = store._clean_tools(list(closest.get("tools") or []) + trace)
            store.upsert({"id": closest["id"], "tools": merged_tools})
            store.record_use(closest["id"], success=True)
            log.debug("Merged a run into %s (%s, %.2f)", closest["id"], resolved, best)
            return {"merged": closest["id"], "category": resolved,
                    "similarity": round(best, 3), "tools": merged_tools}

        title = _shorten(goal, 60) or " → ".join(trace[:3])
        out = store.upsert({
            "category": resolved,
            "title": title,
            "steps": steps,
            "tools": trace,
            "source": "learned",
        })
        if not out.get("success"):
            return {"skipped": out.get("error") or "could not be saved"}
        store.record_use(out["sop"]["id"], success=True)
        log.info("Learned a procedure for '%s': %s", resolved, title)
        return {"created": out["sop"]["id"], "category": resolved,
                "title": title, "tools": trace}
    except Exception as e:
        # Learning is a side effect of a conversation that has already
        # succeeded; failing it must never surface to the user.
        log.debug("procedure learning failed: %s", e)
        return {"skipped": str(e)}
