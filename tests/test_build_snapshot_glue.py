"""End-to-end (glue -> engine) integration tests that drive the REAL
``build_snapshot`` against faked Electrum objects and then run the real
``evaluate``, so the whole decision path is exercised for the three fixes:

  * issue #1: a channel whose unsettled HTLC is the in-flight leg of a reverse
    swap we issued is marked ``unsettled_is_swap`` in the snapshot and declined
    as "our swap still settling", not "possible stuck payment";
  * issues #2/#3: two over-trigger channels against a single provider with room
    for one swap yield one swap action and one *benign* "capacity committed this
    cycle" decline in the SAME evaluation (all eligible channels considered at
    once; the second is not sent, so it cannot become a spurious RPC fault).

Skipped outside the Electrum venv (needs the real lnutil / lnchannel enums).
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


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeChan:
    def __init__(self, *, cid: bytes, short: str, capacity: int, local_msat: int,
                 spendable: int, unsettled_hashes: Optional[List[bytes]] = None) -> None:
        self.channel_id = cid
        self.short_channel_id = short
        self._capacity = capacity
        self._local_msat = local_msat
        self._spendable = spendable
        self._unsettled = unsettled_hashes or []
        self.hm = SimpleNamespace(
            htlcs=lambda subject: [("dir", SimpleNamespace(payment_hash=ph))
                                   for ph in self._unsettled])

    def get_capacity(self) -> int:
        return self._capacity

    def get_state(self):
        return ChannelState.OPEN

    def has_unsettled_htlcs(self) -> bool:
        return bool(self._unsettled)

    def balance(self, subject) -> int:
        return self._local_msat if subject == LOCAL else (self._capacity * 1000 - self._local_msat)

    def available_to_spend(self, subject) -> int:
        return self._spendable * 1000

    def is_active(self) -> bool:
        return True


def _swap_manager(*, known_swap_hashes=(), max_forward=1_000_000, min_amount=200_000):
    known = set(known_swap_hashes)
    return SimpleNamespace(
        get_pending_swaps=lambda: [],
        percentage=0.3,
        mining_fee=0,
        get_fee_for_txbatcher=lambda: 0,
        get_provider_max_forward_amount=lambda: max_forward,
        get_min_amount=lambda: min_amount,
        get_swap=lambda ph: object() if ph in known else None,
    )


class _FakeWallet:
    """A real (hashable-by-identity) wallet; the plugin keys per-wallet dicts on
    the wallet object, so a SimpleNamespace would blow up on ``dict[wallet]``."""
    def __init__(self, channels, sm) -> None:
        self.db = _FakeDB()
        self.lnworker = SimpleNamespace(
            channels={c.channel_id: c for c in channels}, swap_manager=sm)

    def get_spendable_balance_sat(self) -> int:
        return 0

    def basename(self) -> str:
        return "liqtest"


def _wallet(channels, sm) -> _FakeWallet:
    return _FakeWallet(channels, sm)


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.snapshot")
    p._last_offers = {}
    p.config = SimpleNamespace()      # getattr defaults kick in
    return p


def _config(**overrides) -> LiquidityConfig:
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


def _transport_with_offer(npub: str, *, pct=0.3, mining=0, lo=200_000, max_forward=1_000_000):
    offer = SimpleNamespace(
        server_npub=npub, pow_bits=0,
        pairs=SimpleNamespace(percentage=pct, mining_fee=mining,
                              min_amount=lo, max_forward=max_forward))
    return SimpleNamespace(get_recent_offers=lambda: [offer])


# --- issue #1: unsettled HTLC that is our own swap ------------------------
def test_build_snapshot_marks_our_swap_htlc_and_declines_benignly() -> None:
    swap_ph = b"\xaa" * 32
    chan = _FakeChan(cid=b"\x01" * 32, short="103x1x0", capacity=2_000_000,
                     local_msat=1_000_000 * 1000, spendable=900_000,
                     unsettled_hashes=[swap_ph])
    sm = _swap_manager(known_swap_hashes=[swap_ph])
    p, w = _plugin(), _wallet([chan], sm)

    snap = p.build_snapshot(w, transport=None)
    (cs,) = snap.channels
    assert cs.has_unsettled_htlcs is True
    assert cs.unsettled_is_swap is True

    result = evaluate(snap, _config())
    assert not [a for a in result.actions if isinstance(a, ReverseSwapAction)]
    swap_declines = [d for d in result.declines if d.kind == "swap"]
    assert swap_declines and "reverse swap we initiated is still in flight" in swap_declines[0].reason


def test_build_snapshot_marks_foreign_htlc_as_not_ours() -> None:
    chan = _FakeChan(cid=b"\x02" * 32, short="106x1x0", capacity=2_000_000,
                     local_msat=1_000_000 * 1000, spendable=900_000,
                     unsettled_hashes=[b"\xbb" * 32])
    sm = _swap_manager(known_swap_hashes=[])       # no swap matches the HTLC
    p, w = _plugin(), _wallet([chan], sm)
    snap = p.build_snapshot(w, transport=None)
    (cs,) = snap.channels
    assert cs.has_unsettled_htlcs is True
    assert cs.unsettled_is_swap is False


# --- issues #2/#3: batching through the real snapshot ---------------------
def test_two_channels_one_provider_batches_without_fault() -> None:
    a = _FakeChan(cid=b"\x03" * 32, short="103x1x0", capacity=2_000_000,
                  local_msat=1_000_000 * 1000, spendable=900_000)
    b = _FakeChan(cid=b"\x04" * 32, short="106x1x0", capacity=2_000_000,
                  local_msat=1_000_000 * 1000, spendable=900_000)
    sm = _swap_manager(max_forward=1_000_000, min_amount=200_000)
    p, w = _plugin(), _wallet([a, b], sm)

    npub = "npub1" + "a" * 58  # valid npub shape (see clean_npub)
    snap = p.build_snapshot(w, transport=_transport_with_offer(npub))
    result = evaluate(snap, _config())

    swaps = [act for act in result.actions if isinstance(act, ReverseSwapAction)]
    assert len(swaps) == 1                                # provider only had room for one
    assert swaps[0].provider_npub == npub
    committed = [d for d in result.declines
                 if d.kind == "swap" and "committed to earlier swaps this cycle" in d.reason]
    assert len(committed) == 1                            # the other channel: benign, not a fault
