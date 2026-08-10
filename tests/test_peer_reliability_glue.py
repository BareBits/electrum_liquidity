"""Glue-level tests for channel-peer reliability (the Electrum-facing layer):
the persisted peer fault/success store (soft vs hard faults, decay, clear), the
auto-ban threshold writing into the banned-partners config, the channel-health
watchdog (force-close => one hard fault; wedged open => fault + force-close), and
the reverse-swap attribution fix (a failed Lightning payment faults the *peer*,
not the provider). Heavy Electrum objects are faked; skipped outside the venv.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    PEER_RELIABILITY_DB_KEY,
    PENDING_SWAPS_DB_KEY,
    _parse_banned_partners,
)

NODE_A = "02" + "aa" * 32
NODE_B = "03" + "bb" * 32


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
        # Settled, connected wallet -- these watchdog tests run past the
        # startup-race guard (see _plugin: _startup_grace_sec=0.0).
        self.network = SimpleNamespace(is_connected=lambda: True)

    def save_db(self) -> None:
        self.saved += 1


def _plugin(**config_overrides) -> LiquidityPlugin:
    import logging
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.peer")
    p._remediating_opens = {}
    p._local_closes = {}
    p._wedged_faulted = {}
    p._close_capped_logged = {}
    p._known_chan_states = {}
    p._last_decline_sigs = {}
    p._started_at = {}
    p._peer_seen_online = {}
    p._coop_closing = {}
    p._coop_close_cooldown_until = {}
    p._startup_grace_sec = 0.0
    cfg = dict(
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED=True,
        INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=3,
        INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN=60,
        INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN=360,
        INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN=True,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
    )
    cfg.update(config_overrides)
    p.config = SimpleNamespace(**cfg)
    return p


# --- store: soft vs hard, success, decay, clear ---------------------------
def test_soft_fault_does_not_count_toward_autoban() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "offline", hard=False)
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["consecutive_faults"] == 1
    assert s["fault_count"] == 1
    assert int(s.get("hard_fault_count", 0)) == 0


def test_hard_fault_counts_and_penalises() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "open failed", hard=True)
    p._record_peer_fault(w, NODE_A, "open failed", hard=True)
    rows = p.peer_reliability_rows(w)[NODE_A.lower()]
    assert rows["hard_fault_count"] == 2
    assert rows["fault_count"] == 2
    assert rows["penalty_pct"] == pytest.approx(1.0, abs=1e-3)   # 0.5 * 2^(2-1)


def test_success_resets_consecutive_faults() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "x", hard=True)
    p._record_peer_success(w, NODE_A)
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["consecutive_faults"] == 0
    assert s["success_count"] == 1
    assert s["hard_fault_count"] == 1                            # lifetime hard tally kept
    assert p.peer_reliability_rows(w)[NODE_A.lower()]["penalty_pct"] == 0.0


def test_penalty_decays_with_age() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "x", hard=False)            # 0.5% fresh
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()]["last_fault_ts"] = time.time() - 6 * 3600   # one half-life
    p._save_peer_reliability(w, data)
    assert p.peer_reliability_rows(w)[NODE_A.lower()]["penalty_pct"] == pytest.approx(0.25, abs=1e-3)


def test_peer_penalties_mapping_and_disabled() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "x", hard=False)
    assert p._peer_penalties(w)[NODE_A.lower()] == pytest.approx(0.5, abs=1e-3)
    p.config.INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED = False
    assert p._peer_penalties(w) == {}


def test_clear_one_and_all() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "x", hard=False)
    p._record_peer_fault(w, NODE_B, "y", hard=False)
    p.clear_peer_reliability(w, NODE_A)
    assert set(p._load_peer_reliability(w)) == {NODE_B.lower()}
    p.clear_peer_reliability(w)
    assert p._load_peer_reliability(w) == {}


# --- auto-ban -------------------------------------------------------------
def test_autoban_after_threshold_hard_faults() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=3), _FakeWallet()
    for _ in range(2):
        p._record_peer_fault(w, NODE_A, "open failed", hard=True)
    assert NODE_A.lower() not in _parse_banned_partners(p.config.INBOUND_LIQUIDITY_BANNED_PARTNERS)
    p._record_peer_fault(w, NODE_A, "open failed", hard=True)   # crosses threshold
    assert NODE_A.lower() in _parse_banned_partners(p.config.INBOUND_LIQUIDITY_BANNED_PARTNERS)


def test_soft_faults_never_autoban() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=2), _FakeWallet()
    for _ in range(5):
        p._record_peer_fault(w, NODE_A, "offline", hard=False)
    assert NODE_A.lower() not in _parse_banned_partners(p.config.INBOUND_LIQUIDITY_BANNED_PARTNERS)


def test_autoban_disabled_with_zero_threshold() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=0), _FakeWallet()
    for _ in range(5):
        p._record_peer_fault(w, NODE_A, "open failed", hard=True)
    assert p.config.INBOUND_LIQUIDITY_BANNED_PARTNERS == ""


# --- channel-health watchdog ----------------------------------------------
class _Chan:
    """A mutable fake channel: reassign ``state`` / ``active`` / ``confs`` /
    ``peer_reachable`` between scans to drive transitions through the watchdog.

    ``confs``/``min_depth`` model the funding tx's confirmation progress, which
    is what separates a channel that is merely slow to confirm from one that is
    genuinely wedged (see the plugin's ``_is_confirming``). ``confs < min_depth``
    means "still confirming, nothing wrong".
    """

    def __init__(self, state, *, node_id=NODE_A, cid="aa" * 32, init_ts=None,
                 closing_txid=None, active=True, confs=6, min_depth=3,
                 peer_reachable=False) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = state
        self.active = active
        self.confs = confs
        self.min_depth = min_depth
        self.peer_reachable = peer_reachable
        self.unconfirmed_closing_txid = closing_txid
        store = {"init_timestamp": init_ts} if init_ts is not None else {}
        self.storage = SimpleNamespace(get=lambda k, d=None: store.get(k, d))
        # Distinct per channel so the fake address-db can look confirmations up
        # per funding tx rather than conflating several channels' progress.
        self.funding_outpoint = SimpleNamespace(txid=cid)
        self.lnworker = None            # set by _lnworker_with

    def get_state(self):
        return self.state

    def is_active(self):
        return self.active

    def funding_txn_minimum_depth(self):
        return self.min_depth

    @property
    def peer_state(self):
        from electrum.lnchannel import PeerState
        return PeerState.GOOD if self.peer_reachable else PeerState.DISCONNECTED


def _lnworker_with(channels):
    forced: List[bytes] = []
    by_txid = {c.funding_outpoint.txid: c for c in channels}
    adb = SimpleNamespace(
        get_tx_height=lambda txid: SimpleNamespace(
            conf=by_txid[txid].confs if txid in by_txid else 0))
    ln = SimpleNamespace(
        channels={c.channel_id: c for c in channels},
        schedule_force_closing=lambda cid: forced.append(cid),
        lnwatcher=SimpleNamespace(adb=adb),
        lnpeermgr=SimpleNamespace(
            get_peer_by_pubkey=lambda pk: next(
                (object() for c in channels
                 if c.node_id == pk and c.peer_reachable), None)),
    )
    for c in channels:
        c.lnworker = ln
    return ln, forced


class _Clock:
    """Controllable stand-in for the plugin module's ``time``. The wedged-open
    gates deliberately require observations spread over real time, so tests
    drive the clock rather than sleeping through hours of it."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_watchdog_peer_force_close_faults_once() -> None:
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)                                   # first scan: OPEN, seed
    assert p._load_peer_reliability(w) == {}
    chan.state = ChannelState.FORCE_CLOSING                     # peer force-closes
    p._scan_channel_health(w)
    p._scan_channel_health(w)                                   # still closing: no double
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["hard_fault_count"] == 1
    assert "force-closed by peer" in s["last_reason"]


