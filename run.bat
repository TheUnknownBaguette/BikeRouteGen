@echo off
rem Double-click to launch the windroute web app (no PowerShell needed).
rem Starts a local server and opens your browser to http://127.0.0.1:5000
cd /d "%~dp0"
call ".venv\Scripts\activate.bat"
python webapp.py
pause
