"""Stored session rows are price-neutral: rates change without rereading logs.

Costs used to be baked into the cached rows, with the pricing content folded
into their source signature — so editing one rate marked every unchanged Codex,
Claude and Kimi log as changed and reparsed the whole corpus. Rows now carry the
billing inputs and are priced by whoever reads them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokdash import sessions
from tokdash.pricing import PricingDatabase
from tokdash.sessions import get_sessions_data, reload_pricing_db
from tokdash.usage_store import UsageEntryStore

TS = "2026-05-19T12:00:00Z"
MODEL = "claude-opus-5"
KIMI_MODEL = "kimi-k3"
DSH_MODEL = "deepseek-v4-flash"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DSH_HOME", str(tmp_path / ".dsh"))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    yield tmp_path
    reload_pricing_db()


@pytest.fixture
def counting_parsers(monkeypatch):
    """Count how many source files each parser reads."""
    counts = {"codex": 0, "claude": 0, "kimi": 0, "dsh": 0}
    for tool, name in (
        ("codex", "_parse_codex_session_file"),
        ("claude", "_parse_claude_session_file"),
        ("kimi", "_parse_kimi_session_file"),
        ("dsh", "_parse_dsh_session_file"),
    ):
        original = getattr(sessions, name)

        def counting(*args, _original=original, _tool=tool, **kwargs):
            counts[_tool] += 1
            return _original(*args, **kwargs)

        # reload_pricing_db() clears these caches, so keep the lru_cache API.
        counting.cache_clear = original.cache_clear
        counting.cache_info = original.cache_info
        monkeypatch.setattr(sessions, name, counting)
    return counts


def _pricing(rates: dict[str, dict]) -> dict:
    return {"version": "test", "aliases": {}, "models": rates}


def _write_pricing(models: dict[str, dict]) -> None:
    """Install a pricing override for this process and reload the singleton."""
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_pricing(models)), encoding="utf-8")
    reload_pricing_db()


def _rates(input_rate: float = 3.0, output_rate: float = 15.0, cache_read: float = 0.3) -> dict:
    priced = {
        "provider": "anthropic",
        "input": input_rate,
        "output": output_rate,
        "cache_read": cache_read,
        "cache_write": input_rate,
        "unit": "per_million_tokens",
    }
    return {
        MODEL: priced,
        KIMI_MODEL: dict(priced, provider="moonshotai"),
        DSH_MODEL: dict(priced, provider="deepseek"),
    }


def _claude_row(session_id: str, message_id: str, timestamp: str = TS, **usage) -> dict:
    return {
        "sessionId": session_id,
        "cwd": "/work/proj",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": MODEL,
            "usage": {
                "input_tokens": usage.get("input", 1_000),
                "output_tokens": usage.get("output", 100),
                "cache_read_input_tokens": usage.get("cache_read", 2_000),
                "cache_creation_input_tokens": usage.get("cache_write", 500),
            },
        },
    }


def _write_claude(home: Path, stem: str, rows: list[dict]) -> Path:
    path = home / ".claude" / "projects" / "proj" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _codex_rows(session_id: str, model: str = MODEL, provider: str = "anthropic") -> list[dict]:
    return [
        {
            "type": "session_meta",
            # The provider is a session fact; the model can change per turn.
            "payload": {"id": session_id, "cwd": "/work/proj", "timestamp": TS, "model_provider": provider},
        },
        {"type": "turn_context", "payload": {"model": model}},
        {
            "type": "event_msg",
            "timestamp": TS,
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 3_000,
                        "cached_input_tokens": 2_000,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 50,
                    }
                },
            },
        },
    ]


def _write_codex(home: Path, stem: str, rows: list[dict]) -> Path:
    path = home / ".codex" / "sessions" / "2026" / "05" / "19" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _write_kimi(home: Path, session_id: str, message_id: str) -> Path:
    # Legacy layout: sessions/<userId>/<sessionId>/wire.jsonl
    path = home / ".kimi" / "sessions" / "user" / session_id / "wire.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "usage.record",
        "time": 1_779_278_400_000,
        "model": "k3",
        "usage": {
            "inputOther": 1_000,
            "output": 100,
            "inputCacheRead": 2_000,
            "inputCacheCreation": 500,
        },
        "id": message_id,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def _write_dsh(home: Path, session_id: str) -> Path:
    """A minimal dsh log: v0 header plus one usage-bearing assistant/message."""
    path = home / ".dsh" / "sessions" / "--work-proj--" / session_id / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": 1_779_278_400_000,
            "cwd": "/work/proj",
        },
        {
            "type": "assistant/message",
            "seq": 1,
            "time": 1_779_278_400_000,
            "data": {
                "turn": 0,
                "step": 0,
                "message": {
                    "id": "a1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "source": {"kind": "model", "provider": "deepseek", "model": DSH_MODEL},
                },
                "usage": {
                    "inputTokens": 1_000,
                    "outputTokens": 100,
                    "cacheReadTokens": 2_000,
                    "cacheWriteTokens": 500,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _cost(tool: str) -> float:
    return get_sessions_data(tool, "all")["summary"]["cost"]


# --- 1: a pricing edit must not reread unchanged logs -----------------------


def test_adding_a_model_does_not_reread_any_source_log(_isolated_home, counting_parsers):
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1")])
    _write_codex(home, "c", _codex_rows("codex-1"))
    _write_kimi(home, "k1", "kimi-msg-1")
    _write_pricing(_rates())

    for tool in ("claude", "codex", "kimi"):
        get_sessions_data(tool, "all")
    assert counting_parsers == {"claude": 1, "codex": 1, "kimi": 1, "dsh": 0}, "first read parses each file once"

    for tool in counting_parsers:
        counting_parsers[tool] = 0
    _write_pricing({**_rates(), "some-unrelated-model": {"provider": "x", "input": 9.0, "output": 9.0}})
    for tool in ("claude", "codex", "kimi"):
        get_sessions_data(tool, "all")

    assert counting_parsers == {"claude": 0, "codex": 0, "kimi": 0, "dsh": 0}


# --- 2: rate changes still reach the reported cost --------------------------


def test_a_rate_change_reprices_without_rereading(_isolated_home, counting_parsers):
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1")])
    _write_pricing(_rates(input_rate=3.0))

    before = get_sessions_data("claude", "all")
    assert counting_parsers["claude"] == 1
    counting_parsers["claude"] = 0

    _write_pricing(_rates(input_rate=6.0))
    after = get_sessions_data("claude", "all")

    assert counting_parsers["claude"] == 0, "a rate change must not reread the log"
    # input 1000 + cache_write 500 bill at the input rate; doubling it adds
    # 1500 tokens * 3.0 / 1e6 to both the session and the summary.
    expected_delta = 1_500 * 3.0 / 1_000_000
    assert after["summary"]["cost"] == pytest.approx(before["summary"]["cost"] + expected_delta)
    assert after["sessions"][0]["cost"] == pytest.approx(before["sessions"][0]["cost"] + expected_delta)
    assert after["sessions"][0]["tokens"] == before["sessions"][0]["tokens"]


def test_a_model_disappearing_from_pricing_costs_nothing(_isolated_home, counting_parsers):
    """Pricing is whatever the reader's database says, including "unknown"."""
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1")])
    _write_pricing(_rates())
    assert _cost("claude") > 0

    counting_parsers["claude"] = 0
    _write_pricing({"other-model": {"provider": "x", "input": 1.0, "output": 1.0}})

    assert _cost("claude") == 0.0
    assert counting_parsers["claude"] == 0


