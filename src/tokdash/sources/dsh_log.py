"""Shared DeepSeek Harness (dsh) session-log decoding.

Both the usage parser (``coding_tools.DSHParser``) and the session parser
(``sessions._parse_dsh_session_file``) go through this module so the framing
rules live in exactly one place: multi-frame zstd, torn tails, header-version
gating, fork seed boundaries, model attribution, and the (turn, step)
replace-not-add usage fold. Nothing here prices tokens or knows about Tokdash
entry shapes.

Format reference: docs/development/technical-notes/DSH_SUPPORT_DESIGN.md.
"""
from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zstandard import ZstdDecompressor

logger = logging.getLogger(__name__)

# Bump when framing/extraction semantics change; included in the persistent
# session-store signature so stored rows reparse instead of going stale.
# 2: zstd frames decode independently, and the header-version gate became a
#    generation set (v0, v3, v4).
DSH_DECODER_VERSION = 2
# Bump when usage-accounting rules change (fold keys, seed boundary, zero skip).
# 2: fold_dsh_usage_samples keys its replace-not-add fold on the whole file's
#    (turn, step) instead of only the previous sample, so a non-adjacent repeat
#    yields one sample instead of two, and the v4 seed boundary is derived from
#    the session/end-seed marker. Stored rows must reparse.
DSH_ACCOUNTING_VERSION = 2

# Format generations Tokdash reads. A version outside this set is unsupported,
# not corrupt — the file is skipped, never treated as empty.
#
# 0 is the developer-preview layout. 4 is what dsh >= 0.2.0 writes
# (SESSION_FORMAT_VERSION in @deepseek-ai/dsh-session); its usage events keep
# the v0 shape (assistant/message with data.turn/step/data.usage), so the fold
# reads both. dsh names those files session.v4.jsonl[.zstd]; discovery matches
# them on the suffix. 3 is what earlier dsh builds left on disk: its
# header has the v4 key set, and dsh's own v3 reader cuts a seeded log at the
# final inherited end-seed marker exactly as v4 does, so the same fold and seed
# boundary apply (a real 24-log v3/v4 corpus decoded whole, #147). Generations
# 1-2 were superseded in place upstream and are not claimed here until a corpus
# proves the read.
SUPPORTED_SESSION_FORMAT_VERSIONS = frozenset({0, 3, 4})

# zstd frame magic (LE 0xFD2FB528), the resync marker for a damaged frame.
_ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"


def _decode_dsh_zstd_frames(raw: bytes) -> Tuple[bytes, int]:
    """Decode a dsh log frame-by-frame, returning ``(text, failed_frames)``.

    The recovery path for a file whose single-pass read raised. dsh concatenates
    one independently framed, checksummed zstd stream per durable append batch
    (DSH_SUPPORT_DESIGN.md, "Zstandard decoding"), so a corrupt frame need not
    cost the file. Each frame is decompressed on its own; a frame that raises is
    counted and skipped, and the scan resumes at the next frame magic. Rows before
    and after a bad byte therefore survive -- the same containment the torn tail
    already got, extended to interior damage. A single
    ``stream_reader(read_across_frames=True)`` call aborts on the first bad frame
    and loses the whole file, which is why the fast path cannot stay the only one.

    Quadratic in frame count: ``unused_data`` hands back the rest of the buffer as
    a fresh bytes object per frame. Keep it off the healthy path.

    A skipped frame's rows are genuinely gone (they are compressed inside it);
    the count is returned so callers can surface the loss instead of reporting a
    silently short file. Never raises.
    """
    decompressor = ZstdDecompressor()
    out = bytearray()
    remaining = raw
    failed = 0
    while remaining:
        try:
            context = decompressor.decompressobj()
            out += context.decompress(remaining)
            out += context.flush()
            nxt = context.unused_data
        except Exception:
            failed += 1
            # Resync: the next frame starts at the next magic after this frame's
            # own header. A false positive only costs a frame (the bogus slice
            # fails and is skipped too), so recovery cannot invent rows.
            nxt = remaining.find(_ZSTD_FRAME_MAGIC, 4)
            if nxt < 0:
                break
            remaining = remaining[nxt:]
            continue
        if not nxt or nxt == remaining:
            # No progress: either the stream ended or the decoder consumed
            # nothing. Stop rather than spin on the same bytes forever.
            break
        remaining = nxt
    return bytes(out), failed



