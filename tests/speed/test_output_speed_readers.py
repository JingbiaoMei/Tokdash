"""The two source readers: does a duration land on the call that earned it?

Kimi is the hard one. Its counted tokens and its duration are two separate
records, so the association has to survive concurrent agents, replays,
compaction rows and a duration that arrives after its token row. omp is the
easy case, and the test for it is that the easy case stays easy: pi_agent shares
the parser and must inherit nothing.
"""
import json
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def _timing_reader_mode():
    from tokdash.speed_mode import collect_timings
    with collect_timings():
        yield


from tokdash import output_speed as sp
from tokdash.output_speed import (
    KIMI_MAX_PLAUSIBLE_TOK_PER_S,
    STATUS_AMBIGUOUS_PAIR,
    STATUS_INCOMPLETE,
    STATUS_INVALID_TIMING,
    STATUS_MEASURED,
    STATUS_MISSING_TIMING,
    kimi_step_timings,
    kimi_usage_record_key,
    omp_message_timing,
)

T0 = 1_780_000_000_000  # a fixed epoch-ms base; nothing here reads the clock


def _usage_record(path, ts, usage, model="kimi-k3", agent=None):
    row = {"type": "usage.record", "time": ts, "model": model, "usage": usage}
    if agent is not None:
        row["agentId"] = agent
    return row


def _step(path, ts, event):
    return {"type": "context.append_loop_event", "time": ts, "event": event}


def _bracket(ts, usage, decode_ms, step=1, extra=None):
    """One step.end that closes a step, carrying the same usage as the row."""
    event = {
        "type": "step.end", "uuid": f"u{step}", "turnId": "0", "step": step,
        "usage": usage, "finishReason": "tool_use",
        "llmServerDecodeMs": decode_ms, "llmStreamDurationMs": decode_ms + 8,
    }
    if extra:
        event.update(extra)
    return event


def _write(tmp_path, rows):
    path = tmp_path / "wire.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def _read_text(lines):
    """Feed records to an association the way both Kimi readers do, then resolve.

    The split signals are about a TEXT rather than a file, so the tests hand the
    association a list of records directly: that is exactly what the store's tail
    append does, minus the temporary file.
    """
    association = sp.KimiStepAssociation()
    for row in lines:
        association.observe(row)
        if row.get("type") == "usage.record":
            usage_, stamp = row.get("usage"), row.get("time")
            if isinstance(usage_, dict) and isinstance(stamp, (int, float)):
                association.register_usage(
                    agent=row.get("agentId"), timestamp_ms=int(stamp),
                    usage=usage_, identity=f"{stamp}",
                )
    association.resolve()
    return association


def _usage(output=620, other=6621, read=19200, create=0):
    return {"inputOther": other, "output": output, "inputCacheRead": read,
            "inputCacheCreation": create}


def test_kimi_pairs_a_usage_row_with_the_bracket_that_contains_it(tmp_path):
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1000, usage),
        _step(None, T0 + 21000, _bracket(T0 + 21000, usage, decode_ms=20000)),
    ]
    path = _write(tmp_path, rows)
    key = kimi_usage_record_key(path, T0 + 1000, "kimi-k3", usage)
    got = kimi_step_timings(path)[key]
    assert got["speed_status"] == STATUS_MEASURED
    assert got["speed_tokens"] == 620
    assert got["speed_ms"] == 20000.0
    assert got["speed_kind"] == "server_decode"
    # Reasoning is not a separate counter in this schema, so it is not claimed clean.
    assert got["speed_token_basis"] == "output_reasoning_unspecified"


def test_kimi_requires_the_whole_usage_object_to_match(tmp_path):
    # A bracket that merely spans the row is not evidence. Here it ran with a
    # different cache-read count, so it belongs to some other call.
    row_usage = _usage(output=620, read=19200)
    other_usage = _usage(output=620, read=9999)
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1000, row_usage),
        _step(None, T0 + 20000, _bracket(T0 + 20000, other_usage, decode_ms=19000)),
    ]
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0 + 1000, "kimi-k3", row_usage)]
    assert got["speed_status"] == STATUS_MISSING_TIMING
    assert got["speed_calls"] == 0


