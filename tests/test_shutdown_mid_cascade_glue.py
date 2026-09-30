"""Glue tests for quitting Electrum while a swap cascade is in progress.

The contract, in one sentence: stop cascading immediately, let the ONE attempt
already under way finish, then save and exit.

Each half of that is here. The "stop cascading" half is a flag the cascade reads
between rungs and between providers -- never mid-attempt, because interrupting a
swap whose Lightning payment may be in flight is the one thing the whole failover
design exists to avoid. The "let it finish" half is a threading.Event the
executor sets on every exit path, which teardown blocks on from the GUI thread.

Why this is not merely tidy: before it, `_evaluate` was a bare task on the
asyncio loop (see request_evaluation -- nothing holds its handle), so it was not
in the daemon taskgroup that `daemon.stop()` cancels. Teardown therefore ran
`wallet.stop()` -- which stops lnworker, sets `wallet.lnworker = None`, and
performs the LAST `db.write()` of the process -- underneath a cascade that was
still starting new providers. A swap created after that write exists only in
memory, and with it goes the privkey and preimage needed to ever claim its
on-chain output.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from typing import List, Optional

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    _InflightAttempt,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderAttempt,
    ReverseSwapAction,
)

A, B, C = "npubAAAprovider", "npubBBBprovider", "npubCCCprovider"


class _Wallet:
    """A hashable wallet stub -- the per-wallet stores are keyed by the wallet
    object, and SimpleNamespace defines __eq__ and so is unhashable."""

    def __init__(self, sm, ln_payment: str = "failed") -> None:
        self.lnworker = _LnWorker(sm, ln_payment)
        self.db = _DB()
        self.saves = 0

    def save_db(self) -> None:
        self.saves += 1

    @staticmethod
    def basename() -> str:
        return "test-wallet"


class _DB(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)

    def put(self, key, value) -> None:
        self[key] = value


class _LnWorker:
    def __init__(self, sm, ln_payment: str) -> None:
        self.swap_manager = sm
        self.channels: dict = {}
        self._sm = sm
        self._ln_payment = ln_payment

    @staticmethod
    def get_channel_by_id(cid):
        return SimpleNamespace(node_id=b"\x11" * 33)

    def get_payments(self, *, status=None):
        return set()

    def get_payment_status(self, ph, direction=None):
        from electrum.invoices import PR_UNPAID  # type: ignore
        return PR_UNPAID

    @property
    def logs(self):
        return {k: ["route attempt"] for k in self._sm._swaps}


class _SM(SimpleNamespace):
    def __init__(self, outcomes) -> None:
        self.outcomes = outcomes
        self.attempts: List[tuple] = []
        self.mining_fee = 1_000
        self.is_initialized = asyncio.Event()
        self.is_initialized.set()
        self._swaps = {}
        self._current = ""

    def update_pairs(self, pairs) -> None:
        self._current = pairs.npub

    @staticmethod
    def get_recv_amount(amt, *, is_reverse):
        return amt - 500

    def get_swap(self, payment_hash: bytes):
        return self._swaps.get(payment_hash.hex())

    async def reverse_swap(self, **kw):
        npub = self._current
        self.attempts.append((npub, kw["lightning_amount_sat"]))
        outcome = self.outcomes.get(npub, "txid-" + npub)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(self, **kw)
        return outcome


async def _accept_without_funding(sm, **kw):
    key = f"{len(sm._swaps):02x}" + "ff" * 31
    sm._swaps[key] = SimpleNamespace(prepay_hash=None)
    return None


def _plugin() -> LiquidityPlugin:
    """A plugin with the shutdown stores present, as a real one has them from
    ``__init__``. The other glue harnesses deliberately omit them (their wallets
    are unhashable stubs), which is why the shutdown handshake needs its own."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.shutdown")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p._reverse_swap_timeout_sec = 5.0
    p._swap_init_timeout_sec = 0.01
    p._htlc_resolve_wait_sec = 0.05
    p._htlc_poll_interval_sec = 0.01
    p.config = SimpleNamespace(SWAPSERVER_NPUB=None, SWAPSERVER_URL=None)
    # The stores under test.
    p._stopping = set()
    p._inflight_attempt = {}
    p._attempt_done_events = {}
    p.wallets = {}
    p._heartbeat_tasks = {}
    p.faults = []
    p.successes = []
    p.dev_fees = []
    p.tracked = []
    p.diags = []
    p.logged = []
    p.declines = []
    p._record_provider_fault = lambda wallet, npub, reason, **kw: \
        p.faults.append((npub, reason, kw))
    p._record_peer_fault = lambda wallet, node_id, reason, **kw: None
    p._record_provider_success = lambda wallet, npub: p.successes.append(npub)
    p._accrue_dev_fee = lambda wallet, amount_sat, source=None: \
        p.dev_fees.append((amount_sat, source))
    p._channel_peer_node_id = lambda wallet, channel_id: "peer-node-id"

    def _track(wallet, sm, before, npub, action, exp):
        new = set(sm._swaps) - before
        if new:
            p.tracked.append((npub, exp))
        return new
    p._track_new_swaps = _track
    p._diag_event = lambda wallet, **kw: p.diags.append(kw)
    p._log_action = lambda wallet, **kw: p.logged.append(kw)
    p._log_decline = lambda wallet, decline, state: p.declines.append((decline, state))
    p._last_exec_decline_sig = {}
    p.on_action_done = lambda wallet, msg: None
    return p


