# Ověření ClassRally na Windows — checklist

Tento dokument je návod pro **otestování ClassRally na reálném Windows počítači**
(vývoj a testy běžely na Linuxu; samotný `.bat` + přibalený Python je potřeba
ověřit přímo na Windows). Projdi kroky a u každého zaškrtni výsledek; dole je
místo na poznámky a co nahlásit zpět.

> Verze balíku v tomto adresáři: `docs/overeni/ClassRally-Windows.zip`
> (sestaveno z aktuálního kódu, port **48217**, obsahuje režim Hra i Písemka).

---

## Co budeš potřebovat
- Windows počítač (notebook učitele) — ideálně bez nainstalovaného Pythonu,
  ať se ověří přibalený přenosný Python.
- 1–2 mobily (nebo druhý počítač) jako „žák“.
- Wi-Fi, na kterou připojíš notebook i mobil (stačí hotspot z telefonu).

---

## Krok 0 — Získání balíku
1. Na Windows stroji stáhni repozitář (nebo jen tento ZIP):
   - přes git: `git pull` a vezmi `docs/overeni/ClassRally-Windows.zip`, **nebo**
   - stáhni `ClassRally-Windows.zip` přímo z GitHubu (tlačítko *Download raw*).
2. Rozbal ZIP — vznikne složka `ClassRally`.

- [ ] ZIP rozbalen, vidím soubor `Spustit-ClassRally.bat`

---

## Krok 1 — Spuštění serveru
1. Dvojklik na `Spustit-ClassRally.bat`.
2. Při PRVNÍM spuštění Windows ukáže dotaz firewallu → **Povolit přístup**
   (klidně zaškrtni i veřejné sítě, jinak se mobil nepřipojí).
3. Mělo by se objevit černé okno a samo otevřít prohlížeč na
   `http://127.0.0.1:48217/admin`.

- [ ] Černé okno běží (drží server), nespadlo
- [ ] Prohlížeč se otevřel na učitelském portálu `/admin`
- [ ] (Když Python není v systému) přibalený `python\python.exe` se použil
      a server přesto naběhl

**Když to nejde:** viz „Řešení potíží“ dole.

---

## Krok 2 — Rychlý test režimu Hra (kontrola základu)
1. V portálu záložka **Sady otazek** → u libovolné sady klikni **Aktivovat**.
2. Otevři **Host obrazovka**, na mobilu naskenuj QR (nebo zadej URL pro žáky).
3. Přihlas „žáka“, na hostu klikni **Start**, odpověz na mobilu.

- [ ] Žák se přes QR/URL připojil z mobilu
- [ ] Otázky běží, odpovědi se počítají, na konci je žebříček/podium

---

## Krok 3 — HLAVNÍ test: režim Písemka
1. V portálu záložka **Pisemka (test)**.
2. Vyber sadu, nastav **časový limit** (klidně 5 min na test), nech zaškrtnuté
   míchání i zákaz kopírování, případně nastav **auto-odevzdat po N opuštění okna**
   (např. 3 pro test funkce).
3. Klikni **Pripravit pisemku**.
4. Na mobilu otevři URL pro žáky — měl(a) by ses automaticky dostat na **písemku**
   (`/exam`), ne na hru.
5. Přihlas žáka (klidně 2 žáky na 2 mobilech).
6. V portálu klikni **Spustit pisemku** → žákům naběhne test a odpočet.

- [ ] Mobil žáka se sám přepnul na písemku (`/exam`), ne na hru
- [ ] Po „Spustit“ se žákovi zobrazily otázky a běží odpočet (MM:SS)
- [ ] Navigátor otázek funguje (skok mezi otázkami, zelené = zodpovězené)

### 3a — Dohled (anti-cheat) — TOHLE je klíčové ověřit
Na mobilu žáka **během testu přepni do jiné aplikace** (nebo na jinou záložku)
a zase zpět.

- [ ] Při opuštění se žákovi ukázal červený VAROVNÝ overlay přes celou obrazovku
- [ ] V portálu **Zivy prehled** se u žáka zvýšil počet „Opuštění okna“
      a řádek zoranžověl
- [ ] (Pokud nastaveno auto-odevzdání) po N opuštěních se test sám odevzdal

### 3b — Zákaz kopírování + míchání
- [ ] Na mobilu nejde označit/kopírovat text otázky (zákaz kopírování)
- [ ] Dva žáci mají **různé pořadí** otázek i odpovědí (proti opisování)

### 3c — Odevzdání a známka
1. Na mobilu žáka klikni **Odevzdat písemku** → potvrď.

- [ ] Žák uvidí výslednou **známku (1–5)** a „Správně X z N (%)“
- [ ] V portálu **Zivy prehled** je u žáka stav „Odevzdano“ a známka
- [ ] Tlačítko **Stahnout vysledky (CSV)** stáhne soubor, který se v Excelu
      otevře správně (diakritika OK, sloupce: Jméno, Známka, Opuštění okna…)

---

## Krok 4 — Připojení žáků přes síť (Wi-Fi)
Zkus reálné připojení podle `WIFI-NAVOD.txt` v balíku:

- [ ] Notebook i mobil na stejné síti (ideálně hotspot z telefonu)
- [ ] Mobil načte přihlašovací stránku z QR / URL
- [ ] (Pokud se NEnačte na školní Wi-Fi) ověř, že to jde přes hotspot z telefonu
      → to potvrdí „oddělení klientů“ na školní síti

---

## Řešení potíží
- **„Nenašel jsem Python“** → použij ZIP z tohoto adresáře (má Python přibalený),
  ne jen samotné `.bat`.
- **Žák se nepřipojí, ale stránka /admin na notebooku jede** → firewall nebyl
  povolen, nebo školní Wi-Fi odděluje klienty → hotspot z telefonu.
- **Port obsazený** → spusť přes `set QUIZ_PORT=49000` a pak `Spustit-ClassRally.bat`.
- **Antivirus blokuje** → balík neobsahuje `.exe`, jen `.bat` + Python; povolit složku.

---

## Výsledek ověření (vyplň a nahlas zpět)
- Windows verze: ______________________________
- Měl počítač předinstalovaný Python? ANO / NE
- Co fungovalo: ________________________________
- Co NEfungovalo / chyby (opiš text z černého okna): 
  ______________________________________________
  ______________________________________________
- Připojení žáků: školní Wi-Fi / hotspot telefonu / jiné: __________
- Celkový dojem (1–5) a poznámky: _____________
