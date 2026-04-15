import json
from datetime import datetime, timezone
from pathlib import Path

from tokdash.sources.openclaw import get_session_usage


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_cachewrite_is_counted_as_input_tokens(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # github-copilot reports most prompt tokens under cacheWrite instead of input.
    _write_jsonl(
        sessions_dir / "sess.jsonl",
        [
            {
                "type": "message",
                "timestamp": 1700000000,
                "message": {
                    "role": "assistant",
                    "provider": "github-copilot",
                    "model": "claude-opus-4.6",
                    "usage": {"input": 5, "cacheWrite": 27_000, "cacheRead": 100, "output": 50},
                },
            }
        ],
    )

    result = get_session_usage(str(sessions_dir))
    model_key = "github-copilot/claude-opus-4.6"

    assert result["total_messages"] == 1
    assert result["models"][model_key]["tokens_in"] == 27_005
    assert result["models"][model_key]["tokens_out"] == 50
    assert result["models"][model_key]["tokens_cache"] == 100
    assert result["models"][model_key]["tokens"] == (27_005 + 50 + 100)
    assert result["total_tokens"] == (27_005 + 50 + 100)

    day = result["contributions"][0]
    assert day["tokenBreakdown"]["input"] == 27_005
    assert day["tokenBreakdown"]["output"] == 50
    assert day["tokenBreakdown"]["cacheRead"] == 100
    assert day["totals"]["tokens"] == (27_005 + 50 + 100)


def test_cachewrite_token_alias_keys_are_supported(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    _write_jsonl(
        sessions_dir / "sess.jsonl",
        [
            {
                "type": "message",
                "timestamp": "2026-02-27T12:34:56Z",
                "message": {
                    "role": "assistant",
                    "provider": "minimax",
                    "model": "MiniMax-M2.5",
                    "usage": {"inputTokens": 24, "cacheWriteTokens": 100, "cacheReadTokens": 10, "outputTokens": 5},
                },
            }
        ],
    )

    result = get_session_usage(str(sessions_dir))
    model_key = "minimax/MiniMax-M2.5"
    assert result["models"][model_key]["tokens_in"] == 124
    assert result["models"][model_key]["tokens_out"] == 5
    assert result["models"][model_key]["tokens_cache"] == 10
    assert result["models"][model_key]["tokens"] == 139


def test_inner_message_timestamp_is_used_for_date_filtering(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    _write_jsonl(
        sessions_dir / "sess.jsonl",
        [
            {
                "type": "message",
                "timestamp": "2026-04-15T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "timestamp": 1700000000000,  # 2023-11-14T22:13:20Z
                    "provider": "infini-ai",
                    "model": "glm-5.1",
                    "usage": {"input": 100, "cacheRead": 10, "output": 5},
                },
            }
        ],
    )

    april_15 = datetime(2026, 4, 15, tzinfo=timezone.utc)
    april_16 = datetime(2026, 4, 16, tzinfo=timezone.utc)
    nov_14 = datetime(2023, 11, 14, tzinfo=timezone.utc)
    nov_15 = datetime(2023, 11, 15, tzinfo=timezone.utc)

    result_april = get_session_usage(str(sessions_dir), since_date=april_15, until_date=april_16)
    assert result_april["total_messages"] == 0
    assert result_april["total_tokens"] == 0

    result_nov = get_session_usage(str(sessions_dir), since_date=nov_14, until_date=nov_15)
    assert result_nov["total_messages"] == 1
    assert result_nov["total_tokens"] == 115


def test_archived_and_checkpoint_transcripts_are_included(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    row = {
        "type": "message",
        "timestamp": "2026-04-15T00:00:01Z",
        "message": {
            "role": "assistant",
            "timestamp": "2026-04-15T00:00:01Z",
            "provider": "infini-ai",
            "model": "glm-5.1",
            "usage": {"input": 10, "cacheRead": 1, "output": 2},
        },
    }

    _write_jsonl(sessions_dir / "base.jsonl", [row])
    _write_jsonl(sessions_dir / "archived.jsonl.deleted.123", [row])
    _write_jsonl(sessions_dir / "reset.jsonl.reset.456", [row])
    _write_jsonl(sessions_dir / "ckpt.checkpoint.789.jsonl", [row])

    result = get_session_usage(str(sessions_dir))
    model_key = "infini-ai/glm-5.1"

    assert result["total_messages"] == 4
    assert result["models"][model_key]["messages"] == 4
    assert result["models"][model_key]["tokens"] == 52
