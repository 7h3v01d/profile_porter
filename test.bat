@echo off
cd /d "%~dp0"
python -m pytest test_profile_porter.py -v
pause
