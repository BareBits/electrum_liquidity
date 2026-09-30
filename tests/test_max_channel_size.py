"""Unit tests for the max-channel-size ceiling (``max_channel_size_sat``).

The open rule funds a channel with everything on-chain bar the reserve, which on a
wallet holding real money means committing the whole balance to one channel with
one peer. The ceiling bounds a single open, so a large balance builds several
channels of a sane size instead.

The ceiling is applied through ONE function, :func:`channel_funding_sat`, shared by
the open rule and the undersized-channel replacement rule. That sharing is the
point: a ceiling below the liquidity goal would otherwise let the close rule fire
on funds the open rule was never going to spend, reopen at the ceiling -- still
under the goal -- and close the same channel again next tick, paying a mining fee
every round. The churn tests below are the ones that pin that shut.

All pure engine; no Electrum import required.
"""
from __future__ import annotations

from typing import List

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ChannelSnapshot,
    CloseChannelAction,
    DeclineRecord,
    LiquidityConfig,
    LiquiditySnapshot,
    MIN_FUNDING_SAT,
    OpenChannelAction,
    channel_funding_sat,
    evaluate,
)


GOAL = 500_000
CAP = 800_000              # above the goal: the shipped relationship
RESERVE = 10_000
# Far more on-chain than any ceiling under test, so "the balance" is never the
# thing limiting the open.
RICH_ONCHAIN = 50_000_000


def make_config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=60_000,
        onchain_reserve_sat=RESERVE,
        max_channels=2,
        max_swap_fee_pct=0.9,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
        liquidity_goal_sat=GOAL,
        max_channel_size_sat=CAP,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


def make_channel(**overrides) -> ChannelSnapshot:
    """A drained, undersized, plugin-opened, healthy channel -- the shape that
    qualifies for replacement."""
    base = dict(
        channel_id="aa" * 32,
        short_id="100x1x0",
        capacity_sat=200_000,          # below GOAL
        local_sat=0,                   # drained -> under both swap triggers
        remote_sat=200_000,
        spendable_local_sat=0,
        is_active=True,
        has_unsettled_htlcs=False,
        unsettled_is_swap=False,
        is_plugin_opened=True,
    )
    base.update(overrides)
    return ChannelSnapshot(**base)


