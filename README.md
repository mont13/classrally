# ClassRally - Kvizova a testovaci platforma pro tridu

ClassRally je lokalni nastroj pro tridu se dvema rezimy:

- **Hra (Kahoot styl)** — ziva lockstep hra, vsichni na stejne otazce, body za rychlost, dvojnasobne body, leaderboard, hudba, oceneni, konfety a slavnostni podium.
- **Pisemka (test pro znamku)** — kazdy zak svym tempem, vlastni zamichane poradi otazek/odpovedi, casovy limit, automaticke znamkovani (ceske 1-5) a **dohled** (vidite, kdo opustil okno testu).

Ucitel server spusti na notebooku, zaci se pripoji pres QR kod z mobilu. Internet ani studentske ucty nejsou potreba — vse bezi v lokalni siti.

Staci Python nebo Docker (Linux/Mac), na Windows hotovy ZIP s pribalenym Pythonem — nic dalsiho neni treba instalovat.

> **Port:** vychozi je atypicky **48217** (aby nekolidoval na sdilenych strojich). Zmenite promennou `QUIZ_PORT`.

> **Verze 3.0** (rijen 2026): rozbor vysledku po otazkach (hra i pisemka), trvale ukladane pisemky s exporty
> a polozkovou analyzou, volby „bez bonusu za rychlost“ a „jen stupne vitezu“, ucty s e-mailem, mistnosti
> s PINem pro vice ucitelu a zalohovani; polozky otazek na serazeni se zakum michaji a otevrena odpoved
> muze vyzadovat presnou shodu (`"exact": true`). Verze 2.1 (cerven 2026) mela jen jednu tridu, souhrnne vysledky
> a pisemku v pameti. Verzi hlasi `/api/health` (`"version": "3.0"`).

## Jak odehrat prvni hru

1. **Spust server** — `./start.sh` (Docker) nebo `python3 server.py --host 0.0.0.0`
2. **Otevri `/admin`** — vyber nebo vytvor sadu otazek, klikni **Aktivovat**
3. **Otevri `/host`** na projektoru — zadej pristupovy klic (zobrazen pri startu)
4. **Zaci nascanuji QR kod** z projektoru nebo zadaji URL na mobilu
5. **Klikni Start** — otazky se zacnou zobrazovat, zaci odpovidaji
6. Po posledni otazce se zobrazi podium s vysledky

## Spusteni

### Docker (doporuceno)

```bash
./start.sh
```

Skript automaticky nainstaluje Docker (pokud chybi), detekuje IP adresu pocitace a spusti aplikaci. Na konci vypise URL pro zaky.

```bash
# S heslem pro ucitelsky portal
QUIZ_ADMIN_PASSWORD=mojeHeslo ./start.sh

# Zastaveni / logy / rebuild
./stop.sh
./logs.sh
./rebuild.sh
```

> **Doporuceni pro tridu:** pri ostrem pouziti nastavte `QUIZ_ADMIN_PASSWORD`.
> Bez hesla je ucitelsky portal v lokalni siti otevreny komukoli, kdo zna adresu serveru.

### Bez Dockeru

```bash
python3 server.py --host 0.0.0.0 --port 48217
```

Server vypise URL a pristupovy klic:

```
Host screen:   http://192.168.1.10:48217/host
Player screen: http://192.168.1.10:48217/play
Admin portal:  http://192.168.1.10:48217/admin
*** HOST TOKEN: a1b2c3d4e5f6... ***
```

| Adresa | Kdo ji pouziva | K cemu slouzi |
|--------|---------------|---------------|
| `/host` | Ucitel (projektor) | Ridici obrazovka hry — otazky, zebricek, hudba, QR kod |
| `/play` | Zaci (mobily) | Prihlaseni a odpovedi (hra) |
| `/exam` | Zaci (mobily) | Pisemka — test pro znamku (sem se `/play` automaticky prepne v rezimu Pisemka) |
| `/admin` | Ucitel (PC) | Sprava sad, rezim Pisemka, AI generator, historie, nastaveni |

Mobily musi byt ve stejne Wi-Fi siti jako server.

### Windows (pro ucitele bez znalosti)

Na Windows nepouzivame `.exe` (kvuli podpisu/SmartScreen). Misto toho **prenosny Python v ZIPu**:

