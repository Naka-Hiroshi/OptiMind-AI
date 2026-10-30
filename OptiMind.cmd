@echo off
rem OptiMind AI from source: double-click to start. Windows asks for administrator
rem rights, then OptiMind opens its window, with an icon in the notification area.
rem (The installed app starts from the Start menu instead; see README.md.)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo OptiMind AI isn't installed in this folder yet. Run:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
