"""Glue-level tests for the reverse-swap provider cascade.

When a swap fails, the executor walks the engine's ranked failover providers
within the SAME cycle instead of burning the 180s channel cooldown on a single
attempt. What makes this safe is a hard line drawn in Electrum's own
``reverse_swap`` (submarine_swaps.py): every pre-payment check raises BEFORE
``add_reverse_swap`` creates the swap object and fires ``pay_invoice``, so those
failures have moved no funds and can be retried elsewhere. Past that line the
Lightning payment may be in flight and retrying would drain the channel twice.

Covered here:
  * failover on each pre-payment failure class (unreachable, unswappable amount,
    provider decline, SwapServerError, cheat marker, too-close locktime);
  * NO failover once funds may be committed -- the safety-critical case;
  * abort (no failover) on provider-independent our-side conditions and on
    unrecognised errors;
  * the cascade stops at the first success, and honours its wall-clock deadline;
  * per-attempt sizing/targeting: each provider gets ITS amount and ITS pairs;
  * cooldown is armed once for the whole cascade, and not at all if every
    candidate had vanished.

Heavy Electrum objects are faked; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import List, Optional

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.submarine_swaps import SwapServerError  # type: ignore  # noqa: E402
from electrum.util import UserFacingException  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    SWAP_COOLDOWN_SEC,
    LiquidityPlugin,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderAttempt,
    ReverseSwapAction,
)

A, B, C = "npubAAAprovider", "npubBBBprovider", "npubCCCprovider"


def _plugin(**config_over) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.failover")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p._reverse_swap_timeout_sec = 5.0
    cfg = dict(SWAPSERVER_NPUB=None, SWAPSERVER_URL=None)
    cfg.update(config_over)
    p.config = SimpleNamespace(**cfg)
    p.faults = []        # (npub, reason, kwargs)
    p.peer_faults = []   # (node_id, reason, kwargs)
    p.successes = []     # npub
    p.dev_fees = []      # (amount_sat, source)
    p.tracked = []       # (npub, expected_onchain_sat)
    p.diags = []         # _diag_event kwargs
    p.logged = []        # _log_action kwargs
    p._record_provider_fault = lambda wallet, npub, reason, **kw: \
        p.faults.append((npub, reason, kw))
    p._record_peer_fault = lambda wallet, node_id, reason, **kw: \
        p.peer_faults.append((node_id, reason, kw))
    p._record_provider_success = lambda wallet, npub: p.successes.append(npub)
    p._accrue_dev_fee = lambda wallet, amount_sat, source=None: \
        p.dev_fees.append((amount_sat, source))
    p._channel_peer_node_id = lambda wallet, channel_id: "peer-node-id"

    # Mirrors the real helper: records only swaps that actually appeared (so
    # `tracked` reflects what reconciliation would really pick up) and RETURNS
    # their payment hashes, which is what _unfunded_swap_outcome classifies.
    def _track(wallet, sm, before, npub, action, exp):
        new = set(sm._swaps) - before
        if new:
            p.tracked.append((npub, exp))
        return new
    p._track_new_swaps = _track
    p._diag_event = lambda wallet, **kw: p.diags.append(kw)
    p._log_action = lambda wallet, **kw: p.logged.append(kw)
    p.on_action_done = lambda wallet, msg: None
    return p


class _SM(SimpleNamespace):
    """Swap manager that records which provider's pairs were applied, and drives
    a scripted per-attempt outcome."""

    def __init__(self, outcomes, *, recv_amount=None) -> None:
        # outcomes: npub -> value to return, or an exception instance to raise.
        self.outcomes = outcomes
        self.attempts: List[tuple] = []     # (npub, lightning_amount_sat)
        self.pairs_applied: List[str] = []  # npub, in order
        self.mining_fee = 1_000
        self.is_initialized = asyncio.Event()
        self.is_initialized.set()
        self._swaps = {}
        self._current = ""
        self._recv_amount = recv_amount

    def update_pairs(self, pairs) -> None:
        self._current = pairs.npub
        self.pairs_applied.append(pairs.npub)

    def get_recv_amount(self, amt, *, is_reverse):
        if self._recv_amount is not None:
            return self._recv_amount(self._current, amt)
        return amt - 500

    def get_swap(self, payment_hash: bytes):
        return self._swaps.get(payment_hash.hex())

    async def reverse_swap(self, **kw):
        npub = self._current
        self.attempts.append((npub, kw["lightning_amount_sat"]))
        outcome = self.outcomes.get(npub, "txid-" + npub)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(self, **kw)
        return outcome


def _wallet(sm, ln_payment: str = "failed") -> SimpleNamespace:
    """``ln_payment`` describes what Electrum would say about the Lightning
    payment of any swap this wallet created, which is what decides whether an
    unfunded swap may fail over:

      "failed"    -- PR_UNPAID, nothing in flight, route attempts logged: we
                     tried and gave up (the 120s PAYMENT_TIMEOUT case).
      "inflight"  -- an HTLC may still be live: must NOT fail over.
      "no_route"  -- PR_UNPAID, nothing in flight, and no route was ever
                     attempted so there are no logs. The fastest failure there
                     is (well under a second) and nothing is committed, so it
                     MUST fail over -- see _unfunded_swap_outcome.
      "paid"      -- it settled; funding just has not landed yet.
    """
    return SimpleNamespace(lnworker=_LnWorker(sm, ln_payment))


class _LnWorker:
    """Reports on the swaps the SM has created *at call time* -- the swap does
    not exist yet when the wallet is built, so these must not be snapshotted."""

    def __init__(self, sm, ln_payment: str) -> None:
        self.swap_manager = sm
        self.channels: dict = {}
        self._sm = sm
        self._ln_payment = ln_payment

    @staticmethod
    def get_channel_by_id(cid):
        return SimpleNamespace(node_id=b"\x11" * 33)

    def get_payments(self, *, status=None):
        if status == "inflight" and self._ln_payment == "inflight":
            return {bytes.fromhex(k) for k in self._sm._swaps}
        return set()

    def get_payment_status(self, ph, direction=None):
        from electrum.invoices import PR_PAID, PR_UNPAID  # type: ignore
        return PR_PAID if self._ln_payment == "paid" else PR_UNPAID

    @property
    def inflight_payments(self):
        return set(self._sm._swaps) if self._ln_payment == "inflight" else set()

    @property
    def logs(self):
        if self._ln_payment != "failed":
            return {}
        return {k: ["route attempt"] for k in self._sm._swaps}


def _transport(npubs) -> SimpleNamespace:
    offers = {n: SimpleNamespace(server_pubkey="ab" * 32,
                                 pairs=SimpleNamespace(npub=n))
              for n in npubs}
    return SimpleNamespace(target_pubkey=None, target_npub=None,
                           get_offer=lambda npub: offers.get(npub))


def _action(chosen: str = A, alternates=(B,), amount: int = 400_000,
            alt_amounts: Optional[dict] = None) -> ReverseSwapAction:
    alt_amounts = alt_amounts or {}
    return ReverseSwapAction(
        channel_id="aa" * 32, short_id="1x1x1",
        lightning_amount_sat=amount, reason="drain",
        provider_npub=chosen,
        alternates=tuple(ProviderAttempt(npub=n,
                                         amount_sat=alt_amounts.get(n, amount),
                                         all_in_cost_pct=0.4)
                         for n in alternates))


def _run(p, sm, action, transport=None, ln_payment: str = "failed") -> None:
    asyncio.run(p._reverse_swap(_wallet(sm, ln_payment), action, state={},
                                transport=transport or _transport(
                                    [action.provider_npub]
                                    + [a.npub for a in action.alternates])))


# --- failover on each pre-payment failure class ---------------------------
@pytest.mark.parametrize("failure,expect_fault", [
    (SwapServerError(), True),
    (UserFacingException("Total swap costs are insane."), False),
    (Exception("rswap check failed: onchain_amount is less than expected: 1 < 2"), True),
    (Exception("rswap check failed: inconsistent RHASH and invoice"), True),
    (Exception("rswap check failed: locktime too close"), False),
])
def test_pre_payment_failure_fails_over_to_next_provider(failure, expect_fault) -> None:
    """Every one of these raises before Electrum's add_reverse_swap, so no funds
    moved and the next provider is tried immediately -- in the same cycle."""
    sm = _SM({A: failure})           # B falls through to a successful default
    p = _plugin()
    _run(p, sm, _action())
    assert [n for n, _amt in sm.attempts] == [A, B]
    assert p.successes == [B]                     # the failover completed it
    assert p.dev_fees == [(399_500, "1x1x1")]
    assert (len(p.faults) == 1) == expect_fault
    if expect_fault:
        assert p.faults[0][0] == A                # charged to the one that failed


def test_unreachable_provider_fails_over() -> None:
    """A provider whose pairs never initialise is skipped without ever issuing an
    RPC, and the cascade moves on."""
    sm = _SM({})
    sm.is_initialized = asyncio.Event()           # never set -> init timeout
    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(aw, timeout):
        # Only the 15s is_initialized wait is shortened; leave others alone.
        return await real_wait_for(aw, 0.01 if timeout == 15 else timeout)

    p = _plugin()
    asyncio.set_event_loop(asyncio.new_event_loop())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "wait_for", _fast_wait_for)
        _run(p, sm, _action())
    # Both attempts time out on init; both are faulted, neither reaches the RPC.
    assert sm.attempts == []
    assert [n for n, _r, _k in p.faults] == [A, B]
    assert "not reachable" in p.faults[0][1]


def test_unswappable_amount_fails_over() -> None:
    """get_recv_amount is provider-specific: A cannot host the amount, B can."""
    sm = _SM({}, recv_amount=lambda npub, amt: None if npub == A else amt - 500)
    p = _plugin()
    _run(p, sm, _action())
    assert [n for n, _amt in sm.attempts] == [B]   # A never issued an RPC
    assert p.successes == [B]
    assert p.faults == []                          # not a provider fault
    assert any("not swappable" in d.get("reason", "") for d in p.diags)


def test_cascade_walks_three_providers() -> None:
    sm = _SM({A: SwapServerError(), B: SwapServerError()})
    p = _plugin()
    _run(p, sm, _action(chosen=A, alternates=(B, C)))
    assert [n for n, _amt in sm.attempts] == [A, B, C]
    assert p.successes == [C]


# --- the safety line: never fail over once funds may be committed ---------
def test_no_failover_once_a_swap_object_exists() -> None:
    """THE safety-critical case. The backstop fired after Electrum reached
    add_reverse_swap, so a Lightning payment may be in flight. Retrying on
    another provider would drain the same channel twice -- the cascade must stop
    dead, even though a perfectly good failover provider is queued."""
    async def _hang_after_creating(sm, **kw):
        sm._swaps["ee" * 32] = object()       # add_reverse_swap happened
        await asyncio.sleep(30)               # ... then stall past the backstop
        return "unreachable"

    sm = _SM({A: _hang_after_creating})
    p = _plugin()
    p._reverse_swap_timeout_sec = 0.05
    _run(p, sm, _action())
    assert [n for n, _amt in sm.attempts] == [A]      # B was NEVER tried
    assert p.successes == [] and p.dev_fees == []
    assert p.tracked == [(A, 399_500)]                # handed to reconciliation
    assert len(p.faults) == 1 and p.faults[0][0] == A


def test_timeout_before_any_swap_exists_does_fail_over() -> None:
    """The mirror image: the stall happened before add_reverse_swap, so nothing
    was committed and the next provider is safe to try."""
    async def _hang_before_creating(sm, **kw):
        await asyncio.sleep(30)
        return "unreachable"

    sm = _SM({A: _hang_before_creating})
    p = _plugin()
    p._reverse_swap_timeout_sec = 0.05
    _run(p, sm, _action())
    assert [n for n, _amt in sm.attempts] == [A, B]
    assert p.successes == [B]
    assert p.tracked == []                            # nothing was ever created
    assert len(p.faults) == 1 and p.faults[0][0] == A


# --- the unfunded (no funding txid) swap -----------------------------------
# Electrum's reverse_swap races pay_invoice against funding detection and returns
# whichever finishes FIRST, then `return swap.funding_txid`. Because pay_invoice
# RETURNS (success, log) instead of raising, a failed payment simply completes the
# race -- so None overwhelmingly means "the Lightning payment failed", not
# "accepted, funding pending". Which one it is decides whether we may fail over.
async def _accept_without_funding(sm, **kw):
    sm._swaps["ff" * 32] = SimpleNamespace(prepay_hash=None)  # add_reverse_swap ran
    return None                                                # ... no funding txid


def test_unfunded_swap_with_a_failed_payment_fails_over() -> None:
    """The regression this file exists for. The payment gave up (PAYMENT_TIMEOUT =
    120s), nothing is committed on the channel, and two ranked failover providers
    were sitting ready -- so the cascade must move on instead of scoring the dead
    attempt as a completed swap."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="failed")
    assert [n for n, _amt in sm.attempts] == [A, B]    # B was actually tried
    assert p.successes == [B]                          # and it delivered
    assert p.dev_fees == [(399_500, "1x1x1")]
    # The dead attempt is still tracked, so reconciliation keeps its own verdict.
    assert p.tracked == [(A, 399_500)]
    # No swap action is logged for the attempt that moved nothing.
    assert len(p.logged) == 1


