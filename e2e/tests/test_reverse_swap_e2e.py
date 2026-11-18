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
    set_client_plugin_setting,
    CLIENT,
    electrum_cli,
    mine,
    wait_wallet_height,
    wallet_path,
)


def _setcfg(key: str, value: str) -> None:
    """Set a config key -- or, for one of the plugin's settings, the CLIENT
    WALLET's value of it.

    The plugin's settings live in the wallet now, so `setconfig
    plugins.inbound_liquidity.<key>` configures nothing. Routed here rather than
    at the ~300 call sites, which all read the same either way: "configure the
    plugin thus". Non-plugin keys (lightning_max_funding_sat, use_gossip, ...)
    are Electrum's own and still go to the config."""
    prefix = "plugins.inbound_liquidity."
    if key.startswith(prefix):
        set_client_plugin_setting(key[len(prefix):], value)
        return
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


# Wallet-db keys, as literal strings -- matching the convention the rest of this
# module already uses (see `_dev_fee_owed`).
#
# Deliberately NOT read off the plugin by importing it here. The plugin registers
# its ConfigVars on SimpleConfig at import time, into a process-global key
# registry, and this pytest process already holds it as
# `electrum.plugins.inbound_liquidity`; importing it a second time under another
# module name re-runs the module body and dies with
# "duplicate config key str: plugins.inbound_liquidity.automation_enabled".
# (The plugin's own comments flag that double-import hazard.)
#
# The risk a literal carries is that a renamed key silently reads `{}` and the
# assertion passes for the wrong reason. That is closed in the unit suite, where
# importing the plugin IS safe: test_swap_completion_confirmation_glue.py pins
# both of these strings against the real constants.
PENDING_SWAPS_DB_KEY = "inbound_liquidity_pending_swap_providers"
USAGE_TOTALS_DB_KEY = "inbound_liquidity_usage_totals"


def _pending_swaps() -> Dict:
    return _wallet_db().get(PENDING_SWAPS_DB_KEY, {}) or {}


def _usage_totals() -> Dict:
    return _wallet_db().get(USAGE_TOTALS_DB_KEY, {}) or {}


def _dev_fee_owed() -> int:
    return int(_wallet_db().get("inbound_liquidity_dev_fee_owed_sat", 0) or 0)


def _dev_fee_charged() -> int:
    """Total dev fee this wallet has been charged: still owed, plus already paid.

    The durable observable, and the only correct one. ``owed`` alone is transient
    and racy: the rig configures a working local LNURL payout stub, and
    ``_evaluate`` runs `_reconcile_pending_swaps` (which ACCRUES the fee) and
    `_maybe_pay_dev_fee` (which pays it out, zeroing the ledger) in the SAME tick.
    Once the accrued amount clears the 1000-sat payout threshold -- which 0.5% of
    a ~700k-sat swap does -- ``owed`` returns to 0 before any outside poll can see
    it.

    That race is new. The fee used to accrue synchronously in the executor, a
    different path at a different moment from the payout tick, so a window
    existed; now both happen in one tick. Summing the two makes the assertion
    independent of whether the payout has fired yet, which is what the tests
    actually mean by "a fee was charged".
    """
    totals = _usage_totals()
    return _dev_fee_owed() + int(totals.get("dev_fee_paid_total_sat", 0) or 0)


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
    assert _wait_until(lambda: _dev_fee_charged() > 0, rig=rig, timeout=600), \
        "no dev fee was charged after the reverse swap completed"


def test_the_dev_fee_waits_for_the_funding_tx_to_confirm(rig):
    """A real swap's fee is granted by RECONCILIATION after a confirmation, not
    by the executor on first sight of a funding txid.

    Why this needs the rig. The executor sees ``swap.funding_txid`` as soon as the
    swap manager's watcher spots a matching output in an UNCONFIRMED transaction,
    and Electrum notes it can still change under RBF
    (``submarine_swaps.py``: "swap.funding_txid can change due to RBF"). So the
    plugin logs the action immediately but defers the provider's success, the dev
    fee and the lifetime totals until the funding confirms. Only a real swap
    against a real provider on a real chain exercises that ordering; the glue
    tests (test_swap_completion_confirmation_glue.py) pin the arms in isolation.

    Asserted here, in order:
      * the swap action is logged while its pending record still exists -- i.e.
        the executor committed but did not complete it;
      * once blocks are mined, the fee accrues and the record is consumed.
    """
    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _dev_fee_charged() == 0, "a dev fee was already charged before the swap"

    # The action appears as soon as funding is SEEN.
    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=240), \
        "plugin never executed a reverse swap"

    # The swap is tracked for reconciliation rather than being completed inline.
    # (Checked as "it was tracked at some point", because reconciliation runs on
    # the very next tick and may legitimately have consumed it already once the
    # rig's 30s block interval has produced a confirmation.)
    tracked_or_settled = (
        bool(_pending_swaps())
        or _dev_fee_charged() > 0)
    assert tracked_or_settled, (
        "the swap was neither tracked for reconciliation nor already settled; "
        "the executor appears to have completed it without tracking")

    # With blocks mined, it reconciles: the fee accrues and the record clears.
    #
    # The timeout has to clear the HEARTBEAT, not just a confirmation. The fee is
    # granted by `_reconcile_pending_swaps`, which runs on an evaluation TICK, and
    # a funding tx merely confirming need not change any wallet balance -- so it
    # need not produce the material-state change that bypasses the auto-eval
    # backoff. In the worst case the next tick is the heartbeat
    # (HEARTBEAT_INTERVAL_SEC = 600s). In practice it resolves far sooner, once
    # the claim lands and moves the on-chain balance.
    #
    # An earlier version of this test allowed 180s and failed here for that
    # reason alone, while the swap itself had completed perfectly well.
    assert _wait_until(lambda: _dev_fee_charged() > 0, rig=rig, timeout=750), \
        "the dev fee was never charged after the funding tx confirmed"
    assert _wait_until(
        lambda: not _pending_swaps(),
        rig=rig, timeout=180), \
        "the pending-swap record was never consumed by reconciliation"

    # And the lifetime totals were granted exactly once alongside it.
    totals = _usage_totals()
    assert int(totals.get("swaps_completed_total", 0)) >= 1, \
        f"the completed swap was not counted in the lifetime totals: {totals}"
