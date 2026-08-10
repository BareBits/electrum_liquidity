"""A reverse swap that failed on OUR side must not be charged to the provider.

``_reconcile_pending_swaps`` has three outcomes for a tracked swap: funded =>
provider success; our Lightning payment failed => the channel *peer*'s fault;
neither, past the stuck timeout => the *provider* left it stuck.

The middle branch was unreachable. It compared
``lnworker.get_payment_status(..., direction=SENT)`` to ``PR_FAILED``, but nothing
in Electrum ever *stores* PR_FAILED on a sent payment -- ``get_payment_status``
returns the persisted ``PaymentInfo.status``, and PR_FAILED is only *derived*, in
``lnworker.get_invoice_status``: PR_UNPAID + not in ``inflight_payments`` + has
route-attempt logs in ``lnworker.logs``. So every swap that did not fund, whatever
the cause, fell through to the 60-minute timeout and blamed the provider -- which
is why every failure looked identical ("stuck: no on-chain funding within 60 min").

These tests pin the derivation and the resulting attribution.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from types import SimpleNamespace
from typing import Dict

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.invoices import PR_FAILED, PR_INFLIGHT, PR_PAID, PR_UNPAID  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    PENDING_SWAPS_DB_KEY,
)

PH = "ab" * 32
NPUB = "npubPROVIDER"
NODE_ID = "cd" * 33


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

    def __init__(self, *, status=PR_UNPAID, logs=(), inflight=()) -> None:
        self._status = status
        self.logs = defaultdict(list)
        for key in logs:
            self.logs[key] = ["htlc-log-entry"]
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


# --- the predicate --------------------------------------------------------
def test_gave_up_after_route_attempts_is_a_failure() -> None:
    """PR_UNPAID + attempt logs + nothing in flight: we tried and gave up."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs=[PH]))
    assert p._ln_payment_failed(w, PH) is True


def test_still_in_flight_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs=[PH], inflight=[PH]))
    assert p._ln_payment_failed(w, PH) is False


def test_never_attempted_is_not_a_failure() -> None:
    """No route logs at all: the payment has not been tried, so nothing failed."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs=[]))
    assert p._ln_payment_failed(w, PH) is False


def test_paid_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_PAID, logs=[PH]))
    assert p._ln_payment_failed(w, PH) is False


def test_inflight_status_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_INFLIGHT, logs=[PH]))
    assert p._ln_payment_failed(w, PH) is False


def test_persisted_pr_failed_is_still_honoured() -> None:
    """Forward compatibility: an Electrum that does store PR_FAILED still works."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_FAILED, logs=[]))
    assert p._ln_payment_failed(w, PH) is True


def test_missing_lnworker_is_not_a_failure() -> None:
    p = _plugin()
    w = _Wallet(None)
    assert p._ln_payment_failed(w, PH) is False


# --- the attribution it drives -------------------------------------------
def _track(p: LiquidityPlugin, w: _Wallet, *, age_sec: float) -> None:
    p._track_pending_swap(w, PH, NPUB, node_id=NODE_ID,
                          channel_id="ee" * 32, fee_basis_sat=0)
    data = p._load_pending_swaps(w)
    data[PH]["started_ts"] = time.time() - age_sec
    p._save_pending_swaps(w, data)


def test_failed_payment_charges_the_peer_not_the_provider() -> None:
    """The point of the fix: a swap that never funded because OUR payment failed
    is a peer fault, resolved immediately -- the provider stays clean."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs=[PH]))
    _track(p, w, age_sec=10)                    # well inside the stuck timeout

    p._reconcile_pending_swaps(w)

    assert p._load_reliability(w) == {}          # provider not blamed
    peers = p._load_peer_reliability(w)
    assert NODE_ID in peers
    assert peers[NODE_ID]["fault_count"] == 1
    assert p._load_pending_swaps(w) == {}        # resolved, not left to time out


def test_unattempted_swap_past_timeout_still_blames_the_provider() -> None:
    """The stuck branch must survive the fix: no payment failure, no funding,
    past the timeout => the provider left it stuck."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs=[]))
    _track(p, w, age_sec=61 * 60)               # past the 60 min default

    p._reconcile_pending_swaps(w)

    stats = p._load_reliability(w)
    assert stats[NPUB]["fault_count"] == 1
    assert "stuck: no on-chain funding" in stats[NPUB]["last_reason"]
    assert p._load_peer_reliability(w) == {}     # peer not blamed
    assert p._load_pending_swaps(w) == {}


def test_failed_payment_resolves_before_the_stuck_timeout_elapses() -> None:
    """Previously this swap sat for a full hour and then blamed the provider;
    now it is attributed on the very next reconciliation tick."""
    p = _plugin()
    w = _Wallet(_Lnworker(status=PR_UNPAID, logs=[PH]))
    _track(p, w, age_sec=5)

    p._reconcile_pending_swaps(w)

    assert PH not in p._load_pending_swaps(w)
    assert NPUB not in p._load_reliability(w)
