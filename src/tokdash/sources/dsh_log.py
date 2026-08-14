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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zstandard import ZstdDecompressor

# Bump when framing/extraction semantics change; included in the persistent
# session-store signature so stored rows reparse instead of going stale.
DSH_DECODER_VERSION = 1
# Bump when usage-accounting rules change (fold keys, seed boundary, zero skip).
DSH_ACCOUNTING_VERSION = 1

# Version 0 is a developer-preview format; anything else is unsupported, not
# corrupt — the file is skipped, never treated as empty.
SUPPORTED_SESSION_FORMAT_VERSION = 0


@dataclass(frozen=True)
class DSHDecodedSession:
    """One decoded dsh log: the session header, the event rows, or why not."""

    header: Optional[Dict[str, Any]] = None
    events: Tuple[Dict[str, Any], ...] = ()
    skip_reason: Optional[str] = None


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
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return DSHDecodedSession(skip_reason="read-error")

    if str(path).endswith(".jsonl.zstd"):
        # Not one plain zstd stream: dsh concatenates a separately framed and
        # checksummed zstd frame per durable append batch.
        try:
            raw = ZstdDecompressor().stream_reader(raw, read_across_frames=True).read()
        except Exception:
            return DSHDecodedSession(skip_reason="decode-error")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return DSHDecodedSession(skip_reason="decode-error")

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
                return DSHDecodedSession(skip_reason="invalid-header")
            continue
        if not isinstance(row, dict):
            if header is None:
                return DSHDecodedSession(skip_reason="invalid-header")
            continue
        if header is None:
            if row.get("type") != "session":
                return DSHDecodedSession(skip_reason="missing-header")
            if row.get("version") != SUPPORTED_SESSION_FORMAT_VERSION:
                return DSHDecodedSession(skip_reason="unsupported-version")
            header = row
            continue
        events.append(row)

    if header is None:
        return DSHDecodedSession(skip_reason="missing-header")
    return DSHDecodedSession(header=header, events=tuple(events))


def _to_int(value: Any) -> Optional[int]:
    """An explicit non-negative integer, or None when absent or invalid."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def fold_dsh_usage_samples(
    header: Dict[str, Any],
    events: Tuple[Dict[str, Any], ...],
) -> List[Dict[str, Any]]:
    """Fold provider usage events into one sample per ``(turn, step)``.

    Two event shapes report usage: an early ``assistant/chunk`` whose
    ``chunk.type == "usage"``, and the finalized ``assistant/message`` carrying
    ``data.usage``. Samples for one ``(turn, step)`` are adjacent in a legal
    log, so this is a replace-not-add fold against the most recent sample: a
    final message replaces its earlier chunk instead of double-counting it,
    and an early chunk with no final message remains counted.

    Events before the fork boundary (``seq < header.seedLength``) are inherited
    from the parent log and skipped; ``parentSession`` alone skips nothing, and
    with a declared boundary an event whose ``seq`` is unreadable is skipped
    too (fail closed). All-zero samples, samples with any negative bucket, and
    samples without an explicit numeric input/output pair or a usable event
    time are dropped — an absent usage object is not zero usage.
    """
    seed_length = _to_int(header.get("seedLength")) or 0
    samples: List[Dict[str, Any]] = []
    last_key: Optional[Tuple[int, int]] = None
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
        # prove it is not inherited, so it is skipped too.
        seq = _to_int(event.get("seq"))
        if seed_length and (seq is None or seq < seed_length):
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
        key = (turn, step)
        if last_key == key and samples:
            samples[-1] = sample
        else:
            samples.append(sample)
            last_key = key

    return samples


def dsh_entry_id(session_id: Any, turn: Any, step: Any) -> str:
    """Stable usage-entry identity, unchanged when a chunk row is replaced."""
    return f"dsh:{session_id}:{turn}:{step}"
