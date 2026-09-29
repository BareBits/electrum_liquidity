"""Where a reverse swap's Lightning payment died decides WHO the plugin blames --
proven against real onion errors rather than synthetic ``HtlcLog``s.

THE BUG. ``_reconcile_pending_swaps`` used to charge the channel peer a fault for
any reverse swap whose Lightning payment failed, without asking where the HTLC
actually died. "Our payment failed" does not implicate the first hop: the peer may
have forwarded it perfectly and a node several hops out killed it. Live, that
surfaced as partners carrying a "last fault reason" of "reverse-swap Lightning
payment failed" and sinking down the ranking for other people's problems.

THE FIX. Electrum decrypts the onion error and records ``HtlcLog.sender_idx``, the
route index of the node that REPORTED the failure. ``route[0]`` is always the far
end of our own channel, so the peer is charged only when every decodable attempt
points at index 0.

WHY THE RIG. The unit tests next door
(``../../tests/test_ln_payment_failure_attribution.py``) build HtlcLogs by hand,
which pins the aggregation but assumes Electrum populates ``sender_idx`` the way
we read it. Only a real multi-hop payment, failed by a real remote node and
decrypted by a real ``decode_onion_error``, proves that assumption.

THE TOPOLOGY (``run.py --deep-hop``):

    client --[2 chans]--> partner --[hop]--> partner2 --[deep hop]--> partner3
                            ^                   ^                        ^
                       our channel         REFUSES to            cheapest swap
                          peer              forward               provider

partner3 undercuts every other provider on fee (0.05%), so the plugin plans its
swap against it -- which forces the Lightning leg across partner2. partner2 runs
with ``lightning_forward_payments=false``, so ``_maybe_forward_htlc`` answers with
``OnionRoutingFailure(PERMANENT_CHANNEL_FAILURE)``: a real onion error, encrypted
with partner2's own shared secret, decoding to ``sender_idx == 1``.

Capacity starvation could NOT have produced this. A swapserver advertises
``max_forward = num_sats_can_receive()``, so the largest swap partner3 will accept
is by construction the most partner2 could still have forwarded to it -- the two
numbers are the same number, and the payment would always fit. Refusing to forward
is the only lever that fails a payment PAST the first hop deterministically.

Heavy and slow (~10-12 min per test: four daemons, three announced channels to
bury, gossip sync, then a full swap cascade). Needs the electrum venv + docker.
Function-scoped rig; it wipes ``.run`` and kills any previous rig, so it must NOT
run while a manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_ln_failure_hop_attribution_e2e.py -q -s
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
    PARTNER,
    electrum_cli,
    mine,
    wait_wallet_height,
    wallet_path,
)

PEER_RELIABILITY_DB_KEY = "inbound_liquidity_peer_reliability"
PROVIDER_RELIABILITY_DB_KEY = "inbound_liquidity_provider_reliability"
PENDING_SWAPS_DB_KEY = "inbound_liquidity_pending_swaps"

# The reason string the plugin writes against a peer it holds responsible. Must
# stay in step with PEER_LN_FAILURE_REASON in the plugin.
PEER_FAULT_REASON = "reverse-swap Lightning payment failed at this peer (first hop)"
# The log line the suppression path emits. Asserting on it is what proves the
# verdict came from a decoded sender_idx, not merely that no fault happened to be
# written (which an unattempted swap would also satisfy).
SUPPRESSED_LOG = "Lightning payment failed beyond our channel peer"


def _setcfg(key: str, value: str, *, inst=CLIENT) -> None:
    electrum_cli("setconfig", key, value, inst=inst)


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _peer_reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get(PEER_RELIABILITY_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _provider_reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get(PROVIDER_RELIABILITY_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _pending_swaps() -> Dict[str, Dict]:
    raw = _wallet_db().get(PENDING_SWAPS_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _peer_faults(node_id: str) -> int:
    """How many faults the plugin has recorded against ``node_id``."""
    stats = _peer_reliability().get(node_id.lower(), {})
    return int(stats.get("fault_count", 0) or 0)


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


def _arm_swap_config() -> None:
    """Make the plugin swap rather than open or close, and make sure nothing
    ELSE can fault the peer while we are watching that store.

    The fee ceiling is deliberately permissive: the rig's providers charge a fixed
    45k prepayment, so the honest all-in cost on these swap sizes is several
    percent and the gate would otherwise (correctly) decline every swap before the
    attribution under test could run.
    """
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")
    # The peer-reliability store is the assertion target, so every OTHER writer
    # into it has to be off or a passing/failing result would be ambiguous.
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.max_closes_per_day", "0")


@pytest.fixture
def rig():
    """A four-node rig whose middle node refuses to forward."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--deep-hop", "--channels", "1"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def _assert_deep_hop_topology(rig) -> str:
    """Confirm the shape the whole test rests on, and return the peer node id."""
    assert rig.partner3_nodeid, "rig did not bring up the deep-hop provider"
    assert rig.deep_hop_channel, "rig did not open the partner3 -> partner2 channel"
    peer_id = rig.partner_nodeid.split("@", 1)[0].lower()
    chans = _channels()
    assert chans, "client has no channels"
    assert all(c["remote_pubkey"].lower() == peer_id for c in chans), \
        ("the client must hold channels to the PARTNER only -- a direct channel "
         "to a provider would make its swap payment one-hop and never reach "
         "partner2 at all")
    assert _peer_faults(peer_id) == 0, "peer already carried a fault before the test"
    return peer_id


