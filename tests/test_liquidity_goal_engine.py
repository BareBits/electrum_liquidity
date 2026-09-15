"""Unit tests for the undersized-channel replacement rule (the "liquidity goal").

The rule: a plugin-opened channel whose CAPACITY is below the configured goal is
closed -- freeing its slot for a bigger one -- but only when we are at the channel
ceiling, hold enough on-chain to fund a replacement that would actually reach the
goal, and the channel is genuinely idle (not over a swap trigger, no unsettled
HTLCs, peer reachable).

Every gate is exercised on its own, so a regression names the gate it broke. No
Electrum import is required -- this is all the pure engine.
"""
from __future__ import annotations

from typing import List

import pytest

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    ChannelSnapshot,
    CloseChannelAction,
    DeclineRecord,
    LiquidityConfig,
    LiquiditySnapshot,
    MIN_FUNDING_SAT,
    OpenChannelAction,
    evaluate,
    over_swap_trigger,
)


GOAL = 500_000
# Comfortably funds a replacement at the goal: goal + reserve, and above
# MIN_FUNDING_SAT so the funding floor is never the thing under test.
RICH_ONCHAIN = GOAL + 10_000


def make_config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=60_000,
        onchain_reserve_sat=10_000,
        max_channels=2,
        max_swap_fee_pct=0.9,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
        liquidity_goal_sat=GOAL,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


def make_channel(**overrides) -> ChannelSnapshot:
    """A DRAINED, undersized, plugin-opened, healthy channel: the shape that
    qualifies for replacement. Each test perturbs exactly one field."""
    base = dict(
        channel_id="aa" * 32,
        short_id="100x1x0",
        capacity_sat=200_000,          # below GOAL
        local_sat=0,                   # drained -> under both swap triggers
        remote_sat=200_000,
        spendable_local_sat=0,
        is_active=True,
        has_unsettled_htlcs=False,
        unsettled_is_swap=False,
        is_plugin_opened=True,
    )
    base.update(overrides)
    return ChannelSnapshot(**base)


def make_snapshot(channels, **overrides) -> LiquiditySnapshot:
    base = dict(
        onchain_spendable_sat=RICH_ONCHAIN,
        channels=tuple(channels),
        swap_percentage_fee=None,
        provider_max_reverse_sat=None,
        provider_min_amount_sat=None,
    )
    base.update(overrides)
    return LiquiditySnapshot(**base)


def closes(result) -> List[CloseChannelAction]:
    return [a for a in result.actions if isinstance(a, CloseChannelAction)]


def opens(result) -> List[OpenChannelAction]:
    return [a for a in result.actions if isinstance(a, OpenChannelAction)]


def close_declines(result) -> List[DeclineRecord]:
    return [d for d in result.declines if d.kind == "close"]


def at_ceiling(**chan_overrides) -> LiquiditySnapshot:
    """Two channels (= the default max_channels): one replaceable candidate plus
    a healthy big one, so we are exactly at the ceiling."""
    candidate = make_channel(**chan_overrides)
    big = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                       capacity_sat=2_000_000, remote_sat=2_000_000)
    return make_snapshot([candidate, big])


# --- the happy path -------------------------------------------------------
def test_replaces_undersized_channel_when_blocked_and_funded():
    result = evaluate(at_ceiling(), make_config())
    acts = closes(result)
    assert len(acts) == 1
    assert acts[0].channel_id == "aa" * 32
    assert acts[0].short_id == "100x1x0"
    assert acts[0].capacity_sat == 200_000
    # The reason has to carry the arithmetic that justifies an irreversible act.
    assert "200000" in acts[0].reason
    assert str(GOAL) in acts[0].reason


def test_close_suppresses_the_contradictory_at_max_channels_open_decline():
    # We are at the ceiling with funds, so _decide_channel_open would normally
    # log "at max channels; not opening". When a close is what resolves that,
    # logging both would read as the plugin arguing with itself.
    result = evaluate(at_ceiling(), make_config())
    assert closes(result)
    assert not [d for d in result.declines
                if d.kind == "open" and "at max channels" in d.reason]


def test_at_max_channels_decline_survives_when_no_close_is_possible():
    # Same state, but the candidate is protected -> the open decline must still
    # be logged; suppressing it only makes sense when a close replaces it.
    snap = at_ceiling(local_sat=100_000)      # over the pct trigger -> protected
    result = evaluate(snap, make_config())
    assert not closes(result)
    assert [d for d in result.declines
            if d.kind == "open" and "at max channels" in d.reason]


