#!/usr/bin/env bash
#
# download_free_quiz_music.sh — stáhne legální (CC BY / CC0) kvízovou hudbu
# a zvukové efekty do static/audio/, odkud je server nabídne hostiteli.
#
# Server třídí soubory PODLE NÁZVU (server.py::list_audio_tracks):
#   název obsahuje  stinger|hit|reveal|correct|win|lock|end|ding  → efekt (stinger)
#   vše ostatní                                                   → hudba (loop)
# Proto konvence:  loop_*.mp3  = smyčky,  win_/lock_/correct_/end_*.ogg = efekty.
#
# Zdroje:
#   1) Kevin MacLeod / incompetech.com — kvízové smyčky (CC BY 4.0, nutná atribuce)
#   2) Kenney.nl — interface-sounds + music-jingles (CC0, bez povinné atribuce)
#   3) Fallback: OpenGameArt — Juhani Junkala, 512 retro SFX (CC0) — jen když Kenney selže
#
# Vlastnosti: idempotentní (existující soubory přeskočí), nepadá kvůli jednomu
# nefunkčnímu zdroji, funguje bez sudo, potřebuje jen curl (+ unzip pro Kenney).
# Audio soubory jsou v .gitignore — do veřejného repa se NIKDY necommitují.
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUDIO_DIR="$SCRIPT_DIR/static/audio"
ATTR_OUT="$AUDIO_DIR/ATTRIBUTION.md"
MIN_MP3_BYTES=10240   # menší stažený soubor = nejspíš chybová stránka
MIN_OGG_BYTES=2048    # malé UI cinknutí z ověřeného ZIPu smí být drobné

# ── výsledky pro závěrečnou tabulku ─────────────────────────────────
RESULTS=()   # řádky "STAV|soubor|zdroj"
add_result() { RESULTS+=("$1|$2|$3"); }

say()  { printf '%s\n' "$*"; }
warn() { printf 'VAROVÁNÍ: %s\n' "$*" >&2; }

# ── kontrola nástrojů ───────────────────────────────────────────────
if ! command -v curl >/dev/null 2>&1; then
  say "CHYBA: chybí 'curl'. Nainstalujte jej (např. 'sudo apt install curl') a spusťte skript znovu."
  exit 1
fi
HAVE_UNZIP=1
if ! command -v unzip >/dev/null 2>&1; then
  HAVE_UNZIP=0
  warn "chybí 'unzip' — Kenney/OpenGameArt balíčky (efekty) budou přeskočeny. Nainstalujte: sudo apt install unzip"
fi
HAVE_FILE=0
command -v file >/dev/null 2>&1 && HAVE_FILE=1

mkdir -p "$AUDIO_DIR"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── pomocné funkce ──────────────────────────────────────────────────

# je soubor validní audio? $1=cesta $2=min. velikost
looks_like_audio() {
  local path="$1" min="$2" size kind
  [ -f "$path" ] || return 1
  size=$(stat -c%s "$path" 2>/dev/null || wc -c < "$path" 2>/dev/null || echo 0)
  [ "$size" -ge "$min" ] || return 1
  if [ "$HAVE_FILE" -eq 1 ]; then
    kind=$(file -b "$path" 2>/dev/null || true)
    case "$kind" in
      *[Aa]udio*|*MPEG*|*MP3*|*Ogg*|*WAVE*|*AIFF*) return 0 ;;
      *HTML*|*text*) return 1 ;;
      *) return 0 ;;  # neznámý popis, ale velikost sedí → nechme projít
    esac
  fi
  return 0
}

# HEAD ověření URL (nefunkční zdroj jen přeskočíme)
url_ok() {
  curl -sSfI -L --connect-timeout 15 --max-time 40 -o /dev/null "$1" 2>/dev/null
}

# stáhne MP3: $1=url $2=cílový soubor $3=popis zdroje
fetch_mp3() {
  local url="$1" out="$2" src="$3" tmp
  local name; name="$(basename "$out")"
  if [ -f "$out" ] && looks_like_audio "$out" "$MIN_MP3_BYTES"; then
    add_result "PŘESKOČENO" "$name" "$src (už existuje)"
    return 0
  fi
  if ! url_ok "$url"; then
    warn "zdroj nedostupný: $url"
    add_result "SELHALO" "$name" "$src (URL nedostupná)"
    return 1
  fi
  say "  Stahuji: $name"
  tmp="$TMP_DIR/$name.part"
  if curl -sSfL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 300 \
       -o "$tmp" "$url" && looks_like_audio "$tmp" "$MIN_MP3_BYTES"; then
    mv "$tmp" "$out"
    add_result "STAŽENO" "$name" "$src"
  else
    rm -f "$tmp"
    warn "stažení selhalo nebo soubor není validní audio: $url"
    add_result "SELHALO" "$name" "$src"
    return 1
  fi
}

