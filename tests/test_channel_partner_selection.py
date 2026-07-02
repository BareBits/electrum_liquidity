"""Unit tests for the pure channel-partner ordering helpers in the rules engine
(`normalize_node_id` / `order_channel_partners`). These import the engine module
directly (via conftest's sys.path shim), with no Electrum dependency."""
from __future__ import annotations

from liquidity_manager import normalize_node_id, order_channel_partners  # type: ignore

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
