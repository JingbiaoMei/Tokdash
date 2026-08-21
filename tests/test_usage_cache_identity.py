"""The persistent usage cache keeps three identities apart.

Source identity (paths, mtimes, sizes), parse identity (each parser's explicit
version) and pricing identity (the effective rates plus the code that reads
them) used to be one signature built partly from a hash of ``coding_tools.py``
and the whole pricing file. So adding a parser reparsed every source, and adding
one pricing entry reparsed every log.

Now a parser names its own version, rows carry the billing inputs they were
priced from, and a pricing change is applied by repricing those rows in one
transaction. These tests hold that apart: what must reparse, what must reprice,
and what must do neither.

See docs/development/technical-notes/USAGE_CACHE_IDENTITY.md.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tokdash
import tokdash.compute as compute
import tokdash.usage_store as usage_store_module
from tokdash.pricing import PricingDatabase
from tokdash.sources import coding_tools
from tokdash.sources.coding_tools import (
    BaseParser,
    ClaudeParser,
    CodexParser,
    CodingToolsUsageTracker,
    PiAgentParser,
    _sig_cache,
)
from tokdash.usage_store import (
    UsageEntryStore,
    build_source_signature,
    usage_billing_fixed,
    usage_billing_pricing,
    usage_entry_cost,
)

TS = "2026-05-19T12:00:00Z"
TS_MS = 1_779_278_400_000
CODEX_MODEL = "tokdash-codex-test"
CLAUDE_MODEL = "tokdash-claude-test"
PI_MODEL = "tokdash-pi-test"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    monkeypatch.setenv("TOKDASH_USAGE_DB_DURABLE", "1")
    # Every client root that has its own env override, pinned inside the fake
    # home so a developer's real logs can never reach these assertions.
    for var, relative in (
        ("DSH_HOME", ".dsh"),
        ("GROK_HOME", ".grok"),
        ("REASONIX_HOME", ".reasonix"),
        ("OPENCLAW_HOME", ".openclaw"),
        ("ZCODE_HOME", ".zcode"),
        ("KIMI_SHARE_DIR", ".kimi"),
        ("KIMI_CODE_HOME", ".kimi-code"),
        ("PI_CODING_AGENT_DIR", ".pi/agent"),
    ):
        monkeypatch.setenv(var, str(tmp_path / relative))
    yield tmp_path


@pytest.fixture
def parse_counts(monkeypatch):
    """How many times each parser actually read its source logs."""
    counts = {"codex": 0, "claude": 0, "pi_agent": 0}
    for name, cls in (
        ("codex", CodexParser),
        ("claude", ClaudeParser),
        ("pi_agent", PiAgentParser),
    ):
        original = cls._parse_all

        def counting(self, _original=original, _name=name):
            counts[_name] += 1
            return _original(self)

        monkeypatch.setattr(cls, "_parse_all", counting)
    return counts


def _reset(counts=None) -> None:
    """Drop the in-process caches that sit in front of the persistent store."""
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    if counts is not None:
        for key in counts:
            counts[key] = 0


def _sync() -> tuple[UsageEntryStore, list[str]]:
    _sig_cache.clear()
    BaseParser._entry_cache.clear()
    return compute._sync_usage_store(CodingToolsUsageTracker())


def _cost(store: UsageEntryStore, source: str) -> float:
    return store.aggregate_entries(sources=[source])["total_cost"]


def _row_costs(store: UsageEntryStore, source: str) -> list[float]:
    return [float(row["cost"]) for row in store.query_entries(sources=[source])]


# --- source fixtures --------------------------------------------------------


def _write_pricing(models: dict, aliases: dict | None = None) -> None:
    path = PricingDatabase().override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": "test", "aliases": aliases or {}, "models": models}),
        encoding="utf-8",
    )


def _rates(**overrides) -> dict:
    priced = {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.0}
    models = {CODEX_MODEL: dict(priced), CLAUDE_MODEL: dict(priced)}
    for key, value in overrides.items():
        models[key] = value
    return models


def _write_codex(home: Path, stem: str, turns: int = 1) -> Path:
    rows = [
        {"type": "session_meta", "payload": {"id": stem, "cwd": "/w", "timestamp": TS}},
        {"type": "turn_context", "payload": {"model": CODEX_MODEL}},
    ]
    for index in range(turns):
        rows.append(
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
                    "id": f"{stem}-{index}",
                },
            }
        )
    path = home / ".codex" / "sessions" / "2026" / "05" / "19" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _write_claude(home: Path, stem: str, message_ids: tuple[str, ...] = ("m1",)) -> Path:
    rows = [
        {
            "sessionId": stem,
            "cwd": "/w",
            "timestamp": TS,
            "message": {
                "role": "assistant",
                "id": message_id,
                "model": CLAUDE_MODEL,
                "usage": {
                    "input_tokens": 1_000,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 2_000,
                    "cache_creation_input_tokens": 500,
                },
            },
        }
        for message_id in message_ids
    ]
    path = home / ".claude" / "projects" / "proj" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _write_pi(home: Path, stem: str, recorded_cost: float) -> Path:
    rows = [
        {"type": "session", "id": stem},
        {
            "type": "message",
            "id": f"{stem}-1",
            "timestamp": TS,
            "message": {
                "role": "assistant",
                "model": PI_MODEL,
                "provider": "minimax",
                "usage": {
                    "input": 1_000,
                    "output": 100,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "cost": {"total": recorded_cost},
                },
            },
        },
    ]
    path = home / ".pi" / "agent" / "sessions" / "proj" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


# --- 1: the parse identity is explicit, not derived from the install --------


def test_the_package_version_alone_causes_zero_parser_calls(_isolated_home, parse_counts, monkeypatch):
    """An upgrade that changes no parser must not rebuild the cache."""
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())

    _sync()
    assert parse_counts["codex"] == 1 and parse_counts["claude"] == 1

    _reset(parse_counts)
    monkeypatch.setattr(tokdash, "__version__", "99.9.9")
    store, _sources = _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    assert "99.9.9" not in json.dumps(_stored_signatures(store))


def test_the_parse_identity_names_no_file_and_no_content(_isolated_home):
    """Nothing path-, mtime- or content-derived may enter a parse identity.

    The old shape hashed coding_tools.py, so any parser edit invalidated every
    parser in it and a byte-identical reinstall at a new path could too.
    """
    tracker = CodingToolsUsageTracker()
    signature = tracker.parsers["codex"].persistent_parser_signature()

    assert signature == {
        "object": "tokdash.sources.coding_tools.CodexParser",
        "version": CodexParser.persistent_parser_version,
        "entry_format": usage_store_module.USAGE_ENTRY_FORMAT_VERSION,
    }
    assert "content_sha1" not in signature
    serialized = json.dumps(signature)
    assert str(Path(coding_tools.__file__)) not in serialized
    assert "/" not in serialized.replace("tokdash.sources", "")


def test_restamping_the_installed_parser_module_causes_zero_parser_calls(
    _isolated_home, parse_counts
):
    """`pipx upgrade` restamps every installed file's mtime; rows must survive."""
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    module_path = Path(coding_tools.__file__)
    stat = module_path.stat()
    try:
        os.utime(module_path, (stat.st_atime + 10_000, stat.st_mtime + 10_000))
        _sync()
    finally:
        os.utime(module_path, (stat.st_atime, stat.st_mtime))

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}


