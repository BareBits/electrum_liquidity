"""End-to-end test for the undersized-channel replacement rule (the "liquidity
goal"), driven against the live rig (bitcoind + Fulcrum + nostr + the client
daemon with the plugin + the partner running a real swapserver).

The rule: once the plugin is AT its channel ceiling and holds enough on-chain to
fund a channel that would reach the goal, a plugin-opened channel whose capacity
is below that goal is closed -- cooperatively -- so its slot can be reused by a
bigger one. Without it, a wallet is permanently capped at the size of the small
channels it happened to open first.

What this proves against a real Electrum, which no unit test can:

  1. the plugin actually closes the undersized channel it opened, and does so
     COOPERATIVELY (no force-close fee, no CSV timelock);
  2. the rig's two baseline channels -- opened by the WALLET, not the plugin --
     are left completely alone. This is the safety invariant of the whole
     feature: the rule is irreversible and must never touch a channel the user
     set up themselves;
  3. the freed slot is then refilled with a channel that DOES meet the goal, and
     that bigger channel is not itself closed again -- i.e. the rule converges
     instead of churning.

Staging note: the plugin's own channels are opened with no push, so a fresh
channel has local == capacity and is over the percentage swap trigger -- which
deliberately PROTECTS it from this rule (see ``_decide_undersized_closes``). The
production path to a replaceable channel is therefore "open, then drain it into
inbound via reverse swaps". Draining takes several swaps and many minutes of rig
time, so this test instead puts both swap triggers out of reach, which leaves the
fresh channel un-protected for the same reason a drained one is: it is not over a
trigger. The rule under test is the close, not the drain -- the drain already has
its own e2e (``test_reverse_swap_e2e``).

Heavy and slow (~6-10 min for the bring-up); needs the electrum venv + docker.
Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_liquidity_goal_e2e.py -q -s
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

# The plugin's first (undersized) channel, and the goal it falls short of. The
# rig's baseline channels are 2_000_000 sat, so the goal sits between the two:
# the plugin's channel is undersized, the baseline ones are not (and are out of
# scope anyway, being wallet-opened).
SMALL_FUND_SAT = 1_000_000
GOAL_SAT = 1_500_000
# What the replacement is allowed to be funded with. Above the goal, so the
# channel that refills the freed slot actually satisfies it.
BIG_FUND_SAT = 1_800_000

CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}
FORCE_CLOSE_STATES = {"FORCE_CLOSING", "REQUESTED_FCLOSE"}


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _live_channels() -> List[Dict]:
    return [c for c in _channels() if c.get("state") not in CLOSING_STATES]


def _channel(cid: str) -> Dict:
    return next((c for c in _channels() if c["channel_id"] == cid), {})


def _capacity(c: Dict) -> int:
    """A channel's capacity in sat.

    Derived rather than read: Electrum's ``list_channels`` reports the two
    balances, not the capacity, and (absent in-flight HTLCs) they sum to it --
    the rig's baseline channels report 1_000_000 + 1_000_000 for their 0.02 BTC.
    This is the same quantity the plugin's engine compares against the goal."""
    return int(c.get("local_balance") or 0) + int(c.get("remote_balance") or 0)


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


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


def _goal_close_actions() -> List[Dict]:
    return [e for e in _close_actions()
            if "liquidity goal" in (str(e.get("reason") or "")
                                    + str(e.get("detail") or ""))]


def _coop_close_actions() -> List[Dict]:
    return [e for e in _close_actions()
            if "cooperatively closed" in str(e.get("reason") or "")]


def _open_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "open"]


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


def _settle(rig, ticks: int = 8, period: float = 2.0) -> None:
    """Advance the chain a while so the plugin gets several evaluation ticks in
    which it *could* act -- used to give a wrong behaviour every chance to show
    up before asserting it did not happen."""
    for _ in range(ticks):
        _mine(rig, 1)
        time.sleep(period)


def _base_config() -> None:
    """Config shared by both phases.

    ``one_channel_per_peer`` must be off: the rig's baseline channels already go
    to the partner, which is the only node available here, and the guard would
    (correctly) veto any further channel to it.

    Both swap triggers are put out of reach so no reverse swap ever fires -- see
    the staging note in the module docstring. ``swap_trigger_pct`` has to exceed
    100 for that: at exactly 100 a fresh channel (local == capacity) is still
    over the trigger.
    """
    # NB: manage_plugin_opened_only is deliberately left at the rig's OFF, which
    # makes the baseline channels in-scope for reverse swaps. The close rule
    # ignores that switch entirely -- it is plugin-opened-only unconditionally --
    # so leaving it off is what proves the scope is a property of the rule and
    # not something inherited from the swap setting.
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "1000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.min_onchain_to_open_sat", "60000")
    _setcfg("plugins.inbound_liquidity.onchain_reserve_sat", "10000")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.auto_remediate_stuck_open", "false")
    _setcfg("plugins.inbound_liquidity.dev_fee_pct", "0")
    _setcfg("plugins.inbound_liquidity.max_closes_per_day", "5")
    _setcfg("plugins.inbound_liquidity.diag_log_enabled", "true")


@pytest.fixture(scope="module")
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # 2 baseline 2M-sat channels, opened by the WALLET (not the plugin)
    try:
        yield r
    finally:
        r.shutdown()


