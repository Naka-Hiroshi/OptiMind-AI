"""
Local optimization engine — the "fast loop". Runs every
OPTIMIND_FAST_LOOP_SECONDS and decides/applies actions based on the
current OptimizationPolicy (which the AI adjusts periodically).

Every function here:
  1. only really executes on Windows (IS_WINDOWS);
  2. goes through app.agent.safety validation before touching anything;
  3. respects `dry_run` (test mode, OPTIMIND_DRY_RUN): if True, it only
     logs what it would do;
  4. checks the result: after each change Windows is asked again, and a
     change Windows didn't keep is logged as an error, never as "applied";
  5. never raises an exception out of the main loop — failures become
     a log entry with result="error", and the agent keeps running.

Changes to a running app (priority, efficiency mode, I/O and memory
priority, CPU cores, Focus mode's boost) are remembered together with
the app's ORIGINAL setting, so they are put back exactly as they were:
when the app comes back to the front, when OptiMind quits, and, if
OptiMind was closed abruptly, the next time it starts (data/held_back.json).

Techniques used (all common in legitimate optimization tools and
"RAM cleaners" — nothing here uses an unsigned driver or touches kernel
areas outside the documented/whitelisted APIs):

  - Priority: psutil.Process.nice() (SetPriorityClass). Only Normal or
    Above normal apps are lowered: an app already running low, or set to
    High/Realtime on purpose, is left as it is.
  - Efficiency mode (EcoQoS): SetProcessInformation with
    ProcessPowerThrottling — the same switch as Task Manager's
    "Efficiency mode" (Windows 11). Apps that already run in efficiency
    mode (browsers do it for background tabs) are left as they are.
  - I/O priority: psutil.Process.ionice() (low/normal I/O priority), so
    background disk work yields to the app the user is using.
  - Compressing an idle app's memory: EmptyWorkingSet (psapi.dll) moves
    the app's pages out of RAM; Windows compresses the changed ones into
    its memory compression store (or keeps unchanged ones as cache), and
    they come back when the app needs them. Its memory priority is also
    lowered (SetProcessInformation with ProcessMemoryPriority, what
    Microsoft recommends for background work), so if RAM runs short,
    Windows trims that app's pages before the ones of the app in front.
  - Merging identical memory pages: NtSetSystemInformation with
    SystemCombinePhysicalMemoryInformation, the "page combining" the
    memory manager also runs on its own schedule (Enable-MMAgent
    -PageCombining). Used by System Informer and Mem Reduct. Needs an
    elevated (admin) process.
  - Emptying the system standby list: NtSetSystemInformation with
    SystemMemoryListInformation — the technique used by the public
    EmptyStandbyList.exe / RAMMap (Sysinternals) utilities. Requires
    the SeProfileSingleProcessPrivilege privilege and an elevated
    (admin) process.
  - Keeping a background app on one core while a game is in focus
    (Maximum focus mode): SetProcessDefaultCpuSets, Windows' "soft"
    affinity, so the game's cores aren't shared with it.
  - Focus mode's boost: the app in front runs at Above normal priority
    (see app/agent/focus.py).
  - GPU priority, while a game is in focus: a background app using the
    graphics card gets Below normal GPU scheduling priority
    (D3DKMTSetProcessSchedulingPriorityClass, the GPU's equivalent of a
    priority class), so the GPU scheduler serves the game first. Never
    raised above Above normal.
  - Power plan: `powercfg /setactive <GUID>` (the same thing the
    Control Panel does).
"""
from __future__ import annotations

import ctypes
import functools
import json
import logging
import subprocess
import threading
import time
from dataclasses import asdict, dataclass

import psutil

from app import i18n
from app.agent import safety
from app.agent.logger import log_action
from app.config import DATA_DIR, IS_WINDOWS
from app.i18n import tx, txc, txn
from app.models import ActionLogEntry, ActionType, OptimizationAction
from app.system import memory
from app.system import gpu_optimization
from app.system.admin_rights import enable_privilege, is_elevated

logger = logging.getLogger("optimind.optimizer")

# Default GUIDs of the built-in Windows power plans.
POWER_PLAN_GUIDS = {
    "High performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "Balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "Power saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}
# ...and their names as Windows shows them in your language
PLAN_NAMES = {"High performance": txc("plan", "High performance"), "Balanced": txc("plan", "Balanced"),
              "Power saver": txc("plan", "Power saver")}
_POWER_SCHEMES_KEY = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HELD_BACK_PATH = DATA_DIR / "held_back.json"
_MB = 1024 * 1024

# Priority classes and I/O priorities (psutil's names; plain numbers off Windows)
_IDLE = getattr(psutil, "IDLE_PRIORITY_CLASS", 0x40)
_BELOW_NORMAL = getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
_NORMAL = getattr(psutil, "NORMAL_PRIORITY_CLASS", 0x20)
_ABOVE_NORMAL = getattr(psutil, "ABOVE_NORMAL_PRIORITY_CLASS", 0x8000)
_LOWERABLE_PRIORITIES = {_NORMAL, _ABOVE_NORMAL}  # lower = idle/below normal already; higher = set on purpose
_IO_LOW = getattr(psutil, "IOPRIO_LOW", 1)
_IO_NORMAL = getattr(psutil, "IOPRIO_NORMAL", 2)

# SetProcessInformation(ProcessPowerThrottling / ProcessMemoryPriority) — see Microsoft's
# "SetProcessInformation", "Quality of Service" and "MEMORY_PRIORITY_INFORMATION" docs.
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SET_LIMITED_INFORMATION = 0x2000  # enough for CPU sets
_PROCESS_MEMORY_PRIORITY = 0  # PROCESS_INFORMATION_CLASS.ProcessMemoryPriority
_PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_POWER_THROTTLING_CURRENT_VERSION = 1
_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_MEMORY_PRIORITY_LOW = 2  # MEMORY_PRIORITY_LOW; every process starts at 5 (MEMORY_PRIORITY_NORMAL)

# NtSetSystemInformation(SystemCombinePhysicalMemoryInformation): page combining on demand.
_SYSTEM_COMBINE_PHYSICAL_MEMORY_INFORMATION = 130
_PAGE_SIZE = 4096

# D3DKMT[Get|Set]ProcessSchedulingPriorityClass (D3DKMT_SCHEDULINGPRIORITYCLASS: idle 0, below normal 1,
# normal 2, above normal 3, high 4, realtime 5). Windows asks for PROCESS_SET_INFORMATION even to read it,
# and answers STATUS_INVALID_PARAMETER for a process that has never used the GPU.
_GPU_BELOW_NORMAL, _GPU_NORMAL, _GPU_ABOVE_NORMAL = 1, 2, 3
_GPU_LOWERABLE = {_GPU_NORMAL, _GPU_ABOVE_NORMAL}  # lower = already low; higher = set on purpose (e.g. a recorder)
_STATUS_INVALID_PARAMETER = 0xC000000D


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


class _MemoryPriority(ctypes.Structure):
    _fields_ = [("MemoryPriority", ctypes.c_ulong)]


class _CombineInformation(ctypes.Structure):
    """MEMORY_COMBINE_INFORMATION_EX: Windows fills in how many pages it merged."""
    _fields_ = [("Handle", ctypes.c_void_p), ("PagesCombined", ctypes.c_size_t), ("Flags", ctypes.c_ulong)]


class _CpuSet(ctypes.Structure):
    """SYSTEM_CPU_SET_INFORMATION (one entry per logical processor)."""
    _fields_ = [
        ("Size", ctypes.c_ulong), ("Type", ctypes.c_int), ("Id", ctypes.c_ulong), ("Group", ctypes.c_ushort),
        ("LogicalProcessorIndex", ctypes.c_ubyte), ("CoreIndex", ctypes.c_ubyte),
        ("LastLevelCacheIndex", ctypes.c_ubyte), ("NumaNodeIndex", ctypes.c_ubyte),
        ("EfficiencyClass", ctypes.c_ubyte), ("AllFlags", ctypes.c_ubyte), ("Reserved", ctypes.c_ulong),
        ("AllocationTag", ctypes.c_ulonglong),
    ]


@functools.lru_cache(maxsize=1)
def _kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetProcessInformation.restype = wintypes.BOOL
    kernel32.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetProcessInformation.restype = wintypes.BOOL
    kernel32.GetSystemCpuSetInformation.argtypes = [ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
                                                    wintypes.HANDLE, wintypes.ULONG]
    kernel32.GetSystemCpuSetInformation.restype = wintypes.BOOL
    kernel32.SetProcessDefaultCpuSets.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]
    kernel32.SetProcessDefaultCpuSets.restype = wintypes.BOOL
    kernel32.GetProcessDefaultCpuSets.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG,
                                                  ctypes.POINTER(wintypes.ULONG)]
    kernel32.GetProcessDefaultCpuSets.restype = wintypes.BOOL
    return kernel32


