"""
System metrics monitor.

Responsible only for OBSERVING (it never changes anything). Uses psutil,
which is portable and reasonably cheap — but note the honest limitation
in the README: since this runs in Python, the monitor itself costs more
CPU/RAM than a native C++/Rust service would. To keep it as "light" as
possible:

  - On Windows, all processes are read with ONE performance-counter
    query ("Process V2", whose instance names include the PID). Asking
    psutil process by process costs ~50x more, because for every
    protected system process psutil has to rescan the whole process list
    (~700 ms vs ~15 ms per cycle on a typical PC). psutil remains the
    fallback (other OSes, or if the counters are unavailable).
  - Non-blocking sampling (rates since the previous cycle), instead of
    sleeping inside the measurement.
  - We only keep the top N CPU/RAM consumers (plus the heaviest I/O
    users and the foreground app) in the snapshot.
  - The persisted history is aggregated (we don't store a raw snapshot
    of every process, only what the AI needs to see trends).
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime

import psutil

from app.agent import focus, safety
from app.agent.logger import trim_jsonl
from app.agent.optimizer import POWER_PLAN_GUIDS, access_problem
from app.config import IS_WINDOWS, METRICS_HISTORY_PATH
from app.i18n import tx
from app.models import ProcessSample, SystemSnapshot
from app.system import audio, memory
from app.system.admin_rights import is_elevated

_TOP_N_PROCESSES = 12
_TOP_N_IO_PROCESSES = 3
_TOP_N_MEMORY_PROCESSES = 8  # idle apps holding lots of RAM (CPU order alone would miss them)
_COMPRESSION_NAMES = {"memcompression", "memory compression"}
_IO_NOISE_BPS = 1024 * 1024  # below 1 MB/s of I/O a process isn't interesting for I/O alone
_MB = 1024 * 1024

_GUID_RE = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)
_PLAN_NAMES_BY_GUID = {guid: name for name, guid in POWER_PLAN_GUIDS.items()}

# IMPORTANT: psutil.Process.cpu_percent(interval=None) only computes a
# correct delta when called TWICE on the SAME Process instance, with
# real time passing between the calls. If we created new Process
# objects on every cycle (as psutil.process_iter() does by default),
# the value would always go back to 0%. That's why we keep a
# pid -> Process cache that is reused across take_snapshot calls.
_process_cache: dict[int, psutil.Process] = {}
_io_cache: dict[int, tuple[float, float]] = {}  # pid -> (bytes read+written, monotonic time)


def _iter_processes():
    """Iterates over the current processes, reusing Process objects we
    already know, so that cpu_percent(interval=None) measures the delta
    since the LAST cycle (not since the process started)."""
    current_pids = set(psutil.pids())

    for pid in current_pids:
        proc = _process_cache.get(pid)
        if proc is None:
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)  # "warms up" this instance
                _process_cache[pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        yield proc

    # processes that exited since the last cycle are removed from the caches
    for pid in list(_process_cache):
        if pid not in current_pids:
            _process_cache.pop(pid, None)
            _io_cache.pop(pid, None)


def _io_rate(pid: int, total_bytes: float, now: float) -> float:
    """Bytes per second since the previous cycle (0 the first time we see a process)."""
    prev = _io_cache.get(pid)
    _io_cache[pid] = (total_bytes, now)
    if prev is None or now <= prev[1]:
        return 0.0
    return max(0.0, (total_bytes - prev[0]) / (now - prev[1]))


def _priority_class(base: int) -> str | None:
    """Windows base priority (PDH "Priority Base") -> psutil priority class, for display.
    Classes start at 4 / 6 / 8 / 10 / 13 / 24, and a process can sit a step above
    its class's start (lsass.exe reports 9), so ranges are used, not exact values."""
    if base <= 0:
        return None
    for highest, priority_class in ((4, 64), (6, 16384), (9, 32), (12, 32768), (15, 128)):
        if base <= highest:
            return str(priority_class)
    return "256"


