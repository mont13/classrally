#!/usr/bin/env python3
"""Local tour CPU benchmark; no dependencies beyond the installed Playwright.

Compare the same workload with --source pointing to a saved tour.js. Outputs and
browser profiles stay in .cache/tour-qa/kolo5. CPU throttle is always 6x.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
OUT = ROOT / '.cache/tour-qa/kolo5'

# Test-only instrumentation. Nested calls count once in the inclusive total.
# Wrap observers too: ignoring mutations has a cost that must stay in the result.
PROBE = r"""
  const __perf = window.__tourPerf = {spans: [], counts: {}, longTasks: [], depth: 0};
  function __measure(name, fn, self, args) {
    __perf.counts[name] = (__perf.counts[name] || 0) + 1;
    if (__perf.depth) return fn.apply(self, args);
    const start = performance.now(); __perf.depth = 1;
    try { return fn.apply(self, args); }
    finally {
      __perf.depth = 0;
      __perf.spans.push({name, start, end: performance.now()});
    }
  }
  const MutationObserver = class extends window.MutationObserver {
    constructor(fn) { super((...args) => __measure('mutations', fn, null, args)); }
  };
  const ResizeObserver = class extends window.ResizeObserver {
    constructor(fn) { super((...args) => __measure('resize', fn, null, args)); }
  };
  new PerformanceObserver(list => __perf.longTasks.push(...list.getEntries().map(
    e => ({start: e.startTime, end: e.startTime + e.duration, duration: e.duration})
  ))).observe({type: 'longtask', buffered: true});
