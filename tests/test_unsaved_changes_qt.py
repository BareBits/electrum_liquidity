"""Unsaved changes on the four Apply-gated settings sub-tabs.

What is pinned here is the whole life of one pending edit:

  * the status area says "Click APPLY to save changes" while it is pending, and
    stops saying so once it is applied or undone by hand;
  * a background wallet event (which repopulates three of these tabs from
    storage) does not reach in and overwrite it;
  * leaving the tab asks about it, and the two answers behave as specified --
    Apply saves it, Ignore keeps it in the widget without re-asking;
  * a refused value does not let the user walk away thinking it was saved.

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QTabWidget, QTreeWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin, _plain_line_edits,
    _silence_log_tab_timer, qapp,
)

SETTINGS_TAB = 0
PROVIDERS_TAB = 1
PARTNERS_TAB = 2
ADVANCED_TAB = 3

HINT = "Click APPLY to save changes"


# --- scaffolding ---------------------------------------------------------
class _Answered:
    """Stands in for the two-choice dialog, recording what it was asked.

    The guard calls module-level ``_ask_two_choices``; monkeypatching that is the
    only seam, since the real one blocks on QMessageBox.exec().
    """

    def __init__(self, answer: Optional[bool]) -> None:
        self.answer = answer
        self.asked: List[dict] = []

    def __call__(self, window, **kwargs) -> Optional[bool]:
        self.asked.append(kwargs)
        return self.answer

    @property
    def count(self) -> int:
        return len(self.asked)

    def last_text(self) -> str:
        return self.asked[-1]["text"] if self.asked else ""


@pytest.fixture
def dialog(monkeypatch):
    """Install a stub dialog; each test sets its answer."""
    stub = _Answered(None)
    monkeypatch.setattr(qt_mod, "_ask_two_choices", stub)
    return stub


def _built(plugin=None):
    p = plugin or _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    return p, window, wallet


def _sub_tabs(plugin, wallet) -> QTabWidget:
    return plugin._tabs[wallet].container.findChild(QTabWidget)


def _pane(plugin, wallet, index: int):
    return plugin._tabs[wallet].panes[index]


def _status_text(plugin, wallet, index: int) -> str:
    """Everything the tab's status area says, hint included."""
    return _pane(plugin, wallet, index).tracker.status.text()


def _settings_edits(plugin, wallet):
    tabs = _sub_tabs(plugin, wallet)
    return _plain_line_edits(tabs.widget(SETTINGS_TAB))


def _advanced_edits(plugin, wallet):
    tabs = _sub_tabs(plugin, wallet)
    return _plain_line_edits(tabs.widget(ADVANCED_TAB))


def _goal_edit(plugin, wallet):
    """The liquidity-goal amount widget (a BTCAmountEdit, so it is deliberately
    excluded from _plain_line_edits)."""
    from electrum.gui.qt.amountedit import BTCAmountEdit
    tab = _sub_tabs(plugin, wallet).widget(SETTINGS_TAB)
    return tab.findChildren(BTCAmountEdit)[0]


def _preferred_provider_row(plugin, wallet) -> QTreeWidget:
    """Put one discovered provider on the Providers tab and mark it Preferred in
    the stored settings, then return the tree. A provider earns a row by being
    discovered (or by having a history), not by being named in the list -- so
    both halves are needed to get a ticked row to untick."""
    plugin._last_offers[wallet] = [
        ProviderOffer(npub="npub1already", percentage_fee=0.5, mining_fee_sat=1,
                      min_amount_sat=1, max_reverse_sat=1)]
    plugin.set_setting(wallet, "INBOUND_LIQUIDITY_PREFERRED_NPUBS", "npub1already")
    plugin._tabs[wallet].refresh()
    return _sub_tabs(plugin, wallet).widget(PROVIDERS_TAB).findChild(QTreeWidget)


def _row_for(tree: QTreeWidget, ident: str):
    """The row carrying this npub / node id. Looked up fresh after every rebuild,
    since the items are new objects each time."""
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and str(data).lower() == ident.lower():
            return item
    raise AssertionError(f"no row for {ident}")


