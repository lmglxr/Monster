@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [First run] Installing the private runtime. Keep the network connected...
  call "%~dp0一键安装.bat"
  if errorlevel 1 exit /b 1
)

"%~dp0.venv\Scripts\python.exe" %*
exit /b %errorlevel%