```bash
# Sestaveni balicku (na Linux/Mac; stahne prenosny Python a zabali aplikaci)
./make-windows-package.sh
# -> dist/ClassRally-Windows.zip
```

Ucitel pak:
1. Rozbali `ClassRally-Windows.zip`.
2. Dvojklik na `Spustit-ClassRally.bat` (zadny Python instalovat netreba).
3. Pri prvnim spusteni klikne ve firewallu na **Povolit pristup**.
4. Sam se otevre ucitelsky portal; zaci se pripoji pres QR.

Navody pro ucitele jsou primo v ZIPu (`CTI-ME-PRVNI.txt`, `WIFI-NAVOD.txt`).

### Na serveru (internet, trvala sluzba)

Provoz na serveru s HTTPS — Docker + Caddy, Docker za existujici proxy nebo systemd sluzba —
vcetne uctu, zabezpeceni, zaloh a aktualizaci popisuje [docs/NASAZENI.md](docs/NASAZENI.md).

## Pisemka (test pro znamku)

V ucitelskem portalu (`/admin`) zalozka **Pisemka (test)**:

1. **Vyber sadu** a nastav casovy limit, michani poradi, zakaz kopirovani, hranice znamek (min % pro 1/2/3/4) a pripadne auto-odevzdani po N opusteni okna.
2. **Pripravit pisemku** — aktivuje sadu do rezimu Pisemka (`/play` u zaku se automaticky prepne na `/exam`).
3. **Spustit pisemku** — od ted bezi spolecny casovy limit.
4. **Zivy prehled (dohled)** — u kazdeho zaka vidite: u ktere otazky je, kolik zodpovedel, **kolikrat opustil okno testu** a jak dlouho byl mimo, stav a vyslednou znamku.
5. **Stahnout vysledky (CSV)** — otevrete v Excelu. Krome souhrnu (body, procenta, znamka, dohled) maji
   sloupce `O1…On` s 1/0 za kazdou otazku. Dalsi exporty: **Odpovedi zaku** (kazda odpoved oznacena
   spravne / chybne / bez odpovedi) a **Rozbor otazek**.
6. **Rozbor otazek** — bez jmen, lze ho promitnout tride: uspesnost kazde otazky, kolik zaku zvolilo
   kterou moznost, nejcastejsi chybna odpoved a rozlisovaci schopnost otazky (korigovana korelace
   polozky s celkem). Otazky pod 50 % jsou zvyraznene k opakovani.

**Vysledky se ukladaji prubezne** (kazda odpoved, opusteni okna, odevzdani), takze se neztrati ani
pri restartu serveru. Pisemku prerusenou restartem server pri dalsim startu ohodnoti podle ulozenych
odpovedi. Vsechny pisemky najdete v portalu v casti **Ulozene pisemky** (CSV exporty, rozbor, smazani).

**Dohled / anti-cheat:** detekce prepnuti aplikace/zalozky (Page Visibility API) s varovnim pres celou obrazovku, volitelne auto-odevzdani, zakaz kopirovani a michani poradi otazek i odpovedi kazdemu zakovi jinak. Pozn.: v mobilnim prohlizeci nelze vynutit tvrdy lockdown — opusteni okna se **meri a hlasi ucitelovi**, neda se mu zabranit.

## Proc ClassRally

- **Bezi lokalne** — bez internetu, bez registraci, bez cloudu
- **Zdarma bez limitu** — zadne omezeni poctu hracu (prakticky limit je sit a zarizeni)
- **Data zustavaji u vas** — nic se neodesila do zadne externi sluzby
- **Staci Python nebo Docker** — zadne dalsi zavislosti, zadny npm install
- **Open source / MIT licence** — muzete upravit, rozsirit a provozovat zdarma

### Inspirace Kahootem

ClassRally pouziva overeny koncept: barevne odpovedi, odpocet, bodovani za rychlost, zive hlasovani, zebricek, podium. Ale na rozdil od cloudovych sluzeb bezi kompletne ve vasi siti.

### Herni prvky

- **Odpocet "Priprav se"** pred kazdou otazkou (0-10 s, vychozi 3 — nastavitelne v portalu)
- **Dvojnasobne body** — per otazka (checkbox v editoru) nebo volba "posledni otazka za dvojnasobek"
- **Oceneni na konci hry** — nejrychlejsi prst, mistr serii, ostrostrelec, velky navrat
- **Konfety a slavnostni podium** pro nejlepsi hrace
- **Na mobilu**: prubezne poradi + "ztracis X b na Y", vibrace a volitelne zvuky

