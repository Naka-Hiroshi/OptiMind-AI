"""
Windows' memory lists, read with one small performance-counter query:
free (free + zeroed pages), standby (the cache Windows hands out
instantly) and modified (changed pages waiting to be compressed or
written to the page file). Used by the fast loop, and to check what a
standby purge really freed. The same query reads the hard faults: pages
Windows had to read back from disk because they weren't in memory (from
the page file, or from a program's own files, a game's data among them).

The commit charge: how much memory Windows has promised to apps (RAM
plus the page file), against its limit. Near the limit Windows grows the
page file; compressing or trimming memory doesn't lower it, closing apps
does.

Also how well Windows compresses memory: the memory manager's own
statistics for its compression store, the numbers behind Task Manager's
"Compressed memory stores an estimated X MB of data" tooltip. Windows
packs the changed pages of idle apps with its XPRESS compressor;
Microsoft's figure is that compressed data takes about 40% of its
original size, and the real ratio depends on the data (text and app
heaps pack well; images, video and encrypted data hardly at all).
"""
from __future__ import annotations

import ctypes
import functools
import logging
import math
import threading
import time

from app.config import IS_WINDOWS

logger = logging.getLogger("optimind.memory")

_MB = 1024 * 1024
_PAGE = 4096
_PATHS = {
    "free": r"\Memory\Free & Zero Page List Bytes",
    "standby_core": r"\Memory\Standby Cache Core Bytes",
    "standby_normal": r"\Memory\Standby Cache Normal Priority Bytes",
    "standby_reserve": r"\Memory\Standby Cache Reserve Bytes",
    "modified": r"\Memory\Modified Page List Bytes",
}
_PAGES_INPUT = r"\Memory\Pages Input/sec"  # pages read from disk for hard faults (a rate)
_query = None
_counters: dict = {}
_unavailable = False
_lock = threading.Lock()


def lists_mb() -> dict[str, float | None] | None:
    """{"free": MB, "standby": MB, "modified": MB, "hard_faults_mb_s": MB/s}, or None when
    Windows can't say. The hard faults are a rate: None at the first reading."""
    global _query, _counters, _unavailable
    if not IS_WINDOWS or _unavailable:
        return None
    with _lock:
        try:
            import win32pdh

            if _query is None:
                query = win32pdh.OpenQuery()
                counters = {key: win32pdh.AddEnglishCounter(query, path) for key, path in _PATHS.items()}
                try:
                    counters["pages_input"] = win32pdh.AddEnglishCounter(query, _PAGES_INPUT)
                except Exception:
                    pass  # without it the lists still work
                _query, _counters = query, counters
            win32pdh.CollectQueryData(_query)
            raw = {k: win32pdh.GetFormattedCounterValue(_counters[k], win32pdh.PDH_FMT_LARGE)[1] for k in _PATHS}
            try:
                pages = win32pdh.GetFormattedCounterValue(_counters["pages_input"], win32pdh.PDH_FMT_DOUBLE)[1]
            except Exception:
                pages = None  # the first reading of a rate, or no such counter
        except Exception:
            if _query is None:
                _unavailable = True  # no memory counters on this PC: stop asking
            return None
    return {
        "free": raw["free"] / _MB,
        "standby": (raw["standby_core"] + raw["standby_normal"] + raw["standby_reserve"]) / _MB,
        "modified": raw["modified"] / _MB,
        "hard_faults_mb_s": None if pages is None else pages * _PAGE / _MB,
    }


# ------------------------------------------------------------ commit charge

@functools.lru_cache(maxsize=1)
def _performance_information() -> type:
    from ctypes import wintypes

    class _PerformanceInformation(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
            ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
            ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
            ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
            ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
            ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD),
        ]

    return _PerformanceInformation


def performance_info() -> dict:
    """The commit charge (memory promised to apps, against Windows' limit), the kernel's
    memory, handles and threads: GetPerformanceInfo, one cheap call. {} when Windows can't say."""
    if not IS_WINDOWS:
        return {}
    info = _performance_information()()
    info.cb = ctypes.sizeof(info)
    if not ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(info), info.cb):
        return {}
    page_mb = info.PageSize / _MB
    return {
        "commit_used_mb": round(info.CommitTotal * page_mb),
        "commit_limit_mb": round(info.CommitLimit * page_mb),
        "kernel_paged_mb": round(info.KernelPaged * page_mb),
        "kernel_nonpaged_mb": round(info.KernelNonpaged * page_mb),
        "handles": info.HandleCount,
        "threads": info.ThreadCount,
    }


