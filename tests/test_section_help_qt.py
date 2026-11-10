"""Hover help and on-screen wording on the settings tabs.

Two of the Settings tab's fields charge or move money in ways their one-line
labels cannot explain -- the dev fee and the liquidity sink -- so each carries a
tooltip, on BOTH halves of its row: a user hovering over a section should not
have to find the right half of it to get an answer.

Also pinned here is the Channel partners tab's description of its own table,
which used to claim the rows were peers you already have a channel with. They
are not: the table also lists peers with a fault history and no channel, which
is precisely where a user goes looking after a failed open.

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QLabel, QLineEdit, QTabWidget  # noqa: E402

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)

SETTINGS_TAB = 0
PARTNERS_TAB = 2

SINK_HELP = ("Instead of doing a swap-out to obtain inbound liquidity, "
             "utilize a liquidity sink instead")


def _tab(plugin, wallet, index):
    return plugin._tabs[wallet].container.findChild(QTabWidget).widget(index)


def _built():
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    return p, wallet


def _row_widgets(tab, label_fragment: str):
    """The label and the input of the settings row whose label contains
    ``label_fragment``, found the way a user finds them: by reading the label and
    looking at what sits next to it.

    The grid has to be located by searching the tab's layouts rather than asking
    the label for its own: a widget in a QGridLayout is parented to the widget
    that OWNS the layout, so ``label.parentWidget().layout()`` is the tab's outer
    QVBoxLayout, not the grid the label is actually in.
    """
    from PyQt6.QtWidgets import QGridLayout
    for grid in tab.findChildren(QGridLayout):
        for i in range(grid.count()):
            item = grid.itemAt(i)
            widget = item.widget() if item is not None else None
            if not isinstance(widget, QLabel) or label_fragment not in widget.text():
                continue
            row, col, _rs, _cs = grid.getItemPosition(i)
            neighbour = grid.itemAtPosition(row, col + 1)
            assert neighbour is not None, f"{label_fragment!r} has no input beside it"
            return widget, neighbour.widget()
    raise AssertionError(f"no settings row labelled {label_fragment!r}")


def _tab_texts(tab) -> str:
    return "\n".join(w.text() for w in tab.findChildren(QLabel))


# --- the dev fee ---------------------------------------------------------
def test_the_dev_fee_row_explains_itself(qapp):
    p, wallet = _built()
    label, field = _row_widgets(_tab(p, wallet, SETTINGS_TAB), "Dev fee")
    tip = label.toolTip()
    assert tip
    assert tip == field.toolTip(), "the label and its input disagree"
    # What it is, what it is charged on, and the two things that bound it.
    assert "development" in tip
    assert "reverse swap" in tip and "liquidity sink" in tip
    assert "1,000 sat" in tip and "10,000 sat" in tip
    assert "0 to" in tip or "0 =" in tip or "set it to 0" in tip


def test_the_dev_fee_tooltip_says_it_only_touches_plugin_drains(qapp):
    # The question a user actually has: is this taking a cut of MY payments?
    p, wallet = _built()
    label, _field = _row_widgets(_tab(p, wallet, SETTINGS_TAB), "Dev fee")
    assert "Your own payments are never touched" in label.toolTip()


def test_the_dev_fee_readout_carries_the_same_explanation(qapp):
    # The input row and the "x sat owed" read-out are one section to a user
    # hovering over them.
    p, wallet = _built()
    tab = _tab(p, wallet, SETTINGS_TAB)
    label, _field = _row_widgets(tab, "Dev fee (")
    readouts = [w for w in tab.findChildren(QLabel)
                if w.text().startswith("Dev fee:")]
    assert readouts, "the dev-fee read-out is missing"
    assert readouts[0].toolTip() == label.toolTip()


# --- the liquidity sink --------------------------------------------------
def test_the_liquidity_sink_row_explains_itself(qapp):
    p, wallet = _built()
    label, field = _row_widgets(_tab(p, wallet, SETTINGS_TAB), "Liquidity sink")
    assert label.toolTip() == SINK_HELP
    assert field.toolTip() == SINK_HELP


def test_the_sink_help_is_on_the_input_a_user_would_hover(qapp):
    # Specifically the QLineEdit, not just the label beside it.
    p, wallet = _built()
    _label, field = _row_widgets(_tab(p, wallet, SETTINGS_TAB), "Liquidity sink")
    assert isinstance(field, QLineEdit)
    assert field.toolTip() == SINK_HELP


def test_the_sink_switches_keep_their_own_fuller_help(qapp):
    # The two checkboxes under the address have longer, more specific tooltips
    # of their own; the section-level wording must not have flattened them.
    from PyQt6.QtWidgets import QCheckBox
    p, wallet = _built()
    boxes = [cb for cb in _tab(p, wallet, SETTINGS_TAB).findChildren(QCheckBox)
             if "liquidity sink" in cb.text() or "submarine swaps" in cb.text()]
    assert len(boxes) == 2
    for cb in boxes:
        assert cb.toolTip() and cb.toolTip() != SINK_HELP


# --- the channel partners table's description ----------------------------
def test_the_partners_table_is_described_as_peers_seen_or_tried(qapp):
    p, wallet = _built()
    text = _tab_texts(_tab(p, wallet, PARTNERS_TAB))
    assert "These rows below are peers we have seen or tried" in text


def test_the_partners_table_no_longer_claims_they_all_have_channels(qapp):
    # The old wording sent a user looking for a failed peer to the wrong place.
    p, wallet = _built()
    text = _tab_texts(_tab(p, wallet, PARTNERS_TAB))
    assert "ALREADY have a channel with" not in text


def test_the_partners_table_still_says_what_ticking_a_row_does(qapp):
    # The clause that was replaced sat in a paragraph that also explained the
    # tick-boxes and the free-text box; that half is still needed.
    p, wallet = _built()
    text = _tab_texts(_tab(p, wallet, PARTNERS_TAB))
    assert "prefer/ban that node" in text
    assert "Preferred partners" in text