# --- the two-choice dialog itself ----------------------------------------
# Everything above stubs _ask_two_choices out, because the real one blocks on
# QMessageBox.exec(). These few drive the real helper against a fake window, so
# the piece that actually puts both of this change's dialogs on screen is not
# the one piece nothing exercises.
class _RecordingWindow:
    """A window exposing Electrum's msg_box, which is what carries custom button
    labels (`question()` hard-codes Yes/No)."""

    def __init__(self, click: str) -> None:
        self.click = click          # "accept", "reject" or "escape"
        self.calls: List[dict] = []

    def msg_box(self, icon, parent, title, text, *, buttons=None, **kw):
        self.calls.append({"title": title, "text": text, "buttons": buttons})
        from PyQt6.QtWidgets import QMessageBox
        roles = {}
        for button, role, value in buttons:
            roles[role] = (button.text(), value)
        if self.click == "accept":
            return roles[QMessageBox.ButtonRole.AcceptRole][1]
        if self.click == "reject":
            return roles[QMessageBox.ButtonRole.RejectRole][1]
        return 0                    # dismissed without clicking a button


def test_the_dialog_reports_the_accept_button(qapp):
    window = _RecordingWindow("accept")
    assert qt_mod._ask_two_choices(
        window, title="T", text="X",
        accept_label="Apply changes", reject_label="Ignore") is True
    labels = [b.text() for b, _role, _v in window.calls[0]["buttons"]]
    assert labels == ["Apply changes", "Ignore"]


def test_the_dialog_reports_the_reject_button(qapp):
    window = _RecordingWindow("reject")
    assert qt_mod._ask_two_choices(
        window, title="T", text="X",
        accept_label="Apply changes", reject_label="Ignore") is False


def test_dismissing_the_dialog_counts_as_the_reject_answer(qapp):
    # Escape and the window's close button resolve to the RejectRole button, and
    # reject is the safe answer for both callers: it keeps your unsaved edits,
    # and it leaves your Lightning routing alone.
    window = _RecordingWindow("escape")
    assert qt_mod._ask_two_choices(
        window, title="T", text="X",
        accept_label="Apply changes", reject_label="Ignore") is False


def test_a_window_without_msg_box_still_gets_asked(qapp):
    # An older Electrum: ask the right question in the wrong words rather than
    # silently not asking.
    class _Old:
        def __init__(self):
            self.asked = []

        def question(self, msg, title=None):
            self.asked.append((msg, title))
            return True

    window = _Old()
    assert qt_mod._ask_two_choices(
        window, title="T", text="X", accept_label="A", reject_label="B") is True
    assert window.asked == [("X", "T")]


def test_a_window_that_cannot_ask_returns_no_answer(qapp):
    # Distinct from False: "could not ask" must never be read as consent.
    class _Mute:
        pass

    assert qt_mod._ask_two_choices(
        _Mute(), title="T", text="X", accept_label="A", reject_label="B") is None


def test_a_dialog_that_raises_returns_no_answer(qapp):
    class _Broken:
        def msg_box(self, *a, **kw):
            raise RuntimeError("no display")

    assert qt_mod._ask_two_choices(
        _Broken(), title="T", text="X", accept_label="A", reject_label="B") is None


# --- the hint ------------------------------------------------------------
def test_a_freshly_built_tab_is_clean(qapp):
    p, _window, wallet = _built()
    for index in (SETTINGS_TAB, PROVIDERS_TAB, PARTNERS_TAB, ADVANCED_TAB):
        pane = _pane(p, wallet, index)
        assert not pane.tracker.is_dirty(), f"{pane.name} was born dirty"
        assert HINT not in _status_text(p, wallet, index)


def test_typing_asks_for_apply(qapp):
    p, _window, wallet = _built()
    _settings_edits(p, wallet)[1].setText("7")      # max channels
    assert _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    assert _status_text(p, wallet, SETTINGS_TAB) == HINT


def test_typing_a_value_back_to_what_it_was_is_not_a_change(qapp):
    # The point of comparing against a baseline rather than latching a flag:
    # an edit the user undoes themselves leaves nothing to save.
    p, _window, wallet = _built()
    edit = _settings_edits(p, wallet)[1]
    before = edit.text()
    edit.setText("7")
    assert _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    edit.setText(before)
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    assert HINT not in _status_text(p, wallet, SETTINGS_TAB)


def test_applying_clears_the_hint_and_says_saved(qapp):
    p, _window, wallet = _built()
    _settings_edits(p, wallet)[1].setText("7")
    assert _pane(p, wallet, SETTINGS_TAB).apply() is True
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    text = _status_text(p, wallet, SETTINGS_TAB)
    assert "Settings saved." in text and HINT not in text
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == 7


