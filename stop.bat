@echo off
rem Stop Billing -- the server started by run.bat or by the Billing icon --
rem and close Billing's own window, so make_exe.bat can rebuild Billing.exe.
setlocal
cd /d "%~dp0"

call "%~dp0findpython.bat"

if not defined PY (
  echo Python 3 was not found. Nothing here can stop the server without it;
  echo end "pythonw.exe" in Task Manager instead.
  pause
  endlocal
  exit /b 1
)

%PY% billing.py --stop %*
endlocal
