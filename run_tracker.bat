@echo off
rem Double-click to start the tracker. Extra options also work, e.g.:
rem   run_tracker.bat --dry-run
rem   run_tracker.bat --test-sheet
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

".venv\Scripts\python.exe" pandathon_tracker.py %*
echo.
pause
exit /b

:error
echo.
echo Setup failed. Is Python 3 installed and on PATH?
pause
exit /b 1
