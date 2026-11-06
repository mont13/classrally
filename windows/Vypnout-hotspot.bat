@echo off
chcp 65001 >nul
title ClassRally - vypnout hotspot
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0hotspot-vypnout.ps1"
set "CLASSRALLY_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %CLASSRALLY_RESULT%
