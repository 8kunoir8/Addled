"""The Kokoro voice models, downloaded when the user asks for them.

Kokoro is the good offline TTS engine. Its two model files are 356 MB of a
589 MB installer, and most of that is one 325 MB ONNX graph — for a feature
that not every user turns on. So they are no longer bundled: Settings → Voice
offers a button, and this downloads them.

That is not a new kind of thing for this codebase. The Chromium Playwright
installs (~150 MB) and the torch stack behind local embeddings (~2.5 GB) are
already fetched on demand, and **SenseVoice, the other voice model, is already
unbundled** — `scripts/fetch_voice_models.py --sensevoice`. Kokoro was the odd
one out in still being shipped.

What this guarantees, and what it does not, because the difference matters:

* The URL is HTTPS on a host `download.ALLOWED_HOSTS` permits, checked before a
  request is made.
* The bytes are streamed to a `.part` file and checked against a **pinned
  sha256**, then moved into place with `os.replace`. The pin is in this file.
* The pinned digests were computed from copies proven to be the upstream ones:
  the byte counts match the release's `Content-Length` exactly, and the first
  megabyte of `voices-v1.0.bin` matches upstream's. Upstream publishes **no**
  digest for these files, so this pin is ours and is the only thing standing
  between a corrupted transfer and a broken engine.
* This is **not** signature verification and does not defend against a
  compromised upstream release — only against a bad transfer or a substituted
  URL. Saying so is better than implying otherwise.
* Nothing here runs on its own. No startup path downloads; a click starts it.

The models land in the data directory, never beside the code: the app installs
to `C:\\Program Files`, which is not writable. `voice.tts` looks there second,
after a bundled copy, so a checkout and a pre-existing install keep working
without ever needing this.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.error
from pathlib import Path

log = logging.getLogger("addled.voice.model_install")

from backend.tools.download import (MAX_MODEL_BYTES,  # noqa: F401
                                    fetch_to_file, require_allowed)

# The release these come from, and the two files. Pinned by digest below.
RELEASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
           "model-files-v1.0")

# fp32 only. The quantised int8 (92 MB) and fp16 (177 MB) variants exist
# upstream and would be cheaper to fetch, but they are a different model
# quality and are an explicit follow-up, not a silent substitution. A user who
# clicks once should get the same voice the installer used to carry.
MODELS = (
    {
        "filename": "kokoro-v1.0.onnx",
        "bytes": 325_532_387,
        "sha256": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    },
    {
        "filename": "voices-v1.0.bin",
        "bytes": 28_214_398,
        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    },
)

TOTAL_BYTES = sum(m["bytes"] for m in MODELS)
PROVENANCE = "installed.json"

# The cap the download honours is the model one, not the archive one: 325 MB
# does not fit under `MAX_ARCHIVE_BYTES`, which is 96 MB.
for _m in MODELS:
    assert _m["bytes"] <= MAX_MODEL_BYTES, _m["filename"]

_installing = False
_task: asyncio.Task | None = None
_phase = ""
_pct = 0
_detail = ""
_error = ""

# -- where they live ----------------------------------------------------------

def install_dir() -> Path:
    """The data directory's `voice/models`, where a download is written.

    Never beside the code: the install is read-only, so a model written there
    would fail on a real install and would vanish on upgrade anyway. The
    bundled copy is found by `voice.tts`, not written by this module.
    """
    from backend.app_paths import VOICE_MODELS_DIR
    return VOICE_MODELS_DIR

def bundled_dir() -> Path:
    """Where a checkout or a pre-change install keeps its copy."""
    from backend.voice.tts import BUNDLED_MODELS_DIR
    return BUNDLED_MODELS_DIR

def _target(filename: str) -> Path:
    return install_dir() / filename

def _provenance() -> dict:
    try:
        path = install_dir() / PROVENANCE
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}

def _verified(filename: str, record: dict) -> bool:
    """Is the file on disk the one the pin describes, and not still a `.part`?"""
    entry = (record.get("files") or {}).get(filename) or {}
    if entry.get("sha256") != _pin(filename)["sha256"]:
        return False
    path = _target(filename)
    try:
        return path.is_file() and path.stat().st_size == _pin(filename)["bytes"]
    except OSError:
        return False

def _pin(filename: str) -> dict:
    for model in MODELS:
        if model["filename"] == filename:
            return model
    raise KeyError(filename)

def status() -> dict:
    """What the dashboard needs to draw the row: five states, and the size.

    "Installed" means a real file whose digest the provenance file records,
    not merely a file of the right name. A leftover `.part` or a hand-copied
    file must not read as installed, or the button would never fix it.

    `bundled` is reported separately, because it changes what the button should
    say: a checkout already has the models and needs no download at all.
    """
    bundled = all((bundled_dir() / m["filename"]).is_file() for m in MODELS)
    record = _provenance()
    installed = all(_verified(m["filename"], record) for m in MODELS)
    # A file present but not matching the pin: worth distinguishing from absent,
    # because the fix is a re-download rather than a first download.
    downloaded = all(_target(m["filename"]).is_file() for m in MODELS)
    return {
        "supported": sys.platform == "win32",
        "bundled": bundled,
        "installed": installed,
        "download_present": downloaded,
        "available": bundled or installed,
        "models": [{"filename": m["filename"], "bytes": m["bytes"]}
                   for m in MODELS],
        "total_bytes": TOTAL_BYTES,
        "installed_at": str(record.get("installed_at") or ""),
        "size_bytes": int(record.get("total_bytes") or 0),
        "install_dir": str(install_dir()),
        "source": ("bundled" if bundled else
                   "downloaded" if installed else "absent"),
        # Named so the dashboard can say where they come from and how big the
        # download is before anyone clicks, rather than only after.
        "sources": [f"{RELEASE}/{m['filename']}" for m in MODELS],
        "installing": _installing,
        "phase": _phase,
        "percent": _pct,
        "detail": _detail,
        "error": _error,
    }

# -- the install --------------------------------------------------------------

def _emit(phase: str, pct: int = 0, detail: str = "") -> None:
    global _phase, _pct, _detail
    _phase, _pct, _detail = phase, max(0, min(100, int(pct))), detail
    log.info("Kokoro model install: %s %d%% %s", phase, _pct, detail)
    try:
        from backend.ws_server import get_server
        get_server().broadcast_nowait("system.voiceModelProgress", status())
    except Exception as e:  # noqa: BLE001
        log.debug("Could not broadcast voice model progress: %s", e)

def installing() -> bool:
    return _installing

async def wait(timeout: float = 1800) -> dict:
    """Await the running install. For tests and for a caller that wants to know."""
    if _task is None:
        return status()
    try:
        await asyncio.wait_for(asyncio.shield(_task), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("Kokoro model install did not finish within %.0fs", timeout)
    return status()

async def install(force: bool = False, remote: bool = False) -> dict:
    """Start the download. Returns immediately; progress is broadcast."""
    global _installing, _task, _error
    if remote:
        return {"success": False,
                "error": "Downloading the voice models has to be done on the "
                         "machine itself. Remote access can use the voice, but "
                         "not fetch 356 MB for it."}
    if sys.platform != "win32":
        return {"success": False,
                "error": "The voice models are only published for Windows "
                         "here. Run scripts/fetch_voice_models.py instead."}
    if _installing:
        return {"success": False, "already_running": True,
                "message": "A download is already in progress.", **status()}
    if status()["available"] and not force:
        return {"success": False, "already_installed": True,
                "error": "The voice models are already installed.", **status()}

    _installing = True
    _error = ""
    _emit("starting", 0, f"About to download {TOTAL_BYTES / 1048576:.0f} MB")
    _task = asyncio.create_task(asyncio.to_thread(download_all))
    return {"success": True, "started": True,
            "message": "Downloading the voice models…"}

def download_all() -> None:
    """The whole download, synchronously.

    Public because `scripts/fetch_kokoro.py` and the suite both drive it
    directly — one implementation of the verification, called from three
    places, so a test exercises the same code the button does.
    """
    global _installing, _error
    destination = install_dir()
    written: dict[str, dict] = {}
    total = 0
    try:
        destination.mkdir(parents=True, exist_ok=True)
        # A stale provenance file would let a failed run read as complete, so
        # it goes first and is rewritten only once every file is in place.
        (destination / PROVENANCE).unlink(missing_ok=True)

        for index, model in enumerate(MODELS):
            filename = model["filename"]
            url = f"{RELEASE}/{filename}"
            require_allowed(url)
            base = int(100 * index / len(MODELS))
            span = int(100 / len(MODELS))
            _emit("downloading", base,
                  f"{filename} — starting "
                  f"({model['bytes'] / 1048576:.0f} MB)")

            def on_progress(received: int, expected: int, _f=filename,
                            _base=base, _span=span) -> None:
                expected = expected or _pin(_f)["bytes"]
                pct = _base + int(_span * received / expected) if expected else _base
                _emit("downloading", pct,
                      f"{_f} — {received / 1048576:.1f} MB"
                      + (f" of {expected / 1048576:.1f} MB" if expected else ""))

            result = fetch_to_file(url, _target(filename),
                                   on_progress=on_progress,
                                   expected_bytes=model["bytes"])
            # `fetch_to_file` proves the transfer finished; it cannot know the
            # digest that is *ours*. That check is here, and it is the one that
            # matters: a substituted file of the right length passes the first
            # and fails this.
            if result["sha256"] != model["sha256"]:
                raise ValueError(
                    f"{filename} does not match the pinned sha256 — got "
                    f"{result['sha256'][:16]}…, expected "
                    f"{model['sha256'][:16]}…. Refusing to install it.")
            written[filename] = {
                "sha256": result["sha256"], "bytes": result["bytes"],
                "url": url,
            }
            total += result["bytes"]
            _emit("downloading", base + span, f"{filename} — verified")

        # Provenance last: its presence is what makes the install complete, so a
        # half-finished download does not read as a successful one, and
        # `status()` will keep reporting "absent" until it is written.
        (destination / PROVENANCE).write_text(json.dumps({
            "release": RELEASE,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_bytes": total,
            "files": written,
        }, indent=2), encoding="utf-8")

        _installing = False
        _emit("done", 100, f"Installed into {destination}")
        # The catalogue caches an empty list when the pack is missing, so
        # without this the voices stay unlisted until the next restart.
        try:
            from backend.voice import voices
            voices.kokoro_voices(refresh=True)
        except Exception as e:  # noqa: BLE001
            log.debug("Could not refresh the voice catalogue: %s", e)
    except (urllib.error.URLError, ValueError, OSError) as e:
        _error = str(e)
        log.warning("Kokoro model install failed: %s", e)
        _installing = False
        # Remove every model file this run could have created, not merely the
        # ones it got as far as recording. The digest check raises *after*
        # `fetch_to_file` has moved the file into place, so the failing file is
        # precisely the one that is not in `written` — cleaning only `written`
        # would leave the bad file for `status()` and the engine to find.
        for model in MODELS:
            try:
                _target(model["filename"]).unlink(missing_ok=True)
            except OSError:
                pass
        _emit("failed", 0, str(e))
    except Exception as e:  # noqa: BLE001
        _error = f"{type(e).__name__}: {e}"
        log.exception("Kokoro model install failed unexpectedly")
        _installing = False
        _emit("failed", 0, _error)
