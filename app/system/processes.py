"""
The full process list for the dashboard's Processes tab, grouped like
Task Manager: one row per program (all of its processes added up), in
three sections — Apps (programs with a window you can switch to),
Background processes and Windows processes — with the program's real
name ("Visual Studio Code") next to its file name ("Code.exe").

Read-only, and built from the numbers the fast loop already measured,
so refreshing the tab costs almost nothing.
"""
from __future__ import annotations

import os
import time
from collections import Counter

import psutil

from app.agent import safety
from app.agent.optimizer import OTHER_ACCOUNT, blocked_kind
from app.config import IS_WINDOWS
from app.system import monitor, sysinfo

_WINDIR = os.environ.get("SystemRoot", r"C:\Windows").rstrip("\\").lower() + "\\"
_KERNEL_PROCESSES = {"system", "registry", "memory compression", "memcompression", "secure system"}
_SHARED_RUNTIMES = {"msedgewebview2.exe"}  # serves many apps at once: closing "it" isn't something you can do
_OWN_PID = os.getpid()
OWN_NAME = psutil.Process().name().lower()  # OptiMind's program (its window is one too)

_path_cache: dict[int, str | None] = {}
_windows_cache: tuple[float, dict[int, list[int]]] = (0.0, {})


def _exe_path(pid: int) -> str | None:
    if pid not in _path_cache:
        try:
            path = psutil.Process(pid).exe()
        except Exception:
            path = None  # e.g. a protected process
        # Kernel parts report a bare name ("Registry", "MemCompression"), not a file.
        _path_cache[pid] = path if path and (":\\" in path or path.startswith("\\\\")) else None
    return _path_cache[pid]