def test_every_persistently_stored_parser_declares_a_valid_version(_isolated_home):
    """Registry check: a new stored parser cannot ship without an identity."""
    tracker = CodingToolsUsageTracker()
    seen_persistent = 0
    for name, parser in tracker.parsers.items():
        mode = parser.sync_capability.mode
        version = type(parser).persistent_parser_version
        if mode == "source_native_db":
            assert version is None, f"{name} is queried live and stores nothing"
            continue
        assert mode in {"file_replace", "source_replace"}, f"unknown sync mode for {name}"
        assert isinstance(version, int) and not isinstance(version, bool), name
        assert version >= 1, f"{name} must declare a positive persistent_parser_version"
        signature = parser.persistent_parser_signature()
        assert signature["object"].endswith(type(parser).__name__)
        assert signature["version"] == version
        assert signature["entry_format"] == usage_store_module.USAGE_ENTRY_FORMAT_VERSION
        seen_persistent += 1
    assert seen_persistent >= 10, "the registry shrank; check this test still covers it"


def test_a_parser_without_a_version_is_rejected_rather_than_silently_hashed(_isolated_home):
    class VersionlessParser(BaseParser):
        source_name = "versionless"

        def _parse_all(self):
            return []

    with pytest.raises(ValueError, match="persistent_parser_version"):
        VersionlessParser(PricingDatabase()).persistent_parser_signature()


def test_bumping_codex_parser_version_reparses_codex_only(
    _isolated_home, parse_counts, monkeypatch
):
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pi(_isolated_home, "p1", recorded_cost=0.25)
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    monkeypatch.setattr(
        CodexParser, "persistent_parser_version", CodexParser.persistent_parser_version + 1
    )
    _sync()

    assert parse_counts == {"codex": 1, "claude": 0, "pi_agent": 0}


def test_bumping_another_parser_leaves_codex_and_claude_cached(
    _isolated_home, parse_counts, monkeypatch
):
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pi(_isolated_home, "p1", recorded_cost=0.25)
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    monkeypatch.setattr(
        PiAgentParser, "persistent_parser_version", PiAgentParser.persistent_parser_version + 1
    )
    _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 1}


def test_adding_an_unrelated_parser_leaves_existing_sources_cached(
    _isolated_home, parse_counts, monkeypatch
):
    """A new source is a new row in the registry, not a cache-wide event."""
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    class NewToolParser(BaseParser):
        source_name = "new_tool"
        sync_capability = coding_tools.SourceSyncCapability(mode="source_replace")
        persistent_parser_version = 1

        def _file_signatures(self):
            return ()

        def _parse_all(self):
            return []

    original_init = CodingToolsUsageTracker.__init__

    def patched_init(self):
        original_init(self)
        self.parsers["new_tool"] = NewToolParser(self.pricing_db)

    monkeypatch.setattr(CodingToolsUsageTracker, "__init__", patched_init)
    _store, sources = _sync()

    assert "new_tool" in sources
    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}


def test_a_shared_entry_format_bump_invalidates_every_stored_source(
    _isolated_home, parse_counts, monkeypatch
):
    """The one identity that IS shared: what a row must carry to be priceable."""
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    monkeypatch.setattr(
        coding_tools,
        "USAGE_ENTRY_FORMAT_VERSION",
        usage_store_module.USAGE_ENTRY_FORMAT_VERSION + 1,
    )
    _sync()

    assert parse_counts["codex"] == 1
    assert parse_counts["claude"] == 1


def test_a_dsh_decoder_version_bump_invalidates_dsh_only(
    _isolated_home, parse_counts, monkeypatch
):
    """DSH's extraction lives in the shared log decoder, so its versions ride along."""
    from tokdash.sources import dsh_log

    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    tracker = CodingToolsUsageTracker()
    before = tracker.parsers["dsh"].persistent_parser_signature()
    codex_before = tracker.parsers["codex"].persistent_parser_signature()

    monkeypatch.setattr(dsh_log, "DSH_DECODER_VERSION", dsh_log.DSH_DECODER_VERSION + 1)
    tracker = CodingToolsUsageTracker()
    after = tracker.parsers["dsh"].persistent_parser_signature()

    assert before != after
    assert after["decoder"]["version"] == dsh_log.DSH_DECODER_VERSION
    assert tracker.parsers["codex"].persistent_parser_signature() == codex_before

    monkeypatch.setattr(dsh_log, "DSH_ACCOUNTING_VERSION", dsh_log.DSH_ACCOUNTING_VERSION + 1)
    assert CodingToolsUsageTracker().parsers["dsh"].persistent_parser_signature() != after


