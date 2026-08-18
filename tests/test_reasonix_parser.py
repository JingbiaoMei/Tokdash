"""Tests for ReasonixParser (``~/.reasonix/stats/YYYY-MM-DD.jsonl``)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokdash import clientpaths
from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, ReasonixParser, _sig_cache


def _stats_line(
    ts="2026-08-15T12:24:35.556495944+01:00",
    model="minimax-cn/MiniMax-M3",
    prompt=8247,
    completion=56,
    cache_hit=128,
    cache_miss=8119,
    **extra,
) -> str:
    payload = {
        "ts": ts,
        "model": model,
        "source": "cli",
        "prompt": prompt,
        "completion": completion,
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
        # Reasonix reports total redundantly; the parser ignores it. Guarded so
        # the deliberately-malformed prompt values below can still be written out.
        "total": (
            prompt + completion
            if isinstance(prompt, int) and isinstance(completion, int)
            else None
        ),
        "requests": 1,
        "usage_source": "executor",
    }
    payload.update(extra)
    for key, value in list(payload.items()):
        if value is None:
            del payload[key]
    return json.dumps(payload)


@pytest.fixture(autouse=True)
def _isolated_reasonix_home(monkeypatch, tmp_path):
    home = tmp_path / "reasonix-home"
    monkeypatch.setenv("REASONIX_HOME", str(home))
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    yield home
    _sig_cache.clear()
    BaseParser._entry_cache.clear()


def _write_stats(home: Path, name: str, *lines: str) -> Path:
    stats_dir = home / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    stats_file = stats_dir / name
    stats_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stats_file


def _collect(home: Path):
    return ReasonixParser(PricingDatabase()).collect(None, None)


def test_reasonix_parser_basic(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(home, "2026-08-15.jsonl", _stats_line())

    entries = _collect(home)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "reasonix"
    assert e["model"] == "MiniMax-M3"
    assert e["provider"] == "minimax-cn"
    # Reasonix's prompt counts cached and uncached input together, so the two
    # Tokdash buckets must partition it rather than both claiming cache_hit.
    assert e["input"] == 8119
    assert e["cacheRead"] == 128
    assert e["input"] + e["cacheRead"] == 8247
    assert e["output"] == 56
    assert e["cacheWrite"] == 0
    assert e["reasoning"] == 0
    assert e["cost"] > 0
    assert e["timestamp"] > 0
    assert e["entry_id"].startswith("reasonix:")


def test_reasonix_cost_prices_the_cached_half_at_the_cache_rate(_isolated_reasonix_home):
    """The split is not cosmetic: billing prompt as input overcharges."""
    home = _isolated_reasonix_home
    _write_stats(home, "2026-08-15.jsonl", _stats_line())

    entry = _collect(home)[0]
    db = PricingDatabase()

    assert entry["cost"] == pytest.approx(
        db.get_cost("MiniMax-M3", 8119, 56, cache_read=128, cache_write=0)
    )
    assert entry["cost"] < db.get_cost("MiniMax-M3", 8247, 56, cache_read=128, cache_write=0)


def test_reasonix_parser_multiple_entries(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        _stats_line(ts="2026-08-15T12:00:00.000Z", model="minimax-cn/MiniMax-M3", prompt=1000, completion=50, cache_hit=100, cache_miss=900),
        _stats_line(ts="2026-08-15T13:00:00.000Z", model="vllm-hpc/qwen3.8-27B-FP8", prompt=2000, completion=80, cache_hit=0, cache_miss=2000),
    )

    entries = _collect(home)

    assert len(entries) == 2
    assert entries[0]["model"] == "MiniMax-M3"
    assert entries[0]["input"] == 900
    assert entries[1]["model"] == "qwen3.8-27B-FP8"
    assert entries[1]["provider"] == "vllm-hpc"


def test_reasonix_parser_invalid_lines_skipped(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        "not json at all",
        _stats_line(),
        '{"ts": "invalid-ts", "model": "MiniMax-M3", "prompt": 10}',
        '{"ts": "2026-08-15T12:00:00.000Z", "model": "MiniMax-M3", "prompt": 0, "completion": 0, "cache_hit": 0}',
        "[1, 2, 3]",
    )

    entries = _collect(home)

    assert len(entries) == 1
    assert entries[0]["model"] == "MiniMax-M3"


@pytest.mark.parametrize("bad", ["oops", {"n": 1}, [5], -4, False, "", [], {}])
def test_reasonix_bad_token_field_drops_its_row_not_the_rest_of_the_file(
    _isolated_reasonix_home, bad
):
    """A row that raises must not take the remainder of the day file with it."""
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        _stats_line(ts="2026-08-15T12:00:00.000000+01:00", prompt=1000, cache_hit=0, cache_miss=1000),
        _stats_line(ts="2026-08-15T12:01:00.000000+01:00", prompt=bad, cache_hit=0, cache_miss=None),
        _stats_line(ts="2026-08-15T12:02:00.000000+01:00", prompt=500, cache_hit=0, cache_miss=500),
    )

    entries = _collect(home)

    assert [e["input"] for e in entries] == [1000, 500]


def test_reasonix_cache_hit_absent_means_nothing_cached(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        '{"ts": "2026-08-15T12:00:00.000Z", "model": "MiniMax-M3", "prompt": 700, "completion": 20}',
    )

    entry = _collect(home)[0]

    assert entry["cacheRead"] == 0
    assert entry["input"] == 700


def test_reasonix_input_derived_from_prompt_when_cache_miss_absent(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        '{"ts": "2026-08-15T12:00:00.000Z", "model": "MiniMax-M3", "prompt": 700, "completion": 20, "cache_hit": 200}',
    )

    entry = _collect(home)[0]

    assert entry["input"] == 500
    assert entry["cacheRead"] == 200


def test_reasonix_nine_digit_fractional_seconds_parse(_isolated_reasonix_home):
    """datetime.fromisoformat takes only 3 or 6 digits before 3.11; Tokdash supports 3.10."""
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        _stats_line(ts="2026-08-15T12:24:35.556495944+01:00"),
    )

    entry = _collect(home)[0]

    # 11:24:35.556 UTC — the +01:00 offset is honored, not dropped.
    assert entry["timestamp"] == 1786793075556


def test_reasonix_naive_timestamp_is_read_as_utc(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        _stats_line(ts="2026-08-15T12:00:00.000"),
    )

    entry = _collect(home)[0]

    assert entry["timestamp"] == 1786795200000


def test_reasonix_multiple_day_files_combine_in_timestamp_order(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(home, "2026-08-16.jsonl", _stats_line(ts="2026-08-16T09:00:00.000Z", prompt=300, cache_hit=0, cache_miss=300))
    _write_stats(home, "2026-08-14.jsonl", _stats_line(ts="2026-08-14T09:00:00.000Z", prompt=100, cache_hit=0, cache_miss=100))
    _write_stats(home, "2026-08-15.jsonl", _stats_line(ts="2026-08-15T09:00:00.000Z", prompt=200, cache_hit=0, cache_miss=200))

    entries = _collect(home)

    assert [e["input"] for e in entries] == [100, 200, 300]
    assert [e["timestamp"] for e in entries] == sorted(e["timestamp"] for e in entries)


def test_reasonix_unknown_model_keeps_tokens_and_costs_nothing(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        _stats_line(model="vllm-hpc/qwen3.8-27B-FP8", prompt=4000, completion=100, cache_hit=0, cache_miss=4000),
    )

    entry = _collect(home)[0]

    assert entry["model"] == "qwen3.8-27B-FP8"
    assert entry["input"] == 4000
    assert entry["output"] == 100
    assert entry["cost"] == 0


def test_reasonix_bare_model_id_has_no_provider(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(home, "2026-08-15.jsonl", _stats_line(model="MiniMax-M3"))

    entry = _collect(home)[0]

    assert entry["model"] == "MiniMax-M3"
    assert entry["provider"] == ""


def test_reasonix_entry_ids_are_content_keyed_not_path_or_line_keyed(tmp_path, monkeypatch):
    """Moving REASONIX_HOME or shifting a row's line must not re-mint entry ids."""
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    row = _stats_line(ts="2026-08-15T12:00:00.000Z", prompt=900, completion=10, cache_hit=100, cache_miss=800)
    other = _stats_line(ts="2026-08-15T11:00:00.000Z", prompt=50, completion=1, cache_hit=0, cache_miss=50)

    first = tmp_path / "home-a"
    _write_stats(first, "2026-08-15.jsonl", row)
    monkeypatch.setenv("REASONIX_HOME", str(first))
    before = _collect(first)

    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    # Different directory, and the row is no longer line 0.
    second = tmp_path / "home-b"
    _write_stats(second, "2026-08-15.jsonl", other, row)
    monkeypatch.setenv("REASONIX_HOME", str(second))
    after = _collect(second)

    moved = [e for e in after if e["input"] == 800]
    assert len(moved) == 1
    assert moved[0]["entry_id"] == before[0]["entry_id"]


