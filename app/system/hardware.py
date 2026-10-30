"""
Hardware check (read-only): how this PC's parts are connected and set up,
and what would make them faster, including the things OptiMind can't change
itself (BIOS settings, where games are installed, a security trade-off).

Every part talks to the CPU over a link of fixed width and speed: the
memory channels, the PCIe lanes of the graphics card and of the NVMe
drive, the chipset link to the SATA disks. A slow or narrow link caps what
the part can deliver whatever Windows does (docs/performance-model.md).
This check reads those links and settings from Windows (WMI, the PnP
device properties, the storage driver, D3DKMT) and explains each one.

Nothing here changes the system. Cached for 10 minutes.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psutil

from app import i18n
from app.agent import optimizer
from app.config import IS_WINDOWS
from app.i18n import pct, tx, txn
from app.system import monitor
from app.system.admin_rights import is_elevated

logger = logging.getLogger("optimind.hardware")

_CACHE_SECONDS = 600.0
_cache: tuple[float, dict] | None = None
_lock = threading.Lock()

_MICROSOFT_HVCI = ("https://learn.microsoft.com/en-us/windows/security/hardware-security/"
                   "enable-virtualization-based-protection-of-code-integrity")
_MICROSOFT_GAMING = "https://support.microsoft.com/windows/options-to-optimize-gaming-performance-in-windows-11"
_GB = 1024 ** 3
_MEMORY_TYPES = {24: "DDR3", 26: "DDR4", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}
_PCIE_GEN = {1: "1.0", 2: "2.0", 3: "3.0", 4: "4.0", 5: "5.0"}
_LANE_GBPS = {1: 0.25, 2: 0.5, 3: 0.985, 4: 1.969, 5: 3.938}  # usable GB/s per lane and direction


def _check(id: str, title: str, status: str, summary: str, detail: str = "", action: str = "",
           tweak: str | None = None, link: str | None = None) -> dict:
    """status: "good", "tip" (worth doing), "warn" (something's wrong) or "info"."""
    return {"id": id, "title": title, "status": status, "summary": summary, "detail": detail,
            "action": action, "tweak": tweak, "link": link}


def _gb(value: float) -> str:
    return tx("{gb} GB", gb=i18n.num(value, ",.0f" if value >= 10 else ".1f"))


# A drive's kind, as _drive_kind() names it
_KINDS = {"NVMe SSD": tx("NVMe SSD"), "SSD": tx("SSD"), "hard disk": tx("hard disk"), "USB drive": tx("USB drive"),
          "drive": tx("drive")}


def _kind(drive: dict) -> str:
    return _KINDS.get(drive["kind"], drive["kind"])


def _wmi(query: str, namespace: str = r"root\cimv2") -> list:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()  # COM is per thread; the API runs each request on a worker thread
    service = win32com.client.Dispatch("WbemScripting.SWbemLocator").ConnectServer(".", namespace)
    return list(service.ExecQuery(query))


def _reg(path: str, name: str, hive=None):
    import winreg

    try:
        with winreg.OpenKey(hive if hive is not None else winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _cpu_name() -> str:
    return str(_reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString") or "").strip()


def display_adapters() -> list[str]:
    """The graphics adapters in this PC now, by name, as Device Manager lists them (not
    drivers of cards that were removed)."""
    from app.system import devices

    try:
        return [d["name"] for d in devices.read_devices() if d.get("present") and d.get("class") == "Display"]
    except Exception:
        return []


_BOARD_MAKERS = (("asus", "asus"), ("micro-star", "msi"), ("msi", "msi"), ("gigabyte", "gigabyte"), ("asrock", "asrock"))
# Where the BIOS keeps two settings, by the board's maker (the menus differ a little between models)
_MEMORY_PROFILE_MENU = {
    "asus": tx("on ASUS boards: Ai Tweaker > Ai Overclock Tuner, D.O.C.P. on older AMD boards"),
    "msi": tx("on MSI boards: the XMP / A-XMP / EXPO switch on the BIOS's first screen"),
    "gigabyte": tx("on Gigabyte boards: Tweaker > Extreme Memory Profile (X.M.P.) or EXPO"),
    "asrock": tx("on ASRock boards: OC Tweaker, the XMP / EXPO profile setting"),
}
_PBO_MENU = {
    "asus": tx("on ASUS boards under Advanced > AMD Overclocking, or Ai Tweaker"),
    "msi": tx("on MSI boards under OC > Advanced CPU Configuration > AMD Overclocking"),
    "gigabyte": tx("on Gigabyte boards under Tweaker > Advanced CPU Settings"),
    "asrock": tx("on ASRock boards under Advanced > AMD Overclocking"),
}


def _board_maker() -> str:
    """The motherboard's maker: "asus", "msi", "gigabyte", "asrock", or "" for any other."""
    try:
        board = _wmi("SELECT Manufacturer FROM Win32_BaseBoard")
    except Exception:
        return ""
    maker = str(board[0].Manufacturer or "").lower() if board else ""
    return next((name for key, name in _BOARD_MAKERS if key in maker), "")


def _is_ryzen_apu(cpu: str) -> bool:
    """A Ryzen with built-in graphics (APU), e.g. "Ryzen 5 3400G"."""
    return bool(re.search(r"Ryzen\s+\d\s+(PRO\s+)?\d{4}GE?\b", cpu))


# ------------------------------------------------------------ CPU

def _cpu_check() -> dict:
    cpu = _cpu_name() or tx("Your processor")
    physical, logical = psutil.cpu_count(logical=False) or 0, psutil.cpu_count() or 0
    sets = optimizer.cpu_sets()
    hybrid = len({s.EfficiencyClass for s in sets}) > 1
    background = optimizer.background_cores()
    smt = logical > physical
    counts = {"cpu": cpu, "cores": txn(physical, "{n} core", "{n} cores"), "threads": txn(logical, "{n} thread", "{n} threads")}
    summary = (tx("{cpu}: {cores}, {threads}, with performance and efficiency cores", **counts) if hybrid
               else tx("{cpu}: {cores}, {threads}, all cores alike", **counts))
    if hybrid:
        detail = tx("Windows runs efficiency-mode (EcoQoS) work on the efficiency cores. Maximum focus mode keeps "
                    "background apps there while a game is in focus, leaving the performance cores to the game.")
    elif smt and background:
        lps = sorted(s.LogicalProcessorIndex for s in sets if s.Id in background)
        detail = tx("Each core runs two threads (SMT) that share its execution units and caches, so a busy background "
                    "thread slows the game thread on the same core even at a lower priority. Maximum focus mode keeps "
                    "background apps on the last core (logical processors {processors}) while a game is in focus, so "
                    "the game's cores aren't shared.", processors=", ".join(map(str, lps)))
    else:
        detail = tx("Focus mode gives the app in front Above normal priority and holds busy background apps back.")
    return _check("cpu", tx("Processor"), "info", summary, detail)


# ------------------------------------------------------------ memory

def _channel(locator: str, bank: str) -> str | None:
    """The memory channel letter from the slot labels ("DIMM_A2", "ChannelA-DIMM0", "A1_DIMM0")."""
    text = f"{locator or ''} {bank or ''}".upper()
    for pattern in (r"CHANNEL\s*([A-H])", r"DIMM[_\s-]?([A-H])\d", r"(?<![A-Z])([A-H])[0-9](?![0-9])"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _memory_checks() -> list[dict]:
    modules = _wmi("SELECT Capacity, Speed, ConfiguredClockSpeed, DeviceLocator, BankLabel, SMBIOSMemoryType "
                   "FROM Win32_PhysicalMemory")
    if not modules:
        return []
    checks = []
    kind = _MEMORY_TYPES.get(int(modules[0].SMBIOSMemoryType or 0)) or tx("memory")
    total = sum(int(m.Capacity or 0) for m in modules) / _GB
    rated = max(int(m.Speed or 0) for m in modules)
    running = min((int(m.ConfiguredClockSpeed) for m in modules if m.ConfiguredClockSpeed), default=0)
    ryzen = "ryzen" in _cpu_name().lower()
    fabric = tx("On Ryzen it also sets the speed of the Infinity Fabric, the link between the CPU cores, the memory "
                "and the graphics, so it matters even more.") if ryzen else None
    speed, title = {"kind": kind, "running": running, "rated": rated}, tx("Memory speed")
    if rated and running and running < rated * 0.97:
        checks.append(_check(
            "ram_speed", title, "tip",
            tx("Your {kind} runs at {running} MT/s, below the {rated} MT/s it's rated for.", **speed),
            i18n.joined([tx("Memory speed is how fast every app's data reaches the CPU."), fabric]),
            tx("Turn on the memory's XMP / EXPO profile in the BIOS ({where}), then check here again.",
               where=_MEMORY_PROFILE_MENU.get(_board_maker(), tx("usually in its overclocking or memory section; some "
                                                                 "boards call it D.O.C.P. or A-XMP")))))
    elif running:
        checks.append(_check(
            "ram_speed", title, "good",
            tx("Your {kind} runs at {running} MT/s, the top speed it reports to Windows.", **speed),
            i18n.joined([tx("If the box of your memory promises a faster speed (an XMP / EXPO profile), you can turn it on "
                            "in the BIOS."), fabric])))

    slots = [m.DeviceLocator or m.BankLabel or "?" for m in modules]
    channels = {_channel(m.DeviceLocator, m.BankLabel) for m in modules}
    layout = tx("{count} × {size} {kind} in slots {slots}", count=len(modules), size=_gb(int(modules[0].Capacity or 0) / _GB),
                kind=kind, slots=", ".join(slots))
    title = tx("Memory channels")
    if len(modules) == 1:
        checks.append(_check(
            "ram_channels", title, "warn", tx("{layout}: one memory channel in use.", layout=layout),
            tx("With a single stick the CPU reaches memory over one channel: half the bandwidth of two. Games and "
               "built-in graphics feel it most."),
            tx("Add a second, identical stick (in the slot your motherboard manual marks for dual channel).")))
    elif None not in channels and len(channels) == 1:
        checks.append(_check(
            "ram_channels", title, "warn", tx("{layout}: all on the same channel.", layout=layout),
            tx("Both sticks share one channel, so the second channel's bandwidth is unused."),
            tx("Move one stick to the other channel's slot (your motherboard manual shows which; usually A2 and B2).")))
    elif None not in channels:
        checks.append(_check(
            "ram_channels", title, "good",
            tx("{layout}: {n} channels in use (dual channel or more).", layout=layout, n=len(channels)),
            tx("The CPU reads from the sticks in parallel, doubling the memory bandwidth of a single stick.")))
    else:
        checks.append(_check("ram_channels", title, "info", tx("{layout}.", layout=layout),
                             tx("Windows doesn't say which channel each slot belongs to on this board.")))

    history = monitor.read_recent_history(900)  # about the last 30 minutes
    if len(history) >= 60:
        ram = sorted(h["ram"] for h in history)
        typical = ram[round((len(ram) - 1) * 0.75)]
        status = "tip" if typical >= 85 else "info" if typical >= 70 else "good"
        checks.append(_check(
            "ram_size", tx("Memory size"), status,
            tx("{size} installed; in the last half hour it was usually up to {pct} full.", size=_gb(total), pct=pct(typical)),
            i18n.joined([tx("Windows compresses the memory of idle apps before it writes anything to disk, and OptiMind "
                            "compresses idle background apps sooner."),
                         tx("When memory is this full most of the time, more RAM is the real fix.") if typical >= 85
                         else tx("That's enough room for what you run.")])))
    return checks


# ------------------------------------------------------------ PCIe links and interrupts

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


def _key(fmtid: str, pid: int) -> _DEVPROPKEY:
    return _DEVPROPKEY(_GUID.from_buffer_copy(uuid.UUID(fmtid).bytes_le), pid)


_DEVICE = "A45C254E-DF1C-4EFD-8020-67D146A850E0"  # DEVPKEY_Device_*: DeviceDesc 2, Class 9, Driver 11, FriendlyName 14
_PCI = "3AB22E31-8264-4B4E-9AF5-A8D2D8E33E62"  # DEVPKEY_PciDevice_*: link speed/width 9-12, interrupt support 14
_CR_SUCCESS, _DEVPROP_TYPE_UINT32, _DEVPROP_TYPE_STRING = 0, 0x7, 0x12


def _property(cfgmgr32, devinst: int, fmtid: str, pid: int):
    key, kind, size = _key(fmtid, pid), ctypes.c_ulong(), ctypes.c_ulong(0)
    cfgmgr32.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(kind), None, ctypes.byref(size), 0)
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if cfgmgr32.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key), ctypes.byref(kind), buffer, ctypes.byref(size),
                                         0) != _CR_SUCCESS:
        return None
    if kind.value == _DEVPROP_TYPE_STRING:
        return ctypes.wstring_at(buffer)
    if kind.value == _DEVPROP_TYPE_UINT32:
        return int.from_bytes(buffer.raw[:4], "little")
    return None


def _pci_devices() -> list[dict]:
    """Every PCI device present, with its link speed/width (current and maximum)."""
    cfgmgr32 = ctypes.WinDLL("cfgmgr32")
    flags = 0x1 | 0x100  # CM_GETIDLIST_FILTER_ENUMERATOR | CM_GETIDLIST_FILTER_PRESENT
    size = ctypes.c_ulong()
    if cfgmgr32.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), "PCI", flags) != _CR_SUCCESS or not size.value:
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    if cfgmgr32.CM_Get_Device_ID_ListW("PCI", buffer, size.value, flags) != _CR_SUCCESS:
        return []
    devices = []
    for instance in filter(None, buffer[: size.value].split("\0")):
        devinst = ctypes.c_ulong()
        if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), instance, 0) != _CR_SUCCESS:
            continue
        prop = lambda fmtid, pid: _property(cfgmgr32, devinst.value, fmtid, pid)  # noqa: E731
        devices.append({
            "instance": instance, "name": prop(_DEVICE, 14) or prop(_DEVICE, 2) or instance,
            "class": prop(_DEVICE, 9) or "", "driver": prop(_DEVICE, 11),
            "speed": prop(_PCI, 9), "width": prop(_PCI, 10), "max_speed": prop(_PCI, 11), "max_width": prop(_PCI, 12),
            "interrupts": prop(_PCI, 14),
        })
    return devices


