"""The dev-fee payout must never pay the same fee twice.

The bug this suite pins down. ``lnworker.pay_invoice`` returns ``(False, log)``
once ``pay_to_node`` passes ``LNWallet.PAYMENT_TIMEOUT`` (120s), and
``pay_to_node``'s ``finally`` deliberately keeps the paysession alive while HTLCs
are unresolved (lnworker.py) -- so "not success" does NOT prove nothing was sent.
The payout used to read it as proof: it left the owed ledger untouched, set a
300s backoff, and paid again on the next tick. If the first HTLC then settled,
the user paid the fee twice.

Electrum's own re-pay guard cannot catch it. That guard keys on the payment HASH
("A previous attempt to pay this invoice did not clear"), and each retry mints a
FRESH invoice from the LNURL callback -- a different hash every time. The daily
cap did not bound it either, because ``_record_dev_fee_payment`` only stamps the
cap on the success branch, so a silently-settled payment was never counted
against it.

The fix is the liquidity sink's guard, which the payout path was missing: ask
whether an HTLC may still be live, and if so HOLD -- persisting the attempt so
the hold survives a restart -- until the payment's fate is known.

Covered here:
  * a reported failure with an HTLC possibly live holds, and does not re-pay;
  * the hold is persisted, so a restart mid-payment still holds;
  * a payment that later settles draws the ledger down exactly ONCE and stamps
    the daily cap, rather than being paid again;
  * a payment that definitively failed clears the hold and the fee stays owed;
  * an outcome that can never be established resolves as PAID, not as failed --
    the user's funds are not risked to collect our own fee;
  * an invoice with no readable payment hash is not paid at all, because an
    untracked payment is the one that cannot be prevented from repeating;
  * a timeout holds, exactly like a reported failure with a live HTLC.

Reuses the fakes from test_dev_fee_glue. Skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, Set

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEV_FEE_INFLIGHT_DB_KEY,
    DEV_FEE_INFLIGHT_MAX_AGE_SEC,
    DEV_FEE_OWED_DB_KEY,
)

from test_dev_fee_glue import (  # type: ignore  # noqa: E402
    ADDR, _FakeWallet, _hash_for, _patch_lnurl, _plugin,
)


class _LnWorker:
    """A Lightning worker whose verdict on one payment is scripted.

    The three states that matter are exactly the ones the real derivation reads
    (see ``_ln_payment_failed``): PAID, still in flight, and definitively failed
    (PR_UNPAID with route-attempt logs and nothing in flight).
    """

    def __init__(self, *, pay_result=(False, []), state: str = "inflight",
                 raises: Optional[BaseException] = None) -> None:
        self._pay_result = pay_result
        self.state = state
        self._raises = raises
        self.paid_amounts: list = []

    async def pay_invoice(self, invoice, **kw):
        self.paid_amounts.append(invoice.get_amount_sat())
        if self._raises is not None:
            raise self._raises
        return self._pay_result

    # --- what Electrum would say about the payment afterwards -------------
    def _hashes(self) -> Set[bytes]:
        return {_hash_for(sat) for sat in self.paid_amounts}

    def get_payments(self, *, status=None):
        if status == "inflight" and self.state == "inflight":
            return self._hashes()
        return set()

    def get_payment_status(self, payment_hash, direction=None):
        from electrum.invoices import PR_PAID, PR_UNPAID  # type: ignore
        return PR_PAID if self.state == "paid" else PR_UNPAID

    @property
    def inflight_payments(self):
        return ({h.hex() for h in self._hashes()}
                if self.state == "inflight" else set())

    @property
    def logs(self):
        # Route attempts were logged and nothing is in flight => we gave up.
        if self.state != "failed":
            return {}
        return {h.hex(): ["route attempt"] for h in self._hashes()}


def _owed(p, w, sat: int) -> None:
    w.db.put(DEV_FEE_OWED_DB_KEY, sat)


def _run_payout(p, w, amount: int = 3000) -> None:
    asyncio.run(p._do_pay_dev_fee(w, ADDR, amount))


# --- the hold -------------------------------------------------------------
def test_a_failure_with_a_live_htlc_holds_instead_of_retrying(monkeypatch) -> None:
    ln = _LnWorker(pay_result=(False, []), state="inflight")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)
    _run_payout(p, w)

    # Paid once, and the ledger is untouched -- the fee is neither drawn down
    # (we cannot show it settled) nor stamped against the cap.
    assert ln.paid_amounts == [3000]
    assert p._load_dev_fee_owed(w) == 3000
    assert p._dev_fee_paid_last_24h(w) == 0
    # ... and the attempt is remembered, which is what stops the retry.
    record = p._load_dev_fee_inflight(w)
    assert record is not None
    assert record["payment_hash"] == _hash_for(3000).hex()
    assert record["amount_sat"] == 3000


def test_the_gate_refuses_to_start_another_payout_while_holding() -> None:
    """``_maybe_pay_dev_fee`` is the tick-level gate: with an unresolved payout
    on record it must not schedule another, whatever is owed."""
    ln = _LnWorker(state="inflight")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 50_000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time(),
    })
    scheduled = []
    p._do_pay_dev_fee = lambda *a, **k: scheduled.append(a)  # type: ignore[assignment]
    p._maybe_pay_dev_fee(w)
    assert scheduled == [], "a payout was started while one was unresolved"


def test_the_hold_survives_a_restart() -> None:
    """The record is in the wallet db, not in memory, so a process that died
    mid-payment still holds when it comes back.

    Simulated by building a FRESH plugin over the same wallet -- which is what a
    restart is from the ledger's point of view.
    """
    ln = _LnWorker(state="inflight")
    p1, w = _plugin(), _FakeWallet(ln)
    _owed(p1, w, 3000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time(),
    })
    p2 = _plugin()                      # a new process, same wallet file
    assert p2._resolve_dev_fee_inflight(w) is True
    assert p2._load_dev_fee_owed(w) == 3000


# --- resolution -----------------------------------------------------------
def test_a_held_payment_that_settles_is_drawn_down_exactly_once() -> None:
    """The whole point. The payment reported failure, then settled. The fee must
    be accounted for ONCE -- not paid again, and not left owed forever."""
    ln = _LnWorker(state="inflight")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time(),
    })
    ln.paid_amounts = [3000]            # so the hash lookups resolve
    ln.state = "paid"                   # the HTLC settled after all

    assert p._resolve_dev_fee_inflight(w) is False     # resolved
    assert p._load_dev_fee_owed(w) == 0                # drawn down
    assert p._dev_fee_paid_last_24h(w) == 3000         # and stamped on the cap
    assert p._load_dev_fee_inflight(w) is None         # record cleared
    assert ln.paid_amounts == [3000], "it must not have been paid a second time"

    # Idempotent: a second resolve cannot double-draw.
    assert p._resolve_dev_fee_inflight(w) is False
    assert p._load_dev_fee_owed(w) == 0
    assert p._dev_fee_paid_last_24h(w) == 3000


def test_a_definitively_failed_payment_clears_the_hold_and_keeps_the_fee_owed() -> None:
    ln = _LnWorker(state="failed")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time(),
    })
    ln.paid_amounts = [3000]

    assert p._resolve_dev_fee_inflight(w) is False      # free to retry
    assert p._load_dev_fee_owed(w) == 3000              # nothing was paid
    assert p._dev_fee_paid_last_24h(w) == 0             # so nothing is stamped
    assert p._load_dev_fee_inflight(w) is None


def test_an_unestablishable_outcome_resolves_as_paid_not_as_failed() -> None:
    """The asymmetry that decides this. Being wrong about "failed" charges the
    USER a second time for a fee they already paid; being wrong about "paid"
    costs the DEVELOPER a fee that was owed. So an outcome we cannot establish,
    after a week of asking, resolves in the user's favour."""
    ln = _LnWorker(state="unknown")      # never paid, never in flight, no logs
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time() - DEV_FEE_INFLIGHT_MAX_AGE_SEC - 1,
    })
    assert p._resolve_dev_fee_inflight(w) is False
    assert p._load_dev_fee_owed(w) == 0                 # treated as paid
    assert p._dev_fee_paid_last_24h(w) == 3000
    assert p._load_dev_fee_inflight(w) is None


