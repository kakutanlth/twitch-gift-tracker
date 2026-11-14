@echo off
rem Double-click to open the tracker window: buttons at the top, the log at the bottom.
rem (run_tracker.bat still works for the plain console version.)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating a private Python environment...
  python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -c "import gspread, websocket" 2>nul
if errorlevel 1 (
  echo Installing the two libraries the tracker needs...
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" -c "import tkinter" 2>nul
if errorlevel 1 (
  echo This Python has no Tkinter, which the window needs.
  echo Run the Python installer again, choose Modify, and tick "tcl/tk and IDLE".
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" pandathon_gui.py
exit /b

:error
echo.
echo Setup failed. Is Python 3 installed and on PATH?
pause
exit /b 1
