"""GUI tests for the liquidity-goal knob on the Settings tab.

This is the one amount on that tab entered as MONEY rather than as a bare sat
count, so it uses Electrum's own BTCAmountEdit: it renders and parses in whatever
base unit the user has selected, with an optional fiat companion. What is tested
here is that the displayed unit actually tracks the user's choice, that the round
trip through Apply always stores SATS, and that a cleared field cannot silently
switch the feature off.

Needs PyQt6 and Electrum's Qt GUI; skipped when either is unavailable."""
from __future__ import annotations

import os
from typing import Optional

import pytest

from conftest import seed_settings  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QPushButton, QScrollArea, QTabWidget,
)

from electrum.gui.qt.amountedit import AmountEdit, BTCAmountEdit  # noqa: E402
from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_LIQUIDITY_GOAL_SAT,
)
from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402

TOOLTIP = ("Automatically close small channels and re-open bigger channels "
           "if we have sufficient funds to reach this goal")

# Electrum's base units, as decimal_point values (electrum.util.base_units).
BTC, MBTC, BITS, SAT = 8, 5, 2, 0


# The Settings tab builds the Advanced sub-tab too, which reads every one of the
# plugin's ConfigVars -- so the config/wallet/plugin fakes are reused wholesale
# from the main Qt tab tests rather than duplicated (and left to drift) here.
from test_qt_tab import (  # type: ignore  # noqa: E402
    _FakeStatusBar, _FakeWallet, _make_plugin as _make_base_plugin,
)


class _FakeFx:
    """Just enough of Electrum's FxThread for the fiat companion field."""

    def __init__(self, enabled: bool = False, ccy: str = "USD") -> None:
        self.enabled = enabled
        self.ccy = ccy

    def is_enabled(self) -> bool:
        return self.enabled

    def get_currency(self) -> str:
        return self.ccy


class _FakeWindow:
    def __init__(self, *, decimal_point: int = BTC, fx: Optional[_FakeFx] = None) -> None:
        self.tabs = QTabWidget()
        self.decimal_point = decimal_point
        self.fx = fx
        self._sb = _FakeStatusBar()
        self.connected = []

    def statusBar(self) -> _FakeStatusBar:
        return self._sb

    def get_decimal_point(self) -> int:
        return self.decimal_point

    def connect_fields(self, btc_e, fiat_e) -> None:
        # Record the pairing; the real conversion needs live exchange rates.
        self.connected.append((btc_e, fiat_e))


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _make_plugin() -> qt_mod.Plugin:
    p = _make_base_plugin()
    # The shipped default, so the unit-rendering cases have a known amount.
    p.config.INBOUND_LIQUIDITY_GOAL_SAT = DEFAULT_LIQUIDITY_GOAL_SAT
    # Settings are per wallet now; hand this plugin's wallets what its
    # config carries -- see conftest.seed_settings.
    seed_settings(p)
    return p


def _build(decimal_point: int = BTC, fx: Optional[_FakeFx] = None):
    p = _make_plugin()
    window, wallet = _FakeWindow(decimal_point=decimal_point, fx=fx), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    settings_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
    return p, window, wallet, settings_tab


def _goal_edit(settings_tab) -> BTCAmountEdit:
    edits = settings_tab.findChildren(BTCAmountEdit)
    assert len(edits) == 1, "expected exactly one BTC amount field on Settings"
    return edits[0]


def _apply(settings_tab) -> None:
    next(b for b in settings_tab.findChildren(QPushButton)
         if b.text() == "Apply").click()


def _status_text(settings_tab) -> str:
    return " ".join(lbl.text() for lbl in settings_tab.findChildren(QLabel))


# --- the field exists, is labelled, and carries the tooltip ---------------
def test_goal_field_is_present_and_labelled(qapp) -> None:
    _, _, _, tab = _build()
    assert _goal_edit(tab) is not None
    assert any(lbl.text() == "Liquidity goal" for lbl in tab.findChildren(QLabel))


def test_tooltip_is_the_specified_text(qapp) -> None:
    _, _, _, tab = _build()
    assert _goal_edit(tab).toolTip() == TOOLTIP
    label = next(lbl for lbl in tab.findChildren(QLabel)
                 if lbl.text() == "Liquidity goal")
    assert label.toolTip() == TOOLTIP


