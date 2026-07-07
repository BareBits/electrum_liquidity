"""End-to-end test for the offline-channel auto-close watchdog, exercised through
the REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons (the
client with the inbound-liquidity plugin, plus the swap partner), a real funded
wallet and real Lightning channels.

Scenario (driven only by the live plugin's own event loop and the real
``wallet.db``):

  * The plugin opens ONE channel of its own to the partner (so it is tagged as
    plugin-opened -- the only channels the watchdog will ever auto-close). The
    rig's two baseline channels were opened by the rig, NOT the plugin, so they
    must be left untouched throughout (scope check).
  * The partner daemon is then killed, so that plugin channel's peer goes offline
    for good. With a tiny uptime window and a tiny force-close deadline, the
    plugin commits to closing the channel (peer uptime below the floor) and,
    since the peer never comes back to close cooperatively, force-closes it after
    the deadline -- writing a "force-closed offline" close action to the log.
  * The two baseline channels (same, now-dead peer, but NOT plugin-opened) are
    asserted to stay open -- proving the watchdog only touches channels it opened.

Heavy and slow (~5-8 min) and needs the electrum venv + docker. Function-scoped
rig per test; it wipes ``.run`` and kills any previous rig, so it must NOT run
while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_offline_autoclose_e2e.py -q -s
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

# Cap each plugin open small so the plugin opens exactly one modest channel.
FUND_CAP_SAT = 1_000_000
# Tiny auto-close thresholds (in DAYS) so the whole lifecycle runs in minutes:
#   window ~130s, force-close ~60s after commit.
UPTIME_WINDOW_DAYS = 130.0 / 86400.0
FORCE_CLOSE_DAYS = 60.0 / 86400.0
CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


def _live_channels() -> int:
    return sum(1 for c in _channels() if c.get("state") not in CLOSING_STATES)


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


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _close_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "close"]


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


def test_offline_plugin_channel_is_force_closed(rig):
    baseline = _channel_ids()
    assert len(baseline) == 2

    # Configure so the plugin opens exactly one channel of its own to the partner,
    # does no swaps, and auto-closes offline channels on tiny timers. The
    # one-channel-per-peer guard is turned off so the plugin may open a channel to
    # the partner it already has baseline channels with.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "true")
    _setcfg("plugins.inbound_liquidity.offline_uptime_window_days", f"{UPTIME_WINDOW_DAYS:.8f}")
    _setcfg("plugins.inbound_liquidity.offline_min_uptime_pct", "10")
    _setcfg("plugins.inbound_liquidity.offline_force_close_days", f"{FORCE_CLOSE_DAYS:.8f}")

    # The plugin opens its own channel...
    assert _wait_until(lambda: _live_channels() >= 3, rig=rig, timeout=150), \
        "plugin never opened its own channel"
    # ...identify it (the one channel the plugin tagged as its own) and let it
    # reach OPEN so it counts as a healthy channel before the peer dies.
    assert _wait_until(lambda: len(_plugin_opened()) >= 1, rig=rig, timeout=60), \
        "plugin-opened channel was not tagged in wallet.db"
    plugin_cid = _plugin_opened()[0]
    assert plugin_cid not in baseline, "tagged channel should be the new plugin one"
    assert _wait_until(lambda: _state_of(plugin_cid) == "OPEN", rig=rig, timeout=150), \
        "plugin channel never reached OPEN"

    # Kill the partner: the plugin channel's peer is now offline for good.
    stop_daemon(PARTNER)

    # The watchdog commits (uptime below the floor) then force-closes the plugin
    # channel after the deadline. Force-close is offline-safe, so it proceeds
    # without the peer.
    assert _wait_until(lambda: _is_closing(plugin_cid), rig=rig, timeout=240), \
        "watchdog never force-closed the offline plugin channel"

    # It was logged as a force-close of an offline channel.
    assert _wait_until(
        lambda: any("force-closed offline" in (e.get("reason") or "")
                    for e in _close_actions()),
        rig=rig, timeout=60), "expected a 'force-closed offline' close action in the log"

    # Scope: the two baseline channels (same dead peer, but NOT plugin-opened)
    # must NOT have been auto-closed.
    for cid in baseline:
        assert not _is_closing(cid), \
            f"baseline channel {cid[:12]} was auto-closed but is not plugin-opened"
