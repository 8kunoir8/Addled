"""What each agent knows — its own notebook, kept between runs.

The swarm used to have one shared blackboard, in memory only. That meant an
agent had no memory of its *own* work: run `planner → coder`, restart, and the
planner was a stranger to its own plan. It also meant a flow that was
interrupted lost everything, because the transcript lived in a Python list.

This is the per-agent half. Every desk gets a notebook on disk:

  memory/swarm/notebooks/<agent_id>.json

Three things make it usable rather than merely large:

* **Entries are capped and summarised.** A notebook that grew without bound
  would eventually cost more context than it returns, and on a small local
  model it would crowd out the task itself. Past a threshold the oldest entries
  fold into a running summary.
* **Distilled, not dumped.** `digest()` returns what this agent should carry
  into its next task — its own recent findings plus the standing summary —
  while the full entries stay on disk for a later question.
* **Written as work happens, not at the end.** A run that is killed mid-step
  keeps whatever it had already written, which is the only version of this that
  survives the crash it is meant to survive.

It is deliberately plain JSON beside the roster, so it can be read, edited or
deleted by hand and is never touched by an update.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("addled.swarm.notebook")

# One lock per notebook file.
#
# Every write is a read-modify-write of the whole file, so two writers at once
# lose one of the two records: the second reads the file before the first has
# written it and then saves its own version over the top. Measured at 3 of 100
# records surviving with four concurrent writers.
#
# This happens in practice, not in theory: a flow can name the same desk in two
# steps, `run_parallel` runs agents concurrently, and two goals can use one
# desk at the same time. The agent whose work was dropped has no way to tell.
#
# A per-file lock rather than one global lock, because two desks writing their
# own notebooks do not contend and should not serialise behind each other.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock

_DIR = Path(__file__).resolve().parent.parent / "memory" / "swarm" / "notebooks"

# How many entries one notebook keeps verbatim before the oldest are folded
# into the summary. Chosen to be more than a normal run produces, so the fold
# only happens for a desk that has genuinely been working a long time.
MAX_ENTRIES = 60

# How many recent entries `digest()` hands back. This is what rides into the
# next prompt, so it is the number that matters for context cost.
DIGEST_ENTRIES = 6

# Characters of digest. Bounded for the same reason: it is prepended to a task
# that also carries a persona, a brief and the flow transcript.
MAX_DIGEST_CHARS = 3000

# Characters kept per entry. A step's output can be a whole document; the
# notebook keeps the head of it and the summary holds the rest.
MAX_ENTRY_CHARS = 1500

# The standing summary. Its own bound, because it is re-summarised into itself
# and would otherwise creep upward with every fold.
MAX_SUMMARY_CHARS = 2500

def _ensure() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.debug("could not create the notebook folder: %s", e)

def path_for(agent_id: str) -> Path | None:
    """Where one agent's notebook lives, or None for an unusable id."""
    clean = str(agent_id or "").strip()
    if not clean or len(clean) > 120:
        return None
    # ids come from the roster and from generated agent_N values; a separator
    # in one would escape the folder.
    if "/" in clean or "\\" in clean or clean in (".", ".."):
        return None
    return _DIR / f"{clean}.json"

def _blank(agent_id: str = "", name: str = "") -> dict:
    return {
        "agentId": str(agent_id or ""),
        "name": str(name or ""),
        "summary": "",
        "entries": [],
        "updated": 0.0,
        "flows": [],
    }

