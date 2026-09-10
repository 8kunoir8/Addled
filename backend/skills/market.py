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

import json
import logging
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

import yaml

from backend.skills.registry import SkillDefinition, skill_registry

log = logging.getLogger("addled.market")

MARKET_DIR = Path(__file__).parent.parent / "memory" / "market_skills"
MAX_SKILL_MD = 64 * 1024
MAX_SCRIPT = 1024 * 1024
MAX_FILES = 20

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
UA = {"User-Agent": "Addled/1.0 (skill market)"}


def _safe_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip().lower())
    name = name.strip("-")
    if not name:
        raise ValueError("skill name is empty after sanitizing")
    return name[:60]


def parse_skill_md(text: str) -> dict:
    """Parse SKILL.md into a normalized dict."""
    m = _FRONTMATTER_RE.match(text.strip())
    if not m:
        body = text.strip()
        title = re.match(r"\s*#\s+(.+?)\s*\n", body)
        if not title:
            raise ValueError("SKILL.md has no frontmatter and no title")
        return {
            "name": title.group(1).strip(),
            "description": "",
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
    if not str(meta.get("name", "")).strip():
        raise ValueError("frontmatter missing 'name'")
    allowed = meta.get("allowed-tools") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    return {
        "name": str(meta["name"]).strip(),
        "description": str(meta.get("description", "")).strip(),
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
        quoted = " ".join(f'"{str(a)}"' for a in args)
        cmd = f'python "{script_path}" {quoted}'.strip()
        return await TerminalExecutor().execute(cmd, cwd=str(base_dir),
                                                timeout=120)
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
        md = d / "SKILL.md"
        if not md.is_file():
            raise ValueError("no SKILL.md in folder")
        text = md.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > MAX_SKILL_MD:
            raise ValueError("SKILL.md too large")
        meta = parse_skill_md(text)
        name = _safe_name(meta["name"])
        scripts = sorted(p.name for p in d.iterdir()
                         if p.is_file() and p.suffix.lower() == ".py")
        meta["name"] = name
        meta["scripts"] = scripts
        meta["dir"] = d
        if scripts:
            handler = _script_handler(name, scripts[0], d)
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

    def install_from_text(self, text: str) -> dict:
        meta = parse_skill_md(text)
        name = _safe_name(meta["name"])
        d = MARKET_DIR / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        return self._register_dir(d)

    def install_from_url(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_SKILL_MD + 1)
        if len(data) > MAX_SKILL_MD:
            raise ValueError("SKILL.md too large")
        return self.install_from_text(data.decode("utf-8", errors="replace"))

    def install_from_github(self, repo: str, path: str = "") -> dict:
        """Download a skill folder from a GitHub repo (contents API / gh CLI)."""
        listing = self._gh_contents(repo, path)
        if isinstance(listing, dict) and listing.get("type") == "file":
            files = [listing]
        elif isinstance(listing, list):
            files = [f for f in listing if f.get("type") == "file"][:MAX_FILES]
        else:
            raise ValueError("could not list repository contents")
        md_entry = next((f for f in files if f["name"].lower() == "skill.md"),
                        None)
        if not md_entry:
            raise ValueError("no SKILL.md in that folder")
        md_text = self._download_text(md_entry["download_url"], MAX_SKILL_MD)
        meta = parse_skill_md(md_text)
        name = _safe_name(meta["name"])
        d = MARKET_DIR / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(md_text, encoding="utf-8")
        for f in files:
            if f["name"].lower() == "skill.md":
                continue
            if int(f.get("size", 0)) > MAX_SCRIPT:
                continue
            try:
                data = self._download_bytes(f["download_url"], MAX_SCRIPT)
                (d / f["name"]).write_bytes(data)
            except Exception as e:
                log.debug("skip %s: %s", f["name"], e)
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
    def _gh_contents(repo: str, path: str):
        """GitHub contents API via urllib, with gh CLI fallback."""
        api = (f"https://api.github.com/repos/{repo}/contents/{path}"
               .rstrip("/"))
        try:
            req = urllib.request.Request(api, headers={**UA, "Accept":
                "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            log.debug("REST contents failed (%s) — trying gh CLI", e)
        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{repo}/contents/{path}".rstrip("/")],
                capture_output=True, text=True, timeout=60)
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
            raise ValueError("file too large")
        return data


# Singleton — loads installed market skills on first import
market = SkillMarket()
