@echo off
rem Say what state this PC is in, so a problem can be read rather than guessed.
rem Double-click it. Nothing is changed, started or installed.
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Billing check

echo.
echo   Billing -- check this PC
echo   =======================
echo.
echo   Folder      %~dp0
ver
echo.

rem ---------------------------------------------------------- the folder --
echo   Files here
if exist "billing.py" (echo     [ok] billing.py) else (echo     [MISSING] billing.py)
if exist "app\server.py" (echo     [ok] app\server.py) else (echo     [MISSING] app\server.py)
if exist "app\db.py" (echo     [ok] app\db.py) else (echo     [MISSING] app\db.py)
if exist "app\static\app.js" (echo     [ok] app\static\app.js) else (echo     [MISSING] app\static\app.js)
if exist "app\static\billing.ico" (echo     [ok] app\static\billing.ico) else (echo     [MISSING] app\static\billing.ico)
if exist "Billing.exe" (echo     [ok] Billing.exe) else (echo     [none] Billing.exe -- double-click make_exe.bat to build it)
if exist "app\data\billing.db" (echo     [ok] app\data\billing.db -- your books) else (echo     [none yet] billing.db is made on first run)
echo.

rem ---------------------------------------------------------- where it is --
echo   Location
echo %~dp0 | findstr /i "onedrive dropbox icloud" >nul
if not errorlevel 1 (
  echo     [WARNING] This folder is inside a syncing folder.
  echo               Move the whole Billing folder to C:\Billing before
  echo               entering any bills. Syncing corrupts the database.
) else (
  echo     [ok] not inside OneDrive, Dropbox or iCloud
)
echo.

rem -------------------------------------------------------------- Python --
echo   Python
set "FOUND="

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "FOUND=py -3"
  for /f "tokens=2" %%V in ('py -3 -V 2^>^&1') do echo     [ok] py launcher     %%V
) else (
  echo     [no] py launcher not usable
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  if not defined FOUND set "FOUND=python"
  for /f "tokens=2" %%V in ('python -V 2^>^&1') do echo     [ok] python on PATH  %%V
) else (
  echo     [no] python on PATH not usable
)

if not defined FOUND (
  call "%~dp0findpython.bat"
  if defined PY (
    set "FOUND=!PY!"
    echo     [ok] found an install that is not on the PATH:
    echo          !PY!
  )
)

if not defined FOUND (
  echo.
  echo     [PROBLEM] No usable Python 3 on this PC.
  echo.
  echo               Install it from https://www.python.org/downloads/
  echo               and tick "Add python.exe to PATH" on the FIRST screen.
  echo.
  where python >nul 2>nul
  if not errorlevel 1 (
    echo               Note: something called python IS on the PATH but will
    echo               not run. That is the Microsoft Store stub, not Python.
    echo               Installing real Python from the link above replaces it.
  )
  echo.
  echo   =======================
  echo   Send the text above and I can say what to do next.
  echo.
  pause
  endlocal
  exit /b 1
)
echo.

rem ------------------------------------------------------------ the rest --
echo   Printing
!FOUND! -c "import reportlab" >nul 2>nul
if errorlevel 1 (
  echo     [no] reportlab not installed -- install.bat adds it.
  echo          Everything except printed PDFs works without it.
) else (
  echo     [ok] reportlab installed
)
echo.

echo   Billing's own window
if not exist "Billing.exe" goto :nowindow
!FOUND! -c "import sys; sys.exit(0 if b'billing-window-check' in open('Billing.exe', 'rb').read() else 1)" >nul 2>nul
if errorlevel 1 (
  echo     [old] Billing.exe was built before 6 Oct 2026 -- double-click
  echo           make_exe.bat once to rebuild it, then run this again.
  goto :windowdone
)
del "%LOCALAPPDATA%\Billing\window-check.txt" >nul 2>nul
start "" /wait "Billing.exe" --window-check
set "WC=!errorlevel!"
if "!WC!"=="0" (echo     [ok] it opens on this PC) else (echo     [PROBLEM] it does not open -- code !WC!)
if exist "%LOCALAPPDATA%\Billing\window-check.txt" type "%LOCALAPPDATA%\Billing\window-check.txt"
goto :windowdone
:nowindow
echo     [none] no Billing.exe -- double-click make_exe.bat to build it
:windowdone
echo.

echo   Internet mark
powershell -NoProfile -Command "$n = @(Get-ChildItem -LiteralPath '.' -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notlike '*\app\data\*' } | Where-Object { Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue }).Count; if ($n) { '    [note] ' + $n + ' files carry the came-from-the-internet mark -- make_exe.bat takes it off' } else { '    [ok] no file here carries the came-from-the-internet mark' }"
echo.

echo   Port 8800
netstat -ano | findstr ":8800" > "%TEMP%\billing_port.txt" 2>nul
for /f %%A in ('type "%TEMP%\billing_port.txt" ^| find /c "LISTENING"') do set "BUSY=%%A"
if "!BUSY!"=="0" (
  echo     [ok] free -- Billing is not running
) else (
  echo     [busy] something is already listening on 8800:
  type "%TEMP%\billing_port.txt"
  echo            If that is Billing it is already running -- open
  echo            http://127.0.0.1:8800/ in your browser.
)
del "%TEMP%\billing_port.txt" >nul 2>nul
echo.

echo   Shortcuts
if exist "%USERPROFILE%\Desktop\Billing.lnk" echo     [ok] Desktop
if exist "%USERPROFILE%\OneDrive\Desktop\Billing.lnk" echo     [ok] Desktop ^(OneDrive^)
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Billing.lnk" echo     [ok] Start menu
echo.

echo   Last lines of the log
if exist "app\data\server.log" (
  powershell -NoProfile -Command "Get-Content 'app\data\server.log' -Tail 12"
) else (
  echo     ^(no log yet -- Billing has not run on this PC^)
)

echo.
echo   Last lines of Billing.exe's own log
if exist "%LOCALAPPDATA%\Billing\launch-log.txt" (
  powershell -NoProfile -Command "Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'Billing\launch-log.txt') -Tail 15"
) else (
  echo     ^(none yet^)
)
if exist "%LOCALAPPDATA%\Billing\window-errors.txt" (
  echo   Errors inside Billing's window
  powershell -NoProfile -Command "Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'Billing\window-errors.txt') -Tail 6"
)

echo.
echo   =======================
echo   Copy everything above and send it to me.
echo.
pause
endlocal
