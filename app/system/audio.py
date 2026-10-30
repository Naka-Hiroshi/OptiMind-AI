"""
Apps playing sound right now, read from Windows' Core Audio sessions, so
the agent never slows them down: Windows itself runs audible apps at its
highest Quality of Service (see Microsoft's "Quality of Service" docs),
and a voice chat or music player held back in the background would
stutter. An app counts while its audio session is active (a voice call
stays active even while nobody talks). Read-only.

Plain ctypes calls into the documented COM interfaces (IMMDeviceEnumerator,
IAudioSessionManager2, IAudioSessionControl2), so no extra package is
needed. Focus mode's thread refreshes the list every few seconds and keeps
its COM objects between reads, so a refresh only lists the sessions.
"""
from __future__ import annotations

import ctypes
import threading
import time
import uuid

from app.config import IS_WINDOWS

_STALE_SECONDS = 10.0  # older than this, the next reader refreshes it itself
_DEVICES_SECONDS = 60.0  # playback devices are listed again this often (a headset plugged in)
_SESSIONS_SECONDS = 15.0  # ...and their sessions this often (an app that just started playing)
_CLSCTX_ALL = 0x17
_E_RENDER = 0  # EDataFlow.eRender: playback devices
_DEVICE_STATE_ACTIVE = 0x1
_SESSION_ACTIVE = 1  # AudioSessionState.AudioSessionStateActive


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_CLSID_DEVICE_ENUMERATOR = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_DEVICE_ENUMERATOR = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_SESSION_MANAGER2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
_IID_SESSION_CONTROL2 = _guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
_PTR = ctypes.POINTER(ctypes.c_void_p)

_lock = threading.Lock()
_local = threading.local()  # the COM objects of the thread that reads
_audible: tuple[float, frozenset[int]] = (float("-inf"), frozenset())


_prototypes: dict[tuple, type] = {}


def _call(obj: ctypes.c_void_p, index: int, argtypes: tuple, *args) -> int:
    """Calls method `index` of a COM interface (its vtable slot). Raises OSError on a failed HRESULT."""
    proto = _prototypes.get(argtypes)
    if proto is None:
        proto = _prototypes[argtypes] = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    vtable = ctypes.cast(obj, ctypes.POINTER(_PTR)).contents
    return proto(vtable[index])(obj, *args)


_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _RELEASE(ctypes.cast(obj, ctypes.POINTER(_PTR)).contents[2])(obj)  # IUnknown::Release


def _session_managers() -> list[ctypes.c_void_p]:
    """One IAudioSessionManager2 per active playback device, kept by this thread."""
    now = time.monotonic()
    if getattr(_local, "managers", None) is not None and now - _local.listed < _DEVICES_SECONDS:
        return _local.managers
    _drop_managers()
    ole32 = ctypes.WinDLL("ole32")
    if not getattr(_local, "com", False):
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; harmless if this thread already has COM
        _local.com = True
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    enumerator = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_CLSID_DEVICE_ENUMERATOR), None, _CLSCTX_ALL,
                           ctypes.byref(_IID_DEVICE_ENUMERATOR), ctypes.byref(enumerator))
    managers: list[ctypes.c_void_p] = []
    try:
        devices = ctypes.c_void_p()
        _call(enumerator, 3, (ctypes.c_int, ctypes.c_ulong, _PTR),  # IMMDeviceEnumerator::EnumAudioEndpoints
              _E_RENDER, _DEVICE_STATE_ACTIVE, ctypes.byref(devices))
        try:
            count = ctypes.c_uint()
            _call(devices, 3, (ctypes.POINTER(ctypes.c_uint),), ctypes.byref(count))
            for i in range(count.value):
                device = ctypes.c_void_p()
                _call(devices, 4, (ctypes.c_uint, _PTR), i, ctypes.byref(device))
                try:
                    manager = ctypes.c_void_p()
                    _call(device, 3, (ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, _PTR),  # Activate
                          ctypes.byref(_IID_SESSION_MANAGER2), _CLSCTX_ALL, None, ctypes.byref(manager))
                    managers.append(manager)
                except OSError:
                    pass  # one device that can't say doesn't hide the others
                finally:
                    _release(device)
        finally:
            _release(devices)
    finally:
        _release(enumerator)
    _local.managers, _local.listed = managers, now
    return managers


def _drop_managers() -> None:
    _drop_sessions()
    for manager in getattr(_local, "managers", None) or []:
        _release(manager)
    _local.managers = None


def _list_sessions(manager: ctypes.c_void_p, out: list[tuple[ctypes.c_void_p, int]]) -> None:
    """Every session of one device that belongs to an app, with that app's PID."""
    sessions = ctypes.c_void_p()
    _call(manager, 5, (_PTR,), ctypes.byref(sessions))  # IAudioSessionManager2::GetSessionEnumerator
    try:
        count = ctypes.c_int()
        _call(sessions, 3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(count))
        for i in range(count.value):
            control = ctypes.c_void_p()
            _call(sessions, 4, (ctypes.c_int, _PTR), i, ctypes.byref(control))
            pid = ctypes.c_ulong()
            control2 = ctypes.c_void_p()
            try:
                _call(control, 0, (ctypes.POINTER(_GUID), _PTR), ctypes.byref(_IID_SESSION_CONTROL2),
                      ctypes.byref(control2))  # QueryInterface
                _call(control2, 14, (ctypes.POINTER(ctypes.c_ulong),), ctypes.byref(pid))  # GetProcessId
            except OSError:
                pass  # a session without a single owner process
            finally:
                _release(control2)
            if pid.value:
                out.append((control, pid.value))  # kept (and released when the sessions are listed again)
            else:
                _release(control)
    finally:
        _release(sessions)


def _sessions() -> list[tuple[ctypes.c_void_p, int]]:
    now = time.monotonic()
    if getattr(_local, "sessions", None) is not None and now - _local.sessions_listed < _SESSIONS_SECONDS:
        return _local.sessions
    _drop_sessions()
    sessions: list[tuple[ctypes.c_void_p, int]] = []
    for manager in _session_managers():
        try:
            _list_sessions(manager, sessions)
        except OSError:
            _local.listed = float("-inf")  # a device went away: list them again next time
    _local.sessions, _local.sessions_listed = sessions, now
    return sessions


def _drop_sessions() -> None:
    for control, _ in getattr(_local, "sessions", None) or []:
        _release(control)
    _local.sessions = None


def refresh() -> frozenset[int]:
    """Reads which audio sessions are active now, on the calling thread (which keeps its
    COM objects for next time). Focus mode's thread calls this every few seconds."""
    global _audible
    pids: set[int] = set()
    try:
        for control, pid in _sessions():
            state = ctypes.c_int()
            try:
                _call(control, 3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(state))  # GetState
            except OSError:
                _local.sessions_listed = float("-inf")
                continue
            if state.value == _SESSION_ACTIVE:
                pids.add(pid)
    except OSError:
        _drop_managers()
    result = frozenset(pids)
    _audible = (time.monotonic(), result)
    return result


def audible_pids() -> frozenset[int]:
    """PIDs of the processes with an active audio session on any playback device, as
    last read (read here if nobody has for a while). Empty when Windows can't say."""
    if not IS_WINDOWS:
        return frozenset()
    at, pids = _audible
    if time.monotonic() - at <= _STALE_SECONDS:
        return pids
    with _lock:
        return refresh()
