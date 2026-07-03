"""Tests for Antigravity CLI usage parsing."""

import sqlite3
import time
from pathlib import Path

from tokdash import clientpaths
from tokdash.compute import _collect_parser_file
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools
from tokdash.sources.coding_tools import BaseParser, _sig_cache
from tokdash.usage_store import UsageEntryStore


AntigravityCLIParser = getattr(coding_tools, "AntigravityCLIParser", None)


def _get_parser_class():
    assert AntigravityCLIParser is not None, "AntigravityCLIParser is not yet implemented."
    return AntigravityCLIParser


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _encode_tag(field: int, wire_type: int) -> bytes:
    return _encode_varint((field << 3) | wire_type)


def _encode_varint_field(field: int, value: int) -> bytes:
    return _encode_tag(field, 0) + _encode_varint(int(value))


def _encode_len_field(field: int, raw: bytes) -> bytes:
    return _encode_tag(field, 2) + _encode_varint(len(raw)) + raw


def _encode_gen_metadata_blob(
    *,
    model: str,
    seconds: int,
    nanos: int,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int | None = None,
    response_output_tokens: int | None = None,
    display_name: str | None = None,
) -> bytes:
    # Build path 1.9.4.1/1.9.4.2.
    ts_inner = _encode_varint_field(1, seconds) + _encode_varint_field(2, nanos)
    ts_msg = _encode_len_field(4, ts_inner)
    ts_outer = _encode_len_field(9, ts_msg)

    # Build path 1.4.{2,3,4,5,9}.
    usage_fields = [
        _encode_varint_field(2, input_tokens),
        _encode_varint_field(3, output_tokens),
        _encode_varint_field(4, cache_write_tokens),
        _encode_varint_field(5, cache_read_tokens),
    ]
    if reasoning_tokens is not None:
        usage_fields.append(_encode_varint_field(9, reasoning_tokens))
    if response_output_tokens is not None:
        usage_fields.append(_encode_varint_field(10, response_output_tokens))
    usage_inner = b"".join(usage_fields)
    usage_msg = _encode_len_field(4, usage_inner)

    top_inner = ts_outer + usage_msg
    top_inner += _encode_len_field(19, model.encode("utf-8"))
    if display_name is not None:
        top_inner += _encode_len_field(21, display_name.encode("utf-8"))

    return _encode_len_field(1, top_inner)


def _create_antigravity_db(path: Path, rows: list[tuple[int, bytes | bytearray]]) -> None:
    conn = sqlite3.connect(str(path))
    with conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB, size)")
        cur.executemany(
            "INSERT INTO gen_metadata VALUES (?, ?, ?)",
            [(idx, sqlite3.Binary(data), len(data)) for idx, data in rows],
        )


def _prepare_antigravity_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    antigravity_cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    conversations_dir = antigravity_cli_dir / "conversations"
    conversations_dir.mkdir(parents=True)
    monkeypatch.setattr(clientpaths, "antigravity_cli_dir", lambda: antigravity_cli_dir)
    monkeypatch.setattr(clientpaths, "antigravity_conversations_dir", lambda: conversations_dir, raising=False)
    monkeypatch.setattr(
        clientpaths,
        "antigravity_conversations_glob",
        lambda: str(conversations_dir / "*.db"),
        raising=False,
    )
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    return conversations_dir


def test_antigravity_cli_parser_maps_gemini_and_claude_fields(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)

    db_path = conversations_dir / "session-abc.db"
    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    display_name="Gemini 3.5 Flash (a)",
                    seconds=1_800_000_000,
                    nanos=456_000_000,
                    input_tokens=120,
                    output_tokens=80,
                    cache_write_tokens=10,
                    cache_read_tokens=70,
                    reasoning_tokens=55,
                    response_output_tokens=25,
                ),
            ),
            (
                2,
                _encode_gen_metadata_blob(
                    model="claude-opus-4-6-thinking",
                    display_name="Claude Opus 4.6",
                    seconds=1_800_000_001,
                    nanos=123_000_000,
                    input_tokens=300,
                    output_tokens=150,
                    cache_write_tokens=20,
                    cache_read_tokens=15,
                    response_output_tokens=150,
                ),
            ),
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 2

    gemini = entries[0]
    assert gemini["source"] == "antigravity_cli"
    assert gemini["provider"] == "google"
    assert gemini["model"] == "gemini-3-flash-a"
    assert gemini["entry_id"] == "antigravity_cli:session-abc:1"
    assert gemini["input"] == 120
    assert gemini["output"] == 25
    assert gemini["cacheRead"] == 70
    assert gemini["cacheWrite"] == 10
    assert gemini["reasoning"] == 55
    assert gemini["timestamp"] == 1_800_000_000_000 + 456

    claude = entries[1]
    assert claude["source"] == "antigravity_cli"
    assert claude["provider"] == "anthropic"
    assert claude["model"] == "claude-opus-4-6-thinking"
    assert claude["entry_id"] == "antigravity_cli:session-abc:2"
    assert claude["input"] == 300
    assert claude["output"] == 150
    assert claude["cacheRead"] == 15
    assert claude["cacheWrite"] == 20
    assert claude["reasoning"] == 0
    assert claude["timestamp"] == 1_800_000_001_000 + 123


