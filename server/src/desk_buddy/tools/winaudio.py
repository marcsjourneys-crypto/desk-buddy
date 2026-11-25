"""Small comtypes bindings for Windows Core Audio's per-app sessions (the volume mixer).

Only what ducking other apps needs:

- IMMDeviceEnumerator -> the default render endpoint (IMMDevice);
- IMMDevice.Activate -> IAudioSessionManager2 -> IAudioSessionEnumerator;
- each session's IAudioSessionControl2 (state, session identifier, process ID) and its
  ISimpleAudioVolume (the app's level in the mixer).

The vtables follow the Windows SDK headers (mmdeviceapi.h, audiopolicy.h, audioclient.h;
checked against the mingw-w64 copies): methods are listed in header order, including the
ones never called, because a COM method is found by its slot.

COM is used only on a thread made by com_worker() (COM initialised there), and every COM
pointer stays on that thread: SessionBackend's methods must be called there. comtypes is
imported lazily, so importing this module never initialises COM on the caller.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

E_RENDER = 0  # EDataFlow eRender
E_MULTIMEDIA = 1  # ERole eMultimedia
SESSION_INACTIVE = 0  # AudioSessionState: AudioSessionStateInactive
SESSION_ACTIVE = 1  # AudioSessionStateActive: playing right now
SESSION_EXPIRED = 2  # AudioSessionStateExpired
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class Session:
    """One app's audio session. get() and set() read and set its mixer level (0.0-1.0);
    they must run on the COM thread that listed it."""

    id: str  # the session identifier: stable across the app's sessions (and restarts)
    pid: int
    state: int  # SESSION_INACTIVE or SESSION_ACTIVE
    get: Callable[[], float]
    set: Callable[[float], None]


def com_worker(name: str) -> ThreadPoolExecutor:
    """One worker thread with COM initialised for these calls. Single-threaded apartment,
    as comtypes itself initialises a thread that imports it first; every pointer is made,
    used and released on this one thread, so that's all it needs."""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix=name, initializer=_com_init)


def _com_init() -> None:
    import comtypes

    comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)


class SessionBackend:
    """The real backend: the audio sessions on the default playback device."""

    def sessions(self) -> list[Session]:
        """Every live session, each with a level it can read and set. Call on a COM
        worker (com_worker). A session that can't be read is left out."""
        import comtypes

        api = _api()
        enumerator = comtypes.CoCreateInstance(
            api.CLSID_MMDeviceEnumerator,
            interface=api.IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        device = enumerator.GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA)
        unknown = device.Activate(
            ctypes.byref(api.IAudioSessionManager2._iid_), comtypes.CLSCTX_ALL, None
        )
        manager = unknown.QueryInterface(api.IAudioSessionManager2)
        found = manager.GetSessionEnumerator()
        sessions = []
        for i in range(found.GetCount()):
            try:
                control = found.GetSession(i).QueryInterface(api.IAudioSessionControl2)
                state = int(control.GetState())
                if state == SESSION_EXPIRED:
                    continue
                session_id = _take_string(control.GetSessionIdentifier())
                pid = int(control.GetProcessId())
                volume = control.QueryInterface(api.ISimpleAudioVolume)
            except Exception as exc:  # e.g. a session that ended while listing
                log.debug("Skipped an audio session: %s", exc)
                continue
            sessions.append(Session(session_id, pid, state, *_level_functions(volume)))
        return sessions


def _level_functions(volume: Any) -> tuple[Callable[[], float], Callable[[float], None]]:
    def get() -> float:
        return float(volume.GetMasterVolume())

    def set_(level: float) -> None:
        volume.SetMasterVolume(max(0.0, min(1.0, float(level))), None)  # no event context

    return get, set_


def _take_string(address: Any) -> str:
    """A string COM allocated (CoTaskMemAlloc), read and then freed."""
    if not address:
        return ""
    try:
        return ctypes.wstring_at(address)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(address))  # type: ignore[attr-defined]


def process_name(pid: int) -> str | None:
    """The file name of a process's program ("spotify.exe", lowercase), or None if it
    can't be read (it ended, or it's protected)."""
    if pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return os.path.basename(buffer.value).lower() or None
    finally:
        kernel32.CloseHandle(handle)


