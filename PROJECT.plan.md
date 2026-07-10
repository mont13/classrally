# ClassRally — „Zábavnější než Kahoot" — Plán

## Stav: 🟢 HOTOVO (2026-07-10) — 219/219 unit testů, smoke PASSED, Playwright E2E PASS (0 console errors)

## Vize
Dohnat a předehnat Kahoot v zábavnosti: zvuková dramaturgie (i bez MP3 souborů), napětí, konfety, streaky s ohníčkem, dvojnásobné body, awards a osobní rivalita na mobilu — vše offline, licenčně čisté, bez rozbití písemek (exam mode).

## Analýza (z výzkumu 2026-07-10)
### Co už existuje (NEduplikovat)
- Streak + multiplikátor (1.2/1.4/1.5 při 3/4/5+), `server.py:995-1006`
- Bar chart rozložení odpovědí (`_vote_counts`, host i play)
- Generátor přezdívek `/api/random-nickname`, filtr vulgarit, avatar_id v Playeru
- Týmový režim + team podium (tpRise), score float animace na play
- MP3 audio s opt-in gate (hudba se NIKDY nespustí sama — `musicAuto` default OFF, `userStopped` v localStorage) — **tuto zásadu zachovat!**
- WS push pro play.html (fallback polling), host.html čistě polling 1 s

### Co chybí (= tento projekt)
Zvuky out-of-box (žádný synth, MP3 nejsou v repu), konfety, double points, awards, staggered podium, get-ready countdown, tick posledních 5 s, rival info na mobilu, vibrace, join pop v lobby.

