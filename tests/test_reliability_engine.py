"""Unit tests for the pure reliability penalty + its effect on provider ranking.

Covers the clock-free penalty math (exponential growth with consecutive faults,
half-life decay = auto-recover, cap, zero when healthy) and that the penalty
softly de-prioritises a flaky provider in select_provider without ever excluding
it (it is still chosen when it is the only gate-passing option). No Electrum
import required.
"""
from __future__ import annotations

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ProviderReliability,
    cheapest_hosting_cost,
    reliability_penalty_pct,
    select_provider,
)
from test_provider_selection import make_config, make_offer  # type: ignore


HALFLIFE = 6 * 3600.0  # 6h, the default half-life


# --- penalty math ---------------------------------------------------------
def test_no_faults_means_no_penalty() -> None:
    rel = ProviderReliability(consecutive_faults=0, age_since_last_fault_sec=0.0)
    assert reliability_penalty_pct(rel, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0) == 0.0


def test_base_penalty_at_first_fault() -> None:
    rel = ProviderReliability(consecutive_faults=1, age_since_last_fault_sec=0.0)
    assert reliability_penalty_pct(rel, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0) == pytest.approx(0.5)


def test_penalty_grows_exponentially_with_consecutive_faults() -> None:
    kw = dict(base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=100.0)
    p1 = reliability_penalty_pct(ProviderReliability(consecutive_faults=1), **kw)
    p2 = reliability_penalty_pct(ProviderReliability(consecutive_faults=2), **kw)
    p3 = reliability_penalty_pct(ProviderReliability(consecutive_faults=3), **kw)
    assert (p1, p2, p3) == pytest.approx((0.5, 1.0, 2.0))


def test_penalty_is_capped() -> None:
    rel = ProviderReliability(consecutive_faults=10, age_since_last_fault_sec=0.0)
    assert reliability_penalty_pct(rel, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0) == 5.0


def test_penalty_halves_after_one_halflife() -> None:
    rel = ProviderReliability(consecutive_faults=1, age_since_last_fault_sec=HALFLIFE)
    assert reliability_penalty_pct(rel, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0) == pytest.approx(0.25)


def test_penalty_decays_to_near_zero_after_many_halflives() -> None:
    rel = ProviderReliability(consecutive_faults=2, age_since_last_fault_sec=HALFLIFE * 10)
    p = reliability_penalty_pct(rel, base_pct=0.5, halflife_sec=HALFLIFE, cap_pct=5.0)
    assert 0.0 < p < 0.01


# --- effect on ranking ----------------------------------------------------
def test_penalty_deprioritises_flaky_cheapest() -> None:
    # "cheap" is 0.1% cheaper but carries a 0.3% penalty; "solid" should win.
    cheap = make_offer("cheap", pct=0.30)
    cheap = type(cheap)(**{**cheap.__dict__, "reliability_penalty_pct": 0.30})
    solid = make_offer("solid", pct=0.40)
    sel = select_provider([cheap, solid], 900_000, 0, make_config(max_swap_fee_pct=1.0))
    assert sel is not None and sel.offer.npub == "solid"
    # The real cost is reported untouched; rank cost carries the penalty.
    assert sel.all_in_cost_pct == pytest.approx(0.40)
    assert sel.rank_cost_pct == pytest.approx(0.40)


def test_flaky_still_used_when_sole_option() -> None:
    # Even with a large penalty, the only gate-passing provider is still chosen.
    cheap = make_offer("cheap", pct=0.30)
    cheap = type(cheap)(**{**cheap.__dict__, "reliability_penalty_pct": 4.0})
    sel = select_provider([cheap], 900_000, 0, make_config(max_swap_fee_pct=1.0))
    assert sel is not None and sel.offer.npub == "cheap"
    assert sel.all_in_cost_pct == pytest.approx(0.30)
    assert sel.rank_cost_pct == pytest.approx(4.30)


def test_penalty_never_rescues_over_gate_provider() -> None:
    # The gate is on REAL cost: an expensive provider is excluded regardless of
    # how reliable it is, and a 0-penalty cheap one is selected.
    dear = make_offer("dear", pct=5.0)
    sel = select_provider([dear], 900_000, 0, make_config(max_swap_fee_pct=0.6))
    assert sel is None


def test_penalty_does_not_push_under_gate_provider_over_it() -> None:
    # A provider whose real cost passes the gate stays eligible even if its rank
    # cost (incl. penalty) would exceed the ceiling -- the penalty only reorders.
    flaky = make_offer("flaky", pct=0.50)
    flaky = type(flaky)(**{**flaky.__dict__, "reliability_penalty_pct": 2.0})
    sel = select_provider([flaky], 900_000, 0, make_config(max_swap_fee_pct=0.6))
    assert sel is not None and sel.offer.npub == "flaky"


# --- decline helper -------------------------------------------------------
def test_cheapest_hosting_cost_below_min_is_none() -> None:
    offers = [make_offer("a", lo=500_000)]
    assert cheapest_hosting_cost(offers, 100_000, 0, make_config()) is None


def test_cheapest_hosting_cost_reports_cheapest_over_ceiling() -> None:
    offers = [make_offer("a", pct=5.0), make_offer("b", pct=3.0)]
    host = cheapest_hosting_cost(offers, 900_000, 0, make_config(max_swap_fee_pct=0.6))
    assert host is not None
    amount, cost_pct = host
    assert amount == 900_000 and cost_pct == pytest.approx(3.0)