def test_a_stale_saved_message_gives_way_to_the_hint(qapp):
    # "Settings saved." stops being true the moment the user types again, so it
    # must not sit on screen above "Click APPLY to save changes".
    p, _window, wallet = _built()
    _pane(p, wallet, SETTINGS_TAB).apply()
    assert "Settings saved." in _status_text(p, wallet, SETTINGS_TAB)
    _settings_edits(p, wallet)[1].setText("7")
    assert _status_text(p, wallet, SETTINGS_TAB) == HINT


def test_a_warning_keeps_its_place_above_the_hint(qapp):
    # The other half of that rule: a configuration warning describes something
    # still in force, so it stays, and the hint goes underneath it.
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    settings = tabs.widget(SETTINGS_TAB)
    from PyQt6.QtWidgets import QCheckBox
    sink_only = [cb for cb in settings.findChildren(QCheckBox)
                 if "Disable submarine swaps" in cb.text()][0]
    sink_only.setChecked(True)                  # applies immediately; warns
    warning = _status_text(p, wallet, SETTINGS_TAB)
    assert "nothing can drain a channel" in warning
    _settings_edits(p, wallet)[1].setText("7")
    text = _status_text(p, wallet, SETTINGS_TAB)
    assert "nothing can drain a channel" in text
    assert text.endswith(HINT)                  # warning first, hint under it


def test_the_liquidity_goal_amount_is_tracked_too(qapp):
    # It is the one field on the tab entered as MONEY rather than a bare sat
    # count, so it is a different widget with a different snapshot rule.
    p, _window, wallet = _built()
    goal = _goal_edit(p, wallet)
    goal.setAmount(250_000)
    assert _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    assert HINT in _status_text(p, wallet, SETTINGS_TAB)
    assert _pane(p, wallet, SETTINGS_TAB).apply() is True
    assert p.settings(wallet).INBOUND_LIQUIDITY_GOAL_SAT == 250_000
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()


def test_redisplaying_the_goal_in_another_unit_is_not_an_edit(qapp):
    # The goal is snapshotted as its parsed SAT amount, not as the text in the
    # box, because Electrum re-renders that box whenever the user changes their
    # base unit. 0.001 BTC and 100000 sat are one stored setting; switching
    # between those two displays must not read as an unsaved change.
    p, window, wallet = _built()
    goal = _goal_edit(p, wallet)
    before_sat = goal.get_amount()
    before_text = goal.text()
    window.decimal_point = 0                    # BTC -> sat
    goal.setAmount(before_sat)                  # what _reload_goal does
    assert goal.text() != before_text, "premise: the display really changed"
    assert goal.get_amount() == before_sat
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    assert HINT not in _status_text(p, wallet, SETTINGS_TAB)


def test_every_apply_gated_tab_tracks_its_own_edits(qapp):
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    # Advanced: a line edit.
    _advanced_edits(p, wallet)[0].setText("9")
    # Providers / partners: the free-text npub and partner boxes.
    from PyQt6.QtWidgets import QPlainTextEdit
    tabs.widget(PROVIDERS_TAB).findChildren(QPlainTextEdit)[0].setPlainText("npub1x")
    tabs.widget(PARTNERS_TAB).findChildren(QPlainTextEdit)[0].setPlainText("02ab@h:9735")
    for index in (PROVIDERS_TAB, PARTNERS_TAB, ADVANCED_TAB):
        assert _pane(p, wallet, index).tracker.is_dirty(), index
        assert HINT in _status_text(p, wallet, index)
    # ...and the Settings tab, which nobody touched, is still clean.
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()


def test_an_advanced_checkbox_waits_for_apply_and_counts_as_a_change(qapp):
    p, _window, wallet = _built()
    from PyQt6.QtWidgets import QCheckBox
    tabs = _sub_tabs(p, wallet)
    cb = [c for c in tabs.widget(ADVANCED_TAB).findChildren(QCheckBox)
          if "diagnostic log" in c.text()][0]
    before = bool(p.settings(wallet).INBOUND_LIQUIDITY_DIAG_LOG_ENABLED)
    cb.setChecked(not before)
    assert _pane(p, wallet, ADVANCED_TAB).tracker.is_dirty()
    # Still unsaved: this tab's boxes are Apply-gated, unlike the Settings tab's.
    assert bool(p.settings(wallet).INBOUND_LIQUIDITY_DIAG_LOG_ENABLED) is before


