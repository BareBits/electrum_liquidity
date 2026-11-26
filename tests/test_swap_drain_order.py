"""Unit tests for the order channels are planned in -- ``drain_order`` and the
allocation it decides inside ``_decide_reverse_swaps``.

A planning pass is an ALLOCATION, not just a loop: providers advertise a finite
``max_forward``, the pass draws it down as it plans (``consumed``), and the first
channel to be planned takes the cheapest provider's capacity. Whichever channel
goes first therefore gets the cheapest swap, and a later one may find only
dearer rungs -- or none that clear the cost gate at all.

That order used to be Electrum's: ``LNWallet.__init__`` loads the channel dict
through ``random_shuffled_copy``, and ``build_snapshot`` iterated it unchanged.
So the same wallet, with the same providers, spent a different amount on every
restart -- and a small channel could consume a cheap provider's whole allowance
and push a large one onto an expensive provider.

No Electrum import required.
"""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    drain_order,
    _decide_reverse_swaps,
)

from test_provider_selection import (  # type: ignore
    make_channel,
    make_config,
    make_offer,
    make_snapshot,
)


def _chan(short_id: str, local: int, **overrides):
    """A channel identified by short_id, holding ``local`` sat of outbound."""
    base = dict(
        channel_id=short_id.replace("x", "") .rjust(64, "0"),
        short_id=short_id,
        capacity_sat=max(2_000_000, local * 2),
        local_sat=local,
        remote_sat=max(2_000_000, local * 2) - local,
        spendable_local_sat=int(local * 0.9),
    )
    base.update(overrides)
    return make_channel(**base)


# --- the ordering itself --------------------------------------------------
def test_fullest_channel_is_planned_first() -> None:
    """The scarce resource goes where it buys the most inbound liquidity."""
    small = _chan("100x1x0", 200_000)
    big = _chan("200x1x0", 1_800_000)
    assert [c.short_id for c in drain_order([small, big])] == ["200x1x0", "100x1x0"]


def test_order_is_independent_of_input_order() -> None:
    """The input is Electrum's shuffled dict; the output must not be."""
    a = _chan("100x1x0", 300_000)
    b = _chan("200x1x0", 900_000)
    c = _chan("300x1x0", 600_000)
    expected = ["200x1x0", "300x1x0", "100x1x0"]
    for ordering in ([a, b, c], [c, b, a], [b, a, c], [c, a, b]):
        assert [x.short_id for x in drain_order(ordering)] == expected


def test_equal_channels_break_ties_on_short_id() -> None:
    """Equal-sized channels are exactly the case the shuffle used to decide, so
    leaving ties to input order would preserve the nondeterminism in the case
    most likely to hit a uniformly-funded wallet -- which is what the rig is."""
    first = _chan("117x1x0", 1_000_000)
    second = _chan("121x1x0", 1_000_000)
    third = _chan("124x1x0", 1_000_000)
    for ordering in ([third, first, second], [second, third, first],
                     [first, second, third]):
        assert [c.short_id for c in drain_order(ordering)] == [
            "117x1x0", "121x1x0", "124x1x0"]


def test_drain_order_does_not_mutate_or_drop() -> None:
    channels = [_chan("200x1x0", 10), _chan("100x1x0", 20)]
    original = list(channels)
    ordered = drain_order(channels)
    assert channels == original, "the caller's sequence was reordered in place"
    assert sorted(c.short_id for c in ordered) == ["100x1x0", "200x1x0"]


def test_drain_order_handles_empty_and_single() -> None:
    assert drain_order([]) == []
    one = _chan("100x1x0", 5)
    assert [c.short_id for c in drain_order([one])] == ["100x1x0"]


def test_drain_order_tolerates_a_missing_short_id() -> None:
    """``short_id`` is a plain str off the snapshot; a channel without one must
    sort rather than raise on ``None < str``."""
    named = _chan("100x1x0", 1_000)
    unnamed = make_channel(channel_id="bb" * 32, short_id="",
                           local_sat=1_000, spendable_local_sat=900)
    assert len(drain_order([named, unnamed])) == 2


