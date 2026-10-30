"""
The "slow loop": runs every OPTIMIND_AI_LOOP_SECONDS (default 15 min).
Sends a compact summary of the machine's recent behavior — including a
24-hour view with baselines, anomalies and trends — to a free AI
provider (see providers.py) and gets back an updated policy as JSON
that must match a schema — never free text that we would have to try
to interpret. If no provider is available, the built-in rule-based
brain (local_brain.py) tunes the policy instead.

The answer goes through a field allowlist, a sanity check and pydantic
validation (OptimizationPolicy) before being saved — the AI never
writes directly to the policy file or executes anything on the system.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from app.agent import games
from app.agent.logger import read_recent_actions
from app.ai import local_brain, monitor_brain
from app.ai.prompts import POLICY_BRAIN_SYSTEM_PROMPT, POLICY_JSON_SCHEMA
from app.ai.providers import LOCAL, ProviderError, chat_completion, run_with_fallback
from app.i18n import tx
from app.models import OptimizationPolicy, utc_now
from app.system.monitor import read_daily_history, read_recent_history

_HISTORY_SAMPLES = 450  # ~15 min of fast-loop samples at 2s each
_ACTION_SAMPLES = 40


def _summarize_history(history: list[dict]) -> str:
    if not history:
        return "Not enough history yet."
    cpu_values = [h["cpu"] for h in history]
    ram_values = [h["ram"] for h in history]
    top_names: dict[str, int] = {}
    for h in history:
        for entry in h.get("top", []):
            name = entry.split(":")[0]
            top_names[name] = top_names.get(name, 0) + 1
    frequent = sorted(top_names.items(), key=lambda kv: kv[1], reverse=True)[:8]

    return (
        f"Samples: {len(history)}\n"
        f"CPU% -> avg {sum(cpu_values)/len(cpu_values):.1f}, max {max(cpu_values):.1f}\n"
        f"RAM% -> avg {sum(ram_values)/len(ram_values):.1f}, max {max(ram_values):.1f}\n"
        f"Processes most often at the top of resource usage: "
        f"{', '.join(f'{n}({c}x)' for n, c in frequent)}"
    )


def _summarize_actions(actions: list[dict]) -> str:
    if not actions:
        return "No actions recorded yet."
    applied = [a for a in actions if a.get("applied")]
    blocked = [a for a in actions if "blocked" in str(a.get("result", ""))]
    lines = [f"Total in log: {len(actions)} | applied: {len(applied)} | blocked by safety: {len(blocked)}"]
    for a in actions[-10:]:
        lines.append(f"- {a['action']} on '{a['target']}': {a['result']}")
    return "\n".join(lines)


def _summarize_games(sessions: list[dict]) -> str:
    if not sessions:
        return "No recent game sessions."
    lines = []
    for s in sessions[-5:]:
        fps = f"{s.get('fps', 0):.0f} fps avg, 1% low {s.get('low_1_fps', 0):.0f} fps" if s.get("fps") else "FPS not measured"
        limiter = s.get("limiter") or "unknown"
        lines.append(f"- {s.get('game', '?')}: {fps}, limited by {limiter}, {s.get('minutes', 0):.0f} min")
    return "\n".join(lines)


def build_context(current_policy: OptimizationPolicy, history: list[dict], actions: list[dict], sessions: list[dict] | None = None) -> str:
    # 24-hour monitoring context: baselines, anomalies, trends
    baseline = monitor_brain.build_baseline(history)
    anomalies = monitor_brain.detect_anomalies(history, baseline)
    trends = monitor_brain.analyze_trends(history)
    monitoring = monitor_brain.build_24h_context(
        history, baseline, anomalies, trends, actions, sessions
    )

    return (
        "## Recent metrics history (last ~15 min)\n"
        f"{_summarize_history(history)}\n\n"
        "## 24-hour monitoring\n"
        f"{monitoring}\n\n"
        "## Latest agent actions\n"
        f"{_summarize_actions(actions)}\n\n"
        "## Recent game sessions\n"
        f"{_summarize_games(sessions or [])}\n\n"
        "## Current policy\n"
        f"{current_policy.model_dump_json(indent=2, exclude={'notes_i18n'})}\n\n"
        "## Response format\n"
        "Reply with only a JSON object that matches this JSON Schema:\n"
        f"{json.dumps(POLICY_JSON_SCHEMA)}\n"
    )


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):  # some models wrap JSON in a ```json block
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _sanity_check(policy: OptimizationPolicy) -> None:
    if not 30 <= policy.cpu_high_threshold <= 99:
        raise ValueError(f"cpu_high_threshold out of range: {policy.cpu_high_threshold}")
    if not 30 <= policy.ram_high_threshold <= 99:
        raise ValueError(f"ram_high_threshold out of range: {policy.ram_high_threshold}")
    if not 0 <= policy.background_grace_period_seconds <= 3600:
        raise ValueError(f"background_grace_period_seconds out of range: {policy.background_grace_period_seconds}")
    if not 1 <= policy.user_idle_minutes <= 120:
        raise ValueError(f"user_idle_minutes out of range: {policy.user_idle_minutes}")
    if not 2 <= policy.idle_compress_minutes <= 240:
        raise ValueError(f"idle_compress_minutes out of range: {policy.idle_compress_minutes}")


def _ask_ai(provider: str, current_policy: OptimizationPolicy, context: str) -> OptimizationPolicy:
    raw = chat_completion(
        provider,
        [
            {"role": "system", "content": POLICY_BRAIN_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        max_tokens=2048,
        json_schema=POLICY_JSON_SCHEMA,
    )
    try:
        proposed = _parse_json(raw)
        # Merge with the current policy: only the fields the AI is allowed to
        # adjust are overwritten; the rest (e.g. enable_startup_management,
        # which requires manual user confirmation) stays untouched.
        allowed = {k: v for k, v in proposed.items() if k in POLICY_JSON_SCHEMA["properties"]}
        merged = current_policy.model_copy(update={
            **allowed,
            **({"notes_i18n": None} if "notes" in allowed else {}),  # the AI's own words: no built-in translation
            "updated_by": "ai",
            "updated_at": utc_now(),
        })
        policy = OptimizationPolicy.model_validate(merged.model_dump())
        _sanity_check(policy)
        return policy
    except (ValueError, ValidationError) as e:
        raise ProviderError(tx("{provider}: invalid policy answer ({error})", provider=provider, error=str(e))) from e


def review_policy(current_policy: OptimizationPolicy) -> tuple[OptimizationPolicy, str, list[str]]:
    """Reviews the policy with the first provider that answers.
    Returns (new policy, provider used, reasons other providers failed)."""
    history = read_recent_history(_HISTORY_SAMPLES)
    actions = read_recent_actions(_ACTION_SAMPLES)
    sessions = games.recent_summary(10)
    context = build_context(current_policy, history, actions, sessions)

    def task(provider: str) -> OptimizationPolicy:
        if provider == LOCAL:
            return local_brain.propose_policy(current_policy, history, actions)
        return _ask_ai(provider, current_policy, context)

    return run_with_fallback(task)


if __name__ == "__main__":
    # Demo: prints the context that would be sent, without calling any provider.
    print(build_context(OptimizationPolicy(), read_recent_history(_HISTORY_SAMPLES), read_recent_actions(_ACTION_SAMPLES)))
