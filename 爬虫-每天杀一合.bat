@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PY_EXE="
set "PY_ARGS="
if exist ".venv\Scripts\python.exe" set "PY_EXE=%CD%\.venv\Scripts\python.exe"
if not defined PY_EXE (
  py -3 --version >nul 2>nul
  if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
  )
)
if not defined PY_EXE (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY_EXE=python"
)
if not defined PY_EXE (
  if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY_EXE=%LocalAppData%\Programs\Python\Python310\python.exe"
)
if not defined PY_EXE (
  echo Cannot find Python 3.10 or newer.
  pause
  exit /b 1
)

:input_period
set "PERIOD="
set /p "PERIOD=Input period: "
"%PY_EXE%" %PY_ARGS% -c "import os,sys; p=os.environ.get('PERIOD','').strip(); sys.exit(0 if p.isascii() and p.isdigit() and int(p)>0 else 2)"
if errorlevel 1 (
  echo Period must be a positive integer.
  goto input_period
)
choice /c NF /n /m "抓取模式：N=全站，F=仅重抓失败TXT站点："
set "RUN_MODE="
if errorlevel 2 set "RUN_MODE=--retry-failures"
"%PY_EXE%" %PY_ARGS% he_crawler.py --period "%PERIOD%" --hard-timeout 90 --browser-hard-timeout 130 %RUN_MODE%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Finished. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
