"""
llamafile-backed local model runtime.

Owns three things:
  1. the "ask before downloading" state,
  2. the resumable download of the runtime + GGUF weights,
  3. the server process lifecycle (lazy start, readiness poll, idle unload).

The heavy lifting for downloads lives in ``backend.local_models``.

NOTE on GPU flags: llamafile 0.10.x accepts llama.cpp flags, so layer offload is
``-ngl``. ``gpu="auto"`` offloads everything when an NVIDIA driver is present and
otherwise runs on CPU. Run ``llamafile.exe --help`` after the first download to
confirm the flags for your build, and use ``local_llm.extra_args`` to override.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time

from backend.config import config
from backend.local_models import paths
from backend.local_models.downloader import DownloadCancelled, download, has_space

log = logging.getLogger("addled.local_llm")

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOOPBACK = "127.0.0.1"
RUNTIME_PCT = 15.0          # runtime occupies the first 15% of the progress bar
READY_TIMEOUT_S = 300.0
IDLE_TICK_S = 60.0


class LocalLlmManager:
    """Singleton owner of the llamafile server + weights."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._log_handle = None
        self._port = 0
        self._model_id: str | None = None
        self._phase = "idle"           # idle | downloading | starting | running | error
        self._progress = 0.0
        self._detail = ""
        self._last_error = ""
        self._last_used = 0.0
        self._downloading = False
        self._start_lock: asyncio.Lock | None = None
        self._gpu_checked = False
        self._has_nvidia = False
        self._hf_deps_installing = False
        self._hf_deps_cache: tuple[bool, str] | None = None

    # ---- small helpers -------------------------------------------------------

    def _cfg(self, key: str, default=None):
        return config.get("local_llm", key, default=default)

    def _async_lock(self) -> asyncio.Lock:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        return self._start_lock

    def _broadcast(self, method: str, params: dict) -> None:
        try:
            from backend.ws_server import get_server
            get_server().broadcast_nowait(method, params)
        except Exception:
            pass

    def _log_path(self) -> str:
        return str(paths.LLAMAFILE_DIR / "llamafile.log")

    def _has_gpu(self) -> bool:
        if not self._gpu_checked:
            self._gpu_checked = True
            self._has_nvidia = shutil.which("nvidia-smi") is not None
        return self._has_nvidia

    # ---- status --------------------------------------------------------------

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.returncode is None

    def port(self) -> int:
        return self._port or int(self._cfg("port", default=8090) or 8090)

    def api_base(self) -> str:
        return f"http://{LOOPBACK}:{self.port()}/v1"

    def model_id(self) -> str:
        return self._model_id or str(self._cfg("model_name", default="local-model"))

    def size_mb(self) -> int:
        return int(self._cfg("size_mb", default=2400) or 2400)

    def should_ask(self) -> bool:
        return bool(self._cfg("prompt_on_first_run", default=True)) \
            and not bool(self._cfg("asked", default=False)) \
            and not paths.installed()

    def is_declined(self) -> bool:
        return bool(self._cfg("declined", default=False))

    def status(self) -> dict:
        free_ok, free_mb = has_space(self.size_mb(), margin_mb=0)
        return {
            "backend": self._cfg("backend", default="llamafile"),
            "installed": paths.installed(),
            "runtime_present": paths.runtime_exists(),
            "model_present": paths.model_exists(),
            "running": self.is_running(),
            "pid": getattr(self._proc, "pid", None),
            "port": self.port(),
            "phase": self._phase,
            "downloading": self._downloading,
            "progress": self._progress,
            "detail": self._detail,
            "error": self._last_error,
            "model_name": self._cfg("model_name", default=""),
            "model_file": self._cfg("model_file", default=""),
            "size_mb": self.size_mb(),
            "installed_mb": int(paths.installed_bytes() / (1024 * 1024)),
            "free_mb": free_mb,
            "space_ok": free_ok,
            "asked": bool(self._cfg("asked", default=False)),
            "approved": bool(self._cfg("download_approved", default=False)),
            "declined": self.is_declined(),
            # Ask on every launch until the user actually answers, so the prompt
            # is never lost to a missed broadcast.
            "should_ask": self.should_ask() and not self._downloading,
            "dir": str(paths.LLAMAFILE_DIR),
        }

    def hf_status(self) -> dict:
        """Optional Hugging Face (Local) provider readiness."""
        from backend.providers.huggingface_local_provider import (
            deps_available, models_root, snapshot_present,
        )
        from backend.config import config as app_config
        configured_root = app_config.get(
            "providers", "builtin", "huggingface", "model_root", default="") or ""
        model_id = app_config.get(
            "providers", "builtin", "huggingface", "default_model",
            default="Qwen/Qwen3-4B-Instruct-2507") or ""
        if self._hf_deps_cache is None:
            self._hf_deps_cache = deps_available()
        deps_ok, reason = self._hf_deps_cache
        return {
            "model_id": model_id,
            "deps_ready": deps_ok,
            "error": reason,
            "downloaded": snapshot_present(model_id, configured_root),
            "root": models_root(configured_root),
            "installing": self._hf_deps_installing,
        }

    # ---- ask / approve / decline --------------------------------------------

    def ask(self) -> dict:
        """Broadcast the 'download the local model?' prompt.

        ``asked`` is only recorded when the user actually answers (approve or
        decline), so the prompt survives a missed broadcast.
        """
        payload = {
            "size_mb": self.size_mb(),
            "model_file": self._cfg("model_file", default=""),
            "runtime_mb": 368,
        }
        self._broadcast("local.llmInstallRequest", payload)
        return payload

    def approve(self) -> dict:
        config.set("local_llm", "asked", value=True)
        config.set("local_llm", "download_approved", value=True)
        config.set("local_llm", "declined", value=False)
        return {"ok": True, "active": config.active_provider}

    def decline(self) -> dict:
        """User said no → OpenRouter becomes the default (needs their API key)."""
        config.set("local_llm", "asked", value=True)
        config.set("local_llm", "download_approved", value=False)
        config.set("local_llm", "declined", value=True)
        config.set("providers", "active", value="openrouter")
        key = config.get("providers", "builtin", "openrouter", "api_key",
                         default="") or ""
        log.info("Local download declined — active provider switched to OpenRouter")
        return {"ok": True, "active": "openrouter", "needs_key": not bool(key)}

    # ---- download ------------------------------------------------------------

    async def install(self) -> dict:
        """Download whatever is missing (runtime and/or weights)."""
        if self._downloading:
            return {"ok": False, "error": "A download is already in progress."}

        need_runtime = not paths.runtime_exists()
        need_model = not paths.model_exists()
        if not need_runtime and not need_model:
            config.set("local_llm", "enabled", value=True)
            config.set("local_llm", "declined", value=False)
            return {"ok": True, "already_installed": True}

        ok, free_mb = has_space(self.size_mb())
        if not ok:
            message = (f"Not enough free disk space: {free_mb} MB free, about "
                       f"{self.size_mb()} MB needed.")
            self._last_error = message
            return {"ok": False, "error": message}

        paths.ensure_dirs()
        self._downloading = True
        self._phase = "downloading"
        self._progress = 0.0
        self._detail = "starting"
        self._last_error = ""
        self._broadcast("local.llmProgress",
                        {"phase": "starting", "pct": 0.0, "detail": "starting"})
        try:
            if need_runtime:
                await self._download_runtime()
            if need_model:
                await self._download_model()
        except DownloadCancelled:
            self._phase = "idle"
            self._last_error = "Download cancelled."
            self._broadcast("local.llmProgress",
                            {"phase": "cancelled", "pct": self._progress,
                             "detail": self._last_error})
            return {"ok": False, "error": self._last_error}
        except Exception as exc:
            self._phase = "error"
            self._last_error = f"Download failed: {exc}"
            log.warning(self._last_error)
            self._broadcast("local.llmProgress",
                            {"phase": "failed", "pct": self._progress,
                             "detail": self._last_error})
            return {"ok": False, "error": self._last_error}
        finally:
            self._downloading = False

        config.set("local_llm", "enabled", value=True)
        config.set("local_llm", "declined", value=False)
        config.set("local_llm", "download_approved", value=True)
        self._phase = "idle"
        self._progress = 100.0
        self._detail = "installed"
        self._broadcast("local.llmProgress",
                        {"phase": "done", "pct": 100.0, "detail": "installed"})
        self._broadcast("local.llmStatus", self.status())
        log.info("Local model installed (%s MB)", self.status()["installed_mb"])
        return {"ok": True}

    async def _download_runtime(self) -> None:
        url = self._cfg("runtime_url", default="")
        if not url:
            raise ValueError("local_llm.runtime_url is not configured.")
        dest = paths.llamafile_exe()
        await download(
            url, dest,
            expected_sha256=self._cfg("runtime_sha256", default="") or "",
            on_progress=lambda phase, done, total: self._emit_progress(
                "runtime", phase, done, total, 0.0, RUNTIME_PCT),
        )
        if os.name != "nt":
            try:
                os.chmod(dest, 0o755)
            except OSError:
                pass

    async def _download_model(self) -> None:
        url = self._cfg("model_url", default="")
        if not url:
            raise ValueError("local_llm.model_url is not configured.")
        await download(
            url, paths.gguf_path(),
            expected_sha256=self._cfg("model_sha256", default="") or "",
            on_progress=lambda phase, done, total: self._emit_progress(
                "model", phase, done, total, RUNTIME_PCT, 100.0),
        )

    def _emit_progress(self, part: str, phase: str, done: int,
                       total: int, lo: float, hi: float) -> None:
        if phase == "cached":
            pct = hi
        elif total:
            pct = lo + (hi - lo) * (done / total)
        else:
            pct = lo
        self._progress = round(min(99.0, pct), 1) if pct < 100 else 100.0
        mb = done / 1048576
        total_mb = total / 1048576 if total else 0
        self._detail = (
            f"{part}: {mb:.0f} MB / {total_mb:.0f} MB" if total_mb
            else f"{part}: {mb:.0f} MB"
        )
        self._broadcast("local.llmProgress", {
            "phase": phase,
            "part": part,
            "pct": self._progress,
            "downloaded_mb": int(mb),
            "total_mb": int(total_mb),
            "detail": self._detail,
        })

    # ---- process lifecycle ---------------------------------------------------

    def _gpu_args(self) -> list[str]:
        mode = str(self._cfg("gpu", default="auto") or "auto").lower()
        if mode in ("cpu", "off", "none"):
            return ["-ngl", "0"]
        if mode == "auto":
            return ["-ngl", "999"] if self._has_gpu() else ["-ngl", "0"]
        return ["-ngl", mode]

    def _launch_args(self) -> list[str]:
        args = [
            str(paths.llamafile_exe()),
            "-m", str(paths.gguf_path()),
            "--server",
            "--host", LOOPBACK,
            "--port", str(self.port()),
        ]
        ctx = int(self._cfg("ctx", default=8192) or 0)
        if ctx > 0:
            args += ["-c", str(ctx)]
        threads = int(self._cfg("threads", default=0) or 0)
        if threads > 0:
            args += ["-t", str(threads)]
        args += self._gpu_args()
        args += [str(a) for a in (self._cfg("extra_args", default=[]) or [])]
        return args

    async def start(self) -> tuple[bool, str]:
        if self.is_running():
            return True, ""
        if not paths.installed():
            return False, ("Local model is not downloaded yet. Approve the download "
                           "in Settings — Providers — Local AI.")
        paths.ensure_dirs()
        preferred = int(self._cfg("port", default=8090) or 8090)
        self._port = paths.resolve_free_port(preferred, LOOPBACK)
        if self._port != preferred:
            log.info("Port %s busy — local model will use %s", preferred, self._port)

        args = self._launch_args()
        log.info("Starting local model: %s", " ".join(args))
        try:
            self._log_handle = open(self._log_path(), "ab")
        except OSError:
            self._log_handle = None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(paths.LLAMAFILE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle or subprocess.DEVNULL,
                stderr=self._log_handle or subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self._last_error = f"Cannot start the local model: {exc}"
            log.warning(self._last_error)
            return False, self._last_error

        self._phase = "starting"
        ready, problem = await self._wait_ready()
        if not ready:
            self._phase = "error"
            self._last_error = problem
            await self.stop()
            return False, problem
        self._phase = "running"
        self._last_used = time.monotonic()
        self._last_error = ""
        log.info("Local model ready on %s (model id: %s)", self.api_base(),
                 self._model_id)
        self._broadcast("local.llmStatus", self.status())
        return True, ""

    async def _wait_ready(self, timeout: float = READY_TIMEOUT_S) -> tuple[bool, str]:
        import httpx
        url = f"http://{LOOPBACK}:{self.port()}/v1/models"
        deadline = time.monotonic() + timeout
        last = "no response"
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.returncode is not None:
                code = getattr(self._proc, "returncode", "?")
                return False, (f"llamafile exited with code {code}. "
                               f"See {self._log_path()} for details.")
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url)
                if resp.status_code == 200:
                    ids = [m.get("id") for m in (resp.json().get("data") or [])
                           if m.get("id")]
                    if ids:
                        self._model_id = str(ids[0])
                    return True, ""
                last = f"HTTP {resp.status_code}"
            except Exception as exc:
                last = str(exc)
            await asyncio.sleep(1.5)
        return False, f"Timed out waiting for the local model to load ({last})."

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()
            except Exception as exc:
                log.debug("Local model stop failed: %s", exc)
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        if self._phase != "error":
            self._phase = "idle"
        log.info("Local model stopped")

    async def restart(self) -> tuple[bool, str]:
        await self.stop()
        return await self.start()

    async def ensure_running(self) -> str | None:
        """Start the server if needed. Returns None on success, else a message."""
        if self.is_running():
            self._last_used = time.monotonic()
            return None
        if not paths.installed():
            if self.is_declined():
                return ("The local model is not installed. Pick a provider in "
                        "Settings — Providers.")
            return ("The local model is not downloaded yet. Approve the download in "
                    "Settings — Providers — Local AI.")
        async with self._async_lock():
            if self.is_running():
                self._last_used = time.monotonic()
                return None
            ok, problem = await self.start()
            if not ok:
                return problem
        self._last_used = time.monotonic()
        return None

    async def remove(self) -> dict:
        await self.stop()
        freed = paths.installed_bytes()
        for path in (paths.llamafile_exe(), paths.gguf_path(),
                     paths.gguf_path().with_suffix(paths.gguf_path().suffix + ".part")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._model_id = None
        config.set("local_llm", "enabled", value=False)
        self._broadcast("local.llmStatus", self.status())
        return {"ok": True, "freed_mb": int(freed / (1024 * 1024))}

    async def watchdog_loop(self) -> None:
        """Stop the server after ``idle_unload_min`` of no use to free RAM."""
        while True:
            await asyncio.sleep(IDLE_TICK_S)
            try:
                minutes = float(self._cfg("idle_unload_min", default=15) or 0)
                if minutes <= 0 or not self.is_running():
                    continue
                idle = time.monotonic() - self._last_used
                if idle >= minutes * 60:
                    log.info("Unloading local model after %.0f min idle", idle / 60)
                    await self.stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Local model watchdog error: %s", exc)

    async def start_if_configured(self) -> None:
        """Boot-time: honour an approved download, or preload a warm server."""
        if paths.installed():
            if bool(self._cfg("enabled", default=False)):
                ok, problem = await self.start()
                if not ok:
                    log.warning("Local model preload failed: %s", problem)
            return
        if bool(self._cfg("download_approved", default=False)) \
                and not self.is_declined():
            log.info("Local model download approved earlier — starting download")
            await self.install()
        elif self.should_ask():
            self.ask()

    # ---- optional Hugging Face (Local) dependencies --------------------------

    async def install_hf_deps(self) -> dict:
        """pip install torch + transformers for the HF (Local) provider."""
        if self._hf_deps_installing:
            return {"ok": False, "error": "Already installing."}
        self._hf_deps_installing = True
        self._hf_deps_cache = None
        packages = ["torch", "transformers", "accelerate"]
        self._broadcast("local.hfProgress",
                        {"phase": "installing", "detail": " ".join(packages)})
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-s", "-m", "pip", "install", "--upgrade", *packages,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=3600)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("pip install timed out after 60 minutes")
            if proc.returncode != 0:
                tail = (out or b"").decode("utf-8", "replace")[-400:]
                raise RuntimeError(f"pip exited with {proc.returncode}: {tail}")
        except Exception as exc:
            self._hf_deps_installing = False
            self._broadcast("local.hfProgress",
                            {"phase": "failed", "detail": str(exc)})
            return {"ok": False, "error": str(exc)}
        self._hf_deps_installing = False
        self._hf_deps_cache = None  # re-probe on next status call
        self._broadcast("local.hfProgress", {"phase": "done", "detail": "installed"})
        log.info("Hugging Face (Local) dependencies installed")
        return {"ok": True}


local_llm = LocalLlmManager()
