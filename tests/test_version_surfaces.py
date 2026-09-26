"""Phase 0a onboarding prerequisites: version surfaces, /health fingerprint, python -m entrypoint."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import tokdash.api as api
import tokdash.cli as cli
from tokdash import __version__


# --- CLI: `tokdash version` and `tokdash --version` -------------------------------

def test_version_command_parses():
    args = cli.build_parser("tokdash").parse_args(["version"])
    assert args.command == "version"


def test_version_command_prints(capsys):
    rc = cli.cli(["version"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == f"tokdash {__version__}"


def test_version_flag_exits_zero(capsys):
    # argparse's `version` action prints and raises SystemExit(0) before any subcommand.
    with pytest.raises(SystemExit) as exc:
        cli.build_parser("tokdash").parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_existing_verbs_still_parse():
    # Parser-compatibility contract (plan §7.1): lifecycle additions must not break these.
    # A bare `tokdash` parses to command=None (round 4): no verb means help, not serve.
    p = cli.build_parser("tokdash")
    assert p.parse_args([]).command is None
    assert p.parse_args(["export", "--json"]).json is True
    assert p.parse_args(["db", "repair", "--dry-run"]).dry_run is True


def test_bare_command_prints_help_and_does_not_serve(capsys, monkeypatch):
    # Bare `tokdash` used to fall through to `serve` and open a browser. It is now
    # a no-op that shows help. Autostart is safe: every service writer embeds
    # "serve" explicitly, so nothing unattended relied on the old default.
    def _no_serve(*args, **kwargs):
        pytest.fail("bare `tokdash` reached serve() -- it must print help instead")

    monkeypatch.setattr(cli, "serve", _no_serve)
    assert cli.cli([]) == 0
    out = capsys.readouterr().out
    assert "usage: tokdash" in out
    assert "show help" in out  # the choices help line advertises the new default
    # Global flags with no verb are still no verb: help, not serve.
    assert cli.cli(["--no-open"]) == 0
    assert "usage: tokdash" in capsys.readouterr().out


# --- API: /health fingerprint and /api/version -----------------------------------

def test_health_carries_fingerprint():
    body = asyncio.run(api.health_check())
    assert body["status"] == "ok"
    assert body["service"] == "tokdash"
    assert body["version"] == __version__


def test_api_version_reports_runtime():
    body = asyncio.run(api.get_version())
    assert body["service"] == "tokdash"
    assert body["runtime_version"] == __version__
    assert body["install_method"] is None  # no manifest in the isolated test data dir
    assert body["update_check_enabled"] is False


def test_api_version_reads_install_manifest(monkeypatch, tmp_path):
    data_dir = tmp_path / "dd"
    data_dir.mkdir()
    (data_dir / "install.json").write_text('{"install_method": "managed-venv"}', encoding="utf-8")
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    body = asyncio.run(api.get_version())
    assert body["install_method"] == "managed-venv"


@pytest.mark.parametrize("content", ["{not json", "[1,2,3]", '"juststring"', "42"])
def test_api_version_survives_bad_manifest(monkeypatch, tmp_path, content):
    # Both malformed JSON AND valid-JSON-non-dict (list/str/int) must not raise (HTTP 500).
    data_dir = tmp_path / "dd"
    data_dir.mkdir()
    (data_dir / "install.json").write_text(content, encoding="utf-8")
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    body = asyncio.run(api.get_version())  # must not raise
    assert body["install_method"] is None


# --- opt-in update check (Phase 3b) ----------------------------------------------

def test_api_version_reflects_update_check_flag(monkeypatch):
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", "1")
    assert asyncio.run(api.get_version())["update_check_enabled"] is True
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", "0")
    assert asyncio.run(api.get_version())["update_check_enabled"] is False


def test_update_check_endpoint_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    body = asyncio.run(api.run_update_check())
    assert body["enabled"] is False and body["update_available"] is False


def test_update_check_endpoint_reports_when_enabled(monkeypatch):
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", "1")
    from tokdash.onboard import updatecheck

    monkeypatch.setattr(
        updatecheck, "check",
        lambda v, **k: {"current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False},
    )
    body = asyncio.run(api.run_update_check())
    assert body["enabled"] is True and body["update_available"] is True


def test_update_check_consent_persists(monkeypatch):
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    from tokdash.onboard import updatecheck

    assert updatecheck.is_enabled() is False
    body = asyncio.run(api.update_check_consent())
    assert body["enabled"] is True and updatecheck.is_enabled() is True


# --- Entrypoint: `python -m tokdash` ---------------------------------------------

def test_python_dash_m_version_runs():
    """`python -m tokdash --version` must work (collision-proof runtime entrypoint)."""
    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    res = subprocess.run(
        [sys.executable, "-m", "tokdash", "--version"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
    )
    assert res.returncode == 0, res.stderr
    assert __version__ in res.stdout
