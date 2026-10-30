from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException

from app import __version__, i18n
from app.agent import focus, impact, policy_store
from app.agent.logger import log_action, remove_actions, result_status, shown_entry
from app.ai import monitor_brain
from app.ai.providers import provider_stats
from app.api import LocalizedRoute
from app.config import settings
from app.desktop import confirm
from app.i18n import tx
from app.models import (ActionLogEntry, ActionType, ActivityRemoval, AnomalyReport, ChatRequest, ChatResponse,
                        CleanupRequest, CloseAppRequest, FocusRequest, Monitoring24h, OptimizationPolicy,
                        PerformanceBaselineEntry, TrendReport, UnprotectRequest)
from app.service import orchestrator
from app.service.state import app_state
from app.system import hardware, monitor, processes, sysinfo
from app.system.admin_rights import is_elevated
from app.tools import cleanup, close_app, restore_point, startup_manager, tweaks

router = APIRouter(route_class=LocalizedRoute)

_MAX_HISTORY_MINUTES = 60

StartupScope = Literal["user", "machine"]  # your startup apps, or the ones for all users


@router.get("/status")
def get_status() -> dict:
    return {
        "version": __version__,
        "snapshot": app_state.latest_snapshot,
        "policy": app_state.current_policy,
        "dry_run": app_state.dry_run,  # test mode (OPTIMIND_DRY_RUN): the agent only logs
        "fast_loop_ticks": app_state.fast_loop_ticks,
        "ai_loop_last_run": app_state.ai_loop_last_run,
        "ai_loop_last_error": app_state.ai_loop_last_error,
        "ai_last_provider": app_state.ai_last_provider,
        "ai_providers": provider_stats(),
        "managed_processes": app_state.managed_processes,
        "focus": focus.status(),
    }


@router.get("/focus")
def get_focus() -> dict:
    """Focus mode: the app in front, whether it's boosted or a game, the apps kept ready for Alt+Tab."""
    return focus.status()


@router.post("/focus")
def set_focus(req: FocusRequest) -> dict:
    """Focus mode on/off and Balanced/Maximum (remembered with the policy; the AI never changes it)."""
    policy = orchestrator.set_focus(req.enabled, req.mode)
    return {"enabled": policy.enable_focus_mode, "mode": policy.focus_mode, "focus": focus.status()}


@router.get("/system")
def get_system() -> dict:
    """Hardware, per-core CPU, memory composition, GPU, drives, network, uptime."""
    return sysinfo.live_details()


@router.get("/hardware")
def get_hardware(refresh: bool = False) -> dict:
    """Hardware check (read-only): RAM speed and channels, PCIe links, drives and where
    your games are, Memory integrity, GPU scheduling, Game Mode — with what to do."""
    return hardware.report(refresh)


@router.get("/services")
def get_services() -> dict:
    """Windows services with publisher and memory use (read-only, cached for a minute)."""
    return sysinfo.services()


@router.get("/history")
def get_history(minutes: float = 15) -> list[dict]:
    """CPU/RAM samples from the metrics history (one per fast-loop cycle)."""
    minutes = max(1.0, min(minutes, _MAX_HISTORY_MINUTES))
    samples = int(minutes * 60 / settings.optimind_fast_loop_seconds)
    return [{"t": h["t"], "cpu": h["cpu"], "ram": h["ram"]} for h in monitor.read_recent_history(samples)]


@router.get("/impact")
def get_impact(days: int = 7) -> dict:
    """What OptiMind changed over the last days, in numbers, and games measured with Focus mode
    Off and on, side by side."""
    return impact.summary(max(1, min(days, 90)))


@router.get("/logs")
def get_logs(limit: int = 50) -> list:
    """The latest activity entries, each with its label (status: Applied, Blocked, Error, Logged
    only or Unchanged), whether you made the change, and the memory it freed."""
    return [shown_entry(e) for e in app_state.recent_actions[-limit:]]