def test_an_unestablishable_outcome_still_holds_before_it_ages_out() -> None:
    """Up to that deadline it keeps asking rather than guessing either way."""
    ln = _LnWorker(state="unknown")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time() - 60,
    })
    assert p._resolve_dev_fee_inflight(w) is True
    assert p._load_dev_fee_owed(w) == 3000
    assert p._load_dev_fee_inflight(w) is not None


# --- the success and clean-failure paths still work -----------------------
def test_a_settled_payout_clears_the_record_and_draws_down(monkeypatch) -> None:
    """The ordinary success path, re-asserted here because the in-flight record
    is now written BEFORE every payment: a settled payout has to clear it, or the
    first successful fee would hold every later one."""
    ln = _LnWorker(pay_result=(True, []), state="paid")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)
    _run_payout(p, w)
    assert ln.paid_amounts == [3000]
    assert p._load_dev_fee_owed(w) == 0                 # drawn down
    assert p._dev_fee_paid_last_24h(w) == 3000          # stamped on the cap
    assert p._load_dev_fee_inflight(w) is None          # and not holding
    # A payout action is still logged for the Actions view.
    assert any(e.get("kind") == "dev_fee"
               for e in p.get_decision_log(w, "action"))


def test_a_clean_failure_leaves_no_hold_so_the_fee_is_retried(monkeypatch) -> None:
    """Provably nothing committed: the record is cleared and the fee stays owed,
    so the next tick (after the backoff) tries again. That retry is the behaviour
    the in-flight check exists to PRESERVE -- the fix must not make every failure
    permanent."""
    ln = _LnWorker(pay_result=(False, []), state="failed")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)
    _run_payout(p, w)
    assert p._load_dev_fee_owed(w) == 3000              # still owed
    assert p._dev_fee_paid_last_24h(w) == 0
    assert p._load_dev_fee_inflight(w) is None          # free to retry