# --- gate 1: the goal must be configured ----------------------------------
@pytest.mark.parametrize("goal", [0, -1])
def test_goal_zero_or_negative_disables_the_rule(goal):
    result = evaluate(at_ceiling(), make_config(liquidity_goal_sat=goal))
    assert closes(result) == []
    assert close_declines(result) == []


def test_default_config_has_the_rule_off():
    # The dataclass default is 0 (off) so every pre-feature caller and pure test
    # keeps its old behaviour; the SHIPPED default is set by the ConfigVar.
    assert LiquidityConfig(
        automation_enabled=True, min_onchain_to_open_sat=1, onchain_reserve_sat=0,
        max_channels=2, max_swap_fee_pct=1.0, swap_trigger_pct=25.0,
        swap_trigger_sat=25_000).liquidity_goal_sat == 0


# --- gate 2: only at the channel ceiling ----------------------------------
def test_below_the_ceiling_nothing_is_closed():
    # Room to open a bigger channel alongside, so closing anything is gratuitous.
    result = evaluate(at_ceiling(), make_config(max_channels=5))
    assert closes(result) == []
    assert close_declines(result) == []
    # ...and the open the ceiling was blocking now proceeds.
    assert len(opens(result)) == 1


def test_over_the_ceiling_still_fires():
    # max_channels lowered under us (or a manual open pushed us past it): the
    # gate is >=, not ==, so the rule still works.
    result = evaluate(at_ceiling(), make_config(max_channels=1))
    assert len(closes(result)) == 1


# --- gate 3: the replacement must be fundable -----------------------------
def test_insufficient_onchain_closes_nothing():
    # One sat short of funding a channel at the goal, after the reserve.
    snap = make_snapshot(at_ceiling().channels,
                         onchain_spendable_sat=GOAL + 10_000 - 1)
    result = evaluate(snap, make_config())
    assert closes(result) == []
    # Purely "waiting for funds" -- not a near miss, so nothing is logged.
    assert close_declines(result) == []


def test_exactly_enough_onchain_fires():
    snap = make_snapshot(at_ceiling().channels,
                         onchain_spendable_sat=GOAL + 10_000)
    assert len(closes(evaluate(snap, make_config()))) == 1


def test_funding_floor_applies_when_the_goal_is_below_it():
    # A goal under MIN_FUNDING_SAT must not let us close on the strength of funds
    # that could not actually open a channel.
    goal = MIN_FUNDING_SAT // 2
    cfg = make_config(liquidity_goal_sat=goal)
    chans = [make_channel(capacity_sat=goal - 1, remote_sat=goal - 1),
             make_channel(channel_id="bb" * 32, short_id="200x1x0",
                          capacity_sat=2_000_000, remote_sat=2_000_000)]
    # Enough for the goal, but below the funding floor -> no close.
    short = make_snapshot(chans, onchain_spendable_sat=goal + 10_000)
    assert closes(evaluate(short, cfg)) == []
    # Clearing the floor unblocks it.
    ok = make_snapshot(chans, onchain_spendable_sat=MIN_FUNDING_SAT + 10_000)
    assert len(closes(evaluate(ok, cfg))) == 1


# --- gate 4: plugin-opened only -------------------------------------------
def test_user_opened_channel_is_never_closed():
    result = evaluate(at_ceiling(is_plugin_opened=False), make_config())
    assert closes(result) == []
    # Not even a decline: a hand-opened channel is simply out of scope, not a
    # candidate that got blocked.
    assert close_declines(result) == []


def test_scope_holds_even_with_manage_plugin_opened_only_off():
    # That switch governs reverse swaps. Closing is irreversible and costs a
    # mining fee, so the plugin-opened scope is unconditional here.
    result = evaluate(at_ceiling(is_plugin_opened=False),
                      make_config(manage_plugin_opened_only=False))
    assert closes(result) == []


# --- gate 5: capacity below the goal --------------------------------------
def test_channel_at_the_goal_is_not_undersized():
    result = evaluate(at_ceiling(capacity_sat=GOAL, remote_sat=GOAL), make_config())
    assert closes(result) == []


def test_channel_one_sat_under_the_goal_is_undersized():
    snap = at_ceiling(capacity_sat=GOAL - 1, remote_sat=GOAL - 1)
    assert len(closes(evaluate(snap, make_config()))) == 1


