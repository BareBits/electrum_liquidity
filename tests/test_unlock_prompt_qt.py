"""GUI-glue tests for the "your wallet is locked" prompt.

Background: typing your password when *opening* an encrypted wallet only
decrypts the wallet file. On desktop Qt it never calls ``wallet.unlock()``, so
Electrum's in-memory unlock cache -- the one the plugin reads before signing --
stays empty, and automation silently parks on "wallet locked" with the remedy
buried in the Wallet menu. So the plugin now asks.

What matters about asking:

  * it happens on the GUI thread, from a tick that runs on the asyncio thread;
  * it says who is asking, so the dialog is not a bare password box out of
    nowhere;
  * "no" is respected -- automation ticks every few minutes and every one of
    those ticks re-reports "locked", so without a cooldown a dismissed prompt
    would be back within minutes;
  * "no" means "not now", not "turn the plugin off";
  * unlocking by any route resumes automation without waiting for the heartbeat.

Needs PyQt6 and Electrum's Qt GUI importable; skipped outside the electrum venv.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from typing import List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity.log_buffer import (  # type: ignore  # noqa: E402
    LogCapture,
    LogRingBuffer,
)

from test_qt_tab import _FakeConfig, _FakeDB  # type: ignore  # noqa: E402

PASSWORD = "hunter2"


class _FakeWallet:
    def __init__(self, *, keystore_encrypted: bool = True,
                 unlocked_password: Optional[str] = None) -> None:
        self.db = _FakeDB()
        self._keystore_encrypted = keystore_encrypted
        self._unlocked_password = unlocked_password

    def basename(self) -> str:
        return "test-wallet"

    def save_db(self) -> None:
        pass

    def has_keystore_encryption(self) -> bool:
        return self._keystore_encrypted

    def has_password(self) -> bool:
        return self._keystore_encrypted

    def get_unlocked_password(self) -> Optional[str]:
        return self._unlocked_password


class _FakeWindow:
    """Stands in for ElectrumWindow. ``unlock_wallet`` is the real GUI entry
    point the plugin delegates to (it also updates the padlock and hands the
    password to the txbatcher); here it records the message it was given and
    unlocks or not, standing in for the user typing or cancelling."""

    def __init__(self, wallet: _FakeWallet, *, user_unlocks: bool = True) -> None:
        self.tabs = QTabWidget()
        self.wallet = wallet
        self.user_unlocks = user_unlocks
        self.messages: List[str] = []

    def unlock_wallet(self, message: Optional[str] = None) -> None:
        self.messages.append(message or "")
        if self.user_unlocks:
            self.wallet._unlocked_password = PASSWORD


def _plugin() -> qt_mod.Plugin:
    p = object.__new__(qt_mod.Plugin)
    p.config = _FakeConfig()
    p.logger = logging.getLogger("test.inbound_liquidity.unlock_qt")
    p.signals = None
    p._tabs = {}
    p._tick_status = {}
    p.wallets = {}
    p._unlock_declined_at = {}
    p._unlock_prompting = set()
    p._unlock_prompt_cooldown_sec = qt_mod.UNLOCK_PROMPT_COOLDOWN_SEC
    p.evaluations: List[object] = []
    p.request_evaluation = lambda wallet, **kw: p.evaluations.append(wallet)
    # Only needed by the tests that build the real tab (the Log sub-tab reads
    # both); a fresh buffer per plugin keeps them isolated.
    p.log_buffer = LogRingBuffer()
    p.log_capture = LogCapture(p.log_buffer,
                               root_logger_name="test.inbound_liquidity.unlock_qt")
    p._last_offers = {}
    return p


def _wire_tab(p: qt_mod.Plugin, wallet: _FakeWallet, window: _FakeWindow) -> SimpleNamespace:
    """A stand-in _TabState: the prompt path only needs the window and the
    button-visibility callback, not a built tab."""
    state = SimpleNamespace(window=window, wallet=wallet, locked_flags=[])
    state.set_locked = lambda locked: state.locked_flags.append(locked)
    p._tabs[wallet] = state
    return state


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --- the prompt itself ------------------------------------------------------
def test_locked_wallet_is_offered_an_unlock(qapp):
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert len(window.messages) == 1


def test_the_prompt_says_who_is_asking(qapp):
    """A bare password box appearing on its own is alarming; the dialog has to
    name the plugin and say what is blocked."""
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    msg = window.messages[0]
    assert "Inbound Liquidity" in msg
    assert "paused" in msg.lower()


def test_no_prompt_when_the_wallet_is_already_unlocked(qapp):
    # The tick and the queued signal race; by delivery time the user may have
    # unlocked via the Wallet menu.
    p = _plugin()
    w = _FakeWallet(unlocked_password=PASSWORD)
    window = _FakeWindow(w)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert window.messages == []


def test_no_prompt_without_a_window(qapp):
    # close_wallet races a queued signal: no tab, nothing to parent a dialog to.
    p = _plugin()
    w = _FakeWallet()
    p._on_unlock_requested_ui(w)          # no tab registered
    assert p._unlock_declined_at == {}


# --- unlocking succeeds -----------------------------------------------------
def test_unlocking_resumes_automation_immediately(qapp):
    """Having just unlocked *for* the plugin, the user should not then wait out
    a 10-minute heartbeat to see it act."""
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=True)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert w.get_unlocked_password() == PASSWORD
    assert p.evaluations == [w]
    assert w not in p._unlock_declined_at


def test_unlocking_hides_the_button(qapp):
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=True)
    state = _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert state.locked_flags[-1] is False


# --- the user says no -------------------------------------------------------
def test_declining_starts_the_cooldown(qapp):
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert len(window.messages) == 1
    assert w in p._unlock_declined_at
    # Every subsequent tick still reports "locked"; none of them may re-ask.
    for _ in range(5):
        p._on_unlock_requested_ui(w)
    assert len(window.messages) == 1


def test_the_prompt_returns_after_the_cooldown(qapp):
    """Not suppressed forever: a plugin quietly doing nothing is the state this
    whole gate exists to make visible."""
    p = _plugin()
    p._unlock_prompt_cooldown_sec = 0.0        # cooldown already served
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    p._on_unlock_requested_ui(w)
    assert len(window.messages) == 2


def test_declining_does_not_switch_automation_off(qapp):
    # "Not now" is about the dialog, not about the feature.
    p = _plugin()
    p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED = True
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is True


def test_declining_leaves_the_button_showing(qapp):
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    state = _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert state.locked_flags[-1] is True


def test_unlocking_by_another_route_clears_the_cooldown(qapp):
    """Unlocked via Wallet > Unlock after declining ours: the next locked
    detection (they locked it again) must ask, not sit out a stale cooldown."""
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)
    assert w in p._unlock_declined_at

    w._unlocked_password = PASSWORD            # unlocked from Electrum's menu
    p._on_unlock_requested_ui(w)               # a straggler signal
    assert w not in p._unlock_declined_at


# --- guards -----------------------------------------------------------------
def test_no_second_dialog_while_one_is_open(qapp):
    """unlock_wallet is modal, but Qt keeps delivering queued signals while it
    runs, so a second tick must not stack a second dialog."""
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)

    reentered: List[int] = []

    def _unlock_wallet(message=None):
        window.messages.append(message or "")
        # A tick lands while the dialog is up.
        p._on_unlock_requested_ui(w)
        reentered.append(len(window.messages))

    window.unlock_wallet = _unlock_wallet
    p._on_unlock_requested_ui(w)
    assert reentered == [1]                    # the re-entrant call added nothing
    assert len(window.messages) == 1
    assert p._unlock_prompting == set()        # guard released


def test_a_failing_dialog_does_not_break_the_tab(qapp):
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w)
    _wire_tab(p, w, window)

    def _boom(message=None):
        raise RuntimeError("no window manager")

    window.unlock_wallet = _boom
    p._on_unlock_requested_ui(w)               # must not raise
    assert p._unlock_prompting == set()
    assert w in p._unlock_declined_at          # treated as "not unlocked"


def test_the_button_bypasses_the_cooldown(qapp):
    """The cooldown exists to stop *us* nagging; a user pressing the button is
    asking for the dialog."""
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)

    p._on_unlock_requested_ui(w)               # declined -> cooldown running
    p._prompt_unlock(w)                        # what the button calls
    assert len(window.messages) == 2


def test_reopening_a_wallet_clears_the_cooldown(qapp):
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w, user_unlocks=False)
    _wire_tab(p, w, window)
    p.stop_wallet = lambda wallet: None
    p._remove_liquidity_tab = lambda wallet: p._tabs.pop(wallet, None)

    p._on_unlock_requested_ui(w)
    assert w in p._unlock_declined_at

    p.close_wallet(w)
    assert w not in p._unlock_declined_at


# --- the button on the real tab --------------------------------------------
def test_the_button_is_hidden_until_the_wallet_is_locked(qapp):
    """Built against the real Settings tab rather than a stand-in, so the
    handle wiring (_build_liquidity_tab -> handles -> _TabState) is covered too."""
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=False)      # unencrypted: nothing to unlock
    window = _FakeWindow(w)
    p._add_liquidity_tab(window, w)
    state = p._tabs[w]
    assert state.unlock_button.isHidden()

    # Same wallet, now password-protected and locked (the user set a password).
    w._keystore_encrypted = True
    p._on_status_changed_ui(w, "wallet locked")
    assert not state.unlock_button.isHidden()

    # ...and it goes away again once unlocked.
    w._unlocked_password = PASSWORD
    p._on_status_changed_ui(w, "sleeping")
    assert state.unlock_button.isHidden()


def test_pressing_the_button_prompts(qapp):
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    window = _FakeWindow(w, user_unlocks=True)
    p._add_liquidity_tab(window, w)

    p._tabs[w].unlock_button.click()
    assert len(window.messages) == 1
    assert w.get_unlocked_password() == PASSWORD


# --- the asyncio-thread hand-off -------------------------------------------
def test_on_wallet_locked_emits_rather_than_prompting_directly(qapp):
    """The hook runs on the asyncio thread; touching widgets there would be a
    crash, so it may only emit."""
    p = _plugin()
    w = _FakeWallet()
    window = _FakeWindow(w)
    _wire_tab(p, w, window)

    seen: List[object] = []
    p.signals = qt_mod._Signals()
    p.signals.unlock_requested.connect(lambda wallet: seen.append(wallet))

    p.on_wallet_locked(w)
    assert seen == [w]
    assert window.messages == []               # not prompted inline


def test_on_wallet_locked_is_safe_before_the_gui_exists(qapp):
    # Ticks can fire before load_wallet has built the signals object.
    p = _plugin()
    p.signals = None
    p.on_wallet_locked(_FakeWallet())          # must not raise
