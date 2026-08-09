"""
Goal planner — decomposes user goals into actionable steps using LLM.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("addled.goals.planner")


async def plan_goal(title: str, description: str = "", provider=None) -> dict:
    """Use LLM to break a goal into sequential steps."""
    if not provider:
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:
            pass

    prompt = f"""Break this user goal into sequential, actionable steps.
Each step must be a specific action the agent can perform (click, type, launch, read_file, write_file, list_dir, run_command, etc.).
Return ONLY a JSON array of step objects with these fields:
- "description": what this step does (string)
- "action_type": the action to execute (string, from the list above)
- "params": parameters for the action (object)
- "dependencies": indices of steps that must complete first (array of ints, empty if none)

Goal: {title}
{f'Details: {description}' if description else ''}

Return format: [{{"description":"...","action_type":"...","params":{{...}},"dependencies":[]}},...]"""

    try:
        if provider:
            result = await provider.chat([{"role": "user", "content": prompt}], max_tokens=2000, temperature=0.3)
            if result.ok:
                # Extract JSON array from response
                text = result.response.strip()
                # Handle code blocks
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.strip()
                elif text.startswith("[") and "]" in text:
                    start = text.index("[")
                    end = text.rindex("]") + 1
                    text = text[start:end]
                steps = json.loads(text)
                # Validate steps
                valid_steps = []
                for i, step in enumerate(steps):
                    if isinstance(step, dict) and "description" in step:
                        valid_steps.append({
                            "index": i,
                            "description": step.get("description", f"Step {i+1}"),
                            "action_type": step.get("action_type", "run_command"),
                            "params": step.get("params", {}),
                            "dependencies": step.get("dependencies", []),
                            "status": "pending",
                        })
                if valid_steps:
                    return {"steps": valid_steps, "count": len(valid_steps)}
    except Exception as e:
        log.warning("Goal planning failed, using fallback: %s", e)

    # Fallback: simple decomposition
    return {"steps": _fallback_plan(title, description), "count": 3}


def _fallback_plan(title: str, description: str = "") -> list[dict]:
    """Generate a simple fallback plan without LLM."""
    steps = [
        {"index": 0, "description": f"Analyze requirements for: {title}",
         "action_type": "run_command", "params": {"command": f"echo Analyzing: {title}"},
         "dependencies": [], "status": "pending"},
        {"index": 1, "description": f"Execute: {title}",
         "action_type": "run_command", "params": {"command": f"echo Executing: {title}"},
         "dependencies": [0], "status": "pending"},
        {"index": 2, "description": f"Verify completion of: {title}",
         "action_type": "run_command", "params": {"command": f"echo Verifying: {title}"},
         "dependencies": [1], "status": "pending"},
    ]
    return steps
