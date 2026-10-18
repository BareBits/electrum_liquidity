"""Every open wallet gets its own Liquidity tab, and keeps it.

The reported symptom was a Liquidity tab in only one of several open wallets.
Electrum runs ``load_wallet`` once per window, and the plugin keys its tab
handles by wallet, so the happy path was already right -- these tests pin the
paths where it was NOT:

  * a window REPLACED without ``close_wallet`` firing (``reload_window``, which
    the Plugins dialog uses, closes the window and builds a new one regardless
    of whether the close was accepted), which left a handle pointing at a dead
    window and made the next add a no-op -- permanently, for that wallet;
  * an exception while building or populating a tab, which used to escape into
    Electrum's ``run_hook`` and cost that wallet its tab silently.

Offscreen Qt against the fake window/wallet pair the other GUI-glue tests use.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QApplication, QTabWidget, QWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402

from test_qt_tab import (  # type: ignore  # noqa: E402
    _FakeWallet,
    _FakeWindow,
    _make_plugin,
    _tab_titles,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _silence_log_tab_timer(monkeypatch):
    """Stop the Log tab's QTimer from firing into widgets Python is collecting.
    Same reason, and same three constraints, as the fixture of this name in
    test_qt_tab.py -- see the long note there."""
    from PyQt6.QtCore import QTimer
    original = qt_mod.Plugin._add_liquidity_tab

    def _add_and_silence(self, window, wallet, *args, **kwargs):
        result = original(self, window, wallet, *args, **kwargs)
        state = (getattr(self, "_tabs", None) or {}).get(wallet)
        container = getattr(state, "container", None)
        if container is not None:
            for timer in container.findChildren(QTimer):
                try:
                    timer.timeout.disconnect()
                except TypeError:
                    pass
                timer.stop()
        return result

    monkeypatch.setattr(qt_mod.Plugin, "_add_liquidity_tab", _add_and_silence)
    yield


def _liquidity_tab_count(window: _FakeWindow) -> int:
    return _tab_titles(window).count("Liquidity")


# --- one tab per open wallet ---------------------------------------------
def test_every_open_wallet_gets_a_tab(qapp):
    p = _make_plugin()
    windows = [(_FakeWindow(), _FakeWallet()) for _ in range(3)]
    for window, wallet in windows:
        p._add_liquidity_tab(window, wallet)
    for window, wallet in windows:
        assert _liquidity_tab_count(window) == 1
        assert wallet in p._tabs
    assert len(p._tabs) == 3


def test_each_tab_belongs_to_its_own_window(qapp):
    """The handle has to name the window it is in; a tab removed for one wallet
    must not reach into another wallet's window."""
    p = _make_plugin()
    w1, wal1 = _FakeWindow(), _FakeWallet()
    w2, wal2 = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(w1, wal1)
    p._add_liquidity_tab(w2, wal2)
    assert p._tabs[wal1].window is w1
    assert p._tabs[wal2].window is w2
    p._remove_liquidity_tab(wal1)
    assert _liquidity_tab_count(w1) == 0
    assert _liquidity_tab_count(w2) == 1


def test_adding_twice_for_the_same_window_is_a_no_op(qapp):
    """``load_wallet`` can fire more than once for a window's lifetime; the
    second must not stack a second tab."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    first = p._tabs[wallet].container
    p._add_liquidity_tab(window, wallet)
    assert _liquidity_tab_count(window) == 1
    assert p._tabs[wallet].container is first, "the tab was rebuilt needlessly"


# --- the stale-handle path ------------------------------------------------
def test_a_replaced_window_gets_a_fresh_tab(qapp):
    """``reload_window``: the window is closed and rebuilt. If the close did not
    fire ``close_wallet`` -- which happens when ``closeEvent`` ignores it -- the
    plugin is still holding a handle for the old window. The new window must
    still get a tab."""
    p = _make_plugin()
    old_window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(old_window, wallet)
    assert _liquidity_tab_count(old_window) == 1

    new_window = _FakeWindow()          # same wallet, new window, no close hook
    p._add_liquidity_tab(new_window, wallet)
    assert _liquidity_tab_count(new_window) == 1, "the replacement window has no tab"
    assert p._tabs[wallet].window is new_window


def test_a_tab_taken_out_of_the_bar_is_rebuilt(qapp):
    """Belt and braces for the same class of failure: the handle is live but its
    container is no longer in the window's tab bar."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    index = window.tabs.indexOf(p._tabs[wallet].container)
    window.tabs.removeTab(index)        # as if something else took it out
    assert _liquidity_tab_count(window) == 0

    p._add_liquidity_tab(window, wallet)
    assert _liquidity_tab_count(window) == 1


