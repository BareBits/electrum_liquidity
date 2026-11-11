"""Glue-level tests for looking up swap providers' display names on nostr.

The lookup is one kind-0 query against the relay session an evaluation already
holds open, cached per wallet so a name survives a restart and still shows for a
provider that has gone offline. It is entirely cosmetic, which is the property
most of these tests are about: nothing it can do -- a silent relay, a hostile
relay, a provider with no profile, a wallet with no db -- may cost the caller
anything but a blank column.

Heavy Electrum objects are faked; skipped if the plugin package can't be
imported (outside the electrum venv).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from conftest import seed_settings  # noqa: E402

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    MAX_PROVIDER_NAME_CHARS,
    PROFILE_FETCH_TIMEOUT_SEC,
    PROVIDER_PROFILE_MAX_ROWS,
    PROVIDER_PROFILE_TTL_SEC,
    PROVIDER_PROFILES_DB_KEY,
    RELIABILITY_DB_KEY,
    RELIABILITY_ROW_MAX_IDLE_SEC,
    LiquidityPlugin,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
)

# Two REAL npubs, with the raw pubkeys they decode to. They have to be real:
# the lookup converts each npub to the hex `authors` a nostr filter takes, and
# drops any that fails bech32 -- so made-up identities would silently test the
# "unusable npub" path instead of the happy one. Round-trip asserted below.
NPUB_A = "npub1zuuajd7u3sx8x7z4yamp2guxprcjug8mvgsxjudxx68pgp7xfh0snv6m4q"
NPUB_B = "npub1xtsczwc652rkwmcknaztql3ak263gwr4s5s4hameanlv796xwy4qh5pqq3"
HEX_A = "1739d937dc8c0c737855277615238608f12e20fb62206971a6368e1407c64ddf"
HEX_B = "32e1813b1aa287676f169f44b07e3db2b514387585215bf779ecfecf1746712a"


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.profiles")
    p._last_offers = {}
    seed_settings(p)
    return p


class _FakeDB:
    """Just enough wallet db for the profile store."""

    def __init__(self) -> None:
        self.data: Dict[str, object] = {}
        self.puts = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def put(self, key, value) -> None:
        self.puts += 1
        self.data[key] = value


def _wallet() -> SimpleNamespace:
    return SimpleNamespace(db=_FakeDB())


class _HashableWallet:
    """A wallet that can key the plugin's per-wallet dicts.

    ``SimpleNamespace`` cannot (it is unhashable), which is fine for the lookup
    itself -- it only touches ``wallet.db`` -- but not for the prune, which
    consults ``discovered_providers`` and so reads a dict keyed by wallet.
    """

    def __init__(self, db) -> None:
        self.db = db


class _Event:
    """A nostr event as aionostr hands it over (signature already verified)."""

    def __init__(self, pubkey: str, content: str, created_at: int = 1_000) -> None:
        self.pubkey = pubkey
        self.content = content
        self.created_at = created_at


def _profile(name: str) -> str:
    return json.dumps({"display_name": name})


class _Relays:
    """A fake relay manager. Records the filters it was asked for and streams
    back the events it was seeded with."""

    def __init__(self, events: List[_Event], *, hang: bool = False,
                 raises: Optional[Exception] = None) -> None:
        self.events = events
        self.hang = hang
        self.raises = raises
        self.filters: List[dict] = []

    async def get_events(self, *filters, **kwargs):
        self.filters.extend(filters)
        if self.raises is not None:
            raise self.raises
        for event in self.events:
            yield event
        if self.hang:
            # A relay that never sends end-of-stored-events: the generator
            # stays open, exactly what PROFILE_FETCH_TIMEOUT_SEC is for.
            await asyncio.sleep(3600)


def _transport(relays: Optional[_Relays], offers=()) -> SimpleNamespace:
    return SimpleNamespace(
        relay_manager=relays,
        get_recent_offers=lambda: list(offers))


class _Offer:
    """A nostr SwapOffer as `_offers_from_transport` reads one."""

    def __init__(self, npub: str) -> None:
        self.server_npub = npub
        self.pow_bits = 0
        self.pairs = SimpleNamespace(percentage=0.5, mining_fee=1_000,
                                     min_amount=20_000, max_forward=500_000)


def _run(coro):
    return asyncio.run(coro)


# --- the fixtures' own premise -------------------------------------------
def test_the_test_npubs_really_decode_to_the_pinned_pubkeys() -> None:
    # If this ever fails, every "found a name" test below has quietly become a
    # test of the malformed-npub path instead.
    p = _plugin()
    assert p._npub_to_hex(NPUB_A) == HEX_A
    assert p._npub_to_hex(NPUB_B) == HEX_B
    assert p._npub_to_hex("npub1thisisnotrealbech32soitwillnotdecode") == ""


# --- the happy path -------------------------------------------------------
def test_a_published_name_is_found_and_cached() -> None:
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"
    assert p.provider_names(w) == {NPUB_A: "Fast Swaps"}


def test_the_query_asks_for_kind_zero_by_author() -> None:
    p, w = _plugin(), _wallet()
    relays = _Relays([])
    _run(p._refresh_provider_profiles(
        w, _transport(relays, [_Offer(NPUB_A), _Offer(NPUB_B)])))
    assert len(relays.filters) == 1
    assert relays.filters[0]["kinds"] == [0]
    assert sorted(relays.filters[0]["authors"]) == sorted([HEX_A, HEX_B])


def test_each_provider_gets_its_own_name() -> None:
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps")),
                      _Event(HEX_B, _profile("Bargain Bin"))])
    _run(p._refresh_provider_profiles(
        w, _transport(relays, [_Offer(NPUB_A), _Offer(NPUB_B)])))
    assert p.provider_names(w) == {NPUB_A: "Fast Swaps", NPUB_B: "Bargain Bin"}


def test_a_provider_with_no_profile_is_recorded_as_looked_up() -> None:
    # The "looked and found nothing" entry is the point: without it every tick
    # would re-query the common no-profile case forever.
    p, w = _plugin(), _wallet()
    _run(p._refresh_provider_profiles(
        w, _transport(_Relays([]), [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == ""
    cached = w.db.get(PROVIDER_PROFILES_DB_KEY)
    assert cached[NPUB_A]["name"] == ""
    assert cached[NPUB_A]["checked_ts"] > 0


# --- the gap-fill / TTL contract -----------------------------------------
def test_a_cached_provider_is_not_queried_again() -> None:
    p, w = _plugin(), _wallet()
    transport = _transport(_Relays([_Event(HEX_A, _profile("Fast Swaps"))]),
                           [_Offer(NPUB_A)])
    _run(p._refresh_provider_profiles(w, transport))
    second = _Relays([])
    _run(p._refresh_provider_profiles(
        w, _transport(second, [_Offer(NPUB_A)])))
    assert second.filters == []                 # no relay traffic at all
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"


def test_only_the_uncached_provider_is_queried() -> None:
    p, w = _plugin(), _wallet()
    _run(p._refresh_provider_profiles(
        w, _transport(_Relays([_Event(HEX_A, _profile("Fast"))]), [_Offer(NPUB_A)])))
    relays = _Relays([])
    _run(p._refresh_provider_profiles(
        w, _transport(relays, [_Offer(NPUB_A), _Offer(NPUB_B)])))
    assert relays.filters[0]["authors"] == [HEX_B]


def test_a_stale_entry_is_re_queried() -> None:
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Old Name",
                 "checked_ts": time.time() - PROVIDER_PROFILE_TTL_SEC - 1}})
    relays = _Relays([_Event(HEX_A, _profile("New Name"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "New Name"


def test_force_re_queries_even_a_fresh_entry() -> None:
    # The Providers tab's Refresh button: the user is explicitly asking.
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Old Name", "checked_ts": time.time()}})
    relays = _Relays([_Event(HEX_A, _profile("New Name"))])
    _run(p._refresh_provider_profiles(
        w, _transport(relays, [_Offer(NPUB_A)]), force=True))
    assert p.provider_name(w, NPUB_A) == "New Name"


def test_a_provider_that_drops_its_profile_loses_its_name_on_a_forced_read() -> None:
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Fast Swaps", "checked_ts": time.time()}})
    _run(p._refresh_provider_profiles(
        w, _transport(_Relays([]), [_Offer(NPUB_A)]), force=True))
    assert p.provider_name(w, NPUB_A) == ""


@pytest.mark.parametrize("checked_ts", [0, -1, "", None, "soon", float("nan")])
def test_an_unusable_timestamp_counts_as_stale(checked_ts) -> None:
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "x", "checked_ts": checked_ts}})
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"


def test_a_future_timestamp_counts_as_stale() -> None:
    # A clock that moved backwards (or a hand-edited wallet) must not pin a row
    # as fresh forever.
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "x", "checked_ts": time.time() + 10 * 86400}})
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"


# --- hostile / broken relays ---------------------------------------------
def test_a_profile_for_an_author_we_did_not_ask_about_is_ignored() -> None:
    # aionostr verifies the signature, so a relay cannot FORGE a profile for
    # another pubkey -- but it can volunteer an unrelated author's real one,
    # and that must not land in a provider's row.
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_B, _profile("Someone Else")),
                      _Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_names(w) == {NPUB_A: "Fast Swaps"}


def test_the_newest_vintage_of_a_replaceable_profile_wins() -> None:
    # kind 0 is replaceable, so several relays may each answer with a different
    # vintage of the same profile.
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_A, _profile("Older"), created_at=100),
                      _Event(HEX_A, _profile("Newest"), created_at=300),
                      _Event(HEX_A, _profile("Middle"), created_at=200)])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "Newest"


def test_an_event_with_an_unusable_timestamp_is_skipped() -> None:
    p, w = _plugin(), _wallet()
    bad = _Event(HEX_A, _profile("Bad"))
    bad.created_at = "not-a-number"
    relays = _Relays([bad, _Event(HEX_A, _profile("Good"), created_at=5)])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "Good"


def test_a_hostile_name_is_sanitised_before_it_is_stored() -> None:
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_A, _profile("Fast\n‮Swaps​"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"


def test_a_stored_name_is_re_sanitised_on_the_way_out() -> None:
    # The cache is a wallet-file field: an older build or a hand edit could
    # hold a value today's bound would reject.
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Evil\nName" + "S" * 500, "checked_ts": time.time()}})
    name = p.provider_name(w, NPUB_A)
    assert "\n" not in name
    assert len(name) <= MAX_PROVIDER_NAME_CHARS
    assert p.provider_names(w)[NPUB_A] == name


def test_a_silent_relay_costs_a_name_and_nothing_else(monkeypatch) -> None:
    # The relay streams one profile then never sends EOSE. The partial result
    # is kept and the call returns on the timeout rather than hanging the tick.
    monkeypatch.setattr(
        "electrum.plugins.inbound_liquidity.PROFILE_FETCH_TIMEOUT_SEC", 0.05)
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))], hang=True)
    started = time.monotonic()
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert time.monotonic() - started < 2.0
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"


def test_a_timeout_with_no_reply_at_all_is_not_cached_as_nameless(monkeypatch) -> None:
    # A relay that answers nothing within the deadline is indistinguishable from
    # one that never answered us. Stamping that would hide every name for a full
    # TTL on the strength of one slow lookup, so nothing is written and the next
    # tick retries.
    monkeypatch.setattr(
        "electrum.plugins.inbound_liquidity.PROFILE_FETCH_TIMEOUT_SEC", 0.05)
    p, w = _plugin(), _wallet()
    relays = _Relays([], hang=True)
    _run(p._refresh_provider_profiles(
        w, _transport(relays, [_Offer(NPUB_A), _Offer(NPUB_B)])))
    assert not w.db.get(PROVIDER_PROFILES_DB_KEY)


def test_a_partial_timeout_keeps_what_did_arrive(monkeypatch) -> None:
    # One provider answered, so the relay was demonstrably talking to us: the
    # silent one really does have no profile, and both are stamped.
    monkeypatch.setattr(
        "electrum.plugins.inbound_liquidity.PROFILE_FETCH_TIMEOUT_SEC", 0.05)
    p, w = _plugin(), _wallet()
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))], hang=True)
    _run(p._refresh_provider_profiles(
        w, _transport(relays, [_Offer(NPUB_A), _Offer(NPUB_B)])))
    cached = w.db.get(PROVIDER_PROFILES_DB_KEY)
    assert cached[NPUB_A]["name"] == "Fast Swaps"
    assert cached[NPUB_B]["name"] == ""


def test_a_relay_that_raises_costs_a_name_and_nothing_else() -> None:
    p, w = _plugin(), _wallet()
    relays = _Relays([], raises=RuntimeError("relay exploded"))
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == ""
    # Nothing was stamped, so the next tick will try again rather than treating
    # a failed lookup as "this provider has no name".
    assert not w.db.get(PROVIDER_PROFILES_DB_KEY)


def test_the_lookup_never_touches_provider_reliability() -> None:
    # The npubs are read straight off the transport rather than through
    # `_offers_from_transport`, which records a soft fault for malformed terms
    # and is already called once per tick by build_snapshot. Routing the name
    # lookup through it too would charge such a provider twice per tick.
    p, w = _plugin(), _wallet()
    faults = []
    p._record_provider_fault = lambda *a, **k: faults.append(a)
    broken = _Offer(NPUB_A)
    broken.pairs = SimpleNamespace(percentage=-5.0, mining_fee="nope",
                                   min_amount=None, max_forward=float("nan"))
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [broken])))
    assert faults == []
    # ...and the provider still gets its name: a rejected offer is still a row
    # on the Providers tab, so it is still worth looking one up for.
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"
    assert not w.db.get(RELIABILITY_DB_KEY)


def test_an_unreadable_offer_list_is_a_no_op() -> None:
    p, w = _plugin(), _wallet()

    def _boom():
        raise RuntimeError("transport is closing")

    transport = SimpleNamespace(relay_manager=_Relays([]),
                                get_recent_offers=_boom)
    _run(p._refresh_provider_profiles(w, transport))
    assert not w.db.get(PROVIDER_PROFILES_DB_KEY)


def test_a_transport_with_no_offer_reader_is_a_no_op() -> None:
    p, w = _plugin(), _wallet()
    transport = SimpleNamespace(relay_manager=_Relays([]))
    _run(p._refresh_provider_profiles(w, transport))
    assert not w.db.get(PROVIDER_PROFILES_DB_KEY)


def test_a_malformed_npub_is_dropped_without_costing_the_others() -> None:
    p, w = _plugin(), _wallet()
    offers = [_Offer(NPUB_A), _Offer("npub1thisisnotrealbech32soitwillnotdecode")]
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, offers)))
    assert relays.filters[0]["authors"] == [HEX_A]
    assert p.provider_name(w, NPUB_A) == "Fast Swaps"


# --- degenerate wiring ----------------------------------------------------
def test_no_transport_is_a_no_op() -> None:
    p, w = _plugin(), _wallet()
    _run(p._refresh_provider_profiles(w, None))
    assert not w.db.get(PROVIDER_PROFILES_DB_KEY)


def test_a_transport_with_no_relay_manager_is_a_no_op() -> None:
    # The URL/HTTP swap transport, or a session whose relays never came up.
    p, w = _plugin(), _wallet()
    _run(p._refresh_provider_profiles(w, _transport(None, [_Offer(NPUB_A)])))
    assert not w.db.get(PROVIDER_PROFILES_DB_KEY)


def test_no_discovered_providers_is_a_no_op() -> None:
    p, w = _plugin(), _wallet()
    relays = _Relays([])
    _run(p._refresh_provider_profiles(w, _transport(relays, [])))
    assert relays.filters == []


def test_a_db_less_wallet_does_not_raise() -> None:
    p = _plugin()
    w = SimpleNamespace(db=None)
    relays = _Relays([_Event(HEX_A, _profile("Fast Swaps"))])
    _run(p._refresh_provider_profiles(w, _transport(relays, [_Offer(NPUB_A)])))
    assert p.provider_name(w, NPUB_A) == ""


def test_provider_name_tolerates_junk_in_the_store() -> None:
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {NPUB_A: "not-a-dict", NPUB_B: None})
    assert p.provider_name(w, NPUB_A) == ""
    assert p.provider_names(w) == {}


def test_provider_name_of_nothing_is_nothing() -> None:
    p, w = _plugin(), _wallet()
    assert p.provider_name(w, "") == ""
    assert p.provider_name(w, "npub1unknown") == ""


# --- bounding the store ---------------------------------------------------
def test_the_cache_is_capped_newest_checked_first() -> None:
    p, w = _plugin(), _wallet()
    now = time.time()
    data = {f"npub1row{i:04d}": {"name": f"n{i}", "checked_ts": now - i}
            for i in range(PROVIDER_PROFILE_MAX_ROWS + 50)}
    p._save_provider_profiles(w, data)
    stored = w.db.get(PROVIDER_PROFILES_DB_KEY)
    assert len(stored) == PROVIDER_PROFILE_MAX_ROWS
    # The 50 oldest went; the newest survived.
    assert "npub1row0000" in stored
    assert f"npub1row{PROVIDER_PROFILE_MAX_ROWS + 49:04d}" not in stored


def test_a_name_is_pruned_once_nothing_can_display_it() -> None:
    p, w = _plugin(), _wallet()
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Still Here", "checked_ts": time.time()},
        NPUB_B: {"name": "Forgotten", "checked_ts": time.time()}})
    w.db.put(RELIABILITY_DB_KEY, {
        NPUB_A: {"fault_count": 1, "last_fault_ts": time.time()}})
    p._prune_reliability_store(w)
    assert set(p.provider_names(w)) == {NPUB_A}


def test_a_discovered_provider_keeps_its_name_with_no_reliability_row() -> None:
    # Discovery alone is reason enough to keep a name: that provider has a row
    # on the Providers tab even with no fault history at all.
    p = _plugin()
    w = _HashableWallet(_FakeDB())
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Fast Swaps", "checked_ts": time.time()},
        NPUB_B: {"name": "Not Discovered", "checked_ts": time.time()}})
    p._last_offers[w] = [
        ProviderOffer(npub=NPUB_A, percentage_fee=0.5, mining_fee_sat=1,
                      min_amount_sat=1, max_reverse_sat=1)]
    p._prune_reliability_store(w)       # no reliability rows at all
    assert set(p.provider_names(w)) == {NPUB_A}


def test_an_idle_reliability_row_takes_its_name_with_it() -> None:
    p = _plugin()
    w = _HashableWallet(_FakeDB())
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Long Gone", "checked_ts": time.time()}})
    w.db.put(RELIABILITY_DB_KEY, {
        NPUB_A: {"fault_count": 1, "success_count": 1,
                 "last_fault_ts": time.time() - RELIABILITY_ROW_MAX_IDLE_SEC - 1,
                 "last_success_ts": time.time() - RELIABILITY_ROW_MAX_IDLE_SEC - 1}})
    p._prune_reliability_store(w)
    assert p.provider_names(w) == {}


def test_pruning_writes_nothing_when_every_name_is_still_referenced() -> None:
    p = _plugin()
    w = _HashableWallet(_FakeDB())
    w.db.put(PROVIDER_PROFILES_DB_KEY, {
        NPUB_A: {"name": "Fast Swaps", "checked_ts": time.time()}})
    w.db.put(RELIABILITY_DB_KEY, {
        NPUB_A: {"fault_count": 1, "last_fault_ts": time.time()}})
    before = w.db.puts
    p._prune_reliability_store(w)
    assert w.db.puts == before


# --- the timeout is sized for a cosmetic lookup --------------------------
def test_the_lookup_timeout_is_far_below_aionostr_s_own() -> None:
    # aionostr's EOSE_TIMEOUT_SEC is 60s, which is the wrong order of magnitude
    # for a name fetched on the swap path.
    assert 0 < PROFILE_FETCH_TIMEOUT_SEC <= 10
