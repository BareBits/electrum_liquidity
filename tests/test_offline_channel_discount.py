"""Unit tests for the offline-channel discount against ``max_channels``.

The rule: a PLUGIN-OPENED channel whose peer has gone dark stops occupying a
channel slot, so the plugin may open a replacement without waiting out the
auto-close watchdog's force-close horizon (and a CSV timelock after it). The
evidence is either a contiguous run of OBSERVED offline time past
``offline_discount_after_sec``, or the watchdog's own close intent. The resulting
overshoot is capped by ``offline_open_allowance``.

Exercised here: the offline-streak accumulator (accrual, the sample-gap cap, the
reset on recovery), the predicate (every prong and every guard against it), the
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
    OpenChannelAction,
    UPTIME_MAX_SAMPLE_GAP_SEC,
    channel_is_offline_discounted,
    discounted_channel_count,
    effective_channel_count,
    evaluate,
    has_room_for_another_channel,
    liquidity_goal_met,
    offline_discount_applies,
    offline_streak_sec,
    record_offline_streak,
)

GOAL = 500_000
# Enough on-chain to fund a channel at the goal plus the reserve, so neither the
# funding floor nor the min-to-open gate is ever the thing under test.
RICH_ONCHAIN = GOAL + 10_000
HOUR = 3600.0
# The shipped threshold, in seconds.
DISCOUNT_AFTER = 4 * HOUR


def make_config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=60_000,
        onchain_reserve_sat=10_000,
        max_channels=2,
        max_swap_fee_pct=0.9,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
        # The shipped defaults. Both dataclass defaults are 0 (= feature off) so
        # that every pre-existing pure test keeps its original behaviour.
        offline_open_allowance=1,
        offline_discount_after_sec=DISCOUNT_AFTER,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


CFG = make_config()


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
    """A plugin-opened channel whose peer has been gone long enough to be
    discounted via the streak prong."""
    base = dict(is_active=False, offline_streak_sec=DISCOUNT_AFTER)
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


# --- the offline-streak accumulator ---------------------------------------
def test_first_offline_sample_accrues_nothing() -> None:
    # We have no idea how long the peer had already been gone before we first
    # looked, so the clock must start now rather than in the past.
    acc = record_offline_streak(None, now=1000.0, online=False)
    assert offline_streak_sec(acc) == 0.0
    assert acc["last_ts"] == 1000.0


def test_streak_accrues_between_samples() -> None:
    acc = record_offline_streak(None, now=0.0, online=False)
    acc = record_offline_streak(acc, now=600.0, online=False)
    acc = record_offline_streak(acc, now=1200.0, online=False)
    assert offline_streak_sec(acc) == 1200.0


def test_streak_sample_gap_is_capped() -> None:
    # A long gap is OUR downtime (wallet shut, network down), not the peer's
    # absence, so only the cap is credited -- the same protection the uptime
    # ratio has. This is what stops a wallet closed over a weekend from waking up
    # convinced every peer has been gone for days.
    acc = record_offline_streak(None, now=0.0, online=False)
    acc = record_offline_streak(acc, now=10 * HOUR, online=False)
    assert offline_streak_sec(acc) == UPTIME_MAX_SAMPLE_GAP_SEC


def test_online_sample_ends_the_streak() -> None:
    acc = record_offline_streak(None, now=0.0, online=False)
    acc = record_offline_streak(acc, now=600.0, online=False)
    assert record_offline_streak(acc, now=1200.0, online=True) is None


def test_streak_restarts_from_zero_after_recovery() -> None:
    # Not resumed: a peer that came back and dropped out again has to earn the
    # whole threshold afresh, so flapping never accumulates its way past it.
    acc = record_offline_streak(None, now=0.0, online=False)
    acc = record_offline_streak(acc, now=3 * HOUR, online=False)
    acc = record_offline_streak(acc, now=3 * HOUR + 1, online=True)
    assert acc is None
    acc = record_offline_streak(acc, now=4 * HOUR, online=False)
    assert offline_streak_sec(acc) == 0.0


def test_streak_survives_hand_edited_garbage() -> None:
    assert offline_streak_sec({"offline_sec": "not-a-number"}) == 0.0
    assert offline_streak_sec(None) == 0.0
    assert offline_streak_sec({}) == 0.0
    acc = record_offline_streak({"offline_sec": "x", "last_ts": "y"},
                                now=100.0, online=False)
    assert offline_streak_sec(acc) == 0.0


def test_streak_ignores_a_clock_that_went_backwards() -> None:
    acc = record_offline_streak(None, now=1000.0, online=False)
    acc = record_offline_streak(acc, now=500.0, online=False)
    assert offline_streak_sec(acc) == 0.0


# --- the predicate: what earns a discount ---------------------------------
def test_healthy_channel_is_never_discounted() -> None:
    assert not channel_is_offline_discounted(make_channel(), CFG)


def test_offline_past_the_threshold_is_discounted() -> None:
    assert channel_is_offline_discounted(dead_channel(), CFG)


def test_offline_below_the_threshold_is_not_enough() -> None:
    # A peer restart, a node upgrade, an hour of ISP trouble: all of these must
    # ride out without costing a replacement channel.
    assert not channel_is_offline_discounted(
        make_channel(is_active=False, offline_streak_sec=DISCOUNT_AFTER - 1), CFG)
    assert not channel_is_offline_discounted(
        make_channel(is_active=False, offline_streak_sec=HOUR), CFG)


def test_streak_without_being_offline_now_is_not_enough() -> None:
    # The streak is only as fresh as the last sample. A peer that has answered
    # since must not be discounted on a stale clock.
    assert not channel_is_offline_discounted(
        make_channel(is_active=True, offline_streak_sec=10 * DISCOUNT_AFTER), CFG)


def test_zero_threshold_disables_the_streak_prong() -> None:
    cfg = make_config(offline_discount_after_sec=0.0)
    assert not channel_is_offline_discounted(dead_channel(), cfg)
    # ...but the close intent still counts.
    assert channel_is_offline_discounted(
        make_channel(is_active=False, close_intent=True), cfg)


def test_a_longer_threshold_is_honoured() -> None:
    cfg = make_config(offline_discount_after_sec=12 * HOUR)
    assert not channel_is_offline_discounted(dead_channel(), cfg)
    assert channel_is_offline_discounted(
        make_channel(is_active=False, offline_streak_sec=12 * HOUR), cfg)


def test_close_intent_alone_is_enough() -> None:
    # The watchdog has committed on uptime-ratio evidence; no streak needed. This
    # is the prong that catches a FLAKY peer -- one reconnecting for a moment
    # every hour never builds a contiguous streak, but its ratio stays low.
    assert channel_is_offline_discounted(
        make_channel(is_active=False, close_intent=True, offline_streak_sec=0.0),
        CFG)


def test_close_intent_discounts_even_while_peer_is_momentarily_active() -> None:
    # A peer blipping back online does not lift its uptime ratio above the floor,
    # and the watchdog cancels its own intent when the ratio does recover -- so
    # while the intent stands, the slot is spent.
    assert channel_is_offline_discounted(
        make_channel(is_active=True, close_intent=True), CFG)


def test_user_opened_channel_is_never_discounted() -> None:
    # The plugin never closes a hand-opened channel, so discounting one would put
    # the wallet permanently over its ceiling.
    assert not channel_is_offline_discounted(
        dead_channel(is_plugin_opened=False), CFG)
    assert not channel_is_offline_discounted(
        make_channel(is_plugin_opened=False, close_intent=True, is_active=False),
        CFG)


def test_pending_channel_is_never_discounted() -> None:
    # THE regression guard: a pre-OPEN channel reads inactive by construction.
    # Without the pending exclusion an in-flight open could discount itself and
    # the plugin would open channels in a loop.
    assert not channel_is_offline_discounted(dead_channel(is_pending=True), CFG)
    assert not channel_is_offline_discounted(
        make_channel(is_pending=True, is_active=False, close_intent=True), CFG)


def test_negative_threshold_cannot_discount_anything() -> None:
    # Only reachable from a hand-edited config.
    assert not offline_discount_applies(
        is_plugin_opened=True, is_pending=False, is_active=False,
        close_intent=False, offline_streak_sec=0.0, discount_after_sec=-5.0)


# --- counting --------------------------------------------------------------
def test_effective_count_excludes_discounted() -> None:
    snap = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    assert len(snap.channels) == 2
    assert effective_channel_count(snap, CFG) == 1
    assert discounted_channel_count(snap, CFG) == 1


def test_effective_count_equals_raw_count_when_nothing_is_dead() -> None:
    snap = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert effective_channel_count(snap, CFG) == len(snap.channels) == 2
    assert discounted_channel_count(snap, CFG) == 0


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
    assert effective_channel_count(make_snapshot(two_dead), cfg) == 0
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


def test_a_brief_outage_does_not_buy_a_channel() -> None:
    # End to end on the thing the threshold exists to prevent: an hour of absence
    # under a 4h threshold changes nothing about what the plugin does.
    cfg = make_config(max_channels=2)
    blip = make_channel(channel_id="bb" * 32, is_active=False,
                        offline_streak_sec=HOUR)
    result = evaluate(make_snapshot([make_channel(), blip]), cfg)
    assert not opens(result)
    assert any("at max channels" in d.reason for d in open_declines(result))


# --- the liquidity goal agrees with the open rule -------------------------
def test_goal_reads_unmet_while_a_dead_channel_is_discounted() -> None:
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
    # channel to "make room" would be gratuitous.
    cfg = make_config(max_channels=2, liquidity_goal_sat=GOAL)
    undersized = make_channel(capacity_sat=100_000, remote_sat=100_000)
    snap = make_snapshot([undersized, dead_channel(channel_id="bb" * 32)])
    result = evaluate(snap, cfg)
    assert not closes(result)
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
    assert not closes(evaluate(snap, cfg))


# --- no behaviour change when nothing is dead ----------------------------
@pytest.mark.parametrize("allowance", [0, 1, 5])
def test_counting_is_unchanged_for_a_wallet_with_healthy_channels(allowance) -> None:
    cfg = make_config(max_channels=2, offline_open_allowance=allowance)
    one = make_snapshot([make_channel()])
    two = make_snapshot([make_channel(), make_channel(channel_id="bb" * 32)])
    assert has_room_for_another_channel(one, cfg)
    assert not has_room_for_another_channel(two, cfg)


def test_shipped_defaults_are_off_in_the_dataclass() -> None:
    # Both knobs default to 0 in the dataclass so that pure tests and
    # non-populating callers see pre-feature behaviour; read_config always passes
    # the user's real values.
    bare = LiquidityConfig(
        automation_enabled=True, min_onchain_to_open_sat=60_000,
        onchain_reserve_sat=10_000, max_channels=2, max_swap_fee_pct=0.9,
        swap_trigger_pct=25.0, swap_trigger_sat=25_000)
    assert bare.offline_open_allowance == 0
    assert bare.offline_discount_after_sec == 0.0
    dead = make_snapshot([make_channel(), dead_channel(channel_id="bb" * 32)])
    assert effective_channel_count(dead, bare) == 2
    assert not has_room_for_another_channel(dead, bare)
