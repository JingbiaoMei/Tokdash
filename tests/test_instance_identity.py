"""Issue #108 P0: ``/health`` carries a stable per-daemon ``instance_id``.

The dashboard decides whether two URLs are one Tokdash or two from this one field, so
everything the merge depends on is pinned here: the id is UUID-shaped, survives a restart,
two daemons over one data dir agree on it, two over two dirs do not, and a state dir that
cannot hold it leaves the field out instead of substituting a guess.

The failure modes get the most attention, because they are the ones that quietly cost the
merge: a boot race that leaves one daemon with no id, a backoff that never ends, a file
that retries forever without ever becoming readable.
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

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


class _Clock:
    """A monotonic clock a test can move, so backoff tests do not sleep.

    ``instance_identity`` reads time through one ``_now`` seam for exactly this.
    """

    def __init__(self):
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(instance_identity, "_now", fake)
    return fake


@pytest.fixture
def resolves(monkeypatch) -> list:
    """Record every ``_resolve`` call, so "did it retry?" is answerable directly.

    Asserting on the return value alone cannot tell a backoff off from a directory that
    is still broken: both answer ``None``.
    """
    calls: list = []
    real = instance_identity._resolve

    def spy(target):
        calls.append(target)
        return real(target)

    monkeypatch.setattr(instance_identity, "_resolve", spy)
    return calls


def _warnings(caplog) -> list:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _wait_for_instance_id(client, timeout: float = 5.0) -> dict:
    """Poll ``/health`` until it carries an id, and fail loudly if it never does.

    The warm-up runs in the background so that startup cannot queue behind the data
    directory, which means the first answer after startup may legitimately not have the
    field yet. Every reader of it -- P1's probing included -- has to keep asking.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/health").json()
        if "instance_id" in body:
            return body
        time.sleep(0.02)
    raise AssertionError(f"/health never reported an instance_id within {timeout} s: {body}")


class _ThreadHops:
    """Stand-in for ``asyncio`` that counts worker-thread handoffs.

    ``/health`` is async so it answers while every worker is busy, which the identity
    lookup undoes if it does its mkdir/write/fsync/link on the loop. The only acceptable
    answer is: settled values inline, anything that might touch the disk elsewhere.
    """

    def __init__(self):
        self.hops = 0

    async def to_thread(self, fn, *args, **kwargs):
        self.hops += 1
        return await asyncio.to_thread(fn, *args, **kwargs)


# --- what /health answers --------------------------------------------------------


def test_health_reports_uuid_shaped_instance_id():
    # Built without `with` on purpose: that skips the lifespan, which is why the accessor
    # resolves lazily rather than depending on a startup hook alone.
    body = TestClient(api.app).get("/health").json()
    assert body["service"] == "tokdash"
    assert uuid.UUID(body["instance_id"]).version == 4


def test_health_answers_through_a_lifespan_run_too():
    """The warm-up the lifespan starts in the background has to actually deliver an id."""
    with TestClient(api.app) as client:
        body = _wait_for_instance_id(client)
    assert uuid.UUID(body["instance_id"]).version == 4


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


