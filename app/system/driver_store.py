"""
The driver store (read-only): the driver packages that hardware and app makers added to
Windows (published as oemNN.inf), which devices use each one, who signed it, what it takes
on disk, and the older versions a newer package replaced.

Windows keeps every package it installs in C:\\Windows\\System32\\DriverStore\\FileRepository,
so a device can get its driver back without the original download, and Device Manager
can roll a driver back to the previous version. Old versions stay there until removed.

The list comes from `pnputil /enum-drivers /devices /format xml` (language-independent
XML; Windows 11 26H2 and later). On older Windows the same facts are read from the
INF files themselves (SetupAPI), with the devices from the device list.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

from app.config import IS_WINDOWS

logger = logging.getLogger("optimind.driver_store")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INF_DIR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "INF"


def version_key(version: str) -> tuple[int, ...]:
    """'32.0.16.1742' -> (32, 0, 16, 1742); parts that aren't numbers count as 0."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.,]", (version or "").strip())[:4])


def parse_driver_ver(text: str) -> tuple[str | None, str]:
    """INF DriverVer / pnputil 'Driver Version' ("09/30/2026 32.0.16.1742") -> ("2026-09-30", "32.0.16.1742")."""
    match = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*,?\s*(\S*)", text or "")
    if not match:
        return None, (text or "").strip()
    month, day, year, version = match.groups()
    try:
        date = datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        date = None
    return date, version


def parse_packages(xml_text: str) -> list[dict]:
    """The packages in pnputil's XML (`/enum-drivers /devices /format xml`)."""
    root = ET.fromstring(xml_text)
    packages = []
    for d in root.iter("Driver"):
        date, version = parse_driver_ver(d.findtext("DriverVersion") or "")
        devices = [{"id": x.get("InstanceId") or "", "name": x.findtext("DeviceDescription") or "",
                    "status": x.findtext("Status") or ""} for x in d.iter("Device")]
        packages.append({
            "name": (d.get("DriverName") or "").lower(),
            "original": (d.findtext("OriginalName") or "").lower(),
            "provider": (d.findtext("ProviderName") or "").strip(),
            "class": d.findtext("ClassName") or "",
            "class_guid": (d.findtext("ClassGuid") or "").strip("{}").lower() or None,
            "version": version,
            "date": date,
            "signer": d.findtext("SignerName") or None,
            "attributes": [a.text for a in d.iter("Attribute") if a.text],
            "devices": devices,
        })
    return packages