def _utc(t: datetime | None) -> datetime | None:
    """Log times are UTC without a time zone; times from the dashboard carry one."""
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t and t.tzinfo else t


@router.post("/logs/remove")
async def remove_logs(req: ActivityRemoval) -> dict:
    """Removes activity entries from the log file and the list (the AI no longer sees them
    either). Nothing the agent did is undone. async: it runs on the event loop, like the
    agent's own updates of the list, so an entry being added meanwhile isn't lost."""
    keys = {(_utc(k.timestamp), k.action, k.target) for k in req.entries}
    since, until = _utc(req.since), _utc(req.until)

    def picked(e: ActionLogEntry) -> bool:
        t = _utc(e.timestamp)
        return ((not keys or (t, e.action.value, e.target) in keys)
                and (since is None or t >= since) and (until is None or t < until)
                and (req.status is None or result_status(e) == req.status))

    everything = req.everything and not (keys or since or until or req.status)
    removed = remove_actions(None if everything else picked)
    with app_state.actions_lock:
        app_state.recent_actions = [] if everything else [e for e in app_state.recent_actions if not picked(e)]
    return {"removed": removed}


@router.get("/policy", response_model=OptimizationPolicy)
def get_policy() -> OptimizationPolicy:
    return app_state.current_policy


@router.get("/policy/history")
def get_policy_history(limit: int = 20) -> list[dict]:
    """Past policy reviews: who made them (AI provider or local rules) and why."""
    return policy_store.read_history(limit)


@router.get("/monitoring/24h", response_model=Monitoring24h)
def get_monitoring_24h() -> Monitoring24h:
    """24-hour monitoring summary: baselines, anomalies, trends — what the AI sees."""
    history = monitor.read_recent_history(monitor_brain._HISTORY_SAMPLES if hasattr(monitor_brain, '_HISTORY_SAMPLES') else 450)
    baseline = monitor_brain.build_baseline(history)
    anomalies = monitor_brain.detect_anomalies(history, baseline)
    trends = monitor_brain.analyze_trends(history)

    # Update app_state so the dashboard can show it at a glance
    app_state.ai_baseline_saved = datetime.now(timezone.utc).isoformat()
    app_state.ai_anomaly_count = len(anomalies)
    app_state.ai_performance_trend = trends.get("trend", "unknown")

    return Monitoring24h(
        baseline=[
            PerformanceBaselineEntry(
                hour=h,
                cpu_p25=baseline.hourly[h].get("cpu", {}).get("p25", 0.0),
                cpu_p50=baseline.hourly[h].get("cpu", {}).get("p50", 0.0),
                cpu_p75=baseline.hourly[h].get("cpu", {}).get("p75", 0.0),
                ram_p25=baseline.hourly[h].get("ram", {}).get("p25", 0.0),
                ram_p50=baseline.hourly[h].get("ram", {}).get("p50", 0.0),
                ram_p75=baseline.hourly[h].get("ram", {}).get("p75", 0.0),
                samples=baseline.hourly[h].get("cpu", {}).get("count", 0),
            )
            for h in sorted(baseline.hourly)
        ],
        anomalies=[
            AnomalyReport(
                when=a.when,
                metric=a.metric,
                value=a.value,
                baseline_p75=a.baseline_p75,
                severity=a.severity,
                context=a.context,
            )
            for a in anomalies
        ],
        trend=TrendReport(
            trend=trends.get("trend", "unknown"),
            note=trends.get("note", ""),
            recent_cpu_avg=trends.get("recent_cpu_avg"),
            previous_cpu_avg=trends.get("previous_cpu_avg"),
            recent_ram_avg=trends.get("recent_ram_avg"),
            previous_ram_avg=trends.get("previous_ram_avg"),
        ),
        samples=baseline.samples,
        days_used=baseline.days_used,
    )


