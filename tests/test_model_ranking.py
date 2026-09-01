"""Model arrays must be ranked by tokens, as the API reference says they are.

Every model list the API returns was sorted by cost while `docs/reference/API.md`
described `top_models` as "top N models by token usage". A consumer building a
podium off the array got the priciest model rather than the most-used one.

The reason this survived review is that over a short window the two orderings
usually coincide -- a month of data ranks the same either way, so a spot check
passes. Every fixture here is therefore built so the orderings *disagree*, and
each test asserts that disagreement before asserting the order. A fixture that
drifted back into agreement would silently stop testing anything.
"""
from __future__ import annotations

import tokdash.compute as C
from tokdash.compute import _merge_parsed_usage, compute_usage, parse_entries_json
from tokdash.usage_store import UsageEntryStore, build_source_signature, model_rank_key

# Token order and cost order are exact reverses of each other here.
#   tokens: workhorse 10k > codex-heavy 8k > mid 7k > boutique 4k
#   cost:   boutique 9.0 > mid 5.0 > codex-heavy 2.0 > workhorse 1.0
CODING = [
    ("claude", "cheap-workhorse", 10_000, 1.0),
    ("claude", "mid-runner", 7_000, 5.0),
    ("claude", "pricey-boutique", 4_000, 9.0),
    ("codex", "codex-heavy", 8_000, 2.0),
]

# The openclaw side diverges too, and by more: its biggest model is its cheapest.
OPENCLAW = [
    ("ocl-big", 9_000, 0.5),
    ("ocl-small", 3_000, 12.0),
]


def _entry(source: str, model: str, tokens: int, cost: float, ts: int) -> dict:
    """One usage row whose four token buckets sum to exactly `tokens`."""
    quarter = tokens // 4
    return {
        "source": source,
        "model": model,
        "provider": "",
        "input": quarter,
        "output": quarter,
        "cacheRead": tokens - 2 * quarter,
        "cacheWrite": 0,
        "reasoning": 0,
        "cost": cost,
        "timestamp": ts,
    }


def _entries() -> list[dict]:
    return [
        _entry(source, model, tokens, cost, 1_700_000_000_000 + i)
        for i, (source, model, tokens, cost) in enumerate(CODING)
    ]