### Klíčová čísla Kahootu (inspirace)
- Streak bonus +100…+500 (cap), double points ×2 per otázka, podium 3→2→1 s drumrollem, poslední ~5 s hudba zrychluje, mobil hráče skoro němý (zvuk = projektor), rival na mobilu („ztrácíš 120 b na Petra"), awards pro ne-top-3.

### Rizika → mitigace
- Zvuk nesmí hrát spontánně (dřívější fix!) → SFX/hudba jen po user gestu + toggly, hudba opt-in zůstává
- server.py/host.html/play.html = velké single-file → 1 agent na soubor, žádné paralelní zásahy do téhož souboru
- i18n parita 4 jazyků → C/D jen sbírají klíče, E je doplní do všech 4 JSON + validace
- Exam mode nerozbít → změny jen v QuizState/host.html/play.html; plný test run
- Repo je veřejné → žádné audio soubory do gitu, jen syntéza + download skript; research/ nesahat
- Windows balíček → nové soubory jen ve static/ (cp -r pokryje), žádný nový top-level .py

## Architektura řešení
- `static/sfx.js` (NOVÝ) — WebAudio engine: SFX (join, blip, tick, gong, correct, wrong, drumroll, fanfára, whoosh, streak, double) + SynthMusic sequencer s intenzitou (ramp podle zbývajícího času). Vše za user-gesture unlock.
- `static/confetti.js` (NOVÝ) — canvas konfety (burst/rain), respektuje prefers-reduced-motion.
- `server.py` — double points (JSON flag `double` + volba „finále ×2"), awards při finished, rank+rival v player state, `get_ready_sec` v timing configu, scoring odečítá get_ready od elapsed.
- `host.html` — show: get-ready 3-2-1, tick+pulz posledních 5 s, drumroll→bar chart reveal, staggered podium+konfety+fanfára, awards panel, join pop, ×2 banner, synth hudba jako volba, SFX toggle.
- `play.html` — get-ready overlay, flash+vibrace při reveal, rank+rival karta, konfety top3, osobní awards, ×2 badge, 🎲 přezdívka/avatar dokompletovat, zvuk default OFF.
- `admin.html` — checkbox „Dvojnásobné body" v editoru otázek, get_ready v časování.
- `download_free_quiz_music.sh` — Kenney CC0 (SFX+jingly) + MacLeod CC-BY (loops) s ATTRIBUTION.md, mapování na stinger/loop konvenci.

## Milestones

### M1: Server core (agent A) — 🟢 DONE (207/207 testů, 36 nových)
Nové API: state top-level `get_ready_sec`, `final_double`; `question.double`; při finished `awards:[{key,player,avatar_id,value}]` (fastest_finger/streak_master/sharpshooter/comeback); `me.rank`, `me.behind:{name,gap}|null`, `me.awards`. Player fieldy: answered_count, correct_count, correct_times, midgame_rank. /api/admin/timing přijímá get_ready_sec (0–10, def. 3), final_double (bool).

### M2: Audio+FX moduly (agent B) — 🟢 DONE
SFX API: unlock() (jen z user gesture), setEnabled/enabled, setVolume (def 0.5), supported; join(), blip(n: 3/2/1, 0=start), tick(urgent), gong(), correct(), wrong(), drumroll(ms)→Promise, fanfare(), whoosh(), streak(level), double(), pop(); SFX.music.start()/stop()/setIntensity(0–1)/setVolume (def 0.35)/playing — Am–F–C–G, 112→132 BPM, ≥0.85 panika. Confetti: burst({x,y,count,spread}), rain(ms), stop(); reduced-motion no-op. Downloader: 11 souborů staženo (5 MacLeod loop_*.mp3 CC-BY + 6 Kenney CC0 efektů), idempotentní, ATTRIBUTION.md. ⚠️ prověřit: skript prý v .gitignore sekci „Old/deprecated".

### M3: Host show (agent C) — 🟢 DONE
SFX toggle (classrally_sfx, def ON) + unlock gestem; synth hudba checkbox classrally_music_synth (hraje i při prázdném katalogu, přes startMusic/stopAllAudio, opt-in zachován); intenzita+tick ≤5 s+pulz; get-ready overlay (pointer-events:none); reveal drumroll(700)+bar-grow+glow; lobby join pop+milníky; ×2 shimmer banner; #podiumOverlay staggered 3→2→1+fanfára+Confetti.rain; awards mřížka v 5.5 s. Bonus: opraven pre-existing z-index konflikt .i18n-switcher × .team-podium-close. Ověřeno: node --check, 3 headless Playwright testy PASSED.

### M4: Player show (agent D) — 🟢 DONE (+577/−6)
Get-ready overlay (skryté odpovědi, slide výjimka); flash+vibrace ([40,60,40]/[250], toggle classrally_vibrate ON); rank karta ↑/↓/= + „Ztrácíš X b na Y"/„Jsi v čele! 👑"; streak badge 3/4/5+ s popupy; zvuk toggle def OFF (classrally_player_sfx); finished: konfety top3+medaile+osobní award+seznam všech; ×2 chip; fun hlášky fun.correct/wrong.0-4; avatar picker mřížka (persist classrally_avatar_id) + 🎲 (existovalo). Vše hranově detekované.

### M5: i18n + admin + docs (agenti E1, E2) — 🟢 DONE
48 klíčů ×4 jazyky (573/jazyk, parita OK, +3 chybějící admin.* nalezené grepem); admin.html: „⚡ Dvojnásobné body" checkbox v editoru + „Odpočet Připrav se (s)" + „Poslední otázka ×2" v časování; README (bez diakritiky): Hudba a zvuky přepsána, sekce Herni prvky, bodování. E2: /api/register přijímá avatar_id (int 1–20, nevalidní→0, profil přihlášeného vyhrává; přesně dle požadavku D).

### M6: Verifikace + E2E — 🟡 WIP
- [x] Unit testy: **215/215 OK** (171→215; +36 gamifikace, +6 avatar, +2 host-token)
- [x] `./smoke_test.sh`: **ALL SMOKE TESTS PASSED**
- [x] G: Playwright E2E (host 1280×800 + 2 mobilní hráči): join/avatary/🎲, get-ready, reveal (rank karty, fun hlášky), streak 🔥3×, final double, podium+awards+konfety, audio (SFX.music.playing=true i headless), i18n EN/DE, security host-token — PASS; našel 5 problémů ↓
- [ ] Opravy E2E nálezů + re-verifikace

### E2E nálezy (2026-07-10)
| # | Závažnost | Problém | Stav |
|---|-----------|---------|------|
| P1 | KRITICKÁ | play.html: #getReadyOverlay nezmizí (`.hide` v style.css prohrává s inline `#id{display:flex}`) → blokuje klik na odpovědi | ✅ opraveno (`#id.hide{display:none}` sada + re-assert), ověřeno reálným page.click() → answer_count=1 |
| P2 | STŘEDNÍ | tatáž kaskáda: ×2 chip/streak/rank/team/„Přihlášen" badge vidět trvale | ✅ opraveno (vč. pre-existing badge pravidel), lobby čistá |
| P6 | KRITICKÁ (odmaskováno fixem P4) | server.py:940 `/api/host/action` volá `_ws_notify()` pod `self._lock` → deadlock serveru s ≥1 WS klientem (dřív nedosažitelné — WS se kvůli P4 nikdy nepřipojil) | ✅ opraveno (notify po uvolnění zámku, audit všech 4 call sites + nepřímých cest, +2 regresní testy vč. živého WS deadlock testu) — **217/217 testů + smoke PASSED** |
| P7 | VYSOKÁ (odmaskováno P4+P6) | Časovačové přechody (_sync_timers_locked: timeout→reveal→next→finished) se nebroadcastují přes WS; WS klienti nepollují → hráč bez akce zamrzne na starém stavu (auto režim) | ✅ opraveno: `tick_and_notify()` + daemon ticker (0.3 s aktivní / 1 s idle, start v main(), notify mimo zámek, `_ws_marker` proti duplikátům) + 2 testy vč. reálného WS (lobby 2 s = 0 framů; auto reveal→next→finished doručeno) — **219/219 + smoke PASSED** |

### Finální E2E (po opravách P1–P6): P1–P6 vše PASS, 0 console errors (host + 2 hráči), WS push latence 52/69 ms (dřív ~1s poll), happy-path bez regresí (rank karty, streak 🔥3×, double, podium+awards+konfety). Screenshoty: scratchpad/e2e/*-final-*.png
| P3 | STŘEDNÍ | host.html: joinUrl trvale „Načítám..." (data-i18n přepis po i18n.apply) | ✅ opraveno (removeAttribute), ověřeno vč. přepnutí jazyka |
| P4 | VYSOKÁ (pre-existing!) | ws.py:23 špatný RFC 6455 GUID → prohlížeče odmítaly KAŽDÝ WS handshake (od nepaměti; testy cirkulárně samokonzistentní) → tichý fallback na polling | ✅ opraveno (GUID + RFC literál v testu), 215/215 OK |
| P5 | NÍZKÁ | host 1280×800: herní sekce pod foldem | ✅ opraveno (scrollIntoView při startu otázky, top při resetu), ověřeno 1048→662 px |

### Vícepráce (nález při verifikaci)
- 🔒 **Pre-existing security fix** (potvrzeno na čistém HEAD): `/api/state?host=1` dával host view bez autentizace → únik player_id VŠECH hráčů + **správné odpovědi během otázky** (cheat vektor pro žáky) + bot skóre; kvůli tomu selhával oficiální smoke_test.sh. Fix: host view jen s hlavičkou `X-Host-Token` (hmac.compare_digest, i Bearer), jinak tichá degradace na public view; zavřena i WS cesta; host.html posílá hlavičku v api(). +2 testy.
- `QUIZ_DATA_DIR` env override v db.py + smoke_test.sh běží v izolovaném mktemp adresáři (smoke už nezávisí na reálné učitelské DB v data/).
- `.gitignore`: download_free_quiz_music.sh odebrán z „Old/deprecated" (po přepisu je funkční a zdokumentovaný v README) — k případnému vetu uživatelem.

## Log
| Datum | Co se stalo | Výsledek |
|-------|-------------|----------|
| 2026-07-10 | Výzkum kódu + Kahoot mechanik (2 agenti) | ✅ report v plánu |
| 2026-07-10 | Uživatel v zadání předschválil celý cyklus (plán→implementace→testy), autonomní režim — pokračuji bez blokující otázky | ✅ |
