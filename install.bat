@echo off
rem Set Billing up on this PC, then start it.
rem
rem This is the file to double-click first on a new Windows machine. Afterwards
rem use the Billing icon on the Desktop.
rem
rem Safe to run again -- every step checks before it acts, and running it a
rem second time only replaces the shortcuts. It never touches your books.
setlocal
cd /d "%~dp0"
title Billing setup

echo.
echo   Billing ^& Order Management -- setup
echo   ==================================
echo.

call "%~dp0findpython.bat"

if not defined PY (
  echo   Python 3 was not found on this PC.
  echo.
  echo   Windows does not come with it, and Billing is written in it.
  echo   Install it once:
  echo.
  echo     1. Open   https://www.python.org/downloads/
  echo     2. Click the big yellow "Download Python" button
  echo     3. On the FIRST screen of the installer, tick the box
  echo            Add python.exe to PATH
  echo     4. Click "Install Now" and let it finish
  echo     5. Double-click this install.bat again
  echo.
  echo   If Python is already on this PC but nothing here can see it, run the
  echo   python.org installer again, choose Modify, and tick that same box.
  echo.
  pause
  endlocal
  exit /b 1
)

rem Billing.exe first, so the shortcuts setup makes point at it. Should it
rem not build, they point at Python instead and everything still works.
call "%~dp0make_exe.bat" /quiet

rem Everything else lives in billing.py, so it can be read, changed and tested
rem in one place rather than being written twice in two languages.
%PY% billing.py --setup
if errorlevel 1 (
  echo.
  echo   Setup did not finish. Nothing was lost -- read the message above,
  echo   put it right, and run install.bat again.
  echo.
  pause
  endlocal
  exit /b 1
)

echo   ==================================
echo   Ready. Starting Billing now.
echo.
echo   From now on, open it from the Billing icon on your Desktop or in
echo   the Start menu, or Billing.exe in this folder. To stop it,
echo   double-click stop.bat in this folder.
echo.

%PY% billing.py
if errorlevel 1 pause
endlocal
