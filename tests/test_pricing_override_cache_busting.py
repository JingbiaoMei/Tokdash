"""Regression coverage for runtime and persistent pricing cache identities.

Runtime caches use file metadata plus override content so out-of-band edits are noticed
immediately. Persistent stores use effective pricing content plus the pricing implementation
so unchanged reinstalls remain fast while real data or cost-calculation changes rebuild rows.
"""
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import tokdash.api as api
import tokdash.compute as compute_module
import tokdash.usage_store as usage_store_module
from tokdash.onboard import paths
from tokdash.pricing import PricingDatabase
from tokdash.sources import openclaw
from tokdash.sources.coding_tools import ClaudeParser


def _write_override() -> None:
    ov = paths.pricing_db_override_path()
    ov.parent.mkdir(parents=True, exist_ok=True)
    ov.write_text(json.dumps({"models": {"foo": {"input": 999.0, "output": 999.0}}}), encoding="utf-8")


def _installed_pricing_db(root, input_rate: float, mtime: float) -> PricingDatabase:
    root.mkdir(parents=True, exist_ok=True)
    baseline = root / "pricing_db.json"
    baseline.write_text(
        json.dumps(
            {
                "models": {
                    "foo": {
                        "input": input_rate,
                        "output": 2.0,
                        "unit": "per_million_tokens",
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.utime(baseline, (mtime, mtime))
    return PricingDatabase(
        db_path=baseline,
        override_path=root / "missing-override.json",
    )


def test_pricing_content_signature_ignores_reinstall_metadata(tmp_path):
    baseline = tmp_path / "pricing_db.json"
    baseline_lf = '{\n  "models": {"foo": {"input": 1, "output": 2}}\n}\n'
    baseline.write_text(baseline_lf, encoding="utf-8")
    pricing = PricingDatabase(
        db_path=baseline,
        override_path=tmp_path / "missing-override.json",
    )
    before = pricing.content_signature()

    stat = baseline.stat()
    os.utime(baseline, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert pricing.content_signature() == before

    baseline.write_bytes(baseline_lf.replace("\n", "\r\n").encode("utf-8"))
    assert pricing.content_signature() == before

    # Same-length content changes still invalidate the identity.
    baseline.write_text(
        json.dumps({"models": {"foo": {"input": 9, "output": 2}}}),
        encoding="utf-8",
    )
    assert pricing.content_signature() != before


def test_coding_tools_pricing_signature_busts_on_override():
    pdb = PricingDatabase()
    parser = ClaudeParser(pdb)
    before = parser._pricing_signature()
    _write_override()
    after = parser._pricing_signature()
    assert before != after  # the override write must change the cache-busting signature
    # ...and it must track exactly the files PricingDatabase.load() reads (baseline + override)
    assert after == tuple(pdb.signature())


def test_openclaw_pricing_signature_busts_on_override():
    pdb = PricingDatabase()
    before = openclaw._pricing_signature(pdb)
    _write_override()
    after = openclaw._pricing_signature(pdb)
    assert before != after
    assert after == tuple(pdb.signature())


def test_coding_tools_persistent_store_survives_identical_reinstall(monkeypatch, tmp_path):
    first_db = _installed_pricing_db(tmp_path / "install-v1", 1.0, 1_700_000_000)
    reinstalled_db = _installed_pricing_db(tmp_path / "install-v2", 1.0, 1_800_000_000)
    changed_db = _installed_pricing_db(tmp_path / "install-v3", 9.0, 1_900_000_000)

    file_sig = ((str(tmp_path / "usage.jsonl"), 123, 456),)
    parse_calls: list[str] = []

    def tracker_for(pricing_db: PricingDatabase):
        parser = ClaudeParser(pricing_db)
        parser._file_signatures = lambda: file_sig

        def parse_all():
            parse_calls.append(str(pricing_db.db_path))
            return [
                {
                    "source": "claude",
                    "model": "foo",
                    "provider": "anthropic",
                    "timestamp": 1_700_000_000_000,
                    "input": 10,
                    "output": 5,
                    "cost": 0.0,
                }
            ]

        parser._parse_all = parse_all
        tracker = type("Tracker", (), {})()
        tracker.pricing_db = pricing_db
        tracker.parsers = {"claude": parser}
        return tracker, parser

    first_tracker, first_parser = tracker_for(first_db)
    reinstalled_tracker, reinstalled_parser = tracker_for(reinstalled_db)
    changed_tracker, changed_parser = tracker_for(changed_db)

    assert first_parser._pricing_signature() != reinstalled_parser._pricing_signature()
    assert usage_store_module.persistent_pricing_signature(
        first_db
    ) == usage_store_module.persistent_pricing_signature(reinstalled_db)
    assert usage_store_module.persistent_pricing_signature(
        first_db
    ) != usage_store_module.persistent_pricing_signature(changed_db)

    compute_module._sync_usage_store(first_tracker)
    compute_module._sync_usage_store(reinstalled_tracker)
    assert len(parse_calls) == 1

    original_code_signature = usage_store_module.parser_code_signature

    def changed_pricing_implementation(obj):
        signature = original_code_signature(obj)
        if obj is PricingDatabase:
            return {**signature, "content_sha1": "changed-pricing-implementation"}
        return signature

    monkeypatch.setattr(
        usage_store_module,
        "parser_code_signature",
        changed_pricing_implementation,
    )
    compute_module._sync_usage_store(reinstalled_tracker)
    assert len(parse_calls) == 2

    compute_module._sync_usage_store(changed_tracker)
    assert len(parse_calls) == 3


def test_coding_tools_computes_persistent_pricing_signature_once_per_tracker(monkeypatch):
    sync_calls: list[str] = []
    pricing_calls: list[object] = []

    class FakeStore:
        def sync_files(self, source, _files, **_kwargs):
            sync_calls.append(source)

    pricing_db = object()
    capability = SimpleNamespace(mode="file_replace", append_jsonl=False)
    parsers = {
        name: SimpleNamespace(
            sync_capability=capability,
            _file_signatures=lambda: (),
        )
        for name in ("claude", "codex", "gemini_cli")
    }
    tracker = SimpleNamespace(pricing_db=pricing_db, parsers=parsers)

    monkeypatch.setattr(compute_module, "UsageEntryStore", FakeStore)
    monkeypatch.setattr(
        compute_module,
        "persistent_pricing_signature",
        lambda value: pricing_calls.append(value) or {"pricing": "signature"},
    )

    compute_module._sync_usage_store(tracker)

    assert pricing_calls == [pricing_db]
    assert sync_calls == ["claude", "codex", "gemini_cli"]


def test_openclaw_persistent_store_survives_identical_reinstall(monkeypatch, tmp_path):
    first_db = _installed_pricing_db(tmp_path / "openclaw-v1", 1.0, 1_700_000_000)
    reinstalled_db = _installed_pricing_db(tmp_path / "openclaw-v2", 1.0, 1_800_000_000)
    changed_db = _installed_pricing_db(tmp_path / "openclaw-v3", 9.0, 1_900_000_000)

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
    parse_calls: list[str] = []

    def collect_entries(_session_dirs):
        parse_calls.append("parsed")
        return [
            {
                "msg_dt": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "model": "foo",
                "input_raw": 10,
                "cache_write": 0,
                "output": 5,
                "cache_read": 0,
                "payload_cost": 0.0,
                "entry_id": "openclaw:test-entry",
            }
        ]

    monkeypatch.setattr(openclaw, "_collect_entries", collect_entries)

    assert openclaw._pricing_signature(first_db) != openclaw._pricing_signature(reinstalled_db)
    assert usage_store_module.persistent_pricing_signature(
        first_db
    ) == usage_store_module.persistent_pricing_signature(reinstalled_db)
    assert usage_store_module.persistent_pricing_signature(
        first_db
    ) != usage_store_module.persistent_pricing_signature(changed_db)

    openclaw._sync_openclaw_store([str(sessions_dir)], first_db)
    openclaw._sync_openclaw_store([str(sessions_dir)], reinstalled_db)
    assert len(parse_calls) == 1

    original_code_signature = usage_store_module.parser_code_signature

    def changed_pricing_implementation(obj):
        signature = original_code_signature(obj)
        if obj is PricingDatabase:
            return {**signature, "content_sha1": "changed-pricing-implementation"}
        return signature

    monkeypatch.setattr(
        usage_store_module,
        "parser_code_signature",
        changed_pricing_implementation,
    )
    openclaw._sync_openclaw_store([str(sessions_dir)], reinstalled_db)
    assert len(parse_calls) == 2

    openclaw._sync_openclaw_store([str(sessions_dir)], changed_db)
    assert len(parse_calls) == 3


def test_sessions_singleton_reloads_when_override_changes_out_of_band():
    # sessions computes cost via a long-lived _PRICING_DB singleton refreshed only by
    # reload_pricing_db() (the dashboard PUT). If the override changes by any OTHER path
    # (manual edit while serving / a sibling --workers process), the read path's
    # _pricing_signature() must reload the singleton so a cache MISS re-parses at the NEW
    # rates. (Regression: pricing-sessions-singleton-stale.)
    from tokdash import sessions

    # Start from a known baseline state with the last-loaded signature in sync.
    sessions._PRICING_DB.load()
    sessions._pricing_last_loaded_sig = sessions._PRICING_DB.signature()
    ov = paths.pricing_db_override_path()
    ov.parent.mkdir(parents=True, exist_ok=True)
    try:
        ov.write_text(
            json.dumps({"models": {"zzz-sessions-probe": {"input": 1234.0, "output": 0.0, "unit": "per_million_tokens"}}}),
            encoding="utf-8",
        )
        sessions._pricing_signature()  # a read recomputes the cache key -> reloads the singleton
        assert sessions._PRICING_DB.get_cost("zzz-sessions-probe", 1_000_000, 0) == 1234.0
    finally:
        if ov.exists():
            ov.unlink()
        sessions._PRICING_DB.load()
        sessions._pricing_last_loaded_sig = sessions._PRICING_DB.signature()


def test_api_response_cache_busts_when_override_changes_out_of_band(monkeypatch):
    # Parser/storage cache signatures are not enough: the route-level response cache also
    # needs the pricing signature, or a manual override edit can keep serving stale JSON
    # until TOKDASH_CACHE_TTL expires.
    api._clear_cache()
    calls = []

    def fake_usage(period, date_from, date_to):
        calls.append((period, date_from, date_to))
        return {"call": len(calls)}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    try:
        first = api.get_usage("today")
        cached = api.get_usage("today")
        assert first["call"] == 1
        assert first["response_cache"]["status"] == "recomputed"
        assert cached["call"] == 1
        assert cached["response_cache"]["status"] == "hit"
        _write_override()
        refreshed = api.get_usage("today")
        assert refreshed["call"] == 2
        assert refreshed["response_cache"]["status"] == "recomputed"
        assert calls == [("today", None, None), ("today", None, None)]
    finally:
        api._clear_cache()


def test_api_response_cache_busts_when_existing_override_changes_out_of_band(monkeypatch):
    # The route cache key caches the override content hash between calls for speed. It must
    # still notice a normal external edit to an already-present override via the file stat.
    api._clear_cache()
    api._clear_pricing_signature_cache()
    ov = paths.pricing_db_override_path()
    ov.parent.mkdir(parents=True, exist_ok=True)
    ov.write_text(json.dumps({"models": {"foo": {"input": 1.0, "output": 1.0}}}), encoding="utf-8")
    calls = []

    def fake_usage(period, date_from, date_to):
        calls.append((period, date_from, date_to))
        return {"call": len(calls)}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)

    try:
        first = api.get_usage("today")
        cached = api.get_usage("today")
        assert first["call"] == 1
        assert first["response_cache"]["status"] == "recomputed"
        assert cached["call"] == 1
        assert cached["response_cache"]["status"] == "hit"
        ov.write_text(json.dumps({"models": {"foo": {"input": 2.0, "output": 2.0}}}), encoding="utf-8")
        st = ov.stat()
        os.utime(ov, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        refreshed = api.get_usage("today")
        assert refreshed["call"] == 2
        assert refreshed["response_cache"]["status"] == "recomputed"
        assert calls == [("today", None, None), ("today", None, None)]
    finally:
        api._clear_cache()
        api._clear_pricing_signature_cache()
