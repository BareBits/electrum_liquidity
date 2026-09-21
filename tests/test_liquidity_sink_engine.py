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
