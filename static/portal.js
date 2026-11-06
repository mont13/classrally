/* Optional school-portal login shared by the existing login screens. */
(() => {
  'use strict';

  async function initPortalLogin() {
    const containers = [...document.querySelectorAll('[data-portal-login]')];
    if (!containers.length) return;
    try {
      const response = await fetch('/api/auth/portal/config', {
        cache: 'no-store', credentials: 'same-origin'
      });
      if (!response.ok) return;
      const config = await response.json();
      if (config.enabled !== true || typeof config.login_url !== 'string' || !config.login_url.trim()) return;
      const url = new URL(config.login_url, location.origin);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return;
      url.hash = '';
      url.searchParams.set('next', location.pathname + location.search);

      function updateLabels() {
        const translated = typeof i18n !== 'undefined' ? i18n.t('portal.default_name') : '';
        const name = typeof config.name === 'string' && config.name.trim()
          ? config.name : (translated && translated !== 'portal.default_name' ? translated : 'Školní portál');
        document.dispatchEvent(new CustomEvent('classrally:portal-ready', { detail: { name } }));
        const label = typeof i18n !== 'undefined' ? i18n.t('portal.login', name) : '';
        containers.forEach(container => {
          const link = container.querySelector('a');
          link.href = url.href;
          link.textContent = label && label !== 'portal.login' ? label : 'Přihlásit přes ' + name;
          container.hidden = false;
        });
      }
      updateLabels();
      if (typeof i18n !== 'undefined') i18n.onLangChange(updateLabels);
    } catch {
      // Optional login: an unavailable configuration must not block the existing login.
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPortalLogin, { once: true });
  } else {
    initPortalLogin();
  }
})();
