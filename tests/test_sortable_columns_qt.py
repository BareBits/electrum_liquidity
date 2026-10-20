"""Click-to-sort column headers on the Swap providers and Channel partners tabs.

The point of these tests is that sorting a table of FORMATTED values is a trap:
"1,900,000" sorts between "12" and "2", "3h ago" sorts before "2d ago", and an
unticked box and a ticked one are both the empty string. So most of what is
pinned here is that each column orders by the value it was rendered from.

The rest guards the two ways sorting can leak out of the view it belongs to:
a refresh tick must not silently drop the user's chosen order, and re-sorting
the partners table must not re-prioritise which peer the plugin opens a channel
to next.

Shares the Qt scaffolding with test_qt_tab (see test_partners_name_column_qt)."""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QPlainTextEdit, QPushButton, QTabWidget, QTreeWidget,
)

from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
)
from electrum.plugins.inbound_liquidity.qt_widgets import (  # type: ignore  # noqa: E402
    SortableTreeItem, _sort_tuple,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)

PROVIDERS_TAB = 1
PARTNERS_TAB = 2

NODE_A = "02" + "aa" * 32      # named "Zulu",  heavy penalty
NODE_B = "03" + "bb" * 32      # named "Alpha", no penalty
NODE_C = "02" + "cc" * 32      # unnamed


# --- scaffolding ---------------------------------------------------------
class _FakeChannel:
    def __init__(self, node_id_hex: str) -> None:
        self.node_id = bytes.fromhex(node_id_hex)


def _with_channels(wallet, aliases):
    class _PeerMgr:
        @staticmethod
        def get_node_alias(node_id: bytes):
            return aliases.get(node_id.hex()) or None

    class _LnWorker:
        lnpeermgr = _PeerMgr()
        channels = {i: _FakeChannel(nid) for i, nid in enumerate(aliases)}

    wallet.lnworker = _LnWorker()
    return wallet


def _sub_tab(plugin, wallet, index):
    return plugin._tabs[wallet].container.findChild(QTabWidget).widget(index)


def _tree(plugin, wallet, index) -> QTreeWidget:
    return _sub_tab(plugin, wallet, index).findChild(QTreeWidget)


def _click_header(tree: QTreeWidget, column: int) -> None:
    """What a user clicking the header section does, without a synthetic mouse
    event: the header emits `sectionClicked`, which is what the tab connects."""
    tree.header().sectionClicked.emit(column)


def _column(tree: QTreeWidget, col: int):
    return [tree.topLevelItem(i).text(col) for i in range(tree.topLevelItemCount())]


def _ids(tree: QTreeWidget):
    return [tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(tree.topLevelItemCount())]


