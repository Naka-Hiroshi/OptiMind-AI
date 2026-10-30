"""
Focus mode: the app you are using gets the PC first, and switching apps stays instant.

Windows tells OptiMind the moment another window comes to the front (a WinEvent
hook on EVENT_SYSTEM_FOREGROUND: no polling, no CPU used while nothing changes).
Within a few milliseconds of the switch:

  1. whatever the agent was holding back on that app is put back (priority,
     efficiency mode, I/O and memory priority, CPU cores), so it runs at full
     speed from its first frame;
  2. it runs at Above normal priority (only from Normal: an app set lower or
     higher on purpose keeps its own), and the app that had the boost gets its
     own priority back.

A switch never slows anything down: background apps are only held back later by
the decision engine (after its grace period), and the two apps you used just
before the current one are never memory-compressed, so Alt+Tab back to them
doesn't wait for their memory to come back.

A "game" is an app installed in a game library (Steam, Epic, GOG, Xbox, EA,
Ubisoft, Riot), or one whose window fills its screen (exclusive or borderless
full screen) that isn't a browser, a video player or a chat app. While one is in
focus, the engine acts sooner on background apps that use the CPU and switches to
the High performance plan; Maximum mode also keeps those apps on one core.

OptiMind's own window (a process of its own, app/desktop/window.py) is never "the
app you're using": looking at it while you play leaves the game in front.

The same thread refreshes, every few seconds, which apps play sound
(app/system/audio.py): those are never slowed down.
"""
from __future__ import annotations

import ctypes
import functools
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import psutil

from app import i18n
from app.agent import optimizer
from app.config import FROZEN, IS_WINDOWS
from app.i18n import tx
from app.models import ActionLogEntry, ActionType
from app.system import audio

logger = logging.getLogger("optimind.focus")

_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_WM_TIMER, _WM_QUIT = 0x0113, 0x0012
_AUDIO_EVERY_MS = 3000
_POLL_EVERY_MS = 250  # only if Windows refuses the hook: look at the window in front this often
_KEPT_READY = 2  # apps used just before the current one that are never compressed (Alt+Tab targets)
_OWN_PID = os.getpid()

# Shell surfaces that come to the front for a moment (the Alt+Tab switcher, Start,
# search, notifications, the taskbar, the desktop): never "the app you're using".
_SHELL_PROCESSES = {"searchhost.exe", "searchapp.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
                    "shellhost.exe", "textinputhost.exe", "lockapp.exe", "applicationframehost.exe"}
_EXPLORER_WINDOWS = {"CabinetWClass", "ExploreWClass"}  # File Explorer windows are real apps; the rest is shell
# Full screen, but not games: browsers, video players, chat and streaming apps, launchers, remote desktops,
# and work apps whose own title bar lets them cover the whole screen (maximized with the taskbar hidden).
_NOT_GAMES = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe", "vivaldi.exe",
              "vlc.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be64.exe", "potplayermini64.exe", "mpv.exe",
              "wmplayer.exe", "microsoft.media.player.exe", "video.ui.exe", "powerpnt.exe",
              "discord.exe", "slack.exe", "teams.exe", "ms-teams.exe", "zoom.exe", "spotify.exe",
              "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "mstsc.exe", "msrdc.exe",
              "code.exe", "code - insiders.exe", "cursor.exe", "devenv.exe", "idea64.exe", "pycharm64.exe",
              "notepad++.exe", "winword.exe", "excel.exe", "outlook.exe", "olk.exe", "onenote.exe",
              "windowsterminal.exe", "obs64.exe", "claude.exe", "chatgpt.exe"}
_GAME_FOLDERS = ("\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\", "\\gog games\\",
                 "\\xboxgames\\", "\\ea games\\", "\\ubisoft game launcher\\games\\", "\\riot games\\")