@dataclass(frozen=True)
class DSHDecodedSession:
    """One decoded dsh log: the session header, the event rows, or why not.

    ``failed_frames`` counts zstd frames that would not decode while the file
    still produced a usable header. It is never a ``skip_reason``: the file is
    short, not absent, and the caller is expected to say so out loud.
    """

    header: Optional[Dict[str, Any]] = None
    events: Tuple[Dict[str, Any], ...] = ()
    skip_reason: Optional[str] = None
    failed_frames: int = 0


# A file that cannot be read is skipped, and a source whose every session is
# unreadable is indistinguishable from a user who simply stopped using dsh. That
# is how this bug stayed silent. Both consumers report through one function so a
# broken file is named once per problem rather than once per parse (log spam)
# or never (the behavior this replaces).
_reported: "OrderedDict[tuple, None]" = OrderedDict()
_REPORT_MAX = 512


def report_dsh_diagnostic(path: Any, kind: str, detail: str = "") -> None:
    """Warn once per ``(path, kind, detail)`` about one unreadable dsh log.

    ``kind`` is the stable classifier (``skip:unsupported-version``,
    ``frames-lost``, ``seed-unprovable``) so tests and log filters key on it
    instead of on prose.
    """
    key = (str(path), kind, detail)
    if key in _reported:
        _reported.move_to_end(key)
        return
    _reported[key] = None
    while len(_reported) > _REPORT_MAX:
        _reported.popitem(last=False)
    logger.warning(
        "tokdash dsh: %s [%s]%s", path, kind, f" {detail}" if detail else ""
    )


def report_dsh_decode(path: Any, decoded: "DSHDecodedSession") -> None:
    """Report everything one decode lost, identically for both surfaces.

    The usage parser and the Sessions panel decode the same file on their own
    schedules -- after a restart the persistent store re-reads only changed files,
    so the panel may be the only surface that ever sees an unchanged broken one.
    Each calls this, with one wording per problem, so the registry above names a
    file once no matter which surface read it, and neither can be the silent one.
    """
    if decoded.skip_reason is not None or decoded.header is None:
        report_dsh_diagnostic(path, f"skip:{decoded.skip_reason or 'missing-header'}")
        return
    if decoded.failed_frames:
        report_dsh_diagnostic(
            path,
            "frames-lost",
            f"{decoded.failed_frames} zstd frame(s) did not decode; their rows are missing",
        )
    if dsh_seed_boundary(decoded.header, decoded.events)[1]:
        report_dsh_diagnostic(
            path,
            "seed-unprovable",
            "seeded session with no inherited session/end-seed marker; billing nothing "
            "rather than double-billing the parent's inherited prefix",
        )


def reset_dsh_diagnostics() -> None:
    """Forget what has been reported. For tests and explicit re-scans."""
    _reported.clear()


def dsh_file_signatures(root: Path) -> Tuple[Tuple[str, int, int], ...]:
    """Sorted ``(path, mtime_ns, size)`` for every dsh session log under *root*.

    One recursive pass covers both suffixes (``.jsonl`` and ``.jsonl.zstd``);
    a second scan for the alternate suffix would double the walk.
    """
    if not root.exists():
        return ()
    items: List[Tuple[str, int, int]] = []
    for path in root.rglob("*.jsonl*"):
        name = path.name
        if not (name.endswith(".jsonl") or name.endswith(".jsonl.zstd")):
            continue
        try:
            stat = path.stat()
        except (FileNotFoundError, OSError):
            continue
        items.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(sorted(items))


