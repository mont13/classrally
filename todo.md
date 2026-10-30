# ClassRally - Roadmap k plnohodnotne platforme

## Stav k 30. 10. 2026

Sjednocený audit stavů funkcí; dílčí funkce zůstávají otevřené.

- Z funkcí zbývá formát Quizlet, PDF export výsledků, ligy a verzování REST API.
- Dílčí jsou power-ups: dvojité body už fungují, ochrana série a hráčské power-ups chybí.
- Body napříč hrami a herní ocenění existují; chybí levely a trvalé odznaky za počet her/skóre.
- Start bez admin hesla má jen běžný výpis; výrazné varování a volitelné jednorázové admin heslo chybí.
- Zbývá ověřit skutečné volání Ollama/OpenAI, obě větve uložení AI sady a přepínání jazyků na všech stránkách.
- Chromium ověřilo hru s výběrem a otevřenou odpovědí včetně podia; zbývá mobilní ověření ostatních typů a dlouhých odpovědí.
- Hráčské zvuky odpovědí používají syntézu; požadavek na MP3 zůstává otevřený.
- PWA ukládá rozhraní, živá hra potřebuje server; tmavý režim je automatický na hráčské stránce.
- Starší přehledy a počty testů níže jsou historické; audit nemění uzavřený původní plán.

## Analyza: Co Kahoot dela dobre a co nam chybi

### Co uzivatele na Kahootu nejvic chvali
1. **Nulova bariera vstupu** — PIN + prezdivka, zadny ucet, zadna instalace
2. **Ikonicka hudba a zvuky** — countdown hudba je kulturni mem, vytvari Pavlovovsky engagement
3. **Live spolecny zazitek** — vsichni ve stejne mistnosti, projektor, emoce
4. **Okamzita zpetna vazba** — hned vidim jestli spravne/spatne + body
5. **Leaderboard po kazde otazce** — socialni srovnani, motivace
6. **Podium na konci** — verejne uznani top 3
7. **100M+ komunitnich kvizu** — ucitel nemusi nic vytvaret

### Co uživatelé na Kahootu kritizují
1. **Free tier omezen na 10 hráčů** (dříve bylo víc)
2. **Agresivní upselling a matoucí pricing** (6+ plánů)
3. **Povrchní učení** — žádné vysvětlení PROČ, rychlost > porozumění
4. **Bot spam** — snadné zaplavit hru boty
5. **Nevhodné přezdívky** — omezené filtrování
6. **Opadající novost** — po čase se formát "4 odpovědi" omrzí

### Co ClassRally UŽ má (a funguje dobře)
- ✅ Zero-friction join (PIN + přezdívka, žádný účet)
- ✅ 4 fáze hry (lobby → otázka → vyhodnocení → konec)
- ✅ Bodování za rychlost (600 základ + až 400 bonus)
- ✅ Leaderboard + podium top 3
- ✅ QR kód pro připojení
- ✅ Audio systém (MP3 + WebAudio synth fallback)
- ✅ Admin portál se správou sad otázek
- ✅ AI generátor otázek (Ollama)
- ✅ Historie her
- ✅ Lokální běh bez internetu
- ✅ Docker deployment
- ✅ Režim písemky se známkováním, měkkým dohledem a CSV exportem
- ✅ 93 testů
### Co uzivatele na Kahootu kritizuji
1. **Free tier omezen na 10 hracu** (drive bylo vic)
2. **Agresivni upselling a matouci pricing** (6+ planu)
3. **Povrchni uceni** — zadne vysvetleni PROC, rychlost > porozumeni
4. **Bot spam** — snadne zaplavit hru boty
5. **Nevhodne prezdivky** — omezene filtrovani
6. **Opadajici novost** — po case se format "4 odpovedi" omrzi

---

## Co ClassRally UZ ma

- [x] Zero-friction join (PIN + prezdivka, zadny ucet)
- [x] 4 faze hry (lobby -> otazka -> vyhodnoceni -> konec)
- [x] Bodovani za rychlost (600 zaklad + az 400 bonus)
- [x] Leaderboard + podium top 3
- [x] QR kod pro pripojeni
- [x] Audio system (MP3 přehrávání)
- [x] Admin portal se spravou sad otazek
- [x] AI generator otazek (univerzální — Ollama, OpenAI, Groq, Together, LM Studio)
- [x] Historie her (JSON + SQLite)
- [x] Lokalni beh bez internetu
- [x] Docker deployment
- [x] 155 unit testu + integracni testy

