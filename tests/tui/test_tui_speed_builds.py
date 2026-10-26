"""The Speed build stream: following a snapshot that is still being built.

A snapshot can name a build in progress. The pane subscribes to that job's
events, prints the progress the server sends without re-reading, re-reads the
cache exactly once when the build lands, and stops claiming a build once it has.
The subscription belongs to the pane, the window and the job: leaving the pane
closes it, an event for another job cannot move the note, a reload or a series
change keeps the stream already open, and a stream that ends or is cancelled
early is named as a lost connection rather than re-subscribed. Only a manual
reload asks for a rebuild. Pilots over the contract-shaped payloads in
``support.tui``.
"""
from __future__ import annotations

import copy
import threading

import pytest

textual = pytest.importorskip("textual", reason="the TUI tests need textual")

import tokdash.tui.app as app_mod  # noqa: E402
from tokdash.tui.data import FetchOutcome  # noqa: E402
from support.tui import (  # noqa: E402
    SPEED_ACROSS,
    api,  # noqa: F401  (the fixture these tests ask pytest for)
    press,
    run,
    settle,
    visible,
)


# --------------------------------------------------------------------------- #
# progress, completion and the one read that may rebuild
# --------------------------------------------------------------------------- #


def test_build_progress_from_the_job_events_reaches_the_note(api):
    """A progress event is a NOTE with numbers, and never a re-read.

    ``_on_speed_event`` is the very entry point the SSE reader calls through
    ``call_from_thread``, so this is the stream's own path into the pane.
    """

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        assert len(api.called("speed")) == 1
        # What following a queued build actually looks like: the pane knows the
        # job it subscribed for and the subscription token it subscribed under
        # (an event from an older build, or a stale stream, is dropped).
        app._sp_following = True
        app._sp_follow_job = "job-9"
        app._sp_follow_epoch = app._sp_follow_token
        app._on_speed_event({"id": "job-9", "state": "building",
                             "completed_inputs": 3, "total_inputs": 9})
        await settle(pilot, 2)
        note = visible(app, "#sp-note")
        assert "building 3/9" in note, note     # the numbers the server sent
        assert "last-good rows shown" in note, note
        assert len(api.called("speed")) == 1, api.called("speed")  # progress is free
        app._on_speed_event({"id": "job-9", "state": "error"})
        await settle(pilot, 2)
        assert "build failed" in visible(app, "#sp-note")

    run(api, body)


def test_a_build_stream_that_ends_early_is_a_lost_connection_not_a_rebuild_loop(api):
    """A stream that closes mid-build is named once, and never re-subscribed."""

    async def body(app, pilot):
        api.speed_across = copy.deepcopy(SPEED_ACROSS)
        api.speed_across["cache"] = {"state": "building", "job_id": "job-7",
                                     "updated_at": None}
        api.follow_events = [{"state": "building", "completed_inputs": 4,
                              "total_inputs": 9}]  # and then the stream just stops
        app.action_show_pane("speed")
        await settle(pilot, 5)
        note = visible(app, "#sp-note")
        assert "update connection ended" in note, note
        assert "r Reload" in note, note
        reads = len(api.called("speed"))
        follows = len([c for c in api.calls if c[0] == "job_status"])
        app._paint_speed()   # every later paint would re-arm the subscription
        await settle(pilot, 5)
        assert len(api.called("speed")) == reads, "a dropped stream was re-subscribed"
        assert follows == 0  # no status polling behind the stream's back either

    run(api, body)


def test_build_completion_re_reads_once_and_stops_claiming_a_build(api):
    """The completion re-read is cache-only, so it cannot re-queue the build."""

    async def body(app, pilot):
        rebuilt = copy.deepcopy(SPEED_ACROSS)  # what the service holds once built
        api.speed_across = copy.deepcopy(SPEED_ACROSS)
        api.speed_across["cache"] = {"state": "building", "job_id": "job-7",
                                     "updated_at": None}
        api.follow_events = [
            {"state": "building", "completed_inputs": 3, "total_inputs": 9},
            {"state": "ready", "completed_inputs": 9, "total_inputs": 9},
        ]
        served: list[object] = []

        def serve(*args, **kwargs):
            # The build lands between reads: the first read is the building
            # snapshot, the cache-only re-read is shown the rebuilt one.
            served.append(kwargs.get("cache_only"))
            if len(served) < 2:
                return None
            return FetchOutcome(value=copy.deepcopy(rebuilt), status="hit",
                                age_seconds=1.0)

        api.hooks["speed"] = serve
        app.action_show_pane("speed")
        await settle(pilot, 5)
        speed_reads = api.called("speed")
        assert len(speed_reads) == 2, speed_reads   # completion re-read EXACTLY once
        assert served[0] is False and served[1] is True, served
        last = speed_reads[-1][2]
        assert last["cache_only"] is True, last      # and can not queue a new build
        assert last["refresh"] is False, last
        note = visible(app, "#sp-note")
        assert "building" not in note, note          # the pane says it is finished
        assert "cache ready" in note, note

    run(api, body)