def decode_dsh_session_file(path: Path) -> DSHDecodedSession:
    """Decode one dsh log into its header and event rows.

    Never raises: every failure mode returns a structured ``skip_reason`` so a
    single malformed file cannot blank the whole source. Only complete
    newline-terminated JSON rows are kept; a torn final line (dsh can append
    while Tokdash reads) is discarded.

    A corrupt interior frame costs only its own rows (see
    :func:`_decode_dsh_zstd_frames`). It fails CLOSED on the header, though:
    without the first row there is no ``version`` to gate and no session id, and
    guessing either for an ungated format is worse than skipping the file.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return DSHDecodedSession(skip_reason="read-error")

    failed_frames = 0
    if str(path).endswith(".jsonl.zstd"):
        # Not one plain zstd stream: dsh concatenates a separately framed and
        # checksummed zstd frame per durable append batch. Healthy files read that
        # in one streaming pass, exactly as before -- and they must, because the
        # per-frame recovery below re-slices the remaining buffer at every frame
        # and is quadratic in frame count (measured 117x slower than this call on
        # a 20,000-frame file, and a live session reaches thousands). Recovery is
        # the exception path, entered only when the streaming read raises, so a
        # rare corrupt byte costs that file instead of every refresh of every log.
        try:
            raw = ZstdDecompressor().stream_reader(raw, read_across_frames=True).read()
        except Exception:
            raw, failed_frames = _decode_dsh_zstd_frames(raw)
            if failed_frames and not raw:
                # Every frame failed: nothing was recovered, so this is the old
                # whole-file decode failure rather than a short-but-readable log.
                return DSHDecodedSession(
                    skip_reason="decode-error", failed_frames=failed_frames
                )

    def skip(reason: str) -> DSHDecodedSession:
        # Carry the frame count on every skip so a caller diagnosing a short
        # source can tell "corrupt" from "unsupported" from "empty".
        return DSHDecodedSession(skip_reason=reason, failed_frames=failed_frames)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return skip("decode-error")

    if text and not text.endswith("\n"):
        last_newline = text.rfind("\n")
        text = text[: last_newline + 1] if last_newline >= 0 else ""

    header: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if header is None:
                return skip("invalid-header")
            continue
        if not isinstance(row, dict):
            if header is None:
                return skip("invalid-header")
            continue
        if header is None:
            if row.get("type") != "session":
                return skip("missing-header")
            if row.get("version") not in SUPPORTED_SESSION_FORMAT_VERSIONS:
                return skip("unsupported-version")
            header = row
            continue
        events.append(row)

    if header is None:
        return skip("missing-header")
    return DSHDecodedSession(header=header, events=tuple(events), failed_frames=failed_frames)


def _to_int(value: Any) -> Optional[int]:
    """An explicit non-negative integer, or None when absent or invalid."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def dsh_seed_boundary(
    header: Dict[str, Any],
    events: Tuple[Dict[str, Any], ...],
) -> Tuple[int, bool]:
    """Return ``(exclusive_seed_boundary_seq, fail_closed)`` for one log.

    Events before the boundary are inherited from a parent log and belong to the
    parent's bill. v0 declares it as ``header.seedLength``. v4 dropped that field:
    the cut is now a ``session/end-seed`` event carrying ``data.inherited: true``,
    and the boundary is that marker's ``seq`` (dsh's ``buildForkSeed`` copies the
    parent's events through ``boundary`` inclusive and appends the marker at
    ``boundary + 1``, so ``seq < marker.seq`` is the inherited prefix).

    **Only ``inherited: true`` markers are cuts.** dsh appends a plain
    ``session/end-seed`` with empty data on every *resume* of a session, seeded
    or not (``dsh-session`` constructor: ``this.append("session/end-seed", {})``). Those
    are resume bookkeeping, not a cut, and they land at the end of the log — so
    taking the last marker of either kind would move the cut past every forked
    session's own work and stop billing it from the first resume onward, a little
    more each time. dsh's own invariant is that the seeded prefix is identified by
    the FINAL ``inherited: true`` marker, so filtering on that flag and taking the
    highest seq is the cut.

    The cut applies only when ``header.isSeeded`` is true. A resumed unseeded
    session needs no special case: it has no ``inherited`` marker to find.

    ``fail_closed`` is True when a seeded log has no such marker: no boundary can
    be proven, and a guessed one either double-bills the parent or drops real
    work. Callers must then bill nothing from the file and say so.
    """
    boundary = _to_int(header.get("seedLength")) or 0
    if boundary:
        return boundary, False
    if header.get("isSeeded") is not True:
        return 0, False
    markers = [
        seq
        for event in events
        if event.get("type") == "session/end-seed"
        and isinstance(event.get("data"), dict)
        and event["data"].get("inherited") is True
        and (seq := _to_int(event.get("seq"))) is not None
    ]
    if not markers:
        return 0, True
    return max(markers), False


