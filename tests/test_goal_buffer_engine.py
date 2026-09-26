"""Tests for the liquidity-goal BUFFER arithmetic in the pure rules engine.

The buffer is the display/advisory side of the liquidity goal: the sats the user
means to keep so the goal stays reachable. Three questions are answered here and
nowhere else:

  * how big is the buffer (:func:`goal_buffer_sat`),
  * how is it carved out of the balance for the pie chart
    (:func:`split_goal_buffer`), and
  * does the amount someone is typing into the Send tab eat into it
    (:func:`send_dips_into_buffer`).

All of it is pure, so none of this needs Electrum or Qt. The edges are where the
bugs would be -- a buffer bigger than the wallet, an empty amount field, a goal
of 0 -- so that is what most of these cover.
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore
    BufferSplit,
    LiquidityConfig,
    goal_buffer_sat,
    send_dips_into_buffer,
    spendable_before_buffer_sat,
    split_goal_buffer,
)


def make_config(**overrides) -> LiquidityConfig:
    """A config whose only interesting field is the goal -- everything else is
    the engine's required scaffolding and is never read by the buffer path."""
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=60_000,
        onchain_reserve_sat=10_000,
        max_channels=2,
        max_swap_fee_pct=0.9,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
        liquidity_goal_sat=100_000,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


# --- how big is the buffer ------------------------------------------------
def test_buffer_is_the_configured_goal() -> None:
    assert goal_buffer_sat(make_config(liquidity_goal_sat=100_000)) == 100_000


def test_a_goal_of_zero_means_no_buffer() -> None:
    """A goal of 0 is how the feature is switched off: no slice, no warning."""
    assert goal_buffer_sat(make_config(liquidity_goal_sat=0)) == 0


def test_a_negative_goal_reads_as_no_buffer() -> None:
    """Hand-edited config. Never a negative reserve, which would invert the
    send threshold into "warn about nothing"."""
    assert goal_buffer_sat(make_config(liquidity_goal_sat=-5_000)) == 0


def test_the_buffer_is_held_even_once_the_goal_is_met() -> None:
    """Deliberate: the buffer does NOT shrink as channels get built.

    ``liquidity_goal_met`` going true means the build-out is done *for now* -- a
    channel closing, or the user raising the goal, puts the wallet straight back
    into build-out. A reserve that evaporated at exactly the moment it looked
    unnecessary would be spent by then. Nothing in the buffer path consults the
    snapshot at all, which is what this asserts: the function takes no snapshot,
    so there is no wallet state that could change its answer.
    """
    assert goal_buffer_sat(make_config(liquidity_goal_sat=100_000)) == 100_000
    with pytest.raises(TypeError):
        goal_buffer_sat(make_config(), object())  # type: ignore[call-arg]


# --- carving it out of the balance ----------------------------------------
def test_buffer_comes_out_of_onchain_first() -> None:
    """The headline case from the feature request: 100 on-chain, buffer 10 ->
    90 on-chain + 10 buffer."""
    split = split_goal_buffer(onchain_sat=100, lightning_sat=0, buffer_sat=10)
    assert split == BufferSplit(from_onchain=10, from_lightning=0,
                                onchain_remaining=90, lightning_remaining=0)
    assert split.total() == 10


def test_lightning_is_untouched_while_onchain_can_cover_it() -> None:
    split = split_goal_buffer(onchain_sat=500_000, lightning_sat=300_000,
                              buffer_sat=100_000)
    assert split.from_onchain == 100_000
    assert split.from_lightning == 0
    assert split.onchain_remaining == 400_000
    assert split.lightning_remaining == 300_000


def test_buffer_spills_into_lightning_when_onchain_falls_short() -> None:
    """30k on-chain against a 100k goal: on-chain is emptied into the buffer and
    the remaining 70k is taken from Lightning."""
    split = split_goal_buffer(onchain_sat=30_000, lightning_sat=500_000,
                              buffer_sat=100_000)
    assert split.from_onchain == 30_000
    assert split.from_lightning == 70_000
    assert split.onchain_remaining == 0
    assert split.lightning_remaining == 430_000
    assert split.total() == 100_000


