"""
The Drivers page (read-only): every device and its driver, the drivers apps added, the
driver store, what Windows' logs recorded about drivers, how Windows guards against bad
drivers, the motherboard's firmware, where each driver comes from and who signed it, and
the makers' own driver apps for this PC (maker_tools), with what needs attention first.

A full scan takes about 1–2 seconds (the first one longer: it reads each driver file's
details once) and is kept 10 minutes. Home shows its findings from the last scan without
waiting for a new one. Update checks (Windows Update's own list, NVIDIA's site) run in the
background at most every few hours, so opening the page never waits on the internet.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psutil

from app import i18n
from app.config import IS_WINDOWS, settings
from app.i18n import tx, txc, txn
from app.system import devices as devices_mod
from app.system import driver_events, driver_store, hardware, maker_tools, software_drivers
from app.system.admin_rights import is_elevated
from app.tools import driver_actions, driver_jobs, driver_updates

logger = logging.getLogger("optimind.drivers")

_CACHE_SECONDS = 600.0
_SUMMARY_SECONDS = 1800.0  # Home: a scan this old is refreshed in the background
_WU_CACHE_SECONDS = 6 * 3600.0
_EVENT_DAYS = 30
_lock = threading.Lock()
_cache: tuple[float, dict, dict] | None = None  # (when, report, hardware IDs by device)
_background = threading.Lock()

LINKS = {
    "nvidia": (tx("NVIDIA drivers"), "https://www.nvidia.com/en-us/drivers/"),
    "nvidia_app": ("NVIDIA App", "https://www.nvidia.com/en-us/software/nvidia-app/"),
    "amd": (tx("AMD drivers and support"), "https://www.amd.com/en/support/download/drivers.html"),
    "intel": ("Intel Driver & Support Assistant", "https://www.intel.com/content/www/us/en/support/detect.html"),
    "realtek": (tx("Realtek downloads"), "https://www.realtek.com/Download/Overview"),
    "windows_update": (tx("Optional driver updates"), "ms-settings:windowsupdate-optionalupdates"),
    "windows_security": (tx("Windows Security"), "ms-settings:windowsdefender"),
    "ms_drivers": (tx("Microsoft: getting drivers"), "https://support.microsoft.com/en-us/windows/hardware/drivers/"
                                                     "automatically-get-recommended-and-updated-hardware-drivers"),
    "ms_codes": (tx("Microsoft: Device Manager error codes"), "https://support.microsoft.com/en-us/windows/hardware/"
                                                              "drivers/error-codes-in-device-manager-in-windows"),
    "ms_policy": (tx("Microsoft: the Windows Driver Policy"), "https://support.microsoft.com/en-us/windows/hardware/"
                                                              "drivers/the-windows-driver-policy"),
    "ms_blocklist": (tx("Microsoft: the vulnerable driver blocklist"),
                     "https://support.microsoft.com/en-us/servicing/os/windows/2022/10/kb5020779-the-vulnerable-"
                     "driver-blocklist-after-the-october-2022-preview-release"),
}
# The parts of the PC whose drivers the page lists first (role())
GRAPHICS, SOUND, NETWORK, BLUETOOTH, STORAGE, USB, CHIPSET = (
    tx("Graphics"), tx("Sound"), tx("Network"), tx("Bluetooth"), tx("Storage"), tx("USB"), tx("Chipset"))
_MONTHS = (txc("month", "January"), txc("month", "February"), txc("month", "March"), txc("month", "April"),
           txc("month", "May"), txc("month", "June"), txc("month", "July"), txc("month", "August"),
           txc("month", "September"), txc("month", "October"), txc("month", "November"), txc("month", "December"))
_SHORT_MONTHS = (txc("short month", "Jan"), txc("short month", "Feb"), txc("short month", "Mar"),
                 txc("short month", "Apr"), txc("short month", "May"), txc("short month", "Jun"),
                 txc("short month", "Jul"), txc("short month", "Aug"), txc("short month", "Sep"),
                 txc("short month", "Oct"), txc("short month", "Nov"), txc("short month", "Dec"))
_MAKERS = (  # the maker of the board or PC: where its drivers and BIOS updates are (and its app: maker_tools)
    ("asus", r"asus", "ASUS Download Center", "https://www.asus.com/support/download-center/"),
    ("msi", r"micro-star|\bmsi\b", "MSI Download Center", "https://www.msi.com/support/download"),
    ("gigabyte", r"gigabyte", "GIGABYTE Support", "https://www.gigabyte.com/Support"),
    ("asrock", r"asrock", "ASRock Support", "https://www.asrock.com/support/index.asp"),
    ("dell", r"\bdell\b|alienware", "Dell Drivers & Downloads", "https://www.dell.com/support/home"),
    ("hp", r"\bhp\b|hewlett", "HP Software and Drivers", "https://support.hp.com/drivers"),
    ("lenovo", r"lenovo", "Lenovo Support", "https://pcsupport.lenovo.com/"),
    ("acer", r"\bacer\b", "Acer Drivers and Manuals", "https://www.acer.com/us-en/support/drivers-and-manuals"),
)
_AM4 = {"a320", "b350", "x370", "b450", "x470", "a520", "b550", "x570"}
_GPU_VENDORS = {"VEN_10DE": "nvidia", "VEN_1002": "amd", "VEN_1022": "amd", "VEN_8086": "intel"}
_PLACEHOLDER = re.compile(r"system manufacturer|to be filled|default string|o\.e\.m\.|^$", re.IGNORECASE)

# Problem codes a device restart may clear, that a hardware scan may fix, that a new driver may fix
_RESTARTABLE = {10, 31, 43, 54}
_SCANNABLE = {18, 19, 24, 28, 32, 37, 39, 40, 41}
_NEEDS_DRIVER = {1, 10, 18, 28, 31, 39, 48, 52}
_ORDER = {"warn": 0, "tip": 1, "info": 2, "good": 3}


def _check(id: str, title: str, status: str, summary: str, detail: str = "", action: str = "",
           link: str | None = None, actions: list[dict] | None = None, items: list[str] | None = None) -> dict:
    """status: "warn" (something's wrong), "tip" (worth doing), "info" or "good"; actions are
    buttons ({"do": ..., "label": ..., "target": ...}), items a short list of what it's about."""
    return {"id": id, "title": title, "status": status, "summary": summary, "detail": detail, "action": action,
            "link": link, "actions": actions or [], "items": items or []}


def _button(do: str, label: str, target: str | None = None) -> dict:
    return {"do": do, "label": label, "target": target}


def _years(iso_date: str | None, today: date | None = None) -> float | None:
    if not iso_date:
        return None
    try:
        return ((today or date.today()) - date.fromisoformat(iso_date[:10])).days / 365.25
    except ValueError:
        return None


def _month_year(iso_date: str | None) -> str:
    """"October 2019", in the reader's language."""
    try:
        when = date.fromisoformat(iso_date[:10])
    except (TypeError, ValueError):
        return tx("an unknown date")
    return tx("{month} {year}", month=_MONTHS[when.month - 1], year=when.year)


def _day(when: datetime) -> str:
    """"Oct 09", in the reader's language."""
    return tx("{month} {day}", month=_SHORT_MONTHS[when.month - 1], day=f"{when:%d}")


def _is_microsoft(name: str) -> bool:
    return (name or "").strip().lower().startswith("microsoft")


# ------------------------------------------------------------ firmware and makers

def firmware() -> dict:
    """Board, PC and BIOS as the firmware reports them (the registry copy Windows keeps at start)."""
    import winreg

    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
            for name in ("BaseBoardManufacturer", "BaseBoardProduct", "BIOSVendor", "BIOSVersion", "BIOSReleaseDate",
                         "SystemManufacturer", "SystemProductName"):
                try:
                    values[name] = str(winreg.QueryValueEx(key, name)[0]).strip()
                except OSError:
                    values[name] = ""
    except OSError:
        return {}
    bios_date, _ = driver_store.parse_driver_ver(values["BIOSReleaseDate"])
    system = values["SystemManufacturer"] if not _PLACEHOLDER.search(values["SystemManufacturer"]) else ""
    board_maker = values["BaseBoardManufacturer"]
    return {
        "board": {"maker": board_maker, "model": values["BaseBoardProduct"]},
        "system": {"maker": system, "model": values["SystemProductName"] if system else ""},
        "bios": {"vendor": values["BIOSVendor"], "version": values["BIOSVersion"], "date": bios_date},
        "support": maker_support(system or board_maker),
    }


def maker_support(maker: str) -> dict | None:
    for key, pattern, label, url in _MAKERS:
        if re.search(pattern, maker or "", re.IGNORECASE):
            return {"key": key, "label": label, "url": url}
    return None


def amd_chipset_link(board_model: str) -> tuple[str, str]:
    """AMD's chipset driver page for the board's chipset (AM4: B450, X570…), else AMD's own finder."""
    match = re.search(r"\b([ABX]\d{3})", board_model or "", re.IGNORECASE)
    chip = match.group(1).lower() if match else ""
    if chip in _AM4:
        return (tx("AMD {chipset} chipset drivers", chipset=chip.upper()),
                f"https://www.amd.com/en/support/downloads/drivers.html/chipsets/am4/{chip}.html")
    return LINKS["amd"]


# ------------------------------------------------------------ key drivers

def role(d: dict) -> str | None:
    """The part a device plays, for the devices worth keeping an eye on (None for the rest)."""
    if not d["present"] or not d["driver"]:
        return None
    cls, bus = d["class"], (d["bus"] or "").upper()
    provider = d["driver"]["provider"].lower()
    if cls == "Display":
        return GRAPHICS
    if cls == "MEDIA" and bus in ("HDAUDIO", "PCI", "USB"):
        return SOUND
    if cls == "Net" and bus in ("PCI", "USB"):
        return NETWORK
    if cls == "Bluetooth" and bus in ("PCI", "USB"):
        return BLUETOOTH
    if cls in ("SCSIAdapter", "HDC") and bus == "PCI":
        return STORAGE
    if cls == "USB" and bus == "PCI":
        return USB
    if cls in ("System", "SecurityDevices") and bus in ("PCI", "ACPI") and re.search(r"advanced micro|amd|intel",
                                                                                      provider):
        return CHIPSET
    return None


def _vendor_of(d: dict) -> str | None:
    for key, vendor in _GPU_VENDORS.items():
        if key in (d.get("hardware_id") or d["id"]).upper():
            return vendor
    text = f"{d['manufacturer']} {(d['driver'] or {}).get('provider', '')}".lower()
    return "nvidia" if "nvidia" in text else "amd" if "advanced micro" in text or "amd" in text else \
        "intel" if "intel" in text else None


def source_for(d: dict, part: str, fw: dict) -> dict:
    """Where this driver's updates come from: the maker named by Microsoft's own advice."""
    support = fw.get("support") if fw else None
    vendor = _vendor_of(d)
    provider = (d["driver"] or {}).get("provider", "")
    onboard = d["bus"].upper() != "USB"
    if part == "Graphics" and vendor in ("nvidia", "amd", "intel"):
        label, url = LINKS[vendor]
    elif part == "Chipset" and vendor == "amd":
        label, url = amd_chipset_link(fw.get("board", {}).get("model", "") if fw else "")
    elif part == "Chipset" and vendor == "intel":
        label, url = LINKS["intel"]
    elif _is_microsoft(provider):
        label, url = tx("Windows Update (Windows' own driver)"), LINKS["windows_update"][1]
    elif part == "Network" and "realtek" in provider.lower():
        label, url = tx("Realtek downloads (network controllers)"), LINKS["realtek"][1]
    elif part in ("Sound", "Network", "Bluetooth") and onboard and support:
        label, url = tx("{site}: your board's page", site=support["label"]), support["url"]
    else:
        label, url = tx("Windows Update, then the device maker's website"), LINKS["windows_update"][1]
    return {"label": label, "url": url}


def add_signers(found: list[dict], packages: list[dict]) -> None:
    """Who signed each device's driver: its package's signer for a driver from another maker
    (oemNN.inf), Windows for Windows' own (every other INF came with Windows)."""
    by_name = {p["name"]: p for p in packages}
    for d in found:
        drv = d.get("driver")
        if not drv:
            continue
        inf = (drv.get("inf") or "").lower()
        if inf.startswith("oem"):
            package = by_name.get(inf) or {}
            drv["signer"], drv["signing"] = package.get("signer"), package.get("signing")
        elif inf:
            drv["signer"], drv["signing"] = driver_store.WINDOWS_SIGNER, "windows"
        else:
            drv["signer"] = drv["signing"] = None


def key_drivers(devices: list[dict], fw: dict, today: date | None = None) -> list[dict]:
    out = []
    order = ["Graphics", "Chipset", "Sound", "Network", "Bluetooth", "Storage", "USB"]
    for d in devices:
        part = role(d)
        if part is None:
            continue
        drv = d["driver"]
        age = _years(drv["date"], today)
        windows_own = _is_microsoft(drv["provider"])
        if part == "Graphics" and (d["service"].lower() == "basicdisplay" or drv["inf"] == "display.inf"):
            status = "warn"
        elif windows_own:
            status = "windows"
        elif age is None:
            status = "info"
        else:
            status = "good" if age < 3 else "info" if age < 6 else "old"
        out.append({"part": part, "device": d["name"], "id": d["id"], "provider": drv["provider"],
                    "version": drv["version"], "date": drv["date"], "age_years": round(age, 1) if age else age,
                    "status": status, "source": source_for(d, part, fw), "signer": drv.get("signer"),
                    "signing": drv.get("signing")})
    out.sort(key=lambda k: (order.index(k["part"]), k["device"]))
    return out


# ------------------------------------------------------------ findings from the scan

def _device_findings(devices: list[dict]) -> list[dict]:
    found = []
    for d in devices:
        code = d["problem"]
        if not d["present"] or code in devices_mod.NOT_PROBLEMS:
            continue
        what, fix = devices_mod.problem_text(code)
        if code == 22:
            found.append(_check(f"disabled:{d['id']}", d["name"], "info", tx("{problem} (Code {code})", problem=what, code=22),
                                tx("Turned off in Device Manager, by you or an app. It uses no power or resources "
                                   "while it's off."), fix, LINKS["ms_codes"][1],
                                [_button("enable_device", tx("Turn it on"), d["id"])]))
            continue
        buttons = []
        if code in _RESTARTABLE:
            buttons.append(_button("restart_device", tx("Restart the device"), d["id"]))
        if code in _SCANNABLE:
            buttons.append(_button("scan_hardware", tx("Scan for hardware changes")))
        if code in _NEEDS_DRIVER:
            buttons.append(_button("check_windows_update", tx("Look for a driver on Windows Update")))
        found.append(_check(f"problem:{d['id']}", d["name"], "warn", tx("{problem} (Code {code})", problem=what, code=code),
                            _device_line(d), fix, LINKS["ms_codes"][1], buttons))
    return found


def _device_line(d: dict) -> str:
    """"Net by Realtek, driver 10.68 from Realtek." (its class as Windows names it)"""
    kind, drv = d["class"] or tx("Device"), d["driver"]
    if d["manufacturer"]:
        return (tx("{kind} by {maker}, driver {version} from {provider}.", kind=kind, maker=d["manufacturer"],
                   version=drv["version"], provider=drv["provider"]) if drv
                else tx("{kind} by {maker}, no driver.", kind=kind, maker=d["manufacturer"]))
    return (tx("{kind}, driver {version} from {provider}.", kind=kind, version=drv["version"], provider=drv["provider"])
            if drv else tx("{kind}, no driver.", kind=kind))


def _graphics_findings(keys: list[dict]) -> list[dict]:
    found = []
    for k in (k for k in keys if k["part"] == "Graphics" and k["status"] == "warn"):
        label, url = k["source"]["label"], k["source"]["url"]
        found.append(_check(
            f"basic_display:{k['id']}", tx("Graphics driver missing"), "warn",
            tx("{device} runs on Windows' basic display driver.", device=k["device"]),
            tx("Without its own driver the card can't run games properly: no 3D speed, no G-SYNC/FreeSync, often a "
               "wrong resolution or refresh rate."), tx("Install the driver from {source}.", source=label), url,
            [_button("check_windows_update", tx("Look for a driver on Windows Update"))]))
    return found


def _old_driver_findings(keys: list[dict], fw: dict) -> list[dict]:
    old = [k for k in keys if k["status"] == "old" and k["part"] != "Graphics"]
    if not old:
        return []
    items = [tx("{part}: {device} — {provider} {version}, from {date}", part=k["part"], device=k["device"],
                provider=k["provider"], version=k["version"], date=_month_year(k["date"])) for k in old]
    support = fw.get("support") if fw else None
    where = (tx("your board's page at {site} (search for {model})", site=support["label"], model=fw["board"]["model"])
             if support and fw.get("board", {}).get("model") else tx("the maker's website"))
    return [_check(
        "old_drivers", tx("Old drivers"), "tip",
        txn(len(old), "{n} of your main drivers is more than 6 years old.", "{n} of your main drivers are more than 6 years old."),
        txn(len(old), "An old driver isn't wrong by itself, and Windows Update didn't offer a newer one for it. Newer "
            "versions usually fix bugs, add features and support the latest Windows; for sound and network chips on the "
            "motherboard, the board maker's package also brings its control panel.",
            "An old driver isn't wrong by itself, and Windows Update didn't offer a newer one for them. Newer versions "
            "usually fix bugs, add features and support the latest Windows; for sound and network chips on the "
            "motherboard, the board maker's package also brings its control panel."),
        tx("Check {where} for newer versions, and install the one listed for your exact model.", where=where),
        support["url"] if support else LINKS["ms_drivers"][1], items=items)]


def _software_findings(software: list[dict]) -> list[dict]:
    found = []
    for s in software:
        if s["risk"]:
            risk = {"risk": s["risk"]["title"], "file": s["file"]}
            found.append(_check(
                f"risk:{s['name']}", tx("{risk} driver", risk=s["risk"]["title"]), "warn",
                tx("{risk} ({file}) is installed and running.", **risk) if s["running"]
                else tx("{risk} ({file}) is installed.", **risk),
                s["risk"]["why"], s["risk"]["fix"], s["risk"]["source"]))
    leftovers = [s for s in software if s["leftover"]]
    if leftovers:
        found.append(_check(
            "leftover_drivers", tx("Drivers left behind"), "info",
            txn(len(leftovers), "{n} driver is set to start with Windows, but its file is gone.",
                "{n} drivers are set to start with Windows, but their files are gone."),
            tx("Usually an app was uninstalled and left its driver entry behind. Windows tries to load it at every "
               "start and logs that it couldn't; nothing else happens."),
            tx("Reinstalling and then uninstalling the app that installed it usually removes the entry."),
            items=[f"{s['name']} ({s['file']})" for s in leftovers]))
    return found


def _event_findings(events: list[dict], owners: dict[str, bool], devices: list[dict]) -> list[dict]:
    found = []
    third_party = []
    for e in events:
        seen = {"count": e["count"], "last": e["last"][:10]}
        if e["kind"] == "boot_driver_not_loaded":
            names = [n for n in e.get("names", []) if not owners.get(n.lower(), False)]
            if names:
                third_party.append(tx("{driver} didn't load at start ({count}×, last {last})", driver=", ".join(names), **seen))
        elif e["kind"] in ("failed_to_load", "service_not_started") and not owners.get(e["subject"].lower(), False):
            third_party.append(tx("{driver} failed to load ({count}×, last {last})", driver=e["subject"], **seen)
                               if e["kind"] == "failed_to_load" else
                               tx("{driver} didn't start ({count}×, last {last})", driver=e["subject"], **seen))
    if third_party:
        found.append(_check(
            "failed_drivers", tx("Drivers that didn't start"), "warn",
            txn(len(third_party), "In the last {days} days, {n} driver from other makers than Microsoft didn't start.",
                "In the last {days} days, {n} drivers from other makers than Microsoft didn't start.", days=_EVENT_DAYS),
            tx("A device or app that needs it may not work, or work without its extras."),
            tx("Update or reinstall the app or device driver it belongs to; if you uninstalled that app, the entry is "
               "left over and harmless."), items=third_party))
    by_id = {d["id"].lower(): d for d in devices}
    recovered = [e for e in events if e["kind"] == "device_problem"
                 and (by_id.get(e["subject"].lower()) or {}).get("problem", 1) == 0]
    if recovered:
        found.append(_check(
            "device_hiccups", tx("Devices that had trouble starting"), "info",
            txn(len(recovered), "{n} device had a problem starting in the last {days} days and works now.",
                "{n} devices had a problem starting in the last {days} days and work now.", days=_EVENT_DAYS),
            tx("Common after a driver update or with a device plugged in while the PC starts; if it keeps coming back, "
               "update that device's driver."),
            items=[tx("{device} (Code {code}, {count}×)", device=(by_id.get(e["subject"].lower()) or {}).get("name", e["subject"]),
                      code=e.get("problem", "?"), count=e["count"]) for e in recovered]))
    return found


# How a device that stopped responding is listed: (singular, plural) for how many times it happened
_RESET_TEXT = {
    "network_reset": lambda n, **v: txn(n, "{subject}: reset {n} time (its driver found the adapter not responding)",
                                        "{subject}: reset {n} times (its driver found the adapter not responding)", **v),
    "storage_reset": lambda n, **v: txn(n, "Storage controller ({subject}{port}): reset {n} time",
                                        "Storage controller ({subject}{port}): reset {n} times", **v),
    "disk_retry": lambda n, **v: txn(n, "{subject}: reads or writes had to be retried {n} time",
                                     "{subject}: reads or writes had to be retried {n} times", **v),
    "disk_bad_block": lambda n, **v: txn(n, "{subject}: reported a bad block {n} time",
                                         "{subject}: reported a bad block {n} times", **v),
    "gpu_error": lambda n, **v: txn(n, "Graphics driver ({subject}): {n} error", "Graphics driver ({subject}): {n} errors", **v),
}


def _span(first: str, last: str) -> str:
    a, b = datetime.fromisoformat(first).astimezone(), datetime.fromisoformat(last).astimezone()
    if a.date() == b.date():
        return (tx("{day}, {start}–{end}", day=_day(a), start=f"{a:%H:%M}", end=f"{b:%H:%M}") if a != b
                else tx("{day}, {time}", day=_day(a), time=f"{a:%H:%M}"))
    return tx("{start} – {end}", start=_day(a), end=_day(b))


def _overlapping(groups: list[dict]) -> bool:
    """Did two different devices have trouble during the same hours?"""
    spans = [(datetime.fromisoformat(g["first"]), datetime.fromisoformat(g["last"])) for g in groups]
    return any(a[0] <= b[1] and b[0] <= a[1] for i, a in enumerate(spans) for b in spans[i + 1:])


def _reset_findings(events: list[dict], keys: list[dict], fw: dict, boot: datetime | None,
                    disks: dict[int, str] | None = None, now: datetime | None = None) -> list[dict]:
    """Devices that stopped responding and had to be reset: one finding with each of them, when,
    and what usually causes it (several at once points to something they share)."""
    resets = sorted((e for e in events if e["kind"] in driver_events.RESET_KINDS), key=lambda e: -e["count"])
    if not resets:
        return []
    if disks is None and any(e["kind"].startswith("disk") for e in resets):
        try:
            disks = hardware.physical_disks()
        except Exception:
            disks = {}
    items = []
    for e in resets:
        subject = e["subject"]
        if e.get("disk") is not None and (disks or {}).get(e["disk"]):
            subject = i18n.joined([subject, f"({disks[e['disk']]})"])
        port = ", " + e["port"].rsplit("\\", 1)[-1] if e.get("port") else ""
        items.append(tx("{event} — {when}", event=_RESET_TEXT[e["kind"]](e["count"], subject=subject, port=port),
                        when=_span(e["first"], e["last"])))
    together = len(resets) > 1 and _overlapping(resets)
    stopped = boot is not None and all(datetime.fromisoformat(e["last"]) < boot for e in resets)
    recent = max(datetime.fromisoformat(e["last"]) for e in resets) > (now or datetime.now(timezone.utc)) - \
        timedelta(days=7)
    steps = []
    network = {e["subject"] for e in resets if e["kind"] == "network_reset"}
    for k in keys:
        if k["part"] == "Network" and k["device"] in network and k["status"] in ("old", "info"):
            steps.append(tx("Update the {device} driver: yours is from {date} ({source}).", device=k["device"],
                            date=_month_year(k["date"]), source=k["source"]["label"]))
    if any(e["kind"] in ("storage_reset", "disk_retry", "disk_bad_block") for e in resets):
        steps.append(tx("Check the disk's SATA data and power cables, and its health (Optimize > Hardware check > Drive "
                        "health); back up what matters on it."))
    chipset = amd_chipset_link(fw.get("board", {}).get("model", "")) if fw else LINKS["amd"]
    if together:
        steps.append(tx("Install the latest chipset drivers ({drivers}) and look for a newer BIOS on the board maker's "
                        "page.", drivers=chipset[0]))
    steps.append(tx("If it comes back, note the time and what was running: a part that fails under load or heat shows a "
                    "pattern.") if stopped else tx("Restart the PC; if it continues, the part is likely failing."))
    n = len({(e["kind"], e["subject"]) for e in resets})
    if together and stopped:
        summary = txn(n, "{n} device stopped responding and had to be reset during the same hours (it stopped after the "
                      "last restart).", "{n} devices stopped responding and had to be reset during the same hours (it "
                      "stopped after the last restart).")
    elif together:
        summary = txn(n, "{n} device stopped responding and had to be reset during the same hours.",
                      "{n} devices stopped responding and had to be reset during the same hours.")
    elif stopped:
        summary = txn(n, "{n} device stopped responding and had to be reset (it stopped after the last restart).",
                      "{n} devices stopped responding and had to be reset (it stopped after the last restart).")
    else:
        summary = txn(n, "{n} device stopped responding and had to be reset.",
                      "{n} devices stopped responding and had to be reset.")
    return [_check(
        "device_resets", tx("Devices that stopped responding"), "warn" if recent else "info", summary,
        tx("When several devices fail during the same hours, the cause is usually something they share rather than each "
           "driver: the chipset driver or BIOS, the power supply or PCIe power saving, heat, or one failing part "
           "disturbing the others.") if together else
        tx("A device that keeps resetting usually has an old or broken driver, a loose cable, or is wearing out."),
        i18n.joined(steps), chipset[1] if together else None, items=items)]


def _protection_findings(protection: dict, events: list[dict]) -> list[dict]:
    found = []
    if not protection:
        return found
    flagged = [e for e in events if e["kind"] in ("would_be_blocked", "blocked")
               and e.get("policy") in (driver_events.DRIVER_POLICY_ENFORCED, driver_events.DRIVER_POLICY_EVALUATION)]
    mode = protection["driver_policy"]
    if flagged:
        blocked = any(e["kind"] == "blocked" for e in flagged)
        found.append(_check(
            "driver_policy", tx("Windows Driver Policy"), "warn",
            txn(len(flagged), "{n} driver on this PC was blocked by Windows' new driver policy.",
                "{n} drivers on this PC were blocked by Windows' new driver policy.") if blocked else
            txn(len(flagged), "{n} driver on this PC would be blocked by Windows' new driver policy.",
                "{n} drivers on this PC would be blocked by Windows' new driver policy."),
            i18n.joined([tx("Since the April 2026 update Windows no longer trusts drivers signed only through the old "
                            "cross-signing program."),
                         tx("Windows enforces it here: what needs these drivers doesn't work.") if blocked else
                         tx("This PC is still being evaluated, so they load for now, and each load restarts the "
                            "evaluation.")]),
            tx("Get versions signed through Microsoft's hardware program: Windows Update's optional driver updates, "
               "or the maker's website."), LINKS["ms_policy"][1], [_button("check_windows_update", tx("Check Windows Update"))],
            items=[tx("{driver} ({count}×, last {last})", driver=e["subject"], count=e["count"], last=e["last"][:10])
                   for e in flagged]))
    elif mode != "off":
        found.append(_check(
            "driver_policy", tx("Windows Driver Policy"), "good",
            tx("No driver on this PC is blocked or flagged by Windows' driver policy."),
            i18n.joined([tx("Since the April 2026 update Windows only trusts drivers signed through Microsoft's hardware "
                            "program."),
                         tx("It enforces that here.") if mode == "enforced" else
                         tx("This PC is in its evaluation phase: Windows logs drivers it would block, and found none.")]),
            link=LINKS["ms_policy"][1]))
    if not protection["blocklist"] and not protection["memory_integrity"]:
        found.append(_check(
            "blocklist", tx("Vulnerable driver blocklist"), "warn",
            tx("Microsoft's vulnerable driver blocklist is turned off."),
            tx("It stops drivers with known security holes from loading, the ones malware brings along to get into "
               "Windows' kernel. It's on by default since Windows 11 22H2."),
            tx("Windows Security > Device security > Core isolation details > Microsoft Vulnerable Driver Blocklist: On "
               "(then restart)."), LINKS["ms_blocklist"][1], [_button("open_link", tx("Open Windows Security"),
                                                                     LINKS["windows_security"][1])]))
    for flag, title, text in (("test_signing", tx("Test signing is on"), tx("Windows loads drivers that aren't signed for "
                               "release (test signing), which normally only driver developers turn on.")),
                              ("integrity_checks_off", tx("Driver signature checks are off"),
                               tx("Windows skips the signature check of drivers at start."))):
        if protection[flag]:
            found.append(_check(
                flag, title, "warn", text,
                tx("Any unsigned driver can load, including malware's, and anti-cheat software refuses to run."),
                tx("If you didn't turn it on for a reason: in an administrator terminal, run {command}, then restart.",
                   command="bcdedit /set testsigning off" if flag == "test_signing" else
                   "bcdedit /deletevalue nointegritychecks")))
    if not protection["windows_update_drivers"]:
        found.append(_check(
            "wu_drivers_excluded", tx("Windows Update doesn't deliver drivers"), "info",
            tx("A policy on this PC (Do not include drivers with Windows Updates) keeps drivers out of Windows Update."),
            tx("Some people turn it on to keep their own graphics driver. Windows then also skips fixes for other "
               "devices, and OptiMind's Windows Update check finds none."),
            tx("To change it: Group Policy > Computer Configuration > Administrative Templates > Windows Components > "
               "Windows Update > Manage updates offered from Windows Update > Do not include drivers with Windows "
               "Updates.")))
    return found


def _store_findings(packages: list[dict]) -> list[dict]:
    removable = [p for p in packages if p["removable"]]
    if not removable:
        return []
    size = sum(p["size"] for p in removable)
    return [_check(
        "old_packages", tx("Old driver versions"), "tip" if size >= 2 ** 30 else "info",
        txn(len(removable), "{n} old driver version takes {mb} MB in the driver store.",
            "{n} old driver versions take {mb} MB in the driver store.", mb=i18n.num(size / 2 ** 20)),
        tx("Windows keeps every driver version it installs. These were replaced by newer versions, no device uses "
           "them, and they aren't the version Windows would roll back to (that one stays)."),
        tx("Remove them below (Driver store); Windows refuses any that a device still needs."),
        actions=[_button("remove_old", txn(len(removable), "Remove {n} old version", "Remove {n} old versions"))],
        items=[tx("{name} {version} ({provider}, {mb} MB)", name=p["original"], version=p["version"], provider=p["provider"],
                  mb=i18n.num(p["size"] / 2 ** 20)) for p in removable])]


def _signature_findings(found: list[dict]) -> list[dict]:
    """Who signed the drivers from other makers that connected devices use (one line per driver,
    with its devices): Windows wants them signed through Microsoft's hardware program."""
    drivers_used: dict[str, dict] = {}
    for d in found:
        drv = d["driver"]
        if d["present"] and drv and drv.get("signing") in ("microsoft", "maker", "unsigned"):
            drivers_used.setdefault(drv["inf"], {"driver": drv, "devices": []})["devices"].append(d["name"])
    if not drivers_used:
        return []
    unsigned = [e for e in drivers_used.values() if e["driver"]["signing"] == "unsigned"]
    makers = [e for e in drivers_used.values() if e["driver"]["signing"] == "maker"]
    if not unsigned and not makers:
        return [_check(
            "signatures", tx("Driver signatures"), "good",
            txn(len(drivers_used), "The one driver from other makers that your devices use is signed through Microsoft's "
                "hardware program.", "All {n} drivers from other makers that your devices use are signed through "
                "Microsoft's hardware program."),
            tx("The maker sent each one to Microsoft, which checks it before signing it, and Windows checks the signature "
               "whenever it installs or loads a driver. Windows' own drivers come with Windows."))]

    def line(e: dict, how: str) -> str:
        drv, names = e["driver"], e["devices"]
        who = tx("{devices} and {n} more", devices=", ".join(names[:2]), n=len(names) - 2) if len(names) > 2 \
            else ", ".join(names)
        return tx("{devices}: {provider} {version}, {how}", devices=who, provider=drv["provider"] or tx("maker unknown"),
                  version=drv["version"], how=how)

    n = len(unsigned) + len(makers)
    return [_check(
        "signatures", tx("Driver signatures"), "tip" if unsigned else "info",
        txn(n, "{n} driver your devices use isn't signed through Microsoft's hardware program.",
            "{n} drivers your devices use aren't signed through Microsoft's hardware program."),
        tx("Windows wants drivers from other makers signed through Microsoft's hardware program, which checks them before "
           "signing. A driver signed only with its maker's own certificate is the older way: Windows' driver policy of "
           "2026 stops trusting the kernel drivers among them. An unsigned driver can't be checked for changes made after "
           "its maker built it."),
        tx("Look for a newer version on Windows Update or the maker's website: drivers there are signed through Microsoft. "
           "If the device works well, there's no hurry."), LINKS["ms_policy"][1],
        [_button("check_windows_update", tx("Check Windows Update"))],
        items=[line(e, tx("not signed")) for e in unsigned]
        + [line(e, tx("signed only by {signer}", signer=e["driver"]["signer"])) for e in makers])]


def _firmware_findings(fw: dict, today: date | None = None) -> list[dict]:
    if not fw or not fw.get("bios", {}).get("version"):
        return []
    bios, board, support = fw["bios"], fw["board"], fw.get("support")
    age = _years(bios.get("date"), today)
    months = round(age * 12) if age else 0
    ago = (txn(months, "{n} month ago", "{n} months ago") if 2 <= months < 24
           else txn(int(age), "{n} year ago", "{n} years ago") if months >= 24 else None)
    bios_from = {"pc": fw["system"]["model"] or board["model"] or tx("your PC"), "version": bios["version"],
                 "date": _month_year(bios.get("date"))}
    return [_check(
        "bios", "BIOS", "info",
        tx("{pc}: BIOS {version} from {date} ({ago}).", ago=ago, **bios_from) if ago
        else tx("{pc}: BIOS {version} from {date}.", **bios_from),
        tx("BIOS updates add processor support and fix stability and security issues (AMD and Intel ship many of "
           "their fixes this way). A failed update can leave the board unable to start, so the maker's own tool and "
           "steps matter."),
        tx("Look up your model on the maker's page; update only if the release notes fix something you need or it's "
           "a security fix, with the PC on reliable power. OptiMind never updates a BIOS."),
        support["url"] if support else None)]


def _backup_findings(backups: list[dict]) -> list[dict]:
    mine = [b for b in backups if b["this_pc"]]
    if mine:
        return [_check("backup", tx("Driver backup"), "good",
                       txn(mine[0]["packages"], "Last driver backup: {date} ({n} package), in {folder}.",
                           "Last driver backup: {date} ({n} packages), in {folder}.", date=mine[0]["created"][:10],
                           folder=mine[0]["path"]))]
    return [_check(
        "backup", tx("Driver backup"), "tip", tx("There's no backup of your drivers yet."),
        tx("A copy of every driver package from other makers, on another drive than Windows, survives a clean "
           "reinstall: you install them back in one step, without searching the web for each one."),
        tx("Back up your drivers below (Back up and restore)."), actions=[_button("backup", tx("Back up drivers"))])]


# ------------------------------------------------------------ findings from the update checks

def update_items(wu: dict | None, devices: list[dict], hardware_ids: dict[str, list[str]]) -> list[dict]:
    """Windows Update's offers next to the driver each would replace: "newer", "same", "older"
    (never installed by OptiMind), "unknown" (no matching device found) or "firmware" (the
    PC's BIOS or a device's own firmware: never installed by OptiMind either)."""
    if not wu:
        return []
    by_id = {d["id"]: d for d in devices if d["present"]}
    out = []
    for u in wu.get("updates") or []:
        target = u["hardware_id"]
        device = next((by_id[i] for i, ids in hardware_ids.items() if i in by_id and target and target in ids), None)
        drv = (device or {}).get("driver") or {}
        verdict = "unknown"
        if driver_updates.is_firmware(u):
            verdict = "firmware"
        elif device and drv:
            mine = (drv.get("date") or "", driver_store.version_key(drv.get("version", "")))
            theirs = (u.get("date") or "", driver_store.version_key(u.get("version", "")))
            verdict = "newer" if theirs > mine else "same" if theirs == mine else "older"
        out.append({**u, "device": device["name"] if device else None, "installed_version": drv.get("version"),
                    "installed_date": drv.get("date"), "verdict": verdict})
    return out


def _update_findings(items: list[dict], wu: dict | None, nvidia: dict | None, keys: list[dict]) -> list[dict]:
    found = []
    newer = [i for i in items if i["verdict"] in ("newer", "unknown")]
    if newer:
        found.append(_check(
            "windows_update", tx("Driver updates on Windows Update"), "tip",
            txn(len(newer), "Windows Update offers {n} driver update.", "Windows Update offers {n} driver updates."),
            tx("Drivers on Windows Update are signed through Microsoft's hardware program and matched to your devices."),
            tx("Install them under Updates below (OptiMind creates a restore point first)."),
            items=[i["title"] for i in newer]))
    firmware = [i for i in items if i["verdict"] == "firmware"]
    if firmware:
        found.append(_check(
            "firmware_offer", tx("Firmware on Windows Update"), "info",
            txn(len(firmware), "Windows Update offers a firmware update: OptiMind doesn't install firmware.",
                "Windows Update offers {n} firmware updates: OptiMind doesn't install firmware."),
            tx("Firmware (the PC's BIOS/UEFI, or a device's own) is written into the hardware at the next restart. One that "
               "fails can leave the PC or the device unable to start, so OptiMind never installs it."),
            tx("If you want it, read the maker's notes for your model first, then install it from Windows Update with the "
               "PC on reliable power (a laptop on its charger)."), LINKS["windows_update"][1],
            items=[i["title"] for i in firmware]))
    gpu = next((k for k in keys if k["part"] == "Graphics" and k["status"] != "warn" and k["source"]["url"] ==
                LINKS["nvidia"][1]), None)
    if gpu and nvidia:
        title = tx("Graphics driver")
        if nvidia.get("error"):
            found.append(_check("nvidia", title, "info",
                                tx("{gpu}: driver {version}. Couldn't ask NVIDIA about a newer one ({error}).",
                                   gpu=gpu["device"], version=nvidia.get("installed") or gpu["version"], error=nvidia["error"]),
                                link=LINKS["nvidia"][1]))
        elif nvidia.get("newer"):
            found.append(_check(
                "nvidia", title, "tip",
                tx("NVIDIA {latest} is out ({date}); you have {installed}.", latest=nvidia["latest"],
                   date=_month_year(nvidia.get("date")), installed=nvidia.get("installed")),
                tx("{driver} for the {gpu}, from NVIDIA's site. New drivers bring fixes and speed-ups for recent games; the "
                   "release notes list them.", driver=nvidia.get("name") or tx("Game Ready Driver"), gpu=gpu["device"]),
                tx("Install it from here (NVIDIA's own installer, checked to be signed by NVIDIA) or with NVIDIA App. "
                   "Close your games first."), nvidia.get("details_url") or LINKS["nvidia"][1],
                [_button("install_nvidia", tx("Install NVIDIA {version}", version=nvidia["latest"]))]))
        elif nvidia.get("latest"):
            found.append(_check(
                "nvidia", title, "good",
                tx("{gpu}: NVIDIA {installed}, the newest Game Ready driver (released {date}).", gpu=gpu["device"],
                   installed=nvidia.get("installed"), date=_month_year(nvidia.get("date"))),
                tx("Checked on NVIDIA's site."), link=nvidia.get("details_url") or LINKS["nvidia"][1]))
    return found


# ------------------------------------------------------------ the scan

def _owners(services: list[dict]) -> dict[str, bool]:
    """Driver service name -> is it Windows' own (made by Microsoft)?"""
    out = {}
    for s in services:
        details = software_drivers.file_details(s["file"])
        out[s["name"].lower()] = software_drivers.is_microsoft(details)
    return out


def _scan(refresh: bool = False) -> tuple[dict, dict[str, list[str]]]:
    started = time.perf_counter()
    apps = threading.Thread(target=maker_tools.start_apps, args=(refresh,), name="optimind-start-apps", daemon=True)
    apps.start()  # the Start menu (about a second) is read alongside the rest
    found_devices = devices_mod.read_devices()
    hardware_ids = {d["id"]: d.pop("ids", []) for d in found_devices}
    store = driver_store.packages(found_devices)
    packages = store["packages"]
    add_signers(found_devices, packages)
    services = software_drivers.driver_services()
    device_services = {d["service"] for d in found_devices if d["service"]}
    software = software_drivers.pick(services, device_services, {p["folder"] for p in packages if p["folder"]},
                                     software_drivers.file_details)
    names = {s["display_name"].lower(): s["name"] for s in services}
    try:
        events = driver_events.read_events(_EVENT_DAYS, names)
    except Exception:
        logger.debug("Couldn't read the driver events", exc_info=True)
        events = []
    load_kinds = ("failed_to_load", "service_not_started", "boot_driver_not_loaded")  # where the driver's maker matters
    event_names = {n.lower() for e in events if e["kind"] in load_kinds for n in (e.get("names") or [e["subject"]])}
    owners = _owners([s for s in services if s["name"].lower() in event_names])
    for e in events:
        e["windows_own"] = e["kind"] in load_kinds and all(owners.get(n.lower(), False)
                                                            for n in (e.get("names") or [e["subject"]]))
    protection = driver_events.protection()
    fw = firmware()
    keys = key_drivers(found_devices, fw)
    boot = datetime.fromtimestamp(psutil.boot_time(), timezone.utc)
    findings = (_device_findings(found_devices) + _graphics_findings(keys) + _software_findings(software)
                + _reset_findings(events, keys, fw, boot) + _event_findings(events, owners, found_devices)
                + _protection_findings(protection, events) + _old_driver_findings(keys, fw)
                + _store_findings(packages) + _signature_findings(found_devices) + _firmware_findings(fw))
    apps.join(60)
    tools = maker_tools.tools(found_devices, fw, maker_tools.start_apps())
    connected = [d for d in found_devices if d["present"]]
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scan_ms": round((time.perf_counter() - started) * 1000),
        "summary": {
            "devices": len(connected), "not_connected": len(found_devices) - len(connected),
            "problems": sum(1 for d in connected if d["problem"] not in devices_mod.NOT_PROBLEMS | {22}),
            "disabled": sum(1 for d in connected if d["problem"] == 22),
            "packages": len(packages), "store_bytes": sum(p["size"] for p in packages),
            "removable": sum(1 for p in packages if p["removable"]),
            "removable_bytes": sum(p["size"] for p in packages if p["removable"]),
            "software": len(software), "events": sum(e["count"] for e in events if not e["windows_own"]),
        },
        "findings": findings, "key_drivers": keys, "firmware": fw, "devices": found_devices,
        "packages": packages, "packages_source": store["source"], "software": software, "events": events,
        "protection": protection, "backup_drives": driver_actions.backup_drives(sum(p["size"] for p in packages)),
        "tools": tools,
    }
    return report, hardware_ids


