"""Glue-level tests for the optional dev fee: per-wallet accrual on confirmed
swap completion (immediate + reconciliation paths), the owed ledger and rolling
payout-history persistence, and the LNURL-pay payout executor (success draws the
ledger down and stamps the daily cap; failure leaves the ledger intact and backs
off). Heavy Electrum objects are faked/monkeypatched; skipped outside the venv.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Dict

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    DEV_FEE_OWED_DB_KEY,
    DEV_FEE_PAYMENTS_DB_KEY,
    DEV_FEE_RETRY_BACKOFF_SEC,
    PENDING_SWAPS_DB_KEY,
)
from liquidity_manager import DAILY_WINDOW_SEC  # type: ignore  # noqa: E402

ADDR = "electrum_liqhelper@example.com"


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, lnworker=None) -> None:
        self.db = _FakeDB()
        self.saved = 0
        self.lnworker = lnworker
        self.network = SimpleNamespace(is_connected=lambda: True)

    def save_db(self) -> None:
        self.saved += 1

    def basename(self) -> str:
        return "fakewallet"


def _plugin(**config_overrides) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.devfee")
    p._last_decline_sigs = {}
    p._dev_fee_paying = {}
    p._dev_fee_retry_until = {}
    cfg = dict(
        INBOUND_LIQUIDITY_DEV_FEE_PCT=0.1,
        INBOUND_LIQUIDITY_DEV_FEE_ADDRESS=ADDR,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
    )
    cfg.update(config_overrides)
    p.config = SimpleNamespace(**cfg)
    return p


# --- accrual --------------------------------------------------------------
def test_accrue_adds_and_persists() -> None:
    p, w = _plugin(), _FakeWallet()
    added = p._accrue_dev_fee(w, 900_000, source="chan")
    assert added == 900                       # 0.1% of 900k
    assert p._load_dev_fee_owed(w) == 900
    assert w.db.get(DEV_FEE_OWED_DB_KEY) == 900   # persisted
    assert w.saved >= 1
    # A second completed swap accumulates.
    p._accrue_dev_fee(w, 100_000, source="chan2")
    assert p._load_dev_fee_owed(w) == 1000


def test_accrue_disabled_when_pct_zero() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_DEV_FEE_PCT=0.0), _FakeWallet()
    assert p._accrue_dev_fee(w, 5_000_000, source="chan") == 0
    assert p._load_dev_fee_owed(w) == 0


def test_accrue_clamps_out_of_range_pct() -> None:
    # 99% is clamped to the 5% ceiling before charging.
    p, w = _plugin(INBOUND_LIQUIDITY_DEV_FEE_PCT=99.0), _FakeWallet()
    assert p._accrue_dev_fee(w, 1_000_000, source="chan") == 50_000   # 5%, not 99%


def test_dev_fee_status_reports_owed_and_headroom() -> None:
    p, w = _plugin(), _FakeWallet()
    p._accrue_dev_fee(w, 2_000_000, source="chan")   # 2000 owed
    p._record_dev_fee_payment(w, 3000)
    st = p.dev_fee_status(w)
    assert st["owed_sat"] == 2000
    assert st["paid_last_24h_sat"] == 3000
    assert st["daily_headroom_sat"] == 7000          # 10000 cap - 3000 paid


# --- rolling payout history ----------------------------------------------
def test_payment_history_rolling_window() -> None:
    p, w = _plugin(), _FakeWallet()
    now = time.time()
    # One stale (>24h) payout and one fresh, seeded directly.
    w.db.put(DEV_FEE_PAYMENTS_DB_KEY,
             [[now - DAILY_WINDOW_SEC - 100, 5000.0], [now - 60, 2000.0]])
    assert p._dev_fee_paid_last_24h(w) == 2000        # stale one ignored
    p._record_dev_fee_payment(w, 1000)                # record prunes the stale one
    stored = w.db.get(DEV_FEE_PAYMENTS_DB_KEY)
    assert len(stored) == 2                            # stale dropped, fresh + new
    assert p._dev_fee_paid_last_24h(w) == 3000


# --- reconciliation accrues on confirmed completion -----------------------
def test_reconcile_accrues_on_completion() -> None:
    ph = "ab" * 32
    swap = SimpleNamespace(is_redeemed=False, funding_txid="ff" * 32)
    sm = SimpleNamespace(get_swap=lambda h: swap)
    ln = SimpleNamespace(swap_manager=sm)
    p, w = _plugin(), _FakeWallet(ln)
    # A pending swap we were watching, carrying its expected on-chain basis.
    w.db.put(PENDING_SWAPS_DB_KEY, {
        ph: {"npub": "", "started_ts": time.time(), "node_id": "",
             "channel_id": "cd" * 32, "fee_basis_sat": 800_000}})
    p._reconcile_pending_swaps(w)
    assert p._load_dev_fee_owed(w) == 800            # 0.1% of 800k accrued once
    # The pending record is consumed, so a re-run does not double-charge.
    p._reconcile_pending_swaps(w)
    assert p._load_dev_fee_owed(w) == 800


# --- payout executor ------------------------------------------------------
class _FakeInvoice:
    def __init__(self, sat: int) -> None:
        self._sat = sat

    def get_amount_sat(self) -> int:
        return self._sat


def _patch_lnurl(monkeypatch, *, min_sat=1, max_sat=100_000, pay_result=(True, []),
                 invoice_sat=None):
    """Stub the LNURL-pay round-trip and Invoice decoding so the payout executor
    can run without a network. Returns a dict recording what was paid."""
    import electrum.lnurl as lnurl_mod
    import electrum.invoices as inv_mod
    calls: Dict[str, object] = {}

    async def fake_request_lnurl(url):
        calls["url"] = url
        return lnurl_mod.LNURL6Data(
            callback_url="https://example.com/cb",
            max_sendable_sat=max_sat, min_sendable_sat=min_sat,
            metadata_plaintext="[]", comment_allowed=0)

    async def fake_callback_lnurl(url, params):
        calls["amount_msat"] = params["amount"]
        return {"pr": "lnbc-fake"}

    def fake_from_bech32(bolt11):
        sat = invoice_sat if invoice_sat is not None else calls["amount_msat"] // 1000
        return _FakeInvoice(sat)

    monkeypatch.setattr(lnurl_mod, "request_lnurl", fake_request_lnurl)
    monkeypatch.setattr(lnurl_mod, "callback_lnurl", fake_callback_lnurl)
    monkeypatch.setattr(inv_mod.Invoice, "from_bech32", staticmethod(fake_from_bech32))
    return calls


def test_payout_success_draws_down_and_records(monkeypatch) -> None:
    paid = {}

    async def fake_pay_invoice(invoice, **kw):
        paid["sat"] = invoice.get_amount_sat()
        return (True, [])

    ln = SimpleNamespace(pay_invoice=fake_pay_invoice)
    p, w = _plugin(), _FakeWallet(ln)
    p._accrue_dev_fee(w, 3_000_000, source="chan")    # 3000 owed
    calls = _patch_lnurl(monkeypatch)
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert paid["sat"] == 3000
    assert calls["amount_msat"] == 3_000_000
    assert p._load_dev_fee_owed(w) == 0               # ledger drawn down
    assert p._dev_fee_paid_last_24h(w) == 3000        # stamped against the cap
    assert p._dev_fee_paying.get(w) is False          # in-flight flag cleared
    # A payout action was logged for the Actions view.
    assert any(e.get("kind") == "dev_fee" for e in p.get_decision_log(w, "action"))


def test_payout_failure_keeps_ledger_and_backs_off(monkeypatch) -> None:
    async def fake_pay_invoice(invoice, **kw):
        return (False, [])                            # payment did not settle

    ln = SimpleNamespace(pay_invoice=fake_pay_invoice)
    p, w = _plugin(), _FakeWallet(ln)
    p._accrue_dev_fee(w, 3_000_000, source="chan")    # 3000 owed
    _patch_lnurl(monkeypatch)
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert p._load_dev_fee_owed(w) == 3000            # ledger untouched
    assert p._dev_fee_paid_last_24h(w) == 0           # nothing stamped
    assert p._dev_fee_retry_until.get(w, 0.0) > time.monotonic()   # backoff armed
    assert p._dev_fee_retry_until[w] <= time.monotonic() + DEV_FEE_RETRY_BACKOFF_SEC + 1


def test_payout_refuses_wrong_amount_invoice(monkeypatch) -> None:
    async def fake_pay_invoice(invoice, **kw):
        raise AssertionError("should not pay a mismatched invoice")

    ln = SimpleNamespace(pay_invoice=fake_pay_invoice)
    p, w = _plugin(), _FakeWallet(ln)
    p._accrue_dev_fee(w, 3_000_000, source="chan")
    _patch_lnurl(monkeypatch, invoice_sat=2999)       # endpoint tried to short us
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert p._load_dev_fee_owed(w) == 3000            # not paid; still owed
    assert p._dev_fee_retry_until.get(w, 0.0) > time.monotonic()


def test_payout_deferred_below_endpoint_minimum(monkeypatch) -> None:
    async def fake_pay_invoice(invoice, **kw):
        raise AssertionError("should not pay below the endpoint minimum")

    ln = SimpleNamespace(pay_invoice=fake_pay_invoice)
    p, w = _plugin(), _FakeWallet(ln)
    p._accrue_dev_fee(w, 3_000_000, source="chan")
    _patch_lnurl(monkeypatch, min_sat=5000)           # endpoint won't accept < 5000
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert p._load_dev_fee_owed(w) == 3000            # still owed, waits to accrue more


# --- address resolution ---------------------------------------------------
def test_resolve_lnurl_pay_url_forms() -> None:
    p = _plugin()
    assert p._resolve_lnurl_pay_url("alice@example.com") == \
        "https://example.com/.well-known/lnurlp/alice"
    assert p._resolve_lnurl_pay_url("https://example.com/x") == "https://example.com/x"
    assert p._resolve_lnurl_pay_url("") is None
    assert p._resolve_lnurl_pay_url("not-an-address") is None
