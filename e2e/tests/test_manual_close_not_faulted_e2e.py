"""End-to-end test for the "manual close blamed on the peer" regression, driven
through the REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons
(the client running the inbound-liquidity plugin, plus the channel partner), a
real funded wallet and real Lightning channels.

Scenario (the exact user-reported bug):

  * The rig opens two baseline channels from the client to the partner and both
    reach OPEN, with the partner online throughout.
  * The plugin is armed (automation on) but given nothing else to do -- no opens
    (already at max_channels), no swaps, no offline auto-close -- so its only
    activity is the channel-health watchdog. We wait until it has evaluated at
    least once while the channels are OPEN, so the watchdog has *seeded* them as
    healthy (a later close is then a genuine OPEN -> closing rising edge, which
    without the fix is charged to the peer).
  * The USER then closes both channels through the real CLI -- one cooperative
    close and one force-close -- which route through
    ``commands.close_channel -> lnworker.close_channel / force_close_channel``,
    the very entry points the plugin wraps in ``_install_close_hooks``.
  * We assert the peer is NOT charged a "closed by peer" / "force-closed by peer"
    hard fault (neither in the persisted peer-reliability store nor in the
    decision log), while confirming (positive control) that the close hooks
    actually fired for both channels -- so the test fails if either the hook or
    the watchdog exemption regresses.

Heavy and slow (~5-8 min) and needs the electrum venv + docker. Function-scoped
rig per test; it wipes ``.run`` and kills any previous rig, so it must NOT run
while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_manual_close_not_faulted_e2e.py -q -s
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

# Persisted per-wallet channel-peer reliability store (see the plugin's
# PEER_RELIABILITY_DB_KEY) and decision log, both plain JSON in wallet.db.
PEER_RELIABILITY_DB_KEY = "inbound_liquidity_peer_reliability"
DECISION_LOG_DB_KEY = "inbound_liquidity_decision_log"
# The watchdog's mis-attribution reasons (both contain "closed by peer").
BLAME_SUBSTR = "closed by peer"
CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _open_channels() -> List[Dict]:
    return [c for c in _channels() if c.get("state") == "OPEN"]


def _state_of(cid: str) -> str:
    for c in _channels():
        if c["channel_id"] == cid:
            return c.get("state", "")
    return ""


def _is_closing(cid: str) -> bool:
    for c in _channels():
        if c["channel_id"] == cid:
            return c.get("state") in CLOSING_STATES or bool(c.get("closing_txid"))
    return False


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _peer_reliability() -> Dict:
    raw = _wallet_db().get(PEER_RELIABILITY_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _fault_reasons() -> List[str]:
    """All reliability-fault reasons recorded in the decision log."""
    raw = _wallet_db().get(DECISION_LOG_DB_KEY, [])
    entries = raw if isinstance(raw, list) else []
    return [str(e.get("reason") or "") for e in entries if e.get("category") == "fault"]


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
                mine_each: bool = True, period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if mine_each:
            _mine(rig, 1)
        time.sleep(period)
    return cond()


@pytest.fixture
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # opens 2 baseline channels itself; client loads the plugin
    try:
        yield r
    finally:
        r.shutdown()


def test_user_close_is_not_blamed_on_peer(rig):
    chans = _open_channels()
    assert len(chans) == 2, f"expected 2 open baseline channels, got {len(chans)}"
    partner_node = chans[0]["remote_pubkey"].lower()
    coop_cid, coop_point = chans[0]["channel_id"], chans[0]["channel_point"]
    force_cid, force_point = chans[1]["channel_id"], chans[1]["channel_point"]

    # Arm the plugin but leave it nothing to do but run the health watchdog:
    # already at max_channels (no opens), swaps off, offline auto-close off.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.peer_reliability_enabled", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")

    # Wait until the plugin has run at least one *ready* evaluation (this line is
    # logged only after the startup grace, once the watchdog has scanned) -- so
    # the two channels have been seeded as OPEN before we close them. Mining
    # drives the wallet events that trigger evaluations.
    assert _wait_until(lambda: "action(s), " in _client_log_text(),
                       rig=rig, timeout=200), \
        "plugin never completed a ready evaluation (watchdog never scanned)"

    # Pre-condition: healthy so far -- the peer has not been faulted for anything.
    assert not any(BLAME_SUBSTR in r for r in _fault_reasons()), \
        "peer was blamed for a close before we closed anything"
    assert _state_of(coop_cid) == "OPEN" and _state_of(force_cid) == "OPEN"

    # The USER closes both channels through the real CLI: one cooperative close
    # (peer is online), one force-close. Both go through the wrapped lnworker
    # entry points, so the plugin records them as local closes.
    electrum_cli("close_channel", coop_point, inst=CLIENT)            # cooperative
    electrum_cli("close_channel", force_point, "--force", inst=CLIENT)  # force

    # Both must actually transition into a closing state (so the watchdog sees a
    # real OPEN -> closing rising edge -- the edge that, unfixed, blamed the peer).
    assert _wait_until(lambda: _is_closing(coop_cid) and _is_closing(force_cid),
                       rig=rig, timeout=180), "manual closes never took effect"

    # Let several more watchdog scans run over the now-closing channels.
    for _ in range(6):
        _mine(rig, 1)
        time.sleep(2.0)

    # Positive control: the close hooks fired for BOTH channels (proves we are
    # actually exercising the wrapped path, not passing vacuously).
    log = _client_log_text()
    for cid in (coop_cid, force_cid):
        assert f"local close initiated for channel {cid[:12]}" in log, \
            f"close hook did not fire for {cid[:12]} (path not exercised)"

    # The regression assertion: our own closes were NOT charged to the peer.
    reasons = _fault_reasons()
    assert not any(BLAME_SUBSTR in r for r in reasons), \
        f"user close was blamed on the peer; fault reasons: {reasons}"
    entry = _peer_reliability().get(partner_node, {})
    assert BLAME_SUBSTR not in str(entry.get("last_reason", "")), \
        f"peer-reliability store recorded a close fault: {entry}"