## Ucitelsky portal (/admin)

### Sady otazek
- Zobrazeni vsech sad v `questions/`, vyhledavani podle nazvu
- Aktivace sady = nahraje otazky do hry a resetuje stav
- Vytvoreni nove sady, smazani, editace

### Editor otazek
- Vizualni editor: text otazky, 4 moznosti, vyber spravne, vysvetleni
- Checkbox "Dvojnasobne body" u kazde otazky (bool pole `double` v JSON)
- Pridani/odebrani otazek, ulozeni zmen

### AI navrh otazek (pokrocile)
Pokud mate lokalni Ollama server:
1. Zadejte tema (napr. "Linux prikazy", "sitove protokoly")
2. Zvolte pocet otazek a jazyk
3. AI vygeneruje otazky, ktere muzete zkontrolovat a ulozit

Konfigurace AI serveru je v tabulce Pokrocile nastaveni.

### Historie her
- Automaticke ukladani vysledku po kazde hre
- Datum, pocet hracu, zebricek
- **Rozbor hry**: uspesnost tridy u kazde otazky, nejcastejsi chybna odpoved, prumerny cas spravnych
  odpovedi a matice **zaci × otazky** (✓ spravne, ½ castecne, ✗ spatne, – bez odpovedi)
- Export CSV (poradi, body, sloupce `O1…On`) a CSV rozboru otazek

### Rozbor otazek po hre
Po posledni otazce ukaze host obrazovka tlacitko **Rozbor otazek**: u kazde otazky podil spravnych
odpovedi, kolik hracu zvolilo kterou moznost (spravna zelene, nejcastejsi chyba cervene) a vysvetleni.
Otazky pod 50 % jsou oznacene „projit znovu“ a lze je seradit od nejtezsi. Jmena se neukazuji, rozbor
je urceny k promitnuti a spolecne reflexi chyb.

## Ovladani hry (Host obrazovka)

1. **Start** — spusti prvni otazku
2. **Vyhodnotit** — ukonci odpocet a ukaze spravnou odpoved
3. **Dalsi otazka** — rucni prechod (jinak bezi automaticky)
4. **Reset hry** — vrati do cekani, smaze body vsech hracu

### Casovani
- Na odpoved je **20 sekund** (konfigurovatelne 5–120 s); delsi otazka dostane navic cas na cteni
- Pred otazkou bezi odpocet "Priprav se" (0-10 s, vychozi 3) — **pricita se**, 20 s je 20 s s odpovedmi na obrazovce
- Kdyz odpovi vsichni, na koho hra ceka, prijde po kratke pauze 1,5 s ("Vsichni odpovedeli", hudba dozni) vysledek
- Vysledek bezi 5 s (konfigurovatelne), dlouhe vysvetleni prida az 8 s na cteni; pak dalsi otazka

### Kdo se pocita (nikdo neceka na nereagujiciho)
- **Jedno zarizeni = jeden hrac**: opakovane pripojeni ze stejneho prohlizece (dvojite tuknuti, zpet na
  prihlaseni, druha zalozka) pokracuje jako tentyz hrac, pripadne s novou prezdivkou. Prezdivku, kterou
  uz ve hre ma nekdo jiny, server odmitne.
- Hrac, ktery **3 otazky po sobe** neodpovi, prestane hru brzdit: na nej se uz neceka a ucitel vidi
  nabidku "Cekat na nej" / "Vyhodit". Jedna odpoved ho vrati do hry. Prerusene nebo pozde zobrazene
  otazky (ucitel ukazal vysledek driv, preskocil) se nepocitaji.
- Neceka se ani na zarizeni, ktere je odpojene (bez spojeni 15 s), ani na hrace, ktery prisel az
  behem otazky.
- Historie hry uklada **prubeh**: jak skoncila kazda otazka, prumernou reakci tridy a kazdeho hrace,
  kdo prestal reagovat, pripojeni a odpojeni. Rozbor je v portalu u hry ("Prubeh hry").

