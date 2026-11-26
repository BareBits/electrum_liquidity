"""Property-based tests for the pure rules engine.

The engine is 939 statements at ~99% line coverage and every one of those tests
is example-based. That combination is worth being suspicious of: line coverage
says each branch ran at least once with the numbers somebody thought to write
down, which is a different claim from "the function's stated invariant holds".
Several of those invariants are written out in the docstrings -- "the four
numbers always re-add to exactly the balances that went in", "descending and
deduplicated", "a finite float in [0, max]" -- and each is a property, so it
can be checked over inputs nobody chose.

Scope is deliberately the arithmetic and the clamps, not ``decide()``. A
property over the whole decision would need a generator for
``LiquiditySnapshot`` faithful enough that a counter-example meant something,
and an unfaithful one produces failures that are only ever bugs in the
generator. The functions here each have a short, exactly stated contract, which
is what makes a counter-example a real finding.

Determinism: the Hypothesis profile in ``conftest.py`` is derandomized, so
these behave like regression tests rather than a lottery. See the note there.
"""
from __future__ import annotations

import math
from typing import List

import pytest

pytest.importorskip("hypothesis")

from hypothesis import assume, given, strategies as st  # noqa: E402

from liquidity_manager import (  # type: ignore  # noqa: E402
    SCRUB_MAX_LEN,
    SINK_MAX_ATTEMPTS,
    SINK_MIN_PAYMENT_SAT,
    SWAP_LADDER_FACTORS,
    clamp_pct,
    clamp_sat,
    compute_dev_fee,
    goal_buffer_sat,
    scrub_text,
    send_dips_into_buffer,
    sink_attempt_amounts,
    spendable_before_buffer_sat,
    split_goal_buffer,
    swap_ladder_amounts,
    swap_cost_sat,
)

# Realistic sat magnitudes, plus the degenerate ends.
sats = st.integers(min_value=0, max_value=21_000_000 * 100_000_000)
modest_sats = st.integers(min_value=0, max_value=10_000_000_000)


# --- split_goal_buffer: the pie chart must not gain or lose satoshis -------
@given(onchain=modest_sats, lightning=modest_sats, buffer=modest_sats)
def test_the_buffer_split_conserves_every_satoshi(onchain, lightning, buffer) -> None:
    """The invariant the docstring states outright. This is a DISPLAY function
    -- it decides the blue slice of the balance pie -- so a split that did not
    re-add would show the user a wallet with more or less money in it than they
    have."""
    split = split_goal_buffer(onchain_sat=onchain, lightning_sat=lightning,
                              buffer_sat=buffer)
    assert split.from_onchain + split.onchain_remaining == onchain
    assert split.from_lightning + split.lightning_remaining == lightning
    assert split.total() == split.from_onchain + split.from_lightning


@given(onchain=modest_sats, lightning=modest_sats, buffer=modest_sats)
def test_the_buffer_split_never_shows_a_negative_slice(onchain, lightning, buffer) -> None:
    split = split_goal_buffer(onchain_sat=onchain, lightning_sat=lightning,
                              buffer_sat=buffer)
    for name, value in vars(split).items():
        assert value >= 0, f"{name} is negative: {value}"


@given(onchain=modest_sats, lightning=modest_sats, buffer=modest_sats)
def test_the_buffer_never_exceeds_the_balance_backing_it(onchain, lightning, buffer) -> None:
    """A buffer larger than the whole wallet is a normal state (a fresh wallet
    funded with less than its goal), and must present as "all of it is spoken
    for" rather than as a slice bigger than the pie."""
    split = split_goal_buffer(onchain_sat=onchain, lightning_sat=lightning,
                              buffer_sat=buffer)
    assert split.total() <= onchain + lightning
    assert split.total() <= buffer


@given(onchain=modest_sats, lightning=modest_sats, buffer=modest_sats)
def test_the_buffer_takes_from_onchain_first(onchain, lightning, buffer) -> None:
    """On-chain coins are what fund a channel open, so they are what the goal
    actually needs; Lightning is only drawn on for the remainder. Equivalent
    statement: Lightning contributes nothing until on-chain is exhausted."""
    split = split_goal_buffer(onchain_sat=onchain, lightning_sat=lightning,
                              buffer_sat=buffer)
    if split.from_lightning > 0:
        assert split.onchain_remaining == 0, (
            "the buffer spilled into Lightning while on-chain coins were still "
            "available to back it")


# --- the send warning -----------------------------------------------------
@given(total=modest_sats, buffer=modest_sats)
def test_spendable_before_buffer_is_never_negative(total, buffer) -> None:
    """Once the buffer is at or above the whole balance there is no headroom,
    so every amount dips in. 0 is the truthful threshold; a negative one would
    make the comparison in `send_dips_into_buffer` read backwards."""
    out = spendable_before_buffer_sat(total, buffer)
    assert out >= 0
    if buffer <= total:
        assert out == total - buffer