@router.get("/monitoring/daily")
def get_monitoring_daily(days: int = 30) -> list[dict]:
    """Daily aggregate history for the last `days` days (longer-term trends)."""
    days = max(1, min(days, 90))
    return monitor.read_daily_history(days)


@router.post("/policy/unprotect")
def unprotect_app(req: UnprotectRequest) -> dict:
    """Takes an app off the AI's protected apps: the agent may change its priority again, and the
    AI won't put it back on the list."""
    try:
        return {"policy": orchestrator.unprotect(req.name)}
    except LookupError as e:
        raise HTTPException(status_code=404, detail=i18n.message(e))


@router.get("/startup-items")
def get_startup_items() -> list[dict]:
    return startup_manager.list_startup_items()


def _logged_change(action: ActionType, target: str, reason: str, change, errors: tuple, status_code: int = 400) -> dict:
    """Changes you make yourself (dashboard/API) apply right away (in test mode
    too, except closing an app, which only says what it would close), and are
    written to the action log like the agent's."""
    failed = False
    try:
        result = change()
    except errors as e:
        result, failed = i18n.message(e), True
        status_code = getattr(e, "status", status_code)
    entry = ActionLogEntry(action=action, target=target, reason=reason, result=result,
                           applied=not failed and not result.startswith("simulated"))
    log_action(entry)
    app_state.add_actions([entry])
    if failed:
        raise HTTPException(status_code=status_code, detail=result)
    return {"result": result}


def _manual_startup_change(action: ActionType, name: str, scope: StartupScope, change) -> dict:
    reason = (tx("Changed by you from the dashboard/API (startup app for all users).") if scope == "machine"
              else tx("Changed by you from the dashboard/API."))
    return _logged_change(action, name, reason, lambda: change(name, scope), (startup_manager.StartupItemError,), 404)


@router.post("/startup-items/{name}/disable")
def disable_startup_item(name: str, scope: StartupScope = "user") -> dict:
    """MANUAL disabling (outside the automatic policy) — requires the
    user to confirm explicitly via the API/dashboard; the fast loop never
    does it on its own unless the item is on the safe allowlist.
    `scope=machine` changes an app that starts for all users, which needs
    OptiMind to run as administrator."""
    return _manual_startup_change(ActionType.DISABLE_STARTUP_ITEM, name, scope, startup_manager.disable_startup_item)


@router.post("/startup-items/{name}/enable")
@router.post("/startup-items/{name}/restore")
def enable_startup_item(name: str, scope: StartupScope = "user") -> dict:
    return _manual_startup_change(ActionType.ENABLE_STARTUP_ITEM, name, scope, startup_manager.enable_startup_item)


@router.get("/processes")
def get_processes() -> dict:
    """Every running program, grouped like Task Manager (from the last fast-loop cycle)."""
    return processes.app_groups(app_state.managed_processes)


# ---- Optimize tab: tweaks, restore point and clean-up (only when you ask) ----
_BY_YOU = tx("Changed by you from the Optimize tab.")


@router.get("/tweaks")
def get_tweaks() -> dict:
    """Windows 11 tweaks with their current state and what each would free right now."""
    return tweaks.list_tweaks()


@router.post("/tweaks/{tweak_id}/apply")
def apply_tweak(tweak_id: str) -> dict:
    """Applies one tweak. A security trade-off also needs your Yes in OptiMind's own dialog."""
    tweak = tweaks.find(tweak_id)
    if tweak is not None and tweak.risk == "security" and not confirm.ask(
            tx('Apply "{tweak}"?', tweak=tweak.title),
            tx("{note}\n\nUndo on the Optimize page turns it back on.", note=tweak.risk_note)):
        raise HTTPException(status_code=409, detail=tx("Not applied: it wasn't confirmed in OptiMind's own dialog."))
    return _logged_change(ActionType.APPLY_TWEAK, tweaks.title(tweak_id), _BY_YOU, lambda: tweaks.apply(tweak_id),
                          (tweaks.TweakError,))


