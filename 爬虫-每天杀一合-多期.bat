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

title Kill-He Multi Period Crawler
cd /d "%~dp0"

:input_periods
set "PERIODS="
echo.
set /p "PERIODS=Input periods, separated by spaces: "
if not defined PERIODS (
  echo Periods cannot be empty.
  goto input_periods
)

for %%P in (%PERIODS%) do (
  for /f "delims=0123456789" %%A in ("%%P") do (
    echo Period must be numbers only: %%P
    goto input_periods
  )
)

echo.
echo Running periods: %PERIODS%
%PY_CMD% he_multi_period_crawler.py %PERIODS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Finished. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