# --- 3: alias / model-resolution changes ------------------------------------


def test_an_alias_change_reprices_cached_rows(_isolated_home, counting_parsers):
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1")])
    # The turn's model resolves only through an alias.
    priced = {"house-model": {"provider": "x", "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.0}}
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "t", "aliases": {MODEL: "house-model"}, "models": priced}), encoding="utf-8")
    reload_pricing_db()

    aliased = _cost("claude")
    assert aliased > 0
    counting_parsers["claude"] = 0

    # Drop the alias: the same rows now resolve to nothing.
    path.write_text(json.dumps({"version": "t", "aliases": {}, "models": priced}), encoding="utf-8")
    reload_pricing_db()

    assert _cost("claude") == 0.0
    assert counting_parsers["claude"] == 0


# --- 4: two processes, two pricing files, one database ----------------------


def test_two_pricing_files_share_one_database_without_rewriting_rows(_isolated_home, counting_parsers):
    """The dev server and the installed service must not fight over the rows."""
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1")])
    cheap, dear = _rates(input_rate=3.0), _rates(input_rate=30.0)

    _write_pricing(cheap)
    first = _cost("claude")
    assert counting_parsers["claude"] == 1

    store = UsageEntryStore()
    signatures = _stored_signatures(store)
    counting_parsers["claude"] = 0

    seen = []
    for rates in (dear, cheap, dear, cheap):
        _write_pricing(rates)
        seen.append(_cost("claude"))

    assert counting_parsers["claude"] == 0, "alternating pricing must not reparse"
    assert _stored_signatures(store) == signatures, "alternating pricing must not rewrite rows"
    # Each reader gets its own rates, and they do not drift as they alternate.
    assert seen[0] == seen[2] and seen[1] == seen[3]
    assert seen[1] == pytest.approx(first)
    assert seen[0] > seen[1]


