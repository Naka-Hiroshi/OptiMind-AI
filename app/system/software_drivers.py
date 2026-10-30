"""
Drivers that apps installed (read-only): kernel drivers that don't belong to a device,
such as anti-cheat, antivirus, virtual machines, VPNs, fan and RGB control and virtual
audio. They run with the same rights as Windows itself, so it's worth knowing which ones
are there, who made them, and whether one is a known risk.

From the Service Control Manager's list of driver services (kernel and file system
drivers), their registry entries and the version information in each file. Windows' own
drivers are recognized by their maker or by the driver package they came in; file
details are kept per file, so only the first look reads them.
"""
from __future__ import annotations

import os
import re
import threading

from app.config import IS_WINDOWS
from app.i18n import tx

_START = {0: "boot", 1: "system", 2: "automatic", 3: "manual", 4: "disabled"}
_SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
_DRIVERS_DIR = os.path.normcase(os.path.join(_SYSTEM_ROOT, "System32", "drivers"))
_REPOSITORY = re.compile(r"\\driverstore\\filerepository\\([^\\]+)\\", re.IGNORECASE)

# Drivers with a publicly documented flaw that malware uses to get into the Windows kernel,
# which Windows itself flags. Matched by file name; each entry says where the facts come from.
KNOWN_RISKS = {
    "winring0x64.sys": {
        "title": "WinRing0",
        "why": tx("A hardware-access driver used by many fan control, RGB and monitoring apps. Any program on the PC "
                  "can use it to read and write all memory and take full control of Windows (CVE-2020-14979), and "
                  "Microsoft Defender flags it as HackTool:Win32/Winring0 since March 2025."),
        "fix": tx("Update the app that installed it (FanControl, for example, replaced it with PawnIO in version 239), "
                  "or uninstall that app if you don't use it."),
        "source": "https://cveawg.mitre.org/api/cve/CVE-2020-14979",
    },
}
KNOWN_RISKS["winring0.sys"] = KNOWN_RISKS["winring0x64.sys"]

# What a driver is for, from its service or file name and its maker (a label on the page).
_CATEGORIES = (
    (tx("Anti-cheat"), r"easyanticheat|^eac|beservice|bedaisy|battleye|vgk|vanguard|faceit|anticheatexpert|^ace-|"
                   r"mhyprot|xhunter|npgg|gameguard|eaanticheat|pnkbstr|javelin"),
    (tx("Security"), r"defender|kaspersky|^kl|avast|\bavg|avira|eset|ehdrv|bitdefender|trend ?micro|sophos|"
                 r"malwarebytes|^mbam|norton|symantec|mcafee|^mfe|webroot|crowdstrike|sentinel|cylance"),
    (tx("Virtual machines"), r"vmware|^vm[a-z]|vbox|virtualbox|qemu|parallels|bluestacks|^hd-|ldplayer|memu"),
    (tx("VPN and network"), r"^tap|wintun|wireguard|openvpn|nordvpn|expressvpn|proton|surfshark|zerotier|tailscale|"
                        r"hamachi|npcap|winpcap|netlimiter"),
    (tx("Hardware control"), r"winring0|pawnio|rtcore|inpoutx64|hwinfo|cpuz|gpu-?z|aida|asio|msio|gdrv|nzxt|corsair|"
                         r"icue|aura|lamparray|rgb|fancontrol|logi_|logitech|razer|steelseries|ssdevfactory"),
    (tx("Audio"), r"vb-?audio|voicemeeter|virtual ?audio|sonar|nahimic|dolby|dts|nvvad|krisp"),
    (tx("Capture and streaming"), r"\bobs|elgato|avermedia|streamlabs|xsplit|nvspcap|medal"),
    (tx("Storage and backup"), r"psmounterex|acronis|macrium|veeam|aomei|easeus|paragon|imdisk|ramdisk|veracrypt"),
)
_CATEGORY_RE = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _CATEGORIES]
_OTHER = tx("Other")


def category(name: str, file: str, company: str) -> str:
    text = " ".join((name or "", os.path.basename(file or ""), company or ""))
    return next((label for label, pattern in _CATEGORY_RE if pattern.search(text) or pattern.search(name or "")),
                _OTHER)


def image_path(raw: str | None, service: str) -> str:
    """A driver service's ImagePath as a normal file path (it's often written as
    \\SystemRoot\\..., System32\\... or \\??\\C:\\...), or where Windows looks without one."""
    if not raw:
        return os.path.join(_SYSTEM_ROOT, "System32", "drivers", f"{service}.sys")
    path = os.path.expandvars(raw.strip().strip('"'))
    low = path.lower()
    if low.startswith("\\systemroot\\"):
        return _SYSTEM_ROOT + path[len("\\SystemRoot"):]
    if low.startswith("system32\\"):
        return os.path.join(_SYSTEM_ROOT, path)
    if low.startswith("\\??\\"):
        return path[4:]
    return path


