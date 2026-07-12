"""Phase 1 onboarding engine: setup / doctor / uninstall, manifest, systemd, ownership.

Covers the plan §19 testing matrix without touching the real system: systemd calls and
managed-venv creation are monkeypatched, the port probe is stubbed, and all state is
redirected under a throwaway TOKDASH_DATA_DIR / XDG_CONFIG_HOME.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from tokdash import cli
from tokdash.onboard import detect, engine, launchd, manifest, paths, plan, runtime, systemd, tailscale, updatecheck
from tokdash.onboard.engine import run_lifecycle


# --- harness --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect every onboarding path into tmp and stub the OS-touching probes."""
    data_dir = tmp_path / "dd"
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    # Deterministic, offline detection by default; individual tests override.
    monkeypatch.setattr(detect, "probe_port", lambda port=55423, *a, **k: {"port": port, "open": False, "is_tokdash": False, "version": None})
    monkeypatch.setattr(detect, "os_kind", lambda: "linux")
    monkeypatch.setattr(detect, "is_tty", lambda: True)
    monkeypatch.setattr(detect, "systemd_user_available", lambda: True)
    monkeypatch.setattr(detect, "launchd_available", lambda: False)
    monkeypatch.setattr(detect, "winsched_available", lambda: False)
    updatecheck._cache.update({"ts": 0.0, "data": None})  # no cross-test cache leak
    yield
    updatecheck._cache.update({"ts": 0.0, "data": None})


def _ok_proc():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


@pytest.fixture
def fake_systemd(monkeypatch):
    """Make systemctl calls no-ops that report a healthy, enabled+active service."""
    monkeypatch.setattr(systemd, "daemon_reload", lambda: None)
    monkeypatch.setattr(systemd, "enable_now", lambda name="tokdash": _ok_proc())
    monkeypatch.setattr(systemd, "disable_now", lambda name="tokdash": _ok_proc())
    monkeypatch.setattr(systemd, "restart", lambda name="tokdash": _ok_proc())
    monkeypatch.setattr(systemd, "is_enabled", lambda name="tokdash": True)
    monkeypatch.setattr(systemd, "is_active", lambda name="tokdash": True)
    monkeypatch.setattr(systemd, "is_active_strict", lambda name="tokdash": True)
    monkeypatch.setattr(systemd, "fragment_path", lambda name="tokdash": str(paths.systemd_unit_path()))
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {"ok": True, "port": {"port": port, "open": True, "is_tokdash": True, "version": "test"}},
    )


@pytest.fixture
def macos(monkeypatch, tmp_path):
    """Pretend we're on macOS with launchd available; redirect the plist into tmp."""
    monkeypatch.setattr(detect, "os_kind", lambda: "macos")
    monkeypatch.setattr(detect, "launchd_available", lambda: True)
    monkeypatch.setattr(detect, "systemd_user_available", lambda: False)
    plist = tmp_path / "LaunchAgents" / "com.tokdash.tokdash.plist"
    monkeypatch.setattr(paths, "launchd_plist_path", lambda: plist)


@pytest.fixture
def fake_launchd(monkeypatch):
    """Make launchctl calls no-ops that report a loaded agent."""
    monkeypatch.setattr(launchd, "bootstrap", lambda plist: _ok_proc())
    monkeypatch.setattr(launchd, "bootout", lambda: _ok_proc())
    monkeypatch.setattr(launchd, "kickstart", lambda: _ok_proc())
    monkeypatch.setattr(launchd, "is_loaded", lambda: True)
    monkeypatch.setattr(launchd, "is_loaded_strict", lambda: True)
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {"ok": True, "port": {"port": port, "open": True, "is_tokdash": True, "version": "test"}},
    )


def run(argv):
    args = cli.build_parser("tokdash").parse_args(argv)
    return run_lifecycle(args)


def run_json(argv, capsys):
    capsys.readouterr()  # flush anything an earlier setup/run printed
    rc = run(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


# --- paths / manifest -----------------------------------------------------------


def test_paths_follow_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "x"))
    monkeypatch.setattr(paths, "_windows_venv_layout", lambda: False)
    assert paths.manifest_path() == tmp_path / "x" / "install.json"
    assert paths.managed_venv_python() == tmp_path / "x" / "runtime" / "python-venv" / "bin" / "python"
    assert not paths.is_default_data_dir()


def test_probe_port_fail_closed_on_socket_error(monkeypatch):
    # A restricted sandbox can make socket creation itself raise; probe must fail closed
    # (port "not open") so `setup --dry-run` still produces a plan instead of crashing.
    def boom(*a, **k):
        raise PermissionError("sandbox")

    monkeypatch.setattr(detect.socket, "socket", boom)
    info = detect.probe_port(55423)
    assert info["open"] is False and info["is_tokdash"] is False


def test_manifest_round_trip():
    man = manifest.build_manifest(
        install_method="managed-venv", runtime_kind="venv",
        runtime_command=["/p/python", "-m", "tokdash"], runtime_owned_by_setup=True,
        python_path="/p/python", python_version="3.12.0", service=None,
        runtime_marker=str(paths.runtime_marker_path()), data_dir=str(paths.data_dir()),
        bind="127.0.0.1", port=55423,
    )
    manifest.write_manifest(man)
    back = manifest.read_manifest()
    assert back["runtime_owned_by_setup"] is True
    assert back["schema"] == manifest.SCHEMA


def test_manifest_bad_file_returns_none():
    p = paths.manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert manifest.read_manifest() is None


# --- systemd unit rendering -----------------------------------------------------


def test_unit_carries_marker_and_execstart():
    text = systemd.render_unit(["/v/bin/python", "-m", "tokdash"], "127.0.0.1", 55423, marker_id="abc123")
    assert "X-Tokdash-Managed id=abc123" in text
    assert "ExecStart=/v/bin/python -m tokdash serve --bind 127.0.0.1 --port 55423 --no-open" in text
    assert "Environment=TOKDASH_DATA_DIR" not in text  # default data dir => no env line


def test_unit_env_line_when_non_default_data_dir():
    text = systemd.render_unit(["py", "-m", "tokdash"], "127.0.0.1", 55423, marker_id="x", env_data_dir="/custom/dd")
    assert "Environment=TOKDASH_DATA_DIR=/custom/dd" in text


def test_unit_quotes_paths_with_spaces():
    # systemd splits ExecStart on whitespace; a venv/data path with spaces must be quoted.
    text = systemd.render_unit(
        ["/opt/my venv/bin/python", "-m", "tokdash"], "127.0.0.1", 55423,
        marker_id="x", env_data_dir="/data dir/dd",
    )
    assert 'ExecStart="/opt/my venv/bin/python" -m tokdash serve --bind 127.0.0.1 --port 55423 --no-open' in text
    assert 'Environment="TOKDASH_DATA_DIR=/data dir/dd"' in text


def test_unit_is_managed_detection(tmp_path):
    unit = tmp_path / "tokdash.service"
    unit.write_text(systemd.render_unit(["py", "-m", "tokdash"], "127.0.0.1", 1, marker_id="deadbeef"), encoding="utf-8")
    assert systemd.unit_is_managed(unit) is True
    assert systemd.unit_is_managed(unit, "deadbeef") is True
    assert systemd.unit_is_managed(unit, "other") is False
    unmarked = tmp_path / "manual.service"
    unmarked.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    assert systemd.unit_is_managed(unmarked) is False


def test_systemd_lifecycle_commands_allow_service_manager_timeout(monkeypatch):
    seen = []

    def fake_run(args, timeout=20):
        seen.append((args, timeout))
        return _ok_proc()

    monkeypatch.setattr(systemd, "_run", fake_run)
    systemd.enable_now()
    systemd.restart()
    systemd.disable_now()
    assert seen == [
        (["enable", "--now", "tokdash"], systemd.LIFECYCLE_TIMEOUT),
        (["restart", "tokdash"], systemd.LIFECYCLE_TIMEOUT),
        (["disable", "--now", "tokdash"], systemd.LIFECYCLE_TIMEOUT),
    ]


# --- runtime resolution (ownership matrix §13.1) --------------------------------


def _detection(**over):
    d = detect.detect_all(55423)
    d.update(over)
    return d


def test_runtime_existing_not_owned():
    rt = runtime.resolve("existing", _detection())
    assert rt["owned_by_setup"] is False and rt["needs_create"] is False
    assert rt["command"][-2:] == ["-m", "tokdash"]


def test_runtime_venv_is_owned():
    rt = runtime.resolve("venv", _detection())
    assert rt["owned_by_setup"] is True
    assert rt["kind"] == "venv" and rt["install_method"] == "managed-venv"


def test_runtime_pipx_requires_existing_pipx():
    rt = runtime.resolve("pipx", _detection(pipx_tokdash=None))
    assert rt["error"] and "pipx install" in rt["error"]
    rt2 = runtime.resolve("pipx", _detection(pipx_tokdash="/p/pipx/venvs/tokdash/bin/python"))
    assert rt2["owned_by_setup"] is False and rt2["kind"] == "pipx"


def test_runtime_binary_deferred():
    assert "not available yet" in runtime.resolve("binary", _detection())["error"]


