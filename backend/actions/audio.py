"""System volume through the Windows Core Audio API, via pure ctypes.

Why this module exists rather than three lines of `subprocess`:

The previous implementation shelled out to `nircmd`, a third-party binary that
was never bundled. On a machine without it the call raised `[WinError 2]`, and
the result was `{"success": True}` unconditionally - so the tool reported a
volume change it had not made, on every machine, always. Measured live
2026-10-14: the user asked for 30% and the tool claimed success while the level
never moved. A later attempt using `SendMessage`/`VK_VOLUME_UP` was no better:
PowerShell exited 0, the readback said 100%, and the value had not changed.

The lesson from both is the same, and it is why this is written the way it is:
**a volume setter must read the value back.** Success is not "the command
exited 0" - success is "the endpoint now reports the level we asked for". So
`set_volume` below performs the set and then RE-READS the endpoint, and reports
failure if the two disagree.

No dependency: `pycaw`/`comtypes` are not guaranteed present, so this uses
`ctypes` against the documented COM interfaces directly.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, HRESULT, WINFUNCTYPE, byref, c_float, c_void_p

log = logging.getLogger("addled.audio")

CLSCTX_ALL = 0x17
_eRender, _eMultimedia = 0, 1

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
_NULL_GUID = "{00000000-0000-0000-0000-000000000000}"

# vtable slots, counted from the first method after IUnknown's three.
#   IMMDeviceEnumerator: 3 EnumAudioEndpoints, 4 GetDefaultAudioEndpoint
#   IMMDevice:           3 Activate, 4 OpenPropertyStore
#   IAudioEndpointVolume:6 SetMasterVolumeLevel, 7 SetMasterVolumeLevelScalar,
#                        8 GetMasterVolumeLevel, 9 GetMasterVolumeLevelScalar
# Slot 7 and 9 were confirmed by readback on the live machine: setting slot 7 to
# 0.35 and reading slot 9 returned 0.35. Slot 5 looks like a setter by signature
# and is not one - it faults, which is how the wrong mapping was caught.
_SLOT_GET_DEFAULT_ENDPOINT = 4
_SLOT_ACTIVATE = 3
_SLOT_SET_SCALAR = 7
_SLOT_GET_SCALAR = 9


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(self))


def _slot(obj: c_void_p, index: int, *argtypes):
    """A callable for vtable entry `index` of the COM object at `obj`."""
    table = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    proto = WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
    return proto(table[index])


def _endpoint():
    """The IAudioEndpointVolume for the default render device, or raise."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    enum = c_void_p()
    hr = ole32.CoCreateInstance(
        byref(_GUID(_CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
        byref(_GUID(_IID_IMMDeviceEnumerator)), byref(enum))
    if hr != 0:
        raise OSError(f"CoCreateInstance failed (0x{hr & 0xffffffff:08x})")
    dev = c_void_p()
    _slot(enum, _SLOT_GET_DEFAULT_ENDPOINT, ctypes.c_int, ctypes.c_int,
          POINTER(c_void_p))(enum, _eRender, _eMultimedia, byref(dev))
    if not dev:
        raise OSError("no default audio output device")
    volume = c_void_p()
    _slot(dev, _SLOT_ACTIVATE, POINTER(_GUID), ctypes.c_ulong, c_void_p,
          POINTER(c_void_p))(dev, byref(_GUID(_IID_IAudioEndpointVolume)),
                             CLSCTX_ALL, None, byref(volume))
    if not volume:
        raise OSError("could not activate the audio endpoint volume")
    return volume


def get_volume() -> int | None:
    """The current master volume 0-100, or None when it cannot be read."""
    try:
        volume = _endpoint()
        out = c_float()
        hr = _slot(volume, _SLOT_GET_SCALAR, POINTER(c_float),
                   POINTER(_GUID))(volume, byref(out), byref(_GUID(_NULL_GUID)))
        if hr != 0:
            return None
        return int(round(out.value * 100))
    except Exception:  # noqa: BLE001
        log.debug("could not read the master volume", exc_info=True)
        return None


def set_volume(percent: int) -> dict:
    """Set the master volume and confirm it took.

    Returns the same shape the skills expect: `{"success": True, "level": n}`
    on a verified change, `{"success": False, "error": ...}` otherwise.

    The readback is not a nicety. It is the whole reason this module exists -
    two earlier implementations reported success without changing anything, and
    only a readback can tell those apart from a working one.
    """
    try:
        target = max(0, min(100, int(percent)))
    except (TypeError, ValueError):
        return {"success": False, "error": f"not a volume level: {percent!r}"}
    try:
        volume = _endpoint()
        hr = _slot(volume, _SLOT_SET_SCALAR, c_float,
                   POINTER(_GUID))(volume, c_float(target / 100.0),
                                   byref(_GUID(_NULL_GUID)))
        if hr != 0:
            return {"success": False,
                    "error": f"the audio endpoint refused the change "
                             f"(0x{hr & 0xffffffff:08x})"}
        # 0 is legitimately 0 and the round-trip is exact for every other level,
        # so one percent of tolerance covers the float conversion and nothing
        # else. A wider window would hide a genuine failure.
        actual = get_volume()
        if actual is None:
            return {"success": False,
                    "error": ("the volume was set but could not be read back to "
                              "confirm it")}
        if abs(actual - target) > 1:
            return {"success": False,
                    "error": (f"asked for {target}% but the endpoint reports "
                              f"{actual}%")}
        return {"success": True, "level": actual}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}
