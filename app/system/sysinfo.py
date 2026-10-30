"""
Read-only machine details for the dashboard (Task Manager style):
hardware names, memory composition, GPU, drives, network adapters,
services and pending restarts.

Nothing here changes the system and none of it feeds the agent's
decisions — it only describes the PC. Windows performance counters
(PDH) are added by their English names, so they work on any Windows
display language. Slow parts are cached.
"""
from __future__ import annotations

import os
import platform
import re
import threading
import time
from typing import Callable

import psutil

from app.config import IS_WINDOWS
from app.system import memory

_MB = 1024 * 1024
_GPU_ENGINE_RE = re.compile(r"pid_(\d+)_luid_(\w+?)_phys_(\d+)_eng_(\d+)_engtype_(.*)")

_cache: dict[str, tuple[float, object]] = {}
_rates: dict[str, tuple[float, float]] = {}
_gpu_by_pid: dict[int, float] = {}  # last per-process GPU % (the busiest engine each process uses)


def _cached(key: str, ttl_seconds: float, compute: Callable[[], object]):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl_seconds:
        return hit[1]
    value = compute()
    _cache[key] = (now, value)
    return value


def _rate(key: str, total: float, now: float) -> float:
    """Units per second since the previous call (0 on the first call)."""
    prev = _rates.get(key)
    _rates[key] = (total, now)
    if prev is None or now <= prev[1]:
        return 0.0
    return max(0.0, (total - prev[0]) / (now - prev[1]))


def _reg_value(path: str, name: str, hive=None):
    import winreg

    try:
        with winreg.OpenKey(hive or winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


# ---------------------------------------------------------------- static info

def _gpu_names() -> list[str]:
    import winreg

    display_class = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    names: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, display_class) as key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if sub.isdigit():
                    desc = _reg_value(rf"{display_class}\{sub}", "DriverDesc")
                    if desc and desc not in names and "Basic Display" not in desc:
                        names.append(desc)
    except OSError:
        pass
    return names


