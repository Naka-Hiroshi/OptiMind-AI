"""
Frame times of a running game, measured with Intel PresentMon (MIT license; its
console app ships with OptiMind as tools/PresentMon.exe, see packaging/get_tools.py).

PresentMon reads the frames every app hands to Windows from Event Tracing for
Windows (the DXGI, D3D9, DXGK and DWM events), so unlike an overlay nothing is
loaded into the game. Starting a trace needs administrator rights (or the
"Performance Log Users" group): OptiMind's agent has them.

Per frame, PresentMon's 2.x metrics (--v2_metrics) give:
  FrameTime  time between the starts of two frames: the frame rate;
  CPUBusy    how long the CPU worked on the frame (CPUWait: how long it waited first);
  GPUBusy    how long the GPU worked on it.

What holds the frame rate back:
  - the graphics card, when GPUBusy fills (almost) the whole frame: it never waited,
    so it's fully used (Intel's "GPU Busy" rule);
  - otherwise the GPU sat idle part of every frame, waiting either for the CPU or for
    a frame cap / V-Sync. A cap holds frames at one steady interval (the monitor's
    refresh rate, or a round number like 60 or 141); a CPU that can't keep up gives
    uneven ones. So steady frame times mean a cap, uneven ones the processor.
"""
from __future__ import annotations

import csv
import logging
import os
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Container, Iterable

from app.config import BASE_DIR, IS_WINDOWS
from app.i18n import tx, txn

logger = logging.getLogger("optimind.frames")

SESSION_NAME = "OptiMind"  # PresentMon's trace session (one capture at a time)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MAX_FRAMES = 60_000  # a 60-second capture at 1,000 fps; anything beyond is dropped
_MIN_FRAMES = 30  # fewer than this says nothing about a game
_GPU_BOUND = 0.85  # GPUBusy at least this share of the frame: the GPU limited it
_STEADY_WITHIN = 0.04  # a frame within ±4% of the median frame time counts as "steady"
_CAPPED_STEADY = 0.75  # this share of steady frames, with the CPU idle part of each frame: a cap
_ROUND_CAPS = (30, 60, 72, 75, 90, 100, 120, 141, 144, 160, 165, 180, 200, 240)
_ACCESS_DENIED = 6  # PresentMon's exit code when it can't start a trace session

# PresentMon 2.x metric columns, then the names its default output uses for the same thing.
_COLUMNS = {
    "pid": ("ProcessID",),
    "swapchain": ("SwapChainAddress",),
    "sync": ("SyncInterval",),
    "tearing": ("AllowsTearing",),
    "mode": ("PresentMode",),
    "frame": ("FrameTime", "MsBetweenAppStart", "MsBetweenPresents"),
    "cpu_busy": ("CPUBusy", "MsCPUBusy"),
    "gpu_busy": ("GPUBusy", "MsGPUBusy"),
    "latency": ("DisplayLatency",),
    "displayed": ("DisplayedTime", "MsBetweenDisplayChange"),
}


class FramesError(Exception):
    """PresentMon couldn't measure (not installed, no rights, the game closed…)."""


@dataclass(frozen=True)
class Frame:
    frame_ms: float
    cpu_busy_ms: float | None
    gpu_busy_ms: float | None
    latency_ms: float | None  # from the start of the frame until it was on screen
    displayed: bool
    sync_interval: int
    tearing: bool
    mode: str
    swapchain: str
    pid: int


def executable() -> Path | None:
    """PresentMon's console app: OPTIMIND_PRESENTMON, or tools\\PresentMon.exe (from source:
    in the project folder; installed: next to OptiMind's own files)."""
    configured = os.environ.get("OPTIMIND_PRESENTMON")
    for path in ([Path(configured)] if configured else []) + [BASE_DIR / "tools" / "PresentMon.exe"]:
        if path.is_file():
            return path
    return None


