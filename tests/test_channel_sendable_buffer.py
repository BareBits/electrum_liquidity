"""The swap amount must never exceed what Electrum will actually send.

``chan.available_to_spend(LOCAL)`` is a raw ceiling: balance minus the channel
reserve and the commitment/fee-spike buffer. It is NOT Electrum's answer to "how
much can I send", because a Lightning payment also needs a routing-fee budget on
top of the amount, taken from the same balance. Electrum's own max-send
(``lnworker.num_sats_can_send``) subtracts
``estimate_fee_reserve_for_total_amount()`` from that ceiling, and the estimator's
docstring names this exact case ("reliably allow doing a 'Max' amount lightning
send (e.g. for submarine swaps)").

The plugin used the raw ceiling, so with the default ``min_outbound_sat=0`` it
planned swaps for strictly more than Electrum believed it could send. That does
not fail loudly: the provider splits our ``lightning_amount_sat`` into a main hold
invoice plus a mining-fee prepayment invoice and only creates the on-chain funding
output once BOTH HTLCs arrive, so a leg that cannot be routed leaves the swap
accepted-but-never-funded until the stuck-swap timeout blames the provider.

These tests pin: the per-channel sendable subtracts the estimator's reserve; the
snapshot carries the reduced figure; a missing/raising estimator degrades to the
raw ceiling rather than blocking swaps; and the planned swap amount stays within
Electrum's own limit.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.lnutil import LOCAL, REMOTE  # type: ignore  # noqa: E402
from electrum.lnchannel import ChannelState  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    LiquidityConfig,
    ReverseSwapAction,
    evaluate,
)

# Electrum's defaults: 1% of the amount, floored at 10 sat
# (LIGHTNING_PAYMENT_FEE_MAX_MILLIONTHS / LIGHTNING_PAYMENT_FEE_CUTOFF_MSAT).
ONE_PERCENT_MILLIONTHS = 10_000
CUTOFF_MSAT = 10_000


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _Chan:
    def __init__(self, *, cid: bytes, short: str, capacity: int, local_sat: int,
                 spendable_sat: int) -> None:
        self.channel_id = cid
        self.short_channel_id = short
        self._capacity = capacity
        self._local_sat = local_sat
        self._spendable_sat = spendable_sat
        self.hm = SimpleNamespace(htlcs=lambda subject: [])

    def get_capacity(self) -> int:
        return self._capacity

    def get_state(self):
        return ChannelState.OPEN

    def has_unsettled_htlcs(self) -> bool:
        return False

    def balance(self, subject) -> int:
        return (self._local_sat * 1000 if subject == LOCAL
                else (self._capacity - self._local_sat) * 1000)

    def available_to_spend(self, subject) -> int:
        return self._spendable_sat * 1000

    def is_active(self) -> bool:
        return True


def _real_reserve(amount_sat: int) -> int:
    """Electrum's estimator, replicated for the expected value in assertions.

    Mirrors ``PaymentFeeBudget.reverse_from_total_amount`` at Electrum's default
    config: the fee share of a total that already includes it, floored at cutoff.
    """
    total_msat = amount_sat * 1000
    amount_minus_fees = (total_msat * 1_000_000) // (1_000_000 + ONE_PERCENT_MILLIONTHS)
    fees_msat = max(total_msat - amount_minus_fees, CUTOFF_MSAT)
    fees_msat = min(fees_msat, total_msat)
    return -(-fees_msat // 1000)     # ceil to sat


class _Lnworker:
    """lnworker exposing Electrum's real fee-reserve estimator semantics."""

    def __init__(self, channels, sm, *, raises=False) -> None:
        self.channels = {c.channel_id: c for c in channels}
        self.swap_manager = sm
        self._raises = raises

    def estimate_fee_reserve_for_total_amount(self, amount_sat) -> int:
        if self._raises:
            raise RuntimeError("no fee estimates")
        return _real_reserve(int(amount_sat))


class _Lnworker_NoEstimator:
    def __init__(self, channels, sm) -> None:
        self.channels = {c.channel_id: c for c in channels}
        self.swap_manager = sm


class _Wallet:
    def __init__(self, lnworker) -> None:
        self.db = _FakeDB()
        self.lnworker = lnworker

    def get_spendable_balance_sat(self) -> int:
        return 0

    def basename(self) -> str:
        return "liqtest"


def _swap_manager(*, max_forward=10_000_000, min_amount=20_000, pct=0.1, mining=0):
    return SimpleNamespace(
        get_pending_swaps=lambda: [],
        percentage=pct,
        mining_fee=mining,
        get_fee_for_txbatcher=lambda: 0,
        get_provider_max_forward_amount=lambda: max_forward,
        get_min_amount=lambda: min_amount,
        get_swap=lambda ph: None,
    )


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.sendable")
    p._last_offers = {}
    p.config = SimpleNamespace()
    return p


