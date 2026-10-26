"""Tests for CopilotCLIParser."""
import json
from datetime import datetime, timezone
from pathlib import Path

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, CopilotCLIParser, _sig_cache


def _write_jsonl(path: Path, records: list) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_copilot_cli_parser_otel_chat_span(monkeypatch, tmp_path):
    """Reads tokens from a ChatSpan OTel record."""
    otel_dir = tmp_path / ".copilot" / "otel"
    otel_dir.mkdir(parents=True)

    record = {
        "type": "span",
        "name": "chat gpt-5.2",
        "traceId": "trace-001",
        "spanId": "span-001",
        "startTime": [1748000000, 0],
        "endTime": [1748000010, 500_000_000],
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-5.2",
            "gen_ai.response.model": "gpt-5.2",
            "gen_ai.response.id": "resp-001",
            "gen_ai.usage.input_tokens": 1500,
            "gen_ai.usage.output_tokens": 200,
            "gen_ai.usage.cache_read.input_tokens": 500,
            "gen_ai.usage.cache_write.input_tokens": 0,
        },
    }
    _write_jsonl(otel_dir / "usage.jsonl", [record])

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = CopilotCLIParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "copilot_cli"
    assert e["model"] == "gpt-5.2"
    assert e["provider"] == "openai"
    # input_tokens=1500 includes cache_read=500, so fresh input = 1000
    assert e["input"] == 1000
    assert e["output"] == 200
    assert e["cacheRead"] == 500
    assert e["cacheWrite"] == 0
    # Timestamp from endTime [1748000010, 500_000_000] → 1748000010500 ms
    assert e["timestamp"] == 1748000010500


def test_copilot_cli_parser_inference_log(monkeypatch, tmp_path):
    """Reads tokens from an InferenceLog record when no ChatSpan present."""
    otel_dir = tmp_path / ".copilot" / "otel"
    otel_dir.mkdir(parents=True)

    record = {
        "traceId": "trace-002",
        "timestamp": 1748000100.0,
        "attributes": {
            "event.name": "gen_ai.client.inference.operation.details",
            "gen_ai.response.model": "gpt-5.2",
            "gen_ai.usage.input_tokens": 800,
            "gen_ai.usage.output_tokens": 100,
            "gen_ai.usage.cache_read.input_tokens": 0,
        },
    }
    _write_jsonl(otel_dir / "usage.jsonl", [record])

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = CopilotCLIParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["model"] == "gpt-5.2"
    assert e["input"] == 800
    assert e["output"] == 100


def test_copilot_cli_parser_dedup_inference_suppressed_by_chat_span(monkeypatch, tmp_path):
    """InferenceLog sharing a traceId with a ChatSpan is suppressed."""
    otel_dir = tmp_path / ".copilot" / "otel"
    otel_dir.mkdir(parents=True)

    chat_span = {
        "type": "span",
        "name": "chat gpt-5.2",
        "traceId": "shared-trace",
        "spanId": "span-001",
        "startTime": [1748000000, 0],
        "endTime": [1748000001, 0],
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.response.model": "gpt-5.2",
            "gen_ai.usage.input_tokens": 500,
            "gen_ai.usage.output_tokens": 80,
            "gen_ai.usage.cache_read.input_tokens": 0,
        },
    }
    inference_log = {
        "traceId": "shared-trace",
        "timestamp": 1748000001.0,
        "attributes": {
            "event.name": "gen_ai.client.inference.operation.details",
            "gen_ai.response.model": "gpt-5.2",
            "gen_ai.usage.input_tokens": 500,
            "gen_ai.usage.output_tokens": 80,
        },
    }
    _write_jsonl(otel_dir / "usage.jsonl", [chat_span, inference_log])

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = CopilotCLIParser(PricingDatabase())
    entries = parser.collect(None, None)
    # Only the ChatSpan should be emitted; InferenceLog is suppressed
    assert len(entries) == 1


def test_copilot_cli_parser_events_jsonl_fallback(monkeypatch, tmp_path):
    """Falls back to events.jsonl (output-only) when no OTel files present."""
    state_dir = tmp_path / ".copilot" / "session-state" / "session-xyz"
    state_dir.mkdir(parents=True)

    event = {
        "type": "assistant.message",
        "timestamp": "2026-05-21T20:17:46.413Z",
        "data": {
            "messageId": "msg-abc",
            "model": "gpt-5.2",
            "interactionId": "inter-001",
            "outputTokens": 88,
            "requestId": "req-001",
        },
    }
    _write_jsonl(state_dir / "events.jsonl", [event])

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = CopilotCLIParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "copilot_cli"
    assert e["model"] == "gpt-5.2"
    assert e["provider"] == "openai"
    assert e["output"] == 88
    assert e["input"] == 0
    assert e["cacheRead"] == 0
    expected_ts = int(datetime(2026, 5, 21, 20, 17, 46, 413000, timezone.utc).timestamp() * 1000)
    assert e["timestamp"] == expected_ts