def _gbps(gen: int | None, width: int | None) -> float:
    return _LANE_GBPS.get(gen or 0, 0.0) * (width or 0)


def _video_memory_gb(driver: str | None) -> float | None:
    if not driver:
        return None
    size = _reg(rf"SYSTEM\CurrentControlSet\Control\Class\{driver}", "HardwareInformation.qwMemorySize")
    return int(size) / _GB if isinstance(size, int) and size > 0 else None


def _pcie_checks(devices: list[dict], drives: list[dict]) -> list[dict]:
    checks = []
    cpu = _cpu_name()
    for gpu in [d for d in devices if d["class"] == "Display" and d["max_width"]]:
        width, max_width, gen = gpu["width"] or 0, gpu["max_width"], gpu["max_speed"]
        link = {"gpu": gpu["name"], "link": f"PCIe {_PCIE_GEN.get(gen, '?')}", "width": width,
                "gbps": i18n.num(_gbps(gen, width), ".1f")}
        idle = (tx("Its link speed drops while the card is idle, to save power; that's normal.")
                if gpu["speed"] and gen and gpu["speed"] < gen else None)
        vram = _video_memory_gb(gpu["driver"])
        title = tx("Graphics card link")
        if width < max_width:
            cause = (tx("On Ryzen processors with built-in graphics (APUs) like yours, the CPU gives the graphics slot "
                        "fewer lanes (x8 on the 2000G and 3000G series), so this is the platform's limit, not a fault.")
                     if _is_ryzen_apu(cpu) and width == 8 else
                     tx("Check that the card sits in the top slot (the one wired x16); the lower slots are often x4, "
                        "and a dusty or loose card can also link at fewer lanes."))
            checks.append(_check(
                "gpu_link", title, "info",
                tx("{gpu}: {link} x{width} (the card supports x{max}), about {gbps} GB/s each way.", max=max_width, **link),
                i18n.joined([cause, tx("Most games lose only a few percent at x{width}, more when a game needs more video "
                                       "memory than the card has, because textures then stream over this link: keep texture "
                                       "quality within your card's {vram} GB.", width=width, vram=i18n.num(vram, ".0f"))
                             if vram else
                             tx("Most games lose only a few percent at x{width}, more when a game needs more video memory "
                                "than the card has, because textures then stream over this link.", width=width), idle])))
        else:
            checks.append(_check(
                "gpu_link", title, "good",
                tx("{gpu}: {link} x{width}, its full width (about {gbps} GB/s each way).", **link), idle or ""))

    nvme_drives = [d for d in drives if d["kind"] == "NVMe SSD"]
    for nvme in [d for d in devices if "nvm" in d["name"].lower() and d["max_width"]]:
        name = nvme_drives[0]["model"] if len(nvme_drives) == 1 and nvme_drives[0]["model"] else tx("Your NVMe drive")
        gen, max_gen, width = nvme["speed"] or 0, nvme["max_speed"] or 0, nvme["width"] or 0
        link = {"drive": name, "gen": _PCIE_GEN.get(gen, "?"), "width": width, "gbps": i18n.num(_gbps(gen, width), ".1f")}
        if gen and max_gen > gen:
            checks.append(_check(
                "nvme_link", tx("NVMe drive link"), "info",
                tx("{drive}: runs at PCIe {gen} x{width} although it supports PCIe {max}: the motherboard or CPU's limit.",
                   max=_PCIE_GEN.get(max_gen, "?"), **link),
                tx("Its top sequential speed is capped near {gbps} GB/s instead of about {max} GB/s. Loading apps and "
                   "games is mostly small reads, which barely notice the difference; big file copies do.",
                   gbps=link["gbps"], max=i18n.num(_gbps(max_gen, width), ".1f"))))
        elif gen:
            checks.append(_check(
                "nvme_link", tx("NVMe drive link"), "good",
                tx("{drive}: PCIe {gen} x{width}, its full speed (about {gbps} GB/s).", **link)))
    return checks


