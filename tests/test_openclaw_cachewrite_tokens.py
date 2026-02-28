import json
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
