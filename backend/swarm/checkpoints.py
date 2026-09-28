"""Where a flow got to, so an interrupted one can be picked up again.

A flow is the longest-running thing in the app — several agents, several model
calls, minutes on a local model — and it had no durability at all. Its progress
lived in local variables inside `run_flow`, so closing the app part-way through
threw away every finished step. The work was gone and the only way back was to
run it all again.

This writes progress to disk as each step completes:

  memory/swarm/checkpoints/<flow_id>.json

A checkpoint is written after every step, not at the end, because the case it
exists for is the one where there is no end. Resuming replays the finished
steps from the file and continues from the first unfinished one.

Kept deliberately small and boring: a list of what finished and what each step
produced. The agents' own notebooks hold the longer memory; this only has to be
enough to know where to carry on from.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.swarm.checkpoints")

_DIR = Path(__file__).resolve().parent.parent / "memory" / "swarm" / "checkpoints"

# How many finished checkpoints are kept. A resumable one is removed once it
# completes, so this is a bound on the abandoned ones — the flows that were
# killed and never picked up again.
MAX_KEPT = 30

# How much of a step's output the checkpoint keeps. Enough to rebuild the
# transcript on resume; the notebook keeps the rest.
MAX_OUTPUT_CHARS = 2000

def _ensure() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.debug("could not create the checkpoint folder: %s", e)

def path_for(flow_id: str) -> Path | None:
    clean = str(flow_id or "").strip()
    if not clean or len(clean) > 120:
        return None
    if "/" in clean or "\\" in clean or clean in (".", ".."):
        return None
    return _DIR / f"{clean}.json"

def start(flow_id: str, goal: str, steps: list[dict]) -> bool:
    """Open a checkpoint for a flow that is about to run.

    The step list is stored whole, because resuming needs to know what was
    *planned*, not only what was done — a step that was skipped or branched
    over is as important to the shape of the run as one that finished.
    """
    _ensure()
    path = path_for(flow_id)
    if path is None:
        return False
    payload = {
        "flowId": flow_id,
        "goal": str(goal or ""),
        "steps": steps,
        "finished": [],
        "skipped": [],
        "done": [],
        "started": time.time(),
        "updated": time.time(),
        "complete": False,
    }
    ok = _write(path, payload)
    if ok:
        _trim()
    return ok

def _write(path: Path, payload: dict) -> bool:
    try:
        payload["updated"] = time.time()
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except OSError as e:
        log.warning("could not write the checkpoint %s: %s", path.name, e)
        return False

def _trim() -> None:
    """Drop the oldest finished checkpoints once there are too many."""
    try:
        files = sorted(_DIR.glob("*.json"))
        if len(files) <= MAX_KEPT:
            return
        stamped = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                stamped.append((float(data.get("updated") or 0.0), path))
            except (OSError, json.JSONDecodeError):
                stamped.append((0.0, path))
        stamped.sort()
        for _, path in stamped[:len(stamped) - MAX_KEPT]:
            try:
                path.unlink()
            except OSError:
                pass
    except OSError as e:
        log.debug("could not trim checkpoints: %s", e)

def progress(flow_id: str, *, finished: list[int] | None = None,
             skipped: list[int] | None = None,
             done: list[dict] | None = None) -> bool:
    """Record where the flow has got to. Called after every step."""
    path = path_for(flow_id)
    if path is None or not path.is_file():
        return False
    data = load(flow_id)
    if data is None:
        return False
    if finished is not None:
        data["finished"] = sorted({int(s) for s in finished})
    if skipped is not None:
        data["skipped"] = sorted({int(s) for s in skipped})
    if done is not None:
        trimmed = []
        for entry in done:
            item = dict(entry)
            if "output" in item:
                item["output"] = str(item["output"])[:MAX_OUTPUT_CHARS]
            trimmed.append(item)
        data["done"] = trimmed
    return _write(path, data)

def complete(flow_id: str) -> bool:
    """Mark a flow done. Its checkpoint is no longer needed to resume."""
    path = path_for(flow_id)
    if path is None or not path.is_file():
        return False
    data = load(flow_id)
    if data is None:
        return False
    data["complete"] = True
    written = _write(path, data)
    if written:
        try:
            path.unlink()
        except OSError as e:
            log.debug("could not remove the finished checkpoint: %s", e)
    return written

def load(flow_id: str) -> dict | None:
    """One checkpoint, or None when there is nothing to resume."""
    path = path_for(flow_id)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.debug("checkpoint %s unreadable: %s", flow_id, e)
        return None
    return data if isinstance(data, dict) else None

def resumable() -> list[dict]:
    """Checkpoints that were interrupted, newest first.

    `complete` is checked as well as the file's presence: a flow that finished
    between the write and the unlink is not something to offer to resume.
    """
    _ensure()
    out = []
    try:
        for path in sorted(_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("complete"):
                continue
            steps = data.get("steps") or []
            finished = data.get("finished") or []
            out.append({
                "flowId": str(data.get("flowId") or path.stem),
                "goal": str(data.get("goal") or ""),
                "steps": len(steps),
                "finished": len(finished),
                "updated": float(data.get("updated") or 0.0),
            })
    except OSError as e:
        log.debug("could not list checkpoints: %s", e)
    out.sort(key=lambda e: e["updated"], reverse=True)
    return out

def discard(flow_id: str) -> bool:
    """Throw a checkpoint away without resuming it."""
    path = path_for(flow_id)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        log.warning("could not discard the checkpoint for %s: %s", flow_id, e)
        return False
