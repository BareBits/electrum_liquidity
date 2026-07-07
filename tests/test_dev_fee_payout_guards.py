"""Guard/early-return coverage for the dev-fee payout that test_dev_fee_glue
skips: the synchronous ``_maybe_pay_dev_fee`` gate (already-paying, backoff,
nothing-due, offline, and the scheduling hand-off) and the ``_do_pay_dev_fee``
executor's defensive exits (no lnworker, unresolvable address, non-LNURL-pay
endpoint, endpoint max-sendable clamp, missing invoice) plus the LNURL branch of
address resolution. Reuses the fakes from test_dev_fee_glue. Skipped outside the
Electrum venv.
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEV_FEE_OWED_DB_KEY,
)
from test_dev_fee_glue import _FakeWallet, _plugin, _patch_lnurl, ADDR  # type: ignore  # noqa: E402


def _seed_owed(p, w, sat: int) -> None:
    """Put a known amount on the owed ledger directly (bypassing accrual)."""
    w.db.put(DEV_FEE_OWED_DB_KEY, sat)


# --- _maybe_pay_dev_fee gate ---------------------------------------------
def test_maybe_pay_noop_when_already_paying() -> None:
    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 100_000)
    p._dev_fee_paying[w] = True            # a payout is already in flight
    scheduled = []
    p._do_pay_dev_fee = lambda *a, **k: scheduled.append(a)  # type: ignore[assignment]
    p._maybe_pay_dev_fee(w)
    assert scheduled == []


def test_maybe_pay_noop_during_backoff() -> None:
    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 100_000)
    p._dev_fee_retry_until[w] = time.monotonic() + 100   # recent failure backoff
    scheduled = []
    p._do_pay_dev_fee = lambda *a, **k: scheduled.append(a)  # type: ignore[assignment]
    p._maybe_pay_dev_fee(w)
    assert scheduled == []


def test_maybe_pay_noop_when_nothing_due() -> None:
    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 0)                    # nothing accrued -> should_pay False
    scheduled = []
    p._do_pay_dev_fee = lambda *a, **k: scheduled.append(a)  # type: ignore[assignment]
    p._maybe_pay_dev_fee(w)
    assert scheduled == []


def test_maybe_pay_noop_when_offline_no_loop() -> None:
    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 100_000)
    # _FakeWallet.network has no asyncio_loop -> the payout can't be scheduled.
    assert getattr(w.network, "asyncio_loop", None) is None
    scheduled = []
    p._do_pay_dev_fee = lambda *a, **k: scheduled.append(a)  # type: ignore[assignment]
    p._maybe_pay_dev_fee(w)
    assert scheduled == []
    assert p._dev_fee_paying.get(w) in (None, False)   # flag not left set


def test_maybe_pay_schedules_payout_when_due() -> None:
    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 100_000)

    ran = []

    async def fake_do(wallet, address, amount_sat):
        ran.append((address, amount_sat))

    async def go():
        w.network.asyncio_loop = asyncio.get_running_loop()
        p._do_pay_dev_fee = fake_do        # type: ignore[assignment]
        p._maybe_pay_dev_fee(w)            # schedules fake_do as a sibling task
        # in-flight flag is set synchronously, before the task runs
        assert p._dev_fee_paying.get(w) is True
        await asyncio.sleep(0)             # let the scheduled task execute
        return ran

    out = asyncio.run(go())
    assert len(out) == 1
    address, amount = out[0]
    assert address == ADDR and amount > 0


# --- _do_pay_dev_fee defensive exits -------------------------------------
def test_pay_skips_when_no_lnworker() -> None:
    p, w = _plugin(), _FakeWallet(None)    # wallet has no lnworker
    _seed_owed(p, w, 3000)
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert p._load_dev_fee_owed(w) == 3000            # ledger untouched
    assert p._dev_fee_paying.get(w) in (None, False)


def test_pay_skips_on_unresolvable_address() -> None:
    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 3000)
    asyncio.run(p._do_pay_dev_fee(w, "not-a-lightning-address", 3000))
    assert p._load_dev_fee_owed(w) == 3000            # nothing sent


def test_pay_skips_when_endpoint_not_lnurl_pay(monkeypatch) -> None:
    import electrum.lnurl as lnurl_mod
    from types import SimpleNamespace

    async def fake_request_lnurl(url):
        return SimpleNamespace()                       # not an LNURL6Data
    monkeypatch.setattr(lnurl_mod, "request_lnurl", fake_request_lnurl)

    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 3000)
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert p._load_dev_fee_owed(w) == 3000


def test_pay_skips_when_callback_returns_no_invoice(monkeypatch) -> None:
    import electrum.lnurl as lnurl_mod

    async def fake_request_lnurl(url):
        return lnurl_mod.LNURL6Data(
            callback_url="https://example.com/cb",
            max_sendable_sat=100_000, min_sendable_sat=1,
            metadata_plaintext="[]", comment_allowed=0)

    async def fake_callback_lnurl(url, params):
        return {}                                      # no "pr" invoice field
    monkeypatch.setattr(lnurl_mod, "request_lnurl", fake_request_lnurl)
    monkeypatch.setattr(lnurl_mod, "callback_lnurl", fake_callback_lnurl)

    p, w = _plugin(), _FakeWallet(object())
    _seed_owed(p, w, 3000)
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert p._load_dev_fee_owed(w) == 3000


def test_pay_clamps_to_endpoint_max_sendable(monkeypatch) -> None:
    # Owe more than the endpoint accepts: send exactly its max this round and
    # carry the remainder (only the sent amount is drawn down).
    paid = {}
    from types import SimpleNamespace

    async def fake_pay_invoice(invoice, **kw):
        paid["sat"] = invoice.get_amount_sat()
        return (True, [])

    ln = SimpleNamespace(pay_invoice=fake_pay_invoice)
    p, w = _plugin(), _FakeWallet(ln)
    _seed_owed(p, w, 3000)
    _patch_lnurl(monkeypatch, max_sat=2000)            # endpoint caps at 2000
    asyncio.run(p._do_pay_dev_fee(w, ADDR, 3000))
    assert paid["sat"] == 2000                         # clamped down to the max
    assert p._load_dev_fee_owed(w) == 1000             # 3000 - 2000 carried


# --- address resolution: LNURL branch ------------------------------------
def test_resolve_lnurl_bech32_invalid_is_none() -> None:
    # A bech32 lnurl1… string routes through decode_lnurl; a malformed one must
    # degrade to None, not raise.
    p = _plugin()
    assert p._resolve_lnurl_pay_url("lnurl1invalidbech32") is None
