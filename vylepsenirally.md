# ClassRally — Stav a plán dalších vylepšení

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
- [ ] Otestovat AI generování s lokální Ollama přes nový `/v1/chat/completions` endpoint
- [ ] Otestovat AI generování s OpenAI API klíčem
- [ ] Otestovat výběr/vytvoření sady po generování
- [ ] Ověřit že přepínání jazyků funguje korektně na všech stránkách
- [ ] End-to-end test celé hry (lobby → otázky → podium)

### Priorita 2: Chybějící funkce z roadmapy
- [ ] **Obrazky v otazkach** — upload nebo URL, zobrazení v play.html i host.html
- [ ] **Avatary/profilove obrazky** — výběr z předdefinovaných
- [ ] **Sdileni sad otazek** — export jako odkaz/soubor
- [ ] **Export vysledku** — CSV export herních výsledků pro učitele

### Priorita 3: Gamifikace
- [ ] **XP system** — body přes více her, level progression
- [ ] **Achievementy/odznaky** — za streak, za počet her, za skóre
- [ ] **Ligy mezi třídami** — soutěž tříd

### Priorita 4: UX vylepšení
- [ ] Lepší mobilní responsivita (play.html)
- [ ] Animace přechodů mezi fázemi
- [ ] Zvukové efekty pro správnou/špatnou odpověď (MP3, ne synth)
- [ ] Dark mode
