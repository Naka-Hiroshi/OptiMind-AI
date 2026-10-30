"""
OptiMind's own confirmation for the riskiest changes (turning off a security feature, installing
drivers from a backup folder): a Windows dialog the agent shows itself, on top of every window,
with No as the default. The agent runs with administrator rights, and Windows doesn't let
programs with fewer rights click or type into its windows, so only you can answer it. The page
asks first; this is the agent making sure. No answer within 90 seconds counts as No.

OPTIMIND_NO_DIALOGS=true (the tests set it) answers No without showing anything: it can only
refuse, never confirm.
"""
from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes

from app import i18n
from app.config import IS_WINDOWS
from app.desktop import APP_NAME
from app.i18n import tx

logger = logging.getLogger("optimind.confirm")

TIMEOUT_SECONDS = 90
_MB_YESNO, _MB_ICONWARNING, _MB_DEFBUTTON2 = 0x4, 0x30, 0x100  # No is the default button
_MB_SETFOREGROUND, _MB_TOPMOST = 0x10000, 0x40000
_IDYES = 6


def _user32():
    return ctypes.WinDLL("user32")


def ask(question: str, details: str) -> bool:
    """True only if you click Yes in OptiMind's own dialog."""
    if not IS_WINDOWS or os.environ.get("OPTIMIND_NO_DIALOGS", "").lower() in ("1", "true"):
        logger.info("Not confirmed (no dialog here): %s", question)
        return False
    user32 = _user32()
    flags = _MB_YESNO | _MB_ICONWARNING | _MB_DEFBUTTON2 | _MB_SETFOREGROUND | _MB_TOPMOST
    text = "\n\n".join([i18n.shown(question), i18n.shown(details),
                         i18n.shown(tx("OptiMind asks this itself, so that only you can confirm it."))])
    caption = i18n.shown(tx("{app}: please confirm", app=APP_NAME))
    # Not in Windows' documentation, but in every Windows since XP: a message box that gives up
    timed = getattr(user32, "MessageBoxTimeoutW", None)
    if timed is not None:
        timed.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT, wintypes.WORD,
                          wintypes.DWORD]
        answer = timed(None, text, caption, flags, 0, TIMEOUT_SECONDS * 1000)
    else:
        answer = user32.MessageBoxW(None, text, caption, flags)
    logger.info("%s: %s", question, "confirmed" if answer == _IDYES else "not confirmed")
    return answer == _IDYES