class _ProcessCounters:
    """Every process in one PDH query. Instance names look like "chrome:1234"."""

    PATHS = {
        "pid": r"\Process V2(*)\Process ID",
        "ppid": r"\Process V2(*)\Creating Process ID",
        "cpu": r"\Process V2(*)\% Processor Time",  # 100 = one full core, like psutil
        "ws": r"\Process V2(*)\Working Set",  # = psutil memory_info().rss
        "io": r"\Process V2(*)\IO Data Bytes/sec",  # reads + writes
        "prio": r"\Process V2(*)\Priority Base",
    }
    PRIVATE = r"\Process V2(*)\Private Bytes"  # its own memory, all of it counted in the commit charge

    def __init__(self) -> None:
        import win32pdh

        self._pdh = win32pdh
        self._query = win32pdh.OpenQuery()
        self._counters = {k: win32pdh.AddEnglishCounter(self._query, p) for k, p in self.PATHS.items()}
        try:
            self._counters["private"] = win32pdh.AddEnglishCounter(self._query, self.PRIVATE)
        except Exception:
            pass  # without it the rows have no private memory, and the rest works
        win32pdh.CollectQueryData(self._query)  # rates need a first sample

    def read(self) -> list[dict]:
        pdh = self._pdh
        pdh.CollectQueryData(self._query)
        fmt = pdh.PDH_FMT_DOUBLE | pdh.PDH_FMT_NOCAP100
        data = {k: pdh.GetFormattedCounterArray(c, fmt) for k, c in self._counters.items()}
        private = data.get("private")
        rows = []
        for instance, pid in data["pid"].items():
            if pid <= 0:  # "_Total" and the Idle process
                continue
            rows.append({
                "pid": int(pid),
                "ppid": int(data["ppid"].get(instance, 0)),
                "instance": instance.rsplit(":", 1)[0],
                "name": None,
                "cpu": data["cpu"].get(instance, 0.0),
                "mem_mb": data["ws"].get(instance, 0.0) / _MB,
                "private_mb": None if private is None else private.get(instance, 0.0) / _MB,
                "io_bps": data["io"].get(instance, 0.0),
                "priority": _priority_class(int(data["prio"].get(instance, 0))),
            })
        return rows


_GPU_ENGINE_RE = re.compile(r"pid_(\d+)_luid_(\w+?)_phys_(\d+)_eng_(\d+)_engtype_")
_GPU_NOISE_PERCENT = 3.0  # a background app using at least this much of the GPU is worth listing


class _GpuEngineCounters:
    """Per-process GPU use, like Task Manager's GPU column: each process's busiest engine
    (one PDH query on "GPU Engine(*)\\Utilization Percentage"). Only read while a game
    is in focus, where the engine looks for background GPU users."""

    PATH = r"\GPU Engine(*)\Utilization Percentage"

    def __init__(self) -> None:
        import win32pdh

        self._pdh = win32pdh
        self._query = win32pdh.OpenQuery()
        self._counter = win32pdh.AddEnglishCounter(self._query, self.PATH)
        win32pdh.CollectQueryData(self._query)  # a rate: the first values come next cycle

    def read(self) -> tuple[dict[int, float], dict[int, float]]:
        """(each process's busiest engine %, each process's video-encode %)."""
        pdh = self._pdh
        pdh.CollectQueryData(self._query)
        per_engine: dict[tuple, float] = {}
        encode: dict[int, float] = {}
        for instance, value in pdh.GetFormattedCounterArray(self._counter, pdh.PDH_FMT_DOUBLE).items():
            match = _GPU_ENGINE_RE.match(instance)
            if match and value:
                pid = int(match[1])
                key = (pid, match[2], match[3], match[4])
                per_engine[key] = per_engine.get(key, 0.0) + value
                if "encode" in instance[match.end():].lower():  # the video encoder: recording or streaming
                    encode[pid] = min(100.0, encode.get(pid, 0.0) + value)
        busiest: dict[int, float] = {}
        for (pid, *_), value in per_engine.items():
            busiest[pid] = min(100.0, max(busiest.get(pid, 0.0), value))
        return busiest, encode


