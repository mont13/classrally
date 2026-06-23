/**
 * ClassRally i18n — lightweight runtime, zero dependencies.
 *
 * Usage:
 *   await i18n.setLang('en');   // load + apply
 *   i18n.t('play.join_btn');    // get translated string
 *   i18n.apply();               // re-scan DOM for data-i18n attrs
 */
const i18n = (() => {
  const STORAGE_KEY = 'classrally_lang';
  const DEFAULT_LANG = 'cs';
  const SUPPORTED = ['cs', 'en', 'sk', 'de'];

  let _lang = DEFAULT_LANG;
  let _cache = {};   // lang -> flat translations object
  let _data = {};    // current translations (nested)
  let _callbacks = []; // functions to call after language change

  /** Fetch and cache a translation file. Returns the nested object. */
  async function load(lang) {
    if (!SUPPORTED.includes(lang)) lang = DEFAULT_LANG;
    if (_cache[lang]) return _cache[lang];
    try {
      const res = await fetch(`/static/i18n/${lang}.json`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const obj = await res.json();
      _cache[lang] = obj;
      return obj;
    } catch (e) {
      console.warn(`[i18n] Could not load "${lang}":`, e.message);
      return _cache[DEFAULT_LANG] || {};
    }
  }

  /**
   * Get a translation by dot-notation key.
   * Falls back to the key itself when not found.
   * Supports simple {0} positional placeholders.
   */
  function t(key, ...args) {
    const parts = key.split('.');
    let val = _data;
    for (const p of parts) {
      if (val && typeof val === 'object' && p in val) {
        val = val[p];
      } else {
        return key;   // fallback: return key
      }
    }
    if (typeof val !== 'string') return key;
    // Replace {0}, {1}, … placeholders
    return val.replace(/\{(\d+)\}/g, (_, i) => args[+i] ?? '');
  }

  /**
   * Scan the DOM for elements with data-i18n attributes and update them.
   *
   * Supported attribute forms:
   *   data-i18n="key"              → element.textContent
   *   data-i18n-placeholder="key"  → element.placeholder
   *   data-i18n-title="key"        → element.title
   *   data-i18n-aria="key"         → element.ariaLabel
   */
  function apply(root) {
    const scope = root || document;

    scope.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const translated = t(key);
      if (translated !== key) el.textContent = translated;
    });

    scope.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      const translated = t(key);
      if (translated !== key) el.placeholder = translated;
    });

    scope.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      const translated = t(key);
      if (translated !== key) el.title = translated;
    });

    scope.querySelectorAll('[data-i18n-aria]').forEach(el => {
      const key = el.getAttribute('data-i18n-aria');
      const translated = t(key);
      if (translated !== key) el.setAttribute('aria-label', translated);
    });
  }

  /**
   * Switch language, persist to localStorage, re-apply to DOM.
   * Returns a promise that resolves when done.
   */
  async function setLang(lang) {
    if (!SUPPORTED.includes(lang)) lang = DEFAULT_LANG;
    _lang = lang;
    localStorage.setItem(STORAGE_KEY, lang);
    _data = await load(lang);
    apply();
    // Update <html lang="..."> attribute
    document.documentElement.lang = lang;
    // Update switcher buttons if present
    document.querySelectorAll('.i18n-switcher [data-lang]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.lang === lang);
    });
    // Notify registered callbacks (for JS-generated dynamic content)
    _callbacks.forEach(fn => { try { fn(lang); } catch(e) { console.warn('[i18n] callback error:', e); } });
    return _data;
  }

  /**
   * Register a callback to be invoked after every language change.
   * Use this for re-rendering JS-generated dynamic content.
   */
  function onLangChange(fn) {
    if (typeof fn === 'function') _callbacks.push(fn);
  }

  /** Return the currently active language code. */
  function getLang() { return _lang; }

  /** Return list of supported language codes. */
  function supported() { return [...SUPPORTED]; }

  /**
   * Auto-initialise: read lang from localStorage (or browser preference),
   * load translations, apply to DOM.
   * Call once after DOMContentLoaded.
   */
  async function init() {
    // 1. Persisted choice
    let lang = localStorage.getItem(STORAGE_KEY);
    // 2. Browser language fallback
    if (!lang) {
      const nav = (navigator.language || '').toLowerCase().slice(0, 2);
      if (SUPPORTED.includes(nav)) lang = nav;
    }
    // 3. Default
    if (!lang) lang = DEFAULT_LANG;
    await setLang(lang);
  }

  return { load, t, apply, setLang, getLang, supported, init, onLangChange };
})();

/**
 * Inject a small language-switcher widget into the page.
 * Call after DOMContentLoaded.
 *
 * @param {string[]} [langs]  Subset to show; defaults to all supported.
 */
function injectLangSwitcher(langs) {
  const supported = langs || i18n.supported();

  const wrapper = document.createElement('div');
  wrapper.className = 'i18n-switcher';
  wrapper.setAttribute('aria-label', 'Language / Jazyk');

  supported.forEach(lang => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.dataset.lang = lang;
    btn.textContent = lang.toUpperCase();
    btn.title = lang;
    btn.classList.toggle('active', lang === i18n.getLang());
    btn.addEventListener('click', () => i18n.setLang(lang));
    wrapper.appendChild(btn);
  });

  document.body.appendChild(wrapper);
}