def _transport(npubs) -> SimpleNamespace:
    offers = {n: SimpleNamespace(server_pubkey="ab" * 32,
                                 pairs=SimpleNamespace(npub=n))
              for n in npubs}
    return SimpleNamespace(target_pubkey=None, target_npub=None,
                           get_offer=lambda npub: offers.get(npub))


def _action(chosen: str = A, alternates=(B, C), amount: int = 400_000,
            reduced: Optional[int] = None) -> ReverseSwapAction:
    return ReverseSwapAction(
        channel_id="aa" * 32, short_id="1x1x1",
        lightning_amount_sat=amount, reason="drain",
        provider_npub=chosen,
        reduced_amount_sat=reduced or 0,
        reduced_all_in_cost_pct=0.5 if reduced else None,
        alternates=tuple(ProviderAttempt(npub=n, amount_sat=amount,
                                         all_in_cost_pct=0.4)
                         for n in alternates))


# --- stop cascading, but never mid-attempt --------------------------------
def test_shutdown_stops_the_cascade_before_the_next_provider() -> None:
    """Three ranked providers, and the wallet starts closing during the first
    attempt. The other two must not be tried: each one is a fresh Lightning
    payment on a wallet whose db is about to be written for the last time."""
    sm = _SM({})
    p = _plugin()
    wallet = _Wallet(sm)

    async def _fail_and_begin_closing(sm_, **kw):
        p.begin_shutdown(wallet)          # the user confirmed the quit
        return await _accept_without_funding(sm_, **kw)

    sm.outcomes[A] = _fail_and_begin_closing
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert [n for n, _amt in sm.attempts] == [A]
    assert any(d.get("reason") == "swap cascade stopped for shutdown"
               for d in p.diags)


def test_shutdown_stops_the_cascade_before_a_reduced_rung() -> None:
    """A reduced retry is every bit as much "a new Lightning payment" as the next
    provider is, so the rung loop has to check too -- otherwise a shutdown landing
    on a payment failure would still fire one more payment."""
    sm = _SM({})
    p = _plugin()
    wallet = _Wallet(sm)

    async def _fail_and_begin_closing(sm_, **kw):
        p.begin_shutdown(wallet)
        return await _accept_without_funding(sm_, **kw)

    sm.outcomes[A] = _fail_and_begin_closing
    asyncio.run(p._reverse_swap(wallet, _action(alternates=(), reduced=200_000),
                                state={}, transport=_transport([A])))
    assert [amt for _n, amt in sm.attempts] == [400_000]   # no 200_000 rung


