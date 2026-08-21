"""Tier 2: native Windows Task Scheduler backend (``winsched``).

Mirrors the systemd/launchd coverage in ``test_onboard.py`` — rendering, marker detection,
lifecycle commands, and setup/uninstall *planning* — all via monkeypatched ``schtasks``
(exactly how ``test_onboard.py`` monkeypatches ``systemctl``/``launchctl``). There is no
Windows machine in this environment, so nothing here executes a real ``schtasks``; real
Windows execution is deferred to CI.
"""
from __future__ import annotations

import codecs
import subprocess
from pathlib import Path

import pytest

from tokdash import cli
from tokdash.onboard import detect, engine, manifest, paths, plan, runtime, service_base, winsched
from tokdash.onboard.engine import run_lifecycle


# --- harness --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect every onboarding path into tmp and stub the OS-touching probes."""
    data_dir = tmp_path / "dd"
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(detect, "probe_port", lambda port=55423, *a, **k: {"port": port, "open": False, "is_tokdash": False, "version": None})
    monkeypatch.setattr(detect, "is_tty", lambda: True)
    monkeypatch.setattr(detect, "systemd_user_available", lambda: True)
    yield


def _ok_proc():
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


@pytest.fixture
def windows_env(monkeypatch, tmp_path):
    """Pretend we're on native Windows with Task Scheduler available; redirect the task
    XML into tmp (mirrors the ``macos``/``fake_launchd`` fixtures in test_onboard.py)."""
    monkeypatch.setattr(detect, "os_kind", lambda: "windows")
    monkeypatch.setattr(detect, "winsched_available", lambda: True)
    monkeypatch.setattr(detect, "systemd_user_available", lambda: False)
    monkeypatch.setattr(detect, "launchd_available", lambda: False)
    task_path = tmp_path / "AppData" / "Tokdash" / "Tokdash.xml"
    monkeypatch.setattr(paths, "winsched_task_path", lambda: task_path)


@pytest.fixture
def fake_winsched(monkeypatch):
    """Make schtasks calls no-ops that report a healthy, registered+running task."""
    monkeypatch.setattr(winsched, "create", lambda task_path, name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "delete", lambda name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "run_now", lambda name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "end_task", lambda name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "is_registered", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_registered_strict", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_running", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {"ok": True, "port": {"port": port, "open": True, "is_tokdash": True, "version": "test"}},
    )


def run(argv):
    args = cli.build_parser("tokdash").parse_args(argv)
    return run_lifecycle(args)


def run_json(argv, capsys):
    import json

    capsys.readouterr()
    rc = run(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


# --- rendering --------------------------------------------------------------------


def test_task_carries_marker_and_uses_pythonw():
    text = winsched.render_task(
        ["C:\\dd\\runtime\\python-venv\\Scripts\\python.exe", "-m", "tokdash"],
        "127.0.0.1", 55423, marker_id="abc123",
    )
    # schtasks /Create /XML hands the file to MSXML as UTF-16 and refuses a UTF-8
    # declaration — the bytes write_task emits must be what this declaration promises.
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert "Managed-by: tokdash-setup" in text
    assert "X-Tokdash-Managed id=abc123" in text
    assert "<Command>C:\\dd\\runtime\\python-venv\\Scripts\\pythonw.exe</Command>" in text
    assert "<Arguments>-m tokdash serve --bind 127.0.0.1 --port 55423 --no-open</Arguments>" in text
    assert "TOKDASH_DATA_DIR" not in text  # default data dir => no env snippet
    assert "<LogonType>InteractiveToken</LogonType>" in text  # never SYSTEM/elevated
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text


def test_task_leaves_non_python_command_unchanged():
    # _pythonw_for only swaps a plain "python.exe"; an unexpected runtime is passed through
    # unchanged rather than inventing a nonexistent binary.
    text = winsched.render_task(["C:\\rt\\tokdash.exe", "serveronly"], "127.0.0.1", 1, marker_id="x")
    assert "<Command>C:\\rt\\tokdash.exe</Command>" in text


def test_task_env_snippet_when_non_default_data_dir():
    text = winsched.render_task(
        ["C:\\py\\python.exe", "-m", "tokdash"], "127.0.0.1", 55423,
        marker_id="x", env_data_dir="C:\\custom\\dd",
    )
    assert "<Command>C:\\py\\pythonw.exe</Command>" in text
    assert "<Arguments>-c \"import os,runpy,sys;" in text
    assert "os.environ['TOKDASH_DATA_DIR']='C:\\\\custom\\\\dd';" in text
    assert "sys.argv=['tokdash', 'serve', '--bind', '127.0.0.1', '--port', '55423', '--no-open'];" in text
    assert "sys.argv=['tokdash', '-m', 'tokdash'" not in text
    assert "runpy.run_module('tokdash', run_name='__main__')\"</Arguments>" in text


def test_task_is_managed_detection_via_file(tmp_path):
    text = winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="deadbeef")
    task_path = tmp_path / "Tokdash.xml"
    # The same bytes write_task writes: UTF-16 LE with a BOM.
    task_path.write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
    assert winsched.task_is_managed(task_path) is True
    assert winsched.task_is_managed(task_path, "deadbeef") is True
    assert winsched.task_is_managed(task_path, "other") is False

    # Files written by pre-UTF-16 tokdash were plain UTF-8 — including failed setups,
    # which wrote the file before schtasks rejected it. The reader must still recognize
    # their marker, or uninstall refuses to remove a task setup itself registered.
    legacy = tmp_path / "legacy.xml"
    legacy.write_text(text, encoding="utf-8")
    assert winsched.task_is_managed(legacy) is True
    assert winsched.task_is_managed(legacy, "deadbeef") is True

    unmarked = tmp_path / "manual.xml"
    unmarked.write_text("<Task><Actions/></Task>", encoding="utf-8")
    assert winsched.task_is_managed(unmarked) is False

    missing = tmp_path / "missing.xml"
    assert winsched.task_is_managed(missing) is False


