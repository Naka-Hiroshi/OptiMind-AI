"""
Orchestrator: two async loops running in parallel.

  - fast_loop: every OPTIMIND_FAST_LOOP_SECONDS. Observes (monitor),
    decides (decision_engine) and applies (optimizer). It's all local,
    without calling the AI — it has to be fast and cheap.

  - ai_loop: every OPTIMIND_AI_LOOP_SECONDS. Asks the first available
    free AI provider (policy_brain) to review the policy based on the
    history. If every provider fails (no internet, no API key, quota
    used up), the built-in rule-based brain does the review instead —
    the AI is an enhancement, not a critical real-time dependency.

Plus Focus mode's own thread (app/agent/focus.py), which reacts the
instant you switch apps, between two fast-loop cycles.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from app import i18n
from app.agent import focus, games, optimizer, policy_store
from app.agent.decision_engine import DecisionEngine
from app.agent.logger import log_action, read_recent_actions
from app.agent.optimizer import apply_actions
from app.agent.safety import within_limit
from app.config import settings
from app.i18n import tx
from app.models import ActionLogEntry, ActionType, OptimizationAction, OptimizationPolicy
from app.service.state import app_state
from app.system.monitor import append_history, prime_cpu_counters, take_snapshot

logger = logging.getLogger("optimind.orchestrator")

_engine = DecisionEngine()
_SELF_TRIM_SECONDS = 600.0  # how often OptiMind hands its own idle memory back to Windows


def set_focus(enabled: bool, mode: str) -> OptimizationPolicy:
    """Focus mode on/off and Balanced/Maximum (dashboard or tray), saved with the policy.
    Leaving Maximum gives every app its cores back right away."""
    previous_mode = focus.mode_in_effect()
    policy = app_state.current_policy.model_copy(update={"enable_focus_mode": enabled, "focus_mode": mode})
    app_state.current_policy = policy
    policy_store.save_policy(policy)
    if previous_mode != (mode if enabled else "off"):
        games.focus_mode_changed()
    if not enabled or mode != "maximum":
        if optimizer.restore_field("cores"):
            logger.info("Gave background apps all CPU cores back (Maximum focus mode off)")
        _engine.release_cores()
    if not enabled:
        if optimizer.restore_field("gpu"):
            logger.info("Gave background apps their GPU priority back (Focus mode off)")
        _engine.release_gpu()
        # Focus mode off: put back any game-GPU optimizations that were applied.
        for entry in _engine._game_opt.restore_all():
            log_action(entry)
    if enabled:
        focus.refresh()
    else:
        focus.release()
    return policy


def keep_user_choices(reviewed: OptimizationPolicy) -> OptimizationPolicy:
    """A policy review takes seconds: Focus mode choices you made meanwhile are kept
    (the AI never changes them anyway), and so are the apps you took off its protected list."""
    current = app_state.current_policy
    return policy_store.settle_protected(reviewed.model_copy(update={
        "enable_focus_mode": current.enable_focus_mode, "focus_mode": current.focus_mode,
        "unprotected_by_you": current.unprotected_by_you}))


def unprotect(name: str) -> OptimizationPolicy:
    """Takes an app off the AI's protected list (AI page): the agent may hold it back again,
    and the AI can't put it back. LookupError for an app that isn't on the list."""
    current = app_state.current_policy
    key = name.strip().lower()
    if key not in {p.lower() for p in current.process_denylist_extra}:
        raise LookupError(tx("{app} isn't on the protected list", app=name))
    policy = policy_store.settle_protected(current.model_copy(update={
        "unprotected_by_you": sorted({*(n.lower() for n in current.unprotected_by_you), key})}))
    app_state.current_policy = policy
    policy_store.save_policy(policy)
    logger.info("Took %s off the AI's protected apps", name)
    return policy


# Actions that start holding something back; if one fails, the engine is told.
_HOLD_BACK_ACTIONS = {
    ActionType.LOWER_PRIORITY, ActionType.EFFICIENCY_MODE_ON, ActionType.LOWER_IO_PRIORITY,
    ActionType.SET_AFFINITY, ActionType.TRIM_WORKING_SET, ActionType.PURGE_STANDBY_LIST,
    ActionType.CONFINE_CORES, ActionType.COMBINE_MEMORY, ActionType.LOWER_GPU_PRIORITY,
}


_UNCHANGED_PAUSE_SECONDS = 3600.0  # an app that chose its own setting isn't asked again for an hour


def _release_failed(entries: list[ActionLogEntry], actions: list[OptimizationAction] | None = None) -> None:
    """When Windows refuses a change, the engine stops counting it as active
    and leaves that target alone for a while instead of retrying every cycle.
    An app whose setting was left as it was (set high on purpose, or already low)
    is left alone longer: asking again in 10 minutes would only give the same answer.
    Processes the optimizer didn't find (`actions`, in the entries' order) aren't picked again."""
    for i, entry in enumerate(entries):
        if entry.action not in _HOLD_BACK_ACTIONS:
            continue
        if not entry.applied and entry.result.startswith("error"):
            _engine.forget(entry.target, entry.action)
            logger.warning("Couldn't apply %s to %s (%s); leaving it alone for 10 minutes",
                           entry.action.value, entry.target, entry.result)
        elif entry.applied and entry.result.startswith("nothing changed"):
            _engine.forget(entry.target, entry.action, pause=_UNCHANGED_PAUSE_SECONDS)
        elif entry.result.startswith(optimizer.NOT_FOUND) and actions and i < len(actions):
            _engine.unreachable(actions[i])


