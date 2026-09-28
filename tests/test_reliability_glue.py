"""Glue-level tests for provider reliability tracking (the Electrum-facing layer).

Exercise the persisted reliability store (record fault/success, decay penalty,
clear), the folding of penalties onto live offers, the fault classification in
_reverse_swap (timeout / RPC error = fault; provider decline = NOT a fault;
delivered swap = success), and the stuck-swap reconciliation (funded => success,
never-funded-past-timeout => fault). Heavy Electrum objects are faked; skipped if
the plugin package cannot be imported (outside the electrum venv).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    RELIABILITY_DB_KEY,
    PENDING_SWAPS_DB_KEY,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
    ReverseSwapAction,
)


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, sm=None) -> None:
        self.db = _FakeDB()
        self.saved = 0
        self.lnworker = SimpleNamespace(swap_manager=sm) if sm is not None else None

    def save_db(self) -> None:
        self.saved += 1


def _plugin() -> LiquidityPlugin:
    import logging
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.reliability")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p._reverse_swap_timeout_sec = 30.0
    # Per-provider wait for advertised terms; shrunk so an unreachable
    # provider trips the init timeout instantly instead of in 15s.
    p._swap_init_timeout_sec = 0.01
    p.config = SimpleNamespace()  # getattr defaults kick in
    return p


NPUB = "npubFLAKY"


# --- store: record / decay / clear ----------------------------------------
def test_record_fault_accumulates_and_penalises() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "init timeout")
    p._record_provider_fault(w, NPUB, "RPC error")
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 2
    assert stats["fault_count"] == 2
    # Two consecutive faults -> base * 2^(2-1) = 0.5 * 2 = 1.0% (fresh, no decay).
    rows = p.provider_reliability_rows(w)
    assert rows[NPUB]["penalty_pct"] == pytest.approx(1.0, abs=1e-3)


def test_success_resets_consecutive_faults() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")
    p._record_provider_fault(w, NPUB, "y")
    p._record_provider_success(w, NPUB)
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 0
    assert stats["success_count"] == 1
    assert stats["fault_count"] == 2          # lifetime fault tally is kept
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == 0.0


def test_penalty_decays_with_age() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")    # 1 fault -> 0.5% fresh
    # Backdate the fault by one half-life (6h) -> penalty halves to 0.25%.
    data = p._load_reliability(w)
    data[NPUB]["last_fault_ts"] = time.time() - 6 * 3600
    p._save_reliability(w, data)
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(0.25, abs=1e-3)


def test_clear_one_and_all() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, "a", "x")
    p._record_provider_fault(w, "b", "y")
    p.clear_provider_reliability(w, "a")
    assert set(p._load_reliability(w)) == {"b"}
    p.clear_provider_reliability(w)            # clear all
    assert p._load_reliability(w) == {}


def test_empty_npub_is_ignored() -> None:
    # Single-provider / URL mode has no per-provider identity to track.
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, "", "x")
    p._record_provider_success(w, "")
    assert p._load_reliability(w) == {}


# --- penalty folding onto offers ------------------------------------------
def test_apply_reliability_penalties_folds_decayed_penalty() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")     # 0.5% penalty
    offers = [ProviderOffer(npub=NPUB, percentage_fee=0.3, mining_fee_sat=0,
                            min_amount_sat=1, max_reverse_sat=10**9),
              ProviderOffer(npub="clean", percentage_fee=0.4, mining_fee_sat=0,
                            min_amount_sat=1, max_reverse_sat=10**9)]
    out = {o.npub: o for o in p._apply_reliability_penalties(w, offers)}
    assert out[NPUB].reliability_penalty_pct == pytest.approx(0.5, abs=1e-3)
    assert out["clean"].reliability_penalty_pct == 0.0


def test_apply_reliability_penalties_disabled_is_noop() -> None:
    p, w = _plugin(), _FakeWallet()
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_RELIABILITY_ENABLED=False)
    p._record_provider_fault(w, NPUB, "x")
    offers = [ProviderOffer(npub=NPUB, percentage_fee=0.3, mining_fee_sat=0,
                            min_amount_sat=1, max_reverse_sat=10**9)]
    out = p._apply_reliability_penalties(w, offers)
    assert out[0].reliability_penalty_pct == 0.0


# --- _reverse_swap fault classification -----------------------------------
def _swap_manager(reverse_swap, *, initialized=True) -> SimpleNamespace:
    sm = SimpleNamespace(
        mining_fee=1_000,
        is_initialized=asyncio.Event(),
        update_pairs=lambda pairs: None,
        get_recv_amount=lambda amt, *, is_reverse: amt - 500,
        reverse_swap=reverse_swap,
        _swaps={},
    )
    if initialized:
        sm.is_initialized.set()
    return sm


def _transport():
    offer = SimpleNamespace(server_pubkey="srvpub", pairs=SimpleNamespace())
    return SimpleNamespace(target_pubkey=None,
                           get_offer=lambda npub: offer if npub == NPUB else None)


def _action() -> ReverseSwapAction:
    return ReverseSwapAction(channel_id="aa" * 32, short_id="1x1x1",
                             lightning_amount_sat=400_000, reason="drain",
                             provider_npub=NPUB)


def test_reverse_swap_success_records_success() -> None:
    p, sm = _plugin(), _swap_manager(lambda **kw: _coro("funding-txid"))
    w = _FakeWallet(sm)
    p._log_action = lambda *a, **k: None
    p.on_action_done = lambda *a, **k: None
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 0


def test_reverse_swap_decline_is_not_a_fault() -> None:
    from electrum.util import UserFacingException

    def _raise(**kw):
        raise UserFacingException("uneconomical")
    p, sm = _plugin(), _swap_manager(lambda **kw: _raise(**kw))
    w = _FakeWallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w) == {}     # no fault recorded


def test_reverse_swap_rpc_error_is_a_soft_fault() -> None:
    # A createswap SwapServerError is ambiguous (the server masks the real cause)
    # and is most often transient provider capacity, so it is a SOFT fault: it is
    # recorded for visibility but does not escalate the ranking penalty.
    from electrum.submarine_swaps import SwapServerError

    def _raise(**kw):
        raise SwapServerError("boom")
    p, sm = _plugin(), _swap_manager(lambda **kw: _raise(**kw))
    w = _FakeWallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 1        # floored at one decaying level
    assert stats["fault_count"] == 1
    assert "capacity" in stats["last_reason"].lower()
    # (Non-escalation across repeats is covered at the store level by
    # test_soft_fault_floors_at_one_level_and_does_not_escalate; here a single
    # channel would in any case be blocked from re-attempting by the swap cooldown.)


def test_reverse_swap_unhostable_amount_is_skipped_not_a_fault() -> None:
    # get_recv_amount() returns None => the provider can't host this amount right
    # now (outside its min/max, or nets below dust). The swap must be skipped
    # cleanly: reverse_swap() is never called and the provider is NOT penalised.
    # (Regression guard for the int-minus-None TypeError that used to be charged
    # to the provider as a bogus "swap error" fault.)
    attempted: List[dict] = []
    p, sm = _plugin(), _swap_manager(lambda **kw: attempted.append(kw) or _coro("txid"))
    sm.get_recv_amount = lambda amt, *, is_reverse: None
    w = _FakeWallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert attempted == []                   # reverse_swap never attempted
    assert p._load_reliability(w) == {}      # provider not penalised


def test_reverse_swap_internal_error_is_not_a_fault() -> None:
    # An unexpected exception from reverse_swap() is treated as a bug on our side,
    # logged as an internal error, and NOT charged to the provider's reliability.
    def _raise(**kw):
        raise TypeError("unsupported operand type(s) for -: 'int' and 'NoneType'")
    p, sm = _plugin(), _swap_manager(lambda **kw: _raise(**kw))
    w = _FakeWallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w) == {}      # provider not penalised for our bug


def test_reverse_swap_timeout_is_a_fault(monkeypatch) -> None:
    async def _never(**kw):
        return "unused"
    p, sm = _plugin(), _swap_manager(lambda **kw: _never(**kw), initialized=False)
    w = _FakeWallet(sm)

    async def _raise_timeout(awaitable, timeout):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(pkg.asyncio, "wait_for", _raise_timeout)
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert "timeout" in p._load_reliability(w)[NPUB]["last_reason"].lower()


def test_reverse_swap_no_funding_tracks_pending() -> None:
    # Provider accepted (a swap row appears) but returned no funding txid yet:
    # tracked for the stuck reconciler, no success recorded.
    def _accept(**kw):
        sm._swaps["ph123"] = SimpleNamespace(is_redeemed=False, funding_txid=None)
        return _coro(None)
    p = _plugin()
    sm = _swap_manager(_accept)
    w = _FakeWallet(sm)
    p._log_action = lambda *a, **k: None
    p.on_action_done = lambda *a, **k: None
    asyncio.run(p._reverse_swap(w, _action(), state={}, transport=_transport()))
    pending = p._load_pending_swaps(w)
    assert "ph123" in pending and pending["ph123"]["npub"] == NPUB
    assert NPUB not in p._load_reliability(w)        # no success yet


# --- stuck-swap reconciliation --------------------------------------------
def _reconcile_wallet(swap_obj) -> "_FakeWallet":
    sm = SimpleNamespace(_swaps={}, get_swap=lambda ph: swap_obj)
    return _FakeWallet(sm)


def test_reconcile_funded_swap_is_success() -> None:
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=False, funding_txid="txid"))
    w.db.put(PENDING_SWAPS_DB_KEY, {"abcd": {"npub": NPUB, "started_ts": time.time()}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    assert p._load_pending_swaps(w) == {}            # untracked


def test_reconcile_stuck_swap_is_fault() -> None:
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=False, funding_txid=None))
    # Started well past the default 60-min stuck timeout, never funded.
    w.db.put(PENDING_SWAPS_DB_KEY,
             {"abcd": {"npub": NPUB, "started_ts": time.time() - 4000}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert "stuck" in p._load_reliability(w)[NPUB]["last_reason"]
    assert p._load_pending_swaps(w) == {}


def test_reconcile_young_unfunded_swap_waits() -> None:
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=False, funding_txid=None))
    w.db.put(PENDING_SWAPS_DB_KEY,
             {"abcd": {"npub": NPUB, "started_ts": time.time()}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w) == {}              # nothing recorded yet
    assert "abcd" in p._load_pending_swaps(w)        # still tracked


# --- soft vs hard provider fault ------------------------------------------
def test_soft_fault_floors_at_one_level_and_does_not_escalate() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "transient", soft=True)
    p._record_provider_fault(w, NPUB, "transient", soft=True)
    p._record_provider_fault(w, NPUB, "transient", soft=True)
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 1        # floored, never compounds
    assert stats["fault_count"] == 3               # each recorded for visibility
    # Penalty is the single base level (0.5%), not 0.5 * 2^2.
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(0.5, abs=1e-3)


def test_hard_fault_still_escalates_over_soft_floor() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "transient", soft=True)   # floor -> 1
    p._record_provider_fault(w, NPUB, "unreachable")            # hard -> 2
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 2
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(1.0, abs=1e-3)


def test_soft_fault_logs_soft_prefix() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "transient", soft=True)
    log = w.db.get("inbound_liquidity_decision_log", [])
    fault_entries = [e for e in log if e.get("category") == "fault"]
    assert fault_entries and fault_entries[-1]["reason"].startswith("soft fault: ")


# --- escalation ages out at the same rate as the penalty -------------------
# The penalty's two terms collapse to base * 2^((n-1) - age/H), so a half-life of
# silence is already worth exactly one fault level. Escalation now agrees: the
# counter is incremented as AGED, not as stored. Previously it had unbounded
# memory, so a provider forgiven down to a zero penalty still resumed at
# base * 2^(old faults).
def _backdate(p, w, npub: str, seconds: float) -> None:
    data = p._load_reliability(w)
    data[npub]["last_fault_ts"] = time.time() - seconds
    p._save_reliability(w, data)


HALFLIFE_SEC = 6 * 3600     # the shipped default


def test_quiet_half_life_forgives_one_escalation_level() -> None:
    p, w = _plugin(), _FakeWallet()
    for _ in range(3):
        p._record_provider_fault(w, NPUB, "x")
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 3
    # Two quiet half-lives forgive two levels, so the next fault lands at 3-2+1.
    _backdate(p, w, NPUB, 2 * HALFLIFE_SEC)
    p._record_provider_fault(w, NPUB, "y")
    stats = p._load_reliability(w)[NPUB]
    assert stats["consecutive_faults"] == 2
    assert stats["fault_count"] == 4              # lifetime tally is untouched
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(1.0, abs=1e-3)


def test_long_quiet_period_resets_escalation_to_the_base_level() -> None:
    p, w = _plugin(), _FakeWallet()
    for _ in range(3):
        p._record_provider_fault(w, NPUB, "x")
    # A month of silence at a 6h half-life is ~120 half-lives: fully forgiven, so
    # a returning provider starts over at ONE level (0.5%), not 0.5 * 2^3 = 4%.
    _backdate(p, w, NPUB, 30 * 86400)
    p._record_provider_fault(w, NPUB, "y")
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(0.5, abs=1e-3)


def test_aging_does_not_forgive_faults_inside_one_half_life() -> None:
    # Bursts still compound: the whole point of escalation is to punish a
    # provider failing repeatedly *now*.
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")
    _backdate(p, w, NPUB, HALFLIFE_SEC * 0.9)     # not yet a whole level
    p._record_provider_fault(w, NPUB, "y")
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 2


def test_soft_fault_after_a_quiet_period_does_not_resurrect_escalation() -> None:
    # A soft fault floors at one level. Applied to the AGED counter, so a long
    # quiet spell leaves a soft fault at exactly the base penalty rather than
    # re-pinning whatever escalation the provider used to carry.
    p, w = _plugin(), _FakeWallet()
    for _ in range(4):
        p._record_provider_fault(w, NPUB, "x")
    _backdate(p, w, NPUB, 30 * 86400)
    p._record_provider_fault(w, NPUB, "transient", soft=True)
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 1
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] == pytest.approx(0.5, abs=1e-3)


def test_aging_is_not_applied_on_read() -> None:
    # The stored counter means "levels as of last_fault_ts"; the read path applies
    # the smooth fractional decay against that timestamp. Decaying on read too
    # would count the same quiet time twice.
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")
    p._record_provider_fault(w, NPUB, "y")        # 2 levels -> 1.0% fresh
    _backdate(p, w, NPUB, HALFLIFE_SEC)
    rows = p.provider_reliability_rows(w)
    assert rows[NPUB]["consecutive_faults"] == 2                     # unchanged
    assert rows[NPUB]["penalty_pct"] == pytest.approx(0.5, abs=1e-3)  # 1.0 halved


def test_aging_is_skipped_when_decay_is_disabled() -> None:
    # halflife_hours = 0 switches decay off; with no decay there is nothing to
    # forgive, so escalation must keep compounding.
    p, w = _plugin(), _FakeWallet()
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS=0.0)
    p._record_provider_fault(w, NPUB, "x")
    _backdate(p, w, NPUB, 365 * 86400)
    p._record_provider_fault(w, NPUB, "y")
    assert p._load_reliability(w)[NPUB]["consecutive_faults"] == 2


# --- store housekeeping ----------------------------------------------------
def test_prune_drops_idle_unpenalised_rows() -> None:
    p, w = _plugin(), _FakeWallet()
    now = time.time()
    p._save_reliability(w, {
        # Idle for well over the 90-day bound, and decayed to nothing: goes.
        "stale": {"consecutive_faults": 2, "fault_count": 2,
                  "last_fault_ts": now - 200 * 86400},
        # Same age, but its last contact was a SUCCESS: still idle, still goes.
        "stale-ok": {"success_count": 5, "last_success_ts": now - 200 * 86400},
        # Recent fault: stays.
        "fresh": {"consecutive_faults": 1, "fault_count": 1, "last_fault_ts": now},
        # Old fault but a recent success, so not idle: stays.
        "active": {"consecutive_faults": 0, "fault_count": 1, "success_count": 1,
                   "last_fault_ts": now - 200 * 86400, "last_success_ts": now - 60},
    })
    p._prune_reliability_store(w)
    assert set(p._load_reliability(w)) == {"fresh", "active"}


def test_prune_keeps_an_idle_row_that_still_carries_a_penalty() -> None:
    # A very long half-life means a 100-day-old fault is still penalised. Pruning
    # it would silently pardon the provider, so idleness alone is not enough.
    p, w = _plugin(), _FakeWallet()
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS=24 * 365.0)
    p._save_reliability(w, {
        NPUB: {"consecutive_faults": 3, "fault_count": 3,
               "last_fault_ts": time.time() - 100 * 86400},
    })
    assert p.provider_reliability_rows(w)[NPUB]["penalty_pct"] > 0.001   # premise
    p._prune_reliability_store(w)
    assert NPUB in p._load_reliability(w)


def test_prune_is_not_a_wipe_when_reliability_is_switched_off() -> None:
    # With the feature disabled every penalty reads 0. The prune must not take
    # that as licence to delete a provider's live history.
    p, w = _plugin(), _FakeWallet()
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_RELIABILITY_ENABLED=False,
                               INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS=24 * 365.0)
    p._save_reliability(w, {
        NPUB: {"consecutive_faults": 3, "fault_count": 3,
               "last_fault_ts": time.time() - 100 * 86400},
    })
    p._prune_reliability_store(w)
    assert NPUB in p._load_reliability(w)


def test_prune_does_not_write_when_nothing_is_dropped() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_provider_fault(w, NPUB, "x")
    saves_before = w.saved
    p._prune_reliability_store(w)
    assert w.saved == saves_before          # no pointless wallet write
    assert NPUB in p._load_reliability(w)


def test_prune_of_an_empty_store_is_a_noop() -> None:
    p, w = _plugin(), _FakeWallet()
    p._prune_reliability_store(w)
    assert w.saved == 0


def test_reconcile_drops_ancient_pending_swap_without_blaming_anyone() -> None:
    # 90+ days unresolved means reconciliation never ran on it (automation off, or
    # the wallet closed for months). The swap manager has long since dropped an
    # unfunded swap, so the stuck branch would read a months-dead event as a fresh
    # full-strength fault, stamped today. Drop it unresolved instead.
    p = _plugin()
    w = _reconcile_wallet(None)              # sm.get_swap knows nothing about it
    w.db.put(PENDING_SWAPS_DB_KEY,
             {"abcd": {"npub": NPUB, "node_id": "peer", "started_ts": time.time() - 91 * 86400}})
    p._record_peer_fault = lambda *a, **k: pytest.fail("peer must not be faulted")
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w) == {}      # no provider fault
    assert p._load_pending_swaps(w) == {}    # and the record is gone


def test_reconcile_drops_pending_swap_with_no_start_timestamp() -> None:
    # Only reachable through a corrupted store. It used to age past every timeout
    # instantly and fault its provider on the very first tick.
    p = _plugin()
    w = _reconcile_wallet(None)
    w.db.put(PENDING_SWAPS_DB_KEY, {"abcd": {"npub": NPUB}})   # no started_ts
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w) == {}
    assert p._load_pending_swaps(w) == {}


def test_reconcile_still_credits_an_ancient_swap_that_did_fund() -> None:
    # Age does not invalidate a confirmed delivery, and the dev fee for it is
    # still owed -- so the funded check must sit ahead of the age bound.
    p = _plugin()
    w = _reconcile_wallet(SimpleNamespace(is_redeemed=True, funding_txid="txid"))
    fees: List[int] = []
    p._accrue_dev_fee = lambda wallet, amount_sat, source=None: fees.append(amount_sat)
    w.db.put(PENDING_SWAPS_DB_KEY,
             {"abcd": {"npub": NPUB, "started_ts": time.time() - 200 * 86400,
                       "fee_basis_sat": 1234}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w)[NPUB]["success_count"] == 1
    assert fees == [1234]
    assert p._load_pending_swaps(w) == {}


# --- _chan_unsettled_is_swap ----------------------------------------------
def _htlc(payment_hash: bytes) -> SimpleNamespace:
    return SimpleNamespace(payment_hash=payment_hash)


def _chan_with_htlcs(*payment_hashes: bytes) -> SimpleNamespace:
    hm = SimpleNamespace(htlcs=lambda subject: [("dir", _htlc(ph)) for ph in payment_hashes])
    return SimpleNamespace(hm=hm)


def test_chan_unsettled_is_swap_matches_swap_payment_hash() -> None:
    swap_ph = b"\xab" * 32
    sm = SimpleNamespace(get_swap=lambda ph: object() if ph == swap_ph else None)
    chan = _chan_with_htlcs(swap_ph)
    assert LiquidityPlugin._chan_unsettled_is_swap(chan, sm) is True


def test_chan_unsettled_is_swap_false_for_unknown_htlc() -> None:
    sm = SimpleNamespace(get_swap=lambda ph: None)   # no swap matches
    chan = _chan_with_htlcs(b"\x01" * 32, b"\x02" * 32)
    assert LiquidityPlugin._chan_unsettled_is_swap(chan, sm) is False


def test_chan_unsettled_is_swap_survives_lookup_errors() -> None:
    def _boom(ph):
        raise RuntimeError("adb not ready")
    sm = SimpleNamespace(get_swap=_boom)
    chan = _chan_with_htlcs(b"\x03" * 32)
    # A lookup failure must degrade to False, never propagate.
    assert LiquidityPlugin._chan_unsettled_is_swap(chan, sm) is False


def _coro(value):
    async def _c():
        return value
    return _c()


# --- pending-swap tracking must survive hostile input ---------------------
# Regression: `JsonDB.put` deep-copies what it is given and raises on anything
# it cannot copy. A non-primitive reaching this map produced
# `TypeError: cannot pickle '_thread.RLock' object` from inside db.put, which
# escaped `_reverse_swap` and killed the executor *after* a swap had been
# created -- losing the tracking record that exists to reconcile it.
def test_track_pending_swap_persists_only_json_primitives() -> None:
    import threading

    class _Rich:
        """Stands in for a SwapData / Channel / offer: carries a lock, so a
        deepcopy of it raises exactly as JsonDB's would."""
        def __init__(self, label: str) -> None:
            self._lock = threading.RLock()
            self.label = label

        def __str__(self) -> str:
            return self.label

    p, w = _plugin(), _FakeWallet()
    p._track_pending_swap(w, "ph_rich", _Rich("npubX"),
                          node_id=_Rich("02aa"), channel_id=_Rich("cid"),
                          fee_basis_sat="not-a-number")
    rec = p._load_pending_swaps(w)["ph_rich"]
    assert rec == {"npub": "npubX", "node_id": "02aa", "channel_id": "cid",
                   "fee_basis_sat": 0, "started_ts": rec["started_ts"]}
    for value in rec.values():
        assert isinstance(value, (str, int, float)), rec
    # The whole record is deep-copyable, which is what db.put actually requires.
    import copy
    copy.deepcopy(p._load_pending_swaps(w))


def test_track_pending_swap_coerces_numeric_fee_basis() -> None:
    p, w = _plugin(), _FakeWallet()
    p._track_pending_swap(w, "ph1", "npubA", node_id="02bb", channel_id="cid1",
                          fee_basis_sat=12_345)
    assert p._load_pending_swaps(w)["ph1"]["fee_basis_sat"] == 12_345


def test_save_pending_swaps_never_raises_out_of_the_executor() -> None:
    # Persistence failing must not sink an action that already happened: the
    # swap exists on the wire whether or not we managed to write it down.
    class _BoomDB(_FakeDB):
        def put(self, key, value):
            raise TypeError("cannot pickle '_thread.RLock' object")

    p, w = _plugin(), _FakeWallet()
    w.db = _BoomDB()
    p._track_pending_swap(w, "ph2", "npubA")      # must not raise
    assert p._load_pending_swaps(w) == {}


def test_track_pending_swap_ignores_an_empty_payment_hash() -> None:
    p, w = _plugin(), _FakeWallet()
    p._track_pending_swap(w, "", "npubA")
    p._track_pending_swap(w, None, "npubA")       # type: ignore[arg-type]
    assert p._load_pending_swaps(w) == {}