def _pnputil_packages() -> list[dict] | None:
    """None when this Windows' pnputil can't write XML (older than Windows 11 26H2)."""
    with tempfile.TemporaryDirectory(prefix="optimind-pnputil-") as folder:
        out = Path(folder, "drivers.xml")
        try:
            done = subprocess.run(["pnputil", "/enum-drivers", "/devices", "/format", "xml", "/output-file", str(out)],
                                  capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0 or not out.exists():
            return None
        try:
            return parse_packages(out.read_text(encoding="utf-8-sig", errors="replace"))
        except ET.ParseError:
            logger.warning("pnputil's driver list couldn't be read")
            return None


# ------------------------------------------------------------ older Windows: the INF files themselves

def _read_inf(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace") if raw[:3] == b"\xef\xbb\xbf" else raw.decode("mbcs", "replace")


def _without_comment(line: str) -> str:
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i].strip()
    return line.strip()


def inf_version_section(text: str) -> dict:
    """Provider, Class, ClassGuid and DriverVer of an INF's [Version] section, %strings% resolved."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    for line in text.splitlines():
        line = _without_comment(line)
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            current = sections.setdefault(line[1:line.index("]")].strip().lower(), {})
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current.setdefault(key.strip().lower(), value.strip())
    strings = {k: v.strip().strip('"') for k, v in sections.get("strings", {}).items()}

    def resolve(value: str) -> str:
        resolved = re.sub(r"%([^%]+)%", lambda m: strings.get(m.group(1).lower(), m.group(0)), value or "")
        return resolved.strip().strip('"')

    version = sections.get("version", {})
    return {key: resolve(version.get(key, "")) for key in ("provider", "class", "classguid", "driverver")}


class _OriginalFileInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("OriginalInfName", wintypes.WCHAR * 260),
                ("OriginalCatalogName", wintypes.WCHAR * 260)]


class _InfSignerInfo(ctypes.Structure):  # SP_INF_SIGNER_INFO_V2_W
    _fields_ = [("cbSize", wintypes.DWORD), ("CatalogFile", wintypes.WCHAR * 260),
                ("DigitalSigner", wintypes.WCHAR * 260), ("DigitalSignerVersion", wintypes.WCHAR * 260),
                ("SignerScore", wintypes.DWORD)]


def _setupapi():
    api = ctypes.WinDLL("setupapi", use_last_error=True)
    api.SetupGetInfDriverStoreLocationW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPCWSTR,
                                                    wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.SetupGetInfDriverStoreLocationW.restype = wintypes.BOOL
    api.SetupGetInfInformationW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
    api.SetupGetInfInformationW.restype = wintypes.BOOL
    api.SetupQueryInfOriginalFileInformationW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                                          ctypes.POINTER(_OriginalFileInfo)]
    api.SetupQueryInfOriginalFileInformationW.restype = wintypes.BOOL
    api.SetupVerifyInfFileW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_InfSignerInfo)]
    api.SetupVerifyInfFileW.restype = wintypes.BOOL
    return api


def _original_name(api, inf: Path) -> str | None:
    needed = wintypes.DWORD()
    path = ctypes.create_unicode_buffer(str(inf))
    if not api.SetupGetInfInformationW(path, 2, None, 0, ctypes.byref(needed)):  # INFINFO_INF_NAME_IS_ABSOLUTE
        return None
    info = ctypes.create_string_buffer(needed.value)
    if not api.SetupGetInfInformationW(path, 2, info, needed.value, None):
        return None
    original = _OriginalFileInfo(cbSize=ctypes.sizeof(_OriginalFileInfo))
    if not api.SetupQueryInfOriginalFileInformationW(info, 0, None, ctypes.byref(original)):
        return None
    return original.OriginalInfName or None


def _inf_packages(devices: list[dict]) -> list[dict]:
    api = _setupapi()
    users: dict[str, list[dict]] = {}
    for d in devices:
        inf = (d.get("driver") or {}).get("inf") or ""
        if inf.startswith("oem"):
            users.setdefault(inf, []).append({"id": d["id"], "name": d["name"],
                                              "status": "Started" if d["present"] else "Disconnected"})
    packages = []
    for inf in sorted(INF_DIR.glob("oem*.inf")):
        try:
            version = inf_version_section(_read_inf(inf))
        except OSError:
            continue
        date, ver = parse_driver_ver(version["driverver"])
        name = inf.name.lower()
        packages.append({
            "name": name, "original": (_original_name(api, inf) or name).lower(), "provider": version["provider"],
            "class": version["class"], "class_guid": version["classguid"].strip("{}").lower() or None,
            "version": ver, "date": date, "signer": None, "attributes": [], "devices": users.get(name, []),
        })
    return packages


# ------------------------------------------------------------ who signed a package

WINDOWS_SIGNER = "Microsoft Windows"  # the INF files that came with Windows (every one not named oemNN.inf)
_NOT_CHECKED = {2, 3, 5}  # file or folder not found, access denied: no answer about the signature

_signers: dict[str, tuple[float, tuple[str | None, str | None]]] = {}  # INF -> (its time, (signer, signing))
_signers_lock = threading.Lock()


def signing(signer: str | None) -> str | None:
    """How a driver is signed, from the signer Windows names: "windows" (part of Windows),
    "microsoft" (through Microsoft's hardware program, as Windows wants drivers from other
    makers), "maker" (only with its maker's own certificate, the old way) or None (not known)."""
    name = (signer or "").strip()
    if not name:
        return None
    if name == WINDOWS_SIGNER:
        return "windows"
    return "microsoft" if name.lower().startswith("microsoft") else "maker"


def _verify_inf(path: Path) -> tuple[bool, str, int]:
    """SetupVerifyInfFile: (trusted by Windows on its own, the signer it names, the error)."""
    info = _InfSignerInfo(cbSize=ctypes.sizeof(_InfSignerInfo))
    ok = bool(_setupapi().SetupVerifyInfFileW(str(path), None, ctypes.byref(info)))
    return ok, info.DigitalSigner, 0 if ok else ctypes.get_last_error()


def inf_signer(published: str) -> tuple[str | None, str | None]:
    """(signer, signing) of a package, as Windows checks its INF against its catalog
    (SetupVerifyInfFile, the check behind Device Manager's "Digital Signer"). A signature
    Windows trusts on its own names Windows or Microsoft's hardware program; one made only
    with the maker's own certificate fails that check but still names the maker; with none at
    all, nobody is named ("unsigned"). Checked once per package and run (the INF's time tells
    a reused oemNN.inf name apart)."""
    path = INF_DIR / published
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None, None
    with _signers_lock:
        hit = _signers.get(published)
    if hit and hit[0] == stamp:
        return hit[1]
    ok, signer, error = _verify_inf(path)
    if signer:
        result = (signer, signing(signer))
    elif ok or error in _NOT_CHECKED:
        result = (None, None)
    else:
        result = (None, "unsigned")
    with _signers_lock:
        _signers[published] = (stamp, result)
    return result


# ------------------------------------------------------------ store folders and sizes

_sizes: dict[str, int] = {}  # a store folder never changes once written: measured once per run
_sizes_lock = threading.Lock()


def store_folder(published: str) -> Path | None:
    """FileRepository\\<original>_<arch>_<hash> of an oemNN.inf."""
    api = _setupapi()
    buffer = ctypes.create_unicode_buffer(1024)
    needed = wintypes.DWORD()
    if not api.SetupGetInfDriverStoreLocationW(str(INF_DIR / published), None, None, buffer, 1024,
                                               ctypes.byref(needed)):
        return None
    return Path(buffer.value).parent


def folder_size(folder: Path) -> int:
    key = str(folder).lower()
    with _sizes_lock:
        if key in _sizes:
            return _sizes[key]
    total = 0
    for dirpath, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    with _sizes_lock:
        _sizes[key] = total
    return total


def _added(published: str) -> str | None:
    """When the package was added to this PC: its oemNN.inf was written then."""
    try:
        return datetime.fromtimestamp(os.stat(INF_DIR / published).st_ctime, timezone.utc).isoformat()
    except OSError:
        return None


# ------------------------------------------------------------ old versions

def _newer(a: dict, b: dict) -> bool:
    """Is package a newer than package b (date first, as Windows ranks drivers, then version)?"""
    return (a.get("date") or "", version_key(a["version"])) > (b.get("date") or "", version_key(b["version"]))


def mark_old_versions(packages: list[dict]) -> None:
    """Marks the versions a newer package of the same driver replaced. A package is "old" when
    no device uses it (not even one that isn't connected now) and a newer package of the same
    driver (original INF name, provider and class) is in use. The newest old one stays, so
    Device Manager can still roll the driver back ("rollback"); the older ones are "removable".
    A driver none of whose versions is in use is left alone: its device may just be unplugged."""
    groups: dict[tuple, list[dict]] = {}
    for p in packages:
        p.update(in_use=bool(p["devices"]), old=False, rollback=False, removable=False)
        provider = re.sub(r"[^a-z0-9]", "", p["provider"].lower())
        groups.setdefault((p["original"], provider, p["class"].lower()), []).append(p)
    for group in groups.values():
        used = [p for p in group if p["in_use"]]
        if not used:
            continue
        newest_used = max(used, key=lambda p: ((p.get("date") or ""), version_key(p["version"])))
        old = sorted((p for p in group if not p["in_use"] and _newer(newest_used, p)),
                     key=lambda p: ((p.get("date") or ""), version_key(p["version"])), reverse=True)
        for i, p in enumerate(old):
            p["old"] = True
            p["rollback"] = i == 0
            p["removable"] = i > 0


def packages(devices: list[dict]) -> dict:
    """{"packages": [...], "source": "pnputil" | "inf"}, each with its devices, signer, size and age."""
    if not IS_WINDOWS:
        return {"packages": [], "source": None}
    found = _pnputil_packages()
    source = "pnputil"
    if found is None:
        found, source = _inf_packages(devices), "inf"
    for p in found:
        folder = store_folder(p["name"])
        p["folder"] = folder.name if folder else None
        p["size"] = folder_size(folder) if folder else 0
        p["added"] = _added(p["name"])
        if p.get("signer"):  # pnputil names it
            p["signing"] = signing(p["signer"])
        else:
            p["signer"], p["signing"] = inf_signer(p["name"])
    mark_old_versions(found)
    found.sort(key=lambda p: (p["class"].lower(), p["original"], p.get("date") or ""))
    return {"packages": found, "source": source}
