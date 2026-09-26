"""GUI tests for the liquidity-goal buffer: the blue pie slice and the Send
tab's inline warning.

Two surfaces, neither of which Electrum fully hooks:

  * the balance pie chart (status bar + Wallet Balance dialog), reached by
    WRAPPING Electrum's own list-building so nothing is duplicated; and
  * the Send tab warning, which does use a real hook (``create_send_tab``) and
    is a plain inline label -- no dialog, no confirmation, no extra click.

The risky parts are covered here: that the wrappers find and rewrite the right
entries without losing satoshis, that they stay out of the way when there is no
buffer, that the buffer's blue is not Electrum's frozen-blue, and that the Send
tab's label tracks the amount box on both rails.

Needs PyQt6 and Electrum's Qt GUI; skipped when either is unavailable."""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.balance_dialog")

from PyQt6.QtCore import pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from electrum.gui.qt.balance_dialog import (  # noqa: E402
    COLOR_CONFIRMED, COLOR_FROZEN, COLOR_FROZEN_LIGHTNING, COLOR_LIGHTNING,
    COLOR_UNCONFIRMED, COLOR_UNMATURED, BalanceDialog, LegendWidget,
    PieChartWidget,
)
from electrum.gui.qt.amountedit import BTCAmountEdit  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin, format_goal_buffer_warning,
)
from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402

from test_goal_buffer_glue import (  # type: ignore  # noqa: E402
    _FakePiechartBalance, _FakeWallet, _plugin as _glue_plugin,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_globals():
    """The buffer provider and the Send-tab refresher are module globals (the
    class patches outlive any one wallet), so every test starts from "no plugin
    loaded" and leaves it that way."""
    qt_mod._set_buffer_provider(None)
    qt_mod._set_send_warning_refresher(None)
    yield
    qt_mod._set_buffer_provider(None)
    qt_mod._set_send_warning_refresher(None)


def _stock_entries(*, confirmed=0, lightning=0, frozen=0, unconfirmed=0,
                   unmatured=0, f_lightning=0):
    """The entry list exactly as Electrum builds it, in Electrum's order."""
    return [
        ("Frozen", COLOR_FROZEN, frozen),
        ("Unmatured", COLOR_UNMATURED, unmatured),
        ("Unconfirmed", COLOR_UNCONFIRMED, unconfirmed),
        ("On-chain", COLOR_CONFIRMED, confirmed),
        ("Lightning", COLOR_LIGHTNING, lightning),
        ("Lightning frozen", COLOR_FROZEN_LIGHTNING, f_lightning),
    ]


def _amount_of(entries, color):
    return next(a for _n, c, a in entries if c == color)


# --- the colour -----------------------------------------------------------
def test_buffer_blue_is_not_electrums_frozen_blue() -> None:
    """Electrum already spends ColorScheme.BLUE on the Frozen slice. Two blues
    in one pie is two slices the user cannot tell apart."""
    assert qt_mod.COLOR_GOAL_BUFFER != COLOR_FROZEN
    assert qt_mod.COLOR_GOAL_BUFFER != COLOR_FROZEN_LIGHTNING
    assert qt_mod.COLOR_GOAL_BUFFER != COLOR_CONFIRMED


def test_buffer_blue_is_actually_blue() -> None:
    c = qt_mod.COLOR_GOAL_BUFFER
    assert c.blue() > c.red() and c.blue() > c.green()


# --- rewriting the entry list --------------------------------------------
def test_buffer_slice_comes_out_of_onchain(qapp) -> None:
    """The headline case: 100 on-chain and a 10 buffer shows 90 + 10."""
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    out = qt_mod._rewrite_piechart_entries(_stock_entries(confirmed=100), w)
    assert _amount_of(out, COLOR_CONFIRMED) == 90
    assert _amount_of(out, qt_mod.COLOR_GOAL_BUFFER) == 10


def test_buffer_slice_spills_into_lightning(qapp) -> None:
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    out = qt_mod._rewrite_piechart_entries(
        _stock_entries(confirmed=30_000, lightning=500_000), w)
    assert _amount_of(out, COLOR_CONFIRMED) == 0
    assert _amount_of(out, COLOR_LIGHTNING) == 430_000
    assert _amount_of(out, qt_mod.COLOR_GOAL_BUFFER) == 100_000


def test_rewrite_conserves_the_total(qapp) -> None:
    """The pie must show the same balance before and after the split, or the
    status bar and the chart stop agreeing."""
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    stock = _stock_entries(confirmed=200_000, lightning=300_000, frozen=50_000,
                           unconfirmed=10_000, unmatured=5_000, f_lightning=1_000)
    out = qt_mod._rewrite_piechart_entries(stock, w)
    assert sum(a for _n, _c, a in out) == sum(a for _n, _c, a in stock)


def test_rewrite_leaves_the_other_slices_alone(qapp) -> None:
    """Only On-chain and Lightning are touched. Frozen coins are not available
    to fund a channel, so the buffer never comes out of them."""
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    out = qt_mod._rewrite_piechart_entries(
        _stock_entries(confirmed=500_000, frozen=50_000, unconfirmed=10_000,
                       unmatured=5_000, f_lightning=1_000), w)
    assert _amount_of(out, COLOR_FROZEN) == 50_000
    assert _amount_of(out, COLOR_UNCONFIRMED) == 10_000
    assert _amount_of(out, COLOR_UNMATURED) == 5_000
    assert _amount_of(out, COLOR_FROZEN_LIGHTNING) == 1_000


def test_buffer_slice_is_appended_last(qapp) -> None:
    """Appended rather than inserted so every existing slice keeps the start
    angle Electrum gave it; only the final wedge is new."""
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    stock = _stock_entries(confirmed=500_000)
    out = qt_mod._rewrite_piechart_entries(stock, w)
    assert [c for _n, c, _a in out[:len(stock)]] == [c for _n, c, _a in stock]
    assert out[-1][1] == qt_mod.COLOR_GOAL_BUFFER


def test_no_plugin_loaded_leaves_the_list_untouched(qapp) -> None:
    """Identity, not merely equality: the no-buffer path must not even allocate,
    and the caller uses ``is`` to decide whether to repaint at all."""
    stock = _stock_entries(confirmed=500_000)
    assert qt_mod._rewrite_piechart_entries(stock, _FakeWallet()) is stock


def test_unmanaged_wallet_leaves_the_list_untouched(qapp) -> None:
    w = _FakeWallet()
    p = _glue_plugin()                       # w not managed
    qt_mod._set_buffer_provider(p.goal_buffer_sat)
    stock = _stock_entries(confirmed=500_000)
    assert qt_mod._rewrite_piechart_entries(stock, w) is stock


def test_goal_of_zero_leaves_the_list_untouched(qapp) -> None:
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=0)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)
    stock = _stock_entries(confirmed=500_000)
    assert qt_mod._rewrite_piechart_entries(stock, w) is stock


