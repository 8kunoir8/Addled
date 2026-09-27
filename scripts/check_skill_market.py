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


def installer_checks() -> None:
    """Verify market installer upgrades: 1MB limit, URL normalization, HTML guard."""
    from backend.skills.market import market, parse_skill_md, Path
    import tempfile

    # 1. Skill > 64KB (e.g. 120KB) parses cleanly
    large_body = "# large-skill\n" + ("Lots of documentation and detailed guidance.\n" * 2500)
    assert len(large_body.encode("utf-8")) > 64 * 1024
    meta = parse_skill_md(large_body)
    check("parse_skill_md accepts markdown > 64KB", meta["name"] == "large-skill", len(large_body))

    # 2. SKILL.md > 1MB is rejected by _register_dir
    huge_body = "# huge-skill\n" + ("x" * (1024 * 1024 + 100))
    rejected_dir = False
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "SKILL.md").write_text(huge_body, encoding="utf-8")
        try:
            market._register_dir(tdp)
        except ValueError as e:
            rejected_dir = "too large" in str(e)
    check("SKILL.md > 1MB is rejected by _register_dir", rejected_dir)

    # 3. GitHub tree URL dispatches to install_from_github with ref and path
    calls: list[tuple] = []

    def fake_install_from_github(repo, path="", ref=""):
        calls.append((repo, path, ref))
        return {"name": "tree-skill"}

    with mock.patch.object(market, "install_from_github", fake_install_from_github):
        market.install_from_url("https://github.com/owner/my-repo/tree/main/skills/my-skill")
    check("GitHub tree URL dispatches to install_from_github with ref and path",
          calls == [("owner/my-repo", "skills/my-skill", "main")], calls)

    # 4. GitHub repo root URL dispatches to install_from_github
    calls.clear()
    with mock.patch.object(market, "install_from_github", fake_install_from_github):
        market.install_from_url("https://github.com/owner/root-repo.git")
    check("GitHub repo root URL dispatches to install_from_github",
          calls == [("owner/root-repo", "", "")], calls)

    # 5. GitHub blob URL for SKILL.md attempts install_from_github first with parent folder
    calls.clear()
    with mock.patch.object(market, "install_from_github", fake_install_from_github):
        market.install_from_url("https://github.com/owner/my-repo/blob/master/skills/foo/SKILL.md")
    check("GitHub blob URL for SKILL.md dispatches to install_from_github for companion scripts",
          calls == [("owner/my-repo", "skills/foo", "master")], calls)

    # 6. HTML response rejection
    class FakeHtmlResp:
        def __init__(self):
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
        def read(self, limit):
            return b"<!DOCTYPE html><html><body>GitHub Page</body></html>"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    html_err = ""
    with mock.patch("urllib.request.urlopen", return_value=FakeHtmlResp()):
        try:
            market.install_from_url("https://some-server.com/not-a-skill.html")
        except ValueError as e:
            html_err = str(e)
    check("HTML response is rejected with clear guidance",
          "HTML webpage" in html_err, html_err)


def non_skill_md_checks() -> None:
    """Verify non-SKILL.md file discovery, fallback naming, and script selection."""
    from backend.skills.market import (
        market,
        parse_skill_md,
        _find_candidate_file_in_dir,
        _find_gh_skill_entry,
        _pick_script,
        Path,
    )
    import tempfile

    # 1. Candidate file detection in local directory
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # Empty dir returns None
        check("_find_candidate_file_in_dir returns None for empty dir",
              _find_candidate_file_in_dir(tdp) is None)

        # README.md detected when no SKILL.md
        (tdp / "README.md").write_text("# Readme Skill\nA cool tool", encoding="utf-8")
        cand = _find_candidate_file_in_dir(tdp)
        check("_find_candidate_file_in_dir finds README.md",
              cand is not None and cand.name == "README.md")

        # SKILL.md takes priority over README.md
        (tdp / "SKILL.md").write_text("# Official Skill", encoding="utf-8")
        cand = _find_candidate_file_in_dir(tdp)
        check("_find_candidate_file_in_dir prioritizes SKILL.md over README.md",
              cand is not None and cand.name == "SKILL.md")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "coder.agent.md").write_text("# Coder Agent", encoding="utf-8")
        cand = _find_candidate_file_in_dir(tdp)
        check("_find_candidate_file_in_dir finds *.agent.md",
              cand is not None and cand.name == "coder.agent.md")

    # 2. GitHub file listing discovery
    gh_files = [
        {"name": "helper.py", "type": "file"},
        {"name": "readme.md", "type": "file", "download_url": "http://example.com/readme.md"},
        {"name": "custom.agent.md", "type": "file", "download_url": "http://example.com/agent.md"},
    ]
    gh_entry = _find_gh_skill_entry(gh_files)
    check("_find_gh_skill_entry finds readme.md before agent.md",
          gh_entry is not None and gh_entry["name"] == "readme.md")

    # 3. Script entrypoint selection
    picked = _pick_script(["util.py", "main.py", "zebra.py"], "myskill")
    check("_pick_script prefers main.py over alphabetical first", picked == "main.py", picked)
    picked_named = _pick_script(["util.py", "myskill.py", "main.py"], "myskill")
    check("_pick_script prefers <skill_name>.py over main.py", picked_named == "myskill.py", picked_named)

    # 4. parse_skill_md with fallback name and h2/h3 headings
    meta = parse_skill_md("## Secondary Heading\nThis tool processes data.\nMore lines.", fallback_name="fallback-name")
    check("parse_skill_md parses ## heading as name", meta["name"] == "Secondary Heading")
    check("parse_skill_md extracts first non-header line as description",
          meta["description"] == "This tool processes data.", meta["description"])

    meta_no_head = parse_skill_md("Just instructions without heading.", fallback_name="inferred-skill")
    check("parse_skill_md uses fallback_name when no header exists",
          meta_no_head["name"] == "inferred-skill", meta_no_head["name"])

    # 5. _register_dir on folder with only README.md
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "README.md").write_text("# Readme Only\nWorks without SKILL.md", encoding="utf-8")
        res = market._register_dir(tdp)
        check("_register_dir succeeds on README.md only folder", res["name"] == "readme-only", res)

    # 6. install_from_github with README.md target path
    fake_contents = [
        {"name": "README.md", "type": "file", "download_url": "https://example.com/README.md"},
        {"name": "main.py", "type": "file", "download_url": "https://example.com/main.py", "size": 100},
    ]
    with mock.patch.object(market, "_gh_contents", return_value=fake_contents), \
         mock.patch.object(market, "_download_text", return_value="# GH Tool\nGitHub tool instructions"), \
         mock.patch.object(market, "_download_bytes", return_value=b"print('hello')"):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("backend.skills.market.MARKET_DIR", Path(td)):
                inst_res = market.install_from_github("acme/gh-tool", "README.md")
                check("install_from_github with README.md installs and discovers main.py",
                      inst_res["name"] == "gh-tool" and "main.py" in inst_res.get("scripts", []),
                      inst_res)


def main() -> int:
    from backend.skills import market_search

    encoding_checks(market_search)
    query_checks(market_search)
    threshold_checks(market_search)
    installer_checks()
    non_skill_md_checks()
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: skill market discovery, query reduction and install gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