@functools.lru_cache(maxsize=1)
def _psapi():
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL
    return psapi


# Every right the optimizer may need on a process: priority, efficiency mode
# and I/O priority (set information) and emptying its working set (query + set quota).
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_SET_QUOTA = 0x0100
_MANAGE_RIGHTS = _PROCESS_SET_INFORMATION | _PROCESS_SET_QUOTA | _PROCESS_QUERY_INFORMATION

_PROCESS_PROTECTION_LEVEL_INFO = 7  # PROCESS_INFORMATION_CLASS.ProcessProtectionLevelInfo
_PROTECTION_LEVEL_NONE = 0xFFFFFFFE

# Why the agent leaves a process alone (the Processes page labels them by these first words: blocked_kind).
PROTECTED_BY_WINDOWS = tx("protected by Windows (not even an administrator can change it)")
OTHER_ACCOUNT = tx("runs as a service or under another account (left alone)")
NEEDS_ADMIN = tx("runs as administrator (OptiMind needs administrator rights to change it)")
NOT_FOUND = "process not found"  # the start of a result: none of the picked processes was found (logged as unchanged)
_GONE = tx("process not found (it may have already exited)")
_NOTHING_TO_PUT_BACK = tx("nothing to put back (the agent hadn't changed it)")


def blocked_kind(reason: str | None) -> str | None:
    """What kind of reason the agent has to leave a process alone, for its label on the Processes
    page: "protected" (Windows' own or a security app), "admin" (it runs as administrator) or
    "service" (a service or another account)."""
    if not reason:
        return None
    if reason.startswith(("essential", "protected")):
        return "protected"
    return "admin" if reason.startswith("runs as administrator") else "service"