# --- gate 6: over a swap trigger protects the channel ---------------------
def test_blocked_decline_reason_is_stable_across_ticks():
    """The decline's reason is its dedup key, so a steady state must log once.
    Anything that drifts tick to tick (the on-chain balance) belongs in `detail`,
    which is excluded from the signature."""
    protected = dict(local_sat=100_000, remote_sat=100_000,
                     spendable_local_sat=100_000)
    first = close_declines(evaluate(at_ceiling(**protected), make_config()))
    moved = make_snapshot(at_ceiling(**protected).channels,
                          onchain_spendable_sat=RICH_ONCHAIN + 12_345)
    second = close_declines(evaluate(moved, make_config()))
    assert first[0].reason == second[0].reason
    # ...while the varying arithmetic is still reported, just not in the key.
    assert first[0].detail != second[0].detail
    assert str(RICH_ONCHAIN - 10_000) in first[0].detail


def test_channel_over_the_pct_trigger_is_protected():
    # 25% of 200k = 50k local: the plugin is about to convert this into inbound,
    # so closing it would throw that value away.
    result = evaluate(at_ceiling(local_sat=50_000, remote_sat=150_000,
                                 spendable_local_sat=50_000), make_config())
    assert closes(result) == []
    decl = close_declines(result)
    assert len(decl) == 1
    assert "swap out first" in decl[0].reason or "swap out first" in (decl[0].detail or "")


def test_channel_over_the_sat_trigger_is_protected():
    # Under the pct trigger (30k of 200k = 15%) but over the 25k sat trigger.
    result = evaluate(at_ceiling(local_sat=30_000, remote_sat=170_000,
                                 spendable_local_sat=30_000),
                      make_config(swap_trigger_pct=90.0))
    assert closes(result) == []
    assert len(close_declines(result)) == 1


def test_freshly_opened_channel_cannot_be_immediately_reclosed():
    """The churn guard, stated as the property it protects.

    The plugin funds channels with no push, so a brand-new channel has
    local == capacity and is over the percentage trigger by construction. It
    therefore cannot be opened and closed again in a loop: it only becomes
    replaceable once the plugin has actually drained it into inbound liquidity.
    """
    fresh = at_ceiling(capacity_sat=200_000, local_sat=200_000, remote_sat=0,
                       spendable_local_sat=200_000)
    assert over_swap_trigger(fresh.channels[0], make_config()) == "pct"
    assert closes(evaluate(fresh, make_config())) == []


# --- gate 7: no unsettled HTLCs -------------------------------------------
def test_unsettled_htlcs_protect_the_channel():
    result = evaluate(at_ceiling(has_unsettled_htlcs=True), make_config())
    assert closes(result) == []
    assert "unsettled HTLCs" in (close_declines(result)[0].detail or "")


# --- gate 8: peer must be reachable ---------------------------------------
def test_offline_peer_is_left_to_the_offline_watchdog():
    # Closing this would mean a force-close: a mining fee and a CSV timelock,
    # paid purely to resize. The offline auto-close watchdog owns that case.
    result = evaluate(at_ceiling(is_active=False), make_config())
    assert closes(result) == []
    assert "force-closed" in (close_declines(result)[0].detail or "")


# --- selection and pacing -------------------------------------------------
def test_only_one_channel_is_closed_per_evaluation():
    # Three undersized, drained channels but funds for ONE replacement: closing
    # all three would destroy more inbound than can be rebuilt.
    chans = [make_channel(channel_id=c * 32, short_id=f"{i}x1x0",
                          capacity_sat=cap, remote_sat=cap)
             for i, (c, cap) in enumerate([("aa", 300_000), ("bb", 200_000),
                                           ("cc", 400_000)])]
    result = evaluate(make_snapshot(chans), make_config(max_channels=3))
    assert len(closes(result)) == 1


def test_smallest_capacity_is_chosen_first():
    chans = [make_channel(channel_id=c * 32, short_id=f"{i}x1x0",
                          capacity_sat=cap, remote_sat=cap)
             for i, (c, cap) in enumerate([("aa", 300_000), ("bb", 120_000),
                                           ("cc", 400_000)])]
    acts = closes(evaluate(make_snapshot(chans), make_config(max_channels=3)))
    assert acts[0].capacity_sat == 120_000


def test_selection_is_deterministic_on_a_capacity_tie():
    chans = [make_channel(channel_id="cc" * 32, short_id="1x1x0",
                          capacity_sat=200_000, remote_sat=200_000),
             make_channel(channel_id="aa" * 32, short_id="2x1x0",
                          capacity_sat=200_000, remote_sat=200_000)]
    first = closes(evaluate(make_snapshot(chans), make_config(max_channels=2)))
    # Re-running the same tick must pick the same channel (ties break on id).
    second = closes(evaluate(make_snapshot(list(reversed(chans))),
                             make_config(max_channels=2)))
    assert first[0].channel_id == second[0].channel_id == "aa" * 32