def available() -> bool:
    return IS_WINDOWS and executable() is not None


# ------------------------------------------------------------ reading the CSV

def _number(row: list[str], index: int | None) -> float | None:
    if index is None or index >= len(row):
        return None
    try:
        value = float(row[index])
    except ValueError:  # "NA": not measured for this frame
        return None
    return value if value == value else None  # NaN


def parse(lines: Iterable[str], pid: int | None = None, pids: Container[int] | None = None) -> list[Frame]:
    """The frames in PresentMon CSV output (header first), optionally of one process only
    (`pid`) or of a group of them (`pids`: a game and its helper processes)."""
    reader = csv.reader(lines)
    header = next(reader, None)
    if not header:
        return []
    names = {name.strip(): i for i, name in enumerate(header)}
    where = {key: next((names[c] for c in candidates if c in names), None) for key, candidates in _COLUMNS.items()}
    if where["frame"] is None:
        raise FramesError("PresentMon's output has no frame time column")
    frames: list[Frame] = []
    for row in reader:
        if len(frames) >= _MAX_FRAMES:
            break
        frame_ms = _number(row, where["frame"])
        row_pid = _number(row, where["pid"])
        if not frame_ms or frame_ms <= 0 or (pid is not None and row_pid != pid) \
                or (pids is not None and int(row_pid or 0) not in pids):
            continue
        displayed_ms = _number(row, where["displayed"])
        frames.append(Frame(
            frame_ms=frame_ms,
            cpu_busy_ms=_number(row, where["cpu_busy"]),
            gpu_busy_ms=_number(row, where["gpu_busy"]),
            latency_ms=_number(row, where["latency"]),
            displayed=bool(displayed_ms),
            sync_interval=int(_number(row, where["sync"]) or 0),
            tearing=_number(row, where["tearing"]) == 1,
            mode=row[where["mode"]].strip() if where["mode"] is not None and where["mode"] < len(row) else "",
            swapchain=row[where["swapchain"]] if where["swapchain"] is not None and where["swapchain"] < len(row) else "",
            pid=int(row_pid or 0),
        ))
    return frames


# ------------------------------------------------------------ what the frames say

def _percentile(ordered: list[float], pct: float) -> float:
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * pct / 100))]


def _near(value: float, target: float, tolerance: float = 0.03) -> bool:
    return target > 0 and abs(value - target) / target <= tolerance


def _cap_kind(fps: float, refresh_hz: float | None, sync: int) -> str | None:
    """What a steady frame rate is pinned to: "vsync" (the monitor's refresh rate, or a
    fraction of it, with V-Sync on), "cap" (just under the refresh rate, where G-SYNC /
    FreeSync players and NVIDIA Reflex cap it, or a round number within 1%: limiters hold
    their target far closer than that), or None."""
    if refresh_hz:
        if sync >= 1 and _near(fps, refresh_hz / sync):
            return "vsync"
        if refresh_hz * 0.9 <= fps <= refresh_hz * 1.01:
            return "cap"
    return "cap" if any(_near(fps, cap, 0.01) for cap in _ROUND_CAPS) else None