def test_manual_reload_asks_for_a_rebuild_and_cache_reads_never_do(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 4)
        first = api.last("speed")[2]
        assert first["refresh"] is False, first
        assert first["cache_only"] is False, first
        app.action_refresh()
        await settle(pilot, 4)
        again = api.last("speed")[2]
        assert again["refresh"] is True, again
        assert again["cache_only"] is False, again  # never both: cache_only wins server-side
        press(app, "sp-mode-trend")  # a follow-up read is cache-only
        await settle(pilot, 4)
        trend = [c for c in api.called("speed") if c[2].get("view") == "trend"][-1]
        assert trend[2]["cache_only"] is True and trend[2]["refresh"] is False

    run(api, body)


# --------------------------------------------------------------------- F6 --
# A build subscription used to outlive the pane, the window and the source.


def test_leaving_speed_closes_the_live_build_subscription(api, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def follow(job_id, on_event, **kwargs):
        started.set()
        gate.wait(8)
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)
    api.speed_across["cache"] = {"state": "building", "job_id": "review-job"}

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            assert started.is_set()
            app.action_show_pane("overview")
            await settle(pilot, 1)
            assert not app._sp_following, "the hidden pane still followed the job"
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


def test_the_follower_hands_the_transport_a_predicate_that_tracks_the_pane(
    api, monkeypatch
):
    """The stream is told, in its own terms, when it is no longer wanted.

    The follow runs on the thread it was declared for, so the predicate has to
    be readable from that thread: it is the only way a reader who left the pane
    stops a build that no longer has anywhere to be painted.
    """
    seen: dict = {}

    def follow(job_id, on_event, **kwargs):
        stop = kwargs.get("stop")
        seen["stop"] = stop
        seen["while_following"] = stop() if callable(stop) else None
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)

    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        await app._speed_job_follow("a-queued-job").wait()
        stop = seen.get("stop")
        assert callable(stop), f"the reader was given no way to leave: {seen}"
        assert seen["while_following"] is False, "the guard reads as already-departed"
        app._stop_speed_follow()
        assert stop() is True, "the reader left and the stream cannot notice"

    run(api, body)


def test_an_event_for_another_job_cannot_move_the_note(api):
    async def body(app, pilot):
        app.action_show_pane("speed")
        await settle(pilot, 2)
        app._sp_following = True
        app._sp_follow_job = "the-job-in-force"
        app._sp_follow_epoch = app._sp_follow_token
        app._sp_job_note = "the note the reader is looking at"
        app._on_speed_event({"id": "a-job-from-an-older-load", "state": "error"})
        assert app._sp_job_note == "the note the reader is looking at"
        # Walking away drops the events even when the job id does match.
        app.action_show_pane("overview")
        await settle(pilot, 1)
        app._on_speed_event({"id": "the-job-in-force", "state": "error"})
        assert app._sp_job_note == "the note the reader is looking at"

    run(api, body)


# --------------------------------------------------------------------- R2-F1 --
# A reload re-issues the read; it does not retire the build that was already
# queued for the very same scope.


def test_a_reload_during_a_build_keeps_the_stream_and_rereads_when_it_lands(api, monkeypatch):
    gate = threading.Event()
    followers: list[str] = []

    def follow(job_id, on_event, **kwargs):
        followers.append(job_id)
        gate.wait(8)
        on_event({"id": job_id, "state": "ready"})
        return 1

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)
    api.speed_across = copy.deepcopy(api.speed_across)
    api.speed_across["cache"] = {"state": "building", "job_id": "review-job"}

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            assert followers == ["review-job"], followers
            await pilot.press("r")
            await settle(pilot, 2)
            # The reload moved the generation; the build did not change, so the
            # stream that was already open is still the one in force.
            assert app._sp_following, "the reload dropped a live subscription"
            assert app._sp_follow_job == "review-job"
            assert app._sp_follow_epoch == app._sp_follow_token
            before = len(api.called("speed"))
            api.speed_across["cache"] = {"state": "ready"}
            gate.set()
            await settle(pilot, 3)
            assert len(api.called("speed")) > before, (
                "the build landed but nothing re-read the snapshot: "
                f"followers={followers} note={app._sp_job_note!r}"
            )
            assert not app._sp_following, "a finished build still holds the flag"
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