def make_snapshot(channels=(), **overrides) -> LiquiditySnapshot:
    base = dict(
        onchain_spendable_sat=RICH_ONCHAIN,
        channels=tuple(channels),
        swap_percentage_fee=None,
        provider_max_reverse_sat=None,
        provider_min_amount_sat=None,
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


def opens(result) -> List[OpenChannelAction]:
    return [a for a in result.actions if isinstance(a, OpenChannelAction)]


def closes(result) -> List[CloseChannelAction]:
    return [a for a in result.actions if isinstance(a, CloseChannelAction)]


def declines(result, kind: str) -> List[DeclineRecord]:
    return [d for d in result.declines if d.kind == kind]


# --- channel_funding_sat: the arithmetic ----------------------------------
def test_the_ceiling_caps_the_funding_amount():
    got = channel_funding_sat(make_snapshot(), make_config())
    assert got == CAP


def test_a_ceiling_above_the_balance_is_inert():
    # The balance, not the ceiling, is what limits a small wallet.
    snap = make_snapshot(onchain_spendable_sat=300_000)
    got = channel_funding_sat(snap, make_config(max_channel_size_sat=10_000_000))
    assert got == 300_000 - RESERVE


def test_zero_means_no_ceiling():
    # 0 must not min() the amount down to nothing -- it is how the feature is
    # turned off, matching every other numeric knob in this plugin.
    got = channel_funding_sat(make_snapshot(), make_config(max_channel_size_sat=0))
    assert got == RICH_ONCHAIN - RESERVE


def test_a_negative_ceiling_is_read_as_off():
    # Not reachable through the settings tab, but a hand-edited config must not
    # turn into a negative funding amount.
    got = channel_funding_sat(make_snapshot(), make_config(max_channel_size_sat=-5))
    assert got == RICH_ONCHAIN - RESERVE


def test_the_reserve_is_deducted_before_the_ceiling_is_applied():
    # Both bounds bite, and the ceiling is the smaller one here -- so the answer
    # is the ceiling exactly, not the ceiling minus the reserve.
    snap = make_snapshot(onchain_spendable_sat=CAP + RESERVE + 1)
    assert channel_funding_sat(snap, make_config()) == CAP


def test_a_reserve_larger_than_the_balance_stays_negative():
    # Deliberately NOT floored at 0: callers test the result against the funding
    # floor, and a negative number can never clear it. Flooring would invent a
    # fundable-looking 0.
    snap = make_snapshot(onchain_spendable_sat=1_000)
    got = channel_funding_sat(snap, make_config(onchain_reserve_sat=50_000))
    assert got < 0


# --- the open rule --------------------------------------------------------
def test_the_open_is_funded_at_the_ceiling_not_the_balance():
    result = evaluate(make_snapshot(), make_config())
    acts = opens(result)
    assert len(acts) == 1
    assert acts[0].funding_sat == CAP


def test_the_open_reason_says_the_ceiling_set_the_size():
    # Without this the decision log records a channel visibly smaller than the
    # balance with no explanation, and the leftover on-chain reads as a failure
    # to use the funds rather than as the next channel's funding.
    acts = opens(evaluate(make_snapshot(), make_config()))
    assert "capped" in acts[0].reason
    assert str(CAP) in acts[0].reason


def test_the_open_reason_is_silent_about_a_ceiling_that_did_not_bind():
    snap = make_snapshot(onchain_spendable_sat=300_000)
    acts = opens(evaluate(snap, make_config(max_channel_size_sat=10_000_000)))
    assert len(acts) == 1
    assert "capped" not in acts[0].reason


def test_a_large_balance_still_builds_out_to_the_channel_ceiling():
    # The point of the feature: the leftover balance is not stranded, it funds the
    # NEXT channel on a later tick, up to max_channels.
    one_channel = make_channel(capacity_sat=CAP, remote_sat=CAP)
    result = evaluate(make_snapshot([one_channel]), make_config(max_channels=3))
    acts = opens(result)
    assert len(acts) == 1
    assert acts[0].funding_sat == CAP


def test_an_existing_channel_over_the_ceiling_is_never_closed_for_it():
    # The ceiling bounds new opens. Closing an oversized channel would be the
    # plugin force-resizing something (possibly user-opened) it was never asked
    # to touch, at the cost of a mining fee.
    oversized = make_channel(capacity_sat=CAP * 10, remote_sat=CAP * 10)
    result = evaluate(make_snapshot([oversized]), make_config(max_channels=1))
    assert not closes(result)


# --- a ceiling below the funding floor: permanently inert -----------------
def test_a_ceiling_below_the_funding_floor_declines_and_names_itself():
    # No channel can ever be opened at this ceiling. Left to the generic
    # below-the-floor decline underneath, the log would blame the reserve for a
    # state only this setting can cause.
    result = evaluate(make_snapshot(), make_config(max_channel_size_sat=MIN_FUNDING_SAT - 1))
    assert not opens(result)
    open_declines = declines(result, "open")
    assert len(open_declines) == 1
    assert "max channel size" in open_declines[0].reason
    assert str(MIN_FUNDING_SAT) in open_declines[0].reason


def test_a_ceiling_exactly_at_the_funding_floor_is_allowed():
    # The floor is inclusive in Electrum (`funding_sat < MIN_FUNDING_SAT` fails),
    # so the smallest legal ceiling must still open a channel.
    result = evaluate(make_snapshot(), make_config(max_channel_size_sat=MIN_FUNDING_SAT))
    acts = opens(result)
    assert len(acts) == 1
    assert acts[0].funding_sat == MIN_FUNDING_SAT


def test_a_short_balance_still_blames_the_reserve_not_the_ceiling():
    # The below-the-floor decline quotes "on-chain minus reserve = funding_sat".
    # That arithmetic is only honest while the ceiling is not what truncated the
    # amount -- pin that the ceiling check above it keeps it so.
    snap = make_snapshot(onchain_spendable_sat=MIN_FUNDING_SAT + RESERVE - 1)
    result = evaluate(snap, make_config())
    assert not opens(result)
    open_declines = declines(result, "open")
    assert len(open_declines) == 1
    assert "max channel size" not in open_declines[0].reason
    assert "funding floor" in open_declines[0].reason
    assert open_declines[0].amount_sat == MIN_FUNDING_SAT - 1


# --- the churn guard: a ceiling below the liquidity goal ------------------
def at_ceiling(**chan_overrides) -> LiquiditySnapshot:
    """Two channels (= the default max_channels): one replaceable undersized
    candidate plus a healthy big one, so we are exactly at the channel ceiling."""
    candidate = make_channel(**chan_overrides)
    big = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                       capacity_sat=2_000_000, remote_sat=2_000_000)
    return make_snapshot([candidate, big])


