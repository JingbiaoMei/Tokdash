"""Rows written under the old whole-module session identity keep their rows.

The stored-session identity used to carry a hash of ``sources/coding_tools.py``
for two of its components, because that is what ``parser_code_signature()``
returns for an object living in a shared module. Every coding-tool parser lives
in that file, so a release that touched any parser invalidated every stored row
of the Codex and Kimi session corpora and reparsed them on upgrade -- measured
at 36 s of single-threaded CPU against a median history, on the first request
after the upgrade.

Signing the two objects themselves is only half the fix. The rows already in
everyone's database carry the old shape, so the release that narrows the
identity must still recognise them or it pays the stall once anyway. These hold
the recognition and, just as carefully, its limits: accepting a stale key
derivation would be worse than being slow.

See docs/development/technical-notes/USAGE_CACHE_IDENTITY.md.
"""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import tokdash.sessions as sessions
from tokdash.sources import coding_tools
from tokdash.usage_store import UsageEntryStore, build_source_signature

# sources/coding_tools.py as released under v2.6.1 through v2.6.4: the line in
# the field, and the hash a real database carries for every row it wrote.
RELEASED_CODING_TOOLS_SHA1 = "a623b0ca956c1d7787483cd7dcaaddcf5b6da2c5"
ACTIVITY_INSIGHTS_SHA1 = "73bba1974ff0b22c633a5e398e9db4fe947ae643"


def _session_file_signature(parser_identity: dict, path: str, stamp=(1, 100)) -> str:
    """The stored per-file signature, built the way the store builds it."""
    return build_source_signature(
        files=[(path, stamp[0], stamp[1])], parser=parser_identity, extra={"mode": "session-file"}
    )


def _current_identity(tool: str) -> dict:
    if tool == "codex":
        return sessions._codex_session_parser_signature()
    return sessions._kimi_session_parser_signature()


def _module_hashed_identity(tool: str, sha1: str = RELEASED_CODING_TOOLS_SHA1) -> dict:
    """What a pre-change build recorded: the dependency as its module's hash."""
    identity = dict(_current_identity(tool))
    if tool == "codex":
        identity["event_key"] = {
            "object": "tokdash.sources.coding_tools.codex_token_event_key",
            "content_sha1": sha1,
        }
        identity["activity"] = {
            "object": "tokdash.activity_insights.build_activity_insights",
            "content_sha1": ACTIVITY_INSIGHTS_SHA1,
        }
    else:
        identity["model_map"] = {
            "object": "tokdash.sources.coding_tools.KimiParser",
            "content_sha1": sha1,
        }
    return identity


def test_the_codex_identity_no_longer_signs_a_read_time_aggregator():
    """activity_insights.py aggregates the stored rows and writes none of them."""
    # The session-file parser writes the stored activity record, and both the
    # parser's own version token and the schema version that record carries stay
    # in the comparison, so signing the aggregator meant an edit to it reparsed
    # a corpus that could not have changed.
    identity = sessions._codex_session_parser_signature()

    assert "activity" not in identity
    assert identity["activity_schema"] == 1


def test_the_frozen_module_hashes_are_frozen_history():
    """The accepted set is released history, so widening it is a decision."""
    # Every entry claims a named release derived stored turn keys and model
    # names from code identical to the shipped one -- only ever true of releases
    # already out. The file this build ships is deliberately absent: a row
    # cannot vouch for itself.
    accepted = set(sessions._LEGACY_CODING_TOOLS_OBJECTS)

    assert len(accepted) == 16
    assert all(
        len(item) == 40 and all(ch in "0123456789abcdef" for ch in item)
        for item in accepted
    )
    assert RELEASED_CODING_TOOLS_SHA1 in accepted
    module_hash = hashlib.sha1(Path(coding_tools.__file__).read_bytes()).hexdigest()
    assert module_hash not in accepted


def test_every_accepted_release_pins_both_objects():
    """A release in the map with a component missing is a release not pinned.

    The map's key set IS the accepted set -- there is no second list to keep in
    step -- so an entry that names a module hash without pinning both objects
    would let that row resign on the strength of a hash that vouches for only
    one of them.
    """
    assert all(
        set(components) == {"event_key", "model_map"} and all(
            len(value) == 40 and all(ch in "0123456789abcdef" for ch in value)
            for value in components.values()
        )
        for components in sessions._LEGACY_CODING_TOOLS_OBJECTS.values()
    )


def test_the_pin_holds_today_side_of_the_migration():
    """The migration asserts a fact about twenty-six releases, so check it.

    A stored module hash vouches for what the object was when that release
    shipped. That only excuses today's object if today's object is the same one,
    so the pinned value and the live signature have to be the same number. If
    this fails, the release is not a no-op upgrade: it is a reparse, and the
    accepted set is lying about the rows it lets keep their data.
    """
    live = {
        "event_key": _current_identity("codex")["event_key"]["content_sha1"],
        "model_map": _current_identity("kimi")["model_map"]["content_sha1"],
    }

    assert live == {
        "event_key": sessions._CODEX_EVENT_KEY_SHA1,
        "model_map": sessions._KIMI_MODEL_MAP_SHA1,
    }
    assert all(components == live for components in
               sessions._LEGACY_CODING_TOOLS_OBJECTS.values())