def test_task_is_managed_detection_via_live_name(monkeypatch):
    text = winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="deadbeef")

    def fake_run(args, timeout=20):
        assert args[:2] == ["/Query", "/TN"]
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")

    monkeypatch.setattr(winsched, "_run", fake_run)
    assert winsched.task_is_managed("Tokdash") is True
    assert winsched.task_is_managed("Tokdash", "deadbeef") is True
    assert winsched.task_is_managed("Tokdash", "other") is False


def test_task_is_managed_via_name_query_failure_returns_false(monkeypatch):
    monkeypatch.setattr(
        winsched, "_run", lambda args, timeout=20: subprocess.CompletedProcess([], 1, "", "not found")
    )
    assert winsched.task_is_managed("NotThere") is False


def test_write_task_writes_to_paths_location(monkeypatch, tmp_path):
    task_path = tmp_path / "nested" / "Tokdash.xml"
    monkeypatch.setattr(paths, "winsched_task_path", lambda: task_path)
    text = winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="x")
    out = winsched.write_task(text)
    assert out == task_path
    # Read the written file back as BYTES: the declared encoding must match the actual
    # byte encoding, because MSXML will not switch encodings once it has started.
    data = task_path.read_bytes()
    assert data.startswith(codecs.BOM_UTF16_LE)
    assert data.decode("utf-16") == text
    assert 'encoding="UTF-16"' in text.splitlines()[0]


# --- lifecycle commands -----------------------------------------------------------


def test_winsched_lifecycle_commands_allow_service_manager_timeout(monkeypatch, tmp_path):
    seen = []

    def fake_run(args, timeout=20):
        seen.append((args, timeout))
        return _ok_proc()

    monkeypatch.setattr(winsched, "_run", fake_run)
    task_path = tmp_path / "Tokdash.xml"
    task_path.write_text("<Task/>", encoding="utf-8")
    winsched.create(task_path)
    winsched.run_now()
    winsched.end_task()
    winsched.delete()
    assert seen == [
        (["/Create", "/TN", winsched.TASK_NAME, "/XML", str(task_path), "/F"], winsched.LIFECYCLE_TIMEOUT),
        (["/Run", "/TN", winsched.TASK_NAME], winsched.LIFECYCLE_TIMEOUT),
        (["/End", "/TN", winsched.TASK_NAME], winsched.LIFECYCLE_TIMEOUT),
        (["/Delete", "/TN", winsched.TASK_NAME, "/F"], winsched.LIFECYCLE_TIMEOUT),
    ]


def test_restart_ends_then_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(winsched, "end_task", lambda name=winsched.TASK_NAME: calls.append("end") or _ok_proc())
    monkeypatch.setattr(winsched, "run_now", lambda name=winsched.TASK_NAME: calls.append("run") or _ok_proc())
    winsched.restart()
    assert calls == ["end", "run"]