def test_a_snapshot_naming_a_newer_build_hands_the_note_to_it(api, monkeypatch):
    gate = threading.Event()
    followers: list[str] = []

    def follow(job_id, on_event, **kwargs):
        followers.append(job_id)
        gate.wait(8)
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)
    api.speed_across = copy.deepcopy(api.speed_across)
    api.speed_across["cache"] = {"state": "building", "job_id": "job-one"}

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            assert followers == ["job-one"], followers
            # The next snapshot names a DIFFERENT build. The old stream cannot be
            # allowed to keep the note, and the flag cannot block the new job.
            api.speed_across["cache"] = {"state": "building", "job_id": "job-two"}
            app._restart_speed()
            await settle(pilot, 2)
            assert followers == ["job-one", "job-two"], followers
            assert app._sp_follow_job == "job-two", app._sp_follow_job
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


# --------------------------------------------------------------------- R3-F2 --
# A series change is a view change. It may not cancel the build subscription.


@pytest.mark.parametrize("mode", ["trend", "hour"])
def test_choosing_a_series_keeps_the_build_subscription(api, monkeypatch, mode):
    gate = threading.Event()
    followers: list[str] = []

    def follow(job_id, on_event, **kwargs):
        followers.append(job_id)
        gate.wait(12)
        on_event({"id": job_id, "state": "ready"})
        return 1

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            press(app, f"sp-mode-{mode}")
            await settle(pilot, 2)
            api.speed_across["cache"] = {"state": "building", "job_id": "series-job"}
            await pilot.press("r")
            await settle(pilot, 2)
            assert followers == ["series-job"], followers
            before = len(api.called("speed"))

            # A series change: new selection, new read -- and the stream stays.
            await pilot.press("k")
            await settle(pilot, 1)
            await pilot.press("enter")
            await settle(pilot, 3)
            assert app._sp_following, "the series change dropped a live subscription"
            assert app._sp_follow_job == "series-job"
            assert app._sp_follow_epoch == app._sp_follow_token

            # The build lands. Exactly one cache-only reread, and the flag down.
            api.speed_across["cache"] = {"state": "ready"}
            gate.set()
            await settle(pilot, 4)
            snapshot_rereads = [
                c for c in api.called("speed")[before:]
                if c[2].get("view") == "across-models" and c[2].get("cache_only")
            ]
            assert len(snapshot_rereads) == 1, {
                "rereads": len(snapshot_rereads),
                "note": app._sp_job_note,
                "followers": followers,
            }
            assert not app._sp_following, "a finished build still holds the flag"
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)


def test_a_cancelled_subscription_says_so_instead_of_staying_following(
    api, monkeypatch
):
    """The flag cannot survive the loss of the worker that owned it.

    Whatever takes the reader's stream away -- a cancellation, a dead connection
    -- the pane must stop claiming it is watching, or it never re-subscribes to
    the build that replaced it.
    """
    gate = threading.Event()
    worker_box: dict = {}

    def follow(job_id, on_event, **kwargs):
        worker_box["stop"] = kwargs.get("stop")
        gate.wait(12)
        return 0

    monkeypatch.setattr(app_mod, "follow_output_speed_job", follow)

    async def body(app, pilot):
        try:
            app.action_show_pane("speed")
            await settle(pilot, 2)
            api.speed_across = copy.deepcopy(api.speed_across)
            api.speed_across["cache"] = {"state": "building", "job_id": "gone-job"}
            await pilot.press("r")
            await settle(pilot, 2)
            assert app._sp_following
            stream = next(
                w for w in app.workers._workers if w.group == "speed-follow-io"
            )
            stream.cancel()
            gate.set()
            await settle(pilot, 3)
            assert not app._sp_following, "the flag outlived its own worker"
            assert "update connection lost" in visible(app, "#sp-note"), visible(app, "#sp-note")
            assert "re-reading snapshot" not in visible(app, "#sp-note")
        finally:
            gate.set()
            await settle(pilot, 1)

    run(api, body)