def test_a_row_inside_two_matching_brackets_is_ambiguous_not_guessed(tmp_path):
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "outer", "step": 1}),
        _step(None, T0 + 100, {"type": "step.begin", "uuid": "inner", "step": 2}),
        _usage_record(None, T0 + 200, usage),
        _step(None, T0 + 5000, _bracket(T0 + 5000, usage, decode_ms=4800, step=2)),
        _step(None, T0 + 9000, _bracket(T0 + 9000, usage, decode_ms=8900, step=1)),
    ]
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0 + 200, "kimi-k3", usage)]
    assert got["speed_status"] == STATUS_AMBIGUOUS_PAIR
    assert got["speed_ms"] == 0.0


def test_a_duration_never_crosses_an_agent_boundary(tmp_path):
    # Subagents really do run concurrently, so the main agent's step must not be
    # closed by a subagent's step.end that happens to carry the same usage.
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "m", "step": 1}),
        _usage_record(None, T0 + 1000, usage, agent="main"),
        _step(None, T0 + 20000, _bracket(T0 + 20000, usage, decode_ms=19000)),
    ]
    # Same events, but the usage row belongs to agent-0 and no agent-0 bracket exists.
    rows[1] = _usage_record(None, T0 + 1000, usage, agent="agent-0")
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0 + 1000, "kimi-k3", usage)]
    assert got["speed_status"] == STATUS_MISSING_TIMING


def test_a_usage_row_outside_any_bracket_stays_unmeasured(tmp_path):
    usage = _usage()
    rows = [
        _usage_record(None, T0, usage),
        _step(None, T0 + 1000, {"type": "step.begin", "uuid": "u", "step": 1}),
        _step(None, T0 + 20000, _bracket(T0 + 20000, usage, decode_ms=19000)),
    ]
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0, "kimi-k3", usage)]
    assert got["speed_status"] == STATUS_MISSING_TIMING


def test_a_duration_that_lands_in_a_later_append_still_lands(tmp_path):
    """The usage row is written first; its step.end arrives afterwards.

    This is why the store reparses a speed-tracking file whole instead of reading
    only the appended tail: on the two-line file nothing is measured, and on the
    complete file the same row is.
    """
    usage = _usage()
    early = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1000, usage),
    ]
    path = _write(tmp_path, early)
    key = kimi_usage_record_key(path, T0 + 1000, "kimi-k3", usage)
    assert kimi_step_timings(path)[key]["speed_status"] == STATUS_MISSING_TIMING

    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_step(None, T0 + 21000, _bracket(T0 + 21000, usage, 20000))) + "\n")
    after = kimi_step_timings(path)[key]
    assert after["speed_status"] == STATUS_MEASURED and after["speed_ms"] == 20000.0


def test_duplicated_usage_rows_collapse_onto_one_identity(tmp_path):
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1000, usage),
        _usage_record(None, T0 + 1000, usage),  # the same line written twice
        _step(None, T0 + 21000, _bracket(T0 + 21000, usage, 20000)),
    ]
    path = _write(tmp_path, rows)
    timings = kimi_step_timings(path)
    assert len(timings) == 1  # the parser's own dedup key, not two near-neighbours
    assert next(iter(timings.values()))["speed_ms"] == 20000.0



def test_a_row_stamped_a_millisecond_past_its_step_end_is_still_its_row(tmp_path):
    """Kimi closes the step, then stamps the row on the following millisecond.

    Found auditing the local corpus rather than the fixtures: 786 of 13,161
    brackets that carry usage -- 6 in every thousand calls -- hold their row 1 to
    8 ms after the bracket closed, 773 of them at exactly 1 ms. Read strictly,
    each of those is ordinary usage with no timing attached, which is a
    measurement the log plainly contains.
    """
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, usage, decode_ms=19_000)),
        _usage_record(None, T0 + 20_001, usage),  # one record later, one ms later
    ]
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0 + 20_001, "kimi-k3", usage)]
    assert got["speed_status"] == STATUS_MEASURED
    assert got["speed_ms"] == 19_000.0
    assert got["speed_tokens"] == usage["output"]


def test_a_row_minutes_past_its_step_end_is_not_that_calls_row(tmp_path):
    """The window opens a little past the close, not as far as the next call.

    A slack that reached the following turn would let one duration measure two
    calls, which is the exact error the pairing exists to prevent. Past the slack
    the row is countably unmeasured instead, which is a number the report shows.
    """
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, usage, decode_ms=19_000)),
        _usage_record(None, T0 + 80_000, usage),  # a minute on: some other call's row
    ]
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0 + 80_000, "kimi-k3", usage)]
    assert got["speed_status"] == STATUS_MISSING_TIMING


