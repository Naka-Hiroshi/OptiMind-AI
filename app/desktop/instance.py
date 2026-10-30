"""
One OptiMind at a time.

The agent holds a named mutex while it runs. Starting OptiMind again (Start
menu, desktop icon, a second double-click) then starts no second agent and
shows no Windows prompt: it asks the running one, through the local API, to
open its window, and exits. The installer closes OptiMind the same way
before an update or uninstall (`OptiMind.exe --quit`), after checking the
same mutex.
"""
from __future__ import annotations

import ctypes
import time
import urllib.error
import urllib.request
from ctypes import wintypes

from app.config import IS_WINDOWS, settings

MUTEX_NAME = "OptiMindAI.Agent"  # per signed-in session; also the installer's AppMutex
_SYNCHRONIZE = 0x00100000
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5  # the mutex of an OptiMind running as administrator
_ASFW_ANY = -1


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def claim(wait_seconds: float = 0.0):
    """Takes the mutex: returns a handle to keep for as long as OptiMind runs, or None when
    another OptiMind holds it (after waiting up to `wait_seconds` for that one to quit)."""
    if not IS_WINDOWS:
        return True  # nothing to coordinate with
    kernel32 = _kernel32()
    deadline = time.monotonic() + wait_seconds
    while True:
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        error = ctypes.get_last_error()
        if handle and error != _ERROR_ALREADY_EXISTS:
            return handle
        if handle:
            kernel32.CloseHandle(handle)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def is_running() -> bool:
    """Is an OptiMind agent running in this Windows session?"""
    if not IS_WINDOWS:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == _ERROR_ACCESS_DENIED


def ask(action: str, timeout: float = 5.0) -> bool:
    """Asks the running OptiMind to open its window ("window") or to quit ("quit")."""
    if action == "window" and IS_WINDOWS:
        ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)  # so its window can come to the front
    request = urllib.request.Request(f"http://127.0.0.1:{settings.optimind_api_port}/api/app/{action}",
                                     data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def quit_running(timeout: float = 20.0) -> bool:
    """Asks the running OptiMind to quit and waits for it. True when none is running anymore."""
    if not is_running():
        return True
    ask("quit")
    deadline = time.monotonic() + timeout
    while is_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    return not is_running()
