"""
Built-in rule-based "brain" — the free, offline fallback when no AI
provider is available. It does the same two jobs as the AI, with
simple, explainable rules instead of a language model:

  - propose_policy: tunes the policy from the metrics history and the
    action log;
  - answer: summarizes what the agent has been doing (it does not
    interpret the question — that needs a real AI provider).
"""
from __future__ import annotations

from collections import Counter

from app import i18n
from app.i18n import pct, tx, txc
from app.models import OptimizationPolicy, SystemSnapshot, utc_now

_MIN_SAMPLES = 30
_RAISE_COUNT_TO_PROTECT = 3

_ACTION_LABELS = {
    "lower_priority": tx("lowered priority"),
    "raise_priority": tx("restored priority"),
    "efficiency_mode_on": tx("turned efficiency mode on"),
    "efficiency_mode_off": tx("turned efficiency mode off"),
    "lower_io_priority": tx("lowered I/O priority"),
    "restore_io_priority": tx("restored I/O priority"),
    "set_affinity": tx("limited CPU cores"),
    "trim_working_set": tx("compressed memory"),
    "purge_standby_list": tx("purged the standby list"),
    "restore_app": tx("gave an app its settings back"),
    "confine_cores": tx("kept a background app on one core"),
    "restore_cores": tx("gave an app all CPU cores back"),
    "combine_memory": tx("merged identical memory pages"),
    "lower_gpu_priority": tx("lowered a background app's GPU priority while you played"),
    "restore_gpu_priority": tx("gave an app its GPU priority back"),
    "focus_boost": tx("noted a game in focus"),
    "disable_startup_item": tx("disabled a startup app"),
    "enable_startup_item": tx("enabled a startup app"),
    "switch_power_plan": tx("switched power plan"),
    "apply_tweak": tx("applied a Windows tweak"),
    "undo_tweak": tx("undid a Windows tweak"),
    "clean_up": tx("cleaned up disk space"),
    "restore_point": tx("created a restore point"),
    "close_app": tx("closed an app you picked"),
}
# Who set the policy, as the summary names them (the policy's own word in English)
_SET_BY = {"ai": txc("set by", "ai"), "local": txc("set by", "local"), "default": txc("set by", "default")}
_LIMITED_BY = {"gpu": tx("the graphics card"), "cpu": tx("the processor"), "cap": tx("a frame cap")}


def _with_notes(policy: OptimizationPolicy, notes: i18n.Text, **update) -> OptimizationPolicy:
    """The policy with the built-in rules' notes, which can be shown in any language."""
    return policy.model_copy(update={**update, "notes": notes, "notes_i18n": notes.to_json()})


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * pct / 100)]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def propose_policy(
    current: OptimizationPolicy, history: list[dict], actions: list[dict]
) -> OptimizationPolicy:
    now = utc_now()
    if len(history) < _MIN_SAMPLES:
        return _with_notes(current, tx("Not enough history yet ({have}/{need} samples); policy kept as is.",
                                       have=len(history), need=_MIN_SAMPLES), updated_by="local", updated_at=now)

    cpu_p75 = _percentile([h["cpu"] for h in history], 75)
    ram_p75 = _percentile([h["ram"] for h in history], 75)

    # Put the "high load" line above this machine's normal busy level,
    # so the power plan only switches on real spikes instead of flapping.
    cpu_threshold = _clamp(round(cpu_p75 + 15), 60, 95)
    ram_threshold = _clamp(round(ram_p75 + 10), 70, 92)
    # The fuller the RAM usually is, the sooner idle background apps get compressed.
    idle_minutes = 5.0 if ram_p75 >= 75 else 20.0 if ram_p75 < 50 else 10.0

    # Apps that keep coming back to the foreground after being
    # deprioritized are ones the user actively uses: stop touching them.
    raised = Counter(a["target"].lower() for a in actions if a["action"] in ("raise_priority", "restore_app"))
    already = {p.lower() for p in current.process_denylist_extra + current.unprotected_by_you}  # (you took those off)
    newly_protected = sorted(
        name for name, n in raised.items() if n >= _RAISE_COUNT_TO_PROTECT and name not in already
    )

    # A lot of lower/raise flapping means we deprioritize too eagerly.
    grace = current.background_grace_period_seconds
    if sum(raised.values()) >= 5:
        grace = min(grace * 2, 120.0)

    notes = [
        tx("Typical load (75th percentile): CPU {cpu}, RAM {ram}.", cpu=pct(cpu_p75), ram=pct(ram_p75)),
        tx("High-load thresholds set to CPU {cpu} and RAM {ram}.", cpu=pct(cpu_threshold), ram=pct(ram_threshold)),
        tx("Idle background apps get their memory compressed after {m} min.", m=i18n.num(idle_minutes, ".0f")),
    ]
    if newly_protected:
        notes.append(tx("Now protecting actively used apps: {apps}.", apps=", ".join(newly_protected)))
    if grace != current.background_grace_period_seconds:
        notes.append(tx("Grace period raised to {s}s to reduce priority flapping.", s=i18n.num(grace, ".0f")))

    return _with_notes(current, i18n.joined(notes, " "),
                       cpu_high_threshold=cpu_threshold,
                       ram_high_threshold=ram_threshold,
                       background_grace_period_seconds=grace,
                       idle_compress_minutes=idle_minutes,
                       process_denylist_extra=current.process_denylist_extra + newly_protected,
                       updated_by="local",
                       updated_at=now)


