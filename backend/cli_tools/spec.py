"""The manifest shape for a CLI tool, and what makes one valid.

A tool directory holds two things:

    <data dir>/cli_tools/<slug>/tool.py     the program itself
    <data dir>/cli_tools/<slug>/tool.json   this manifest

The manifest is what lets the agent offer the program as a tool. It is kept
small on purpose: everything in it is something the runner or the skill layer
actually reads. A field nobody reads is a field that drifts out of sync with the
behaviour and then lies to whoever reads it next.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, asdict


def _is_boolean(schema) -> bool:
    """Whether a JSON-Schema fragment describes a boolean.

    Tolerates a list-of-types schema (`{"type": ["boolean", "null"]}`), which is
    what a generated manifest produces when the model marks a flag optional.
    """
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if isinstance(kind, list):
        return "boolean" in kind
    return kind == "boolean"

# A slug becomes a directory name AND the `name` of a skill. Restrictive for the
# directory's sake: Windows refuses a directory ending in a space or a dot, and
# a slug with a slash in it would escape the tools directory entirely.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Skill names never contain whitespace — `tool_loop.py` splits a raw call on the
# first space to find the name, and a name with a space in it is unparseable
# there. Underscore only, because the name appears in prose the model writes.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class CliToolSpec:
    """Everything the runtime needs to offer and run one CLI tool."""

    slug: str
    name: str
    description: str

    # Words that make this tool a match for a request, used by
    # `registry.find_match`. Kept as data rather than derived from the
    # description because a model's capability word ("scrape") is often not a
    # word the user typed ("fetch the prices off that page").
    keywords: list[str] = field(default_factory=list)

    # Where the program actually lives, relative to its own directory. A list so
    # a non-Python tool can be added later without changing the schema: the
    # runner passes it to the interpreter, it is not a PATH lookup.
    entry: list[str] = field(default_factory=lambda: ["python", "tool.py"])

    # JSON Schema, exactly the shape `SkillDefinition.parameters` takes, so the
    # adapter is a straight assignment and every provider's tool payload works
    # unchanged.
    params: dict = field(default_factory=dict)

    # How a parameter becomes argv. A list of slots, NOT a command string:
    # `["--url", "{url}", "--json"]`. Substitution happens per slot, so a value
    # containing a space or a shell metacharacter cannot become two arguments or
    # a second command. Nothing here is ever passed through a shell.
    argv_template: list[str] = field(default_factory=list)

    # Extra spellings the model may send for a parameter: `{"url": "link"}`.
    # Mirrors `SkillDefinition.aliases` so the adapter can pass them straight on.
    aliases: dict[str, str] = field(default_factory=dict)

    # A tool that changes files, sends something or deletes must ask first.
    requires_approval: bool = False

    # Args that exercise the tool without side effects, for the build-time smoke
    # test. None means "use --help only", which is always safe.
    dry_run_args: list[str] | None = None

    timeout_s: int = 60

    category: str = "cli"

    # ── validation ──────────────────────────────────────────────────────────

    def validate(self, *, known_skills: set[str] | None = None) -> str:
        """Return "" when usable, else a sentence naming what is wrong.

        Every rule here corresponds to a real failure, not to taste:

        - a bad slug is a directory Windows may refuse to create;
        - a stdlib slug shadows a standard module for every process whose
          `sys.path` includes the tools directory (the `backend/code/` collision
          documented in `backend/main.py` is the precedent);
        - a colliding skill name silently makes the model call the OTHER tool;
        - a placeholder with no matching parameter renders as a literal `{url}`
          in argv, which the program receives as a path named `{url}`;
        - a required parameter absent from the template is dropped on the floor.
        """
        if not _SLUG_RE.match(self.slug or ""):
            return (f"'{self.slug}' is not a usable tool name — use lowercase "
                    "letters, digits, '-' and '_', starting with a letter or "
                    "digit, at most 40 characters")
        if self.slug in sys.stdlib_module_names:
            return (f"'{self.slug}' is the name of a standard Python module, so "
                    "a tool called that would shadow it for every Python "
                    "process that can see the tools folder. Pick another name.")
        if not _NAME_RE.match(self.name or ""):
            return (f"'{self.name}' is not a usable tool name for the model — "
                    "lowercase letters, digits and '_' only")
        if known_skills and self.name in known_skills:
            return (f"a tool called '{self.name}' already exists; the model "
                    "would call that one")
        if not (self.description or "").strip():
            return "the tool needs a description, or the model cannot tell when to use it"
        if not isinstance(self.params, dict):
            return "parameters must be a JSON Schema object"
        props = (self.params or {}).get("properties")
        if props is not None and not isinstance(props, dict):
            return "parameters.properties must be an object"
        props = props or {}
        required = list((self.params or {}).get("required", []) or [])
        if not self.argv_template:
            return "the tool needs an argv template saying how to run it"

        # Two ways a parameter reaches argv: a `{placeholder}` slot, or a bare
        # flag that is present-or-absent.
        #
        # The flag form has to be recognised, not just tolerated. `--json` with
        # no placeholder is the natural way to write a switch, and a rule that
        # demanded `{json}` would reject every boolean a tool ever declares —
        # including the `--json` convention this whole design is built around.
        # A flag is only accepted when it names a declared BOOLEAN, so a typo in
        # a string parameter's flag is still caught.
        used = set()
        template = [str(s) for s in (self.argv_template or [])]

        # A slot is a bare flag only when the NEXT slot is not a placeholder for
        # the same parameter. `["--url", "{url}"]` is a value pair whose first
        # slot looks exactly like a switch; classifying it as one made every
        # ordinary `--name value` argument fail validation.
        flags: set[str] = set()
        for index, slot in enumerate(template):
            if _PLACEHOLDER_RE.search(slot) is not None or not slot.startswith("-"):
                continue
            bare = slot.lstrip("-").replace("-", "_")
            following = template[index + 1] if index + 1 < len(template) else ""
            if f"{{{bare}}}" in following:
                continue
            flags.add(slot)

        for slot in template:
            for placeholder in _PLACEHOLDER_RE.findall(slot):
                used.add(placeholder)
                if placeholder not in props:
                    return (f"the argv template uses {{{placeholder}}} but no "
                            f"parameter of that name is declared")
        for slot in flags:
            bare = slot.lstrip("-").replace("-", "_")
            if bare not in props:
                return (f"the argv template has the flag '{slot}' but no "
                        f"parameter of that name is declared")
            if (props.get(bare) or {}).get("type") != "boolean":
                return (f"'{slot}' is passed as a flag, so '{bare}' must be "
                        "declared as a boolean — a value parameter needs a "
                        "{placeholder} so its value reaches the program")
            used.add(bare)
        for name in props:
            if name not in used:
                return (f"parameter '{name}' is declared but the argv template "
                        "never passes it to the program")
        for name in required:
            if name not in props:
                return f"'{name}' is required but not declared as a parameter"
            if name not in used:
                return (f"'{name}' is required but the argv template does not "
                        "pass it, so the program cannot receive it")
        return ""

    # ── serialisation ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CliToolSpec":
        """Build from a manifest, ignoring keys this version does not know.

        Forward tolerance is deliberate: a manifest written by a newer build
        should still load here without its unknown fields becoming constructor
        errors, so an older Addled can still run the tool.
        """
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in (data or {}).items() if k in fields})

    def argv_for(self, params: dict) -> tuple[list[str], list[str]]:
        """Render this tool's argv from a parameter dict.

        Returns `(argv, warnings)`. Bool parameters become a present/absent
        flag; everything else becomes `--name value` as TWO slots, so a value
        with a space in it stays one argument. A parameter the template does not
        mention is reported rather than appended: the manifest is the contract,
        and guessing a flag name for an undeclared key produces a call the
        program will reject with a confusing argparse error.
        """
        argv: list[str] = []
        warnings: list[str] = []
        by_real = {real: alt for real, alt in (self.aliases or {}).items()}

        # Accept alias spellings the model may have used.
        supplied = dict(params or {})
        for real, alias in by_real.items():
            if real not in supplied and alias in supplied:
                supplied[real] = supplied.pop(alias)

        declared = set((self.params or {}).get("properties", {}) or {})
        for key in supplied:
            if key not in declared and key not in by_real:
                warnings.append(f"ignored parameter '{key}' — not declared")

        template = [str(s) for s in (self.argv_template or [])]
        index = 0
        while index < len(template):
            text = template[index]
            placeholder = _PLACEHOLDER_RE.search(text)

            if placeholder is None and "{" in text:
                # A brace that is not a placeholder — a literal, or a typo the
                # validation should have caught. Pass it through rather than
                # dropping an argument the program may need.
                argv.append(text)
                index += 1
                continue

            if placeholder is None:
                bare_key = ""
                if text.startswith("-"):
                    bare_key = text.lstrip("-").replace("-", "_")
                declared = (self.params or {}).get("properties", {}) or {}
                is_flag_slot = bool(bare_key) and _is_boolean(declared.get(bare_key))
                if is_flag_slot:
                    # Present only when true. Emitting it unconditionally is how
                    # `--json` ended up on every call, including the ones that
                    # asked for prose.
                    if supplied.get(bare_key):
                        argv.append(text)
                        nxt = template[index + 1] if index + 1 < len(template) else ""
                        if f"{{{bare_key}}}" in nxt:
                            index += 1
                else:
                    argv.append(text)
                index += 1
                continue

            key = placeholder.group(1)
            value = supplied.get(key)
            if value is None or value == "":
                # The flag that introduces an absent value goes with it: leaving
                # `--url` in argv with nothing after it makes argparse consume
                # the NEXT argument as the url.
                if (index > 0 and template[index - 1].startswith("-")
                        and f"{{{key}}}" not in template[index - 1]):
                    if argv and argv[-1] == template[index - 1]:
                        argv.pop()
                index += 1
                continue
            if isinstance(value, bool):
                if value:
                    argv.append(text.replace(placeholder.group(0), key))
                index += 1
                continue
            if isinstance(value, list):
                for item in value:
                    argv.append(text.replace(placeholder.group(0), str(item)))
                index += 1
                continue
            argv.append(text.replace(placeholder.group(0), str(value)))
            index += 1
        return argv, warnings
