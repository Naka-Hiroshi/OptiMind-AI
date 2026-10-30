"""
24-hour monitoring brain for OptiMind.

Collects and analyses metrics over a full day so the AI can make
smarter, continuous optimisation decisions based on longer trends
rather than just the last 15 minutes.

Jobs (all advisory — the AI never executes anything directly):

  - baseline(): what's "normal" for this PC (per-hour CPU/RAM/GPU ranges)
  - anomalies(): spikes that deviate from the baseline (possible problems)
  - trends(): whether performance is improving or degrading over days
  - build_24h_context(): everything the policy brain needs for a full-day review
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import METRICS_HISTORY_PATH
from app.i18n import tx


class PerformanceBaseline:
    """Per-hour-of-day normal ranges for CPU, RAM and GPU usage.

    Built from the last 7 days of data (or whatever is available).
    Each hour of the day gets its own p25/p50/p75 range, so 2 PM on a
    weekday is compared against 2 PM, not against midnight.
    """

    def __init__(self) -> None:
        self.hourly: dict[int, dict[str, dict[str, float]]] = {}  # hour -> {cpu: {p25,p50,p75}, ram: {...}, gpu: {...}}
        self.days_used: int = 0
        self.samples: int = 0

    def is_empty(self) -> bool:
        return not self.hourly


class AnomalyReport:
    """A metric reading that deviates from the baseline."""

    def __init__(
        self,
        when: str,
        metric: str,        # "cpu", "ram", "gpu"
        value: float,
        baseline_p75: float,
        severity: str,      # "low", "medium", "high"
        context: str,
    ) -> None:
        self.when = when
        self.metric = metric
        self.value = value
        self.baseline_p75 = baseline_p75
        self.severity = severity
        self.context = context


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


def _parse_time(entry: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(entry.get("t", ""))
    except (ValueError, TypeError):
        return None


def build_baseline(history: list[dict], lookback_days: int = 7) -> PerformanceBaseline:
    """Build per-hour-of-day baselines from the last lookback_days of history."""
    baseline = PerformanceBaseline()
    if not history:
        return baseline

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=lookback_days)
    by_hour: dict[int, dict[str, list[float]]] = {}

    for entry in history:
        t = _parse_time(entry)
        if t is None or t < cutoff:
            continue
        hour = t.hour
        h = by_hour.setdefault(hour, {"cpu": [], "ram": [], "gpu": []})
        h["cpu"].append(entry.get("cpu", 0.0))
        h["ram"].append(entry.get("ram", 0.0))

    for hour, h in by_hour.items():
        baseline.hourly[hour] = {}
        for metric in ("cpu", "ram"):
            values = sorted(h[metric])
            if not values:
                continue
            n = len(values)
            baseline.hourly[hour][metric] = {
                "p25": values[max(0, n // 4)],
                "p50": values[n // 2],
                "p75": values[min(n - 1, (3 * n) // 4)],
                "count": n,
            }
        baseline.samples += sum(len(h[m]) for m in ("cpu", "ram"))

    baseline.days_used = lookback_days
    return baseline


def detect_anomalies(history: list[dict], baseline: PerformanceBaseline, lookback_hours: int = 24) -> list[AnomalyReport]:
    """Find readings in the last lookback_hours that deviate from the baseline."""
    anomalies: list[AnomalyReport] = []
    if baseline.is_empty() or not history:
        return anomalies

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=lookback_hours)

    for entry in history:
        t = _parse_time(entry)
        if t is None or t < cutoff:
            continue
        hour = t.hour
        hb = baseline.hourly.get(hour)
        if not hb:
            continue

        cpu = entry.get("cpu", 0.0)
        ram = entry.get("ram", 0.0)

        cpu_p75 = hb.get("cpu", {}).get("p75")
        ram_p75 = hb.get("ram", {}).get("p75")

        if cpu_p75 is not None and cpu > cpu_p75 * 1.5 and cpu > 70:
            anomalies.append(AnomalyReport(
                when=t.isoformat(),
                metric="cpu",
                value=cpu,
                baseline_p75=cpu_p75,
                severity="high" if cpu > cpu_p75 * 2 else "medium",
                context=f"CPU at {cpu:.0f}% vs typical {cpu_p75:.0f}% for this hour",
            ))
        if ram_p75 is not None and ram > ram_p75 * 1.4 and ram > 75:
            anomalies.append(AnomalyReport(
                when=t.isoformat(),
                metric="ram",
                value=ram,
                baseline_p75=ram_p75,
                severity="high" if ram > ram_p75 * 1.7 else "medium",
                context=f"RAM at {ram:.0f}% vs typical {ram_p75:.0f}% for this hour",
            ))

    # Sort newest first, keep the most significant
    anomalies.sort(key=lambda a: a.when, reverse=True)
    return anomalies[:20]


def analyze_trends(history: list[dict], lookback_days: int = 7) -> dict:
    """Compare the last 24 hours against the previous period to see if things
    are getting better or worse. Returns a short dict the AI can use."""
    if not history:
        return {"trend": "unknown", "note": "Not enough history yet."}

    times = [( _parse_time(e), e) for e in history if _parse_time(e)]
    times = [(t, e) for t, e in times if t is not None]
    if len(times) < 2:
        return {"trend": "unknown", "note": "Not enough data points."}

    times.sort(key=lambda x: x[0])
    now = times[-1][0]
    day_ago = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)

    recent = [e for t, e in times if t >= day_ago]
    previous = [e for t, e in times if two_days_ago <= t < day_ago]

    if not recent or not previous:
        return {"trend": "unknown", "note": "Building up 24-hour history..."}

    def avg(entries: list[dict], key: str) -> float:
        vals = [e.get(key, 0.0) for e in entries if e.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    recent_cpu = avg(recent, "cpu")
    prev_cpu = avg(previous, "cpu")
    recent_ram = avg(recent, "ram")
    prev_ram = avg(previous, "ram")

    cpu_change = recent_cpu - prev_cpu
    ram_change = recent_ram - prev_ram

    if abs(cpu_change) < 3 and abs(ram_change) < 3:
        trend = "stable"
        note = tx("Performance is stable compared to yesterday.")
    elif cpu_change > 5 or ram_change > 5:
        trend = "degrading"
        note = tx("CPU/RAM usage is higher than yesterday — something may be running heavier.")
    elif cpu_change < -5 or ram_change < -5:
        trend = "improving"
        note = tx("CPU/RAM usage is lower than yesterday — the PC is running lighter.")
    else:
        trend = "stable"
        note = tx("Performance is broadly the same as yesterday.")

    return {
        "trend": trend,
        "note": note,
        "recent_cpu_avg": round(recent_cpu, 1),
        "previous_cpu_avg": round(prev_cpu, 1),
        "recent_ram_avg": round(recent_ram, 1),
        "previous_ram_avg": round(prev_ram, 1),
    }


def build_24h_context(
    history: list[dict],
    baseline: PerformanceBaseline,
    anomalies: list[AnomalyReport],
    trends: dict,
    recent_actions: list[dict],
    recent_sessions: list[dict] | None = None,
) -> str:
    """Build a compact 24-hour monitoring summary for the policy brain / chat AI."""
    parts: list[str] = []

    # Baseline summary
    if baseline.is_empty():
        parts.append("## 24-hour baseline\nNot enough data yet to establish a baseline.")
    else:
        hours = sorted(baseline.hourly)
        peak_hour = max(hours, key=lambda h: baseline.hourly[h].get("cpu", {}).get("p50", 0))
        peak_cpu = baseline.hourly[peak_hour].get("cpu", {}).get("p50", 0)
        parts.append(
            f"## 24-hour baseline (last {baseline.days_used} days, {baseline.samples} samples)\n"
            f"Typical CPU by hour: peak around {peak_hour}:00 at {peak_cpu:.0f}%. "
            f"Busy hours tend to be {', '.join(str(h) for h in sorted(hours) if baseline.hourly[h].get('cpu', {}).get('p50', 0) > 60) or 'none'}.\n"
        )

    # Anomalies
    if anomalies:
        parts.append(f"## Anomalies in the last 24 hours ({len(anomalies)})\n")
        for a in anomalies[:10]:
            parts.append(f"- {a.when[:16]}: {a.context} (severity: {a.severity})")
    else:
        parts.append("## Anomalies in the last 24 hours\nNone detected — usage stayed within normal ranges.")

    # Trends
    parts.append(f"\n## Trend vs yesterday\n{trends.get('note', 'Building history...')}")
    if trends.get("recent_cpu_avg") is not None:
        parts.append(
            f"Recent 24h: CPU avg {trends['recent_cpu_avg']:.0f}% (was {trends['previous_cpu_avg']:.0f}%), "
            f"RAM avg {trends['recent_ram_avg']:.0f}% (was {trends['previous_ram_avg']:.0f}%)."
        )

    # Recent actions summary
    if recent_actions:
        applied = sum(1 for a in recent_actions if a.get("applied"))
        parts.append(f"\n## Agent actions (recent)\n{applied} of {len(recent_actions)} applied. "
                     f"Most common: " + ", ".join(
                         f"{a} ({n}x)" for a, n in
                         sorted(__import__("collections").Counter(a["action"] for a in recent_actions).items(),
                                key=lambda x: -x[1])[:5]
                     ))
    else:
        parts.append("\n## Agent actions (recent)\nNone recorded yet.")

    # Recent game sessions
    if recent_sessions:
        parts.append("\n## Recent game sessions\n")
        for s in recent_sessions[-5:]:
            fps = f"{s.get('fps', 0):.0f} fps" if s.get("fps") else "not measured"
            parts.append(f"- {s.get('game', '?')}: {fps}, {s.get('minutes', 0):.0f} min, "
                         f"limited by {s.get('limiter') or 'unknown'}")
    else:
        parts.append("\n## Recent game sessions\nNone measured yet.")

    return "\n".join(parts)


def save_baseline(baseline: PerformanceBaseline) -> None:
    """Persist the baseline to the settings data dir so it survives restarts."""
    import os
    from app.config import USER_DIR
    path = USER_DIR / "ai_baseline.json"
    data = {
        "hourly": {str(k): v for k, v in baseline.hourly.items()},
        "days_used": baseline.days_used,
        "samples": baseline.samples,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_baseline() -> PerformanceBaseline:
    """Load a previously saved baseline, or an empty one."""
    from app.config import USER_DIR
    path = USER_DIR / "ai_baseline.json"
    baseline = PerformanceBaseline()
    if not path.exists():
        return baseline
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        baseline.hourly = {int(k): v for k, v in data.get("hourly", {}).items()}
        baseline.days_used = data.get("days_used", 7)
        baseline.samples = data.get("samples", 0)
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return baseline