_gpu_counters: _GpuEngineCounters | None = None
_gpu_counters_failed = False
_encoders: dict[int, float] = {}  # pid -> video-encode %, from the last read (a game in focus)


def _gpu_by_pid() -> dict[int, float]:
    """GPU % per process (its busiest engine), or {} where Windows has no GPU counters.
    Also notes who uses the video encoder (see encoders())."""
    global _gpu_counters, _gpu_counters_failed, _encoders
    if not IS_WINDOWS or _gpu_counters_failed:
        return {}
    try:
        if _gpu_counters is None:
            _gpu_counters = _GpuEngineCounters()
        busiest, _encoders = _gpu_counters.read()
        return busiest
    except Exception:
        if _gpu_counters is None:
            _gpu_counters_failed = True
        return {}


def encoders() -> dict[int, float]:
    """Processes using the GPU's video encoder (pid -> %) in the last cycle a game was in
    focus: something recording or streaming the game (app/agent/games.py)."""
    return _encoders


_process_counters: _ProcessCounters | None = None
_process_counters_failed = False
_process_counters_lock = threading.Lock()
_name_cache: dict[int, str] = {}
_access_cache: dict[int, str | None] = {}
_latest_rows: list[dict] = []  # every process from the last cycle (for the dashboard's Processes tab)
_latest_foreground_pid: int | None = None


def latest_processes() -> tuple[list[dict], int | None]:
    """Every process seen by the last snapshot (pid, ppid, name, cpu, mem_mb,
    private_mb, io_bps, priority) and the foreground PID. Read-only: callers get
    the list the fast loop already built, without measuring anything again."""
    return _latest_rows, _latest_foreground_pid