---

## FAZE 1: Zakladni vylepseni — HOTOVO

### 1.1 SQLite databaze + uzivatelske ucty
- [x] `db.py` — SQLite schema (WAL mod, thread-safe), init_db(), migrace JSON historie
- [x] Schema: users, sessions, classes, class_members, games, game_players
- [x] Registrace (volitelna!) — `POST /api/auth/register` (nickname + heslo + role)
- [x] Prihlaseni — `POST /api/auth/login` -> session token (8h TTL)
- [x] Profil — `GET /api/auth/profile` (skore, pocet her, tridy)
- [x] Odhlaseni — `POST /api/auth/logout`
- [x] Role: student (default) | teacher (vyzaduje `--teacher-code`)
- [x] Hra funguje BEZ registrace (jako drive) — anonymni hraci
- [x] `Player.user_id` propojeni — registrovany hrac ma historii v profilu

### 1.2 Skupiny / Tridy
- [x] `POST /api/classes/create` — ucitel vytvori tridu (6-znakovy join code)
- [x] `POST /api/classes/join` — student se pripoji kodem
- [x] `GET /api/classes` — seznam mych trid
- [x] `GET /api/classes/<id>/members` — roster tridy
- [x] `GET /api/classes/<id>/history` — historie her tridy
- [x] `GET /api/classes/<id>/progress` — progress studentu v case
- [x] `DELETE /api/classes/<id>/delete` — smazani tridy (jen ucitel)

### 1.3 Nove typy otazek (5 typu)
- [x] `choice` — klasicke 4 odpovedi (stavajici)
- [x] `truefalse` — 2 velka tlacitka Pravda/Nepravda
- [x] `multiselect` — checkboxy, vyber vsechny spravne (Jaccard scoring)
- [x] `ordering` — serazeni polozek (Kendall tau scoring)
- [x] `openended` — textovy input, fuzzy matching (Levenshtein <= 2)
- [x] Validace vsech typu v `_validate_question()`
- [x] Zpetne kompatibilni — typ je volitelny, default = choice

### 1.4 Streak / Combo system
- [x] `Player.streak` + `Player.max_streak`
- [x] Multiplikator: 1.0x (1-2), 1.2x (3), 1.4x (4), 1.5x (5+)
- [x] Streak badge na player obrazovce (od 3, pulse od 5)
- [x] Streak shoutout banner na host obrazovce
- [x] Fire emoji v zebricku u hracu se streak >= 3
- [x] Reset streaku pri spatne odpovedi nebo timeoutu

### 1.5 WebSocket real-time
- [x] `ws.py` — RFC 6455 implementace (stdlib only: hashlib, base64, struct)
- [x] Handshake, encode/decode frames, ping/pong, close
- [x] `WSConnectionManager` — broadcast, heartbeat (30s ping)
- [x] `/api/ws?player_id=X` nebo `/api/ws?host=1`
- [x] Automaticky fallback na polling (1s interval) kdyz WS selze
- [x] Exponencialni backoff pro WS reconnect

### 1.6 PWA
- [x] `static/manifest.json` — nazev, ikony, standalone mod
- [x] `static/sw.js` — service worker (network-first API, cache-first static)
- [x] `static/offline.html` — offline fallback stranka
- [x] Service worker registrace v play.html
- [x] PWA ikony — `icon-192.png`, `icon-512.png`, `favicon.png` (generovane icongen.py)