def test_failed_payment_softly_faults_the_provider_exactly_once() -> None:
    """Attribution is genuinely ambiguous -- our peer could not route it, and the
    provider may equally be offline or out of inbound -- so the provider's share
    is soft: it should weigh on the ranking, not escalate toward a ban.

    The PEER's share is deliberately not charged here. ``_reconcile_pending_swaps``
    already recognises this swap's failed payment on the next tick and faults the
    peer then, so doing it here too would count one failure twice against the same
    peer. The provider has no such path -- that branch drops the record without
    faulting it -- which is why this arm exists at all.
    """
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="failed")
    assert [(n, kw) for n, _r, kw in p.faults] == [(A, {"soft": True})]
    assert p.peer_faults == []                         # reconciliation's job, not ours
    assert [d["reason"] for d in p.diags] == ["reverse-swap Lightning payment failed"]


def test_unfunded_swap_with_an_inflight_payment_stops_the_cascade() -> None:
    """The safety-critical half. An HTLC may still be live, so draining this
    channel again through another provider could pay out twice."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="inflight")
    assert [n for n, _amt in sm.attempts] == [A]       # B never tried
    assert p.tracked == [(A, 399_500)]
    assert p.successes == [] and p.dev_fees == []
    assert p.faults == [] and p.peer_faults == []
    assert len(p.logged) == 1


def test_unfunded_swap_with_no_route_to_the_provider_fails_over() -> None:
    """The fastest failure there is: no route to the provider, so the payment
    gives up in well under a second having logged no route attempts at all.

    Electrum's own derivation cannot call that "failed" (it needs logs), which is
    why the gate asks whether anything is COMMITTED rather than whether failure
    is provable. Nothing is in flight and nothing settled, so the next provider
    is safe to try -- and this is the case failover is most useful for, since a
    provider we cannot reach at all is precisely the one to skip."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="no_route")
    assert [n for n, _amt in sm.attempts] == [A, B]
    assert p.successes == [B]


