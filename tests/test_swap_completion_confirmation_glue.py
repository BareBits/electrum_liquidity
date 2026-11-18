"""A reverse swap is complete when its funding tx CONFIRMS, not when it is seen.

The distinction, and why it is not pedantry. Electrum's ``reverse_swap`` returns
``swap.funding_txid``, which the swap manager's watcher sets the moment it spots
a matching output in an *unconfirmed* transaction -- and Electrum says in as many
words that it can change afterwards (``submarine_swaps.py``: "note:
swap.funding_txid can change due to RBF, it will get updated here"). Electrum's
own claim path tests for a confirmation before acting
(``_claim_to_output``: ``get_tx_height(...).height() > TX_HEIGHT_UNCONFIRMED``).

The plugin used to treat that first sighting as a completed swap, which did three
things on sats that might never arrive:

  * cleared the provider's ``consecutive_faults`` -- so a provider could wipe an
    accumulated reliability penalty by funding and then replacing the tx;
  * accrued the DEV FEE, charging the user for a conversion that did not happen;
  * bumped the lifetime "swaps completed / sats swapped" totals.

So the executor now logs the action (the swap did commit; the channel was
drained) and tracks the swap, while reconciliation grants the success, the fee and
the totals once the funding confirms.

Covered here:
  * an unconfirmed funding grants nothing, and keeps the record;
  * a confirmation grants all three, exactly once;
  * an already-swept swap (``is_redeemed``) counts without needing the height;
  * a provider that funded is NOT faulted as "stuck" while we wait for its
    confirmation -- the arm ordering that makes that true;
  * a funding that never confirms eventually ages out instead of being held
    forever, and without ever paying the fee;
  * an unreadable confirmation state defers rather than grants.

Skipped outside the Electrum venv.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    PENDING_SWAPS_DB_KEY,
    PENDING_SWAP_MAX_AGE_SEC,
)

from test_dev_fee_glue import _FakeWallet, _plugin  # type: ignore  # noqa: E402

PH = "ab" * 32
NPUB = "npub1" + "q" * 58
BASIS = 800_000          # net on-chain sats; 0.1% of it is the 800 sat fee


def _wallet(swap, *, funding_height: int = 0, started_ts=None) -> "_FakeWallet":
    """A wallet tracking one pending swap, with ``swap`` as the manager's view of
    it and ``funding_height`` as its funding tx's confirmation state."""
    sm = SimpleNamespace(_swaps={}, get_swap=lambda h: swap)
    ln = SimpleNamespace(swap_manager=sm)
    w = _FakeWallet(ln, funding_height=funding_height)
    w.db.put(PENDING_SWAPS_DB_KEY, {
        PH: {"npub": NPUB,
             "started_ts": time.time() if started_ts is None else started_ts,
             "node_id": "", "channel_id": "cd" * 32, "fee_basis_sat": BASIS},
    })
    return w


def _totals(p, w) -> dict:
    return p._load_usage_totals(w)


# --- the gate -------------------------------------------------------------
def test_an_unconfirmed_funding_grants_nothing_and_keeps_the_record() -> None:
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    p, w = _plugin(), _wallet(swap, funding_height=0)      # 0 = UNCONFIRMED
    p._reconcile_pending_swaps(w)

    assert p._load_dev_fee_owed(w) == 0, "the fee was charged before confirmation"
    assert "success_count" not in p._load_reliability(w).get(NPUB, {})
    assert _totals(p, w).get("swaps_completed_total", 0) == 0
    # The record survives, so the next tick can look again.
    assert PH in p._load_pending_swaps(w)


def test_a_confirmed_funding_grants_the_fee_success_and_totals_once() -> None:
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    p, w = _plugin(), _wallet(swap, funding_height=101)    # confirmed
    p._reconcile_pending_swaps(w)

    assert p._load_dev_fee_owed(w) == 800                  # 0.1% of 800_000
    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    totals = _totals(p, w)
    assert totals["swaps_completed_total"] == 1
    assert totals["sats_swapped_total"] == BASIS
    # Consumed, so a second pass cannot charge the fee twice.
    assert p._load_pending_swaps(w) == {}
    p._reconcile_pending_swaps(w)
    assert p._load_dev_fee_owed(w) == 800
    assert _totals(p, w)["swaps_completed_total"] == 1


def test_an_already_swept_swap_counts_without_a_confirmation_height() -> None:
    """``is_redeemed`` means we have claimed the output ourselves -- stronger
    evidence than any confirmation count, and it must not be gated behind one."""
    swap = SimpleNamespace(is_redeemed=True, funding_txid=None)
    p, w = _plugin(), _wallet(swap, funding_height=0)
    p._reconcile_pending_swaps(w)
    assert p._load_dev_fee_owed(w) == 800
    assert p._load_reliability(w)[NPUB]["success_count"] == 1


