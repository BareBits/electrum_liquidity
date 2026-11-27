"""Glue-level tests for reaping the funding waiter Electrum orphans.

``SwapManager.reverse_swap`` races the Lightning payment against funding
detection and never cancels the loser::

    async def wait_for_funding(swap):
        while swap.funding_txid is None:
            await asyncio.sleep(0.1)
    tasks = [create_task(pay_invoice(...)), create_task(wait_for_funding(swap))]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    return swap.funding_txid

When the payment finishes first -- the case the failover cascade exists for --
the waiter polls at 10Hz forever, pinning the swap object. One per failed
attempt, up to three per cascade, for the life of the process.

``_reap_funding_waiters`` cancels it. Identifying the task means matching a
closure inside third-party code, so the fake below reproduces Electrum's real
shape (a nested ``wait_for_funding`` inside a method named ``reverse_swap``,
closing over a ``swap``) rather than stubbing the introspection out -- a fake
that did not would pass while the real matching silently failed.

Covered here:
  * the orphaned waiter is cancelled, on the failed-payment path;
  * a concurrent swap's waiter is NOT touched (scoped to swaps we created);
  * the pay_invoice task is never touched (on the funded path it must run on);
  * the timeout path reaps too -- wait_for cancelling reverse_swap does not
    cancel the tasks inside its own asyncio.wait;
  * a changed upstream shape degrades to a no-op instead of raising.

Heavy Electrum objects are faked; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional, Set

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.reap")
    p._reverse_swap_timeout_sec = 5.0
    # Per-provider wait for advertised terms; shrunk so an unreachable
    # provider trips the init timeout instantly instead of in 15s.
    p._swap_init_timeout_sec = 0.01
    # Bounded wait for a swap's mining-fee prepayment to resolve before the
    # failover gate decides; shrunk so no test sits through the real 150s.
    p._htlc_resolve_wait_sec = 0.05
    p._htlc_poll_interval_sec = 0.01
    # One-slot per-channel dedupe for declines the executor writes itself.
    p._last_exec_decline_sig = {}
    return p


class _FakeSwapManager:
    """Mimics the *shape* of Electrum's SwapManager.reverse_swap, because that
    shape is exactly what the reaper matches on."""

    def __init__(self) -> None:
        self._swaps: Dict[str, object] = {}
        self.pay_tasks: List[asyncio.Task] = []

    async def reverse_swap(self, *, payment_hash_hex: str, fund: bool = False,
                           hang: bool = False) -> Optional[str]:
        swap = SimpleNamespace(funding_txid=None,
                               payment_hash=bytes.fromhex(payment_hash_hex))
        self._swaps[payment_hash_hex] = swap

        async def wait_for_funding(swap):
            while swap.funding_txid is None:
                await asyncio.sleep(0.01)

        async def pay():
            if hang:
                await asyncio.sleep(3600)
            await asyncio.sleep(0.01)
            if fund:
                # Funding lands first: the waiter exits on its own.
                swap.funding_txid = "txid-" + payment_hash_hex[:8]
                await asyncio.sleep(0.05)
            return (not hang, [])

        pay_task = asyncio.create_task(pay())
        self.pay_tasks.append(pay_task)
        tasks = [pay_task, asyncio.create_task(wait_for_funding(swap))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return swap.funding_txid


def _live_waiters() -> List[asyncio.Task]:
    return [t for t in asyncio.all_tasks()
            if not t.done()
            and getattr(t.get_coro(), "__qualname__", "").endswith(
                "reverse_swap.<locals>.wait_for_funding")]


async def _drain() -> None:
    """Let cancellations actually take effect."""
    for _ in range(5):
        await asyncio.sleep(0)


A = "aa" * 32
B = "bb" * 32


def test_orphaned_waiter_is_cancelled_on_the_failed_payment_path() -> None:
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        before: Set[str] = set(sm._swaps)
        txid = await p._call_reverse_swap(sm, {"payment_hash_hex": A}, before)
        assert txid is None                      # the payment lost the race
        await _drain()
        assert _live_waiters() == [], "the funding waiter was left polling"

    asyncio.run(go())


def test_a_concurrent_swaps_waiter_is_left_alone() -> None:
    """Scoped to swaps that appeared during OUR call, so a swap the user started
    by hand keeps the waiter its own reverse_swap is still racing on."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        # Somebody else's swap, already in flight and mid-race.
        other = SimpleNamespace(funding_txid=None, payment_hash=bytes.fromhex(B))
        sm._swaps[B] = other

        async def other_wait():
            # Same qualname shape as the real one, different swap.
            async def reverse_swap():
                async def wait_for_funding(swap):
                    while swap.funding_txid is None:
                        await asyncio.sleep(0.01)
                await wait_for_funding(other)
            await reverse_swap()

        other_task = asyncio.create_task(other_wait())
        await asyncio.sleep(0.02)

        before = set(sm._swaps)                  # B is already present
        await p._call_reverse_swap(sm, {"payment_hash_hex": A}, before)
        await _drain()
        assert not other_task.done(), "cancelled a concurrent swap's waiter"
        other_task.cancel()

    asyncio.run(go())


