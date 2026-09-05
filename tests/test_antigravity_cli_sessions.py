"""Tests for Antigravity CLI as a session source (sessions.py).

Antigravity CLI (agy) writes one SQLite DB per conversation holding a
protobuf `gen_metadata` row per LLM generation. The harness must decode
those rows with the parser's own decoder, apply the parser's skip guard
verbatim, and bill under the `split-cache-write` rule so per-turn cost is
bit-identical to AntigravityCLIParser's entries.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_antigravity_cli_parser import _create_antigravity_db, _encode_gen_metadata_blob

from tokdash import clientpaths
from tokdash import sessions
from tokdash.onboard import paths
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _antigravity_db_signatures,
    _antigravity_sessions,
    get_session_detail,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources import coding_tools
from tokdash.sources.coding_tools import BaseParser, _sig_cache

AntigravityCLIParser = coding_tools.AntigravityCLIParser

MODEL_A = "deepseek-chat"  # priced: split cache_write rate != folded input rate
MODEL_B = "claude-opus-4"  # priced
MODEL_C = "gpt-5"  # priced


def _bounds() -> tuple[int, int, int]:
    """Local midnights (ms) of two consecutive days; windows are half-open."""
    local = datetime.now().astimezone().tzinfo
    day1 = datetime(2026, 7, 20, tzinfo=local)
    s1 = int(day1.timestamp() * 1000)
    u1 = int((day1 + timedelta(days=1)).timestamp() * 1000)
    u2 = int((day1 + timedelta(days=2)).timestamp() * 1000)
    return s1, u1, u2


def _ts(ms: int) -> tuple[int, int]:
    return ms // 1000, (ms % 1000) * 1_000_000


def _patch_antigravity(monkeypatch, root: Path) -> tuple[Path, Path]:
    """Relocate the CLI home; the ACP and IDE siblings are derived from its
    parent and stay absent unless a test creates them."""
    cli_dir = root / ".gemini" / "antigravity-cli"
    conv_dir = cli_dir / "conversations"
    conv_dir.mkdir(parents=True)
    summaries = cli_dir / "conversation_summaries.db"
    monkeypatch.delenv("ANTIGRAVITY_HOME", raising=False)
    monkeypatch.setattr(clientpaths, "antigravity_cli_dir", lambda: cli_dir)
    return conv_dir, summaries


def _build_tree(conv_dir: Path, summaries_path: Path) -> None:
    """conv-alpha: one row per mapping branch; conv-beta: priced row plus a
    corrupt BLOB row; conv-gamma: no summaries row (fallback name).
    conv-notable: opens but has no gen_metadata table. conv-garbage: not
    a SQLite database. All token values come from explicit BLOB fields."""
    s1, u1, _ = _bounds()
    ts1 = s1 + 3_600_000
    ts2 = u1 + 3_600_000

    sec, nano = _ts(ts1)
    alpha_rows = [
        # idx 0: field-10 visible output + cacheWrite folded into input.
        (0, _encode_gen_metadata_blob(
            model=MODEL_A, seconds=sec, nanos=nano,
            input_tokens=1000, output_tokens=200, cache_write_tokens=50,
            cache_read_tokens=40, reasoning_tokens=20, response_output_tokens=180)),
        # idx 1: no field 10 -> output = total - reasoning.
        (1, _encode_gen_metadata_blob(
            model=MODEL_B, seconds=_ts(ts2)[0], nanos=_ts(ts2)[1],
            input_tokens=300, output_tokens=100, reasoning_tokens=40)),
        # idx 2: reasoning-only -> dropped by the parser guard.
        (2, _encode_gen_metadata_blob(
            model=MODEL_A, seconds=_ts(ts2)[0], nanos=_ts(ts2)[1],
            input_tokens=0, output_tokens=0, reasoning_tokens=50)),
        # idx 3: input + cacheWrite, zero output -> kept, cacheWrite folded.
        (3, _encode_gen_metadata_blob(
            model=MODEL_A, seconds=_ts(ts1 + 60_000)[0], nanos=_ts(ts1 + 60_000)[1],
            input_tokens=10, output_tokens=0, cache_write_tokens=50,
            response_output_tokens=0)),
        # idx 4: pure cacheWrite-only -> dropped by the verbatim guard (it
        # checks only input/output/cacheRead); both sides agree.
        (4, _encode_gen_metadata_blob(
            model=MODEL_A, seconds=_ts(ts2)[0], nanos=_ts(ts2)[1],
            input_tokens=0, output_tokens=0, cache_write_tokens=77)),
    ]
    _create_antigravity_db(conv_dir / "conv-alpha.db", alpha_rows)

    sec, nano = _ts(ts1)
    beta_rows = [
        (0, _encode_gen_metadata_blob(
            model=MODEL_C, seconds=sec, nanos=nano,
            input_tokens=500, output_tokens=50, cache_read_tokens=100,
            cache_write_tokens=25, response_output_tokens=50)),
        # Truncated protobuf -> deterministic decode failure, row skipped.
        (1, b"\x0a"),
    ]
    _create_antigravity_db(conv_dir / "conv-beta.db", beta_rows)

    sec, nano = _ts(ts1 + 120_000)
    gamma_rows = [
        (0, _encode_gen_metadata_blob(
            model=MODEL_B, seconds=sec, nanos=nano,
            input_tokens=10, output_tokens=1, response_output_tokens=1)),
    ]
    _create_antigravity_db(conv_dir / "conv-gamma.db", gamma_rows)

    conn = sqlite3.connect(str(conv_dir / "conv-notable.db"))
    with conn:
        conn.execute("CREATE TABLE junk(x INTEGER)")
    conn.close()

    (conv_dir / "conv-garbage.db").write_bytes(b"definitely not sqlite bytes 0123456789")

    conn = sqlite3.connect(str(summaries_path))
    with conn:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT,"
            " step_count INTEGER, last_modified_time INTEGER,"
            " workspace_uris TEXT, status TEXT, source TEXT,"
            " agent_name TEXT, parent_conversation_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO conversation_summaries (conversation_id, title, preview,"
            " step_count, last_modified_time, workspace_uris, status, source,"
            " agent_name, parent_conversation_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("conv-alpha", "Alpha title", "preview", 3, 0,
                 '["file:///home/user/Projects/alpha%20repo"]', "done", "cli", "agy", None),
                ("conv-beta", None, "preview", 1, 0, "[]", "done", "cli", "agy", None),
            ],
        )
    conn.close()


def _turn_sums(raw, since_ms=None, until_ms=None):
    out = {"in": 0, "cache": 0, "out": 0, "reason": 0, "cost": 0.0}
    for s in raw.values():
        for t in s["turns"]:
            ts = t["timestamp_ms"]
            if since_ms is not None and ts < since_ms:
                continue
            if until_ms is not None and ts >= until_ms:
                continue
            out["in"] += t["tokens_in"]
            out["cache"] += t["tokens_cache"]
            out["out"] += t["tokens_out"]
            out["reason"] += t["tokens_reasoning"]
            out["cost"] += t["cost"]
    return out


def _entry_sums(entries):
    return {
        "in": sum(e["input"] + e["cacheWrite"] for e in entries),
        "cache": sum(e["cacheRead"] for e in entries),
        "out": sum(e["output"] for e in entries),
        "reason": sum(e["reasoning"] for e in entries),
        "cost": sum(e["cost"] for e in entries),
    }


@pytest.fixture(autouse=True)
def _clean_caches():
    sessions._load_antigravity_sessions.cache_clear()
    sessions._load_antigravity_summaries.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._load_antigravity_sessions.cache_clear()
    sessions._load_antigravity_summaries.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def test_registered():
    assert "antigravity_cli" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["antigravity_cli"] == "Antigravity CLI"
    with pytest.raises(ValueError):
        get_sessions_data("not_a_tool", "all")


def test_empty_dir_no_error(monkeypatch, tmp_path):
    _patch_antigravity(monkeypatch, tmp_path)
    assert get_sessions_data("antigravity_cli", "all")["sessions"] == []


def test_turn_mapping(monkeypatch, tmp_path):
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    s1, _, _ = _bounds()
    raw = _antigravity_sessions()
    # conv-notable (no gen_metadata) and conv-garbage (not SQLite) skipped.
    assert set(raw) == {"conv-alpha", "conv-beta", "conv-gamma"}
    turns = raw["conv-alpha"]["turns"]
    # The reasoning-only idx-2 row and the pure cacheWrite-only idx-4 row
    # are both dropped by the parser guard (it checks only
    # input/output/cacheRead).
    assert [t["_event_key"] for t in turns] == [
        "antigravity_cli:conv-alpha:0",
        "antigravity_cli:conv-alpha:1",
        "antigravity_cli:conv-alpha:3",
    ]
    assert not any(t["tokens_in"] == 77 and t["tokens_out"] == 0 for t in turns)
    t0 = turns[0]
    assert t0["model"] == MODEL_A
    assert t0["timestamp_ms"] == s1 + 3_600_000
    assert t0["tokens_in"] == 1050  # input 1000 + cacheWrite 50
    assert t0["tokens_cache"] == 40
    assert t0["tokens_out"] == 180  # field 10 visible output
    assert t0["tokens_reasoning"] == 20
    assert t0["tokens"] == 1290
    assert t0["cost"] == pytest.approx(
        PricingDatabase().get_cost(MODEL_A, 1000, 180, 40, 50)
    )
    bill = t0["_bill"]
    assert bill["rule"] == "split-cache-write"
    assert bill["model"] == MODEL_A  # bare model, no provider prefix
    assert bill["input"] == 1000 and bill["output"] == 180
    assert bill["cache_read"] == 40 and bill["cache_write"] == 50
    assert "fixed" not in bill

    t1 = turns[1]
    assert t1["tokens_out"] == 60  # no field 10: 100 total - 40 reasoning
    assert t1["tokens_in"] == 300
    assert t1["tokens"] == 400

    t3 = turns[2]
    assert t3["tokens_in"] == 60  # 10 input + 50 cacheWrite folded
    assert t3["tokens_out"] == 0
    assert t3["tokens"] == 60
    assert t3["cost"] == pytest.approx(
        PricingDatabase().get_cost(MODEL_A, 10, 0, 0, 50)
    )

    beta = raw["conv-beta"]["turns"]
    assert len(beta) == 1  # corrupt idx-1 row skipped
    assert beta[0]["tokens_in"] == 525  # 500 + 25 cacheWrite
    assert beta[0]["tokens_cache"] == 100
    assert beta[0]["tokens_out"] == 50


def test_name_and_project(monkeypatch, tmp_path):
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    raw = _antigravity_sessions()
    alpha = raw["conv-alpha"]
    assert alpha["display_name"] == "Alpha title"
    assert "_display_name_explicit" not in alpha  # machine title: fallback level
    assert alpha["project"] == "alpha repo"  # first file:// URI, unquoted
    beta = raw["conv-beta"]
    assert "display_name" not in beta  # NULL title -> no name on the raw
    assert beta["project"] == "unknown"  # empty workspace_uris
    gamma = raw["conv-gamma"]
    assert "display_name" not in gamma  # no summaries row
    assert gamma["project"] == "unknown"
    listing = get_sessions_data("antigravity_cli", "all")["sessions"]
    by_id = {s["session_id"]: s for s in listing}
    assert by_id["conv-alpha"]["display_name"] == "Alpha title"
    assert by_id["conv-beta"]["display_name"]  # fallback name, non-empty
    assert by_id["conv-gamma"]["display_name"]  # fallback name, non-empty


def test_windowing(monkeypatch, tmp_path):
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    s1, u1, u2 = _bounds()
    day1 = datetime.fromtimestamp((s1 + 3_600_000) / 1000).strftime("%Y-%m-%d")
    day2 = datetime.fromtimestamp((u1 + 3_600_000) / 1000).strftime("%Y-%m-%d")

    all_list = get_sessions_data("antigravity_cli", "all")["sessions"]
    assert {s["session_id"] for s in all_list} == {"conv-alpha", "conv-beta", "conv-gamma"}

    def check(day, since_ms, until_ms):
        window = get_sessions_data(
            "antigravity_cli", "range", date_from=day, date_to=day
        )["sessions"]
        sums = _turn_sums(_antigravity_sessions(), since_ms=since_ms, until_ms=until_ms)
        assert sum(s["tokens"] for s in window) == (
            sums["in"] + sums["cache"] + sums["out"] + sums["reason"]
        )
        assert sum(s["cost"] for s in window) == pytest.approx(sums["cost"], abs=1e-12)
        return {s["session_id"] for s in window}

    assert check(day1, s1, u1) == {"conv-alpha", "conv-beta", "conv-gamma"}
    # Only conv-alpha has a day-2 turn (idx 1).
    assert check(day2, u1, u2) == {"conv-alpha"}


def test_repricing_applies_without_rereading(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "data"))
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    reload_pricing_db()

    base_cost = PricingDatabase().get_cost(MODEL_A, 1000, 180, 40, 50)
    raw1 = _antigravity_sessions()
    assert raw1["conv-alpha"]["turns"][0]["cost"] == pytest.approx(base_cost)

    # Dashboard-edit semantics: a valid override fully replaces the baseline.
    override = paths.pricing_db_override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        json.dumps(
            {
                "models": {
                    MODEL_A: {
                        "input": 11.0,
                        "output": 13.0,
                        "cache_read": 1.0,
                        "cache_write": 2.0,
                        "unit": "per_million_tokens",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    reload_pricing_db()

    raw2 = _antigravity_sessions()
    expected = (1000 * 11.0 + 180 * 13.0 + 40 * 1.0 + 50 * 2.0) / 1e6
    assert raw2["conv-alpha"]["turns"][0]["cost"] == pytest.approx(expected)
    assert raw2["conv-alpha"]["turns"][0]["cost"] != raw1["conv-alpha"]["turns"][0]["cost"]
    override.unlink()


def test_fail_soft_skips_bad_dbs(monkeypatch, tmp_path):
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    s1, _, _ = _bounds()
    sec, nano = _ts(s1 + 3_600_000)
    good = _encode_gen_metadata_blob(
        model=MODEL_C, seconds=sec, nanos=nano,
        input_tokens=77, output_tokens=7, response_output_tokens=7,
    )
    _create_antigravity_db(conv_dir / "conv-good.db", [(0, good)])
    (conv_dir / "conv-badbytes.db").write_bytes(b"not sqlite at all")
    conn = sqlite3.connect(str(conv_dir / "conv-notable.db"))
    with conn:
        conn.execute("CREATE TABLE junk(x INTEGER)")
    conn.close()
    _create_antigravity_db(conv_dir / "conv-badrow.db", [(0, b"\x0a")])
    raw = _antigravity_sessions()
    assert set(raw) == {"conv-good"}
    turn = raw["conv-good"]["turns"][0]
    assert turn["tokens_in"] == 77
    assert turn["tokens_out"] == 7


def test_cache_invalidation(monkeypatch, tmp_path):
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)

    calls = {"n": 0}
    original = AntigravityCLIParser._decode_row

    def counting(data):
        calls["n"] += 1
        return original(data)

    monkeypatch.setattr(AntigravityCLIParser, "_decode_row", counting)

    _antigravity_sessions()
    first = calls["n"]
    # alpha 5 rows + beta 2 rows + gamma 1 row (guard/decode skips happen
    # after the decode call).
    assert first == 8

    _antigravity_sessions()
    assert calls["n"] == first  # lru hit: nothing re-decoded

    # A WAL checkpoint changes the signature tuple -> full re-decode. The
    # scan itself sits behind the shared _SIG_TTL cache, which a real refresh
    # outlives; drop it so the touch is observed inside the test.
    (conv_dir / "conv-alpha.db-wal").touch()
    _sig_cache.clear()
    _antigravity_sessions()
    assert calls["n"] == 2 * first


def test_summary_only_edit_invalidates_sessions(monkeypatch, tmp_path):
    """conversation_summaries.db must ride in the sessions aggregate key:
    a title edit is picked up even when no conversation DB changes.
    (The summary read already has its own cache; without the key, the
    outer lru_cache would return the stale title.)"""
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    raw1 = _antigravity_sessions()
    assert raw1["conv-alpha"]["display_name"] == "Alpha title"

    conn = sqlite3.connect(str(summaries))
    with conn:
        conn.execute("DELETE FROM conversation_summaries")
        conn.execute(
            "INSERT INTO conversation_summaries (conversation_id, title,"
            " preview, step_count, last_modified_time, workspace_uris,"
            " status, source, agent_name, parent_conversation_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("conv-alpha", "Alpha title v2 (longer)", "preview", 3, 0,
             '["file:///home/user/Projects/alpha%20repo"]', "done", "cli", "agy", None),
        )
    conn.close()
    st = summaries.stat()
    os.utime(summaries, ns=(st.st_mtime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))

    raw2 = _antigravity_sessions()
    assert raw2["conv-alpha"]["display_name"] == "Alpha title v2 (longer)"


def test_summary_sidecar_signature_invalidates_summary_cache(monkeypatch, tmp_path):
    """A WAL-only change must invalidate the inner summary cache too."""
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    original_stat = summaries.stat()
    signature = {
        "value": ((str(summaries), original_stat.st_mtime_ns, original_stat.st_size),)
    }
    monkeypatch.setattr(
        sessions, "_antigravity_summary_signatures", lambda: signature["value"]
    )

    first = sessions._antigravity_summaries()
    assert first["conv-alpha"]["title"] == "Alpha title"

    conn = sqlite3.connect(str(summaries))
    with conn:
        conn.execute(
            "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
            ("Alpha t1tle", "conv-alpha"),
        )
    conn.close()
    assert summaries.stat().st_size == original_stat.st_size
    os.utime(
        summaries,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    signature["value"] += ((str(summaries) + "-wal", 1, 1),)

    second = sessions._antigravity_summaries()
    assert second["conv-alpha"]["title"] == "Alpha t1tle"


def _add_product_home(root: Path, product: str, stem: str, ts_ms: int, *, model=MODEL_A) -> Path:
    """One conversation DB under a sibling product home, no summaries DB.

    That is exactly the ACP kernel's layout: it writes conversations/*.db (plus
    .meta sidecars) and no conversation_summaries.db.
    """
    conv_dir = root / ".gemini" / product / "conversations"
    conv_dir.mkdir(parents=True, exist_ok=True)
    sec, nano = _ts(ts_ms)
    _create_antigravity_db(
        conv_dir / f"{stem}.db",
        [(0, _encode_gen_metadata_blob(
            model=model, seconds=sec, nanos=nano,
            input_tokens=700, output_tokens=90, cache_read_tokens=30,
            response_output_tokens=90))],
    )
    (conv_dir / f"{stem}.db.meta").write_bytes(b"ignored sidecar")
    return conv_dir


def test_acp_and_ide_homes_feed_overview_and_sessions(monkeypatch, tmp_path):
    """Issue #72: an ACP host (Paseo, Zed, JetBrains) spawns the official
    agy_acp_server kernel, which writes to ~/.gemini/antigravity-acp. Those
    conversations must count in Overview and list in the Sessions tab, with no
    conversation_summaries.db to name them."""
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    s1, _, _ = _bounds()
    _add_product_home(tmp_path, "antigravity-acp", "conv-acp", s1 + 7_200_000)
    _add_product_home(tmp_path, "antigravity-ide", "conv-ide", s1 + 7_260_000)

    raw = _antigravity_sessions()
    assert {"conv-acp", "conv-ide"} <= set(raw)
    # No summaries row: the stem is the fallback name, and it stays unnamed.
    assert "display_name" not in raw["conv-acp"]
    assert raw["conv-acp"]["project"] == "unknown"
    assert raw["conv-acp"]["tool"] == "antigravity_cli"

    entries = AntigravityCLIParser(PricingDatabase())._parse_all()
    assert {"antigravity_cli:conv-acp:0", "antigravity_cli:conv-ide:0"} <= {
        e["entry_id"] for e in entries
    }
    # The .meta sidecar the kernel writes is not a conversation DB.
    assert not any(".meta" in e["entry_id"] for e in entries)

    # Both sides still agree once the extra homes are in play.
    assert _turn_sums(raw)["in"] == _entry_sums(entries)["in"]
    assert _antigravity_db_signatures() == tuple(
        AntigravityCLIParser(PricingDatabase())._file_signatures()
    )


def test_acp_home_alone_is_discovered(monkeypatch, tmp_path):
    """The CLI need not be installed: an ACP-only machine still reports."""
    monkeypatch.delenv("ANTIGRAVITY_HOME", raising=False)
    monkeypatch.setattr(
        clientpaths, "antigravity_cli_dir", lambda: tmp_path / ".gemini" / "antigravity-cli"
    )
    s1, _, _ = _bounds()
    _add_product_home(tmp_path, "antigravity-acp", "conv-acp", s1 + 3_600_000)

    assert set(_antigravity_sessions()) == {"conv-acp"}
    assert get_sessions_data("antigravity_cli", "all")["sessions"][0]["session_id"] == "conv-acp"


def test_symlinked_acp_db_in_the_cli_dir_counts_once(monkeypatch, tmp_path):
    """The upgrade case: users worked around #72 by symlinking ACP conversation
    DBs into the CLI's conversations dir. Once both homes are scanned, that DB
    is reachable twice under one stem, and the stem dedup is what keeps it a
    single session with a single set of entry ids."""
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    s1, _, _ = _bounds()
    acp_dir = _add_product_home(tmp_path, "antigravity-acp", "conv-acp", s1 + 3_600_000)
    (conv_dir / "conv-acp.db").symlink_to(acp_dir / "conv-acp.db")

    entries = AntigravityCLIParser(PricingDatabase())._parse_all()
    ids = [e["entry_id"] for e in entries]
    assert len(ids) == len(set(ids))
    assert sum(1 for i in ids if i.startswith("antigravity_cli:conv-acp:")) == 1
    assert len(_antigravity_sessions()["conv-acp"]["turns"]) == 1


def test_duplicate_stem_across_homes_keeps_the_first_home(monkeypatch, tmp_path):
    """Two genuinely distinct DBs under one stem: one wins rather than the two
    interleaving. Hypothetical (a stale migration copy) -- the guard exists for
    the symlink case above -- but the tie must still break deterministically."""
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    s1, _, _ = _bounds()
    _add_product_home(tmp_path, "antigravity-acp", "conv-alpha", s1 + 3_600_000)

    entries = AntigravityCLIParser(PricingDatabase())._parse_all()
    ids = [e["entry_id"] for e in entries]
    assert len(ids) == len(set(ids))
    # The CLI home is scanned first: alpha keeps its three surviving rows, not
    # the single row the ACP-side copy holds.
    assert sum(1 for i in ids if i.startswith("antigravity_cli:conv-alpha:")) == 3
    assert len(_antigravity_sessions()["conv-alpha"]["turns"]) == 3


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """The reconciliation gate: per-window bucket sums equal
    AntigravityCLIParser's, file signatures match exactly, and the harness
    event keys equal the parser entry ids."""
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    s1, u1, u2 = _bounds()

    parser = AntigravityCLIParser(PricingDatabase())
    entries = parser._parse_all()
    windows = [(None, None), (s1, u1), (u1, u2)]
    for since_ms, until_ms in windows:
        clipped = [
            e
            for e in entries
            if (since_ms is None or e["timestamp"] >= since_ms)
            and (until_ms is None or e["timestamp"] < until_ms)
        ]
        p = _entry_sums(clipped)
        h = _turn_sums(_antigravity_sessions(), since_ms, until_ms)
        assert h["in"] == p["in"], (since_ms, until_ms)
        assert h["cache"] == p["cache"], (since_ms, until_ms)
        assert h["out"] == p["out"], (since_ms, until_ms)
        assert h["reason"] == p["reason"], (since_ms, until_ms)
        assert h["cost"] == pytest.approx(p["cost"], abs=1e-12), (since_ms, until_ms)

    assert _antigravity_db_signatures() == tuple(parser._file_signatures())
    raw = _antigravity_sessions()
    assert {t["_event_key"] for s in raw.values() for t in s["turns"]} == {
        e["entry_id"] for e in entries
    }


def test_api_surface(monkeypatch, tmp_path):
    conv_dir, summaries = _patch_antigravity(monkeypatch, tmp_path)
    _build_tree(conv_dir, summaries)
    listing = get_sessions_data("antigravity_cli", "all")
    assert listing["tool_label"] == "Antigravity CLI"
    assert {s["session_id"] for s in listing["sessions"]} == {
        "conv-alpha", "conv-beta", "conv-gamma",
    }
    detail = get_session_detail("antigravity_cli", "conv-alpha")
    assert detail["session"]["session_id"] == "conv-alpha"
    turns = detail["turns"]
    assert len(turns) == 3
    assert turns[0]["tokens_in"] == 1050
    assert "_bill" not in turns[0] and "_event_key" not in turns[0]
    assert "timestamp" in turns[0] and "timestamp_ms" not in turns[0]
    with pytest.raises(FileNotFoundError):
        get_session_detail("antigravity_cli", "does-not-exist")


def test_frontend_session_registry_includes_antigravity_cli():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'hermes', 'antigravity_cli'" in source
    assert "hermes: null, antigravity_cli: null, cline: null, workbuddy: null, qoder: null, combined: null" in source
    assert 'updateSessionPanel("antigravity_cli", lastSessionsResponses.antigravity_cli);' in source
    assert 'initSortHeaders("antigravity_cli", renderSessionsTab);' in source
    assert "antigravity_cli: { ...DEFAULT_SORT }," in source
    assert "antigravityCliSessions: 'Antigravity Sessions'," in source
    assert "antigravityCliSessions: 'Antigravity 会话'," in source
    assert 'id="antigravity_cliSessionsTable"' in source
    brand = source.split("const TOOL_BRAND_META = Object.freeze({", 1)[1].split("});", 1)[0]
    assert "antigravity_cli:" in brand
    assert (index.parent / "icons" / "agents" / "antigravity.png").is_file()