def test_restart_tolerates_end_task_failure(monkeypatch):
    def boom(name=winsched.TASK_NAME):
        raise subprocess.TimeoutExpired(["schtasks"], 5)

    monkeypatch.setattr(winsched, "end_task", boom)
    ran = []

    def fake_run_now(name=winsched.TASK_NAME):
        ran.append(True)
        return _ok_proc()

    monkeypatch.setattr(winsched, "run_now", fake_run_now)
    proc = winsched.restart()
    assert ran == [True] and proc.returncode == 0


def test_status_reports_registered_and_running(monkeypatch):
    monkeypatch.setattr(winsched, "is_registered", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_running", lambda name=winsched.TASK_NAME: True)
    assert winsched.status() == {"type": "winsched", "name": winsched.TASK_NAME, "enabled": True, "active": True}


def test_status_not_registered_skips_running_probe(monkeypatch):
    monkeypatch.setattr(winsched, "is_registered", lambda name=winsched.TASK_NAME: False)

    def boom(name=winsched.TASK_NAME):
        raise AssertionError("is_running should not be called when not registered")

    monkeypatch.setattr(winsched, "is_running", boom)
    assert winsched.status() == {"type": "winsched", "name": winsched.TASK_NAME, "enabled": False, "active": False}


# --- paths / detect (Windows-specific bits) ---------------------------------------


def test_winsched_task_path_uses_local_appdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "/fake/AppData/Local")
    assert paths.winsched_task_path() == Path("/fake/AppData/Local") / "Tokdash" / "Tokdash.xml"


def test_winsched_task_path_falls_back_without_local_appdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert paths.winsched_task_path() == Path("~/AppData/Local").expanduser() / "Tokdash" / "Tokdash.xml"


def test_managed_venv_python_windows_uses_scripts(monkeypatch, tmp_path):
    # Simulate the Windows branch via the `_windows_venv_layout` seam, NOT the real `os.name`
    # (flipping the real `os.name` would make pathlib try to build an unusable WindowsPath
    # for every other fresh Path(...) call on this non-Windows test host).
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "dd"))
    monkeypatch.setattr(paths, "_windows_venv_layout", lambda: True)
    assert paths.managed_venv_python() == tmp_path / "dd" / "runtime" / "python-venv" / "Scripts" / "python.exe"


def test_managed_venv_python_posix_unchanged(tmp_path, monkeypatch):
    # Explicitly force the POSIX branch so this remains meaningful on windows-latest too.
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "dd"))
    monkeypatch.setattr(paths, "_windows_venv_layout", lambda: False)
    assert paths.managed_venv_python() == tmp_path / "dd" / "runtime" / "python-venv" / "bin" / "python"


def test_winsched_available_requires_windows_and_schtasks(monkeypatch):
    monkeypatch.setattr(detect, "os_kind", lambda: "windows")
    monkeypatch.setattr(detect.shutil, "which", lambda name: "C:\\Windows\\System32\\schtasks.exe" if name == "schtasks" else None)
    assert detect.winsched_available() is True
    monkeypatch.setattr(detect.shutil, "which", lambda name: None)
    assert detect.winsched_available() is False


def test_winsched_available_false_on_non_windows(monkeypatch):
    monkeypatch.setattr(detect, "os_kind", lambda: "linux")
    monkeypatch.setattr(detect.shutil, "which", lambda name: "/usr/bin/schtasks")
    assert detect.winsched_available() is False


def test_existing_service_ignores_winsched_task_on_non_windows(monkeypatch, tmp_path):
    task_path = tmp_path / "Tokdash.xml"
    task_path.write_text(winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="x"), encoding="utf-8")
    monkeypatch.setattr(paths, "winsched_task_path", lambda: task_path)
    monkeypatch.setattr(detect, "os_kind", lambda: "linux")
    existing = detect.existing_service()
    assert existing["winsched_task"] is None


def test_pipx_tokdash_python_windows_scripts_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(detect, "os_kind", lambda: "windows")
    local_appdata = tmp_path / "AppData" / "Local"
    candidate = local_appdata / "pipx" / "venvs" / "tokdash" / "Scripts" / "python.exe"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.delenv("PIPX_HOME", raising=False)
    assert detect.pipx_tokdash_python() == str(candidate)


# --- select_service: windows matrix (service_base) --------------------------------


