# Pomocník v Průvodci

Stav: **čtvrtek 12. 11. 2026, 08:43 SEČ**. Implementace pro lokální kontrolu;
bez nasazení, commitu nebo pushnutí. Skutečná školní AI nebyla oslovena.

## Cesta učitele

1. Přihlášený učitel otevře **Průvodce** a **Zeptat se pomocníka**.
2. Napíše otázku do 500 znaků. Prohlížeč odešle jeden požadavek
   `POST /api/teacher/help`, s existujícím `UserToken` a jazykem rozhraní.
3. Server ověří aktuální roli učitele; anonym, žák i samotný administrátorský
   Bearer token jsou odmítnuti. Limit je 6 požadavků za 60 sekund na účet,
   společný pro jeho relace. Neplatné dotazy v přijatelně velkém těle limit spotřebují.
4. Server vybere nejvýše tři relevantní kapitoly. Do AI předá pouze jejich
   text s názvy ovládání z existujících překladů a nalezené výrazy **z pevného
   slovníku**, nikoli původní otázku, historii konverzace, žákovská data,
   obsah stránky, účty, známky, PIN nebo token uživatele.
5. Použije aktuální společné `AI_CONFIG` z generátoru: `base_url`, `model`,
   `api_key`; stejný OpenAI kompatibilní `/v1/chat/completions`, odstranění
   případného koncového `/v1` a serverovou Bearer autentizaci. Žádné přepsání
   adresy, klíče nebo modelu z dotazu není možné. Timeout je 30 sekund,
   bez opakování; rozpočet odpovědi 2048 tokenů, maximálně 4000 znaků.
6. Odpověď se zobrazí jako prostý text. Pomocník nemá nástroje ani oprávnění
   cokoli provést. Rozepsaný formulář na stránce zůstane zachován. Dotaz ani
   odpověď se neukládají do browser storage, databáze nebo aplikačních logů.
   Aplikační logger pomocníka zaznamenává pouze počet a délku dotazu.

## Ověřená nápověda

Veřejný datový soubor `help.json` má 13 kapitol: Přehled, Sady otázek,
Písemka, Editor otázek, AI generátor (v UI **AI návrh**), Nastavení, Historie,
Třídy, Domácí úkol, Uživatelé, hra na projektoru, PIN/QR a školní portál.
U každé kapitoly jsou zdroj, cílový selector a vyhledávací výrazy. Fakta
vycházejí z admin/host šablon, jejich obsluhy, serverových oprávnění a portálového
přihlášení. Překladové reference se řeší až při sestavení promptu; pevné české
názvy ovládání písemky se zachovávají i v anglické odpovědi, stejně jako v UI.
Kapitola portálu se vybírá pouze při aktivním `portal_enabled()`.

Při změně ovládání je nutné současně upravit datovou kapitolu a případný
odpovídající postup v Průvodci. Testy kontrolují existenci zdrojových selectorů,
klíčů v pěti jazycích a výběr kapitol, nikoli pravdivost každé věty automaticky.

## Výpadky a hranice

Při chybějící konfiguraci, timeoutu, vadné/prázdné odpovědi či síťové chybě
se zobrazí přeložená zpráva a stále dostupné tlačítko **Katalog Průvodce**.
Chyby poskytovatele, jeho adresy ani odpovědi se nevracejí klientovi a nelogují.
Neznámé téma nevolá AI: doporučí konkrétnější dotaz, katalog nebo Buggy,
je-li zapnuté. Systémový prompt zakazuje domýšlení a instruuje AI přiznat
chybějící podklad. Odpověď AI není zárukou faktické správnosti.

Ochrana soukromí záměrně omezuje porozumění: různé otázky se stejnými
rozpoznanými tématy dostanou stejný typ kontextu. Složitou podmínku nebo
zápor nelze spolehlivě přenést do AI; u nejasností má požádat o upřesnění.
Odpovědi se zobrazují jednorázově, bez návazné konverzační paměti.
Po uzavření panelu se klientské čekání zruší; již běžící serverový dotaz může
doběhnout, ale žádná data ani nastavení nemění.

## Nastavení školní instance a přejímka

