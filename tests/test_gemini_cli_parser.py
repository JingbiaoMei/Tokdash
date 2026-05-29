from datetime import datetime, timezone
from pathlib import Path

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import BaseParser, GeminiCLIParser, _sig_cache


def test_gemini_cli_parser_reads_jsonl_sessions(monkeypatch, tmp_path):
    session_dir = tmp_path / ".gemini" / "tmp" / "project-hash" / "chats"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session-2026-05-20T17-11-a4743980.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"sessionId":"s1","projectHash":"project-hash","startTime":"2026-05-20T17:11:00Z"}',
                '{"id":"u1","type":"user","timestamp":"2026-05-20T17:11:01Z","content":"hello"}',
                (
                    '{"id":"g1","type":"gemini","timestamp":"2026-05-20T17:11:34.898Z",'
                    '"model":"gemini-3-flash-preview",'
                    '"tokens":{"input":323406,"output":1570,"cached":228806,'
                    '"thoughts":2314,"tool":0,"total":327290}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _sig_cache.clear()
    BaseParser._entry_cache.clear()

    entries = GeminiCLIParser(PricingDatabase()).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["source"] == "gemini_cli"
    assert entries[0]["provider"] == "google"
    assert entries[0]["model"] == "gemini-3-flash-preview"
    # Gemini CLI's tokens.input (323406) is INCLUSIVE of the cached prompt tokens
    # (228806). The parser subtracts to recover the fresh/uncached portion so cached
    # tokens are not double-counted in totals/cost. See compute.cache_hit_rate.
    assert entries[0]["input"] == 323406 - 228806  # 94600 fresh prompt tokens
    assert entries[0]["output"] == 1570
    assert entries[0]["cacheRead"] == 228806
    assert entries[0]["cacheWrite"] == 0
    assert entries[0]["reasoning"] == 2314
    # Full prompt input is recovered as fresh + cacheRead == the original 323406, and
    # the reconstructed total now matches Gemini's own reported `total` (327290) with
    # no double-count: 94600 + 1570 + 228806 + 2314 == 327290.
    assert entries[0]["input"] + entries[0]["cacheRead"] == 323406
    assert (
        entries[0]["input"] + entries[0]["output"] + entries[0]["cacheRead"] + entries[0]["reasoning"]
        == 327290
    )
    assert entries[0]["timestamp"] == int(
        datetime(2026, 5, 20, 17, 11, 34, 898000, timezone.utc).timestamp() * 1000
    )