def _interrupts_check(devices: list[dict]) -> dict | None:
    import winreg

    capable, using = [], []
    for d in devices:
        if not (d["interrupts"] or 0) & 0x6:  # MSI or MSI-X
            continue
        capable.append(d["name"])
        path = rf"SYSTEM\CurrentControlSet\Enum\{d['instance']}\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"
        if _reg(path, "MSISupported", winreg.HKEY_LOCAL_MACHINE) == 1:
            using.append(d["name"])
    if not capable:
        return None
    detail = tx("Each device signals the CPU with a small message instead of a shared interrupt line, so devices "
                "never wait on each other: the most direct way a part can call the CPU.")
    title = tx("Device interrupts")
    if len(using) == len(capable):
        return _check("interrupts", title, "good",
                      txn(len(capable), "The PCIe device that supports it uses message-signaled interrupts (MSI/MSI-X).",
                          "All {n} PCIe devices that support it use message-signaled interrupts (MSI/MSI-X)."),
                      detail)
    others: dict[str, int] = {}
    for name in capable:
        if name not in using:
            others[name] = others.get(name, 0) + 1
    rest = ", ".join(f"{count} × {name}" if count > 1 else name for name, count in others.items())
    return _check("interrupts", title, "info",
                  tx("{using} of {capable} PCIe devices use message-signaled interrupts (MSI/MSI-X); the driver of "
                     "{rest} doesn't turn them on.", using=len(using), capable=len(capable), rest=rest),
                  i18n.joined([detail, tx("The driver decides which mode a device uses, and forcing it can stop a device "
                                          "from working, so OptiMind only reports it.")]))


# ------------------------------------------------------------ drives and games

class _StoragePropertyQuery(ctypes.Structure):
    _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int), ("AdditionalParameters", ctypes.c_ubyte * 1)]


_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_BUS_NVME, _BUS_SATA, _BUS_USB = 0x11, 0x0B, 0x07


def _kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                         ctypes.c_void_p]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _storage_query(kernel32, handle, property_id: int, size: int) -> bytes | None:
    from ctypes import wintypes

    query = _StoragePropertyQuery(property_id, 0)
    out = ctypes.create_string_buffer(size)
    returned = wintypes.DWORD()
    ok = kernel32.DeviceIoControl(handle, _IOCTL_STORAGE_QUERY_PROPERTY, ctypes.byref(query), ctypes.sizeof(query),
                                  out, size, ctypes.byref(returned), None)
    return out.raw[: returned.value] if ok else None


def _drive_kind(letter: str) -> tuple[str, str]:
    """("NVMe SSD" / "SSD" / "hard disk" / "USB drive" / "drive", model) of a drive letter."""
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(rf"\\.\{letter}", 0, 0x1 | 0x2, None, 3, 0, None)  # no access needed to ask
    if not handle or handle == ctypes.c_void_p(-1).value:
        return "drive", ""
    try:
        device = _storage_query(kernel32, handle, 0, 1024)  # StorageDeviceProperty -> STORAGE_DEVICE_DESCRIPTOR
        penalty = _storage_query(kernel32, handle, 7, 12)  # StorageDeviceSeekPenaltyProperty
    finally:
        kernel32.CloseHandle(handle)
    bus, model = None, ""
    if device and len(device) >= 32:
        product_offset = int.from_bytes(device[16:20], "little")
        bus = int.from_bytes(device[28:32], "little")
        if 0 < product_offset < len(device):
            model = device[product_offset:].split(b"\0", 1)[0].decode("ascii", "replace").strip()
    seeks = bool(penalty[8]) if penalty and len(penalty) >= 9 else None
    if bus == _BUS_NVME:
        return "NVMe SSD", model
    if bus == _BUS_USB:
        return "USB drive", model
    if seeks is True:
        return "hard disk", model
    if seeks is False:
        return "SSD", model
    return "drive", model


def _drives() -> list[dict]:
    drives = []
    for part in psutil.disk_partitions(all=False):
        if "fixed" not in part.opts:
            continue
        letter = part.mountpoint.rstrip("\\")
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        kind, model = _drive_kind(letter)
        drives.append({"letter": letter.upper(), "kind": kind, "model": model, "free_gb": usage.free / _GB,
                       "total_gb": usage.total / _GB, "percent": usage.percent})
    return drives


