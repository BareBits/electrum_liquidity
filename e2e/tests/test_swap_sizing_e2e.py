"""End-to-end proof that a max-sized reverse swap actually routes, across a
RANGE of channel sizes, against the live rig.

The bug this pins down: the plugin sized a reverse swap at Electrum's own
max-send figure (``num_sats_can_send`` == raw ceiling minus ONE payment's
routing-fee reserve). A reverse swap is TWO payments down one channel -- the
main hold invoice plus a mining-fee prepayment -- and Electrum issues them
concurrently, pinning only the main one to our chosen channel. So the prepayment
frequently lands on that same channel first, and an in-flight HTLC costs the
channel MORE than its face value because it enlarges the commitment
transaction. Measured here: a 45,000 sat prepayment consumed 64,350 sat of
capacity.

The result was ``NoPathFound('Cannot distribute payment over channels.')`` on
the main leg, roughly three runs in four. It fails silently in the worst way:
the swap is accepted, never funded, and later reconciled as a stuck swap --
recording a reliability fault against a provider that did nothing wrong.

Note that Electrum's own ``client_max_amount_reverse_swap()`` is the same
formula, so the number the swap dialog's "Max" button offers has the same gap;
we deliberately do NOT reuse it (it is also wallet-wide, while our swap is
pinned to one channel).

Why sizes matter here: the headroom has a proportional part and a flat part (the
prepayment, which does not shrink with the channel). A single channel size
cannot show that both ends behave -- a large channel could pass on the
proportional term alone while a small one is dominated by the flat prepayment
reservation, or vice versa. Each size gets its own rig.

Heavy and slow: a rig per size, ~5-8 min each. Needs the electrum venv + docker.
Function-scoped rig (wipes .run, kills any previous rig), so it must NOT run
while a manual run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_swap_sizing_e2e.py -q -s
"""
from __future__ import annotations

import glob
import json
import os
import re
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

# Channel capacities to exercise, in BTC. The rig pushes half to the peer, so
# local balance is half of each. Chosen to span the regime change in the
# headroom: at 0.005 BTC the flat prepayment reservation (~45k sat) is a large
# share of a ~250k sat local balance, while at 0.05 BTC it is noise and the
# proportional term dominates. The provider's max_forward (900k sat) caps the
# largest ones, which is itself worth covering -- a capped swap must still leave
# the prepayment room.
CHANNEL_SIZES_BTC = [0.01, 0.02, 0.05]
# Too small to swap economically against this provider -- see
# test_a_channel_too_small_to_swap_declines_cleanly.
UNECONOMIC_SIZE_BTC = 0.005


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


def _max_inbound() -> int:
    return max((c["remote_balance"] for c in _channels()), default=0)


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _unroutable_legs() -> int:
    """How many swap legs Electrum could not route.

    This is the bug's fingerprint. Asserting on it rather than only on the final
    balance distinguishes "the swap worked" from "the swap worked on the third
    attempt after two unroutable ones", which a balance check alone would call a
    pass while the user's provider quietly collected reliability faults.
    """
    return _client_log_text().count("NoPathFound")


def _swap_declines() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "decline" and e.get("kind") == "swap"]


def _declined_amount(reason: str) -> int:
    """The swap size named in a decline reason ("... swap of 38801 all-in ...")."""
    match = re.search(r"swap of (\d+)", reason)
    return int(match.group(1)) if match else 0


def _unfunded_swaps() -> int:
    """Swaps the provider accepted but never funded (``funding txid: None``)."""
    return _client_log_text().count("reverse swap funding txid: None")


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
    """Make the plugin swap rather than open, at any of the sizes under test.

    The rig ships ``automation_enabled=false`` (see ``rig/services.py``), so the
    master switch is flipped LAST -- after every threshold is in place -- or the
    plugin could act on a half-configured ruleset.
    """
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "30000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    # Deliberately permissive. The rig's provider charges a FIXED 22,500 sat
    # mining fee, so on the smallest channel here a perfectly-sized swap still
    # works out near 50% all-in -- and the cost gate would decline it before the
    # routing behaviour under test ever ran. Economics are covered elsewhere
    # (test_reverse_swap_e2e keeps a realistic ceiling); what this file asserts
    # is that whatever swap the engine does choose actually ROUTES.
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "80")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last


