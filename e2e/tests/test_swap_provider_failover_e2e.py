"""End-to-end test that the plugin FAILS OVER between swap providers within a
single evaluation cycle.

Before this behaviour existed, a swap that failed burned the channel's 180s
cooldown and the next cycle re-picked the very same provider (a failed attempt
often records no fault at all, so the ranking was unchanged) -- a wedge that
could persist indefinitely while a perfectly good provider sat idle.

The rig is launched with ``--second-provider``, which adds a second swapserver
that deliberately UNDERCUTS the first (0.1% vs 0.5%), so the plugin's
cheapest-first ranking always tries it first. The test then kills that
provider's daemon. Its offer stays valid on the nostr relay (Electrum treats
offers as fresh for 3600s), so the plugin still ranks it first, tries it, times
out its createswap RPC -- and must then fall back to the surviving provider and
complete a REAL reverse swap, without waiting out the cooldown.

What is asserted:
  * the dead provider is tried first and collects a reliability fault,
  * the swap nevertheless completes, via the OTHER provider, in the same cycle
    (the action's detail records it as attempt 2),
  * inbound liquidity actually increased -- a real swap, not just a log line.

Heavy and slow (~8-12 min); needs the electrum venv + docker. Function-scoped
rig (wipes .run, kills any previous rig), so it must NOT run while a manual
run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_swap_provider_failover_e2e.py -q -s
"""
from __future__ import annotations

import glob
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
from rig import paths  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    PARTNER2,
    electrum_cli,
    kill_inst_daemon,
    mine,
    stop_daemon,
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


def _reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get("inbound_liquidity_provider_reliability", {})
    return raw if isinstance(raw, dict) else {}


def _max_inbound() -> int:
    return max((c["remote_balance"] for c in _channels()), default=0)


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)
        time.sleep(period)
    return cond()


def _arm_swap_config() -> None:
    """Make the plugin swap rather than open or close.

    ``max_channels`` is 3 here (not the usual 2) because ``--second-provider``
    adds a third channel -- leaving it at 2 would have the engine trying to close
    one instead of swapping. The fee ceiling is deliberately permissive: the
    rig's providers charge a fixed 45k prepayment on ~1M swaps, so the honest
    all-in cost is several percent and the gate would otherwise (correctly)
    decline every swap before the failover under test could run.
    """
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")
    # The dead provider's channel goes offline with it; that is the test's own
    # doing, not a peer fault worth force-closing over.
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")


@pytest.fixture
def rig():
    """A rig with TWO swap providers, the cheaper of which we later kill."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--second-provider"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_dead_provider_is_skipped_and_the_swap_completes_via_the_next_one(rig):
    # The rig pins swapserver_npub to the CHEAPEST provider, which is partner2 --
    # the one we are about to kill. Confirm the topology before relying on it.
    assert rig.partner2_nodeid, "rig did not bring up a second provider"
    chans = _channels()
    assert len(chans) == 3, f"expected 2 partner + 1 partner2 channels, got {len(chans)}"
    baseline_inbound = _max_inbound()
    assert baseline_inbound <= 1_100_000, "baseline channels are not ~50/50"

    # Kill the cheap provider. Its offer stays fresh on the relay for an hour, so
    # the plugin will still rank it FIRST and try it -- exactly the wedge this
    # feature fixes.
    stop_daemon(PARTNER2)
    kill_inst_daemon(PARTNER2)
    time.sleep(2)

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) A reverse swap completes despite the first-ranked provider being dead.
    #    Generous timeout: the dead provider's createswap RPC must time out
    #    (~60s) before the cascade moves on.
    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=420), \
        ("plugin never completed a reverse swap -- the cascade did not fall back "
         "to the surviving provider")

    # 2) It was a FAILOVER, not a first-attempt success: the executor records the
    #    attempt index on any swap that was not the first one tried.
    action = _swap_actions()[0]
    detail = action.get("detail", "")
    assert "attempt 2 of" in detail, \
        f"swap did not come from a failover attempt; detail was: {detail!r}"

    # 3) The dead provider was tried and faulted; the swap went to the other one.
    log_text = _client_log_text()
    assert "failing over to the next provider" in log_text, \
        "no failover was logged -- the cascade did not advance past provider 1"
    faults = _reliability()
    assert faults, "the dead provider collected no reliability fault"
    assert any(int(s.get("fault_count", 0)) > 0 for s in faults.values()), \
        f"no provider fault recorded: {faults}"

    # 4) A real swap: inbound liquidity actually increased.
    assert _wait_until(lambda: _max_inbound() > baseline_inbound + 100_000,
                       rig=rig, timeout=300), \
        f"inbound liquidity did not increase (max remote_balance={_max_inbound()})"

    # 5) The dead provider now carries a fault, so the ranking demotes it and
    #    later cycles stop paying its timeout. (The decay/penalty arithmetic
    #    itself is unit-tested; what matters here is that a real fault landed.)
    dead_faults = [s for s in _reliability().values()
                   if int(s.get("fault_count", 0)) > 0]
    assert dead_faults, "the dead provider was not demoted for the next cycle"