def analyze(frames: list[Frame], refresh_hz: float | None = None) -> dict | None:
    """Frame rate, lows, stutter and what limits it, from one capture's frames. Only
    the game's main swap chain counts (a launcher overlay may present on another, and in
    a browser-engine game a helper process presents, not the window's)."""
    if not frames:
        return None
    chain = Counter((f.pid, f.swapchain) for f in frames).most_common(1)[0][0]
    frames = [f for f in frames if (f.pid, f.swapchain) == chain]
    if len(frames) < _MIN_FRAMES:
        return None
    times = sorted(f.frame_ms for f in frames)
    count, total_ms = len(times), sum(times)
    median = _percentile(times, 50)
    fps = count * 1000 / total_ms
    sync = Counter(f.sync_interval for f in frames).most_common(1)[0][0]
    steady = sum(1 for t in times if abs(t - median) <= median * _STEADY_WITHIN) / count

    timed = [f for f in frames if f.gpu_busy_ms is not None]
    gpu_frames = sum(1 for f in timed if f.gpu_busy_ms >= _GPU_BOUND * f.frame_ms)
    gpu_share = gpu_frames / len(timed) if timed else 0.0
    gpu_busy = sum(min(1.0, f.gpu_busy_ms / f.frame_ms) for f in timed) / len(timed) if timed else None
    with_cpu = [f for f in frames if f.cpu_busy_ms is not None]
    cpu_busy = sum(min(1.0, f.cpu_busy_ms / f.frame_ms) for f in with_cpu) / len(with_cpu) if with_cpu else None

    # The frames the GPU didn't limit waited for the CPU or for a cap: a cap keeps them steady, at a
    # rate a cap sets, and (unless the limiter waits inside the frame) leaves the CPU idle part of it.
    cap = _cap_kind(1000 / median, refresh_hz, sync) if steady >= _CAPPED_STEADY else None
    cpu_idle = cpu_busy is not None and cpu_busy <= 0.75
    capped = cap is not None and (cap == "vsync" or cpu_idle or steady >= 0.95)
    if timed:
        rest = 1.0 - gpu_share
        shares = {"gpu": gpu_share, "cpu": 0.0 if capped else rest, "cap": rest if capped else 0.0}
        limiter = max(shares, key=lambda k: shares[k])
    else:  # no GPU timing (PresentMon couldn't track this GPU): only a cap can be told apart
        shares = {"gpu": 0.0, "cpu": 0.0, "cap": 1.0} if capped else {}
        limiter = "cap" if capped else "unknown"

    latencies = [f.latency_ms for f in frames if f.latency_ms]
    modes = Counter(f.mode for f in frames if f.mode)
    return {
        "frames": count,
        "seconds": round(total_ms / 1000, 1),
        "fps": round(fps, 1),
        "low_1_fps": round(1000 / _percentile(times, 99), 1),  # "1% low": the 99th-percentile frame time
        "low_01_fps": round(1000 / _percentile(times, 99.9), 1),
        "frame_ms": {"median": round(median, 2), "p99": round(_percentile(times, 99), 2),
                     "max": round(times[-1], 2)},
        "stutter_share": round(sum(1 for t in times if t > 2 * median) / count, 4),  # frames over twice the median
        "steady_share": round(steady, 3),
        "limiter": limiter,  # "gpu", "cpu", "cap" or "unknown" (no GPU timing on this PC)
        "shares": {k: round(v, 3) for k, v in shares.items()},
        "cap": ({"kind": cap, "fps": round(1000 / median)} if capped else None),
        "gpu_busy": None if gpu_busy is None else round(gpu_busy, 3),
        "cpu_busy": None if cpu_busy is None else round(cpu_busy, 3),
        "latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "displayed_share": round(sum(1 for f in frames if f.displayed) / count, 3),
        "present_mode": modes.most_common(1)[0][0] if modes else None,
        "sync_interval": sync,
        "tearing": Counter(f.tearing for f in frames).most_common(1)[0][0],
    }


# ------------------------------------------------------------ running PresentMon