def test_empty_wallet_leaves_the_list_untouched(qapp) -> None:
    """Nothing to reserve against: a zero-sized slice would be an invisible
    wedge and a legend row reading "0"."""
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)
    stock = _stock_entries()
    assert qt_mod._rewrite_piechart_entries(stock, w) is stock


def test_an_exploding_provider_leaves_the_list_untouched(qapp) -> None:
    """This runs inside a status-bar repaint; a broken plugin must degrade to
    the stock chart rather than take the window down."""
    def _boom(_wallet):
        raise RuntimeError("provider is on fire")

    qt_mod._set_buffer_provider(_boom)
    stock = _stock_entries(confirmed=500_000)
    assert qt_mod._rewrite_piechart_entries(stock, _FakeWallet()) is stock


# --- the status-bar pie ---------------------------------------------------
class _FakeBalanceButton:
    """Stands in for Electrum's BalanceToolButton: the patch only touches
    ``_list`` / ``_warning`` / ``update_list``."""

    def __init__(self, entries, warning=False) -> None:
        self._list = list(entries)
        self._warning = warning
        self.updates = 0
        self.text = ""

    def update_list(self, entries, warning) -> None:
        self._list = list(entries)
        self._warning = warning
        self.updates += 1

    def setText(self, text) -> None:
        self.text = text


class _FakeStatusLabel:
    def __init__(self) -> None:
        self.text = ""
        self.visible = True

    def setText(self, text) -> None:
        self.text = text

    def setVisible(self, visible) -> None:
        self.visible = bool(visible)


class _FakeWindow:
    """Enough of ElectrumWindow for the REAL ``update_status`` to run end to end.

    ``network = None`` puts Electrum on its "Offline" branch, which is the one
    path through update_status that does not touch a live network object -- so
    the original runs for real (rather than being stubbed) and the wrapper is
    exercised exactly as Qt will call it. The offline branch does not build a pie
    itself, so the button is pre-loaded with the entry list Electrum's connected
    branch would have produced.
    """

    def __init__(self, wallet, button) -> None:
        self.wallet = wallet
        self.balance_label = button
        self.network = None
        self.tor_button = None
        self.tray = None
        self.status_button = None
        self.tasks_label = _FakeStatusLabel()
        self._coroutines_scheduled: Dict[object, str] = {}

    def num_tasks(self) -> int:
        return len(self._coroutines_scheduled)