@router.post("/tweaks/{tweak_id}/undo")
def undo_tweak(tweak_id: str) -> dict:
    return _logged_change(ActionType.UNDO_TWEAK, tweaks.title(tweak_id), _BY_YOU, lambda: tweaks.undo(tweak_id),
                          (tweaks.TweakError,))


@router.post("/tweaks/apply-safe")
def apply_safe_tweaks() -> dict:
    """Creates a restore point first (with administrator rights), then applies
    every tweak marked safe that isn't applied yet."""
    ids = tweaks.safe_tweak_ids()
    point = None
    if ids and is_elevated():
        try:
            point = create_restore_point()["result"]
        except HTTPException as e:
            point = tx("not created: {reason}", reason=e.detail)
    elif ids:
        point = tx("skipped: needs administrator rights")
    results = []
    for tweak_id in ids:
        try:
            results.append({"id": tweak_id, "ok": True, "result": apply_tweak(tweak_id)["result"]})
        except HTTPException as e:
            results.append({"id": tweak_id, "ok": False, "result": e.detail})
    return {"restore_point": point, "results": results}


@router.get("/restore-point")
def get_restore_point(refresh: bool = False) -> dict:
    return restore_point.status(refresh)


@router.post("/restore-point")
def create_restore_point() -> dict:
    return _logged_change(ActionType.RESTORE_POINT, "system", _BY_YOU, restore_point.create,
                          (restore_point.RestorePointError,))


@router.get("/cleanup")
def get_cleanup(refresh: bool = False) -> dict:
    """How much disk space each clean-up location would free."""
    return cleanup.scan(refresh)


@router.post("/cleanup")
def run_cleanup(req: CleanupRequest) -> dict:
    outcome: dict = {}

    def clean() -> str:
        outcome.update(cleanup.run(req.ids))
        return tx("freed {mb} MB ({what})", mb=i18n.num(outcome["freed_bytes"] / 1024 ** 2),
                  what=i18n.joined([r["title"] for r in outcome["results"]], ", "))

    result = _logged_change(ActionType.CLEAN_UP, cleanup.titles(req.ids), _BY_YOU, clean, (cleanup.CleanupError,))
    return {**outcome, **result}


@router.post("/apps/close")
def close_an_app(req: CloseAppRequest) -> dict:
    """Closes an app you picked (Games page, Before you play) as its own X does: each of its
    windows is asked to close, so it saves or asks about your work; nothing is ended. In test
    mode it only says what it would close."""
    outcome: dict = {}

    def close() -> str:
        outcome.update(close_app.close(req.name, dry_run=app_state.dry_run))
        return outcome["result"]

    result = _logged_change(ActionType.CLOSE_APP, req.name, tx("Changed by you from the Games page (Before you play): "
                            "its windows were asked to close, as its own X does."), close, (close_app.CloseError,))
    return {**outcome, **result}


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        from app.ai.chat import ask

        reply, provider = ask(req.message, app_state.latest_snapshot)
        return ChatResponse(reply=i18n.shown(reply), provider=provider)  # (the built-in rules' summary is a text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=tx("Failed to query the AI: {error}", error=i18n.message(e)))


@router.post("/policy/refresh-now")
def refresh_policy_now() -> dict:
    """Forces a policy review right now (outside the normal interval),
    useful for demos."""
    try:
        from app.ai.policy_brain import review_policy

        updated, provider, failures = review_policy(app_state.current_policy)
        updated = orchestrator.keep_user_choices(updated)
        app_state.current_policy = updated
        app_state.ai_last_provider = provider
        policy_store.save_policy(updated)
        policy_store.append_history(updated, provider)
        return {"policy": updated, "provider": provider, "failed_providers": failures}
    except Exception as e:
        raise HTTPException(status_code=502, detail=tx("Failed to query the AI: {error}", error=i18n.message(e)))
