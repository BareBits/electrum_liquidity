"""End-to-end tests for the daily channel action ceilings, exercised through the
REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons (the client
with the inbound-liquidity plugin loaded, plus the swap partner), a real funded
wallet and real Lightning channels.

Two ceilings, both asserted against the *live* plugin (driven only by its own
event loop) and the real ``wallet.db``:

  * OPEN ceiling  -- the plugin opens exactly ``max_opens_per_day`` channels then
    refuses further opens (writing a "daily open ceiling reached" decline), and
    resumes when the ceiling is raised.
  * CLOSE ceiling -- the watchdog force-closes wedged channel opens, but only up
    to ``max_closes_per_day`` per rolling 24h: given two wedged opens and a cap of
    1, exactly one is force-closed and the other is deferred until the cap is
    raised. A wedged open is induced by opening channels to the partner and then
    killing the partner before the funding matures, so ``channel_ready`` never
    arrives and the channels stay stuck opening.

Heavy and slow (~5-8 min total) and needs the electrum venv + docker. Each test
gets its OWN rig bring-up (function-scoped fixture) for isolation; bring-up reuses
the rig's single-instance state (``.run``, the process marker, the fixed docker
container), so it wipes ``.run`` and kills any previous rig -- it must NOT run
while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1`` so the default
fast suite never launches services.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_daily_caps_e2e.py -q -s
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

# Repo root on the path so `run` and `rig` import (mirrors test_rig_unit.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    PARTNER,
    electrum_cli,
    mine,
    stop_daemon,
    wait_wallet_height,
    wallet_path,
)

# Cap each plugin-initiated open at 0.01 BTC so on-chain funds are NOT the
# limiter after the first open -- the daily ceiling must be the only thing that
# can block a further open, or the open test would be ambiguous.
FUND_CAP_SAT = 1_000_000
# Small manual channels used to induce wedged opens in the close test.
WEDGE_FUND_BTC = 0.003
# The plugin's stuck-open timeout clamps to a 1-minute floor; a wedged open is
# only remediated once it is older than that. Wait comfortably past it.
STUCK_MIN = 1
WEDGE_AGE_WAIT = 75.0
# Closing/closed channel states as reported by `list_channels`.
CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}


# --- helpers driving the live client daemon over the CLI -----------------
def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


def _live_channels() -> int:
    """Channels that are OPEN or still opening (i.e. not closing/closed) -- so a
    pending open is counted as 'a channel exists', never as room for another."""
    return sum(1 for c in _channels() if c.get("state") not in CLOSING_STATES)


def _closing_among(ids: Set[str]) -> int:
    """How many of ``ids`` are force-closing / closed."""
    return sum(1 for c in _channels()
               if c["channel_id"] in ids
               and (c.get("state") in CLOSING_STATES or c.get("closing_txid")))


def _decision_log() -> List[Dict]:
    """The plugin's decision log, read straight from the real wallet.db file
    (plain JSON; the plugin persists it after every append)."""
    with open(wallet_path(CLIENT)) as fh:
        data = json.load(fh)
    raw = data.get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _log_entries(category: str, kind: str = None) -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == category and (kind is None or e.get("kind") == kind)]


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float, mine_each: bool = True) -> bool:
    """Poll ``cond`` while mining a block each tick (mining confirms funding txs
    and drives the plugin's trigger events). Returns whether it became true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if mine_each:
            _mine(rig, 1)
        time.sleep(1.5)
    return cond()


@pytest.fixture
def rig():
    """A fresh headless rig per test (function-scoped for isolation)."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # opens 2 channels itself; the client daemon loads the plugin
    try:
        yield r
    finally:
        r.shutdown()


def test_open_ceiling_enforced_end_to_end(rig):
    # The rig opened 2 channels itself (not via the plugin), so the plugin's own
    # rolling-24h open count starts at 0.
    assert _live_channels() == 2
    base = 2

    # Configure the client so the ONLY thing that can stop a further open is the
    # daily ceiling: give channel headroom, cap each open small (funds remain),
    # and disable reverse swaps (the rig channels sit far over the swap triggers,
    # which would otherwise have the plugin busy swapping during the test).
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "6")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")

    # The plugin should open exactly ONE channel (opens_last_24h 0 -> 1)...
    assert _wait_until(lambda: _live_channels() >= base + 1, rig=rig, timeout=120), \
        "plugin never opened its first channel"
    # ...and let it reach OPEN so the next evaluation is not frozen on a pending
    # open but actually reaches the open decision (and hits the ceiling).
    assert _wait_until(
        lambda: _live_channels() == base + 1
        and all(c["state"] == "OPEN" for c in _channels()),
        rig=rig, timeout=120), "first plugin open did not confirm to OPEN"

    # Drive several more evaluation ticks: with the ceiling at 1 and one plugin
    # open already recorded, no further channel may be opened.
    for _ in range(8):
        _mine(rig, 1)
        time.sleep(1.5)
    assert _live_channels() == base + 1, "plugin opened past the daily ceiling"

    # The refusal is recorded in the real wallet.db decision log, alongside the
    # one open it did make.
    assert _log_entries("action", "open"), "expected an 'open' action in the log"
    assert any("daily open ceiling" in (e.get("reason") or "")
               for e in _log_entries("decline")), \
        "expected a 'daily open ceiling' decline in the log"

    # Release the ceiling -- same funds, same headroom -- and the plugin opens
    # again, proving the ceiling (nothing else) was the gate.
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "5")
    assert _wait_until(lambda: _live_channels() >= base + 2, rig=rig, timeout=120), \
        "raising the ceiling did not let the plugin open again"


def test_close_ceiling_enforced_end_to_end(rig):
    # Keep automation on (the watchdog only runs while automation is enabled) but
    # give the plugin nothing to do except remediate wedged opens: no room to
    # open (max_channels == current count), and swaps disabled.
    conn = electrum_cli("nodeid", inst=PARTNER)  # capture while partner is up
    originals = _channel_ids()
    assert len(originals) == 2
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.stuck_open_timeout_min", str(STUCK_MIN))
    _setcfg("plugins.inbound_liquidity.auto_remediate_stuck_open", "true")
    _setcfg("plugins.inbound_liquidity.max_closes_per_day", "1")

    # Induce two wedged opens. Open W1, mine one block (advances the height so
    # W2's funding key differs, but W1 is still below the 3-conf min-depth so it
    # cannot open), open W2, then kill the partner so neither ever receives
    # channel_ready; mining their fundings to depth leaves both stuck opening.
    electrum_cli("add_peer", conn, inst=CLIENT)
    electrum_cli("open_channel", conn, f"{WEDGE_FUND_BTC:.8f}", inst=CLIENT)
    t0 = time.monotonic()
    _mine(rig, 1)
    w1 = _channel_ids() - originals
    assert len(w1) == 1, f"expected one new channel after W1 open, got {w1}"
    electrum_cli("open_channel", conn, f"{WEDGE_FUND_BTC:.8f}", inst=CLIENT)
    wedged = _channel_ids() - originals
    assert len(wedged) == 2, f"expected two wedged channels, got {wedged}"

    stop_daemon(PARTNER)          # partner gone: channel_ready will never arrive
    _mine(rig, 3)                 # mature both fundings; they stay stuck FUNDED
    assert all(c["state"] not in ("OPEN",) for c in _channels()
               if c["channel_id"] in wedged), "a wedged channel unexpectedly opened"

    # Let both wedged opens age past the stuck-open timeout, nudging the watchdog
    # with a block each tick (the partner is dead, so they cannot un-wedge).
    while time.monotonic() - t0 < WEDGE_AGE_WAIT:
        _mine(rig, 1)
        time.sleep(3.0)

    # With the close ceiling at 1, the watchdog force-closes exactly ONE of the
    # two wedged opens and defers the other.
    assert _wait_until(lambda: _closing_among(wedged) >= 1, rig=rig, timeout=60), \
        "watchdog never force-closed a wedged open"
    # Hold: several more ticks must NOT close the second one (the cap blocks it).
    for _ in range(6):
        _mine(rig, 1)
        time.sleep(2.0)
    assert _closing_among(wedged) == 1, "close ceiling did not defer the second force-close"
    assert len(_log_entries("action", "close")) == 1, \
        "expected exactly one 'close' action logged under the cap"

    # Raise the ceiling -- the deferred wedged open is now force-closed too.
    _setcfg("plugins.inbound_liquidity.max_closes_per_day", "5")
    assert _wait_until(lambda: _closing_among(wedged) >= 2, rig=rig, timeout=90), \
        "raising the ceiling did not release the deferred force-close"
    assert len(_log_entries("action", "close")) >= 2, \
        "expected a second 'close' action after raising the ceiling"