def _providers_plugin():
    """A plugin whose providers tab has three offers with deliberately awkward
    numbers, plus one offline provider that advertises no terms at all."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._last_offers[wallet] = [
        # Fees chosen so a string sort disagrees with a numeric one:
        # "0.9" > "0.25" as text, but 0.9 > 0.25 numerically too -- the real
        # trap is the sat columns, where "1,900,000" < "20,000" as text.
        ProviderOffer(npub="npubMID", percentage_fee=0.5, mining_fee_sat=1_000,
                      min_amount_sat=20_000, max_reverse_sat=1_900_000, pow_bits=20),
        ProviderOffer(npub="npubCHEAP", percentage_fee=0.25, mining_fee_sat=300,
                      min_amount_sat=100_000, max_reverse_sat=250_000, pow_bits=5),
        ProviderOffer(npub="npubDEAR", percentage_fee=0.9, mining_fee_sat=20_000,
                      min_amount_sat=3_000, max_reverse_sat=90_000, pow_bits=12),
    ]
    # An offline provider: reliability history, no advertised terms.
    p._record_provider_fault(wallet, "npubGONE", "timed out")
    p._add_liquidity_tab(window, wallet)
    return p, wallet, _tree(p, wallet, PROVIDERS_TAB)


def _partners_plugin():
    p = _make_plugin()
    window = _FakeWindow()
    wallet = _with_channels(_FakeWallet(),
                            {NODE_A: "Zulu", NODE_B: "Alpha", NODE_C: ""})
    # Give the peers different histories so the stat columns have something to
    # order by: A is the worst, B is clean, C has one soft fault. Two hard
    # faults, not three: three trips the auto-ban, and a peer that arrives
    # pre-banned would quietly stand in for what these tests tick by hand.
    for _ in range(2):
        p._record_peer_fault(wallet, NODE_A, "open failed", hard=True)
    p._record_peer_fault(wallet, NODE_C, "offline", hard=False)
    p._add_liquidity_tab(window, wallet)
    return p, wallet, _tree(p, wallet, PARTNERS_TAB)


# --- the comparison key --------------------------------------------------
def test_numbers_and_text_never_meet_in_a_comparison() -> None:
    # A mixed column (a number in one row, a placeholder in another) would
    # otherwise raise TypeError inside Qt's sort, taking the repaint with it.
    assert _sort_tuple(1) < _sort_tuple("a")
    assert _sort_tuple(float("inf")) < _sort_tuple("")
    assert _sort_tuple(object()) > _sort_tuple("zzz")


def test_text_sorts_case_insensitively() -> None:
    assert _sort_tuple("alpha") < _sort_tuple("Zulu")


def test_an_item_outside_a_tree_still_compares() -> None:
    # __lt__ reads the sort column off the tree; an item not yet added to one
    # must fall back rather than raise.
    a, b = SortableTreeItem(["a"]), SortableTreeItem(["b"])
    a.set_sort_key(0, 1)
    b.set_sort_key(0, 2)
    assert a < b


# --- swap providers ------------------------------------------------------
def test_provider_headers_are_clickable(qapp) -> None:
    _p, _w, tree = _providers_plugin()
    assert tree.header().sectionsClickable()
    assert tree.header().isSortIndicatorShown()


def test_providers_keep_their_discovery_order_until_a_header_is_clicked(qapp) -> None:
    # The list ships in a meaningful order (discovered providers first, then
    # history-only ones); auto-sorting would throw that away unasked.
    _p, _w, tree = _providers_plugin()
    assert _ids(tree) == ["npubMID", "npubCHEAP", "npubDEAR", "npubGONE"]


def test_sorting_by_fee_orders_by_the_number_not_the_text(qapp) -> None:
    _p, _w, tree = _providers_plugin()
    _click_header(tree, 1)                      # Fee %
    assert _ids(tree)[:3] == ["npubCHEAP", "npubMID", "npubDEAR"]


def test_a_sat_column_does_not_sort_by_its_thousands_separators(qapp) -> None:
    # "1,900,000" sorts between "100,000" and "20,000" as TEXT. This is the
    # column the formatting actually breaks.
    _p, _w, tree = _providers_plugin()
    _click_header(tree, 4)                      # Max (sat), ascending
    # Ignore the offline row (no advertised max); the three real offers are
    # what this is about.
    advertised = [c for c in _column(tree, 4) if c]
    assert advertised == ["90,000", "250,000", "1,900,000"]


def test_an_offline_provider_sinks_to_the_bottom_whichever_term_is_sorted(qapp) -> None:
    # It advertises nothing, so it is never the answer to "who is cheapest" or
    # "who takes the largest swap" -- on either term, in the direction that
    # puts the best first.
    _p, _w, tree = _providers_plugin()
    _click_header(tree, 1)                      # Fee %, ascending = best first
    assert _ids(tree)[-1] == "npubGONE"
    _click_header(tree, 4)                      # Max (sat)
    _click_header(tree, 4)                      # ...descending = best first
    assert _ids(tree)[-1] == "npubGONE"


def test_clicking_the_same_header_twice_reverses_the_order(qapp) -> None:
    _p, _w, tree = _providers_plugin()
    _click_header(tree, 1)
    ascending = _ids(tree)
    _click_header(tree, 1)
    assert _ids(tree) == list(reversed(ascending))


def test_clicking_a_different_header_starts_ascending_again(qapp) -> None:
    _p, _w, tree = _providers_plugin()
    _click_header(tree, 1)
    _click_header(tree, 1)                      # now descending on Fee %
    _click_header(tree, 2)                      # Mining (sat)
    assert tree.header().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert _ids(tree)[:3] == ["npubCHEAP", "npubMID", "npubDEAR"]


def test_the_last_fault_column_sorts_by_age_not_by_its_label(qapp) -> None:
    # "—" (never), "3h ago" and "2d ago" order by the timestamps behind them.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._last_offers[wallet] = [
        ProviderOffer(npub=f"npub{n}", percentage_fee=0.5, mining_fee_sat=1,
                      min_amount_sat=1, max_reverse_sat=1, pow_bits=1)
        for n in ("NEVER", "HOURS", "DAYS")
    ]
    p._add_liquidity_tab(window, wallet)
    now = time.time()
    # Age the faults by hand: only the store carries the timestamps, and the
    # recorder always stamps them "now".
    p._record_provider_fault(wallet, "npubHOURS", "x")
    p._record_provider_fault(wallet, "npubDAYS", "x")
    store = p._load_reliability(wallet)
    store["npubHOURS"]["last_fault_ts"] = now - 3 * 3600
    store["npubDAYS"]["last_fault_ts"] = now - 2 * 86400
    p._save_reliability(wallet, store)
    p._tabs[wallet].refresh()
    tree = _tree(p, wallet, PROVIDERS_TAB)
    # Premise: the three cells really do read as the ages claimed.
    assert set(_column(tree, 11)) == {"—", "3h ago", "2d ago"}
    _click_header(tree, 11)                     # Last fault
    assert _ids(tree) == ["npubNEVER", "npubDAYS", "npubHOURS"]


def test_the_checkbox_columns_sort_on_their_live_state(qapp) -> None:
    # A box ticked after the rows were built must sort where the user can see
    # it is, not where it was when the row was created.
    p, _w, tree = _providers_plugin()
    tree.topLevelItem(2).setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    _click_header(tree, p._PREF_COL)
    _click_header(tree, p._PREF_COL)            # descending: ticked first
    assert _ids(tree)[0] == "npubDEAR"


# --- channel partners ----------------------------------------------------
def test_partner_headers_are_clickable(qapp) -> None:
    _p, _w, tree = _partners_plugin()
    assert tree.header().sectionsClickable()


def test_sorting_by_node_name_is_alphabetical(qapp) -> None:
    p, _w, tree = _partners_plugin()
    _click_header(tree, p._PARTNER_NAME_COL)
    assert _column(tree, p._PARTNER_NAME_COL) == ["Alpha", "Zulu", "—"]


def test_an_unnamed_peer_sorts_last_rather_than_among_the_names(qapp) -> None:
    # "—" is U+2014, which by codepoint lands after "Zulu" ascending but
    # BEFORE every name descending -- so it would interleave. It is pinned to
    # the end either way round.
    p, _w, tree = _partners_plugin()
    _click_header(tree, p._PARTNER_NAME_COL)
    assert _column(tree, p._PARTNER_NAME_COL)[-1] == "—"
    _click_header(tree, p._PARTNER_NAME_COL)
    assert _column(tree, p._PARTNER_NAME_COL)[0] == "—"


def test_sorting_by_penalty_orders_by_the_number(qapp) -> None:
    p, _w, tree = _partners_plugin()
    _click_header(tree, 7)                      # Penalty %
    _click_header(tree, 7)                      # worst first
    assert _ids(tree)[0] == NODE_A


def test_sorting_by_hard_faults_puts_the_worst_peer_first(qapp) -> None:
    p, _w, tree = _partners_plugin()
    _click_header(tree, 6)                      # Hard
    _click_header(tree, 6)
    assert _ids(tree)[0] == NODE_A
    assert _column(tree, 6)[0] == "2"


# --- sorting is a view, and must stay one --------------------------------
def test_a_refresh_does_not_drop_the_chosen_sort(qapp) -> None:
    # Every tick calls repopulate(), which clears the tree and rebuilds it. The
    # user's sort has to survive that or it is unusable on a live table.
    p, wallet, tree = _partners_plugin()
    _click_header(tree, p._PARTNER_NAME_COL)
    before = _column(tree, p._PARTNER_NAME_COL)
    p._tabs[wallet].refresh()
    tree = _tree(p, wallet, PARTNERS_TAB)
    assert _column(tree, p._PARTNER_NAME_COL) == before
    assert tree.header().sortIndicatorSection() == p._PARTNER_NAME_COL


def test_a_refresh_keeps_a_descending_sort_descending(qapp) -> None:
    p, wallet, tree = _partners_plugin()
    _click_header(tree, p._PARTNER_NAME_COL)
    _click_header(tree, p._PARTNER_NAME_COL)
    before = _column(tree, p._PARTNER_NAME_COL)
    p._tabs[wallet].refresh()
    tree = _tree(p, wallet, PARTNERS_TAB)
    assert _column(tree, p._PARTNER_NAME_COL) == before
    assert tree.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder


def test_apply_still_bans_the_peer_whose_row_was_ticked_after_sorting(qapp) -> None:
    # The check states are read off the rows, so a reordered table must not
    # ban the peer that happens to sit at the old index.
    p, wallet, tree = _partners_plugin()
    _click_header(tree, p._PARTNER_NAME_COL)    # Alpha, Zulu, —
    assert _ids(tree)[0] == NODE_B
    tree.topLevelItem(0).setCheckState(p._PARTNER_BAN_COL, Qt.CheckState.Checked)
    tab = _sub_tab(p, wallet, PARTNERS_TAB)
    next(b for b in tab.findChildren(QPushButton) if b.text() == "Apply").click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_BANNED_PARTNERS == NODE_B


def test_sorting_does_not_reorder_which_partner_is_tried_first(qapp) -> None:
    """The decision this change was careful about.

    `preferred` is a TRY-ORDER: the plugin opens to the first partner in it
    that accepts. Rows are read in insertion order when Apply builds that list,
    so sorting the table to read a penalty column cannot re-prioritise which
    peer gets the next channel.
    """
    p, wallet, tree = _partners_plugin()
    tab = _sub_tab(p, wallet, PARTNERS_TAB)
    apply_btn = next(b for b in tab.findChildren(QPushButton) if b.text() == "Apply")

    # Prefer all three, unsorted, and record the order that produced.
    for i in range(tree.topLevelItemCount()):
        tree.topLevelItem(i).setCheckState(p._PARTNER_PREF_COL, Qt.CheckState.Checked)
    apply_btn.click()
    unsorted_order = p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_PARTNERS
    assert unsorted_order.count(",") == 2        # premise: all three are in it

    # Now sort the table the other way about and apply again, changing nothing.
    tree = _tree(p, wallet, PARTNERS_TAB)
    _click_header(tree, p._PARTNER_NAME_COL)
    _click_header(tree, p._PARTNER_NAME_COL)
    assert _ids(tree) != [e.strip() for e in unsorted_order.split(",")]  # premise
    next(b for b in _sub_tab(p, wallet, PARTNERS_TAB).findChildren(QPushButton)
         if b.text() == "Apply").click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_PARTNERS == unsorted_order


def test_a_partner_typed_into_the_box_keeps_its_place_at_the_head(qapp) -> None:
    # Hand-written preferred partners come first and stay first: the row-driven
    # ones are appended after them, sorted table or not.
    p, wallet, tree = _partners_plugin()
    tab = _sub_tab(p, wallet, PARTNERS_TAB)
    typed = "02" + "dd" * 32
    tab.findChildren(QPlainTextEdit)[0].setPlainText(f"{typed}@127.0.0.1:9735")
    _click_header(tree, p._PARTNER_NAME_COL)
    for i in range(tree.topLevelItemCount()):
        tree.topLevelItem(i).setCheckState(p._PARTNER_PREF_COL, Qt.CheckState.Checked)
    next(b for b in tab.findChildren(QPushButton) if b.text() == "Apply").click()
    stored = p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_PARTNERS
    assert stored.split(",")[0].strip() == f"{typed}@127.0.0.1:9735"
