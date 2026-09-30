"""End-to-end test for the max-channel-size ceiling, exercised through the REAL
rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons, a funded wallet
and real Lightning channels.

The open rule funds a channel with "everything on-chain bar the reserve". The rig's
client wallet holds ~12 BTC, so before this ceiling existed the plugin's first open
committed essentially the whole balance to ONE channel with ONE peer -- the exact
behaviour the ceiling exists to stop. That makes the rig the right place to prove
it: the wallet really is rich, the funding tx is really built and broadcast, and
the resulting channel's capacity is a fact on a regtest chain rather than an
arithmetic claim.

What this proves, end to end:

  1. the channel the plugin opens is funded at the CEILING, not at the balance --
     a real funding tx, mined, with the capacity to show for it;
  2. the rest of the balance is still there afterwards. The ceiling bounds one
     channel; it does not strand the remainder;
  3. that remainder then funds the NEXT channel, also at the ceiling. This is the
     whole point of the feature -- a large balance builds several channels of a
     sane size instead of one giant one -- and it is the part no unit test can
     show, because it needs two real opens against a real chain;
  4. the decision log says the ceiling set the size, so a user who sees a channel
     smaller than their balance can find out why.

Every other open-related e2e steers channel size with ``lightning_max_funding_sat``,
Electrum's own limit. This one must not: it has to prove the PLUGIN's ceiling is
what bound the amount. So that knob is left untouched at Electrum's default
(``LN_MAX_FUNDING_SAT_LEGACY``, 16_777_215), comfortably above the ceiling under
test -- and the second test below, which turns our ceiling off, then finds
Electrum's limit as the only thing left bounding an open.

Heavy and slow (~5-8 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_max_channel_size_e2e.py -q -s
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

# The ceiling under test. Well above Electrum's MIN_FUNDING_SAT (200_000) and above
# the rig's baseline channels (2_000_000 sat) so the plugin's channels are
# distinguishable by size alone, yet a tiny fraction of the wallet's ~12 BTC -- so
# an unbounded open would be unmistakably larger.
MAX_CHANNEL_SIZE_SAT = 3_000_000
# The on-chain reserve the open leaves behind, and the min balance needed to open.
RESERVE_SAT = 10_000
# How much richer than the ceiling the wallet must be for this test to mean
# anything: without a large multiple, "capacity == ceiling" could be the balance
# coinciding with the ceiling rather than the ceiling binding.
MIN_BALANCE_MULTIPLE = 4

CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _live_channels() -> List[Dict]:
    return [c for c in _channels() if c.get("state") not in CLOSING_STATES]


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


def _capacity(c: Dict) -> int:
    """A channel's capacity in sat: derived, because Electrum's ``list_channels``
    reports the two balances rather than the capacity and (absent in-flight HTLCs)
    they sum to it. Same quantity the engine compares against the goal."""
    return int(c.get("local_balance") or 0) + int(c.get("remote_balance") or 0)


def _onchain_sat() -> int:
    bal = json.loads(electrum_cli("getbalance", inst=CLIENT))
    return sum(int(round(float(bal.get(k, 0)) * 100_000_000))
               for k in ("confirmed", "unconfirmed", "unmatured"))


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


def _declines() -> List[str]:
    return [str(e.get("reason") or "") for e in _decision_log()
            if e.get("category") == "decline"]


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        # Mining each round also satisfies the rig's "channels to the same peer
        # need a new block height between opens" constraint, which the two-channel
        # phase below depends on.
        _mine(rig, 1)
        time.sleep(period)
    return cond()


def _arm_config(*, max_channels: int, max_opens: int) -> None:
    """Let the plugin open channels, bounded by the ceiling, and do nothing else.

    ``one_channel_per_peer`` must be off: the rig's swap partner is the only node
    available, and the guard would (correctly) veto a second channel to it -- which
    is exactly what the build-out phase needs.

    Both swap triggers are put out of reach so no reverse swap ever fires. A swap
    would move a channel's local balance back on-chain, which would muddy both the
    "the rest of the balance is still there" and the capacity assertions.
    ``swap_trigger_pct`` has to exceed 100: at exactly 100 a fresh channel
    (local == capacity) is still over the trigger.
    """
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", str(max_channels))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", str(max_opens))
    _setcfg("plugins.inbound_liquidity.max_channel_size_sat", str(MAX_CHANNEL_SIZE_SAT))
    _setcfg("plugins.inbound_liquidity.min_onchain_to_open_sat", "60000")
    _setcfg("plugins.inbound_liquidity.onchain_reserve_sat", str(RESERVE_SAT))
    # The goal must not exceed the ceiling, or the replacement rule's churn guard
    # would (correctly) refuse to act and the plugin's channels would look
    # permanently undersized. Below the ceiling here, so nothing is replaced.
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "1000000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "1000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.auto_remediate_stuck_open", "false")
    _setcfg("plugins.inbound_liquidity.dev_fee_pct", "0")
    _setcfg("plugins.inbound_liquidity.diag_log_enabled", "true")


@pytest.fixture
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


def test_the_ceiling_bounds_a_real_open_and_the_rest_builds_out(rig):
    baseline = _channel_ids()
    assert len(baseline) == 2, f"expected the 2 baseline channels, got {len(baseline)}"

    # --- the precondition that gives this test its meaning ----------------
    # A wallet far richer than the ceiling. Without this, "capacity == ceiling"
    # would prove nothing: it could just be the balance.
    balance_before = _onchain_sat()
    assert balance_before > MAX_CHANNEL_SIZE_SAT * MIN_BALANCE_MULTIPLE, (
        f"precondition: the wallet ({balance_before} sat) must hold far more than "
        f"the {MAX_CHANNEL_SIZE_SAT} sat ceiling, or an unbounded open would be "
        f"indistinguishable from a bounded one")

    # --- phase 1: ONE channel, funded at the ceiling ----------------------
    _arm_config(max_channels=3, max_opens=1)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: len(_live_channels()) >= 3, rig=rig, timeout=300), (
        f"plugin never opened a channel (live={len(_live_channels())}, "
        f"declines={_declines()!r})")
    assert _wait_until(lambda: len(_plugin_opened()) >= 1, rig=rig, timeout=90), \
        "plugin opened a channel but never tagged it as its own"

    new_ids = _channel_ids() - baseline
    assert len(new_ids) == 1, f"expected exactly one new channel, got {new_ids}"
    first_id = next(iter(new_ids))
    assert first_id in _plugin_opened()
    first = next(c for c in _channels() if c["channel_id"] == first_id)

    # 1) THE FEATURE: funded at the ceiling, not at the balance.
    first_capacity = _capacity(first)
    assert first_capacity == MAX_CHANNEL_SIZE_SAT, (
        f"the plugin's channel is {first_capacity} sat, not the configured "
        f"{MAX_CHANNEL_SIZE_SAT} sat ceiling")
    # Stated the other way round, because THIS is the bug the feature fixes: the
    # channel must be nowhere near the whole balance.
    assert first_capacity < balance_before // MIN_BALANCE_MULTIPLE, (
        f"the ceiling did not bind: {first_capacity} sat of a {balance_before} sat "
        f"wallet went into one channel")

    # 2) The rest of the balance is still on-chain -- bounded, not stranded, and
    #    not silently spent. (Mining fees and the reserve make this approximate;
    #    what matters is the order of magnitude.)
    balance_after_first = _onchain_sat()
    assert balance_after_first > balance_before - MAX_CHANNEL_SIZE_SAT * 2, (
        f"far more than one capped channel left the wallet: {balance_before} -> "
        f"{balance_after_first}")
    assert balance_after_first > MAX_CHANNEL_SIZE_SAT, (
        "not enough left on-chain to fund another channel, so phase 2 cannot "
        "prove anything")

    # 4) The decision log explains the size, so a channel smaller than the
    #    balance is not a mystery to the user who reads it.
    opens = [e for e in _open_actions() if int(e.get("amount_sat") or 0) > 0]
    assert opens, f"no open action logged; log={_decision_log()!r}"
    assert opens[-1]["amount_sat"] == MAX_CHANNEL_SIZE_SAT
    assert "capped" in str(opens[-1].get("reason") or ""), (
        f"the open's reason does not say the ceiling set the size: "
        f"{opens[-1].get('reason')!r}")

    # --- phase 2: the remainder funds the NEXT channel, also at the ceiling ---
    # The claim the feature actually makes: a large balance becomes SEVERAL sane
    # channels, not one giant one and a stranded remainder.
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "5")
    _setcfg("plugins.inbound_liquidity.max_channels", "4")

    assert _wait_until(lambda: len(_live_channels()) >= 4, rig=rig, timeout=300), (
        f"plugin never opened a second channel (live={len(_live_channels())}, "
        f"declines={_declines()!r})")
    assert _wait_until(lambda: len(_plugin_opened()) >= 2, rig=rig, timeout=90), \
        "the second channel was never tagged as plugin-opened"

    second_ids = _channel_ids() - baseline - {first_id}
    assert len(second_ids) == 1, f"expected one more channel, got {second_ids}"
    second = next(c for c in _channels()
                  if c["channel_id"] == next(iter(second_ids)))

    # 3) Also at the ceiling -- the ceiling is per-channel, applied every time,
    #    not a one-off bound on the first open.
    assert _capacity(second) == MAX_CHANNEL_SIZE_SAT, (
        f"the second channel is {_capacity(second)} sat, not the "
        f"{MAX_CHANNEL_SIZE_SAT} sat ceiling")

    # Two capped channels' worth has left the wallet, and no more.
    balance_end = _onchain_sat()
    assert balance_end < balance_after_first, "the second open spent nothing"
    assert balance_end > balance_before - MAX_CHANNEL_SIZE_SAT * 3, (
        f"more than two capped channels' worth left the wallet: {balance_before} "
        f"-> {balance_end}")

    # Nothing in all that was declined for a reason the ceiling should have
    # prevented: the churn guard and the below-the-floor guard are both about
    # MISCONFIGURATION, and this configuration is sound. Matched on each guard's
    # own distinctive phrase rather than on loose fragments -- the log also carries
    # ordinary declines ("at max channels", the daily ceiling), and a substring
    # broad enough to collide with those would fail for the wrong reason.
    joined = " | ".join(_declines())
    assert "would only reopen another one" not in joined, (
        f"the ceiling/goal churn guard fired on a sound configuration: {joined}")
    assert "channel-funding floor, so no channel can ever be opened" not in joined, \
        joined


def test_no_ceiling_opens_with_everything(rig):
    """The other side of the switch, and the pre-feature behaviour: with the
    ceiling off (0) the plugin funds a channel with the whole balance bar the
    reserve, exactly as it always did.

    Worth an e2e of its own because 0 is the one value that must NOT flow into a
    ``min()`` -- reading it as a ceiling of zero would silently stop the plugin
    opening channels at all, and a wallet that quietly never opens anything is the
    hardest kind of regression to notice.
    """
    baseline = _channel_ids()
    assert len(baseline) == 2

    balance_before = _onchain_sat()
    _arm_config(max_channels=3, max_opens=1)
    _setcfg("plugins.inbound_liquidity.max_channel_size_sat", "0")
    # `lightning_max_funding_sat` is deliberately left at Electrum's default
    # (LN_MAX_FUNDING_SAT_LEGACY, 16_777_215) rather than raised to the balance:
    # above that value a channel needs the peer to support wumbo, and what is
    # under test is our ceiling being absent, not Electrum's being raised. With a
    # ~12 BTC wallet, Electrum's limit is therefore the only thing bounding the
    # open -- which is precisely the pre-feature state.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: len(_live_channels()) >= 3, rig=rig, timeout=300), (
        f"plugin opened nothing with the ceiling off -- 0 was read as a ceiling of "
        f"zero rather than as 'no ceiling' (declines={_declines()!r})")

    new_ids = _channel_ids() - baseline
    assert len(new_ids) == 1, f"expected exactly one new channel, got {new_ids}"
    capacity = _capacity(next(c for c in _channels()
                              if c["channel_id"] == next(iter(new_ids))))

    # Far larger than the ceiling the other test uses -- emphatically not bounded
    # by anything of ours.
    assert capacity > MAX_CHANNEL_SIZE_SAT * MIN_BALANCE_MULTIPLE, (
        f"with no ceiling the channel should be bounded only by Electrum's own "
        f"limit against a {balance_before} sat balance, but it is only "
        f"{capacity} sat")
    # ...and the reason must not claim a cap set the size.
    opens = [e for e in _open_actions() if int(e.get("amount_sat") or 0) > 0]
    assert opens
    assert "capped" not in str(opens[-1].get("reason") or ""), (
        f"an uncapped open claims it was capped: {opens[-1].get('reason')!r}")