def _static_info() -> dict:
    info = {
        "computer_name": platform.node(),
        "cpu_name": platform.processor(),
        "gpu_names": [],
        "os_name": platform.platform(),
    }
    if IS_WINDOWS:
        cpu = _reg_value(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
        info["cpu_name"] = (cpu or info["cpu_name"]).strip()
        info["gpu_names"] = _gpu_names()
        nt = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        product = _reg_value(nt, "ProductName") or "Windows"
        version = _reg_value(nt, "DisplayVersion") or ""
        build = str(_reg_value(nt, "CurrentBuild") or "")
        # Windows 11 still says "Windows 10" in ProductName; builds 22000+ are Windows 11.
        if build.isdigit() and int(build) >= 22000:
            product = product.replace("Windows 10", "Windows 11")
        info["os_name"] = f"{product} {version}".strip() + (f" (build {build})" if build else "")
    return info


def static_info() -> dict:
    """The PC's names: processor, graphics adapters, Windows and the computer's own (kept an hour)."""
    return _cached("static", 3600, _static_info)


# ------------------------------------------------- performance counters (PDH)

class _Counters:
    """One open PDH query, collected once per dashboard refresh."""

    PATHS = {
        "gpu_engine": r"\GPU Engine(*)\Utilization Percentage",
        "gpu_dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
        "gpu_shared": r"\GPU Adapter Memory(*)\Shared Usage",
        "standby_core": r"\Memory\Standby Cache Core Bytes",
        "standby_normal": r"\Memory\Standby Cache Normal Priority Bytes",
        "standby_reserve": r"\Memory\Standby Cache Reserve Bytes",
        "modified": r"\Memory\Modified Page List Bytes",
        "free_zero": r"\Memory\Free & Zero Page List Bytes",
        "pages_input": r"\Memory\Pages Input/sec",  # pages read back from disk for hard faults
        # The CPU's real speed, as Task Manager shows it: base frequency x % of it delivered (boost > 100)
        "cpu_performance": r"\Processor Information(_Total)\% Processor Performance",
        "cpu_base_mhz": r"\Processor Information(_Total)\Processor Frequency",
    }

    def __init__(self) -> None:
        import win32pdh

        self._pdh = win32pdh
        self._query = win32pdh.OpenQuery()
        self._counters = {}
        for key, path in self.PATHS.items():
            try:
                self._counters[key] = win32pdh.AddEnglishCounter(self._query, path)
            except Exception:
                pass  # counter not available on this PC (e.g. no WDDM GPU driver)
        win32pdh.CollectQueryData(self._query)  # rate counters need a first sample

    def read(self) -> dict:
        pdh = self._pdh
        pdh.CollectQueryData(self._query)
        values: dict[str, object] = {}
        for key, counter in self._counters.items():
            try:
                if "*" in self.PATHS[key]:
                    values[key] = pdh.GetFormattedCounterArray(counter, pdh.PDH_FMT_DOUBLE)
                else:
                    values[key] = pdh.GetFormattedCounterValue(counter, pdh.PDH_FMT_DOUBLE)[1]
            except Exception:
                values[key] = None  # e.g. no data yet for a brand-new instance
        return values


_counters: _Counters | None = None
_counters_lock = threading.Lock()
_counters_failed = False


def _read_counters() -> dict:
    global _counters, _counters_failed
    if not IS_WINDOWS or _counters_failed:
        return {}
    with _counters_lock:
        try:
            if _counters is None:
                _counters = _Counters()
            return _counters.read()
        except Exception:
            _counters_failed = True  # PDH unavailable: the dashboard just shows less
            return {}


def _gpu_summary(counters: dict) -> dict | None:
    """Overall GPU % = the busiest engine (summed over processes), like Task Manager."""
    engines = counters.get("gpu_engine")
    if not engines:
        return None
    per_engine: dict[tuple, float] = {}
    engine_type: dict[tuple, str] = {}
    per_pid: dict[int, dict[tuple, float]] = {}
    for instance, value in engines.items():
        match = _GPU_ENGINE_RE.match(instance)
        if not match or value is None:
            continue
        pid = int(match[1])
        key = (match[2], match[3], match[4])
        per_engine[key] = per_engine.get(key, 0.0) + value
        engine_type[key] = match[5]
        by_engine = per_pid.setdefault(pid, {})
        by_engine[key] = by_engine.get(key, 0.0) + value

    by_type: dict[str, float] = {}
    for key, value in per_engine.items():
        by_type[engine_type[key]] = max(by_type.get(engine_type[key], 0.0), min(100.0, value))
    processes = {pid: min(100.0, max(e.values())) for pid, e in per_pid.items()}
    global _gpu_by_pid
    _gpu_by_pid = {pid: round(v, 1) for pid, v in processes.items() if v >= 0.05}
    busiest = sorted(((pid, v) for pid, v in processes.items() if v >= 0.5), key=lambda kv: -kv[1])[:20]

    dedicated = counters.get("gpu_dedicated") or {}
    shared = counters.get("gpu_shared") or {}
    return {
        "utilization": round(min(100.0, max(per_engine.values(), default=0.0)), 1),
        "engines": {t: round(v, 1) for t, v in sorted(by_type.items(), key=lambda kv: -kv[1])[:6]},
        "processes": {str(pid): round(v, 1) for pid, v in busiest},
        "dedicated_mb": round(sum(v for v in dedicated.values() if v) / _MB),
        "shared_mb": round(sum(v for v in shared.values() if v) / _MB),
    }


def _memory_composition(counters: dict, vmem) -> dict:
    total = vmem.total / _MB
    standby_parts = [counters.get(k) for k in ("standby_core", "standby_normal", "standby_reserve")]
    if all(isinstance(v, float) for v in standby_parts) and isinstance(counters.get("modified"), float):
        standby = sum(standby_parts) / _MB
        modified = counters["modified"] / _MB
        free = (counters.get("free_zero") or 0.0) / _MB
        in_use = max(0.0, total - standby - modified - free)
    else:  # no counters: psutil's view only
        standby = modified = None
        free = vmem.available / _MB
        in_use = vmem.used / _MB
    return {
        "total_mb": round(total),
        "in_use_mb": round(in_use),
        "modified_mb": None if modified is None else round(modified),
        "standby_mb": None if standby is None else round(standby),
        "free_mb": round(free),
        "available_mb": round(vmem.available / _MB),
    }


# ---------------------------------------------------------- drives & network

def _drives() -> list[dict]:
    drives = []
    for part in psutil.disk_partitions(all=False):
        if IS_WINDOWS and "fixed" not in part.opts:
            continue  # skip DVD drives, card readers, network shares
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        drives.append({
            "mount": part.mountpoint, "fstype": part.fstype,
            "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
            "percent": usage.percent,
        })
    return drives


def _adapters(now: float, io: dict) -> list[dict]:
    stats = _cached("nic_stats", 30, psutil.net_if_stats)  # link state and speed rarely change; slow to read
    adapters = []
    for name, s in stats.items():
        if not s.isup or "loopback" in name.lower() or name not in io:
            continue
        adapters.append({
            "name": name,
            "speed_mbps": s.speed or None,
            "recv_bps": _rate(f"nic_recv:{name}", io[name].bytes_recv, now),
            "sent_bps": _rate(f"nic_sent:{name}", io[name].bytes_sent, now),
        })
    return adapters


def _restart_pending() -> bool:
    """Windows Update / servicing asked for a restart (read-only registry check)."""
    if not IS_WINDOWS:
        return False
    import winreg

    for path in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
    ):
        try:
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path).Close()
            return True
        except OSError:
            continue
    return False


