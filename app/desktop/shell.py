"""
The agent's side of the desktop app: the notification-area (tray) icon,
OptiMind's window, quitting, and Windows signing you out.

The window is a separate process (`OptiMind.exe --window`, app/desktop/window.py),
started when you open OptiMind and ended when you close it, so the browser
engine that draws it takes no memory while the agent works in the background.
The agent, its local API and the tray icon keep running until you choose Quit.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from app import i18n
from app.config import BASE_DIR, DATA_DIR, FROZEN, IS_WINDOWS, PROGRAM
from app.i18n import tx
from app.service import access
from app.system import admin_rights

logger = logging.getLogger("optimind.shell")

_NOTICES_PATH = DATA_DIR / "notices.json"  # one-time tips already shown
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ASFW_ANY = -1
_SW_RESTORE = 9
_GW_OWNER = 4

# The running desktop app (None under plain uvicorn and in the tests: no window, no tray).
current: Shell | None = None


def window_command(page: str | None = None) -> list[str]:
    """The command that opens OptiMind's window."""
    if FROZEN:
        command = [PROGRAM, "--window"]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = [str(pythonw if pythonw.exists() else sys.executable), str(BASE_DIR / "run.py"), "--window"]
    return command + ([f"--page={page}"] if page else [])


def _first_time(notice: str) -> bool:
    """True the first time `notice` is asked about (remembered in data/notices.json)."""
    try:
        seen = json.loads(_NOTICES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = []
    if notice in seen:
        return False
    try:
        _NOTICES_PATH.write_text(json.dumps(seen + [notice]), encoding="utf-8")
    except OSError:
        pass
    return True


def _top_window_of(pid: int) -> int | None:
    """The visible main window of process `pid` (or of a process it started: from source,
    Python's launcher starts the real interpreter)."""
    import psutil

    user32 = ctypes.windll.user32
    try:
        pids = {pid} | {p.pid for p in psutil.Process(pid).children(recursive=True)}
    except psutil.Error:
        pids = {pid}
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def check(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, _GW_OWNER):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(check, 0)
    return found[0] if found else None


class _StandardRightsProcess:
    """A process started with the signed-in user's normal rights from OptiMind running as
    administrator: it gets a copy of Explorer's token (the desktop shell always runs with
    normal rights). Microsoft advises against running WebView2 elevated, and this way the
    window's browser engine never does. Like subprocess.Popen: pid, poll(), wait(), terminate()."""

    _TOKEN_ACCESS = 0x0001 | 0x0002 | 0x0008 | 0x0080 | 0x0100  # assign primary, duplicate, query, adjust default/session
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.CreateProcessWithTokenW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(self._StartupInfo), ctypes.POINTER(self._ProcessInfo)]

        def fail(what: str):
            return OSError(f"{what} failed ({ctypes.FormatError(ctypes.get_last_error()).strip()})")

        shell_pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(user32.GetShellWindow(), ctypes.byref(shell_pid)):
            raise fail("finding the desktop shell")
        shell = kernel32.OpenProcess(0x1000, False, shell_pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not shell:
            raise fail("opening the desktop shell")
        token, primary = wintypes.HANDLE(), wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(shell, 0x0002, ctypes.byref(token)):  # TOKEN_DUPLICATE
                raise fail("reading the desktop shell's rights")
            if not advapi32.DuplicateTokenEx(token, self._TOKEN_ACCESS, None, 2, 1, ctypes.byref(primary)):
                raise fail("copying them")  # SecurityImpersonation, TokenPrimary
            environment = "".join(f"{k}={v}\0" for k, v in sorted((env or os.environ).items(),
                                                                   key=lambda kv: kv[0].upper()))
            env_block = ctypes.create_unicode_buffer(environment + "\0")
            startup = self._StartupInfo(cb=ctypes.sizeof(self._StartupInfo))
            info = self._ProcessInfo()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            if not advapi32.CreateProcessWithTokenW(primary, 0, None, command_line,
                                                    _NO_WINDOW | self._CREATE_UNICODE_ENVIRONMENT, env_block,
                                                    os.path.dirname(command[0]) or None, ctypes.byref(startup),
                                                    ctypes.byref(info)):
                raise fail("starting the window with normal rights")
        finally:
            for handle in (shell, token, primary):
                if handle:
                    kernel32.CloseHandle(handle)
        kernel32.CloseHandle(info.hThread)
        self._handle = info.hProcess
        self.pid = info.dwProcessId

    def poll(self) -> int | None:
        if self._kernel32.WaitForSingleObject(self._handle, 0) != 0:  # WAIT_OBJECT_0: it has ended
            return None
        code = wintypes.DWORD()
        self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        return code.value

    def wait(self) -> int:
        self._kernel32.WaitForSingleObject(self._handle, 0xFFFFFFFF)
        return self.poll()

    def terminate(self) -> None:
        self._kernel32.TerminateProcess(self._handle, 1)


def _start_window_process(command: list[str]):
    """The window with your normal rights, also when OptiMind runs as administrator (see
    _StandardRightsProcess); if that can't be done, as OptiMind itself runs. Either way it
    gets the secret its page needs to change things (app/service/access.py)."""
    env = access.window_environment()
    if IS_WINDOWS and admin_rights.is_elevated():
        try:
            return _StandardRightsProcess(command, env)
        except OSError:
            logger.warning("Starting the window with administrator rights instead", exc_info=True)
    return subprocess.Popen(command, creationflags=_NO_WINDOW, close_fds=True, env=env)


class Shell:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window: subprocess.Popen | _StandardRightsProcess | None = None
        self._tray = None
        self._quitting = False

    def start(self, open_window: bool) -> None:
        from app.desktop import tray

        try:
            self._tray = tray.start(self)
        except Exception:
            logger.exception("The tray icon could not start; continuing without it")
        if IS_WINDOWS:
            threading.Thread(target=_watch_session_end, daemon=True, name="session-end").start()
        if open_window:
            self.open_window()

    @property
    def window_open(self) -> bool:
        return self._window is not None and self._window.poll() is None

    def open_window(self, page: str | None = None) -> None:
        """Opens OptiMind's window, or brings it to the front if it's already open."""
        with self._lock:
            if self._quitting:
                return
            if self.window_open:
                hwnd = _top_window_of(self._window.pid) if IS_WINDOWS else None
                if hwnd:
                    user32 = ctypes.windll.user32
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, _SW_RESTORE)
                    user32.SetForegroundWindow(hwnd)
                return
            if IS_WINDOWS:
                ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)  # its first window may take the front
            try:
                self._window = _start_window_process(window_command(page))
            except OSError:
                logger.exception("Couldn't open OptiMind's window")
                return
            threading.Thread(target=self._watch_window, args=(self._window,), daemon=True, name="window").start()

    def _watch_window(self, window: subprocess.Popen) -> None:
        window.wait()
        if not self._quitting and _first_time("closed-to-tray"):
            self.notify(tx("OptiMind is still running"),
                        tx("It keeps your PC responsive from the notification area. Open it or quit from its icon "
                           "there."))

    def notify(self, title: str, message: str) -> None:
        try:
            if self._tray is not None:
                self._tray.notify(i18n.shown(message), i18n.shown(title))
        except Exception:
            pass

    def quit(self) -> None:
        """Gives apps their own settings back and ends OptiMind, window included."""
        with self._lock:
            self._quitting = True
            window = self._window
        if window is not None and window.poll() is None:
            window.terminate()
        try:
            if self._tray is not None:
                self._tray.stop()
            from app.agent import focus, games
            from app.agent.optimizer import restore_all

            focus.stop()
            games.stop()  # the game session so far is saved, PresentMon stopped
            restore_all()  # apps the agent was holding back get their own settings back
        finally:
            os._exit(0)  # shuts everything down (uvicorn included) immediately

    def quit_soon(self) -> None:
        """quit() in a moment, so the API call that asked for it gets its answer first."""
        threading.Timer(0.3, self.quit).start()

    def restart_as_admin(self) -> bool:
        """Shows the Windows prompt; on Yes, an OptiMind with administrator rights takes over
        (it waits for this one to quit). False if it's not possible or you clicked No."""
        if not admin_rights.can_ask_for_admin():
            return False
        args = [] if self.window_open else ["--background"]
        if not admin_rights.restart_as_admin(args):
            return False
        self.quit_soon()
        return True


def _watch_session_end() -> None:
    """Signing out or shutting down ends OptiMind without Quit, so first give the apps the
    agent was holding back their own settings again (Windows allows a few seconds for this).
    Windows tells every top-level window, so this thread keeps an invisible one."""
    import win32api
    import win32con
    import win32gui

    def on_message(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_QUERYENDSESSION:
            return True
        if msg == win32con.WM_ENDSESSION and wparam:
            from app.agent import focus, games
            from app.agent.optimizer import restore_all

            focus.stop()
            games.stop()
            restore_all()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    try:
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = on_message
        wc.lpszClassName = "OptiMindAI.SessionWatcher"
        wc.hInstance = win32api.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        win32gui.CreateWindow(wc.lpszClassName, "OptiMind AI", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        win32gui.PumpMessages()
    except Exception:
        logger.exception("Can't watch for sign-out; apps get their settings back at OptiMind's next start instead")
