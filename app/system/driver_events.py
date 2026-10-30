"""
What Windows recorded about drivers (read-only), and how it protects itself from bad ones.

Events of the last 30 days, from Windows' own logs:
  - a device's driver failed to load (Kernel-PnP 219) or a device had a problem starting
    (Kernel-PnP/Configuration 411);
  - a driver service didn't start (Service Control Manager 7000, and 7026 for the drivers
    that should start with Windows);
  - a device stopped responding and was reset: a network adapter (NDIS 10400: "the network
    driver detected that its hardware has stopped responding"), a storage controller (129
    from storahci, stornvme and the other storage port drivers), a disk whose reads or
    writes had to be retried (disk 153) or reported a bad block (disk 7), the graphics
    driver (nvlddmkm 153; Display 4101: "stopped responding and has recovered");
  - Code Integrity blocked a driver (3077), or would block it once a policy is enforced
    (3076), or refused a driver file whose signature didn't pass (3033, 3004; driver files
    only: the same events also record app DLLs a protected process refused).

Protection state:
  - the Windows Driver Policy: since the April 2026 update, drivers signed only through the
    retired cross-signing program aren't trusted by default. Windows first evaluates a PC
    (drivers that would be blocked are only logged, 3076) and enforces the policy after 250
    hours and 3 restarts without such a driver (3077). Its policy files show which mode a PC
    is in (Microsoft, "The Windows Driver Policy");
  - the Microsoft Vulnerable Driver Blocklist (on by default since Windows 11 22H2, KB5020779);
  - test signing or disabled integrity checks, which let unsigned drivers load.
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from app.config import IS_WINDOWS
from app.i18n import tx
from app.system.stability import parse_time

logger = logging.getLogger("optimind.driver_events")

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_MAX_EVENTS = 5000  # per query: a network adapter resetting all night logs thousands
DRIVER_POLICY_ENFORCED = "8f9cb695-5d48-48d6-a329-7202b44607e3"
DRIVER_POLICY_EVALUATION = "784c4414-79f4-4c32-a6a5-f0fb42a51d0d"
BLOCKLIST_POLICY = "d2bda982-ccf6-4344-ac5b-0b44427b6816"
POLICY_NAMES = {
    DRIVER_POLICY_ENFORCED: "the Windows Driver Policy",
    DRIVER_POLICY_EVALUATION: "the Windows Driver Policy (evaluation)",
    BLOCKLIST_POLICY: "the Microsoft Vulnerable Driver Blocklist",
}
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

STORAGE_PORTS = ("storahci", "stornvme", "iaStorAC", "iaStorAVC", "iaStorA", "iaStorV", "amdsata", "amdsbs",
                 "nvstor", "storport")
_RECENT = " and TimeCreated[timediff(@SystemTime) <= {ms}]]]"
_QUERIES = [  # (channel, XPath): one per kind of event, so a flood of one never hides the others
    ("System", "*[System[((Provider[@Name='Microsoft-Windows-Kernel-PnP'] and EventID=219)"
               " or (Provider[@Name='Service Control Manager'] and (EventID=7000 or EventID=7026)))" + _RECENT),
    ("System", "*[System[Provider[@Name='Microsoft-Windows-NDIS'] and EventID=10400" + _RECENT),
    ("System", "*[System[(" + " or ".join(f"Provider[@Name='{p}']" for p in STORAGE_PORTS) + ") and EventID=129"
     + _RECENT),
    ("System", "*[System[Provider[@Name='disk'] and (EventID=153 or EventID=7)" + _RECENT),
    ("System", "*[System[((Provider[@Name='nvlddmkm'] and EventID=153) or (Provider[@Name='Display'] and "
               "EventID=4101))" + _RECENT),
    ("Microsoft-Windows-Kernel-PnP/Configuration", "*[System[EventID=411" + _RECENT),
    ("Microsoft-Windows-CodeIntegrity/Operational", "*[System[(EventID=3076 or EventID=3077 or EventID=3033 or "
                                                    "EventID=3004)" + _RECENT),
]
RESET_KINDS = ("network_reset", "storage_reset", "disk_retry", "disk_bad_block", "gpu_error")


def event_fields(xml: str) -> dict | None:
    """Provider, ID, time and the named data of one event's XML."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    system = root.find(f"{_NS}System")
    if system is None:
        return None
    provider = system.find(f"{_NS}Provider")
    created = system.find(f"{_NS}TimeCreated")
    try:
        event_id = int(system.findtext(f"{_NS}EventID") or 0)
    except ValueError:
        return None
    when = parse_time(created.get("SystemTime", "")) if created is not None else None
    data, unnamed = {}, []
    for item in root.iter(f"{_NS}Data"):
        value = (item.text or "").strip()
        if item.get("Name"):
            data[item.get("Name")] = value
        else:
            unnamed.append(value)
    return {"provider": provider.get("Name", "") if provider is not None else "", "id": event_id, "time": when,
            "data": data, "values": unnamed + list(data.values())}