def test_one_step_end_cannot_time_two_rows_with_the_same_usage(tmp_path):
    """Uniqueness has to hold in both directions, not only the row's.

    Reported as one timing measuring two calls: two usage rows with identical
    counters inside one step each answer "exactly one bracket carries my usage",
    and each then billed the same decode window. A bracket is claimed, not shared,
    so a second row reaching it contests the pair and both rows stay unmeasured.
    """
    usage = _usage()
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1_000, usage),
        _usage_record(None, T0 + 9_000, usage),  # the same counters, a different call
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, usage, decode_ms=19_000)),
    ]
    path = _write(tmp_path, rows)
    timings = kimi_step_timings(path)
    assert len(timings) == 2, "two rows, not one row that swallowed the other"
    assert [row["speed_status"] for row in timings.values()] == [
        STATUS_AMBIGUOUS_PAIR, STATUS_AMBIGUOUS_PAIR,
    ]
    assert all(row["speed_calls"] == 0 and row["speed_ms"] == 0.0 for row in timings.values())


def test_a_slice_holding_only_a_lone_step_end_says_the_pair_was_split(tmp_path):
    """The signal the store's tail append reads, and the case it exists for.

    The store parses only what a file gained. When a turn's usage row went in on
    the last append and its step.end arrives now, the slice holds a bracket and no
    rows at all -- and a rule that demanded a row would call that file complete
    and strand the call unmeasured for good, because the append that would have
    mended it is the one that just happened.
    """
    usage = _usage()
    bracket_only = [
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, usage, decode_ms=19_000)),
    ]

    sliced = _read_text(bracket_only)
    assert sliced.unpaired_brackets == 1
    assert sliced.claims_a_row_this_text_missed is True

    whole = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1_000, usage),
        *bracket_only,
    ])
    assert whole.claims_a_row_this_text_missed is False
    assert whole.unpaired_brackets == 0


def test_a_slice_holding_only_the_row_says_the_pair_was_split_too(tmp_path):
    """The same boundary seen from the other side of the pair.

    Reported as a timing that a whole-file read measures and the store never does:
    the step.end went in on the previous append and only its usage.record arrives
    now, so the slice holds a row and no bracket. A row alone is invisible to a
    rule that counts unmatched brackets -- nothing here could pair it, so nothing
    here complains -- and the row settles as missing_timing for good. So the row
    asks for the whole-file read as well, but only when no step this text opened
    could have timed it: a row stamped after the first opening is a row the file
    genuinely never measured, and reparsing a whole file cannot change that.
    """
    usage = _usage()
    row_only = [_usage_record(None, T0 + 1_000, usage)]

    sliced = _read_text(row_only)
    assert sliced.unpaired_brackets == 0, "the bracket-side signal cannot see this"
    assert sliced.claims_a_bracket_this_text_missed is True

    whole = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        *row_only,
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, usage, decode_ms=19_000)),
    ])
    assert whole.claims_a_bracket_this_text_missed is False
    assert whole.resolve()[f"{T0 + 1_000}"]["speed_status"] == STATUS_MEASURED

    # A row that outstayed every step opening in this text, with brackets of its own
    # here, is the log's own missing timing rather than a split: see
    # test_a_row_minutes_past_its_step_end_is_not_that_calls_row.
    untimed = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, usage, decode_ms=19_000)),
        _usage_record(None, T0 + 80_000, _usage(output=120)),
    ])
    assert untimed.resolve()[f"{T0 + 80_000}"]["speed_status"] == STATUS_MISSING_TIMING
    assert untimed.claims_a_bracket_this_text_missed is False
    # Its own bracket-side signal does fire here, and correctly so: the bracket
    # carrying usage has no row inside its span in this text.
    assert untimed.claims_a_row_this_text_missed is True


