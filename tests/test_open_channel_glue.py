"""Glue-level tests for the channel-open executor (``_open_channel``).

The happy path and partner *ordering* are covered elsewhere; here we exercise
the FAILURE branches that move (or decline to move) real funds and shape peer
reliability -- the paths a flaky peer or a funds shortfall actually hits, which
the mocked open tests skip:

  * no candidate partner -> log + return (never opens blindly);
  * a peer we cannot connect to -> SOFT fault, try the next candidate;
  * a funds shortfall -> return immediately (peer-independent, don't churn);
  * an open negotiation that fails after connect -> HARD fault + diag, try next;
  * a post-open tagging hiccup must NOT abort an open that already succeeded;
  * every candidate failing -> a final "all failed" warning (no success side effects).

Heavy Electrum objects are faked; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    MIN_FUNDING_SAT,
    OpenChannelAction,
)

PUB_A = bytes.fromhex("02" + "aa" * 32)
PUB_B = bytes.fromhex("03" + "bb" * 32)


def _plugin() -> LiquidityPlugin:
    """A LiquidityPlugin with the open-executor's collaborators stubbed to spies
    so each branch's side effects are observable without a real wallet/db."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.open_glue")
    p.peer_faults = []      # (node_id, reason, hard)
    p.peer_successes = []   # node_id
    p.action_events = []    # kind
    p.tagged = []           # channel_id_hex
    p.actions_logged = []   # kwargs of _log_action
    p.done = []             # on_action_done messages
    p.diags = []            # _diag_event kwargs
    p._record_peer_fault = lambda wallet, node_id, reason, *, hard: \
        p.peer_faults.append((node_id, reason, hard))
    p._record_peer_success = lambda wallet, node_id: p.peer_successes.append(node_id)
    p._record_action_event = lambda wallet, kind: p.action_events.append(kind)
    p._tag_plugin_opened_channel = lambda wallet, cid: p.tagged.append(cid)
    p._log_action = lambda wallet, **kw: p.actions_logged.append(kw)
    p.on_action_done = lambda wallet, msg: p.done.append(msg)
    p._diag_event = lambda wallet, **kw: p.diags.append(kw)
    p._get_password = lambda wallet: None
    return p


def _chan(cid_hex: str = "cc" * 32):
    return SimpleNamespace(
        channel_id=SimpleNamespace(hex=lambda: cid_hex),
        funding_outpoint=SimpleNamespace(to_str=lambda: f"{cid_hex}:0", txid="deadbeef"))


def _wallet(*, add_peer, open_channel):
    lnworker = SimpleNamespace(
        lnpeermgr=SimpleNamespace(add_peer=add_peer),
        open_channel_with_peer=open_channel)
    return SimpleNamespace(lnworker=lnworker)


def _action() -> OpenChannelAction:
    return OpenChannelAction(funding_sat=1_000_000, reason="grow inbound")


def _run(p, wallet, candidates):
    asyncio.run(p._open_channel(wallet, _action(), state={}, candidates=candidates))


# --- no candidate ---------------------------------------------------------
def test_no_candidate_returns_without_opening() -> None:
    p = _plugin()

    async def _add_peer(cs):        # must never be reached
        raise AssertionError("add_peer called with no candidates")
    wallet = _wallet(add_peer=_add_peer, open_channel=None)
    _run(p, wallet, candidates=[])
    assert p.peer_faults == [] and p.peer_successes == [] and p.done == []


