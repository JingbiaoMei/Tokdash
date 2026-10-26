"""Tests for ZedParser: the threads.db snapshot (one row per thread), the
zstd/JSON blob decode behind the ClassVar decode cache, the
version-agnostic cumulative_token_usage pass-through (cache-exclusive
TokenUsage, so no subtraction), and per-OS data-dir resolution."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import zstandard

from tokdash import clientpaths, osinfo
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools as ct
from tokdash.sources.coding_tools import (
    BaseParser,
    ZedParser,
    _sig_cache,
    _zed_rfc3339_to_ms,
)

TS_A = 1_700_000_000_000
TS_B = 1_700_010_000_000

THREADS_DDL = """
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  summary TEXT,
  updated_at TEXT,
  data_type TEXT,
  data BLOB,
  parent_id TEXT,
  folder_paths TEXT,
  folder_paths_order TEXT,
  created_at TEXT
)
"""


def _doc(version=None, usage=None, model=None):
    doc = {"id": "thr_1"}
    if version is not None:
        doc["version"] = version
    if usage is not None:
        doc["cumulative_token_usage"] = usage
    if model is not None:
        doc["model"] = model
    return doc


def _usage(**counts):
    # Serde omits zero-valued TokenUsage fields (skip_serializing_if),
    # so only non-zero counts appear on the wire.
    return {k: v for k, v in counts.items() if v}


def _make_db(data_dir: Path, rows) -> Path:
    """rows: (id, updated_at, data_type, doc-dict-or-raw-bytes)."""
    threads_dir = data_dir / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    db = threads_dir / "threads.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(THREADS_DDL)
        for row_id, updated_at, data_type, payload in rows:
            if isinstance(payload, bytes):
                blob = payload
            elif data_type == "zstd":
                blob = zstandard.ZstdCompressor().compress(json.dumps(payload).encode("utf-8"))
            else:
                blob = json.dumps(payload).encode("utf-8")
            conn.execute(
                "INSERT INTO threads (id, summary, updated_at, data_type, data, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, f"summary {row_id}", updated_at, data_type, blob,
                 "2024-01-01T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def _fresh(data_dir: Path) -> ZedParser:
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    ZedParser._decode_cache.clear()
    return ZedParser(PricingDatabase())


def test_zed_parser_reads_zstd_threads(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [
            (
                "thr_a",
                "2024-01-15T10:30:00+00:00",
                "zstd",
                _doc(
                    version="0.3.0",
                    usage=_usage(
                        input_tokens=1200,
                        output_tokens=300,
                        cache_read_input_tokens=80,
                        cache_creation_input_tokens=20,
                    ),
                    model={"provider": "zed.dev", "model": "zed-editor/test-model"},
                ),
            ),
            # A zero-usage thread (all fields omitted) is skipped.
            (
                "thr_zero",
                "2024-01-15T11:00:00+00:00",
                "zstd",
                _doc(version="0.3.0", usage=_usage(), model={"provider": "p", "model": "m"}),
            ),
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    entries = _fresh(data_dir).collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert e["source"] == "zed"
    # Cache-exclusive TokenUsage: pass-through, no subtraction.
    assert (e["input"], e["output"], e["cacheRead"], e["cacheWrite"], e["reasoning"]) == (1200, 300, 80, 20, 0)
    assert e["model"] == "zed-editor/test-model"
    assert e["provider"] == "zed.dev"
    assert e["timestamp"] == 1_705_314_600_000
    assert e["entry_id"] == "zed:thr_a"
    # Cost: pricing DB only; the test model is absent from it.
    assert e["cost"] == 0.0
    assert e["_billing"]["kind"] == "pricing"


def test_zed_parser_reads_legacy_json_and_0_2_0_blobs(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    usage = _usage(input_tokens=100, output_tokens=10)
    model = {"provider": "zed.dev", "model": "legacy-model"}
    _make_db(
        data_dir,
        [
            ("thr_json", "2024-01-15T10:30:00+00:00", "json", _doc(usage=usage, model=model)),
            ("thr_020", "2024-01-15T11:30:00+00:00", "zstd",
             _doc(version="0.2.0", usage=usage, model=model)),
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    entries = _fresh(data_dir).collect(None, None)

    assert {e["entry_id"] for e in entries} == {"zed:thr_json", "zed:thr_020"}
    assert all((e["input"], e["output"]) == (100, 10) for e in entries)


def test_zed_parser_reads_a_frame_without_a_content_size(monkeypatch, tmp_path):
    """A zstd frame whose header omits the decompressed size is valid, and
    the one-shot ZstdDecompressor.decompress() rejects it ("could not
    determine content size in frame header"). _decode_row swallows the
    error and caches has_usage=False, so the install would silently report
    zero tokens; the streaming read has no such requirement."""
    compressor = zstandard.ZstdCompressor().compressobj()
    payload = json.dumps(
        _doc(version="0.3.0", usage=_usage(input_tokens=11, output_tokens=3),
             model={"provider": "anthropic", "model": "claude-sonnet-4"})
    ).encode("utf-8")
    blob = compressor.compress(payload) + compressor.flush()
    assert zstandard.frame_content_size(blob) == -1  # sizeless header

    data_dir = tmp_path / "zed"
    _make_db(data_dir, [("thr_a", "2024-01-15T10:30:00+00:00", "zstd", blob)])
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    entries = _fresh(data_dir).collect(None, None)

    assert [(e["input"], e["output"]) for e in entries] == [(11, 3)]


def test_zed_parser_skips_unusable_rows(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [
            # Versionless pre-token shape: no cumulative_token_usage.
            ("thr_v0", "2024-01-15T10:30:00+00:00", "json", _doc(version=None)),
            # Unknown data_type.
            ("thr_badtype", "2024-01-15T10:31:00+00:00", "msgpack",
             _doc(usage=_usage(input_tokens=1), model={"provider": "p", "model": "m"})),
            # Corrupt zstd blob.
            ("thr_corrupt", "2024-01-15T10:32:00+00:00", "zstd", b"\x00\x01not-zstd"),
            # Unparseable updated_at.
            ("thr_notime", "garbage", "json",
             _doc(usage=_usage(input_tokens=1), model={"provider": "p", "model": "m"})),
            # One good row so the skips are distinguishable from an empty parse.
            ("thr_ok", "2024-01-15T10:33:00+00:00", "json",
             _doc(usage=_usage(input_tokens=7), model={"provider": "p", "model": "m"})),
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    entries = _fresh(data_dir).collect(None, None)

    assert [e["entry_id"] for e in entries] == ["zed:thr_ok"]


def test_zed_parser_missing_model_is_unknown_and_unpriced(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [("thr_n", "2024-01-15T10:30:00+00:00", "zstd", _doc(version="0.3.0", usage=_usage(input_tokens=50, output_tokens=5)))],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    entries = _fresh(data_dir).collect(None, None)

    assert len(entries) == 1
    e = entries[0]
    assert (e["model"], e["provider"]) == ("unknown", "unknown")
    assert e["cost"] == 0.0


def test_zed_parser_prices_known_models(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [
            (
                "thr_p",
                "2024-01-15T10:30:00+00:00",
                "zstd",
                _doc(
                    version="0.3.0",
                    usage=_usage(input_tokens=1_000_000),
                    model={"provider": "anthropic", "model": "claude-sonnet-4"},
                ),
            )
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    entries = _fresh(data_dir).collect(None, None)

    assert len(entries) == 1
    assert entries[0]["cost"] == PricingDatabase().get_cost("claude-sonnet-4", 1_000_000, 0, 0, 0)
    assert entries[0]["cost"] > 0


def test_zed_collect_windowing(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [
            ("thr_a", "2023-11-14T22:13:20+00:00", "json",
             _doc(usage=_usage(input_tokens=1), model={"provider": "p", "model": "m"})),
            ("thr_b", "2023-11-15T02:13:20+00:00", "json",
             _doc(usage=_usage(input_tokens=2), model={"provider": "p", "model": "m"})),
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    parser = _fresh(data_dir)
    since = datetime.fromtimestamp(TS_A / 1000, timezone.utc)
    until = datetime.fromtimestamp(TS_B / 1000, timezone.utc)

    assert [e["entry_id"] for e in parser.collect(since, until)] == ["zed:thr_a"]
    assert [e["entry_id"] for e in parser.collect(until, None)] == ["zed:thr_b"]


def test_zed_decode_cache_survives_across_instances(monkeypatch, tmp_path):
    """CodingToolsUsageTracker is rebuilt on every entry point, so the cache
    must be class-level to do anything: a fresh parser sees the same (id,
    updated_at) keys and must not re-decode the blobs."""
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [
            ("thr_a", "2024-01-15T10:30:00+00:00", "zstd",
             _doc(version="0.3.0", usage=_usage(input_tokens=1), model={"provider": "p", "model": "m"})),
            ("thr_b", "2024-01-15T10:31:00+00:00", "zstd",
             _doc(version="0.3.0", usage=_usage(input_tokens=2), model={"provider": "p", "model": "m"})),
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    _fresh(data_dir)
    calls = {"n": 0}
    real_decompress = ct._zstd_decompress

    def counting(blob):
        calls["n"] += 1
        return real_decompress(blob)

    monkeypatch.setattr(ct, "_zstd_decompress", counting)
    p1 = ZedParser(PricingDatabase())
    assert len(p1._parse_all()) == 2
    assert calls["n"] == 2
    p2 = ZedParser(PricingDatabase())
    assert len(p2._parse_all()) == 2
    assert calls["n"] == 2


def _count_decompressions(monkeypatch) -> dict:
    calls = {"n": 0}
    real = ct._zstd_decompress

    def counting(blob):
        calls["n"] += 1
        return real(blob)

    monkeypatch.setattr(ct, "_zstd_decompress", counting)
    return calls


def test_zed_decode_cache_holds_every_scanned_thread(monkeypatch, tmp_path):
    """_parse_all walks every thread in table order on each refresh, so a
    bound below the thread count would evict exactly the entries the next
    scan reaches first (zero hits). 40 threads is well past the old FIFO
    bound of 32."""
    n = 40
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [
            (f"thr_{i:02d}", f"2024-01-15T10:00:{i:02d}+00:00", "zstd",
             _doc(version="0.3.0", usage=_usage(input_tokens=1),
                  model={"provider": "p", "model": "m"}))
            for i in range(n)
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    _fresh(data_dir)
    calls = _count_decompressions(monkeypatch)
    parser = ZedParser(PricingDatabase())

    assert len(parser._parse_all()) == n
    assert calls["n"] == n
    assert len(ZedParser._decode_cache) == n
    # Second and third refreshes decode nothing.
    assert len(parser._parse_all()) == n
    assert len(ZedParser(PricingDatabase())._parse_all()) == n
    assert calls["n"] == n


def test_zed_decode_cache_drops_threads_absent_from_the_scan(monkeypatch, tmp_path):
    """Eviction is by absence from the current scan: a deleted thread (or a
    superseded updated_at) must not sit in the cache forever."""
    data_dir = tmp_path / "zed"
    db = _make_db(
        data_dir,
        [
            ("thr_a", "2024-01-15T10:30:00+00:00", "json",
             _doc(version="0.3.0", usage=_usage(input_tokens=1),
                  model={"provider": "p", "model": "m"})),
            ("thr_b", "2024-01-15T10:31:00+00:00", "json",
             _doc(version="0.3.0", usage=_usage(input_tokens=2),
                  model={"provider": "p", "model": "m"})),
        ],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    parser = _fresh(data_dir)
    assert len(parser._parse_all()) == 2
    assert set(ZedParser._decode_cache) == {
        ("thr_a", "2024-01-15T10:30:00+00:00"),
        ("thr_b", "2024-01-15T10:31:00+00:00"),
    }

    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM threads WHERE id = 'thr_b'")
        conn.commit()
    finally:
        conn.close()
    _sig_cache.clear()

    assert len(parser._parse_all()) == 1
    assert set(ZedParser._decode_cache) == {("thr_a", "2024-01-15T10:30:00+00:00")}


def test_zed_failed_decode_is_not_cached(monkeypatch, tmp_path):
    """A transient decode error must not pin the thread at zero tokens
    until Zed next saves it under a new updated_at."""
    data_dir = tmp_path / "zed"
    _make_db(
        data_dir,
        [("thr_a", "2024-01-15T10:30:00+00:00", "zstd",
          _doc(version="0.3.0", usage=_usage(input_tokens=7),
               model={"provider": "p", "model": "m"}))],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    parser = _fresh(data_dir)
    real = ct._zstd_decompress
    failing = {"on": True}

    def flaky(blob):
        if failing["on"]:
            raise RuntimeError("transient read error")
        return real(blob)

    monkeypatch.setattr(ct, "_zstd_decompress", flaky)
    assert parser._parse_all() == []
    assert ZedParser._decode_cache == {}

    failing["on"] = False
    assert [e["input"] for e in parser._parse_all()] == [7]


def test_zed_file_signatures_fold_sidecars_into_one_entry(monkeypatch, tmp_path):
    """file_replace sync parses once per signature entry
    (compute._collect_parser_file), so a sidecar must move the signature
    without adding an entry."""
    data_dir = tmp_path / "zed"
    db = _make_db(
        data_dir,
        [("thr_a", "2024-01-15T10:30:00+00:00", "json",
          _doc(version="0.3.0", usage=_usage(input_tokens=1),
               model={"provider": "p", "model": "m"}))],
    )
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    parser = _fresh(data_dir)
    before = parser._file_signatures()
    assert [s[0] for s in before] == [str(db)]

    Path(str(db) + "-wal").write_bytes(b"x" * 64)
    Path(str(db) + "-shm").write_bytes(b"y" * 32)
    _sig_cache.clear()
    after = parser._file_signatures()
    assert [s[0] for s in after] == [str(db)]
    assert after != before


def test_zed_parser_no_db_is_a_noop(monkeypatch, tmp_path):
    data_dir = tmp_path / "zed"
    data_dir.mkdir()
    monkeypatch.setattr(clientpaths, "zed_data_dir", lambda: data_dir)
    parser = _fresh(data_dir)
    assert parser.db_path is None
    assert parser.collect(None, None) == []
    assert parser._file_signatures() == ()


def test_zed_data_dir_per_os(monkeypatch, tmp_path):
    monkeypatch.delenv("FLATPAK_XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    monkeypatch.setattr(osinfo, "os_kind", lambda: "macos")
    assert clientpaths.zed_data_dir() == Path.home() / "Library" / "Application Support" / "Zed"

    monkeypatch.setattr(osinfo, "os_kind", lambda: "windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert clientpaths.zed_data_dir() == tmp_path / "Local" / "Zed"
    monkeypatch.delenv("LOCALAPPDATA")
    assert clientpaths.zed_data_dir() == Path.home() / "AppData" / "Local" / "Zed"

    monkeypatch.setattr(osinfo, "os_kind", lambda: "linux")
    monkeypatch.setenv("FLATPAK_XDG_DATA_HOME", str(tmp_path / "flatpak"))
    assert clientpaths.zed_data_dir() == tmp_path / "flatpak" / "zed"
    monkeypatch.delenv("FLATPAK_XDG_DATA_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert clientpaths.zed_data_dir() == tmp_path / "xdg" / "zed"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert clientpaths.zed_data_dir() == Path.home() / ".local" / "share" / "zed"


def test_zed_rfc3339_to_ms_variants():
    assert _zed_rfc3339_to_ms("2024-01-15T10:30:00+00:00") == 1_705_314_600_000
    assert _zed_rfc3339_to_ms("2024-01-15T10:30:00Z") == 1_705_314_600_000
    assert _zed_rfc3339_to_ms("2024-01-15T12:30:00+02:00") == 1_705_314_600_000
    assert _zed_rfc3339_to_ms("2024-01-15T10:30:00+0000") == 1_705_314_600_000
    # Nanosecond fractions normalize to microsecond precision (dropping the rest).
    assert _zed_rfc3339_to_ms("2024-01-15T10:30:00.123456789+00:00") == 1_705_314_600_123
    assert _zed_rfc3339_to_ms("garbage") is None
    assert _zed_rfc3339_to_ms("") is None
    assert _zed_rfc3339_to_ms(None) is None
