"""
Start with Windows: a scheduled task that starts OptiMind in the notification
area when you sign in, with administrator rights and without the Windows prompt.

Windows doesn't start apps that need administrator rights from the usual
startup list (Task Manager → Startup apps), so a task set to "Run with highest
privileges" is the supported way. Creating or removing it needs administrator
rights: the installer has them, and so does OptiMind when it runs as
administrator. The task starts OptiMind only for the account that created it,
only while that account is signed in, and keeps it running on battery. It's
listed in Task Scheduler as "OptiMind AI".
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

from app.config import BASE_DIR, FROZEN, IS_WINDOWS, PROGRAM
from app.i18n import tx

TASK_NAME = "OptiMind AI"
_TASK_CREATE_OR_UPDATE = 6
_TASK_LOGON_INTERACTIVE_TOKEN = 3
_NOT_FOUND = -2147024894  # HRESULT 0x80070002
_ACCESS_DENIED = -2147024891  # HRESULT 0x80070005


class AutostartError(Exception):
    """The task couldn't be created or removed."""


def command() -> tuple[str, str]:
    """(program, arguments) that start OptiMind in the notification area."""
    if FROZEN:
        return PROGRAM, "--background"
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # from source: no console window
    return str(pythonw if pythonw.exists() else sys.executable), f'"{BASE_DIR / "run.py"}" --background'


def task_xml(user_sid: str, program: str, arguments: str) -> str:
    """The task's definition (Task Scheduler schema 1.4)."""
    sid, program, arguments = escape(user_sid), escape(program), escape(arguments)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>OptiMind AI</Author>
    <Description>Starts OptiMind AI in the notification area when you sign in, with administrator rights and without a prompt. Turn it off in OptiMind: Settings, Start with Windows.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{sid}</UserId>
      <Delay>PT10S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{program}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _com_code(e: Exception) -> int | None:
    # The real error code sits inside the COM exception details (excepinfo's scode).
    excepinfo = getattr(e, "excepinfo", None) or ()
    return excepinfo[5] if len(excepinfo) > 5 and excepinfo[5] else getattr(e, "hresult", None)


def _root_folder():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()  # COM is per thread; the API runs each request on a worker thread
    scheduler = win32com.client.Dispatch("Schedule.Service")
    scheduler.Connect()
    return scheduler.GetFolder("\\")


def _current_user_sid() -> str:
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])


def status() -> bool | None:
    """True when OptiMind starts with Windows (a task that starts this copy of OptiMind),
    False when it doesn't, None when that can't be read (off Windows, or not allowed)."""
    if not IS_WINDOWS:
        return None
    try:
        task = _root_folder().GetTask(TASK_NAME)
        action = task.Definition.Actions.Item(1)
        return bool(task.Enabled) and Path(action.Path.strip('"')) == Path(command()[0])
    except Exception as e:
        return False if _com_code(e) == _NOT_FOUND else None


def enable() -> None:
    """Creates (or updates) the task. Needs administrator rights."""
    if not IS_WINDOWS:
        raise AutostartError(tx("only available on Windows"))
    try:
        _root_folder().RegisterTask(TASK_NAME, task_xml(_current_user_sid(), *command()), _TASK_CREATE_OR_UPDATE,
                                    None, None, _TASK_LOGON_INTERACTIVE_TOKEN)
    except Exception as e:
        if _com_code(e) == _ACCESS_DENIED:
            raise AutostartError(tx("needs OptiMind to run as administrator")) from e
        raise AutostartError(tx("Task Scheduler refused it ({error})", error=str(e))) from e


def disable() -> None:
    """Removes the task (nothing to do if there isn't one). Needs administrator rights."""
    if not IS_WINDOWS:
        return
    try:
        _root_folder().DeleteTask(TASK_NAME, 0)
    except Exception as e:
        code = _com_code(e)
        if code == _NOT_FOUND:
            return
        if code == _ACCESS_DENIED:
            raise AutostartError(tx("needs OptiMind to run as administrator")) from e
        raise AutostartError(tx("Task Scheduler refused it ({error})", error=str(e))) from e
