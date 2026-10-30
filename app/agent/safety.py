"""
Safety layer. EVERY optimizer action goes through here before it is
(or isn't) actually applied.

Golden rule: essential Windows processes are NEVER touched, even if
the AI "suggests" it — the validation is done in code, not left up to
the model.
"""
from __future__ import annotations

from app.i18n import tx
from app.models import ActionType, OptimizationAction

# System processes that must never have their priority/affinity
# changed or their memory "cleaned". Deliberately conservative list.
PROTECTED_PROCESSES: set[str] = {
    "system",
    "system idle process",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",  # hosts critical services; touching it is too risky
    "explorer.exe",  # the user's shell — degrading it hurts the experience
    "dwm.exe",  # window compositor
    "fontdrvhost.exe",
    "audiodg.exe",
    "conhost.exe",
    "python.exe",  # never optimizes / kills itself
    "pythonw.exe",
    "secure system",
    "memcompression",  # "Memory Compression" in Task Manager
    "memory compression",
    "lsaiso.exe",  # Credential Guard
    "ngciso.exe",  # Windows Hello
    # Typing and accessibility helpers: they work for the app in front even
    # while "in the background", so any slowdown is felt right away.
    "ctfmon.exe",  # keyboard layouts and input methods (IME)
    "textinputhost.exe",  # touch keyboard, emoji panel, IME candidate window
    "osk.exe",  # On-Screen Keyboard
    "narrator.exe",
    "magnify.exe",
    # Security software. Slowing it down slows every app (real-time
    # scanning holds file access until each scan finishes), and Windows
    # runs it as a protected process that even administrators can't change.
    "msmpeng.exe",  # Microsoft Defender antivirus engine
    "mpdefendercoreservice.exe",  # Defender core service
    "nissrv.exe",  # Defender network inspection
    "mssense.exe",  # Defender for Endpoint
    "securityhealthservice.exe",
    "securityhealthsystray.exe",
    "smartscreen.exe",
    "sgrmbroker.exe",  # System Guard runtime monitor
}

# Apps that record or stream the game you're playing: they work on the game's own frames,
# so their GPU priority is never lowered (it would drop frames from the recording).
RECORDING_APPS: set[str] = {
    "obs64.exe", "obs32.exe", "obs.exe", "streamlabs obs.exe", "xsplit.core.exe", "nvidia share.exe",
    "nvidia overlay.exe", "nvcontainer.exe", "nvsphelper64.exe", "gamebar.exe", "gamebarftserver.exe",
    "gamebarpresencewriter.exe", "medal.exe", "outplayed.exe", "action.exe", "bdcam.exe", "discord.exe",
    "discord_clips.exe", "radeonsoftware.exe", "amdrsserv.exe", "amdow.exe", "steelseriescapturesvc.exe",
    "overwolf.exe",
}

# Startup items "known" to be safe to disable (common
# bloatware/updater examples). Anything outside this list requires
# explicit user confirmation before being disabled.
KNOWN_SAFE_STARTUP_PATTERNS: tuple[str, ...] = (
    "updater",
    "update helper",
    "cef helper",
    "promo",
    "tips",
)

MAX_ACTIONS_PER_CYCLE = 10  # new changes per cycle; giving something back is never held up

# Actions that only put back what the agent changed.
GIVE_BACK_ACTIONS = {
    ActionType.RESTORE_APP, ActionType.RAISE_PRIORITY, ActionType.EFFICIENCY_MODE_OFF,
    ActionType.RESTORE_IO_PRIORITY, ActionType.RESTORE_CORES, ActionType.RESTORE_GPU_PRIORITY,
    ActionType.GAME_OPTIMIZE_RESTORE,
}


def within_limit(actions: list[OptimizationAction]) -> tuple[list[OptimizationAction], list[OptimizationAction]]:
    """(what runs this cycle, what waits for the next one): every give-back runs, in its
    order; new changes run up to MAX_ACTIONS_PER_CYCLE."""
    now, later = [], []
    changes = 0
    for action in actions:
        if action.action in GIVE_BACK_ACTIONS:
            now.append(action)
        elif changes < MAX_ACTIONS_PER_CYCLE:
            now.append(action)
            changes += 1
        else:
            later.append(action)
    return now, later


class SafetyViolation(Exception):
    pass


def is_protected(process_name: str, extra_denylist: list[str] | None = None) -> bool:
    name = process_name.strip().lower()
    if name in PROTECTED_PROCESSES:
        return True
    if extra_denylist and name in {p.lower() for p in extra_denylist}:
        return True
    return False


def validate_action(action: OptimizationAction, extra_denylist: list[str] | None = None) -> None:
    """Raises SafetyViolation if the action is not safe to proceed."""

    process_targeting_actions = {
        ActionType.LOWER_PRIORITY,
        ActionType.RAISE_PRIORITY,
        ActionType.EFFICIENCY_MODE_ON,
        ActionType.EFFICIENCY_MODE_OFF,
        ActionType.LOWER_IO_PRIORITY,
        ActionType.RESTORE_IO_PRIORITY,
        ActionType.SET_AFFINITY,
        ActionType.TRIM_WORKING_SET,
        ActionType.RESTORE_APP,
        ActionType.CONFINE_CORES,
        ActionType.RESTORE_CORES,
        ActionType.LOWER_GPU_PRIORITY,
        ActionType.RESTORE_GPU_PRIORITY,
        ActionType.GAME_OPTIMIZE,
    }

    # The AI's list (extra_denylist) keeps an app's CPU, disk and GPU priority: its idle memory may still be
    # compressed, and whatever the agent changed is always given back (the app may have joined the list since).
    extra = None if action.action in GIVE_BACK_ACTIONS | {ActionType.TRIM_WORKING_SET} else extra_denylist
    if action.action in process_targeting_actions and is_protected(action.target, extra):
        raise SafetyViolation(tx("Refused: '{app}' is on the protected process list.", app=action.target))

    if action.action == ActionType.LOWER_GPU_PRIORITY and action.target.strip().lower() in RECORDING_APPS:
        raise SafetyViolation(tx("Refused: '{app}' records or streams the game; its GPU priority stays.", app=action.target))

    if action.action == ActionType.DISABLE_STARTUP_ITEM:
        target = action.target.lower()
        if not any(pat in target for pat in KNOWN_SAFE_STARTUP_PATTERNS):
            raise SafetyViolation(tx("Refused: '{app}' is not on the list of startup items known to be safe. "
                                     "Requires manual approval.", app=action.target))

    if action.action == ActionType.RAISE_PRIORITY:
        # "Raise" only ever restores normal priority —
        # "high"/"realtime" priority can freeze the whole system.
        pass  # the priority level is enforced in optimizer.py
