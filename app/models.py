"""
Shared (pydantic) schemas used by the monitor, the optimizer, the AI
and the API. Keeping everything typed here makes it easy to validate
what the AI returns before applying anything to the operating system.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app import i18n


def utc_now() -> datetime:
    """The time in UTC without a time zone, the way OptiMind's files and API have always
    written it (what the deprecated datetime.utcnow() returned)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _WithTexts(BaseModel):
    """A model with texts OptiMind shows (app/i18n): a field such as "reason" given as a Text also
    keeps, in "reason_i18n", how to show it in another language, while the field itself holds its
    English (what files and the AI read). The API translates the two together."""

    @model_validator(mode="before")
    @classmethod
    def _keep_recipes(cls, data):
        if isinstance(data, dict):
            recipes = {f"{name}_i18n": value.to_json() for name, value in data.items()
                       if isinstance(value, i18n.Text) and f"{name}_i18n" in cls.model_fields}
            if recipes:
                return {**data, **recipes}
        return data

    def text(self, name: str) -> str:
        """A field as a text that can be shown in any language (its English if it has no recipe)."""
        return i18n.recalled(getattr(self, name), getattr(self, f"{name}_i18n", None))


class ProcessSample(BaseModel):
    pid: int
    name: str
    cpu_percent: float  # psutil scale: 100 = one full core
    memory_mb: float
    io_bps: float = 0.0  # bytes/s read + written (disk, network and device I/O)
    gpu_percent: float = 0.0  # its busiest GPU engine, % (only measured while a game is in focus)
    is_foreground: bool = False
    priority: str | None = None
    blocked_reason: str | None = None  # why the agent leaves it alone (None = it may manage it)


class SystemSnapshot(BaseModel):
    """A point-in-time reading of the machine's state."""

    timestamp: datetime = Field(default_factory=utc_now)
    cpu_percent: float
    ram_percent: float
    ram_available_mb: float
    top_processes: list[ProcessSample] = Field(default_factory=list)
    power_plan: str | None = None
    power_plans: list[str] | None = None  # the built-in plans this PC has (None = unknown)
    user_idle_seconds: float | None = None  # time since the last keyboard/mouse input
    on_battery: bool | None = None  # None = no battery (desktop) or unknown
    battery_percent: float | None = None
    agent_is_admin: bool | None = None  # is OptiMind running as administrator? (None = not Windows)
    free_mb: float | None = None  # free memory, not counting the cache (None = unknown)
    standby_mb: float | None = None  # cached memory ("standby") Windows can hand out instantly
    compressed_mb: float | None = None  # memory Windows keeps compressed (the Memory Compression process)
    compressed_data_mb: float | None = None  # the app data packed into it, before compression (administrator only)
    compression_ratio: float | None = None  # compressed_data_mb / the compressed size, as Windows reports it
    commit_used_mb: float | None = None  # memory promised to apps (RAM + page file): Task Manager's "Committed"
    commit_limit_mb: float | None = None  # ...and how much Windows can promise before it grows the page file
    hard_faults_mb_s: float | None = None  # memory Windows read back from disk (the page file or a program's files)
    # Focus mode (app/agent/focus.py)
    foreground_game: bool = False  # the app in focus is a game or runs full screen
    game_running: bool = False  # a game session is open: the game in front or behind another window (agent/games.py)
    recent_apps: list[str] = Field(default_factory=list)  # apps used just before the one in focus (Alt+Tab targets)
    audible_apps: list[str] = Field(default_factory=list)  # apps playing sound right now