# --- the allocation it drives --------------------------------------------
def _single_cheap_provider_snapshot():
    """Two equal channels, one cheap provider with room for only ONE of them.

    This is the rig's shape, and the shape the bug lived in: ``cheap`` can host
    900k, each channel wants ~810k of drain, so the first channel planned takes
    it and the second must fall back to ``dear``.
    """
    cheap = make_offer("npub1cheap", pct=0.1, mining=0, lo=20_000, hi=900_000)
    dear = make_offer("npub1dear", pct=2.0, mining=0, lo=20_000, hi=5_000_000)
    return cheap, dear


def test_the_cheap_provider_is_spent_on_the_biggest_drain() -> None:
    cheap, dear = _single_cheap_provider_snapshot()
    snapshot = make_snapshot(
        [cheap, dear],
        channels=(_chan("100x1x0", 400_000), _chan("200x1x0", 1_000_000)),
    )
    actions, _ = _decide_reverse_swaps(snapshot, make_config(
        swap_trigger_sat=100_000, swap_trigger_pct=10, max_swap_fee_pct=10))
    by_channel = {a.short_id: a.provider_npub for a in actions}
    assert by_channel["200x1x0"] == "npub1cheap", (
        "the cheap provider went to the smaller channel; its capacity buys the "
        "most inbound on the largest drain")
    assert by_channel["100x1x0"] == "npub1dear"


def test_allocation_is_the_same_whatever_order_electrum_hands_us() -> None:
    """The regression itself: same wallet, same providers, same answer -- on
    every restart. Before ``drain_order`` this flipped with the shuffle."""
    cheap, dear = _single_cheap_provider_snapshot()
    small = _chan("100x1x0", 400_000)
    large = _chan("200x1x0", 1_000_000)
    config = make_config(swap_trigger_sat=100_000, swap_trigger_pct=10,
                         max_swap_fee_pct=10)
    results = []
    for channels in ((small, large), (large, small)):
        actions, _ = _decide_reverse_swaps(
            make_snapshot([cheap, dear], channels=channels), config)
        results.append({a.short_id: a.provider_npub for a in actions})
    assert results[0] == results[1], (
        f"the provider allocation depended on channel order: {results}")


def test_equal_channels_allocate_deterministically() -> None:
    """Ties are where the shuffle did its damage on a uniformly-funded wallet:
    nothing distinguishes the channels but one of them still gets the cheap
    provider, so the tie-break has to decide it rather than the input order."""
    cheap, dear = _single_cheap_provider_snapshot()
    a = _chan("117x1x0", 1_000_000)
    b = _chan("121x1x0", 1_000_000)
    config = make_config(swap_trigger_sat=100_000, swap_trigger_pct=10,
                         max_swap_fee_pct=10)
    for channels in ((a, b), (b, a)):
        actions, _ = _decide_reverse_swaps(
            make_snapshot([cheap, dear], channels=channels), config)
        by_channel = {act.short_id: act.provider_npub for act in actions}
        assert by_channel["117x1x0"] == "npub1cheap"
        assert by_channel["121x1x0"] == "npub1dear"


def test_every_channel_still_gets_an_action() -> None:
    """Reordering must not drop anyone: the order decides who is served first,
    never who is served."""
    cheap, dear = _single_cheap_provider_snapshot()
    channels = tuple(_chan(f"{100 + i}x1x0", 500_000 + i * 1_000)
                     for i in range(5))
    actions, _ = _decide_reverse_swaps(
        make_snapshot([cheap, dear], channels=channels),
        make_config(swap_trigger_sat=100_000, swap_trigger_pct=10,
                    max_swap_fee_pct=10))
    assert {a.short_id for a in actions} == {c.short_id for c in channels}
