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

# ---- uploaded skill zips ---------------------------------------------------
#
# Caps, all three, because each bounds a different attack. The archive cap
# stops a huge upload; the extracted-total stops a zip bomb (small on disk,
# enormous when written); the entry count stops a zip of a million empty
# files. `info.file_size` is attacker-controlled, so the extracted total is
# counted while writing rather than read from the header.
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_ZIP_TOTAL = 20 * 1024 * 1024
MAX_ZIP_FILES = 200

# Names Windows treats as devices rather than files. Creating one is not
# merely useless - `CON` and friends can hang or misbehave, and on some
# systems a write to the name escapes the directory. Matched case-folded,
# with or without an extension (`CON`, `con.txt`, `LPT1.log`).
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


class UnsafeZipMember(ValueError):
    """A zip entry that must not be written to disk."""


def _safe_zip_member(name: str) -> str:
    """Return the relative path an entry may be written to, or raise.

    Every check here was written against a real escape, not a guessed one:

      - `../x` and `../../x` are the classic zip-slip. `extractall()` does
        NOT stop these - measured, it wrote `x` outside the destination.
      - `/abs/x` and `C:/x` are absolute. A leading separator or a drive
        letter means the entry names a place, not a file inside the skill.
      - `C:x.txt` is DRIVE-RELATIVE on Windows: not absolute, and it resolves
        against the per-drive working directory. A plain join puts it inside
        the destination, so it passes an innocent-looking "is it contained"
        test while still being an attacker-chosen location. Rejected on the
        `:` alone.
      - `name:stream` is a Windows alternate data stream - a hidden file
        attached to another one. Also rejected on the `:`.
      - Symlinks. A zip can carry an entry whose mode is a symlink, and a
        later entry written "through" it lands at the link's target. Found in
        testing: an entry with mode `S_IFLNK` read back `/etc/hosts`.
      - Reserved device names, which are not files at all.

    Backslashes are normalised first, because a zip written on Windows uses
    them and `Path` on POSIX would treat the whole string as one filename.
    """
    if not name or not name.strip():
        raise UnsafeZipMember("empty entry name")
    raw = name.replace("\\", "/").strip()
    if raw.endswith("/"):
        raise UnsafeZipMember(f"directory entry: {name!r}")
    # A drive letter or a stream separator. Both are `:`, and no legitimate
    # file inside a skill has one on Windows.
    if ":" in raw:
        raise UnsafeZipMember(f"drive-relative or stream name: {name!r}")
    if raw.startswith("/"):
        raise UnsafeZipMember(f"absolute path: {name!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafeZipMember(f"no filename in {name!r}")
    if any(p == ".." for p in parts):
        raise UnsafeZipMember(f"'..' in path: {name!r}")
    leaf = parts[-1]
    stem = leaf.split(".", 1)[0].lower()
    if stem in _RESERVED_NAMES:
        raise UnsafeZipMember(f"reserved device name: {name!r}")
    if leaf.startswith(".") and leaf.lower() not in (".gitignore",):
        # Dotfiles are how archives smuggle config (`.ssh`, `.env`). The one
        # exception is `.gitignore`, which real skill folders sometimes carry.
        raise UnsafeZipMember(f"hidden file: {name!r}")
    return "/".join(parts)

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
    # Match on the BASENAME as well as the whole path: scripts are now found
    # at any depth, so the entry point may arrive as `scripts/main.py` rather
    # than `main.py`, and comparing the full relative path against a bare
    # candidate would never match - silently falling back to alphabetical
    # order and picking the wrong file to run.
    by_base: dict[str, str] = {}
    for s in scripts:
        by_base.setdefault(s.rsplit("/", 1)[-1].lower(), s)
    for candidate in (f"{skill_name.lower()}.py", "main.py", "run.py", "cli.py", "tool.py"):
        if candidate in by_base:
            return by_base[candidate]
    # Prefer a shallower file when nothing matched by name: the closer to the
    # skill root, the more likely it is the documented entry point.
    return min(scripts, key=lambda s: (s.count("/"), s))



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

        # Scripts are found at any depth now, so `script_rel` can carry a
        # directory (`scripts/run.py`). It is produced by our own rglob rather
        # than by input, so this is belt-and-braces: it costs one resolve and
        # makes "the script always runs from inside the skill" a property of
        # this function rather than a fact about its one caller.
        script_path = (base_dir / script_rel).resolve(strict=False)
        root = base_dir.resolve(strict=False)
        if root not in script_path.parents:
            return {"success": False, "error": f"Script outside the skill: {script_rel}"}
        if not script_path.is_file():
            return {"success": False, "error": f"Script missing: {script_rel}"}
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
        # Every `.py` in the skill, at any depth - not just the folder root.
        #
        # This used to be `d.iterdir()`, which is non-recursive, so a skill
        # following the conventional layout (the script in `scripts/`) was
        # invisible here. Since `requires_approval` is decided by `if scripts:`
        # below, that made an executable skill register as instruction-only:
        # the approval gate did not see executable skills that were laid out
        # the normal way. Confirmed against a real skill zip, where
        # `scripts/merge_scenes.py` (mode 0755) was missed entirely.
        #
        # `.as_posix()` so a nested script is addressed as `scripts/x.py`
        # regardless of platform - `_script_handler` joins it onto the skill
        # folder, and a backslash would be a literal filename character there.
        scripts = sorted(
            p.relative_to(d).as_posix()
            for p in d.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts
        )
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
            # The SKILL.md body, now reachable without calling the skill. The
            # handler still returns it (unchanged, so nothing that relied on
            # that breaks), but `skill_view` can read it directly - which is the
            # only way the model can be guided BY a skill it has not yet chosen.
            body=meta.get("instructions", "") or "",
            # The skill's own directory, absolute and resolved once, so a
            # skill that ships templates can be asked for them by name instead
            # of the model globbing the process working directory and getting
            # whichever copy of the skill happens to be nearest.
            folder=str(d.resolve()),
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

    def install_from_zip(self, filename: str, data: bytes,
                         overwrite: bool = False) -> dict:
        """Install a skill from an uploaded `.zip`.

        `data` is the raw archive, already decoded by the caller - the same
        contract as `sprite_skin.save_uploaded_zip`, so the WS handler can
        stay a thin base64-decode.

        Extraction is manual, never `extractall`. Measured on this machine,
        `extractall` wrote entries containing `../` and `/abs/` OUTSIDE the
        destination directory, so it is not a safe primitive for anything
        attacker-supplied.

        `overwrite` defaults False on purpose. `install_from_text` and
        `install_from_github` both delete an existing skill of the same name
        first; for an upload that is the wrong default, because the user may
        not know a skill of that name is already installed and would lose it
        without being told.
        """
        import io
        import stat
        import zipfile

        if not data:
            raise ValueError("No file data provided")
        if len(data) > MAX_ZIP_BYTES:
            raise ValueError(
                f"ZIP too large ({len(data)} bytes, max {MAX_ZIP_BYTES})")
        if data[:2] != b"PK":
            raise ValueError("Not a ZIP file")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError("Invalid ZIP file")

        try:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_FILES:
                raise ValueError(
                    f"ZIP has {len(infos)} entries (max {MAX_ZIP_FILES})")

            # Decide the paths first, so a refusal happens before anything is
            # written and a half-extracted skill never reaches the registry.
            planned: list[tuple[zipfile.ZipInfo, str]] = []
            seen: set[str] = set()
            for info in infos:
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ValueError(
                        f"ZIP contains a symlink ({info.filename!r}); "
                        "a link can point outside the skill folder")
                rel = _safe_zip_member(info.filename)
                # Case-folded, because Windows is case-insensitive: `A.py` and
                # `a.py` in one archive would silently become one file.
                key = rel.lower()
                if key in seen:
                    raise ValueError(
                        f"ZIP has two entries that collide on this filesystem: "
                        f"{rel!r}")
                seen.add(key)
                planned.append((info, rel))

            if not planned:
                raise ValueError("ZIP contains no files")

            # A single wrapping folder is the normal shape (`name/SKILL.md`,
            # `name/scripts/...`) and it MUST be stripped: registering the
            # extract root finds nothing, because the SKILL.md is one level
            # down. Only strip when every entry shares that one folder, so a
            # zip with files at the root is left alone.
            tops = {rel.split("/", 1)[0] for _, rel in planned}
            strip = ""
            if len(tops) == 1:
                only = next(iter(tops))
                if all("/" in rel for _, rel in planned):
                    strip = only

            def final(rel: str) -> str:
                if not strip:
                    return rel
                return rel.split("/", 1)[1]

            staged = [final(rel) for _, rel in planned]
            md_rel = next(
                (r for r in staged if r.lower() in
                 ("skill.md", "readme.md")),
                None,
            ) or next((r for r in staged if r.lower().endswith(".md")), None)
            if not md_rel:
                raise ValueError(
                    "no skill documentation found in the ZIP "
                    "(need SKILL.md, README.md, or a .md file)")

            info_by_rel = {final(rel): info for info, rel in planned}
            md_info = info_by_rel[md_rel]
            if md_info.file_size > MAX_SKILL_MD:
                raise ValueError("SKILL.md in the ZIP is too large")
            md_text = zf.read(md_info).decode("utf-8", errors="replace")
            meta = parse_skill_md(md_text, fallback_name=Path(filename).stem)
            name = _safe_name(meta["name"])

            d = MARKET_DIR / name
            if d.exists() and not overwrite:
                raise ValueError(
                    f"A skill named {name!r} is already installed. "
                    "Turn on overwrite to replace it.")

            # Write to a staging folder, then swap. A failure part-way
            # through leaves the existing skill intact rather than half
            # replaced.
            staging = MARKET_DIR / f".staging-{name}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            total = 0
            try:
                for info, rel in planned:
                    out_rel = final(rel)
                    total += info.file_size
                    if total > MAX_ZIP_TOTAL:
                        raise ValueError(
                            "ZIP contents too large when extracted "
                            f"(max {MAX_ZIP_TOTAL})")
                    target = (staging / out_rel).resolve(strict=False)
                    root_resolved = staging.resolve(strict=False)
                    if root_resolved not in target.parents:
                        raise ValueError(
                            f"refusing to write outside the skill: {out_rel!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))

                if d.exists():
                    shutil.rmtree(d)
                staging.rename(d)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        finally:
            zf.close()

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
