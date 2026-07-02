"""Glue-level tests for provider reliability tracking (the Electrum-facing layer).

Exercise the persisted reliability store (record fault/success, decay penalty,
clear), the folding of penalties onto live offers, the fault classification in
_reverse_swap (timeout / RPC error = fault; provider decline = NOT a fault;
delivered swap = success), and the stuck-swap reconciliation (funded => success,
never-funded-past-timeout => fault). Heavy Electrum objects are faked; skipped if
the plugin package cannot be imported (outside the electrum venv).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    RELIABILITY_DB_KEY,
    PENDING_SWAPS_DB_KEY,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
    ReverseSwapAction,
)


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
        self.lnworker = SimpleNamespace(swap_manager=sm) if sm is not None else None

    def save_db(self) -> None:
        self.saved += 1


def _plugin() -> LiquidityPlugin:
    import logging
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.reliability")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p.config = SimpleNamespace()  # getattr defaults kick in
    return p


NPUB = "npubFLAKY"


# --- store: record / decay / clear ----------------------------------------
def test_record_fault_accumulates_and_penalises() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "init timeout")
    p._record_provider_fault(w, NPUB, "RPC error")
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 2
    assert stats["fault_count"] == 2
    # Two consecutive faults -> base * 2^(2-1) = 0.5 * 2 = 1.0% (fresh, no decay).
    rows = p.provider_reliability_rows(w)
    assert rows[NPUB]["penalty_pct"] == pytest.approx(1.0, abs=1e-3)


def test_success_resets_consecutive_faults() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")
    p._record_provider_fault(w, NPUB, "y")
    p._record_provider_success(w, NPUB)
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 0
    assert stats["success_count"] == 1
    assert stats["fault_count"] == 2          # lifetime fault tally is kept
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == 0.0


def test_penalty_decays_with_age() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")    # 1 fault -> 0.5% fresh
    # Backdate the fault by one half-life (6h) -> penalty halves to 0.25%.
    data = p._load_reliability(w)
    data[NPUB]["last_fault_ts"] = time.time() - 6 * 3600
    p._save_reliability(w, data)
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(0.25, abs=1e-3)


def test_clear_one_and_all() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, "a", "x")
    p._record_provider_fault(w, "b", "y")
    p.clear_provider_reliability(w, "a")
    assert set(p._load_reliability(w)) == {"b"}
    p.clear_provider_reliability(w)            # clear all
    assert p._load_reliability(w) == {}


def test_empty_npub_is_ignored() -> None:
    # Single-provider / URL mode has no per-provider identity to track.
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, "", "x")
    p._record_provider_success(w, "")
    assert p._load_reliability(w) == {}


# --- penalty folding onto offers ------------------------------------------
def test_apply_reliability_penalties_folds_decayed_penalty() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")     # 0.5% penalty
    offers = [ProviderOffer(npub=NPUB, percentage_fee=0.3, mining_fee_sat=0,
                            min_amount_sat=1, max_reverse_sat=10**9),
              ProviderOffer(npub="clean", percentage_fee=0.4, mining_fee_sat=0,
                            min_amount_sat=1, max_reverse_sat=10**9)]
    out = {o.npub: o for o in p._apply_reliability_penalties(w, offers)}
    assert out[NPUB].reliability_penalty_pct == pytest.approx(0.5, abs=1e-3)
    assert out["clean"].reliability_penalty_pct == 0.0


def test_apply_reliability_penalties_disabled_is_noop() -> None:
    p, w = _plugin(), _FakeWallet()
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_RELIABILITY_ENABLED=False)
    p._record_provider_fault(w, NPUB, "x")
    offers = [ProviderOffer(npub=NPUB, percentage_fee=0.3, mining_fee_sat=0,
                            min_amount_sat=1, max_reverse_sat=10**9)]
    out = p._apply_reliability_penalties(w, offers)
    assert out[0].reliability_penalty_pct == 0.0


# --- _reverse_swap fault classification -----------------------------------
def _swap_manager(reverse_swap, *, initialized=True) -> SimpleNamespace:
    sm = SimpleNamespace(
        mining_fee=1_000,
        is_initialized=asyncio.Event(),
        update_pairs=lambda pairs: None,
        get_recv_amount=lambda amt, *, is_reverse: amt - 500,
        reverse_swap=reverse_swap,
        _swaps={},
    )
    if initialized:
        sm.is_initialized.set()
    return sm


def _transport():
    offer = SimpleNamespace(server_pubkey="srvpub", pairs=SimpleNamespace())
    return SimpleNamespace(target_pubkey=None,
                           get_offer=lambda npub: offer if npub == NPUB else None)


def _action() -> ReverseSwapAction:
    return ReverseSwapAction(channel_id="aa" * 32, short_id="1x1x1",
                             lightning_amount_sat=400_000, reason="drain",
                             provider_npub=NPUB)


def test_reverse_swap_success_records_success() -> None:
    p, sm = _plugin(), _swap_manager(lambda **kw: _coro("funding-txid"))
    w = _FakeWallet(sm)
    p._log_action = lambda *a, **k: None
    p.on_action_done = lambda *a, **k: None
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 0


def test_reverse_swap_decline_is_not_a_fault() -> None:
    from electrum.util import UserFacingException

    def _raise(**kw):
        raise UserFacingException("uneconomical")
    p, sm = _plugin(), _swap_manager(lambda **kw: _raise(**kw))
    w = _FakeWallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w) == {}     # no fault recorded


def test_reverse_swap_rpc_error_is_a_fault() -> None:
    from electrum.submarine_swaps import SwapServerError

    def _raise(**kw):
        raise SwapServerError("boom")
    p, sm = _plugin(), _swap_manager(lambda **kw: _raise(**kw))
    w = _FakeWallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1


def test_reverse_swap_timeout_is_a_fault(monkeypatch) -> None:
    async def _never(**kw):
        return "unused"
    p, sm = _plugin(), _swap_manager(lambda **kw: _never(**kw), initialized=False)
    w = _FakeWallet(sm)

    async def _raise_timeout(awaitable, timeout):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(pkg.asyncio, "wait_for", _raise_timeout)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert "timeout" in p._load_reliability(w)[NPUB]["last_reason"].lower()


def test_reverse_swap_no_funding_tracks_pending() -> None:
    # Provider accepted (a swap row appears) but returned no funding txid yet:
    # tracked for the stuck reconciler, no success recorded.
    def _accept(**kw):
        sm._swaps["ph123"] = SimpleNamespace(is_redeemed=False, funding_txid=None)
        return _coro(None)
    p = _plugin()
    sm = _swap_manager(_accept)
    w = _FakeWallet(sm)
    p._log_action = lambda *a, **k: None
    p.on_action_done = lambda *a, **k: None
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    pending = p._load_pending_swaps(w)
    assert "ph123" in pending and pending["ph123"]["npub"] == NPUB
    assert NPUB not in p._load_reliability(w)        # no success yet


# --- stuck-swap reconciliation --------------------------------------------
def _reconcile_wallet(swap_obj) -> "_FakeWallet":
    sm = SimpleNamespace(_swaps={}, get_swap=lambda ph: swap_obj)
    return _FakeWallet(sm)


def test_reconcile_funded_swap_is_success() -> None:
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=False, funding_txid="txid"))
    w.db.put(PENDING_SWAPS_DB_KEY, {"abcd": {"npub": NPUB, "started_ts": time.time()}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    assert p._load_pending_swaps(w) == {}            # untracked


def test_reconcile_stuck_swap_is_fault() -> None:
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=False, funding_txid=None))
    # Started well past the default 60-min stuck timeout, never funded.
    w.db.put(PENDING_SWAPS_DB_KEY,
             {"abcd": {"npub": NPUB, "started_ts": time.time() - 4000}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert "stuck" in p._load_reliability(w)[NPUB]["last_reason"]
    assert p._load_pending_swaps(w) == {}


def test_reconcile_young_unfunded_swap_waits() -> None:
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=False, funding_txid=None))
    w.db.put(PENDING_SWAPS_DB_KEY,
             {"abcd": {"npub": NPUB, "started_ts": time.time()}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w) == {}              # nothing recorded yet
    assert "abcd" in p._load_pending_swaps(w)        # still tracked


def _coro(value):
    async def _c():
        return value
    return _c()
