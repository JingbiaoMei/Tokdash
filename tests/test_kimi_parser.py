from datetime import datetime, timezone
from pathlib import Path

from tokdash.pricing import PricingDatabase
from tokdash.sources.coding_tools import KimiParser


def _isolate_home(monkeypatch, tmp_path):
    """Keep tests hermetic (and Windows-safe) by faking the user home dir."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


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

    # Isolate from the real machine's ~/.kimi-code so only the fixture is seen.
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
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


def _write_new_format_session(root: Path, lines: list, session: str = "session_0000-1111") -> Path:
    session_dir = root / "sessions" / "wd_proj_abc123" / session / "agents" / "main"
    session_dir.mkdir(parents=True)
    wire_path = session_dir / "wire.jsonl"
    wire_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return wire_path


def test_kimi_parser_reads_kimi_code_usage_records(monkeypatch, tmp_path):
    """Kimi Code >=0.26 layout + usage.record schema (camelCase, ms time)."""
    code_home = tmp_path / "kimi-code"
    _write_new_format_session(
        code_home,
        [
            '{"type": "metadata", "version": 1}',
            (
                '{"type": "usage.record", "model": "kimi-code/kimi-for-coding", '
                '"usage": {"inputOther": 5821, "output": 42, "inputCacheRead": 17920, '
                '"inputCacheCreation": 0}, "usageScope": "turn", "time": 1784221371692}'
            ),
            (
                '{"type": "usage.record", "model": "kimi-code/k3", '
                '"usage": {"inputOther": 201, "output": 191, "inputCacheRead": 24832, '
                '"inputCacheCreation": 512}, "usageScope": "turn", "time": 1784221624889}'
            ),
        ],
    )

    home = _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)
    monkeypatch.setenv("KIMI_CODE_HOME", str(code_home))

    parser = KimiParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert parser.kimi_roots == [code_home, home / ".kimi"]
    assert len(entries) == 2

    first, second = entries
    # Managed alias maps to the K2.7 Coding pricing key.
    assert first["source"] == "kimi"
    assert first["provider"] == "moonshotai"
    assert first["model"] == "kimi-k2.7-code"
    assert first["input"] == 5821
    assert first["output"] == 42
    assert first["cacheRead"] == 17920
    assert first["cacheWrite"] == 0
    assert first["timestamp"] == 1784221371692  # already ms, no x1000
    assert first["entry_id"].startswith("kimi:")
    assert first["cost"] > 0.0

    # k3 maps to the canonical kimi-k3 pricing key.
    assert second["model"] == "kimi-k3"
    assert second["input"] == 201
    assert second["cacheWrite"] == 512
    assert second["timestamp"] == 1784221624889
    assert second["cost"] > 0.0

    # Distinct rows get distinct dedup ids.
    assert first["entry_id"] != second["entry_id"]


def test_kimi_parser_scans_both_old_and_new_roots(monkeypatch, tmp_path):
    """Old CLI (~/.kimi via KIMI_SHARE_DIR) and new CLI (~/.kimi-code) coexist."""
    share_dir = tmp_path / "kimi-share"
    old_dir = share_dir / "sessions" / "uid" / "sid"
    old_dir.mkdir(parents=True)
    (old_dir / "wire.jsonl").write_text(
        (
            '{"timestamp": 1772830161.3361917, "message": {"type": "StatusUpdate", '
            '"payload": {"token_usage": {"input_other": 1, "output": 2, '
            '"input_cache_read": 3, "input_cache_creation": 4}, '
            '"message_id": "chatcmpl-old"}}}\n'
        ),
        encoding="utf-8",
    )

    code_home = tmp_path / "kimi-code"
    _write_new_format_session(
        code_home,
        [
            (
                '{"type": "usage.record", "model": "kimi-code/k3", '
                '"usage": {"inputOther": 10, "output": 20, "inputCacheRead": 30, '
                '"inputCacheCreation": 40}, "usageScope": "turn", "time": 1784221371692}'
            ),
        ],
    )

    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("KIMI_SHARE_DIR", str(share_dir))
    monkeypatch.setenv("KIMI_CODE_HOME", str(code_home))

    parser = KimiParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 2
    by_model = {e["model"]: e for e in entries}
    assert by_model["kimi-k2.5"]["input"] == 1  # legacy row
    assert by_model["kimi-k3"]["input"] == 10  # new row


def test_kimi_parser_counts_all_scopes_and_dedups_within_file(monkeypatch, tmp_path):
    """Every usage.record row is an incremental delta, regardless of scope.

    usageScope is attribution metadata (turn vs. session source), so
    session-scope rows (e.g. compaction calls) must be counted too. Exact
    duplicate lines within one file still collapse, but the same row appearing
    in a different session file is distinct usage and counts again.
    """
    code_home = tmp_path / "kimi-code"
    row = (
        '{"type": "usage.record", "model": "kimi-code/k3", '
        '"usage": {"inputOther": 10, "output": 20, "inputCacheRead": 30, '
        '"inputCacheCreation": 40}, "usageScope": "turn", "time": 1784221371692}'
    )
    _write_new_format_session(
        code_home,
        [
            row,
            row,  # duplicate line within one file counts once
            (
                '{"type": "usage.record", "model": "kimi-code/k3", '
                '"usage": {"inputOther": 999, "output": 999, "inputCacheRead": 999, '
                '"inputCacheCreation": 999}, "usageScope": "session", "time": 1784221379999}'
            ),
            (
                '{"type": "usage.record", "model": "kimi-code/k3", '
                '"usage": {"inputOther": 5, "output": 5, "inputCacheRead": 5, '
                '"inputCacheCreation": 5}, "time": 1784221380000}'  # scope absent
            ),
        ],
    )
    # Same row content in a different session file is distinct usage.
    _write_new_format_session(code_home, [row], session="session_0000-2222")

    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)
    monkeypatch.setenv("KIMI_CODE_HOME", str(code_home))

    parser = KimiParser(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 4
    assert sum(e["input"] for e in entries) == 10 + 999 + 5 + 10
