"""
Devices and their drivers (read-only), from Windows' Plug and Play manager (CfgMgr32,
the API Device Manager itself reads): every device Windows knows, connected or not,
with its driver's provider, version, date and INF file, and the problem code Device
Manager shows next to its yellow mark.

Reading ~250 devices takes about 0.2 s. Nothing is changed here.

Problem codes and their fixes: Microsoft, "Error codes in Device Manager in Windows"
(support.microsoft.com/kb/310123) and "Device Manager error messages" (Cfg.h).
"""
from __future__ import annotations

import ctypes
import uuid
from datetime import datetime, timedelta, timezone

from app.config import IS_WINDOWS
from app.i18n import tx


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


_DEVICE = "a45c254e-df1c-4efd-8020-67d146a850e0"
_DRIVER = "a8b865dd-2e3d-4094-ad97-e593a70c75d6"
_INSTALL = "83da6326-97a6-4088-9453-a1923f573b29"
_NODE = "4340a6c5-93fa-4706-972c-7b648008a5a7"
_PRESENCE = "540b947e-8b40-45bc-a8a2-6a0b894cbda2"
_NAME = "b725f130-47ef-101a-a5f1-02608c9eebac"

KEYS = {  # DEVPKEY_* (devpkey.h): property set, property id
    "name": (_NAME, 10),  # DEVPKEY_NAME: the name Device Manager shows
    "hardware_ids": (_DEVICE, 3),
    "compatible_ids": (_DEVICE, 4),
    "service": (_DEVICE, 6),
    "class": (_DEVICE, 9),
    "class_guid": (_DEVICE, 10),
    "manufacturer": (_DEVICE, 13),
    "bus": (_DEVICE, 24),  # EnumeratorName
    "driver_date": (_DRIVER, 2),
    "driver_version": (_DRIVER, 3),
    "driver_description": (_DRIVER, 4),
    "inf": (_DRIVER, 5),  # DriverInfPath: oemNN.inf (a package added to the driver store) or Windows' own INF
    "matching_id": (_DRIVER, 8),
    "provider": (_DRIVER, 9),
    "installed": (_INSTALL, 100),  # InstallDate: when its driver was last installed
    "first_installed": (_INSTALL, 101),  # FirstInstallDate: when the device first appeared
    "parent": (_NODE, 8),
    "present": (_PRESENCE, 5),  # IsPresent
}
_PHANTOM_KEYS = ("name", "class", "class_guid", "manufacturer", "bus", "driver_date", "driver_version", "inf",
                 "provider", "service", "installed")  # enough for a device that isn't connected

_CR_SUCCESS, _CR_BUFFER_SMALL = 0x0, 0x1A
_DN_HAS_PROBLEM = 0x400
_CM_GETIDLIST_FILTER_PRESENT = 0x100
_CM_LOCATE_DEVNODE_PHANTOM = 0x1