def test_buffer_is_capped_by_the_whole_balance() -> None:
    """A wallet holding less than its goal is a normal state (a fresh wallet),
    not an error. The slice is the whole balance and nothing goes negative."""
    split = split_goal_buffer(onchain_sat=1_000, lightning_sat=2_000,
                              buffer_sat=100_000)
    assert split.total() == 3_000
    assert split.onchain_remaining == 0
    assert split.lightning_remaining == 0


def test_split_conserves_every_satoshi() -> None:
    """The pie must not gain or lose sats just because a slice was split off it:
    whatever went in has to come back out across the four fields."""
    for onchain, lightning, buffer_sat in [
        (100, 0, 10), (0, 100, 10), (30_000, 500_000, 100_000),
        (1, 1, 1), (0, 0, 100_000), (999_999, 1, 1_000_000),
    ]:
        split = split_goal_buffer(onchain_sat=onchain, lightning_sat=lightning,
                                  buffer_sat=buffer_sat)
        assert split.from_onchain + split.onchain_remaining == onchain
        assert split.from_lightning + split.lightning_remaining == lightning


def test_no_buffer_leaves_the_balance_alone() -> None:
    split = split_goal_buffer(onchain_sat=100, lightning_sat=50, buffer_sat=0)
    assert split.total() == 0
    assert split.onchain_remaining == 100
    assert split.lightning_remaining == 50


def test_negative_inputs_are_floored_rather_than_propagated() -> None:
    """Electrum's own piechart doc says it returns only positive values, but a
    negative slice here would paint a negative-angle wedge -- so it is floored
    rather than trusted."""
    split = split_goal_buffer(onchain_sat=-5, lightning_sat=-5, buffer_sat=10)
    assert split == BufferSplit(from_onchain=0, from_lightning=0,
                                onchain_remaining=0, lightning_remaining=0)


# --- the send threshold ---------------------------------------------------
def test_spendable_is_total_minus_buffer() -> None:
    assert spendable_before_buffer_sat(1_000_000, 100_000) == 900_000


def test_spendable_floors_at_zero_when_the_buffer_exceeds_the_balance() -> None:
    """Never a negative threshold: with the whole balance spoken for, every
    amount dips in, which is the truthful answer."""
    assert spendable_before_buffer_sat(50_000, 100_000) == 0


@pytest.mark.parametrize("amount,expected", [
    (899_999, False),   # comfortably under
    (900_000, False),   # exactly the threshold: spends down TO the buffer, not into it
    (900_001, True),    # one sat over
    (1_000_000, True),  # the whole balance
])
def test_warning_fires_only_strictly_above_the_threshold(amount, expected) -> None:
    """The boundary is ``>``, not ``>=``: sending exactly the spendable amount
    leaves the buffer intact, so warning about it would be wrong."""
    assert send_dips_into_buffer(amount_sat=amount, total_balance_sat=1_000_000,
                                 buffer_sat=100_000) is expected


def test_empty_amount_field_never_warns() -> None:
    """``None`` is an empty or unparseable amount box -- not a send."""
    assert send_dips_into_buffer(amount_sat=None, total_balance_sat=1_000_000,
                                 buffer_sat=100_000) is False


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amounts_never_warn(amount) -> None:
    assert send_dips_into_buffer(amount_sat=amount, total_balance_sat=1_000_000,
                                 buffer_sat=100_000) is False


def test_no_goal_means_no_warning_however_large_the_send() -> None:
    """A goal of 0 switches the whole feature off, including for a send that
    empties the wallet."""
    assert send_dips_into_buffer(amount_sat=1_000_000, total_balance_sat=1_000_000,
                                 buffer_sat=0) is False


def test_every_send_warns_once_the_buffer_covers_the_whole_balance() -> None:
    """A wallet funded with less than its goal: there is no headroom at all, so
    even a 1-sat send dips in. Noisy by design -- the alternative is silently
    not defending the goal exactly when it is furthest away."""
    assert send_dips_into_buffer(amount_sat=1, total_balance_sat=50_000,
                                 buffer_sat=100_000) is True


def test_an_empty_wallet_still_warns_on_a_positive_amount() -> None:
    assert send_dips_into_buffer(amount_sat=1, total_balance_sat=0,
                                 buffer_sat=100_000) is True