### Bodovani
- Spravna odpoved: **600 bodu** + bonus za rychlost (az +400)
- Cim rychleji odpovis, tim vice bodu — odpocet "Priprav se" se od casu odecita, takze speed bonus bezi az od zobrazeni odpovedi
- **Dvojnasobne body**: otazka oznacena v editoru (nebo posledni otazka pri zapnute volbe finale) se pocita 2x

### Pravidla hry (portal → Nastaveni)
- **Bonus za rychlost** (vychozi zapnuto): vypnuto = kazda spravna odpoved ma plnych 1000 bodu bez
  ohledu na cas, pomalejsi zaci nejsou znevyhodneni.
- **Ukazovat cely zebricek** (vychozi zapnuto): vypnuto = projektor ani mobily neukazuji poradi
  ostatnich, na konci jen stupne vitezu; kazdy vidi jen sve body.

## QR kod pro pripojeni

Na host obrazovce i hracske obrazovce se automaticky zobrazi QR kod s URL pro pripojeni. Ucitel muze kliknout na QR pro zvetseni na celou obrazovku (idealni pro projektor). Na hracske obrazovce je QR velky, aby ho slo vyfotit i z vetsi vzdalenosti.

## Hudba a zvuky

Vychozi chovani: **vestaveny syntetizator (WebAudio)** — zadne soubory nejsou potreba. Zvukove efekty (spravna/spatna odpoved, tikot odpoctu, drumroll, fanfara) funguji hned po prvnim kliknuti na host obrazovce.

Hudba na pozadi je stale **opt-in** — nikdy nezacne hrat sama. Zapina se tlacitkem **Hudba ON**, pripadne auto rezim pak ridi hudbu podle faze kvizu:
- jedna skladba na celou hru, na dalsi otazce pokracuje, kde prestala;
- zesili se, kdyz se ukazou odpovedi, a dozni tak, aby byla ticho presne na konci otazky (nikdy useknuti uprostred);
- skladba s "heartbeat"/"last-second" v nazvu prevezme poslednich 6 s delsich otazek;
- vysledek ma vzdy stejne poradi: konec casu (gong) → drumroll → spravna odpoved.
Hlasitost skladeb se srovna podle `static/audio/levels.json`, ktery vytvori `python3 measure_audio_levels.py` (potrebuje ffmpeg).

### Vlastni soubory (maji prednost)
Nahrajte MP3/OGG/WAV do `static/audio/` — server je automaticky nacte a pouzije misto syntezatoru:
- Soubory s `stinger`, `reveal`, `hit`, `win`, `ding`, `correct`, `lock`, `end` v nazvu → kratke efekty
- Ostatni → hudba na pozadi (nahodny vyber; `heartbeat`/`last-second` = posledni sekundy, `start`/`intro`/`lobby` = cekarna)

### Stazeni legalni hudby
Skript `./download_free_quiz_music.sh` stahne legalni hudbu a zvukove efekty (Kevin MacLeod, CC-BY — nutna atribuce; Kenney, CC0) do `static/audio/` vcetne souboru `ATTRIBUTION.md`.

## Predpripravene sady otazek (16 sad)

| Tema | Pocet |
|------|:---:|
| VirtualBox, Ubuntu, Docker | 10 |
| TCP/IP, DNS, DHCP, porty, NAT | 8 |
| APT, chmod, procesy, pipe, grep | 8 |
| AD, GPO, Hyper-V, PowerShell, RDP | 8 |
| Phishing, MFA, DDoS, ransomware, XSS | 8 |
| HTML tagy, CSS selektory, Flexbox | 8 |
| Python typy, funkce, pip, virtualenv | 8 |
| SELECT, JOIN, normalizace, agregace | 8 |
| Git init, clone, merge, stash | 8 |
| RAM, SSD, RAID, NVMe, BIOS | 8 |
| Docker, K8s, CI/CD, Terraform | 8 |
| Teams, OneDrive, Excel, Power Automate | 8 |
| Serverove OS | 10 |
| Active Directory a GPO | 12 |
| Sitove sluzby a porty | 12 |
| Virtualizace a kontejnery | 10 |

Celkem **142 otazek**. Dalsi sady lze vytvorit v ucitelskem portalu nebo pres AI.

---

## Konfigurace (pro pokrocile)

### CLI argumenty / ENV promenne