# Process names that are very likely games even when not installed in a game folder
# (helps on minimum-spec PCs where games may live anywhere).
_GAME_NAMES = {
    "eldenring.exe", "elden ring.exe", "cyberpunk2077.exe", "cyberpunk 2077.exe",
    "gta5.exe", "gta v.exe", "reddeadredemption2.exe", "rdr2.exe",
    "minecraft.exe", "javaw.exe",  # Minecraft (Java launcher)
    "roblox.exe", "robloxplayerbeta.exe",
    "fortnite.exe", "fortniteclient-win64-shipping.exe",
    "apex.exe", "apex legends.exe",
    "valorant.exe", "league of legends.exe", "leagueclient.exe",
    "cs2.exe", "csgo.exe", "counter-strike.exe",
    "dota 2.exe", "dota2.exe",
    "overwatch.exe", "overwatch 2.exe",
    "minecraftlauncher.exe", "starbound.exe",
    "terraria.exe", "stardewvalley.exe",
    "skyrim.exe", "skyrimse.exe", "fallout4.exe", "fallout76.exe",
    "witcher3.exe", "the witcher 3.exe",
    "dark souls iii.exe", "darksoulsiii.exe",
    "sekiro.exe", "sekiro™: shadows die twice.exe",
    "resident evil.exe", "re2.exe", "re3.exe", "re4.exe", "re8.exe",
    "monsterhunter.exe", "mhworld.exe",
    "arma.exe", "dayz.exe",
    "squad.exe", "hell let loose.exe",
    "rainbow six.exe", "r6.exe",
    "the finals.exe",
    "baldurs gate 3.exe", "bg3.exe",
    "starfield.exe",
    "call of duty.exe", "modernwarfare.exe", "warzone.exe",
    "rocket league.exe",
    "fall guys.exe",
    "among us.exe",
    "subnautica.exe",
    "hades.exe",
    "celeste.exe",
    "hollow knight.exe",
    "omicron.exe",
}
_GWL_STYLE, _WS_CAPTION = -16, 0x00C00000
_MONITOR_DEFAULTTONEAREST = 2


@dataclass
class _Focus:
    pid: int = 0
    name: str = ""
    path: str | None = None
    since: float = 0.0
    boost: str = "off"  # "boosted", "kept", "protected", "refused", "off" (Focus mode off) or "simulation" (test mode)
    game_logged: bool = False
    full_screen: bool = False  # its window filled its screen when last seen in front


_lock = threading.RLock()
_current = _Focus()
_recent: deque[str] = deque(maxlen=8)  # apps used before the current one, most recent last
_events: deque[tuple[str, float, bool]] = deque(maxlen=200)  # (app, when, settings put back) for the engine
_log: deque[ActionLogEntry] = deque(maxlen=50)  # entries for the fast loop to record
# Games already noted in the activity: pid -> (name, result). An Alt+Tab back into the same game isn't noted
# again (real log: one game refused Above normal on each of 6 returns in 17 minutes).
_games_noted: dict[int, tuple[str, str]] = {}
_options: Callable[[], tuple[bool, str, bool]] = lambda: (False, "balanced", True)  # (on, mode, test mode)
_thread: threading.Thread | None = None
_thread_id = 0


# ------------------------------------------------------------ Windows calls

@functools.lru_cache(maxsize=1)
def _user32():
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, _ENUM_PROC, wintypes.LPARAM]
    user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, _WINEVENT_PROC,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    return user32


if IS_WINDOWS:
    from ctypes import wintypes as _wt

    _ENUM_PROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
    _WINEVENT_PROC = ctypes.WINFUNCTYPE(None, _wt.HANDLE, _wt.DWORD, _wt.HWND, _wt.LONG, _wt.LONG, _wt.DWORD,
                                        _wt.DWORD)


def _window_pid(hwnd) -> int:
    from ctypes import wintypes

    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(64)
    _user32().GetClassNameW(hwnd, buffer, 64)
    return buffer.value