def test_select_service_windows_winsched_available():
    sel = service_base.select_service(
        "auto", "windows", no_service=False, systemd_available=False, launchd_available=False,
        winsched_available=True,
    )
    assert sel.result == {"type": "winsched", "reason": None}
    assert sel.blockers == [] and sel.notes == []


def test_select_service_windows_winsched_unavailable():
    sel = service_base.select_service(
        "auto", "windows", no_service=False, systemd_available=False, launchd_available=False,
        winsched_available=False,
    )
    assert sel.result == {"type": "none", "reason": "Task Scheduler (schtasks) is unavailable"}
    assert sel.blockers == []
    assert sel.notes == ["Task Scheduler (schtasks) is unavailable; falling back to foreground guidance."]


def test_select_service_windows_explicit_systemd_blocked():
    sel = service_base.select_service(
        "systemd", "windows", no_service=False, systemd_available=False, launchd_available=False,
    )
    assert sel.result == {"type": "none", "reason": "unsupported"}
    assert sel.blockers == ["--service systemd is not supported on windows."]


def test_backend_for_winsched_registered():
    assert service_base.backend_for("winsched") is winsched


# --- setup / uninstall planning (via monkeypatched schtasks) ----------------------


def test_windows_auto_uses_winsched(windows_env):
    p = plan.build_setup_plan(plan.Options(auto=True), detect.detect_all(55423))
    assert p["service"]["type"] == "winsched"


def test_windows_setup_writes_task_and_manifest(windows_env, fake_winsched, capsys):
    rc, payload = run_json(["setup", "--auto", "--service", "winsched", "--json"], capsys)
    assert rc == 0 and payload["service"]["type"] == "winsched"
    task_path = paths.winsched_task_path()
    assert task_path.is_file() and "X-Tokdash-Managed" in task_path.read_bytes().decode("utf-16")
    assert manifest.read_manifest()["service"]["type"] == "winsched"
    assert "service:winsched" in payload["changed"]


def test_windows_setup_refuses_unmarked_task(windows_env, fake_winsched):
    task_path = paths.winsched_task_path()
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("<Task></Task>", encoding="utf-8")
    rc = run(["setup", "--auto", "--service", "winsched"])
    assert rc == 1 and "X-Tokdash-Managed" not in task_path.read_text(encoding="utf-8")


def test_windows_setup_force_overwrites_unmarked_task(windows_env, fake_winsched):
    task_path = paths.winsched_task_path()
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("<Task></Task>", encoding="utf-8")
    rc = run(["setup", "--auto", "--service", "winsched", "--force"])
    assert rc == 0 and "X-Tokdash-Managed" in task_path.read_bytes().decode("utf-16")


def test_windows_uninstall_removes_winsched(windows_env, fake_winsched, capsys):
    run(["setup", "--auto", "--service", "winsched"])
    assert paths.winsched_task_path().is_file()
    rc, payload = run_json(["uninstall", "--auto", "--json"], capsys)
    assert rc == 0 and "service" in payload["changed"]
    assert not paths.winsched_task_path().exists()


def test_windows_uninstall_fails_closed_when_delete_fails_and_still_registered(windows_env, fake_winsched, monkeypatch):
    run(["setup", "--auto", "--service", "winsched"])
    monkeypatch.setattr(winsched, "delete", lambda name=winsched.TASK_NAME: subprocess.CompletedProcess([], 1, "", "denied"))
    monkeypatch.setattr(winsched, "is_registered_strict", lambda name=winsched.TASK_NAME: True)
    rc = run(["uninstall", "--auto"])
    assert rc == 1
    # The task file must still be on disk (unlink happens only after a confirmed-safe delete).
    assert paths.winsched_task_path().is_file()


def test_windows_doctor_reports_winsched(windows_env, fake_winsched, capsys):
    run(["setup", "--auto", "--service", "winsched"])
    rc, payload = run_json(["doctor", "--json"], capsys)
    assert payload["winsched"] is True
    assert payload["service"]["type"] == "winsched"
    assert payload["service"]["enabled"] is True and payload["service"]["active"] is True
    # The autouse `_isolate` fixture stubs detect.probe_port to always report closed/not-tokdash,
    # so doctor correctly flags that (stubbed) mismatch -- mirrors
    # test_doctor_flags_active_service_without_tokdash_port for systemd in test_onboard.py.
    assert rc == 1 and any("not answering" in i for i in payload["issues"])