# vybalí jeden soubor ze ZIPu: $1=zip $2=cesta_v_zipu $3=cílový název $4=popis zdroje
extract_one() {
  local zip="$1" member="$2" outname="$3" src="$4"
  local out="$AUDIO_DIR/$outname"
  if [ -f "$out" ] && looks_like_audio "$out" "$MIN_OGG_BYTES"; then
    add_result "PŘESKOČENO" "$outname" "$src (už existuje)"
    return 0
  fi
  if unzip -p "$zip" "$member" > "$TMP_DIR/extract.part" 2>/dev/null \
     && looks_like_audio "$TMP_DIR/extract.part" "$MIN_OGG_BYTES"; then
    mv "$TMP_DIR/extract.part" "$out"
    add_result "STAŽENO" "$outname" "$src ($member)"
  else
    rm -f "$TMP_DIR/extract.part"
    warn "v ZIPu chybí nebo je nevalidní: $member"
    add_result "SELHALO" "$outname" "$src ($member chybí)"
    return 1
  fi
}

# najde přímý ZIP na stránce Kenney.nl (hash v cestě se časem mění): $1=slug
kenney_zip_url() {
  local slug="$1"
  curl -sSfL --connect-timeout 15 --max-time 40 "https://kenney.nl/assets/$slug" 2>/dev/null \
    | grep -oE "https://kenney\.nl/media/pages/assets/$slug/[A-Za-z0-9-]+/kenney_[A-Za-z0-9._-]+\.zip" \
    | head -1
}

# stáhne ZIP (s fallback URL): $1=slug $2=fallback_url $3=cíl
fetch_kenney_zip() {
  local slug="$1" fallback="$2" dest="$3" url
  url="$(kenney_zip_url "$slug" || true)"
  [ -n "$url" ] || url="$fallback"
  if ! url_ok "$url"; then
    [ "$url" != "$fallback" ] && url="$fallback"
    url_ok "$url" || return 1
  fi
  say "  Stahuji balíček: $slug"
  curl -sSfL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 300 \
    -o "$dest" "$url" 2>/dev/null || return 1
  [ "$(stat -c%s "$dest" 2>/dev/null || echo 0)" -ge 100000 ] || return 1
}

# ═════════════════════════════════════════════════════════════════════
# 1) Incompetech — kvízové smyčky (CC BY 4.0)
# ═════════════════════════════════════════════════════════════════════
say ""
say "── 1/2 Hudební smyčky — Kevin MacLeod / incompetech.com (CC BY 4.0)"

INC_BASE="https://incompetech.com/music/royalty-free/mp3-royaltyfree"
INCOMPETECH_OK=0
# "Název skladby|url-encoded soubor|cílový název" (cíl NESMÍ obsahovat klasifikační
# klíčová slova stinger/hit/reveal/correct/win/lock/end/ding → zůstane smyčkou)
INC_TRACKS=(
  "Thinking Music|Thinking%20Music|loop_thinking_music.mp3"
  "Sneaky Snitch|Sneaky%20Snitch|loop_sneaky_snitch.mp3"
  "Quirky Dog|Quirky%20Dog|loop_quirky_dog.mp3"
  "Monkeys Spinning Monkeys|Monkeys%20Spinning%20Monkeys|loop_monkeys_spinning_monkeys.mp3"
  "Local Forecast - Elevator|Local%20Forecast%20-%20Elevator|loop_local_forecast_elevator.mp3"
)
for entry in "${INC_TRACKS[@]}"; do
  IFS='|' read -r title urlname outname <<< "$entry"
  if fetch_mp3 "$INC_BASE/$urlname.mp3" "$AUDIO_DIR/$outname" "incompetech: $title"; then
    INCOMPETECH_OK=1
  fi
done

# ═════════════════════════════════════════════════════════════════════
# 2) Kenney.nl — zvukové efekty a jingly (CC0)
# ═════════════════════════════════════════════════════════════════════
say ""
say "── 2/2 Zvukové efekty — Kenney.nl (CC0)"

# "cesta v ZIPu|cílový název" — cílové názvy OBSAHUJÍ klíčové slovo → server je
# zařadí mezi efekty (stingers)
IFS_MEMBERS=(
  "Audio/click_001.ogg|lock_click.ogg"
  "Audio/confirmation_001.ogg|correct_ding.ogg"
  "Audio/error_004.ogg|wrong_hit.ogg"
  "Audio/question_004.ogg|reveal_question.ogg"
  "Audio/bong_001.ogg|end_gong.ogg"
)
JINGLE_MEMBERS=(
  "Audio/Pizzicato jingles/jingles_PIZZI07.ogg|win_jingle.ogg"
)

