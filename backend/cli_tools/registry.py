"""The set of CLI tools on this machine, and which one answers a request.

Tools live as directories under `app_paths.CLI_TOOLS_DIR`:

    <data dir>/cli_tools/<slug>/tool.py
    <data dir>/cli_tools/<slug>/tool.json

Nothing about them is compiled into the app, so a user's tools survive an
upgrade and an upgrade cannot bring any with it. `load()` scans that directory
at boot and registers each tool as a skill.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.cli_tools.adapter import as_skill
from backend.cli_tools.spec import CliToolSpec

log = logging.getLogger("addled.cli_tools")

MANIFEST_NAME = "tool.json"

# How much of a match is enough. Matches the market search's default for the
# same reason: the two are consulted in sequence for the same question, and a
# threshold that disagreed would make one of them answer where the other did not.
DEFAULT_MATCH_THRESHOLD = 0.5

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenise(text: str) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


def _stem(word: str) -> str:
    """A crude stem, so inflected forms of one word compare equal.

    `greeting`, `greets` and `greet` are one word to anyone reading a tool name,
    and matching them exactly meant `do_greeting` found nothing while a tool
    whose keyword is "greet" sat installed and unused.

    Four characters, not a real stemmer: the vocabulary here is keyword lists
    and tool names, and a porter-stemmer would be a dependency and a surprise.
    Truncating is predictable, and its failure mode — two different words
    sharing four letters — is bounded by the corroboration rule in
    `find_match`, where a request word only counts once a call word agrees.
    """
    word = str(word or "").lower()
    return word[:4] if len(word) > 4 else word


@dataclass
class CliTool:
    """One installed tool: its manifest, where it lives, and how it has gone."""

    spec: CliToolSpec
    source_dir: Path
    created_at: float = 0.0
    built_by: str = "user"          # "user" | "agent"
    run_count: int = 0
    last_run: float = 0.0
    last_error: str | None = None

    @property
    def slug(self) -> str:
        return self.spec.slug

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def bin_path(self) -> Path:
        entry = self.spec.entry or ["python", "tool.py"]
        target = entry[-1] if len(entry) > 1 else "tool.py"
        path = Path(target)
        return path if path.is_absolute() else self.source_dir / path

    def stats_dict(self) -> dict:
        return {
            "run_count": self.run_count,
            "last_run": self.last_run,
            "last_error": self.last_error,
        }

    def to_dict(self) -> dict:
        """The manifest as it is written to disk, stats included.

        Stats live in the manifest rather than a side file because a tool
        directory should be movable: copying `<slug>/` to another machine should
        bring what the tool is, and the counts are small enough that a second
        file would be more machinery than the data deserves.
        """
        data = self.spec.to_dict()
        data["created_at"] = self.created_at
        data["built_by"] = self.built_by
        data.update(self.stats_dict())
        return data


class CliToolRegistry:
    """Every CLI tool this machine has, in memory, backed by the data dir."""

    def __init__(self) -> None:
        self._tools: dict[str, CliTool] = {}

    # ── discovery ───────────────────────────────────────────────────────────

    def tools_dir(self) -> Path:
        from backend import app_paths
        return app_paths.CLI_TOOLS_DIR

    def load(self) -> int:
        """Read every tool directory. Returns how many registered.

        Soft-fails per tool, always: one malformed manifest must not stop Addled
        booting, and it must not stop the OTHER tools loading. A tool that will
        not load is logged with its reason so the person who built it can see
        what is wrong with it.
        """
        root = self.tools_dir()
        if not root.exists():
            return 0
        registered = 0
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / MANIFEST_NAME
            if not manifest.exists():
                log.debug("cli_tools: %s has no %s; skipping", entry.name,
                          MANIFEST_NAME)
                continue
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.warning("cli_tools: could not read %s: %s", manifest, exc)
                continue
            try:
                spec = CliToolSpec.from_dict(raw)
            except TypeError as exc:
                log.warning("cli_tools: %s has an unusable manifest: %s",
                            entry.name, exc)
                continue
            if spec.slug != entry.name:
                # The directory IS the identity; a manifest that disagrees would
                # let two directories claim one skill name.
                log.warning("cli_tools: %s declares slug '%s'; using the "
                            "directory name", entry.name, spec.slug)
                spec.slug = entry.name
            known = {t.name for t in self._tools.values()}
            trouble = spec.validate(known_skills=known)
            if trouble:
                log.warning("cli_tools: not loading '%s': %s", spec.slug, trouble)
                continue
            tool = CliTool(
                spec=spec,
                source_dir=entry,
                created_at=float(raw.get("created_at") or 0.0),
                built_by=str(raw.get("built_by") or "user"),
                run_count=int(raw.get("run_count") or 0),
                last_run=float(raw.get("last_run") or 0.0),
                last_error=raw.get("last_error"),
            )
            self._register(tool)
            registered += 1
        if registered:
            log.info("loaded %d CLI tool(s)", registered)
        return registered

    # ── membership ──────────────────────────────────────────────────────────

    def _register(self, tool: CliTool) -> None:
        from backend.skills.registry import skill_registry
        skill_registry.register(as_skill(tool.spec, tool.source_dir))
        self._tools[tool.slug] = tool

    def add(self, spec: CliToolSpec, source_dir: Path, *, built_by: str = "user",
            created_at: float | None = None) -> dict:
        """Register a tool that is already on disk, and return its record."""
        known = {t.name for t in self._tools.values()}
        trouble = spec.validate(known_skills=known)
        if trouble:
            return {"success": False, "error": trouble}
        tool = CliTool(spec=spec, source_dir=Path(source_dir),
                       created_at=created_at or time.time(), built_by=built_by)
        self._register(tool)
        self._save(tool)
        log.info("registered CLI tool '%s' (%s)", spec.slug, built_by)
        return {"success": True, "tool": tool.to_dict()}

    def remove(self, slug: str, *, delete_files: bool = True) -> dict:
        """Unregister a tool, then delete it.

        That order is not cosmetic. Deleting first leaves a window in which the
        skill is still in the registry but its program is gone, so a call that
        lands in that window fails with "file not found" from inside a
        subprocess — a fault that reads as a broken tool rather than a deletion.
        """
        tool = self._tools.get(slug)
        if tool is None:
            return {"success": False, "error": f"no CLI tool called '{slug}'"}
        from backend.skills.registry import skill_registry
        skill_registry.unregister(tool.name)
        self._tools.pop(slug, None)
        if delete_files:
            import shutil
            try:
                shutil.rmtree(tool.source_dir)
            except OSError as exc:
                # The skill is already gone, which is the part that matters for
                # correctness; say so rather than reporting the whole removal as
                # failed and leaving the caller to guess.
                log.warning("cli_tools: unregistered '%s' but could not delete "
                            "%s: %s", slug, tool.source_dir, exc)
                return {"success": True, "removed": slug,
                        "warning": f"files left in place: {exc}"}
        log.info("removed CLI tool '%s'", slug)
        return {"success": True, "removed": slug}

    def get(self, slug: str) -> CliTool | None:
        return self._tools.get(str(slug or ""))

    def list_all(self) -> list[CliTool]:
        return sorted(self._tools.values(), key=lambda t: t.slug)

    # ── matching ────────────────────────────────────────────────────────────

    def find_match(self, name: str, request_text: str = "",
                   threshold: float | None = None) -> CliTool | None:
        """The tool that answers a call for `name`, if one does.

        Scoring is deliberately simple and deterministic so it can be asserted
        without a model in the loop. `request_text` is the user's own words for
        this turn: a tool-call name is often generic (`fetch_page`) while the
        request carries the intent (`get the prices off this page`), and
        matching on the name alone misses that.
        """
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None

        if threshold is None:
            try:
                from backend.config import config
                threshold = float(config.get("cli_tools", "match_threshold",
                                             default=DEFAULT_MATCH_THRESHOLD))
            except Exception:  # noqa: BLE001
                threshold = DEFAULT_MATCH_THRESHOLD

        request_tokens = _tokenise(request_text)
        best: tuple[float, CliTool] | None = None

        # Scoring, in one place: the per-tool block below carries the reasoning.
        #
        #   exact slug        -> identity, returned immediately
        #   exact name        -> 0.95
        #   a declared alias  -> 0.85
        #   keyword in the call name        -> 0.60 upward
        #   keyword in the user's own words -> only alongside a call-name hit
        #
        # The shape of that table is the design: a call the model named well is
        # enough on its own, and the user's prose can only corroborate it.
        name_tokens = _tokenise(wanted)
        for tool in self._tools.values():
            spec = tool.spec
            keys = {spec.slug.lower(), spec.name.lower()}
            if wanted == spec.slug.lower():
                return tool                      # exact slug is identity
            if wanted == spec.name.lower():
                score = 0.95
            elif wanted in keys:
                score = 0.9
            else:
                aliases = {str(a).lower() for a in (spec.aliases or {}).values()}
                if wanted in aliases:
                    score = 0.85
                else:
                    words = {w.lower() for w in (spec.keywords or [])}
                    words |= _tokenise(spec.name)
                    words |= _tokenise(spec.slug)

                    # Compare on stems, not raw tokens. `do_greeting` and a
                    # keyword "greet" are one word to a reader and differ only by
                    # an ending; requiring an exact string match made every
                    # inflected call fall through to the market.
                    stems = {_stem(w) for w in words}
                    from_name = {t for t in name_tokens if _stem(t) in stems}
                    from_request = {t for t in (request_tokens - name_tokens)
                                    if _stem(t) in stems}
                    score = 0.0
                    if from_name:
                        # A keyword in the CALL NAME is the strongest evidence
                        # short of a name match: the model chose that word to
                        # name the capability. One hit therefore clears the
                        # threshold on its own — at 0.45 it did not, so
                        # `greet_someone` fell through to the market while a
                        # tool whose keywords include "greet" sat unused.
                        score = min(0.6 + 0.15 * (len(from_name) - 1), 0.85)
                    if from_request:
                        # The user's own words carry the intent when the call
                        # name only gestures at it — "fetch me the prices off a
                        # page" names the capability that `do_the_thing` does
                        # not.
                        #
                        # Corroboration, not a free pass, and the gate is the
                        # whole design. Prose is long and its words collide by
                        # accident: `send_an_email_to_bob` alongside "greet
                        # everyone in the company" shares "greet" with this tool
                        # and has nothing else to do with it. Letting a single
                        # prose word clear the bar matched exactly that, and
                        # would have run the wrong tool on a call the model had
                        # already named correctly.
                        #
                        # So prose corroborates when EITHER
                        #   - a word of the call name also matches (the two
                        #     signals agree, neither decides alone), or
                        #   - at least two of the request's words match, which
                        #     is no longer an accident: one shared word is
                        #     common English, two is a description of this tool.
                        if from_name or len(from_request) >= 2:
                            score += min(0.55 * len(from_request), 0.7)
                        else:
                            score = max(score, 0.2)
            if score and (best is None or score > best[0]):
                best = (score, tool)
        if best and best[0] >= threshold:
            return best[1]
        return None

    # ── stats ───────────────────────────────────────────────────────────────

    def bump_run(self, slug: str, ok: bool, error: str | None = None) -> None:
        """Record that a tool ran, without letting a write failure matter.

        Stats are diagnostics. A full disk or a read-only data directory must
        not turn a working tool call into a failure, so this swallows its own
        errors and logs them at debug.
        """
        tool = self._tools.get(slug)
        if tool is None:
            return
        tool.run_count += 1
        tool.last_run = time.time()
        tool.last_error = None if ok else (error or "failed")
        try:
            self._save(tool)
        except OSError as exc:
            log.debug("cli_tools: could not save stats for '%s': %s", slug, exc)

    def _save(self, tool: CliTool) -> None:
        """Write a manifest atomically, the way `config.save` does.

        A plain write truncates first, so a crash mid-write leaves a tool whose
        manifest cannot be parsed — and `load()` then skips it, which looks like
        the tool vanished.
        """
        target = tool.source_dir / MANIFEST_NAME
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tool.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, target)


cli_tools = CliToolRegistry()
