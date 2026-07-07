"""End-to-end test for the ENABLED/DISABLED master slider, exercised through the
REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons (the client
with the inbound-liquidity plugin, plus the swap partner), a real funded wallet
and real Lightning channels.

The slider on the Settings tab just writes the ``automation_enabled`` ConfigVar
(applied immediately), so here we drive that same key over the CLI and assert the
plugin's *behaviour* through the live daemon and its real ``wallet.db``:

  * DISABLED (the shipped default): with the open-enabling config already in
    place so the *only* thing holding the plugin back is the master switch, the
    plugin opens NO channel of its own over a sustained mining window -- proving
    the slider-off state moves no funds and alters no channels.
  * ENABLED (flip the slider on = set automation_enabled true): with the exact
    same config, the plugin now opens its own channel -- proving the switch is
    what arms it.

Heavy and slow (~4-6 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_toggle_e2e.py -q -s
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
    electrum_cli,
    mine,
    wait_wallet_height,
    wallet_path,
)

# Cap each plugin open small so the plugin opens exactly one modest channel.
FUND_CAP_SAT = 1_000_000
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


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _plugin_opened() -> List[str]:
    raw = _wallet_db().get("inbound_liquidity_plugin_opened_channels", [])
    return raw if isinstance(raw, list) else []


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _open_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "open"]


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


def _stays_false(cond: Callable[[], bool], *, rig, duration: float,
                 period: float = 1.5) -> bool:
    """Drive the rig for `duration` seconds (mining each period so wallet events
    keep firing) and return True iff `cond` never became true -- i.e. the plugin
    took no action the whole time."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if cond():
            return False
        _mine(rig, 1)
        time.sleep(period)
    return not cond()


# Config that (when automation is ON) makes the plugin open exactly one modest
# channel of its own to the partner and do no swaps. Applied in BOTH phases so
# the master switch is the only difference between them.
def _arm_open_config() -> None:
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
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


def test_slider_gates_all_automation(rig):
    baseline = _channel_ids()
    assert len(baseline) == 2

    # The rig starts the plugin DISABLED (automation_enabled=false). Put the
    # open-enabling config in place so the master switch is the only brake.
    assert electrum_cli("getconfig", "plugins.inbound_liquidity.automation_enabled",
                        inst=CLIENT).strip().lower() not in ("true", "1")
    _arm_open_config()

    # --- Phase A: DISABLED -> the plugin must open nothing ------------------
    # 75s is comfortably longer than the enabled plugin needs to open below, so
    # "no open in 75s" is a real proof the switch (not timing) blocks it.
    assert _stays_false(lambda: _live_channels() > 2, rig=rig, duration=75.0), \
        "plugin opened a channel while automation was DISABLED"
    assert _plugin_opened() == [], "plugin tagged an opened channel while DISABLED"
    assert _open_actions() == [], "plugin logged an open action while DISABLED"
    assert _channel_ids() == baseline

    # --- Phase B: flip the slider ON (set automation_enabled) --------------
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: _live_channels() >= 3, rig=rig, timeout=150), \
        "plugin never opened its own channel after being ENABLED"
    assert _wait_until(lambda: len(_plugin_opened()) >= 1, rig=rig, timeout=60), \
        "enabled plugin's channel was not tagged in wallet.db"
    plugin_cid = _plugin_opened()[0]
    assert plugin_cid not in baseline, "tagged channel should be the new plugin one"
    assert any(e.get("kind") == "open" for e in _open_actions()), \
        "expected an 'open' action in the decision log after enabling"
