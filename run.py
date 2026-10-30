"""
OptiMind AI's entry point: `python run.py` from the source folder, and the
installed app's OptiMind.exe (this same file, compiled with Nuitka).

Without options it starts OptiMind: the agent with its local API, the icon in
the notification area, and OptiMind's window. First it asks Windows for
administrator rights (the usual "allow this app to make changes" prompt;
click No and it runs without them, or set OPTIMIND_RUN_AS_ADMIN=false to skip
it). If OptiMind is already running, it opens that one's window instead,
without a prompt.

  --background            start in the notification area, without the window (sign-in)
  --window                only OptiMind's window (the agent starts it)
  --quit                  ask the running OptiMind to quit (the installer uses it)
  --register-autostart    start with Windows on (the installer uses it; needs admin)
  --unregister-autostart  start with Windows off
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler

# From source, run from the project folder whichever folder started OptiMind (an
# administrator window starts in C:\Windows\System32).
if "__compiled__" not in globals() and not getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import __version__, i18n  # noqa: E402
from app.config import FROZEN, IS_WINDOWS, LOG_DIR, settings  # noqa: E402
from app.desktop import APP_NAME, instance  # noqa: E402
from app.i18n import tx  # noqa: E402
from app.system import admin_rights  # noqa: E402

_MB_ICONERROR = 0x10


def _option(name: str) -> str | None:
    """The value of --name=value, or "" for a bare --name; None when absent."""
    for arg in sys.argv[1:]:
        if arg == f"--{name}":
            return ""
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return None


def _no_console() -> bool:
    return FROZEN or sys.stdout is None  # the installed app, or pythonw (started with Windows)


def _log_to_file(name: str) -> None:
    """Without a console, the log goes to %LOCALAPPDATA%\\OptiMind\\logs (Settings → About opens it)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # Anything a library prints goes to the same folder instead of nowhere (started afresh past 1 MB).
    output = LOG_DIR / f"{name}-output.log"
    mode = "w" if output.exists() and output.stat().st_size > 1_000_000 else "a"
    sys.stdout = sys.stderr = open(output, mode, encoding="utf-8", buffering=1)  # noqa: SIM115


def _show_error(message: str) -> None:
    message = i18n.shown(message)
    if _no_console() and IS_WINDOWS:
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, _MB_ICONERROR)
    else:
        print(f"[error] {message}", file=sys.stderr)


def _restarted_as_admin() -> bool:
    """Asks for administrator rights if OptiMind doesn't have them yet.
    True when a copy with them has started (this one then exits)."""
    if not settings.optimind_run_as_admin or not admin_rights.can_ask_for_admin():
        return False
    if sys.gettrace() is not None or "debugpy" in sys.modules:
        print(
            "[info] Debugger attached, so running without administrator rights (it can't "
            "follow OptiMind into a new window). Start VS Code as administrator to debug with them.",
            file=sys.stderr,
        )
        return False
    print("Asking Windows for administrator rights; OptiMind will continue in a new window...")
    if admin_rights.restart_as_admin(sys.argv[1:]):
        return True
    logging.getLogger("optimind").warning(
        "Running without administrator rights (the prompt was declined): your apps that run as administrator, "
        "startup apps for all users, the standby purge and page combining are skipped.")
    return False


def _restore_on_console_close() -> None:
    """From source: closing the console window ends OptiMind without a normal shutdown, so
    first give the apps the agent was holding back their own settings again."""
    try:
        import win32api
    except ImportError:
        return

    def handler(ctrl_type: int) -> bool:
        if ctrl_type == 2:  # CTRL_CLOSE_EVENT (signing out is handled by app/desktop/shell.py)
            from app.agent import focus, games
            from app.agent.optimizer import restore_all

            focus.stop()
            games.stop()  # the game session so far is saved, PresentMon stopped
            restore_all()
        return False  # then let Windows and Python carry on as usual

    try:
        win32api.SetConsoleCtrlHandler(handler, True)
    except Exception:
        pass  # no console window


def _serve(background: bool, replacing: bool) -> int:
    import uvicorn

    claim = instance.claim(wait_seconds=30 if replacing else 0)
    if claim is None:  # started twice at the same moment
        instance.ask("window")
        return 0

    from app.desktop import shell
    from app.main import app

    if not IS_WINDOWS:
        logging.getLogger("optimind").warning(
            "Not on Windows: the agent only logs what it would do, since its actions use Windows APIs.")
    elif not _no_console():
        _restore_on_console_close()
    shell.current = shell.Shell()
    shell.current.start(open_window=not background)

    print(f"{APP_NAME} {__version__}: running. Its window opens from the icon in the notification area; "
          "quit from there too.")
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=settings.optimind_api_port, log_level="info", ws="none",  # no WebSockets used
        access_log=not _no_console(),  # in a log file, 3 requests every 2 seconds would bury everything else
        log_config=None if _no_console() else uvicorn.config.LOGGING_CONFIG,
    ))
    try:
        server.run()
    except SystemExit:  # uvicorn exits when it can't open the port
        pass
    if not server.started:
        _show_error(tx("{app} couldn't start: port {port} is in use by another program. Set another "
                       "OPTIMIND_API_PORT in the settings file and start OptiMind again.", app=APP_NAME,
                       port=settings.optimind_api_port))
    shell.current.quit()  # also closes the window and the tray icon (e.g. after Ctrl+C in the console)
    return 0


def main() -> int:
    if _option("window") is not None:
        if _no_console():
            _log_to_file("window")
        from app.desktop import window

        return window.main(_option("page") or None)

    if _option("quit") is not None:
        return 0 if instance.quit_running() else 1
    if _option("register-autostart") is not None or _option("unregister-autostart") is not None:
        from app.desktop import autostart

        try:
            autostart.enable() if _option("register-autostart") is not None else autostart.disable()
            return 0
        except autostart.AutostartError as e:
            _show_error(tx("Start with Windows: {error}", error=i18n.message(e)))
            return 1

    background = _option("background") is not None
    replacing = admin_rights.RELAUNCHED_FLAG in sys.argv  # the copy started by the prompt
    if not replacing and instance.is_running():
        if not background:
            instance.ask("window")  # no second agent, no prompt: the running one opens its window
        return 0
    if not replacing and _restarted_as_admin():
        return 0
    if _no_console():
        _log_to_file("optimind")
    elif replacing and IS_WINDOWS:
        ctypes.windll.kernel32.SetConsoleTitleW(f"{APP_NAME} {__version__} (administrator)")
    try:
        return _serve(background, replacing)
    except SystemExit:
        raise
    except Exception:
        logging.getLogger("optimind").exception("OptiMind stopped because of an error")
        _show_error(tx("{app} stopped because of an error:\n\n{details}", app=APP_NAME,
                       details=traceback.format_exc(limit=3)))
        if not _no_console() and replacing:
            input("\nPress Enter to close this window.")  # the prompt's console window closes on exit
        return 1


if __name__ == "__main__":
    sys.exit(main())
