import json

from fastapi import HTTPException

import tokdash.api as api


def _write_pricing_db(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_pricing_db_editor_reads_and_saves_valid_json(tmp_path, monkeypatch):
    pricing_path = tmp_path / "pricing_db.json"
    original = {
        "version": "test",
        "aliases": {},
        "models": {
            "demo-model": {
                "provider": "demo",
                "input": 1.0,
                "output": 2.0,
                "cache_read": 0.1,
                "cache_write": 1.0,
                "unit": "per_million_tokens",
            }
        },
    }
    updated = {
        **original,
        "models": {
            **original["models"],
            "new-model": {
                "provider": "demo",
                "input": 3.0,
                "output": 4.0,
                "cache_read": 0.3,
                "cache_write": 3.0,
                "unit": "per_million_tokens",
            },
        },
    }
    _write_pricing_db(pricing_path, original)
    monkeypatch.setattr(api, "PRICING_DB_PATH", pricing_path, raising=False)
    # Edits persist to the data-dir override (survives `tokdash update`), NOT the packaged baseline.
    override_path = api._pricing_override_path()
    assert not override_path.exists()
    events = []

    def fake_reload_pricing_db():
        events.append("reload")

    def fake_clear_cache():
        events.append("clear")
        api._cache.clear()

    monkeypatch.setattr(api, "reload_pricing_db", fake_reload_pricing_db)
    monkeypatch.setattr(api, "_clear_cache", fake_clear_cache)
    api._cache["stale"] = (0.0, {"old": True})

    read_response = api.get_pricing_db()
    assert read_response["data"] == original  # no override yet -> baseline only
    assert read_response["text"] == json.dumps(original, indent=2, ensure_ascii=False) + "\n"
    assert read_response["baseline_path"] == str(pricing_path)

    save_response = api.update_pricing_db({"text": json.dumps(updated)})
    assert save_response["data"] == updated  # full-replacement: the saved override IS the effective DB
    assert json.loads(override_path.read_text(encoding="utf-8")) == updated  # written to override
    assert json.loads(pricing_path.read_text(encoding="utf-8")) == original  # baseline untouched
    assert api._cache == {}
    assert events == ["reload", "clear"]
    # a subsequent read reflects the override (authoritative full replacement, not a merge)
    assert api.get_pricing_db()["data"] == updated


def test_pricing_edit_persists_under_data_dir_not_packaged_file(tmp_path, monkeypatch):
    # The packaged baseline must never be written (it lives in site-packages and is wiped by
    # `tokdash update`); edits must land under TOKDASH_DATA_DIR so they survive.
    from tokdash.onboard import paths

    baseline = tmp_path / "packaged" / "pricing_db.json"
    baseline.parent.mkdir(parents=True)
    _write_pricing_db(baseline, {"models": {"base-only": {"input": 1.0, "output": 1.0}}})
    baseline_before = baseline.read_text(encoding="utf-8")
    monkeypatch.setattr(api, "PRICING_DB_PATH", baseline, raising=False)
    monkeypatch.setattr(api, "reload_pricing_db", lambda: None)
    monkeypatch.setattr(api, "_clear_cache", lambda: None)

    api.update_pricing_db({"data": {"models": {"edited": {"input": 9.0, "output": 9.0}}}})

    # write went under the data dir, baseline untouched
    assert paths.pricing_db_override_path().is_file()
    assert str(paths.pricing_db_override_path()).startswith(str(paths.data_dir()))
    assert baseline.read_text(encoding="utf-8") == baseline_before
    # Full-replacement (WYSIWYG): the override is authoritative — `edited` is present and the
    # baseline-only model the user did NOT include is gone (a deletion sticks).
    effective = api.get_pricing_db()
    assert effective["source"] == "override"
    assert "edited" in effective["data"]["models"] and "base-only" not in effective["data"]["models"]


def test_pricing_db_response_reports_baseline_version(tmp_path, monkeypatch):
    # A saved override fully replaces (and thus freezes) the baseline, so the API surfaces the
    # shipped baseline's version even when an override is active, letting a UI flag drift.
    baseline = tmp_path / "pricing_db.json"
    _write_pricing_db(baseline, {"version": "2026.06.01", "models": {"m": {"input": 1.0, "output": 1.0}}})
    monkeypatch.setattr(api, "PRICING_DB_PATH", baseline, raising=False)
    assert api.get_pricing_db()["baseline_version"] == "2026.06.01"
    # still reported when an override is in effect
    monkeypatch.setattr(api, "reload_pricing_db", lambda: None)
    monkeypatch.setattr(api, "_clear_cache", lambda: None)
    save = api.update_pricing_db({"data": {"models": {"edited": {"input": 9.0, "output": 9.0}}}})
    assert save["source"] == "override" and save["baseline_version"] == "2026.06.01"


def test_corrupt_override_falls_back_to_baseline(tmp_path, monkeypatch):
    from tokdash.onboard import paths

    baseline = tmp_path / "packaged" / "pricing_db.json"
    baseline.parent.mkdir(parents=True)
    _write_pricing_db(baseline, {"models": {"base-only": {"input": 1.0, "output": 1.0}}})
    monkeypatch.setattr(api, "PRICING_DB_PATH", baseline, raising=False)
    paths.pricing_db_override_path().parent.mkdir(parents=True, exist_ok=True)
    paths.pricing_db_override_path().write_text("{corrupt", encoding="utf-8")
    # A corrupt override must NOT wipe pricing — fall back to the baseline.
    eff = api.get_pricing_db()
    assert eff["source"] == "baseline" and "base-only" in eff["data"]["models"]


def test_pricing_db_editor_rejects_invalid_json_without_overwriting(tmp_path, monkeypatch):
    pricing_path = tmp_path / "pricing_db.json"
    original = {"version": "test", "aliases": {}, "models": {}}
    _write_pricing_db(pricing_path, original)
    monkeypatch.setattr(api, "PRICING_DB_PATH", pricing_path, raising=False)

    try:
        api.update_pricing_db({"text": "{not json"})
    except HTTPException as e:
        assert e.status_code == 400
        assert "Invalid JSON" in e.detail
    else:
        raise AssertionError("Expected invalid JSON to be rejected")
    assert json.loads(pricing_path.read_text(encoding="utf-8")) == original


def test_pricing_db_editor_rejects_missing_models_object(tmp_path, monkeypatch):
    pricing_path = tmp_path / "pricing_db.json"
    original = {"version": "test", "aliases": {}, "models": {}}
    _write_pricing_db(pricing_path, original)
    monkeypatch.setattr(api, "PRICING_DB_PATH", pricing_path, raising=False)

    try:
        api.update_pricing_db({"data": {"version": "test"}})
    except HTTPException as e:
        assert e.status_code == 400
        assert "models" in e.detail
    else:
        raise AssertionError("Expected missing models object to be rejected")
    assert json.loads(pricing_path.read_text(encoding="utf-8")) == original


def test_startup_warmer_populates_initial_overview_date_range(monkeypatch):
    api._clear_cache()
    usage_calls = []
    stats_calls = []
    session_calls = []

    def fake_usage(period, date_from, date_to):
        usage_calls.append((period, date_from, date_to))
        return {"period": period, "date_from": date_from, "date_to": date_to}

    def fake_stats(year):
        stats_calls.append(year)
        return {"year": year}

    def fake_sessions(tool, period, date_from, date_to, *, include_review_sessions):
        session_calls.append((tool, period, date_from, date_to, include_review_sessions))
        return {"tool": tool, "period": period}

    monkeypatch.setattr(api, "compute_usage_with_comparison", fake_usage)
    monkeypatch.setattr(api, "compute_stats", fake_stats)
    monkeypatch.setattr(api, "get_sessions_data", fake_sessions)

    try:
        api._warm_caches()
        assert ("today", None, None) in usage_calls
        date_range_calls = [call for call in usage_calls if call[1] is not None or call[2] is not None]
        assert len(date_range_calls) == 1

        period, date_from, date_to = date_range_calls[0]
        assert period == "today"
        assert date_from == date_to
        assert api._pricing_cache_key(f"usage_today_{date_from}_{date_to}") in api._cache
        assert api._pricing_cache_key("stats_None") in api._cache
        assert stats_calls == [None]
        assert session_calls == [(tool, "today", None, None, None) for tool in api.SESSION_TOOLS]
        for tool in api.SESSION_TOOLS:
            assert api._pricing_cache_key(f"sessions_{tool}_today_None_None_None") in api._cache
    finally:
        api._clear_cache()
