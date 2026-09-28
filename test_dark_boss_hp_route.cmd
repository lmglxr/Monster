@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%"

if not exist ".venv\Scripts\python.exe" (
    echo Private runtime is missing.
    if exist "一键安装.bat" call "一键安装.bat"
)

if not exist ".venv\Scripts\python.exe" (
    echo Failed to prepare the private Python runtime.
    set "EXIT_CODE=1"
    goto :finish
)

".venv\Scripts\python.exe" "mvp_bot.py" --config-overrides "dark_shark_config.json" --live --dark-boss-hp-test --test-cycles 0
set "EXIT_CODE=%ERRORLEVEL%"

:finish
popd
pause
exit /b %EXIT_CODE%
