"""Unit tests for the provider *failover order* in the pure rules engine.

Where test_provider_selection covers "which single provider wins", this covers
the rest of the ranking: `rank_providers` returning every gate-passing provider
best-first, and `_decide_reverse_swaps` threading that tail onto the action as
`ReverseSwapAction.alternates` so the executor can fail over within one cycle.

Key invariant exercised here: each alternate carries its OWN amount_sat, because
providers advertise different capacities -- a failover is a differently-sized
swap, not the same swap re-addressed. No Electrum import required.
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    MAX_SWAP_PROVIDER_ATTEMPTS,
    ProviderAttempt,
    ReverseSwapAction,
    evaluate,
    rank_providers,
    select_provider,
    swap_cost_sat,
)

from test_provider_selection import (  # type: ignore
    make_channel,
    make_config,
    make_offer,
    make_snapshot,
)


def _swap_actions(result) -> list:
    return [a for a in result.actions if isinstance(a, ReverseSwapAction)]


# --- rank_providers: the full ordered list --------------------------------
def test_rank_providers_returns_all_gate_passers_best_first() -> None:
    offers = [make_offer("npubA", pct=0.5), make_offer("npubB", pct=0.2),
              make_offer("npubC", pct=0.35)]
    ranked = rank_providers(offers, 900_000, 0, make_config())
    assert [s.offer.npub for s in ranked] == ["npubB", "npubC", "npubA"]
    # Costs are the real all-in costs, ascending.
    assert [round(s.all_in_cost_pct, 3) for s in ranked] == [0.2, 0.35, 0.5]


def test_rank_providers_excludes_over_ceiling() -> None:
    # Ceiling 0.6%: the 0.9% provider must not appear even as a failover -- the
    # cost gate is absolute, it is not relaxed just because a cheaper one failed.
    offers = [make_offer("ok", pct=0.5), make_offer("dear", pct=0.9)]
    ranked = rank_providers(offers, 900_000, 0, make_config(max_swap_fee_pct=0.6))
    assert [s.offer.npub for s in ranked] == ["ok"]


def test_rank_providers_excludes_banned_and_honours_whitelist() -> None:
    offers = [make_offer("cheap", pct=0.1), make_offer("mid", pct=0.3),
              make_offer("fav", pct=0.5)]
    banned = rank_providers(offers, 900_000, 0,
                            make_config(banned_npubs=frozenset({"cheap"})))
    assert [s.offer.npub for s in banned] == ["mid", "fav"]
    preferred = rank_providers(offers, 900_000, 0,
                               make_config(preferred_npubs=frozenset({"fav", "mid"})))
    assert [s.offer.npub for s in preferred] == ["mid", "fav"]


def test_rank_providers_excludes_below_minimum() -> None:
    # 'big' will not accept anything under 800k, so at 500k only 'small' ranks.
    offers = [make_offer("small", pct=0.5, lo=20_000),
              make_offer("big", pct=0.1, lo=800_000)]
    ranked = rank_providers(offers, 500_000, 0, make_config())
    assert [s.offer.npub for s in ranked] == ["small"]


def test_rank_providers_orders_by_reliability_penalty_not_raw_cost() -> None:
    # 'flaky' is cheaper on paper but carries a penalty that sinks it behind
    # 'steady'. The REAL cost is untouched -- only the order changes.
    flaky = make_offer("flaky", pct=0.2)
    flaky = type(flaky)(**{**flaky.__dict__, "reliability_penalty_pct": 0.5})
    steady = make_offer("steady", pct=0.4)
    ranked = rank_providers([flaky, steady], 900_000, 0, make_config())
    assert [s.offer.npub for s in ranked] == ["steady", "flaky"]
    assert round(ranked[1].all_in_cost_pct, 3) == 0.2      # real cost unchanged
    assert round(ranked[1].rank_cost_pct, 3) == 0.7        # 0.2 + 0.5 penalty


def test_rank_providers_tie_breaks_on_higher_pow() -> None:
    a = make_offer("low-pow", pct=0.3, pow_bits=10)
    b = make_offer("high-pow", pct=0.3, pow_bits=25)
    ranked = rank_providers([a, b], 900_000, 0, make_config())
    assert [s.offer.npub for s in ranked] == ["high-pow", "low-pow"]


def test_select_provider_is_the_head_of_the_ranking() -> None:
    """select_provider must stay exactly rank_providers()[0] -- the refactor that
    introduced failover must not have shifted which provider wins."""
    offers = [make_offer("npubA", pct=0.5), make_offer("npubB", pct=0.2),
              make_offer("npubC", pct=0.35, hi=400_000)]
    cfg = make_config()
    for desired in (100_000, 400_000, 900_000):
        ranked = rank_providers(offers, desired, 0, cfg)
        sel = select_provider(offers, desired, 0, cfg)
        assert (sel is None) == (not ranked)
        if sel is not None:
            assert sel == ranked[0]


def test_rank_providers_empty_when_nobody_qualifies() -> None:
    assert rank_providers([], 900_000, 0, make_config()) == []
    dear = [make_offer("dear", pct=5.0)]
    assert rank_providers(dear, 900_000, 0, make_config()) == []


# --- alternates on the action --------------------------------------------
def test_action_carries_ranked_alternates_excluding_the_chosen() -> None:
    offers = [make_offer("npubA", pct=0.5), make_offer("npubB", pct=0.2),
              make_offer("npubC", pct=0.35)]
    actions = _swap_actions(evaluate(make_snapshot(offers), make_config()))
    assert len(actions) == 1
    act = actions[0]
    assert act.provider_npub == "npubB"                       # cheapest chosen
    assert [a.npub for a in act.alternates] == ["npubC", "npubA"]
    assert act.provider_npub not in {a.npub for a in act.alternates}
    assert all(isinstance(a, ProviderAttempt) for a in act.alternates)


def test_alternates_are_capped_to_the_attempt_budget() -> None:
    # Six eligible providers, but the cascade may only make
    # MAX_SWAP_PROVIDER_ATTEMPTS attempts in total (chosen + alternates).
    offers = [make_offer(f"npub{i}", pct=0.1 + i * 0.05) for i in range(6)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert len(act.alternates) == MAX_SWAP_PROVIDER_ATTEMPTS - 1
    assert [a.npub for a in act.alternates] == ["npub1", "npub2"]


def test_each_alternate_carries_its_own_amount_and_cost() -> None:
    """A provider with less advertised capacity hosts a SMALLER swap. The
    alternate must carry that amount, not the chosen provider's -- otherwise a
    failover would ask for more than the fallback ever offered."""
    offers = [make_offer("roomy", pct=0.2, hi=1_900_000),
              make_offer("tight", pct=0.3, hi=300_000)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert act.provider_npub == "roomy"
    assert act.lightning_amount_sat == 900_000               # full spendable
    assert len(act.alternates) == 1
    alt = act.alternates[0]
    assert alt.npub == "tight"
    assert alt.amount_sat == 300_000                          # capped to ITS max
    # And its cost is computed on ITS amount, not on the chosen one's.
    expected = swap_cost_sat(0.3, 0, 0, 300_000) / 300_000 * 100.0
    assert alt.all_in_cost_pct == expected


def test_single_provider_has_no_alternates() -> None:
    act = _swap_actions(evaluate(make_snapshot([make_offer("solo", pct=0.2)]),
                                 make_config()))[0]
    assert act.provider_npub == "solo"
    assert act.alternates == ()


def test_legacy_single_provider_mode_has_no_alternates() -> None:
    """URL/legacy mode synthesises one offer with an empty npub; there is nobody
    to fail over to, and the executor must keep its old single-shot behaviour."""
    snap = make_snapshot([], swap_percentage_fee=0.2, provider_min_amount_sat=20_000,
                         swap_mining_fee_sat=0, swap_claim_fee_sat=0)
    act = _swap_actions(evaluate(snap, make_config()))[0]
    assert act.provider_npub == ""
    assert act.alternates == ()


def test_alternates_reported_in_the_action_reason() -> None:
    offers = [make_offer("npubA", pct=0.2), make_offer("npubB", pct=0.3)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert "1 failover provider(s) ready" in act.reason
    solo = _swap_actions(evaluate(make_snapshot([make_offer("solo", pct=0.2)]),
                                  make_config()))[0]
    assert "failover" not in solo.reason


# --- capacity budgeting across channels -----------------------------------
def test_only_the_chosen_provider_is_charged_capacity() -> None:
    """Alternates are contingencies that usually never run, so they must not
    reserve capacity -- otherwise planning channel 1 would starve channel 2 of
    providers it could have used."""
    # Both cap at 1M and refuse anything under 200k, so one 900k swap leaves only
    # 100k -- below the minimum -- and fully exhausts whichever provider took it.
    offers = [make_offer("npubA", pct=0.2, lo=200_000, hi=1_000_000),
              make_offer("npubB", pct=0.3, lo=200_000, hi=1_000_000)]
    chans = (make_channel(channel_id="aa" * 32, short_id="1x1x1"),
             make_channel(channel_id="bb" * 32, short_id="2x2x2"))
    actions = _swap_actions(evaluate(make_snapshot(offers, channels=chans),
                                     make_config()))
    # Channel 1 takes npubA (cheapest) and exhausts it. Channel 2 must still be
    # able to use npubB -- which is only true because npubB, though listed as
    # channel 1's alternate, was never charged for capacity it did not use.
    assert len(actions) == 2
    assert actions[0].provider_npub == "npubA"
    assert actions[0].alternates[0].npub == "npubB"      # was a mere contingency
    assert actions[1].provider_npub == "npubB"
    assert actions[1].lightning_amount_sat == 900_000