def test_an_unreadable_confirmation_state_defers_rather_than_grants() -> None:
    """Best-effort and conservative: if the height cannot be read, the answer is
    "not yet". Deferring costs a tick; granting would charge a fee we cannot
    justify."""
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    p, w = _plugin(), _wallet(swap, funding_height=0)

    def _boom(txid):
        raise RuntimeError("adb unavailable")
    w.adb.get_tx_height = _boom

    assert p._swap_funding_confirmed(w, swap) is False
    p._reconcile_pending_swaps(w)
    assert p._load_dev_fee_owed(w) == 0
    assert PH in p._load_pending_swaps(w)


# --- the provider must not be blamed for our waiting ---------------------
def test_a_provider_that_funded_is_not_faulted_as_stuck_while_unconfirmed() -> None:
    """The arm-ordering bug this gate could easily have introduced.

    The record is older than the stuck-swap timeout, so without an explicit
    "funding seen, awaiting confirmation" arm it would fall through to the
    timeout arm and be charged as "stuck: no on-chain funding" -- faulting a
    provider that funded perfectly well, because WE are waiting for a block.
    """
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    p = _plugin()
    # Well past the default 60-minute stuck timeout.
    w = _wallet(swap, funding_height=0, started_ts=time.time() - 7200)
    p._reconcile_pending_swaps(w)

    row = p._load_reliability(w).get(NPUB, {})
    assert row.get("fault_count", 0) == 0, (
        f"a provider that funded was faulted while awaiting a confirmation: {row}")
    assert PH in p._load_pending_swaps(w), "the record should still be waiting"


def test_a_swap_that_never_funded_is_still_faulted_as_stuck() -> None:
    """The counterpart: with NO funding txid at all, the stuck-swap fault still
    fires as it always did. The new arm must not have swallowed it."""
    swap = SimpleNamespace(is_redeemed=False, funding_txid=None)
    p = _plugin()
    w = _wallet(swap, funding_height=0, started_ts=time.time() - 7200)
    p._reconcile_pending_swaps(w)

    assert p._load_reliability(w)[NPUB]["fault_count"] == 1
    assert p._load_pending_swaps(w) == {}
    assert p._load_dev_fee_owed(w) == 0


def test_a_funding_that_never_confirms_ages_out_without_charging_the_fee() -> None:
    """A replaced funding tx would otherwise hold its record forever. The
    staleness arm sits ABOVE the awaiting-confirmation arm precisely so this
    terminates -- and it drops the record unresolved, so no fee is charged."""
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    p = _plugin()
    w = _wallet(swap, funding_height=0,
                started_ts=time.time() - PENDING_SWAP_MAX_AGE_SEC - 1)
    p._reconcile_pending_swaps(w)

    assert p._load_pending_swaps(w) == {}, "the stale record should be dropped"
    assert p._load_dev_fee_owed(w) == 0
    assert "success_count" not in p._load_reliability(w).get(NPUB, {})
    # Nor is it faulted: too old to be evidence against anybody.
    assert p._load_reliability(w).get(NPUB, {}).get("fault_count", 0) == 0


# --- the helper in isolation ---------------------------------------------
@pytest.mark.parametrize("height,expected", [
    (0, False),        # TX_HEIGHT_UNCONFIRMED
    (-1, False),       # TX_HEIGHT_UNCONF_PARENT / local
    (-2, False),
    (1, True),
    (101, True),
])
def test_swap_funding_confirmed_reads_the_height(height, expected) -> None:
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    p, w = _plugin(), _FakeWallet(None, funding_height=height)
    assert p._swap_funding_confirmed(w, swap) is expected


def test_swap_funding_confirmed_is_false_without_a_txid() -> None:
    p, w = _plugin(), _FakeWallet(None, funding_height=101)
    assert p._swap_funding_confirmed(
        w, SimpleNamespace(is_redeemed=False, funding_txid=None)) is False


def test_swap_delivered_accepts_either_evidence() -> None:
    p = _plugin()
    redeemed = SimpleNamespace(is_redeemed=True, funding_txid=None)
    confirmed = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    unconfirmed = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    assert p._swap_delivered(_FakeWallet(None, funding_height=0), redeemed) is True
    assert p._swap_delivered(_FakeWallet(None, funding_height=5), confirmed) is True
    assert p._swap_delivered(_FakeWallet(None, funding_height=0), unconfirmed) is False


# --- the e2e suite's literal key strings ---------------------------------
def test_the_db_keys_the_e2e_suite_hardcodes_still_match() -> None:
    """Pin the wallet-db key strings that ``e2e/tests/test_reverse_swap_e2e.py``
    writes out as literals.

    It cannot import the plugin to read them: the plugin registers its ConfigVars
    on SimpleConfig at import time into a process-global registry, and the e2e
    pytest process already holds the package as
    ``electrum.plugins.inbound_liquidity`` -- so a second import under another
    name re-runs the module body and dies with "duplicate config key str".

    The hazard a literal carries is silence: a renamed key makes the e2e test
    read ``{}`` and pass for the wrong reason. This closes it here, where
    importing the plugin is safe, so a rename breaks loudly in the fast suite.
    """
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        PENDING_SWAPS_DB_KEY as real_pending,
        USAGE_TOTALS_DB_KEY as real_totals,
    )
    assert real_pending == "inbound_liquidity_pending_swap_providers"
    assert real_totals == "inbound_liquidity_usage_totals"
