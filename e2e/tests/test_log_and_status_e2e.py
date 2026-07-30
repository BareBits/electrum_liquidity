"""End-to-end tests for the two diagnostics surfaces, exercised through the REAL
rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons (the client with
the inbound-liquidity plugin loaded), a funded wallet and real Lightning channels.

Both features exist to answer the same complaint -- "the plugin says it has funds
and room to open but no reachable channel partner, and I cannot see why" -- so
what matters is that they carry real information out of a *live* plugin, not that
the widgets exist (the offscreen-Qt tests in ``tests/test_qt_tab.py`` cover the
widgets, and ``tests/test_log_buffer.py`` covers the ring itself).

  * TICK STATUS -- every step transition is published to the GUI *and* logged, so
    a live daemon's log has to show the tick walking its steps and coming to rest
    on a terminal state. A status that never returns to "sleeping" would leave the
    Settings tab wedged on a step that finished minutes ago.
  * PARTNER BREAKDOWN -- a real "no reachable channel partner" decline, written by
    the live plugin into the real ``wallet.db``, has to carry the arithmetic that
    explains it (how many preferred, how many suggested, what dropped them, and
    which routing mode Electrum is in).

The second test deliberately reproduces the reported condition -- no preferred
partner, nothing for ``suggest_peer()`` to return -- and asserts the plugin now
explains it instead of dead-ending.

Heavy and slow (~4-6 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_log_and_status_e2e.py -q -s
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

# Terminal statuses the plugin comes to rest on (mirrors TERMINAL_STATUSES in the
# plugin; duplicated rather than imported because the plugin package is only
# importable inside the client daemon's environment).
TERMINAL = ("sleeping", "automation disabled", "idle (manual run only)",
            "waiting for wallet to settle", "not started")


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _open_declines() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "decline" and e.get("kind") == "open"]


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _status_lines() -> List[str]:
    """Every tick status the live plugin published, in order.

    ``_set_status`` logs each transition at INFO, which is exactly what makes the
    Log tab a usable tick trace -- so reading them back out of the daemon's own
    log file is a faithful end-to-end check of the same mechanism.
    """
    out: List[str] = []
    for line in _client_log_text().splitlines():
        # Electrum's file formatter is "asctime | LEVEL | logger | message".
        _head, sep, tail = line.partition(" | status: ")
        if sep:
            out.append(tail.strip())
    return out


def _last_status() -> str:
    """The most recent status, or "" before any tick has run (so a wait
    condition can be evaluated before the plugin has said anything)."""
    lines = _status_lines()
    return lines[-1] if lines else ""


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


def _quiet_config() -> None:
    """Let the plugin tick without moving any funds, so the diagnostics are the
    only thing under test: no swaps, no auto-close, no opens beyond one."""
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")


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


def test_tick_status_walks_the_steps_and_comes_to_rest(rig):
    """A live tick publishes its steps and ends on a terminal state."""
    _quiet_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    # The plugin defers everything during its startup grace, so the first
    # statuses are "waiting for wallet to settle"; wait for a real tick.
    assert _wait_until(lambda: "reading wallet state" in _status_lines(),
                       rig=rig, timeout=240), \
        f"no tick ever ran; statuses seen: {_status_lines()!r}"

    seen = _status_lines()
    # The watchdog/snapshot steps run in a fixed order every tick.
    for step in ("reconciling pending swaps", "checking channel health",
                 "checking for offline peers", "reading wallet state"):
        assert step in seen, f"missing step {step!r} in {seen!r}"
    order = [seen.index(s) for s in ("reconciling pending swaps",
                                     "checking channel health",
                                     "checking for offline peers",
                                     "reading wallet state")]
    assert order == sorted(order), f"steps out of order: {seen!r}"

    # And it comes to rest -- the property that keeps the Settings tab honest.
    assert _wait_until(lambda: _last_status() in TERMINAL, rig=rig, timeout=120), \
        f"tick never reached a terminal status; last was {_last_status()!r}"
    assert "sleeping" in _status_lines()


def test_disabling_automation_rests_on_disabled_not_sleeping(rig):
    """A switched-off plugin must not claim it is merely sleeping."""
    _quiet_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    assert _wait_until(lambda: "sleeping" in _status_lines(), rig=rig, timeout=240), \
        f"plugin never reached the armed resting state: {_status_lines()!r}"

    _setcfg("plugins.inbound_liquidity.automation_enabled", "false")
    assert _wait_until(lambda: _last_status() == "automation disabled",
                       rig=rig, timeout=180), \
        f"status did not follow the master switch: {_status_lines()[-5:]!r}"


def test_no_partner_decline_carries_the_breakdown(rig):
    """The reported dead end, now with the arithmetic that explains it.

    With no preferred partner configured and no trampoline node for regtest's
    ``suggest_peer()`` to return, the plugin correctly declines the open. What is
    new is that the decline the user reads carries the counts: how many partners
    were configured, how many Electrum suggested, what dropped them, and which
    routing mode Electrum is in.
    """
    _quiet_config()
    # No configured partner at all: the only candidate source is suggest_peer(),
    # which regtest cannot satisfy. Guard off so it is not the blocking reason.
    _setcfg("plugins.inbound_liquidity.preferred_partners", "")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", "1000000")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: any("have funds and room to open" in str(e.get("reason"))
                                   for e in _open_declines()),
                       rig=rig, timeout=240), \
        f"expected an open decline; decision log: {_decision_log()!r}"

    decline = next(e for e in _open_declines()
                   if "have funds and room to open" in str(e.get("reason")))
    detail = str(decline.get("detail") or "")
    assert detail.startswith("partner resolution: "), \
        f"the decline carries no breakdown: {decline!r}"
    # The counts that turn the dead end into a diagnosis.
    assert "preferred 0/0" in detail, detail
    assert "suggested" in detail, detail
    assert "routing " in detail, detail

    # The same arithmetic is logged, so it reaches the Log tab (and the diagnostic
    # files) as well as the decision-log row.
    assert "channel partners: 0 candidate(s)" in _client_log_text()


def test_breakdown_names_the_one_channel_per_peer_guard(rig):
    """The other common cause of an empty candidate list, told apart from it.

    The rig's baseline channels already go to the partner, and the partner is the
    only peer configured -- so with the guard ON there is nothing left to open to.
    That is a completely different situation from "the network has nobody", and
    the breakdown has to say which one you are in.
    """
    _quiet_config()
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.partners_strict", "true")   # preferred only
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", "1000000")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: any("one-channel-per-peer" in str(e.get("reason"))
                                   for e in _open_declines()),
                       rig=rig, timeout=240), \
        f"expected the guard decline; got {[e.get('reason') for e in _open_declines()]!r}"

    decline = next(e for e in _open_declines()
                   if "one-channel-per-peer" in str(e.get("reason")))
    detail = str(decline.get("detail") or "")
    assert "already have a channel with" in detail, detail
    assert "strict mode" in detail, detail