#### 2.4 Admin vylepšení
- [ ] Import/export otázek (CSV, JSON, Quizlet formát) — CSV/JSON hotovo (`server.py`: `questions_from_csv()`, `questions_to_csv()`, export JSON; `static/admin.html`: import/export). Chybí podpora formátu Quizlet.
- [x] Drag & drop řazení otázek — `static/admin.html`: `renderEditor()`, obsluha `dragstart`/`drop` přeskládá `editorQuestions`.
- [x] Obrázky v otázkách (upload nebo URL) — `server.py`: `/api/admin/upload-image`, `image_url`; `static/admin.html`: editor a náhled; `static/play.html` a `static/host.html`: `questionImage`. Samostatný test obrázků chybí.
- [x] Sdílení sad otázek (export jako odkaz/soubor) — JSON/CSV soubor přes `static/admin.html`: `btnExportJson`/`btnExportCsv`, `triggerDownload()`. Samostatný odkaz na sadu není implementovaný; požadavek splňuje soubor.
- [x] Export výsledků písemky do CSV
- [x] Detailní per-otázkový export výsledků písemky — `server.py`: `ExamState.answers_csv()`, `items_csv()` i export uložených písemek; `test_server.py`: `TestExamAnalysis.test_csv_exports()`.
- [ ] PDF export výsledků — `server.py` a `static/admin.html` nabízejí CSV, vlastní PDF export ani jeho test není implementovaný.
### 1.7 Bezpecnost
- [x] Filtr prezdivek — `_is_nickname_clean()` s `_BANNED_WORDS` (CZ+EN)
- [x] Rate limiting — `_RegistrationRateLimiter` (10/min/IP)
- [x] Kick hrace — `POST /api/host/kick` + tlacitko v host UI
- [x] Player secret tokeny — overeni identity pri odpovedich

### 1.8 Frontend
- [x] `static/profile.html` — login/register, profil, KPI, tridy, historie
- [x] `static/play.html` — renderery pro 5 typu otazek, streak badge, WS klient, volitelne prihlaseni
- [x] `static/host.html` — streak shoutout, fire v zebricku, WS klient, fullscreen
- [x] `static/admin.html` — editor pro 5 typu otazek, dropdown pro typ, tab Tridy

### 1.9 Automaticke ukladani her
- [x] Hra se automaticky ulozi do DB po posledni otazce (ne jen manualne)
- [x] `_class_id` atribut pro propojeni hry s tridou

---

## FAZE 2: Stredne pokrocile — HOTOVO

### 2.1 Import/Export
- [x] Export otazek JSON — download tlacitko v adminu
- [x] Import otazek JSON — file upload v adminu
- [x] CSV import/export (csv stdlib modul)
- [x] Export výsledků her do CSV pro učitele — `server.py`: `game_results_to_csv()`, `/api/admin/history/export`; `test_server.py`: `TestGameAnalysis.test_history_record_and_exports()`. PDF export tím není splněný.

### 2.2 i18n (CZ/EN/SK/DE)
- [x] `static/i18n/` — JSON preklady per jazyk (cs, en, sk, de — 453 klicu)
- [x] `static/i18n.js` — lightweight runtime (~162 radku) s `onLangChange()` callback
- [x] `data-i18n` atributy v HTML (vsechny 4 stranky — kompletni pokryti)
- [x] Language switcher UI (CS | EN | SK | DE pill widget na vsech strankach)

### 2.3 Deploy konfigurace
- [x] `deploy/caddy/Caddyfile` — reverse proxy s auto-HTTPS
- [x] `deploy/nginx/nginx.conf` — s Let's Encrypt
- [x] `deploy/docker-compose.web.yml` — rozsireni pro web nasazeni
- [x] `--base-url` / `QUIZ_BASE_URL` pro verejnou URL
- [x] `--trusted-proxies` / `QUIZ_TRUSTED_PROXIES` pro duveryhodne proxy
- [x] Podpora X-Forwarded-For/Proto/Host proxy headeru

### 2.4 Nove testy (156 celkem)
- [x] TestAuth — 17 testu (register, login, session, profile, logout)
- [x] TestClasses — 14 testu (create, join, list, members, delete)
- [x] TestQuestionTypes — 16 testu (vsech 5 typu + scoring)
- [x] TestStreakCombo — 9 testu (streak, multiplikatory, max_streak)
- [x] TestWebSocket — 16 testu (handshake, frames, ping, close, manager)

---

## FAZE 3: Pokrocile funkce — CASTECNE HOTOVO

### 3.1 Herni mody
- [x] Klasicky (soucasny — live, teacher-paced)
- [x] Tymovy mod (virtualni tymy 2-6, round-robin, team leaderboard + podium)
- [x] Self-paced / Domaci ukol (student si projde sam, okamzita zpetna vazba, progress bar)

