"""End-to-end test that peer-reliability bookkeeping survives being written more
than once, driven through the REAL rig: bitcoind + Fulcrum + nostr + two headless
Electrum daemons, a real funded wallet and a real on-disk wallet file.

The regression, as reported from a live run. Electrum's ``JsonDB`` does not hand
back what it was given: ``put`` stores into a ``StoredDict`` that re-wraps nested
dicts as more ``StoredDict``s, each carrying ``_db`` -> the ``JsonDB`` -> a
``threading.RLock``. The plugin read its peer-reliability store back with a
shallow ``dict(raw)``, so the per-peer entries it held were live db objects, and
writing them back made ``put``'s ``copy.deepcopy`` walk into that lock:

    TypeError: cannot pickle '_thread.RLock' object

raised out of ``_record_peer_fault`` -- inside ``_open_channel``'s candidate
loop. The result was not a lost statistic: the exception aborted the loop and
the whole evaluation, so a run with 10 partners tried 2 and stopped, twice in a
row, and the wallet never got the channel it had the funds for.

Why the rig and not a mock: every unit-level fake db in ``../tests`` hands back
the dict it was handed, which is exactly the property the real one lacks -- 593
green unit tests sat on top of this bug. Here the plugin runs inside a real
Electrum daemon and writes to a real wallet file, and the assertions read that
file from disk.

The scenario, shaped to hit the write that failed:

  * two well-formed but unreachable peers are configured as the ONLY channel
    partners (``partners_strict``), so a tick that wants a channel walks both
    and faults both -- two distinct writes to the same store in one tick;
  * the second write is the one that used to raise, because it adds a *new key*
    (the first fault of a fresh store always passed, which is why this looked
    intermittent). Unfixed, the second peer never reaches the wallet file at all;
  * then the client daemon is restarted and the plugin faults again, against a
    store that is now db-backed from its very first read -- the shape that made
    the *first* fault of the reported run's second attempt crash.

Heavy and slow (~8-10 min: rig bring-up, plus ~20s per unreachable candidate per
tick from Electrum's ``LN_P2P_NETWORK_TIMEOUT``) and needs the electrum venv +
docker. Function-scoped rig; it wipes ``.run`` and kills any previous rig, so it
must NOT run while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_peer_fault_persistence_e2e.py -q -s
"""
from __future__ import annotations

import glob
import json
import os
import socket
import sys
import time
from typing import Callable, Dict, List, Tuple

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import electrum_ecc as ecc  # noqa: E402

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

PEER_RELIABILITY_DB_KEY = "inbound_liquidity_peer_reliability"
DECISION_LOG_DB_KEY = "inbound_liquidity_decision_log"
FUND_CAP_SAT = 1_000_000

# The crash itself, plus the two ways the plugin now reports a write it could not
# make and the two ways an escaped exception showed up in the log. None of these
# may appear.
CRASH_MARKER = "cannot pickle"
PERSIST_FAILED_MARKER = "could not persist peer reliability"
EXECUTOR_ABORT_MARKERS = ("Exception in _execute", "liquidity evaluation failed")


def _unreachable_partners() -> List[Tuple[str, str]]:
    """Two (node_id, connect_str) pairs: valid secp256k1 pubkeys at ports nobody
    is listening on. Valid keys on purpose -- a malformed one would fail in
    parsing, before the connect/open attempt that produces the fault we are
    after."""
    out = []
    for seed in (0x11, 0x22):
        node_id = ecc.ECPrivkey(bytes([seed]) * 32).get_public_key_hex(compressed=True)
        with socket.socket() as s:          # a port that was free a moment ago
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        out.append((node_id.lower(), f"{node_id}@127.0.0.1:{port}"))
    return out


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _peer_reliability() -> Dict:
    raw = _wallet_db().get(PEER_RELIABILITY_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _fault_count(node_id: str) -> int:
    stats = _peer_reliability().get(node_id) or {}
    return int(stats.get("fault_count", 0) or 0)


def _fault_log_idents() -> List[str]:
    raw = _wallet_db().get(DECISION_LOG_DB_KEY, [])
    entries = raw if isinstance(raw, list) else []
    return [str(e.get("source") or "") for e in entries if e.get("category") == "fault"]


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    return "".join(open(path, errors="replace").read() for path in logs)


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)                      # wallet events drive evaluations
        time.sleep(period)
    return cond()


