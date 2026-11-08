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
        results, errors, foreign, mutations, state_checks, offer_checks, student_checks = [], [], [], [], [], [], []
        launcher_checks, game_checks = [], []
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
                    launcher_geometry(page, 'ready:' + path)
                def launcher_geometry(page, label, *, hidden=None):
                    # Independent DOM measurement: include custom click targets,
                    # QR images and the Buggy fixture, not just native buttons.
                    # Restarting homework reloads the document; wait for i18n
                    # and tour initialization before measuring the new page.
                    page.locator('#cr-tour-launch').wait_for(state='attached')
                    probe = """() => {
                      const launch=document.querySelector('#cr-tour-launch');
                      const visible=e=>e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0 &&
                        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                      if(!visible(launch)) return {hidden:true,collisions:[]};
                      const a=launch.getBoundingClientRect(), collisions=[];
                      for(const e of document.body.querySelectorAll('*')) {
                        if(e.closest('[id^="cr-tour"],.cr-tour-shade') || !visible(e)) continue;
                        const s=getComputedStyle(e);
                        if(s.pointerEvents==='none' || !(e.matches('button,a[href],input:not([type="hidden"]),select,textarea,summary,[role="button"],[role="link"],[tabindex],[onclick]') || s.cursor==='pointer')) continue;
                        const b=e.getBoundingClientRect();
                        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>.5 &&
                           Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>.5)
                          collisions.push({id:e.id,tag:e.tagName,rect:{x:b.x,y:b.y,w:b.width,h:b.height}});
                      }
                      return {hidden:false,rect:{x:a.x,y:a.y,w:a.width,h:a.height},collisions};
                    }"""
                    page.wait_for_function('(' + probe + ')().collisions.length === 0')
                    if hidden is not None:
                        try:
                            page.locator('#cr-tour-launch').wait_for(state='hidden' if hidden else 'visible')
                        except Exception:
                            print('FAIL launcher availability: ' + label + ' ' + json.dumps(page.evaluate(probe)), flush=True)
                            page.screenshot(path=str(shots / 'launcher-failure.png'))
                            raise
                    state = page.evaluate(probe)
                    assert not state['collisions'], (label, state)
                    assert hidden is None or state['hidden'] == hidden, (label, state)
                    launcher_checks.append({'label': label, 'viewport': page.viewport_size, **state})
                def overlay_roundtrip(page, label, selector, open_overlay, close_overlay):
                    launcher_geometry(page, label + ':before', hidden=False)
                    open_overlay()
                    page.locator(selector).wait_for(state='visible')
                    launcher_geometry(page, label + ':open', hidden=True)
                    # A running guide must not be started above the page dialog.
                    page.evaluate("ClassRallyTour.start('language')")
                    assert not page.locator('#cr-tour-card').is_visible(), label
                    close_overlay()
                    # Podiums close by fading opacity to zero, retaining their
                    # layout box (Playwright's `hidden` does not cover that).
                    page.wait_for_function("""sel => {
                      const el=document.querySelector(sel);
                      return !el || !el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                    }""", arg=selector)
                    launcher_geometry(page, label + ':closed', hidden=False)
                def student_catalog(page, label, *, wait=True):
                    if wait: page.wait_for_timeout(1600)
                    assert not page.locator('#cr-tour-offer').count(), ('student auto-offer', label)
                    assert not page.locator('#cr-tour-card').is_visible(), ('unsolicited dialog', label)
                    assert not page.evaluate("JSON.parse(localStorage.getItem('classrally.tour.page.v1:' + location.pathname))?.offered"), label
                    # CSS's visible '?' may prefix the accessible text as well.
                    launcher = page.get_by_role('button', name='Průvodce')
                    assert launcher.is_visible(), ('manual launcher unavailable', label)
                    if page.viewport_size['width'] <= 600:
                        rect = launcher.bounding_box()
                        assert rect['width'] == 44 and rect['height'] == 44, ('mobile launcher', label, rect)
                    launcher.click()
                    assert page.locator('#cr-tour-title').inner_text() == page.evaluate("i18n.t('tour.studentCatalog')"), label
                    guides = page.locator('[data-tour-guide]').evaluate_all('(items) => items.map(item => item.dataset.tourGuide)')
                    assert set(guides) == {'join', 'classes', 'language', 'bug'}, (label, guides)
                    page.locator('[data-tour-guide="language"]').click()
                    page.wait_for_selector('[data-step="studentlanguage"]')
                    page.keyboard.press('Escape')
                    student_checks.append({'page': label, 'viewport': page.viewport_size, 'guides': guides, 'auto_offer': False})
                def teacher_offer_geometry(page, label, selectors):
                    offer = page.locator('#cr-tour-offer')
                    offer.wait_for(state='visible')
                    page.evaluate('window.scrollTo(0, 0)')
                    rect = offer.bounding_box()
                    assert 0 <= rect['y'] and rect['y'] + rect['height'] <= page.viewport_size['height'], (label, rect)
                    assert page.evaluate("!document.querySelector('#cr-tour-card').contains(document.activeElement) && !document.querySelector('#cr-tour-offer').contains(document.activeElement)"), label
                    checks = []
                    for selector in selectors:
                        for control in page.locator(selector).all():
                            assert control.is_visible(), (label, 'protected control not visible', selector)
                            control.scroll_into_view_if_needed()
                            a, b = offer.bounding_box(), control.bounding_box()
                            overlap = min(a['x'] + a['width'], b['x'] + b['width']) - max(a['x'], b['x']) > .5 and min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y']) > .5
                            assert not overlap, ('offer covers teacher control', label, selector, a, b)
                            checks.append({'control': selector, 'offer': a, 'target': b, 'overlap': overlap})
                    offer_checks.append({'page': label, 'viewport': page.viewport_size, 'checks': checks})
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path=str(shots / (label + '-offer.png')))
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
                # Full game, real teacher room + separate player's browser. The
                # host's normal buttons drive both questions through the podium.
                viewports = [(1440, 900), (1280, 720), (1024, 768), (390, 844), (844, 390)]
                server.save_wifi_settings({'ssid': 'Tour QA local fixture', 'password': ''})
                original_laptop = server.LAPTOP_MODE
                server.LAPTOP_MODE = True
                for room_mode in ('single', 'multi'):
                    server.ROOM_MODE = room_mode
                    room = server.DEFAULT_ROOM if room_mode == 'single' else server.ROOMS.for_owner(teacher['id'])
                    for width, height in viewports:
                        viewport = {'width': width, 'height': height}
                        label = f'{room_mode}-{width}x{height}'
                        room.mode = 'game'
                        room.quiz.reload_questions(bank, 'tour.json')
                        room.quiz.get_ready_sec = 0
                        room.quiz.question_duration_sec = 300
                        room.quiz.reveal_duration_sec = 300
                        host_ctx = context(viewport); host = host_ctx.new_page(); ready(host, '/host')
                        phone_ctx = context(viewport, authenticated=False)
                        phone = phone_ctx.new_page(); ready(phone, '/play?pin=' + room.pin)
                        phone.locator('#nameInput').fill('Tour-' + label)
                        phone.locator('#joinBtn').click(); phone.locator('#gameCard').wait_for(state='visible')
                        launcher_geometry(host, label + ':host-lobby', hidden=False)
                        launcher_geometry(phone, label + ':phone-lobby', hidden=False)
                        overlay_roundtrip(host, label + ':QR', '#qrOverlay',
                                          lambda: host.locator('#qrImg').click(),
                                          lambda: host.locator('#qrOverlay').click(position={'x': 8, 'y': 8}))
                        if room_mode == 'single':
                            host.locator('#wifiSettings summary').click()
                            host.locator('#showWifiQr').check()
                            host.locator('#wifiQrImg').wait_for(state='visible')
                            overlay_roundtrip(host, label + ':WiFi-QR', '#qrOverlay',
                                              lambda: host.locator('#wifiQrImg').click(),
                                              lambda: host.locator('#qrOverlay').click(position={'x': 8, 'y': 8}))
                        host.locator('#btnStart').click()
                        for question in range(len(bank)):
                            phone.wait_for_function("document.body.dataset.phase === 'question'")
                            host.wait_for_function("document.body.classList.contains('in-game')")
                            launcher_geometry(host, label + f':host-question-{question}', hidden=True)
                            launcher_geometry(phone, label + f':phone-question-{question}', hidden=True)
                            # Cover both teacher reveal and a real pupil answer.
                            answer = bank[question]['options'][bank[question]['correct_index']]
                            with phone.expect_response(lambda response: urlparse(response.url).path == '/api/submit') as submission:
                                phone.locator('#options button').filter(has_text=answer).click()
                            assert submission.value.ok, submission.value.status
                            # This embedded server intentionally has no ticker;
                            # reveal explicitly if the answer has not ended it.
                            if room.quiz.phase == 'question': host.locator('#btnReveal').click()
                            phone.wait_for_function("document.body.dataset.phase === 'reveal'")
                            launcher_geometry(host, label + f':host-reveal-{question}', hidden=True)
                            launcher_geometry(phone, label + f':phone-reveal-{question}', hidden=True)
                            host.locator('#btnNext').click()
                        phone.wait_for_function("document.body.dataset.phase === 'finished'")
                        phone.locator('#finishCard').wait_for(state='visible')
                        host.locator('#podiumOverlay.visible').wait_for(state='visible')
                        launcher_geometry(phone, label + ':phone-finished', hidden=True)
                        launcher_geometry(host, label + ':podium', hidden=True)
                        host.locator('#podiumAwards .award-card').first.wait_for(state='visible')
                        launcher_geometry(host, label + ':awards', hidden=True)
                        launcher_geometry(phone, label + ':phone-awards', hidden=True)
                        host.screenshot(path=str(shots / (label + '-podium.png')))
                        # Never force clicks: this is the original regression.
                        host.locator('#podiumClose').click()
                        launcher_geometry(host, label + ':podium-closed', hidden=False)
                        overlay_roundtrip(host, label + ':analysis', '#analysisOverlay',
                                          lambda: host.locator('#btnAnalysis').click(),
                                          lambda: host.locator('#btnAnalysisClose').click())
                        if width == 390:
                            overlay_roundtrip(host, label + ':team-podium-fixture', '#teamPodiumOverlay',
                                              lambda: host.evaluate("document.querySelector('#teamPodiumOverlay').classList.add('visible')"),
                                              lambda: host.locator('#teamPodiumClose').click())
                        host.once('dialog', lambda dialog: dialog.accept())
                        host.locator('#btnReset').click()
                        phone.wait_for_function("document.body.dataset.phase === 'lobby'")
                        launcher_geometry(host, label + ':host-reset', hidden=False)
                        launcher_geometry(phone, label + ':phone-reset', hidden=False)
                        host_ctx.close(); phone_ctx.close()
                        game_checks.append({'mode': room_mode, 'viewport': viewport, 'questions': len(bank),
                                            'podium_close': True, 'analysis_open_close': True, 'awards': True})
                        print(f'PASS full game {label}: lobby/QR/question/reveal/podium/analysis/reset', flush=True)
                server.ROOM_MODE = 'single'
                server.LAPTOP_MODE = original_laptop
                # Actual page dialogs and future overlays: styles, insertion,
                # native dialog open/close and viewport/scroll changes.
                for width, height in viewports:
                    ctx = context({'width': width, 'height': height}); page = ctx.new_page()
                    for path, selector, opening, close_selector in (
                        ('/admin', '#analysisOverlay', "showAnalysis('Tour QA', {items:[],students:0})", '#analysisClose'),
                        ('/profile', '#qrOverlay', "showQrModal({name:'Tour QA',join_code:'LOCALQA'})", '#btnQrClose'),
                        ('/exam', '#antiCheatOverlay', "document.querySelector('#antiCheatOverlay').classList.remove('hide')", None),
                    ):
                        server.DEFAULT_ROOM.mode = 'exam' if path == '/exam' else 'game'
                        ready(page, path)
                        overlay_roundtrip(page, f'{width}x{height}{path}', selector,
                                          lambda script=opening: page.evaluate(script),
                                          lambda sel=close_selector, overlay=selector: page.locator(sel).click() if sel else
                                              page.evaluate('(sel)=>document.querySelector(sel).classList.add("hide")', overlay))
                    server.DEFAULT_ROOM.mode = 'game'
                    ready(page, '/admin')
                    page.evaluate("""() => {
                      const layer=document.createElement('div'); layer.id='qa-future-layer';
                      layer.style.cssText='position:fixed;inset:0;z-index:200;background:white;display:none';
                      const close=document.createElement('button'); close.textContent='Close';
                      close.onclick=()=>layer.style.display='none'; layer.append(close); document.body.append(layer);
                    }""")
                    overlay_roundtrip(page, f'{width}:unnamed-fixed-overlay', '#qa-future-layer',
                                      lambda: page.evaluate("document.querySelector('#qa-future-layer').style.display='block'"),
                                      lambda: page.locator('#qa-future-layer button').click())
                    page.evaluate("document.querySelector('#qa-future-layer').remove()")
                    launcher_geometry(page, f'{width}:removed-overlay', hidden=False)
                    page.evaluate("""() => {
                      const dialog=document.createElement('dialog'); dialog.id='qa-dialog';
                      const close=document.createElement('button'); close.textContent='Close';
                      close.onclick=()=>dialog.close(); dialog.append(close);
                      document.body.append(dialog); dialog.showModal();
                    }""")
                    launcher_geometry(page, f'{width}:native-dialog', hidden=True)
                    page.locator('#qa-dialog button').click()
                    launcher_geometry(page, f'{width}:native-dialog-closed', hidden=False)
                    # Simulate the optional offline Buggy button and a control
                    # placed at the launcher's old position, without its service.
                    page.evaluate("""() => {
                      const a=document.querySelector('#cr-tour-launch').getBoundingClientRect();
                      const button=document.createElement('button'); button.id='qa-corner-control'; button.textContent='Page control';
                      button.style.cssText=`position:fixed;right:8px;top:${a.top}px;width:160px;height:44px;z-index:200`;
                      document.body.append(button);
                      const buggy=document.createElement('button'); buggy.id='qa-buggy'; buggy.textContent='Buggy';
                      buggy.style.cssText='position:fixed;right:8px;bottom:8px;width:100px;height:44px;z-index:200';
                      document.body.append(buggy);
                    }""")
                    launcher_geometry(page, f'{width}:corner-controls')
                    page.locator('#qa-corner-control').click(); page.locator('#qa-buggy').click()
                    page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    launcher_geometry(page, f'{width}:scrolled-corner-controls')
                    page.set_viewport_size({'width': 844, 'height': 390})
                    launcher_geometry(page, f'{width}:resized-corner-controls')
                    page.evaluate("document.querySelector('#qa-corner-control').remove(); document.querySelector('#qa-buggy').remove()")
                    launcher_geometry(page, f'{width}:controls-removed', hidden=False)
                    page.locator('#cr-tour-launch').click()
                    page.keyboard.press('Escape')
                    page.wait_for_function("document.activeElement.id === 'cr-tour-launch'")
                    if width == 1440:
                        page.evaluate("ClassRallyTour.start('language')")
                        page.wait_for_selector('[data-step="language"]')
                        page.evaluate("showAnalysis('Tour QA', {items:[],students:0})")
                        page.locator('#cr-tour-card').wait_for(state='hidden')
                        launcher_geometry(page, 'dialog-interrupts-active-tour', hidden=True)
                        page.locator('#analysisClose').click()
                        launcher_geometry(page, 'interrupted-tour-dialog-closed', hidden=False)
                    ctx.close()
                # Keep the timer fallback and selective, nested layer detection
                # covered independently of Chromium's requestIdleCallback path.
                for fallback in (False, True):
                    ctx = context({'width': 390, 'height': 844})
                    if fallback:
                        ctx.add_init_script('window.requestIdleCallback=undefined; window.cancelIdleCallback=undefined;')
                    page = ctx.new_page(); ready(page, '/admin')
                    page.add_style_tag(content='.qa-sheet {position:fixed;inset:0;z-index:4000;background:white}')
                    page.evaluate("""() => {
                      const holder=document.createElement('div'); holder.id='qa-layer-holder';
                      const layer=document.createElement('div'); layer.className='qa-sheet'; layer.hidden=true;
                      const close=document.createElement('button'); close.textContent='Close';
                      close.onclick=()=>layer.hidden=true; layer.append(close); holder.append(layer);
                      document.querySelector('main').append(holder);
                    }""")
                    overlay_roundtrip(page, f'idle-fallback-{fallback}:nested-css-layer', '.qa-sheet',
                                      lambda: page.evaluate("document.querySelector('.qa-sheet').hidden=false"),
                                      lambda: page.locator('.qa-sheet button').click())
                    page.evaluate("document.querySelector('.qa-sheet button').remove()")
                    overlay_roundtrip(page, f'idle-fallback-{fallback}:empty-css-layer', '.qa-sheet',
                                      lambda: page.evaluate("document.querySelector('.qa-sheet').hidden=false"),
                                      lambda: page.evaluate("document.querySelector('.qa-sheet').hidden=true"))
                    page.locator('#cr-tour-launch').click()
                    page.keyboard.press('Escape')
                    page.wait_for_function("document.activeElement.id === 'cr-tour-launch'")
                    launcher_geometry(page, f'idle-fallback-{fallback}:focus-restored', hidden=False)
                    ctx.close()
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
                # Student entry pages never offer automatically, including fresh
                # visits, reload, bfcache, translations and resized viewports.
                for viewport in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}):
                    for path in ('/play', '/profile', '/exam'):
                        server.DEFAULT_ROOM.mode = 'exam' if path == '/exam' else 'game'
                        ctx = context(viewport, account=pupil_token)
                        page = ctx.new_page(); ready(page, path)
                        label = str(viewport['width']) + path
                        student_catalog(page, label)
                        page.screenshot(path=str(shots / (str(viewport['width']) + '-' + path[1:] + '-manual-only.png')))
                        page.reload(); page.wait_for_load_state('networkidle')
                        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true})); i18n.setLang('en')")
                        page.wait_for_timeout(1600)
                        assert not page.locator('#cr-tour-offer').count(), label
                        page.evaluate("i18n.setLang('cs')")
                        page.wait_for_function("document.querySelector('#cr-tour-launch').textContent === 'Průvodce'")
                        student_catalog(page, label + '-reload-bfcache-language', wait=False)
                        ctx.close()
                server.DEFAULT_ROOM.mode = 'game'
                # Teacher offers reserve page space and stay clear while scrolling.
                # Multi-room mode exposes the actual PIN, alongside the real QR.
                for room_mode in ('single', 'multi'):
                    server.ROOM_MODE = room_mode
                    try:
                        for viewport in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}):
                            for path in ('/host', '/admin'):
                                ctx = context(viewport); page = ctx.new_page(); ready(page, path)
                                selectors = ['#btnStart', '#qrImg'] if path == '/host' else ['.tabs .tab']
                                if path == '/host' and room_mode == 'multi': selectors.append('#roomPin')
                                label = path[1:] + '-' + room_mode + '-' + str(viewport['width'])
                                teacher_offer_geometry(page, label, selectors)
                                # Keep the offer open across a live resize too.
                                page.set_viewport_size({'width': 390, 'height': 844} if viewport['width'] == 1440 else {'width': 1440, 'height': 900})
                                teacher_offer_geometry(page, label + '-resized', selectors)
                                ctx.close()
                    finally:
                        server.ROOM_MODE = 'single'
                # The gentle teacher offer never takes focus; dismissal is per page.
                ctx = context({'width': 390, 'height': 844}); page = ctx.new_page(); ready(page, '/admin')
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
                # Observe from BEFORE navigation, including the initial request.
                # Every page stays open for at least 10 s; invalid/no PIN must
                # never produce /api/state (400 in multi mode). Complete PINs
                # also need no request: the tour is entirely local.
                for room_mode in ('single', 'multi'):
                    server.ROOM_MODE = room_mode
                    samples = []
                    try:
                        for query in ('', '?pin=', '?pin=123', '?pin=12a456', '?pin=000000', '?pin=' + server.DEFAULT_ROOM.pin):
                            ctx = context({'width': 390, 'height': 844}, authenticated=False)
                            page = ctx.new_page()
                            requests, console_errors = [], []
                            page.on('request', lambda request, seen=requests: seen.append(request.url)
                                    if urlparse(request.url).path == '/api/state' else None)
                            page.on('console', lambda msg, seen=console_errors: seen.append(msg.text) if msg.type == 'error' else None)
                            ready(page, '/play' + query)
                            started = time.monotonic()
                            samples.append((ctx, page, query, requests, console_errors, started))
                            if not query and room_mode == 'multi':
                                page.locator('#pinInput').fill('12')
                                page.locator('#pinInput').fill(server.DEFAULT_ROOM.pin)
                            for _ in range(3):
                                page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide')); window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));")
                        page.wait_for_timeout(10000)
                        for _, observed, query, requests, console_errors, started in samples:
                            assert not requests, (room_mode, query, 'unexpected state request', requests)
                            assert not console_errors, (room_mode, query, console_errors)
                            student_catalog(observed, room_mode + '/play' + query, wait=False)
                            state_checks.append({'mode': room_mode, 'query': query, 'seconds': round(time.monotonic() - started, 2),
                                                 'state_requests': len(requests), 'console_errors': console_errors})
                        print(f'PASS {room_mode}: 6 join pages, >=10 s each, 0 state requests (including bfcache and PIN edits)', flush=True)
                    finally:
                        for ctx, *_ in samples: ctx.close()
                        server.ROOM_MODE = 'single'
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
                # Real joins and game transitions in both server modes. Only a
                # connected player's own phase hides the tour; join forms stay
                # manual-only even when the teacher has started a question.
                for room_mode in ('single', 'multi'):
                    server.ROOM_MODE = room_mode
                    ctx = context({'width': 390, 'height': 844}, authenticated=False)
                    page = ctx.new_page(); ready(page, '/play?pin=' + server.DEFAULT_ROOM.pin)
                    student_catalog(page, room_mode + '-before-join')
                    page.locator('#nameInput').fill('TourPlayer-' + room_mode); page.locator('#joinBtn').click()
                    page.wait_for_selector('#gameCard', state='visible')
                    page.evaluate("ClassRallyTour.start('join')"); page.wait_for_selector('.cr-tour-never input')
                    page.keyboard.press('Escape')
                    page.evaluate("ClassRallyTour.start('language')"); page.wait_for_selector('[data-step="studentlanguage"]')
                    def host_action(action):
                        response = ctx.request.post(base + '/api/host/action', data={'action': action},
                                                    headers={'Authorization': 'Bearer ' + server.HOST_TOKEN})
                        assert response.ok, (action, response.status, response.text())
                    host_action('start')
                    page.wait_for_function("document.body.dataset.phase === 'question'")
                    for selector in ('#cr-tour-card', '#cr-tour-launch', '#cr-tour-offer'):
                        page.locator(selector).wait_for(state='hidden')
                    page.locator('#options').wait_for(state='visible')
                    page.screenshot(path=str(shots / f'mobile-{room_mode}-question-no-tour.png'))
                    # No PIN and a complete PIN both show only the join form.
                    for query in ('', '?pin=' + server.DEFAULT_ROOM.pin):
                        late_ctx = context({'width': 390, 'height': 844}, authenticated=False)
                        late = late_ctx.new_page(); ready(late, '/play' + query)
                        student_catalog(late, room_mode + '-late-join' + query)
                        assert late.locator('#joinCard').is_visible()
                        late_ctx.close()
                    if room_mode == 'single':
                        host_ctx = context({'width': 390, 'height': 844}); host = host_ctx.new_page(); ready(host, '/host')
                        host.wait_for_timeout(1600); assert not host.locator('#cr-tour-offer').count(); host_ctx.close()
                    host_action('reveal')
                    page.wait_for_function("document.body.dataset.phase === 'reveal'")
                    page.evaluate("ClassRallyTour.start('language')")
                    assert not page.locator('#cr-tour-card').is_visible()
                    assert not page.locator('#cr-tour-launch').is_visible()
                    host_action('reset')
                    page.wait_for_function("document.body.dataset.phase === 'lobby'")
                    page.wait_for_selector('#cr-tour-launch')
                    ctx.close(); server.ROOM_MODE = 'single'
                    print(f'PASS {room_mode}: joined question/reveal hides tour; late join form stays manual-only', flush=True)
                # Homework uses its existing page phase without live-game state.
                ctx = context({'width': 390, 'height': 844}, authenticated=False)
                response = ctx.request.post(base + '/api/selfpaced/start', data={'filename': 'tour.json'},
                                            headers={'Authorization': 'UserToken ' + token})
                assert response.ok, response.text()
                hw_session = response.json()['session_id']
                for viewport in ({'width': 1440, 'height': 900}, {'width': 390, 'height': 844}):
                    hw_ctx = context(viewport, authenticated=False)
                    hw_page = hw_ctx.new_page(); ready(hw_page, '/play?hw=' + hw_session)
                    student_catalog(hw_page, str(viewport['width']) + '/play?hw')
                    hw_ctx.close()
                page = ctx.new_page(); hw_requests = []
                page.on('request', lambda request: hw_requests.append(request.url) if urlparse(request.url).path == '/api/state' else None)
                ready(page, '/play?hw=' + hw_session)
                student_catalog(page, 'homework-before-join')
                page.locator('#spNameInput').fill('TourHomework')
                page.evaluate("ClassRallyTour.start('language')"); page.wait_for_selector('[data-step="studentlanguage"]')
                page.evaluate("document.getElementById('spJoinBtn').click()")
                page.wait_for_function("document.body.dataset.phase === 'question'")
                page.locator('#spOptions button').first.wait_for(state='visible')
                for selector in ('#cr-tour-card', '#cr-tour-launch', '#cr-tour-offer'):
                    page.locator(selector).wait_for(state='hidden')
                page.locator('#spOptions button').first.click()
                page.wait_for_function("document.body.dataset.phase === 'reveal'")
                assert not page.locator('#cr-tour-launch').is_visible()
                page.locator('#spNextBtn').click()
                page.wait_for_function("document.body.dataset.phase === 'question'")
                page.locator('#spOptions button').first.click()
                page.wait_for_function("document.body.dataset.phase === 'reveal'")
                page.locator('#spNextBtn').click()
                page.wait_for_function("document.body.dataset.phase === 'finished'")
                launcher_geometry(page, 'homework-results', hidden=True)
                page.locator('#spRestartBtn').click()
                launcher_geometry(page, 'homework-results-closed', hidden=False)
                assert not hw_requests, hw_requests
                ctx.close()
                print('PASS homework: manual-only/join/question/reveal/finished, 0 live state requests', flush=True)
                browser.close()
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)
        assert not errors, errors
        assert not foreign, ('non-local requests attempted', foreign)
        report = {'status': 'PASS', 'catalog_runs': len(results), 'runs': results, 'js_errors': errors,
                  'foreign_requests': foreign, 'state_checks': state_checks, 'offer_checks': offer_checks,
                  'student_checks': student_checks, 'launcher_checks': launcher_checks,
                  'game_checks': game_checks, 'screenshots': str(shots)}
        (QA / 'browser-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': 'PASS', 'catalog_runs': len(results), 'js_errors': len(errors), 'foreign_requests': len(foreign), 'screenshots': str(shots)}, ensure_ascii=False))


if __name__ == '__main__':
    run()