def test_an_immediate_apply_switch_is_never_called_unsaved(qapp):
    # The master switch writes through on toggle, so there is nothing pending
    # and claiming otherwise would be a lie.
    p, _window, wallet = _built()
    from electrum.plugins.inbound_liquidity.qt_widgets import ToggleSwitch
    tabs = _sub_tabs(p, wallet)
    toggle = tabs.widget(SETTINGS_TAB).findChildren(ToggleSwitch)[0]
    toggle.setChecked(True)
    assert bool(p.settings(wallet).INBOUND_LIQUIDITY_AUTOMATION_ENABLED)
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    assert HINT not in _status_text(p, wallet, SETTINGS_TAB)


# --- a background refresh must not eat the edit ---------------------------
def test_a_refresh_does_not_overwrite_pending_advanced_edits(qapp):
    # refresh() runs on ordinary wallet events (channels_updated and friends),
    # and used to call repopulate() straight into the fields.
    p, _window, wallet = _built()
    edit = _advanced_edits(p, wallet)[0]
    edit.setText("9")
    p._tabs[wallet].refresh()
    assert edit.text() == "9"
    assert _pane(p, wallet, ADVANCED_TAB).tracker.is_dirty()
    assert HINT in _status_text(p, wallet, ADVANCED_TAB)


def test_a_refresh_does_not_overwrite_pending_provider_edits(qapp):
    p, _window, wallet = _built()
    from PyQt6.QtWidgets import QPlainTextEdit
    box = _sub_tabs(p, wallet).widget(PROVIDERS_TAB).findChildren(QPlainTextEdit)[0]
    box.setPlainText("npub1typed")
    p._tabs[wallet].refresh()
    assert box.toPlainText() == "npub1typed"
    assert _pane(p, wallet, PROVIDERS_TAB).tracker.is_dirty()


def test_a_refresh_does_not_overwrite_pending_partner_edits(qapp):
    p, _window, wallet = _built()
    from PyQt6.QtWidgets import QPlainTextEdit
    box = _sub_tabs(p, wallet).widget(PARTNERS_TAB).findChildren(QPlainTextEdit)[0]
    box.setPlainText("02ab@host:9735")
    p._tabs[wallet].refresh()
    assert box.toPlainText() == "02ab@host:9735"
    assert _pane(p, wallet, PARTNERS_TAB).tracker.is_dirty()


def test_a_refresh_does_not_undo_an_unticked_provider_row(qapp):
    # The subtle half of carrying ticks across a rebuild. UNticking a row is an
    # edit too, and the stored list still names that provider -- so a rebuild
    # that merges the row state with the stored list puts the tick straight back
    # and the user's change evaporates.
    p, _window, wallet = _built()
    tree = _preferred_provider_row(p, wallet)
    row = _row_for(tree, "npub1already")
    assert row.checkState(p._PREF_COL) == Qt.CheckState.Checked   # premise
    row.setCheckState(p._PREF_COL, Qt.CheckState.Unchecked)
    assert _pane(p, wallet, PROVIDERS_TAB).tracker.is_dirty()
    p._tabs[wallet].refresh()
    row = _row_for(tree, "npub1already")
    assert row.checkState(p._PREF_COL) == Qt.CheckState.Unchecked


def test_a_refresh_does_not_undo_an_unticked_partner_row(qapp):
    p, _window, wallet = _built()
    node_id = "02" + "ab" * 32
    # A peer earns a row by having a history with us, not by being listed.
    p._record_peer_fault(wallet, node_id, "channel open failed", hard=True)
    p.set_setting(wallet, "INBOUND_LIQUIDITY_BANNED_PARTNERS", node_id)
    p._tabs[wallet].refresh()
    tree = _sub_tabs(p, wallet).widget(PARTNERS_TAB).findChild(QTreeWidget)
    row = _row_for(tree, node_id)
    assert row.checkState(p._PARTNER_BAN_COL) == Qt.CheckState.Checked   # premise
    row.setCheckState(p._PARTNER_BAN_COL, Qt.CheckState.Unchecked)
    p._tabs[wallet].refresh()
    row = _row_for(tree, node_id)
    assert row.checkState(p._PARTNER_BAN_COL) == Qt.CheckState.Unchecked


def test_a_refresh_keeps_a_newly_ticked_provider_row_ticked(qapp):
    # The other direction, which the row state alone already gets right.
    p, _window, wallet = _built()
    p._last_offers[wallet] = [
        ProviderOffer(npub="npub1fresh", percentage_fee=0.5, mining_fee_sat=1,
                      min_amount_sat=1, max_reverse_sat=1)]
    p._tabs[wallet].refresh()
    tree = _sub_tabs(p, wallet).widget(PROVIDERS_TAB).findChild(QTreeWidget)
    _row_for(tree, "npub1fresh").setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    p._tabs[wallet].refresh()
    assert _row_for(tree, "npub1fresh").checkState(p._PREF_COL) == Qt.CheckState.Checked
    # ...and it is still unsaved, because it has not been applied.
    assert "npub1fresh" not in p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS
    assert _pane(p, wallet, PROVIDERS_TAB).tracker.is_dirty()


