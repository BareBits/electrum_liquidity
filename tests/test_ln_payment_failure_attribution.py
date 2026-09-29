"""A reverse swap that failed on OUR side must be charged to whoever actually
failed it -- and to nobody at all when that cannot be shown.

``_reconcile_pending_swaps`` resolves a tracked swap four ways: funded => provider
success; our Lightning payment failed at the FIRST HOP => the channel peer's
fault; our payment failed anywhere else => closed with no fault; nothing at all
past the stuck timeout => the provider left it stuck.

Two separate defects are pinned here.

THE FIRST (the predicate). The payment-failure branch was unreachable. It compared
``lnworker.get_payment_status(..., direction=SENT)`` to ``PR_FAILED``, but nothing
in Electrum ever *stores* PR_FAILED on a sent payment -- ``get_payment_status``
returns the persisted ``PaymentInfo.status``, and PR_FAILED is only *derived*, in
``lnworker.get_invoice_status``: PR_UNPAID + not in ``inflight_payments`` + has
route-attempt logs in ``lnworker.logs``. So every swap that did not fund, whatever
the cause, fell through to the 60-minute timeout and blamed the provider.

THE SECOND (the attribution). Once the branch worked, it charged the channel peer
for *any* failed payment, without ever asking where the HTLC died. "Our payment
failed" does not implicate the first hop: the peer may have forwarded it perfectly
and a node several hops out killed it. Electrum decodes the culprit for us --
``HtlcLog.sender_idx`` is the route index of the node that reported the failure,
and ``route[0]`` is always the far end of our own channel (lnworker itself looks
the peer up by ``shi.route[0].node_id``; ``create_trampoline_route`` builds hop 0
the same way). So the peer is charged only on unanimous hop-0 evidence.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.invoices import PR_FAILED, PR_INFLIGHT, PR_PAID, PR_UNPAID  # type: ignore  # noqa: E402
from electrum.lnutil import HtlcLog  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    PEER_LN_FAILURE_REASON,
    PENDING_SWAPS_DB_KEY,
    LiquidityPlugin,
    _LnFailureAttribution,
)

PH = "ab" * 32
NPUB = "npubPROVIDER"
NODE_ID = "cd" * 33


def _attempt(sender_idx: Optional[int], *, hops: int = 2,
             success: bool = False) -> HtlcLog:
    """One route attempt, as Electrum records it.

    A real ``HtlcLog``, so the field names this code reads stay pinned to the
    ones lnworker writes. ``hops`` is the route length: 1 means the payment went
    straight to its destination over our own channel, i.e. the peer and the swap
    provider are the same node.
    """
    route = [SimpleNamespace(node_id=bytes([i + 1]) * 33) for i in range(hops)]
    return HtlcLog(success=success, amount_msat=100_000, route=route,
                   sender_idx=sender_idx)


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _Lnworker:
    """Reproduces Electrum's real payment-status bookkeeping.

    Note what is *not* here: no code path that writes PR_FAILED. That mirrors
    lnworker, and is the whole reason the old check could never fire.
    """

    def __init__(self, *, status: int = PR_UNPAID,
                 logs: Optional[Dict[str, Sequence[HtlcLog]]] = None,
                 inflight: Iterable[str] = ()) -> None:
        self._status = status
        self.logs: Dict[str, List[HtlcLog]] = {
            k: list(v) for k, v in (logs or {}).items()}
        self.inflight_payments = set(inflight)
        self.swap_manager = SimpleNamespace(get_swap=lambda ph: None)

    def get_payment_status(self, payment_hash, *, direction) -> int:
        return self._status


class _Wallet:
    def __init__(self, lnworker) -> None:
        self.db = _FakeDB()
        self.lnworker = lnworker
        self.saved = 0

    def save_db(self) -> None:
        self.saved += 1


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.attribution")
    p._last_offers = {}
    p.config = SimpleNamespace()
    return p


def _failed(**kwargs) -> _Wallet:
    """A wallet whose payment for PH has definitively failed, with the given
    attempt logs."""
    return _Wallet(_Lnworker(status=PR_UNPAID, logs={PH: kwargs["attempts"]}))


# --- the predicate --------------------------------------------------------
def test_gave_up_after_route_attempts_is_a_failure() -> None:
    """PR_UNPAID + attempt logs + nothing in flight: we tried and gave up."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0)])
    assert p._ln_payment_failed(w, PH) is True


def test_still_in_flight_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs={PH: [_attempt(0)]},
                          inflight=[PH]))
    assert p._ln_payment_failed(w, PH) is False


def test_never_attempted_is_not_a_failure() -> None:
    """No route logs at all: the payment has not been tried, so nothing failed."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs={}))
    assert p._ln_payment_failed(w, PH) is False


def test_paid_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_PAID, logs={PH: [_attempt(0)]}))
    assert p._ln_payment_failed(w, PH) is False


def test_inflight_status_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_INFLIGHT, logs={PH: [_attempt(0)]}))
    assert p._ln_payment_failed(w, PH) is False


def test_persisted_pr_failed_is_still_honoured() -> None:
    """Forward compatibility: an Electrum that does store PR_FAILED still works."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_FAILED, logs={}))
    assert p._ln_payment_failed(w, PH) is True


