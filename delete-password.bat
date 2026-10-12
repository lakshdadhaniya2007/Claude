@echo off
rem Choose the delete password -- or a new one, if it is forgotten.
rem Deleting a challan, a bill or an account asks for it.
rem Double-click this file on the computer Billing runs on.
setlocal
cd /d "%~dp0"
title Billing delete password

call "%~dp0findpython.bat"

if not defined PY (
  echo Python 3 was not found. Run install.bat first.
  pause
  endlocal
  exit /b 1
)

echo.
echo   Deleting a challan, a bill or an account will ask for this password.
echo   It is typed without showing on screen.
echo.
%PY% billing.py --set-delete-password
echo.
pause
endlocal