def _is_protected_by_windows(pid: int) -> bool:
    """A Windows protected process (e.g. an antivirus engine): not even an
    administrator may change it. False when Windows won't say."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        level = ctypes.c_ulong()
        if not kernel32.GetProcessInformation(handle, _PROCESS_PROTECTION_LEVEL_INFO, ctypes.byref(level), ctypes.sizeof(level)):
            return False
        return level.value != _PROTECTION_LEVEL_NONE
    finally:
        kernel32.CloseHandle(handle)


@functools.lru_cache(maxsize=1)
def _own_account() -> str:
    return psutil.Process().username().lower()


def _is_own_account(pid: int) -> bool:
    """Does this process run under OptiMind's own Windows account?
    (When Windows won't tell, e.g. for a system process, the answer is no.)"""
    try:
        return psutil.Process(pid).username().lower() == _own_account()
    except Exception:
        return False


def access_problem(pid: int) -> str | None:
    """Why the agent leaves this process alone, or None if it may change it.
    It only asks Windows (opens and closes handles) — nothing is changed.
    Even with administrator rights, only processes of OptiMind's own account
    are managed: Windows services and other people's programs are left alone."""
    if not IS_WINDOWS:
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_MANAGE_RIGHTS, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
    elif is_elevated() or _is_protected_by_windows(pid):
        return PROTECTED_BY_WINDOWS
    if not _is_own_account(pid):
        return OTHER_ACCOUNT
    return None if handle else NEEDS_ADMIN  # yours, but running as administrator


def _not_changed(procs: list) -> PermissionError:
    """Logged as an error, never as a misleading "applied"."""
    return PermissionError(tx("not changed, {why}", why=access_problem(procs[0].pid) or tx("access denied")))


# ------------------------------------------------------------ what the agent holds back

@dataclass
class _Held:
    """What the agent changed on one running process, with the values to put back."""
    pid: int
    name: str
    created: float  # the process's start time, so a reused PID is never mistaken for it
    priority: int | None = None  # original priority class (before a hold-back or Focus mode's boost)
    throttle: list[int] | None = None  # original efficiency-mode state [ControlMask, StateMask]
    io: int | None = None  # original I/O priority
    memory: int | None = None  # original memory priority (1-5)
    cores: list[int] | None = None  # original default CPU sets ([] = none: every core)
    boosted: bool = False  # the priority change is Focus mode's boost, not a hold-back
    gpu: int | None = None  # original GPU scheduling priority class


# field -> dashboard label ("focus" instead of "priority" while it is Focus mode's boost)
_KINDS = {"priority": "priority", "throttle": "efficiency", "io": "io", "memory": "memory", "cores": "cores",
          "gpu": "gpu"}
_LABEL_ORDER = ["priority", "efficiency", "io", "memory", "cores", "gpu", "focus"]
_WHAT = {"priority": tx("priority"), "throttle": tx("efficiency mode"), "io": tx("I/O priority"),
         "memory": tx("memory priority"), "cores": tx("CPU cores"), "gpu": tx("GPU priority")}
_ORIGINAL = {"priority": tx("original priority"), "throttle": tx("original efficiency setting"),
             "io": tx("original I/O priority"), "memory": tx("original memory priority"),
             "cores": tx("original CPU cores"), "gpu": tx("original GPU priority")}
_held: dict[int, _Held] = {}
_held_lock = threading.RLock()


def _save_held() -> None:
    try:
        HELD_BACK_PATH.write_text(json.dumps([asdict(h) for h in _held.values()]), encoding="utf-8")
    except OSError as e:
        logger.warning("Couldn't save what the agent holds back (%s)", e)


def _hold(proc: psutil.Process) -> _Held:
    """The record for this process (created on first change). Call with _held_lock held."""
    held = _held.get(proc.pid)
    created = proc.create_time()
    if held is None or held.created != created:
        held = _held[proc.pid] = _Held(proc.pid, proc.name(), created)
    return held


def _process_of(held: _Held) -> psutil.Process | None:
    """The process this record is about, if it's still running (and not a new one with the same PID)."""
    try:
        proc = psutil.Process(held.pid)
        return proc if proc.create_time() == held.created else None
    except psutil.Error:
        return None


def _labels(h: _Held) -> set[str]:
    labels = {label for field, label in _KINDS.items() if getattr(h, field) is not None}
    if h.boosted:
        labels.discard("priority")
        labels.add("focus")
    return labels


def held_back() -> dict[str, list[str]]:
    """Apps the agent is holding back right now, and how ("priority", "efficiency",
    "io", "memory", "cores"; "focus" marks the app in front that Focus mode boosts).
    Records of apps that have closed are dropped."""
    out: dict[str, set[str]] = {}
    with _held_lock:
        gone = [pid for pid, h in _held.items() if _process_of(h) is None]
        for pid in gone:
            _held.pop(pid)
        if gone:
            _save_held()
        for h in _held.values():
            labels = _labels(h)
            if labels:
                out.setdefault(h.name.lower(), set()).update(labels)
    return {name: sorted(labels, key=_LABEL_ORDER.index) for name, labels in sorted(out.items())}


def _put_back(held: _Held, proc: psutil.Process, field: str) -> None:
    """Restores one setting to its original value and checks Windows kept it."""
    original = getattr(held, field)
    if field == "priority":
        if held.boosted and proc.nice() != _ABOVE_NORMAL:
            return  # changed by someone else since the boost (e.g. in Task Manager): theirs stays
        proc.nice(original)
        if proc.nice() != original:
            raise PermissionError(tx("Windows didn't keep the original priority"))
    elif field == "io":
        proc.ionice(original)
        if proc.ionice() != original:
            raise PermissionError(tx("Windows didn't keep the original I/O priority"))
    elif field == "throttle":
        _set_throttle_checked(proc.pid, *original)
    elif field == "memory":
        _set_memory_priority_checked(proc.pid, original)
    elif field == "cores":
        _set_cores_checked(proc.pid, original)
    elif field == "gpu":
        _set_gpu_priority_checked(proc.pid, min(int(original), _GPU_ABOVE_NORMAL))


def _release(h: _Held, fields) -> tuple[set[str], int, bool]:
    """Puts back the given fields of one record (those it holds) and forgets them.
    Returns (fields put back, how many Windows refused, whether the process has closed)."""
    proc = _process_of(h)
    restored: set[str] = set()
    failed = 0
    for field in fields:
        if getattr(h, field) is None:
            continue
        if proc is not None:
            try:
                _put_back(h, proc, field)
                restored.add(field)
            except (psutil.Error, OSError):
                failed += 1
        with _held_lock:
            setattr(h, field, None)
            if field == "priority":
                h.boosted = False
    with _held_lock:
        if all(getattr(h, f) is None for f in _KINDS) and _held.get(h.pid) is h:
            _held.pop(h.pid)
    return restored, failed, proc is None


def _also(first: i18n.Text, gone: int, failed: int) -> i18n.Text:
    """A give-back's result, with the instances that had closed and those Windows refused."""
    return i18n.joined([first, txn(gone, "{n} already closed", "{n} already closed") if gone else None,
                        txn(failed, "{n} refused", "{n} refused") if failed else None], ", ")


def _restore(name: str, field: str) -> str:
    """Puts back the original `field` on every instance of `name` the agent changed."""
    with _held_lock:
        held = [h for h in _held.values() if h.name.lower() == name.lower() and getattr(h, field) is not None]
    restored = gone = failed = 0
    for h in held:
        done, refused, closed = _release(h, (field,))
        restored += bool(done)
        failed += refused
        gone += closed
    if held:
        with _held_lock:
            _save_held()
    what = _ORIGINAL[field]
    if not held:
        return _NOTHING_TO_PUT_BACK
    if failed and not restored:
        raise PermissionError(tx("{what} not restored: Windows refused", what=what))
    return _also(txn(restored, "{what} restored on {n} instance (checked)", "{what} restored on {n} instances (checked)",
                     what=what), gone, failed)


def restore_app(name: str) -> str:
    """Puts back everything the agent changed on every instance of `name`, in one go
    (the app came back to the front, or started playing sound). Focus mode's boost isn't
    something held back: it stays, and unboost() moves it with the focus. (Counting it made
    every switch between two windows of one app, like a browser and its file dialog,
    log a give-back.)"""
    with _held_lock:
        held = [h for h in _held.values() if h.name.lower() == name.lower()]
    if not held:
        return _NOTHING_TO_PUT_BACK
    restored: set[str] = set()
    instances = failed = gone = 0
    for h in held:
        done, refused, closed = _release(h, tuple(f for f in _KINDS if not (f == "priority" and h.boosted)))
        restored |= done
        instances += bool(done)
        failed += refused
        gone += closed
    with _held_lock:
        _save_held()
    if failed and not restored:
        raise PermissionError(tx("not restored: Windows refused"))
    if not restored:
        return (txn(gone, "nothing to put back ({n} instance already closed)", "nothing to put back ({n} instances already closed)")
                if gone else _NOTHING_TO_PUT_BACK)
    what = i18n.items([_WHAT[f] for f in _KINDS if f in restored])
    return _also(txn(instances, "{what} put back on {n} instance (checked)", "{what} put back on {n} instances (checked)",
                     what=what), gone, failed)


def restore_field(field: str) -> int:
    """Puts back one kind of change on every app (e.g. "cores" when Maximum focus mode
    is switched off). Returns how many processes got it back."""
    with _held_lock:
        held = [h for h in _held.values() if getattr(h, field) is not None]
    count = sum(1 for h in held if _release(h, (field,))[0])
    if held:
        with _held_lock:
            _save_held()
    return count


def restore_all() -> int:
    """Puts back everything the agent changed on running apps (OptiMind quitting).
    Returns how many processes were restored."""
    with _held_lock:
        held = list(_held.values())
        _held.clear()
    restored = 0
    for h in held:
        proc = _process_of(h)
        if proc is None:
            continue
        for field in _KINDS:
            if getattr(h, field) is not None:
                try:
                    _put_back(h, proc, field)
                except (psutil.Error, OSError):
                    pass
        restored += 1
    with _held_lock:
        _save_held()
    if restored:
        logger.info("Put back the original settings of %d process(es)", restored)
    return restored


def restore_saved() -> int:
    """At start: puts back what a previous run left changed (if it was closed abruptly)."""
    try:
        saved = json.loads(HELD_BACK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    with _held_lock:
        for item in saved if isinstance(saved, list) else []:
            try:
                h = _Held(int(item["pid"]), str(item["name"]), float(item["created"]),
                          item.get("priority"), item.get("throttle"), item.get("io"),
                          item.get("memory"), item.get("cores"), bool(item.get("boosted", False)),
                          gpu=item.get("gpu"))
            except (KeyError, TypeError, ValueError):
                continue
            _held[h.pid] = h
    return restore_all()


# ------------------------------------------------------------ Focus mode's boost

def boost(pid: int) -> str:
    """Focus mode: the app in front runs at Above normal priority. Only an app at Normal
    is raised (one set lower or higher on purpose keeps its own), never a protected one
    or another account's. Returns "boosted" (read back from Windows), "kept" (it keeps
    its own priority), "protected" or "refused" (Windows said no, e.g. anti-cheat)."""
    if not IS_WINDOWS:
        return "protected"
    try:
        proc = psutil.Process(pid)
        if safety.is_protected(proc.name()) or not _is_own_account(pid):
            return "protected"
        with _held_lock:
            held = _held.get(pid)
            if held is not None and held.boosted and held.created == proc.create_time():
                return "boosted"
        if proc.nice() != _NORMAL:
            return "kept"
        proc.nice(_ABOVE_NORMAL)
        if proc.nice() != _ABOVE_NORMAL:
            return "refused"
        with _held_lock:
            held = _hold(proc)
            if held.priority is None:
                held.priority = _NORMAL
            held.boosted = True
            _save_held()
        return "boosted"
    except psutil.AccessDenied:
        return "refused"
    except (psutil.Error, OSError):
        return "protected"


def unboost(pid: int) -> None:
    """The boosted app left the front: it gets its own priority back."""
    with _held_lock:
        held = _held.get(pid)
        if held is None or not held.boosted:
            return
    _release(held, ("priority",))
    with _held_lock:
        _save_held()


# ------------------------------------------------------------ apply

def _apply(action: OptimizationAction, extra_denylist: list[str] | None) -> ActionLogEntry:
    """Validates + executes (or simulates) an action, always returning a log entry."""
    target, reason = action.text("target"), action.text("reason")
    try:
        safety.validate_action(action, extra_denylist)
    except safety.SafetyViolation as e:
        return ActionLogEntry(
            action=action.action, target=target, reason=reason,
            applied=False, result=tx("blocked_by_safety: {reason}", reason=i18n.message(e)),
        )

    if action.dry_run or not IS_WINDOWS:
        return ActionLogEntry(
            action=action.action, target=target, reason=reason,
            applied=False,
            result=tx("simulated (dry_run)") if action.dry_run else tx("simulated (not Windows)"),
        )

    try:
        result = _execute(action)
        return ActionLogEntry(
            action=action.action, target=target, reason=reason,
            applied=not result.startswith(NOT_FOUND), result=result,  # nothing found: nothing changed
        )
    except Exception as e:  # never let the main loop crash
        return ActionLogEntry(
            action=action.action, target=target, reason=reason,
            applied=False, result=tx("error: {error}", error=i18n.message(e)),
        )


def _find_processes_by_name(name: str) -> list[psutil.Process]:
    """Every instance of the app that runs under OptiMind's own account.
    Same-named processes of services or other accounts (e.g. a system
    dllhost.exe next to yours) are left alone, even with administrator rights."""
    found = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == name.lower()]
    ours = [p for p in found if _is_own_account(p.pid)]
    if found and not ours:
        raise PermissionError(tx("not changed, it {why}", why=OTHER_ACCOUNT))
    return ours


def _targets(action: OptimizationAction) -> list[psutil.Process]:
    """The instances to change: the ones the engine picked (if they're still
    running, still that app and yours), or every instance of the app you own."""
    if not action.pids:
        return _find_processes_by_name(action.target)
    procs = []
    for pid in action.pids:
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() == action.target.lower() and _is_own_account(pid):
                procs.append(proc)
        except psutil.Error:
            continue
    return procs


def _execute(action: OptimizationAction) -> str:
    if action.action == ActionType.LOWER_PRIORITY:
        return _lower_priority(_targets(action))
    if action.action == ActionType.RAISE_PRIORITY:
        return _restore(action.target, "priority")
    if action.action == ActionType.EFFICIENCY_MODE_ON:
        return _efficiency_on(_targets(action))
    if action.action == ActionType.EFFICIENCY_MODE_OFF:
        return _restore(action.target, "throttle")
    if action.action == ActionType.LOWER_IO_PRIORITY:
        return _lower_io(_targets(action))
    if action.action == ActionType.RESTORE_IO_PRIORITY:
        return _restore(action.target, "io")
    if action.action == ActionType.RESTORE_APP:
        return restore_app(action.target)
    if action.action == ActionType.CONFINE_CORES:
        return _confine_cores(_targets(action))
    if action.action == ActionType.RESTORE_CORES:
        return _restore(action.target, "cores")
    if action.action == ActionType.LOWER_GPU_PRIORITY:
        return _lower_gpu_priority(_targets(action))
    if action.action == ActionType.RESTORE_GPU_PRIORITY:
        return _restore(action.target, "gpu")
    if action.action == ActionType.SET_AFFINITY:
        return _set_affinity(action.target)
    if action.action == ActionType.TRIM_WORKING_SET:
        return _trim_working_set(_targets(action))
    if action.action == ActionType.PURGE_STANDBY_LIST:
        return _purge_standby_list()
    if action.action == ActionType.COMBINE_MEMORY:
        return _combine_memory_pages()
    if action.action == ActionType.DISABLE_STARTUP_ITEM:
        from app.tools import startup_manager

        return startup_manager.disable_startup_item(action.target)
    if action.action == ActionType.ENABLE_STARTUP_ITEM:
        from app.tools import startup_manager

        return startup_manager.enable_startup_item(action.target)
    if action.action == ActionType.SWITCH_POWER_PLAN:
        return _switch_power_plan(action.target)
    if action.action == ActionType.GAME_OPTIMIZE:
        return _game_optimize(action.target)
    if action.action == ActionType.GAME_OPTIMIZE_RESTORE:
        return _game_optimize_restore(action.target)
    raise ValueError(f"Unknown action: {action.action}")


def _skipped(denied: int) -> i18n.Text | None:
    return txn(denied, "{n} skipped: no permission", "{n} skipped: no permission") if denied else None


def _summary(done: i18n.Text, changed: int, left: int, left_why: i18n.Text, denied: int) -> i18n.Text:
    parts = [txn(changed, "{done} for {n} instance (checked)", "{done} for {n} instances (checked)", done=done) if changed
             else tx("nothing changed: {why}", why=left_why)]
    if changed and left:
        parts.append(txn(left, "{n} left as it was ({why})", "{n} left as they were ({why})", why=left_why))
    parts.append(_skipped(denied))
    return i18n.joined(parts, "; ")


def _lower_priority(procs: list[psutil.Process]) -> str:
    if not procs:
        return _GONE
    changed = left = denied = 0
    for p in procs:
        try:
            before = p.nice()
            if before not in _LOWERABLE_PRIORITIES:
                left += 1
                continue
            p.nice(_BELOW_NORMAL)
            if p.nice() != _BELOW_NORMAL:
                denied += 1
                continue
            with _held_lock:
                held = _hold(p)
                if held.priority is None:
                    held.priority = before
                held.boosted = False  # a hold-back now, even if Focus mode had boosted it (its original is kept)
        except psutil.AccessDenied:
            denied += 1
            continue
        except psutil.NoSuchProcess:
            continue
        changed += 1
    if changed:
        with _held_lock:
            _save_held()
    if not changed and denied:
        raise _not_changed(procs)
    return _summary(tx("below normal priority"), changed, left, tx("already low, or set high on purpose"), denied)


def _get_throttle(handle) -> tuple[int, int] | None:
    state = _PowerThrottlingState(_POWER_THROTTLING_CURRENT_VERSION, 0, 0)
    ok = _kernel32().GetProcessInformation(handle, _PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state))
    return (state.ControlMask, state.StateMask) if ok else None


