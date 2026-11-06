/* Run synchronously before any other callback-page script or request. */
(() => {
  'use strict';
  let fragment = location.hash;
  let token = '';
  let fragmentCleared = false;
  try {
    history.replaceState(null, '', location.pathname + location.search);
    fragmentCleared = true;
    const params = new URLSearchParams(fragment.slice(1));
    if (params.getAll('t').length === 1) token = params.get('t') || '';
  } catch {
    // Never submit a credential while it remains in the address bar.
  }
  fragment = '';

  function safeNext(next, role) {
    const fallback = role === 'teacher' ? '/host' : '/play';
    if (typeof next !== 'string' || next.length > 2048) return fallback;
    let decoded = next;
    // Check nested escaping too, so another URL parser cannot turn this into
    // a protocol-relative URL or a backslash-based authority.
    for (let depth = 0; depth < 8; depth++) {
      if (!decoded.startsWith('/') || decoded.startsWith('//') || /[\\\x00-\x20\x7f]/.test(decoded)) return fallback;
      try {
        const unescaped = decodeURIComponent(decoded);
        if (unescaped === decoded) {
          const url = new URL(decoded, location.origin);
          if (url.origin !== location.origin || url.pathname.startsWith('//')) return fallback;
          return next;
        }
        decoded = unescaped;
      } catch {
        return fallback;
      }
    }
    return fallback;
  }

  async function finishLogin() {
    const status = document.getElementById('portalStatus');
    const back = document.getElementById('portalBack');
    let statusKey = 'portal.working';
    let portalConfig = null;
    const fallbackTexts = {
      'portal.working': 'Ověřuji přihlášení…',
      'portal.error': 'Přihlášení se nepodařilo. Odkaz mohl vypršet nebo už byl použitý. Otevřete nový odkaz ve školním portálu.',
      'portal.disabled': 'Přihlášení přes školní portál na tomto serveru není zapnuté.',
      'portal.storage_error': 'Přihlášení nelze uložit. Povolte úložiště prohlížeče a otevřete nový odkaz ve školním portálu.',
      'portal.default_name': 'Školní portál'
    };
    function translate(key, ...args) {
      const value = typeof i18n !== 'undefined' ? i18n.t(key, ...args) : key;
      return value === key ? (fallbackTexts[key] || key) : value;
    }
    function render() {
      status.setAttribute('data-i18n', statusKey);
      status.textContent = translate(statusKey);
      if (portalConfig) {
        const name = typeof portalConfig.name === 'string' && portalConfig.name.trim()
          ? portalConfig.name : translate('portal.default_name');
        const label = translate('portal.back', name);
        back.textContent = label === 'portal.back' ? 'Zpět na ' + name : label;
      }
    }

    if (!fragmentCleared) {
      statusKey = 'portal.error';
      render();
      return;
    }
    const configPromise = fetch('/api/auth/portal/config', {
      cache: 'no-store', credentials: 'same-origin'
    }).then(async response => {
      if (!response.ok) return;
      const config = await response.json();
      if (config.enabled !== true || typeof config.login_url !== 'string' || !config.login_url.trim()) return;
      const url = new URL(config.login_url, location.origin);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return;
      url.hash = '';
      back.href = url.href;
      portalConfig = config;
      document.getElementById('portalBackWrap').hidden = false;
      render();
    }).catch(() => {});

    if (typeof i18n !== 'undefined') {
      i18n.onLangChange(render);
      // Translations must not delay exchanging the short-lived portal token.
      i18n.init().then(() => {
        if (typeof injectLangSwitcher === 'function') injectLangSwitcher();
      }).catch(() => {});
    }
    render();
    if (!token) {
      statusKey = 'portal.error';
      render();
      await configPromise;
      return;
    }

    try {
      const requestBody = JSON.stringify({ token });
      token = '';
      const response = await fetch('/api/auth/portal', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: requestBody
      });
      if (!response.ok) {
        statusKey = response.status === 404 ? 'portal.disabled' : 'portal.error';
        render();
        await configPromise;
        return;
      }
      const data = await response.json();
      if (!data.ok || typeof data.token !== 'string' || !data.token || !data.user) throw new Error('Invalid response');
      try {
        localStorage.setItem('quiz_user_token', data.token);
      } catch {
        statusKey = 'portal.storage_error';
        render();
        await configPromise;
        return;
      }
      location.replace(safeNext(data.next, data.user.role));
    } catch {
      statusKey = 'portal.error';
      render();
      await configPromise;
    } finally {
      token = '';
    }
  }

  document.addEventListener('DOMContentLoaded', finishLogin, { once: true });
})();
