"""GUI-glue tests for the persistent "Liquidity" tab.

These need PyQt6 and Electrum's Qt GUI importable; they run a headless
(offscreen) QApplication and exercise the tab lifecycle on a Plugin instance
built *without* BasePlugin.__init__ (so no network/parent), against a fake
ElectrumWindow whose `.tabs` is a real QTabWidget. Skipped when PyQt6 / the Qt
GUI is unavailable (e.g. running outside the electrum venv).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Both must import or these tests cannot run.
pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LOG_DB_KEY,
    DEFAULT_LOG_RETENTION_DAYS,
)
from electrum.plugins.inbound_liquidity import qt as qt_mod  # type: ignore  # noqa: E402


# --- fakes ---------------------------------------------------------------
class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self) -> None:
        self.db = _FakeDB()

    def save_db(self) -> None:
        pass


class _FakeConfig:
    """Plain attribute bag mirroring the INBOUND_LIQUIDITY_* ConfigVars."""
    def __init__(self) -> None:
        self.INBOUND_LIQUIDITY_AUTOMATION_ENABLED = False
        self.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY = False
        self.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT = 1_000_000
        self.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT = 10_000
        self.INBOUND_LIQUIDITY_MAX_CHANNELS = 2
        self.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT = 0.6
        self.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT = 25.0
        self.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT = 25_000
        self.INBOUND_LIQUIDITY_CHANNEL_PEER = ""
        self.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS = DEFAULT_LOG_RETENTION_DAYS
        self.INBOUND_LIQUIDITY_PREFERRED_NPUBS = ""
        self.INBOUND_LIQUIDITY_BANNED_NPUBS = ""
        self.INBOUND_LIQUIDITY_PREFERRED_PARTNERS = ""
        self.INBOUND_LIQUIDITY_BANNED_PARTNERS = ""
        self.INBOUND_LIQUIDITY_PARTNERS_STRICT = False
        self.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER = True
        self.INBOUND_LIQUIDITY_RELIABILITY_ENABLED = True
        self.INBOUND_LIQUIDITY_RELIABILITY_BASE_PENALTY_PCT = 0.5
        self.INBOUND_LIQUIDITY_RELIABILITY_PENALTY_CAP_PCT = 5.0
        self.INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS = 6.0
        self.INBOUND_LIQUIDITY_RELIABILITY_STUCK_TIMEOUT_MIN = 60
        self.INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED = True
        self.INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS = 3
        self.INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN = 60
        self.INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN = 180
        self.INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN = True
        self.INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED = True
        self.INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS = 2.0
        self.INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT = 10.0
        self.INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS = 7.0
        self.INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY = 5
        self.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY = 5
        self.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED = False
        self.INBOUND_LIQUIDITY_DEV_FEE_PCT = 0.1
        self.INBOUND_LIQUIDITY_DEV_FEE_ADDRESS = "electrum_liqhelper@getbarebits.com"


class _FakeStatusBar:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def showMessage(self, msg, timeout=0) -> None:
        self.messages.append(msg)


class _FakeWindow:
    def __init__(self) -> None:
        self.tabs = QTabWidget()
        self._sb = _FakeStatusBar()

    def statusBar(self) -> _FakeStatusBar:
        return self._sb


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_plugin() -> qt_mod.Plugin:
    p = object.__new__(qt_mod.Plugin)
    p.config = _FakeConfig()
    p.logger = logging.getLogger("test.inbound_liquidity.qt")
    p.signals = None
    p._tabs = {}
    p._last_offers = {}
    return p


def _seed_log(wallet: _FakeWallet, entries: List[Dict]) -> None:
    wallet.db.put(LOG_DB_KEY, entries)


def _tab_titles(window: _FakeWindow) -> List[str]:
    return [window.tabs.tabText(i) for i in range(window.tabs.count())]


# --- tests ---------------------------------------------------------------
def test_add_tab_inserts_liquidity_tab(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert "Liquidity" in _tab_titles(window)
    assert wallet in p._tabs
    # The container holds the three sub-tabs.
    state = p._tabs[wallet]
    sub = state.container.findChild(QTabWidget)
    assert [sub.tabText(i) for i in range(sub.count())] == [
        "Settings", "Swap providers", "Channel partners", "Advanced",
        "Actions", "Declines", "Faults"]


def test_add_tab_is_idempotent(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    p._add_liquidity_tab(window, wallet)
    assert _tab_titles(window).count("Liquidity") == 1


def test_remove_tab(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    p._remove_liquidity_tab(wallet)
    assert "Liquidity" not in _tab_titles(window)
    assert wallet not in p._tabs


def test_log_populates_trees(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    _seed_log(wallet, [
        {"ts": now, "category": "action", "kind": "swap", "amount_sat": 50_000,
         "source": "117x1x0", "dest": "on-chain", "reason": "drain"},
        {"ts": now, "category": "decline", "kind": "swap", "amount_sat": 0,
         "reason": "too expensive"},
        {"ts": now, "category": "action", "kind": "open", "amount_sat": 1_000,
         "source": "on-chain", "dest": "02ab..", "reason": "new capacity"},
    ])
    p._add_liquidity_tab(window, wallet)  # refresh() runs on add
    state = p._tabs[wallet]
    assert state.actions_tree.topLevelItemCount() == 2
    assert state.declines_tree.topLevelItemCount() == 1


def test_on_log_changed_refreshes(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    assert state.actions_tree.topLevelItemCount() == 0
    _seed_log(wallet, [
        {"ts": time.time(), "category": "action", "kind": "swap",
         "amount_sat": 1, "reason": "x"},
    ])
    p._on_log_changed_ui(wallet)
    assert state.actions_tree.topLevelItemCount() == 1
    # An unknown wallet is a no-op (no crash).
    p._on_log_changed_ui(_FakeWallet())


def test_show_activity_uses_window_statusbar(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    p._show_activity(wallet, "opened channel")
    assert any("opened channel" in m for m in window._sb.messages)
    # Unknown wallet: no-op.
    p._show_activity(_FakeWallet(), "nope")


def test_apply_persists_and_clamps(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    sub = state.container.findChild(QTabWidget)
    settings_tab = sub.widget(0)
    from PyQt6.QtWidgets import QLineEdit, QPushButton
    line_edits = settings_tab.findChildren(QLineEdit)
    apply_btn = next(b for b in settings_tab.findChildren(QPushButton)
                     if b.text() == "Apply")

    # Settings-tab QLineEdit order after the Advanced-tab reorg: 0 min-onchain,
    # 1 max-channels, 2 max-cost, 3 trigger-%, 4 trigger-sat, 5 dev-fee-%.
    # (Automation on/off is the slider, applied immediately and independently of
    # this Apply button. Reserve / log-retention / tuning knobs live on Advanced.)
    assert len(line_edits) == 6
    line_edits[1].setText("5")              # Maximum number of channels
    line_edits[5].setText("99")             # dev fee % -> clamped to DEV_FEE_MAX_PCT
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNELS == 5
    from electrum.plugins.inbound_liquidity import DEV_FEE_MAX_PCT  # noqa
    assert p.config.INBOUND_LIQUIDITY_DEV_FEE_PCT == DEV_FEE_MAX_PCT
    # Field reloaded to the clamped value.
    assert line_edits[5].text() == str(DEV_FEE_MAX_PCT)


def test_apply_rejects_invalid_without_persisting(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    sub = state.container.findChild(QTabWidget)
    settings_tab = sub.widget(0)
    from PyQt6.QtWidgets import QLineEdit, QPushButton
    line_edits = settings_tab.findChildren(QLineEdit)
    before = p.config.INBOUND_LIQUIDITY_MAX_CHANNELS
    line_edits[1].setText("not-a-number")   # max-channels is index 1 post-reorg
    apply_btn = next(b for b in settings_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()
    # Nothing persisted.
    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNELS == before


# --- ENABLED/DISABLED slider ---------------------------------------------
def _settings_tab(p, wallet):
    return p._tabs[wallet].container.findChild(QTabWidget).widget(0)


def _slider(p, wallet):
    from electrum.plugins.inbound_liquidity.qt_widgets import ToggleSwitch  # type: ignore
    return _settings_tab(p, wallet).findChild(ToggleSwitch)


def _has_label(widget, text: str) -> bool:
    from PyQt6.QtWidgets import QLabel
    return any(lbl.text() == text for lbl in widget.findChildren(QLabel))


def test_slider_reflects_default_disabled(qapp):
    # _FakeConfig defaults automation off, matching the shipped default; the
    # slider and its status label must show DISABLED, and no automation runs.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is False
    p._add_liquidity_tab(window, wallet)
    sw = _slider(p, wallet)
    assert sw is not None
    assert sw.isChecked() is False
    assert _has_label(_settings_tab(p, wallet), "DISABLED")


def test_slider_enables_immediately_and_triggers_eval(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    evaluated: List = []
    p.request_evaluation = lambda w: evaluated.append(w)  # type: ignore[assignment]
    p._add_liquidity_tab(window, wallet)
    sw = _slider(p, wallet)

    # Flipping the slider persists at once, without any Apply click.
    sw.setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is True
    # ...and kicks an evaluation so it starts acting right away.
    assert evaluated == [wallet]
    assert _has_label(_settings_tab(p, wallet), "ENABLED")


def test_slider_disables_immediately_without_eval(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED = True
    evaluated: List = []
    p.request_evaluation = lambda w: evaluated.append(w)  # type: ignore[assignment]
    p._add_liquidity_tab(window, wallet)
    sw = _slider(p, wallet)
    assert sw.isChecked() is True  # reflects the enabled config on build

    sw.setChecked(False)
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is False
    # Disabling does not schedule new work.
    assert evaluated == []
    assert _has_label(_settings_tab(p, wallet), "DISABLED")


def test_apply_does_not_disturb_slider_state(qapp):
    # The Apply button (other fields) must leave automation on/off untouched.
    from PyQt6.QtWidgets import QLineEdit, QPushButton
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.request_evaluation = lambda w: None  # type: ignore[assignment]
    p._add_liquidity_tab(window, wallet)
    sw = _slider(p, wallet)
    sw.setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is True

    settings_tab = _settings_tab(p, wallet)
    settings_tab.findChildren(QLineEdit)[1].setText("4")   # max-channels index 1
    next(b for b in settings_tab.findChildren(QPushButton)
         if b.text() == "Apply").click()

    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNELS == 4
    # Slider (and its config) unchanged by Apply.
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is True
    assert sw.isChecked() is True


# --- "Manual run only" checkbox + "Run now" button -----------------------
def _manual_only_cb(p, wallet):
    from PyQt6.QtWidgets import QCheckBox
    return next(
        cb for cb in _settings_tab(p, wallet).findChildren(QCheckBox)
        if "Manual run only" in cb.text())


def _run_now_btn(p, wallet):
    from PyQt6.QtWidgets import QPushButton
    return next(
        b for b in _settings_tab(p, wallet).findChildren(QPushButton)
        if b.text() == "Run now")


def test_manual_run_only_checkbox_present_and_reflects_default(qapp):
    # The Settings tab (next to the master switch) carries the "Manual run only"
    # checkbox and a "Run now" button; the checkbox mirrors the shipped default.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    assert p.config.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY is False
    p._add_liquidity_tab(window, wallet)
    cb = _manual_only_cb(p, wallet)
    assert cb.isChecked() is False
    assert _run_now_btn(p, wallet) is not None


def test_manual_run_only_checkbox_persists_immediately(qapp):
    # Like the master switch, flipping the checkbox persists at once (no Apply).
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    cb = _manual_only_cb(p, wallet)

    cb.setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY is True
    cb.setChecked(False)
    assert p.config.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY is False


def test_manual_run_only_checkbox_reflects_config_on_build(qapp):
    # Built against an already-on config, the checkbox shows checked.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY = True
    p._add_liquidity_tab(window, wallet)
    assert _manual_only_cb(p, wallet).isChecked() is True


def test_run_now_button_triggers_manual_evaluation(qapp):
    # "Run now" calls request_evaluation with manual=True (bypassing the guard),
    # even while manual-run-only is on.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY = True
    seen: List = []
    p.request_evaluation = lambda w, *, manual=False: seen.append((w, manual))  # type: ignore[assignment]
    p._add_liquidity_tab(window, wallet)

    _run_now_btn(p, wallet).click()
    assert seen == [(wallet, True)]


# --- Providers sub-tab ---------------------------------------------------
def _seed_offers(p: qt_mod.Plugin, wallet: _FakeWallet) -> None:
    from electrum.plugins.inbound_liquidity.liquidity_manager import ProviderOffer  # type: ignore
    p._last_offers[wallet] = [
        ProviderOffer(npub="npubAAA", percentage_fee=0.5, mining_fee_sat=1000,
                      min_amount_sat=20_000, max_reverse_sat=1_900_000, pow_bits=20),
        ProviderOffer(npub="npubBBB", percentage_fee=0.2, mining_fee_sat=0,
                      min_amount_sat=20_000, max_reverse_sat=1_900_000, pow_bits=10),
    ]


def test_providers_tab_lists_discovered(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    _seed_offers(p, wallet)
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    sub = state.container.findChild(QTabWidget)
    assert sub.tabText(1) == "Swap providers"
    from PyQt6.QtWidgets import QTreeWidget
    tree = sub.widget(1).findChild(QTreeWidget)
    assert tree.topLevelItemCount() == 2


def test_providers_tab_apply_persists_checked_and_manual(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QPlainTextEdit, QPushButton, QTreeWidget
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    _seed_offers(p, wallet)
    p._add_liquidity_tab(window, wallet)
    providers_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(1)
    tree = providers_tab.findChild(QTreeWidget)

    # Ban the first discovered provider (npubAAA) via its checkbox.
    row0 = tree.topLevelItem(0)
    assert row0.data(0, Qt.ItemDataRole.UserRole) == "npubAAA"
    row0.setCheckState(p._BAN_COL, Qt.CheckState.Checked)
    # Prefer an offline provider by typing it into the manual box.
    pref_box = providers_tab.findChildren(QPlainTextEdit)[0]
    pref_box.setPlainText("npubOFFLINE")

    apply_btn = next(b for b in providers_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_BANNED_NPUBS == "npubAAA"
    assert p.config.INBOUND_LIQUIDITY_PREFERRED_NPUBS == "npubOFFLINE"


# --- Channel partners sub-tab --------------------------------------------
def test_channel_partners_tab_present_after_providers(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    assert sub.tabText(2) == "Channel partners"


def test_channel_partners_apply_persists_text_and_strict(qapp):
    from PyQt6.QtWidgets import QCheckBox, QPlainTextEdit, QPushButton
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    partners_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(2)

    pub_a = "02" + "aa" * 32
    pub_b = "03" + "bb" * 32
    pref_box, ban_box = partners_tab.findChildren(QPlainTextEdit)[:2]
    pref_box.setPlainText(f"{pub_a}@127.0.0.1:9735")
    ban_box.setPlainText(pub_b)
    # Two checkboxes in this tab, in layout order: strict, then one-per-peer.
    strict_cb, one_per_peer_cb = partners_tab.findChildren(QCheckBox)[:2]
    strict_cb.setChecked(True)
    one_per_peer_cb.setChecked(False)

    apply_btn = next(b for b in partners_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS == f"{pub_a}@127.0.0.1:9735"
    assert p.config.INBOUND_LIQUIDITY_BANNED_PARTNERS == pub_b
    assert p.config.INBOUND_LIQUIDITY_PARTNERS_STRICT is True
    assert p.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER is False


# --- Advanced sub-tab ----------------------------------------------------
def test_advanced_tab_present_after_partners(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    assert sub.tabText(3) == "Advanced"


def test_advanced_tab_persists_daily_ceilings(qapp):
    from PyQt6.QtWidgets import QLineEdit, QPushButton
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)

    opens_edit, closes_edit = advanced_tab.findChildren(QLineEdit)[:2]
    opens_edit.setText("7")
    closes_edit.setText("0")   # 0 = unlimited
    apply_btn = next(b for b in advanced_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY == 7
    assert p.config.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY == 0


def test_advanced_tab_rejects_invalid_without_persisting(qapp):
    from PyQt6.QtWidgets import QLineEdit, QPushButton
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)

    opens_edit, closes_edit = advanced_tab.findChildren(QLineEdit)[:2]
    opens_edit.setText("-3")   # negative rejected
    closes_edit.setText("abc")  # non-int rejected
    apply_btn = next(b for b in advanced_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()

    # Neither invalid value is written; defaults are untouched.
    assert p.config.INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY == 5
    assert p.config.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY == 5


def test_settings_tab_omits_moved_and_removed_fields(qapp):
    """Reserve and log-retention moved to Advanced; the dev-fee payout address
    field is removed entirely (the address is no longer user-editable). The cost
    field is renamed to 'Max fee to move LN -> on-chain'."""
    from PyQt6.QtWidgets import QLabel
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    settings_tab = _settings_tab(p, wallet)
    labels = [lbl.text() for lbl in settings_tab.findChildren(QLabel)]
    assert not any("payout address" in t for t in labels)
    assert not any(t.startswith("On-chain reserve") for t in labels)
    assert not any(t.startswith("Keep decision log") for t in labels)
    assert any("Max fee to move LN" in t for t in labels)
    # The removed address field must not have disturbed the persisted default.
    assert p.config.INBOUND_LIQUIDITY_DEV_FEE_ADDRESS == "electrum_liqhelper@getbarebits.com"


def test_advanced_tab_persists_toggles_reserve_and_clamps(qapp):
    from PyQt6.QtWidgets import QCheckBox, QLineEdit, QPushButton
    from electrum.plugins.inbound_liquidity import MAX_LOG_RETENTION_DAYS
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    edits = advanced_tab.findChildren(QLineEdit)
    # Advanced QLineEdit order: 0 opens/day, 1 closes/day, 2 on-chain reserve,
    # 3 log retention, then the reliability/offline tuning knobs.
    edits[2].setText("7500")                 # on-chain reserve (moved from Settings)
    edits[3].setText("99999")                # log retention -> clamped
    for cb in advanced_tab.findChildren(QCheckBox):
        cb.setChecked(False)                 # flip every feature toggle off
    apply_btn = next(b for b in advanced_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT == 7500
    assert p.config.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS == MAX_LOG_RETENTION_DAYS
    assert edits[3].text() == str(MAX_LOG_RETENTION_DAYS)
    assert p.config.INBOUND_LIQUIDITY_RELIABILITY_ENABLED is False
    assert p.config.INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED is False
    assert p.config.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED is False


# --- description text wrapping -------------------------------------------
def _has_wrapped_paragraph(widget) -> bool:
    """True if the sub-tab carries a multi-word description QLabel whose text
    wraps to the panel width (rather than being clipped at the right edge)."""
    from PyQt6.QtWidgets import QLabel
    return any(
        lbl.wordWrap() and len(lbl.text().split()) > 6
        for lbl in widget.findChildren(QLabel)
    )


def test_settings_sub_tabs_have_wrapping_descriptions(qapp):
    # Each settings sub-tab opens with a paragraph of guidance; without word
    # wrap that text is cut off at the right edge. Guard every such tab.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    sub = p._tabs[wallet].container.findChild(QTabWidget)
    for idx in range(4):  # Settings, Swap providers, Channel partners, Advanced
        assert _has_wrapped_paragraph(sub.widget(idx)), \
            f"sub-tab {sub.tabText(idx)!r} has no word-wrapped description label"
