# QuizWeb - Lokalni kvizova platforma pro tridu (Kahoot-style)

Webova aplikace pro interaktivni kvizove souteze ve tride. Ucitel promita host obrazovku na projektor, zaci odpovidaji z mobilu. Vse bezi lokalne bez internetu a bez uctu tretich stran.

**Verze:** 2.1 | **Python:** 3.10+ (stdlib only, zero dependencies) | **Docker ready**

## Rychly start

### Docker (doporuceno)

```bash
./start.sh
```

Skript automaticky nainstaluje Docker (pokud chybi) a spusti aplikaci. Hotovo.

```bash
# S heslem pro admin portal
QUIZ_ADMIN_PASSWORD=mojeHeslo ./start.sh

# Zastaveni / logy / rebuild
./stop.sh
./logs.sh
./rebuild.sh
```

### Bez Dockeru

```bash
cd quiz_web
python3 server.py --host 0.0.0.0 --port 8765
```

Server vypise URL a **Host token** (pro ovladani hry):

```
Host screen: http://192.168.1.10:8765/host
Player screen: http://192.168.1.10:8765/play
Admin portal: http://192.168.1.10:8765/admin
Host token: a1b2c3d4e5f6...
```

| URL | Kdo | Popis |
|-----|-----|-------|
| `/host` | Ucitel (projektor) | Ridici obrazovka s otazkami, zebrickem, hudbou |
| `/play` | Zaci (mobily) | Hracska obrazovka - prihlaseni + odpovedi |
| `/admin` | Ucitel (PC) | Sprava otazek, AI generator, historie, nastaveni |

Mobily musi byt ve stejne Wi-Fi/LAN. Nepouzivej `127.0.0.1` pro zaky.

## Co ma spolecne s Kahoot

| Funkce | Kahoot | QuizWeb |
|--------|--------|---------|
| Barevne odpovedi (cervena/modra/zluta/zelena) | Ano | Ano |
| Odpocet na otazku s vizualnim timerem | Ano | Ano (countdown bar) |
| Bodovani za rychlost odpovedi | Ano | Ano (600 + speed bonus az 400) |
| Zive zobrazeni poctu hlasu | Ano | Ano (vote bars) |
| Zebricek v realnem case | Ano | Ano |
| Hudba/zvukove efekty | Ano | Ano (MP3 + synth fallback) |
| Mobilni pripojeni pres URL | Ano (PIN) | Ano (primo URL v LAN) |
| Podium na konci hry | Ano | Ano (zlato/stribro/bronz) |
| Animace skore (+body popup) | Ano | Ano |

## Co je jinak nez Kahoot

| Funkce | Kahoot | QuizWeb |
|--------|--------|---------|
| **Bezi lokalne** - bez internetu, bez uctu | Ne (cloud) | Ano (Python server v LAN) |
| **Zdarma bez omezeni** | Free plan = max 10 hracu | Neomezeno |
| **AI generovani otazek** (Ollama) | Ne (jen rucne/import) | Ano (lokalni LLM) |
| **Vlastni Ollama modely** | - | Konfigurovatelny model/host/port |
| **Ucitelsky portal** s heslem | Dashboard v cloudu | `/admin` s volitelnym heslem |
| **Banky otazek** (vice sad) | Kahooty | JSON soubory v `questions/` |
| **Historie her** | V cloudu | Lokalne v `history/` |
| **Zero dependencies** | - | Jen Python 3 stdlib |
| **Open source** | Ne | Ano |
| **GDPR** | Problematicke | Zadna data neodesila |

## Struktura projektu

```
quiz_web/
  server.py                        # hlavni server (stdlib http.server)
  qrgen.py                         # QR kod generator (pure Python, zero deps)
  Dockerfile                       # Docker image
  docker-compose.yml               # Docker Compose konfigurace
  start.sh                         # spusti quiz server v Dockeru
  stop.sh                          # zastavi kontejner
  rebuild.sh                       # rebuild image a spusti znovu
  logs.sh                          # zobrazi logy
  docker-common.sh                 # sdilene funkce pro skripty
  static/
    host.html                      # ucitelska obrazovka (projektor)
    play.html                      # hracska obrazovka (mobil)
    admin.html                     # ucitelsky portal (sprava)
    style.css                      # spolecne styly
    audio/                         # vlastni MP3/OGG/WAV (neni v repu, viz nize)
  questions/                       # banky otazek (12 tematickych sad)
  history/                         # ulozena historie her (JSON, generovano za behu)
  test_server.py                   # 77 unit + integracnich testu
  smoke_test.sh                    # end-to-end smoke test
```

