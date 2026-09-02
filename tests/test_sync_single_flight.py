"""One parse per change, however many threads and windows ask for it.

A dashboard refresh fans out to several routes, each of which syncs the stored
sources before reading, and the Overview reads two windows per route (current
and comparison). While a large rollout was being appended to, that turned into
the same multi-hundred-MB file being parsed once per thread and twice per
request, with the whole file held in memory each time. These tests pin the
three fixes: the parsers stream, concurrent syncs of one source collapse into
one, and the comparison window reads what the current window just synced.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tokdash.compute as compute
import tokdash.sessions as sessions
from tokdash.sources import coding_tools
from tokdash.sources.coding_tools import CodingToolsUsageTracker
from tokdash.usage_store import UsageEntryStore

TS = "2026-05-19T12:00:00Z"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TOKDASH_DATA_DIR", str(tmp_path / ".tokdash"))
    monkeypatch.setenv("TOKDASH_USAGE_DB", "1")
    for var, relative in (
        ("DSH_HOME", ".dsh"),
        ("GROK_HOME", ".grok"),
        ("REASONIX_HOME", ".reasonix"),
        ("OPENCLAW_HOME", ".openclaw"),
        ("ZCODE_HOME", ".zcode"),
        ("KIMI_SHARE_DIR", ".kimi"),
        ("KIMI_CODE_HOME", ".kimi-code"),
        ("PI_CODING_AGENT_DIR", ".pi/agent"),
    ):
        monkeypatch.setenv(var, str(tmp_path / relative))
    coding_tools._sig_cache.clear()
    coding_tools.BaseParser._entry_cache.clear()
    yield tmp_path


def _write_codex(home: Path, stem: str, turns: int = 2) -> Path:
    rows = [
        {"type": "session_meta", "payload": {"id": stem, "cwd": "/w", "timestamp": TS}},
        {"type": "turn_context", "payload": {"model": "gpt-5"}},
    ]
    for index in range(turns):
        rows.append(
            {
                "type": "event_msg",
                "timestamp": TS,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 3_000,
                            "cached_input_tokens": 2_000,
                            "output_tokens": 100,
                            "reasoning_output_tokens": 50,
                        }
                    },
                    "id": f"{stem}-{index}",
                },
            }
        )
    path = home / ".codex" / "sessions" / "2026" / "05" / "19" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _write_claude(home: Path, stem: str, message_ids: tuple[str, ...] = ("m1", "m2")) -> Path:
    rows = [
        {
            "sessionId": stem,
            "cwd": "/w",
            "timestamp": TS,
            "message": {
                "role": "assistant",
                "id": message_id,
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input_tokens": 1_000,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 2_000,
                    "cache_creation_input_tokens": 500,
                },
            },
        }
        for message_id in message_ids
    ]
    path = home / ".claude" / "projects" / "proj" / f"{stem}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


# --- the parsers stream -----------------------------------------------------


@pytest.mark.parametrize("source", ["codex", "claude"])
def test_usage_parsers_stream_instead_of_reading_whole_file(monkeypatch, _isolated_home, source):
    """Neither parser may materialise a log with read_text() any more."""
    if source == "codex":
        _write_codex(_isolated_home, "s1")
    else:
        _write_claude(_isolated_home, "s1")

    original_read_text = Path.read_text

    def forbidden(self, *args, **kwargs):
        # Only the logs are in question. Other reads stay allowed: on a fresh
        # process the platform detector reads /proc/version on the way here.
        if self.suffix == ".jsonl":
            raise AssertionError(f"read_text() on {self}")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbidden)
    entries = CodingToolsUsageTracker().parsers[source]._parse_all()
    assert len(entries) == 2


def test_streaming_parse_matches_splitlines_semantics(_isolated_home):
    """CRLF endings, a blank line and a torn final line parse as before."""
    path = _write_codex(_isolated_home, "s1", turns=2)
    text = path.read_text(encoding="utf-8").replace("\n", "\r\n")
    path.write_text(text + "\r\n" + '{"type": "event_msg", "payload": {"type": "token_count"', encoding="utf-8")
    entries = CodingToolsUsageTracker().parsers["codex"]._parse_all()
    assert len(entries) == 2
    assert entries[0]["entry_id"] != entries[1]["entry_id"]


# --- concurrent syncs of one source collapse into one -------------------------


def _race(first_call, second_call, parse_started: threading.Event, release: threading.Event):
    """Start one sync; let a second arrive while the first is parsing; release both."""
    results: dict[str, object] = {}

    def first():
        results["first"] = first_call()

    def second():
        results["second"] = second_call()

    t1 = threading.Thread(target=first)
    t1.start()
    assert parse_started.wait(5)
    t2 = threading.Thread(target=second)
    t2.start()
    # Give the second caller time to reach the gate while the first still holds
    # it; nothing observable distinguishes "parked" from "not started yet", so
    # this is a grace period rather than a wait on a condition.
    time.sleep(0.2)
    release.set()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    return results


def _grow(path: Path) -> tuple[str, int, int]:
    """Append to a log, as a live client does between two requests' scans."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def test_concurrent_sync_files_parse_once(_isolated_home):
    path = _write_codex(_isolated_home, "s1")
    stat = path.stat()
    parse_started = threading.Event()
    release = threading.Event()
    parses: list[str] = []

    def parse(file_sig):
        parses.append(file_sig[0])
        parse_started.set()
        assert release.wait(5)
        return [
            {
                "source": "codex",
                "model": "gpt-5",
                "provider": "openai",
                "input": 1,
                "output": 1,
                "cacheRead": 0,
                "cacheWrite": 0,
                "reasoning": 0,
                "cost": 0.0,
                "timestamp": 1_779_278_400_000,
                "entry_id": "e1",
            }
        ]

    def call_sync(sig):
        return UsageEntryStore().sync_files(
            "codex",
            [sig],
            parser={"object": "test", "version": 1},
            parse_file_entries=parse,
        )

    first_sig = (str(path), stat.st_mtime_ns, stat.st_size)
    # The second request scanned after the client appended, so its signature is
    # newer than the one being synced. Without the gate it would parse the whole
    # file again for a few more bytes.
    later_sig = _grow(path)
    results = _race(lambda: call_sync(first_sig), lambda: call_sync(later_sig), parse_started, release)
    assert parses == [str(path)], "the waiter re-parsed a file the holder had just synced"
    assert results["first"] is True
    assert results["second"] is False

    # A later, sequential call with that newer signature still syncs: the gate
    # only collapses callers that overlapped in time.
    parses.clear()
    assert call_sync(later_sig) is True
    assert parses == [str(path)]


