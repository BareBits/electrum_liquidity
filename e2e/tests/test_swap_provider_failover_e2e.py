"""End-to-end test that the plugin FAILS OVER between swap providers within a
single evaluation cycle.

Before this behaviour existed, a swap that failed burned the channel's 180s
cooldown and the next cycle re-picked the very same provider (a failed attempt
often records no fault at all, so the ranking was unchanged) -- a wedge that
could persist indefinitely while a perfectly good provider sat idle.

The rig is launched with ``--second-provider``, which adds a second swapserver
that deliberately UNDERCUTS the first (0.1% vs 0.5%), so the plugin's
cheapest-first ranking always tries it first. The test then kills that
provider's daemon. Its offer stays valid on the nostr relay (Electrum treats
offers as fresh for 3600s), so the plugin still ranks it first, tries it, times
out its createswap RPC -- and must then fall back to the surviving provider and
complete a REAL reverse swap, without waiting out the cooldown.

WHY THE CLIENT HAS NO CHANNEL TO PARTNER2, which every test here depends on.

Partner2's advertised reverse cap comes from PARTNER, over a channel the client
is not party to (``Rig._open_provider2_supply``). It used to come from a channel
the CLIENT held, and that made this whole file fail roughly one run in three, a
different test each time:

  * a swapserver advertises ``max_forward = num_sats_can_receive()``, and one
    0.02 BTC channel is only worth ~900k of it -- enough for ONE of the client's
    ~760k swaps;
  * the engine draws that cap down as it plans (``_decide_reverse_swaps``'s
    ``consumed``), so exactly one channel per pass could be planned against
    partner2 and the rest fell back to partner1 with their rungs re-priced;
  * which channel got it was decided by ``LNWallet.__init__``'s
    ``random_shuffled_copy`` of the channel dict -- a fresh coin flip per daemon
    start.

When the shuffle put the client->partner2 channel first, that channel drained
through its OWN direct peer, succeeded, consumed the cap, and no payment failed
anywhere in the run -- so the three tests below that open by waiting for a failed
payment waited out their full timeout. Sourcing partner2's capacity from PARTNER
removes the choice entirely: every client channel is a partner1 channel, every
one of them ranks partner2 first on price, and every one of them finds it
unpayable. (The engine now also fixes its own half of this -- ``drain_order``
plans the fullest channel first instead of taking Electrum's shuffle -- but the
rig must not depend on that to be deterministic.)

What is asserted:
  * the dead provider is tried first and collects a reliability fault,
  * the swap nevertheless completes, via the OTHER provider, in the same cycle
    (the action's detail records which provider served it),
  * inbound liquidity actually increased -- a real swap, not just a log line.

A second test covers the failure on the OTHER side of that RPC: a provider that
answers createswap normally but cannot be paid over Lightning. It reaches the
executor as ``reverse_swap`` returning None, which used to be misread as "the
provider accepted, funding is pending" and ended the cascade with nothing done.
That one needs no sabotage either: partner2 is reachable over nostr and has real
capacity, so it answers createswap normally, but the client holds no channel to
it, gossip is off and Electrum nodes do not forward as trampolines -- so the
Lightning leg, pinned to a partner1 channel, can find no route at any size.

That same test now also pins the reason the cascade still went nowhere in
practice: at the instant ``reverse_swap`` returns, BOTH of a swap's HTLCs are
typically still unresolved, and the failover gate -- which must not fail over with
a live HTLC -- read "unresolved" as "committed" and stopped.

The mining-fee PREPAYMENT was the first leg found to do this: Electrum fires the
minerFeeInvoice fire-and-forget and races only the main payment against funding
detection, so ``reverse_swap`` returns without ever looking at it, and because the
provider only fulfils it once the main payment's MPP set arrives too, a dead main
payment dooms it -- so it was live essentially every time.

The MAIN payment does the same thing for its own reason, and was left strict when
the prepayment was fixed: ``pay_to_node`` raises PaymentFailure the moment it
passes PAYMENT_TIMEOUT or runs out of attempts, while its ``finally`` deliberately
leaves unresolved HTLCs alone, so what it already sent sits in the channel until
the far end fails it back. Observed on mainnet as two rungs of one provider
failing ~126s apart: the first read as "nothing committed" and correctly retried
smaller, the second as "may have committed funds", ending the cascade with four
ranked providers untried.

Note this rig hid both for a long while: the surviving provider is a direct
channel peer, so HTLCs resolve fast enough that the old code sometimes got past
the gate anyway.

A final test covers the rest of the fix: a swap that never funded is never
written to the decision log as a completed action.

WHAT IS DELIBERATELY NOT HERE: the SIZE LADDER. A test in this file used to
demand that a provider whose payment failed be retried at a smaller size, and it
could not pass on this topology for a reason that has nothing to do with
failover. Rungs are ordered by cost per sat, and the fixed mining fee makes a
small swap dear per sat -- so the surviving provider's FULL-size rung (6.425%)
sorts ahead of every reduced rung (6.684% and up), and being a direct channel
peer it settles. The ladder is therefore never descended here, and the test only
ever passed when an unrelated capacity clamp happened to shrink some rung.

That behaviour is covered where it can be made deterministic, and already was:

  * ``tests/test_swap_ladder_e2e.py`` -- the whole ladder walked when no rung can
    route, its sizes pinned to ``SWAP_LADDER_FACTORS``, descending, no repeats,
    only sub-planned rungs labelled "reduced", cost per sat rising as size falls,
    and one fault per cascade rather than one per rung. It gets determinism from
    the ``preferred_npubs`` whitelist, which keeps the direct peer out of the
    ranking -- the move this file cannot make, since the direct peer IS the
    failover target under test;
  * ``tests/test_swap_ladder_cascade_glue.py`` -- a smaller rung of the same
    provider being the one that works, which that file records as un-stageable on
    any of these rigs after three separate attempts.

Heavy and slow (~8-12 min each); needs the electrum venv + docker. Function-scoped
rig (wipes .run, kills any previous rig), so it must NOT run while a manual
run.py rig is up. Gated behind RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest \
          tests/test_swap_provider_failover_e2e.py -q -s
"""
from __future__ import annotations