def _arm_open_against(partners: List[str]) -> None:
    """Make the engine want a channel and give it only unreachable partners.

    ``partners_strict`` keeps Electrum's own suggestions out of the try-order, so
    the walk is exactly these two and the test cannot accidentally succeed at
    opening a real channel. Auto-ban is pushed out of reach so repeated ticks
    keep faulting the same two peers instead of removing them from the list."""
    _setcfg("plugins.inbound_liquidity.preferred_partners", ",".join(partners))
    _setcfg("plugins.inbound_liquidity.partners_strict", "true")
    _setcfg("plugins.inbound_liquidity.peer_reliability_enabled", "true")
    _setcfg("plugins.inbound_liquidity.peer_autoban_faults", "999")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "99")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")


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


def test_repeated_peer_faults_persist_and_do_not_abort_the_candidate_walk(rig):
    partners = _unreachable_partners()
    node_a, node_b = partners[0][0], partners[1][0]
    _arm_open_against([cs for _nid, cs in partners])

    # Phase 1 -- one tick, two candidates, two writes to the same store.
    # BOTH peers must reach the wallet file. Unfixed, the second write raises and
    # takes the evaluation with it, so `node_b` never appears however long we wait.
    assert _wait_until(lambda: node_a in _peer_reliability()
                       and node_b in _peer_reliability(),
                       rig=rig, timeout=420), (
        "both unreachable partners should have been faulted and persisted; "
        f"store={_peer_reliability()!r}")

    # The walk really did move on from the first candidate (so the assertion
    # above is not passing on two ticks that each stopped at candidate 1).
    log = _client_log_text()
    assert "(2 of 2)" in log, "the candidate walk never reached the second partner"

    # Nothing escaped: no crash, no swallowed-but-logged persistence failure, and
    # no aborted evaluation.
    assert CRASH_MARKER not in log, log[-4000:]
    assert PERSIST_FAILED_MARKER not in log, log[-4000:]
    for marker in EXECUTOR_ABORT_MARKERS:
        assert marker not in log, f"{marker!r} in client log:\n{log[-4000:]}"

    # Both faults are in the user-visible decision log too, not just the store.
    idents = " ".join(_fault_log_idents())
    assert node_a[:6] in idents and node_b[:6] in idents, idents

    before = {node_a: _fault_count(node_a), node_b: _fault_count(node_b)}

    # Phase 2 -- restart the client. The store is now on disk, so the plugin's
    # very first read after startup is db-backed and its very first fault write
    # is the shape that crashed in the reported run's second attempt.
    kill_inst_daemon(CLIENT)
    start_electrum_daemon_ready(rig.pm, CLIENT, log=run_mod.log)
    wait_electrum_ready(CLIENT)
    wait_lightning_ready(CLIENT)

    assert _wait_until(
        lambda: all(_fault_count(n) > before[n] for n in (node_a, node_b)),
        rig=rig, timeout=420), (
        f"fault counts did not increase after a restart: before={before!r} "
        f"after={ {n: _fault_count(n) for n in (node_a, node_b)} !r}")

    log = _client_log_text()
    assert CRASH_MARKER not in log, log[-4000:]
    assert PERSIST_FAILED_MARKER not in log, log[-4000:]
    for marker in EXECUTOR_ABORT_MARKERS:
        assert marker not in log, f"{marker!r} in client log:\n{log[-4000:]}"