def _file_name(path: str) -> str:
    return re.split(r"[\\/]", path or "")[-1]


def classify(fields: dict, driver_names: dict[str, str]) -> dict | None:
    """One driver event in plain terms: kind, subject (a driver, file or device) and details.
    `driver_names` maps driver services' display names (lowercase) to their names, to tell a
    driver that didn't start (7000) from an ordinary service. None when it's not about a driver."""
    pid, provider, data, values = fields["id"], fields["provider"], fields["data"], fields["values"]
    if provider == "Microsoft-Windows-Kernel-PnP" and pid == 219:
        driver = next((v for v in values if v.lower().startswith("\\driver\\")), "")
        device = next((v for v in values if "\\" in v and not v.lower().startswith("\\driver\\")), "")
        return {"kind": "failed_to_load", "subject": driver.split("\\")[-1] or "a driver", "device": device}
    if provider == "Service Control Manager" and pid == 7026:
        listed = data.get("param1") or (values[0] if values else "")
        names = [n.strip() for n in re.split(r"[\r\n]+", listed) if n.strip()]
        return {"kind": "boot_driver_not_loaded", "subject": ", ".join(names) or "a driver", "names": names}
    if provider == "Service Control Manager" and pid == 7000:
        shown = data.get("param1") or (values[0] if values else "")
        name = driver_names.get(shown.lower())
        if not name:
            return None  # an ordinary service, not a driver
        return {"kind": "service_not_started", "subject": name, "detail": data.get("param2") or ""}
    if pid == 411:
        try:
            problem = int(data.get("Problem") or "0", 16)
        except ValueError:
            problem = 0
        return {"kind": "device_problem", "subject": data.get("DeviceInstanceId") or tx("a device"), "problem": problem,
                "status": data.get("Status") or ""}
    if provider == "Microsoft-Windows-NDIS" and pid == 10400:
        return {"kind": "network_reset", "subject": data.get("AdapterName") or tx("a network adapter")}
    if provider in STORAGE_PORTS and pid == 129:
        port = next((v for v in values if v.lower().startswith("\\device\\")), "")
        return {"kind": "storage_reset", "subject": provider, "port": port}
    if provider == "disk" and pid in (153, 7):
        disk = re.search(r"harddisk(\d+)", " ".join(values), re.IGNORECASE)
        return {"kind": "disk_retry" if pid == 153 else "disk_bad_block",
                "subject": tx("Disk {number}", number=int(disk.group(1))) if disk else tx("a disk"),
                "disk": int(disk.group(1)) if disk else None}
    if (provider, pid) in (("nvlddmkm", 153), ("Display", 4101)):
        driver = "nvlddmkm" if provider == "nvlddmkm" else next((v for v in values if v), tx("the graphics driver"))
        return {"kind": "gpu_error", "subject": driver}
    if pid in (3076, 3077, 3033, 3004):
        file = data.get("FileNameBuffer") or data.get("File Name") or next((v for v in values if "\\" in v), "")
        if not _file_name(file).lower().endswith(".sys"):
            return None  # an app's DLL a protected process refused: not a driver
        policies = {g.lower() for v in values for g in _GUID.findall(v)}
        policy = next((p for p in POLICY_NAMES if p in policies), None)
        kind = {3076: "would_be_blocked", 3077: "blocked"}.get(pid, "signature_refused")
        return {"kind": kind, "subject": _file_name(file), "file": file, "policy": policy,
                "policy_name": POLICY_NAMES.get(policy) or data.get("PolicyNameBuffer") or None}
    return None


