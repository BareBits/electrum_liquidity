"""Unit tests for the offline-channel discount against ``max_channels``.

The rule: a PLUGIN-OPENED channel whose peer has gone dark stops occupying a
channel slot, so the plugin may open a replacement without waiting out the
auto-close watchdog's force-close horizon (and a CSV timelock after it). The
evidence is either the watchdog's own close intent, or the peer being offline now
with at least ``OFFLINE_DISCOUNT_MIN_FAULTS`` recorded offline faults. The
resulting overshoot is capped by ``offline_open_allowance``.

Exercised here: the predicate (every prong and every guard against it), the
effective count, the shared ``has_room_for_another_channel`` bound, and the fact
that all THREE rules that ask about room -- the open rule, the liquidity goal and
the undersized-channel replacement rule -- agree with it. Pure engine: no
Electrum, no clock.
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
    OFFLINE_DISCOUNT_MIN_FAULTS,
    OpenChannelAction,
    channel_is_offline_discounted,
    discounted_channel_count,
    effective_channel_count,
    evaluate,
    has_room_for_another_channel,
    liquidity_goal_met,
    offline_discount_applies,
)

GOAL = 500_000
# Enough on-chain to fund a channel at the goal plus the reserve, so neither the
# funding floor nor the min-to-open gate is ever the thing under test.
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
        # The shipped default; the dataclass default is 0 (= feature off) so that
        # every pre-existing pure test keeps its original behaviour.
        offline_open_allowance=1,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


def make_channel(**overrides) -> ChannelSnapshot:
    """A healthy, online, plugin-opened channel at the goal: counts in full.
    Each test perturbs exactly the fields it is about."""
    base = dict(
        channel_id="aa" * 32,
        short_id="100x1x0",
        capacity_sat=GOAL,
        local_sat=0,                   # drained -> under both swap triggers
        remote_sat=GOAL,
        spendable_local_sat=0,
        is_active=True,
        is_plugin_opened=True,
    )
    base.update(overrides)
    return ChannelSnapshot(**base)


def dead_channel(**overrides) -> ChannelSnapshot:
    """A plugin-opened channel whose peer is gone with enough offline faults to
    be discounted via the fault prong."""
    base = dict(is_active=False, offline_fault_count=OFFLINE_DISCOUNT_MIN_FAULTS)
    base.update(overrides)
    return make_channel(**base)


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


def opens(result) -> List[OpenChannelAction]:
    return [a for a in result.actions if isinstance(a, OpenChannelAction)]


def closes(result) -> List[CloseChannelAction]:
    return [a for a in result.actions if isinstance(a, CloseChannelAction)]


def open_declines(result) -> List[DeclineRecord]:
    return [d for d in result.declines if d.kind == "open"]


# --- the predicate: what earns a discount ---------------------------------
def test_healthy_channel_is_never_discounted() -> None:
    assert not channel_is_offline_discounted(make_channel())


def test_offline_with_enough_faults_is_discounted() -> None:
    assert channel_is_offline_discounted(dead_channel())


def test_one_offline_fault_is_not_enough() -> None:
    # "More than one fault" -- a single 24h outage is a blip, not a dead peer.
    assert not channel_is_offline_discounted(
        make_channel(is_active=False, offline_fault_count=1))


def test_faults_without_being_offline_now_is_not_enough() -> None:
    # History alone never discounts: the peer is answering us right now, so
    # whatever it did last week, its channel is working.
    assert not channel_is_offline_discounted(
        make_channel(is_active=True, offline_fault_count=50))


def test_close_intent_alone_is_enough() -> None:
    # The watchdog has committed on uptime evidence; no fault tally needed.
    assert channel_is_offline_discounted(
        make_channel(is_active=False, close_intent=True, offline_fault_count=0))


def test_close_intent_discounts_even_while_peer_is_momentarily_active() -> None:
    # A peer blipping back online does not lift its uptime ratio above the floor,
    # and the watchdog cancels its own intent when the ratio does recover -- so
    # while the intent stands, the slot is spent.
    assert channel_is_offline_discounted(
        make_channel(is_active=True, close_intent=True))


def test_user_opened_channel_is_never_discounted() -> None:
    # The plugin never closes a hand-opened channel, so discounting one would put
    # the wallet permanently over its ceiling.
    assert not channel_is_offline_discounted(
        dead_channel(is_plugin_opened=False))
    assert not channel_is_offline_discounted(
        make_channel(is_plugin_opened=False, close_intent=True, is_active=False))


def test_pending_channel_is_never_discounted() -> None:
    # THE regression guard: a pre-OPEN channel reads inactive by construction,
    # and its peer may carry offline faults from an earlier channel. Without the
    # pending exclusion every in-flight open would discount itself and the plugin
    # would open channels in a loop.
    assert not channel_is_offline_discounted(
        dead_channel(is_pending=True))
    assert not channel_is_offline_discounted(
        make_channel(is_pending=True, is_active=False, close_intent=True))


def test_min_faults_threshold_is_overridable() -> None:
    chan = make_channel(is_active=False, offline_fault_count=3)
    assert channel_is_offline_discounted(chan, min_offline_faults=3)
    assert not channel_is_offline_discounted(chan, min_offline_faults=4)


def test_min_faults_of_zero_still_requires_one_fault() -> None:
    # A degenerate threshold must not discount a channel with no evidence at all.
    assert not offline_discount_applies(
        is_plugin_opened=True, is_pending=False, is_active=False,
        close_intent=False, offline_fault_count=0, min_offline_faults=0)
    assert offline_discount_applies(
        is_plugin_opened=True, is_pending=False, is_active=False,
        close_intent=False, offline_fault_count=1, min_offline_faults=0)


# --- counting --------------------------------------------------------------
def test_effective_count_excludes_discounted() -> None:
    snap = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    assert len(snap.channels) == 2
    assert effective_channel_count(snap) == 1
    assert discounted_channel_count(snap) == 1


def test_effective_count_equals_raw_count_when_nothing_is_dead() -> None:
    snap = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert effective_channel_count(snap) == len(snap.channels) == 2
    assert discounted_channel_count(snap) == 0


# --- has_room: the two bounds ---------------------------------------------
def test_room_opens_up_when_a_channel_goes_dark() -> None:
    cfg = make_config(max_channels=2)
    healthy = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert not has_room_for_another_channel(healthy, cfg)
    dead = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    assert has_room_for_another_channel(dead, cfg)


def test_allowance_caps_the_overshoot() -> None:
    # Both peers dead under a ceiling of 2 and an allowance of 1: the effective
    # count is 0, but the wallet already holds 2 channels, so exactly ONE
    # replacement is allowed -- not two.
    cfg = make_config(max_channels=2, offline_open_allowance=1)
    two_dead = [dead_channel(), dead_channel(channel_id="bb" * 32)]
    assert effective_channel_count(make_snapshot(two_dead)) == 0
    assert has_room_for_another_channel(make_snapshot(two_dead), cfg)
    # ...and once that replacement exists, the real count is at 2 + 1.
    with_replacement = two_dead + [make_channel(channel_id="cc" * 32)]
    assert not has_room_for_another_channel(make_snapshot(with_replacement), cfg)


def test_bigger_allowance_permits_more_replacements() -> None:
    cfg = make_config(max_channels=2, offline_open_allowance=2)
    chans = [dead_channel(), dead_channel(channel_id="bb" * 32),
             make_channel(channel_id="cc" * 32)]
    assert has_room_for_another_channel(make_snapshot(chans), cfg)
    chans.append(make_channel(channel_id="dd" * 32))
    assert not has_room_for_another_channel(make_snapshot(chans), cfg)


def test_zero_allowance_turns_the_discount_off() -> None:
    # The kill switch: an offline channel keeps occupying its slot, exactly as it
    # did before the feature existed.
    cfg = make_config(max_channels=2, offline_open_allowance=0)
    dead = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    assert not has_room_for_another_channel(dead, cfg)


def test_negative_allowance_cannot_tighten_the_ceiling() -> None:
    # Only reachable from a hand-edited config; must not make the ceiling
    # stricter than max_channels.
    cfg = make_config(max_channels=2, offline_open_allowance=-5)
    one = make_snapshot([make_channel()])
    assert has_room_for_another_channel(one, cfg)


def test_effective_ceiling_still_binds_with_a_big_allowance() -> None:
    # A generous allowance does not let HEALTHY channels past the ceiling.
    cfg = make_config(max_channels=2, offline_open_allowance=5)
    healthy = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert not has_room_for_another_channel(healthy, cfg)


# --- the open rule ---------------------------------------------------------
def test_open_rule_opens_a_replacement_for_a_dead_channel() -> None:
    cfg = make_config(max_channels=2)
    snap = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    result = evaluate(snap, cfg)
    assert len(opens(result)) == 1
    assert "1 offline channel(s) discounted" in opens(result)[0].reason


def test_open_rule_declines_at_the_effective_ceiling() -> None:
    cfg = make_config(max_channels=2)
    result = evaluate(make_snapshot(
        [make_channel(), make_channel(channel_id="bb" * 32)]), cfg)
    assert not opens(result)
    assert any("at max channels (2 >= 2)" in d.reason
               for d in open_declines(result))


def test_open_rule_names_the_allowance_when_that_is_the_binding_bound() -> None:
    # Room by the ceiling (effective count 0 < 2), blocked by the allowance.
    cfg = make_config(max_channels=2, offline_open_allowance=1)
    snap = make_snapshot([dead_channel(), dead_channel(channel_id="bb" * 32),
                          make_channel(channel_id="cc" * 32)])
    result = evaluate(snap, cfg)
    assert not opens(result)
    decline = open_declines(result)
    assert decline, "the allowance cap is a near miss and must be logged"
    assert "offline-replacement allowance" in decline[0].reason


def test_no_decline_logged_when_there_are_simply_no_funds() -> None:
    # Waiting for funds is not a near miss, discount or no discount.
    cfg = make_config(max_channels=2)
    snap = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)],
                         onchain_spendable_sat=1_000)
    assert not open_declines(evaluate(snap, cfg))


def test_discount_does_not_bypass_the_daily_open_ceiling() -> None:
    cfg = make_config(max_channels=2, max_opens_per_day=1)
    snap = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)],
                         opens_last_24h=1)
    result = evaluate(snap, cfg)
    assert not opens(result)
    assert any("daily open ceiling" in d.reason for d in open_declines(result))


# --- the liquidity goal agrees with the open rule -------------------------
def test_goal_reads_unmet_while_a_dead_channel_is_discounted() -> None:
    # A slot has freed up, so another satoshi can still buy a channel: the
    # build-out is not done, and (with defer_sink_until_goal on) the sink stays
    # held back while the replacement is funded.
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL)
    snap = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    assert not liquidity_goal_met(snap, cfg)


def test_goal_reads_met_once_the_allowance_is_exhausted() -> None:
    # No further open is possible, so the goal must NOT sit deferred forever
    # behind a channel the plugin is not allowed to open.
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL,
                      offline_open_allowance=1)
    snap = make_snapshot([dead_channel(), dead_channel(channel_id="bb" * 32),
                          make_channel(channel_id="cc" * 32)])
    assert liquidity_goal_met(snap, cfg)


def test_goal_still_met_when_every_channel_is_healthy_and_at_the_goal() -> None:
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL)
    snap = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert liquidity_goal_met(snap, cfg)


# --- the replacement rule agrees too -------------------------------------
def test_undersized_replacement_pauses_while_a_slot_is_free() -> None:
    # The open rule has room to open alongside, so closing a healthy undersized
    # channel to "make room" would be gratuitous -- and would spend a close on
    # room we already have.
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL)
    undersized = make_channel(capacity_sat=100_000, remote_sat=100_000)
    snap = make_snapshot([undersized, dead_channel(channel_id="bb" * 32)])
    result = evaluate(snap, cfg)
    assert not closes(result)
    # ...and the open the room was freed for is the action that does happen.
    assert len(opens(result)) == 1


def test_undersized_replacement_resumes_once_the_allowance_is_exhausted() -> None:
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL,
                      offline_open_allowance=1)
    undersized = make_channel(capacity_sat=100_000, remote_sat=100_000)
    snap = make_snapshot([undersized, dead_channel(channel_id="bb" * 32),
                          make_channel(channel_id="cc" * 32)])
    result = evaluate(snap, cfg)
    assert [c.channel_id for c in closes(result)] == [undersized.channel_id]


def test_dead_channel_is_never_the_undersized_close_candidate() -> None:
    # Gate 9: closing it would have to be a force-close. That expense belongs to
    # the auto-close watchdog, which has uptime evidence for it.
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL,
                      offline_open_allowance=0)
    dead_undersized = dead_channel(capacity_sat=100_000, remote_sat=100_000)
    snap = make_snapshot([dead_undersized, make_channel(channel_id="bb" * 32)])
    result = evaluate(snap, cfg)
    assert not closes(result)


# --- no behaviour change when nothing is dead ----------------------------
@pytest.mark.parametrize("allowance", [0, 1, 5])
def test_counting_is_unchanged_for_a_wallet_with_healthy_channels(allowance) -> None:
    cfg = make_config(max_channels=2, offline_open_allowance=allowance)
    one = make_snapshot([make_channel()])
    two = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert has_room_for_another_channel(one, cfg)
    assert not has_room_for_another_channel(two, cfg)
