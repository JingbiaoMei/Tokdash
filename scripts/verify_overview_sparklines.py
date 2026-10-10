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
import signal
import statistics
import subprocess
import time
import threading
from playwright.sync_api import sync_playwright

PRESETS=['today','yesterday','last7days','lastWeek','last14days','last4weeks','thisMonth','lastMonth','thisYear','lastYear']
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline', required=True, type=Path)
parser.add_argument('--repeats', type=int, default=32)
parser.add_argument('--output', type=Path, default=Path('output/sparkline-validation'))
parser.add_argument('--port', type=int, default=55791)
parser.add_argument('--server-python', default='python3')
parser.add_argument('--visual-only', action='store_true', help='Check every preset and responsive curves without repeated timings')
parser.add_argument('--performance-only', action='store_true', help='Skip preset screenshots already checked by a previous run')
parser.add_argument('--presets', nargs='+', choices=PRESETS, default=PRESETS, help='Bounded diagnostic subset; default covers all presets')
parser.add_argument('--profile', action='store_true', help='Record synchronous dashboard function durations for diagnosis')
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
REVISIONS={label:subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    for label,repo in [('baseline',BASELINE),('candidate',ROOT)]}

def interrupted(_signum, _frame):
    # Run finally on an interrupted benchmark, so its isolated fixtures close.
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, interrupted)