def _steam_games() -> list[dict]:
    import winreg

    steam = _reg(r"Software\Valve\Steam", "SteamPath", winreg.HKEY_CURRENT_USER)
    if not steam:
        return []
    try:
        folders = Path(steam, "steamapps", "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    games = []
    for library in re.findall(r'"path"\s+"([^"]+)"', folders):
        apps = Path(library.replace("\\\\", "\\"), "steamapps")
        for manifest in apps.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]+)"', text)
            size = re.search(r'"SizeOnDisk"\s+"(\d+)"', text)
            played = re.search(r'"LastPlayed"\s+"(\d+)"', text)
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            folder = re.search(r'"installdir"\s+"([^"]+)"', text)
            if name and size and int(size.group(1)) > 0:
                games.append({"name": name.group(1), "path": str(apps), "gb": int(size.group(1)) / _GB,
                              "played": int(played.group(1)) if played else 0, "store": "Steam",
                              "appid": appid.group(1) if appid else None,
                              "install_path": str(apps / "common" / folder.group(1)) if folder else None})
    return games


def _epic_games() -> list[dict]:
    root = Path(os.environ.get("ProgramData", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "Data", "Manifests")
    games = []
    for item in root.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
            games.append({"name": data["DisplayName"], "path": data["InstallLocation"],
                          "gb": int(data.get("InstallSize", 0)) / _GB, "played": 0, "store": "Epic",
                          "appid": None, "install_path": data["InstallLocation"]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return games


_kept_values: dict[str, tuple[float, object]] = {}
_kept_lock = threading.Lock()


def _kept(key: str, seconds: float, compute):
    with _kept_lock:
        hit = _kept_values.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
    value = compute()
    with _kept_lock:
        _kept_values[key] = (time.monotonic(), value)
    return value


def drives() -> list[dict]:
    """The fixed drives: letter, kind ("NVMe SSD", "SSD", "hard disk"…), model, free space (kept a minute)."""
    return _kept("drives", 60, _drives) if IS_WINDOWS else []


def _page_file() -> dict:
    try:
        system = _wmi("SELECT AutomaticManagedPagefile FROM Win32_ComputerSystem")
        files = [{"path": str(p.Name), "size_mb": int(p.AllocatedBaseSize or 0), "used_mb": int(p.CurrentUsage or 0),
                  "peak_mb": int(p.PeakUsage or 0)}
                 for p in _wmi("SELECT Name, AllocatedBaseSize, CurrentUsage, PeakUsage FROM Win32_PageFileUsage")]
    except Exception:
        return {"system_managed": None, "files": []}
    return {"system_managed": bool(system[0].AutomaticManagedPagefile) if system else None, "files": files}


def page_file() -> dict:
    """The page file as Windows has it: whether Windows manages its size, and each file's path,
    size, use now and highest use since the PC started, in MB (kept a minute)."""
    return _kept("page_file", 60, _page_file) if IS_WINDOWS else {"system_managed": None, "files": []}


_GAMES_SECONDS = 600.0  # reading every Steam manifest can take seconds on a hard disk; libraries rarely change


def installed_games() -> list[dict]:
    """Games installed through Steam or Epic: name, store, size, install folder, last played (kept 10 min)."""
    return _kept("games", _GAMES_SECONDS, lambda: _steam_games() + _epic_games()) if IS_WINDOWS else []


_games_refresh = threading.Lock()


def installed_games_cached() -> list[dict] | None:
    """The installed games as last read, at once (None before the first read); a missing or
    stale list is read again in the background. For the agent's 2-second cycle, which mustn't
    wait on a hard disk."""
    if not IS_WINDOWS:
        return []
    with _kept_lock:
        hit = _kept_values.get("games")
    if (not hit or time.monotonic() - hit[0] >= _GAMES_SECONDS) and _games_refresh.acquire(blocking=False):
        def refresh() -> None:
            try:
                installed_games()
            except Exception:
                logger.debug("Couldn't read the game libraries", exc_info=True)
            finally:
                _games_refresh.release()
        threading.Thread(target=refresh, name="optimind-games-scan", daemon=True).start()
    return hit[1] if hit else None


def physical_disks() -> dict[int, str]:
    """Disk number (Disk 0, as Windows' logs name it) -> the drive's model (kept 10 minutes)."""
    def read() -> dict[int, str]:
        return {int(d.DeviceId): str(d.FriendlyName or "").strip()
                for d in _wmi("SELECT DeviceId, FriendlyName FROM MSFT_PhysicalDisk", r"root\Microsoft\Windows\Storage")
                if str(d.DeviceId).isdigit()}
    return _kept("physical_disks", 600, read) if IS_WINDOWS else {}


def drive_kind(path: str | None) -> str | None:
    """The kind of drive a path is on ("NVMe SSD", "hard disk"…), or None if unknown."""
    if not path or len(path) < 2 or path[1] != ":":
        return None
    return next((d["kind"] for d in drives() if d["letter"] == path[:2].upper()), None)


def _storage_checks(drives: list[dict]) -> list[dict]:
    checks = []
    if drives:
        listing = i18n.joined([
            tx("{drive} {kind} ({model}), {free} free", drive=d["letter"], kind=_kind(d), model=d["model"], free=_gb(d["free_gb"]))
            if d["model"] else tx("{drive} {kind}, {free} free", drive=d["letter"], kind=_kind(d), free=_gb(d["free_gb"]))
            for d in drives], " · ")
        system = os.environ.get("SystemDrive", "C:").upper()
        full = [d for d in drives if d["letter"] == system and d["percent"] >= 85]
        checks.append(_check(
            "drives", tx("Drives"), "warn" if full else "info", listing,
            tx("Windows and your apps load from the system drive; {drive} is {pct} full, and an SSD slows down when it's "
               "nearly full.", drive=system, pct=pct(full[0]["percent"])) if full else
            tx("Windows and your apps load from the system drive; keep it on the fastest drive (an NVMe SSD reads about "
               "20 times faster than a hard disk).")))

    by_letter = {d["letter"]: d for d in drives}
    paging = page_file()
    pagefiles = [f["path"] for f in paging["files"]]
    if pagefiles:
        on = [by_letter.get(p[:2].upper()) for p in pagefiles]
        slow = [d for d in on if d and d["kind"] == "hard disk"]
        fast = [d for d in drives if d["kind"] in ("NVMe SSD", "SSD")]
        first = paging["files"][0]
        use = (tx("It's {size}; at most {peak} MB of it was used since the PC started.", size=_gb(first["size_mb"] / 1024),
                  peak=i18n.num(first["peak_mb"])) if first["size_mb"] else None)
        virtual_memory = tx("System Properties > Advanced > Performance > Advanced > Virtual memory")
        title = tx("Page file")
        if slow and fast:
            checks.append(_check(
                "pagefile", title, "tip", tx("The page file is on a hard disk ({drive}).", drive=slow[0]["letter"]),
                tx("When memory runs out, Windows pages to this file; on a hard disk that's where the long freezes "
                   "come from."), tx("Move it to {drive} in {where}.", drive=fast[0]["letter"], where=virtual_memory)))
        elif paging["system_managed"] is False:
            checks.append(_check(
                "pagefile", title, "tip", i18n.joined([tx("The page file has a size set by hand."), use]),
                tx("A page file Windows manages grows when apps need more memory than the RAM and the page file hold; "
                   "one of a fixed size can't, and an app that runs out of memory then closes."),
                tx("Let Windows manage it: {where} > Automatically manage paging file size for all drives.",
                   where=virtual_memory)))
        else:
            where = on[0]
            checks.append(_check(
                "pagefile", title, "good",
                i18n.joined([tx("The page file is on {drive} ({kind}).", drive=where["letter"], kind=_kind(where)) if where
                             else tx("The page file is at {path}.", path=pagefiles[0]),
                             tx("Windows manages its size.") if paging["system_managed"] else None, use]),
                tx("Windows compresses memory before it pages, so the page file is rarely used; when it is, it's on a "
                   "fast drive.")))

    games = [g for g in _steam_games() + _epic_games() if g["gb"] >= 2]
    slow_games = [g for g in games if by_letter.get(g["path"][:2].upper(), {}).get("kind") == "hard disk"]
    fast = sorted((d for d in drives if d["kind"] in ("NVMe SSD", "SSD")), key=lambda d: -d["free_gb"])
    if slow_games and fast:
        slow_games.sort(key=lambda g: -g["gb"])
        total = sum(g["gb"] for g in slow_games)
        names = i18n.joined([tx("{game} ({size})", game=g["name"], size=_gb(g["gb"])) for g in slow_games[:5]], ", ")
        target = fast[0]
        checks.append(_check(
            "games_on_hdd", tx("Games on a hard disk"), "tip",
            txn(len(slow_games), "{n} game ({size}) is installed on a hard disk, including {games}.",
                "{n} games ({size}) are installed on a hard disk, including {games}.", size=_gb(total), games=names),
            tx("A hard disk reads around 100–200 MB/s and needs about 10 ms to reach each piece of data; your {kind} has "
               "almost no seek time. Loading screens and the stutter when a game streams in new areas or textures are "
               "where a game feels it. No setting in Windows can make the disk itself faster.", kind=_kind(target)),
            tx("Move the games you play to {drive} ({free} free). In Steam: Library > right-click the game > Properties > "
               "Installed Files > Move install folder.", drive=target["letter"], free=_gb(target["free_gb"]))))
    elif games:
        checks.append(_check("games_on_hdd", tx("Where your games are"), "good",
                             txn(len(games), "Your game of 2 GB or more is on an SSD.",
                                 "All {n} games of 2 GB or more are on SSDs.")))
    return checks


# ------------------------------------------------------------ Windows settings

def _memory_integrity_check() -> dict | None:
    guard = _wmi("SELECT SecurityServicesRunning, AvailableSecurityProperties FROM Win32_DeviceGuard",
                 r"root\Microsoft\Windows\DeviceGuard")
    if not guard:
        return None
    running = 2 in (guard[0].SecurityServicesRunning or ())
    hardware = 7 in (guard[0].AvailableSecurityProperties or ())
    configured = _reg(r"SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity",
                      "Enabled")
    title = tx("Memory integrity")
    if running and configured == 0:
        return _check("memory_integrity", title, "info",
                      tx("Memory integrity turns off at the next restart."), link=_MICROSOFT_GAMING)
    if not running and configured == 1:
        return _check("memory_integrity", title, "info",
                      tx("Memory integrity turns on at the next restart."), link=_MICROSOFT_HVCI)
    if running and not hardware:
        return _check(
            "memory_integrity", title, "tip",
            tx("Memory integrity (Core isolation) is on, and your processor lacks the feature that makes it fast "
               "(MBEC / GMET), so Windows emulates it."),
            tx("Microsoft: processors without it \"rely on an emulation of these features, called Restricted User Mode, "
               "and will have a bigger impact on performance\". Microsoft also lists turning Memory integrity off while "
               "gaming as an option. The trade-off: it blocks malicious or vulnerable drivers from injecting code into "
               "the Windows kernel, and that protection is gone while it's off."),
            tx("Your call. To turn it off: Optimize > Security trade-offs > Turn off Memory integrity (Undo turns it "
               "back on; both need a restart), or Windows Security > Device security > Core isolation."),
            tweak="memory_integrity", link=_MICROSOFT_HVCI)
    if running:
        return _check("memory_integrity", title, "good",
                      tx("Memory integrity is on, with hardware support (MBEC / GMET): a small performance cost."),
                      link=_MICROSOFT_HVCI)
    return _check(
        "memory_integrity", title, "info",
        tx("Memory integrity is off: games get the whole CPU, but drivers aren't checked by the hypervisor."),
        tx("Turn it back on when you don't need the extra speed: Optimize > Undo on its tweak, or Windows Security > "
           "Device security > Core isolation."), tweak="memory_integrity", link=_MICROSOFT_HVCI)


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _AdapterInfo(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint), ("AdapterLuid", _LUID), ("NumOfSources", ctypes.c_ulong),
                ("bPrecisePresentRegionsPreferred", ctypes.c_int)]


class _EnumAdapters2(ctypes.Structure):
    _fields_ = [("NumAdapters", ctypes.c_ulong), ("pAdapters", ctypes.POINTER(_AdapterInfo))]


class _QueryAdapterInfo(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint), ("Type", ctypes.c_int), ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", ctypes.c_uint)]


_KMTQAITYPE_ADAPTERTYPE, _KMTQAITYPE_WDDM_2_7_CAPS = 15, 70
_gpu_state: tuple[float, dict | None] | None = None


def gpu_scheduling() -> dict | None:
    """Hardware-accelerated GPU scheduling as the graphics driver reports it (D3DKMT WDDM 2.7
    caps of the first hardware GPU): {"supported", "enabled"}, or None when unknown.
    It only changes with a restart, so the answer is kept a minute."""
    global _gpu_state
    if _gpu_state is None or time.monotonic() - _gpu_state[0] > 60:
        try:
            _gpu_state = (time.monotonic(), _read_gpu_scheduling() if IS_WINDOWS else None)
        except OSError:
            _gpu_state = (time.monotonic(), None)
    return _gpu_state[1]


def _read_gpu_scheduling() -> dict | None:
    gdi32 = ctypes.WinDLL("gdi32")
    enum = _EnumAdapters2(0, None)
    if gdi32.D3DKMTEnumAdapters2(ctypes.byref(enum)) != 0 or not enum.NumAdapters:
        return None
    adapters = (_AdapterInfo * enum.NumAdapters)()
    enum.pAdapters = adapters
    if gdi32.D3DKMTEnumAdapters2(ctypes.byref(enum)) != 0:
        return None
    found = None
    for adapter in adapters[: enum.NumAdapters]:
        kind, caps = ctypes.c_uint(), ctypes.c_uint()
        query = _QueryAdapterInfo(adapter.hAdapter, _KMTQAITYPE_ADAPTERTYPE,
                                  ctypes.cast(ctypes.byref(kind), ctypes.c_void_p), 4)
        software = gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(query)) != 0 or kind.value & 0x4
        if found is None and not software:
            query = _QueryAdapterInfo(adapter.hAdapter, _KMTQAITYPE_WDDM_2_7_CAPS,
                                      ctypes.cast(ctypes.byref(caps), ctypes.c_void_p), 4)
            if gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(query)) == 0:
                found = {"supported": bool(caps.value & 0x1), "enabled": bool(caps.value & 0x2)}
        gdi32.D3DKMTCloseAdapter(ctypes.byref(ctypes.c_uint(adapter.hAdapter)))
    return found


