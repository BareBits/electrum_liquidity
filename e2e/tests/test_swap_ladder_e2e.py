"""End-to-end proof that the SWAP LADDER is planned, walked and bounded against
a live rig.

The engine no longer picks one provider and one size. ``rank_swap_rungs`` expands
every eligible provider across a geometric size ladder (1.0 / 0.9 / 0.5 / 0.3),
drops any rung that breaches the cost ceiling, the user's minimum swap size or a
provider's own minimum, and orders what survives by cost per sat of inbound
liquidity. The executor walks that flat list until something commits.

What these prove that a mock cannot:

  * the ladder reaches a REAL Electrum and a REAL swapserver -- the amounts are
    accepted, priced and paid over Lightning, not merely logged;
  * a rung the network cannot route really fails, and a smaller one really
    settles, over a topology with a genuine capacity limit;
  * a provider whose every rung fails is demoted ONCE. That invariant used to come
    free from the old providers-outer / sizes-inner nesting; with one flat
    interleaved list it is explicit code, and getting it wrong would demote the
    CHEAPEST providers hardest -- they are the ones the ladder gives most rungs.

Two things about reading the rig's log are load-bearing here and were both got
wrong first time round, so they are worth stating:

  * Cascades INTERLEAVE. Several channels drain concurrently, so a flat count of
    "how many rungs ran" mixes them together. Everything below is parsed into
    per-cascade groups (:func:`_cascades`) and asserted one cascade at a time.
  * A rung logs its cost only when it HAS one. The head rung of every cascade
    carries ``all_in_cost_pct=None`` deliberately -- the action's reason already
    states the cost -- so a pattern that requires the cost suffix silently matches
    nothing and every assertion built on it passes vacuously.

What is NOT covered here, and why. The case this whole change exists for -- a
full-size rung failing mid-route and a SMALLER one settling -- could not be staged
in this rig, and three attempts are recorded here so nobody repeats them:

  * A swapserver advertises ``max_forward = num_sats_can_receive()``, so in the
    stock rigs the largest swap a provider will accept is by construction one the
    route can already carry. No gap to exploit.
  * Putting the provider two hops away (deep-hop) and narrowing the first hop
    does create the gap on paper, but narrowing it by ``push`` WIDENS it: PARTNER
    opens that channel, so it forwards ``capacity - push``. Lowering push to
    0.003 against the 0.03 deep-hop capacity left the partner holding 2.7M sat
    and a ~758k swap settled on the first rung.
  * Narrowing the CAPACITY instead (0.004, then 0.005) stops the route being
    discoverable at all rather than making it capacity-limited: every rung then
    dies on ``NoPathFound`` -- including the 45,000 sat mining-fee prepayment --
    so it fails identically at every size and proves nothing about size. This is
    the same wall rig/services.disable_forwarding documents ("starving the hop of
    capacity can never be" deterministic).

So the descend-until-one-routes behaviour is covered where it can be made
deterministic: tests/test_swap_ladder_cascade_glue.py
(``test_a_smaller_rung_of_the_same_provider_can_be_the_one_that_works``). What IS
asserted here against a live rig is the part those runs did reproduce every time:
the whole ladder is generated, walked in cost order, descending, correctly
labelled, with cost per sat rising as size falls.

Heavy and slow: a rig per test, ~5-8 min each. Needs the electrum venv + docker.
Function-scoped rigs (each wipes .run and kills any previous rig), so these must
NOT run while a manual run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_swap_ladder_e2e.py -q -s
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional

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

# --- reading the rig ------------------------------------------------------
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


def _entries(category: str, kind: str = "swap") -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == category and e.get("kind") == kind]


def _swap_actions() -> List[Dict]:
    return _entries("action")


def _swap_declines() -> List[Dict]:
    return _entries("decline")


def _decline_reasons() -> str:
    return json.dumps([d.get("reason") for d in _swap_declines()][-5:])


def _reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get("inbound_liquidity_provider_reliability", {})
    return raw if isinstance(raw, dict) else {}


def _max_inbound() -> int:
    return max((c["remote_balance"] for c in _channels()), default=0)


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


_CASCADE_RE = re.compile(r"reverse swap for (\S+): (\d+) rung\(s\) to try")
# e.g. "reverse swap via npub199r…… (rung 1 of 4, reduced size): 759478 sat, all-in cost 6.425%"
# Parenthetical and cost are BOTH optional -- see the module docstring.
_RUNG_RE = re.compile(
    r"reverse swap via (\S+?)(?: \(([^)]*)\))?: (\d+) sat"
    r"(?:, all-in cost ([\d.]+)%)?")


def _cascades() -> List[Dict]:
    """Every cascade in the log, in order, each with the rungs it attempted.

    ``{"short_id", "planned", "rungs": [{"provider", "amount", "cost",
    "reduced"}]}`` -- ``planned`` is how long a ladder the ENGINE handed over,
    ``rungs`` how far the executor actually got down it.
    """
    out: List[Dict] = []
    for line in _client_log_text().splitlines():
        m = _CASCADE_RE.search(line)
        if m:
            out.append({"short_id": m.group(1), "planned": int(m.group(2)),
                        "rungs": []})
            continue
        m = _RUNG_RE.search(line)
        if m and out:
            blob = m.group(2) or ""
            out[-1]["rungs"].append({
                "provider": m.group(1),
                "amount": int(m.group(3)),
                "cost": float(m.group(4)) if m.group(4) else None,
                "reduced": "reduced size" in blob,
            })
    return out


def _all_rungs() -> List[Dict]:
    return [r for c in _cascades() for r in c["rungs"]]


def _cascades_for(short_id: str) -> List[Dict]:
    return [c for c in _cascades() if c["short_id"] == short_id]


def _faulted_providers() -> Dict[str, int]:
    """npub -> fault_count, for providers carrying a reliability fault."""
    return {npub: int(s.get("fault_count", 0))
            for npub, s in _reliability().items()
            if int(s.get("fault_count", 0)) > 0}


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


def _arm_swap_config(*, max_fee_pct: str = "80",
                     min_swap_sat: str = "0",
                     only_npub: Optional[str] = None) -> None:
    """Make the plugin reverse-swap, and nothing else.

    The ceiling defaults to deliberately permissive. The rig's providers charge a
    FIXED 22,500 sat mining fee and the on-chain claim costs about as much again,
    so even a full-size ~760k swap prices at ~6.4% and the deepest ladder rung at
    ~20% -- a realistic ceiling would decline the very rungs these tests are
    about. Economics are covered by test_reverse_swap_e2e; what is under test here
    is which rungs get planned and in what order.

    ``min_swap_sat`` defaults to 0 -- OFF -- for the ladder tests. The floor is a
    second, independent reason a rung can be dropped, and leaving the shipped
    25,000 sat value in place would mean every ladder assertion here quietly
    depended on it. Only ``test_the_minimum_swap_size_...`` turns it back on.

    ``only_npub`` whitelists a single provider, for tests about one provider's
    ladder: another provider's full-size rung would otherwise sort ahead of the
    small rungs under test -- correctly, since the fixed fee makes small rungs
    dear per sat -- and the ladder would never be walked that far.

    Master switch LAST, as everywhere in this suite: the rig ships
    automation_enabled=false so the plugin cannot act on a half-written ruleset.
    """
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "30000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", max_fee_pct)
    _setcfg("plugins.inbound_liquidity.min_swap_sat", min_swap_sat)
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    # No sink: the sink is the plugin's first choice for draining, and would
    # pre-empt the swap path entirely.
    _setcfg("plugins.inbound_liquidity.sink_address", "")
    if only_npub:
        _setcfg("plugins.inbound_liquidity.preferred_npubs", only_npub)
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")


# --- rigs -----------------------------------------------------------------
@pytest.fixture
def plain_rig():
    """The stock rig with ONE channel: the provider is our direct channel peer,
    so a correctly sized top rung routes first time.

    One channel, not the usual two, so the cascade under test is unambiguous --
    with two, both drain concurrently and their cascades interleave in the log.
    """
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--channels", "1"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


@pytest.fixture
def deep_dead_rig():
    """Stock deep-hop: partner2 refuses to relay, so EVERY rung of the provider
    behind it fails, at every size, with a real onion error."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--deep-hop", "--channels", "1"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


