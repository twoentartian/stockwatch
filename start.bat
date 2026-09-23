@echo off
rem ============================================================
rem  StockWatch - one-click start (double-click this file)
rem
rem  The server is started DETACHED inside WSL: it keeps running
rem  after this window is closed. Logs go to logs\stockwatch.log.
rem  Use stop.bat to stop it.
rem
rem  NOTE: keep this file pure ASCII. A .bat containing UTF-8 text
rem  gets mangled by the console codepage and breaks into garbage
rem  commands. chcp below only affects OUTPUT, which is fine.
rem ============================================================
chcp 65001 >nul
setlocal
set "PORT=8765"

set "HEREDIR=%~dp0"
rem strip the trailing backslash: "...\" escapes the quote and wslpath gets junk
if "%HEREDIR:~-1%"=="\" set "HEREDIR=%HEREDIR:~0,-1%"

echo Starting StockWatch (this window can be closed afterwards)...
echo.
rem --cd instead of bash -lc "cd ... && ...": cmd mangles "&&" inside arguments.
rem run.sh --bg waits until the port is really up, then returns.
wsl.exe --cd "%HEREDIR%" -e ./run.sh --bg
if errorlevel 1 goto failed

start "" "http://127.0.0.1:%PORT%/"
echo.
echo Page opened. The server keeps running in the background.
echo   stop  : double-click stop.bat
echo   logs  : logs\stockwatch.log
echo.
pause
exit /b 0

:failed
echo.
echo Start failed - see the messages above, or logs\stockwatch.log
echo.
pause
exit /b 1
