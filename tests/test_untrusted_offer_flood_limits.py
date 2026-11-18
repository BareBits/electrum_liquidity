"""Bounds on what a flood of nostr offers can cost us.

Publishing a nostr swap offer is free and unpermissioned: anyone can sign as many
as they like, under as many fresh identities as they like. Every per-offer step
the plugin took therefore scaled with whatever an attacker chose to publish, and
three of those steps were worse than merely slow:

  * each malformed offer from a distinct identity called
    ``_record_provider_fault``, and each of those is a FULL WALLET-FILE WRITE --
    over a dict that grew with every one, so N identities cost O(N^2) disk I/O,
    performed while the per-wallet evaluation lock was held;
  * each wrote a row to the decision log, which is a ring pruned to
    ``MAX_LOG_ENTRIES``. Enough of them evicts every genuine entry -- the opens,
    swaps and closes an operator audits their money with -- from the only record
    of them;
  * each left a reliability row in the wallet file that ``_prune_reliability_store``
    only removes after 90 days of idleness.

Covered here:
  * discovery is capped, and the cap keeps the offers hardest to manufacture
    (providers already known to this wallet, then highest announcement PoW);
  * capping is never silent;
  * the whole pass costs ONE reliability write however many identities flood it;
  * the reliability store has a row ceiling, and evicting prefers to forget the
    uninformative rows rather than a penalised provider's record;
  * the decision log cannot be flushed by a fault flood, and a real fault from a
    quiet party is still logged;
  * the diagnostic log has a byte ceiling, and says so when it stops.

Skipped outside the Electrum venv.
"""
from __future__ import annotations

import datetime
import logging
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    FAULT_LOG_MIN_INTERVAL_SEC,
    MAX_DISCOVERED_PROVIDERS,
    MAX_LOG_ENTRIES,
    RELIABILITY_DB_KEY,
    RELIABILITY_MAX_ROWS,
)
from electrum.plugins.inbound_liquidity.diag_log import DiagLog  # type: ignore  # noqa: E402

from test_dev_fee_glue import _FakeWallet, _plugin  # type: ignore  # noqa: E402


def _npub(i: int) -> str:
    """A distinct, well-formed npub per index -- a flooder's fresh identity."""
    return "npub1" + f"{i:058d}".replace("0", "q")[:58].ljust(58, "q")


def _offer(npub: str, *, pct=0.3, mining=0, lo=200_000, mx=1_000_000, pow_bits=0):
    return SimpleNamespace(
        server_npub=npub, pow_bits=pow_bits,
        pairs=SimpleNamespace(percentage=pct, mining_fee=mining,
                              min_amount=lo, max_forward=mx))


def _transport(offers):
    return SimpleNamespace(get_recent_offers=lambda: list(offers))


# --- discovery cap --------------------------------------------------------
def test_discovery_is_capped() -> None:
    p, w = _plugin(), _FakeWallet(None)
    flood = [_offer(_npub(i)) for i in range(MAX_DISCOVERED_PROVIDERS * 3)]
    out = p._offers_from_transport(w, _transport(flood))
    assert len(out) == MAX_DISCOVERED_PROVIDERS


def test_the_cap_keeps_providers_this_wallet_already_knows() -> None:
    """A flooder's identities have no history and cannot get any without being
    selected and transacted with, so known providers are kept first."""
    p, w = _plugin(), _FakeWallet(None)
    known = _npub(9001)
    w.db.put(RELIABILITY_DB_KEY, {known: {"success_count": 3,
                                          "last_success_ts": time.time()}})
    # The known provider is LAST on the wire, so arrival order cannot explain it
    # surviving.
    flood = [_offer(_npub(i)) for i in range(MAX_DISCOVERED_PROVIDERS * 2)]
    flood.append(_offer(known))
    out = p._offers_from_transport(w, _transport(flood))
    assert known in [o.npub for o in out], "a known provider was crowded out"


def test_the_cap_prefers_higher_announcement_proof_of_work() -> None:
    """PoW is the one thing in an offer that costs an attacker real work per
    identity, so among unknown providers it decides who survives."""
    p, w = _plugin(), _FakeWallet(None)
    cheap = [_offer(_npub(i), pow_bits=0)
             for i in range(MAX_DISCOVERED_PROVIDERS * 2)]
    costly = _offer(_npub(7777), pow_bits=24)
    out = p._offers_from_transport(w, _transport(cheap + [costly]))
    assert _npub(7777) in [o.npub for o in out]


