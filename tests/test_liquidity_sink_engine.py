"""Unit tests for the liquidity sink in the pure rules engine.

The sink is the second way to drain a channel: instead of reverse-swapping
outbound on-chain, pay it to a Lightning address. Liquidity-wise the two are
identical -- local balance leaves the channel, inbound capacity comes back -- so
the sink plugs into exactly the same triggers and guards as a swap, and the only
genuinely new pure logic is:

  * the halving amount ladder (:func:`sink_attempt_amounts`), including the
    clamped final rung at the floor, and
  * which of the two drains the engine plans, given a sink address and the
    "disable submarine swaps" switch.

Everything the swap path already guarded -- the triggers, activity, unsettled
HTLCs, plugin-opened scope, the outbound floor -- must guard the sink too, and
that is asserted here rather than assumed. No Electrum import required.
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    LiquiditySinkAction,
    ReverseSwapAction,
    SINK_MAX_ATTEMPTS,
    SINK_MIN_PAYMENT_SAT,
    evaluate,
    liquidity_goal_met,
    sink_attempt_amounts,
)

from test_provider_selection import (  # type: ignore
    make_channel,
    make_config,
    make_offer,
    make_snapshot,
)

SINK = "alice@example.com"


def _sinks(result) -> list:
    return [a for a in result.actions if isinstance(a, LiquiditySinkAction)]


def _swaps(result) -> list:
    return [a for a in result.actions if isinstance(a, ReverseSwapAction)]


def _reasons(result) -> str:
    return " | ".join(d.reason for d in result.declines)


# --- the halving ladder ---------------------------------------------------
def test_ladder_halves_and_ends_on_a_clamped_floor_rung() -> None:
    # The worked example from the spec: halve until the next rung would fall
    # below the floor, then make one final attempt at exactly the floor.
    assert sink_attempt_amounts(200_000) == (
        200_000, 100_000, 50_000, 25_000, 12_500, 10_000)


def test_ladder_skips_the_clamp_when_it_would_repeat_the_last_rung() -> None:
    # 20_000 -> 10_000 already ends exactly on the floor; a clamped rung would
    # be the identical payment twice, which tells us nothing new.
    assert sink_attempt_amounts(20_000) == (20_000, 10_000)
    assert sink_attempt_amounts(10_000) == (10_000,)


def test_ladder_is_empty_below_the_floor() -> None:
    # Not "one attempt at the floor" -- we would be sending more than the channel
    # has to give. The caller falls back to a swap instead.
    assert sink_attempt_amounts(SINK_MIN_PAYMENT_SAT - 1) == ()
    assert sink_attempt_amounts(0) == ()
    assert sink_attempt_amounts(-5) == ()


def test_ladder_uses_integer_halves() -> None:
    assert sink_attempt_amounts(40_001) == (40_001, 20_000, 10_000)
    assert sink_attempt_amounts(15_000) == (15_000, 10_000)


def test_ladder_is_strictly_descending_and_bounded() -> None:
    for desired in (10_000, 10_001, 33_333, 999_999, 8_000_000):
        rungs = sink_attempt_amounts(desired)
        assert rungs[0] == desired
        assert list(rungs) == sorted(rungs, reverse=True)
        assert len(set(rungs)) == len(rungs)
        assert min(rungs) >= SINK_MIN_PAYMENT_SAT
        assert len(rungs) <= SINK_MAX_ATTEMPTS


def test_ladder_runaway_backstop_truncates_absurd_amounts() -> None:
    # A nonsense snapshot must not produce an unbounded list of payments.
    rungs = sink_attempt_amounts(10_000 * 2 ** 40)
    assert len(rungs) == SINK_MAX_ATTEMPTS


def test_ladder_floor_is_configurable_for_callers() -> None:
    assert sink_attempt_amounts(100_000, floor_sat=50_000) == (100_000, 50_000)
    assert sink_attempt_amounts(100_000, floor_sat=0) == ()


# --- which drain the engine plans -----------------------------------------
def test_sink_is_preferred_over_a_swap_and_carries_it_as_fallback() -> None:
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    assert not _swaps(result)                     # not planned as its own action
    [sink] = _sinks(result)
    assert sink.address == SINK
    assert sink.amounts == sink_attempt_amounts(900_000)
    assert sink.channel_id == "aa" * 32
    # The swap is fully planned and held in reserve, provider and all.
    assert isinstance(sink.swap_fallback, ReverseSwapAction)
    assert sink.swap_fallback.provider_npub == "npubA"
    assert "held in reserve" in sink.reason


def test_no_sink_configured_plans_a_swap_exactly_as_before() -> None:
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config())
    assert not _sinks(result)
    [swap] = _swaps(result)
    assert swap.lightning_amount_sat == 900_000


def test_sink_acts_with_no_provider_at_all() -> None:
    # The whole point of the sink: a channel that no swap provider could drain
    # (none discovered) is still drained. Before the sink this was a decline.
    snap = make_snapshot([])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    [sink] = _sinks(result)
    assert sink.swap_fallback is None
    assert "no swap provider available as a fallback" in sink.reason
    # ...and the swap's "no provider known" near miss is NOT logged: we acted.
    assert "no swap provider is known yet" not in _reasons(result)


def test_sink_acts_when_every_provider_is_over_the_cost_ceiling() -> None:
    # Providers exist but all are too dear. The sink has no cost gate, so it
    # drains the channel the swap path would have refused.
    snap = make_snapshot([make_offer("dear", pct=5.0)])
    result = evaluate(snap, make_config(max_swap_fee_pct=0.6,
                                        liquidity_sink_address=SINK))
    [sink] = _sinks(result)
    assert sink.swap_fallback is None
    assert not _swaps(result)


def test_disabling_swaps_plans_a_sink_with_no_fallback() -> None:
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        disable_submarine_swaps=True))
    [sink] = _sinks(result)
    assert sink.swap_fallback is None
    assert "submarine swaps are disabled" in sink.reason


def test_disabling_swaps_without_a_sink_declines_rather_than_swapping() -> None:
    # The switch is honoured literally. Quietly reverse-swapping because the sink
    # is empty would move funds on-chain through a third party after the user
    # explicitly asked for no swaps.
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(disable_submarine_swaps=True))
    assert not result.actions
    assert "submarine swaps are disabled and no liquidity sink is configured" \
        in _reasons(result)


def test_sink_only_declines_when_the_drain_is_below_the_floor() -> None:
    chan = make_channel(capacity_sat=40_000, local_sat=20_000,
                        spendable_local_sat=9_000)
    snap = make_snapshot([make_offer("npubA", pct=0.2)], channels=(chan,))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        disable_submarine_swaps=True))
    assert not result.actions
    assert f"below the {SINK_MIN_PAYMENT_SAT} sat liquidity-sink minimum" \
        in _reasons(result)


def test_a_sub_floor_drain_still_swaps_when_swaps_are_enabled() -> None:
    # The sink cannot help at this size, but a provider whose minimum is lower
    # still can -- the sink must not suppress the swap it cannot replace.
    chan = make_channel(capacity_sat=40_000, local_sat=20_000,
                        spendable_local_sat=9_000)
    snap = make_snapshot([make_offer("npubA", pct=0.2, lo=1_000)], channels=(chan,))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    assert not _sinks(result)
    [swap] = _swaps(result)
    assert swap.lightning_amount_sat == 9_000


def test_whitespace_only_sink_address_is_treated_as_unset() -> None:
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(liquidity_sink_address="   "))
    assert not _sinks(result)
    assert _swaps(result)


def test_sink_address_is_stored_trimmed_on_the_action() -> None:
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(liquidity_sink_address=f"  {SINK}\n"))
    [sink] = _sinks(result)
    assert sink.address == SINK


# --- the sink obeys every guard the swap path obeys -----------------------
def test_sink_respects_the_outbound_floor() -> None:
    # min_outbound_sat is deducted before the ladder is built, so the top rung
    # never drains past the floor the user set.
    snap = make_snapshot([])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        min_outbound_sat=400_000))
    [sink] = _sinks(result)
    assert sink.amounts[0] == 500_000


def test_sink_blocked_when_the_outbound_floor_covers_everything() -> None:
    snap = make_snapshot([])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        min_outbound_sat=900_000))
    assert not result.actions
    assert "outbound floor" in _reasons(result)


def test_sink_skips_inactive_channels() -> None:
    chan = make_channel(is_active=False)
    snap = make_snapshot([], channels=(chan,))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    assert not result.actions
    assert "not active" in _reasons(result)


def test_sink_skips_channels_with_unsettled_htlcs() -> None:
    chan = make_channel(has_unsettled_htlcs=True)
    snap = make_snapshot([], channels=(chan,))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    assert not result.actions
    assert "unsettled HTLCs" in _reasons(result)


def test_sink_respects_the_plugin_opened_only_scope() -> None:
    chan = make_channel(is_plugin_opened=False)
    snap = make_snapshot([], channels=(chan,))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        manage_plugin_opened_only=True))
    assert not result.actions
    assert "manage-plugin-opened-only is on" in _reasons(result)


def test_sink_skips_channels_under_both_triggers() -> None:
    chan = make_channel(capacity_sat=2_000_000, local_sat=10_000,
                        spendable_local_sat=9_000)
    snap = make_snapshot([], channels=(chan,))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        swap_trigger_sat=25_000))
    assert not result.actions
    assert not result.declines          # not a near miss, just nothing to do


def test_sink_is_not_planned_while_frozen() -> None:
    snap = make_snapshot([], inflight_swap_count=1)
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    assert not result.actions
    assert result.frozen


def test_automation_off_plans_nothing() -> None:
    snap = make_snapshot([])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        automation_enabled=False))
    assert not result.actions and not result.declines


# --- several channels in one pass -----------------------------------------
def test_each_over_trigger_channel_gets_its_own_sink_action() -> None:
    a = make_channel(channel_id="aa" * 32, short_id="100x1x0")
    b = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                     spendable_local_sat=300_000)
    snap = make_snapshot([], channels=(a, b))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    sinks = _sinks(result)
    assert [s.short_id for s in sinks] == ["100x1x0", "200x1x0"]
    assert sinks[0].amounts[0] == 900_000
    assert sinks[1].amounts[0] == 300_000


def test_a_reserved_swap_does_not_consume_provider_capacity() -> None:
    # Provider capacity is only charged against swaps we are actually making. A
    # swap held in reserve behind a sink almost certainly never runs, and
    # reserving for it would starve the second channel of its own fallback.
    offer = make_offer("npubA", pct=0.2, hi=1_000_000)
    a = make_channel(channel_id="aa" * 32, short_id="100x1x0",
                     spendable_local_sat=900_000)
    b = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                     spendable_local_sat=900_000)
    snap = make_snapshot([offer], channels=(a, b))
    result = evaluate(snap, make_config(liquidity_sink_address=SINK))
    sinks = _sinks(result)
    assert len(sinks) == 2
    assert all(s.swap_fallback is not None for s in sinks)
    assert [s.swap_fallback.lightning_amount_sat for s in sinks] == [900_000, 900_000]


# --- holding the sink back until the liquidity goal is met ----------------
# A sink payment leaves the wallet for good: it lands in whatever account the
# address belongs to, NOT on-chain here. While the wallet is still building its
# channels that starves the very balance the next (or bigger) channel is opened
# with, so `defer_sink_until_goal` (SHIPPED ON) swaps instead until the build-out
# is done -- a swap returns the same drain as on-chain coins.
#
# "Done" is `liquidity_goal_met`: at the channel ceiling, with no PLUGIN-OPENED
# channel still under the goal (those are the two things on-chain funds can buy).
def _deferred(**overrides):
    base = dict(liquidity_sink_address=SINK, defer_sink_until_goal=True,
                liquidity_goal_sat=1_000_000, max_channels=2)
    base.update(overrides)
    return make_config(**base)


def test_goal_is_met_when_the_ceiling_is_full_and_nothing_is_undersized() -> None:
    chans = (make_channel(channel_id="aa" * 32, capacity_sat=1_000_000),
             make_channel(channel_id="bb" * 32, capacity_sat=2_000_000))
    snap = make_snapshot([], channels=chans)
    assert liquidity_goal_met(snap, _deferred())


def test_goal_is_unmet_below_the_channel_ceiling() -> None:
    # Room for another channel: on-chain funds can still open one, so the
    # build-out is not done however big the existing channels are.
    snap = make_snapshot([], channels=(make_channel(capacity_sat=9_000_000),))
    assert not liquidity_goal_met(snap, _deferred())


def test_goal_is_unmet_while_a_plugin_channel_is_under_the_goal() -> None:
    # At the ceiling, but the small one is still replaceable by a bigger one --
    # which is exactly what on-chain balance is for.
    chans = (make_channel(channel_id="aa" * 32, capacity_sat=300_000),
             make_channel(channel_id="bb" * 32, capacity_sat=2_000_000))
    snap = make_snapshot([], channels=chans)
    assert not liquidity_goal_met(snap, _deferred())


def test_a_hand_opened_undersized_channel_does_not_hold_the_goal_open() -> None:
    # The plugin never closes a channel the user opened, so a small one would
    # otherwise keep the gate shut forever with no action able to open it.
    chans = (make_channel(channel_id="aa" * 32, capacity_sat=300_000,
                          is_plugin_opened=False),
             make_channel(channel_id="bb" * 32, capacity_sat=2_000_000))
    snap = make_snapshot([], channels=chans)
    assert liquidity_goal_met(snap, _deferred())


def test_goal_of_zero_is_always_met() -> None:
    # 0 disables the goal itself, and with it this gate -- there is no build-out
    # to wait for.
    snap = make_snapshot([], channels=(make_channel(capacity_sat=1_000),))
    assert liquidity_goal_met(snap, _deferred(liquidity_goal_sat=0))


def test_channels_are_counted_as_the_open_rule_counts_them() -> None:
    # Everything not yet closed, including one whose close is settling -- so the
    # two rules cannot disagree about whether there is room for another channel.
    chans = (make_channel(channel_id="aa" * 32, capacity_sat=1_000_000),
             make_channel(channel_id="bb" * 32, capacity_sat=1_000_000,
                          is_active=False))
    snap = make_snapshot([], channels=chans, closing_channel_count=1)
    assert liquidity_goal_met(snap, _deferred())


def test_deferred_sink_swaps_instead_and_says_why() -> None:
    # The whole feature: the drain still happens, but on-chain where it can fund
    # the next channel -- and the log explains the sink's absence.
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, _deferred())
    assert not _sinks(result)
    [swap] = _swaps(result)
    assert swap.lightning_amount_sat == 900_000
    assert "the liquidity sink is held back until the 1000000 sat liquidity goal" \
        in swap.reason
    # Acting on the channel, so the skipped sink is not also a near miss.
    assert not result.declines


def test_the_sink_takes_over_once_the_goal_is_met() -> None:
    chans = (make_channel(channel_id="aa" * 32, capacity_sat=1_000_000,
                          spendable_local_sat=900_000),
             make_channel(channel_id="bb" * 32, capacity_sat=1_000_000,
                          spendable_local_sat=0, local_sat=0,
                          remote_sat=1_000_000))
    snap = make_snapshot([make_offer("npubA", pct=0.2)], channels=chans)
    result = evaluate(snap, _deferred())
    [sink] = _sinks(result)
    assert sink.short_id == "100x1x0"
    assert sink.amounts == sink_attempt_amounts(900_000)
    assert "held back" not in sink.reason


def test_the_gate_is_off_by_default_in_the_engine() -> None:
    # The dataclass default preserves the pre-feature behaviour every existing
    # pure test was written against (the shipped ConfigVar default is the
    # opposite -- see the note on LiquidityConfig.defer_sink_until_goal).
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(liquidity_sink_address=SINK,
                                        liquidity_goal_sat=1_000_000))
    assert _sinks(result) and not _swaps(result)


def test_the_gate_does_nothing_without_a_sink() -> None:
    # Nothing to hold back: the swap is the only drain either way, and its reason
    # must not grow a note about a sink the user never configured.
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, make_config(defer_sink_until_goal=True,
                                        liquidity_goal_sat=1_000_000))
    [swap] = _swaps(result)
    assert "held back" not in swap.reason


def test_a_deferred_sink_is_not_used_when_no_swap_can_be_planned() -> None:
    # Strictly honoured: the sink stays held back even though it is the only
    # thing that COULD drain this channel. Paying the user's outbound away to a
    # third party is not a fallback for a missing provider -- the funds stay in
    # the channel, recoverable, and the decline says so.
    snap = make_snapshot([])
    result = evaluate(snap, _deferred())
    assert not result.actions
    [decline] = result.declines
    assert "no swap provider is known yet" in decline.reason
    # The gate rides in `detail`, so the decline keeps its existing dedup
    # signature and still reads as one steady-state row.
    assert "the liquidity sink is held back" in (decline.detail or "")


def test_a_deferred_sink_over_the_cost_ceiling_declines_too() -> None:
    snap = make_snapshot([make_offer("dear", pct=5.0)])
    result = evaluate(snap, _deferred(max_swap_fee_pct=0.6))
    assert not result.actions
    [decline] = result.declines
    assert "all-in cost" in decline.reason
    assert "the liquidity sink is held back" in (decline.detail or "")


def test_a_deferred_sink_with_swaps_disabled_declines_naming_both() -> None:
    # Both halves switched off by the user's own settings. Honoured rather than
    # second-guessed, but the decline names both switches so the log shows which
    # one to change.
    snap = make_snapshot([make_offer("npubA", pct=0.2)])
    result = evaluate(snap, _deferred(disable_submarine_swaps=True))
    assert not result.actions
    [decline] = result.declines
    assert "submarine swaps are disabled and the liquidity sink is held back" \
        in decline.reason
    assert decline.amount_sat == 900_000
    assert "the liquidity sink is held back" in (decline.detail or "")


def test_a_deferred_sink_still_respects_the_triggers() -> None:
    # The gate only chooses BETWEEN drains; a channel under both triggers is
    # still nothing to do, not a near miss.
    chan = make_channel(capacity_sat=2_000_000, local_sat=10_000,
                        spendable_local_sat=9_000)
    snap = make_snapshot([make_offer("npubA", pct=0.2)], channels=(chan,))
    result = evaluate(snap, _deferred())
    assert not result.actions and not result.declines
