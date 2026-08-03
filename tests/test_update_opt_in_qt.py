"""GUI-glue tests for the update-check opt-in prompt.

The plugin can ask GitHub once a day whether a newer version of itself exists.
That request tells a third party this wallet's IP and that it is running, which
is not something to switch on for someone quietly -- so it is asked, once, in
plain words, and both answers stick:

  * a user who has never been asked is asked;
  * "no" is remembered forever ("prompted" is stored separately from "enabled",
    so declining is not re-asked at every startup);
  * "yes" enables it and checks immediately, rather than waiting for a heartbeat;
  * opening a second wallet does not stack a second dialog;
  * a dialog that cannot be shown never breaks loading the wallet.

Needs PyQt6 and Electrum's Qt GUI importable; skipped outside the electrum venv.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402

from test_qt_tab import _FakeConfig  # type: ignore  # noqa: E402


class _FakeWallet:
    def basename(self) -> str:
        return "test-wallet"


class _FakeWindow:
    """Stands in for ElectrumWindow. ``question`` is Electrum's own yes/no
    dialog (MessageBoxMixin); here it records what was asked and answers."""

    def __init__(self, *, answer: bool = True, raises: bool = False) -> None:
        self.tabs = QTabWidget()
        self.answer = answer
        self.raises = raises
        self.asked: List[str] = []
        self.titles: List[Optional[str]] = []

    def question(self, msg: str, *, title: Optional[str] = None, **kwargs) -> bool:
        if self.raises:
            raise RuntimeError("no GUI available")
        self.asked.append(msg)
        self.titles.append(title)
        return self.answer


def _plugin() -> qt_mod.Plugin:
    p = object.__new__(qt_mod.Plugin)
    p.config = _FakeConfig()
    p.logger = logging.getLogger("test.inbound_liquidity.update_opt_in")
    p._update_opt_in_asked = False
    p.checks: List[object] = []
    p._request_update_check = lambda wallet: p.checks.append(wallet)
    return p


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# --- being asked ----------------------------------------------------------
def test_a_first_run_user_is_asked(qapp):
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow()
    p._maybe_prompt_update_opt_in(window, w)
    assert len(window.asked) == 1


def test_the_question_states_the_privacy_cost_and_that_nothing_is_installed(qapp):
    # Consent is only meaningful if the dialog says what it costs. It must name
    # the third party, and it must not leave the user fearing a self-updater.
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow()
    p._maybe_prompt_update_opt_in(window, w)
    msg = window.asked[0].lower()
    assert "github.com" in msg
    assert "proxy" in msg
    assert "downloaded" in msg and "installed" in msg


def test_yes_enables_the_check_and_looks_immediately(qapp):
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow(answer=True)
    p._maybe_prompt_update_opt_in(window, w)
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is True
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED is True
    assert p.checks == [w], "the user said yes and then had to wait for a heartbeat"


def test_no_is_remembered_and_never_checks(qapp):
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow(answer=False)
    p._maybe_prompt_update_opt_in(window, w)
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED is True
    assert p.checks == []


# --- not being asked again ------------------------------------------------
def test_a_user_who_declined_is_not_asked_again(qapp):
    p, w = _plugin(), _FakeWallet()
    first = _FakeWindow(answer=False)
    p._maybe_prompt_update_opt_in(first, w)

    # A later session: the flag is persisted, the in-session guard is not.
    p._update_opt_in_asked = False
    second = _FakeWindow(answer=True)
    p._maybe_prompt_update_opt_in(second, w)
    assert second.asked == []
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False


def test_enabling_it_from_advanced_settings_stops_the_question(qapp):
    # Turned on by hand before ever being asked: the answer is already given.
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow()
    p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = True
    p._maybe_prompt_update_opt_in(window, w)
    assert window.asked == []


def test_a_second_wallet_does_not_stack_a_second_dialog(qapp):
    # Electrum can open several wallets at once; the config flag is only written
    # after the first dialog is answered, so the in-session guard is what stops
    # two dialogs racing onto the screen.
    p, window = _plugin(), _FakeWindow()
    p._maybe_prompt_update_opt_in(window, _FakeWallet())
    p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED = False   # as if unanswered
    p._maybe_prompt_update_opt_in(window, _FakeWallet())
    assert len(window.asked) == 1


# --- failure is harmless --------------------------------------------------
def test_a_dialog_that_cannot_be_shown_does_not_break_loading(qapp):
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow(raises=True)
    p._maybe_prompt_update_opt_in(window, w)      # must not raise
    # Nothing was decided on the user's behalf, and the question is still open.
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False
    assert p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED is False
    assert p.checks == []

    working = _FakeWindow(answer=True)
    p._maybe_prompt_update_opt_in(working, w)     # asked again next time
    assert len(working.asked) == 1