def _throttled(state: tuple[int, int] | None) -> bool:
    return bool(state and state[0] & _POWER_THROTTLING_EXECUTION_SPEED and state[1] & _POWER_THROTTLING_EXECUTION_SPEED)


def _set_throttle_checked(pid: int, control: int, state_mask: int) -> tuple[int, int] | None:
    """Sets the efficiency-mode state and checks Windows kept it. Returns the
    state before the change. Raises PermissionError when it can't be done."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(tx("no permission"))
    try:
        before = _get_throttle(handle)
        wanted = _PowerThrottlingState(_POWER_THROTTLING_CURRENT_VERSION, control, state_mask)
        if not kernel32.SetProcessInformation(handle, _PROCESS_POWER_THROTTLING, ctypes.byref(wanted), ctypes.sizeof(wanted)):
            raise PermissionError(tx("Windows refused"))
        after = _get_throttle(handle)
        if after is not None and _throttled(after) != bool(control & state_mask & _POWER_THROTTLING_EXECUTION_SPEED):
            raise PermissionError(tx("Windows didn't keep the efficiency setting"))
        return before
    finally:
        kernel32.CloseHandle(handle)


def _efficiency_on(procs: list[psutil.Process]) -> str:
    """Windows 11 Efficiency mode (EcoQoS) on; the app's own setting is kept to put back."""
    if not procs:
        return _GONE
    kernel32 = _kernel32()
    changed = left = denied = 0
    for p in procs:
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, p.pid)
        current = None
        if handle:
            try:
                current = _get_throttle(handle)
            finally:
                kernel32.CloseHandle(handle)
        if _throttled(current):
            left += 1  # the app (e.g. a browser for its background tabs) already did it
            continue
        speed = _POWER_THROTTLING_EXECUTION_SPEED
        try:
            before = _set_throttle_checked(p.pid, speed, speed)
            with _held_lock:
                held = _hold(p)
                if held.throttle is None:
                    held.throttle = list(before or (0, 0))  # (0, 0) = "let Windows decide"
        except PermissionError:
            denied += 1
            continue
        except psutil.NoSuchProcess:
            continue
        changed += 1
    if changed:
        with _held_lock:
            _save_held()
    if not changed and denied:
        raise _not_changed(procs)
    return _summary(tx("efficiency mode on"), changed, left, tx("already in efficiency mode"), denied)


