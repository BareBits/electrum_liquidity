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

from conftest import seed_settings  # noqa: E402

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
    p._update_opt_in_asked = set()
    p.checks: List[object] = []
    p._request_update_check = lambda wallet: p.checks.append(wallet)
    # Settings are per wallet now; hand this plugin's wallets what its
    # config carries -- see conftest.seed_settings.
    seed_settings(p)
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
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is True
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED is True
    assert p.checks == [w], "the user said yes and then had to wait for a heartbeat"


def test_no_is_remembered_and_never_checks(qapp):
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow(answer=False)
    p._maybe_prompt_update_opt_in(window, w)
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED is True
    assert p.checks == []


# --- not being asked again ------------------------------------------------
def test_a_user_who_declined_is_not_asked_again(qapp):
    p, w = _plugin(), _FakeWallet()
    first = _FakeWindow(answer=False)
    p._maybe_prompt_update_opt_in(first, w)

    # A later session: the flag is persisted, the in-session guard is not.
    p._update_opt_in_asked = set()
    second = _FakeWindow(answer=True)
    p._maybe_prompt_update_opt_in(second, w)
    assert second.asked == []
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False


def test_enabling_it_from_advanced_settings_stops_the_question(qapp):
    # Turned on by hand before ever being asked: the answer is already given.
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow()
    p.set_setting(w, "INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED", True)
    p._maybe_prompt_update_opt_in(window, w)
    assert window.asked == []


def test_the_same_wallet_is_not_asked_twice_in_one_session(qapp):
    # The persisted flag records the answer; the in-session guard is what stops
    # a second load_wallet for the SAME wallet (the Plugins dialog's
    # reload_windows(), a window replaced without a close hook) putting a
    # second dialog on screen before the first is answered.
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow()
    p._maybe_prompt_update_opt_in(window, w)
    p.set_setting(w, "INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED", False)  # as if unanswered
    p._maybe_prompt_update_opt_in(window, w)
    assert len(window.asked) == 1


def test_every_wallet_is_asked_in_its_own_right(qapp):
    """Electrum can hold several wallets open in one process, and this opt-in
    is a PER-WALLET setting -- so a second wallet is a second decision. The
    guard used to be one flag for the whole process, which meant the first
    wallet opened was asked and every other wallet was silently skipped for the
    rest of the session."""
    p, window = _plugin(), _FakeWindow()
    a, b, c = _FakeWallet(), _FakeWallet(), _FakeWallet()
    for wallet in (a, b, c):
        p._maybe_prompt_update_opt_in(window, wallet)
    assert len(window.asked) == 3, \
        "a wallet after the first was never asked whether it may check GitHub"


def test_one_wallets_answer_does_not_answer_for_another(qapp):
    p = _plugin()
    a, b = _FakeWallet(), _FakeWallet()
    p._maybe_prompt_update_opt_in(_FakeWindow(answer=True), a)
    p._maybe_prompt_update_opt_in(_FakeWindow(answer=False), b)
    assert p.settings(a).INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is True
    assert p.settings(b).INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False
    assert p.checks == [a], "the wallet that declined was checked anyway"


def test_a_failed_prompt_leaves_the_other_wallets_askable(qapp):
    """The retry has to be scoped to the wallet whose dialog failed: clearing
    one process-wide flag used to make the failure the only thing that could
    re-open the question for anyone."""
    p = _plugin()
    a, b = _FakeWallet(), _FakeWallet()
    p._maybe_prompt_update_opt_in(_FakeWindow(raises=True), a)
    window = _FakeWindow(answer=False)
    p._maybe_prompt_update_opt_in(window, b)
    assert len(window.asked) == 1
    retry = _FakeWindow(answer=False)
    p._maybe_prompt_update_opt_in(retry, a)
    assert len(retry.asked) == 1, "the wallet whose dialog failed was not re-asked"


# --- failure is harmless --------------------------------------------------
def test_a_dialog_that_cannot_be_shown_does_not_break_loading(qapp):
    p, w, window = _plugin(), _FakeWallet(), _FakeWindow(raises=True)
    p._maybe_prompt_update_opt_in(window, w)      # must not raise
    # Nothing was decided on the user's behalf, and the question is still open.
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED is False
    assert p.settings(w).INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED is False
    assert p.checks == []

    working = _FakeWindow(answer=True)
    p._maybe_prompt_update_opt_in(working, w)     # asked again next time
    assert len(working.asked) == 1
