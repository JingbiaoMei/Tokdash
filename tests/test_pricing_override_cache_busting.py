"""Regression: a dashboard pricing edit writes ONLY the data-dir override, so the
coding-tools and OpenClaw cache-busting signatures must cover the override too.

Before the fix these signatures stat'd only the packaged baseline (``pricing_db.db_path``),
so an edit never changed them — the in-memory entry caches AND the persistent usage store
(which key on the same signature) kept serving costs computed at the OLD prices.
"""
import json
import os

import tokdash.api as api
from tokdash.onboard import paths
from tokdash.pricing import PricingDatabase
from tokdash.sources import openclaw
from tokdash.sources.coding_tools import ClaudeParser


def _write_override() -> None:
    ov = paths.pricing_db_override_path()
    ov.parent.mkdir(parents=True, exist_ok=True)
    ov.write_text(json.dumps({"models": {"foo": {"input": 999.0, "output": 999.0}}}), encoding="utf-8")


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
