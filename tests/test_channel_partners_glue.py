"""Glue-level tests for channel-partner selection (the Electrum-facing layer):
the partner-list parsing, the one-time channel_peer migration, the
preferred-then-suggested resolution (incl. strict mode + banned), and the
_open_channel fallback loop that tries the next partner when one fails.

Also covers the two suggested-peer bugs: the lookup is offloaded off the asyncio
loop (Electrum's gossip path asserts it is not called from the loop thread) and
its failures are reported rather than swallowed into "no peer available"; several
suggestions are collected, not one; and a rejection that is only about the channel
size charges the peer no fault.

Heavy Electrum objects are faked; skipped if the plugin package can't be imported."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    MAX_SUGGESTED_PARTNERS,
    _parse_partner_list,
    _parse_banned_partners,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    OpenChannelAction,
)
from electrum.lnchannel import ChannelState  # type: ignore  # noqa: E402
from electrum.util import get_running_loop  # type: ignore  # noqa: E402

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
    # Per-wallet state the suggestion lookup writes to (object.__new__ skips
    # __init__, so it is wired explicitly here).
    p._suggest_errors = {}
    return p


def _resolve(p, wallet, **kwargs):
    """_resolve_channel_partners is a coroutine (the suggestion lookup has to be
    awaited so it can run OFF the asyncio loop); drive it from these sync tests."""
    return asyncio.run(p._resolve_channel_partners(wallet, **kwargs))


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
class _Wallet:
    """Minimal stand-in for an Electrum wallet. A plain class (not
    SimpleNamespace) because the plugin keys per-wallet state by the wallet
    object, and SimpleNamespace defines __eq__ and is therefore unhashable."""

    def __init__(self, lnworker) -> None:
        self.lnworker = lnworker


def _wallet_with_suggestion(node_id_hex):
    ln = SimpleNamespace(suggest_peer=lambda: bytes.fromhex(node_id_hex) if node_id_hex else None)
    return _Wallet(ln)


def test_resolve_prefers_then_suggested():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    wallet = _wallet_with_suggestion(PUB_B)
    assert _resolve(p, wallet) == [f"{PUB_A}@h:1", PUB_B]


def test_resolve_strict_ignores_suggestion():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1",
                INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    wallet = _wallet_with_suggestion(PUB_B)
    assert _resolve(p, wallet) == [f"{PUB_A}@h:1"]


def test_resolve_excludes_banned_suggestion():
    p = _plugin(INBOUND_LIQUIDITY_BANNED_PARTNERS=PUB_B)
    wallet = _wallet_with_suggestion(PUB_B)
    # The only candidate (the suggestion) is banned -> nothing to try.
    assert _resolve(p, wallet) == []


# --- one-channel-per-peer guard -------------------------------------------
def _fake_channel(node_id_hex, state=ChannelState.OPEN):
    return SimpleNamespace(node_id=bytes.fromhex(node_id_hex), get_state=lambda: state)


def _wallet(suggestion=None, channels=()):
    ln = SimpleNamespace(
        suggest_peer=lambda: bytes.fromhex(suggestion) if suggestion else None,
        channels={i: ch for i, ch in enumerate(channels)},
    )
    return _Wallet(ln)


def test_resolve_excludes_existing_peer_when_guard_on():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    wallet = _wallet(channels=[_fake_channel(PUB_A)])
    # We already have a (non-closed) channel with PUB_A -> it is excluded.
    assert _resolve(p, wallet) == [f"{PUB_B}@h:2"]


def test_resolve_excludes_existing_peer_from_suggestion():
    # The guard also drops an existing peer that arrives via Electrum's suggestion.
    p = _plugin()
    wallet = _wallet(suggestion=PUB_A, channels=[_fake_channel(PUB_A)])
    assert _resolve(p, wallet) == []


def test_resolve_keeps_existing_peer_when_guard_off():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2",
                INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    wallet = _wallet(channels=[_fake_channel(PUB_A)])
    assert _resolve(p, wallet) == [f"{PUB_A}@h:1", f"{PUB_B}@h:2"]


def test_resolve_guard_ignores_fully_closed_channel():
    # A CLOSED/REDEEMED channel no longer counts -> the peer is free to reopen to.
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    for closed in (ChannelState.CLOSED, ChannelState.REDEEMED):
        wallet = _wallet(channels=[_fake_channel(PUB_A, state=closed)])
        assert _resolve(p, wallet) == [f"{PUB_A}@h:1"]


def test_resolve_guard_counts_closing_channel():
    # A still-closing (not yet CLOSED) channel keeps the peer excluded.
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    wallet = _wallet(channels=[_fake_channel(PUB_A, state=ChannelState.CLOSING)])
    assert _resolve(p, wallet) == []


def test_no_partner_decline_reports_guard():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    wallet = _wallet(channels=[_fake_channel(PUB_A)])
    d = asyncio.run(p._no_partner_decline(wallet, OpenChannelAction(funding_sat=500_000, reason="x")))
    assert d.kind == "open" and d.amount_sat == 500_000
    assert "one-channel-per-peer" in d.reason


def test_no_partner_decline_reports_generic_when_no_partner_at_all():
    # Strict + no preferred + no suggestion: empty even without the guard.
    p = _plugin(INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    wallet = _wallet()
    d = asyncio.run(p._no_partner_decline(wallet, OpenChannelAction(funding_sat=1, reason="x")))
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
    wallet = _Wallet(ln)
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
    wallet = _Wallet(ln)
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    p._max_funding_minus_reserve = lambda w, node_id: 0  # below MIN_FUNDING_SAT
    p._get_password = lambda w: None

    _run_open(p, wallet, OpenChannelAction(funding_sat=500_000, reason="test"))

    # Funds shortfall is peer-independent: stop after the first connect.
    assert attempts == [f"{PUB_A}@h:1"]


# --- suggested-peer lookup (loop-safety, multiplicity, failure reporting) ---
# Bug 1: the lookup was wrapped in a bare `except Exception: node_id = None`, so a
# hard failure was indistinguishable from "the network has nobody for you".
# Bug 2: `suggest_peer()` was called inline from the evaluation coroutine, i.e. ON
# the asyncio loop -- but in gossip mode it reaches
# Network.run_from_another_thread(), which asserts it is NOT on that loop. The
# AssertionError was then swallowed by bug 1, so every open silently declined.
def _pubkey(byte: str) -> str:
    return "02" + byte * 32


def test_suggest_peers_runs_off_the_asyncio_loop():
    # The regression test for bug 2: assert from inside the callback that there is
    # no running loop, which is exactly what Electrum's own assert requires.
    on_loop = []

    def suggest_peer():
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return bytes.fromhex(PUB_A)

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))

    async def _drive():
        # Await from a coroutine, so a naive inline call *would* be on the loop.
        assert asyncio.get_running_loop() is not None
        return await p._suggest_peers(wallet, limit=1)

    assert asyncio.run(_drive()) == [bytes.fromhex(PUB_A)]
    assert on_loop and not any(on_loop), \
        "suggest_peer() must not be called from the asyncio thread"


def test_suggest_peers_reproduces_the_real_gossip_mode_assertion():
    # What Electrum actually raises when suggest_peer() is called on the loop
    # (Network.run_from_another_thread's assert). Off-loop it cannot happen, so
    # this only fires if the offload regresses -- and if it does, it must be
    # reported, never swallowed.
    loop_holder = {}

    def suggest_peer():
        # Verbatim Network.run_from_another_thread's guard, using Electrum's own
        # helper, so this fails exactly where the real gossip path would.
        assert get_running_loop() != loop_holder["loop"], \
            "must not be called from asyncio thread"
        return bytes.fromhex(PUB_B)

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))

    async def _drive():
        loop_holder["loop"] = asyncio.get_running_loop()
        return await p._suggest_peers(wallet, limit=1)

    # The worker thread has no loop of its own, so the assert passes there.
    assert asyncio.run(_drive()) == [bytes.fromhex(PUB_B)]


def test_suggest_peers_collects_up_to_the_cap_and_dedupes():
    # suggest_peer() returns ONE node per call, so several distinct suggestions
    # means calling it repeatedly. Duplicates are dropped, order preserved.
    sequence = [_pubkey("a"), _pubkey("b"), _pubkey("a"), _pubkey("c")]
    calls = {"n": 0}

    def suggest_peer():
        calls["n"] += 1
        idx = calls["n"] - 1
        return bytes.fromhex(sequence[idx]) if idx < len(sequence) else bytes.fromhex(_pubkey("c"))

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))
    out = asyncio.run(p._suggest_peers(wallet, limit=3))
    assert [n.hex() for n in out] == [_pubkey("a"), _pubkey("b"), _pubkey("c")]


def test_suggest_peers_stops_on_a_run_of_duplicates():
    # A small trampoline set repeats immediately; don't spin the full attempt
    # budget for nothing.
    calls = {"n": 0}

    def suggest_peer():
        calls["n"] += 1
        return bytes.fromhex(PUB_A)

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))
    out = asyncio.run(p._suggest_peers(wallet, limit=MAX_SUGGESTED_PARTNERS))
    assert out == [bytes.fromhex(PUB_A)]
    # 1 unique + a short run of duplicates, not limit*3 attempts.
    assert calls["n"] <= 5, f"spun {calls['n']} times on a single-node set"


def test_suggest_peers_stops_when_electrum_has_nothing():
    calls = {"n": 0}

    def suggest_peer():
        calls["n"] += 1
        return None

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))
    assert asyncio.run(p._suggest_peers(wallet, limit=MAX_SUGGESTED_PARTNERS)) == []
    assert calls["n"] == 1                     # asked once, got nothing, stopped
    assert wallet not in p._suggest_errors     # "nothing to suggest" is not a failure


@pytest.mark.parametrize("exc", [
    # regtest/trampoline mode: hardcoded_trampoline_nodes() is {} for regtest, so
    # Electrum's random.choice(list({}.values())) raises.
    IndexError("Cannot choose from an empty sequence"),
    # gossip mode, if the off-loop hop ever regresses.
    AssertionError("must not be called from asyncio thread"),
    # lnrater is None until the network is up.
    AttributeError("'NoneType' object has no attribute 'suggest_peer'"),
])
def test_suggest_peers_reports_failure_instead_of_swallowing_it(exc, caplog):
    # The regression test for bug 1: a failed lookup must be logged, remembered,
    # and emitted as a diagnostics event -- not silently become "no peer".
    def suggest_peer():
        raise exc

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))
    events = []
    p._diag_event = lambda w, **kw: events.append(kw)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(p._suggest_peers(wallet, limit=3)) == []

    assert type(exc).__name__ in p._suggest_errors[wallet]
    assert "suggested-peer lookup failed" in caplog.text
    assert events and events[0]["category"] == "error"
    assert events[0]["kind"] == "partners"
    assert type(exc).__name__ in events[0]["detail"]


def test_suggest_error_clears_after_a_good_lookup():
    outcomes = [None, bytes.fromhex(PUB_A)]

    def suggest_peer():
        value = outcomes[0]
        if isinstance(value, Exception):
            raise value
        return value

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer))
    p._diag_event = lambda w, **kw: None

    outcomes[0] = RuntimeError("boom")
    assert asyncio.run(p._suggest_peers(wallet, limit=1)) == []
    assert wallet in p._suggest_errors

    outcomes[0] = bytes.fromhex(PUB_A)
    assert asyncio.run(p._suggest_peers(wallet, limit=1)) == [bytes.fromhex(PUB_A)]
    assert wallet not in p._suggest_errors, "a good lookup must clear the stale error"


def test_resolve_offers_several_suggestions_after_the_preferred_ones():
    # The try-order the open loop walks: preferred first, then up to the cap of
    # distinct suggestions -- not just one.
    suggestions = [_pubkey(c) for c in "bcdef"]
    calls = {"n": 0}

    def suggest_peer():
        calls["n"] += 1
        idx = calls["n"] - 1
        return bytes.fromhex(suggestions[idx]) if idx < len(suggestions) else None

    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1",
                INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer, channels={}))
    assert _resolve(p, wallet) == [f"{PUB_A}@h:1"] + suggestions


def test_resolve_caps_suggestions_at_the_documented_maximum():
    calls = {"n": 0}

    def suggest_peer():
        calls["n"] += 1
        # An endless supply of distinct nodes.
        return bytes.fromhex("02" + f"{calls['n']:064x}"[:64])

    p = _plugin(INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer, channels={}))
    assert len(_resolve(p, wallet)) == MAX_SUGGESTED_PARTNERS


def test_no_partner_decline_names_a_failed_lookup():
    # The decline the user actually sees must say the lookup broke, rather than
    # blaming the absence of peers.
    def suggest_peer():
        raise IndexError("Cannot choose from an empty sequence")

    p = _plugin()
    wallet = _Wallet(SimpleNamespace(suggest_peer=suggest_peer, channels={}))
    p._diag_event = lambda w, **kw: None
    d = asyncio.run(p._no_partner_decline(wallet, OpenChannelAction(funding_sat=1, reason="x")))
    assert "suggested peer failed" in d.reason
    assert "IndexError" in d.reason
    assert "no reachable channel partner" not in d.reason


# --- per-peer fault charging across a multi-peer walk ----------------------
def _open_loop_wallet(*, fail_with):
    """A wallet whose peers all connect fine but reject the open with
    ``fail_with(connect_str)`` (return None to accept)."""
    async def add_peer(connect_str):
        return _FakePeer(connect_str)

    async def open_channel_with_peer(peer, funding_sat, push_sat, password):
        exc = fail_with(peer.pubkey.hex())
        if exc is not None:
            raise exc
        return _fake_chan(), object()

    return _Wallet(SimpleNamespace(
        lnpeermgr=SimpleNamespace(add_peer=add_peer),
        open_channel_with_peer=open_channel_with_peer,
        suggest_peer=lambda: None,
        channels={},
    ))


def _wire_open(p):
    p._max_funding_minus_reserve = lambda w, node_id: 500_000
    p._get_password = lambda w: None
    p._log_action = lambda wallet, **kw: None
    p.on_action_done = lambda w, m: None
    p._diag_event = lambda w, **kw: None
    p._record_action_event = lambda w, kind: None
    p._tag_plugin_opened_channel = lambda w, cid: None
    p._record_peer_success = lambda w, nid: None
    faults = []
    p._record_peer_fault = lambda w, nid, reason, **kw: faults.append((nid, reason, kw))
    return faults


def test_channel_size_rejection_records_no_fault_and_tries_the_next_peer():
    # A peer that will not do a channel this size is not a flaky peer: it must
    # keep a clean record (no hard fault, no soft fault, so no auto-ban tally and
    # no penalty), while the open moves on.
    def fail_with(pubkey):
        if pubkey == PUB_A:
            return Exception(
                "remote peer sent error [DO NOT TRUST THIS MESSAGE]: chan size of "
                "0.002 BTC is below min chan size of 0.02 BTC")
        return None

    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    faults = _wire_open(p)
    wallet = _open_loop_wallet(fail_with=fail_with)
    opened = []
    p._log_action = lambda wallet, **kw: opened.append(kw)

    _run_open(p, wallet, OpenChannelAction(funding_sat=500_000, reason="test"))

    assert faults == [], f"a channel-size rejection must not fault the peer: {faults}"
    assert opened and opened[0]["dest"] == PUB_B


def test_non_size_open_failure_still_records_a_hard_fault():
    def fail_with(pubkey):
        return Exception("remote peer sent error [DO NOT TRUST THIS MESSAGE]: internal error") \
            if pubkey == PUB_A else None

    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1, {PUB_B}@h:2")
    faults = _wire_open(p)
    wallet = _open_loop_wallet(fail_with=fail_with)

    _run_open(p, wallet, OpenChannelAction(funding_sat=500_000, reason="test"))

    assert len(faults) == 1
    node_id, reason, kwargs = faults[0]
    assert node_id == PUB_A and kwargs.get("hard") is True
    assert "channel open failed" in reason


def test_open_walks_every_suggestion_until_one_accepts():
    # End of the chain: many suggestions, all but the last rejecting on size, and
    # the open still succeeds -- with nobody faulted.
    accepted = _pubkey("f")
    suggestions = [_pubkey(c) for c in "abcdef"]
    calls = {"n": 0}

    def suggest_peer():
        calls["n"] += 1
        idx = calls["n"] - 1
        return bytes.fromhex(suggestions[idx]) if idx < len(suggestions) else None

    def fail_with(pubkey):
        return None if pubkey == accepted else Exception("funding amount too small")

    p = _plugin(INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    faults = _wire_open(p)
    wallet = _open_loop_wallet(fail_with=fail_with)
    wallet.lnworker.suggest_peer = suggest_peer
    opened = []
    p._log_action = lambda wallet, **kw: opened.append(kw)

    _run_open(p, wallet, OpenChannelAction(funding_sat=500_000, reason="test"))

    assert opened and opened[0]["dest"] == accepted, \
        "the open should have fallen through to the last suggestion"
    assert faults == []


# --- partner-resolution breakdown -----------------------------------------
# "No reachable channel partner" collapses half a dozen distinct situations into
# one sentence. These pin the arithmetic that tells them apart, which is what the
# decline's detail line and the Log tab actually show.
def _breakdown(p, wallet):
    res = asyncio.run(p._resolve_partners_detailed(wallet))
    return p._partner_breakdown(wallet, res)


def test_breakdown_reports_kept_over_total_for_both_lists():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    text = _breakdown(p, _wallet(suggestion=PUB_B))
    assert "preferred 1/1" in text
    assert "suggested 1/1" in text


def test_breakdown_names_the_guard_that_emptied_the_list():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1")
    text = _breakdown(p, _wallet(channels=[_fake_channel(PUB_A)]))
    assert "preferred 0/1" in text
    assert "1 already have a channel with" in text


def test_breakdown_names_banned_partners():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS=f"{PUB_A}@h:1",
                INBOUND_LIQUIDITY_BANNED_PARTNERS=PUB_A)
    text = _breakdown(p, _wallet())
    assert "1 banned" in text


def test_breakdown_says_strict_mode_suppressed_suggestions():
    p = _plugin(INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    text = _breakdown(p, _wallet(suggestion=PUB_B))
    assert "strict mode" in text


def test_breakdown_flags_an_unparseable_preferred_partner():
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_PARTNERS="oops-typo")
    assert "1 unparseable" in _breakdown(p, _wallet())


def test_breakdown_reports_the_routing_mode():
    # trampoline vs gossip are entirely different suggestion paths, and each
    # fails to produce a peer for its own reason.
    p = _plugin()
    wallet = _wallet()
    wallet.lnworker.uses_trampoline = lambda: True
    assert "routing trampoline" in _breakdown(p, wallet)
    wallet.lnworker.uses_trampoline = lambda: False
    assert "routing gossip" in _breakdown(p, wallet)


def test_breakdown_reports_unknown_routing_when_electrum_cannot_say():
    # _wallet()'s lnworker has no uses_trampoline at all -> reported honestly as
    # unknown rather than guessed.
    assert "routing unknown" in _breakdown(_plugin(), _wallet())


def test_breakdown_names_a_failed_suggestion_lookup():
    p = _plugin()

    def _boom():
        raise AssertionError("must not be called from the asyncio thread")
    wallet = _Wallet(SimpleNamespace(suggest_peer=_boom, channels={}))
    text = _breakdown(p, wallet)
    assert "suggestion lookup failed" in text and "AssertionError" in text


# --- the decline carries the breakdown ------------------------------------
def test_no_partner_decline_carries_the_breakdown_as_detail():
    p = _plugin(INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    wallet = _wallet()
    d = asyncio.run(p._no_partner_decline(
        wallet, OpenChannelAction(funding_sat=1, reason="x")))
    assert "no reachable channel partner" in d.reason
    assert d.detail is not None and d.detail.startswith("partner resolution: ")
    assert "preferred 0/0" in d.detail


def test_decline_reason_is_unchanged_by_the_breakdown():
    # The counts go in `detail`, NOT the reason, so the decline's dedup
    # signature (kind, channel_id, reason) is stable and a steady state is still
    # logged once rather than every tick.
    p = _plugin(INBOUND_LIQUIDITY_PARTNERS_STRICT=True)
    wallet = _wallet()
    action = OpenChannelAction(funding_sat=1, reason="x")
    first = asyncio.run(p._no_partner_decline(wallet, action))
    p.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS = f"{PUB_A}@h:1"
    p.config.INBOUND_LIQUIDITY_BANNED_PARTNERS = PUB_A
    second = asyncio.run(p._no_partner_decline(wallet, action))
    assert first.reason == second.reason        # same signature...
    assert first.detail != second.detail        # ...different explanation


def test_no_partner_decline_reuses_a_resolution_it_is_given():
    # _run_decision has already resolved once; passing the result through must
    # not trigger another suggest_peer round-trip (a thread hop + network call).
    calls = {"n": 0}

    def _suggest():
        calls["n"] += 1
        return None
    p = _plugin(INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER=False)
    wallet = _Wallet(SimpleNamespace(suggest_peer=_suggest, channels={}))
    res = asyncio.run(p._resolve_partners_detailed(wallet))
    before = calls["n"]
    asyncio.run(p._no_partner_decline(
        wallet, OpenChannelAction(funding_sat=1, reason="x"), res))
    assert calls["n"] == before
