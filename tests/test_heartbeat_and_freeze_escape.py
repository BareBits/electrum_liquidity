"""Glue tests for the three robustness fixes:

  * #1 the periodic heartbeat that advances time-based watchdogs without
    depending on wallet events,
  * #2/#3 stop_wallet clearing all per-wallet state (+ cancelling the heartbeat)
    and on_close unregistering the global event callback,
  * #4 the stuck-reverse-swap freeze escape: a swap wedged past the timeout stops
    counting toward the in-flight freeze.

All exercise the real plugin glue against minimal fakes (no running Electrum).
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin, SWAP_FIRST_SEEN_DB_KEY)


# --- fakes ----------------------------------------------------------------
class _FakeFuture:
    """Stand-in for the concurrent.futures.Future a heartbeat is stored as."""
    def __init__(self) -> None:
        self.cancel_called = False

    def cancel(self) -> None:
        self.cancel_called = True

    def done(self) -> bool:
        return self.cancel_called


class _FakeDB:
    def __init__(self) -> None:
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value) -> None:
        self._d[key] = value


class _FakeWallet:
    def __init__(self, db=None, loop=None) -> None:
        self.db = db
        self.network = SimpleNamespace(asyncio_loop=loop) if loop is not None else None

    def save_db(self) -> None:
        pass

    def basename(self) -> str:
        return "test-wallet"


class _PendingSwap:
    """Minimal SwapData stand-in: only the payment_hash the freeze-count reads."""
    def __init__(self, payment_hash: bytes) -> None:
        self.payment_hash = payment_hash


class _FakeSM:
    def __init__(self, pending) -> None:
        self._pending = pending

    def get_pending_swaps(self):
        return list(self._pending)


def _bare_plugin() -> LiquidityPlugin:
    """A plugin with every per-wallet dict initialised (from the class's own
    canonical list) but none of __init__'s side effects (no callback register)."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.heartbeat")
    p.wallets = {}
    p._heartbeat_tasks = {}
    for attr in LiquidityPlugin._PER_WALLET_STATE_ATTRS:
        setattr(p, attr, {})
    p.config = SimpleNamespace()
    return p


# --- #1 heartbeat ---------------------------------------------------------
def test_heartbeat_ticks_then_stops_on_stop_wallet() -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        p = _bare_plugin()
        p._heartbeat_interval_sec = 0.02
        calls: list = []

        async def _fake_eval(wallet) -> None:
            calls.append(wallet)

        p._evaluate = _fake_eval  # type: ignore[assignment]
        wallet = _FakeWallet(loop=loop)
        p.wallets[wallet] = asyncio.Lock()

        p._start_heartbeat(wallet)
        assert wallet in p._heartbeat_tasks
        fut = p._heartbeat_tasks[wallet]

        await asyncio.sleep(0.1)          # ~5 intervals
        assert len(calls) >= 2, "heartbeat should have evaluated several times"

        # Stop: wallet leaves the managed set, heartbeat is cancelled and forgotten.
        p.stop_wallet(wallet)
        assert wallet not in p.wallets
        assert wallet not in p._heartbeat_tasks
        seen = len(calls)
        await asyncio.sleep(0.1)
        assert len(calls) == seen, "heartbeat kept ticking after stop_wallet"
        assert fut.cancelled() or fut.done()

    asyncio.run(main())


def test_start_heartbeat_does_not_double_start() -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        p = _bare_plugin()
        p._heartbeat_interval_sec = 5.0     # long enough that neither ticks
        wallet = _FakeWallet(loop=loop)
        p.wallets[wallet] = asyncio.Lock()

        p._start_heartbeat(wallet)
        first = p._heartbeat_tasks[wallet]
        p._start_heartbeat(wallet)          # a second call must be a no-op
        assert p._heartbeat_tasks[wallet] is first
        p.stop_wallet(wallet)

    asyncio.run(main())


def test_start_heartbeat_noop_without_loop() -> None:
    p = _bare_plugin()
    wallet = _FakeWallet(loop=None)         # offline wallet: no asyncio loop
    p.wallets[wallet] = "lock"
    p._start_heartbeat(wallet)
    assert wallet not in p._heartbeat_tasks


# --- #2 stop_wallet clears all per-wallet state ---------------------------
def test_stop_wallet_forgets_all_per_wallet_state() -> None:
    p = _bare_plugin()
    wallet = _FakeWallet()
    # Populate every per-wallet dict + the managed set + a (fake) heartbeat.
    for attr in LiquidityPlugin._PER_WALLET_STATE_ATTRS:
        getattr(p, attr)[wallet] = {"marker": True}
    p.wallets[wallet] = "lock"
    fut = _FakeFuture()
    p._heartbeat_tasks[wallet] = fut

    p.stop_wallet(wallet)

    assert wallet not in p.wallets
    assert fut.cancel_called, "heartbeat future should be cancelled"
    for attr in LiquidityPlugin._PER_WALLET_STATE_ATTRS:
        assert wallet not in getattr(p, attr), f"{attr} still holds the wallet"


