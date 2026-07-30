"""Glue-level tests for the startup/shutdown race guard (the Electrum-facing
layer): the all-automation deferral in ``_evaluate`` until the wallet has
settled (``_wallet_ready``), and the per-peer observation gate (``_observe_peer``)
that keeps a not-yet-connected peer at startup -- or a torn-down connection at
shutdown -- from being faulted or poisoning the offline-autoclose uptime metric.

A controllable clock drives the grace window; heavy Electrum objects are faked.
Skipped outside the electrum venv."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    CHANNEL_UPTIME_DB_KEY,
    PEER_RELIABILITY_DB_KEY,
    LiquidityPlugin,
)

NODE_A = "02" + "aa" * 32
CID = "aa" * 32
GRACE = 120.0
DAY = 86400.0


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    import electrum.plugins.inbound_liquidity as mod
    c = _Clock()
    monkeypatch.setattr(mod.time, "time", c)
    return c


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, lnworker=None, *, connected: bool = True,
                 synced: bool = True) -> None:
        self.db = _FakeDB()
        self.saved = 0
        self.lnworker = lnworker
        # ``connected`` / ``synced`` are mutable boxes so a test can flip them
        # (shutdown, mid-sync) between scans without rebuilding the wallet.
        self._connected = connected
        self._synced = synced
        self.network = SimpleNamespace(
            asyncio_loop=None, is_connected=lambda: self._connected)

    def is_up_to_date(self) -> bool:
        return self._synced

    def basename(self) -> str:
        return "test-wallet"

    def save_db(self) -> None:
        self.saved += 1


class _Chan:
    def __init__(self, state, *, node_id=NODE_A, cid=CID, active=False) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = state
        self.active = active
        self.unconfirmed_closing_txid = None
        self.storage = SimpleNamespace(get=lambda k, d=None: d)

    def get_state(self):
        return self.state

    def is_active(self):
        return self.active


def _lnworker_with(channels):
    forced: List[bytes] = []
    ln = SimpleNamespace(
        channels={c.channel_id: c for c in channels},
        schedule_force_closing=lambda cid: forced.append(cid),
    )
    return ln, forced


def _plugin(clock: Optional[_Clock] = None, *, grace: float = GRACE,
            **overrides) -> LiquidityPlugin:
    """A plugin with every state dict the two watchdogs + the readiness gate
    touch, and a real (production) startup grace so the guard is exercised."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.readiness")
    # readiness / observation state
    p._started_at = {}
    p._peer_seen_online = {}
    p._startup_grace_sec = grace
    # health-watchdog state
    p._remediating_opens = {}
    p._local_closes = {}
    p._wedged_faulted = {}
    p._close_capped_logged = {}
    p._known_chan_states = {}
    p._last_decline_sigs = {}
    # offline-autoclose state
    p._coop_closing = {}
    p._coop_close_cooldown_until = {}
    cfg = dict(
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED=True,
        INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=3,
        INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN=60,
        INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN=True,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
        INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED=True,
        INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS=2.0,
        INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT=10.0,
        INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS=7.0,
    )
    cfg.update(overrides)
    p.config = SimpleNamespace(**cfg)
    return p


def _open(name: str):
    from electrum.lnchannel import ChannelState
    return getattr(ChannelState, name)


def _mark_started(p, w, clock) -> None:
    """Simulate start_wallet at the current clock instant."""
    p._started_at[w] = clock.t
    p._peer_seen_online.setdefault(w, set())


# --- _wallet_ready --------------------------------------------------------
def test_wallet_ready_requires_connection_sync_and_the_window(clock) -> None:
    ln, _ = _lnworker_with([])
    p, w = _plugin(clock), _FakeWallet(ln)
    # Not managed yet (no recorded load time) -> never ready.
    assert p._wallet_ready(w) is False
    _mark_started(p, w, clock)
    assert p._wallet_ready(w) is False            # inside the startup window
    clock.advance(GRACE + 1)
    assert p._wallet_ready(w) is True             # window elapsed
    w._connected = False
    assert p._wallet_ready(w) is False            # shutdown: connection gone
    w._connected, w._synced = True, False
    assert p._wallet_ready(w) is False            # still catching up with server
    w._synced = True
    assert p._wallet_ready(w) is True