def test_editing_a_source_file_still_reparses_only_that_file(
    _isolated_home, parse_counts
):
    _write_codex(_isolated_home, "c1")
    _write_codex(_isolated_home, "c2")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    store, _sources = _sync()
    before_rows = len(store.query_entries(sources=["codex"]))
    _reset(parse_counts)

    _write_codex(_isolated_home, "c2", turns=2)
    store, _sources = _sync()

    assert parse_counts == {"codex": 1, "claude": 0, "pi_agent": 0}
    assert len(store.query_entries(sources=["codex"])) == before_rows + 1


# --- 2: pricing changes reprice, they never reparse -------------------------


def test_adding_an_unrelated_pricing_model_causes_zero_parser_calls(
    _isolated_home, parse_counts
):
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    store, _sources = _sync()
    codex_before = _cost(store, "codex")
    claude_before = _cost(store, "claude")
    assert codex_before > 0 and claude_before > 0
    _reset(parse_counts)

    _write_pricing(_rates(**{"some-unrelated-model": {"input": 9.0, "output": 9.0}}))
    store, _sources = _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    assert _cost(store, "codex") == pytest.approx(codex_before)
    assert _cost(store, "claude") == pytest.approx(claude_before)


def test_changing_the_rate_of_a_used_model_reprices_without_reparsing(
    _isolated_home, parse_counts
):
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    before = _cost(store, "codex")
    assert before > 0
    _reset(parse_counts)

    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)
    store, _sources = _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    # 3000 input tokens of which 2000 were cached: 1000 fresh at +3.0/M.
    assert _cost(store, "codex") == pytest.approx(before + 1_000 * 3.0 / 1_000_000)


def test_pricing_a_previously_unresolved_model_lifts_the_stored_cost_off_zero(
    _isolated_home, parse_counts
):
    """The stored row must move, not just the aggregate's zero-cost fallback."""
    _write_codex(_isolated_home, "c1")
    _write_pricing({"other-model": {"input": 1.0, "output": 1.0}})
    store, _sources = _sync()
    assert _row_costs(store, "codex") == [0.0]
    _reset(parse_counts)

    _write_pricing(_rates())
    store, _sources = _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    assert _row_costs(store, "codex")[0] > 0


def test_a_model_disappearing_from_pricing_returns_the_row_to_zero(
    _isolated_home, parse_counts
):
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    assert _row_costs(store, "codex")[0] > 0
    _reset(parse_counts)

    _write_pricing({"other-model": {"input": 1.0, "output": 1.0}})
    store, _sources = _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    assert _row_costs(store, "codex") == [0.0]


def test_an_alias_change_reprices_cached_rows(_isolated_home, parse_counts):
    _write_codex(_isolated_home, "c1")
    _write_pricing(
        {"house-model": {"input": 3.0, "output": 15.0, "cache_read": 0.3}},
        aliases={CODEX_MODEL: "house-model"},
    )
    store, _sources = _sync()
    aliased = _row_costs(store, "codex")[0]
    assert aliased > 0
    _reset(parse_counts)

    _write_pricing({"house-model": {"input": 3.0, "output": 15.0, "cache_read": 0.3}})
    store, _sources = _sync()

    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    assert _row_costs(store, "codex") == [0.0]


def test_a_provider_reported_cost_is_identical_after_a_pricing_change(
    _isolated_home, parse_counts
):
    """Pi ships its own per-message cost. Tokdash rates may never move it."""
    _write_pi(_isolated_home, "p1", recorded_cost=0.25)
    _write_pricing(_rates())
    store, _sources = _sync()
    assert _row_costs(store, "pi_agent") == [pytest.approx(0.25)]
    _reset(parse_counts)

    _write_pricing(_rates(**{PI_MODEL: {"input": 900.0, "output": 900.0}}))
    store, _sources = _sync()

    assert parse_counts["pi_agent"] == 0
    assert _row_costs(store, "pi_agent") == [pytest.approx(0.25)]


def test_a_pi_row_without_a_recorded_cost_is_priced_and_repriced(
    _isolated_home, parse_counts
):
    """The other half of the same parser still follows Tokdash's rates."""
    _write_pi(_isolated_home, "p1", recorded_cost=0.0)
    _write_pricing(_rates(**{PI_MODEL: {"input": 3.0, "output": 15.0}}))
    store, _sources = _sync()
    before = _row_costs(store, "pi_agent")[0]
    assert before == pytest.approx((1_000 * 3.0 + 100 * 15.0) / 1_000_000)
    _reset(parse_counts)

    _write_pricing(_rates(**{PI_MODEL: {"input": 6.0, "output": 15.0}}))
    store, _sources = _sync()

    assert parse_counts["pi_agent"] == 0
    assert _row_costs(store, "pi_agent")[0] == pytest.approx(before + 1_000 * 3.0 / 1_000_000)


# --- 3: billing provenance is exact and private -----------------------------