def _gpu_scheduling_check() -> dict | None:
    state = gpu_scheduling()
    if state is None:
        return None
    title = tx("GPU scheduling")
    if state["enabled"]:
        return _check("gpu_scheduling", title, "good",
                      tx("Hardware-accelerated GPU scheduling is on: the graphics card schedules its own work."))
    if state["supported"]:
        return _check("gpu_scheduling", title, "tip",
                      tx("Your graphics card supports hardware-accelerated GPU scheduling, but it's off."),
                      tx("With it on, the card schedules its own work instead of the CPU doing it, which saves CPU time "
                         "and is needed for DLSS Frame Generation."),
                      tx("Optimize > Gaming > Hardware-accelerated GPU scheduling (takes effect after a restart)."),
                      tweak="gpu_scheduling")
    return _check("gpu_scheduling", title, "info",
                  tx("Your graphics card or its driver doesn't support hardware-accelerated GPU scheduling."))


def _game_mode_check() -> dict:
    import winreg

    value = _reg(r"Software\Microsoft\GameBar", "AutoGameModeEnabled", winreg.HKEY_CURRENT_USER)
    if value == 0:
        return _check("game_mode", tx("Game Mode"), "tip", tx("Windows' Game Mode is off."),
                      tx("Game Mode gives the game priority and holds back Windows Update driver installs and restart "
                         "prompts while you play."), tx("Optimize > Gaming > Keep Game Mode on."), tweak="game_mode")
    return _check("game_mode", tx("Game Mode"), "good",
                  tx("Windows' Game Mode is on: it works together with OptiMind's Focus mode while you play."))


