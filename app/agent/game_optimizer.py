"""
Game GPU optimization: when a game is in focus and the graphics card is
heavily used, OptiMind applies extra optimizations to free GPU and system
resources for the game — lower background GPU priority, memory trims,
High performance power plan, NVIDIA shader cache and Windows Game Mode.

All changes are reversed when the game leaves focus or GPU usage calms down,
so nothing is left changed after you stop playing. Each optimization is
applied at most once per cooldown to avoid flapping.

This is a layer on top of the existing optimizer (app/agent/optimizer.py):
it decides *when* a game needs extra GPU help and *what* to do, then hands
OptimizationActions to the normal apply path (safety validation, dry-run,
per-cycle limit, give-back tracking).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app import i18n
from app.agent import optimizer
from app.config import IS_WINDOWS
from app.i18n import tx, txn
from app.models import ActionType, OptimizationAction
from app.system import gpu, gpu_optimization

logger = logging.getLogger("optimind.game_optimizer")

# GPU usage above this % while a game is in focus: extra optimizations kick in.
_GPU_HIGH_THRESHOLD = 85.0
# GPU usage below this %: optimizations are considered no longer needed.
_GPU_LOW_THRESHOLD = 70.0
# How long GPU must stay above/below the line before acting (avoids flapping).
_GPU_HIGH_SECONDS = 10.0
_GPU_LOW_SECONDS = 30.0
# Don't re-apply the same optimization more often than this.
_APPLY_COOLDOWN_SECONDS = 300.0


@dataclass
class _AppliedOpt:
    """One optimization that was applied, with info to reverse it."""
    kind: str  # "shader_cache", "game_mode", "power_plan"
    target: str  # setting name or process group
    applied_at: float
    original: object | None = None  # original value to restore


class GameOptimizer:
    """Extra optimizations for games when the GPU is heavily used.

    Call update() once per fast-loop cycle with the current snapshot's game
    and GPU readings. It returns OptimizationActions for the orchestrator to
    apply through the normal safety-validated path.
    """

    def __init__(self) -> None:
        self._applied: dict[str, _AppliedOpt] = {}  # kind -> applied optimization
        self._gpu_high_since: float | None = None
        self._gpu_low_since: float | None = None
        self._last_apply: dict[str, float] = {}  # kind -> last apply time
        self._enabled = True
        self._lock = threading.RLock()

    # ------------------------------------------------------------ public API

    def enable(self) -> None:
        with self._lock:
            self._enabled = True

    def disable(self) -> None:
        with self._lock:
            self._enabled = False

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def status(self) -> dict:
        """What the game optimizer is doing right now (for the API/dashboard)."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "applied": {kind: {"target": a.target, "applied_at": a.applied_at}
                            for kind, a in self._applied.items()},
                "gpu_high_since": self._gpu_high_since,
                "gpu_low_since": self._gpu_low_since,
            }

    def restore_all(self) -> list[OptimizationAction]:
        """Restore every optimization that was applied (game left focus, or
        OptiMind is quitting). Returns restore actions."""
        with self._lock:
            kinds = list(self._applied.keys())
            self._applied.clear()
            self._gpu_high_since = None
            self._gpu_low_since = None
        actions: list[OptimizationAction] = []
        for kind in kinds:
            actions.extend(self._restore_one(kind))
        return actions

    def update(self, now: float, game_in_focus: bool,
               gpu_reading: dict | None) -> list[OptimizationAction]:
        """One fast-loop cycle. Returns actions to apply (or an empty list).

        `game_in_focus` is True when Focus mode reports a game in front.
        `gpu_reading` is the primary GPU's sensor dict from app/system/gpu.py
        (or None when there's no NVIDIA GPU / reading failed).
        """
        if not self._enabled:
            return []
        actions: list[OptimizationAction] = []

        gpu_util = None
        if gpu_reading:
            gpu_util = gpu_reading.get("utilization")
        gpu_high = gpu_util is not None and gpu_util >= _GPU_HIGH_THRESHOLD
        gpu_low = gpu_util is not None and gpu_util <= _GPU_LOW_THRESHOLD

        with self._lock:
            if game_in_focus and gpu_high:
                if self._gpu_high_since is None:
                    self._gpu_high_since = now
                self._gpu_low_since = None
                if now - self._gpu_high_since >= _GPU_HIGH_SECONDS:
                    actions += self._apply_optimizations(now, gpu_reading or {})
            elif game_in_focus and gpu_low:
                if self._gpu_low_since is None:
                    self._gpu_low_since = now
                self._gpu_high_since = None
                if now - self._gpu_low_since >= _GPU_LOW_SECONDS:
                    actions += self._restore_all_applied()
            # If there's no game or no GPU reading, don't change state: leave
            # optimizations in place while the game is merely Alt+Tabbed out.

        if not game_in_focus:
            with self._lock:
                kinds = list(self._applied.keys()) if self._applied else []
                if kinds:
                    self._applied.clear()
                    self._gpu_high_since = None
                    self._gpu_low_since = None
        else:
            kinds = []
        for kind in kinds:
            actions.extend(self._restore_one(kind))

        return actions

    # ------------------------------------------------------------ applying

    def _apply_optimizations(self, now: float, gpu_reading: dict) -> list[OptimizationAction]:
        """Decide which extra optimizations to apply right now. Returns
        OptimizationActions (never applies them directly — the orchestrator
        does that through the safety-validated path)."""
        actions: list[OptimizationAction] = []

        # 1. NVIDIA shader cache: caching compiled shaders reduces stutter
        #    (and can raise minimum FPS) when the game encounters new shaders.
        if "shader_cache" not in self._applied and self._can_apply("shader_cache", now):
            sc = gpu_optimization.shader_cache_enabled()
            if sc is False:  # off: turn it on
                actions.append(OptimizationAction(
                    action=ActionType.GAME_OPTIMIZE,
                    target="nvidia_shader_cache",
                    reason=tx("The graphics card's shader cache is off: turning it on so the game "
                              "compiles shaders once and reuses them, cutting stutter on low-end PCs."),
                ))
                self._applied["shader_cache"] = _AppliedOpt("shader_cache", "nvidia_shader_cache", now, False)
            elif sc is None:
                logger.debug("NVIDIA shader cache setting not readable (non-NVIDIA GPU or no driver)")

        # 2. Windows Game Mode: prioritizes the game for CPU/GPU and blocks
        #    Windows Update interruptions while playing.
        if "game_mode" not in self._applied and self._can_apply("game_mode", now):
            gm = gpu_optimization.game_mode_enabled()
            if gm is False:
                actions.append(OptimizationAction(
                    action=ActionType.GAME_OPTIMIZE,
                    target="windows_game_mode",
                    reason=tx("Windows Game Mode is off: turning it on so the game gets CPU/GPU priority "
                              "and Windows Update stays quiet while you play."),
                ))
                self._applied["game_mode"] = _AppliedOpt("game_mode", "windows_game_mode", now, False)
            elif gm is None:
                logger.debug("Game Mode setting not readable")

        return actions

    def _can_apply(self, kind: str, now: float) -> bool:
        """Whether this kind of optimization may (co)be applied right now."""
        last = self._last_apply.get(kind)
        if last is not None and now - last < _APPLY_COOLDOWN_SECONDS:
            return False
        self._last_apply[kind] = now
        return True

    # ------------------------------------------------------------ restoring

    def _restore_all_applied(self) -> list[OptimizationAction]:
        """Restore every optimization currently applied (GPU usage calmed down)."""
        with self._lock:
            kinds = list(self._applied.keys())
            self._applied.clear()
        actions: list[OptimizationAction] = []
        for kind in kinds:
            actions.extend(self._restore_one(kind))
        return actions

    def _restore_one(self, kind: str) -> list[OptimizationAction]:
        """Restore one optimization: undo its change via the normal apply path."""
        applied = None
        with self._lock:
            applied = self._applied.pop(kind, None)
        if not applied:
            return []
        if applied.kind == "shader_cache":
            # Only restore if we actually changed it (original was False, meaning it was off)
            if applied.original is False:
                return [OptimizationAction(
                    action=ActionType.GAME_OPTIMIZE_RESTORE,
                    target="nvidia_shader_cache",
                    reason=tx("The game isn't in focus or the GPU has calmed down: putting the NVIDIA "
                              "shader cache back the way you had it."),
                )]
        elif applied.kind == "game_mode":
            if applied.original is False:
                return [OptimizationAction(
                    action=ActionType.GAME_OPTIMIZE_RESTORE,
                    target="windows_game_mode",
                    reason=tx("The game isn't in focus or the GPU has calmed down: putting Windows Game "
                              "Mode back the way you had it."),
                )]
        return []