def test_the_payment_task_is_never_touched() -> None:
    """On the funded path the waiter has already exited and pay_invoice must keep
    running -- the swap only settles once the payment completes."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        txid = await p._call_reverse_swap(
            sm, {"payment_hash_hex": A, "fund": True}, set())
        assert txid == "txid-" + A[:8]
        await _drain()
        assert not sm.pay_tasks[0].cancelled(), "cancelled the payment task"
        await asyncio.gather(*sm.pay_tasks)

    asyncio.run(go())


def test_the_timeout_path_reaps_too() -> None:
    """asyncio.wait_for cancels reverse_swap, but NOT the tasks it left running
    inside its own asyncio.wait -- so the waiter leaks there as well."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        p._reverse_swap_timeout_sec = 0.05
        with pytest.raises(asyncio.TimeoutError):
            await p._call_reverse_swap(
                sm, {"payment_hash_hex": A, "hang": True}, set())
        await _drain()
        assert _live_waiters() == [], "the timeout path left a funding waiter"
        for t in sm.pay_tasks:
            t.cancel()

    asyncio.run(go())


def test_reaping_reports_how_many_it_cancelled() -> None:
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        await p._call_reverse_swap(sm, {"payment_hash_hex": A}, set())
        await _drain()
        # Already reaped by the call above; a second pass finds nothing.
        assert p._reap_funding_waiters(sm, set()) == 0

    asyncio.run(go())


def test_no_new_swap_means_nothing_to_reap() -> None:
    """A failure before add_reverse_swap creates no swap and no waiter."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        assert p._reap_funding_waiters(sm, set()) == 0

    asyncio.run(go())


def test_the_funded_path_does_not_cry_shape_change(caplog) -> None:
    """Finding nothing to reap is EXPECTED once funding lands: there the waiter is
    what won the race, so it has already exited. Warning then would fire on every
    successful swap and train the eye to ignore the one message that matters --
    which is exactly what the rig caught the first time round."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        with caplog.at_level(logging.DEBUG, logger=p.logger.name):
            txid = await p._call_reverse_swap(
                sm, {"payment_hash_hex": A, "fund": True}, set())
        assert txid is not None
        assert "may have changed shape" not in caplog.text
        await asyncio.gather(*sm.pay_tasks)

    asyncio.run(go())


