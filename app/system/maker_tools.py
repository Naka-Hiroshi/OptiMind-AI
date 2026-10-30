"""
The makers' own driver apps (the Drivers page). Windows Update covers most devices, and
OptiMind installs those and NVIDIA's driver itself. AMD, Intel and the PC makers deliver
some drivers through their own app instead, which knows each model: for the parts of this
PC (its graphics, its Intel parts) and for the PC itself, this names the maker's official
app, whether it's in your Start menu, and the maker's own page to get it from.

Read-only: it never downloads or installs anything. The window opens an app you have, from
the Start menu, with your normal rights (app/desktop/window.py).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass

from app.config import IS_WINDOWS
from app.i18n import tx

logger = logging.getLogger("optimind.maker_tools")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_KEEP_SECONDS = 600.0  # installing an app changes the Start menu: read it again after 10 minutes
_START_APPS = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
               "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")


@dataclass(frozen=True)
class Tool:
    id: str
    name: str  # as its maker names it
    updates: str  # what it keeps up to date
    match: str  # its name in the Start menu (a regular expression, any case)
    fits: frozenset  # "part:<vendor>" for a part's maker, "pc:<maker>" for a brand PC or laptop, "board:<maker>"
    url: tuple[str, str] | None = None  # the maker's page for it; None: the PC or board maker's support page
    note: str = ""
    suggest: bool = True  # False: listed only when it's installed


TOOLS = (
    Tool("nvidia_app", "NVIDIA App", tx("GeForce drivers (OptiMind also checks NVIDIA's newest driver itself)"),
         r"^(nvidia app|geforce experience)$", frozenset({"part:nvidia"}),
         ("NVIDIA App", "https://www.nvidia.com/en-us/software/nvidia-app/")),
    Tool("amd_software", "AMD Software: Adrenalin Edition", tx("Radeon graphics drivers"), r"^amd software",
         frozenset({"part:amd"}), (tx("AMD drivers and support"), "https://www.amd.com/en/support/download/drivers.html")),
    Tool("intel_dsa", "Intel Driver & Support Assistant", tx("drivers for Intel graphics, Wi-Fi, Bluetooth, network "
         "and chipsets"), r"^intel.*driver\s*(&|and)\s*support\s*assistant", frozenset({"part:intel"}),
         ("Intel Driver & Support Assistant", "https://www.intel.com/content/www/us/en/support/detect.html")),
    Tool("dell", "Dell SupportAssist", tx("drivers for your Dell"), r"supportassist|command\s*\|\s*update",
         frozenset({"pc:dell"}), note=tx("Business PCs use Dell Command | Update")),
    Tool("hp", "HP Support Assistant", tx("drivers for your HP"), r"^hp support assistant", frozenset({"pc:hp"})),
    Tool("lenovo", "Lenovo Vantage", tx("drivers for your Lenovo"), r"^lenovo (commercial )?vantage|^(lenovo )?system update$",
         frozenset({"pc:lenovo"}), note=tx("ThinkPad, ThinkCentre and ThinkStation can also use Lenovo System Update")),
    Tool("myasus", "MyASUS", tx("drivers for your ASUS laptop or PC"), r"^myasus$", frozenset({"pc:asus"})),
    Tool("armoury_crate", "Armoury Crate", tx("drivers and utilities for ASUS boards and ROG and TUF PCs"),
         r"^armoury crate", frozenset({"pc:asus", "board:asus"}), suggest=False),
    Tool("msi_center", "MSI Center", tx("drivers and utilities for MSI laptops and boards"), r"^msi center",
         frozenset({"pc:msi", "board:msi"})),
    Tool("gigabyte_control_center", "GIGABYTE Control Center", tx("drivers and utilities for GIGABYTE laptops and "
         "boards"), r"^gigabyte control center", frozenset({"pc:gigabyte", "board:gigabyte"})),
    Tool("acer", "Acer Care Center", tx("drivers for your Acer"), r"^(acer care center|acersense)", frozenset({"pc:acer"}),
         note=tx("Newer models use AcerSense")),
)


# ------------------------------------------------------------ the Start menu

def parse_start_apps(text: str) -> list[dict]:
    """Get-StartApps' JSON (one object, a list, or nothing) as [{"name", "app_id"}]."""
    try:
        data = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    return [{"name": str(a["Name"]), "app_id": str(a["AppID"])} for a in items
            if isinstance(a, dict) and a.get("Name") and a.get("AppID")]


_apps: tuple[float, list[dict]] | None = None
_apps_lock = threading.Lock()


def start_apps(refresh: bool = False) -> list[dict]:
    """The apps in your Start menu, classic and Store apps alike (Get-StartApps), kept 10 minutes."""
    global _apps
    if not IS_WINDOWS:
        return []
    with _apps_lock:
        if _apps and not refresh and time.monotonic() - _apps[0] < _KEEP_SECONDS:
            return _apps[1]
        try:
            done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _START_APPS],
                                  capture_output=True, timeout=60, creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("Couldn't read the Start menu", exc_info=True)
            return []
        if done.returncode != 0:
            return []  # not kept: asked again next time
        _apps = (time.monotonic(), parse_start_apps(done.stdout.decode("utf-8", "replace")))
        return _apps[1]


# ------------------------------------------------------------ which apps fit this PC

def parts(devices: list[dict]) -> set[str]:
    """The part makers with an app of their own among the connected devices: NVIDIA and AMD
    graphics, and any Intel part (graphics, Wi-Fi, network, chipset; Intel Bluetooth on USB)."""
    found = set()
    for d in devices:
        if not d.get("present"):
            continue
        ids = f"{d.get('id') or ''} {d.get('hardware_id') or ''}".upper()
        if d.get("class") == "Display" and "VEN_10DE" in ids:
            found.add("part:nvidia")
        elif d.get("class") == "Display" and "VEN_1002" in ids:
            found.add("part:amd")
        if "VEN_8086" in ids or "VID_8087" in ids:
            found.add("part:intel")
    return found


def tools(devices: list[dict], fw: dict | None, apps: list[dict]) -> list[dict]:
    """The makers' apps that fit this PC: each with whether it's in your Start menu (its name
    and ID there, to open it) and the maker's own page for it. A brand PC or laptop gets its
    maker's app; a board in a self-built PC only shows its maker's app if it's installed (the
    board's download page has the same drivers without it)."""
    fits = parts(devices)
    fw = fw or {}
    support = fw.get("support") or {}
    if support.get("key"):
        fits.add(("pc:" if (fw.get("system") or {}).get("maker") else "board:") + support["key"])
    out = []
    for tool in TOOLS:
        context = tool.fits & fits
        if not context:
            continue
        found = next((a for a in apps if re.search(tool.match, a["name"], re.IGNORECASE)), None)
        suggested = tool.suggest and any(not c.startswith("board:") for c in context)
        if not found and not suggested:
            continue
        label, url = tool.url or (support.get("label"), support.get("url"))
        out.append({"id": tool.id, "name": tool.name, "updates": tool.updates, "note": tool.note,
                    "installed": bool(found), "start_name": found["name"] if found else None,
                    "app_id": found["app_id"] if found else None, "url": url, "url_label": label})
    return out
