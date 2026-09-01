"""Tests for omp (oh-my-pi) as a session source (sessions.py).

omp shares pi's JSONL format, so the harness is a dispatch branch over the
generalized pi-file core: recorded cost ignored (O6), the ``title`` row, the
O3 split of a qualified ``model_change``, and the pi/omp dir-ownership rule.
The load-bearing property is bucket-for-bucket parity with OmpParser,
including the resume continuation that re-logs a row under the same session
UUID with the same outer id.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.dateutil import parse_date_range
from tokdash.pricing import PricingDatabase
from tokdash.sessions import (
    SESSION_TOOLS,
    _omp_session_roots,
    _omp_sessions,
    _pi_sessions,
    get_sessions_data,
    reload_pricing_db,
)
from tokdash.sources.coding_tools import BaseParser, OmpParser, _sig_cache

from test_omp_parser import _omp_session_lines

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"

BUCKET = "--tmp-project--"
CWD = "/tmp/project"


@pytest.fixture(autouse=True)
def _clean_omp_state(monkeypatch):
    # Isolate the search dirs: no XDG omp tree, no PI_* overrides, no profiles.
    for var in (
        "XDG_DATA_HOME",
        "PI_CONFIG_DIR",
        "PI_CODING_AGENT_DIR",
        "PI_CODING_AGENT_SESSION_DIR",
        "PI_AGENT_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    sessions._load_omp_sessions.cache_clear()
    sessions._load_pi_sessions.cache_clear()
    sessions._parse_omp_session_file.cache_clear()
    sessions._parse_pi_session_file.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()
    yield
    sessions._load_omp_sessions.cache_clear()
    sessions._load_pi_sessions.cache_clear()
    sessions._parse_omp_session_file.cache_clear()
    sessions._parse_pi_session_file.cache_clear()
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    reload_pricing_db()


def _fresh_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    return home


def _home_env(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: home)


def _usage(inp, out, cr=0, cw=0, total=None, cost_total=None):
    usage = {
        "input": inp,
        "output": out,
        "cacheRead": cr,
        "cacheWrite": cw,
        "totalTokens": total if total is not None else inp + out + cr + cw,
    }
    if cost_total is not None:
        usage["cost"] = {"total": cost_total}
    return usage


def _assistant_line(msg_id, ts_iso, usage, model="deepseek-chat", provider="deepseek"):
    return json.dumps(
        {
            "type": "message",
            "id": msg_id,
            "timestamp": ts_iso,
            "message": {"role": "assistant", "provider": provider, "model": model, "usage": usage},
        }
    )


def _user_line(ts_iso):
    return json.dumps(
        {
            "type": "message",
            "id": "user0000",
            "timestamp": ts_iso,
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        }
    )


def _session_header(session_id, ts_iso):
    return [
        json.dumps({"type": "title", "v": 1, "title": "", "updatedAt": ts_iso}),
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": ts_iso,
                "cwd": CWD,
            }
        ),
        json.dumps(
            {
                "type": "model_change",
                "id": "d5fefefb",
                "parentId": None,
                "timestamp": ts_iso,
                "model": "deepseek/deepseek-chat",
            }
        ),
    ]


def _write_session_file(home: Path, name: str, session_id: str, ts_iso, lines) -> Path:
    path = home / ".omp" / "agent" / "sessions" / BUCKET / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_session_header(session_id, ts_iso) + lines) + "\n",
                    encoding="utf-8")
    return path


def _parser_totals(home: Path, since=None, until=None):
    parser = OmpParser(PricingDatabase())
    entries = parser.collect(since, until)
    return {
        "count": len(entries),
        "input": sum(e["input"] for e in entries),
        "output": sum(e["output"] for e in entries),
        "cacheRead": sum(e["cacheRead"] for e in entries),
        "cacheWrite": sum(e["cacheWrite"] for e in entries),
        "cost": sum(e["cost"] for e in entries),
    }


def _listing_totals(listing):
    return {
        "count": sum(s["token_events"] for s in listing["sessions"]),
        "input": sum(s["tokens_in"] for s in listing["sessions"]),
        "cache": sum(s["tokens_cache"] for s in listing["sessions"]),
        "output": sum(s["tokens_out"] for s in listing["sessions"]),
        "cost": sum(s["cost"] for s in listing["sessions"]),
    }


def _write_parity_tree(home: Path) -> None:
    # S1 (day 1): a normal row + a warm-cache row.
    _write_session_file(
        home,
        f"2026-08-22T10-44-58_{S1}.jsonl",
        S1,
        "2026-08-22T10:44:58.954Z",
        [
            _user_line("2026-08-22T10:44:59.498Z"),
            _assistant_line("a1000001", "2026-08-22T10:45:01.007Z",
                            _usage(3342, 46, cost_total=0.0007328)),
            _assistant_line("a1000002", "2026-08-22T11:00:00.000Z",
                            _usage(206, 33, cr=3136)),
        ],
    )
    # S2 (day 2): a resume — file 2 re-logs r1 with the same outer id and
    # identical usage, then adds r3.
    r1 = _assistant_line("aaaa1111", "2026-08-23T09:00:00.000Z", _usage(100, 10, cw=25))
    r2 = _assistant_line("bbbb2222", "2026-08-23T09:01:00.000Z", _usage(50, 5, cr=10))
    r3 = _assistant_line("cccc3333", "2026-08-23T09:02:00.000Z", _usage(80, 8))
    _write_session_file(home, f"2026-08-23T09-00-00_{S2}.jsonl", S2,
                        "2026-08-23T09:00:00.000Z",
                        [_user_line("2026-08-23T08:59:59.000Z"), r1, r2])
    _write_session_file(home, f"2026-08-23T09-02-30_{S2}.jsonl", S2,
                        "2026-08-23T09:02:30.000Z",
                        [r1, r3])
    # S3 (day 1): an all-zero row (dropped), a totalTokens-only row, a corrupt
    # line, and a row for a model absent from the pricing DB.
    _write_session_file(
        home,
        f"2026-08-22T08-00-00_{S3}.jsonl",
        S3,
        "2026-08-22T08:00:00.000Z",
        [
            _user_line("2026-08-22T07:59:59.000Z"),
            _assistant_line("a3000000", "2026-08-22T08:00:30.000Z", _usage(0, 0)),
            "{this line is corrupt json and both sides must skip it",
            _assistant_line("a3000001", "2026-08-22T08:01:00.000Z", _usage(0, 0, total=123)),
            _assistant_line("a3000002", "2026-08-22T08:02:00.000Z",
                            _usage(10, 1, cost_total=0.5),
                            model="no-such-model", provider="ghost"),
        ],
    )


def test_omp_registered():
    assert "omp" in SESSION_TOOLS
    assert sessions.TOOL_LABELS["omp"] == "omp"


def test_missing_dir_empty(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    assert _omp_sessions() == {}
    listing = get_sessions_data("omp", "all")
    assert listing["sessions"] == []
    with pytest.raises(ValueError):
        get_sessions_data("nope", "all")


def test_mapping(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    bucket = home / ".omp" / "agent" / "sessions" / BUCKET
    bucket.mkdir(parents=True)
    # The imported row shape keeps the fixture aligned with the token parser.
    (bucket / "2026-08-22T10-44-58_x.jsonl").write_text(
        _omp_session_lines(model="deepseek-chat", provider="deepseek"), encoding="utf-8"
    )
    raw = _omp_sessions()
    sid = "01a02912-e3ca-7000-b186-e024973e94b2"
    assert set(raw) == {sid}
    s = raw[sid]
    assert s["tool"] == "omp"
    assert s["project"] == "project"  # basename of the header cwd
    assert s["display_name"] == "hi"  # empty title -> user preview

    turn = s["turns"][0]
    assert turn["model"] == "deepseek-chat"  # bare model from the row
    assert turn["tokens_in"] == 3342 + 0  # input + cacheWrite
    assert turn["tokens_cache"] == 0
    assert turn["tokens_out"] == 46
    assert turn["tokens_reasoning"] == 0
    assert turn["tokens"] == 3388
    # O6: the recorded cost.total (0.0007328) is ignored; the pricing DB
    # decides, at the split-cache-write shape.
    bill = turn["_bill"]
    assert bill["rule"] == "split-cache-write"
    assert bill["model"] == "deepseek/deepseek-chat"
    assert "fixed" not in bill
    assert turn["cost"] == pytest.approx(
        PricingDatabase().get_cost("deepseek-chat", 3342, 46, 0, 0)
    )


def test_recorded_cost_ignored(monkeypatch, tmp_path):
    """O6: the recorded cost.total never wins. For a priced model the
    pricing DB decides (S1's first row records 0.0007328); for a model
    absent from the DB the row costs 0.00, not the recorded 0.5."""
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    _write_parity_tree(home)
    raw = _omp_sessions()
    pricing = PricingDatabase()
    s1_first = raw[S1]["turns"][0]
    assert s1_first["cost"] == pytest.approx(
        pricing.get_cost("deepseek-chat", 3342, 46, 0, 0)
    )
    assert s1_first["cost"] != 0.0007328
    assert "fixed" not in s1_first["_bill"]
    unpriced = [t for t in raw[S3]["turns"] if t["model"] == "no-such-model"]
    assert len(unpriced) == 1
    assert unpriced[0]["cost"] == 0.0


def test_model_change_fallback_splits_qualified_model(monkeypatch, tmp_path):
    """O3: an assistant row without provider/model falls back to the last
    model_change, split into provider/model when qualified."""
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    # _omp_session_lines(assistant_extras={}) drops the row-level model and
    # provider; the qualified model_change is the only model source.
    bucket = home / ".omp" / "agent" / "sessions" / BUCKET
    bucket.mkdir(parents=True)
    (bucket / "2026-08-22T10-44-58_y.jsonl").write_text(
        _omp_session_lines(assistant_extras={}), encoding="utf-8"
    )
    raw = _omp_sessions()
    sid = "01a02912-e3ca-7000-b186-e024973e94b2"
    turn = raw[sid]["turns"][0]
    assert turn["model"] == "selfhosted-qwen"
    assert turn["_bill"]["model"] == "vllm-hpc/selfhosted-qwen"
    assert turn["cost"] == 0.0  # absent from the pricing DB


def test_title_row_and_user_fallback(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    bucket = home / ".omp" / "agent" / "sessions" / BUCKET
    bucket.mkdir(parents=True)
    text = _omp_session_lines()
    (bucket / "2026-08-22T10-44-58_z.jsonl").write_text(text, encoding="utf-8")
    raw = _omp_sessions()
    sid = "01a02912-e3ca-7000-b186-e024973e94b2"
    assert raw[sid]["display_name"] == "hi"  # title "" -> user preview

    # A non-empty title row wins.
    sessions._load_omp_sessions.cache_clear()
    sessions._parse_omp_session_file.cache_clear()
    (bucket / "2026-08-22T10-44-58_z.jsonl").write_text(
        text.replace('"title": ""', '"title": "My Task"', 1), encoding="utf-8"
    )
    raw = _omp_sessions()
    assert raw[sid]["display_name"] == "My Task"


def test_date_window_clips(monkeypatch, tmp_path):
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    _write_parity_tree(home)
    listing = get_sessions_data("omp", "range", date_from="2026-08-22",
                                date_to="2026-08-22")
    ids = {s["session_id"] for s in listing["sessions"]}
    assert ids == {S1, S3}  # S2 lives on day 2
    totals = _listing_totals(listing)
    assert totals["count"] == 4  # 2 S1 rows + 2 surviving S3 rows
    # Parser over the same UTC-day bounds.
    since, until = parse_date_range("2026-08-22", "2026-08-22")
    parser = _parser_totals(home, since, until)
    assert parser["count"] == 4
    assert totals["input"] == parser["input"] + parser["cacheWrite"]
    assert totals["cache"] == parser["cacheRead"]
    assert totals["output"] == parser["output"]
    assert totals["cost"] == pytest.approx(parser["cost"], abs=1e-12)


def test_dir_ownership_pi_claims_omp_tree(monkeypatch, tmp_path):
    """A dir claimed by pi_agent (PI_CODING_AGENT_DIR pointed at the omp
    tree) is omp's default dir; omp must drop it and pi must keep it, so the
    Sessions tab never double-counts where Overview counts once."""
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    _write_parity_tree(home)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".omp" / "agent"))

    assert _omp_session_roots() == []
    assert _omp_sessions() == {}
    pi = _pi_sessions()
    assert set(pi) == {S1, S2, S3}
    assert pi[S2]["tool"] == "pi_agent"
    assert pi[S1]["turns"]


def test_parity_with_usage_parser(monkeypatch, tmp_path):
    """The load-bearing property: OmpParser row sums equal the harness
    summary, bucket for bucket, in total (i), per session via an isolated
    resume tree (ii), and over a day window (iii)."""
    home = _fresh_home(tmp_path)
    _home_env(monkeypatch, home)
    _write_parity_tree(home)
    pricing = PricingDatabase()

    def expected_cost(*rows):
        return sum(
            pricing.get_cost(model, inp, out, cr, cw) for inp, out, cr, cw, model in rows
        )

    # (i) Full tree: the parser bills 7 rows (the all-zero and corrupt rows
    # die on both sides; the resume re-log counts once).
    parser = _parser_totals(home)
    assert parser["count"] == 7
    assert (parser["input"], parser["output"]) == (3788, 226)
    assert (parser["cacheRead"], parser["cacheWrite"]) == (3146, 25)
    assert parser["cost"] == pytest.approx(
        expected_cost(
            (3342, 46, 0, 0, "deepseek-chat"),
            (206, 33, 3136, 0, "deepseek-chat"),
            (100, 10, 0, 25, "deepseek-chat"),
            (50, 5, 10, 0, "deepseek-chat"),
            (80, 8, 0, 0, "deepseek-chat"),
            (0, 123, 0, 0, "deepseek-chat"),
            (10, 1, 0, 0, "no-such-model"),
        )
    )
    listing = get_sessions_data("omp", "all")
    assert {s["session_id"] for s in listing["sessions"]} == {S1, S2, S3}
    totals = _listing_totals(listing)
    assert totals["count"] == 7
    assert totals["input"] == parser["input"] + parser["cacheWrite"]
    assert totals["cache"] == parser["cacheRead"]
    assert totals["output"] == parser["output"]
    assert totals["cost"] == pytest.approx(parser["cost"], abs=1e-12)

    # (ii) Per session: the isolated resume pair — the re-logged row counts
    # once, so the session summary equals the parser's totals for the files.
    home2 = _fresh_home(tmp_path / "iso")
    _home_env(monkeypatch, home2)
    r1 = _assistant_line("aaaa1111", "2026-08-23T09:00:00.000Z", _usage(100, 10, cw=25))
    r2 = _assistant_line("bbbb2222", "2026-08-23T09:01:00.000Z", _usage(50, 5, cr=10))
    r3 = _assistant_line("cccc3333", "2026-08-23T09:02:00.000Z", _usage(80, 8))
    _write_session_file(home2, f"2026-08-23T09-00-00_{S2}.jsonl", S2,
                        "2026-08-23T09:00:00.000Z", [r1, r2])
    _write_session_file(home2, f"2026-08-23T09-02-30_{S2}.jsonl", S2,
                        "2026-08-23T09:02:30.000Z", [r1, r3])
    parser2 = _parser_totals(home2)
    assert parser2["count"] == 3
    listing2 = get_sessions_data("omp", "all")
    assert [s["session_id"] for s in listing2["sessions"]] == [S2]
    s2 = listing2["sessions"][0]
    assert s2["token_events"] == 3
    assert (s2["tokens_in"], s2["tokens_cache"], s2["tokens_out"]) == (255, 10, 23)
    assert s2["cost"] == pytest.approx(
        expected_cost(
            (100, 10, 0, 25, "deepseek-chat"),
            (50, 5, 10, 0, "deepseek-chat"),
            (80, 8, 0, 0, "deepseek-chat"),
        )
    )

    # (iii) A day window over the full tree (S1 + S3 on day 1, S2 on day 2).
    _home_env(monkeypatch, home)
    since, until = parse_date_range("2026-08-22", "2026-08-22")
    parser3 = _parser_totals(home, since, until)
    listing3 = get_sessions_data("omp", "range", date_from="2026-08-22",
                                 date_to="2026-08-22")
    totals3 = _listing_totals(listing3)
    assert parser3["count"] == 4
    assert totals3["count"] == 4
    assert totals3["input"] == parser3["input"] + parser3["cacheWrite"]
    assert totals3["cache"] == parser3["cacheRead"]
    assert totals3["output"] == parser3["output"]
    assert totals3["cost"] == pytest.approx(parser3["cost"], abs=1e-12)


def test_frontend_session_registry_includes_omp():
    index = Path(sessions.__file__).parent / "static" / "index.html"
    source = index.read_text(encoding="utf-8")
    assert "'zcode', 'kilocode', 'omp'" in source
    assert "kilocode: null, omp: null, grok: null, hermes: null, antigravity_cli: null, cline: null, workbuddy: null, qoder: null, combined: null" in source
    assert 'updateSessionPanel("omp", lastSessionsResponses.omp);' in source
    assert 'initSortHeaders("omp", renderSessionsTab);' in source
    assert "omp: { ...DEFAULT_SORT }," in source
    assert "omp: 'omp'," in source
    assert "ompSessions: 'OMP Sessions'," in source
    assert "ompSessions: 'OMP 会话'," in source
    assert 'id="ompSessionsTable"' in source
    assert 'data-panel-details="omp"' in source
    assert 'id="ompPanelCount"' in source
    assert (index.parent / "icons" / "agents" / "omp.png").is_file()
