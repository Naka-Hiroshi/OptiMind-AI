"""Shared in-memory state between the orchestrator (loops) and the
API (FastAPI). Simple on purpose — it's a local prototype, not a
distributed system."""
from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.models import ActionLogEntry, OptimizationPolicy, SystemSnapshot

_KEEP_ACTIONS = 200


@dataclass
class AppState:
    latest_snapshot: SystemSnapshot | None = None
    current_policy: OptimizationPolicy = field(default_factory=OptimizationPolicy)
    recent_actions: list[ActionLogEntry] = field(default_factory=list)
    dry_run: bool = True  # changes nothing until the orchestrator starts; then True only in test mode
    ai_loop_last_run: str | None = None
    ai_loop_last_error: str | None = None
    ai_last_provider: str | None = None
    fast_loop_ticks: int = 0
    managed_processes: dict[str, list[str]] = field(default_factory=dict)  # name -> ["priority", "efficiency", "io"]
    # 24-hour monitoring state
    ai_baseline_saved: str | None = None  # ISO time when the baseline was last built
    ai_anomaly_count: int = 0  # anomalies detected in last 24h
    ai_performance_trend: str = "unknown"  # "improving", "degrading", "stable", "unknown"
    # The agent's loop and the API (on its worker threads) both change recent_actions.
    actions_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add_actions(self, entries: Iterable[ActionLogEntry]) -> None:
        with self.actions_lock:
            self.recent_actions = (self.recent_actions + list(entries))[-_KEEP_ACTIONS:]


app_state = AppState()
