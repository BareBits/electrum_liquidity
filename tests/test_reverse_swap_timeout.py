"""Stability tests for the reverse-swap timeout hardening.

Covers three fixes that stop an unresponsive (but connected) swap provider from
wedging the plugin, and a snapshot that no longer aborts on one bad provider
field:

  * the transport-level bound on the createswap RPC reply
    (``TargetedNostrTransport.send_request_to_server``): a provider that ACKs the
    request over Nostr but never replies now raises ``asyncio.TimeoutError`` and
    the dangling reply slot is cleaned up (instead of awaiting a Future forever);
  * the coarse backstop around the whole ``sm.reverse_swap`` call in
    ``_reverse_swap``: a swap that stalls past ``_reverse_swap_timeout_sec`` is
    recorded as a provider reliability fault and any already-created swap is
    still tracked for reconciliation (so its outcome is not lost);
  * ``build_snapshot`` degrades a single failing provider-economics field to
    None rather than letting it abort the whole snapshot.

Heavy Electrum objects are faked; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.swap_transport import (  # type: ignore  # noqa: E402
    TargetedNostrTransport,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ReverseSwapAction,
)

NPUB = "npubSLOWPROVIDER"


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, sm=None) -> None:
        self.db = _FakeDB()
        self.saved = 0
        chan = SimpleNamespace(node_id=b"\x11" * 33)
        self.lnworker = SimpleNamespace(
            swap_manager=sm,
            channels={},
            get_channel_by_id=lambda cid: chan)

    def save_db(self) -> None:
        self.saved += 1

    def get_spendable_balance_sat(self) -> int:
        return 0

    def basename(self) -> str:
        return "liqtest"


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.rswaptimeout")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p._reverse_swap_timeout_sec = 0.05     # shrink so a stall trips at once
    p.config = SimpleNamespace()           # getattr defaults kick in
    return p


def _transport():
    offer = SimpleNamespace(server_pubkey="srvpub", pairs=SimpleNamespace())
    return SimpleNamespace(target_pubkey=None,
                           get_offer=lambda npub: offer if npub == NPUB else None)


def _action() -> ReverseSwapAction:
    return ReverseSwapAction(channel_id="aa" * 32, short_id="1x1x1",
                             lightning_amount_sat=400_000, reason="drain",
                             provider_npub=NPUB)


def _swap_manager(reverse_swap) -> SimpleNamespace:
    sm = SimpleNamespace(
        mining_fee=1_000,
        is_initialized=asyncio.Event(),
        update_pairs=lambda pairs: None,
        get_recv_amount=lambda amt, *, is_reverse: amt - 500,
        reverse_swap=reverse_swap,
        _swaps={},
    )
    sm.is_initialized.set()
    return sm


# --- transport-level RPC reply timeout ------------------------------------
def test_transport_rpc_reply_timeout_raises_and_cleans_up() -> None:
    """A provider that never replies to a swap RPC must not hang the caller: the
    reply wait is bounded and the dangling reply slot is removed on timeout."""
    t = object.__new__(TargetedNostrTransport)
    t.logger = logging.getLogger("test.inbound_liquidity.transport")
    t.target_pubkey = "ab" * 32
    t.dm_replies = {}
    t.RPC_REPLY_TIMEOUT_SEC = 0.05          # instance override; reply never comes

    async def _send_dm(pubkey, content, *, retries=0):
        return "event-id-1"                 # DM published, but no reply will arrive
    t.send_direct_message = _send_dm

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(t.send_request_to_server("createswap", {"foo": "bar"}))
    # No dangling reply Future left behind (else a late reply would resolve a
    # Future nobody awaits, and the dict would grow without bound).
    assert t.dm_replies == {}


# --- reverse-swap coarse backstop -----------------------------------------
def test_reverse_swap_backstop_times_out_records_fault_and_tracks_swap() -> None:
    """A reverse swap that stalls past the backstop is charged to the provider as
    a (escalating) reliability fault, and a swap object created before the stall
    is still tracked so reconciliation can resolve it later."""
    created_hash = "cd" * 32

    async def _hanging(**kw):
        # The swap object is created (payment leg started) and then the attempt
        # stalls indefinitely -- exactly what the backstop must cut off.
        sm._swaps[created_hash] = object()
        await asyncio.sleep(30)
        return "unreachable"

    p = _plugin()
    sm = _swap_manager(_hanging)
    w = _FakeWallet(sm)

    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))

    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 1          # escalating fault (not soft)
    assert stats["fault_count"] == 1
    assert "timeout" in stats["last_reason"].lower()
    # The swap created before the stall is tracked for later reconciliation.
    pending = p._load_pending_swaps(w)
    assert created_hash in pending
    assert pending[created_hash]["npub"] == NPUB


def test_reverse_swap_backstop_no_swap_created_still_faults() -> None:
    """If the stall happens before any swap object exists (the createswap RPC
    reply never came), the provider is still faulted and nothing is tracked."""
    async def _hang_before_create(**kw):
        await asyncio.sleep(30)              # never even creates a swap
        return "unreachable"

    p = _plugin()
    sm = _swap_manager(_hang_before_create)
    w = _FakeWallet(sm)

    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))

    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert p._load_pending_swaps(w) == {}


# --- build_snapshot degrades one bad provider field -----------------------
def test_build_snapshot_survives_provider_field_error() -> None:
    """A raising provider-economics accessor degrades that one field to None; the
    snapshot is still built (so it can still carry an unrelated open decision)."""
    def _boom():
        raise RuntimeError("provider terms not ready")

    sm = SimpleNamespace(
        get_pending_swaps=lambda: [],
        percentage=0.3,
        mining_fee=0,
        get_fee_for_txbatcher=lambda: 0,
        get_provider_max_forward_amount=_boom,   # <- raises
        get_min_amount=lambda: 200_000,
    )
    p = _plugin()
    w = _FakeWallet(sm)

    snap = p.build_snapshot(w, transport=None)          # must not raise
    assert snap.provider_max_reverse_sat is None        # degraded field
    assert snap.swap_percentage_fee == pytest.approx(0.3)   # others still populated
    assert snap.provider_min_amount_sat == 200_000
