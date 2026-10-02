"""
Market skill loader — imports SKILL.md-format skills (Claude Code, Copilot,
opencode, ...) into Addled's registry.

Skill folder layout:
  memory/market_skills/<name>/
      SKILL.md          # YAML frontmatter (name, description, allowed-tools,
                        # license, version) + markdown instructions
      <script>.py       # optional bundled script(s)

Two skill flavors:
  - Instruction skills (no scripts): the markdown body is returned to the
    model as guidance when the skill is called. Safe — auto-approved.
  - Script skills: the bundled script runs via TerminalExecutor. Gated by
    config skills.allow_script_skills (default False) — market code never
    runs without explicit opt-in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from backend.skills.registry import SkillDefinition, skill_registry

log = logging.getLogger("addled.market")

from backend import app_paths

MARKET_DIR = app_paths.subdir("market_skills")
MAX_SKILL_MD = 1024 * 1024
MAX_SCRIPT = 1024 * 1024
MAX_FILES = 20

def installed_dir(name: str) -> Path | None:
    """Where an installed skill lives, or None if it is not one.

    Public because the approval policy needs it: "Always allow" is granted to a
    skill's *code*, not merely to its name, so the policy has to be able to find
    the files it is binding the grant to.
    """
    clean = str(name or "").strip()
    if not clean or clean in (".", "..") or "/" in clean or "\\" in clean:
        return None
    folder = MARKET_DIR / clean
    try:
        return folder if folder.is_dir() else None
    except OSError:
        return None

def folder_digest(folder: Path) -> str:
    """A digest of every file in an installed skill, by name and content.

    The point is to notice that the skill is not the one that was allowed. File
    names and bytes both go in, so a swapped script, an added file or an edited
    SKILL.md all change the answer. Order is fixed by sorting, so the digest
    does not depend on how the filesystem happens to enumerate a directory.
    """
    try:
        digest = hashlib.sha256()
        files = sorted(p for p in folder.rglob("*") if p.is_file())
        for path in files:
            try:
                relative = path.relative_to(folder).as_posix()
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
            except OSError as e:
                # A file that cannot be read makes the digest untrustworthy
                # rather than merely incomplete, so the whole thing fails and
                # the caller treats the skill as un-fingerprinted.
                log.debug("could not read %s: %s", path, e)
                return ""
        if not files:
            return ""
        return digest.hexdigest()[:32]
    except Exception as e:  # noqa: BLE001
        log.debug("could not digest %s: %s", folder, e)
        return ""

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
UA = {"User-Agent": "Addled/1.0 (skill market)"}

_GH_BLOB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)$",
    re.IGNORECASE,
)
_GH_TREE_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/tree/([^/]+)/?(.*)$",
    re.IGNORECASE,
)
_GH_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_GH_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.*)$",
    re.IGNORECASE,
)
_GH_RAW_DIRECT_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/raw/([^/]+)/(.*)$",
    re.IGNORECASE,
)
_RESERVED_GH_OWNERS = {
    "features", "pricing", "security", "enterprise", "explore",
    "topics", "collections", "trending", "login", "signup", "settings",
}


# Candidate skill markdown files in priority order
_CANDIDATE_EXACT = ("skill.md", "readme.md")
_CANDIDATE_SUFFIXES = (".agent.md", ".prompt.md", ".instructions.md", ".md")


def _find_candidate_file_in_dir(d: Path) -> Path | None:
    """Find skill markdown file in a local directory by priority."""
    children = [p for p in d.iterdir() if p.is_file()]
    # 1. Exact matches (case-insensitive)
    for target in _CANDIDATE_EXACT:
        for child in children:
            if child.name.lower() == target:
                return child

    # 2. Prompt / agent / instruction suffixes, then any .md
    for suffix in _CANDIDATE_SUFFIXES:
        for child in sorted(children, key=lambda p: p.name.lower()):
            if child.name.lower().endswith(suffix):
                return child
    return None


def _find_gh_skill_entry(files: list[dict]) -> dict | None:
    """Find skill markdown entry in a GitHub file listing by priority."""
    for target in _CANDIDATE_EXACT:
        entry = next((f for f in files if f.get("name", "").lower() == target), None)
        if entry:
            return entry

    for suffix in _CANDIDATE_SUFFIXES:
        entry = next(
            (f for f in files if f.get("name", "").lower().endswith(suffix)),
            None,
        )
        if entry:
            return entry
    return None


def _pick_script(scripts: list[str], skill_name: str) -> str:
    """Pick preferred entrypoint script over naive alphabetical order."""
    if not scripts:
        return ""
    lowered = {s.lower(): s for s in scripts}
    for candidate in (f"{skill_name.lower()}.py", "main.py", "run.py", "cli.py", "tool.py"):
        if candidate in lowered:
            return lowered[candidate]
    return scripts[0]


def _safe_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip().lower())
    name = name.strip("-")
    if not name:
        raise ValueError("skill name is empty after sanitizing")
    return name[:60]


def parse_skill_md(text: str, fallback_name: str = "") -> dict:
    """Parse skill markdown into a normalized dict."""
    m = _FRONTMATTER_RE.match(text.strip())
    if not m:
        body = text.strip()
        title_match = re.search(r"^\s*#{1,3}\s+(.+?)\s*$", body, re.M)
        name = title_match.group(1).strip() if title_match else fallback_name.strip()
        if not name:
            raise ValueError("skill markdown has no frontmatter, no heading, and no fallback name")
        desc = ""
        for line in body.splitlines():
            line_s = line.strip()
            if line_s and not line_s.startswith("#") and not line_s.startswith("```"):
                desc = line_s[:150]
                break
        return {
            "name": name,
            "description": desc,
            "allowed_tools": [],
            "license": "",
            "version": "",
            "instructions": body,
        }
    raw_meta, body = m.group(1), m.group(2)
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"bad frontmatter: {e}")
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    name = str(meta.get("name") or "").strip()
    if not name:
        title_match = re.search(r"^\s*#{1,3}\s+(.+?)\s*$", body, re.M)
        name = title_match.group(1).strip() if title_match else fallback_name.strip()
    if not name:
        raise ValueError("frontmatter missing 'name'")
    allowed = meta.get("allowed-tools") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    desc = str(meta.get("description", "")).strip()
    if not desc:
        for line in body.splitlines():
            line_s = line.strip()
            if line_s and not line_s.startswith("#") and not line_s.startswith("```"):
                desc = line_s[:150]
                break
    return {
        "name": name,
        "description": desc,
        "allowed_tools": list(allowed),
        "license": str(meta.get("license", "")),
        "version": str(meta.get("version", "")),
        "instructions": body.strip(),
    }


def _instruction_handler(instructions: str):
    async def _handler(params: dict) -> dict:
        return {"success": True, "instructions": instructions}
    return _handler


def _script_handler(skill_name: str, script_rel: str, base_dir: Path):
    async def _handler(params: dict) -> dict:
        from backend.config import config
        if not config.get("skills", "allow_script_skills", default=False):
            return {"success": False,
                    "error": "Script skills require opt-in (Settings → Tools)."}
        from backend.actions.terminal import TerminalExecutor
        script_path = base_dir / script_rel
        if not script_path.is_file():
            return {"success": False, "error": f"Script missing: {script_rel}"}
        args = params.get("args") or []
        if isinstance(args, str):
            args = [args]
        # Passed as an ARGV, not folded into a command string. The previous
        # `f'python "{script_path}" {quoted}'` went to `powershell -Command`,
        # where wrapping an argument in quotes is not escaping: an arg of
        # `$(Remove-Item -Recurse -Force C:\)` or `"; del *` executed as code,
        # making every installed script skill an arbitrary-command primitive.
        return await TerminalExecutor().execute_argv(
            [sys.executable, str(script_path), *[str(a) for a in args]],
            cwd=str(base_dir), timeout=120)
    return _handler


class SkillMarket:
    """Discovers, installs and manages SKILL.md market skills."""

    def __init__(self):
        self._skills: dict[str, dict] = {}
        MARKET_DIR.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _load_all(self):
        for d in sorted(MARKET_DIR.glob("*/")):
            try:
                self._register_dir(d)
            except Exception as e:
                log.warning("Failed to load market skill %s: %s", d.name, e)

    def _register_dir(self, d: Path) -> dict:
        md = _find_candidate_file_in_dir(d)
        if not md or not md.is_file():
            raise ValueError("no skill documentation found in folder (need SKILL.md, README.md, or .md file)")
        text = md.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > MAX_SKILL_MD:
            raise ValueError(f"SKILL.md too large (maximum size is {MAX_SKILL_MD // (1024 * 1024)}MB)")
        meta = parse_skill_md(text, fallback_name=d.name)
        name = _safe_name(meta["name"])
        scripts = sorted(p.name for p in d.iterdir()
                         if p.is_file() and p.suffix.lower() == ".py")
        meta["name"] = name
        meta["scripts"] = scripts
        meta["dir"] = d
        if scripts:
            script_file = _pick_script(scripts, name)
            handler = _script_handler(name, script_file, d)
            requires_approval = True
        else:
            handler = _instruction_handler(meta["instructions"])
            requires_approval = False
        skill = SkillDefinition(
            name=name,
            description=meta["description"] or f"Market skill: {name}",
            parameters={"type": "object", "properties": {
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "Arguments passed to the script"}}
                if scripts else {},
                "required": []},
            handler=handler,
            category="market",
            requires_approval=requires_approval,
        )
        skill_registry.register(skill)
        self._skills[name] = meta
        log.info("Loaded market skill: %s (%s)",
                 name, "script" if scripts else "instructions")
        return meta

    # ---- install paths ----------------------------------------------------

    def install_from_text(self, text: str, fallback_name: str = "") -> dict:
        meta = parse_skill_md(text, fallback_name=fallback_name)
        name = _safe_name(meta["name"])
        d = MARKET_DIR / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        return self._register_dir(d)

    def install_from_url(self, url: str) -> dict:
        url = (url or "").strip()
        if not url:
            raise ValueError("URL is empty")

        # 1. GitHub tree URL: github.com/owner/repo/tree/branch/path
        m_tree = _GH_TREE_RE.match(url)
        if m_tree:
            owner, repo_name, ref, path = m_tree.groups()
            repo = f"{owner}/{repo_name.removesuffix('.git')}"
            return self.install_from_github(repo, path, ref=ref)

        # 2. GitHub blob URL: github.com/owner/repo/blob/branch/path
        m_blob = _GH_BLOB_RE.match(url)
        if m_blob:
            owner, repo_name, ref, path = m_blob.groups()
            repo = f"{owner}/{repo_name.removesuffix('.git')}"
            # If pointing to any markdown file, try fetching folder via contents API to pull companion scripts
            if path.lower().endswith(".md"):
                try:
                    folder = path.rsplit("/", 1)[0] if "/" in path else ""
                    gh_path = folder if path.lower().endswith("skill.md") else path
                    return self.install_from_github(repo, gh_path, ref=ref)
                except Exception as e:
                    log.debug("install_from_github failed for blob URL, fallback to raw: %s", e)
            url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{ref}/{path}"

        # 3. GitHub raw direct URL: github.com/owner/repo/raw/branch/path
        m_raw_direct = _GH_RAW_DIRECT_RE.match(url)
        if m_raw_direct:
            owner, repo_name, ref, path = m_raw_direct.groups()
            url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{ref}/{path}"

        # 4. GitHub repo root URL: github.com/owner/repo
        m_repo = _GH_REPO_RE.match(url)
        if m_repo and m_repo.group(1).lower() not in _RESERVED_GH_OWNERS:
            owner, repo_name = m_repo.groups()
            repo = f"{owner}/{repo_name.removesuffix('.git')}"
            return self.install_from_github(repo, "")

        # 5. Direct HTTP(S) download
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            data = resp.read(MAX_SKILL_MD + 1)

        stripped = data.lstrip()
        if (
            "text/html" in content_type
            or stripped.startswith((b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML"))
        ):
            raise ValueError(
                "URL returned an HTML webpage instead of raw markdown. "
                "Use a raw file link (e.g. raw.githubusercontent.com) or repository path."
            )

        if len(data) > MAX_SKILL_MD:
            raise ValueError(f"SKILL.md too large (maximum size is {MAX_SKILL_MD // (1024 * 1024)}MB)")

        path_stem = Path(urllib.parse.urlparse(url).path).stem
        return self.install_from_text(
            data.decode("utf-8", errors="replace"),
            fallback_name=path_stem,
        )

    def install_from_github(self, repo: str, path: str = "", ref: str = "") -> dict:
        """Download a skill folder from a GitHub repo (contents API / gh CLI)."""
        repo = repo.strip()
        if repo.startswith("https://github.com/"):
            repo = repo[len("https://github.com/"):]
        elif repo.startswith("http://github.com/"):
            repo = repo[len("http://github.com/"):]
        repo = repo.rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]

        path = (path or "").strip().strip("/")
        target_file = ""
        # If path points directly to a markdown file, split folder and file
        if path.lower().endswith(".md"):
            if "/" in path:
                folder_path, target_file = path.rsplit("/", 1)
            else:
                folder_path, target_file = "", path
            path = folder_path

        listing = self._gh_contents(repo, path, ref=ref)
        if isinstance(listing, dict) and listing.get("type") == "file":
            files = [listing]
        elif isinstance(listing, list):
            files = [f for f in listing if f.get("type") == "file"][:MAX_FILES]
        else:
            raise ValueError("could not list repository contents")

        if target_file:
            md_entry = next((f for f in files if f.get("name", "").lower() == target_file.lower()), None)
            if not md_entry:
                md_entry = _find_gh_skill_entry(files)
        else:
            md_entry = _find_gh_skill_entry(files)

        if not md_entry:
            raise ValueError(
                f"no skill markdown found in {repo}/{path} "
                "(looked for SKILL.md, README.md, *.agent.md, *.prompt.md)"
            )

        md_text = self._download_text(md_entry["download_url"], MAX_SKILL_MD)
        fallback = (
            (target_file.removesuffix(".md") if target_file else "")
            or (path.split("/")[-1] if path else "")
            or repo.split("/")[-1]
        )
        meta = parse_skill_md(md_text, fallback_name=fallback)
        name = _safe_name(meta["name"])
        d = MARKET_DIR / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(md_text, encoding="utf-8")
        for f in files:
            if f.get("download_url") == md_entry.get("download_url"):
                continue
            if int(f.get("size", 0)) > MAX_SCRIPT:
                continue
            # `f["name"]` is the filename from the GitHub API response, and it
            # was used as a path directly. A name containing `../` (a hostile or
            # renamed repo) wrote outside the skill folder — into the Addled
            # tree, or anywhere the process can write. `_safe_name` guarded the
            # skill's own name but never the files inside it.
            raw_name = str(f.get("name") or "")
            safe_file = Path(raw_name).name
            if (not safe_file or safe_file.startswith(".")
                    or "/" in raw_name or "\\" in raw_name
                    or safe_file in (".", "..")):
                log.warning("skipping suspicious skill file name: %r", raw_name)
                continue
            target = (d / safe_file).resolve()
            if d.resolve() not in target.parents:
                log.warning("refusing skill file outside its folder: %r",
                            raw_name)
                continue
            try:
                data = self._download_bytes(f["download_url"], MAX_SCRIPT)
                (d / safe_file).write_bytes(data)
            except Exception as e:
                log.debug("skip %s: %s", f.get("name"), e)
        return self._register_dir(d)

    # ---- deletion ----------------------------------------------------------

    def delete(self, name: str) -> bool:
        d = MARKET_DIR / name
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        skill_registry.unregister(name)
        self._skills.pop(name, None)
        log.info("Deleted market skill: %s", name)
        return True

    # ---- GitHub plumbing ----------------------------------------------------

    @staticmethod
    def _gh_contents(repo: str, path: str = "", ref: str = ""):
        """GitHub contents API via urllib, with gh CLI fallback."""
        clean_path = path.strip("/")
        endpoint = f"repos/{repo}/contents/{clean_path}".rstrip("/")
        query = f"?ref={ref}" if ref else ""
        api = f"https://api.github.com/{endpoint}{query}"
        try:
            req = urllib.request.Request(api, headers={**UA, "Accept":
                "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.debug("REST contents failed (%s) — trying gh CLI", e)
        try:
            gh_cmd = ["gh", "api", endpoint]
            if ref:
                gh_cmd.extend(["-f", f"ref={ref}"])
            r = subprocess.run(
                gh_cmd,
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace")
            if r.returncode == 0:
                return json.loads(r.stdout)
            raise ValueError(r.stderr.strip()[:200] or "gh api failed")
        except Exception as e:
            raise ValueError(f"could not fetch repository contents: {e}")

    @staticmethod
    def _download_text(url: str, limit: int) -> str:
        return SkillMarket._download_bytes(url, limit).decode("utf-8",
                                                              errors="replace")

    @staticmethod
    def _download_bytes(url: str, limit: int) -> bytes:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"file too large (limit {limit // 1024} KB)")
        return data


# Singleton — loads installed market skills on first import
market = SkillMarket()
