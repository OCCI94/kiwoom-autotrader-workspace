@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if errorlevel 1 goto failed

if exist ".venv\Scripts\python.exe" goto install
py -3 -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv ".venv"
    if errorlevel 1 goto failed
    goto install
)
python -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 (
    echo Python 3.11 or newer is required. Install Python and enable Add Python to PATH.
    goto failed
)
python -m venv ".venv"
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3,11))"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" "main.py" %*
set "task_exit=%errorlevel%"
if not "%task_exit%"=="0" echo See README.md for first-time credentials and Telegram setup.
pause
exit /b %task_exit%

:failed
echo Setup failed. The trading program was not started.
pause
exit /b 1