def test_windows_update_restarts_winsched(windows_env, fake_winsched, monkeypatch, capsys):
    svc = {"type": "winsched", "unit": str(paths.winsched_task_path()), "name": winsched.TASK_NAME,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    man = manifest.build_manifest(
        install_method="pipx", runtime_kind="pipx", runtime_command=["/p/python", "-m", "tokdash"],
        runtime_owned_by_setup=False, python_path="/p/python", python_version="3.12.0", service=svc,
        runtime_marker=None, data_dir=str(paths.data_dir()), bind="127.0.0.1", port=55423,
    )
    manifest.write_manifest(man)
    monkeypatch.setattr(detect, "find_pipx", lambda: "/usr/bin/pipx")
    monkeypatch.setattr(
        engine.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr=""),
    )
    restarted = []

    def fake_restart(name=winsched.TASK_NAME):
        restarted.append(True)
        return _ok_proc()

    monkeypatch.setattr(winsched, "restart", fake_restart)
    rc, payload = run_json(["update", "--json"], capsys)
    assert rc == 0 and payload["service_restarted"] is True and restarted == [True]


def test_cli_service_choices_include_winsched():
    args = cli.build_parser("tokdash").parse_args(["setup", "--service", "winsched", "--auto"])
    assert args.service == "winsched"

# --- per-user LogonTrigger (standard-user registration) ----------------------------


def test_task_logon_trigger_names_current_user(monkeypatch):
    # A bare <LogonTrigger> means "any user's logon" — a system-wide event Task
    # Scheduler refuses to register from a standard user (Access denied). The
    # trigger must name the invoking user to stay registerable non-elevated.
    monkeypatch.setenv("USERDOMAIN", "HOWARD")
    monkeypatch.setenv("USERNAME", "H1937")
    text = winsched.render_task(["C:\\rt\\python.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="x")
    trig = text.split("<LogonTrigger>")[1].split("</LogonTrigger>")[0]
    assert "<UserId>HOWARD\\H1937</UserId>" in trig


def test_task_logon_trigger_uses_bare_username_without_domain(monkeypatch):
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.setenv("USERNAME", "H1937")
    text = winsched.render_task(["C:\\rt\\python.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="x")
    assert "<UserId>H1937</UserId>" in text


def test_task_logon_trigger_falls_back_without_user(monkeypatch):
    # Non-interactive context without USERNAME: degrade to the bare trigger
    # (pre-fix behaviour, needs elevation) rather than render an invalid definition.
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    text = winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="x")
    assert "<UserId>" not in text


def test_task_logon_trigger_escapes_user(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "DOM&CO")
    monkeypatch.setenv("USERNAME", "U")
    text = winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="x")
    assert "<UserId>DOM&amp;CO\\U</UserId>" in text


def test_windows_setup_stops_previous_instance_before_start(windows_env, monkeypatch, capsys):
    # A running instance of a previously registered task holds the port and
    # MultipleInstancesPolicy=IgnoreNew would swallow the fresh /Run — setup must
    # stop it before registering/starting the replacement.
    calls = []

    def _record(tag, proc):
        calls.append(tag)
        return proc

    monkeypatch.setattr(winsched, "create", lambda task_path, name=winsched.TASK_NAME: _record("create", _ok_proc()))
    monkeypatch.setattr(winsched, "delete", lambda name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "run_now", lambda name=winsched.TASK_NAME: _record("run", _ok_proc()))
    monkeypatch.setattr(winsched, "end_task", lambda name=winsched.TASK_NAME: _record("end", _ok_proc()))
    monkeypatch.setattr(winsched, "is_registered", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_registered_strict", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_running", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {"ok": True, "port": {"port": port, "open": True, "is_tokdash": True, "version": "test"}},
    )
    rc, payload = run_json(["setup", "--auto", "--service", "winsched", "--json"], capsys)
    assert rc == 0
    assert calls.index("end") < calls.index("create") < calls.index("run")


def test_windows_setup_create_access_denied_suggests_elevation(windows_env, monkeypatch, capsys):
    def _denied(task_path, name=winsched.TASK_NAME):
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="ERROR: Access is denied.")

    monkeypatch.setattr(winsched, "create", _denied)
    monkeypatch.setattr(winsched, "end_task", lambda name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "is_registered", lambda name=winsched.TASK_NAME: True)
    rc, payload = run_json(["setup", "--auto", "--service", "winsched", "--json"], capsys)
    assert rc != 0
    assert "schtasks /Create failed" in payload["error"]
    assert "elevated PowerShell" in payload["error"]


def test_readiness_refuses_foreign_responder(monkeypatch):
    # Port answers with Tokdash's fingerprint (e.g. a WSL distro's mirrored
    # localhost) but our own service is not running: readiness must not pass.
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "1.9.0"},
    )
    r = engine._wait_for_service_ready(
        "127.0.0.1", 55423, timeout=0.5, service_up=lambda: False, service_desc="the Task Scheduler task Tokdash"
    )
    assert r["ok"] is False
    assert "foreign/relayed" in r["error"]


