"""Glue-level tests for the peer's human-readable node name.

Two halves:

* `_clean_node_alias`, the sanitiser. An alias is chosen by the REMOTE node and
  arrives in its gossip `node_announcement`, so it is untrusted display input:
  these pin that a hostile one cannot break a table row or impersonate another
  node's name.
* `Plugin.node_alias` / `current_channel_partners`, the lookup. Every hop to
  Electrum is optional (no Lightning, no `lnpeermgr`, a raising lookup), and
  each must cost the caller a name rather than its row -- plus one test that
  drives Electrum's REAL `LNPeerManager.get_node_alias`, so the plugin is held
  to the function it actually calls rather than to a fake of it.

Skipped if the plugin package can't be imported."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    MAX_NODE_ALIAS_CHARS,
    _clean_node_alias,
)

NODE_A = "02" + "aa" * 32
NODE_B = "03" + "bb" * 32


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.alias")
    return p


def _wallet(*, alias=None, channels=(), raises=False, peermgr=True,
            lnworker=True) -> SimpleNamespace:
    """A wallet whose Lightning stack is present only as far as asked for."""
    if not lnworker:
        return SimpleNamespace(lnworker=None)

    def get_node_alias(node_id: bytes):
        if raises:
            raise RuntimeError("gossip db is closed")
        return alias

    mgr = SimpleNamespace(get_node_alias=get_node_alias) if peermgr else None
    chans = {i: SimpleNamespace(node_id=bytes.fromhex(nid))
             for i, nid in enumerate(channels)}
    return SimpleNamespace(lnworker=SimpleNamespace(lnpeermgr=mgr, channels=chans))


# --- the sanitiser -------------------------------------------------------
def test_an_ordinary_alias_is_passed_through() -> None:
    assert _clean_node_alias("ACINQ") == "ACINQ"
    assert _clean_node_alias("Bitrefill Routing Node") == "Bitrefill Routing Node"


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\n", "​​"])
def test_nothing_in_cleans_to_nothing_out(empty) -> None:
    assert _clean_node_alias(empty) == ""


def test_a_line_break_cannot_spill_the_row_onto_the_next_one() -> None:
    # A newline in a table cell is the cheapest way to wreck the layout of
    # every row around it.
    assert _clean_node_alias("ACINQ\nrow two") == "ACINQ row two"
    assert _clean_node_alias("a\r\nb\tc") == "a b c"


def test_control_characters_are_dropped() -> None:
    assert _clean_node_alias("AC\x00IN\x07Q") == "ACINQ"


def test_invisible_characters_cannot_forge_another_nodes_name() -> None:
    # Zero-width space / soft hyphen: "A​CINQ" renders identically to
    # "ACINQ" but compares unequal, so a peer could sit next to the real one
    # looking exactly like it.
    assert _clean_node_alias("A​C­INQ") == "ACINQ"
    # Right-to-left override: renders the rest of the string reversed.
    assert _clean_node_alias("‮QNICA") == "QNICA"


def test_a_long_alias_is_bounded_and_marked_as_cut() -> None:
    cleaned = _clean_node_alias("N" * 200)
    assert len(cleaned) == MAX_NODE_ALIAS_CHARS
    assert cleaned.endswith("…")


def test_an_alias_exactly_at_the_limit_is_not_cut() -> None:
    exact = "N" * MAX_NODE_ALIAS_CHARS
    assert _clean_node_alias(exact) == exact


def test_no_alias_can_impersonate_the_unnamed_sort_sentinel() -> None:
    """The Channel partners tab sorts unnamed peers last by giving them U+FFFF
    as their sort key. That only holds if no real alias can contain it -- a
    peer could otherwise announce one and pin itself past every named row.
    U+FFFF is a noncharacter, so the sanitiser drops it."""
    assert _clean_node_alias("￿") == ""
    assert _clean_node_alias("￿ACINQ") == "ACINQ"
    assert "￿" not in _clean_node_alias("A￿B")


def test_a_non_string_alias_does_not_raise() -> None:
    class Hostile:
        def __str__(self):
            raise ValueError("nope")

    assert _clean_node_alias(Hostile()) == ""
    assert _clean_node_alias(12345) == "12345"


# --- the lookup ----------------------------------------------------------
def test_the_alias_is_read_from_electrum() -> None:
    p = _plugin()
    assert p.node_alias(_wallet(alias="ACINQ"), NODE_A) == "ACINQ"


def test_the_alias_is_sanitised_on_the_way_out() -> None:
    p = _plugin()
    assert p.node_alias(_wallet(alias="AC\nINQ​"), NODE_A) == "AC INQ"


def test_an_unknown_alias_is_empty_not_none() -> None:
    # The caller renders it straight into a cell, so None would read "None".
    assert _plugin().node_alias(_wallet(alias=None), NODE_A) == ""


def test_a_wallet_without_lightning_has_no_names() -> None:
    assert _plugin().node_alias(_wallet(lnworker=False), NODE_A) == ""


def test_an_lnworker_without_a_peer_manager_has_no_names() -> None:
    # Another Electrum version may not expose `lnpeermgr`; that costs the name,
    # not the tab.
    assert _plugin().node_alias(_wallet(peermgr=False), NODE_A) == ""


def test_a_lookup_that_raises_costs_only_the_name() -> None:
    assert _plugin().node_alias(_wallet(alias="ACINQ", raises=True), NODE_A) == ""


def test_a_malformed_node_id_is_not_a_crash() -> None:
    p = _plugin()
    assert p.node_alias(_wallet(alias="ACINQ"), "not-hex") == ""
    assert p.node_alias(_wallet(alias="ACINQ"), "") == ""


# --- against Electrum's real implementation ------------------------------
def test_the_real_electrum_lookup_resolves_a_trampoline_node() -> None:
    """Drive `LNPeerManager.get_node_alias` itself, not a fake of it.

    With gossip off -- Electrum's default, and the rig's -- `uses_trampoline()`
    is true and the lookup matches against the hardcoded trampoline nodes.
    Electrum keeps `_TRAMPOLINE_NODES_UNITTESTS` as the supported way to stand
    one up, so this is the genuine code path, name and all.
    """
    from electrum import trampoline  # type: ignore
    from electrum.lnworker import LNPeerManager  # type: ignore
    from electrum.lntransport import LNPeerAddr  # type: ignore

    node_id = bytes.fromhex(NODE_A)
    saved = dict(trampoline._TRAMPOLINE_NODES_UNITTESTS)
    trampoline._TRAMPOLINE_NODES_UNITTESTS.update(
        {"Trampoline Test Node": LNPeerAddr("127.0.0.1", 9735, node_id)})
    try:
        mgr = object.__new__(LNPeerManager)
        # uses_trampoline() is `not bool(self.channel_db)`, and channel_db is
        # `self.network.channel_db if self.network else None`.
        mgr.network = None
        wallet = SimpleNamespace(lnworker=SimpleNamespace(lnpeermgr=mgr))
        assert _plugin().node_alias(wallet, NODE_A) == "Trampoline Test Node"
        # A node it has never heard of still yields no name rather than raising.
        assert _plugin().node_alias(wallet, NODE_B) == ""
    finally:
        trampoline._TRAMPOLINE_NODES_UNITTESTS.clear()
        trampoline._TRAMPOLINE_NODES_UNITTESTS.update(saved)


# --- current_channel_partners carries it ---------------------------------
def test_each_current_partner_carries_its_name() -> None:
    p = _plugin()
    rows = p.current_channel_partners(_wallet(alias="ACINQ", channels=[NODE_A]))
    assert rows == [{"node_id": NODE_A, "alias": "ACINQ"}]


def test_a_partner_with_no_known_name_still_gets_the_key() -> None:
    # The Qt tab reads `partner["alias"]`; a missing key would be a KeyError
    # per row for the common (trampoline) case.
    p = _plugin()
    rows = p.current_channel_partners(_wallet(alias=None, channels=[NODE_A]))
    assert rows == [{"node_id": NODE_A, "alias": ""}]


def test_partners_are_still_deduplicated_by_node_id() -> None:
    p = _plugin()
    rows = p.current_channel_partners(
        _wallet(alias="ACINQ", channels=[NODE_A, NODE_A, NODE_B]))
    assert [r["node_id"] for r in rows] == [NODE_A, NODE_B]


def test_a_wallet_without_lightning_lists_no_partners() -> None:
    assert _plugin().current_channel_partners(_wallet(lnworker=False)) == []
