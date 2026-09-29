"""Dashboard click-to-update: eligibility, journal, auth, API, helper, mechanics.

Every test is offline and filesystem-sandboxed: ``TOKDASH_DATA_DIR`` points at a tmp
dir, PyPI discovery is monkeypatched, and nothing here starts systemd, pip, or the
real updater. The helper is exercised through its injectable ``Runner`` seam.
"""
import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import tokdash.api as api
from tokdash.onboard import (
    manifest as manifest_mod,
    paths,
    update_auth,
    update_control,
    update_eligibility,
    update_helper,
    update_jobs,
    update_mechanics,
    updatecheck,
)

HOST = "127.0.0.1:55423"
TAILNET_HOST = "wsl.tail76535.ts.net"
TAILNET_ORIGIN = "https://wsl.tail76535.ts.net"


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TOKDASH_UPDATE_ORIGIN", raising=False)
    monkeypatch.delenv("TOKDASH_USAGE_DB_PATH", raising=False)
    yield tmp_path


@pytest.fixture()
def loopback_app():
    api.app.state.bind = "127.0.0.1"
    api.app.state.port = 55423
    yield
    api.app.state.bind = None
    api.app.state.port = None


def managed_manifest(tmp_path, marker_id="abcd1234"):
    unit = tmp_path / "tokdash.service"
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    if not py.exists():
        py.write_text("#!/bin/sh\n", encoding="utf-8")
    marker = manifest_mod.marker_token(marker_id)
    unit.write_text(
        "[Service]\n"
        f"# {marker}\n"
        f"ExecStart={tmp_path}/venv/bin/python -m tokdash serve --bind 127.0.0.1 --port 55423 --no-open\n",
        encoding="utf-8",
    )
    data = manifest_mod.build_manifest(
        install_method="pipx",
        runtime_kind="pipx",
        runtime_command=[f"{tmp_path}/venv/bin/python", "-m", "tokdash"],
        runtime_owned_by_setup=False,
        python_path=f"{tmp_path}/venv/bin/python",
        python_version="3.12.0",
        service={
            "type": "systemd-user",
            "unit": str(unit),
            "name": "tokdash",
            "created_by_setup": True,
            "marker": marker,
        },
        runtime_marker=None,
        data_dir=str(tmp_path),
        bind="127.0.0.1",
        port=55423,
    )
    manifest_mod.write_manifest(data)
    return data, marker


def stub_who_runs_the_service(monkeypatch, py, *, bind="127.0.0.1", port=55423, loaded=True, in_cgroup=True):
    """Make the live-system probes in update_eligibility answer about this tmp env:
    the LOADED ExecStart argv and this process's membership in the service cgroup.
    (The argv-level parse of systemd's real structured output is covered separately
    by a test against a captured live response.)"""
    if loaded:
        monkeypatch.setattr(
            update_eligibility, "_loaded_execstart",
            lambda unit: [str(py), "-m", "tokdash", "serve", "--bind", bind, "--port", str(port)],
        )
    else:
        monkeypatch.setattr(update_eligibility, "_loaded_execstart", lambda unit: None)
    monkeypatch.setattr(update_eligibility, "_process_in_service_cgroup", lambda unit: in_cgroup)


@pytest.fixture()
def eligible_env(tmp_path, monkeypatch):
    """A manifest + unit that pass eligibility; interpreter file must exist too."""
    data, marker = managed_manifest(tmp_path)
    py = Path(data["python_path"])
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    stub_who_runs_the_service(monkeypatch, py)
    return data, marker


# --- mechanics --------------------------------------------------------------------


def test_valid_target_shape():
    assert update_mechanics.valid_target_version("2.7.0")
    assert update_mechanics.valid_target_version("2.7.0rc1")
    assert not update_mechanics.valid_target_version("2.7.0; rm -rf /")
    assert not update_mechanics.valid_target_version("latest")
    assert not update_mechanics.valid_target_version("")
    assert not update_mechanics.valid_target_version(None)


def test_validate_target_rules():
    assert update_mechanics.validate_target("2.7.0", current="2.6.6") is None
    assert "not newer" in update_mechanics.validate_target("2.6.6", current="2.6.6")
    assert "not newer" in update_mechanics.validate_target("2.6.5", current="2.6.6")
    assert "not the available release" in update_mechanics.validate_target("2.7.0", current="2.6.6", latest="2.7.1")
    assert "required" in update_mechanics.validate_target(None, current="2.6.6")


def test_install_argv_pinned_and_unpinned():
    unpinned = update_mechanics.install_argv("pipx", "/p/python", None)
    assert unpinned == ["pipx", "upgrade", "tokdash"]
    pinned = update_mechanics.install_argv("pipx", "/p/python", "2.7.0")
    assert pinned == ["/p/python", "-m", "pip", "install", "--no-input", "tokdash==2.7.0"]
    venv = update_mechanics.install_argv("managed-venv", "/v/python", "2.7.0")
    assert venv == ["/v/python", "-m", "pip", "install", "--no-input", "tokdash==2.7.0"]
    assert update_mechanics.install_argv("conda", "/c/python", None) is None
    assert update_mechanics.install_argv("pipx", "", "2.7.0") is None


def test_safe_install_argv():
    assert update_mechanics.safe_install_argv(["/p/python", "-m", "pip", "install", "--no-input", "tokdash==2.7.0"])
    assert not update_mechanics.safe_install_argv(["/p/python", "-m", "pip", "install", "tokdash>=2.0"])
    assert not update_mechanics.safe_install_argv(["/p/python", "-m", "pip", "install", "evil==1.0"])
    assert not update_mechanics.safe_install_argv(["rm", "-rf", "/"])
    assert not update_mechanics.safe_install_argv(["/p/python", "-m", "pip", "install", "tokdash==2.7.0; rm -rf /"])
    assert not update_mechanics.safe_install_argv("not a list")


# --- eligibility --------------------------------------------------------------------


def test_eligible_pipx(eligible_env):
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is True, result["reason"]
    assert result["method"] == "pipx"


def test_ineligible_missing_manifest(data_dir, monkeypatch):
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False
    assert "manifest" in result["reason"]


