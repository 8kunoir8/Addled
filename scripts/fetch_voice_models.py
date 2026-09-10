"""Download voice models into backend/voice/models/.

Idempotent — skips files that already exist. Run during build/packaging and
optionally by hand (deploy copies backend/voice/models/ to the installed app).

The app is safe without these files:
  - VAD:    falls back to RMS energy gating (legacy behavior)
  - TTS:    falls back to edge-tts (online)
  - STT:    falls back to faster-whisper (auto-downloads its own model)

Models:
  silero_vad.onnx   Silero VAD v5 (~2 MB)      — copied from the bundled
                    silero-vad package when present, else GitHub.
  kokoro-v1.0.onnx  Kokoro 82M TTS (~330 MB)   — thewh1teagle kokoro-onnx
  voices-v1.0.bin   Kokoro voice packs (~90 MB)— same release (np.load .bin)
  SenseVoiceSmall/* SenseVoice ASR (optional, --sensevoice) — ModelScope
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import urllib.request
from pathlib import Path

SILERO_URLS = [
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
    "https://huggingface.co/snakers4/silero-vad/resolve/main/silero_vad.onnx",
]
KOKORO_RELEASES = [
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0",
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/v1.0",
]
SENSEVOICE_FILES = [
    # (remote relative path on modelscope, local filename)
    ("onnx/model.onnx", "model.onnx"),
    ("config.yaml", "config.yaml"),
    ("am.mvn", "am.mvn"),
    ("tokens.json", "tokens.json"),
]
SENSEVOICE_BASE = "https://www.modelscope.cn/models/iic/SenseVoiceSmall/resolve/master"

UA = {"User-Agent": "Addled/1.0 (local voice model fetcher)"}


def _fetch(url: str, dest: Path, timeout: int = 600) -> bool:
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"skip {dest.name} (exists)")
        return True
    print(f"fetch {url}")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        dest.write_bytes(data)
        print(f"  -> {dest} ({len(data) // 1024} KB)")
        return len(data) > 1000
    except Exception as e:
        print(f"FAILED {dest.name}: {e}")
        return False


def fetch_silero(out: Path) -> bool:
    dest = out / "silero_vad.onnx"
    # Prefer the model bundled with the silero-vad package (already on disk).
    try:
        spec = importlib.util.find_spec("silero_vad")
        if spec and spec.submodule_search_locations:
            src = Path(spec.submodule_search_locations[0]) / "data" / "silero_vad.onnx"
            if src.exists():
                if dest.exists() and dest.stat().st_size > 1000:
                    print("skip silero_vad.onnx (exists)")
                    return True
                shutil.copyfile(src, dest)
                print(f"copied {src} -> {dest} ({dest.stat().st_size // 1024} KB)")
                return True
    except Exception as e:
        print(f"bundled silero copy failed: {e}")
    for url in SILERO_URLS:
        if _fetch(url, dest):
            return True
    return False


def fetch_kokoro(out: Path) -> bool:
    ok = True
    for base in KOKORO_RELEASES:
        if (out / "kokoro-v1.0.onnx").exists():
            break
        _fetch(f"{base}/kokoro-v1.0.onnx", out / "kokoro-v1.0.onnx")
    if not (out / "voices-v1.0.bin").exists():
        _fetch(f"{KOKORO_RELEASES[0]}/voices-v1.0.bin", out / "voices-v1.0.bin")
    ok = ((out / "kokoro-v1.0.onnx").exists()
          and (out / "voices-v1.0.bin").exists())
    return ok


def fetch_sensevoice(out: Path) -> bool:
    model_dir = out / "SenseVoiceSmall"
    model_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for remote, local in SENSEVOICE_FILES:
        if not _fetch(f"{SENSEVOICE_BASE}/{remote}", model_dir / local):
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensevoice", action="store_true",
                    help="also fetch SenseVoice ASR (~900 MB)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out = root / "backend" / "voice" / "models"
    out.mkdir(parents=True, exist_ok=True)

    ok = True
    ok &= fetch_silero(out)
    ok &= fetch_kokoro(out)
    if args.sensevoice:
        ok &= fetch_sensevoice(out)

    print("VOICE MODELS OK" if ok else "VOICE MODELS INCOMPLETE (fallbacks active)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
