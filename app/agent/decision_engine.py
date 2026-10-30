"""
LOCAL decision engine (heuristic, does not call any AI).

This is what runs every 1-2 seconds: it takes a SystemSnapshot +
the current OptimizationPolicy (which the AI adjusts every N minutes)
and decides which OptimizationActions to apply right now.

It is deliberately simple and deterministic — it is the real-time
engine, so it has to be predictable, cheap and explainable. The more
sophisticated "intelligence" (recognizing usage patterns, adjusting
thresholds, learning from history) lives in ai/policy_brain.py, which
runs much more slowly.

It keeps a "short-term memory" (internal state) only to avoid
flapping: it doesn't keep toggling the same thing on/off every 2s, and
it doesn't trim the same app's memory over and over. Memory pressure
has to last (10 s above the line) and ends 5 points below it, and an app
opened while OptiMind runs counts as unused only since it appeared.

The AI's protected apps (policy.process_denylist_extra) keep their CPU,
disk and GPU priority: the AI adds an app there when the agent kept
holding it back and you kept switching back to it. Compressing an idle
app's memory has its own rules (never the app in front, one of your last
apps, one playing sound or one that's working), so the list doesn't stop
it: while you play, those apps are often the memory the game needs.

Focus mode (app/agent/focus.py) puts things back the instant you switch
apps; the engine adds the slower half: while a game is in focus it acts
sooner on background apps that use the CPU (and Maximum mode keeps them
on one core), gives background apps that use the GPU a lower GPU
priority, apps playing sound are never held back, the apps you used
just before the current one are never compressed, and the High
performance plan stays on while you play.
"""
from __future__ import annotations

import time
from collections import deque

from app import i18n
from app.agent import game_optimizer
from app.agent.optimizer import PLAN_NAMES
from app.agent.safety import PROTECTED_PROCESSES, RECORDING_APPS
from app.i18n import pct, tx
from app.models import ActionType, OptimizationAction, OptimizationPolicy, ProcessSample, SystemSnapshot
from app.system import gpu
from app.config import IS_WINDOWS

_CPU_HOG_THRESHOLD = 15.0  # CPU % of a single background process (100 = one full core)
_IO_HOG_BPS = 5 * 1024 * 1024  # 5 MB/s of reads + writes by a single background process
# While a game is in focus the engine acts sooner, and on lighter background work.
_GAME_GRACE_SECONDS = 10.0
_GAME_CPU_HOG_THRESHOLD = 10.0
_GPU_HOG_PERCENT = 5.0  # a background app's busiest GPU engine, while a game is in focus
_CORES_RELEASE_SECONDS = 30.0  # apps kept on one core (or at low GPU priority) get it back this long after the game

# Memory. Trimming moves an app's pages out of RAM: changed ones are compressed by
# Windows, the rest stay as cache. It only pays off for apps that are idle and that
# you aren't using, and only once in a while (the pages come back when needed).
_RAM_HOG_THRESHOLD_MB = 300.0  # under memory pressure: apps holding at least this much
_IDLE_COMPRESS_MIN_MB = 150.0  # idle compression: apps holding at least this much
_BUSY_CPU = 5.0  # an app using more CPU than this (% of one core) is working: never trimmed
_IDLE_CPU = 1.0  # ...and below this it counts as idle
_RECENT_USE_SECONDS = 120.0  # an app you were using this recently isn't trimmed
_TRIM_COOLDOWN_SECONDS = 300.0  # the same app is trimmed under pressure at most every 5 min
_COMPRESS_COOLDOWN_SECONDS = 1800.0  # ...and its idle memory compressed at most every 30 min
_COMPRESS_MIN_RAM_PERCENT = 50.0  # with this much RAM still free, compressing gains nothing
_MAX_TRIMS_PER_CYCLE = 3
# Memory pressure: RAM at the policy's line for this long (a game loading or a spike isn't pressure),
# and it ends this many points below the line, so readings around it don't switch it on and off.
_PRESSURE_SECONDS = 10.0
_PRESSURE_MARGIN = 5.0
_SEEN_FORGET_SECONDS = 3600.0  # when a process was first seen is forgotten an hour after it was last seen
# Standby purge (like ISLC): only when the free list is nearly empty while lots of
# cache sits in standby, under real memory pressure, at most every 10 minutes. Below 90%
# Windows reuses standby memory by itself as apps need it; purging it there only made
# apps and games read their files from the disk again.
_PURGE_RAM_PERCENT = 90.0
_PURGE_FREE_BELOW_MB = 512.0
_PURGE_STANDBY_ABOVE_MB = 2048.0
_STANDBY_PURGE_COOLDOWN_SECONDS = 600.0
# Page combining: Windows merges identical pages (the same data held by several apps).
# The scan costs CPU, so it runs at most every 30 minutes, never while a game runs (in front or
# behind another window) and only with the processor calm. Real log: it ran during an Alt+Tab out
# of a game, 17.5 s for 935 MB on a 4-core processor already at 94-99%.
_COMBINE_TARGET = tx("memory pages")
_COMBINE_MIN_RAM_PERCENT = 60.0
_COMBINE_FIRST_AFTER_SECONDS = 300.0
_COMBINE_COOLDOWN_SECONDS = 1800.0
_COMBINE_RETRY_PAUSE_SECONDS = 6 * 3600.0  # if Windows refuses, it isn't asked again for hours
_COMBINE_MAX_CPU = 50.0  # calm: every reading of the last ~30 s below this
_COMBINE_CALM_SAMPLES = 15
_UNREACHABLE_SECONDS = 6 * 3600.0  # a process the optimizer couldn't find under its name: left alone this long