| Promenna | Default | Popis |
|----------|---------|-------|
| `QUIZ_ADMIN_PASSWORD` | _(prazdne)_ | Heslo pro ucitelsky portal |
| `QUIZ_PORT` | `48217` | Port serveru (atypicky vychozi kvuli sdilenym strojum) |
| `QUIZ_EXTERNAL_IP` | _(auto)_ | IP adresa pro URL hracu (Docker) |
| `OLLAMA_HOST` | `localhost` | Adresa AI serveru |
| `OLLAMA_PORT` | `11434` | Port AI serveru |
| `OLLAMA_MODEL` | `gpt-oss:20b` | AI model |
| `QUESTION_TIME` | `20` | Cas na otazku (sekundy) |
| `REVEAL_TIME` | `5` | Cas na vyhodnoceni (sekundy) |
| `QUIZ_BASE_URL` | _(prazdne)_ | Verejna adresa za proxy (QR kody a odkazy), napr. `https://classrally.example.com` |
| `QUIZ_TRUSTED_PROXIES` | _(privatni rozsahy)_ | IP/CIDR proxy, ktere smi nastavit `X-Forwarded-For` |
| `QUIZ_DATA_DIR` | `data/` | Slozka SQLite databaze (ucty, tridy) |
| `QUIZ_ROOMS` | `single` | `multi` = vice ucitelu na jednom serveru: kazdy ma mistnost s PINem a oddelena data (hry, ukoly, tridy, zaci, vlastni sady) |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | _(Ollama)_ | OpenAI-kompatibilni AI API pro generovani otazek (nastavene na serveru = zamcene, klic se neposila do prohlizece) |
| `MAIL_MODE` | `log` | Odchozi posta: `off`, `log` (jen ulozit do `data/outbox`), `live` (SMTP) |
| `MAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD` | — | SMTP pro obnovu hesla, overeni e-mailu a pozvanky ucitelu |

### Zabezpeceni

- **Pristupovy klic** — server pri startu vygeneruje nahodny klic pro ovladani hry. Zobrazen v konzoli a v ucitelskem portalu (Pokrocile nastaveni).
- **Heslo portalu** — volitelne (`--admin-password`), SHA-256, session tokeny (8h), rate limiting.
- **Ucty** — hesla solena PBKDF2-SHA256 (ucitel min. 8 znaku), limit neuspesnych prihlaseni,
  obnova zapomenuteho hesla potvrzenym e-mailem (`/reset`), pozvanky ucitelu e-mailem,
  nouzove heslo `python3 server.py --set-password <prezdivka>`.
- **Ochrana hracu** — kazdy hrac ma tajny identifikator, nelze odpovedet za jineho.
- **XSS prevence** — vsude DOM API, zadny innerHTML s uzivatelskymi daty.
- **Validace** — otazky, ID her i soubory se validuji. Ochrana proti path traversal.

## Struktura projektu

```
classrally/
  server.py                        # hlavni server (stdlib http.server)
  qrgen.py                         # QR kod generator (pure Python)
  backup.py                        # zalohy (7 dennich + 12 mesicnich), kontrola stari, obnova
  Dockerfile                       # Docker image
  docker-compose.yml               # Docker Compose konfigurace
  start.sh / stop.sh / rebuild.sh / logs.sh  # Docker skripty
  docker-common.sh                 # sdilene funkce
  deploy/                          # serverove nasazeni: compose za proxy, Caddy, nginx, systemd
  docs/NASAZENI.md                 # navod: nasazeni, ucty, zalohy, aktualizace
  make-windows-package.sh          # sestavi ClassRally-Windows.zip (prenosny Python)
  windows/
    Spustit-ClassRally.bat         # spousteci skript pro ucitele
    CTI-ME-PRVNI.txt               # rychly start pro ucitele
    WIFI-NAVOD.txt                 # navod na pripojeni zaku
  static/
    host.html                      # ucitelska obrazovka hry (projektor)
    play.html                      # hracska obrazovka hry (mobil)
    exam.html                      # studentska obrazovka pisemky (mobil)
    admin.html                     # ucitelsky portal (vc. rezimu Pisemka)
    style.css                      # spolecne styly
    audio/                         # vlastni MP3/OGG/WAV
  questions/                       # sady otazek (JSON)
  history/                         # historie her (generovano za behu)
  test_server.py                   # unit + integracni testy
```