def _patched_update_status(window) -> None:
    """Drive the patched ElectrumWindow.update_status against a stand-in window.

    The patch is installed on the real class, so it is fetched from there and
    invoked unbound -- which is exactly how Qt will call it.
    """
    from electrum.gui.qt.main_window import ElectrumWindow
    ElectrumWindow.update_status(window)


def test_status_bar_patch_is_idempotent(qapp) -> None:
    from electrum.gui.qt.main_window import ElectrumWindow

    assert qt_mod._patch_status_bar_piechart() is True
    first = ElectrumWindow.update_status
    assert qt_mod._patch_status_bar_piechart() is True
    # Second call must not wrap the wrapper -- that would re-run the original
    # update_status (and its network calls) once per install.
    assert ElectrumWindow.update_status is first


def test_status_bar_pie_gains_the_buffer_slice(qapp, monkeypatch) -> None:
    from electrum.gui.qt.main_window import ElectrumWindow

    qt_mod._patch_status_bar_piechart()
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    button = _FakeBalanceButton(_stock_entries(confirmed=100))
    window = _FakeWindow(w, button)
    _patched_update_status(window)

    assert _amount_of(button._list, COLOR_CONFIRMED) == 90
    assert _amount_of(button._list, qt_mod.COLOR_GOAL_BUFFER) == 10


def test_repeated_repaints_do_not_compound_the_buffer_slice(qapp) -> None:
    """The offline / not-connected / synchronizing branches of update_status do
    NOT rebuild the entry list -- they leave whatever was on the button. So on
    the second pass the wrapper is handed its own previous output, and a naive
    rewrite would subtract the buffer from the already-reduced on-chain slice and
    append a second blue wedge. Every repaint while the network is down would
    shrink the on-chain slice again.
    """
    from electrum.gui.qt.main_window import ElectrumWindow  # noqa: F401

    qt_mod._patch_status_bar_piechart()
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    button = _FakeBalanceButton(_stock_entries(confirmed=100))
    window = _FakeWindow(w, button)
    for _ in range(3):
        _patched_update_status(window)
        assert _amount_of(button._list, COLOR_CONFIRMED) == 90
        assert _amount_of(button._list, qt_mod.COLOR_GOAL_BUFFER) == 10
        assert sum(1 for _n, c, _a in button._list
                   if c == qt_mod.COLOR_GOAL_BUFFER) == 1
        assert sum(a for _n, _c, a in button._list) == 100


def test_a_goal_change_still_lands_while_the_list_is_stale(qapp) -> None:
    """The flip side of not compounding: re-deriving from the stashed stock list
    (rather than skipping) means an edited goal still takes effect on the next
    repaint, even on a branch that did not rebuild the list."""
    qt_mod._patch_status_bar_piechart()
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    button = _FakeBalanceButton(_stock_entries(confirmed=100))
    window = _FakeWindow(w, button)
    _patched_update_status(window)
    assert _amount_of(button._list, qt_mod.COLOR_GOAL_BUFFER) == 10

    p.config.INBOUND_LIQUIDITY_GOAL_SAT = 25
    _patched_update_status(window)
    assert _amount_of(button._list, qt_mod.COLOR_GOAL_BUFFER) == 25
    assert _amount_of(button._list, COLOR_CONFIRMED) == 75


def test_turning_the_goal_off_restores_the_stock_slices(qapp) -> None:
    """Setting the goal to 0 has to give the on-chain slice its sats back, not
    just stop adding new ones."""
    qt_mod._patch_status_bar_piechart()
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    button = _FakeBalanceButton(_stock_entries(confirmed=100))
    window = _FakeWindow(w, button)
    _patched_update_status(window)
    assert _amount_of(button._list, COLOR_CONFIRMED) == 90

    p.config.INBOUND_LIQUIDITY_GOAL_SAT = 0
    _patched_update_status(window)
    assert _amount_of(button._list, COLOR_CONFIRMED) == 100
    assert all(c != qt_mod.COLOR_GOAL_BUFFER for _n, c, _a in button._list)


