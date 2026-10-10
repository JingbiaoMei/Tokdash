#!/usr/bin/env python3
"""Overview contention across native scopes, plus ordinary model-page reopen."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from urllib.parse import urlsplit, parse_qs

from benchmark_lazy_speed_review_browser import run
from benchmark_lazy_speed_review_fixes import backup
from speed_refresh_browser import Server, BROWSER_ARGS
from output_speed_expansion_fixtures import codex_call


def reopen(pw, arm, src, common, out):
    out.mkdir(parents=True, exist_ok=True)
    data = out / 'data'
    for name in ('usage.sqlite3', 'output_speed.sqlite3'):
        backup(common / 'prepared' / name, data / name)
    server = Server(common / 'corpus', data, out / 'xdg', os.environ.get('TOKDASH_BENCH_PYTHON',sys.executable), src,
                    'codex,dsh,qwen_code,opencode,kilocode,mimo')
    browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda req: requests.append((time.perf_counter(), req.url)) if '/api/' in req.url else None)
    result = dict(arm=arm, measurements={}, checks=[], page_errors=errors, device=out.stat().st_dev)
    url = server.base + '/?tab=speed&range=thisYear'

    def speed_ready():
        page.wait_for_function("() => usageReportSpeedState.active && usageReportSpeedState.payload && !usageReportSpeedState.loading && !usageReportSpeedState.jobEvents && !usageReportSpeedState.payload.cache?.job_id && usageReportSpeedState.loadedKey===usageReportSpeedKey()", timeout=300000)

    def overview_ready():
        page.wait_for_function("() => lastUsageResponse && !updateInFlight && overviewActiveTimeState.status==='ready' && activityInsightsState.data && !activityInsightsState.promise && ['ready','empty'].includes(activityInsightsState.status)", timeout=300000)

    def measured(name, action, settle):
        started, cpu = time.perf_counter(), server.cpu_ms()
        action(); settle()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        peak = next(int(row.split()[1]) / 1024 for row in Path(f'/proc/{server.proc.pid}/status').read_text().splitlines() if row.startswith('VmHWM:'))
        selected = [url for ts, url in requests if ts >= started]
        with closing(sqlite3.connect(f'file:{data / "output_speed.sqlite3"}?mode=ro', uri=True)) as conn:
            worker = {key: value for key, value in conn.execute("SELECT key,value FROM speed_meta WHERE key LIKE 'last_job_%'")}
            jobs = [dict(id=row[0], state=row[1]) for row in conn.execute('SELECT id,state FROM speed_jobs')]
        result['measurements'][name] = dict(paint_ms=(time.perf_counter() - started) * 1000, server_cpu_ms=server.cpu_ms() - cpu,
                                             server_peak_rss_mb=peak, requests=selected, jobs=jobs, last_worker_telemetry=worker)
        if name != 'overview_after_append':
            assert any(urlsplit(url).path == '/api/output-speed' and parse_qs(urlsplit(url).query).get('cache_only', ['false'])[0] not in ('1', 'true') for url in selected), 'normal reopen did not make an ordinary ensure request'
            assert page.evaluate('() => usageReportSpeedState.payload.cache.state') == 'ready'
        return page.evaluate('() => usageReportSpeedState.payload.rows') if name != 'overview_after_append' else None

    # Restore the shared fixture before the next arm, keeping its indexed input
    # signatures equal to its prepared snapshot (no timestamps are invented).
    original = common / 'corpus/codex/sessions/2026/09/01/rollout-bench.jsonl'
    stat, contents = original.stat(), original.read_bytes()
    try:
        first = measured('normal_first_build_paint', lambda: page.goto(url, wait_until='load'), speed_ready)
        second = measured('normal_cached_reopen_paint', lambda: page.reload(wait_until='load'), speed_ready)
        assert first == second
        page.screenshot(path=str(out / 'normal-cached-reopen.png'), full_page=True)
        page.click('a[data-tab-target="overview"]'); overview_ready()
        with original.open('a') as stream:
            stream.write(''.join(json.dumps(row) + '\n' for row in codex_call(20)))
        measured('overview_after_append', lambda: page.click('#refreshBtn'), overview_ready)
        close = page.locator('#refreshReportClose')
        if close.is_visible(): close.click()
        after = measured('normal_reopen_after_append_paint', lambda: page.goto(url, wait_until='load'), speed_ready)
        old_codex = sum(row['speed_calls'] for row in first if row['source'] == 'codex')
        new_codex = sum(row['speed_calls'] for row in after if row['source'] == 'codex')
        assert new_codex == old_codex + 1
        assert not any('speed' in urlsplit(url).path for url in result['measurements']['overview_after_append']['requests'])
        assert not errors
        result['checks'] = [dict(name='ordinary speed requests (no route override)', ok=True),
                            dict(name='normal reopen after append publishes exactly one added response', ok=True),
                            dict(name='Overview after append makes no speed requests', ok=True),
                            dict(name='no JavaScript errors', ok=True)]
        page.screenshot(path=str(out / 'normal-after-append.png'), full_page=True)
    finally:
        browser.close(); server.stop()
        original.write_bytes(contents)
        import os
        os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        (out / 'result.json').write_text(json.dumps(result, indent=2))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'fixed', 'common', 'out'): ap.add_argument('--' + name, type=Path, required=True)
    ap.add_argument('--scope-counts', type=int, nargs='+', default=[1, 1000, 10000])
    ap.add_argument('--pairs', type=int, default=3); ap.add_argument('--reps', type=int, default=1)
    args = ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    results, normal, orders = [], [], []
    with sync_playwright() as pw:
        for scopes in args.scope_counts:
            common = args.common / f'scopes-{scopes}'
            for pair in range(args.pairs):
                order = ['baseline', 'fixed'] if pair % 2 == 0 else ['fixed', 'baseline']
                orders.append(dict(scopes=scopes, pair=pair, order=order))
                for arm in order:
                    src = args.baseline if arm == 'baseline' else args.fixed
                    result = run(pw, arm, src, common, args.out / f'scopes-{scopes}-pair-{pair}-{arm}', args.reps)
                    result['scopes'] = scopes; results.append(result)
                    if scopes == max(args.scope_counts):
                        sample = reopen(pw, arm, src, common, args.out / f'normal-pair-{pair}-{arm}')
                        sample['scopes'] = scopes; normal.append(sample)
    assert all(r['accounting'] == results[0]['accounting'] for r in results)
    def spread(values): return dict(median=statistics.median(values), min=min(values), max=max(values), n=len(values))
    summary = {}
    for scopes in args.scope_counts:
        summary[str(scopes)] = {}
        for arm in ('baseline', 'fixed'):
            selected = [r for r in results if r['arm'] == arm and r['scopes'] == scopes]
            summary[str(scopes)][arm] = {name: {field: spread([sample[field] for r in selected for sample in r['measurements'][name]]) for field in ('paint_ms', 'server_cpu_ms', 'server_peak_rss_mb') if field in selected[0]['measurements'][name][0]} for name in selected[0]['measurements']}
    normal_summary = {}
    for arm in ('baseline', 'fixed'):
        selected = [r for r in normal if r['arm'] == arm]
        normal_summary[arm] = {name: {field: spread([r['measurements'][name][field] for r in selected]) for field in ('paint_ms', 'server_cpu_ms', 'server_peak_rss_mb')} for name in selected[0]['measurements']}
    paired = {}
    for scopes in args.scope_counts:
        differences = []
        for pair in range(args.pairs):
            # Results are recorded consecutively in alternating arm order.
            block = [r for r in results if r['scopes'] == scopes][pair * 2:pair * 2 + 2]
            samples = {r['arm']: r for r in block}
            differences.append(samples['fixed']['measurements']['overview_during_session_build'][0]['paint_ms'] - samples['baseline']['measurements']['overview_during_session_build'][0]['paint_ms'])
        paired[str(scopes)] = spread(differences)
    (args.out / 'benchmark.json').write_text(json.dumps(dict(orders=orders, results=results, summary=summary, normal_reopen=normal,
                                                           normal_summary=normal_summary, paired_overview_worker_ms=paired,
                                                           interpretation='Actual fixture refresh/paint. Ordinary reopen may build; no cache_only route override. Real-history snapshot-only results remain separate.'), indent=2))


if __name__ == '__main__': main()