# Code: (what Device Manager says, in plain words; what to do). Codes 45 and 46 (not connected,
# shutting down) aren't problems, and a device you turned off (22) is your choice.
PROBLEMS = {
    1: (tx("This device isn't configured correctly."), tx("Update its driver.")),
    3: (tx("Its driver might be damaged, or Windows was low on memory."), tx("Close some apps, then reinstall its driver.")),
    9: (tx("Windows can't identify this hardware: it reports no valid hardware ID."),
        tx("Contact the hardware maker: the device or its firmware is faulty.")),
    10: (tx("This device can't start."), tx("Restart the device; if that doesn't help, update or reinstall its driver.")),
    12: (tx("This device can't find enough free resources."),
         tx("Turn off a device it conflicts with, or check the BIOS settings.")),
    14: (tx("This device can't work properly until you restart the PC."), tx("Restart the PC.")),
    16: (tx("Windows can't identify all the resources this device uses."),
         tx("Assign them in Device Manager > the device > Properties > Resources.")),
    18: (tx("Its driver needs to be reinstalled."), tx("Scan for hardware changes, or update the driver.")),
    19: (tx("Its configuration in the registry is incomplete or damaged."),
         tx("Reinstall its driver, or remove the device and scan for hardware changes.")),
    21: (tx("Windows is removing this device."), tx("Wait a moment and check again, or restart the PC.")),
    22: (tx("This device is turned off (disabled)."), tx("Turn it back on if you need it.")),
    24: (tx("This device isn't present, isn't working properly, or doesn't have all its drivers."),
         tx("Reconnect it, or scan for hardware changes.")),
    28: (tx("Its driver isn't installed."), tx("Install it: Windows Update first, then the device maker's website.")),
    29: (tx("The firmware (BIOS) didn't give this device the resources it needs."), tx("Turn the device on in the BIOS.")),
    31: (tx("Windows can't load the drivers this device needs."), tx("Restart the device, then reinstall its driver.")),
    32: (tx("A driver (service) this device needs is turned off."),
         tx("Reinstall its driver, or remove the device and scan for hardware changes.")),
    33: (tx("Windows can't determine which resources this device needs."),
         tx("Update the BIOS; if that doesn't help, the hardware may need repair.")),
    34: (tx("Windows can't determine this device's settings."),
         tx("Set it up as its manual says (Device Manager > Properties > Resources).")),
    35: (tx("The PC's firmware lacks the information to set up this device."),
         tx("Update the BIOS from the PC or motherboard maker's website.")),
    36: (tx("This device asks for a PCI interrupt but is set up for an ISA one."),
         tx("Change the interrupt settings in the BIOS.")),
    37: (tx("Windows can't initialize this device's driver."),
         tx("Remove the device and scan for hardware changes, then update its driver.")),
    38: (tx("A previous copy of its driver is still in memory."), tx("Restart the PC.")),
    39: (tx("Its driver may be damaged or missing."),
         tx("Remove the device and scan for hardware changes, then reinstall its driver.")),
    40: (tx("Its driver's entry in the registry is missing."), tx("Remove the device and scan for hardware changes.")),
    41: (tx("Windows loaded its driver but can't find the hardware."),
         tx("Reconnect it, or remove it and scan for hardware changes.")),
    42: (tx("Windows found a duplicate of this device."), tx("Restart the PC.")),
    43: (tx("Windows stopped this device because it reported a problem."),
         tx("Restart the device; if it comes back, update or reinstall its driver. If it persists, the hardware may "
         "be failing.")),
    44: (tx("An app or service shut this device down."), tx("Restart the PC.")),
    47: (tx("This device was prepared for safe removal."), tx("Unplug it and plug it back in, or restart the PC.")),
    48: (tx("Its driver is blocked because it's known to cause problems with Windows."),
         tx("Get a newer driver from the device maker or Windows Update.")),
    49: (tx("Windows can't start new devices because the system registry is too large."),
         tx("Remove devices you no longer use, then restart.")),
    50: (tx("Windows can't apply all of this device's properties."), tx("Reinstall it, or get a newer driver from its maker.")),
    51: (tx("This device is waiting for another device to start."), tx("Check the devices it depends on.")),
    52: (tx("Windows can't verify the digital signature of its driver."),
         tx("Get a signed driver from Windows Update or the device maker.")),
    53: (tx("This device is reserved for the Windows kernel debugger."), tx("Turn off kernel debugging, then restart.")),
    54: (tx("This device failed and is being reset."), tx("Restart the PC if it stays like this.")),
    56: (tx("Windows is still setting up this kind of device."), tx("Wait a moment, or restart the PC.")),
    57: (tx("This device couldn't be assigned to a virtual machine."), tx("Check the virtual machine's settings.")),
}
NOT_PROBLEMS = {0, 45, 46}  # fine, not connected, shutting down


def problem_text(code: int) -> tuple[str, str]:
    """(what's wrong, what to do) for a Device Manager problem code."""
    return PROBLEMS.get(code, (tx("Device Manager reports problem code {code}.", code=code),
                               tx("Restart the PC; if it stays, update or reinstall the device's driver.")))


def _cfgmgr32():
    return ctypes.WinDLL("cfgmgr32")


def _key(name: str) -> _DEVPROPKEY:
    fmtid, pid = KEYS[name]
    return _DEVPROPKEY(_GUID.from_buffer_copy(uuid.UUID(fmtid).bytes_le), pid)


