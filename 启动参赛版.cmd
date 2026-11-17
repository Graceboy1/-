@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "CONTEST_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "CONTEST_PYTHON=%~dp0.venv\Scripts\python.exe"
set "CONTEST_WEB_BUILD_DIR="
set "CONTEST_START_MODE="
if exist "web\.next\BUILD_ID" set "CONTEST_START_MODE=--production"
"%CONTEST_PYTHON%" -X utf8 launch.py %CONTEST_START_MODE% %*
if errorlevel 1 pause
endlocal
