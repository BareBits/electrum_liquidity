"""End-to-end test that the plugin performs a REAL reverse swap: it drives the
live rig (bitcoind + Fulcrum + nostr + the client daemon with the plugin + the
partner running a real swapserver), arms the swap path, and asserts the plugin

  * executes a reverse swap against the discovered provider (a `swap` action in
    the real decision log),
  * thereby INCREASES inbound liquidity (a channel's receivable/remote balance
    climbs above its 50/50 baseline as local is swapped out to on-chain), and
  * accrues a dev fee on the completed swap.

This is the one path the mocked unit tests can never prove: a swap actually
executed end-to-end against a live provider and the receive capacity moved. The
other e2e suites deliberately DISABLE swaps; this one enables them.

The rig's provider quotes 0.5% + a fixed 45k-sat prepayment on ~0.02 BTC
channels, so the effective all-in cost is several percent -- we raise
`max_swap_fee_pct` well above it so the plugin does not (correctly) decline.

Heavy and slow (~6-10 min); needs the electrum venv + docker. Function-scoped
rig (wipes .run, kills any previous rig), so it must NOT run while a manual
run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_reverse_swap_e2e.py -q -s
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


def _dev_fee_owed() -> int:
    return int(_wallet_db().get("inbound_liquidity_dev_fee_owed_sat", 0) or 0)


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


def _arm_swap_config() -> None:
    """Config that makes the plugin swap (not open): both channels are already to
    the only partner (so no new open), and the swap trigger is low while the fee
    ceiling is high enough to accept the rig's several-percent all-in cost."""
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")          # already at 2 -> no open
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")  # 1M local >> trigger
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")      # permit ~5-6% all-in
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.dev_fee_pct", "0.5")          # accrue a dev fee


@pytest.fixture
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


def test_reverse_swap_increases_inbound_and_accrues_dev_fee(rig):
    chans = _channels()
    assert len(chans) == 2, f"expected the 2 baseline channels, got {len(chans)}"
    baseline_inbound = _max_inbound()          # ~1_000_000 (50/50)
    assert baseline_inbound <= 1_100_000, "baseline channels are not ~50/50"

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) A reverse swap is executed against the discovered provider.
    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=240), \
        "plugin never executed a reverse swap (no 'swap' action in the decision log)"

    # 2) Inbound liquidity increased: as one channel's local is swapped out to
    #    on-chain, its receivable (remote) balance climbs well above the baseline.
    assert _wait_until(lambda: _max_inbound() > baseline_inbound + 100_000,
                       rig=rig, timeout=300), \
        f"inbound liquidity did not increase (max remote_balance={_max_inbound()})"
    assert _min_local() < 900_000, \
        f"no channel was drained by the swap (min local_balance={_min_local()})"

    # 3) A dev fee accrued on the completed swap.
    assert _wait_until(lambda: _dev_fee_owed() > 0, rig=rig, timeout=120), \
        "no dev fee accrued after the reverse swap completed"