@given(total=modest_sats, buffer=st.integers(min_value=1, max_value=10_000_000_000),
       amount=st.integers(min_value=1, max_value=10_000_000_000),
       extra=st.integers(min_value=1, max_value=10_000_000_000))
def test_the_send_warning_is_monotone_in_the_amount(total, buffer, amount, extra) -> None:
    """If sending X dips into the buffer then sending more than X must too.

    A non-monotone warning is the version of this feature that trains users to
    ignore it: the warning would appear at one amount and vanish at a larger
    one, which reads as a bug in the plugin rather than as a fact about their
    balance.
    """
    if send_dips_into_buffer(amount_sat=amount, total_balance_sat=total,
                             buffer_sat=buffer):
        assert send_dips_into_buffer(amount_sat=amount + extra,
                                     total_balance_sat=total,
                                     buffer_sat=buffer), (
            f"sending {amount} warned but {amount + extra} did not")


@given(total=modest_sats, amount=st.integers(min_value=1, max_value=10_000_000_000))
def test_no_goal_means_no_warning_ever(total, amount) -> None:
    """A goal of 0 is how the whole feature is switched off."""
    assert send_dips_into_buffer(amount_sat=amount, total_balance_sat=total,
                                 buffer_sat=0) is False


# --- the swap ladder ------------------------------------------------------
@given(desired=st.integers(min_value=0, max_value=10_000_000_000))
def test_the_swap_ladder_is_strictly_descending_and_bounded(desired) -> None:
    """Descending and deduplicated, as the docstring states. Deduplication
    matters because rounding collapses two factors onto the same integer on
    small amounts, and a repeated rung spends a whole cascade attempt
    re-running an identical payment."""
    rungs = swap_ladder_amounts(desired)
    assert list(rungs) == sorted(rungs, reverse=True)
    assert len(set(rungs)) == len(rungs), f"a rung repeats: {rungs}"
    assert all(r > 0 for r in rungs)
    assert all(r <= desired for r in rungs)
    assert len(rungs) <= len(SWAP_LADDER_FACTORS)


@given(desired=st.integers(min_value=1, max_value=10_000_000_000),
       # NaN and the infinities have to be injected explicitly: Hypothesis
       # refuses allow_nan together with min_value/max_value, and these are
       # exactly the values the function's range test exists to exclude.
       factors=st.lists(st.one_of(
           st.floats(min_value=-10.0, max_value=10.0, allow_nan=False,
                     allow_infinity=False),
           st.sampled_from([float("nan"), float("inf"), float("-inf"),
                            0.0, 1.0, -1.0]),
       ), max_size=8))
def test_a_nonsense_ladder_factor_costs_that_rung_and_nothing_else(desired, factors) -> None:
    """The factors are user-reachable via config in principle, so a NaN, an
    infinity, a zero or a negative must be skipped rather than raise -- losing
    that rung, not the whole swap. NaN is the interesting one: every comparison
    with it is False, so it has to be excluded by the range test rather than
    detected separately."""
    rungs = swap_ladder_amounts(desired, factors=factors)
    assert list(rungs) == sorted(rungs, reverse=True)
    assert all(0 < r <= desired for r in rungs)
    usable = [f for f in factors if not math.isnan(f) and 0.0 < f <= 1.0]
    assert len(rungs) <= len(usable)


# --- the sink ladder ------------------------------------------------------
@given(desired=st.integers(min_value=0, max_value=10_000_000_000))
def test_the_sink_ladder_stays_within_its_bounds(desired) -> None:
    """Descending, never below the floor the feature is willing to send, never
    above what was asked for, and never longer than the attempt cap."""
    rungs = sink_attempt_amounts(desired)
    assert list(rungs) == sorted(rungs, reverse=True)
    assert len(rungs) <= SINK_MAX_ATTEMPTS
    assert all(r >= SINK_MIN_PAYMENT_SAT for r in rungs), rungs
    assert all(r <= desired for r in rungs), rungs


@given(desired=st.integers(min_value=0, max_value=10_000_000_000))
def test_the_sink_ladder_is_empty_exactly_below_the_floor(desired) -> None:
    """"Nothing to send" and "send the floor" are different answers, and which
    one applies is decided purely by the floor."""
    rungs = sink_attempt_amounts(desired)
    assert bool(rungs) == (desired >= SINK_MIN_PAYMENT_SAT)


@given(desired=st.integers(min_value=SINK_MIN_PAYMENT_SAT,
                           max_value=10_000_000_000))