def test_provider_qualified_then_bare_model_fallback_order_is_preserved(tmp_path):
    """Hermes prices provider/model first and only then the bare name."""
    pricing = PricingDatabase(
        db_path=_pricing_file(
            tmp_path / "qualified.json",
            {"acme/m1": {"input": 10.0, "output": 0.0}, "m1": {"input": 1.0, "output": 0.0}},
        ),
        override_path=tmp_path / "absent.json",
    )
    bill = usage_billing_pricing(["acme/m1", "m1"], input_tokens=1_000_000)

    assert usage_entry_cost(bill, pricing) == pytest.approx(10.0)

    bare_only = PricingDatabase(
        db_path=_pricing_file(tmp_path / "bare.json", {"m1": {"input": 1.0, "output": 0.0}}),
        override_path=tmp_path / "absent.json",
    )
    assert usage_entry_cost(bill, bare_only) == pytest.approx(1.0)

    neither = PricingDatabase(
        db_path=_pricing_file(tmp_path / "none.json", {"z": {"input": 1.0, "output": 0.0}}),
        override_path=tmp_path / "absent.json",
    )
    assert usage_entry_cost(bill, neither) == 0.0


def test_a_pricing_record_with_no_candidates_stays_at_zero(tmp_path):
    """Codex rows with no model signal anywhere must never acquire a price."""
    pricing = PricingDatabase(
        db_path=_pricing_file(tmp_path / "p.json", {"unknown": {"input": 99.0, "output": 99.0}}),
        override_path=tmp_path / "absent.json",
    )
    assert usage_entry_cost(usage_billing_pricing([], input_tokens=1_000_000), pricing) == 0.0


def test_a_source_reported_fallback_applies_only_when_nothing_resolves(tmp_path):
    """OpenClaw prefers Tokdash pricing and falls back to its own number."""
    priced = PricingDatabase(
        db_path=_pricing_file(tmp_path / "p.json", {"m1": {"input": 2.0, "output": 0.0}}),
        override_path=tmp_path / "absent.json",
    )
    unpriced = PricingDatabase(
        db_path=_pricing_file(tmp_path / "q.json", {"z": {"input": 2.0, "output": 0.0}}),
        override_path=tmp_path / "absent.json",
    )
    bill = usage_billing_pricing(["m1"], input_tokens=1_000_000, fallback=7.5)

    assert usage_entry_cost(bill, priced) == pytest.approx(2.0)
    assert usage_entry_cost(bill, unpriced) == pytest.approx(7.5)


def test_private_provenance_never_reaches_a_caller(_isolated_home):
    """Private in the row, absent from every read path and from export."""
    _write_codex(_isolated_home, "c1")
    _write_pi(_isolated_home, "p1", recorded_cost=0.25)
    _write_pricing(_rates())
    store, sources = _sync()

    rows = store.query_entries(sources=sources)
    assert rows, "fixture produced no rows"
    for row in rows:
        assert not _private_keys(row), f"query_entries leaked {_private_keys(row)}"

    with sqlite3.connect(store.path) as conn:
        stored = conn.execute(
            "SELECT raw_json, billing_json FROM usage_entries WHERE source = 'codex'"
        ).fetchone()
    assert not _private_keys(json.loads(stored[0])), "raw_json must stay public"
    assert json.loads(stored[1])["kind"] == "pricing", "provenance lives in its own column"

    # /api/usage and /api/tools aggregate through here; `tokdash export` through
    # compute_usage. Neither may carry the private record.
    assert not _private_keys(compute.get_tools_data_for_range(None, None))
    for entry in compute.run_local_coding_tools_json([])["entries"]:
        assert not _private_keys(entry)


def test_the_live_parser_path_hands_back_the_same_public_shape(_isolated_home, monkeypatch):
    """A store failure falls back to live parsing; the rows must not differ."""
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    _sync()

    monkeypatch.setenv("TOKDASH_USAGE_DB", "0")
    _reset()
    live = compute.run_local_coding_tools_json([])

    assert live["entries"], "live fallback produced no rows"
    for entry in live["entries"]:
        assert not _private_keys(entry)


def test_stored_and_live_paths_agree_on_totals_and_costs(_isolated_home):
    """query_entries, aggregate_entries, contribution_days and the live parser."""
    _write_codex(_isolated_home, "c1", turns=2)
    _write_claude(_isolated_home, "a", message_ids=("m1", "m2"))
    _write_pi(_isolated_home, "p1", recorded_cost=0.25)
    _write_pricing(_rates(**{PI_MODEL: {"input": 3.0, "output": 15.0}}))

    store, sources = _sync()
    aggregate = store.aggregate_entries(sources=sources)
    from_rows = compute.parse_entries_json({"entries": store.query_entries(sources=sources)})
    days = store.contribution_days(sources=sources)

    assert aggregate["total_cost"] == pytest.approx(from_rows["total_cost"])
    assert aggregate["total_tokens"] == from_rows["total_tokens"]
    assert sum(day["totals"]["cost"] for day in days) == pytest.approx(aggregate["total_cost"])
    assert sum(day["totals"]["tokens"] for day in days) == aggregate["total_tokens"]

    _reset()
    live = compute.parse_entries_json(
        {"entries": _live_entries(sources)}
    )
    assert live["total_cost"] == pytest.approx(aggregate["total_cost"])
    assert live["total_tokens"] == aggregate["total_tokens"]


# --- 4: the repricing transaction -------------------------------------------


def test_pricing_identity_is_committed_only_with_the_rows_it_describes(_isolated_home):
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    cost_before = _row_costs(store, "codex")
    identity_before = store.stored_pricing_identity()
    assert cost_before[0] > 0 and identity_before

    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)

    def exploding(_billing, _pricing_db):
        raise RuntimeError("pricing blew up mid-pass")

    # A scoped patcher: undoing it must not also undo _isolated_home's patches,
    # which would point the rest of the test at the developer's real logs.
    with pytest.MonkeyPatch.context() as mutation:
        mutation.setattr(usage_store_module, "usage_entry_cost", exploding)
        with pytest.raises(RuntimeError, match="blew up"):
            UsageEntryStore(store.path).apply_pricing(
                usage_store_module.persistent_pricing_signature(PricingDatabase())
            )

    after = UsageEntryStore(store.path)
    assert _row_costs(after, "codex") == cost_before, "rows rolled back"
    assert after.stored_pricing_identity() == identity_before, "identity rolled back"


