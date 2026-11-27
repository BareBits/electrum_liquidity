"""Pure-engine tests for the wedged channel-open gates and the
cooperative-close-before-force-close invariant.

These cover the decision logic that stops the plugin force-closing a channel
that is merely slow: the strike accumulator (count + spread + reset), the wedge
clock, the ``coop_before_force_ready`` escalation gate, and the split freeze
classification. No Electrum, no clock, no I/O -- ``now`` is passed in.
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore
    COOP_BEFORE_FORCE_WINDOW_SEC,
    WEDGE_MIN_SPREAD_SEC,
    WEDGE_REQUIRED_STRIKES,
    classify_pending_freeze,
    coop_before_force_ready,
    record_wedge_strike,
    should_remediate_wedged_open,
    wedge_clock_expired,
    wedge_strikes_satisfied,
)

T0 = 1_700_000_000.0
SIX_HOURS = 6 * 3600.0


# --- strike accumulator ---------------------------------------------------
def test_first_eligible_observation_starts_the_clock() -> None:
    acc = record_wedge_strike(None, T0, True)
    assert acc == {"strikes": 1, "first_ts": T0, "last_ts": T0}


def test_ineligible_observation_clears_the_record() -> None:
    acc = record_wedge_strike(None, T0, True)
    acc = record_wedge_strike(acc, T0 + 60, True)
    assert acc["strikes"] == 2
    # A single recovery wipes the history entirely -- a later, unrelated wedge
    # must not inherit strikes from this one.
    assert record_wedge_strike(acc, T0 + 120, False) is None


def test_strikes_accumulate_and_first_ts_is_stable() -> None:
    acc = None
    for i in range(5):
        acc = record_wedge_strike(acc, T0 + i * 900, True)
    assert acc["strikes"] == 5
    assert acc["first_ts"] == T0            # clock origin never moves
    assert acc["last_ts"] == T0 + 4 * 900


def test_input_accumulator_is_not_mutated() -> None:
    acc = record_wedge_strike(None, T0, True)
    before = dict(acc)
    record_wedge_strike(acc, T0 + 60, True)
    assert acc == before


def test_backwards_clock_reanchors_first_ts() -> None:
    """A stored first_ts in the future means the wall clock moved backwards
    (NTP correction, wrong-clock boot). Trusting it would distort the age."""
    acc = {"strikes": 3, "first_ts": T0 + 10_000, "last_ts": T0 + 10_000}
    out = record_wedge_strike(acc, T0, True)
    assert out["first_ts"] == T0


def test_malformed_accumulator_is_tolerated() -> None:
    for junk in ({"strikes": "x"}, {"first_ts": None}, {"first_ts": "nope"}, {}):
        out = record_wedge_strike(junk, T0, True)
        assert out["strikes"] >= 1 and isinstance(out["first_ts"], float)


# --- the strike gate: count AND spread ------------------------------------
def test_burst_of_strikes_does_not_satisfy_the_spread() -> None:
    """Evaluation ticks are event-driven and can fire several times a second.
    Count alone would be nearly free to satisfy; the spread is what makes the
    gate mean 'persistently wedged'."""
    acc = None
    for i in range(WEDGE_REQUIRED_STRIKES + 5):
        acc = record_wedge_strike(acc, T0 + i * 0.1, True)
    assert acc["strikes"] > WEDGE_REQUIRED_STRIKES
    assert not wedge_strikes_satisfied(acc)


def test_spread_without_enough_strikes_does_not_satisfy() -> None:
    acc = record_wedge_strike(None, T0, True)
    acc = record_wedge_strike(acc, T0 + WEDGE_MIN_SPREAD_SEC * 2, True)
    assert acc["last_ts"] - acc["first_ts"] > WEDGE_MIN_SPREAD_SEC
    assert acc["strikes"] < WEDGE_REQUIRED_STRIKES
    assert not wedge_strikes_satisfied(acc)


def test_count_and_spread_together_satisfy() -> None:
    acc = None
    step = WEDGE_MIN_SPREAD_SEC / (WEDGE_REQUIRED_STRIKES - 1)
    for i in range(WEDGE_REQUIRED_STRIKES):
        acc = record_wedge_strike(acc, T0 + i * step, True)
    assert wedge_strikes_satisfied(acc)


def test_no_accumulator_never_satisfies() -> None:
    assert not wedge_strikes_satisfied(None)
    assert not wedge_clock_expired(None, T0, SIX_HOURS)
    assert not should_remediate_wedged_open(None, T0, timeout_sec=SIX_HOURS)


# --- the wedge clock ------------------------------------------------------
def test_clock_runs_from_first_strike_not_from_now() -> None:
    acc = record_wedge_strike(None, T0, True)
    assert not wedge_clock_expired(acc, T0 + SIX_HOURS - 1, SIX_HOURS)
    assert wedge_clock_expired(acc, T0 + SIX_HOURS, SIX_HOURS)


def test_remediation_needs_both_clock_and_strikes() -> None:
    # Clock expired, but only one strike: not yet.
    slow = record_wedge_strike(None, T0, True)
    assert wedge_clock_expired(slow, T0 + SIX_HOURS, SIX_HOURS)
    assert not should_remediate_wedged_open(slow, T0 + SIX_HOURS,
                                            timeout_sec=SIX_HOURS)
    # Strikes satisfied, but the clock has not run: also not yet.
    acc = None
    step = WEDGE_MIN_SPREAD_SEC / (WEDGE_REQUIRED_STRIKES - 1)
    for i in range(WEDGE_REQUIRED_STRIKES):
        acc = record_wedge_strike(acc, T0 + i * step, True)
    assert wedge_strikes_satisfied(acc)
    assert not should_remediate_wedged_open(acc, T0 + WEDGE_MIN_SPREAD_SEC,
                                            timeout_sec=SIX_HOURS)
    # Both: remediate.
    acc = record_wedge_strike(acc, T0 + SIX_HOURS, True)
    assert should_remediate_wedged_open(acc, T0 + SIX_HOURS, timeout_sec=SIX_HOURS)


# --- cooperative close before force close ---------------------------------
def test_no_record_never_escalates() -> None:
    assert not coop_before_force_ready(None, T0)
    assert not coop_before_force_ready({}, T0)


def test_never_reachable_peer_escalates_after_the_window() -> None:
    """Nobody to cooperate with: the peer gets the window to come back, then a
    force-close is the only option left."""
    rec = {"first_ts": T0, "coop_attempts": 0, "last_coop_ts": None}
    assert not coop_before_force_ready(rec, T0 + COOP_BEFORE_FORCE_WINDOW_SEC - 1)
    assert coop_before_force_ready(rec, T0 + COOP_BEFORE_FORCE_WINDOW_SEC)


def test_attempted_coop_defers_escalation_by_a_fresh_window() -> None:
    """An attempt made late must reset the window -- otherwise a cooperative
    close launched at the deadline would be force-closed out from under itself
    while still negotiating."""
    rec = {"first_ts": T0, "coop_attempts": 1,
           "last_coop_ts": T0 + COOP_BEFORE_FORCE_WINDOW_SEC * 3}
    # Far past first_ts, but the attempt is recent: do not escalate.
    assert not coop_before_force_ready(rec, T0 + COOP_BEFORE_FORCE_WINDOW_SEC * 3 + 1)
    assert coop_before_force_ready(
        rec, T0 + COOP_BEFORE_FORCE_WINDOW_SEC * 4)


def test_malformed_close_attempt_does_not_escalate() -> None:
    """Unparseable persisted state must fail CLOSED (no force-close), never
    open -- the irreversible action is the one that needs certainty."""
    for junk in ({"first_ts": "x"}, {"last_coop_ts": "x"}, {"first_ts": 0}):
        assert not coop_before_force_ready(junk, T0 + 10 * COOP_BEFORE_FORCE_WINDOW_SEC)


# --- split freeze classification -----------------------------------------
STUCK = 3600.0
CONFIRMING_FREEZE = 3 * 86400.0


def _freeze(is_confirming, age):
    return classify_pending_freeze(is_confirming, age, stuck_sec=STUCK,
                                   confirming_freeze_sec=CONFIRMING_FREEZE)


def test_confirming_channel_blocks_opens_only() -> None:
    assert _freeze(True, 60.0) == (True, False)
    assert _freeze(True, CONFIRMING_FREEZE - 1) == (True, False)


def test_confirming_channel_stops_blocking_after_its_window() -> None:
    assert _freeze(True, CONFIRMING_FREEZE) == (False, False)


def test_wedged_channel_blocks_everything_until_it_ages_out() -> None:
    assert _freeze(False, 60.0) == (True, True)
    assert _freeze(False, STUCK - 1) == (True, True)
    assert _freeze(False, STUCK) == (False, False)


def test_confirming_never_blocks_swaps_at_any_age() -> None:
    """The regression this split exists to prevent: a days-long confirmation
    stalling reverse swaps on unrelated channels."""
    for age in (0.0, STUCK, CONFIRMING_FREEZE / 2, CONFIRMING_FREEZE * 10):
        assert _freeze(True, age)[1] is False


# --- a corrupt accumulator ------------------------------------------------
# The strike accumulator is JSON in wallet.db -- the same exposure the uptime
# accumulator has (see test_uptime_malformed_data), and the same answer: a value
# that will not coerce must cost the decision, never raise. It is read on the
# path to a FORCE-CLOSE, so the safe answer is always "do not remediate": a
# missed remediation leaves funds locked in a channel that another tick will
# look at again, while a spurious one spends a mining fee and a CSV timelock on
# a channel that may have been fine.
#
# ``record_wedge_strike`` already had this covered; the two predicates that READ
# the record did not, and nothing exercised a ``first_ts`` of 0 either -- the
# shape a record gets from a half-written or hand-edited wallet file.
_BAD_STRIKES = {"strikes": "lots", "first_ts": T0, "last_ts": T0 + SIX_HOURS}
_BAD_FIRST_TS = {"strikes": 9, "first_ts": "yesterday", "last_ts": T0}
_BAD_LAST_TS = {"strikes": 9, "first_ts": T0, "last_ts": ["nope"]}
_ALL_NULL = {"strikes": None, "first_ts": None, "last_ts": None}
_ALL_DICTS = {"strikes": 9, "first_ts": {}, "last_ts": {}}

MALFORMED = [
    pytest.param(_BAD_STRIKES, id="strikes-not-a-number"),
    pytest.param(_BAD_FIRST_TS, id="first_ts-not-a-number"),
    pytest.param(_BAD_LAST_TS, id="last_ts-not-a-number"),
    pytest.param(_ALL_NULL, id="all-null"),
    pytest.param(_ALL_DICTS, id="dicts-where-numbers-go"),
]

# The subset whose ``first_ts`` is itself unusable. Only these make the wedge
# CLOCK refuse: it reads nothing but ``first_ts``, so a record with a sound
# origin and a corrupt strike count has a legitimately expired clock -- and is
# refused by the strike gate instead. The two predicates are deliberately
# independent, and ``should_remediate_wedged_open`` needing both is what makes
# any single corrupt field enough to stop a force-close.
MALFORMED_ORIGIN = [
    pytest.param(_BAD_FIRST_TS, id="first_ts-not-a-number"),
    pytest.param(_ALL_NULL, id="all-null"),
    pytest.param(_ALL_DICTS, id="dicts-where-numbers-go"),
]


@pytest.mark.parametrize("acc", MALFORMED)
def test_a_malformed_record_satisfies_no_strike_gate(acc) -> None:
    assert wedge_strikes_satisfied(acc) is False


@pytest.mark.parametrize("acc", MALFORMED_ORIGIN)
def test_an_unreadable_origin_never_expires_the_wedge_clock(acc) -> None:
    assert wedge_clock_expired(acc, T0 + 10 * SIX_HOURS, SIX_HOURS) is False


@pytest.mark.parametrize("acc", [
    pytest.param(_BAD_STRIKES, id="strikes-not-a-number"),
    pytest.param(_BAD_LAST_TS, id="last_ts-not-a-number"),
])
def test_a_sound_origin_still_expires_its_clock(acc) -> None:
    """Documented so the asymmetry above is not read as an oversight: these
    records have a real ``first_ts``, so the clock answers honestly. The strike
    gate is what refuses them, and the combined verdict below is what matters.
    """
    assert wedge_clock_expired(acc, T0 + 10 * SIX_HOURS, SIX_HOURS) is True
    assert wedge_strikes_satisfied(acc) is False


@pytest.mark.parametrize("acc", MALFORMED)
def test_a_malformed_record_never_remediates(acc) -> None:
    """The verdict as the glue asks it: whatever the record says, a force-close
    is not authorised by a record we cannot read."""
    assert should_remediate_wedged_open(
        acc, T0 + 10 * SIX_HOURS, timeout_sec=SIX_HOURS) is False


def test_a_first_ts_of_zero_never_expires_the_clock() -> None:
    """0 is what a missing/half-written timestamp deserialises to, and it is 1970
    -- so treating it as a real origin would make every such channel instantly
    past its timeout. It has to read as "no origin recorded" instead."""
    acc = {"strikes": WEDGE_REQUIRED_STRIKES + 1, "first_ts": 0.0,
           "last_ts": T0 + WEDGE_MIN_SPREAD_SEC}
    assert wedge_clock_expired(acc, T0 + 10 * SIX_HOURS, SIX_HOURS) is False
    # The strike gate is satisfied on its own, so the clock is demonstrably the
    # only thing refusing -- which is what makes this the deciding guard.
    assert wedge_strikes_satisfied(acc) is True
    assert should_remediate_wedged_open(
        acc, T0 + 10 * SIX_HOURS, timeout_sec=SIX_HOURS) is False


def test_a_negative_first_ts_is_refused_too() -> None:
    """A clock that went backwards far enough to produce a negative stamp is the
    same class of nonsense as 0, and ``<= 0`` has to catch both."""
    acc = {"strikes": WEDGE_REQUIRED_STRIKES + 1, "first_ts": -5.0,
           "last_ts": T0 + WEDGE_MIN_SPREAD_SEC}
    assert wedge_clock_expired(acc, T0, SIX_HOURS) is False


def test_an_empty_record_is_not_a_wedge() -> None:
    """``{}`` and ``None`` are both "never seen wedged", which must never
    remediate -- the channel has not even been observed once."""
    for acc in (None, {}):
        assert wedge_strikes_satisfied(acc) is False
        assert wedge_clock_expired(acc, T0 + 10 * SIX_HOURS, SIX_HOURS) is False
        assert should_remediate_wedged_open(
            acc, T0 + 10 * SIX_HOURS, timeout_sec=SIX_HOURS) is False


def test_a_malformed_record_is_repaired_by_the_next_observation() -> None:
    """A corrupt record must not wedge the gate shut for good: folding in the
    next eligible observation rebuilds a usable record, so the channel starts
    accruing real strikes again from now."""
    acc = record_wedge_strike({"strikes": "lots", "first_ts": "yesterday"},
                              T0, True)
    assert acc == {"strikes": 1, "first_ts": T0, "last_ts": T0}
    for i in range(1, WEDGE_REQUIRED_STRIKES):
        acc = record_wedge_strike(acc, T0 + i * WEDGE_MIN_SPREAD_SEC, True)
    assert wedge_strikes_satisfied(acc) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
