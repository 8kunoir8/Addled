"""
Goal planner — decomposes user goals into actionable steps using LLM.

A step is one of two kinds, and which kind it is decides who runs it:

- **agent** — reasoning work. Handed to a swarm desk, through the same pipeline
  chat uses, so it has tools, memory and the agents' notebooks. This is what a
  goal like "research X and write it up" needs.
- **action** — a mechanical call (`run_command`, `write_file`). Run by the
  ActionExecutor with no model involved. This is what "pip install X" needs, and
  it stays because routing it through an agent would only add a model call that
  decides to do the thing the step already says.

A plan may mix them, and usually should: the research is agent work and the
`git commit` at the end is not.

The plan names **roles** rather than specific agents, because the desks a user
has are theirs to rename and add to. The executor maps a role onto a real
agent — preferring an exact roster match, then a type match, then spawning a
desk of that type for the run.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("addled.goals.planner")

# The roles a plan may ask for. Each maps onto a swarm agent type
# (`roster.DEFAULT_TYPES`) so a desk can be found or made for it.
PLAN_ROLES = ("researcher", "planner", "analyst", "coder", "writer",
              "devops", "qa", "reviewer", "general")

# The action types an `action` step may name. Deliberately the mechanical ones:
# anything needing judgement should be an agent step, and a step naming an
# action nobody can run is a plan that cannot start.
PLAN_ACTIONS = ("run_command", "read_file", "write_file", "list_dir",
                "create_dir", "search_files", "file_info", "launch")

def _prompt(title: str, description: str, roles: list[str],
            feedback: str = "") -> str:
    """The planning prompt.

    `feedback` carries what happened on a previous attempt, so a re-plan is
    informed by the failure rather than being the same request again — which is
    what makes the loop converge instead of repeating itself.
    """
    retry = ""
    if feedback:
        retry = (f"\nThis goal was attempted before and did not finish. What "
                 f"happened:\n{feedback}\n\nPlan around that: do not repeat "
                 f"what already worked, and address what did not.\n")
    return f"""Break this goal into steps that will actually finish it.

A step is either:
- "agent": reasoning work for a desk — research, decide, write, review. Give
  it a clear task, the role best suited, and what it should produce.
- "action": a single mechanical call. Use this only when the step is a command
  or a file operation with no judgement in it.

Prefer agent steps. Most goals are not a shell script.

Available roles: {", ".join(roles)}
Available actions: {", ".join(PLAN_ACTIONS)}

Rules:
- Each step must say what "done" looks like in its task text, so the agent
  knows when to stop.
- Use "dependencies" (1-based step numbers) when a step needs an earlier
  result. Steps with no dependency may run together.
- End with a step that produces the finished deliverable.

Goal: {title}
{f'Details: {description}' if description else ''}
{retry}
Return ONLY a JSON array, no prose:
[{{"kind":"agent","role":"researcher","task":"...","dependencies":[]}}, ...]"""

def _clean_steps(raw: str) -> list[dict]:
    """Parse the model's JSON array out of its reply. Returns [] on failure.

    Models wrap JSON in code fences and put a sentence before it often enough
    that a plain `json.loads` fails on replies that are otherwise fine.
    """
    text = str(raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            body = parts[1]
            if body.lstrip().lower().startswith("json"):
                body = body.lstrip()[4:]
            text = body.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("goal plan was not valid JSON: %s", e)
        return []
    return parsed if isinstance(parsed, list) else []

def _normalise_step(step: dict, index: int, roles: list[str]) -> dict | None:
    """Shape one planned step, or None when it cannot run.

    An unknown action or role is repaired rather than rejected where the intent
    is obvious — a plan that is 90% right should run, not fail at validation and
    leave the user with nothing.
    """
    if not isinstance(step, dict):
        return None
    task = str(step.get("task") or step.get("description") or "").strip()
    kind = str(step.get("kind") or "").strip().lower()

    if kind not in ("agent", "action"):
        # Infer from what the step names. A step carrying `action_type` was
        # written for the action executor; anything else is reasoning work.
        kind = "action" if step.get("action_type") in PLAN_ACTIONS else "agent"

    if not task:
        if kind == "action":
            task = f"{step.get('action_type', 'run')} a command"
        else:
            return None

    deps = []
    for d in (step.get("dependencies") or []):
        try:
            value = int(d)
        except (TypeError, ValueError):
            continue
        # 1-based. A step cannot wait on itself or on one that does not exist
        # yet: both would leave it permanently blocked, which reads as a hang
        # rather than a mistake the user can fix. `index` is the step's own
        # 1-based position, so "not yet" means anything above it.
        if 1 <= value < index:
            deps.append(value)

    if kind == "action":
        action_type = str(step.get("action_type") or "").strip()
        if action_type not in PLAN_ACTIONS:
            action_type = "run_command"
        params = step.get("params")
        if not isinstance(params, dict):
            params = {}
        return {
            "index": index, "kind": "action", "description": task,
            "action_type": action_type, "params": params,
            "dependencies": sorted(set(deps)), "status": "pending",
        }

    role = str(step.get("role") or "").strip().lower()
    if role not in roles:
        role = "general"
    return {
        "index": index, "kind": "agent", "role": role,
        "description": task, "task": task,
        "expects": str(step.get("expects") or step.get("produces") or "").strip(),
        "dependencies": sorted(set(deps)), "status": "pending",
    }


async def plan_goal(title: str, description: str = "", provider=None,
                    *, roles: list[str] | None = None,
                    feedback: str = "") -> dict:
    """Break a goal into steps. Falls back to a single agent step.

    The fallback is deliberately an agent step rather than a set of `echo`
    commands. The old fallback produced three shell steps that printed the
    goal's title and reported success — a goal that did nothing looked like a
    goal that worked. One agent step that may not finish is honest about it.
    """
    roles = [r for r in (roles or PLAN_ROLES) if r]
    if not provider:
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception as e:  # noqa: BLE001
            log.debug("no provider for goal planning: %s", e)

    if provider:
        try:
            from backend.providers import router
            result = await provider.chat(
                [{"role": "user", "content":
                  _prompt(title, description, roles, feedback)}],
                model=router.for_provider(provider, "reasoning"),
                max_tokens=2000, temperature=0.3)
            if getattr(result, "ok", False):
                steps = []
                for i, raw in enumerate(_clean_steps(result.response), 1):
                    shaped = _normalise_step(raw, i, roles)
                    if shaped:
                        steps.append(shaped)
                if steps:
                    return {
                        "steps": steps, "count": len(steps),
                        "kind": "planned",
                        "roles": sorted({s["role"] for s in steps
                                         if s["kind"] == "agent"}),
                    }
                log.warning("goal planning returned no usable steps")
        except Exception as e:  # noqa: BLE001
            log.warning("Goal planning failed, using fallback: %s", e)

    return {"steps": _fallback_plan(title, description), "count": 1,
            "kind": "fallback", "roles": ["general"]}


def _fallback_plan(title: str, description: str = "") -> list[dict]:
    """One agent step that does the work.

    Not a three-step echo. A fallback that pretends to work is worse than one
    that visibly asks a desk to do it and reports what came back.
    """
    task = f"Work on this goal and report what you did and what is left: {title}"
    if description:
        task += f"\n\nDetails: {description}"
    task += ("\n\nIf the goal needs something you cannot do with your tools, "
             "say exactly what is missing rather than reporting success.")
    return [{
        "index": 1, "kind": "agent", "role": "general",
        "description": task, "task": task, "expects": "",
        "dependencies": [], "status": "pending",
    }]
