#!/usr/bin/env python3
"""Local Chromium acceptance: omez-pamet -- python3 test_mobile_game_browser.py.

Only synthetic banks/accounts in .cache; all non-loopback requests are blocked.
"""
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse


def run():
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parent
    qa = root / '.cache' / 'mobile-game-qa'
    qa.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=qa) as runtime:
        runtime = Path(runtime)
        os.environ.update(QUIZ_DATA_DIR=str(runtime / 'data'), QUIZ_HISTORY_DIR=str(runtime / 'history'),
                          QUIZ_QUESTIONS_DIR=str(runtime / 'questions'), QUIZ_UPLOADS_DIR=str(runtime / 'uploads'),
                          QUIZ_ROOMS='single', MAIL_MODE='off', QUIZ_GAME_LOG='0', BUGGY_URL='',
                          BUGGY_PROJECT_KEY='', BUGGY_WIDGET_DIR='', CLASSRALLY_PORTAL_SECRET='',
                          QUIZ_BASE_URL='', AI_BASE_URL='http://127.0.0.1:1', AI_API_KEY='')
        for directory in ('questions', 'history', 'uploads'):
            (runtime / directory).mkdir()
        bank = [{'id': 'o', 'type': 'ordering', 'prompt': 'Seřaď čísla vzestupně',
                 'items': ['jedna', 'dva', 'tři', 'čtyři', 'pět']},
                {'id': 'm', 'type': 'multiselect', 'prompt': 'Vyber čísla',
                 'options': ['jedna', 'dva', 'tři', 'písmeno'], 'correct_indices': [0, 1, 2]}]
        (runtime / 'questions' / 'mobile.json').write_text(json.dumps(bank), encoding='utf-8')
        import server
        server.user_db.PBKDF2_ITERATIONS = 1000
        server.user_db.init_db()
        teacher = server.user_db.create_user('MobileTeacher', secrets.token_hex(12), 'teacher')
        token = server.user_db.create_session(teacher['id'])

        class Handler(server.Handler):
            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        base = f'http://127.0.0.1:{httpd.server_port}'
        server.SERVER_INFO.update(host_urls=[base + '/host'], play_urls=[base + '/play'], loopback_only=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        errors, foreign = [], []
        checks = 0
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)

                def context(width, teacher=False):
                    ctx = browser.new_context(viewport={'width': width, 'height': 850}, has_touch=width < 600,
                                              service_workers='block', locale='cs-CZ')

                    def route(r):
                        u = urlparse(r.request.url)
                        if u.scheme in ('http', 'https') and (u.hostname != '127.0.0.1' or u.port != httpd.server_port):
                            foreign.append(u.hostname)
                            r.abort()
                        else:
                            r.continue_()
                    ctx.route('**/*', route)
                    seed = {'classrally_lang': 'cs'}
                    if teacher:
                        seed['quiz_user_token'] = token
                    ctx.add_init_script('if (location.origin === ' + json.dumps(base) + ') Object.entries(' + json.dumps(seed) + ').forEach(([k,v])=>localStorage.setItem(k,v))')
                    ctx.on('page', lambda p: p.on('pageerror', lambda e: errors.append(str(e))))
                    return ctx

                # Actual editor save/reload, strict validation, JSON/CSV export/import.
                ctx = context(1200, teacher=True)
                admin = ctx.new_page()
                admin.goto(base + '/admin')
                admin.wait_for_load_state('networkidle')
                admin.evaluate("switchTab('editor')")
                admin.select_option('#editorBank', 'mobile.json')
                admin.click('#btnLoadBank')
                admin.locator('.q-time-sec').first.wait_for()
                assert admin.locator('.q-time-sec').first.input_value() == ''
                admin.locator('.q-time-sec').first.fill('47')
                admin.on('dialog', lambda d: d.accept(d.default_value) if d.type == 'prompt' else d.accept())
                with admin.expect_response(lambda r: '/api/admin/bank/save' in r.url) as saved:
                    admin.click('#btnSaveBank')
                assert saved.value.status == 200
                admin.click('#btnLoadBank')
                admin.wait_for_function("document.querySelector('.q-time-sec')?.value === '47'")
                # Invalid local input is reported and cannot silently become the default.
                admin.locator('.q-time-sec').first.fill('4')
                assert not admin.locator('.q-time-sec').first.evaluate('(el)=>el.checkValidity()')
                assert not admin.evaluate("document.querySelector('.q-time-sec').reportValidity()")
                admin.locator('.q-time-sec').first.fill('47')
                for fmt in ('Json', 'Csv'):
                    with admin.expect_download() as downloaded:
                        admin.click('#btnExport' + fmt)
                    content = Path(downloaded.value.path()).read_text()
                    if fmt == 'Json':
                        assert json.loads(content)[0]['time_sec'] == 47
                    else:
                        assert server.questions_from_csv(content)[0]['time_sec'] == 47
                    # Real import controls, not only serialization helpers.
                    with admin.expect_file_chooser() as chooser:
                        admin.click('#btnImport' + fmt)
                    with admin.expect_response(lambda r: '/api/admin/bank/' in r.url and r.request.method == 'POST') as imported:
                        chooser.value.set_files({'name': 'copy.' + fmt.lower(),
                                                 'mimeType': 'text/plain', 'buffer': content.encode()})
                    assert imported.value.status == 200
                    imported_name = imported.value.json()['filename']
                    assert server.load_questions_from_file(imported_name, teacher['id'])[0]['time_sec'] == 47
                checks += 1
                ctx.close()

                # Touch at 320/390 px and keyboard at desktop width; server mapping remains exact.
                for width in (320, 390, 1200):
                    question = dict(bank[0])
                    if width == 320:
                        question['items'] = ['velmi-dlouhá-položka' * 12, 'dva', 'tři', 'čtyři', 'pět']
                    if width == 1200:
                        question['time_sec'] = 47
                    server.QUIZ = server.QuizState([question, bank[1], dict(bank[0], id='o2', items=list('ABCDEFGH'))])
                    server.DEFAULT_ROOM.quiz = server.QUIZ
                    server.QUIZ.set_timing(question_sec=20, get_ready_sec=0)
                    ctx = context(width)
                    page = ctx.new_page()
                    page.goto(base + '/play')
                    page.fill('#nameInput', 'MobileTester')
                    page.click('#joinBtn')
                    page.wait_for_function("localStorage.getItem('quiz_player_id')")
                    server.QUIZ.host_action('start')
                    page.evaluate('refresh()')
                    page.locator('.ord-pick').first.wait_for()
                    limit = server.QUIZ.public_state()['question_time_sec']
                    assert page.evaluate('cd.total') == limit
                    host_ctx = context(1200, teacher=True)
                    host = host_ctx.new_page()
                    host.goto(base + '/host')
                    host.wait_for_function("/\\d/.test(document.querySelector('#kTimer').textContent)")
                    seconds = int(host.locator('#kTimer').inner_text().split()[0])
                    assert limit - 5 <= seconds <= limit, (limit, seconds)
                    host_ctx.close()
                    confirm = page.get_by_role('button', name='Potvrdit pořadí', exact=True)
                    assert confirm.is_disabled()
                    tap = lambda locator: locator.tap() if width < 600 else locator.press('Enter')
                    picks = page.locator('.ord-pick')
                    tap(picks.nth(2))
                    tap(picks.nth(0))
                    page.evaluate('refresh()')  # polling must retain the draft
                    assert page.locator('.ord-pick[aria-pressed="true"]').count() == 2
                    page.screenshot(path=str(qa / f'ordering-{width}.png'), full_page=True)
                    tap(page.get_by_role('button', name='Zpět', exact=True))
                    assert page.locator('.ord-pick[aria-pressed="true"]').count() == 1
                    tap(page.get_by_role('button', name='Vymazat', exact=True))
                    assert not page.locator('.ord-pick[aria-pressed="true"]').count()
                    for rect in picks.evaluate_all('(els)=>els.map(el=>({w:el.getBoundingClientRect().width,h:el.getBoundingClientRect().height}))'):
                        assert rect['w'] >= 44 and rect['h'] >= 44, rect
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    order = server.QUIZ._item_order_locked(0)
                    for original in range(5):
                        tap(picks.nth(order.index(original)))
                    assert not confirm.is_disabled()
                    # A failed request keeps the complete answer available for retry.
                    def fail_submit(r):
                        r.fulfill(status=503, content_type='application/json', body='{"error":"Test spojení"}')
                    page.route('**/api/submit', fail_submit)
                    page.on('dialog', lambda d: d.accept())
                    tap(confirm)
                    page.wait_for_function("!document.querySelector('.ord-confirm').disabled")
                    assert page.locator('.ord-pick[aria-pressed="true"]').count() == 5
                    page.unroute('**/api/submit', fail_submit)
                    tap(confirm)
                    page.wait_for_function("document.querySelectorAll('.ord-pick').length === 0")
                    pid = page.evaluate("localStorage.getItem('quiz_player_id')")
                    assert server.QUIZ.answers[pid]['choice'] == list(range(5))
                    server.QUIZ.host_action('reveal')
                    page.evaluate('refresh()')
                    assert page.locator('.ord-item.correct-pos').count() == 5
                    server.QUIZ.host_action('next')
                    page.evaluate('refresh()')
                    page.locator('.ms-btn').first.wait_for()
                    assert server.QUIZ.public_state()['question_time_sec'] == 32
                    assert not page.locator('.ord-pick').count()
                    server.QUIZ.host_action('next')
                    page.evaluate('refresh()')
                    page.wait_for_function("document.querySelectorAll('.ord-pick').length === 8")
                    assert not page.locator('.ord-pick[aria-pressed="true"]').count()
                    assert page.locator('.ord-confirm').is_disabled()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                    assert page.evaluate('cd.total') == 50
                    checks += 1
                    ctx.close()
                # Upgrade an installed PWA with stale cache-first translations.
                sw_ctx = browser.new_context(service_workers='allow')
                sw_ctx.on('page', lambda p: p.on('pageerror', lambda e: errors.append(str(e))))
                sw_page = sw_ctx.new_page()
                sw_page.goto(base + '/static/offline.html')
                sw_page.evaluate("""async () => {
                  await Promise.all((await navigator.serviceWorker.getRegistrations()).map(r=>r.unregister()));
                  const cache=await caches.open('classrally-v25');
                  await cache.put('/static/i18n/cs.json', new Response('{"play":{"order_tap_hint":"stale"}}'));
                  await navigator.serviceWorker.register('/static/sw.js');
                  await Promise.race([navigator.serviceWorker.ready,
                    new Promise((_, reject)=>setTimeout(()=>reject(new Error('SW readiness timeout')), 10000))]);
                }""")
                sw_page.wait_for_function('navigator.serviceWorker.controller !== null')
                sw_page.wait_for_function("""async () => {
                  const keys=await caches.keys();
                  return keys.includes('classrally-v26') && !keys.includes('classrally-v25');
                }""")
                data = sw_page.evaluate("fetch('/static/i18n/cs.json').then(r=>r.json())")
                assert data['play']['order_undo'] == 'Zpět'
                assert data['play']['order_tap_hint'] != 'stale'
                checks += 1
                sw_ctx.close()
                browser.close()
            assert not errors, errors
            assert not foreign, foreign
            print(f'PASS: {checks} editor/mobile/desktop scenarios; no JS errors or external HTTP requests')
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    run()
