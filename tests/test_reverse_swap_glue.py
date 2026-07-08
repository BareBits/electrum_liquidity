"""Glue-level tests for the reverse-swap executor branches not covered by the
targeting test (test_glue_providers) or the timeout test (test_reverse_swap_timeout).

Covered here:
  * per-channel swap cooldown suppresses a re-attempt entirely;
  * no provider configured (empty npub + no SWAPSERVER_*) -> skip;
  * an amount the provider cannot host (get_recv_amount -> None) -> skip, no RPC;
  * SwapServerError from the provider -> a SOFT provider fault (transient capacity);
  * any OTHER exception -> internal error, provider NOT faulted (documents current
    behaviour; Phase 3 revisits which of these are really provider-caused);
  * a funded swap -> provider success recorded AND dev fee accrued;
  * an accepted-but-not-yet-funded swap -> tracked for reconciliation, no success/fee yet.

Heavy Electrum objects are faked; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.submarine_swaps import SwapServerError  # type: ignore  # noqa: E402
from electrum.util import UserFacingException  # type: ignore  # noqa: E402  (imported for parity)
from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ReverseSwapAction,
)

NPUB = "npubCHOSENPROVIDER"


def _plugin(**config_over) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.rswap_glue")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p._reverse_swap_timeout_sec = 5.0
    cfg = dict(SWAPSERVER_NPUB=None, SWAPSERVER_URL=None)
    cfg.update(config_over)
    p.config = SimpleNamespace(**cfg)
    # Spies for every side-effecting collaborator the branches touch.
    p.faults = []       # (npub, reason, kwargs)
    p.successes = []     # npub
    p.dev_fees = []      # (amount_sat, source)
    p.tracked = []       # (swaps_before, npub)
    p.diags = []         # _diag_event kwargs
    p.logged = []        # _log_action kwargs
    p._record_provider_fault = lambda wallet, npub, reason, **kw: \
        p.faults.append((npub, reason, kw))
    p._record_provider_success = lambda wallet, npub: p.successes.append(npub)
    p._accrue_dev_fee = lambda wallet, amount_sat, source=None: \
        p.dev_fees.append((amount_sat, source))
    p._track_new_swaps = lambda wallet, sm, before, npub, action, exp: \
        p.tracked.append((npub, exp))
    p._diag_event = lambda wallet, **kw: p.diags.append(kw)
    p._log_action = lambda wallet, **kw: p.logged.append(kw)
    p.on_action_done = lambda wallet, msg: None
    return p


def _sm(reverse_swap) -> SimpleNamespace:
    sm = SimpleNamespace(
        mining_fee=1_000,
        is_initialized=asyncio.Event(),
        update_pairs=lambda pairs: None,
        get_recv_amount=lambda amt, *, is_reverse: amt - 500,
        reverse_swap=reverse_swap,
        _swaps={})
    sm.is_initialized.set()
    return sm


def _wallet(sm) -> SimpleNamespace:
    return SimpleNamespace(lnworker=SimpleNamespace(
        swap_manager=sm,
        channels={},
        get_channel_by_id=lambda cid: SimpleNamespace(node_id=b"\x11" * 33)))


def _transport():
    offer = SimpleNamespace(server_pubkey="ab" * 32, pairs=SimpleNamespace())
    return SimpleNamespace(target_pubkey=None,
                           get_offer=lambda npub: offer if npub == NPUB else None)


def _action(npub: str = NPUB, channel_id: str = "aa" * 32) -> ReverseSwapAction:
    return ReverseSwapAction(channel_id=channel_id, short_id="1x1x1",
                             lightning_amount_sat=400_000, reason="drain",
                             provider_npub=npub)


def _run(p, wallet, action, transport):
    asyncio.run(p._reverse_swap(wallet, action, state={}, transport=transport))


# --- cooldown -------------------------------------------------------------
def test_cooldown_suppresses_reattempt() -> None:
    calls = []

    async def _rs(**kw):
        calls.append(kw)
        return "txid"
    p = _plugin()
    action = _action()
    p._swap_cooldown_until[action.channel_id] = time.monotonic() + 100  # cooling down
    _run(p, _wallet(_sm(_rs)), action, _transport())
    assert calls == []                    # never attempted the swap
    assert p.faults == [] and p.successes == []


# --- no provider configured ----------------------------------------------
def test_no_provider_configured_skips() -> None:
    calls = []

    async def _rs(**kw):
        calls.append(kw)
        return "txid"
    # Empty npub + no SWAPSERVER_* -> nothing to swap with.
    p = _plugin(SWAPSERVER_NPUB=None, SWAPSERVER_URL=None)
    _run(p, _wallet(_sm(_rs)), _action(npub=""), _transport())
    assert calls == [] and p.faults == []


# --- amount not swappable -------------------------------------------------
def test_amount_none_guard_skips_rpc() -> None:
    calls = []

    async def _rs(**kw):
        calls.append(kw)
        return "txid"
    sm = _sm(_rs)
    sm.get_recv_amount = lambda amt, *, is_reverse: None    # unswappable amount
    p = _plugin()
    _run(p, _wallet(sm), _action(), _transport())
    assert calls == []                    # no RPC issued
    assert p.faults == []                 # not a provider fault ("wait/retry")
    assert any("not swappable" in d.get("reason", "") for d in p.diags)


# --- provider rejects (SwapServerError) -> soft fault ---------------------
def test_swap_server_error_is_soft_fault() -> None:
    async def _rs(**kw):
        raise SwapServerError()
    p = _plugin()
    _run(p, _wallet(_sm(_rs)), _action(), _transport())
    assert len(p.faults) == 1
    npub, reason, kw = p.faults[0]
    assert npub == NPUB and kw.get("soft") is True and "transient capacity" in reason
    assert p.successes == [] and p.dev_fees == []


# --- exception classification (Phase 3) -----------------------------------
def test_generic_our_side_exception_does_not_fault_provider() -> None:
    # An exception carrying none of the cheat markers is treated as our-side
    # (a bug / local condition): logged as internal error, provider NOT faulted.
    async def _rs(**kw):
        raise RuntimeError("bad argument on our side")
    p = _plugin()
    _run(p, _wallet(_sm(_rs)), _action(), _transport())
    assert p.faults == []
    assert p.successes == []
    assert any(d.get("reason") == "reverse swap internal error" for d in p.diags)


@pytest.mark.parametrize("msg", [
    # Electrum's real pre-payment sanity-check messages (submarine_swaps.py).
    "rswap check failed: onchain_amount is less than what we expected: 100 < 200",
    "rswap check failed: inconsistent RHASH and invoice",
    "rswap check failed: invoice_amount (399000) not what we requested (400000)",
])
def test_provider_cheat_records_escalating_fault(msg) -> None:
    # An unambiguous cheat caught before any payment -> an escalating (hard)
    # provider fault so repeat offenders sink toward a ban. No funds moved, so
    # no success/dev-fee.
    async def _rs(**kw):
        raise Exception(msg)
    p = _plugin()
    _run(p, _wallet(_sm(_rs)), _action(), _transport())
    assert len(p.faults) == 1
    npub, reason, kw = p.faults[0]
    assert npub == NPUB
    assert kw.get("soft") is not True          # escalating, not a soft fault
    assert "sanity check" in reason
    assert p.successes == [] and p.dev_fees == []
    assert any("possible cheat" in d.get("reason", "") for d in p.diags)


@pytest.mark.parametrize("msg", [
    "our blockchain tip is stale",             # our node is behind
    "rswap check failed: locktime too close",  # locktime vs OUR height
])
def test_our_side_swap_condition_does_not_fault_provider(msg) -> None:
    # Ambiguous / our-side conditions must never penalise the provider.
    async def _rs(**kw):
        raise Exception(msg)
    p = _plugin()
    _run(p, _wallet(_sm(_rs)), _action(), _transport())
    assert p.faults == []
    assert any(d.get("reason") == "reverse swap internal error" for d in p.diags)


# --- funded swap -> success recorded AND dev fee accrued ------------------
def test_funded_swap_records_success_and_dev_fee() -> None:
    async def _rs(**kw):
        return "funding-txid-abc"        # provider created the funding output
    p = _plugin()
    _run(p, _wallet(_sm(_rs)), _action(), _transport())
    assert p.successes == [NPUB]
    # dev fee accrues on the net on-chain amount (get_recv_amount: 400_000-500).
    assert p.dev_fees == [(399_500, "1x1x1")]
    assert p.tracked == []               # funded now, nothing to reconcile
    assert len(p.logged) == 1


# --- accepted but not yet funded -> tracked, no success/fee yet -----------
def test_accepted_not_funded_is_tracked() -> None:
    async def _rs(**kw):
        return None                      # accepted, funding not created yet
    p = _plugin()
    _run(p, _wallet(_sm(_rs)), _action(), _transport())
    assert p.successes == [] and p.dev_fees == []
    assert p.tracked == [(NPUB, 399_500)]    # queued for reconciliation
    assert len(p.logged) == 1


# --- payment is pinned to the target channel ------------------------------
def test_reverse_swap_pins_payment_to_target_channel() -> None:
    """The engine drains a specific channel; the reverse swap's Lightning
    payment must be restricted to that channel (channels=[chan]). Otherwise,
    when several channels share a peer, the payment can drain the wrong one and
    the target channel never empties (see the multi-channel drain bug)."""
    captured = {}

    async def _rs(**kw):
        captured.update(kw)
        return "funding-txid-abc"

    action = _action(channel_id="cc" * 32)
    sentinel_chan = SimpleNamespace(node_id=b"\x22" * 33, tag="TARGET")
    sm = _sm(_rs)
    wallet = SimpleNamespace(lnworker=SimpleNamespace(
        swap_manager=sm, channels={},
        # Only resolves for THIS action's channel id, proving the right one is used.
        get_channel_by_id=lambda cid: sentinel_chan
        if cid == bytes.fromhex(action.channel_id) else None))
    p = _plugin()
    asyncio.run(p._reverse_swap(wallet, action, state={}, transport=_transport()))
    assert captured.get("channels") == [sentinel_chan]


# --- unresolvable channel falls back to unpinned routing ------------------
def test_reverse_swap_unresolvable_channel_routes_unpinned() -> None:
    """If the target channel can't be resolved, the swap still proceeds with no
    `channels` restriction (graceful degradation, never abort the swap)."""
    captured = {}

    async def _rs(**kw):
        captured.update(kw)
        return "funding-txid-abc"

    sm = _sm(_rs)
    wallet = SimpleNamespace(lnworker=SimpleNamespace(
        swap_manager=sm, channels={},
        get_channel_by_id=lambda cid: None))   # cannot resolve
    p = _plugin()
    asyncio.run(p._reverse_swap(wallet, _action(), state={}, transport=_transport()))
    assert "channels" not in captured           # unpinned
    assert p.successes == [NPUB]                 # but the swap still completed
