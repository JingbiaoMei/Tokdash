"""Tier 2: native Windows Task Scheduler backend (``winsched``).

Mirrors the systemd/launchd coverage in ``test_onboard.py`` — rendering, marker detection,
lifecycle commands, and setup/uninstall *planning* — all via monkeypatched ``schtasks``
(exactly how ``test_onboard.py`` monkeypatches ``systemctl``/``launchctl``). There is no
Windows machine in this environment, so nothing here executes a real ``schtasks``; real
Windows execution is deferred to CI.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tokdash import cli
from tokdash.onboard import detect, engine, manifest, paths, plan, service_base, winsched
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
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
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
    task_path = tmp_path / "Tokdash.xml"
    task_path.write_text(
        winsched.render_task(["py.exe", "-m", "tokdash"], "127.0.0.1", 1, marker_id="deadbeef"),
        encoding="utf-8",
    )
    assert winsched.task_is_managed(task_path) is True
    assert winsched.task_is_managed(task_path, "deadbeef") is True
    assert winsched.task_is_managed(task_path, "other") is False

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
    assert task_path.read_text(encoding="utf-8") == text


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
    assert task_path.is_file() and "X-Tokdash-Managed" in task_path.read_text(encoding="utf-8")
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
    assert rc == 0 and "X-Tokdash-Managed" in task_path.read_text(encoding="utf-8")


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