# ------------------------------------------------------------ monitors

def _display_checks() -> list[dict]:
    from app.system import display

    checks = []
    for i, d in enumerate(display.displays()):
        screen = {"monitor": d["monitor"] or tx("Your monitor"), "size": f"{d['width']}×{d['height']}", "hz": d["hz"],
                  "max": d["max_hz"]}
        if d["max_hz"] > d["hz"] + 1:
            checks.append(_check(
                f"refresh_rate_{i}", tx("Refresh rate"), "tip",
                tx("{monitor} runs at {hz} Hz ({size}), but it offers up to {max} Hz.", **screen),
                tx("The refresh rate is how many frames the monitor shows each second: at {max} Hz games and the desktop "
                   "move more smoothly and each frame reaches the screen sooner, at no cost.", max=d["max_hz"]),
                tx("Settings > System > Display > Advanced display > Choose a refresh rate: {max} Hz.", max=d["max_hz"]),
                link="ms-settings:display-advanced"))
        else:
            checks.append(_check(f"refresh_rate_{i}", tx("Refresh rate"), "good",
                                 tx("{monitor}: {size} at {hz} Hz, the most it offers at this resolution.", **screen)))
    return checks


# ------------------------------------------------------------ headroom: GPU tuning, PBO, upgrades

def _recent_limits() -> Counter:
    """What limited the games you played lately (from the measured sessions)."""
    try:
        from app.agent import games

        return Counter((s.get("frames") or {}).get("limiter") for s in games.sessions(10) if s.get("frames"))
    except Exception:
        return Counter()


def _nvidia_app_installed() -> bool:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVIDIA app").is_dir()


_RADEON = re.compile(r"\bRadeon\s+(RX|PRO\s+W)", re.IGNORECASE)  # AMD's graphics cards (not built-in graphics)


def _gpu_tuning_check() -> dict | None:
    from app.system import gpu

    g = gpu.primary()
    if not g or "RTX" not in g["name"].upper():
        radeon = next((name for name in display_adapters() if _RADEON.search(name)), None)
        return _radeon_tuning_check(radeon) if radeon else None
    limited = _recent_limits()["gpu"]
    limit, top = g.get("power_limit_w"), g.get("max_power_limit_w")
    detail = i18n.joined([
        _limited_by_gpu(limited),
        tx("NVIDIA's automatic tuning tests your card for 10–20 minutes and keeps the highest clocks it found stable: "
           "usually a few percent more frames in games the graphics card limits. NVIDIA says it doesn't void the "
           "warranty, and Reset in the same place undoes it. Leave the PC alone while it scans, then watch the "
           "Stability check below for a few days: graphics driver resets mean it went too far."),
        tx("The same page has a power target: your card runs at {limit} W and its own firmware allows up to {top} W, so a "
           "GPU-limited game can boost higher there, with more heat and fan noise.", limit=i18n.num(limit, ".0f"),
           top=i18n.num(top, ".0f")) if limit and top and top > limit + 1 else None])
    return _check("gpu_tuning", tx("Graphics card tuning"), "tip" if limited else "info",
                  tx("{gpu}: NVIDIA App can overclock it automatically, within what the card proves stable.", gpu=g["name"]),
                  detail,
                  tx("NVIDIA App > System > Performance > Automatic tuning.") if _nvidia_app_installed() else
                  tx("Install NVIDIA App, then System > Performance > Automatic tuning."),
                  link=None if _nvidia_app_installed() else "https://www.nvidia.com/en-us/software/nvidia-app/")


def _limited_by_gpu(limited: int) -> str | None:
    return txn(limited, "{n} of your last measured game sessions was limited by the graphics card.",
               "{n} of your last measured game sessions were limited by the graphics card.") if limited else None


def _radeon_tuning_check(card: str) -> dict:
    """The same for AMD's graphics cards, with AMD's own app."""
    limited = _recent_limits()["gpu"]
    return _check(
        "gpu_tuning", tx("Graphics card tuning"), "tip" if limited else "info",
        tx("{gpu}: AMD Software can tune it automatically, within what the card proves stable.", gpu=card),
        i18n.joined([_limited_by_gpu(limited),
                     tx("AMD Software: Adrenalin Edition has automatic tuning: Undervolt GPU keeps the clocks with less "
                        "power and heat, often the best first step, and Overclock GPU raises them (some cards offer only "
                        "some of these). Reset in the same place undoes it. Afterwards, watch the Stability check below "
                        "for a few days: graphics driver resets mean it went too far.")]),
        tx("AMD Software > Performance > Tuning, then Undervolt GPU or Overclock GPU."),
        link="https://www.amd.com/en/products/software/adrenalin.html")


_NO_PBO = re.compile(r"\b(2200GE?|2400GE?|3200GE?|3000G|Athlon)\b", re.IGNORECASE)
_AM4_CHIPSETS = re.compile(r"\b(A320|B350|X370|B450|X470|A520|B550|X570)", re.IGNORECASE)  # "B450M-…" too


def _cpu_headroom_checks() -> list[dict]:
    cpu = _cpu_name()
    if "ryzen" not in cpu.lower():
        return []
    checks = []
    limited = _recent_limits()["cpu"]
    seen = txn(limited, "{n} of your last measured game sessions was limited by the processor.",
               "{n} of your last measured game sessions were limited by the processor.") if limited else None
    if not _NO_PBO.search(cpu):
        checks.append(_check(
            "pbo", tx("Processor boost (PBO)"), "tip" if limited else "info",
            tx("{cpu} supports Precision Boost Overdrive, AMD's own automatic overclocking, set in the BIOS.", cpu=cpu),
            i18n.joined([seen, tx("PBO lets the processor use the motherboard's power headroom above AMD's warranted "
                                  "limits, so it holds higher clocks for longer while cooling allows: usually 0–5% in "
                                  "games, more in heavy multi-core work, with more heat and power. AMD states that PBO, "
                                  "like any overclocking, voids the processor's AMD warranty. With a stock cooler, keep an "
                                  "eye on temperatures.")]),
            tx("In the BIOS: Precision Boost Overdrive > Enabled ({where}). Afterwards, the Stability check below shows "
               "whether the processor stays error-free.",
               where=_PBO_MENU.get(_board_maker(), tx("usually in its overclocking section"))),
            link="https://www.amd.com/en/products/software/ryzen-master.html"))
    try:
        board = _wmi("SELECT Product FROM Win32_BaseBoard")
        bios = _wmi("SELECT ReleaseDate FROM Win32_BIOS")
    except Exception:
        return checks
    product = str(board[0].Product or "") if board else ""
    released = str(bios[0].ReleaseDate or "")[:8] if bios else ""
    series = re.search(r"Ryzen\s+\d\s+(?:PRO\s+)?(\d)\d{3}", cpu)
    old = series and (int(series.group(1)) <= 3 or (_is_ryzen_apu(cpu) and int(series.group(1)) <= 4))
    if old and _AM4_CHIPSETS.search(product) and released[:4].isdigit() and int(released[:4]) >= 2021:
        cores = psutil.cpu_count(logical=False) or 0
        checks.append(_check(
            "cpu_upgrade", tx("Processor upgrade"), "tip" if limited else "info",
            tx("Your {board} is an AM4 board with a BIOS from {year}: Ryzen 5000 processors fit the same socket.",
               board=product, year=released[:4]),
            i18n.joined([seen, txn(cores, "Your processor has {n} core and older cores.",
                                   "Your processor has {n} cores and older cores.") if cores else None,
                         tx("In games the processor limits, a Ryzen 5 5600 or Ryzen 7 5700X3D is the biggest single "
                            "upgrade this PC can get, and without built-in graphics it gives the graphics card all 16 PCIe "
                            "lanes. No setting does the same.")]),
            tx("Check your motherboard's CPU support list for the processor and the BIOS version it needs before "
               "buying, and update the BIOS first if it asks for a newer one.")))
    return checks