def _config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=1_000_000,
        onchain_reserve_sat=10_000,
        max_channels=5,
        max_swap_fee_pct=5.0,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


# --- the unit: per-channel sendable ---------------------------------------
def test_sendable_subtracts_electrums_fee_reserve() -> None:
    chan = _Chan(cid=b"\x01" * 32, short="1x1x1", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    sendable = LiquidityPlugin._channel_sendable_sat(ln, chan)
    reserve = _real_reserve(1_000_000)
    assert reserve > 0                                   # the estimator does bite
    assert sendable == 1_000_000 - reserve
    assert sendable < 1_000_000                          # strictly below the raw ceiling


def test_sendable_matches_electrums_own_max_send_formula() -> None:
    """Same arithmetic Electrum uses in num_sats_can_send: raw - reserve(raw)."""
    chan = _Chan(cid=b"\x02" * 32, short="1x1x2", capacity=4_000_000,
                 local_sat=2_000_000, spendable_sat=1_987_654)
    ln = _Lnworker([chan], _swap_manager())
    raw = chan.available_to_spend(LOCAL) // 1000
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == raw - _real_reserve(raw)


def test_sendable_degrades_to_raw_when_estimator_absent() -> None:
    """An Electrum without the estimator must not stop swaps outright."""
    chan = _Chan(cid=b"\x03" * 32, short="1x1x3", capacity=2_000_000,
                 local_sat=900_000, spendable_sat=800_000)
    ln = _Lnworker_NoEstimator([chan], _swap_manager())
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == 800_000


def test_sendable_degrades_to_raw_when_estimator_raises() -> None:
    chan = _Chan(cid=b"\x04" * 32, short="1x1x4", capacity=2_000_000,
                 local_sat=900_000, spendable_sat=800_000)
    ln = _Lnworker([chan], _swap_manager(), raises=True)
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == 800_000


def test_sendable_never_negative() -> None:
    """A tiny balance whose reserve exceeds it clamps at 0, not below."""
    chan = _Chan(cid=b"\x05" * 32, short="1x1x5", capacity=2_000_000,
                 local_sat=5, spendable_sat=5)
    ln = _Lnworker([chan], _swap_manager())
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == 0


# --- through the real snapshot --------------------------------------------
def test_snapshot_carries_the_reduced_sendable() -> None:
    chan = _Chan(cid=b"\x06" * 32, short="1x1x6", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    p, w = _plugin(), _Wallet(ln)
    snap = p.build_snapshot(w, transport=None)
    (cs,) = snap.channels
    assert cs.local_sat == 1_000_000                     # balance untouched
    assert cs.spendable_local_sat == 1_000_000 - _real_reserve(1_000_000)


# --- the regression: planned amount stays within Electrum's limit ---------
def test_planned_swap_amount_fits_what_electrum_can_send() -> None:
    """With min_outbound_sat=0 (the default) the swap used to be planned at the
    raw ceiling -- more than Electrum would send, so one of the swap's two
    Lightning legs could not be routed and the swap hung unfunded."""
    chan = _Chan(cid=b"\x07" * 32, short="1x1x7", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    p, w = _plugin(), _Wallet(ln)

    npub = "npub1" + "a" * 58
    offer = SimpleNamespace(
        server_npub=npub, pow_bits=0,
        pairs=SimpleNamespace(percentage=0.1, mining_fee=0,
                              min_amount=20_000, max_forward=10_000_000))
    transport = SimpleNamespace(get_recent_offers=lambda: [offer])

    snap = p.build_snapshot(w, transport=transport)
    result = evaluate(snap, _config(min_outbound_sat=0))
    (swap,) = [a for a in result.actions if isinstance(a, ReverseSwapAction)]

    electrum_max_send = 1_000_000 - _real_reserve(1_000_000)
    assert swap.lightning_amount_sat <= electrum_max_send
    # And it is still a worthwhile swap, not shaved to nothing.
    assert swap.lightning_amount_sat > 0.9 * electrum_max_send


def test_outbound_floor_still_applies_on_top_of_the_reserve() -> None:
    """The reserve and the operator's min_outbound_sat floor compose; the floor
    is measured against the honest sendable, not the raw ceiling."""
    chan = _Chan(cid=b"\x08" * 32, short="1x1x8", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    p, w = _plugin(), _Wallet(ln)
    npub = "npub1" + "b" * 58
    offer = SimpleNamespace(
        server_npub=npub, pow_bits=0,
        pairs=SimpleNamespace(percentage=0.1, mining_fee=0,
                              min_amount=20_000, max_forward=10_000_000))
    snap = p.build_snapshot(w, transport=SimpleNamespace(get_recent_offers=lambda: [offer]))
    floor = 100_000
    result = evaluate(snap, _config(min_outbound_sat=floor))
    (swap,) = [a for a in result.actions if isinstance(a, ReverseSwapAction)]
    sendable = 1_000_000 - _real_reserve(1_000_000)
    assert swap.lightning_amount_sat == sendable - floor
