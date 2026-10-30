"""The Drivers page: every device and its driver, what needs attention, updates from Windows
Update and NVIDIA, and the driver tools (scan for hardware changes, restart or turn on a
device, remove old versions, back up and restore). Everything that changes the PC is your
click, lands in the activity log, and is refused while a game runs or without
administrator rights."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import i18n
from app.agent import games
from app.agent.logger import log_action
from app.api import LocalizedRoute
from app.config import settings
from app.desktop import confirm
from app.i18n import tx, txc, txn
from app.models import (ActionLogEntry, ActionType, DeviceRequest, DriverBackupRef, DriverBackupRequest,
                        DriverPackagesRequest, DriverUpdateRequest)
from app.service.state import app_state
from app.system import drivers
from app.system.admin_rights import is_elevated
from app.tools import driver_actions, driver_jobs, driver_updates, restore_point

router = APIRouter(route_class=LocalizedRoute)

_BY_YOU = tx("Changed by you from the Drivers page.")


def _log(action: ActionType, target: str, applied: bool, result: str) -> None:
    entry = ActionLogEntry(action=action, target=target, reason=_BY_YOU, applied=applied, result=result)
    log_action(entry)
    app_state.add_actions([entry])


def _simulated(text: str) -> bool:
    return text.startswith("simulated")


def _change(action: ActionType, target: str, change) -> dict:
    """A quick change (seconds): run, log, and the next look at the page scans again."""
    try:
        result = change()
    except driver_actions.DriverActionError as e:
        _log(action, target, False, i18n.message(e))
        raise HTTPException(status_code=e.status, detail=i18n.message(e))
    text = result if isinstance(result, str) else result["result"]
    _log(action, target, not _simulated(text), text)
    drivers.forget()
    return {**({"result": text} if isinstance(result, str) else result), "simulated": _simulated(text)}


def _ready_to_install() -> None:
    if not is_elevated():
        raise HTTPException(status_code=403, detail=tx("Installing drivers needs OptiMind to run as administrator."))
    if games.status().get("current"):
        raise HTTPException(status_code=409, detail=tx("A game is running: close it first, then install (a driver "
                                                       "change can stop the screen or the sound for a moment)."))


def _restore_point(what: str) -> str:
    """Windows' own safety net before a driver change; the change goes ahead without one when
    System Protection is off (and says so)."""
    if settings.optimind_dry_run:
        return txc("restore point", "skipped (test mode)")
    try:
        restore_point.create(f"OptiMind AI (before {what})", "DEVICE_DRIVER_INSTALL")  # Windows' record: English
        return txc("restore point", "created")
    except restore_point.RestorePointError as e:
        return tx("not created: {reason}", reason=i18n.message(e))


def _start(kind: str, title: str, action: ActionType | None, target: str, work) -> dict:
    def on_end(job) -> None:
        drivers.forget()
        if action is None:
            return
        if job.state == "done":
            text = (job.result or {}).get("result") or tx("done")
            _log(action, target, not _simulated(text), text)
        else:
            _log(action, target, False, tx("error: {error}", error=job.error))

    try:
        return {"job": driver_jobs.start(kind, title, work, on_end)}
    except driver_jobs.JobBusy as e:
        raise HTTPException(status_code=409, detail=tx("Wait a moment: {reason}.", reason=i18n.message(e)))


@router.get("/drivers")
def get_drivers(refresh: bool = False) -> dict:
    """Every device and driver, the drivers apps added, the driver store, driver events, how
    Windows guards against bad drivers, the firmware, updates and what needs attention."""
    return drivers.report(refresh)


@router.get("/drivers/summary")
def get_drivers_summary() -> dict:
    """What needs attention, from the last scan (never waits for a new one): for Home."""
    return drivers.summary()


@router.get("/drivers/job")
def get_driver_job() -> dict:
    """The driver task running now or that ran last: step, progress, result."""
    return {"job": driver_jobs.status()}


@router.get("/drivers/tools")
def get_driver_tools() -> dict:
    """The makers' own driver apps for this PC, from the last scan: OptiMind's window checks an
    app it's asked to open is one of these."""
    return {"tools": drivers.tools()}


