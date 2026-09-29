@echo off
rem ============================================================
rem  StockWatch - deploy the current code to a Synology NAS
rem
rem    deploy.bat 192.168.1.20 admin     (password is prompted)
rem    deploy.bat                        (reuses the last IP/user)
rem    deploy.bat --dry-run              (package only, no connection)
rem
rem  Keep this file pure ASCII (console codepage, see start.bat).
rem ============================================================
chcp 65001 >nul
setlocal
set "HEREDIR=%~dp0"
if "%HEREDIR:~-1%"=="\" set "HEREDIR=%HEREDIR:~0,-1%"
wsl.exe --cd "%HEREDIR%" -e ./deploy.sh %*
echo.
pause