### Tok dat

```
Ucitel (projektor)           Zaci (mobily)
     /host  <--poll 1s-->  server.py  <--poll 1s-->  /play
                              |
     /admin  <--REST API---   |
                              |
                    Ollama (lokalni LLM)
```

## Spusteni

### Docker (doporuceno)

```bash
# Jednim prikazem (nainstaluje Docker pokud chybi)
./start.sh

# S konfiguraci
QUIZ_ADMIN_PASSWORD=heslo OLLAMA_MODEL=llama3:8b ./start.sh

# Zastaveni / logy / rebuild
./stop.sh
./logs.sh
./rebuild.sh
```

Skripty automaticky detekuji LAN IP hosta (ne Dockeru!) a zobrazi spravnou URL pro zaky.
Pokud ma pocitac vic sitovych rozhrani, nabidne vyber. Data (`questions/`, `history/`, `static/audio/`) jsou mountovana jako volumes - preziji restart.

### Bez Dockeru

```bash
cd quiz_web

# Zakladni spusteni (bez hesla, default nastaveni)
python3 server.py --host 0.0.0.0 --port 8765

# S heslem pro admin portal
python3 server.py --admin-password mojeHeslo123

# S vlastnim casovanim (30s na otazku, 8s reveal)
python3 server.py --question-time 30 --reveal-time 8

# S vlastnim Ollama nastavenim
python3 server.py --ollama-host 192.168.1.100 --ollama-port 11434 --ollama-model llama3:8b
```

### ENV promenne (alternativa k CLI argumentum)

| Promenna | Default | Popis |
|----------|---------|-------|
| `QUIZ_ADMIN_PASSWORD` | _(prazdne)_ | Heslo pro admin portal |
| `QUIZ_EXTERNAL_IP` | _(auto-detekce)_ | IP hosta pro URL hracu (Docker) |
| `OLLAMA_HOST` | `localhost` / `host.docker.internal` | Ollama hostname |
| `OLLAMA_PORT` | `11434` | Ollama port |
| `OLLAMA_MODEL` | `gpt-oss:20b` | Ollama model |
| `QUESTION_TIME` | `20` | Cas na otazku (sekundy) |
| `REVEAL_TIME` | `5` | Cas na reveal (sekundy) |

## Zabezpeceni (v2.1)

### Player identity
- Kazdy hrac pri registraci dostane `player_id` (verejny) + `player_secret` (privatni)
- Odeslani odpovedi vyzaduje `player_secret` - nelze odpovedet za jineho hrace
- Zebricek nezverejnuje `player_id` - jen jmeno a skore

### Host token
- Server pri startu vygeneruje nahodny `HOST_TOKEN`
- Ovladani hry (`/api/host/action`) vyzaduje `Authorization: Bearer <token>` hlavicku
- Token je videt v konzoli serveru a v admin portalu (tab Nastaveni)

### Admin portal
- Volitelna ochrana heslem (`--admin-password`)
- SHA-256 hash hesla, session tokeny (platnost 8h)
- Rate limiting: max 5 pokusu za 5 minut na IP adresu
- Token se prenasi jen v `Authorization` hlavicce, nikdy v URL

### XSS prevence
- Vsechny HTML soubory pouzivaji DOM API (`createElement`/`textContent`)
- Zadny `innerHTML` s uzivatelskymi daty
- Zadne inline `onclick` - vsude `addEventListener`

### Validace dat
- Otazky se validuji pri ukladani (povinne pole, rozsahy, typy)
- ID hry pro mazani historie se overuje regexem (presna shoda)
- Path traversal ochrana pri praci se soubory

## Predpripravene banky otazek (11 temat)

| Soubor | Tema | Pocet otazek |
|--------|------|:---:|
| `virtualbox_ubuntu_docker.json` | VirtualBox, Ubuntu, Docker | 10 |
| `sit_zaklady.json` | TCP/IP, DNS, DHCP, porty, NAT | 8 |
| `linux_sprava.json` | APT, chmod, procesy, pipe, grep | 8 |
| `windows_server.json` | AD, GPO, Hyper-V, PowerShell, RDP | 8 |
| `bezpecnost_it.json` | Phishing, MFA, DDoS, ransomware, XSS | 8 |
| `html_css_zaklady.json` | HTML tagy, CSS selektory, Flexbox | 8 |
| `python_programovani.json` | Typy, funkce, pip, virtualenv | 8 |
| `databaze_sql.json` | SELECT, JOIN, normalizace, agregace | 8 |
| `git_verzovani.json` | init, clone, merge, stash, .gitignore | 8 |
| `hardware_pc.json` | RAM, SSD, RAID, NVMe, BIOS | 8 |
| `cloud_devops.json` | Docker, K8s, CI/CD, Terraform, IaaS | 8 |
| `office365_spoluprace.json` | Teams, OneDrive, Excel, Power Automate | 8 |