def test_two_pricing_databases_price_the_same_rows_differently(_isolated_home, tmp_path):
    """Two processes reading one row, each with its own rates."""
    _write_pricing(_rates(input_rate=3.0))
    cheap_path = tmp_path / "cheap.json"
    dear_path = tmp_path / "dear.json"
    cheap_path.write_text(json.dumps(_pricing(_rates(input_rate=3.0))), encoding="utf-8")
    dear_path.write_text(json.dumps(_pricing(_rates(input_rate=30.0))), encoding="utf-8")
    cheap = PricingDatabase(override_path=cheap_path)
    dear = PricingDatabase(override_path=dear_path)

    turn = sessions._build_turn(
        turn_index=1,
        timestamp_ms=1_779_278_400_000,
        model=MODEL,
        tokens_in=1_500,
        tokens_cache=2_000,
        tokens_out=100,
        tokens_reasoning=0,
        bill=sessions._billing_record(
            MODEL, "input-plus-cache-write", input_tokens=1_000, output_tokens=100,
            cache_read=2_000, cache_write=500,
        ),
    )

    cheap_cost = sessions._repriced_turns([turn], cheap)[0]["cost"]
    dear_cost = sessions._repriced_turns([turn], dear)[0]["cost"]

    assert dear_cost > cheap_cost
    assert dear_cost - cheap_cost == pytest.approx(1_500 * 27.0 / 1_000_000)
    # Neither reading changed the row they share.
    assert turn["_bill"]["input"] == 1_000 and turn["_bill"]["cache_write"] == 500


def _stored_signatures(store: UsageEntryStore) -> dict[str, tuple[str, str]]:
    import sqlite3

    with sqlite3.connect(str(store.path)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            str(row["file_path"]): (str(row["signature"]), str(row["raw_json"]))
            for row in conn.execute("SELECT file_path, signature, raw_json FROM session_records")
        }


# --- 5 & 6: real changes still invalidate -----------------------------------


def test_editing_a_source_log_still_reparses_it(_isolated_home, counting_parsers):
    home = _isolated_home
    path = _write_claude(home, "a", [_claude_row("s1", "m1")])
    _write_pricing(_rates())
    first = get_sessions_data("claude", "all")
    counting_parsers["claude"] = 0

    _write_claude(home, "a", [_claude_row("s1", "m1"), _claude_row("s1", "m2", timestamp="2026-05-19T12:01:00Z")])
    assert path.exists()
    second = get_sessions_data("claude", "all")

    assert counting_parsers["claude"] == 1
    assert second["sessions"][0]["token_events"] == first["sessions"][0]["token_events"] + 1