def _load_recent_actions(n: int = 200) -> list[ActionLogEntry]:
    """Actions from earlier runs, so the dashboard isn't empty after a restart."""
    entries = []
    for row in read_recent_actions(n):
        try:
            entries.append(ActionLogEntry.model_validate(row))
        except Exception:
            continue  # entry written by an older version
    return entries


async def fast_loop() -> None:
    prime_cpu_counters()
    await asyncio.sleep(1.0)  # let the first cpu_percent "warm up"
    last_self_trim = time.monotonic() - _SELF_TRIM_SECONDS + 60  # first time a minute after start

    while True:
        try:
            # Sampling ~300 processes takes a moment, so it runs in a thread
            # to keep the API/dashboard responsive meanwhile.
            snapshot = await asyncio.to_thread(take_snapshot)
            snapshot.game_running = games.running()  # a game behind another window still counts (page combining)
            append_history(snapshot)
            app_state.latest_snapshot = snapshot

            _engine.note_focus(focus.drain_events())  # app switches since the last cycle
            actions, waiting = within_limit(_engine.decide(snapshot, app_state.current_policy))
            _engine.postpone(waiting)  # past the per-cycle limit: proposed again next cycle
            for a in actions:
                a.dry_run = app_state.dry_run
            entries = await asyncio.to_thread(
                apply_actions, actions, app_state.current_policy.process_denylist_extra
            )
            _release_failed(entries, actions)
            for entry in focus.drain_log():  # what Focus mode did between cycles
                log_action(entry)
                entries.append(entry)
            if app_state.dry_run:
                app_state.managed_processes = _engine.managed_processes()  # what it would hold back
            else:
                held = await asyncio.to_thread(optimizer.held_back)  # what really is in effect
                _engine.sync(held)
                app_state.managed_processes = held

            if entries:
                app_state.add_actions(entries)

            try:  # game sessions: what the game in front gets from the PC (app/agent/games.py)
                await asyncio.to_thread(games.update, snapshot)
            except Exception:
                logger.exception("Game session tracking failed this cycle")

            if time.monotonic() - last_self_trim >= _SELF_TRIM_SECONDS:
                optimizer.trim_self()
                last_self_trim = time.monotonic()
            optimizer.adaptive_self_optimize()  # OptiMind yields to your apps when the PC is busy

            app_state.fast_loop_ticks += 1
        except Exception:
            logger.exception("Error in fast_loop (cycle skipped, agent keeps running)")

        await asyncio.sleep(settings.optimind_fast_loop_seconds)


async def ai_loop() -> None:
    # First review only after we have a minimum amount of history.
    await asyncio.sleep(min(120, settings.optimind_ai_loop_seconds))

    while True:
        try:
            from app.ai.policy_brain import review_policy  # lazy import: only loaded when needed

            # The provider call is blocking, so run it in a thread to keep the fast loop going.
            updated, provider, failures = await asyncio.to_thread(review_policy, app_state.current_policy)
            for reason in failures:
                logger.warning("AI provider unavailable, trying the next one: %s", reason)
            updated = keep_user_choices(updated)
            app_state.current_policy = updated
            app_state.ai_last_provider = provider
            app_state.ai_loop_last_error = i18n.joined(failures, "; ") or None
            policy_store.save_policy(updated)
            policy_store.append_history(updated, provider)
            logger.info("Policy updated by %s: %s", provider, updated.notes)
        except Exception as e:
            app_state.ai_loop_last_error = i18n.message(e)
            logger.exception("Error in ai_loop (keeping the current policy)")
        finally:
            app_state.ai_loop_last_run = datetime.now(timezone.utc).isoformat()

        await asyncio.sleep(settings.optimind_ai_loop_seconds)


async def run_forever() -> None:
    app_state.current_policy = policy_store.load_policy()
    policy_store.save_policy(app_state.current_policy)  # when each protected app was added, kept from the first start
    # OPTIMIND_DRY_RUN wins over the settings file: tests/__init__.py sets it in the
    # environment, but app.config may already have been imported by then (import order
    # depends on which test module loads first), so read the env var here too.
    env_dry_run = os.environ.get("OPTIMIND_DRY_RUN")
    app_state.dry_run = (
        env_dry_run.strip().lower() in ("true", "1", "yes", "on")
        if env_dry_run is not None
        else settings.optimind_dry_run
    )
    if app_state.dry_run:
        logger.warning("Test mode (OPTIMIND_DRY_RUN=true): the agent only logs what it would do")
    app_state.recent_actions = _load_recent_actions()
    optimizer.lighten_self()
    # If the last run was closed abruptly, put back what it had left changed.
    restored = await asyncio.to_thread(optimizer.restore_saved)
    if restored:
        logger.info("Put back the settings of %d app process(es) left changed by the last run", restored)
    focus.start(lambda: (app_state.current_policy.enable_focus_mode, app_state.current_policy.focus_mode,
                         app_state.dry_run))
    await asyncio.gather(fast_loop(), ai_loop())