def test_unfunded_swap_whose_payment_settled_stops_the_cascade() -> None:
    """A reverse swap's main invoice is a hold invoice, so a payment that reached
    the provider normally reads as in flight. PR_PAID here therefore means the
    swap really is progressing and only its funding txid is lagging."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="paid")
    assert [n for n, _amt in sm.attempts] == [A]
    assert p.successes == [] and p.dev_fees == []


def test_the_cascade_deadline_lets_every_ranked_provider_start() -> None:
    """The deadline is checked before each attempt, and an attempt in progress
    runs to its own backstop -- so the LAST provider is only reached at
    (cap - 1) * backstop. A deadline below that silently drops a failover the
    engine had already ranked and vetted, which is what a flat 500s used to do
    with a cap of 3 and a 300s backstop.

    Pinned as an invariant rather than a value so the cap and the backstop can be
    retuned without quietly reintroducing the gap."""
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        REVERSE_SWAP_TIMEOUT_SEC, SWAP_CASCADE_DEADLINE_SEC)
    from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore
        MAX_SWAP_PROVIDER_ATTEMPTS)
    latest_start = (MAX_SWAP_PROVIDER_ATTEMPTS - 1) * REVERSE_SWAP_TIMEOUT_SEC
    assert SWAP_CASCADE_DEADLINE_SEC > latest_start, (
        f"cascade deadline {SWAP_CASCADE_DEADLINE_SEC}s cannot reach provider "
        f"{MAX_SWAP_PROVIDER_ATTEMPTS}, which starts at {latest_start}s")


def test_a_live_prepay_htlc_blocks_failover() -> None:
    """The minerFeeInvoice is a separate fire-and-forget payment. Its HTLC is
    still something committed on this channel, so it must gate failover even when
    the main invoice has definitively failed.

    Deliberately conservative: the prepay is small (2x mining fee) and unpinned,
    so it does not itself risk draining the target channel twice -- but holding
    the cascade while ANY HTLC for the swap is live is the chosen trade, at the
    cost of deferring that channel to the next cycle. Observed live: a main
    payment that fails instantly for want of a route returns while its prepay is
    still in flight, and the cascade stops there."""
    prepay = bytes.fromhex("ab" * 32)

    async def _accept_with_live_prepay(sm, **kw):
        sm._swaps["ff" * 32] = SimpleNamespace(prepay_hash=prepay)
        return None

    sm = _SM({A: _accept_with_live_prepay})
    p = _plugin()
    wallet = _wallet(sm, "failed")
    # Main invoice: failed. Prepay: still in flight.
    wallet.lnworker.get_payments = lambda *, status=None: (
        {prepay} if status == "inflight" else set())
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(B,)), state={},
                                transport=_transport([A, B])))
    assert [n for n, _amt in sm.attempts] == [A]       # B never tried
    assert p.successes == []


def test_unfunded_swap_with_no_swap_object_stops_the_cascade() -> None:
    """No swap object means nothing to inspect. We cannot show the channel is
    clear, so the conservative answer wins even though nothing looks committed."""
    async def _return_none_without_creating(sm, **kw):
        return None

    sm = _SM({A: _return_none_without_creating})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="failed")
    assert [n for n, _amt in sm.attempts] == [A]
    assert p.tracked == []


# --- abort: failing over would be pointless or unsafe ---------------------
def test_our_side_condition_aborts_without_failover() -> None:
    """A stale local tip is provider-independent -- every provider would fail
    identically, so the cascade aborts rather than burning its budget."""
    sm = _SM({A: Exception("our blockchain tip is stale")})
    p = _plugin()
    _run(p, sm, _action())
    assert [n for n, _amt in sm.attempts] == [A]      # B not tried
    assert p.faults == []                             # never blame the provider
    assert any(d.get("reason") == "reverse swap internal error" for d in p.diags)


def test_unrecognised_error_aborts_without_failover() -> None:
    """An error we cannot classify might have committed funds, so stop."""
    sm = _SM({A: RuntimeError("something nobody has seen before")})
    p = _plugin()
    _run(p, sm, _action())
    assert [n for n, _amt in sm.attempts] == [A]
    assert p.faults == []
    assert any(d.get("reason") == "reverse swap internal error" for d in p.diags)


# --- stop at the first success -------------------------------------------
def test_first_success_stops_the_cascade() -> None:
    sm = _SM({})                                      # A succeeds by default
    p = _plugin()
    _run(p, sm, _action(chosen=A, alternates=(B, C)))
    assert [n for n, _amt in sm.attempts] == [A]
    assert p.successes == [A]
    assert len(p.logged) == 1


# --- per-attempt sizing and targeting ------------------------------------
def test_each_attempt_uses_its_own_amount_and_pairs() -> None:
    """A failover provider advertises its own capacity, so the retry is a
    differently-sized swap -- and it must be priced with ITS pairs, never the
    failed provider's."""
    sm = _SM({A: SwapServerError()})
    p = _plugin()
    _run(p, sm, _action(chosen=A, alternates=(B,), amount=400_000,
                        alt_amounts={B: 250_000}))
    assert sm.attempts == [(A, 400_000), (B, 250_000)]
    assert sm.pairs_applied == [A, B]                 # re-applied per attempt
    assert p.dev_fees == [(249_500, "1x1x1")]         # priced on B's amount


