"""
OptiMind's window (`OptiMind.exe --window`): the dashboard in a native Windows
11 window, drawn by WebView2, the web engine built into Windows 11. There's no
browser around it: no address bar, no tabs, its own taskbar button.

It's a process of its own, started by the agent when you open OptiMind and
gone when you close the window, so the engine takes no memory while the agent
works in the notification area. It only shows the agent's own pages: websites
open in your usual browser, and the makers' driver apps from your Start menu,
with your normal rights. It remembers the window's
size and place (data/window.json), and it's always dark: OptiMind's black, the
title bar included.
"""
from __future__ import annotations

import ctypes
import html
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes

from app import i18n
from app.config import BASE_DIR, DATA_DIR, FROZEN, WEBVIEW_DIR, settings
from app.desktop import APP_ID, APP_NAME
from app.i18n import tx
from app.service import access

logger = logging.getLogger("optimind.window")

STATE_PATH = DATA_DIR / "window.json"
ICON_PATH = BASE_DIR / "app" / "static" / "optimind.ico"
BACKGROUND = "#000000"  # the page's black: no white flash, and the same black title bar
DEFAULT_SIZE = (1280, 820)
MIN_SIZE = (720, 560)
_OUTSIDE = ("https://", "http://", "ms-settings:")  # what may open outside the window
_STEAM_GAME_PAGE = re.compile(r"^steam://nav/games/details/\d{1,10}$")  # a game's page in Steam (Games page), only

# DwmSetWindowAttribute
_DARK_MODE = 20
_CAPTION_COLOR = 35
_BACKDROP = 38
_NO_BACKDROP = 1


def _load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def open_outside(url: str) -> bool:
    """Opens a website (or a page of Windows Settings, or a game's page in Steam) the way a
    click in Explorer does: in your usual browser, with your normal rights even while
    OptiMind runs as administrator."""
    if not (url.startswith(_OUTSIDE) or _STEAM_GAME_PAGE.match(url)):
        return False
    subprocess.Popen(["explorer.exe", url])
    return True


def open_maker_app(app_id: str) -> bool:
    """Opens one of the makers' own driver apps from your Start menu, the way a click there does,
    with your normal rights. Only an app the agent lists for this PC right now (the Drivers
    page's "The makers' own apps"): no other program can be started from the page."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{settings.optimind_api_port}/api/drivers/tools",
                                    timeout=10) as r:
            listed = {t.get("app_id") for t in json.load(r).get("tools", []) if t.get("installed")}
    except (OSError, ValueError, AttributeError):
        return False
    if not app_id or app_id not in listed:
        return False
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
    return True


class _OutsideBrowser(webbrowser.BaseBrowser):
    """Where links that open a new window go (pywebview hands them to `webbrowser`)."""

    def open(self, url, new=0, autoraise=True):
        return open_outside(url)


def _colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | g << 8 | b << 16


def style_title_bar(hwnd: int) -> None:
    """Title bar in the page's black (one surface from top to bottom), with light caption buttons."""
    dwm = ctypes.WinDLL("dwmapi")
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    for attribute, value in ((_DARK_MODE, 1), (_BACKDROP, _NO_BACKDROP), (_CAPTION_COLOR, _colorref(BACKGROUND))):
        v = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v), ctypes.sizeof(v))


class DesktopApi:
    """What the page may ask of its window: window.pywebview.api.<name>() in JavaScript."""

    def open_link(self, url: str) -> bool:
        return open_outside(str(url))

    def open_app(self, app_id: str) -> bool:
        return open_maker_app(str(app_id))

    def key(self) -> str:
        """The secret the agent gave this window when it started it: the page sends it with
        every change (app/service/access.py). Only OptiMind's own page loads in this window."""
        return os.environ.get(access.ENVIRONMENT, "")


def _reachable(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url + "api/app", timeout=2):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


def _status_page(title: str, text: str, busy: bool) -> str:
    """The small page shown while the agent starts (or if it can't be reached), in the page's colors
    and OptiMind's language."""
    title, text = html.escape(i18n.shown(title)), html.escape(i18n.shown(text))
    ring = ('<svg width="32" height="32" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="13" '
            'fill="none" stroke="#3d7bff" stroke-width="3" stroke-linecap="round" stroke-dasharray="20 62">'
            '<animateTransform attributeName="transform" type="rotate" from="0 16 16" to="360 16 16" dur="1s" '
            'repeatCount="indefinite"/></circle></svg>') if busy else ""
    return f"""<!doctype html><html lang="{i18n.current()}"><head><meta charset="utf-8"><style>
html,body{{height:100%;margin:0;background:{BACKGROUND};color:#f2f4f7;font:14px/20px "Segoe UI Variable Text","Segoe UI",sans-serif;cursor:default;user-select:none}}
main{{height:100%;display:grid;place-content:center;justify-items:center;gap:14px;text-align:center}}
h1{{font:600 22px/28px Bahnschrift,"Segoe UI Variable Display","Segoe UI",sans-serif;letter-spacing:.6px;text-transform:uppercase;margin:0}}
p{{margin:0;color:#a1a8b3;max-width:420px}}
</style></head><body><main>
<svg width="56" height="56" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="16" fill="#2f60c7"/>
<polyline points="12,34 24,34 30,20 37,46 43,30 52,30" fill="none" stroke="#fff" stroke-width="5.6" stroke-linecap="round"
stroke-linejoin="round"/></svg>
<h1>{title}</h1><p>{text}</p>{ring}</main></body></html>"""