class ActionType(str, Enum):
    LOWER_PRIORITY = "lower_priority"
    RAISE_PRIORITY = "raise_priority"
    EFFICIENCY_MODE_ON = "efficiency_mode_on"
    EFFICIENCY_MODE_OFF = "efficiency_mode_off"
    LOWER_IO_PRIORITY = "lower_io_priority"
    RESTORE_IO_PRIORITY = "restore_io_priority"
    SET_AFFINITY = "set_affinity"
    TRIM_WORKING_SET = "trim_working_set"
    PURGE_STANDBY_LIST = "purge_standby_list"
    DISABLE_STARTUP_ITEM = "disable_startup_item"
    ENABLE_STARTUP_ITEM = "enable_startup_item"
    SWITCH_POWER_PLAN = "switch_power_plan"
    RESTORE_APP = "restore_app"  # everything the agent changed on an app, put back at once
    CONFINE_CORES = "confine_cores"  # Maximum focus: a background app runs on one core while a game is in focus
    RESTORE_CORES = "restore_cores"
    COMBINE_MEMORY = "combine_memory"  # Windows merges identical memory pages into one shared copy
    LOWER_GPU_PRIORITY = "lower_gpu_priority"  # a background app using the GPU while a game is in focus
    RESTORE_GPU_PRIORITY = "restore_gpu_priority"
    FOCUS_BOOST = "focus_boost"  # Focus mode noted a game in front (logged by app/agent/focus.py)
    GAME_OPTIMIZE = "game_optimize"  # extra optimizations for a game when the GPU is heavily used
    GAME_OPTIMIZE_RESTORE = "game_optimize_restore"  # put game optimizations back as they were
    # Things you do yourself in the Optimize tab (never decided by the agent)
    APPLY_TWEAK = "apply_tweak"
    UNDO_TWEAK = "undo_tweak"
    CLEAN_UP = "clean_up"
    RESTORE_POINT = "restore_point"
    CLOSE_APP = "close_app"  # ...and on the Games page (Before you play), as the app's own X does
    # ... and on the Drivers page
    INSTALL_DRIVER = "install_driver"
    REMOVE_DRIVER_PACKAGE = "remove_driver_package"
    BACKUP_DRIVERS = "backup_drivers"
    RESTORE_DRIVERS = "restore_drivers"
    SCAN_DEVICES = "scan_devices"
    RESTART_DEVICE = "restart_device"
    ENABLE_DEVICE = "enable_device"


class OptimizationAction(_WithTexts):
    """A concrete action the local engine can execute."""

    action: ActionType
    target: str  # process name, startup key, or power plan name
    target_i18n: dict | None = None  # a target that is a text ("memory pages"), in another language
    reason: str
    reason_i18n: dict | None = None
    dry_run: bool = True  # set by the fast loop before it runs; True only in test mode (just logged)
    pids: list[int] = Field(default_factory=list)  # the instances to change (empty = every instance of the app)


class ActionLogEntry(_WithTexts):
    timestamp: datetime = Field(default_factory=utc_now)
    action: ActionType
    target: str  # in English: it's also how an entry is found (ActivityEntryKey)
    target_i18n: dict | None = None  # how to show the target, the reason and the result in another language
    reason: str
    reason_i18n: dict | None = None
    applied: bool  # False when it was blocked, failed, or only logged (test mode)
    result: str = "ok"
    result_i18n: dict | None = None


class ActivityEntryKey(BaseModel):
    """One activity entry, as GET /api/logs lists it."""

    timestamp: datetime
    action: str
    target: str


class ActivityRemoval(BaseModel):
    """Which activity entries to remove (POST /api/logs/remove): those matching every
    criterion given. `entries` picks single entries; since/until (UTC, until excluded) a
    time range such as a day; `status` one kind of result, as the dashboard labels it.
    Removing the whole log takes `everything`, so an empty request removes nothing."""

    model_config = {"extra": "forbid"}

    entries: list[ActivityEntryKey] = Field(default_factory=list, max_length=500)
    since: datetime | None = None
    until: datetime | None = None
    status: Literal["Applied", "Blocked", "Error"] | None = None
    everything: bool = False

    @model_validator(mode="after")
    def _says_which(self) -> ActivityRemoval:
        if not (self.entries or self.since or self.until or self.status or self.everything):
            raise ValueError("say which entries to remove: entries, since/until, status or everything")
        return self