def test_undersized_channel_is_replaced_with_a_bigger_one(rig):
    baseline_ids = _channel_ids()
    assert len(baseline_ids) == 2, \
        f"expected the 2 baseline channels, got {len(baseline_ids)}"

    # --- phase 1: let the plugin open ONE small channel -------------------
    # The goal is off here, so nothing can be closed while we build the state.
    _base_config()
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    _setcfg("lightning_max_funding_sat", str(SMALL_FUND_SAT))
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: len(_live_channels()) >= 3, rig=rig, timeout=300), (
        f"plugin never opened its channel (live={len(_live_channels())}, "
        f"opens={_open_actions()})")
    assert _wait_until(lambda: len(_plugin_opened()) >= 1, rig=rig, timeout=90), \
        "plugin opened a channel but never tagged it as its own"
    small_cid = _plugin_opened()[0]
    assert small_cid not in baseline_ids, "tagged a baseline channel as plugin-opened"
    # It must reach OPEN before the rule can consider it: gate 8 requires a
    # reachable peer, and while the funding is still confirming the engine is in
    # its opens-only freeze anyway.
    assert _wait_until(lambda: _channel(small_cid).get("state") == "OPEN",
                       rig=rig, timeout=300), (
        f"the plugin's channel never reached OPEN "
        f"(state={_channel(small_cid).get('state')!r})")
    small_capacity = _capacity(_channel(small_cid))
    assert 0 < small_capacity < GOAL_SAT, (
        f"precondition: the plugin's channel ({small_capacity}) must be below the "
        f"goal ({GOAL_SAT}) for the rule to see it as undersized")

    # Nothing has been closed yet -- the rule is off.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "false")
    assert _goal_close_actions() == []

    # --- phase 2: arm the goal, at the ceiling, with funds -----------------
    # 3 channels == max_channels (blocked), on-chain far exceeds the goal after
    # the reserve (fundable), and the plugin's channel is under the goal.
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", str(GOAL_SAT))
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "5")
    _setcfg("lightning_max_funding_sat", str(BIG_FUND_SAT))
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    # 1) THE FEATURE: the undersized channel is closed, and the decision log says
    #    why -- an irreversible act has to be explained, not merely performed.
    assert _wait_until(lambda: len(_goal_close_actions()) >= 1, rig=rig, timeout=300), (
        f"plugin never closed the undersized channel "
        f"(closes={_close_actions()}, live={len(_live_channels())})")
    assert _wait_until(
        lambda: _channel(small_cid).get("state") in CLOSING_STATES,
        rig=rig, timeout=300), (
        f"the close was decided but the channel never entered a closing state "
        f"(state={_channel(small_cid).get('state')!r})")

    # 2) It was COOPERATIVE: the peer is reachable, so the plugin must never have
    #    paid for a force-close (a mining fee plus a CSV timelock) to resize.
    assert _channel(small_cid).get("state") not in FORCE_CLOSE_STATES, \
        f"undersized channel was force-closed: {_channel(small_cid).get('state')!r}"
    assert _wait_until(lambda: len(_coop_close_actions()) >= 1, rig=rig, timeout=120), \
        "no cooperative close was recorded for the replacement"

    # 3) THE SAFETY INVARIANT: the wallet's own channels are untouched. The rule
    #    is scoped to plugin-opened channels unconditionally, and this is the
    #    property that makes shipping it on by default defensible.
    for cid in baseline_ids:
        assert _channel(cid).get("state") not in CLOSING_STATES, (
            f"a baseline (wallet-opened) channel was closed by the goal rule: "
            f"{cid} state={_channel(cid).get('state')!r}")

    # 4) The freed slot is refilled with a channel that actually MEETS the goal.
    def _has_goal_sized_channel() -> bool:
        return any(c["channel_id"] not in baseline_ids
                   and c["channel_id"] != small_cid
                   and _capacity(c) >= GOAL_SAT
                   and c.get("state") not in CLOSING_STATES
                   for c in _channels())

    assert _wait_until(_has_goal_sized_channel, rig=rig, timeout=420), (
        f"the freed slot was never refilled with a channel at the goal "
        f"(channels={[(c['channel_id'][:8], _capacity(c), c.get('state')) for c in _channels()]})")

    # 5) CONVERGENCE: the replacement is not itself replaced. Give the plugin
    #    plenty of further ticks in which it could churn, then confirm it closed
    #    exactly the one channel and left the goal-sized one alone.
    closes_after_replacement = len(_goal_close_actions())
    _settle(rig, ticks=10)
    assert len(_goal_close_actions()) == closes_after_replacement, (
        f"the rule kept closing channels after reaching the goal (churn): "
        f"{_goal_close_actions()[closes_after_replacement:]}")
    assert _has_goal_sized_channel(), \
        "the goal-sized replacement was itself closed"
    for cid in baseline_ids:
        assert _channel(cid).get("state") not in CLOSING_STATES, \
            f"a baseline channel was closed during the convergence window: {cid}"


def test_goal_of_zero_closes_nothing(rig):
    """The off switch. With the goal at 0 the rule must not fire at all, even
    with the plugin sitting at its ceiling holding a small channel."""
    _base_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "false")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    # At the ceiling, so an open is blocked and the rule would otherwise be the
    # thing that unblocks it.
    _setcfg("plugins.inbound_liquidity.max_channels", str(len(_live_channels())))
    before = len(_goal_close_actions())
    live_before = {c["channel_id"] for c in _live_channels()}

    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _settle(rig, ticks=10)

    assert len(_goal_close_actions()) == before, \
        "the rule fired despite the goal being 0 (disabled)"
    assert {c["channel_id"] for c in _live_channels()} == live_before, \
        "a channel was closed with the goal disabled"
    _setcfg("plugins.inbound_liquidity.automation_enabled", "false")