import re
import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig.services import (  # noqa: E402
    set_client_plugin_setting,
    CLIENT,
    PARTNER2,
    electrum_cli,
    kill_inst_daemon,
    mine,
    stop_daemon,
    wait_wallet_height,
    wallet_path,
    read_electrum_log,
)


def _setcfg(key: str, value: str) -> None:
    """Set a config key -- or, for one of the plugin's settings, the CLIENT
    WALLET's value of it.

    The plugin's settings live in the wallet now, so `setconfig
    plugins.inbound_liquidity.<key>` configures nothing. Routed here rather than
    at the ~300 call sites, which all read the same either way: "configure the
    plugin thus". Non-plugin keys (lightning_max_funding_sat, use_gossip, ...)
    are Electrum's own and still go to the config."""
    prefix = "plugins.inbound_liquidity."
    if key.startswith(prefix):
        set_client_plugin_setting(key[len(prefix):], value)
        return
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _swap_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "swap"]


def _reliability() -> Dict[str, Dict]:
    raw = _wallet_db().get("inbound_liquidity_provider_reliability", {})
    return raw if isinstance(raw, dict) else {}


def _max_inbound() -> int:
    return max((c["remote_balance"] for c in _channels()), default=0)


def _client_log_text() -> str:
    """Everything the client daemon has logged, oldest first.

    Thin alias for ``rig.services.read_electrum_log``: it reads the rotation
    backups and the files left by earlier daemon runs, neither of which the
    ``electrum_log_*.log`` glob this used to do could see.
    """
    return read_electrum_log(CLIENT)


# One "reverse swap via <provider>… (rung N of M…): <amount> sat" line per
# attempt the cascade makes. The provider label is the npub truncated to 20
# chars by the executor, which is still unique between two providers.
_ATTEMPT_RE = re.compile(r"reverse swap via (\S+?)…? \(rung \d+ of \d+")

# The provider recorded on a completed swap's decision-log entry.
_ACTION_NPUB_RE = re.compile(r"provider (npub1[0-9a-z]+)")


def _attempted_providers() -> List[str]:
    """The provider labels the cascade actually tried, in order, with repeats.

    This is how "the cascade advanced past the first provider" is read now.
    There is no longer a log line that says so: since c926c05 ("rank swaps, not
    providers -- price every (provider, size) pair") the cascade walks one
    cost-ordered list of (provider, size) rungs and simply logs the next
    attempt. The old ``"failing over to the next provider"`` line went with it.
    """
    return _ATTEMPT_RE.findall(_client_log_text())


