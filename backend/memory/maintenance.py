"""
Idle-time memory maintenance — runs on the engine tick in the background.

Jobs (each config-gated and independently throttled):
  reembed — migrate legacy hash vectors to semantic embeddings (batches)
  compact — rolling conversation compaction
  facts   — auto-extract durable facts via the LLM (only if memory.auto_facts)
  triples — extract knowledge-graph triples (only if memory.graph_extract)
  dedup   — drop near-duplicate conversation vectors
  expire  — prune vectors older than memory.max_age_days
  links   — drop relations whose target no longer exists, re-mirror the wiki
  linkdup — mark near-identical facts as same_as
  wiki    — lint the wiki (broken links, orphans, stubs)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.maintenance")

from backend import app_paths

STATE_PATH = app_paths.MEMORY_DIR / "maintenance_state.json"
_running = False

INTERVALS = {  # seconds between runs of each job
    "reembed": 5 * 60,
    "compact": 5 * 60,
    "facts": 30 * 60,
    "triples": 30 * 60,
    "dedup": 24 * 3600,
    "expire": 24 * 3600,
    "links": 24 * 3600,
    "linkdup": 24 * 3600,
    "wiki": 24 * 3600,
}


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _due(name: str, state: dict) -> bool:
    return time.time() - float(state.get(name, 0)) >= INTERVALS.get(name, 3600)


async def run_maintenance() -> dict:
    """Run due maintenance jobs (single-flight). Returns per-job results."""
    global _running
    if _running:
        return {"skipped": "already running"}
    _running = True
    results: dict = {}
    try:
        from backend.config import config
        state = _load_state()

        # 1. Semantic re-embed of legacy rows (batched; idle otherwise)
        if config.get("memory", "semantic_embeddings", default=True) and \
                _due("reembed", state):
            from backend.memory.reembed import reembed_legacy_batch
            done = await reembed_legacy_batch(max_rows=100)
            if done == 0:
                state["reembed"] = time.time()  # nothing left — stop polling
            results["reembed"] = done

        # 2. Rolling compaction
        if _due("compact", state):
            from backend.memory.compaction import maybe_compact
            did = await maybe_compact()
            state["compact"] = time.time()
            results["compact"] = did

        # 3. Auto fact extraction (opt-in — uses provider credits)
        if config.get("memory", "auto_facts", default=False) and \
                _due("facts", state):
            from backend.memory.facts import auto_extract_facts
            added = await auto_extract_facts()
            state["facts"] = time.time()
            results["facts"] = added

        # 4. Knowledge-graph triples (opt-in — uses provider credits)
        if config.get("memory", "graph_extract", default=False) and \
                _due("triples", state):
            from backend.memory.knowledge_graph import extract_triples
            added = await extract_triples()
            state["triples"] = time.time()
            results["triples"] = added

        # 5. Dedup + expire
        if _due("dedup", state):
            from backend.memory.vector_store import vector_store
            removed = vector_store.dedup(
                category="conversation",
                min_sim=float(config.get("memory", "dedup_min_sim",
                                         default=0.95)))
            state["dedup"] = time.time()
            results["dedup"] = removed
        if _due("expire", state):
            from backend.memory.vector_store import vector_store
            days = float(config.get("memory", "max_age_days", default=365))
            removed = vector_store.expire(category="conversation",
                                          max_age_h=days * 24)
            state["expire"] = time.time()
            results["expire"] = removed

        # 6. Relations: forgetting a memory leaves its edges behind, and a file
        # that was moved or deleted is the common case. Re-mirroring the wiki
        # afterwards is cheap and keeps its links in step with the pages.
        if config.get("links", "enabled", default=True) and \
                _due("links", state):
            from backend.memory.links import link_store
            report = link_store.prune()
            try:
                from backend.wiki import store as wiki_store
                if wiki_store.enabled():
                    mirror = wiki_store.reindex_links()
                    report.update({f"wiki_{k}": v for k, v in mirror.items()
                                   if k != "available"})
            except Exception as e:
                log.debug("wiki re-mirror failed: %s", e)
            state["links"] = time.time()
            results["links"] = report

        # 7. The same fact recorded twice in different words.
        if config.get("links", "dedup_links", default=True) and \
                _due("linkdup", state):
            from backend.memory.autolink import link_near_duplicates
            marked = link_near_duplicates(
                min_sim=float(config.get("memory", "dedup_min_sim",
                                         default=0.92)))
            state["linkdup"] = time.time()
            results["linkdup"] = marked

        # 8. Wiki hygiene — reported, never destructive.
        if config.get("wiki", "enabled", default=True) and _due("wiki", state):
            from backend.wiki import store as wiki_store
            if wiki_store.enabled():
                report = wiki_store.lint()
                results["wiki"] = {
                    "pages": report["pages"],
                    "broken_links": len(report["broken_links"]),
                    "orphans": len(report["orphans"]),
                    "empty": len(report["empty"]),
                }
                if not report["healthy"]:
                    log.info("Wiki lint: %d broken link(s), %d orphan(s), "
                             "%d stub(s)", len(report["broken_links"]),
                             len(report["orphans"]), len(report["empty"]))
            state["wiki"] = time.time()

        _save_state(state)
    except Exception as e:
        log.warning("maintenance run failed: %s", e)
        results["error"] = str(e)
    finally:
        _running = False
    return results
