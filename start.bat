@echo off
REM Zoomollo for Windows: double-click to start.
REM First run sets everything up (a minute or two); after that it starts in seconds.
cd /d "%~dp0"

if exist ".venv\Scripts\zoomollo.exe" goto run

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Zoomollo needs Python 3.10 or newer.
  echo Opening the download page. During setup, tick "Add python.exe to PATH".
  echo Then double-click start.bat again.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

echo Setting up Zoomollo (first run only)...
%PY% -m venv .venv || goto fail
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -e . || goto fail

:run
if not exist "zoomollo-settings.txt" copy "zoomollo-settings.example.txt" "zoomollo-settings.txt" >nul
".venv\Scripts\zoomollo.exe" serve --open
pause
exit /b 0

:fail
echo Setup failed. Check your internet connection and try again.
pause
exit /b 1
