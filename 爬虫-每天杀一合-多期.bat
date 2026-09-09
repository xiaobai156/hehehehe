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

:input_periods
set "PERIODS="
set /p "PERIODS=Input periods, separated by spaces: "
"%PY_EXE%" %PY_ARGS% -c "import os,sys; ps=os.environ.get('PERIODS','').split(); sys.exit(0 if ps and all(p.isascii() and p.isdigit() and int(p)>0 for p in ps) else 2)"
if errorlevel 1 (
  echo Periods must be positive integers separated by spaces.
  goto input_periods
)
"%PY_EXE%" %PY_ARGS% he_multi_period_crawler.py %PERIODS%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Finished. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
