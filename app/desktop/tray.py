"""
The notification-area (tray) icon. Runs in its own thread, since
pystray.Icon.run() is blocking.

Menu:
  - Open OptiMind -> its window (also a click on the icon)
  - Focus mode    -> Off / Balanced / Maximum
  - Quit OptiMind -> gives apps their own settings back and ends OptiMind
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from app import __version__, i18n
from app.desktop import APP_NAME
from app.desktop.icon import draw_mark
from app.i18n import tx
from app.service.state import app_state
from app.system.admin_rights import is_elevated

if TYPE_CHECKING:
    import pystray

    from app.desktop.shell import Shell


def _set_focus(mode: str | None):
    """Menu handler: None = Focus mode off (its Balanced/Maximum choice is kept)."""
    def handler(icon, item) -> None:
        from app.service.orchestrator import set_focus

        set_focus(mode is not None, mode or app_state.current_policy.focus_mode)
    return handler


def _focus_is(mode: str | None):
    def checked(item) -> bool:
        policy = app_state.current_policy
        return not policy.enable_focus_mode if mode is None else policy.enable_focus_mode and policy.focus_mode == mode
    return checked


def _text(text):
    """A menu item's text, in OptiMind's language when the menu is made (again when it changes)."""
    return lambda item: i18n.shown(text)


def _title() -> str:
    if is_elevated():
        return i18n.shown(tx("{app} {version} (administrator)", app=APP_NAME, version=__version__))
    return f"{APP_NAME} {__version__}"


def start(shell: Shell) -> pystray.Icon:
    import pystray

    focus_menu = pystray.Menu(
        pystray.MenuItem(_text(tx("Off")), _set_focus(None), checked=_focus_is(None), radio=True),
        pystray.MenuItem(_text(tx("Balanced")), _set_focus("balanced"), checked=_focus_is("balanced"), radio=True),
        pystray.MenuItem(_text(tx("Maximum (games: background on one core)")), _set_focus("maximum"),
                         checked=_focus_is("maximum"), radio=True),
    )
    menu = pystray.Menu(
        pystray.MenuItem(_text(tx("Open OptiMind")), lambda icon, item: shell.open_window(), default=True),
        pystray.MenuItem(_text(tx("Focus mode")), focus_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_text(tx("Quit OptiMind")), lambda icon, item: shell.quit()),
    )
    icon = pystray.Icon("optimind-ai", draw_mark(64), _title(), menu)

    def relabel() -> None:  # another language chosen in Settings
        icon.title = _title()
        icon.update_menu()

    i18n.on_change(relabel)
    threading.Thread(target=icon.run, daemon=True, name="tray").start()
    return icon