def test_wallet_ready_manual_bypasses_the_window_only(clock) -> None:
    """The reported bug. A manual run skips the startup window, but a
    disconnected or still-syncing wallet is refused either way."""
    ln, _ = _lnworker_with([_Chan(_open("OPEN"), active=False)])
    p, w = _plugin(clock), _FakeWallet(ln)
    _mark_started(p, w, clock)
    assert p._wallet_ready(w) is False                    # automatic: defers
    assert p._wallet_ready(w, manual=True) is True        # "Run now": proceeds
    w._connected = False
    assert p._wallet_ready(w, manual=True) is False
    w._connected, w._synced = True, False
    assert p._wallet_ready(w, manual=True) is False


def test_readiness_block_names_the_reason(clock) -> None:
    from electrum.plugins.inbound_liquidity import BLOCK_NOT_MANAGED  # type: ignore
    from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore
        BLOCK_NO_CONNECTION, BLOCK_STARTING_UP, BLOCK_SYNCING,
    )

    ln, _ = _lnworker_with([])
    p, w = _plugin(clock), _FakeWallet(ln)
    assert p._readiness_block(w) == BLOCK_NOT_MANAGED
    _mark_started(p, w, clock)
    assert p._readiness_block(w) == BLOCK_STARTING_UP
    w._synced = False
    assert p._readiness_block(w) == BLOCK_SYNCING
    w._connected = False
    assert p._readiness_block(w) == BLOCK_NO_CONNECTION


def test_wallet_synced_defaults_true_when_wallet_cannot_answer(clock) -> None:
    # A wallet with no is_up_to_date, or one that raises, must not stall the
    # plugin forever -- the old behaviour never consulted sync state at all.
    p = _plugin(clock)
    assert p._wallet_synced(SimpleNamespace()) is True

    def _boom() -> bool:
        raise RuntimeError("nope")

    assert p._wallet_synced(SimpleNamespace(is_up_to_date=_boom)) is True


def test_evaluate_defers_all_automation_until_ready(clock) -> None:
    ln, _ = _lnworker_with([])
    p, w = _plugin(clock), _FakeWallet(ln)
    p.wallets = {w: asyncio.Lock()}
    called: List[str] = []
    p.read_config = lambda: SimpleNamespace(automation_enabled=True)
    p._reconcile_pending_swaps = lambda wal: called.append("reconcile")
    p._scan_channel_health = lambda wal: called.append("health")
    p._scan_offline_autoclose = lambda wal: called.append("autoclose")
    p.build_snapshot = lambda wal, t=None: called.append("snapshot") or SimpleNamespace()
    p._swap_may_be_needed = lambda base, config: False
    async def _run_decision(wal, snap, config, transport):
        called.append("decision")
    p._run_decision = _run_decision

    _mark_started(p, w, clock)
    asyncio.run(p._evaluate(w))                   # inside the window -> deferred
    assert called == []

    clock.advance(GRACE + 1)                      # window elapsed -> runs
    asyncio.run(p._evaluate(w))
    assert "health" in called and "autoclose" in called


def test_evaluate_manual_run_acts_inside_the_startup_window(clock) -> None:
    """The reported bug: "Run now" during the startup window logged "waiting for
    wallet to settle" and did nothing. A manual run must act."""
    ln, _ = _lnworker_with([])
    p, w = _plugin(clock), _FakeWallet(ln)
    p.wallets = {w: asyncio.Lock()}
    called: List[str] = []
    p.read_config = lambda: SimpleNamespace(automation_enabled=True)
    p._reconcile_pending_swaps = lambda wal: called.append("reconcile")
    p._scan_channel_health = lambda wal: called.append("health")
    p._scan_offline_autoclose = lambda wal: called.append("autoclose")
    p.build_snapshot = lambda wal, t=None: SimpleNamespace()
    p._swap_may_be_needed = lambda base, config: False
    # Manual-run-only mode on, to prove both gates are bypassed together.
    p.config.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY = True
    async def _run_decision(wal, snap, config, transport):
        called.append("decision")
    p._run_decision = _run_decision

    _mark_started(p, w, clock)
    asyncio.run(p._evaluate(w))                            # automatic: nothing
    assert called == []
    asyncio.run(p._evaluate(w, manual=True))               # "Run now": acts
    assert "decision" in called


def test_evaluate_manual_run_still_refused_when_disconnected(clock) -> None:
    ln, _ = _lnworker_with([])
    p, w = _plugin(clock), _FakeWallet(ln, connected=False)
    p.wallets = {w: asyncio.Lock()}
    called: List[str] = []
    p.read_config = lambda: SimpleNamespace(automation_enabled=True)
    p._scan_channel_health = lambda wal: called.append("health")
    _mark_started(p, w, clock)
    asyncio.run(p._evaluate(w, manual=True))
    assert called == []


