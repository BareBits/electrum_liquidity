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
    ProviderAttempt,
    ReverseSwapAction,
    SWAP_REDUCTION_FACTOR,
    evaluate,
    rank_providers,
    reduced_attempt_amount,
    select_provider,
    swap_cost_sat,
    swap_ladder_amounts,
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


# --- the ladder on the action --------------------------------------------
# `alternates` is no longer "the other providers": it is the rest of the engine's
# flat (provider, size) ladder, ordered by cost per sat of inbound liquidity. The
# same provider therefore appears several times, at different sizes, and a cheap
# provider's smaller rung can sit ahead of a dearer provider's full size.
#
# NB: make_offer defaults to mining=0 and make_snapshot to claim=0, so in these
# tests cost-per-sat is exactly the provider's percentage at EVERY size. That
# isolates the ordering from the fee arithmetic (covered separately below): rungs
# of one provider tie, and the stable sort keeps them largest-first.
_LADDER = (900_000, 810_000, 450_000, 270_000)   # make_channel's 900k desired


def _rungs(act) -> list:
    """(npub, amount) for the whole walk: the chosen swap, then its fallbacks."""
    return ([(act.provider_npub, act.lightning_amount_sat)]
            + [(a.npub, a.amount_sat) for a in act.alternates])


def test_action_carries_the_whole_ladder_cheapest_first() -> None:
    offers = [make_offer("npubA", pct=0.5), make_offer("npubB", pct=0.2),
              make_offer("npubC", pct=0.35)]
    actions = _swap_actions(evaluate(make_snapshot(offers), make_config()))
    assert len(actions) == 1
    act = actions[0]
    assert act.provider_npub == "npubB"                       # cheapest chosen
    assert act.lightning_amount_sat == 900_000                # at full size
    # Every provider contributes every ladder size, grouped by cost, and within a
    # provider ordered largest (cheapest per sat) first.
    assert _rungs(act) == ([("npubB", a) for a in _LADDER]
                           + [("npubC", a) for a in _LADDER]
                           + [("npubA", a) for a in _LADDER])
    assert all(isinstance(a, ProviderAttempt) for a in act.alternates)


def test_the_chosen_rung_is_never_repeated_as_a_fallback() -> None:
    """The head of the ladder is the swap being made; repeating it as a fallback
    would spend a cascade rung re-running the identical payment."""
    offers = [make_offer("npubA", pct=0.5), make_offer("npubB", pct=0.2)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    chosen = (act.provider_npub, act.lightning_amount_sat)
    assert chosen not in [(a.npub, a.amount_sat) for a in act.alternates]


def test_ladder_rungs_are_ordered_by_cost_per_sat_ascending() -> None:
    """The ordering contract, asserted on the costs rather than on names: a swap
    of `a` sat buys `a` sat of inbound, so all_in_cost_pct IS the per-sat price,
    and the executor must walk it cheapest-first."""
    offers = [make_offer("cheap", pct=0.2, mining=500),
              make_offer("dear", pct=0.45, mining=500)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config(
        max_swap_fee_pct=5.0)))[0]
    costs = [a.all_in_cost_pct for a in act.alternates]
    assert all(c is not None for c in costs)
    assert costs == sorted(costs), f"ladder is not cost-ordered: {costs}"


def test_a_cheap_providers_small_rung_can_outrank_a_dear_providers_full_size() -> None:
    """The interleaving that makes this a ladder rather than a failover list.

    With a real fixed fee, shrinking raises cost-per-sat -- but not without limit.
    A cheap provider stays cheaper than a dear one several rungs down, so its
    smaller (more routable) swap is tried BEFORE the dear provider's full size.
    """
    # 0.2% + 500 fixed vs 2.0% + 500 fixed. cheap@270k = 0.385%, dear@900k = 2.06%.
    offers = [make_offer("cheap", pct=0.2, mining=500),
              make_offer("dear", pct=2.0, mining=500)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config(
        max_swap_fee_pct=5.0)))[0]
    walk = _rungs(act)
    cheap_last = max(i for i, (n, _a) in enumerate(walk) if n == "cheap")
    dear_first = min(i for i, (n, _a) in enumerate(walk) if n == "dear")
    assert cheap_last < dear_first, (
        f"every cheap rung should precede any dear rung, got {walk}")