def test_transport_is_retargeted_for_each_attempt() -> None:
    sm = _SM({A: SwapServerError()})
    seen = []
    tr = _transport([A, B])
    base_offer = tr.get_offer

    def _get_offer(npub):
        offer = base_offer(npub)
        if offer is not None:
            offer.server_pubkey = ("aa" if npub == A else "bb") * 32
        return offer
    tr.get_offer = _get_offer
    p = _plugin()
    p._set_status = lambda wallet, msg: seen.append(tr.target_npub)
    _run(p, sm, _action(), transport=tr)
    assert tr.target_npub == B                        # ends pointed at the winner
    assert tr.target_pubkey == "bb" * 32


# --- deadline -------------------------------------------------------------
def test_cascade_deadline_stops_before_starting_another_attempt(monkeypatch) -> None:
    """The deadline is checked BETWEEN attempts -- never mid-attempt, since
    aborting a swap whose payment may be in flight is exactly what we avoid."""
    import electrum.plugins.inbound_liquidity as mod
    monkeypatch.setattr(mod, "SWAP_CASCADE_DEADLINE_SEC", 0.05)
    slow = SwapServerError()

    async def _slow_fail(sm, **kw):
        await asyncio.sleep(0.2)                      # outlives the deadline
        raise slow

    sm = _SM({A: _slow_fail})
    p = _plugin()
    _run(p, sm, _action(chosen=A, alternates=(B, C)))
    # A ran to completion (not cut short); B and C were never started.
    assert [n for n, _amt in sm.attempts] == [A]
    assert p.successes == []
    assert any(d.get("reason") == "swap provider cascade deadline reached"
               for d in p.diags)


