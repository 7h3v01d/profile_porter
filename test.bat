@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m pytest test_profile_porter.py -v
pause
