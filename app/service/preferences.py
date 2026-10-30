"""
The Settings page: reads and saves OptiMind's settings file.

Only the settings in SettingsUpdate (app/models.py) can be changed from the
dashboard. Saving rewrites the matching KEY=value lines in place (your comments
and every other line stay as they are), adds missing ones at the end, and
applies the new values to the running agent right away. API keys are
write-only: the page only learns whether one is set, and they are saved
encrypted for your Windows account (app/system/dpapi.py).
"""
from __future__ import annotations

import codecs
import os
import re
import tempfile
from pathlib import Path

from app import i18n
from app.config import SETTINGS_FILE, settings
from app.i18n import tx
from app.models import SettingsUpdate
from app.system import dpapi

LOCAL = "local"  # the built-in rule-based brain: always on, always last

# Settings page name -> (settings field, environment variable)
_FIELDS = {
    "ai_providers": ("optimind_ai_providers", "OPTIMIND_AI_PROVIDERS"),
    "gemini_api_key": ("gemini_api_key", "GEMINI_API_KEY"),
    "groq_api_key": ("groq_api_key", "GROQ_API_KEY"),
    "gemini_model": ("optimind_gemini_model", "OPTIMIND_GEMINI_MODEL"),
    "groq_model": ("optimind_groq_model", "OPTIMIND_GROQ_MODEL"),
    "ollama_url": ("optimind_ollama_url", "OPTIMIND_OLLAMA_URL"),
    "ollama_model": ("optimind_ollama_model", "OPTIMIND_OLLAMA_MODEL"),
    "custom_ai_api_key": ("custom_ai_api_key", "CUSTOM_AI_API_KEY"),
    "custom_ai_base_url": ("custom_ai_base_url", "CUSTOM_AI_BASE_URL"),
    "custom_ai_model": ("custom_ai_model", "CUSTOM_AI_MODEL"),
    "ai_review_minutes": ("optimind_ai_loop_seconds", "OPTIMIND_AI_LOOP_SECONDS"),
    "run_as_admin": ("optimind_run_as_admin", "OPTIMIND_RUN_AS_ADMIN"),
    "measure_games": ("optimind_measure_games", "OPTIMIND_MEASURE_GAMES"),
    "language": ("optimind_language", "OPTIMIND_LANGUAGE"),
    "backup": ("", "OPTIMIND_BACKUP"),  # temporary: triggers backup export
}
_SWITCHES = {"run_as_admin", "measure_games"}  # true/false settings
_BACKUP_FILE = "optimi18n_backup.json"  # exported settings backup filename
_SECRETS = {"gemini_api_key", "groq_api_key", "custom_ai_api_key"}
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class SettingsFileError(Exception):
    """The settings file couldn't be written (e.g. it's read-only)."""


def current() -> dict:
    """What the Settings page shows. API keys only say whether one is set."""
    chain = [p.strip().lower() for p in settings.optimind_ai_providers.split(",") if p.strip()]
    return {
        "file": str(SETTINGS_FILE),
        "ai_providers": [p for p in chain if p != LOCAL],
        "gemini_api_key_set": bool(settings.gemini_api_key),
        "groq_api_key_set": bool(settings.groq_api_key),
        "gemini_model": settings.optimind_gemini_model,
        "groq_model": settings.optimind_groq_model,
        "ollama_url": settings.optimind_ollama_url,
        "ollama_model": settings.optimind_ollama_model,
        "custom_ai_api_key_set": bool(settings.custom_ai_api_key),
        "custom_ai_base_url": settings.custom_ai_base_url,
        "custom_ai_model": settings.custom_ai_model,
        "ai_review_minutes": round(settings.optimind_ai_loop_seconds / 60),
        "run_as_admin": settings.optimind_run_as_admin,
        "measure_games": settings.optimind_measure_games,
        "language": settings.optimind_language,  # "" = Windows' language
        # Set by an environment variable, which wins over the file at the next start
        "fixed_by_environment": sorted(name for name, (_, env) in _FIELDS.items() if env in os.environ),
    }