def test_ladder_is_not_capped() -> None:
    """Every (provider, size) that passed the cost gate is offered.

    The bound is purely temporal and lives in the executor
    (``swap_cascade_deadline_sec``), so the engine hands over the full ladder and
    lets the wall clock decide how far down it gets.
    """
    offers = [make_offer(f"npub{i}", pct=0.1 + i * 0.05) for i in range(6)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert act.provider_npub == "npub0"
    assert _rungs(act) == [(f"npub{i}", a) for i in range(6) for a in _LADDER]
    assert len(act.alternates) == 6 * len(_LADDER) - 1


def test_each_rung_carries_its_own_amount_and_cost() -> None:
    """A provider with less advertised capacity hosts a SMALLER swap. Each rung
    must carry that amount, not the chosen provider's -- otherwise a fallback
    would ask for more than that provider ever offered."""
    offers = [make_offer("roomy", pct=0.2, hi=1_900_000),
              make_offer("tight", pct=0.3, hi=300_000)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert act.provider_npub == "roomy"
    assert act.lightning_amount_sat == 900_000               # full spendable
    tight = [a for a in act.alternates if a.npub == "tight"]
    # 900k/810k/450k all clamp to tight's 300k cap and collapse to ONE rung; only
    # the 270k ladder step is below the cap and survives as its own.
    assert [a.amount_sat for a in tight] == [300_000, 270_000]
    # And each cost is computed on ITS amount, not on the chosen one's.
    assert tight[0].all_in_cost_pct == swap_cost_sat(0.3, 0, 0, 300_000) / 300_000 * 100.0


def test_capacity_clamping_never_yields_a_duplicate_rung() -> None:
    """The dedup contract on its own: a provider whose cap sits below the whole
    ladder must be offered exactly once, not once per ladder step."""
    offers = [make_offer("capped", pct=0.2, hi=100_000)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    walk = _rungs(act)
    assert walk == [("capped", 100_000)], walk
    assert len(walk) == len(set(walk))


def test_single_provider_still_gets_its_ladder() -> None:
    """One provider is not one attempt any more: its smaller sizes are genuinely
    different payments, and are exactly what a route-starved channel needs."""
    act = _swap_actions(evaluate(make_snapshot([make_offer("solo", pct=0.2)]),
                                 make_config()))[0]
    assert act.provider_npub == "solo"
    assert _rungs(act) == [("solo", a) for a in _LADDER]


def test_legacy_single_provider_mode_gets_a_ladder_too() -> None:
    """URL/legacy mode synthesises one offer with an empty npub. It has nobody to
    fail over TO, but it can still be retried smaller -- which is the whole point
    of the ladder, and used to be unavailable here."""
    snap = make_snapshot([], swap_percentage_fee=0.2, provider_min_amount_sat=20_000,
                         swap_mining_fee_sat=0, swap_claim_fee_sat=0)
    act = _swap_actions(evaluate(snap, make_config()))[0]
    assert act.provider_npub == ""
    assert [a.amount_sat for a in act.alternates] == list(_LADDER[1:])
    assert all(a.npub == "" for a in act.alternates)


def test_ladder_reported_in_the_action_reason() -> None:
    offers = [make_offer("npubA", pct=0.2), make_offer("npubB", pct=0.3)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert f"{2 * len(_LADDER) - 1} fallback rung(s) ready" in act.reason


def test_no_fallback_rungs_is_reported_as_no_rungs() -> None:
    """A provider whose minimum leaves exactly one viable size has no fallbacks,
    and the reason must not claim otherwise."""
    # lo=880_000 admits only the 900k ladder step.
    solo = _swap_actions(evaluate(make_snapshot([make_offer("solo", pct=0.2,
                                                            lo=880_000)]),
                                  make_config()))[0]
    assert solo.alternates == ()
    assert "fallback rung" not in solo.reason


# --- capacity budgeting across channels -----------------------------------
def test_only_the_chosen_rung_is_charged_capacity() -> None:
    """Fallback rungs are contingencies that usually never run, so they must not
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
    assert len(actions) == 2
    # Channel 1 takes npubA at full size and exhausts it. Its next fallback is
    # npubA's OWN 810k rung -- cheaper per sat than anything npubB offers -- which
    # is the ladder interleaving, not a bug.
    assert actions[0].provider_npub == "npubA"
    assert (actions[0].alternates[0].npub,
            actions[0].alternates[0].amount_sat) == ("npubA", 810_000)
    # Channel 2 must still be able to use npubB -- which is only true because
    # npubB, though present throughout channel 1's ladder, was never charged for
    # capacity it did not use.
    assert actions[1].provider_npub == "npubB"
    assert actions[1].lightning_amount_sat == 900_000
    # ...and npubA, whose capacity IS spent, contributes no rung to channel 2.
    assert "npubA" not in {a.npub for a in actions[1].alternates}


# --- the cost ceiling as the ladder's floor ---------------------------------------------
# A provider whose Lightning payment failed gets one more try at a slightly
# smaller amount, because a payment can fail for want of a single route carrying
# the full size and succeed just below it. Observed on mainnet: 95,468 sat out of
# a 109,921 sat channel failed 29 route attempts in a row, every failure a
# TEMPORARY_CHANNEL_FAILURE or UNKNOWN_NEXT_PEER from somewhere downstream.
#
# The reason this needs testing rather than just doing is the direction of the
# cost curve: shrinking a swap makes it MORE expensive as a percentage, because
# the provider's mining fee and our claim fee do not shrink with it. So the rung
# walks toward the user's ceiling and must be re-gated against it, every time.
def test_reduced_amount_is_the_configured_fraction() -> None:
    offer = make_offer("npub", pct=0.2, lo=20_000)
    reduced = reduced_attempt_amount(offer, 900_000, 0, make_config())
    assert reduced is not None
    amount, cost_pct = reduced
    assert amount == int(900_000 * SWAP_REDUCTION_FACTOR)
    # Pure percentage fee, no fixed fees: the cost does not move at all here.
    assert round(cost_pct, 3) == 0.2


def test_reduced_amount_costs_more_when_fees_are_fixed() -> None:
    """The whole reason the rung is re-gated. Same provider, smaller swap, higher
    all-in percentage -- because 315 sat of mining fee is 315 sat either way."""
    offer = make_offer("npub", pct=0.39, mining=315, lo=20_000)
    full = swap_cost_sat(0.39, 315, 315, 95_468) / 95_468 * 100.0
    reduced = reduced_attempt_amount(offer, 95_468, 315, make_config(max_swap_fee_pct=1.2))
    assert reduced is not None
    amount, cost_pct = reduced
    assert amount == 85_921
    assert cost_pct > full                      # smaller swap, dearer swap
    assert round(full, 3) == 1.051              # the figures from the live log
    assert round(cost_pct, 3) == 1.124


def test_reduced_amount_refused_when_it_breaches_the_ceiling() -> None:
    """One step further along that same curve and the ceiling bites: 77,328 sat
    costs 1.205% against a 1.2% ceiling, so no retry is offered. The user's cost
    limit is not negotiable just because a payment failed."""
    offer = make_offer("npub", pct=0.39, mining=315, lo=20_000)
    cfg = make_config(max_swap_fee_pct=1.2)
    assert reduced_attempt_amount(offer, 85_921, 315, cfg) is None
    # Sanity: it is the ceiling doing this, not the arithmetic falling over.
    assert round(swap_cost_sat(0.39, 315, 315, 77_328) / 77_328 * 100.0, 3) == 1.205
    assert reduced_attempt_amount(offer, 85_921, 315,
                                  make_config(max_swap_fee_pct=1.3)) is not None


def test_reduced_amount_refused_below_the_provider_minimum() -> None:
    """A reduced rung the provider would reject outright is not a retry, it is a
    wasted round trip."""
    offer = make_offer("npub", pct=0.2, lo=100_000)
    assert reduced_attempt_amount(offer, 900_000, 0, make_config()) is not None
    assert reduced_attempt_amount(offer, 105_000, 0, make_config()) is None


def test_reduced_amount_refused_when_it_would_not_shrink() -> None:
    """Degenerate sizes must not produce a 'retry' at the same amount (which would
    burn a rung re-running the identical payment) or at zero."""
    offer = make_offer("npub", pct=0.2, lo=0)
    assert reduced_attempt_amount(offer, 0, 0, make_config()) is None
    assert reduced_attempt_amount(offer, 1, 0, make_config()) is None


def test_cost_ceiling_truncates_the_ladder_from_the_bottom() -> None:
    """Shrinking RAISES cost per sat (the fixed fees stop being amortised), so the
    ceiling decides how far down the ladder a wallet can reach. This is the
    central trade of the whole design and is asserted as a monotone shape, not a
    magic number: a tighter ceiling can only ever remove rungs from the bottom.
    """
    offers = [make_offer("p", pct=0.2, mining=500)]
    sizes = {}
    for ceiling in (5.0, 0.4, 0.3, 0.25):
        act = _swap_actions(evaluate(make_snapshot(offers),
                                     make_config(max_swap_fee_pct=ceiling)))
        sizes[ceiling] = [a for _n, a in _rungs(act[0])] if act else []
    # Loosest ceiling reaches every ladder step; each tightening is a suffix of
    # the looser one (rungs drop off the small end, never the large end).
    assert sizes[5.0] == list(_LADDER)
    for tighter, looser in ((0.4, 5.0), (0.3, 0.4), (0.25, 0.3)):
        assert sizes[tighter] == sizes[looser][:len(sizes[tighter])], (
            f"ceiling {tighter} did not truncate {looser} from the bottom: "
            f"{sizes[tighter]} vs {sizes[looser]}")


def test_smallest_reachable_rung_obeys_the_ceiling_formula() -> None:
    """The floor a ceiling implies: cost(a)/a = pct + fixed/a <= ceiling means
    a >= fixed / (ceiling - pct). Every surviving rung must clear it, and the
    ceiling must be the only thing that removed the rest."""
    fixed, pct, ceiling = 500, 0.2, 0.3
    act = _swap_actions(evaluate(make_snapshot([make_offer("p", pct=pct,
                                                           mining=fixed)]),
                                 make_config(max_swap_fee_pct=ceiling)))[0]
    floor = fixed / ((ceiling - pct) / 100.0)
    kept = [a for _n, a in _rungs(act)]
    assert kept, "the ceiling should still admit the larger rungs"
    assert all(a >= floor for a in kept), f"{kept} vs floor {floor}"
    assert all(a < floor for a in _LADDER if a not in kept)


def test_provider_minimum_truncates_the_ladder() -> None:
    """A rung below the provider's advertised minimum is dropped -- it would be
    rejected server-side -- and that is independent of the cost gate."""
    offers = [make_offer("p", pct=0.2, lo=500_000)]
    act = _swap_actions(evaluate(make_snapshot(offers), make_config()))[0]
    assert [a for _n, a in _rungs(act)] == [900_000, 810_000]


def test_ladder_amounts_are_descending_deduplicated_and_positive() -> None:
    """The size ladder on its own, including the degenerate inputs that would
    otherwise reach the cascade as wasted or crashing rungs."""
    assert swap_ladder_amounts(900_000) == (900_000, 810_000, 450_000, 270_000)
    # Strictly descending and unique at every size, including ones where rounding
    # collapses neighbouring factors.
    for desired in (0, 1, 2, 3, 7, 19, 1_000, 123_457, 10**9):
        out = swap_ladder_amounts(desired)
        assert list(out) == sorted(set(out), reverse=True), (desired, out)
        assert all(a > 0 for a in out), (desired, out)
        assert all(a <= desired for a in out), (desired, out)
    # Degenerate desired sizes yield nothing rather than a zero-sat rung.
    assert swap_ladder_amounts(0) == ()
    assert swap_ladder_amounts(-5) == ()


def test_ladder_ignores_nonsense_factors_rather_than_raising() -> None:
    """Factors are reachable from configuration in principle; a bad entry should
    cost that rung, not the swap. NaN is the one that would otherwise crash."""
    assert swap_ladder_amounts(1_000, factors=(1.0, 0.0, -0.5, 2.0)) == (1_000,)
    assert swap_ladder_amounts(1_000, factors=(float("nan"), 0.5)) == (500,)
    assert swap_ladder_amounts(1_000, factors=(float("inf"),)) == ()
    assert swap_ladder_amounts(1_000, factors=()) == ()


# --- the user's minimum swap size -----------------------------------------
# A floor in SAT, separate from the percentage ceiling. The ceiling asks whether
# a swap is worth its cost, which a cheap provider can answer yes to at an
# absurdly small size; this asks whether a swap that small is worth doing at all,
# which is about the on-chain claim it will need and the UTXO it leaves behind.
# The ladder makes this matter more than it used to: small amounts are now
# generated deliberately, not just inherited from a small channel.
def test_min_swap_size_truncates_the_ladder_from_the_bottom() -> None:
    offers = [make_offer("p", pct=0.2, lo=1_000)]
    act = _swap_actions(evaluate(make_snapshot(offers),
                                 make_config(min_swap_sat=400_000)))[0]
    assert [a for _n, a in _rungs(act)] == [900_000, 810_000, 450_000]


def test_min_swap_size_drops_a_rung_however_cheap_it_prices() -> None:
    """The floor is not a restatement of the cost gate: with zero fees every rung
    costs the same per sat and the ceiling has no opinion at all, yet the small
    ones must still go."""
    offers = [make_offer("p", pct=0.2, mining=0, lo=1_000)]
    cfg = make_config(min_swap_sat=400_000, max_swap_fee_pct=99.0)
    act = _swap_actions(evaluate(make_snapshot(offers), cfg))[0]
    kept = [a for _n, a in _rungs(act)]
    assert kept == [900_000, 810_000, 450_000]
    # Every dropped rung was comfortably inside the ceiling -- only the floor
    # removed them.
    assert all(a.all_in_cost_pct is not None and a.all_in_cost_pct < 99.0
               for a in act.alternates)


def test_min_swap_size_composes_with_the_provider_minimum() -> None:
    """Whichever floor is higher wins, in both directions."""
    # User's floor is higher than the provider's.
    act = _swap_actions(evaluate(
        make_snapshot([make_offer("p", pct=0.2, lo=100_000)]),
        make_config(min_swap_sat=500_000)))[0]
    assert [a for _n, a in _rungs(act)] == [900_000, 810_000]
    # Provider's minimum is higher than the user's floor.
    act2 = _swap_actions(evaluate(
        make_snapshot([make_offer("p", pct=0.2, lo=500_000)]),
        make_config(min_swap_sat=100_000)))[0]
    assert [a for _n, a in _rungs(act2)] == [900_000, 810_000]


def test_min_swap_size_of_zero_disables_the_floor() -> None:
    offers = [make_offer("p", pct=0.2, lo=1_000)]
    act = _swap_actions(evaluate(make_snapshot(offers),
                                 make_config(min_swap_sat=0)))[0]
    assert [a for _n, a in _rungs(act)] == list(_LADDER)


def test_a_channel_below_the_floor_declines_and_names_the_floor() -> None:
    """The decline has to name the USER's floor, not the provider's minimum: they
    are different facts with different fixes, and pointing at the wrong one sends
    the user to the wrong setting."""
    chan = make_channel(capacity_sat=80_000, local_sat=40_000,
                        spendable_local_sat=30_000)
    result = evaluate(make_snapshot([make_offer("p", pct=0.2, lo=1_000)],
                                    channels=(chan,)),
                      make_config(min_swap_sat=50_000))
    assert not _swap_actions(result)
    reasons = " | ".join(d.reason for d in result.declines)
    assert "below your 50000 sat minimum swap size" in reasons
    assert "provider minimum" not in reasons


def test_the_floor_defaults_to_25000_sat() -> None:
    """The shipped default, pinned where the engine can see it -- a change here
    silently alters every wallet that never touched the setting."""
    from liquidity_manager import DEFAULT_MIN_SWAP_SAT  # type: ignore
    assert DEFAULT_MIN_SWAP_SAT == 25_000
    assert make_config().min_swap_sat == 25_000


def test_capacity_capped_below_the_floor_names_the_floor_not_the_provider() -> None:
    """A swap the channel could afford, pushed back under the user's floor by the
    provider's remaining capacity. The decline must say which bound refused it --
    "raise your floor" and "find a roomier provider" are different actions."""
    offers = [make_offer("p", pct=0.2, lo=1_000, hi=30_000)]
    result = evaluate(make_snapshot(offers), make_config(min_swap_sat=50_000))
    assert not _swap_actions(result)
    reasons = " | ".join(d.reason for d in result.declines)
    assert "below your 50000 sat minimum swap size" in reasons
    assert "capped by provider capacity" in reasons
    assert "provider minimum" not in reasons
