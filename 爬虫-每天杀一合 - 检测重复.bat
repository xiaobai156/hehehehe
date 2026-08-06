@echo off
setlocal EnableExtensions

set "PY_CMD="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"

if not defined PY_CMD (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
  if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY_CMD=%LocalAppData%\Programs\Python\Python310\python.exe"
)

if not defined PY_CMD (
  echo Cannot find Python. Please install Python or add it to PATH.
  pause
  exit /b 1
)

title Kill-He Duplicate Checker
cd /d "%~dp0"
echo Checking duplicates...
%PY_CMD% he_duplicate_checker.py --workers 8
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Finished. Exit code: %EXIT_CODE%
echo Check latest_results.txt.
pause
exit /b %EXIT_CODE%