def fold_dsh_usage_samples(
    header: Dict[str, Any],
    events: Tuple[Dict[str, Any], ...],
) -> List[Dict[str, Any]]:
    """Fold provider usage events into one sample per ``(turn, step)``.

    Two event shapes report usage: an early ``assistant/chunk`` whose
    ``chunk.type == "usage"``, and the finalized ``assistant/message`` carrying
    ``data.usage``. The fold is replace-not-add against a map keyed on the whole
    file's ``(turn, step)``, so the LAST sample for a key wins wherever it sits:
    a final message replaces its earlier chunk instead of double-counting it, an
    early chunk with no final message stays counted, and a key that repeats
    non-adjacently still yields exactly one sample.

    Keying the whole file rather than only the previous sample is what keeps the
    dashboard's two surfaces in agreement. ``coding_tools.DSHParser`` and
    ``sessions._parse_dsh_session_file`` both consume this list and both key
    their own dedup on :func:`dsh_entry_id`, which carries no file path — so a
    second sample sharing a ``(turn, step)`` collapsed to one entry in the usage
    view while staying two turns in the Sessions view, and the two surfaces
    reported different totals for one corpus. One sample per key makes that
    impossible by construction.

    Events before the fork boundary are inherited from the parent log and
    skipped; ``parentSession`` alone skips nothing, and with a declared boundary
    an event whose ``seq`` is unreadable is skipped too (fail closed). All-zero
    samples, samples with any negative bucket, and samples without an explicit
    numeric input/output pair or a usable event time are dropped — an absent
    usage object is not zero usage.
    """
    seed_length, seed_unprovable = dsh_seed_boundary(header, events)
    # Insertion-ordered: a replaced key keeps the position of its first sighting,
    # so the emitted order stays the log's order rather than the fold's.
    samples: Dict[Tuple[int, int], Dict[str, Any]] = {}
    latest_model = ""
    latest_provider = ""

    for event in events:
        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}

        if event_type == "request/context":
            model = str(data.get("model") or "").strip()
            if model:
                latest_model = model
            provider = str(data.get("provider") or "").strip()
            if provider:
                latest_provider = provider
            continue

        if event_type == "assistant/chunk":
            chunk = data.get("chunk") if isinstance(data.get("chunk"), dict) else {}
            if chunk.get("type") != "usage":
                continue
            usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else None
            if usage is None:
                continue
            # A usage-only chunk carries no provenance of its own.
            model = latest_model
            provider = latest_provider
        elif event_type == "assistant/message":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            if usage is None:
                continue
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            source = message.get("source") if isinstance(message.get("source"), dict) else {}
            model = str(source.get("model") or "").strip() or latest_model
            provider = str(source.get("provider") or "").strip() or latest_provider
        else:
            continue

        # A forked child clones the parent's completed prefix into its own
        # durable log; the parent already owns those events. Fail closed: with
        # a declared seed boundary, an event whose seq is unreadable cannot
        # prove it is not inherited, so it is skipped too — and a seeded log
        # whose boundary is missing skips everything instead of double-billing
        # the parent's whole prefix.
        seq = _to_int(event.get("seq"))
        if seed_unprovable or (seed_length and (seq is None or seq < seed_length)):
            continue

        turn = _to_int(data.get("turn"))
        step = _to_int(data.get("step"))
        if turn is None or step is None:
            continue

        input_tokens = _to_int(usage.get("inputTokens"))
        output_tokens = _to_int(usage.get("outputTokens"))
        if input_tokens is None or output_tokens is None:
            continue
        cache_read = _to_int(usage.get("cacheReadTokens"))
        cache_write = _to_int(usage.get("cacheWriteTokens"))
        # Optional cache fields may be absent (meaning zero); present but
        # invalid — negative or non-numeric — makes the whole sample unusable.
        if (cache_read is None and usage.get("cacheReadTokens") is not None) or (
            cache_write is None and usage.get("cacheWriteTokens") is not None
        ):
            continue
        cache_read = cache_read or 0
        cache_write = cache_write or 0
        if input_tokens + output_tokens + cache_read + cache_write == 0:
            continue

        # A sample without a usable event time would vanish from date-ranged
        # views while still counting in unfiltered totals; skip it (Pi
        # precedent) instead of anchoring anything at the epoch.
        timestamp_ms = _to_int(event.get("time"))
        if timestamp_ms is None:
            continue

        sample = {
            "turn": turn,
            "step": step,
            "timestamp_ms": timestamp_ms,
            "model": model or "unknown",
            "provider": provider,
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
            # dsh outputTokens already includes reasoningTokens; reporting it
            # separately here would count those tokens twice downstream.
            "reasoning": 0,
        }
        samples[(turn, step)] = sample

    return list(samples.values())


def dsh_entry_id(session_id: Any, turn: Any, step: Any) -> str:
    """Stable usage-entry identity, unchanged when a chunk row is replaced."""
    return f"dsh:{session_id}:{turn}:{step}"