KENNEY_OK=0
process_kenney_pack() { # $1=slug $2=fallback_url $3…=members ("cesta|cíl")
  local slug="$1" fallback="$2"
  shift 2
  local members=("$@")
  local missing=0 entry member outname zip
  for entry in "${members[@]}"; do
    outname="${entry#*|}"
    if ! { [ -f "$AUDIO_DIR/$outname" ] && looks_like_audio "$AUDIO_DIR/$outname" "$MIN_OGG_BYTES"; }; then
      missing=1
    fi
  done
  if [ "$missing" -eq 0 ]; then
    for entry in "${members[@]}"; do
      add_result "PŘESKOČENO" "${entry#*|}" "kenney/$slug (už existuje)"
    done
    KENNEY_OK=1
    return 0
  fi
  zip="$TMP_DIR/kenney_$slug.zip"
  if ! fetch_kenney_zip "$slug" "$fallback" "$zip"; then
    warn "Kenney balíček '$slug' se nepodařilo stáhnout"
    for entry in "${members[@]}"; do
      add_result "SELHALO" "${entry#*|}" "kenney/$slug (ZIP nedostupný)"
    done
    return 1
  fi
  for entry in "${members[@]}"; do
    member="${entry%%|*}"; outname="${entry#*|}"
    extract_one "$zip" "$member" "$outname" "kenney/$slug" && KENNEY_OK=1
  done
}

if [ "$HAVE_UNZIP" -eq 1 ]; then
  # fallback URL ověřené 2026-07 (hash v cestě se může změnit — pak zafunguje scraping)
  process_kenney_pack "interface-sounds" \
    "https://kenney.nl/media/pages/assets/interface-sounds/fa43c1dd4d-1677589452/kenney_interface-sounds.zip" \
    "${IFS_MEMBERS[@]}"
  process_kenney_pack "music-jingles" \
    "https://kenney.nl/media/pages/assets/music-jingles/f37e530b9e-1677590399/kenney_music-jingles.zip" \
    "${JINGLE_MEMBERS[@]}"
else
  for entry in "${IFS_MEMBERS[@]}" "${JINGLE_MEMBERS[@]}"; do
    add_result "SELHALO" "${entry#*|}" "kenney (chybí unzip)"
  done
fi

# ── Fallback: OpenGameArt (Juhani Junkala, CC0) — jen když Kenney selhal ──
OGA_USED=0
if [ "$KENNEY_OK" -eq 0 ] && [ "$HAVE_UNZIP" -eq 1 ]; then
  say ""
  say "── Fallback: OpenGameArt — Juhani Junkala, 512 retro SFX (CC0)"
  OGA_URL="https://opengameart.org/sites/default/files/The%20Essential%20Retro%20Video%20Game%20Sound%20Effects%20Collection%20%5B512%20sounds%5D.zip"
  OGA_ZIP="$TMP_DIR/oga512.zip"
  if url_ok "$OGA_URL" \
     && curl -sSfL --retry 2 --retry-delay 3 --connect-timeout 20 --max-time 900 \
          -o "$OGA_ZIP" "$OGA_URL" 2>/dev/null \
     && [ "$(stat -c%s "$OGA_ZIP" 2>/dev/null || echo 0)" -ge 1000000 ]; then
    # názvy uvnitř kolekce se liší podle verze → vybereme první rozumné kandidáty
    map_oga() { # $1=vzor v názvu $2=cílový název
      local member
      member="$(unzip -Z1 "$OGA_ZIP" 2>/dev/null | grep -i "$1" | grep -iE '\.wav$' | head -1 || true)"
      if [ -n "$member" ]; then
        extract_one "$OGA_ZIP" "$member" "$2" "opengameart/junkala" && OGA_USED=1
      else
        add_result "SELHALO" "$2" "opengameart (vzor '$1' nenalezen)"
      fi
    }
    map_oga "jingle"  "win_jingle.wav"
    map_oga "click"   "lock_click.wav"
    map_oga "coin"    "correct_ding.wav"
  else
    warn "OpenGameArt fallback nedostupný"
    add_result "SELHALO" "(OpenGameArt balíček)" "opengameart (nedostupné)"
  fi
fi

