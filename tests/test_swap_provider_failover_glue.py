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
  * the mining-fee PREPAYMENT, which is what used to make the whole cascade
    unreachable: Electrum returns from ``reverse_swap`` while that HTLC is still
    live, and reading "not resolved yet" as "may be committed" stopped every
    cascade at its first provider. The gate now waits for it, bounded;
  * the single reduced retry: a provider whose PAYMENT failed is tried once more at
    the engine's smaller size (if that size still clears the cost ceiling) before we
    move on, and is faulted once for the pair rather than once per rung;
  * abort (no failover) on provider-independent our-side conditions and on
    unrecognised errors;
  * the cascade stops at the first success, and honours its wall-clock deadline;
  * per-attempt sizing/targeting: each provider gets ITS amount and ITS pairs;
  * a swap that never funded is a DECLINE, never an action;
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
    _CascadeFaults,
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
    # Per-provider wait for advertised terms; shrunk so an unreachable
    # provider trips the init timeout instantly instead of in 15s.
    p._swap_init_timeout_sec = 0.01
    # Bounded wait for a swap's mining-fee prepayment to resolve before the
    # failover gate decides; shrunk so no test sits through the real 150s.
    p._prepay_resolve_wait_sec = 0.05
    p._prepay_poll_interval_sec = 0.01
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
    # A swap that did not fund is a DECLINE, never an action -- see
    # _attempt_reverse_swap's unfunded arms. Captured separately so a test
    # can tell the two apart.
    p.declines = []      # (DeclineRecord, state)
    p._log_decline = lambda wallet, decline, state: \
        p.declines.append((decline, state))
    # Real one-slot dedupe, not a stub: a channel whose swaps keep failing is
    # re-evaluated every cooldown, and one row per cycle would flood the log.
    p._last_exec_decline_sig = {}
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
    RPC, and the cascade moves on.

    Whether that skip also *faults* the provider is a separate question, decided
    after the cascade by ``_commit_cascade_faults`` -- see the common-mode tests
    below. Here only the walk itself is asserted."""
    sm = _SM({})
    sm.is_initialized = asyncio.Event()           # never set -> init timeout
    p = _plugin()
    _run(p, sm, _action())
    # Both attempts time out on init; neither ever reaches the RPC.
    assert sm.attempts == []


# --- reachability faults: provider at fault vs. our transport at fault -----
# An is_initialized timeout is reached over OUR nostr transport, so one such
# timeout cannot distinguish "this provider is gone" from "our relay is down".
# The cascade supplies the missing observation: if nobody responded, the common
# factor is us. See _CascadeFaults.
def test_whole_cascade_unreachable_faults_nobody() -> None:
    """Every provider timed out and none responded -> our transport is the likely
    cause, so NO provider is charged. This is the regression that used to land a
    hard, escalating fault on every provider in the cascade on each local
    outage."""
    sm = _SM({})
    sm.is_initialized = asyncio.Event()           # never set -> all time out
    p = _plugin()
    _run(p, sm, _action(chosen=A, alternates=(B, C)))
    assert sm.attempts == []
    assert p.faults == []                         # nobody blamed for our outage
    # ...but the operator can still see it happened.
    assert any("common-mode" in d.get("reason", "") for d in p.diags), p.diags


def test_unreachable_providers_are_faulted_when_one_responded() -> None:
    """A provider that responded proves the transport works, so the timeouts in
    that same cascade are real evidence about their own providers."""
    # A and C never initialise; B does, and completes the swap.
    reachable_for = {B}
    real_event = asyncio.Event()
    real_event.set()
    never = asyncio.Event()

    sm = _SM({})

    class _Pairs(SimpleNamespace):
        pass

    # is_initialized is read per attempt, after update_pairs() has told us which
    # provider is current -- mirroring the real transport, where the event is set
    # once that provider's terms arrive.
    def _current_event():
        return real_event if sm._current in reachable_for else never
    type(sm).is_initialized = property(lambda self: _current_event())
    try:
        p = _plugin()
        _run(p, sm, _action(chosen=A, alternates=(B, C)))
        # A timed out, B ran and succeeded, so the cascade stopped before C.
        assert [n for n, _amt in sm.attempts] == [B]
        assert p.successes == [B]
        assert [n for n, _r, _k in p.faults] == [A]
        assert "not reachable" in p.faults[0][1]
        assert p.faults[0][2].get("soft") in (None, False)   # escalating
        assert not any("common-mode" in d.get("reason", "") for d in p.diags)
    finally:
        del type(sm).is_initialized


def test_single_unreachable_provider_is_still_faulted() -> None:
    """One attempt is one sample: it proves nothing either way about the
    transport, so the pre-existing behaviour stands and the provider is faulted.
    Without this, a wallet with a single configured provider would never record a
    reachability fault again."""
    sm = _SM({})
    sm.is_initialized = asyncio.Event()           # never set
    p = _plugin()
    _run(p, sm, _action(chosen=A, alternates=()))
    assert [n for n, _r, _k in p.faults] == [A]
    assert "not reachable" in p.faults[0][1]
    assert not any("common-mode" in d.get("reason", "") for d in p.diags)


def test_reachability_faults_are_committed_even_if_the_cascade_raises() -> None:
    """The buffer is adjudicated in a ``finally``, so evidence collected before an
    unexpected failure is not silently lost."""
    boom = RuntimeError("transport exploded")
    sm = _SM({})
    # A times out on init (buffered); B then reaches the RPC, which blows up in a
    # way no arm catches -- get_recv_amount raising is outside every handler.
    reachable_for = {B}
    real_event = asyncio.Event()
    real_event.set()
    never = asyncio.Event()
    type(sm).is_initialized = property(
        lambda self: real_event if sm._current in reachable_for else never)

    def _explode(amt, *, is_reverse):
        raise boom
    sm.get_recv_amount = _explode
    try:
        p = _plugin()
        with pytest.raises(RuntimeError):
            _run(p, sm, _action(chosen=A, alternates=(B,)))
        # B reached init, so A's buffered timeout is real evidence and is written
        # despite the cascade dying afterwards.
        assert [n for n, _r, _k in p.faults] == [A]
    finally:
        del type(sm).is_initialized


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
    # A DISTINCT payment hash per call, as Electrum's reverse_swap gives (it mints a
    # fresh preimage each time). Reusing one key made the executor's
    # "swaps that appeared during OUR call" diff come back empty on a second
    # attempt, which reads as "no swap object" -- a fake-only artefact that would
    # mask the rung walk.
    key = f"{len(sm._swaps):02x}" + "ff" * 31
    sm._swaps[key] = SimpleNamespace(prepay_hash=None)         # add_reverse_swap ran
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
    channel again through another provider could pay out twice.

    Note this is the MAIN payment in flight, which is the one that means "possibly
    committed" and is never waited on -- only a live prepayment gets the bounded
    wait (see the prepay tests below)."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _action(alternates=(B,)), ln_payment="inflight")
    assert [n for n, _amt in sm.attempts] == [A]       # B never tried
    assert p.tracked == [(A, 399_500)]
    assert p.successes == [] and p.dev_fees == []
    assert p.faults == [] and p.peer_faults == []
    # Recorded, but as a decline -- nothing funded, so nothing is an action.
    assert p.logged == []
    assert len(p.declines) == 1
    assert "did not complete" in p.declines[0][0].reason


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


def test_the_cascade_deadline_reaches_every_ranked_provider() -> None:
    """The deadline is checked before each attempt, and an attempt in progress runs
    to its own backstop -- so the LAST rung is only reached at
    (rungs - 1) * per-rung cost. A budget below that silently drops a failover the
    engine had already ranked and vetted, which is what a flat 500s used to do with
    three providers and a 300s backstop.

    Pinned as an invariant over the derivation, at every provider count, so the
    backstops and the rung count can be retuned without reintroducing the gap. It
    has to be a derivation now rather than a constant: the provider cap is gone, so
    there is no fixed number for a constant to have been sized against.
    """
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        PREPAY_RESOLVE_WAIT_SEC, REVERSE_SWAP_TIMEOUT_SEC,
        swap_cascade_deadline_sec)
    per_rung = REVERSE_SWAP_TIMEOUT_SEC + PREPAY_RESOLVE_WAIT_SEC
    for providers in (1, 2, 3, 7, 25):
        budget = swap_cascade_deadline_sec(providers)
        # Two rungs per provider: the planned amount and its reduced retry.
        latest_start = (providers * 2 - 1) * per_rung
        assert budget > latest_start, (
            f"a {providers}-provider cascade budgets {budget}s, which cannot reach "
            f"its last rung at {latest_start}s")
    # Monotonic in the provider count: adding a provider must never shrink the
    # budget (which would drop a provider that used to be reachable).
    budgets = [swap_cascade_deadline_sec(n) for n in range(1, 10)]
    assert budgets == sorted(budgets)
    # A degenerate count still yields a usable budget rather than zero.
    assert swap_cascade_deadline_sec(0) > 0


# --- the mining-fee prepayment, and why failover used to be unreachable ---
# Electrum's reverse_swap fires the minerFeeInvoice with asyncio.ensure_future and
# then races ONLY the main payment against funding detection, so it returns while
# the prepayment's HTLC is typically still live. The failover gate must not fail
# over with a live HTLC -- but treating "not resolved yet" as "may be committed"
# meant the gate said COMMITTED on essentially every failed swap, and no cascade
# ever reached its second provider.
#
# Observed on mainnet: main payment gave up at 16:45:27 ('Giving up after 29
# attempts'), prepayment MPP_TIMEOUTed at 16:45:30, and two ranked failover
# providers were never tried. Three seconds of patience was the whole difference.
_PREPAY = bytes.fromhex("ab" * 32)


async def _accept_with_prepay(sm, **kw):
    key = f"{len(sm._swaps):02x}" + "ff" * 31       # distinct per call, as in Electrum
    sm._swaps[key] = SimpleNamespace(prepay_hash=_PREPAY, funding_txid=None)
    return None


def _prepay_wallet(sm, inflight_calls: int):
    """A wallet whose prepayment reads as in flight for the first
    ``inflight_calls`` polls and resolved (failed) afterwards. The main invoice is
    failed throughout."""
    wallet = _wallet(sm, "failed")
    calls = {"n": 0}

    def get_payments(*, status=None):
        if status != "inflight":
            return set()
        calls["n"] += 1
        return {_PREPAY} if calls["n"] <= inflight_calls else set()

    wallet.lnworker.get_payments = get_payments
    return wallet, calls


def test_a_resolving_prepay_htlc_no_longer_blocks_failover() -> None:
    """The fix. The prepayment is live when the gate is first asked and resolves a
    moment later, so the cascade waits and then does exactly what it exists for."""
    sm = _SM({A: _accept_with_prepay})
    p = _plugin()
    wallet, calls = _prepay_wallet(sm, inflight_calls=2)
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(B,)), state={},
                                transport=_transport([A, B])))
    assert [n for n, _amt in sm.attempts] == [A, B]     # B WAS tried
    assert p.successes == [B]
    assert calls["n"] > 2, "the gate never re-asked after the first live reading"


def test_a_live_prepay_htlc_still_blocks_failover_while_it_stays_live() -> None:
    """The safety property is unchanged: a prepayment that never resolves within
    the budget still stops the cascade. Waiting replaced guessing; it did not
    replace the guarantee."""
    sm = _SM({A: _accept_with_prepay})
    p = _plugin()
    wallet, _calls = _prepay_wallet(sm, inflight_calls=10_000)
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(B,)), state={},
                                transport=_transport([A, B])))
    assert [n for n, _amt in sm.attempts] == [A]        # B never tried
    assert p.successes == []
    assert any("prepayment did not resolve in time" in (d.get("reason") or "")
               for d in p.diags)


def test_the_prepay_wait_is_bounded() -> None:
    """It must not be able to hold the evaluation lock indefinitely on a prepayment
    that never resolves."""
    sm = _SM({A: _accept_with_prepay})
    p = _plugin()
    p._prepay_resolve_wait_sec = 0.2
    p._prepay_poll_interval_sec = 0.01
    wallet, _calls = _prepay_wallet(sm, inflight_calls=10_000)
    started = time.monotonic()
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(B,)), state={},
                                transport=_transport([A, B])))
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 3.0, f"prepay wait took {elapsed:.2f}s"


def test_a_swap_that_funds_during_the_prepay_wait_stops_the_cascade() -> None:
    """The provider funded while we were waiting, so the swap is alive and this
    channel is emphatically not free. Must not wait out the prepayment and then
    fail over into a second drain."""
    sm = _SM({A: _accept_with_prepay})
    p = _plugin()
    wallet, _calls = _prepay_wallet(sm, inflight_calls=10_000)
    original = sm._swaps

    def _fund_on_second_poll(*, status=None):
        if status != "inflight":
            return set()
        # Funding appears while the prepayment is still live.
        for swap in sm._swaps.values():
            swap.funding_txid = "deadbeef"
        return {_PREPAY}

    wallet.lnworker.get_payments = _fund_on_second_poll
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(B,)), state={},
                                transport=_transport([A, B])))
    assert original is sm._swaps
    assert [n for n, _amt in sm.attempts] == [A]        # B never tried
    assert p.successes == []


def test_a_settled_prepay_stops_the_cascade() -> None:
    """A prepayment the provider actually fulfilled is evidence the swap is live:
    the provider only settles it once the main payment's MPP set has arrived."""
    sm = _SM({A: _accept_with_prepay})
    p = _plugin()
    wallet = _wallet(sm, "paid")                        # PR_PAID for every hash
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(B,)), state={},
                                transport=_transport([A, B])))
    assert [n for n, _amt in sm.attempts] == [A]
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
def _run_cascade_with_deadline(p, sm, action, deadline: float, npubs) -> None:
    """Drive ``_run_swap_cascade`` with a deadline we choose outright.

    The budget ``_reverse_swap`` derives is, by construction, at least what all its
    rungs can spend (see swap_cascade_deadline_sec), so no well-behaved attempt can
    ever exhaust it -- which is the design intent and also why the deadline cannot
    be tripped through the public entry point. The loop's between-attempts check is
    still worth pinning, so it is exercised with the deadline supplied directly.
    """
    from contextlib import nullcontext
    attempts = [(ProviderAttempt(npub=n, amount_sat=400_000, all_in_cost_pct=0.4),
                 SimpleNamespace(server_pubkey="ab" * 32,
                                 pairs=SimpleNamespace(npub=n)))
                for n in npubs]
    wallet = _wallet(sm, "failed")
    asyncio.run(p._run_swap_cascade(
        wallet, action, attempts, nullcontext(_transport(npubs)), {},
        deadline, len(attempts), _CascadeFaults(), deadline_sec=1.0))


