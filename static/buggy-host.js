/* ClassRally host for an optional bug-report widget ("Buggy").
 *
 * Does nothing unless the server has a collector configured
 * (/api/bug-report/config). The widget itself is served by the server under
 * /buggy/; this file only supplies the screenshot and the transport:
 * reports go to /api/bug-report on this server, never straight to the
 * collector. Passwords, tokens and secret fields are masked in the
 * screenshot before it leaves the browser.
 */
(() => {
  'use strict';
  if (window.__classrallyBuggy) return;
  window.__classrallyBuggy = true;

  const SECRET = '[data-buggy-secret], input[type="password"], [name*="password" i], [name*="token" i], [name*="secret" i], [autocomplete="current-password"], [autocomplete="new-password"]';
  const SECRET_FIELD = /(?:^|[^a-z0-9])(?:password|passwd|pwd|hesl[oa]|token|secret|otp|api[\s_-]*(?:key|klic))(?:$|[^a-z0-9])/i;
  const EXCLUDE = '#classrally-buggy-root, script, iframe, video, audio';
  const LIMIT = 800000; // base64 characters, keeps uploads small on school Wi-Fi
  const LOCALES = ['cs', 'sk', 'en', 'de'];

  function page() {
    const path = location.pathname.replace(/\/+$/, '') || '/';
    const known = {'/': 'landing', '/play': 'play', '/host': 'host', '/admin': 'admin', '/exam': 'exam',
      '/profile': 'profile', '/reset': 'account', '/verify-email': 'account', '/invite': 'account'};
    return known[path] || 'other';
  }

  function locale() {
    // i18n.js declares a top-level const, which is not a window property.
    const api = typeof i18n !== 'undefined' ? i18n : null;  // eslint-disable-line no-undef
    let lang = api && typeof api.getLang === 'function' ? api.getLang() : '';
    if (!lang) { try { lang = localStorage.getItem('classrally_lang') || 'cs'; } catch (error) { lang = 'cs'; } }
    return LOCALES.includes(lang) ? lang : (lang === 'uk' ? 'en' : 'cs');
  }

  function secretElements(root) {
    const elements = new Set(root.querySelectorAll(SECRET));
    for (const field of root.querySelectorAll('input, textarea')) {
      // Judge the field by its labels and attributes, never by what was typed.
      const names = ['name', 'id', 'autocomplete', 'aria-label', 'placeholder']
        .map(attribute => field.getAttribute(attribute) || '');
      for (const label of field.labels || []) names.push(label.textContent || '');
      if (names.some(name => SECRET_FIELD.test(name.replace(/([a-z0-9])([A-Z])/g, '$1 $2')
        .normalize('NFKD').replace(/[̀-ͯ]/g, '')))) elements.add(field);
    }
    return [...elements];
  }

  function foreignImage(element) {
    if (element.tagName !== 'IMG') return false;
    try { return new URL(element.currentSrc || element.src, location.href).origin !== location.origin; }
    catch (error) { return true; }
  }

  async function capture() {
    const origin = location.href;
    const width = window.innerWidth, height = window.innerHeight;
    const scrollX = window.scrollX, scrollY = window.scrollY;
    const scale = Math.min(1, 1600 / width, 1600 / height, Math.sqrt(2000000 / (width * height)));
    const visible = element => !element.closest('[hidden]') && (typeof element.checkVisibility !== 'function' ||
      element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
    const rectangles = secretElements(document).filter(visible)
      .flatMap(element => [...element.getClientRects()])
      .filter(rect => rect.width && rect.height && rect.right > 0 && rect.bottom > 0 &&
        rect.left < width && rect.top < height);
    let timeout;
    try {
      const canvas = await Promise.race([
        window.InvoAIMBuggy.captureCanvas(document.documentElement, {
          x: scrollX, y: scrollY, width, height, windowWidth: width, windowHeight: height,
          scale, logging: false, useCORS: false, allowTaint: false, imageTimeout: 3000,
          ignoreElements: element => Boolean(element.closest(EXCLUDE)) || foreignImage(element),
          onclone: clone => {
            for (const element of secretElements(clone)) {
              element.style.setProperty('visibility', 'hidden', 'important');
              if ('value' in element) element.value = '';
              element.removeAttribute('value');
            }
            clone.defaultView.scrollTo(scrollX, scrollY);
          },
        }),
        new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('capture_timeout')), 8000); }),
      ]);
      if (location.href !== origin) throw new Error('capture_origin_changed');
      const context = canvas.getContext('2d');
      if (!context) throw new Error('capture_unavailable');
      context.setTransform(1, 0, 0, 1, 0, 0);
      context.fillStyle = '#e8edf4';
      // Opaque masks with a pixel margin: no secret pixels remain.
      for (const rect of rectangles) {
        const x = Math.floor(rect.left * scale) - 1, y = Math.floor(rect.top * scale) - 1;
        context.fillRect(x, y, Math.ceil(rect.right * scale) + 1 - x, Math.ceil(rect.bottom * scale) + 1 - y);
      }
      let result = canvas, dataUrl = result.toDataURL('image/png');
      while (dataUrl.length > LIMIT && Math.max(result.width, result.height) > 320) {
        const reduced = document.createElement('canvas');
        reduced.width = Math.max(1, Math.floor(result.width * 0.8));
        reduced.height = Math.max(1, Math.floor(result.height * 0.8));
        reduced.getContext('2d').drawImage(result, 0, 0, reduced.width, reduced.height);
        result = reduced; dataUrl = result.toDataURL('image/png');
      }
      if (dataUrl.length > LIMIT) throw new Error('capture_too_large');
      return {dataUrl, surface: 'page', url: location.pathname};
    } finally { clearTimeout(timeout); }
  }

  function authHeader() {
    try {
      const user = localStorage.getItem('quiz_user_token');
      if (user) return 'UserToken ' + user;
      const admin = localStorage.getItem('quiz_admin_token');
      if (admin) return 'Bearer ' + admin;
    } catch (error) { /* storage blocked: report anonymously */ }
    return '';
  }

  async function submit(payload) {
    const headers = {'Content-Type': 'application/json'};
    const auth = authHeader();
    if (auth) headers.Authorization = auth;
    const body = {popis: payload.popis, typ: payload.typ, clientRequestId: payload.clientRequestId,
      viewport: payload.viewport, surface: payload.surface, url: location.pathname, page: page(),
      phase: document.body ? document.body.dataset.phase || null : null};
    if (payload.screenshotDataUrl) body.screenshotDataUrl = payload.screenshotDataUrl;
    let response;
    try {
      response = await fetch('/api/bug-report', {method: 'POST', credentials: 'same-origin', headers,
        body: JSON.stringify(body)});
    } catch (error) {
      throw Object.assign(new Error('report_failed'), {code: 'REPORT_FAILED'});
    }
    let receipt = null;
    try { receipt = await response.json(); } catch (error) { receipt = null; }
    if (response.status !== 201 || !receipt || !Number.isSafeInteger(receipt.id) || receipt.id <= 0) {
      const code = response.status === 429 ? 'TOO_MANY_REQUESTS' :
        response.status === 413 ? 'SCREENSHOT_TOO_LARGE' : 'REPORT_FAILED';
      throw Object.assign(new Error('report_failed'), {code});
    }
    return {id: String(receipt.id), screenshotSaved: Boolean(receipt.screenshotSaved)};
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = src; script.async = true;
      script.onload = resolve; script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  async function start() {
    let config;
    try {
      const response = await fetch('/api/bug-report/config', {credentials: 'same-origin'});
      config = response.ok ? await response.json() : null;
    } catch (error) { return; }
    if (!config || !config.enabled || !config.script) return;
    try { await loadScript(config.script); } catch (error) { return; }
    if (!window.InvoAIMBuggy || typeof window.InvoAIMBuggy.mountBuggyWidget !== 'function') return;
    const target = document.createElement('div');
    target.id = 'classrally-buggy-root';
    // Own stacking context above every page layer (language flags, overlays).
    // The widget positions its launcher and dialog relative to the viewport.
    target.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;z-index:2147483000;';
    document.body.appendChild(target);
    window.InvoAIMBuggy.mountBuggyWidget({target, locale: locale(), capture, submit, url: location.pathname,
      captureNotice: 'Snímek zachytí právě viditelnou část stránky. Hesla a tokeny jsou skryté. ' +
        'Před odesláním náhled zkontroluj; přiložení snímku můžeš vypnout.'});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