def test_status_bar_pie_is_not_repainted_when_there_is_no_buffer(qapp) -> None:
    qt_mod._patch_status_bar_piechart()
    button = _FakeBalanceButton(_stock_entries(confirmed=100))
    _patched_update_status(_FakeWindow(_FakeWallet(), button))
    assert button.updates == 0


def test_low_reserve_warning_icon_is_left_alone(qapp) -> None:
    """With the low-reserve warning up, Electrum draws a warning icon instead of
    a pie -- there is no chart to restyle, so the patch stays out of it."""
    qt_mod._patch_status_bar_piechart()
    w = _FakeWallet()
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=10)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    button = _FakeBalanceButton(_stock_entries(confirmed=100), warning=True)
    _patched_update_status(_FakeWindow(w, button))
    assert button.updates == 0
    assert button._warning is True


def test_status_bar_patch_refreshes_the_send_warning(qapp) -> None:
    """The threshold is balance-dependent, so a Send tab label refreshed only on
    keystrokes would go stale the moment a payment landed."""
    qt_mod._patch_status_bar_piechart()
    fired: List[int] = []
    qt_mod._set_send_warning_refresher(lambda: fired.append(1))
    _patched_update_status(
        _FakeWindow(_FakeWallet(), _FakeBalanceButton(_stock_entries())))
    assert fired == [1]


def test_a_broken_refresher_cannot_break_the_status_bar(qapp) -> None:
    qt_mod._patch_status_bar_piechart()

    def _boom():
        raise RuntimeError("refresher is on fire")

    qt_mod._set_send_warning_refresher(_boom)
    _patched_update_status(          # must not raise
        _FakeWindow(_FakeWallet(), _FakeBalanceButton(_stock_entries())))


# --- the Wallet Balance dialog -------------------------------------------
class _FakeConfigForDialog:
    LN_UTXO_RESERVE = 10_000

    def format_amount_and_units(self, amount) -> str:
        return f"{amount} sat"


class _FakeFx:
    def format_amount_and_units(self, amount) -> str:
        return f"{amount} fiat"

    def is_enabled(self) -> bool:
        return True


class _DialogWallet(_FakeWallet):
    def is_low_reserve(self) -> bool:
        return False

    def has_lightning(self) -> bool:
        return True


def _dialog_parent(qapp, wallet) -> QWidget:
    parent = QWidget()
    parent.config = _FakeConfigForDialog()
    parent.fx = _FakeFx()
    parent.wallet = wallet
    return parent


def test_balance_dialog_patch_is_idempotent(qapp) -> None:
    assert qt_mod._patch_balance_dialog() is True
    first = BalanceDialog.__init__
    assert qt_mod._patch_balance_dialog() is True
    assert BalanceDialog.__init__ is first


