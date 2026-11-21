"""Every settings row on every settings pane has hover help.

The labels on these forms are short by necessity -- "Wedged-open: those checks
must span (minutes)" is already as long as a grid label can be -- so for a great
many of them the tooltip IS the explanation, and a row without one leaves the
user guessing at what moves their funds.

What this file pins is COVERAGE rather than wording: a loop over the rows each
pane actually built, asserting each has help on both halves. That shape is
deliberate. The help is inherited from the ConfigVar that declares the setting
(see ``qt._setting_help``), so a setting added later arrives with a tooltip
already attached and this test keeps passing -- whereas a hand-written list of
expected tooltips here would be one more thing to remember to extend, and would
fail by going quietly out of date rather than by going red.

The wording of the individually hand-written tooltips (the dev fee, the
liquidity sink) lives in test_section_help_qt; the two swap-out triggers, whose
text quotes the value in their own box, are pinned here because that behaviour
is the tooltip machinery rather than a fixed string.

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QGridLayout, QLabel, QLineEdit, QTabWidget, QWidget,
)

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_DEV_FEE_PCT, _plugin_config_vars,
)
from electrum.plugins.inbound_liquidity.qt import (  # type: ignore  # noqa: E402
    _setting_help,
)

from test_qt_tab import (  # noqa: E402,F401
    ADVANCED_CONTENT_PANES, _FakeWallet, _FakeWindow, _advanced_pane,
    _make_plugin, _silence_log_tab_timer, qapp,
)

SETTINGS_TAB = 0

# The percentage and sat swap-out triggers, by the label each row carries.
TRIGGER_PCT = "Swap-out trigger (% of capacity)"
TRIGGER_SAT = "Swap-out trigger (sat)"


def _built():
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    return p, wallet


def _settings_tab(plugin, wallet) -> QWidget:
    return plugin._tabs[wallet].container.findChild(QTabWidget).widget(SETTINGS_TAB)


def _rows(pane: QWidget) -> List[Tuple[str, QLabel, QWidget]]:
    """Every ``(label text, label, input)`` row on ``pane``.

    Found the way a user finds a row -- a label in a grid, with an input beside
    it -- rather than from a list this test keeps of its own, so a row added to
    a pane is picked up here without being registered anywhere.

    A grid cell holding a nested LAYOUT rather than a widget is skipped: that is
    the liquidity-goal row, whose amount box and fiat companion share a cell
    (see the goal tests, which cover that row's help).
    """
    out: List[Tuple[str, QLabel, QWidget]] = []
    for grid in pane.findChildren(QGridLayout):
        for i in range(grid.count()):
            item = grid.itemAt(i)
            label = item.widget() if item is not None else None
            if not isinstance(label, QLabel) or not label.text().strip():
                continue
            row, col, _rs, _cs = grid.getItemPosition(i)
            if col != 0:
                continue        # a read-out in column 2, not a row's label
            beside = grid.itemAtPosition(row, 1)
            field = beside.widget() if beside is not None else None
            if not isinstance(field, QLineEdit):
                continue
            out.append((label.text(), label, field))
    return out


def _all_panes(plugin, wallet) -> Dict[str, QWidget]:
    """Every pane that hosts settings rows, by a name for failure messages."""
    panes = {"Settings": _settings_tab(plugin, wallet)}
    for title in ADVANCED_CONTENT_PANES:
        panes[f"Advanced/{title}"] = _advanced_pane(plugin, wallet, title)
    return panes


# --- coverage ------------------------------------------------------------
def test_every_settings_row_has_hover_help(qapp):
    p, wallet = _built()
    missing = []
    for pane_name, pane in _all_panes(p, wallet).items():
        for text, label, field in _rows(pane):
            if not label.toolTip().strip():
                missing.append(f"{pane_name}: {text!r} label")
            if not field.toolTip().strip():
                missing.append(f"{pane_name}: {text!r} input")
    assert not missing, "settings rows with no hover help: " + "; ".join(missing)


def test_both_halves_of_a_row_say_the_same_thing(qapp):
    """A user hovering a setting should not have to find the right half of the
    row to get an answer, nor get two different answers depending which they
    land on."""
    p, wallet = _built()
    disagree = []
    for pane_name, pane in _all_panes(p, wallet).items():
        for text, label, field in _rows(pane):
            if label.toolTip() != field.toolTip():
                disagree.append(f"{pane_name}: {text!r}")
    assert not disagree, "label and input disagree on: " + "; ".join(disagree)


def test_the_panes_actually_have_rows_to_check(qapp):
    """Guards the two tests above against passing vacuously: a `_rows` helper
    that silently stopped matching anything would make them both green."""
    p, wallet = _built()
    counts = {name: len(_rows(pane))
              for name, pane in _all_panes(p, wallet).items()}
    assert counts["Settings"] >= 8, counts
    assert counts["Advanced/General"] >= 6, counts
    assert counts["Advanced/Reliability"] >= 5, counts
    assert counts["Advanced/Channel recovery"] >= 10, counts


def test_help_is_inherited_from_the_config_var_not_rewritten(qapp):
    """The rows nobody wrote bespoke copy for carry their ConfigVar's own
    long_desc verbatim. This is what makes the config docs and the hover help
    one thing rather than two that can drift."""
    p, wallet = _built()
    general = _advanced_pane(p, wallet, "General")
    rows = {text: label for text, label, _f in _rows(general)}
    reserve = next(t for t in rows if t.startswith("On-chain reserve"))
    assert rows[reserve].toolTip() == _setting_help(
        'INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT')
    assert rows[reserve].toolTip(), "the ConfigVar lost its long_desc"


def test_only_internal_bookkeeping_settings_lack_a_long_desc(qapp):
    """The inheritance above only has something to inherit where the ConfigVar
    HAS a long_desc, so the set that lacks one is the blast radius: if a setting
    ever joins it, any form row added for that setting would ship as a bare
    label. Every current member is internal state with no field on any form --
    timestamps, the install id, the "already prompted" flags -- so the set is
    pinned by name. A new entry here is a prompt to write the description, not
    to extend the list.
    """
    undescribed = {attr.replace("INBOUND_LIQUIDITY_", "")
                   for attr in _plugin_config_vars() if not _setting_help(attr)}
    assert undescribed == {
        "TRAMPOLINE_WARNED",
        "UPDATE_CHECK_PROMPTED",
        "UPDATE_LAST_CHECK_TS",
        "UPDATE_LATEST_URL",
        "UPDATE_LATEST_VERSION",
        "USAGE_STATS_INSTALL_ID",
        "USAGE_STATS_LAST_ATTEMPT_TS",
        "USAGE_STATS_LAST_ERROR",
        "USAGE_STATS_LAST_SENT_TS",
        "USAGE_STATS_PROMPTED",
    }


def test_no_settings_tooltip_is_mistaken_for_markup(qapp):
    """Every one of these must reach the user as PLAIN text.

    QToolTip decides per string whether to render markup, via
    ``Qt.mightBeRichText()``, and offers no per-item PlainText switch (the same
    asymmetry ``_literal_tooltip`` exists to work around for text that comes
    from outside). These tooltips are ours, so none of them contains markup
    today -- but they are multi-paragraph, and rich-text rendering COLLAPSES the
    blank lines that separate those paragraphs into spaces. So a future
    description that merely looked tag-like ("values <b>elow this", "a <tag>
    style name") would not just risk interpretation: it would silently reflow
    the whole tooltip into one block.

    Cheap to assert and it fails at the moment such wording is written, rather
    than looking subtly wrong on a form nobody re-reads.
    """
    p, wallet = _built()
    rich = []
    for pane_name, pane in _all_panes(p, wallet).items():
        for text, label, _f in _rows(pane):
            if Qt.mightBeRichText(label.toolTip()):
                rich.append(f"{pane_name}: {text!r}")
    assert not rich, "tooltips Qt would render as markup: " + "; ".join(rich)


def test_multi_paragraph_tooltips_keep_their_paragraph_breaks(qapp):
    """The flip side of the test above: the tooltips that rely on blank lines
    really do still carry them, so a reformat that joined them into one wall of
    text is caught here rather than by eye."""
    p, wallet = _built()
    tab = _settings_tab(p, wallet)
    for prefix in ("Dev fee", "Liquidity sink", "Swap-out trigger (sat)"):
        label = next(lbl for text, lbl, _f in _rows(tab)
                     if text.startswith(prefix))
        assert "\n\n" in label.toolTip(), prefix


def test_a_setting_with_no_long_desc_yields_no_help_rather_than_raising(qapp):
    """_setting_help is called while a tab is being built, and a tab that fails
    to build is far worse than a missing tooltip -- so an unknown or undescribed
    setting must come back empty, not explode."""
    assert _setting_help('INBOUND_LIQUIDITY_USAGE_STATS_PROMPTED') == ""
    assert _setting_help('INBOUND_LIQUIDITY_NO_SUCH_SETTING') == ""
    assert _setting_help('') == ""


# --- the swap-out triggers -----------------------------------------------
def _trigger_row(plugin, wallet, label_text: str) -> Tuple[QLabel, QLineEdit]:
    for text, label, field in _rows(_settings_tab(plugin, wallet)):
        if text == label_text:
            return label, field
    raise AssertionError(f"no settings row labelled {label_text!r}")


def test_the_pct_trigger_explains_itself_in_the_requested_words(qapp):
    p, wallet = _built()
    label, field = _trigger_row(p, wallet, TRIGGER_PCT)
    tip = label.toolTip()
    assert tip.startswith("When the channel is ")
    assert "full or greater, trigger a swap out to make more inbound liquidity" in tip
    assert tip == field.toolTip()


def test_the_sat_trigger_explains_itself_in_the_requested_words(qapp):
    p, wallet = _built()
    label, field = _trigger_row(p, wallet, TRIGGER_SAT)
    tip = label.toolTip()
    assert tip.startswith("When the channel has more than ")
    assert "trigger a swap out to make more inbound liquidity" in tip
    assert tip == field.toolTip()


def test_the_pct_trigger_quotes_the_value_in_its_own_box(qapp):
    p, wallet = _built()
    label, field = _trigger_row(p, wallet, TRIGGER_PCT)
    # The shipped default, as built.
    assert "25% full or greater" in label.toolTip()
    field.setText("40")
    assert "40% full or greater" in label.toolTip()
    assert "40% full or greater" in field.toolTip()
    assert "25%" not in label.toolTip()


def test_the_sat_trigger_quotes_the_value_in_its_own_box(qapp):
    p, wallet = _built()
    label, field = _trigger_row(p, wallet, TRIGGER_SAT)
    assert "more than 25,000 sat" in label.toolTip()
    field.setText("80000")
    # Thousands separated, because this is prose and not the input itself.
    assert "more than 80,000 sat" in label.toolTip()
    assert "more than 80,000 sat" in field.toolTip()


def test_a_trigger_mid_edit_falls_back_to_a_placeholder(qapp):
    """Every keystroke re-renders the tooltip, including the ones that leave the
    box holding nothing usable -- cleared, half-typed, or a stray minus. None of
    those may produce a tooltip that reads as a real threshold, and none may
    raise on the way through."""
    p, wallet = _built()
    pct_label, pct_field = _trigger_row(p, wallet, TRIGGER_PCT)
    sat_label, sat_field = _trigger_row(p, wallet, TRIGGER_SAT)
    junk = [
        "", "   ", "-", "-5", "abc", ".", "1e", "%", "- 5", "+", "0x10",
        # float() accepts all four of these; int() then raises on the first
        # three (ValueError, OverflowError), which would land inside a Qt slot.
        "nan", "inf", "-inf", "1e400",
        # Finite and parseable, but above the total bitcoin supply in sats --
        # a 17-digit figure has no business in a floating window.
        "1e30", "999999999999999999999",
    ]
    for text in junk:
        pct_field.setText(text)
        sat_field.setText(text)
        assert "x% full or greater" in pct_label.toolTip(), text
        assert "more than x sats" in sat_label.toolTip(), text


def test_a_trigger_accepts_the_largest_sane_threshold(qapp):
    # The upper bound rejects nonsense without rejecting a real, if eccentric,
    # setting: the whole supply in sats is exactly on the boundary.
    p, wallet = _built()
    label, field = _trigger_row(p, wallet, TRIGGER_SAT)
    field.setText(str(21_000_000 * 100_000_000))
    assert "more than 2,100,000,000,000,000 sat" in label.toolTip()


def test_a_trigger_tooltip_survives_a_decimal(qapp):
    # The percentage parses as a float, so 12.5 is a legitimate threshold and
    # must not be rendered as "12" or "12.500000".
    p, wallet = _built()
    label, field = _trigger_row(p, wallet, TRIGGER_PCT)
    field.setText("12.5")
    assert "12.5% full or greater" in label.toolTip()


def test_the_sat_trigger_is_honest_about_its_comparison(qapp):
    """The engine fires the sat trigger at ``local_sat > trigger`` -- strictly
    more -- while the percentage one fires at ``>=``. A channel holding exactly
    the sat trigger does NOT swap, so the sentence that states the threshold
    must not claim it is inclusive.

    Asserted on that sentence rather than on the whole tooltip, because the
    paragraph below it rules the inclusive reading out IN those words ('"More
    than", not "at least"') -- which a whole-tooltip search would read as the
    very mistake it exists to prevent.
    """
    p, wallet = _built()
    label, _field = _trigger_row(p, wallet, TRIGGER_SAT)
    claim = label.toolTip().split("\n", 1)[0]
    assert "more than" in claim
    assert "or greater" not in claim
    assert "at least" not in claim
    # And the paragraph below does spell the edge case out.
    assert "exactly this much does not swap" in label.toolTip()


def test_the_triggers_say_they_are_checked_together(qapp):
    # Two triggers either of which can fire is not guessable from two labels.
    p, wallet = _built()
    for label_text in (TRIGGER_PCT, TRIGGER_SAT):
        label, _field = _trigger_row(p, wallet, label_text)
        assert "whichever is reached first wins" in label.toolTip(), label_text


def test_the_triggers_say_a_swap_is_not_guaranteed(qapp):
    """The most likely support question this feature generates: "it is over the
    trigger and nothing happened". The fee ceiling, the minimum swap size and
    the keep-outbound floor can all decline it."""
    p, wallet = _built()
    for label_text in (TRIGGER_PCT, TRIGGER_SAT):
        label, _field = _trigger_row(p, wallet, label_text)
        tip = label.toolTip()
        assert "does not guarantee" in tip, label_text
        assert "Declines tab" in tip, label_text


# --- the dev fee's arithmetic --------------------------------------------
def test_the_dev_fee_help_shows_how_the_number_is_reached(qapp):
    p, wallet = _built()
    tab = _settings_tab(p, wallet)
    row = next((lbl for text, lbl, _f in _rows(tab) if text.startswith("Dev fee")),
               None)
    assert row is not None, "no dev-fee row"
    tip = row.toolTip()
    assert "amount drained × the percentage here" in tip
    assert "rounded down to whole sats" in tip


def test_the_dev_fee_worked_example_matches_the_real_calculation(qapp):
    """The example is quoted at the shipped default, so it must agree with what
    a fresh wallet would actually charge -- computed by the same function the
    accrual path uses, not by hand."""
    from electrum.plugins.inbound_liquidity.liquidity_manager import compute_dev_fee
    p, wallet = _built()
    tab = _settings_tab(p, wallet)
    row = next(lbl for text, lbl, _f in _rows(tab) if text.startswith("Dev fee"))
    expected = compute_dev_fee(100_000, DEFAULT_DEV_FEE_PCT)
    assert f"draining 100,000 sat accrues {expected:,} sat" in row.toolTip()
    # And the default really is what the example claims it is.
    assert p.settings(wallet).INBOUND_LIQUIDITY_DEV_FEE_PCT == DEFAULT_DEV_FEE_PCT


def test_the_dev_fee_help_says_what_the_basis_actually_is(qapp):
    """The fee is charged on the NET on-chain receipt -- after the provider's
    percentage fee, its mining fee and our own claim-tx fee (Electrum's
    get_recv_amount subtracts all three). Saying "the amount drained" without
    that reads as the gross, which would overstate the fee."""
    p, wallet = _built()
    tab = _settings_tab(p, wallet)
    row = next(lbl for text, lbl, _f in _rows(tab) if text.startswith("Dev fee"))
    tip = row.toolTip()
    assert "net on-chain amount" in tip
    assert "claim-transaction fee" in tip
    assert "a swap that fails accrues" in tip
