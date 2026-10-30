# ClassRally — Stav a plán dalších vylepšení

## Stav k 30. 10. 2026

Sjednoceno s `todo.md`: XP a odznaky jsou dílčí; body a herní ocenění existují, levely a trvalé odznaky chybí. Sdílené funkce mají v obou seznamech stejný rozsah a stav.

## HOTOVO (17.3.2026)

### 1. Univerzální AI provider
- `OLLAMA_CONFIG` přepsán na `AI_CONFIG` (`base_url`, `api_key`, `model`)
- Endpoint přepnut z `/api/generate` na `/v1/chat/completions` (OpenAI-kompatibilní)
- Funguje s: **Ollama, OpenAI, Groq, Together AI, LM Studio** a jakýmkoli OpenAI-kompatibilním API
- `Authorization: Bearer` header se posílá při zadaném API klíči
- Zpětná kompatibilita se starými env vars (`OLLAMA_HOST`, `OLLAMA_PORT`)
- UI: Host+Port nahrazeny jedním polem "AI Server URL" + polem "API klíč"

### 2. Dropdown výběr sady v AI generátoru
- `#aiSaveAs` přepsán z textového inputu na `<select>` dropdown
- Zobrazuje existující sady + možnost "Vytvořit novou sadu"
- Při výběru "Vytvořit novou" se objeví textový input pro název

### 3. Synth fallback zvuky odstraněny
- Odstraněny funkce: `startSynthLoop()`, `playSynthStinger()`, `ensureAudioContext()`
- Odstraněn veškerý WebAudio oscillátor kód (~150 řádků)
- MP3 přehrávání zůstává plně funkční
- Odstraněny i18n klíče: `synth_fallback`, `synth_fallback_msg`, `music_synth_fallback`

### 4. i18n kompletní přepracování (454→453 klíčů × 4 jazyky)
- Všechny hardcoded stringy nahrazeny `data-i18n` atributy a `i18n.t()` voláními
- Language switcher (CS|EN|SK|DE) na všech stránkách
- `onLangChange()` callback pro dynamicky generovaný obsah
- 155 testů projde, JSON validní

---

## DALŠÍ KROKY

### Priorita 1: Testování a stabilizace
- [ ] Otestovat AI generování s lokální Ollama přes nový `/v1/chat/completions` endpoint — `test_server.py`: `TestAIGenerationParsing` ověřuje URL a odpověď s náhradou `urlopen`; skutečné volání Ollama tím není ověřené.
- [ ] Otestovat AI generování s OpenAI API klíčem — `server.py`: `generate_questions_ai()` posílá Bearer klíč, `TestAIConfigHTTP` kontroluje konfiguraci s náhradou generování; úspěšné volání OpenAI neprokazuje.
- [ ] Otestovat výběr/vytvoření sady po generování — `static/admin.html`: `updateAiBankSelect()`, `aiSaveAsNew` a uložení přes `saveBtn` už existují; prohlížečový test obou větví chybí.
- [ ] Ověřit že přepínání jazyků funguje korektně na všech stránkách — `static/i18n.js` a `test_server.py`: `TestI18nCatalogs` dokládají runtime a shodu katalogů; interaktivní kontrola všech stránek ve všech jazycích není součástí testu.
- [x] End-to-end test celé hry (lobby → otázky → podium) — místní Chromium průchod 30. 10. 2026 ověřil výběr a otevřenou odpověď na 390×844, 360×740 a 1440×900, skutečné podium i souhrn obou odpovědí (`static/play.html`: `renderPodium()`); API/scoring navíc pokrývá `TestGameAnalysis` v `test_server.py`. Ostatní typy a dlouhé odpovědi tím nejsou ověřené.

### Priorita 2: Chybějící funkce z roadmapy
- [x] Obrázky v otázkách (upload nebo URL) — `server.py`: `/api/admin/upload-image`, `image_url`; `static/admin.html`: editor a náhled; `static/play.html` a `static/host.html`: `questionImage`. Samostatný test obrázků chybí.
- [x] Avatary / profilové obrázky (výběr z předdefinovaných) — `static/profile.html`: `AVATARS`, `renderAvatarGrid()`, `selectAvatar()`; `db.py`: `update_avatar()`; `test_server.py`: `TestRegisterAvatarHTTP`.
- [x] Sdílení sad otázek (export jako odkaz/soubor) — JSON/CSV soubor přes `static/admin.html`: `btnExportJson`/`btnExportCsv`, `triggerDownload()`. Samostatný odkaz na sadu není implementovaný; požadavek splňuje soubor.
- [x] Export výsledků her do CSV pro učitele — `server.py`: `game_results_to_csv()`, `/api/admin/history/export`; `test_server.py`: `TestGameAnalysis.test_history_record_and_exports()`. PDF export tím není splněný.

### Priorita 3: Gamifikace
- [ ] XP systém přes více her (body a postup po levelech) — `db.py`: `save_game_to_db()` sčítá `users.total_score` a počet her, `static/profile.html`: `kpiScore` zobrazuje součet; postup po levelech chybí.
- [ ] Achievementy / odznaky (série, počet her a skóre) — herní ocenění včetně série už jsou (`server.py`: `QuizState._compute_awards_locked()`, `static/play.html`: `renderFinishCard()`, `test_server.py`: `TestAwards`); trvalé odznaky za počet her a kumulované skóre chybí.
- [ ] Ligy mezi třídami — `db.py` má skupiny a jejich historii; liga, její pořadí ani testy nejsou implementované.

### Priorita 4: UX vylepšení
- [ ] Lepší mobilní responsivita (play.html) — `static/play.html` a `static/style.css` mají mobilní breakpointy, velké ovládací prvky a zalamování; automatické testy neprokazují použitelnost všech typů otázek a dlouhých odpovědí na telefonu.
- [x] Animace přechodů mezi fázemi — `static/play.html`: `prepareStateEffects()`, odpočet `grPop`, vyhodnocení `flashFade` a závěrečné konfety; respektují omezení pohybu. Vizuální regresní test animací chybí.
- [ ] Zvukové efekty pro správnou/špatnou odpověď (MP3, ne synth) — katalog nahrávek a jeho test už existují (`TestAudioTracks`), ale hráčské `sfxPlay()` v `static/play.html` volá syntézu `correct()`/`wrong()` v `static/sfx.js`; požadavek na MP3 zde není splněný.
- [x] Dark mode — hráčská stránka automaticky podle systému: `static/style.css`, `@media (prefers-color-scheme: dark)` pro `.player-page`. Globální přepínač ani plošné pokrytí ostatních stránek tím nevzniká; vizuální test chybí.
