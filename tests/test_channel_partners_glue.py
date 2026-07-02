"""Glue-level tests for channel-partner selection (the Electrum-facing layer):
the partner-list parsing, the one-time channel_peer migration, the
preferred-then-suggested resolution (incl. strict mode + banned), and the
_open_channel fallback loop that tries the next partner when one fails. Heavy
Electrum objects are faked; skipped if the plugin package can't be imported."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    _parse_partner_list,
    _parse_banned_partners,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    OpenChannelAction,
)
from electrum.lnchannel import ChannelState  # type: ignore  # noqa: E402

PUB_A = "02" + "aa" * 32
PUB_B = "03" + "bb" * 32
PUB_C = "02" + "cc" * 32


def _plugin(**config) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.partners")
    base = dict(
        INBOUND_LIQUIDITY_CHANNEL_PEER="",
        INBOUND_LIQUIDITY_PREFERRED_PARTNERS="",
        INBOUND_LIQUIDITY_BANNED_PARTNERS="",
        INBOUND_LIQUIDITY_PARTNERS_STRICT=False,
        INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=True,
    )
    base.update(config)
    p.config = SimpleNamespace(**base)
    return p


# --- parsing --------------------------------------------------------------
def test_parse_partner_list_orders_and_dedupes():
    raw = f"{PUB_A}@h:1\n{PUB_B}@h:2, {PUB_A}@h:3"
    assert _parse_partner_list(raw) == [f"{PUB_A}@h:1", f"{PUB_B}@h:2"]


def test_parse_banned_partners_normalizes_to_pubkeys():
    assert _parse_banned_partners(f"{PUB_A.upper()}@h:1, {PUB_B}") == frozenset({PUB_A, PUB_B})


# --- migration ------------------------------------------------------------
def test_migrate_channel_peer_prepends_and_clears():
    p = _plugin(INBOUND_LIQUIDITY_CHANNEL_PEER=f"{PUB_A}@h:1",
                INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_B}@h:2")
    p._migrate_channel_peer()
    assert _parse_partner_list(p.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS) == [
        f"{PUB_A}@h:1", f"{PUB_B}@h:2"]
    assert p.config.INBOUND_LIQUIDITY_CHANNEL_PEER == ""
    # Idempotent: a second run is a no-op.
    p._migrate_channel_peer()
    assert _parse_partner_list(p.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS) == [
        f"{PUB_A}@h:1", f"{PUB_B}@h:2"]


def test_migrate_channel_peer_skips_duplicate():
    p = _plugin(INBOUND_LIQUIDITY_CHANNEL_PEER=f"{PUB_A}@h:1",
                INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:9")
    p._migrate_channel_peer()
    # Already present (by pubkey) -> not duplicated, just cleared.
    assert _parse_partner_list(p.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS) == [f"{PUB_A}@h:9"]
    assert p.config.INBOUND_LIQUIDITY_CHANNEL_PEER == ""


# --- resolution -----------------------------------------------------------
def _wallet_with_suggestion(node_id_hex):
    ln = SimpleNamespace(suggest_peer=lambda: bytes.fromhex(node_id_hex) if node_id_hex else None)
    return SimpleNamespace(lnworker=ln)


def test_resolve_prefers_then_suggested():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    wallet = _wallet_with_suggestion(PUB_B)
    assert p._resolve_channel_partners(wallet) == [f"{PUB_A}@h:1", PUB_B]


def test_resolve_strict_ignores_suggestion():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1",
                INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    wallet = _wallet_with_suggestion(PUB_B)
    assert p._resolve_channel_partners(wallet) == [f"{PUB_A}@h:1"]


def test_resolve_excludes_banned_suggestion():
    p = _plugin(INBOUND_LIQUIDITY_BANNED_PARTNERS=PUB_B)
    wallet = _wallet_with_suggestion(PUB_B)
    # The only candidate (the suggestion) is banned -> nothing to try.
    assert p._resolve_channel_partners(wallet) == []


# --- one-channel-per-peer guard -------------------------------------------
def _fake_channel(node_id_hex, state=ChannelState.OPEN):
    return SimpleNamespace(node_id=bytes.fromhex(node_id_hex), get_state=lambda: state)


def _wallet(suggestion=None, channels=()):
    ln = SimpleNamespace(
        suggest_peer=lambda: bytes.fromhex(suggestion) if suggestion else None,
        channels={i: ch for i, ch in enumerate(channels)},
    )
    return SimpleNamespace(lnworker=ln)


def test_resolve_excludes_existing_peer_when_guard_on():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    wallet = _wallet(channels=[_fake_channel(PUB_A)])
    # We already have a (non-closed) channel with PUB_A -> it is excluded.
    assert p._resolve_channel_partners(wallet) == [f"{PUB_B}@h:2"]


def test_resolve_excludes_existing_peer_from_suggestion():
    # The guard also drops an existing peer that arrives via Electrum's suggestion.
    p = _plugin()
    wallet = _wallet(suggestion=PUB_A, channels=[_fake_channel(PUB_A)])
    assert p._resolve_channel_partners(wallet) == []


def test_resolve_keeps_existing_peer_when_guard_off():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2",
                INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    wallet = _wallet(channels=[_fake_channel(PUB_A)])
    assert p._resolve_channel_partners(wallet) == [f"{PUB_A}@h:1", f"{PUB_B}@h:2"]


def test_resolve_guard_ignores_fully_closed_channel():
    # A CLOSED/REDEEMED channel no longer counts -> the peer is free to reopen to.
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    for closed in (ChannelState.CLOSED, ChannelState.REDEEMED):
        wallet = _wallet(channels=[_fake_channel(PUB_A, state=closed)])
        assert p._resolve_channel_partners(wallet) == [f"{PUB_A}@h:1"]


def test_resolve_guard_counts_closing_channel():
    # A still-closing (not yet CLOSED) channel keeps the peer excluded.
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    wallet = _wallet(channels=[_fake_channel(PUB_A, state=ChannelState.CLOSING)])
    assert p._resolve_channel_partners(wallet) == []


def test_no_partner_decline_reports_guard():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    wallet = _wallet(channels=[_fake_channel(PUB_A)])
    d = p._no_partner_decline(wallet, OpenChannelAction(funding_sat=500_000, reason="x"))
    assert d.kind == "open" and d.amount_sat == 500_000
    assert "one-channel-per-peer" in d.reason


def test_no_partner_decline_reports_generic_when_no_partner_at_all():
    # Strict + no preferred + no suggestion: empty even without the guard.
    p = _plugin(INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    wallet = _wallet()
    d = p._no_partner_decline(wallet, OpenChannelAction(funding_sat=1, reason="x"))
    assert "no reachable channel partner" in d.reason


# --- open fallback loop ---------------------------------------------------
class _FakePeer:
    def __init__(self, connect_str):
        self.pubkey = bytes.fromhex(connect_str.split("@", 1)[0])


def _fake_chan():
    outpoint = SimpleNamespace(to_str=lambda: "txid:0", txid="txid")
    return SimpleNamespace(funding_outpoint=outpoint)


def _run_open(p, wallet, action):
    asyncio.run(p._open_channel(wallet, action))


def test_open_channel_falls_back_to_next_partner():
    opened = {}

    async def add_peer(connect_str):
        if connect_str.startswith(PUB_A):
            raise ConnectionError("offline")
        return _FakePeer(connect_str)

    async def open_channel_with_peer(peer, funding_sat, push_sat, password):
        opened["pubkey"] = peer.pubkey.hex()
        opened["funding_sat"] = funding_sat
        return _fake_chan(), object()

    ln = SimpleNamespace(
        lnpeermgr=SimpleNamespace(add_peer=add_peer),
        open_channel_with_peer=open_channel_with_peer,
        suggest_peer=lambda: None,
    )
    wallet = SimpleNamespace(lnworker=ln)
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    # Isolate the fallback logic from the real funding-tx math / password / log.
    p._max_funding_minus_reserve = lambda w, node_id: 500_000
    p._get_password = lambda w: None
    logged = []
    p._log_action = lambda wallet, **kw: logged.append(kw)
    p.on_action_done = lambda w, m: None

    _run_open(p, wallet, OpenChannelAction(funding_sat=500_000, reason="test"))

    # First partner (PUB_A) failed to connect; opened against the second (PUB_B).
    assert opened["pubkey"] == PUB_B
    assert opened["funding_sat"] == 500_000
    assert logged and logged[0]["dest"] == PUB_B


def test_open_channel_aborts_on_insufficient_funds_without_trying_others():
    attempts = []

    async def add_peer(connect_str):
        attempts.append(connect_str)
        return _FakePeer(connect_str)

    async def open_channel_with_peer(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("should not open when funds are insufficient")

    ln = SimpleNamespace(
        lnpeermgr=SimpleNamespace(add_peer=add_peer),
        open_channel_with_peer=open_channel_with_peer,
        suggest_peer=lambda: None,
    )
    wallet = SimpleNamespace(lnworker=ln)
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    p._max_funding_minus_reserve = lambda w, node_id: 0  # below MIN_FUNDING_SAT
    p._get_password = lambda w: None

    _run_open(p, wallet, OpenChannelAction(funding_sat=500_000, reason="test"))

    # Funds shortfall is peer-independent: stop after the first connect.
    assert attempts == [f"{PUB_A}@h:1"]