@router.post("/drivers/windows-update/check")
def check_windows_update() -> dict:
    """Asks Windows Update's servers for driver updates now (the same as Check for updates)."""
    def work(job) -> dict:
        job.update(tx("Asking Windows Update… (this can take a few minutes)"))
        if settings.optimind_dry_run:
            data = driver_updates.search_windows_update(online=False)  # test mode: what Windows already knows
        else:
            data = driver_updates.search_windows_update(online=True)
        n = len(data["updates"])
        return {"result": txn(n, "Windows Update offers {n} driver update", "Windows Update offers {n} driver updates")}

    return _start("windows_update_check", tx("Checking Windows Update"), None, "Windows Update", work)


@router.post("/drivers/windows-update/install")
def install_windows_update(req: DriverUpdateRequest) -> dict:
    """Installs driver updates Windows Update offers (never one older than the installed driver,
    never firmware), after a System Restore point."""
    _ready_to_install()
    offered = {u["id"]: u for u in ((drivers.report()["updates"].get("windows_update") or {}).get("updates") or [])}
    wanted = [i.lower() for i in req.ids]
    unknown = [i for i in wanted if i not in offered]
    if unknown:
        raise HTTPException(status_code=404,
                            detail=tx("Windows Update doesn't offer that driver (anymore): check again."))
    firmware = [offered[i]["title"] for i in wanted if offered[i]["verdict"] == "firmware"]
    if firmware:
        raise HTTPException(status_code=409, detail=tx("Not installed: OptiMind never installs firmware or BIOS "
                                                       "updates ({updates}).", updates="; ".join(firmware)))
    older = [offered[i]["title"] for i in wanted if offered[i]["verdict"] == "older"]
    if older:
        raise HTTPException(status_code=409,
                            detail=tx("Not installed: older than the driver you have ({updates}).",
                                      updates="; ".join(older)))
    titles = "; ".join(offered[i]["title"] for i in wanted)

    def work(job) -> dict:
        job.update(tx("Creating a restore point…"), 0.02)
        point = _restore_point("a driver update")
        if settings.optimind_dry_run:
            return {"result": tx("simulated (test mode): would install {updates}", updates=titles), "restore_point": point}
        done = driver_updates.install_windows_update(wanted, job)
        text = (tx("installed {done} of {n}: {updates}; restart to finish", done=done["installed"], n=len(wanted),
                   updates=titles) if done["restart"]
                else tx("installed {done} of {n}: {updates}", done=done["installed"], n=len(wanted), updates=titles))
        return {**done, "result": text, "restore_point": point}

    return _start("windows_update_install", tx("Installing driver updates"), ActionType.INSTALL_DRIVER, titles, work)


def _nvidia_gpu() -> dict:
    report = drivers.report()
    gpu = next((d for d in report["devices"] if d["present"] and d["class"] == "Display" and d["driver"]
                and "nvidia" in d["driver"]["provider"].lower()), None)
    if gpu is None:
        raise HTTPException(status_code=404, detail=tx("No NVIDIA graphics card with NVIDIA's driver on this PC."))
    return gpu


@router.post("/drivers/nvidia/check")
def check_nvidia() -> dict:
    """Asks NVIDIA's site for the newest Game Ready driver for this card now."""
    gpu = _nvidia_gpu()
    return driver_updates.check_nvidia(gpu["name"], gpu["driver"]["version"], force=True)


