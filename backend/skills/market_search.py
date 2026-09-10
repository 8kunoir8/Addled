"""
Market discovery — find SKILL.md skills in public GitHub markets and
auto-install the best match when Addled lacks a skill.

Sources: GitHub repo search (topic `claude-skills`) via the authed `gh` CLI
when available, REST API fallback. Candidates are ranked locally with the
MiniLM embedder — no provider credits.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.parse
import urllib.request

log = logging.getLogger("addled.market_search")

UA = {"User-Agent": "Addled/1.0 (skill market search)"}
TOPIC = "claude-skills"
_cache: dict = {}   # query -> (ts, results)


def _gh_search_repos(query: str, limit: int = 8) -> list[dict]:
    """Repo search: gh CLI first (authed, no rate limits), REST fallback."""
    candidates: list[dict] = []
    try:
        r = subprocess.run(
            ["gh", "search", "repos", f"--topic={TOPIC}", query,
             "--limit", str(limit), "--json", "fullName,description"],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            for it in json.loads(r.stdout or "[]"):
                candidates.append({
                    "repo": it.get("fullName", ""),
                    "name": (it.get("fullName") or "").split("/")[-1],
                    "description": it.get("description") or "",
                })
            return candidates
    except Exception as e:
        log.debug("gh search failed: %s", e)

    try:
        url = ("https://api.github.com/search/repositories?q="
               + urllib.parse.quote(f"topic:{TOPIC} {query}")
               + f"&per_page={limit}")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        for it in data.get("items", []):
            candidates.append({
                "repo": it.get("full_name", ""),
                "name": it.get("name", ""),
                "description": it.get("description") or "",
            })
    except Exception as e:
        log.debug("REST repo search failed: %s", e)
    return candidates


async def search(query: str, limit: int = 8) -> list[dict]:
    """Rank market skill repos for a query. Results cached for 1h."""
    now = time.time()
    hit = _cache.get(query)
    if hit and now - hit[0] < 3600:
        return hit[1]

    repos = _gh_search_repos(query, limit=max(8, limit * 2))
    if not repos:
        return []

    from backend.memory.embedding import embed_text_async
    try:
        qv = await embed_text_async(query)
    except Exception:
        qv = None

    scored = []
    for r in repos:
        text = f"{r['name']} {r['description']}"
        sim = 0.0
        if qv is not None:
            try:
                v = await embed_text_async(text)
                sim = float(qv @ v)
            except Exception:
                sim = 0.0
        scored.append({**r, "path": "",
                       "similarity": round(sim, 3),
                       "url": f"https://github.com/{r['repo']}"})
    scored.sort(key=lambda x: -x["similarity"])
    out = scored[:limit]
    _cache[query] = (now, out)
    return out


async def search_and_install(skill_name: str,
                             threshold: float = 0.45) -> str | None:
    """Search markets for a missing skill; install the best match.
    Returns the installed skill's registry name, or None."""
    from backend.config import config
    if not config.get("skills", "market_search", default=True):
        return None

    results = await search(skill_name, limit=4)
    if not results:
        return None

    from backend.memory.embedding import embed_text_async
    best = None
    best_sim = -1.0
    for r in results:
        try:
            nv = await embed_text_async(skill_name)
            cv = await embed_text_async(r["name"])
            sim = float(nv @ cv)
        except Exception:
            sim = 0.0
        if sim > best_sim:
            best, best_sim = r, sim
    if best is None or best_sim < 0.4:
        return None

    try:
        from backend.skills.market import market
        meta = market.install_from_github(best["repo"])
        log.info("Installed market skill '%s' from %s (sim=%.2f)",
                 meta["name"], best["repo"], best_sim)
        return meta["name"]
    except Exception as e:
        log.warning("Market install failed for %s: %s", skill_name, e)
        return None