def test_a_ceiling_below_the_goal_does_not_close_anything():
    # THE churn guard. The funds are there, we are at the ceiling, and the
    # candidate is idle and undersized -- every gate but this one says "replace
    # it". Replacing it would reopen at the ceiling, still under the goal, and
    # close it again on the next tick, forever, paying a mining fee each round.
    config = make_config(max_channel_size_sat=GOAL - 1)
    result = evaluate(at_ceiling(), config)
    assert not closes(result)


def test_a_ceiling_below_the_goal_explains_itself():
    # Waiting never resolves this, so unlike being short of funds it must not be
    # silent: the decision log is the only place a stuck wallet can be diagnosed.
    config = make_config(max_channel_size_sat=GOAL - 1)
    close_declines = declines(evaluate(at_ceiling(), config), "close")
    assert len(close_declines) == 1
    reason = close_declines[0].reason
    assert "max channel size" in reason
    assert str(GOAL) in reason
    assert close_declines[0].amount_sat == GOAL - 1
    # The knock-on a user will actually notice: the goal can never be met, so a
    # deferred liquidity sink stays deferred.
    assert "sink" in (close_declines[0].detail or "")


def test_being_short_of_funds_stays_silent_even_with_a_low_ceiling():
    # Distinguished from the case above on purpose. Here waiting IS the answer,
    # and a decline every tick would be noise.
    config = make_config(max_channel_size_sat=GOAL - 1)
    snap = make_snapshot(
        [make_channel(),
         make_channel(channel_id="bb" * 32, short_id="200x1x0",
                      capacity_sat=2_000_000, remote_sat=2_000_000)],
        onchain_spendable_sat=100_000)
    result = evaluate(snap, config)
    assert not closes(result)
    assert not declines(result, "close")


def test_a_ceiling_at_or_above_the_goal_replaces_as_before():
    # Regression guard for the shipped relationship: the ceiling must not break
    # the replacement rule it is only meant to bound.
    for cap in (GOAL, GOAL + 1, CAP):
        result = evaluate(at_ceiling(), make_config(max_channel_size_sat=cap))
        acts = closes(result)
        assert len(acts) == 1, f"cap={cap}"
        assert acts[0].channel_id == "aa" * 32


def test_no_ceiling_replaces_as_before():
    result = evaluate(at_ceiling(), make_config(max_channel_size_sat=0))
    assert len(closes(result)) == 1


def test_the_goal_conflict_decline_needs_something_undersized_to_replace():
    # Gate 5 is tested before gate 3 so this decline is a genuine near miss. With
    # every channel already at or above the goal there is nothing being blocked,
    # so a complaint about the ceiling would be pure noise.
    big = make_channel(capacity_sat=GOAL, remote_sat=GOAL)
    other = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                         capacity_sat=2_000_000, remote_sat=2_000_000)
    config = make_config(max_channel_size_sat=GOAL - 1)
    result = evaluate(make_snapshot([big, other]), config)
    assert not declines(result, "close")


def test_the_goal_conflict_decline_has_a_stable_dedup_signature():
    # Declines dedup on `reason`, so a value that drifts with every block would
    # re-log this steady state on every tick. Only stable config numbers may
    # appear in it; the drifting arithmetic belongs in `detail`.
    config = make_config(max_channel_size_sat=GOAL - 1)
    first = declines(evaluate(at_ceiling(), config), "close")[0]
    later = declines(
        evaluate(make_snapshot(
            [make_channel(),
             make_channel(channel_id="bb" * 32, short_id="200x1x0",
                          capacity_sat=2_000_000, remote_sat=2_000_000)],
            onchain_spendable_sat=RICH_ONCHAIN + 123_456), config), "close")[0]
    assert first.reason == later.reason
    assert first.detail != later.detail


# --- the two rules agree --------------------------------------------------
@pytest.mark.parametrize("cap", [0, MIN_FUNDING_SAT, GOAL, CAP, 10_000_000])
def test_both_rules_size_a_replacement_the_same_way(cap):
    """The close rule's "could we fund a real replacement?" test and the amount
    the open rule would actually use must be the same number. Any divergence is
    what churn is made of, so it is asserted directly rather than inferred."""
    config = make_config(max_channel_size_sat=cap)
    snap = make_snapshot()
    sized = channel_funding_sat(snap, config)
    acts = opens(evaluate(snap, config))
    assert len(acts) == 1
    assert acts[0].funding_sat == sized
