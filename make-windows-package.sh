#!/usr/bin/env bash
# Linux/macOS -> Windows x64 ZIP; optional per-user NSIS installer.
# Only the existing portable Python download from python.org is permitted.
set -euo pipefail
PYVER="3.11.9"
PYTAG="python311"
ARCH="amd64"
EMBED_URL="https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-embed-${ARCH}.zip"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="${ROOT}/dist"
CACHE="${ROOT}/.cache"
mkdir -p "${ROOT}/windows-build" "${DIST}" "${CACHE}"
# Fresh payload: never reuse a tree that could contain a teacher's runtime data.
# Older builds are preserved; this script deletes no source or data folders.
BUILD="$(mktemp -d "${ROOT}/windows-build/package.XXXXXX")"
APP="${BUILD}/ClassRally"
mkdir -p "${APP}/python"
EMBED_ZIP="${CACHE}/python-${PYVER}-embed-${ARCH}.zip"
if [ ! -f "${EMBED_ZIP}" ]; then
  echo ">> Stahuji přenosný Python ${PYVER} pro Windows x64..."
  PART="${BUILD}/python-download.zip"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --max-time 180 --max-filesize 20000000 "${EMBED_URL}" -o "${PART}"
  else
    wget --timeout=180 -O "${PART}" "${EMBED_URL}"
  fi
  unzip -tq "${PART}"
  mv "${PART}" "${EMBED_ZIP}"
else
  echo ">> Používám přenosný Python z cache."
fi
unzip -q "${EMBED_ZIP}" -d "${APP}/python"
PTH="${APP}/python/${PYTAG}._pth"
[ -f "${PTH}" ] || { echo "CHYBA: chybí ${PYTAG}._pth" >&2; exit 1; }
printf '..\r\n' >> "${PTH}"
echo ">> Kopíruji pouze veřejné součásti aplikace..."
python3 - "${ROOT}" "${APP}" <<'PY'
from pathlib import Path
import shutil
import subprocess
import sys
root, app = map(Path, sys.argv[1:])
for name in ('server.py', 'qrgen.py', 'db.py', 'ws.py', 'mailer.py', 'bugreport.py', 'help_assistant.py', 'LICENSE'):
    shutil.copy2(root / name, app / name)
# No untracked/private uploads, question banks, music, data, history or secrets.
paths = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--', 'static', 'questions'])
for raw in paths.split(b'\0'):
    if not raw:
        continue
    path = Path(raw.decode('utf-8'))
    if (path.parts[:2] in [('static', 'uploads'), ('questions', 'users')]
            or (path.parts[:2] == ('static', 'audio') and path.suffix.lower() not in ('.md', '.txt'))):
        continue
    source = root / path
    if not source.is_file() or source.is_symlink():
        raise SystemExit(f'Nelze přibalit veřejný soubor: {path}')
    dest = app / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
for source in sorted((root / 'windows').iterdir()):
    if source.suffix.lower() in ('.bat', '.cmd', '.ps1', '.txt', '.py') and source.is_file():
        shutil.copy2(source, app / source.name)
PY
echo ">> Vytvářím ZIP..."
( cd "${BUILD}" && zip -q -r ClassRally-Windows.zip ClassRally )
mv "${BUILD}/ClassRally-Windows.zip" "${DIST}/ClassRally-Windows.zip"
if command -v makensis >/dev/null 2>&1; then
  makensis -V2 "-DAPP_SOURCE=${APP}" "-DOUTPUT_FILE=${DIST}/ClassRally-Windows-Setup.exe" "${ROOT}/windows/ClassRally.nsi"
else
  echo ">> makensis není dostupný: NSIS instalátor přeskakuji; ZIP je hotový."
fi
echo "Hotovo: ${DIST}/ClassRally-Windows.zip ($(du -h "${DIST}/ClassRally-Windows.zip" | cut -f1))"
echo "Rozbalte celý ZIP a spusťte Spustit-ClassRally.bat."
