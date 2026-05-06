import plistlib

from tokdash.macos_native_app import build_dashboard_view_model
from tokdash.macos_app import create_macos_app_bundle


def test_create_macos_app_bundle(tmp_path):
    app_path = create_macos_app_bundle(tmp_path, force=True)

    assert app_path.name == "Tokdash.app"
    assert (app_path / "Contents" / "Info.plist").exists()

    info = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleName"] == "Tokdash"
    assert info["CFBundlePackageType"] == "APPL"

    executable = app_path / "Contents" / "MacOS" / info["CFBundleExecutable"]
    assert executable.exists()
    assert executable.stat().st_mode & 0o111
    assert (app_path / "Contents" / "Resources" / "python" / "tokdash" / "macos_native_app.py").exists()


def test_build_dashboard_view_model_formats_usage_data():
    view_model = build_dashboard_view_model(
        {
            "period": "today",
            "total_tokens": 1234567,
            "total_cost": 3.2,
            "total_messages": 42,
            "timestamp": "2026-05-06T10:30:00",
            "comparison": {"tokens_pct": 12.5, "cost_pct": -4.0, "messages_pct": None},
            "apps": {
                "codex": {"tokens": 1200000, "cost": 3.1, "messages": 40},
                "claude": {"tokens": 34567, "cost": 0.1, "messages": 2},
            },
            "combined_models": [
                {
                    "name": "gpt-5.5",
                    "tokens": 1234567,
                    "tokens_in": 200000,
                    "tokens_out": 34567,
                    "tokens_cache": 1000000,
                    "cost": 3.2,
                }
            ],
        }
    )

    assert view_model["metrics"][0] == {
        "label": "Total tokens",
        "value": "1.23M",
        "delta": "+12.5% vs previous",
    }
    assert view_model["metrics"][1] == {
        "label": "Estimated cost",
        "value": "$3.20",
        "delta": "-4.0% vs previous",
    }
    assert view_model["metrics"][2] == {"label": "Messages", "value": "42", "delta": "No previous data"}
    assert view_model["breakdown"][0]["name"] == "codex"
    assert view_model["models"][0] == {
        "name": "gpt-5.5",
        "tokens": "1.23M",
        "tokens_raw": 1234567,
        "input": "200.0K",
        "output": "34.6K",
        "cache": "1.00M",
        "cost": "$3.20",
    }