def process_tree(rows: list[dict], root: int | None, stop: set[int] | frozenset[int] = frozenset()) -> set[int]:
    """`root` and every process it started, directly or not (from the rows' parent PIDs),
    except the ones in `stop` and whatever they started in turn."""
    if not root:
        return set()
    children: dict[int, list[int]] = {}
    for r in rows:
        parent = r.get("ppid")
        if parent is not None and parent != r["pid"]:
            children.setdefault(parent, []).append(r["pid"])
    tree, stack = {root}, [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in tree and child not in stop:
                tree.add(child)
                stack.append(child)
    return tree


_STARTS_OTHER_APPS = {"explorer.exe"}  # everything you open from the desktop or Start is its child


def app_tree(rows: list[dict], pid: int | None) -> set[int]:
    """An app and its helper processes: what it started that has no window of its own (a
    browser engine's renderer and GPU processes, msedgewebview2.exe of a WebView2 app, an
    Electron or NW.js game's own copies). A child with its own window is an app of its own
    (a game started from Steam), and so is everything started from the desktop."""
    if not pid:
        return set()
    root = next((r for r in rows if r["pid"] == pid), None)
    if root is None or (root["name"] or "").lower() in _STARTS_OTHER_APPS:
        return {pid}
    from app.system.processes import app_window_pids  # (processes imports this module)

    return process_tree(rows, pid, stop=app_window_pids() - {pid})


_ESSENTIAL = tx("essential Windows or security process (always left alone)")


def blocked_reason(pid: int, name: str) -> str | None:
    """Why the agent must leave this process alone, or None if it may manage it.
    The permission check runs once per process (it only asks Windows)."""
    if safety.is_protected(name):
        return _ESSENTIAL
    if pid not in _access_cache:
        _access_cache[pid] = access_problem(pid)
    return _access_cache[pid]


def _rows_from_counters() -> list[dict] | None:
    """All processes via PDH, or None when that isn't possible (then psutil is used)."""
    global _process_counters, _process_counters_failed
    if not IS_WINDOWS or _process_counters_failed:
        return None
    with _process_counters_lock:
        try:
            if _process_counters is None:
                _process_counters = _ProcessCounters()
            return _process_counters.read()
        except Exception:
            if _process_counters is None:
                _process_counters_failed = True  # counters missing on this PC: use psutil from now on
            return None  # a one-off read error: psutil for this cycle only


def _rows_from_psutil(now: float) -> list[dict]:
    rows = []
    for proc in _iter_processes():
        try:
            with proc.oneshot():
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_info()
                mem_mb = mem.rss / _MB
                private = getattr(mem, "private", None)  # Windows only
                name = proc.name()
                try:
                    nice = proc.nice()
                except Exception:
                    nice = None
                try:
                    io = proc.io_counters()
                    io_bps = _io_rate(proc.pid, io.read_bytes + io.write_bytes, now)
                except Exception:
                    io_bps = 0.0
                ppid = proc.ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        rows.append({"pid": proc.pid, "ppid": ppid, "instance": name, "name": name, "cpu": cpu, "mem_mb": mem_mb,
                     "private_mb": None if private is None else private / _MB,
                     "io_bps": io_bps, "priority": None if nice is None else str(int(nice))})
    return rows


def _process_name(pid: int, instance: str) -> str:
    """The real image name (e.g. "chrome.exe"), which is what the optimizer and
    safety lists use. Looked up once per process and cached."""
    if pid not in _name_cache:
        try:
            _name_cache[pid] = psutil.Process(pid).name() or instance
        except psutil.Error:
            _name_cache[pid] = instance if instance in ("System", "Registry", "Secure System", "Memory Compression") \
                else f"{instance}.exe"
    return _name_cache[pid]


def _get_foreground_pid() -> int | None:
    """PID of the foreground window. Only really exists on Windows."""
    if not IS_WINDOWS:
        return None
    try:
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid
    except Exception:
        return None


def _get_user_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input in this session (GetLastInputInfo)."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes

        class _LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]

        info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32
        idle_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return round(idle_ms / 1000, 1)
    except Exception:
        return None


def _get_battery() -> tuple[bool | None, float | None]:
    """(on battery?, percent). Both None on desktops without a battery."""
    try:
        battery = psutil.sensors_battery()
    except Exception:
        return None, None
    if battery is None:
        return None, None
    on_battery = None if battery.power_plugged is None else not battery.power_plugged
    return on_battery, round(battery.percent, 1)


_POWER_SCHEMES_KEY = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"