def test_an_untick_survives_all_the_way_to_apply(qapp):
    # End to end: untick, let a background event rebuild the table, then apply.
    p, _window, wallet = _built()
    tree = _preferred_provider_row(p, wallet)
    _row_for(tree, "npub1already").setCheckState(p._PREF_COL, Qt.CheckState.Unchecked)
    p._tabs[wallet].refresh()
    assert _pane(p, wallet, PROVIDERS_TAB).apply() is True
    assert "npub1already" not in p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS


def test_sorting_a_table_is_not_an_edit(qapp):
    # Clicking a column header reorders the view and changes nothing that would
    # be saved. Asking a user to Apply because they sorted a table -- and then
    # asking again on the way out -- would be nonsense.
    p, _window, wallet = _built()
    tree = _preferred_provider_row(p, wallet)
    assert not _pane(p, wallet, PROVIDERS_TAB).tracker.is_dirty()   # premise
    for col in (p._ATTEMPTS_COL, p._SUCCESS_COL, p._PREF_COL):
        tree.header().sectionClicked.emit(col)
        tree.header().sectionClicked.emit(col)                      # and reverse
    assert not _pane(p, wallet, PROVIDERS_TAB).tracker.is_dirty()
    assert HINT not in _status_text(p, wallet, PROVIDERS_TAB)


def test_a_refresh_still_refreshes_a_clean_tab(qapp):
    # The dirty guard above must not freeze a tab nobody is editing.
    p, _window, wallet = _built()
    p.set_setting(wallet, "INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY", 4)
    p._tabs[wallet].refresh()
    assert _advanced_edits(p, wallet)[0].text() == "4"


# --- leaving a tab -------------------------------------------------------
def test_leaving_a_clean_tab_asks_nothing(qapp, dialog):
    p, _window, wallet = _built()
    _sub_tabs(p, wallet).setCurrentIndex(ADVANCED_TAB)
    assert dialog.count == 0


def test_leaving_a_dirty_tab_asks(qapp, dialog):
    p, _window, wallet = _built()
    _settings_edits(p, wallet)[1].setText("7")
    _sub_tabs(p, wallet).setCurrentIndex(ADVANCED_TAB)
    assert dialog.count == 1
    assert "Settings" in dialog.last_text()


def test_apply_from_the_dialog_saves_and_lets_the_switch_stand(qapp, dialog):
    dialog.answer = True
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    _settings_edits(p, wallet)[1].setText("7")
    tabs.setCurrentIndex(ADVANCED_TAB)
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == 7
    assert tabs.currentIndex() == ADVANCED_TAB
    assert not _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()


def test_ignore_keeps_the_typed_value_without_saving_it(qapp, dialog):
    dialog.answer = False
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    before = p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS
    edit = _settings_edits(p, wallet)[1]
    edit.setText("7")
    tabs.setCurrentIndex(ADVANCED_TAB)
    assert tabs.currentIndex() == ADVANCED_TAB
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == before
    assert edit.text() == "7"            # nothing was discarded
    assert _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()
    assert HINT in _status_text(p, wallet, SETTINGS_TAB)


def test_ignore_is_not_asked_again_for_the_same_pending_edit(qapp, dialog):
    dialog.answer = False
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    _settings_edits(p, wallet)[1].setText("7")
    tabs.setCurrentIndex(ADVANCED_TAB)
    assert dialog.count == 1
    tabs.setCurrentIndex(SETTINGS_TAB)
    tabs.setCurrentIndex(PROVIDERS_TAB)      # left Settings again, unchanged
    assert dialog.count == 1


def test_editing_further_after_an_ignore_asks_again(qapp, dialog):
    dialog.answer = False
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    edit = _settings_edits(p, wallet)[1]
    edit.setText("7")
    tabs.setCurrentIndex(ADVANCED_TAB)
    assert dialog.count == 1
    tabs.setCurrentIndex(SETTINGS_TAB)
    edit.setText("8")                        # a different set of pending edits
    tabs.setCurrentIndex(PROVIDERS_TAB)
    assert dialog.count == 2