def test_balance_dialog_gains_a_slice_and_a_legend_row(qapp) -> None:
    qt_mod._patch_balance_dialog()
    wallet = _DialogWallet(_FakePiechartBalance(confirmed=500_000, lightning=100_000))
    p = _glue_plugin(managed=wallet, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    parent = _dialog_parent(qapp, wallet)
    dialog = BalanceDialog(parent, wallet=wallet)

    piechart = dialog.findChild(PieChartWidget)
    assert _amount_of(piechart._list, COLOR_CONFIRMED) == 400_000
    assert _amount_of(piechart._list, qt_mod.COLOR_GOAL_BUFFER) == 100_000

    labels = [c.text() for c in dialog.findChildren(QLabel)]
    assert any("Liquidity goal buffer" in t for t in labels)
    # Its legend swatch carries the same blue as its wedge.
    assert any(lw.color == qt_mod.COLOR_GOAL_BUFFER
               for lw in dialog.findChildren(LegendWidget))
    dialog.deleteLater()


def _legend_rows(dialog) -> Dict[str, str]:
    """The dialog's legend as {label: amount text}, read off the real grid."""
    grid = dialog.findChild(QGridLayout)
    rows = {}
    for row in range(grid.rowCount()):
        name_item = grid.itemAtPosition(row, 1)
        amount_item = grid.itemAtPosition(row, 2)
        if name_item is None or amount_item is None:
            continue
        rows[name_item.widget().text()] = amount_item.widget().text()
    return rows


def test_balance_dialog_legend_follows_the_wedges(qapp) -> None:
    """Electrum builds the legend straight from the raw PiechartBalance, so
    without restating it the dialog would show a shrunken on-chain wedge beside
    a legend still quoting the full on-chain balance -- and the rows would no
    longer add up to the total shown in the status bar."""
    qt_mod._patch_balance_dialog()
    wallet = _DialogWallet(_FakePiechartBalance(confirmed=400_000, lightning=200_000,
                                                frozen=80_000))
    p = _glue_plugin(managed=wallet, INBOUND_LIQUIDITY_GOAL_SAT=150_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    dialog = BalanceDialog(_dialog_parent(qapp, wallet), wallet=wallet)
    rows = _legend_rows(dialog)
    assert rows["On-chain:"] == "250000 sat"          # 400k - 150k, not 400k
    assert rows["Lightning:"] == "200000 sat"         # on-chain covered it
    assert rows["Frozen:"] == "80000 sat"             # never touched
    assert rows["Liquidity goal buffer:"] == "150000 sat"
    # And the legend still accounts for every satoshi in the wallet.
    total = sum(int(v.split()[0]) for v in rows.values())
    assert total == int(wallet.get_balances_for_piechart().total())
    dialog.deleteLater()


def test_balance_dialog_legend_shows_a_fully_reserved_onchain_balance(qapp) -> None:
    """When the buffer eats the whole on-chain balance the row reads 0 rather
    than vanishing -- "all of it is spoken for" is the useful thing to say."""
    qt_mod._patch_balance_dialog()
    wallet = _DialogWallet(_FakePiechartBalance(confirmed=30_000, lightning=500_000))
    p = _glue_plugin(managed=wallet, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    dialog = BalanceDialog(_dialog_parent(qapp, wallet), wallet=wallet)
    rows = _legend_rows(dialog)
    assert rows["On-chain:"] == "0 sat"
    assert rows["Lightning:"] == "430000 sat"
    assert rows["Liquidity goal buffer:"] == "100000 sat"
    dialog.deleteLater()


def test_restate_legend_amount_reports_a_colour_with_no_row(qapp) -> None:
    """Electrum omits a legend row for a zero balance, so the colour being asked
    about may simply not be on screen. That is a normal miss, not an error."""
    from electrum.gui.qt.balance_dialog import LegendWidget as LW

    grid = QGridLayout()
    grid.addWidget(LW(COLOR_CONFIRMED), 0, 0)
    grid.addWidget(QLabel("On-chain:"), 0, 1)
    grid.addWidget(QLabel("400000 sat"), 0, 2)
    config = _FakeConfigForDialog()

    assert qt_mod._restate_legend_amount(grid, COLOR_CONFIRMED, 250_000,
                                         config, None) is True
    assert grid.itemAtPosition(0, 2).widget().text() == "250000 sat"
    assert qt_mod._restate_legend_amount(grid, COLOR_LIGHTNING, 1, config, None) is False


def test_balance_dialog_legend_row_does_not_overwrite_a_stock_row(qapp) -> None:
    """The legend is a QGridLayout Electrum fills a variable number of rows of
    (it skips empty balances), so the new row is placed past rowCount() rather
    than at a hard-coded index -- two widgets in one cell would overlap."""
    qt_mod._patch_balance_dialog()
    wallet = _DialogWallet(_FakePiechartBalance(
        confirmed=500_000, lightning=100_000, frozen=1_000, unconfirmed=2_000,
        unmatured=3_000, lightning_frozen=4_000))
    p = _glue_plugin(managed=wallet, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)

    dialog = BalanceDialog(_dialog_parent(qapp, wallet), wallet=wallet)
    grid = dialog.findChild(QGridLayout)
    # Every occupied cell holds exactly one widget: no row was landed on twice.
    seen = set()
    for row in range(grid.rowCount()):
        for col in range(grid.columnCount()):
            item = grid.itemAtPosition(row, col)
            if item is not None:
                assert (row, col) not in seen
                seen.add((row, col))
    labels = [c.text() for c in dialog.findChildren(QLabel)]
    for stock in ("Frozen:", "Unconfirmed:", "Unmatured:", "On-chain:",
                  "Lightning:", "Lightning (frozen):"):
        assert stock in labels
    dialog.deleteLater()


def test_balance_dialog_is_untouched_without_a_buffer(qapp) -> None:
    qt_mod._patch_balance_dialog()
    wallet = _DialogWallet(_FakePiechartBalance(confirmed=500_000))
    dialog = BalanceDialog(_dialog_parent(qapp, wallet), wallet=wallet)

    piechart = dialog.findChild(PieChartWidget)
    assert all(c != qt_mod.COLOR_GOAL_BUFFER for _n, c, _a in piechart._list)
    labels = [c.text() for c in dialog.findChildren(QLabel)]
    assert not any("Liquidity goal buffer" in t for t in labels)
    dialog.deleteLater()


# --- the Send tab warning -------------------------------------------------
class _FakeSendTab(QWidget):
    """A stand-in for Electrum's SendTab carrying the three things the warning
    touches: the wallet, a REAL BTCAmountEdit (so the real parsing runs), and a
    grid nested in layouts exactly as Electrum nests its own.

    The nesting matters: ``create_send_tab`` hands the plugin only the grid, and
    the whole feature rests on ``grid.parentWidget()`` resolving up through
    QVBoxLayout -> QHBoxLayout -> QVBoxLayout to the tab widget.
    """

    def __init__(self, wallet, *, decimal_point=0) -> None:
        QWidget.__init__(self)
        self.wallet = wallet
        self.amount_e = BTCAmountEdit(lambda: decimal_point)
        self.payto_e = None
        self.send_grid = grid = QGridLayout()
        grid.addWidget(QLabel("Pay to"), 0, 0)
        grid.addWidget(QLabel("Description"), 1, 0)
        grid.addWidget(QLabel("Comment"), 2, 0)
        grid.addWidget(QLabel("Amount"), 3, 0)
        grid.addWidget(self.amount_e, 3, 1)
        grid.addWidget(QLabel("Buttons"), 6, 1)
        vbox0 = QVBoxLayout()
        vbox0.addLayout(grid)
        hbox = QHBoxLayout()
        hbox.addLayout(vbox0)
        vbox = QVBoxLayout(self)
        vbox.addLayout(hbox)


def _install(send_tab, plugin):
    return qt_mod._install_send_tab_warning(send_tab.send_grid,
                                            plugin.send_warning_text)


def _warning_label(send_tab) -> Optional[QLabel]:
    for child in send_tab.findChildren(QLabel):
        if child.text().startswith("Spending this much"):
            return child
    return None


def test_the_grid_resolves_back_to_its_send_tab(qapp) -> None:
    """The load-bearing assumption of the ``create_send_tab`` hook: the hook is
    handed a nested layout, not the widget."""
    tab = _FakeSendTab(_FakeWallet())
    assert tab.send_grid.parentWidget() is tab


def test_warning_appears_when_the_amount_dips_into_the_buffer(qapp) -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    _install(tab, p)

    tab.amount_e.setAmount(900_001)
    label = _warning_label(tab)
    assert label is not None
    assert label.isVisibleTo(tab)
    assert label.text() == format_goal_buffer_warning(100_000)


def test_warning_hides_again_when_the_amount_comes_back_under(qapp) -> None:
    """It has to disappear as well as appear -- a warning that sticks after the
    user corrects the amount is a warning they learn to ignore."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    _install(tab, p)

    tab.amount_e.setAmount(900_001)
    assert _warning_label(tab) is not None
    tab.amount_e.setAmount(900_000)
    assert _warning_label(tab) is None


def test_no_warning_on_an_ordinary_send(qapp) -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    _install(tab, p)

    tab.amount_e.setAmount(1_000)
    assert _warning_label(tab) is None


def test_the_label_starts_hidden(qapp) -> None:
    """``create_send_tab`` fires from the window constructor, before the plugin
    even knows whether it manages this wallet -- so an empty Send tab must show
    no trace of the feature."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    _install(tab, p)
    assert _warning_label(tab) is None


def test_the_label_lands_directly_under_the_amount_row(qapp) -> None:
    """Row 4: under the Amount field (row 3) and above the buttons (row 6). A
    warning about an amount has to sit next to that amount to read as being
    about it."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    _install(tab, p)
    tab.amount_e.setAmount(900_000)

    item = tab.send_grid.itemAtPosition(qt_mod.SEND_WARNING_GRID_ROW, 1)
    assert item is not None
    assert isinstance(item.widget(), QLabel)


def test_the_label_moves_to_the_bottom_if_row_four_is_taken(qapp) -> None:
    """A future Electrum may claim row 4. Two widgets in one QGridLayout cell
    overlap rather than error, so the row is checked before it is used."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    squatter = QLabel("some future Electrum widget")
    tab.send_grid.addWidget(squatter, qt_mod.SEND_WARNING_GRID_ROW, 1)
    rows_before = tab.send_grid.rowCount()

    _install(tab, p)
    tab.amount_e.setAmount(900_000)

    assert tab.send_grid.itemAtPosition(
        qt_mod.SEND_WARNING_GRID_ROW, 1).widget() is squatter
    assert tab.send_grid.rowCount() > rows_before


def test_unmanaged_wallet_never_warns(qapp) -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=1_000_000))
    p = _glue_plugin()                                  # w not managed
    tab = _FakeSendTab(w)
    _install(tab, p)
    tab.amount_e.setAmount(1_000_000)
    assert _warning_label(tab) is None


def test_the_refresher_repaints_the_label_when_the_balance_moves(qapp) -> None:
    """Same typed amount, smaller balance: the warning has to appear without a
    keystroke, which is why install returns a refresher at all."""
    balance = _FakePiechartBalance(confirmed=900_000, lightning=100_000)
    w = _FakeWallet(balance)
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)
    refresh = _install(tab, p)

    tab.amount_e.setAmount(800_000)
    assert _warning_label(tab) is None
    balance.confirmed = 700_000            # a payment went out elsewhere
    refresh()
    assert _warning_label(tab) is not None