def test_shutdown_does_not_interrupt_the_attempt_already_running() -> None:
    """The safety line. An attempt whose Lightning payment may already be in
    flight runs to completion even though the wallet is closing -- and if it
    funds, it is recorded as the success it is."""
    sm = _SM({})
    p = _plugin()
    wallet = _Wallet(sm)

    async def _begin_closing_then_fund(sm_, **kw):
        p.begin_shutdown(wallet)
        await asyncio.sleep(0)            # a real await, mid-attempt
        return "txid-A"

    sm.outcomes[A] = _begin_closing_then_fund
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert p.successes == [A]
    assert p.dev_fees == [(399_500, "1x1x1")]
    assert len(p.logged) == 1


def test_a_cascade_never_starts_on_a_closing_wallet() -> None:
    """The check sits ahead of the per-channel cooldown deliberately. That
    cooldown is keyed by CHANNEL and survives a wallet reload inside the same
    process, so arming it for a cascade that then did nothing would make a
    reopened wallet skip that channel for three minutes for no reason."""
    sm = _SM({})
    p = _plugin()
    wallet = _Wallet(sm)
    p.begin_shutdown(wallet)
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert sm.attempts == []
    assert p._swap_cooldown_until == {}, \
        "a cascade that never ran still armed the channel's cooldown"


def test_the_sink_ladder_stops_for_shutdown() -> None:
    """The sink drains a channel by PAYING somebody, so a rung of its ladder is
    as much a live Lightning payment as a swap attempt is, and the same rule
    applies: finish the one in flight, start no more."""
    p = _plugin()
    wallet = _Wallet(_SM({}))
    p.begin_shutdown(wallet)
    paid: List[int] = []

    async def _never_called(*a, **kw):
        paid.append(1)
        raise AssertionError("a sink rung was attempted while closing")

    p._attempt_sink_payment = _never_called
    p._lnurl_pay_params = _stub_lnurl
    from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore
        LiquiditySinkAction)
    action = LiquiditySinkAction(
        channel_id="aa" * 32, short_id="1x1x1", address="me@example.com",
        amounts=(200_000, 100_000), reason="drain", swap_fallback=None)
    outcome = asyncio.run(p._pay_liquidity_sink(wallet, action, state={}))
    assert paid == []
    assert any(d.get("reason") == "liquidity sink ladder stopped for shutdown"
               for d in p.diags), f"no shutdown decline recorded: {p.diags}"
    assert outcome is not None


async def _stub_lnurl(address, *, purpose=""):
    return SimpleNamespace(min_sendable_sat=1_000, max_sendable_sat=1_000_000,
                           callback_url="https://example.com/cb")


def test_a_cascade_on_a_wallet_that_is_not_closing_is_untouched() -> None:
    """The gate must be inert in the ordinary case: no flag, no behaviour
    change."""
    sm = _SM({A: _accept_without_funding, B: _accept_without_funding})
    p = _plugin()
    wallet = _Wallet(sm)
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert [n for n, _amt in sm.attempts] == [A, B, C]
    assert p.successes == [C]


# --- the in-flight record, and what teardown waits on ---------------------
def test_an_attempt_publishes_itself_while_it_runs() -> None:
    """The record is what the closing warning reads and what the shutdown wait
    counts down, so it has to exist for the whole attempt and be gone after."""
    sm = _SM({})
    p = _plugin()
    wallet = _Wallet(sm)
    seen: List[Optional[_InflightAttempt]] = []

    async def _observe(sm_, **kw):
        seen.append(p.inflight_swap_attempt(wallet))
        return "txid-A"

    sm.outcomes[A] = _observe
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert seen and seen[0] is not None
    assert seen[0].short_id == "1x1x1"
    assert seen[0].amount_sat == 400_000
    assert seen[0].provider_npub == A
    assert p.inflight_swap_attempt(wallet) is None, "record outlived the attempt"