def test_a_refused_value_sends_the_user_back_to_fix_it(qapp, dialog):
    # Apply from the dialog, but the form does not validate: the switch must not
    # be allowed to stand, or the user walks away believing it saved.
    dialog.answer = True
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    _settings_edits(p, wallet)[1].setText("not-a-number")
    tabs.setCurrentIndex(ADVANCED_TAB)
    assert tabs.currentIndex() == SETTINGS_TAB
    assert "Invalid value" in _status_text(p, wallet, SETTINGS_TAB)
    assert _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()


def test_no_dialog_available_does_not_trap_the_user_on_the_tab(qapp, dialog):
    dialog.answer = None                     # could not be shown at all
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    _settings_edits(p, wallet)[1].setText("7")
    tabs.setCurrentIndex(ADVANCED_TAB)
    assert tabs.currentIndex() == ADVANCED_TAB
    assert _pane(p, wallet, SETTINGS_TAB).tracker.is_dirty()   # still pending


def test_leaving_a_read_only_sub_tab_asks_nothing(qapp, dialog):
    p, _window, wallet = _built()
    tabs = _sub_tabs(p, wallet)
    tabs.setCurrentIndex(5)                  # Declines: nothing to save
    tabs.setCurrentIndex(6)
    assert dialog.count == 0


# --- leaving the Liquidity tab for one of Electrum's ---------------------
def test_leaving_the_liquidity_tab_entirely_asks(qapp, dialog):
    from PyQt6.QtWidgets import QWidget
    p, window, wallet = _built()
    other = QWidget()
    window.tabs.addTab(other, "Send")
    window.tabs.setCurrentIndex(window.tabs.indexOf(p._tabs[wallet].container))
    dialog.asked.clear()
    _settings_edits(p, wallet)[1].setText("7")
    window.tabs.setCurrentIndex(window.tabs.indexOf(other))
    assert dialog.count == 1


def test_leaving_the_liquidity_tab_asks_about_every_dirty_sub_tab(qapp, dialog):
    # Edits can be left behind on a sub-tab that is not the visible one; they
    # are all about to go unsaved together, so they are all named.
    from PyQt6.QtWidgets import QPlainTextEdit, QWidget
    dialog.answer = True
    p, window, wallet = _built()
    other = QWidget()
    window.tabs.addTab(other, "Send")
    window.tabs.setCurrentIndex(window.tabs.indexOf(p._tabs[wallet].container))
    tabs = _sub_tabs(p, wallet)
    _advanced_edits(p, wallet)[0].setText("9")
    tabs.widget(PROVIDERS_TAB).findChildren(QPlainTextEdit)[0].setPlainText("npub1x")
    dialog.asked.clear()
    window.tabs.setCurrentIndex(window.tabs.indexOf(other))
    assert dialog.count == 1
    text = dialog.last_text()
    assert "Swap providers" in text and "Advanced" in text
    # Apply was chosen, so both were saved.
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY == 9
    assert "npub1x" in p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS


def test_a_refused_value_from_the_main_tab_bar_lands_on_the_offending_sub_tab(qapp, dialog):
    # Leaving the plugin entirely with an invalid value on a sub-tab that is not
    # the one on screen: we must come back, show that sub-tab, and ask nothing
    # more -- the corrective navigation is ours, not the user leaving a tab.
    from PyQt6.QtWidgets import QWidget
    dialog.answer = True
    p, window, wallet = _built()
    other = QWidget()
    window.tabs.addTab(other, "Send")
    window.tabs.setCurrentIndex(window.tabs.indexOf(p._tabs[wallet].container))
    tabs = _sub_tabs(p, wallet)
    _advanced_edits(p, wallet)[0].setText("not-a-number")
    dialog.asked.clear()
    window.tabs.setCurrentIndex(window.tabs.indexOf(other))
    assert dialog.count == 1                 # asked once, not twice
    assert window.tabs.currentWidget() is p._tabs[wallet].container
    assert tabs.currentIndex() == ADVANCED_TAB
    assert "Invalid value" in _status_text(p, wallet, ADVANCED_TAB)


def test_closing_the_tab_disconnects_the_main_window_guard(qapp, dialog):
    # The guard is connected to the main window's tab bar, which outlives the
    # plugin's tab; left connected it would ask about dead widgets forever.
    from PyQt6.QtWidgets import QWidget
    p, window, wallet = _built()
    other = QWidget()
    window.tabs.addTab(other, "Send")
    p._remove_liquidity_tab(wallet)
    window.tabs.setCurrentIndex(window.tabs.indexOf(other))
    assert dialog.count == 0
