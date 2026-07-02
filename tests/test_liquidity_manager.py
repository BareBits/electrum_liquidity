"""Unit tests for the pure inbound-liquidity rules engine.

These cover every rule the operator specified and the boundary conditions
around them. No Electrum import is required.
"""
from __future__ import annotations

from typing import List

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ChannelSnapshot,
    DeclineRecord,
    DecisionResult,
    LiquidityConfig,
    LiquiditySnapshot,
    MIN_FUNDING_SAT,
    OpenChannelAction,
    ReverseSwapAction,
    decide,
    effective_swap_cost_pct,
    evaluate,
)


def make_config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=1_000_000,
        onchain_reserve_sat=10_000,
        max_channels=2,
        max_swap_fee_pct=0.6,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


def make_channel(**overrides) -> ChannelSnapshot:
    base = dict(
        channel_id="aa" * 32,
        short_id="100x1x0",
        capacity_sat=2_000_000,
        local_sat=1_000,
        remote_sat=1_999_000,
        spendable_local_sat=1_000,
        is_active=True,
    )
    base.update(overrides)
    return ChannelSnapshot(**base)


def make_snapshot(**overrides) -> LiquiditySnapshot:
    base = dict(
        onchain_spendable_sat=0,
        channels=(),
        swap_percentage_fee=0.5,
        provider_max_reverse_sat=1_900_000,
        provider_min_amount_sat=20_000,
        # Default fixed fees to 0 so reverse-swap tests isolate the rules they
        # exercise; the effective-cost tests below set realistic fixed fees.
        swap_mining_fee_sat=0,
        swap_claim_fee_sat=0,
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


# --- master switch --------------------------------------------------------
def test_automation_disabled_yields_nothing() -> None:
    snap = make_snapshot(onchain_spendable_sat=5_000_000)
    assert decide(snap, make_config(automation_enabled=False)) == []


# --- channel opening ------------------------------------------------------
def test_opens_channel_with_max_minus_reserve() -> None:
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=())
    actions = decide(snap, make_config())
    opens = [a for a in actions if isinstance(a, OpenChannelAction)]
    assert len(opens) == 1
    assert opens[0].funding_sat == 5_000_000 - 10_000


def test_no_open_below_min_onchain() -> None:
    snap = make_snapshot(onchain_spendable_sat=999_999, channels=())
    assert not any(isinstance(a, OpenChannelAction) for a in decide(snap, make_config()))


def test_no_open_at_max_channels() -> None:
    chans = (make_channel(), make_channel())
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=chans)
    assert not any(isinstance(a, OpenChannelAction) for a in decide(snap, make_config()))


def test_no_open_when_funding_below_min_funding_floor() -> None:
    # Enough to pass the min-to-open gate, but not enough left after reserve to
    # clear Electrum's MIN_FUNDING_SAT.
    spendable = MIN_FUNDING_SAT + 10_000 - 1
    snap = make_snapshot(onchain_spendable_sat=spendable, channels=())
    cfg = make_config(min_onchain_to_open_sat=100_000, onchain_reserve_sat=10_000)
    assert not any(isinstance(a, OpenChannelAction) for a in decide(snap, cfg))


def test_only_one_channel_opened_per_cycle() -> None:
    snap = make_snapshot(onchain_spendable_sat=50_000_000, channels=())
    opens = [a for a in decide(snap, make_config()) if isinstance(a, OpenChannelAction)]
    assert len(opens) == 1


# --- reverse swaps --------------------------------------------------------
def test_reverse_swap_triggered_by_sat_threshold() -> None:
    chan = make_channel(local_sat=30_000, spendable_local_sat=29_000, capacity_sat=2_000_000)
    snap = make_snapshot(channels=(chan,))
    swaps = [a for a in decide(snap, make_config()) if isinstance(a, ReverseSwapAction)]
    assert len(swaps) == 1
    assert swaps[0].lightning_amount_sat == 29_000


def test_reverse_swap_triggered_by_pct_threshold() -> None:
    # 26% of capacity, but under the 25_000 sat trigger would not fire on sats;
    # raise the sat trigger so only the pct rule can fire.
    chan = make_channel(local_sat=260_000, spendable_local_sat=255_000, capacity_sat=1_000_000)
    snap = make_snapshot(channels=(chan,))
    cfg = make_config(swap_trigger_sat=10_000_000)
    swaps = [a for a in decide(snap, cfg) if isinstance(a, ReverseSwapAction)]
    assert len(swaps) == 1


