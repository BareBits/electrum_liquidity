"""Unit tests for the pure daily-ceiling logic in the rules engine: the
rolling-window counter, the cap predicate, and the channel-open ceiling that
turns a would-be open into a near-miss decline. No Electrum import required.
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    DAILY_WINDOW_SEC,
    LiquidityConfig,
    LiquiditySnapshot,
    OpenChannelAction,
    count_within_window,
    daily_cap_reached,
    evaluate,
)


def _config(**overrides) -> LiquidityConfig:
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


def _open_ready_snapshot(**overrides) -> LiquiditySnapshot:
    """A snapshot in which every rule *except* a daily ceiling would open a
    channel (funds present, no channels, nothing in flight)."""
    base = dict(
        onchain_spendable_sat=1_000_000,
        channels=(),
        swap_percentage_fee=None,
        provider_max_reverse_sat=None,
        provider_min_amount_sat=None,
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


# --- count_within_window --------------------------------------------------
def test_count_within_window_includes_only_trailing_window() -> None:
    now = 1_000_000.0
    tss = [now - 10, now - 3600, now - DAILY_WINDOW_SEC + 1, now - DAILY_WINDOW_SEC - 1]
    # The first three are within 24h; the last is just outside it.
    assert count_within_window(tss, now) == 3


def test_count_within_window_boundary_is_inclusive() -> None:
    now = 500.0
    assert count_within_window([now - DAILY_WINDOW_SEC], now) == 1     # exactly at cutoff
    assert count_within_window([now - DAILY_WINDOW_SEC - 0.5], now) == 0
    assert count_within_window([], now) == 0


# --- daily_cap_reached ----------------------------------------------------
def test_daily_cap_reached_semantics() -> None:
    assert not daily_cap_reached(4, 5)
    assert daily_cap_reached(5, 5)
    assert daily_cap_reached(6, 5)


def test_zero_cap_is_unlimited() -> None:
    assert not daily_cap_reached(0, 0)
    assert not daily_cap_reached(1_000_000, 0)
    assert not daily_cap_reached(1, -1)   # negative also treated as unlimited


# --- engine open ceiling --------------------------------------------------
def test_open_ceiling_blocks_and_records_near_miss() -> None:
    snap = _open_ready_snapshot(opens_last_24h=5)
    result = evaluate(snap, _config(max_opens_per_day=5))
    assert not result.actions                       # ceiling blocks the open
    assert len(result.declines) == 1
    d = result.declines[0]
    assert d.kind == "open"
    assert "daily open ceiling reached" in d.reason
    assert "5 >= 5" in d.reason


def test_open_allowed_below_ceiling() -> None:
    snap = _open_ready_snapshot(opens_last_24h=4)
    result = evaluate(snap, _config(max_opens_per_day=5))
    assert len(result.actions) == 1
    assert isinstance(result.actions[0], OpenChannelAction)
    assert not result.declines


def test_open_ceiling_zero_is_unlimited() -> None:
    snap = _open_ready_snapshot(opens_last_24h=999)
    result = evaluate(snap, _config(max_opens_per_day=0))
    assert len(result.actions) == 1
    assert isinstance(result.actions[0], OpenChannelAction)


def test_open_ceiling_only_fires_as_near_miss() -> None:
    # Funds below the min-to-open gate: waiting for funds, not a ceiling hit --
    # so no decline is recorded even though the count is over the ceiling.
    snap = _open_ready_snapshot(onchain_spendable_sat=0, opens_last_24h=99)
    result = evaluate(snap, _config(max_opens_per_day=5))
    assert not result.actions
    assert not result.declines
