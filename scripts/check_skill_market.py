"""Checks for the skill market: the path that lets Addled learn a new skill.

It never worked, and this is why: `_gh_search_repos` ran the `gh` CLI with
``text=True`` and no encoding, so on Windows the output was decoded as cp1252.
GitHub descriptions contain bytes cp1252 has no mapping for (0x8f in one
response), which raised inside subprocess's reader thread and killed the gh
path. Every search then fell through to the unauthenticated REST API, hit its
rate limit, and returned nothing — so nothing could ever be discovered or
installed. ``forged_skills`` and ``market_skills`` were both empty.

Two smaller faults sat behind it: a whole question was passed to GitHub's repo
search, which is a keyword index ("extract text from a pdf file" matched
nothing, "pdf" matched sixteen), and the similarity cutoff was hardcoded, so
``skills.market_sim_threshold`` did nothing.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_skill_market.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


REPOS = [
    {"repo": "acme/pdf-tools", "name": "pdf-tools",
     "description": "Extract text from PDFs — with a dash"},
    {"repo": "acme/other", "name": "other", "description": "unrelated"},
]


def encoding_checks(market_search) -> None:
    """The gh call must not decode as the locale codec."""
    seen: list[dict] = []

    def fake_run(args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(
            args, 0,
            stdout=json.dumps([{"fullName": "acme/pdf-tools",
                                "description": "Extract text — dashes"}]),
            stderr="")

    with mock.patch.object(market_search.subprocess, "run", fake_run):
        repos = market_search._gh_search_repos("pdf", limit=8)

    check("the gh call returns its repositories", len(repos) == 1, repos)
    check("the gh call asks for utf-8, not the locale codec",
          seen and seen[0].get("encoding") == "utf-8",
          seen[0] if seen else "subprocess.run not called")
    check("and cannot die on an undecodable byte",
          bool(seen) and seen[0].get("errors") == "replace",
          seen[0].get("errors") if seen else None)


def query_checks(market_search) -> None:
    terms = market_search._search_terms(
        "extract text from a pdf file for the user")
    check("a question reduces to its topical words",
          "pdf" in terms and "extract" in terms
          and "the" not in terms and "user" not in terms, terms)
    check("the most substantial word is offered first", terms[0] == "extract",
          terms)
    single = market_search._search_terms("pdf")
    check("a single word is left alone", single == ["pdf"], single)
    invented = market_search._search_terms("extract_pdf_text")
    check("an invented tool name splits into keywords",
          invented == ["extract", "pdf"], invented)
    hyphenated = market_search._search_terms("convert-heic-to-png")
    check("a hyphenated name splits too",
          "heic" in hyphenated and "png" in hyphenated, hyphenated)
    empty = market_search._search_terms("the and for")
    check("a question of only stopwords still yields something", bool(empty),
          empty)

    # A long query that GitHub cannot match must fall back to keywords.
    calls: list[str] = []

    def fake_repos(query, limit=8):
        calls.append(query)
        if query == "extract text from a pdf file":
            return []
        return list(REPOS) if "pdf" in query else []

    with mock.patch.object(market_search, "_gh_search_repos", fake_repos), \
         mock.patch.object(market_search, "_cache", {}):
        results = asyncio.run(
            market_search.search("extract text from a pdf file"))

    check("a question that matches nothing falls back to keywords",
          len(results) > 0, f"calls={calls}")
    check("and the fallback used fewer words",
          len(calls) > 1 and len(calls[1].split()) < len(calls[0].split()),
          calls)


def threshold_checks(market_search) -> None:
    """skills.market_sim_threshold must actually gate the install.

    Asserting only that a weak match is refused would pass even if the gate
    never opened at all, so this watches for the install call itself.
    """
    async def fake_search(query, limit=4):
        return list(REPOS)

    async def fake_embed(text):
        class Vector:
            def __matmul__(self, other):
                return 0.30        # below the high threshold, above the low one
        return Vector()

    installed: list[str] = []

    def fake_install(repo):
        installed.append(repo)
        return {"name": "pdf-tools"}

    with mock.patch.object(market_search, "search", fake_search), \
         mock.patch("backend.memory.embedding.embed_text_async", fake_embed), \
         mock.patch("backend.skills.market.market") as fake_market:
        fake_market.install_from_github.side_effect = fake_install
        refused = asyncio.run(market_search.search_and_install("pdf", 0.45))
        refused_installs = list(installed)
        installed.clear()
        allowed = asyncio.run(market_search.search_and_install("pdf", 0.10))
        allowed_installs = list(installed)

    check("a weak match is refused at the configured threshold",
          refused is None, refused)
    check("and nothing is installed for it", refused_installs == [],
          refused_installs)
    check("a low enough threshold lets the match through",
          allowed == "pdf-tools", allowed)
    check("and the install is actually attempted",
          allowed_installs == ["acme/pdf-tools"], allowed_installs)


def main() -> int:
    from backend.skills import market_search

    encoding_checks(market_search)
    query_checks(market_search)
    threshold_checks(market_search)
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: skill market discovery, query reduction and install gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
