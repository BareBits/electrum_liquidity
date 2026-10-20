"""The Channel partners tab's human-readable "Node name" column.

Column 0 is the peer's announced alias; the node id moved to column 1 and every
column after it shifted by one. That shift is the risk in this change -- the
Preferred/Banned check states are addressed BY INDEX, so getting it wrong bans
the peer the user meant to prefer -- so these tests pin the header layout, the
checkbox columns, and that the row's identity still rides on column 0's
UserRole where every reader expects it.

Shares the Qt scaffolding (fake window/wallet/config, the offscreen QApplication
and the Log-tab timer muzzle) with test_qt_tab; importing the fixtures binds
them in this module too."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QTabWidget, QTreeWidget  # noqa: E402

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)

PARTNERS_TAB = 2
NODE_A = "02" + "aa" * 32
NODE_B = "03" + "bb" * 32


class _FakeChannel:
    def __init__(self, node_id_hex: str) -> None:
        self.node_id = bytes.fromhex(node_id_hex)


def _with_channels(wallet, aliases):
    """Give ``wallet`` a channel to each node in ``aliases`` (hex -> name), and
    a peer manager that answers with that name ('' meaning "not known")."""
    class _PeerMgr:
        @staticmethod
        def get_node_alias(node_id: bytes):
            return aliases.get(node_id.hex()) or None

    class _LnWorker:
        lnpeermgr = _PeerMgr()
        channels = {i: _FakeChannel(nid) for i, nid in enumerate(aliases)}

    wallet.lnworker = _LnWorker()
    return wallet


def _partners_tree(plugin, wallet) -> QTreeWidget:
    tab = plugin._tabs[wallet].container.findChild(QTabWidget).widget(PARTNERS_TAB)
    return tab.findChild(QTreeWidget)


def _headers(tree: QTreeWidget):
    head = tree.headerItem()
    return [head.text(i) for i in range(tree.columnCount())]


def _build(aliases):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _with_channels(_FakeWallet(), aliases)
    p._add_liquidity_tab(window, wallet)
    return p, wallet, _partners_tree(p, wallet)


# --- the column ----------------------------------------------------------
def test_node_name_is_the_first_column(qapp) -> None:
    _p, _w, tree = _build({NODE_A: "ACINQ"})
    assert _headers(tree) == [
        "Node name", "Channel peer (node id)", "Preferred", "Banned",
        "OK", "Faults", "Hard", "Penalty %", "Last fault", "Last fault reason",
    ]


def test_a_known_name_is_shown_next_to_its_node_id(qapp) -> None:
    p, _w, tree = _build({NODE_A: "ACINQ"})
    row = tree.topLevelItem(0)
    assert row.text(p._PARTNER_NAME_COL) == "ACINQ"
    # The id column still carries the abbreviated hex it always did.
    assert row.text(p._PARTNER_NODE_ID_COL) == p._abbrev(NODE_A, 12, 6)


def test_an_unknown_name_reads_as_a_dash(qapp) -> None:
    # The common case: with gossip off, Electrum knows no alias for an
    # ordinary peer. An empty cell would read as a rendering bug.
    p, _w, tree = _build({NODE_A: ""})
    assert tree.topLevelItem(0).text(p._PARTNER_NAME_COL) == "—"


def test_the_name_cell_shows_the_full_name_and_id_on_hover(qapp) -> None:
    # Both values are shortened in the table: the name is elided at the column
    # width, the id is abbreviated outright.
    p, _w, tree = _build({NODE_A: "ACINQ"})
    assert tree.topLevelItem(0).toolTip(p._PARTNER_NAME_COL) == f"ACINQ\n{NODE_A}"


def test_an_untrusted_name_is_sanitised_before_it_reaches_the_cell(qapp) -> None:
    # A peer announces its own alias; a newline in it would spill the row.
    p, _w, tree = _build({NODE_A: "AC\nINQ"})
    assert tree.topLevelItem(0).text(p._PARTNER_NAME_COL) == "AC INQ"


def test_every_listed_peer_gets_a_name_cell(qapp) -> None:
    p, _w, tree = _build({NODE_A: "ACINQ", NODE_B: ""})
    assert tree.topLevelItemCount() == 2
    assert {tree.topLevelItem(i).text(p._PARTNER_NAME_COL) for i in range(2)} \
        == {"ACINQ", "—"}


# --- what the shift must not have broken ---------------------------------
def test_the_row_identity_still_rides_on_column_zero(qapp) -> None:
    # `on_apply`, `on_reset_stats` and the tests above all read
    # data(0, UserRole); the name column took over column 0, so this is the
    # assertion that the payload moved with it rather than being left behind.
    _p, _w, tree = _build({NODE_A: "ACINQ"})
    assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == NODE_A


def test_the_checkbox_columns_moved_with_the_headers(qapp) -> None:
    p, _w, tree = _build({NODE_A: "ACINQ"})
    row = tree.topLevelItem(0)
    headers = _headers(tree)
    assert headers[p._PARTNER_PREF_COL] == "Preferred"
    assert headers[p._PARTNER_BAN_COL] == "Banned"
    # A check state only exists on a column that was given one, so this also
    # proves no stray box landed on the name or id column.
    for col in (p._PARTNER_NAME_COL, p._PARTNER_NODE_ID_COL):
        assert row.data(col, Qt.ItemDataRole.CheckStateRole) is None


def test_ticking_preferred_still_persists_that_peer(qapp) -> None:
    from PyQt6.QtWidgets import QPushButton
    p, wallet, tree = _build({NODE_A: "ACINQ"})
    partners_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(PARTNERS_TAB)
    tree.topLevelItem(0).setCheckState(p._PARTNER_PREF_COL, Qt.CheckState.Checked)
    next(b for b in partners_tab.findChildren(QPushButton) if b.text() == "Apply").click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_PARTNERS == NODE_A
    assert p.settings(wallet).INBOUND_LIQUIDITY_BANNED_PARTNERS == ""


def test_ticking_banned_still_persists_that_peer(qapp) -> None:
    from PyQt6.QtWidgets import QPushButton
    p, wallet, tree = _build({NODE_A: "ACINQ"})
    partners_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(PARTNERS_TAB)
    tree.topLevelItem(0).setCheckState(p._PARTNER_BAN_COL, Qt.CheckState.Checked)
    next(b for b in partners_tab.findChildren(QPushButton) if b.text() == "Apply").click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_BANNED_PARTNERS == NODE_A
    assert p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_PARTNERS == ""


def test_the_fault_reason_tooltip_is_on_the_faults_column(qapp) -> None:
    p = _make_plugin()
    window = _FakeWindow()
    wallet = _with_channels(_FakeWallet(), {NODE_A: "ACINQ"})
    p._add_liquidity_tab(window, wallet)
    # Charge the peer a fault so a reason exists, then rebuild the rows.
    p._record_peer_fault(wallet, NODE_A, "could not connect", hard=False)
    p._tabs[wallet].refresh()
    row = _partners_tree(p, wallet).topLevelItem(0)
    assert row.toolTip(p._PARTNER_FAULTS_COL) == "could not connect"
    assert _headers(_partners_tree(p, wallet))[p._PARTNER_FAULTS_COL] == "Faults"


def test_a_peer_with_only_a_fault_history_is_still_listed(qapp) -> None:
    # It has no channel, so `current_channel_partners` does not produce it and
    # no alias is attached to its row; it must still appear, with a dash.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._record_peer_fault(wallet, NODE_B, "force-closed on us", hard=True)
    p._add_liquidity_tab(window, wallet)
    tree = _partners_tree(p, wallet)
    assert tree.topLevelItemCount() == 1
    assert tree.topLevelItem(0).text(p._PARTNER_NAME_COL) == "—"
    assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == NODE_B.lower()