def answer(snapshot: SystemSnapshot, policy: OptimizationPolicy, actions: list[dict],
           sessions: list[dict] | None = None, apps: list[dict] | None = None) -> str:
    load = {"cpu": pct(snapshot.cpu_percent), "ram": pct(snapshot.ram_percent)}
    lines = [tx("Right now: CPU {cpu}, RAM {ram}, power plan '{plan}'.", plan=snapshot.power_plan, **load)
             if snapshot.power_plan else tx("Right now: CPU {cpu}, RAM {ram}.", **load)]
    if apps:
        lines.append(tx("Holding the most memory of their own: {apps}.", apps=i18n.joined(
            [tx("{app} ({mb} MB)", app=a["title"], mb=i18n.num(a["mb"])) for a in apps], ", ")))
    if sessions:
        last = sessions[0]
        limiter = _LIMITED_BY.get(last.get("limiter"))
        game = tx("{game} ({m} min)", game=last["game"], m=i18n.num(last["minutes"], ".0f"))
        if last.get("fps"):
            fps = {"fps": i18n.num(last["fps"], ".0f"), "low": i18n.num(last["low_1_fps"], ".0f")}
            line = (tx("Last game: {game}: {fps} fps on average, 1% low {low} fps, limited by {limiter}.", game=game,
                       limiter=limiter, **fps) if limiter
                    else tx("Last game: {game}: {fps} fps on average, 1% low {low} fps.", game=game, **fps))
        else:
            line = tx("Last game: {game}.", game=game)
        advice = tx("Advice: {advice}.", advice=i18n.joined(last["advice"], "; ")) if last.get("advice") else None
        lines.append(i18n.joined([line, advice], " "))

    if not actions:
        lines.append(tx("No actions have been recorded yet, so there is nothing to report."))
    else:
        applied = sum(1 for a in actions if a["applied"])
        lines.append(tx("In the last {n} recorded actions: {applied} applied, {skipped} not applied (blocked or refused).",
                        n=len(actions), applied=applied, skipped=len(actions) - applied))
        common = Counter(a["action"] for a in actions).most_common(3)
        lines.append(tx("Most common: {actions}.", actions=i18n.joined(
            [tx("{action} ({n}x)", action=_ACTION_LABELS.get(action, action), n=n) for action, n in common], ", ")))
        last = actions[-1]
        lines.append(tx("Latest: {action} on '{target}' — {reason} (result: {result}).",
                        action=_ACTION_LABELS.get(last["action"], last["action"]),
                        target=i18n.recalled(last["target"], last.get("target_i18n")),
                        reason=i18n.recalled(last["reason"], last.get("reason_i18n")),
                        result=i18n.recalled(last["result"], last.get("result_i18n"))))

    lines.append(tx("Current policy (set by {who}): {notes}", who=_SET_BY.get(policy.updated_by, policy.updated_by),
                    notes=policy.text("notes")))
    lines.append(tx("(Built-in summary: no AI provider was available. Add a free GEMINI_API_KEY or GROQ_API_KEY to .env "
                    "to get answers to your actual question.)"))
    return i18n.joined(lines, "\n")