def test_no_reverse_swap_below_both_triggers() -> None:
    chan = make_channel(local_sat=20_000, spendable_local_sat=20_000, capacity_sat=1_000_000)
    snap = make_snapshot(channels=(chan,))
    cfg = make_config(swap_trigger_pct=25.0, swap_trigger_sat=25_000)
    assert not any(isinstance(a, ReverseSwapAction) for a in decide(snap, cfg))


def test_no_reverse_swap_when_fee_too_high() -> None:
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(channels=(chan,), swap_percentage_fee=0.7)
    assert not any(isinstance(a, ReverseSwapAction) for a in decide(snap, make_config()))


def test_no_reverse_swap_when_fee_unknown() -> None:
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(channels=(chan,), swap_percentage_fee=None)
    assert not any(isinstance(a, ReverseSwapAction) for a in decide(snap, make_config()))


def test_reverse_swap_capped_by_provider_max() -> None:
    chan = make_channel(local_sat=1_500_000, spendable_local_sat=1_400_000)
    snap = make_snapshot(channels=(chan,), provider_max_reverse_sat=500_000)
    swaps = [a for a in decide(snap, make_config()) if isinstance(a, ReverseSwapAction)]
    assert swaps[0].lightning_amount_sat == 500_000


def test_no_reverse_swap_when_below_provider_min() -> None:
    chan = make_channel(local_sat=30_000, spendable_local_sat=15_000)
    snap = make_snapshot(channels=(chan,), provider_min_amount_sat=20_000)
    assert not any(isinstance(a, ReverseSwapAction) for a in decide(snap, make_config()))


def test_inactive_channel_not_swapped() -> None:
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000, is_active=False)
    snap = make_snapshot(channels=(chan,))
    assert not any(isinstance(a, ReverseSwapAction) for a in decide(snap, make_config()))


# --- effective all-in cost gate ------------------------------------------
def test_effective_cost_matches_electrum_costs_abs() -> None:
    # Mirrors a real SwapManager case: amount 25_650, 0.5% fee, mining 22_500,
    # claim 22_500 -> costs_abs = ceil(0.5%*25650)=129 + 45000 = 45129 sat,
    # costs_ratio = 45129/25650 = 175.94%.
    snap = make_snapshot(swap_mining_fee_sat=22_500, swap_claim_fee_sat=22_500)
    pct = effective_swap_cost_pct(25_650, snap)
    assert round(pct, 2) == round(45_129 / 25_650 * 100, 2)


def test_effective_cost_unknown_when_no_provider() -> None:
    snap = make_snapshot(swap_percentage_fee=None)
    assert effective_swap_cost_pct(1_000_000, snap) is None


def test_small_swap_blocked_by_fixed_fees() -> None:
    # 900k swap, 0.5% + 45_000 sat fixed -> ~5.5% all-in, over the 0.6% ceiling.
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(channels=(chan,), swap_mining_fee_sat=22_500, swap_claim_fee_sat=22_500)
    assert not any(isinstance(a, ReverseSwapAction) for a in decide(snap, make_config()))


def test_large_swap_amortizes_fixed_fees() -> None:
    # Same fixed fees, but a big channel: raise the ceiling enough that the
    # amortised all-in cost clears it, and confirm the swap is emitted.
    chan = make_channel(capacity_sat=10_000_000, local_sat=9_000_000, spendable_local_sat=1_800_000)
    snap = make_snapshot(channels=(chan,), swap_mining_fee_sat=22_500, swap_claim_fee_sat=22_500)
    cfg = make_config(max_swap_fee_pct=3.5)  # 0.5% + 45000/1.8M = ~3.0% all-in
    swaps = [a for a in decide(snap, cfg) if isinstance(a, ReverseSwapAction)]
    assert len(swaps) == 1
    assert swaps[0].lightning_amount_sat == 1_800_000


def test_open_and_swap_coexist() -> None:
    # On-chain funds to open a (3rd would-be) channel AND an active channel that
    # is over its swap trigger -> both an open and a swap come back.
    active = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=(active,))
    actions: List = decide(snap, make_config(max_channels=2))
    assert any(isinstance(a, OpenChannelAction) for a in actions)
    assert any(isinstance(a, ReverseSwapAction) for a in actions)