# Power plan: the high-load plan needs the CPU busy for a few cycles (~6 s), and going
# back needs it calm (below the line minus a margin) for a minute: no flapping on spikes.
# A game in focus counts as high load right away.
_POWER_PLAN_COOLDOWN_SECONDS = 60.0
_BUSY_SAMPLES = 3
# Back to the idle plan after three calm minutes: with one, a game's menu, a loading screen
# or a quick Alt+Tab flipped the plan back and forth every minute or two.
_CALM_SECONDS = 180.0
_CALM_MARGIN = 15.0

_RETRY_PAUSE_SECONDS = 600.0  # after an action fails, leave that target alone this long


def _detect_hardware_profile() -> dict:
    """Detect low-spec hardware so the engine acts sooner and lighter on minimum-spec PCs.

    Returns {"low_ram", "low_cpu", "slow_disk", "ram_gb", "cpu_cores"}. Anything that can't
    be read defaults to "not low-spec", so the engine stays conservative when in doubt.
    """
    try:
        import psutil

        from app.system import hardware

        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        cpu_cores = psutil.cpu_count(logical=True) or 1
        drives = hardware.drives()
        has_hdd = any(d["kind"] == "hard disk" for d in drives)
        return {
            "low_ram": ram_gb < 8.0,
            "low_cpu": cpu_cores < 4,
            "slow_disk": has_hdd,
            "ram_gb": ram_gb,
            "cpu_cores": cpu_cores,
        }
    except Exception:
        return {"low_ram": False, "low_cpu": False, "slow_disk": False, "ram_gb": 0.0, "cpu_cores": 0}


_THE_GAME = tx("the game")  # when the game in focus isn't in the process list


def _minutes(seconds: float) -> str:
    return tx("{m} min", m=i18n.num(seconds / 60, ".0f"))


