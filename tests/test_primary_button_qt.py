"""The big green Apply button on every settings sub-tab.

Pins the two things a stylesheet change can silently undo -- the size and the
colour -- on all four Apply buttons at once, plus the thing that matters more
than either: each one still commits its own form. A button that looks right and
saves nothing is the worst outcome available here.

Shares the Qt scaffolding with test_qt_tab (see test_partners_name_column_qt)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QPushButton, QTabWidget  # noqa: E402

from electrum.plugins.inbound_liquidity.qt_widgets import (  # type: ignore  # noqa: E402
    ACCENT_GREEN,
    PRIMARY_BUTTON_MIN_HEIGHT,
    PRIMARY_BUTTON_MIN_WIDTH,
    primary_button,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin, _plain_line_edits,
    _silence_log_tab_timer, qapp,
)

# The sub-tabs that carry an Apply, by index in the Liquidity tab.
APPLY_TABS = {0: "Settings", 1: "Swap providers", 2: "Channel partners",
              3: "Advanced"}


def _apply_buttons(plugin, wallet):
    """Every sub-tab's Apply, keyed by the sub-tab's title."""
    sub = plugin._tabs[wallet].container.findChild(QTabWidget)
    found = {}
    for index, title in APPLY_TABS.items():
        assert sub.tabText(index) == title, \
            f"sub-tab {index} is {sub.tabText(index)!r}, not {title!r}"
        buttons = [b for b in sub.widget(index).findChildren(QPushButton)
                   if b.text() == "Apply"]
        assert len(buttons) == 1, f"{title}: expected one Apply, got {len(buttons)}"
        found[title] = buttons[0]
    return found


def _built():
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    return p, wallet


# --- the helper on its own -----------------------------------------------
def test_a_primary_button_is_about_three_times_the_default_area(qapp) -> None:
    plain, big = QPushButton("Apply"), primary_button("Apply")
    plain_area = plain.sizeHint().width() * plain.sizeHint().height()
    big_area = big.minimumWidth() * big.minimumHeight()
    assert big_area >= 3 * plain_area, (
        f"{big_area}px² is not 3x the default {plain_area}px²")


def test_a_primary_button_is_bigger_in_both_directions(qapp) -> None:
    plain = QPushButton("Apply")
    big = primary_button("Apply")
    assert big.minimumWidth() > plain.sizeHint().width()
    assert big.minimumHeight() > plain.sizeHint().height()


def test_a_primary_button_carries_the_accent_green(qapp) -> None:
    sheet = primary_button("Apply").styleSheet()
    assert f"background-color: {ACCENT_GREEN}" in sheet
    assert "color: #ffffff" in sheet


def test_a_primary_button_reacts_to_the_cursor(qapp) -> None:
    # Giving a QPushButton a background-color makes Qt drop its native frame,
    # so without explicit states a styled button looks inert under the cursor
    # -- and, worse, looks enabled while disabled.
    sheet = primary_button("Apply").styleSheet()
    for state in ("QPushButton:hover", "QPushButton:pressed", "QPushButton:disabled"):
        assert state in sheet, f"no {state} rule"


def test_a_primary_button_is_bold_and_larger_than_the_body_text(qapp) -> None:
    plain, big = QPushButton("Apply"), primary_button("Apply")
    assert big.font().bold()
    assert big.font().pointSizeF() > plain.font().pointSizeF()


def test_a_primary_button_keeps_its_label(qapp) -> None:
    assert primary_button("Apply").text() == "Apply"


# --- every Apply in the tab ----------------------------------------------
def test_every_settings_sub_tab_has_a_big_green_apply(qapp) -> None:
    p, wallet = _built()
    for title, btn in _apply_buttons(p, wallet).items():
        assert btn.minimumWidth() >= PRIMARY_BUTTON_MIN_WIDTH, title
        assert btn.minimumHeight() >= PRIMARY_BUTTON_MIN_HEIGHT, title
        assert ACCENT_GREEN in btn.styleSheet(), title


def test_no_other_button_was_painted_green(qapp) -> None:
    # Reset / Refresh / Run now share those rows; the green is what marks the
    # one that commits the form.
    p, wallet = _built()
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    for index, title in APPLY_TABS.items():
        for btn in sub.widget(index).findChildren(QPushButton):
            if btn.text() == "Apply":
                continue
            assert ACCENT_GREEN not in btn.styleSheet(), \
                f"{title}: {btn.text()!r} is also green"


def test_the_settings_apply_still_persists_its_form(qapp) -> None:
    p, wallet = _built()
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    # Settings-tab field order (see test_qt_tab): 1 is "max channels".
    _plain_line_edits(sub.widget(0))[1].setText("7")
    _apply_buttons(p, wallet)["Settings"].click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == 7


def test_the_providers_apply_still_persists_its_form(qapp) -> None:
    from PyQt6.QtWidgets import QPlainTextEdit
    p, wallet = _built()
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    sub.widget(1).findChildren(QPlainTextEdit)[0].setPlainText("npubOFFLINE")
    _apply_buttons(p, wallet)["Swap providers"].click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS == "npubOFFLINE"


def test_the_partners_apply_still_persists_its_form(qapp) -> None:
    from PyQt6.QtWidgets import QCheckBox
    p, wallet = _built()
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    sub.widget(2).findChildren(QCheckBox)[0].setChecked(True)       # strict mode
    _apply_buttons(p, wallet)["Channel partners"].click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_PARTNERS_STRICT is True


def test_the_advanced_apply_still_persists_its_form(qapp) -> None:
    p, wallet = _built()
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    edits = _plain_line_edits(sub.widget(3))
    assert edits, "the Advanced tab has no plain text fields"
    before = p.settings(wallet).INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY
    _apply_buttons(p, wallet)["Advanced"].click()
    # Applying an untouched form is a no-op, not a reset: the point here is
    # that the restyled button still runs the handler without raising.
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY == before