def test_a_failure_past_our_peer_does_not_fault_the_peer(rig):
    """The regression this change fixes.

    The plugin plans its swap against partner3 (cheapest), pays over
    client -> partner -> partner2 -> partner3, and partner2 refuses to relay. The
    onion error therefore comes from partner2, one hop PAST our channel peer --
    and the peer, which forwarded the HTLC perfectly well, must not be charged
    for it.
    """
    peer_id = _assert_deep_hop_topology(rig)

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) The attribution ran and concluded the failure was downstream. This is the
    #    assertion that matters: it can only be reached from a decoded sender_idx
    #    greater than zero, i.e. a real onion error from a real remote node.
    assert _wait_until(lambda: SUPPRESSED_LOG in _client_log_text(),
                       rig=rig, timeout=900), \
        ("the plugin never attributed a failed swap payment past the first hop; "
         "either no swap was attempted, or the payment failed without a decodable "
         "sender_idx (check the client log for 'reverse swap')")

    # 2) ... and the peer was NOT charged for it. Before the fix this store held a
    #    fault against the partner with reason "reverse-swap Lightning payment
    #    failed", which is exactly what demoted innocent partners.
    assert _peer_faults(peer_id) == 0, (
        f"the channel peer was faulted for a failure that happened past it: "
        f"{_peer_reliability().get(peer_id)}")

    # 3) The record was RESOLVED, not merely left unblamed. A record that survives
    #    would age into a provider stuck-fault an hour later, quietly moving the
    #    blame rather than dropping it.
    assert _wait_until(lambda: not _pending_swaps(), rig=rig, timeout=180), \
        f"pending-swap record was not resolved: {_pending_swaps()}"

    # 4) The provider still takes its own soft fault for the failed payment
    #    (``_charge_payment_failure``), so the event is recorded somewhere and the
    #    ranking still learns from it -- we suppressed the PEER fault, not all
    #    accounting.
    assert any(int(s.get("fault_count", 0) or 0) > 0
               for s in _provider_reliability().values()), \
        "no provider was charged either; the failure vanished from the books"


def test_a_failure_at_our_peer_still_faults_the_peer(rig):
    """The other half: the suppression must not have disarmed the fault entirely.

    Same rig, but the PARTNER is made to refuse forwarding too. It is the first
    hop, so it now reports the error itself and ``sender_idx`` is 0 -- the one
    case the peer is genuinely responsible for, and must still be charged.
    """
    peer_id = _assert_deep_hop_topology(rig)

    # Make our own channel peer the node that refuses. ``_maybe_forward_htlc``
    # reads this config at forward time, so no restart is needed -- and a restart
    # would drop the channels and the gossip graph with them.
    _setcfg("lightning_forward_payments", "false", inst=PARTNER)

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # The peer now reports the failure, so it is charged -- with the reason that
    # names the hop, not the old catch-all wording.
    assert _wait_until(lambda: _peer_faults(peer_id) >= 1, rig=rig, timeout=900), \
        ("the channel peer was never faulted for a failure it reported itself; "
         "the hop-0 path is broken")
    stats = _peer_reliability()[peer_id]
    assert stats["last_reason"] == PEER_FAULT_REASON, \
        f"unexpected fault reason: {stats.get('last_reason')!r}"
    # Soft: a routing failure is not force-close-grade evidence and must never
    # count toward the auto-ban tally.
    assert int(stats.get("hard_fault_count", 0) or 0) == 0, \
        "a routing failure was recorded as a HARD fault and can now reach auto-ban"
