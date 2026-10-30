"""
The app itself: the Settings page, OptiMind's window, start with Windows,
administrator rights, the report to send for help, the update check, and quitting.

Window, quit and restart only work in the desktop app (app.desktop.shell);
under plain uvicorn, and in the tests, they answer 409.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException

from app import __version__, i18n
from app.ai import providers
from app.api import LocalizedRoute
from app.config import BASE_DIR, DATA_DIR, FROZEN, LOG_DIR, PROGRAM, SETTINGS_FILE
from app.desktop import autostart, shell
from app.i18n import tx
from app.models import AutostartRequest, BackupImportRequest, SettingsUpdate
from app.service import preferences, updates
from app.system import admin_rights, capabilities
from app.tools import report

router = APIRouter(route_class=LocalizedRoute)

_FOLDERS = {
    "data": lambda: DATA_DIR,
    "logs": lambda: LOG_DIR,
    "settings": lambda: SETTINGS_FILE.parent,
    "program": lambda: Path(PROGRAM).parent if FROZEN else BASE_DIR,  # with THIRD-PARTY-NOTICES.txt
}


def _desktop() -> shell.Shell:
    if shell.current is None:
        raise HTTPException(status_code=409, detail=tx("Only in the OptiMind app (not under plain uvicorn)."))
    return shell.current


@router.get("/app")
def get_app() -> dict:
    """Version, administrator rights, start with Windows and where files are."""
    return {
        "version": __version__,
        "installed": FROZEN,
        "desktop": shell.current is not None,
        "elevated": admin_rights.is_elevated(),
        "can_ask_for_admin": admin_rights.can_ask_for_admin(),
        "autostart": autostart.status(),
        "folders": {name: str(path()) for name, path in _FOLDERS.items()},
    }


@router.get("/app/language")
def get_language() -> dict:
    """OptiMind's language now, the one chosen in Settings ("" = Windows'), Windows' own, the
    languages OptiMind has and the translations (the page also gets these with itself)."""
    return i18n.page_texts()


@router.get("/app/capabilities")
def get_capabilities() -> dict:
    """What works on this PC, and why not (administrator rights, frame rates, the graphics card's
    sensors, the power plan, the cores Maximum mode uses, the newest graphics driver)."""
    return {"items": capabilities.report()}


@router.get("/app/update")
def get_update() -> dict:
    """Whether a newer OptiMind is on the release page, from the last check (a check older than
    a day starts again in the background; this never waits for GitHub)."""
    updates.refresh_in_background()
    return updates.status()


@router.post("/app/update/check")
def check_update() -> dict:
    """Asks the release page now (Settings → About → Check now)."""
    return updates.check()


@router.post("/app/window")
def open_window(page: str | None = None) -> dict:
    """Opens OptiMind's window (or brings it to the front). A second start of OptiMind uses this."""
    _desktop().open_window(page)
    return {"result": "ok"}


@router.post("/app/quit")
def quit_app() -> dict:
    """Gives apps their own settings back and ends OptiMind (the installer uses this before an update)."""
    _desktop().quit_soon()
    return {"result": "quitting"}


@router.post("/app/restart-as-admin")
def restart_as_admin() -> dict:
    """Shows the Windows prompt; on Yes, OptiMind starts again with administrator rights."""
    desktop = _desktop()
    if admin_rights.is_elevated():
        raise HTTPException(status_code=409, detail=tx("OptiMind already runs as administrator."))
    if not admin_rights.can_ask_for_admin():
        raise HTTPException(status_code=403, detail=tx("This Windows account isn't an administrator."))
    if not desktop.restart_as_admin():
        raise HTTPException(status_code=400, detail=tx("Not restarted: the Windows prompt was declined."))
    return {"result": "restarting"}


@router.put("/app/autostart")
def set_autostart(req: AutostartRequest) -> dict:
    """Start with Windows on/off (a scheduled task; needs administrator rights)."""
    try:
        autostart.enable() if req.enabled else autostart.disable()
    except autostart.AutostartError as e:
        raise HTTPException(status_code=403 if "administrator" in str(e) else 400, detail=i18n.message(e))
    return {"autostart": autostart.status()}


@router.post("/app/open-folder/{name}")
def open_folder(name: Literal["data", "logs", "settings", "program"]) -> dict:
    """Opens one of OptiMind's folders in File Explorer."""
    folder = _FOLDERS[name]()
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer.exe", str(folder)])
    return {"result": str(folder)}


@router.post("/app/report")
def create_report() -> dict:
    """Saves a report to send when something doesn't work: a zip on the Desktop with OptiMind's
    logs and the PC's details, without the user name, the PC's name, API keys or game names."""
    try:
        return report.create()
    except OSError as e:
        raise HTTPException(status_code=500, detail=tx("The report couldn't be saved ({error}).", error=e.strerror or str(e)))


@router.post("/app/report/show")
def show_report() -> dict:
    """Shows the report made last in File Explorer, selected (only that file: no path is taken)."""
    path = report.last()
    if path is None:
        raise HTTPException(status_code=404, detail=tx("No report was made since OptiMind started."))
    subprocess.Popen(f'explorer.exe /select,"{path}"')  # a Windows path can't hold a quote
    return {"result": str(path)}


@router.get("/settings")
def get_settings() -> dict:
    return preferences.current()


@router.put("/settings")
def save_settings(update: SettingsUpdate) -> dict:
    """Saves to the settings file and applies right away (API keys are stored encrypted)."""
    try:
        return preferences.save(update)
    except preferences.SettingsFileError as e:
        raise HTTPException(status_code=500, detail=i18n.message(e))


@router.post("/settings/test-ai/{provider}")
def test_ai_provider(provider: Literal["gemini", "groq", "ollama", "custom"]) -> dict:
    """One tiny request with the saved key and model, to check they work."""
    try:
        return {"ok": True, "result": providers.check(provider)}
    except providers.ProviderError as e:
        return {"ok": False, "result": i18n.message(e)}


@router.post("/app/backup/export")
def backup_export() -> dict:
    """Exports all current settings as a backup dictionary."""
    return preferences.backup_export()


@router.post("/app/backup/import")
def backup_import(update: BackupImportRequest) -> dict:
    """Imports settings from a backup dictionary."""
    return preferences.backup_import(update.data)