def _store_app_pid(frame) -> int:
    """A Store app's window frame belongs to ApplicationFrameHost; the app owns the CoreWindow inside."""
    found: list[int] = []

    @_ENUM_PROC
    def on_child(child, _):
        if _class_name(child) == "Windows.UI.Core.CoreWindow":
            found.append(_window_pid(child))
            return False
        return True

    _user32().EnumChildWindows(frame, on_child, 0)
    return found[0] if found else 0


@functools.lru_cache(maxsize=1)
def _own_programs() -> frozenset[str]:
    """OptiMind's own program files. Its window is a process of its own: OptiMind.exe --window
    (from source, the pythonw.exe next to the python.exe the agent runs on)."""
    try:
        exe = psutil.Process(_OWN_PID).exe()
    except psutil.Error:
        return frozenset()
    names = (os.path.basename(exe),) if FROZEN else ("python.exe", "pythonw.exe")
    return frozenset(os.path.normcase(os.path.join(os.path.dirname(exe), name)) for name in names)


def _is_own_window(pid: int) -> bool:
    return os.path.normcase(_exe(pid) or "") in _own_programs()


def _app_of(hwnd) -> tuple[int, str] | None:
    """The app behind the window in front, or None for shell surfaces and OptiMind itself."""
    if not hwnd:
        return None
    pid, window_class = _window_pid(hwnd), _class_name(hwnd)
    if window_class == "ApplicationFrameWindow":
        pid = _store_app_pid(hwnd) or pid
    if not pid or pid == _OWN_PID or _is_own_window(pid):
        return None
    try:
        name = psutil.Process(pid).name()
    except psutil.Error:
        return None
    lower = name.lower()
    if lower in _SHELL_PROCESSES or (lower == "explorer.exe" and window_class not in _EXPLORER_WINDOWS):
        return None
    return pid, name


def _exe(pid: int) -> str | None:
    try:
        return psutil.Process(pid).exe()
    except psutil.Error:
        return None


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4), ("rcWork", ctypes.c_long * 4),
                ("dwFlags", ctypes.c_ulong)]


def _fills_its_screen(hwnd) -> bool:
    """The window covers its whole monitor without a title bar: an app in exclusive or
    borderless full screen. (A maximized window keeps its title bar.) Windows' own "a
    full-screen app is running" state isn't enough: it stays on while you look at another
    app over a full-screen game."""
    from ctypes import wintypes

    user32 = _user32()
    if not hwnd or user32.GetWindowLongW(hwnd, _GWL_STYLE) & _WS_CAPTION == _WS_CAPTION:
        return False
    rect = wintypes.RECT()
    info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    monitor = user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or not monitor or not user32.GetMonitorInfoW(
            monitor, ctypes.byref(info)):
        return False
    left, top, right, bottom = info.rcMonitor
    slack = 2  # pixels lost to rounding on a scaled display
    return (rect.left <= left + slack and rect.top <= top + slack
            and rect.right >= right - slack and rect.bottom >= bottom - slack)


def _is_game(focus: _Focus) -> bool:
    if not focus.pid:
        return False
    path = (focus.path or "").lower()
    if any(folder in path for folder in _GAME_FOLDERS):
        return True
    # A known game process name is a strong signal even outside game folders.
    if focus.name.lower() in _GAME_NAMES:
        return True
    if focus.name.lower() in _NOT_GAMES or not IS_WINDOWS:
        return False
    hwnd = _user32().GetForegroundWindow()
    pid = _window_pid(hwnd) if hwnd else 0
    if pid and _class_name(hwnd) == "ApplicationFrameWindow":
        pid = _store_app_pid(hwnd) or pid
    if pid == focus.pid:  # its own window is in front: look at it again
        focus.full_screen = _fills_its_screen(hwnd)
    return focus.full_screen  # (OptiMind's window in front for a moment leaves it as it was)


# ------------------------------------------------------------ switching