def test_copilot_cli_parser_events_suppressed_by_otel(monkeypatch, tmp_path):
    """An events.jsonl entry whose requestId appears in OTel set is suppressed."""
    otel_dir = tmp_path / ".copilot" / "otel"
    otel_dir.mkdir(parents=True)
    state_dir = tmp_path / ".copilot" / "session-state" / "session-xyz"
    state_dir.mkdir(parents=True)

    resp_id = "resp-shared"
    otel_record = {
        "type": "span",
        "name": "chat gpt-5.2",
        "traceId": "trace-x",
        "spanId": "span-x",
        "startTime": [1748000000, 0],
        "endTime": [1748000001, 0],
        "attributes": {
            "gen_ai.operation.name": "chat",
            "gen_ai.response.model": "gpt-5.2",
            "gen_ai.response.id": resp_id,
            "gen_ai.usage.input_tokens": 300,
            "gen_ai.usage.output_tokens": 60,
            "gen_ai.usage.cache_read.input_tokens": 0,
        },
    }
    _write_jsonl(otel_dir / "usage.jsonl", [otel_record])

    event = {
        "type": "assistant.message",
        "timestamp": "2026-05-21T20:00:00.000Z",
        "data": {
            "messageId": "msg-xyz",
            "model": "gpt-5.2",
            "outputTokens": 60,
            "requestId": resp_id,  # same as OTel response id → suppressed
        },
    }
    _write_jsonl(state_dir / "events.jsonl", [event])

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = CopilotCLIParser(PricingDatabase())
    entries = parser.collect(None, None)
    # Only OTel entry; events.jsonl entry is suppressed
    assert len(entries) == 1
    assert entries[0]["input"] == 300  # OTel entry (fresh input = 300 - 0)


def test_copilot_cli_parser_provider_inference(monkeypatch, tmp_path):
    """Provider is inferred from model name."""
    otel_dir = tmp_path / ".copilot" / "otel"
    otel_dir.mkdir(parents=True)

    records = []
    for model, expected_provider in [
        ("claude-opus-4.7", "anthropic"),
        ("gemini-3-flash-preview", "google"),
        ("gpt-5.2", "openai"),
        ("some-unknown-model", "copilot"),
    ]:
        records.append({
            "type": "span",
            "name": f"chat {model}",
            "traceId": f"trace-{model}",
            "spanId": f"span-{model}",
            "startTime": [1748000000, 0],
            "endTime": [1748000001, 0],
            "attributes": {
                "gen_ai.operation.name": "chat",
                "gen_ai.response.model": model,
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.usage.cache_read.input_tokens": 0,
            },
        })
    _write_jsonl(otel_dir / "usage.jsonl", records)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    parser = CopilotCLIParser(PricingDatabase())
    entries = parser.collect(None, None)
    assert len(entries) == 4
    provider_map = {e["model"]: e["provider"] for e in entries}
    assert provider_map["claude-opus-4.7"] == "anthropic"
    assert provider_map["gemini-3-flash-preview"] == "google"
    assert provider_map["gpt-5.2"] == "openai"
    assert provider_map["some-unknown-model"] == "copilot"


def test_copilot_cli_parser_scalar_timestamp_scales():
    """Scalar OTel timestamps in s/ms/μs/ns all resolve to the same wall-clock ms.

    Regression: an earlier implementation misclassified plain-millisecond
    values like 1748000010500 (~1.748e12) as microseconds and divided by
    1000, landing them in 1970 and dropping them out of today/week/month
    windows. Thresholds now mirror ccusage's copilot::timestamp_from_scalar.
    """
    expected_ms = 1748000010500  # 2025-05-23T11:33:30.500Z
    cases = {
        "seconds":      1748000010,            # ~1.7e9
        "milliseconds": 1748000010500,         # ~1.7e12
        "microseconds": 1748000010500000,      # ~1.7e15
        "nanoseconds":  1748000010500000000,   # ~1.7e18
    }
    for label, raw in cases.items():
        got = CopilotCLIParser._parse_otel_timestamp({"timestamp": raw}, 0.0)
        # seconds-input has no sub-second resolution; everything else must be exact.
        if label == "seconds":
            assert abs(got - expected_ms) < 1000, f"{label}: got {got}"
        else:
            assert got == expected_ms, f"{label}: got {got}, want {expected_ms}"
