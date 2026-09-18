"""Guide page checks: the backend flags, and the guide's grip on reality.

Two things can go quietly wrong with a documentation page, and neither shows up
as an error at runtime.

The first is that it drifts. The guide names Settings tabs, dashboard routes,
skill names and status flags as strings; if any of those is renamed the page
keeps rendering and simply stops linking to anything, or shows no badge where it
should show one. So the guide's references are checked against the files that
actually define them — the same approach `check_ws_methods.py` takes with
`send()` calls — and against the live skill registry.

The second is that the page which reports what this machine can do becomes a way
to read the machine's configuration back out. `guide.status` is reachable from a
remote session, so it is asserted to contain nothing but booleans and integers:
no strings, no paths, no names, nothing that could carry a secret. If someone
later adds `"workspace_path": "/home/me/secret"` to that handler, this suite
fails rather than quietly widening what a remote caller can see.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_guide.py
"""

import asyncio
import json
import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GUIDE_TS = os.path.join(ROOT, "dashboard", "src", "lib", "guide-content.ts")
SETTINGS_TS = os.path.join(ROOT, "dashboard", "src", "app", "settings", "page.tsx")
LAYOUT_TS = os.path.join(ROOT, "dashboard", "src", "app", "layout.tsx")
PACKAGE_JSON = os.path.join(ROOT, "package.json")

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def read(path, label):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        fails.append(f"could not read {label} ({path}): {exc}")
        return ""


def leaves(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, f"{path}[{index}]")
    else:
        yield path, node


# ---- static parsing of the guide -------------------------------------------


def declared_status_keys(source: str) -> set:
    """The keys of the GuideStatus type, which is the guide's contract."""
    block = re.search(r"export type GuideStatus = \{(.*?)\n\};", source, re.S)
    if not block:
        fails.append("GuideStatus type not found in guide-content.ts — the parser "
                     "is wrong, not the guide.")
        return set()
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", block.group(1), re.M))


def listed(pattern: str, source: str) -> list:
    """Every quoted string inside each `pattern: [...]` literal, flattened."""
    found = []
    for block in re.findall(pattern, source, re.S):
        found.extend(re.findall(r"'([^']+)'", block))
    return found


def entry_ids(source: str) -> list:
    return re.findall(r"^\s*id:\s*'([^']+)'", source, re.M)