def test_a_failed_repricing_leaves_the_last_good_cache_servable(
    _isolated_home, parse_counts
):
    """After the failure clears, the next sync reprices — still without parsing."""
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    before = _row_costs(store, "codex")[0]

    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)
    _reset(parse_counts)

    def exploding(_billing, _pricing_db):
        raise RuntimeError("nope")

    with pytest.MonkeyPatch.context() as mutation:
        mutation.setattr(usage_store_module, "usage_entry_cost", exploding)
        with pytest.raises(RuntimeError):
            _sync()
        assert _row_costs(UsageEntryStore(store.path), "codex")[0] == pytest.approx(before)

    _reset(parse_counts)
    store, _sources = _sync()
    assert parse_counts == {"codex": 0, "claude": 0, "pi_agent": 0}
    assert _row_costs(store, "codex")[0] == pytest.approx(before + 1_000 * 3.0 / 1_000_000)


def test_repricing_never_calls_a_parser(_isolated_home, monkeypatch):
    """Explicit: apply_pricing must not reach collect/_parse_all at all."""
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    store, _sources = _sync()

    for cls in (CodexParser, ClaudeParser, PiAgentParser):
        monkeypatch.setattr(
            cls,
            "_parse_all",
            lambda self: pytest.fail("repricing must not parse a source log"),
        )
        monkeypatch.setattr(
            cls,
            "collect",
            lambda self, *a, **k: pytest.fail("repricing must not collect"),
        )

    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)
    fresh = UsageEntryStore(store.path)
    assert fresh.apply_pricing(
        usage_store_module.persistent_pricing_signature(PricingDatabase())
    ) is True
    assert _row_costs(fresh, "codex")[0] > 0


def test_repricing_is_skipped_when_the_pricing_identity_is_unchanged(
    _isolated_home, monkeypatch
):
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()

    identity = usage_store_module.persistent_pricing_signature(PricingDatabase())
    assert UsageEntryStore(store.path).apply_pricing(identity) is False


def test_a_repriced_row_still_deduplicates_against_a_later_reparse(tmp_path):
    """Entry keys must not move when a row is repriced.

    A source without its own row ids is keyed on a hash of its fields. If cost
    were part of that hash, a repriced row would stop colliding with the same
    logical entry reparsed out of another file, and the duplicate would be
    counted twice — the exact failure a pricing edit used to be immune to only
    because it reparsed everything.
    """
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    duplicate = {
        "source": "claude",
        "model": "m1",
        "provider": "anthropic",
        "timestamp": TS_MS,
        "input": 1_000,
        "output": 100,
        "_billing": usage_billing_pricing(["m1"], input_tokens=1_000, output_tokens=100),
    }
    a_path, b_path = str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")
    b_has_duplicate = False

    def parse_file(file_sig):
        if file_sig[0] == a_path:
            return [dict(duplicate, cost=0.001)]
        return [dict(duplicate, cost=0.002)] if b_has_duplicate else []

    store.sync_files(
        "claude",
        ((a_path, 1, 100), (b_path, 1, 100)),
        parser={"v": 1},
        parse_file_entries=parse_file,
    )
    assert len(store.query_entries(sources=["claude"])) == 1

    # Reprice the surviving row, then let the other file produce the same entry
    # at the new price. One logical entry, one row.
    dearer = PricingDatabase(
        db_path=_pricing_file(tmp_path / "p.json", {"m1": {"input": 50.0, "output": 50.0}}),
        override_path=tmp_path / "absent.json",
    )
    assert store.apply_pricing({"content": "v2"}, dearer) is True
    b_has_duplicate = True
    store.sync_files(
        "claude",
        ((a_path, 1, 100), (b_path, 2, 200)),
        parser={"v": 1},
        parse_file_entries=parse_file,
    )

    assert len(store.query_entries(sources=["claude"])) == 1, (
        "a repriced row must still collide with its own duplicate"
    )


# --- 4b: a sync that lands after another process repriced -------------------
#
# Parsing runs OUTSIDE the store lock. So a sync can begin under pricing P1,
# have another process reprice the whole database to P2 while it parses, and
# only then commit its P1-priced rows. If the stored identity were left saying
# P2, every later P2 request would short-circuit in apply_pricing and those
# rows would keep their P1 cost forever.


def _priced_db(tmp_path: Path, name: str, rate: float) -> PricingDatabase:
    return PricingDatabase(
        db_path=_pricing_file(tmp_path / f"{name}.json", {"m1": {"input": rate, "output": 0.0}}),
        override_path=tmp_path / "absent.json",
    )


def _racing_row(cost: float) -> dict:
    return {
        "source": "claude",
        "model": "m1",
        "provider": "",
        "timestamp": TS_MS,
        "input": 1_000_000,
        "output": 0,
        "cost": cost,
        "_billing": usage_billing_pricing(["m1"], input_tokens=1_000_000),
    }