def test_install_declines_a_grid_with_no_send_tab(qapp) -> None:
    """A bare grid (or one nested under something that is not a SendTab) yields
    no refresher rather than a half-installed label."""
    w = _FakeWallet()
    p = _glue_plugin(managed=w)
    orphan = QGridLayout()
    assert qt_mod._install_send_tab_warning(orphan, p.send_warning_text) is None


def test_the_amount_read_survives_a_non_sat_base_unit(qapp) -> None:
    """BTCAmountEdit renders in the user's chosen unit but ``get_amount``
    returns sat, which is the unit the threshold is in. mBTC here."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w, decimal_point=5)        # mBTC
    _install(tab, p)

    tab.amount_e.setAmount(900_001)
    assert _warning_label(tab) is not None


# --- pay-to-many ----------------------------------------------------------
class _FakeOutput:
    def __init__(self, value) -> None:
        self.value = value


class _FakePaymentIdentifier:
    def __init__(self, outputs, *, multiline: bool = True) -> None:
        self._outputs = outputs
        self._multiline = multiline

    def is_multiline(self) -> bool:
        return self._multiline

    def get_onchain_outputs(self, amount):
        # Electrum ignores the amount on the multiline branch and hands back the
        # per-line outputs it parsed; mirrored here.
        return self._outputs


class _FakePayToEdit(QWidget):
    textChanged = pyqtSignal()

    def __init__(self, parent, payment_identifier=None) -> None:
        QWidget.__init__(self, parent)
        self.payment_identifier = payment_identifier


def _multiline_tab(wallet, outputs, *, multiline: bool = True) -> _FakeSendTab:
    tab = _FakeSendTab(wallet)
    tab.payto_e = _FakePayToEdit(
        tab, _FakePaymentIdentifier(outputs, multiline=multiline))
    return tab


def test_pay_to_many_sums_its_lines(qapp) -> None:
    """Pay-to-many leaves ``amount_e`` empty and puts the amounts in the pay-to
    field, so the outputs are summed instead."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _multiline_tab(w, [_FakeOutput(500_000), _FakeOutput(400_001)])
    _install(tab, p)

    assert qt_mod._send_tab_amount_sat(tab) == 900_001
    assert _warning_label(tab) is not None


