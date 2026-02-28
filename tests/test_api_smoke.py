import json
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _fetch(url: str, timeout_s: float = 2.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")


@contextmanager
def _run_uvicorn():
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tokdash.api:app", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 10.0
        last_err = None
        while time.time() < deadline:
            try:
                health = json.loads(_fetch(f"{base}/health", timeout_s=0.5))
            except Exception as e:
                last_err = e
                time.sleep(0.1)
                continue

            if health.get("status") == "ok":
                break

            time.sleep(0.1)
        else:
            out = ""
            err = ""
            try:
                out, err = proc.communicate(timeout=1)
            except Exception:
                pass
            raise AssertionError(f"uvicorn did not start in time (last_err={last_err}).\nstdout:\n{out}\nstderr:\n{err}")

        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_api_endpoints_and_dashboard_smoke():
    with _run_uvicorn() as base:
        usage = json.loads(_fetch(f"{base}/api/usage?period=today"))
        assert "total_tokens" in usage
        assert "openclaw_models" in usage
        assert "coding_apps" in usage

        tools = json.loads(_fetch(f"{base}/api/tools?period=today"))
        assert "apps" in tools
        assert "all_models" in tools

        openclaw = json.loads(_fetch(f"{base}/api/openclaw?period=today"))
        assert "models" in openclaw
        assert "contributions" in openclaw

        # /api/stats can be expensive on large local histories.
        stats = json.loads(_fetch(f"{base}/api/stats", timeout_s=20))
        assert "contributions" in stats
        assert "stats" in stats

        html = _fetch(f"{base}/")
        assert "Tokdash" in html
