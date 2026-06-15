@echo off
rem Double-click to launch the windroute web app (no PowerShell needed).
rem On first run (or on a new machine) it creates the local Python environment;
rem then it starts a local server and opens your browser to http://127.0.0.1:5000
cd /d "%~dp0"

set "VENVPY=.venv\Scripts\python.exe"

rem A virtualenv bakes in an absolute path to the Python that built it, so a
rem .venv copied/synced from another computer (e.g. via OneDrive) can't run here.
rem If one exists but won't even report its version, it's that stale copy -- wipe it.
if exist "%VENVPY%" (
    "%VENVPY%" --version >nul 2>&1 || (
        echo Local environment is from another machine - rebuilding it...
        rmdir /s /q .venv
    )
)

rem Create the environment and install dependencies if it isn't there.
if not exist "%VENVPY%" (
    echo Setting up the local Python environment ^(first run, ~1-2 min^)...
    python -m venv .venv || (
        echo.
        echo Could not create the environment. Is Python installed and on your PATH?
        echo Get it from https://www.python.org/downloads/ then run this again.
        pause & exit /b 1
    )
    "%VENVPY%" -m pip install --upgrade pip
    "%VENVPY%" -m pip install -r requirements.txt || (
        echo.
        echo Installing dependencies failed. Check your internet connection and retry.
        pause & exit /b 1
    )
)

rem Launch with the venv's Python directly -- no "activate" step needed.
"%VENVPY%" webapp.py
pause
