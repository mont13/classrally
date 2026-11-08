#!/usr/bin/env python3
"""Local-only tour acceptance. Uses the installed Playwright, no downloads.

Run: omez-pamet -- python3 test_tour_browser.py
Accounts, banks and history use a disposable .cache directory. Only screenshots
are kept outside the repo (under the user's .cache); no real server is contacted.
"""
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
QA = ROOT / '.cache' / 'tour-qa'


def run():
    from playwright.sync_api import sync_playwright
    QA.mkdir(parents=True, exist_ok=True)
    shots = Path(tempfile.mkdtemp(prefix='classrally-tour-', dir=Path.home() / '.cache'))
    with tempfile.TemporaryDirectory(prefix='runtime-', dir=QA) as runtime:
        runtime = Path(runtime)
        os.environ.update(QUIZ_DATA_DIR=str(runtime / 'data'), QUIZ_HISTORY_DIR=str(runtime / 'history'),
                          QUIZ_QUESTIONS_DIR=str(runtime / 'questions'), QUIZ_UPLOADS_DIR=str(runtime / 'uploads'),
                          QUIZ_ROOMS='single', MAIL_MODE='off', QUIZ_GAME_LOG='0', BUGGY_URL='', BUGGY_PROJECT_KEY='',
                          BUGGY_WIDGET_DIR='', CLASSRALLY_PORTAL_SECRET='', QUIZ_BASE_URL='', AI_BASE_URL='http://127.0.0.1:1', AI_API_KEY='')
        for directory in ('questions', 'history', 'uploads'):
            (runtime / directory).mkdir()
        # A small local fixture, never a customer's bank or account.
        bank = [{'id': 'tour-1', 'prompt': 'Kolik je 2 + 2?', 'options': ['3', '4'], 'correct_index': 1},
                {'id': 'tour-2', 'prompt': 'Kolik je 3 + 3?', 'options': ['6', '7'], 'correct_index': 0}]
        (runtime / 'questions' / 'tour.json').write_text(json.dumps(bank), encoding='utf-8')
        import server
        server.user_db.PBKDF2_ITERATIONS = 1000
        server.user_db.init_db()
        teacher = server.user_db.create_user('TourTeacher', secrets.token_hex(12), 'teacher')
        pupil = server.user_db.create_user('TourPupil', secrets.token_hex(12), 'student')
        server.save_game_history(server.QUIZ)
        token = server.user_db.create_session(teacher['id'])
        pupil_token = server.user_db.create_session(pupil['id'])
        class Handler(server.Handler):
            def log_message(self, *args):
                pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        base = f'http://127.0.0.1:{httpd.server_port}'
        # Avoid LAN discovery, background tickers and CLI configuration entirely.
        server.SERVER_INFO.update(host_urls=[base + '/host'], play_urls=[base + '/play'], loopback_only=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        results, errors, foreign, mutations = [], [], [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=['--disable-background-networking', '--disable-component-update', '--disable-sync'])
                def context(viewport, *, authenticated=True, lang='cs', storage_blocked=False, account=None):
                    ctx = browser.new_context(viewport=viewport, locale='cs-CZ', service_workers='block')
                    def route(request_route):
                        u = urlparse(request_route.request.url)
                        if u.scheme in ('http', 'https') and (u.hostname != '127.0.0.1' or u.port != httpd.server_port):
                            foreign.append(u.hostname); request_route.abort(); return
                        if request_route.request.method not in ('GET', 'HEAD'):
                            mutations.append(u.path)
                        request_route.continue_()
                    ctx.route('**/*', route)
                    seed = {'classrally_lang': lang}
                    if authenticated: seed['quiz_user_token'] = account or token
                    setup = 'if (location.origin === ' + json.dumps(base) + ') { Object.entries(' + json.dumps(seed) + ').forEach(([k,v])=>localStorage.setItem(k,v));'
                    if storage_blocked:
                        setup += 'const tourBlockAllStorage = ' + ('true' if storage_blocked == 'all' else 'false') + ';'
                        setup += """const originalGet = Storage.prototype.getItem;
                        const originalSet = Storage.prototype.setItem;
                        const originalRemove = Storage.prototype.removeItem;
                        Storage.prototype.getItem = function(k) { if(tourBlockAllStorage || k.startsWith('classrally.tour.')) throw new DOMException('blocked','SecurityError'); return originalGet.call(this,k); };
                        Storage.prototype.setItem = function(k,v) { if(tourBlockAllStorage || k.startsWith('classrally.tour.')) throw new DOMException('blocked','SecurityError'); return originalSet.call(this,k,v); };
                        Storage.prototype.removeItem = function(k) { if(tourBlockAllStorage || k.startsWith('classrally.tour.')) throw new DOMException('blocked','SecurityError'); return originalRemove.call(this,k); };"""
                    # Playwright does not guarantee order between independent init scripts.
                    # Seed and storage faults must share one deterministic script.
                    ctx.add_init_script(setup + '}')
                    ctx.on('page', lambda p: p.on('pageerror', lambda e: errors.append(str(e))))
                    return ctx
                def ready(page, path='/admin'):
                    page.goto(base + path)
                    page.wait_for_selector('#cr-tour-launch', state='attached')
                    page.wait_for_load_state('networkidle')
                def inspect(page):
                    return page.evaluate("""() => {
                      const card=document.querySelector('#cr-tour-card'), ring=document.querySelector('#cr-tour-ring');
                      const id=card.dataset.step;
                      const step=ClassRallyTour.data.steps.find(s=>s.id===id);
                      const visible=e=>e && e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0 && !e.closest('[hidden],.hide,.hidden');
                      const target=step ? [...document.querySelectorAll(step.target)].find(visible) : null;
                      const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
                      return {id, card:rect(card), ring: ring.hidden?null:rect(ring), target:target?rect(target):null,
                        text:card.innerText, focus:card.contains(document.activeElement), viewport:{w:innerWidth,h:innerHeight}};
                    }""")
                def geometry(state):
                    card, target, ring, v = state['card'], state['target'], state['ring'], state['viewport']
                    assert 0 <= card['x'] and card['right'] <= v['w'] + 1, state
                    assert 0 <= card['y'] and card['bottom'] <= v['h'] + 1, state
                    assert state['focus'], ('focus', state['id'])
                    if ring:
                        assert target, state
                        overlap = min(card['right'], target['right']) - max(card['x'], target['x']) > .5 and min(card['bottom'], target['bottom']) - max(card['y'], target['y']) > .5
                        assert not overlap, ('card covers target', state)
                        assert abs(ring['x'] - max(0, target['x'] - 5)) <= 1, state
                        assert abs(ring['y'] - max(0, target['y'] - 5)) <= 1, state
                        assert abs(ring['right'] - min(v['w'], target['right'] + 5)) <= 1, state
                        assert abs(ring['bottom'] - min(v['h'], target['bottom'] + 5)) <= 1, state
                def walk(page, guide, size):
                    page.locator('#cr-tour-launch').click()
                    page.locator(f'[data-tour-guide="{guide}"]').click()
                    seen = []
                    before = len(mutations)
                    for count in range(30):
                        page.wait_for_selector('#cr-tour-card', state='visible')
                        page.wait_for_function("document.querySelector('.cr-tour-never') || document.querySelector('#cr-tour-card .cr-tour-actions')")
                        page.wait_for_timeout(100)
                        if page.locator('.cr-tour-never input').count():
                            assert seen, guide
                            page.locator('#cr-tour-card button').last.click()
                            break
                        state = inspect(page); geometry(state)
                        identity = state['id']
                        assert identity not in seen, (guide, seen, state)
                        seen.append(identity)
                        page.screenshot(path=str(shots / f'{size}-{guide}-{identity}.png'))
                        # Recompute when the viewport or scroll position changes.
                        if count == 0:
                            page.evaluate('window.scrollBy(0, 25)'); page.wait_for_timeout(80); geometry(inspect(page))
                        old_url = page.url
                        page.locator('#cr-tour-card .cr-tour-actions button').nth(1).click()
                        page.wait_for_function("""old => location.href !== old.url ||
                          document.querySelector('.cr-tour-never') || document.querySelector('#cr-tour-card').dataset.step !== old.id""",
                          arg={'url': old_url, 'id': identity})
                    else:
                        raise AssertionError(('tour did not finish', guide, seen))
                    assert len(mutations) == before, ('tour mutated application state', mutations[before:])
                    if guide == 'quiz' and not size.startswith('storage'):
                        assert page.evaluate("JSON.parse(localStorage.getItem('classrally.tour.page.v1:/admin')).completed")
                        assert page.evaluate("JSON.parse(localStorage.getItem('classrally.tour.page.v1:/host')).completed")
                    results.append({'viewport': size, 'guide': guide, 'steps': seen})
                    print(f'PASS {size}/{guide}: {len(seen)} steps', flush=True)
                for name, viewport in [('desktop', {'width': 1440, 'height': 900}), ('mobile', {'width': 390, 'height': 844})]:
                    for guide in ['quiz', 'questions', 'results', 'homework', 'exam', 'classes', 'join', 'music', 'language', 'bug']:
                        ctx = context(viewport)
                        page = ctx.new_page(); ready(page)
                        walk(page, guide, name)
                        ctx.close()
                # Entry pages retain their own language/Buggy tours; exam join has real targets.
                for path in ('/', '/profile'):
                    ctx = context({'width': 390, 'height': 844}, account=pupil_token)
                    page = ctx.new_page(); ready(page, path)
                    if path == '/profile': walk(page, 'classes', 'pupil-profile')
                    walk(page, 'language', 'entry-' + path.strip('/'))
                    assert urlparse(page.url).path == path
                    ctx.close()
                server.DEFAULT_ROOM.mode = 'exam'
                for viewport in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}):
                    ctx = context(viewport, authenticated=False); page = ctx.new_page(); ready(page, '/exam')
                    walk(page, 'join', 'exam-' + str(viewport['width']))
                    for guide in ('language', 'bug'):
                        walk(page, guide, 'exam-' + str(viewport['width']))
                        assert urlparse(page.url).path == '/exam'
                    ctx.close()
                server.DEFAULT_ROOM.mode = 'game'
                # Resizing and long translated text keep the card clear of a QR image.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page, '/play')
                page.evaluate("ClassRallyTour.start('join')"); page.wait_for_selector('[data-step="joinqr"]')
                page.set_viewport_size({'width': 844, 'height': 390}); page.wait_for_timeout(180); geometry(inspect(page))
                page.set_viewport_size({'width': 390, 'height': 844})
                for lang in ('de', 'uk', 'en', 'sk', 'cs'):
                    page.evaluate('(lang)=>i18n.setLang(lang)', lang); page.wait_for_timeout(120); geometry(inspect(page))
                page.keyboard.press('Escape'); ctx.close()
                # A structural edit with no input event is also protected before page navigation.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page)
                page.locator('[data-tab="editor"]').click(); page.locator('#btnAddQuestion').click()
                rejected_navigation = []
                def reject_navigation(dialog):
                    rejected_navigation.append(dialog.message); dialog.dismiss()
                page.once('dialog', reject_navigation)
                page.evaluate("ClassRallyTour.start('quiz')")
                assert len(rejected_navigation) == 1
                page.wait_for_timeout(200); assert urlparse(page.url).path == '/admin'
                page.keyboard.press('Escape'); ctx.close()
                # Reordering renders new nodes before dragend; capture must guard it too.
                ctx = context({'width': 1440, 'height': 900}); page = ctx.new_page(); ready(page)
                page.locator('[data-tab="editor"]').click(); page.locator('#btnLoadBank').click()
                page.wait_for_selector('#questionEditor .q-editor:nth-child(2)')
                page.evaluate('''() => {
                  const source=document.querySelector('#questionEditor .q-editor');
                  const dest=source.nextElementSibling, dataTransfer=new DataTransfer();
                  source.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer}));
                  dest.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer,clientY:dest.getBoundingClientRect().bottom-2}));
                }''')
                rejected_navigation.clear(); page.once('dialog', reject_navigation)
                page.evaluate("ClassRallyTour.start('quiz')")
                assert len(rejected_navigation) == 1
                assert urlparse(page.url).path == '/admin'; ctx.close()
                # Keyboard, completion state and opt-out remain available on repeat visits.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page)
                page.evaluate("ClassRallyTour.start('questions')")
                page.wait_for_selector('[data-step="create"]')
                page.keyboard.press('ArrowRight'); page.wait_for_selector('[data-step="editor"]')
                page.keyboard.press('ArrowLeft'); page.wait_for_selector('[data-step="create"]')
                page.keyboard.press('Escape'); assert not page.locator('#cr-tour-card').is_visible()
                page.evaluate("ClassRallyTour.start('language')"); page.wait_for_selector('[data-step="language"]')
                page.keyboard.press('ArrowRight'); page.wait_for_selector('.cr-tour-never input')
                page.locator('.cr-tour-never input').check()
                assert page.evaluate("JSON.parse(localStorage.getItem('classrally.tour.page.v1:/admin')).never")
                page.reload(); page.wait_for_timeout(1600)
                assert not page.locator('#cr-tour-offer').count()
                page.locator('#cr-tour-launch').click(); assert page.locator('[data-tour-guide="quiz"]').is_visible()
                page.keyboard.press('Escape'); ctx.close()
                # The gentle first-visit offer never takes focus; a dismissal is per page.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page, '/play')
                page.wait_for_selector('#cr-tour-offer'); assert not page.locator('#cr-tour-card').is_visible()
                page.get_by_role('button', name='Už mi průvodce nenabízet', exact=True).click()
                page.reload(); page.wait_for_timeout(1600); assert not page.locator('#cr-tour-offer').count()
                ready(page, '/host'); page.wait_for_selector('#cr-tour-offer'); ctx.close()
                # Unavailable localStorage also leaves i18n and the engine usable.
                ctx = context({'width': 390, 'height': 844}, authenticated=False, storage_blocked='all'); page = ctx.new_page(); ready(page, '/')
                walk(page, 'language', 'all-storage-blocked'); ctx.close()
                # Storage-free navigation resumes using a validated fragment.
                ctx = context({'width': 390, 'height': 844}, storage_blocked=True); page = ctx.new_page(); ready(page)
                walk(page, 'quiz', 'storage-blocked'); ctx.close()
                # Missing targets and conditionals, Buggy's optional runtime button, live i18n.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page)
                page.evaluate("document.querySelector('#cfgOllamaModel').value = ''")
                walk(page, 'questions', 'ai-unconfigured')
                page.evaluate("""const root=document.createElement('div');root.id='classrally-buggy-root';
                  root.innerHTML='<button style="position:fixed;bottom:18px;right:18px;width:56px;height:56px">Buggy</button>';document.body.append(root);""")
                page.evaluate("ClassRallyTour.start('bug')"); page.wait_for_selector('[data-step="bug"]'); page.wait_for_timeout(150)
                geometry(inspect(page)); assert page.locator('#cr-tour-ring').is_visible()
                page.screenshot(path=str(shots / 'mobile-buggy-enabled.png'))
                page.keyboard.press('Escape')
                page.evaluate("ClassRallyTour.start('language')"); page.wait_for_selector('[data-step="language"]')
                for lang in ('en', 'de', 'sk', 'uk', 'cs'):
                    page.evaluate('(lang) => i18n.setLang(lang)', lang); page.wait_for_timeout(100)
                    assert 'tour.' not in page.locator('#cr-tour-card').inner_text()
                    geometry(inspect(page))
                page.keyboard.press('Escape'); ctx.close()
                # Portal login is optional, and visible only on the login screen.
                os.environ['CLASSRALLY_PORTAL_SECRET'] = secrets.token_urlsafe(40)
                os.environ['CLASSRALLY_PORTAL_LOGIN_URL'] = base + '/profile'
                ctx = context({'width': 390, 'height': 844}, authenticated=False); page = ctx.new_page(); ready(page, '/profile')
                walk(page, 'classes', 'portal-enabled'); ctx.close()
                os.environ['CLASSRALLY_PORTAL_SECRET'] = ''
                # Restoring a cached page repeatedly must leave exactly one phase timer.
                ctx = context({'width': 390, 'height': 844}, authenticated=False); page = ctx.new_page(); ready(page, '/play')
                phase_requests = []
                page.on('request', lambda request: phase_requests.append(request.url) if urlparse(request.url).path == '/api/state' else None)
                for _ in range(3):
                    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide')); window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));")
                    page.wait_for_timeout(250)
                before = len(phase_requests); page.wait_for_timeout(2200)
                assert len(phase_requests) - before == 1, ('duplicate phase timers', phase_requests)
                ctx.close()
                # PIN is actually served in multi-room mode, not a DOM mock.
                server.ROOM_MODE = 'multi'
                ctx = context({'width': 390, 'height': 844}, authenticated=False); page = ctx.new_page(); ready(page, '/play')
                walk(page, 'join', 'multi-room'); ctx.close()
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page)
                walk(page, 'quiz', 'multi-room-teacher'); ctx.close()
                server.ROOM_MODE = 'single'
                # Back never underflows when preceding targets have been skipped.
                ctx = context({'width': 390, 'height': 844}, account=pupil_token); page = ctx.new_page(); ready(page, '/play')
                page.evaluate("['qrImg','nameInput','joinBtn'].forEach(id=>document.getElementById(id).hidden=true); ClassRallyTour.start('join')")
                page.wait_for_selector('[data-step="signed"]')
                assert page.locator('#cr-tour-card .cr-tour-actions button').first.is_disabled()
                page.keyboard.press('ArrowLeft'); page.wait_for_timeout(150); assert page.locator('[data-step="signed"]').is_visible()
                page.keyboard.press('Escape'); ctx.close()
                # A game transition while awaiting an optional target cancels the pending step.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page, '/play')
                page.evaluate("ClassRallyTour.start('bug'); document.body.dataset.phase='question'")
                page.wait_for_timeout(450); assert not page.locator('#cr-tour-card').is_visible(); ctx.close()
                # The login gate must allow reaching the real login form with Tab.
                ctx = context({'width': 390, 'height': 844}, authenticated=False); page = ctx.new_page(); ready(page)
                page.evaluate("ClassRallyTour.start('questions')"); page.wait_for_function("document.querySelector('#cr-tour-title').textContent === 'Nejprve se přihlaste'")
                for _ in range(20):
                    page.keyboard.press('Shift+Tab')
                    if page.evaluate("document.activeElement.id === 'loginNick'"): break
                else: raise AssertionError('login focus trapped in tour')
                page.keyboard.press('Escape'); ctx.close()
                # Real player, real question: the tour closes and its launcher/offer disappear.
                ctx = context({'width': 390, 'height': 844}, account=pupil_token); page = ctx.new_page(); ready(page, '/play')
                page.locator('#nameInput').fill('TourPlayer'); page.locator('#joinBtn').click()
                page.wait_for_selector('#gameCard', state='visible')
                page.evaluate("ClassRallyTour.start('join')"); page.wait_for_selector('.cr-tour-never input')
                page.keyboard.press('Escape')
                page.evaluate("ClassRallyTour.start('language')"); page.wait_for_selector('[data-step="studentlanguage"]')
                observer_ctx = context({'width': 390, 'height': 844}, authenticated=False)
                observer_page = observer_ctx.new_page(); ready(observer_page, '/play')
                observer_page.wait_for_selector('#cr-tour-offer')
                response = ctx.request.post(base + '/api/host/action', data={'action': 'start'}, headers={'Authorization': 'Bearer ' + server.HOST_TOKEN})
                assert response.ok
                # No ticker is needed for this edge: the player's normal polling sees it.
                page.wait_for_function("document.body.dataset.phase === 'question'")
                assert not page.locator('#cr-tour-card').is_visible()
                assert not page.locator('#cr-tour-launch').is_visible()
                assert not page.locator('#cr-tour-offer').is_visible()
                page.locator('#options').wait_for(state='visible')
                page.screenshot(path=str(shots / 'mobile-question-no-tour.png')); ctx.close()
                observer_page.wait_for_selector('#cr-tour-launch', state='hidden')
                assert not observer_page.locator('#cr-tour-offer').is_visible()
                observer_page.evaluate("ClassRallyTour.start('language')")
                assert not observer_page.locator('#cr-tour-card').is_visible()
                observer_ctx.close()
                # New join-page visits during the same question must not offer a tour.
                ctx = context({'width': 390, 'height': 844}, authenticated=False); page = ctx.new_page(); ready(page, '/play')
                page.wait_for_timeout(1600); assert not page.locator('#cr-tour-offer').count(); ctx.close()
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page, '/host')
                page.wait_for_timeout(1600); assert not page.locator('#cr-tour-offer').count(); ctx.close()
                browser.close()
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)
        assert not errors, errors
        assert not foreign, ('non-local requests attempted', foreign)
        report = {'status': 'PASS', 'catalog_runs': len(results), 'runs': results, 'js_errors': errors,
                  'foreign_requests': foreign, 'screenshots': str(shots)}
        (QA / 'browser-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': 'PASS', 'catalog_runs': len(results), 'js_errors': len(errors), 'foreign_requests': len(foreign), 'screenshots': str(shots)}, ensure_ascii=False))


if __name__ == '__main__':
    run()
