"""
Administrator rights (Windows UAC).

OptiMind works without them, but these things need them:
  - emptying the memory standby list and merging identical memory pages;
  - reading how much Windows compresses memory;
  - managing your own apps that run as administrator;
  - turning startup apps for all users on and off.

With OPTIMIND_RUN_AS_ADMIN=true (the default), OptiMind asks for them when
it starts: Windows shows its usual "Do you want to allow this app to make
changes to your device?" prompt and OptiMind starts again with them. Click
No and it keeps running without them (Settings → Restart as administrator
asks again). Started with Windows, it gets them without a prompt
(app/desktop/autostart.py).

Even as administrator, OptiMind leaves Windows services, other people's
programs and protected processes alone (see optimizer.access_problem).
"""
from __future__ import annotations

import ctypes
import functools
import os
import subprocess
import sys

from app.config import FROZEN, IS_WINDOWS, PROGRAM

RELAUNCHED_FLAG = "--relaunched-as-admin"  # marks the copy started by the prompt, so it never asks again
_TOKEN_ELEVATION_TYPE_LIMITED = 3  # an administrator account running with standard rights
_SW_SHOWNORMAL = 1


@functools.lru_cache(maxsize=1)
def is_elevated() -> bool:
    """Is OptiMind itself running as administrator?"""
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def enable_privilege(name: str) -> None:
    """Switches on a privilege OptiMind's token holds but keeps off by default (an
    administrator has e.g. SeProfileSingleProcessPrivilege, which Windows' memory
    lists and memory compression statistics ask for)."""
    import win32api
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_ADJUST_PRIVILEGES | win32security.TOKEN_QUERY
    )
    privilege = win32security.LookupPrivilegeValue(None, name)
    win32security.AdjustTokenPrivileges(token, False, [(privilege, win32security.SE_PRIVILEGE_ENABLED)])


def can_ask_for_admin() -> bool:
    """True when this Windows account is an administrator running with
    standard rights, so a Yes on the prompt elevates this same account.
    (A standard account would be asked for someone else's password, and
    OptiMind would then run as that other person, so it doesn't ask.)"""
    if not IS_WINDOWS or is_elevated():
        return False
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        elevation = win32security.GetTokenInformation(token, win32security.TokenElevationType)
        return elevation == _TOKEN_ELEVATION_TYPE_LIMITED
    except Exception:
        return False


def restart_as_admin(args: list[str]) -> bool:
    """Shows the Windows prompt and, on Yes, starts OptiMind again with `args` and
    administrator rights (from source: in a new console window). The new copy waits
    for this one to finish. False if refused or failed."""
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32")
    shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    script = [] if FROZEN else [os.path.abspath(sys.argv[0])]  # the installed app is the program itself
    params = subprocess.list2cmdline([*script, *(a for a in args if a != RELAUNCHED_FLAG), RELAUNCHED_FLAG])
    result = shell32.ShellExecuteW(None, "runas", PROGRAM, params, os.getcwd(), _SW_SHOWNORMAL)
    return (result or 0) > 32  # 32 or less is an error code (5 = you clicked No)