def test_reasonix_identical_rows_in_one_day_stay_distinct(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    row = _stats_line(ts="2026-08-15T12:00:00.000Z", prompt=900, completion=10, cache_hit=100, cache_miss=800)
    _write_stats(home, "2026-08-15.jsonl", row, row)

    entries = _collect(home)

    assert len(entries) == 2
    assert len({e["entry_id"] for e in entries}) == 2


def test_reasonix_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("REASONIX_HOME", str(tmp_path / "custom-reasonix"))
    assert clientpaths.reasonix_home() == tmp_path / "custom-reasonix"
    assert clientpaths.reasonix_stats_dir() == tmp_path / "custom-reasonix" / "stats"
    assert clientpaths.reasonix_projects_dir() == tmp_path / "custom-reasonix" / "projects"


def test_reasonix_home_blank_env_falls_back_to_dot_reasonix(monkeypatch):
    monkeypatch.setenv("REASONIX_HOME", "   ")
    assert clientpaths.reasonix_home() == Path.home() / ".reasonix"


def test_reasonix_home_relative_env_resolves_to_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REASONIX_HOME", "rx-home")
    resolved = clientpaths.reasonix_home()
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "rx-home").resolve()


def test_reasonix_missing_stats_dir_is_not_an_error(_isolated_reasonix_home):
    assert _collect(_isolated_reasonix_home) == []