class OptimizationPolicy(_WithTexts):
    """
    The set of thresholds and rules the local engine (fast loop)
    uses to decide what to do on each cycle. The AI (slow loop) is
    what adjusts these values based on the observed history.
    """

    cpu_high_threshold: float = 85.0
    ram_high_threshold: float = 85.0
    background_grace_period_seconds: float = 30.0
    enable_priority_management: bool = True
    enable_efficiency_mode: bool = True  # Windows 11 EcoQoS for deprioritized background apps
    enable_io_priority: bool = True  # low I/O priority for background apps doing heavy I/O
    enable_ram_reclaim: bool = True
    # Background apps you haven't used for this long get their idle memory compressed
    # (Windows packs it into its compressed memory store; it comes back when you return).
    enable_idle_compression: bool = True
    idle_compress_minutes: float = 10.0
    enable_page_combining: bool = True  # Windows merges identical memory pages (needs administrator rights)
    # Focus mode (app/agent/focus.py): the app you're using gets the PC first and switching
    # apps stays instant. "maximum" also keeps background CPU work on one core while a
    # game is in focus. Your choice (dashboard or tray): the AI never changes these two.
    enable_focus_mode: bool = True
    focus_mode: Literal["balanced", "maximum"] = "balanced"
    enable_startup_management: bool = False  # off by default: requires confirmation
    enable_power_plan_switch: bool = True
    high_load_power_plan: str = "High performance"
    idle_power_plan: str = "Balanced"
    user_idle_minutes: float = 5.0  # no input for this long = user away (no high-performance plan)
    # The AI's protected apps: their CPU, disk and GPU priority stay as they are (their idle memory can still be
    # compressed). OptiMind notes when each was added and drops it 14 days later (agent/policy_store.py); the AI
    # can't change the two fields below.
    process_denylist_extra: list[str] = Field(default_factory=list)
    protected_since: dict[str, str] = Field(default_factory=dict)  # app (lower case) -> when it was added (UTC)
    unprotected_by_you: list[str] = Field(default_factory=list)  # apps you took off the list: never put back
    notes: str = "Initial default policy."
    # The built-in rules' notes in another language (the AI writes its own: None)
    notes_i18n: dict | None = Field(default_factory=lambda: i18n.tx("Initial default policy.").to_json())
    updated_by: Literal["default", "ai", "local"] = "default"  # "local" = built-in rule-based brain
    updated_at: datetime = Field(default_factory=utc_now)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    provider: str  # which AI provider answered ("gemini", "groq", "ollama" or "local")


class CleanupRequest(BaseModel):
    ids: list[str]  # which clean-up locations to empty (see GET /api/cleanup)


class FocusRequest(BaseModel):
    enabled: bool
    mode: Literal["balanced", "maximum"] = "balanced"


class UnprotectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=260)  # an app on the AI's protected list (POST /api/policy/unprotect)


class CloseAppRequest(BaseModel):
    name: str = Field(min_length=1, max_length=260)  # a program's file name, e.g. "Discord.exe" (POST /api/apps/close)


class GameNoteRequest(BaseModel):
    """Your note on a game session: what you changed for it (POST /api/games/note). "" removes it."""

    model_config = {"extra": "forbid"}

    started: str = Field(min_length=1, max_length=40)  # the session, by when it started and its program
    exe: str = Field(min_length=1, max_length=260)
    note: str = Field(max_length=120)


# Text the settings file can hold without quoting: model names, URLs, API keys.
_PLAIN_VALUE = r"^[A-Za-z0-9._:/@+=-]*$"


