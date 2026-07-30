"""End-to-end test for the password/unlock gate, through the REAL rig:
bitcoind + Fulcrum + nostr + two headless Electrum daemons, a funded wallet and
real Lightning channels.

The reported bug: on a password-protected wallet every tick ended with

    liquidity evaluation failed: Exception('wallet requires password for channel open')

The plugin reached that raise only after a full evaluation, the error escaped
into the generic handler, and the user was told what was missing but not what to
do about it -- and nothing in the plugin ever supplied a password, so the state
was permanent.

What is asserted here, against a live daemon whose wallet really is encrypted:

  * setting a password puts the plugin into a *named* resting state ("wallet
    locked") instead of erroring -- the block is now a readiness gate, checked
    before the tick does any work;
  * no evaluation error is logged while locked (the symptom that started this);
  * unlocking through Electrum's own ``unlock`` command -- the same cache the
    GUI's Lock/Unlock button fills -- gets the plugin ticking again with no
    restart and no plugin-specific prompt.

Only the *keystore* is encrypted here (``--encrypt_file false``): that is the
kind that makes signing require a password, and it keeps ``wallet.db`` readable
for the rig's own helpers.

Not covered here: the "Run now" path, which is a GUI button with no headless
command behind it, so a rig test cannot reach it. That a manual run is refused
on a locked wallet too is pinned down at unit level instead --
``tests/test_wallet_unlock_glue.py::test_manual_run_cannot_bypass_the_lock``.

Heavy and slow (~4-6 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_wallet_lock_e2e.py -q -s
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
    mine,
    wait_wallet_height,
)

PASSWORD = "rig-test-password"
STATUS_LOCKED = "wallet locked"      # mirrors STATUS_LOCKED in the plugin


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _status_lines() -> List[str]:
    """Every tick status the live plugin published, in order (``_set_status``
    logs each transition at INFO)."""
    out: List[str] = []
    for line in _client_log_text().splitlines():
        _head, sep, tail = line.partition(" | status: ")
        if sep:
            out.append(tail.strip())
    return out


def _last_status() -> str:
    lines = _status_lines()
    return lines[-1] if lines else ""


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    """Poll ``cond``, mining as we go -- new blocks are what nudge the wallet to
    fire the events the plugin ticks on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)
        time.sleep(period)
    return cond()


def _quiet_config() -> None:
    """Let the plugin tick without moving any funds: the gate is what is under
    test, not what it would have done."""
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
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_locked_wallet_rests_on_locked_then_resumes_when_unlocked(rig):
    _quiet_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    # 1. Baseline: the plugin is armed and ticking on the unencrypted wallet.
    assert _wait_until(lambda: "sleeping" in _status_lines(), rig=rig, timeout=240), \
        f"plugin never reached the armed resting state: {_status_lines()!r}"
    log_before = len(_client_log_text())

    # 2. Encrypt the keystore on the LIVE wallet. Electrum's update_password
    #    locks the wallet on the way out, so the daemon is now holding an
    #    encrypted wallet with no password in memory -- exactly the reported
    #    condition.
    electrum_cli("password", "--new_password", PASSWORD, "--encrypt_file", "false",
                 inst=CLIENT)

    assert _wait_until(lambda: _last_status() == STATUS_LOCKED, rig=rig, timeout=180), \
        f"locked wallet did not come to rest on {STATUS_LOCKED!r}: {_status_lines()[-5:]!r}"

    # 3. And it is quiet about it: the whole point of moving this to a readiness
    #    gate is that a locked wallet is a deferral, not a per-tick error.
    new_log = _client_log_text()[log_before:]
    assert "liquidity evaluation failed" not in new_log, \
        "a locked wallet still logs an evaluation error"
    assert "requires password for channel open" not in new_log

    # 4. Unlock through Electrum's own command -- the same in-memory cache the
    #    GUI's Unlock button fills. No restart, no plugin-specific prompt.
    electrum_cli("unlock", "--password", PASSWORD, inst=CLIENT)

    assert _wait_until(lambda: _last_status() == "sleeping", rig=rig, timeout=240), \
        f"plugin did not resume after unlocking: {_status_lines()[-5:]!r}"
    # It really ran a tick rather than just relabelling: the steps are back.
    assert "reading wallet state" in _status_lines()[-12:], \
        f"no tick ran after unlocking: {_status_lines()[-12:]!r}"