_info_cache: dict[tuple, dict] = {}
_info_lock = threading.Lock()


def file_details(path: str) -> dict | None:
    """Company, product, description and version from a file's version resource (None when
    the file is missing). Kept per file, size and time, so it's read once."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path.lower(), st.st_size, st.st_mtime_ns)
    with _info_lock:
        if key in _info_cache:
            return _info_cache[key]
    details = {"company": "", "product": "", "description": "", "version": ""}
    try:
        import win32api

        lang, page = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
        base = f"\\StringFileInfo\\{lang:04x}{page:04x}\\"
        for field, name in (("company", "CompanyName"), ("product", "ProductName"),
                            ("description", "FileDescription"), ("version", "FileVersion")):
            try:
                details[field] = " ".join(str(win32api.GetFileVersionInfo(path, base + name) or "").split())
            except Exception:
                pass
    except Exception:
        pass  # no version resource: listed without these details
    with _info_lock:
        _info_cache[key] = details
    return details


def is_microsoft(details: dict | None) -> bool:
    return bool(details) and (details.get("company") or "").lower().startswith("microsoft")


def _registry(service: str) -> dict:
    import winreg

    values: dict = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{service}") as key:
            for name in ("ImagePath", "Start", "Type"):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    values[name] = None
    except OSError:
        pass
    return values


def driver_services() -> list[dict]:
    """Every kernel and file system driver service: name, state, start type and file."""
    if not IS_WINDOWS:
        return []
    import win32service

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE)
    try:
        services = win32service.EnumServicesStatusEx(scm, win32service.SERVICE_DRIVER,
                                                     win32service.SERVICE_STATE_ALL)
    finally:
        win32service.CloseServiceHandle(scm)
    out = []
    for s in services:
        reg = _registry(s["ServiceName"])
        out.append({
            "name": s["ServiceName"], "display_name": s.get("DisplayName") or s["ServiceName"],
            "running": s["CurrentState"] == win32service.SERVICE_RUNNING,
            "start": _START.get(reg.get("Start"), "unknown"),
            "file_system": bool((reg.get("Type") or 0) & 0x2),
            "file": image_path(reg.get("ImagePath"), s["ServiceName"]),
        })
    return out


def describe(service: dict, details: dict | None, device_services: set[str]) -> dict:
    """One driver service as the Drivers page shows it (also built by the tests)."""
    details = details or {}
    return {
        **service,
        "exists": bool(details),
        "company": details.get("company", ""),
        "product": details.get("product", ""),
        "description": details.get("description", ""),
        "version": details.get("version", ""),
        "microsoft": is_microsoft(details),
        "device_driver": service["name"].lower() in device_services,
        "category": category(service["name"], service["file"], details.get("company", "")),
        "risk": KNOWN_RISKS.get(os.path.basename(service["file"]).lower()),
        "leftover": False,
    }


def pick(services: list[dict], device_services: set[str], third_party_folders: set[str], details_of) -> list[dict]:
    """The drivers worth listing: from other makers than Microsoft and not a device's own driver
    (those are on the device list), plus any known risk, and any driver set to start with
    Windows whose file is gone (an app was uninstalled and left its driver entry behind).
    Skipped without reading the file: Windows' drivers for hardware this PC doesn't have
    (in System32\\drivers and not running) and drivers from packages that came with Windows."""
    device_services = {s.lower() for s in device_services}
    third_party_folders = {f.lower() for f in third_party_folders}
    out = []
    for service in services:
        path = service["file"]
        risky = os.path.basename(path).lower() in KNOWN_RISKS
        folder = _REPOSITORY.search(path)
        if not risky:
            if not service["running"] and os.path.normcase(os.path.dirname(path)) == _DRIVERS_DIR \
                    and os.path.exists(path):
                continue
            if folder and folder.group(1).lower() not in third_party_folders:
                continue  # in a package that came with Windows
        details = details_of(path)
        d = describe(service, details, device_services)
        if details is None:
            d["leftover"] = service["start"] in ("boot", "system", "automatic")
            if d["leftover"] or risky:
                out.append(d)
            continue
        if risky or (not d["microsoft"] and not d["device_driver"]):
            out.append(d)
    out.sort(key=lambda d: (not d["risk"], not d["leftover"], not d["running"], d["category"], d["name"].lower()))
    return out


def software_drivers(device_services: set[str], third_party_folders: set[str]) -> list[dict]:
    return pick(driver_services(), device_services, third_party_folders, file_details)
