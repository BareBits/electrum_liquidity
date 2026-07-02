"""Unit tests for cheapest-provider selection in the pure rules engine.

Covers the offer cost arithmetic, the preferred (strict whitelist) / banned
filters, the per-provider min/max gating, and that the chosen provider's npub is
threaded onto the resulting ReverseSwapAction. No Electrum import required.
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ChannelSnapshot,
    LiquidityConfig,
    LiquiditySnapshot,
    ProviderOffer,
    ReverseSwapAction,
    decide,
    eligible_providers,
    evaluate,
    select_provider,
    swap_cost_sat,
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
        local_sat=1_000_000,
        remote_sat=1_000_000,
        spendable_local_sat=900_000,
        is_active=True,
    )
    base.update(overrides)
    return ChannelSnapshot(**base)


def make_offer(npub: str, *, pct=0.5, mining=0, lo=20_000, hi=1_900_000, pow_bits=0) -> ProviderOffer:
    return ProviderOffer(
        npub=npub, percentage_fee=pct, mining_fee_sat=mining,
        min_amount_sat=lo, max_reverse_sat=hi, pow_bits=pow_bits)


def make_snapshot(offers, **overrides) -> LiquiditySnapshot:
    base = dict(
        onchain_spendable_sat=0,
        channels=(make_channel(),),
        # Single-provider fields unused when provider_offers is non-empty.
        swap_percentage_fee=None,
        provider_max_reverse_sat=None,
        provider_min_amount_sat=None,
        swap_mining_fee_sat=0,
        swap_claim_fee_sat=0,
        provider_offers=tuple(offers),
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


# --- cost arithmetic ------------------------------------------------------
def test_swap_cost_sat_matches_formula() -> None:
    # 0.5% of 1_000_000 = 5000, + 1000 mining + 500 claim = 6500.
    assert swap_cost_sat(0.5, 1_000, 500, 1_000_000) == 6_500


# --- cheapest selection ---------------------------------------------------
def test_selects_cheapest_by_percentage() -> None:
    offers = [make_offer("npubA", pct=0.5), make_offer("npubB", pct=0.2),
              make_offer("npubC", pct=0.9)]
    sel = select_provider(offers, 900_000, 0, make_config())
    assert sel is not None and sel.offer.npub == "npubB"
    assert sel.amount_sat == 900_000


def test_fixed_fees_factor_into_cheapest() -> None:
    # A: low % but big mining fee; B: higher % but no mining fee. At 900k, B wins.
    a = make_offer("npubA", pct=0.1, mining=50_000)
    b = make_offer("npubB", pct=0.4, mining=0)
    sel = select_provider([a, b], 900_000, 0, make_config())
    assert sel.offer.npub == "npubB"


def test_tie_breaks_on_higher_pow() -> None:
    a = make_offer("npubA", pct=0.3, pow_bits=10)
    b = make_offer("npubB", pct=0.3, pow_bits=25)
    sel = select_provider([a, b], 900_000, 0, make_config())
    assert sel.offer.npub == "npubB"  # same cost, B has more PoW


def test_amount_capped_per_provider_max() -> None:
    sel = select_provider([make_offer("npubA", hi=500_000)], 900_000, 0, make_config())
    assert sel.amount_sat == 500_000


# --- preferred (strict whitelist) -----------------------------------------
def test_preferred_restricts_to_whitelist() -> None:
    offers = [make_offer("cheap", pct=0.1), make_offer("fav", pct=0.5)]
    cfg = make_config(preferred_npubs=frozenset({"fav"}))
    assert [o.npub for o in eligible_providers(offers, cfg)] == ["fav"]
    sel = select_provider(offers, 900_000, 0, cfg)
    assert sel.offer.npub == "fav"  # cheaper non-preferred is ignored


def test_preferred_none_available_blocks_swap() -> None:
    # Preferred set names a provider that is not in the discovered offers.
    offers = [make_offer("other", pct=0.1)]
    cfg = make_config(preferred_npubs=frozenset({"offline-fav"}))
    assert eligible_providers(offers, cfg) == []
    result = evaluate(make_snapshot(offers), cfg)
    assert not any(isinstance(a, ReverseSwapAction) for a in result.actions)
    decl = [d for d in result.declines if d.kind == "swap"]
    assert len(decl) == 1 and "preferred" in decl[0].reason


# --- banned ---------------------------------------------------------------
def test_banned_provider_excluded() -> None:
    offers = [make_offer("cheap", pct=0.1), make_offer("ok", pct=0.5)]
    cfg = make_config(banned_npubs=frozenset({"cheap"}))
    sel = select_provider(offers, 900_000, 0, cfg)
    assert sel.offer.npub == "ok"


def test_all_banned_blocks_swap() -> None:
    offers = [make_offer("a"), make_offer("b")]
    cfg = make_config(banned_npubs=frozenset({"a", "b"}))
    result = evaluate(make_snapshot(offers), cfg)
    decl = [d for d in result.declines if d.kind == "swap"]
    assert len(decl) == 1 and "banned" in decl[0].reason


def test_banned_wins_over_preferred() -> None:
    offers = [make_offer("x", pct=0.2), make_offer("y", pct=0.5)]
    cfg = make_config(preferred_npubs=frozenset({"x", "y"}),
                      banned_npubs=frozenset({"x"}))
    sel = select_provider(offers, 900_000, 0, cfg)
    assert sel.offer.npub == "y"


# --- min gating -----------------------------------------------------------
def test_below_all_minimums_blocks_swap() -> None:
    chan = make_channel(local_sat=30_000, spendable_local_sat=15_000)
    offers = [make_offer("a", lo=20_000)]
    result = evaluate(make_snapshot(offers, channels=(chan,)), make_config())
    decl = [d for d in result.declines if d.kind == "swap"]
    assert len(decl) == 1 and "minimum" in decl[0].reason


# --- action carries chosen provider ---------------------------------------
def test_action_carries_provider_npub() -> None:
    offers = [make_offer("npubWINNER", pct=0.2), make_offer("npubX", pct=0.9)]
    actions = [a for a in decide(make_snapshot(offers), make_config())
               if isinstance(a, ReverseSwapAction)]
    assert len(actions) == 1
    assert actions[0].provider_npub == "npubWINNER"


def test_no_offers_blocks_swap_with_known_reason() -> None:
    result = evaluate(make_snapshot([]), make_config())
    decl = [d for d in result.declines if d.kind == "swap"]
    assert len(decl) == 1 and "no swap provider is known" in decl[0].reason


def test_cost_gate_uses_cheapest_provider() -> None:
    # Cheapest provider clears the ceiling; an expensive one alone would not.
    offers = [make_offer("cheap", pct=0.4), make_offer("dear", pct=5.0)]
    result = evaluate(make_snapshot(offers), make_config(max_swap_fee_pct=0.6))
    swaps = [a for a in result.actions if isinstance(a, ReverseSwapAction)]
    assert len(swaps) == 1 and swaps[0].provider_npub == "cheap"