# --- it renders in the user's selected base unit --------------------------
@pytest.mark.parametrize("decimal_point,shown", [
    (BTC, "0.001"),          # 100,000 sat as BTC
    (MBTC, "1"),             # ...as mBTC
    (BITS, "1000"),          # ...as bits
    (SAT, "100000"),         # ...as sat
])
def test_goal_renders_in_the_selected_base_unit(qapp, decimal_point, shown) -> None:
    _, _, _, tab = _build(decimal_point=decimal_point)
    assert _goal_edit(tab).text() == shown


def test_base_unit_name_is_shown_on_the_field(qapp) -> None:
    # The widget paints its unit name; check the accessor the painter uses, so
    # the user can tell BTC from sat at a glance.
    _, _, _, tab = _build(decimal_point=SAT)
    assert _goal_edit(tab).base_unit() == "sat"
    _, _, _, tab = _build(decimal_point=BTC)
    assert _goal_edit(tab).base_unit() == "BTC"


# --- the stored value is always sats, whatever the display unit -----------
@pytest.mark.parametrize("decimal_point,typed,expected_sat", [
    (BTC, "0.005", 500_000),
    (MBTC, "5", 500_000),
    (BITS, "5000", 500_000),
    (SAT, "500000", 500_000),
])
def test_apply_stores_sats_from_any_unit(qapp, decimal_point, typed,
                                         expected_sat) -> None:
    p, _, wallet, tab = _build(decimal_point=decimal_point)
    _goal_edit(tab).setText(typed)
    _apply(tab)
    assert p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT == expected_sat


def test_applied_value_is_reloaded_into_the_field(qapp) -> None:
    p, _, wallet, tab = _build(decimal_point=SAT)
    _goal_edit(tab).setText("250000")
    _apply(tab)
    assert p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT == 250_000
    assert _goal_edit(tab).text() == "250000"


def test_zero_is_accepted_and_disables_the_feature(qapp) -> None:
    p, _, wallet, tab = _build(decimal_point=SAT)
    _goal_edit(tab).setText("0")
    _apply(tab)
    assert p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT == 0


def test_empty_field_is_rejected_rather_than_read_as_zero(qapp) -> None:
    """A cleared box must not silently switch the feature off -- that is a
    destructive change to make by accident. Turning it off takes an explicit 0."""
    p, _, wallet, tab = _build(decimal_point=SAT)
    before = p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT
    _goal_edit(tab).setText("")
    _apply(tab)
    assert p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT == before
    assert "Invalid value for" in _status_text(tab)


def test_invalid_goal_blocks_the_whole_apply(qapp) -> None:
    # Everything is parsed before anything is persisted, so a bad goal must not
    # leave the other fields half-saved.
    p, _, wallet, tab = _build(decimal_point=SAT)
    before_channels = p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS
    from PyQt6.QtWidgets import QLineEdit
    plain = [e for e in tab.findChildren(QLineEdit) if type(e) is QLineEdit]
    plain[1].setText("7")           # max-channels
    _goal_edit(tab).setText("")
    _apply(tab)
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == before_channels


# --- the fiat companion ---------------------------------------------------
def test_fiat_field_is_paired_when_fx_is_enabled(qapp) -> None:
    fx = _FakeFx(enabled=True, ccy="USD")
    _, window, _, tab = _build(fx=fx)
    fiat = [e for e in tab.findChildren(AmountEdit)
            if not isinstance(e, BTCAmountEdit)]
    assert len(fiat) == 1
    assert fiat[0].isVisible() or fiat[0].isVisibleTo(tab)
    assert (_goal_edit(tab), fiat[0]) in window.connected


def test_fiat_field_is_hidden_when_fx_is_disabled(qapp) -> None:
    fx = _FakeFx(enabled=False)
    _, _, _, tab = _build(fx=fx)
    fiat = [e for e in tab.findChildren(AmountEdit)
            if not isinstance(e, BTCAmountEdit)]
    assert len(fiat) == 1
    assert not fiat[0].isVisibleTo(tab)