def test_ineligible_existing_runtime(tmp_path, monkeypatch):
    data, _ = managed_manifest(tmp_path)
    data["install_method"] = "existing"
    manifest_mod.write_manifest(data)
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False
    assert "did not create" in result["reason"]


def test_ineligible_foreground_service(tmp_path, monkeypatch):
    data, _ = managed_manifest(tmp_path)
    data["service"] = None
    manifest_mod.write_manifest(data)
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False


def test_ineligible_unmarked_unit(eligible_env):
    data, _ = eligible_env
    Path(data["service"]["unit"]).write_text("[Service]\nExecStart=x\n", encoding="utf-8")
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False
    assert "marker" in result["reason"] or "match" in result["reason"]


def test_ineligible_runtime_drift(eligible_env):
    data, _ = eligible_env
    py = Path(data["python_path"])
    py.unlink()
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False


def test_ineligible_foreign_data_dir(eligible_env, tmp_path):
    data, _ = eligible_env
    data["data_dir"] = str(tmp_path / "elsewhere")
    manifest_mod.write_manifest(data)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False


def test_ineligible_no_systemd(eligible_env, monkeypatch):
    eligible_env  # noqa: B018 - fixture side effect is the point
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: False)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False


def test_launchd_is_manual_for_now(tmp_path, monkeypatch):
    data, _ = managed_manifest(tmp_path)
    py = Path(data["python_path"])
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    data["service"]["type"] = "launchd"
    manifest_mod.write_manifest(data)
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False
    assert "launchd" in result["reason"]


def test_managed_venv_requires_marker(tmp_path, monkeypatch):
    data, _ = managed_manifest(tmp_path)
    py = Path(data["python_path"])
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    data["install_method"] = "managed-venv"
    manifest_mod.write_manifest(data)
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    stub_who_runs_the_service(monkeypatch, py)
    result = update_eligibility.check_eligibility()
    assert result["eligible"] is False
    assert "marker" in result["reason"]
    paths.runtime_marker_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_marker_path().write_text("created-by=tokdash-setup\n", encoding="utf-8")
    assert update_eligibility.check_eligibility()["eligible"] is True


# --- eligibility: LOADED service proof (§3 — a manifest-selected file proves nothing) ---


def test_eligibility_rejects_unrelated_app(eligible_env, monkeypatch):
    data, _ = eligible_env
    py = Path(data["python_path"])
    monkeypatch.setattr(
        update_eligibility, "_loaded_execstart",
        lambda unit: [str(py), "-m", "totally_unrelated_app", "serve",
                      "--bind", "127.0.0.1", "--port", "55423"],
    )
    r = update_eligibility.check_eligibility()
    assert not r["eligible"]
    assert "Tokdash" in r["reason"]


def test_eligibility_rejects_foreign_interpreter(eligible_env, monkeypatch):
    monkeypatch.setattr(
        update_eligibility, "_loaded_execstart",
        lambda unit: ["/usr/bin/python3", "-m", "tokdash", "serve",
                      "--bind", "127.0.0.1", "--port", "55423"],
    )
    r = update_eligibility.check_eligibility()
    assert not r["eligible"]
    assert "interpreter" in r["reason"]


def test_eligibility_requires_running_managed_instance(eligible_env, monkeypatch):
    # A dev run against the same data dir must not drive the managed service.
    monkeypatch.setattr(update_eligibility, "_process_in_service_cgroup", lambda unit: False)
    r = update_eligibility.check_eligibility()
    assert not r["eligible"]
    assert "managed service" in r["reason"]


def test_eligibility_rejects_base_interpreter_behind_venv_symlink(eligible_env, monkeypatch):
    # venv bin/python is normally a symlink to a shared base interpreter: comparing
    # realpaths would call /usr/bin/python3 and the recorded venv identical. The
    # LOADED ExecStart must name the recorded venv path itself.
    monkeypatch.setattr(
        update_eligibility, "_loaded_execstart",
        lambda unit: ["/usr/bin/python3", "-m", "tokdash", "serve",
                      "--bind", "127.0.0.1", "--port", "55423"],
    )
    r = update_eligibility.check_eligibility()
    assert not r["eligible"]
    assert "interpreter" in r["reason"]


def test_cgroup_membership_proves_identity(monkeypatch):
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice/tokdash.service"
    monkeypatch.setattr(update_eligibility, "_service_control_group", lambda unit: cg.lstrip("/"))

    def self_cgroup(path):
        monkeypatch.setattr(update_eligibility, "_read_self_cgroup", lambda: f"0::/{path}\n")

    self_cgroup(cg.lstrip("/"))
    assert update_eligibility._process_in_service_cgroup("tokdash.service") is True
    self_cgroup(cg.lstrip("/") + "/worker-7.scope")  # descendants belong too
    assert update_eligibility._process_in_service_cgroup("tokdash.service") is True
    self_cgroup("/user.slice/user-1000.slice/user@1000.service/app.slice/tokdash2.service")
    assert update_eligibility._process_in_service_cgroup("tokdash.service") is False
    self_cgroup("/init.scope")  # a login shell
    assert update_eligibility._process_in_service_cgroup("tokdash.service") is False
    monkeypatch.setattr(update_eligibility, "_service_control_group", lambda unit: None)
    assert update_eligibility._process_in_service_cgroup("tokdash.service") is False


# systemd's LOADED ExecStart on this machine's installer (captured live from
# `systemctl --user show tokdash.service -p ExecStart --value`) — the structured form
# real systemd emits. Parsing it as a plain command line would make "{" the executable.
CAPTURED_STRUCTURED_EXECSTART = (
    "{ path=/home/howard/.local/share/pipx/venvs/tokdash/bin/python ; "
    "argv[]=/home/howard/.local/share/pipx/venvs/tokdash/bin/python -m tokdash serve "
    "--bind 127.0.0.1 --port 55423 --no-open ; ignore_errors=no ; start_time=[n/a] ; "
    "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
)


def test_parses_captured_systemd_structured_response():
    argv = update_eligibility._parse_execstart_line(CAPTURED_STRUCTURED_EXECSTART)
    assert argv is not None
    assert argv[0] == "/home/howard/.local/share/pipx/venvs/tokdash/bin/python"
    assert argv[1:5] == ["-m", "tokdash", "serve", "--bind"]
    assert argv[-2:] == ["--port", "55423"] or "--no-open" in argv  # full argv survives
    assert "{" not in "".join(argv)


