"""First-run wizard checks — every step must set something real.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_wizard.py

The wizard asks a first-time user for eight things. A step that collects a value
no code reads is a question asked for nothing, and the user only finds out later
when their choice never took — which is exactly how `character.name` was found
being written while the real key, `agent_name`, sat unset, and how the voice
page came to save `voice`/`enabled`/`rate` that nothing consumes.

So the rule pinned here is a contract, not a shape: every key the wizard writes
in `get_settings()` must be a key that exists in the settings defaults (or is a
top-level scalar the config knows). This file fails by name when they drift.
"""

import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def _flatten(defaults: dict, prefix: str = "") -> set[str]:
    """Every dotted path in the defaults tree, plus top-level scalars."""
    paths: set[str] = set()
    for key, value in (defaults or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            paths.add(path)
            paths |= _flatten(value, path + ".")
        else:
            paths.add(path)
    return paths

def run():
    from backend.config import DEFAULT_SETTINGS

    known = _flatten(DEFAULT_SETTINGS)

    # Read the wizard's own declared settings without building the Qt UI: the
    # method is pure apart from touching widgets, so ask it for the KEYS it
    # would write by parsing the source. Importing PyQt6 would need a display.
    import ast
    from pathlib import Path
    src = Path(ROOT, "backend", "onboarding", "wizard.py").read_text(
        encoding="utf-8", errors="replace")
    tree = ast.parse(src)

    get_settings = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_settings":
            get_settings = node
            break
    check("the wizard has a get_settings()", get_settings is not None,
          "not found")
    if get_settings is None:
        return

    # Walk the returned dict literal to collect section -> keys.
    written: list[tuple[str, str]] = []
    top_level: list[str] = []
    for node in ast.walk(get_settings):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key_node, value_node in zip(node.value.keys, node.value.values):
                if not isinstance(key_node, ast.Constant):
                    continue
                section = str(key_node.value)
                if isinstance(value_node, ast.Dict):
                    for vk in value_node.keys:
                        if isinstance(vk, ast.Constant):
                            written.append((section, str(vk.value)))
                elif isinstance(value_node, (ast.Call, ast.Constant)):
                    top_level.append(section)
            break

    check("get_settings returns a dict literal",
          bool(written) or bool(top_level), "nothing parsed")

    # ---- every section key must exist in the defaults -------------------
    for section, key in written:
        dotted = f"{section}.{key}"
        check(f"'{dotted}' is a real setting",
              dotted in known,
              f"nothing reads {dotted}; known keys near it: "
              + str(sorted(k for k in known if k.startswith(section + "."))[:6]))

    # ---- top-level scalars must exist too --------------------------------
    for name in top_level:
        check(f"'{name}' is a real top-level setting",
              name in known,
              f"nothing reads the top-level key {name}")

    # ---- the specific keys that were wrong before ------------------------
    pairs = {f"{s}.{k}" for s, k in written}
    check("the companion name is written to agent_name",
          "agent_name" in top_level,
          "agent_name missing — the name the user types is discarded")
    check("the name is NOT written to character.name",
          "character.name" not in pairs,
          "character.name is not read by anything")
    check("the voice id is written to voice.tts_voice",
          "voice.tts_voice" in pairs, str(sorted(pairs)))
    check("the voice toggle is written to voice.auto_tts",
          "voice.auto_tts" in pairs, str(sorted(pairs)))
    check("the wake word is written to voice.wake_word",
          "voice.wake_word" in pairs, str(sorted(pairs)))
    check("the mic toggle is written to voice.mic_enabled",
          "voice.mic_enabled" in pairs, str(sorted(pairs)))
    check("the workspace root is written to workspace.root",
          "workspace.root" in pairs, str(sorted(pairs)))
    # Shape and colour are deliberately NOT asked: nothing on screen changes
    # with them, so a step that collected them would be a question with no
    # visible answer. If they come back, this says so rather than silently
    # re-adding a dead step.
    check("the wizard does NOT ask for shape",
          "character.shape" not in pairs,
          "shape is asked again, but nothing on screen changes with it")
    check("the wizard does NOT ask for colour",
          "character.color" not in pairs,
          "colour is asked again, but nothing on screen changes with it")
    check("onboarding completion is recorded",
          "system.onboarded" in pairs and "first_run_complete" in top_level,
          f"pairs={sorted(pairs)} top={top_level}")

    # ---- the pages that must exist ---------------------------------------
    classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for needed in ("WelcomePage", "ProviderPage", "LocalAIPage",
                   "CharacterPage", "WorkspacePage", "VoicePage",
                   "WakePage", "ReadyPage"):
        check(f"the wizard has a {needed}", needed in classes, "missing class")

    # ---- no dead page left behind ----------------------------------------
    check("no page still offers a dead 'speech_rate' setting",
          "speech_rate" not in src,
          "speech_rate returned; nothing consumes a rate string")

    # ---- the workspace page refuses a bad path ---------------------------
    check("the workspace page validates the folder exists",
          "validatePage" in src and "does not exist" in src,
          "a dead workspace path would be saved without complaint")

    # ---- the settings page must agree with the wizard --------------------
    settings_src = Path(ROOT, "dashboard", "src", "app", "settings",
                        "page.tsx").read_text(encoding="utf-8",
                                              errors="replace")
    check("the Settings page does not offer a Shape control",
          "update('character','shape'" not in settings_src,
          "shape is editable in Settings but not in the wizard — the two must "
          "agree about whether it is a user-facing choice")
    check("the Settings page does not offer a Colour control",
          "update('character','color'" not in settings_src,
          "colour is editable in Settings but not in the wizard")

    # ---- the mic check gates the wake toggle ------------------------------
    check("wake listening is gated on a microphone existing",
          "_microphone_available" in src and "mic_ok" in src,
          "listening could be enabled with no microphone")

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: first-run wizard — every step writes a setting that is read")
    return 0

if __name__ == "__main__":
    sys.exit(main())
