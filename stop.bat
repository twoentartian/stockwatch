@echo off
rem StockWatch - stop the background server (double-click this file).
rem Keep this file pure ASCII; see start.bat for why.
chcp 65001 >nul
setlocal
set "HEREDIR=%~dp0"
if "%HEREDIR:~-1%"=="\" set "HEREDIR=%HEREDIR:~0,-1%"
wsl.exe --cd "%HEREDIR%" -e ./run.sh --stop
echo.
pause