# --- _observe_peer matrix -------------------------------------------------
def test_observe_peer_startup_then_settle(clock) -> None:
    p, w = _plugin(clock), _FakeWallet(_lnworker_with([])[0])
    _mark_started(p, w, clock)
    chan = _Chan(_open("OPEN"), active=False)
    # Startup: peer not dialed yet, inside grace -> not observed (None).
    assert p._observe_peer(w, chan, clock.t) is None
    # Past the grace, still inactive -> genuine offline.
    clock.advance(GRACE + 1)
    assert p._observe_peer(w, chan, clock.t) is False


def test_observe_peer_active_is_remembered_then_offline_within_grace(clock) -> None:
    p, w = _plugin(clock), _FakeWallet(_lnworker_with([])[0])
    _mark_started(p, w, clock)
    chan = _Chan(_open("OPEN"), active=True)
    assert p._observe_peer(w, chan, clock.t) is True        # online, remembered
    assert NODE_A.lower() in p._peer_seen_online[w]
    # It drops while still inside the grace -> a real outage (seen before), False.
    chan.active = False
    assert p._observe_peer(w, chan, clock.t) is False


def test_observe_peer_network_down_is_not_observed(clock) -> None:
    p, w = _plugin(clock), _FakeWallet(_lnworker_with([])[0])
    _mark_started(p, w, clock)
    clock.advance(GRACE + 1)                                 # well past grace
    chan = _Chan(_open("OPEN"), active=False)
    w._connected = False                                     # shutdown / no server
    assert p._observe_peer(w, chan, clock.t) is None


# --- integration: offline hard-fault (peer-reliability surface) -----------
def test_offline_fault_suppressed_during_startup_then_fires(clock) -> None:
    chan = _Chan(_open("OPEN"), active=False)
    ln, _ = _lnworker_with([chan])
    p, w = _plugin(clock), _FakeWallet(ln)
    _mark_started(p, w, clock)
    p._scan_channel_health(w)                                # within grace
    assert p._load_peer_reliability(w) == {}                 # no "peer offline" fault
    clock.advance(GRACE + 1)
    p._scan_channel_health(w)                                # settled + still offline
    s = p._load_peer_reliability(w).get(NODE_A.lower())
    assert s is not None and s["hard_fault_count"] == 1
    assert s["last_reason"] == "peer offline"


def test_offline_fault_fires_for_seen_peer_within_grace(clock) -> None:
    chan = _Chan(_open("OPEN"), active=True)
    ln, _ = _lnworker_with([chan])
    p, w = _plugin(clock), _FakeWallet(ln)
    _mark_started(p, w, clock)
    p._scan_channel_health(w)                                # peer online -> remembered
    assert p._load_peer_reliability(w) == {}
    chan.active = False                                      # real drop, still in grace
    p._scan_channel_health(w)
    s = p._load_peer_reliability(w).get(NODE_A.lower())
    assert s is not None and s["hard_fault_count"] == 1


# --- integration: uptime sampling (offline-autoclose surface) -------------
def test_uptime_not_sampled_during_startup(clock) -> None:
    chan = _Chan(_open("OPEN"), active=False)
    ln, _ = _lnworker_with([chan])
    p, w = _plugin(clock), _FakeWallet(ln)
    _mark_started(p, w, clock)
    p._tag_plugin_opened_channel(w, CID)
    # A burst of startup ticks with the peer "offline" must NOT poison the metric.
    for _ in range(5):
        p._scan_offline_autoclose(w)
        clock.advance(10.0)
    assert w.db.get(CHANNEL_UPTIME_DB_KEY, {}) == {}
    # Once settled, a real offline reading IS sampled.
    clock.advance(GRACE + 1)
    p._scan_offline_autoclose(w)
    clock.advance(10.0)
    p._scan_offline_autoclose(w)
    assert CID in w.db.get(CHANNEL_UPTIME_DB_KEY, {})


def test_uptime_not_sampled_when_network_down(clock) -> None:
    chan = _Chan(_open("OPEN"), active=False)
    ln, _ = _lnworker_with([chan])
    p, w = _plugin(clock), _FakeWallet(ln, connected=False)
    _mark_started(p, w, clock)
    p._tag_plugin_opened_channel(w, CID)
    clock.advance(GRACE + 1)                                 # past grace, but no server
    for _ in range(4):
        p._scan_offline_autoclose(w)
        clock.advance(10.0)
    assert w.db.get(CHANNEL_UPTIME_DB_KEY, {}) == {}