def test_an_uncapped_pass_is_left_exactly_as_it_was() -> None:
    """The ordinary case -- a handful of honest providers -- must be untouched,
    order included, so the engine's own ranking stays the only thing ordering
    providers."""
    p, w = _plugin(), _FakeWallet(None)
    offers = [_offer(_npub(i)) for i in range(5)]
    out = p._offers_from_transport(w, _transport(offers))
    assert [o.npub for o in out] == [_npub(i) for i in range(5)]


def test_capping_is_not_silent(caplog) -> None:
    """A capped pass means the cheapest provider on the network may not have been
    considered. That has to be visible rather than inferred from a dear swap."""
    p, w = _plugin(), _FakeWallet(None)
    p._diag_event = lambda wallet, **kw: diags.append(kw)  # type: ignore[assignment]
    diags: list = []
    flood = [_offer(_npub(i)) for i in range(MAX_DISCOVERED_PROVIDERS + 10)]
    with caplog.at_level(logging.WARNING):
        p._offers_from_transport(w, _transport(flood))
    assert any("more than" in r.message for r in caplog.records)
    assert any(d.get("kind") == "providers" for d in diags)


# --- one write per pass ---------------------------------------------------
def test_the_cost_of_a_malformed_offer_flood_does_not_scale_with_it() -> None:
    """The O(N^2) disk bug.

    Each ``_record_provider_fault`` is a whole-wallet-file write, and so is each
    decision-log append, so N malformed offers from N identities used to cost
    ~2N writes over structures that grew with every one. Both are now batched:
    the pass costs a fixed, small number of writes whatever N is, which is the
    property asserted here -- twice the flood must not mean twice the writes.
    """
    def _writes_for(count: int) -> int:
        p, w = _plugin(), _FakeWallet(None)
        # All malformed (a negative fee would otherwise look free), all distinct.
        flood = [_offer(_npub(i), pct=-5.0) for i in range(count)]
        before = w.saved
        out = p._offers_from_transport(w, _transport(flood))
        assert out == []                              # none of them is usable
        # Every identity is still recorded, so nothing is lost by batching.
        assert len(p._load_reliability(w)) == count
        assert all(row["fault_count"] == 1
                   for row in p._load_reliability(w).values())
        return w.saved - before

    small, large = _writes_for(60), _writes_for(120)
    assert small == large, (
        f"the write count scales with the flood ({small} -> {large})")
    # One for the reliability store, one for the decision log.
    assert large <= 2, f"{large} wallet-file writes for one discovery pass"


def test_batched_soft_faults_do_not_escalate() -> None:
    """A soft fault floors the penalty at one decaying level and never compounds
    -- the batch path must keep that, or a flooder could drive a provider's score
    arbitrarily by repeating malformed offers under its identity."""
    p, w = _plugin(), _FakeWallet(None)
    npub = _npub(1)
    for _ in range(5):
        p._record_provider_faults_bulk(w, [npub], "advertised malformed offer")
    row = p._load_reliability(w)[npub]
    assert row["consecutive_faults"] == 1, "a soft fault escalated"
    assert row["fault_count"] == 5, "but every occurrence is still counted"


# --- reliability row ceiling ---------------------------------------------
def test_the_reliability_store_has_a_row_ceiling() -> None:
    p, w = _plugin(), _FakeWallet(None)
    data = {_npub(i): {"fault_count": 1, "last_fault_ts": time.time()}
            for i in range(RELIABILITY_MAX_ROWS * 2)}
    p._save_reliability(w, data)
    assert len(p._load_reliability(w)) == RELIABILITY_MAX_ROWS


def test_the_ceiling_forgets_the_uninformative_rows_first() -> None:
    """Dropping a row forgets a penalty, so a provider we have actually
    transacted with -- or that owes us a bad record -- must be the last thing
    evicted. Otherwise a flooder could wash out its own history."""
    p, w = _plugin(), _FakeWallet(None)
    now = time.time()
    succeeded = "npub1" + "s" * 58
    penalised = "npub1" + "f" * 58
    data = {_npub(i): {"last_attempt_ts": now} for i in range(RELIABILITY_MAX_ROWS * 2)}
    data[succeeded] = {"success_count": 2, "last_success_ts": now}
    data[penalised] = {"fault_count": 4, "consecutive_faults": 3,
                       "last_fault_ts": now}
    p._save_reliability(w, data)
    kept = p._load_reliability(w)
    assert len(kept) == RELIABILITY_MAX_ROWS
    assert succeeded in kept, "a provider we succeeded with was forgotten"
    assert penalised in kept, "a penalised provider's record was forgotten"


