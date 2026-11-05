"""Glue-level tests for the offline-channel discount against ``max_channels``:
the Electrum-facing half of the feature the pure rules are pinned in
``test_offline_channel_discount.py``.

Covered here:
  * the per-peer offline tally -- bumped by the channel-health watchdog once per
    24h outage, and cleared the moment the peer is seen online again;
  * ``build_snapshot`` carrying the three inputs the discount reads
    (``is_pending`` / ``close_intent`` / ``offline_fault_count``) off live
    Electrum state, so the REAL engine then frees the slot;
  * the dead peer being excluded from channel-partner selection even with the
    one-channel-per-peer guard off -- the replacement must not be opened back to
    the node that went dark;
  * the evaluation-backoff signature noticing a channel becoming discounted.

Heavy Electrum objects are faked; skipped outside the venv.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from conftest import seed_settings  # noqa: E402

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.lnutil import LOCAL  # type: ignore  # noqa: E402
from electrum.lnchannel import ChannelState, PeerState  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    CLOSE_INTENT_DB_KEY,
    DEFAULT_OFFLINE_OPEN_ALLOWANCE,
    LiquidityPlugin,
    PEER_OFFLINE_FAULTS_FIELD,
    PEER_OFFLINE_FAULT_TS_FIELD,
    PLUGIN_OPENED_CHANNELS_DB_KEY,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    OFFLINE_DISCOUNT_MIN_FAULTS,
    channel_is_offline_discounted,
    effective_channel_count,
)

NODE_A = "02" + "aa" * 32
NODE_B = "03" + "bb" * 32
CID_A = "aa" * 32
CID_B = "bb" * 32
DAY = 86400.0


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    """Hashable by identity -- the plugin keys per-wallet dicts on the wallet."""

    def __init__(self, lnworker=None, *, onchain_sat: int = 0) -> None:
        self.db = _FakeDB()
        self.lnworker = lnworker
        self.saved = 0
        self._onchain = onchain_sat
        # A settled, connected wallet: these tests run past the startup-race
        # guard (the plugin factory sets _startup_grace_sec = 0).
        self.network = SimpleNamespace(is_connected=lambda: True)

    def get_spendable_balance_sat(self) -> int:
        return self._onchain

    def basename(self) -> str:
        return "liqtest"

    def save_db(self) -> None:
        self.saved += 1


class _Chan:
    """A mutable fake channel: reassign ``state`` / ``active`` between scans to
    drive transitions through the watchdog."""

    def __init__(self, state=ChannelState.OPEN, *, node_id=NODE_A, cid=CID_A,
                 capacity: int = 500_000, local_msat: int = 0,
                 active: bool = True, confs: int = 6, min_depth: int = 3,
                 peer_reachable: bool = False,
                 unsettled: Optional[List[bytes]] = None) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.short_channel_id = "100x1x0"
        self.state = state
        self.active = active
        self.confs = confs
        self.min_depth = min_depth
        self.peer_reachable = peer_reachable
        self.unconfirmed_closing_txid = None
        self._capacity = capacity
        self._local_msat = local_msat
        self._unsettled = unsettled or []
        self.storage = SimpleNamespace(get=lambda k, d=None: {}.get(k, d))
        self.funding_outpoint = SimpleNamespace(txid=cid)
        self.hm = SimpleNamespace(
            htlcs=lambda subject: [("dir", SimpleNamespace(payment_hash=ph))
                                   for ph in self._unsettled])
        self.lnworker = None            # set by _lnworker_with

    def get_state(self):
        return self.state

    def is_active(self) -> bool:
        return self.active

    def get_capacity(self) -> int:
        return self._capacity

    def balance(self, subject) -> int:
        return (self._local_msat if subject == LOCAL
                else self._capacity * 1000 - self._local_msat)

    def available_to_spend(self, subject) -> int:
        return self._local_msat

    def has_unsettled_htlcs(self) -> bool:
        return bool(self._unsettled)

    def funding_txn_minimum_depth(self) -> int:
        return self.min_depth

    @property
    def peer_state(self):
        return PeerState.GOOD if self.peer_reachable else PeerState.DISCONNECTED


def _swap_manager():
    return SimpleNamespace(
        get_pending_swaps=lambda: [],
        percentage=0.3,
        mining_fee=0,
        get_fee_for_txbatcher=lambda: 0,
        get_provider_max_forward_amount=lambda: 1_000_000,
        get_min_amount=lambda: 200_000,
        get_swap=lambda ph: None,
    )


def _lnworker_with(channels):
    forced: List[bytes] = []
    by_txid = {c.funding_outpoint.txid: c for c in channels}
    adb = SimpleNamespace(
        get_tx_height=lambda txid: SimpleNamespace(
            conf=by_txid[txid].confs if txid in by_txid else 0))
    ln = SimpleNamespace(
        channels={c.channel_id: c for c in channels},
        swap_manager=_swap_manager(),
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


def _plugin(**config_overrides) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.discount")
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
    p._last_offers = {}
    p._suggest_errors = {}
    p._startup_grace_sec = 0.0
    cfg = dict(
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PREFERRED_PARTNERS="",
        INBOUND_LIQUIDITY_PARTNERS_STRICT=False,
        INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=True,
        INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED=True,
        # High enough that the auto-ban never fires inside these tests: what is
        # under test is the *discount*, and a banned peer would also vanish from
        # partner selection for an unrelated reason.
        INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS=99,
        INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN=60,
        INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN=360,
        INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN=False,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
        INBOUND_LIQUIDITY_MAX_CHANNELS=2,
        INBOUND_LIQUIDITY_OFFLINE_OPEN_ALLOWANCE=1,
    )
    cfg.update(config_overrides)
    p.config = SimpleNamespace(**cfg)
    seed_settings(p, **cfg)
    return p


def _tag_plugin_opened(wallet, *cids: str) -> None:
    wallet.db.put(PLUGIN_OPENED_CHANNELS_DB_KEY, list(cids))


def _offline_faults(p, wallet, node_id: str = NODE_A) -> int:
    row = p._load_peer_reliability(wallet).get(node_id.lower(), {})
    return int(row.get(PEER_OFFLINE_FAULTS_FIELD, 0) or 0)


def _age_offline_stamp(p, wallet, node_id: str = NODE_A, *, by: float = DAY + 60) -> None:
    """Push the offline fault's rate-limit stamp into the past, so the next scan
    counts a fresh day of being gone rather than being silenced."""
    data = p._load_peer_reliability(wallet)
    data[node_id.lower()][PEER_OFFLINE_FAULT_TS_FIELD] = time.time() - by
    p._save_peer_reliability(wallet, data)


# --- the offline tally ----------------------------------------------------
def test_tally_key_increments_a_named_counter() -> None:
    p, w = _plugin(), _FakeWallet()
    p._record_peer_fault(w, NODE_A, "peer offline", hard=True,
                         tally_key=PEER_OFFLINE_FAULTS_FIELD)
    assert _offline_faults(p, w) == 1


def test_tally_inherits_the_rate_limit() -> None:
    # A long outage must count once per window, not once per tick -- the tally is
    # evidence of DAYS gone, and it is the discount's threshold.
    p, w = _plugin(), _FakeWallet()
    for _ in range(5):
        p._record_peer_fault(w, NODE_A, "peer offline", hard=True,
                             rate_key=PEER_OFFLINE_FAULT_TS_FIELD,
                             rate_limit_sec=DAY,
                             tally_key=PEER_OFFLINE_FAULTS_FIELD)
    assert _offline_faults(p, w) == 1


def test_tally_survives_hand_edited_garbage() -> None:
    p, w = _plugin(), _FakeWallet()
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: "not-a-number"}
    p._save_peer_reliability(w, data)
    p._record_peer_fault(w, NODE_A, "peer offline", hard=True,
                         tally_key=PEER_OFFLINE_FAULTS_FIELD)
    assert _offline_faults(p, w) == 1


def test_watchdog_counts_one_offline_fault_per_day() -> None:
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    # Seen online once, so the next inactive reading is a genuine offline rather
    # than a peer we have simply never dialed (classify_peer_observation).
    chan.active = True
    p._scan_channel_health(w)
    chan.active = False
    p._scan_channel_health(w)
    p._scan_channel_health(w)                       # same window: no double count
    assert _offline_faults(p, w) == 1
    _age_offline_stamp(p, w)
    p._scan_channel_health(w)                       # a day later
    assert _offline_faults(p, w) == 2


def test_recovery_clears_the_tally_but_not_the_autoban_count() -> None:
    chan = _Chan(ChannelState.OPEN)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    chan.active = False
    p._scan_channel_health(w)
    _age_offline_stamp(p, w)
    p._scan_channel_health(w)
    assert _offline_faults(p, w) == 2
    chan.active = True                              # the peer answers again
    p._scan_channel_health(w)
    assert _offline_faults(p, w) == 0
    row = p._load_peer_reliability(w)[NODE_A.lower()]
    # The lifetime auto-ban tally is a different question and must not be healed.
    assert int(row["hard_fault_count"]) == 2
    # The rate-limit stamp goes with the tally, so a fresh outage faults at once
    # instead of being silenced by the previous one's stamp.
    assert PEER_OFFLINE_FAULT_TS_FIELD not in row
    chan.active = False
    p._scan_channel_health(w)
    assert _offline_faults(p, w) == 1


def test_not_observed_leaves_the_tally_alone() -> None:
    # Network down (graceful shutdown): _observe_peer returns None, so neither a
    # fault nor a clear. A healthy peer must never be faulted for OUR downtime,
    # and a dead one must not have its evidence wiped by it either.
    chan = _Chan(ChannelState.OPEN)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    p._scan_channel_health(w)
    chan.active = False
    p._scan_channel_health(w)
    _age_offline_stamp(p, w)
    p._scan_channel_health(w)
    assert _offline_faults(p, w) == 2
    w.network = SimpleNamespace(is_connected=lambda: False)
    _age_offline_stamp(p, w)
    p._scan_channel_health(w)
    assert _offline_faults(p, w) == 2               # unchanged in both directions


def test_clear_is_a_noop_for_an_unknown_peer() -> None:
    p, w = _plugin(), _FakeWallet()
    assert p._clear_peer_offline_faults(w, NODE_B) is False
    assert p._load_peer_reliability(w) == {}


# --- build_snapshot carries the discount inputs ---------------------------
def test_snapshot_carries_offline_fault_count() -> None:
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: OFFLINE_DISCOUNT_MIN_FAULTS}
    p._save_peer_reliability(w, data)
    snap = p.build_snapshot(w)
    (ch,) = snap.channels
    assert ch.offline_fault_count == OFFLINE_DISCOUNT_MIN_FAULTS
    assert ch.is_plugin_opened and not ch.is_pending
    assert channel_is_offline_discounted(ch)
    assert effective_channel_count(snap) == 0


def test_snapshot_carries_close_intent() -> None:
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    w.db.put(CLOSE_INTENT_DB_KEY, {CID_A: {"marked_ts": time.time(),
                                           "reason": "peer uptime 0%"}})
    (ch,) = p.build_snapshot(w).channels
    assert ch.close_intent
    assert channel_is_offline_discounted(ch)


def test_snapshot_marks_a_pending_channel_pending() -> None:
    # The in-flight-open guard, end to end: a FUNDED channel reads inactive, and
    # its peer even carries offline faults, but it must still hold its slot.
    chan = _Chan(ChannelState.FUNDED, active=False, confs=0, min_depth=3)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: 9}
    p._save_peer_reliability(w, data)
    snap = p.build_snapshot(w)
    (ch,) = snap.channels
    assert ch.is_pending
    assert not channel_is_offline_discounted(ch)
    assert effective_channel_count(snap) == 1


def test_snapshot_does_not_discount_a_user_opened_channel() -> None:
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    # No plugin-opened tag: the user opened this one by hand.
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: 9}
    p._save_peer_reliability(w, data)
    snap = p.build_snapshot(w)
    (ch,) = snap.channels
    assert not ch.is_plugin_opened
    assert not channel_is_offline_discounted(ch)
    assert effective_channel_count(snap) == 1


def test_snapshot_healthy_channel_counts_in_full() -> None:
    chan = _Chan(ChannelState.OPEN, active=True)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    snap = p.build_snapshot(w)
    (ch,) = snap.channels
    assert ch.offline_fault_count == 0 and not ch.close_intent
    assert effective_channel_count(snap) == 1


# --- the dead peer is not a candidate for the replacement ----------------
def test_discounted_peer_is_excluded_from_partner_selection() -> None:
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: OFFLINE_DISCOUNT_MIN_FAULTS}
    p._save_peer_reliability(w, data)
    assert p._offline_discounted_peer_ids(w) == frozenset({NODE_A.lower()})


def test_discounted_peer_excluded_even_with_the_per_peer_guard_off() -> None:
    # With the guard ON the dead peer is excluded anyway (it still holds a
    # non-closed channel). This is the case that needs its own exclusion: the
    # discount is what authorised the extra open, so it must not be spent
    # re-opening to the node that went dark.
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p = _plugin(INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    w = _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: OFFLINE_DISCOUNT_MIN_FAULTS}
    p._save_peer_reliability(w, data)

    async def _suggest(_wallet):
        return [bytes.fromhex(NODE_A), bytes.fromhex(NODE_B)]

    p._suggest_peers = _suggest
    res = asyncio.run(p._resolve_partners_detailed(w))
    assert all(not c.lower().startswith(NODE_A.lower()) for c in res.candidates)
    assert any(c.lower().startswith(NODE_B.lower()) for c in res.candidates)


def test_healthy_peer_is_not_excluded_as_discounted() -> None:
    chan = _Chan(ChannelState.OPEN, active=True)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    assert p._offline_discounted_peer_ids(w) == frozenset()


def test_no_lnworker_yields_no_discounted_peers() -> None:
    p, w = _plugin(), _FakeWallet(None)
    assert p._offline_discounted_peer_ids(w) == frozenset()


# --- the allowance setting -------------------------------------------------
def test_allowance_is_read_per_wallet_and_floored() -> None:
    p, w = _plugin(INBOUND_LIQUIDITY_OFFLINE_OPEN_ALLOWANCE=3), _FakeWallet()
    assert p._offline_open_allowance(w) == 3
    p.set_setting(w, "INBOUND_LIQUIDITY_OFFLINE_OPEN_ALLOWANCE", -4)
    assert p._offline_open_allowance(w) == 0


def test_allowance_defaults_to_the_shipped_value() -> None:
    p, w = _plugin(), _FakeWallet()
    p.set_setting(w, "INBOUND_LIQUIDITY_OFFLINE_OPEN_ALLOWANCE",
                  DEFAULT_OFFLINE_OPEN_ALLOWANCE)
    assert p._offline_open_allowance(w) == DEFAULT_OFFLINE_OPEN_ALLOWANCE
    assert p.read_config(w).offline_open_allowance == DEFAULT_OFFLINE_OPEN_ALLOWANCE


# --- the backoff notices --------------------------------------------------
def test_state_signature_changes_when_a_channel_becomes_discounted() -> None:
    # The open a freed slot allows must not wait out a backoff that thinks
    # nothing has changed since the last tick.
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    cfg = p.read_config(w)
    before = p._material_state_sig(p.build_snapshot(w), cfg)
    data = p._load_peer_reliability(w)
    data[NODE_A.lower()] = {PEER_OFFLINE_FAULTS_FIELD: OFFLINE_DISCOUNT_MIN_FAULTS}
    p._save_peer_reliability(w, data)
    after = p._material_state_sig(p.build_snapshot(w), cfg)
    assert before != after


def test_state_signature_changes_on_a_new_close_intent() -> None:
    chan = _Chan(ChannelState.OPEN, active=False)
    ln, _f = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    _tag_plugin_opened(w, CID_A)
    cfg = p.read_config(w)
    before = p._material_state_sig(p.build_snapshot(w), cfg)
    w.db.put(CLOSE_INTENT_DB_KEY, {CID_A: {"marked_ts": 1.0, "reason": "gone"}})
    after = p._material_state_sig(p.build_snapshot(w), cfg)
    assert before != after
