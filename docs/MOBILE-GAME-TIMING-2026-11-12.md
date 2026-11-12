**čtvrtek 12. 11. 2026 09:11:59 SEČ**

# ClassRally: čas a seřazování na mobilu — předání Claudovi

Dokončeno lokálně na větvi `main`, výchozí revize `ff10a1d31160c4f2f6d27e64f495b7e1f542d784`. Bez commitu, pushe a nasazení. Písemka, účty a pomocník nemají implementační změny. Všechny testovací účty, sady a historie jsou syntetické a izolované od skutečných dat; nebyla volána externí AI ani e-INFRA.

## Chování a rozhodnutí

- Adaptivní čas = základ hry + původní příplatek za čtení + nový příplatek za ovládání. Seřazení: `5 × max(0, počet položek − 2)` sekund; více správných: `2 × počet voleb` sekund. Ovládání má samostatný strop +30 s, stejně jako čtení. Vypnutý adaptivní čas nepřidává žádný příplatek.
- Volitelné `time_sec` je přesný limit dané otázky, celé číslo 5–180 s, a má přednost před základem i oběma příplatky. Chybějící pole zachová standardní herní nastavení; `null`, booleany, text, desetinná čísla a hodnoty mimo rozsah server odmítá. Prázdný editor pole vynechá. Pole se ukládá v sadě a zachovává při JSON/CSV exportu a importu. Staré CSV bez sloupce funguje.
- Server počítá společný termín konce; hráč a projektor již používali tento termín a `question_time_sec`, proto jejich časovací kód nepotřeboval samostatný výpočet. Historie `duration_sec` dál znamená skutečně odehranou dobu. Při vypršení odpovídá novému limitu; po odpovědi všech nebo zásahu učitele může být kratší. Přidané `question_time_sec` v historii uchovává plánovaný limit.
- Přípravný odpočet před zobrazením možností je zvlášť. Čtení vysvětlení po otázce se nemění.
- Původní ovládání seřazování byly šipky nahoru/dolů. Nyní se na mobilu i počítači klepá na celé položky v požadovaném pořadí. Jejich poloha zůstává stabilní, zobrazují se čísla 1…n. Zpět odebere poslední krok, Vymazat všechny. Potvrzení vyžaduje všechny položky. Cíle mají nejméně 64 px na výšku; dlouhá slova se zalamují. Polling, změna jazyka a chyba odeslání uchovávají rozpracované pořadí. Nová otázka/hra začíná prázdným výběrem.
- Zachované bodování více správných: přesná shoda = plné body, neprázdná podmnožina správných = `600 × vybrané správné / všechny správné`, chybná volba = nula. Správně nezaškrtnuté volby nově nebodujeme: změnilo by to staré sady a odměňovalo vynechání. Nový režim bodování sady není zaveden. `EXAM_QUESTION_TYPES` zůstává beze změn (`choice`, `truefalse`) a časování písemky nepoužívá `time_sec`.
- Doplněné texty jsou v pěti stávajících jazycích. Upozornění na delší řazení nově doporučuje adaptivní/vlastní čas. Cache statických překladů má verzi v26.

## Příklady při nastavení 20 s

Předpoklad: zapnutý adaptivní čas, krátký text do 80 znaků včetně položek, bez `time_sec`.

| Otázka | Výpočet | Čas na odpověď |
|---|---|---:|
| Seřazení 4 položek | 20 + 5 × (4 − 2) | 30 s |
| Seřazení 5 položek | 20 + 5 × (5 − 2) | 35 s |
| Více správných, 4 volby | 20 + 3 × 4 | 32 s |

Delší text přidá původní čtecí bonus. Například `time_sec: 47` dá přesně 47 s i s adaptivním časem. Přípravné 3 s běží před tímto limitem, pokud je učitel nevypne.

## Ověření

- Přes `omez-pamet` prošel celý modul `test_server`: **478 testů, 54,719 s, exit 0**. Nové testy ověřují příplatky, strop, vypnutí adaptivity, kombinaci se čtením, přednost 5/47/180 s, termíny konce, brzké ukončení, trvale uloženou historii, validaci a zpětnou kompatibilitu CSV. Stávající testy bodování i písemky prošly.
- Přes `omez-pamet` a v závěrečném běhu také s vnějším limitem 90 s prošel nový lokální Chromium test: **5 scénářů, exit 0**. Editor save/reload a JSON/CSV import/export; dotyk 320 a 390 px; klávesnice na 1200 px; Zpět/Vymazat; zachování draftu při pollingu a HTTP 503; správné mapování promíchaných položek serverem; výsledek, další otázka a 8 položek; žádné horizontální přetékání; odpočty hráče i projektoru; obnovení staré cache překladů.
- Nula chyb JavaScriptu v závěrečném běhu. Herní/editorové kontexty blokovaly HTTP mimo místní server. Service worker se testoval samostatně na místní statické stránce v jeho registrovaném rozsahu.
- Úvodní běhy selhaly očekávaně před implementací. Dále byly opraveny chyby testovacího prostředí (potvrzení importního dialogu, vazba výchozí místnosti, inicializace storage na prázdné stránce a čekání na service worker mimo rozsah). Zaseklý doplňkový běh byl přerušen pouze přes jeho ověřené PID; závěrečný běh má časové limity a prošel.
- `git diff --check` prošel. Kontrolní diff chráněných souborů pomocníka, písemky a účtů je prázdný. Existující rozsáhlý test Průvodce nebyl znovu spouštěn; nový browser test používá stejný způsob lokální izolace.

