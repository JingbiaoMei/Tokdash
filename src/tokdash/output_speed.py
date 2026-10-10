"""Output-throughput measurement contract shared by usage storage and session detail.

Why this module exists
----------------------
Tokdash has always stored how many tokens a call produced, but never how long the
model spent producing them, so "tokens per second" was not answerable from the
dashboard. Several sources do persist a per-call duration, but each spells it
differently and pairs it with its tokens differently. If the usage store and the
session drill-down each implemented that pairing, the two surfaces would drift
into reporting different numbers for the same call. Every reader therefore goes
through the helpers here, and the only persisted representation of a measurement
is the additive triple (``speed_tokens``, ``speed_ms``, ``speed_calls``).

The contract
------------
The displayed rate is a ratio of sums, never a mean of per-call rates::

    output_tok_per_s = 1000 * SUM(speed_tokens) / SUM(speed_ms)

so a call that ran 40 ms and one that ran 4 s contribute in proportion to the time
they actually took. Rates are rounded only at the moment they are rendered.

Per-call rates are still workload-dependent (output size, reasoning effort,
provider route), so a rate is always shown next to its measured-call count and its
coverage, and an empty bucket is null rather than zero.

Two labels travel with every measurement, because a number without them is not
comparable to anything:

``measurement_kind``
    What the duration actually covers. ``server_decode`` is a provider-reported
    decode window; ``post_first_token`` is call duration minus time-to-first-token;
    ``request_window`` is a whole request bracket that includes prefill and
    transport. Only ``server_decode`` may be described as decode speed.
``token_basis``
    Whether reasoning tokens are inside the numerator. Reasoning is never added on
    top of a bucket that may already contain it, so a source that does not
    separate thinking gets ``output_reasoning_unspecified`` rather than a guess.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Optional

# Bump when the meaning of a stored measurement changes, so caches and stored rows
# that predate the change cannot be read as if they agreed with it.
SPEED_CONTRACT_VERSION = 3

KIND_SERVER_DECODE = "server_decode"
KIND_POST_FIRST_TOKEN = "post_first_token"
KIND_REQUEST_WINDOW = "request_window"
KIND_RESPONSE_WINDOW = "response_window"

MEASUREMENT_KINDS = (
    KIND_SERVER_DECODE,
    KIND_POST_FIRST_TOKEN,
    KIND_REQUEST_WINDOW,
    KIND_RESPONSE_WINDOW,
)

BASIS_INCLUDING = "output_including_reasoning"
BASIS_EXCLUDING = "output_excluding_reasoning"
BASIS_UNSPECIFIED = "output_reasoning_unspecified"

TOKEN_BASES = (BASIS_INCLUDING, BASIS_EXCLUDING, BASIS_UNSPECIFIED)

STATUS_MEASURED = "measured"
STATUS_MISSING_TIMING = "missing_timing"
STATUS_AMBIGUOUS_PAIR = "ambiguous_pair"
STATUS_INVALID_TIMING = "invalid_timing"
STATUS_INCOMPLETE = "incomplete"
STATUS_RETRY_CONTAMINATED = "retry_contaminated"
STATUS_UNKNOWN = "unknown"

STATUSES = (
    STATUS_MEASURED,
    STATUS_MISSING_TIMING,
    STATUS_AMBIGUOUS_PAIR,
    STATUS_INVALID_TIMING,
    STATUS_INCOMPLETE,
    STATUS_RETRY_CONTAMINATED,
    STATUS_UNKNOWN,
)

# Reasons that count as an explicit exclusion rather than plain absence.
EXCLUSION_STATUSES = (
    STATUS_AMBIGUOUS_PAIR,
    STATUS_INVALID_TIMING,
    STATUS_INCOMPLETE,
    STATUS_RETRY_CONTAMINATED,
)


#: A speed statistic this module produced itself, already in storage shape.
#:
#: Immutability is the point rather than a mannerism: it lets one verdict be
#: shared by every row that carries it (``resolve`` hands out a single dict per
#: status, and the store hangs it on each row without copying), and it is what
#: makes the type usable as a trust boundary. ``sanitize_stored`` exists to make
#: numbers the parser wrote itself safe to store -- coerce the types, refuse NaN,
#: and never let a full statistic sit beside an exclusion status. Re-running it on
#: a dict minted here is a dataclass round-trip per row per sync, which on the
#: audited corpus cost more than the association that produced the number. A
#: parser that hands in a plain dict is still treated as untrusted input.
class CanonicalSpeed(dict):
    """Immutable six-field storage statistic; see the note above the class."""

    __slots__ = ()

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("canonical output-speed statistics are immutable")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("canonical output-speed statistics are immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("canonical output-speed statistics are immutable")

    def setdefault(self, key: Any, default: Any = None) -> Any:
        raise TypeError("canonical output-speed statistics are immutable")

    def pop(self, *args: Any) -> Any:
        raise TypeError("canonical output-speed statistics are immutable")

    def popitem(self) -> Any:
        raise TypeError("canonical output-speed statistics are immutable")

    def clear(self) -> None:
        raise TypeError("canonical output-speed statistics are immutable")


@dataclass(frozen=True)
class SpeedMeasurement:
    """One matched call: the tokens it produced and the time it took."""

    speed_tokens: int
    speed_ms: float
    speed_calls: int
    measurement_kind: str
    token_basis: str
    status: str = STATUS_MEASURED

    def as_storage_dict(self) -> dict[str, Any]:
        return {
            "speed_tokens": self.speed_tokens,
            "speed_ms": self.speed_ms,
            "speed_calls": self.speed_calls,
            "speed_kind": self.measurement_kind,
            "speed_token_basis": self.token_basis,
            "speed_status": self.status,
        }


def unmeasured(status: str = STATUS_UNKNOWN) -> dict[str, Any]:
    """The storage shape for a call that carries no usable duration.

    Zeroes with a status, never a zero *rate*: an unmeasured row must stay
    visibly unmeasured so it cannot dilute an aggregate or read as "fast".

    Canonical, and therefore shared freely: one instance serves every row the
    association could not time, which on a corpus of them is most of them.
    """
    return CanonicalSpeed({
        "speed_tokens": 0,
        "speed_ms": 0.0,
        "speed_calls": 0,
        "speed_kind": "",
        "speed_token_basis": "",
        "speed_status": status if status in STATUSES else STATUS_UNKNOWN,
    })


def finite_positive_ms(value: Any) -> Optional[float]:
    """Return ``value`` as a positive finite float, else None.

    Accepts a present zero as "zero" rather than "missing" by returning it for the
    caller to judge; this function only rejects what cannot be a duration. Callers
    that must distinguish 0 from absent check ``value is None`` first.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number <= 0:
        return None
    return number