Celkem **90 otazek**. Dalsi banky lze vytvorit v admin portalu nebo AI generatorem.

## Ucitelsky portal (/admin)

### Prihlaseni
- Bez `--admin-password`: volny pristup
- S heslem: login formular, session token (8h platnost)

### Banky otazek
- Zobrazeni vsech bank v `questions/`
- Aktivace banky = nahraje otazky do hry a resetuje stav
- Vytvoreni nove banky, smazani

### Editor otazek
- Vizualni editor: text otazky, 4 moznosti, vyber spravne, vysvetleni
- Pridani/odebrani otazek
- Ulozeni zmen

### AI Generator (Ollama)
1. Zadej **tema** (napr. "Linux prikazy", "Docker kontejnery", "TCP/IP")
2. Zvol **pocet otazek**, **model**, **jazyk**
3. Klikni **Generovat** - Ollama vygeneruje otazky (30-120s)
4. **Nahled** vygenerovanych otazek s moznosti ulozit do banky
5. Moznost pridat ke stavajici bance

Konfigurace Ollama (host/port/model) je editovatelna v tabulce Nastaveni.

### Historie her
- Automaticke ukladani vysledku (akce `save_history` z host obrazovky)
- Zobrazeni: datum, pocet hracu, zebricek se zlatem/stribrem/bronzem
- Mazani zaznamu

## Ovladani hry (Host obrazovka)

1. **Start** - spusti 1. otazku (odpocet 20s)
2. **Ukazat spravnou odpoved** - okamzite vyhodnoti a ukaze spravnou
3. **Dalsi otazka** - rucni prechod (jinak bezi automaticky)
4. **Reset hry** - vrati do lobby, vynuluje body

### Odpocet a automaticky postup

- Kazda otazka bezi **20 sekund** (konfigurovatelne 5-120s)
- Pokud odpovi vsichni driv, vyhodnoceni probehne okamzite
- Po vyprseni casu se otazka automaticky vyhodnoti
- Reveal faze trva **5 sekund** (konfigurovatelne 2-30s)
- Po reveal system automaticky prejde na dalsi otazku

### Bodovani

- Spravna odpoved: **600 bodu** + bonus za rychlost (az +400)
- Spatna odpoved: **0 bodu**
- Bonus za rychlost: `max(0, 40 - elapsed_seconds) * 10`
- Cim rychleji odpovite, tim vice bodu

## Atmosfera (hudba a zvuky)

Audio soubory **nejsou soucasti repozitare** (licence). Nahrajte si vlastni MP3/OGG/WAV do `static/audio/`.
Server je automaticky nacte a nahodne vybira behem hry.

**Pokud zadne audio soubory nejsou, host obrazovka pouzije synth fallback (WebAudio) - vse funguje i bez nich.**

### Jak to funguje

1. **Hudba ON** - spusti napetovy loop (prvni klik odemkne zvuk v prohlizeci)
2. **Hlasitost** - posuvnik
3. **Stinger** - kratky zvukovy efekt
4. **Automaticky rezim** (default zapnut):
   - `question` faze -> spusti loop
   - nova otazka -> novy nahodny loop
   - `reveal` faze -> stinger + loop se vypne
   - vsichni odevzdali -> kratky cue "Dohlasovano"

### Klasifikace souboru (podle nazvu)

- Jmeno obsahuje `stinger`, `reveal`, `hit`, `win`, `ding`, `correct`, `lock`, `end` -> **stinger** (kratky efekt)
- Ostatni -> **loop** (hudba na pozadi pri otazce)

Cim vice souboru nahrajete, tim vetsi variabilita.

## QR kod pro pripojeni

Na host obrazovce (`/host`) i hracske obrazovce (`/play`) se automaticky zobrazi QR kod s URL pro pripojeni zaku. Ucitel muze:
- **Kliknout na QR** na host obrazovce → zobrazi se zvetseny overlay (idealni pro projektor)
- Na play obrazovce je QR velky, aby ho zaci mohli vyfotit i z vetsi vzdalenosti (10+ metru od projektoru)

QR kod se generuje ciste v Pythonu (`qrgen.py`) bez externich knihoven.

## Hracska obrazovka (/play) - Kahoot design