@functools.cache
def _api() -> Any:
    """The interface definitions (made on first use: comtypes is imported only then)."""
    from ctypes import POINTER, c_float, c_int, c_void_p
    from types import SimpleNamespace

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    def method(name: str, *params: tuple[list[str], Any, str]) -> Any:
        return COMMETHOD([], HRESULT, name, *params)

    def unused(name: str, *argtypes: Any) -> Any:
        """A slot we never call: listed only so the later methods land in their slots."""
        return COMMETHOD([], HRESULT, name, *(([], t, f"a{i}") for i, t in enumerate(argtypes)))

    class ISimpleAudioVolume(IUnknown):  # audioclient.h
        _iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
        _methods_ = [
            method(
                "SetMasterVolume", (["in"], c_float, "fLevel"), (["in"], c_void_p, "EventContext")
            ),
            method("GetMasterVolume", (["out"], POINTER(c_float), "pfLevel")),
            unused("SetMute", wintypes.BOOL, c_void_p),
            unused("GetMute", POINTER(wintypes.BOOL)),
        ]

    class IAudioSessionControl(IUnknown):  # audiopolicy.h
        _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
        _methods_ = [
            method("GetState", (["out"], POINTER(c_int), "pRetVal")),
            unused("GetDisplayName", c_void_p),
            unused("SetDisplayName", c_void_p, c_void_p),
            unused("GetIconPath", c_void_p),
            unused("SetIconPath", c_void_p, c_void_p),
            unused("GetGroupingParam", c_void_p),
            unused("SetGroupingParam", c_void_p, c_void_p),
            unused("RegisterAudioSessionNotification", c_void_p),
            unused("UnregisterAudioSessionNotification", c_void_p),
        ]

    class IAudioSessionControl2(IAudioSessionControl):
        _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
        _methods_ = [
            # LPWSTR* read as an address, so the string can be freed (_take_string).
            method("GetSessionIdentifier", (["out"], POINTER(c_void_p), "pRetVal")),
            unused("GetSessionInstanceIdentifier", c_void_p),
            method("GetProcessId", (["out"], POINTER(wintypes.DWORD), "pRetVal")),
            unused("IsSystemSoundsSession"),
            unused("SetDuckingPreference", wintypes.BOOL),
        ]

    class IAudioSessionEnumerator(IUnknown):
        _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
        _methods_ = [
            method("GetCount", (["out"], POINTER(c_int), "SessionCount")),
            method(
                "GetSession",
                (["in"], c_int, "SessionCount"),
                (["out"], POINTER(POINTER(IAudioSessionControl)), "Session"),
            ),
        ]

    class IAudioSessionManager(IUnknown):
        _iid_ = GUID("{BFA971F1-4D5E-40BB-935E-967039BFBEE4}")
        _methods_ = [
            unused("GetAudioSessionControl", c_void_p, wintypes.DWORD, c_void_p),
            unused("GetSimpleAudioVolume", c_void_p, wintypes.DWORD, c_void_p),
        ]

    class IAudioSessionManager2(IAudioSessionManager):
        _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        _methods_ = [
            method(
                "GetSessionEnumerator",
                (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum"),
            ),
            unused("RegisterSessionNotification", c_void_p),
            unused("UnregisterSessionNotification", c_void_p),
            unused("RegisterDuckNotification", c_void_p, c_void_p),
            unused("UnregisterDuckNotification", c_void_p),
        ]

    class IMMDevice(IUnknown):  # mmdeviceapi.h
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            method(
                "Activate",
                (["in"], POINTER(GUID), "iid"),
                (["in"], wintypes.DWORD, "dwClsCtx"),
                (["in"], c_void_p, "pActivationParams"),
                (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
            ),
            unused("OpenPropertyStore", wintypes.DWORD, c_void_p),
            unused("GetId", c_void_p),
            unused("GetState", c_void_p),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            unused("EnumAudioEndpoints", c_int, wintypes.DWORD, c_void_p),
            method(
                "GetDefaultAudioEndpoint",
                (["in"], c_int, "dataFlow"),
                (["in"], c_int, "role"),
                (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
            ),
            unused("GetDevice", c_void_p, c_void_p),
            unused("RegisterEndpointNotificationCallback", c_void_p),
            unused("UnregisterEndpointNotificationCallback", c_void_p),
        ]

    return SimpleNamespace(
        CLSID_MMDeviceEnumerator=GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        IMMDeviceEnumerator=IMMDeviceEnumerator,
        IMMDevice=IMMDevice,
        IAudioSessionManager2=IAudioSessionManager2,
        IAudioSessionEnumerator=IAudioSessionEnumerator,
        IAudioSessionControl2=IAudioSessionControl2,
        ISimpleAudioVolume=ISimpleAudioVolume,
    )