def _restore_geometry(state: dict) -> dict:
    """Size and place from last time, if it's still on one of your screens."""
    import webview

    width, height = state.get("width"), state.get("height")
    x, y = state.get("x"), state.get("y")
    geometry = {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1], "maximized": bool(state.get("maximized"))}
    if isinstance(width, int) and isinstance(height, int):
        geometry.update(width=max(width, MIN_SIZE[0]), height=max(height, MIN_SIZE[1]))
    if isinstance(x, int) and isinstance(y, int):
        try:
            visible = any(s.x <= x + 80 <= s.x + s.width and s.y <= y + 20 <= s.y + s.height for s in webview.screens)
        except Exception:
            visible = False
        if visible:
            geometry.update(x=x, y=y)
    return geometry


def main(page: str | None = None) -> int:
    import webview

    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    webbrowser.register("optimind-outside", None, _OutsideBrowser(), preferred=True)

    state = _load_state()
    state.pop("theme", None)  # the Light/Dark choice of OptiMind before 2.0
    base = f"http://127.0.0.1:{settings.optimind_api_port}/"
    url = base + "?app=1" + (f"#{page}" if page else "")

    ready = _reachable(base, 0.5)
    geometry = _restore_geometry(state)
    window = webview.create_window(
        APP_NAME, url=url if ready else None,
        html=None if ready else _status_page(tx("Starting OptiMind…"), tx("The agent is getting ready."), True),
        js_api=DesktopApi(), min_size=MIN_SIZE, background_color=BACKGROUND, text_select=True, **geometry)
    normal = {k: state.get(k) for k in ("x", "y", "width", "height")}
    flags = {"maximized": geometry["maximized"], "minimized": False}

    def on_shown():
        form = window.native
        style_title_bar(form.Handle.ToInt32())

        def keep_inside(sender, args):  # a link without a new window: websites open outside
            target = str(args.Uri)
            if not target.startswith((base, "data:", "about:")):
                args.Cancel = True
                open_outside(target)

        form.webview.NavigationStarting += keep_inside
        from Microsoft.Win32 import SystemEvents

        # Windows switched light/dark: pywebview restyles the title bar its way first, then it's black again
        SystemEvents.UserPreferenceChanged += lambda s, e: style_title_bar(form.Handle.ToInt32())

    def on_moved(x, y):
        if not flags["maximized"] and not flags["minimized"] and x > -30000:
            normal.update(x=x, y=y)

    def on_resized(width, height):
        if not flags["maximized"] and not flags["minimized"] and width >= MIN_SIZE[0] // 2:
            normal.update(width=width, height=height)

    def tell_page(shown: str) -> None:
        """The page pauses its updates while the window is minimized (nothing to see, and the
        agent isn't asked every 2 seconds). On a thread of its own: evaluate_js waits for the page."""
        def send():
            try:
                window.evaluate_js(f"window.dispatchEvent(new CustomEvent('optimind-window', {{detail: '{shown}'}}))")
            except Exception:
                logger.debug("Couldn't tell the page the window is %s", shown, exc_info=True)
        threading.Thread(target=send, daemon=True, name="window-state").start()

    def set_flags(maximized: bool, minimized: bool):
        was_minimized = flags["minimized"]
        flags.update(maximized=maximized, minimized=minimized)
        if minimized != was_minimized:
            tell_page("minimized" if minimized else "shown")

    window.events.shown += on_shown
    window.events.moved += on_moved
    window.events.resized += on_resized
    window.events.maximized += lambda: set_flags(True, False)
    window.events.minimized += lambda: set_flags(flags["maximized"], True)
    window.events.restored += lambda: set_flags(False, False)

    def load_when_ready():
        if ready:
            return
        if _reachable(base, 60):
            window.load_url(url)
        else:
            window.load_html(_status_page(tx("OptiMind isn't running"),
                                          tx("Start it again from the Start menu. If it keeps happening, the log "
                                             "files in Settings → About say why."), False))

    debug = os.environ.get("OPTIMIND_WINDOW_DEBUG", "").lower() in ("1", "true")
    icon = str(ICON_PATH) if not FROZEN and ICON_PATH.exists() else None  # the installed app uses its own icon
    WEBVIEW_DIR.mkdir(parents=True, exist_ok=True)
    webview.start(load_when_ready, private_mode=False, storage_path=str(WEBVIEW_DIR), icon=icon, debug=debug)

    state.update({k: v for k, v in normal.items() if isinstance(v, int)}, maximized=flags["maximized"])
    _save_state(state)
    return 0
