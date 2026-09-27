@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_运行Python.bat" "%~dp0环境检测.py" --download-model --check-game-attach --install-missing
if errorlevel 1 (
  echo.
  echo 环境检测未通过，已取消真实游戏操作。
  pause
  exit /b 1
)
call "%~dp0_运行Python.bat" "%~dp0mvp_bot.py" --full-smoke-test --live --smoke-cycles 2
pause
