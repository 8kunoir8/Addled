"""
Hugging Face (Local) provider — runs a downloaded Hugging Face model in-process.

Loading follows the same strategy as ``backend/providers/hf_vision.py``: lazy load
on first use, a cached load error so a failed 2 GB load is not retried on every
message, and generation offloaded to a worker thread so the asyncio loop stays
responsive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import AsyncIterator

from backend.providers.base import BaseProvider, ProviderResult

log = logging.getLogger("addled.hf_local")

INSTALL_HINT = (
    "Hugging Face (Local) needs the optional torch + transformers packages. "
    "Install them from Settings — Providers — Local AI."
)

GENERATE_TIMEOUT_S = 600

# Packages that must be importable WITH -s for the local models to actually
# run. transformers alone is not enough: Florence-2's remote code (and timm's
# model registry) import einops and timm at call time, so a machine can pass a
# torch+transformers check and still fail on the first image with "requires
# einops, timm".
BASE_DEPS = ("torch", "transformers")
# What local vision needs on TOP of BASE_DEPS. Kept as a superset so
# vision_deps_available() needs one call, not two, and so "is this in the
# vision set?" is answerable without knowing the split.
VISION_DEPS = BASE_DEPS + ("einops", "timm")

_lock = threading.Lock()
_state: dict = {"key": None, "tokenizer": None, "model": None, "device": None}
_load_error: str | None = None


# ---- paths / dependency checks ----------------------------------------------

def models_root(configured: str = "") -> str:
    """Where Hugging Face downloads live (inside Addled's portable memory dir)."""
    if configured:
        return configured
    from backend.config import SETTINGS_PATH
    return str(SETTINGS_PATH.parent / "models" / "hf")


def prepare_env(configured_root: str = "") -> str:
    """Point HF_HOME at Addled's memory folder so nothing escapes the app dir."""
    if os.environ.get("HF_HOME"):
        return os.environ["HF_HOME"]
    root = models_root(configured_root)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create HF model dir %s: %s", root, exc)
        return root
    os.environ["HF_HOME"] = root
    return root


def missing_deps(*names: str) -> list[str]:
    """Which of ``names`` cannot be imported RIGHT NOW (respecting ``-s``).

    Uses find_spec rather than import so the check is cheap and does not pay
    the cost of importing torch.
    """
    import importlib.util

    absent: list[str] = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                absent.append(name)
        except (ImportError, ValueError):
            absent.append(name)
    return absent

def deps_available() -> tuple[bool, str]:
    """Return (ok, reason) for the optional torch/transformers dependency.

    This is the BASE pair only — the Local AI provider needs just these. Vision
    has a larger requirement; see ``vision_deps_available``.
    """
    absent = missing_deps(*BASE_DEPS)
    if absent:
        return False, f"{INSTALL_HINT} (missing: {', '.join(absent)})"
    return True, ""

def vision_deps_available() -> tuple[bool, str]:
    """Return (ok, reason) for the full local-vision stack.

    Florence-2 loads torch and transformers, then its remote code imports
    einops and timm. Checking only the first pair is what let the app report
    "ready" while every image failed with "requires einops, timm".
    """
    absent = missing_deps(*VISION_DEPS)
    if absent:
        return False, (
            "Local vision (Florence-2) needs "
            + ", ".join(absent)
            + ". Install from Settings — Providers — Local AI."
        )
    return True, ""


def snapshot_present(model_id: str, configured_root: str = "") -> bool:
    """Cheap check for a cached snapshot of ``model_id``."""
    root = os.environ.get("HF_HOME") or models_root(configured_root)
    folder = "models--" + model_id.replace("/", "--")
    snapshots = os.path.join(root, "hub", folder, "snapshots")
    try:
        return any(os.scandir(snapshots))
    except OSError:
        return False


# ---- loading ----------------------------------------------------------------

def _resolve_device(torch, preference: str) -> str:
    if preference and preference != "auto":
        return preference
    return "cuda" if torch.cuda.is_available() else "cpu"


def _resolve_dtype(torch, preference: str, device: str):
    if preference and preference != "auto":
        return getattr(torch, preference, None) or torch.float32
    if device == "cuda":
        return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    return torch.float32


def _load(model_id: str, device_pref: str, dtype_pref: str,
          load_in_4bit: bool, token: str) -> dict:
    global _load_error
    key = (model_id, device_pref, dtype_pref, bool(load_in_4bit))
    with _lock:
        if _state["key"] == key and _state["model"] is not None:
            return _state
        if _load_error is not None and _state["key"] == key:
            raise RuntimeError(_load_error)

    ok, reason = deps_available()
    if not ok:
        with _lock:
            _state["key"] = key
            _load_error = reason
        raise RuntimeError(reason)

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = _resolve_device(torch, device_pref)
    dtype = _resolve_dtype(torch, dtype_pref, device)
    auth = {"token": token} if token else {}

    log.info("Loading local HF model %s on %s (%s)...", model_id, device, dtype)
    tokenizer = AutoTokenizer.from_pretrained(model_id, **auth)

    model_kwargs: dict = {"dtype": dtype, "low_cpu_mem_usage": True}
    if load_in_4bit:
        try:
            from transformers import BitsAndBytesConfig
            model_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=dtype)
            model_kwargs["device_map"] = "auto"
            model_kwargs.pop("dtype", None)
        except Exception as exc:  # bitsandbytes missing on this platform
            log.warning("4-bit load unavailable, falling back to %s: %s", dtype, exc)

    try:
        model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
    except TypeError as exc:
        # Older transformers uses `torch_dtype` instead of `dtype`.
        if "dtype" in str(exc) and "dtype" in model_kwargs:
            model_kwargs["torch_dtype"] = model_kwargs.pop("dtype")
            model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)
        else:
            raise

    if "device_map" not in model_kwargs:
        model = model.to(device)
    model.eval()

    with _lock:
        _state.update({"key": key, "tokenizer": tokenizer,
                       "model": model, "device": device})
        _load_error = None
    log.info("Local HF model ready: %s (%s)", model_id, device)
    return _state