@pytest.mark.parametrize(
    "tool, predicate",
    [
        ("codex", sessions._codex_session_signature_compatible),
        ("kimi", sessions._kimi_session_signature_compatible),
    ],
)
def test_editing_the_object_stops_old_rows_resigning(tool, predicate, tmp_path):
    """The gate that makes the migration honest, not just fast.

    A recognised legacy module hash says what the object used to be. If this
    build's object has since changed, resigning would stamp every old row with
    an identity that never produced it, and nothing could ever detect that the
    corpus was written by two different derivations. So the row declines the
    rewrite and reparses -- which is the slow answer, and the correct one.

    Without this gate the first release to touch either object silently freezes
    a wrong corpus in place: the rows look current, and they are not.
    """
    path = str(tmp_path / (tool + "-rollout.jsonl"))
    component = sessions._LEGACY_MODULE_HASH_COMPONENTS[tool][0]

    stored = _session_file_signature(_module_hashed_identity(tool), path)
    changed = dict(_current_identity(tool))
    changed[component] = {
        # This build's own label, which for Kimi names the three-object map
        # rather than the class the old build named. The bytes changed too,
        # and that is what the gate is for.
        "object": changed[component]["object"],
        "content_sha1": "9" * 40,
    }

    assert predicate(stored, _session_file_signature(changed, path)) is False

    # And the same stored row does move while the object is still the one the
    # release shipped, so the decline is about the change and not about shape.
    assert predicate(stored, _session_file_signature(_current_identity(tool), path)) is True


@pytest.mark.parametrize(
    "tool, predicate",
    [
        ("codex", sessions._codex_session_signature_compatible),
        ("kimi", sessions._kimi_session_signature_compatible),
    ],
)
def test_a_row_signed_with_the_module_hash_moves_identity_without_reparsing(
    tool, predicate, tmp_path
):
    path = str(tmp_path / (tool + "-rollout.jsonl"))
    old = _session_file_signature(_module_hashed_identity(tool), path)
    new = _session_file_signature(_current_identity(tool), path)

    assert old != new
    assert predicate(old, new) is True


@pytest.mark.parametrize(
    "tool, predicate",
    [
        ("codex", sessions._codex_session_signature_compatible),
        ("kimi", sessions._kimi_session_signature_compatible),
    ],
)
def test_the_recognition_is_gated_on_hashes_that_are_really_released(
    tool, predicate, tmp_path
):
    """An unknown module hash says nothing about the key derivation behind it."""
    # And a recognised legacy component excuses nothing else: a parser that
    # changed still reparses.
    path = str(tmp_path / (tool + "-rollout.jsonl"))
    new = _session_file_signature(_current_identity(tool), path)

    unknown = _session_file_signature(_module_hashed_identity(tool, "0" * 40), path)
    assert predicate(unknown, new) is False

    bumped = _module_hashed_identity(tool)
    bumped["parser"] = dict(bumped["parser"], version=bumped["parser"]["version"] + 1)
    assert predicate(_session_file_signature(bumped, path), new) is False


def test_the_live_sync_resigns_a_module_hashed_codex_corpus(tmp_path):
    """The store's own decision, not a predicate asserted in isolation."""
    # A reparse here is silent and correct-looking, which is exactly why the
    # migration needs a test at the level where the files get opened.
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    files = (
        (str(tmp_path / "rollout-a.jsonl"), 11, 900),
        (str(tmp_path / "rollout-b.jsonl"), 12, 900),
    )
    read: list[str] = []

    def parse(file_sig):
        read.append(file_sig[0])
        return {
            "tool": "codex",
            "session_id": "s-" + file_sig[0][-6],
            "turns": [{"turn_index": 1, "timestamp_ms": 1_700_000_000_000, "tokens": 10}],
        }

    assert store.sync_session_files(
        "codex", files, parser=_module_hashed_identity("codex"), parse_file_session=parse
    ) is True
    assert len(read) == 2

    read.clear()
    assert store.sync_session_files(
        "codex",
        files,
        parser=sessions._codex_session_parser_signature(),
        parse_file_session=parse,
        signature_compatible=sessions._codex_session_signature_compatible,
    ) is True
    assert read == []

    # Resigned onto the current identity, so the next sync is a no-op rather
    # than a second migration pass over the same rows.
    with closing(sqlite3.connect(store.path)) as conn:
        stored = conn.execute(
            "SELECT file_path, signature FROM session_records"
            " WHERE tool = 'codex' ORDER BY file_path ASC"
        ).fetchall()
    current = sessions._codex_session_parser_signature()
    assert [row[1] for row in stored] == [
        _session_file_signature(current, path, (mtime, size)) for path, mtime, size in files
    ]

    assert store.sync_session_files(
        "codex",
        files,
        parser=current,
        parse_file_session=parse,
        signature_compatible=sessions._codex_session_signature_compatible,
    ) is False
    assert read == []