def _swap_action_npub(action: Dict) -> Optional[str]:
    """The provider npub on a completed swap action, or None if absent."""
    match = _ACTION_NPUB_RE.search(action.get("detail", "") or "")
    return match.group(1) if match else None


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
    """Make the plugin swap rather than open or close.

    ``max_channels`` matches the rig's channel count exactly. Too low and the
    engine tries to CLOSE one instead of swapping; too high and it tries to OPEN
    another, since the client still holds ~11.9 BTC on-chain. Either way the
    cascade under test never runs. ``--second-provider`` gives the client two
    channels, both to partner1 -- partner2's capacity comes from PARTNER, not
    from a channel of ours (see the module docstring).

    The fee ceiling is deliberately permissive: the rig's providers charge a
    fixed 45k prepayment on ~760k swaps, so the honest all-in cost is several
    percent and the gate would otherwise (correctly) decline every swap before
    the failover under test could run.
    """
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "true")
    _setcfg("plugins.inbound_liquidity.max_channels", "2")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "100000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "10")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "10")
    # Killing partner2 is the test's own doing. It no longer costs the client a
    # channel (we hold none to it), but the setting stays off so a slow peer
    # reconnect cannot turn into a force-close mid-test.
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.liquidity_goal_sat", "0")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")


def _assert_no_client_channel_to_partner2(rig, chans: List[Dict]) -> None:
    """The topology invariant this whole file rests on.

    Two things have to be true at once, and they used to be in tension:

      * partner2 must advertise enough reverse capacity to host EVERY channel's
        swap in one planning pass, or the engine's ``consumed`` draw-down starves
        the later channels and which one failed over became a coin flip;
      * the client must not be able to PAY partner2, or the cheapest provider
        simply succeeds and there is no failover to observe.

    A client->partner2 channel would satisfy the first and destroy the second, so
    the capacity comes from PARTNER instead. Asserted rather than assumed: it is
    one ``open_channels`` call away from silently reverting, and if it did, this
    file would go back to failing one run in three for reasons that look like
    Lightning flakiness.
    """
    p2_pubkey = rig.partner2_nodeid.split("@", 1)[0].lower()
    assert not [c for c in chans if c["remote_pubkey"].lower() == p2_pubkey], \
        ("the client holds a channel to partner2, so its swaps would be payable "
         "and no failover could be observed; partner2's capacity must come from "
         "PARTNER (see Rig._open_provider2_supply)")


def _assert_partner2_can_host_every_channel(rig, chans: List[Dict]) -> None:
    """...and the first half of that invariant: partner2's advertised cap has to
    cover the whole pass, not just one channel.

    ``max_forward_sat`` is what bounds a client REVERSE swap (the plugin reads
    it, not ``max_reverse_sat`` -- see liquidity_manager.ProviderOffer). If it
    only covers one channel, the engine plans the rest against partner1 and they
    never reach the failover path at all.
    """
    offer = rig.swap_offer or {}
    cap = int(offer.get("max_forward_sat") or 0)
    # What one channel actually gets planned for, less the headroom the planner
    # subtracts: ~760k on a 0.02 BTC 50/50 channel.
    needed = len(chans) * 760_000
    assert cap >= needed, (
        f"partner2 advertises {cap} sat of reverse capacity but the pass needs "
        f"{needed} for {len(chans)} channel(s); the later channels would fall "
        f"back to partner1 and never exercise the failover")


