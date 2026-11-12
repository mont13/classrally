#!/usr/bin/env python3
"""Local helper acceptance with installed Playwright and fake AI; no downloads.

Run: omez-pamet -- python3 test_help_browser.py
"""
import io
import json
import secrets
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from test_server import _AccountHTTPBase, server
from test_help_assistant import EXAMPLES, fake_reply


def run():
    from playwright.sync_api import sync_playwright
    _AccountHTTPBase.setUpClass()
    harness = _AccountHTTPBase
    teacher = server.user_db.create_user('HelpTeacher', secrets.token_hex(12), 'teacher')
    pupil = server.user_db.create_user('HelpPupil', secrets.token_hex(12), 'student')
    token = server.user_db.create_session(teacher['id'])
    pupil_token = server.user_db.create_session(pupil['id'])
    foreign, errors, help_posts, other_posts, payloads = [], [], [], [], []
    base = harness.base_url
    port = urlparse(base).port
    mode = {'value': 'success'}

    def fake_ai(request, **kwargs):
        payloads.append(json.loads(request.data))
        if mode['value'] == 'offline':
            raise TimeoutError('test only')
        if mode['value'] == 'markup':
            return fake_reply('<img src=x onerror=alert(1)>')
        if mode['value'] == 'english':
            return fake_reply('1. Open "Question editor".\n2. Click "Import JSON" and choose a file.')
        return fake_reply(EXAMPLES['Jak spustím písemku?'])

    try:
        with mock.patch.dict(server.AI_CONFIG, base_url='http://127.0.0.1:1', model='fake', api_key=''), \
             mock.patch('help_assistant.urlopen', side_effect=fake_ai), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--disable-background-networking', '--disable-sync'])
            def context(account, language='cs', viewport=None):
                ctx = browser.new_context(viewport=viewport or {'width':1280,'height':800}, service_workers='block')
                seed = {'classrally_lang': language}
                if account:
                    seed['quiz_user_token'] = account
                ctx.add_init_script('if(location.origin===' + json.dumps(base) + ') Object.entries(' + json.dumps(seed) + ').forEach(([k,v])=>localStorage.setItem(k,v));')
                ctx.on('page', lambda p: p.on('pageerror', lambda e: errors.append(str(e))))
                def route(r):
                    parsed = urlparse(r.request.url)
                    if parsed.scheme in ('http', 'https') and (parsed.hostname != '127.0.0.1' or parsed.port != port):
                        foreign.append(parsed.hostname); r.abort(); return
                    if r.request.method == 'POST':
                        (help_posts if parsed.path == '/api/teacher/help' else other_posts).append(parsed.path)
                    r.continue_()
                ctx.route('**/*', route)
                return ctx

            def catalog(page):
                page.locator('#cr-tour-launch').wait_for(state='visible')
                page.locator('#cr-tour-launch').click()
                page.locator('[data-tour-guide]').first.wait_for()

            def helper(page):
                catalog(page); page.locator('#cr-tour-help-open').click()
                page.locator('#cr-tour-help-question').wait_for()
                assert page.locator('#cr-tour-help-question').evaluate('(e)=>e===document.activeElement')

            def ask(page, question, expected):
                field = page.locator('#cr-tour-help-question')
                field.fill(question)
                page.locator('#cr-tour-help-form button[type=submit]').click()
                page.wait_for_function("expected => document.querySelector('#cr-tour-help-result')?.textContent.includes(expected)", arg=expected)
                assert page.locator('#cr-tour-help-form button[type=submit]').is_enabled()

            for language, size in [('cs', {'width':1280,'height':800}), ('en', {'width':390,'height':844})]:
                server.HELP_LIMITER._attempts.clear()
                ctx = context(token, language, size); page = ctx.new_page(); page.goto(base + '/admin')
                page.wait_for_selector('#adminPanel:not(.hide)')
                # Opening help preserves a teacher's unsaved editor text and tab.
                page.locator('[data-tab=questions]').click()
                page.locator('#newBankName').fill('Unsaved fixture')
                helper(page)
                if language == 'cs':
                    mode['value'] = 'success'
                    ask(page, 'Jak spustím písemku?', 'Spustit písemku')
                    mode['value'] = 'markup'; ask(page, 'Písemka', '<img')
                    assert page.locator('#cr-tour-help-result img').count() == 0
                    mode['value'] = 'offline'; ask(page, 'Písemka', 'nyní neodpovídá')
                    mode['value'] = 'success'
                    with mock.patch.dict(server.AI_CONFIG, model=''):
                        ask(page, 'Písemka', 'AI není nastavená')
                    ask(page, 'Kolik stojí banány?', 'nemám ověřený podklad')
                    with mock.patch.object(server.HELP_LIMITER, 'max_requests', 0):
                        ask(page, 'Písemka', 'Limit je 6')
                else:
                    mode['value'] = 'english'
                    ask(page, 'How do I import a set?', 'Import JSON')
                    assert 'Answer in English' in payloads[-1]['messages'][0]['content']
                box = page.locator('#cr-tour-card').bounding_box()
                assert 0 <= box['x'] and box['x'] + box['width'] <= size['width'] + 1
                assert 0 <= box['y'] and box['y'] + box['height'] <= size['height'] + 1
                # Focus wraps and Escape closes only the helper, with no game action.
                page.locator('#cr-tour-help-form button').last.focus(); page.keyboard.press('Tab')
                assert page.locator('#cr-tour-help-question').evaluate('(e)=>e===document.activeElement')
                page.locator('#cr-tour-help-form button').nth(1).click()
                assert page.locator('[data-tour-guide]').count() > 5
                assert page.locator('#newBankName').input_value() == 'Unsaved fixture'
                assert not page.evaluate("Object.values(localStorage).some(v=>v.includes('Jak spustím písemku?'))")
                page.keyboard.press('Escape')
                assert not page.locator('#cr-tour-card').is_visible()
                # Let the catalog's role check finish before closing the test
                # tab; otherwise its aborted GET can print a BrokenPipe trace.
                page.wait_for_load_state('networkidle')
                ctx.close()

            for account, path in [(pupil_token, '/play'), (pupil_token, '/admin'), (None, '/admin')]:
                ctx = context(account); page = ctx.new_page(); page.goto(base + path)
                catalog(page); page.wait_for_load_state('networkidle')
                assert page.locator('#cr-tour-help-open').count() == 0
                ctx.close()
            browser.close()
        assert not foreign, foreign
        assert not errors, errors
        assert not other_posts, other_posts
        report = {'status':'PASS', 'desktop_cs':True, 'mobile_en':True, 'question_posts':len(help_posts),
                  'fake_ai_calls':len(payloads), 'foreign_requests':foreign, 'other_posts':other_posts,
                  'js_errors':errors, 'checks':['success','plain text / XSS','AI timeout','AI unconfigured',
                  'unknown topic','rate limit','teacher only UI','focus wrap','Escape','manual catalog',
                  'unsaved form preserved','no question storage']}
        out = Path(__file__).resolve().parent / '.cache/helper-browser.json'
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        harness.tearDownClass()


if __name__ == '__main__':
    run()