_self = psutil.Process()


def _agent_usage() -> dict:
    """OptiMind's own footprint (this Python process), as % of the whole CPU."""
    try:
        cpu = _self.cpu_percent(interval=None) / (psutil.cpu_count() or 1)
        return {"cpu_percent": round(cpu, 1), "memory_mb": round(_self.memory_info().rss / _MB, 1),
                "threads": _cached("self_threads", 15, _self.num_threads)}  # reading it scans every process
    except psutil.Error:
        return {}


def gpu_by_pid() -> dict[int, float]:
    """GPU % per process from the last dashboard refresh (no new measurement)."""
    return _gpu_by_pid


def _cpu_speed(counters: dict, freq) -> tuple[int | None, int | None]:
    """(current MHz, base MHz). psutil only knows the base clock on Windows; the counters
    give what the CPU really runs at, boost included."""
    base, performance = counters.get("cpu_base_mhz"), counters.get("cpu_performance")
    if isinstance(base, float) and base > 0 and isinstance(performance, float) and performance > 0:
        return round(base * performance / 100), round(base)
    return (round(freq.current) if freq else None), (round(freq.max) if freq and freq.max else None)


def live_details() -> dict:
    """Everything the dashboard shows besides the agent's own snapshot."""
    from app.system import gpu as gpu_sensors
    from app.system import hardware

    now = time.monotonic()
    vmem = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    disk = psutil.disk_io_counters()
    nics = psutil.net_io_counters(pernic=True)  # read once: per adapter, and summed for the total
    counters = _read_counters()
    perf = memory.performance_info()
    pages = counters.get("pages_input")
    speed, base = _cpu_speed(counters, freq)
    return {
        "static": static_info(),
        # percpu keeps its own delta state in psutil, separate from the fast loop's total
        "cpu_per_core": psutil.cpu_percent(interval=None, percpu=True),
        "cpu_logical_cores": psutil.cpu_count(logical=True),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_freq_mhz": speed,
        "cpu_base_mhz": base,
        "process_count": len(psutil.pids()),
        "handles": perf.get("handles"),
        "threads": perf.get("threads"),
        "memory": {**_memory_composition(counters, vmem), **{
            k: perf.get(k) for k in ("commit_used_mb", "commit_limit_mb", "kernel_paged_mb", "kernel_nonpaged_mb")
        }, "hard_faults_mb_s": round(pages * 4096 / _MB, 1) if isinstance(pages, float) else None,
            "page_file": hardware.page_file()},
        "disk_read_bps": _rate("disk_read", disk.read_bytes, now) if disk else 0.0,
        "disk_write_bps": _rate("disk_write", disk.write_bytes, now) if disk else 0.0,
        "drives": _cached("drives", 30, _drives),
        "net_recv_bps": _rate("net_recv", sum(n.bytes_recv for n in nics.values()), now),
        "net_sent_bps": _rate("net_sent", sum(n.bytes_sent for n in nics.values()), now),
        "adapters": _adapters(now, nics),
        "gpu": _gpu_summary(counters),
        "gpu_sensors": gpu_sensors.primary(),  # NVIDIA cards: temperature, clocks, power, why it slows down
        "uptime_seconds": round(time.time() - psutil.boot_time()),
        "restart_pending": _cached("restart", 300, _restart_pending),
        "agent": _agent_usage(),
    }