def test_legacy_plain_execstart_still_parses():
    argv = update_eligibility._parse_execstart_line(
        "/home/u/.local/share/pipx/venvs/tokdash/bin/python -m tokdash serve --bind 127.0.0.1 --port 55423"
    )
    assert argv and argv[1:4] == ["-m", "tokdash", "serve"]


def test_helper_parser_matches_eligibility_parser():
    # The staged helper cannot import the eligibility parser; the mirror must agree.
    for sample in (CAPTURED_STRUCTURED_EXECSTART,
                   "/venv/bin/python -m tokdash serve --bind 127.0.0.1 --port 55423"):
        assert update_helper._parse_execstart_line(sample) == update_eligibility._parse_execstart_line(sample), sample


def test_eligibility_end_to_end_with_captured_systemd_output(tmp_path, monkeypatch):
    # Full path: captured raw systemctl text -> parser -> checks. No argv stubbing.
    data, _ = managed_manifest(tmp_path)
    py = str(Path(data["python_path"]))
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: True)
    monkeypatch.setattr(update_eligibility, "_process_in_service_cgroup", lambda unit: True)
    captured = CAPTURED_STRUCTURED_EXECSTART.replace(
        "/home/howard/.local/share/pipx/venvs/tokdash/bin/python", py)
    monkeypatch.setattr(
        update_eligibility.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, captured if "ExecStart" in cmd else "0\n", ""),
    )
    r = update_eligibility.check_eligibility()
    assert r["eligible"] is True, r["reason"]


def test_eligibility_requires_loaded_unit(eligible_env, monkeypatch):
    data, _ = eligible_env
    stub_who_runs_the_service(monkeypatch, Path(data["python_path"]), loaded=False)
    r = update_eligibility.check_eligibility()
    assert not r["eligible"]
    assert "not loaded" in r["reason"]


def test_eligibility_rejects_port_drift(eligible_env, monkeypatch):
    data, _ = eligible_env
    py = Path(data["python_path"])
    monkeypatch.setattr(
        update_eligibility, "_loaded_execstart",
        lambda unit: [str(py), "-m", "tokdash", "serve", "--bind", "127.0.0.1", "--port", "9999"],
    )
    r = update_eligibility.check_eligibility()
    assert not r["eligible"]
    assert "--port" in r["reason"]


# --- job journal ----------------------------------------------------------------------


def test_job_create_and_attach():
    job, created = update_jobs.create_job(
        to_version="2.7.0", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p", "-m", "pip", "install", "--no-input", "tokdash==2.7.0"],
        service_type="systemd-user", service_name="tokdash", service_marker="X-Tokdash-Managed id=1",
        python_path="/p", usage_db=None,
    )
    assert created and job["phase"] == "accepted"
    again, created2 = update_jobs.create_job(
        to_version="2.7.0", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p", "-m", "pip", "install", "--no-input", "tokdash==2.7.0"],
        service_type="systemd-user", service_name="tokdash", service_marker="m",
        python_path="/p", usage_db=None,
    )
    assert not created2 and again["id"] == job["id"]  # attach, never double-apply


def test_stale_job_is_reconciled():
    job, _ = update_jobs.create_job(
        to_version="2.7.0", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
        service_marker="m", python_path="/p", usage_db=None,
    )
    # Age the job past the heartbeat window and reconcile.
    with update_jobs.with_update_lock():
        data = update_jobs._load()
        data["jobs"][job["id"]]["updated_at"] = "2020-01-01T00:00:00Z"
        update_jobs._store(data)
    latest = update_jobs.latest_job()
    assert latest["phase"] == "failed"
    assert latest["failed_phase"] == "interrupted"
    # The dead job must no longer block admission.
    fresh, created = update_jobs.create_job(
        to_version="2.7.1", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
        service_marker="m", python_path="/p", usage_db=None,
    )
    assert created and fresh["id"] != job["id"]


def test_public_view_hides_local_paths():
    job, _ = update_jobs.create_job(
        to_version="2.7.0", from_version="2.6.6", trigger="dashboard",
        install_argv=["/secret/venv/bin/python", "-m", "pip", "install", "--no-input", "tokdash==2.7.0"],
        service_type="systemd-user", service_name="tokdash", service_marker="m",
        python_path="/secret/venv/bin/python", usage_db="/secret/usage.sqlite3",
    )
    view = update_jobs.public_view(job)
    blob = json.dumps(view)
    assert "/secret" not in blob
    assert "install_argv" not in view
    assert view["to_version"] == "2.7.0"


def test_set_phase_terminal():
    job, _ = update_jobs.create_job(
        to_version="2.7.0", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
        service_marker="m", python_path="/p", usage_db=None,
    )
    update_jobs.set_phase(job["id"], "installing")
    assert update_jobs.get_job(job["id"])["phase"] == "installing"
    update_jobs.set_phase(job["id"], "succeeded", result_version="2.7.0")
    done = update_jobs.get_job(job["id"])
    assert done["phase"] == "succeeded" and done["result_version"] == "2.7.0"


def _mk_job(trigger="dashboard"):
    job, _ = update_jobs.create_job(
        to_version="2.7.0", from_version="2.6.6", trigger=trigger,
        install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
        service_marker="m", python_path="/p", usage_db=None,
    )
    return job


def test_journal_calls_nest_under_the_outer_update_lock():
    # The CLI holds update.lock across the whole apply while journal functions inside
    # it take the same lock again; flock is per-fd, so without the in-process guard
    # the CLI would deadlock against itself waiting for its own lock.
    with update_jobs.with_update_lock():
        job, created = update_jobs.create_job(
            to_version="2.7.0", from_version="2.6.6", trigger="cli",
            install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
            service_marker="m", python_path="/p", usage_db=None,
        )
        assert created
        update_jobs.set_phase(job["id"], "stopping")
        assert update_jobs.get_job(job["id"])["phase"] == "stopping"
    assert update_jobs.latest_job()["id"] == job["id"]