def _lower_io(procs: list[psutil.Process]) -> str:
    if not procs:
        return _GONE
    changed = left = denied = 0
    for p in procs:
        try:
            before = int(p.ionice())
            if before <= _IO_LOW:
                left += 1
                continue
            p.ionice(_IO_LOW)
            if int(p.ionice()) != _IO_LOW:
                denied += 1
                continue
            with _held_lock:
                held = _hold(p)
                if held.io is None:
                    held.io = before
        except psutil.AccessDenied:
            denied += 1
            continue
        except psutil.NoSuchProcess:
            continue
        changed += 1
    if changed:
        with _held_lock:
            _save_held()
    if not changed and denied:
        raise _not_changed(procs)
    return _summary(tx("low I/O priority"), changed, left, tx("already low"), denied)


def _set_affinity(process_name: str) -> str:
    """Restricts a noisy background process to half of the cores,
    leaving the remaining cores freer for the process in focus.
    (Available through the API only; the agent doesn't use it on its own.)"""
    total_cores = psutil.cpu_count(logical=True) or 1
    half = max(1, total_cores // 2)
    procs = _find_processes_by_name(process_name)
    if not procs:
        return tx("process not found")
    changed = denied = 0
    for p in procs:
        try:
            p.cpu_affinity(list(range(half)))
            changed += 1
        except psutil.AccessDenied:
            denied += 1
        except (psutil.NoSuchProcess, NotImplementedError):
            continue
    if not changed and denied:
        raise _not_changed(procs)
    return i18n.joined([txn(changed, "{n} instance restricted to {half}/{total} cores", "{n} instances restricted to {half}/{total} cores",
                            half=half, total=total_cores), _skipped(denied)], "; ")


def _get_memory_priority(handle) -> int | None:
    info = _MemoryPriority(0)
    ok = _kernel32().GetProcessInformation(handle, _PROCESS_MEMORY_PRIORITY, ctypes.byref(info), ctypes.sizeof(info))
    return info.MemoryPriority if ok else None


def _set_memory_priority_checked(pid: int, value: int) -> int | None:
    """Sets the app's memory priority and checks Windows kept it. Returns the one before.
    Raises PermissionError when it can't be done."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(tx("no permission"))
    try:
        before = _get_memory_priority(handle)
        info = _MemoryPriority(value)
        if not kernel32.SetProcessInformation(handle, _PROCESS_MEMORY_PRIORITY, ctypes.byref(info), ctypes.sizeof(info)):
            raise PermissionError(tx("Windows refused"))
        after = _get_memory_priority(handle)
        if after is not None and after != value:
            raise PermissionError(tx("Windows didn't keep the memory priority"))
        return before
    finally:
        kernel32.CloseHandle(handle)


def _lower_memory_priority(proc: psutil.Process) -> bool:
    """A compressed app gets Low memory priority until it's used again: if RAM runs
    short, Windows trims its pages before those of the app in front. An app already
    at Low or below (its own choice) is left as it is. True when lowered."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, proc.pid)
    if not handle:
        return False
    try:
        current = _get_memory_priority(handle)
    finally:
        kernel32.CloseHandle(handle)
    if current is None or current <= _MEMORY_PRIORITY_LOW:
        return False
    try:
        before = _set_memory_priority_checked(proc.pid, _MEMORY_PRIORITY_LOW)
        with _held_lock:
            held = _hold(proc)
            if held.memory is None:
                held.memory = current if before is None else before
    except (PermissionError, psutil.Error):
        return False
    return True


def _trim_working_set(procs: list[psutil.Process]) -> str:
    """Moves the app's memory out of RAM and lowers its memory priority; reports how
    much Windows really freed."""
    if not procs:
        return _GONE
    kernel32, psapi = _kernel32(), _psapi()
    trimmed = denied = lowered = 0
    freed = 0.0
    for p in procs:
        try:
            before = p.memory_info().rss
        except psutil.Error:
            continue
        handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_SET_QUOTA, False, p.pid)
        if not handle:
            denied += 1
            continue
        try:
            ok = psapi.EmptyWorkingSet(handle)
        finally:
            kernel32.CloseHandle(handle)
        if not ok:
            denied += 1
            continue
        try:
            freed += max(0, before - p.memory_info().rss)
        except psutil.Error:
            freed += before  # it closed meanwhile
        trimmed += 1
        lowered += _lower_memory_priority(p)
    if lowered:
        with _held_lock:
            _save_held()
    if not trimmed and denied:
        raise _not_changed(procs)
    # (the impact summary and the Agent page read the MB back from the English: "freed 530 MB")
    return i18n.joined([
        txn(trimmed, "freed {mb} MB of RAM from {n} instance (checked; Windows compresses or caches it until the app needs it)",
            "freed {mb} MB of RAM from {n} instances (checked; Windows compresses or caches it until the app needs it)",
            mb=i18n.num(freed / _MB, ".0f")),
        tx("memory priority Low until you use it again") if lowered else None, _skipped(denied)], "; ")


