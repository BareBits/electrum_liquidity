"""End-to-end tests for how channel-PEER faults age out and how the peer store is
pruned, driven through the REAL rig: bitcoind + Fulcrum + nostr + two headless
Electrum daemons, a real funded wallet, and every assertion read back from the
real on-disk wallet file.

The peer counterpart of ``test_fault_aging_and_pruning_e2e``. Peer reliability
shares the provider tuning (``_peer_reliability_params`` takes base/cap/half-life
straight from ``_reliability_params``) and had the same defect: the penalty's
*magnitude* decayed at one half-life but the escalation *counter* never did, so a
peer forgiven all the way down to a zero penalty still resumed at
``base * 2^(old faults)`` the moment it faulted once more.

Two things separate the peer case from the provider case, and both are asserted
here because both are decisions rather than mechanics:

  * ``hard_fault_count`` -- the auto-ban tally -- deliberately does NOT age. It
    answers "how many times has this peer force-closed on us", which quiet time
    should not heal, and ageing it would need a window far longer than the 6h
    penalty half-life to leave auto-ban reachable at all (3 hard faults inside
    18h). Only the ranking counter heals.
  * For the same reason the prune never drops a row carrying that tally, however
    idle. Dropping it would silently pardon a peer sitting one force-close short
    of the threshold -- undoing a protection the operator is relying on, quietly.
    That is the assertion worth the most in this file.

Why the rig and not mocks: these are read-modify-write cycles against Electrum's
``JsonDB``, which does NOT hand back what it was given (it re-wraps nested dicts
as ``StoredDict``s carrying a ``threading.RLock``). Every fake db in
``../../tests`` returns the dict it was handed, which is exactly the property the
real one lacks -- the blind spot that once let a ``TypeError: cannot pickle
'_thread.RLock' object`` sit under a green unit suite (see
``test_peer_fault_persistence_e2e``, the regression test for that).

How the staleness is staged: the client daemon is stopped and the stale rows are
written straight into its wallet file before it is restarted. That is not a
shortcut around the code under test -- it is precisely the state a wallet that sat
unused for months comes back up in, and the prune then runs for real on the next
tick. Waiting 90 days is not available and the bound is a module constant, so
there is no knob to shrink instead.

The real fault that follows comes from a genuine failed channel open: a
well-formed but unreachable peer is configured as the ONLY channel partner
(``partners_strict``), so a tick that wants a channel tries it and fails. Note
which arm that is -- ``lnpeermgr.add_peer`` does NOT raise for a dead port
(Electrum registers the peer and the handshake fails later), so the attempt gets
past the connect stage and dies at the open, recording ``channel open failed`` as
a HARD fault. ``peer_autoban_faults`` is therefore load-bearing here, not
decorative: left at its default of 3 the staged tally plus this fault would ban
the peer and remove it from the try-order mid-test.

That the fault is hard is what lets one row prove both halves of the design at
once. The staged history is aged 24h -- four of the shipped 6h half-lives -- so
the ranking counter beside it must be forgiven all the way down and land at level
1 rather than level 4, while the hard tally on that same row, over that same 24h,
must come out with nothing forgiven.

Do NOT try to shorten the half-life from the test instead: ``setconfig`` cannot
store a fractional float at all. It parses "0.25" into a ``Decimal`` that
SimpleConfig then refuses to save, logging only an INFO-level "json error: cannot
save" line and leaving the default in force -- so the test would silently measure
the default. (Learned the hard way in the provider version of this file.)

Heavy and slow (~8-12 min: rig bring-up, plus ~20s per unreachable candidate per
tick from Electrum's ``LN_P2P_NETWORK_TIMEOUT``); needs the electrum venv +
docker. Function-scoped rig: it wipes ``.run`` and kills any previous rig, so it
must NOT run while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_peer_fault_aging_and_pruning_e2e.py -q -s
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
    stop_daemon,
    wait_electrum_ready,
    wait_lightning_ready,
    wait_wallet_height,
    wallet_path,
)

PEER_RELIABILITY_DB_KEY = "inbound_liquidity_peer_reliability"

# Matches RELIABILITY_ROW_MAX_IDLE_SEC in the plugin. Not imported: this test
# asserts the SHIPPED bound, so a change to the constant should fail here and be
# an explicit decision rather than silently followed.
MAX_IDLE_DAYS = 90

# The staged history, aged against the shipped half-life rather than shortening
# it (see the module docstring for why shortening is not possible). 24h at a 6h
# half-life is 4 elapsed levels, which more than forgives the three staged
# faults, so the next real fault must start over at level 1.
SHIPPED_HALFLIFE_HOURS = 6
STAGED_FAULT_AGE_SEC = 24 * 3600
STAGED_FAULT_LEVELS = 3
# Staged on the SAME row, aged identically. Its job is to prove the other half of
# the decision: this tally must come out un-forgiven while the ranking counter
# beside it is forgiven completely.
STAGED_HARD_FAULTS = 2

# Rows that exist only to be pruned (or, in the last case, kept). Never connected
# to, so they need only be well-formed node-id strings.
GHOST_IDLE_FAULT = "02" + "1a" * 32
GHOST_IDLE_SUCCESS = "02" + "2b" * 32
GHOST_IDLE_HARD = "02" + "3c" * 32

CRASH_MARKER = "cannot pickle"
PERSIST_FAILED_MARKER = "could not persist peer reliability"
EXECUTOR_ABORT_MARKERS = ("Exception in _execute", "liquidity evaluation failed")


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _peer_reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get(PEER_RELIABILITY_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


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
        _mine(rig, 1)          # wallet events drive evaluations
        time.sleep(period)
    return cond()


def _unreachable_partner() -> Tuple[str, str]:
    """One (node_id, connect_str): a valid secp256k1 pubkey at a port nobody is
    listening on. Valid on purpose -- a malformed key would fail in parsing,
    before the connect attempt that produces the fault we are after."""
    node_id = ecc.ECPrivkey(bytes([0x11]) * 32).get_public_key_hex(compressed=True)
    with socket.socket() as s:              # a port that was free a moment ago
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return node_id.lower(), f"{node_id}@127.0.0.1:{port}"


def _stage_stale_rows(rig, aged_node_id: str) -> None:
    """Stop the client, write the stale peer rows into its wallet file, restart it.

    Done with the daemon down so the write cannot race Electrum's own; on restart
    the plugin's first read of the store is db-backed, which is also the shape
    that has broken reliability writes before.
    """
    stop_daemon(CLIENT)
    kill_inst_daemon(CLIENT)
    time.sleep(2)

    path = wallet_path(CLIENT)
    with open(path) as fh:
        db = json.load(fh)
    now = time.time()
    ancient = now - (MAX_IDLE_DAYS + 10) * 86400

    rel = dict(db.get(PEER_RELIABILITY_DB_KEY) or {})
    # (a) Idle past the bound, no penalty left, no hard faults: both go.
    rel[GHOST_IDLE_FAULT] = {"consecutive_faults": 2, "fault_count": 2,
                             "last_fault_ts": ancient, "last_reason": "long gone"}
    rel[GHOST_IDLE_SUCCESS] = {"success_count": 4, "last_success_ts": ancient}
    # (b) Just as idle, but carries the auto-ban tally: must be KEPT.
    rel[GHOST_IDLE_HARD] = {"consecutive_faults": 0, "fault_count": 2,
                            "hard_fault_count": 2, "last_fault_ts": ancient,
                            "last_reason": "force-closed twice, long ago"}
    # (c) The partner the test will really fault, carrying a day-old history that
    #     the shipped half-life should have fully forgiven by now.
    rel[aged_node_id] = {
        "consecutive_faults": STAGED_FAULT_LEVELS,
        "fault_count": STAGED_FAULT_LEVELS,
        "hard_fault_count": STAGED_HARD_FAULTS,
        "last_fault_ts": now - STAGED_FAULT_AGE_SEC,
        "last_reason": "staged history",
    }
    db[PEER_RELIABILITY_DB_KEY] = rel

    with open(path, "w") as fh:
        json.dump(db, fh)

    start_electrum_daemon_ready(rig.pm, CLIENT, log=run_mod.log)
    wait_electrum_ready(CLIENT)
    wait_lightning_ready(CLIENT)


def _arm_open_against(connect_str: str) -> None:
    """Make the engine want a channel and give it only the unreachable partner.

    ``partners_strict`` keeps Electrum's own suggestions out of the try-order, so
    the walk is exactly this one peer and the test cannot accidentally succeed at
    opening a real channel. Auto-ban is pushed out of reach because it has to be:
    a failed open is a HARD fault, so at the default threshold of 3 the staged
    tally plus one new fault would ban this peer and drop it from the try-order
    before the test could fault it again.
    """
    _setcfg("plugins.inbound_liquidity.preferred_partners", connect_str)
    _setcfg("plugins.inbound_liquidity.partners_strict", "true")
    _setcfg("plugins.inbound_liquidity.peer_reliability_enabled", "true")
    _setcfg("plugins.inbound_liquidity.peer_autoban_faults", "999")
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "99")
    # Swaps and the offline watchdog would fault other peers and muddy the store.
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last


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


def test_peer_escalation_ages_out_and_idle_rows_are_pruned(rig):
    node_id, connect_str = _unreachable_partner()
    _stage_stale_rows(rig, node_id)

    # Premise check: the staged rows really are on disk, read back through the
    # real wallet file after a real restart.
    staged = _peer_reliability()
    assert set(staged) >= {GHOST_IDLE_FAULT, GHOST_IDLE_SUCCESS, GHOST_IDLE_HARD,
                           node_id}, f"staging did not persist: {sorted(staged)!r}"
    assert staged[node_id]["consecutive_faults"] == STAGED_FAULT_LEVELS

    _arm_open_against(connect_str)

    # 1) PRUNING -- the two idle, unpenalised, hard-fault-free rows go.
    assert _wait_until(lambda: GHOST_IDLE_FAULT not in _peer_reliability()
                       and GHOST_IDLE_SUCCESS not in _peer_reliability(),
                       rig=rig, timeout=300), (
        f"idle peer rows were not pruned; store={_peer_reliability()!r}")
    assert "pruned 2 idle channel-peer reliability row(s)" in _client_log_text()

    # 2) PRUNING -- and the equally idle row carrying an auto-ban tally STAYS.
    #    This is the guard that keeps the prune from quietly pardoning a peer one
    #    force-close short of the threshold.
    assert GHOST_IDLE_HARD in _peer_reliability(), (
        "an idle row with a hard-fault tally was pruned, pardoning a ban "
        f"candidate; store={_peer_reliability()!r}")
    assert int(_peer_reliability()[GHOST_IDLE_HARD]["hard_fault_count"]) == 2, (
        "the preserved row lost the tally it was preserved for")

    # 3) AGING -- the real fault that follows. Captured from a SINGLE read of the
    #    wallet file the moment it appears: reading twice would let a second fault
    #    land in between and turn the level assertion into a flake.
    snapshot: Dict = {}

    def _faulted_again() -> bool:
        stats = _peer_reliability().get(node_id) or {}
        if int(stats.get("fault_count", 0)) > STAGED_FAULT_LEVELS:
            snapshot.clear()
            snapshot.update(stats)
            return True
        return False

    assert _wait_until(_faulted_again, rig=rig, timeout=420), (
        "the unreachable partner never collected a real fault, so the aging "
        f"assertion has nothing to measure; row={_peer_reliability().get(node_id)!r}")

    levels = int(snapshot["consecutive_faults"])
    tally = int(snapshot["fault_count"])
    new_faults = tally - STAGED_FAULT_LEVELS
    # Holds however many new faults we happened to catch: the staged history is
    # fully forgiven, so escalation can only reflect faults recorded SINCE
    # staging. Unfixed, the first new fault alone puts this at 4 against a
    # new_faults of 1.
    assert levels <= new_faults, (
        f"peer escalation did not age out: {STAGED_FAULT_LEVELS} faults aged "
        f"{STAGED_FAULT_AGE_SEC / 3600:.0f}h at a {SHIPPED_HALFLIFE_HOURS}h "
        f"half-life should all be forgiven, so at most {new_faults} level(s) "
        f"should remain, got {levels} (row={snapshot!r})")
    if new_faults == 1:      # the expected case: exactly one fault since staging
        assert levels == 1, (
            f"a single fault after a fully-forgiven history must sit at the base "
            f"level, got {levels} (row={snapshot!r})")

    # 4) The OTHER half of the decision, on the very same row and the very same
    #    elapsed time: the auto-ban tally is not forgiven at all. A failed open is
    #    a hard fault, so the staged tally must have grown by exactly the new
    #    faults -- none of it aged away, while consecutive_faults above was
    #    forgiven down to nothing over the identical 24h.
    assert int(snapshot.get("hard_fault_count", 0)) == STAGED_HARD_FAULTS + new_faults, (
        f"the auto-ban tally must not age: {STAGED_HARD_FAULTS} staged plus "
        f"{new_faults} new hard fault(s) should be "
        f"{STAGED_HARD_FAULTS + new_faults}, got "
        f"{snapshot.get('hard_fault_count')} (row={snapshot!r})")
    assert levels < int(snapshot["hard_fault_count"]), (
        "the whole point of this test: over one span of quiet time the ranking "
        f"counter healed and the ban tally did not (row={snapshot!r})")

    # 5) Nothing escaped while all of that ran.
    log = _client_log_text()
    assert CRASH_MARKER not in log, log[-4000:]
    assert PERSIST_FAILED_MARKER not in log, log[-4000:]
    for marker in EXECUTOR_ABORT_MARKERS:
        assert marker not in log, f"{marker!r} in client log:\n{log[-4000:]}"