def test_reads_answer_while_a_runner_holds_the_lock():
    # The helper holds update.lock for the entire (minutes-long) apply, and status
    # polling must read the journal while it works -- a blocking read there would
    # stall every poll until the update finished.
    job = _mk_job()
    with update_jobs.with_update_lock():
        assert update_jobs.latest_job()["id"] == job["id"]
        assert update_jobs.get_job(job["id"])["phase"] == "accepted"


def test_journal_updates_are_thread_safe(data_dir):
    # The helper's heartbeat thread and phase machine hit the same read-modify-write
    # file; unsynchronized writers could resurrect a stale phase or corrupt the store.
    import threading

    job = _mk_job()
    journal = update_helper.Journal(str(data_dir), lock_free=True)
    errors = []

    def hammer(tag):
        for i in range(30):
            try:
                journal.update(job["id"], message=f"{tag}-{i}")
            except Exception as exc:  # pragma: no cover - failure path detail
                errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = update_jobs.get_job(job["id"])
    assert final["phase"] == "accepted"  # timestamp-only writes never move the phase
    assert "-" in (final["message"] or "")  # one whole write survived, not a torn blend


def test_stale_reconcile_never_blocks_behind_a_live_lock(data_dir):
    # An async request handler must never wait on update.lock: a live updater holding
    # it means the "stale" job is NOT interrupted — reconciliation skips, not stalls.
    fcntl = pytest.importorskip("fcntl")
    import os

    job = _mk_job()
    with update_jobs.with_update_lock():
        data = update_jobs._load()
        data["jobs"][job["id"]]["updated_at"] = "2020-01-01T00:00:00Z"
        update_jobs._store(data)
    fd = os.open(str(update_jobs.update_lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        latest = update_jobs.latest_job()  # would hang forever on a blocking reconcile
        assert latest["phase"] == "accepted"  # untouched: holder is a live updater
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- auth -------------------------------------------------------------------------------


def test_origin_config_and_matching(monkeypatch, data_dir):
    assert update_auth.configured_origin() is None
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", f"{TAILNET_ORIGIN}/tokdash")  # path → invalid
    assert update_auth.configured_origin() is None
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    assert update_auth.configured_origin() == TAILNET_ORIGIN
    assert update_auth.origin_matches(TAILNET_ORIGIN, host_header=TAILNET_HOST)
    assert update_auth.origin_matches(TAILNET_ORIGIN, host_header=TAILNET_HOST, origin_header=TAILNET_ORIGIN)
    assert not update_auth.origin_matches(TAILNET_ORIGIN, host_header="other.tail76535.ts.net")
    assert not update_auth.origin_matches(TAILNET_ORIGIN, host_header=TAILNET_HOST, origin_header="https://evil.com")
    assert not update_auth.origin_matches(TAILNET_ORIGIN, host_header=f"{TAILNET_HOST}.evil.com")


def test_origin_from_config_file(data_dir):
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(json.dumps({"update_origin": TAILNET_ORIGIN}), encoding="utf-8")
    assert update_auth.configured_origin() == TAILNET_ORIGIN


def test_pairing_code_single_use():
    code = update_auth.create_pairing_code()
    assert update_auth.redeem_pairing_code(f" {code.lower()} ")  # canonicalization
    assert not update_auth.redeem_pairing_code(code)  # single use
    assert not update_auth.redeem_pairing_code("WRONGCODE1")


def test_pairing_rate_limit():
    code = update_auth.create_pairing_code()
    for _ in range(update_auth.MAX_ATTEMPTS):
        update_auth.redeem_pairing_code("BADCODE12")
    assert update_auth.enrollment_locked()
    assert not update_auth.redeem_pairing_code(code)  # locked even with the right code


def test_sessions_survive_and_expire():
    import os

    os.environ["TOKDASH_UPDATE_ORIGIN"] = TAILNET_ORIGIN
    try:
        session = update_auth.create_session()
        assert session
        token = session["token"]
        assert update_auth.validate_session(token)
        assert update_auth.validate_session(token, csrf=session["csrf"])
        assert not update_auth.validate_session(token, csrf="wrong")
        assert not update_auth.validate_session("nope")
        with pytest.MonkeyPatch.context() as mp:
            real_time = time.time
            mp.setattr(time, "time", lambda: real_time() + update_auth.SESSION_TTL_SECONDS * 2)
            assert not update_auth.validate_session(token)
    finally:
        del os.environ["TOKDASH_UPDATE_ORIGIN"]


def test_no_session_without_origin():
    assert update_auth.create_session() is None


def test_csrf_rotation_recovers_after_token_loss(monkeypatch):
    # A reload keeps the cookie but loses the in-memory CSRF token; the session must be
    # able to mint a fresh one — and the old token must die with the rotation.
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    session = update_auth.create_session()
    token = session["token"]
    fresh = update_auth.rotate_csrf(token)
    assert fresh and fresh != session["csrf"]
    assert update_auth.validate_session(token, csrf=fresh)
    assert not update_auth.validate_session(token, csrf=session["csrf"])
    assert update_auth.rotate_csrf("bogus-token") is None
    assert update_auth.rotate_csrf(None) is None


# --- control ------------------------------------------------------------------------------


def test_capability_shape(eligible_env, monkeypatch):
    monkeypatch.setattr(updatecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(updatecheck, "check", lambda v, **k: {
        "current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False,
    })
    payload = update_control.capability("2.6.6")
    assert payload["eligible"] is True
    assert payload["target"] == "9.9.9"
    assert payload["manual_command"] == "tokdash update"


def test_start_rejects_ineligible(monkeypatch):
    monkeypatch.setattr(update_eligibility.detect, "systemd_user_available", lambda: False)
    status, body = update_control.start_update("9.9.9", current_version="2.6.6")
    assert status == 403


def test_start_rejects_stale_target(eligible_env, monkeypatch):
    monkeypatch.setattr(updatecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(updatecheck, "check", lambda v, **k: {
        "current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False,
    })
    monkeypatch.setattr(update_control.shutil, "which", lambda name: "/usr/bin/systemd-run")
    status, body = update_control.start_update("9.9.8", current_version="2.6.6")
    assert status == 400
    assert "available release" in body["detail"]


def test_start_launches_staged_helper(eligible_env, monkeypatch, data_dir):
    monkeypatch.setattr(updatecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(updatecheck, "check", lambda v, **k: {
        "current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False,
    })
    monkeypatch.setattr(update_control.shutil, "which", lambda name: "/usr/bin/systemd-run")
    launched = {}

    def fake_run(cmd, **kwargs):
        launched["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(update_control.subprocess, "run", fake_run)
    status, body = update_control.start_update("9.9.9", current_version="2.6.6")
    assert status == 202, body
    cmd = launched["cmd"]
    assert cmd[0] == "systemd-run" and "--user" in cmd and "--collect" in cmd
    job = body["job"]
    assert job["phase"] in ("accepted", "staged")
    staged = Path(update_jobs.staging_dir()) / f"update-helper-{job['id']}.py"
    assert staged.is_file()
    assert cmd[-2:] == [str(data_dir), job["id"]]
    # The staged script is the helper source, verbatim.
    assert "Independent updater helper" in staged.read_text(encoding="utf-8")


def test_start_launch_failure_records_failed(eligible_env, monkeypatch):
    monkeypatch.setattr(updatecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(updatecheck, "check", lambda v, **k: {
        "current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False,
    })
    monkeypatch.setattr(update_control.shutil, "which", lambda name: "/usr/bin/systemd-run")

    def boom(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    monkeypatch.setattr(update_control.subprocess, "run", boom)
    status, body = update_control.start_update("9.9.9", current_version="2.6.6")
    assert status == 500
    job = update_jobs.latest_job()
    assert job["phase"] == "failed" and job["failed_phase"] == "preflight"


def test_control_imports_on_platforms_without_fcntl():
    # Windows capability checks must answer with manual guidance, not ImportError:
    # importing control must not drag in the Linux-only helper module.
    import os
    import sys

    probe = (
        "import sys\n"
        "sys.modules['fcntl'] = None\n"
        "import tokdash.onboard.update_control as c\n"
        "import tokdash.onboard.update_helper as h\n"
        "assert h.fcntl is None\n"
        "assert c._helper_source_path().is_file()\n"
        "print('ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# --- API (ASGI) -----------------------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(api.app, raise_server_exceptions=False)


def _loopback_headers():
    return {"host": HOST, "x-tokdash-token": api._CSRF_TOKEN}


def test_api_capability_loopback(loopback_app, eligible_env):
    r = _client().get("/api/update/capability", headers={"host": HOST})
    assert r.status_code == 200
    body = r.json()
    assert body["eligible"] is True and "enrolled" in body


def test_api_capability_rejects_foreign_host(loopback_app):
    r = _client().get("/api/update/capability", headers={"host": "evil.example.com"})
    assert r.status_code == 403


def test_api_start_requires_auth(loopback_app, eligible_env):
    # Tailnet Host, no configured origin → the write gate still applies → 403.
    r = _client().post(
        "/api/update/start",
        headers={"host": TAILNET_HOST},
        json={"version": "9.9.9"},
    )
    assert r.status_code == 403


def test_api_start_remote_requires_session(loopback_app, eligible_env, monkeypatch):
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    r = _client().post(
        "/api/update/start",
        headers={"host": TAILNET_HOST, "origin": TAILNET_ORIGIN},
        json={"version": "9.9.9"},
    )
    assert r.status_code == 403  # no cookie


def test_api_enroll_and_start_remote(loopback_app, eligible_env, monkeypatch):
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    monkeypatch.setattr(updatecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(updatecheck, "check", lambda v, **k: {
        "current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False,
    })
    monkeypatch.setattr(update_control.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(
        update_control.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    client = _client()
    headers = {"host": TAILNET_HOST, "origin": TAILNET_ORIGIN}

    # Enroll without the code fails.
    r = client.post("/api/update/enroll", headers=headers, json={"code": "BADCODE12"})
    assert r.status_code == 403

    code = update_auth.create_pairing_code()
    r = client.post("/api/update/enroll", headers=headers, json={"code": code})
    assert r.status_code == 200
    csrf = r.json()["csrf"]
    cookie = update_auth.SESSION_COOKIE

    # Start with the session cookie + CSRF header is admitted.
    r = client.post(
        "/api/update/start",
        headers={**headers, update_auth.CSRF_HEADER: csrf},
        cookies={cookie: "unused-per-request-cookie"},  # placeholder, replaced below
        json={"version": "9.9.9"},
    )
    # The placeholder cookie above is NOT the session token; without the real one it
    # must be rejected — proving the cookie is load-bearing, not decoration.
    assert r.status_code == 403

    # Re-enroll for a fresh session token by reading back the store would leak the
    # hash only; instead mint again:
    code = update_auth.create_pairing_code()
    r = client.post("/api/update/enroll", headers=headers, json={"code": code})
    assert r.status_code == 200
    csrf = r.json()["csrf"]
    # Extract the real cookie value from the response Set-Cookie.
    set_cookie = r.headers["set-cookie"]
    token = set_cookie.split("=", 1)[1].split(";", 1)[0]
    lowered = set_cookie.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered

    r = client.post(
        "/api/update/start",
        headers={**headers, update_auth.CSRF_HEADER: csrf},
        cookies={cookie: token},
        json={"version": "9.9.9"},
    )
    assert r.status_code in (200, 202), r.text

    # CSRF missing → rejected even with the session cookie.
    r = client.post(
        "/api/update/start",
        headers=headers,
        cookies={cookie: token},
        json={"version": "9.9.9"},
    )
    assert r.status_code == 403


def test_api_status_remote_requires_session(loopback_app):
    monkeypatch_env = {"TOKDASH_UPDATE_ORIGIN": TAILNET_ORIGIN}
    import os

    os.environ.update(monkeypatch_env)
    try:
        r = _client().get("/api/update/status", headers={"host": TAILNET_HOST, "origin": TAILNET_ORIGIN})
        assert r.status_code == 403
        r = _client().get("/api/update/status", headers={"host": HOST})
        assert r.status_code == 200
    finally:
        del os.environ["TOKDASH_UPDATE_ORIGIN"]


def test_other_writes_stay_loopback(loopback_app, monkeypatch):
    # The remote exception must not leak into unrelated write endpoints.
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    r = _client().post(
        "/api/update-check/consent",
        headers={"host": TAILNET_HOST, "origin": TAILNET_ORIGIN},
    )
    assert r.status_code == 403


def test_api_remote_plane_denied_when_bound_wide(eligible_env, monkeypatch):
    # The remote update plane exists ONLY for a loopback bind behind a serve proxy. A
    # 0.0.0.0 bind invalidates that assumption, so origin + session + CSRF together
    # still buy nothing: the plane is refused outright.
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    api.app.state.bind = "0.0.0.0"
    api.app.state.port = 55423
    try:
        client = _client()
        headers = {"host": TAILNET_HOST, "origin": TAILNET_ORIGIN}
        code = update_auth.create_pairing_code()
        r = client.post("/api/update/enroll", headers=headers, json={"code": code})
        assert r.status_code == 403
        r = client.get("/api/update/capability", headers=headers)
        assert r.status_code == 403
        r = client.get("/api/update/status", headers=headers)
        assert r.status_code == 403
    finally:
        api.app.state.bind = None
        api.app.state.port = None


def test_api_csrf_recovery_after_token_loss(loopback_app, eligible_env, monkeypatch):
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    monkeypatch.setattr(updatecheck, "is_enabled", lambda: True)
    monkeypatch.setattr(updatecheck, "check", lambda v, **k: {
        "current": v, "latest": "9.9.9", "update_available": True, "error": None, "cached": False,
    })
    monkeypatch.setattr(update_control.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(
        update_control.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    client = _client()
    headers = {"host": TAILNET_HOST, "origin": TAILNET_ORIGIN}

    code = update_auth.create_pairing_code()
    r = client.post("/api/update/enroll", headers=headers, json={"code": code})
    assert r.status_code == 200
    old_csrf = r.json()["csrf"]
    token = r.headers["set-cookie"].split("=", 1)[1].split(";", 1)[0]
    cookie = {update_auth.SESSION_COOKIE: token}

    # The token lived only in the (now reloaded) page. Recovery over the session:
    r = client.get("/api/update/capability?want_csrf=1", headers=headers, cookies=cookie)
    assert r.status_code == 200
    fresh = r.json().get("csrf")
    assert fresh and fresh != old_csrf

    # The rotated-out token must no longer start anything:
    r = client.post("/api/update/start", headers={**headers, update_auth.CSRF_HEADER: old_csrf},
                    cookies=cookie, json={"version": "9.9.9"})
    assert r.status_code == 403

    # The recovered token does:
    r = client.post("/api/update/start", headers={**headers, update_auth.CSRF_HEADER: fresh},
                    cookies=cookie, json={"version": "9.9.9"})
    assert r.status_code in (200, 202), r.text

    # Plain capability polls never rotate tokens (a second tab must not be poisoned).
    r = client.get("/api/update/capability", headers=headers, cookies=cookie)
    assert "csrf" not in r.json()


# --- helper script ---------------------------------------------------------------------------


class FakeRunner(update_helper.Runner):
    def __init__(self, data_dir, job_id, log):
        super().__init__(data_dir, job_id, log)
        self.calls = []
        self.install_ok = True

    def run(self, args, timeout=60):
        self.calls.append(list(args))
        stdout = ""
        # The preflight proves the LOADED unit really runs the recorded service; answer
        # `systemctl show` in systemd's REAL structured form (captured from a live
        # pipx-managed install), not a bare command line.
        if args[:3] == ["systemctl", "--user", "show"] and "-p" in args and "ExecStart" in args:
            py = f"{self.data_dir}/venv/bin/python"
            stdout = (
                f"{{ path={py} ; argv[]={py} -m tokdash serve --bind 127.0.0.1 "
                "--port 55423 --no-open ; ignore_errors=no ; start_time=[n/a] ; "
                "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\n"
            )
        elif args[:3] == ["systemctl", "--user", "show"] and "MainPID" in args:
            stdout = "0\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def port_open(self, host, port):
        return False

    def install(self, argv):
        self.calls.append(["install"] + list(argv))
        if not self.install_ok:
            raise RuntimeError("pip exploded")


def _seed_job(tmp_path, to_version="9.9.9", usage_db=None):
    job, created = update_jobs.create_job(
        to_version=to_version, from_version="2.6.6", trigger="dashboard",
        install_argv=[f"{tmp_path}/venv/bin/python", "-m", "pip", "install", "--no-input", f"tokdash=={to_version}"],
        service_type="systemd-user", service_name="tokdash",
        service_marker=manifest_mod.marker_token("abcd1234"),
        python_path=f"{tmp_path}/venv/bin/python", usage_db=usage_db,
    )
    return job


def test_helper_happy_path(eligible_env, tmp_path):
    db = tmp_path / "usage.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("create table t(x)")
    conn.commit()
    conn.close()
    job = _seed_job(tmp_path, usage_db=str(db))

    ok = None

    def opener(url, timeout=5):
        if url.endswith("/health"):
            return {"status": "ok", "service": "tokdash", "version": "9.9.9"}
        if url.endswith("/api/version"):
            return {"runtime_version": "9.9.9"}
        return {}

    runner = FakeRunner(tmp_path, job["id"], lambda m: None)
    # Point the runner journal at lock-free? main acquires the real lock itself.
    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=opener)
    assert ok is True
    record = update_jobs.get_job(job["id"])
    assert record["phase"] == "succeeded"
    assert record["result_version"] == "9.9.9"
    assert record["backup_path"] and Path(record["backup_path"]).is_file()
    stops = [c for c in runner.calls if c[:3] == ["systemctl", "--user", "stop"]]
    starts = [c for c in runner.calls if c[:3] == ["systemctl", "--user", "start"]]
    assert stops and starts
    # Stop strictly precedes the install, install precedes start.
    stop_i = runner.calls.index(stops[0])
    install_i = next(i for i, c in enumerate(runner.calls) if c[0] == "install")
    start_i = runner.calls.index(starts[0])
    assert stop_i < install_i < start_i


def test_helper_install_failure_recovers_service(eligible_env, tmp_path):
    job = _seed_job(tmp_path)
    runner = FakeRunner(tmp_path, job["id"], lambda m: None)
    runner.install_ok = False

    def opener(url, timeout=5):
        return {"status": "ok", "service": "tokdash", "version": "2.6.6", "runtime_version": "2.6.6"}

    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=opener)
    assert ok is False
    record = update_jobs.get_job(job["id"])
    assert record["phase"] == "failed"
    assert record["failed_phase"] == "install"
    # Service recovery attempted: a start ran after the failed install.
    starts = [c for c in runner.calls if c[:3] == ["systemctl", "--user", "start"]]
    assert starts


def test_helper_refuses_rerun_on_terminal_job(eligible_env, tmp_path):
    job = _seed_job(tmp_path)
    update_jobs.set_phase(job["id"], "succeeded", result_version="9.9.9")
    runner = FakeRunner(tmp_path, job["id"], lambda m: None)
    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=lambda *a, **k: {})
    assert ok is False
    record = update_jobs.get_job(job["id"])
    assert record["phase"] == "succeeded"  # not overwritten by the second run attempt
    assert not [c for c in runner.calls if c[:3] == ["systemctl", "--user", "stop"]]


def test_helper_rejects_bad_install_argv(eligible_env, tmp_path, monkeypatch):
    job = _seed_job(tmp_path)
    with update_jobs.with_update_lock():
        data = update_jobs._load()
        data["jobs"][job["id"]]["install_argv"] = ["rm", "-rf", "/"]
        update_jobs._store(data)
    runner = FakeRunner(tmp_path, job["id"], lambda m: None)
    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=lambda *a, **k: {})
    assert ok is False
    record = update_jobs.get_job(job["id"])
    assert record["failed_phase"] == "preflight"
    assert not runner.calls  # never touched the service


def test_helper_readiness_version_mismatch_fails(eligible_env, tmp_path):
    job = _seed_job(tmp_path)
    runner = FakeRunner(tmp_path, job["id"], lambda m: None)

    def opener(url, timeout=5):
        return {"status": "ok", "service": "tokdash", "version": "2.6.6", "runtime_version": "2.6.6"}

    # Shrink the readiness window so the test is fast.
    import tokdash.onboard.update_helper as helper_mod

    import importlib
    saved = helper_mod.READY_TIMEOUT
    helper_mod.READY_TIMEOUT = 2
    try:
        ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=opener)
    finally:
        helper_mod.READY_TIMEOUT = saved
    assert ok is False
    record = update_jobs.get_job(job["id"])
    assert record["failed_phase"] == "starting"


def test_helper_heartbeat_joined_before_terminal_write(eligible_env, tmp_path, monkeypatch):
    # A heartbeat thread still mid-write when install fails could land its (stale)
    # snapshot AFTER the terminal write and resurrect a non-terminal phase. The runner
    # must join the heartbeat before ANY terminal write.
    import threading

    job = _seed_job(tmp_path)
    runner = FakeRunner(tmp_path, job["id"], lambda m: None)
    runner.install_ok = False
    order = []
    real_update = runner.journal.update

    def spy(job_id, **fields):
        order.append((fields.get("phase"), threading.current_thread().name))
        return real_update(job_id, **fields)

    monkeypatch.setattr(update_helper, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(runner.journal, "update", spy)
    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=lambda *a, **k: {})
    assert ok is False
    phases = [ph for ph, _ in order]
    assert "failed" in phases
    after = order[phases.index("failed") + 1:]
    assert all(th == "MainThread" for _, th in after), after
    assert update_jobs.get_job(job["id"])["phase"] == "failed"


def test_backup_holds_db_lock_until_replacement_done(eligible_env, tmp_path):
    # Snapshot alone is not enough: the lock must exclude other tokdash writers for the
    # whole install window (it is released only when the NEW server is about to start).
    fcntl = pytest.importorskip("fcntl")

    db = tmp_path / "usage.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("create table t(x)")
    conn.commit()
    conn.close()
    runner = update_helper.Runner(tmp_path, "j", lambda m: None)
    dest = runner.backup_db(str(db), tmp_path / "backups")
    assert dest and Path(dest).is_file()

    probe = (tmp_path / "usage.sqlite3.lock").open("a+")
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        probe.close()
    runner.release_db_lock()
    probe = (tmp_path / "usage.sqlite3.lock").open("a+")
    try:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    finally:
        probe.close()


def test_helper_refuses_sibling_tokdash_process(eligible_env, tmp_path, monkeypatch):
    # An idle co-tenant tokdash on the same data dir would resume writing the DB the
    # replacement migrates; the honest answer is refusal, before any downtime.
    job = _seed_job(tmp_path)
    monkeypatch.setattr(
        update_helper.Runner, "_sibling_tokdash_pids",
        lambda self, exclude, usage_db=None: [4242],
    )
    runner = FakeRunner(tmp_path, job["id"], lambda m: None)
    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=lambda *a, **k: {})
    assert ok is False
    record = update_jobs.get_job(job["id"])
    assert record["phase"] == "failed" and record["failed_phase"] == "preflight"
    assert "another Tokdash process" in (record["message"] or "")
    assert not [c for c in runner.calls if c[:3] == ["systemctl", "--user", "stop"]]


def test_helper_preflight_rejects_unrelated_execstart(eligible_env, tmp_path):
    # Loaded configuration must run the RECORDED tokdash service — an ExecStart that
    # merely starts with our interpreter proves nothing.
    job = _seed_job(tmp_path)

    class LiarRunner(FakeRunner):
        def run(self, args, timeout=60):
            self.calls.append(list(args))
            if args[:3] == ["systemctl", "--user", "show"] and "ExecStart" in args:
                return subprocess.CompletedProcess(
                    args, 0,
                    f"{self.data_dir}/venv/bin/python -m totally_unrelated_app serve\n", "",
                )
            return super().run(args, timeout=timeout)

    runner = LiarRunner(tmp_path, job["id"], lambda m: None)
    ok = update_helper.main(str(tmp_path), job["id"], runner=runner, opener=lambda *a, **k: {})
    assert ok is False
    record = update_jobs.get_job(job["id"])
    assert record["failed_phase"] == "preflight"
    assert not [c for c in runner.calls if c[:3] == ["systemctl", "--user", "stop"]]


def test_looks_like_tokdash():
    assert update_helper._looks_like_tokdash(["/home/u/.local/bin/tokdash", "serve"])
    assert update_helper._looks_like_tokdash(["tokdash", "serve"])  # resolved from PATH
    assert update_helper._looks_like_tokdash(["/venv/bin/python", "-m", "tokdash", "serve"])
    # console script launched through its interpreter — the shape a venv parent produces
    assert update_helper._looks_like_tokdash(
        ["/venv/bin/python", "/home/user/.local/bin/tokdash", "serve"]
    )
    assert update_helper._looks_like_tokdash(["python3", "main.py"])
    assert not update_helper._looks_like_tokdash(
        ["/venv/bin/python", "-m", "pip", "install", "tokdash==9.9.9"]
    )
    assert not update_helper._looks_like_tokdash(["systemctl", "--user", "restart", "tokdash"])


def test_sibling_scan_covers_console_scripts_and_shared_db(eligible_env, tmp_path, monkeypatch):
    import os as _os

    db = str(tmp_path / "usage.sqlite3")

    def fake_snapshot(self):
        # 111: console-script shape, different data dir, SHARED usage DB → caught
        yield 111, ["/venv/bin/python", "/home/user/.local/bin/tokdash", "serve"], \
            {"TOKDASH_USAGE_DB_PATH": db}
        # 222: our own pip child (not tokdash), shared DB → ignored
        yield 222, ["/venv/bin/python", "-m", "pip", "install", "tokdash==9.9.9"], \
            {"TOKDASH_USAGE_DB_PATH": db}
        # 333: same data dir → caught
        yield 333, ["tokdash", "serve"], {"TOKDASH_DATA_DIR": str(tmp_path)}
        # 444: unrelated install → ignored
        yield 444, ["tokdash", "serve"], {
            "TOKDASH_DATA_DIR": "/home/u/.tokdash",
            "TOKDASH_USAGE_DB_PATH": "/elsewhere/usage.sqlite3", "HOME": "/home/u",
        }
        # self: excluded regardless of shape
        yield _os.getpid(), ["tokdash", "serve"], {"TOKDASH_DATA_DIR": str(tmp_path)}

    monkeypatch.setattr(update_helper.Runner, "_proc_snapshot", fake_snapshot)
    r = update_helper.Runner(tmp_path, "j", lambda m: None)
    pids = r._sibling_tokdash_pids(exclude={999}, usage_db=db)
    assert sorted(pids) == [111, 333]


# --- CLI/engine contract -------------------------------------------------------------------------


def test_update_help_and_verbs():
    from tokdash.cli_help import CARD_VERBS, COMMAND_VERBS

    assert "update-enroll" in COMMAND_VERBS
    assert "update-enroll" in CARD_VERBS

    from tokdash.cli import build_parser

    args = build_parser("tokdash").parse_args(["update", "--to", "2.7.0"])
    assert args.to_version == "2.7.0"
    args = build_parser("tokdash").parse_args(["update"])
    assert args.to_version is None


def test_cmd_update_rejects_bad_target(tmp_path, monkeypatch, capsys):
    managed_manifest(tmp_path)
    from tokdash.onboard import engine
    from tokdash.onboard.plan import Options

    monkeypatch.setattr(engine.updatecheck, "_is_newer", lambda a, b: True)
    rc = engine.cmd_update(Options(action="update", to_version="latest"))
    assert rc == engine.EXIT_OK
    out = capsys.readouterr().out
    assert "not a plain release version" in out


def test_cmd_update_attaches_to_live_job(eligible_env, capsys, monkeypatch):
    from tokdash.onboard import engine
    from tokdash.onboard.plan import Options

    monkeypatch.setattr(engine.detect, "find_pipx", lambda: "/usr/bin/pipx")

    update_jobs.create_job(
        to_version="9.9.9", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
        service_marker="m", python_path="/p", usage_db=None,
    )
    rc = engine.cmd_update(Options(action="update"))
    assert rc == engine.EXIT_OK
    out = capsys.readouterr().out
    assert "already running" in out


def test_cmd_update_heartbeats_during_install(eligible_env, monkeypatch):
    # A minutes-long terminal update must keep its job's heartbeat warm; otherwise a
    # dashboard polling beside it watches a LIVE update age into "interrupted".
    from tokdash.onboard import engine
    from tokdash.onboard.plan import Options

    monkeypatch.setattr(engine.detect, "find_pipx", lambda: "/usr/bin/pipx")
    monkeypatch.setattr(engine, "_CLI_HEARTBEAT_SECONDS", 0.01)
    heartbeats = []
    real_hb = update_jobs.touch_heartbeat
    monkeypatch.setattr(
        update_jobs, "touch_heartbeat",
        lambda job_id: (heartbeats.append(job_id), real_hb(job_id))[1],
    )

    def slow_run(cmd, *a, **k):
        time.sleep(0.1)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(engine.subprocess, "run", slow_run)
    rc = engine.cmd_update(Options(action="update"))
    assert rc == engine.EXIT_OK
    assert heartbeats, "no heartbeat refreshes while the install ran"


def test_update_enroll_mints_code(monkeypatch, capsys):
    monkeypatch.setenv("TOKDASH_UPDATE_ORIGIN", TAILNET_ORIGIN)
    from tokdash.onboard import engine
    from tokdash.onboard.plan import Options

    rc = engine.cmd_update_enroll(Options(action="update-enroll"))
    assert rc == engine.EXIT_OK
    out = capsys.readouterr().out
    assert "Pairing code:" in out
    assert TAILNET_ORIGIN in out


# --- boot reconciliation ---------------------------------------------------------------------------


def test_boot_reconciliation_marks_stale_failed():
    job, _ = update_jobs.create_job(
        to_version="9.9.9", from_version="2.6.6", trigger="dashboard",
        install_argv=["/p"], service_type="systemd-user", service_name="tokdash",
        service_marker="m", python_path="/p", usage_db=None,
    )
    with update_jobs.with_update_lock():
        data = update_jobs._load()
        data["jobs"][job["id"]]["updated_at"] = "2020-01-01T00:00:00Z"
        update_jobs._store(data)
    update_jobs.reconcile_boot()
    assert update_jobs.get_job(job["id"], reconcile=False)["phase"] == "failed"
