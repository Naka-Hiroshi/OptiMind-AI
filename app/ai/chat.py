"""Simple chat: the user asks "what have you been doing on my PC?" and
the AI answers based on the current snapshot + the log of real actions
(it never makes up numbers). Without an AI provider, the built-in
rule-based brain replies with a summary instead."""
from __future__ import annotations

import psutil

from app.agent import focus, games, policy_store
from app.agent.logger import read_recent_actions
from app.ai import local_brain
from app.ai.prompts import CHAT_SYSTEM_PROMPT
from app.ai.providers import LOCAL, chat_completion, run_with_fallback
from app.models import OptimizationPolicy, SystemSnapshot
from app.system import drivers, hardware, processes, sysinfo
from app.system.monitor import take_snapshot


def _focus_text(snapshot: SystemSnapshot) -> str:
    state = focus.status()
    if not state["enabled"]:
        return "Focus mode: off."
    app = state["app"] or "nothing yet"
    return (
        f"Focus mode: {state['mode']}. In focus: {app}"
        + (" (a game)" if snapshot.foreground_game else "") + f", priority boost: {state['boost']}. "
        f"Kept ready for Alt+Tab (never compressed): {', '.join(snapshot.recent_apps) or 'none'}. "
        f"Playing sound (never slowed): {', '.join(snapshot.audible_apps) or 'none'}."
    )


def _pc_text() -> str:
    """The parts, so the AI never guesses them (it once called this 16 GB PC "a 12-GB system")."""
    info = sysinfo.static_info()  # (never the PC's name: that stays on the PC)
    cores = f"{psutil.cpu_count(logical=False) or '?'} cores / {psutil.cpu_count() or '?'} threads"
    gpus = ", ".join(info.get("gpu_names") or []) or "unknown"
    return (f"PC: processor {info.get('cpu_name') or 'unknown'} ({cores}); graphics {gpus}; "
            f"RAM {psutil.virtual_memory().total / 2 ** 30:.1f} GB usable.")


def _memory_text(snapshot: SystemSnapshot) -> str:
    parts = [f"Memory: {snapshot.ram_available_mb / 1024:.1f} GB available."]
    if snapshot.commit_used_mb and snapshot.commit_limit_mb:
        parts.append(f"Committed (promised to apps) {snapshot.commit_used_mb / 1024:.1f} of "
                     f"{snapshot.commit_limit_mb / 1024:.1f} GB, Windows' limit (the RAM plus the page file).")
    paging = hardware.page_file()
    if paging["files"]:
        f = paging["files"][0]
        managed = {True: "its size managed by Windows", False: "a size set by hand"}.get(paging["system_managed"], "")
        parts.append(f"Page file: {f['path']}, {f['size_mb'] / 1024:.1f} GB" + (f", {managed}" if managed else "")
                     + f"; {f['used_mb']} MB in use, at most {f['peak_mb']} MB since the PC started.")
    if snapshot.hard_faults_mb_s is not None:
        parts.append(f"Read back from disk for memory right now: {snapshot.hard_faults_mb_s:.1f} MB/s.")
    if snapshot.compression_ratio and snapshot.compressed_data_mb and snapshot.compressed_mb:
        parts.append(f"Windows memory compression: {snapshot.compressed_data_mb:.0f} MB of app data kept in "
                     f"{snapshot.compressed_mb:.0f} MB ({snapshot.compression_ratio:.1f}:1).")
    elif snapshot.compressed_mb:
        parts.append(f"Windows memory compression holds {snapshot.compressed_mb:.0f} MB (ratio not reported).")
    return " ".join(parts)


