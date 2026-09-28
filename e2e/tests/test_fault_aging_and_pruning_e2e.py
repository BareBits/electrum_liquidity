"""End-to-end tests for how provider faults AGE OUT and how the two fault-related
stores are PRUNED, driven through the REAL rig: bitcoind + Fulcrum + nostr + a
client Electrum daemon + two swapserver daemons, with every assertion read back
from the real on-disk wallet file.

Two behaviours are covered, both of which used to misjudge providers:

1. Escalation had unbounded memory. The ranking penalty is
   ``base · 2^(n-1) · 0.5^(age/H)``, whose terms collapse to
   ``base · 2^((n-1) - age/H)`` -- so a half-life of silence is already worth
   exactly one fault level. The *magnitude* decayed accordingly but the
   *counter* never did, so a provider that had been forgiven all the way down to
   a zero penalty still resumed at ``base · 2^(old faults)`` the moment it
   faulted once more. ``_aged_consecutive_faults`` now forgives one level per
   elapsed half-life at write time.

2. Neither store was bounded. A pending-swap record that reconciliation never
   got to (automation switched off, or the wallet closed for months) sat forever
   and was eventually read as a FRESH full-strength stuck-swap fault against a
   provider, stamped with today's date -- because by then Electrum has dropped
   the unfunded swap (``_fail_swap`` pops it) and the "did it fund?" branch can
   no longer answer. Provider-reliability rows likewise accumulated one per npub
   ever seen, for a provider set that churns.

Why the rig and not mocks: both paths are read-modify-write cycles against
Electrum's ``JsonDB``, which does NOT hand back what it was given (it re-wraps
nested dicts as ``StoredDict``s carrying a ``threading.RLock``). Every fake db in
``../../tests`` returns the dict it was handed, which is exactly the property the
real one lacks -- the same blind spot that let a ``TypeError: cannot pickle
'_thread.RLock' object`` sit under a green unit suite once before (see
``test_peer_fault_persistence_e2e``). Here the plugin runs inside a real daemon
and the assertions read the real wallet JSON.

How the staleness is staged: the client daemon is stopped and the stale records
are written straight into its wallet file before it is restarted. That is not a
shortcut around the code under test -- it is precisely the state a wallet that
sat unused for months comes back up in, and the pruning code then runs for real
on the next tick. The alternative (waiting 90 days) is not available, and the
bounds are deliberately module constants rather than config, so there is no knob
to shrink instead.

The escalation test needs only ONE real fault: three faults aged a day are staged
in the same way, and because a day is four of the shipped 6h half-lives, the real
fault that follows must land at level 1 rather than level 4. The half-life is
left at its default deliberately -- ``setconfig`` cannot store a fractional one
(it parses "0.25" into a ``Decimal`` that SimpleConfig refuses to save, silently
keeping the default), so shortening it from the test is not available and an
earlier draft that tried ended up measuring the default while claiming otherwise.

NOT covered here, deliberately: the common-mode reachability suppression
(``_CascadeFaults``). Its trigger -- ``sm.is_initialized`` not firing -- is
unreachable from the rig, because ``_attempt_reverse_swap`` calls
``sm.update_pairs(offer.pairs)`` immediately before the wait and
``update_pairs`` sets that event synchronously on the asyncio thread. A dead
provider in this rig therefore fails at its createswap RPC instead (see
``test_swap_provider_failover_e2e``, whose docstring says so). Reaching the init
timeout with a real npub needs a transport ``stop()`` racing the wait, which the
rig cannot stage; it is covered at glue level in
``../../tests/test_swap_provider_failover_glue.py``.

Heavy and slow (~10-14 min); needs the electrum venv + docker. Function-scoped
rig: it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_fault_aging_and_pruning_e2e.py -q -s
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
    PARTNER2,
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

RELIABILITY_DB_KEY = "inbound_liquidity_provider_reliability"
PENDING_SWAPS_DB_KEY = "inbound_liquidity_pending_swap_providers"
DECISION_LOG_DB_KEY = "inbound_liquidity_decision_log"

# Matches PENDING_SWAP_MAX_AGE_SEC / RELIABILITY_ROW_MAX_IDLE_SEC in the plugin.
# Not imported: this test asserts the *shipped* bound, so a change to the
# constant should fail here and be an explicit decision, not silently followed.
MAX_AGE_DAYS = 90

# The escalation test stages faults OLD ENOUGH for the shipped half-life to
# forgive them, rather than shortening the half-life. That is not just simpler --
# a fractional half-life cannot be set at all: Electrum's ``setconfig`` parses
# "0.25" into a ``Decimal``, which SimpleConfig then refuses to store ("json
# error: cannot save ... Decimal('0.25')"), leaving the default silently in force.
# An earlier draft of this test did exactly that and measured the default.
#
# 24h at the shipped 6h half-life is 4 elapsed levels, which more than forgives
# the three staged faults, so the next real fault must start over at level 1.
SHIPPED_HALFLIFE_HOURS = 6
STAGED_FAULT_AGE_SEC = 24 * 3600
STAGED_FAULT_LEVELS = 3

# An npub that never existed, used for the rows that must be pruned. Keeping them
# distinct from the real provider's npub is what lets the test assert that
# pruning is selective rather than a wipe.
GHOST_NPUB = "npub1ghostproviderthatwentawaylongago00000000000000000000000000"
GHOST_PENDING_HASH = "ab" * 32


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get(RELIABILITY_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _pending_swaps() -> Dict[str, Dict]:
    raw = _wallet_db().get(PENDING_SWAPS_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _fault_log() -> List[Dict]:
    raw = _wallet_db().get(DECISION_LOG_DB_KEY, [])
    entries = raw if isinstance(raw, list) else []
    return [e for e in entries if e.get("category") == "fault"]


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


def _stage_stale_records(rig) -> None:
    """Stop the client, write the stale records into its wallet file, restart it.

    Done with the daemon down so the write cannot race Electrum's own; on restart
    the plugin's first read of both stores is db-backed, which is also the shape
    that has broken reliability writes before.
    """
    stop_daemon(CLIENT)
    kill_inst_daemon(CLIENT)
    time.sleep(2)

    path = wallet_path(CLIENT)
    with open(path) as fh:
        db = json.load(fh)
    now = time.time()
    ancient = now - (MAX_AGE_DAYS + 10) * 86400

    # (a) A pending-swap record far past the retention bound, attributed to the
    #     REAL provider. Unfixed, reconciliation reads it as a fresh stuck-swap
    #     fault against that provider on the very first tick.
    pending = dict(db.get(PENDING_SWAPS_DB_KEY) or {})
    pending[GHOST_PENDING_HASH] = {
        "npub": rig.swap_npub, "started_ts": ancient,
        "node_id": "", "channel_id": "", "fee_basis_sat": 0,
    }
    db[PENDING_SWAPS_DB_KEY] = pending

    # (b) Two reliability rows that must go: idle past the bound with no
    #     remaining penalty, one last active via a fault and one via a success.
    # (c) One row that must STAY and must be aged rather than compounded: the
    #     real provider, carrying three faults from an hour ago.
    rel = dict(db.get(RELIABILITY_DB_KEY) or {})
    rel[GHOST_NPUB] = {"consecutive_faults": 2, "fault_count": 2,
                       "last_fault_ts": ancient, "last_reason": "long gone"}
    rel[GHOST_NPUB + "b"] = {"success_count": 4, "last_success_ts": ancient}
    rel[rig.swap_npub] = {
        "consecutive_faults": STAGED_FAULT_LEVELS,
        "fault_count": STAGED_FAULT_LEVELS,
        "last_fault_ts": now - STAGED_FAULT_AGE_SEC,
        "last_reason": "staged history",
    }
    db[RELIABILITY_DB_KEY] = rel

    with open(path, "w") as fh:
        json.dump(db, fh)

    start_electrum_daemon_ready(rig.pm, CLIENT, log=run_mod.log)
    wait_electrum_ready(CLIENT)
    wait_lightning_ready(CLIENT)


def _arm_swap_config() -> None:
    """Make the plugin want to swap (not open or close), and set the half-life the
    escalation assertion depends on.

    Mirrors ``test_swap_provider_failover_e2e._arm_swap_config``: three channels
    because ``--second-provider`` adds one, and a permissive fee ceiling because
    the rig's fixed 45k prepayment makes the honest all-in cost of a ~1M swap
    several percent, which the default 0.9% gate would rightly decline.
    """
    _setcfg("plugins.inbound_liquidity.reliability_enabled", "true")
    # The half-life is deliberately left at its shipped default -- see
    # SHIPPED_HALFLIFE_HOURS for why it cannot be shortened from here anyway.
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")
    # The killed provider's channel goes offline with it; that is this test's own
    # doing, not a peer fault worth force-closing over.
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")


@pytest.fixture
def rig():
    """A rig with TWO swap providers; the cheaper one is killed by the test so it
    collects a real reliability fault."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--second-provider"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_stale_stores_are_pruned_and_escalation_ages_out(rig):
    assert rig.partner2_nodeid, "rig did not bring up a second provider"
    assert rig.swap_npub, "rig did not discover a swap provider npub"
    provider = rig.swap_npub

    # Kill the cheapest provider. Its offer stays fresh on the relay for an hour,
    # so the plugin still ranks it first and tries it -- and its createswap RPC
    # then times out, which is the real hard fault this test needs.
    stop_daemon(PARTNER2)
    kill_inst_daemon(PARTNER2)
    time.sleep(2)

    _stage_stale_records(rig)

    # Premise check: the staged records really are on disk and really are stale.
    assert GHOST_PENDING_HASH in _pending_swaps(), "staging did not persist"
    staged = _reliability()
    assert set(staged) >= {GHOST_NPUB, GHOST_NPUB + "b", provider}
    assert staged[provider]["consecutive_faults"] == STAGED_FAULT_LEVELS

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) PRUNING -- the ancient pending-swap record is dropped UNRESOLVED. The
    #    provider must not be blamed for it: a months-dead event is not evidence.
    assert _wait_until(lambda: GHOST_PENDING_HASH not in _pending_swaps(),
                       rig=rig, timeout=300), (
        "the 100-day-old pending-swap record was never dropped; "
        f"store={_pending_swaps()!r}")
    log = _client_log_text()
    assert "too stale to attribute" in log, (
        "the stale record was removed but not by the retention bound -- it was "
        "resolved by one of the attribution branches instead")
    assert not any("stuck" in (e.get("reason") or "") for e in _fault_log()), (
        f"a stuck-swap fault was charged for the stale record: {_fault_log()!r}")

    # 2) PRUNING -- the two idle, unpenalised rows are gone, and the real
    #    provider's row is NOT (selective, not a wipe).
    assert _wait_until(lambda: GHOST_NPUB not in _reliability()
                       and (GHOST_NPUB + "b") not in _reliability(),
                       rig=rig, timeout=300), (
        f"idle reliability rows were not pruned; store={_reliability()!r}")
    assert provider in _reliability(), (
        "the active provider's row was pruned along with the idle ones")
    assert "pruned 2 idle provider reliability row(s)" in _client_log_text()

    # 3) AGING -- the real fault that follows. The staged three faults are a day
    #    old at the shipped 6h half-life, i.e. four levels forgiven, so this fault
    #    must land at level 1. Unfixed it lands at 4, which at the default base
    #    is a 4% ranking penalty instead of 0.5%.
    # Captured from a SINGLE read of the wallet file, the moment the new fault
    # first shows up: reading the store twice would let a second real fault land
    # in between and turn the level assertion below into a flake.
    snapshot: Dict = {}

    def _faulted_again() -> bool:
        stats = _reliability().get(provider) or {}
        if int(stats.get("fault_count", 0)) > STAGED_FAULT_LEVELS:
            snapshot.clear()
            snapshot.update(stats)
            return True
        return False

    assert _wait_until(_faulted_again, rig=rig, timeout=600), (
        "the dead provider never collected a real fault, so the aging assertion "
        f"has nothing to measure; row={_reliability().get(provider)!r}")

    levels = int(snapshot["consecutive_faults"])
    tally = int(snapshot["fault_count"])
    new_faults = tally - STAGED_FAULT_LEVELS
    # The regression guard, and it holds however many new faults we happened to
    # catch: the staged history is fully forgiven, so escalation can only reflect
    # faults recorded SINCE staging. Unfixed, the first new fault alone puts this
    # at 4 against a new_faults of 1.
    assert levels <= new_faults, (
        f"escalation did not age out: {STAGED_FAULT_LEVELS} faults aged "
        f"{STAGED_FAULT_AGE_SEC / 3600:.0f}h at a {SHIPPED_HALFLIFE_HOURS}h half-life "
        f"should all be forgiven, so at most {new_faults} level(s) should remain, "
        f"got {levels} (row={snapshot!r})")
    if new_faults == 1:      # the expected case: exactly one fault since staging
        assert levels == 1, (
            f"a single fault after a fully-forgiven history must sit at the base "
            f"level, got {levels} (row={snapshot!r})")
    assert tally == STAGED_FAULT_LEVELS + new_faults, (
        f"the lifetime tally must keep counting every fault: {snapshot!r}")

    # 4) Nothing escaped while all of that ran.
    log = _client_log_text()
    for marker in ("cannot pickle", "could not persist provider reliability",
                   "could not persist pending-swap tracking",
                   "Exception in _execute", "liquidity evaluation failed"):
        assert marker not in log, f"{marker!r} in client log:\n{log[-4000:]}"
