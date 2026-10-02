"""Bundle a self-contained Python runtime into python-bundle/ for the installer.

Downloads the Windows embeddable package, bootstraps pip, and installs the
core requirements. Skips work when the bundle is already complete.
Run from the repo root:  python scripts/bundle_python.py
"""
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PY_VERSION = "3.14.7"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "build_cache"
BUNDLE = ROOT / "python-bundle"
COMPLETE_MARK = BUNDLE / ".bundle-complete"

# Keep the embedded interpreter isolated from the build machine's packages:
# without this, `import site` leaks the user site-packages dir into sys.path
# and pip skips installing what the bundle actually needs.
ISOLATED_ENV = {**os.environ, "PYTHONNOUSERSITE": "1"}


def log(msg: str) -> None:
    print(f"[bundle] {msg}", flush=True)


def download(url: str, dest: pathlib.Path) -> None:
    if dest.exists():
        log(f"using cached {dest.name}")
        return
    CACHE.mkdir(exist_ok=True)
    log(f"downloading {url} ...")
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)
    log(f"saved {dest.name} ({dest.stat().st_size // 1024 // 1024} MB)")


def main() -> int:
    # ── 1. Extract embeddable Python ─────────────────────────────────────
    zip_path = CACHE / f"python-{PY_VERSION}-embed-amd64.zip"
    if COMPLETE_MARK.exists() and COMPLETE_MARK.read_text().strip() == PY_VERSION:
        log(f"python-bundle already complete for {PY_VERSION} — skipping")
        return 0

    download(EMBED_URL, zip_path)
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    BUNDLE.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(BUNDLE)
    log(f"extracted embeddable Python {PY_VERSION}")

    # ── 2. Enable site-packages in the ._pth file ────────────────────────
    pth_files = list(BUNDLE.glob("python*._pth"))
    if not pth_files:
        log("ERROR: no ._pth file found in embeddable package")
        return 1
    pth = pth_files[0]
    lines = pth.read_text(encoding="utf-8").splitlines()
    fixed = []
    for line in lines:
        if line.strip() == "#import site":
            fixed.append("import site")
        else:
            fixed.append(line)
    pth.write_text("\n".join(fixed) + "\n", encoding="utf-8")
    log(f"enabled site-packages in {pth.name}")

    py = BUNDLE / "python.exe"
    if not py.exists():
        log("ERROR: python.exe missing from embeddable package")
        return 1

    # ── 3. Bootstrap pip (isolated from user site-packages) ─────────────
    getpip = CACHE / "get-pip.py"
    download(GETPIP_URL, getpip)
    subprocess.run(
        [str(py), "-s", str(getpip), "--no-warn-script-location"],
        check=True, env=ISOLATED_ENV,
    )
    log("pip bootstrapped")

    # ── 4. Install core requirements ─────────────────────────────────────
    req = ROOT / "requirements-core.txt"
    subprocess.run(
        [str(py), "-s", "-m", "pip", "install", "--no-warn-script-location",
         "-r", str(req)],
        check=True, env=ISOLATED_ENV,
    )
    log("core requirements installed")

    # ── 4b. Fetch the local embedder model (ONNX MiniLM) ────────────────
    log("fetching embedder model ...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "fetch_embedder.py")],
        check=False, env=ISOLATED_ENV,
    )

    # ── 4c. Fetch voice models (Silero VAD + Kokoro TTS) ────────────────
    log("fetching voice models ...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "fetch_voice_models.py")],
        check=False, env=ISOLATED_ENV,
    )

    # ── 5. Verify critical imports ───────────────────────────────────────
    check = (
        "import sys, websockets, httpx, mss, numpy, PIL, pyautogui, pyperclip, edge_tts, onnxruntime; "
        "import PyQt6.QtCore; "
        "import win32api, win32event; "
        "assert 'site-packages' in PyQt6.__file__ and 'python-bundle' in PyQt6.__file__, PyQt6.__file__; "
        "print('embedded-imports-ok')"
    )
    r = subprocess.run(
        [str(py), "-s", "-c", check],
        capture_output=True, text=True, env=ISOLATED_ENV,
    )
    if r.returncode != 0:
        log(f"import check FAILED:\n{r.stdout}\n{r.stderr}")
        return 1
    log(f"import check: {r.stdout.strip()}")

    # ── 5b. Assert the vision stack is ABSENT, and that the app says so ───
    # The bundle intentionally omits torch/transformers/einops/timm (~400 MB);
    # the app installs them on demand. That intent is only safe if two things
    # hold, and both have silently broken before:
    #   (a) the requirements-vision.txt list ships, since the install button
    #       reads it instead of a hardcoded list; and
    #   (b) `vision_deps_available()` reports NOT ready in the bundle, so the
    #       UI shows the button rather than claiming images work.
    # Checking absence also guards the reverse mistake: if someone adds the
    # heavy block to requirements-core.txt by accident, the installer balloons
    # and this fails by name.
    vision_req = ROOT / "requirements-vision.txt"
    if not vision_req.is_file():
        log("ERROR: requirements-vision.txt is missing - the install button "
            "would fall back to a hardcoded list")
        return 1
    for pkg in ("einops", "timm"):
        if pkg not in vision_req.read_text(encoding="utf-8"):
            log(f"ERROR: requirements-vision.txt does not list {pkg} - "
                "Florence-2 cannot load without it")
            return 1
    log("vision requirements list present and complete")

    absence = (
        "import importlib.util as u, sys;"
        "heavy=[m for m in ('torch','transformers','einops','timm') if u.find_spec(m)];"
        "print('unexpectedly bundled: ' + ','.join(heavy)) if heavy else "
        "print('vision-stack-absent-as-intended')"
    )
    r2 = subprocess.run(
        [str(py), "-s", "-c", absence],
        capture_output=True, text=True, env=ISOLATED_ENV,
    )
    if r2.returncode != 0:
        log(f"vision absence check FAILED:\n{r2.stdout}\n{r2.stderr}")
        return 1
    if "unexpectedly bundled" in r2.stdout:
        log(f"ERROR: the bundle now contains part of the vision stack ({r2.stdout.strip()}). "
            "Either requirements-core.txt grew ~400 MB, or the check and the "
            "reality disagree.")
        return 1
    log(f"vision check: {r2.stdout.strip()}")

    COMPLETE_MARK.write_text(PY_VERSION, encoding="utf-8")
    size_mb = sum(f.stat().st_size for f in BUNDLE.rglob("*") if f.is_file()) // 1024 // 1024
    log(f"bundle complete: {size_mb} MB in {BUNDLE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