def _get_active_power_plan() -> str | None:
    """Reads the active plan from the registry (instant, no extra process).
    Falls back to `powercfg /getactivescheme` if the key can't be read."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _POWER_SCHEMES_KEY) as key:
            guid = str(winreg.QueryValueEx(key, "ActivePowerScheme")[0]).lower()
        if guid in _PLAN_NAMES_BY_GUID:
            return _PLAN_NAMES_BY_GUID[guid]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{_POWER_SCHEMES_KEY}\{guid}") as key:
            friendly = str(winreg.QueryValueEx(key, "FriendlyName")[0])
        # Built-in names look like "@...powrprof.dll,-15,Balanced"; custom ones are plain text.
        return (friendly.rsplit(",", 1)[-1] if friendly.startswith("@") else friendly).strip() or None
    except OSError:
        pass
    try:
        import subprocess

        out = subprocess.check_output(
            ["powercfg", "/getactivescheme"], text=True, timeout=5
        )
        return _parse_power_plan(out)
    except Exception:
        return None


_plans_cache: tuple[float, list[str]] | None = None


def installed_power_plans() -> list[str] | None:
    """The built-in plans OptiMind switches between that this PC has, e.g. only ["Balanced"] on
    many laptops, where Windows' Power mode replaces High performance. None when unknown.
    Kept 10 minutes (plans rarely change)."""
    global _plans_cache
    if not IS_WINDOWS:
        return None
    now = time.monotonic()
    if _plans_cache is not None and now - _plans_cache[0] < 600:
        return _plans_cache[1]
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _POWER_SCHEMES_KEY) as key:
            guids = {winreg.EnumKey(key, i).lower() for i in range(winreg.QueryInfoKey(key)[0])}
    except OSError:
        return None
    plans = [name for guid, name in _PLAN_NAMES_BY_GUID.items() if guid in guids]
    _plans_cache = (now, plans)
    return plans


def _parse_power_plan(powercfg_output: str) -> str | None:
    """Typical format: "Power Scheme GUID: <GUID>  (Plan name)".
    The plan name is translated on non-English Windows, so built-in plans
    are identified by their GUID and reported with the same English names
    the optimizer uses; custom plans fall back to the name in parentheses."""
    match = _GUID_RE.search(powercfg_output)
    if match and match.group(0).lower() in _PLAN_NAMES_BY_GUID:
        return _PLAN_NAMES_BY_GUID[match.group(0).lower()]
    name = powercfg_output.strip().rsplit("(", 1)[-1].rstrip(")").strip()
    return name or None


def prime_cpu_counters() -> None:
    """
    psutil.cpu_percent()/Process.cpu_percent() need a first "throwaway"
    call before they start measuring the delta correctly.
    Call this once at program startup, before the main loop.
    """
    psutil.cpu_percent(interval=None)
    if _rows_from_counters() is None:
        for _ in _iter_processes():
            pass  # _iter_processes itself already "warms up" every new Process


def take_snapshot() -> SystemSnapshot:
    """Captures the current state of the machine. Non-blocking (interval=None)."""
    cpu_percent = psutil.cpu_percent(interval=None)
    vmem = psutil.virtual_memory()
    # Focus mode's view first: the Alt+Tab switcher, Start or the taskbar in front for a
    # moment don't make the app you're using count as "in the background". The app and
    # whether it's a game come from one read, so a switch meanwhile can't mix them up.
    focus_pid, game, recent = focus.snapshot_info()
    foreground_pid = focus_pid or _get_foreground_pid()

    rows = _rows_from_counters()
    if rows is None:
        rows = _rows_from_psutil(time.monotonic())
    live_pids = {r["pid"] for r in rows}
    for cache in (_name_cache, _access_cache):
        for pid in list(cache):
            if pid not in live_pids:
                cache.pop(pid, None)
    for r in rows:
        r["name"] = r["name"] or _process_name(r["pid"], r["instance"])  # cached per process
    global _latest_rows, _latest_foreground_pid
    _latest_rows, _latest_foreground_pid = rows, foreground_pid
    in_front = app_tree(rows, foreground_pid)  # the app you're using and its helper processes
    compressed_mb = next((r["mem_mb"] for r in rows if r["name"].lower() in _COMPRESSION_NAMES), None)
    compression = memory.compression_stats(compressed_mb)
    audible_pids = audio.audible_pids()
    audible = sorted({r["name"] for r in rows if r["pid"] in audible_pids and not safety.is_protected(r["name"])})
    global _encoders
    if game:
        gpu_use = _gpu_by_pid()  # per-process GPU % only matters while a game is in focus
    else:
        gpu_use, _encoders = {}, {}

    rows = [
        r for r in rows
        if r["cpu"] > 0 or r["mem_mb"] >= 20 or r["io_bps"] >= _IO_NOISE_BPS or r["pid"] == foreground_pid
    ]  # skip idle noise
    rows.sort(key=lambda r: (r["cpu"], r["mem_mb"]), reverse=True)
    top, rest = rows[:_TOP_N_PROCESSES], rows[_TOP_N_PROCESSES:]
    # Heavy I/O users, the biggest memory holders and the foreground app are always
    # included, so the engine can manage disk hogs, compress idle apps' memory and
    # restore the app the user is using.
    top += sorted((r for r in rest if r["io_bps"] >= _IO_NOISE_BPS), key=lambda r: r["io_bps"], reverse=True)[
        :_TOP_N_IO_PROCESSES
    ]
    chosen = {r["pid"] for r in top}
    top += sorted((r for r in rest if r["pid"] not in chosen), key=lambda r: r["mem_mb"], reverse=True)[
        :_TOP_N_MEMORY_PROCESSES
    ]
    chosen = {r["pid"] for r in top}
    top += [r for r in rest if r["pid"] == foreground_pid and r["pid"] not in chosen]
    # Its helper processes under other names are in front too; the engine goes by name, so one of each is enough.
    names = {r["name"].lower() for r in top if r["pid"] in in_front}
    for r in rest:
        if r["pid"] in in_front and r["pid"] not in chosen and r["name"].lower() not in names:
            top.append(r)
            names.add(r["name"].lower())
    if gpu_use:  # background GPU users, for the engine's GPU priority while you play
        chosen = {r["pid"] for r in top}
        top += sorted((r for r in rest if r["pid"] not in chosen and gpu_use.get(r["pid"], 0) >= _GPU_NOISE_PERCENT),
                      key=lambda r: -gpu_use[r["pid"]])[:_TOP_N_IO_PROCESSES]

    samples = []
    for r in top:
        name = r["name"]
        samples.append(ProcessSample(
            pid=r["pid"],
            name=name,
            cpu_percent=round(r["cpu"], 1),
            memory_mb=round(r["mem_mb"], 1),
            io_bps=round(r["io_bps"]),
            gpu_percent=round(gpu_use.get(r["pid"], 0.0), 1),
            is_foreground=r["pid"] in in_front,
            priority=r["priority"],
            blocked_reason=blocked_reason(r["pid"], name),
        ))

    on_battery, battery_percent = _get_battery()
    lists = memory.lists_mb() or {}
    commit = memory.performance_info()
    faults = lists.get("hard_faults_mb_s")
    return SystemSnapshot(
        cpu_percent=cpu_percent,
        ram_percent=vmem.percent,
        ram_available_mb=round(vmem.available / _MB, 1),
        top_processes=samples,
        power_plan=_get_active_power_plan(),
        power_plans=installed_power_plans(),
        user_idle_seconds=_get_user_idle_seconds(),
        on_battery=on_battery,
        battery_percent=battery_percent,
        agent_is_admin=is_elevated() if IS_WINDOWS else None,
        free_mb=None if "free" not in lists else round(lists["free"]),
        standby_mb=None if "standby" not in lists else round(lists["standby"]),
        compressed_mb=None if compressed_mb is None else round(compressed_mb, 1),
        compressed_data_mb=compression["data_mb"] if compression else None,
        compression_ratio=compression["ratio"] if compression else None,
        commit_used_mb=commit.get("commit_used_mb"),
        commit_limit_mb=commit.get("commit_limit_mb"),
        hard_faults_mb_s=None if faults is None else round(faults, 1),
        foreground_game=game,
        recent_apps=recent,
        audible_apps=audible,
    )


_HISTORY_MAX_BYTES = 6_000_000  # about 24 hours of 2-second samples
_HISTORY_KEEP_BYTES = 4_500_000
_DAILY_AGGREGATE_PATH = METRICS_HISTORY_PATH.parent / "metrics_daily.jsonl"


def _daily_aggregate(entries: list[dict]) -> dict:
    """Roll up one day of 2-second samples into a single summary entry."""
    if not entries:
        return {}
    cpus = [e.get("cpu", 0.0) for e in entries if e.get("cpu") is not None]
    rams = [e.get("ram", 0.0) for e in entries if e.get("ram") is not None]
    return {
        "t": entries[-1].get("t", ""),  # last timestamp of the day
        "type": "daily",
        "cpu_avg": round(sum(cpus) / len(cpus), 1) if cpus else 0.0,
        "cpu_max": round(max(cpus), 1) if cpus else 0.0,
        "ram_avg": round(sum(rams) / len(rams), 1) if rams else 0.0,
        "ram_max": round(max(rams), 1) if rams else 0.0,
        "samples": len(entries),
    }


def append_history(snapshot: SystemSnapshot) -> None:
    """
    Stores an aggregated summary (not the full process list) so the AI
    can see trends without blowing up the prompt or the disk.
    Keeps about 24 hours of 2-second samples plus daily aggregates for
    longer-term trend analysis.
    """
    entry = {
        "t": snapshot.timestamp.isoformat(),
        "cpu": snapshot.cpu_percent,
        "ram": snapshot.ram_percent,
        "ram_avail_mb": snapshot.ram_available_mb,
        "top": [f"{p.name}:{p.cpu_percent:.0f}%cpu/{p.memory_mb:.0f}MB" for p in snapshot.top_processes[:5]],
        "power_plan": snapshot.power_plan,
    }
    with METRICS_HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Only the size is checked each cycle; the file is rewritten about once an hour.
    trim_jsonl(METRICS_HISTORY_PATH, _HISTORY_MAX_BYTES, _HISTORY_KEEP_BYTES)

    # Once per day, roll up yesterday's samples into a daily aggregate.
    _maybe_write_daily_aggregate()


def _last_aggregate_date() -> str | None:
    if not _DAILY_AGGREGATE_PATH.exists():
        return None
    try:
        lines = _DAILY_AGGREGATE_PATH.read_text(encoding="utf-8").splitlines()
        if not lines:
            return None
        last = json.loads(lines[-1])
        return last.get("date")
    except (json.JSONDecodeError, OSError):
        return None


def _load_recent_history(max_bytes: int = 6_000_000) -> list[dict]:
    """Read the metrics history JSONL, keeping at most max_bytes from the end."""
    if not METRICS_HISTORY_PATH.exists():
        return []
    data = METRICS_HISTORY_PATH.read_text(encoding="utf-8")
    lines = data.splitlines()
    # Trim from the front until we fit in max_bytes
    while lines and sum(len(l) + 1 for l in lines) > max_bytes:
        lines.pop(0)
    entries: list[dict] = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _maybe_write_daily_aggregate() -> None:
    """Roll up the previous calendar day into one daily entry (once per day)."""
    now = datetime.now().replace(tzinfo=None)
    today = now.date().isoformat()
    if _last_aggregate_date() == today:
        return  # already aggregated today

    history = _load_recent_history()
    if not history:
        return
    # Group entries by date
    by_day: dict[str, list[dict]] = {}
    for entry in history:
        t = entry.get("t", "")
        day = t[:10]  # "YYYY-MM-DD"
        by_day.setdefault(day, []).append(entry)

    # Aggregate every completed day (not today)
    lines = []
    for day in sorted(by_day):
        if day >= today:
            continue
        agg = _daily_aggregate(by_day[day])
        agg["date"] = day
        lines.append(json.dumps(agg, ensure_ascii=False))

    if lines:
        with _DAILY_AGGREGATE_PATH.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")


def read_recent_history(n: int = 40) -> list[dict]:
    if not METRICS_HISTORY_PATH.exists():
        return []
    lines = METRICS_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:]]


def read_daily_history(days: int = 30) -> list[dict]:
    """Read daily aggregate entries, newest first, up to `days`."""
    if not _DAILY_AGGREGATE_PATH.exists():
        return []
    lines = _DAILY_AGGREGATE_PATH.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in lines[-days:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(entries))


if __name__ == "__main__":
    # Quick demo via `python -m app.system.monitor`
    prime_cpu_counters()
    time.sleep(1)
    snap = take_snapshot()
    print(snap.model_dump_json(indent=2))
