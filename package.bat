@echo off
rem ============================================================
rem  StockWatch - build a zip (double-click this file)
rem  Default: code only, no database and no API keys.
rem  With data:   package.bat --with-data
rem  With keys:   package.bat --with-secrets
rem  NOTE: keep this file pure ASCII (console codepage).
rem ============================================================
setlocal
set "HEREDIR=%~dp0"
if "%HEREDIR:~-1%"=="\" set "HEREDIR=%HEREDIR:~0,-1%"
for /f "usebackq delims=" %%i in (`wsl wslpath -a "%HEREDIR%"`) do set "WSLDIR=%%i"
if not defined WSLDIR (
  echo Cannot reach WSL.
  pause
  exit /b 1
)
wsl -e bash -lc "cd '%WSLDIR%' && ./package.sh %*"
echo.
echo The zip is in the dist\ folder.
pause
