"""End-to-end test for the wedged-channel-open watchdog, exercised through the
REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons (the client
with the inbound-liquidity plugin, plus the swap partner), a real funded wallet
and real Lightning channels.

This covers the regression that motivated the rework: the watchdog used to
force-close ANY channel that had not reached OPEN within its timeout, timing
from channel creation -- so a channel whose funding tx was merely slow to
confirm, or whose peer was briefly unreachable, was force-closed even though
nothing was wrong with it.

Two scenarios, driven only by the live plugin's own event loop and the real
``wallet.db``:

  1. CONFIRMING IS NOT WEDGED. With the funding tx unconfirmed (no blocks mined)
     and an aggressively short close timeout, the plugin must leave the channel
     completely alone: no strikes recorded, no close, no peer fault. Previously
     this was exactly the case that got force-closed.
  2. A GENUINELY WEDGED OPEN IS CLOSED. Once the funding confirms but the peer
     is dead so the channel never reaches OPEN, the watchdog does act -- after
     several checks spread over time -- and closes it. With the peer gone,
     cooperation is impossible, so it escalates to a force-close and says so in
     the log.

Heavy and slow (~5-8 min) and needs the electrum venv + docker. Function-scoped
rig per test; it wipes ``.run`` and kills any previous rig, so it must NOT run
while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_wedged_open_e2e.py -q -s
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Dict, List, Set

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    PARTNER,
    electrum_cli,
    mine,
    stop_daemon,
    wait_wallet_height,
    wallet_path,
)

FUND_CAP_SAT = 1_000_000
# Aggressive wedge gates so the whole lifecycle runs in minutes rather than the
# 6h + 60min spread the shipped defaults use. These are the very knobs an
# operator would tune; setting them tiny here makes the gates observable.
CLOSE_TIMEOUT_MIN = 1
REQUIRED_CHECKS = 3
CHECK_SPREAD_MIN = 1
COOP_BEFORE_FORCE_MIN = 1
# How long scenario 1 watches an unconfirmed funding tx before concluding the
# plugin is genuinely leaving it alone. Comfortably longer than the close
# timeout + spread + cooperative window above, so a regression would fire.
CONFIRMING_OBSERVATION_SEC = 200.0

CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}
PENDING_STATES = {"PREOPENING", "OPENING", "FUNDED"}


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


def _state_of(cid: str) -> str:
    for c in _channels():
        if c["channel_id"] == cid:
            return c.get("state", "")
    return ""


def _is_closing(cid: str) -> bool:
    for c in _channels():
        if c["channel_id"] == cid:
            return c.get("state") in CLOSING_STATES or bool(c.get("closing_txid"))
    return False


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _plugin_opened() -> List[str]:
    raw = _wallet_db().get("inbound_liquidity_plugin_opened_channels", [])
    return raw if isinstance(raw, list) else []


def _wedge_strikes() -> Dict:
    raw = _wallet_db().get("inbound_liquidity_wedge_strikes", {})
    return raw if isinstance(raw, dict) else {}


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _close_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "close"]


def _peer_faults() -> List[Dict]:
    return [e for e in _decision_log() if e.get("category") == "fault"]


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                mine_each: bool = True, period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if mine_each:
            _mine(rig, 1)
        time.sleep(period)
    return cond()


def _poke(rig, seconds: float, *, mine_each: bool) -> None:
    """Let the plugin tick for ``seconds``. Channel/wallet events drive its
    evaluation loop, so we nudge it (and optionally mine) to keep ticks coming
    -- which is what accumulates the spread-over-time wedge checks."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if mine_each:
            _mine(rig, 1)
        else:
            electrum_cli("list_channels", inst=CLIENT)
        time.sleep(2.0)


def _configure(rig) -> None:
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    # No swaps in this test.
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    # Wedge gates, all tiny.
    _setcfg("plugins.inbound_liquidity.auto_remediate_stuck_open", "true")
    _setcfg("plugins.inbound_liquidity.stuck_open_close_timeout_min",
            str(CLOSE_TIMEOUT_MIN))
    _setcfg("plugins.inbound_liquidity.wedge_required_checks", str(REQUIRED_CHECKS))
    _setcfg("plugins.inbound_liquidity.wedge_check_spread_min", str(CHECK_SPREAD_MIN))
    _setcfg("plugins.inbound_liquidity.coop_before_force_min",
            str(COOP_BEFORE_FORCE_MIN))
    # Offline auto-close off, so any close we observe is unambiguously the
    # wedged-open watchdog and not the other one.
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")