def load(agent_id: str, name: str = "") -> dict:
    """One agent's notebook. Never raises — an unreadable one reads as empty.

    An empty notebook is the correct failure: the agent simply starts without
    history, which is no worse than the behaviour this replaced.
    """
    _ensure()
    path = path_for(agent_id)
    if path is None or not path.is_file():
        return _blank(agent_id, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.debug("notebook for %s unreadable, starting empty: %s", agent_id, e)
        return _blank(agent_id, name)
    if not isinstance(data, dict):
        return _blank(agent_id, name)
    out = _blank(agent_id, name)
    out["summary"] = str(data.get("summary") or "")[:MAX_SUMMARY_CHARS]
    entries = data.get("entries")
    out["entries"] = [e for e in entries if isinstance(e, dict)][-MAX_ENTRIES:] \
        if isinstance(entries, list) else []
    out["updated"] = float(data.get("updated") or 0.0)
    flows = data.get("flows")
    out["flows"] = [str(f) for f in flows][-40:] if isinstance(flows, list) else []
    return out

def save(agent_id: str, data: dict) -> bool:
    """Write one notebook. Never raises."""
    _ensure()
    path = path_for(agent_id)
    if path is None:
        return False
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except OSError as e:
        log.warning("could not write the notebook for %s: %s", agent_id, e)
        return False

def record(agent_id: str, *, name: str = "", task: str = "", output: str = "",
           kind: str = "step", flow: str = "", summary: str = "") -> dict:
    """Append one thing this agent learned or did.

    Called after every step rather than at the end of a flow, because a run
    that is killed mid-way is exactly the case this exists for.

    The whole read-modify-write is done under the notebook's lock. Without it
    two concurrent writers each read the file before the other saved, and one
    of the two records is lost — silently, because both writers report success.
    """
    path = path_for(agent_id)
    if path is None:
        return load(agent_id, name)
    with _lock_for(path):
        return _record_locked(agent_id, name=name, task=task, output=output,
                              kind=kind, flow=flow, summary=summary)

def _record_locked(agent_id: str, *, name: str = "", task: str = "",
                   output: str = "", kind: str = "step", flow: str = "",
                   summary: str = "") -> dict:
    """The body of `record`, called with the notebook's lock already held."""
    data = load(agent_id, name)
    if name:
        data["name"] = str(name)
    text = str(output or "").strip()
    if text:
        data["entries"].append({
            "task": " ".join(str(task or "").split())[:200],
            "output": text[:MAX_ENTRY_CHARS],
            "kind": str(kind or "step"),
            "flow": str(flow or ""),
            "timestamp": time.time(),
        })
    if summary:
        data["summary"] = _merge_summary(data.get("summary", ""), summary)
    if flow and flow not in data["flows"]:
        data["flows"].append(str(flow))
    data["updated"] = time.time()

    # Fold the oldest entries into the summary once there are too many to hand
    # back. The fold keeps a compressed trace of them so the detail is not lost
    # outright — it stops being verbatim, which is the trade this makes.
    overflow = len(data["entries"]) - MAX_ENTRIES
    if overflow > 0:
        folded = data["entries"][:overflow]
        data["entries"] = data["entries"][overflow:]
        joined = "\n".join(
            f"- {e.get('task')}: {str(e.get('output') or '')[:200]}"
            for e in folded)
        data["summary"] = _merge_summary(data.get("summary", ""), joined)
    save(agent_id, data)
    return data

def _merge_summary(existing: str, addition: str) -> str:
    """Append to the standing summary, keeping it inside its bound.

    Trims from the front, because the oldest part of a summary is the part
    least likely to matter now.
    """
    left = str(existing or "").strip()
    right = str(addition or "").strip()
    if not right:
        return left[:MAX_SUMMARY_CHARS]
    combined = f"{left}\n{right}".strip() if left else right
    if len(combined) > MAX_SUMMARY_CHARS:
        combined = combined[-MAX_SUMMARY_CHARS:]
    return combined

def digest(agent_id: str, name: str = "") -> str:
    """What this agent should carry into its next task.

    Its standing summary plus its own most recent work — nothing from other
    agents, which is the point: a desk's own history is what it was missing
    when every agent shared one undifferentiated blackboard.
    """
    data = load(agent_id, name)
    blocks = []
    if data["summary"]:
        blocks.append("What you have done before on this desk:\n"
                      + data["summary"])
    recent = data["entries"][-DIGEST_ENTRIES:]
    if recent:
        lines = []
        for entry in recent:
            lines.append(f"[{entry.get('task') or 'earlier work'}]\n"
                         f"{str(entry.get('output') or '')[:MAX_ENTRY_CHARS]}")
        blocks.append("Your most recent work, newest last:\n\n"
                      + "\n\n".join(lines))
    text = "\n\n".join(blocks)
    if len(text) > MAX_DIGEST_CHARS:
        text = text[-MAX_DIGEST_CHARS:]
    return text

def search(agent_id: str, query: str, name: str = "") -> list[dict]:
    """Entries of this agent's notebook matching a word in task or output.

    The full entries stay on disk after the digest has moved on, so something
    the agent did forty steps ago is still answerable — which is the half a
    summary alone cannot give.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    data = load(agent_id, name)
    hits = []
    for entry in reversed(data["entries"]):
        haystack = (f"{entry.get('task', '')}\n"
                    f"{entry.get('output', '')}").lower()
        if needle in haystack:
            hits.append(entry)
    return hits

def forget(agent_id: str) -> bool:
    """Delete one agent's notebook. Used when the agent itself is removed."""
    path = path_for(agent_id)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        log.warning("could not remove the notebook for %s: %s", agent_id, e)
        return False

def listing() -> list[dict]:
    """Every notebook, without its contents — for a page that lists them."""
    _ensure()
    out = []
    try:
        for path in sorted(_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            out.append({
                "agentId": str(data.get("agentId") or path.stem),
                "name": str(data.get("name") or ""),
                "entries": len(data.get("entries") or []),
                "flows": len(data.get("flows") or []),
                "hasSummary": bool(str(data.get("summary") or "").strip()),
                "updated": float(data.get("updated") or 0.0),
            })
    except OSError as e:
        log.debug("could not list notebooks: %s", e)
    out.sort(key=lambda e: e["updated"], reverse=True)
    return out