def _give_back(name: str) -> bool:
    """Puts back what the agent held back on the app you switched to. True if there was something."""
    try:
        result, applied = optimizer.restore_app(name), True
    except (PermissionError, OSError) as e:
        result, applied = tx("error: {error}", error=i18n.message(e)), False
    if result.startswith("nothing to put back"):
        return False
    _log.append(ActionLogEntry(
        action=ActionType.RESTORE_APP, target=name, applied=applied, result=result,
        reason=tx("You switched to it (Focus mode): everything the agent was holding back is put back at once."),
    ))
    return applied


def _remember(name: str) -> None:
    for kept in [n for n in _recent if n.lower() == name.lower()]:
        _recent.remove(kept)
    _recent.append(name)


def _switch(hwnd) -> None:
    global _current
    app = _app_of(hwnd)
    if app is None:
        return
    pid, name = app
    with _lock:
        previous = _current
        if pid == previous.pid:
            return
        enabled, _, simulation = _options()
        restored = False
        if enabled and not simulation:
            restored = _give_back(name)
            if previous.boost == "boosted":
                optimizer.unboost(previous.pid)
            boost = optimizer.boost(pid)
        else:
            boost = "simulation" if enabled else "off"
        if previous.name and previous.name.lower() != name.lower():
            _remember(previous.name)
        _current = _Focus(pid, name, _exe(pid), time.time(), boost, full_screen=_fills_its_screen(hwnd))
        _events.append((name.lower(), _current.since, restored))


def _safe_switch(hwnd) -> None:
    try:
        _switch(hwnd)
    except Exception:  # never let the hook thread die
        logger.exception("Focus switch failed")


def _safe_audio() -> None:
    try:
        audio.refresh()
    except Exception:
        logger.debug("Couldn't read the audio sessions", exc_info=True)


def _on_event(hook, event, hwnd, id_object, id_child, thread, at) -> None:
    if event == _EVENT_SYSTEM_FOREGROUND:
        _safe_switch(hwnd)