def test_an_unfunded_swap_with_no_waiter_does_report_shape_change(caplog) -> None:
    """The complement: an UNFUNDED swap should still have a live waiter, so
    finding none really does mean the match stopped firing."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        sm._swaps[A] = SimpleNamespace(funding_txid=None,
                                       payment_hash=bytes.fromhex(A))
        with caplog.at_level(logging.DEBUG, logger=p.logger.name):
            # Must run inside a loop: asyncio.all_tasks() raises RuntimeError
            # without one, and the reaper bails out before it can report.
            assert p._reap_funding_waiters(sm, set()) == 0
        assert "may have changed shape" in caplog.text

    asyncio.run(go())


def test_reaping_outside_an_event_loop_is_a_no_op() -> None:
    """``asyncio.all_tasks()`` raises without a running loop. The reaper runs in
    the ``finally`` of a call that may just have created a swap, so it has to
    bail out quietly rather than let that exception escape over the top of
    whatever the swap attempt was returning."""
    sm = _FakeSwapManager()
    p = _plugin()
    sm._swaps[A] = SimpleNamespace(funding_txid=None,
                                   payment_hash=bytes.fromhex(A))
    # Deliberately not inside asyncio.run(): that is the condition under test.
    assert p._reap_funding_waiters(sm, set()) == 0


def _live_waiter_hashes() -> Set[str]:
    """The payment hash of every live funding waiter, read the way the reaper
    reads it. Lets a test say WHICH waiter survived rather than only how many,
    which is the whole point of the scoping rule."""
    out: Set[str] = set()
    for task in _live_waiters():
        frame = getattr(task.get_coro(), "cr_frame", None)
        swap = frame.f_locals.get("swap") if frame is not None else None
        ph = getattr(swap, "payment_hash", None)
        if ph is not None:
            out.add(ph.hex())
    return out


def test_only_our_own_swaps_waiter_is_cancelled() -> None:
    """The scoping rule, actually exercised.

    ``test_a_concurrent_swaps_waiter_is_left_alone`` above builds its other
    waiter by hand, and the task it ends up with has the qualname of the
    *wrapper* -- so it is filtered out by the qualname match and never reaches
    the ``payment_hash not in new_hashes`` check at all. That test therefore
    passes whether or not the hash scoping exists.

    Here the other waiter comes from the fake's own ``reverse_swap``, so it has
    the matching qualname and the hash is the only thing that can spare it:
    delete the scoping and B's waiter is cancelled along with A's.
    """
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        # B was started earlier and lost its own race, so its waiter is still
        # polling -- a user-initiated swap running while we attempt ours.
        assert await sm.reverse_swap(payment_hash_hex=B) is None
        assert _live_waiter_hashes() == {B}

        before: Set[str] = set(sm._swaps)            # {B}: not ours to reap
        assert await p._call_reverse_swap(
            sm, {"payment_hash_hex": A}, before) is None
        await _drain()

        assert _live_waiter_hashes() == {B}, \
            "the concurrent swap's waiter was cancelled with ours"
        for task in _live_waiters():
            task.cancel()

    asyncio.run(go())


def test_reaping_counts_only_the_waiters_it_cancelled() -> None:
    """A matching waiter that belongs to somebody else must not be counted as
    reaped either -- the count is what the debug line reports, and an inflated
    one would hide the shape-change warning this file's last test relies on."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()
        assert await sm.reverse_swap(payment_hash_hex=B) is None
        before: Set[str] = set(sm._swaps)
        await sm.reverse_swap(payment_hash_hex=A)     # ours: leaves a waiter
        assert p._reap_funding_waiters(sm, before) == 1
        await _drain()
        assert _live_waiter_hashes() == {B}
        for task in _live_waiters():
            task.cancel()

    asyncio.run(go())


def test_an_unreadable_waiter_is_skipped_rather_than_raising(caplog) -> None:
    """Introspecting a third-party closure is inherently brittle, so a waiter we
    cannot read must cost nothing. Here reading the swap's payment hash raises --
    the reaper has to step over it and leave the swap unharmed, because this runs
    in the ``finally`` of a call that may just have committed funds."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()

        class _Hostile:
            funding_txid = None

            @property
            def payment_hash(self):
                raise ValueError("cannot be read")

        async def reverse_swap():                     # matching qualname shape
            async def wait_for_funding(swap):
                while swap.funding_txid is None:
                    await asyncio.sleep(0.01)
            return asyncio.create_task(wait_for_funding(_Hostile()))

        leaked = await reverse_swap()
        await asyncio.sleep(0.02)
        sm._swaps[A] = SimpleNamespace(funding_txid=None,
                                       payment_hash=bytes.fromhex(A))
        with caplog.at_level(logging.DEBUG, logger=p.logger.name):
            assert p._reap_funding_waiters(sm, set()) == 0
        assert "could not inspect a task" in caplog.text
        assert not leaked.done(), "an unreadable waiter must be left alone"
        leaked.cancel()

    asyncio.run(go())


def test_a_changed_upstream_shape_degrades_to_a_no_op() -> None:
    """If Electrum restructures reverse_swap the match stops firing. That must
    put the leak back, not raise -- the swap itself is never at risk."""
    async def go():
        sm = _FakeSwapManager()
        p = _plugin()

        async def renamed_reverse_swap(*, payment_hash_hex: str):
            swap = SimpleNamespace(funding_txid=None,
                                   payment_hash=bytes.fromhex(payment_hash_hex))
            sm._swaps[payment_hash_hex] = swap

            async def poll_for_funding(swap):        # <-- renamed upstream
                while swap.funding_txid is None:
                    await asyncio.sleep(0.01)

            task = asyncio.create_task(poll_for_funding(swap))
            await asyncio.sleep(0.02)
            return None, task

        _txid, leaked = await renamed_reverse_swap(payment_hash_hex=A)
        assert p._reap_funding_waiters(sm, set()) == 0   # no match, no crash
        assert not leaked.done()                          # leak is back, as expected
        leaked.cancel()

    asyncio.run(go())