def test_antigravity_cli_parser_falls_back_to_total_minus_reasoning_when_visible_output_absent(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "missing-visible-output.db"
    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_000_010,
                    nanos=0,
                    input_tokens=10,
                    output_tokens=80,
                    reasoning_tokens=55,
                ),
            )
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    assert entries[0]["output"] == 25
    assert entries[0]["reasoning"] == 55


def test_antigravity_cli_parser_skips_corrupt_rows_and_zero_rows(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "broken-rows.db"

    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_001_000,
                    nanos=5_000_000,
                    input_tokens=100,
                    output_tokens=40,
                    cache_read_tokens=20,
                ),
            ),
            (2, b"not-a-valid-protobuf"),
            (3, _encode_gen_metadata_blob(model="gemini-3-flash-a", seconds=1_800_001_001, nanos=0, input_tokens=0, output_tokens=0, cache_read_tokens=0)),
            (
                4,
                _encode_gen_metadata_blob(
                    model="claude-opus-4-6-thinking",
                    seconds=1_800_001_002,
                    nanos=0,
                    input_tokens=7,
                    output_tokens=3,
                    cache_read_tokens=0,
                    cache_write_tokens=1,
                    reasoning_tokens=2,
                ),
            ),
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 2
    assert {entry["entry_id"] for entry in entries} == {
        "antigravity_cli:broken-rows:1",
        "antigravity_cli:broken-rows:4",
    }


def test_antigravity_cli_parser_ignores_pb_files(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "session-pb.db"
    pb_path = conversations_dir / "legacy-session.pb"
    pb_path.write_bytes(_encode_gen_metadata_blob(model="gemini-3-flash-a", seconds=1, nanos=0, input_tokens=99, output_tokens=99, cache_read_tokens=0))

    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_002_000,
                    nanos=111_000_000,
                    input_tokens=10,
                    output_tokens=11,
                ),
            )
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 1
    assert entries[0]["entry_id"] == "antigravity_cli:session-pb:1"


def test_antigravity_cli_parser_skips_missing_gen_metadata_table(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    missing_table_db = conversations_dir / "missing-table.db"

    conn = sqlite3.connect(str(missing_table_db))
    with conn:
        conn.execute("CREATE TABLE other_table(id INTEGER)")

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)
    assert entries == []


def test_antigravity_cli_parser_retries_plain_connect_when_readonly_execute_fails(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "wal-recovery.db"
    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_002_100,
                    nanos=0,
                    input_tokens=10,
                    output_tokens=12,
                    response_output_tokens=12,
                ),
            )
        ],
    )

    original_connect = coding_tools.sqlite3.connect
    calls: list[str] = []

    class ExecuteFailsConnection:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        def close(self):
            pass

    def flaky_connect(target, *args, **kwargs):
        target_s = str(target)
        if kwargs.get("uri"):
            calls.append("readonly")
            return ExecuteFailsConnection()
        calls.append("plain")
        return original_connect(target_s, *args, **kwargs)

    monkeypatch.setattr(coding_tools.sqlite3, "connect", flaky_connect)

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)

    assert calls == ["readonly", "plain"]
    assert len(entries) == 1
    assert entries[0]["entry_id"] == "antigravity_cli:wal-recovery:1"


def test_antigravity_cli_parser_entry_id_is_stable_and_cached(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "session-cache.db"

    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_003_000,
                    nanos=0,
                    input_tokens=12,
                    output_tokens=7,
                ),
            )
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    first = parser.collect(None, None)
    second = parser.collect(None, None)

    assert first == second
    assert first[0]["entry_id"] == "antigravity_cli:session-cache:1"


def test_antigravity_cli_file_signatures_include_wal_state_in_db_signature(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "wal-signature.db"
    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_004_000,
                    nanos=0,
                    input_tokens=1,
                    output_tokens=1,
                ),
            )
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    first = parser._file_signatures()
    assert len(first) == 1
    assert Path(first[0][0]).name == "wal-signature.db"

    wal = db_path.with_suffix(db_path.suffix + "-wal")
    wal.write_bytes(b"wal marker")
    # Ensure the signature changes when WAL metadata changes.
    time.sleep(0.01)
    _sig_cache.clear()
    second = parser._file_signatures()

    assert len(second) == 1
    assert Path(second[0][0]).name == "wal-signature.db"
    assert second != first