# ------------------------------------------------------------------ services

_version_cache: dict[str, dict[str, str]] = {}


def file_version_info(path: str | None) -> dict[str, str]:
    """CompanyName, FileDescription and ProductName from a program file's
    version info (what Task Manager and msconfig show). Cached per file."""
    if not path:
        return {}
    if path not in _version_cache:
        info: dict[str, str] = {}
        try:
            import win32api

            translations = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation") or []
            for field in ("CompanyName", "FileDescription", "ProductName"):
                for lang, codepage in translations:
                    value = win32api.GetFileVersionInfo(path, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}")
                    if value and value.strip():
                        info[field] = value.strip()
                        break
        except Exception:
            pass  # no version info (or no access): callers fall back to the file name
        _version_cache[path] = info
    return _version_cache[path]


def _exe_from_command(command: str) -> str | None:
    command = os.path.expandvars((command or "").strip())
    if not command:
        return None
    if command.startswith('"'):
        end = command.find('"', 1)
        path = command[1:end] if end > 0 else command[1:]
    else:
        match = re.match(r"(.+?\.exe)\b", command, re.IGNORECASE)
        path = match.group(1) if match else command.split(" ")[0]
    if path.lower().startswith("\\systemroot\\"):
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), path[len("\\systemroot\\"):])
    return path


def _publisher(path: str | None) -> str | None:
    """CompanyName from the file's version info (what msconfig uses to spot Microsoft services)."""
    return file_version_info(path).get("CompanyName")


def _services() -> dict:
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower()
    items = []
    for service in psutil.win_service_iter():
        try:  # as_dict() fails on services without a description, so read fields one by one
            name, display = service.name(), service.display_name()
            status, start_type = service.status(), service.start_type()
            pid, binpath = service.pid(), service.binpath()
        except Exception:
            continue
        exe = _exe_from_command(binpath)
        publisher = _publisher(exe)
        exe_lower = (exe or "").lower()
        is_microsoft = (
            bool(publisher and ("microsoft" in publisher.lower() or publisher.startswith("Windows (R)")))
            or (exe_lower.startswith(windir) and not publisher)
            or "\\windowsapps\\microsoft." in exe_lower  # Store apps published by Microsoft
        )
        memory_mb = None
        if pid:
            try:
                memory_mb = round(psutil.Process(pid).memory_info().rss / _MB, 1)
            except Exception:
                pass
        items.append({
            "name": name, "display_name": display, "status": status, "start_type": start_type,
            "pid": pid or None, "publisher": publisher, "microsoft": is_microsoft,
            "memory_mb": memory_mb, "path": exe,
        })
    items.sort(key=lambda s: (s["status"] != "running", -(s["memory_mb"] or 0), s["display_name"].lower()))
    return {
        "total": len(items),
        "running": sum(1 for s in items if s["status"] == "running"),
        "items": items,
    }


def services() -> dict:
    """Windows services with their publisher and memory use (cached for a minute)."""
    if not IS_WINDOWS:
        return {"total": 0, "running": 0, "items": []}
    return _cached("services", 60, _services)