# ------------------------------------------------------------ CPU sets (Maximum focus mode)

@functools.lru_cache(maxsize=1)
def cpu_sets() -> tuple[_CpuSet, ...]:
    """Windows' CPU sets of the first processor group: one per logical processor, with
    its core and efficiency class (GetSystemCpuSetInformation)."""
    if not IS_WINDOWS:
        return ()
    from ctypes import wintypes

    kernel32 = _kernel32()
    needed = wintypes.ULONG()
    kernel32.GetSystemCpuSetInformation(None, 0, ctypes.byref(needed), None, 0)
    if not needed.value:
        return ()
    buffer = ctypes.create_string_buffer(needed.value)
    if not kernel32.GetSystemCpuSetInformation(buffer, needed.value, ctypes.byref(needed), None, 0):
        return ()
    sets, offset, size = [], 0, ctypes.sizeof(_CpuSet)
    while offset + size <= needed.value:
        entry = _CpuSet.from_buffer_copy(buffer, offset)
        if not entry.Size:
            break
        if entry.Type == 0 and entry.Group == 0:  # CpuSetInformation, first processor group
            sets.append(entry)
        offset += entry.Size
    return tuple(sets)


@functools.lru_cache(maxsize=1)
def background_cores() -> tuple[int, ...]:
    """The CPU sets background work is kept on while a game is in focus (Maximum focus
    mode): the efficiency cores of a hybrid CPU, otherwise the last physical core with
    both of its threads. Empty when there is nothing to spare (a single core)."""
    sets = cpu_sets()
    cores = {s.CoreIndex for s in sets}
    if len(cores) < 2:
        return ()
    classes = {s.EfficiencyClass for s in sets}
    if len(classes) > 1:  # hybrid CPU: the efficiency cores (lowest class), where EcoQoS work goes anyway
        chosen = [s for s in sets if s.EfficiencyClass == min(classes)]
    else:
        chosen = [s for s in sets if s.CoreIndex == max(cores)]
    return tuple(s.Id for s in chosen)


def _get_cores(handle) -> list[int] | None:
    from ctypes import wintypes

    ids = (wintypes.ULONG * 64)()
    count = wintypes.ULONG()
    if not _kernel32().GetProcessDefaultCpuSets(handle, ids, 64, ctypes.byref(count)):
        return None
    return list(ids[: count.value])


