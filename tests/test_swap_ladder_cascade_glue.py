"""Glue-level tests for walking the engine's SWAP LADDER.

The cascade no longer walks providers with sizes nested inside them. The engine
(``rank_swap_rungs``) hands over ONE flat list of (provider, size) rungs ordered
by cost per sat of inbound liquidity, and the executor walks it until something
commits. Consequences this file pins down:

  * a provider appears MORE THAN ONCE in the list, at different sizes, and a
    cheap provider's smaller rung can sit ahead of a dearer provider's full size
    -- so "which provider is this" and "which position is this" are no longer the
    same question;
  * the soft fault for a failed Lightning payment belongs to the PROVIDER, so it
    must be charged once per cascade however many rungs that provider has. The
    old code got this for free from the nesting (rungs were contiguous); with an
    interleaved list it has to be tracked explicitly, and getting it wrong would
    demote the CHEAPEST providers hardest, since those are the ones the ladder
    tries most often;
  * the walk stops at the first commit. There is no "carry on for the remainder"
    pass -- a partially drained channel is just a channel whose next evaluation
    sees a smaller balance and re-plans from a fresh snapshot.

Heavy Electrum objects are faked; skipped outside the Electrum venv. The harness
is shared with test_swap_provider_failover_glue rather than re-stated.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.submarine_swaps import SwapServerError  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderAttempt,
    ReverseSwapAction,
)

from test_swap_provider_failover_glue import (  # type: ignore  # noqa: E402
    A, B, C,
    _SM,
    _accept_without_funding,
    _plugin,
    _transport,
    _wallet,
)

PAYMENT_FAILED = "reverse-swap Lightning payment failed"


class _SizedSM(_SM):
    """``_SM`` keyed by (npub, amount) instead of by npub alone.

    The ladder's whole point is that the same provider behaves differently at
    different sizes, which the npub-keyed outcomes map cannot express.
    """

    def __init__(self, sized_outcomes: Dict[Tuple[str, int], object]) -> None:
        super().__init__({})
        self._sized = sized_outcomes

    async def reverse_swap(self, **kw):
        amount = kw["lightning_amount_sat"]
        npub = self._current
        self.attempts.append((npub, amount))
        outcome = self._sized.get((npub, amount), "txid-" + npub)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome(self, **kw)
        return outcome


def _ladder_action(rungs: Sequence[Tuple[str, int]],
                   planned: Optional[int] = None) -> ReverseSwapAction:
    """An action whose head is ``rungs[0]`` and whose alternates are the rest.

    ``planned`` defaults to the head's amount, matching the engine: the head of a
    cost-ordered ladder is always its largest viable rung.
    """
    (head_npub, head_amount), rest = rungs[0], rungs[1:]
    return ReverseSwapAction(
        channel_id="aa" * 32, short_id="1x1x1",
        lightning_amount_sat=planned if planned is not None else head_amount,
        reason="drain", provider_npub=head_npub,
        alternates=tuple(ProviderAttempt(npub=n, amount_sat=a,
                                         all_in_cost_pct=0.4)
                         for n, a in rest))


def _run(p, sm, action, ln_payment: str = "failed", transport=None) -> None:
    npubs = [action.provider_npub] + [a.npub for a in action.alternates]
    asyncio.run(p._reverse_swap(_wallet(sm, ln_payment), action, state={},
                                transport=transport or _transport(npubs)))


def _payment_faults(p) -> List[str]:
    return [n for n, reason, _kw in p.faults if reason == PAYMENT_FAILED]


# --- walking the flat list ------------------------------------------------
def test_cascade_walks_the_flat_ladder_in_the_order_given() -> None:
    """The executor imposes no ordering of its own: the engine already sorted by
    cost per sat, and the walk is that list, verbatim."""
    rungs = [(A, 900_000), (A, 450_000), (B, 900_000), (B, 450_000)]
    sm = _SizedSM({r: _accept_without_funding for r in rungs})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert sm.attempts == rungs


def test_a_cheap_providers_small_rung_is_tried_before_a_dear_providers_full_size() -> None:
    """The interleaving the ladder exists for. A is cheaper per sat even at half
    size, so its small rung must run before B is touched at all -- which the old
    providers-outer walk could not express."""
    rungs = [(A, 900_000), (A, 450_000), (B, 900_000)]
    sm = _SizedSM({(A, 900_000): _accept_without_funding,
                   (A, 450_000): _accept_without_funding})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert sm.attempts == [(A, 900_000), (A, 450_000), (B, 900_000)]
    assert p.successes == [B]


def test_a_smaller_rung_of_the_same_provider_can_be_the_one_that_works() -> None:
    """The mainnet failure mode, end to end: the provider was fine, the amount was
    not routable. Nothing below the winning rung is attempted."""
    rungs = [(A, 900_000), (A, 450_000), (B, 900_000)]
    sm = _SizedSM({(A, 900_000): _accept_without_funding})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert sm.attempts == [(A, 900_000), (A, 450_000)]
    assert p.successes == [A]
    assert B not in [n for n, _a in sm.attempts]


def test_the_walk_stops_at_the_first_commit_and_does_not_continue_the_ladder() -> None:
    """No "start again for the remainder" pass. A partial drain is picked up by
    the next evaluation from a fresh snapshot, never by continuing down a ladder
    that was priced against a balance which has since changed."""
    rungs = [(A, 900_000), (A, 450_000), (A, 270_000), (B, 900_000)]
    sm = _SizedSM({(A, 900_000): _accept_without_funding})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    # Committed at 450k -- which is a PARTIAL drain of the 900k the engine
    # planned -- and the cascade still ends there.
    assert sm.attempts == [(A, 900_000), (A, 450_000)]
    assert p.successes == [A]


# --- one fault per provider, however many rungs ---------------------------
def test_a_provider_is_faulted_once_however_many_rungs_it_has() -> None:
    """Three rungs of one provider failing is ONE observation about that provider.

    Charging per rung would sink it at triple rate -- and would do so hardest to
    the cheapest providers, which are exactly the ones the ladder gives the most
    rungs to. That would invert the ranking over time.
    """
    rungs = [(A, 900_000), (A, 450_000), (A, 270_000), (B, 900_000)]
    sm = _SizedSM({(A, a): _accept_without_funding
                   for a in (900_000, 450_000, 270_000)})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert sm.attempts == rungs
    assert _payment_faults(p) == [A]
    # The per-rung evidence is still recorded -- it is the ranking that is
    # deduplicated, not the diagnostics.
    assert len([d for d in p.diags if d.get("reason") == PAYMENT_FAILED]) == 3


def test_interleaved_providers_are_each_faulted_exactly_once() -> None:
    """The case the old contiguous-rungs assumption cannot handle: A, B, A, B."""
    rungs = [(A, 900_000), (B, 900_000), (A, 450_000), (B, 450_000), (C, 900_000)]
    sm = _SizedSM({r: _accept_without_funding for r in rungs[:4]})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert sm.attempts == rungs
    assert sorted(_payment_faults(p)) == [A, B]
    assert p.successes == [C]


def test_a_provider_faulted_on_an_early_rung_keeps_its_later_rungs() -> None:
    """A soft fault is a ranking signal for the NEXT cycle, not an in-cascade
    exclusion. Dropping the remaining rungs would discard vetted, cheaper-per-sat
    swaps on the strength of a failure the ladder exists to work around."""
    rungs = [(A, 900_000), (A, 450_000), (B, 900_000)]
    sm = _SizedSM({(A, 900_000): _accept_without_funding})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert (A, 450_000) in sm.attempts
    assert p.successes == [A]


def test_non_payment_failures_still_do_not_earn_a_payment_fault() -> None:
    """Only the payment-failed arm feeds this fault. A provider that rejected the
    swap outright is faulted through its own path, and must not also be charged
    for a Lightning payment that never happened."""
    rungs = [(A, 900_000), (A, 450_000), (B, 900_000)]
    sm = _SizedSM({(A, 900_000): SwapServerError(),
                   (A, 450_000): SwapServerError()})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    assert _payment_faults(p) == []


# --- labelling ------------------------------------------------------------
def test_rungs_below_the_planned_amount_are_labelled_reduced() -> None:
    """"Reduced" is relative to what the engine PLANNED for the channel, not to
    the rung's own size -- under the ladder every rung is its own size, so the
    old self-comparison could never fire and the label would have vanished."""
    rungs = [(A, 900_000), (A, 450_000), (B, 900_000)]
    sm = _SizedSM({r: _accept_without_funding for r in rungs})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs))
    diags = [d for d in p.diags if d.get("reason") == PAYMENT_FAILED]
    assert "reduced size" not in diags[0]["detail"]      # the planned 900k
    assert "reduced size" in diags[1]["detail"]          # 450k
    assert "reduced size" not in diags[2]["detail"]      # B's full 900k


# --- interaction with the rest of the cascade -----------------------------
def test_a_vanished_provider_loses_every_one_of_its_rungs() -> None:
    """Resolution is per rung against the live offer book, so a provider that
    stopped advertising costs no attempt slots at all -- not one per ladder step."""
    rungs = [(A, 900_000), (A, 450_000), (A, 270_000), (B, 900_000)]
    sm = _SizedSM({})
    p = _plugin()
    _run(p, sm, _ladder_action(rungs), transport=_transport([B]))
    assert sm.attempts == [(B, 900_000)]
    assert p.successes == [B]


def test_deadline_truncates_the_ladder_and_says_how_many_rungs_were_left() -> None:
    """The only bound on the walk is wall-clock, so the message it leaves behind
    has to be in rungs -- the unit the list is actually in."""
    from contextlib import nullcontext

    from electrum.plugins.inbound_liquidity import _CascadeFaults  # type: ignore

    async def _slow(sm, **kw):
        await asyncio.sleep(0.05)
        raise SwapServerError()

    rungs = [(A, 900_000), (A, 450_000), (B, 900_000), (B, 450_000)]
    sm = _SizedSM({r: _slow for r in rungs})
    p = _plugin()
    action = _ladder_action(rungs)
    tr = _transport([A, B])
    attempts = [(ProviderAttempt(npub=n, amount_sat=a, all_in_cost_pct=0.4),
                 tr.get_offer(n)) for n, a in rungs]
    asyncio.run(p._run_swap_cascade(
        _wallet(sm), action, attempts, nullcontext(tr), {},
        time.monotonic() + 0.01, len(attempts), _CascadeFaults(), deadline_sec=1.0))
    assert len(sm.attempts) == 1                     # only the in-flight one ran
    hit = [d for d in p.diags
           if d.get("reason") == "swap provider cascade deadline reached"]
    assert len(hit) == 1
    assert "3 rung(s) untried" in hit[0]["detail"]


def test_hand_built_attempts_with_a_reduced_rung_still_expand() -> None:
    """Backwards compatibility for callers predating the ladder: an attempt that
    still carries ``reduced_amount_sat`` is expanded as before. The engine no
    longer produces these, so in production every entry yields exactly one rung --
    which is what stops a 0.5x ladder rung from sprouting its own 0.45x retry."""
    action = ReverseSwapAction(
        channel_id="aa" * 32, short_id="1x1x1", lightning_amount_sat=900_000,
        reason="drain", provider_npub=A,
        reduced_amount_sat=810_000, reduced_all_in_cost_pct=0.45)
    sm = _SizedSM({(A, 900_000): _accept_without_funding})
    p = _plugin()
    _run(p, sm, action)
    assert sm.attempts == [(A, 900_000), (A, 810_000)]
    # The 810k rung succeeded, but the 900k one's payment really did fail, so the
    # provider is still charged once for it. Pre-existing semantics, preserved:
    # the fault records what was observed, and the success is recorded too.
    assert _payment_faults(p) == [A]
    assert p.successes == [A]


def test_engine_built_ladder_rungs_never_sprout_sub_rungs() -> None:
    """The guard the above implies: every engine rung has reduced_amount_sat
    unset, so the ladder is exactly as long as the engine priced it."""
    rungs = [(A, 900_000), (A, 450_000)]
    action = _ladder_action(rungs)
    assert action.reduced_amount_sat is None
    assert all(a.reduced_amount_sat is None for a in action.alternates)
    sm = _SizedSM({r: _accept_without_funding for r in rungs})
    p = _plugin()
    _run(p, sm, action)
    assert sm.attempts == rungs          # two rungs, not four


# --- the user's minimum swap size, config -> engine -----------------------
# The floor itself is engine behaviour (test_swap_provider_failover_engine);
# what is checked here is that the setting reaches it, and that a wallet whose
# config predates the setting -- or has been hand-edited into nonsense -- still
# gets the shipped floor rather than an exception on every tick.
def _cfg_plugin(**overrides):
    from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore

    from test_liquidity_goal_glue import _plugin as _goal_plugin  # type: ignore
    p = _goal_plugin(**overrides)
    assert isinstance(p, LiquidityPlugin)
    return p


def test_read_config_passes_the_minimum_swap_size_through() -> None:
    p = _cfg_plugin(INBOUND_LIQUIDITY_MIN_SWAP_SAT=60_000)
    assert p.read_config().min_swap_sat == 60_000


def test_minimum_swap_size_is_clamped_non_negative() -> None:
    p = _cfg_plugin(INBOUND_LIQUIDITY_MIN_SWAP_SAT=-5)
    assert p.read_config().min_swap_sat == 0


def test_garbage_minimum_swap_size_falls_back_to_the_shipped_default() -> None:
    from electrum.plugins.inbound_liquidity import DEFAULT_MIN_SWAP_SAT  # type: ignore
    p = _cfg_plugin(INBOUND_LIQUIDITY_MIN_SWAP_SAT="not-a-number")
    assert p.read_config().min_swap_sat == DEFAULT_MIN_SWAP_SAT


def test_a_config_predating_the_setting_still_gets_the_floor() -> None:
    """An older wallet has no such ConfigVar. It must get the shipped floor, not
    zero -- the floor is a rule that should apply whether or not the user has
    ever seen the setting."""
    from electrum.plugins.inbound_liquidity import DEFAULT_MIN_SWAP_SAT  # type: ignore
    p = _cfg_plugin()
    if hasattr(p.config, "INBOUND_LIQUIDITY_MIN_SWAP_SAT"):
        del p.config.INBOUND_LIQUIDITY_MIN_SWAP_SAT
    assert p.read_config().min_swap_sat == DEFAULT_MIN_SWAP_SAT


def test_the_shipped_configvar_default_is_25000() -> None:
    from electrum.simple_config import SimpleConfig  # type: ignore
    assert SimpleConfig.INBOUND_LIQUIDITY_MIN_SWAP_SAT.get_default_value() == 25_000
