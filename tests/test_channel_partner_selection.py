"""Unit tests for the pure channel-partner helpers in the rules engine
(`normalize_node_id` / `order_channel_partners` / `is_channel_size_rejection`).
These import the engine module directly (via conftest's sys.path shim), with no
Electrum dependency."""
from __future__ import annotations

from liquidity_manager import (  # type: ignore
    is_channel_size_rejection,
    normalize_node_id,
    order_channel_partners,
    resolve_channel_partners,
)

PUB_A = "02" + "aa" * 32
PUB_B = "03" + "bb" * 32
PUB_C = "02" + "cc" * 32


def test_normalize_strips_host_and_lowercases():
    assert normalize_node_id(f"{PUB_A.upper()}@127.0.0.1:9735") == PUB_A
    assert normalize_node_id(f"  {PUB_B}  ") == PUB_B
    assert normalize_node_id(PUB_C) == PUB_C


def test_normalize_handles_empty():
    assert normalize_node_id("") == ""
    assert normalize_node_id(None) == ""


def test_preferred_first_then_suggested():
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1"], banned=frozenset(),
        suggested=[PUB_B], strict=False)
    assert out == [f"{PUB_A}@h:1", PUB_B]


def test_preferred_order_preserved():
    out = order_channel_partners(
        preferred=[f"{PUB_B}@h:1", f"{PUB_A}@h:2"], banned=frozenset(),
        suggested=[], strict=False)
    assert out == [f"{PUB_B}@h:1", f"{PUB_A}@h:2"]


def test_strict_drops_suggestions():
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1"], banned=frozenset(),
        suggested=[PUB_B], strict=True)
    assert out == [f"{PUB_A}@h:1"]


def test_banned_excluded_by_pubkey_from_both():
    # Banned by bare pubkey removes it whether it appears in preferred (with host)
    # or in the suggestions (bare).
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1", f"{PUB_B}@h:2"], banned=frozenset({PUB_A}),
        suggested=[PUB_A, PUB_C], strict=False)
    assert out == [f"{PUB_B}@h:2", PUB_C]


def test_dedupe_keeps_first_occurrence():
    # Same node id in preferred (with host) and suggested (bare): only the first.
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1"], banned=frozenset(),
        suggested=[PUB_A], strict=False)
    assert out == [f"{PUB_A}@h:1"]


def test_empty_inputs():
    assert order_channel_partners([], frozenset(), [], strict=False) == []
    assert order_channel_partners([""], frozenset(), ["  "], strict=False) == []


# --- one-channel-per-peer exclusion ---------------------------------------

def test_exclude_drops_existing_peer_from_both():
    # A peer we already have a channel with is dropped whether it appears in
    # preferred (with host) or in the suggestions (bare) -- the same as banned,
    # but semantically the transient "already a peer" guard.
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1", f"{PUB_B}@h:2"], banned=frozenset(),
        suggested=[PUB_A, PUB_C], strict=False, exclude=frozenset({PUB_A}))
    assert out == [f"{PUB_B}@h:2", PUB_C]


def test_exclude_can_empty_the_list():
    # If every candidate is an existing peer, nothing is returned -- the glue
    # turns this into a "one-channel-per-peer" decline.
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1"], banned=frozenset(),
        suggested=[PUB_B], strict=False, exclude=frozenset({PUB_A, PUB_B}))
    assert out == []


def test_exclude_defaults_to_no_op():
    # Omitting exclude preserves the prior behaviour exactly.
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1"], banned=frozenset(),
        suggested=[PUB_B], strict=False)
    assert out == [f"{PUB_A}@h:1", PUB_B]


def test_exclude_and_banned_compose():
    # Banned and excluded are both removed; a peer that is only excluded is gone
    # too, and order among survivors is preserved.
    out = order_channel_partners(
        preferred=[f"{PUB_A}@h:1", f"{PUB_B}@h:2", f"{PUB_C}@h:3"],
        banned=frozenset({PUB_A}), suggested=[], strict=False,
        exclude=frozenset({PUB_B}))
    assert out == [f"{PUB_C}@h:3"]


# --- channel-size rejection classification --------------------------------
# A failed open that was only ever about the channel SIZE must not be charged to
# the peer (see is_channel_size_rejection's docstring and the _open_channel
# caller): a node that declines a channel below its own minimum is healthy, and
# the plugin now walks up to MAX_SUGGESTED_PARTNERS peers per tick, so faulting
# on size would auto-ban swathes of the graph for nothing.
def test_size_rejection_matches_electrum_local_bounds():
    # Raised by lnutil.ChannelConfig.validate_params / cross_validate_params.
    assert is_channel_size_rejection("funding_sat too low: 100000 sat < 200000")
    assert is_channel_size_rejection(
        "funding_sat too high: 20000000 sat > 16777215 (legacy limit)")
    assert is_channel_size_rejection("remote. reserve too high: 50000, funding_sat: 200000")
    assert is_channel_size_rejection(
        "local. max_htlc_value_in_flight_msat is too small: 1000")