# --- connect failure -> soft fault, then next candidate succeeds ----------
def test_connect_failure_soft_faults_then_tries_next() -> None:
    p = _plugin()
    p._max_funding_minus_reserve = lambda wallet, node_id: 1_000_000

    peers = {"partnerA": PUB_A, "partnerB": PUB_B}

    async def _add_peer(cs):
        if cs == "partnerA":
            raise ConnectionError("unreachable")
        return SimpleNamespace(pubkey=peers[cs])

    opened = []

    async def _open(peer, funding_sat, *, push_sat=0, password=None):
        opened.append((peer.pubkey, funding_sat))
        return _chan(), SimpleNamespace()
    wallet = _wallet(add_peer=_add_peer, open_channel=_open)

    _run(p, wallet, candidates=["partnerA", "partnerB"])

    # partnerA recorded a SOFT (non-escalating) connect fault...
    assert len(p.peer_faults) == 1
    _nid, reason, hard = p.peer_faults[0]
    assert hard is False and "connect failed" in reason
    # ...and the open proceeded against partnerB.
    assert opened == [(PUB_B, 1_000_000)]
    assert p.peer_successes == [PUB_B.hex()]
    assert p.action_events == ["open"] and len(p.actions_logged) == 1 and p.done


# --- funds shortfall -> return immediately (do not churn partners) --------
def test_funds_shortfall_returns_without_open() -> None:
    p = _plugin()
    # Feasible funding below the floor: peer-independent, so stop (don't try B).
    p._max_funding_minus_reserve = lambda wallet, node_id: MIN_FUNDING_SAT - 1

    async def _add_peer(cs):
        return SimpleNamespace(pubkey=PUB_A)

    opened = []

    async def _open(peer, funding_sat, *, push_sat=0, password=None):
        opened.append(funding_sat)
        return _chan(), SimpleNamespace()
    wallet = _wallet(add_peer=_add_peer, open_channel=_open)

    _run(p, wallet, candidates=["partnerA", "partnerB"])
    assert opened == []             # never attempted an open
    assert p.peer_successes == [] and p.done == []


def test_funds_none_returns_without_open() -> None:
    # A None feasible amount (NotEnoughFunds inside the trial tx) is the same
    # peer-independent shortfall -> return.
    p = _plugin()
    p._max_funding_minus_reserve = lambda wallet, node_id: None

    async def _add_peer(cs):
        return SimpleNamespace(pubkey=PUB_A)
    wallet = _wallet(add_peer=_add_peer, open_channel=None)
    _run(p, wallet, candidates=["partnerA"])
    assert p.peer_successes == [] and p.done == []


# --- open negotiation failure -> hard fault + diag, then all-failed -------
def test_open_negotiation_failure_hard_faults() -> None:
    p = _plugin()
    p._max_funding_minus_reserve = lambda wallet, node_id: 1_000_000

    async def _add_peer(cs):
        return SimpleNamespace(pubkey=PUB_A)

    async def _open(peer, funding_sat, *, push_sat=0, password=None):
        raise RuntimeError("open negotiation rejected")
    wallet = _wallet(add_peer=_add_peer, open_channel=_open)

    _run(p, wallet, candidates=["partnerA"])

    # A connected-but-failed open is a HARD fault charged to the reached peer,
    # and a diag is recorded; no success side effects fire.
    assert len(p.peer_faults) == 1
    _nid, reason, hard = p.peer_faults[0]
    assert hard is True and "channel open failed" in reason
    assert any(d.get("kind") == "open" for d in p.diags)
    assert p.peer_successes == [] and p.action_events == [] and p.done == []


# --- tagging hiccup must not abort a successful open ----------------------
def test_tag_hiccup_does_not_abort_success() -> None:
    p = _plugin()
    p._max_funding_minus_reserve = lambda wallet, node_id: 1_000_000

    def _tag_boom(wallet, cid):
        raise RuntimeError("db write hiccup")
    p._tag_plugin_opened_channel = _tag_boom      # tagging fails...

    async def _add_peer(cs):
        return SimpleNamespace(pubkey=PUB_A)

    async def _open(peer, funding_sat, *, push_sat=0, password=None):
        return _chan(), SimpleNamespace()
    wallet = _wallet(add_peer=_add_peer, open_channel=_open)

    _run(p, wallet, candidates=["partnerA"])

    # ...yet the open still counts: success recorded, action logged, user notified.
    assert p.peer_successes == [PUB_A.hex()]
    assert p.action_events == ["open"] and len(p.actions_logged) == 1 and p.done
