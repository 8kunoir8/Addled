"""
The voices each TTS engine can actually speak with.

Settings has to offer the truth about *this* machine rather than a guess, and
the two engines answer that question in opposite ways:

  * **Kokoro** is installed locally, so its voices are read straight out of the
    installed ``voices-v1.0.bin``. That read must never construct a ``Kokoro``,
    because the constructor builds the 82 MB ONNX session — the voices are just
    the keys of an npz, which is all ``kokoro_onnx.Kokoro.get_voices()`` reads.
  * **Edge** voices live on Microsoft's service. They are fetched, cached, and
    only when that fetch fails does the dropdown fall back to the small curated
    set below, which is deliberately not a hardcoded catalogue of the service.
"""

from __future__ import annotations

import asyncio
import logging
import time

from backend.voice.tts import (DEFAULT_EDGE_VOICE, KOKORO_LANG_CODES,
                              KOKORO_VOICES, LANG_TO_EDGE)

log = logging.getLogger("addled.voices")

EDGE_CACHE_TTL = 24 * 3600
EDGE_TIMEOUT = 20.0

# Human-facing names; the letter assignments themselves come from tts.py so
# there is only one owner of "which letter means which language".
KOKORO_LANGUAGE_LABELS = {
    "en": "American English", "en-gb": "British English",
    "es": "Spanish", "fr": "French", "hi": "Hindi", "it": "Italian",
    "ja": "Japanese", "pt": "Brazilian Portuguese", "zh": "Mandarin Chinese",
}
_KOKORO_BY_LETTER = {letter: code for code, letter in KOKORO_LANG_CODES.items()}

# Read once: the voice pack cannot change while the process is running, and the
# settings page may ask for this repeatedly.
_KOKORO_CACHE: list[dict] | None = None
_EDGE_CACHE: dict = {"voices": [], "ts": 0.0, "error": None}


def _describe_kokoro(name: str) -> dict | None:
    """``af_heart`` → the language/gender it is filed under.

    Voice names are ``<lang><gender>_<name>``: the first letter is the Kokoro
    lang_code, the second is f/m.
    """
    if len(name) < 3 or name[1] not in ("f", "m"):
        return None
    language = _KOKORO_BY_LETTER.get(name[0])
    if not language:
        return None
    return {
        "id": name,
        "language": language,
        "language_label": KOKORO_LANGUAGE_LABELS.get(language, language),
        "gender": "female" if name[1] == "f" else "male",
        "label": name.split("_", 1)[-1].replace("_", " ").title(),
    }


def kokoro_voices(refresh: bool = False) -> list[dict]:
    """Voices in the installed Kokoro pack. Offline, and no model load."""
    global _KOKORO_CACHE
    if _KOKORO_CACHE is not None and not refresh:
        return _KOKORO_CACHE
    try:
        import numpy as np
        if not KOKORO_VOICES.exists():
            log.debug("Kokoro voices file missing: %s", KOKORO_VOICES)
            _KOKORO_CACHE = []
            return _KOKORO_CACHE
        with np.load(str(KOKORO_VOICES)) as data:
            names = list(data.keys())
    except Exception as e:
        log.debug("could not read the Kokoro voice pack: %s", e)
        _KOKORO_CACHE = []
        return _KOKORO_CACHE
    described = (_describe_kokoro(name) for name in sorted(names))
    _KOKORO_CACHE = [v for v in described if v]
    return _KOKORO_CACHE


def kokoro_languages() -> list[str]:
    """Language codes Kokoro can actually speak (it has no Indonesian)."""
    return sorted({v["language"] for v in kokoro_voices()})


def _describe_edge(raw: dict) -> dict | None:
    short = raw.get("ShortName")
    locale = raw.get("Locale") or ""
    if not short or not locale:
        return None
    name = short.split("-")[-1]
    if name.endswith("Neural"):
        name = name[: -len("Neural")]
    return {
        "id": short,
        "locale": locale,
        "language": locale.split("-")[0].lower(),
        "gender": (raw.get("Gender") or "").lower(),
        "label": name,
    }


def static_edge_voices() -> list[dict]:
    """The voices Addled already names for language routing.

    Only used when the service cannot be reached, so the dropdown is never
    empty — it is not an attempt to mirror the catalogue.
    """
    out = []
    for language, voice in LANG_TO_EDGE.items():
        parts = voice.split("-")
        locale = "-".join(parts[:2]) if len(parts) >= 3 else "en-US"
        name = parts[-1]
        if name.endswith("Neural"):
            name = name[: -len("Neural")]
        out.append({"id": voice, "locale": locale, "language": language,
                    "gender": "", "label": name})
    return sorted(out, key=lambda v: v["id"])


async def edge_voices(force: bool = False) -> dict:
    """The service's voice list, cached, with an offline fallback.

    Returns ``{"voices", "source", "error"}`` where source is one of
    ``live``/``cache``/``fallback``.
    """
    now = time.time()
    cached_fresh = (now - _EDGE_CACHE["ts"]) < EDGE_CACHE_TTL
    if not force and _EDGE_CACHE["voices"] and cached_fresh:
        return {"voices": _EDGE_CACHE["voices"], "source": "cache",
                "error": None}
    try:
        import edge_tts
        raw = await asyncio.wait_for(edge_tts.list_voices(),
                                     timeout=EDGE_TIMEOUT)
        voices = [v for v in (_describe_edge(r) for r in raw or []) if v]
        if not voices:
            raise RuntimeError("the service returned no voices")
        voices.sort(key=lambda v: (v["locale"], v["id"]))
        _EDGE_CACHE.update({"voices": voices, "ts": now, "error": None})
        return {"voices": voices, "source": "live", "error": None}
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.debug("Edge voice list unavailable (%s)", error)
        if _EDGE_CACHE["voices"]:
            return {"voices": _EDGE_CACHE["voices"], "source": "cache",
                    "error": error}
        return {"voices": static_edge_voices(), "source": "fallback",
                "error": error}


async def describe(force: bool = False) -> dict:
    """Everything the voice pickers need, in one payload."""
    edge = await edge_voices(force=force)
    return {
        "kokoro": kokoro_voices(refresh=force),
        "kokoro_languages": kokoro_languages(),
        "edge": edge["voices"],
        "edge_source": edge["source"],
        "edge_error": edge["error"],
        "defaults": {"edge": DEFAULT_EDGE_VOICE},
    }
