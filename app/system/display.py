"""
Monitors (read-only): each active display's resolution and refresh rate, and the
highest refresh rate it offers at that resolution (EnumDisplaySettings), with the
monitor's model name from its EDID (WMI WmiMonitorID). A monitor left at 60 Hz when
it can do 144 or 165 is the most common free upgrade there is; the game tracker also
uses the refresh rate to recognize V-Sync.
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from ctypes import wintypes

from app.config import IS_WINDOWS

logger = logging.getLogger("optimind.display")

_ATTACHED_TO_DESKTOP, _PRIMARY = 0x1, 0x4
_ENUM_CURRENT_SETTINGS = -1
_CACHE_SECONDS = 30.0
_lock = threading.Lock()
_cache: tuple[float, list[dict]] | None = None


class _DevMode(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


class _DisplayDevice(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


def _monitor_names() -> dict[str, str]:
    """EDID product code (e.g. "SAM7059") -> model name (e.g. "LC34G55T"), from WmiMonitorID."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    service = win32com.client.Dispatch("WbemScripting.SWbemLocator").ConnectServer(".", r"root\wmi")
    names = {}
    for monitor in service.ExecQuery("SELECT InstanceName, UserFriendlyName FROM WmiMonitorID"):
        code = str(monitor.InstanceName or "").split("\\")[1:2]
        name = "".join(chr(c) for c in (monitor.UserFriendlyName or ()) if c).strip()
        if code and name:
            names[code[0].upper()] = name
    return names


def _read() -> list[dict]:
    user32 = ctypes.windll.user32
    try:
        names = _monitor_names()
    except Exception:
        names = {}
    found = []
    i = 0
    while True:
        adapter = _DisplayDevice(cb=ctypes.sizeof(_DisplayDevice))
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(adapter), 0):
            break
        i += 1
        if not adapter.StateFlags & _ATTACHED_TO_DESKTOP:
            continue
        current = _DevMode(dmSize=ctypes.sizeof(_DevMode))
        if not user32.EnumDisplaySettingsW(adapter.DeviceName, _ENUM_CURRENT_SETTINGS, ctypes.byref(current)):
            continue
        rates, best_any, mode = set(), 0, 0
        while True:
            dm = _DevMode(dmSize=ctypes.sizeof(_DevMode))
            if not user32.EnumDisplaySettingsW(adapter.DeviceName, mode, ctypes.byref(dm)):
                break
            mode += 1
            best_any = max(best_any, dm.dmDisplayFrequency)
            if (dm.dmPelsWidth, dm.dmPelsHeight) == (current.dmPelsWidth, current.dmPelsHeight):
                rates.add(dm.dmDisplayFrequency)
        monitor = _DisplayDevice(cb=ctypes.sizeof(_DisplayDevice))
        user32.EnumDisplayDevicesW(adapter.DeviceName, 0, ctypes.byref(monitor), 0)
        code = re.search(r"MONITOR\\([A-Z0-9]+)\\", monitor.DeviceID or "", re.IGNORECASE)
        found.append({
            "device": adapter.DeviceName, "adapter": adapter.DeviceString,
            "monitor": names.get(code.group(1).upper()) if code else None,
            "width": current.dmPelsWidth, "height": current.dmPelsHeight,
            "hz": current.dmDisplayFrequency, "max_hz": max(rates, default=current.dmDisplayFrequency),
            "max_hz_any": best_any, "primary": bool(adapter.StateFlags & _PRIMARY),
        })
    return found


def displays() -> list[dict]:
    """Every active display, primary first (kept 30 s)."""
    global _cache
    if not IS_WINDOWS:
        return []
    with _lock:
        if _cache and time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
        try:
            found = sorted(_read(), key=lambda d: not d["primary"])
        except Exception:
            logger.debug("Couldn't read the displays", exc_info=True)
            found = []
        _cache = (time.monotonic(), found)
        return found


def refresh_hz() -> float | None:
    """The primary display's refresh rate right now (where games usually run)."""
    shown = displays()
    return float(shown[0]["hz"]) if shown and shown[0]["hz"] > 1 else None