_INTEL_13_14 = re.compile(r"\bi([579])-1[34]\d{2,3}([A-Z]*)\b")  # Core i5/i7/i9 of the 13th/14th generation
_INTEL_FIX, _INTEL_NEWEST_FIX = 0x12B, 0x12F
_INTEL_NOTICE = "https://www.intel.com/content/www/us/en/support/articles/000102331/processors.html"


def _microcode_revision() -> int | None:
    """The processor microcode version Windows reports (Intel keeps it in the upper half of the value)."""
    raw = _reg(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "Update Revision")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 4:
        return None
    if len(raw) >= 8:
        return int.from_bytes(raw[4:8], "little") or int.from_bytes(raw[:4], "little")
    return int.from_bytes(raw[:4], "little")


def _intel_stability_check() -> dict | None:
    """Intel's 13th and 14th generation desktop processors: Intel's fix for their instability ("Vmin
    shift") comes with the BIOS, as microcode 0x12B and then 0x12F (Intel's support article)."""
    cpu = _cpu_name()
    match = _INTEL_13_14.search(cpu)
    if "intel" not in cpu.lower() or not match or set(match.group(2)) & set("HPU"):
        return None  # another processor, or a laptop one (Intel: not exposed to the issue)
    revision = _microcode_revision()
    shown = f"0x{revision:X}" if revision is not None else "unknown"
    detail = tx("Intel found that some 13th and 14th generation desktop processors can become unstable over time from "
                "too-high operating voltage. Its fix comes with your motherboard's BIOS: microcode 0x12B, then 0x12F, "
                "together with the Intel Default Settings profile. Intel calls it preventive: a processor that already "
                "crashes needs Intel's support. Laptop processors aren't affected.")
    title = tx("Intel 13th/14th gen stability")
    if revision is not None and revision >= _INTEL_NEWEST_FIX:
        return _check("intel_stability", title, "good",
                      tx("{cpu}: microcode {version}, with Intel's newest fix.", cpu=cpu, version=shown), detail,
                      link=_INTEL_NOTICE)
    quiet = match.group(1) == "5" and "K" not in match.group(2)  # Intel: no confirmed reports for non-K i5
    status = "info" if revision is None or quiet else "warn" if revision < _INTEL_FIX else "tip"
    summary = (tx("{cpu}: Intel recommends a BIOS with microcode 0x12F or newer.", cpu=cpu) if revision is None else
               tx("{cpu}: microcode {version}, older than Intel's fix for 13th/14th gen instability.", cpu=cpu, version=shown)
               if revision < _INTEL_FIX else
               tx("{cpu}: microcode {version}, older than Intel's newest fix (0x12F).", cpu=cpu, version=shown))
    return _check("intel_stability", title, status, summary, detail,
                  tx("Update the BIOS from your motherboard maker's support page to a version with microcode 0x12F or "
                     "newer, then choose Intel Default Settings in the BIOS. OptiMind never updates a BIOS itself."),
                  link=_INTEL_NOTICE)


# ------------------------------------------------------------ stability (overclocking safety net)

def _stability_check() -> dict | None:
    from app.system import stability

    data = stability.report(30)
    if data is None:
        return None
    c = data["categories"]
    serious = ("hardware", "gpu_reset", "bluescreen")
    found = [_STABILITY_EVENTS[k](c[k]["count"]) for k in (*serious, "shutdown") if c[k]["count"]]
    crashes = c["app_crash"]
    apps = ", ".join(f"{name} ({n})" for name, n in (crashes.get("apps") or {}).items())
    crash_text = tx("Apps that crashed: {apps}.", apps=apps) if apps else None
    if not found:
        return _check("stability", tx("Stability"), "good",
                      tx("No hardware errors, graphics driver resets, bluescreens or unexpected shutdowns in the last 30 "
                         "days."),
                      i18n.joined([tx("OptiMind watches Windows' own logs for the first signs of an unstable overclock or "
                                      "failing hardware, so if you try PBO, memory overclocking or GPU tuning, check back "
                                      "here."), crash_text]))
    recent = any(c[k]["last_7_days"] for k in serious)
    whea = c["hardware"].get("ids") or {}
    detail = [tx("Hardware errors (WHEA) come from the processor, the memory or a PCIe link; the classic causes are an "
                 "overclock, an undervolt or a memory profile (XMP/EXPO) the system can't hold. Graphics driver resets "
                 "come from an unstable GPU overclock, the driver or heat.")]
    if 19 in whea or 18 in whea:
        detail.append(tx("Yours include machine-check errors (WHEA 18/19): the processor or memory."))
    if 17 in whea:
        detail.append(tx("Yours include PCIe errors (WHEA 17): a link to the graphics card or an NVMe drive."))
    found_text = i18n.joined(found, ", ")
    return _check(
        "stability", tx("Stability"), "warn" if recent else "info",
        tx("In the last 30 days: {found} (some this week).", found=found_text) if recent
        else tx("In the last 30 days: {found}.", found=found_text),
        i18n.joined([*detail, crash_text]),
        tx("If you overclocked anything (PBO, memory, GPU tuning), go back one step or to the defaults. Otherwise "
           "update the graphics driver and the BIOS, and check temperatures under load."))


# How many of each kind of event Windows logged (app/system/stability.py)
_STABILITY_EVENTS = {
    "hardware": lambda n: txn(n, "{n} hardware error (WHEA)", "{n} hardware errors (WHEA)"),
    "gpu_reset": lambda n: txn(n, "{n} graphics driver reset", "{n} graphics driver resets"),
    "bluescreen": lambda n: txn(n, "{n} bluescreen", "{n} bluescreens"),
    "shutdown": lambda n: txn(n, "{n} unexpected shutdown", "{n} unexpected shutdowns"),
}


# ------------------------------------------------------------ drive health

_HEALTH = {0: tx("Healthy"), 1: tx("Warning"), 2: tx("Unhealthy")}


def _reliability_counters() -> dict[str, dict]:
    """Storage reliability counters per disk (Get-StorageReliabilityCounter; administrators only)."""
    import subprocess

    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "Get-PhysicalDisk | ForEach-Object { $d = $_; $r = $d | Get-StorageReliabilityCounter -ErrorAction Stop; "
         "[pscustomobject]@{ Id = $d.DeviceId; Temperature = $r.Temperature; Wear = $r.Wear; "
         "ReadErrorsUncorrected = $r.ReadErrorsUncorrected; WriteErrorsUncorrected = $r.WriteErrorsUncorrected; "
         "PowerOnHours = $r.PowerOnHours; LoadUnloadCycleCount = $r.LoadUnloadCycleCount } } | "
         "ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0 or not done.stdout.strip():
        return {}
    data = json.loads(done.stdout)
    return {str(d["Id"]): d for d in ([data] if isinstance(data, dict) else data)}