# --- interaction with the rest of the engine ------------------------------
def test_automation_off_closes_nothing():
    result = evaluate(at_ceiling(), make_config(automation_enabled=False))
    assert result.actions == ()
    assert result.declines == ()


def test_global_freeze_blocks_the_close():
    # A reverse swap is mid-flight: the tick takes no action at all.
    snap = make_snapshot(at_ceiling().channels, inflight_swap_count=1)
    result = evaluate(snap, make_config())
    assert result.frozen is not None
    assert closes(result) == []


def test_confirming_open_blocks_the_close():
    """A still-confirming open has already committed the on-chain funds that the
    "we can afford a replacement" test counts. Closing on the strength of them
    would spend the same balance twice across two decisions."""
    snap = make_snapshot(at_ceiling().channels, pending_open_freeze_count=1)
    result = evaluate(snap, make_config())
    assert closes(result) == []


def test_close_and_reverse_swap_can_be_decided_in_the_same_tick():
    # The protected channel is drained by a swap while the idle undersized one is
    # replaced -- the two rules address different channels and must not interfere.
    from liquidity_manager import ProviderOffer, ReverseSwapAction
    drainable = make_channel(channel_id="bb" * 32, short_id="200x1x0",
                             capacity_sat=2_000_000, local_sat=1_000_000,
                             remote_sat=1_000_000, spendable_local_sat=1_000_000)
    snap = make_snapshot(
        [make_channel(), drainable],
        swap_claim_fee_sat=100,
        provider_offers=(ProviderOffer(npub="npub1" + "x" * 30, percentage_fee=0.1,
                                       mining_fee_sat=100, min_amount_sat=1_000,
                                       max_reverse_sat=10_000_000),))
    result = evaluate(snap, make_config())
    assert len(closes(result)) == 1
    swaps = [a for a in result.actions if isinstance(a, ReverseSwapAction)]
    assert len(swaps) == 1
    assert swaps[0].channel_id == "bb" * 32


# --- the shared trigger helper --------------------------------------------
@pytest.mark.parametrize("local,capacity,expected", [
    (0, 200_000, None),            # drained
    (50_000, 200_000, "pct"),      # exactly 25%
    (49_999, 200_000, "sat"),      # under 25% but over the 25k sat trigger
    (20_000, 200_000, None),       # under both
])
def test_over_swap_trigger_classification(local, capacity, expected):
    chan = make_channel(capacity_sat=capacity, local_sat=local,
                        spendable_local_sat=local)
    assert over_swap_trigger(chan, make_config()) == expected


# --- one replacement at a time, across ticks ------------------------------
def test_a_settling_close_blocks_a_second_replacement():
    """The gap this closes: a channel we asked to close stays in the snapshot
    until its closing tx is mined, and by then it reads as merely not-active. So
    the very next tick would find the NEXT undersized channel eligible and close
    it too -- on the strength of the same on-chain balance that justified the
    first. One funding amount must buy exactly one replacement."""
    closing = make_channel(channel_id="aa" * 32, short_id="1x1x0",
                           is_active=False)          # the one we just closed
    still_eligible = make_channel(channel_id="bb" * 32, short_id="2x1x0")
    snap = make_snapshot([closing, still_eligible], closing_channel_count=1)
    result = evaluate(snap, make_config(max_channels=2))
    assert closes(result) == []
    decl = close_declines(result)
    assert len(decl) == 1
    assert "already settling" in decl[0].reason


def test_settling_close_decline_reason_is_stable():
    # Logged once for the duration of the close, not on every tick.
    def _decl(n_undersized):
        chans = [make_channel(channel_id=c * 32, short_id=f"{i}x1x0")
                 for i, c in enumerate(["aa", "bb", "cc"][:n_undersized])]
        return close_declines(evaluate(
            make_snapshot(chans, closing_channel_count=1),
            make_config(max_channels=n_undersized)))[0]
    assert _decl(2).reason == _decl(3).reason


def test_no_settling_close_does_not_block():
    snap = make_snapshot(at_ceiling().channels, closing_channel_count=0)
    assert len(closes(evaluate(snap, make_config()))) == 1


def test_settling_close_gate_is_after_the_undersized_check():
    # With nothing undersized there is no near miss, so a settling close must not
    # manufacture a decline out of an ordinary idle tick.
    big = make_channel(capacity_sat=GOAL, remote_sat=GOAL)
    other = make_channel(channel_id="bb" * 32, short_id="2x1x0",
                         capacity_sat=GOAL, remote_sat=GOAL)
    snap = make_snapshot([big, other], closing_channel_count=1)
    assert close_declines(evaluate(snap, make_config())) == []
