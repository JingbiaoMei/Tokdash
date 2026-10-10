"""Compare retained Overview memory after GC, outside all timed benchmarks.

Run from the repository root. Each version gets its own browser process and
isolated synthetic fixture server; no local history or credentials are read.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import threading
from playwright.sync_api import sync_playwright

PRESETS = ['today', 'yesterday', 'last7days', 'lastWeek', 'last14days',
           'last4weeks', 'thisMonth', 'lastMonth', 'thisYear', 'lastYear']
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline', required=True, type=Path)
parser.add_argument('--output', type=Path, default=Path('output/sparkline-validation/retained-memory'))
parser.add_argument('--server-python', default='python3')
parser.add_argument('--repeats', type=int, default=3)
parser.add_argument('--port', type=int, default=55821)
args = parser.parse_args()
if args.repeats < 1 or not 1 <= args.port < 65535:
    parser.error('positive repeats and two valid consecutive ports are required')
root, baseline, output = Path.cwd(), args.baseline.resolve(), args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
servers = []
errors = []

def interrupted(_signal, _frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, interrupted)

def start(repo, port, label):
    env = dict(os.environ, PYTHONPATH=str(repo / 'src'),
               TOKDASH_DATA_DIR=str(output / (label + '-data')))
    proc = subprocess.Popen([args.server_python, 'main.py', '--no-open',
        '--dev-fixture', 'dense', '--dev-seed', '17', '--port', str(port)],
        cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    servers.append(proc)
    event, ready = threading.Event(), []
    def drain():
        with (output / (label + '-server.log')).open('w') as log:
            for line in proc.stdout:
                log.write(line)
                if 'Uvicorn running on' in line:
                    ready.append(True)
                    event.set()
        event.set()
    threading.Thread(target=drain, daemon=True).start()
    if not event.wait(20) or not ready:
        raise RuntimeError(f'{label} fixture did not start')
    return f'http://127.0.0.1:{port}'

samples = {preset: {'baseline': [], 'candidate': []} for preset in PRESETS}
revisions = {label: subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    for label, repo in [('baseline', baseline), ('candidate', root)]}
try:
    urls = {label: start(repo, args.port + index, label)
        for index, (label, repo) in enumerate([('baseline', baseline), ('candidate', root)])}
    with sync_playwright() as p:
        for label in ['baseline', 'candidate']:
            # Separate processes avoid attribution to another page's V8 heap.
            browser = p.chromium.launch(headless=True,
                args=['--disable-gpu', '--disable-features=Vulkan,VizDisplayCompositor'])
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1050},
                    reduced_motion='reduce', service_workers='block')
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                session = context.new_cdp_session(page)
                session.send('Performance.enable')
                for iteration in range(args.repeats):
                    for preset in PRESETS:
                        page.goto(urls[label], wait_until='domcontentloaded')
                        page.wait_for_function("""() => lastUsageResponse &&
                            overviewActiveTimeState.status === 'ready' && statsCache.default && !updateInFlight""",
                            timeout=15000)
                        if preset != 'today':
                            button = page.locator(f'#overviewQuickRanges [data-range="{preset}"]')
                            if not button.is_visible():
                                page.locator('#quickRangeMoreToggle').click()
                            button.click()
                            page.wait_for_function("""preset => activeQuickRange === preset && !updateInFlight &&
                                overviewActiveTimeState.status === 'ready' && overviewActiveTimeState.key ===
                                activeTimeRequestKey(null, formatDateKey(currentStartDate), formatDateKey(currentEndDate))""",
                                arg=preset, timeout=15000)
                        # Let existing chart animations finish, then collect only
                        # outside the measured latency/CPU campaigns.
                        page.wait_for_timeout(1100)
                        session.send('HeapProfiler.collectGarbage')
                        session.send('HeapProfiler.collectGarbage')
                        metrics = {row['name']: row['value']
                            for row in session.send('Performance.getMetrics')['metrics']}
                        dom = session.send('Memory.getDOMCounters')
                        samples[preset][label].append({'js_heap_bytes': metrics['JSHeapUsedSize'],
                            'js_heap_total_bytes': metrics['JSHeapTotalSize'], **dom})
                    print(f'{label}: retained-memory round {iteration + 1}/{args.repeats}', flush=True)
            finally:
                browser.close()
    assert not errors, errors
    cases = {}
    for preset, values in samples.items():
        row = {label: {key: statistics.median(sample[key] for sample in values[label])
            for key in values[label][0]} for label in ['baseline', 'candidate']}
        row['retained_heap_delta_bytes'] = row['candidate']['js_heap_bytes'] - row['baseline']['js_heap_bytes']
        cases[preset] = row
    report = {'revisions': revisions, 'repeats_per_range': args.repeats,
        'method': 'separate browser processes; fresh Today navigation before every range; charts settled for 1100 ms; two explicit GC passes outside timed benchmarks; CDP heap and DOM counters',
        'cases': cases, 'samples': samples, 'errors': errors,
        'limits': 'Retained JavaScript heap and DOM only; browser native allocations and total process RSS are not measured.'}
    (output / 'retained-memory.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(cases, indent=2))
finally:
    for proc in servers:
        proc.terminate()
    for proc in servers:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