def test_stop_wallet_clears_wedging_guard_flags() -> None:
    """A payout / eval flag left True across a stop must not survive to wedge the
    next session (the concrete bug the cleanup fixes)."""
    p = _bare_plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = "lock"
    p._dev_fee_paying[wallet] = True
    p._eval_pending[wallet] = True

    p.stop_wallet(wallet)

    assert wallet not in p._dev_fee_paying
    assert wallet not in p._eval_pending


# --- #3 on_close unregisters the callback + cancels heartbeats -------------
def test_on_close_cancels_heartbeats_and_unregisters(monkeypatch) -> None:
    from electrum import util
    p = _bare_plugin()
    recorded: list = []
    monkeypatch.setattr(util, "unregister_callback", lambda cb: recorded.append(cb))
    p._on_wallet_event = "SENTINEL_CB"      # type: ignore[assignment]
    w1, w2 = _FakeWallet(), _FakeWallet()
    f1, f2 = _FakeFuture(), _FakeFuture()
    p._heartbeat_tasks[w1] = f1
    p._heartbeat_tasks[w2] = f2

    p.on_close()

    assert f1.cancel_called and f2.cancel_called
    assert p._heartbeat_tasks == {}
    assert recorded == ["SENTINEL_CB"]


# --- #4 stuck-swap freeze escape ------------------------------------------
def _plugin_with_timeout(minutes: int) -> LiquidityPlugin:
    p = _bare_plugin()
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN=minutes)
    return p


def test_fresh_pending_swaps_all_freeze_and_are_recorded() -> None:
    p = _plugin_with_timeout(60)
    wallet = _FakeWallet(db=_FakeDB())
    sm = _FakeSM([_PendingSwap(b"\x11" * 32), _PendingSwap(b"\x22" * 32)])
    now = 1_000_000.0

    count = p._count_freezing_swaps(wallet, sm, now)

    assert count == 2                       # both fresh -> both still freeze
    first_seen = wallet.db.get(SWAP_FIRST_SEEN_DB_KEY)
    assert set(first_seen) == {"11" * 32, "22" * 32}
    assert all(ts == now for ts in first_seen.values())


def test_swap_past_timeout_stops_freezing() -> None:
    p = _plugin_with_timeout(60)            # 3600s
    db = _FakeDB()
    now = 1_000_000.0
    aged = "11" * 32
    fresh = "22" * 32
    # aged swap first seen well beyond the timeout; fresh one just now.
    db.put(SWAP_FIRST_SEEN_DB_KEY, {aged: now - 4_000.0, fresh: now - 10.0})
    wallet = _FakeWallet(db=db)
    sm = _FakeSM([_PendingSwap(bytes.fromhex(aged)), _PendingSwap(bytes.fromhex(fresh))])

    count = p._count_freezing_swaps(wallet, sm, now)

    assert count == 1                       # only the fresh swap still freezes


def test_first_seen_pruned_when_swap_no_longer_pending() -> None:
    p = _plugin_with_timeout(60)
    db = _FakeDB()
    now = 1_000_000.0
    stale = "99" * 32                       # tracked but no longer pending
    live = "22" * 32
    db.put(SWAP_FIRST_SEEN_DB_KEY, {stale: now - 100.0, live: now - 100.0})
    wallet = _FakeWallet(db=db)
    sm = _FakeSM([_PendingSwap(bytes.fromhex(live))])   # only `live` still pending

    count = p._count_freezing_swaps(wallet, sm, now)

    assert count == 1
    assert set(db.get(SWAP_FIRST_SEEN_DB_KEY)) == {live}, "stale entry not pruned"


def test_no_pending_swaps_counts_zero() -> None:
    p = _plugin_with_timeout(60)
    wallet = _FakeWallet(db=_FakeDB())
    assert p._count_freezing_swaps(wallet, _FakeSM([]), 1_000_000.0) == 0


def test_swaps_without_payment_hash_are_ignored() -> None:
    p = _plugin_with_timeout(60)
    wallet = _FakeWallet(db=_FakeDB())
    sm = _FakeSM([SimpleNamespace(payment_hash=None), SimpleNamespace()])
    # Neither yields a usable hash; nothing freezes and nothing crashes.
    assert p._count_freezing_swaps(wallet, sm, 1_000_000.0) == 0