# --- success mode ---------------------------------------------------------
def test_a_routable_swap_completes_on_the_top_rung(plain_rig):
    """The ordinary case must not regress: when the planned size routes, the
    ladder costs nothing. A full ladder is planned, the first rung commits, and no
    smaller (and therefore dearer per sat) rung is ever attempted."""
    rig = plain_rig
    _arm_swap_config()
    baseline_inbound = _max_inbound()

    assert _wait_until(lambda: bool(_cascades()), rig=rig, timeout=420), (
        "the plugin never started a swap cascade; declines: " + _decline_reasons())
    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=420), (
        "no swap completed; declines: " + _decline_reasons())
    assert _wait_until(lambda: _max_inbound() > baseline_inbound, rig=rig,
                       timeout=300), (
        f"inbound liquidity did not increase (max remote_balance={_max_inbound()}, "
        f"baseline={baseline_inbound})")

    first = _cascades()[0]
    # The ENGINE planned the FULL ladder: the ceiling is wide open here and the
    # rig provider's minimum (20,000 sat) is far below the smallest rung, so
    # nothing truncates it. This is the baseline the truncation test measures
    # against.
    assert first["planned"] == 4, (
        f"expected the full 4-rung ladder to be planned, got {first}")
    # ...and exactly one rung ran, at full size.
    assert len(first["rungs"]) == 1, (
        f"a routable swap should commit on its first rung: {first}")
    assert first["rungs"][0]["reduced"] is False, (
        f"the top rung was labelled reduced: {first['rungs'][0]}")


