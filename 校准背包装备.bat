@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "_运行Python.bat" mvp_bot.py --calibrate-inventory
pause
