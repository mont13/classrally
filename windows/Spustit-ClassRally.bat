@echo off
REM ===========================================================================
REM  ClassRally - spousteci skript pro Windows
REM  Dvojklik na tento soubor spusti server a otevre ucitelsky portal.
REM  Zavrenim tohoto cerneho okna server zastavite.
REM ===========================================================================
chcp 65001 >nul
title ClassRally server
cd /d "%~dp0"

REM Port lze zmenit promennou QUIZ_PORT; jinak atypicky vychozi 48217
if defined QUIZ_PORT (set "PORT=%QUIZ_PORT%") else (set "PORT=48217")

REM --- Najdi Python: nejdriv pribaleny (python\python.exe), pak systemovy ---
set "PY="
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo.
  echo  [CHYBA] Nenasel jsem Python.
  echo  Pouzijte balicek ClassRally-Windows.zip s pribalenym Pythonem,
  echo  nebo nainstalujte Python 3 z https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo.
echo  Spoustim ClassRally...
echo  Ucitelsky portal se za chvili otevre v prohlizeci.
echo  (Pokud se neotevre, zadejte rucne:  http://127.0.0.1:%PORT%/admin )
echo.
echo  Toto okno NECHTE OTEVRENE - drzi server bezici.
echo  Zavrenim okna server zastavite.
echo.

REM Otevri prohlizec za 2 s (server uz pobezi), server bezi v tomto okne
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start """" http://127.0.0.1:%PORT%/admin"

REM Windows Firewall pri prvnim spusteni zobrazi dotaz - kliknete na "Povolit pristup"
%PY% "%~dp0server.py" --host 0.0.0.0 --port %PORT%

echo.
echo  Server byl zastaven.
pause
