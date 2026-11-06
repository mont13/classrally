@echo off
chcp 65001 >nul
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
title ClassRally
cd /d "%~dp0"
if not exist "%~dp0python\python.exe" (
  echo Chybí přenosný Python. Rozbalte celý balíček ClassRally-Windows.zip.
  pause
  exit /b 1
)
"%~dp0python\python.exe" -B -u "%~dp0launcher.py"
echo.
echo Server byl ukončen. Případná chyba je vypsaná výše.
pause
