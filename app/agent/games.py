"""
Game sessions: what each game got from the PC while you played, what held it back,
and what to do about it.

A session starts when a game comes to the front (Focus mode's game detection, see
app/agent/focus.py) and ends when the game closes or has stayed in the background
for 5 minutes (an Alt+Tab doesn't end it). While the game is in front, every
fast-loop cycle records the CPU (overall and its busiest core), memory (and the
commit charge against Windows' limit, and what Windows read back from disk), the
game's own disk activity and the graphics card's sensors (app/system/gpu.py).
When memory is fullest, it notes the apps holding the most of it, for the advice.

Frame times come from short PresentMon captures (app/system/frames.py): 30 seconds,
45 seconds after the game comes to the front (past its menus and loading), then every
3 minutes while it stays there, and 60 seconds on demand ("Measure now"). A capture
stops when the game leaves the front: frames of a game in the background say nothing.

At the end the session is summarized (frame rate, 1% lows, what limited it, GPU
temperature, power and throttling, the busiest CPU core) with advice, and saved to
data/game_sessions.jsonl (the last 200 sessions), where your note on it goes too.

Before you play, the Games page lists the apps holding the most memory of their own
and the recorders that recorded your last games (before_play): closing one is your
choice (app/tools/close_app.py).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psutil

from app import i18n
from app.agent import focus
from app.config import DATA_DIR, IS_WINDOWS, settings
from app.i18n import pct, tx, txn
from app.models import SystemSnapshot
from app.system import display, frames, gpu, hardware, memory, monitor, processes
from app.system.admin_rights import is_elevated

logger = logging.getLogger("optimind.games")

SESSIONS_PATH = DATA_DIR / "game_sessions.jsonl"
_KEEP_SESSIONS = 200
_MIN_SESSION_SECONDS = 60.0  # in front for less than this: a launcher or a quick look, not saved
_AWAY_ENDS_SESSION = 300.0
_FIRST_CAPTURE_AFTER = 45.0
_CAPTURE_SECONDS = 30.0
_CAPTURE_EVERY = 180.0
_HISTORY_CAPTURES = 20  # the frame-rate chart while you play (Home, Games page): about the last hour, a capture every 3 minutes
_MEASURE_SECONDS = 60.0
_RETRY_AFTER_ERROR = 600.0
_MAX_TICK = 10.0  # a gap longer than this between two cycles (sleep, a stall) isn't counted as play time
_MB = 1024 * 1024
_RECORDING_PERCENT = 3.0  # an app keeping the GPU's video encoder this busy is recording or streaming
_RECORDING_SHARE = 0.3  # ...for at least this share of the session
_MEMORY_FULL = 90.0  # RAM (or the commit charge, against its limit) this % full: the session names what holds it
_BEFORE_PLAY_APPS = 6  # Before you play: the apps holding the most memory of their own...
_BEFORE_PLAY_MIN_MB = 150.0  # ...at least this much each
# A recorder that recorded one of these last sessions is named there, running or not: SteelSeries GG Moments and
# Discord Clips start their capture with the game, so before you play they don't run yet. Two sessions after you
# turn one off, it's gone.
_RECENT_SESSIONS = 2
_NOTE_CHARS = 120
# Apps that record gameplay in the background, by their process name: what they're called, and where
# their recording is switched off.
_INSTANT_REPLAY = tx("Alt+Z > Instant Replay > Off")
_GAME_BAR = tx("Optimize > Gaming > Turn off background game recording")
_OBS = tx("stop OBS's replay buffer or recording")
_RELIVE = tx("AMD Software > Record & Stream: turn off Instant Replay")
RECORDERS = {
    "steelseriescapturesvc.exe": ("SteelSeries GG Moments",
                                  tx("SteelSeries GG > Moments > Settings: turn off auto-clipping (or Moments)")),
    "nvcontainer.exe": ("NVIDIA Instant Replay", _INSTANT_REPLAY),
    "nvidia share.exe": ("NVIDIA Instant Replay", _INSTANT_REPLAY),
    "nvsphelper64.exe": ("NVIDIA Instant Replay", _INSTANT_REPLAY),
    "gamebar.exe": ("Xbox Game Bar", _GAME_BAR),
    "gamebarftserver.exe": ("Xbox Game Bar", _GAME_BAR),
    "obs64.exe": ("OBS Studio", _OBS),
    "obs32.exe": ("OBS Studio", _OBS),
    "streamlabs obs.exe": ("Streamlabs", tx("stop its replay buffer or recording")),
    "discord.exe": ("Discord", tx("stop the Go Live stream")),
    "discord_clips.exe": ("Discord Clips", tx("Discord > User Settings > Clips: turn clipping off")),
    "medal.exe": ("Medal", tx("Medal > Settings > Recorder: turn off background recording")),
    "outplayed.exe": ("Outplayed (Overwolf)", tx("Outplayed > Settings: turn off auto-capture")),
    "overwolf.exe": ("Overwolf", tx("turn off its auto-capture")),
    "radeonsoftware.exe": ("AMD ReLive", _RELIVE),
    "amdrsserv.exe": ("AMD ReLive", _RELIVE),
    "action.exe": ("Mirillis Action!", tx("stop its recording")),
    "bdcam.exe": ("Bandicam", tx("stop its recording")),
}
# The drive a game is installed on, by the kind hardware.drive_kind() names (the Games page's words too)
_DRIVE_KINDS = {"hard disk": tx("hard disk"), "NVMe SSD": tx("NVMe SSD"), "SSD": tx("SSD"), "USB drive": tx("USB drive")}


@dataclass
class _Stat:
    total: float = 0.0
    count: int = 0
    peak: float | None = None

    def add(self, value: float | None) -> None:
        if value is None:
            return
        self.total += value
        self.count += 1
        self.peak = value if self.peak is None else max(self.peak, value)

    @property
    def avg(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass
class _Session:
    pid: int
    exe: str
    path: str | None
    title: str
    started: float
    front_since: float
    last_front: float
    created: float = 0.0  # the game process's start time (a reused PID is another process)
    play_seconds: float = 0.0
    stats: dict[str, _Stat] = field(default_factory=lambda: {k: _Stat() for k in (
        "cpu", "core", "game_cpu", "ram", "game_mem", "disk", "gpu_util", "gpu_temp", "gpu_clock",
        "gpu_power", "vram", "commit", "faults")})
    commit_peak: tuple[float, float] | None = None  # (committed MB, limit MB) when it was highest
    memory_peak: float = 0.0  # the fullest RAM or commit % seen...
    memory_apps: list[dict] = field(default_factory=list)  # ...and the apps holding the most memory then
    gpu_samples: int = 0
    reasons: Counter = field(default_factory=Counter)
    gpu_info: dict = field(default_factory=dict)
    captures: list[dict] = field(default_factory=list)
    frames_error: str | None = None
    live: dict = field(default_factory=dict)
    samples: int = 0
    recorders: dict[str, _Stat] = field(default_factory=dict)  # apps using the video encoder while you played
    modes: Counter = field(default_factory=Counter)  # Focus mode while you played, per sample


class _CoreMeter:
    """Overall CPU % and the busiest logical processor's %, from its own per-CPU time
    deltas (so it doesn't disturb the dashboard's psutil readings)."""

    def __init__(self) -> None:
        self._last = None

    def read(self) -> tuple[float, float] | None:
        times = psutil.cpu_times(percpu=True)
        last, self._last = self._last, times
        if last is None or len(last) != len(times):
            return None
        busy = []
        for before, after in zip(last, times):
            total = sum(after) - sum(before)
            idle = after.idle - before.idle
            busy.append(0.0 if total <= 0 else max(0.0, min(100.0, 100.0 * (1 - idle / total))))
        return sum(busy) / len(busy), max(busy)


_lock = threading.RLock()
_session: _Session | None = None
_capture: frames.Capture | None = None
_capture_manual = False
_capture_focus_mode: str | None = None
_capture_mode_mixed = False
_next_capture = 0.0
_frames_paused_until = 0.0
_last_tick: float | None = None
_cores = _CoreMeter()
_saved: list[dict] | None = None  # sessions on disk, oldest first (read once)


# ------------------------------------------------------------ names and places

def _match_library(path: str | None) -> dict | None:
    """The installed game (Steam/Epic) whose folder holds this exe, from the library as last
    read (never waits for a disk: the agent's cycle calls this)."""
    if not path:
        return None
    lower = os.path.normcase(path)
    for game in hardware.installed_games_cached() or []:
        folder = game.get("install_path")
        if folder and lower.startswith(os.path.normcase(folder.rstrip("\\/")) + os.sep):
            return game
    return None


def _title(exe: str, path: str | None) -> str:
    """The game's name: its library name, else its exe's product name (the library may not be
    read yet when a session starts; the summary looks again)."""
    game = _match_library(path)
    if game:
        return game["name"]
    if path:
        from app.system.sysinfo import file_version_info

        info = file_version_info(path)
        for field_name in ("ProductName", "FileDescription"):
            if info.get(field_name):
                return info[field_name]
    return exe[:-4] if exe.lower().endswith(".exe") else exe


def _alive(s: _Session) -> bool:
    try:
        return psutil.Process(s.pid).create_time() == s.created
    except psutil.Error:
        return False


# ------------------------------------------------------------ the fast loop's part

def update(snapshot: SystemSnapshot, now: float | None = None) -> None:
    """One fast-loop cycle: follow the game in front, sample the PC, run captures."""
    global _session, _last_tick
    now = time.time() if now is None else now
    load = _cores.read()
    with _lock:
        tick = 0.0 if _last_tick is None else min(max(0.0, now - _last_tick), _MAX_TICK)
        _last_tick = now
        _collect_capture(now)
        if _session is not None and not _alive(_session):
            _end(now)
        app = focus.current_app()
        playing = bool(snapshot.foreground_game and app)
        if playing:
            pid, exe, path = app
            if _session is not None and _session.pid != pid:
                _end(now)  # another game came to the front
            if _session is None:
                try:
                    created = psutil.Process(pid).create_time()
                except psutil.Error:
                    return
                _session = _Session(pid, exe, path, _title(exe, path), now, now, now, created)
            s = _session
            if now - s.last_front > max(2 * tick, 5.0):
                s.front_since = now  # back in front after a while
            else:
                s.play_seconds += tick
            s.last_front = now
            _sample(s, snapshot, load)
            _maybe_capture(s, now)
        elif _session is not None:
            if _capture is not None and not _capture.done.is_set() and not _capture_manual:
                _capture.stop()  # frames of a game in the background say nothing
            if now - _session.last_front > _AWAY_ENDS_SESSION:
                _end(now)


def focus_mode_changed() -> None:
    """Focus mode was switched: the capture running now measured two modes, so the comparison
    leaves it out. Never waits for the game loop (a switch must stay instant): one assignment,
    which the next capture's start clears."""
    global _capture_mode_mixed
    _capture_mode_mixed = True


def _capture_mode() -> str | None:
    """The Focus mode the capture measured from start to end, for the Agent page's comparison;
    None when the mode changed meanwhile, or for a capture you started (it goes on while the
    game is in the background)."""
    mode = _capture_focus_mode
    if _capture_manual or _capture_mode_mixed or mode != focus.mode_in_effect():
        return None
    return mode


def _sample(s: _Session, snapshot: SystemSnapshot, load: tuple[float, float] | None) -> None:
    st = s.stats
    s.samples += 1
    s.modes[focus.mode_in_effect()] += 1
    names = {r["pid"]: r["name"] for r in monitor.latest_processes()[0]}
    for pid, percent in monitor.encoders().items():
        if percent >= _RECORDING_PERCENT and pid != s.pid and pid in names:
            s.recorders.setdefault(names[pid], _Stat()).add(percent)
    if load:
        st["cpu"].add(load[0])
        st["core"].add(load[1])
    st["ram"].add(snapshot.ram_percent)
    commit = (100 * snapshot.commit_used_mb / snapshot.commit_limit_mb
              if snapshot.commit_used_mb and snapshot.commit_limit_mb else None)
    if commit is not None and (st["commit"].peak is None or commit > st["commit"].peak):
        s.commit_peak = (snapshot.commit_used_mb, snapshot.commit_limit_mb)
    st["commit"].add(commit)
    st["faults"].add(snapshot.hard_faults_mb_s)
    fullest = max(snapshot.ram_percent, commit or 0.0)
    if fullest >= _MEMORY_FULL and fullest > s.memory_peak:  # what to close, named when memory was fullest
        s.memory_peak = fullest
        s.memory_apps = processes.biggest_apps(leave_out=monitor.app_tree(monitor.latest_processes()[0], s.pid))
    game = _game_load(s, snapshot)
    if game:
        st["game_cpu"].add(game["cpu"])
        st["game_mem"].add(game["mem_mb"])
        st["disk"].add(game["io_bps"] / _MB)
    g = gpu.primary()
    if g:
        s.gpu_samples += 1
        st["gpu_util"].add(g["utilization"])
        st["gpu_temp"].add(g["temperature_c"])
        st["gpu_clock"].add(g["clock_mhz"])
        st["gpu_power"].add(g["power_w"])
        st["vram"].add(g["memory_used_mb"])
        s.reasons.update(g["reasons"])
        s.gpu_info = {k: g[k] for k in ("name", "slowdown_c", "max_clock_mhz", "power_limit_w", "max_power_limit_w",
                                        "memory_total_mb")}
    s.live = {"cpu": None if not load else round(load[0], 1), "core": None if not load else round(load[1], 1),
              "ram": snapshot.ram_percent, "gpu": g, "game_cpu": round(game["cpu"], 1) if game else None,
              "game_mem_mb": round(game["mem_mb"], 1) if game else None}


def _game_load(s: _Session, snapshot: SystemSnapshot) -> dict | None:
    """The game's CPU, memory and I/O with its helper processes (a browser-engine game does
    most of its work in them), from the fast loop's process list; or the game's own sample."""
    rows, _ = monitor.latest_processes()
    tree = monitor.app_tree(rows, s.pid)
    members = [r for r in rows if r["pid"] in tree]
    if members:
        return {key: sum(r.get(key) or 0.0 for r in members) for key in ("cpu", "mem_mb", "io_bps")}
    own = next((p for p in snapshot.top_processes if p.pid == s.pid), None)
    return {"cpu": own.cpu_percent, "mem_mb": own.memory_mb, "io_bps": own.io_bps} if own else None


class _GameProcesses:
    """Which processes are the game's for a capture: the ones known when it started, and any
    process started by one of them later (a browser engine restarting its GPU process)."""

    def __init__(self, known: set[int]) -> None:
        self._yes, self._no = set(known), set()

    def __contains__(self, pid: object) -> bool:
        if pid in self._yes:
            return True
        if pid in self._no or not isinstance(pid, int):
            return False
        try:
            mine = any(parent.pid in self._yes for parent in psutil.Process(pid).parents())
        except psutil.Error:
            mine = False
        (self._yes if mine else self._no).add(pid)
        return mine


def _maybe_capture(s: _Session, now: float) -> None:
    if (_capture is not None or not settings.optimind_measure_games or now < _frames_paused_until
            or now - s.front_since < _FIRST_CAPTURE_AFTER or now < _next_capture):
        return
    _start_capture(s, _CAPTURE_SECONDS, manual=False, now=now)


def _start_capture(s: _Session, seconds: float, manual: bool, now: float) -> str | None:
    """Starts PresentMon on the game; returns why it can't, or None."""
    global _capture, _capture_focus_mode, _capture_manual, _capture_mode_mixed, _frames_paused_until
    problem = None
    if not frames.available():
        problem = tx("PresentMon isn't installed with OptiMind, so frame rates can't be measured")
    elif not is_elevated():
        problem = tx("measuring frame rates needs OptiMind to run as administrator")
    if problem:
        s.frames_error = problem
        _frames_paused_until = now + _RETRY_AFTER_ERROR
        return problem
    rows, _ = monitor.latest_processes()
    tree = monitor.app_tree(rows, s.pid) | {s.pid}
    names = {r["name"] for r in rows if r["pid"] in tree} | {s.exe}  # the game's program and its helpers'
    _capture = frames.Capture(pid=s.pid, seconds=seconds, refresh_hz=display.refresh_hz(), name=s.title,
                              names=names, pids=_GameProcesses(tree)).start()
    _capture_manual = manual
    _capture_focus_mode = focus.mode_in_effect()
    _capture_mode_mixed = False
    return None


def _collect_capture(now: float) -> None:
    global _capture, _capture_focus_mode, _capture_mode_mixed, _next_capture, _frames_paused_until
    if _capture is None or not _capture.done.is_set():
        return
    done, _capture = _capture, None
    capture_mode = _capture_mode()
    _capture_focus_mode, _capture_mode_mixed = None, False
    _next_capture = now + _CAPTURE_EVERY
    if _session is None or _session.pid != done.pid:
        return
    if done.result:
        # A capture cut short by an Alt+Tab is kept only if it still has a few seconds of frames.
        if not done.result.get("interrupted") or done.result["seconds"] >= 10:
            _session.captures.append({**done.result, "at": datetime.now(timezone.utc).isoformat(),
                                      "manual": _capture_manual, "focus_mode": capture_mode})
        _session.frames_error = None
    elif done.error:
        _session.frames_error = done.error
        if "administrator" in done.error or "installed" in done.error:
            _frames_paused_until = now + _RETRY_AFTER_ERROR


# ------------------------------------------------------------ ending a session

def _end(now: float) -> None:
    global _session, _capture, _capture_focus_mode, _capture_mode_mixed
    s, _session = _session, None
    if s is None:
        return
    if _capture is not None and _capture.pid == s.pid:
        capture_mode = _capture_mode()
        _capture.stop()
        if _capture.done.wait(3.0) and _capture.result and _capture.result["seconds"] >= 10:
            s.captures.append({**_capture.result, "at": datetime.now(timezone.utc).isoformat(),
                               "manual": _capture_manual, "focus_mode": capture_mode})
        _capture = None
        _capture_focus_mode, _capture_mode_mixed = None, False
    if s.play_seconds < _MIN_SESSION_SECONDS:
        return
    summary = summarize(s, now)
    _save(summary)
    logger.info("Game session saved: %s, %.0f min", summary["game"], summary["minutes"])


def stop() -> None:
    """OptiMind is quitting: the session so far is saved, PresentMon stopped."""
    with _lock:
        if _capture is not None:
            _capture.stop()
        _end(time.time())


def _save(summary: dict) -> None:
    global _saved
    entries = _load()
    entries.append(summary)
    _saved = entries[-_KEEP_SESSIONS:]
    _write()


def _write() -> None:
    try:  # (each text with how to show it in another language: i18n.storable)
        SESSIONS_PATH.write_text("".join(json.dumps(i18n.storable(e), ensure_ascii=False) + "\n" for e in _saved or []),
                                 encoding="utf-8")
    except OSError as e:
        logger.warning("Couldn't save the game sessions (%s)", e)


def _load() -> list[dict]:
    global _saved
    if _saved is None:
        _saved = []
        try:
            for line in SESSIONS_PATH.read_text(encoding="utf-8").splitlines():
                try:
                    _saved.append(json.loads(line))
                except ValueError:
                    continue
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Couldn't read the game sessions (%s)", e)
    return _saved


# ------------------------------------------------------------ summaries and advice

def _round(value: float | None, digits: int = 0):
    return None if value is None else round(value, digits) if digits else round(value)


def _merge_captures(captures: list[dict]) -> dict | None:
    """All captures of a session as one: the frame rate is all their frames over all their time,
    the 1% low is weighted by the time each measured, and a share of frames by its frames."""
    if not captures:
        return None
    frames_total = sum(c["frames"] for c in captures)
    seconds_total = sum(c["seconds"] for c in captures)
    weight = lambda key, by: sum(c[key] * c[by] for c in captures) / sum(c[by] for c in captures)  # noqa: E731
    shares: Counter = Counter()
    for c in captures:
        for key, value in (c.get("shares") or {}).items():
            shares[key] += value * c["frames"]
    shares = {k: round(v / frames_total, 3) for k, v in shares.items()}
    limiter = max(shares, key=lambda k: shares[k]) if shares else "unknown"
    caps = Counter((c["cap"]["kind"], c["cap"]["fps"]) for c in captures if c.get("cap"))
    modes = Counter(c["present_mode"] for c in captures if c.get("present_mode"))
    latency = [c["latency_ms"] for c in captures if c.get("latency_ms")]
    gpu_busy = [c["gpu_busy"] for c in captures if c.get("gpu_busy") is not None]
    return {
        "captures": len(captures),
        "seconds": round(seconds_total, 1),
        "frames": frames_total,
        "fps": round(frames_total / seconds_total, 1) if seconds_total else 0.0,
        "low_1_fps": round(weight("low_1_fps", "seconds"), 1),
        "worst_low_1_fps": min(c["low_1_fps"] for c in captures),
        "stutter_share": round(weight("stutter_share", "frames"), 4),
        "limiter": limiter,
        "shares": shares,
        "cap": ({"kind": caps.most_common(1)[0][0][0], "fps": caps.most_common(1)[0][0][1]}
                if caps and limiter == "cap" else None),
        "gpu_busy": round(sum(gpu_busy) / len(gpu_busy), 3) if gpu_busy else None,
        "latency_ms": round(sum(latency) / len(latency), 1) if latency else None,
        "present_mode": modes.most_common(1)[0][0] if modes else None,
    }


def _facts() -> dict:
    """What the advice needs to know about this PC."""
    fast = sorted((d for d in hardware.drives() if d["kind"] in ("NVMe SSD", "SSD")), key=lambda d: -d["free_gb"])
    return {
        "cores": psutil.cpu_count(logical=False) or 0,
        "threads": psutil.cpu_count() or 0,
        "refresh_hz": display.refresh_hz(),
        "ram_gb": round(psutil.virtual_memory().total / 1024 ** 3),
        "best_ssd": {"letter": fast[0]["letter"], "kind": fast[0]["kind"], "free_gb": round(fast[0]["free_gb"])}
        if fast else None,
    }


def summarize(s: _Session, now: float, facts: dict | None = None) -> dict:
    st = s.stats
    game = _match_library(s.path)
    samples = max(1, s.gpu_samples)
    gpu_summary = None
    if s.gpu_samples:
        gpu_summary = {
            **s.gpu_info,
            "util_avg": _round(st["gpu_util"].avg), "temp_avg": _round(st["gpu_temp"].avg),
            "temp_max": st["gpu_temp"].peak, "clock_avg": _round(st["gpu_clock"].avg),
            "power_avg": _round(st["gpu_power"].avg, 1), "vram_max_mb": _round(st["vram"].peak),
            "reasons": {k: round(v / samples, 3) for k, v in s.reasons.items() if k != "idle"},
        }
    if game:
        s.title = game["name"]  # the library may have been read since the session started
    summary = {
        "game": s.title, "exe": s.exe, "path": s.path,
        "store": game["store"] if game else None, "appid": game.get("appid") if game else None,
        "size_gb": round(game["gb"], 1) if game else None,
        "drive": hardware.drive_kind(s.path),
        "started": datetime.fromtimestamp(s.started, timezone.utc).isoformat(),
        "ended": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "minutes": round(s.play_seconds / 60, 1),
        "focus_mode": s.modes.most_common(1)[0][0] if s.modes else None,  # the one used most while you played
        "frames": _merge_captures(s.captures),
        "frames_by_focus_mode": {
            mode: merged for mode in ("off", "balanced", "maximum")
            if (merged := _merge_captures([c for c in s.captures if c.get("focus_mode") == mode]))
        },
        "frames_error": None if s.captures else s.frames_error,
        "cpu": {"avg": _round(st["cpu"].avg), "busiest_core_avg": _round(st["core"].avg),
                "busiest_core_max": _round(st["core"].peak), "game_avg": _round(st["game_cpu"].avg)},
        "memory": {"max_percent": _round(st["ram"].peak), "game_max_mb": _round(st["game_mem"].peak),
                   "commit_max_percent": _round(st["commit"].peak),
                   "commit_max_mb": _round(s.commit_peak[0]) if s.commit_peak else None,
                   "commit_limit_mb": _round(s.commit_peak[1]) if s.commit_peak else None,
                   "hard_faults_avg_mbps": _round(st["faults"].avg, 1), "hard_faults_max_mbps": _round(st["faults"].peak, 1),
                   "apps": s.memory_apps},
        "gpu": gpu_summary,
        "disk": {"avg_mbps": _round(st["disk"].avg, 1), "max_mbps": _round(st["disk"].peak, 1)},
        "recording": [
            {"app": name, "label": RECORDERS.get(name.lower(), (None, None))[0],
             "encode_avg": _round(stat.avg), "share": round(stat.count / max(1, s.samples), 2)}
            for name, stat in sorted(s.recorders.items(), key=lambda kv: -kv[1].count)
            if stat.count / max(1, s.samples) >= _RECORDING_SHARE],
    }
    summary["advice"] = advise(summary, facts if facts is not None else _facts())
    return summary


def _pct(share: float) -> i18n.Num:
    return pct(share * 100)


def _apps_text(apps: list[dict]) -> i18n.Items:
    """"Visual Studio Code (2.2 GB), Brave (1.5 GB) and Discord (950 MB)"."""
    return i18n.items(tx("{app} ({gb} GB)", app=a["title"], gb=i18n.num(a["mb"] / 1024, ".1f")) if a["mb"] >= 1024
                      else tx("{app} ({mb} MB)", app=a["title"], mb=a["mb"]) for a in apps)


def advise(summary: dict, facts: dict) -> list[dict]:
    """What to do about what held the game back, most urgent first. Each item:
    {"id", "kind" ("warn", "tip", "info" or "good"), "title", "text"}."""
    out: list[dict] = []
    fr, g = summary.get("frames"), summary.get("gpu") or {}
    reasons = g.get("reasons") or {}
    game = summary.get("game") or tx("this game")
    mem = summary.get("memory") or {}
    commit_full = (mem.get("commit_max_percent") or 0) >= _MEMORY_FULL and bool(mem.get("commit_limit_mb"))
    memory_full = (mem.get("max_percent") or 0) >= _MEMORY_FULL or commit_full

    if summary.get("drive") == "hard disk":
        ssd = facts.get("best_ssd")
        room = ssd and (summary.get("size_gb") is None or ssd["free_gb"] > (summary.get("size_gb") or 0) + 20)
        where = (tx("to {drive} ({kind}, {free} GB free)", drive=ssd["letter"], kind=_DRIVE_KINDS.get(ssd["kind"], ssd["kind"]),
                    free=ssd["free_gb"]) if room else tx("to an SSD"))
        text = (tx("{game} is on a hard disk, which needs about 10 ms to reach each piece of data: that's the long loading "
                   "screens and the hitches when the game streams in new areas. Move it {where} (Steam: right-click it > "
                   "Properties > Installed Files > Move install folder).", game=game, where=where)
                if summary.get("store") == "Steam" else
                tx("{game} is on a hard disk, which needs about 10 ms to reach each piece of data: that's the long loading "
                   "screens and the hitches when the game streams in new areas. Move it {where}.", game=game, where=where))
        out.append({"id": "move_to_ssd", "kind": "tip", "title": tx("Move it to the SSD"), "text": text})

    if fr:
        limiter, shares = fr["limiter"], fr.get("shares") or {}
        if limiter == "gpu":
            thermal = reasons.get("thermal", 0.0)
            power = reasons.get("power", 0.0)
            if thermal >= 0.05:
                out.append({"id": "gpu_hot", "kind": "warn", "title": tx("The graphics card slowed down from heat"),
                            "text": tx("It held its clocks back for temperature {share} of the time (up to {temp} °C; it "
                                       "slows down at {slowdown} °C). Clean the dust out of the card and the case, check "
                                       "the case fans pull air through, or set a steeper fan curve.", share=_pct(thermal),
                                       temp=g.get("temp_max"), slowdown=g.get("slowdown_c"))})
            busy = (tx("It was busy the whole frame {share} of the time and at its full power budget ({watts} W) {power} of "
                       "the time: the PC gives the game everything the card has.", share=_pct(shares.get("gpu", 0)),
                       watts=i18n.num(g.get("power_limit_w") or 0, ".0f"), power=_pct(power))
                    if power >= 0.2 and g.get("power_limit_w") else
                    tx("It was busy the whole frame {share} of the time: the PC gives the game everything the card has.",
                       share=_pct(shares.get("gpu", 0))))
            out.append({"id": "gpu_bound", "kind": "info", "title": tx("The graphics card is the limit"), "text": i18n.joined([
                busy, tx("For more frames lower the settings that load the GPU (resolution scale, shadows, ray tracing) or "
                         "turn on DLSS / FSR Quality, which renders fewer pixels with little visible difference."),
                tx("NVIDIA App's automatic tuning can add a few percent (see the Hardware check).")
                if "nvidia" in str(g.get("name", "")).lower() else None])})
        elif limiter == "cpu":
            core = (summary.get("cpu") or {}).get("busiest_core_avg")
            share = _pct(shares.get("cpu", 0))
            waited = (tx("The graphics card waited for the processor {share} of the time (its busiest core averaged {core}).",
                         share=share, core=pct(core)) if core is not None else
                      tx("The graphics card waited for the processor {share} of the time.", share=share))
            cores = facts.get("cores")
            out.append({"id": "cpu_bound", "kind": "info", "title": tx("The processor is the limit"), "text": i18n.joined([
                waited,
                tx("So the card has room to spare: you can raise resolution or graphics quality (or use DLAA instead of DLSS) "
                   "without losing frames. For more frames, lower the settings that load the CPU (view distance, crowds, "
                   "physics, simulation), keep Focus mode on Maximum while you play, and close what you don't need in the "
                   "background."),
                txn(cores, "With {n} core, a faster processor is what really lifts this game (see the Hardware check for what "
                    "fits your motherboard).", "With {n} cores, a faster processor is what really lifts this game (see the "
                    "Hardware check for what fits your motherboard).") if cores and cores <= 4 else None])})
        elif limiter == "cap" and fr.get("cap"):
            cap = fr["cap"]
            refresh = facts.get("refresh_hz")
            held = (tx("The frame rate was held at {fps} fps by V-Sync (your monitor's refresh rate), so the PC had headroom.",
                       fps=cap["fps"]) if cap["kind"] == "vsync" else
                    tx("The frame rate was held at {fps} fps by a frame rate cap, so the PC had headroom.", fps=cap["fps"]))
            if refresh and cap["fps"] < refresh * 0.9:
                more = tx("Your monitor runs at {hz} Hz: raise or remove the cap in the game's settings for smoother motion.",
                          hz=i18n.num(refresh, ".0f"))
            elif refresh:
                more = tx("With a G-SYNC or FreeSync monitor, a cap 3 fps below the refresh rate (or NVIDIA Reflex) keeps it "
                          "smooth without V-Sync's delay.")
            else:
                more = None
            out.append({"id": "capped", "kind": "good", "title": tx("Capped at {fps} fps", fps=cap["fps"]),
                        "text": i18n.joined([held, more])})

        if fr["stutter_share"] >= 0.02 or (fr["fps"] and fr["low_1_fps"] < 0.5 * fr["fps"]):
            causes = [tx("shader compilation (the first minutes after a game or driver update)")]
            if summary.get("drive") == "hard disk":
                causes.insert(0, tx("the hard disk it streams from"))
            if memory_full:
                faults = mem.get("hard_faults_max_mbps") or 0
                causes.insert(0, tx("full memory (Windows read up to {mbps} MB/s back from disk for what wasn't in memory)",
                                    mbps=i18n.num(faults, ".0f")) if faults >= 10 else tx("full memory"))
            out.append({"id": "stutter", "kind": "tip", "title": tx("Frame time spikes"),
                        "text": tx("{share} of frames took more than twice as long as usual (1% low {low} fps against {fps} "
                                   "fps on average). Likely causes: {causes}.", share=_pct(fr["stutter_share"]),
                                   low=i18n.num(fr["low_1_fps"], ".0f"), fps=i18n.num(fr["fps"], ".0f"),
                                   causes=i18n.joined(causes, ", "))})
        mode = (fr.get("present_mode") or "").lower()
        if "copy with" in mode:
            out.append({"id": "blt_model", "kind": "tip", "title": tx("Old presentation model"),
                        "text": tx("The game ran in a window with the old \"copy\" presentation, which adds latency. Turn on "
                                   "Optimize > Gaming > Optimizations for windowed games, or play in exclusive full screen.")})
    elif summary.get("frames_error"):
        out.append({"id": "no_frames", "kind": "info", "title": tx("Frame rate not measured"),
                    "text": i18n.sentence(summary["frames_error"])})

    # Whatever limited the frames (or if they weren't measured): full video memory means stutter.
    if g.get("vram_max_mb") and g.get("memory_total_mb") and g["vram_max_mb"] >= 0.95 * g["memory_total_mb"]:
        out.append({"id": "vram_full", "kind": "tip", "title": tx("Video memory was full"),
                    "text": tx("{used} of {total} GB in use. When textures don't fit, they stream over the PCIe link instead, "
                               "which causes stutter: lower texture quality one step.",
                               used=i18n.num(g["vram_max_mb"] / 1024, ".1f"), total=i18n.num(g["memory_total_mb"] / 1024, ".0f"))})

    for rec in summary.get("recording") or []:
        label, where = RECORDERS.get(rec["app"].lower(), (None, None))
        name = label or rec["app"]
        values = {"who": tx("{name} ({exe})", name=label, exe=rec["app"]) if label else rec["app"],
                  "encode": pct(rec["encode_avg"] or 0), "share": _pct(rec["share"])}
        text = (tx("{who} kept the graphics card's video encoder about {encode} busy for {share} of the time you played: "
                   "that's background recording (instant replay or automatic clips). Capturing every frame costs a few "
                   "percent of frame rate; if you don't use the clips, turn it off: {where}.", where=where, **values)
                if where else
                tx("{who} kept the graphics card's video encoder about {encode} busy for {share} of the time you played: "
                   "that's background recording (instant replay or automatic clips). Capturing every frame costs a few "
                   "percent of frame rate; if you don't use the clips, turn it off.", **values))
        out.append({"id": "recording", "kind": "tip", "title": tx("{name} recorded the game", name=name), "text": text})

    apps = mem.get("apps") or []
    close = (tx("The apps holding the most: {apps}. Close the ones you don't need while you play, or add RAM.",
                apps=_apps_text(apps)) if apps else tx("Close other apps while you play, or add RAM."))
    if commit_full:  # it says what the RAM's tip would, and more
        out.append({"id": "commit_full", "kind": "tip", "title": tx("Windows was near its memory limit"), "text": i18n.joined([
            tx("Up to {used} of {limit} GB was promised to apps (your RAM plus the page file). Near that limit Windows grows "
               "the page file and writes memory out to the disk, and an app that gets no more memory can close.",
               used=i18n.num(mem["commit_max_mb"] / 1024, ".1f"), limit=i18n.num(mem["commit_limit_mb"] / 1024, ".1f")),
            close])})
    elif (mem.get("max_percent") or 0) >= _MEMORY_FULL:
        out.append({"id": "ram_full", "kind": "tip", "title": tx("Memory was nearly full"), "text": i18n.joined([
            tx("Up to {pct} of your {gb} GB was in use, so Windows had to compress or page memory.",
               pct=pct(mem["max_percent"]), gb=facts.get("ram_gb", "?")), close])})
    order = {"warn": 0, "tip": 1, "info": 2, "good": 3}
    return sorted(out, key=lambda a: order.get(a["kind"], 4))


# ------------------------------------------------------------ what the API shows

def measure_now(seconds: float = _MEASURE_SECONDS) -> dict:
    """Starts a frame-time measurement of the game being played (any time during a
    session, also in the background). Raises LookupError without a game, RuntimeError
    when it can't measure."""
    with _lock:
        if _session is None or not _alive(_session):
            raise LookupError(tx("no game is running: start a game, then measure"))
        if _capture is not None and not _capture.done.is_set():
            return _capture.status()
        problem = _start_capture(_session, seconds, manual=True, now=time.time())
        if problem:
            raise RuntimeError(problem)
        return _capture.status()


def running() -> bool:
    """A game session is open: a game came to the front and hasn't closed (or stayed in the
    background for 5 minutes) since. One read that never waits: the engine asks every cycle."""
    return _session is not None


def sessions(limit: int = 30) -> list[dict]:
    with _lock:
        return list(reversed(_load()[-limit:]))


def set_note(started: str, exe: str, note: str) -> dict:
    """Your note on a saved session: what you changed for it ("render scale Quality", "closed the
    browser"), so two sessions can be compared knowing why they differ. An empty note removes it.
    The session is found by when it started and its program; LookupError when there's none."""
    note = " ".join(note.split())[:_NOTE_CHARS]
    with _lock:
        for s in _load():
            if s.get("started") == started and str(s.get("exe") or "").lower() == exe.lower():
                if note:
                    s["note"] = note
                else:
                    s.pop("note", None)
                _write()
                return s
    raise LookupError(tx("there's no such game session"))


def before_play(limit: int = _BEFORE_PLAY_APPS) -> dict:
    """Before you play (Games page): memory now, the apps holding the most memory of their own
    (closing one gives it back; each says whether it has a window to close), and the recorders
    that recorded one of your last 2 games, with where to turn them off. Read-only, from the
    fast loop's last process list; the game being played isn't listed."""
    rows, _ = monitor.latest_processes()
    with _lock:
        playing = _session
        recorded: dict[str, str] = {}
        for s in _load()[-_RECENT_SESSIONS:]:
            for rec in s.get("recording") or []:
                recorded.setdefault(str(rec.get("app") or "").lower(), str(rec.get("app") or ""))
    leave = monitor.app_tree(rows, playing.pid) if playing is not None else set()
    window_pids = processes.app_window_pids()
    with_window = {r["name"].lower() for r in rows if r["pid"] in window_pids}
    apps = [{**a, "window": a["name"].lower() in with_window}
            for a in processes.biggest_apps(limit, leave_out=leave, min_mb=_BEFORE_PLAY_MIN_MB)]
    recorders = [{"app": app, "label": RECORDERS.get(key, (None, None))[0] or app, "where": RECORDERS.get(key, (None, None))[1]}
                 for key, app in sorted(recorded.items()) if key]
    vm, perf = psutil.virtual_memory(), memory.performance_info()
    return {
        "memory": {"total_mb": round(vm.total / _MB), "available_mb": round(vm.available / _MB), "percent": vm.percent,
                   "commit_used_mb": perf.get("commit_used_mb"), "commit_limit_mb": perf.get("commit_limit_mb")},
        "apps": apps, "recorders": recorders, "game": playing.title if playing is not None else None,
    }


def status() -> dict:
    with _lock:
        current = None
        if _session is not None:
            s = _session
            try:
                advice = summarize(s, time.time())["advice"]  # what to do, from what's been measured so far
            except Exception:
                logger.debug("Live advice failed", exc_info=True)
                advice = []
            current = {
                "game": s.title, "exe": s.exe, "pid": s.pid,
                "since": datetime.fromtimestamp(s.started, timezone.utc).isoformat(),
                "minutes": round(s.play_seconds / 60, 1), "in_front": time.time() - s.last_front < 5,
                "live": s.live, "captures": s.captures[-3:], "frames": _merge_captures(s.captures),
                "history": [{"at": c["at"], "fps": c["fps"], "low_1_fps": c["low_1_fps"]}
                            for c in s.captures[-_HISTORY_CAPTURES:]],  # each capture's frame rate, nothing else
                "frames_error": s.frames_error, "advice": advice,
            }
        return {
            "presentmon": frames.available(), "elevated": is_elevated() if IS_WINDOWS else False,
            "measure_games": settings.optimind_measure_games, "gpu": gpu.available(),
            "current": current,
            "measuring": _capture.status() if _capture is not None and not _capture.done.is_set() else None,
        }


def library() -> dict:
    """Installed games with their drive and the sessions OptiMind measured, recently
    played first; and which ones to move to an SSD first. Never waits on a disk: until
    the libraries have been read once (a hard disk busy with a download can take minutes),
    "reading" is true and the games follow on a later call."""
    installed = hardware.installed_games_cached()
    played = {}
    for s in _load():
        key = os.path.normcase(s.get("path") or "")
        item = played.setdefault(key, {"sessions": 0, "minutes": 0.0, "last": None, "fps": None})
        item["sessions"] += 1
        item["minutes"] += s.get("minutes") or 0
        item["last"] = s.get("ended")
        if s.get("frames"):
            item["fps"] = s["frames"]["fps"]
    games = []
    for g in installed or []:
        folder = os.path.normcase((g.get("install_path") or "").rstrip("\\/"))
        stats = [v for k, v in played.items() if folder and k.startswith(folder + os.sep)]
        games.append({
            "name": g["name"], "store": g["store"], "appid": g.get("appid"), "size_gb": round(g["gb"], 1),
            "drive": hardware.drive_kind(g.get("install_path") or g.get("path")),
            "letter": (g.get("install_path") or g.get("path") or "")[:2].upper() or None,
            "last_played": datetime.fromtimestamp(g["played"], timezone.utc).isoformat() if g.get("played") else None,
            "sessions": sum(x["sessions"] for x in stats),
            "minutes": round(sum(x["minutes"] for x in stats), 1),
            "fps": next((x["fps"] for x in stats if x["fps"]), None),
        })
    games.sort(key=lambda g: (g["last_played"] or "", g["size_gb"]), reverse=True)
    fast = sorted((d for d in hardware.drives() if d["kind"] in ("NVMe SSD", "SSD")), key=lambda d: -d["free_gb"])
    move = [g for g in games if g["drive"] == "hard disk" and g["size_gb"] >= 2 and g["last_played"]]
    return {
        "games": games,
        "move_first": move[:8],
        "best_ssd": {"letter": fast[0]["letter"], "kind": fast[0]["kind"], "free_gb": round(fast[0]["free_gb"])}
        if fast else None,
        "reading": installed is None,
    }


def recent_summary(limit: int = 3) -> list[dict]:
    """The last few sessions in a few words each (for the AI's context)."""
    out = []
    for s in sessions(limit):
        fr = s.get("frames") or {}
        mem = s.get("memory") or {}
        out.append({"game": s["game"], "ended": s["ended"], "minutes": s["minutes"], "fps": fr.get("fps"),
                    "low_1_fps": fr.get("low_1_fps"), "limiter": fr.get("limiter"), "drive": s.get("drive"),
                    "gpu_temp_max": (s.get("gpu") or {}).get("temp_max"), "ram_max_percent": mem.get("max_percent"),
                    "commit_max_percent": mem.get("commit_max_percent"), "note": s.get("note"),
                    "advice": [i18n.recalled(a["title"], a.get("title_i18n")) for a in s.get("advice", [])]})
    return out