### 3.2 Player experience
- [x] Animovane podium (canvas confetti, WebAudio fanfare, staggered rise animace)
- [x] Nahodny generator prezdivek (30 adjektiv + 30 zvirat, CZ)
- [x] Avatary / profilové obrázky (výběr z předdefinovaných) — `static/profile.html`: `AVATARS`, `renderAvatarGrid()`, `selectAvatar()`; `db.py`: `update_avatar()`; `test_server.py`: `TestRegisterAvatarHTTP`.
- [x] Dark mode — hráčská stránka automaticky podle systému: `static/style.css`, `@media (prefers-color-scheme: dark)` pro `.player-page`. Globální přepínač ani plošné pokrytí ostatních stránek tím nevzniká; vizuální test chybí.
- [ ] Power-ups (ochrana streaku, double points) — volitelne; dvojnásobné body otázky/finále už jsou (`test_server.py`: `TestDoublePoints`), ochrana streaku a hráčské power-ups chybí.

### 3.3 Admin vylepseni
- [x] Drag & drop razeni otazek (HTML5 DnD API)
- [x] Obrázky v otázkách (upload nebo URL) — `server.py`: `/api/admin/upload-image`, `image_url`; `static/admin.html`: editor a náhled; `static/play.html` a `static/host.html`: `questionImage`. Samostatný test obrázků chybí.
- [x] Sdílení sad otázek (export jako odkaz/soubor) — JSON/CSV soubor přes `static/admin.html`: `btnExportJson`/`btnExportCsv`, `triggerDownload()`. Samostatný odkaz na sadu není implementovaný; požadavek splňuje soubor.

### 3.4 Ankety a hlasovani
- [x] Poll mod (zadna spravna odpoved, live vote bary s procenty)
- [x] Word cloud z odpovedi (CSS sizing dle frekvence, 7 barev)
- [x] Slide/info karta mezi otazkami (title + body, zadny answer)

### 3.5 Gamifikace
- [ ] XP systém přes více her (body a postup po levelech) — `db.py`: `save_game_to_db()` sčítá `users.total_score` a počet her, `static/profile.html`: `kpiScore` zobrazuje součet; postup po levelech chybí.
- [ ] Achievementy / odznaky (série, počet her a skóre) — herní ocenění včetně série už jsou (`server.py`: `QuizState._compute_awards_locked()`, `static/play.html`: `renderFinishCard()`, `test_server.py`: `TestAwards`); trvalé odznaky za počet her a kumulované skóre chybí.
- [ ] Ligy mezi třídami — `db.py` má skupiny a jejich historii; liga, její pořadí ani testy nejsou implementované.

#### 3.3 Moderace a bezpečnost
- [ ] Při startu bez `QUIZ_ADMIN_PASSWORD` výrazně varovat, že admin je v lokální síti otevřený — `server.py`: `main()` jen vypíše „Admin portal: open (no password)“. Výrazné varování chybí; `Handler._require_admin()` už navíc vyžaduje přihlášení, pokud existuje učitelský účet.
- [ ] Volitelně generovat jednorázové admin heslo/token při startu — `server.py`: `main()` umí zadané heslo; generovaný `HOST_TOKEN` chrání ovládání hry, nenahrazuje admin heslo.
- [x] Filtr nevhodných přezdívek — `server.py`: `_BANNED_WORDS`, `_is_nickname_clean()`, kontrola v `QuizState.register_player()`; samostatný regresní test filtru chybí.
- [x] Generátor náhodných přezdívek (volitelné) — `server.py`: `_generate_random_nickname()`, `/api/random-nickname`; `static/play.html`: `randomNickBtn`; samostatný regresní test generátoru chybí.
- [x] Anti-bot ochrana — základní omezení registrací a detekce příliš rychlých odpovědí: `server.py`: `REGISTER_LIMITER`, `bot_score`; `test_server.py`: `TestRateLimiters.test_class_behind_nat_can_join()`. Detekce sama automaticky nevyhazuje hráče.
- [x] Kick hráče — `server.py`: `QuizState.kick_player()`, `/api/host/kick`; `static/host.html`: `kickPlayer()`; `test_server.py`: `TestRoomsMulti.test_kicked_player_is_told_and_may_join_again()`.