def test_cascade_deadline_stops_before_starting_another_attempt() -> None:
    """The deadline is checked BETWEEN attempts -- never mid-attempt, since
    aborting a swap whose payment may be in flight is exactly what we avoid.

    So A runs to completion despite outliving the deadline, and B and C -- which
    had not started -- are left for the next cycle.
    """
    slow = SwapServerError()

    async def _slow_fail(sm, **kw):
        await asyncio.sleep(0.05)                     # outlives the deadline
        raise slow

    sm = _SM({A: _slow_fail})
    p = _plugin()
    _run_cascade_with_deadline(
        p, sm, _action(chosen=A, alternates=(B, C)),
        deadline=time.monotonic() + 0.01, npubs=[A, B, C])
    assert [n for n, _amt in sm.attempts] == [A]
    assert p.successes == []
    deadline_diags = [d for d in p.diags
                      if d.get("reason") == "swap provider cascade deadline reached"]
    assert len(deadline_diags) == 1
    # It says how much was left undone, so a recurring deadline is diagnosable.
    assert "2 provider(s) untried" in deadline_diags[0]["detail"]


def test_an_expired_deadline_starts_nothing_at_all() -> None:
    """The check comes before the first attempt too, not just between them."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run_cascade_with_deadline(
        p, sm, _action(chosen=A, alternates=(B,)),
        deadline=time.monotonic() - 1.0, npubs=[A, B])
    assert sm.attempts == []
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


# --- the single reduced retry ---------------------------------------------
# A provider whose Lightning PAYMENT failed gets one more try at the engine's
# reduced size before we move on, because a payment can fail for want of a single
# route carrying the full amount and succeed just below it. The mainnet log that
# prompted this shows 95,468 sat failing 29 route attempts in a row, every failure
# a TEMPORARY_CHANNEL_FAILURE or UNKNOWN_NEXT_PEER from downstream -- nothing that
# says anything about the provider, and nothing a different provider alone fixes.
#
# Only the payment-failed arm earns the retry. A provider that declined, rejected,
# cheated or never answered would answer the same way at any size.
def _reduced_action(chosen: str = A, alternates=(B,), amount: int = 400_000,
                    reduced: Optional[int] = 360_000,
                    alt_reduced: Optional[int] = 360_000) -> ReverseSwapAction:
    return ReverseSwapAction(
        channel_id="aa" * 32, short_id="1x1x1",
        lightning_amount_sat=amount, reason="drain",
        provider_npub=chosen,
        reduced_amount_sat=reduced,
        reduced_all_in_cost_pct=0.45 if reduced else None,
        alternates=tuple(ProviderAttempt(npub=n, amount_sat=amount,
                                         all_in_cost_pct=0.4,
                                         reduced_amount_sat=alt_reduced,
                                         reduced_all_in_cost_pct=0.45)
                         for n in alternates))


def test_a_failed_payment_retries_the_same_provider_smaller_first() -> None:
    """Full size, then reduced size, THEN the next provider -- not straight to the
    next provider."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,)), ln_payment="failed")
    assert sm.attempts == [(A, 400_000), (A, 360_000), (B, 400_000)]
    assert p.successes == [B]


