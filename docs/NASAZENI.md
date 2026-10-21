# Nasazeni a provoz ClassRally

Navod, jak ClassRally provozovat **lokalne** (notebook ucitele, PC ve tride) nebo **na serveru
jako trvalou sluzbu** dostupnou z internetu: instalace, ucty, zabezpeceni, zalohy, aktualizace
a kontrola, ze vse bezi.

## Prehled variant

| Varianta | Kdy ji zvolit | Spusteni | HTTPS |
|----------|---------------|----------|-------|
| Lokalne – Docker | notebook ucitele (Linux/Mac), trida v jedne Wi-Fi | `./start.sh` | ne |
| Lokalne – Python | cokoli s Pythonem 3.10+ | `python3 server.py --host 0.0.0.0` | ne |
| Lokalne – Windows | ucitel na Windows | `Spustit-ClassRally.bat` (viz README) | ne |
| Server A – Docker + Caddy | samostatny server jen pro ClassRally | `deploy/docker-compose.web.yml` | automaticky |
| Server B – Docker za proxy | server, kde uz bezi nginx/Traefik s dalsimi weby | `deploy/docker-compose.server.yml` | na proxy |
| Server C – systemd | server bez Dockeru | `deploy/systemd/classrally.service` | na proxy |

## Co aplikace potrebuje

- **Python 3.10+** (jen standardni knihovna, zadne balicky) nebo **Docker** (Compose 2.24+).
- **Jeden TCP port**, vychozi `48217` (`QUIZ_PORT`).
- **Zapisovatelna data** (cesty relativne ke slozce aplikace):

| Slozka | Obsah | Zalohovat |
|--------|-------|-----------|
| `data/` | SQLite `classrally.db` — ucty, tridy, ukoly (cesta jde zmenit `QUIZ_DATA_DIR`) | ano |
| `history/` | vysledky odehranych her (JSON) | ano |
| `questions/` | sady otazek (JSON), ucitel je upravuje v `/admin` | ano |
| `static/uploads/` | obrazky nahrane k otazkam | ano |
| `static/audio/` | volitelna hudba (viz README) | ne, jde stahnout znovu |

- Volitelne **AI server** (Ollama nebo OpenAI-kompatibilni API) pro generovani otazek:
  `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`.
- Konfigurace serveru: sablona [`deploy/classrally.env.example`](../deploy/classrally.env.example).

## 1. Lokalni provoz (trida, jedna Wi-Fi)