def test_swaps_each_eligible_channel() -> None:
    chans = (
        make_channel(channel_id="11" * 32, short_id="1x1x1", local_sat=1_000_000, spendable_local_sat=900_000),
        make_channel(channel_id="22" * 32, short_id="2x2x2", local_sat=1_000_000, spendable_local_sat=900_000),
    )
    snap = make_snapshot(channels=chans)
    swaps = [a for a in decide(snap, make_config()) if isinstance(a, ReverseSwapAction)]
    assert {s.short_id for s in swaps} == {"1x1x1", "2x2x2"}


# --- global in-flight freeze ---------------------------------------------
def _swaps(result: DecisionResult):
    return [d for d in result.declines if d.kind == "swap"]


def _opens(result: DecisionResult):
    return [d for d in result.declines if d.kind == "open"]


def test_freeze_when_channel_open_pending() -> None:
    # Plenty of on-chain to open another channel, but one open is still pending:
    # the whole tick is frozen -- no opens, no swaps.
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=(chan,),
                         pending_channel_count=1)
    result = evaluate(snap, make_config())
    assert result.actions == ()
    assert result.frozen is not None
    assert [d.kind for d in result.declines] == ["freeze"]


def test_freeze_when_reverse_swap_in_flight() -> None:
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=(chan,),
                         inflight_swap_count=1)
    result = evaluate(snap, make_config())
    assert result.actions == ()
    assert result.frozen is not None


def test_no_freeze_when_nothing_in_flight() -> None:
    # Same state but with zero in-flight ops: the open proceeds.
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=())
    result = evaluate(snap, make_config())
    assert any(isinstance(a, OpenChannelAction) for a in result.actions)
    assert result.frozen is None


def test_freeze_blocks_otherwise_eligible_swap() -> None:
    chan = make_channel(local_sat=30_000, spendable_local_sat=29_000, capacity_sat=2_000_000)
    snap = make_snapshot(channels=(chan,), inflight_swap_count=1)
    result = evaluate(snap, make_config())
    assert not any(isinstance(a, ReverseSwapAction) for a in result.actions)
    assert result.frozen is not None


# --- decline records ------------------------------------------------------
def test_decline_recorded_when_swap_cost_too_high() -> None:
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    snap = make_snapshot(channels=(chan,), swap_percentage_fee=0.7)
    result = evaluate(snap, make_config())
    assert result.actions == ()
    swaps = _swaps(result)
    assert len(swaps) == 1 and "ceiling" in swaps[0].reason


def test_decline_recorded_at_max_channels_with_funds() -> None:
    chans = (make_channel(), make_channel())
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=chans)
    result = evaluate(snap, make_config())
    opens = _opens(result)
    assert len(opens) == 1 and "at max channels" in opens[0].reason


def test_no_open_decline_when_just_waiting_for_funds() -> None:
    # Below the min-to-open gate is "waiting for funds", not a near miss.
    snap = make_snapshot(onchain_spendable_sat=500_000, channels=())
    result = evaluate(snap, make_config())
    assert _opens(result) == []


def test_decline_recorded_below_provider_min() -> None:
    chan = make_channel(local_sat=30_000, spendable_local_sat=15_000)
    snap = make_snapshot(channels=(chan,), provider_min_amount_sat=20_000)
    result = evaluate(snap, make_config())
    swaps = _swaps(result)
    assert len(swaps) == 1 and "minimum" in swaps[0].reason


def test_decline_recorded_for_inactive_over_trigger() -> None:
    chan = make_channel(local_sat=30_000, spendable_local_sat=29_000, is_active=False)
    snap = make_snapshot(channels=(chan,))
    result = evaluate(snap, make_config())
    assert not any(isinstance(a, ReverseSwapAction) for a in result.actions)
    swaps = _swaps(result)
    assert len(swaps) == 1 and "not " in swaps[0].reason  # "not active"


def test_no_decline_when_below_triggers() -> None:
    # An idle channel below both triggers produces neither action nor decline.
    chan = make_channel(local_sat=1_000, spendable_local_sat=1_000, capacity_sat=2_000_000)
    snap = make_snapshot(channels=(chan,))
    result = evaluate(snap, make_config())
    assert result.actions == ()
    assert result.declines == ()


def test_evaluate_actions_match_decide_wrapper() -> None:
    snap = make_snapshot(onchain_spendable_sat=5_000_000, channels=())
    assert list(evaluate(snap, make_config()).actions) == decide(snap, make_config())


def test_disabled_yields_empty_result() -> None:
    snap = make_snapshot(onchain_spendable_sat=5_000_000, pending_channel_count=3)
    result = evaluate(snap, make_config(automation_enabled=False))
    assert result.actions == () and result.declines == () and result.frozen is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