Správce nastaví **stejnou školní AI jako pro generátor**: adresu API, model
Gemma podle skutečně dostupného identifikátoru a případný klíč. U uzamčené
nebo sdílené instalace patří adresa a klíč do existující serverové konfigurace
`AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY`; u lokální instalace lze použít
stávající **Nastavení → Uložit AI config**. Není potřeba nový účet, služba ani
nová databázová migrace. Konkrétní školní adresy nebo přihlašovací údaje do
veřejného repozitáře nepatří.

Docker i Windows recept zahrnuje nový Python modul. Nové statické soubory
se distribuují existujícím mechanismem; verze service worker cache je zvýšena.
Balíček ani Docker image se v rámci této změny nesestavovaly.

Před přijetím na škole zůstává ověřit skutečnou dostupnost, latenci a kvalitu
krátkých odpovědí Gemmy včetně případné spotřeby tokenů na interní uvažování,
anglické odpovědi a skutečně zapnutého portálu. Testy používají výhradně falešnou
AI a lokální izolované účty. Nejde o ověření produkčního provozu.

## Lokální testovací příkazy

Spouštějí se z kořene repozitáře, bez kontaktování skutečné AI:

```bash
omez-pamet -- .venv/bin/python -m pytest -q test_server.py test_help_assistant.py
omez-pamet -- python3 test_help_browser.py
omez-pamet -- python3 test_tour_browser.py
```

Python pro poslední dva příkazy musí mít již dostupný Playwright a Chromium;
testy nic nestahují. Účty, databáze a historie jsou izolované v dočasných adresářích.
Důkazy z běhu jsou v ignorovaném lokálním scratchpadu, ne ve veřejném Gitu.

## Dvě ukázky z falešné AI

**Jak spustím písemku?**

1. Otevřete „Písemka“ a vyberte sadu.
2. Nastavte „Časový limit (minuty)“.
3. Klikněte „Připravit písemku“, potom „Spustit písemku“.

**Kde nastavím časový limit?**

1. Pro kvíz otevřete „Nastavení“, pole „Čas na otázku (sekundy)“, potom „Uložit nastavení“.
2. Pro písemku použijte „Písemka“ → „Časový limit (minuty)“ před přípravou písemky.

Jde o předem stanovené testovací odpovědi, nikoli o výstup skutečného modelu.

## Výsledky ověření

**čtvrtek 12. 11. 2026, 08:43 SEČ** — všechny uvedené testy přes `omez-pamet`:

- Celá serverová sada včetně pomocníka: **487 testů + 736 dílčích kontrol PASS**, 61,48 s.
  Běh před závěrečným zpřesněním výběru kapitoly vypnutého portálu.
- Finální sada pomocníka po této úpravě: **15 testů + 11 dílčích kontrol PASS**, 1,44 s.
  Zahrnuje také změnu role v již přihlášené relaci a živé zapnutí/vypnutí portálu.
- Finální Playwright pomocníka: desktop CS, mobil EN, **PASS**; 7 lokálních dotazů,
  4 falešná AI volání, 0 jiných měnících požadavků, 0 vnějších požadavků,
  0 JavaScriptových chyb. Úspěch, bezpečné zobrazení HTML jako textu, výpadek,
  chybějící konfigurace, limit, neznámé téma, role, klávesnice a zachování formuláře.
- Původní Playwright Průvodce: **35 katalogových průchodů PASS**, 0 vnějších
  požadavků a 0 JavaScriptových chyb; včetně hry, písemky, PIN, více učitelů,
  nefunkčního úložiště, jazyka a domácího úkolu.
- Kontrola syntaxe Pythonu při testech, JavaScriptu včetně ES modulu,
  shellového balicího receptu a `git diff --check`: PASS.
- Přidané řádky a nové soubory neobsahují privátní IP adresy, privátní klíče
  ani běžné formáty ostrých API tokenů; testovací identifikátory jsou falešné.

Výchozí revize: `dec0d95`, větev `main`; všechny změny jsou ponechané pouze
v pracovním stromu. Není spuštěn žádný testovací běh na pozadí. Claude má
zkontrolovat zejména věcnou nápovědu a vhodnost slovníkového převodu dotazu
před skutečným nasazením a přejímkou proti školní Gemmě.