def test_a_row_stamped_before_its_agents_first_opening_is_the_split_case(tmp_path):
    """The narrow edge that keeps the row-side signal from firing on every append.

    A slice carrying a later turn's whole pair plus the previous turn's row is the
    reverse split with company: one row here precedes any step this agent opened in
    this text, so it belongs to a bracket that went in earlier. Rows stamped inside
    the span do not qualify, and a signal that could not tell the two apart would
    ask for a whole-file read on appends that need none.
    """
    first, second = _usage(output=600), _usage(output=300)
    sliced = _read_text([
        _usage_record(None, T0 + 1_000, first),  # its step.end went in last append
        _step(None, T0 + 30_000, {"type": "step.begin", "uuid": "b2", "step": 2}),
        _usage_record(None, T0 + 31_000, second),
        _step(None, T0 + 40_000, _bracket(T0 + 40_000, second, decode_ms=9_000, step=2)),
    ])
    verdicts = sliced.resolve()
    assert verdicts[f"{T0 + 1_000}"]["speed_status"] == STATUS_MISSING_TIMING
    assert verdicts[f"{T0 + 31_000}"]["speed_status"] == STATUS_MEASURED
    assert sliced.claims_a_bracket_this_text_missed is True


def test_a_row_inside_a_step_that_has_not_closed_yet_asks_for_nothing(tmp_path):
    """The append-cost edge, which is why the bound is the opening and not the close.

    A slice that cut a turn in half holds the step.begin and the row with the
    step.end still unwritten. That row is not a split pair: there is no bracket
    anywhere in the file for a whole-file read to find, because the log has not
    written one yet. Asking anyway would cost a whole-file reparse an answer could
    not come from, and on a file whose steps stay open across appends it would cost
    one per append. The opening is the bound that settles it -- the step opened here,
    so whatever times this row is expected from here.
    """
    usage = _usage()
    sliced = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "b1", "step": 1}),
        _usage_record(None, T0 + 1_000, usage),
    ])
    assert sliced.resolve()[f"{T0 + 1_000}"]["speed_status"] == STATUS_MISSING_TIMING
    assert sliced.claims_a_bracket_this_text_missed is False
    assert sliced.claims_a_row_this_text_missed is False


def test_legacy_kimi_rows_have_no_timing_at_all(tmp_path):
    # The pre-0.26 StatusUpdate schema carries tokens only; those rows must be
    # unmeasured rather than measured against something invented.
    path = _write(tmp_path, [
        {"timestamp": 1772830161.3, "message": {"type": "StatusUpdate", "payload": {
            "token_usage": {"input_other": 1, "output": 2, "input_cache_read": 3,
                            "input_cache_creation": 0}, "message_id": "m1"}}},
    ])
    assert kimi_step_timings(path) == {}


def test_the_implausible_kimi_decode_window_is_excluded_not_ceiling_ed(tmp_path):
    # 1882 output tokens in a 1 ms "server decode" window: the signature of a
    # buffered response being consumed. It must leave the measurement pool.
    usage = _usage(output=1882)
    rows = [
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _usage_record(None, T0 + 1000, usage),
        _step(None, T0 + 1005, _bracket(T0 + 1005, usage, decode_ms=1,
                                        extra={"llmFirstTokenLatencyMs": 7655})),
    ]
    path = _write(tmp_path, rows)
    got = kimi_step_timings(path)[kimi_usage_record_key(path, T0 + 1000, "kimi-k3", usage)]
    assert got["speed_status"] == STATUS_INVALID_TIMING
    assert got["speed_calls"] == 0
    # And a fast-but-real call is not censored by the same rule.
    ok = sp.measured(tokens=100, duration_ms=100.0, measurement_kind=sp.KIND_SERVER_DECODE,
                     token_basis=sp.BASIS_UNSPECIFIED,
                     max_tok_per_s=KIMI_MAX_PLAUSIBLE_TOK_PER_S)
    assert ok["speed_status"] == STATUS_MEASURED


def test_other_sources_are_not_given_the_kimi_ceiling(tmp_path):
    # The same arithmetic without a source-specific reason is a measurement.
    row = sp.measured(tokens=1882, duration_ms=1.0, measurement_kind=sp.KIND_REQUEST_WINDOW,
                      token_basis=sp.BASIS_UNSPECIFIED)
    assert row["speed_status"] == STATUS_MEASURED


def test_kimi_reader_fails_loudly_on_an_unreadable_file(tmp_path):
    # Returning {} here would let a locked wire.jsonl parse as "no timings" and get
    # memoized under a key that never changes again. The caller owns that decision.
    with pytest.raises(OSError):
        kimi_step_timings(str(tmp_path / "nope.jsonl"))