@router.post("/drivers/nvidia/install")
def install_nvidia() -> dict:
    """Downloads the newest NVIDIA driver from NVIDIA, checks NVIDIA's signature, and starts
    NVIDIA's installer, after a System Restore point."""
    _ready_to_install()
    gpu = _nvidia_gpu()
    info = driver_updates.check_nvidia(gpu["name"], gpu["driver"]["version"])
    if info.get("error"):
        raise HTTPException(status_code=502, detail=tx("Couldn't ask NVIDIA: {error}.", error=info["error"]))
    if not info.get("newer"):
        raise HTTPException(status_code=409, detail=tx("NVIDIA {version} is already the newest driver.",
                                                       version=info.get("installed")))
    target = f"NVIDIA {info['latest']} ({gpu['name']})"

    def work(job) -> dict:
        job.update(tx("Creating a restore point…"), 0.01)
        point = _restore_point(f"NVIDIA {info['latest']}")
        if settings.optimind_dry_run:
            return {"result": tx("simulated (test mode): would download and start NVIDIA {version}'s installer",
                                 version=info["latest"]), "restore_point": point}
        done = driver_updates.install_nvidia(info, job)
        return {**done, "restore_point": point,
                "result": tx("NVIDIA {version}: its installer is open (signed by NVIDIA); follow its steps",
                             version=info["latest"])}

    return _start("nvidia_install", tx("Installing NVIDIA {version}", version=info["latest"]), ActionType.INSTALL_DRIVER,
                  target, work)


@router.post("/drivers/scan")
def scan_hardware() -> dict:
    """Scan for hardware changes, as Device Manager does."""
    return _change(ActionType.SCAN_DEVICES, tx("devices"), driver_actions.scan_hardware)


@router.post("/drivers/devices/restart")
def restart_device(req: DeviceRequest) -> dict:
    return _change(ActionType.RESTART_DEVICE, req.id, lambda: driver_actions.restart_device(req.id))


@router.post("/drivers/devices/enable")
def enable_device(req: DeviceRequest) -> dict:
    return _change(ActionType.ENABLE_DEVICE, req.id, lambda: driver_actions.enable_device(req.id))


@router.post("/drivers/packages/remove")
def remove_packages(req: DriverPackagesRequest) -> dict:
    """Removes old driver versions (only the ones marked removable), after a restore point."""
    if not is_elevated():
        raise HTTPException(status_code=403, detail=tx("Removing drivers needs OptiMind to run as administrator."))

    def change() -> dict:
        point = _restore_point("removing old driver versions")
        done = driver_actions.remove_old_packages(req.names)
        return {**done, "restore_point": point}

    return _change(ActionType.REMOVE_DRIVER_PACKAGE, ", ".join(n.lower() for n in req.names), change)


@router.get("/drivers/backups")
def get_backups() -> dict:
    return {"backups": driver_actions.backups()}


@router.post("/drivers/backup")
def backup_drivers(req: DriverBackupRequest) -> dict:
    """Copies every driver from other makers to <drive>\\OptiMind driver backup\\<date>."""
    if not is_elevated():
        raise HTTPException(status_code=403, detail=tx("Backing up drivers needs OptiMind to run as administrator."))
    return _start("backup", tx("Backing up drivers to {drive}", drive=req.drive.upper()), ActionType.BACKUP_DRIVERS,
                  req.drive.upper(), lambda job: driver_actions.backup(req.drive, job))


@router.post("/drivers/backups/restore")
def restore_backup(req: DriverBackupRef) -> dict:
    """Installs every driver of one of OptiMind's backups, after a restore point and your Yes in
    OptiMind's own dialog."""
    _ready_to_install()
    try:
        folder = driver_actions.known_backup(req.path)
    except driver_actions.DriverActionError as e:
        raise HTTPException(status_code=e.status, detail=i18n.message(e))
    if not confirm.ask(tx("Install every driver from this backup?"),
                       tx("{folder}\n\nWindows adds each driver in it and installs the ones that fit your devices. "
                          "OptiMind creates a restore point first.", folder=str(folder))):
        raise HTTPException(status_code=409, detail=tx("Not started: it wasn't confirmed in OptiMind's own dialog."))

    def work(job) -> dict:
        job.update(tx("Creating a restore point…"), 0.02)
        point = _restore_point("restoring drivers")
        return {**driver_actions.restore(req.path, job), "restore_point": point}

    return _start("restore", tx("Installing drivers from a backup"), ActionType.RESTORE_DRIVERS, req.path, work)


@router.post("/drivers/backups/open")
def open_backup(req: DriverBackupRef) -> dict:
    try:
        return {"result": driver_actions.open_backup(req.path)}
    except driver_actions.DriverActionError as e:
        raise HTTPException(status_code=e.status, detail=i18n.message(e))
