"""Glue-level tests for draining a channel into the liquidity sink.

The engine plans a ladder of amounts (largest first, halving, floored at
SINK_MIN_PAYMENT_SAT); this is the executor that walks it: resolve the address
once, mint one invoice per rung, pay it pinned to the channel being drained, and
halve on failure. If every rung fails, delegate to the reverse swap the engine
held in reserve.

The safety-critical case here is the mirror of the swap path's commit boundary.
A failed ``pay_invoice`` does NOT prove nothing was sent -- ``pay_to_node``
raises PaymentFailure once it passes Electrum's 120s PAYMENT_TIMEOUT while HTLCs
may still be unresolved -- so halving is only permitted after checking that no
payment with that hash is still in flight. If one is, the ladder stops AND the
swap fallback is skipped: draining a channel twice is worse than not draining it.

Covered here:
  * the happy path (first rung settles: dev fee accrued, action logged, cooldown);
  * halving past failures until a rung settles, and the ladder exhausting;
  * the in-flight abort -- no halving, no swap fallback;
  * endpoint-level failures (unresolvable address, not LNURL-pay, no invoice,
    wrong invoice amount) going STRAIGHT to the swap fallback, since no smaller
    amount would fare better;
  * endpoint min/max sendable reshaping the ladder, including dedup;
  * the payment pinned to the right channel and bounded by max_swap_fee_pct;
  * the ladder's wall-clock deadline, and the cooldown NOT blocking the fallback.

Heavy Electrum objects are faked; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    SWAP_COOLDOWN_SEC,
    LiquidityPlugin,
    _SinkAttempt,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    LiquiditySinkAction,
    ReverseSwapAction,
)

SINK = "alice@example.com"
CHAN_ID = "aa" * 32


# --- fakes ----------------------------------------------------------------
class _Invoice:
    def __init__(self, sat: int, rhash: str) -> None:
        self._sat = sat
        self.rhash = rhash

    def get_amount_sat(self) -> int:
        return self._sat


class _LNWorker:
    """Records pay_invoice calls and drives a scripted per-amount outcome."""

    def __init__(self, outcomes, *, inflight=()) -> None:
        # outcomes: amount_sat -> True/False, or an exception instance to raise.
        self.outcomes = outcomes
        self.paid: List[tuple] = []          # (amount_sat, channels, fee_msat)
        self._inflight = set(inflight)       # payment hashes still unresolved
        self.channels: Dict = {}

    def get_channel_by_id(self, cid):
        return SimpleNamespace(short_channel_id="1x1x1", _cid=cid)

    def get_payments(self, *, status=None):
        assert status == 'inflight'
        return {h: [] for h in self._inflight}

    async def pay_invoice(self, invoice, **kw):
        chans = kw.get("channels")
        budget = kw.get("budget")
        self.paid.append((invoice.get_amount_sat(), chans,
                          getattr(budget, "fee_msat", None)))
        outcome = self.outcomes.get(invoice.get_amount_sat(), False)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(self, invoice)
        return outcome, []


def _plugin(*, max_fee_pct: float = 0.9, **config_over) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.sink")
    p._swap_cooldown_until = {}
    p._sink_payment_timeout_sec = 5.0
    cfg = dict(INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=max_fee_pct)
    cfg.update(config_over)
    p.config = SimpleNamespace(**cfg)
    p.dev_fees = []      # (amount_sat, source)
    p.diags = []         # _diag_event kwargs
    p.logged = []        # _log_action kwargs
    p.statuses = []      # _set_status messages
    p.swapped = []       # ReverseSwapAction handed to the fallback
    p._accrue_dev_fee = lambda wallet, amount_sat, source=None: \
        p.dev_fees.append((amount_sat, source))
    p._diag_event = lambda wallet, **kw: p.diags.append(kw)
    p._log_action = lambda wallet, **kw: p.logged.append(kw)
    p._set_status = lambda wallet, msg: p.statuses.append(msg)
    p.on_action_done = lambda wallet, msg: None

    async def _fake_reverse_swap(wallet, action, state=None, transport=None):
        p.swapped.append(action)
    p._reverse_swap = _fake_reverse_swap
    return p


def _lnurl(p, *, min_sat=1, max_sat=10_000_000, invoice_sat=None,
           params_exc=None, invoice_exc=None, no_invoice=False):
    """Stub the two LNURL steps the executor uses, recording what was asked for."""
    asked: List[int] = []

    async def _params(address, *, purpose):
        if params_exc is not None:
            raise params_exc
        if min_sat is None:
            return None            # not an LNURL-pay endpoint
        return SimpleNamespace(min_sendable_sat=min_sat, max_sendable_sat=max_sat,
                               callback_url="https://example.com/cb")

    async def _invoice(lnurl_data, pay_sat, *, purpose):
        asked.append(pay_sat)
        if invoice_exc is not None:
            raise invoice_exc
        if no_invoice:
            return None
        sat = invoice_sat if invoice_sat is not None else pay_sat
        return _Invoice(sat, f"{pay_sat:064x}")

    p._lnurl_pay_params = _params
    p._lnurl_invoice = _invoice
    return asked


def _hash_for(amount_sat: int) -> bytes:
    return bytes.fromhex(f"{amount_sat:064x}")


def _action(amounts=(400_000, 200_000, 100_000), *, fallback=True
            ) -> LiquiditySinkAction:
    swap = ReverseSwapAction(
        channel_id=CHAN_ID, short_id="1x1x1", lightning_amount_sat=400_000,
        reason="swap", provider_npub="npubA") if fallback else None
    return LiquiditySinkAction(
        channel_id=CHAN_ID, short_id="1x1x1", address=SINK,
        amounts=tuple(amounts), reason="drain", swap_fallback=swap)


def _run(p, lnworker, action) -> None:
    wallet = SimpleNamespace(lnworker=lnworker)
    asyncio.run(p._liquidity_sink_drain(wallet, action, state={}))


# --- the happy path -------------------------------------------------------
def test_first_rung_settles_and_is_recorded() -> None:
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({400_000: True})
    _run(p, ln, _action())

    assert asked == [400_000]                       # no halving needed
    assert [amt for amt, _c, _f in ln.paid] == [400_000]
    # Dev fee accrues on what was drained -- the sink's equivalent of a swap's
    # net on-chain receipt.
    assert p.dev_fees == [(400_000, "1x1x1")]
    [entry] = p.logged
    assert entry["kind"] == "sink"
    assert entry["amount_sat"] == 400_000
    assert entry["source"] == "1x1x1" and entry["dest"] == SINK
    assert not p.swapped                            # the fallback never ran
    assert p._swap_cooldown_until[CHAN_ID] > time.monotonic()


def test_payment_is_pinned_to_the_channel_being_drained() -> None:
    # Without this, lnworker routes over ANY channel with enough outbound and a
    # drain planned for channel X quietly empties channel Y instead.
    p = _plugin()
    _lnurl(p)
    ln = _LNWorker({400_000: True})
    _run(p, ln, _action())
    _amt, chans, _fee = ln.paid[0]
    assert chans is not None and len(chans) == 1
    assert chans[0]._cid == bytes.fromhex(CHAN_ID)


def test_routing_budget_comes_from_the_max_swap_fee_ceiling() -> None:
    p = _plugin(max_fee_pct=0.5)
    _lnurl(p)
    ln = _LNWorker({400_000: True})
    _run(p, ln, _action())
    _amt, _chans, fee_msat = ln.paid[0]
    assert fee_msat == 400_000 * 1000 * 0.5 / 100      # 0.5% of the amount


# --- halving --------------------------------------------------------------
def test_ladder_halves_until_a_rung_settles() -> None:
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({400_000: False, 200_000: False, 100_000: True})
    _run(p, ln, _action())

    assert asked == [400_000, 200_000, 100_000]
    assert p.dev_fees == [(100_000, "1x1x1")]
    assert p.logged[0]["amount_sat"] == 100_000
    assert "attempt 3 of 3" in p.logged[0]["detail"]
    assert not p.swapped


def test_exhausted_ladder_falls_back_to_the_reserved_swap() -> None:
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({})                      # every amount fails
    _run(p, ln, _action())

    assert asked == [400_000, 200_000, 100_000]
    assert not p.logged                     # nothing was drained by the sink
    assert not p.dev_fees
    [swap] = p.swapped
    assert isinstance(swap, ReverseSwapAction)
    assert swap.lightning_amount_sat == 400_000


def test_exhausted_ladder_without_a_fallback_just_stops() -> None:
    p = _plugin()
    _lnurl(p)
    ln = _LNWorker({})
    _run(p, ln, _action(fallback=False))

    assert not p.swapped
    assert any("no swap fallback" in d["reason"] for d in p.diags)
    assert p._swap_cooldown_until[CHAN_ID] > time.monotonic()


def test_the_cooldown_does_not_block_the_swap_fallback() -> None:
    # Regression guard: _reverse_swap returns early if the channel's cooldown is
    # already armed, so arming it before delegating would silently cancel the
    # fallback and make "swaps as a backstop" a lie.
    p = _plugin()
    _lnurl(p)
    ln = _LNWorker({})
    _run(p, ln, _action())
    assert len(p.swapped) == 1


def test_a_cooled_down_channel_is_not_touched_at_all() -> None:
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({400_000: True})
    p._swap_cooldown_until[CHAN_ID] = time.monotonic() + SWAP_COOLDOWN_SEC
    _run(p, ln, _action())
    assert asked == [] and ln.paid == [] and not p.swapped


# --- the safety gate: a failure that may still be in flight ---------------
def test_failure_with_an_htlc_still_in_flight_aborts_everything() -> None:
    # The whole point: halving here could drain the channel twice if the first
    # payment later settles. Neither the next rung NOR the swap fallback may run.
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({}, inflight={_hash_for(400_000)})
    _run(p, ln, _action())

    assert asked == [400_000]               # stopped dead, no halving
    assert not p.swapped                    # and no swap either
    assert not p.logged and not p.dev_fees
    assert any("HTLC possibly in flight" in d["reason"] for d in p.diags)
    assert p._swap_cooldown_until[CHAN_ID] > time.monotonic()


def test_a_timeout_with_nothing_in_flight_is_safe_to_halve() -> None:
    async def _hang(_ln, _invoice):
        await asyncio.sleep(30)
        return True, []
    p = _plugin()
    p._sink_payment_timeout_sec = 0.05
    asked = _lnurl(p)
    ln = _LNWorker({400_000: _hang, 200_000: True})
    _run(p, ln, _action())

    assert asked == [400_000, 200_000]
    assert p.logged[0]["amount_sat"] == 200_000
    assert any("timed out" in d["reason"] for d in p.diags)


def test_a_timeout_with_an_htlc_in_flight_aborts() -> None:
    async def _hang(_ln, _invoice):
        await asyncio.sleep(30)
        return True, []
    p = _plugin()
    p._sink_payment_timeout_sec = 0.05
    asked = _lnurl(p)
    ln = _LNWorker({400_000: _hang}, inflight={_hash_for(400_000)})
    _run(p, ln, _action())

    assert asked == [400_000]
    assert not p.swapped


def test_an_unknown_exception_does_not_halve_blindly() -> None:
    # We cannot show it left nothing committed, so we stop the ladder. With
    # nothing in flight the swap fallback is still allowed.
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({400_000: RuntimeError("boom")})
    _run(p, ln, _action())

    assert asked == [400_000]
    assert len(p.swapped) == 1


def test_an_unknown_exception_with_an_htlc_in_flight_aborts() -> None:
    p = _plugin()
    ln = _LNWorker({400_000: RuntimeError("boom")}, inflight={_hash_for(400_000)})
    _lnurl(p)
    _run(p, ln, _action())
    assert not p.swapped


def test_an_unreadable_inflight_map_is_treated_as_in_flight() -> None:
    # Best-effort by design: a false "all clear" costs a double drain, a false
    # alarm costs one skipped tick.
    class _Broken(_LNWorker):
        def get_payments(self, *, status=None):
            raise RuntimeError("db locked")
    p = _plugin()
    _lnurl(p)
    ln = _Broken({})
    _run(p, ln, _action())
    assert not p.swapped


# --- endpoint-level failures skip the ladder entirely ---------------------
def test_an_unresolvable_address_goes_straight_to_the_swap() -> None:
    p = _plugin()
    asked = _lnurl(p, params_exc=OSError("dns"))
    ln = _LNWorker({})
    _run(p, ln, _action())

    assert asked == []                      # no payment was ever attempted
    assert ln.paid == []
    assert len(p.swapped) == 1
    assert any("could not be resolved" in d["reason"] for d in p.diags)


def test_a_non_lnurl_pay_endpoint_goes_straight_to_the_swap() -> None:
    p = _plugin()
    _lnurl(p, min_sat=None)
    ln = _LNWorker({})
    _run(p, ln, _action())
    assert ln.paid == [] and len(p.swapped) == 1


def test_no_invoice_from_the_endpoint_stops_the_ladder() -> None:
    # An endpoint that will not mint an invoice at 400k will not mint one at
    # 200k either; halving is pointless.
    p = _plugin()
    asked = _lnurl(p, no_invoice=True)
    ln = _LNWorker({})
    _run(p, ln, _action())

    assert asked == [400_000]
    assert ln.paid == []
    assert len(p.swapped) == 1


def test_a_wrong_amount_invoice_is_refused(monkeypatch) -> None:
    # The REAL _lnurl_invoice, because this is the security-relevant check: the
    # endpoint chooses the invoice, so one for more than we asked must never be
    # handed to pay_invoice.
    import electrum.lnurl as lnurl_mod
    import electrum.invoices as inv_mod

    async def _callback(url, params):
        return {"pr": "lnbc-whatever"}

    monkeypatch.setattr(lnurl_mod, "callback_lnurl", _callback)
    monkeypatch.setattr(inv_mod.Invoice, "from_bech32",
                        staticmethod(lambda b: _Invoice(400_001, "ab" * 32)))
    p = _plugin()
    bounds = SimpleNamespace(min_sendable_sat=1, max_sendable_sat=10 ** 9,
                             callback_url="https://example.com/cb")
    got = asyncio.run(p._lnurl_invoice(bounds, 400_000, purpose="liquidity sink"))
    assert got is None

    # ...and the exact amount is accepted.
    monkeypatch.setattr(inv_mod.Invoice, "from_bech32",
                        staticmethod(lambda b: _Invoice(400_000, "ab" * 32)))
    got = asyncio.run(p._lnurl_invoice(bounds, 400_000, purpose="liquidity sink"))
    assert got is not None and got.get_amount_sat() == 400_000


def test_no_usable_invoice_means_nothing_is_paid() -> None:
    p = _plugin()
    _lnurl(p, no_invoice=True)
    ln = _LNWorker({})
    _run(p, ln, _action())
    assert ln.paid == []


def test_an_invoice_request_that_raises_stops_the_ladder() -> None:
    p = _plugin()
    asked = _lnurl(p, invoice_exc=OSError("http 500"))
    ln = _LNWorker({})
    _run(p, ln, _action())
    assert asked == [400_000] and len(p.swapped) == 1


def test_a_wallet_without_lnworker_falls_back() -> None:
    p = _plugin()
    _lnurl(p)
    wallet = SimpleNamespace(lnworker=None)
    asyncio.run(p._liquidity_sink_drain(wallet, _action(), state={}))
    assert len(p.swapped) == 1


# --- endpoint min/max sendable reshapes the ladder ------------------------
def test_rungs_above_the_endpoint_maximum_are_clamped_down() -> None:
    p = _plugin()
    asked = _lnurl(p, max_sat=250_000)
    ln = _LNWorker({250_000: False, 200_000: False, 100_000: True})
    _run(p, ln, _action())
    # 400k clamped to 250k; 200k and 100k already fit.
    assert asked == [250_000, 200_000, 100_000]


def test_clamping_does_not_produce_a_duplicate_attempt() -> None:
    # 400k and 200k both clamp to 200k -- paying the identical amount twice
    # tells us nothing new, so the duplicate is dropped.
    p = _plugin()
    asked = _lnurl(p, max_sat=200_000)
    ln = _LNWorker({200_000: False, 100_000: True})
    _run(p, ln, _action())
    assert asked == [200_000, 100_000]


def test_rungs_below_the_endpoint_minimum_are_dropped() -> None:
    # The endpoint's floor can sit above the plugin's own 10k floor.
    p = _plugin()
    asked = _lnurl(p, min_sat=150_000)
    ln = _LNWorker({400_000: False, 200_000: True})
    _run(p, ln, _action())
    assert asked == [400_000, 200_000]      # 100_000 is under the endpoint min


def test_an_endpoint_that_accepts_nothing_falls_back_to_the_swap() -> None:
    p = _plugin()
    asked = _lnurl(p, min_sat=5_000_000)
    ln = _LNWorker({})
    _run(p, ln, _action())
    assert asked == [] and len(p.swapped) == 1
    assert any("accepts no amount" in d["reason"] for d in p.diags)


def test_rungs_within_bounds_is_pure_and_order_preserving() -> None:
    p = _plugin()
    bounds = SimpleNamespace(min_sendable_sat=50_000, max_sendable_sat=300_000)
    assert p._sink_rungs_within_bounds(
        (400_000, 200_000, 100_000, 50_000, 25_000), bounds) == \
        [300_000, 200_000, 100_000, 50_000]


# --- the ladder's wall-clock budget ---------------------------------------
def test_the_ladder_stops_at_its_deadline_and_still_falls_back() -> None:
    from electrum.plugins import inbound_liquidity as mod

    async def _slow(_ln, _invoice):
        await asyncio.sleep(0.05)
        return False, []
    p = _plugin()
    asked = _lnurl(p)
    ln = _LNWorker({400_000: _slow, 200_000: _slow, 100_000: _slow})
    original = mod.SINK_CASCADE_DEADLINE_SEC
    try:
        mod.SINK_CASCADE_DEADLINE_SEC = 0.06
        _run(p, ln, _action())
    finally:
        mod.SINK_CASCADE_DEADLINE_SEC = original

    assert len(asked) < 3                   # ran out of time mid-ladder
    assert any("deadline" in d["reason"] for d in p.diags)
    assert len(p.swapped) == 1              # the fallback still gets its turn


# --- config plumbing ------------------------------------------------------
def _full_config(**over) -> SimpleNamespace:
    base = dict(
        INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
        INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=1_000_000,
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=10_000,
        INBOUND_LIQUIDITY_MAX_CHANNELS=2,
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.6,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT=25_000,
        INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT=0,
        INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY=False,
        INBOUND_LIQUIDITY_PREFERRED_NPUBS="",
        INBOUND_LIQUIDITY_BANNED_NPUBS="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_read_config_carries_the_sink_settings() -> None:
    p = _plugin()
    p.config = _full_config(INBOUND_LIQUIDITY_SINK_ADDRESS=f"  {SINK} ",
                            INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS=True)
    cfg = p.read_config()
    assert cfg.liquidity_sink_address == SINK      # trimmed on the way in
    assert cfg.disable_submarine_swaps is True


def test_read_config_defaults_the_sink_off_on_an_older_config() -> None:
    # A config predating these settings must read as "off", not raise on a tick.
    p = _plugin()
    p.config = _full_config()
    cfg = p.read_config()
    assert cfg.liquidity_sink_address == ""
    assert cfg.disable_submarine_swaps is False


def test_read_config_carries_the_defer_until_goal_switch() -> None:
    p = _plugin()
    p.config = _full_config(INBOUND_LIQUIDITY_SINK_ADDRESS=SINK,
                            INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL=False)
    assert p.read_config().defer_sink_until_goal is False


def test_defer_until_goal_defaults_ON_on_an_older_config() -> None:
    # Note this default is the opposite of the two above: a config predating the
    # switch must read as ON, which is the funds-preserving answer -- the sink
    # keeps sending outbound out of the wallet only once the goal it would
    # otherwise starve has been met. (The engine dataclass defaults the other
    # way, for the pure tests; read_config always passes this explicitly.)
    p = _plugin()
    p.config = _full_config(INBOUND_LIQUIDITY_SINK_ADDRESS=SINK)
    assert p.read_config().defer_sink_until_goal is True


# --- the outcome enum -----------------------------------------------------
def test_pay_liquidity_sink_reports_its_outcome() -> None:
    p = _plugin()
    _lnurl(p)
    wallet = SimpleNamespace(lnworker=_LNWorker({400_000: True}))
    assert asyncio.run(p._pay_liquidity_sink(wallet, _action(), None)) \
        is _SinkAttempt.PAID

    p2 = _plugin()
    _lnurl(p2, params_exc=OSError("dns"))
    wallet2 = SimpleNamespace(lnworker=_LNWorker({}))
    assert asyncio.run(p2._pay_liquidity_sink(wallet2, _action(), None)) \
        is _SinkAttempt.UNUSABLE

    p3 = _plugin()
    _lnurl(p3)
    wallet3 = SimpleNamespace(lnworker=_LNWorker({}, inflight={_hash_for(400_000)}))
    assert asyncio.run(p3._pay_liquidity_sink(wallet3, _action(), None)) \
        is _SinkAttempt.ABORT