def test_the_record_is_retracted_even_when_the_attempt_raises() -> None:
    """A shutdown blocked on this event must be released by EVERY exit path, or
    the quit sits out the whole budget for an attempt that died instantly."""
    sm = _SM({A: Exception("our blockchain tip is stale")})
    p = _plugin()
    wallet = _Wallet(sm)
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert p.inflight_swap_attempt(wallet) is None
    assert p._attempt_done_event(wallet).is_set()


def test_wait_returns_at_once_when_nothing_is_in_flight() -> None:
    """The overwhelmingly common close. It must not cost the user a delay."""
    p = _plugin()
    wallet = _Wallet(_SM({}))
    started = time.monotonic()
    assert p.wait_for_swap_attempt(wallet) is True
    assert time.monotonic() - started < 0.5


def test_teardown_waits_for_the_attempt_then_proceeds() -> None:
    """stop_wallet blocks on the in-flight attempt and only then drops the
    wallet's state -- the ordering that lets Electrum's own `wallet.stop()`
    (and its final db write) run after the swap has recorded itself."""
    p = _plugin()
    wallet = _Wallet(_SM({}))
    p.wallets[wallet] = "lock"
    p._note_attempt_started(wallet, _InflightAttempt(
        short_id="1x1x1", provider_npub=A, amount_sat=400_000,
        started_at=time.monotonic(),
        deadline=time.monotonic() + 30.0))

    finished_at: List[float] = []

    def _finish_shortly() -> None:
        time.sleep(0.2)
        finished_at.append(time.monotonic())
        p._note_attempt_finished(wallet)

    threading.Thread(target=_finish_shortly, daemon=True).start()
    started = time.monotonic()
    p.stop_wallet(wallet)
    returned_at = time.monotonic()

    assert finished_at, "the attempt never finished"
    assert returned_at >= finished_at[0], "teardown returned before the attempt"
    assert 0.2 <= returned_at - started < 5.0
    assert wallet not in p.wallets
    assert wallet not in p._inflight_attempt


def test_teardown_gives_up_at_the_attempts_own_deadline() -> None:
    """Waiting is bounded by what the attempt itself has left to run, so a wedged
    swap cannot hold the quit open indefinitely. Expiry is not an error -- we save
    and exit, and the record is reconciled on the next load."""
    p = _plugin()
    wallet = _Wallet(_SM({}))
    p.wallets[wallet] = "lock"
    p._note_attempt_started(wallet, _InflightAttempt(
        short_id="1x1x1", provider_npub=A, amount_sat=400_000,
        started_at=time.monotonic(),
        deadline=time.monotonic() + 0.2))     # nothing ever sets the event

    started = time.monotonic()
    p.stop_wallet(wallet)
    elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 3.0, f"teardown took {elapsed:.2f}s"
    assert wallet not in p.wallets


def test_the_stopping_flag_survives_teardown_but_not_a_reload() -> None:
    """It must outlive _forget_wallet: an evaluation can still be mid-cascade when
    teardown returns (we waited out the ATTEMPT, not the cascade), and clearing
    the flag there would let its next check read "not stopping" and start another
    provider on a wallet we have just dismantled. start_wallet is what retracts
    it, i.e. when the wallet is genuinely live again."""
    p = _plugin()
    wallet = _Wallet(_SM({}))
    p.wallets[wallet] = "lock"
    p.stop_wallet(wallet)
    assert p._shutting_down(wallet)

    # A reload clears it. start_wallet does much more than this, so drive just the
    # retraction the way it does.
    p._stopping.discard(wallet)
    assert not p._shutting_down(wallet)


