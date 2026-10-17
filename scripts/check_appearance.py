"""Settings -> Appearance must actually change the app.

Reported from the installed app: the Theme and Accent controls in Settings do
nothing. They did nothing because they were not connected to anything - two
inputs with no `value`, no `onChange` and no `update`, rendered from a
component that never received the settings or the save helper. Clicking them
could not have worked, and nothing recorded that they had been chosen.

Wiring them was half the job. The other half is the reason this check exists:
a colour written as a Tailwind arbitrary value - `bg-[#0d1117]` - is fixed at
build time, so no amount of theming reaches it. There were 1,500 of them. The
dashboard now names a token everywhere instead, so the assertions below are
about that property rather than about one control:

  1. every surface colour in the components is a `var(--token)`, not a literal
  2. `:root` defines the whole palette and the light theme overrides it
  3. the appearance module derives the accent shades and persists the choice
  4. the glow exists and is drawn from the accent
  5. the settings section is wired to `update`, so the choice is saved

Deliberately NOT asserted: that the colours look right. That is a judgement,
and a check that encoded one palette would fail the next reasonable one.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_appearance.py
"""

import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def read(*parts):
    # Newlines normalised: the dashboard sources are CRLF on disk, and every
    # pattern below anchors on "\n\}\n". Without this they silently never
    # match - which looks exactly like the code being absent.
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as f:
        return f.read().replace("\r\n", "\n").replace("\r", "\n")


# The colours that describe SURFACES. A status colour (danger/success/warning)
# is included: a red that is chosen for black is unreadable on white, which is
# the same class of bug.
SURFACE_TOKENS = (
    "--bg-primary", "--bg-secondary", "--bg-tertiary", "--bg-hover",
    "--text-primary", "--text-secondary", "--text-muted",
    "--accent", "--accent-hover", "--accent-soft",
    "--danger", "--success", "--warning", "--border",
)

# Literals that must no longer appear as a Tailwind colour value. Kept narrow
# on purpose: an arbitrary value that is a deliberate one-off (a chart series
# colour, say) is not a theming failure, and failing on it would make this
# check something to work around rather than something to trust.
BANNED = {
    "0d1117": "background",
    "161b22": "background",
    "21262d": "background",
    "e8eaed": "text",
    "8b949e": "text",
    "484f58": "text",
    "30363d": "border",
    "3380ff": "accent",
    "58a6ff": "accent",
}

ARBITRARY = re.compile(r"-\[#([0-9a-fA-F]{6})\]")