def test_a_parser_version_bump_still_reparses(_isolated_home, counting_parsers, monkeypatch):
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1")])
    _write_pricing(_rates())
    get_sessions_data("claude", "all")
    counting_parsers["claude"] = 0

    bumped = dict(sessions._SESSION_FILE_PARSER_VERSIONS)
    bumped["_parse_claude_session_file"] += 1
    monkeypatch.setattr(sessions, "_SESSION_FILE_PARSER_VERSIONS", bumped)
    get_sessions_data("claude", "all")

    assert counting_parsers["claude"] == 1


def test_a_dsh_decoder_version_bump_still_reparses(_isolated_home, counting_parsers, monkeypatch):
    """The dsh signature folds in the shared decoder's extraction and
    accounting versions; bumping either must reparse, exactly like a parser
    version bump. ``_session_signature_compatible`` ignores only ``pricing``
    and ``cost_basis``, so the ``decoder`` keys are compared."""
    from tokdash.sources import dsh_log

    home = _isolated_home
    _write_dsh(home, "dsh-1")
    _write_pricing(_rates())
    get_sessions_data("dsh", "all")
    assert counting_parsers["dsh"] == 1, "cold store parses once"

    counting_parsers["dsh"] = 0
    get_sessions_data("dsh", "all")
    assert counting_parsers["dsh"] == 0, "unchanged signature hits the store"

    for attr in ("DSH_DECODER_VERSION", "DSH_ACCOUNTING_VERSION"):
        monkeypatch.setattr(dsh_log, attr, getattr(dsh_log, attr) + 1)
        get_sessions_data("dsh", "all")
        assert counting_parsers["dsh"] == 1, f"{attr} bump must reparse"
        counting_parsers["dsh"] = 0
        get_sessions_data("dsh", "all")
        assert counting_parsers["dsh"] == 0, f"re-reading at the bumped {attr} hits the store"


# --- 8: the frozen v1.5.9 constants -----------------------------------------


def test_the_v159_constants_are_pinned_to_their_historical_values():
    """Literal values, so repointing them at today's pricing file fails here.

    They identify what v1.5.9 shipped. Moving them would let rows priced at those
    rates be resigned as if they matched some later pricing content.
    """
    assert sessions._V159_BASELINE_PRICING_CONTENT_SIGNATURE == (
        "pricing-content-v1",
        "baseline",
        63321,
        "be7be7ec40f29e7e264f3ab572f24446",
    )
    assert sessions._V159_BASELINE_PRICING_RAW_SIZE == 84983


# --- 9: durable rows whose source log is gone -------------------------------


def test_a_durable_row_reprices_after_its_log_disappears(_isolated_home, counting_parsers):
    """Missing rows are kept and priced from their stored billing inputs."""
    home = _isolated_home
    path = _write_claude(home, "a", [_claude_row("s1", "m1")])
    _write_pricing(_rates(input_rate=3.0))
    before = _cost("claude")
    assert before > 0

    path.unlink()
    counting_parsers["claude"] = 0
    _write_pricing(_rates(input_rate=6.0))
    after = get_sessions_data("claude", "all")

    assert counting_parsers["claude"] == 0
    assert after["summary"]["session_count"] == 1, "durable history survives the deleted log"
    assert after["summary"]["cost"] == pytest.approx(before + 1_500 * 3.0 / 1_000_000)


def test_a_row_written_before_billing_inputs_existed_still_reprices():
    """Rows from older builds carry totals only; they reprice from those.

    Codex, Claude and Kimi all billed a turn as
    get_cost(model, tokens_in, tokens_out, tokens_cache, 0), so the stored totals
    reproduce it. What they cannot do is separate a Claude or Kimi cache write
    from fresh input again.
    """
    legacy_turn = {
        "turn_index": 1,
        "timestamp_ms": 1_779_278_400_000,
        "model": MODEL,
        "tokens_in": 1_500,
        "tokens_cache": 2_000,
        "tokens_out": 100,
        "tokens_reasoning": 0,
        "tokens": 3_600,
        "cost": 999.0,  # whatever the rates were when it was written
    }
    _write_pricing(_rates(input_rate=3.0, output_rate=15.0, cache_read=0.3))

    priced = sessions._repriced_turns([legacy_turn])[0]

    expected = (1_500 * 3.0 + 100 * 15.0 + 2_000 * 0.3) / 1_000_000
    assert priced["cost"] == pytest.approx(expected)
    assert legacy_turn["cost"] == 999.0, "the caller's row is not mutated"
    assert priced["_bill"]["model"] == MODEL