Důkazy a SHA-256 zdrojů: [/home/miro/Documents/git/classrally/.cache/mobile-game-verification.json](/home/miro/Documents/git/classrally/.cache/mobile-game-verification.json). Serverový log: [/home/miro/Documents/git/classrally/.cache/mobile-server-tests.log](/home/miro/Documents/git/classrally/.cache/mobile-server-tests.log). Browserový log obsahuje na prvním řádku závěrečný PASS; dále zůstala stopa dříve přerušeného běhu kvůli souběžnému uzavírání jeho logu: [/home/miro/Documents/git/classrally/.cache/mobile-game-browser.log](/home/miro/Documents/git/classrally/.cache/mobile-game-browser.log).

Snímky syntetické hry: [/home/miro/Documents/git/classrally/.cache/mobile-game-qa/ordering-320.png](/home/miro/Documents/git/classrally/.cache/mobile-game-qa/ordering-320.png), [/home/miro/Documents/git/classrally/.cache/mobile-game-qa/ordering-390.png](/home/miro/Documents/git/classrally/.cache/mobile-game-qa/ordering-390.png), [/home/miro/Documents/git/classrally/.cache/mobile-game-qa/ordering-1200.png](/home/miro/Documents/git/classrally/.cache/mobile-game-qa/ordering-1200.png).

## Změněné soubory

- [/home/miro/Documents/git/classrally/README.md](/home/miro/Documents/git/classrally/README.md)
- [/home/miro/Documents/git/classrally/server.py](/home/miro/Documents/git/classrally/server.py)
- [/home/miro/Documents/git/classrally/static/admin.html](/home/miro/Documents/git/classrally/static/admin.html)
- [/home/miro/Documents/git/classrally/static/play.html](/home/miro/Documents/git/classrally/static/play.html)
- [/home/miro/Documents/git/classrally/static/sw.js](/home/miro/Documents/git/classrally/static/sw.js)
- [/home/miro/Documents/git/classrally/static/i18n/cs.json](/home/miro/Documents/git/classrally/static/i18n/cs.json)
- [/home/miro/Documents/git/classrally/static/i18n/sk.json](/home/miro/Documents/git/classrally/static/i18n/sk.json)
- [/home/miro/Documents/git/classrally/static/i18n/en.json](/home/miro/Documents/git/classrally/static/i18n/en.json)
- [/home/miro/Documents/git/classrally/static/i18n/de.json](/home/miro/Documents/git/classrally/static/i18n/de.json)
- [/home/miro/Documents/git/classrally/static/i18n/uk.json](/home/miro/Documents/git/classrally/static/i18n/uk.json)
- [/home/miro/Documents/git/classrally/test_server.py](/home/miro/Documents/git/classrally/test_server.py)
- [/home/miro/Documents/git/classrally/test_mobile_game_browser.py](/home/miro/Documents/git/classrally/test_mobile_game_browser.py)
- [/home/miro/Documents/git/classrally/docs/MOBILE-GAME-TIMING-2026-11-12.md](/home/miro/Documents/git/classrally/docs/MOBILE-GAME-TIMING-2026-11-12.md) — toto předání.

## Co zůstává nejisté

Číselné příplatky jsou návrh podle dodaného pozorování ze třídy, nikoli výsledek nového měření žáků. Chromium emuluje dotyk; fyzický Android/iPhone, pomalá školní Wi-Fi a skutečné dokončení v další třídě nejsou ověřeny. Dlouhé texty mohou vyžadovat svislé posouvání. Příplatek za ovládání nezaručuje porozumění otázce ani správnost odpovědi. Claude zvýšil příplatek na 3 s za volbu (4 volby = 32 s, medián ze hry 26,7 s); vhodnost je třeba ověřit v praxi; učitel může konkrétní otázce nastavit vyšší přesný limit. Čeká kontrola Claudem; nic není nasazeno. Žádný testovací server ani výpočet nezůstává spuštěný.