def _enum_app_windows() -> dict[int, list[int]]:
    """Processes owning a window you can Alt+Tab to — Task Manager's "Apps" — and those windows."""
    import ctypes
    from ctypes import wintypes

    user32, dwmapi = ctypes.WinDLL("user32"), ctypes.WinDLL("dwmapi")
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, enum_proc, wintypes.LPARAM]
    for fn in (user32.IsWindowVisible, user32.GetWindowTextLengthW):
        fn.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    gw_owner, gwl_exstyle, ws_ex_toolwindow, dwmwa_cloaked = 4, -20, 0x80, 14

    def pid_of(hwnd) -> int:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def class_of(hwnd) -> str:
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        return buf.value

    pids: dict[int, list[int]] = {}

    @enum_proc
    def on_window(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, gw_owner) or not user32.GetWindowTextLengthW(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, gwl_exstyle) & ws_ex_toolwindow:
            return True
        cloaked = wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(hwnd, dwmwa_cloaked, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:  # hidden: suspended Store apps, the input panel (and windows on other desktops)
            return True
        pid = pid_of(hwnd)
        if class_of(hwnd) == "ApplicationFrameWindow":
            # A Store app: the frame belongs to ApplicationFrameHost, the app itself owns the inner CoreWindow.
            inner: list[int] = []

            @enum_proc
            def on_child(child, _):
                if class_of(child) == "Windows.UI.Core.CoreWindow":
                    inner.append(pid_of(child))
                    return False
                return True

            user32.EnumChildWindows(hwnd, on_child, 0)
            pid = inner[0] if inner else pid
        pids.setdefault(pid, []).append(hwnd)  # (a Store app's frame: closing it closes the app)
        return True

    user32.EnumWindows(on_window, 0)
    return pids


def app_windows(fresh: bool = False) -> dict[int, list[int]]:
    """PIDs with a window you can switch to, and those windows (refreshed at most every
    2 seconds, unless `fresh`)."""
    global _windows_cache
    now = time.monotonic()
    if fresh or now - _windows_cache[0] >= 2.0:
        try:
            windows = _enum_app_windows() if IS_WINDOWS else {}
        except Exception:
            windows = {}
        _windows_cache = (now, windows)
    return _windows_cache[1]


def app_window_pids() -> set[int]:
    """PIDs with a window you can switch to (refreshed at most every 2 seconds)."""
    return set(app_windows())


def _category(key: str, pids: list[int], path: str | None, reason: str | None, window_pids: set[int]) -> str:
    if any(pid in window_pids for pid in pids):
        return "apps"
    if path:
        return "windows" if path.lower().startswith(_WINDIR) else "background"
    # No path (Windows won't say): kernel parts and services of other accounts are Windows'.
    return "windows" if key in _KERNEL_PROCESSES or reason == OTHER_ACCOUNT else "background"


def _title(name: str, path: str | None) -> str:
    bare = name[:-4] if name.lower().endswith(".exe") else name
    description = sysinfo.file_version_info(path).get("FileDescription", "")
    if description and description.lower() not in (name.lower(), bare.lower()):
        return description
    return bare


def biggest_apps(limit: int = 3, leave_out: set[int] | frozenset[int] = frozenset(), min_mb: float = 0.0) -> list[dict]:
    """The programs holding the most memory of their own (private bytes, all of their processes
    added up, at least `min_mb`), from the last fast-loop cycle: what closing would give back,
    also to the commit charge. Windows' own processes, OptiMind and the PIDs in `leave_out` (a
    game and its helpers) aren't listed.
    [{"name": "Code.exe", "title": "Visual Studio Code", "mb": 2180, "processes": 12}, ...]"""
    rows, _ = monitor.latest_processes()
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("private_mb") is not None and r["pid"] != _OWN_PID and r["pid"] not in leave_out:
            by_name.setdefault(r["name"].lower(), []).append(r)
    found: list[dict] = []
    # The biggest first, so only a few programs' paths are looked up
    for key, members in sorted(by_name.items(), key=lambda kv: -sum(r["private_mb"] for r in kv[1])):
        mb = sum(r["private_mb"] for r in members)
        if len(found) >= limit or mb < min_mb:
            break
        if key in _KERNEL_PROCESSES or key in _SHARED_RUNTIMES or key == OWN_NAME or safety.is_protected(key):
            continue
        path = next((p for p in map(_exe_path, (r["pid"] for r in members)) if p), None)
        if not path or path.lower().startswith(_WINDIR):
            continue  # Windows' own, or a process Windows won't name
        name = members[0]["name"]
        found.append({"name": name, "title": _title(name, path), "mb": round(mb), "processes": len(members)})
    return found


def app_groups(managed: dict[str, list[str]] | None = None) -> dict:
    """One entry per program with its processes added up, for the Processes tab."""
    rows, foreground_pid = monitor.latest_processes()
    live = {r["pid"] for r in rows}
    for pid in list(_path_cache):
        if pid not in live:
            _path_cache.pop(pid, None)
    window_pids = app_window_pids()
    gpu = sysinfo.gpu_by_pid()
    managed = managed or {}

    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(r["name"].lower(), []).append(r)

    groups = []
    for key, members in by_name.items():
        members.sort(key=lambda r: r["pid"])
        name = members[0]["name"]
        pids = [r["pid"] for r in members]
        path = next((p for p in map(_exe_path, pids) if p), None)
        reasons = [monitor.blocked_reason(pid, name) for pid in pids]
        # A reason only shows when it applies to every process of the program.
        reason = Counter(reasons).most_common(1)[0][0] if all(reasons) else None
        priorities = {r["priority"] for r in members}
        instances = [{
            "pid": r["pid"], "cpu": round(r["cpu"], 1), "memory_mb": round(r["mem_mb"], 1),
            "io_bps": round(r["io_bps"]), "gpu": gpu.get(r["pid"], 0.0), "priority": r["priority"],
        } for r in members]
        groups.append({
            "key": key,
            "name": name,
            "title": _title(name, path),
            "path": path,
            "category": _category(key, pids, path, reason, window_pids),
            "count": len(members),
            "cpu": round(sum(i["cpu"] for i in instances), 1),
            "memory_mb": round(sum(i["memory_mb"] for i in instances), 1),
            # its own memory (private bytes): what closing it gives back, also to the commit charge
            "private_mb": round(sum(r["private_mb"] for r in members), 1)
            if all(r.get("private_mb") is not None for r in members) else None,
            "io_bps": sum(i["io_bps"] for i in instances),
            "gpu": round(min(100.0, sum(i["gpu"] for i in instances)), 1),
            "priority": priorities.pop() if len(priorities) == 1 else "mixed",
            "is_foreground": foreground_pid in pids,
            "is_self": _OWN_PID in pids,
            "blocked_reason": reason,
            "blocked_kind": blocked_kind(reason),  # its label: "protected", "admin" or "service"
            "managed": managed.get(key, []),
            "instances": instances if len(instances) > 1 else [],
        })
    groups.sort(key=lambda g: g["title"].lower())
    return {
        "process_count": len(rows),
        "cores": psutil.cpu_count(logical=True) or 1,
        "groups": groups,
    }
