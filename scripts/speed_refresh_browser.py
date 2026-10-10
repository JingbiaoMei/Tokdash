#!/usr/bin/env python3
"""What the standalone speed page asks the server for, measured in a browser.

    python3 scripts/speed_refresh_browser.py --server-python python3

Endpoint timings say how fast one request runs. They cannot say what a page load
cost, because the cost of a dashboard page is the sum of the requests it decided to
make -- including the ones made for a panel nobody is looking at. So this drives the
real page against a throwaway server on a frozen fixture corpus and counts them.

Five things are measured, and the first four are assertions:

  1. A Refresh pressed on the Overview tab, after the reader has visited the speed
     subtab earlier in the same page life, asks for no speed data at all.
  2. A Refresh pressed on the speed page synchronises first and reads speed
     second, so the numbers shown are the ones the synchronise made possible --
     proven with a real log append between the two reads.
  3. Sitting on the speed page costs no summary work: no facet scan, no active-time
     merge, because both fill the other subtab.
  4. Leaving the Report tab in the middle of a load stops the follow-up work. Not
     "hides it": no request is issued after the switch.
  5. What a visible speed table costs: the time from the click to the first row
     painted, the requests behind it, their median and p95, and the CPU the server
     spent. Reported, not asserted -- this is the number a change is judged against.

Server CPU is read from /proc, not from the client: a request that got behind
another one in the queue looks cheap to the browser and expensive to the machine.

Writes output/agent-speed/benchmarks/refresh_browser_latest.json. Nothing outside
the work directory and that JSON is touched, and no live Tokdash data is read.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import speed_fixtures  # noqa: E402

BROWSER_ARGS = ["--disable-gpu", "--disable-features=Vulkan,VizDisplayCompositor"]
OUT_PATH = ROOT / "output" / "agent-speed" / "benchmarks" / "refresh_browser_latest.json"

#: The paths this page can ask for, named the way the page names them.
REPORT_PATHS = {
    "usage": "/api/usage",
    "insights": "/api/insights",
    "active": "/api/active-time",
    "speed": "/api/output-speed",
    "version": "/api/version",
}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """A tokdash server on the fixture corpus, with its CPU accounted separately."""

    def __init__(self, corpus: Path, data: Path, xdg: Path, python: str, src: Path = ROOT / "src", parsers='kimi,omp'):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = Path(data).parent / f"server-{Path(data).name}.log"
        Path(data).parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.log_path, "wb")
        env = dict(os.environ, **speed_fixtures.isolated_env(corpus, data, xdg))
        env["PYTHONPATH"] = str(src)
        self.proc = subprocess.Popen(
            [python, str(ROOT / "scripts" / "bench_tokdash_server.py"),
             "--src", str(src), "--host", "127.0.0.1", "--port", str(self.port),
             "--parsers", parsers],
            cwd=str(ROOT), env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 180
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early; see {self.log_path}")
            try:
                if self.get("/api/version")[0] == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            self.stop()
            raise RuntimeError("server never became ready")
        self._cpu_stop = False
        self._samples: list[tuple[float, int]] = []
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()

    def _sample(self):
        while not self._cpu_stop:
            self._samples.append((time.monotonic(), self.cpu_ms()))
            time.sleep(0.1)

    def cpu_ms(self) -> int:
        """Process CPU so far, in milliseconds (user + system, all threads)."""
        try:
            fields = Path(f"/proc/{self.proc.pid}/stat").read_text().split(") ", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
        except (OSError, IndexError, ValueError):
            return 0
        return int(ticks * 1000 / os.sysconf("SC_CLK_TCK"))

    def cpu_between(self, start_ms: int, now: bool = False) -> dict:
        end = self.cpu_ms()
        wall = None
        return {"cpu_ms": max(0, end - int(start_ms))}

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=300) as resp:
            return resp.status, resp.read()

    def stop(self):
        self._cpu_stop = True
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
        self.log.close()


def kimi_turn(lines: list, stamp_ms: int, usage: dict, *, step: int, decode_ms: int) -> None:
    """One complete turn: open, close carrying the usage, and the row that bills it."""
    lines.append(json.dumps({
        "type": "context.append_loop_event", "agentId": "main", "time": stamp_ms,
        "event": {"type": "step.begin", "uuid": f"live-{step}", "turnId": str(step),
                  "step": step}}))
    lines.append(json.dumps({
        "type": "context.append_loop_event", "agentId": "main", "time": stamp_ms + decode_ms,
        "event": {"type": "step.end", "uuid": f"live-{step}", "turnId": str(step),
                  "step": step, "usage": usage, "finishReason": "end_turn",
                  "llmServerDecodeMs": decode_ms, "llmStreamDurationMs": decode_ms + 40}}))
    lines.append(json.dumps({
        "type": "usage.record", "agentId": "main", "time": stamp_ms + decode_ms + 1,
        "model": "bench/live-model", "usage": usage, "usageScope": "turn"}))


def summarise(times: list[float]) -> dict:
    clean = sorted(float(v) for v in times if v is not None)
    if not clean:
        return {"n": 0}
    pick = lambda q: clean[min(len(clean) - 1, max(0, int(round(q * (len(clean) - 1)))))]
    return {"n": len(clean), "min": round(clean[0], 1), "median": round(pick(0.5), 1),
            "p95": round(pick(0.95), 1), "max": round(clean[-1], 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workdir", default="output/output-speed-refresh")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--server-python", default=os.environ.get('TOKDASH_BENCH_PYTHON', sys.executable))
    ap.add_argument("--src", type=Path, default=ROOT / "src")
    ap.add_argument("--expanded-calls", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=5, help="measured speed-table loads")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    work = Path(args.workdir)
    if work.exists():
        subprocess.run(["rm", "-rf", str(work)], check=False)
    corpus = work / "corpus"
    (corpus).mkdir(parents=True, exist_ok=True)
    speed_fixtures.build(corpus, scale="medium", days=21,
        base=datetime(2026,9,1,tzinfo=timezone.utc) if args.expanded_calls else None)
    parsers='kimi,omp'
    if args.expanded_calls:
        import output_speed_expansion_fixtures
        output_speed_expansion_fixtures.build(corpus,args.expanded_calls)
        parsers='kimi,omp,codex,dsh,qwen_code,opencode,kilocode,mimo'

    evidence: dict = {"generated_utc": datetime.now(timezone.utc).isoformat(),
                      "rounds": args.rounds, "checks": [], "scenarios": {}}

    def check(name: str, ok, detail: str = ""):
        evidence["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # The file a live turn will be appended to, so the refresh test is a real one.
    live_session = (corpus / "kimi-code" / "sessions" / "wd_bench_live"
                    / "session_live" / "agents" / "main")
    live_session.mkdir(parents=True, exist_ok=True)
    live_log = live_session / "wire.jsonl"

    server = Server(corpus, work / "data", work / "xdg", args.server_python, args.src.resolve(), parsers)
    requests: list[dict] = []
    t0 = time.monotonic()
    code = 1

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=BROWSER_ARGS)
        page = browser.new_page(viewport={"width": 1440, "height": 1000},
                                reduced_motion="reduce")

        def on_request(request):
            url = request.url
            path = url.split("?")[0].replace(server.base, "")
            if path.startswith("/api/"):
                requests.append({"t": (time.monotonic() - t0) * 1000.0,
                                 "path": path, "url": url})

        page.on("request", on_request)

        def mark() -> float:
            return (time.monotonic() - t0) * 1000.0

        def since(start_ms: float) -> list[dict]:
            return [r for r in requests if r["t"] >= start_ms]

        def counts(start_ms: float) -> dict:
            out = {name: 0 for name in REPORT_PATHS}
            for row in since(start_ms):
                for name, needle in REPORT_PATHS.items():
                    if row["path"].startswith(needle):
                        out[name] += 1
            return out

        def wait_report_idle(timeout_ms=300_000):
            page.wait_for_function(
                "() => !usageReportState.loading && !usageReportState.pendingPeriod"
                " && !usageReportState.pendingBack && !usageReportState.pendingServerId",
                timeout=timeout_ms)

        def wait_speed_settled(timeout_ms=300_000):
            page.wait_for_function(
                "() => !usageReportSpeedState.loading"
                " && usageReportSpeedState.loadedKey === usageReportSpeedKey()",
                timeout=timeout_ms)

        def speed_rows() -> int:
            return page.evaluate(
                "() => document.querySelectorAll('#usageReportSpeedBody tr').length")

        def open_speed():
            page.click('a[data-tab-target="speed"]')
            wait_speed_settled()

        try:
            # Ingestion is setup; the page benchmark below prices cache reads.
            server.get(f"/api/usage?date_from={datetime.now().year}-01-01&date_to={datetime.now().date()}")
            page_start=mark()
            page_cpu=server.cpu_ms()
            page.goto(server.base + "/?tab=speed&range=thisYear", wait_until="load")
            wait_speed_settled()
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            evidence['scenarios']['initial_page_paint_ms']=mark()-page_start
            evidence['scenarios']['initial_page_server_cpu_ms']=server.cpu_ms()-page_cpu

            def change_range(key):
                button = page.locator(f'.quick-range-btn[data-range="{key}"]')
                if not button.is_visible():
                    page.click('#quickRangeMoreToggle')
                button.click()

            paints, round_counts, cpu = [], [], []
            for index in range(args.rounds):
                before = mark()
                cpu_before = server.cpu_ms()
                change_range('thisMonth' if index % 2 == 0 else 'thisYear')
                wait_speed_settled()
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                paints.append(mark() - before)
                cpu.append(server.cpu_ms() - cpu_before)
                round_counts.append(counts(before))
            evidence["scenarios"]["speed_table"] = {
                "paint_ms": summarise(paints), "server_cpu_ms": summarise(cpu),
                "requests_per_round": round_counts,
            }
            check("speed ranges use one request each", all(r['speed'] == 1 for r in round_counts), round_counts)
            check("speed ranges do no usage, facet or active-time work",
                  all(r['usage'] == r['insights'] == r['active'] == 0 for r in round_counts), round_counts)

            change_range('thisYear')
            wait_speed_settled()
            # Hold the selected temporal workload constant across the two arms;
            # expanded native rows can otherwise become the default selection.
            page.select_option('#usageReportSpeedTool','kimi')
            chart=[]
            for index in range(args.rounds):
                start=mark();cpu_before=server.cpu_ms()
                page.click('[data-speed-mode="temporal"]')
                page.wait_for_function('() => !usageReportSpeedState.temporalLoading && !!usageReportSpeedState.temporal',timeout=300_000)
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                chart.append(dict(paint_ms=mark()-start,server_cpu_ms=server.cpu_ms()-cpu_before,requests=counts(start)))
                page.click('[data-speed-mode="across"]')
            evidence['scenarios']['temporal_chart']=chart
            check('first chart requests timing and cached returns reuse it',
                  chart[0]['requests']['speed']==1 and all(r['requests']['speed']==0 for r in chart[1:]),chart)
            page.select_option('#usageReportSpeedTool','')

            start = mark()
            page.click('a[data-tab-target="overview"]')
            page.click('a[data-tab-target="speed"]')
            wait_speed_settled()
            check("returning to a cached window issues no speed request", counts(start)['speed'] == 0, counts(start))

            # A manual speed refresh includes a real appended call.
            change_range('thisYear')
            wait_speed_settled()
            usage_before = page.evaluate("() => usageReportSpeedState.rows.reduce((n,r) => n+r.speed_calls,0)")
            stamp = int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp() * 1000)
            lines = []
            kimi_turn(lines, stamp, {"inputOther": 2000, "output": 900,
                                    "inputCacheRead": 5000, "inputCacheCreation": 0},
                      step=1, decode_ms=15000)
            with live_log.open('a', encoding='utf-8') as handle:
                handle.write('\n'.join(lines) + '\n')
            start = mark()
            cpu_before = server.cpu_ms()
            page.click('#refreshBtn')
            wait_speed_settled()
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            refresh_requests = since(start)
            spent = counts(start)
            order = {name: next((i for i,r in enumerate(refresh_requests)
                     if r['path'] == path), None) for name,path in REPORT_PATHS.items()}
            usage_after = page.evaluate("() => usageReportSpeedState.rows.reduce((n,r) => n+r.speed_calls,0)")
            evidence['scenarios']['speed_refresh_after_append'] = {
                'refresh_paint_ms': mark()-start,
                'requests': spent, 'server_cpu_ms': server.cpu_ms()-cpu_before,
                'measured_calls_before': usage_before, 'measured_calls_after': usage_after,
                'usage_index': order['usage'], 'speed_index': order['speed'],
            }
            check('refresh synchronises exactly once', spent['usage'] == 1, spent)
            check('refresh reads speed exactly once', spent['speed'] == 1, spent)
            check('rates follow the synchronise', order['usage'] is not None and order['speed'] is not None
                  and order['usage'] < order['speed'], order)
            check('the real append reaches the table', usage_after > usage_before, f'{usage_before} -> {usage_after}')
            check('speed refresh does no summary work', spent['insights'] == spent['active'] == 0, spent)

            page.click('a[data-tab-target="overview"]')
            start = mark()
            cpu_before=server.cpu_ms()
            page.click('#refreshBtn')
            page.wait_for_function('() => !updateInFlight')
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            evidence['scenarios']['overview_refresh_paint_ms']=mark()-start
            evidence['scenarios']['overview_refresh_server_cpu_ms']=server.cpu_ms()-cpu_before
            check('Overview refresh asks for no speed data', counts(start)['speed'] == 0, counts(start))
            page.click('a[data-tab-target="speed"]')
            wait_speed_settled()

            # Pause the sync response, leave, then release it: no speed follow-up.
            parked = []
            page.route('**/api/usage?*refresh=1', lambda route: parked.append(route))
            start = mark()
            page.click('#refreshBtn')
            page.wait_for_function('() => usageReportSpeedState.loading')
            page.wait_for_timeout(100)
            assert parked, 'manual refresh did not dispatch its sync request'
            page.click('a[data-tab-target="overview"]')
            left = mark()
            with page.expect_response(lambda response: '/api/usage?' in response.url):
                parked[0].continue_()
            page.unroute('**/api/usage?*refresh=1')
            page.wait_for_timeout(100)
            check('leaving during sync sends no hidden speed read', counts(left)['speed'] == 0, counts(left))
            start = mark()
            page.click('a[data-tab-target="speed"]')
            wait_speed_settled()
            check('returning reads the measurements still owed', counts(start)['speed'] == 1, counts(start))
            evidence['server_peak_rss_mb']=next((int(line.split()[1])/1024 for line in Path(f'/proc/{server.proc.pid}/status').read_text().splitlines() if line.startswith('VmHWM:')),None)
            page.screenshot(path=str(Path(args.out).with_suffix('.png')),full_page=True)
        except BaseException:      # a crashed run still reports what it observed
            evidence["error"] = traceback.format_exc()
            raise
        finally:
            evidence["requests"] = requests
            if not args.keep:
                server.stop()
                browser.close()
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
            print(f"wrote {out}")
            for row in evidence["requests"]:
                print(f"  t={row['t']:9.1f}ms  {row['url'].replace(server.base, '')}")
            for item in evidence["checks"]:
                print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}")
            if evidence.get("error"):
                print("SCENARIO CRASHED:")
                print(evidence["error"])
            failed = [i for i in evidence["checks"] if not i["ok"]]
            ok = not failed and not evidence.get("error")
            print("PASS" if ok else f"FAIL ({len(failed) or 'crash'})")
            code = 0 if ok else 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