- **4 barevne bloky** (cervena/modra/zluta/zelena) s geometrickymi tvary
- **Countdown bar** - vizualni odpocet, cervena kdyz zbyva < 30% casu
- **Score popup** - animace `+body` pri ziskani bodu
- **Correct/Wrong banner** - zeleny/cerveny banner po vyhodnoceni
- **Podium** na konci hry - medaile pro top 3

## Testy

```bash
# Unit + integracni testy (77 testu)
python3 test_server.py

# Kompletni smoke test (unit testy + HTTP end-to-end)
./smoke_test.sh
```

Pokryti testu (8 testovych trid):

| Trida | Pocet | Co testuje |
|-------|:---:|-----------|
| TestQuizState | 25 | Herni logika, bodovani, casovace, faze, player_secret |
| TestAdminAuth | 6 | Heslo, session, expirace |
| TestQuestionValidation | 7 | Validace otazek (prompt, options, correct_index) |
| TestHistoryDeletion | 2 | Presne mazani, neplatne ID |
| TestQuestionBanks | 6 | CRUD, path traversal, auto-extension |
| TestScoringHistory | 3 | Ukladani, mazani historie |
| TestHTTPIntegration | 14 | API endpointy, cely herni flow, host token auth |
| TestAdminAuthHTTP | 7 | Autorizace pres HTTP, Bearer token, rate limiting |

## API Reference

### Verejne endpointy

| Metoda | Endpoint | Popis |
|--------|----------|-------|
| GET | `/api/health` | Health check (`{"ok":true,"version":"2.1"}`) |
| GET | `/api/state?player_id=X&host=1` | Stav hry |
| GET | `/api/network` | Sitove info (URL pro zaky) |
| GET | `/api/audio-tracks` | Seznam audio souboru |
| POST | `/api/register` | `{"name":"Jmeno"}` -> `{player_id, player_secret, name}` |
| POST | `/api/submit` | `{"player_id":"X","player_secret":"S","choice":0}` |
| POST | `/api/host/action` | `{"action":"start\|reveal\|next\|reset\|save_history"}` (Bearer token!) |
| GET | `/api/host/token` | Vrati host token (vyzaduje admin auth) |

### Admin endpointy (vyzaduji `Authorization: Bearer <token>` pokud je heslo nastavene)

| Metoda | Endpoint | Popis |
|--------|----------|-------|
| GET | `/api/admin/auth-status` | Stav autentizace |
| POST | `/api/admin/login` | `{"password":"X"}` -> `{token}` |
| GET | `/api/admin/banks` | Seznam bank otazek |
| GET | `/api/admin/bank?filename=X` | Nacist banku |
| POST | `/api/admin/bank/save` | `{"filename":"X","questions":[...]}` |
| POST | `/api/admin/bank/delete` | `{"filename":"X"}` |
| POST | `/api/admin/bank/activate` | `{"filename":"X"}` -> nacte do hry |
| POST | `/api/admin/timing` | `{"question_duration_sec":20,"reveal_duration_sec":5}` |
| GET | `/api/admin/history` | Historie her |
| POST | `/api/admin/history/delete` | `{"game_id":"X"}` |
| GET | `/api/admin/ollama/config` | Ollama konfigurace |
| POST | `/api/admin/ollama/config` | `{"host":"X","port":N,"model":"X"}` |
| GET | `/api/admin/ollama/models` | Seznam Ollama modelu |
| POST | `/api/admin/ai/generate` | `{"topic":"X","count":5,"model":"X","language":"cs"}` |

## Format otazek (JSON)

```json
[
  {
    "id": "q1",
    "prompt": "Co je VirtualBox?",
    "options": [
      "Spravce Docker image",
      "Hypervizor typu 2",
      "Linuxovy spravce balicku",
      "Nastroj pro monitoring site"
    ],
    "correct_index": 1,
    "explanation": "VirtualBox je hypervizor typu 2."
  }
]
```

Validace: `prompt` nesmi byt prazdny, `options` 2-6 polozek (string), `correct_index` v rozsahu.

## Jak zjistit IP ucitelskeho PC

```bash
hostname -I       # Linux/Ubuntu
ipconfig          # Windows
```

Server IP automaticky detekuje a vypise po startu.

## Poznamky

- **Zero dependencies** - jen Python 3.10+ standard library
- **Bez internetu** - vse bezi v lokalni siti
- **GDPR safe** - zadna data se neodesila nikam
- **Self-contained** - cely projekt je v `quiz_web/`, staci zkopirovat slozku
- **Docker ready** - `./start.sh` nainstaluje Docker a spusti aplikaci
- Zvuk: vlastni MP3 v `static/audio/` nebo WebAudio synth fallback
- Migrace ze stareho `questions_*.json` je automaticka