def test_shutting_down_is_false_on_a_partially_built_plugin() -> None:
    """The glue harnesses build plugins via object.__new__; an absent store must
    read as "not stopping" rather than raising into the middle of a cascade."""
    p = object.__new__(LiquidityPlugin)
    assert p._shutting_down(object()) is False


# --- durability: a created swap reaches disk ------------------------------
def test_a_created_swap_flushes_the_wallet_file() -> None:
    """add_reverse_swap writes the privkey, preimage and redeem script into a
    StoredDict, which only reaches disk on an explicit db.write(). The last one
    Electrum performs is inside wallet.stop(), so a swap created after that point
    would exist only in memory -- and its on-chain output could never be claimed.
    """
    p = _plugin()
    sm = _SM({})
    wallet = _Wallet(sm)
    before = set(sm._swaps)
    sm._swaps["aa" + "ff" * 31] = SimpleNamespace(prepay_hash=None)
    assert p._flush_new_swaps(wallet, sm, before) is True
    assert wallet.saves == 1


def test_no_swap_means_no_flush() -> None:
    """An attempt that failed before add_reverse_swap created nothing, so there is
    nothing to make durable and no reason to write the wallet file."""
    p = _plugin()
    sm = _SM({})
    wallet = _Wallet(sm)
    assert p._flush_new_swaps(wallet, sm, set(sm._swaps)) is False
    assert wallet.saves == 0


def test_a_failing_flush_never_sinks_the_swap() -> None:
    """Persistence is best-effort everywhere in this plugin, and most of all
    here: a db write that fails must not take down the swap it exists to
    protect."""
    p = _plugin()
    sm = _SM({})
    wallet = _Wallet(sm)
    wallet.save_db = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
    sm._swaps["aa" + "ff" * 31] = SimpleNamespace(prepay_hash=None)
    assert p._flush_new_swaps(wallet, sm, set()) is False


def test_the_cascade_flushes_after_creating_a_swap() -> None:
    """End to end: a real (failing) attempt through the cascade leaves the wallet
    file written."""
    sm = _SM({A: _accept_without_funding, B: _accept_without_funding,
              C: _accept_without_funding})
    p = _plugin()
    wallet = _Wallet(sm)
    asyncio.run(p._reverse_swap(wallet, _action(), state={},
                                transport=_transport([A, B, C])))
    assert wallet.saves >= 3, "each created swap should have been flushed"


def test_every_flush_lands_before_teardown_is_released() -> None:
    """The ordering the whole handshake exists to create.

    Teardown is released by the attempt's ``finally``, and Electrum writes the
    wallet file shortly after that. So everything fund-critical -- the swap's
    claim secrets, and the pending-swap record that reconciles it -- has to be
    flushed BEFORE the release, or a close could still land between the two and
    lose exactly what the handshake was protecting.

    Asserted by watching when each save happens relative to the done-event, which
    is the signal teardown actually waits on.
    """
    sm = _SM({A: _accept_without_funding})
    p = _plugin()
    wallet = _Wallet(sm)
    # Track the real pending-swap write too, not just the flush: both are
    # fund-critical and both must precede the release.
    p._track_new_swaps = lambda w, sm_, before, npub, action, exp: (
        w.save_db() or (set(sm_._swaps) - before))
    saves_after_release: List[int] = []
    real_save = wallet.save_db

    def _watched_save() -> None:
        event = p._attempt_done_event(wallet)
        if event is not None and event.is_set():
            saves_after_release.append(wallet.saves)
        real_save()

    wallet.save_db = _watched_save
    asyncio.run(p._reverse_swap(wallet, _action(alternates=()), state={},
                                transport=_transport([A])))
    assert wallet.saves >= 2, "neither the flush nor the tracking write happened"
    assert saves_after_release == [], \
        (f"{len(saves_after_release)} fund-critical write(s) happened after "
         f"teardown had already been released")