#### 3.4 Technické
- [x] i18n (přepínání jazyků v UI — CZ/EN/SK/DE) — `static/i18n.js`: `setLang()`, přepínač a katalogy CS/EN/SK/DE/UK; `test_server.py`: `TestI18nCatalogs` kontroluje klíče a parametry, nikoli celé UI v prohlížeči.
- [x] PWA (offline přístup, instalace na mobil) — `static/manifest.json`, `static/sw.js`, registrace v `static/play.html`. Offline je dostupné uložené rozhraní/fallback; živá hra potřebuje spojení se serverem. Instalace na zařízení nemá automatický test.
- [x] HTTPS podpora — konfigurace TLS a proxy v `deploy/caddy/Caddyfile` a `deploy/nginx/nginx.conf`; jde o podporu nasazení, nikoli o nové živé ověření certifikátu.
- [ ] REST API verzování — routy v `server.py`: `Handler` jsou `/api/...`, verze typu `/api/v1/...` ani její testy nejsou zavedené.
### 3.6 Moderace
- [x] Generator nahodnych prezdivek (adjektivum + zvire, CZ, 🎲 tlacitko)
- [x] Anti-bot detekce (timing heuristika < 0.3s, bot_score, 🤖 indikator pro hosta)

---

## Architektura souboru

```
classrally/
├── server.py          # HTTP server, API, herni logika (~1760 radku)
├── db.py              # SQLite databaze, auth, tridy, historie (~585 radku)
├── ws.py              # WebSocket RFC 6455 implementace (~260 radku)
├── qrgen.py           # QR kod generator
├── icongen.py         # Generator PWA ikon (pure Python PNG)
├── test_server.py     # 77 unit testu
├── Dockerfile
├── docker-compose.yml
├── start.sh           # Spousteci script (Docker)
├── docker-common.sh   # Sdilene Docker helpery
├── static/
│   ├── play.html      # Hracska obrazovka (5 typu otazek, WS, streak)
│   ├── host.html      # Projektor/ucitelska obrazovka (WS, fullscreen)
│   ├── admin.html     # Sprava otazek, editor 5 typu, tridy
│   ├── profile.html   # Registrace, login, profil, historie
│   ├── style.css      # Spolecne styly
│   ├── manifest.json  # PWA manifest
│   ├── sw.js          # Service worker
│   ├── offline.html   # Offline fallback
│   ├── icon-512.png   # PWA ikona 512x512
│   ├── icon-192.png   # PWA ikona 192x192
│   └── favicon.png    # Favicon 32x32
├── questions/         # JSON soubory se sadami otazek
├── history/           # JSON zaznamy odehranych her
└── data/              # SQLite databaze (classrally.db)
```

## Stack

- **Backend:** Python 3.12+ stdlib only (http.server, sqlite3, hashlib, uuid, json, csv, base64, struct, threading)
- **Frontend:** Vanilla HTML/CSS/JS (zadny framework, zadny build)
- **DB:** SQLite (WAL mod, stdlib modul)
- **WebSocket:** Vlastni RFC 6455 implementace (stdlib only)
- **Deploy:** Docker / docker-compose / bare metal

**Zero external dependencies.** Cela aplikace bezi s cistym Pythonem.

---

## Jak spustit

### Lokalne
```bash
python3 server.py
# nebo s teacher kodem pro registraci ucitelu:
python3 server.py --teacher-code MOJEKOD123
```
Otevri http://localhost:8080

### Docker
```bash
# Zakladni spusteni:
./start.sh

# S teacher kodem (pro registraci ucitelskych uctu):
QUIZ_TEACHER_CODE=MOJEKOD123 ./start.sh

# S heslem pro admin portal:
QUIZ_ADMIN_PASSWORD=tajneheslo QUIZ_TEACHER_CODE=MOJEKOD123 ./start.sh
```

### Environment promenne
| Promenna | Popis | Default |
|----------|-------|---------|
| `QUIZ_TEACHER_CODE` | Kod pro registraci ucitelu (prazdny = registrace ucitelu zakazana) | `""` |
| `QUIZ_ADMIN_PASSWORD` | Heslo pro admin portal (prazdny = bez hesla) | `""` |
| `QUIZ_EXTERNAL_IP` | Verejna IP pro QR kod a URL | auto-detect |
| `QUESTION_TIME` | Cas na otazku v sekundach | `20` |
| `REVEAL_TIME` | Cas na zobrazeni vysledku | `5` |
| `AI_BASE_URL` | AI server URL (OpenAI-kompatibilni) | `http://localhost:11434` |
| `AI_API_KEY` | API klic pro AI provider (volitelne) | `""` |
| `AI_MODEL` | AI model pro generovani otazek | `gpt-oss:20b` |
| `OLLAMA_HOST` | (deprecated) Ollama server host | `localhost` |
| `OLLAMA_PORT` | (deprecated) Ollama server port | `11434` |
| `OLLAMA_MODEL` | (deprecated) Ollama model | `gpt-oss:20b` |