def _seed_legacy_codex_rows(cost_basis: str | None = None) -> UsageEntryStore:
    """Store Codex rows the way a build predating billing inputs wrote them.

    cost_basis signs them as an intermediate build did: price-neutral in the
    signature, but with rows that never carried the inputs that shape promises.
    """
    import sqlite3

    from tokdash import clientpaths

    store = UsageEntryStore()
    parser = sessions._codex_session_parser_signature()
    if cost_basis is None:
        parser.pop("cost_basis")
        parser["pricing"] = sessions._session_pricing_content_signature()
    else:
        parser["cost_basis"] = cost_basis
    store.sync_session_files(
        "codex",
        sessions._iter_file_signatures(clientpaths.codex_sessions_dir()),
        parser=parser,
        parse_file_session=lambda file_sig: sessions._parse_codex_session_file(*file_sig),
    )
    with sqlite3.connect(str(store.path)) as conn:
        rows = conn.execute(
            "SELECT file_path, raw_json FROM session_records WHERE tool = 'codex'"
        ).fetchall()
        for file_path, raw_json in rows:
            raw = json.loads(raw_json)
            for turn in raw.get("turns", []):
                turn.pop("_bill", None)
            conn.execute(
                "UPDATE session_records SET raw_json = ? WHERE tool = 'codex' AND file_path = ?",
                (json.dumps(raw), file_path),
            )
    return store


def test_a_legacy_codex_row_reprices_under_its_provider(_isolated_home, counting_parsers):
    """Codex bills provider/model but stores the bare name, so it reparses once.

    The pricing file keys aliases by provider (kimi-code/k3 and friends), so a
    row whose totals are all that survive cannot prove which entry priced it.
    Reconstructing the bill from the bare name would quietly move it onto the
    other rate, which is exactly what must not happen.
    """
    home = _isolated_home
    _write_codex(home, "c", _codex_rows("codex-1", model="k3", provider="kimi-code"))
    dear = {"provider": "moonshotai", "input": 10.0, "output": 50.0, "cache_read": 1.0, "unit": "per_million_tokens"}
    cheap = dict(dear, input=1.0, output=5.0, cache_read=0.1)
    _write_pricing({"kimi-code/k3": dear, "k3": cheap})
    _seed_legacy_codex_rows()
    counting_parsers["codex"] = 0

    cost = _cost("codex")

    qualified = (1_000 * 10.0 + 100 * 50.0 + 2_000 * 1.0) / 1_000_000
    assert counting_parsers["codex"] == 1, "the row must be rebuilt, not resigned"
    assert cost == pytest.approx(qualified)
    assert cost != pytest.approx(qualified / 10), "the bare k3 rate is the wrong entry"

    # Rebuilt once: the row now carries the qualified model itself.
    stored = sessions._stored_sessions_for_tool("codex")["codex-1"]
    assert stored["turns"][0]["_bill"]["model"] == "kimi-code/k3"
    counting_parsers["codex"] = 0
    sessions._stored_sessions_for_tool("codex")
    assert counting_parsers["codex"] == 0, "and does not reparse again"