def unload() -> None:
    """Drop the loaded model to free RAM (called from Settings)."""
    global _load_error
    with _lock:
        _state.update({"key": None, "tokenizer": None, "model": None, "device": None})
        _load_error = None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _generate(state: dict, messages: list[dict], max_tokens: int,
              temperature: float) -> tuple[str, int, int]:
    import torch

    tokenizer = state["tokenizer"]
    model = state["model"]

    try:
        prompt = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
    except Exception:
        # No chat template in the checkpoint — fall back to a plain transcript.
        prompt = "\n".join(
            f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
        ) + "\nassistant:"

    inputs = tokenizer(prompt, return_tensors="pt")
    inputs = {name: tensor.to(model.device) for name, tensor in inputs.items()}

    gen_kwargs: dict = {"max_new_tokens": int(max_tokens)}
    if temperature and float(temperature) > 0:
        gen_kwargs.update({"do_sample": True, "temperature": float(temperature),
                           "top_p": 0.95})
    else:
        gen_kwargs["do_sample"] = False

    with torch.inference_mode():
        output = model.generate(**inputs, **gen_kwargs)

    prompt_len = int(inputs["input_ids"].shape[-1])
    new_tokens = output[0][prompt_len:]
    text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    return text, prompt_len, int(new_tokens.shape[-1])


# ---- provider ---------------------------------------------------------------

class HuggingFaceLocalProvider(BaseProvider):
    provider_id = "huggingface"
    provider_name = "Hugging Face (Local)"
    supports_vision = False
    supports_streaming = False

    def _model_id(self) -> str:
        return self._config.get("default_model") or "Qwen/Qwen3-4B-Instruct-2507"

    def _token(self) -> str:
        return self._config.get("api_key", "") or os.environ.get("HF_TOKEN", "")

    async def _ensure_loaded(self, model_id: str) -> dict:
        prepare_env(self._config.get("model_root", ""))
        return await asyncio.to_thread(
            _load,
            model_id,
            self._config.get("device", "auto"),
            self._config.get("dtype", "auto"),
            bool(self._config.get("load_in_4bit", False)),
            self._token(),
        )

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ProviderResult:
        t0 = time.monotonic()
        model_id = model or self._model_id()
        try:
            state = await self._ensure_loaded(model_id)
        except Exception as exc:
            return ProviderResult(ok=False, error=str(exc),
                                  duration_ms=int((time.monotonic() - t0) * 1000))

        configured_cap = int(self._config.get("max_new_tokens", 1024) or 1024)
        limit = max(1, min(int(max_tokens or configured_cap), configured_cap))

        try:
            text, tok_in, tok_out = await asyncio.wait_for(
                asyncio.to_thread(_generate, state, messages, limit, temperature),
                timeout=GENERATE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return ProviderResult(
                ok=False,
                error=f"Local generation timed out after {GENERATE_TIMEOUT_S}s.",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as exc:
            return ProviderResult(ok=False, error=f"Local generation failed: {exc}",
                                  duration_ms=int((time.monotonic() - t0) * 1000))

        return ProviderResult(
            ok=True,
            response=text,
            model=model_id,
            tokens_in=tok_in,
            tokens_out=tok_out,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        result = await self.chat(messages, model=model, max_tokens=max_tokens,
                                 temperature=temperature)
        if result.ok and result.response:
            yield result.response

    async def list_models(self) -> list[str]:
        return self._config.get("models", [])

    async def validate(self) -> dict:
        ok, reason = deps_available()
        if not ok:
            return {"ok": False, "models": [], "error": reason}
        model_id = self._model_id()
        if not snapshot_present(model_id, self._config.get("model_root", "")):
            return {
                "ok": False,
                "models": [],
                "error": f"{model_id} is not downloaded yet.",
            }
        return {"ok": True, "models": await self.list_models()}
