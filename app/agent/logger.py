"""JSONL action log — it's what the AI uses to explain to the user what
was done, and what lets you audit/debug the agent afterwards. Entries you
remove (Agent → Activity) are gone from the file, so the AI no longer sees
them either; removing them doesn't undo anything."""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from app.config import ACTION_LOG_PATH
from app.models import ActionLogEntry, ActionType

_MAX_LOG_BYTES = 2 * 1024 * 1024
_KEEP_LOG_BYTES = 1536 * 1024
_lock = threading.Lock()  # the agent's loop and the API both write the file


def trim_jsonl(path: Path, max_bytes: int, keep_bytes: int) -> None:
    """Keeps a JSON-lines file from growing forever: once it's over `max_bytes`, only the
    newest whole lines that fit in `keep_bytes` stay. The gap between the two makes the
    rewrite rare whatever the length of the lines (a line count alone doesn't)."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        data = path.read_bytes()
    except OSError:
        return
    start = data.find(b"\n", len(data) - keep_bytes - 1)  # the newest lines start after this line break
    path.write_bytes(data[start + 1:] if start != -1 else b"")


def log_action(entry: ActionLogEntry) -> None:
    with _lock:
        with ACTION_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        trim_jsonl(ACTION_LOG_PATH, _MAX_LOG_BYTES, _KEEP_LOG_BYTES)


def read_recent_actions(n: int = 50) -> list[dict]:
    with _lock:
        if not ACTION_LOG_PATH.exists():
            return []
        lines = ACTION_LOG_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-n:]]


def result_status(entry: ActionLogEntry) -> str:
    """How the dashboard labels an entry: Applied, Blocked, Error, Logged only or Unchanged (it
    shows these words in its language; its filters and the API keep them as they are)."""
    result = entry.result or ""
    if result.startswith("blocked"):
        return "Blocked"
    if result.startswith(("error", "failed")) or (not entry.applied and by_you(entry)):
        return "Error"
    if entry.applied:
        return "Applied"
    return "Logged only" if result.startswith("simulated") else "Unchanged"


def by_you(entry: ActionLogEntry) -> bool:
    """A change you made yourself (a tweak, a clean-up, a startup app…), not the agent's."""
    return entry.reason.startswith("Changed by you")


_FREED = re.compile(r"(?:freed|merged) ([\d,.]+) MB")
_PURGED = re.compile(r"free memory ([\d,.]+) MB -> ([\d,.]+) MB")
_MEMORY_ACTIONS = {ActionType.TRIM_WORKING_SET, ActionType.PURGE_STANDBY_LIST, ActionType.COMBINE_MEMORY}


def freed_mb(entry: ActionLogEntry) -> float | None:
    """The memory a compress, merge or standby purge freed, as Windows reported it in its result
    (None for anything else)."""
    if not entry.applied or entry.action not in _MEMORY_ACTIONS:
        return None
    mb = lambda s: float(s.replace(",", ""))  # noqa: E731
    if match := _FREED.search(entry.result):
        return mb(match.group(1))
    if match := _PURGED.search(entry.result):
        return max(0.0, mb(match.group(2)) - mb(match.group(1)))
    return None


def shown_entry(entry: ActionLogEntry) -> dict:
    """An entry as the dashboard lists it: with its label (status), whether you made the change and
    the memory it freed, so the page never reads the English of its reason or result. Its target
    stays English (the page sends it back to remove the entry), shown as "target_shown"."""
    data = entry.model_dump(mode="json", exclude={"target_i18n"})
    return {**data, "target_shown": entry.text("target"), "status": result_status(entry), "by_you": by_you(entry),
            "freed_mb": freed_mb(entry)}


def remove_actions(picked: Callable[[ActionLogEntry], bool] | None = None) -> int:
    """Deletes the entries `picked` chooses from the log, or all of them when it's None.
    Returns how many. With a choice, lines this version can't read are kept."""
    with _lock:
        if not ACTION_LOG_PATH.exists():
            return 0
        kept, removed = [], 0
        for line in ACTION_LOG_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                drop = picked is None or picked(ActionLogEntry.model_validate_json(line))
            except ValueError:  # written by an older version
                drop = False
            if drop:
                removed += 1
            else:
                kept.append(line)
        if removed:
            ACTION_LOG_PATH.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        return removed
