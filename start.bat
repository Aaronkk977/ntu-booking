@echo off
cd /d "%~dp0"

rem uv (https://docs.astral.sh/uv/) downloads the Python this project uses into .venv, so nothing has to be
rem installed by hand. The version is fixed here and only changes after it has been tried.
set "PYTHON_VERSION=3.12"
set "UV_DIR=%USERPROFILE%\.local\bin"
rem uv's cache and .venv may sit on different drives; copying is fine for a few packages.
set "UV_LINK_MODE=copy"

rem A .venv from another Python version (e.g. made by an older version of this file) is replaced.
set "REBUILD="
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; sys.exit(sys.version_info[:2] != (%PYTHON_VERSION:.=, %))" >nul 2>nul
  if errorlevel 1 set "REBUILD=1"
)
set "SETUP="
if not exist .venv\Scripts\python.exe set "SETUP=1"
if defined REBUILD set "SETUP=1"
if not defined SETUP (
  .venv\Scripts\python.exe -c "import playwright" >nul 2>nul
  if errorlevel 1 set "SETUP=1"
)

if defined SETUP (
  echo ============================================================
  echo  First launch: setting up the environment.
  echo  This downloads Python and Playwright, about 75 MB, and
  echo  usually takes 1 to 3 minutes. Please keep this window open.
  echo ============================================================
  echo.
)

where uv >nul 2>nul
if errorlevel 1 if not exist "%UV_DIR%\uv.exe" (
  echo Installing uv, the tool that sets up Python ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" >nul
)
set "PATH=%UV_DIR%;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo.
  echo Setup failed: uv could not be installed. Check the internet connection and run this file again,
  echo or install uv yourself from https://docs.astral.sh/uv/
  pause
  exit /b 1
)

if defined REBUILD (
  echo The Python version has changed: removing the old environment ...
  rmdir /s /q .venv
)
if not exist .venv\Scripts\python.exe (
  echo Setting up Python %PYTHON_VERSION% ...
  uv venv --quiet --python %PYTHON_VERSION% .venv
  if errorlevel 1 (
    echo.
    echo Setup failed: Python could not be set up. See the messages above, then run this file again.
    pause
    exit /b 1
  )
)

.venv\Scripts\python.exe -c "import playwright" >nul 2>nul
if errorlevel 1 (
  echo Installing Playwright, the largest download ...
  uv pip install --python .venv\Scripts\python.exe -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Setup failed: Playwright could not be installed. See the messages above, then run this file again.
    pause
    exit /b 1
  )
)

if defined SETUP (
  echo.
  echo Setup complete. Next time the console starts right away.
  echo.
)
echo Starting the console ...
.venv\Scripts\python.exe server.py
pause