# --- the shape of the walk, against a path nothing can cross ---------------
def test_the_whole_ladder_is_walked_when_no_rung_can_route(deep_dead_rig):
    """No rung of this provider can route -- partner2 refuses to relay at any
    size -- so the cascade walks the ladder to the bottom. That makes this the
    place to assert the ladder's SHAPE against a live Electrum and a live
    swapserver, which is what three separate rig runs reproduced identically.

    Every property here failed at least once during development, so none of them
    is decoration: the sizes are the engine's geometric steps and not numbers the
    executor invented, they descend without repeating, only the sizes below the
    planned amount are labelled "reduced", and cost per sat RISES as size falls --
    the trade-off the cost ceiling exists to bound.
    """
    rig = deep_dead_rig
    assert rig.swap_npub, "rig did not discover a swap provider"
    # Whitelist the unreachable provider so the ladder under test is its own: the
    # directly-connected partner would win on cost-per-sat at full size and the
    # walk would stop there.
    _arm_swap_config(only_npub=rig.swap_npub)

    assert _wait_until(
        lambda: any(len(c["rungs"]) >= 4 for c in _cascades()), rig=rig,
        timeout=1200), (
        f"no cascade walked a full 4-rung ladder; cascades: {_cascades()}; "
        f"declines: {_decline_reasons()}")

    walked = next(c for c in _cascades() if len(c["rungs"]) >= 4)
    assert walked["planned"] == 4, f"engine planned {walked['planned']} rungs"
    amounts = [r["amount"] for r in walked["rungs"]]
    planned_amount = amounts[0]

    # The sizes are SWAP_LADDER_FACTORS off the planned amount.
    from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore
        SWAP_LADDER_FACTORS)
    expected = [int(planned_amount * f) for f in SWAP_LADDER_FACTORS]
    assert amounts == expected, f"rungs {amounts} are not the ladder {expected}"

    # Descending, no repeats -- a repeated rung would spend an attempt re-running
    # an identical payment.
    assert amounts == sorted(amounts, reverse=True), f"not descending: {amounts}"
    assert len(amounts) == len(set(amounts)), f"a rung repeated: {amounts}"

    # Only the sizes below the planned amount are labelled reduced.
    assert walked["rungs"][0]["reduced"] is False, "the full-size rung was labelled reduced"
    assert all(r["reduced"] for r in walked["rungs"][1:]), (
        f"a sub-planned rung was not labelled reduced: {walked['rungs']}")

    # Cost per sat rises as the ladder descends. The head rung carries no cost on
    # purpose (the action's reason states it), so compare the ones that do.
    costs = [r["cost"] for r in walked["rungs"] if r["cost"] is not None]
    assert len(costs) >= 3, f"expected costs on the fallback rungs: {walked['rungs']}"
    assert costs == sorted(costs), f"cost per sat did not rise as size fell: {costs}"

    # Nothing committed: every rung failed, so there is no action and no drain.
    assert not _swap_actions(), (
        "a swap completed even though no rung could route -- this rig is not "
        "actually blocking the path")


