"""Unit tests for the two outbound-liquidity-preservation mechanisms in the pure
rules engine:

  * ``LiquidityConfig.min_outbound_sat`` -- a per-channel floor a reverse swap
    never drains a channel's outbound (local) balance below; and
  * ``LiquidityConfig.manage_plugin_opened_only`` -- a scope switch that leaves
    user-opened channels entirely untouched.

No Electrum import is required (the engine is pure).
"""
from __future__ import annotations

from typing import List

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ChannelSnapshot,
    LiquidityConfig,
    LiquiditySnapshot,
    ReverseSwapAction,
    decide,
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
    # A channel well over the % trigger (1M local of a 2M-capacity channel), fully
    # spendable, so the swap path is reached unless a preservation rule blocks it.
    base = dict(
        channel_id="aa" * 32,
        short_id="100x1x0",
        capacity_sat=2_000_000,
        local_sat=1_000_000,
        remote_sat=1_000_000,
        spendable_local_sat=1_000_000,
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
        swap_mining_fee_sat=0,
        swap_claim_fee_sat=0,
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


def _swaps(actions) -> List[ReverseSwapAction]:
    return [a for a in actions if isinstance(a, ReverseSwapAction)]


# --- outbound floor -------------------------------------------------------
def test_floor_zero_drains_full_spendable() -> None:
    """The default floor of 0 preserves the original drain-everything behaviour."""
    snap = make_snapshot(channels=(make_channel(),))
    swaps = _swaps(decide(snap, make_config(min_outbound_sat=0)))
    assert len(swaps) == 1
    assert swaps[0].lightning_amount_sat == 1_000_000


def test_floor_reduces_swap_amount_by_floor() -> None:
    """The swap drains only ``spendable - floor``, leaving the floor as outbound."""
    snap = make_snapshot(channels=(make_channel(),))
    swaps = _swaps(decide(snap, make_config(min_outbound_sat=200_000)))
    assert len(swaps) == 1
    assert swaps[0].lightning_amount_sat == 800_000


def test_floor_covering_whole_spendable_blocks_and_declines() -> None:
    """When the floor covers the entire spendable balance there is nothing to
    drain: no swap, and a near-miss decline explaining the floor."""
    chan = make_channel(local_sat=150_000, spendable_local_sat=150_000)
    result = evaluate(make_snapshot(channels=(chan,)),
                      make_config(min_outbound_sat=200_000))
    assert _swaps(result.actions) == []
    swap_declines = [d for d in result.declines if d.kind == "swap"]
    assert len(swap_declines) == 1
    assert "outbound floor" in swap_declines[0].reason


def test_floor_leaves_at_least_floor_of_outbound() -> None:
    """Property: after the planned swap, remaining local balance >= floor."""
    floor = 300_000
    chan = make_channel(local_sat=1_000_000, spendable_local_sat=900_000)
    swaps = _swaps(decide(make_snapshot(channels=(chan,)),
                          make_config(min_outbound_sat=floor)))
    assert len(swaps) == 1
    remaining_local = chan.local_sat - swaps[0].lightning_amount_sat
    assert remaining_local >= floor


# --- plugin-opened-only scope --------------------------------------------
def test_scope_off_manages_user_opened_channel() -> None:
    """With the scope switch off (default), a user-opened channel is still drained."""
    chan = make_channel(is_plugin_opened=False)
    swaps = _swaps(decide(make_snapshot(channels=(chan,)),
                          make_config(manage_plugin_opened_only=False)))
    assert len(swaps) == 1


def test_scope_on_skips_user_opened_channel() -> None:
    """With the scope switch on, a user-opened channel is left untouched and the
    skip is recorded as a near miss."""
    chan = make_channel(is_plugin_opened=False)
    result = evaluate(make_snapshot(channels=(chan,)),
                      make_config(manage_plugin_opened_only=True))
    assert _swaps(result.actions) == []
    swap_declines = [d for d in result.declines if d.kind == "swap"]
    assert len(swap_declines) == 1
    assert "not opened by the plugin" in swap_declines[0].reason


def test_scope_on_still_drains_plugin_opened_channel() -> None:
    """With the scope switch on, a plugin-opened channel is drained normally."""
    chan = make_channel(is_plugin_opened=True)
    swaps = _swaps(decide(make_snapshot(channels=(chan,)),
                          make_config(manage_plugin_opened_only=True)))
    assert len(swaps) == 1
    assert swaps[0].lightning_amount_sat == 1_000_000


def test_scope_on_mixed_channels_drains_only_plugin_opened() -> None:
    """A mix: only the plugin-opened channel is drained; the manual one is spared."""
    plugin_chan = make_channel(channel_id="aa" * 32, short_id="100x1x0",
                               is_plugin_opened=True)
    manual_chan = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                               is_plugin_opened=False)
    result = evaluate(make_snapshot(channels=(plugin_chan, manual_chan)),
                      make_config(manage_plugin_opened_only=True))
    swaps = _swaps(result.actions)
    assert len(swaps) == 1
    assert swaps[0].short_id == "100x1x0"


# --- the two mechanisms compose ------------------------------------------
def test_floor_and_scope_compose() -> None:
    """Floor applies to the plugin-opened channel that survives the scope filter."""
    plugin_chan = make_channel(channel_id="aa" * 32, short_id="100x1x0",
                               is_plugin_opened=True)
    manual_chan = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                               is_plugin_opened=False)
    swaps = _swaps(decide(
        make_snapshot(channels=(plugin_chan, manual_chan)),
        make_config(manage_plugin_opened_only=True, min_outbound_sat=250_000)))
    assert len(swaps) == 1
    assert swaps[0].short_id == "100x1x0"
    assert swaps[0].lightning_amount_sat == 750_000