def test_pay_to_many_under_the_threshold_does_not_warn(qapp) -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _multiline_tab(w, [_FakeOutput(500_000), _FakeOutput(400_000)])
    _install(tab, p)
    assert _warning_label(tab) is None


def test_a_max_weight_among_the_lines_is_not_guessed_at(qapp) -> None:
    """A '!' line makes the total unknowable until a transaction is built. That
    is reported as "no amount" rather than guessed -- a warning invented from a
    wrong number is worse than no warning."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _multiline_tab(w, [_FakeOutput(500_000), _FakeOutput("!")])
    _install(tab, p)

    assert qt_mod._send_tab_amount_sat(tab) is None
    assert _warning_label(tab) is None


def test_a_single_line_payto_is_left_to_the_amount_box(qapp) -> None:
    """Not multiline: the amount lives in ``amount_e``, and an empty box means
    no amount rather than a sum of placeholder outputs."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    tab = _multiline_tab(w, [_FakeOutput(0)], multiline=False)
    assert qt_mod._send_tab_amount_sat(tab) is None


def test_editing_the_payto_field_refreshes_the_warning(qapp) -> None:
    """The multiline case needs its own trigger: amount_e stays silent while the
    user types lines into the pay-to box."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _glue_plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _multiline_tab(w, [_FakeOutput(1_000)])
    _install(tab, p)
    assert _warning_label(tab) is None

    tab.payto_e.payment_identifier = _FakePaymentIdentifier([_FakeOutput(900_001)])
    tab.payto_e.textChanged.emit()
    assert _warning_label(tab) is not None


# --- _free_grid_row -------------------------------------------------------
def test_free_grid_row_prefers_the_requested_row(qapp) -> None:
    grid = QGridLayout()
    grid.addWidget(QLabel("a"), 0, 0)
    grid.addWidget(QLabel("b"), 6, 0)
    assert qt_mod._free_grid_row(grid, 4) == 4


def test_free_grid_row_falls_past_the_end_when_taken(qapp) -> None:
    grid = QGridLayout()
    grid.addWidget(QLabel("a"), 4, 2)
    assert qt_mod._free_grid_row(grid, 4) == grid.rowCount()


# --- the plugin hook and its cleanup -------------------------------------
def _qt_plugin(managed_wallet=None, **cfg) -> qt_mod.Plugin:
    """A qt.Plugin with only the attributes the buffer path touches. Built the
    way the rest of the Qt suite builds one: __init__ needs a real Electrum
    plugin parent, which these tests have no use for."""
    p = object.__new__(qt_mod.Plugin)
    p.logger = logging.getLogger("test.inbound_liquidity.goal_buffer_qt")
    p.config = _glue_plugin(**cfg).config
    p.wallets = {}
    p._send_warning_refreshers = {}
    p._tabs = {}
    p._unlock_declined_at = {}
    p._unlock_prompting = set()
    if managed_wallet is not None:
        p.wallets[managed_wallet] = object()
    return p


def test_create_send_tab_hook_installs_and_registers_the_refresher(qapp) -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _qt_plugin(w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    tab = _FakeSendTab(w)

    assert p.create_send_tab(tab.send_grid) is None   # run_hook asserts on truthy
    assert w in p._send_warning_refreshers

    tab.amount_e.setAmount(900_001)
    assert _warning_label(tab) is not None


def test_close_wallet_drops_the_refresher_and_clears_the_globals(qapp) -> None:
    """A class patch outlives any one wallet, so a stale captured reference would
    keep a closed wallet's plugin alive -- and keep reserving against it."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    p = _qt_plugin(w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    p.stop_wallet = lambda wallet: p.wallets.pop(wallet, None)
    p._remove_liquidity_tab = lambda wallet: None
    tab = _FakeSendTab(w)          # kept alive: see the two-wallet test below
    p.create_send_tab(tab.send_grid)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)
    qt_mod._set_send_warning_refresher(p._refresh_send_warnings)

    p.close_wallet(w)

    assert w not in p._send_warning_refreshers
    assert qt_mod._BUFFER_PROVIDER is None
    assert qt_mod._SEND_WARNING_REFRESH is None