### Stranky
| URL | Popis |
|-----|-------|
| `/play` | Hracska obrazovka (pripojeni do hry) |
| `/host` | Host/projektor obrazovka |
| `/admin` | Sprava otazek, tridy |
| `/profile` | Registrace, login, profil |

### Ucitelsky kod (teacher code)
Teacher code je jednorazovy registracni kod ktery ucitel zada pri vytvareni uctu.
Slouzi k tomu aby se jako ucitel nemohl zaregistrovat kdokoliv.

- Nastavuje se pri spusteni serveru: `QUIZ_TEACHER_CODE=MOJEKOD123 ./start.sh`
- Ucitel ho zada na strance `/profile` pri registraci (zvoli roli "Ucitel")
- Studenti teacher code nepotrebuji
- Pokud neni nastaven, registrace ucitelu je zakazana (pouze studenti)

---

## Srovnani s konkurenci

| Feature | ClassRally | Kahoot | Blooket | Quizizz |
|---------|:-------:|:------:|:-------:|:-------:|
| Lokální/offline | ✅ | ❌ | ❌ | ❌ |
| Bez registrace hráčů | ✅ | ✅ | ✅ | ✅* |
| Neomezený free tier | ✅ | ❌ (10) | ✅ (60) | ✅ |
| Open source | ✅ | ❌ | ❌ | ❌ |
| Self-hosted | ✅ | ❌ | ❌ | ❌ |
| Multiple choice | ✅ | ✅ | ✅ | ✅ |
| True/False | ❌ | ✅ | ✅ | ✅ |
| Open-ended | ❌ | 💰 | ❌ | ✅ |
| Obrázky v otázkách | ❌ | ✅ | ✅ | ✅ |
| Týmový mód | ❌ | 💰 | ✅ | ❌ |
| Self-paced/homework | ❌ | 💰 | ❌ | ✅ |
| Streaky/combo | ❌ | ✅ | ✅ | ✅ |
| WebSocket realtime | ❌ | ✅ | ✅ | ✅ |
| Skupiny/třídy | ❌ | 💰 | ✅ | ✅ |
| Historie per student | ❌ | 💰 | 💰 | ✅ |
| AI generátor | ✅ | 💰 | ❌ | ✅ |
| Export výsledků | ✅ CSV | 💰 | 💰 | ✅ |
| Hudba/zvuky | ✅ | ✅ | ✅ | ✅ |
| Lokalni/offline | Y | N | N | N |
| Bez registrace hracu | Y | Y | Y | Y* |
| Neomezeny free tier | Y | N (10) | Y (60) | Y |
| Open source | Y | N | N | N |
| Self-hosted | Y | N | N | N |
| Multiple choice | Y | Y | Y | Y |
| True/False | Y | Y | Y | Y |
| Multi-select | Y | $ | N | Y |
| Ordering | Y | N | N | N |
| Open-ended | Y | $ | N | Y |
| Obrazky v otazkach | N | Y | Y | Y |
| Tymovy mod | Y | $ | Y | N |
| Self-paced/homework | Y | $ | N | Y |
| Streaky/combo | Y | Y | Y | Y |
| WebSocket realtime | Y | Y | Y | Y |
| Skupiny/tridy | Y | $ | Y | Y |
| Uzivatelske ucty | Y | Y | Y | Y |
| Historie per student | Y | $ | $ | Y |
| AI generator | Y | $ | N | Y |
| Export vysledku | Y | $ | $ | Y |
| Hudba/zvuky | Y | Y | Y | Y |
| PWA | Y | Y | N | Y |
| Zero dependencies | Y | N | N | N |

(Y = ano, N = ne, $ = jen v placenem planu)

## Nase konkurencni vyhody
1. **Lokalni beh bez internetu** — zadna konkurence to nenabizi
2. **Neomezeny free tier** — Kahoot omezuje na 10 hracu
3. **Open source, self-hosted** — data zustavaji ve skole
4. **Zero dependencies** — zadny npm install, zadne build nastroje
5. **AI generator zdarma** — Kahoot ho ma jen v placenem planu
6. **5 typu otazek zdarma** — Kahoot ma multiselect a open-ended jen v placenem planu
7. **Tridy a historie zdarma** — Kahoot a Blooket to maji jen v placenem planu
