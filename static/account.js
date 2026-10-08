// ClassRally account page: forgotten password, reset link, e-mail
// confirmation and teacher invitation (/reset, /verify-email, /invite).
// Tokens are redeemed by POST from this page, never by the GET of the link,
// so mail scanners that prefetch links cannot use them up.
(function () {
  'use strict';

  var TOKEN_KEY = 'quiz_user_token';
  var params = new URLSearchParams(location.search);
  var token = params.get('token') || '';

  function $(id) { return document.getElementById(id); }

  function show(id) {
    ['forgotSection', 'resetSection', 'verifySection', 'inviteSection', 'invalidSection']
      .forEach(function (s) { $(s).classList.toggle('hide', s !== id); });
  }

  function message(id, key, ok, extra) {
    var el = $(id);
    el.textContent = extra || i18n.t(key);
    el.className = 'msg ' + (ok ? 'ok' : 'err');
  }

  async function post(path, body) {
    body.lang = i18n.getLang();
    var r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var data = {};
    try { data = await r.json(); } catch (_) {}
    return { status: r.status, data: data };
  }

  async function tokenValid(purpose) {
    if (!token) return null;
    var r = await post('/api/auth/token-check', { token: token, purpose: purpose });
    return r.data.valid ? r.data : null;
  }

  // Remove the token from the address bar (history, screenshots, shoulder surfing).
  function hideTokenFromUrl() {
    if (token && window.history && history.replaceState) {
      history.replaceState(null, '', location.pathname);
    }
  }

  $('btnForgot').addEventListener('click', async function () {
    var login = $('forgotLogin').value.trim();
    if (!login) { $('forgotLogin').focus(); return; }
    this.disabled = true;
    var r = await post('/api/auth/forgot-password', { login: login });
    this.disabled = false;
    if (r.status === 429) message('forgotMsg', 'account.too_many', false);
    else if (r.status === 200) message('forgotMsg', 'account.forgot_sent', true);
    else message('forgotMsg', 'account.error', false);
  });

  $('btnReset').addEventListener('click', async function () {
    var p1 = $('resetPass').value, p2 = $('resetPass2').value;
    if (p1 !== p2) { message('resetMsg', 'account.passwords_differ', false); return; }
    this.disabled = true;
    var r = await post('/api/auth/reset-password', { token: token, new_password: p1 });
    if (r.status === 200) {
      localStorage.removeItem(TOKEN_KEY);
      message('resetMsg', 'account.reset_done', true);
      $('resetPass').value = $('resetPass2').value = '';
      return;
    }
    this.disabled = false;
    if (r.data.code === 'invalid_token') { show('invalidSection'); return; }
    message('resetMsg', 'account.error', false, r.data.error);
  });

  $('btnInvite').addEventListener('click', async function () {
    var p1 = $('invitePass').value, p2 = $('invitePass2').value;
    if (p1 !== p2) { message('inviteMsg', 'account.passwords_differ', false); return; }
    this.disabled = true;
    var r = await post('/api/auth/accept-invite', {
      token: token, nickname: $('inviteNick').value.trim(), password: p1,
    });
    if (r.status === 200 && r.data.token) {
      localStorage.setItem(TOKEN_KEY, r.data.token);
      message('inviteMsg', 'account.invite_done', true);
      setTimeout(function () { location.href = '/admin'; }, 1200);
      return;
    }
    this.disabled = false;
    if (r.data.code === 'invalid_token') { show('invalidSection'); return; }
    message('inviteMsg', 'account.error', false, r.data.error);
  });

  async function start() {
    var path = location.pathname;
    if (path === '/verify-email') {
      show('verifySection');
      hideTokenFromUrl();
      var r = await post('/api/auth/verify-email', { token: token });
      if (r.status === 200) message('verifyMsg', 'account.verify_done', true, i18n.t('account.verify_done', r.data.email));
      else if (r.data.code === 'email_taken') message('verifyMsg', 'account.error', false, r.data.error);
      else show('invalidSection');
      return;
    }
    if (path === '/invite') {
      var inv = await tokenValid('invite');
      hideTokenFromUrl();
      if (!inv) { show('invalidSection'); return; }
      $('inviteEmail').textContent = inv.email;
      show('inviteSection');
      return;
    }
    // /reset: with a token set a new password, without one ask for a link
    if (token) {
      var ok = await tokenValid('reset');
      hideTokenFromUrl();
      show(ok ? 'resetSection' : 'invalidSection');
      return;
    }
    show('forgotSection');
  }

  i18n.init().then(function () {
    injectLangSwitcher();
    var titleEl = document.querySelector('title[data-i18n]');
    if (titleEl) document.title = i18n.t(titleEl.getAttribute('data-i18n'));
    start();
  });
})();