@pytest.mark.parametrize("field", ["prompt", "completion", "cache_hit", "cache_miss"])
@pytest.mark.parametrize("bad", [False, "", [], {}])
def test_falsy_malformed_token_fields_are_rejected_not_read_as_zero(
    _isolated_reasonix_home, field, bad
):
    """`or 0` coercion would let every one of these through as a valid zero."""
    home = _isolated_reasonix_home
    row = json.loads(_stats_line(prompt=900, completion=10, cache_hit=100, cache_miss=800))
    row[field] = bad
    _write_stats(home, "2026-08-15.jsonl", json.dumps(row))

    assert _collect(home) == []


@pytest.mark.parametrize("field", ["prompt", "completion", "cache_hit", "cache_miss"])
def test_absent_and_null_token_fields_read_as_zero(_isolated_reasonix_home, field):
    """Absence is not corruption: Reasonix omits cache_hit when nothing cached."""
    home = _isolated_reasonix_home
    base = json.loads(_stats_line(prompt=900, completion=10, cache_hit=0, cache_miss=900))

    absent = dict(base)
    absent.pop(field, None)
    absent["ts"] = "2026-08-15T12:00:00.000Z"
    nulled = dict(base)
    nulled[field] = None
    nulled["ts"] = "2026-08-15T12:01:00.000Z"
    _write_stats(home, "2026-08-15.jsonl", json.dumps(absent), json.dumps(nulled))

    entries = _collect(home)

    assert len(entries) == 2
    for entry in entries:
        assert entry["input"] >= 0
        assert entry["cacheRead"] >= 0


def test_a_row_of_only_zeroes_is_not_usage(_isolated_reasonix_home):
    home = _isolated_reasonix_home
    _write_stats(
        home,
        "2026-08-15.jsonl",
        _stats_line(prompt=0, completion=0, cache_hit=0, cache_miss=0),
    )

    assert _collect(home) == []
