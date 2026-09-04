from __future__ import annotations

from pathlib import Path

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
def no_background_warmers(monkeypatch):
    """Keep the lifespan's warm threads out of every test, on ANY code path.

    ``_lifespan`` has no shutdown side, so a thread it starts outlives the
    ``TestClient`` block that started it — the daily warmer then sleeps on with the
    test's monkeypatches long gone, and a suite run straddling the warm minute fires a
    real full-history ``_warm_previous_day()`` mid-run against whatever data dir is
    current. Both are daemons, so pytest still exits and the damage is silent.

    This lives in conftest rather than in one test file's fixture because any future
    ``with TestClient(api.app)`` inherits the same problem. Tests that want a warm call
    the warmers directly.
    """
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    monkeypatch.setenv("TOKDASH_DAILY_WARM", "0")


@pytest.fixture(autouse=True)
def hermetic_claude_installs(monkeypatch, tmp_path):
    """Point Claude Code's config dir at an empty dir that exists nowhere.

    With ``quota.credential_scan`` consent on, the quota readers open
    ``$CLAUDE_CONFIG_DIR/.credentials.json`` and every ``~/.claude*`` install on the
    machine. Tests that grant that consent to check dashboard plumbing would otherwise
    read the developer's real sign-in (and on macOS could raise a Keychain prompt), so a
    run would be both flaky and leaky. Tests that mean to exercise those readers point
    the paths somewhere themselves.

    ``$HOME`` is part of the surface, not just ``$CLAUDE_CONFIG_DIR``: the sibling scan
    globs ``Path.home()`` directly, so overriding only the env var would still hand a test
    the real ``~/.claude-academic`` beside it. Both the env var and the home directory are
    therefore redirected, to a home that contains no Claude directory at all.

    Redirecting the home is NOT what keeps the Keychain out, though: the default profile
    falls back to `_read_keychain_credentials()` whenever its credential file is missing,
    which on a macOS dev box is a `security find-generic-password` subprocess against the
    developer's real login keychain (and a possible permission prompt) on every consented
    test. The empty redirected home guarantees that fallthrough, so the platform check the
    reader gates on is disabled here as well. Tests that mean to exercise those readers
    point the paths somewhere themselves and restore the platform themselves.
    """
    from tokdash.sources.quota import claude as claude_quota

    home = tmp_path / "claude-free-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows' home, for parity
    monkeypatch.setattr(Path, "home", lambda: home, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "no-claude-config-dir"))
    monkeypatch.delenv("TOKDASH_CLAUDE_PROFILES", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claude_quota, "_is_macos", lambda: False)


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
      are marked ``opens_browser`` (registered in pyproject.toml); the marker
      lifts the backstop only — such a test still gets the kill switch, so
      forgetting to stub a sink cannot leak a real browser.
    """
    monkeypatch.setenv("TOKDASH_SETUP_NO_OPEN", "1")
    if "opens_browser" in request.keywords:
        return

    # Imported here, not at module scope: tokdash.cli pulls in the FastAPI app,
    # and conftest is loaded for every test file including those that never
    # touch it.
    from tokdash import cli
    from tokdash.onboard import engine

    def _no_open(*_args, **_kwargs):
        pytest.fail("test reached a real browser-open sink (xdg-open/webbrowser)")

    monkeypatch.setattr(engine, "_open_dashboard_url", _no_open)
    monkeypatch.setattr(cli, "_open_browser", _no_open)
