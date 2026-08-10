"""Glue-level tests for the offline-channel auto-close watchdog (the
Electrum-facing layer): plugin-opened tagging (scope), the persisted peer-uptime
sampling, committing to a close when uptime drops below the floor, cancelling it
when the peer recovers, attempting a cooperative close when the peer is reachable,
escalating to a force-close after the deadline, and honouring the daily
close-ceiling. A controllable clock drives the time-based logic; heavy Electrum
objects are faked. Skipped outside the electrum venv."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    CHANNEL_UPTIME_DB_KEY,
    CLOSE_INTENT_DB_KEY,
    PLUGIN_OPENED_CHANNELS_DB_KEY,
    LiquidityPlugin,
)

NODE_A = "02" + "aa" * 32
CID = "aa" * 32
CID2 = "bb" * 32

# Tiny thresholds so the time-based logic runs in a handful of clock steps.
WINDOW_SEC = 200.0
# Default deadline is large so commit/cooperative/cancel tests never trip the
# force-close by accident; the force-close tests override it small and advance
# the clock explicitly past it.
FORCE_SEC_DEFAULT = 10 * 86400.0
FORCE_SEC = 100.0
DAY = 86400.0


class _Clock:
    """A mutable fake wall clock, installed over ``time.time`` in the plugin
    module for the duration of a test."""

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
    def __init__(self, lnworker=None) -> None:
        self.db = _FakeDB()
        self.saved = 0
        self.lnworker = lnworker
        # A settled, connected wallet: these watchdog tests exercise behavior
        # AFTER the startup-race guard has passed (see _observe_peer / grace=0.0
        # in _plugin), so the network reads connected.
        self.network = SimpleNamespace(asyncio_loop=None, is_connected=lambda: True)

    def save_db(self) -> None:
        self.saved += 1


class _Chan:
    """Fake channel.

    ``active`` (``is_active()``) drives the UPTIME metric, while ``reachable``
    (``peer_state`` / the peer manager) drives whether a cooperative close can
    be attempted. They usually coincide and ``reachable`` defaults to ``active``,
    but they are genuinely separate signals in Electrum -- ``is_active()`` is
    hardcoded to ``state == OPEN``, so it cannot express reachability for the
    pre-OPEN channels the wedged-open path closes. Tests that need to make the
    peer talkable without also feeding the uptime metric set them apart.
    """

    def __init__(self, state, *, node_id=NODE_A, cid=CID, active=False,
                 reachable=None) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = state
        self.active = active
        self.reachable = active if reachable is None else reachable
        self.funding_outpoint = SimpleNamespace(txid=cid)
        self.lnworker = None            # set by _lnworker_with

    def get_state(self):
        return self.state

    def is_active(self):
        return self.active

    def funding_txn_minimum_depth(self):
        return 3

    @property
    def peer_state(self):
        from electrum.lnchannel import PeerState
        return PeerState.GOOD if self.reachable else PeerState.DISCONNECTED


def _lnworker_with(channels):
    forced: List[bytes] = []
    coop: List[bytes] = []

    async def _close(cid):
        coop.append(cid)
        return "coop-txid"

    adb = SimpleNamespace(get_tx_height=lambda txid: SimpleNamespace(conf=6))
    ln = SimpleNamespace(
        channels={c.channel_id: c for c in channels},
        schedule_force_closing=lambda cid: forced.append(cid),
        close_channel=_close,
        lnwatcher=SimpleNamespace(adb=adb),
        lnpeermgr=SimpleNamespace(
            get_peer_by_pubkey=lambda pk: next(
                (object() for c in channels
                 if c.node_id == pk and c.reachable), None)),
    )
    for c in channels:
        c.lnworker = ln
    return ln, forced, coop


def _plugin(**overrides) -> LiquidityPlugin:
    import logging
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.autoclose")
    p._coop_closing = {}
    p._coop_close_cooldown_until = {}
    p._close_capped_logged = {}
    p._last_decline_sigs = {}
    # Startup-race guard state: grace=0.0 means the wallet is treated as fully
    # settled, so a not-connected peer is a genuine offline (these tests predate
    # the guard and assert that post-settle behavior).
    p._started_at = {}
    p._peer_seen_online = {}
    p._startup_grace_sec = 0.0
    cfg = dict(
        INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED=True,
        INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS=WINDOW_SEC / DAY,
        INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT=10.0,
        INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS=FORCE_SEC_DEFAULT / DAY,
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
    )
    cfg.update(overrides)
    p.config = SimpleNamespace(**cfg)
    return p


def _tag(p, w, *cids) -> None:
    for cid in cids:
        p._tag_plugin_opened_channel(w, cid)


def _drive_to_commit(p, w, chan, *, clock, max_steps=8, dt=60.0) -> bool:
    """Sample the (offline) channel, advancing the clock between scans, and stop
    the moment a close is committed -- so we don't overshoot into the force-close
    deadline. Returns whether it committed."""
    for _ in range(max_steps):
        p._scan_offline_autoclose(w)
        if CID in w.db.get(CLOSE_INTENT_DB_KEY, {}):
            return True
        clock.advance(dt)
    return CID in w.db.get(CLOSE_INTENT_DB_KEY, {})


def _scan_n(p, w, *, clock, n=6, dt=60.0) -> None:
    """Run ``n`` watchdog scans advancing the clock between them (for cases that
    should NOT commit)."""
    for _ in range(n):
        p._scan_offline_autoclose(w)
        clock.advance(dt)


# --- tagging / scope ------------------------------------------------------
def test_tag_and_scope_only_plugin_opened(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    # NOT tagged -> watchdog ignores it entirely (no uptime, no intent).
    _scan_n(p, w, clock=clock)
    assert w.db.get(CHANNEL_UPTIME_DB_KEY, {}) == {}
    assert w.db.get(CLOSE_INTENT_DB_KEY, {}) == {}
    assert forced == []


def test_tag_round_trips_and_dedupes(clock) -> None:
    p, w = _plugin(), _FakeWallet()
    p._tag_plugin_opened_channel(w, CID)
    p._tag_plugin_opened_channel(w, CID)  # idempotent
    assert w.db.get(PLUGIN_OPENED_CHANNELS_DB_KEY) == [CID]


# --- commit ---------------------------------------------------------------
def test_offline_channel_commits_to_close(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)
    intents = w.db.get(CLOSE_INTENT_DB_KEY, {})
    assert CID in intents
    assert "uptime" in intents[CID]["reason"]
    # Not yet past the force-close deadline -> no force-close, just committed.
    assert forced == []


def test_healthy_channel_never_commits(clock) -> None:
    chan = _Chan(_state("OPEN"), active=True)          # peer reachable throughout
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag(p, w, CID)
    _scan_n(p, w, clock=clock, n=6)
    assert w.db.get(CLOSE_INTENT_DB_KEY, {}) == {}
    assert forced == []


# --- cancel on recovery ---------------------------------------------------
def test_recovered_peer_cancels_pending_close(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    # Stub the cooperative close so a mid-recovery reachable peer doesn't schedule.
    p._maybe_cooperative_close = lambda *a, **k: None
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)
    assert CID in w.db.get(CLOSE_INTENT_DB_KEY, {})
    # Peer comes back and stays up long enough for uptime to climb over the floor.
    chan.active = True
    for _ in range(8):
        p._scan_offline_autoclose(w)
        clock.advance(60.0)
    assert CID not in w.db.get(CLOSE_INTENT_DB_KEY, {})
    assert forced == []


# --- force-close escalation ----------------------------------------------
# Reaching the deadline no longer force-closes on the spot: the close is handed
# to the single close path, which force-closes only once a cooperative close has
# been attempted or shown to be impossible (COOP_BEFORE_FORCE_WINDOW_SEC).
COOP_WINDOW = pkg.COOP_BEFORE_FORCE_WINDOW_SEC


def test_force_close_after_deadline(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS=FORCE_SEC / DAY), _FakeWallet(ln)
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)     # commit
    assert CID in w.db.get(CLOSE_INTENT_DB_KEY, {})
    clock.advance(FORCE_SEC + 1)                          # past the deadline
    p._scan_offline_autoclose(w)
    # The peer is unreachable, so cooperation is impossible -- but it still gets
    # its window to come back before we spend a force-close.
    assert forced == []
    clock.advance(COOP_WINDOW + 1)
    p._scan_offline_autoclose(w)
    assert forced == [bytes.fromhex(CID)]
    # It counted against the daily close ceiling and logged a close action.
    assert p._count_actions_last_24h(w, "close") == 1
    closes = [e for e in p.get_decision_log(w, "action") if e.get("kind") == "close"]
    assert any("force-closed offline" in (e.get("reason") or "") for e in closes)
    # The log says why a force-close was justified rather than a cooperative one.
    assert any("never reachable" in (e.get("detail") or "") for e in closes)


def test_force_close_deferred_when_ceiling_reached(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=1,
                   INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS=FORCE_SEC / DAY), _FakeWallet(ln)
    _tag(p, w, CID)
    p._record_action_event(w, "close")                   # ceiling (1) already used
    assert _drive_to_commit(p, w, chan, clock=clock)
    clock.advance(FORCE_SEC + 1)
    p._scan_offline_autoclose(w)
    clock.advance(COOP_WINDOW + 1)
    p._scan_offline_autoclose(w)
    assert forced == []                                  # deferred by the cap
    # Raising the ceiling releases it.
    p.config.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY = 5
    p._scan_offline_autoclose(w)
    assert forced == [bytes.fromhex(CID)]


# --- cooperative-first ----------------------------------------------------
def test_cooperative_close_attempted_when_peer_reachable(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    calls: List[str] = []
    p._maybe_cooperative_close = lambda wal, ch, cid, nid, now, **kw: calls.append(cid) or True
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)     # commit (ratio ~0%)
    assert CID in w.db.get(CLOSE_INTENT_DB_KEY, {})
    # Peer briefly reachable, still before the deadline and before uptime recovers.
    chan.active = True
    p._scan_offline_autoclose(w)
    assert calls == [CID]                                # cooperative close attempted
    assert forced == []


def test_reachable_peer_gets_coop_close_before_force_at_deadline(clock) -> None:
    """The invariant: even past the force-close deadline, a peer we can talk to
    gets a cooperative close attempt first -- a force-close is never the plugin's
    opening move."""
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS=FORCE_SEC / DAY), _FakeWallet(ln)
    calls: List[str] = []
    p._maybe_cooperative_close = lambda wal, ch, cid, nid, now, **kw: calls.append(cid) or True
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)
    clock.advance(FORCE_SEC + 1)                         # deadline reached
    # The peer is talkable again (so cooperation is possible) but the channel is
    # still not usable, so uptime stays low and the close intent holds.
    chan.reachable = True
    p._scan_offline_autoclose(w)
    assert calls == [CID] and forced == []               # cooperated, did not force
    # Only after cooperation has had its window does a force-close follow.
    chan.reachable = False
    clock.advance(COOP_WINDOW + 1)
    p._scan_offline_autoclose(w)
    assert forced == [bytes.fromhex(CID)]
    closes = [e for e in p.get_decision_log(w, "action") if e.get("kind") == "close"]
    assert any("cooperative close attempt" in (e.get("detail") or "") for e in closes)


def test_recovered_peer_clears_the_close_attempt_record(clock) -> None:
    """A cancelled close must drop its attempt record. Otherwise a re-commit
    later would find a long-expired ``first_ts``, decide cooperation had already
    had its chance, and force-close on the very first tick."""
    from electrum.plugins.inbound_liquidity import CLOSE_ATTEMPT_DB_KEY
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS=FORCE_SEC / DAY), _FakeWallet(ln)
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)
    clock.advance(FORCE_SEC + 1)
    p._scan_offline_autoclose(w)                     # stamps the attempt record
    assert CID in w.db.get(CLOSE_ATTEMPT_DB_KEY, {})

    # Peer comes back for good: uptime recovers, the close intent is cancelled.
    chan.active = True
    for _ in range(40):
        clock.advance(WINDOW_SEC / 20)
        p._scan_offline_autoclose(w)
    assert CID not in w.db.get(CLOSE_INTENT_DB_KEY, {}), "intent should be cancelled"
    assert CID not in w.db.get(CLOSE_ATTEMPT_DB_KEY, {}), \
        "the close-attempt record must be cleared with the intent"
    assert forced == []


def test_close_attempts_are_forgotten_for_vanished_channels(clock) -> None:
    """The attempt store must not grow without bound as channels are redeemed
    and removed."""
    from electrum.plugins.inbound_liquidity import CLOSE_ATTEMPT_DB_KEY
    chan = _Chan(_state("OPEN"), active=False)
    ln, _forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag(p, w, CID)
    w.db.put(CLOSE_ATTEMPT_DB_KEY, {"dead" * 16: {"first_ts": 1.0}})
    p._scan_offline_autoclose(w)
    assert "dead" * 16 not in w.db.get(CLOSE_ATTEMPT_DB_KEY, {})


def test_do_cooperative_close_calls_lnworker_and_logs(clock) -> None:
    chan = _Chan(_state("OPEN"), active=True)
    ln, _forced, coop = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._coop_closing = {w: {CID}}
    asyncio.run(p._do_cooperative_close(w, chan.channel_id, CID, NODE_A))
    assert coop == [chan.channel_id]                     # close_channel awaited
    assert p._count_actions_last_24h(w, "close") == 1
    closes = [e for e in p.get_decision_log(w, "action") if e.get("kind") == "close"]
    assert any("cooperatively closed" in (e.get("reason") or "") for e in closes)
    assert CID not in p._coop_closing[w]                 # inflight flag cleared


def test_disabled_does_nothing(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, forced, _ = _lnworker_with([chan])
    p, w = _plugin(INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED=False), _FakeWallet(ln)
    _tag(p, w, CID)
    _scan_n(p, w, clock=clock)
    clock.advance(FORCE_SEC + 1)
    p._scan_offline_autoclose(w)
    assert w.db.get(CLOSE_INTENT_DB_KEY, {}) == {}
    assert forced == []


# --- cleanup --------------------------------------------------------------
def test_stores_cleaned_when_channel_gone(clock) -> None:
    chan = _Chan(_state("OPEN"), active=False)
    ln, _forced, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag(p, w, CID)
    assert _drive_to_commit(p, w, chan, clock=clock)
    assert w.db.get(CHANNEL_UPTIME_DB_KEY, {})
    # Channel disappears (redeemed/removed).
    ln.channels.clear()
    p._scan_offline_autoclose(w)
    assert w.db.get(CHANNEL_UPTIME_DB_KEY, {}) == {}
    assert w.db.get(CLOSE_INTENT_DB_KEY, {}) == {}


def _state(name: str):
    from electrum.lnchannel import ChannelState
    return getattr(ChannelState, name)
