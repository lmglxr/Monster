@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating the private Python runtime...
  py -3 -m venv ".venv"
  if errorlevel 1 python -m venv ".venv"
)
if not exist ".venv\Scripts\python.exe" (
  echo Failed to create the private Python runtime.
  endlocal & exit /b 1
)
".venv\Scripts\python.exe" "visual_entry.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%