def test_closing_a_window_drops_only_that_windows_handle(qapp):
    """``on_close_window`` is the backstop for teardown paths that never close
    the wallet. It must not disturb another wallet's tab."""
    p = _make_plugin()
    w1, wal1 = _FakeWindow(), _FakeWallet()
    w2, wal2 = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(w1, wal1)
    p._add_liquidity_tab(w2, wal2)
    w1.wallet = wal1

    p.on_close_window(w1)
    assert wal1 not in p._tabs
    assert wal2 in p._tabs and _liquidity_tab_count(w2) == 1


def test_closing_a_window_that_is_not_the_handles_window_is_a_no_op(qapp):
    """A late teardown signal for a window the wallet has already moved off
    must not take the tab the wallet is now using."""
    p = _make_plugin()
    old_window, wallet = _FakeWindow(), _FakeWallet()
    new_window = _FakeWindow()
    p._add_liquidity_tab(old_window, wallet)
    p._add_liquidity_tab(new_window, wallet)      # the handle moves to the new one
    old_window.wallet = wallet

    p.on_close_window(old_window)
    assert wallet in p._tabs
    assert _liquidity_tab_count(new_window) == 1


def test_closing_a_window_with_no_wallet_is_harmless(qapp):
    p = _make_plugin()
    p.on_close_window(_FakeWindow())              # a window mid-teardown


# --- one wallet's failure must not cost another its tab -------------------
def test_a_build_failure_is_contained_and_logged(qapp, caplog):
    """The build runs inside the ``load_wallet`` hook, where an exception is
    swallowed by Electrum's run_hook. One wallet with unreadable stored state
    must not take another wallet's tab with it."""
    p = _make_plugin()
    bad_window, bad_wallet = _FakeWindow(), _FakeWallet()
    good_window, good_wallet = _FakeWindow(), _FakeWallet()

    original = qt_mod.Plugin._build_liquidity_tab

    def _build(self, window, wallet):
        if wallet is bad_wallet:
            raise RuntimeError("this wallet's stored state is nonsense")
        return original(self, window, wallet)

    with caplog.at_level(logging.ERROR):
        qt_mod.Plugin._build_liquidity_tab = _build
        try:
            p._add_liquidity_tab(bad_window, bad_wallet)
            p._add_liquidity_tab(good_window, good_wallet)
        finally:
            qt_mod.Plugin._build_liquidity_tab = original

    assert _liquidity_tab_count(bad_window) == 0
    assert bad_wallet not in p._tabs, "a half-built tab must not be remembered"
    assert _liquidity_tab_count(good_window) == 1
    assert any("could not build the Liquidity tab" in r.message
               for r in caplog.records), "the failure has to be diagnosable"