_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def filetime(raw: bytes) -> datetime | None:
    ticks = int.from_bytes(raw[:8], "little")
    return _EPOCH + timedelta(microseconds=ticks // 10) if ticks else None


def decode(kind: int, raw: bytes):
    """A device property's value from its DEVPROPTYPE and bytes."""
    if kind == 0x2012:  # DEVPROP_TYPE_STRING_LIST
        return [s for s in raw.decode("utf-16-le", "replace").split("\0") if s]
    base = kind & 0xFFF
    if base == 0x12:  # string
        return raw.decode("utf-16-le", "replace").rstrip("\0")
    if base == 0x7:  # uint32
        return int.from_bytes(raw[:4], "little")
    if base == 0x10:  # FILETIME
        return filetime(raw)
    if base == 0x11:  # boolean
        return raw[:1] != b"\0"
    if base == 0xD:  # GUID
        return str(uuid.UUID(bytes_le=bytes(raw[:16])))
    return None


class _Reader:
    """One CfgMgr32 handle and a reused buffer: a property costs one call unless it's long."""

    def __init__(self) -> None:
        self.cm = _cfgmgr32()
        self.keys = {name: _key(name) for name in KEYS}
        self.size = 2048
        self.buffer = ctypes.create_string_buffer(self.size)

    def ids(self, present_only: bool) -> list[str]:
        flags = _CM_GETIDLIST_FILTER_PRESENT if present_only else 0
        length = ctypes.c_ulong()
        if self.cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(length), None, flags) != _CR_SUCCESS:
            return []
        buffer = ctypes.create_unicode_buffer(length.value)
        if self.cm.CM_Get_Device_ID_ListW(None, buffer, length.value, flags) != _CR_SUCCESS:
            return []
        return [s for s in buffer[: length.value].split("\0") if s]

    def locate(self, instance: str) -> int | None:
        node = ctypes.c_ulong()
        if self.cm.CM_Locate_DevNodeW(ctypes.byref(node), instance, _CM_LOCATE_DEVNODE_PHANTOM) != _CR_SUCCESS:
            return None
        return node.value

    def prop(self, node: int, name: str):
        kind, size = ctypes.c_ulong(), ctypes.c_ulong(self.size)
        key = ctypes.byref(self.keys[name])
        result = self.cm.CM_Get_DevNode_PropertyW(node, key, ctypes.byref(kind), self.buffer, ctypes.byref(size), 0)
        if result == _CR_BUFFER_SMALL:
            self.size = size.value
            self.buffer = ctypes.create_string_buffer(self.size)
            result = self.cm.CM_Get_DevNode_PropertyW(node, key, ctypes.byref(kind), self.buffer,
                                                      ctypes.byref(size), 0)
        return decode(kind.value, self.buffer.raw[: size.value]) if result == _CR_SUCCESS else None

    def problem(self, node: int) -> int:
        status, code = ctypes.c_ulong(), ctypes.c_ulong()
        if self.cm.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(code), node, 0) != _CR_SUCCESS:
            return 0
        return code.value if status.value & _DN_HAS_PROBLEM else 0


def _iso_date(value) -> str | None:
    return value.date().isoformat() if isinstance(value, datetime) else None


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def describe(instance: str, props: dict, present: bool, problem: int) -> dict:
    """The device as the rest of OptiMind uses it (also built by the tests from plain values).
    "ids" (its hardware and compatible IDs, to match driver updates) isn't for the page."""
    has_driver = bool(props.get("inf") or props.get("driver_version"))
    hardware_ids = props.get("hardware_ids") or []
    return {
        "ids": [i.lower() for i in hardware_ids + (props.get("compatible_ids") or [])],
        "id": instance,
        "name": props.get("name") or instance,
        "class": props.get("class") or "",
        "class_guid": props.get("class_guid"),
        "manufacturer": props.get("manufacturer") or "",
        "bus": props.get("bus") or instance.split("\\", 1)[0],
        "present": present,
        "problem": problem if present else 0,
        "service": props.get("service") or "",
        "hardware_id": hardware_ids[0] if hardware_ids else None,
        "parent": props.get("parent"),
        "driver": {
            "provider": props.get("provider") or "",
            "version": props.get("driver_version") or "",
            "date": _iso_date(props.get("driver_date")),
            "description": props.get("driver_description") or "",
            "inf": (props.get("inf") or "").lower(),
            "matching_id": props.get("matching_id") or "",
        } if has_driver else None,
        "installed": _iso(props.get("installed")),
        "first_installed": _iso(props.get("first_installed")),
    }


def read_devices() -> list[dict]:
    """Every device Windows knows: connected ones with all their driver details, the others
    (connected before: a USB stick, an old graphics card) with the essentials."""
    if not IS_WINDOWS:
        return []
    reader = _Reader()
    connected = set(reader.ids(present_only=True))
    devices = []
    for instance in reader.ids(present_only=False):
        node = reader.locate(instance)
        if node is None:
            continue
        present = instance in connected
        names = KEYS if present else _PHANTOM_KEYS
        props = {name: reader.prop(node, name) for name in names}
        devices.append(describe(instance, props, present, reader.problem(node) if present else 0))
    return devices
