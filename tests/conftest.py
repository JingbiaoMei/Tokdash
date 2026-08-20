from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_usage_db(monkeypatch, tmp_path):
    """Keep the default-on persistent usage DB isolated per test.

    The runtime default is intentionally controlled by application code, but
    tests must not share ~/.tokdash/usage.sqlite3 or one test's cached rows can
    leak into another source fixture.
    """
    data_dir = tmp_path / ".tokdash-test"
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TOKDASH_USAGE_DB_PATH", str(data_dir / "usage.sqlite3"))


@pytest.fixture(autouse=True)
def no_setup_browser_open(monkeypatch):
    """Never let a test spawn a real browser from `tokdash setup`.

    The interactive setup tests fake a TTY (test_onboard._isolate), so on a
    machine with a display (e.g. WSLg, where DISPLAY=:0) `tokdash setup`'s
    optional dashboard-open would Popen a DETACHED xdg-open -> Chrome per test
    — a window the test process never owns and never closes. Unguarded, every
    pytest run leaked a generation of headful browsers onto the dev desktop.
    TOKDASH_SETUP_NO_OPEN is the engine's supported kill switch for exactly
    this (see engine._maybe_open_dashboard). The serve path's auto-open is
    guarded separately: osinfo.has_display's PYTEST_CURRENT_TEST check, pinned
    by test_cli_serve.test_serve_never_arms_browser_timer_under_pytest.
    """
    monkeypatch.setenv("TOKDASH_SETUP_NO_OPEN", "1")
