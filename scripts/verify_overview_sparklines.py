"""Check Overview sparklines and compare browser requests/latency with a checkout.

Run from the repository root using a Python with Playwright installed:
  python scripts/verify_overview_sparklines.py --baseline /path/to/reference
The two servers use dense synthetic fixtures and separate output/ data folders.
"""
import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import time
import threading
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline', required=True, type=Path)
parser.add_argument('--repeats', type=int, default=24)
parser.add_argument('--output', type=Path, default=Path('output/sparkline-validation'))
parser.add_argument('--port', type=int, default=55791)
parser.add_argument('--server-python', default='python3')
parser.add_argument('--performance-only', action='store_true', help='Skip preset screenshots already checked by a previous run')
args = parser.parse_args()
if args.repeats < 1:
    parser.error('--repeats must be positive')
if not 1 <= args.port < 65535:
    parser.error('--port must leave room for two consecutive ports')
ROOT = Path.cwd()
BASELINE = args.baseline.resolve()
OUT = args.output.resolve()
OUT.mkdir(parents=True, exist_ok=True)
REPEATS = args.repeats
servers=[]

def start(repo, port, label):
    env=dict(os.environ,PYTHONPATH=str(repo/'src'),TOKDASH_DATA_DIR=str(OUT/(label+'-data')))
    proc=subprocess.Popen([args.server_python,'main.py','--no-open','--dev-fixture','dense','--dev-seed','17','--port',str(port)],cwd=repo,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    servers.append(proc)
    ready = threading.Event()
    startup = []
    def drain():
        with (OUT / (label + '-server.log')).open('w') as log:
            for line in proc.stdout:
                log.write(line)
                if len(startup) < 30:
                    startup.append(line)
                if 'Uvicorn running on' in line:
                    ready.set()
    threading.Thread(target=drain, daemon=True).start()
    if not ready.wait(20):
        raise RuntimeError('Server did not become ready: ' + ''.join(startup))
    return 'http://127.0.0.1:'+str(port)

METRICS=['sparklineTokens','sparklineCost','sparklineMessages','sparklineActiveTime','sparklineCacheRate','sparklineTopModel']
SETTLED="""() => lastUsageResponse && overviewActiveTimeState.status === 'ready' && statsCache.default && !updateInFlight"""

def capture(page):
    return page.evaluate("""ids => ({curves:ids.map(id=>({id,reason:document.getElementById(id).dataset.reason,points:document.getElementById(id).dataset.points,granularity:document.getElementById(id).dataset.granularity,path:document.getElementById(id+'Path').getAttribute('d'),label:document.getElementById(id).getAttribute('aria-label')})),totals:[lastUsageResponse.total_tokens,lastUsageResponse.total_cost,lastUsageResponse.total_messages,overviewActiveTimeState.data.active_ms_sum]})""",METRICS)

try:
    urls={'baseline':start(BASELINE,args.port,'baseline'),'candidate':start(ROOT,args.port+1,'candidate')}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--disable-features=Vulkan,VizDisplayCompositor'])
        context=browser.new_context(viewport={'width':1440,'height':1050},reduced_motion='reduce',service_workers='block')
        context.add_init_script("""window.__sparklineLongTasks = [];
          new PerformanceObserver(list => window.__sparklineLongTasks.push(...list.getEntries().map(
            entry => ({start_ms:entry.startTime,duration_ms:entry.duration})))).observe({type:'longtask',buffered:true});""")
        errors=[]
        pages={key:context.new_page() for key in urls}
        for page in pages.values():
            page.on('pageerror',lambda error:errors.append(str(error)))
        initial={};screens=[]
        for label,page in pages.items():
            page.bring_to_front()
            page.goto(urls[label]);page.wait_for_function(SETTLED,timeout=15000)
            initial[label]=capture(page)
        assert initial['candidate']['totals'][:3]==initial['baseline']['totals'][:3], initial
        # Agent fixture magnitudes scale with elapsed time today; servers answer at different instants.
        assert abs(initial['candidate']['totals'][3]-initial['baseline']['totals'][3]) < initial['baseline']['totals'][3]*.005, initial
        presets = [] if args.performance_only else [('today','hour'),('yesterday','hour'),('last7days','day'),('lastWeek','day'),('thisMonth','day'),('lastMonth','day')]
        for selection,granularity in presets:
            page=pages['candidate']
            page.locator(f'#overviewQuickRanges [data-range="{selection}"]').click()
            page.wait_for_function("""() => !updateInFlight && overviewActiveTimeState.status === 'ready' && overviewActiveTimeState.key === activeTimeRequestKey(null,formatDateKey(currentStartDate),formatDateKey(currentEndDate))""",timeout=15000)
            page.wait_for_function("""ids => ids.every(id=>document.getElementById(id).dataset.reason === 'drawn')""",arg=METRICS,timeout=8000)
            snap=capture(page)
            assert all(row['granularity']==granularity and row['path'] for row in snap['curves']),snap
            path=OUT/(selection+'.png');page.screenshot(path=str(path),full_page=True,timeout=8000)
            screens.append({'selection':selection,'screenshot':str(path.relative_to(ROOT)),'data':snap})
        # Reset to Today before comparing reloads with identical endpoint selections.
        pages['candidate'].locator('#overviewQuickRanges [data-range="today"]').click()
        pages['candidate'].wait_for_function(SETTLED,timeout=15000)
        samples={label:[] for label in pages}
        requests={label:[] for label in pages}
        for label,page in pages.items():
            page.on('request',lambda request,key=label:requests[key].append(request.url.split('/api/',1)[1]) if '/api/' in request.url else None)
        request_sets={label:[] for label in pages}
        diagnostics={label:[] for label in pages}
        for i in range(REPEATS):
            for label in (['baseline','candidate'] if i%2==0 else ['candidate','baseline']):
                page=pages[label]
                # Both versions must paint as the foreground tab; background-tab
                # throttling otherwise makes this an unequal rendering comparison.
                page.bring_to_front()
                requests[label].clear();t=time.perf_counter()
                page.reload(wait_until='domcontentloaded');page.wait_for_function(SETTLED,timeout=15000)
                settled_ms=(time.perf_counter()-t)*1000
                page.wait_for_load_state('networkidle')
                samples[label].append((time.perf_counter()-t)*1000)
                request_sets[label].append(dict(Counter(requests[label])))
                diagnostics[label].append(page.evaluate("""settled => ({settled_ms:settled,
                  long_tasks:window.__sparklineLongTasks,resources:performance.getEntriesByType('resource')
                    .filter(entry=>entry.name.includes('/api/')).map(entry=>({path:entry.name.split('/api/')[1],
                      start_ms:entry.startTime,duration_ms:entry.duration,bytes:entry.encodedBodySize}))})""",settled_ms))
        summaries={key:{'median_ms':statistics.median(values),'p95_ms':sorted(values)[math.ceil(len(values)*.95)-1]} for key,values in samples.items()}
        budget=max(10,summaries['baseline']['p95_ms']*.1)
        report={'repeats':REPEATS,'samples':samples,'summary':summaries,'budget_ms':budget,
                'latency_pass':summaries['candidate']['p95_ms']<=summaries['baseline']['p95_ms']+budget,
                'requests_equal':request_sets['baseline']==request_sets['candidate'],'requests':request_sets,'diagnostics':diagnostics,'errors':errors,'screens':screens,'baseline':str(BASELINE),'candidate':str(ROOT),
                'method':'alternating baseline/candidate reloads; both in foreground; same seed and endpoint selection',
                'gate':'candidate p95 <= baseline p95 + max(10 ms, 10% of baseline p95)'}
        (OUT/'browser-performance.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({key:value for key,value in report.items() if key not in ['samples','requests','screens','diagnostics']},indent=2))
        assert not errors
        assert report['requests_equal']
        assert report['latency_pass']
        # Responsive card paths must remain inside their SVG bounds.
        page=pages['candidate'];page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(OUT/'mobile.png'),full_page=True,timeout=8000)
        browser.close()
finally:
    for proc in servers:
        proc.terminate()
    for proc in servers:
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
