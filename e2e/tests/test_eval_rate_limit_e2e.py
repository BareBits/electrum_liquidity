"""End-to-end test that automatic mode actually takes a break, exercised through
the REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons (the
client with the inbound-liquidity plugin, plus the swap partner), a real funded
wallet and real Lightning channels.

The reported bug: armed in automatic mode with a channel over the swap trigger
but a swap the cost gate keeps declining, the plugin never rested. It opened a
nostr session, Electrum emitted ``swap_offers_changed`` for each provider
announcement it received, the plugin treated its own echo as news, and evaluated
again -- re-connecting every relay every few seconds, indefinitely, while the
wallet sat at exactly the same balances tick after tick.

This test recreates that condition on the rig -- a live nostr relay with a real
advertising provider, a channel over the trigger, and a fee ceiling low enough
that every swap is declined -- and asserts the two properties the fix must give:

  * BOUNDED -- over a sustained window the plugin evaluates a handful of times,
    not dozens. Counted from the live daemon's own log, so it measures the real
    thing the user saw (sessions opened against real relays), not a mock.
  * STILL ALIVE -- automation is not simply switched off: a manual "Run now"
    evaluates immediately, and the resting status names when the next automatic
    check is due rather than claiming to be idle.

A unit-level companion (``tests/test_eval_rate_limit_glue.py``) pins the
mechanics -- defer-not-drop, the material-change bypass, the backoff curve. This
one exists because the storm was only ever visible against real relays: it is
the end-to-end evidence that the loop is gone.

Heavy and slow (~6-8 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_eval_rate_limit_e2e.py -q -s
"""
from __future__ import annotations

import glob
import os
import sys
import time
from typing import Callable, List

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
    fund_address,
    mine,
    wait_wallet_height,
)

# How long to watch the armed, idle plugin, and the most evaluations that window
# may contain. Sized against the shipped floor (MIN_AUTO_EVAL_INTERVAL_SEC = 60s)
# with generous headroom: the pre-fix plugin managed a full evaluation every 3-6
# seconds, i.e. 50-100 in this window, so the two regimes are an order of
# magnitude apart and the threshold is not sensitive to rig timing.
OBSERVE_SEC = 300.0
MAX_EVALUATIONS = 8


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _decision_lines() -> List[str]:
    """One line per completed evaluation.

    ``_run_decision`` logs exactly one "-> N action(s), M decline(s)" summary per
    tick, which makes it the cheapest honest tick counter available from outside
    the process.
    """
    return [line for line in _client_log_text().splitlines()
            if "action(s)," in line and "decline(s)" in line]


def _session_lines() -> List[str]:
    """One line per nostr swap session opened -- the thing that hammered the
    relays. Counted separately from evaluations because the session is the
    expensive half, and a fix that merely stopped *logging* the storm while still
    opening sessions would pass a tick count and fail this."""
    return [line for line in _client_log_text().splitlines()
            if "starting nostr transport with pubkey" in line]


def _status_lines() -> List[str]:
    out: List[str] = []
    for line in _client_log_text().splitlines():
        _head, sep, tail = line.partition(" | status: ")
        if sep:
            out.append(tail.strip())
    return out


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


def _storm_config() -> None:
    """Exactly the reported condition: armed, a channel over the swap trigger, and
    a fee ceiling low enough that the all-in cost gate declines every swap.

    That combination is what made the loop self-sustaining -- ``_swap_may_be_needed``
    stays true forever because the decline never changes the balances that made it
    true -- so every tick opened another nostr session.
    """
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.manual_run_only", "false")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "1")     # any channel qualifies
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "1")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "0.01")  # ...but nothing is cheap enough
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")


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


def test_armed_and_idle_the_plugin_does_not_storm(rig):
    """The regression test for the bug: a steady state must cost few ticks."""
    _storm_config()

    # Wait for the plugin to get past the startup grace and actually tick, so the
    # window below measures the steady state rather than the warm-up.
    assert _wait_until(lambda: len(_decision_lines()) >= 1, rig=rig, timeout=300), \
        "the plugin never completed an evaluation; nothing to measure"

    before_ticks = len(_decision_lines())
    before_sessions = len(_session_lines())
    deadline = time.monotonic() + OBSERVE_SEC
    while time.monotonic() < deadline:
        _mine(rig, 1)          # keep the chain (and the wallet's events) moving
        time.sleep(5)
    ticks = len(_decision_lines()) - before_ticks
    sessions = len(_session_lines()) - before_sessions

    assert ticks <= MAX_EVALUATIONS, (
        f"{ticks} evaluations in {OBSERVE_SEC:.0f}s -- the plugin is not resting "
        f"between ticks (pre-fix this was 50-100)")
    assert sessions <= MAX_EVALUATIONS, (
        f"{sessions} nostr sessions opened in {OBSERVE_SEC:.0f}s -- the relay "
        f"churn is back even if the tick count looks reasonable")


def test_resting_status_says_when_the_next_check_is_due(rig):
    """A plugin that is deliberately waiting must say so, not look wedged."""
    _storm_config()
    assert _wait_until(lambda: any(s.startswith("sleeping") for s in _status_lines()),
                       rig=rig, timeout=300), \
        f"plugin never reached the armed resting state: {_status_lines()[-5:]!r}"
    resting = [s for s in _status_lines() if s.startswith("sleeping")]
    assert any("next check in" in s for s in resting), \
        f"resting status does not name the next check: {resting[-5:]!r}"


def test_money_arriving_gets_an_immediate_tick_despite_the_backoff(rig):
    """The property the rate limit must never cost: responsiveness to funds.

    Rate-limited is not switched off. Once the plugin has settled into its idle
    backoff, real money landing in the wallet must produce an evaluation at once
    -- not one backoff later -- because that is precisely when there may be work
    to do. Exercised with an on-chain receive: it is the change this rig can
    create on demand, and it travels the same "material state changed" path as a
    received Lightning payment (both move a balance the signature covers).

    ("Run now" gets the same exemption, but it is a Qt button with no CLI entry
    point, so its exemption is pinned in the glue tests instead.)
    """
    _storm_config()
    assert _wait_until(lambda: len(_decision_lines()) >= 1, rig=rig, timeout=300), \
        "the plugin never completed an evaluation"

    # Let it settle into the backoff: from here an unchanged wallet would wait
    # minutes, so a tick right after the receive can only be the bypass.
    time.sleep(90)
    before = len(_decision_lines())
    addr = electrum_cli("getunusedaddress", inst=CLIENT).strip().strip('"')
    fund_address(rig.ep, addr, 0.05)
    _mine(rig, 1)

    assert _wait_until(lambda: len(_decision_lines()) > before, rig=rig, timeout=60), \
        ("funds arrived but the plugin did not evaluate; the rate limit must "
         "never delay a real change in the wallet")