# --------------------------------------------------------------------------
# omp: tokens and duration arrive on one record
# --------------------------------------------------------------------------


def _omp(usage=None, duration=5000, ttft=1000, role="assistant"):
    msg = {"role": role, "usage": usage if usage is not None else {
        "input": 100, "output": 200, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 300}}
    if duration is not None:
        msg["duration"] = duration
    if ttft is not None:
        msg["ttft"] = ttft
    return msg


def test_omp_window_is_the_call_minus_time_to_first_token():
    got = omp_message_timing(_omp(duration=5000, ttft=1000))
    assert got["speed_status"] == STATUS_MEASURED
    assert got["speed_ms"] == 4000.0 and got["speed_tokens"] == 200
    assert got["speed_kind"] == "post_first_token"


def test_zero_ttft_is_a_measurement_while_absent_ttft_is_not():
    # A zero first-token latency is real ("the first token was immediate"). An
    # absent one means the output window was never separated from the call.
    zero = omp_message_timing(_omp(duration=5000, ttft=0))
    assert zero["speed_status"] == STATUS_MEASURED and zero["speed_ms"] == 5000.0
    absent = omp_message_timing(_omp(duration=5000, ttft=None))
    assert absent["speed_status"] == STATUS_MISSING_TIMING


def test_a_window_that_never_started_produces_no_rate():
    for ttft, expected in ((5000, STATUS_INVALID_TIMING), (6000, STATUS_INVALID_TIMING)):
        got = omp_message_timing(_omp(duration=5000, ttft=ttft))
        assert got["speed_status"] == expected, ttft


def test_omp_reasoning_is_inside_the_numerator_not_added_to_it():
    # totalTokens == input + cacheRead + cacheWrite + output on every local row,
    # so reasoning already lives inside `output` and must not be summed again.
    usage = {"input": 100, "output": 200, "cacheRead": 0, "cacheWrite": 0,
             "totalTokens": 300, "reasoningTokens": 60}
    got = omp_message_timing(_omp(usage=usage, duration=5000, ttft=1000))
    assert got["speed_tokens"] == 200
    assert got["speed_token_basis"] == "output_including_reasoning"


@pytest.mark.parametrize("message", [None, {}, {"role": "user"}, {"role": "assistant"},
                                     {"role": "assistant", "usage": {}}])
def test_omp_non_message_rows_are_unmeasured(message):
    assert omp_message_timing(message)["speed_calls"] == 0

def test_an_omp_call_the_source_says_failed_is_not_a_measurement():
    # The local omp corpus labels 116 of 491 assistant records error/aborted.
    # Those endings are explicit source state, so they are reported as an
    # exclusion with a reason -- never as a rate, and never as "no timing".
    for stop_reason in ("error", "aborted", "ERROR"):
        message = _omp(duration=5000, ttft=1000)
        message["stopReason"] = stop_reason
        got = omp_message_timing(message)
        assert got["speed_status"] == STATUS_INCOMPLETE, stop_reason
        assert got["speed_calls"] == 0 and got["speed_tokens"] == 0

    errored = _omp(duration=5000, ttft=1000)
    errored["errorId"] = "upstream_500"
    assert omp_message_timing(errored)["speed_status"] == STATUS_INCOMPLETE


def test_a_completed_omp_call_is_still_measured_even_when_it_used_tools():
    # Only error/abort are exclusions. toolUse and stop are ordinary endings, and
    # excluding them would empty the omp population rather than clean it.
    for stop_reason in ("stop", "toolUse"):
        message = _omp(duration=5000, ttft=1000)
        message["stopReason"] = stop_reason
        assert omp_message_timing(message)["speed_status"] == STATUS_MEASURED, stop_reason