def drive_health(disks: list[dict], counters: dict[str, dict]) -> dict | None:
    """One check for every physical disk: Windows' health verdict and, with administrator
    rights, temperature, wear and uncorrectable errors."""
    if not disks:
        return None
    worst, parts, problems = "good", [], []
    rank = {"good": 0, "info": 1, "tip": 2, "warn": 3}
    for d in disks:
        name = d["name"] or tx("A drive")
        status = "good"
        health = _HEALTH.get(d.get("health"), tx("Unknown"))
        bits = [health]
        if d.get("health") in (1, 2):
            status = "warn"
            problems.append(tx("Windows reports {drive} as {health}: back up what's on it now.", drive=name,
                               health=i18n.lowercase(health)))
        c = counters.get(str(d.get("id")))
        if c:
            temp, wear = c.get("Temperature"), c.get("Wear")
            errors = (c.get("ReadErrorsUncorrected") or 0) + (c.get("WriteErrorsUncorrected") or 0)
            if temp:
                bits.append(f"{temp} °C")
            if wear is not None and d.get("ssd"):
                bits.append(tx("{pct} worn", pct=pct(wear)))
            if c.get("PowerOnHours"):
                bits.append(txn(c["PowerOnHours"], "{n} hour", "{n} hours"))
            if errors:
                status = "warn"
                problems.append(txn(errors, "{drive} has {n} error it couldn't correct: back up what's on it now.",
                                    "{drive} has {n} errors it couldn't correct: back up what's on it now.", drive=name))
            if wear is not None and d.get("ssd") and wear >= 90:
                status = "warn"
                problems.append(tx("{drive} has used {pct} of its rated write endurance: plan to replace it.", drive=name,
                                   pct=pct(wear)))
            elif temp and temp >= (70 if d.get("ssd") else 55):
                status = max(status, "tip", key=rank.get)
                problems.append(tx("{drive} runs at {temp} °C: give it more airflow (or a heatsink for an NVMe drive).",
                                   drive=name, temp=temp))
        parts.append(tx("{drive}: {state}", drive=name, state=i18n.joined(bits, ", ")))
        worst = max(worst, status, key=rank.get)
    detail = i18n.joined(problems) if problems else i18n.joined([
        tx("Windows' own health check finds nothing wrong."),
        None if counters else tx("Temperature and wear need OptiMind to run as administrator.")])
    return _check("drive_health", tx("Drive health"), worst, i18n.joined(parts, " · "), detail)


def _drive_health_check() -> dict | None:
    disks = [{"id": str(d.DeviceId), "name": str(d.FriendlyName or "").strip(),
              "health": 5 if d.HealthStatus is None else int(d.HealthStatus),  # 0 = Healthy (not "missing")
              "ssd": int(d.MediaType or 0) == 4}
             for d in _wmi("SELECT DeviceId, FriendlyName, MediaType, HealthStatus FROM MSFT_PhysicalDisk",
                           r"root\Microsoft\Windows\Storage")]
    try:
        counters = _reliability_counters() if is_elevated() else {}
    except Exception:
        counters = {}
    return drive_health(disks, counters)


# ------------------------------------------------------------ hard disk spin-down, hypervisor

def power_setting(plan: str, subgroup: str, setting: str) -> tuple[int, int] | None:
    """(plugged in, on battery) value of one power plan setting, or None when the plan or
    setting doesn't exist. powercfg's labels are translated, so the values are read from
    the end of its output: the last two hex numbers are always the AC and DC indexes."""
    import subprocess

    done = subprocess.run(["powercfg", "/q", plan, subgroup, setting], capture_output=True, text=True, timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    values = re.findall(r":\s*0x([0-9a-f]+)\s*$", done.stdout, re.IGNORECASE | re.MULTILINE)
    if done.returncode != 0 or len(values) < 2:
        return None
    return int(values[-2], 16), int(values[-1], 16)


def disk_idle_seconds() -> int | None:
    """The active power plan's "Turn off hard disk after" while plugged in (0 = never)."""
    value = power_setting("SCHEME_CURRENT", "SUB_DISK", "DISKIDLE")
    return value[0] if value else None


def _disk_spin_check(drives: list[dict]) -> dict | None:
    hard = {d["letter"] for d in drives if d["kind"] == "hard disk"}
    games_there = [g for g in installed_games() if (g.get("install_path") or g["path"])[:2].upper() in hard]
    if not games_there:
        return None
    seconds = disk_idle_seconds()
    title = tx("Hard disk spin-down")
    if not seconds:
        return _check("disk_spin", title, "good",
                      tx("Your hard disks keep spinning while the PC is plugged in, so games start without waiting."))
    return _check(
        "disk_spin", title, "tip",
        txn(seconds // 60, "Your games' hard disk stops spinning after {n} minute without use.",
            "Your games' hard disk stops spinning after {n} minutes without use."),
        tx("Starting a game from it later first waits a few seconds for the disk to spin up again (and so does anything "
           "else that reads it). On a desktop, keeping it spinning costs about a watt; it also spares the disk the "
           "wear of thousands of stops and starts."),
        tx("Optimize > Storage > Keep hard disks spinning (plugged in)."), tweak="disk_spin")


def _hypervisor_check() -> dict | None:
    system = _wmi("SELECT HypervisorPresent FROM Win32_ComputerSystem")
    if not system or not system[0].HypervisorPresent:
        return None
    guard = _wmi("SELECT SecurityServicesRunning FROM Win32_DeviceGuard", r"root\Microsoft\Windows\DeviceGuard")
    if guard and 2 in (guard[0].SecurityServicesRunning or ()):
        return None  # Memory integrity runs it: that check covers it
    return _check(
        "hypervisor", tx("Virtualization"), "info",
        tx("A hypervisor is running (Virtual Machine Platform, Hyper-V, WSL 2, Windows Sandbox or an Android emulator)."),
        tx("Windows then runs on top of it too, which costs a little in games; Microsoft lists turning off Virtual Machine "
           "Platform as an option for gamers. If you don't use WSL 2, virtual machines or Android emulators, you can "
           "turn those features off."),
        tx("Settings > System > Optional features > More Windows features: untick Virtual Machine Platform, Hyper-V and "
           "Windows Hypervisor Platform, then restart."), link=_MICROSOFT_GAMING)


# ------------------------------------------------------------ report

def cached_report() -> dict | None:
    """The last hardware check if it's still fresh, without running one (it takes seconds)."""
    with _lock:
        return _cache[1] if _cache and time.monotonic() - _cache[0] < _CACHE_SECONDS else None


def report(refresh: bool = False) -> dict:
    """Every check, worst first; each has a status, what was found, why it matters and what to do."""
    global _cache
    if not IS_WINDOWS:
        return {"checked_at": None, "checks": []}
    with _lock:
        if _cache and not refresh and time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
        checks: list[dict] = []

        def add(build, *args) -> None:
            try:
                result = build(*args)
            except Exception:
                logger.debug("Hardware check %s failed", build.__name__, exc_info=True)
                return
            checks.extend(result if isinstance(result, list) else [result] if result else [])

        try:
            devices = _pci_devices()
        except Exception:
            devices = []
        try:
            drives = _drives()
        except Exception:
            drives = []
        add(_cpu_check)
        add(_memory_checks)
        add(_pcie_checks, devices, drives)
        add(_display_checks)
        add(_storage_checks, drives)
        add(_disk_spin_check, drives)
        add(_drive_health_check)
        # Headroom (overclocking, upgrades) after what's simply wrong, then the stability safety net
        add(_cpu_headroom_checks)
        add(_intel_stability_check)
        add(_gpu_tuning_check)
        add(_stability_check)
        add(_memory_integrity_check)
        add(_hypervisor_check)
        add(_gpu_scheduling_check)
        add(_game_mode_check)
        add(_interrupts_check, devices)
        order = {"warn": 0, "tip": 1, "info": 2, "good": 3}
        checks.sort(key=lambda c: order.get(c["status"], 4))
        data = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks}
        _cache = (time.monotonic(), data)
        return data
