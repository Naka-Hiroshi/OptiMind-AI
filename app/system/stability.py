"""
Stability watch (read-only): what Windows' own event logs recorded about crashes and
hardware errors. It's the safety net for overclocking: an unstable CPU, memory (XMP)
or GPU overclock shows up here first, often before you notice anything.

  - Hardware errors (WHEA-Logger): the CPU, memory or a PCIe link reported an error.
    Corrected ones (ID 17 PCIe, 19 machine check, 47 memory) are the early sign; a
    fatal one (ID 18) usually ends in a bluescreen. Overclocks, undervolts, XMP/EXPO
    memory profiles and high Infinity Fabric clocks are the classic causes.
  - Graphics driver resets: "Display driver stopped responding and has successfully
    recovered" (Display 4101, nvlddmkm 153): an unstable GPU overclock, a driver
    problem or overheating.
  - Bluescreens (BugCheck 1001) and unexpected shutdowns (Kernel-Power 41: the PC lost
    power or froze and was reset).
  - App crashes (Application Error 1000), counted per app.

Cached for 10 minutes; one query reads both logs.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.config import IS_WINDOWS

logger = logging.getLogger("optimind.stability")

_CACHE_SECONDS = 600.0
_MAX_EVENTS = 2000
_lock = threading.Lock()
_cache: dict[int, tuple[float, dict]] = {}

CATEGORIES = {
    "hardware": "hardware errors (WHEA)",
    "gpu_reset": "graphics driver resets",
    "bluescreen": "bluescreens",
    "shutdown": "unexpected shutdowns",
    "app_crash": "app crashes",
}
_PROVIDERS = {
    "Microsoft-Windows-WHEA-Logger": "hardware",
    "Microsoft-Windows-WER-SystemErrorReporting": "bluescreen",
    "Microsoft-Windows-Kernel-Power": "shutdown",
}
_QUERIES = {  # channel -> XPath (the events above, newer than {ms} milliseconds)
    "System": "*[System[(Provider[@Name='Microsoft-Windows-WHEA-Logger']"
              " or (Provider[@Name='Microsoft-Windows-WER-SystemErrorReporting'] and EventID=1001)"
              " or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41)"
              " or (Provider[@Name='Display'] and EventID=4101)"
              " or (Provider[@Name='nvlddmkm'] and EventID=153))"
              " and TimeCreated[timediff(@SystemTime) <= {ms}]]]",
    "Application": "*[System[Provider[@Name='Application Error'] and EventID=1000"
                   " and TimeCreated[timediff(@SystemTime) <= {ms}]]]",
}
_PROVIDER_RE = re.compile(r"<Provider Name=['\"]([^'\"]+)['\"]")
_ID_RE = re.compile(r"<EventID(?: [^>]*)?>(\d+)</EventID>")
_TIME_RE = re.compile(r"<TimeCreated SystemTime=['\"]([^'\"]+)['\"]")
_DATA_RE = re.compile(r"<Data(?: Name=['\"][^'\"]*['\"])?>([^<]*)</Data>")


def classify(provider: str, event_id: int) -> str | None:
    if provider in _PROVIDERS:
        return _PROVIDERS[provider]
    if (provider, event_id) in (("Display", 4101), ("nvlddmkm", 153)):
        return "gpu_reset"
    if provider == "Application Error" and event_id == 1000:
        return "app_crash"
    return None


def parse_time(text: str) -> datetime | None:
    """An event's time ("2026-10-18T15:09:36.4890840Z") in UTC (also for app/system/driver_events.py)."""
    try:  # Windows writes 7 decimals, Python reads 6
        return datetime.strptime(text[:26].rstrip("Z") + "Z", "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def parse_event(xml: str) -> dict | None:
    """One event's provider, ID, time and (for an app crash) the app, from its XML."""
    provider, event_id, created = _PROVIDER_RE.search(xml), _ID_RE.search(xml), _TIME_RE.search(xml)
    if not (provider and event_id and created):
        return None
    category = classify(provider.group(1), int(event_id.group(1)))
    when = parse_time(created.group(1))
    if category is None or when is None:
        return None
    data = _DATA_RE.findall(xml)
    return {"category": category, "id": int(event_id.group(1)), "time": when,
            "app": data[0].strip() if category == "app_crash" and data else None}


def _read_events(days: int) -> list[dict]:
    import win32evtlog

    events: list[dict] = []
    for channel, xpath in _QUERIES.items():
        handle = win32evtlog.EvtQuery(channel, win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection,
                                      xpath.replace("{ms}", str(days * 86_400_000)))
        found = 0
        while found < _MAX_EVENTS:
            batch = win32evtlog.EvtNext(handle, 100)
            if not batch:
                break
            for event in batch:
                parsed = parse_event(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                if parsed:
                    events.append(parsed)
                    found += 1
    return events


def summarize(events: list[dict], days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    week = now - timedelta(days=7)
    out: dict = {"days": days, "checked_at": now.isoformat(), "categories": {}}
    for category in CATEGORIES:
        mine = [e for e in events if e["category"] == category]
        item = {"count": len(mine), "last_7_days": sum(1 for e in mine if e["time"] >= week),
                "last": max((e["time"] for e in mine), default=None)}
        item["last"] = item["last"].isoformat() if item["last"] else None
        if category == "hardware":
            item["ids"] = dict(Counter(e["id"] for e in mine))
        if category == "app_crash":
            item["apps"] = dict(Counter(e["app"] for e in mine if e["app"]).most_common(5))
        out["categories"][category] = item
    return out


def report(days: int = 30, refresh: bool = False) -> dict | None:
    """Counts per category for the last `days` days (None when the logs can't be read)."""
    if not IS_WINDOWS:
        return None
    with _lock:
        hit = _cache.get(days)
        if hit and not refresh and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]
        try:
            data = summarize(_read_events(days), days)
        except Exception as e:
            logger.debug("Couldn't read the event logs: %s", e)
            return None
        _cache[days] = (time.monotonic(), data)
        return data
