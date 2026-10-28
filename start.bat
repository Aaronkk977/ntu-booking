@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)

python -c "import playwright" >nul 2>nul
if errorlevel 1 (
  echo First run: installing Playwright ...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Installing Playwright failed. See the messages above.
    pause
    exit /b 1
  )
)

python server.py
pause
