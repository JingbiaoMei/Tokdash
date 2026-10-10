#!/usr/bin/env python3
"""Bench-only launcher: serve a chosen tokdash tree with the parser set pruned.

Why this exists instead of a fake HOME: the handoff forbids repurposing HOME or
CODEX_HOME to redirect data, and pruning is the stronger guarantee anyway. With
only the fixture-backed parsers alive, nothing in the process walks the real
~/.codex, ~/.claude or ~/.tokdash, so a harness bug cannot quietly read the real
corpus (which would make a before/after comparison meaningless) or write to the
live dashboard cache.

This file is never imported by the package and never shipped. It mutates the
imported tokdash module in memory only.

    python3 scripts/bench_tokdash_server.py --src src --port 55999 --parsers kimi,omp
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _prune_parsers(allowed: set[str]) -> dict:
    """Keep only the fixture-backed parsers; drop every real-home reader."""
    from tokdash.sources.coding_tools import CodingToolsUsageTracker
    # Native speed reads bypass the parser registry. Redirect those too, and
    # keep Codex's HOME variables intact by overriding only its path resolver.
    corpus = os.environ.get("TOKDASH_BENCH_CORPUS")
    if corpus:
        from tokdash import clientpaths
        root = Path(corpus)
        clientpaths.codex_sessions_dir = lambda: root / "codex" / "sessions"
        clientpaths.codex_archived_sessions_dir = lambda: root / "codex" / "archived_sessions"
        clientpaths.dsh_sessions_dir = lambda: root / "dsh" / "sessions"
        clientpaths.qwen_runtime_base = lambda: root / "qwen"
        clientpaths.opencode_db_path = lambda: root / "opencode.db"
        clientpaths.kilo_db_paths = lambda: [root / "kilocode.db"]
        clientpaths.mimocode_db_path = lambda: root / "mimo.db"
        from tokdash import sessions
        sessions.SESSION_TOOLS = tuple(s for s in sessions.SESSION_TOOLS if s in allowed)

    original_init = CodingToolsUsageTracker.__init__
    seen: dict = {}

    def init(self):  # pragma: no cover - exercised by the bench child
        original_init(self)
        present = set(self.parsers)
        seen.setdefault("original_registry", sorted(present))
        dropped = sorted(present - allowed)
        for name in dropped:
            self.parsers.pop(name, None)
        missing = sorted(allowed - set(self.parsers))
        if missing:
            raise RuntimeError(
                f"bench parser(s) not available in this tokdash tree: {', '.join(missing)}"
            )
        # Belt and braces: a parser that is not allowed must never be reachable.
        if set(self.parsers) != allowed:
            raise RuntimeError(f"parser pruning failed: {sorted(self.parsers)} != {sorted(allowed)}")

    CodingToolsUsageTracker.__init__ = init
    return {"requested": sorted(allowed), "registry_seen_at_first_tracker": seen}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, help="path to the tokdash package root (…/src)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--parsers", default="kimi,omp", help="comma-separated allowlist")
    ap.add_argument("--log-level", default="warning")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    if not (src / "tokdash" / "api.py").is_file():
        print(f"bench server: no tokdash package under {src}", file=sys.stderr)
        return 4
    # First on the path so the requested tree wins over anything already installed.
    sys.path.insert(0, str(src))

    allowed = {p.strip() for p in args.parsers.split(",") if p.strip()}
    inventory = _prune_parsers(allowed)
    print(
        "bench-server "
        + repr({"src": str(src), "parsers": inventory, "pid": os.getpid()}),
        flush=True,
    )

    import uvicorn
    from tokdash.api import app, _clear_cache
    try:
        from tokdash import speed_cache
    except ImportError:
        pass  # Frozen pre-feature/eager arms have no separate worker.
    else:
        # Children must inherit the same fixture isolation as the HTTP process.
        # Intercept only the worker command; production source is untouched.
        original_popen=speed_cache.subprocess.Popen
        def fixture_worker(command,*pos,**kwargs):
            if command==[sys.executable,'-m','tokdash.speed_worker']:
                command=[sys.executable,str(Path(__file__).with_name('bench_speed_worker.py')),
                         '--src',str(src),'--parsers',args.parsers]
            return original_popen(command,*pos,**kwargs)
        speed_cache.subprocess.Popen=fixture_worker

    # This route exists only in this isolated benchmark launcher, never in
    # the package application or the development preview service.
    @app.get('/__benchmark__/clear-cache')
    def clear_response_cache():
        _clear_cache()
        return {'cleared': True}

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