def test_the_sink_ladder_always_ends_on_the_floor(desired) -> None:
    """The clamped final rung. A ladder that stopped at 12_500 because halving
    would undershoot leaves a drainable channel undrained -- 12_500 failing
    says nothing about 10_000 -- so the last rung is the floor itself unless
    the attempt cap ran out first."""
    rungs = sink_attempt_amounts(desired)
    if len(rungs) < SINK_MAX_ATTEMPTS:
        assert rungs[-1] == SINK_MIN_PAYMENT_SAT, (
            f"ladder for {desired} ended on {rungs[-1]}, not the floor: {rungs}")


# --- the clamps: arbitrary garbage in, a valid setting out ----------------
garbage = st.one_of(
    st.none(), st.booleans(), st.text(max_size=30),
    st.integers(min_value=-(2 ** 70), max_value=2 ** 70),
    st.floats(allow_nan=True, allow_infinity=True),
    st.lists(st.integers(), max_size=3), st.dictionaries(st.text(), st.text(),
                                                         max_size=2),
)


@given(value=garbage)
def test_clamp_sat_always_returns_a_usable_sat_amount(value) -> None:
    """These read hand-editable wallet-file values, so the input really is
    arbitrary. The contract is that one unusable setting costs that setting and
    never the whole evaluation -- so this must not raise, for anything."""
    out = clamp_sat(value)
    assert isinstance(out, int)
    assert 0 <= out <= 21_000_000 * 100_000_000


@given(value=garbage)
def test_clamp_pct_always_returns_a_finite_percentage(value) -> None:
    """NaN is the case worth a property test: it is rejected to the default
    rather than clamped, because every comparison with NaN is False, so a NaN
    ceiling reads as whichever way the surrounding `if` happens to be written
    -- "no limit" or "no swaps" depending on the caller."""
    out = clamp_pct(value)
    assert isinstance(out, float)
    assert math.isfinite(out), f"{value!r} produced {out!r}"
    assert 0.0 <= out <= 100.0


@given(value=garbage)
def test_scrub_text_is_bounded_and_single_line(value) -> None:
    """Scrubbing is what makes the decision log safe to render and to write to
    disk, and its inputs include text an outside party chose (a swap provider's
    error message). Length and the absence of control characters are the two
    properties the renderers depend on."""
    out = scrub_text(value)
    assert isinstance(out, str)
    assert len(out) <= SCRUB_MAX_LEN, f"{len(out)} chars from {value!r}"
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert not any(ord(ch) < 0x20 for ch in out), repr(out)


# --- fees -----------------------------------------------------------------
@given(basis=modest_sats,
       pct=st.floats(min_value=0.0, max_value=100.0, allow_nan=False,
                     allow_infinity=False))
def test_the_dev_fee_is_never_negative_and_never_exceeds_the_basis(basis, pct) -> None:
    """It is charged out of the amount swapped, so a fee above the basis would
    be charging more than moved."""
    fee = compute_dev_fee(basis, pct)
    assert isinstance(fee, int)
    assert 0 <= fee <= basis


@given(pct=st.floats(min_value=0.0, max_value=50.0, allow_nan=False,
                     allow_infinity=False),
       mining=st.integers(min_value=0, max_value=1_000_000),
       claim=st.integers(min_value=0, max_value=1_000_000),
       amount=st.integers(min_value=1, max_value=10_000_000_000),
       extra=st.integers(min_value=1, max_value=1_000_000))
def test_a_bigger_swap_never_costs_less_in_absolute_terms(pct, mining, claim,
                                                          amount, extra) -> None:
    """The percentage leg grows with the amount and the two fixed legs do not
    shrink, so total cost is monotone non-decreasing in the amount.

    This is what makes the ladder meaningful: the engine walks DOWN the rungs
    expecting each smaller size to be no more expensive, and `rank_swap_rungs`
    orders by cost-per-sat on that basis.
    """
    low = swap_cost_sat(pct, mining, claim, amount)
    high = swap_cost_sat(pct, mining, claim, amount + extra)
    assert high >= low, f"{amount + extra} cost {high} but {amount} cost {low}"


@given(goal=st.integers(min_value=-(2 ** 40), max_value=2 ** 40))
def test_the_goal_buffer_is_never_negative(goal) -> None:
    """Held unconditionally at the configured goal, floored at 0 -- a negative
    goal in a hand-edited wallet file must read as "feature off", not as a
    negative reserve that would make every send look safe."""
    from types import SimpleNamespace
    out = goal_buffer_sat(SimpleNamespace(liquidity_goal_sat=goal))
    assert out >= 0
    assert out == max(0, goal)
