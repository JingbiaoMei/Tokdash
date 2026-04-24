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
    reload_calls = []
    monkeypatch.setattr(api, "reload_pricing_db", lambda: reload_calls.append(True))
    api._cache["stale"] = (0.0, {"old": True})

    read_response = api.get_pricing_db()
    assert read_response["data"] == original
    assert read_response["text"] == json.dumps(original, indent=2, ensure_ascii=False) + "\n"

    save_response = api.update_pricing_db({"text": json.dumps(updated)})
    assert save_response["data"] == updated
    assert json.loads(pricing_path.read_text(encoding="utf-8")) == updated
    assert api._cache == {}
    assert reload_calls == [True]


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
