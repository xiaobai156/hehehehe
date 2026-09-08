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

title Kill-He Crawler
cd /d "%~dp0"

:input_period
set "PERIOD="
echo.
set /p "PERIOD=Input period: "
set "PERIOD=%PERIOD: =%"
if not defined PERIOD (
  echo Period cannot be empty.
  goto input_period
)
for /f "delims=0123456789" %%A in ("%PERIOD%") do (
  echo Period must be numbers only.
  goto input_period
)

echo.
echo Running period %PERIOD% ...
choice /c NF /n /m "抓取模式：N=全站，F=仅重抓失败TXT站点："
set "RUN_MODE="
if errorlevel 2 set "RUN_MODE=--retry-failures"
%PY_CMD% he_crawler.py --period %PERIOD% %RUN_MODE%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Finished. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
