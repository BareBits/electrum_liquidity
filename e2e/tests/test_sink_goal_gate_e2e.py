"""End-to-end test for "Don't use liquidity sink until inbound liquidity goal is
met" (``defer_sink_until_goal``), driven against the live rig (bitcoind +
Fulcrum + nostr + the client daemon with the plugin + the partner running a real
swapserver).

The rule: a sink payment leaves this wallet for good -- it lands in whatever
account the address belongs to, not in the on-chain balance the plugin opens
channels with. So while the wallet is still building its channels, a channel over
its trigger is REVERSE-SWAPPED instead of sunk (the same outbound leaves, the
same inbound comes back, but as on-chain coins that can fund the next channel).
Once the build-out is done -- ``max_channels`` held, no plugin-opened channel
below ``liquidity_goal_sat`` -- the sink takes over.

What this proves against a real Electrum, which no unit test can:

  1. with the goal UNMET and a perfectly good sink configured, the plugin
     executes a real reverse swap and pays the sink nothing -- and the swap it
     logs says the sink was held back, so the choice is explained rather than
     silent;
  2. once the goal is MET, the very same configuration pays the sink instead,
     over real Lightning, and inbound liquidity actually rises;
  3. unticking the switch with the goal still unmet pays the sink -- i.e. what
     held the sink back in (1) was this switch and nothing else about the rig.

Staging note: the goal condition is steered through ``max_channels`` rather than
by growing real channels. The rig's two baseline channels are opened by the
WALLET, so they are never "plugin-opened below the goal" and test 2 of
``liquidity_goal_met`` is satisfied from the start; setting ``max_channels`` to 3
leaves the ceiling test unmet (room for another channel) and setting it to 2 --
what the wallet already holds -- meets it. Opens are put out of reach for the
whole test (``min_onchain_to_open_sat`` far above the rig's funding, plus
``one_channel_per_peer``), so the ceiling is a pure gate control and no open ever
competes with the drain under test.

The sink points at the rig's LNURL-pay stub, whose invoices are minted by the
partner the client has a direct channel to -- so a sink payment settles on the
first rung. The halving ladder has its own e2e (``test_liquidity_sink_e2e``);
what is under test here is WHICH drain the plugin chooses.

Heavy and slow (~10-20 min; two function-scoped rigs); needs the electrum venv +
docker. Function-scoped rig (wipes .run, kills any previous rig), so it must NOT
run while a manual run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_sink_goal_gate_e2e.py -q -s
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

# The liquidity goal used throughout. Below the rig's 2_000_000 sat baseline
# channels, but they are wallet-opened and so out of scope for the goal test
# either way -- what makes the goal met or unmet here is the channel ceiling.
GOAL_SAT = 1_500_000
# Trigger the drain well below a baseline channel's 1_000_000 sat local balance.
TRIGGER_SAT = 100_000
# Far above the rig's ~12 BTC per wallet, so no channel open is ever affordable
# and `max_channels` is free to be used purely as the goal-gate control.
UNAFFORDABLE_OPEN_SAT = 5_000_000_000


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


def _actions(kind: str) -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == kind]


def _declines() -> List[str]:
    return [str(e.get("reason") or "") for e in _decision_log()
            if e.get("category") == "decline"]


def _max_inbound() -> int:
    """Largest receivable (remote) channel balance -- our inbound liquidity."""
    return max((c["remote_balance"] for c in _channels()), default=0)


def _max_local() -> int:
    return max((c["local_balance"] for c in _channels()), default=0)


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)     # keep the chain advancing so a swap can confirm
        time.sleep(period)
    return cond()


def _settle(rig, ticks: int = 8, period: float = 2.0) -> None:
    """Advance the chain a while so the plugin gets several evaluation ticks in
    which it *could* act -- used to give a wrong behaviour every chance to show
    up before asserting it did not happen."""
    for _ in range(ticks):
        _mine(rig, 1)
        time.sleep(period)


def _arm_config(rig, *, max_channels: int, defer: bool) -> None:
    """Point the plugin at the rig's sink, make it want to drain, and set the two
    knobs under test.

    ``max_channels`` is the goal-gate control (see the staging note): 3 leaves
    room for another channel, so the build-out is unfinished; 2 is what the
    wallet already holds, so it is done. The swap fee ceiling is permissive
    because the rig's provider quotes several percent all-in on channels this
    size -- a (correct) cost decline would look exactly like the sink being
    preferred, which is the thing under test.
    """
    assert rig.lnurl_stub is not None
    _setcfg("plugins.inbound_liquidity.sink_address",
            rig.lnurl_stub.lightning_address)
    _setcfg("plugins.inbound_liquidity.defer_sink_until_goal",
            "true" if defer else "false")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", str(GOAL_SAT))
    _setcfg("plugins.inbound_liquidity.max_channels", str(max_channels))
    _setcfg("plugins.inbound_liquidity.disable_submarine_swaps", "false")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.min_onchain_to_open_sat",
            str(UNAFFORDABLE_OPEN_SAT))
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", str(TRIGGER_SAT))
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    # The dev fee is paid to this same LNURL stub; at 0 it cannot be mistaken for
    # a sink payment (or race one) in the assertions below.
    _setcfg("plugins.inbound_liquidity.dev_fee_pct", "0")


@pytest.fixture
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # 2 baseline 50/50 channels, opened by the WALLET (not the plugin)
    try:
        yield r
    finally:
        r.shutdown()


def test_sink_waits_for_the_goal_then_takes_over(rig):
    assert len(_channels()) == 2, \
        f"expected the 2 baseline channels, got {len(_channels())}"
    baseline_inbound = _max_inbound()
    assert baseline_inbound <= 1_100_000, "baseline channels are not ~50/50"

    # --- phase A: goal UNMET -> swap, and the sink is left alone ----------
    _arm_config(rig, max_channels=3, defer=True)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) THE FEATURE: the drain still happens, but on-chain -- where the proceeds
    #    can fund the channel the wallet is still missing.
    assert _wait_until(lambda: len(_actions("swap")) >= 1, rig=rig, timeout=300), (
        "plugin never reverse-swapped with the sink held back; declines were: "
        + json.dumps(_declines()[-5:]))
    # Stop here: one swap is all this phase needs, and the engine is frozen on it
    # being in flight anyway. Disarming now leaves a channel over its trigger for
    # phase B to drain.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "false")

    # 2) Nothing was paid to the sink, even though it is configured and reachable.
    assert not _actions("sink"), (
        "the sink was paid while the liquidity goal was unmet: "
        + json.dumps(_actions("sink")[:2]))

    # 3) The choice is explained, not silent: the swap says why the sink was not
    #    used, so a user with a configured sink is never left guessing.
    reason = str(_actions("swap")[0].get("reason") or "")
    assert "liquidity sink is held back" in reason, \
        f"the swap did not record why the sink was skipped: {reason!r}"
    assert f"{GOAL_SAT} sat liquidity goal" in reason, \
        f"the swap's reason did not name the goal it is waiting for: {reason!r}"

    # --- phase B: goal MET -> the sink takes over -------------------------
    # Same sink, same triggers; the only change is that the wallet now holds its
    # full complement of channels, so on-chain balance can no longer buy a better
    # one and there is nothing left to build.
    swaps_before = len(_actions("swap"))
    assert _max_local() > TRIGGER_SAT, (
        f"precondition: no channel is still over the {TRIGGER_SAT} sat trigger "
        f"for phase B to drain (max local {_max_local()})")
    _arm_config(rig, max_channels=2, defer=True)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: len(_actions("sink")) >= 1, rig=rig, timeout=420), (
        "plugin never paid the sink after the goal was met; declines were: "
        + json.dumps(_declines()[-5:]))
    sink = _actions("sink")[0]

    # 4) A real payment to the configured address -- the log's `dest` is
    #    abbreviated (the plugin elides identities), so the whole address is
    #    checked in the detail line where it is recorded in full.
    assert rig.lnurl_stub.lightning_address in str(sink.get("detail") or ""), \
        f"sink action did not record the sink address: {sink!r}"
    paid = int(sink.get("amount_sat") or 0)
    assert paid >= 10_000, f"sink paid {paid} sat, below the plugin's floor"

    # 5) ...and it did the liquidity job: inbound rose by roughly what was paid.
    assert _wait_until(lambda: _max_inbound() > baseline_inbound + paid // 2,
                       rig=rig, timeout=300), (
        f"inbound liquidity did not increase after a {paid} sat sink payment "
        f"(max remote_balance={_max_inbound()}, baseline={baseline_inbound})")

    # 6) The sink was PREFERRED, not merely permitted: with the goal met the
    #    plugin drains through it rather than swapping again.
    assert len(_actions("swap")) == swaps_before, (
        "a further reverse swap ran after the goal was met, instead of the sink: "
        + json.dumps(_actions("swap")[swaps_before:]))


def test_unticking_the_switch_uses_the_sink_before_the_goal(rig):
    """The isolating control for phase A above: identical rig, identical unmet
    goal, switch OFF -- and the sink is paid. Whatever held it back was this
    setting, not the state of the rig."""
    assert len(_channels()) == 2
    baseline_inbound = _max_inbound()

    _arm_config(rig, max_channels=3, defer=False)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: len(_actions("sink")) >= 1, rig=rig, timeout=420), (
        "plugin never paid the sink with the gate unticked; declines were: "
        + json.dumps(_declines()[-5:]))
    assert not _actions("swap"), (
        "a reverse swap ran even though the sink was free to drain the channel: "
        + json.dumps(_actions("swap")[:2]))
    assert _wait_until(lambda: _max_inbound() > baseline_inbound,
                       rig=rig, timeout=300), \
        "the sink payment did not increase inbound liquidity"
    # And the goal really was unmet the whole time -- otherwise this proves
    # nothing about the switch.
    _settle(rig, ticks=2)
    assert len(_channels()) < 3, \
        "the wallet reached its channel ceiling; the goal was not unmet after all"
