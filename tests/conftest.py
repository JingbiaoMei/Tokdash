from __future__ import annotations

import pytest

from tokdash import cli
from tokdash.onboard import engine


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
def no_browser_open(request, monkeypatch):
    """Never let a test spawn a real browser, on ANY code path.

    The interactive setup tests fake a TTY (test_onboard._isolate), so on a
    machine with a display (e.g. WSLg, where DISPLAY=:0) `tokdash setup`'s
    optional dashboard-open would Popen a DETACHED xdg-open -> Chrome per test
    — a window the test process never owns and never closes. Unguarded, every
    pytest run leaked a generation of headful browsers onto the dev desktop
    (and, from a second checkout running the suite on a schedule, kept leaking
    on a ~25-minute cadence).

    Two layers:
    - TOKDASH_SETUP_NO_OPEN: the engine's supported kill switch for the setup
      path (see engine._maybe_open_dashboard); osinfo.has_display's
      PYTEST_CURRENT_TEST check covers the serve path.
    - Backstop: the two real browser-open sinks fail loudly if ANY test
      reaches them, so a future open path cannot silently reopen the leak
      without tripping this fixture. Tests that legitimately exercise a sink
      are marked ``opens_browser`` (registered in pyproject.toml) and are
      exempt.
    """
    if "opens_browser" in request.keywords:
        return
    monkeypatch.setenv("TOKDASH_SETUP_NO_OPEN", "1")

    def _no_open(*_args, **_kwargs):
        pytest.fail("test reached a real browser-open sink (xdg-open/webbrowser)")

    monkeypatch.setattr(engine, "_open_dashboard_url", _no_open)
    monkeypatch.setattr(cli, "_open_browser", _no_open)