def test_readiness_refuses_wrong_holder(monkeypatch):
    # Fingerprint + task "Running" is not enough: the port must be owned by the
    # service's runtime, not a relay/foreign process answering on its behalf.
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "1.9.0"},
    )
    monkeypatch.setattr(detect, "port_holder_process", lambda port: "wslrelay.exe")
    r = engine._wait_for_service_ready(
        "127.0.0.1", 55423, timeout=0.5, service_up=lambda: True, expected_holders={"pythonw.exe"}
    )
    assert r["ok"] is False
    assert "wslrelay.exe" in r["error"] and "pythonw.exe" in r["error"]


def test_readiness_accepts_expected_holder(monkeypatch):
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "2.0.0"},
    )
    monkeypatch.setattr(detect, "port_holder_process", lambda port: "pythonw.exe")
    r = engine._wait_for_service_ready(
        "127.0.0.1", 55423, timeout=0.5, service_up=lambda: True,
        expected_holders={"pythonw.exe", "python.exe"},
    )
    assert r["ok"] is True


def test_readiness_ignores_unknown_holder(monkeypatch):
    # Holder lookup failed (None) -> the gate must not veto a healthy service.
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "2.0.0"},
    )
    monkeypatch.setattr(detect, "port_holder_process", lambda port: None)
    r = engine._wait_for_service_ready(
        "127.0.0.1", 55423, timeout=0.5, service_up=lambda: True, expected_holders={"pythonw.exe"}
    )
    assert r["ok"] is True


def test_readiness_passes_when_service_running(monkeypatch):
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "2.0.0"},
    )
    r = engine._wait_for_service_ready("127.0.0.1", 55423, timeout=0.5, service_up=lambda: True)
    assert r["ok"] is True


# --- re-setup: stale-instance port release (uvicorn exit-3 race) -----------------
#
# Live-verified failure: schtasks /End is a *request* — the previous pythonw kept the
# port while the replacement was registered and /Run fired; the fresh instance exited
# with uvicorn's startup-failure code 3 ("address in use") and readiness passed on the
# stale responder's fingerprint. The port release must be a precondition of (re)start,
# and a lingering own-runtime holder must be hard-killed by PID.


def test_port_release_wait_passes_when_port_free(monkeypatch):
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": False, "is_tokdash": False, "version": None},
    )
    assert engine._wait_for_port_release(55423, kill_names={"pythonw.exe"}, timeout=1.0) is None


def test_port_release_wait_kills_lingering_own_holder(monkeypatch):
    # The old pythonw outlived schtasks /End: the port still answers and the holder is
    # our runtime -> taskkill /F by PID; the wait returns once the port frees.
    state = {"killed": False}
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": not state["killed"], "is_tokdash": True, "version": "2.0.0"},
    )
    monkeypatch.setattr(detect, "port_holder", lambda port: (4242, "pythonw.exe"))
    kills = []

    def fake_run(args, *a, **k):
        kills.append(args)
        if args[:1] == ["taskkill"]:
            state["killed"] = True
        return _ok_proc()

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine._wait_for_port_release(55423, kill_names={"pythonw.exe", "python.exe"}, timeout=2.0) is None
    assert kills == [["taskkill", "/PID", "4242", "/F"]]


def test_port_release_wait_never_kills_foreign_holder(monkeypatch):
    # A non-runtime occupant (wslrelay etc.) is reported, never killed.
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "1.9.0"},
    )
    monkeypatch.setattr(detect, "port_holder", lambda port: (777, "wslrelay.exe"))

    def boom(*a, **k):
        raise AssertionError("taskkill must not run for a foreign holder")

    monkeypatch.setattr(engine.subprocess, "run", boom)
    err = engine._wait_for_port_release(55423, kill_names={"pythonw.exe"}, timeout=0.8)
    assert err is not None and "still busy" in err and "wslrelay.exe" in err