def _set_cores_checked(pid: int, ids: list[int]) -> list[int] | None:
    """Sets the app's default CPU sets ([] = every core again) and checks Windows kept
    them. Returns the ones before. Raises PermissionError when it can't be done."""
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_SET_LIMITED_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(tx("no permission"))
    try:
        before = _get_cores(handle)
        array = (wintypes.ULONG * len(ids))(*ids) if ids else None
        if not kernel32.SetProcessDefaultCpuSets(handle, array, len(ids)):
            raise PermissionError(tx("Windows refused"))
        after = _get_cores(handle)
        if after is not None and sorted(after) != sorted(ids):
            raise PermissionError(tx("Windows didn't keep the CPU sets"))
        return before
    finally:
        kernel32.CloseHandle(handle)


def _confine_cores(procs: list[psutil.Process]) -> str:
    """Maximum focus mode: a background app's threads run only on the background cores
    while a game is in focus, so the game's cores (and their caches) aren't shared."""
    if not procs:
        return _GONE
    ids = list(background_cores())
    if not ids:
        raise ValueError(tx("not changed: this CPU has no core to spare for background work"))
    kernel32 = _kernel32()
    changed = left = denied = 0
    for p in procs:
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, p.pid)
        current = None
        if handle:
            try:
                current = _get_cores(handle)
            finally:
                kernel32.CloseHandle(handle)
        if current:
            left += 1  # the app picked its own cores: left as it is
            continue
        try:
            before = _set_cores_checked(p.pid, ids)
            with _held_lock:
                held = _hold(p)
                if held.cores is None:
                    held.cores = list(before or [])
        except PermissionError:
            denied += 1
            continue
        except psutil.NoSuchProcess:
            continue
        changed += 1
    if changed:
        with _held_lock:
            _save_held()
    if not changed and denied:
        raise _not_changed(procs)
    total = psutil.cpu_count(logical=True) or len(ids)
    return _summary(tx("kept on {n} of {total} logical processors", n=len(ids), total=total), changed, left,
                    tx("it chose its own cores"), denied)


# ------------------------------------------------------------ GPU priority (a game in focus)

@functools.lru_cache(maxsize=1)
def _gdi32():
    from ctypes import wintypes

    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.D3DKMTGetProcessSchedulingPriorityClass.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
    gdi32.D3DKMTGetProcessSchedulingPriorityClass.restype = ctypes.c_long
    gdi32.D3DKMTSetProcessSchedulingPriorityClass.argtypes = [wintypes.HANDLE, ctypes.c_int]
    gdi32.D3DKMTSetProcessSchedulingPriorityClass.restype = ctypes.c_long
    return gdi32


def _get_gpu_priority(handle) -> int | None:
    """The process's GPU priority class (0-5), or None (no access, or it never used the GPU)."""
    value = ctypes.c_int(-1)
    status = _gdi32().D3DKMTGetProcessSchedulingPriorityClass(handle, ctypes.byref(value))
    return value.value if status == 0 and 0 <= value.value <= 5 else None


def _set_gpu_priority_checked(pid: int, value: int) -> int | None:
    """Sets the app's GPU priority class and checks Windows kept it. Returns the one
    before. Never above Above normal: OptiMind only lowers it, or puts back what the
    app had. Raises PermissionError when it can't be done."""
    if not 0 <= value <= _GPU_ABOVE_NORMAL:
        raise ValueError(tx("OptiMind never sets a GPU priority above Above normal"))
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(tx("no permission"))
    try:
        before = _get_gpu_priority(handle)
        status = _gdi32().D3DKMTSetProcessSchedulingPriorityClass(handle, value) & 0xFFFFFFFF
        if status == _STATUS_INVALID_PARAMETER:
            raise PermissionError(tx("it doesn't use the GPU"))
        if status != 0:
            raise PermissionError(tx("Windows refused (status {status})", status=f"{status:#x}"))
        after = _get_gpu_priority(handle)
        if after is not None and after != value:
            raise PermissionError(tx("Windows didn't keep the GPU priority"))
        return before
    finally:
        kernel32.CloseHandle(handle)


def _lower_gpu_priority(procs: list[psutil.Process]) -> str:
    """A game is in focus: a background app using the GPU gets Below normal GPU priority,
    so the GPU scheduler serves the game first. An app already lower, or set higher on
    purpose (e.g. a recorder at High), is left as it is."""
    if not procs:
        return _GONE
    kernel32 = _kernel32()
    changed = left = denied = 0
    for p in procs:
        handle = kernel32.OpenProcess(_PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION, False, p.pid)
        if not handle:
            denied += 1
            continue
        try:
            current = _get_gpu_priority(handle)
        finally:
            kernel32.CloseHandle(handle)
        if current not in _GPU_LOWERABLE:
            left += 1  # already low, set high on purpose, or it doesn't use the GPU (None)
            continue
        try:
            before = _set_gpu_priority_checked(p.pid, _GPU_BELOW_NORMAL)
            with _held_lock:
                held = _hold(p)
                if held.gpu is None:
                    held.gpu = current if before is None else before
        except PermissionError:
            denied += 1
            continue
        except psutil.NoSuchProcess:
            continue
        changed += 1
    if changed:
        with _held_lock:
            _save_held()
    if not changed and denied:
        raise _not_changed(procs)
    return _summary(tx("Below normal GPU priority"), changed, left, tx("already low, set high on purpose, or not using the GPU"),
                    denied)


# ------------------------------------------------------------ system memory

@functools.lru_cache(maxsize=1)
def _ntdll():
    ntdll = ctypes.WinDLL("ntdll.dll")
    ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    ntdll.NtSetSystemInformation.restype = ctypes.c_long
    return ntdll


def _system_memory_privilege(what: i18n.Text) -> None:
    if not is_elevated():
        raise PermissionError(tx("not changed, {what} needs OptiMind to run as administrator", what=what))
    try:
        enable_privilege("SeProfileSingleProcessPrivilege")
    except Exception as e:
        raise PermissionError(tx("not changed, couldn't get the required privilege: {error}", error=i18n.message(e))) from e


def _refused_status(status: int) -> PermissionError:
    return PermissionError(tx("not changed, Windows returned status {status}", status=f"{status & 0xFFFFFFFF:#x}"))


