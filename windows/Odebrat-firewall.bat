@echo off
chcp 65001 >nul
title ClassRally - odebrat firewall
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0firewall-povolit.ps1" -Odebrat
set "CLASSRALLY_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %CLASSRALLY_RESULT%
