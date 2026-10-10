#!/usr/bin/env python3
"""Actual Overview refresh during a scoped worker and cached speed-table paints."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
import urllib.request

from speed_refresh_browser import Server,BROWSER_ARGS
from benchmark_lazy_speed_review_fixes import backup

ROOT=Path(__file__).resolve().parents[1]


def run(pw,arm,src,common,out,reps):
    data=out/'data';out.mkdir(parents=True,exist_ok=True)
    for name in ('usage.sqlite3','output_speed.sqlite3'):
        backup(common/'prepared'/name,data/name)
    server=Server(common/'corpus',data,out/'xdg',os.environ.get('TOKDASH_BENCH_PYTHON',sys.executable),src,'codex,dsh,qwen_code,opencode,kilocode,mimo')
    browser=pw.chromium.launch(headless=True,args=BROWSER_ARGS)
    page=browser.new_page(viewport={'width':1440,'height':1100},reduced_motion='reduce')
    result=dict(arm=arm,device=out.stat().st_dev,src_device=src.stat().st_dev,measurements={},checks=[],page_errors=[])
    requests=[]
    page.on('request',lambda r:requests.append((time.perf_counter(),r.url)) if '/api/' in r.url else None)
    page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
    def paint():page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    def overview():
        page.wait_for_function('''() => lastUsageResponse && !updateInFlight && overviewActiveTimeState.status==='ready'
            && overviewActiveTimeState.data && ['ready','empty'].includes(activityInsightsState.status)
            && activityInsightsState.data && !activityInsightsState.promise
            && (!window.reviewRefreshBefore || (activityInsightsState.data!==window.reviewRefreshBefore.activity
              && overviewActiveTimeState.data!==window.reviewRefreshBefore.active))''',timeout=300000)
    def speed():page.wait_for_function("() => !usageReportSpeedState.loading && usageReportSpeedState.loadedKey===usageReportSpeedKey() && !usageReportSpeedState.payload?.cache?.job_id",timeout=300000)
    def refresh():
        page.evaluate('() => {window.reviewRefreshBefore={activity:activityInsightsState.data,active:overviewActiveTimeState.data};}')
        page.click('#refreshBtn')
    def close_report():
        close=page.locator('#refreshReportClose')
        if close.is_visible():close.click()
    def measure(name,action,settle,range_action=False):
        start=time.perf_counter();cpu=server.cpu_ms();action()
        if range_action:
            paint();result['measurements'].setdefault(name+'_control',[]).append({'paint_ms':(time.perf_counter()-start)*1000})
        settle();paint()
        rss=next(int(s.split()[1])/1024 for s in Path(f'/proc/{server.proc.pid}/status').read_text().splitlines() if s.startswith('VmHWM:'))
        paths=[url.split('?')[0].replace(server.base,'') for ts,url in requests if ts>=start]
        sample=dict(paint_ms=(time.perf_counter()-start)*1000,server_cpu_ms=server.cpu_ms()-cpu,server_peak_rss_mb=rss,paths=paths)
        result['measurements'].setdefault(name,[]).append(sample);close_report();return sample
    def terminal(job):
        with urllib.request.urlopen(server.base+f'/api/output-speed/jobs/{job}/events',timeout=300) as events:
            for line in events:
                if line.startswith(b'data:'):
                    status=json.loads(line[5:])
                    if status['state'] in ('ready','error'):return status
        raise RuntimeError('job ended without terminal event')
    def ensure(body):
        req=urllib.request.Request(server.base+'/api/output-speed/ensure',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)['cache']['job_id']
    def range_button(key):
        button=page.locator(f'.quick-range-btn[data-range="{key}"]')
        if not button.is_visible():page.locator('#quickRangeMoreToggle').click()
        button.click()
    try:
        measure('overview_first',lambda:page.goto(server.base+'/?tab=overview&range=thisYear',wait_until='load'),overview)
        result['accounting']=page.evaluate('() => ({total_tokens:lastUsageResponse.total_tokens,total_cost:lastUsageResponse.total_cost,total_messages:lastUsageResponse.total_messages,by_tool:lastUsageResponse.by_tool})')
        for _ in range(reps):
            sample=measure('overview_warm',refresh,overview)
            result['checks'].append(dict(name='Overview refresh zero speed requests',ok=not any('speed' in p for p in sample['paths'])))
        job=ensure(dict(date_from='2026-09-01',date_to='2026-09-28',sessions=[dict(tool='codex',session_id='short-session')]))
        assert job
        started=time.time()
        sample=measure('overview_during_session_build',refresh,overview)
        finished=terminal(job)
        result['overlap']=dict(refresh_started_at=started,job_finished_at=finished['updated_at'],job_state=finished['state'])
        result['checks'].append(dict(name='worker overlaps Overview refresh',ok=finished['state']=='ready' and finished['updated_at']>started))
        result['checks'].append(dict(name='Overview during worker zero speed requests',ok=not any('speed' in p for p in sample['paths'])))
        # Model work completes explicitly before cached browser range comparisons.
        with urllib.request.urlopen(server.base+'/api/output-speed?date_from=2026-01-01&date_to=2026-12-31',timeout=30) as response:
            model=json.load(response)
        if model['cache'].get('job_id'):assert terminal(model['cache']['job_id'])['state']=='ready'
        measure('model_first_paint',lambda:page.click('a[data-tab-target="speed"]'),speed)
        for _ in range(reps):
            for key in ('lastMonth','thisYear'):
                sample=measure('model_range_paint',lambda key=key:range_button(key),speed,range_action=True)
                result['checks'].append(dict(name='range reads no ordinary reports',ok=not any(p in ('/api/usage','/api/insights','/api/active-time') for p in sample['paths'])))
        page.screenshot(path=str(out/'model-speed.png'),full_page=True)
        measure('hour_paint',lambda:page.click('[data-speed-mode="temporal"]'),lambda:page.wait_for_function('() => usageReportSpeedState.temporal && !usageReportSpeedState.temporalLoading && !usageReportSpeedState.temporal.cache?.job_id',timeout=300000))
        page.screenshot(path=str(out/'hour-speed.png'),full_page=True)
        after=page.evaluate('() => ({total_tokens:lastUsageResponse.total_tokens,total_cost:lastUsageResponse.total_cost,total_messages:lastUsageResponse.total_messages,by_tool:lastUsageResponse.by_tool})')
        result['checks'].append(dict(name='usage and cost totals preserved',ok=after==result['accounting']))
        result['checks'].append(dict(name='no JavaScript errors',ok=not result['page_errors']))
        assert all(check['ok'] for check in result['checks']),result['checks']
    finally:
        (out/'result.json').write_text(json.dumps(result,indent=2));browser.close();server.stop()
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--baseline',type=Path,required=True);ap.add_argument('--fixed',type=Path,required=True);ap.add_argument('--common',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--pairs',type=int,default=3);ap.add_argument('--reps',type=int,default=3)
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright
    results=[];orders=[]
    with sync_playwright() as pw:
        for pair in range(args.pairs):
            order=['baseline','fixed'] if pair%2==0 else ['fixed','baseline'];orders.append(order)
            for arm in order:
                results.append(run(pw,arm,args.baseline if arm=='baseline' else args.fixed,args.common,args.out/f'pair-{pair}-{arm}',args.reps))
    assert all(r['accounting']==results[0]['accounting'] for r in results), 'paired public accounting drift'
    summary={}
    for arm in ('baseline','fixed'):
        summary[arm]={}
        for name in results[0]['measurements']:
            samples=[sample for r in results if r['arm']==arm for sample in r['measurements'][name]]
            summary[arm][name]={field:dict(median=statistics.median([s[field] for s in samples]),min=min(s[field] for s in samples),max=max(s[field] for s in samples),n=len(samples)) for field in ('paint_ms','server_cpu_ms','server_peak_rss_mb') if field in samples[0]}
    (args.out/'benchmark.json').write_text(json.dumps(dict(orders=orders,results=results,summary=summary),indent=2))
if __name__=='__main__':main()