#: Kimi only. A ceiling on what a reported decode window may imply, in output
#: tokens per second. This is not a universal speed cap and is applied to no other
#: source: it exists because the CLI's own stream timer collapses on a real slice
#: of its steps. Measured on 152 local wire logs (13,188 usable step.end rows) on
#: 2026-10-05: 248 of them report a server-decode window of 1-5 ms for completions
#: of 130-2,000+ output tokens, always alongside a first-token latency of seconds
#: (the shape is llmStreamDurationMs collapsing while llmServerFirstTokenMs is
#: 6-113 s), which is the signature of a buffered response being consumed rather
#: than decoded. The legitimate distribution tops out around 500 tok/s with p90 at
#: 95, so 1,000 sits well clear of any real call while still naming the artifact.
#: Excluded rows stay counted as usage and are reported as exclusions in the UI;
#: they are never silently averaged away. Dropping them moves the local aggregate
#: from 45.0 to 44.8 tok/s, and matters where it always does: a one-call hour.
KIMI_MAX_PLAUSIBLE_TOK_PER_S = 1000.0

# An omp assistant record says how the call ended. These endings are a call
# that did not finish normally, so its duration is not the window that
# produced the tokens it did write -- and reading it as one would let a
# failed attempt inflate a rate. The source states this outright, so the
# reader states it back rather than guessing from a suspicious speed.
OMP_INCOMPLETE_STOP_REASONS = frozenset({"error", "aborted"})


def measured(
    *,
    tokens: Any,
    duration_ms: Any,
    measurement_kind: str,
    token_basis: str,
    max_tok_per_s: Optional[float] = None,
) -> dict[str, Any]:
    """Build a measured row, or an unmeasured row when the inputs do not hold up.

    Rejection is silent-but-typed: a non-finite, zero or reversed duration, or a
    non-positive numerator, becomes a status the UI can name. It never becomes a
    rate, and it never discards the usage row it belongs to.

    ``max_tok_per_s`` is a per-source instrument-plausibility ceiling, left to None
    by every source that has not evidenced a reason to set one. It rejects the
    measurement, not the call.
    """
    if measurement_kind not in MEASUREMENT_KINDS or token_basis not in TOKEN_BASES:
        raise ValueError("unknown measurement kind or token basis")
    ms = finite_positive_ms(duration_ms)
    try:
        token_count = int(tokens)
    except (TypeError, ValueError):
        token_count = 0
    if ms is None:
        return unmeasured(STATUS_INVALID_TIMING)
    if token_count <= 0:
        # A zero-output call has no numerator. It is not excluded, merely without
        # a rate, so it stays in the unmeasured population.
        return unmeasured(STATUS_MISSING_TIMING)
    if max_tok_per_s is not None and (1000.0 * token_count / ms) > max_tok_per_s:
        return unmeasured(STATUS_INVALID_TIMING)
    # The same six fields SpeedMeasurement.as_storage_dict writes, assembled here
    # because this runs once per measured call in every reader: on a source that
    # logs one call per turn, building the row costs more than reading it did.
    return CanonicalSpeed({
        "speed_tokens": token_count,
        "speed_ms": ms,
        "speed_calls": 1,
        "speed_kind": measurement_kind,
        "speed_token_basis": token_basis,
        "speed_status": STATUS_MEASURED,
    })


