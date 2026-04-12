@echo off
setlocal

REM Facebook Session Creator — Windows launcher.
REM Double-click this file to run.

cd /d "%~dp0"

set PY=
where python >nul 2>nul && set PY=python
if not defined PY (
    where py >nul 2>nul && set PY=py
)

if not defined PY (
    echo.
    echo ERROR: Python is not installed.
    echo.
    echo Please install Python 3.9 or newer from:
    echo     https://www.python.org/downloads/
    echo.
    echo During installation, check the box "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

REM Create a local virtual environment on first run.
if not exist ".venv\Scripts\python.exe" (
    echo First-time setup — creating local Python environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Could not create the Python environment.
        pause
        exit /b 1
    )
)

set VENV_PY=.venv\Scripts\python.exe
"%VENV_PY%" -m pip install --quiet --upgrade pip >nul 2>nul
"%VENV_PY%" fb_session.py

if %errorlevel% neq 0 (
    echo.
    pause
)

endlocal
