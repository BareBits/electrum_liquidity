"""Glue-level tests for the tick status shown in the Settings tab.

An evaluation can run for minutes, so the plugin publishes what step it is on.
The properties worth pinning down are the ones a status line lives or dies by:

  * every exit path -- each gate's early return, an exception, cancellation --
    lands on a TERMINAL state, so the label can never stick on "opening
    channel…" forever;
  * the terminal state names the right reason (disabled / manual-only /
    settling / sleeping), because "sleeping" on a switched-off plugin is a lie;
  * the in-flight steps name the concrete peer/provider being tried;
  * a status update can never break the action it describes.

Heavy Electrum objects are faked; skipped outside the electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    STATUS_DISABLED,
    STATUS_MANUAL_ONLY,
    STATUS_NOT_STARTED,
    STATUS_SETTLING,
    STATUS_SLEEPING,
    TERMINAL_STATUSES,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    OpenChannelAction,
    ReverseSwapAction,
)

PUB_A = "02" + "aa" * 32
PUB_B = "03" + "bb" * 32


class _FakeWallet:
    def __init__(self, *, connected: bool = True) -> None:
        self.network = SimpleNamespace(asyncio_loop=None,
                                       is_connected=lambda: connected)

    def basename(self) -> str:
        return "test-wallet"


def _plugin(**config) -> LiquidityPlugin:
    """A plugin wired with just enough state for _evaluate + _set_status."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.status")
    p._tick_status = {}
    p.wallets = {}
    p.seen: List[str] = []                       # every status, in order
    p.on_status_changed = lambda wallet, status: p.seen.append(status)
    p._started_at = {}
    p._startup_grace_sec = 120.0
    cfg = dict(INBOUND_LIQUIDITY_MANUAL_RUN_ONLY=False)
    cfg.update(config)
    p.config = SimpleNamespace(**cfg)
    return p


def _wire_tick(p, *, automation: bool = True) -> None:
    """Stub everything _evaluate calls so only the status plumbing is under test."""
    p._enforce_min_funding_floor = lambda: 0
    p.read_config = lambda: SimpleNamespace(automation_enabled=automation)
    p._reconcile_pending_swaps = lambda wallet: None
    p._maybe_pay_dev_fee = lambda wallet: None
    p._scan_channel_health = lambda wallet: None
    p._scan_offline_autoclose = lambda wallet: None
    p.build_snapshot = lambda wallet, transport=None: SimpleNamespace()
    p._swap_may_be_needed = lambda base, config: False

    async def _run_decision(wallet, snapshot, config, transport):
        return None
    p._run_decision = _run_decision


def _ready(p, wallet) -> None:
    p._wallet_ready = lambda w, **kw: True
    p._started_at[wallet] = 0.0


# --- initial state --------------------------------------------------------
def test_status_starts_as_not_started() -> None:
    p, w = _plugin(), _FakeWallet()
    assert p.tick_status(w) == STATUS_NOT_STARTED
    assert STATUS_NOT_STARTED in TERMINAL_STATUSES


def test_tick_status_tolerates_a_partially_constructed_plugin() -> None:
    # The glue test harnesses build a plugin without BasePlugin.__init__; a
    # status read/write must degrade quietly rather than AttributeError into the
    # middle of an action executor.
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.status")
    w = _FakeWallet()
    assert p.tick_status(w) == STATUS_NOT_STARTED
    p._set_status(w, "opening channel")          # must not raise
    assert p.tick_status(w) == STATUS_NOT_STARTED


# --- terminal states on every exit path -----------------------------------
def test_full_tick_walks_the_steps_and_ends_sleeping() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    _ready(p, w)

    asyncio.run(p._evaluate(w))

    assert p.seen == [
        "reconciling pending swaps",
        "checking channel health",
        "checking for offline peers",
        "reading wallet state",
        STATUS_SLEEPING,
    ]
    assert p.tick_status(w) == STATUS_SLEEPING


def test_disabled_automation_ends_disabled_not_sleeping() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p, automation=False)
    _ready(p, w)

    asyncio.run(p._evaluate(w))
    assert p.tick_status(w) == STATUS_DISABLED


def test_manual_run_only_ends_manual_only() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_MANUAL_RUN_ONLY=True), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    _ready(p, w)

    asyncio.run(p._evaluate(w))                  # automatic trigger -> gated
    assert p.tick_status(w) == STATUS_MANUAL_ONLY

    # An explicit "Run now" bypasses the gate, so it runs and ends sleeping.
    asyncio.run(p._evaluate(w, manual=True))
    assert p.tick_status(w) == STATUS_SLEEPING


def test_not_ready_wallet_ends_warming_up() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    p._wallet_ready = lambda wallet, **kw: False

    asyncio.run(p._evaluate(w))
    assert p.tick_status(w) == STATUS_SETTLING