def test_antigravity_cli_pricing_aliases_resolve_non_zero_cost(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "session-pricing.db"

    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_005_000,
                    nanos=0,
                    input_tokens=1000,
                    output_tokens=2000,
                ),
            ),
            (
                2,
                _encode_gen_metadata_blob(
                    model="gemini-3-pro-high",
                    seconds=1_800_005_001,
                    nanos=0,
                    input_tokens=300,
                    output_tokens=700,
                ),
            ),
            (
                3,
                _encode_gen_metadata_blob(
                    model="gemini-3-pro-low",
                    seconds=1_800_005_002,
                    nanos=0,
                    input_tokens=333,
                    output_tokens=777,
                ),
            ),
            (
                4,
                _encode_gen_metadata_blob(
                    model="claude-opus-4-6-thinking",
                    seconds=1_800_005_003,
                    nanos=0,
                    input_tokens=111,
                    output_tokens=222,
                    reasoning_tokens=0,
                ),
            ),
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    entries = parser.collect(None, None)

    assert len(entries) == 4
    by_model = {entry["model"]: entry["cost"] for entry in entries}
    assert by_model["gemini-3-flash-a"] > 0.0
    assert by_model["gemini-3-pro-high"] > 0.0
    assert by_model["gemini-3-pro-low"] > 0.0
    assert by_model["claude-opus-4-6-thinking"] > 0.0


def test_antigravity_cli_parser_scales_across_many_small_dbs(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    expected_rows = 0

    for db_index in range(50):
        rows = []
        for row_index in range(100):
            expected_rows += 1
            rows.append(
                (
                    row_index,
                    _encode_gen_metadata_blob(
                        model="gemini-3-flash-a",
                        seconds=1_800_010_000 + db_index,
                        nanos=row_index,
                        input_tokens=10 + row_index,
                        output_tokens=2,
                    ),
                )
            )
        _create_antigravity_db(conversations_dir / f"scale-{db_index:03d}.db", rows)

    parser = _get_parser_class()(PricingDatabase())
    entries = parser._parse_all()

    assert len(entries) == expected_rows


def test_antigravity_cli_collect_uses_entry_cache_when_signatures_unchanged(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    _create_antigravity_db(
        conversations_dir / "cached.db",
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_020_000,
                    nanos=0,
                    input_tokens=5,
                    output_tokens=6,
                ),
            )
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    assert len(parser.collect(None, None)) == 1

    calls = {"count": 0}
    original_connect = coding_tools.sqlite3.connect

    def counting_connect(*args, **kwargs):
        calls["count"] += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(coding_tools.sqlite3, "connect", counting_connect)
    assert len(parser.collect(None, None)) == 1
    assert calls["count"] == 0


def test_antigravity_cli_file_replace_sync_reparses_only_changed_db(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_a = conversations_dir / "a.db"
    db_b = conversations_dir / "b.db"
    _create_antigravity_db(
        db_a,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_030_000,
                    nanos=0,
                    input_tokens=10,
                    output_tokens=1,
                ),
            )
        ],
    )
    _create_antigravity_db(
        db_b,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_030_001,
                    nanos=0,
                    input_tokens=20,
                    output_tokens=1,
                ),
            )
        ],
    )

    parser = _get_parser_class()(PricingDatabase())
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    calls: list[str] = []

    def parse_file(file_sig):
        calls.append(Path(file_sig[0]).name)
        return _collect_parser_file(parser, file_sig)

    files_v1 = parser._file_signatures()
    assert store.sync_files("antigravity_cli", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == ["a.db", "b.db"]

    assert store.sync_files("antigravity_cli", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is False
    assert calls == ["a.db", "b.db"]

    wal_b = Path(str(db_b) + "-wal")
    wal_b.write_bytes(b"changed")
    time.sleep(0.01)
    _sig_cache.clear()
    files_v2 = parser._file_signatures()

    assert store.sync_files("antigravity_cli", files_v2, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == ["a.db", "b.db", "b.db"]
    assert store.aggregate_entries(sources=["antigravity_cli"])["total_messages"] == 2


def test_antigravity_cli_signature_scan_only_stats_db_and_sidecars(monkeypatch, tmp_path):
    conversations_dir = _prepare_antigravity_home(monkeypatch, tmp_path)
    db_path = conversations_dir / "scan.db"
    _create_antigravity_db(
        db_path,
        [
            (
                1,
                _encode_gen_metadata_blob(
                    model="gemini-3-flash-a",
                    seconds=1_800_040_000,
                    nanos=0,
                    input_tokens=1,
                    output_tokens=1,
                ),
            )
        ],
    )
    (conversations_dir / "scan.db-wal").write_bytes(b"wal")
    (conversations_dir / "scan.db-shm").write_bytes(b"shm")
    (conversations_dir / "legacy.pb").write_bytes(b"ignored")
    (conversations_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    parser = _get_parser_class()(PricingDatabase())
    stat_calls: list[str] = []
    original_stat = Path.stat

    def counting_stat(self: Path, *args, **kwargs):
        stat_calls.append(self.name)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)

    assert parser._file_signatures()

    assert set(stat_calls) == {"scan.db", "scan.db-wal", "scan.db-shm"}
