"""
Hugging Face DeepSeek-VL2-tiny — local vision fallback for ALL providers.

Lazy-loads `deepseek-ai/deepseek-vl2-tiny` via transformers (trust_remote_code).
Used when a provider's cloud vision call fails, so Addled always has eyes —
even offline, with no API key at all.

Requires: pip install transformers torch
Model: https://huggingface.co/deepseek-ai/deepseek-vl2-tiny (~3.4B params)
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

DEFAULT_MODEL_ID = "deepseek-ai/deepseek-vl2-tiny"


class HFVisionFallback:
    """Local DeepSeek-VL2-tiny vision model with lazy loading and caching."""

    def __init__(self, model_id: str | None = None):
        from backend.config import config
        self._model_id = model_id or config.get(
            "vision", "hf_model", default=DEFAULT_MODEL_ID)
        self._vl_gpt = None
        self._tokenizer = None
        self._load_error: str | None = None
        self._load_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        from backend.config import config
        return config.get("vision", "fallback_enabled", default=True)

    @property
    def model_id(self) -> str:
        return self._model_id

    def _ensure_loaded(self) -> bool:
        """Blocking load — call only from executor thread."""
        if self._vl_gpt is not None:
            return True
        if self._load_error:
            log.debug("HF vision unavailable (cached error): %s", self._load_error)
            return False

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            log.info("Loading %s (first use — may take a few minutes)...", self._model_id)
            self._vl_gpt = AutoModelForCausalLM.from_pretrained(
                self._model_id,
                trust_remote_code=True,
                torch_dtype=torch.bfloat16,
            )
            self._tokenizer = AutoTokenizer.from_pretrained(
                self._model_id, trust_remote_code=True)

            if torch.cuda.is_available():
                self._vl_gpt = self._vl_gpt.cuda().eval()
                log.info("DeepSeek-VL2-tiny loaded on CUDA")
            else:
                self._vl_gpt = self._vl_gpt.eval()
                log.warning("No CUDA — DeepSeek-VL2-tiny on CPU will be slow")
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
        """Blocking generate using the official DeepSeek-VL2 recipe."""
        if not self._ensure_loaded():
            return ""

        conversation = [
            {
                "role": "<|User|>",
                "content": f"<image>\n{prompt}",
                "images": [image_path],
            },
            {"role": "<|Assistant|>", "content": ""},
        ]

        prepare_inputs = self._vl_gpt.prepare_inputs_processing(
            conversation,
            images=[image_path],
            force_batchify=True,
            system_prompt="",
        ).to(self._vl_gpt.device)

        inputs_embeds = self._vl_gpt.prepare_inputs_embeds(**prepare_inputs)

        outputs = self._vl_gpt.language_model.generate(
            inputs_embeds=inputs_embeds,
            attention_mask=prepare_inputs.attention_mask,
            pad_token_id=self._tokenizer.eos_token_id,
            bos_token_id=self._tokenizer.bos_token_id,
            eos_token_id=self._tokenizer.eos_token_id,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            use_cache=True,
        )

        answer = self._tokenizer.decode(
            outputs[0].cpu().tolist(), skip_special_tokens=True)
        return answer.strip()

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