def test_port_release_wait_without_kill_names_never_kills(monkeypatch):
    # No kill authority (a port this install does not own): report, never touch.
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": True, "is_tokdash": True, "version": "2.0.0"},
    )
    monkeypatch.setattr(detect, "port_holder", lambda port: (4242, "pythonw.exe"))

    def boom(*a, **k):
        raise AssertionError("taskkill must not run without kill_names")

    monkeypatch.setattr(engine.subprocess, "run", boom)
    err = engine._wait_for_port_release(55423, timeout=0.8)
    assert err is not None and "still busy" in err and "PID 4242" in err


def test_runtime_holder_names_accepts_pythonw_twin():
    assert engine._runtime_holder_names({"command": [r"C:\rt\python.exe", "-m", "tokdash"]}) == {
        "python.exe", "pythonw.exe", "pythonw"
    }
    assert engine._runtime_holder_names({"command": [r"C:\rt\tokdash.exe"]}) == {"tokdash.exe"}
    assert engine._runtime_holder_names(None) is None


def test_windows_resetup_kills_stale_holder_before_starting_replacement(windows_env, monkeypatch, capsys):
    calls = []
    state = {"killed": False}
    monkeypatch.setattr(
        detect, "probe_port",
        lambda port=55423, *a, **k: {"port": port, "open": not state["killed"], "is_tokdash": True, "version": "2.0.0"},
    )
    monkeypatch.setattr(detect, "port_holder", lambda port: (4242, "pythonw.exe"))

    def fake_run(args, *a, **k):
        if args[:1] == ["taskkill"]:
            calls.append("kill")
            state["killed"] = True
        return _ok_proc()

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    monkeypatch.setattr(winsched, "create", lambda task_path, name=winsched.TASK_NAME: calls.append("create") or _ok_proc())
    monkeypatch.setattr(winsched, "delete", lambda name=winsched.TASK_NAME: _ok_proc())
    monkeypatch.setattr(winsched, "run_now", lambda name=winsched.TASK_NAME: calls.append("run") or _ok_proc())
    monkeypatch.setattr(winsched, "end_task", lambda name=winsched.TASK_NAME: calls.append("end") or _ok_proc())
    monkeypatch.setattr(winsched, "is_registered", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_registered_strict", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(winsched, "is_running", lambda name=winsched.TASK_NAME: True)
    monkeypatch.setattr(
        engine,
        "_wait_for_service_ready",
        lambda bind, port, **k: {"ok": True, "port": {"port": port, "open": True, "is_tokdash": True, "version": "test"}},
    )
    # The runtime the plan sees must be a Windows venv python.exe (the test host's
    # interpreter would not match the holder's image name).
    monkeypatch.setattr(
        runtime, "resolve",
        lambda flag, detection: {
            "kind": "existing", "install_method": "manual",
            "command": [r"C:\rt\python.exe", "-m", "tokdash"], "python": r"C:\rt\python.exe",
            "owned_by_setup": False, "needs_create": False, "error": None,
        },
    )

    # A previous install of ours recorded exactly this port and its marked task exists.
    task_path = paths.winsched_task_path()
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_bytes(
        codecs.BOM_UTF16_LE
        + winsched.render_task([r"C:\rt\python.exe", "-m", "tokdash"], "127.0.0.1", 55423, marker_id="x").encode("utf-16-le")
    )
    svc = {"type": "winsched", "unit": str(task_path), "name": winsched.TASK_NAME,
           "created_by_setup": True, "marker": "X-Tokdash-Managed id=x"}
    man = manifest.build_manifest(
        install_method="manual", runtime_kind="existing",
        runtime_command=[r"C:\rt\python.exe", "-m", "tokdash"],
        runtime_owned_by_setup=False, python_path=r"C:\rt\python.exe", python_version="3.13.5",
        service=svc, runtime_marker=None, data_dir=str(paths.data_dir()),
        bind="127.0.0.1", port=55423,
    )
    manifest.write_manifest(man)

    rc, payload = run_json(["setup", "--auto", "--service", "winsched", "--json"], capsys)
    assert rc == 0
    assert payload["port"] == 55423
    assert calls.index("end") < calls.index("kill") < calls.index("create") < calls.index("run")