def test_a_file_sync_landing_after_another_process_repriced_is_not_lost(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    p1, p2 = _priced_db(tmp_path, "p1", 1.0), _priced_db(tmp_path, "p2", 5.0)
    id1, id2 = {"rates": "p1"}, {"rates": "p2"}

    store.apply_pricing(id1, p1)

    def parse_file(_file_sig):
        # Another process reprices the database to p2 while this parse runs —
        # the real interleaving, at the real seam (parsing is outside the lock).
        UsageEntryStore(store.path).apply_pricing(id2, p2)
        return [_racing_row(p1.get_cost("m1", 1_000_000, 0, 0, 0))]

    store.sync_files(
        "claude",
        ((str(tmp_path / "a.jsonl"), 1, 100),),
        parser={"v": 1},
        pricing_identity=id1,
        parse_file_entries=parse_file,
    )

    assert store.stored_pricing_identity() is None, (
        "a write under superseded pricing must invalidate the stored identity"
    )
    assert store.apply_pricing(id2, p2) is True, "so the next p2 request still reprices"
    assert _row_costs(store, "claude") == [pytest.approx(5.0)]


def test_a_source_sync_landing_after_another_process_repriced_is_not_lost(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    p1, p2 = _priced_db(tmp_path, "p1", 1.0), _priced_db(tmp_path, "p2", 5.0)
    id1, id2 = {"rates": "p1"}, {"rates": "p2"}

    store.apply_pricing(id1, p1)

    def parse_entries():
        UsageEntryStore(store.path).apply_pricing(id2, p2)
        return [_racing_row(p1.get_cost("m1", 1_000_000, 0, 0, 0))]

    store.sync_source(
        "claude",
        build_source_signature(files=[["a.jsonl", 1, 1]], parser={"v": 1}),
        parse_entries,
        pricing_identity=id1,
    )

    assert store.stored_pricing_identity() is None
    assert store.apply_pricing(id2, p2) is True
    assert _row_costs(store, "claude") == [pytest.approx(5.0)]


def test_a_sync_under_the_current_pricing_leaves_the_identity_alone(tmp_path):
    """The normal case must not churn: no race, no invalidation."""
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    p1 = _priced_db(tmp_path, "p1", 1.0)
    id1 = {"rates": "p1"}
    store.apply_pricing(id1, p1)

    store.sync_files(
        "claude",
        ((str(tmp_path / "a.jsonl"), 1, 100),),
        parser={"v": 1},
        pricing_identity=id1,
        parse_file_entries=lambda _s: [_racing_row(1.0)],
    )

    assert store.stored_pricing_identity() == usage_store_module.stable_json(id1)
    assert store.apply_pricing(id1, p1) is False, "an unraced sync leaves nothing to redo"


def test_openclaw_sync_landing_after_another_process_repriced_is_not_lost(
    _isolated_home, monkeypatch, tmp_path
):
    """OpenClaw runs the same apply-then-sync sequence and needs the same guard."""
    from tokdash.sources import openclaw

    sessions_dir = _isolated_home / ".openclaw" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "s.jsonl").write_text("{}\n", encoding="utf-8")

    p1 = _priced_db(tmp_path, "oc1", 1.0)
    p2 = _priced_db(tmp_path, "oc2", 5.0)
    store_path = usage_store_module.usage_db_path()

    def collect_entries(_dirs):
        # Another process reprices mid-parse.
        UsageEntryStore(store_path).apply_pricing({"rates": "p2"}, p2)
        return [
            {
                "msg_dt": datetime(2026, 5, 19, tzinfo=timezone.utc),
                "model": "m1",
                "input_raw": 1_000_000,
                "cache_write": 0,
                "output": 0,
                "cache_read": 0,
                "payload_cost": 0.0,
                "entry_id": "openclaw:racing",
            }
        ]

    monkeypatch.setattr(openclaw, "_collect_entries", collect_entries)
    store = openclaw._sync_openclaw_store([str(sessions_dir)], p1)

    # The trailing apply_pricing in _sync_openclaw_store rebuilt the costs under
    # this request's own database rather than leaving them under p2's identity.
    assert store.stored_pricing_identity() == usage_store_module.stable_json(
        usage_store_module.persistent_pricing_signature(p1)
    )
    assert _row_costs(store, "openclaw") == [pytest.approx(1.0)]
    # ...and a later p2 request still reprices, because the identity is p1's.
    assert UsageEntryStore(store.path).apply_pricing({"rates": "p2"}, p2) is True
    assert _row_costs(UsageEntryStore(store.path), "openclaw") == [pytest.approx(5.0)]


def test_a_racing_sync_self_heals_within_the_same_request(_isolated_home, monkeypatch):
    """End to end: _sync_usage_store must never return a mixed-price table."""
    home = _isolated_home
    _write_codex(home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    priced_at_3 = _row_costs(store, "codex")[0]
    assert priced_at_3 > 0

    dearer = _rates()
    dearer[CODEX_MODEL]["input"] = 30.0
    other_identity = {"rates": "somebody-elses"}
    original_parse = CodexParser._parse_all

    def parse_then_get_overtaken(self):
        entries = original_parse(self)
        # Another process reprices the whole database while this one parses.
        UsageEntryStore(usage_store_module.usage_db_path()).apply_pricing(
            other_identity, PricingDatabase()
        )
        return entries

    monkeypatch.setattr(CodexParser, "_parse_all", parse_then_get_overtaken)
    _write_codex(home, "c1", turns=2)  # force a reparse
    store, _sources = _sync()

    # Whatever the interleaving, the table is internally consistent: every row
    # priced under the identity the database now claims.
    identity = store.stored_pricing_identity()
    assert identity is not None
    fresh = UsageEntryStore(store.path)
    assert fresh.apply_pricing(json.loads(identity), PricingDatabase()) is False
    costs = _row_costs(store, "codex")
    assert len(costs) == 2 and all(c == pytest.approx(priced_at_3) for c in costs)


# --- 5: the one-time legacy migration ---------------------------------------


LEGACY_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_state (
    source TEXT PRIMARY KEY, signature TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL, entry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE file_state (
    source TEXT NOT NULL, path TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL, safe_offset INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0, signature TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL, entry_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, path)
);
CREATE TABLE usage_entries (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '', entry_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '',
    timestamp INTEGER NOT NULL, input INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0, reasoning INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0, message_count INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT NOT NULL
);
CREATE TABLE session_records (
    tool TEXT NOT NULL, session_id TEXT NOT NULL, file_path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
    safe_offset INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0,
    signature TEXT NOT NULL, updated_at_ms INTEGER NOT NULL,
    started_at_ms INTEGER, last_seen_at_ms INTEGER, raw_json TEXT NOT NULL,
    activity_json TEXT, PRIMARY KEY (tool, file_path, session_id)
);
CREATE TABLE quota_snapshots (
    id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT 'default', bucket TEXT NOT NULL,
    bucket_label TEXT, used_percent REAL, resets_at INTEGER, plan TEXT,
    captured_at INTEGER NOT NULL, source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok', raw_json TEXT,
    UNIQUE(provider, account, bucket, source, captured_at)
);
CREATE TABLE quota_file_state (
    source TEXT NOT NULL, path TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL, safe_offset INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL, PRIMARY KEY (source, path)
);
CREATE UNIQUE INDEX idx_usage_entries_source_key
    ON usage_entries(source, entry_key) WHERE entry_key != '';
"""


def _write_legacy_db(path: Path, *, source: str, file_path: str, cost: float, missing: int) -> None:
    """A v7 database: no billing_json, a module-hash parser, pricing in the signature."""
    raw = {
        "source": source,
        "model": CODEX_MODEL,
        "provider": "openai",
        "input": 1_000,
        "output": 100,
        "cacheRead": 2_000,
        "cacheWrite": 0,
        "reasoning": 50,
        "cost": cost,
        "timestamp": TS_MS,
        "messageCount": 1,
        "entry_key": "legacy-row-1",
    }
    legacy_signature = json.dumps(
        {
            "signature_version": 3,
            "files": [[file_path, 1, 100]],
            "pricing": {"content": ["pricing-content-v1", "baseline", 1, "abc"]},
            "parser": {
                "object": "tokdash.sources.coding_tools.CodexParser",
                "content_sha1": "a-module-hash-from-v2.0.0",
            },
            "extra": {"mode": "file"},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '7')")
        conn.execute(
            """
            INSERT INTO usage_entries(
                source, file_path, entry_key, model, provider, timestamp,
                input, output, cache_read, cache_write, reasoning,
                cost, message_count, raw_json
            ) VALUES (?, ?, 'legacy-row-1', ?, 'openai', ?, 1000, 100, 2000, 0, 50, ?, 1, ?)
            """,
            (source, file_path, CODEX_MODEL, TS_MS, cost, json.dumps(raw, sort_keys=True)),
        )
        conn.execute(
            """
            INSERT INTO file_state(
                source, path, mtime_ns, size, safe_offset, missing,
                signature, updated_at_ms, entry_count
            ) VALUES (?, ?, 1, 100, 100, ?, ?, 1, 1)
            """,
            (source, file_path, missing, legacy_signature),
        )
        conn.execute(
            "INSERT INTO source_state(source, signature, updated_at_ms, entry_count)"
            " VALUES (?, ?, 1, 1)",
            (source, legacy_signature),
        )
        conn.commit()


def test_a_legacy_row_whose_log_still_exists_rebuilds_once_then_hits_the_cache(
    _isolated_home, parse_counts, monkeypatch
):
    log = _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    db_path = Path(os.environ["TOKDASH_USAGE_DB_PATH"])
    _write_legacy_db(db_path, source="codex", file_path=str(log), cost=0.5, missing=0)

    store, _sources = _sync()
    assert parse_counts["codex"] == 1, "the legacy signature rebuilds once"
    rebuilt = _row_costs(store, "codex")
    assert rebuilt != [0.5], "the rebuilt row is priced by this build"

    _reset(parse_counts)
    store, _sources = _sync()
    assert parse_counts["codex"] == 0, "and never again"
    assert _row_costs(store, "codex") == rebuilt

    # A later pricing edit now reprices instead of triggering a second rebuild.
    _reset(parse_counts)
    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)
    store, _sources = _sync()
    assert parse_counts["codex"] == 0
    assert _row_costs(store, "codex")[0] > rebuilt[0]


def test_a_legacy_durable_row_whose_log_is_gone_keeps_its_cost(
    _isolated_home, parse_counts
):
    """No trustworthy provenance and no log to reread: preserve, do not guess."""
    _write_pricing(_rates())
    db_path = Path(os.environ["TOKDASH_USAGE_DB_PATH"])
    missing_log = str(_isolated_home / ".codex" / "sessions" / "gone.jsonl")
    _write_legacy_db(db_path, source="codex", file_path=missing_log, cost=0.5, missing=0)

    store, _sources = _sync()
    assert _row_costs(store, "codex") == [pytest.approx(0.5)]

    _reset(parse_counts)
    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 600.0
    _write_pricing(doubled)
    store, _sources = _sync()

    assert parse_counts["codex"] == 0
    assert _row_costs(store, "codex") == [pytest.approx(0.5)], (
        "a legacy orphan's cost is preserved as fixed, not repriced from a guess"
    )
    assert store.status()["usage_entries"] == 1, "durable history is never dropped"


def test_the_legacy_migration_keeps_session_records_and_quota_history(
    _isolated_home, parse_counts
):
    _write_pricing(_rates())
    db_path = Path(os.environ["TOKDASH_USAGE_DB_PATH"])
    log = _write_codex(_isolated_home, "c1")
    _write_legacy_db(db_path, source="codex", file_path=str(log), cost=0.5, missing=0)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO session_records(
                tool, session_id, file_path, mtime_ns, size, safe_offset, missing,
                signature, updated_at_ms, raw_json
            ) VALUES ('codex', 's1', '/gone.jsonl', 1, 2, 2, 1, 'old', 3, '{"turns": []}')
            """
        )
        conn.execute(
            """
            INSERT INTO quota_snapshots(
                provider, bucket, used_percent, captured_at, source, status
            ) VALUES ('codex', '5h', 12.5, 1, 'codex_api', 'ok')
            """
        )
        conn.commit()

    _sync()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quota_snapshots").fetchone()[0] == 1


# --- 6: mutation checks -----------------------------------------------------
#
# Each of these installs the defect the design forbids and asserts the symptom
# the tests above rule out. If one of these starts passing without its
# mutation, the corresponding guarantee has stopped being tested.


def test_mutation_pricing_back_in_the_parse_signature_reparses_everything(
    _isolated_home, parse_counts, monkeypatch
):
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    original = BaseParser.persistent_parser_signature

    def with_pricing(self):
        signature = original(self)
        signature["pricing"] = usage_store_module.persistent_pricing_signature(self.pricing_db)
        return signature

    monkeypatch.setattr(BaseParser, "persistent_parser_signature", with_pricing)
    _write_pricing(_rates(**{"some-unrelated-model": {"input": 9.0, "output": 9.0}}))
    _sync()

    assert parse_counts["codex"] == 1 and parse_counts["claude"] == 1, (
        "with pricing in the parse signature an unrelated pricing entry reparses "
        "every source — which is exactly what the tests above forbid"
    )


def test_mutation_skipping_the_repricing_pass_serves_a_stale_cost(
    _isolated_home, monkeypatch
):
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    before = _row_costs(store, "codex")[0]

    monkeypatch.setattr(UsageEntryStore, "apply_pricing", lambda *a, **k: False)
    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)
    store, _sources = _sync()

    assert _row_costs(store, "codex")[0] == pytest.approx(before), (
        "without the repricing pass the new rate never reaches the stored row"
    )