def test_size_rejection_matches_peer_error_text_across_implementations():
    # A peer's BOLT error, as Electrum relays it (lnpeer.wait_for_message ->
    # GracefulDisconnect). The text is implementation-specific free text.
    for text in (
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: channel too small",
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: chan size of 0.002 BTC "
        "is below min chan size of 0.02 BTC",
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: funding amount is too small",
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: Amount too small",
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: funding_satoshis is too small",
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: exceeds maximum channel size",
    ):
        assert is_channel_size_rejection(text), text


def test_non_size_failures_are_not_size_rejections():
    # These stay faultable: they are real reliability signals.
    for text in (
        "remote peer sent error [DO NOT TRUST THIS MESSAGE]: internal error",
        "Received unexpected 'error'",
        "minimum depth too high, 42",
        "feerate lower than min relay fee. 100 sat/kw.",
        "ConnectionError: [Errno 111] Connection refused",
        "Channel type is not the one that we sent.",
        "",
    ):
        assert not is_channel_size_rejection(text), text


def test_size_rejection_handles_none_and_is_case_insensitive():
    assert not is_channel_size_rejection(None)
    assert is_channel_size_rejection("FUNDING AMOUNT TOO SMALL")


# --- resolve_channel_partners: the counts behind the result ---------------
# `order_channel_partners` answers "who do I try"; this answers "and why is that
# list the length it is" -- the arithmetic the plugin shows when an open is
# declined for having no reachable partner.
def test_resolution_counts_a_clean_run():
    res = resolve_channel_partners(
        preferred=[PUB_A], banned=frozenset(), suggested=[PUB_B, PUB_C],
        strict=False)
    assert list(res.candidates) == [PUB_A, PUB_B, PUB_C]
    assert (res.preferred_total, res.preferred_kept) == (1, 1)
    assert (res.suggested_total, res.suggested_kept) == (2, 2)
    assert res.banned_hits == res.guard_hits == res.duplicate_hits == 0
    assert res.malformed_hits == 0
    assert res.total_kept == 3


def test_resolution_attributes_each_drop_to_one_rule():
    # PUB_A banned, PUB_B blocked by the one-channel-per-peer guard, PUB_C listed
    # twice. Each drop is charged to exactly one rule, checked banned -> guard ->
    # duplicate, so the numbers reconcile.
    res = resolve_channel_partners(
        preferred=[PUB_A, PUB_B, PUB_C],
        banned=frozenset({PUB_A}),
        suggested=[PUB_C],
        strict=False,
        exclude=frozenset({PUB_B}))
    assert list(res.candidates) == [PUB_C]
    assert res.banned_hits == 1
    assert res.guard_hits == 1
    assert res.duplicate_hits == 1
    kept = res.preferred_kept + res.suggested_kept
    total = res.preferred_total + res.suggested_total
    assert total - kept == res.banned_hits + res.guard_hits + res.duplicate_hits


def test_resolution_flags_unparseable_entries_without_dropping_them():
    # A typo'd preferred partner is named in the breakdown instead of looking
    # like a peer that simply refused -- but it is still tried, so partner
    # selection behaves exactly as it did before the counts existed.
    res = resolve_channel_partners(
        preferred=["not-a-pubkey", PUB_A], banned=frozenset(), suggested=[],
        strict=False)
    assert list(res.candidates) == ["not-a-pubkey", PUB_A]
    assert res.malformed_hits == 1
    assert res.preferred_total == res.preferred_kept == 2


def test_resolution_reports_strict_mode():
    res = resolve_channel_partners(
        preferred=[PUB_A], banned=frozenset(), suggested=[PUB_B], strict=True)
    assert list(res.candidates) == [PUB_A]
    assert res.strict is True
    assert (res.suggested_total, res.suggested_kept) == (0, 0)


def test_resolution_applies_penalties_like_the_ordering_wrapper():
    res = resolve_channel_partners(
        preferred=[PUB_A, PUB_B], banned=frozenset(), suggested=[], strict=False,
        penalties={PUB_A: 5.0})
    assert list(res.candidates) == [PUB_B, PUB_A]     # flaky peer sinks


def test_order_channel_partners_still_returns_a_plain_list():
    # The wrapper is what most callers use; it must stay a mutable list of the
    # same entries the resolution produced.
    args = dict(preferred=[PUB_A], banned=frozenset(), suggested=[PUB_B],
                strict=False)
    out = order_channel_partners(**args)
    assert isinstance(out, list)
    assert out == list(resolve_channel_partners(**args).candidates)
