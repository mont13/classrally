/* ClassRally task tours. Data is kept as JSON for the translation/selector contract tests. */
(() => {
  'use strict';
  const DATA = {
  "steps": [
    {
      "id": "portal",
      "page": "/host",
      "target": "a[href=\"/admin\"]",
      "titleKey": "tour.steps.portal.title",
      "textKey": "tour.steps.portal.text"
    },
    {
      "id": "banks",
      "page": "/admin",
      "target": "#bankSearch",
      "titleKey": "tour.steps.banks.title",
      "textKey": "tour.steps.banks.text",
      "tab": "questions"
    },
    {
      "id": "activate",
      "page": "/admin",
      "target": "#bankList .bank-actions > button:first-child",
      "titleKey": "tour.steps.activate.title",
      "textKey": "tour.steps.activate.text",
      "tab": "questions",
      "missing": "text"
    },
    {
      "id": "projector",
      "page": "/admin",
      "target": "a[href=\"/host\"]",
      "titleKey": "tour.steps.projector.title",
      "textKey": "tour.steps.projector.text"
    },
    {
      "id": "qr",
      "page": "/host",
      "target": "#qrBox",
      "titleKey": "tour.steps.qr.title",
      "textKey": "tour.steps.qr.text"
    },
    {
      "id": "players",
      "page": "/host",
      "target": "#kPlayers",
      "titleKey": "tour.steps.players.title",
      "textKey": "tour.steps.players.text"
    },
    {
      "id": "start",
      "page": "/host",
      "target": "#btnStart",
      "titleKey": "tour.steps.start.title",
      "textKey": "tour.steps.start.text"
    },
    {
      "id": "reveal",
      "page": "/host",
      "target": "#btnReveal",
      "titleKey": "tour.steps.reveal.title",
      "textKey": "tour.steps.reveal.text"
    },
    {
      "id": "next",
      "page": "/host",
      "target": "#btnNext",
      "titleKey": "tour.steps.next.title",
      "textKey": "tour.steps.next.text"
    },
    {
      "id": "analysis",
      "page": "/host",
      "target": "#btnAnalysis",
      "titleKey": "tour.steps.analysis.title",
      "textKey": "tour.steps.analysis.text",
      "missing": "text"
    },
    {
      "id": "again",
      "page": "/host",
      "target": "#btnReset",
      "titleKey": "tour.steps.again.title",
      "textKey": "tour.steps.again.text"
    },
    {
      "id": "create",
      "page": "/admin",
      "target": "#newBankName",
      "titleKey": "tour.steps.create.title",
      "textKey": "tour.steps.create.text",
      "tab": "questions"
    },
    {
      "id": "editor",
      "page": "/admin",
      "target": "#editorBank",
      "titleKey": "tour.steps.editor.title",
      "textKey": "tour.steps.editor.text",
      "tab": "editor"
    },
    {
      "id": "types",
      "page": "/admin",
      "target": "#btnAddQuestion",
      "titleKey": "tour.steps.types.title",
      "textKey": "tour.steps.types.text",
      "tab": "editor"
    },
    {
      "id": "save",
      "page": "/admin",
      "target": "#btnSaveBank",
      "titleKey": "tour.steps.save.title",
      "textKey": "tour.steps.save.text",
      "tab": "editor"
    },
    {
      "id": "ai",
      "page": "/admin",
      "target": "#btnAiGenerate",
      "titleKey": "tour.steps.ai.title",
      "textKey": "tour.steps.ai.text",
      "tab": "ai",
      "condition": "ai"
    },
    {
      "id": "history",
      "page": "/admin",
      "target": "#btnRefreshHistory",
      "titleKey": "tour.steps.history.title",
      "textKey": "tour.steps.history.text",
      "tab": "history"
    },
    {
      "id": "exports",
      "page": "/admin",
      "target": "#historyList .history-item .btn-row",
      "titleKey": "tour.steps.exports.title",
      "textKey": "tour.steps.exports.text",
      "tab": "history",
      "missing": "text"
    },
    {
      "id": "hw",
      "page": "/admin",
      "target": "#hwBankSelect",
      "titleKey": "tour.steps.hw.title",
      "textKey": "tour.steps.hw.text",
      "tab": "homework"
    },
    {
      "id": "hwlink",
      "page": "/admin",
      "target": "#btnCreateHomework",
      "titleKey": "tour.steps.hwlink.title",
      "textKey": "tour.steps.hwlink.text",
      "tab": "homework"
    },
    {
      "id": "exam",
      "page": "/admin",
      "target": "#examBank",
      "titleKey": "tour.steps.exam.title",
      "textKey": "tour.steps.exam.text",
      "tab": "exam"
    },
    {
      "id": "examprep",
      "page": "/admin",
      "target": "#btnExamPrepare",
      "titleKey": "tour.steps.examprep.title",
      "textKey": "tour.steps.examprep.text",
      "tab": "exam"
    },
    {
      "id": "examresults",
      "page": "/admin",
      "target": "#btnExamExport",
      "titleKey": "tour.steps.examresults.title",
      "textKey": "tour.steps.examresults.text",
      "tab": "exam"
    },
    {
      "id": "classes",
      "page": "/admin",
      "target": "[data-tab=\"classes\"]",
      "titleKey": "tour.steps.classes.title",
      "textKey": "tour.steps.classes.text",
      "tab": "classes"
    },
    {
      "id": "profile",
      "page": "/profile",
      "target": "#joinClassCode",
      "titleKey": "tour.steps.profile.title",
      "textKey": "tour.steps.profile.text",
      "missing": "text"
    },
    {
      "id": "school",
      "page": "/profile",
      "target": "[data-portal-login]",
      "titleKey": "tour.steps.school.title",
      "textKey": "tour.steps.school.text",
      "condition": "portal"
    },
    {
      "id": "joinqr",
      "page": "/play",
      "target": "#qrImg",
      "titleKey": "tour.steps.joinqr.title",
      "textKey": "tour.steps.joinqr.text"
    },
    {
      "id": "pin",
      "page": "/play",
      "target": "#pinInput",
      "titleKey": "tour.steps.pin.title",
      "textKey": "tour.steps.pin.text",
      "condition": "pin"
    },
    {
      "id": "nickname",
      "page": "/play",
      "target": "#nameInput",
      "titleKey": "tour.steps.nickname.title",
      "textKey": "tour.steps.nickname.text"
    },
    {
      "id": "join",
      "page": "/play",
      "target": "#joinBtn",
      "titleKey": "tour.steps.join.title",
      "textKey": "tour.steps.join.text"
    },
    {
      "id": "signed",
      "page": "/play",
      "target": "[data-account-signed-in]",
      "titleKey": "tour.steps.signed.title",
      "textKey": "tour.steps.signed.text",
      "condition": "signed"
    },
    {
      "id": "studentaccount",
      "page": "/play",
      "target": "[data-account-login]",
      "titleKey": "tour.steps.studentaccount.title",
      "textKey": "tour.steps.studentaccount.text",
      "condition": "anonymous"
    },
    {
      "id": "music",
      "page": "/host",
      "target": "#musicPanel > summary",
      "titleKey": "tour.steps.music.title",
      "textKey": "tour.steps.music.text"
    },
    {
      "id": "volume",
      "page": "/host",
      "target": "#musicVol",
      "titleKey": "tour.steps.volume.title",
      "textKey": "tour.steps.volume.text",
      "details": "musicPanel"
    },
    {
      "id": "sound",
      "page": "/host",
      "target": "#sfxToggle",
      "titleKey": "tour.steps.sound.title",
      "textKey": "tour.steps.sound.text",
      "details": "musicPanel"
    },
    {
      "id": "language",
      "page": "*",
      "target": ".i18n-switcher",
      "titleKey": "tour.steps.language.title",
      "textKey": "tour.steps.language.text"
    },
    {
      "id": "bug",
      "page": "*",
      "target": "#classrally-buggy-root button",
      "titleKey": "tour.steps.bug.title",
      "textKey": "tour.steps.bug.text",
      "missing": "text"
    },
    {
      "id": "studentlanguage",
      "page": "*",
      "target": ".i18n-switcher",
      "titleKey": "tour.steps.studentlanguage.title",
      "textKey": "tour.steps.studentlanguage.text"
    },
    {
      "id": "studentbug",
      "page": "*",
      "target": "#classrally-buggy-root button",
      "titleKey": "tour.steps.studentbug.title",
      "textKey": "tour.steps.studentbug.text",
      "missing": "text"
    },
    {
      "id": "examname",
      "page": "/exam",
      "target": "#nicknameInput",
      "titleKey": "tour.steps.examname.title",
      "textKey": "tour.steps.examname.text"
    },
    {
      "id": "examjoin",
      "page": "/exam",
      "target": "#joinButton",
      "titleKey": "tour.steps.examjoin.title",
      "textKey": "tour.steps.examjoin.text"
    },
    {
      "id": "examqr",
      "page": "/exam",
      "target": "#qrImage",
      "titleKey": "tour.steps.examqr.title",
      "textKey": "tour.steps.examqr.text"
    },
    {
      "id": "ranking",
      "page": "/host",
      "target": "#leaderboardTitle",
      "titleKey": "tour.steps.ranking.title",
      "textKey": "tour.steps.ranking.text"
    },
    {
      "id": "newclass",
      "page": "/host",
      "target": "#btnNewPin",
      "titleKey": "tour.steps.again.title",
      "textKey": "tour.steps.again.text"
    }
  ],
  "guides": [
    {
      "id": "quiz",
      "titleKey": "tour.guides.quiz.title",
      "textKey": "tour.guides.quiz.text",
      "steps": [
        "portal",
        "banks",
        "activate",
        "projector",
        "qr",
        "players",
        "start",
        "reveal",
        "next",
        "ranking",
        "analysis",
        "again",
        "newclass"
      ]
    },
    {
      "id": "questions",
      "titleKey": "tour.guides.questions.title",
      "textKey": "tour.guides.questions.text",
      "steps": [
        "create",
        "editor",
        "types",
        "save",
        "ai"
      ]
    },
    {
      "id": "results",
      "titleKey": "tour.guides.results.title",
      "textKey": "tour.guides.results.text",
      "steps": [
        "history",
        "exports"
      ]
    },
    {
      "id": "homework",
      "titleKey": "tour.guides.homework.title",
      "textKey": "tour.guides.homework.text",
      "steps": [
        "hw",
        "hwlink"
      ]
    },
    {
      "id": "exam",
      "titleKey": "tour.guides.exam.title",
      "textKey": "tour.guides.exam.text",
      "steps": [
        "exam",
        "examprep",
        "examresults"
      ]
    },
    {
      "id": "classes",
      "titleKey": "tour.guides.classes.title",
      "textKey": "tour.guides.classes.text",
      "steps": [
        "classes",
        "profile",
        "school"
      ]
    },
    {
      "id": "join",
      "titleKey": "tour.guides.join.title",
      "textKey": "tour.guides.join.text",
      "steps": [
        "joinqr",
        "pin",
        "nickname",
        "join",
        "signed",
        "studentaccount"
      ]
    },
    {
      "id": "music",
      "titleKey": "tour.guides.music.title",
      "textKey": "tour.guides.music.text",
      "steps": [
        "music",
        "volume",
        "sound"
      ]
    },
    {
      "id": "language",
      "titleKey": "tour.guides.language.title",
      "textKey": "tour.guides.language.text",
      "steps": [
        "language"
      ]
    },
    {
      "id": "bug",
      "titleKey": "tour.guides.bug.title",
      "textKey": "tour.guides.bug.text",
      "steps": [
        "bug"
      ]
    }
  ]
};
  const PAGES = {'/': 'landing', '/host': 'host', '/admin': 'admin', '/play': 'play', '/profile': 'profile', '/exam': 'exam'};
  const page = location.pathname.replace(/\/$/, '') || '/';
  if (!Object.hasOwn(PAGES, page) || window.ClassRallyTour) return;
  const student = ['/play', '/profile', '/exam'].includes(page);
  const SESSION = 'classrally.tour.active.v1';
  const PREF = 'classrally.tour.page.v1:';
  const stepsById = Object.fromEntries(DATA.steps.map(step => [step.id, step]));
  let active = null, view = '', card, ring, shades = [], launcher, offer;
  let target = null, previousFocus = null, frame = 0, tracking = 0, lastRect = '', generation = 0, dirty = false;
  let availabilityTimer = 0, availabilityIdle = 0, lastAvailability = -Infinity, restoreFocus = false;
  let visited = new Set();
  let originalPadding = '', basePadding = 0;
  const memory = {};
  const read = (store, key) => { try { return JSON.parse(window[store].getItem(key)); } catch (_) { return memory[key] || null; } };
  const write = (store, key, value) => {
    memory[key] = value;
    try { if (value === null) window[store].removeItem(key); else window[store].setItem(key, JSON.stringify(value)); } catch (_) { /* optional storage */ }
  };
  const t = (key, ...args) => i18n.t(key.startsWith('tour.') ? key : 'tour.' + key, ...args);
  const pref = () => read('localStorage', PREF + page) || {};
  const remember = values => write('localStorage', PREF + page, {...pref(), ...values});
  function visible(el) {
    if (!el || el.closest('[hidden], .hide, .hidden')) return false;
    const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  }
  const find = selector => {
    if (selector === '#classrally-buggy-root button') {
      const inline = [...document.querySelectorAll('[data-buggy-open]')].find(visible);
      if (inline) return inline;
      const root = document.getElementById('classrally-buggy-root');
      const launcher = (root?.shadowRoot || root)?.querySelector('button');
      return visible(launcher) ? launcher : null;
    }
    return selector ? [...document.querySelectorAll(selector)].find(visible) || null : null;
  };
  function busy() {
    return (page === '/play' && ['question', 'reveal'].includes(document.body.dataset.phase)) ||
      (page === '/host' && document.body.classList.contains('in-game')) ||
      (page === '/exam' && visible(document.getElementById('testSection')));
  }
  const TOUR_UI = '[id^="cr-tour"], .cr-tour-shade';
  const PAGE_OVERLAYS = '[role="dialog"], [aria-modal="true"], dialog[open], .overlay, .modal, .qr-overlay, ' +
    '#podiumOverlay, #teamPodiumOverlay, #analysisOverlay, #qrOverlay, #hostLoginOverlay, ' +
    '#getReadyOverlay, #finishCard, #spSummaryPane';
  const CONTROLS = 'button, a[href], input:not([type="hidden"]), select, textarea, summary, ' +
    '[role="button"], [role="link"], [tabindex], [onclick]';
  const CONTROL_CANDIDATES = CONTROLS + ', img, label, [style*="cursor"]';
  const LAYER_CANDIDATES = PAGE_OVERLAYS + ', [class*="overlay"], [class*="modal"], [style*="position"]';
  const ANIMATED_CONTENT = '#scoreBody, #teamLeaderboard, #kTimer, #kTimerItem, #timer, #timerItem, ' +
    '#countdownFill, #kAnswered, #kPlayers, #answered, #players, .countdown-bar, .confetti, canvas';
  const ANIMATED_IDS = new Set(['scoreBody', 'teamLeaderboard', 'kTimer', 'kTimerItem', 'timer', 'timerItem',
    'countdownFill', 'kAnswered', 'kPlayers', 'answered', 'players']);
  const STYLE_TARGETS = LAYER_CANDIDATES + ', ' + CONTROL_CANDIDATES + ', main, section, form, .card, [style]';
  const PAGE_OBSERVATION = {attributes: true, attributeOldValue: true,
    attributeFilter: ['data-phase', 'class', 'hidden', 'open', 'aria-modal', 'role'], childList: true, subtree: true};
  const layers = new Set();
  let styleObserver, pageObserver, gameplayBlocked = false;
  function watchStyle(el) {
    if (!el.closest(TOUR_UI) && !el.closest(ANIMATED_CONTENT)) {
      styleObserver.observe(el, {attributes: true, attributeFilter: ['style'], attributeOldValue: true});
    }
  }
  function rememberLayers(root) {
    if (root.nodeType !== 1 || root.closest(TOUR_UI) || root.closest(ANIMATED_CONTENT)) return;
    if (root.parentElement === document.body || root.matches(LAYER_CANDIDATES)) layers.add(root);
    if (root === document.body) {
      [...root.children].forEach(el => { layers.add(el); watchStyle(el); });
    }
    root.querySelectorAll(LAYER_CANDIDATES).forEach(el => layers.add(el));
    watchStyle(root);
    root.querySelectorAll(STYLE_TARGETS).forEach(watchStyle);
  }
  function pageOverlayOpen() {
    // Registered dialogs/layers and direct body children, never the whole DOM.
    // Hit testing also finds nested, unlabelled layers positioned by a stylesheet.
    const candidates = new Set(layers);
    document.elementsFromPoint(innerWidth / 2, innerHeight / 2).forEach(el => {
      if (el !== document.body && el !== document.documentElement) candidates.add(el);
    });
    return [...candidates].some(el => {
      if (!el.isConnected) { layers.delete(el); return false; }
      if (el.closest(TOUR_UI) || el.closest('[hidden], .hide, .hidden')) return false;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return false;
      const style = getComputedStyle(el);
      if (style.visibility !== 'visible' || style.pointerEvents === 'none' ||
          el.checkVisibility?.({checkVisibilityCSS: true}) === false) return false;
      // A podium starts fading in at opacity zero but already intercepts clicks.
      if (el.matches(PAGE_OVERLAYS)) return true;
      if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
      return ['fixed', 'absolute'].includes(style.position) && Number(style.zIndex) > 0 &&
        r.width >= innerWidth * .75 && r.height >= innerHeight * .75;
    });
  }
  function placeLauncher() {
    if (launcher.hidden) return;
    const languages = find('.i18n-switcher');
    const top = Math.max(12, (languages?.getBoundingClientRect().bottom || 0) + 10);
    launcher.style.top = top + 'px';
    launcher.style.right = innerWidth <= 600 ? '8px' : '12px';
    const r = launcher.getBoundingClientRect();
    // Read rectangles only for controls, then visibility/style only in the two
    // corner strips where the launcher can actually be placed.
    const columns = [r.left, 8];
    const candidates = new Set(document.querySelectorAll(CONTROL_CANDIDATES));
    for (const x of columns) {
      document.elementsFromPoint(x + r.width / 2, top + r.height / 2).forEach(el => candidates.add(el));
    }
    const obstacles = [];
    for (const el of candidates) {
      if (el.closest(TOUR_UI) || el.closest('[hidden], .hide, .hidden')) continue;
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height || b.bottom <= 0 || b.top >= innerHeight ||
          !columns.some(x => b.right + 8 > x && b.left < x + r.width + 8)) continue;
      const style = getComputedStyle(el);
      if (style.pointerEvents === 'none' || !(el.matches(CONTROLS) || style.cursor === 'pointer') ||
          el.checkVisibility?.({checkOpacity: true, checkVisibilityCSS: true}) === false) continue;
      obstacles.push(b);
    }
    const rows = [top, 12, ...obstacles.flatMap(b => [b.bottom + 8, b.top - r.height - 8])];
    let safe = null;
    for (const x of columns) {
      if (x < 8 || x + r.width > innerWidth - 8) continue;
      const y = rows.find(y => y >= 8 && y + r.height <= innerHeight - 8 && obstacles.every(b =>
        x + r.width + 8 <= b.left || x >= b.right + 8 || y + r.height + 8 <= b.top || y >= b.bottom + 8));
      if (y != null) { safe = {x, y}; break; }
    }
    // If the viewport has no free place, yield the space to the page controls.
    launcher.hidden = !safe;
    if (safe) {
      launcher.style.right = (innerWidth - safe.x - r.width) + 'px';
      launcher.style.top = safe.y + 'px';
    }
  }
  function eligible(step) {
    switch (step.condition) {
      case 'pin': return visible(document.getElementById('pinInput'));
      case 'signed': return visible(document.querySelector('[data-account-signed-in]'));
      case 'anonymous': return visible(document.querySelector('[data-account-login]'));
      case 'portal': return visible(document.querySelector('[data-portal-login]'));
      case 'ai': return Boolean(document.getElementById('cfgOllamaModel')?.value.trim() &&
        document.getElementById('cfgAiBaseUrl')?.value.trim() && !document.getElementById('btnAiGenerate')?.disabled);
      default: return true;
    }
  }
  function buildSteps(id, origin) {
    const guide = DATA.guides.find(item => item.id === id);
    if (!guide) return [];
    let ids = guide.steps;
    if (['/play', '/profile', '/exam'].includes(origin)) {
      if (id === 'language') ids = ['studentlanguage'];
      if (id === 'bug') ids = ['studentbug'];
      if (id === 'classes') ids = ['profile', 'school'];
      if (id === 'join' && origin === '/exam') ids = ['examname', 'examjoin', 'examqr'];
    }
    return ids.map(id => ({...stepsById[id], page: stepsById[id].page === '*' ? origin : stepsById[id].page,
      onlyIf: () => eligible(stepsById[id]),
      before: () => {
        const step = stepsById[id];
        if (step.tab) {
          const tab = document.querySelector(`[data-tab="${step.tab}"]`);
          if (visible(tab) && !tab.classList.contains('active')) tab.click();
        }
        if (step.details) {
          const details = document.getElementById(step.details);
          if (details) details.open = true;
        }
      }}));
  }
  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.textContent = text;
    return el;
  }
  function button(label, action, className = '') {
    const btn = element('button', 'cr-tour-button ' + className, t(label));
    btn.type = 'button'; btn.addEventListener('click', action); return btn;
  }
  function hideSpotlight() {
    cancelAnimationFrame(tracking); tracking = 0; lastRect = '';
    target = null; ring.hidden = true; shades.forEach(el => { el.hidden = true; });
  }
  function save() {
    if (active) write('sessionStorage', SESSION, {id: active.id, index: active.index, origin: active.origin,
      expected: active.steps[active.index]?.page || page, visited: [...visited], shown: active.shown || [], rendered: active.rendered});
  }
  function close(restore = true) {
    generation++; active = null; view = ''; card.hidden = true; hideSpotlight();
    document.body.style.paddingBottom = originalPadding;
    write('sessionStorage', SESSION, null);
    restoreFocus = restore;
    schedule();
  }
  function prepareCard(title, text, modal = false) {
    hideSpotlight(); card.replaceChildren(); card.hidden = false;
    card.className = 'cr-tour-card';
    delete card.dataset.step; delete card.dataset.guide;
    card.setAttribute('aria-modal', String(modal));
    const heading = element('h2', '', title); heading.id = 'cr-tour-title';
    card.appendChild(heading);
    if (text) { const p = element('p', '', text); p.id = 'cr-tour-text'; card.appendChild(p); }
    card.setAttribute('aria-describedby', text ? 'cr-tour-text' : 'cr-tour-title');
  }
  function followTarget() {
    if (document.hidden || !target || card.hidden) { tracking = 0; return; }
    const r = target.getBoundingClientRect();
    const key = [r.x, r.y, r.width, r.height].map(value => value.toFixed(1)).join(':');
    if (key !== lastRect) { lastRect = key; schedule(); }
    tracking = requestAnimationFrame(followTarget);
  }
  function focusCard() {
    card.focus({preventScroll: true}); schedule();
    if (target && !tracking) tracking = requestAnimationFrame(followTarget);
  }
  function finish() {
    for (const location of visited) {
      const key = PREF + location;
      write('localStorage', key, {...(read('localStorage', key) || {}), completed: true});
    }
    write('sessionStorage', SESSION, null); active = null; view = 'complete';
    prepareCard(t('complete'), t(student ? 'studentComplete' : 'completeText'));
    const label = element('label', 'cr-tour-never');
    const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = Boolean(pref().never);
    checkbox.addEventListener('change', () => remember({never: checkbox.checked}));
    label.append(checkbox, document.createTextNode(t('never'))); card.appendChild(label);
    card.appendChild(button('done', () => close())); focusCard();
  }
  function navigate(step) {
    save();
    // The fragment contains only a validated guide id/index and is a storage-free
    // fallback. No credentials, answers or user data are put in the URL.
    const url = new URL(step.page, location.origin);
    url.hash = 'cr-tour=' + encodeURIComponent(active.id + ':' + active.index + ':' + active.origin);
    location.assign(url.pathname + url.hash); return true;
  }
  async function show(index, direction = 1) {
    if (!active || busy()) { if (busy()) close(false); return; }
    const token = ++generation;
    if (index < 0) { if (active.rendered != null) show(active.rendered); return; }
    if (index >= active.steps.length) { finish(); return; }
    const step = active.steps[index];
    if (step.page !== page) {
      if (dirty && !window.confirm(t('leave'))) {
        if (!['step', 'login'].includes(view)) close();
        return;
      }
      active.index = index; navigate(step); return;
    }
    active.index = index; card.hidden = true; hideSpotlight();
    visited.add(page); save();
    // Authentication is a gate, never silently skip an entire teacher guide.
    const login = page === '/admin' ? find('#loginNick') : page === '/host' ? find('#hostLoginNick') : null;
    if (login) {
      view = 'login'; prepareCard(t('loginTitle'), t('loginText')); target = login;
      card.append(button('next', () => show(index)), button('end', () => close())); focusCard(); return;
    }
    step.before();
    // A tab may asynchronously fetch banks/history. Wait only for its target;
    // never contact AI or create records to make a tour step available.
    let el = find(step.target);
    for (let attempt = 0; !el && attempt < 5; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 60));
      if (token !== generation || !active) return;
      if (busy()) { close(false); return; }
      el = find(step.target);
    }
    if (busy()) { close(false); return; }
    if (!step.onlyIf() || (!el && step.missing !== 'text')) { show(index + direction, direction); return; }
    active.rendered = index; active.shown = [...new Set([...(active.shown || []), index])].sort((a, b) => a - b); save();
    view = 'step'; prepareCard(t(step.titleKey), t(step.textKey));
    card.dataset.step = step.id; card.dataset.guide = active.id;
    card.appendChild(element('p', 'cr-tour-progress', t('progress', index + 1, active.steps.length)));
    const actions = element('div', 'cr-tour-actions');
    const previous = previousStep();
    const back = button('back', () => { if (previous != null) show(previous, -1); }); back.disabled = previous == null;
    actions.append(back, button(index === active.steps.length - 1 ? 'done' : 'next', () => show(active.index + 1), 'cr-tour-primary'),
      button('end', () => close()));
    card.appendChild(actions); target = el;
    if (target) target.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    focusCard();
  }
  function previousStep() { return (active?.shown || []).filter(index => index < active.index).at(-1); }
  function start(id) {
    if (document.hidden || busy() || pageOverlayOpen()) return;
    previousFocus = document.activeElement;
    if (offer) offer.hidden = true;
    visited = new Set(); active = {id, index: 0, origin: page, steps: buildSteps(id, page)};
    if (active.steps.length) show(0);
  }
  function catalog() {
    if (document.hidden || busy() || pageOverlayOpen()) return;
    previousFocus = document.activeElement; close(false); view = 'catalog';
    if (offer) offer.hidden = true;
    prepareCard(t(student ? 'studentCatalog' : 'catalog'), '', false);
    card.classList.add('cr-tour-catalog');
    const list = element('div', 'cr-tour-grid');
    for (const guide of DATA.guides) {
      if (student && !['join', 'language', 'bug', 'classes'].includes(guide.id)) continue;
      const btn = button(guide.titleKey, () => start(guide.id), 'cr-tour-task');
      btn.dataset.tourGuide = guide.id;
      btn.replaceChildren(element('strong', '', t(guide.titleKey)), element('span', '', t(guide.textKey)));
      list.appendChild(btn);
    }
    card.append(list, button('end', () => close())); focusCard();
  }
  function refreshAvailability() {
    if (document.hidden) return;
    lastAvailability = performance.now();
    updateAvailability();
    placeLauncher();
    if (restoreFocus && !view) {
      restoreFocus = false;
      if (previousFocus?.isConnected && visible(previousFocus)) previousFocus.focus({preventScroll: true});
      else if (!launcher.hidden) launcher.focus({preventScroll: true});
    }
  }
  function scheduleAvailability() {
    if (document.hidden || availabilityTimer || availabilityIdle) return;
    availabilityTimer = setTimeout(() => {
      availabilityTimer = 0;
      if (document.hidden) return;
      if (window.requestIdleCallback) {
        availabilityIdle = requestIdleCallback(() => { availabilityIdle = 0; refreshAvailability(); }, {timeout: 100});
      } else refreshAvailability();
    }, Math.max(0, 250 - (performance.now() - lastAvailability)));
  }
  function schedule() {
    if (document.hidden) return;
    scheduleAvailability();
    // Spotlight follows its target at frame speed without re-scanning controls.
    if (view && !frame) frame = requestAnimationFrame(layout);
  }
  function layout() {
    frame = 0;
    if (document.hidden || card.hidden) return;
    const width = window.innerWidth, height = window.innerHeight, gap = 16;
    card.style.maxHeight = (height - gap * 2) + 'px';
    const padding = target ? (basePadding + card.getBoundingClientRect().height + gap * 3) + 'px' : originalPadding;
    if (document.body.style.paddingBottom !== padding) document.body.style.paddingBottom = padding;
    card.style.top = ''; card.style.bottom = '16px';
    card.style.left = '50%'; card.style.transform = 'translateX(-50%)';
    if (!visible(target)) { hideSpotlight(); return; }
    const r = target.getBoundingClientRect();
    if (r.height + card.getBoundingClientRect().height + gap * 3 > height) {
      card.style.maxHeight = Math.max(120, height - r.height - gap * 3) + 'px';
    }
    const h = card.getBoundingClientRect().height;
    // Reserve a separate viewport band for the target. On small screens keep
    // the card at the bottom and scroll the target above it. Fixed widgets at
    // the bottom instead get the card above them.
    const fixed = getComputedStyle(target).position === 'fixed' || target.closest('#classrally-buggy-root');
    let top = height - h - gap;
    if (r.bottom + gap <= top) { /* bottom band fits */ }
    else if (r.top - gap >= h + gap && (width > 600 || fixed)) top = gap;
    else if (!fixed) {
      const available = height - h - gap * 3;
      const desiredTop = Math.min(gap + Math.max(0, (available - r.height) / 2), top - gap - r.height);
      window.scrollBy({top: r.top - desiredTop, behavior: 'instant'});
    } else top = r.top - gap >= h ? gap : height - h - gap;
    card.style.bottom = ''; card.style.top = top + 'px';
    const b = target.getBoundingClientRect();
    const x = Math.max(0, b.left - 5), y = Math.max(0, b.top - 5);
    const right = Math.min(width, b.right + 5), bottom = Math.min(height, b.bottom + 5);
    if (right <= x || bottom <= y) { hideSpotlight(); return; }
    const box = (el, left, top, w, h) => {
      el.hidden = false; Object.assign(el.style, {left: left + 'px', top: top + 'px', width: Math.max(0, w) + 'px', height: Math.max(0, h) + 'px'});
    };
    box(ring, x, y, right - x, bottom - y);
    box(shades[0], 0, 0, width, y); box(shades[1], 0, y, x, bottom - y);
    box(shades[2], right, y, width - right, bottom - y); box(shades[3], 0, bottom, width, height - bottom);
  }
  function updateAvailability() {
    const playing = busy();
    if (['/host', '/play'].includes(page) && playing !== gameplayBlocked) {
      gameplayBlocked = playing;
      pageObserver.disconnect(); styleObserver.disconnect();
      if (playing) {
        // During questions/reveal the guide is unavailable. Only the game's
        // body phase/class can bring it back; do not watch animated descendants.
        pageObserver.observe(document.body, {attributes: true, attributeOldValue: true,
          attributeFilter: ['data-phase', 'class']});
      } else {
        pageObserver.observe(document.body, PAGE_OBSERVATION);
        layers.clear(); rememberLayers(document.body);
      }
    }
    const blocked = playing || pageOverlayOpen();
    launcher.hidden = blocked || !card.hidden;
    if (page === '/host' && document.body.classList.contains('in-game') && offer) offer.hidden = true;
    if (blocked) { if (offer) offer.hidden = true; if (active || view) close(false); }
  }
  function offerTour() {
    // Pupils open the catalog explicitly; joining must never need a dismissal.
    if (document.hidden || student || busy() || pageOverlayOpen() || active || view || pref().never || pref().completed || pref().offered) return;
    remember({offered: true});
    offer = element('aside', 'cr-tour-offer'); offer.id = 'cr-tour-offer';
    offer.setAttribute('aria-label', t(student ? 'studentOffer' : 'offer'));
    offer.appendChild(element('p', '', t(student ? 'studentOffer' : 'offer')));
    const defaultGuide = page === '/host' ? 'quiz' : page === '/admin' ? 'questions' : page === '/profile' ? 'classes' : student ? 'join' : 'quiz';
    offer.append(button('yes', () => start(defaultGuide)), button('later', () => { offer.hidden = true; }),
      button('never', () => { remember({never: true}); offer.hidden = true; }));
    // Reserve real space on teacher pages instead of covering controls while
    // scrolling, including the phone layout and the projector's PIN/QR panel.
    const teacherMain = ['/host', '/admin'].includes(page) ? document.querySelector('main.app') : null;
    if (teacherMain) {
      offer.classList.add('cr-tour-offer-inline');
      teacherMain.prepend(offer);
    } else document.body.appendChild(offer);
  }
  async function init() {
    // The pages initialise i18n themselves. A slow/offline load must not expose keys.
    for (let attempt = 0; t('launch') === 'tour.launch' && attempt < 100; attempt++) await new Promise(resolve => setTimeout(resolve, 50));
    if (t('launch') === 'tour.launch') return;
    launcher = button('launch', catalog, 'cr-tour-launch'); launcher.id = 'cr-tour-launch';
    launcher.setAttribute('aria-haspopup', 'dialog'); document.body.appendChild(launcher);
    card = element('section', 'cr-tour-card'); card.id = 'cr-tour-card'; card.hidden = true; card.tabIndex = -1;
    card.setAttribute('role', 'dialog'); card.setAttribute('aria-labelledby', 'cr-tour-title'); card.setAttribute('aria-live', 'polite');
    ring = element('div', 'cr-tour-ring'); ring.id = 'cr-tour-ring'; ring.hidden = true; ring.setAttribute('aria-hidden', 'true');
    shades = Array.from({length: 4}, () => { const el = element('div', 'cr-tour-shade'); el.hidden = true; el.setAttribute('aria-hidden', 'true'); return el; });
    originalPadding = document.body.style.paddingBottom;
    basePadding = parseFloat(getComputedStyle(document.body).paddingBottom) || 0;
    document.body.append(...shades, ring, card);
    window.addEventListener('resize', schedule); document.addEventListener('scroll', schedule, {passive: true, capture: true});
    new ResizeObserver(schedule).observe(card);
    new ResizeObserver(schedule).observe(document.body);
    document.addEventListener('load', schedule, true);
    document.addEventListener('transitionend', schedule, true);
    // No geometry/style reads in the observer. Timers, scores and decorations
    // cannot change availability; body ResizeObserver covers their size changes.
    function observePage(records) {
      if (document.hidden) return;
      let relevant = false;
      for (const record of records) {
        const el = record.target;
        if (ANIMATED_IDS.has(el.id) || el.closest?.(TOUR_UI) || el.closest?.(ANIMATED_CONTENT)) continue;
        // The game redraws unchanged labels/classes on every state message.
        // Ignore those writes before walking any ancestors or indexing layers.
        if (record.type === 'attributes' && record.oldValue === el.getAttribute(record.attributeName)) continue;
        if (record.type === 'childList' && record.addedNodes.length === 1 && record.removedNodes.length === 1 &&
            record.addedNodes[0].nodeType === 3 && record.removedNodes[0].nodeType === 3 &&
            record.addedNodes[0].textContent === record.removedNodes[0].textContent) continue;
        if (record.type === 'childList') {
          record.addedNodes.forEach(node => rememberLayers(node));
          relevant = true;
        } else if (['hidden', 'open', 'role', 'aria-modal', 'class'].includes(record.attributeName)) {
          // A nested layer may get all positioning from a stylesheet and have
          // no controls. Track its visibility/class change without scanning it.
          layers.add(el); watchStyle(el); relevant = true;
        } else if (el === document.body || layers.has(el) || el.matches(LAYER_CANDIDATES) ||
                   el.matches(CONTROL_CANDIDATES) || el.querySelector(CONTROL_CANDIDATES)) {
          if (el.matches(LAYER_CANDIDATES)) { layers.add(el); watchStyle(el); }
          relevant = true;
        }
      }
      if (relevant) schedule();
    }
    styleObserver = new MutationObserver(observePage);
    rememberLayers(document.body);
    // Inline animation styles are not observed across the whole subtree.
    // Only registered layers, controls and layout containers need that channel.
    pageObserver = new MutationObserver(observePage);
    pageObserver.observe(document.body, PAGE_OBSERVATION);
    document.addEventListener('visibilitychange', () => {
      clearTimeout(availabilityTimer); availabilityTimer = 0;
      if (availabilityIdle) window.cancelIdleCallback(availabilityIdle);
      availabilityIdle = 0;
      cancelAnimationFrame(frame); frame = 0;
      cancelAnimationFrame(tracking); tracking = 0;
      styleObserver.disconnect();
      if (!document.hidden) {
        // Changes while hidden were ignored; refresh the selective layer index.
        if (!gameplayBlocked) { layers.clear(); rememberLayers(document.body); }
        schedule();
        if (target && view) tracking = requestAnimationFrame(followTarget);
      }
    });
    document.addEventListener('input', event => { if (!event.target.closest('[id^="cr-tour"]')) dirty = true; });
    document.addEventListener('change', event => { if (!event.target.closest('[id^="cr-tour"]')) dirty = true; });
    document.addEventListener('click', event => {
      if (event.target.closest('#questionEditor button, #btnAddQuestion, #btnImportJson, #btnImportCsv')) dirty = true;
    }, true);
    for (const type of ['dragstart', 'drop']) {
      document.addEventListener(type, event => { if (event.target.closest('#questionEditor')) dirty = true; }, true);
    }
    document.addEventListener('keydown', event => {
      if (!view) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(); return; }
      if (view === 'step' && active && !event.altKey && !event.ctrlKey && !event.metaKey &&
          !event.target.matches('input, textarea, select, [contenteditable="true"]') && ['ArrowRight', 'ArrowLeft'].includes(event.key)) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (event.key === 'ArrowRight') show(active.index + 1);
        else if (previousStep() != null) show(previousStep(), -1);
      }
      if (event.key === 'Tab' && view === 'catalog' && card.contains(document.activeElement)) {
        const buttons = [...card.querySelectorAll('button:not(:disabled), input')].filter(visible);
        if (event.shiftKey && (document.activeElement === buttons[0] || document.activeElement === card)) { event.preventDefault(); buttons.at(-1)?.focus(); }
        else if (!event.shiftKey && document.activeElement === buttons.at(-1)) { event.preventDefault(); buttons[0]?.focus(); }
      }
    }, true);
    i18n.onLangChange(() => {
      launcher.textContent = t('launch');
      if (active) show(active.index); else if (view === 'catalog') catalog(); else if (view === 'complete') finish();
      if (offer) { offer.remove(); offer = null; }
    });
    // Before joining, /play only shows the form. Once joined, the game owns
    // data-phase (including homework); the tour never requests room state.
    window.addEventListener('pageshow', schedule);
    let resume = read('sessionStorage', SESSION);
    const match = location.hash.match(/^#cr-tour=([^#]+)$/);
    if (match) {
      try {
        const [id, index, origin] = decodeURIComponent(match[1]).split(':');
        resume = {...(resume?.id === id ? resume : {}), id, index: Number(index), origin, expected: page};
      } catch (_) { resume = null; }
      history.replaceState(null, '', location.pathname + location.search);
    }
    if (resume && resume.expected === page && Object.hasOwn(PAGES, resume.origin)) {
      const steps = buildSteps(resume.id, resume.origin);
      if (Number.isInteger(resume.index) && resume.index >= 0 && resume.index < steps.length && steps[resume.index].page === page) {
        active = {...resume, steps,
          shown: Array.isArray(resume.shown) ? resume.shown.filter(index => Number.isInteger(index) && index >= 0 && index < steps.length) : [],
          rendered: Number.isInteger(resume.rendered) && resume.rendered >= 0 && resume.rendered < steps.length ? resume.rendered : null};
        const priorPages = Array.isArray(resume.visited) ? resume.visited : steps.slice(0, resume.index + 1).map(step => step.page);
        visited = new Set(priorPages.filter(path => Object.hasOwn(PAGES, path)));
        show(resume.index);
      }
    }
    launcher.hidden = busy(); schedule();
    if (!active) setTimeout(offerTour, 1200);
  }
  window.ClassRallyTour = {start, catalog, close, data: DATA};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true}); else init();
})();