def test_mutation_repricing_a_fixed_cost_moves_a_provider_reported_number(
    _isolated_home, monkeypatch
):
    _write_pi(_isolated_home, "p1", recorded_cost=0.25)
    _write_pricing(_rates(**{PI_MODEL: {"input": 3.0, "output": 15.0}}))
    store, _sources = _sync()
    assert _row_costs(store, "pi_agent") == [pytest.approx(0.25)]

    def repricing_fixed(billing, pricing_db):
        # The defect: treat a fixed record as if it were a pricing one.
        return float(
            pricing_db.get_cost(PI_MODEL, 1_000, 100, 0, 0)
            if billing.get("kind") == "fixed"
            else usage_entry_cost(billing, pricing_db)
        )

    monkeypatch.setattr(usage_store_module, "usage_entry_cost", repricing_fixed)
    _write_pricing(_rates(**{PI_MODEL: {"input": 900.0, "output": 900.0}}))
    store, _sources = _sync()

    assert _row_costs(store, "pi_agent") != [pytest.approx(0.25)], (
        "repricing a fixed record rewrites a cost the provider reported"
    )


def test_mutation_a_module_hash_parser_identity_invalidates_unrelated_sources(
    _isolated_home, parse_counts, monkeypatch
):
    _write_codex(_isolated_home, "c1")
    _write_claude(_isolated_home, "a")
    _write_pricing(_rates())
    _sync()
    _reset(parse_counts)

    # The pre-2.1 identity: one hash of coding_tools.py, shared by every parser.
    def module_hash(self):
        return usage_store_module.parser_code_signature(self)

    monkeypatch.setattr(BaseParser, "persistent_parser_signature", module_hash)
    monkeypatch.setattr(
        usage_store_module,
        "_parser_file_content_hash",
        lambda _path, _stat: "an-unrelated-parser-was-edited",
    )
    _sync()

    assert parse_counts["codex"] == 1 and parse_counts["claude"] == 1, (
        "a module hash makes one parser's edit invalidate every parser sharing "
        "the file"
    )