def test_missing_lnworker_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(None)
    assert p._ln_payment_failed(w, PH) is False


# --- the hop attribution --------------------------------------------------
def test_a_payment_that_has_not_failed_attributes_nothing() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_PAID, logs={PH: [_attempt(0)]}))
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.NOT_FAILED


def test_unanimous_first_hop_blames_the_peer() -> None:
    """Every decodable failure reported by the node at the far end of our own
    channel: the one case the peer is charged for."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0), _attempt(0), _attempt(0)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.PEER


def test_a_single_downstream_failure_pardons_the_peer() -> None:
    """The asymmetry that matters: a peer that carried even one attempt out into
    the network is demonstrably forwarding, so the whole set stops being evidence
    against it. Deliberately conservative -- a soft ranking signal is not worth a
    wrong call."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0), _attempt(0), _attempt(1)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.DOWNSTREAM


def test_all_downstream_failures_blame_nobody_local() -> None:
    p = _plugin()
    w = _failed(attempts=[_attempt(2), _attempt(3)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.DOWNSTREAM


def test_no_decodable_sender_idx_is_unattributable() -> None:
    """Electrum could not decrypt the onion error, or got
    update_fail_malformed_htlc -- the case its own source shrugs at with
    "well... who to penalise now?". Neither can we."""
    p = _plugin()
    w = _failed(attempts=[_attempt(None), _attempt(None)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.UNATTRIBUTABLE


def test_undecodable_attempts_are_silent_not_exculpatory() -> None:
    """A ``sender_idx`` of None says nothing about the peer, so it must not
    dilute the hop-0 attempts that DO say something."""
    p = _plugin()
    w = _failed(attempts=[_attempt(None), _attempt(0)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.PEER


def test_undecodable_attempts_do_not_rescue_a_downstream_verdict() -> None:
    p = _plugin()
    w = _failed(attempts=[_attempt(None), _attempt(1)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.DOWNSTREAM


def test_single_hop_route_means_the_peer_is_the_provider() -> None:
    """The route has one hop, so the node that reported the error is also the
    payment's destination: the swap provider's own node. That is the provider
    rejecting us, and ``_charge_payment_failure`` already charged it."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0, hops=1)])
    assert (p._ln_failure_attribution(w, PH)
            is _LnFailureAttribution.PEER_IS_DESTINATION)


def test_one_multihop_attempt_makes_it_a_real_forwarding_failure() -> None:
    """Mixed route lengths: the multi-hop attempt shows the peer failing as a
    FORWARDER, which is a genuine partner-quality signal, so it decides."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0, hops=1), _attempt(0, hops=3)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.PEER


def test_successful_shards_are_not_evidence_against_anyone() -> None:
    """An MPP shard that landed says nothing about who failed the others."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0, success=True), _attempt(1)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.DOWNSTREAM


def test_only_successful_attempts_leave_nothing_to_attribute() -> None:
    p = _plugin()
    w = _failed(attempts=[_attempt(0, success=True)])
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.UNATTRIBUTABLE


def test_an_attempt_without_a_route_is_skipped() -> None:
    """``sender_idx`` indexes the route, so one without a route cannot be placed
    and must not be read as hop 0."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs={PH: [
        HtlcLog(success=False, amount_msat=1000, route=None, sender_idx=0)]}))
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.UNATTRIBUTABLE


def test_a_non_integer_sender_idx_is_skipped() -> None:
    """Defensive: a malformed log entry must not crash reconciliation, nor be
    read as hop 0."""
    p = _plugin()
    w = _failed(attempts=[_attempt("not-an-index")])  # type: ignore[arg-type]
    assert p._ln_failure_attribution(w, PH) is _LnFailureAttribution.UNATTRIBUTABLE


def test_only_the_three_conclusive_verdicts_resolve_a_record() -> None:
    """The two inconclusive ones must leave the record for the stuck-timeout arm,
    which is what lets UNATTRIBUTABLE fall through to the provider."""
    assert _LnFailureAttribution.PEER.is_resolved is True
    assert _LnFailureAttribution.DOWNSTREAM.is_resolved is True
    assert _LnFailureAttribution.PEER_IS_DESTINATION.is_resolved is True
    assert _LnFailureAttribution.NOT_FAILED.is_resolved is False
    assert _LnFailureAttribution.UNATTRIBUTABLE.is_resolved is False


# --- the attribution it drives -------------------------------------------
def _track(p: LiquidityPlugin, w: _Wallet, *, age_sec: float) -> None:
    p._track_pending_swap(w, PH, NPUB, node_id=NODE_ID,
                          channel_id="ee" * 32, fee_basis_sat=0)
    data = p._load_pending_swaps(w)
    data[PH]["started_ts"] = time.time() - age_sec
    p._save_pending_swaps(w, data)


def test_first_hop_failure_charges_the_peer_not_the_provider() -> None:
    """The point of the original fix: a swap that never funded because OUR
    payment failed at the peer is a peer fault, resolved immediately -- the
    provider stays clean."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0)])
    _track(p, w, age_sec=10)                    # well inside the stuck timeout

    p._reconcile_pending_swaps(w)

    assert p._load_reliability(w) == {}          # provider not blamed
    peers = p._load_peer_reliability(w)
    assert NODE_ID in peers
    assert peers[NODE_ID]["fault_count"] == 1
    assert peers[NODE_ID]["last_reason"] == PEER_LN_FAILURE_REASON
    assert peers[NODE_ID].get("hard_fault_count", 0) == 0   # soft: never bans
    assert p._load_pending_swaps(w) == {}        # resolved, not left to time out


def test_a_downstream_failure_charges_nobody() -> None:
    """The point of THIS fix. The peer forwarded the HTLC and something further
    out killed it; neither the peer nor the provider did anything wrong, so the
    record is closed silently."""
    p = _plugin()
    w = _failed(attempts=[_attempt(2)])
    _track(p, w, age_sec=10)

    p._reconcile_pending_swaps(w)

    assert p._load_peer_reliability(w) == {}
    assert p._load_reliability(w) == {}
    assert p._load_pending_swaps(w) == {}        # resolved, so it cannot later
                                                # age into a provider fault


def test_the_provider_rejecting_us_is_not_also_a_peer_fault() -> None:
    """One-hop route: our peer IS the provider's node. It already took a soft
    provider fault in ``_charge_payment_failure``; charging the peer store too
    would demote one node twice for one event."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0, hops=1)])
    _track(p, w, age_sec=10)

    p._reconcile_pending_swaps(w)

    assert p._load_peer_reliability(w) == {}
    assert p._load_reliability(w) == {}
    assert p._load_pending_swaps(w) == {}


def test_an_unattributable_failure_waits_rather_than_guessing() -> None:
    """Nothing decodable: no one is charged yet, and -- crucially -- the record
    is NOT resolved, so it stays eligible for the stuck timeout."""
    p = _plugin()
    w = _failed(attempts=[_attempt(None)])
    _track(p, w, age_sec=10)

    p._reconcile_pending_swaps(w)

    assert p._load_peer_reliability(w) == {}
    assert p._load_reliability(w) == {}
    assert PH in p._load_pending_swaps(w)       # still tracked


def test_an_unattributable_failure_ages_into_a_provider_fault() -> None:
    """...and once the stuck timeout passes it lands where it did before this
    attribution existed: on the provider, which never delivered the funding."""
    p = _plugin()
    w = _failed(attempts=[_attempt(None)])
    _track(p, w, age_sec=61 * 60)               # past the 60 min default

    p._reconcile_pending_swaps(w)

    stats = p._load_reliability(w)
    assert stats[NPUB]["fault_count"] == 1
    assert "stuck: no on-chain funding" in stats[NPUB]["last_reason"]
    assert p._load_peer_reliability(w) == {}    # peer still not blamed
    assert p._load_pending_swaps(w) == {}


def test_unattempted_swap_past_timeout_still_blames_the_provider() -> None:
    """The stuck branch must survive both fixes: no payment failure, no funding,
    past the timeout => the provider left it stuck."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs={}))
    _track(p, w, age_sec=61 * 60)

    p._reconcile_pending_swaps(w)

    stats = p._load_reliability(w)
    assert stats[NPUB]["fault_count"] == 1
    assert "stuck: no on-chain funding" in stats[NPUB]["last_reason"]
    assert p._load_peer_reliability(w) == {}
    assert p._load_pending_swaps(w) == {}


def test_failed_payment_resolves_before_the_stuck_timeout_elapses() -> None:
    """Previously this swap sat for a full hour and then blamed the provider;
    now it is attributed on the very next reconciliation tick."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0)])
    _track(p, w, age_sec=5)

    p._reconcile_pending_swaps(w)

    assert PH not in p._load_pending_swaps(w)
    assert NPUB not in p._load_reliability(w)


def test_a_funded_swap_still_beats_every_payment_verdict() -> None:
    """Branch order: funding is the ground truth. Even with hop-0 logs sitting
    there, a swap the provider actually funded is a provider SUCCESS and no
    peer fault."""
    p = _plugin()
    w = _failed(attempts=[_attempt(0)])
    w.lnworker.swap_manager = SimpleNamespace(
        get_swap=lambda ph: SimpleNamespace(is_redeemed=True, funding_txid="ff" * 32))
    _track(p, w, age_sec=10)

    p._reconcile_pending_swaps(w)

    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    assert p._load_peer_reliability(w) == {}
    assert p._load_pending_swaps(w) == {}