def save(update: SettingsUpdate) -> dict:
    """Writes the changed settings to the file and applies them to the running agent."""
    changes = update.model_dump(exclude_none=True)
    file_values: dict[str, str] = {}
    live_values: dict[str, object] = {}
    for name, value in changes.items():
        field, env = _FIELDS[name]
        if name == "ai_providers":
            chain = list(dict.fromkeys(value)) + [LOCAL]  # no repeats; the built-in brain last
            live_values[field] = file_values[env] = ",".join(chain)
        elif name == "ai_review_minutes":
            live_values[field] = float(value * 60)
            file_values[env] = str(value * 60)
        elif name in _SWITCHES:
            live_values[field] = value
            file_values[env] = "true" if value else "false"
        else:
            value = value.strip()
            live_values[field] = value
            file_values[env] = dpapi.protect(value) if name in _SECRETS else value
    if file_values:
        write_values(SETTINGS_FILE, file_values)
    for field, value in live_values.items():
        setattr(settings, field, value)
    if "language" in changes:
        i18n.changed()
    return current()


def write_values(path: Path, values: dict[str, str]) -> None:
    """Sets KEY=value lines in a settings file: existing lines are changed in place (every copy
    of the key), missing keys are added at the end, and everything else is kept, as are the
    file's encoding mark (BOM) and line endings."""
    raw = path.read_bytes() if path.exists() else b""
    bom = raw.startswith(codecs.BOM_UTF8)
    text = raw[len(codecs.BOM_UTF8):].decode("utf-8") if bom else raw.decode("utf-8")
    newline = "\n" if text and "\r\n" not in text else "\r\n"
    lines = text.splitlines()
    found = set()
    for i, line in enumerate(lines):
        match = _LINE.match(line)
        key = match and match.group(1).upper()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            found.add(key)
    missing = [key for key in values if key not in found]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"{key}={values[key]}" for key in missing]
    data = (codecs.BOM_UTF8 if bom else b"") + (newline.join(lines) + newline).encode("utf-8")

    # Written to a temporary file first, so a failure never leaves half a settings file.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError as e:
        raise SettingsFileError(tx("couldn't save {file}: {error}", file=path.name, error=e.strerror or str(e))) from e


def backup_export() -> dict:
    """Export all current settings as a backup dictionary.
    
    Returns a dict with all setting key-value pairs that can be saved
    to a JSON file and later imported with backup_import().
    """
    from app.config import settings as s
    result = {}
    for name, (field, env) in _FIELDS.items():
        if field and hasattr(s, field):
            val = getattr(s, field)
            # Handle special types
            if name == "language":
                result[name] = val if val else ""
            elif name in {"ai_review_minutes",}:
                result[name] = val
            else:
                result[name] = str(val) if val is not None else ""
    return result


def backup_import(data: dict) -> dict:
    """Import settings from a backup dictionary, returning the current state.
    
    Returns the current settings state after applying the import.
    """
    from app.config import settings as s, SETTINGS_FILE
    from app.i18n import changed as i18n_changed
    
    changes = {}
    for name, value in data.items():
        field, env = _FIELDS.get(name, (None, None))
        if not field or name not in [k for k in _FIELDS]:
            continue
        if name == "language":
            value = value.strip() if value else ""
            live_values = {"optimind_language": value}
        elif name == "ai_review_minutes":
            live_values = {"optimind_ai_loop_seconds": float(value) * 60 if value else 1200.0}
        elif name in _SWITCHES:
            live_values = {field: value.lower() == "true" if isinstance(value, str) else bool(value)}
        else:
            live_values = {field: value}
        
        # Apply live values
        for field, value in live_values.items():
            setattr(settings, field, value)
        
        # Also update file values
        if env:
            file_values = {env: dpapi.protect(value) if name in _SECRETS else value}
            if file_values:
                write_values(SETTINGS_FILE, file_values)
        
        changes[name] = live_values.get(field)
    
    if "language" in changes:
        i18n_changed()
    
    return current()