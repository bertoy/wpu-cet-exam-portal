@echo off
REM ===================================================================
REM  WPU CET Examination Portal - run on the room's own network
REM
REM  Double-click this file. It sets everything up the first time and
REM  starts in a few seconds every time after that.
REM ===================================================================
setlocal
cd /d "%~dp0"

echo.
echo  Preparing the examination portal...
echo.

REM --- find Python -------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py -3
if "%PY%"=="" ( where python >nul 2>&1 && set PY=python )
if "%PY%"=="" (
  echo  Python is not installed on this laptop.
  echo  Install it from python.org, tick "Add Python to PATH", then
  echo  run this file again.
  echo.
  pause
  exit /b 1
)

REM --- first run: make the virtual environment ----------------------
if not exist ".venv\Scripts\python.exe" (
  echo  First run - setting up. This takes a few minutes, once.
  %PY% -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  REM waitress serves properly on Windows; gunicorn does not run here at all.
  ".venv\Scripts\python.exe" -m pip install waitress
)

REM --- open the firewall for the exam port, once --------------------
REM  Without this, Windows blocks the phones silently. Needs
REM  administrator rights; if it fails, the script carries on and the
REM  Windows prompt will appear instead.
netsh advfirewall firewall show rule name="WPU Exam Portal" >nul 2>&1
if errorlevel 1 (
  netsh advfirewall firewall add rule name="WPU Exam Portal" dir=in ^
    action=allow protocol=TCP localport=8000 profile=private >nul 2>&1
)

echo.
".venv\Scripts\python.exe" run_lan.py --port 8000
goto done

:failed
echo.
echo  Setup failed. Check that this laptop has internet access for the
echo  first run only, then try again.
echo.
pause
exit /b 1

:done
echo.
echo  The examination server has stopped.
pause
