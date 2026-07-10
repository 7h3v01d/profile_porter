@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 (
  echo [error] Python not found on PATH. Install Python 3.11+ and tick "Add to PATH".
  pause
  exit /b 1
)
python -c "import PySide6" >nul 2>&1
if errorlevel 1 (
  echo [error] PySide6 not installed. Run:  pip install PySide6
  pause
  exit /b 1
)
python profile_porter.py %*
if errorlevel 1 pause
