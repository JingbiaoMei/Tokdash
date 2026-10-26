"""The output-speed metric contract and the two shared source readers.

These are the gates the feature stands on: a rate is always a ratio of summed
statistics, a duration only ever reaches the row whose tokens it describes, and
anything that cannot be paired stays visibly unmeasured rather than becoming a
number. Every case here came from a shape seen in a real log.
"""
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokdash import output_speed as sp
from tokdash.output_speed import (
    KIMI_MAX_PLAUSIBLE_TOK_PER_S,
    STATUS_AMBIGUOUS_PAIR,
    STATUS_INVALID_TIMING,
    STATUS_MEASURED,
    STATUS_MISSING_TIMING,
    STATUS_UNKNOWN,
    kimi_step_timings,
    kimi_usage_record_key,
    omp_message_timing,
    public_turn_speed,
    rate_from_totals,
    turn_measurement_summary,
    unmeasured,
)


# --------------------------------------------------------------------------
# The derived rate
# --------------------------------------------------------------------------


def test_rate_is_a_ratio_of_sums_not_a_mean_of_rates():
    # Call A: 1000 tokens in one second (1000 tok/s). Call B: 10 tokens in ten
    # seconds (1 tok/s). The mean of the two rates is 500.5, which describes no
    # window that ever existed; the throughput across both windows is 91.8.
    assert sp.merge_totals((1000, 1000.0), (10, 10000.0)) == pytest.approx(91.8, abs=0.05)
    assert rate_from_totals(1010, 11000.0) == pytest.approx(91.8, abs=0.05)


def test_unequal_durations_weight_by_time_not_by_call():
    # A 4 s call must dominate a 40 ms call, which averaging per-call rates undoes.
    long_call = rate_from_totals(400, 4000.0)
    short_call = rate_from_totals(2, 40.0)
    assert long_call == 100.0 and short_call == 50.0
    assert rate_from_totals(402, 4040.0) == pytest.approx(99.5, abs=0.05)


def test_rounding_happens_after_aggregation():
    # Two calls that each round to 33.3 individually still sum to their true ratio.
    assert rate_from_totals(100 + 100, 3000 + 3000) == 33.3


@pytest.mark.parametrize("tokens,ms", [(0, 100.0), (100, 0), (100, None), (None, 100.0),
                                       (100, float("nan")), (100, float("inf")),
                                       (-5, 100.0), (100, -1.0)])
def test_absence_and_broken_inputs_are_never_a_rate(tokens, ms):
    assert rate_from_totals(tokens, ms) is None


def test_fractional_milliseconds_survive():
    assert rate_from_totals(1000, 1234.567) == pytest.approx(810.0, abs=0.1)


# --------------------------------------------------------------------------
# measured() / sanitize_stored()
# --------------------------------------------------------------------------


def test_measured_keeps_kind_and_basis():
    row = sp.measured(tokens=800, duration_ms=20000.0,
                      measurement_kind=sp.KIND_RESPONSE_WINDOW,
                      token_basis=sp.BASIS_UNSPECIFIED)
    assert row == {"speed_tokens": 800, "speed_ms": 20000.0, "speed_calls": 1,
                   "speed_kind": "response_window",
                   "speed_token_basis": "output_reasoning_unspecified",
                   "speed_status": "measured"}


def test_measured_rejects_unknown_labels_instead_of_inventing_them():
    with pytest.raises(ValueError):
        sp.measured(tokens=1, duration_ms=1.0, measurement_kind="vibes",
                    token_basis=sp.BASIS_UNSPECIFIED)


@pytest.mark.parametrize("duration", [0, 0.0, -1.0, None, "abc", float("nan"), float("inf")])
def test_unusable_durations_become_a_status_not_a_zero_window(duration):
    row = sp.measured(tokens=100, duration_ms=duration,
                      measurement_kind=sp.KIND_SERVER_DECODE,
                      token_basis=sp.BASIS_UNSPECIFIED)
    assert row["speed_status"] == STATUS_INVALID_TIMING
    assert row["speed_calls"] == 0 and row["speed_ms"] == 0.0


def test_zero_output_call_is_unmeasured_rather_than_infinitely_fast():
    row = sp.measured(tokens=0, duration_ms=1000.0, measurement_kind=sp.KIND_SERVER_DECODE,
                      token_basis=sp.BASIS_UNSPECIFIED)
    assert row["speed_status"] == STATUS_MISSING_TIMING
    assert row["speed_calls"] == 0