def _games_text(sessions: list[dict]) -> str:
    if not sessions:
        return "No game sessions measured yet."
    lines = []
    for s in sessions:
        fps = (f"{s['fps']:.0f} fps average, 1% low {s['low_1_fps']:.0f} fps, limited by {s['limiter']}"
               if s.get("fps") else "frame rate not measured")
        memory = (f"; RAM up to {s['ram_max_percent']}%" if s.get("ram_max_percent") is not None else "") + (
            f", committed up to {s['commit_max_percent']}% of Windows' limit" if s.get("commit_max_percent") else "")
        lines.append(f"- {s['game']} ({s['minutes']:.0f} min, ended {s['ended']}): {fps}; installed on: "
                     f"{s.get('drive') or 'unknown'}; GPU max {s.get('gpu_temp_max') or '?'} °C{memory}; "
                     f"advice given: {', '.join(s['advice']) or 'none'}"
                     + (f"; the user's note on what they changed: {s['note']}" if s.get("note") else ""))
    return "\n".join(lines)


def _apps_text() -> str:
    """The apps holding the most memory of their own: what closing would give back."""
    apps = processes.biggest_apps(5)
    return ("Apps holding the most memory of their own (closing one gives it back): "
            + ", ".join(f"{a['title']} {a['mb']} MB" for a in apps) + ".") if apps else ""


def _hardware_text(checks: list[dict]) -> str:
    open_items = [c for c in checks if c["status"] in ("warn", "tip")]
    return "\n".join(f"- [{c['status']}] {c['title']}: {c['summary']}" for c in open_items) or "Nothing to fix."


def _drivers_text(findings: list[dict] | None) -> str:
    if findings is None:
        return "Not checked yet (the Drivers page checks them)."
    return "\n".join(f"- [{f['status']}] {f['title']}: {f['summary']}" + (f" ({'; '.join(f['items'][:4])})"
                                                                          if f.get("items") else "")
                     for f in findings) or "Nothing to fix."


def _build_grounding_context(snapshot: SystemSnapshot, policy: OptimizationPolicy, actions: list[dict],
                             sessions: list[dict] | None = None, checks: list[dict] | None = None,
                             driver_findings: list[dict] | None = None) -> str:
    actions_text = "\n".join(
        f"- [{a['timestamp']}] {a['action']} on '{a['target']}' -> {a['result']} "
        f"({'applied' if a['applied'] else 'not applied'}): {a['reason']}"
        for a in actions
    ) or "No actions recorded yet."

    return (
        f"{_pc_text()}\n"
        f"Current snapshot: CPU {snapshot.cpu_percent:.0f}%, RAM {snapshot.ram_percent:.0f}%, "
        f"power plan: {snapshot.power_plan or 'unknown'}. {_memory_text(snapshot)} {_apps_text()}\n"
        f"{_focus_text(snapshot)}\n\n"
        f"Policy in effect: {policy.model_dump_json(exclude={'notes_i18n'})}\n\n"
        f"Latest actions:\n{actions_text}\n\n"
        f"Last game sessions (measured by OptiMind):\n{_games_text(sessions or [])}\n\n"
        f"Hardware check, open recommendations:\n{_hardware_text(checks or [])}\n\n"
        f"Drivers check, open findings:\n{_drivers_text(driver_findings)}"
    )


def ask(user_message: str, snapshot: SystemSnapshot | None = None) -> tuple[str, str]:
    """Returns (reply, provider that answered). `snapshot` is the agent's latest one (at most a
    cycle old); measuring again here would also reset the rates the agent's next cycle reads."""
    snapshot = snapshot or take_snapshot()
    policy = policy_store.load_policy()
    actions = read_recent_actions(20)
    sessions = games.recent_summary(3)
    checks = (hardware.cached_report() or {}).get("checks", [])  # only if fresh: a new check takes seconds
    driver_findings = drivers.cached_findings()  # the last scan's, never a new one

    def task(provider: str) -> str:
        if provider == LOCAL:
            return local_brain.answer(snapshot, policy, actions, sessions, processes.biggest_apps(5))
        context = _build_grounding_context(snapshot, policy, actions, sessions, checks, driver_findings)
        return chat_completion(
            provider,
            [
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": f"[System context]\n{context}"},
                {"role": "assistant", "content": "Understood, I will use only this data to answer."},
                {"role": "user", "content": user_message},
            ],
            max_tokens=1024,
        )

    reply, provider, _ = run_with_fallback(task)
    return reply, provider
