"""The failover gate: ``_unfunded_swap_verdict``.

This is the function that decides, after a reverse-swap attempt has not
produced a funding txid, whether it is safe to try a DIFFERENT provider with
the same channel. Getting it wrong in the permissive direction means a double
drain: two providers each paid out of one channel's local balance, past the
outbound floor the user configured and for more than the plugin ever decided to
send. Getting it wrong in the conservative direction costs one skipped cascade.

So the three answers are deliberately not two:

  NEXT          nothing in flight, nothing settled -- the channel is free.
  HTLC_PENDING  an HTLC is still live. UNDECIDED, and explicitly not the same
                as committed; the caller waits it out.
  COMMITTED     something settled, OR we could not tell. Every "cannot tell"
                path lands here.

The distinction between HTLC_PENDING and COMMITTED is the one that used to kill
cascades, and the docstring on the function explains why: both legs of a failed
swap reach "HTLC still out" on essentially every failure -- the main payment
because ``pay_to_node`` returns while HTLCs are unresolved, the prepayment
because ``reverse_swap`` never waits for it -- so answering COMMITTED to a live
HTLC is answering COMMITTED to the normal case.

It had no unit test. The rig suite exercises the happy paths through a real
provider, but the arms that matter most here are the ones a real provider will
not produce on demand: an unreadable swap object, an unparseable payment hash,
a settled prepayment. Those are asserted directly.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Dict, List, Optional, Set

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.invoices import PR_PAID, PR_UNPAID  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    _SwapAttempt,
)

MAIN = "aa" * 32
PREPAY = "bb" * 32


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.verdict")
    return p


class _Lnworker:
    """Only the two lookups the gate makes.

    ``inflight`` is the set of payment hashes with a live HTLC;
    ``statuses`` maps a hash to what ``get_payment_status`` answers. Either
    accessor can be told to raise, because "the lookup failed" is a distinct
    arm of the gate and must resolve to COMMITTED.
    """

    def __init__(self, *, inflight: Set[bytes] = frozenset(),
                 statuses: Optional[Dict[bytes, int]] = None,
                 payments_raise: bool = False,
                 status_raises: bool = False) -> None:
        self._inflight = set(inflight)
        self._statuses = statuses or {}
        self._payments_raise = payments_raise
        self._status_raises = status_raises

    def get_payments(self, status=None):
        if self._payments_raise:
            raise RuntimeError("lnworker is mid-teardown")
        return set(self._inflight) if status == "inflight" else set()

    def get_payment_status(self, payment_hash, direction=None):
        if self._status_raises:
            raise RuntimeError("payment db is not readable")
        return self._statuses.get(payment_hash, PR_UNPAID)


class _SM:
    """Swap manager stub: maps a main payment hash to a swap object."""

    def __init__(self, swaps: Optional[Dict[bytes, object]] = None,
                 *, raises: bool = False) -> None:
        self._by_hash = swaps or {}
        self._raises = raises

    def get_swap(self, payment_hash: bytes):
        if self._raises:
            raise RuntimeError("swap store is unreadable")
        return self._by_hash.get(payment_hash)


def _swap(*, prepay_hash: Optional[bytes] = None, funding_txid=None):
    return SimpleNamespace(prepay_hash=prepay_hash, funding_txid=funding_txid)


def _wallet(lnworker) -> SimpleNamespace:
    return SimpleNamespace(lnworker=lnworker)


# --- the clear case -------------------------------------------------------
def test_nothing_in_flight_and_nothing_settled_frees_the_channel() -> None:
    """The only arm that lets a second provider be tried."""
    p = _plugin()
    sm = _SM({bytes.fromhex(MAIN): _swap()})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker()), sm, {MAIN})
    assert verdict is _SwapAttempt.NEXT, reason
    assert "channel is free" in reason


# --- undecided, not committed --------------------------------------------
def test_a_live_main_htlc_is_undecided_not_committed() -> None:
    """The ordinary state of a payment Electrum has just given up on. Calling
    this COMMITTED is what used to stop every cascade."""
    p = _plugin()
    main = bytes.fromhex(MAIN)
    sm = _SM({main: _swap()})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(inflight={main})), sm, {MAIN})
    assert verdict is _SwapAttempt.HTLC_PENDING, reason
    assert "main Lightning payment" in reason, \
        "the reason must name which leg, since only the main one drains us"


def test_a_live_prepayment_htlc_is_also_undecided() -> None:
    """``reverse_swap`` never waits for the mining-fee prepayment, so its HTLC
    is routinely still out when the attempt returns."""
    p = _plugin()
    main, prepay = bytes.fromhex(MAIN), bytes.fromhex(PREPAY)
    sm = _SM({main: _swap(prepay_hash=prepay)})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(inflight={prepay})), sm, {MAIN})
    assert verdict is _SwapAttempt.HTLC_PENDING, reason
    assert "prepayment" in reason


def test_the_main_leg_is_named_when_both_are_in_flight() -> None:
    """Both legs live is the common case; the reason names the main payment
    because that is the one draining this channel."""
    p = _plugin()
    main, prepay = bytes.fromhex(MAIN), bytes.fromhex(PREPAY)
    sm = _SM({main: _swap(prepay_hash=prepay)})
    _verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(inflight={main, prepay})), sm, {MAIN})
    assert "main Lightning payment" in reason


# --- settled means the swap is live --------------------------------------
def test_a_settled_main_payment_stops_the_cascade() -> None:
    """A reverse swap's main invoice is a HOLD invoice, so a payment that has
    merely reached the provider reads as in-flight, not paid. PR_PAID here
    therefore means the swap really is progressing and its funding txid is just
    lagging -- trying another provider would drain the channel twice."""
    p = _plugin()
    main = bytes.fromhex(MAIN)
    sm = _SM({main: _swap()})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(statuses={main: PR_PAID})), sm, {MAIN})
    assert verdict is _SwapAttempt.COMMITTED, reason
    assert "has settled" in reason


def test_a_settled_prepayment_also_stops_the_cascade() -> None:
    """The provider only fulfils the prepayment once the main payment's MPP set
    has arrived, so a settled prepayment is evidence the swap is live even
    though the main invoice is still held."""
    p = _plugin()
    main, prepay = bytes.fromhex(MAIN), bytes.fromhex(PREPAY)
    sm = _SM({main: _swap(prepay_hash=prepay)})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(statuses={prepay: PR_PAID})), sm, {MAIN})
    assert verdict is _SwapAttempt.COMMITTED, reason


# --- every "cannot tell" is COMMITTED ------------------------------------
# These are the arms the rig cannot produce on demand, and they are the ones
# where a permissive answer costs real money.
def test_a_missing_lnworker_is_committed() -> None:
    """The wallet is being torn down, or is a stub. Nothing can be checked, so
    nothing can be cleared."""
    p = _plugin()
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(None), _SM(), {MAIN})
    assert verdict is _SwapAttempt.COMMITTED
    assert "Lightning worker is gone" in reason


def test_no_tracked_swap_is_committed() -> None:
    """No swap object was created, so there is no identity to inspect -- and a
    swap we cannot identify is one we cannot clear."""
    p = _plugin()
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker()), _SM(), set())
    assert verdict is _SwapAttempt.COMMITTED
    assert "no swap object" in reason


def test_an_unparseable_payment_hash_is_committed() -> None:
    """Defensive: the tracked set comes from Electrum's own keys, but a value
    that is not hex must not become an "all clear" via an exception."""
    p = _plugin()
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker()), _SM(), {"not-hex-at-all"})
    assert verdict is _SwapAttempt.COMMITTED
    assert "could not be parsed" in reason


def test_an_unreadable_swap_object_is_committed() -> None:
    """``get_swap`` raising means we cannot learn the prepay hash, so we cannot
    rule out a live prepayment HTLC."""
    p = _plugin()
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker()), _SM(raises=True), {MAIN})
    assert verdict is _SwapAttempt.COMMITTED
    assert "prepay hash" in reason


def test_an_unreadable_payment_status_is_committed() -> None:
    """The settled check itself failing has to be conservative too."""
    p = _plugin()
    main = bytes.fromhex(MAIN)
    sm = _SM({main: _swap()})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(status_raises=True)), sm, {MAIN})
    assert verdict is _SwapAttempt.COMMITTED
    assert "payment status" in reason


def test_an_unreadable_inflight_lookup_reads_as_in_flight() -> None:
    """``_payment_still_inflight`` answers True on any failure, so an
    unreadable payment list is HTLC_PENDING rather than a free channel.

    Not COMMITTED, deliberately: the caller can wait that out and re-ask, which
    is strictly better than giving up on the cascade, and it is still safe --
    nothing is tried while the answer is unknown.
    """
    p = _plugin()
    main = bytes.fromhex(MAIN)
    sm = _SM({main: _swap()})
    verdict, reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(payments_raise=True)), sm, {MAIN})
    assert verdict is _SwapAttempt.HTLC_PENDING, reason


# --- more than one tracked swap ------------------------------------------
def test_one_committed_swap_among_several_decides_the_whole_verdict() -> None:
    """Any single piece of evidence that a swap is live stops the cascade; a
    clear second swap cannot vote it back down."""
    p = _plugin()
    other = "cc" * 32
    main, other_h = bytes.fromhex(MAIN), bytes.fromhex(other)
    sm = _SM({main: _swap(), other_h: _swap()})
    verdict, _reason = p._unfunded_swap_verdict(
        _wallet(_Lnworker(statuses={other_h: PR_PAID})), sm, {MAIN, other})
    assert verdict is _SwapAttempt.COMMITTED


# --- the funding probe its caller uses first ------------------------------
def test_any_tracked_swap_funded_spots_a_funding_txid() -> None:
    """The probe the cascade makes BEFORE asking the gate: a funded swap needs
    no further reasoning. Keyed by hex string, which is how ``sm._swaps`` is
    keyed -- not by the decoded bytes the gate itself uses."""
    p = _plugin()
    funded = SimpleNamespace(_swaps={MAIN: _swap(funding_txid="deadbeef")})
    assert p._any_tracked_swap_funded(funded, {MAIN}) is True
    unfunded = SimpleNamespace(_swaps={MAIN: _swap(funding_txid=None)})
    assert p._any_tracked_swap_funded(unfunded, {MAIN}) is False
    # A hash we are tracking that the swap manager has never heard of.
    assert p._any_tracked_swap_funded(unfunded, {"ff" * 32}) is False


def test_any_tracked_swap_funded_answers_false_when_it_cannot_look() -> None:
    """False is the conservative answer here, unlike everywhere else in this
    file: the caller's fallback for "not funded" is the verdict gate above,
    which is itself conservative. Answering True would skip that gate and
    report a swap as alive on no evidence."""
    p = _plugin()

    class _Boom:
        @property
        def _swaps(self):
            raise RuntimeError("nope")

    assert p._any_tracked_swap_funded(_Boom(), {MAIN}) is False
