@echo off
rem Start Billing and open it in your browser.
rem Double-click this file, or run it from a Command Prompt.
setlocal
cd /d "%~dp0"

call "%~dp0findpython.bat"

if not defined PY (
  echo.
  echo   Python 3 was not found on this PC.
  echo.
  echo   Billing needs it, and Windows does not come with it. Install it once
  echo   from
  echo.
  echo       https://www.python.org/downloads/
  echo.
  echo   and tick "Add python.exe to PATH" on the first screen of the
  echo   installer. Then run install.bat in this folder.
  echo.
  pause
  endlocal
  exit /b 1
)

%PY% billing.py %*
if errorlevel 1 pause
endlocal
