@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_运行Python.bat" "%~dp0mvp_bot.py" --mode full
pause
