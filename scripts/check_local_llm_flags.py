"""Local model launch flags: the ones that decide resident RAM.

Three flags set the footprint of the llamafile server, and all three used to be
left to llamafile's own defaults:

* ``-c`` is the context **per slot**.
* ``-np`` is how many slots that context is multiplied by. The default is
  "auto", which picked **4** here, so a configured ``ctx=8192`` became 32768
  tokens of KV cache -- 4608 MiB instead of 1152 MiB. Confirmed against a
  running server: ``GET /slots`` returned four entries, each ``n_ctx=8192``.
* ``-ctk``/``-ctv`` halve the cache again when set to ``q8_0``.

Nothing in the app issues a concurrent request to the local model --
``swarm.Orchestrator._single_generation_provider`` forces local flows to run
one at a time -- so the extra slots only split memory and the prompt cache.

The GPU check is here for the same reason. ``_has_gpu`` used to be
``shutil.which("nvidia-smi") is not None``: the binary *existing* counted as a
usable GPU, so ``-ngl 999`` (offload every layer) was passed even with ~500 MiB
free. When that offload cannot fit, the driver pages weights through system RAM,
which is the memory this suite exists to bound.
"""

from __future__ import annotations

import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.config import config          # noqa: E402
from backend.local_llm import manager as mgr  # noqa: E402

fails: list[str] = []

