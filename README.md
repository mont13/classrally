# ClassRally - Kvizova platforma pro tridu

ClassRally je lokalni kviz pro tridu. Ucitel ho spusti na notebooku, promita host obrazovku na projektor a zaci se pripoji pres QR kod z mobilu. Internet ani studentske ucty nejsou potreba — vse bezi v lokalni siti.

Staci Python nebo Docker, nic dalsiho neni treba instalovat.

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

### Bez Dockeru

```bash
python3 server.py --host 0.0.0.0 --port 8765
```

Server vypise URL a pristupovy klic:

```
Host screen:   http://192.168.1.10:8765/host
Player screen: http://192.168.1.10:8765/play
Admin portal:  http://192.168.1.10:8765/admin
*** HOST TOKEN: a1b2c3d4e5f6... ***
```

| Adresa | Kdo ji pouziva | K cemu slouzi |
|--------|---------------|---------------|
| `/host` | Ucitel (projektor) | Ridici obrazovka — otazky, zebricek, hudba, QR kod |
| `/play` | Zaci (mobily) | Prihlaseni a odpovedi |
| `/admin` | Ucitel (PC) | Sprava sad otazek, AI generator, historie, nastaveni |

Mobily musi byt ve stejne Wi-Fi siti jako server.

## Proc ClassRally

- **Bezi lokalne** — bez internetu, bez registraci, bez cloudu
- **Zdarma bez limitu** — zadne omezeni poctu hracu (prakticky limit je sit a zarizeni)
- **Data zustavaji u vas** — nic se neodesila do zadne externi sluzby
- **Staci Python nebo Docker** — zadne dalsi zavislosti, zadny npm install
- **Open source** — muzete upravit, rozsirit, pouzit jak chcete

### Inspirace Kahootem

ClassRally pouziva overeny koncept: barevne odpovedi, odpocet, bodovani za rychlost, zive hlasovani, zebricek, podium. Ale na rozdil od cloudovych sluzeb bezi kompletne ve vasi siti.

## Ucitelsky portal (/admin)

### Sady otazek
- Zobrazeni vsech sad v `questions/`, vyhledavani podle nazvu
- Aktivace sady = nahraje otazky do hry a resetuje stav
- Vytvoreni nove sady, smazani, editace

### Editor otazek
- Vizualni editor: text otazky, 4 moznosti, vyber spravne, vysvetleni
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

## Ovladani hry (Host obrazovka)

1. **Start** — spusti prvni otazku
2. **Vyhodnotit** — ukonci odpocet a ukaze spravnou odpoved
3. **Dalsi otazka** — rucni prechod (jinak bezi automaticky)
4. **Reset hry** — vrati do cekani, smaze body vsech hracu

### Casovani
- Kazda otazka bezi **20 sekund** (konfigurovatelne 5–120s)
- Pokud odpovi vsichni driv, vyhodnoceni probehne okamzite
- Po vyhodnoceni system automaticky prejde na dalsi otazku

### Bodovani
- Spravna odpoved: **600 bodu** + bonus za rychlost (az +400)
- Cim rychleji odpovis, tim vice bodu

## QR kod pro pripojeni

Na host obrazovce i hracske obrazovce se automaticky zobrazi QR kod s URL pro pripojeni. Ucitel muze kliknout na QR pro zvetseni na celou obrazovku (idealni pro projektor). Na hracske obrazovce je QR velky, aby ho slo vyfotit i z vetsi vzdalenosti.

## Hudba a zvuky

Audio soubory nejsou soucasti repozitare (licence). Nahrajte vlastni MP3/OGG/WAV do `static/audio/` — server je automaticky nacte a nahodne vybira.

Pokud zadne soubory nenahrajete, host obrazovka pouzije vestaveny syntetizator (WebAudio) — vse funguje i bez MP3.

### Automaticky rezim (default)
- Pri otazce se spusti hudba
- Pri vyhodnoceni se pusti zvukovy efekt
- Kdyz vsichni odpovedeli, zazni kratke upozorneni

### Vlastni soubory
- Soubory s `stinger`, `reveal`, `hit`, `win`, `ding`, `correct`, `lock`, `end` v nazvu → kratke efekty
- Ostatni → hudba na pozadi

## Predpripravene sady otazek (12 temat)

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

Celkem **90 otazek**. Dalsi sady lze vytvorit v ucitelskem portalu nebo pres AI.

---

## Konfigurace (pro pokrocile)

### CLI argumenty / ENV promenne

| Promenna | Default | Popis |
|----------|---------|-------|
| `QUIZ_ADMIN_PASSWORD` | _(prazdne)_ | Heslo pro ucitelsky portal |
| `QUIZ_EXTERNAL_IP` | _(auto)_ | IP adresa pro URL hracu (Docker) |
| `OLLAMA_HOST` | `localhost` | Adresa AI serveru |
| `OLLAMA_PORT` | `11434` | Port AI serveru |
| `OLLAMA_MODEL` | `gpt-oss:20b` | AI model |
| `QUESTION_TIME` | `20` | Cas na otazku (sekundy) |
| `REVEAL_TIME` | `5` | Cas na vyhodnoceni (sekundy) |

### Zabezpeceni

- **Pristupovy klic** — server pri startu vygeneruje nahodny klic pro ovladani hry. Zobrazen v konzoli a v ucitelskem portalu (Pokrocile nastaveni).
- **Heslo portalu** — volitelne (`--admin-password`), SHA-256, session tokeny (8h), rate limiting.
- **Ochrana hracu** — kazdy hrac ma tajny identifikator, nelze odpovedet za jineho.
- **XSS prevence** — vsude DOM API, zadny innerHTML s uzivatelskymi daty.
- **Validace** — otazky, ID her i soubory se validuji. Ochrana proti path traversal.

## Struktura projektu

```
classrally/
  server.py                        # hlavni server (stdlib http.server)
  qrgen.py                         # QR kod generator (pure Python)
  Dockerfile                       # Docker image
  docker-compose.yml               # Docker Compose konfigurace
  start.sh / stop.sh / rebuild.sh / logs.sh  # Docker skripty
  docker-common.sh                 # sdilene funkce
  static/
    host.html                      # ucitelska obrazovka (projektor)
    play.html                      # hracska obrazovka (mobil)
    admin.html                     # ucitelsky portal
    style.css                      # spolecne styly
    audio/                         # vlastni MP3/OGG/WAV
  questions/                       # sady otazek (JSON)
  history/                         # historie her (generovano za behu)
  test_server.py                   # 77 unit + integracnich testu
```

## Testy

```bash
python3 test_server.py        # 77 unit + integracnich testu
./smoke_test.sh               # end-to-end smoke test
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

### Admin endpointy

| Metoda | Endpoint | Popis |
|--------|----------|-------|
| POST | `/api/admin/login` | Prihlaseni (pokud je heslo nastavene) |
| GET | `/api/admin/banks` | Seznam sad otazek |
| GET | `/api/admin/bank?filename=X` | Nacist sadu |
| POST | `/api/admin/bank/save` | Ulozit sadu |
| POST | `/api/admin/bank/delete` | Smazat sadu |
| POST | `/api/admin/bank/activate` | Aktivovat sadu do hry |
| POST | `/api/admin/timing` | Nastavit casovani |
| GET | `/api/admin/history` | Historie her |
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
