@echo off
rem Build Billing.exe in this folder -- the one thing to double-click.
rem
rem It is made here, on the PC, with the C# compiler that is part of .NET
rem Framework 4 and comes with every Windows 10 and 11 machine. Nothing is
rem downloaded, and a program built on the PC is not stopped by SmartScreen
rem the way one downloaded from the internet is.
rem
rem install.bat runs this for you. Run it yourself after copying a newer
rem app\launcher\Billing.cs across. /quiet skips the pause at the end.
setlocal
cd /d "%~dp0"

set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" goto :nocompiler

if not exist "app\launcher\Billing.cs" goto :nosource

rem A Billing.exe still open cannot be written over.
tasklist /FI "IMAGENAME eq Billing.exe" 2>nul | find /i "Billing.exe" >nul
if not errorlevel 1 goto :running

rem A Billing folder that came by download, e-mail or WhatsApp carries
rem Windows' "came from the internet" mark on every file in it, and .NET will
rem not load the WebView2 files Billing's window is drawn with while they
rem carry it. Taken off here -- the books in app\data are left alone.
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '.' -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notlike '*\app\data\*' } | Unblock-File -ErrorAction SilentlyContinue" >nul 2>nul

set "LOGF=%TEMP%\billing_make_exe.txt"
set "REFS=/reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Core.dll"
set "ICON=/win32icon:app\launcher\Billing.ico"

rem Billing's own window -- its screens shown inside Billing.exe, with no
rem browser -- needs Microsoft's WebView2 files, kept in app\launcher\webview2.
rem Without them Billing.exe is built as before and opens a browser window.
set "WV=app\launcher\webview2"
set "WVREFS="
if exist "%WV%\Microsoft.Web.WebView2.Core.dll" if exist "%WV%\Microsoft.Web.WebView2.WinForms.dll" set "WVREFS=/define:WEBVIEW /reference:%WV%\Microsoft.Web.WebView2.Core.dll /reference:%WV%\Microsoft.Web.WebView2.WinForms.dll"

"%CSC%" /nologo /target:winexe /optimize+ /out:"Billing.exe" %ICON% %REFS% %WVREFS% "app\launcher\Billing.cs" > "%LOGF%" 2>&1
if not errorlevel 1 goto :built

rem Built without its icon rather than not at all, should the icon be refused.
set "ICON="
"%CSC%" /nologo /target:winexe /optimize+ /out:"Billing.exe" %REFS% %WVREFS% "app\launcher\Billing.cs" >> "%LOGF%" 2>&1
if not errorlevel 1 goto :built

rem Still nothing: built without its own window, which opens a browser window
rem instead -- and what stopped the window is shown, to be sent on.
if not defined WVREFS goto :failed
"%CSC%" /nologo /target:winexe /optimize+ /out:"Billing.exe" /win32icon:"app\launcher\Billing.ico" %REFS% "app\launcher\Billing.cs" >> "%LOGF%" 2>&1
if errorlevel 1 goto :failed
type "%LOGF%"
echo.
echo   Built Billing.exe WITHOUT its own window: it opens Billing in a browser
echo   window. Send the lines above to whoever looks after Billing.
goto :done

:built
if not defined WVREFS goto :builtplain

rem Its window tried out on this PC before anyone relies on it: Billing.exe
rem opens one off the screen for a moment and says whether WebView2 drew a
rem page in it. Should it not, Billing.exe is built again without the window,
rem so the icon always opens Billing -- in a browser window, as before.
echo   Built Billing.exe. Checking its window on this PC...
set "CHECKF=%LOCALAPPDATA%\Billing\window-check.txt"
del "%CHECKF%" >nul 2>nul
start "" /wait "Billing.exe" --window-check
set "WC=%errorlevel%"
if "%WC%"=="0" goto :windowok
if "%WC%"=="2" goto :windownoruntime

echo.
echo   Billing's own window does not open on this PC:
if exist "%CHECKF%" type "%CHECKF%"
echo.
"%CSC%" /nologo /target:winexe /optimize+ /out:"Billing.exe" %ICON% %REFS% "app\launcher\Billing.cs" > "%LOGF%" 2>&1
if errorlevel 1 goto :failed
echo   Built Billing.exe again WITHOUT its own window: it opens Billing in a
echo   browser window. Send the lines above to whoever looks after Billing.
goto :done

:windowok
echo   Built Billing.exe, with its own window -- checked: it opens on this PC.
goto :done

:windownoruntime
echo   Built Billing.exe, with its own window -- but Microsoft's WebView2 is
echo   not installed on this PC. Until it is, Billing opens in a browser window
echo   and offers Microsoft's free download page.
goto :done

:builtplain
if defined ICON echo   Built Billing.exe
if not defined ICON echo   Built Billing.exe, without its icon.

:done
del "%LOGF%" >nul 2>nul
if /i "%~1"=="/quiet" goto :end

rem The Desktop and Start menu shortcuts are pointed at Billing.exe now.
call "%~dp0findpython.bat"
if defined PY %PY% billing.py --shortcut >nul 2>nul
if defined PY echo   The Billing icons on the Desktop and in the Start menu open it.
echo.
echo   Double-click Billing.exe, or the Billing icon, to start Billing.
echo.
pause
goto :end

:running
echo.
echo   Billing is open. Close its window -- or double-click stop.bat, which
echo   closes it too -- then run make_exe.bat again.
goto :fail

:nocompiler
echo.
echo   The C# compiler that comes with Windows was not found:
echo     %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
echo.
echo   Turn on .NET Framework 4.8 under Control Panel - Programs -
echo   Turn Windows features on or off. Billing itself does not need it:
echo   run.bat and the Billing icon on the Desktop still start it.
goto :fail

:nosource
echo.
echo   app\launcher\Billing.cs is missing. Copy it across from the Billing
echo   folder on the other computer, then run this again.
goto :fail

:failed
type "%LOGF%"
echo.
echo   Billing.exe could not be built. Send the lines above.
echo   Billing still starts from run.bat and the Billing icon on the Desktop.

:fail
echo.
if /i not "%~1"=="/quiet" pause
endlocal
exit /b 1

:end
endlocal
exit /b 0
