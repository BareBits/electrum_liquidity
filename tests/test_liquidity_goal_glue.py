"""Glue-level tests for the undersized-channel replacement rule.

The pure decision is covered in ``test_liquidity_goal_engine``; what is tested
here is the Electrum-facing half:

  * the ConfigVar reaches the engine through ``read_config`` (and a garbage value
    cannot abort a tick);
  * ``_run_decision`` pre-checks that a channel partner would be available to
    reopen to, and exempts the closing channel's OWN peer from the
    one-channel-per-peer guard while doing so;
  * ``_execute`` routes the action into the single close path (``_request_close``)
    rather than closing directly, so cooperation-before-force and the daily close
    ceiling apply for free.

Heavy Electrum objects are faked. Skipped outside the electrum venv."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_LIQUIDITY_GOAL_SAT,
    LOG_DB_KEY,
    LiquidityPlugin,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    CloseChannelAction,
    LiquiditySnapshot,
    OpenChannelAction,
)

NODE_A = "02" + "aa" * 32       # peer of the channel we want to replace
NODE_B = "03" + "bb" * 32       # peer of the other channel we hold
NODE_C = "04" + "cc" * 32       # a free peer, suggested by Electrum
CID = "aa" * 32
CID_B = "bb" * 32


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
        self.lnworker = lnworker
        self.network = SimpleNamespace(asyncio_loop=None, is_connected=lambda: True)

    def basename(self) -> str:
        return "test-wallet"

    def save_db(self) -> None:
        pass


class _Chan:
    def __init__(self, *, cid=CID, node_id=NODE_A, state=None) -> None:
        from electrum.lnchannel import ChannelState
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = ChannelState.OPEN if state is None else state
        self.lnworker = None

    def get_state(self):
        return self.state

    def is_active(self):
        return True

    @property
    def peer_state(self):
        from electrum.lnchannel import PeerState
        return PeerState.GOOD


def _lnworker_with(channels):
    closed: List[bytes] = []
    forced: List[bytes] = []

    async def _close(cid):
        closed.append(cid)
        return "coop-txid"

    by_id = {c.channel_id: c for c in channels}
    ln = SimpleNamespace(
        channels=by_id,
        get_channel_by_id=by_id.get,
        close_channel=_close,
        schedule_force_closing=lambda cid: forced.append(cid),
        lnpeermgr=SimpleNamespace(get_peer_by_pubkey=lambda pk: object()),
    )
    for c in channels:
        c.lnworker = ln
    return ln, closed, forced


def _plugin(**overrides) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.goal")
    p._coop_closing = {}
    p._coop_close_cooldown_until = {}
    p._close_capped_logged = {}
    p._last_decline_sigs = {}
    p._tick_status = {}
    p._suggest_errors = {}
    p._started_at = {}
    p._peer_seen_online = {}
    p._startup_grace_sec = 0.0
    cfg = dict(
        INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
        INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=60_000,
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=10_000,
        INBOUND_LIQUIDITY_MAX_CHANNELS=2,
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.9,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT=25_000,
        INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT=0,
        INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY=True,
        INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY=5,
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
        INBOUND_LIQUIDITY_GOAL_SAT=500_000,
        INBOUND_LIQUIDITY_PREFERRED_NPUBS="",
        INBOUND_LIQUIDITY_BANNED_NPUBS="",
        INBOUND_LIQUIDITY_PREFERRED_PARTNERS="",
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PARTNERS_STRICT=False,
        INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=True,
        INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED=False,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
        INBOUND_LIQUIDITY_DIAG_LOG_ENABLED=False,
    )
    cfg.update(overrides)
    p.config = SimpleNamespace(**cfg)
    return p


def _action(cid=CID, short_id="100x1x0", capacity=200_000) -> CloseChannelAction:
    return CloseChannelAction(channel_id=cid, short_id=short_id,
                              capacity_sat=capacity,
                              reason="below the liquidity goal")


# --- config -> engine -----------------------------------------------------
def test_read_config_passes_the_goal_through() -> None:
    p = _plugin(INBOUND_LIQUIDITY_GOAL_SAT=750_000)
    assert p.read_config().liquidity_goal_sat == 750_000


def test_goal_is_clamped_non_negative() -> None:
    assert _plugin(INBOUND_LIQUIDITY_GOAL_SAT=-5).read_config().liquidity_goal_sat == 0


def test_garbage_goal_falls_back_to_the_shipped_default() -> None:
    # Read on every tick: a hand-edited config must not be able to raise and take
    # down the whole evaluation.
    p = _plugin(INBOUND_LIQUIDITY_GOAL_SAT="not-a-number")
    assert p.read_config().liquidity_goal_sat == DEFAULT_LIQUIDITY_GOAL_SAT


def test_missing_config_var_falls_back_to_the_shipped_default() -> None:
    p = _plugin()
    del p.config.INBOUND_LIQUIDITY_GOAL_SAT
    assert p.read_config().liquidity_goal_sat == DEFAULT_LIQUIDITY_GOAL_SAT


def test_shipped_default_is_active_not_off() -> None:
    assert DEFAULT_LIQUIDITY_GOAL_SAT == 100_000


def test_state_dict_records_the_goal() -> None:
    p = _plugin()
    snap = LiquiditySnapshot(onchain_spendable_sat=0, channels=(),
                             swap_percentage_fee=None,
                             provider_max_reverse_sat=None,
                             provider_min_amount_sat=None)
    state = p._state_dict(snap, p.read_config())
    assert state["config"]["liquidity_goal_sat"] == 500_000


# --- execution routes through the single close path -----------------------
def test_execute_routes_to_request_close() -> None:
    """The action must NOT close directly: _request_close is what enforces
    cooperation-before-force, the daily ceiling and the WE_ARE_TOXIC refusal."""
    chan = _Chan()
    ln, closed, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    seen = {}
    p._request_close = lambda *a, **kw: seen.update(args=a, kwargs=kw)
    asyncio.run(p._execute(w, _action()))
    assert seen, "close was not routed through _request_close"
    assert seen["args"][1] is chan
    assert seen["args"][2] == CID
    assert "liquidity goal" in seen["kwargs"]["reason"]


def test_execute_writes_no_log_entry_before_the_close_is_attempted() -> None:
    """_request_close may legitimately do nothing this tick (the daily ceiling
    defers it, or a cooperative close is already negotiating). An entry written
    up front would claim a close that never happened, so the close paths do the
    logging and this one only hands them the arithmetic."""
    chan = _Chan()
    ln, _, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    seen = {}
    p._request_close = lambda *a, **kw: seen.update(kw)
    asyncio.run(p._execute(w, _action()))
    assert w.db.get(LOG_DB_KEY, []) == []
    # ...and the justification for the close reaches the path that will log it.
    assert "500000" in seen["detail"]      # the goal
    assert "200000" in seen["detail"]      # the channel's capacity


def test_cooperative_close_entry_carries_the_goal_arithmetic() -> None:
    # The real entry, written once the close actually completes, has to explain
    # WHY this channel was closed -- not merely that it was.
    chan = _Chan()
    ln, closed, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)

    async def _go():
        await p._execute(w, _action())
        for _ in range(4):
            await asyncio.sleep(0)
    asyncio.run(_go())
    entries = [e for e in w.db.get(LOG_DB_KEY, [])
               if e.get("category") == "action" and e.get("kind") == "close"]
    assert len(entries) == 1
    assert "liquidity goal" in entries[0]["reason"]
    assert "500000" in entries[0]["detail"]
    assert "200000" in entries[0]["detail"]


def test_execute_reaches_a_real_cooperative_close() -> None:
    # End to end through the real _request_close, with a reachable peer: the
    # close must be cooperative, never a force-close.
    chan = _Chan()
    ln, closed, forced = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)

    async def _go():
        await p._execute(w, _action())
        # The cooperative close is launched as a background task (it is an async
        # negotiation); yield so it actually runs before we assert.
        for _ in range(4):
            await asyncio.sleep(0)
    asyncio.run(_go())
    assert closed == [chan.channel_id]
    assert forced == [], "a reachable peer must never be force-closed"


def test_execute_is_a_no_op_when_the_channel_vanished() -> None:
    # Closed or forgotten between snapshot and execution.
    ln, closed, forced = _lnworker_with([])
    p, w = _plugin(), _FakeWallet(ln)
    asyncio.run(p._execute(w, _action()))
    assert closed == [] and forced == []
    assert w.db.get(LOG_DB_KEY, []) == []


def test_daily_close_ceiling_defers_the_replacement() -> None:
    chan = _Chan()
    ln, closed, forced = _lnworker_with([chan])
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=1), _FakeWallet(ln)
    p._record_action_event(w, "close")      # today's one close is spent
    asyncio.run(p._execute(w, _action()))
    assert p._coop_closing.get(w, set()) == set()
    assert forced == []
    # Nothing was closed, so nothing may be logged as closed.
    assert [e for e in w.db.get(LOG_DB_KEY, [])
            if e.get("category") == "action" and e.get("kind") == "close"] == []


# --- the partner pre-check ------------------------------------------------
def _run_decision(p, w, action, *, suggested):
    """Drive _run_decision with an engine result carrying just ``action``."""
    async def _suggest(_w, *a, **kw):
        return [bytes.fromhex(n) for n in suggested]
    p._suggest_peers = _suggest
    p._execute = _noop_execute(p)
    snap = LiquiditySnapshot(onchain_spendable_sat=510_000, channels=(),
                             swap_percentage_fee=None,
                             provider_max_reverse_sat=None,
                             provider_min_amount_sat=None)
    cfg = p.read_config()
    p._evaluate_result = SimpleNamespace(actions=(action,), declines=(), frozen=None)
    import electrum.plugins.inbound_liquidity as mod
    orig = mod.evaluate
    mod.evaluate = lambda s, c: p._evaluate_result
    try:
        asyncio.run(p._run_decision(w, snap, cfg, None))
    finally:
        mod.evaluate = orig


def _noop_execute(p):
    executed = p._executed = []

    async def _ex(wallet, action, state=None, transport=None, candidates=None):
        executed.append(action)
    return _ex


def test_close_is_executed_when_a_partner_is_available() -> None:
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p, w = _plugin(), _FakeWallet(ln)
    _run_decision(p, w, _action(), suggested=[NODE_C])
    assert len(p._executed) == 1
    assert isinstance(p._executed[0], CloseChannelAction)


def test_close_is_declined_when_no_partner_is_available() -> None:
    """No partner to reopen to means the close would destroy this channel's
    inbound liquidity and buy nothing."""
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p, w = _plugin(), _FakeWallet(ln)
    _run_decision(p, w, _action(), suggested=[])
    assert p._executed == []
    entries = [e for e in w.db.get(LOG_DB_KEY, [])
               if e.get("category") == "decline" and e.get("kind") == "close"]
    assert len(entries) == 1
    assert "no channel partner is available" in entries[0]["reason"]


def test_closing_channels_own_peer_is_exempt_from_the_one_per_peer_guard() -> None:
    """The guard excludes peers we already hold a channel with -- including the
    peer of the channel we are about to close. Left in place it would veto the
    very close that frees it, so on a small peer set no replacement could ever
    happen. Only that one peer is exempted."""
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p, w = _plugin(), _FakeWallet(ln)
    # NODE_A is the closing channel's own peer -> must be usable again.
    _run_decision(p, w, _action(), suggested=[NODE_A])
    assert len(p._executed) == 1


def test_other_held_peers_stay_excluded_by_the_guard() -> None:
    # NODE_B belongs to the channel we are KEEPING, so the guard must still
    # exclude it: the exemption is scoped to the closing channel alone.
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p, w = _plugin(), _FakeWallet(ln)
    _run_decision(p, w, _action(), suggested=[NODE_B])
    assert p._executed == []


def test_guard_off_means_every_peer_is_available() -> None:
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p = _plugin(INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    w = _FakeWallet(ln)
    _run_decision(p, w, _action(), suggested=[NODE_B])
    assert len(p._executed) == 1


def test_banned_partner_does_not_count_as_available() -> None:
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p = _plugin(INBOUND_LIQUIDITY_BANNED_PARTNERS=NODE_C)
    w = _FakeWallet(ln)
    _run_decision(p, w, _action(), suggested=[NODE_C])
    assert p._executed == []


def test_resolve_partners_ignore_peers_narrows_only_the_named_peer() -> None:
    # Direct test of the mechanism the pre-check relies on.
    from electrum.plugins.inbound_liquidity.liquidity_manager import normalize_node_id
    chan, other = _Chan(), _Chan(cid=CID_B, node_id=NODE_B)
    ln, _, _ = _lnworker_with([chan, other])
    p, w = _plugin(), _FakeWallet(ln)

    async def _suggest(_w, *a, **kw):
        return [bytes.fromhex(NODE_A), bytes.fromhex(NODE_B)]
    p._suggest_peers = _suggest

    both_held = asyncio.run(p._resolve_partners_detailed(w))
    assert both_held.candidates == ()
    exempt_a = asyncio.run(p._resolve_partners_detailed(
        w, ignore_peers=frozenset([normalize_node_id(NODE_A)])))
    assert [normalize_node_id(c) for c in exempt_a.candidates] == [
        normalize_node_id(NODE_A)]


# --- an open action still behaves exactly as before ------------------------
def test_open_action_still_gets_its_own_partner_resolution() -> None:
    """Regression guard: the CloseChannelAction branch must not have changed how
    an OpenChannelAction is pre-resolved (it passes its candidates to _execute;
    the close branch passes None and re-reads the channel itself)."""
    chan = _Chan()
    ln, _, _ = _lnworker_with([chan])
    p, w = _plugin(), _FakeWallet(ln)
    captured = {}

    async def _ex(wallet, action, state=None, transport=None, candidates=None):
        captured["candidates"] = candidates
    p._execute = _ex
    _run_decision_open(p, w, suggested=[NODE_C])
    assert captured["candidates"] and NODE_C in captured["candidates"][0]


def _run_decision_open(p, w, *, suggested):
    async def _suggest(_w, *a, **kw):
        return [bytes.fromhex(n) for n in suggested]
    p._suggest_peers = _suggest
    action = OpenChannelAction(funding_sat=500_000, reason="test")
    snap = LiquiditySnapshot(onchain_spendable_sat=510_000, channels=(),
                             swap_percentage_fee=None,
                             provider_max_reverse_sat=None,
                             provider_min_amount_sat=None)
    import electrum.plugins.inbound_liquidity as mod
    orig = mod.evaluate
    mod.evaluate = lambda s, c: SimpleNamespace(
        actions=(action,), declines=(), frozen=None)
    try:
        asyncio.run(p._run_decision(w, snap, p.read_config(), None))
    finally:
        mod.evaluate = orig