# --- setup planning -------------------------------------------------------------


def test_auto_refuses_non_loopback_bind():
    opts = plan.Options(auto=True, bind="0.0.0.0")
    p = plan.build_setup_plan(opts, _detection())
    assert p["ok"] is False and any("non-loopback" in b for b in p["blockers"])


def test_interactive_non_loopback_warns():
    opts = plan.Options(auto=False, bind="0.0.0.0")
    p = plan.build_setup_plan(opts, _detection())
    assert p["ok"] is True and any("UNAUTHENTICATED" in w for w in p["warnings"])


def test_blocked_plan_separates_changes(monkeypatch):
    # When blocked, would-run actions move to blocked_changes; `changes` is empty so a
    # bundler reading the JSON never mistakes "would do" for "will do".
    monkeypatch.setattr(detect, "probe_port", lambda *a, **k: {"port": 55423, "open": False, "is_tokdash": False, "version": None})
    p = plan.build_setup_plan(plan.Options(auto=True, bind="0.0.0.0"), _detection())
    assert p["ok"] is False
    assert p["changes"] == [] and p["blocked_changes"]


def test_busy_tokdash_port_is_reused(monkeypatch):
    monkeypatch.setattr(detect, "probe_port", lambda *a, **k: {"port": 55423, "open": True, "is_tokdash": True, "version": "0.6.2"})
    p = plan.build_setup_plan(plan.Options(auto=True), _detection())
    assert p["port"] == 55423 and any("already serves Tokdash" in n for n in p["notes"])


def test_systemd_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(detect, "systemd_user_available", lambda: False)
    p = plan.build_setup_plan(plan.Options(auto=True), _detection(systemd_user=False))
    assert p["service"]["type"] == "none"


# --- setup apply (dry-run, guards, real) ----------------------------------------


def test_setup_auto_dry_run_changes_nothing(capsys):
    rc, payload = run_json(["setup", "--auto", "--no-service", "--dry-run", "--json"], capsys)
    assert rc == 0 and payload["dry_run"] is True
    assert not paths.manifest_path().exists()


def test_setup_non_tty_without_auto_does_not_mutate(monkeypatch, capsys):
    monkeypatch.setattr(detect, "is_tty", lambda: False)
    rc = run(["setup", "--no-service"])
    assert rc == 2
    assert not paths.manifest_path().exists()


