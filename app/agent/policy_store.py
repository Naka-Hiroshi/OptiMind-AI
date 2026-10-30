"""Persists the current OptimizationPolicy to disk, so it survives
restarts and so the AI can read/update it consistently. Every review
is also appended to a small history file, so you can see how the
policy changed over time.

The AI's protected apps are kept here too: when each was added, and
gone 14 days later. The AI only ever adds apps (every app you keep
switching back to), so without that the list would only grow, and
everything on it would be left alone for good."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.config import POLICY_HISTORY_PATH, POLICY_STATE_PATH
from app.models import OptimizationPolicy, utc_now

_HISTORY_KEEP_LINES = 500
_PROTECTED_DAYS = 14  # an app the AI protected leaves the list after this (the AI lists it again if it still has a reason)


def load_policy() -> OptimizationPolicy:
    if POLICY_STATE_PATH.exists():
        try:
            return settle_protected(OptimizationPolicy.model_validate_json(POLICY_STATE_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass  # corrupted file/old version -> fall back to the default
    return OptimizationPolicy()


def settle_protected(policy: OptimizationPolicy, now: datetime | None = None) -> OptimizationPolicy:
    """The AI's protected apps as OptiMind keeps them: never one you took off, each noted with
    the time it was added (UTC), and gone 14 days after that."""
    now = now or utc_now()
    removed = {name.lower() for name in policy.unprotected_by_you}
    kept: list[str] = []
    since: dict[str, str] = {}
    for name in policy.process_denylist_extra:
        key = name.strip().lower()
        if not key or key in removed or key in since:
            continue
        added = policy.protected_since.get(key) or now.isoformat()
        try:
            if now - datetime.fromisoformat(added) > timedelta(days=_PROTECTED_DAYS):
                continue
        except (ValueError, TypeError):
            added = now.isoformat()  # unreadable: counts from now
        kept.append(name)
        since[key] = added
    return policy.model_copy(update={"process_denylist_extra": kept, "protected_since": since})


def save_policy(policy: OptimizationPolicy) -> None:
    POLICY_STATE_PATH.write_text(policy.model_dump_json(indent=2), encoding="utf-8")


def append_history(policy: OptimizationPolicy, provider: str) -> None:
    entry = {
        "t": policy.updated_at.isoformat(),
        "provider": provider,
        "notes": policy.notes,
        **({"notes_i18n": policy.notes_i18n} if policy.notes_i18n else {}),  # the built-in rules' notes, translatable
        "cpu_high_threshold": policy.cpu_high_threshold,
        "ram_high_threshold": policy.ram_high_threshold,
        "background_grace_period_seconds": policy.background_grace_period_seconds,
        "process_denylist_extra": policy.process_denylist_extra,
    }
    with POLICY_HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if POLICY_HISTORY_PATH.stat().st_size > 512 * 1024:
        lines = POLICY_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        POLICY_HISTORY_PATH.write_text("\n".join(lines[-_HISTORY_KEEP_LINES:]) + "\n", encoding="utf-8")


def read_history(n: int = 20) -> list[dict]:
    if not POLICY_HISTORY_PATH.exists():
        return []
    lines = POLICY_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:]]
