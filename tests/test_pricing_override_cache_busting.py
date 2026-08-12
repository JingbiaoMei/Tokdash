"""Regressions for runtime and persistent pricing cache identities.

Both identities must cover the data-dir override. Runtime identities may use filesystem
metadata, while persistent identities must survive reinstall path and mtime changes.
"""
import json
import os

import tokdash.api as api
import tokdash.compute as compute
from tokdash.onboard import paths
from tokdash.pricing import PricingDatabase
from tokdash.sources import openclaw
from tokdash.sources.coding_tools import ClaudeParser
from tokdash.usage_store import UsageEntryStore


def _write_override() -> None:
    ov = paths.pricing_db_override_path()
    ov.parent.mkdir(parents=True, exist_ok=True)
    ov.write_text(json.dumps({"models": {"foo": {"input": 999.0, "output": 999.0}}}), encoding="utf-8")


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
    persistent_before = parser._persistent_pricing_signature()
    _write_override()
    after = parser._pricing_signature()
    persistent_after = parser._persistent_pricing_signature()
    assert before != after  # the override write must change the cache-busting signature
    assert persistent_before != persistent_after
    # ...and it must track exactly the files PricingDatabase.load() reads (baseline + override)
    assert after == tuple(pdb.signature())
    assert persistent_after == tuple(pdb.content_signature())


def test_openclaw_pricing_signature_busts_on_override():
    pdb = PricingDatabase()
    before = openclaw._pricing_signature(pdb)
    persistent_before = openclaw._persistent_pricing_signature(pdb)
    _write_override()
    after = openclaw._pricing_signature(pdb)
    persistent_after = openclaw._persistent_pricing_signature(pdb)
    assert before != after
    assert persistent_before != persistent_after
    assert after == tuple(pdb.signature())
    assert persistent_after == tuple(pdb.content_signature())


def test_persistent_pricing_signatures_survive_reinstall_metadata(tmp_path):
    payload = '{\n  "models": {"foo": {"input": 1, "output": 2}}\n}\n'
    first_path = tmp_path / "first-install" / "pricing_db.json"
    second_path = tmp_path / "second-install" / "pricing_db.json"
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_text(payload, encoding="utf-8")
    second_path.write_text(payload, encoding="utf-8")
    os.utime(second_path, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))

    first = PricingDatabase(db_path=first_path, override_path=tmp_path / "missing-first.json")
    second = PricingDatabase(db_path=second_path, override_path=tmp_path / "missing-second.json")

    # Runtime identities intentionally include install metadata.
    assert first.signature() != second.signature()
    # Persistent identities depend only on effective pricing content.
    assert (
        ClaudeParser(first)._persistent_pricing_signature()
        == ClaudeParser(second)._persistent_pricing_signature()
    )
    assert openclaw._persistent_pricing_signature(first) == openclaw._persistent_pricing_signature(second)

    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    files = ((str(tmp_path / "session.jsonl"), 1, 100),)
    parse_calls = []

    def parse_file(file_signature):
        parse_calls.append(file_signature)
        return []

    assert store.sync_files(
        "claude",
        files,
        pricing=ClaudeParser(first)._persistent_pricing_signature(),
        parse_file_entries=parse_file,
    )
    assert not store.sync_files(
        "claude",
        files,
        pricing=ClaudeParser(second)._persistent_pricing_signature(),
        parse_file_entries=parse_file,
    )
    assert parse_calls == [files[0]]


def test_usage_store_sync_uses_persistent_pricing_identity(monkeypatch):
    tracker = compute.CodingToolsUsageTracker()
    parser = tracker.parsers["claude"]
    captured = {}

    class FakeStore:
        def sync_files(self, source, files, **kwargs):
            captured["source"] = source
            captured["pricing"] = kwargs["pricing"]
            return False

    monkeypatch.setattr(compute, "UsageEntryStore", FakeStore)
    monkeypatch.setattr(compute, "_usage_store_sources", lambda _tracker: ["claude"])
    monkeypatch.setattr(parser, "_file_signatures", lambda: ())
    monkeypatch.setattr(parser, "_pricing_signature", lambda: ("runtime-metadata",))
    monkeypatch.setattr(parser, "_persistent_pricing_signature", lambda: ("content",))

    compute._sync_usage_store(tracker)

    assert captured == {"source": "claude", "pricing": ("content",)}


def test_openclaw_store_sync_uses_persistent_pricing_identity(monkeypatch, tmp_path):
    captured = {}

    class FakeStore:
        def sync_source(self, source, signature, collect):
            captured["source"] = source
            captured["signature"] = json.loads(signature)
            return False

    pricing = PricingDatabase(
        db_path=tmp_path / "missing-baseline.json",
        override_path=tmp_path / "missing-override.json",
    )
    monkeypatch.setattr(openclaw, "UsageEntryStore", FakeStore)
    monkeypatch.setattr(openclaw, "_session_files", lambda _session_dirs: [])
    monkeypatch.setattr(openclaw, "_persistent_pricing_signature", lambda _pricing: ("content",))

    openclaw._sync_openclaw_store([], pricing)

    assert captured["source"] == "openclaw"
    assert captured["signature"]["pricing"] == ["content"]


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
