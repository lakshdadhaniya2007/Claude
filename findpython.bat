@echo off
rem Find a usable Python 3 and leave it in %PY%. The other .bat files call
rem this, so there is one place that knows how to look.
rem
rem Three things make this longer than "where python":
rem
rem   * Windows 10 and 11 ship a stub at WindowsApps\python.exe that is not
rem     Python at all -- it opens the Microsoft Store. It is zero bytes, and it
rem     sits on the PATH ahead of everything, so "where python" finds it first
rem     and the app looks broken for a reason nothing on screen explains.
rem   * The python.org installer only puts Python on the PATH when that box was
rem     ticked, and it usually is not. The install is still there to be found.
rem   * The py launcher is the reliable way in where it exists, and it does not
rem     always exist.
rem
rem Everything below goes through :try rather than a for loop, because a batch
rem block cannot contain %ProgramFiles(x86)% -- the bracket in the name closes
rem the block -- and cannot reliably contain the brackets in a line of Python.

set "PY="

rem The py launcher, which picks the newest Python installed.
call :try py -3
if defined PY goto :eof

rem python.exe on the PATH, if it is really Python and not the Store stub.
call :try python
if defined PY goto :eof

rem The usual install folders, newest first. Bracketed names are copied into
rem plain variables first so the paths below can be written normally.
set "PF=%ProgramFiles%"
set "PF86=%ProgramFiles(x86)%"
set "LAD=%LOCALAPPDATA%\Programs\Python"

call :scan 315
call :scan 314
call :scan 313
call :scan 312
call :scan 311
call :scan 310
call :scan 39
call :scan 38
goto :eof


:scan
if defined PY goto :eof
call :try "%LAD%\Python%~1\python.exe"
call :try "%PF%\Python%~1\python.exe"
call :try "%PF86%\Python%~1\python.exe"
call :try "C:\Python%~1\python.exe"
goto :eof


:try
rem Run the candidate and let Python itself say whether it is new enough. A
rem Store stub fails this outright; so does a Python 2 left over on the PATH.
if defined PY goto :eof
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%*"
goto :eof
