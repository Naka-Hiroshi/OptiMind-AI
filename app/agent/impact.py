"""
What OptiMind changed, in numbers (the Agent page's "What OptiMind changed"):

- what it did over the last days, with the amounts Windows reported: memory freed from idle
  apps, identical memory merged, background apps held back, background work kept off a game's
  cores and its graphics card;
- how busy the PC was: OptiMind matters most under load, and a PC that's rarely busy has little
  to gain;
- for a game measured both with Focus mode Off and with it on, the frame rates side by side:
  the one fair comparison of what Focus mode changes in that game.

Everything comes from OptiMind's own files (the activity log, game sessions, metrics history).
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import ACTION_LOG_PATH, METRICS_HISTORY_PATH

_FREED = re.compile(r"freed ([\d,.]+) MB")
_MERGED = re.compile(r"merged ([\d,.]+) MB")
_HOLDS = {"lower_priority", "efficiency_mode_on", "lower_io_priority"}
_MODES = ("off", "balanced", "maximum")


def _rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _when(value) -> datetime | None:
    """OptiMind's timestamps, in UTC (older ones without a zone are UTC too)."""
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _mb(pattern: re.Pattern, text: str | None) -> float:
    match = pattern.search(text or "")
    return float(match.group(1).replace(",", "")) if match else 0.0


def _activity(entries: list[dict]) -> dict:
    done = [e for e in entries if e.get("applied")]
    kinds = Counter(e.get("action") for e in done)
    held = Counter(e.get("target") for e in done if e.get("action") in _HOLDS)
    return {
        "memory": {"freed_mb": round(sum(_mb(_FREED, e.get("result")) for e in done
                                         if e.get("action") == "trim_working_set")),
                   "trims": kinds["trim_working_set"],
                   "merged_mb": round(sum(_mb(_MERGED, e.get("result")) for e in done
                                          if e.get("action") == "combine_memory")),
                   "merges": kinds["combine_memory"], "cache_purges": kinds["purge_standby_list"]},
        "held_back": {"times": sum(held.values()),
                      "apps": [{"name": name, "times": n} for name, n in held.most_common(5)]},
        "games": {"one_core": kinds["confine_cores"], "gpu_priority": kinds["lower_gpu_priority"]},
        "power_plan_switches": kinds["switch_power_plan"],
    }


def _load() -> dict | None:
    """How busy the PC was over the hours OptiMind keeps (its metrics history)."""
    rows = [(t, float(r["cpu"])) for r in _rows(METRICS_HISTORY_PATH)
            for t in [_when(r.get("t"))] if t and isinstance(r.get("cpu"), (int, float))]
    if len(rows) < 20:
        return None
    cpu = [c for _, c in rows]
    return {"hours": round((rows[-1][0] - rows[0][0]).total_seconds() / 3600, 1),
            "cpu_median": round(statistics.median(cpu), 1),
            "busy_share": round(sum(c >= 85 for c in cpu) / len(cpu), 3)}


def _measured(sessions: list[dict]) -> dict:
    """Frame rates of captures in these sessions, weighted by capture duration."""
    seconds = sum(s["frames"]["seconds"] for s in sessions)
    frames = sum(s["frames"]["frames"] for s in sessions)
    return {
        "sessions": len(sessions),
        "captures": sum(s["frames"]["captures"] for s in sessions),
        "minutes": round(sum(s.get("minutes") or 0 for s in sessions), 1),
        "fps": round(frames / seconds, 1) if seconds else 0.0,
        "low_1_fps": round(sum(s["frames"]["low_1_fps"] * s["frames"]["seconds"] for s in sessions) / seconds, 1)
        if seconds else 0.0,
    }


def _compare(sessions: list[dict]) -> list[dict]:
    """Compare captures tagged with the Focus mode active for their full duration.
    Older sessions and captures spanning a mode change aren't assigned a mode."""
    by_game: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for s in sessions:
        for mode, frame_data in (s.get("frames_by_focus_mode") or {}).items():
            if (mode in _MODES and frame_data.get("fps") and frame_data.get("low_1_fps")
                    and frame_data.get("seconds") and frame_data.get("frames")):
                by_game[s.get("game") or s.get("exe") or "?"][mode].append({**s, "frames": frame_data})
    out = []
    for game, modes in sorted(by_game.items()):
        if "off" not in modes or len(modes) < 2:
            continue
        measured = {mode: _measured(modes[mode]) for mode in _MODES if mode in modes}
        off = measured["off"]
        out.append({"game": game, "modes": measured, "change": {
            mode: {"fps": round((m["fps"] / off["fps"] - 1) * 100, 1),
                   "low_1_fps": round((m["low_1_fps"] / off["low_1_fps"] - 1) * 100, 1)}
            for mode, m in measured.items() if mode != "off"},
            "repeatable": all(m["captures"] >= 3 for m in measured.values())})
    return out


def summary(days: int = 7, now: datetime | None = None) -> dict:
    from app.agent.games import SESSIONS_PATH

    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    entries = [e for e in _rows(ACTION_LOG_PATH) if (_when(e.get("timestamp")) or since) > since]
    sessions = _rows(SESSIONS_PATH)
    recent = [s for s in sessions if (_when(s.get("started")) or since) > since]
    return {
        "days": days, "since": since.isoformat(timespec="seconds"),
        **_activity(entries),
        "played": {"sessions": len(recent), "minutes": round(sum(s.get("minutes") or 0 for s in recent)),
                   "measured": sum(1 for s in recent if (s.get("frames") or {}).get("fps"))},
        "load": _load(),
        "compare": _compare(sessions),
    }
