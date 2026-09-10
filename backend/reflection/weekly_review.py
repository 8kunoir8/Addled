"""Reflection loop — weekly self-review from skill telemetry.

Emits one gentle insight per week summarizing which skills got used and
which kept failing, so the agent (and user) know where to improve. No LLM
required for the summary — it's computed from counters.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("addled.reflection")

STATE_PATH = Path(__file__).resolve().parent.parent / "memory" \
    / "integrations" / "reflection_state.json"


def weekly_review(emit) -> dict:
    """Emit at most one weekly reflection. Returns {emitted, text}."""
    from backend.skills.telemetry import stats
    st = stats()
    if st.get("total_uses", 0) == 0:
        return {"emitted": False, "text": ""}

    now = datetime.now()
    week_key = f"{now.year}-W{now.isocalendar()[1]}"
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) \
            if STATE_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    if state.get("week") == week_key:
        return {"emitted": False, "text": ""}

    parts = []
    top = st.get("top", [])
    if top:
        names = ", ".join(t["name"] for t in top[:3])
        parts.append(f"I used {st['total_uses']} skills this week, mostly "
                     f"{names}.")
    if st.get("failing"):
        worst = st["failing"][0]
        parts.append(f"'{worst[0]}' failed {worst[1]} times — worth a "
                     "look.")
    if not parts:
        return {"emitted": False, "text": ""}
    text = " ".join(parts)
    try:
        emit(text)
    except Exception:
        pass
    state["week"] = week_key
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    return {"emitted": True, "text": text}