def test_concurrent_sync_session_files_parse_once(_isolated_home):
    path = _write_claude(_isolated_home, "s1")
    stat = path.stat()
    parse_started = threading.Event()
    release = threading.Event()
    parses: list[str] = []

    def parse(file_sig):
        parses.append(file_sig[0])
        parse_started.set()
        assert release.wait(5)
        return {"id": "s1", "turns": [], "started_at_ms": 1, "last_seen_at_ms": 2}

    def call_sync(sig):
        return UsageEntryStore().sync_session_files(
            "claude",
            [sig],
            parser={"object": "test", "version": 1},
            parse_file_session=parse,
        )

    first_sig = (str(path), stat.st_mtime_ns, stat.st_size)
    later_sig = _grow(path)
    results = _race(lambda: call_sync(first_sig), lambda: call_sync(later_sig), parse_started, release)
    assert parses == [str(path)]
    assert results["first"] is True
    assert results["second"] is False
    parses.clear()
    assert call_sync(later_sig) is True
    assert parses == [str(path)]


def test_single_flight_does_not_skip_after_the_holder_failed(_isolated_home):
    """A waiter whose holder raised must sync itself, or its rows go missing."""
    path = _write_codex(_isolated_home, "s1")
    stat = path.stat()
    parse_started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def parse(file_sig):
        calls.append("parse")
        parse_started.set()
        assert release.wait(5)
        if len(calls) == 1:
            raise RuntimeError("torn read")
        return []

    def call_sync():
        try:
            return UsageEntryStore().sync_files(
                "codex",
                [(str(path), stat.st_mtime_ns, stat.st_size)],
                parser={"object": "test", "version": 1},
                parse_file_entries=parse,
            )
        except RuntimeError:
            return "raised"

    results = _race(call_sync, call_sync, parse_started, release)
    assert results["first"] == "raised"
    assert results["second"] is True
    assert calls == ["parse", "parse"]


# --- the comparison window reads what the current window synced ---------------


def test_tools_data_for_range_sync_false_never_syncs(monkeypatch):
    def explode(tracker):
        raise AssertionError("sync=False must not sync")

    monkeypatch.setattr(compute, "_sync_usage_store", explode)
    now = datetime.now(timezone.utc)
    data = compute.get_tools_data_for_range(now - timedelta(days=1), now, sync=False)
    assert data["total_tokens"] == 0


def test_usage_with_comparison_syncs_once(monkeypatch, _isolated_home):
    _write_codex(_isolated_home, "s1")
    original = compute._sync_usage_store
    calls: list[str] = []

    def counting(tracker):
        calls.append("sync")
        return original(tracker)

    monkeypatch.setattr(compute, "_sync_usage_store", counting)
    payload = compute.compute_usage_with_comparison("7")
    assert calls == ["sync"], "the previous window synced again"
    assert "comparison" in payload


def test_active_time_syncs_each_stored_tool_once(monkeypatch):
    synced: list[str] = []

    def counting(self, tool, *args, **kwargs):
        synced.append(tool)
        return False

    monkeypatch.setattr(UsageEntryStore, "sync_session_files", counting)
    sessions.get_active_time_data("today")
    stored = [tool for tool in synced if tool in {"codex", "claude"}]
    assert sorted(stored) == ["claude", "codex"], synced


def test_suppressed_stored_read_skips_sync_and_scan(monkeypatch):
    monkeypatch.setattr(
        UsageEntryStore,
        "sync_session_files",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("synced")),
    )
    monkeypatch.setattr(
        sessions,
        "_iter_file_signatures",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked the project tree")),
    )
    with sessions._store_sync_suppressed():
        assert sessions._stored_sessions_for_tool("claude") == {}
    assert sessions._store_sync_wanted() is True
