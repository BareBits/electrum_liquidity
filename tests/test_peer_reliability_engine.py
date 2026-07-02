"""Unit tests for the pure channel-peer reliability primitives in the rules
engine: the shared decaying-penalty math, the auto-ban threshold predicate, the
penalty-aware partner ordering, and the unsettled-HTLC swap guard. No Electrum
import required (the engine module is import-free; conftest adds it to sys.path).
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ProviderReliability,
    decayed_penalty_pct,
    evaluate,
    order_channel_partners,
    reliability_penalty_pct,
    should_auto_ban,
)
from test_provider_selection import make_channel, make_config, make_offer, make_snapshot  # type: ignore

HALFLIFE = 6 * 3600.0
PUB_A = "02" + "aa" * 32
PUB_B = "03" + "bb" * 32
PUB_C = "02" + "cc" * 32


# --- shared penalty math --------------------------------------------------
def test_decayed_penalty_matches_provider_wrapper() -> None:
    # The provider wrapper must delegate to the shared core: same inputs, same
    # output, so peer and provider rankings decay identically.
    for faults, age in [(1, 0.0), (3, HALFLIFE), (5, 2 * HALFLIFE)]:
        rel = ProviderReliability(consecutive_faults=faults, age_since_last_fault_sec=age)
        via_wrapper = reliability_penalty_pct(rel, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0)
        via_core = decayed_penalty_pct(faults, age, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0)
        assert via_wrapper == via_core


def test_decayed_penalty_zero_when_healthy() -> None:
    assert decayed_penalty_pct(0, 0.0, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0) == 0.0


def test_decayed_penalty_grows_and_caps() -> None:
    fresh = decayed_penalty_pct(1, 0.0, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0)
    grown = decayed_penalty_pct(3, 0.0, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0)
    assert fresh == pytest.approx(0.5)
    assert grown == pytest.approx(2.0)            # 0.5 * 2^(3-1)
    capped = decayed_penalty_pct(10, 0.0, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0)
    assert capped == 5.0


# --- auto-ban predicate ---------------------------------------------------
def test_should_auto_ban_threshold() -> None:
    assert not should_auto_ban(2, 3)
    assert should_auto_ban(3, 3)
    assert should_auto_ban(4, 3)


def test_should_auto_ban_disabled_with_zero_threshold() -> None:
    assert not should_auto_ban(99, 0)
    assert not should_auto_ban(99, -1)


# --- penalty-aware partner ordering ---------------------------------------
def test_penalty_sinks_flaky_preferred_peer() -> None:
    # A and B both preferred (A first); B has a heavy penalty -> A stays ahead.
    out = order_channel_partners(
        [PUB_A, PUB_B], frozenset(), [], strict=True,
        penalties={PUB_B.lower(): 4.0})
    assert out == [PUB_A, PUB_B]
    # Now A is the flaky one -> B overtakes it despite A's earlier position.
    out2 = order_channel_partners(
        [PUB_A, PUB_B], frozenset(), [], strict=True,
        penalties={PUB_A.lower(): 4.0})
    assert out2 == [PUB_B, PUB_A]


def test_flaky_preferred_can_fall_behind_clean_suggestion() -> None:
    out = order_channel_partners(
        [PUB_A], frozenset(), [PUB_C], strict=False,
        penalties={PUB_A.lower(): 3.0})
    assert out == [PUB_C, PUB_A]


def test_no_penalties_preserves_order() -> None:
    out = order_channel_partners([PUB_A, PUB_B], frozenset(), [PUB_C], strict=False)
    assert out == [PUB_A, PUB_B, PUB_C]


def test_equal_penalties_are_stable() -> None:
    out = order_channel_partners(
        [PUB_A, PUB_B], frozenset(), [], strict=True,
        penalties={PUB_A.lower(): 1.0, PUB_B.lower(): 1.0})
    assert out == [PUB_A, PUB_B]


def test_penalty_never_excludes() -> None:
    # Even a maximally penalised sole peer is still returned (banning is separate).
    out = order_channel_partners([PUB_A], frozenset(), [], strict=True,
                                 penalties={PUB_A.lower(): 99.0})
    assert out == [PUB_A]


# --- unsettled-HTLC swap guard --------------------------------------------
def test_unsettled_htlcs_declines_swap() -> None:
    # A channel well over the swap trigger, active, but with unsettled HTLCs must
    # be declined (not swapped) with an explanatory reason.
    chan = make_channel(local_sat=1_500_000, spendable_local_sat=1_400_000,
                        is_active=True, has_unsettled_htlcs=True)
    snap = make_snapshot([make_offer(PUB_A, pct=0.1)], channels=(chan,))
    result = evaluate(snap, make_config(max_swap_fee_pct=5.0))
    assert result.actions == ()
    assert any(d.kind == "swap" and "unsettled HTLC" in d.reason for d in result.declines)


def test_clear_channel_still_swaps() -> None:
    # Same channel without unsettled HTLCs proceeds to a swap (guard is specific).
    chan = make_channel(local_sat=1_500_000, spendable_local_sat=1_400_000,
                        is_active=True, has_unsettled_htlcs=False)
    snap = make_snapshot([make_offer(PUB_A, pct=0.1)], channels=(chan,))
    result = evaluate(snap, make_config(max_swap_fee_pct=5.0))
    assert len(result.actions) == 1