def test_sanitize_refuses_to_store_a_poison_duration():
    # SQLite stores NaN and Infinity without complaint, and one of them in a group
    # would make SUM(speed_ms) useless for every other row in that group.
    for poison in (float("nan"), float("inf"), "1e400"):
        row = sp.sanitize_stored({"speed_tokens": 100, "speed_ms": poison, "speed_calls": 1,
                                  "speed_kind": "server_decode",
                                  "speed_token_basis": "output_reasoning_unspecified",
                                  "speed_status": "measured"})
        assert row["speed_status"] != STATUS_MEASURED
        assert row["speed_calls"] == 0


def test_sanitize_keeps_an_exclusion_reason_and_drops_its_statistics():
    row = sp.sanitize_stored({"speed_tokens": 100, "speed_ms": 1.0, "speed_calls": 1,
                              "speed_kind": "server_decode",
                              "speed_token_basis": "output_reasoning_unspecified",
                              "speed_status": STATUS_AMBIGUOUS_PAIR})
    assert row["speed_status"] == STATUS_AMBIGUOUS_PAIR
    assert row["speed_tokens"] == 0 and row["speed_ms"] == 0.0 and row["speed_calls"] == 0


def test_a_minted_verdict_is_immutable_and_trusted_by_identity():
    """``CanonicalSpeed`` is the store's trust boundary, so the type carries weight.

    A verdict is now shared by every row that carries it, and re-validated only
    when it did not come from here. That needs both halves: the object cannot be
    edited after the fact, and a plain dict that merely looks canonical gets no
    pass. Both are pinned here because getting either wrong means a measurement
    that quietly applies to several calls, or a poisoned column.
    """
    minted = sp.measured(tokens=120, duration_ms=1000.0,
                         measurement_kind=sp.KIND_SERVER_DECODE,
                         token_basis=sp.BASIS_UNSPECIFIED)
    assert isinstance(minted, sp.CanonicalSpeed)
    assert sp.sanitize_stored(minted) is minted
    assert isinstance(unmeasured(), sp.CanonicalSpeed)

    for mutate in (lambda: minted.__setitem__("speed_ms", 1.0),
                   lambda: minted.__delitem__("speed_ms"),
                   lambda: minted.update({"speed_ms": 1.0}),
                   lambda: minted.setdefault("speed_ms", 1.0),
                   lambda: minted.pop("speed_ms"),
                   lambda: minted.popitem(),
                   lambda: minted.clear()):
        with pytest.raises(TypeError):
            mutate()

    # Copying one loses the trust it was minted with, which is the point: the copy
    # is validated like any other hand-written dict, and agrees with the original.
    assert type(dict(minted)) is dict
    assert sp.sanitize_stored(dict(minted)) == dict(minted)
    poisoned = sp.sanitize_stored({**dict(minted), "speed_ms": float("nan")})
    assert poisoned["speed_status"] != STATUS_MEASURED


def test_unmeasured_is_zeroes_with_a_status_never_a_zero_rate():
    assert unmeasured(STATUS_MISSING_TIMING) == {
        "speed_tokens": 0, "speed_ms": 0.0, "speed_calls": 0, "speed_kind": "",
        "speed_token_basis": "", "speed_status": STATUS_MISSING_TIMING}
    assert unmeasured("nonsense")["speed_status"] == STATUS_UNKNOWN


def test_coverage_stays_unknown_when_the_denominator_is_unknown():
    # 0% and 100% are both claims about a population this row has never seen.
    assert sp.coverage(5, None) is None
    assert sp.coverage(5, 0) is None
    assert sp.coverage(0, 0) is None
    assert sp.coverage(5, 10) == 0.5


def test_coverage_never_claims_more_than_everything():
    assert sp.coverage(7, 5) == 1.0


def test_public_turn_shape_and_absence():
    row = {"speed_tokens": 800, "speed_ms": 20000.0, "speed_calls": 1,
           "speed_kind": "response_window", "speed_token_basis": "output_reasoning_unspecified"}
    assert public_turn_speed(row) == {"output_tok_per_s": 40.0, "speed_tokens": 800,
                                      "speed_ms": 20000.0, "speed_calls": 1,
                                      "measurement_kind": "response_window",
                                      "token_basis": "output_reasoning_unspecified"}
    assert public_turn_speed(unmeasured()) is None