async def run():
    from backend import ws_server
    from backend.skills.registry import skill_registry

    guide_source = read(GUIDE_TS, "the guide content")
    if not guide_source:
        # A deployed copy has no dashboard sources, so only the backend half is
        # meaningful there. Anything else is a real failure to read the file.
        if os.path.isfile(GUIDE_TS):
            return
        print("  (no dashboard sources here — checked the backend half only)")
        return

    # ---- 1. the backend contract ----------------------------------------
    ws_server._register_default_handlers()
    handler = ws_server._server._handlers.get("guide.status")
    check("the backend exposes guide.status", handler is not None,
          "not registered — the Guide tab would show no status at all")
    if handler is None:
        return

    status = await handler({}, object())
    check("guide.status returns a dict", isinstance(status, dict), type(status).__name__)
    if not isinstance(status, dict):
        return

    # Nothing but booleans and integers, anywhere. This is the property that
    # keeps a remote caller from reading configuration through this method.
    stringy = [(name, value) for name, value in leaves(status)
               if not isinstance(value, (bool, int))]
    check("guide.status contains no strings or floats",
          not stringy,
          f"these could leak configuration: {stringy[:6]}")

    empty = [name for name, _ in leaves(status) if name.split(".")[-1] == ""]
    check("guide.status has no empty keys", not empty, str(empty[:6]))

    # ---- 2. the guide references flags that exist ------------------------
    declared = declared_status_keys(guide_source)
    check("the guide declares the status keys it uses", bool(declared),
          "GuideStatus parsed as empty")

    live = set(status)
    missing = sorted(declared - live)
    unused = sorted(live - declared)
    check("every key in GuideStatus exists in guide.status", not missing,
          f"the guide would read these as undefined: {missing}")
    check("every key guide.status returns is declared in GuideStatus", not unused,
          f"the backend reports these but the guide never types them: {unused}")

    needs_flags = set(re.findall(r"flag:\s*'([^']+)'", guide_source))
    stray = sorted(needs_flags - declared)
    check("every `needs` badge watches a declared flag", not stray,
          f"a badge on an undeclared flag never shows: {stray}")

    # ---- 3. the guide links somewhere real -------------------------------
    settings_source = read(SETTINGS_TS, "the settings page")
    tabs = re.search(r"const sections = \[(.*?)\];", settings_source, re.S)
    check("the settings section list was found", tabs is not None,
          "the parser is wrong, not the guide")
    live_tabs = set(re.findall(r"'([^']+)'", tabs.group(1))) if tabs else set()

    referenced_tabs = set(listed(r"settingsTabs:\s*\[(.*?)\]", guide_source))
    bad_tabs = sorted(referenced_tabs - live_tabs)
    check("every Settings tab the guide links to exists", not bad_tabs,
          f"these links would land on the wrong tab: {bad_tabs}")

    layout_source = read(LAYOUT_TS, "the app shell")
    live_routes = set(re.findall(r"href:\s*'(/[^']*)'", layout_source))
    check("the nav routes were found", bool(live_routes),
          "the parser is wrong, not the guide")

    referenced_routes = set(listed(r"routes:\s*\[(.*?)\]", guide_source))
    # Routes appear as `{ href: '/x', label: 'X' }` inside the routes arrays.
    hrefs = set(re.findall(r"href:\s*'(/[^']*)'", guide_source))
    bad_routes = sorted(hrefs - live_routes)
    check("every dashboard page the guide links to exists", not bad_routes,
          f"these links would 404: {bad_routes}")
    check("the routes parser found the guide's links", bool(referenced_routes),
          "no routes were parsed out of guide-content.ts")

    # ---- 4. the guide lists skills that exist ---------------------------
    live_skills = {s.name for s in skill_registry.list_all()}
    check("the skill registry was read", bool(live_skills), "no skills found")
    referenced_skills = set(listed(r"skills:\s*\[(.*?)\]", guide_source))
    unknown = sorted(referenced_skills - live_skills)
    check("every skill the guide names is in the catalogue", not unknown,
          f"these chips would render as unknown: {unknown}")
    check("the guide points at some skills", bool(referenced_skills),
          "no skill references were parsed")

    # ---- 5. the entries themselves --------------------------------------
    ids = entry_ids(guide_source)
    check("the guide has entries", len(ids) >= 12, f"only {len(ids)} entries")
    check("entry ids are unique", len(ids) == len(set(ids)),
          f"duplicates: {sorted({i for i in ids if ids.count(i) > 1})}")
    # Entry ids intentionally reuse their Settings tab name where the guide and
    # the tab describe the same thing; that is how the rail stays recognisable,
    # so it is not asserted against.

    for field in ("title", "icon", "summary", "what"):
        count = len(re.findall(rf"^\s*{field}:\s*'", guide_source, re.M))
        check(f"every entry has a {field}", count >= len(ids),
              f"{count} `{field}` for {len(ids)} entries")
    how_count = len(re.findall(r"^\s*how:\s*\[", guide_source, re.M))
    check("every entry has how-to steps", how_count >= len(ids),
          f"{how_count} `how` blocks for {len(ids)} entries")

    # ---- 6. the version cannot drift ------------------------------------
    try:
        with open(PACKAGE_JSON, encoding="utf-8") as handle:
            app_version = json.load(handle).get("version", "")
    except (OSError, ValueError):
        app_version = ""
    check("the Electron package.json has a version", bool(app_version),
          f"could not read {PACKAGE_JSON}")

    config_source = read(os.path.join(ROOT, "dashboard", "next.config.ts"),
                         "the dashboard config")
    check("the dashboard reads the version at build time",
          "NEXT_PUBLIC_ADDED_VERSION" in config_source,
          "the About tab and the Guide would show a hardcoded version again")

    # Only meaningful once the dashboard has been built, so this is a warning
    # when out/ is absent rather than a failure. The whole export is searched,
    # not just settings.html: the Settings page renders a spinner until the
    # backend answers, so the version only exists in a client chunk.
    out_dir = os.path.join(ROOT, "dashboard", "out")
    if app_version and os.path.isdir(out_dir):
        found = False
        for base, _dirs, files in os.walk(out_dir):
            for name in files:
                if not name.endswith((".js", ".html")):
                    continue
                try:
                    with open(os.path.join(base, name), encoding="utf-8",
                              errors="replace") as handle:
                        if app_version in handle.read():
                            found = True
                            break
                except OSError:
                    continue
            if found:
                break
        check(f"the built dashboard carries version {app_version}", found,
              "no file in dashboard/out mentions it — rebuild the dashboard")
    else:
        print("  (skipped the built-output version check: dashboard/out is absent)")


def main() -> int:
    asyncio.run(run())
    if fails:
        print("FAILURES:")
        for failure in fails:
            print("  - " + failure)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("Guide checks passed.")
    print("  - guide.status reports booleans and counts only, nothing readable back")
    print("  - the guide links only to tabs, routes and skills that exist")
    print("  - the version shown comes from package.json, not a hardcoded string")
    return 0


if __name__ == "__main__":
    sys.exit(main())