def test_a_late_row_behind_the_next_step_opening_is_still_a_split(tmp_path):
    """The mixed tail: the boundary falls inside a pair AND behind another step.

    Reported as a row stored missing_timing that a whole-file read measures at 600
    tokens over 19,000 ms. Step A closes before the cut; the tail is B opening, A's
    usage record arriving late, then B's whole rest. The row-side bound used to be
    the earliest step BEGIN in the slice, and A's row clears it -- it is stamped
    after B opened, which says nothing at all about where A's bracket closed -- so
    nothing asked for the whole file and the timing was gone for good.

    The bound that holds is the earliest RECORD of that agent plus the row clock's
    slack. A bracket hidden from this text closed before this text's first record,
    and it only ever times a row stamped within the slack of that close, so a row
    unmeasured inside that horizon is a split and a row outside it is not.
    """
    first, second = _usage(output=600), _usage(output=300)
    sliced = _read_text([
        _step(None, T0 + 20_100, {"type": "step.begin", "uuid": "b2", "step": 2}),
        _usage_record(None, T0 + 20_200, first),  # A's row, arriving after B opened
        _step(None, T0 + 30_000, _bracket(T0 + 30_000, second, decode_ms=9_000, step=2)),
        _usage_record(None, T0 + 30_100, second),
    ])
    verdicts = sliced.resolve()
    assert verdicts[f"{T0 + 20_200}"]["speed_status"] == STATUS_MISSING_TIMING
    assert verdicts[f"{T0 + 30_100}"]["speed_status"] == STATUS_MEASURED
    assert sliced.claims_a_row_this_text_missed is False, (
        "every bracket here finds its own row; the bracket-side signal is blind to this"
    )
    assert sliced.claims_a_bracket_this_text_missed is True

    whole = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "b1", "step": 1}),
        # A closed before the cut, and its row landed a record and 200 ms later.
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, first, decode_ms=19_000)),
        _step(None, T0 + 20_100, {"type": "step.begin", "uuid": "b2", "step": 2}),
        _usage_record(None, T0 + 20_200, first),
        _step(None, T0 + 30_000, _bracket(T0 + 30_000, second, decode_ms=9_000, step=2)),
        _usage_record(None, T0 + 30_100, second),
    ])
    assert whole.resolve()[f"{T0 + 20_200}"]["speed_status"] == STATUS_MEASURED
    assert whole.resolve()[f"{T0 + 20_200}"]["speed_ms"] == 19_000.0
    assert whole.claims_a_bracket_this_text_missed is False


@pytest.mark.parametrize("closed_step", [False, True])
@pytest.mark.parametrize("delay_ms, split", [(499, True), (500, True), (501, False)])
def test_a_split_row_includes_the_matching_clocks_upper_boundary(closed_step, delay_ms, split):
    first, second = _usage(output=600), _usage(output=300)
    stamp = T0 + 20_000 + delay_ms
    tail = [
        _step(None, T0 + 20_000, {"type": "step.begin", "uuid": "u2", "step": 2}),
        _usage_record(None, stamp, first),
    ]
    if closed_step:
        tail.extend([
            _usage_record(None, T0 + 21_000, second),
            _step(None, T0 + 30_000, _bracket(T0 + 30_000, second, decode_ms=9_000, step=2)),
        ])
    sliced = _read_text(tail)
    assert sliced.resolve()[str(stamp)]["speed_status"] == STATUS_MISSING_TIMING
    assert sliced.claims_a_bracket_this_text_missed is split

    # The fallback horizon must include every row a whole read could match.
    whole = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "u1", "step": 1}),
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, first, decode_ms=19_000)),
        *tail,
    ])
    expected = STATUS_MEASURED if split else STATUS_MISSING_TIMING
    assert whole.resolve()[str(stamp)]["speed_status"] == expected


def test_a_row_past_the_horizon_behind_an_opening_is_not_a_split(tmp_path):
    """The other side of the same horizon, so the wider bound stays narrow.

    A row unmeasured long after this text's first record could only have been
    timed by a bracket this text holds. It is the log's own missing timing, and a
    whole-file reparse would cost itself an answer it cannot give.
    """
    sliced = _read_text([
        _step(None, T0, {"type": "step.begin", "uuid": "b1", "step": 1}),
        _step(None, T0 + 20_000, _bracket(T0 + 20_000, _usage(), decode_ms=19_000)),
        _usage_record(None, T0 + 90_000, _usage(output=120)),
        _step(None, T0 + 100_000, {"type": "step.begin", "uuid": "b2", "step": 2}),
    ])
    assert sliced.resolve()[f"{T0 + 90_000}"]["speed_status"] == STATUS_MISSING_TIMING
    assert sliced.claims_a_bracket_this_text_missed is False
