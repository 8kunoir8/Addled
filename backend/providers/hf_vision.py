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
import warnings
from pathlib import Path

# transformers 5.x prints deprecation noise from its own internals
# (attention mask API, CLIPImageProcessor mapping). Cosmetic — silence it.
warnings.filterwarnings(
    "ignore", message=".*attention mask API.*", module=r"transformers.*")
warnings.filterwarnings(
    "ignore", message=".*AttentionMaskConverter.*", module=r"transformers.*")
warnings.filterwarnings(
    "ignore", message=".*image_processor_class.*", module=r"transformers.*")

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
            from transformers import (AutoModelForCausalLM, AutoProcessor,
                                      PretrainedConfig, PreTrainedModel,
                                      PreTrainedTokenizerBase)

            # ── transformers 5.x compatibility shims ────────────────────
            # (1) Florence-2's remote config code reads/writes
            #     config.forced_bos_token_id, removed from PretrainedConfig
            #     in transformers 5.x.
            if not hasattr(PretrainedConfig, "forced_bos_token_id"):
                def _get_fbos(self):
                    return getattr(self, "_forced_bos_token_id", None)

                def _set_fbos(self, value):
                    self._forced_bos_token_id = value

                PretrainedConfig.forced_bos_token_id = property(_get_fbos, _set_fbos)
                log.debug("Patched PretrainedConfig.forced_bos_token_id for transformers 5.x")

            # (2) transformers 5 checks self._supports_sdpa during __init__,
            #     BEFORE Florence-2's language_model attribute exists, so its
            #     property getter raises AttributeError. Swallow it → eager attn.
            _orig_sdpa = getattr(PreTrainedModel, "_sdpa_can_dispatch", None)
            if _orig_sdpa and not getattr(PreTrainedModel, "_addled_sdpa_patched", False):
                def _safe_sdpa(self, is_init_check=False):
                    try:
                        return _orig_sdpa(self, is_init_check)
                    except AttributeError:
                        return False

                PreTrainedModel._sdpa_can_dispatch = _safe_sdpa
                PreTrainedModel._addled_sdpa_patched = True
                log.debug("Patched PreTrainedModel._sdpa_can_dispatch for Florence-2 init")

            # (3) processing_florence2.py reads tokenizer.additional_special_tokens,
            #     removed from tokenizers in transformers 5.x. Rebuild it from
            #     special_tokens_map.
            if not hasattr(PreTrainedTokenizerBase, "additional_special_tokens"):
                def _get_ast(self):
                    m = getattr(self, "special_tokens_map", {}) or {}
                    v = m.get("additional_special_tokens", [])
                    return v if isinstance(v, list) else [v]

                PreTrainedTokenizerBase.additional_special_tokens = property(_get_ast)
                log.debug("Patched PreTrainedTokenizerBase.additional_special_tokens")
            # ────────────────────────────────────────────────────────────

            log.info("Loading %s (first use — may take a few minutes)...", self._model_id)
            # Florence-2-base checkpoints are stored in fp16; on CPU we must
            # force fp32 or conv/biases mismatch. On CUDA use bf16 for speed.
            load_dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
            self._model = AutoModelForCausalLM.from_pretrained(
                self._model_id, trust_remote_code=True, dtype=load_dtype)
            self._processor = AutoProcessor.from_pretrained(
                self._model_id, trust_remote_code=True)

            # ── (4) tie embeddings manually ─────────────────────────────
            # The checkpoint stores one shared weight
            # (language_model.model.shared.weight). The remote _tie_weights
            # hook is not invoked under transformers 5, leaving encoder
            # embed_tokens / decoder embed_tokens / lm_head randomly
            # initialized → gibberish output. Tie them to shared.weight.
            lm = self._model.language_model
            shared_w = lm.model.shared.weight
            lm.model.encoder.embed_tokens.weight = torch.nn.Parameter(shared_w)
            lm.model.decoder.embed_tokens.weight = torch.nn.Parameter(shared_w)
            lm.lm_head.weight = torch.nn.Parameter(shared_w)
            # NOTE: the 'MISSING' entries in transformers' LOAD REPORT above
            # are expected on transformers 5.x — the checkpoint stores one
            # shared weight and we re-tie it right here.
            log.info("Re-tied encoder/decoder/lm_head embeddings to shared.weight "
                     "(the MISSING entries in the load report are expected)")

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

    def _patch_generate_compat(self):
        """Patch Florence-2's prepare_inputs_for_generation for transformers 5.x.

        The remote code does `past_key_values[0][0].shape[2]` (old tuple
        cache) on BOTH Florence2ForConditionalGeneration and its inner
        Florence2LanguageForConditionalGeneration. transformers 5 passes an
        EncoderDecoderCache which isn't subscriptable — rewrite the method
        with a get_seq_length() fallback.
        """
        targets = [type(self._model)]
        inner = getattr(self._model, "language_model", None)
        if inner is not None:
            targets.append(type(inner))

        for model_cls in targets:
            try:
                if getattr(model_cls, "_addled_cache_patched", False):
                    continue

                def _patched_prepare(self_, decoder_input_ids,
                                     past_key_values=None, attention_mask=None,
                                     pixel_values=None, decoder_attention_mask=None,
                                     head_mask=None, decoder_head_mask=None,
                                     cross_attn_head_mask=None, use_cache=None,
                                     encoder_outputs=None, **kwargs):
                    if past_key_values is not None:
                        # transformers 4.x tuple cache: past_key_values[0][0].shape[2]
                        # transformers 5.x EncoderDecoderCache: .get_seq_length()
                        try:
                            past_length = past_key_values[0][0].shape[2]
                        except (TypeError, IndexError, AttributeError):
                            gl = getattr(past_key_values, "get_seq_length", None)
                            past_length = gl() if callable(gl) else 0

                        if decoder_input_ids.shape[1] > past_length:
                            remove_prefix_length = past_length
                        else:
                            remove_prefix_length = decoder_input_ids.shape[1] - 1
                        decoder_input_ids = decoder_input_ids[:, remove_prefix_length:]

                    return {
                        "input_ids": None,
                        "encoder_outputs": encoder_outputs,
                        "past_key_values": past_key_values,
                        "decoder_input_ids": decoder_input_ids,
                        "attention_mask": attention_mask,
                        "decoder_attention_mask": decoder_attention_mask,
                        "head_mask": head_mask,
                        "decoder_head_mask": decoder_head_mask,
                        "cross_attn_head_mask": cross_attn_head_mask,
                        "use_cache": use_cache,
                    }

                model_cls.prepare_inputs_for_generation = _patched_prepare
                model_cls._addled_cache_patched = True
                log.debug("Patched %s.prepare_inputs_for_generation for transformers 5.x",
                          model_cls.__name__)
            except Exception as e:
                log.warning("Cache compat patch failed on %s: %s",
                            getattr(model_cls, "__name__", "?"), e)

    def _generate_sync(self, image_path: str, prompt: str,
                       max_new_tokens: int = 512) -> str:
        """Blocking generate using the official Florence-2 recipe."""
        if not self._ensure_loaded():
            return ""
        self._patch_generate_compat()

        try:
            from PIL import Image

            image = Image.open(image_path).convert("RGB")
            # Florence-2's vision tower (DaViT) only supports square feature
            # maps — resize to 768x768 before processing.
            image = image.resize((768, 768), Image.LANCZOS)

            # Florence-2 captioning tasks accept ONLY the task token as text
            # input — a custom prompt is ignored for these task types.
            if prompt:
                log.debug("Florence-2 caption task ignores custom prompt: %s",
                          prompt[:80])
            text_input = DEFAULT_TASK

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
                use_cache=False,  # sidesteps transformers 5 cache-format issues
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
