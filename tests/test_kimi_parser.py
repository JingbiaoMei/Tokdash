from datetime import datetime, timezone
from pathlib import Path

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import KimiParser


def test_kimi_parser_honors_kimi_share_dir(monkeypatch, tmp_path):
    share_dir = tmp_path / "kimi-share"
    session_dir = share_dir / "sessions" / "workdir-hash" / "session-id"
    session_dir.mkdir(parents=True)

    wire_path = session_dir / "wire.jsonl"
    wire_path.write_text(
        "\n".join(
            [
                '{"type": "metadata", "protocol_version": "1.3"}',
                (
                    '{"timestamp": 1772830161.3361917, "message": {"type": "StatusUpdate", '
                    '"payload": {"token_usage": {"input_other": 5543, "output": 199, '
                    '"input_cache_read": 5376, "input_cache_creation": 0}, '
                    '"message_id": "chatcmpl-test-kimi"}}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("KIMI_SHARE_DIR", str(share_dir))

    parser = KimiParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert parser.kimi_root == Path(str(share_dir))
    assert len(entries) == 1
    assert entries[0]["source"] == "kimi"
    assert entries[0]["model"] == "kimi-k2.5"
    assert entries[0]["provider"] == "moonshotai"
    assert entries[0]["input"] == 5543
    assert entries[0]["output"] == 199
    assert entries[0]["cacheRead"] == 5376
    assert entries[0]["cacheWrite"] == 0
    assert entries[0]["timestamp"] == int(datetime.fromtimestamp(1772830161.3361917, timezone.utc).timestamp() * 1000)
