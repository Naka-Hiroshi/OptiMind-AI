"""
What works on this PC, and why not (Settings → About → What works on this PC, and the report).

OptiMind runs on any Windows 11 PC or laptop, with any processor, graphics card or brand, but some
parts need administrator rights, NVIDIA's driver or a power plan not every PC has. Each item says
whether it works here ("on"), works with less ("partial") or doesn't ("off"), why, and what to do.
"""
from __future__ import annotations

import logging

import psutil

from app import i18n
from app.config import IS_WINDOWS, settings
from app.i18n import tx

logger = logging.getLogger("optimind.capabilities")

_AMD_DRIVERS = "https://www.amd.com/en/support/download/drivers.html"
_INTEL_DRIVERS = "https://www.intel.com/content/www/us/en/support/detect.html"


def _item(id: str, title: str, status: str, text: str, action: str = "", link: str | None = None) -> dict:
    return {"id": id, "title": title, "status": status, "text": text, "action": action, "link": link}


def _vendor(adapters: list[str]) -> str:
    """"nvidia", "amd" or "intel": the maker of the graphics card that matters most here."""
    names = " ".join(adapters).lower()
    return next((v for v in ("nvidia", "amd", "intel") if v in names or (v == "amd" and "radeon" in names)), "")


def _admin() -> dict:
    from app.system import admin_rights

    if admin_rights.is_elevated():
        return _item("admin", tx("Administrator rights"), "on",
                     tx("OptiMind runs with them, so it can manage every app, empty the standby cache, merge memory "
                        "pages, measure frame rates and install drivers."))
    return _item("admin", tx("Administrator rights"), "partial",
                 tx("OptiMind runs with standard rights, so it leaves alone your apps that run as administrator and "
                    "startup apps for all users, and skips the standby cache and page merging. Frame rates and driver "
                    "installs need them too."),
                 tx("Settings > Administrator rights > Restart with administrator rights.")
                 if admin_rights.can_ask_for_admin() else tx("This Windows account isn't an administrator."))


def _frames() -> dict:
    from app.system import admin_rights, frames

    title = tx("Frame-rate measurement")
    if not frames.available():
        return _item("frames", title, "off", tx("PresentMon, the tool that measures frames, is missing."),
                     tx("Install OptiMind again."))
    if not admin_rights.is_elevated():
        return _item("frames", title, "off", tx("Measuring frames needs OptiMind to run with administrator rights."))
    if not settings.optimind_measure_games:
        return _item("frames", title, "partial", tx("Turned off in Settings: Measure for 60 s on the Games page still "
                                                    "works."))
    return _item("frames", title, "on", tx("Frame rates, 1% lows and what holds them back are measured while you "
                                           "play, with any graphics card."))


def _sensors(adapters: list[str], vendor: str) -> dict:
    from app.system import gpu

    title = tx("Graphics card sensors")
    if gpu.available():
        g = gpu.primary() or {}
        return _item("gpu_sensors", title, "on", tx("{card}: temperature, clocks, power and why it holds its clocks "
                                                    "back, from NVIDIA's driver.",
                                                    card=g.get("name") or tx("Your NVIDIA card")))
    card = ", ".join(adapters) or tx("Your graphics card")
    own = {"amd": tx("AMD Software shows them for your card."), "intel": tx("Intel's graphics app shows them.")}.get(vendor)
    return _item("gpu_sensors", title, "partial", i18n.joined([
        tx("{card}: Windows shows how busy it is. Temperature, clocks and power come from NVIDIA's driver, so OptiMind "
           "shows them for NVIDIA cards only.", card=card), own]))


def _power_plans() -> dict | None:
    from app.system import monitor

    plans = monitor.installed_power_plans()
    if plans is None:
        return None
    try:
        battery = psutil.sensors_battery() is not None
    except Exception:
        battery = False
    title = tx("Power plan while you play")
    if "High performance" in plans:
        return _item("power_plan", title, "on",
                     tx("OptiMind switches to High performance while you play or the PC is busy, and back to Balanced "
                        "after three calm minutes, never on battery.") if battery else
                     tx("OptiMind switches to High performance while you play or the PC is busy, and back to Balanced "
                        "after three calm minutes."))
    return _item("power_plan", title, "partial",
                 tx("This PC has no High performance plan: Windows' Power mode replaces it, as on many laptops, so "
                    "OptiMind leaves the plan alone. For games, set Power mode to Best performance while plugged in.")
                 if battery else
                 tx("This PC has no High performance plan: Windows' Power mode replaces it, as on many laptops, so "
                    "OptiMind leaves the plan alone. For games, set Power mode to Best performance."),
                 tx("Windows Settings > System > Power & battery > Power mode.") if battery else
                 tx("Windows Settings > System > Power > Power mode."),
                 "ms-settings:powersleep")


def _cores() -> dict:
    from app.agent import optimizer

    sets = optimizer.cpu_sets()
    if not sets:
        return _item("cores", tx("Background apps while you play"), "partial",
                     tx("Windows didn't list the processor's cores, so Maximum focus mode lowers background apps' "
                        "priority but doesn't move them to one core."))
    hybrid = len({s.EfficiencyClass for s in sets}) > 1
    return _item("cores", tx("Background apps while you play"), "on",
                 tx("Maximum focus mode keeps busy background apps on the efficiency cores, leaving the performance "
                    "cores to the game.") if hybrid else
                 tx("Maximum focus mode keeps busy background apps on the last core, leaving the others to the game."))


def _newest_driver(vendor: str) -> dict:
    title = tx("Newest graphics driver")
    if vendor == "nvidia":
        return _item("newest_driver", title, "on",
                     tx("The Drivers page checks NVIDIA's newest Game Ready driver and installs it after a restore "
                        "point."))
    if vendor == "amd":
        return _item("newest_driver", title, "partial",
                     tx("The Drivers page checks NVIDIA's newest driver only. For your AMD card, AMD Software: "
                        "Adrenalin Edition checks for AMD's newest driver; Windows Update's drivers are on the Drivers "
                        "page."),
                     tx("AMD's drivers page, or AMD Software."), _AMD_DRIVERS)
    if vendor == "intel":
        return _item("newest_driver", title, "partial",
                     tx("The Drivers page checks NVIDIA's newest driver only. For Intel graphics, Intel Driver & "
                        "Support Assistant checks for Intel's newest driver; Windows Update's drivers are on the "
                        "Drivers page."),
                     tx("Intel Driver & Support Assistant."), _INTEL_DRIVERS)
    return _item("newest_driver", title, "partial", tx("Windows Update's drivers for your graphics card are on the "
                                                       "Drivers page."))


def report() -> list[dict]:
    """Every item, each built on its own: one that fails is left out (and logged)."""
    if not IS_WINDOWS:
        return []
    from app.system import hardware

    adapters = hardware.display_adapters()
    vendor = _vendor(adapters)
    items = []
    for build in (_admin, _frames, lambda: _sensors(adapters, vendor), _power_plans, _cores,
                  lambda: _newest_driver(vendor)):
        try:
            item = build()
        except Exception:
            logger.warning("A 'what works on this PC' item failed", exc_info=True)
            continue
        if item:
            items.append(item)
    return items
