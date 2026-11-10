"""The Providers tab's Attempts tally.

"Successes" has always answered "how many swaps did this provider deliver". It
could not answer the question next to it -- how many times did we turn to this
provider at all -- and without that, a provider with 0 successes and 0 faults is
indistinguishable from one we have never tried.

The counting rule is once per provider per CASCADE, not once per attempt made:
the size ladder tries one provider at several amounts, and turning to it once is
one attempt however many amounts follow. That mirrors how a payment-failure
fault is already charged, and is the only reading under which Successes <=
Attempts holds.

Also pinned: the tally is display-only (it is not a verdict and must not reach
the ranking), it survives the idle-row pruner, and a row written before the field
existed reads 0 rather than breaking.

Shares the cascade scaffolding with test_swap_provider_failover_glue.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Dict

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    RELIABILITY_DB_KEY,
    RELIABILITY_ROW_MAX_IDLE_SEC,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderAttempt,
    ReverseSwapAction,
)
from electrum.util import UserFacingException  # type: ignore  # noqa: E402

from test_swap_provider_failover_glue import (  # noqa: E402
    A, B, C, _SM, _LnWorker, _accept_without_funding, _plugin, _transport,
)


# --- scaffolding ---------------------------------------------------------
class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


def _wallet(sm, ln_payment: str = "failed") -> SimpleNamespace:
    """The failover module's wallet, plus the storage the attempt tally needs:
    its own wallet has no db, so the real recorder has nowhere to write."""
    return SimpleNamespace(lnworker=_LnWorker(sm, ln_payment),
                           db=_FakeDB(), save_db=lambda: None)


def _attempts(p, w) -> Dict[str, int]:
    """npub -> Attempts, as the tab renders it. Providers with no row are absent."""
    return {npub: row["attempt_count"]
            for npub, row in p.provider_reliability_rows(w).items()}


def _action(chosen: str = A, alternates=(), amount: int = 400_000,
            reduced: int = 0) -> ReverseSwapAction:
    return ReverseSwapAction(
        channel_id="aa" * 32, short_id="1x1x1",
        lightning_amount_sat=amount, reason="drain",
        provider_npub=chosen,
        reduced_amount_sat=reduced,
        reduced_all_in_cost_pct=0.4 if reduced else None,
        alternates=tuple(ProviderAttempt(npub=n, amount_sat=amount,
                                         all_in_cost_pct=0.4)
                         for n in alternates))


def _run(p, sm, action, ln_payment: str = "failed") -> None:
    npubs = [action.provider_npub] + [a.npub for a in action.alternates]
    asyncio.run(p._reverse_swap(_wallet(sm, ln_payment), action, state={},
                                transport=_transport(npubs)))


# --- the recorder --------------------------------------------------------
def test_recording_an_attempt_increments_and_stamps() -> None:
    p = _plugin()
    w = _wallet(_SM({}))
    p._record_provider_attempt(w, A)
    p._record_provider_attempt(w, A)
    row = p._load_reliability(w)[A]
    assert row["attempt_count"] == 2
    assert row["last_attempt_ts"] == pytest.approx(time.time(), abs=30)


def test_an_attempt_is_not_a_verdict() -> None:
    # Trying somebody is not evidence against them: nothing the ranking reads
    # may be touched, or merely being cheap enough to try would penalise a
    # provider.
    p = _plugin()
    w = _wallet(_SM({}))
    p._record_provider_attempt(w, A)
    row = p._load_reliability(w)[A]
    assert "consecutive_faults" not in row
    assert "fault_count" not in row
    assert "success_count" not in row
    assert p.provider_reliability_rows(w)[A]["penalty_pct"] == 0.0


def test_an_empty_npub_is_ignored() -> None:
    # Single-provider / URL mode has no per-provider identity to count against.
    p = _plugin()
    w = _wallet(_SM({}))
    p._record_provider_attempt(w, "")
    assert p._load_reliability(w) == {}


def test_a_row_predating_the_field_reads_zero() -> None:
    p = _plugin()
    w = _wallet(_SM({}))
    w.db.put(RELIABILITY_DB_KEY, {A: {"success_count": 3, "fault_count": 1}})
    row = p.provider_reliability_rows(w)[A]
    assert row["attempt_count"] == 0
    assert row["success_count"] == 3


# --- counted once per provider per cascade -------------------------------
def test_a_delivered_swap_counts_one_attempt() -> None:
    p = _plugin()
    sm = _SM({A: "txidA"})
    w = _wallet(sm)
    asyncio.run(p._reverse_swap(w, _action(), state={},
                                transport=_transport([A])))
    assert _attempts(p, w) == {A: 1}


def test_every_provider_the_cascade_turns_to_is_counted() -> None:
    # A declines, B declines, C delivers: three providers tried, one success.
    p = _plugin()
    sm = _SM({A: UserFacingException("uneconomical"),
              B: UserFacingException("uneconomical"),
              C: "txidC"})
    w = _wallet(sm)
    asyncio.run(p._reverse_swap(w, _action(chosen=A, alternates=(B, C)),
                                state={}, transport=_transport([A, B, C])))
    assert _attempts(p, w) == {A: 1, B: 1, C: 1}
    assert p.successes == [C]


def test_the_size_ladder_does_not_count_a_provider_twice() -> None:
    # The whole point of the chosen rule. A's Lightning payment fails at full
    # size, so it is retried smaller -- two rungs run against ONE provider,
    # which is one occasion on which we turned to it.
    p = _plugin()
    sm = _SM({A: _accept_without_funding})
    w = _wallet(sm)
    asyncio.run(p._reverse_swap(w, _action(chosen=A, amount=400_000,
                                           reduced=360_000),
                                state={}, transport=_transport([A])))
    # Premise: the ladder really did run two rungs against the one provider.
    assert sm.attempts == [(A, 400_000), (A, 360_000)]
    assert _attempts(p, w) == {A: 1}


def test_a_second_cascade_counts_again() -> None:
    # Once per cascade, not once ever: two separate drains of the same channel
    # are two occasions on which we turned to this provider.
    p = _plugin()
    sm = _SM({A: UserFacingException("uneconomical")})
    w = _wallet(sm)
    for _ in range(2):
        p._swap_cooldown_until.clear()      # a fresh cycle, not the same one
        asyncio.run(p._reverse_swap(w, _action(), state={},
                                    transport=_transport([A])))
    assert _attempts(p, w) == {A: 2}


def test_an_unreachable_provider_still_counts_as_tried() -> None:
    # It never advertised terms, so there is no swap and no success -- but we
    # did turn to it, and "0 attempts" would wrongly read as "never considered".
    p = _plugin()
    sm = _SM({B: "txidB"}, )
    sm.is_initialized.clear()               # nobody's terms ever arrive
    w = _wallet(sm)
    asyncio.run(p._reverse_swap(w, _action(chosen=A, alternates=(B,)),
                                state={}, transport=_transport([A, B])))
    assert _attempts(p, w) == {A: 1, B: 1}


def test_successes_never_exceed_attempts() -> None:
    p = _plugin()
    sm = _SM({A: UserFacingException("nope"), B: "txidB"})
    w = _wallet(sm)
    asyncio.run(p._reverse_swap(w, _action(chosen=A, alternates=(B,)),
                                state={}, transport=_transport([A, B])))
    rows = p.provider_reliability_rows(w)
    for npub, row in rows.items():
        assert row["success_count"] <= row["attempt_count"], npub


# --- the pruner ----------------------------------------------------------
def test_an_attempt_keeps_a_row_alive() -> None:
    # Without this, a provider we keep trying but that never cleanly succeeds or
    # faults would have its row pruned and its tally silently reset to 0.
    p = _plugin()
    w = _wallet(_SM({}))
    p._record_provider_attempt(w, A)
    p._prune_reliability_store(w)
    assert _attempts(p, w) == {A: 1}


def test_an_idle_attempt_only_row_is_still_pruned_eventually() -> None:
    # The tally is not a reason to keep a row forever -- only a reason not to
    # drop one that is still in use.
    p = _plugin()
    w = _wallet(_SM({}))
    p._record_provider_attempt(w, A)
    store = p._load_reliability(w)
    store[A]["last_attempt_ts"] = time.time() - RELIABILITY_ROW_MAX_IDLE_SEC - 1
    p._save_reliability(w, store)
    p._prune_reliability_store(w)
    assert p._load_reliability(w) == {}


def test_reset_stats_clears_the_tally() -> None:
    p = _plugin()
    w = _wallet(_SM({}))
    p._record_provider_attempt(w, A)
    p.clear_provider_reliability(w, A)
    assert _attempts(p, w) == {}
