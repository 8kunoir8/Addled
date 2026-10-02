"""Local vision must be installable AND actually usable, not just "pip said ok".

Two defects shipped together and each one hid the other:

1. `install_hf_deps()` (backend/local_llm/manager.py) ran
   `sys.executable -s -m pip install torch transformers accelerate` with no
   `--target`. pip's default destination is the *user* site-packages,
   `%APPDATA%\\Python\\Python3xx\\site-packages`, and the app runs every
   interpreter with `-s`, which EXCLUDES the user site. So pip reported
   "Successfully installed" and the app kept reporting "missing" forever.
   A user pressing Install could repeat it indefinitely with no effect.

2. Nothing installed or even checked `einops`/`timm`. Florence-2's remote code
   imports both at call time, so the machine passed a torch+transformers check
   and then failed on the first image with "requires einops, timm".
   `deps_available()` only ever looked at torch/transformers, which is why the
   Settings panel said "ready" while every photo failed.

The user-visible symptom was: send a picture, get
"Could not analyse attached image — vision provider and local Florence-2 both
failed". These checks pin the fixes and, importantly, pin that the destination
is a directory the `-s` interpreter can actually import from.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_vision_deps.py
"""

import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

MANAGER = os.path.join(ROOT, "backend", "local_llm", "manager.py")
HF_PROVIDER = os.path.join(ROOT, "backend", "providers", "huggingface_local_provider.py")
VISION_REQ = os.path.join(ROOT, "requirements-vision.txt")
BUILDER = os.path.join(ROOT, "electron-builder.yml")

fails: list[str] = []

def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)

def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def main() -> int:
    for path in (MANAGER, HF_PROVIDER):
        if not os.path.isfile(path):
            print(f"missing file: {path}")
            return 2

    mgr = read(MANAGER)
    prov = read(HF_PROVIDER)

    # -- 1. the install must target a dir the -s interpreter can import -------
    check("the app's site-packages is resolved from the interpreter",
          "_app_site_packages" in mgr,
          "hard-coding a path breaks when the bundle moves")
    # Strip comments first: the bug is CALLING getusersitepackages, and the
    # docstring explains why we deliberately do not. Matching the bare word
    # would fail on the explanation of the fix.
    code = "\n".join(
        line for line in mgr.splitlines()
        if not line.lstrip().startswith("#")
    )
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    check("the code does not call getusersitepackages()",
          "getusersitepackages" not in code,
          "installing there is invisible to a -s interpreter — the original bug")
    check("install_hf_deps passes --target",
          re.search(r'"--target"\s*,\s*target', mgr) is not None,
          "without --target, pip writes to the user site and the app never sees it")
    check("the target is resolved, not hard-coded",
          "_app_site_packages()" in mgr)

    # The resolution itself must agree with the running interpreter: whatever it
    # returns has to be a directory this process can import from.
    sys.path.insert(0, ROOT)
    try:
        from backend.local_llm.manager import _app_site_packages
        site = _app_site_packages()
        check("_app_site_packages() resolves to an existing dir",
              site is None or os.path.isdir(site),
              f"returned {site!r}")
        if site:
            import importlib.util
            check("the returned dir is one this interpreter searches",
                  any(os.path.normcase(p) == os.path.normcase(site)
                      for p in sys.path if p),
                  f"{site!r} is not on sys.path — packages would still be invisible")
    except Exception as exc:  # noqa: BLE001
        check("manager imports cleanly", False, str(exc))

    # -- 2. einops and timm must be installed and checked --------------------
    check("install_hf_deps includes einops",
          re.search(r'packages\s*=\s*\[[^\]]*"einops"', mgr) is not None
          or "_vision_requirements" in mgr,
          "Florence-2 cannot load without it")
    check("install_hf_deps includes timm",
          re.search(r'packages\s*=\s*\[[^\]]*"timm"', mgr) is not None
          or "_vision_requirements" in mgr,
          "Florence-2 cannot load without it")
    check("the vision dependency set names einops and timm",
          '"einops"' in prov and '"timm"' in prov,
          "the check and the install must agree on the same set")

    # -- 2b. the install list is declared, not hardcoded ---------------------
    # A literal list drifts: it is what let einops/timm be missing in the first
    # place. The button should read the same file the repository maintains.
    vision_req = os.path.join(ROOT, "requirements-vision.txt")
    check("requirements-vision.txt exists",
          os.path.isfile(vision_req),
          "the install button reads this file")
    if os.path.isfile(vision_req):
        req_text = read(vision_req)
        for pkg in ("torch", "transformers", "einops", "timm"):
            check(f"requirements-vision.txt lists {pkg}",
                  re.search(rf"^\s*{pkg}\b", req_text, re.M) is not None,
                  "an unlisted package is an uninstalled package")
    check("manager can locate the requirements file",
          "_vision_requirements" in mgr)
    check("install_hf_deps prefers the declared file",
          re.search(r'install_args\s*=\s*\["-r"', mgr) is not None,
          "otherwise the file is decoration")

    # The resolver must find the file in THIS tree, whichever layout that is.
    try:
        from backend.local_llm.manager import _vision_requirements
        found = _vision_requirements()
        check("_vision_requirements() finds the file in this tree",
              found is not None and os.path.isfile(str(found)),
              f"returned {found!r}")
    except Exception as exc:  # noqa: BLE001
        check("_vision_requirements importable", False, str(exc))

    # -- 2c. the installer must actually carry the file ----------------------
    builder = os.path.join(ROOT, "electron-builder.yml")
    if os.path.isfile(builder):
        yml = read(builder)
        check("requirements-vision.txt is an extraResources source",
              re.search(r"-\s*from:\s*requirements-vision\.txt", yml) is not None,
              "an installed app without the file falls back to the old literal list")

    try:
        from backend.providers import huggingface_local_provider as hf
        check("vision check is separate from the base check",
              hf.vision_deps_available is not hf.deps_available)
        base_pkgs = set(hf.BASE_DEPS)
        vision_pkgs = set(hf.VISION_DEPS)
        check("vision deps are a superset of the base deps",
              base_pkgs <= vision_pkgs,
              f"base={sorted(base_pkgs)} vision={sorted(vision_pkgs)}")
        check("einops/timm are in the vision set",
              {"einops", "timm"} <= vision_pkgs)
        check("einops/timm are NOT required by the base check",
              "einops" not in base_pkgs and "timm" not in base_pkgs,
              "the Local AI provider works without them; requiring them would "
              "report a false failure")

        # missing_deps must actually detect a module that is not there.
        check("missing_deps detects a definitely-absent module",
              "addled_no_such_module_xyz" in hf.missing_deps("addled_no_such_module_xyz"))
        check("missing_deps reports nothing for stdlib",
              hf.missing_deps("json", "os") == [])
    except Exception as exc:  # noqa: BLE001
        check("hf provider imports cleanly", False, str(exc))

    # -- 3. the status the UI reads must expose vision readiness --------------
    check("hf_status reports vision readiness",
          '"vision_ready"' in mgr,
          "the panel must be able to say 'chat ok, images not'")
    check("hf_status reports WHY vision is not ready",
          '"vision_error"' in mgr)

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: local vision installs into a directory the app can import from, "
          "and the check matches what Florence-2 needs")
    return 0

if __name__ == "__main__":
    sys.exit(main())