def test_a_refresh_failure_still_leaves_the_tab(qapp, caplog):
    """The first refresh reads this wallet's stored history. A single malformed
    record there must cost stale contents, not the whole tab."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()

    original = qt_mod.Plugin._build_liquidity_tab

    def _build(self, window_, wallet_):
        container, handles = original(self, window_, wallet_)

        def _boom():
            raise RuntimeError("one malformed log record")

        handles["refresh"] = _boom
        return container, handles

    with caplog.at_level(logging.ERROR):
        qt_mod.Plugin._build_liquidity_tab = _build
        try:
            p._add_liquidity_tab(window, wallet)
        finally:
            qt_mod.Plugin._build_liquidity_tab = original

    assert _liquidity_tab_count(window) == 1
    assert wallet in p._tabs
    assert any("could not populate the Liquidity tab" in r.message
               for r in caplog.records)


def test_a_window_that_cannot_take_the_tab_is_not_remembered(qapp, caplog):
    p = _make_plugin()

    class _NoTabs(_FakeWindow):
        def __init__(self) -> None:
            super().__init__()
            self.tabs = None            # as if the window is already torn down

    window, wallet = _NoTabs(), _FakeWallet()
    with caplog.at_level(logging.ERROR):
        p._add_liquidity_tab(window, wallet)
    assert wallet not in p._tabs
    assert any("could not add the Liquidity tab" in r.message
               for r in caplog.records)


# --- settings are the wallet's own, per tab ------------------------------
def test_each_tab_reads_and_writes_its_own_wallets_settings(qapp):
    """Two tabs, two wallets: applying a value on one must not move the other.
    Driven through the real Settings tab, not through the settings layer."""
    from PyQt6.QtWidgets import QLineEdit, QPushButton

    p = _make_plugin()
    w1, wal1 = _FakeWindow(), _FakeWallet()
    w2, wal2 = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(w1, wal1)
    p._add_liquidity_tab(w2, wal2)

    def _settings_tab(wallet):
        return p._tabs[wallet].container.findChild(QTabWidget).widget(0)

    def _apply(wallet, max_channels: str) -> None:
        tab = _settings_tab(wallet)
        edits = [e for e in tab.findChildren(QLineEdit) if type(e) is QLineEdit]
        edits[1].setText(max_channels)          # "Maximum number of channels"
        next(b for b in tab.findChildren(QPushButton)
             if b.text() == "Apply").click()

    _apply(wal1, "7")
    assert p.settings(wal1).INBOUND_LIQUIDITY_MAX_CHANNELS == 7
    assert p.settings(wal2).INBOUND_LIQUIDITY_MAX_CHANNELS \
        == p.config.INBOUND_LIQUIDITY_MAX_CHANNELS

    _apply(wal2, "1")
    assert p.settings(wal2).INBOUND_LIQUIDITY_MAX_CHANNELS == 1
    assert p.settings(wal1).INBOUND_LIQUIDITY_MAX_CHANNELS == 7, \
        "the other wallet's setting moved"


def test_the_arm_switch_arms_only_its_own_wallet(qapp):
    """The switch that decides whether a wallet moves funds at all."""
    from electrum.plugins.inbound_liquidity.qt_widgets import ToggleSwitch  # type: ignore

    p = _make_plugin()
    p.request_evaluation = lambda wallet, **kw: None
    w1, wal1 = _FakeWindow(), _FakeWallet()
    w2, wal2 = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(w1, wal1)
    p._add_liquidity_tab(w2, wal2)

    def _slider(wallet):
        tab = p._tabs[wallet].container.findChild(QTabWidget).widget(0)
        return tab.findChild(ToggleSwitch)

    _slider(wal1).setChecked(True)
    assert p.settings(wal1).INBOUND_LIQUIDITY_AUTOMATION_ENABLED is True
    assert p.settings(wal2).INBOUND_LIQUIDITY_AUTOMATION_ENABLED is False
    assert _slider(wal2).isChecked() is False


# --- the reset affordance -------------------------------------------------
class _AskingWindow(_FakeWindow):
    """A window that can answer a confirmation dialog, which the stock fake
    cannot (nothing else in these tests asks the user anything)."""

    def __init__(self, answer: bool = True) -> None:
        super().__init__()
        self.answer = answer
        self.asked: List[str] = []

    def question(self, msg, **kwargs) -> bool:
        self.asked.append(msg)
        return self.answer


def _advanced_tab(p, wallet):
    return p._tabs[wallet].container.findChild(QTabWidget).widget(3)


def _reset_button(p, wallet):
    from PyQt6.QtWidgets import QPushButton
    return next(b for b in _advanced_tab(p, wallet).findChildren(QPushButton)
                if b.text() == "Reset this wallet to defaults")


def test_reset_clears_only_the_asking_wallet(qapp):
    """Settings being per wallet raises "how do I undo mine?", and the answer
    must not reach into another wallet."""
    p = _make_plugin()
    w1, wal1 = _AskingWindow(True), _FakeWallet()
    w2, wal2 = _AskingWindow(True), _FakeWallet()
    p._add_liquidity_tab(w1, wal1)
    p._add_liquidity_tab(w2, wal2)
    p.set_setting(wal1, "INBOUND_LIQUIDITY_MAX_CHANNELS", 8)
    p.set_setting(wal2, "INBOUND_LIQUIDITY_MAX_CHANNELS", 3)

    _reset_button(p, wal1).click()
    assert p.settings(wal1).INBOUND_LIQUIDITY_MAX_CHANNELS \
        == p.config.INBOUND_LIQUIDITY_MAX_CHANNELS
    assert p.settings(wal2).INBOUND_LIQUIDITY_MAX_CHANNELS == 3


def test_reset_is_not_done_without_confirmation(qapp):
    p = _make_plugin()
    window, wallet = _AskingWindow(False), _FakeWallet()   # the user says no
    p._add_liquidity_tab(window, wallet)
    p.set_setting(wallet, "INBOUND_LIQUIDITY_MAX_CHANNELS", 8)
    _reset_button(p, wallet).click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == 8


def test_a_window_that_cannot_ask_does_not_reset(qapp):
    """A confirmation that cannot be shown must leave the settings alone -- the
    one answer that cannot lose anything."""
    p = _make_plugin()
    window, wallet = _AskingWindow(True), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    p.set_setting(wallet, "INBOUND_LIQUIDITY_MAX_CHANNELS", 8)

    def _boom(*args, **kwargs):
        raise RuntimeError("no window to ask on")

    window.question = _boom
    _reset_button(p, wallet).click()
    assert p.settings(wallet).INBOUND_LIQUIDITY_MAX_CHANNELS == 8