def test_a_timeout_holds_like_a_live_htlc(monkeypatch) -> None:
    """Past our own backstop the payment is still running, so it is held, not
    retried. The payout path had no timeout at all before this."""
    class _HangingLn(_LnWorker):
        async def pay_invoice(self, invoice, **kw):
            self.paid_amounts.append(invoice.get_amount_sat())
            await asyncio.sleep(10)
            return (False, [])

    ln = _HangingLn(state="inflight")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)
    monkeypatch.setattr(
        "electrum.plugins.inbound_liquidity.DEV_FEE_PAYMENT_TIMEOUT_SEC", 0.05)
    _run_payout(p, w)
    assert p._load_dev_fee_owed(w) == 3000              # untouched
    assert p._load_dev_fee_inflight(w) is not None      # and held


# --- untrackable payments are not made at all -----------------------------
def test_an_invoice_with_no_payment_hash_is_not_paid(monkeypatch) -> None:
    """An attempt we could not identify afterwards is one we could never avoid
    repeating, so it is refused rather than made."""
    ln = _LnWorker(pay_result=(True, []), state="paid")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)

    import electrum.invoices as inv_mod

    class _NoHashInvoice:
        def __init__(self, sat): self._sat = sat
        def get_amount_sat(self): return self._sat
        rhash = ""                      # nothing to track it by

    monkeypatch.setattr(inv_mod.Invoice, "from_bech32",
                        staticmethod(lambda b: _NoHashInvoice(3000)))
    _run_payout(p, w)
    assert ln.paid_amounts == [], "an untrackable invoice was paid"
    assert p._load_dev_fee_owed(w) == 3000
    assert p._load_dev_fee_inflight(w) is None


def test_a_payout_is_not_made_if_the_attempt_cannot_be_recorded(monkeypatch) -> None:
    """Same rule, one step earlier: if the in-flight record cannot be persisted,
    the payment is not made. Paying without a record it could be held to is
    precisely the unguarded case."""
    ln = _LnWorker(pay_result=(True, []), state="paid")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)

    def _boom(key, value):
        if key == DEV_FEE_INFLIGHT_DB_KEY:
            raise RuntimeError("disk full")
        return None
    monkeypatch.setattr(w.db, "put", _boom)

    _run_payout(p, w)
    assert ln.paid_amounts == [], "paid without being able to record the attempt"


# --- a corrupt record must not wedge payouts shut -------------------------
@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {},                                              # no payment hash
    {"payment_hash": "short", "amount_sat": 1},      # not 32 bytes of hex
    {"payment_hash": "zz" * 32, "amount_sat": 1},    # not hex at all
])
def test_an_unusable_record_reads_as_no_record(bad) -> None:
    """A hand-edited or older-build value must not hold payouts forever: there
    is nothing to re-check, so there is nothing to wait for."""
    p, w = _plugin(), _FakeWallet(_LnWorker())
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, bad)
    assert p._load_dev_fee_inflight(w) is None
    assert p._resolve_dev_fee_inflight(w) is False


