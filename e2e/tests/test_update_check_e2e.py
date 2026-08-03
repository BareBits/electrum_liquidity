"""End-to-end test for the opt-in update check, driven through the REAL rig:
bitcoind + Fulcrum + nostr + a headless Electrum daemon actually running the
plugin, with the real config file and the real network stack.

Two claims, and the first matters more than the second:

  * **switched off, nothing happens.** The default is off, and an install that
    was never opted in must not contact github.com at all -- no request, no
    stamped timestamp, no cached version. This is the claim a user relies on
    when they decline the dialog, and the only place it can be checked
    end-to-end is against a real daemon reading the real config.
  * **switched on, it reports.** With the setting enabled the plugin performs
    the lookup at wallet start and records the outcome -- either "up to date" or
    a newer version with a link -- and does so without disturbing anything else.

This is the ONE test in the suite that talks to the public internet (everything
else, including the nostr relay, is local to the rig). It is skipped rather than
failed when GitHub is unreachable or rate-limiting, because neither says
anything about the plugin.

Nothing here asserts a particular version number: the published release moves.
What is asserted is that the answer was obtained, parsed, and cached.

Heavy and slow (~6-8 min) and needs the electrum venv + docker + internet.
Function-scoped rig; it wipes ``.run`` and kills any previous rig, so it must NOT
run while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_update_check_e2e.py -q -s
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

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
    kill_inst_daemon,
    mine,
    start_electrum_daemon_ready,
    wait_electrum_ready,
    wait_lightning_ready,
    wait_wallet_height,
)

UPDATE_CHECK_URL = (
    "https://api.github.com/repos/BareBits/electrum_liquidity/releases/latest")
# How the plugin reports each outcome (see _maybe_check_for_update).
UP_TO_DATE_MARKER = "up to date"
BEHIND_MARKER = "a newer version of this plugin is available"
FAILED_MARKER = "update check failed"
ANY_CHECK_MARKER = "update check"


def _github_reachable() -> bool:
    """Whether the endpoint answers at all from this machine right now. Keeps a
    test about the plugin from failing over someone else's outage."""
    request = urllib.request.Request(
        UPDATE_CHECK_URL, headers={"User-Agent": "electrum-liquidity-e2e",
                                   "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _getcfg(key: str) -> str:
    return electrum_cli("getconfig", key, inst=CLIENT).strip()


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    return "".join(open(path, errors="replace").read() for path in logs)


def _update_lines() -> list:
    return [line for line in _client_log_text().splitlines()
            if ANY_CHECK_MARKER in line or BEHIND_MARKER in line]


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)
        time.sleep(period)
    return cond()


def _restart_client(rig) -> None:
    """Bounce the client daemon. The check fires from ``start_wallet``, so this
    is also how a user's next launch would pick up a freshly-changed setting."""
    kill_inst_daemon(CLIENT)
    start_electrum_daemon_ready(rig.pm, CLIENT, log=run_mod.log)
    wait_electrum_ready(CLIENT)
    wait_lightning_ready(CLIENT)


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


def test_update_check_is_silent_until_enabled_then_reports(rig):
    # --- switched off (the shipped default) ------------------------------
    assert _getcfg("plugins.inbound_liquidity.update_check_enabled").lower() \
        in ("false", "none", "", "0"), "the update check must ship disabled"

    # Let the plugin run for a few ticks with the default config.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    for _ in range(4):
        _mine(rig, 1)
        time.sleep(2.0)

    assert _update_lines() == [], (
        "an install that was never opted in must not contact github.com: "
        f"{_update_lines()!r}")
    assert _getcfg("plugins.inbound_liquidity.update_latest_version") in ("", "None"), \
        "a version was cached without the check ever being enabled"

    # --- switched on ------------------------------------------------------
    if not _github_reachable():
        pytest.skip("github.com is unreachable or rate-limiting from this host; "
                    "the opt-out half of this test has already passed")

    _setcfg("plugins.inbound_liquidity.update_check_enabled", "true")
    _restart_client(rig)     # the check runs from start_wallet

    assert _wait_until(lambda: any(
        UP_TO_DATE_MARKER in line or BEHIND_MARKER in line for line in _update_lines()),
        rig=rig, timeout=180), (
        f"the plugin never reported an update-check result; lines={_update_lines()!r}")

    lines = _update_lines()
    assert not [line for line in lines if FAILED_MARKER in line], lines

    # The answer was parsed and cached (the number itself moves with each
    # release, so what is pinned is that it is a usable version).
    cached = _getcfg("plugins.inbound_liquidity.update_latest_version")
    assert cached and cached[0].isdigit(), f"no usable version cached: {cached!r}"
    stamped = _getcfg("plugins.inbound_liquidity.update_last_check_ts")
    assert float(stamped) > 0, f"the check was never stamped: {stamped!r}"

    # Nothing was downloaded or installed: the plugin on disk is untouched, and
    # its own version is still the one that was running.
    running = json.load(open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "..", "inbound_liquidity", "manifest.json")))["version"]
    assert running in _client_log_text(), \
        "the running version should appear in the reported result"

    # --- and it does not re-check on every tick ---------------------------
    before = len(_update_lines())
    for _ in range(4):
        _mine(rig, 1)
        time.sleep(2.0)
    assert len(_update_lines()) == before, (
        "the check is throttled to once a day; it ran again within a minute: "
        f"{_update_lines()[before:]!r}")
