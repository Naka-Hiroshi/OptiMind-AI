"""
The graphics card's own sensors, through NVIDIA's management library (NVML: the
nvml.dll every NVIDIA driver installs; Python bindings: nvidia-ml-py). Temperature,
clocks, power against the card's power limit, fan, video memory, PCIe link, and the
reasons the card is holding its clocks back right now ("clocks event reasons").

Read-only: OptiMind changes no GPU setting. An overclock has to be tested on each
card for stability, which is what NVIDIA App's automatic tuning does (see the
Hardware check), so OptiMind points there instead.

Other graphics cards: Windows' own counters only (app/system/sysinfo.py).
"""
from __future__ import annotations

import logging
import threading
import time

from app.config import IS_WINDOWS

logger = logging.getLogger("optimind.gpu")

# nvmlClocksEventReasons (nvml.h): bit -> the reason, in words
_REASONS = (
    (0x1, "idle", "nothing to do"),
    (0x2, "app_clocks", "an application clock setting"),
    (0x4, "power", "its power limit"),
    (0x8, "slowdown", "a hardware slowdown (power supply or temperature protection)"),
    (0x10, "sync_boost", "boosting in sync with another GPU"),
    (0x20, "thermal", "its temperature"),
    (0x40, "thermal", "its temperature (hardware protection)"),
    (0x80, "power_brake", "the power supply's brake signal"),
    (0x100, "display", "the display clock"),
)
_LOCK = threading.Lock()
_CACHE_SECONDS = 1.0
# Asking the driver why it holds the clocks back takes ~16 ms (every other sensor ~1 ms or less),
# and the reasons change slowly: they're asked at most this often.
_REASONS_SECONDS = 4.0
_nvml = None  # the pynvml module once NVML started, False when it can't
_handles: list = []
_cache: tuple[float, list[dict]] | None = None
_reasons_cache: dict[int, tuple[float, int]] = {}  # GPU index -> (when, mask)


def reasons(mask: int) -> list[str]:
    """The clocks event reason codes in a bit mask ("idle", "power", "thermal"…), no repeats."""
    out: list[str] = []
    for bit, code, _ in _REASONS:
        if mask & bit and code not in out:
            out.append(code)
    return out


def _start() -> bool:
    global _nvml, _handles
    if _nvml is not None:
        return bool(_nvml)
    if not IS_WINDOWS:
        _nvml = False
        return False
    try:
        import pynvml

        pynvml.nvmlInit()
        _handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(pynvml.nvmlDeviceGetCount())]
        _nvml = pynvml
    except Exception as e:  # no NVIDIA driver (or bindings): not an error, just nothing to read
        logger.debug("NVML unavailable: %s", e)
        _nvml, _handles = False, []
    return bool(_nvml)


def available() -> bool:
    with _LOCK:
        return _start() and bool(_handles)


def _try(call, *args, default=None):
    try:
        return call(*args)
    except Exception:  # NVML says "not supported" for sensors a card doesn't have
        return default


def _read_one(n, h, index: int) -> dict:
    name = _try(n.nvmlDeviceGetName, h, default="NVIDIA GPU")
    memory = _try(n.nvmlDeviceGetMemoryInfo, h)
    utilization = _try(n.nvmlDeviceGetUtilizationRates, h)
    known = _reasons_cache.get(index)
    if known and time.monotonic() - known[0] < _REASONS_SECONDS:
        mask = known[1]
    else:
        event_reasons = (getattr(n, "nvmlDeviceGetCurrentClocksEventReasons", None)
                         or n.nvmlDeviceGetCurrentClocksThrottleReasons)
        mask = _try(event_reasons, h, default=0) or 0
        _reasons_cache[index] = (time.monotonic(), mask)
    limits = _try(n.nvmlDeviceGetPowerManagementLimitConstraints, h)
    milliwatts = lambda value: None if value is None else round(value / 1000, 1)  # noqa: E731
    return {
        "index": index,
        "name": name.decode() if isinstance(name, bytes) else name,
        "temperature_c": _try(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU),
        "slowdown_c": _try(n.nvmlDeviceGetTemperatureThreshold, h, n.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN),
        "clock_mhz": _try(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_GRAPHICS),
        "max_clock_mhz": _try(n.nvmlDeviceGetMaxClockInfo, h, n.NVML_CLOCK_GRAPHICS),
        "memory_clock_mhz": _try(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_MEM),
        "power_w": milliwatts(_try(n.nvmlDeviceGetPowerUsage, h)),
        "power_limit_w": milliwatts(_try(n.nvmlDeviceGetEnforcedPowerLimit, h)),
        "default_power_limit_w": milliwatts(_try(n.nvmlDeviceGetPowerManagementDefaultLimit, h)),
        "max_power_limit_w": milliwatts(limits[1]) if limits else None,
        "fan_percent": _try(n.nvmlDeviceGetFanSpeed, h),
        "utilization": utilization.gpu if utilization else None,
        "memory_used_mb": round(memory.used / 2 ** 20) if memory else None,
        "memory_total_mb": round(memory.total / 2 ** 20) if memory else None,
        "pstate": _try(n.nvmlDeviceGetPerformanceState, h),
        "pcie": {"gen": _try(n.nvmlDeviceGetCurrPcieLinkGeneration, h),
                 "width": _try(n.nvmlDeviceGetCurrPcieLinkWidth, h),
                 "max_gen": _try(n.nvmlDeviceGetMaxPcieLinkGeneration, h),
                 "max_width": _try(n.nvmlDeviceGetMaxPcieLinkWidth, h)},
        "reasons": reasons(mask),
        "reasons_mask": mask,
    }


def read() -> list[dict]:
    """Every NVIDIA GPU's sensors right now (kept a second, so the dashboard and the
    game tracker share one reading). Empty without an NVIDIA GPU."""
    global _cache
    with _LOCK:
        if not _start() or not _handles:
            return []
        if _cache and time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
        try:
            gpus = [_read_one(_nvml, h, i) for i, h in enumerate(_handles)]
        except Exception:
            logger.debug("NVML read failed", exc_info=True)
            gpus = []
        _cache = (time.monotonic(), gpus)
        return gpus


def driver_version() -> str | None:
    with _LOCK:
        if not _start():
            return None
        version = _try(_nvml.nvmlSystemGetDriverVersion)
        return version.decode() if isinstance(version, bytes) else version


def primary() -> dict | None:
    """The first NVIDIA GPU (the one games use on a PC with one card)."""
    gpus = read()
    return gpus[0] if gpus else None
