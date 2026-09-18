"""
Declarative registry of the guideline packs Addled knows about.

Pure data — no I/O, no imports, so it is safe to import from anywhere. Each pack
lists its upstream documents in order of preference: the first that responds is
the one cached, and the URL that answered is recorded so the Settings panel can
show exactly where the text came from.
"""

from __future__ import annotations

PACKS: dict[str, dict] = {
    "ponytail": {
        "title": "Ponytail",
        "subtitle": "lazy senior dev",
        "summary": ("A 7-rung ladder that stops you writing code that did not "
                    "need to exist, plus the things it is never lazy about."),
        "home": "https://github.com/DietrichGebert/ponytail",
        "license": "MIT",
        "levels": ("lite", "full", "ultra"),
        "default_level": "full",
        # Upstream ships the same ruleset for many hosts; AGENTS.md is the
        # host-agnostic one, so prefer it.
        "sources": (
            "https://raw.githubusercontent.com/DietrichGebert/ponytail/HEAD/AGENTS.md",
            "https://raw.githubusercontent.com/DietrichGebert/ponytail/HEAD/.github/copilot-instructions.md",
            "https://raw.githubusercontent.com/DietrichGebert/ponytail/HEAD/.cursor/rules/ponytail.mdc",
        ),
    },
    "karpathy": {
        "title": "Karpathy guidelines",
        "subtitle": "four principles",
        "summary": ("Think before coding, simplicity first, surgical changes, "
                    "goal-driven execution."),
        "home": "https://github.com/multica-ai/andrej-karpathy-skills",
        # Upstream has no LICENSE file; the README states MIT. Noted here rather
        # than assumed, and the reason this text is fetched rather than vendored.
        "license": "MIT per the README (no LICENSE file upstream)",
        "levels": ("lite", "full", "ultra"),
        "default_level": "full",
        "sources": (
            "https://raw.githubusercontent.com/multica-ai/andrej-karpathy-skills/HEAD/CLAUDE.md",
            "https://raw.githubusercontent.com/forrestchang/andrej-karpathy-skills/HEAD/CLAUDE.md",
        ),
    },
}

# Extra text appended when a pack is set to the "ultra" level. Upstream drives
# its levels through per-host hooks that Addled cannot run, so the levels here
# are an honest local approximation: they control how much text is sent and how
# firmly it is framed. The ruleset itself is never rewritten.
LEVEL_FRAMING: dict[str, str] = {
    "lite": "Core rule only — apply this before writing any code.",
    "full": "",
    "ultra": ("Apply these rules aggressively for this task: climb every rung "
              "before writing code, and state which rung you stopped at."),
}
