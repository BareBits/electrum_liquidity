"""The one-time trampoline-routing warning, and the banner that outlasts it.

Trampoline is Electrum's default routing mode and the worse one for this plugin
(dearer swaps, a handful of central nodes to open channels to, no gossip to read
node names from), so the plugin says so once per wallet and offers to switch.

The two things most worth pinning are the ones a careless implementation gets
wrong: that "once" really is once however the question was answered, and that
answering it cannot be confused with the routing mode itself -- which lives in
Electrum's process-wide config, while the "already asked" flag is per wallet.

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os
from typing import List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QPushButton, QTabWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)

SETTINGS_TAB = 0

WARNING_TEXT = (
    "Warning: Trampoline routing is enabled (and gossip is disabled). "
    "Trampoline routing is more expensive, more centralized, and makes this "
    "plugin less useful. Turn it off and turn on gossip?")


# --- scaffolding ---------------------------------------------------------
class _Answered:
    def __init__(self, answer: Optional[bool]) -> None:
        self.answer = answer
        self.asked: List[dict] = []

    def __call__(self, window, **kwargs) -> Optional[bool]:
        self.asked.append(kwargs)
        return self.answer

    @property
    def count(self) -> int:
        return len(self.asked)


@pytest.fixture
def dialog(monkeypatch):
    stub = _Answered(None)
    monkeypatch.setattr(qt_mod, "_ask_two_choices", stub)
    return stub


class _FakeNetwork:
    def __init__(self) -> None:
        self.gossip_starts = 0

    def start_gossip(self) -> None:
        self.gossip_starts += 1


def _managed(plugin, wallet) -> None:
    """Make the plugin consider this wallet one of its own, which is the gate on
    saying anything about routing at all."""
    plugin.wallets[wallet] = object()


def _trampoline_plugin(*, trampoline: bool = True, network=None):
    p = _make_plugin()
    p.config.LIGHTNING_USE_GOSSIP = not trampoline
    window, wallet = _FakeWindow(), _FakeWallet()
    if network is not None:
        wallet.network = network
    _managed(p, wallet)
    return p, window, wallet


def _banner_button(plugin, wallet) -> QPushButton:
    tabs = plugin._tabs[wallet].container.findChild(QTabWidget)
    return [b for b in tabs.widget(SETTINGS_TAB).findChildren(QPushButton)
            if b.text() == "Switch to full gossip mode"][0]


# --- reading the routing mode --------------------------------------------
def test_trampoline_is_the_absence_of_gossip(qapp):
    # Electrum's ConfigVar is inverted: use_gossip False IS trampoline mode, so
    # "trampoline on and gossip off" is one condition, not two.
    p = _make_plugin()
    p.config.LIGHTNING_USE_GOSSIP = False
    assert p.trampoline_routing_active() is True
    p.config.LIGHTNING_USE_GOSSIP = True
    assert p.trampoline_routing_active() is False


def test_a_config_without_the_setting_is_not_called_trampoline(qapp):
    # An older Electrum or a stub config is not grounds for nagging.
    from types import SimpleNamespace
    p = _make_plugin()
    p.config = SimpleNamespace()
    assert p.trampoline_routing_active() is False


# --- the one-time dialog -------------------------------------------------
def test_the_warning_is_shown_with_the_specified_wording(qapp, dialog):
    p, window, wallet = _trampoline_plugin()
    p._maybe_warn_trampoline(window, wallet)
    assert dialog.count == 1
    asked = dialog.asked[0]
    assert asked["text"] == WARNING_TEXT
    assert asked["accept_label"] == "Yes, switch to full gossip mode (suggested)"
    assert asked["reject_label"] == "No"


def test_saying_yes_switches_to_gossip(qapp, dialog):
    dialog.answer = True
    network = _FakeNetwork()
    p, window, wallet = _trampoline_plugin(network=network)
    p._maybe_warn_trampoline(window, wallet)
    assert p.config.LIGHTNING_USE_GOSSIP is True
    assert network.gossip_starts == 1
    assert p.trampoline_routing_active() is False


def test_saying_no_leaves_the_routing_alone(qapp, dialog):
    dialog.answer = False
    network = _FakeNetwork()
    p, window, wallet = _trampoline_plugin(network=network)
    p._maybe_warn_trampoline(window, wallet)
    assert p.config.LIGHTNING_USE_GOSSIP is False
    assert network.gossip_starts == 0


def test_it_is_asked_only_once_whichever_way_it_was_answered(qapp, dialog):
    dialog.answer = False
    p, window, wallet = _trampoline_plugin()
    p._maybe_warn_trampoline(window, wallet)
    p._maybe_warn_trampoline(window, wallet)
    p._maybe_warn_trampoline(window, wallet)
    assert dialog.count == 1
    assert bool(p.settings(wallet).INBOUND_LIQUIDITY_TRAMPOLINE_WARNED)


def test_the_warned_flag_is_per_wallet(qapp, dialog):
    # The routing mode is process-wide, but the record of having had the
    # conversation belongs to the wallet: a second wallet is a second person to
    # tell, and declining on one must not silence the other.
    dialog.answer = False
    p, window, wallet_a = _trampoline_plugin()
    p._maybe_warn_trampoline(window, wallet_a)
    assert dialog.count == 1
    wallet_b = _FakeWallet()
    _managed(p, wallet_b)
    p._maybe_warn_trampoline(window, wallet_b)
    assert dialog.count == 2
    assert bool(p.settings(wallet_a).INBOUND_LIQUIDITY_TRAMPOLINE_WARNED)
    assert bool(p.settings(wallet_b).INBOUND_LIQUIDITY_TRAMPOLINE_WARNED)


def test_nothing_is_said_when_gossip_is_already_on(qapp, dialog):
    p, window, wallet = _trampoline_plugin(trampoline=False)
    p._maybe_warn_trampoline(window, wallet)
    assert dialog.count == 0


def test_a_gossip_user_is_not_pre_marked_as_warned(qapp, dialog):
    # If they switch to trampoline later, that is exactly when this is worth
    # saying -- so it must still be sayable then.
    p, window, wallet = _trampoline_plugin(trampoline=False)
    p._maybe_warn_trampoline(window, wallet)
    assert not bool(p.settings(wallet).INBOUND_LIQUIDITY_TRAMPOLINE_WARNED)
    p.config.LIGHTNING_USE_GOSSIP = False
    p._maybe_warn_trampoline(window, wallet)
    assert dialog.count == 1


def test_an_unmanaged_wallet_is_not_lectured_about_routing(qapp, dialog):
    # No Lightning (or offline): its routing mode is none of our business.
    p, window, wallet = _trampoline_plugin()
    p.wallets.clear()
    p._maybe_warn_trampoline(window, wallet)
    assert dialog.count == 0
    assert not bool(p.settings(wallet).INBOUND_LIQUIDITY_TRAMPOLINE_WARNED)


def test_a_dialog_that_cannot_be_shown_is_re_asked_next_time(qapp, dialog):
    dialog.answer = None
    p, window, wallet = _trampoline_plugin()
    p._maybe_warn_trampoline(window, wallet)
    assert not bool(p.settings(wallet).INBOUND_LIQUIDITY_TRAMPOLINE_WARNED)
    dialog.answer = False
    p._maybe_warn_trampoline(window, wallet)
    assert dialog.count == 2


# --- switching without a network -----------------------------------------
def test_an_offline_wallet_still_gets_the_setting(qapp):
    # Refusing to change a preference because the network is down would be
    # worse than saving it and syncing on the next run.
    p, _window, wallet = _trampoline_plugin()
    assert p.switch_to_gossip_routing(wallet) is True
    assert p.config.LIGHTNING_USE_GOSSIP is True


def test_a_network_that_cannot_start_gossip_does_not_fail_the_switch(qapp):
    class _Broken:
        def start_gossip(self):
            raise RuntimeError("no channel db")

    p, _window, wallet = _trampoline_plugin(network=_Broken())
    assert p.switch_to_gossip_routing(wallet) is True
    assert p.config.LIGHTNING_USE_GOSSIP is True


# --- the standing banner -------------------------------------------------
def test_the_banner_is_shown_while_trampoline_is_on(qapp, dialog):
    p, window, wallet = _trampoline_plugin()
    p._add_liquidity_tab(window, wallet)
    banner = p._tabs[wallet].trampoline_banner
    assert not banner.isHidden()
    assert "less effective" in banner.text()
    assert not _banner_button(p, wallet).isHidden()


def test_the_banner_is_absent_on_gossip(qapp, dialog):
    p, window, wallet = _trampoline_plugin(trampoline=False)
    p._add_liquidity_tab(window, wallet)
    assert p._tabs[wallet].trampoline_banner.isHidden()
    assert _banner_button(p, wallet).isHidden()


def test_the_banner_button_switches_to_gossip_and_takes_the_banner_down(qapp, dialog):
    network = _FakeNetwork()
    p, window, wallet = _trampoline_plugin(network=network)
    p._add_liquidity_tab(window, wallet)
    _banner_button(p, wallet).click()
    assert p.config.LIGHTNING_USE_GOSSIP is True
    assert network.gossip_starts == 1
    assert p._tabs[wallet].trampoline_banner.isHidden()


def test_the_banner_follows_the_setting_on_refresh(qapp, dialog):
    # The routing mode is changeable from Electrum's own Preferences, so the
    # banner cannot be decided once at build time.
    p, window, wallet = _trampoline_plugin(trampoline=False)
    p._add_liquidity_tab(window, wallet)
    assert p._tabs[wallet].trampoline_banner.isHidden()
    p.config.LIGHTNING_USE_GOSSIP = False       # switched to trampoline elsewhere
    p._tabs[wallet].refresh()
    assert not p._tabs[wallet].trampoline_banner.isHidden()


def test_answering_yes_takes_down_an_open_tabs_banner(qapp, dialog):
    dialog.answer = True
    network = _FakeNetwork()
    p, window, wallet = _trampoline_plugin(network=network)
    p._add_liquidity_tab(window, wallet)
    assert not p._tabs[wallet].trampoline_banner.isHidden()
    p._maybe_warn_trampoline(window, wallet)
    assert p._tabs[wallet].trampoline_banner.isHidden()