def test_the_reduced_rung_can_be_the_one_that_works() -> None:
    """The point of the whole mechanism: the provider was fine, the size was not."""
    async def _fail_big_succeed_small(sm, **kw):
        if kw["lightning_amount_sat"] > 380_000:
            # A swap object DID appear (add_reverse_swap ran) and then the payment
            # failed -- that is what makes this the retryable arm rather than the
            # "no swap object, cannot tell" one.
            return await _accept_without_funding(sm, **kw)
        return "txid-small"

    sm = _SM({A: _fail_big_succeed_small})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,)), ln_payment="failed")
    assert sm.attempts == [(A, 400_000), (A, 360_000)]
    assert p.successes == [A]               # same provider, smaller swap
    assert [n for n, _amt in sm.attempts if n == B] == []   # B never needed


def test_no_reduced_rung_when_the_engine_priced_none() -> None:
    """The cost ceiling is the engine's call. When it refused to price a reduction
    -- because a smaller swap would breach max_swap_fee_pct -- the executor must not
    invent one."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,), reduced=None, alt_reduced=None),
         ln_payment="failed")
    assert sm.attempts == [(A, 400_000), (B, 400_000)]
    assert p.successes == [B]


def test_every_provider_gets_its_own_reduced_rung() -> None:
    """Per provider, reset to full each time: each provider is priced independently,
    so each is entitled to its own full-then-reduced pair."""
    sm = _SM({A: _accept_without_funding, B: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B, C)), ln_payment="failed")
    assert sm.attempts == [(A, 400_000), (A, 360_000),
                           (B, 400_000), (B, 360_000),
                           (C, 400_000)]
    assert p.successes == [C]


def test_a_failed_payment_faults_the_provider_once_not_once_per_rung() -> None:
    """Two rungs failing is one observation about that provider. Charging both
    would sink a provider at double rate for a fault it may well not own -- the
    mainnet case was our own peer being unable to route."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,)), ln_payment="failed")
    assert [(n, kw) for n, _r, kw in p.faults] == [(A, {"soft": True})]
    # The diag events, though, are per rung: there the detail is the point.
    payment_diags = [d for d in p.diags
                     if d.get("reason") == "reverse-swap Lightning payment failed"]
    assert len(payment_diags) == 2
    assert "400000 sat" in payment_diags[0]["detail"]
    assert "360000 sat" in payment_diags[1]["detail"]
    assert "reduced size" in payment_diags[1]["detail"]