"""


def instrument(source):
    # Time entry points, not every call to visible()/getRect inside a scan:
    # thousands of nested probes would materially distort the old version.
    boundaries = {'layout', 'pageOverlayOpen', 'placeLauncher', 'schedule', 'refreshAvailability',
                  'followTarget', 'offerTour'}
    names = [name for name in re.findall(r'^  (?:async )?function (\w+)\(', source, re.M) if name in boundaries]
    wrappers = '\n'.join(
        f'  {name} = ((fn) => function(...args) {{ return __measure({json.dumps(name)}, fn, this, args); }})({name});'
        for name in names)
    source = source.replace("  'use strict';", "  'use strict';\n" + PROBE, 1)
    return source.replace('  window.ClassRallyTour =', wrappers + '\n  window.ClassRallyTour =', 1)


def run(source_path, label, enforce):
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    source = source_path.read_text()
    measured_source = instrument(source)
    results, errors, foreign, hidden_checks = [], [], [], []
    with tempfile.TemporaryDirectory(prefix='perf-runtime-', dir=OUT) as runtime:
        runtime = Path(runtime)
        os.environ.update(QUIZ_DATA_DIR=str(runtime / 'data'), QUIZ_HISTORY_DIR=str(runtime / 'history'),
                          QUIZ_QUESTIONS_DIR=str(runtime / 'questions'), QUIZ_UPLOADS_DIR=str(runtime / 'uploads'),
                          MAIL_MODE='off', QUIZ_ROOMS='single', QUIZ_GAME_LOG='0', BUGGY_URL='',
                          BUGGY_PROJECT_KEY='', BUGGY_WIDGET_DIR='', CLASSRALLY_PORTAL_SECRET='',
                          QUIZ_BASE_URL='', AI_BASE_URL='http://127.0.0.1:1', AI_API_KEY='')
        for directory in ('history', 'questions', 'uploads'):
            (runtime / directory).mkdir()
        bank = [{'id': 'perf-1', 'prompt': 'Kolik je 2 + 2?', 'options': ['3', '4'], 'correct_index': 1}]
        (runtime / 'questions/perf.json').write_text(json.dumps(bank))
        import server
        server.user_db.PBKDF2_ITERATIONS = 1000
        server.user_db.init_db()
        teacher = server.user_db.create_user('PerfTeacher', secrets.token_hex(12), 'teacher')
        token = server.user_db.create_session(teacher['id'])

        class Handler(server.Handler):
            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        base = f'http://127.0.0.1:{httpd.server_port}'
        server.SERVER_INFO.update(host_urls=[base + '/host'], play_urls=[base + '/play'], loopback_only=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=[
                    '--disable-background-networking', '--disable-component-update', '--disable-sync'])
                try:
                    for viewport in ({'width': 390, 'height': 844}, {'width': 1440, 'height': 900}):
                        for scenario in ('play-waiting', 'host-lobby-30', 'host-question-30'):
                            room = server.DEFAULT_ROOM
                            room.quiz.clear_players()
                            room.quiz.reload_questions(bank, 'perf.json')
                            room.quiz.get_ready_sec = 0
                            room.quiz.question_duration_sec = 300
                            for index in range(30):
                                room.quiz.register_player(f'Perf{index + 1}')
                            ctx = browser.new_context(viewport=viewport, locale='cs-CZ', service_workers='block')
                            ctx.add_init_script('if(location.origin===' + json.dumps(base) + ') {' +
                                                'localStorage.setItem("quiz_user_token",' + json.dumps(token) + ');' +
                                                'localStorage.setItem("classrally_lang","cs");}')

                            def route(request_route):
                                url = urlparse(request_route.request.url)
                                if url.scheme in ('http', 'https') and (url.hostname != '127.0.0.1' or url.port != httpd.server_port):
                                    foreign.append(url.hostname)
                                    request_route.abort()
                                elif url.path == '/static/tour.js':
                                    request_route.fulfill(body=measured_source, content_type='text/javascript')
                                else:
                                    request_route.continue_()

                            ctx.route('**/*', route)
                            page = ctx.new_page()
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            cdp = ctx.new_cdp_session(page)
                            cdp.send('Emulation.setCPUThrottlingRate', {'rate': 6})
                            cdp.send('Performance.enable')
                            path = '/play?pin=' + room.pin if scenario.startswith('play') else '/host'
                            page.goto(base + path)
                            page.wait_for_selector('#cr-tour-launch', state='attached')
                            page.wait_for_load_state('networkidle')
                            if scenario.startswith('play'):
                                page.locator('#nameInput').fill('PerfWaiting')
                                page.locator('#joinBtn').click()
                                page.wait_for_function("document.body.dataset.phase === 'lobby'")
                            elif scenario == 'host-question-30':
                                page.locator('#btnStart').click()
                                page.wait_for_function("document.body.classList.contains('in-game')")
                            # Let initial layout, offers, fonts and phase changes settle.
                            page.wait_for_timeout(1500)
                            # Deterministic 60 Hz inline animation and leaderboard DOM churn,
                            # on the real waiting/game page. Same workload before and after.
                            page.evaluate(r"""() => {
                              const root = document.createElement('div'); root.className='confetti';
                              root.style.cssText='position:fixed;inset:0;pointer-events:none';
                              for(let i=0;i<60;i++) root.append(document.createElement('i'));
                              document.body.append(root);
                              const timer=document.querySelector('#kTimer, #timer');
                              const rows=document.querySelector('#scoreBody');
                              let tick=0;
                              window.__perfChurn=setInterval(() => {
                                tick++;
                                root.children[tick%60].style.transform=`translateX(${tick%100}px)`;
                                if(timer) timer.textContent=String(300-tick%300);
                                if(rows?.firstElementChild && tick%6===0) {
                                  rows.firstElementChild.lastElementChild.dataset.perf=String(tick);
                                  rows.firstElementChild.children[2].textContent=String(tick);
                                }
                              },16);
                            }""")
                            before = {m['name']: m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
                            page.evaluate("__tourPerf.spans=[]; __tourPerf.counts={}; __tourPerf.longTasks=[]; window.__perfStart=performance.now()")
                            page.wait_for_timeout(10000)
                            data = page.evaluate(r"""() => {
                              clearInterval(__perfChurn);
                              const end=performance.now(), spans=__tourPerf.spans.filter(s=>s.start>=__perfStart);
                              return {elapsed_ms:end-__perfStart, spans, counts:__tourPerf.counts,
                                long_tasks:__tourPerf.longTasks.filter(t=>t.start>=__perfStart)};
                            }""")
                            after = {m['name']: m['value'] for m in cdp.send('Performance.getMetrics')['metrics']}
                            durations = [s['end'] - s['start'] for s in data['spans']]
                            total = sum(durations)
                            task_ms = (after['TaskDuration'] - before['TaskDuration']) * 1000
                            guide_long = [s for s in data['spans'] if s['end'] - s['start'] > 50]
                            overlap = [task for task in data['long_tasks'] if any(
                                span['start'] < task['end'] and span['end'] > task['start'] for span in data['spans'])]
                            result = dict(scenario=scenario, viewport=viewport, cpu_throttle=6,
                                          elapsed_ms=round(data['elapsed_ms'], 3), tour_ms=round(total, 3),
                                          budget_percent=round(total / data['elapsed_ms'] * 100, 3),
                                          main_thread_task_ms=round(task_ms, 3),
                                          busy_percent=round(total / task_ms * 100, 3) if task_ms else 0,
                                          max_tour_span_ms=round(max(durations, default=0), 3),
                                          tour_long_tasks=len(guide_long), overlapping_long_tasks=len(overlap),
                                          counts=data['counts'])
                            results.append(result)
                            print(json.dumps(result), flush=True)
                            if enforce:
                                assert data['counts'].get('refreshAvailability', 0) <= int(data['elapsed_ms'] / 250) + 1, result
                                # Visibility is a local DOM fixture: unlike freezing the
                                # renderer, it proves the guide's own early-return gate.
                                page.evaluate(r"""() => {
                                  Object.defineProperty(document,'hidden',{configurable:true,value:true});
                                  document.dispatchEvent(new Event('visibilitychange'));
                                  __tourPerf.counts={};
                                  document.body.dataset.phase='lobby';
                                  window.dispatchEvent(new Event('resize'));
                                  document.dispatchEvent(new Event('scroll'));
                                }""")
                                page.wait_for_timeout(750)
                                counts = page.evaluate('__tourPerf.counts')
                                assert all(counts.get(name, 0) == 0 for name in (
                                    'refreshAvailability', 'pageOverlayOpen', 'placeLauncher', 'layout', 'followTarget')), counts
                                page.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
                                hidden_checks.append(dict(scenario=scenario, viewport=viewport, counts=counts))
                            ctx.close()
                finally:
                    browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)
    report = dict(label=label, source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                  results=results, hidden_checks=hidden_checks, js_errors=errors, foreign_requests=foreign)
    (OUT / f'performance-{label}.json').write_text(json.dumps(report, indent=2) + '\n')
    assert not errors, errors
    assert not foreign, foreign
    if enforce:
        # Brána: podíl z celkové doby měření (stabilní) < 2 % a žádný úsek průvodce > 50 ms.
        # busy_percent (podíl z práce hlavního vlákna) má malý jmenovatel (~1,4 s za 10 s při klidné stránce)
        # a na vytíženém stroji kolísá (8. 11. 2026: 1,4 % vs. 2,5 % bez změny kódu) — jen se zaznamenává,
        # s volným stropem 5 % proti skutečné regresi (kolo 4 mělo 52–86 %).
        assert all(r['budget_percent'] < 2 and r['busy_percent'] < 5 and not r['tour_long_tasks'] for r in results), results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'static/tour.js')
    parser.add_argument('--label', choices=('before', 'after'), default='after')
    parser.add_argument('--enforce', action='store_true')
    args = parser.parse_args()
    run(args.source, args.label, args.enforce)