## Testy

```bash
python3 test_server.py        # unit + integracni testy (hra, pisemka, ucty, zalohy)
./smoke_test.sh               # end-to-end smoke test (hra i pisemka)
PORT=48999 ./smoke_test.sh    # na jinem portu, kdyz je vychozi obsazeny
```

## API Reference

### Verejne endpointy

| Metoda | Endpoint | Popis |
|--------|----------|-------|
| GET | `/api/health` | Health check |
| GET | `/api/state?player_id=X&host=1` | Stav hry |
| GET | `/api/network` | Sitove info (URL pro zaky) |
| GET | `/api/qr` | QR kod (SVG) |
| GET | `/api/audio-tracks` | Seznam audio souboru |
| POST | `/api/register` | `{"name":"Jmeno"}` → registrace hrace |
| POST | `/api/submit` | `{"player_id":"X","player_secret":"S","choice":0}` |
| POST | `/api/host/action` | `{"action":"start\|reveal\|next\|reset\|save_history"}` |
| GET | `/api/mode` | Aktivni rezim (`game` / `exam`) — `/play` se podle nej prepina |

### Pisemka (exam) endpointy

| Metoda | Endpoint | Popis |
|--------|----------|-------|
| GET | `/api/exam/state?player_id=X&secret=S` | Stav pisemky pro zaka (otazky, cas, vysledek) |
| POST | `/api/exam/answer` | `{"player_id","player_secret","question_id","choice"}` (choice = original `oid`) |
| POST | `/api/exam/position` | `{"player_id","player_secret","position"}` (pro zivy prehled) |
| POST | `/api/exam/event` | `{"player_id","player_secret","type":"blur\|focus"}` (dohled) |
| POST | `/api/exam/submit` | Odevzdani + znamkovani |
| POST | `/api/host/exam-action` | host token: `{"action":"open\|end\|extend\|reset"}` |

### Admin endpointy

| Metoda | Endpoint | Popis |
|--------|----------|-------|
| POST | `/api/admin/login` | Prihlaseni (pokud je heslo nastavene) |
| GET | `/api/admin/banks` | Seznam sad otazek |
| GET | `/api/admin/bank?filename=X` | Nacist sadu |
| POST | `/api/admin/bank/save` | Ulozit sadu |
| POST | `/api/admin/bank/delete` | Smazat sadu |
| POST | `/api/admin/bank/activate` | Aktivovat sadu (`{"filename","mode":"game\|exam"}`) |
| POST | `/api/admin/mode` | Prepnout rezim (`{"mode":"game\|exam"}`) |
| GET/POST | `/api/admin/exam/config` | Cist/ulozit nastaveni pisemky |
| GET | `/api/admin/exam/overview` | Zivy prehled zaku (dohled) |
| GET | `/api/admin/exam/results.csv` | Export vysledku (CSV pro Excel, vc. sloupcu O1…On) |
| GET | `/api/admin/exam/answers.csv` | Odpovedi zaku (kazda oznacena spravne/chybne) |
| GET | `/api/admin/exam/items.csv` | Rozbor otazek (uspesnost, nejcastejsi chyba, korelace s celkem) |
| GET | `/api/admin/exam/items` | Rozbor otazek jako JSON (bez jmen) |
| GET | `/api/admin/exams` | Ulozene pisemky |
| GET | `/api/admin/exams/{results,answers,items}.csv?id=X`, `/api/admin/exams/detail?id=X` | Exporty a rozbor ulozene pisemky |
| POST | `/api/admin/exams/delete` | Smazat ulozenou pisemku (`{"id"}`) |
| POST | `/api/admin/timing` | Casovani hry a pravidla (`speed_bonus`, `show_leaderboard`) |
| GET | `/api/admin/history` | Historie her (bez detailu po otazkach) |
| GET | `/api/admin/history/detail?game_id=X` | Hra vc. odpovedi po otazkach a rozboru |
| GET | `/api/admin/history/export?game_id=X`, `/api/admin/history/items.csv?game_id=X` | CSV vysledku a rozboru otazek |
| GET | `/api/admin/activity` | Kolik se prave hraje a pise, bez jmen (pri vice ucitelich jen spravce serveru s heslem portalu) |
| POST | `/api/admin/ai/generate` | AI generovani otazek |

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
