import json
from pathlib import Path

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, GrokParser


def _write_log(tmp_path, rows):
    grok_home = tmp_path / ".grok"
    (grok_home / "logs").mkdir(parents=True)
    (grok_home / "logs" / "unified.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return grok_home


def _model_event(pid, model, ts="2026-07-20T13:00:00Z"):
    return {"ts": ts, "pid": pid, "msg": "model catalog: notifying clients", "ctx": {"current_model_id": model}}


def _inference(pid, ts, prompt, completion, reasoning=0, cached=0, loop_index=1):
    return {
        "ts": ts,
        "pid": pid,
        "msg": "shell.turn.inference_done",
        "ctx": {
            "prompt_tokens": prompt,
            "cached_prompt_tokens": cached,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "loop_index": loop_index,
        },
    }


def _parser(monkeypatch, grok_home):
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    BaseParser._entry_cache.clear()
    return GrokParser(PricingDatabase())


def test_grok_parses_inference_done_with_real_token_split(monkeypatch, tmp_path):
    # Matches the live shape verified 2026-07-23 (prompt=15482, completion=40, reasoning=30).
    grok_home = _write_log(
        tmp_path,
        [
            _model_event(101, "grok-4.5"),
            _inference(101, "2026-07-20T13:25:51Z", prompt=15482, completion=40, reasoning=30, cached=0),
        ],
    )

    entries = _parser(monkeypatch, grok_home).collect()

    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == "grok-4.5"
    assert e["provider"] == "xai"
    assert e["source"] == "grok"
    assert e["input"] == 15482
    assert e["cacheRead"] == 0
    assert e["output"] == 70  # completion + reasoning (reasoning is billed as output)
    assert e["cacheWrite"] == 0
    # Real per-inference counts, not a cumulative-delta estimate.
    assert e["estimated"] is False
    assert e["cost"] == 0.0  # priced by the compute layer from the split above


def test_grok_splits_cached_prompt_tokens_from_input(monkeypatch, tmp_path):
    grok_home = _write_log(
        tmp_path,
        [
            _model_event(7, "grok-4.5"),
            _inference(7, "2026-07-20T14:00:00Z", prompt=1000, completion=100, cached=800),
        ],
    )

    e = _parser(monkeypatch, grok_home).collect()[0]

    assert e["input"] == 200  # prompt - cached
    assert e["cacheRead"] == 800
    assert e["output"] == 100


def test_grok_attributes_model_per_pid(monkeypatch, tmp_path):
    grok_home = _write_log(
        tmp_path,
        [
            _model_event(1, "grok-4.5"),
            _model_event(2, "grok-4.3"),
            _inference(1, "2026-07-20T13:00:00Z", prompt=100, completion=10, loop_index=1),
            _inference(2, "2026-07-20T13:00:01Z", prompt=200, completion=20, loop_index=1),
        ],
    )

    by_model = {e["model"]: e for e in _parser(monkeypatch, grok_home).collect()}

    assert by_model["grok-4.5"]["input"] == 100
    assert by_model["grok-4.3"]["input"] == 200


def test_grok_excludes_rows_it_cannot_attribute_to_a_model(monkeypatch, tmp_path):
    # No model event for this pid → unpriceable, so the row is excluded entirely (matches the
    # Grok CLI / openusage accounting) rather than bucketed under an unknown model.
    grok_home = _write_log(
        tmp_path,
        [_inference(999, "2026-07-20T13:00:00Z", prompt=100, completion=10)],
    )

    assert _parser(monkeypatch, grok_home).collect() == []


def test_grok_dedupes_identical_rows(monkeypatch, tmp_path):
    row = _inference(5, "2026-07-20T13:00:00Z", prompt=100, completion=10, loop_index=2)
    grok_home = _write_log(tmp_path, [_model_event(5, "grok-4.5"), row, row])

    assert len(_parser(monkeypatch, grok_home).collect()) == 1


def test_grok_cost_priced_from_split_is_nonzero_for_known_model(monkeypatch, tmp_path):
    # The parser leaves cost 0.0; pricing grok-4.5 against the real split yields a real cost.
    db = PricingDatabase()
    cost = db.get_cost("grok-4.5", 15482, 70, 0, 0)
    assert cost > 0.0


def test_grok_signature_tracks_the_unified_log(monkeypatch, tmp_path):
    grok_home = _write_log(tmp_path, [_model_event(1, "grok-4.5")])
    parser = _parser(monkeypatch, grok_home)

    signatures = parser._file_signatures()

    assert {Path(entry[0]).name for entry in signatures} == {"unified.jsonl"}