def test_a_codex_row_resigned_without_billing_inputs_is_rebuilt(_isolated_home, counting_parsers):
    """The stranded shape: price-neutral signature, pre-billing-inputs content.

    A build that let Codex take the free migration left rows claiming the
    price-neutral basis while carrying only totals. They cannot be told apart by
    content, so the Codex basis names what the row must carry and they rebuild.
    """
    home = _isolated_home
    _write_codex(home, "c", _codex_rows("codex-1", model="k3", provider="kimi-code"))
    dear = {"provider": "moonshotai", "input": 10.0, "output": 50.0, "cache_read": 1.0, "unit": "per_million_tokens"}
    _write_pricing({"kimi-code/k3": dear, "k3": dict(dear, input=1.0, output=5.0, cache_read=0.1)})
    _seed_legacy_codex_rows(cost_basis=sessions._SESSION_COST_BASIS)
    counting_parsers["codex"] = 0

    cost = _cost("codex")

    assert counting_parsers["codex"] == 1
    assert cost == pytest.approx((1_000 * 10.0 + 100 * 50.0 + 2_000 * 1.0) / 1_000_000)
    # Claude and Kimi rows written by that same build keep their free migration.
    assert sessions._claude_session_parser_signature()["cost_basis"] == sessions._SESSION_COST_BASIS
    assert sessions._kimi_session_parser_signature()["cost_basis"] == sessions._SESSION_COST_BASIS


def test_only_codex_withholds_the_free_migration():
    """Claude and Kimi bill under the name they store, so their rows resign."""

    def signature(parser: dict) -> str:
        return json.dumps({"files": [["s.jsonl", 1, 2]], "mode": "session-file", "parser": parser})

    priced = sessions._codex_session_parser_signature()
    priced.pop("cost_basis")
    priced["pricing"] = sessions._session_pricing_content_signature()
    old = signature(priced)
    new = signature(sessions._codex_session_parser_signature())

    assert sessions._session_signature_compatible(old, new)
    assert not sessions._codex_session_signature_compatible(old, new)
    # Only that one move is withheld; an unchanged row still resigns as a no-op.
    assert sessions._codex_session_signature_compatible(new, new)


# --- 10 & 11: cached and live agree, streams intact -------------------------


@pytest.mark.parametrize("tool", ["claude", "codex", "kimi", "dsh"])
def test_cached_and_live_payloads_are_identical(_isolated_home, monkeypatch, tool):
    home = _isolated_home
    _write_claude(home, "a", [_claude_row("s1", "m1"), _claude_row("s1", "m2", timestamp="2026-05-19T12:02:00Z")])
    _write_codex(home, "c", _codex_rows("codex-1"))
    _write_kimi(home, "k1", "kimi-msg-1")
    _write_dsh(home, "dsh-1")
    _write_pricing(_rates())

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    reload_pricing_db()
    live = get_sessions_data(tool, "all")

    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    reload_pricing_db()
    cached = get_sessions_data(tool, "all")

    live.pop("timestamp")
    cached.pop("timestamp")
    assert cached == live
    assert live["summary"]["cost"] > 0


def test_repriced_rows_keep_their_agent_streams(_isolated_home):
    """Repricing must not flatten the private stream ids active time needs."""
    home = _isolated_home
    _write_claude(home, "main", [_claude_row("s1", "m1"), _claude_row("s1", "m2", timestamp="2026-05-19T12:01:00Z")])
    subagent = home / ".claude" / "projects" / "proj" / "s1" / "subagents" / "agent-a1.jsonl"
    subagent.parent.mkdir(parents=True, exist_ok=True)
    subagent.write_text(
        "".join(
            json.dumps({**_claude_row("s1", mid, timestamp=ts), "agentId": "a1", "isSidechain": True}) + "\n"
            for mid, ts in (("s1-a", TS), ("s1-b", "2026-05-19T12:01:00Z"))
        ),
        encoding="utf-8",
    )
    _write_pricing(_rates())

    cached = get_sessions_data("claude", "all")["sessions"][0]

    assert cached["active_ms"] == 60_000
    assert cached["active_ms_sum"] == 120_000
    raw = sessions._stored_sessions_for_tool("claude")["s1"]
    assert {turn["_stream_id"] for turn in raw["turns"]} == {"main", "a1"}
    assert all("_bill" in turn for turn in raw["turns"])
    detail = sessions.get_session_detail("claude", "s1")
    assert all("_bill" not in turn for turn in detail["turns"])