# --- cooldown -------------------------------------------------------------
def test_cooldown_armed_once_for_the_whole_cascade() -> None:
    sm = _SM({A: SwapServerError()})
    p = _plugin()
    before = time.monotonic()
    _run(p, sm, _action())
    armed = p._swap_cooldown_until["aa" * 32]
    # One cooldown window from the start of the cascade, not one per attempt.
    assert before + SWAP_COOLDOWN_SEC <= armed <= time.monotonic() + SWAP_COOLDOWN_SEC


def test_all_providers_vanished_arms_no_cooldown() -> None:
    """Nothing was attempted, so the channel must stay free to be re-evaluated as
    soon as offers come back (the long-standing single-provider behaviour)."""
    sm = _SM({})
    p = _plugin()
    _run(p, sm, _action(), transport=_transport([]))   # nobody advertising
    assert sm.attempts == []
    assert p._swap_cooldown_until == {}


def test_vanished_provider_is_skipped_and_the_next_one_runs() -> None:
    sm = _SM({})
    p = _plugin()
    _run(p, sm, _action(), transport=_transport([B]))  # A gone, B present
    assert [n for n, _amt in sm.attempts] == [B]
    assert p.successes == [B]


def test_cooldown_still_suppresses_a_reattempt() -> None:
    sm = _SM({})
    p = _plugin()
    action = _action()
    p._swap_cooldown_until[action.channel_id] = time.monotonic() + 100
    _run(p, sm, action)
    assert sm.attempts == [] and p.faults == []


# --- legacy single-provider mode -----------------------------------------
def test_legacy_mode_still_single_shot() -> None:
    """URL/legacy mode has no alternates and no offer to resolve; it must behave
    exactly as before."""
    sm = _SM({"": SwapServerError()})
    p = _plugin(SWAPSERVER_URL="https://swap.example")
    _run(p, sm, _action(chosen="", alternates=()), transport=_transport([]))
    assert [n for n, _amt in sm.attempts] == [""]
    assert p.successes == []


def test_legacy_mode_with_nothing_configured_skips() -> None:
    sm = _SM({})
    p = _plugin()
    _run(p, sm, _action(chosen="", alternates=()), transport=_transport([]))
    assert sm.attempts == []
    assert p._swap_cooldown_until == {}
