"""End-to-end test that the plugin drains a channel into a LIQUIDITY SINK, and
halves the payment until one routes.

A sink is a Lightning address the plugin pays instead of reverse-swapping
on-chain. The liquidity effect is identical -- local balance leaves the channel,
inbound capacity comes back -- but it settles in seconds, needs no provider, no
on-chain claim and no cost gate.

The halving retry exists because a big payment can fail where a smaller one
succeeds, and proving that needs a route with REAL, limited capacity. On the
default rig it cannot be done: with ``use_gossip=false`` the client can only pay
its direct peers, and on a direct channel Electrum's ``available_to_spend``
already folds in the reserve, the fee-spike buffer, ``remaining_max_inflight``
and HTLC slots -- with the plugin subtracting more headroom still -- so the
ladder's top rung is always sized to succeed and halving has nothing to fix.

So this runs the rig in ``--gossip`` mode, which uses the real channel graph and
adds an announced but deliberately under-funded forwarding hop:

    client --[0.02 BTC, ~960k sendable]--> partner --[300k forwardable]--> partner2
                                                                             |
                                        the liquidity sink's invoices are minted here

Every rung is a genuine payment attempt over that topology, and the failures are
genuine capacity failures reported by the partner -- nothing is stubbed except
which node mints the invoice.

What is asserted:
  * the plugin pays the sink rather than reverse-swapping,
  * the FIRST rungs fail on real capacity and a halved one settles (the action
    records which attempt it was),
  * inbound liquidity actually increased -- a real payment, not just a log line,
  * the dev fee accrued on the amount drained,
  * with submarine swaps disabled, a sink that cannot deliver produces a decline
    and NO swap.

Heavy and slow (~10-15 min); needs the electrum venv + docker. Function-scoped
rig (wipes .run, kills any previous rig), so it must NOT run while a manual
run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_liquidity_sink_e2e.py -q -s
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


def _actions(kind: str) -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == kind]


def _declines() -> List[Dict]:
    return [e for e in _decision_log() if e.get("category") == "decline"]


def _dev_fee_owed() -> int:
    try:
        return int(_wallet_db().get("inbound_liquidity_dev_fee_owed_sat", 0))
    except (TypeError, ValueError):
        return 0


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


def _arm_sink_config(rig, *, disable_swaps: bool = False) -> None:
    """Point the plugin at the rig's sink and make it want to drain.

    ``max_channels``/``liquidity_goal_sat`` are pinned so the engine reaches the
    drain rules rather than deciding to open or replace a channel first, and the
    swap fee ceiling is left permissive so that when swaps ARE enabled the
    fallback is a real possibility rather than something the cost gate would
    have declined anyway.
    """
    assert rig.sink_stub is not None, "rig was not brought up in --gossip mode"
    _setcfg("plugins.inbound_liquidity.sink_address",
            rig.sink_stub.lightning_address)
    _setcfg("plugins.inbound_liquidity.disable_submarine_swaps",
            "true" if disable_swaps else "false")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")


@pytest.fixture
def rig():
    """A gossip-mode rig: announced channels, a routed two-hop path to the sink,
    and a forwarding hop too small to carry the first rungs of the ladder.

    One client channel (not the usual two) so the ladder under test is
    unambiguous: with two, both would be drained through the same starved hop and
    the second one's rungs would depend on what the first had already consumed.
    """
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--gossip", "--channels", "1"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_sink_payment_halves_until_it_routes_and_creates_inbound(rig):
    # Topology precondition: exactly one client channel, and partner2 reachable
    # only by routing (no client->partner2 channel).
    assert rig.sink_stub is not None
    chans = _channels()
    assert len(chans) == 1, f"expected a single client channel, got {len(chans)}"
    baseline_inbound = _max_inbound()
    baseline_owed = _dev_fee_owed()

    _arm_sink_config(rig)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) The sink is paid. Generous timeout: the first rungs must genuinely fail
    #    (one with no route at all, one on the hop's real balance) before a rung
    #    small enough to forward is reached.
    assert _wait_until(lambda: len(_actions("sink")) >= 1, rig=rig, timeout=600), \
        ("plugin never paid the liquidity sink; declines were: "
         + json.dumps([d.get("reason") for d in _declines()][-5:]))
    action = _actions("sink")[0]

    # 2) It took halving to get there: the executor records the attempt index on
    #    any rung that was not the first one tried.
    detail = action.get("detail", "")
    assert "attempt " in detail, \
        f"sink settled on the FIRST rung -- halving was never exercised: {detail!r}"
    log_text = _client_log_text()
    assert "halving the liquidity-sink payment" in log_text, \
        "no halving was logged -- the ladder did not advance past its top rung"

    # 3) The amount paid is below the forwarding hop's capacity, and at or above
    #    the plugin's 10_000 sat floor.
    paid = int(action.get("amount_sat") or 0)
    hop_forwardable = int(run_mod.GOSSIP_HOP_PUSH_BTC * 1e8)
    assert paid >= 10_000, f"paid {paid} sat, below the sink floor"
    assert paid < hop_forwardable, \
        (f"paid {paid} sat, which the {hop_forwardable} sat forwarding hop "
         f"could not have carried -- the ladder did not really halve")

    # 4) A real payment: the sink was paid over Lightning, not merely logged.
    #    The log's `dest` is abbreviated (the plugin elides identities), so the
    #    full address is checked in the detail line where it is recorded whole.
    assert rig.sink_stub.lightning_address in detail, \
        f"sink action did not record the sink address: {action!r}"
    assert _wait_until(lambda: _max_inbound() > baseline_inbound + paid // 2,
                       rig=rig, timeout=300), \
        (f"inbound liquidity did not increase after a {paid} sat sink payment "
         f"(max remote_balance={_max_inbound()}, baseline={baseline_inbound})")

    # 5) The dev fee accrued on what was drained.
    assert _dev_fee_owed() > baseline_owed, \
        "no dev fee accrued on the sink payment"

    # 6) No reverse swap was made: the sink is the plugin's first choice, and it
    #    succeeded, so the fallback must never have run.
    assert not _actions("swap"), \
        "a reverse swap ran even though the sink payment settled"


def test_sink_only_mode_declines_instead_of_swapping(rig):
    # With swaps disabled and a sink that cannot deliver, the plugin must say so
    # rather than quietly reverse-swapping -- the user asked for no swaps.
    _arm_sink_config(rig, disable_swaps=True)
    # An address that resolves but has no LNURL-pay endpoint behind it: every
    # rung is unusable, so the ladder cannot deliver at any size.
    _setcfg("plugins.inbound_liquidity.sink_address",
            "nobody@127.0.0.1:1")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    # Give it several evaluation cycles to try and fail.
    assert _wait_until(
        lambda: "liquidity sink" in _client_log_text()
        and "could not be resolved" in _client_log_text(),
        rig=rig, timeout=420), \
        "the plugin never attempted (and failed) the unreachable sink"

    assert not _actions("swap"), \
        ("a reverse swap was made despite disable_submarine_swaps -- the switch "
         "must be honoured even when the sink fails")
    assert not _actions("sink"), "a sink payment settled against a dead endpoint"
