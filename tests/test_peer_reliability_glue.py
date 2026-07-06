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
    p._startup_grace_sec = 0.0
    cfg = dict(
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED=True,
        INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=3,
        INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN=60,
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
    """A mutable fake channel: reassign ``state`` / ``active`` between scans to
    drive transitions through the watchdog."""

    def __init__(self, state, *, node_id=NODE_A, cid="aa" * 32, init_ts=None,
                 closing_txid=None, active=True) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = state
        self.active = active
        self.unconfirmed_closing_txid = closing_txid
        store = {"init_timestamp": init_ts} if init_ts is not None else {}
        self.storage = SimpleNamespace(get=lambda k, d=None: store.get(k, d))

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


def test_watchdog_stuck_open_faults_and_force_closes() -> None:
    from electrum.lnchannel import ChannelState
    old = time.time() - 7200                                    # 2h, past the 60-min timeout
    ln, forced = _lnworker_with([_Chan(ChannelState.OPENING, init_ts=old)])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    s = p._load_peer_reliability(w)[NODE_A.lower()]
    assert s["hard_fault_count"] == 1
    assert "wedged" in s["last_reason"]
    assert forced == [bytes.fromhex("aa" * 32)]                 # force-close scheduled
    # Idempotent: a second tick neither re-faults nor re-force-closes.
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 1
    assert len(forced) == 1


def test_watchdog_stuck_open_no_remediation_when_disabled() -> None:
    from electrum.lnchannel import ChannelState
    old = time.time() - 7200
    ln, forced = _lnworker_with([_Chan(ChannelState.OPENING, init_ts=old)])
    p, w = _plugin(INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN=False), _FakeWallet(ln)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 1
    assert forced == []                                        # flagged but not force-closed


def test_watchdog_young_open_is_left_alone() -> None:
    from electrum.lnchannel import ChannelState
    ln, forced = _lnworker_with([_Chan(ChannelState.OPENING, init_ts=time.time())])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    assert p._load_peer_reliability(w) == {}
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