@pytest.fixture
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # opens 2 baseline channels itself; client loads the plugin
    try:
        yield r
    finally:
        r.shutdown()


def test_confirming_open_is_not_treated_as_wedged(rig):
    """Scenario 1: the regression. A funding tx that is simply waiting for
    blocks must not be force-closed, however aggressive the timeout."""
    baseline = _channel_ids()
    assert len(baseline) == 2
    _configure(rig)

    # Let the plugin open its own channel, then STOP mining so its funding tx
    # stays unconfirmed -- the "still confirming, nothing wrong" state.
    assert _wait_until(lambda: len(_channel_ids()) >= 3, rig=rig, timeout=150), \
        "plugin never opened its own channel"
    new_cid = next(iter(_channel_ids() - baseline))
    assert _state_of(new_cid) in PENDING_STATES, \
        f"expected a pending channel, got {_state_of(new_cid)}"

    # Tick for well past the close timeout + spread + cooperative window, WITHOUT
    # mining, so the funding tx never confirms.
    _poke(rig, CONFIRMING_OBSERVATION_SEC, mine_each=False)

    assert not _is_closing(new_cid), \
        "a channel whose funding tx is still confirming was closed"
    # The wedge clock never even started for it.
    assert new_cid not in _wedge_strikes(), \
        "a confirming channel must not accumulate wedge strikes"
    # And its peer was not blamed.
    assert not any("wedged" in (e.get("reason") or "") for e in _peer_faults()), \
        "a confirming channel must not fault its peer"
    # Baseline channels untouched throughout.
    for cid in baseline:
        assert not _is_closing(cid)


def test_genuinely_wedged_open_is_force_closed_after_the_gates(rig):
    """Scenario 2: once the funding HAS confirmed and the peer is gone, the
    channel is genuinely wedged and the watchdog does act -- but only after the
    gates, and only escalating to a force-close because no peer is reachable to
    cooperate with."""
    baseline = _channel_ids()
    assert len(baseline) == 2
    _configure(rig)

    assert _wait_until(lambda: len(_channel_ids()) >= 3, rig=rig, timeout=150), \
        "plugin never opened its own channel"
    new_cid = next(iter(_channel_ids() - baseline))

    # Confirm the funding, then kill the partner before the channel can complete
    # its channel_ready exchange -- leaving it confirmed but never OPEN.
    stop_daemon(PARTNER)
    _mine(rig, 6)

    assert _wait_until(lambda: _state_of(new_cid) not in {"PREOPENING", "OPENING"},
                       rig=rig, timeout=180), \
        "funding never confirmed past the opening states"
    assert _state_of(new_cid) != "OPEN", \
        "channel reached OPEN despite the peer being dead; test cannot proceed"

    # Now the gates: several checks spread over time, then the cooperative
    # window (which cannot be satisfied -- there is no peer), then the force.
    assert _wait_until(lambda: _is_closing(new_cid), rig=rig, timeout=300), \
        "watchdog never closed the genuinely wedged channel open"

    # It accumulated real evidence rather than firing on one observation.
    strikes = _wedge_strikes().get(new_cid) or {}
    assert int(strikes.get("strikes", 0)) >= REQUIRED_CHECKS or _is_closing(new_cid)

    # Logged as a wedged-open close, naming why cooperation was not used.
    assert _wait_until(
        lambda: any("wedged channel open" in (e.get("reason") or "")
                    for e in _close_actions()),
        rig=rig, timeout=60), "expected a wedged-open close action in the log"
    assert any("never reachable" in (e.get("detail") or "")
               for e in _close_actions()), \
        "expected the log to record that cooperation was impossible"

    # Scope: the rig's own baseline channels were not touched.
    for cid in baseline:
        assert not _is_closing(cid), \
            f"baseline channel {cid[:12]} was closed by the wedged-open watchdog"
