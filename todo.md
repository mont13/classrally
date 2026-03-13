# ClassRally - Roadmap k plnohodnotné platformě

## Analýza: Co Kahoot dělá dobře a co nám chybí

### Co uživatelé na Kahootu nejvíc chválí
1. **Nulová bariéra vstupu** — PIN + přezdívka, žádný účet, žádná instalace
2. **Ikonická hudba a zvuky** — countdown hudba je kulturní mém, vytváří Pavlovovský engagement
3. **Live společný zážitek** — všichni ve stejné místnosti, projektor, emoce
4. **Okamžitá zpětná vazba** — hned vidím jestli správně/špatně + body
5. **Leaderboard po každé otázce** — sociální srovnání, motivace
6. **Podium na konci** — veřejné uznání top 3
7. **100M+ komunitních kvízů** — učitel nemusí nic vytvářet

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
- ✅ 77 testů

---

## Co nám chybí — prioritizováno

### 🔴 FÁZE 1: Základní vylepšení (musí být)

#### 1.1 Registrace uživatelů (volitelná)
- [ ] Volitelná registrace (jméno + heslo nebo jen jméno + kód třídy)
- [ ] Hra funguje i BEZ registrace (jako teď)
- [ ] Registrovaný hráč vidí svoji historii her a skóre
- [ ] Jednoduchý profil (přezdívka, celkové skóre, počet her)
- [ ] SQLite databáze pro uživatele a historii

#### 1.2 Skupiny / Třídy
- [ ] Učitel vytvoří třídu (název + kód pro připojení)
- [ ] Studenti se připojí do třídy kódem
- [ ] Historie her per třída
- [ ] Učitel vidí progress studentů v čase

#### 1.3 Více typů otázek
- [ ] True/False (dedikovaný typ, 2 tlačítka)
- [ ] Multi-select (vyber všechny správné)
- [ ] Seřazení (drag & drop pořadí)
- [ ] Otevřená odpověď (krátký text, validace)

#### 1.4 Streaky a combo
- [ ] Streak bonus za po sobě jdoucí správné odpovědi
- [ ] Vizuální streak indikátor na player obrazovce
- [ ] Streak shoutout na host obrazovce

### 🟡 FÁZE 2: Engagement a UX (důležité)

#### 2.1 Herní módy
- [ ] Klasický (současný — live, teacher-paced)
- [ ] Týmový mód (virtuální týmy, team score)
- [ ] Self-paced / Domácí úkol (student si projde sám, bez časového tlaku)

#### 2.2 Player experience
- [ ] Avatary / profilové obrázky (výběr z předdefinovaných)
- [ ] Animované podium (confetti, zvuky)
- [ ] Power-ups (ochrana streaku, double points) — volitelné, host zapíná

#### 2.3 Host experience
- [ ] WebSocket místo pollingu (realtime bez zpoždění)
- [ ] Náhodné pořadí otázek (shuffle)
- [ ] Přeskočit otázku
- [ ] Pozastavit hru
- [ ] Fullscreen prezentační mód

#### 2.4 Admin vylepšení
- [ ] Import/export otázek (CSV, JSON, Quizlet formát)
- [ ] Drag & drop řazení otázek
- [ ] Obrázky v otázkách (upload nebo URL)
- [ ] Sdílení sad otázek (export jako link/soubor)
- [ ] Export výsledků (CSV/PDF)

### 🟢 FÁZE 3: Pokročilé funkce (nice to have)

#### 3.1 Ankety a hlasování
- [ ] Poll mód (žádná správná odpověď, zobrazí výsledky)
- [ ] Word cloud z odpovědí
- [ ] Slide/info karta mezi otázkami

#### 3.2 Gamifikace meta-game
- [ ] XP systém přes více her
- [ ] Achievementy / odznaky
- [ ] Třídy jako "ostrovy" s vizuálním progressem (inspirace Kahootopia)
- [ ] Ligy mezi třídami

#### 3.3 Moderace a bezpečnost
- [ ] Filtr nevhodných přezdívek
- [ ] Generátor náhodných přezdívek (volitelné)
- [ ] Anti-bot ochrana
- [ ] Kick hráče

#### 3.4 Technické
- [ ] i18n (přepínání jazyků v UI — CZ/EN/SK/DE)
- [ ] PWA (offline přístup, instalace na mobil)
- [ ] HTTPS podpora
- [ ] REST API verzování

---

## Otázka technologie

### Současný stack: Python stdlib (http.server)
**Výhody:** zero dependencies, jednoduché, funguje všude
**Nevýhody:** polling místo WebSocket, žádná DB, škálování omezené

### Možné směry:

| Varianta | Stack | Pro | Proti |
|----------|-------|-----|-------|
| **A) Zůstat u Pythonu** | FastAPI + SQLite + websockets | Známý jazyk, rychlý přechod | Python WS výkon omezený |
| **B) Node.js** | Express/Fastify + Socket.io + SQLite | Nejlepší realtime výkon, industry standard pro kvízy | Nový jazyk |
| **C) Go** | Gorilla WS + SQLite | Výkon, single binary | Méně knihoven pro web |
| **D) SvelteKit fullstack** | Svelte + Node backend | Moderní, SSR, WS nativně | Složitější |

**Doporučení:** Rozhodnutí až PO fázi 1. Pokud stačí SQLite + WebSocket, **FastAPI + python-websockets** je nejmenší skok. Pokud chceme škálovat na stovky hráčů, **Node.js + Socket.io** je ověřená volba.

---

## Srovnání s konkurencí

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
| Export výsledků | ❌ | 💰 | 💰 | ✅ |
| Hudba/zvuky | ✅ | ✅ | ✅ | ✅ |

*Quizizz (Wayground) vyžaduje účet pro async mód

---

## Naše konkurenční výhody (zachovat!)
1. **Lokální běh bez internetu** — žádná konkurence to nenabízí
2. **Neomezený free tier** — Kahoot omezuje na 10 hráčů
3. **Open source, self-hosted** — data zůstávají ve škole
4. **Zero dependencies** — žádný npm install, žádné build nástroje
5. **AI generátor zdarma** — Kahoot ho má jen v placeném plánu