def test_setup_auto_no_service_writes_manifest(capsys):
    rc, payload = run_json(["setup", "--auto", "--no-service", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True
    man = manifest.read_manifest()
    assert man["runtime_owned_by_setup"] is False and man["service"] is None


def test_setup_auto_systemd_writes_unit_and_manifest(fake_systemd, capsys):
    rc, payload = run_json(["setup", "--auto", "--service", "systemd", "--json"], capsys)
    assert rc == 0 and payload["service"]["type"] == "systemd-user"
    unit = paths.systemd_unit_path()
    assert unit.is_file() and "X-Tokdash-Managed" in unit.read_text(encoding="utf-8")
    man = manifest.read_manifest()
    assert man["service"]["created_by_setup"] is True
    assert "service:systemd-user" in payload["changed"]


def test_setup_systemd_fails_if_loaded_unit_is_different(fake_systemd, monkeypatch, capsys, tmp_path):
    # systemctl operates by unit name. If an existing tokdash.service is already loaded from
    # another path, setup must not report success for the newly-written unit.
    loaded = tmp_path / "real" / "tokdash.service"
    monkeypatch.setattr(systemd, "fragment_path", lambda name="tokdash": str(loaded))
    rc, payload = run_json(["setup", "--auto", "--service", "systemd", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert str(loaded) in payload["error"]
    assert paths.manifest_path().exists()  # cleanup can still be driven by uninstall


def test_setup_systemd_fails_if_service_never_answers(fake_systemd, monkeypatch, capsys):
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {
            "ok": False,
            "error": "service did not become ready: nothing answered on 127.0.0.1:55423",
            "port": {"port": port, "open": False, "is_tokdash": False, "version": None},
        },
    )
    rc, payload = run_json(["setup", "--auto", "--service", "systemd", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "did not become ready" in payload["error"]
    assert paths.manifest_path().exists()


def test_setup_refuses_existing_unmarked_unit(fake_systemd):
    # A hand-installed tokdash.service (no marker) must not be silently overwritten.
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["setup", "--auto", "--service", "systemd"])
    assert rc == 1
    assert "X-Tokdash-Managed" not in unit.read_text(encoding="utf-8")
    assert not paths.manifest_path().exists()


def test_setup_force_overwrites_unmarked_unit(fake_systemd):
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["setup", "--auto", "--service", "systemd", "--force"])
    assert rc == 0 and "X-Tokdash-Managed" in unit.read_text(encoding="utf-8")


def test_setup_systemd_restarts_after_writing_unit(fake_systemd, monkeypatch):
    restarted = {}
    monkeypatch.setattr(systemd, "restart", lambda name="tokdash": restarted.setdefault("proc", _ok_proc()))
    rc = run(["setup", "--auto", "--service", "systemd"])
    assert rc == 0 and restarted.get("proc") is not None


def test_setup_systemd_restart_timeout_succeeds_when_service_is_ready(fake_systemd, monkeypatch, capsys):
    def slow_restart(name="tokdash"):
        raise subprocess.TimeoutExpired(["systemctl", "--user", "restart", name], 20)

    monkeypatch.setattr(systemd, "restart", slow_restart)
    rc, payload = run_json(["setup", "--auto", "--service", "systemd", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True
    assert "systemctl restart timed out" in payload["service"]["restart_error"]
    assert payload["readiness"]["ok"] is True
    assert paths.manifest_path().exists()


def test_setup_systemd_restart_timeout_fails_closed_when_not_ready(fake_systemd, monkeypatch, capsys):
    def slow_restart(name="tokdash"):
        raise subprocess.TimeoutExpired(["systemctl", "--user", "restart", name], 20)

    monkeypatch.setattr(systemd, "restart", slow_restart)
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {
            "ok": False,
            "error": "service did not become ready: nothing answered on 127.0.0.1:55423",
            "port": {"port": port, "open": False, "is_tokdash": False, "version": None},
        },
    )
    rc, payload = run_json(["setup", "--auto", "--service", "systemd", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "did not become ready" in payload["error"]
    assert "systemctl restart timed out" in payload["service"]["restart_error"]
    assert paths.manifest_path().exists()


def test_setup_open_dashboard_uses_detached_opener(monkeypatch):
    calls = []

    def fake_which(name):
        return f"/usr/bin/{name}" if name == "xdg-open" else None

    class FakeProcess:
        pass

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProcess()

    monkeypatch.setattr(engine.sys, "platform", "linux")
    monkeypatch.setattr(engine.os, "name", "posix")
    monkeypatch.setattr(detect, "os_kind", lambda: "linux")
    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine.subprocess, "Popen", fake_popen)

    assert engine._open_dashboard_url("http://127.0.0.1:55423") is True
    cmd, kwargs = calls[0]
    assert cmd == ["xdg-open", "http://127.0.0.1:55423"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_setup_open_dashboard_records_note(monkeypatch):
    monkeypatch.setattr(engine, "_has_display", lambda: True)
    monkeypatch.setattr(engine, "_open_dashboard_url", lambda url: True)
    result = {"ok": True, "url": "http://127.0.0.1:55423"}

    assert engine._maybe_open_dashboard(result, plan.Options(), {"tty": True}) is True
    assert result["opened_url"] == "http://127.0.0.1:55423"
    assert result["notes"] == ["Opened dashboard in your browser: http://127.0.0.1:55423"]


def test_setup_opens_dashboard_after_quota_wizard(monkeypatch, fake_systemd):
    calls = []

    monkeypatch.setattr(engine, "_print_setup_human_plan", lambda p: None)
    monkeypatch.setattr(engine, "_confirm", lambda prompt, default=True: True)
    monkeypatch.setattr(engine, "_offer_tailscale", lambda result: None)
    monkeypatch.setattr(engine, "_quota_setup_wizard", lambda: calls.append("quota"))
    monkeypatch.setattr(engine, "_maybe_open_dashboard", lambda result, opts, detection: calls.append("open") or True)

    assert run(["setup", "--service", "systemd"]) == 0
    assert calls == ["quota", "open"]


# --- update-check setup step -----------------------------------------------------


def test_setup_update_check_prompt_accepted_enables(monkeypatch, fake_systemd):
    # Bare-Enter / accepted (default=True) prompt -> updatecheck.enable() is called and
    # is_enabled() flips True, using the isolated config dir from the autouse fixture.
    monkeypatch.setattr(engine, "_print_setup_human_plan", lambda p: None)
    monkeypatch.setattr(engine, "_confirm", lambda prompt, default=True: True)
    monkeypatch.setattr(engine, "_offer_tailscale", lambda result: None)
    monkeypatch.setattr(engine, "_quota_setup_wizard", lambda: None)
    monkeypatch.setattr(engine, "_maybe_open_dashboard", lambda result, opts, detection: True)

    assert updatecheck.is_enabled() is False
    assert run(["setup", "--service", "systemd"]) == 0
    assert updatecheck.is_enabled() is True


def test_setup_update_check_prompt_declined_stays_disabled(monkeypatch, fake_systemd):
    def fake_confirm(prompt, default=True):
        if "update notices" in prompt.lower():
            return False
        return True

    monkeypatch.setattr(engine, "_print_setup_human_plan", lambda p: None)
    monkeypatch.setattr(engine, "_confirm", fake_confirm)
    monkeypatch.setattr(engine, "_offer_tailscale", lambda result: None)
    monkeypatch.setattr(engine, "_quota_setup_wizard", lambda: None)
    monkeypatch.setattr(engine, "_maybe_open_dashboard", lambda result, opts, detection: True)

    assert run(["setup", "--service", "systemd"]) == 0
    assert updatecheck.is_enabled() is False


def test_setup_yes_skips_update_check_prompt(monkeypatch):
    # --yes is the non-interactive signal: scripted/CI setups must never block on this
    # prompt or silently enable a network-calling feature, so the step function itself
    # must not even be invoked (not just "prompt answered with a default").
    calls = []
    monkeypatch.setattr(engine, "_update_check_setup_step", lambda: calls.append("update_check"))
    monkeypatch.setattr(engine, "_quota_setup_wizard", lambda: calls.append("quota"))

    assert run(["setup", "--yes", "--no-service"]) == 0
    assert calls == []
    assert updatecheck.is_enabled() is False


@pytest.mark.parametrize("kill_value", ["0", "off", "false", "no", "OFF", "False"])
def test_setup_update_check_kill_switch_skips_prompt(monkeypatch, fake_systemd, kill_value):
    # TOKDASH_UPDATE_CHECK's disabling forms (0/false/no/off, case-insensitive) are a hard
    # kill switch: even in an otherwise fully interactive setup, offering to enable something
    # the env is force-disabling would be misleading, so the prompt must not appear at all.
    # Regression: the step once matched only the literal "0", so off/false/no slipped through
    # and could persist dormant consent that silently activated once the env var was removed.
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", kill_value)
    prompts = []

    def fake_confirm(prompt, default=True):
        prompts.append(prompt)
        return True

    monkeypatch.setattr(engine, "_print_setup_human_plan", lambda p: None)
    monkeypatch.setattr(engine, "_confirm", fake_confirm)
    monkeypatch.setattr(engine, "_offer_tailscale", lambda result: None)
    monkeypatch.setattr(engine, "_quota_setup_wizard", lambda: None)
    monkeypatch.setattr(engine, "_maybe_open_dashboard", lambda result, opts, detection: True)

    assert run(["setup", "--service", "systemd"]) == 0
    assert not any("update notices" in p.lower() for p in prompts)
    assert updatecheck.is_enabled() is False


def test_setup_overwrites_its_own_marked_unit(fake_systemd):
    # Re-running setup is idempotent: an existing *marked* unit is replaced without --force.
    assert run(["setup", "--auto", "--service", "systemd"]) == 0
    assert run(["setup", "--auto", "--service", "systemd"]) == 0
    assert paths.systemd_unit_path().is_file()


def test_setup_venv_creates_managed_runtime(monkeypatch, fake_systemd, capsys):
    created = {}

    def fake_create(builder_python=None):
        paths.runtime_dir().mkdir(parents=True, exist_ok=True)
        paths.runtime_marker_path().write_text("created-by=tokdash-setup\n", encoding="utf-8")
        created["yes"] = True
        return str(paths.managed_venv_python())

    monkeypatch.setattr(runtime, "create_managed_venv", fake_create)
    rc, payload = run_json(["setup", "--auto", "--runtime", "venv", "--service", "systemd", "--json"], capsys)
    assert rc == 0 and created.get("yes")
    man = manifest.read_manifest()
    assert man["runtime_owned_by_setup"] is True
    assert man["runtime_marker"] == str(paths.runtime_marker_path())


# --- doctor ---------------------------------------------------------------------


def test_doctor_reports_no_manifest(capsys):
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert payload["manifest_present"] is False and payload["install_method"] is None
    assert rc == 0 and payload["ok"] is True


def test_doctor_reports_tokdash_version(capsys):
    from tokdash import __version__

    rc, payload = run_json(["doctor", "--json"], capsys)
    assert rc == 0 and payload["version"] == __version__


def test_doctor_flags_manual_unit_without_manifest(fake_systemd, capsys):
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve --port 55423\n", encoding="utf-8")
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert any("no Tokdash setup manifest" in i and "--force" in i for i in payload["issues"])


def test_doctor_flags_prefingerprint_service_without_manifest(fake_systemd, monkeypatch, capsys):
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve --port 55423\n", encoding="utf-8")
    monkeypatch.setattr(
        detect,
        "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": False, "version": None},
    )
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert any("does not expose Tokdash's /health fingerprint" in i for i in payload["issues"])


def test_doctor_probes_manifest_port(monkeypatch, capsys):
    # After setup auto-picks a port, `doctor` (no --port) must diagnose that port.
    man = manifest.build_manifest(
        install_method="existing", runtime_kind="existing", runtime_command=["py", "-m", "tokdash"],
        runtime_owned_by_setup=False, python_path="py", python_version="3.11", service=None,
        runtime_marker=None, data_dir=str(paths.data_dir()), bind="127.0.0.1", port=55999,
    )
    manifest.write_manifest(man)
    seen = {}
    real = detect.detect_all

    def wrapper(port):
        seen["port"] = port
        return real(port)

    monkeypatch.setattr(detect, "detect_all", wrapper)
    run(["doctor", "--json"])
    assert seen["port"] == 55999


def test_doctor_probes_manifest_bind(monkeypatch, capsys):
    man = manifest.build_manifest(
        install_method="existing", runtime_kind="existing", runtime_command=["py", "-m", "tokdash"],
        runtime_owned_by_setup=False, python_path="py", python_version="3.11", service=None,
        runtime_marker=None, data_dir=str(paths.data_dir()), bind="192.0.2.10", port=55999,
    )
    manifest.write_manifest(man)
    calls = []

    def fake_probe(port=55423, host="127.0.0.1", *a, **k):
        calls.append((port, host))
        return {"port": port, "open": False, "is_tokdash": False, "version": None}

    monkeypatch.setattr(detect, "probe_port", fake_probe)
    run(["doctor", "--json"])
    assert (55999, "192.0.2.10") in calls


def test_doctor_flags_data_dir_mismatch(capsys):
    man = manifest.build_manifest(
        install_method="existing", runtime_kind="existing", runtime_command=["py", "-m", "tokdash"],
        runtime_owned_by_setup=False, python_path="py", python_version="3.11", service=None,
        runtime_marker=None, data_dir="/somewhere/else", bind="127.0.0.1", port=55423,
    )
    manifest.write_manifest(man)
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert rc == 1 and any("data_dir" in i for i in payload["issues"])


def test_doctor_flags_active_service_without_tokdash_port(fake_systemd, capsys):
    run(["setup", "--auto", "--service", "systemd"])
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert any("not answering" in i for i in payload["issues"])


def test_doctor_flags_systemd_fragment_mismatch(fake_systemd, monkeypatch, capsys, tmp_path):
    run(["setup", "--auto", "--service", "systemd"])
    loaded = tmp_path / "real" / "tokdash.service"
    monkeypatch.setattr(systemd, "fragment_path", lambda name="tokdash": str(loaded))
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert any(str(loaded) in i and "manifest unit" in i for i in payload["issues"])


# --- uninstall ------------------------------------------------------------------


def test_uninstall_keeps_not_owned_runtime_and_data(fake_systemd, capsys):
    run(["setup", "--auto", "--service", "systemd"])
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "service" in payload["changed"] and "manifest" in payload["changed"]
    assert not paths.manifest_path().exists()
    assert not paths.systemd_unit_path().exists()
    assert any("installed yourself" in k for k in payload["kept"])


def test_uninstall_systemd_fragment_mismatch_does_not_disable_foreign_service(fake_systemd, monkeypatch, capsys, tmp_path):
    run(["setup", "--auto", "--service", "systemd"])
    loaded = tmp_path / "real" / "tokdash.service"
    monkeypatch.setattr(systemd, "fragment_path", lambda name="tokdash": str(loaded))
    called = {}

    def fail_if_called(name="tokdash"):
        called["disable"] = name
        return subprocess.CompletedProcess([], 1, "", "should not be called")

    monkeypatch.setattr(systemd, "disable_now", fail_if_called)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True
    assert "disable" not in called
    assert not paths.systemd_unit_path().exists()
    assert not paths.manifest_path().exists()


def test_uninstall_removes_setup_owned_venv(monkeypatch, fake_systemd, capsys):
    def fake_create(builder_python=None):
        paths.managed_venv_python().parent.mkdir(parents=True, exist_ok=True)
        paths.managed_venv_python().write_text("#!/bin/sh\n", encoding="utf-8")
        paths.runtime_marker_path().write_text("created-by=tokdash-setup\n", encoding="utf-8")
        return str(paths.managed_venv_python())

    monkeypatch.setattr(runtime, "create_managed_venv", fake_create)
    run(["setup", "--auto", "--runtime", "venv", "--service", "systemd"])
    assert paths.runtime_dir().is_dir()
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "runtime" in payload["changed"]
    assert not paths.runtime_dir().exists()


def test_uninstall_keep_runtime_flag(monkeypatch, fake_systemd, capsys):
    def fake_create(builder_python=None):
        paths.managed_venv_python().parent.mkdir(parents=True, exist_ok=True)
        paths.managed_venv_python().write_text("#!/bin/sh\n", encoding="utf-8")
        paths.runtime_marker_path().write_text("x\n", encoding="utf-8")
        return str(paths.managed_venv_python())

    monkeypatch.setattr(runtime, "create_managed_venv", fake_create)
    run(["setup", "--auto", "--runtime", "venv", "--service", "systemd"])
    rc, payload = run_json(["uninstall", "--auto", "--keep-runtime", "--json"], capsys)
    assert rc == 0 and "runtime" not in payload["changed"]
    assert paths.runtime_dir().is_dir()


def test_uninstall_purge_deletes_data(fake_systemd):
    run(["setup", "--auto", "--no-service"])
    db = paths.usage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        (db.parent / (db.name + suffix)).write_text("x", encoding="utf-8")
    paths.config_path().write_text("{}", encoding="utf-8")
    rc = run(["uninstall", "--auto", "--purge"])
    assert rc == 0
    assert not db.exists() and not paths.config_path().exists()
    assert not (db.parent / (db.name + "-wal")).exists()


def test_uninstall_dry_run_changes_nothing(fake_systemd, capsys):
    run(["setup", "--auto", "--service", "systemd"])
    rc = run(["uninstall", "--dry-run"])
    assert rc == 0
    assert paths.manifest_path().exists() and paths.systemd_unit_path().exists()


def test_uninstall_refuses_unmarked_unit_without_manifest(capsys):
    # A manually-installed unit (no marker) and no manifest must NOT be removed.
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["uninstall", "--auto"])
    assert rc == 1 and unit.exists()


def test_uninstall_adopts_unmarked_unit_with_force(fake_systemd):
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["uninstall", "--auto", "--force"])
    assert rc == 0 and not unit.exists()


def test_uninstall_marked_unit_without_manifest(fake_systemd):
    # Manifest gone but the unit carries our marker => safe to remove.
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(systemd.render_unit(["py", "-m", "tokdash"], "127.0.0.1", 1, marker_id="abc"), encoding="utf-8")
    rc = run(["uninstall", "--auto"])
    assert rc == 0 and not unit.exists()


def test_uninstall_refuses_manifest_unit_replaced_by_manual(fake_systemd):
    # setup wrote a marked unit + manifest; the user then replaced the unit with a manual
    # one at the same path. uninstall must NOT trust the manifest and delete the manual unit.
    run(["setup", "--auto", "--service", "systemd"])
    unit = paths.systemd_unit_path()
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["uninstall", "--auto"])
    assert rc == 1 and unit.exists()
    assert "X-Tokdash-Managed" not in unit.read_text(encoding="utf-8")


def test_uninstall_force_removes_replaced_unit(fake_systemd):
    run(["setup", "--auto", "--service", "systemd"])
    unit = paths.systemd_unit_path()
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["uninstall", "--auto", "--force"])
    assert rc == 0 and not unit.exists()


def test_uninstall_removes_orphaned_marked_runtime_without_manifest(fake_systemd, capsys):
    # A `--runtime venv` setup that crashed after building the venv+marker but before
    # writing the manifest must still be cleanable (§12.2/§12.3 partial-safe).
    paths.managed_venv_python().parent.mkdir(parents=True, exist_ok=True)
    paths.managed_venv_python().write_text("#!/bin/sh\n", encoding="utf-8")
    paths.runtime_marker_path().write_text("created-by=tokdash-setup\n", encoding="utf-8")
    assert manifest.read_manifest() is None and detect.managed_runtime_present()
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "runtime" in payload["changed"]
    assert not paths.runtime_dir().exists()


# --- uninstall interactive wizard (§12 two decisions) ---------------------------


def test_uninstall_wizard_keeps_data_by_default(monkeypatch, fake_systemd):
    run(["setup", "--auto", "--no-service"])
    db = paths.usage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("x", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # accept all defaults
    rc = run(["uninstall"])  # interactive (tty True, no --auto/--yes)
    assert rc == 0 and db.exists() and not paths.manifest_path().exists()


def test_uninstall_wizard_purges_when_confirmed(monkeypatch, fake_systemd):
    run(["setup", "--auto", "--no-service"])
    db = paths.usage_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("x", encoding="utf-8")
    replies = iter(["y", "y"])  # delete data? yes ; proceed? yes
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
    rc = run(["uninstall"])
    assert rc == 0 and not db.exists()


# --- update (§14) ---------------------------------------------------------------


def _manifest(method, *, service=None, python="/p/python", marker=None, owned=False):
    return manifest.build_manifest(
        install_method=method, runtime_kind=method, runtime_command=[python, "-m", "tokdash"],
        runtime_owned_by_setup=owned, python_path=python, python_version="3.12",
        service=service, runtime_marker=marker, data_dir=str(paths.data_dir()),
        bind="127.0.0.1", port=55423,
    )


def _svc_block():
    return {
        "type": "systemd-user", "unit": str(paths.systemd_unit_path()), "name": "tokdash",
        "created_by_setup": True, "marker": "X-Tokdash-Managed id=x",
    }


def _capture_run(monkeypatch):
    calls = {"cmds": []}

    def fake_run(cmd, *a, **k):
        calls["cmds"].append(cmd)
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    return calls


def test_update_pipx_upgrades_and_restarts(monkeypatch, capsys):
    manifest.write_manifest(_manifest("pipx", service=_svc_block()))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    calls = _capture_run(monkeypatch)
    restarted = {}

    def fake_restart(name="tokdash"):
        restarted["n"] = name
        return _ok_proc()

    monkeypatch.setattr(systemd, "restart", fake_restart)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True
    assert ["pipx", "upgrade", "tokdash"] in calls["cmds"]
    assert payload["service_restarted"] is True and restarted["n"] == "tokdash"


def test_update_restart_failure_is_reported(monkeypatch, capsys):
    # The upgrade lands but the managed service fails to restart: must be ok:false / exit 1,
    # never a silent "updated" while the service still runs the old code (U1).
    manifest.write_manifest(_manifest("pipx", service=_svc_block()))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)  # pipx upgrade "succeeds"
    monkeypatch.setattr(systemd, "restart", lambda name="tokdash": subprocess.CompletedProcess([], 1, "", "boom"))
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert payload["restart_failed"] is True and payload["service_restarted"] is False


def test_update_managed_venv_pip_installs(monkeypatch, capsys):
    py = str(paths.managed_venv_python())
    manifest.write_manifest(_manifest("managed-venv", python=py, owned=True))
    calls = _capture_run(monkeypatch)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and [py, "-m", "pip", "install", "-U", "tokdash"] in calls["cmds"]
    assert payload["service_restarted"] is False  # no managed service in this manifest


def test_update_reports_version_before_and_after(monkeypatch, capsys):
    py = str(paths.managed_venv_python())
    manifest.write_manifest(_manifest("managed-venv", python=py, owned=True))
    versions = iter(["tokdash 1.0.1\n", "tokdash 1.0.2\n"])
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd == [py, "-m", "tokdash", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, next(versions), "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True
    assert payload["version_before"] == "1.0.1"
    assert payload["version_after"] == "1.0.2"
    assert payload["updated"] is True
    assert [py, "-m", "pip", "install", "-U", "tokdash"] in calls


def test_update_human_reports_no_version_change(monkeypatch, capsys):
    py = str(paths.managed_venv_python())
    manifest.write_manifest(_manifest("managed-venv", python=py, owned=True))

    def fake_run(cmd, *a, **k):
        if cmd == [py, "-m", "tokdash", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, "tokdash 1.0.2\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    capsys.readouterr()
    rc = run(["update"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Tokdash is already at 1.0.2 via managed-venv." in out


def test_update_existing_runtime_only_guides(monkeypatch, capsys):
    manifest.write_manifest(_manifest("existing"))
    ran = {}
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: ran.setdefault("x", True))
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["updated"] is False and "existing" in payload["reason"]
    assert not ran  # never mutates an interpreter setup did not create


def test_update_no_manifest_guides(capsys):
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["updated"] is False


def test_update_dry_run_changes_nothing(monkeypatch, capsys):
    manifest.write_manifest(_manifest("pipx", service=_svc_block()))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    ran = {}
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: ran.setdefault("x", True))
    rc, payload = run_json(["update", "--dry-run", "--json"], capsys)
    assert rc == 0 and payload["dry_run"] is True and not ran


def test_update_pipx_missing_on_path_guides(monkeypatch, capsys):
    manifest.write_manifest(_manifest("pipx"))
    monkeypatch.setattr(detect, "find_pipx", lambda: None)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["updated"] is False and "PATH" in payload["reason"]


# --- Phase 4: macOS launchd -----------------------------------------------------


def test_macos_auto_uses_launchd(macos):
    p = plan.build_setup_plan(plan.Options(auto=True), detect.detect_all(55423))
    assert p["service"]["type"] == "launchd"


def test_linux_launchd_flag_blocked():
    p = plan.build_setup_plan(plan.Options(auto=True, service="launchd"), _detection())
    assert p["ok"] is False and any("only supported on macOS" in b for b in p["blockers"])


def test_macos_setup_writes_plist_and_manifest(macos, fake_launchd, capsys):
    rc, payload = run_json(["setup", "--auto", "--service", "launchd", "--json"], capsys)
    assert rc == 0 and payload["service"]["type"] == "launchd"
    plist = paths.launchd_plist_path()
    assert plist.is_file() and "X-Tokdash-Managed" in plist.read_text(encoding="utf-8")
    assert manifest.read_manifest()["service"]["type"] == "launchd"
    assert "service:launchd" in payload["changed"]


def test_launchd_lifecycle_commands_allow_service_manager_timeout(macos, monkeypatch, tmp_path):
    seen = []

    def fake_run(args, timeout=20):
        seen.append((args, timeout))
        return _ok_proc()

    monkeypatch.setattr(launchd, "_run", fake_run)
    plist = tmp_path / "com.tokdash.tokdash.plist"
    launchd.bootstrap(plist)
    launchd.kickstart()
    launchd.bootout()
    assert seen == [
        (["bootstrap", f"gui/{launchd._uid()}", str(plist)], launchd.LIFECYCLE_TIMEOUT),
        (["kickstart", "-k", f"gui/{launchd._uid()}/{launchd.LABEL}"], launchd.LIFECYCLE_TIMEOUT),
        (["bootout", f"gui/{launchd._uid()}/{launchd.LABEL}"], launchd.LIFECYCLE_TIMEOUT),
    ]


def test_macos_setup_bootstrap_timeout_succeeds_when_service_is_ready(macos, fake_launchd, monkeypatch, capsys):
    def slow_bootstrap(plist):
        raise subprocess.TimeoutExpired(["launchctl", "bootstrap"], 20)

    monkeypatch.setattr(launchd, "bootstrap", slow_bootstrap)
    rc, payload = run_json(["setup", "--auto", "--service", "launchd", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True
    assert "launchctl bootstrap timed out" in payload["service"]["start_error"]
    assert payload["readiness"]["ok"] is True
    assert paths.manifest_path().exists()


def test_macos_setup_bootstrap_error_fails_closed_when_not_ready(macos, fake_launchd, monkeypatch, capsys):
    monkeypatch.setattr(launchd, "bootstrap", lambda plist: subprocess.CompletedProcess([], 1, "", "boom"))
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {
            "ok": False,
            "error": "service did not become ready: nothing answered on 127.0.0.1:55423",
            "port": {"port": port, "open": False, "is_tokdash": False, "version": None},
        },
    )
    rc, payload = run_json(["setup", "--auto", "--service", "launchd", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "did not become ready" in payload["error"]
    assert "launchctl bootstrap: boom" in payload["service"]["start_error"]
    assert paths.manifest_path().exists()


def test_macos_setup_refuses_unmarked_plist(macos, fake_launchd):
    plist = paths.launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist></plist>", encoding="utf-8")
    rc = run(["setup", "--auto", "--service", "launchd"])
    assert rc == 1 and "X-Tokdash-Managed" not in plist.read_text(encoding="utf-8")


def test_macos_uninstall_removes_launchd(macos, fake_launchd, capsys):
    run(["setup", "--auto", "--service", "launchd"])
    assert paths.launchd_plist_path().is_file()
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "service" in payload["changed"]
    assert not paths.launchd_plist_path().exists()


def test_macos_update_restarts_launchd(macos, fake_launchd, monkeypatch, capsys):
    svc = {"type": "launchd", "unit": str(paths.launchd_plist_path()), "name": launchd.LABEL,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    manifest.write_manifest(_manifest("pipx", service=svc))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)
    kicked = {}

    def fake_kick():
        kicked["x"] = True
        return _ok_proc()

    monkeypatch.setattr(launchd, "kickstart", fake_kick)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["service_restarted"] is True and kicked.get("x")


# --- Phase 3a: Tailscale Serve --------------------------------------------------


def test_tailscale_command_pairing():
    assert tailscale.serve_command(55423)[:2] == ["tailscale", "serve"]
    assert "--set-path=/tokdash" in tailscale.serve_command(55423)
    assert tailscale.teardown_command()[-1] == "off"
    assert "--set-path=/tokdash" in tailscale.teardown_command()
    blk = tailscale.manifest_block(55423)
    assert blk["configured_by_setup"] is True and blk["teardown_command"][-1] == "off"
    assert blk["path"] == "/tokdash"


def test_tailscale_parse_serve_status_url():
    status = """https://tokdash-node.example.test (tailnet only)
|-- /tokdash proxy http://127.0.0.1:55423
"""
    assert tailscale.parse_serve_url(status, 55423) == "https://tokdash-node.example.test/tokdash"


def test_parse_serve_url_fallback_scopes_to_path():
    # Status layout where the target line never resolves: the fallback must still scope the
    # host URL to /tokdash rather than advertising the bare tailnet host root.
    status = "https://tokdash-node.example.test (tailnet only)\n"
    assert tailscale.parse_serve_url(status, 55423) == "https://tokdash-node.example.test/tokdash"


def test_write_manifest_removes_tmp_on_replace_failure(monkeypatch, tmp_path):
    target = tmp_path / "install.json"

    def boom(self, dst):
        raise OSError("disk full")

    monkeypatch.setattr(manifest.Path, "replace", boom)
    with pytest.raises(OSError):
        manifest.write_manifest({"schema": 1}, path=target)
    assert not (tmp_path / "install.json.tmp").exists()  # no half-written sidecar left behind
    assert not target.exists()


def test_uninstall_runs_recorded_tailscale_teardown(fake_systemd, monkeypatch, capsys):
    mid = "tsmark"
    svc = {"type": "systemd-user", "unit": str(paths.systemd_unit_path()), "name": "tokdash",
           "created_by_setup": True, "marker": manifest.marker_token(mid)}
    man = _manifest("existing", service=svc)
    man["tailscale_serve"] = {"configured_by_setup": True, "target": "x",
                              "teardown_command": ["tailscale", "serve", "--https=443", "--set-path=/tokdash", "off"]}
    manifest.write_manifest(man)
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(systemd.render_unit(["py", "-m", "tokdash"], "127.0.0.1", 1, marker_id=mid), encoding="utf-8")
    calls = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "tailscale" in payload["changed"]
    assert calls["cmd"] == ["tailscale", "serve", "--https=443", "--set-path=/tokdash", "off"]


def test_setup_interactive_offers_and_records_tailscale(monkeypatch, fake_systemd):
    monkeypatch.setattr(detect, "tailscale_available", lambda: True)
    monkeypatch.setattr(
        tailscale, "run_serve",
        lambda port, **k: {
            "ok": True,
            "command": ["tailscale", "serve"],
            "block": tailscale.manifest_block(port, url="https://tokdash-node.example.test/tokdash"),
            "url": "https://tokdash-node.example.test/tokdash",
            "error": None,
        },
    )
    replies = iter(["y", "y"])  # confirm setup ; run tailscale
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
    rc = run(["setup", "--service", "systemd"])  # interactive (tty True, not --auto)
    assert rc == 0
    man = manifest.read_manifest()
    assert man["tailscale_serve"]["configured_by_setup"] is True
    assert man["tailscale_serve"]["url"] == "https://tokdash-node.example.test/tokdash"


def test_setup_result_hides_generic_remote_hint_after_tailscale_url():
    notes = engine._setup_result_notes({
        "tailscale_url": "https://tokdash-node.example.test/tokdash",
        "notes": [
            "Remote access (optional, explicit): `tailscale serve ...`",
            "Tailscale URL: https://tokdash-node.example.test/tokdash (tailnet only; write actions stay disabled through Serve).",
        ],
    })
    assert notes == [
        "Tailscale URL: https://tokdash-node.example.test/tokdash (tailnet only; write actions stay disabled through Serve)."
    ]


def test_setup_interactive_tailscale_operator_grant_and_retry(monkeypatch, fake_systemd):
    monkeypatch.setattr(detect, "tailscale_available", lambda: True)
    attempts = []

    def fake_run_serve(port, **k):
        attempts.append(port)
        if len(attempts) == 1:
            return {
                "ok": False,
                "command": ["tailscale", "serve"],
                "block": None,
                "error": "Access denied: serve config denied",
            }
        return {"ok": True, "command": ["tailscale", "serve"], "block": tailscale.manifest_block(port), "error": None}

    granted = {}
    monkeypatch.setattr(tailscale, "run_serve", fake_run_serve)
    monkeypatch.setattr(
        tailscale,
        "grant_operator",
        lambda **k: granted.setdefault("result", {"ok": True, "command": ["sudo", "tailscale", "set", "--operator=howard"], "error": None}),
    )
    replies = iter(["y", "y", "y"])  # confirm setup ; run tailscale ; grant operator
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))

    rc = run(["setup", "--service", "systemd"])
    assert rc == 0 and len(attempts) == 2 and granted
    assert manifest.read_manifest()["tailscale_serve"]["configured_by_setup"] is True


def test_setup_records_tailscale_teardown_before_activation(monkeypatch, fake_systemd):
    # The teardown must be in install.json BEFORE `tailscale serve` can activate, so a crash
    # in the window can never strand an unauthenticated exposure that uninstall can't revert.
    monkeypatch.setattr(detect, "tailscale_available", lambda: True)
    seen = {}

    def fake_run_serve(port, **k):
        man = manifest.read_manifest()
        seen["pre"] = man["tailscale_serve"]
        return {"ok": True, "command": ["tailscale", "serve"], "block": tailscale.manifest_block(port), "error": None}

    monkeypatch.setattr(tailscale, "run_serve", fake_run_serve)
    replies = iter(["y", "y"])  # confirm setup ; run tailscale
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
    rc = run(["setup", "--service", "systemd"])
    assert rc == 0
    assert seen["pre"]["configured_by_setup"] is True
    assert seen["pre"]["teardown_command"][-1] == "off"


def test_setup_tailscale_failure_does_not_strand_record(monkeypatch, fake_systemd):
    # A failed `tailscale serve` (nothing exposed) must reconcile the pre-record back out so
    # uninstall has no stale teardown to run; setup still succeeds (Tailscale is optional).
    monkeypatch.setattr(detect, "tailscale_available", lambda: True)
    monkeypatch.setattr(
        tailscale, "run_serve",
        lambda port, **k: {"ok": False, "command": ["tailscale", "serve"], "block": None, "error": "boom"},
    )
    replies = iter(["y", "y"])  # confirm setup ; run tailscale
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
    rc = run(["setup", "--service", "systemd"])
    assert rc == 0
    assert manifest.read_manifest()["tailscale_serve"]["configured_by_setup"] is False


def test_setup_tailscale_final_url_write_failure_does_not_crash(monkeypatch, fake_systemd, capsys):
    # Serve succeeds but the post-success manifest write (the one carrying the resolved URL)
    # fails. Setup must NOT crash with a traceback: the pre-recorded teardown already covers
    # revert, so the user gets the manual teardown hint and uninstall still works.
    monkeypatch.setattr(detect, "tailscale_available", lambda: True)
    monkeypatch.setattr(
        tailscale, "run_serve",
        lambda port, **k: {
            "ok": True,
            "command": ["tailscale", "serve"],
            "block": tailscale.manifest_block(port, url="https://tokdash-node.example.test/tokdash"),
            "url": "https://tokdash-node.example.test/tokdash",
            "error": None,
        },
    )
    real_write = manifest.write_manifest

    def failing_write(data, path=None):
        # Fail only on the write carrying the resolved URL (post-success); the base manifest
        # and the url=None pre-record must still go through.
        if (data.get("tailscale_serve") or {}).get("url"):
            raise OSError("disk full")
        return real_write(data, path)

    monkeypatch.setattr(manifest, "write_manifest", failing_write)
    replies = iter(["y", "y"])  # confirm setup ; run tailscale
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))

    rc = run(["setup", "--service", "systemd"])
    assert rc == 0  # no traceback
    assert "uninstall" in capsys.readouterr().err  # manual teardown hint surfaced
    # The pre-recorded teardown survived, so uninstall can still revert the exposure.
    man = manifest.read_manifest()
    assert man["tailscale_serve"]["configured_by_setup"] is True
    assert man["tailscale_serve"]["teardown_command"][-1] == "off"
    assert man["tailscale_serve"]["url"] is None  # the URL write is exactly what failed


def test_auto_never_runs_tailscale_but_prints_hint(monkeypatch, fake_systemd):
    monkeypatch.setattr(detect, "tailscale_available", lambda: True)
    ran = {}
    monkeypatch.setattr(tailscale, "run_serve", lambda *a, **k: ran.setdefault("x", True))
    run(["setup", "--auto", "--service", "systemd"])
    assert not ran  # --auto must never expose
    man = manifest.read_manifest()
    assert man["tailscale_serve"]["configured_by_setup"] is False


# --- Phase 3b: opt-in update check ----------------------------------------------


def test_updatecheck_enabled_via_env(monkeypatch):
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", "1")
    assert updatecheck.is_enabled() is True
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", "0")
    assert updatecheck.is_enabled() is False  # hard kill switch wins


@pytest.mark.parametrize("value", ["0", "off", "false", "no", "OFF", "False", "  no  "])
def test_updatecheck_kill_switch_forms(monkeypatch, value):
    # kill_switched() and is_enabled() must agree on every disabling form (case-insensitive,
    # whitespace-trimmed), so the setup consent step and the runtime never diverge on "off".
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", value)
    assert updatecheck.kill_switched() is True
    assert updatecheck.is_enabled() is False


def test_updatecheck_enable_writes_config(monkeypatch):
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    assert updatecheck.is_enabled() is False
    updatecheck.enable()
    assert updatecheck.is_enabled() is True


def test_updatecheck_detects_newer(monkeypatch):
    import json as _json

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        updatecheck.urllib.request, "urlopen",
        lambda *a, **k: _Resp(_json.dumps({"info": {"version": "9.9.9"}}).encode("utf-8")),
    )
    res = updatecheck.check("0.6.2", use_cache=False)
    assert res["latest"] == "9.9.9" and res["update_available"] is True
    assert updatecheck.check("9.9.9", use_cache=False)["update_available"] is False


def test_doctor_reports_update_when_enabled(monkeypatch, capsys):
    monkeypatch.setenv("TOKDASH_UPDATE_CHECK", "1")
    monkeypatch.setattr(
        updatecheck, "check",
        lambda v, **k: {"current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False},
    )
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert payload["update_check"]["enabled"] is True and payload["update_check"]["update_available"] is True


def test_doctor_update_disabled_by_default(monkeypatch, capsys):
    monkeypatch.delenv("TOKDASH_UPDATE_CHECK", raising=False)
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert payload["update_check"] == {"enabled": False}


# --- Round-1 adversarial-review regressions -------------------------------------


def test_uninstall_removes_orphaned_marked_unit_when_manifest_says_no_service(fake_systemd, capsys):
    # setup --service systemd (marked unit + manifest), then setup --no-service overwrites the
    # manifest with service:None but leaves the marked unit on disk. A SINGLE uninstall must
    # still stop+remove the setup-owned marked unit (not silently leave it running).
    run(["setup", "--auto", "--service", "systemd"])
    unit = paths.systemd_unit_path()
    assert unit.is_file() and "X-Tokdash-Managed" in unit.read_text(encoding="utf-8")
    run(["setup", "--auto", "--no-service"])  # manifest now records service:None
    assert manifest.read_manifest()["service"] is None
    assert unit.is_file()  # not torn down by setup
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "service" in payload["changed"]
    assert not unit.exists()


def test_uninstall_no_service_manifest_leaves_unmarked_unit(fake_systemd):
    # Manifest says service:None and an UNMARKED (user's own) unit sits at the path — uninstall
    # must NOT block on it and must NOT remove it; it is provably not ours.
    run(["setup", "--auto", "--no-service"])
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve\n", encoding="utf-8")
    rc = run(["uninstall", "--auto"])
    assert rc == 0 and unit.exists()  # left alone, no block


def test_uninstall_tailscale_teardown_failure_is_reported(fake_systemd, monkeypatch, capsys):
    mid = "tsfail"
    svc = {"type": "systemd-user", "unit": str(paths.systemd_unit_path()), "name": "tokdash",
           "created_by_setup": True, "marker": manifest.marker_token(mid)}
    man = _manifest("existing", service=svc)
    man["tailscale_serve"] = {"configured_by_setup": True, "target": "x",
                              "teardown_command": ["tailscale", "serve", "--https=443", "--set-path=/tokdash", "off"]}
    manifest.write_manifest(man)
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(systemd.render_unit(["py", "-m", "tokdash"], "127.0.0.1", 1, marker_id=mid), encoding="utf-8")
    # tailscale present but the `off` command fails -> exposure may still be live.
    monkeypatch.setattr(engine.subprocess, "run", lambda c, *a, **k: subprocess.CompletedProcess(c, 1, "", "serve config error"))
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "tailscale" not in payload["changed"]
    assert any("tailscale" in e for e in payload["errors"])
    # The manifest (sole record of the teardown command) MUST be preserved so a re-run can
    # retry — deleting it would strand the live exposure and the next uninstall would lie.
    assert paths.manifest_path().is_file()
    assert payload.get("manifest_kept_for_retry") is True
    # Re-run with tailscale now succeeding: teardown retried, manifest finally removed.
    monkeypatch.setattr(engine.subprocess, "run", lambda c, *a, **k: subprocess.CompletedProcess(c, 0, "", ""))
    rc2, payload2 = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc2 == 0 and "tailscale" in payload2["changed"]
    assert not paths.manifest_path().exists()


def test_uninstall_runtime_removal_failure_preserves_manifest(monkeypatch, fake_systemd, capsys):
    def fake_create(builder_python=None):
        paths.managed_venv_python().parent.mkdir(parents=True, exist_ok=True)
        paths.managed_venv_python().write_text("#!/bin/sh\n", encoding="utf-8")
        paths.runtime_marker_path().write_text("x\n", encoding="utf-8")
        return str(paths.managed_venv_python())

    monkeypatch.setattr(runtime, "create_managed_venv", fake_create)
    run(["setup", "--auto", "--runtime", "venv", "--no-service"])
    assert paths.runtime_dir().is_dir() and paths.manifest_path().is_file()
    # rmtree silently leaves the tree (simulate read-only/busy): must surface as an error,
    # NOT be reported as a successful removal, and the manifest must be preserved.
    monkeypatch.setattr(engine.shutil, "rmtree", lambda *a, **k: None)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "runtime" not in payload["changed"]
    assert paths.manifest_path().is_file() and payload.get("manifest_kept_for_retry") is True


def test_resetup_from_managed_venv_keeps_ownership(monkeypatch, fake_systemd):
    # Re-running setup while the invoking interpreter IS the managed venv must keep
    # owned_by_setup=True so uninstall can still remove the venv.
    paths.managed_venv_python().parent.mkdir(parents=True, exist_ok=True)
    paths.managed_venv_python().write_text("#!/bin/sh\n", encoding="utf-8")
    paths.runtime_marker_path().write_text("created-by=tokdash-setup\n", encoding="utf-8")
    monkeypatch.setattr(
        detect, "classify_current_runtime",
        lambda: {"kind": "venv", "install_method": "managed-venv",
                 "python": str(paths.managed_venv_python()),
                 "command": [str(paths.managed_venv_python()), "-m", "tokdash"]},
    )
    rt = runtime.resolve("auto", detect.detect_all(55423))
    assert rt["owned_by_setup"] is True and rt["install_method"] == "managed-venv"


def test_setup_honors_tokdash_port(monkeypatch, capsys):
    # Goes through the real cli.cli() entrypoint (where TOKDASH_PORT is resolved, symmetric
    # with serve) — not the run() helper, which bypasses that dispatch.
    monkeypatch.setenv("TOKDASH_PORT", "9123")
    capsys.readouterr()
    rc = cli.cli(["setup", "--auto", "--no-service", "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["port"] == 9123


def test_doctor_ignores_tokdash_port_prefers_manifest(monkeypatch, capsys):
    # doctor must NOT take TOKDASH_PORT over the manifest-recorded port.
    monkeypatch.setenv("TOKDASH_PORT", "9123")
    man = manifest.build_manifest(
        install_method="existing", runtime_kind="existing", runtime_command=["py", "-m", "tokdash"],
        runtime_owned_by_setup=False, python_path="py", python_version="3.11", service=None,
        runtime_marker=None, data_dir=str(paths.data_dir()), bind="127.0.0.1", port=55555,
    )
    manifest.write_manifest(man)
    capsys.readouterr()
    cli.cli(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["port"]["port"] == 55555


def test_interactive_busy_foreign_port_blocks_service(monkeypatch):
    monkeypatch.setattr(detect, "probe_port", lambda *a, **k: {"port": 55423, "open": True, "is_tokdash": False, "version": None})
    p = plan.build_setup_plan(plan.Options(auto=False, service="systemd"), detect.detect_all(55423))
    assert p["ok"] is False and any("busy" in b.lower() for b in p["blockers"])


def test_force_unmarked_systemd_allows_prefingerprint_tokdash_port(monkeypatch):
    # Migration path for pre-1.0 manual services: the old service occupies 55423 but its
    # /health lacks the Tokdash fingerprint, so the port probe says "not Tokdash". With
    # --force and an unmarked tokdash.service at the managed path, setup should replace and
    # restart that unit instead of blocking on its own old process.
    unit = paths.systemd_unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\nExecStart=python -m tokdash serve --port 55423\n", encoding="utf-8")
    monkeypatch.setattr(detect, "probe_port", lambda *a, **k: {"port": 55423, "open": True, "is_tokdash": False, "version": None})
    p = plan.build_setup_plan(plan.Options(auto=False, service="systemd", force=True), detect.detect_all(55423))
    assert p["ok"] is True
    assert p["port"] == 55423
    assert any("--force will replace" in n for n in p["notes"])


def test_interactive_busy_foreign_port_ok_when_no_service(monkeypatch):
    monkeypatch.setattr(detect, "probe_port", lambda *a, **k: {"port": 55423, "open": True, "is_tokdash": False, "version": None})
    p = plan.build_setup_plan(plan.Options(auto=False, no_service=True), detect.detect_all(55423))
    assert p["ok"] is True  # no service to bind -> just a warning


def test_db_dry_run_rejected_for_resync():
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        cli.db_command("resync", False, None, "today", dry_run=True)
    with _pytest.raises(SystemExit):
        cli.db_command("sync", False, None, "today", dry_run=True)


def test_update_launchd_restart_failure_is_reported(macos, fake_launchd, monkeypatch, capsys):
    svc = {"type": "launchd", "unit": str(paths.launchd_plist_path()), "name": launchd.LABEL,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    manifest.write_manifest(_manifest("pipx", service=svc))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)
    monkeypatch.setattr(launchd, "kickstart", lambda: subprocess.CompletedProcess([], 1, "", "boom"))
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert payload["restart_failed"] is True and payload["service_restarted"] is False


def test_version_compare_trailing_zero_and_prerelease():
    assert updatecheck._is_newer("1.0.0", "1.0") is False  # same version, no phantom update
    assert updatecheck._is_newer("0.6.1.0", "0.6.1") is False
    assert updatecheck._is_newer("0.7.0", "0.6.2") is True
    # Prerelease ordering (PEP 440 via packaging): final > rc, later rc > earlier, rc < final.
    assert updatecheck._is_newer("1.0.0", "1.0.0rc1") is True
    assert updatecheck._is_newer("1.0.0rc2", "1.0.0rc1") is True
    assert updatecheck._is_newer("1.0.0rc1", "1.0.0") is False


def test_version_compare_fallback_handles_trailing_zero(monkeypatch):
    # Force the packaging-absent fallback (_version_key) and confirm trailing-zero equality.
    import builtins

    real_import = builtins.__import__

    def no_packaging(name, *a, **k):
        if name == "packaging.version" or name.startswith("packaging"):
            raise ImportError("simulated: packaging unavailable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_packaging)
    assert updatecheck._is_newer("1.0.0", "1.0") is False
    assert updatecheck._is_newer("0.7.0", "0.6.2") is True


def test_update_launchd_restart_failure_message_uses_launchctl(macos, fake_launchd, monkeypatch, capsys):
    svc = {"type": "launchd", "unit": str(paths.launchd_plist_path()), "name": launchd.LABEL,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    manifest.write_manifest(_manifest("pipx", service=svc))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)
    monkeypatch.setattr(launchd, "kickstart", lambda: subprocess.CompletedProcess([], 1, "", "boom"))
    capsys.readouterr()
    rc = run(["update"])  # human output (not --json)
    out = capsys.readouterr().out
    assert rc == 1 and "launchctl kickstart" in out and "systemctl" not in out


def test_updatecheck_cache_recomputes_per_current(monkeypatch):
    import json as _json

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(updatecheck.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(_json.dumps({"info": {"version": "0.7.0"}}).encode("utf-8")))
    first = updatecheck.check("0.6.2")  # populates cache, update_available True
    assert first["update_available"] is True
    # A cache hit with current == latest must NOT report an update from the stale verdict.
    second = updatecheck.check("0.7.0")
    assert second["cached"] is True and second["update_available"] is False


# --- adversarial-review regressions (round 3/4) ---------------------------------


def test_uninstall_systemd_disable_failure_preserves_manifest(monkeypatch, fake_systemd, capsys):
    # A failed `systemctl --user disable --now` on a service that is STILL active (stop-job
    # timeout on a hung service; is_active_strict -> True via fake_systemd) must NOT report
    # success: uninstall reports ok:false, leaves the unit in place, and PRESERVES the manifest
    # for retry. (Regression: uninstall-disable-failure-silently-succeeds.)
    run(["setup", "--auto", "--service", "systemd"])
    monkeypatch.setattr(
        systemd, "disable_now",
        lambda name="tokdash": subprocess.CompletedProcess([], 1, "", "Job failed: timeout stopping service"),
    )
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "service" not in payload["changed"] and "manifest" not in payload["changed"]
    assert payload["manifest_kept_for_retry"] is True
    assert paths.manifest_path().exists()          # preserved so a re-run can retry
    assert paths.systemd_unit_path().exists()       # not unlinked after a failed stop
    assert any("disable" in e for e in payload["errors"])


def test_uninstall_systemd_disable_nonzero_but_inactive_is_benign(monkeypatch, fake_systemd, capsys):
    # disable_now returns non-zero only because the unit/service is already gone/stopped (e.g. a
    # prior partial uninstall removed the unit file -> "Unit file does not exist"). A confirmed-
    # inactive service is benign success, not a false failure. (Regression:
    # uninstall-systemd-disable-false-fail-on-gone-unit.)
    run(["setup", "--auto", "--service", "systemd"])
    monkeypatch.setattr(
        systemd, "disable_now",
        lambda name="tokdash": subprocess.CompletedProcess([], 1, "", "Unit file tokdash.service does not exist."),
    )
    monkeypatch.setattr(systemd, "is_active_strict", lambda name="tokdash": False)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True and "service" in payload["changed"]
    assert not paths.manifest_path().exists()


def test_uninstall_systemd_disable_unconfirmable_fails_closed(monkeypatch, fake_systemd, capsys):
    # disable_now fails AND the strict active probe can't confirm the state (hung systemctl ->
    # raises). is_active() would swallow that and return False (fail OPEN); the strict probe
    # must make uninstall fail CLOSED instead, matching the launchd arm.
    run(["setup", "--auto", "--service", "systemd"])
    monkeypatch.setattr(
        systemd, "disable_now",
        lambda name="tokdash": subprocess.CompletedProcess([], 1, "", "stop timed out"),
    )

    def hung(name="tokdash"):
        raise subprocess.TimeoutExpired("systemctl", 10)

    monkeypatch.setattr(systemd, "is_active_strict", hung)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False and "service" not in payload["changed"]
    assert paths.manifest_path().exists() and paths.systemd_unit_path().exists()


def test_uninstall_launchd_bootout_failure_preserves_manifest(macos, fake_launchd, monkeypatch, capsys):
    # launchd analogue: bootout fails AND the agent is still loaded -> a real "couldn't stop
    # it", so ok:false + manifest/plist preserved for retry.
    run(["setup", "--auto", "--service", "launchd"])
    monkeypatch.setattr(launchd, "bootout", lambda: subprocess.CompletedProcess([], 1, "", "Boot-out failed: busy"))
    monkeypatch.setattr(launchd, "is_loaded_strict", lambda: True)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "service" not in payload["changed"]
    assert paths.manifest_path().exists() and paths.launchd_plist_path().exists()


def test_uninstall_launchd_bootout_nonzero_but_unloaded_is_benign(macos, fake_launchd, monkeypatch, capsys):
    # bootout returns non-zero simply because the agent wasn't loaded (nothing to stop). The
    # end state is what matters: confirmed not loaded -> treat as success and remove plist/manifest.
    run(["setup", "--auto", "--service", "launchd"])
    monkeypatch.setattr(launchd, "bootout", lambda: subprocess.CompletedProcess([], 3, "", "No such process"))
    monkeypatch.setattr(launchd, "is_loaded_strict", lambda: False)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and payload["ok"] is True and "service" in payload["changed"]
    assert not paths.launchd_plist_path().exists()


def test_uninstall_launchd_bootout_fail_unconfirmable_fails_closed(macos, fake_launchd, monkeypatch, capsys):
    # The dangerous correlated case: bootout returns non-zero AND the strict load probe can't
    # confirm the state (hung launchctl -> raises). is_loaded() would swallow that and return
    # False (fail OPEN); the strict probe must make uninstall fail CLOSED instead.
    # (Regression: uninstall-launchd-bootout-fail-open-on-hung-launchctl.)
    run(["setup", "--auto", "--service", "launchd"])
    monkeypatch.setattr(launchd, "bootout", lambda: subprocess.CompletedProcess([], 1, "", "stop timed out"))

    def hung():
        raise subprocess.TimeoutExpired("launchctl", 10)

    monkeypatch.setattr(launchd, "is_loaded_strict", hung)
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False and "service" not in payload["changed"]
    assert paths.manifest_path().exists() and paths.launchd_plist_path().exists()


def test_update_systemd_restart_exception_is_reported_not_raised(monkeypatch, capsys):
    # A hung `systemctl restart` raises TimeoutExpired AFTER a successful upgrade. cmd_update
    # must treat it as a failed restart (ok:false, remediation), never let the traceback escape.
    # (Regression: update-restart-exception-uncaught.)
    manifest.write_manifest(_manifest("pipx", service=_svc_block()))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)  # pipx upgrade "succeeds"

    def boom(name="tokdash"):
        raise subprocess.TimeoutExpired("systemctl", 20)

    monkeypatch.setattr(systemd, "restart", boom)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert payload["restart_failed"] is True and payload["service_restarted"] is False


def test_update_launchd_restart_exception_is_reported_not_raised(macos, fake_launchd, monkeypatch, capsys):
    svc = {"type": "launchd", "unit": str(paths.launchd_plist_path()), "name": launchd.LABEL,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    manifest.write_manifest(_manifest("pipx", service=svc))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)

    def boom():
        raise subprocess.TimeoutExpired("launchctl", 20)

    monkeypatch.setattr(launchd, "kickstart", boom)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False and payload["restart_failed"] is True


def test_update_launchd_restart_message_uses_label_when_name_missing(macos, fake_launchd, monkeypatch, capsys):
    # A manifest whose launchd service block lacks 'name' must still produce a remediation
    # command targeting the real label (com.tokdash.tokdash), not the literal "tokdash".
    # (Regression: update-launchd-service-name-fallback.)
    svc = {"type": "launchd", "unit": str(paths.launchd_plist_path()),
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}  # no 'name'
    manifest.write_manifest(_manifest("pipx", service=svc))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)
    monkeypatch.setattr(launchd, "kickstart", lambda: subprocess.CompletedProcess([], 1, "", "boom"))
    capsys.readouterr()
    rc = run(["update"])  # human output
    out = capsys.readouterr().out
    assert rc == 1 and "com.tokdash.tokdash" in out and out.rstrip().endswith("com.tokdash.tokdash")


def test_purge_data_removes_pricing_override(monkeypatch):
    # The round-2 pricing relocation put dashboard pricing edits under the data dir, so
    # `--purge` (delete usage history + config) must remove them too. (Regression:
    # r2-purge-pricing-override.)
    override = paths.pricing_db_override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text('{"models": {}}', encoding="utf-8")
    tmp = override.with_suffix(override.suffix + ".tmp")
    tmp.write_text("{}", encoding="utf-8")  # simulate a crashed-write sidecar
    engine._purge_data()
    assert not override.exists() and not tmp.exists()


def test_uninstall_purge_reports_failure_when_target_survives(fake_systemd, capsys):
    # _purge_data must not claim success when a target can't be removed: a still-present file
    # after the unlink attempt -> error -> ok:false + manifest preserved (mirrors runtime step).
    # (Regression: uninstall-purge-false-success-on-unlink-error.)
    run(["setup", "--auto", "--no-service"])  # writes manifest
    cfg = paths.config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.mkdir()  # a directory where _purge_data expects a file -> unlink skipped, still present
    rc, payload = run_json(["uninstall", "--auto", "--purge", "--json"], capsys)
    assert rc == 1 and payload["ok"] is False
    assert "data" not in payload["changed"]
    assert payload["manifest_kept_for_retry"] is True and paths.manifest_path().exists()


def test_update_dryrun_and_live_share_managed_service_key(monkeypatch, capsys):
    # The --json schema must be stable across dry-run and apply for bundlers: both expose
    # has_managed_service (not the old dry-run-only restart_service).
    # (Regression: update-json-dryrun-live-key-divergence.)
    manifest.write_manifest(_manifest("pipx", service=_svc_block()))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)
    monkeypatch.setattr(systemd, "restart", lambda name="tokdash": _ok_proc())
    _, dry = run_json(["update", "--dry-run", "--json"], capsys)
    _, live = run_json(["update", "--json"], capsys)
    assert "has_managed_service" in dry and "restart_service" not in dry
    assert "has_managed_service" in live
    assert dry["has_managed_service"] is True and live["has_managed_service"] is True


def test_update_uninstall_tolerate_malformed_tokdash_port(monkeypatch, fake_systemd):
    # update/uninstall never bind a port, so a malformed TOKDASH_PORT must NOT make them die
    # with "Invalid TOKDASH_PORT". Driven through cli.cli (where the port is resolved), unlike
    # the run() helper. setup STILL validates it. (Regression:
    # update-uninstall-spurious-tokdash-port-validation.)
    import pytest as _pytest

    run(["setup", "--auto", "--service", "systemd"])  # manifest + marked unit exist
    monkeypatch.setenv("TOKDASH_PORT", "notaport")
    assert cli.cli(["uninstall", "--auto"]) == 0  # tolerates the bad port (uninstalls cleanly)
    with _pytest.raises(SystemExit):
        cli.cli(["setup", "--auto", "--service", "systemd"])  # the one command that binds a port


def test_update_restart_message_label_when_name_is_null(macos, fake_launchd, monkeypatch, capsys):
    # A manifest whose service name is explicitly null (corrupt/hand-edited) must still produce
    # a valid remediation label, not "...restart None". (Regression:
    # update-restart-remediation-none-on-explicit-null-name.)
    svc = {"type": "launchd", "unit": str(paths.launchd_plist_path()), "name": None,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    manifest.write_manifest(_manifest("pipx", service=svc))
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    _capture_run(monkeypatch)
    monkeypatch.setattr(launchd, "kickstart", lambda: subprocess.CompletedProcess([], 1, "", "boom"))
    capsys.readouterr()
    rc = run(["update"])  # human output
    out = capsys.readouterr().out
    assert rc == 1 and "com.tokdash.tokdash" in out and "None" not in out