class DecisionEngine:
    def __init__(self) -> None:
        self._first_seen_hogging: dict[str, float] = {}
        self._first_seen_io: dict[str, float] = {}
        self._deprioritized: set[str] = set()
        self._efficiency: set[str] = set()
        self._io_lowered: set[str] = set()
        self._memory_lowered: set[str] = set()  # compressed apps: Low memory priority until used again
        self._confined: set[str] = set()  # Maximum focus mode: kept on one core while a game is in focus
        self._last_standby_purge = 0.0
        self._last_combine = float("-inf")
        self._last_power_plan_switch = 0.0
        self._current_power_plan_target: str | None = None
        self._paused_until: dict[str, float] = {}
        self._started: float | None = None
        self._last_foreground: dict[str, float] = {}  # app -> last time it was in front
        self._last_trim: dict[int, float] = {}  # pid -> last time its memory was trimmed
        self._recent_cpu: deque[float] = deque(maxlen=_BUSY_SAMPLES)
        self._calm_since: float | None = None
        self._game_seen = 0.0  # last time a game was in focus with Maximum mode on
        self._cores_released = False  # Maximum mode switched off: the optimizer already gave the cores back
        self._gpu_lowered: set[str] = set()  # background apps at Below normal GPU priority while a game is in focus
        self._first_seen_gpu: dict[str, float] = {}
        self._gpu_game_seen = 0.0  # last time a game was in focus (Focus mode on)
        self._pressure_since: float | None = None  # RAM at the line since (None = below it)
        self._under_pressure = False
        self._seen: dict[int, tuple[str, float, float]] = {}  # pid -> (name, first seen, last seen)
        self._looked = False  # the first look done: processes seen later count from when they appeared
        self._load: deque[float] = deque(maxlen=_COMBINE_CALM_SAMPLES)  # the last CPU readings, for page combining
        self._unreachable: dict[tuple[int, str], float] = {}  # (pid, app) the optimizer didn't find -> when
        self._hardware_profile: dict | None = None  # low-spec detection, done once
        self._hardware_checked = False
        self._game_opt = game_optimizer.GameOptimizer()  # extra GPU optimizations while a game plays

    def _measures(self) -> tuple[tuple[str, set[str]], ...]:
        """(label used by optimizer.held_back, the engine's own set) for each kind of hold-back."""
        return (("priority", self._deprioritized), ("efficiency", self._efficiency), ("io", self._io_lowered),
                ("memory", self._memory_lowered), ("cores", self._confined), ("gpu", self._gpu_lowered))

    def _sets_by_action(self) -> dict[ActionType, set[str]]:
        return {
            ActionType.LOWER_PRIORITY: self._deprioritized,
            ActionType.EFFICIENCY_MODE_ON: self._efficiency,
            ActionType.LOWER_IO_PRIORITY: self._io_lowered,
            ActionType.CONFINE_CORES: self._confined,
            ActionType.LOWER_GPU_PRIORITY: self._gpu_lowered,
        }

    def forget(self, target: str, action: ActionType | None = None, now: float | None = None,
               pause: float | None = None) -> None:
        """An action on this target failed (e.g. no permission), or found nothing to change
        (`pause`: the app chose its own setting): stop counting THAT measure as active and
        start no new ones on it for a while. Measures that did work stay tracked, so they
        are still undone when the app comes back to the foreground."""
        key = target.lower()
        for measure_action, managed in self._sets_by_action().items():
            if action is None or action == measure_action:
                managed.discard(key)
        if pause is None:
            pause = _COMBINE_RETRY_PAUSE_SECONDS if action == ActionType.COMBINE_MEMORY else _RETRY_PAUSE_SECONDS
        self._paused_until[key] = max(self._paused_until.get(key, 0.0), (time.time() if now is None else now) + pause)

    def unreachable(self, action: OptimizationAction, now: float | None = None) -> None:
        """The optimizer didn't find the processes this action picked, although they were listed
        (Windows names some differently, or they belong to another account): they aren't proposed
        again for 6 hours. Real log: a Steam helper was "not found" every 30 minutes for 5 hours."""
        now = time.time() if now is None else now
        for pid in action.pids:
            self._unreachable[(pid, action.target.lower())] = now

    def _unreached(self, proc: ProcessSample) -> bool:
        return (proc.pid, proc.name.lower()) in self._unreachable

    def postpone(self, actions: list[OptimizationAction]) -> None:
        """Actions past the per-cycle limit didn't run: they're proposed again next cycle."""
        sets = self._sets_by_action()
        for a in actions:
            key = a.target.lower()
            if a.action in sets:
                sets[a.action].discard(key)
            elif a.action == ActionType.TRIM_WORKING_SET:
                self._memory_lowered.discard(key)
                for pid in a.pids:
                    self._last_trim.pop(pid, None)
            elif a.action == ActionType.PURGE_STANDBY_LIST:
                self._last_standby_purge = 0.0
            elif a.action == ActionType.COMBINE_MEMORY:
                self._last_combine = float("-inf")
            elif a.action == ActionType.SWITCH_POWER_PLAN:
                self._current_power_plan_target = None
                self._last_power_plan_switch = 0.0

    def note_focus(self, events: list[tuple[str, float, bool]]) -> None:
        """Switches seen by Focus mode since the last cycle: (app, when, whether everything
        held back on it was already put back). Switches between two cycles still count."""
        for name, at, restored in events:
            self._last_foreground[name] = max(self._last_foreground.get(name, 0.0), at)
            if restored:
                for _, managed in self._measures():
                    managed.discard(name)

    def release_cores(self) -> None:
        """Maximum mode was switched off and every app already got its cores back."""
        self._cores_released = True

    def release_gpu(self) -> None:
        """Focus mode was switched off and every app already got its GPU priority back."""
        self._gpu_lowered.clear()
        self._first_seen_gpu.clear()

    def sync(self, held: dict[str, list[str]], now: float | None = None) -> None:
        """Real mode: `held` is what the optimizer really holds back. Apps it doesn't
        (nothing needed changing, the app closed, or Windows refused) are dropped and
        left alone for a while, instead of getting the same change proposed every cycle
        (unless you just used them: then they got everything back on purpose)."""
        now = time.time() if now is None else now
        for label, managed in self._measures():
            for name in list(managed):
                if label not in held.get(name, []):
                    managed.discard(name)
                    # (a compressed app whose memory priority was already low isn't re-proposed anyway)
                    if label != "memory" and now - self._last_foreground.get(name, float("-inf")) > _RECENT_USE_SECONDS:
                        self._paused_until[name] = max(self._paused_until.get(name, 0.0), now + _RETRY_PAUSE_SECONDS)

    def _paused(self, name_lower: str, now: float) -> bool:
        return self._paused_until.get(name_lower, 0.0) > now

    def managed_processes(self) -> dict[str, list[str]]:
        """Which processes the agent is holding back (as far as the engine knows: in
        test mode nothing really changes, so this is what it would hold back)."""
        managed: dict[str, list[str]] = {}
        for label, names in self._measures():
            for name in names:
                managed.setdefault(name, []).append(label)
        return dict(sorted(managed.items()))

    def decide(self, snapshot: SystemSnapshot, policy: OptimizationPolicy) -> list[OptimizationAction]:
        actions: list[OptimizationAction] = []
        now = time.time()
        if not self._hardware_checked:
            self._hardware_profile = _detect_hardware_profile()
            self._hardware_checked = True
        if self._started is None:
            self._started = now
        for key, at in list(self._unreachable.items()):
            if now - at > _UNREACHABLE_SECONDS:
                del self._unreachable[key]
        denylist = {p.lower() for p in policy.process_denylist_extra} | PROTECTED_PROCESSES
        # Actions target processes by name, so every instance of the app the
        # user is using (e.g. all chrome.exe) counts as foreground.
        foreground = {p.name.lower() for p in snapshot.top_processes if p.is_foreground}
        for name in foreground:
            self._last_foreground[name] = now
        focus_on = policy.enable_focus_mode
        game = focus_on and snapshot.foreground_game
        game_name = next((p.name for p in snapshot.top_processes if p.is_foreground), _THE_GAME)
        audible = {n.lower() for n in snapshot.audible_apps}
        kept_ready = {n.lower() for n in snapshot.recent_apps} if focus_on else set()
        hands_off = foreground | audible  # never held back

        actions += self._give_back_actions(snapshot, foreground, audible)

        if policy.enable_priority_management:
            actions += self._process_priority_actions(snapshot, policy, denylist, hands_off, now, game, game_name)

        if policy.enable_io_priority:
            actions += self._io_priority_actions(snapshot, policy, denylist, hands_off, now, game, game_name)

        if policy.enable_ram_reclaim:  # the AI's protected apps keep their priority, not their idle memory
            actions += self._ram_actions(snapshot, policy, PROTECTED_PROCESSES, hands_off, now, kept_ready, game)

        actions += self._cores_release_actions(policy, game, now)
        actions += self._gpu_actions(snapshot, denylist, hands_off, now, game, game_name)

        if policy.enable_power_plan_switch:
            action = self._power_plan_action(snapshot, policy, now, game, game_name)
            if action:
                actions.append(action)

        # Extra GPU optimizations for a game when the graphics card is heavily used:
        # shader cache, Game Mode, and their reversal when the game leaves focus or
        # GPU usage calms down.
        gpu_reading = gpu.primary() if IS_WINDOWS else None
        actions += self._game_opt.update(now, game, gpu_reading)

        return actions

    # -- giving things back -------------------------------------------------
    def _give_back_actions(self, snapshot, foreground, audible) -> list[OptimizationAction]:
        """An app the agent holds back that is in front again, or now playing sound, gets
        everything back at once. (In real mode with Focus mode on, the app you switch to
        got it back the moment you switched; this covers everything else.)"""
        actions: list[OptimizationAction] = []
        given: set[str] = set()
        for proc in snapshot.top_processes:
            name_lower = proc.name.lower()
            if name_lower in given or name_lower not in foreground | audible:
                continue
            if not any(name_lower in managed for _, managed in self._measures()):
                continue
            given.add(name_lower)
            for _, managed in self._measures():
                managed.discard(name_lower)
            actions.append(OptimizationAction(
                action=ActionType.RESTORE_APP,
                target=proc.name,
                reason=tx("Process is back in the foreground; putting back everything the agent changed.")
                if name_lower in foreground else
                tx("Playing sound; putting back everything the agent changed so the audio doesn't stutter."),
            ))
        return actions

    # -- priority / efficiency mode / cores ---------------------------------
    def _process_priority_actions(self, snapshot, policy, denylist, hands_off, now, game=False,
                                  game_name=_THE_GAME) -> list[OptimizationAction]:
        actions: list[OptimizationAction] = []
        hogs: dict[str, list[ProcessSample]] = {}
        threshold = _GAME_CPU_HOG_THRESHOLD if game else _CPU_HOG_THRESHOLD
        grace = policy.background_grace_period_seconds
        if game:
            grace = min(grace, _GAME_GRACE_SECONDS)
        # On a low-CPU PC (fewer than 4 logical processors), act sooner on background
        # CPU hogs and give them less grace: every background percent costs the foreground app.
        hw = self._hardware_profile or {}
        if hw.get("low_cpu"):
            threshold = min(threshold, 10.0 if not game else 7.0)
            grace = min(grace, 15.0)
        maximum = game and policy.focus_mode == "maximum"

        for proc in snapshot.top_processes:
            name_lower = proc.name.lower()
            if name_lower in denylist or name_lower in hands_off or proc.blocked_reason or self._unreached(proc):
                continue  # never touched, in front, playing sound, or not ours to change
            if proc.cpu_percent >= threshold and not self._paused(name_lower, now):
                hogs.setdefault(name_lower, []).append(proc)

        for name_lower, procs in hogs.items():
            first_seen = self._first_seen_hogging.setdefault(name_lower, now)
            elapsed = now - first_seen
            if elapsed < grace:
                continue
            busiest = max(procs, key=lambda p: p.cpu_percent)
            pids = [p.pid for p in procs]  # only the instances doing the work
            cpu, seconds = pct(sum(p.cpu_percent for p in procs)), i18n.num(elapsed, ".0f")
            using = (tx("Using {pct} CPU in the background for {s}s while {game} is in focus.", pct=cpu, s=seconds, game=game_name)
                     if game else tx("Using {pct} CPU in the background for {s}s without being in focus.", pct=cpu, s=seconds))
            if name_lower not in self._deprioritized:
                actions.append(OptimizationAction(
                    action=ActionType.LOWER_PRIORITY, target=busiest.name, pids=pids, reason=using,
                ))
                self._deprioritized.add(name_lower)
            if policy.enable_efficiency_mode and name_lower not in self._efficiency:
                actions.append(OptimizationAction(
                    action=ActionType.EFFICIENCY_MODE_ON,
                    target=busiest.name,
                    pids=pids,
                    reason=tx("Background CPU work: running it in Windows efficiency mode "
                              "(EcoQoS) to save power and keep the foreground app responsive."),
                ))
                self._efficiency.add(name_lower)
            if maximum and name_lower not in self._confined:
                actions.append(OptimizationAction(
                    action=ActionType.CONFINE_CORES,
                    target=busiest.name,
                    pids=pids,
                    reason=tx("{game} is in focus (Maximum focus mode): keeping this background work on one "
                              "core, so the game's cores and their caches aren't shared with it.", game=game_name),
                ))
                self._confined.add(name_lower)

        # forget processes that stopped using a lot of CPU
        for name in set(self._first_seen_hogging) - set(hogs):
            self._first_seen_hogging.pop(name, None)

        return actions

    def _cores_release_actions(self, policy, game, now) -> list[OptimizationAction]:
        """Apps kept on one core get every core back half a minute after the game left the
        focus (not on a quick Alt+Tab), or right away when Maximum mode is switched off."""
        maximum = policy.enable_focus_mode and policy.focus_mode == "maximum"
        if game and maximum:
            self._game_seen = now
        if self._cores_released:
            self._cores_released = False
            self._confined.clear()  # the optimizer already put them back
            return []
        if not self._confined or (maximum and now - self._game_seen < _CORES_RELEASE_SECONDS):
            return []
        actions = [OptimizationAction(
            action=ActionType.RESTORE_CORES,
            target=name,
            reason=tx("No game in focus any more; giving it all CPU cores back.")
            if maximum else tx("Maximum focus mode is off; giving it all CPU cores back."),
        ) for name in sorted(self._confined)]
        self._confined.clear()
        return actions

    # -- GPU priority (a game in focus) -----------------------------------------
    def _gpu_actions(self, snapshot, denylist, hands_off, now, game=False,
                     game_name=_THE_GAME) -> list[OptimizationAction]:
        """While a game is in focus, background apps using the GPU get Below normal GPU
        priority after 10 s, so the graphics card serves the game first. Half a minute
        after the game left the focus (not on a quick Alt+Tab) they get their own back.
        Apps recording or streaming the game are never touched."""
        actions: list[OptimizationAction] = []
        if not game:
            self._first_seen_gpu.clear()
            if self._gpu_lowered and now - self._gpu_game_seen >= _CORES_RELEASE_SECONDS:
                actions = [OptimizationAction(
                    action=ActionType.RESTORE_GPU_PRIORITY, target=name,
                    reason=tx("No game in focus any more; giving it its own GPU priority back."),
                ) for name in sorted(self._gpu_lowered)]
                self._gpu_lowered.clear()
            return actions

        self._gpu_game_seen = now
        hogs: dict[str, list[ProcessSample]] = {}
        for proc in snapshot.top_processes:
            name_lower = proc.name.lower()
            if (name_lower in denylist or name_lower in hands_off or name_lower in RECORDING_APPS
                    or proc.blocked_reason or self._unreached(proc) or self._paused(name_lower, now)):
                continue
            if proc.gpu_percent >= _GPU_HOG_PERCENT:
                hogs.setdefault(name_lower, []).append(proc)
        for name_lower, procs in hogs.items():
            first_seen = self._first_seen_gpu.setdefault(name_lower, now)
            if now - first_seen < _GAME_GRACE_SECONDS or name_lower in self._gpu_lowered:
                continue
            busiest = max(procs, key=lambda p: p.gpu_percent)
            actions.append(OptimizationAction(
                action=ActionType.LOWER_GPU_PRIORITY, target=busiest.name, pids=[p.pid for p in procs],
                reason=tx("Using {pct} of the GPU in the background while {game} is in focus; lowering its GPU "
                          "priority so the graphics card serves the game first.", pct=pct(busiest.gpu_percent), game=game_name),
            ))
            self._gpu_lowered.add(name_lower)
        for name in set(self._first_seen_gpu) - set(hogs):
            self._first_seen_gpu.pop(name, None)
        return actions

    # -- disk / I/O priority ---------------------------------------------------
    def _io_priority_actions(self, snapshot, policy, denylist, hands_off, now, game=False,
                             game_name=_THE_GAME) -> list[OptimizationAction]:
        actions: list[OptimizationAction] = []
        hogs: dict[str, list[ProcessSample]] = {}
        grace = policy.background_grace_period_seconds
        if game:
            grace = min(grace, _GAME_GRACE_SECONDS)

        for proc in snapshot.top_processes:
            name_lower = proc.name.lower()
            if name_lower in denylist or name_lower in hands_off or proc.blocked_reason or self._unreached(proc):
                continue
            if proc.io_bps >= _IO_HOG_BPS and not self._paused(name_lower, now):
                hogs.setdefault(name_lower, []).append(proc)

        for name_lower, procs in hogs.items():
            first_seen = self._first_seen_io.setdefault(name_lower, now)
            elapsed = now - first_seen
            if elapsed < grace or name_lower in self._io_lowered:
                continue
            busiest = max(procs, key=lambda p: p.io_bps)
            actions.append(OptimizationAction(
                action=ActionType.LOWER_IO_PRIORITY,
                target=busiest.name,
                pids=[p.pid for p in procs],
                reason=tx("Reading/writing {mbps} MB/s in the background for {s}s; {app} gets the disk first.",
                          mbps=i18n.num(sum(p.io_bps for p in procs) / (1024 * 1024), ".0f"), s=i18n.num(elapsed, ".0f"),
                          app=game_name if game else tx("the app in focus")),
            ))
            self._io_lowered.add(name_lower)

        for name in set(self._first_seen_io) - set(hogs):
            self._first_seen_io.pop(name, None)

        return actions

    # -- memory --------------------------------------------------------
    def _pressure(self, ram_percent: float, threshold: float, now: float) -> bool:
        """Memory pressure that lasts: RAM at the line for 10 s starts it, and it ends once RAM
        is 5 points below the line, so readings around the line don't switch it on and off."""
        if ram_percent >= threshold:
            if self._pressure_since is None:
                self._pressure_since = now
            if now - self._pressure_since >= _PRESSURE_SECONDS:
                self._under_pressure = True
        else:
            self._pressure_since = None
            if ram_percent < threshold - _PRESSURE_MARGIN:
                self._under_pressure = False
        return self._under_pressure

    def _note_seen(self, snapshot, now: float, started: float) -> None:
        """When each process was first seen: an app opened while OptiMind runs has been unused
        only since it appeared (it was compressed at once, as if idle since OptiMind started).
        The ones there at the first look count from OptiMind's start."""
        for proc in snapshot.top_processes:
            name = proc.name.lower()
            seen = self._seen.get(proc.pid)
            first = seen[1] if seen and seen[0] == name else started if not self._looked else now
            self._seen[proc.pid] = (name, first, now)
        self._looked = True
        for pid, (_, _, last) in list(self._seen.items()):
            if now - last > _SEEN_FORGET_SECONDS:
                del self._seen[pid]

    def _ram_actions(self, snapshot, policy, denylist, hands_off, now, kept_ready=frozenset(),
                     game=False) -> list[OptimizationAction]:
        actions: list[OptimizationAction] = []
        hw = self._hardware_profile or {}
        # On a low-RAM PC (under 8 GB), compress idle apps sooner and trim smaller hogs:
        # every MB freed goes to the app you're using.
        ram_hog_mb = _RAM_HOG_THRESHOLD_MB
        compress_min_mb = _IDLE_COMPRESS_MIN_MB
        compress_min_percent = _COMPRESS_MIN_RAM_PERCENT
        purge_ram_percent = _PURGE_RAM_PERCENT
        if hw.get("low_ram"):
            ram_hog_mb = min(ram_hog_mb, 150.0)
            compress_min_mb = min(compress_min_mb, 80.0)
            compress_min_percent = min(compress_min_percent, 40.0)
            purge_ram_percent = min(purge_ram_percent, 85.0)
        pressure = self._pressure(snapshot.ram_percent, policy.ram_high_threshold, now)
        compress = policy.enable_idle_compression and snapshot.ram_percent >= compress_min_percent
        started = now if self._started is None else self._started  # apps not seen in front: unused since then
        self._note_seen(snapshot, now, started)
        self._load.append(snapshot.cpu_percent)
        for pid, at in list(self._last_trim.items()):
            if now - at > 2 * _COMPRESS_COOLDOWN_SECONDS:
                self._last_trim.pop(pid)  # old entries (the process has most likely closed)

        if pressure or compress:
            candidates = sorted(snapshot.top_processes, key=lambda p: p.memory_mb, reverse=True)
            for proc in candidates:
                if len(actions) >= _MAX_TRIMS_PER_CYCLE:
                    break
                name_lower = proc.name.lower()
                if (name_lower in denylist or name_lower in hands_off or name_lower in kept_ready
                        or proc.blocked_reason or self._unreached(proc) or self._paused(name_lower, now)):
                    continue  # never compressed: in front, playing sound, or one of your last apps (Alt+Tab)
                unused = now - max(self._last_foreground.get(name_lower, started), self._seen[proc.pid][1])
                last_trim = self._last_trim.get(proc.pid)
                since_trim = float("inf") if last_trim is None else now - last_trim
                if (pressure and proc.memory_mb >= ram_hog_mb and proc.cpu_percent < _BUSY_CPU
                        and unused >= _RECENT_USE_SECONDS and since_trim >= _TRIM_COOLDOWN_SECONDS):
                    reason = tx("System RAM at {pct}; '{app}' holds {mb} MB in the background and isn't busy.",
                                pct=pct(snapshot.ram_percent), app=proc.name, mb=i18n.num(proc.memory_mb, ".0f"))
                elif (compress and proc.memory_mb >= compress_min_mb and proc.cpu_percent < _IDLE_CPU
                        and unused >= policy.idle_compress_minutes * 60 and since_trim >= _COMPRESS_COOLDOWN_SECONDS):
                    reason = tx("'{app}' has been idle in the background for {time}; compressing its {mb} MB so the "
                                "apps you use get the RAM.", app=proc.name, time=_minutes(unused),
                                mb=i18n.num(proc.memory_mb, ".0f"))
                else:
                    continue
                actions.append(OptimizationAction(
                    action=ActionType.TRIM_WORKING_SET, target=proc.name, pids=[proc.pid], reason=reason,
                ))
                self._last_trim[proc.pid] = now
                self._memory_lowered.add(name_lower)  # its memory priority goes Low with it

        if (
            pressure and snapshot.ram_percent >= max(policy.ram_high_threshold, purge_ram_percent)
            and snapshot.free_mb is not None and snapshot.standby_mb is not None
            and snapshot.free_mb < _PURGE_FREE_BELOW_MB
            and snapshot.standby_mb >= _PURGE_STANDBY_ABOVE_MB
            and (now - self._last_standby_purge) >= _STANDBY_PURGE_COOLDOWN_SECONDS
            and snapshot.agent_is_admin is not False  # Windows only allows this to administrators
            and not self._paused("system", now)  # paused after a failed attempt
        ):
            actions.append(OptimizationAction(
                action=ActionType.PURGE_STANDBY_LIST,
                target=tx("system"),
                reason=tx("RAM at {pct} with only {free} MB free while {standby} GB sits in the standby cache; "
                          "purging the cache.", pct=pct(snapshot.ram_percent), free=i18n.num(snapshot.free_mb, ".0f"),
                          standby=i18n.num(snapshot.standby_mb / 1024, ".1f")),
            ))
            self._last_standby_purge = now

        if (
            policy.enable_page_combining
            and snapshot.ram_percent >= _COMBINE_MIN_RAM_PERCENT
            and not game and not snapshot.game_running  # not while a game runs, even behind another window
            and max(self._load) < _COMBINE_MAX_CPU  # ...and only with the processor calm
            and now - started >= _COMBINE_FIRST_AFTER_SECONDS
            and now - self._last_combine >= _COMBINE_COOLDOWN_SECONDS
            and snapshot.agent_is_admin is not False
            and not self._paused(_COMBINE_TARGET, now)
        ):
            actions.append(OptimizationAction(
                action=ActionType.COMBINE_MEMORY,
                target=_COMBINE_TARGET,
                reason=tx("RAM at {pct}: asking Windows to merge identical memory pages (the same data held by "
                          "several apps) into one shared copy.", pct=pct(snapshot.ram_percent)),
            ))
            self._last_combine = now

        return actions

    # -- power plan ------------------------------------------------------
    def _power_plan_action(self, snapshot, policy, now, game=False, game_name=_THE_GAME) -> OptimizationAction | None:
        self._recent_cpu.append(snapshot.cpu_percent)
        cpu_threshold = policy.cpu_high_threshold
        hw = self._hardware_profile or {}
        # On a weak CPU, switch to the high-load plan sooner so the foreground app
        # isn't starved while background work catches up.
        if hw.get("low_cpu"):
            cpu_threshold = min(cpu_threshold, 70.0)
        busy = (len(self._recent_cpu) == _BUSY_SAMPLES
                and sum(self._recent_cpu) / _BUSY_SAMPLES >= cpu_threshold)
        if snapshot.cpu_percent < policy.cpu_high_threshold - _CALM_MARGIN:
            self._calm_since = self._calm_since or now
        else:
            self._calm_since = None
        calm = self._calm_since is not None and now - self._calm_since >= _CALM_SECONDS
        away = (
            snapshot.user_idle_seconds is not None
            and snapshot.user_idle_seconds >= policy.user_idle_minutes * 60
        )
        on_battery = snapshot.on_battery is True
        playing = game and not away and not on_battery
        # The high-performance plan only makes sense while someone is using
        # the PC and it is plugged in; otherwise go back to the idle plan.
        if playing or (busy and not away and not on_battery):
            target_plan = policy.high_load_power_plan
        elif calm or away or on_battery:
            target_plan = policy.idle_power_plan
        else:
            return None  # in between: keep the current plan
        if snapshot.power_plans is not None and target_plan not in snapshot.power_plans:
            return None  # this PC doesn't have it (on many laptops Windows' Power mode replaces High performance)

        already_there = snapshot.power_plan and target_plan.lower() in snapshot.power_plan.lower()
        same_target_as_last_switch = target_plan == self._current_power_plan_target
        on_cooldown = (now - self._last_power_plan_switch) < _POWER_PLAN_COOLDOWN_SECONDS

        # Propose each target only once: if the plan didn't change afterwards
        # (test mode, or the user switched it back by hand), leave it alone until
        # the load calls for a different plan. The cooldown stops flapping
        # between plans when the CPU hovers around the threshold.
        if already_there or same_target_as_last_switch or on_cooldown:
            return None

        if target_plan == policy.high_load_power_plan:
            why = (tx("a game is in focus ({game})", game=game_name) if playing
                   else tx("CPU at {pct} for the last few seconds", pct=pct(sum(self._recent_cpu) / _BUSY_SAMPLES)))
        elif on_battery:
            why = tx("on battery ({pct})", pct=pct(snapshot.battery_percent or 0))
        elif away:
            why = tx("no keyboard/mouse input for {m} min", m=i18n.num(snapshot.user_idle_seconds / 60, ".0f"))
        else:
            why = tx("CPU calm (now {pct}) for {m} min", pct=pct(snapshot.cpu_percent), m=i18n.num(_CALM_SECONDS / 60, ".0f"))

        self._last_power_plan_switch = now
        self._current_power_plan_target = target_plan
        return OptimizationAction(
            action=ActionType.SWITCH_POWER_PLAN,
            target=target_plan,
            reason=tx("{why} — switching power plan to '{plan}'.", why=i18n.Capital(why),
                      plan=PLAN_NAMES.get(target_plan, target_plan)),
        )
