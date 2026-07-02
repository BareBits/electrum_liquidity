"""Pure-engine tests for the optional dev fee: the fee computation, the
percentage clamp, and the batch/daily-cap payout decision. No Electrum needed
(the engine is import-free); runs anywhere.
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  # noqa: E402
    clamp_dev_fee_pct,
    compute_dev_fee,
    decide_dev_fee_payout,
)


# --- compute_dev_fee ------------------------------------------------------
def test_compute_dev_fee_basic() -> None:
    # 0.1% of 900_000 sat = 900 sat.
    assert compute_dev_fee(900_000, 0.1) == 900
    # 1% of 1_000_000 = 10_000.
    assert compute_dev_fee(1_000_000, 1.0) == 10_000


def test_compute_dev_fee_floors_never_overcharges() -> None:
    # 0.1% of 12_345 = 12.345 -> floored to 12 (favouring the operator).
    assert compute_dev_fee(12_345, 0.1) == 12
    # A sub-sat fee rounds down to nothing rather than up.
    assert compute_dev_fee(500, 0.1) == 0   # 0.5 sat -> 0


def test_compute_dev_fee_disabled_or_zero() -> None:
    assert compute_dev_fee(1_000_000, 0.0) == 0     # fee disabled
    assert compute_dev_fee(0, 0.1) == 0             # nothing swapped
    assert compute_dev_fee(-5, 0.1) == 0            # guard against negatives
    assert compute_dev_fee(1_000_000, -1.0) == 0    # negative pct -> 0


# --- clamp_dev_fee_pct ----------------------------------------------------
def test_clamp_dev_fee_pct() -> None:
    assert clamp_dev_fee_pct(0.1) == 0.1
    assert clamp_dev_fee_pct(-3.0) == 0.0       # below range -> 0
    assert clamp_dev_fee_pct(9.0) == 5.0        # above range -> max
    assert clamp_dev_fee_pct(5.0) == 5.0        # boundary kept
    assert clamp_dev_fee_pct(7.0, max_pct=10.0) == 7.0  # custom max


# --- decide_dev_fee_payout ------------------------------------------------
def test_payout_below_threshold_waits() -> None:
    d = decide_dev_fee_payout(999, 0, threshold_sat=1000, daily_cap_sat=10000)
    assert not d.should_pay
    assert d.amount_sat == 0


def test_payout_at_threshold_pays_full() -> None:
    d = decide_dev_fee_payout(1000, 0, threshold_sat=1000, daily_cap_sat=10000)
    assert d.should_pay
    assert d.amount_sat == 1000


def test_payout_capped_to_daily_headroom() -> None:
    # 8000 already paid today, 5000 owed -> only 2000 headroom left.
    d = decide_dev_fee_payout(5000, 8000, threshold_sat=1000, daily_cap_sat=10000)
    assert d.should_pay
    assert d.amount_sat == 2000   # remainder (3000) carries forward


def test_payout_suppressed_when_cap_exhausted() -> None:
    d = decide_dev_fee_payout(5000, 10000, threshold_sat=1000, daily_cap_sat=10000)
    assert not d.should_pay
    assert d.amount_sat == 0


def test_payout_cap_disabled_pays_everything() -> None:
    d = decide_dev_fee_payout(50_000, 40_000, threshold_sat=1000, daily_cap_sat=0)
    assert d.should_pay
    assert d.amount_sat == 50_000   # no cap -> whole balance


def test_payout_zero_threshold_pays_any_dust() -> None:
    d = decide_dev_fee_payout(1, 0, threshold_sat=0, daily_cap_sat=10000)
    assert d.should_pay
    assert d.amount_sat == 1