def start(repo, port, label):
    env=dict(os.environ,PYTHONPATH=str(repo/'src'),TOKDASH_DATA_DIR=str(OUT/(label+'-data')))
    proc=subprocess.Popen([args.server_python,'main.py','--no-open','--dev-fixture','dense','--dev-seed','17','--port',str(port)],cwd=repo,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    servers.append(proc)
    startup_done = threading.Event()
    ready = False
    startup = []
    def drain():
        nonlocal ready
        with (OUT / (label + '-server.log')).open('w') as log:
            for line in proc.stdout:
                log.write(line)
                if len(startup) < 30:
                    startup.append(line)
                if 'Uvicorn running on' in line:
                    ready = True
                    startup_done.set()
        startup_done.set()
    threading.Thread(target=drain, daemon=True).start()
    if not startup_done.wait(20) or not ready:
        raise RuntimeError('Server did not become ready: ' + ''.join(startup))
    return 'http://127.0.0.1:'+str(port)

SELECTED_PRESETS=args.presets
METRICS=['sparklineTokens','sparklineCost','sparklineMessages','sparklineActiveTime','sparklineCacheRate','sparklineTopModel']
SETTLED="""() => lastUsageResponse && overviewActiveTimeState.status === 'ready' && statsCache.default && !updateInFlight"""

def capture(page):
    return page.evaluate("""ids => ({curves:ids.map(id=>({id,reason:document.getElementById(id).dataset.reason,points:document.getElementById(id).dataset.points,granularity:document.getElementById(id).dataset.granularity,path:document.getElementById(id+'Path').getAttribute('d'),label:document.getElementById(id).getAttribute('aria-label')})),totals:[lastUsageResponse.total_tokens,lastUsageResponse.total_cost,lastUsageResponse.total_messages,overviewActiveTimeState.data.active_ms_sum]})""",METRICS)

def select(page, selection):
    button = page.locator(f'#overviewQuickRanges [data-range="{selection}"]')
    if not button.is_visible():
        page.locator('#quickRangeMoreToggle').click()
    button.click()
    page.wait_for_function("""selection => activeQuickRange === selection && !updateInFlight &&
      overviewActiveTimeState.status === 'ready' && overviewActiveTimeState.key ===
      activeTimeRequestKey(null,formatDateKey(currentStartDate),formatDateKey(currentEndDate))""",
      arg=selection, timeout=15000)


def summary(values):
    return {'median_ms':statistics.median(values), 'p95_ms':sorted(values)[math.ceil(len(values)*.95)-1]}


try:
    urls={'baseline':start(BASELINE,args.port,'baseline'),'candidate':start(ROOT,args.port+1,'candidate')}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-gpu','--disable-features=Vulkan,VizDisplayCompositor'])
        context=browser.new_context(viewport={'width':1440,'height':1050},reduced_motion='reduce',service_workers='block')
        context.add_init_script("""window.__sparklineLongTasks = [];
          new PerformanceObserver(list => window.__sparklineLongTasks.push(...list.getEntries().map(
            entry => ({start_ms:entry.startTime,duration_ms:entry.duration})))).observe({type:'longtask',buffered:true});""")
        if args.profile:
            context.add_init_script("""window.__sparklineFunctionTimes=[];
              document.addEventListener('DOMContentLoaded',()=>{
                for(const name of ['renderOverviewTab','renderOverviewSparklines','deriveSparklineCurves',
                  'sparklineShape','fitKpiValue','fitOverviewKpis','renderOverviewProfilePreview',
                  'renderOverviewActiveTime','updateTokenCompositionBar','updateToolChart','updateModelChart',
                  'updateAppsBreakdown','updateCombinedModelsTable','updateToolsTable','renderYearHeatmap',
                  'renderMonthHeatmap','buildOverviewProfilePreview','renderOverviewActivityInsights',
                  'reconcileTodayProfileContribution','combineUsagePayloads']) {
                  const original=window[name]; if(typeof original!=='function')continue;
                  window[name]=function(...args){const start=performance.now();try{return original.apply(this,args)}
                    finally{window.__sparklineFunctionTimes.push({name,duration_ms:performance.now()-start})}};
                }
              });""")
        errors=[]
        pages={key:context.new_page() for key in urls}
        sessions={key:context.new_cdp_session(page) for key,page in pages.items()}
        for session in sessions.values():
            session.send('Performance.enable')
        def browser_metrics(label):
            return {row['name']:row['value'] for row in sessions[label].send('Performance.getMetrics')['metrics']}
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
        present=pages['candidate'].locator('#overviewQuickRanges [data-range]').evaluate_all(
            "buttons => buttons.map(button=>button.dataset.range)")
        assert set(present)==set(PRESETS), {'uncovered_presets':set(present)-set(PRESETS)}
        if not args.performance_only:
            for width,height in [(1440,1050),(390,844)]:
                page=pages['candidate'];page.set_viewport_size({'width':width,'height':height})
                page.bring_to_front()
                for selection in SELECTED_PRESETS:
                    select(page,selection)
                    page.wait_for_function("""ids => ids.every(id=>document.getElementById(id).dataset.reason === 'drawn')""",
                        arg=METRICS,timeout=8000)
                    snap=capture(page)
                    expected=page.evaluate("""() => {
                      const days=Math.round((Date.UTC(currentEndDate.getFullYear(),currentEndDate.getMonth(),currentEndDate.getDate())-
                        Date.UTC(currentStartDate.getFullYear(),currentStartDate.getMonth(),currentStartDate.getDate()))/86400000)+1;
                      return days===1?'hour':days<=31?'day':'month';
                    }""")
                    assert all(row['granularity']==expected and row['path'] for row in snap['curves']),snap
                    bounds=page.evaluate("""ids => { const rect=r=>({x:r.x,y:r.y,width:r.width,height:r.height});
                      return ids.map(id=>({id,box:rect(document.getElementById(id+'Path').getBBox()),
                      viewport:rect(document.getElementById(id).viewBox.baseVal)})); }""",METRICS)
                    for row in bounds:
                        box,view=row['box'],row['viewport']
                        assert box['x']>=view['x']-1 and box['y']>=view['y']-1, row
                        assert box['x']+box['width']<=view['x']+view['width']+1, row
                        assert box['y']+box['height']<=view['y']+view['height']+1, row
                    path=OUT/(selection+('-mobile' if width==390 else '')+'.png')
                    page.screenshot(path=str(path),full_page=width==390,timeout=8000)
                    screens.append({'selection':selection,'width':width,'screenshot':str(path.relative_to(ROOT)),'data':snap})
            pages['candidate'].set_viewport_size({'width':1440,'height':1050})
        (OUT/'browser-visual.json').write_text(json.dumps({'screens':screens,'errors':errors,'presets':present},indent=2)+'\n')
        if not args.visual_only:
            samples={selection:{label:[] for label in pages} for selection in SELECTED_PRESETS}
            settled_samples={selection:{label:[] for label in pages} for selection in SELECTED_PRESETS}
            requests={label:[] for label in pages}
            for label,page in pages.items():
                page.on('request',lambda request,key=label:requests[key].append(request.url.split('/api/',1)[1]) if '/api/' in request.url else None)
            request_sets={selection:{label:[] for label in pages} for selection in SELECTED_PRESETS}
            diagnostics={selection:{label:[] for label in pages} for selection in SELECTED_PRESETS}
            for i in range(REPEATS):
                for selection in SELECTED_PRESETS:
                    for label in (['baseline','candidate'] if i%2==0 else ['candidate','baseline']):
                        page=pages[label];page.bring_to_front()
                        # Every range change starts from a freshly loaded Today page.
                        # Today measures a full reload; other ranges measure actual
                        # menu selection after the same initial page has settled.
                        if selection!='today':
                            page.reload(wait_until='domcontentloaded');page.wait_for_function(SETTLED,timeout=15000)
                            page.wait_for_load_state('networkidle')
                        page.evaluate("performance.clearResourceTimings();window.__sparklineLongTasks=[];window.__sparklineFunctionTimes=[]")
                        requests[label].clear()
                        before_metrics=browser_metrics(label)
                        t=time.perf_counter()
                        if selection=='today':
                            page.reload(wait_until='domcontentloaded');page.wait_for_function(SETTLED,timeout=15000)
                        else:
                            select(page,selection)
                        settled_ms=(time.perf_counter()-t)*1000
                        page.wait_for_load_state('networkidle')
                        samples[selection][label].append((time.perf_counter()-t)*1000)
                        settled_samples[selection][label].append(settled_ms)
                        request_sets[selection][label].append(dict(Counter(requests[label])))
                        after_metrics=browser_metrics(label)
                        timings=page.evaluate("""settled => ({settled_ms:settled,
                          long_tasks:window.__sparklineLongTasks,function_times:window.__sparklineFunctionTimes,resources:performance.getEntriesByType('resource')
                            .filter(entry=>entry.name.includes('/api/')).map(entry=>({path:entry.name.split('/api/')[1],
                              start_ms:entry.startTime,duration_ms:entry.duration,bytes:entry.encodedBodySize}))})""",settled_ms)
                        # Navigation resets Chromium's document CPU counters.
                        # A reload uses the new document's total; a range switch
                        # stays in one document and uses the counter difference.
                        timings['browser_cpu_ms']={key:(after_metrics[key] -
                            (0 if selection=='today' else before_metrics[key]))*1000
                            for key in ('TaskDuration','ScriptDuration','LayoutDuration','RecalcStyleDuration')}
                        assert all(value>=0 for value in timings['browser_cpu_ms'].values()), timings
                        timings['js_heap_bytes']=after_metrics['JSHeapUsedSize']
                        diagnostics[selection][label].append(timings)
                print(f'Completed paired browser iteration {i+1}/{REPEATS}',flush=True)
            cases={}
            for selection in SELECTED_PRESETS:
                before,after=summary(samples[selection]['baseline']),summary(samples[selection]['candidate'])
                budget=max(10,before['p95_ms']*.1)
                settled_before=summary(settled_samples[selection]['baseline'])
                settled_after=summary(settled_samples[selection]['candidate'])
                settled_budget=max(10,settled_before['p95_ms']*.1)
                cases[selection]={'baseline':before,'candidate':after,'budget_ms':budget,
                    'settled_baseline':settled_before,'settled_candidate':settled_after,
                    'settled_budget_ms':settled_budget,
                    'settled_pass':settled_after['p95_ms']<=settled_before['p95_ms']+settled_budget,
                    'latency_pass':after['p95_ms']<=before['p95_ms']+budget,
                    'requests_equal':request_sets[selection]['baseline']==request_sets[selection]['candidate']}
            report={'repeats_per_range':REPEATS,'cases':cases,'samples':samples,'settled_samples':settled_samples,
                'revisions':REVISIONS,
                'requests':request_sets,'diagnostics':diagnostics,'errors':errors,'screens':screens,
                'baseline':str(BASELINE),'candidate':str(ROOT),
                'method':'alternating foreground tabs; same seed; Today reload and all nine range selections from fresh Today; latency includes network-idle, settled timings recorded separately',
                'browser_cpu_method':'fresh-document counters for Today navigation; counter deltas for in-document range selection',
                'gate':'candidate p95 <= baseline p95 + max(10 ms, 10% of baseline p95)'}
            (OUT/'browser-performance.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({'cases':cases,'errors':errors},indent=2))
            assert all(row['requests_equal'] and row['latency_pass'] and row['settled_pass'] for row in cases.values()),cases
        assert not errors, errors
        browser.close()
finally:
    for proc in servers:
        proc.terminate()
    for proc in servers:
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