def test_no_fiat_field_without_an_fx_at_all(qapp) -> None:
    _, _, _, tab = _build(fx=None)
    fiat = [e for e in tab.findChildren(AmountEdit)
            if not isinstance(e, BTCAmountEdit)]
    assert fiat == []


# --- robustness -----------------------------------------------------------
def test_tab_still_builds_on_a_window_without_get_decimal_point(qapp) -> None:
    """A settings tab that fails to build is far worse than one showing the
    wrong unit, so the decimal-point lookup falls back to BTC."""
    class _OldWindow(_FakeWindow):
        get_decimal_point = None      # as if the attribute were absent

        def __getattribute__(self, name):
            if name == "get_decimal_point":
                raise AttributeError(name)
            return super().__getattribute__(name)

    p = _make_plugin()
    window, wallet = _OldWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
    assert _goal_edit(tab).text().startswith("0.001")    # 100k sat as BTC


# --- base-unit changes reach this field too -------------------------------
class _FakeSignal:
    """Minimal stand-in for the QApplication signals Electrum emits when the
    base unit or the fiat setting changes."""

    def __init__(self) -> None:
        self._slots = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self) -> None:
        for slot in list(self._slots):
            slot()


class _FakeApp:
    def __init__(self) -> None:
        self.refresh_amount_edits_signal = _FakeSignal()
        self.update_fiat_signal = _FakeSignal()


def test_base_unit_change_re_renders_from_the_stored_value(qapp) -> None:
    """Electrum's own refresh_amount_edits only knows about the Send and Receive
    tabs, and it *reinterprets* the typed number under the new unit. A stored
    setting must instead be re-rendered from config: 100 000 sat is still
    100 000 sat after you switch units, just displayed differently."""
    p = _make_plugin()
    window, wallet = _FakeWindow(decimal_point=SAT), _FakeWallet()
    window.app = _FakeApp()
    p._add_liquidity_tab(window, wallet)
    tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
    assert _goal_edit(tab).text() == "100000"

    window.decimal_point = BTC          # user switches the base unit
    window.app.refresh_amount_edits_signal.emit()
    assert _goal_edit(tab).text() == "0.001"
    # ...and the stored value never moved.
    assert p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT == 100_000


def test_fiat_visibility_follows_the_update_fiat_signal(qapp) -> None:
    fx = _FakeFx(enabled=False)
    p = _make_plugin()
    window, wallet = _FakeWindow(fx=fx), _FakeWallet()
    window.app = _FakeApp()
    p._add_liquidity_tab(window, wallet)
    tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
    fiat = [e for e in tab.findChildren(AmountEdit)
            if not isinstance(e, BTCAmountEdit)][0]
    assert not fiat.isVisibleTo(tab)
    fx.enabled = True
    window.app.update_fiat_signal.emit()
    assert fiat.isVisibleTo(tab)


def test_signal_after_the_tab_is_gone_does_not_raise(qapp) -> None:
    """The slot lives on the long-lived QApplication but its widgets belong to
    one wallet's tab, and a plain closure is not something Qt can auto-disconnect.
    Closing a wallet and then changing the base unit must not raise."""
    p = _make_plugin()
    window, wallet = _FakeWindow(decimal_point=SAT), _FakeWallet()
    window.app = _FakeApp()
    p._add_liquidity_tab(window, wallet)
    tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
    goal_e = _goal_edit(tab)
    from PyQt6 import sip
    sip.delete(goal_e)                  # as Qt does when the tab is torn down
    window.app.refresh_amount_edits_signal.emit()   # must not raise


# --- where the fiat companion sits ---------------------------------------
# The fiat box restates the amount beside it, so it has to read as part of that
# field. It used to sit in its own grid column, which put it most of a window
# width to the right of the sats it was restating -- and nothing was stretching
# it there: Electrum's AmountEdit is a fixed ~144px while the plain QLineEdits
# on the rows above expand, so column 1 was sized by THEM and column 2 began
# after the whole empty remainder. The gap therefore GREW with the window, which
# is why these cases measure a default-sized window and a roomy one: at 840px
# the old layout was wrong by ~236px, at 1280px by ~690px, and a single-size
# test could have been satisfied by a fix that still drifted apart on a big
# screen.
#
# Measuring needs a real layout pass, which needs some care -- see the header of
# test_no_clipped_text_qt for why WA_DontShowOnScreen is how that is had under
# the offscreen platform.
MAX_PAIR_GAP_PX = 24            # a layout spacing, not a column's remainder