def _openclaw_payload() -> dict:
    models = {}
    for name, tokens, cost in OPENCLAW:
        models[name] = {
            "name": name,
            "tokens": tokens,
            "tokens_in": tokens // 4,
            "tokens_out": tokens // 4,
            "tokens_cache": tokens - 2 * (tokens // 4),
            "cost": cost,
            "messages": 1,
            "cache_hit_rate": None,
        }
    return {
        "total_tokens": sum(t for _, t, _ in OPENCLAW),
        "total_cost": sum(c for _, _, c in OPENCLAW),
        "total_messages": len(OPENCLAW),
        "total_tokens_in": sum(t // 4 for _, t, _ in OPENCLAW),
        "total_tokens_cache": sum(t - 2 * (t // 4) for _, t, _ in OPENCLAW),
        "cache_hit_rate": None,
        "models": models,
        "contributions": [],
    }


def _assert_orderings_disagree(rows: list[dict]) -> None:
    """Guard the fixture: a coincidence here would make the test vacuous."""
    by_tokens = [r["name"] for r in sorted(rows, key=lambda r: -r["tokens"])]
    by_cost = [r["name"] for r in sorted(rows, key=lambda r: -r["cost"])]
    assert by_tokens != by_cost, "fixture no longer distinguishes token order from cost order"


def _tokens(rows: list[dict]) -> list[int]:
    return [int(r["tokens"]) for r in rows]


def test_rank_key_orders_by_tokens_then_cost_then_name():
    rows = [
        {"name": "b", "tokens": 100, "cost": 1.0},
        {"name": "a", "tokens": 100, "cost": 1.0},
        {"name": "c", "tokens": 100, "cost": 9.0},
        {"name": "d", "tokens": 500, "cost": 0.0},
    ]
    assert [r["name"] for r in sorted(rows, key=model_rank_key)] == ["d", "c", "a", "b"]


def test_rank_key_tolerates_missing_and_null_fields():
    rows = [{"name": "x"}, {"name": "y", "tokens": None, "cost": None}, {"name": "z", "tokens": 5}]
    assert [r["name"] for r in sorted(rows, key=model_rank_key)] == ["z", "x", "y"]


def test_parse_entries_json_ranks_models_by_tokens():
    out = parse_entries_json({"entries": _entries()})

    claude_models = out["apps"]["claude"]["models"]
    _assert_orderings_disagree(claude_models)
    assert [m["name"] for m in claude_models] == ["cheap-workhorse", "mid-runner", "pricey-boutique"]
    # Under the old cost sort this array led with the 4k-token model.
    assert claude_models[0]["tokens"] == 10_000

    all_models = out["all_models"]
    _assert_orderings_disagree(all_models)
    assert _tokens(all_models) == sorted(_tokens(all_models), reverse=True)


def test_merge_parsed_usage_ranks_models_by_tokens():
    # Two parts that each hold half of a model's usage, so the merge -- not the
    # inputs -- decides the order.
    left = parse_entries_json({"entries": _entries()})
    right = parse_entries_json({"entries": _entries()})
    merged = _merge_parsed_usage([left, right])

    all_models = merged["all_models"]
    _assert_orderings_disagree(all_models)
    assert _tokens(all_models) == sorted(_tokens(all_models), reverse=True)
    assert all_models[0]["name"] == "cheap-workhorse"
    assert all_models[0]["tokens"] == 20_000  # both halves folded in

    claude_models = merged["apps"]["claude"]["models"]
    assert [m["name"] for m in claude_models] == ["cheap-workhorse", "mid-runner", "pricey-boutique"]


def test_compute_usage_ranks_every_model_array_by_tokens(monkeypatch):
    monkeypatch.setattr(C, "get_tools_data", lambda period: parse_entries_json({"entries": _entries()}))
    monkeypatch.setattr(C, "get_openclaw_data", lambda period: _openclaw_payload())

    out = compute_usage("today")

    for key in ("coding_models", "openclaw_models", "combined_models", "top_models"):
        rows = out[key]
        assert _tokens(rows) == sorted(_tokens(rows), reverse=True), f"{key} is not token-ranked"

    _assert_orderings_disagree(out["combined_models"])
    assert [m["name"] for m in out["combined_models"]] == [
        "cheap-workhorse",   # 10k, $1.00
        "ocl-big",           #  9k, $0.50  -- cheapest model, second most used
        "codex-heavy",       #  8k, $2.00
        "mid-runner",        #  7k, $5.00
        "pricey-boutique",   #  4k, $9.00
        "ocl-small",         #  3k, $12.00 -- priciest model, least used
    ]

    # top_models is the head of combined_models and nothing else.
    assert out["top_models"] == out["combined_models"][:5]

    # The regression in full: cost ordering put ocl-small first and pushed
    # ocl-big -- the second-most-used model -- off the five-entry podium.
    assert out["top_models"][0]["name"] == "cheap-workhorse"
    assert "ocl-big" in [m["name"] for m in out["top_models"]]
    assert "ocl-small" not in [m["name"] for m in out["top_models"]]

    # Per-app arrays follow the same rule.
    assert [m["name"] for m in out["apps"]["claude"]["models"]] == [
        "cheap-workhorse",
        "mid-runner",
        "pricey-boutique",
    ]


def test_top_models_by_cost_is_the_spend_podium(monkeypatch):
    """The one array that ranks by money, served so consumers need not derive it."""
    monkeypatch.setattr(C, "get_tools_data", lambda period: parse_entries_json({"entries": _entries()}))
    monkeypatch.setattr(C, "get_openclaw_data", lambda period: _openclaw_payload())

    out = compute_usage("today")
    by_cost = out["top_models_by_cost"]

    assert [m["name"] for m in by_cost] == [
        "ocl-small",         # $12.00, 3k tokens -- least used, most expensive
        "pricey-boutique",   # $ 9.00, 4k
        "mid-runner",        # $ 5.00, 7k
        "codex-heavy",       # $ 2.00, 8k
        "cheap-workhorse",   # $ 1.00, 10k
    ]
    costs = [m["cost"] for m in by_cost]
    assert costs == sorted(costs, reverse=True)
    assert len(by_cost) == 5

    # The two podiums must be genuinely different lists, or this fixture has
    # stopped exercising the thing it exists to exercise.
    assert [m["name"] for m in by_cost] != [m["name"] for m in out["top_models"]]

    # Why the array is served rather than derived from top_models: the five
    # biggest models do not contain the priciest one, so a client holding only
    # top_models could not compute this.
    assert "ocl-small" not in [m["name"] for m in out["top_models"]]

    # Both podiums are drawn from the same merged list.
    names = {m["name"] for m in out["combined_models"]}
    assert {m["name"] for m in by_cost} <= names


def test_cost_podium_ties_break_on_tokens(monkeypatch):
    entries = [
        _entry("claude", "same-cost-small", 1_000, 3.0, 1_700_000_000_000),
        _entry("claude", "same-cost-big", 9_000, 3.0, 1_700_000_000_001),
    ]
    monkeypatch.setattr(C, "get_tools_data", lambda period: parse_entries_json({"entries": entries}))
    monkeypatch.setattr(C, "get_openclaw_data", lambda period: {
        "total_tokens": 0, "total_cost": 0.0, "total_messages": 0,
        "total_tokens_in": 0, "total_tokens_cache": 0, "cache_hit_rate": None,
        "models": {}, "contributions": [],
    })

    out = compute_usage("today")
    assert [m["name"] for m in out["top_models_by_cost"]] == ["same-cost-big", "same-cost-small"]


def test_stats_podium_agrees_with_the_arrays(monkeypatch):
    """`most_used_model` and `top_models[0]` must not name different models."""
    monkeypatch.setattr(C, "get_tools_data", lambda period: parse_entries_json({"entries": _entries()}))
    monkeypatch.setattr(C, "get_openclaw_data", lambda period: _openclaw_payload())

    usage = compute_usage("today")
    tools_only = parse_entries_json({"entries": _entries()})
    top_coding = tools_only["all_models"][0]["name"]

    assert usage["coding_models"][0]["name"] == top_coding
    assert usage["top_models"][0]["tokens"] >= usage["top_models"][-1]["tokens"]


def test_usage_store_aggregate_ranks_models_by_tokens(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    for source in ("claude", "codex"):
        rows = [e for e in _entries() if e["source"] == source]
        sig = build_source_signature(files=[[f"{source}.jsonl", 1, 2]], pricing=[3, 4], parser={"v": 1})
        assert store.sync_source(source, sig, lambda rows=rows: rows) is True

    out = store.aggregate_entries(sources=["claude", "codex"])

    all_models = out["all_models"]
    _assert_orderings_disagree(all_models)
    assert _tokens(all_models) == sorted(_tokens(all_models), reverse=True)
    assert all_models[0]["name"] == "cheap-workhorse"

    claude_models = out["apps"]["claude"]["models"]
    assert [m["name"] for m in claude_models] == ["cheap-workhorse", "mid-runner", "pricey-boutique"]


def test_live_and_persistent_paths_rank_identically(tmp_path):
    """The SQL path and the parser path must not disagree about model order."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    for source in ("claude", "codex"):
        rows = [e for e in _entries() if e["source"] == source]
        sig = build_source_signature(files=[[f"{source}.jsonl", 1, 2]], pricing=[3, 4], parser={"v": 1})
        store.sync_source(source, sig, lambda rows=rows: rows)

    stored = [m["name"] for m in store.aggregate_entries(sources=["claude", "codex"])["all_models"]]
    live = [m["name"] for m in parse_entries_json({"entries": _entries()})["all_models"]]
    assert stored == live