# ═════════════════════════════════════════════════════════════════════
# 3) ATTRIBUTION.md — přehled licencí (CC BY vyžaduje atribuci!)
# ═════════════════════════════════════════════════════════════════════
{
  echo "# Audio — zdroje a licence"
  echo
  echo "Soubory v této složce se do repozitáře necommitují (.gitignore)."
  echo "Tento přehled generuje \`download_free_quiz_music.sh\`."
  echo
  echo "## Kevin MacLeod — incompetech.com (CC BY 4.0 — atribuce POVINNÁ)"
  echo
  any=0
  for entry in "${INC_TRACKS[@]}"; do
    IFS='|' read -r title _urlname outname <<< "$entry"
    [ -f "$AUDIO_DIR/$outname" ] && { echo "- \"$title\" → \`$outname\`"; any=1; }
  done
  [ -f "$AUDIO_DIR/tension-loop.mp3" ]    && { echo "- \"Rynos Theme\" → \`tension-loop.mp3\`"; any=1; }
  [ -f "$AUDIO_DIR/reveal-stinger.mp3" ]  && { echo "- \"Discovery Hit\" → \`reveal-stinger.mp3\`"; any=1; }
  [ "$any" -eq 0 ] && echo "- (žádné soubory z tohoto zdroje)"
  echo
  echo "Licence: Creative Commons Attribution 4.0 International"
  echo "(https://creativecommons.org/licenses/by/4.0/)"
  echo
  echo "Povinný kredit — uveďte na slajdu / v popisu nahrávky:"
  echo
  echo "> Music by Kevin MacLeod (incompetech.com),"
  echo "> licensed under CC BY 4.0 (creativecommons.org/licenses/by/4.0/)"
  echo
  echo "## Kenney.nl (CC0 1.0 — public domain, atribuce vítaná, ne povinná)"
  echo
  any=0
  for entry in "${IFS_MEMBERS[@]}"; do
    outname="${entry#*|}"
    [ -f "$AUDIO_DIR/$outname" ] && { echo "- \`$outname\` — Interface Sounds (https://kenney.nl/assets/interface-sounds)"; any=1; }
  done
  for entry in "${JINGLE_MEMBERS[@]}"; do
    outname="${entry#*|}"
    [ -f "$AUDIO_DIR/$outname" ] && { echo "- \`$outname\` — Music Jingles (https://kenney.nl/assets/music-jingles)"; any=1; }
  done
  [ "$any" -eq 0 ] && echo "- (žádné soubory z tohoto zdroje)"
  echo
  if [ "$OGA_USED" -eq 1 ]; then
    echo "## OpenGameArt — Juhani Junkala (CC0 1.0)"
    echo
    echo "- retro SFX z kolekce \"The Essential Retro Video Game Sound Effects Collection\""
    echo "  (https://opengameart.org/content/512-sound-effects-8-bit-style)"
    echo
  fi
  echo "## Ostatní soubory"
  echo
  echo "Soubory přidané ručně (mimo tento skript) si licenčně ohlídejte sami:"
  any=0
  for f in "$AUDIO_DIR"/*.mp3 "$AUDIO_DIR"/*.ogg "$AUDIO_DIR"/*.wav "$AUDIO_DIR"/*.m4a "$AUDIO_DIR"/*.aac; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    known=0
    for entry in "${INC_TRACKS[@]}"; do [ "${entry##*|}" = "$base" ] && known=1; done
    for entry in "${IFS_MEMBERS[@]}" "${JINGLE_MEMBERS[@]}"; do [ "${entry#*|}" = "$base" ] && known=1; done
    case "$base" in tension-loop.mp3|reveal-stinger.mp3) known=1 ;; esac
    [ "$OGA_USED" -eq 1 ] && case "$base" in win_jingle.wav|lock_click.wav|correct_ding.wav) known=1 ;; esac
    [ "$known" -eq 0 ] && { echo "- \`$base\`"; any=1; }
  done
  [ "$any" -eq 0 ] && echo "- (žádné)"
} > "$ATTR_OUT"

# ═════════════════════════════════════════════════════════════════════
# 4) Souhrn
# ═════════════════════════════════════════════════════════════════════
say ""
say "═══════════════════ SOUHRN ═══════════════════"
printf '%-12s %-36s %s\n' "STAV" "SOUBOR" "ZDROJ"
printf '%-12s %-36s %s\n' "----" "------" "-----"
N_OK=0; N_SKIP=0; N_FAIL=0
for row in "${RESULTS[@]}"; do
  IFS='|' read -r st fname src <<< "$row"
  printf '%-12s %-36s %s\n' "$st" "$fname" "$src"
  case "$st" in
    STAŽENO)    N_OK=$((N_OK+1)) ;;
    PŘESKOČENO) N_SKIP=$((N_SKIP+1)) ;;
    *)          N_FAIL=$((N_FAIL+1)) ;;
  esac
done
say ""
say "Staženo: $N_OK   Přeskočeno (už existuje): $N_SKIP   Selhalo: $N_FAIL"
say "Atribuce: $ATTR_OUT"
say ""
if [ "$N_OK" -eq 0 ] && [ "$N_SKIP" -eq 0 ]; then
  say "Nic se nepodařilo stáhnout — zkontrolujte připojení k internetu."
  exit 1
fi
say "Hotovo. Hudba se hostiteli nabídne automaticky (server třídí podle názvů)."