# --- fault accounting across a multi-rung provider ------------------------
def test_a_provider_is_faulted_once_per_cascade_not_once_per_rung(deep_dead_rig):
    """Every rung of this provider fails -- partner2 will not relay at any size --
    so the ladder is walked to the bottom. That is ONE observation about the
    provider, and must cost it one fault per cascade.

    Charging per rung would demote the cheapest providers fastest, since the cost
    ceiling grants them the most rungs, inverting the ranking over time.
    """
    rig = deep_dead_rig
    assert rig.swap_npub, "rig did not discover a swap provider"
    _arm_swap_config(only_npub=rig.swap_npub)

    assert _wait_until(lambda: bool(_faulted_providers()), rig=rig, timeout=1200), (
        f"the unreachable provider collected no fault; cascades: {_cascades()}")
    # The count is only meaningful once a cascade has walked past its first rung.
    assert _wait_until(
        lambda: any(len(c["rungs"]) > 1 for c in _cascades()), rig=rig,
        timeout=900), (
        f"no cascade got past its first rung, so once-per-rung and "
        f"once-per-cascade are indistinguishable here: {_cascades()}")

    cascades = [c for c in _cascades() if c["rungs"]]
    total_rungs = sum(len(c["rungs"]) for c in cascades)
    faults = _faulted_providers()
    assert len(faults) == 1, f"expected one faulted provider, got {faults}"
    count = next(iter(faults.values()))
    assert count <= len(cascades), (
        f"{count} fault(s) across {len(cascades)} cascade(s) totalling "
        f"{total_rungs} rung(s) -- the provider is being charged per rung")
    assert count < total_rungs, (
        f"{count} fault(s) for {total_rungs} rung(s): per-rung charging")


# --- the ceiling as the ladder's floor ------------------------------------
def test_the_cost_ceiling_truncates_the_ladder_from_the_bottom(plain_rig):
    """The cost ceiling decides how far down the ladder a wallet can reach:
    shrinking a swap raises its cost per sat, so the small (most routable) rungs
    breach it first.

    Measured against the 4-rung baseline the same rig produces under a permissive
    ceiling in ``test_a_routable_swap_completes_on_the_top_rung``, so this needs
    only ONE cascade rather than a second one after the channel is drained.

    The arithmetic is forced, not hoped for. The rig provider charges 0.5% plus a
    22,500 sat mining fee, and the on-chain claim costs about as much again -- so
    a ~760k full-size swap prices near 6.4% and the 50% rung near 12%. A 10%
    ceiling must therefore admit the top rungs and drop the bottom ones, whatever
    the claim fee happens to be at regtest feerates.
    """
    rig = plain_rig
    _arm_swap_config(max_fee_pct="10")

    assert _wait_until(lambda: bool(_cascades()), rig=rig, timeout=420), (
        "no ladder was planned under the tight ceiling; declines: "
        + _decline_reasons())
    first = _cascades()[0]
    assert 1 <= first["planned"] < 4, (
        f"a 10% ceiling should truncate the 4-rung ladder without erasing it, "
        f"got {first['planned']} rung(s): {first}")

    # Every rung that actually ran was priced within the ceiling -- the gate is
    # absolute, never relaxed to keep the walk going.
    assert _wait_until(lambda: bool(_all_rungs()), rig=rig, timeout=420), (
        "no rung was attempted under the tight ceiling; declines: "
        + _decline_reasons())
    over = [r for r in _all_rungs() if r["cost"] is not None and r["cost"] > 10.0]
    assert not over, f"rungs above the 10% ceiling were attempted: {over}"


# --- the user's minimum swap size -----------------------------------------
def test_the_minimum_swap_size_blocks_a_swap_the_ceiling_would_allow(plain_rig):
    """The floor is an independent rule, proved against a live provider that is
    perfectly willing to take the swap.

    The ceiling is wide open (80%) and the provider's own minimum (20,000 sat) is
    far below the amount, so nothing except the user's floor can refuse this.
    """
    rig = plain_rig
    # A floor far above anything a 0.02 BTC channel can drain.
    _arm_swap_config(max_fee_pct="80", min_swap_sat="5000000")

    assert _wait_until(lambda: bool(_swap_declines()), rig=rig, timeout=420), (
        "the plugin neither swapped nor explained why; decision log tail: "
        + json.dumps(_decision_log()[-3:]))
    reasons = " | ".join((d.get("reason") or "") for d in _swap_declines())
    assert "minimum swap size" in reasons, (
        f"declined for the wrong reason -- the floor must name itself so the user "
        f"knows which setting to change: {reasons!r}")

    # The floor is a PLANNING rule: no cascade should ever have started, so no
    # rung reached Electrum and nothing was paid.
    assert _cascades() == [], f"a cascade ran below the configured floor: {_cascades()}"
    assert not _swap_actions(), "a swap completed despite the floor"

    # Lower it and the same channel becomes swappable -- so the decline really was
    # the floor talking and not some other gate.
    _setcfg("plugins.inbound_liquidity.min_swap_sat", "25000")
    assert _wait_until(lambda: bool(_cascades()), rig=rig, timeout=420), (
        "lowering the floor did not make the swap possible, so the original "
        "decline may not have been the floor after all; declines: "
        + _decline_reasons())
    small = [r for r in _all_rungs() if r["amount"] < 25_000]
    assert not small, f"rung(s) below the lowered floor were attempted: {small}"