def _combine_memory_pages() -> str:
    """Windows merges memory pages with identical content (across every app) into one
    shared copy; a page gets its own copy back the moment an app writes to it. Reports
    how much Windows says it merged."""
    _system_memory_privilege(tx("merging memory pages"))
    info = _CombineInformation(None, 0, 0)
    started = time.monotonic()
    status = _ntdll().NtSetSystemInformation(_SYSTEM_COMBINE_PHYSICAL_MEMORY_INFORMATION, ctypes.byref(info),
                                             ctypes.sizeof(info))
    if status != 0:
        raise _refused_status(status)
    merged = info.PagesCombined * _PAGE_SIZE / _MB
    return txn(info.PagesCombined,
               "merged {mb} MB of identical memory pages into shared copies (checked: Windows combined {n} page in {s}s)",
               "merged {mb} MB of identical memory pages into shared copies (checked: Windows combined {n} pages in {s}s)",
               mb=i18n.num(merged), s=i18n.num(time.monotonic() - started, ".1f"))


def _purge_standby_list() -> str:
    """
    Empties the Windows memory standby list ("cached" RAM that shows
    up as used but can be freed instantly). Reports the free memory gained.
    Requires an elevated process (Run as administrator).
    """
    _system_memory_privilege(tx("purging the standby list"))
    before = memory.lists_mb()
    SystemMemoryListInformation = 0x50
    MemoryPurgeStandbyList = 4

    command = ctypes.c_int(MemoryPurgeStandbyList)
    status = _ntdll().NtSetSystemInformation(
        SystemMemoryListInformation, ctypes.byref(command), ctypes.sizeof(command)
    )
    if status != 0:
        raise _refused_status(status)
    after = memory.lists_mb()
    if before and after:
        return tx("standby cache purged: free memory {before} MB -> {after} MB (checked)",
                  before=i18n.num(before["free"]), after=i18n.num(after["free"]))
    return tx("standby cache purged")


def active_power_plan_guid() -> str | None:
    """GUID of the active power plan (registry: instant, no extra process)."""
    if not IS_WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _POWER_SCHEMES_KEY) as key:
            return str(winreg.QueryValueEx(key, "ActivePowerScheme")[0]).lower()
    except OSError:
        return None


def _switch_power_plan(plan_name: str) -> str:
    guid = POWER_PLAN_GUIDS.get(plan_name)
    if not guid:
        raise ValueError(tx("unknown power plan '{plan}'", plan=plan_name))
    done = subprocess.run(["powercfg", "/setactive", guid], capture_output=True, text=True, timeout=10,
                          creationflags=_NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError(tx("not changed: the '{plan}' plan isn't available on this PC", plan=PLAN_NAMES[plan_name]))
    active = active_power_plan_guid()
    if active is not None and active != guid:
        raise RuntimeError(tx("not changed: Windows kept the previous plan"))
    return tx("power plan changed to {plan} (checked)", plan=PLAN_NAMES[plan_name])


# ------------------------------------------------------------ OptiMind itself

def lighten_self() -> None:
    """OptiMind's own background work yields to your apps: efficiency mode
    (EcoQoS), low I/O priority and Low memory priority (Windows trims its
    memory before your apps'). Its CPU priority stays Normal, so it can
    still step in while a background app is hogging the CPU."""
    if not IS_WINDOWS:
        return
    try:
        psutil.Process().ionice(_IO_LOW)
    except (psutil.Error, OSError):
        pass
    kernel32 = _kernel32()
    speed = _POWER_THROTTLING_EXECUTION_SPEED
    state = _PowerThrottlingState(_POWER_THROTTLING_CURRENT_VERSION, speed, speed)
    kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING, ctypes.byref(state),
                                   ctypes.sizeof(state))
    low = _MemoryPriority(_MEMORY_PRIORITY_LOW)
    kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), _PROCESS_MEMORY_PRIORITY, ctypes.byref(low),
                                   ctypes.sizeof(low))


def trim_self() -> None:
    """Hands OptiMind's own idle memory (start-up code, old data) back to Windows."""
    if IS_WINDOWS:
        _psapi().EmptyWorkingSet(_kernel32().GetCurrentProcess())


_self_priority_below = False  # whether OptiMind is currently yielding at Below normal


def adaptive_self_optimize() -> None:
    """Adjusts OptiMind's own priority to the current load, so it never competes
    with the app you're using. On a busy PC it steps back (Below normal CPU priority);
    on an idle PC it steps forward again (Normal priority). Hysteresis (70% down,
    50% up) prevents flapping. Safe to call every cycle — it only changes something
    when the load level has clearly shifted."""
    global _self_priority_below
    if not IS_WINDOWS:
        return
    try:
        load = psutil.cpu_percent(interval=None)
    except psutil.Error:
        return
    try:
        proc = psutil.Process()
        if not _self_priority_below and load >= 70.0:
            # System is busy: OptiMind yields to your apps.
            try:
                proc.nice(_BELOW_NORMAL)
                _self_priority_below = True
            except psutil.Error:
                pass
        elif _self_priority_below and load <= 50.0:
            # System is calm: OptiMind can respond at Normal priority again.
            try:
                proc.nice(_NORMAL)
                _self_priority_below = False
            except psutil.Error:
                pass
    except psutil.Error:
        pass


def _game_optimize(target: str) -> str:
    """Apply one game-GPU optimization: turn on NVIDIA shader cache or Windows
    Game Mode. Checks the result by reading it back from Windows."""
    if not IS_WINDOWS:
        return tx("simulated (not Windows)")
    if target == "nvidia_shader_cache":
        try:
            return gpu_optimization.set_shader_cache(True)
        except OSError as e:
            return str(e)
    if target == "windows_game_mode":
        try:
            return gpu_optimization.set_game_mode(True)
        except OSError as e:
            return str(e)
    return tx("unknown game optimization '{target}'", target=target)


def _game_optimize_restore(target: str) -> str:
    """Put one game-GPU optimization back the way it was (off)."""
    if not IS_WINDOWS:
        return tx("simulated (not Windows)")
    if target == "nvidia_shader_cache":
        try:
            return gpu_optimization.set_shader_cache(False)
        except OSError as e:
            return str(e)
    if target == "windows_game_mode":
        try:
            return gpu_optimization.set_game_mode(False)
        except OSError as e:
            return str(e)
    return tx("unknown game optimization '{target}'", target=target)


def apply_actions(
    actions: list[OptimizationAction], extra_denylist: list[str] | None = None
) -> list[ActionLogEntry]:
    """Entry point used by the orchestrator: validates, executes (or
    simulates) and logs each action, respecting the per-cycle action limit
    (the orchestrator hands what's past it back to the engine for the next cycle)."""
    entries: list[ActionLogEntry] = []
    for action in safety.within_limit(actions)[0]:
        entry = _apply(action, extra_denylist)
        log_action(entry)
        entries.append(entry)
    return entries
