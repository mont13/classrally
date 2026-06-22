#!/usr/bin/env bash
# ===========================================================================
# Sestavi ClassRally-Windows.zip = prenosny Python + aplikace + spousteci .bat
# Spustitelne na Linuxu i Macu (jen stahne a zabali, NIC nekompiluje).
# Vysledek: dist/ClassRally-Windows.zip -> ucitel rozbali a 2x klikne na .bat.
#
# Pouziti:  ./make-windows-package.sh
# Vyzaduje: curl (nebo wget), unzip, zip
# ===========================================================================
set -euo pipefail

PYVER="3.11.9"
PYTAG="python311"   # nazev ._pth souboru odpovida verzi (3.11 -> python311)
ARCH="amd64"        # 64-bit Windows; pro stare 32-bit zmente na "win32"
EMBED_URL="https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-embed-${ARCH}.zip"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="${ROOT}/windows-build"
APP="${BUILD}/ClassRally"
DIST="${ROOT}/dist"
CACHE="${ROOT}/.cache"

echo ">> Cistim predchozi build..."
rm -rf "${BUILD}"
mkdir -p "${APP}/python" "${DIST}" "${CACHE}"

EMBED_ZIP="${CACHE}/python-${PYVER}-embed-${ARCH}.zip"
if [ ! -f "${EMBED_ZIP}" ]; then
  echo ">> Stahuji prenosny Python ${PYVER} (${ARCH})..."
  if command -v curl >/dev/null 2>&1; then
    curl -fL "${EMBED_URL}" -o "${EMBED_ZIP}"
  else
    wget -O "${EMBED_ZIP}" "${EMBED_URL}"
  fi
else
  echo ">> Pouzivam stazeny Python z cache: ${EMBED_ZIP}"
fi

echo ">> Rozbaluji Python do balicku..."
unzip -q -o "${EMBED_ZIP}" -d "${APP}/python"

# Pridej korenovou slozku aplikace na sys.path (relativni k python.exe = "..")
PTH="${APP}/python/${PYTAG}._pth"
if [ -f "${PTH}" ]; then
  # -F = fixed string (jinak by ".." byl regex "dva libovolne znaky" a matchnul radek ".")
  if ! grep -Fqx ".." "${PTH}"; then
    printf '..\r\n' >> "${PTH}"   # CRLF jako zbytek souboru (Windows)
  fi
else
  echo ">> POZOR: ${PTH} nenalezen (jina verze Pythonu?). Zkontrolujte nazev ._pth."
fi

echo ">> Kopiruji aplikaci..."
cp "${ROOT}/server.py" "${ROOT}/qrgen.py" "${APP}/"
cp -r "${ROOT}/static" "${APP}/static"
cp -r "${ROOT}/questions" "${APP}/questions"
mkdir -p "${APP}/history" "${APP}/static/audio"

echo ">> Kopiruji spousteci skripty a navody..."
cp "${ROOT}/windows/"*.bat "${APP}/" 2>/dev/null || true
cp "${ROOT}/windows/"*.txt "${APP}/" 2>/dev/null || true

echo ">> Baleni do ZIP..."
ZIPOUT="${DIST}/ClassRally-Windows.zip"
rm -f "${ZIPOUT}"
( cd "${BUILD}" && zip -q -r "${ZIPOUT}" "ClassRally" )

SIZE="$(du -h "${ZIPOUT}" | cut -f1)"
echo ""
echo "================================================================"
echo " Hotovo:  ${ZIPOUT}  (${SIZE})"
echo " Predejte ucitelovi: rozbalit a 2x kliknout na Spustit-ClassRally.bat"
echo "================================================================"