def test_manual_run_during_warm_up_does_not_end_warming_up() -> None:
    """A "Run now" that was deliberately let past the peer/time limb must not
    come to rest on the very state it skipped -- that read as "the button did
    nothing"."""
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    p._started_at[w] = 0.0
    ran: List[bool] = []
    p._scan_channel_health = lambda wallet: ran.append(True)
    # Still inside the startup window: only a manual run is let through. Both the
    # gate _evaluate consults and the one _terminal_status re-checks are stubbed
    # so they agree.
    p._readiness_block = lambda wallet, *, manual=False: None if manual else "still connecting to Lightning peers"
    p._wallet_ready = lambda wallet, *, manual=False: manual

    asyncio.run(p._evaluate(w))
    assert ran == []                                   # automatic tick deferred
    assert p.tick_status(w) == STATUS_SETTLING
    # ...but the manual run acts and rests on "sleeping".
    asyncio.run(p._evaluate(w, manual=True))
    assert ran == [True]
    assert p.tick_status(w) == STATUS_SLEEPING


def test_status_wording_is_not_about_funds_settling() -> None:
    # The reported confusion: the old "waiting for wallet to settle" read as
    # "your coins are unconfirmed". Readiness has nothing to do with funds.
    assert "settle" not in STATUS_SETTLING.lower()
    assert STATUS_SETTLING in TERMINAL_STATUSES


def test_an_exception_mid_tick_still_ends_terminal() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    _ready(p, w)

    def _boom(wallet) -> None:
        raise RuntimeError("watchdog exploded")
    p._scan_channel_health = _boom
    p._diag_event = lambda wallet, **kw: None

    asyncio.run(p._evaluate(w))                  # _evaluate swallows the error
    assert p.tick_status(w) == STATUS_SLEEPING
    assert p.seen[-1] in TERMINAL_STATUSES


def test_cancellation_mid_tick_still_ends_terminal() -> None:
    # Shutdown cancels the heartbeat task mid-tick; the status must not be left
    # parked on whatever step was running.
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    _ready(p, w)

    def _cancel(wallet) -> None:
        raise asyncio.CancelledError()
    p._scan_channel_health = _cancel

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p._evaluate(w))
    assert p.tick_status(w) in TERMINAL_STATUSES


# --- notification robustness ---------------------------------------------
def test_a_failing_gui_notification_does_not_break_the_tick() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    _wire_tick(p)
    _ready(p, w)

    def _explode(wallet, status) -> None:
        raise RuntimeError("widget deleted")
    p.on_status_changed = _explode

    asyncio.run(p._evaluate(w))                  # must complete regardless
    assert p.tick_status(w) == STATUS_SLEEPING


def test_repeated_identical_status_is_not_re_notified() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    p._set_status(w, "opening channel")
    p._set_status(w, "opening channel")
    assert p.seen == ["opening channel"]


def test_status_is_not_stored_for_an_unmanaged_wallet() -> None:
    # A step landing after stop_wallet must not resurrect per-wallet state we
    # just dropped (it would leak a dict entry per closed wallet).
    p, w = _plugin(), _FakeWallet()
    p._set_status(w, "opening channel")
    assert w not in p._tick_status
    assert p.tick_status(w) == STATUS_NOT_STARTED


# --- per-candidate detail -------------------------------------------------
def test_open_channel_names_each_partner_it_tries() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    p._get_password = lambda wallet: None
    p._record_peer_fault = lambda wallet, node_id, reason, *, hard: None
    p._max_funding_minus_reserve = lambda wallet, node_id: None    # stop after connect

    async def _add_peer(connect_str):
        if connect_str == PUB_A:
            raise ConnectionError("unreachable")
        return SimpleNamespace(pubkey=bytes.fromhex(PUB_B))
    w.lnworker = SimpleNamespace(
        lnpeermgr=SimpleNamespace(add_peer=_add_peer), open_channel_with_peer=None)

    action = OpenChannelAction(funding_sat=1_000_000, reason="grow")
    asyncio.run(p._open_channel(w, action, state={}, candidates=[PUB_A, PUB_B]))

    # Both candidates are named, with their position in the try-order, so a tick
    # that stalls on peer 2 of 4 says exactly that.
    assert p.seen[0].startswith("connecting to partner ") and "(1 of 2)" in p.seen[0]
    assert "(2 of 2)" in p.seen[1]
    assert "02aaaa" in p.seen[0]                  # abbreviated, not the raw 66 chars
    assert len(p.seen[0]) < 80


def test_open_channel_status_names_the_amount() -> None:
    p, w = _plugin(), _FakeWallet()
    p.wallets[w] = asyncio.Lock()
    p._get_password = lambda wallet: None
    p._max_funding_minus_reserve = lambda wallet, node_id: 1_000_000
    p._record_peer_fault = lambda wallet, node_id, reason, *, hard: None
    p._diag_event = lambda wallet, **kw: None

    async def _add_peer(connect_str):
        return SimpleNamespace(pubkey=bytes.fromhex(PUB_A))

    async def _open(peer, funding_sat, push_sat, password):
        raise RuntimeError("peer said no")
    w.lnworker = SimpleNamespace(
        lnpeermgr=SimpleNamespace(add_peer=_add_peer), open_channel_with_peer=_open)

    asyncio.run(p._open_channel(w, OpenChannelAction(funding_sat=1_000_000, reason="grow"),
                                state={}, candidates=[PUB_A]))
    assert any(s.startswith("opening channel with ") and "1,000,000 sat" in s
               for s in p.seen)
