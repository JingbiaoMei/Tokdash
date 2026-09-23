"""Issue #108 P0: ``/health`` carries a stable per-daemon ``instance_id``.

The dashboard decides whether two URLs are one Tokdash or two from this one field, so
everything the merge depends on is pinned here: the id is UUID-shaped, survives a restart,
two daemons over one data dir agree on it, two over two dirs do not, and a state dir that
cannot hold it leaves the field out instead of substituting a guess.
"""
import json
import os
import threading
import uuid

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import tokdash.api as api
from tokdash import instance_identity
from tokdash.clientpaths import tokdash_data_dir


@pytest.fixture(autouse=True)
def _fresh_identity_cache():
    """Keep the accessor's path-keyed cache out of every other test.

    ``conftest.isolated_usage_db`` gives each test its own data dir, but a test that
    repoints ``TOKDASH_DATA_DIR`` twice inside one test would otherwise inherit the id
    its neighbour cached against the earlier path.
    """
    instance_identity.clear_instance_id_cache()
    yield
    instance_identity.clear_instance_id_cache()


def _write_identity(payload: str):
    path = instance_identity.instance_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# --- what /health answers --------------------------------------------------------


def test_health_reports_uuid_shaped_instance_id():
    # Built without `with` on purpose: that skips the lifespan, which is why the accessor
    # resolves lazily rather than depending on a startup hook alone.
    body = TestClient(api.app).get("/health").json()
    assert body["service"] == "tokdash"
    assert uuid.UUID(body["instance_id"]).version == 4


def test_health_answers_through_a_lifespan_run_too():
    with TestClient(api.app) as client:
        body = client.get("/health").json()
    assert uuid.UUID(body["instance_id"])


def test_instance_id_is_persisted_and_stable_across_clients():
    first = TestClient(api.app).get("/health").json()["instance_id"]

    stored = json.loads(instance_identity.instance_json_path().read_text(encoding="utf-8"))
    assert stored["instance_id"] == first
    assert stored["schema_version"] == instance_identity.SCHEMA_VERSION

    instance_identity.clear_instance_id_cache()
    assert TestClient(api.app).get("/health").json()["instance_id"] == first


def test_cached_id_survives_the_file_disappearing():
    """A live daemon keeps its identity if the state dir goes away mid-run."""
    instance_id = instance_identity.get_instance_id()
    instance_identity.instance_json_path().unlink()
    assert instance_identity.get_instance_id() == instance_id


# --- one data dir, one identity --------------------------------------------------


def test_two_daemons_over_one_data_dir_agree():
    """Decision 4: two services sharing a data dir are one host, so they need one id.

    They race at boot, which is the case ``open(path, "x")`` gets wrong: it creates the
    file empty, the second daemon reads nothing, and the two end up with different ids.
    """
    ids = set()
    failures = []
    barrier = threading.Barrier(4)

    def race():
        try:
            barrier.wait()
            ids.add(instance_identity.get_instance_id())
        except Exception as exc:  # pragma: no cover - surfaces the real error in pytest
            failures.append(exc)

    threads = [threading.Thread(target=race) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    stored = json.loads(instance_identity.instance_json_path().read_text(encoding="utf-8"))
    assert ids == {stored["instance_id"]}


def test_publishing_leaves_no_temp_files_behind():
    assert instance_identity.get_instance_id()
    strays = [p.name for p in tokdash_data_dir().iterdir()
              if p.name != instance_identity.INSTANCE_FILENAME]
    assert strays == []


def test_empty_file_is_retried_then_adopted():
    """The mid-write window the exclusive create alone cannot close.

    The reader arrives while the publisher's file is still empty. Retrying is what keeps
    both daemons on one id instead of one id and a permanent absence.
    """
    path = _write_identity("")
    published = str(uuid.uuid4())

    def finish():
        import time
        time.sleep(instance_identity._READ_RETRY_DELAY_S)
        path.write_text(json.dumps({"schema_version": 1, "instance_id": published}),
                        encoding="utf-8")

    writer = threading.Thread(target=finish)
    writer.start()
    try:
        assert instance_identity.get_instance_id() == published
    finally:
        writer.join()


# --- two data dirs are two daemons -----------------------------------------------


def test_second_data_dir_gets_a_different_id(monkeypatch, tmp_path):
    first = instance_identity.get_instance_id()
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / "other-tokdash"))
    second = instance_identity.get_instance_id()

    assert first != second
    assert uuid.UUID(second).version == 4


def test_id_is_never_derived_from_the_data_dir_path(monkeypatch, tmp_path):
    """A hash of the path would merge two machines that happen to share one string."""
    here = instance_identity.get_instance_id()
    twin = tmp_path / "same-path-different-machine"
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(twin))
    assert instance_identity.get_instance_id() != here


# --- unreadable state: omit, never guess -----------------------------------------


def test_unwritable_data_dir_omits_the_field(monkeypatch, tmp_path):
    if _as_root():
        pytest.skip("running as root: file modes are not enforced")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(locked))

    assert instance_identity.get_instance_id() is None
    body = TestClient(api.app).get("/health").json()
    assert body["service"] == "tokdash"
    assert "instance_id" not in body


def test_malformed_file_reads_as_absent_and_is_left_alone():
    path = _write_identity("{not json")
    assert instance_identity.get_instance_id() is None
    assert path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("payload", [
    '""',
    "[1, 2, 3]",
    '{"schema_version": 1}',
    '{"instance_id": ""}',
    '{"instance_id": null}',
    '{"instance_id": 61}',
    '{"instance_id": "not-a-uuid"}',
    '{"instance_id": "6c1f"}',
])
def test_non_uuid_identity_is_never_adopted(payload):
    """Anything that is not UUID-shaped cannot become a merge key."""
    _write_identity(payload)
    assert instance_identity.get_instance_id() is None


def test_stored_id_is_canonicalised():
    """Braces and casing name the same id; letting both through splits one daemon in two."""
    _write_identity(json.dumps({
        "schema_version": 1,
        "instance_id": "{00000000-0000-4000-8000-000000000000}",
    }))
    assert instance_identity.get_instance_id() == "00000000-0000-4000-8000-000000000000"


def test_failed_identity_is_retried_after_the_backoff(monkeypatch, tmp_path):
    """A full disk at boot must not cost identity for the life of the process."""
    if _as_root():
        pytest.skip("running as root: file modes are not enforced")
    flaky = tmp_path / "flaky"
    flaky.mkdir()
    flaky.chmod(0o500)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(flaky))

    assert instance_identity.get_instance_id() is None
    # Still inside the backoff: no fresh attempt, so no fresh writes to a bad dir.
    assert instance_identity.get_instance_id() is None

    flaky.chmod(0o700)
    instance_identity._NEXT_TRY.clear()
    assert uuid.UUID(instance_identity.get_instance_id())