def test_a_rejected_provider_gets_no_reduced_rung() -> None:
    """A capacity rejection is that provider's own verdict; a smaller swap is if
    anything less attractive to it. Straight to the next provider."""
    sm = _SM({A: SwapServerError()})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,)), ln_payment="failed")
    assert sm.attempts == [(A, 400_000), (B, 400_000)]


def test_an_unreachable_provider_gets_no_reduced_rung() -> None:
    """It never answered. Asking it again, smaller, is a wasted round trip."""
    sm = _SM({A: _accept_without_funding})
    sm.is_initialized = asyncio.Event()      # never set -> init timeout
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=()), ln_payment="failed")
    assert sm.attempts == []


def test_a_possibly_committed_swap_gets_no_reduced_rung() -> None:
    """The safety rule outranks the retry: an HTLC may be live, so this channel is
    not touched again -- at any size."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,)), ln_payment="inflight")
    assert sm.attempts == [(A, 400_000)]
    assert p.successes == []


def test_a_reduced_rung_is_skipped_if_it_would_not_shrink() -> None:
    """Defensive: a reduced amount that is not actually smaller would burn a rung
    re-running the identical payment."""
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(), reduced=400_000), ln_payment="failed")
    assert sm.attempts == [(A, 400_000)]


def test_the_reduced_rung_uses_the_reduced_amount_for_costs_and_fees() -> None:
    """The dev fee and the expected on-chain amount must follow the rung that
    actually ran, not the size originally planned."""
    async def _fail_big_succeed_small(sm, **kw):
        if kw["lightning_amount_sat"] > 380_000:
            return await _accept_without_funding(sm, **kw)
        return "txid-small"

    sm = _SM({A: _fail_big_succeed_small})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=()), ln_payment="failed")
    # get_recv_amount subtracts 500, so the fee accrues on 360_000 - 500.
    assert p.dev_fees == [(359_500, "1x1x1")]
    assert len(p.logged) == 1
    assert p.logged[0]["amount_sat"] == 360_000
    assert "reduced size" in p.logged[0]["detail"]


def test_the_chosen_provider_inherits_the_actions_reduced_rung() -> None:
    """The engine puts the chosen provider's figures on the ACTION, not on a
    ProviderAttempt, so the executor synthesises one for it. That synthesis has to
    carry the reduced rung across -- otherwise the best-ranked provider, the one
    tried first and most likely to be the right answer, would be the only one that
    never got its smaller retry."""
    p = _plugin()
    action = _reduced_action(chosen=A, alternates=())
    attempts = p._resolve_swap_attempts(action, _transport([A]))
    assert len(attempts) == 1
    attempt, _offer = attempts[0]
    assert attempt.npub == A
    assert attempt.amount_sat == action.lightning_amount_sat
    assert attempt.reduced_amount_sat == action.reduced_amount_sat
    assert attempt.reduced_all_in_cost_pct == action.reduced_all_in_cost_pct
    # ... and the rung list built from it has both sizes, in order.
    assert p._attempt_rungs(attempt) == [
        (action.lightning_amount_sat, None),
        (action.reduced_amount_sat, action.reduced_all_in_cost_pct)]


def test_attempt_rungs_is_a_single_rung_without_a_reduction() -> None:
    assert LiquidityPlugin._attempt_rungs(
        ProviderAttempt(npub=A, amount_sat=400_000, all_in_cost_pct=0.4)) == [
            (400_000, 0.4)]


def test_attempt_rungs_refuses_a_reduction_that_does_not_shrink() -> None:
    """Defensive: a bad action must not buy a rung that re-runs the same payment."""
    for bad in (400_000, 500_000, 0, None):
        assert LiquidityPlugin._attempt_rungs(
            ProviderAttempt(npub=A, amount_sat=400_000, all_in_cost_pct=0.4,
                            reduced_amount_sat=bad,
                            reduced_all_in_cost_pct=0.45)) == [(400_000, 0.4)]


# --- the executor's own declines are de-duplicated ------------------------
# A channel whose swaps keep failing is re-evaluated every SWAP_COOLDOWN_SEC. One
# decline row per cycle would push real history out of the 2000-entry decision log
# within days -- the same flood the engine's declines are already protected from,
# except _filter_new_declines cannot help here: it is rebuilt from the engine's own
# output every tick, so a row written straight from the executor would be discarded
# from that set and then re-logged forever.
def _run_unfunded_cascade(p, ln_payment: str = "inflight") -> None:
    sm = _SM({A: _accept_without_funding})
    _run(p, sm, _action(alternates=()), ln_payment=ln_payment)
    p._swap_cooldown_until.clear()          # next cycle


def test_an_identical_consecutive_executor_decline_is_not_relogged() -> None:
    p = _plugin()
    for _ in range(4):
        _run_unfunded_cascade(p)
    assert len(p.declines) == 1, \
        f"a steadily-failing channel logged {len(p.declines)} rows, not 1"


def test_a_failure_after_a_success_is_logged_afresh() -> None:
    """The dedupe must not swallow a recurrence: once the channel has actually
    completed a swap, the next failure is news."""
    p = _plugin()
    _run_unfunded_cascade(p)
    assert len(p.declines) == 1

    # A funded swap on the same channel.
    sm = _SM({A: "txid-ok"})
    _run(p, sm, _action(alternates=()), ln_payment="failed")
    p._swap_cooldown_until.clear()
    assert p.successes == [A]

    _run_unfunded_cascade(p)
    assert len(p.declines) == 2, "the post-success failure was swallowed as a repeat"


def test_the_dedupe_is_per_channel() -> None:
    """Two channels failing must both be reported; the slot is per channel, not one
    global slot that they would take turns evicting."""
    p = _plugin()
    for channel_id, short_id in (("aa" * 32, "1x1x1"), ("bb" * 32, "2x2x2")):
        sm = _SM({A: _accept_without_funding})
        action = ReverseSwapAction(
            channel_id=channel_id, short_id=short_id,
            lightning_amount_sat=400_000, reason="drain", provider_npub=A)
        asyncio.run(p._reverse_swap(_wallet(sm, "inflight"), action, state={},
                                    transport=_transport([A])))
    assert len(p.declines) == 2
    assert {d.short_id for d, _s in p.declines} == {"1x1x1", "2x2x2"}


def test_a_different_failure_reason_is_logged() -> None:
    """The slot holds a signature, not a boolean: a channel that starts failing for
    a different reason must say so."""
    p = _plugin()
    _run_unfunded_cascade(p)
    assert len(p.declines) == 1
    first = p.declines[0][0].reason
    # Same channel, different amount -> different reason text.
    sm = _SM({A: _accept_without_funding})
    _run(p, sm, _action(alternates=(), amount=250_000), ln_payment="inflight")
    assert len(p.declines) == 2
    assert p.declines[1][0].reason != first


def test_a_provider_is_faulted_even_if_its_reduced_rung_is_refused() -> None:
    """The fault for a failed payment must survive the reduced rung ending for a
    reason that never reaches a payment.

    Charging it from "the last rung" looked equivalent and was not: here rung 1's
    payment fails (no fault yet, because a rung remains) and rung 2 is turned away by
    the provider's own bounds before any payment exists (``get_recv_amount`` -> None,
    which is not a fault). The provider then escaped the fault for the payment that
    HAD failed, stayed top-ranked on an unchanged ranking, and got picked again next
    cycle -- exactly the wedge the cascade exists to break.
    """
    def _recv(npub, amt):
        return None if amt < 400_000 else amt - 500     # reduced rung unswappable

    sm = _SM({A: _accept_without_funding}, recv_amount=_recv)
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=()), ln_payment="failed")
    # Only the full-size rung ever reached the provider.
    assert sm.attempts == [(A, 400_000)]
    assert [(n, kw) for n, _r, kw in p.faults] == [(A, {"soft": True})], \
        "the provider escaped its fault because the reduced rung was refused"


def test_the_payment_failure_fault_is_charged_once_per_provider() -> None:
    """Two rungs failing is one observation, and each provider is charged separately
    -- so a two-provider cascade with both payments failing is exactly two faults."""
    sm = _SM({A: _accept_without_funding, B: _accept_without_funding})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=(B,)), ln_payment="failed")
    assert sm.attempts == [(A, 400_000), (A, 360_000),
                           (B, 400_000), (B, 360_000)]
    assert [n for n, _r, _kw in p.faults] == [A, B]
    assert all(kw == {"soft": True} for _n, _r, kw in p.faults)


def test_no_payment_failure_fault_when_the_payment_never_failed() -> None:
    """The flag must not leak across providers: a provider that merely rejected
    createswap gets its own (different) fault and nothing from this path."""
    sm = _SM({A: SwapServerError()})
    p = _plugin()
    _run(p, sm, _reduced_action(alternates=()), ln_payment="failed")
    reasons = [r for _n, r, _kw in p.faults]
    assert reasons == ["swap rejected (likely transient capacity)"]
    assert "reverse-swap Lightning payment failed" not in reasons
