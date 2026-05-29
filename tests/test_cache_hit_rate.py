"""Tests for the prompt cache-hit-rate metric.

The faithful definition is cacheRead / (prompt input), where prompt input is
tokens_in + tokens_cache (tokens_in already folds cacheWrite into billable input).
Output and reasoning tokens are excluded. None when there is no prompt input.
"""
import tokdash.compute as C
from tokdash.compute import cache_hit_rate, parse_entries_json
from tokdash.sessions import _build_turn, _summarize_session


def test_helper_edges_and_math():
    assert cache_hit_rate(0, 0) is None          # no prompt input -> n/a
    assert cache_hit_rate(None, None) is None
    assert cache_hit_rate(100, 0) == 0.0         # all fresh, nothing cached
    assert cache_hit_rate(0, 100) == 1.0         # fully cached
    assert cache_hit_rate(20, 80) == 0.8         # 80 / (20 + 80)
    assert cache_hit_rate(200, 300) == 0.6


def _entries():
    return [
        # claude: tokens_in = input + cacheWrite = 100 + 100 = 200; cache = 300 -> 0.6
        {"source": "claude", "model": "claude-x", "provider": "anthropic",
         "input": 100, "output": 50, "cacheRead": 300, "cacheWrite": 100, "reasoning": 0, "timestamp": 1},
        # codex: tokens_in = 200 (cacheWrite 0); cache = 600 -> 600/800 = 0.75
        {"source": "codex", "model": "gpt", "provider": "openai",
         "input": 200, "output": 20, "cacheRead": 600, "cacheWrite": 0, "reasoning": 0, "timestamp": 2},
        # output-only row contributes nothing to the rate (den stays 0 for its model)
        {"source": "copilot_cli", "model": "c", "provider": "github",
         "input": 0, "output": 999, "cacheRead": 0, "cacheWrite": 0, "reasoning": 0, "timestamp": 3},
    ]


def test_parse_entries_json_attaches_rate():
    out = parse_entries_json({"entries": _entries()})
    apps = out["apps"]
    assert apps["claude"]["cache_hit_rate"] == 0.6
    assert apps["codex"]["cache_hit_rate"] == 0.75
    assert apps["copilot_cli"]["cache_hit_rate"] is None  # output-only -> n/a
    # per-model rate present too
    claude_model = apps["claude"]["models"][0]
    assert claude_model["cache_hit_rate"] == 0.6
    # tools-level token-weighted aggregate: (300+600) / ((200+200) + (300+600))
    assert out["cache_hit_rate"] == round(900 / 1300, 4)
    # output tokens must NOT be in the denominator: claude den is 500, not 550.
    assert apps["claude"]["cache_hit_rate"] == round(300 / 500, 4)


def test_compute_usage_by_tool_combined_and_header(monkeypatch):
    tools = parse_entries_json({"entries": _entries()})
    openclaw = {
        "total_tokens": 500, "total_cost": 1.0, "total_messages": 2,
        "total_tokens_in": 100, "total_tokens_cache": 400,
        "cache_hit_rate": 0.8,
        "models": {
            "moonshot/kimi": {
                "name": "moonshot/kimi", "tokens": 500, "tokens_in": 100, "tokens_out": 0,
                "tokens_cache": 400, "cost": 1.0, "messages": 2, "cache_hit_rate": 0.8,
            }
        },
        "contributions": [],
    }
    monkeypatch.setattr(C, "get_tools_data", lambda period: tools)
    monkeypatch.setattr(C, "get_openclaw_data", lambda period: openclaw)

    out = C.compute_usage("today")

    # by_tool rates
    assert out["by_tool"]["claude"]["cache_hit_rate"] == 0.6
    assert out["by_tool"]["codex"]["cache_hit_rate"] == 0.75
    assert out["by_tool"]["openclaw"]["cache_hit_rate"] == 0.8

    # combined per-model rows all carry a rate; the openclaw row (model name is
    # normalized so look it up by its token signature) keeps its 0.8.
    assert all("cache_hit_rate" in r for r in out["combined_models"])
    kimi_row = next(r for r in out["combined_models"] if r["tokens_cache"] == 400 and r["tokens_in"] == 100)
    assert kimi_row["cache_hit_rate"] == 0.8

    # header global token-weighted across tools + openclaw:
    # cache = 300 + 600 + 400 = 1300 ; in = 200 + 200 + 100 = 500 ; 1300 / 1800
    assert out["cache_hit_rate"] == round(1300 / 1800, 4)


def test_sessions_turn_and_summary_rate():
    t1 = _build_turn(0, 1000, "m", tokens_in=200, tokens_cache=300, tokens_out=50, tokens_reasoning=0, cost=0.0)
    t2 = _build_turn(1, 2000, "m", tokens_in=0, tokens_cache=0, tokens_out=10, tokens_reasoning=0, cost=0.0)
    assert t1["cache_hit_rate"] == 0.6
    assert t2["cache_hit_rate"] is None  # output-only turn -> n/a

    summary = _summarize_session({"tool": "claude", "session_id": "s", "project": "p", "turns": [t1, t2]})
    # session prompt input = 200 ; cache = 300 -> 0.6
    assert summary["cache_hit_rate"] == 0.6
    # the legacy cache_ratio (over ALL tokens incl output) is a different, lower number
    assert summary["cache_ratio"] != summary["cache_hit_rate"]
    assert summary["cache_ratio"] == round(300 / (200 + 300 + 50 + 10), 10) or summary["cache_ratio"] < summary["cache_hit_rate"]