@pytest.fixture
def sized_rig(request):
    """A fresh rig whose channels are `request.param` BTC each."""
    capacity_btc = request.param
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(
        ["--no-gui", "--channel-btc", str(capacity_btc)]))
    r.preflight()
    r.allocate()
    r.bring_up()
    r.capacity_btc = capacity_btc
    try:
        yield r
    finally:
        r.shutdown()


@pytest.mark.parametrize("sized_rig", CHANNEL_SIZES_BTC, indirect=True)
def test_a_max_swap_routes_first_time_at_this_channel_size(sized_rig):
    """A max-sized reverse swap must route on the FIRST attempt, whatever the
    channel size -- no unroutable legs, no accepted-but-unfunded swaps."""
    rig = sized_rig
    _arm_swap_config()
    baseline_inbound = _max_inbound()

    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=300), (
        f"plugin never attempted a swap on {rig.capacity_btc} BTC channels; "
        f"channels={_channels()!r}")

    assert _wait_until(lambda: _max_inbound() > baseline_inbound, rig=rig,
                       timeout=300), (
        f"inbound liquidity did not increase on {rig.capacity_btc} BTC channels "
        f"(max remote_balance={_max_inbound()}, baseline={baseline_inbound})")

    # The headline assertion: it worked *first time*. Before the sizing fix the
    # main leg was unroutable because the prepayment had already taken its value
    # -- plus the commitment cost of an extra in-flight HTLC -- out of the very
    # channel the main leg was pinned to.
    assert _unroutable_legs() == 0, (
        f"{_unroutable_legs()} swap leg(s) could not be routed at "
        f"{rig.capacity_btc} BTC -- the swap is still sized too close to what "
        f"the channel can carry")
    assert _unfunded_swaps() == 0, (
        f"{_unfunded_swaps()} swap(s) were accepted but never funded at "
        f"{rig.capacity_btc} BTC; these become stuck-swap faults against an "
        f"innocent provider")


@pytest.mark.parametrize("sized_rig", [UNECONOMIC_SIZE_BTC], indirect=True)
def test_a_channel_too_small_to_swap_declines_cleanly(sized_rig):
    """A channel whose swap cannot pay for itself must be DECLINED, not attempted.

    The rig provider charges a fixed 22,500 sat mining fee, and the on-chain
    claim costs about as much again at regtest feerates -- roughly 45,000 sat of
    fixed cost. A 0.005 BTC channel can send less than that, so no sizing makes
    the swap worth doing and the engine is right to refuse. What matters is HOW
    it refuses: a cost decline, with nothing sent. Attempting it anyway would
    burn a prepayment and leave an unfunded swap that later faults the provider.

    This also guards the sizing fix from the opposite direction. The decline
    names the amount the engine would have swapped, so an over-conservative
    headroom shows up here as that number collapsing -- it was 22,826 sat when
    the headroom reserved the prepayment's full principal, and is ~38,800 now
    that it prices the second HTLC's actual cost.
    """
    rig = sized_rig
    _arm_swap_config()

    assert _wait_until(lambda: len(_swap_declines()) >= 1, rig=rig, timeout=300), (
        f"plugin neither swapped nor explained why on {rig.capacity_btc} BTC "
        f"channels; decision log={_decision_log()[-3:]!r}")

    reason = _swap_declines()[-1].get("reason") or ""
    assert "all-in cost" in reason, (
        f"a channel that is merely too small must be declined on COST, not "
        f"silently skipped: {reason!r}")

    # Nothing was attempted: no unroutable leg, no accepted-but-unfunded swap.
    assert _unroutable_legs() == 0, "a declined swap must not have been attempted"
    assert _unfunded_swaps() == 0, "a declined swap must leave no unfunded swap"

    # And the swap it declined was a sensible size -- the headroom must not be
    # eating the channel (see the docstring).
    sized = _declined_amount(reason)
    assert sized > 30_000, (
        f"the engine only managed to size a {sized} sat swap out of a 250,000 "
        f"sat local balance; the headroom is over-reserving again")