# --- the decision log cannot be flushed ----------------------------------
def test_a_fault_flood_cannot_evict_the_action_log() -> None:
    """The log is the record an operator audits with. A party that can provoke
    faults at will must not be able to push a real action out of it."""
    p, w = _plugin(), _FakeWallet(None)
    p._log_action(w, kind="swap", amount_sat=400_000, source="1x1x1",
                  dest="on-chain", reason="drain", detail="the real one",
                  state=None)
    for i in range(MAX_LOG_ENTRIES * 2):
        p._log_fault(w, kind="provider", ident=_npub(i),
                     reason="advertised malformed offer", hard=False, soft=True)
    actions = p.get_decision_log(w, "action")
    assert any(e.get("detail") == "the real one" for e in actions), (
        "a flood of faults evicted a genuine action from the log")


def test_one_party_is_logged_once_per_window_but_others_are_not_muted() -> None:
    """Throttling is per identity on purpose: a single noisy provider must not be
    able to silence the log for everybody else, which a global throttle would let
    it do."""
    p, w = _plugin(), _FakeWallet(None)
    noisy = _npub(1)
    for _ in range(50):
        p._log_fault(w, kind="provider", ident=noisy, reason="noise",
                     hard=False, soft=True)
    faults = p.get_decision_log(w, "fault")
    assert len(faults) == 1, f"{len(faults)} rows for one party in one window"

    # A different party is still heard immediately.
    p._log_fault(w, kind="provider", ident=_npub(2), reason="a real fault",
                 hard=False, soft=True)
    assert len(p.get_decision_log(w, "fault")) == 2


def test_a_faults_row_is_logged_again_once_the_window_passes() -> None:
    """Suppression is a throttle, not a latch: a recurrence after the window is
    news and must be recorded."""
    p, w = _plugin(), _FakeWallet(None)
    npub = _npub(3)
    p._log_fault(w, kind="provider", ident=npub, reason="first",
                 hard=False, soft=True)
    stamps = p._per_wallet_store("_fault_log_stamps")[w]
    # Age the stamp past the window rather than sleeping through it.
    for key in list(stamps):
        stamps[key] -= FAULT_LOG_MIN_INTERVAL_SEC + 1
    p._log_fault(w, kind="provider", ident=npub, reason="again",
                 hard=False, soft=True)
    assert len(p.get_decision_log(w, "fault")) == 2


# --- the diagnostic log has a byte ceiling -------------------------------
def test_the_diag_log_stops_at_its_byte_ceiling(tmp_path) -> None:
    """Retention is by DAY, which bounds how many files exist but not how big one
    gets -- and the plugin writes a diagnostic line on every error path, including
    ones an outside party can provoke."""
    log = DiagLog(str(tmp_path), retention_days=30, max_day_file_bytes=2000)
    ts = time.time()
    written = 0
    for i in range(500):
        if log.write("w", {"ts": ts, "category": "error", "kind": "swap",
                           "reason": f"entry {i}", "detail": "x" * 100}):
            written += 1
    assert 0 < written < 500, f"the cap did not engage (wrote {written})"

    day = datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc).date()
    path = tmp_path / "w" / f"inbound_liquidity-{day.isoformat()}.log"
    size = path.stat().st_size
    # The notice line may carry it slightly over; the point is that it is bounded
    # rather than growing with the 500 attempts.
    assert size < 2000 * 2, f"the day file reached {size} bytes"


def test_the_diag_log_says_once_that_it_stopped(tmp_path) -> None:
    """A log that silently stops is indistinguishable from a plugin that silently
    stopped, so the gap is recorded explicitly -- and only once."""
    log = DiagLog(str(tmp_path), retention_days=30, max_day_file_bytes=1200)
    ts = time.time()
    for i in range(200):
        log.write("w", {"ts": ts, "category": "error", "kind": "swap",
                        "reason": f"entry {i}", "detail": "y" * 100})
    day = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date()
    entries = log.read_day("w", day)
    notices = [e for e in entries if e.get("kind") == "diag_log"]
    assert len(notices) == 1, f"{len(notices)} size-limit notices"
    assert "size limit" in notices[0]["reason"]


def test_the_diag_log_keeps_the_earliest_entries(tmp_path) -> None:
    """Which end to drop is a real choice. The entries worth keeping are the
    EARLIEST ones -- the beginning of whatever went wrong -- so a runaway day
    loses its repetitive tail, not its diagnosis."""
    log = DiagLog(str(tmp_path), retention_days=30, max_day_file_bytes=1500)
    ts = time.time()
    for i in range(200):
        log.write("w", {"ts": ts, "category": "error", "kind": "swap",
                        "reason": f"entry {i}", "detail": "z" * 100})
    day = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date()
    reasons = [e.get("reason") for e in log.read_day("w", day)]
    assert "entry 0" in reasons, "the first entry was lost"
    assert "entry 199" not in reasons, "the cap did not drop the tail"