Zakladni spusteni je v [README](../README.md#spusteni). Pro opakovany provoz ve tride:

- **Heslo portalu:** nastavte `QUIZ_ADMIN_PASSWORD`, jinak je `/admin` v siti otevreny
  komukoli, dokud neexistuje ucitelsky ucet.
- **Ucitelsky ucet:** zalozte hned po prvnim spusteni (uvodni stranka → registrace ucitele).
  Prvni ucitel ziska plna prava; dalsi se uz jako ucitel sam nezaregistruje (viz kap. 2.5).
- **Automaticky start po zapnuti pocitace:**
  - Docker: `docker-compose.yml` ma `restart: unless-stopped`, ClassRally nabehne s Dockerem.
  - Bez Dockeru: systemd sluzba z kap. 2.4, jen v `ExecStart` zmente `--host 127.0.0.1` na
    `--host 0.0.0.0` (zaci se pripojuji primo, bez proxy) a port povolte ve firewallu jen pro
    lokalni sit.
- Zaci musi byt ve stejne siti jako pocitac se serverem; adresu a QR kod ukazuje `/host`.

## 2. Server jako sluzba (internet)

### 2.1 Nejdriv zabezpecit, pak zverejnit

Poradi kroku je dulezite:

1. **Nastavte `QUIZ_ADMIN_PASSWORD`.** Bez nej je `/admin` otevreny komukoli, dokud neexistuje
   ucitelsky ucet.
2. **Hned po startu zalozte prvniho ucitele — jeste nez je aplikace videt z internetu.**
   Registrace ucitele je otevrena jen do zalozeni prvniho ucitelskeho uctu a ucitel ma
   v aplikaci plna prava. Kdo by prisel prvni, ziskal by je on.
3. **Teprve potom aplikaci zpristupnete** pres reverzni proxy s HTTPS.

Dalsi pravidla:

- Aplikaci vazte jen na `127.0.0.1` nebo na interni IP, kterou vidi proxy — nikdy na verejnou
  adresu ani `0.0.0.0`. Z internetu jen pres proxy.
- Proxy musi hlavicku `X-Forwarded-For` **prepsat** skutecnou IP klienta (nginx `$remote_addr`;
  Caddy to dela sam), ne k ni pripojovat. Aplikace bere levou polozku jako IP klienta pro
  omezeni pokusu o prihlaseni. Sablony v `deploy/` to delaji spravne.
- `QUIZ_TRUSTED_PROXIES` = adresa, ze ktere aplikace vidi pripojeni proxy: systemd a proxy na
  stejnem stroji `127.0.0.1`; Docker a proxy na stejnem stroji `172.17.0.1` (docker0) nebo
  prazdne; proxy na jinem stroji jeji IP. Prazdne = duvera vsem privatnim rozsahum. Spatna
  hodnota zpusobi, ze vsichni klienti sdili jeden limit pokusu — 5 spatnych hesel od kohokoli
  pak na 5 minut zablokuje prihlaseni heslem portalu vsem.
- WebSocket `/api/ws` potrebuje v proxy hlavicky `Upgrade`/`Connection` a dlouhy timeout.
- Hesla patri do konfiguracniho souboru s pravy 600/640 mimo git, ne na prikazovou radku.

**Zalozeni prvniho ucitele** z prikazove radky serveru (heslo se nezobrazi ani neulozi do
historie shellu; pouzijte heslo bez uvozovek a zpetnych lomitek):

```bash
read -r -p "Prezdivka ucitele: " NICK; read -r -s -p "Heslo: " PW; echo
printf '{"nickname":"%s","password":"%s","role":"teacher"}' "$NICK" "$PW" \
  | curl -s -H 'Content-Type: application/json' --data-binary @- \
      http://127.0.0.1:48217/api/auth/register
curl -s http://127.0.0.1:48217/api/setup-status      # ocekavano {"setup_needed": false}
```

Kdyz nemate na serveru `curl`, staci SSH tunel a registrace v prohlizeci:
`ssh -L 48217:127.0.0.1:48217 uzivatel@server`, pak `http://127.0.0.1:48217/`.

### 2.2 Varianta A: Docker + Caddy (samostatny server)

Caddy zaridi HTTPS certifikat sam. Server potrebuje volne porty 80 a 443 a DNS zaznam domeny
mirici na server. Data lezi ve slozce s aplikaci (`data/`, `history/`, `questions/`,
`static/uploads/`), kontejner bezi pod uctem `classrally` bez Linux capabilities.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin classrally
sudo git clone https://github.com/mont13/classrally.git /opt/classrally
cd /opt/classrally
sudo mkdir -p data history static/uploads static/audio
sudo chown -R classrally:classrally data history questions static/uploads
# konfigurace mimo git (prava 600)
sudo install -d -m 700 /etc/classrally
sudo sh -c 'umask 077; printf "CLASSRALLY_DOMAIN=%s\nQUIZ_ADMIN_PASSWORD=%s\nCLASSRALLY_UID=%s\nCLASSRALLY_GID=%s\n" \
  classrally.example.com "$(openssl rand -base64 24)" "$(id -u classrally)" "$(id -g classrally)" \
  > /etc/classrally/classrally.env'
DC="sudo docker compose --env-file /etc/classrally/classrally.env -f docker-compose.yml -f deploy/docker-compose.web.yml"

# 1) jen aplikace (Caddy jeste ne — zvenku neni nic videt)
$DC up -d --build classrally
# 2) prvni ucitel uvnitr kontejneru (image nema curl, posle se pres Python)
read -r -p "Prezdivka ucitele: " NICK; read -r -s -p "Heslo: " PW; echo
printf '{"nickname":"%s","password":"%s","role":"teacher"}' "$NICK" "$PW" | $DC exec -T classrally \
  python3 -c "import sys,urllib.request as u; print(u.urlopen(u.Request('http://127.0.0.1:48217/api/auth/register', sys.stdin.buffer.read(), {'Content-Type': 'application/json'})).read().decode())"
# 3) zverejnit: Caddy ziska certifikat a spusti HTTPS
$DC up -d
```

Heslo portalu si prectete z `/etc/classrally/classrally.env` (`sudo cat …`).

### 2.3 Varianta B: Docker za existujici reverzni proxy

Pro server, kde uz bezi nginx (nebo jina proxy) pro vic webu. Kontejner bezi pod
neprivilegovanym uctem, bez Linux capabilities, a port vystavuje jen na zadane adrese.

```bash
# 1) systemovy ucet a datove slozky
sudo useradd --system --no-create-home --shell /usr/sbin/nologin classrally
sudo install -d -o classrally -g classrally /srv/classrally
for d in data history questions uploads audio; do sudo install -d -o classrally -g classrally /srv/classrally/$d; done
# 2) kod + vychozi sady otazek
sudo git clone https://github.com/mont13/classrally.git /opt/classrally
sudo install -o classrally -g classrally -m 644 /opt/classrally/questions/*.json /srv/classrally/questions/
# 3) konfigurace mimo git (prava 600)
sudo install -d -m 700 /etc/classrally
sudo install -m 600 /opt/classrally/deploy/classrally.env.example /etc/classrally/classrally.env
sudoedit /etc/classrally/classrally.env
#    QUIZ_BASE_URL, QUIZ_ADMIN_PASSWORD
#    QUIZ_TRUSTED_PROXIES=            (proxy na stejnem stroji: prazdne nebo 172.17.0.1)
#    CLASSRALLY_BIND_ADDR=127.0.0.1   (proxy na jinem stroji: interni IP serveru, viz nize)
#    CLASSRALLY_DATA_DIR=/srv/classrally
#    CLASSRALLY_UID / CLASSRALLY_GID = vystup "id -u classrally" / "id -g classrally"
# 4) start a kontrola
cd /opt/classrally
sudo docker compose --env-file /etc/classrally/classrally.env -f deploy/docker-compose.server.yml -p classrally up -d --build
curl -s http://127.0.0.1:48217/api/health
```

Pak: prvni ucitel (kap. 2.1), proxy podle [`deploy/nginx/nginx.conf`](../deploy/nginx/nginx.conf)
(upstream na `CLASSRALLY_BIND_ADDR:CLASSRALLY_PORT`), certifikat certbotem. Pred kazdym
reloadem nginx `nginx -t`.

Kdyz proxy bezi na jinem stroji a port je vystaveny na interni IP, omezte pristup k nemu jen na
proxy. Pozor: porty zverejnene Dockerem obchazi UFW — pravidla patri do retezce `DOCKER-USER`
(iptables), ne do UFW.

### 2.4 Varianta C: systemd bez Dockeru

Aplikace bezi jako sluzba pod uctem `classrally`. Kod je pro sluzbu jen ke cteni, zapisovat
smi jen do datovych slozek (`ProtectSystem=strict` v [`classrally.service`](../deploy/systemd/classrally.service)).

```bash
sudo useradd --system --no-create-home --home-dir /opt/classrally --shell /usr/sbin/nologin classrally
sudo git clone https://github.com/mont13/classrally.git /opt/classrally
cd /opt/classrally
sudo mkdir -p data history static/uploads
sudo chown -R classrally:classrally data history questions static/uploads
sudo install -d -m 750 -o root -g classrally /etc/classrally
sudo install -m 640 -o root -g classrally deploy/classrally.env.example /etc/classrally/classrally.env
sudoedit /etc/classrally/classrally.env          # QUIZ_BASE_URL, QUIZ_ADMIN_PASSWORD, QUIZ_TRUSTED_PROXIES=127.0.0.1
sudo cp deploy/systemd/classrally.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now classrally
systemctl status classrally --no-pager
curl -s http://127.0.0.1:48217/api/health
```

Pak: prvni ucitel (kap. 2.1) a nginx podle [`deploy/nginx/nginx.conf`](../deploy/nginx/nginx.conf).

### 2.5 Ucty

**Na serveru (operacni system):**

| Ucet | K cemu | Prava |
|------|--------|-------|
| `root` / sudo | instalace, aktualizace, konfigurace proxy a sluzby | plna |
| `classrally` (systemovy, bez shellu) | bezi pod nim aplikace (systemd `User=`, Docker `CLASSRALLY_UID/GID`) | zapis jen do datovych slozek |
| clenove skupiny `docker` | sprava kontejneru | fakticky root — jen spravci |

Aplikaci nikdy nespoustejte pod rootem.

**V aplikaci:**

| Ucet | Jak vznikne | Co muze |
|------|-------------|---------|
| Heslo portalu (`QUIZ_ADMIN_PASSWORD`) | konfigurace serveru | vstup do `/admin` bez osobniho uctu (nouzovy, sdileny); prihlaseni plati 8 h, max. 5 pokusu za 5 min z jedne IP |
| Ucitel | prvni se zaregistruje sam (jen dokud zadny ucitel neexistuje), dalsi na pozvanku e-mailem | `/admin`, rizeni hry na `/host`, sprava uzivatelu a trid, pozvanky |
| Zak | sam se zaregistruje (volitelne) | hrat, tridy, ukoly, profil — zadna sprava |
| Hrac bez uctu | prezdivka na `/play` | zahrat si hru |

- Pristupovy klic pro `/host` se generuje pri kazdem startu serveru; ucitel ho dostane
  automaticky po prihlaseni na `/host`. Po restartu serveru staci `/host` znovu nacist.
- Hesla: ucitel min. 8 znaku, zak min. 4; ukladaji se jako solene PBKDF2-SHA256 (stare
  databaze s nesolenym SHA-256 se prevedou samy pri dalsim prihlaseni). Pro heslo portalu
  pouzijte dlouhe nahodne heslo (napr. `openssl rand -base64 24`).
- Prihlaseni do uctu: prezdivkou, nebo potvrzenym e-mailem uctu (nepotvrzena adresa se
  neprihlasi). Na 5 minut se zablokuje po 8 spatnych pokusech na prezdivku z jedne IP,
  po 40 na prezdivku celkem nebo po 50 z jedne IP (utocnik z jedne adresy tak ucitele
  nezamkne pro ostatni). Pokusy pres e-mail se pocitaji zvlast: spolecny zamek by
  prozradil, ke kteremu uctu adresa patri.
- Zapomenute heslo: na `/reset` (odkaz „Zapomenute heslo?“ je u kazdeho prihlaseni:
  `/admin`, `/host`, `/profile`, uvodni stranka) si ho obnovi kazdy, kdo ma u uctu **potvrzeny e-mail**
  (odkaz plati 60 minut, jen jednou, a odhlasi vsechna zarizeni). Bez e-mailu heslo nastavi
  ucitel (Uzivatele → Reset hesla). E-mail se nastavuje pri registraci nebo v profilu a
  potvrzuje odkazem z e-mailu. Vyzaduje nastavene odesilani posty (kap. 2.6).
- Registrace zaku je na verejnem serveru otevrena komukoli; zakovsky ucet nema zadna prava,
  nevhodne ucty ucitel smaze.

**Dalsi ucitel:** v `/admin` → Uzivatele → *Pozvat ucitele* zadate jeho e-mail; prijde mu odkaz
(plati 7 dni, jen jednou) na zalozeni ucitelskeho uctu s uz potvrzenym e-mailem.

Ucitel spravuje jen zakovske ucty (reset hesla, smazani, povyseni na ucitele). Ucitelsky ucet
nemuze jiny ucitel resetovat, smazat ani sesadit — zmeni ho jen jeho majitel, nebo spravce
serveru prikazem `--set-password` (nize).

Bez e-mailu: zaregistruje se jako zak a stavajici ucitel mu zmeni roli (stejne API slouzi
i na reset hesla a smazani uctu). Pri `QUIZ_ROOMS=multi` (kap. 2.8) spravuje ucitel jen zaky
svych trid — budouci ucitel proto nejdriv vstoupi kodem do jeho tridy, nebo roli zmeni spravce
serveru z konzole (`--set-role`, nize):

```bash
B=http://127.0.0.1:48217
read -r -p "Vas ucitelsky ucet: " NICK; read -r -s -p "Heslo: " PW; echo
TOKEN=$(printf '{"nickname":"%s","password":"%s"}' "$NICK" "$PW" \
  | curl -s -H 'Content-Type: application/json' --data-binary @- $B/api/auth/login \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: UserToken $TOKEN" $B/api/users                  # najdete id uctu
curl -s -H "Authorization: UserToken $TOKEN" -H 'Content-Type: application/json' \
  -d '{"role":"teacher"}' $B/api/users/<id>/set-role
# reset hesla:  ... -d '{"new_password":"..."}' $B/api/users/<id>/reset-password
# smazani:      ... -X POST $B/api/users/<id>/delete
```

**Nouzove heslo z prikazove radky** (napr. kdyz jediny ucitel zapomene heslo a nema e-mail).
Nove heslo se zada bez zobrazeni, vsechna prihlaseni uctu se ukonci:

```bash
sudo -u classrally python3 /opt/classrally/server.py --set-password <prezdivka>    # systemd
sudo docker exec -it classrally python3 server.py --set-password <prezdivka>       # Docker
```

Stejne se z konzole meni role (napr. odebrani ucitele): `python3 server.py --set-role <prezdivka> student`.

### 2.6 E-mail (obnova hesla, pozvanky)

Odchozi posta ma tri rezimy (`MAIL_MODE`):

| Rezim | Co se deje |
|-------|------------|
| `off` | nic se neposila ani neuklada |
| `log` | **vychozi** — kazda zprava se jen ulozi do `data/outbox/*.eml` (prava 600), nic neodejde |
| `live` | odesila se pres SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY=starttls\|ssl`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`) |

- Odkazy v e-mailech se skladaji z `QUIZ_BASE_URL`, nikdy z hlavicky pozadavku — na serveru
  ji vzdy nastavte.
- Pred prepnutim na `live` overte prihlaseni k SMTP **bez odeslani zpravy**:

  ```bash
  read -r -s -p "SMTP heslo: " PW; echo
  printf '%s' "$PW" | python3 -c "import smtplib,ssl,sys; s=smtplib.SMTP('mail.example.com',587,timeout=20); s.starttls(context=ssl.create_default_context()); print(s.login('classrally@example.com', sys.stdin.read())); s.quit()"
  ```

- Do `live` prepinejte vedome: odchazi z nej skutecne zpravy (overeni e-mailu, obnova hesla,
  oznameni o zmene hesla, pozvanky). Zmena rezimu vyzaduje restart.
- `data/outbox/` obsahuje odkazy s jednorazovymi tokeny — nesdilejte ho a stare soubory mazte.
- U nezletilych zaku je e-mail volitelny; bez nej heslo obnovi ucitel.

### 2.7 AI generator otazek

Generator vola OpenAI-kompatibilni API (napr. Ollama nebo jinou sluzbu): `AI_BASE_URL`
(bez `/v1`, aplikace si ho doplni), `AI_API_KEY`, `AI_MODEL`.

- Kdyz je `AI_BASE_URL` nastaveny v konfiguraci serveru, je nastaveni **zamcene**: ucitele
  vybiraji jen model, adresu AI serveru v portalu jen vidi, klic se do prohlizece nedostane
  a server nevola adresy zadane z prohlizece.
- Bez nej (lokalni provoz) jde AI server nastavit v `/admin` → Nastaveni; klic se ulozi jen
  v pameti serveru a do prohlizece se nevraci.
- Ucitel muze zadat tema, pocet otazek, jazyk (cs, en, sk, de, uk) a volitelne podkladovy
  text (napr. odstavec z ucebnice) — otazky pak vzniknou jen z nej. Poradi odpovedi se po
  vygenerovani nahodne zamicha.

### 2.7a Hlaseni chyb do centralniho collectoru (volitelne)

Kdyz bezi vlastni sluzba na sber hlaseni (API `POST /api/reports` s `projectKey`), muze
ClassRally na kazde strance ukazat tlacitko pro nahlaseni chyby se snimkem obrazovky.

- Nastaveni serveru: `BUGGY_URL` (adresa collectoru), `BUGGY_PROJECT_KEY` (klic projektu),
  `BUGGY_ORIGIN` (verejna adresa ClassRally, ktera je v collectoru povolena) a
  `BUGGY_WIDGET_DIR` se soubory widgetu (`widget.js`, `widget.css`, `capture.css`). Ve
  variante B patri soubory do `CLASSRALLY_DATA_DIR/buggy`. Bez techto hodnot je hlaseni vypnute.
- Prohlizec posila hlaseni jen na vlastni server (`/api/bug-report`); ten ho zkontroluje,
  odstrani z popisu e-maily, hesla a tokeny a preda ho collectoru jednim pokusem. Zadny
  administratorsky klic collectoru server nepotrebuje.
- Ve snimku jsou zakryta pole hesel a tokenu. Zaci se do hlaseni nepodepisuji, u ucitele se
  prida jeho prezdivka. Omezeni: 60 hlaseni za 10 minut z jedne adresy, 300 za hodinu celkem.

### 2.8 Vice ucitelu na jednom serveru (mistnosti s PINem)

Vychozi `QUIZ_ROOMS=single` je jedna spolecna hra — stejne jako na notebooku ve tride.
Na verejnem serveru pro vic ucitelu nastavte `QUIZ_ROOMS=multi` (a restartujte):

- Kazdy ucitel ma po otevreni `/host` **vlastni mistnost se sestimistnym PINem**. Zaci ho
  zadaji pri pripojeni; QR kod a odkaz na obrazovce ucitele uz PIN obsahuji. Tlacitko
  **Novy PIN** (dalsi hodina) vyprazdni cekarnu a da mistnosti PIN, ktery predchozi trida
  nezna. Zak, ktery otevre odkaz jine mistnosti, zacina tam znovu (nepokracuje ve stare hre).
- Ucitel vidi **jen sva data**: historii svych her, sve domaci ukoly a jejich vysledky,
  sve tridy a zaky v nich. Ostatni ucitele a jejich zaci jsou oddeleni.
- **Ucty zaku:** heslo resetuje, ucet smaze nebo roli zmeni ucitel jen u zaka, ktery je
  vyhradne v jeho tridach. Zaka sdileneho s kolegou (napr. matematika u jednoho, fyzika
  u druheho) muze jen vyradit ze sve tridy; heslo si zak obnovi e-mailem, pripadne pomuze
  spravce serveru z konzole (`--set-password`).
- **Sady otazek:** vlastni sady ucitele (`moje/…`, na disku `questions/users/<id>/`) muze
  menit jen on. Spolecne sady ve `questions/` jsou pro ucitele jen ke cteni — kdyz nejakou
  upravi, ulozi se mu jako vlastni kopie (s volnym nazvem, existujici vlastni sadu
  neprepise). Spolecne sady spravuje heslo portalu (`QUIZ_ADMIN_PASSWORD`), ktere vidi vse
  a hraje ve vychozi mistnosti.
- **AI:** vychozi nastaveni AI (server, klic, model) je spolecne a meni ho jen heslo portalu;
  ucitel voli model u kazdeho generovani.
- **Hadani PINu:** po 100 ruznych neexistujicich PINech za 10 minut z jedne adresy server
  dalsi pokusy z ni na chvili odmitne (HTTP 429). Stejny neplatny PIN (napr. stary PIN ve
  vsech mobilech skoly po restartu) se pocita jen jednou.
- Mistnosti jsou v pameti: restart serveru rozehrane hry ukonci. Mistnost, kterou 12 hodin
  nikdo nepouzil, zanikne i s hraci (pristi hodina dostane novy PIN); necha se jen mistnost
  s rozepsanou nebo neexportovanou pisemkou. Vysledky her, ukolu a ucty zustavaji
  v databazi. Hry a ukoly z doby pred zapnutim `multi` (bez vlastnika) vidi jen heslo portalu.

## 3. Provoz

### Kontrola, ze bezi

```bash
U=https://classrally.example.com
curl -s $U/api/health                                          # {"ok": true, ...}
curl -s $U/api/setup-status                                    # {"setup_needed": false}
curl -s $U/api/admin/auth-status                               # "auth_required": true
curl -s -o /dev/null -w '%{http_code}\n' $U/api/admin/banks    # 401
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://classrally.example.com/play   # 301 (nginx) / 308 (Caddy) https://...
```

WebSocket: na `/play` v prohlizeci (Vyvojarske nastroje → Network → WS) musi byt spojeni na
`wss://…/api/ws` se stavem 101. A nakonec cela hra: ucitel na `/host`, zak z mobilu pres QR.

### Logy

- Docker: `docker logs -f classrally` (varianta A: `docker compose … logs -f classrally`)
- systemd: `journalctl -u classrally -f`

Pri startu se do logu vypisuje pristupovy klic hosta — logy nesdilejte.

### Aktualizace

Pred aktualizaci udelejte zalohu (nize).

```bash
# Varianta B (Docker za proxy)
cd /opt/classrally && sudo git pull
sudo docker tag classrally:server classrally:prev        # predchozi verze pro navrat
sudo docker compose --env-file /etc/classrally/classrally.env -f deploy/docker-compose.server.yml -p classrally up -d --build
curl -s http://127.0.0.1:48217/api/health
#   navrat: sudo docker tag classrally:prev classrally:server && sudo docker compose … up -d --no-build

# Varianta A (Docker + Caddy)
cd /opt/classrally && sudo git pull \
  && sudo docker compose --env-file /etc/classrally/classrally.env -f docker-compose.yml -f deploy/docker-compose.web.yml up -d --build

# Varianta C (systemd) — git pri aktualizaci prepise vlastnika souboru, proto chown
cd /opt/classrally && sudo git pull \
  && sudo chown -R classrally:classrally data history questions static/uploads \
  && sudo systemctl restart classrally
```

Ve variantach A a C jsou sady otazek v `questions/` soucasti gitu: kdyz ucitel upravil
nekterou z vychozich sad, `git pull` ji odmitne prepsat — zalohujte ji a slucte rucne.

Restart prerusi rozehranou hru (stav hry je v pameti); ucty, sady otazek a historie zustanou.
Vysledky pisemek se ukladaji prubezne do databaze: pisemku prerusenou restartem server pri dalsim
startu ohodnoti podle ulozenych odpovedi a najdete ji v portalu v casti Ulozene pisemky.

Jestli se prave hraje nebo pise, zjistite pred restartem bez jmen zaku (jen heslo portalu):

```bash
B=https://classrally.example.com
read -r -s -p "Heslo portalu: " PW; echo      # heslo nejde na prikazovou radku
T=$(printf '%s' "$PW" | python3 -c 'import json,sys; print(json.dumps({"password": sys.stdin.read()}))' \
    | curl -s -H 'Content-Type: application/json' --data-binary @- $B/api/admin/login \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $T" $B/api/admin/activity
# "busy": true = bezi hra nebo pisemka, nekdo je pripojeny, nebo byla mistnost pouzita v poslednich 15 min
```

### Zalohy

Zalohy dela [`backup.py`](../backup.py) (jen standardni knihovna Pythonu). Jedna zaloha je
jeden `.tar.gz`, vedle nej lezi `.sha256`. Obsahuje:

- databazi, zkopirovanou pres SQLite online backup (konzistentni i za behu serveru);
- historii her;
- vsechny sady otazek, spolecne i vlastni sady ucitelu;
- nahrane obrazky;
- `MANIFEST.json` s kontrolnimi soucty souboru a s pocty radku v tabulkach.

Vynecha odchozi postu (jednorazove odkazy) a hudbu v `audio/`, ktera je soucasti instalace
(`--with-audio` ji prida). Rotace drzi nejnovejsi zalohu z kazdeho z poslednich **7 dni**
(`daily/`) a prvni zalohu kazdeho z poslednich **12 mesicu** (`monthly/`).

Spoustejte ji **na serveru**, ne z notebooku, a pod uctem, kteremu patri databaze
(`classrally`), ne jako root. Cil dejte idealne na jiny disk nez data. Systemd jednotky v
[`deploy/systemd/`](../deploy/systemd) jsou pripravene pro varianty A a C. Zaloha bezi
kazdou noc v 01:40 UTC (UTC kvuli letnimu casu).

```bash
sudo install -d -m 700 -o classrally -g classrally /var/backups/classrally
sudo cp /opt/classrally/deploy/systemd/classrally-backup*.service \
        /opt/classrally/deploy/systemd/classrally-backup*.timer /etc/systemd/system/
```

Jen varianta B (data v `/srv/classrally`):

```bash
sudo sed -i -e 's|--app-dir /opt/classrally|--data-root /srv/classrally|' \
            -e 's|/opt/classrally/data|/srv/classrally/data|' /etc/systemd/system/classrally-backup.service
```

Pak ve vsech variantach:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now classrally-backup.timer classrally-backup-check.timer
sudo systemctl start classrally-backup.service                  # prvni zaloha hned
sudo -u classrally python3 /opt/classrally/backup.py list --dest /var/backups/classrally
```

Kontrola bezi kazdou hodinu (`classrally-backup-check.timer`). Skonci chybou, kdyz:

- je posledni zaloha starsi nez 26 h, nebo ma cas v budoucnosti (spatne hodiny serveru);
- nesedi jeji kontrolni soucet nebo databaze;
- chybi mesicni zaloha;
- posledni beh selhal.

Rucne:

```bash
sudo -u classrally python3 /opt/classrally/backup.py check --dest /var/backups/classrally
sudo -u classrally python3 /opt/classrally/backup.py check --dest /var/backups/classrally --all   # vsechny zalohy
```

O chybe se nekdo musi dozvedet. V Uptime Kuma zalozte monitor typu **Push** s intervalem
2 hodiny. Jeho URL ulozte do `/etc/classrally/backup.env` (vlastnik root, prava 600) jako
`CLASSRALLY_BACKUP_PUSH_URL=https://…/api/push/<token>`. Klidne i tak, jak ji Kuma zkopiruje,
tedy s `?status=up&msg=OK&ping=`: skript tyto parametry nahradi svymi. Kontrola pak kazdou
hodinu hlasi `up` nebo `down` a Kuma zaalarmuje i ve chvili, kdy kontrola sama prestane bezet.

Soubor, ktery do zalohy nejde dat (zpetne lomitko nebo ridici znak ve jmenu, neplatne UTF-8,
neprecitelna slozka), zaloha vynecha a kontrola hlasi chybu, dokud ho neprejmenujete nebo
neopravite prava. Ostatni data se zalohuji dal. Zalohu s casem v budoucnosti (spatne hodiny)
po oprave hodin smazte i s jejim `.sha256`.

**Obnova** jde vzdy do nove, prazdne slozky, existujici data nikdy neprepise. Kdyz se zapis
nepovede (napr. plny disk), co uz zapsala, zase smaze:

```bash
sudo install -d -m 700 -o classrally -g classrally /srv/classrally-obnova
sudo -u classrally python3 /opt/classrally/backup.py restore \
  /var/backups/classrally/daily/classrally-<datum>Z.tar.gz --into /srv/classrally-obnova
# varianty A a C: pridejte --layout app-dir (obrazky pujdou do static/uploads)
```

Pak:

1. Zastavte server.
2. Soucasne slozky `data/`, `history/`, `questions/` a `uploads/` (A a C: `static/uploads/`)
   odsunte stranou, neprepisujte je.
3. Nahradte je obnovenymi slozkami.
4. Server spustte.

Obnovu obcas vyzkousejte nanecisto:

- **Docker:** obnovte zalohu do docasne slozky (`--layout data-root`). Spustte druhy kontejner
  z tehoz image, s obnovenymi slozkami pripojenymi misto ostrych (jako v
  `deploy/docker-compose.server.yml`), s `--network none`, `MAIL_MODE=off` a jinym jmenem.
  Kontejner nema sit, takze prihlaseni ucitele overte uvnitr nej (`docker exec`, Python a
  `/api/auth/login`).
- **Bez Dockeru:** zkopirujte aplikaci do docasne slozky a jeji `data/`, `history/`,
  `questions/` a `static/uploads/` nahradte obnovenymi (`--layout app-dir`). Spustte ji na jinem
  portu s `MAIL_MODE=off`.

Casy v nazvech zaloh jsou v UTC (pismeno `Z`).

**Kopie mimo server:** slozku `/var/backups/classrally` stahujte z jineho, stale zapnuteho
stroje (pull, napr. rsync pres SSH s uctem jen pro cteni), aby se k zalohum napadeny server
nedostal. I tam hlidejte stari posledni kopie.

### Monitoring

- HTTP kontrola `https://…/api/health` (ocekavano 200 a `"ok": true`), napr. Uptime Kuma.
- Stari a citelnost posledni zalohy: `classrally-backup-check` s push monitorem (viz Zalohy).
- Platnost certifikatu: certbot timer, resp. Caddy, ho obnovuji samy. Kuma umi hlidat i expiraci.

## 4. Kontrolni seznam po nasazeni

- [ ] `QUIZ_ADMIN_PASSWORD` nastaveno, `/api/admin/auth-status` vraci `auth_required: true`
- [ ] ucitelsky ucet zalozen pred zverejnenim, `/api/setup-status` vraci `setup_needed: false`
- [ ] port aplikace neni dostupny primo z internetu, jen pres proxy
- [ ] HTTPS funguje, HTTP presmerovava na HTTPS se zachovanim cesty
- [ ] WebSocket `wss://…/api/ws` se pripoji (101)
- [ ] proxy prepisuje `X-Forwarded-For`
- [ ] konfigurace s hesly je mimo git s pravy 600/640
- [ ] `QUIZ_ROOMS=multi`, pokud server pouziva vic ucitelu
- [ ] `MAIL_MODE` odpovida zameru (`log` dokud neni SMTP overene), `QUIZ_BASE_URL` nastavene
- [ ] obnova hesla vyzkousena (e-mail potvrzen, odkaz z `/reset` funguje)
- [ ] zalohy bezi a hlida se jejich stari, obnova vyzkousena
- [ ] monitoring hlida `/api/health`
- [ ] odehrana cela hra: ucitel na `/host`, zak z mobilu pres QR

## Pro vyvojare

- Po zmene souboru ve `static/` zvyste `CACHE_NAME` v `static/sw.js`, jinak vracejici se
  uzivatele uvidi starou verzi (PWA cache).
- Lokalni zkouska serverove varianty: `deploy/docker-compose.server.yml` s
  `CLASSRALLY_BIND_ADDR=127.0.0.1` a docasnou datovou slozkou.