def _run(ready: threading.Event) -> None:
    global _thread_id
    from ctypes import wintypes

    user32 = _user32()
    _thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    callback = _WINEVENT_PROC(_on_event)  # kept alive for as long as the hook is
    hook = user32.SetWinEventHook(_EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None, callback, 0, 0,
                                  _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
    audio_timer = user32.SetTimer(None, 0, _AUDIO_EVERY_MS, None)
    poll_timer = 0 if hook else user32.SetTimer(None, 0, _POLL_EVERY_MS, None)
    if not hook:
        logger.warning("Windows refused the focus hook; checking the window in front every %d ms instead",
                       _POLL_EVERY_MS)
    ready.set()
    try:
        _safe_switch(user32.GetForegroundWindow())  # the app already in front
        _safe_audio()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == _WM_TIMER and msg.wParam == audio_timer:
                _safe_audio()
            elif msg.message == _WM_TIMER and poll_timer and msg.wParam == poll_timer:
                _safe_switch(user32.GetForegroundWindow())
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if hook:
            user32.UnhookWinEvent(hook)
        for timer in (audio_timer, poll_timer):
            if timer:
                user32.KillTimer(None, timer)
        del callback


# ------------------------------------------------------------ public API

def start(options: Callable[[], tuple[bool, str, bool]]) -> None:
    """Starts watching which app is in front. `options` returns (Focus mode on, its
    mode, test mode on), read at every switch."""
    global _options, _thread
    _options = options
    if not IS_WINDOWS or (_thread is not None and _thread.is_alive()):
        return
    ready = threading.Event()
    _thread = threading.Thread(target=_run, args=(ready,), name="optimind-focus", daemon=True)
    _thread.start()
    ready.wait(2.0)


def stop() -> None:
    """Stops watching (OptiMind quitting), so nothing is boosted after everything is put back."""
    global _thread
    thread = _thread
    if thread is not None and thread.is_alive() and _thread_id:
        _user32().PostThreadMessageW(_thread_id, _WM_QUIT, 0, 0)
        thread.join(2.0)
    _thread = None


def release() -> None:
    """Focus mode switched off: the app in front gets its own priority back."""
    with _lock:
        if _current.boost == "boosted":
            optimizer.unboost(_current.pid)
        enabled, _, simulation = _options()
        _current.boost = "simulation" if enabled and simulation else "off"


def refresh() -> None:
    """Focus mode switched on: the app in front is treated as just switched to, so it
    gets its settings back and its boost right away."""
    global _current
    if _thread is None or not _thread.is_alive():
        return
    with _lock:
        if _current.boost == "boosted":
            optimizer.unboost(_current.pid)
        _current = _Focus()
    _safe_switch(_user32().GetForegroundWindow())


def current_app() -> tuple[int, str, str | None] | None:
    """The app in front as Focus mode sees it: (pid, exe name, exe path), or None."""
    if _thread is None or not _thread.is_alive():
        return None
    with _lock:
        return (_current.pid, _current.name, _current.path) if _current.pid else None


def recent_apps() -> list[str]:
    """The apps used just before the one in front (most recent first): kept ready for Alt+Tab."""
    with _lock:
        current = _current.name.lower()
        return [n for n in reversed(_recent) if n.lower() != current][:_KEPT_READY]


def snapshot_info() -> tuple[int | None, bool, list[str]]:
    """(the app in front, whether it is a game, the apps kept ready for Alt+Tab) for the fast
    loop's snapshot, read together: read apart, a switch in between paired one app with the
    other's game state ("a game is in focus (Discord.exe)"). The app is None when Focus mode
    isn't watching. A game coming into focus is noted once in the activity log, and an
    Alt+Tab back into it isn't noted again unless the result changed."""
    with _lock:
        current = _current
        pid = current.pid or None if _thread is not None and _thread.is_alive() else None
        game = _is_game(current)
        if game and not current.game_logged:
            current.game_logged = True
            enabled, mode, _ = _options()
            result = {"boosted": tx("Above normal priority (checked)"),
                      "kept": tx("keeps its own priority (not Normal)"),
                      "protected": tx("left alone (protected, or another account's)"),
                      "refused": tx("Windows refused the priority change (protected game)"),
                      "simulation": tx("simulated (dry_run)")}.get(current.boost, current.boost)
            if enabled and _games_noted.get(current.pid) != (current.name, result):
                _games_noted[current.pid] = (current.name, result)
                while len(_games_noted) > 50:
                    _games_noted.pop(next(iter(_games_noted)))  # the oldest
                _log.append(ActionLogEntry(
                    action=ActionType.FOCUS_BOOST, target=current.name,
                    reason=tx("A game is in focus: the agent acts on background apps using the CPU after 10 s and keeps "
                              "them on one core (Maximum mode), and switches to the High performance plan while you play.")
                    if mode == "maximum" else
                    tx("A game is in focus: the agent acts on background apps using the CPU after 10 s, and switches "
                       "to the High performance plan while you play."),
                    applied=current.boost == "boosted", result=result,
                ))
        return pid, game, recent_apps()


def drain_events() -> list[tuple[str, float, bool]]:
    """Focus switches since the last call: (app, when, whether its settings were put back)."""
    out = []
    while _events:
        out.append(_events.popleft())
    return out


def drain_log() -> list[ActionLogEntry]:
    out = []
    while _log:
        out.append(_log.popleft())
    return out


def mode_in_effect() -> str:
    """"off", "balanced" or "maximum": Focus mode as it works right now (game sessions note it)."""
    enabled, mode, _ = _options()
    return mode if enabled else "off"


def status() -> dict:
    """Focus mode as the dashboard shows it."""
    enabled, mode, _ = _options()
    with _lock:
        current = _current
        return {
            "enabled": enabled,
            "mode": mode,
            "watching": _thread is not None and _thread.is_alive(),
            "app": current.name or None,
            "pid": current.pid or None,
            "since": datetime.fromtimestamp(current.since, timezone.utc).isoformat() if current.since else None,
            "boost": current.boost,
            "game": _is_game(current),
            "recent": recent_apps(),
        }