def _refresh_updates(report: dict) -> None:
    """Windows Update's own list (from its last check) every few hours and NVIDIA's newest
    driver every 12 hours, on a thread of their own: the page never waits for them."""
    if driver_jobs.busy() or not _background.acquire(blocking=False):
        return
    gpu = next((d for d in report["devices"] if d["present"] and d["class"] == "Display" and d["driver"]
                and _vendor_of(d) == "nvidia"), None)

    def run() -> None:
        try:
            wu = driver_updates.last_results()["windows_update"] or {}
            if time.time() - _timestamp(wu.get("checked_at")) > _WU_CACHE_SECONDS:
                driver_updates.search_windows_update(online=False)
            if gpu:
                driver_updates.check_nvidia(gpu["name"], gpu["driver"]["version"])
                driver_updates.tidy_downloads(driver_updates.nvidia_version(gpu["driver"]["version"]))
        except Exception:
            logger.debug("Background driver update check failed", exc_info=True)
        finally:
            _background.release()

    threading.Thread(target=run, name="optimind-driver-updates", daemon=True).start()


def _timestamp(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _with_updates(report: dict, hardware_ids: dict[str, list[str]], backups: list[dict] | None) -> dict:
    """The scan plus what the update checks know now (they finish on their own time) and the
    driver backups (None: not looked for, as on Home, where looking could wake a hard disk)."""
    results = driver_updates.last_results()
    wu, nvidia = results["windows_update"], results["nvidia"]
    items = update_items(wu, report["devices"], hardware_ids)
    findings = (report["findings"] + _update_findings(items, wu, nvidia, report["key_drivers"])
                + (_backup_findings(backups) if backups is not None else []))
    findings.sort(key=lambda c: _ORDER.get(c["status"], 4))
    return {
        **report, "findings": findings, "elevated": is_elevated(), "dry_run": settings.optimind_dry_run,
        "updates": {"windows_update": {**(wu or {}), "updates": items} if wu else None, "nvidia": nvidia},
        "backups": backups or [], "job": driver_jobs.status(),
    }


def _scanned(refresh: bool = False) -> tuple[dict, dict[str, list[str]]]:
    global _cache
    with _lock:
        if refresh or _cache is None or time.monotonic() - _cache[0] >= _CACHE_SECONDS:
            scanned, hardware_ids = _scan(refresh)
            _cache = (time.monotonic(), scanned, hardware_ids)
        _, scanned, hardware_ids = _cache
    _refresh_updates(scanned)
    return scanned, hardware_ids


def report(refresh: bool = False) -> dict:
    """Everything the Drivers page shows, worst finding first. The backups on the fixed drives
    are looked for the first time and when you check again."""
    if not IS_WINDOWS:
        return {"checked_at": None, "findings": [], "devices": [], "key_drivers": [], "packages": [], "software": [],
                "events": [], "summary": {}, "updates": {}, "backups": [], "job": None, "tools": []}
    scanned, hardware_ids = _scanned(refresh)
    return _with_updates(scanned, hardware_ids, driver_actions.backups(refresh=refresh))


def summary() -> dict:
    """For Home: what needs attention, from the last scan; a scan older than half an hour is
    refreshed in the background (this never waits for one)."""
    hit = _cache
    if hit is None or time.monotonic() - hit[0] >= _SUMMARY_SECONDS:
        if not _lock.locked():
            threading.Thread(target=_quiet_scan, name="optimind-drivers-scan", daemon=True).start()
    if hit is None:
        return {"checked_at": None, "findings": []}
    full = _with_updates(hit[1], hit[2], None)
    return {"checked_at": full["checked_at"], "summary": full["summary"],
            "findings": [f for f in full["findings"] if f["status"] in ("warn", "tip")]}


def _quiet_scan() -> None:
    try:
        _scanned()
    except Exception:
        logger.debug("Background driver scan failed", exc_info=True)


def cached_findings() -> list[dict] | None:
    """The open findings of the last scan (for the AI chat); None before the first scan."""
    hit = _cache
    if hit is None:
        return None
    return [f for f in _with_updates(hit[1], hit[2], None)["findings"] if f["status"] in ("warn", "tip")]


def tools() -> list[dict]:
    """The makers' own apps the last scan found for this PC (the window opens only one of these)."""
    hit = _cache
    return hit[1].get("tools", []) if hit else []


def forget() -> None:
    """After a change (a driver installed, a device restarted): the next look scans again."""
    global _cache
    with _lock:
        _cache = None
