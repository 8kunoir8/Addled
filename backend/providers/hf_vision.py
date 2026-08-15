"""
Hugging Face Florence-2-base — local vision fallback for ALL providers.

Lazy-loads `microsoft/Florence-2-base` via transformers (trust_remote_code).
Used when a provider's cloud vision call fails, so Addled always has eyes —
even offline, with no API key at all.

Requires: pip install transformers torch
Model: https://huggingface.co/microsoft/Florence-2-base (~0.23B params, CPU-friendly)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
import time
from pathlib import Path

from backend.providers.base import ProviderResult

log = logging.getLogger("addled.hf_vision")

DEFAULT_MODEL_ID = "microsoft/Florence-2-base"
DEFAULT_TASK = "<MORE_DETAILED_CAPTION>"


class HFVisionFallback:
    """Local Florence-2 vision model with lazy loading and caching."""

    def __init__(self, model_id: str | None = None):
        from backend.config import config
        self._model_id = model_id or config.get(
            "vision", "hf_model", default=DEFAULT_MODEL_ID)
        self._model = None
        self._processor = None
        self._load_error: str | None = None
        self._on_cuda = False

    @property
    def enabled(self) -> bool:
        from backend.config import config
        return config.get("vision", "fallback_enabled", default=True)

    @property
    def model_id(self) -> str:
        return self._model_id

    def _ensure_loaded(self) -> bool:
        """Blocking load — call only from executor thread."""
        if self._model is not None:
            return True
        if self._load_error:
            log.debug("HF vision unavailable (cached error): %s", self._load_error)
            return False

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor

            log.info("Loading %s (first use — may take a few minutes)...", self._model_id)
            self._model = AutoModelForCausalLM.from_pretrained(
                self._model_id, trust_remote_code=True)
            self._processor = AutoProcessor.from_pretrained(
                self._model_id, trust_remote_code=True)

            if torch.cuda.is_available():
                self._model = self._model.cuda().eval()
                self._on_cuda = True
                log.info("Florence-2 loaded on CUDA")
            else:
                self._model = self._model.eval()
                log.warning("No CUDA — Florence-2 on CPU will be slower")
            return True
        except ImportError as e:
            self._load_error = f"Missing dependency: {e}. Run: pip install transformers torch"
            log.warning(self._load_error)
            return False
        except Exception as e:
            self._load_error = f"Model load failed: {e}"
            log.warning(self._load_error)
            return False

    def _generate_sync(self, image_path: str, prompt: str,
                       max_new_tokens: int = 512) -> str:
        """Blocking generate using the official Florence-2 recipe."""
        if not self._ensure_loaded():
            return ""

        try:
            from PIL import Image

            image = Image.open(image_path).convert("RGB")
            text_input = f"{DEFAULT_TASK}{prompt}"

            inputs = self._processor(
                text=text_input, images=image, return_tensors="pt")
            if self._on_cuda:
                inputs = {k: v.cuda() for k, v in inputs.items()}

            generated_ids = self._model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=max_new_tokens,
                num_beams=3 if self._on_cuda else 1,
                do_sample=False,
            )

            generated_text = self._processor.batch_decode(
                generated_ids, skip_special_tokens=False)[0]

            parsed = self._processor.post_process_generation(
                generated_text,
                task=DEFAULT_TASK,
                image_size=(image.width, image.height),
            )
            answer = parsed.get(DEFAULT_TASK, generated_text)
            return str(answer).strip()
        except Exception as e:
            log.warning("Florence-2 generation failed: %s", e)
            self._load_error = f"Generation failed: {e}"
            return ""

    async def analyze(self, image_b64: str, prompt: str,
                      max_new_tokens: int = 512) -> ProviderResult:
        """Async wrapper. Returns ProviderResult like any provider.vision()."""
        if not self.enabled:
            return ProviderResult(ok=False, error="HF vision fallback disabled in settings")

        t0 = time.monotonic()

        # Quick availability check without loading the model
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
        except ImportError as e:
            return ProviderResult(
                ok=False,
                error=f"HF fallback unavailable: {e}. Run: pip install transformers torch",
            )

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        try:
            tmp.write(base64.b64decode(image_b64))
            tmp.close()

            loop = asyncio.get_running_loop()
            answer = await loop.run_in_executor(
                None, self._generate_sync, tmp.name, prompt, max_new_tokens)
        finally:
            try:
                Path(tmp.name).unlink(missing_ok=True)
            except OSError:
                pass

        if not answer:
            return ProviderResult(
                ok=False,
                error=self._load_error or "HF vision model returned no answer",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        return ProviderResult(
            ok=True,
            response=answer,
            model=self._model_id,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


# ── Singleton ────────────────────────────────────────────────────────────

hf_vision = HFVisionFallback()