def test_closing_one_of_two_wallets_keeps_the_globals_live(qapp) -> None:
    w1 = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    w2 = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    p = _qt_plugin(w1, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    p.wallets[w2] = object()
    p.stop_wallet = lambda wallet: p.wallets.pop(wallet, None)
    p._remove_liquidity_tab = lambda wallet: None
    # Held in locals on purpose: dropping the last Python reference to a tab
    # takes its C++ grid with it, and the hook would then see a dead layout.
    tab1, tab2 = _FakeSendTab(w1), _FakeSendTab(w2)
    p.create_send_tab(tab1.send_grid)
    p.create_send_tab(tab2.send_grid)
    qt_mod._set_buffer_provider(p.goal_buffer_sat)
    qt_mod._set_send_warning_refresher(p._refresh_send_warnings)

    p.close_wallet(w1)

    assert w2 in p._send_warning_refreshers
    assert qt_mod._BUFFER_PROVIDER is not None
    assert qt_mod._SEND_WARNING_REFRESH is not None


def test_refresh_send_warnings_survives_a_dead_widget(qapp) -> None:
    """Qt deletes the C++ widget with its window while the Python wrapper lives
    on; touching it raises RuntimeError. One dead tab must not stop the others
    refreshing."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000))
    p = _qt_plugin(w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    fired: List[int] = []

    def _dead():
        raise RuntimeError("wrapped C/C++ object has been deleted")

    p._send_warning_refreshers = {object(): _dead, w: lambda: fired.append(1)}
    p._refresh_send_warnings()               # must not raise
    assert fired == [1]