def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {label}" +
          (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        fails.append(label)


def _arg(args: list[str], flag: str):
    """Value following `flag`, or None when absent."""
    return args[args.index(flag) + 1] if flag in args else None


def _with(**overrides):
    """Build argv with config temporarily overridden, then restore."""
    saved = {}
    try:
        for key, value in overrides.items():
            saved[key] = config.get("local_llm", key, default=None)
            config.set("local_llm", key, value=value)
        return mgr.local_llm._launch_args()
    finally:
        for key, value in saved.items():
            config.set("local_llm", key, value=value)


def slot_args_checks() -> None:
    args = _with(slots=1)
    check("a slot count is passed at all",
          "-np" in args,
          "without -np, llamafile's 'auto' default multiplies the KV cache")
    check("the default is a single slot",
          _arg(args, "-np") == "1", f"-np {_arg(args, '-np')}")
    check("the configured context is unchanged",
          _arg(args, "-c") == "8192", f"-c {_arg(args, '-c')}")

    check("one slot is enough for this app",
          mgr.default_slots() == 1,
          "swarm.Orchestrator._single_generation_provider runs local flows "
          "sequentially, so more slots only split memory")

    # 0 and negative must mean "let llamafile choose", which is what the old
    # behaviour was -- it has to stay reachable, not become an error.
    for raw in (0, -1):
        args = _with(slots=raw)
        check(f"slots={raw} leaves -np off (llamafile decides)",
              "-np" not in args, " ".join(args[-6:]))

    args = _with(slots=2)
    check("an explicit slot count is honoured",
          _arg(args, "-np") == "2", f"-np {_arg(args, '-np')}")


def kv_args_checks() -> None:
    args = _with(kv_type="")
    check("no KV flag by default (the build's own default applies)",
          "-ctk" not in args and "-ctv" not in args)
    check("f16 is what the build defaults to",
          mgr.KV_DEFAULT == "f16", mgr.KV_DEFAULT)

    args = _with(kv_type="q8_0")
    check("kv_type=q8_0 sets both K and V",
          _arg(args, "-ctk") == "q8_0" and _arg(args, "-ctv") == "q8_0",
          f"-ctk {_arg(args, '-ctk')} -ctv {_arg(args, '-ctv')}")

    args = _with(kv_type="Q8_0")
    check("kv_type is case-insensitive",
          _arg(args, "-ctk") == "q8_0", f"-ctk {_arg(args, '-ctk')}")

    args = _with(kv_type="   ")
    check("a blank kv_type is treated as unset", "-ctk" not in args)


def footprint_checks() -> None:
    """The arithmetic that keeps resident RAM inside the 8 GiB budget."""
    # Qwen3-8B: 36 layers, 8 KV heads (GQA), head dim 128. K and V, 2 bytes.
    per_token = 36 * 8 * 128 * 2 * 2
    mib = 1024 * 1024
    kv_one_slot = per_token * 8192 / mib
    kv_four_slots = per_token * 8192 * 4 / mib

    check("a single 8192-token slot is ~1152 MiB of KV cache",
          1100 < kv_one_slot < 1200, f"{kv_one_slot:.0f} MiB")
    check("the old four-slot default was 4x that",
          4500 < kv_four_slots < 4700, f"{kv_four_slots:.0f} MiB")
    # The point of the fix: the four-slot KV cache alone plus the weights would
    # not fit a CPU-mode run inside 8 GiB.
    weights = 4795
    check("CPU-mode default would have exceeded an 8 GiB budget",
          weights + kv_four_slots > 8192,
          f"weights {weights} + KV {kv_four_slots:.0f} = "
          f"{weights + kv_four_slots:.0f} MiB")
    check("CPU-mode single slot fits comfortably",
          weights + kv_one_slot < 8192,
          f"weights {weights} + KV {kv_one_slot:.0f} = "
          f"{weights + kv_one_slot:.0f} MiB")


def gpu_checks() -> None:
    """-ngl must follow free VRAM, not the presence of a driver."""
    check("the free-VRAM floor is defined",
          isinstance(mgr.GPU_MIN_FREE_MB, int) and mgr.GPU_MIN_FREE_MB > 0,
          f"{mgr.GPU_MIN_FREE_MB} MiB")

    free = mgr.local_llm._free_vram_mb()
    check("free VRAM is actually queried",
          free is None or isinstance(free, int),
          f"nvidia-smi reports {free} MiB free" if free is not None
          else "no NVIDIA GPU on this machine (None is the honest answer)")

    if free is not None:
        # The defect was: the binary exists, so offload everything. These two
        # directions pin the corrected relationship.
        check("with little free VRAM, auto does not offload",
              _arg(_with(gpu="auto"), "-ngl") == "0" if free < mgr.GPU_MIN_FREE_MB
              else True,
              f"{free} MiB free vs a {mgr.GPU_MIN_FREE_MB} MiB floor")

    check("gpu='cpu' forces -ngl 0",
          _arg(_with(gpu="cpu"), "-ngl") == "0")
    check("gpu='off' forces -ngl 0",
          _arg(_with(gpu="off"), "-ngl") == "0")

    args = _with(gpu="20")
    check("an explicit layer count is passed through",
          _arg(args, "-ngl") == "20", f"-ngl {_arg(args, '-ngl')}")

    # A failure to query must not be read as "no GPU": falling back to CPU
    # costs speed, whereas a wrong offload costs RAM.
    original = mgr.local_llm._free_vram_mb
    try:
        mgr.local_llm._free_vram_mb = lambda: None
        mgr.local_llm._gpu_checked = False
        check("an unreadable VRAM query falls back to auto-detect, not to CPU",
              mgr.local_llm._has_gpu() == (mgr.shutil.which("nvidia-smi") is not None),
              f"_has_gpu()={mgr.local_llm._has_gpu()}")
    finally:
        mgr.local_llm._free_vram_mb = original
        mgr.local_llm._gpu_checked = False


def override_checks() -> None:
    """extra_args must still be the last word, so a user can always override."""
    args = _with(extra_args=["-np", "4"])
    check("extra_args still lets a user override the slot count",
          args[-2:] == ["-np", "4"], " ".join(args[-4:]))
    _with(extra_args=[])
    check("the default slot count is restored after an override",
          _arg(_with(slots=1), "-np") == "1")


def orphan_checks() -> None:
    """A model server must never outlive the app.

    `pythonProcess.kill()` in electron/main.js is a hard terminate, so the
    backend never runs its own `local_llm.stop()`. A leftover llama server held
    5.8 GB of VRAM for hours with no window open to explain it, and the only
    cleanup was `_reap_orphans()` at the NEXT startup.
    """
    import inspect

    source = inspect.getsource(mgr.LocalLlmManager.watchdog_loop)
    check("the idle watchdog also sweeps for orphans",
          "_reap_orphans" in source,
          "without this, an orphan survives until Addled is next opened")

    # `is_running()` alone is NOT a safe guard, and the first version of this
    # check asserted only that the name appeared — which passed while the guard
    # was wrong. The Code page's planner reaches the model over HTTP through the
    # provider, so `self._proc` stays None while a real server answers; the sweep
    # then matched that server by image path and killed it mid-request. Observed
    # as "Reclaimed 1 orphaned llamafile process(es)" moments after two
    # successful tool calls.
    check("the sweep also checks the port is idle, not just self._proc",
          "_port_serving" in source and "not self._port_serving()" in source,
          "a server answering on our port is live even when we do not own the "
          "process — killing it breaks an in-flight request")
    check("_port_serving actually probes the port",
          hasattr(mgr.LocalLlmManager, "_port_serving"),
          "the guard needs to exist as a method")
    import inspect as _ins2
    probe = _ins2.getsource(mgr.LocalLlmManager._port_serving)
    check("the probe uses a socket connect",
          "connect_ex" in probe, "a guess is not a check")

    # The reaper must match its own runtime exactly, or it would kill an
    # unrelated llamafile the user runs from elsewhere.
    reap = inspect.getsource(mgr.LocalLlmManager._reap_orphans)
    check("the reaper matches on the exact image path",
          "image.lower() == target" in reap,
          "a name-only match would kill a user's own llamafile")
    check("the reaper targets our bundled runtime",
          "paths.llamafile_exe()" in reap)

    # main.js must clean up too, since the backend gets no chance to.
    import os as _os
    main_js = _os.path.join(ROOT, "electron", "main.js")
    if _os.path.exists(main_js):
        with open(main_js, encoding="utf-8", errors="replace") as handle:
            js = handle.read()
        check("electron kills the model server on quit",
              "killLlamafile" in js and "before-quit" in js,
              "the backend is hard-killed, so it cannot do this itself")
        check("electron matches the server by path, not name",
              "ExecutablePath" in js,
              "so an unrelated llamafile is left alone")
    else:
        check("electron main.js is present", False, main_js)


def main() -> int:
    print("-- launch flags that decide resident RAM --")
    slot_args_checks()
    print("-- KV cache precision --")
    kv_args_checks()
    print("-- footprint arithmetic --")
    footprint_checks()
    print("-- GPU offload follows free VRAM --")
    gpu_checks()
    print("-- user overrides still win --")
    override_checks()
    print("-- a model server never outlives the app --")
    orphan_checks()
    if fails:
        print(f"\nFAIL: {len(fails)} check(s) failed")
        return 1
    print("\nPASS: slot count, KV precision, footprint, VRAM-aware offload "
          "and orphan cleanup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