def _laid_out_goal_row(qapp, width: int, height: int):
    """``(sat box, fiat box)`` from a Settings tab laid out for real."""
    fx = _FakeFx(enabled=True, ccy="USD")
    p = _make_plugin()
    window, wallet = _FakeWindow(decimal_point=SAT, fx=fx), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tabs = p._tabs[wallet].container.findChild(QTabWidget)

    host = window.tabs
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    host.show()
    host.resize(width, height)
    tabs.setCurrentIndex(0)
    for _ in range(5):
        qapp.processEvents()

    page = tabs.widget(0)
    body = page.findChild(QScrollArea)
    scope = body.widget() if body is not None else page
    sat_box = scope.findChildren(BTCAmountEdit)[0]
    fiat_box = [e for e in scope.findChildren(AmountEdit)
                if not isinstance(e, BTCAmountEdit)][0]
    return sat_box, fiat_box


@pytest.mark.parametrize("width,height", [(840, 400), (1280, 1024)])
def test_fiat_box_sits_immediately_beside_the_sat_box(qapp, width, height) -> None:
    sat_box, fiat_box = _laid_out_goal_row(qapp, width, height)
    sat_right = sat_box.mapTo(sat_box.window(), sat_box.rect().topRight()).x()
    fiat_left = fiat_box.mapTo(fiat_box.window(), fiat_box.rect().topLeft()).x()
    gap = fiat_left - sat_right
    assert 0 <= gap <= MAX_PAIR_GAP_PX, (
        f"fiat box is {gap}px from the sat box at {width}x{height}")


def test_the_two_boxes_share_a_row(qapp) -> None:
    # Beside it, not under it: a gap measured horizontally means nothing if the
    # layout has wrapped the fiat box onto the next line.
    sat_box, fiat_box = _laid_out_goal_row(qapp, 1280, 1024)
    sat_mid = sat_box.mapTo(sat_box.window(), sat_box.rect().center()).y()
    fiat_mid = fiat_box.mapTo(fiat_box.window(), fiat_box.rect().center()).y()
    assert abs(sat_mid - fiat_mid) <= 2


def test_the_gap_does_not_grow_with_the_window(qapp) -> None:
    """The regression itself: the old layout's gap was column 1's unused
    remainder, so it widened as the window did. A fix that merely narrowed it at
    one size would still fail here."""
    _, _ = _laid_out_goal_row(qapp, 840, 400)
    gaps = []
    for width, height in [(840, 400), (1280, 1024)]:
        sat_box, fiat_box = _laid_out_goal_row(qapp, width, height)
        sat_right = sat_box.mapTo(sat_box.window(), sat_box.rect().topRight()).x()
        fiat_left = fiat_box.mapTo(fiat_box.window(), fiat_box.rect().topLeft()).x()
        gaps.append(fiat_left - sat_right)
    assert gaps[0] == gaps[1], f"gap moved with the window: {gaps}"


def test_the_sat_box_does_not_move_when_fiat_is_off(qapp) -> None:
    """A hidden widget is ignored by the layout, so switching fiat off must
    leave the row exactly where it was rather than re-centring it."""
    with_fiat, _ = _laid_out_goal_row(qapp, 1280, 1024)
    x_with = with_fiat.mapTo(with_fiat.window(),
                             with_fiat.rect().topLeft()).x()

    p = _make_plugin()
    window, wallet = _FakeWindow(decimal_point=SAT,
                                 fx=_FakeFx(enabled=False)), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tabs = p._tabs[wallet].container.findChild(QTabWidget)
    host = window.tabs
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    host.show()
    host.resize(1280, 1024)
    tabs.setCurrentIndex(0)
    for _ in range(5):
        qapp.processEvents()
    page = tabs.widget(0)
    body = page.findChild(QScrollArea)
    scope = body.widget() if body is not None else page
    without = scope.findChildren(BTCAmountEdit)[0]
    assert without.mapTo(without.window(), without.rect().topLeft()).x() == x_with
