@echo off
rem Choose the passcode that phones and other PCs are asked for.
rem Double-click this file on the computer Billing runs on.
setlocal
cd /d "%~dp0"
title Billing passcode

call "%~dp0findpython.bat"

if not defined PY (
  echo Python 3 was not found. Run install.bat first.
  pause
  endlocal
  exit /b 1
)

echo.
echo   Phones and other PCs will be asked for this passcode.
echo   It is typed without showing on screen.
echo.
%PY% billing.py --set-passcode
echo.
pause
endlocal
