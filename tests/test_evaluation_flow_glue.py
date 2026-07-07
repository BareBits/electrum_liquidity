"""Glue-level tests for the evaluation orchestration between the rules engine and
the executor: ``_run_decision`` (candidate pre-resolution, first-class
no-partner declines, decline logging, action dispatch), the brief offer-await
poll, and the URL-mode swap session. The nostr-mode session builds a real
transport and is left to the e2e rig. Skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

import electrum.plugins.inbound_liquidity as pkg  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    OpenChannelAction,
    ReverseSwapAction,
)


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.evalflow")
    p.config = SimpleNamespace(SWAPSERVER_URL=None)
    return p


def _snapshot() -> SimpleNamespace:
    # Only the fields _run_decision's log line + engine call touch.
    return SimpleNamespace(
        onchain_spendable_sat=0, channels=(), pending_channel_count=0,
        inflight_swap_count=0, provider_offers=())


def _wire_common_spies(p):
    p._state_dict = lambda snapshot, config: {}
    p._filter_new_declines = lambda wallet, declines: declines   # log them all
    logged_declines = []
    p._log_decline = lambda wallet, decline, state: logged_declines.append(decline)
    executed = []

    async def _fake_execute(wallet, action, state=None, transport=None, candidates=None):
        executed.append((action, candidates))
    p._execute = _fake_execute
    return logged_declines, executed


def _fake_result(actions, declines=(), frozen=False):
    return SimpleNamespace(actions=list(actions), declines=list(declines), frozen=frozen)


# --- _run_decision --------------------------------------------------------
def test_run_decision_resolves_candidates_and_dispatches(monkeypatch) -> None:
    p = _plugin()
    logged, executed = _wire_common_spies(p)
    p._resolve_channel_partners = lambda wallet: ["partnerA"]      # candidates exist

    open_act = OpenChannelAction(funding_sat=1_000_000, reason="grow")
    swap_act = ReverseSwapAction(channel_id="aa" * 32, short_id="1x1x1",
                                 lightning_amount_sat=400_000, reason="drain",
                                 provider_npub="npubX")
    monkeypatch.setattr(pkg, "evaluate",
                        lambda snap, cfg: _fake_result([open_act, swap_act]))

    wallet = SimpleNamespace(basename=lambda: "w")
    asyncio.run(p._run_decision(wallet, _snapshot(), SimpleNamespace(), transport=None))

    # The open carries its pre-resolved candidates; the swap carries None.
    assert executed == [(open_act, ["partnerA"]), (swap_act, None)]
    assert logged == []                       # no declines


def test_run_decision_open_without_partner_becomes_decline(monkeypatch) -> None:
    p = _plugin()
    logged, executed = _wire_common_spies(p)
    p._resolve_channel_partners = lambda wallet: []               # no eligible partner
    sentinel_decline = object()
    p._no_partner_decline = lambda wallet, action: sentinel_decline

    open_act = OpenChannelAction(funding_sat=1_000_000, reason="grow")
    monkeypatch.setattr(pkg, "evaluate", lambda snap, cfg: _fake_result([open_act]))

    wallet = SimpleNamespace(basename=lambda: "w")
    asyncio.run(p._run_decision(wallet, _snapshot(), SimpleNamespace(), transport=None))

    # The unexecutable open is surfaced as a first-class decline, not executed.
    assert executed == []
    assert logged == [sentinel_decline]


def test_run_decision_logs_engine_declines(monkeypatch) -> None:
    p = _plugin()
    logged, executed = _wire_common_spies(p)
    p._resolve_channel_partners = lambda wallet: ["partnerA"]

    decline = object()
    monkeypatch.setattr(pkg, "evaluate",
                        lambda snap, cfg: _fake_result([], declines=[decline], frozen=True))

    wallet = SimpleNamespace(basename=lambda: "w")
    asyncio.run(p._run_decision(wallet, _snapshot(), SimpleNamespace(), transport=None))

    assert executed == []
    assert logged == [decline]                # engine decline flows to the log


# --- _await_offers --------------------------------------------------------
def test_await_offers_returns_once_offers_arrive() -> None:
    # Empty on the first poll, populated on the next -> returns without waiting
    # out the whole timeout.
    calls = {"n": 0}

    def _get_recent_offers():
        calls["n"] += 1
        return [] if calls["n"] < 2 else ["offer"]
    transport = SimpleNamespace(get_recent_offers=_get_recent_offers)

    asyncio.run(LiquidityPlugin._await_offers(transport, timeout=5.0))
    assert calls["n"] >= 2


# --- _swap_session, URL mode ---------------------------------------------
def test_swap_session_url_mode_yields_http_transport() -> None:
    fake_transport = SimpleNamespace(name="http")

    class _CM:
        async def __aenter__(self):
            return fake_transport

        async def __aexit__(self, *exc):
            return False

    sm = SimpleNamespace(create_transport=lambda: _CM(), is_initialized=asyncio.Event())
    sm.is_initialized.set()
    wallet = SimpleNamespace(lnworker=SimpleNamespace(swap_manager=sm))

    p = _plugin()
    p.config = SimpleNamespace(SWAPSERVER_URL="http://provider.example")

    async def go():
        async with p._swap_session(wallet) as tr:
            return tr
    assert asyncio.run(go()) is fake_transport