def test_publish_race_over_one_data_dir_agrees():
    """Decision 4: two services sharing a data dir are one host, so they need one id.

    Raced through ``_resolve``, not ``get_instance_id``, and this is the reason: the
    accessor takes ``_LOCK``, so four threads through it resolve one at a time and can
    never collide. Two daemon processes starting at boot do collide, and ``_resolve`` is
    all either of them does to the file, so that is what races here.

    The collision that matters is the one ``open(path, "x")`` loses. It puts the final
    name in place already existing but still empty, so the sibling that reads it a
    moment later gets nothing, gives up on identity for the life of the process, and
    never merges with the twin it was written to agree with. ``os.link`` is the fix: the
    name appears with its content, or it is already there and its owner wins.
    """
    target = instance_identity.instance_json_path()
    racers = 4
    outcomes = []
    failures = []
    barrier = threading.Barrier(racers)

    def race():
        try:
            barrier.wait()
            outcomes.append(instance_identity._resolve(target))
        except BaseException as exc:  # pragma: no cover - surfaces the real error in pytest
            failures.append(exc)

    threads = [threading.Thread(target=race) for _ in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    # Nobody lands in an empty-file window: every racer reads an id, not a gap.
    assert [o.outcome for o in outcomes] == [instance_identity._Outcome.OK] * racers
    ids = {o.value for o in outcomes}
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert ids == {stored["instance_id"]}
    for value in ids:
        assert uuid.UUID(value).version == 4


def test_a_publish_that_fills_the_name_later_loses_the_race(monkeypatch):
    """Teeth for the test above, so it cannot quietly stop checking anything.

    The whole reason that race goes through ``_resolve`` is the accessor's lock, and a
    lock is easy to put back. Then every assertion in the file would keep passing over a
    publish that loses, which is how this shipped once.

    So this is the losing publish: an exclusive create of the final name, which exists
    empty from the moment it is created. It holds the name empty until every other racer
    has walked away, so the loss is scheduled rather than a matter of luck, and what it
    costs a daemon is exactly what the field exists to prevent -- one of two daemons over
    one data dir ends up with no identity and never merges with its twin.
    """
    target = instance_identity.instance_json_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    racers = 4
    finished: list = []
    outcomes: list = []
    published = threading.Event()
    barrier = threading.Barrier(racers)
    deadline = time.monotonic() + 10.0

    def fill_later(path: Path):
        payload = json.dumps({
            "schema_version": instance_identity.SCHEMA_VERSION,
            "instance_id": str(uuid.uuid4()),
        }) + "\n"
        try:
            handle = open(path, "x", encoding="utf-8")  # the name exists here, empty
        except FileExistsError:
            return instance_identity._read(path)
        published.set()
        while len(finished) < racers - 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        with handle:
            handle.write(payload)
        return instance_identity._Result(
            instance_identity._Outcome.OK, json.loads(payload)["instance_id"]
        )

    def race():
        barrier.wait()
        result = instance_identity._resolve(target)
        outcomes.append(result)
        finished.append(result)

    monkeypatch.setattr(instance_identity, "_create", fill_later)
    threads = [threading.Thread(target=race) for _ in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert published.is_set(), "no racer published, so the race never happened"
    with_ids = [o for o in outcomes if o.outcome is instance_identity._Outcome.OK]
    assert len(with_ids) < racers, (
        f"every racer still got an id, so nothing here can see an empty-file window: "
        f"{[o.outcome.value for o in outcomes]}"
    )


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


def _unwritable_dir(tmp_path) -> Path:
    """A directory that cannot be created, on POSIX and on Windows alike.

    ``chmod`` is not the probe: NTFS answers mode bits with ACLs the owner ignores, so a
    ``0o500`` directory stays writable there and CI would test nothing. A parent that is
    a regular file fails the ``mkdir`` on both, which is the case worth pinning.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("still a file", encoding="utf-8")
    return blocker / "data-dir"


def test_unwritable_data_dir_omits_the_field(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(_unwritable_dir(tmp_path)))

    assert instance_identity.get_instance_id() is None
    body = TestClient(api.app).get("/health").json()
    assert body["service"] == "tokdash"
    assert "instance_id" not in body


def test_malformed_file_is_left_alone():
    """Someone else's file in the data dir is not Tokdash's to overwrite."""
    path = _write_identity("{not json")
    assert instance_identity.get_instance_id() is None
    assert path.read_text(encoding="utf-8") == "{not json"


def test_malformed_file_is_given_up_on_rather_than_retried_forever(
    caplog, resolves, clock
):
    """A file that exists and cannot be parsed cannot heal, so stop writing to it.

    The retry backoff exists for failures that can come back -- a full disk, a read-only
    mount. This one cannot, and retrying it meant a write, an fsync and an unlink against
    the same dead file once a minute for as long as the daemon was up.
    """
    _write_identity("{not json")

    with caplog.at_level(logging.WARNING, logger="tokdash.instance_identity"):
        for _ in range(4):
            assert instance_identity.get_instance_id() is None
            clock.advance(instance_identity._FAILURE_RETRY_S * 10)

    assert len(resolves) == 1
    warned = _warnings(caplog)
    assert len(warned) == 1
    message = warned[0].getMessage()
    assert str(instance_identity.instance_json_path()) in message
    # Given up on for the life of the process, so "delete it" alone sends the user off to
    # do something that cannot take effect until they happen to restart.
    assert "delete" in message.lower()
    assert "restart" in message.lower()


def test_a_broken_data_dir_warns_once_and_still_retries(
    tmp_path, monkeypatch, caplog, resolves, clock
):
    """The failure is invisible in the dashboard, so it has to reach the log.

    A missing id does not surface anywhere: the merge simply never happens and two rows
    keep double-counting. Everything else in the accessor logs at DEBUG, which is right
    for a retry but useless for the one failure the user can act on -- so warn once,
    naming the file, and keep retrying quietly, since this failure can heal.
    """
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(_unwritable_dir(tmp_path)))

    with caplog.at_level(logging.WARNING, logger="tokdash.instance_identity"):
        for _ in range(4):
            assert instance_identity.get_instance_id() is None
            clock.advance(instance_identity._FAILURE_RETRY_S + 1)

    assert len(resolves) == 4
    warned = _warnings(caplog)
    assert len(warned) == 1
    assert str(instance_identity.instance_json_path()) in warned[0].getMessage()


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


def test_a_data_dir_that_is_not_a_directory_is_not_reported_as_absent(monkeypatch, tmp_path):
    """``FileExistsError`` from ``mkdir(exist_ok=True)`` means the path is not a directory.

    It is not a publish race, and it is not the id being merely absent: on Windows a path
    under a regular file answers exactly this, so treating it as a race would have the
    daemon describe a permanently unusable location as one it will look at again shortly.
    """
    target = tmp_path / "instance.json"
    monkeypatch.setattr(
        Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(FileExistsError("nope"))
    )

    result = instance_identity._create(target)

    assert result.outcome is instance_identity._Outcome.UNREADABLE
    assert "nope" in result.detail


def test_startup_does_not_wait_for_the_identity_lookup(monkeypatch, tmp_path):
    """Serving must not queue behind the data directory.

    The warm-up belongs with the other warm-ups, which run in the background. Awaiting it
    in the lifespan made startup take exactly as long as the lookup did -- five seconds on
    a slow disk, forever on a hung NFS mount -- and a supervisor with Restart=on-failure
    cannot help, because the process is alive and merely never serves.
    """
    started = threading.Event()
    release = threading.Event()

    def hang(target):
        started.set()
        # Long against the 2 s assertion below, short enough that a failure reports in
        # seconds rather than after a long wait.
        release.wait(8)
        return instance_identity._Result(instance_identity._Outcome.OK, str(uuid.uuid4()))

    monkeypatch.setattr(instance_identity, "_resolve", hang)
    monkeypatch.setenv("TOKDASH_WARM_ON_START", "0")
    monkeypatch.setenv("TOKDASH_DAILY_WARM", "0")

    client = TestClient(api.app)
    began = time.monotonic()
    try:
        client.__enter__()  # runs the lifespan, which is where the await used to be
        startup_s = time.monotonic() - began
        assert started.wait(5), "the warm-up never started, so this proves nothing"
        assert startup_s < 2.0, f"startup waited {startup_s:.1f} s on the data dir"

        release.set()
        warm = api.app.state.identity_warm
        assert warm is not None, "the lifespan never queued the warm-up"
        for _ in range(500):
            if warm.done():
                break
            time.sleep(0.02)
        assert warm.done(), "the background lookup never finished"
    finally:
        release.set()
        client.__exit__(None, None, None)


def test_failed_identity_is_retried_after_the_backoff(
    monkeypatch, tmp_path, resolves, clock
):
    """A full disk at boot must not cost identity for the life of the process.

    One path, the whole way through, and the retry counted at ``_resolve``. Pointing the
    data dir somewhere new and clearing the backoff would pass with an infinite backoff,
    and ``None`` twice is what a still-broken directory answers anyway, so neither half of
    "it waits, then it retries" is visible that way.
    """
    blocker = tmp_path / "not-a-directory"
    broken = _unwritable_dir(tmp_path)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(broken))

    assert instance_identity.get_instance_id() is None
    assert len(resolves) == 1

    # Inside the backoff: no fresh attempt, so no fresh writes against a bad directory.
    clock.advance(instance_identity._FAILURE_RETRY_S / 2)
    assert instance_identity.get_instance_id() is None
    assert len(resolves) == 1

    # Repair the SAME path rather than moving to a new one: a state dir that recovers has
    # to get its identity back, which is the whole point of backing off instead of giving
    # up. Clearing nothing here, so only the clock moves.
    blocker.unlink()
    clock.advance(instance_identity._FAILURE_RETRY_S)
    assert uuid.UUID(instance_identity.get_instance_id())
    assert len(resolves) == 2
    assert (broken / instance_identity.INSTANCE_FILENAME).is_file()

    # Cached from here on: the recovery costs one attempt, not one per health check.
    assert uuid.UUID(instance_identity.get_instance_id())
    assert len(resolves) == 2


# --- /health must not do this work on the event loop ----------------------------


def test_cold_lookup_leaves_the_event_loop(resolves, monkeypatch):
    """An uncached resolution is mkdir, write, fsync and link, plus two sleeps.

    It belongs on a worker thread, or the liveness route that exists to answer while
    every worker is busy becomes the thing that cannot answer.
    """
    hops = _ThreadHops()
    monkeypatch.setattr(instance_identity, "asyncio", hops)

    cold = asyncio.run(instance_identity.get_instance_id_async())
    assert uuid.UUID(cold)
    assert hops.hops == 1
    assert len(resolves) == 1


def test_settled_lookup_stays_on_the_calling_thread(resolves, monkeypatch):
    """The cached answer costs a dict read, not a thread handoff."""
    hops = _ThreadHops()
    monkeypatch.setattr(instance_identity, "asyncio", hops)
    cold = asyncio.run(instance_identity.get_instance_id_async())

    assert asyncio.run(instance_identity.get_instance_id_async()) == cold
    assert hops.hops == 1
    assert len(resolves) == 1


def test_a_probe_during_someone_elses_lookup_answers_at_once_without_the_id(monkeypatch):
    """The contract P1 reads: no id in this answer, ask again.

    One lookup per path at a time. The caller that asked first waits for it -- it has to,
    there is no other answer -- and a caller that arrives behind it answers immediately
    without the field rather than starting a thread that would only queue behind the first.
    On a directory stuck in ``mkdir`` that is the difference between one wedged thread for
    the life of the daemon and a fresh one on every probe.

    Losing the field for one response is safe; guessing at an identity is not. Absence
    already means "identity unknown" everywhere it is read.
    """
    started = threading.Event()
    release = threading.Event()
    published = str(uuid.uuid4())

    def hang(target):
        started.set()
        release.wait(10)
        return instance_identity._Result(instance_identity._Outcome.OK, published)

    hops = _ThreadHops()
    monkeypatch.setattr(instance_identity, "_resolve", hang)
    monkeypatch.setattr(instance_identity, "asyncio", hops)

    async def probe_while_busy():
        first = asyncio.ensure_future(instance_identity.get_instance_id_async())
        waited = 0.0
        while not started.is_set() and waited < 10.0:
            await asyncio.sleep(0.01)
            waited += 0.01
        second = await instance_identity.get_instance_id_async()
        release.set()
        return await first, second

    first, second = asyncio.run(probe_while_busy())

    assert first == published
    assert second is None, "a caller behind a lookup must answer at once, without a guess"
    assert hops.hops == 1, "every probe started a lookup of its own"


def test_a_broken_data_dir_answers_inline_after_the_first_try(
    tmp_path, monkeypatch, resolves, clock
):
    """The every-minute stall this prevents.

    With the id unobtainable, the retry used to run inside the handler: on a slow or
    broken state dir, one blocked event loop a minute, forever. Now only the first
    attempt leaves the loop and the backoff window answers from memory.
    """
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(_unwritable_dir(tmp_path)))
    hops = _ThreadHops()
    monkeypatch.setattr(instance_identity, "asyncio", hops)

    for _ in range(5):
        assert asyncio.run(instance_identity.get_instance_id_async()) is None

    assert hops.hops == 1
    assert len(resolves) == 1


def test_health_handler_uses_the_async_accessor(resolves, monkeypatch):
    """The wiring the tests above are only meaningful if the handler actually uses it.

    Calls the handler coroutine rather than a client, so the count is the handler's own.
    """
    hops = _ThreadHops()
    monkeypatch.setattr(instance_identity, "asyncio", hops)

    body = asyncio.run(api.health_check())

    assert uuid.UUID(body["instance_id"])
    assert hops.hops == 1
    assert len(resolves) == 1
