"""Unit tests for the reverse-swap batching behaviour in the pure rules engine:

  * Per-provider capacity budgeting: when several channels are drained in one
    decision pass, the engine subtracts capacity already committed to a provider
    so it never plans two swaps that together exceed that provider's advertised
    max_forward (which the provider would reject server-side). A channel left
    without capacity this cycle is declined *benignly* ("committed this cycle"),
    not as a below-minimum near miss.
  * All eligible channels are considered in a single pass (issue #3): given
    enough provider capacity, every over-trigger channel gets its own swap action
    in the same evaluation -- it is NOT one-at-a-time.
  * Swap-aware unsettled-HTLC declines (issue #1): an unsettled HTLC that is the
    in-flight leg of a reverse swap we initiated is logged as such, not as a
    "possible stuck payment".

No Electrum import required (pure engine).
"""
from __future__ import annotations

from typing import List

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ChannelSnapshot,
    LiquidityConfig,
    LiquiditySnapshot,
    ProviderOffer,
    ReverseSwapAction,
    cheapest_hosting_cost,
    evaluate,
    select_provider,
)


def make_config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=1_000_000,
        onchain_reserve_sat=10_000,
        max_channels=5,
        max_swap_fee_pct=0.6,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


def make_channel(short_id: str, **overrides) -> ChannelSnapshot:
    base = dict(
        channel_id=short_id.replace("x", "") + "0" * 40,
        short_id=short_id,
        capacity_sat=2_000_000,
        local_sat=1_000_000,
        remote_sat=1_000_000,
        spendable_local_sat=900_000,
        is_active=True,
    )
    base.update(overrides)
    return ChannelSnapshot(**base)


def make_offer(npub: str, *, pct=0.3, mining=0, lo=20_000, hi=1_900_000) -> ProviderOffer:
    return ProviderOffer(
        npub=npub, percentage_fee=pct, mining_fee_sat=mining,
        min_amount_sat=lo, max_reverse_sat=hi)


def make_snapshot(channels, offers, **overrides) -> LiquiditySnapshot:
    base = dict(
        onchain_spendable_sat=0,          # no funds -> no channel-open action to add noise
        channels=tuple(channels),
        swap_percentage_fee=None,
        provider_max_reverse_sat=None,
        provider_min_amount_sat=None,
        swap_mining_fee_sat=0,
        swap_claim_fee_sat=0,
        provider_offers=tuple(offers),
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


def _swap_actions(result) -> List[ReverseSwapAction]:
    return [a for a in result.actions if isinstance(a, ReverseSwapAction)]


def _swap_declines(result):
    return [d for d in result.declines if d.kind == "swap"]


# --- select_provider capacity budget --------------------------------------
def test_select_provider_subtracts_consumed_capacity() -> None:
    offer = make_offer("npubA", hi=1_000_000)
    # Fresh: hosts the full 900k.
    assert select_provider([offer], 900_000, 0, make_config()).amount_sat == 900_000
    # 850k already committed to npubA this pass -> only 150k left to host.
    sel = select_provider([offer], 900_000, 0, make_config(), consumed={"npubA": 850_000})
    assert sel is not None and sel.amount_sat == 150_000


def test_select_provider_none_when_remaining_below_min() -> None:
    offer = make_offer("npubA", hi=1_000_000, lo=200_000)
    # 900k committed -> 100k remains, below the 200k minimum -> no selection.
    assert select_provider([offer], 900_000, 0, make_config(),
                           consumed={"npubA": 900_000}) is None
    # cheapest_hosting_cost with the same budget agrees (nobody can host)...
    assert cheapest_hosting_cost([offer], 900_000, 0, make_config(),
                                 consumed={"npubA": 900_000}) is None
    # ...but without the budget the provider *could* have hosted it.
    assert cheapest_hosting_cost([offer], 900_000, 0, make_config()) is not None


# --- batching across channels in one pass ---------------------------------
def test_second_channel_declined_when_provider_capacity_committed() -> None:
    # One provider with room for a single 900k swap; two channels want to drain.
    # The first swaps; the second is declined *benignly* (capacity committed this
    # cycle) -- NOT as a below-minimum near miss, and never sent (so no fault).
    offers = [make_offer("npubA", hi=1_000_000, lo=200_000)]
    chans = [make_channel("103x1x0"), make_channel("106x1x0")]
    result = evaluate(make_snapshot(chans, offers), make_config())
    actions = _swap_actions(result)
    assert len(actions) == 1 and actions[0].short_id == "103x1x0"
    assert actions[0].lightning_amount_sat == 900_000
    declines = [d for d in _swap_declines(result) if d.short_id == "106x1x0"]
    assert len(declines) == 1
    reason = declines[0].reason
    assert "committed to earlier swaps this cycle" in reason
    assert "will retry next cycle" in reason
    assert "below provider minimum" not in reason


def test_all_eligible_channels_swap_when_capacity_allows() -> None:
    # Issue #3 regression: with two providers (one channel's worth of capacity
    # each), BOTH over-trigger channels get a swap action in the SAME pass -- the
    # engine is not one-at-a-time. Channels are steered to different providers by
    # the capacity budget.
    offers = [make_offer("npubA", pct=0.2, hi=900_000),
              make_offer("npubB", pct=0.3, hi=900_000)]
    chans = [make_channel("103x1x0"), make_channel("106x1x0")]
    result = evaluate(make_snapshot(chans, offers), make_config())
    actions = _swap_actions(result)
    assert len(actions) == 2
    assert {a.short_id for a in actions} == {"103x1x0", "106x1x0"}
    # One swap per provider (each provider only had room for one).
    assert {a.provider_npub for a in actions} == {"npubA", "npubB"}
    assert not _swap_declines(result)          # nobody left waiting


def test_partial_capacity_still_used_for_second_channel() -> None:
    # A provider with room for 1.5 channels: first channel takes 900k, the second
    # still gets the leftover 600k (capacity budgeting caps, it does not veto).
    offers = [make_offer("npubA", hi=1_500_000, lo=20_000)]
    chans = [make_channel("103x1x0"), make_channel("106x1x0")]
    result = evaluate(make_snapshot(chans, offers), make_config())
    actions = sorted(_swap_actions(result), key=lambda a: a.short_id)
    assert [a.lightning_amount_sat for a in actions] == [900_000, 600_000]


# --- swap-aware unsettled-HTLC decline (issue #1) -------------------------
def test_unsettled_htlc_from_our_swap_is_not_stuck_payment() -> None:
    offers = [make_offer("npubA")]
    chan = make_channel("103x1x0", has_unsettled_htlcs=True, unsettled_is_swap=True)
    result = evaluate(make_snapshot([chan], offers), make_config())
    assert not _swap_actions(result)
    d = _swap_declines(result)[0]
    assert "reverse swap we initiated is still in flight" in d.reason
    assert "stuck payment" not in d.reason


def test_unsettled_htlc_not_ours_is_still_flagged_stuck() -> None:
    offers = [make_offer("npubA")]
    chan = make_channel("103x1x0", has_unsettled_htlcs=True, unsettled_is_swap=False)
    result = evaluate(make_snapshot([chan], offers), make_config())
    assert not _swap_actions(result)
    d = _swap_declines(result)[0]
    assert "possible stuck payment" in d.reason
