@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install_packages

set "PYTHON_EXE="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE for %%P in (python.exe) do if not "%%~$PATH:P"=="" set "PYTHON_EXE=%%~$PATH:P"

if defined PYTHON_EXE (
  "%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,10),(3,11),(3,12)) else 1)"
  if errorlevel 1 set "PYTHON_EXE="
)

if not defined PYTHON_EXE (
  where winget >nul 2>nul
  if errorlevel 1 goto no_python
  echo Python 3.10-3.12 was not found. Installing Python 3.12 with winget...
  winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto install_failed
  if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
)

if not defined PYTHON_EXE goto no_python

echo Creating the project virtual environment...
"%PYTHON_EXE%" -m venv ".venv"
if errorlevel 1 goto install_failed

:install_packages
echo Installing or checking packages. The first run may take several minutes...
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check --index-url https://pypi.org/simple --upgrade pip
if errorlevel 1 goto install_failed
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check --index-url https://pypi.org/simple -r requirements.txt
if errorlevel 1 goto install_failed

echo.
echo Setup complete. You can now run the main launcher.
exit /b 0

:no_python
echo [ERROR] Python 3.10-3.12 was not found and winget could not install it.
echo Install Python 3.12 from https://www.python.org/downloads/ and retry.
exit /b 1

:install_failed
echo [ERROR] Setup failed. Check the network connection and retry.
exit /b 1
