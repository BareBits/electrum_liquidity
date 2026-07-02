"""Glue-level tests for the daily action ceilings (rolling 24h): the persisted
action-timestamp store and its windowing/pruning, and the channel-close ceiling
gating the watchdog's wedged-open force-close (deferred when the ceiling is
reached, retried once it clears). Heavy Electrum objects are faked; skipped
outside the electrum venv.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    ACTION_TIMESTAMPS_DB_KEY,
    DEFAULT_MAX_OPENS_PER_DAY,
    DEFAULT_MAX_CLOSES_PER_DAY,
)
from liquidity_manager import DAILY_WINDOW_SEC  # type: ignore  # noqa: E402

NODE_A = "02" + "aa" * 32
CID = "aa" * 32


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
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.caps")
    p._remediating_opens = {}
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
        INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY=DEFAULT_MAX_OPENS_PER_DAY,
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=DEFAULT_MAX_CLOSES_PER_DAY,
    )
    cfg.update(config_overrides)
    p.config = SimpleNamespace(**cfg)
    return p


# --- timestamp store ------------------------------------------------------
def test_record_and_count_within_window() -> None:
    p, w = _plugin(), _FakeWallet()
    for _ in range(3):
        p._record_action_event(w, "open")
    assert p._count_actions_last_24h(w, "open") == 3
    assert p._count_actions_last_24h(w, "close") == 0
    assert w.saved == 3   # each record persists


def test_old_timestamps_pruned_on_record() -> None:
    p, w = _plugin(), _FakeWallet()
    # Seed one stale (>24h) and one fresh timestamp directly.
    now = time.time()
    w.db.put(ACTION_TIMESTAMPS_DB_KEY,
             {"close": [now - DAILY_WINDOW_SEC - 100, now - 60]})
    assert p._count_actions_last_24h(w, "close") == 1   # count ignores the stale one
    p._record_action_event(w, "close")                  # record prunes the stale one out
    stored = w.db.get(ACTION_TIMESTAMPS_DB_KEY)["close"]
    assert len(stored) == 2                             # stale dropped, fresh + new kept
    assert all(t >= now - DAILY_WINDOW_SEC for t in stored)


def test_within_close_cap_predicate() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=2), _FakeWallet()
    assert p._within_close_cap(w)
    p._record_action_event(w, "close")
    assert p._within_close_cap(w)
    p._record_action_event(w, "close")
    assert not p._within_close_cap(w)                  # 2 >= cap 2


def test_zero_close_cap_is_unlimited() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=0), _FakeWallet()
    for _ in range(50):
        p._record_action_event(w, "close")
    assert p._within_close_cap(w)


# --- watchdog force-close gated by the close ceiling ----------------------
class _Chan:
    def __init__(self, state, *, node_id=NODE_A, cid=CID, init_ts=None) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = state
        self.active = True
        self.unconfirmed_closing_txid = None
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


def _wedged_channel():
    from electrum.lnchannel import ChannelState
    return _Chan(ChannelState.OPENING, init_ts=time.time() - 7200)   # 2h, past 60-min timeout


def test_wedged_open_force_close_deferred_when_close_cap_reached() -> None:
    ln, forced = _lnworker_with([_wedged_channel()])
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=1), _FakeWallet(ln)
    p._record_action_event(w, "close")                 # already at the cap (1/1)
    p._scan_channel_health(w)
    assert forced == []                                # remediation deferred
    assert p._count_actions_last_24h(w, "close") == 1  # no new close counted
    # The peer was still faulted exactly once for the wedged open.
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 1
    assert CID in p._close_capped_logged[w]
    # A repeat tick neither re-faults nor force-closes while still capped.
    p._scan_channel_health(w)
    assert forced == []
    assert p._load_peer_reliability(w)[NODE_A.lower()]["hard_fault_count"] == 1


def test_wedged_open_force_close_retried_after_cap_clears() -> None:
    ln, forced = _lnworker_with([_wedged_channel()])
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=1), _FakeWallet(ln)
    p._record_action_event(w, "close")
    p._scan_channel_health(w)
    assert forced == []
    # Window rolls over / operator raises the ceiling: clear the close history.
    w.db.put(ACTION_TIMESTAMPS_DB_KEY, {})
    p._scan_channel_health(w)
    assert forced == [bytes.fromhex(CID)]              # now force-closed
    assert p._count_actions_last_24h(w, "close") == 1  # and counted
    # A close action was logged for visibility in the Actions view.
    log = p.get_decision_log(w, "action")
    assert any(e.get("kind") == "close" for e in log)


def test_wedged_open_force_close_counts_toward_cap() -> None:
    ln, forced = _lnworker_with([_wedged_channel()])
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5), _FakeWallet(ln)
    p._scan_channel_health(w)
    assert forced == [bytes.fromhex(CID)]
    assert p._count_actions_last_24h(w, "close") == 1  # the watchdog close is counted
