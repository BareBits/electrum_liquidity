"""End-to-end test for the offline-channel discount against ``max_channels``,
exercised through the REAL rig: bitcoind + Fulcrum + nostr + three headless
Electrum daemons (the client with the inbound-liquidity plugin, plus TWO swap
providers), a real funded wallet and real Lightning channels.

Scenario (driven only by the live plugin's own event loop and the real
``wallet.db``):

  * The rig opens 3 baseline channels of its own -- two to PARTNER, one to
    PARTNER2 -- none of them plugin-opened.
  * The plugin opens ONE channel of its own to PARTNER (strict preferred
    partners, PARTNER first), taking the wallet to ``max_channels`` = 4. With the
    ceiling reached, further opens are declined.
  * PARTNER is then killed. Three channels now have a dead peer, but only ONE of
    them was opened by the plugin, and only that one is discounted -- the
    decision log's own ``effective_channels`` is asserted to drop by exactly one.
  * That freed slot is spent on a replacement, which must go to PARTNER2: the
    dead peer is excluded from partner selection even though
    ``one_channel_per_peer`` is off, because re-opening to a node that has gone
    dark would buy nothing.
  * The dead channel is still OPEN while this happens (the force-close horizon is
    set days out), which is the whole point: the wallet does not have to wait out
    the auto-close to regain its ability to receive.
  * Finally the ``offline_open_allowance`` cap is asserted: the wallet settles at
    ``max_channels + 1`` channels and the next open is declined naming the
    allowance, rather than opening one replacement per dead channel.

Two things are NOT covered here, deliberately, and both are covered by the unit
suites instead:

  * the discount's second prong (peer offline now AND >= 2 recorded offline
    faults). Offline faults are rate-limited to one per peer per 24h, so
    reaching two takes two days of wall clock -- untestable in a rig run. Pinned
    in ``tests/test_offline_channel_discount.py`` (the rule) and
    ``tests/test_offline_discount_glue.py`` (the tally, its rate limit and its
    reset when the peer comes back).
  * the ALLOWANCE branch of the open decline. This topology discounts exactly
    one channel, so once its replacement is open the effective count is back at
    ``max_channels`` and the ceiling is what binds; the allowance only becomes
    the binding bound with two or more channels discounted at once. What IS
    asserted here is the behaviour that matters -- the channel count never
    exceeds ``max_channels + allowance`` -- with the branch's own decline text
    pinned in ``test_open_rule_names_the_allowance_when_that_is_the_binding_bound``.

Heavy and slow (~6-10 min) and needs the electrum venv + docker. Function-scoped
rig per test; it wipes ``.run`` and kills any previous rig, so it must NOT run
while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_offline_discount_e2e.py -q -s
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Set

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig.services import (  # noqa: E402
    set_client_plugin_setting,
    CLIENT,
    PARTNER,
    electrum_cli,
    mine,
    stop_daemon,
    wait_wallet_height,
    wallet_path,
)

# Cap each plugin open small so the plugin opens modest channels and the 12 BTC
# wallet comfortably funds both of them.
FUND_CAP_SAT = 1_000_000
# The rig's own baseline topology with --second-provider.
BASELINE_CHANNELS = 3
# One plugin channel on top of the baseline: the ceiling the test starts at.
MAX_CHANNELS = BASELINE_CHANNELS + 1
# ...and how far the discount may push past it.
OFFLINE_OPEN_ALLOWANCE = 1
# Tiny uptime window (~130s) so a killed peer is committed to closing in minutes,
# but a force-close horizon DAYS out so the dead channel is still OPEN while the
# replacement is opened -- that is exactly what this test is about.
UPTIME_WINDOW_DAYS = 130.0 / 86400.0
FORCE_CLOSE_DAYS = 5.0
CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}


def _setcfg(key: str, value: str) -> None:
    """Set a config key -- or, for one of the plugin's settings, the CLIENT
    WALLET's value of it (the plugin's settings live in the wallet)."""
    prefix = "plugins.inbound_liquidity."
    if key.startswith(prefix):
        set_client_plugin_setting(key[len(prefix):], value)
        return
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _setcfg_float(key: str, value: float) -> None:
    """Set a *float* plugin config value over the CLI so it actually persists.

    ``setconfig`` parses floats as ``Decimal`` and stores ``plugins.*`` keys via
    raw ``set_key``, which cannot ``json.dumps`` a ``Decimal`` and SILENTLY DROPS
    the value. A leading ``+`` makes the token invalid JSON yet a valid Python
    float literal, so the CLI's ``ast.literal_eval`` fallback yields a real float
    that persists. (Same workaround as test_offline_autoclose_e2e.)
    """
    _setcfg(key, f"+{value:.8f}")


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


def _live_channels() -> int:
    return sum(1 for c in _channels() if c.get("state") not in CLOSING_STATES)


def _state_of(cid: str) -> str:
    for c in _channels():
        if c["channel_id"] == cid:
            return c.get("state", "")
    return ""


def _peer_of(cid: str) -> str:
    for c in _channels():
        if c["channel_id"] == cid:
            return (c.get("remote_pubkey") or c.get("node_id") or "").lower()
    return ""


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _plugin_opened() -> List[str]:
    raw = _wallet_db().get("inbound_liquidity_plugin_opened_channels", [])
    return raw if isinstance(raw, list) else []


def _close_intents() -> Dict:
    raw = _wallet_db().get("inbound_liquidity_close_intent", {})
    return raw if isinstance(raw, dict) else {}


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _open_declines() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "decline" and e.get("kind") == "open"]


def _short_id_of(cid: str) -> str:
    for c in _channels():
        if c["channel_id"] == cid:
            return c.get("short_channel_id") or ""
    return ""


def _discounted(state: Dict) -> List[Dict]:
    """The per-channel entries a state dict marks as offline-discounted."""
    return [c for c in state.get("channels", []) if c.get("offline_discounted")]


def _latest_state() -> Optional[Dict]:
    """The state dict of the most recent log entry that captured one."""
    for entry in reversed(_decision_log()):
        state = entry.get("state")
        if isinstance(state, dict) and "effective_channels" in state:
            return state
    return None


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


def _hold(cond: Callable[[], bool], *, rig, seconds: float) -> bool:
    """Keep ticking for ``seconds`` and report whether ``cond`` held throughout.
    Used for the "and nothing more happens" assertions, which a one-shot check
    cannot make."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not cond():
            return False
        _mine(rig, 1)
        time.sleep(1.5)
    return cond()


