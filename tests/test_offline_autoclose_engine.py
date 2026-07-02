"""Pure-engine tests for the offline-channel auto-close metric: the rolling,
time-weighted peer-uptime accumulator (bucketing, window pruning, sample-gap
cap), the uptime ratio + observation-confidence gate, the commit decision, and
the force-close deadline. All clock-free -- ``now`` is passed in -- so no Electrum
and no wall clock are involved (``conftest`` puts the engine module on path)."""
from __future__ import annotations

import liquidity_manager as lm

HOUR = 3600.0
DAY = 86400.0


# --- record_uptime_sample -------------------------------------------------
def test_first_sample_records_no_interval() -> None:
    acc = lm.record_uptime_sample(None, now=1000.0, online=True, window_sec=DAY)
    assert acc["last_ts"] == 1000.0
    assert acc["last_online"] is True
    assert acc["buckets"] == {}          # nothing to attribute yet


def test_interval_attributed_to_previous_state() -> None:
    # Online at t=0, still online sampled 10 min later: the 600s interval is
    # attributed to the previous state (online).
    acc = lm.record_uptime_sample(None, now=0.0, online=True, window_sec=DAY)
    acc = lm.record_uptime_sample(acc, now=600.0, online=True, window_sec=DAY)
    (online_frac, observed_frac) = lm.uptime_ratio(acc, now=600.0, window_sec=DAY)
    assert online_frac == 1.0
    assert observed_frac == 600.0 / DAY


def test_offline_interval_lowers_ratio() -> None:
    # 10 min online, then 10 min offline -> 50% uptime over the observed 20 min.
    acc = lm.record_uptime_sample(None, now=0.0, online=True, window_sec=DAY)
    acc = lm.record_uptime_sample(acc, now=600.0, online=False, window_sec=DAY)  # prev=online
    acc = lm.record_uptime_sample(acc, now=1200.0, online=False, window_sec=DAY)  # prev=offline
    online_frac, _ = lm.uptime_ratio(acc, now=1200.0, window_sec=DAY)
    assert online_frac == 0.5


def test_sample_gap_is_capped() -> None:
    # A huge gap between samples (our own downtime) is capped at max_gap_sec, so
    # it is not fully charged against the peer.
    acc = lm.record_uptime_sample(None, now=0.0, online=False, window_sec=DAY,
                                  max_gap_sec=900.0)
    acc = lm.record_uptime_sample(acc, now=10 * HOUR, online=False, window_sec=DAY,
                                  max_gap_sec=900.0)
    _, observed_frac = lm.uptime_ratio(acc, now=10 * HOUR, window_sec=DAY)
    # Only 900s of the 10h gap was counted.
    assert observed_frac == 900.0 / DAY


def test_buckets_prune_to_window() -> None:
    # Sample far in the past then far in the future: the old bucket falls out of
    # the trailing window and is dropped.
    acc = lm.record_uptime_sample(None, now=0.0, online=True, window_sec=2 * HOUR)
    acc = lm.record_uptime_sample(acc, now=600.0, online=True, window_sec=2 * HOUR)
    assert acc["buckets"]                          # something recorded
    # Advance 5 hours with a fresh sample; window is only 2h.
    acc = lm.record_uptime_sample(acc, now=5 * HOUR, online=True, window_sec=2 * HOUR)
    acc = lm.record_uptime_sample(acc, now=5 * HOUR + 600, online=True, window_sec=2 * HOUR)
    # The original t~0 bucket is now outside the 2h window.
    ratio = lm.uptime_ratio(acc, now=5 * HOUR + 600, window_sec=2 * HOUR)
    assert ratio is not None
    _, observed_frac = ratio
    assert observed_frac == 600.0 / (2 * HOUR)     # only the recent interval counts


# --- uptime_ratio ---------------------------------------------------------
def test_ratio_none_when_no_observation() -> None:
    assert lm.uptime_ratio(None, now=0.0, window_sec=DAY) is None
    assert lm.uptime_ratio({}, now=0.0, window_sec=DAY) is None
    # An accumulator whose only bucket is outside the window -> None.
    acc = lm.record_uptime_sample(None, now=0.0, online=True, window_sec=HOUR)
    acc = lm.record_uptime_sample(acc, now=600.0, online=True, window_sec=HOUR)
    assert lm.uptime_ratio(acc, now=100 * HOUR, window_sec=HOUR) is None


# --- should_commit_offline_close ------------------------------------------
def test_commit_requires_enough_observation() -> None:
    # Low uptime but too little of the window observed -> no commit yet.
    ratio = (0.0, 0.10)   # 0% uptime, only 10% of window observed
    assert not lm.should_commit_offline_close(
        ratio, min_uptime_pct=10.0, min_observed_frac=0.25)


def test_commit_when_uptime_below_floor_and_observed() -> None:
    ratio = (0.05, 0.5)   # 5% uptime over half the window
    assert lm.should_commit_offline_close(
        ratio, min_uptime_pct=10.0, min_observed_frac=0.25)


def test_no_commit_when_uptime_above_floor() -> None:
    ratio = (0.5, 1.0)    # healthy-ish peer -> spared
    assert not lm.should_commit_offline_close(
        ratio, min_uptime_pct=10.0, min_observed_frac=0.25)


def test_commit_none_ratio_is_false() -> None:
    assert not lm.should_commit_offline_close(
        None, min_uptime_pct=10.0, min_observed_frac=0.25)


def test_commit_floor_is_strict_less_than() -> None:
    # Exactly at the floor is NOT below it -> spared.
    ratio = (0.10, 1.0)
    assert not lm.should_commit_offline_close(
        ratio, min_uptime_pct=10.0, min_observed_frac=0.25)


# --- deadline_reached -----------------------------------------------------
def test_deadline_not_reached_when_unmarked() -> None:
    assert not lm.deadline_reached(None, now=1e9, deadline_sec=DAY)


def test_deadline_reached_after_elapsed() -> None:
    assert lm.deadline_reached(1000.0, now=1000.0 + 7 * DAY, deadline_sec=7 * DAY)
    assert not lm.deadline_reached(1000.0, now=1000.0 + DAY, deadline_sec=7 * DAY)


def test_zero_deadline_reached_immediately_once_committed() -> None:
    assert lm.deadline_reached(1000.0, now=1000.0, deadline_sec=0.0)