def sanitize_stored(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalise whatever a parser produced into the canonical storage fields.

    Paired validity is enforced here rather than in SQLite, because SQLite happily
    stores NaN and Infinity, and a single NaN duration would poison SUM() for
    every row in the group.
    """
    if isinstance(entry, CanonicalSpeed):
        # Minted here, so every rule below has already run on it.
        return entry
    calls = entry.get("speed_calls")
    tokens = entry.get("speed_tokens")
    ms = entry.get("speed_ms")
    kind = str(entry.get("speed_kind") or "")
    basis = str(entry.get("speed_token_basis") or "")
    status = str(entry.get("speed_status") or STATUS_UNKNOWN)

    try:
        calls_i = int(calls or 0)
    except (TypeError, ValueError):
        calls_i = 0
    try:
        tokens_i = int(tokens or 0)
    except (TypeError, ValueError):
        tokens_i = 0
    ms_f = finite_positive_ms(ms)

    # An explicit reason wins over the numbers beside it. A row that carries a
    # full statistic *and* says it could not be paired is a bug upstream, and the
    # safe reading of that disagreement is the reason: promoting it to measured
    # because the arithmetic looked fine would put an unpaired duration on the
    # board, which is the exact failure this whole module exists to prevent.
    if status in EXCLUSION_STATUSES:
        return unmeasured(status)
    if status in STATUSES and status != STATUS_MEASURED:
        return unmeasured(status)
    if calls_i > 0 and ms_f is not None and tokens_i > 0 and kind and basis:
        return CanonicalSpeed(SpeedMeasurement(
            speed_tokens=tokens_i,
            speed_ms=ms_f,
            speed_calls=1,
            measurement_kind=kind,
            token_basis=basis,
        ).as_storage_dict())
    return unmeasured(STATUS_UNKNOWN)


def rate_from_totals(speed_tokens: Any, speed_ms: Any) -> Optional[float]:
    """Derived throughput for already-summed statistics, or None when empty."""
    try:
        tokens = float(speed_tokens or 0)
        ms = float(speed_ms or 0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(tokens) or not math.isfinite(ms):
        return None
    if tokens <= 0 or ms <= 0:
        return None
    return round(1000.0 * tokens / ms, 1)


def merge_totals(*pairs: tuple[Any, Any]) -> Optional[float]:
    """Rate for the sum of several (tokens, ms) pairs.

    Used for a rendered turn that merged several calls, and for the six-hour rows
    that must not average the hourly rates beneath them.
    """
    tokens = 0.0
    ms = 0.0
    for t, m in pairs:
        try:
            tokens += float(t or 0)
            ms += float(m or 0)
        except (TypeError, ValueError):
            continue
    return rate_from_totals(tokens, ms)


def coverage(measured_calls: Any, eligible_calls: Any) -> Optional[float]:
    """Share of eligible calls that carry a measurement.

    Returns None whenever the denominator is unknown. An unknown denominator stays
    unknown rather than becoming 0% or 100%, which is how a partially measured
    source could otherwise be read as a fully measured one.
    """
    try:
        got = int(measured_calls or 0)
        total = int(eligible_calls or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return round(min(1.0, got / total), 4)


def public_turn_speed(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The ``output_speed`` object for one measured turn, or None."""
    calls = int(row.get("speed_calls") or 0)
    if calls <= 0:
        return None
    tokens = int(row.get("speed_tokens") or 0)
    ms = row.get("speed_ms")
    rate = rate_from_totals(tokens, ms)
    if rate is None:
        return None
    return {
        "output_tok_per_s": rate,
        "speed_tokens": tokens,
        "speed_ms": round(float(ms), 3),
        "speed_calls": calls,
        "measurement_kind": str(row.get("speed_kind") or ""),
        "token_basis": str(row.get("speed_token_basis") or ""),
    }


# --------------------------------------------------------------------------
# Shared source readers
#
# Each reader turns one source file into {usage-entry-identity: measurement}.
# Both the usage-store parser and the session drill-down call them, so the two
# surfaces cannot disagree about which call took how long. They return
# unmeasured dicts rather than nothing when a call's timing is absent, because
# "absent" has to stay countable in the coverage numbers.
# --------------------------------------------------------------------------

#: How far past the end of a step a usage row may be stamped and still be that
#: step's call. Kimi writes the step.end and the usage.record a hair apart, and a
#: hair is enough to cross a millisecond: measured on 152 local wire logs
#: (2026-10-07, 13,161 brackets that carry usage), 786 of them -- 6 in every
#: thousand calls -- hold a row stamped 1 to 8 ms after the bracket closed, 773 of
#: them at exactly 1 ms, and no row anywhere before a bracket opened. So the clock
#: runs one way here, and the window has to be open that far. Half a second stays
#: four orders of magnitude below the gap between two calls of one agent, which is
#: the only thing it could otherwise be confused with, and it still covers a timer
#: that ticks in milliseconds rather than microseconds.
_ROW_CLOCK_SLACK_MS = 500

#: Marks a claim that a second contender reached. A claim slot otherwise holds one
#: bracket index or one row identity, so anything else in it means "more than one".
_CONTESTED = object()

#: Rows a step bracket may span before the plain time-ordered scan stops being the
#: cheap way to find a pair. Real logs pair one bracket with one row, so this is a
#: ceiling on the pathological case rather than a tuned value: past it the pairing
#: switches to a usage-bucketed index, which costs more per call but stays linear
#: however deeply a log nests its steps.
_WIDE_BRACKET_ROWS = 32

def _usage_bucket(usage: dict) -> tuple:
    """A cheap stand-in for "these two usage objects are equal".

    Equal dicts agree on every counter they carry, so two rows that could ever
    match a bracket necessarily land in the same bucket; unequal ones may share a
    bucket, and the full comparison the caller makes still settles those.

    Only the wide-bracket path of ``KimiStepAssociation.resolve`` uses it. Pairing
    rows by timestamp alone would mean a whole-dict comparison per row a bracket
    spans, which is fine at one row per bracket and quadratic in a log that nests its
    steps; bucketing by the counters first trims that to the rows that could pair.

    Values that cannot be hashed are replaced by their type name, which only ever
    widens a bucket, so the pairing stays conservative rather than wrong.
    """
    bucket = (len(usage),)
    for name in ("output", "inputOther", "inputCacheRead", "inputCacheCreation"):
        value = usage.get(name)
        bucket += (
            value
            if value is None or isinstance(value, (bool, int, float, str))
            else f"<{type(value).__name__}>",
        )
    return bucket


class KimiStepAssociation:
    """One Kimi text's step brackets, paired with the rows those brackets measured.

    Kimi writes the counted ``usage.record`` and the ``step.end`` that closes it as
    two separate records, so no row can be judged until the whole text has been
    read. Both readers already loop over the records for their own reasons -- the
    usage parser to bill tokens, the session drill-down to print turns -- so each
    feeds this object as it goes and asks for the verdict at the end. That is what
    keeps a wire log at one JSON parse per line rather than two, which on a corpus
    of them is the difference between the association being free and being the most
    expensive part of reading the file.

    The pairing itself is structural, and the rules are in ``resolve``: scope to one
    ``agentId``, take the ``step.begin`` -> ``step.end`` bracket containing the row's
    own timestamp, and require the two records to carry identical usage objects.
    Never nearest-timestamp and never token-equality alone; each on its own would
    happily pair a retried call with its neighbour. The bracket stays open a little
    past its own close, because the row is written on the far side of it: see
    ``_ROW_CLOCK_SLACK_MS``.

    ``claims_a_row_this_text_missed`` exists for the one caller that is not reading a
    whole file. The store tail-appends, and a duration arriving for a row it already
    stored means the slice was too short: the bracket and its row were split by the
    append boundary. A bracket carrying a usage object is a claim that one specific
    call was measured, so a bracket whose call never appeared is evidence about the
    slice, not about the log -- and the caller can settle it by reading the file
    whole instead of storing a row that is missing a duration it cannot recover.
    """

    def __init__(self) -> None:
        # Per agent: closed brackets as (begin_ms, end_ms, step.end event). The
        # bracket search must never cross an agent boundary, because agents in one
        # session genuinely run concurrently.
        self._brackets: dict[str, list[tuple[int, int, dict]]] = {}
        # A stack per agent, not one slot: steps nest, and a single slot would let an
        # inner step.end close the outer step and leave the row with one bracket it
        # never had. A nested pair stays two brackets, which is the case the
        # ambiguity rule in resolve() exists to catch.
        self._open: dict[str, list[tuple[int, dict]]] = {}
        # Rows that still need a duration, under the agent whose brackets can supply
        # one, as (timestamp_ms, identity, usage), and deduped by identity as they
        # arrive: the same row twice -- a repeated line, or the same call seen by
        # both readers -- keeps its first registration and one verdict.
        self._rows_by_agent: dict[str, list[tuple[int, str, dict]]] = {}
        self._usage_of: dict[str, dict] = {}
        self._resolved = False
        self._unpaired_brackets = 0
        # Rows no bracket in THIS text could have timed, which is the append
        # boundary seen from the other side: see ``claims_a_bracket_this_text_missed``.
        self._rows_no_bracket_possible = 0
        # Earliest record of any kind seen per agent -- a step opening, a step
        # closing, or a counted row. This is the bound the row-side signal needs:
        # records reach the log in clock order, so everything this text did not
        # see happened no later than the earliest thing it did. See
        # ``claims_a_bracket_this_text_missed``.
        self._earliest_seen: dict[str, int] = {}

    def observe(self, obj: dict) -> None:
        """Note the step brackets in one already-parsed record."""
        if not isinstance(obj, dict) or obj.get("type") != "context.append_loop_event":
            return
        event = obj.get("event")
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type != "step.begin" and event_type != "step.end":
            return
        timestamp = obj.get("time")
        if not isinstance(timestamp, (int, float)):
            return
        agent = str(obj.get("agentId") or "")
        seen = self._earliest_seen.get(agent)
        if seen is None or int(timestamp) < seen:
            self._earliest_seen[agent] = int(timestamp)
        if event_type == "step.begin":
            # Not optional: without it no bracket ever opens, and every usage row
            # in the file then reads as having no timing at all.
            self._open.setdefault(agent, []).append((int(timestamp), event))
            return
        stack = self._open.get(agent)
        if not stack:
            # A step.end with no step.begin in this text. There is nothing to pair
            # it with, and guessing where the step started would be the whole
            # invention the association exists to avoid.
            if isinstance(event.get("usage"), dict):
                self._unpaired_brackets += 1
            return
        opened = stack.pop()
        self._brackets.setdefault(agent, []).append((opened[0], int(timestamp), event))

    def register_usage(self, *, agent: Any, timestamp_ms: int, usage: dict, identity: str) -> None:
        """Add a counted row under the identity its own reader dedups and stores it by.

        The caller supplies the key rather than having it recomputed here: the usage
        parser and the session parser already build it for every row, and rebuilding
        it a second time would be the most expensive part of the association.
        """
        if not identity or not isinstance(usage, dict) or identity in self._usage_of:
            return
        self._usage_of[identity] = usage
        who = str(agent or "")
        self._rows_by_agent.setdefault(who, []).append(
            (int(timestamp_ms), identity, usage)
        )
        seen = self._earliest_seen.get(who)
        if seen is None or int(timestamp_ms) < seen:
            self._earliest_seen[who] = int(timestamp_ms)

    @property
    def unpaired_brackets(self) -> int:
        """Brackets that claim to have measured a call this text never showed."""
        return self._unpaired_brackets

    @property
    def claims_a_row_this_text_missed(self) -> bool:
        """True once ``resolve`` has met a bracket whose row is not in this text.

        Only meaningful after ``resolve``, which is where the brackets that did find
        their row are known. Deliberately it does not ask whether this text counted
        any rows at all, because the one caller that acts on it -- the store's tail
        append, via ``parsing_tail_slice`` -- most often holds exactly that: the
        usage row went in on the previous append and only its step.end arrived now,
        so the slice has a bracket and no rows. Insisting on a row here would strand
        that call unmeasured for good, since the file's next change is what would
        have mended it.

        The cost of trusting the signal is measured rather than assumed: across 152
        local wire logs, 13,161 brackets that carry usage, a whole-file read leaves
        zero of them without a row (see ``_ROW_CLOCK_SLACK_MS`` for the last of
        them). So on a real log an unmatched bracket in a slice is a split pair, and
        a whole-file reparse is both the right answer and a rare one.
        """
        return self._resolved and self._unpaired_brackets > 0

    @property
    def claims_a_bracket_this_text_missed(self) -> bool:
        """True once ``resolve`` has met a row no bracket in this text could time.

        The mirror of ``claims_a_row_this_text_missed``, for the append that lands
        the other way round: the ``step.end`` went in on the previous append and
        only its ``usage.record`` arrived now, so the slice holds a row and no
        bracket. Nothing in the pairing loop can spot that -- a row with no
        bracket is simply unmeasured, and the loop counts the brackets it could
        not place, not the rows nobody placed -- so the row would settle as
        ``missing_timing`` and stay there, because a tail append never revisits a
        byte it has already consumed.

        The test is narrow on purpose, because a signal that fired on every
        unmeasured row would cost a whole-file reparse on every append. It is
        ``resolve``'s horizon: records reach the log in clock order, so a bracket
        hidden from this text closed no later than this text's earliest record of
        that agent, and it only ever times a row stamped within the row clock's
        slack of that close. An unmeasured row past that horizon could only have
        been timed by a bracket this text does hold, so its silence is the log's
        own missing timing and a reparse cannot mend it.

        The horizon is the earliest record of any kind, not the earliest
        ``step.begin``. A slice can open mid-turn and still hold the previous
        turn's row: step A closes before the boundary, and the tail is B opening,
        A's late row, then B's whole rest. A row-side bound on openings lets that
        row through -- it is stamped after B began, so an opening of B is not
        evidence of anything -- and it settles unmeasured while a whole-file read
        measures it. The close, which is what the row's stamp is actually allowed
        to sit beside, is the bound that catches it.
        """
        return self._resolved and self._rows_no_bracket_possible > 0

    def resolve(self) -> dict[str, dict[str, Any]]:
        """``identity -> measurement`` for every row this text registered.

        The association is one-to-one, and that has to hold in BOTH directions.
        Deciding row by row only ever asks "does this row have exactly one bracket?",
        which lets one timing measure several calls: two rows with identical usage
        inside one nested step both answer yes, and each then bills the same decode
        window. So the claim is recorded first and handed out afterwards, and a
        bracket reached from two rows is contested rather than shared -- exactly as a
        row facing two identical brackets cannot be assigned. Contested rows stay
        unmeasured and count as ``ambiguous_pair``, so the report shows the exclusion
        instead of hiding it.

        Brackets are the driver, and each one looks up the rows it spans in the
        time-ordered rows of its own agent. One bracket per call and one row per call
        is what these logs actually contain -- every bracket in the audited corpus
        spanned exactly one row -- so the span is a slice found by two bisections and
        the pass costs about one usage comparison per call. A bracket that does span
        a crowd, the open-longer-than-it-looked case, is matched through the coarser
        usage-bucketed index instead, so a log that nests steps two hundred deep
        cannot turn this into a whole-dict comparison per bracket per row.
        """
        out: dict[str, dict[str, Any]] = {}
        # One dict per status, shared by every row that status describes rather than
        # each carrying its own copy. Both readers copy a verdict before hanging it on
        # a row, so nothing can write through it.
        missing = unmeasured(STATUS_MISSING_TIMING)
        contested_row = unmeasured(STATUS_AMBIGUOUS_PAIR)
        unpaired = 0
        # Mirror of ``unpaired``, from the row's side: see the property of that name.
        rows_no_bracket = 0
        for agent, rows in self._rows_by_agent.items():
            brackets = self._brackets.get(agent)
            # The horizon past which a row cannot belong to a record this text never
            # read. Records reach the log in clock order, so anything hidden from this
            # text closed no later than its earliest record of this agent, and a
            # bracket only times rows stamped within the row clock's slack of its own
            # close. A row that is unmeasured here yet still inside that horizon is
            # therefore a row whose bracket may be bytes this read never saw -- the
            # split the row-side signal exists for. A row past the horizon could only
            # have been timed by a bracket this text does contain, so its silence is
            # the log's own missing timing and reparsing cannot mend it.
            # Include the upper boundary, just as bisect_right does when matching.
            horizon = self._earliest_seen.get(agent)
            horizon = None if horizon is None else horizon + _ROW_CLOCK_SLACK_MS
            if not brackets:
                # Nothing ever closed a step for this agent, so none of its rows can
                # be timed. Countably missing, not silently rate-less.
                for _stamp, identity, _usage in rows:
                    out[identity] = missing
                    if horizon is None or _stamp <= horizon:
                        # Not untimed by the log's own account, but unreadably timed:
                        # its bracket is bytes this read never saw.
                        rows_no_bracket += 1
                continue
            rows.sort()
            stamps = [row[0] for row in rows]
            # Claims are per agent: a row can only be claimed by a bracket of its own
            # agent, and the bracket index is agent-local. A slot holds its single
            # claimer, or the sentinel once a second one reaches it -- a list per
            # claim is the dearest thing in this loop and one a well-formed log
            # fills exactly once.
            bracket_claims: dict[int, Any] = {}
            row_claims: dict[str, Any] = {}
            # Built on demand, only for a log wide enough to need it.
            by_usage: Optional[dict[tuple, tuple[list[int], list[tuple]]]] = None
            for index, (began, ended, event) in enumerate(brackets):
                event_usage = event.get("usage")
                if not isinstance(event_usage, dict):
                    # A step that recorded no usage claims no row, so there is no
                    # pair to find and nothing to count against the text.
                    continue
                low = bisect_left(stamps, began)
                high = bisect_right(stamps, ended + _ROW_CLOCK_SLACK_MS, low, len(stamps))
                candidates = rows
                if high - low > _WIDE_BRACKET_ROWS:
                    if by_usage is None:
                        groups: dict[tuple, list[tuple]] = {}
                        for row in rows:
                            groups.setdefault(_usage_bucket(row[2]), []).append(row)
                        by_usage = {
                            bucket: ([row[0] for row in group], group)
                            for bucket, group in groups.items()
                        }
                    spanned = by_usage.get(_usage_bucket(event_usage))
                    if spanned is None:
                        # No row of this agent carries these counters: the call this
                        # bracket timed is not in the text at all.
                        unpaired += 1
                        continue
                    bucket_stamps, candidates = spanned
                    low = bisect_left(bucket_stamps, began)
                    high = bisect_right(
                        bucket_stamps, ended + _ROW_CLOCK_SLACK_MS, low, len(bucket_stamps)
                    )
                claimed = False
                for position in range(low, high):
                    candidate = candidates[position]
                    # Identical usage objects on both records: the pair's whole
                    # basis for being called the same call.
                    if candidate[2] != event_usage:
                        continue
                    claimed = True
                    identity = candidate[1]
                    # setdefault hands back what it stored when the key was absent, so
                    # "is not" here is the same as "this row had a claimer already".
                    if row_claims.setdefault(identity, index) is not index:
                        row_claims[identity] = _CONTESTED
                    if bracket_claims.setdefault(index, identity) is not identity:
                        bracket_claims[index] = _CONTESTED
                if not claimed:
                    # A bracket that timed a call this text never showed is evidence
                    # about the read, not about the log; see
                    # ``claims_a_row_this_text_missed``.
                    unpaired += 1

            for _stamp, identity, usage in rows:
                mine = row_claims.get(identity)
                if mine is None:
                    # No bracket of this agent spans the row, or none of the ones that
                    # do carry its usage: the timing is not there, which is countable
                    # and is not the same claim as a call that measured zero.
                    out[identity] = missing
                    if horizon is None or _stamp <= horizon:
                        # And it is not even a row this text could have timed: the
                        # bracket that closed it went in before the slice did.
                        rows_no_bracket += 1
                elif mine is _CONTESTED or bracket_claims[mine] is _CONTESTED:
                    # Contested in either direction: two brackets that both claim this
                    # row, or one bracket that two rows both answer to. No rule here
                    # could pick correctly, so nothing is measured and the exclusion is
                    # reported instead of hidden.
                    out[identity] = contested_row
                else:
                    out[identity] = measured(
                        tokens=usage.get("output"),
                        duration_ms=brackets[mine][2].get("llmServerDecodeMs"),
                        measurement_kind=KIND_SERVER_DECODE,
                        token_basis=BASIS_UNSPECIFIED,
                        max_tok_per_s=KIMI_MAX_PLAUSIBLE_TOK_PER_S,
                    )

        if not self._resolved:
            # Brackets of an agent that billed no rows at all: their calls cannot be
            # in a text that recorded none of them, which is the same evidence about
            # the read as a bracket whose row is simply absent. Counted once, because
            # resolve may be asked again after the first verdict.
            unpaired += sum(
                1
                for agent, brackets in self._brackets.items()
                if agent not in self._rows_by_agent
                for _began, _ended, event in brackets
                if isinstance(event.get("usage"), dict)
            )
            self._unpaired_brackets += unpaired
            self._rows_no_bracket_possible += rows_no_bracket
        self._resolved = True
        return out


def kimi_step_timings(path_str: str) -> dict[str, dict[str, Any]]:
    """Associate each Kimi Code ``usage.record`` in a file with its own ``step.end``.

    Whole-file convenience reader over :class:`KimiStepAssociation`, for callers that
    have nothing else to do with the file. The usage parser and the session parser
    feed an association from the loop they already run instead, so a wire log is
    parsed once rather than twice.

    The returned key is the same SHA-1 the usage parser and the session parser
    already use as their dedup identity, so a timing lands on exactly the row the
    tokens were billed under.
    """
    from pathlib import Path

    with Path(path_str).open("r", encoding="utf-8") as handle:
        return _kimi_step_timings(handle, path_str)


def kimi_step_timings_from(handle: Any, path_str: str) -> dict[str, dict[str, Any]]:
    """As :func:`kimi_step_timings`, from a handle the caller already has.

    The handle comes back positioned at the start, so a caller that has to open the
    file anyway can read the association from the same open. The open stays with the
    caller on purpose: a lock has to fail the parse that is about to read the same
    path, not quietly yield an empty timing map that the session parser's lru_cache
    would memoize for a file whose lock was transient.
    """
    timings = _kimi_step_timings(handle, path_str)
    handle.seek(0)
    return timings


def _kimi_step_timings(lines: Any, path_str: str) -> dict[str, dict[str, Any]]:
    association = KimiStepAssociation()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Only three record kinds carry what we need. Skipping the JSON parse for
        # the rest matters: a large wire log is mostly unrelated events.
        if (
            '"usage.record"' not in line
            and '"step.end"' not in line
            and '"step.begin"' not in line
        ):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        association.observe(obj)
        if obj.get("type") == "usage.record":
            usage = obj.get("usage")
            timestamp = obj.get("time")
            if isinstance(usage, dict) and isinstance(timestamp, (int, float)):
                association.register_usage(
                    agent=obj.get("agentId"),
                    timestamp_ms=int(timestamp),
                    usage=usage,
                    identity=kimi_row_identity(
                        path_str, int(timestamp), obj.get("model"), usage
                    ),
                )
    return association.resolve()


def kimi_usage_record_key(path_str: str, timestamp_ms: int, model: Any, usage: dict) -> str:
    """The one identity a Kimi ``usage.record`` row is stored and read under.

    Three places need this: the usage parser, the session drill-down, and the
    timing reader that hands a duration to whichever of them asks. Each used to
    own a copy of the SHA-1, which meant a duration could be attached to the row
    its tokens were *not* billed under the moment any copy drifted. One function,
    three callers.

    The path is in the payload on purpose: identical usage rows in sibling agent
    files stay separately countable after the cross-file merge.
    """
    payload = json.dumps(
        [
            path_str,
            int(timestamp_ms),
            model,
            usage.get("inputOther"),
            usage.get("output"),
            usage.get("inputCacheRead"),
            usage.get("inputCacheCreation"),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def kimi_row_identity(path_str: str, timestamp_ms: int, model_raw: Any, usage: dict) -> Optional[str]:
    """``kimi_usage_record_key`` for a row that still carries its wire model name."""
    from .sources.coding_tools import KimiParser

    return kimi_usage_record_key(
        path_str, timestamp_ms, KimiParser._model_for_wire_name(model_raw), usage
    )


def omp_message_timing(message: Any) -> dict[str, Any]:
    """Timing for one omp assistant message, or an unmeasured row.

    omp is the easy case, and deliberately so: ``duration``, ``ttft`` and
    ``usage`` all live on the same assistant record, so the numerator and the
    denominator arrive together and no association has to be inferred at all.

    The duration is the whole call, and ``ttft`` is the time to the first token,
    so the output-producing window is ``duration - ttft``. That subtraction is
    only valid while ttft is genuinely smaller; a record where it is not gets a
    status, not a clamped number.

    Two distinctions the source itself makes have to survive:
    * ``ttft`` present as 0 is "the first token was immediate", which is a real
      measurement, whereas an absent ``ttft`` means the whole window is unknown.
      Reading 0 as missing would silently discard valid calls.
    * ``usage.output`` already contains ``reasoningTokens`` (verified:
      ``totalTokens == input + cacheRead + cacheWrite + output`` on every local
      row), so reasoning is inside the numerator and must not be added again.
    * ``stopReason`` / ``errorId`` say whether the call finished. An error or an
      abort is reported ``incomplete``, which the report counts as an exclusion
      with a reason rather than as a call that simply had no clock.

    pi_agent parses the same file format and writes none of these fields, so it
    inherits nothing here; a source only gets speed when it actually measures it.
    """
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return unmeasured()
    if message.get("errorId") or str(message.get("stopReason") or "").lower() in \
            OMP_INCOMPLETE_STOP_REASONS:
        # Checked first, on purpose: 116 of the 491 assistant records in the local
        # corpus ended in error or abort. Labelling them "without timing" would
        # describe the wrong thing, and one that still carried output tokens would
        # have been measured as if it had completed.
        return unmeasured(STATUS_INCOMPLETE)
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return unmeasured()

    duration = message.get("duration")
    ttft = message.get("ttft")
    if duration is None:
        return unmeasured(STATUS_MISSING_TIMING)
    if ttft is None:
        # Absent, not zero. Without a first-token instant the output window
        # cannot be separated from the whole call.
        return unmeasured(STATUS_MISSING_TIMING)

    duration_ms = finite_positive_ms(duration)
    try:
        ttft_ms = float(ttft)
    except (TypeError, ValueError):
        return unmeasured(STATUS_INVALID_TIMING)
    if duration_ms is None or not math.isfinite(ttft_ms) or ttft_ms < 0:
        return unmeasured(STATUS_INVALID_TIMING)
    window = duration_ms - ttft_ms
    if window <= 0:
        # ttft >= duration is contradictory timing; clamping it to a tiny window
        # would manufacture an implausibly fast call.
        return unmeasured(STATUS_INVALID_TIMING)

    return measured(
        tokens=usage.get("output"),
        duration_ms=window,
        measurement_kind=KIND_POST_FIRST_TOKEN,
        token_basis=BASIS_INCLUDING,
    )


def turn_measurement_summary(speeds: list[Any]) -> dict[str, Any]:
    """Detail-only summary for one session's responses.

    ``speeds`` is one entry per *rendered turn* (a ``_speed`` dict, or None), so
    the counts answer "how many of the responses you can see carry a measurement"
    -- which is not the same question as "how many API calls were measured", since
    a turn that merged three calls is still one response. Both counts are returned
    rather than one, because the UI line and the comparison table need different
    denominators.

    A single session-level rate is only emitted when every measured turn shares one
    measurement kind and token basis. Two incompatible components never collapse
    into one scalar; the caller gets the per-component rows instead.
    """
    responses = len(speeds)
    measured_rows = [s for s in speeds if isinstance(s, dict) and int(s.get("speed_calls") or 0) > 0]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    excluded: dict[str, int] = {}
    for row in speeds:
        if not isinstance(row, dict):
            continue
        status = str(row.get("speed_status") or STATUS_UNKNOWN)
        if status in EXCLUSION_STATUSES:
            excluded[status] = excluded.get(status, 0) + 1
    for row in measured_rows:
        key = (str(row.get("speed_kind") or ""), str(row.get("speed_token_basis") or ""))
        group = groups.setdefault(
            key, {"speed_tokens": 0, "speed_ms": 0.0, "speed_calls": 0, "responses": 0}
        )
        group["speed_tokens"] += int(row.get("speed_tokens") or 0)
        group["speed_ms"] += float(row.get("speed_ms") or 0.0)
        group["speed_calls"] += int(row.get("speed_calls") or 0)
        group["responses"] += 1

    rows = []
    for (kind, basis), group in sorted(groups.items()):
        rows.append(
            {
                "measurement_kind": kind,
                "token_basis": basis,
                "speed_tokens": group["speed_tokens"],
                "speed_ms": round(group["speed_ms"], 3),
                "speed_calls": group["speed_calls"],
                "responses": group["responses"],
                "output_tok_per_s": rate_from_totals(group["speed_tokens"], group["speed_ms"]),
            }
        )

    summary: dict[str, Any] = {
        "responses": responses,
        "measured_responses": len(measured_rows),
        "measured_calls": sum(int(r.get("speed_calls") or 0) for r in measured_rows),
        "excluded": [{"status": k, "responses": v} for k, v in sorted(excluded.items())],
        "components": rows,
        "status": "measured" if measured_rows else "unavailable",
    }
    if len(rows) == 1:
        summary["output_tok_per_s"] = rows[0]["output_tok_per_s"]
        summary["measurement_kind"] = rows[0]["measurement_kind"]
        summary["token_basis"] = rows[0]["token_basis"]
    return summary
