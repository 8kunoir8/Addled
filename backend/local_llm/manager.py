"""
llamafile-backed local model runtime.

Owns three things:
  1. the "ask before downloading" state,
  2. the resumable download of the runtime + GGUF weights,
  3. the server process lifecycle (lazy start, readiness poll, idle unload).

The heavy lifting for downloads lives in ``backend.local_models``.

NOTE on GPU flags: llamafile 0.10.x accepts llama.cpp flags, so layer offload is
``-ngl``. ``gpu="auto"`` offloads only when there is actually free VRAM to hold
the layers (see ``_has_gpu``) and otherwise runs on CPU. Run
``llamafile.exe --help`` after the first download to confirm the flags for your
build, and use ``local_llm.extra_args`` to override.

NOTE on memory: three flags decide the resident footprint, and all three are
set here rather than left to llamafile's defaults.

* ``-c`` (``ctx``) is the prompt context **per slot**, not for the whole server.
* ``-np`` (``slots``) is how many slots that context is multiplied by. The
  default was "auto", which picked **4** — so a configured ``ctx=8192`` became
  32768 tokens of KV cache (4608 MiB instead of 1152 MiB). One slot is correct
  for this app: ``swarm.Orchestrator._single_generation_provider`` already
  forces local-model flows to run one at a time, so extra slots only split
  memory and the prompt cache, and they caused cache evictions in practice.
* ``-ctk``/``-ctv`` (``kv_type``) halve the cache again if set to ``q8_0``.

Measured on an RTX 4060 with Qwen3-8B-Q4_K_M: the 4-slot default held 6162 MiB
of committed RAM and 5815 MiB of VRAM. With ``-np 1`` the KV cache is 1152 MiB
(f16) or 576 MiB (q8_0), which keeps system RAM well inside an 8 GiB budget even
when the weights are mapped by a CPU-only run.
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
# Minimum free VRAM before "gpu: auto" will ask for any layer offload. Below
# this, llama.cpp ends up paging weights through system RAM instead, which
# costs the memory the local model is meant to bound.
GPU_MIN_FREE_MB = 1024
# Slots to run with when the user has not chosen. One is right because the app
# serves the local model one request at a time (see _slot_args).
DEFAULT_SLOTS = 1
# What llama.cpp uses when neither -ctk nor -ctv is given.
KV_DEFAULT = "f16"


def default_slots() -> int:
    """The slot count `local_llm.slots` falls back to.

    Exposed so a test can assert the shipped default without restating a
    literal, and so the value has one definition.
    """
    return DEFAULT_SLOTS


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
        self._vram_free_mb: int | None = None
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

    def _free_vram_mb(self) -> int | None:
        """Free VRAM in MiB, or None when it cannot be determined.

        Only meaningful on Windows with an NVIDIA driver. Any failure returns
        None rather than 0 so the caller can tell "no GPU" from "could not ask".
        """
        if shutil.which("nvidia-smi") is None:
            return None
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, creationflags=CREATE_NO_WINDOW,
            )
            if out.returncode != 0:
                return None
            first = (out.stdout or "").strip().splitlines()
            if not first:
                return None
            return int(float(first[0].strip()))
        except Exception as e:
            log.debug("nvidia-smi query failed: %s", e)
            return None

    def _has_gpu(self) -> bool:
        """Whether offloading layers is worth attempting.

        This used to be `shutil.which("nvidia-smi") is not None` — the binary
        existing on PATH was treated as a usable GPU. It cannot see how much
        VRAM is actually free, so a machine with an NVIDIA driver and a nearly
        full card still asked llama.cpp to offload every layer. Worse than
        useless: when the offload cannot fit, the driver falls back to paging
        the weights through system RAM, which is exactly the memory this path
        is supposed to bound.

        Now the free VRAM is the deciding factor. When it cannot be read, fall
        back to the old presence check, because a CPU-only fallback that is
        wrong costs speed while an offload that is wrong costs RAM.
        """
        if not self._gpu_checked:
            self._gpu_checked = True
            free = self._free_vram_mb()
            if free is None:
                self._has_nvidia = shutil.which("nvidia-smi") is not None
                self._vram_free_mb = None
            else:
                self._vram_free_mb = free
                # Enough for the smallest useful offload of an 8B Q4 model.
                # A partial offload still helps, so this is a floor for
                # "worth asking at all", not a fit-for-the-whole-model test.
                self._has_nvidia = free >= GPU_MIN_FREE_MB
                log.info("GPU auto-detect: %s MiB VRAM free -> %s", free,
                         "offload" if self._has_nvidia else "CPU only")
        return self._has_nvidia

    def _reap_orphans(self) -> int:
        """Kill leftover llamafile processes started from our own runtime path.

        A forced backend kill (app update, Task Manager) can leave the server
        orphaned holding our port, which would push the next launch to 8091, ...
        Only processes whose image path is exactly our bundled runtime are hit.
        """
        if os.name != "nt" or not paths.runtime_exists():
            return 0
        try:
            import win32api
            import win32con
            import win32process
        except Exception:
            return 0
        target = str(paths.llamafile_exe()).lower()
        killed = 0
        for pid in win32process.EnumProcesses():
            if pid <= 0:
                continue
            handle = None
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_TERMINATE,
                    False, pid)
                image = win32process.GetModuleFileNameEx(handle, 0)
                if image and image.lower() == target:
                    win32api.TerminateProcess(handle, 0)
                    killed += 1
            except Exception:
                continue
            finally:
                if handle is not None:
                    try:
                        win32api.CloseHandle(handle)
                    except Exception:
                        pass
        if killed:
            log.info("Reclaimed %s orphaned llamafile process(es)", killed)
        return killed

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

    # ---- when may the server run? -------------------------------------------
    #
    # Policy: the local model is never started eagerly. It runs only when it is
    # the provider Addled would actually use ("autostart", on by default), or
    # when the user explicitly keeps it warm with the "enabled" toggle. On top
    # of that, a request routed to `local` still starts it on demand.

    def is_chosen(self) -> bool:
        """True when `local` is the provider Addled would use right now."""
        try:
            return config.active_provider == "local"
        except Exception:
            return False

    def keep_running(self) -> bool:
        """Explicit 'keep it warm' toggle, independent of the chosen provider."""
        return bool(self._cfg("enabled", default=False))

    def autostart_on_select(self) -> bool:
        """Start the server when `local` becomes the selected provider."""
        return bool(self._cfg("autostart", default=True))

    def should_run(self) -> bool:
        if self.keep_running():
            return True
        return self.autostart_on_select() and self.is_chosen()

    def run_reason(self) -> str:
        if self.keep_running():
            return "keep-running"
        if self.autostart_on_select() and self.is_chosen():
            return "selected provider"
        return ""

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
            # Start policy (so the UI can explain why it is or is not running)
            "chosen": self.is_chosen(),
            "keep_running": self.keep_running(),
            "autostart": self.autostart_on_select(),
            "should_run": self.should_run(),
            "run_reason": self.run_reason(),
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
            config.set("local_llm", "declined", value=False)
            started = False
            if self.should_run():
                started, _ = await self.start()
            return {"ok": True, "already_installed": True, "started": started}

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

        config.set("local_llm", "declined", value=False)
        config.set("local_llm", "download_approved", value=True)
        self._phase = "idle"
        self._progress = 100.0
        self._detail = "installed"
        self._broadcast("local.llmProgress",
                        {"phase": "done", "pct": 100.0, "detail": "installed"})
        # Only spin the server up if the user is actually using the local model.
        started = False
        if self.should_run():
            started, problem = await self.start()
            if not started:
                log.warning("Local model installed but failed to start: %s", problem)
        self._broadcast("local.llmStatus", self.status())
        log.info("Local model installed (%s MB)%s", self.status()["installed_mb"],
                 " and started" if started else " - idle until selected")
        return {"ok": True, "started": started}

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

    def _slot_args(self) -> list[str]:
        """`-np`: how many server slots to split the context across.

        llamafile defaults to "auto", which chose 4 on this machine. Because
        `-c` is applied PER SLOT, that silently multiplied both the context and
        the KV cache by 4 (4 x 8192 tokens = 4608 MiB instead of 1152 MiB).
        The app never issues a second concurrent request to the local model, so
        the extra slots only split memory and the prompt cache.
        """
        slots = int(self._cfg("slots", default=default_slots()) or 0)
        if slots <= 0:
            return []                      # let llamafile choose
        return ["-np", str(slots)]

    def _kv_args(self) -> list[str]:
        """`-ctk`/`-ctv`: KV cache precision.

        f16 is the llama.cpp default; q8_0 halves the cache. Left unset unless
        configured, so the build's own default always applies otherwise.
        """
        kv = str(self._cfg("kv_type", default="") or "").strip().lower()
        if not kv:
            return []
        return ["-ctk", kv, "-ctv", kv]

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
        args += self._slot_args()
        args += self._gpu_args()
        args += self._kv_args()
        args += [str(a) for a in (self._cfg("extra_args", default=[]) or [])]
        return args

    async def _pick_port(self, preferred: int) -> int:
        """Prefer the configured port, waiting briefly for a dying orphan.

        TerminateProcess is asynchronous: the socket can stay bound for a moment
        after the reaper kills a leftover server. Without this wait the next
        launch would silently migrate 8090 -> 8091 -> ... on every restart.
        """
        port = preferred
        for attempt in range(8):            # up to ~4 s
            port = paths.resolve_free_port(preferred, LOOPBACK)
            if port == preferred:
                if attempt:
                    log.info("Reclaimed configured port %s", preferred)
                return preferred
            await asyncio.sleep(0.5)
        log.warning("Port %s stayed busy — using %s instead", preferred, port)
        return port

    async def start(self) -> tuple[bool, str]:
        if self.is_running():
            return True, ""
        if not paths.installed():
            return False, ("Local model is not downloaded yet. Approve the download "
                           "in Settings — Providers — Local AI.")
        paths.ensure_dirs()
        # Free our port from a previous forced shutdown before picking one.
        self._reap_orphans()
        preferred = int(self._cfg("port", default=8090) or 8090)
        self._port = await self._pick_port(preferred)
        if self._port != preferred:
            log.info("Port %s busy — local model will use %s", preferred, self._port)

        args = self._launch_args()
        log.info("Starting local model: %s", " ".join(args))
        # Stamp activity before spawning so the idle watchdog cannot unload the
        # server while it is still loading the weights.
        self._last_used = time.monotonic()
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
            if self._proc is None:
                return False, ("The local model was stopped before it finished "
                               "starting.")
            if self._proc.returncode is not None:
                return False, (f"llamafile exited with code {self._proc.returncode}. "
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
        """Stop the server after ``idle_unload_min`` of no use to free RAM.

        Also reclaims an orphaned server. The normal path is that this process
        owns the child and ``stop()`` ends it, but that only works if this
        process is alive to do it. If the app is closed with a hard terminate —
        which is what Electron's ``before-quit`` does — the model server
        survives, holding several gigabytes for as long as the machine is up.
        ``_reap_orphans()`` handled that at the next startup, so a copy could
        sit resident for days until Addled was next opened. Running the same
        sweep on the idle tick closes that window whenever a backend *is*
        running, and is a no-op when there is nothing to reclaim.
        """
        while True:
            await asyncio.sleep(IDLE_TICK_S)
            try:
                # Reclaim a server nobody owns — but ONLY when nothing is
                # serving on our port.
                #
                # `is_running()` is not enough on its own, and trusting it here
                # killed a healthy model mid-request: the Code page's planner
                # reaches the model over HTTP through the provider, NOT through
                # this manager, so `self._proc` stays None while a real server is
                # answering. The sweep then matched that server by image path and
                # terminated it — observed as "Reclaimed 1 orphaned llamafile
                # process(es)" moments after two successful tool calls, which is
                # exactly the failure this guard was meant to prevent.
                #
                # A port that answers is proof of a live server, whoever started
                # it, so that is the condition to respect.
                if not self.is_running() and not self._port_serving():
                    await asyncio.to_thread(self._reap_orphans)

                minutes = float(self._cfg("idle_unload_min", default=15) or 0)
                # Never unload while a start or download is still in flight, and
                # never while the local model is the chosen provider or has been
                # explicitly kept warm.
                if (minutes <= 0 or self._phase != "running"
                        or self._downloading or self.should_run()):
                    continue
                idle = time.monotonic() - self._last_used
                if idle >= minutes * 60:
                    log.info("Unloading local model after %.0f min idle", idle / 60)
                    await self.stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Local model watchdog error: %s", exc)

    def _port_serving(self, timeout: float = 1.0) -> bool:
        """Whether something is already answering on the local model's port.

        Used instead of trusting `self._proc`: a server started by an earlier
        run, or reached over HTTP without going through this manager, is still a
        server, and killing it mid-request is worse than leaving it.
        """
        import socket
        try:
            port = self.port()
        except Exception:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((LOOPBACK, int(port))) == 0

    async def boot(self) -> None:
        """Boot policy: never start eagerly — only when chosen or kept warm."""
        if paths.installed():
            # Clean up after a forced shutdown before deciding anything.
            await asyncio.to_thread(self._reap_orphans)
            if self.should_run():
                ok, problem = await self.start()
                if not ok:
                    log.warning("Local model start failed: %s", problem)
            else:
                log.info("Local model idle - it starts when 'Addled Local' is the "
                         "provider, or when 'Keep running' is enabled")
            return
        if bool(self._cfg("download_approved", default=False)) \
                and not self.is_declined():
            log.info("Local model download approved earlier — starting download")
            await self.install()
        elif self.should_ask():
            self.ask()

    async def apply_policy(self) -> dict:
        """Make the running state match the policy (after a settings change)."""
        if self.should_run() and paths.installed():
            if not self.is_running():
                ok, problem = await self.start()
                if not ok:
                    return {"ok": False, "error": problem}
                return {"ok": True, "started": True}
        elif self.is_running() and not self.should_run():
            await self.stop()
            return {"ok": True, "stopped": True}
        return {"ok": True}

    async def set_option(self, key: str, value) -> dict:
        """Toggle `enabled` (keep running) or `autostart` (start when selected)."""
        if key not in ("enabled", "autostart"):
            return {"ok": False, "error": f"unknown option: {key}"}
        config.set("local_llm", key, value=bool(value))
        result = await self.apply_policy()
        self._broadcast("local.llmStatus", self.status())
        result["status"] = self.status()
        return result

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
