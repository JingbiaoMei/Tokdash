#!/usr/bin/env python3
"""Deterministic synthetic corpora for the output-speed benchmark harness.

Real harness logs are read-only and grow between runs, so a frozen corpus is the
only way an "after" arm can be compared with its "before" arm. These writers emit
the exact record shapes the parsers read, verified against live logs on 2026-10-05:

* Kimi Code wire.jsonl  - usage.record rows + nested context.append_loop_event
                          step.end events carrying llmServerDecodeMs.
* omp session JSONL     - assistant messages carrying usage + duration + ttft.

Nothing here writes outside the fixture root it is handed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCALE = {"small": 1, "medium": 4, "large": 12}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _kimi_file(root: Path, *, seed: int, steps: int, base: datetime) -> Path:
    rng = random.Random(seed)
    workspace = f"wd_bench_{hashlib.sha1(str(seed).encode()).hexdigest()[:12]}"
    session = f"session_{hashlib.sha1(f's{seed}'.encode()).hexdigest()[:32]}"
    path = root / "sessions" / workspace / session / "agents" / "main" / "wire.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    t = base
    rows: list[str] = []
    rows.append(json.dumps({"type": "metadata", "protocol_version": "1.5"}))
    for step in range(1, steps + 1):
        turn = step // 4
        begin = t
        rows.append(
            json.dumps(
                {
                    "type": "context.append_loop_event",
                    "agentId": "main",
                    "event": {
                        "type": "step.begin",
                        "uuid": f"b-{seed}-{step}",
                        "turnId": str(turn),
                        "step": step,
                    },
                    "time": _ms(begin),
                }
            )
        )
        rows.append(
            json.dumps(
                {
                    "type": "llm.request",
                    "agentId": "main",
                    "kind": "loop",
                    "provider": "openai",
                    "model": "bench-model",
                    "modelAlias": "bench/bench-model",
                    "turnStep": f"{turn}.{step}",
                    "time": _ms(begin + timedelta(milliseconds=20)),
                }
            )
        )
        usage = {
            "inputOther": rng.randint(2_000, 20_000),
            "output": rng.randint(80, 1_800),
            "inputCacheRead": rng.randint(4_000, 40_000),
            "inputCacheCreation": 0,
        }
        decode_ms = rng.randint(1_500, 25_000)
        stream_end = begin + timedelta(milliseconds=decode_ms)
        rows.append(
            json.dumps(
                {
                    "type": "usage.record",
                    "agentId": "main",
                    "model": "bench/bench-model",
                    "usage": usage,
                    "usageScope": "turn",
                    "time": _ms(stream_end),
                }
            )
        )
        # ~4% of steps keep no timing at all: the missing_timing status must survive
        # a real corpus rather than only a hand-written unit fixture.
        if rng.random() > 0.04:
            rows.append(
                json.dumps(
                    {
                        "type": "context.append_loop_event",
                        "agentId": "main",
                        "event": {
                            "type": "step.end",
                            "uuid": f"b-{seed}-{step}",
                            "turnId": str(turn),
                            "step": step,
                            "finishReason": "tool_use",
                            "usage": usage,
                            "llmFirstTokenLatencyMs": rng.randint(500, 4_000),
                            "llmStreamDurationMs": decode_ms + rng.randint(0, 40),
                            "llmServerFirstTokenMs": rng.randint(500, 4_000),
                            "llmServerDecodeMs": decode_ms,
                            "messageId": f"chatcmpl-{seed}-{step}",
                        },
                        "time": _ms(stream_end + timedelta(milliseconds=140)),
                    }
                )
            )
        t = stream_end + timedelta(seconds=rng.randint(2, 20))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _omp_file(root: Path, *, seed: int, turns: int, base: datetime) -> Path:
    rng = random.Random(seed ^ 0x5EED)
    path = (
        root
        / "agent"
        / "sessions"
        / f"bench-{seed}"
        / f"{hashlib.sha1(f'o{seed}'.encode()).hexdigest()}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    t = base
    for i in range(turns):
        out = rng.randint(60, 1_400)
        usage = {
            "input": rng.randint(1_000, 30_000),
            "output": out,
            "cacheRead": rng.randint(0, 20_000),
            "cacheWrite": 0,
            "totalTokens": 0,
            "reasoningTokens": rng.randint(0, max(1, out // 6)),
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        }
        usage["totalTokens"] = (
            usage["input"] + usage["output"] + usage["cacheRead"] + usage["cacheWrite"]
        )
        duration = rng.randint(2_000, 30_000) + rng.random()
        msg = {
            "role": "assistant",
            "model": "bench-omp-model",
            "usage": usage,
            "duration": duration,
            "ttft": rng.randint(300, 3_000) + rng.random(),
        }
        if i % 25 == 24:
            # Present-but-zero TTFT must never be read as "missing".
            msg["ttft"] = 0
        elif i % 17 == 16:
            msg.pop("ttft", None)
        rows.append(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": t.isoformat(),
                    "message": msg,
                }
            )
        )
        rows.append(
            json.dumps(
                {
                    "type": "message",
                    "timestamp": (t + timedelta(seconds=1)).isoformat(),
                    "message": {"role": "user", "content": "bench turn"},
                }
            )
        )
        t += timedelta(seconds=rng.randint(30, 300))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def isolated_env(corpus_root: Path, data_dir: Path, xdg_root: Path) -> dict:
    """Environment for a throwaway tokdash server, without touching HOME.

    Shared by the benchmark harness and the browser check so the two cannot drift
    into measuring different corpora. HOME and CODEX_HOME are deliberately absent:
    repurposing them is off limits, and the parser allowlist in
    scripts/bench_tokdash_server.py is what keeps the readers inside these roots.
    """
    corpus_root = Path(corpus_root)
    return {
        "TOKDASH_BENCH_CORPUS": str(corpus_root.resolve()),
        "TOKDASH_USAGE_DB_PATH": str(Path(data_dir).resolve() / "usage.sqlite3"),
        "TOKDASH_USAGE_DB_WATCH": "0",
        "TOKDASH_QUOTA_POLL": "0",
        "TOKDASH_DATA_DIR": str(Path(data_dir)),
        "XDG_DATA_HOME": str(Path(xdg_root) / "share"),
        "XDG_CONFIG_HOME": str(Path(xdg_root) / "config"),
        "XDG_CACHE_HOME": str(Path(xdg_root) / "cache"),
        "TOKDASH_WARM_ON_START": "0",
        "TOKDASH_DAILY_WARM": "0",
        "KIMI_CODE_HOME": str(corpus_root / "kimi-code"),
        "KIMI_SHARE_DIR": str(corpus_root / "kimi-legacy-absent"),
        "PI_CONFIG_DIR": str(corpus_root / "omp"),
        "OPENCLAW_HOME": str(corpus_root / "openclaw-absent"),
    }


def build(root: Path, *, scale: str = "large", days: int = 28, base: datetime | None = None) -> dict:
    """Write a frozen corpus under ``root`` and return its inventory."""
    root = Path(root)
    kimi_root = root / "kimi-code"
    omp_root = root / "omp"
    kimi_root.mkdir(parents=True, exist_ok=True)
    omp_root.mkdir(parents=True, exist_ok=True)

    unit = SCALE[scale]
    base = base or datetime.now(timezone.utc) - timedelta(days=days)
    kimi_files = omp_files = 0
    for i in range(unit * 12):
        _kimi_file(
            kimi_root,
            seed=i,
            steps=40 + (i % 7) * 12,
            base=base + timedelta(days=i % days, hours=i % 11),
        )
        kimi_files += 1
    for i in range(unit * 6):
        _omp_file(
            omp_root,
            seed=i,
            turns=30 + (i % 5) * 10,
            base=base + timedelta(days=i % days, hours=(i * 3) % 24),
        )
        omp_files += 1

    total = 0
    for p in list(kimi_root.rglob("wire.jsonl")) + list(omp_root.rglob("*.jsonl")):
        total += p.stat().st_size
    return {
        "scale": scale,
        "kimi_files": kimi_files,
        "omp_files": omp_files,
        "corpus_bytes": total,
        "kimi_root": str(kimi_root),
        "omp_root": str(omp_root),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--scale", default="large", choices=sorted(SCALE))
    ap.add_argument("--days", type=int, default=28)
    args = ap.parse_args()
    info = build(Path(args.root).expanduser(), scale=args.scale, days=args.days)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