@pytest.fixture
def rig():
    """A rig with TWO swap providers, the cheaper of which we later kill."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--second-provider"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_dead_provider_is_skipped_and_the_swap_completes_via_the_next_one(rig):
    # The rig pins swapserver_npub to the CHEAPEST provider, which is partner2 --
    # the one we are about to kill. Confirm the topology before relying on it.
    assert rig.partner2_nodeid, "rig did not bring up a second provider"
    assert rig.swap_npub, "rig did not pin a cheapest provider to kill"
    chans = _channels()
    assert len(chans) == 2, \
        f"expected 2 client->partner channels, got {len(chans)}"
    _assert_no_client_channel_to_partner2(rig, chans)
    baseline_inbound = _max_inbound()
    assert baseline_inbound <= 1_100_000, "baseline channels are not ~50/50"

    # Kill the cheap provider. Its offer stays fresh on the relay for an hour, so
    # the plugin will still rank it FIRST and try it -- exactly the wedge this
    # feature fixes.
    stop_daemon(PARTNER2)
    kill_inst_daemon(PARTNER2)
    time.sleep(2)

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) A reverse swap completes despite the first-ranked provider being dead.
    #    Generous timeout: the dead provider's createswap RPC must time out
    #    (~60s) before the cascade moves on.
    assert _wait_until(lambda: len(_swap_actions()) >= 1, rig=rig, timeout=420), \
        ("plugin never completed a reverse swap -- the cascade did not fall back "
         "to the surviving provider")

    # 2) It was a FAILOVER, not a first-attempt success: the swap was served by a
    #    provider OTHER than the one we killed.
    #
    #    Asserted on the provider, not on an attempt index. Under the size ladder
    #    the index in the detail is the rung's position in one cost-ordered list
    #    of (provider, size) pairs, so "rung 2" can perfectly well be the SAME
    #    provider at a smaller size, and the second provider's only rung can be
    #    "rung 1 of 1". The index cannot answer "did we fail over"; the identity
    #    can. (The old assertion looked for "provider 2 of", which c926c05
    #    renamed -- it had been failing ever since.)
    action = _swap_actions()[0]
    detail = action.get("detail", "")
    served_by = _swap_action_npub(action)
    assert served_by, f"the swap action does not record its provider: {detail!r}"
    assert served_by != rig.swap_npub, \
        ("the swap was served by the provider we killed, so no failover "
         f"happened; detail was: {detail!r}")

    # 3) The dead provider was tried FIRST, and the cascade then moved to another
    #    one. Both read off the attempts themselves (see _attempted_providers).
    log_text = _client_log_text()
    attempts = _attempted_providers()
    assert attempts, "the cascade logged no swap attempt at all"
    assert attempts[0] == rig.swap_npub[:20], \
        (f"the dead provider was not tried first; attempts began with "
         f"{attempts[0]!r} and the rig pinned {rig.swap_npub[:20]!r}")
    assert len(set(attempts)) >= 2, \
        (f"the cascade never tried a second provider; it only attempted "
         f"{set(attempts)}")
    faults = _reliability()
    assert faults, "the dead provider collected no reliability fault"
    assert any(int(s.get("fault_count", 0)) > 0 for s in faults.values()), \
        f"no provider fault recorded: {faults}"

    # 4) A real swap: inbound liquidity actually increased.
    assert _wait_until(lambda: _max_inbound() > baseline_inbound + 100_000,
                       rig=rig, timeout=300), \
        f"inbound liquidity did not increase (max remote_balance={_max_inbound()})"

    # 5) The dead provider now carries a fault, so the ranking demotes it and
    #    later cycles stop paying its timeout. (The decay/penalty arithmetic
    #    itself is unit-tested; what matters here is that a real fault landed.)
    dead_faults = [s for s in _reliability().values()
                   if int(s.get("fault_count", 0)) > 0]
    assert dead_faults, "the dead provider was not demoted for the next cycle"


def test_failed_lightning_payment_fails_over_to_the_next_provider(rig):
    """The OTHER way a swap dies: the provider answers createswap perfectly well,
    but our Lightning payment to it never lands.

    This one hid behind a wrong reading of Electrum for a long time.
    ``reverse_swap`` races ``pay_invoice`` against funding detection
    (``asyncio.wait(..., FIRST_COMPLETED)``) and then returns
    ``swap.funding_txid``; because ``pay_invoice`` RETURNS ``(success, log)``
    rather than raising, a failed payment simply wins the race and the function
    returns None. The executor used to read that None as "provider accepted,
    funding pending", score it as a completed swap and stop -- so the ranked
    failover providers were never tried and the channel burned its cooldown on a
    swap that had moved nothing. Live symptom: a 125s gap (Electrum's
    ``PAYMENT_TIMEOUT`` of 120s plus the createswap round trip) followed by
    ``reverse swap funding txid: None`` and no second attempt.

    No sabotage is needed -- the rig's own topology produces it, for EVERY channel
    rather than for a lucky one. The engine emits a swap action per over-trigger
    channel and ranks providers by cost, and all of the client's channels go to
    partner1, so every one of them is planned against the CHEAPER provider,
    partner2. That provider has ample inbound -- over a channel from PARTNER, not
    from us -- so it advertises real capacity and answers createswap normally. But
    the swap's Lightning leg is pinned to the channel being drained, gossip is
    off, and Electrum nodes do not forward as trampolines -- so the client has no
    route to partner2 at any size and the payment cannot land. The failover
    provider, partner1, sits at the far end of the very channel being drained, so
    the second attempt completes a real swap.

    An earlier version of this test closed the client's channel to partner2 to
    force the failure. That does not reach this path at all: a provider with no
    inbound ANYWHERE has no receive capacity, so it REJECTS createswap and the
    pre-existing SwapServerError arm handles it -- which is why the assertions
    below check the classification, not merely that some failover happened. The
    rig now gets this right by construction: partner2 has ample inbound (from
    PARTNER, so it accepts) and the client has no way to reach it (so the payment
    then fails). Both halves are asserted below, because the test is vacuous if
    either slips.
    """
    assert rig.partner2_nodeid, "rig did not bring up a second provider"
    chans = _channels()
    assert len(chans) == 2, \
        f"expected 2 client->partner channels, got {len(chans)}"
    _assert_no_client_channel_to_partner2(rig, chans)
    _assert_partner2_can_host_every_channel(rig, chans)

    baseline_inbound = _max_inbound()

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")   # arm last

    # 1) The unfunded swap is classified as a FAILED PAYMENT rather than as an
    #    accepted one. This is the regression itself: before the fix the executor
    #    read `reverse_swap() -> None` as "accepted, funding pending", logged
    #    "reverse swap funding txid: None" and stopped the cascade dead.
    #    Generous: the doomed payment burns the full 120s PAYMENT_TIMEOUT first.
    assert _wait_until(
        lambda: "the Lightning payment failed (no funds committed)"
                in _client_log_text(),
        rig=rig, timeout=600), \
        ("the unfunded swap was never classified as a failed payment; the "
         "executor most likely scored it as accepted and stopped the cascade")

    # 2) ... and the cascade actually advanced on that classification, in the SAME
    #    attempt rather than some later cycle.
    #
    #    This is where an unresolved HTLC used to stop everything -- first the
    #    mining-fee prepayment, which reverse_swap never waits for, and then (once
    #    that leg was given patience) the MAIN payment, whose HTLCs sit in the
    #    channel until the far end fails them back. Either one, read at the instant
    #    reverse_swap returns, said "funds may be committed" and ended the cascade,
    #    so it essentially never advanced. The gate now waits for both (bounded),
    #    which is why this is a plain wait for the failover rather than "give the
    #    wallet a few cycles and hope".
    #    Read off the attempts rather than a log line that says "failing over":
    #    c926c05 removed that line when it made the cascade a walk over ranked
    #    rungs (see _attempted_providers).
    assert _wait_until(
        lambda: len(set(_attempted_providers())) >= 2,
        rig=rig, timeout=600), \
        ("the failed payment never advanced the cascade to another provider; "
         f"attempts were: {_attempted_providers()}")

    # 2b) The HTLC-resolution gate must never be the reason a cascade stopped.
    #
    #     The gate covers BOTH of a swap's HTLCs. The prepayment was the first leg
    #     to be given patience; the MAIN payment was left strict and went on
    #     killing cascades for the same reason -- pay_to_node raises PaymentFailure
    #     while the HTLCs it already sent are unresolved, and get_payments(
    #     status='inflight') reads live channel state, so it keeps naming them
    #     until the far end fails them back. Observed on mainnet as two rungs of
    #     one provider failing ~126s apart: the first read as "nothing committed"
    #     and retried smaller, the second as "may have committed funds", ending
    #     the cascade with four ranked providers untried.
    #
    #     Asserted as a NEGATIVE, deliberately. Whether the gate has to wait at all
    #     depends on rig timing that this test cannot pin: the rig's providers are
    #     direct channel peers, so HTLCs sometimes resolve before the gate first
    #     asks (nothing to wait for) and sometimes after it (a real wait). An
    #     earlier version of this assertion demanded the wait happen every run and was
    #     flaky for exactly that reason -- the rig cannot reproduce mainnet's timing on
    #     demand, and pretending otherwise would be a test that lies about its
    #     coverage. The gate's outcomes are pinned precisely, with the timing under
    #     control, in tests/test_swap_provider_failover_glue.py.
    #
    #     What IS invariant is that the wait must not expire: if it does, the gate
    #     answered "possibly committed" and stopped a cascade that should have
    #     continued -- the original bug, back again, just slower.
    log_text = _client_log_text()
    assert "still has an HTLC in flight after" not in log_text, \
        ("the HTLC-resolution wait expired and the cascade stopped -- the budget "
         "is too small for real timing, or the HTLCs are not resolving at all")

    # 2c) However the gate answered, it must SAY why. Every COMMITTED verdict ends
    #     a cascade with providers untried, and the gate used to reach it in
    #     silence -- a live main HTLC, a settled prepayment and a failed lookup
    #     were indistinguishable in the log, which is what made the mainnet
    #     occurrence take a source re-read to diagnose rather than a log read.
    assert "failover gate:" in log_text, \
        "the failover gate reached a verdict without recording its reason"

    # When the gate DID have to wait (observed live: an 11s wait on this rig), it must
    # have resolved into a decision rather than timing out. Recorded either way, so a
    # run where the timing never produced a wait is not a failure.
    if "waiting up to" in log_text and "HTLC in flight" in log_text:
        assert "swap HTLCs resolved" in log_text, \
            "the gate started waiting for the HTLCs and never got an answer"
        print("HTLC gate exercised: the wait happened and resolved")
    else:
        print("HTLC gate not exercised this run (the HTLCs resolved before the "
              "gate asked); see the glue tests for its outcomes")

    # 3) A swap then completed, via a provider other than the first one tried --
    #    so the failover delivered. On the provider, not the rung index, for the
    #    reason given in the first test.
    first_tried = _attempted_providers()[0]
    assert _wait_until(
        lambda: any((_swap_action_npub(a) or "")[:20] not in ("", first_tried)
                    for a in _swap_actions()),
        rig=rig, timeout=480), \
        (f"no swap completed via a provider other than {first_tried!r}; swap "
         f"actions were: {[a.get('detail') for a in _swap_actions()]}")

    # 3b) The funding waiter Electrum orphans for the dead attempt was cancelled
    #     rather than left polling at 10Hz for the life of the daemon. Asserted
    #     against a REAL Electrum here, because the reaper identifies the task by
    #     matching a closure inside third-party code -- the unit tests fake that
    #     shape faithfully, but only this run proves the shape is still real.
    assert "reaped" in _client_log_text() and \
        "orphaned funding waiter" in _client_log_text(), \
        ("no funding waiter was reaped; Electrum's reverse_swap has most likely "
         "changed shape and the match silently stopped firing")
    assert "may have changed shape" not in _client_log_text(), \
        "the reaper ran but matched nothing -- upstream shape has changed"

    # 3c) Every swap the cascade created is ON DISK, not merely in memory.
    #
    #     add_reverse_swap stores the privkey, preimage and redeem script -- all of
    #     what is needed to ever claim the funding output -- into the
    #     `submarine_swaps` StoredDict. A StoredDict only reaches the wallet file
    #     on an explicit db.write(), and the LAST one Electrum performs is inside
    #     wallet.stop(). So a swap created after that point would exist only in
    #     memory, and a process ending there could never claim it. The executor
    #     therefore flushes the moment a swap object exists.
    #
    #     Asserted against the real file (not the running daemon's memory) and
    #     against a real StoredDict + JsonDB, because that is the part the unit
    #     tests fake.
    on_disk = _wallet_db().get("submarine_swaps", {}) or {}
    assert on_disk, \
        ("no swap reached the wallet file; the flush after add_reverse_swap is "
         "not firing, and a swap created during shutdown would be unclaimable")
    assert any(s.get("privkey") and s.get("preimage") for s in on_disk.values()), \
        f"a swap was persisted without its claim secrets: {list(on_disk)}"

    # 3d) The shutdown gate is inert while nothing is closing. It is checked
    #     between every rung and every provider, so a gate that misfired would
    #     silently truncate ordinary cascades -- the same symptom it exists to
    #     fix, from the opposite direction.
    assert "stopping the swap cascade" not in _client_log_text(), \
        "the shutdown gate fired on a wallet that is not closing"

    # 3e) The send-freeze gate is inert on channels Electrum is perfectly happy to
    #     send over.
    #
    #     Asserted here because this is the gate's real regression risk. It reads
    #     Channel.is_frozen_for_sending, which under trampoline routing (what these
    #     Electrum-to-Electrum channels use) is True not only for a user freeze but
    #     for any peer without trampoline support. If that read were wrong -- or if
    #     the rig's peers stopped advertising trampoline forwarding -- the plugin
    #     would decline EVERY channel and quietly stop draining anything, which no
    #     amount of faked channel objects in the glue tests would catch.
    #
    #     The positive cases (both decline reasons, and the defensive read) are
    #     covered against the real build_snapshot in tests/test_build_snapshot_glue.py;
    #     there is no CLI to freeze a channel, so they cannot be driven from here.
    log_text = _client_log_text()
    for phrase in ("frozen it for sending", "does not advertise trampoline routing"):
        assert phrase not in log_text, \
            (f"the send-freeze gate declined a usable rig channel ({phrase!r}); "
             f"the plugin would drain nothing at all in this state")

    # 4) Both sides end up faulted, from two different places. The executor
    #    charges the PROVIDER as it fails over (softly -- attribution is
    #    ambiguous), while the PEER's share is left to _reconcile_pending_swaps,
    #    which recognises the same swap's failed payment on a later tick. Charging
    #    the peer in both places would count one failure twice, so the peer fault
    #    is expected to arrive second rather than immediately.
    assert any(int(s.get("fault_count", 0)) > 0 for s in _reliability().values()), \
        f"the unpayable provider collected no reliability fault: {_reliability()}"
    assert _wait_until(
        lambda: any(int(s.get("fault_count", 0)) > 0
                    for s in (_wallet_db().get(
                        "inbound_liquidity_peer_reliability", {}) or {}).values()),
        rig=rig, timeout=240), \
        ("reconciliation never charged the channel peer for the failed payment: "
         f"{_wallet_db().get('inbound_liquidity_peer_reliability', {})}")

    # 5) A real swap: inbound liquidity actually increased.
    assert _wait_until(lambda: _max_inbound() > baseline_inbound + 100_000,
                       rig=rig, timeout=300), \
        f"inbound liquidity did not increase (max remote_balance={_max_inbound()})"


def test_an_unfunded_swap_is_not_recorded_as_a_completed_action(rig):
    """A swap that produced no on-chain funding must never appear in Actions.

    It used to: the "possibly committed" branch fell through into the same
    ``_log_action`` the funded path uses, so a swap whose Lightning payment had just
    failed was written to the decision log as a completed drain with
    ``funding txid None`` in its detail. In the Actions view -- where the user looks
    to see whether anything worked -- that is indistinguishable from a swap that
    did work, which is precisely the confusion that made "no swaps are completing"
    hard to see.
    """
    # This test is built on a swap whose payment fails, so the topology that
    # guarantees one is checked up front -- otherwise a reverted rig shows up as
    # a 600s timeout on the wait below rather than as the topology error it is.
    chans = _channels()
    _assert_no_client_channel_to_partner2(rig, chans)
    _assert_partner2_can_host_every_channel(rig, chans)

    _arm_swap_config()
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(
        lambda: "the Lightning payment failed (no funds committed)"
                in _client_log_text(), rig=rig, timeout=600), \
        "no swap failed its Lightning payment, so there is nothing to check"

    # Every swap ACTION carries a real funding txid. None of them says "None".
    for action in _swap_actions():
        detail = action.get("detail", "")
        assert "funding txid None" not in detail, \
            f"an unfunded swap was logged as a completed action: {detail!r}"
        assert re.search(r"funding txid [0-9a-f]{64}", detail), \
            f"a swap action carries no real funding txid: {detail!r}"

    # ... and the failure is still recorded somewhere the user can see. WHICH view
    # depends on the arm, and the distinction is deliberate:
    #
    #   * a failed Lightning PAYMENT (this test's trigger) charges the provider a
    #     soft reliability fault and moves on -- Faults, not Declines, because the
    #     thing worth recording is the evidence about that provider;
    #   * a swap that may have COMMITTED funds stops the cascade and writes a
    #     "did not complete" decline, because there the useful fact is that the
    #     channel is being left alone.
    #
    # So this asserts the fault, not a decline. An earlier version of this test
    # demanded the decline and failed here against a real rig -- the assertion, not
    # the behaviour, was wrong.
    assert _wait_until(
        lambda: any(int(s.get("fault_count", 0)) > 0
                    for s in _reliability().values()),
        rig=rig, timeout=240), \
        (f"a swap failed its payment without being recorded anywhere the user can "
         f"see: reliability={_reliability()}")

    # And nothing in the decision log claims this channel was drained.
    for entry in _decision_log():
        if entry.get("category") == "action" and entry.get("kind") == "swap":
            assert "funding txid None" not in (entry.get("detail") or ""), \
                f"an unfunded swap reached the Actions view: {entry}"
