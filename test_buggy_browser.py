#!/usr/bin/env python3
"""Local Buggy host regression, using an open-shadow widget with a public handle.

Run: omez-pamet -- python3 test_buggy_browser.py
No widget build, collector, credentials or external requests are needed.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OUT = ROOT / '.cache/buggy-qa'
STUB = r"""window.InvoAIMBuggy = {mountBuggyWidget({target}) {
  const shadow = target.attachShadow({mode:'open'});
  shadow.innerHTML = `<style>
    .fixed {position:fixed;bottom:80px;right:16px;width:56px;height:56px}
    [role=dialog] {position:fixed;inset:20px;background:white}
    [hidden] {display:none!important}
    @media(min-width:1024px) {.fixed {bottom:20px;right:20px}}
  </style><div><button type="button" class="fixed bottom-20 rounded-full"
    aria-label="Buggy — nahlásit problém">Buggy</button>
    <section role="dialog" aria-label="Test report" hidden><button type="button">Close</button></section></div>`;
  const launcher=shadow.querySelector('button'), dialog=shadow.querySelector('[role=dialog]');
  if (window.__changedWidget) {
    const wrapper=document.createElement('section'); launcher.before(wrapper); wrapper.append(launcher);
  }
  const handle={open() { window.__openedBuggy=(window.__openedBuggy||0)+1; dialog.hidden=false; }};
  launcher.onclick=()=>handle.open();
  dialog.querySelector('button').onclick=()=>{dialog.hidden=true;};
  return handle;
}};
"""


def inspect(page, label, enabled):
    active = enabled and label not in ('question', 'answered', 'reveal')
    controls = page.locator('[data-buggy-open]:visible')
    assert controls.count() == int(active), (label, enabled, controls.count())
    if not enabled:
        assert page.locator('#classrally-buggy-root').count() == 0
    # The shadow launcher has no layout at all, so it cannot intersect controls
    # at any scroll position. Check the rest of the shadow tree as well, and
    # independently measure all page controls at the beginning, middle and end.
    measured = page.evaluate(r"""() => {
      const visible=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) &&
        e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0;
      const root=document.querySelector('#classrally-buggy-root');
      const shadow=root?.shadowRoot;
      const widget=[...(shadow?.querySelectorAll('button,[role=dialog],input,textarea')||[])];
      const launcher=shadow?.querySelector('button');
      const max=document.documentElement.scrollHeight-innerHeight, samples=[];
      const selector='button,a[href],input:not([type=hidden]),select,textarea,summary,[role=button],[tabindex],[onclick]';
      for(const y of [0,Math.floor(max/2),max]) {
        scrollTo(0,y);
        const collisions=[];
        for(const a of widget.filter(visible)) {
          const r=a.getBoundingClientRect();
          for(const b of [...document.querySelectorAll(selector)].filter(visible)) {
            const s=b.getBoundingClientRect();
            if(Math.min(r.right,s.right)-Math.max(r.left,s.left)>.5 &&
               Math.min(r.bottom,s.bottom)-Math.max(r.top,s.top)>.5) collisions.push(b.id||b.tagName);
          }
        }
        samples.push({scroll:scrollY,collisions});
      }
      return {launcherDisplay:launcher ? getComputedStyle(launcher).display : null,
        shadowVisible:widget.filter(visible).length,samples,maxScroll:max,
        paddingBottom:getComputedStyle(document.body).paddingBottom};
    }""")
    assert measured['shadowVisible'] == 0, (label, measured)
    if enabled: assert measured['launcherDisplay'] == 'none', (label, measured)
    assert all(not row['collisions'] for row in measured['samples']), (label, measured)
    if active:
        report = controls.first
        assert report.get_attribute('aria-label') == page.evaluate("i18n.t('common.report_bug')")
        report.click()
        page.get_by_role('dialog', name='Test report').wait_for()
        assert page.evaluate('window.__openedBuggy') >= 1
        page.get_by_role('button', name='Close', exact=True).click()
    if active and label in ('join', 'lobby'):
        # Both bug guide IDs resolve to the student step on /play.
        page.evaluate("ClassRallyTour.start('bug')")
        page.locator('[data-step=studentbug]').wait_for()
        page.wait_for_function("!document.querySelector('#cr-tour-ring').hidden")
        ring = page.locator('#cr-tour-ring').bounding_box()
        target = report.bounding_box()
        assert ring['x'] <= target['x'] and ring['y'] <= target['y'], (label, ring, target)
        assert ring['width'] >= target['width'] and ring['height'] >= target['height'], (label, ring, target)
        page.keyboard.press('Escape')
    return measured


def run():
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    # Keep Playwright profiles and temporary DBs inside this repository as well.
    os.environ['TMPDIR'] = str(OUT)
    tempfile.tempdir = str(OUT)
    with tempfile.TemporaryDirectory(prefix='runtime-', dir=OUT) as runtime:
        runtime = Path(runtime)
        os.environ.update(QUIZ_DATA_DIR=str(runtime/'data'), QUIZ_HISTORY_DIR=str(runtime/'history'),
                          QUIZ_QUESTIONS_DIR=str(runtime/'questions'), QUIZ_UPLOADS_DIR=str(runtime/'uploads'),
                          QUIZ_ROOMS='single', MAIL_MODE='off', QUIZ_GAME_LOG='0',
                          BUGGY_URL='', BUGGY_PROJECT_KEY='', BUGGY_WIDGET_DIR='',
                          CLASSRALLY_PORTAL_SECRET='', QUIZ_BASE_URL='',
                          AI_BASE_URL='http://127.0.0.1:1', AI_API_KEY='')
        for directory in ('questions', 'history', 'uploads'): (runtime/directory).mkdir()
        bank = [{'id':'one', 'prompt':'Kolik je 2 + 2?', 'options':['3','4'], 'correct_index':1}]
        (runtime/'questions/buggy.json').write_text(json.dumps(bank))
        import server
        server.user_db.PBKDF2_ITERATIONS = 1000
        server.user_db.init_db()
        server.QUIZ.get_ready_sec = 0
        server.QUIZ.question_duration_sec = 300

        class Handler(server.Handler):
            def log_message(self, *args): pass

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        base = f'http://127.0.0.1:{httpd.server_port}'
        server.SERVER_INFO.update(host_urls=[base+'/host'], play_urls=[base+'/play'], loopback_only=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        errors, foreign, results = [], [], []

        def action(name):
            with urlopen(Request(base+'/api/host/action', data=json.dumps({'action':name}).encode(),
                                 headers={'Content-Type':'application/json', 'Authorization':'Bearer '+server.HOST_TOKEN})) as r:
                assert r.status == 200

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=[
                    '--disable-background-networking', '--disable-component-update', '--disable-sync'])

                def context(width, height, enabled=True, changed=False):
                    ctx = browser.new_context(viewport={'width':width,'height':height},
                                              is_mobile=width<500, has_touch=width<500, service_workers='block')
                    ctx.add_init_script('if(location.origin==='+json.dumps(base)+') {'+
                                        'localStorage.setItem("classrally_lang","cs"); window.__changedWidget='+json.dumps(changed)+';}')

                    def route(r):
                        u = urlsplit(r.request.url)
                        if u.hostname != '127.0.0.1' or u.port != httpd.server_port:
                            foreign.append(r.request.url); r.abort()
                        elif u.path == '/api/bug-report/config':
                            r.fulfill(json={'enabled':enabled, 'script':'/buggy/fixture.js'} if enabled else {'enabled':False})
                        elif u.path == '/buggy/fixture.js':
                            r.fulfill(body=STUB, content_type='text/javascript')
                        else: r.continue_()

                    ctx.route('**/*', route)
                    ctx.on('page', lambda p: p.on('pageerror', lambda e: errors.append(str(e))))
                    return ctx

                for width,height in ((360,740),(390,844),(1440,900)):
                    for enabled in (True,False):
                        server.set_app_mode('game')
                        action('reset')
                        ctx=context(width,height,enabled); page=ctx.new_page()
                        page.goto(base+'/play'); page.wait_for_load_state('networkidle')
                        def phase(name):
                            measured=inspect(page,name,enabled)
                            results.append(dict(viewport=[width,height],enabled=enabled,phase=name,**measured))
                        phase('join')
                        if enabled:
                            for lang in ('cs','en','de','sk','uk'):
                                page.evaluate('lang=>i18n.setLang(lang)', lang)
                                label=page.evaluate("i18n.t('common.report_bug')")
                                assert label != 'common.report_bug'
                                assert page.locator('[data-buggy-open]:visible').get_attribute('aria-label') == label
                            page.evaluate("i18n.setLang('cs')")
                        page.locator('#nameInput').fill(f'QA{width}{enabled}')
                        page.locator('#joinBtn').click()
                        page.wait_for_function("document.body.dataset.phase==='lobby'"); phase('lobby')
                        with urlopen(Request(base+'/api/register', data=json.dumps({'name':f'Wait{width}{enabled}'}).encode(),
                                             headers={'Content-Type':'application/json'})) as r:
                            assert r.status == 200
                        action('start')
                        page.wait_for_function("document.body.dataset.phase==='question'"); phase('question')
                        page.locator('#options .kahoot-btn').first.click()
                        page.wait_for_function("document.body.dataset.phase==='question' && document.body.hasAttribute('data-answered')")
                        phase('answered')
                        action('reveal')
                        page.wait_for_function("document.body.dataset.phase==='reveal'"); phase('reveal')
                        action('next')
                        page.wait_for_function("document.body.dataset.phase==='finished'")
                        page.locator('#mySummary .my-summary-row').last.wait_for(); phase('finished')
                        server.set_app_mode('exam')
                        page.goto(base+'/exam'); page.wait_for_load_state('networkidle')
                        phase('exam-join')
                        ctx.close()

                # A changed widget layout must leave its launcher available and
                # the inline public API functional; never hide its dialog.
                server.set_app_mode('game')
                ctx=context(390,844,changed=True); page=ctx.new_page()
                page.goto(base+'/play'); page.wait_for_load_state('networkidle')
                assert page.get_by_role('button', name='Buggy — nahlásit problém').is_visible()
                page.locator('[data-buggy-open]:visible').click()
                page.get_by_role('dialog', name='Test report').wait_for(); ctx.close()

                # Outside /play the tour must highlight the actual shadow
                # launcher. Disabled mode still supplies the textual fallback.
                for enabled in (True,False):
                    ctx=context(390,844,enabled); page=ctx.new_page()
                    page.goto(base+'/profile'); page.wait_for_load_state('networkidle')
                    page.evaluate("ClassRallyTour.start('bug')")
                    page.locator('[data-step=studentbug]').wait_for()
                    page.wait_for_function('Boolean(document.querySelector("#cr-tour-ring").hidden) === '+json.dumps(not enabled))
                    ctx.close()
                browser.close()
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)
        assert not errors, errors
        assert not foreign, foreign
        (OUT/'results.json').write_text(json.dumps(results,indent=2)+'\n')
        print(json.dumps({'status':'PASS','phase_checks':len(results),'viewports':3,
                          'languages':5,'js_errors':len(errors),'foreign_requests':len(foreign)}))


if __name__ == '__main__':
    run()
