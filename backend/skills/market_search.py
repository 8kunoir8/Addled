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
import re
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
            # utf-8, not the locale codec. On Windows text=True decodes as
            # cp1252, and GitHub descriptions carry characters cp1252 has no
            # mapping for (a byte 0x8f in one response). That raised inside the
            # reader thread, killed the gh path, and dropped every search to the
            # unauthenticated REST API - which is rate limited, so the market
            # returned nothing and no skill could ever be installed.
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
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


def _search_terms(query: str) -> list[str]:
    """Reduce a query to the words worth searching GitHub for.

    GitHub's repo search is a keyword match, not a question answerer: the
    phrase "extract text from a pdf file" matched nothing at all, while "pdf"
    matched eight repositories. Words that carry no topical signal are dropped
    and the rest are offered longest first.

    Underscores and hyphens split words because the caller is usually a tool
    name the model invented - "extract_pdf_text" has to become "extract pdf"
    or GitHub sees one nonsense token and matches nothing.
    """
    words = re.findall(r"[A-Za-z0-9]{3,}", query.lower())
    useful = [w for w in words if w not in _GH_STOPWORDS]
    return sorted(useful, key=len, reverse=True) or words


_GH_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "you", "can", "get", "use", "using", "make", "new", "how", "what",
    "skill", "skills", "tool", "tools", "file", "files", "text", "data",
    "python", "library", "function", "called", "implement", "based",
    "user", "want", "wants", "need", "needs", "help", "please", "them",
    "then", "than", "when", "where", "which", "about", "some", "something",
    "their", "there", "here", "have", "has", "its", "out", "all", "any",
}


async def search(query: str, limit: int = 8) -> list[dict]:
    """Rank market skill repos for a query. Results cached for 1h."""
    now = time.time()
    hit = _cache.get(query)
    if hit and now - hit[0] < 3600:
        return hit[1]

    repos = _gh_search_repos(query, limit=max(8, limit * 2))
    if not repos:
        # A long question matches nothing; GitHub wants keywords. Retry with
        # the most substantial words, then with fewer of them.
        terms = _search_terms(query)
        tried = {query}
        for count in (3, 2, 1):
            joined = " ".join(terms[:count])
            if not joined or joined in tried:
                continue
            tried.add(joined)
            repos = _gh_search_repos(joined, limit=max(8, limit * 2))
            if repos:
                log.info("Market query %r matched nothing; %r found %d",
                         query, joined, len(repos))
                break
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


async def find_match(skill_name: str,
                     threshold: float = 0.45) -> dict | None:
    """The best market skill for this name, WITHOUT installing it.

    Split out so a caller can ask the user before any code is fetched. The
    install used to happen inside the search, which meant a match installed
    itself and ran while the user was never told — the loop even set
    `"market": True` and `"installed_skill"`, and nothing read either. Consent
    needs a moment between finding and installing, and this is it.

    Returns the candidate (name, repo, description, similarity, url) or None.
    """
    from backend.config import config
    if not config.get("skills", "market_search", default=True):
        return None

    results = await search(skill_name, limit=4)
    if not results:
        return None

    from backend.memory.embedding import embed_text_async
    try:
        name_vector = await embed_text_async(skill_name)
    except Exception:
        return None

    best = None
    best_sim = -1.0
    for r in results:
        try:
            sim = float(name_vector @ await embed_text_async(r["name"]))
        except Exception:
            sim = 0.0
        if sim > best_sim:
            best, best_sim = r, sim
    # The cutoff is the caller's threshold (skills.market_sim_threshold). It used
    # to be a hardcoded 0.4, which meant the setting could not be tuned at all.
    if best is None or best_sim < threshold:
        return None
    return {**best, "similarity": round(best_sim, 3)}


def install_match(candidate: dict) -> str | None:
    """Install a candidate `find_match` returned. Returns the skill's name.

    Separate from the search so the decision to install is a distinct, callable
    act rather than a side effect of looking.
    """
    repo = str((candidate or {}).get("repo") or "").strip()
    if not repo:
        return None
    try:
        from backend.skills.market import market
        meta = market.install_from_github(repo)
        log.info("Installed market skill '%s' from %s", meta["name"], repo)
        return meta["name"]
    except Exception as e:
        log.warning("Market install failed for %s: %s", repo, e)
        return None


async def search_and_install(skill_name: str,
                             threshold: float = 0.45) -> str | None:
    """Search markets for a missing skill; install the best match.

    Kept as the one-call form for callers that have already established consent
    (or do not need it, like the dashboard's own install button). It is now a
    composition of `find_match` and `install_match`, so there is exactly one
    implementation of each half and the pair cannot drift apart.
    """
    candidate = await find_match(skill_name, threshold)
    if not candidate:
        return None
    return install_match(candidate)