def summarize(items: list[tuple[datetime, dict]]) -> list[dict]:
    """Events grouped by what happened to which driver or device, newest first, with a count."""
    groups: dict[tuple, dict] = {}
    for when, item in items:
        key = (item["kind"], item["subject"].lower())
        g = groups.get(key)
        if g is None:
            groups[key] = g = {**item, "count": 0, "first": when, "last": when}
        g["count"] += 1
        g["first"], g["last"] = min(g["first"], when), max(g["last"], when)
    out = sorted(groups.values(), key=lambda g: g["last"], reverse=True)
    for g in out:
        g["first"], g["last"] = g["first"].isoformat(), g["last"].isoformat()
    return out


def read_events(days: int, driver_names: dict[str, str]) -> list[dict]:
    """The driver events of the last `days` days, grouped (a log that can't be read is skipped)."""
    if not IS_WINDOWS:
        return []
    import win32evtlog

    items = []
    for channel, xpath in _QUERIES:
        try:
            flags = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection
            handle = win32evtlog.EvtQuery(channel, flags, xpath.replace("{ms}", str(days * 86_400_000)))
        except Exception as e:
            logger.debug("Couldn't read %s: %s", channel, e)
            continue
        found = 0
        while found < _MAX_EVENTS:
            batch = win32evtlog.EvtNext(handle, 100)
            if not batch:
                break
            for event in batch:
                found += 1
                fields = event_fields(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                item = classify(fields, driver_names) if fields and fields["time"] else None
                if item:
                    items.append((fields["time"], item))
    return summarize(items)


# ------------------------------------------------------------ protection state

def _reg(path: str, name: str):
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def driver_policy_mode(active_policies: set[str]) -> str:
    """"enforced", "evaluation" or "off" from the policy files Windows has active."""
    if DRIVER_POLICY_ENFORCED in active_policies:
        return "enforced"
    if DRIVER_POLICY_EVALUATION in active_policies:
        return "evaluation"
    return "off"


def protection() -> dict:
    """The Windows Driver Policy's mode, the vulnerable driver blocklist, Memory integrity,
    unsigned-driver modes and whether Windows Update may deliver drivers (registry and the
    Code Integrity policy folder: no administrator rights needed)."""
    if not IS_WINDOWS:
        return {}
    folder = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "CodeIntegrity" / "CiPolicies" / "Active"
    try:
        active = {p.stem.strip("{}").lower() for p in folder.glob("*.cip")}
    except OSError:
        active = set()
    options = str(_reg(r"SYSTEM\CurrentControlSet\Control", "SystemStartOptions") or "").upper()
    blocklist = _reg(r"SYSTEM\CurrentControlSet\Control\CI\Config", "VulnerableDriverBlocklistEnable")
    hvci = _reg(r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity", "Enabled")
    return {
        "driver_policy": driver_policy_mode(active),
        "blocklist": blocklist != 0,  # on unless turned off (on by default since Windows 11 22H2)
        "memory_integrity": hvci == 1,
        "test_signing": "TESTSIGNING" in options,
        "integrity_checks_off": "DISABLE_INTEGRITY_CHECKS" in options,
        "windows_update_drivers": _reg(r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate",
                                       "ExcludeWUDriversInQualityUpdate") != 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