def main():
    src = os.path.join(ROOT, "dashboard", "src")

    # ---- 1. no hardcoded surface colours left in the components -------------
    offenders = {}
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            if not name.endswith(".tsx"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8", errors="replace").read()
            for hexval in ARBITRARY.findall(text):
                if hexval.lower() in BANNED:
                    rel = os.path.relpath(path, src)
                    offenders[rel] = offenders.get(rel, 0) + 1
    check("surface colours are named by token, not written as literals",
          not offenders,
          f"{sum(offenders.values())} literals in {len(offenders)} file(s): "
          + ", ".join(f"{f}({c})" for f, c in list(offenders.items())[:4]))

    # ---- 2. the palette is defined, and the light theme overrides it --------
    css = read("dashboard", "src", "app", "globals.css")
    root_block = re.search(r":root \{(.*?)\n\}", css, re.S)
    check("globals.css defines a :root palette", root_block is not None, "no :root block")
    light_block = re.search(r"html\.theme-light \{(.*?)\n\}", css, re.S)
    check("a light theme exists to switch to", light_block is not None,
          "the Theme control would have only one real value")

    if root_block:
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", root_block.group(1), re.M))
        missing = [t for t in SURFACE_TOKENS if t not in defined]
        check("every surface token is defined", not missing, ", ".join(missing))
    if root_block and light_block:
        in_root = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", root_block.group(1), re.M))
        in_light = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", light_block.group(1), re.M))
        # --accent and --accent-rgb are written by the appearance script from the
        # user's choice, so the light block is not required to restate them.
        derived = {"--accent", "--accent-hover", "--accent-rgb"}
        missing = [t for t in SURFACE_TOKENS
                   if t in in_root and t not in in_light and t not in derived]
        check("the light theme overrides every surface token",
              not missing,
              f"would stay dark-on-white: {', '.join(missing)}")

    # ---- 3. the glow is the accent, drawn from it --------------------------
    #
    # Asserted against the RULE, not against the file. An earlier version only
    # looked for the token names anywhere in globals.css, so deleting the whole
    # glow block still passed - the tokens are still declared in `:root`, and a
    # declaration nobody consumes looks identical to a used one.
    glow = re.search(r"\.bubble-shell::after \{(.*?)\n\}", css, re.S)
    check("a glow overlay is defined on the window shell",
          glow is not None,
          "the accent would colour the buttons but not the frame")
    if glow:
        body = glow.group(1)
        check("the glow is drawn from the accent",
              "--accent-rgb" in body,
              "the frame would not follow the chosen colour")
        check("the glow fades inward rather than being a flat band",
              body.count("inset") >= 2 and "var(--glow-width)" in body,
              "one inset shadow ends in a hard edge, not a falloff")
        check("the glow does not swallow clicks",
              re.search(r"pointer-events:\s*none", body) is not None,
              "an overlay covering the whole app without pointer-events:none blocks it")
    check(".bubble-shell is positioned for the glow overlay",
          re.search(r"\.bubble-shell \{[^}]*position:\s*relative", css, re.S) is not None,
          "the overlay would position against the viewport instead")

    # ---- 4. the appearance module does the derived work ---------------------
    mod = read("dashboard", "src", "lib", "appearance.ts")
    check("the accent is split into components for the glow",
          "accent-rgb" in mod, "rgba() cannot be built from a hex token")
    check("the choice is remembered across reloads",
          "localStorage" in mod, "Theme and Accent would reset on every reload")
    check("the light/dark switch is a class on the document",
          "classList.toggle" in mod and "theme-light" in mod,
          "the CSS would never see the choice")
    check("a hover shade is derived rather than asked for",
          "lighten(" in mod and "darken(" in mod,
          "the user would have to pick three colours to get one")
    check("System follows the OS setting",
          "prefers-color-scheme" in mod,
          "choosing System would freeze at whatever was current when chosen")

    # ---- 5. the controls are wired to the save path ------------------------
    settings = read("dashboard", "src", "app", "settings", "page.tsx")
    # From the declaration to the first line that is exactly "}" - the helpers
    # inside are indented, so a column-0 brace can only be the function's own.
    # The props are a destructure that ends in a type annotation (`}: any)`),
    # not a bare `}`, so the parameter list is matched loosely and the closing
    # brace of the body is anchored to column 0 - the helpers inside are
    # indented, so a flush brace can only be this function's.
    section = re.search(
        r"function AppearanceSection\(.*?\) \{\n(.*?)\n\}\n", settings, re.S)
    check("AppearanceSection is a component with the save helper",
          section is not None, "not found - has it been renamed?")
    if section:
        body = section.group(1)
        check("it receives and calls `update`",
              "update(" in body,
              "a choice would apply to this view and be forgotten on reload")
        check("the theme control is controlled",
              re.search(r"<select[^>]*value=\{theme\}", body, re.S) is not None,
              "an uncontrolled select forgets what the user picked")
        check("the accent control is controlled",
              re.search(r'type="color"[^>]*value=\{accent\}', body, re.S) is not None,
              "an uncontrolled colour input forgets what the user picked")
        check("both controls have handlers",
              body.count("onChange") >= 2,
              "a control with no onChange cannot do anything")

    check("the call site passes the settings into the section",
          re.search(r"AppearanceSection settings=\{settings\}", settings) is not None,
          "the section cannot show what is actually saved")

    # ---- 6. the stored choice is applied before the settings arrive -------
    layout = read("dashboard", "src", "app", "layout.tsx")
    check("the layout applies the stored appearance on mount",
          "applyStoredAppearance" in layout,
          "every reload would flash the default palette")

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: appearance is wired, themed, and saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
