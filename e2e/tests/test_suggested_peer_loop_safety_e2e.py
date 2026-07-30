"""End-to-end test that the suggested-peer lookup is safe to make from the
plugin's evaluation coroutine when Electrum is in GOSSIP mode -- against a real
Electrum daemon, not a mock.

The bug: ``_resolve_channel_partners`` called ``wallet.lnworker.suggest_peer()``
inline. That runs on Electrum's asyncio loop (the plugin's ``_evaluate`` is
dispatched there with ``run_coroutine_threadsafe``), and in gossip mode
``suggest_peer()`` goes

    LNWallet.suggest_peer -> LNRater.suggest_peer -> LNRater.maybe_analyze_graph
                          -> Network.run_from_another_thread

whose first statement is ``assert util.get_running_loop() != loop, 'must not be
called from asyncio thread'`` -- and which would deadlock waiting on its own loop
even without that assert. The AssertionError was then swallowed by the bare
``except Exception`` beside it, so the rater was never consulted at all and every
channel open was declined for "no reachable channel partner".

The fix awaits the lookup in a worker thread. This test proves it against reality
by looking for the one thing only a *successful* off-loop call can produce:
``LNRater``'s own graph-analysis log line. Its analysis is unreachable if the call
never gets past ``run_from_another_thread``, so its presence is direct evidence the
plugin reached Electrum's rater from a decision tick.

A regtest graph is empty, so the rater legitimately has nobody to suggest; the
plugin must then decline cleanly ("no preferred/suggested peer") with no lookup
error recorded. Both halves are asserted: the rater ran, and nothing broke.

Heavy and slow (~4-6 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_suggested_peer_loop_safety_e2e.py -q -s
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
    kill_inst_daemon,
    mine,
    start_electrum_daemon_ready,
    wait_electrum_ready,
    wait_lightning_ready,
    wait_wallet_height,
    wallet_path,
)

FUND_CAP_SAT = 1_000_000

# LNRater's own log line, emitted at the end of _collect_purged_stats -- i.e. only
# after maybe_analyze_graph() actually ran. Unreachable from the asyncio thread.
RATER_ANALYSED_MARKER = "node statistics done"
# Network.run_from_another_thread's assertion message.
ON_LOOP_ASSERT_MARKER = "must not be called from asyncio thread"


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


def _declines() -> List[str]:
    return [str(e.get("reason") or "") for e in _decision_log()
            if e.get("category") == "decline"]


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _diag_log_text() -> str:
    return " ".join(
        open(path, errors="replace").read()
        for path in glob.glob(str(paths.CLIENT_DATADIR / "regtest"
                                  / "inbound_liquidity_logs" / "*.log")))


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


def _arm_open_config() -> None:
    """Make the engine want to open a channel, with no preferred partner, so the
    suggestion lookup is the only thing standing between it and a candidate."""
    _setcfg("plugins.inbound_liquidity.preferred_partners", "")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.diag_log_enabled", "true")


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


def _switch_client_to_gossip_mode(rig) -> None:
    """Restart the client daemon with ``use_gossip=true``.

    The rig configures ``use_gossip=false`` (trampoline mode) because that is how
    two Electrum nodes channel directly. Gossip mode is chosen at network startup
    -- ``LNWallet.channel_db`` is what ``uses_trampoline()`` keys off -- so the
    daemon has to come back up for the rater path to exist at all.
    """
    _setcfg("use_gossip", "true")
    kill_inst_daemon(CLIENT)
    start_electrum_daemon_ready(rig.pm, CLIENT, log=run_mod.log)
    wait_electrum_ready(CLIENT)
    wait_lightning_ready(CLIENT)


def test_gossip_mode_suggestion_runs_off_the_loop(rig):
    assert len(_channels()) == 2, "expected the 2 baseline channels"

    _switch_client_to_gossip_mode(rig)
    # Sanity: the wallet really is in gossip mode now, so suggest_peer() takes the
    # LNRater path (the one that asserts against the asyncio thread) and not the
    # trampoline shortcut.
    assert electrum_cli("getconfig", "use_gossip", inst=CLIENT).strip().lower() \
        in ("true", "1"), "client did not come back up in gossip mode"

    _arm_open_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    # Drive ticks until the plugin has evaluated an open at least once (regtest has
    # no graph, so the honest outcome is a decline -- what matters is HOW).
    assert _wait_until(lambda: any("have funds and room to open" in r
                                   for r in _declines()), rig=rig, timeout=210), \
        f"plugin never evaluated an open; declines={_declines()!r}"

    log_text = _client_log_text()

    # The proof: LNRater actually analysed the graph. That code is reachable only
    # through Network.run_from_another_thread, i.e. only from OFF the asyncio loop.
    assert RATER_ANALYSED_MARKER in log_text, (
        "LNRater never ran, so the suggested-peer lookup never got past "
        "Network.run_from_another_thread -- the on-loop call is back"
    )
    # And the assertion that used to fire (and be swallowed) never did.
    assert ON_LOOP_ASSERT_MARKER not in log_text, (
        f"found {ON_LOOP_ASSERT_MARKER!r} in the Electrum log: the lookup ran on "
        "the asyncio loop"
    )

    # Nothing was swallowed and nothing broke: an empty regtest graph is a clean
    # "no suggestion", not a failure.
    assert "suggested-peer lookup failed" not in log_text, log_text[-4000:]
    assert "suggested-peer lookup failed" not in _diag_log_text()
    joined = " | ".join(_declines())
    assert "no preferred/suggested peer" in joined, joined
    assert "asking Electrum for a suggested peer failed" not in joined, joined