class SettingsUpdate(BaseModel):
    """What the Settings page may change (app/service/preferences.py). Fields left out stay
    as they are; an API key set to "" is removed."""

    model_config = {"extra": "forbid"}

    ai_providers: list[Literal["gemini", "groq", "ollama", "custom"]] | None = None  # in the order to try them
    gemini_api_key: str | None = Field(None, max_length=300, pattern=_PLAIN_VALUE)
    groq_api_key: str | None = Field(None, max_length=300, pattern=_PLAIN_VALUE)
    gemini_model: str | None = Field(None, min_length=1, max_length=100, pattern=_PLAIN_VALUE)
    groq_model: str | None = Field(None, min_length=1, max_length=100, pattern=_PLAIN_VALUE)
    ollama_url: str | None = Field(None, max_length=200, pattern=r"^https?://[A-Za-z0-9._:/@+=-]+$")
    ollama_model: str | None = Field(None, max_length=100, pattern=_PLAIN_VALUE)
    custom_ai_api_key: str | None = Field(None, max_length=300, pattern=_PLAIN_VALUE)
    custom_ai_base_url: str | None = Field(None, max_length=500, pattern=r"^https?://[A-Za-z0-9._:/@+=-]+$")
    custom_ai_model: str | None = Field(None, min_length=1, max_length=100, pattern=_PLAIN_VALUE)
    ai_review_minutes: int | None = Field(None, ge=5, le=1440)
    run_as_admin: bool | None = None
    measure_games: bool | None = None  # frame-rate captures (PresentMon) while you play
    language: str | None = Field(None, max_length=20)  # a language OptiMind has (app/i18n), or "" for Windows' own

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str | None) -> str | None:
        if value is not None and value.strip() and value.strip() not in i18n.LANGUAGES:
            raise ValueError(f"OptiMind isn't translated into {value!r}")
        return value


class BackupImportRequest(BaseModel):
    """Backup data to import (POST /api/app/backup/import)."""

    model_config = {"extra": "forbid"}

    data: dict


class AutostartRequest(BaseModel):
    enabled: bool


# ---- 24-hour monitoring models ----


class PerformanceBaselineEntry(BaseModel):
    """Per-hour normal ranges built from the last 7 days of metrics."""
    hour: int  # 0-23
    cpu_p25: float
    cpu_p50: float
    cpu_p75: float
    ram_p25: float
    ram_p50: float
    ram_p75: float
    samples: int


class AnomalyReport(BaseModel):
    """A metric reading that deviates from the 24-hour baseline."""
    when: str
    metric: str  # "cpu" or "ram"
    value: float
    baseline_p75: float
    severity: str  # "low", "medium", "high"
    context: str


class TrendReport(BaseModel):
    """Comparison of the last 24 hours against the previous day."""
    trend: str  # "improving", "degrading", "stable", "unknown"
    note: str
    recent_cpu_avg: float | None = None
    previous_cpu_avg: float | None = None
    recent_ram_avg: float | None = None
    previous_ram_avg: float | None = None


class Monitoring24h(BaseModel):
    """Complete 24-hour monitoring snapshot for the dashboard."""
    baseline: list[PerformanceBaselineEntry]
    anomalies: list[AnomalyReport]
    trend: TrendReport
    samples: int
    days_used: int


# ---- Drivers page ----
_UPDATE_ID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class DriverUpdateRequest(BaseModel):
    """Windows Update driver updates to install, by their update ID (GET /api/drivers lists them)."""

    model_config = {"extra": "forbid"}

    ids: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _update_ids(self) -> DriverUpdateRequest:
        import re

        if not all(re.match(_UPDATE_ID, i) for i in self.ids):
            raise ValueError("ids must be Windows Update update IDs")
        return self


class DeviceRequest(BaseModel):
    """A device, by its instance ID (as GET /api/drivers lists it)."""

    model_config = {"extra": "forbid"}

    id: str = Field(min_length=3, max_length=400)


class DriverPackagesRequest(BaseModel):
    """Old driver packages to remove from the driver store, by their published name (oemNN.inf)."""

    model_config = {"extra": "forbid"}

    names: list[str] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _published_names(self) -> DriverPackagesRequest:
        import re

        if not all(re.match(r"^oem\d{1,5}\.inf$", n, re.IGNORECASE) for n in self.names):
            raise ValueError("names must be published driver package names (oemNN.inf)")
        return self


class DriverBackupRequest(BaseModel):
    model_config = {"extra": "forbid"}

    drive: str = Field(pattern=r"^[A-Za-z]:$")  # where to put the backup, e.g. "D:"


class DriverBackupRef(BaseModel):
    """One of OptiMind's driver backups, by its folder (GET /api/drivers/backups lists them)."""

    model_config = {"extra": "forbid"}

    path: str = Field(min_length=4, max_length=400)