class Capture:
    """One PresentMon run: a live game (pid, for `seconds`) or a recorded trace (etl_file,
    used by the tests). A thread reads the CSV as PresentMon writes it; nothing touches
    the disk. `done` is set when it ends; then `result` (analyze()) or `error` is set.

    A game built on a browser engine (Electron, NW.js: many RPG Maker games) draws its
    frames in a helper process, not in the one that owns its window. Given the program
    names of the game and its helpers (`names`) and which processes belong to it (`pids`),
    the capture follows all of them and analyze() keeps the one that presents the frames."""

    def __init__(self, pid: int | None = None, seconds: float = 30.0, refresh_hz: float | None = None,
                 etl_file: str | os.PathLike | None = None, name: str = "", names: Iterable[str] = (),
                 pids: Container[int] | None = None):
        if pid is None and etl_file is None:
            raise ValueError("a process or a trace file to measure")
        self.pid, self.seconds, self.refresh_hz, self.etl_file, self.name = pid, seconds, refresh_hz, etl_file, name
        self.names = sorted({n for n in names if n})
        self.pids = pids if self.names else None
        self.started = 0.0
        self.finished: float | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.done = threading.Event()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def command(self, exe: Path) -> list[str]:
        args = [str(exe)]
        if self.etl_file is not None:
            args += ["--etl_file", str(self.etl_file)]
        else:
            targets = [arg for name in self.names for arg in ("--process_name", name)] if self.names \
                else ["--process_id", str(self.pid)]
            args += targets + ["--timed", str(max(1, round(self.seconds))), "--terminate_after_timed",
                               "--terminate_on_proc_exit"]
        return args + ["--output_stdout", "--no_console_stats", "--v2_metrics", "--no_track_input",
                       "--session_name", SESSION_NAME, "--stop_existing_session"]

    def start(self) -> Capture:
        exe = executable()
        if exe is None:
            self._finish(error=tx("PresentMon isn't installed with OptiMind"))
            return self
        self.started = time.time()
        try:
            self._process = subprocess.Popen(self.command(exe), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, encoding="utf-8", errors="replace", bufsize=1,
                                             creationflags=_NO_WINDOW, cwd=str(exe.parent))
        except OSError as e:
            self._finish(error=tx("couldn't start PresentMon ({error})", error=e.strerror or str(e)))
            return self
        threading.Thread(target=self._read, name="optimind-presentmon", daemon=True).start()
        return self

    def running(self) -> bool:
        return self._process is not None and not self.done.is_set()

    def stop(self) -> None:
        """Ends the capture early (the game left the focus, OptiMind quits); what was
        measured so far is still analyzed."""
        self._cancelled = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def progress(self) -> float:
        if self.done.is_set():
            return 1.0
        if not self.started or self.etl_file is not None:
            return 0.0
        return min(0.99, (time.time() - self.started) / max(1.0, self.seconds + 2))

    def _read(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        error_lines: list[str] = []

        def drain_stderr() -> None:  # read alongside, so a chatty stderr can't block PresentMon
            for line in process.stderr:
                if len(error_lines) < 20:
                    error_lines.append(line.strip())

        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()
        try:
            frames = parse(process.stdout, None if self.pids is not None else self.pid, pids=self.pids)
        except FramesError as e:
            frames = []
            error_lines.insert(0, str(e))
        for _ in process.stdout:  # anything after the frame limit
            pass
        code = process.wait()
        reader.join(2.0)
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        if code == _ACCESS_DENIED:
            self._finish(error=tx("measuring frames needs OptiMind to run as administrator"))
        elif not frames:
            why = next((line for line in error_lines if line.lower().startswith("error")), None)  # PresentMon's words
            self._finish(error=why or tx("no frames: OptiMind saw none from the game while it measured (it was minimized, "
                                         "on a loading screen or closed, or it draws them from a program OptiMind can't "
                                         "follow)"))
        else:
            result = analyze(frames, self.refresh_hz)
            self._finish(result=result, error=None if result else txn(
                len(frames), "only {n} frame: too few to say anything", "only {n} frames: too few to say anything"))

    def _finish(self, result: dict | None = None, error: str | None = None) -> None:
        self.result, self.error = result, error
        self.finished = time.time()
        if result is not None:
            result["interrupted"] = self._cancelled
        self.done.set()

    def status(self) -> dict:
        return {"name": self.name, "pid": self.pid, "seconds": self.seconds, "started": self.started,
                "progress": round(self.progress(), 2), "done": self.done.is_set(), "error": self.error}
