"""End-to-end tests for the two outbound-liquidity-preservation mechanisms,
driven against the live rig (bitcoind + Fulcrum + nostr + the client daemon with
the plugin + the partner running a real swapserver):

  * ``manage_plugin_opened_only`` -- with the scope switch ON, the plugin must
    leave the wallet's manually-opened channels (the rig's baseline channels,
    which the plugin did NOT open) entirely untouched: no reverse swap fires, it
    records the deliberate "not opened by the plugin" skip, and the channel
    balances do not move; and
  * ``min_outbound_sat`` -- with a per-channel outbound floor set, the plugin may
    drain a channel but must never take its local (outbound) balance below the
    floor.

The two tests SHARE one rig (module-scoped) to avoid two expensive bring-ups.
The scope test runs first (it drains nothing, so the channels stay ~50/50 for
the floor test); the floor test then drains down to the configured floor.

Heavy and slow (~6-10 min for the shared bring-up); needs the electrum venv +
docker. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_outbound_preservation_e2e.py -q -s
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Dict, List

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


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _swap_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "swap"]


def _scope_skip_declines() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "decline" and e.get("kind") == "swap"
            and "not opened by the plugin" in (e.get("reason") or "")]


def _max_inbound() -> int:
    """Largest receivable (remote) channel balance -- our inbound liquidity."""
    return max((c["remote_balance"] for c in _channels()), default=0)


def _min_local() -> int:
    return min((c["local_balance"] for c in _channels()), default=0)


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)     # keep chain advancing so swap claim/settle can confirm
        time.sleep(period)
    return cond()


def _settle(rig, ticks: int = 8, period: float = 2.0) -> None:
    """Advance the chain a while so the plugin gets several evaluation ticks in
    which it *could* act -- used to give a wrong behaviour every chance to show
    up before asserting it did not happen."""
    for _ in range(ticks):
        _mine(rig, 1)
        time.sleep(period)


def _base_arm_config() -> None:
    """Config common to both scenarios: both channels are already to the only
    partner (so no new open), the swap trigger is low (1M local >> trigger), and
    the fee ceiling is high enough to accept the rig's several-percent all-in
    cost. Automation is armed by each test *last*."""
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")          # already at 2 -> no open
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "25")     # permit the rig's all-in cost
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.dev_fee_pct", "0")


@pytest.fixture(scope="module")
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # 2 baseline 50/50 channels; client loads the plugin; provider discovered
    try:
        yield r
    finally:
        r.shutdown()


def test_scope_switch_leaves_manual_channels_untouched(rig):
    """manage_plugin_opened_only=ON: the rig's baseline channels were opened by
    the wallet (not the plugin), so the plugin must not swap them."""
    chans = _channels()
    assert len(chans) == 2, f"expected the 2 baseline channels, got {len(chans)}"
    baseline_min_local = _min_local()          # ~1_000_000 (50/50)
    baseline_inbound = _max_inbound()
    assert baseline_inbound <= 1_100_000, "baseline channels are not ~50/50"

    _base_arm_config()
    _setcfg("plugins.inbound_liquidity.manage_plugin_opened_only", "true")   # the switch
    _setcfg("plugins.inbound_liquidity.min_outbound_sat", "0")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")          # arm last

    # The plugin evaluates, sees both channels over the trigger, and DELIBERATELY
    # skips them because it did not open them -- a positive signal it ran and
    # spared them (not merely that nothing happened yet).
    assert _wait_until(lambda: len(_scope_skip_declines()) >= 1, rig=rig, timeout=240), \
        "plugin never recorded the manage-plugin-opened-only skip"

    # Give it ample further ticks in which it *could* wrongly swap.
    _settle(rig, ticks=8)

    # No reverse swap was ever executed on a manual channel...
    assert len(_swap_actions()) == 0, \
        f"plugin swapped a manual channel despite the scope switch: {_swap_actions()}"
    # ...and the channel balances did not move.
    assert _min_local() >= baseline_min_local - 50_000, \
        f"a manual channel's local balance dropped (min_local={_min_local()})"
    assert _max_inbound() <= baseline_inbound + 50_000, \
        f"inbound liquidity changed; a manual channel was drained (max_inbound={_max_inbound()})"

    # Disarm before the next test rearms with a different policy.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "false")


def test_outbound_floor_is_respected(rig):
    """min_outbound_sat=FLOOR: the plugin drains a channel but never below the
    per-channel outbound floor."""
    FLOOR = 400_000
    baseline_min_local = _min_local()          # channels still ~50/50 from bring-up
    assert baseline_min_local > FLOOR + 300_000, \
        f"precondition: baseline local ({baseline_min_local}) must be well above the floor"

    _base_arm_config()
    _setcfg("plugins.inbound_liquidity.manage_plugin_opened_only", "false")  # manage all channels
    _setcfg("plugins.inbound_liquidity.min_outbound_sat", str(FLOOR))
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")          # arm last

    # 1) A reverse swap fires (the floor did not block everything).
    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=240), \
        "plugin never executed a reverse swap under the outbound floor"

    # 2) Draining actually happened: a channel's local dropped from the baseline.
    assert _wait_until(lambda: _min_local() < baseline_min_local - 100_000,
                       rig=rig, timeout=300), \
        f"no channel was drained (min local_balance={_min_local()})"

    # 3) Let the plugin keep evaluating -- it must NOT drive local below the floor.
    _settle(rig, ticks=10)

    # THE REQUIREMENT: the floor is respected on every channel, at all times.
    assert _min_local() >= FLOOR, \
        f"outbound floor violated: min local_balance {_min_local()} < floor {FLOOR}"
    # And it really did drain toward the floor (feature is not vacuously satisfied).
    assert _min_local() < baseline_min_local - 100_000, \
        f"channel was not meaningfully drained (min local_balance={_min_local()})"
