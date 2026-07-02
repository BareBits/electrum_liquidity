"""Glue-level tests for the decision-log store: retention pruning, consecutive-
duplicate decline suppression, entry building/abbreviation, and the wallet.db
persistence round-trip.

These import the real plugin package (which pulls in Electrum core, but not the
Qt GUI), and exercise the log helpers on a LiquidityPlugin instance built
*without* running BasePlugin.__init__ (so no network/parent is needed) against a
dict-backed fake wallet.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Dict, List

import pytest

# Import the package (Electrum core must be importable via the venv). If it is
# not (e.g. running these without the electrum venv), skip rather than error.
inbound_liquidity = pytest.importorskip("electrum.plugins.inbound_liquidity")
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    LOG_DB_KEY,
    MAX_LOG_ENTRIES,
    MAX_LOG_RETENTION_DAYS,
    DEFAULT_LOG_RETENTION_DAYS,
)
from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)  # noqa: E402
    ChannelSnapshot,
    DeclineRecord,
    LiquidityConfig,
    LiquiditySnapshot,
)


class _FakeDB:
    """Minimal stand-in for wallet.db: a dict with get/put."""
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self) -> None:
        self.db = _FakeDB()
        self.saved = 0

    def save_db(self) -> None:
        self.saved += 1


class _FakeConfig:
    def __init__(self, retention_days=DEFAULT_LOG_RETENTION_DAYS) -> None:
        self.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS = retention_days


def _make_plugin(retention_days=DEFAULT_LOG_RETENTION_DAYS) -> LiquidityPlugin:
    # Build the instance without BasePlugin.__init__ (which needs a parent /
    # network); wire up only the attributes the log helpers touch.
    p = object.__new__(LiquidityPlugin)
    p.config = _FakeConfig(retention_days)
    p.logger = logging.getLogger("test.inbound_liquidity")
    p._last_decline_sigs = {}
    return p


def _config() -> LiquidityConfig:
    return LiquidityConfig(
        automation_enabled=True, min_onchain_to_open_sat=1_000_000,
        onchain_reserve_sat=10_000, max_channels=2, max_swap_fee_pct=0.6,
        swap_trigger_pct=25.0, swap_trigger_sat=25_000)


def _snapshot() -> LiquiditySnapshot:
    chan = ChannelSnapshot(
        channel_id="aa" * 32, short_id="117x1x0", capacity_sat=2_000_000,
        local_sat=1_000_000, remote_sat=1_000_000, spendable_local_sat=900_000,
        is_active=True)
    return LiquiditySnapshot(
        onchain_spendable_sat=5_000_000, channels=(chan,),
        swap_percentage_fee=0.5, provider_max_reverse_sat=1_900_000,
        provider_min_amount_sat=20_000, swap_mining_fee_sat=22_500,
        swap_claim_fee_sat=22_500, pending_channel_count=1, inflight_swap_count=0)


# --- abbreviation ---------------------------------------------------------
def test_abbrev_shortens_long_ids() -> None:
    node = "02b2a9bbdd7513a559deb22666afd04a4c80fe400eeda34dd7ba53d3e027a06501"
    assert LiquidityPlugin._abbrev(node) == "02b2a9…6501"


def test_abbrev_passes_short_values_through() -> None:
    assert LiquidityPlugin._abbrev("117x1x0") == "117x1x0"
    assert LiquidityPlugin._abbrev(None) is None


# --- state dict -----------------------------------------------------------
def test_state_dict_is_json_plain_and_complete() -> None:
    import json
    p = _make_plugin()
    state = p._state_dict(_snapshot(), _config())
    # Must round-trip through JSON (it is persisted in wallet.db).
    json.loads(json.dumps(state))
    assert state["pending_channel_count"] == 1
    assert state["num_channels"] == 1 and state["active_channels"] == 1
    assert state["config"]["max_channels"] == 2
    assert state["channels"][0]["short_id"] == "117x1x0"


# --- action logging + persistence ----------------------------------------
def test_log_action_persists_and_abbreviates() -> None:
    p = _make_plugin()
    w = _FakeWallet()
    node = "02b2a9bbdd7513a559deb22666afd04a4c80fe400eeda34dd7ba53d3e027a06501"
    p._log_action(w, kind="open", amount_sat=1_990_000, source="on-chain",
                  dest=node, reason="cold wallet", detail="funding txid abc",
                  state=p._state_dict(_snapshot(), _config()))
    assert w.saved == 1
    stored: List[Dict] = w.db.get(LOG_DB_KEY)
    assert len(stored) == 1
    e = stored[0]
    assert e["category"] == "action" and e["kind"] == "open"
    assert e["source"] == "on-chain"            # passthrough, not abbreviated
    assert e["dest"] == "02b2a9…6501"           # node id abbreviated
    assert e["amount_sat"] == 1_990_000


def test_get_decision_log_filters_and_orders_newest_first() -> None:
    p = _make_plugin()
    w = _FakeWallet()
    p._log_action(w, kind="open", amount_sat=1, source="on-chain", dest="x",
                  reason="first", detail=None, state={})
    p._log_action(w, kind="swap", amount_sat=2, source="117x1x0", dest="on-chain",
                  reason="second", detail=None, state={})
    actions = p.get_decision_log(w, "action")
    assert [e["reason"] for e in actions] == ["second", "first"]  # newest first
    assert p.get_decision_log(w, "decline") == []


# --- decline dedupe (tick-level) -----------------------------------------
def test_filter_new_declines_collapses_repeated_tick() -> None:
    # A rotating set of declines (1 open + 2 swaps) recurring every tick: logged
    # in full once, then nothing on subsequent identical ticks.
    p = _make_plugin()
    w = _FakeWallet()
    tick = [
        DeclineRecord(kind="open", reason="at max channels"),
        DeclineRecord(kind="swap", channel_id="aa", short_id="1x1x1", reason="cost too high"),
        DeclineRecord(kind="swap", channel_id="bb", short_id="2x2x2", reason="cost too high"),
    ]
    assert len(p._filter_new_declines(w, tick)) == 3   # first tick: all new
    assert p._filter_new_declines(w, tick) == []       # second identical tick: none
    assert p._filter_new_declines(w, tick) == []       # still none


def test_filter_new_declines_reports_only_the_delta() -> None:
    p = _make_plugin()
    w = _FakeWallet()
    t1 = [DeclineRecord(kind="swap", channel_id="aa", short_id="1x1x1", reason="cost")]
    t2 = [
        DeclineRecord(kind="swap", channel_id="aa", short_id="1x1x1", reason="cost"),
        DeclineRecord(kind="freeze", reason="frozen: 1 swap in flight"),
    ]
    assert len(p._filter_new_declines(w, t1)) == 1
    new = p._filter_new_declines(w, t2)
    assert [d.kind for d in new] == ["freeze"]          # only the newcomer


def test_action_resets_decline_dedupe() -> None:
    # After an action (a real state change), the same decline should pass the
    # filter afresh rather than being deduped against the pre-action set.
    p = _make_plugin()
    w = _FakeWallet()
    tick = [DeclineRecord(kind="swap", channel_id="aa", short_id="1x1x1", reason="cost")]
    assert len(p._filter_new_declines(w, tick)) == 1
    assert p._filter_new_declines(w, tick) == []        # deduped
    p._log_action(w, kind="open", amount_sat=1, source="on-chain", dest="x",
                  reason="opened", detail=None, state={})
    assert len(p._filter_new_declines(w, tick)) == 1    # reset -> logs again


# --- retention pruning ----------------------------------------------------
def test_prune_drops_entries_older_than_retention() -> None:
    p = _make_plugin(retention_days=30)
    w = _FakeWallet()
    now = time.time()
    old = {"ts": now - 31 * 86400, "category": "action", "reason": "old"}
    fresh = {"ts": now - 1 * 86400, "category": "action", "reason": "fresh"}
    w.db.put(LOG_DB_KEY, [old, fresh])
    kept = p.get_decision_log(w)
    assert [e["reason"] for e in kept] == ["fresh"]


def test_retention_days_clamped_to_bounds() -> None:
    assert _make_plugin(retention_days=0)._retention_days() == 1
    assert _make_plugin(retention_days=10_000)._retention_days() == MAX_LOG_RETENTION_DAYS
    assert _make_plugin(retention_days="bad")._retention_days() == DEFAULT_LOG_RETENTION_DAYS


def test_prune_caps_total_entries() -> None:
    p = _make_plugin(retention_days=999)
    w = _FakeWallet()
    now = time.time()
    many = [{"ts": now, "category": "action", "reason": str(i)}
            for i in range(MAX_LOG_ENTRIES + 50)]
    w.db.put(LOG_DB_KEY, many)
    kept = p._prune(list(many))
    assert len(kept) == MAX_LOG_ENTRIES
    # the most recent are kept (tail of the list)
    assert kept[-1]["reason"] == str(MAX_LOG_ENTRIES + 49)


# --- build_snapshot wiring (exercises the real ChannelState / swap manager) --
class _FakeChan:
    def __init__(self, *, cid: bytes, short, capacity, local_msat, remote_msat,
                 spendable_msat, state, active) -> None:
        self.channel_id = cid
        self.short_channel_id = short
        self._capacity = capacity
        self._local = local_msat
        self._remote = remote_msat
        self._spendable = spendable_msat
        self._state = state
        self._active = active

    def get_capacity(self):
        return self._capacity

    def get_state(self):
        return self._state

    def is_active(self):
        return self._active

    def balance(self, direction):
        from electrum.lnutil import LOCAL
        return self._local if direction == LOCAL else self._remote

    def available_to_spend(self, direction):
        return self._spendable


class _FakeSwapManager:
    def __init__(self, pending_swaps) -> None:
        self._pending = pending_swaps
        self.percentage = 0.5
        self.mining_fee = 22_500

    def get_pending_swaps(self):
        return self._pending

    def get_fee_for_txbatcher(self):
        return 22_500

    def get_provider_max_reverse_amount(self):
        return 1_900_000

    def get_min_amount(self):
        return 20_000


class _FakeLnworker:
    def __init__(self, channels, swap_manager) -> None:
        self._channels = {c.channel_id: c for c in channels}
        self.swap_manager = swap_manager

    @property
    def channels(self):
        return self._channels


class _FakeSnapWallet:
    def __init__(self, lnworker, onchain_sat) -> None:
        self.lnworker = lnworker
        self._onchain = onchain_sat

    def get_spendable_balance_sat(self):
        return self._onchain


def test_build_snapshot_counts_pending_channels_and_inflight_swaps() -> None:
    from electrum.lnchannel import ChannelState
    p = _make_plugin()
    open_chan = _FakeChan(
        cid=b"\xaa" * 32, short="117x1x0", capacity=2_000_000,
        local_msat=1_000_000_000, remote_msat=1_000_000_000,
        spendable_msat=900_000_000, state=ChannelState.OPEN, active=True)
    opening_chan = _FakeChan(
        cid=b"\xbb" * 32, short=None, capacity=2_000_000,
        local_msat=0, remote_msat=2_000_000_000,
        spendable_msat=0, state=ChannelState.OPENING, active=False)
    funded_chan = _FakeChan(
        cid=b"\xcc" * 32, short=None, capacity=2_000_000,
        local_msat=0, remote_msat=2_000_000_000,
        spendable_msat=0, state=ChannelState.FUNDED, active=False)
    sm = _FakeSwapManager(pending_swaps=["swap1", "swap2"])
    ln = _FakeLnworker([open_chan, opening_chan, funded_chan], sm)
    wallet = _FakeSnapWallet(ln, onchain_sat=5_000_000)

    snap = p.build_snapshot(wallet)
    assert snap.onchain_spendable_sat == 5_000_000
    assert len(snap.channels) == 3
    assert snap.pending_channel_count == 2          # OPENING + FUNDED
    assert snap.inflight_swap_count == 2            # len(get_pending_swaps())
    # msat -> sat conversion and short-id fallback to channel_id[:8]
    open_snap = next(c for c in snap.channels if c.short_id == "117x1x0")
    assert open_snap.local_sat == 1_000_000 and open_snap.spendable_local_sat == 900_000
    opening_snap = next(c for c in snap.channels if c.channel_id == "bb" * 32)
    assert opening_snap.short_id == ("bb" * 32)[:8]


def test_build_snapshot_freeze_then_clear_end_to_end() -> None:
    # An OPENING channel + on-chain funds -> evaluate() freezes (no second open).
    # Once it reaches OPEN, the same state is no longer frozen.
    from electrum.lnchannel import ChannelState
    from liquidity_manager import evaluate  # type: ignore
    p = _make_plugin()
    chan = _FakeChan(
        cid=b"\xbb" * 32, short=None, capacity=2_000_000, local_msat=0,
        remote_msat=2_000_000_000, spendable_msat=0,
        state=ChannelState.OPENING, active=False)
    sm = _FakeSwapManager(pending_swaps=[])
    wallet = _FakeSnapWallet(_FakeLnworker([chan], sm), onchain_sat=5_000_000)
    cfg = _config()

    frozen_snap = p.build_snapshot(wallet)
    frozen_result = evaluate(frozen_snap, cfg)
    assert frozen_result.frozen is not None
    assert frozen_result.actions == ()           # no second channel open

    chan._state = ChannelState.OPEN
    chan._active = True
    open_snap = p.build_snapshot(wallet)
    assert open_snap.pending_channel_count == 0
    assert evaluate(open_snap, cfg).frozen is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