def test_a_wallet_with_no_lnworker_keeps_holding() -> None:
    """Without an lnworker the payment's fate cannot be read at all, so the
    conservative answer is to keep holding rather than to free the retry."""
    p, w = _plugin(), _FakeWallet(None)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time(),
    })
    assert p._resolve_dev_fee_inflight(w) is True


# --- the guard is load-bearing -------------------------------------------
def test_without_the_hold_the_same_fee_is_paid_twice(monkeypatch) -> None:
    """The regression itself, demonstrated.

    This drives two payout cycles against a wallet whose payment reports failure
    while its HTLC stays live -- the exact condition the old code read as "safe to
    retry". With the hold defeated (the record cleared between cycles, which is
    what the old code effectively did by never writing one), the fee is paid a
    SECOND time, for the same 3000 sat that was never drawn down.

    Kept as a test rather than a comment so the guard cannot be quietly removed:
    delete the hold and this passes while the tests above fail.
    """
    ln = _LnWorker(pay_result=(False, []), state="inflight")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)

    _run_payout(p, w)
    assert ln.paid_amounts == [3000]
    # Defeat the hold, as the pre-fix code did by never recording the attempt.
    p._save_dev_fee_inflight(w, None)
    p._dev_fee_retry_until.pop(w, None)             # and skip the backoff
    _run_payout(p, w)

    assert ln.paid_amounts == [3000, 3000], (
        "two payments for one fee -- this is the bug the hold prevents")
    assert p._load_dev_fee_owed(w) == 3000, (
        "and the ledger still says it is owed, so a third tick would pay again")
    # Meanwhile the daily cap never saw either payment, which is why it could not
    # bound the leak: it is only stamped when a payout is believed to have settled.
    assert p._dev_fee_paid_last_24h(w) == 0


def test_with_the_hold_a_second_cycle_pays_nothing(monkeypatch) -> None:
    """The same two cycles, with the hold left in place: the second one makes no
    payment at all. Read against the test above, this is the fix."""
    ln = _LnWorker(pay_result=(False, []), state="inflight")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    _patch_lnurl(monkeypatch)

    _run_payout(p, w)
    assert ln.paid_amounts == [3000]
    p._dev_fee_retry_until.pop(w, None)             # even with no backoff...
    p._maybe_pay_dev_fee(w)                         # ... the gate declines
    assert ln.paid_amounts == [3000], "a second payment was made despite the hold"
    assert p._load_dev_fee_owed(w) == 3000


def test_a_failure_to_clear_the_record_does_not_escape_mid_settlement() -> None:
    """Recording an attempt and clearing one fail differently, on purpose.

    Recording raises (the caller then declines to pay -- an unrecordable payment
    is the one that cannot be prevented from repeating). Clearing must NOT raise,
    because it always follows the ledger being drawn down, and an exception there
    would escape mid-settlement. A stale record is harmless by comparison: the
    next resolve finds the payment PR_PAID, and the drawdown is floored at zero.
    """
    ln = _LnWorker(state="paid")
    p, w = _plugin(), _FakeWallet(ln)
    _owed(p, w, 3000)
    w.db.put(DEV_FEE_INFLIGHT_DB_KEY, {
        "payment_hash": _hash_for(3000).hex(),
        "amount_sat": 3000,
        "started_ts": time.time(),
    })
    ln.paid_amounts = [3000]

    real_put = w.db.put

    def _fail_to_clear(key, value):
        if key == DEV_FEE_INFLIGHT_DB_KEY and value is None:
            raise RuntimeError("disk full")
        return real_put(key, value)
    w.db.put = _fail_to_clear

    # Must not raise, and must still have accounted for the settled payment.
    p._resolve_dev_fee_inflight(w)
    assert p._load_dev_fee_owed(w) == 0
    assert p._dev_fee_paid_last_24h(w) == 3000

    # Re-resolving the record that could not be cleared cannot draw down twice.
    w.db.put = real_put
    p._resolve_dev_fee_inflight(w)
    assert p._load_dev_fee_owed(w) == 0
    assert ln.paid_amounts == [3000], "no second payment was made"
