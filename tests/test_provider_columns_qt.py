"""The Swap providers table's Successes / Attempts columns, as rendered.

The counting itself is pinned in test_provider_attempts; this is about the tab:
that the two tallies have their own headers (rather than one ambiguous "OK"),
that each row shows the provider's own figures, that an offline row -- which
advertises no terms and so has a shorter cell list -- still lines up with the
headers, and that both columns sort numerically rather than as text.

That last one is not pedantry: every cell here is a string, so "10" sorts before
"9" unless the column was given the number behind it.

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QTreeWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import RELIABILITY_DB_KEY  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)
from test_sortable_columns_qt import (  # noqa: E402
    PROVIDERS_TAB, _click_header, _column, _ids, _tree,
)


def _with_history(history):
    """A providers tab for three live providers, seeded with the given
    ``npub -> {field: value}`` reliability rows."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._last_offers[wallet] = [
        ProviderOffer(npub=npub, percentage_fee=0.5, mining_fee_sat=100,
                      min_amount_sat=1_000, max_reverse_sat=1_000_000, pow_bits=0)
        for npub in history
    ]
    wallet.db.put(RELIABILITY_DB_KEY, dict(history))
    p._add_liquidity_tab(window, wallet)
    return p, wallet, _tree(p, wallet, PROVIDERS_TAB)


# --- the headers ---------------------------------------------------------
def test_the_two_tallies_have_their_own_headers(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tree = _tree(p, wallet, PROVIDERS_TAB)
    header = tree.headerItem()
    labels = [header.text(i) for i in range(tree.columnCount())]
    assert labels[p._SUCCESS_COL] == "Successes"
    assert labels[p._ATTEMPTS_COL] == "Attempts"
    assert labels[p._FAULTS_COL] == "Faults"
    # The ambiguous old name is gone, not merely moved.
    assert "OK" not in labels


def test_every_column_has_a_header(qapp):
    # Inserting a column shifted three of them; a cell list one short of the
    # headers would silently misalign the whole right-hand side of the table.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tree = _tree(p, wallet, PROVIDERS_TAB)
    header = tree.headerItem()
    assert tree.columnCount() == p._LAST_FAULT_COL + 1
    assert all(header.text(i) for i in range(tree.columnCount()))


# --- the cells -----------------------------------------------------------
def test_each_row_shows_its_own_tallies(qapp):
    p, _wallet, tree = _with_history({
        "npubGOOD": {"success_count": 4, "attempt_count": 5, "fault_count": 1},
        "npubNEW": {},
    })
    by_npub = {tree.topLevelItem(i).data(0, 0x0100): tree.topLevelItem(i)
               for i in range(tree.topLevelItemCount())}
    good = by_npub["npubGOOD"]
    assert good.text(p._SUCCESS_COL) == "4"
    assert good.text(p._ATTEMPTS_COL) == "5"
    assert good.text(p._FAULTS_COL) == "1"
    new = by_npub["npubNEW"]
    assert new.text(p._SUCCESS_COL) == "0"
    assert new.text(p._ATTEMPTS_COL) == "0"


def test_a_never_tried_provider_reads_zero_rather_than_blank(qapp):
    # "" in a tally column reads as "unknown"; 0 is the honest answer and the
    # one that sorts.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._last_offers[wallet] = [
        ProviderOffer(npub="npubFRESH", percentage_fee=0.5, mining_fee_sat=1,
                      min_amount_sat=1, max_reverse_sat=1)]
    p._add_liquidity_tab(window, wallet)
    tree = _tree(p, wallet, PROVIDERS_TAB)
    assert _column(tree, p._ATTEMPTS_COL) == ["0"]


def test_an_offline_row_still_lines_up_with_the_headers(qapp):
    # An offline provider advertises no terms, so its cell list is built from a
    # different (shorter) prefix -- the tallies must still land in their columns.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    wallet.db.put(RELIABILITY_DB_KEY, {
        "npubGONE": {"success_count": 2, "attempt_count": 7, "fault_count": 3}})
    p._add_liquidity_tab(window, wallet)
    tree = _tree(p, wallet, PROVIDERS_TAB)
    item = tree.topLevelItem(0)
    assert item.text(1) == "(offline)"
    assert item.text(p._SUCCESS_COL) == "2"
    assert item.text(p._ATTEMPTS_COL) == "7"
    assert item.text(p._FAULTS_COL) == "3"


# --- sorting -------------------------------------------------------------
def test_attempts_sorts_numerically_not_as_text(qapp):
    p, _wallet, tree = _with_history({
        "npubTWO": {"attempt_count": 2},
        "npubTEN": {"attempt_count": 10},
        "npubNINE": {"attempt_count": 9},
    })
    _click_header(tree, p._ATTEMPTS_COL)
    assert _column(tree, p._ATTEMPTS_COL) == ["2", "9", "10"]
    _click_header(tree, p._ATTEMPTS_COL)                # reverse
    assert _column(tree, p._ATTEMPTS_COL) == ["10", "9", "2"]


def test_successes_sorts_numerically_not_as_text(qapp):
    p, _wallet, tree = _with_history({
        "npubTWO": {"success_count": 2},
        "npubTEN": {"success_count": 10},
        "npubNINE": {"success_count": 9},
    })
    _click_header(tree, p._SUCCESS_COL)
    assert _ids(tree) == ["npubTWO", "npubNINE", "npubTEN"]


def test_faults_still_sorts_from_its_new_position(qapp):
    # The inserted column shifted Faults by one; its sort key had to move with
    # it or the column would have started ordering by Attempts.
    p, _wallet, tree = _with_history({
        "npubA": {"fault_count": 1, "attempt_count": 9},
        "npubB": {"fault_count": 5, "attempt_count": 1},
    })
    _click_header(tree, p._FAULTS_COL)
    assert _ids(tree) == ["npubA", "npubB"]