def test_watchdog_cooperative_close_is_a_fault() -> None:
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    chan.state = ChannelState.CLOSING                           # cooperative close
    p._scan_channel_health(w)
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["hard_fault_count"] == 1
    assert s["last_reason"] == "channel closed by peer"


def _lnworker_closable(channels):
    """An lnworker whose local close entry points are real (async/sync) callables,
    so _install_close_hooks can wrap them and a call records the channel as a
    local close. Records what was closed for assertions."""
    coop: List[bytes] = []
    forced: List[bytes] = []

    async def _close_channel(chan_id):
        coop.append(chan_id)
        return "txid"

    async def _force_close_channel(chan_id):
        forced.append(chan_id)
        return "txid"

    async def _request_force_close(channel_id, *, connect_str=None):
        forced.append(channel_id)

    def _schedule_force_closing(chan_id):
        forced.append(chan_id)

    ln = SimpleNamespace(
        channels={c.channel_id: c for c in channels},
        close_channel=_close_channel,
        force_close_channel=_force_close_channel,
        request_force_close=_request_force_close,
        schedule_force_closing=_schedule_force_closing,
    )
    return ln, coop, forced


def test_install_close_hooks_records_and_is_idempotent() -> None:
    import asyncio
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, coop, forced = _lnworker_closable([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._install_close_hooks(w, ln)
    # Second install is a no-op (idempotent): the wrappers are not re-wrapped.
    hooked = ln.close_channel
    p._install_close_hooks(w, ln)
    assert ln.close_channel is hooked
    # The sync force-close path records the channel and still delegates.
    ln.schedule_force_closing(chan.channel_id)
    assert forced == [chan.channel_id]
    assert chan.channel_id.hex() in p._local_closes[w]
    # The async cooperative path records and still delegates (and returns).
    p._local_closes[w].clear()
    assert asyncio.run(ln.close_channel(chan.channel_id)) == "txid"
    assert coop == [chan.channel_id]
    assert chan.channel_id.hex() in p._local_closes[w]


def test_watchdog_user_cooperative_close_not_blamed_on_peer() -> None:
    import asyncio
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, _c, _f = _lnworker_closable([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._install_close_hooks(w, ln)
    p._scan_channel_health(w)                                   # seed OPEN
    asyncio.run(ln.close_channel(chan.channel_id))             # the user closes it
    chan.state = ChannelState.CLOSING
    p._scan_channel_health(w)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w) == {}                    # peer NOT faulted


def test_watchdog_user_force_close_not_blamed_on_peer() -> None:
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, _c, _f = _lnworker_closable([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._install_close_hooks(w, ln)
    p._scan_channel_health(w)                                   # seed OPEN
    ln.schedule_force_closing(chan.channel_id)                 # the user force-closes it
    chan.state = ChannelState.FORCE_CLOSING
    p._scan_channel_health(w)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w) == {}                    # peer NOT faulted


def test_watchdog_local_close_forgotten_after_channel_gone() -> None:
    # Once the closed channel is redeemed/removed, its id is pruned from the
    # local-close set so a later reused channel_id starts fresh.
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, _c, _f = _lnworker_closable([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._install_close_hooks(w, ln)
    p._scan_channel_health(w)
    ln.schedule_force_closing(chan.channel_id)
    chan.state = ChannelState.FORCE_CLOSING
    p._scan_channel_health(w)
    assert chan.channel_id.hex() in p._local_closes[w]
    ln.channels.clear()                                        # channel fully gone
    p._scan_channel_health(w)
    assert p._local_closes[w] == set()


def test_watchdog_remote_force_close_detected_before_mined() -> None:
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    chan.unconfirmed_closing_txid = "deadbeef"                  # remote fc, still OPEN
    p._scan_channel_health(w)
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["hard_fault_count"] == 1
    assert "force-closed by peer" in s["last_reason"]


def test_watchdog_preexisting_closed_channel_not_blamed() -> None:
    from electrum.lnchannel import ChannelState
    # A channel already closed when we start managing must not be faulted.
    ln, _f = _lnworker_with([_Chan(ChannelState.CLOSED)])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w) == {}


def test_watchdog_peer_offline_is_daily_rate_limited_hard_fault() -> None:
    from electrum.lnchannel import ChannelState
    chan = _Chan(ChannelState.OPEN, active=False)              # OPEN but peer disconnected
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    p._scan_channel_health(w)                                   # same day: not re-counted
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["hard_fault_count"] == 1
    assert s["last_reason"] == "peer offline"
    # Backdate the rate-limit stamp by > 24h: the next scan faults again.
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()]["last_offline_fault_ts"] = time.time() - 90000
    p._save_peer_reliability(w, data)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 2


def test_watchdog_active_peer_no_offline_fault() -> None:
    from electrum.lnchannel import ChannelState
    ln, _f = _lnworker_with([_Chan(ChannelState.OPEN, active=True)])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w) == {}


# --- wedged-open remediation: the three gates ----------------------------
# Force-closing is irreversible and costs a mining fee, so remediation is gated
# on (1) the channel not merely confirming, (2) a wedge clock timed from the
# first wedged observation, and (3) several strikes spread over real time. On
# top of that, a cooperative close is always attempted (or established as
# impossible) before a force-close. These tests drive each gate separately.

def _wedged_chan(clock, **kw):
    """A channel that is pre-OPEN, fully confirmed, and whose peer is gone --
    i.e. wedge-eligible from the first observation."""
    from electrum.lnchannel import ChannelState
    kw.setdefault("confs", 6)
    kw.setdefault("min_depth", 3)
    kw.setdefault("peer_reachable", False)
    return _Chan(ChannelState.OPENING, init_ts=clock.time(), **kw)


def _tick_until_forced(p, w, clock, forced, *, max_ticks=20, step=3600.0):
    """Scan repeatedly, advancing an hour each time, until a force-close is
    scheduled. Returns the number of ticks it took (or max_ticks)."""
    for i in range(1, max_ticks + 1):
        p._scan_channel_health(w)
        if forced:
            return i
        clock.advance(step)
    return max_ticks


def test_watchdog_stuck_open_faults_and_force_closes(monkeypatch) -> None:
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)

    # One tick is nowhere near enough: the wedge clock has not run and there is
    # only a single strike.
    p._scan_channel_health(w)
    assert forced == []
    assert p._load_peer_reliability(w) == {}

    ticks = _tick_until_forced(p, w, clock, forced)
    assert forced == [bytes.fromhex("aa" * 32)]
    # 6h clock + the 10-min cooperative-close window, at one tick per hour.
    assert ticks >= 7, f"force-closed too eagerly after {ticks} ticks"
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["hard_fault_count"] == 1
    assert "wedged" in s["last_reason"]
    # Idempotent: a further tick neither re-faults nor re-force-closes.
    clock.advance(3600.0)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 1
    assert len(forced) == 1


def test_watchdog_stuck_open_no_remediation_when_disabled(monkeypatch) -> None:
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    ln, forced = _lnworker_with([_wedged_chan(clock)])
    p, w = _plugin(INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN=False), _FakeWallet(ln)
    _tick_until_forced(p, w, clock, forced)
    # The peer is still faulted for the wedge, but nothing is closed.
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 1
    assert forced == []


def test_watchdog_young_open_is_left_alone(monkeypatch) -> None:
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    ln, forced = _lnworker_with([_wedged_chan(clock)])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w) == {}
    assert forced == []


def test_confirming_open_is_never_wedged(monkeypatch) -> None:
    """Gate 1. A funding tx still accruing confirmations is not wedged, however
    long it takes -- this is the regression that force-closed a healthy channel
    whose funding was simply slow to confirm."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock, confs=1, min_depth=3)     # 1 of 3 confirmations
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    ticks = _tick_until_forced(p, w, clock, forced, max_ticks=48)   # ~2 days
    assert ticks == 48 and forced == []
    assert p._load_peer_reliability(w) == {}             # and the peer is blameless
    # The wedge clock has not even started.
    assert w.db.get(pkg.WEDGE_STRIKES_DB_KEY, {}) == {}


def test_confirmation_progress_starts_the_clock_late(monkeypatch) -> None:
    """Gate 1 + 2. Time spent confirming does not count toward the force-close
    clock: it starts only once the tx is confirmed and the peer still has not
    completed the handshake."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock, confs=0, min_depth=3)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    for _ in range(12):                                  # 12h of honest confirming
        p._scan_channel_health(w)
        clock.advance(3600.0)
    assert forced == []
    chan.confs = 3                                       # now confirmed; peer silent
    started = clock.time()
    _tick_until_forced(p, w, clock, forced)
    assert forced != []
    # The clock ran from the moment it stopped confirming, not from creation.
    assert clock.time() - started >= 6 * 3600.0


def test_wedge_strikes_reset_when_channel_recovers(monkeypatch) -> None:
    """Gate 3. A channel that reaches OPEN wipes its strike history, so an
    unrelated later wedge starts from zero rather than escalating instantly."""
    from electrum.lnchannel import ChannelState
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    for _ in range(4):
        p._scan_channel_health(w)
        clock.advance(3600.0)
    assert w.db.get(pkg.WEDGE_STRIKES_DB_KEY, {})        # strikes accumulated
    chan.state = ChannelState.OPEN                       # peer completed the handshake
    p._scan_channel_health(w)
    assert w.db.get(pkg.WEDGE_STRIKES_DB_KEY, {}) == {}  # history wiped
    assert forced == []


def test_absolute_cap_overrides_confirmation_exemption(monkeypatch) -> None:
    """A funding tx that never confirms would otherwise stay exempt forever,
    with nothing able to free the funds. The absolute cap backstops it."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    # Created 8 days ago, still 0 of 3 confirmations: past the 7-day cap.
    chan = _wedged_chan(clock, confs=0, min_depth=3)
    chan.storage = SimpleNamespace(
        get=lambda k, d=None: {"init_timestamp": clock.time() - 8 * 86400.0}.get(k, d))
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tick_until_forced(p, w, clock, forced)
    assert forced == [bytes.fromhex("aa" * 32)]


def test_wedged_open_with_reachable_peer_cooperates_before_forcing(monkeypatch) -> None:
    """The invariant, on the wedged-open path: if we can talk to the peer at
    all, cooperation is tried BEFORE any force-close -- even though the channel
    is pre-OPEN, where ``chan.is_active()`` is always False and so cannot be
    used to detect reachability."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock, peer_reachable=True)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    events: List[str] = []
    p._maybe_cooperative_close = lambda wal, ch, cid, nid, now, **kw: (
        events.append("coop") or True)
    for _ in range(20):
        before = len(forced)
        p._scan_channel_health(w)
        if len(forced) > before:
            events.append("force")
        clock.advance(3600.0)
    assert "coop" in events, "a reachable peer must get a cooperative attempt"
    assert "force" in events, "an uncooperative peer must still be escalated"
    # Ordering is the point: cooperation is never preceded by a force-close.
    assert events.index("coop") < events.index("force")
    # And cooperation is retried, not tried once, before giving up.
    assert events.count("coop") == pkg.COOP_MAX_ATTEMPTS


def test_wedged_open_with_unreachable_peer_forces_after_one_window(monkeypatch) -> None:
    """With nobody to cooperate with, the peer gets exactly one window to come
    back -- we do not sit on locked funds indefinitely waiting for a dead peer."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock, peer_reachable=False)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    coop: List[str] = []
    p._maybe_cooperative_close = lambda wal, ch, cid, nid, now, **kw: (
        coop.append(cid) or True)
    _tick_until_forced(p, w, clock, forced)
    assert coop == [], "cooperation is impossible with no peer connection"
    assert forced == [bytes.fromhex("aa" * 32)]


def test_wedged_open_force_closes_once_coop_keeps_failing(monkeypatch) -> None:
    """Cooperation is tried first, but a peer that is reachable and simply will
    not cooperate must not block the backstop forever."""
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock, peer_reachable=True)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    coop: List[str] = []
    # Attempts are launched but never complete (peer accepts nothing).
    p._maybe_cooperative_close = lambda wal, ch, cid, nid, now, **kw: (
        coop.append(cid) or True)
    _tick_until_forced(p, w, clock, forced, max_ticks=30)
    assert coop                                   # cooperation was tried...
    assert forced == [bytes.fromhex("aa" * 32)]   # ...then escalated
    closes = [e for e in p.get_decision_log(w, "action") if e.get("kind") == "close"]
    assert any("cooperative close attempt" in (e.get("detail") or "") for e in closes)


def test_toxic_channel_is_never_closed_by_us(monkeypatch) -> None:
    """Electrum states that force-closing a WE_ARE_TOXIC channel is unsafe (our
    commitment tx is revoked). The close path must refuse outright."""
    from electrum.lnchannel import ChannelState
    clock = _Clock(1_700_000_000.0)
    monkeypatch.setattr(pkg, "time", clock)
    chan = _wedged_chan(clock)
    ln, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    chan.state = ChannelState.WE_ARE_TOXIC
    p._request_close(w, chan, "aa" * 32, NODE_A, clock.time(),
                     reason="test", detail="test")
    assert forced == []


def test_open_age_exceeded_helper() -> None:
    from electrum.lnchannel import ChannelState
    now = time.time()
    assert LiquidityPlugin._open_age_exceeded(
        _Chan(ChannelState.OPENING, init_ts=now - 7200), 3600.0, now)
    assert not LiquidityPlugin._open_age_exceeded(
        _Chan(ChannelState.OPENING, init_ts=now - 100), 3600.0, now)
    # No init_timestamp -> never considered stuck.
    assert not LiquidityPlugin._open_age_exceeded(
        _Chan(ChannelState.OPENING), 3600.0, now)


def test_fault_is_logged_to_decision_log() -> None:
    # A peer fault surfaces in the "fault" category of the decision log, reason
    # and all, so the GUI Faults view can show it.
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "peer offline", hard=True)
    faults = p.get_decision_log(w, "fault")
    assert len(faults) == 1
    assert faults[0]["kind"] == "peer"
    assert "peer offline" in faults[0]["reason"]
    assert faults[0]["reason"].startswith("hard fault:")


# --- reverse-swap attribution fix -----------------------------------------
def test_reconcile_failed_ln_payment_faults_peer_not_provider() -> None:
    from electrum.invoices import PR_FAILED
    ph = "ab" * 32
    swap = SimpleNamespace(is_redeemed=False, funding_txid=None)
    ln = SimpleNamespace(
        swap_manager=SimpleNamespace(_swaps={}, get_swap=lambda h: swap),
        get_payment_status=lambda h, *, direction: PR_FAILED,
    )
    p, w = _plugin(), _FakeWallet(ln)
    w.db.put(PENDING_SWAPS_DB_KEY,
             {ph: {"npub": "npubPROV", "node_id": NODE_A, "channel_id": "aa" * 32,
                   "started_ts": time.time()}})
    p._reconcile_pending_swaps(w)
    # Peer charged, provider untouched, tracking cleared.
    assert p._load_peer_reliability(w)[NODE_A.lower()]["fault_count"] == 1
    assert p._load_reliability(w) == {}
    assert p._load_pending_swaps(w) == {}


def test_reconcile_inflight_ln_payment_waits_then_provider_stuck() -> None:
    from electrum.invoices import PR_INFLIGHT
    ph = "ab" * 32
    swap = SimpleNamespace(is_redeemed=False, funding_txid=None)
    ln = SimpleNamespace(
        swap_manager=SimpleNamespace(_swaps={}, get_swap=lambda h: swap),
        get_payment_status=lambda h, *, direction: PR_INFLIGHT,
    )
    p, w = _plugin(), _FakeWallet(ln)
    # Past the provider stuck timeout, payment still inflight -> provider fault.
    w.db.put(PENDING_SWAPS_DB_KEY,
             {ph: {"npub": "npubPROV", "node_id": NODE_A, "channel_id": "aa" * 32,
                   "started_ts": time.time() - 99999}})
    p._reconcile_pending_swaps(w)
    assert p._load_reliability(w)["npubPROV"]["consecutive_faults"] == 1
    assert p._load_peer_reliability(w) == {}