# ------------------------------------------------------------ compression statistics

# NtQuerySystemInformation(SystemStoreInformation) with a MemCompressionInfoRequest,
# the store manager query System Informer uses (layouts from its phnt headers).
_SYSTEM_STORE_INFORMATION = 109
_MEM_COMPRESSION_INFO_REQUEST = 22
_STORE_INFORMATION_VERSION = 1
_STATS_SECONDS = 10.0


class _CompressionInfo(ctypes.Structure):
    """SM_STORE_COMPRESSION_INFORMATION_REQUEST: version 4 (48 bytes) since Windows 11
    24H2, version 3 (40 bytes, without PartitionHandle) before."""
    _fields_ = [
        ("Version", ctypes.c_ulong), ("CompressionPid", ctypes.c_ulong), ("WorkingSetSize", ctypes.c_ulong),
        ("TotalDataCompressed", ctypes.c_size_t), ("TotalCompressedSize", ctypes.c_size_t),
        ("TotalUniqueDataCompressed", ctypes.c_size_t), ("PartitionHandle", ctypes.c_void_p),
    ]


class _StoreInformation(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("StoreInformationClass", ctypes.c_int),
                ("Data", ctypes.c_void_p), ("Length", ctypes.c_ulong)]


_stats: tuple[float, dict | None] = (-_STATS_SECONDS, None)
_stats_unavailable = False
_privilege_on = False


def _query_compression() -> _CompressionInfo | None:
    ntdll = ctypes.WinDLL("ntdll")
    for version, size in ((4, ctypes.sizeof(_CompressionInfo)), (3, _CompressionInfo.PartitionHandle.offset)):
        info = _CompressionInfo(Version=version)
        request = _StoreInformation(_STORE_INFORMATION_VERSION, _MEM_COMPRESSION_INFO_REQUEST,
                                    ctypes.addressof(info), size)
        status = ntdll.NtQuerySystemInformation(_SYSTEM_STORE_INFORMATION, ctypes.byref(request),
                                                ctypes.sizeof(request), None)
        if status == 0:
            return info
    return None


def _interpret(info: _CompressionInfo, store_mb: float | None) -> dict | None:
    """Windows doesn't document the units of these numbers. The compressed size has to
    match the store's own size, so whichever unit (bytes or 4 KB pages) makes it match
    is the one used; anything that still doesn't add up is not shown."""
    reference = store_mb * _MB if store_mb else info.WorkingSetSize * _PAGE
    if not reference or not info.TotalCompressedSize or not info.TotalDataCompressed:
        return None
    unit = min((1, _PAGE), key=lambda u: abs(math.log(info.TotalCompressedSize * u / reference)))
    compressed, data = info.TotalCompressedSize * unit, info.TotalDataCompressed * unit
    ratio = data / compressed
    if not 0.25 <= compressed / reference <= 4 or not 1.0 <= ratio <= 50:
        return None
    return {"data_mb": round(data / _MB), "compressed_mb": round(compressed / _MB),
            "ratio": round(ratio, 2), "saved_mb": round((data - compressed) / _MB)}


def compression_stats(store_mb: float | None) -> dict | None:
    """How much app data Windows keeps in its compressed memory and how well it packs:
    {"data_mb", "compressed_mb", "ratio", "saved_mb"}. `store_mb` is the store's size as
    the process counters see it (the Memory Compression process), used to check the
    answer. Needs administrator rights; None when Windows can't say. Cached briefly."""
    global _stats, _stats_unavailable, _privilege_on
    if not IS_WINDOWS or _stats_unavailable:
        return None
    now = time.monotonic()
    if now - _stats[0] < _STATS_SECONDS:
        return _stats[1]
    from app.system.admin_rights import enable_privilege, is_elevated

    stats = None
    try:
        if not is_elevated():
            _stats_unavailable = True  # Windows only answers administrators
            return None
        if not _privilege_on:
            enable_privilege("SeProfileSingleProcessPrivilege")
            _privilege_on = True
        info = _query_compression()
        if info is None:
            _stats_unavailable = True
            logger.info("Windows doesn't report memory compression statistics on this version")
        else:
            stats = _interpret(info, store_mb)
    except Exception as e:  # never let a statistic break the fast loop
        logger.debug("Compression statistics unavailable: %s", e)
    _stats = (now, stats)
    return stats
