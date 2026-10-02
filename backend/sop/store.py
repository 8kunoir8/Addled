"""
Standard operating procedures: the steps that worked last time.

A procedure is a short ordered recipe for a task category — "read before you
overwrite", "search, then fetch the best hit" — learned from runs that actually
succeeded, and looked up again when a similar task starts. That is the whole
point: the second time Addled does something, it should not rediscover how.

They are plain JSON under the memory folder, so they can be read, edited or
deleted by hand like everything else Addled knows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("addled.sop")

VERSION = 1
MAX_STEPS = 24
MAX_STEP_CHARS = 400
MAX_TITLE_CHARS = 120
MAX_TOOLS = 24
# The stored "why". Kept short because it is read back on every match and
# injected into the prompt alongside the steps.
MAX_REASON_CHARS = 300
# A guard is a precondition that must hold before a procedure's destructive step
# runs — "the target already exists", "the output is not the source". They are
# checked by code, not injected as advice, so a small number is enough and each
# one has to be machine-readable.
MAX_GUARDS = 8
MAX_GUARD_CHARS = 200
# Learned procedures are capped. Seeding is bounded and the user's own entries
# are deliberate, but LEARNING has no natural limit: it writes one procedure per
# distinct successful task, so a long-lived install accumulates them forever.
# That is not just clutter — matching refuses when two procedures are too close
# to call, so a store full of near-duplicates makes the matcher refuse tasks it
# would otherwise have answered. Observed live: six procedures left by a test
# run went on to outscore the built-ins and caused real tasks to be refused.
#
# The cap applies to `source: "learned"` only. Seeds are never pruned (they are
# the built-ins) and neither are manual entries (the user typed them), so a user
# who writes their own procedures can exceed this without losing any.
MAX_LEARNED = 200

CATEGORY_RE = re.compile(r"[^a-z0-9_ -]+")


# -- locations ----------------------------------------------------------------


def base_dir() -> Path:
    """Where procedures live. Configurable so they can sit in a synced folder."""
    custom = ""
    try:
        from backend.config import config
        custom = str(config.get("sop", "dir", default="") or "").strip()
    except Exception:
        custom = ""
    if custom:
        return Path(custom)
    from backend import app_paths
    return app_paths.subdir("sop")


def path() -> Path:
    return base_dir() / "sops.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- settings -----------------------------------------------------------------


def enabled() -> bool:
    try:
        from backend.config import config
        return bool(config.get("sop", "enabled", default=True))
    except Exception:
        return True


def learning_enabled() -> bool:
    try:
        from backend.config import config
        return bool(config.get("sop", "learn", default=True))
    except Exception:
        return True


def _setting(key: str, default):
    try:
        from backend.config import config
        return config.get("sop", key, default=default)
    except Exception:
        return default


# -- normalising --------------------------------------------------------------


def clean_category(value: str) -> str:
    text = CATEGORY_RE.sub("", str(value or "").strip().lower().replace(" ", "_"))
    return (text or "general")[:40]


def _clean_steps(steps) -> list[str]:
    if isinstance(steps, str):
        steps = steps.replace("\r\n", "\n").split("\n")
    if not isinstance(steps, (list, tuple)):
        return []
    out = []
    for step in steps:
        text = str(step or "").strip()
        if not text:
            continue
        out.append(text[:MAX_STEP_CHARS])
        if len(out) >= MAX_STEPS:
            break
    return out


def _clean_tools(tools) -> list[str]:
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",")]
    if not isinstance(tools, (list, tuple)):
        return []
    out = []
    for tool in tools:
        name = str(tool or "").strip()
        if name and name not in out:
            out.append(name[:80])
        if len(out) >= MAX_TOOLS:
            break
    return out

def _clean_guards(guards) -> list[str]:
    """Preconditions attached to a procedure.

    A guard is prose for a human to read and a marker for code to act on, so it
    is stored as written rather than parsed here — enforcement lives at the point
    a destructive step would run, and this only has to keep them clean and
    bounded. A newline-separated string is accepted because that is how one
    arrives from the skill interface.
    """
    if isinstance(guards, str):
        guards = [g.strip() for g in guards.split("\n")]
    if not isinstance(guards, (list, tuple)):
        return []
    out = []
    for guard in guards:
        text = str(guard or "").strip()
        if text and text not in out:
            out.append(text[:MAX_GUARD_CHARS])
        if len(out) >= MAX_GUARDS:
            break
    return out


def _clean_sop(raw: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    category = clean_category(raw.get("category") or existing.get("category"))
    title = str(raw.get("title") or existing.get("title") or "").strip()
    if not title:
        title = category.replace("_", " ").strip().title() or "Procedure"
    steps = _clean_steps(raw.get("steps") if "steps" in raw
                         else existing.get("steps"))
    tools = _clean_tools(raw.get("tools") if "tools" in raw
                         else existing.get("tools"))
    guards = _clean_guards(raw.get("guards") if "guards" in raw
                           else existing.get("guards"))
    sop = {
        "id": str(existing.get("id") or raw.get("id") or uuid.uuid4().hex[:12]),
        "category": category,
        "title": title[:MAX_TITLE_CHARS],
        "steps": steps,
        "tools": tools,
        "guards": guards,
        # Why this route was chosen, taken from the context the turn ran with.
        # Empty for seeds and for anything learned before this field existed;
        # `_clean_sop` keeps them loadable rather than rejecting the file.
        "reason": str(raw.get("reason") if raw.get("reason") is not None
                      else existing.get("reason") or "")[:MAX_REASON_CHARS].strip(),
        "uses": int(existing.get("uses") or 0),
        "successes": int(existing.get("successes") or 0),
        "created": existing.get("created") or _now(),
        "updated": _now(),
        "source": str(raw.get("source") or existing.get("source") or "manual"),
    }
    if raw.get("uses") is not None:
        try:
            sop["uses"] = max(0, int(raw["uses"]))
        except (TypeError, ValueError):
            pass
    if raw.get("successes") is not None:
        try:
            sop["successes"] = max(0, int(raw["successes"]))
        except (TypeError, ValueError):
            pass
    return sop


# -- load / save --------------------------------------------------------------


def _empty() -> dict:
    return {"version": VERSION, "sops": [], "removed_seeds": []}


def load() -> dict:
    """Read the file. A corrupt file is reported and treated as empty.

    Records written before a field existed come back without it, so anything
    reading a procedure goes through `.get(...)`. `reason` is filled in here
    rather than only in `_clean_sop`, because `_clean_sop` runs on the way IN
    (`upsert`) and a file written by an older build never passes through it.
    """
    file = path()
    if not file.exists():
        return _empty()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s (%s) — starting from empty", file, e)
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("sops"), list):
        log.warning("%s is not in the expected shape — ignoring it", file)
        return _empty()
    sops = []
    for raw in data["sops"]:
        if not isinstance(raw, dict):
            continue
        if "reason" not in raw:
            raw = {**raw, "reason": ""}
        if "guards" not in raw:
            # Records written before guards existed. Defaulted here as well as
            # in `_clean_sop` because that only runs on the way IN, and an
            # existing store never passes through it.
            raw = {**raw, "guards": []}
        sops.append(raw)
    # Carried through explicitly: `removed_seeds` records which seeds the user
    # deleted, and dropping it here would let them all come back on the next
    # start — the top-up would silently undo a deliberate deletion.
    removed = data.get("removed_seeds")
    if not isinstance(removed, list):
        removed = []
    return {"version": VERSION, "sops": sops,
            "removed_seeds": [str(k) for k in removed]}


def save(data: dict) -> None:
    """Write atomically: a half-written file would lose every procedure."""
    file = path()
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": VERSION, "sops": list(data.get("sops") or []),
                   "removed_seeds": list(data.get("removed_seeds") or [])}
        tmp = file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, file)
    except OSError as e:
        log.warning("Could not save procedures to %s: %s", file, e)


# -- queries ------------------------------------------------------------------


def list_all(category: str | None = None) -> list[dict]:
    sops = load()["sops"]
    if category:
        wanted = clean_category(category)
        sops = [s for s in sops if s.get("category") == wanted]
    return sorted(sops, key=lambda s: (-int(s.get("uses") or 0),
                                       s.get("title") or ""))


def get(sop_id: str) -> dict | None:
    for sop in load()["sops"]:
        if sop.get("id") == sop_id:
            return sop
    return None


def find_by_title(category: str, title: str) -> dict | None:
    wanted = clean_category(category)
    wanted_title = str(title or "").strip().lower()
    for sop in load()["sops"]:
        if (sop.get("category") == wanted
                and str(sop.get("title") or "").strip().lower() == wanted_title):
            return sop
    return None


def categories() -> list[dict]:
    """Category names with a count, for the dashboard's left-hand list."""
    counts: dict[str, int] = {}
    for sop in load()["sops"]:
        key = sop.get("category") or "general"
        counts[key] = counts.get(key, 0) + 1
    return [{"category": c, "count": n}
            for c, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def upsert(raw: dict) -> dict:
    """Create or update. Matching is by id, else by category + title."""
    data = load()
    sops = data["sops"]
    sop_id = str(raw.get("id") or "").strip()

    target = None
    if sop_id:
        target = next((s for s in sops if s.get("id") == sop_id), None)
    if target is None and raw.get("title"):
        wanted_category = clean_category(raw.get("category"))
        wanted_title = str(raw["title"]).strip().lower()
        target = next((s for s in sops
                       if s.get("category") == wanted_category
                       and str(s.get("title") or "").strip().lower() == wanted_title),
                      None)

    merged = _clean_sop(raw, target)
    if not merged["steps"] and not merged["tools"]:
        # An empty procedure teaches nothing and only costs tokens later.
        if target is None:
            return {"success": False,
                    "error": "A procedure needs at least one step or tool."}
        merged["steps"] = _clean_steps(target.get("steps")) or ["(no steps recorded)"]

    if target is not None:
        target.update(merged)
        result = target
    else:
        sops.append(merged)
        result = merged

    _trim(sops, merged["category"])
    save(data)
    return {"success": True, "sop": result}


def _trim(sops: list[dict], category: str) -> None:
    """Cap per category, dropping the least-used non-seed entries first."""
    limit = int(_setting("max_per_category", 40) or 40)
    if limit <= 0:
        return
    in_category = [s for s in sops if s.get("category") == category]
    if len(in_category) <= limit:
        return
    in_category.sort(key=lambda s: (int(s.get("uses") or 0),
                                    s.get("updated") or ""))
    # Drop by the SOP's own id, not by `id(s)`. Every other function in this
    # module addresses an entry by its "id" field; using the object identity
    # here worked only because the dicts happen to be alive and distinct for
    # the duration of the call, and would silently drop the wrong entries if
    # the same dict object were ever referenced twice.
    drop = {s.get("id") for s in in_category[:len(in_category) - limit]}
    sops[:] = [s for s in sops if s.get("id") not in drop]


def delete(sop_id: str) -> bool:
    data = load()
    before = len(data["sops"])
    gone = next((s for s in data["sops"] if s.get("id") == sop_id), None)
    data["sops"] = [s for s in data["sops"] if s.get("id") != sop_id]
    if len(data["sops"]) == before:
        return False
    # Remember the deletion by TITLE, not just the id.
    #
    # Seeds are topped up on every start now, so a deleted one would otherwise
    # come straight back — an instruction the user gave and the app ignored. The
    # id is not enough to suppress that: a re-seed mints a fresh id, which is why
    # the record is keyed on category+title, the same pair seeding matches on.
    if gone and str(gone.get("source") or "") == "seed":
        key = f"{gone.get('category')}//{str(gone.get('title') or '').lower()}"
        removed = data.setdefault("removed_seeds", [])
        if key not in removed:
            removed.append(key)
    save(data)
    return True

def removed_seed_keys() -> set[str]:
    """Seeds the user has deleted, which must not be re-added."""
    try:
        data = load()
    except Exception:  # noqa: BLE001
        return set()
    return {str(k) for k in (data.get("removed_seeds") or [])}

def tombstone_seed(category: str, title: str) -> bool:
    """Record a seed as not-to-be-seeded, without deleting anything.

    `delete` can only tombstone a procedure it is removing, which is no use for
    a built-in that a later version simply stopped shipping: there is nothing to
    remove on a fresh install, and on an old one the removal and the record
    should not be forced to happen together. Returns whether the record was new.
    """
    try:
        data = load()
        key = seed_key(category, title)
        removed = data.setdefault("removed_seeds", [])
        if key in removed:
            return False
        removed.append(key)
        save(data)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("Could not tombstone seed %s/%s: %s", category, title, e)
        return False

def seed_key(category: str, title: str) -> str:
    return f"{clean_category(category)}//{str(title or '').lower()}"

def prune_learned(limit: int = MAX_LEARNED) -> int:
    """Drop the least useful learned procedures beyond `limit`. Returns how many.

    Ranked by a use ratio shrunk toward 0.5, so a procedure that has worked
    consistently outranks one that succeeded once, and then by recency. A
    procedure that was learned and never applied is the cheapest thing to lose,
    and that is exactly what a run that was recorded once and never repeated
    leaves behind.

    Only `source: "learned"` is considered. Seeds and anything the user wrote by
    hand are exempt, so this can never remove a built-in or the user's own work.
    """
    try:
        data = load()
        learned = [s for s in data["sops"] if s.get("source") == "learned"]
        if len(learned) <= limit:
            return 0

        def keeper_rank(sop: dict) -> tuple[float, str]:
            uses = int(sop.get("uses") or 0)
            wins = int(sop.get("successes") or 0)
            ratio = (wins + 1.0) / (uses + 2.0) if uses else 0.5
            # Weighted by how often it has actually been used, so a 100%-of-2
            # recipe does not outrank a 90%-of-50 one.
            return (ratio * (1.0 + min(uses, 20) / 20.0),
                    str(sop.get("updated") or sop.get("created") or ""))

        ranked = sorted(learned, key=keeper_rank, reverse=True)
        keep_ids = {str(s.get("id")) for s in ranked[:limit]}
        before = len(data["sops"])
        data["sops"] = [s for s in data["sops"]
                        if s.get("source") != "learned"
                        or str(s.get("id")) in keep_ids]
        dropped = before - len(data["sops"])
        if dropped:
            save(data)
            log.info("Pruned %d learned procedure(s) over the cap of %d",
                     dropped, limit)
        return dropped
    except Exception as e:  # noqa: BLE001
        log.debug("Could not prune learned procedures: %s", e)
        return 0


def record_use(sop_id: str, success: bool = True) -> None:
    """Note that a procedure was applied, and whether the run worked out."""
    data = load()
    for sop in data["sops"]:
        if sop.get("id") == sop_id:
            sop["uses"] = int(sop.get("uses") or 0) + 1
            if success:
                sop["successes"] = int(sop.get("successes") or 0) + 1
            sop["updated"] = _now()
            save(data)
            return


def describe() -> dict:
    """What the Settings panel needs: is it on, and how much has it learned."""
    data = load()
    sops = data["sops"]
    learned = [s for s in sops if s.get("source") == "learned"]
    return {
        "enabled": enabled(),
        "learn": learning_enabled(),
        "dir": str(base_dir()),
        "count": len(sops),
        "learned": len(learned),
        "seeded": len([s for s in sops if s.get("source") == "seed"]),
        "categories": categories(),
    }