def test_mutation_committing_the_pricing_identity_first_loses_a_rate_change(_isolated_home):
    _write_codex(_isolated_home, "c1")
    _write_pricing(_rates())
    store, _sources = _sync()
    before = _row_costs(store, "codex")[0]

    doubled = _rates()
    doubled[CODEX_MODEL]["input"] = 6.0
    _write_pricing(doubled)

    def identity_first(self, pricing_identity, pricing_db=None, **_kwargs):
        # The defect: stamp the identity in its own committed transaction and
        # only then start updating rows — a crash in between is unrecoverable.
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (
                    usage_store_module._PRICING_IDENTITY_META_KEY,
                    usage_store_module.stable_json(pricing_identity),
                ),
            )
            conn.commit()
        raise RuntimeError("crashed after stamping the identity")

    with pytest.MonkeyPatch.context() as mutation:
        mutation.setattr(UsageEntryStore, "apply_pricing", identity_first)
        with pytest.raises(RuntimeError):
            _sync()

    _reset()
    store, _sources = _sync()
    assert _row_costs(store, "codex")[0] == pytest.approx(before), (
        "the identity says the rows are current while they still hold the old "
        "cost, and no later sync will ever fix them"
    )


# --- helpers ----------------------------------------------------------------


def _private_keys(value) -> list[str]:
    """Every underscore-prefixed key anywhere in a JSON-shaped payload."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.startswith("_"):
                found.append(key)
            found.extend(_private_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_private_keys(child))
    return found


def _pricing_file(path: Path, models: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "t", "aliases": {}, "models": models}), encoding="utf-8")
    return path


def _stored_signatures(store: UsageEntryStore) -> list[str]:
    with sqlite3.connect(store.path) as conn:
        return [
            str(row[0])
            for row in conn.execute("SELECT signature FROM source_state").fetchall()
        ] + [
            str(row[0]) for row in conn.execute("SELECT signature FROM file_state").fetchall()
        ]


def _live_entries(sources: list[str]) -> list[dict]:
    tracker = CodingToolsUsageTracker()
    tracker.collect(None, None, sources)
    return [
        usage_store_module.public_usage_entry(entry)
        for entry in tracker.to_json()["entries"]
    ]