@pytest.fixture
def rig():
    run_mod._ensure_marked()
    # --second-provider gives the replacement somewhere to go: with only PARTNER
    # in the graph, killing it would leave no reachable partner at all and the
    # freed slot could not be spent.
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--second-provider"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_offline_channel_frees_a_slot_for_a_replacement(rig):
    assert rig.partner2_nodeid, "rig did not bring up a second provider"
    baseline = _channel_ids()
    assert len(baseline) == BASELINE_CHANNELS

    partner_pubkey = rig.partner_nodeid.split("@", 1)[0].lower()
    partner2_pubkey = rig.partner2_nodeid.split("@", 1)[0].lower()

    # Strict preferred partners, PARTNER first: the plugin's first open is to
    # PARTNER (deterministically -- Electrum's own suggestions are kept out of
    # the try-order), which is the peer the test then kills.
    _setcfg("plugins.inbound_liquidity.preferred_partners",
            ",".join([rig.partner_nodeid, rig.partner2_nodeid]))
    _setcfg("plugins.inbound_liquidity.partners_strict", "true")
    # Off, so the plugin may open alongside the rig's baseline channels to the
    # same peers -- and so the dead-peer exclusion is the ONLY thing keeping the
    # replacement away from PARTNER.
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", str(MAX_CHANNELS))
    _setcfg("plugins.inbound_liquidity.offline_open_allowance",
            str(OFFLINE_OPEN_ALLOWANCE))
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_channel_size_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "99")
    # No swaps and no undersized-channel replacement: the only moves under test
    # are opens, and a goal of 0 disables the replacement rule entirely.
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    # Auto-close ON (its close intent is the discount's evidence) but with the
    # force-close horizon days out, so the dead channel stays OPEN for the whole
    # test and the replacement is demonstrably opened ALONGSIDE it.
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "true")
    _setcfg_float("plugins.inbound_liquidity.offline_uptime_window_days",
                  UPTIME_WINDOW_DAYS)
    _setcfg("plugins.inbound_liquidity.offline_min_uptime_pct", "10")
    _setcfg_float("plugins.inbound_liquidity.offline_force_close_days",
                  FORCE_CLOSE_DAYS)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    # --- the plugin fills the last slot ----------------------------------
    assert _wait_until(lambda: _live_channels() >= MAX_CHANNELS, rig=rig, timeout=180), \
        "plugin never opened its own channel"
    assert _wait_until(lambda: len(_plugin_opened()) >= 1, rig=rig, timeout=60), \
        "plugin-opened channel was not tagged in wallet.db"
    plugin_cid = _plugin_opened()[0]
    assert plugin_cid not in baseline
    assert _peer_of(plugin_cid) == partner_pubkey, \
        "strict preferred partners should have put the first open on PARTNER"
    assert _wait_until(lambda: _state_of(plugin_cid) == "OPEN", rig=rig, timeout=180), \
        "plugin channel never reached OPEN"

    # At the ceiling with every peer healthy: no further open, and the decline
    # says so. This is the "before" state the discount then changes.
    assert _wait_until(
        lambda: any("at max channels" in (e.get("reason") or "")
                    for e in _open_declines()),
        rig=rig, timeout=120), "expected an 'at max channels' decline at the ceiling"
    assert _live_channels() == MAX_CHANNELS

    # --- the peer dies ----------------------------------------------------
    stop_daemon(PARTNER)

    # The watchdog commits to closing the plugin channel (uptime below the
    # floor). That intent is the discount's evidence.
    assert _wait_until(lambda: plugin_cid in _close_intents(), rig=rig, timeout=300), \
        "watchdog never committed a close intent for the offline plugin channel"

    # --- the freed slot is spent on a replacement, to the LIVE peer -------
    assert _wait_until(lambda: _live_channels() >= MAX_CHANNELS + 1,
                       rig=rig, timeout=300), \
        "plugin never opened a replacement for the offline channel"
    assert _wait_until(lambda: len(_plugin_opened()) >= 2, rig=rig, timeout=60), \
        "the replacement was not tagged as plugin-opened"
    replacement_cid = [c for c in _plugin_opened() if c != plugin_cid][0]
    assert _peer_of(replacement_cid) == partner2_pubkey, (
        "the replacement must go to the LIVE provider, not back to the node that "
        f"went dark (got peer {_peer_of(replacement_cid)[:16]}…)")
    # The dead channel is still open: this was an extra channel alongside it, not
    # a slot that the auto-close had already given back.
    assert _state_of(plugin_cid) not in CLOSING_STATES, \
        "the dead channel should still be OPEN (force-close horizon is days out)"

    # --- SCOPE: only the plugin's dead channel is discounted --------------
    # Three channels have a dead peer by now (the two baseline ones plus the
    # plugin's), and the plugin must discount ONLY the one it opened -- it will
    # never close a hand-opened channel, so discounting one would put the wallet
    # permanently over its ceiling.
    #
    # Asserted as a property of whatever the latest state is, NOT as an absolute
    # count at one instant: the replacement is opened within a tick or two of the
    # slot freeing up, so there is no reliable moment at which to catch
    # "effective == max - 1" before it is spent.
    assert _wait_until(lambda: len(_discounted(_latest_state() or {})) == 1,
                       rig=rig, timeout=180), (
        "expected exactly one discounted channel; last state: "
        f"{_latest_state()}")
    state = _latest_state() or {}
    (discounted,) = _discounted(state)
    assert discounted.get("is_plugin_opened") is True, \
        "the discounted channel must be the plugin's own"
    assert discounted["short_id"] == _short_id_of(plugin_cid)
    # ...and the dead channels the plugin did NOT open are still counted, even
    # though they read just as inactive.
    dead_not_ours = [c for c in state.get("channels", [])
                     if not c.get("is_active") and not c.get("is_plugin_opened")]
    assert dead_not_ours, "expected the baseline channels to the dead peer"
    assert not any(c.get("offline_discounted") for c in dead_not_ours), \
        "a channel the user opened by hand must never be discounted"
    # The ceiling is measured against the reduced count, and the discount is
    # worth exactly the one channel above.
    assert state["effective_channels"] == state["num_channels"] - 1

    # --- the allowance caps the overshoot --------------------------------
    assert _hold(lambda: _live_channels() <= MAX_CHANNELS + OFFLINE_OPEN_ALLOWANCE,
                 rig=rig, seconds=90), (
        "the offline-replacement allowance was exceeded: the plugin opened more "
        "than one replacement")
    # The decline that now holds says so, and names the discount that got us
    # here. (It is the CEILING branch, not the allowance branch: with the
    # replacement open, the effective count is back at max_channels. Reaching the
    # allowance branch needs two or more channels discounted at once -- see the
    # note in the module docstring.)
    assert _wait_until(
        lambda: any("offline channel(s) already discounted" in (e.get("reason") or "")
                    for e in _open_declines()),
        rig=rig, timeout=120), (
        "expected the 'at max channels' decline to report the discounted channel; "
        f"declines so far: {[e.get('reason') for e in _open_declines()]}")

    # Scope, once more: the two baseline channels to the dead peer are NOT
    # plugin-opened, so the watchdog must not have touched them either.
    for cid in baseline:
        assert _state_of(cid) not in CLOSING_STATES, \
            f"baseline channel {cid[:12]} was closed but is not plugin-opened"
