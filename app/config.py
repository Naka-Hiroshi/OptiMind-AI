"""
OptiMind AI central settings.

Everything adjustable comes from environment variables or the settings file,
so the agent's behavior can be changed without touching the code:
  - running from source: `.env` in the project folder;
  - the installed app: `settings.env` in %LOCALAPPDATA%\\OptiMind, the file
    the Settings page edits (app/service/preferences.py).
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.system import dpapi

IS_WINDOWS = sys.platform == "win32"
# The installed app: compiled with Nuitka (see packaging/), which marks each compiled module
# with __compiled__ (PyInstaller, used up to 1.3.0, set sys.frozen). Its program folder is
# read-only, so your settings and OptiMind's data live in your user folder instead.
FROZEN = "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def _program_file() -> str:
    """The installed app's OptiMind.exe by its full path, as Windows loaded it. Not sys.executable:
    in a Nuitka build that names a python.exe that doesn't exist until anyio or multiprocessing
    loads, then this program in short form (C:\\PROGRA~1\\OPTIMI~1\\OptiMind.exe), the name Windows
    would then give the window started from it."""
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer))
    return os.path.realpath(buffer.value[:length])


# The running program, which starts OptiMind's window, start with Windows and the prompt for
# administrator rights: the installed app's OptiMind.exe, or Python from source.
PROGRAM = _program_file() if FROZEN and IS_WINDOWS else sys.executable

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent  # the project folder (installed app: its program files)
_LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
USER_DIR = _LOCAL_APPDATA / "OptiMind" if FROZEN else BASE_DIR
SETTINGS_FILE = Path(os.environ.get("OPTIMIND_SETTINGS_FILE") or USER_DIR / ("settings.env" if FROZEN else ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=SETTINGS_FILE, env_file_encoding="utf-8-sig", env_prefix="",
                                      extra="ignore")

    # --- AI (free providers only) ---
    # Order to try them in; "local" (rule-based, offline) is always the last resort.
    optimind_ai_providers: str = "gemini,groq,local"
    gemini_api_key: str = ""
    optimind_gemini_model: str = "gemini-3.8-flash"
    groq_api_key: str = ""
    optimind_groq_model: str = "openai/gpt-oss-120b"
    optimind_ollama_url: str = "http://localhost:11434/v1"
    optimind_ollama_model: str = ""

    # --- Custom AI (any OpenAI-compatible /chat/completions endpoint) ---
    custom_ai_api_key: str = ""
    custom_ai_base_url: str = ""
    custom_ai_model: str = ""

    # --- Loops ---
    optimind_fast_loop_seconds: float = 2.0
    optimind_ai_loop_seconds: float = 900.0

    # --- Test mode (for developers, not a user setting) ---
    # true = the agent only logs what it would do and changes nothing on the PC.
    # The automated tests run OptiMind this way (see docs/development.md).
    optimind_dry_run: bool = False

    @field_validator("optimind_dry_run", mode="before")
    @classmethod
    def _dry_run_from_env(cls, value):
        env = os.environ.get("OPTIMIND_DRY_RUN")
        if env is not None:
            return env.strip().lower() in ("true", "1", "yes", "on")
        return value

    # --- Local API ---
    optimind_api_port: int = 8765

    # --- Windows ---
    # Ask for administrator rights at start (see app/system/admin_rights.py).
    optimind_run_as_admin: bool = True

    # --- Games ---
    # Measure frame rates with PresentMon while you play (app/agent/games.py).
    optimind_measure_games: bool = True

    # --- Language ---
    # OptiMind's language ("pt-BR", "ja"…, see app/i18n). Empty: Windows' display language, or English
    # when OptiMind isn't translated into it.
    optimind_language: str = ""

    # --- Updates ---
    # OptiMind's release page on GitHub ("owner/name"): the update check asks it which version is
    # newest, about once a day (app/service/updates.py). Empty: no check.
    optimind_update_repo: str = "Naka-Hiroshi/OptiMind-AI"

    # --- Files ---
    # Where the logs, policy, history and tweak backups are kept (default: the
    # data folder next to the settings file). The tests point it at a temporary folder.
    optimind_data_dir: str = ""

    @field_validator("gemini_api_key", "groq_api_key", "custom_ai_api_key")
    @classmethod
    def _readable_key(cls, value: str) -> str:
        return dpapi.unprotect(value)  # the Settings page saves keys encrypted


settings = Settings()


def is_dry_run() -> bool:
    """Effective test mode: the settings value, overridden by the OPTIMIND_DRY_RUN
    environment variable. Tests set OPTIMIND_DRY_RUN=true in tests/__init__.py, but
    app.config may already have been imported by then (import order depends on which
    test module loads first), so check the env var here too."""
    env = os.environ.get("OPTIMIND_DRY_RUN")
    if env is not None:
        return env.strip().lower() in ("true", "1", "yes", "on")
    return settings.optimind_dry_run

DATA_DIR = Path(settings.optimind_data_dir) if settings.optimind_data_dir else USER_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = USER_DIR / "logs"  # the installed app's log files (from source, the console shows them)
WEBVIEW_DIR = _LOCAL_APPDATA / "OptiMind" / "WebView2"  # the window's browser-engine cache

ACTION_LOG_PATH = DATA_DIR / "action_log.jsonl"
POLICY_STATE_PATH = DATA_DIR / "policy_state.json"
POLICY_HISTORY_PATH = DATA_DIR / "policy_history.jsonl"
METRICS_HISTORY_PATH = DATA_DIR / "metrics_history.jsonl"
