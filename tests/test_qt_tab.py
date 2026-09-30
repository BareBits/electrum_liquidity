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

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QFrame, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTabWidget,
)

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LOG_DB_KEY,
    DEFAULT_LOG_BUFFER_LINES,
    DEFAULT_LOG_RETENTION_DAYS,
    STATUS_NOT_STARTED,
    STATUS_SLEEPING,
)
from electrum.plugins.inbound_liquidity.log_buffer import (  # type: ignore  # noqa: E402
    LogCapture,
    LogLine,
    LogRingBuffer,
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
        # Mirrors the shipped ConfigVar default (10x the shipped liquidity goal).
        self.INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT = 1_000_000
        self.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT = 0.6
        self.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT = 25.0
        self.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT = 25_000
        self.INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT = 0
        self.INBOUND_LIQUIDITY_MIN_SWAP_SAT = 25_000
        self.INBOUND_LIQUIDITY_GOAL_SAT = 100_000
        # Mirrors the shipped ConfigVar default (on: never touch a channel the
        # user opened by hand unless they say so).
        self.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY = True
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
        self.INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN = 360
        self.INBOUND_LIQUIDITY_CONFIRMING_FREEZE_DAYS = 3.0
        self.INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS = 5
        self.INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN = 60
        self.INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN = 10
        self.INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN = 180
        self.INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN = True
        self.INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED = True
        self.INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS = 2.0
        self.INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT = 10.0
        self.INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS = 7.0
        self.INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY = 5
        self.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY = 5
        self.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED = False
        self.INBOUND_LIQUIDITY_LOG_BUFFER_LINES = DEFAULT_LOG_BUFFER_LINES
        self.INBOUND_LIQUIDITY_LOG_CAPTURE_LN = False
        self.INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG = False
        self.INBOUND_LIQUIDITY_DEV_FEE_PCT = 0.1
        self.INBOUND_LIQUIDITY_DEV_FEE_ADDRESS = "electrum_liqhelper@getbarebits.com"
        self.INBOUND_LIQUIDITY_SINK_ADDRESS = ""
        self.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS = False
        # Mirrors the shipped ConfigVar default (on: the sink waits until the
        # channel build-out has reached the liquidity goal).
        self.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL = True
        self.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = False
        self.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED = False
        self.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS = 0.0
        self.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = ""
        self.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = ""


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

    def get_decimal_point(self) -> int:
        """Base unit the amount widgets render in, as ElectrumWindow exposes it.
        Default 8 = BTC, matching Electrum's own default; tests that care set it."""
        return getattr(self, "decimal_point", 8)


def _plain_line_edits(widget):
    """The tab's plain QLineEdit fields, in layout order.

    Filtered by EXACT type on purpose: Electrum's BTCAmountEdit/AmountEdit are
    QLineEdit subclasses, so a bare findChildren(QLineEdit) also picks up the
    liquidity-goal amount widget (and any future one) and silently shifts the
    positional indices these tests address fields by."""
    from PyQt6.QtWidgets import QLineEdit
    return [w for w in widget.findChildren(QLineEdit) if type(w) is QLineEdit]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _silence_log_tab_timer(monkeypatch):
    """Disconnect the Log tab's refresh timer on every tab any test builds.

    The Log tab owns a QTimer parented to it, and nothing ever stops it: a tab is
    simply abandoned when a test ends, and `_remove_liquidity_tab` (where it is
    called at all) only takes the widget out of its QTabWidget. Those timers go
    on firing into widgets Python is collecting, which raises out of the QT EVENT
    LOOP -- where pytest-qt blames whichever test is running, never the one that
    leaked it.

    It was invisible for a long time because the leaked timers had nothing left
    to fire during: the suite ended. Appending tests to this module gave them
    somewhere to land, and the suite began failing about one run in three, in a
    different innocent test each time.

    Three things this had to get right, each learned by getting it wrong:
      * Disconnect at BUILD time, not in fixture teardown. pytest-qt drains the
        event loop inside `pytest_runtest_call`, which runs before any fixture
        teardown -- so by then the queued timeout has already fired.
      * DISCONNECT, not just stop(). stop() prevents future timeouts but does not
        withdraw one already queued.
      * Silence the timer; do not delete the widgets. deleteLater()/sip.delete()
        removes the flake and replaces it with an intermittent SEGFAULT, because
        a timer firing mid-deletion reaches genuinely freed C++ objects rather
        than ones PyQt can still report on.

    Nothing here depends on the periodic refresh -- the tests that exercise the
    Log tab's rendering drive it explicitly -- so removing the timer costs no
    coverage. The leak itself is the plugin's (`_remove_liquidity_tab` stops no
    timers), but fixing it at the source belongs in a change about the Log tab,
    not one about the liquidity sink.
    """
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
                    pass            # nothing was connected
                timer.stop()
        return result

    monkeypatch.setattr(qt_mod.Plugin, "_add_liquidity_tab", _add_and_silence)
    yield


def _make_plugin() -> qt_mod.Plugin:
    p = object.__new__(qt_mod.Plugin)
    p.config = _FakeConfig()
    p.logger = logging.getLogger("test.inbound_liquidity.qt")
    p.signals = None
    p._tabs = {}
    p._last_offers = {}
    p._tick_status = {}
    p.wallets = {}
    p._send_warning_refreshers = {}
    # The Log tab reads both of these; a fresh buffer per plugin keeps tests
    # isolated, and the capture is left detached (no global handler installed).
    p.log_buffer = LogRingBuffer()
    p.log_capture = LogCapture(p.log_buffer, root_logger_name="test.inbound_liquidity.qt")
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
        "Actions", "Declines", "Faults", "Log"]


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


def test_plugin_never_writes_to_electrums_status_bar(qapp):
    """The plugin owns the Liquidity tab, not the space next to your balance.

    It used to push transient notices into Electrum's main status bar. Those are
    gone: completed actions live in the Actions / Log sub-tabs, and what a tick
    is currently doing lives in the Settings tab's Status footer. This pins that
    — including that `on_action_done` (still called from the base plugin when an
    open or swap completes) resolves to the inherited no-op.
    """
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    assert not hasattr(p, "_show_activity")
    assert not hasattr(qt_mod._Signals, "activity")
    # The base class's hook is what the Qt plugin inherits, and it is a no-op.
    p.on_action_done(wallet, "Opened channel: 1,000,000 sat")
    p._on_status_changed_ui(wallet, "connecting to partner 02aaaa…aaaa (1 of 2)")
    p._on_log_changed_ui(wallet)
    assert window._sb.messages == []


def test_apply_persists_and_clamps(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    sub = state.container.findChild(QTabWidget)
    settings_tab = sub.widget(0)
    from PyQt6.QtWidgets import QPushButton
    line_edits = _plain_line_edits(settings_tab)
    apply_btn = next(b for b in settings_tab.findChildren(QPushButton)
                     if b.text() == "Apply")

    # Settings-tab QLineEdit order after the Advanced-tab reorg: 0 min-onchain,
    # 1 max-channels, 2 max-channel-size, 3 max-cost, 4 trigger-%, 5 trigger-sat,
    # 6 dev-fee-%, 7 liquidity-sink address.
    # (Automation on/off is the slider, applied immediately and independently of
    # this Apply button. Reserve / log-retention / tuning knobs live on Advanced.)
    assert len(line_edits) == 8
    line_edits[1].setText("5")              # Maximum number of channels
    line_edits[6].setText("99")             # dev fee % -> clamped to DEV_FEE_MAX_PCT
    line_edits[7].setText("  alice@example.com  ")   # sink -> stored trimmed
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_MAX_CHANNELS == 5
    from electrum.plugins.inbound_liquidity import DEV_FEE_MAX_PCT  # noqa
    assert p.config.INBOUND_LIQUIDITY_DEV_FEE_PCT == DEV_FEE_MAX_PCT
    assert p.config.INBOUND_LIQUIDITY_SINK_ADDRESS == "alice@example.com"
    # Fields reloaded to the clamped / normalised values.
    assert line_edits[6].text() == str(DEV_FEE_MAX_PCT)
    assert line_edits[7].text() == "alice@example.com"


def test_apply_rejects_invalid_without_persisting(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    sub = state.container.findChild(QTabWidget)
    settings_tab = sub.widget(0)
    from PyQt6.QtWidgets import QPushButton
    line_edits = _plain_line_edits(settings_tab)
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


def test_slider_knob_matches_enabled_config_on_build(qapp):
    # Regression: when the tab is built with automation already enabled, the
    # knob must be parked ON to match the green ENABLED label -- not lag behind
    # in the off position. The config is read into the switch through a
    # signal-blocked setChecked() (to avoid re-firing the toggle handler), which
    # mutes the slide animation; the tab must snap the knob so the two agree on
    # the very first paint. Asserting on `offset` (the knob position), not just
    # isChecked(), is what catches the desync.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED = True
    p.request_evaluation = lambda w: None  # type: ignore[assignment]
    p._add_liquidity_tab(window, wallet)  # refresh() -> _sync_toggle_from_config
    sw = _slider(p, wallet)
    assert sw.isChecked() is True
    assert sw.offset == 1.0   # knob ON, matching the ENABLED label
    assert _has_label(_settings_tab(p, wallet), "ENABLED")


def test_slider_knob_matches_disabled_config_on_build(qapp):
    # The shipped default: automation off -> knob parked OFF, DISABLED label.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    assert p.config.INBOUND_LIQUIDITY_AUTOMATION_ENABLED is False
    p._add_liquidity_tab(window, wallet)
    sw = _slider(p, wallet)
    assert sw.isChecked() is False
    assert sw.offset == 0.0   # knob OFF, matching the DISABLED label
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


# --- "Only manage channels the plugin opened" scope switch ----------------
# Moved to the Settings tab (it answers "will this touch the channel I opened
# myself?", which is a main-tab question, not an Advanced one) and now defaults
# ON, so an untouched install never drains a hand-opened channel.
def _plugin_only_cb(p, wallet):
    from PyQt6.QtWidgets import QCheckBox
    return next(
        cb for cb in _settings_tab(p, wallet).findChildren(QCheckBox)
        if "Only manage channels the plugin opened" in cb.text())


def test_scope_switch_ships_on_by_default(qapp):
    # The shipped ConfigVar default, not just the test fake.
    from electrum.simple_config import SimpleConfig
    assert SimpleConfig.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY.get_default_value() is True


def test_scope_switch_is_on_settings_tab_and_checked_by_default(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert _plugin_only_cb(p, wallet).isChecked() is True


def test_scope_switch_no_longer_on_the_advanced_tab(qapp):
    from PyQt6.QtWidgets import QCheckBox
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    texts = [cb.text() for cb in advanced_tab.findChildren(QCheckBox)]
    assert not any("plugin opened" in t for t in texts), texts
    # ...and the old wording is gone from the UI entirely.
    settings_texts = [cb.text() for cb in
                      _settings_tab(p, wallet).findChildren(QCheckBox)]
    assert not any("Only drain" in t for t in texts + settings_texts)


def test_scope_switch_persists_immediately(qapp):
    # Like the master switch and manual-run-only: no Apply needed.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    cb = _plugin_only_cb(p, wallet)

    cb.setChecked(False)
    assert p.config.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY is False
    cb.setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY is True


def test_scope_switch_reflects_config_on_build(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY = False
    p._add_liquidity_tab(window, wallet)
    assert _plugin_only_cb(p, wallet).isChecked() is False


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


def _edit_for(tab, label_prefix: str):
    """The QLineEdit sitting beside the label that starts with ``label_prefix``.

    By label rather than by position: these grids are a running list of tuning
    knobs, and indexing into them positionally means every new setting silently
    re-points an existing assertion at the wrong field.
    """
    from PyQt6.QtWidgets import QGridLayout, QLabel, QLineEdit
    for grid in tab.findChildren(QGridLayout):
        for i in range(grid.count()):
            item = grid.itemAt(i)
            widget = item.widget() if item is not None else None
            if not isinstance(widget, QLabel):
                continue
            if not widget.text().startswith(label_prefix):
                continue
            row, _col, _rs, _cs = grid.getItemPosition(i)
            beside = grid.itemAtPosition(row, 1)
            if beside is not None and isinstance(beside.widget(), QLineEdit):
                return beside.widget()
    raise AssertionError(f"no line edit found beside a label starting "
                         f"{label_prefix!r}")


def test_advanced_tab_persists_toggles_reserve_and_clamps(qapp):
    from PyQt6.QtWidgets import QCheckBox, QLineEdit, QPushButton
    from electrum.plugins.inbound_liquidity import MAX_LOG_RETENTION_DAYS
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    reserve = _edit_for(advanced_tab, "On-chain reserve")
    retention = _edit_for(advanced_tab, "Keep decision log")
    reserve.setText("7500")
    retention.setText("99999")               # -> clamped
    for cb in advanced_tab.findChildren(QCheckBox):
        cb.setChecked(False)                 # flip every feature toggle off
    apply_btn = next(b for b in advanced_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    apply_btn.click()

    assert p.config.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT == 7500
    assert p.config.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS == MAX_LOG_RETENTION_DAYS
    assert retention.text() == str(MAX_LOG_RETENTION_DAYS)
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


# --- Log sub-tab ----------------------------------------------------------
# The Log tab is the merged view: this wallet's decision log plus whatever the
# plugin's own logging captured. These pin the merge, the filters, and the
# blast radius of "Clear".
def _log_view(state) -> QPlainTextEdit:
    return state.log_tab.findChild(QPlainTextEdit)


def _log_text(state) -> str:
    return _log_view(state).toPlainText()


def _level_combo(state) -> QComboBox:
    return state.log_tab.findChild(QComboBox)


def _filter_edit(state) -> QLineEdit:
    # Matched by placeholder, not position: the Log tab has a second QLineEdit
    # (the capture buffer size) and findChild() order must not decide this.
    return next(e for e in state.log_tab.findChildren(QLineEdit)
                if "substring" in e.placeholderText())


def _capture(p, level: int, message: str, ts: float, source: str = "plugin") -> None:
    p.log_buffer.append(LogLine(ts=ts, level=level,
                                level_name=logging.getLevelName(level),
                                source=source, message=message))


def test_log_tab_merges_capture_and_decisions_in_time_order(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    _seed_log(wallet, [
        {"ts": now + 1, "category": "decline", "kind": "open",
         "reason": "no reachable channel partner",
         "detail": "partner resolution: preferred 0/0, suggested 0/0"},
    ])
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    _capture(p, logging.INFO, "resolving channel partners", ts=now)
    _capture(p, logging.INFO, "sleeping", ts=now + 2)
    state.refresh_log()

    text = _log_text(state)
    assert "resolving channel partners" in text
    assert "no reachable channel partner" in text
    assert "partner resolution: preferred 0/0" in text     # the decline's detail
    # Chronological across both halves, not one list appended to the other.
    assert (text.index("resolving channel partners")
            < text.index("no reachable channel partner")
            < text.index("sleeping"))


def test_log_tab_level_filter_applies_to_both_halves(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    _seed_log(wallet, [
        # Actions/declines are informational; a fault is a warning by nature.
        {"ts": now, "category": "decline", "kind": "open", "reason": "cost too high"},
        {"ts": now, "category": "fault", "kind": "peer", "reason": "force-closed on us"},
    ])
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    _capture(p, logging.DEBUG, "chatty detail", ts=now)
    _capture(p, logging.ERROR, "evaluation failed", ts=now)
    state.refresh_log()
    assert "chatty detail" in _log_text(state)              # default: everything

    combo = _level_combo(state)
    combo.setCurrentIndex(1)                                # Warning and above
    text = _log_text(state)
    assert "chatty detail" not in text
    assert "cost too high" not in text                      # decline == INFO
    assert "force-closed on us" in text                     # fault == WARNING
    assert "evaluation failed" in text


def test_log_tab_text_filter_narrows_the_view(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    _capture(p, logging.INFO, "connecting to partner 02aaaa…aaaa", ts=now)
    _capture(p, logging.INFO, "discovering swap providers", ts=now + 1)
    state.refresh_log()

    _filter_edit(state).setText("partner")
    text = _log_text(state)
    assert "connecting to partner" in text
    assert "discovering swap providers" not in text
    # Case-insensitive, and clearing the box restores everything.
    _filter_edit(state).setText("PARTNER")
    assert "connecting to partner" in _log_text(state)
    _filter_edit(state).setText("")
    assert "discovering swap providers" in _log_text(state)


def test_log_tab_renders_a_multiline_record_as_one_indented_event(qapp):
    # A traceback keeps its shape, but only its FIRST line starts at column 0 —
    # so text smuggled into a log message can never look like a separate row.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    _capture(p, logging.ERROR, "failed\nTraceback\n  ValueError: boom", ts=time.time())
    state.refresh_log()

    lines = _log_text(state).splitlines()
    assert len(lines) == 3
    assert not lines[0].startswith(" ")
    assert lines[1].startswith(" ") and lines[2].startswith(" ")


def _click_clear(state) -> None:
    for button in state.log_tab.findChildren(QPushButton):
        if button.text() == "Clear":
            button.click()
            return
    pytest.fail("no Clear button on the Log tab")           # pragma: no cover


def test_log_tab_clear_empties_the_whole_view(qapp):
    """"Clear" means the view is empty -- BOTH halves.

    It used to clear only the captured ring. The decision half is re-read from
    wallet.db on every refresh, so it cannot be cleared by forgetting anything and
    every DECISION row stayed on screen; with Electrum's Lightning capture on, the
    ring also refilled within one 500ms refresh. Between them the button looked
    broken, which is exactly how it was reported.
    """
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    _seed_log(wallet, [
        {"ts": now, "category": "action", "kind": "open", "reason": "new capacity"},
    ])
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    _capture(p, logging.INFO, "captured line", ts=now)
    state.refresh_log()
    assert "captured line" in _log_text(state)
    assert "new capacity" in _log_text(state)

    _click_clear(state)

    assert _log_text(state) == ""
    assert len(p.log_buffer.snapshot()) == 0


def test_log_tab_clear_deletes_nothing_from_the_wallet(qapp):
    """The decision log is only HIDDEN. It is the plugin's audit trail, so "clear
    the view" must not be a way to destroy it -- it stays in wallet.db, stays in
    the Actions / Declines / Faults views, and comes back here when the tab is
    rebuilt (reopening the wallet)."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    _seed_log(wallet, [
        {"ts": now, "category": "action", "kind": "open", "reason": "new capacity"},
    ])
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    state.refresh_log()
    _click_clear(state)
    assert _log_text(state) == ""

    # Still on disk, and still visible everywhere else.
    assert len(p.get_decision_log(wallet)) == 1
    assert p.get_decision_log(wallet)[0]["reason"] == "new capacity"
    # A freshly built tab has no watermark, so the entry is back.
    p._tabs.clear()
    p._add_liquidity_tab(window, wallet)
    rebuilt = p._tabs[wallet]
    rebuilt.refresh_log()
    assert "new capacity" in _log_text(rebuilt)


def test_log_tab_clear_says_how_much_it_hid(qapp):
    """A view that silently empties reads as data loss. The summary names the
    hidden count and says the entries are kept."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    _seed_log(wallet, [
        {"ts": now, "category": "action", "kind": "open", "reason": "first"},
        {"ts": now, "category": "decline", "kind": "swap", "reason": "second"},
    ])
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    state.refresh_log()
    _click_clear(state)

    labels = [w.text() for w in state.log_tab.findChildren(QLabel)]
    summary = next(t for t in labels if "lines shown" in t)
    assert "2 earlier decision entries hidden by Clear (still kept)" in summary


def test_log_tab_clear_still_shows_what_happens_next(qapp):
    """Clear is not a mute button. It hides what came before; anything logged after
    it must appear, or the tab would look dead after one click."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    now = time.time()
    ancient = {"ts": now, "category": "action", "kind": "open",
               "reason": "ancient-entry"}
    _seed_log(wallet, [ancient])
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    _capture(p, logging.INFO, "before-clear-line", ts=now)
    state.refresh_log()
    _click_clear(state)
    assert _log_text(state) == ""

    # Both halves resume. The pre-Clear decision entry is kept in the db alongside
    # the new one, so it is the watermark -- not a rewrite -- hiding it.
    later = now + 5
    _capture(p, logging.INFO, "after-clear-line", ts=later)
    _seed_log(wallet, [ancient,
                       {"ts": later, "category": "action", "kind": "swap",
                        "reason": "brand-new-entry"}])
    state.refresh_log()
    text = _log_text(state)
    assert "after-clear-line" in text
    assert "brand-new-entry" in text
    assert "before-clear-line" not in text
    assert "ancient-entry" not in text
    assert len(p.get_decision_log(wallet)) == 2          # both still on disk


def test_log_tab_summary_reports_the_ring_state(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.log_buffer = LogRingBuffer(100)
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    for i in range(120):
        _capture(p, logging.INFO, f"line {i}", ts=time.time())
    state.refresh_log()

    labels = [w.text() for w in state.log_tab.findChildren(QLabel)]
    summary = next(t for t in labels if "lines shown" in t)
    assert "100 captured" in summary and "20 dropped" in summary


# --- Status section -------------------------------------------------------
def _status_labels(state) -> List[str]:
    settings = state.container.findChild(QTabWidget).widget(0)
    return [w.text() for w in settings.findChildren(QLabel)]


def test_status_section_starts_on_a_terminal_state(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert "Status" in _status_labels(p._tabs[wallet])
    assert STATUS_NOT_STARTED in _status_labels(p._tabs[wallet])


def test_status_section_updates_from_the_plugin_signal(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    p._on_status_changed_ui(wallet, "opening channel with 02aaaa…aaaa (1,000,000 sat)")
    assert "opening channel with 02aaaa…aaaa (1,000,000 sat)" in _status_labels(p._tabs[wallet])
    # An in-flight step also shows when it started; a terminal one does not.
    assert any(t.startswith("since ") for t in _status_labels(p._tabs[wallet]))

    p._on_status_changed_ui(wallet, STATUS_SLEEPING)
    labels = _status_labels(p._tabs[wallet])
    assert STATUS_SLEEPING in labels
    assert not any(t.startswith("since ") for t in labels)


def test_the_annotated_sleeping_status_still_reads_as_resting(qapp):
    """The status the plugin actually publishes once a tick has run carries the
    rate limit's "(next check in ~Xm)" suffix. The tab decides "resting" by
    prefix for exactly this reason -- an equality check against the terminal set
    would paint a deliberately-waiting plugin as busy, in bold green and stamped
    with a "since" time that never advances."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    annotated = f"{STATUS_SLEEPING} (next check in ~10m)"
    p._on_status_changed_ui(wallet, annotated)
    labels = _status_labels(p._tabs[wallet])
    assert annotated in labels, "the wait must be shown, not swallowed"
    assert not any(t.startswith("since ") for t in labels), \
        "a resting plugin must not be stamped with a start time"


def test_status_update_for_an_unknown_wallet_is_a_no_op(qapp):
    p = _make_plugin()
    p._on_status_changed_ui(_FakeWallet(), STATUS_SLEEPING)      # must not raise


def test_status_update_after_tab_teardown_is_a_no_op(qapp):
    # close_wallet can race a status already queued from the asyncio thread;
    # delivering it must not touch deleted C++ objects.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    p._remove_liquidity_tab(wallet)
    p._on_status_changed_ui(wallet, STATUS_SLEEPING)             # wallet is gone
    # Even a stale handle held by a queued signal must degrade quietly.
    state.container.deleteLater()
    p._tabs[wallet] = state
    p._on_status_changed_ui(wallet, "opening channel")
    p._tabs.pop(wallet, None)


def test_refresh_syncs_the_status_from_the_plugin(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    p.wallets[wallet] = object()
    p._tick_status[wallet] = "discovering swap providers"
    p._tabs[wallet].refresh()
    assert "discovering swap providers" in _status_labels(p._tabs[wallet])


# --- the version / update line in the Status footer ------------------------
def test_the_update_line_is_absent_until_the_check_is_switched_on(qapp):
    # A user who declined the update check must not see the feature at all --
    # not even a line telling them they are up to date.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert not [t for t in _status_labels(p._tabs[wallet]) if "version" in t.lower()]


def test_the_update_line_reports_up_to_date(qapp):
    p = _make_plugin()
    p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = True
    p._plugin_version_cache = "0.1.14"
    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = "0.1.14"
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert any("0.1.14 (up to date)" in t for t in _status_labels(p._tabs[wallet]))


def test_the_update_line_links_to_the_release_when_behind(qapp):
    # The link is the whole remedy: nothing is downloaded or installed here, so
    # a user who is behind needs somewhere to go.
    p = _make_plugin()
    p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = True
    p._plugin_version_cache = "0.1.14"
    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = "0.1.15"
    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = "https://example.invalid/releases/tag/v0.1.15"
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    line = next(t for t in _status_labels(p._tabs[wallet]) if "update available" in t)
    assert "0.1.15" in line and "0.1.14" in line
    assert 'href="https://example.invalid/releases/tag/v0.1.15"' in line


def test_the_update_line_escapes_what_it_renders(qapp):
    # The label renders rich text and both halves originate off-machine (a
    # GitHub release payload). `extract_release` sanitises them at the boundary;
    # this pins the second guard, so markup can never reach a QLabel that would
    # render it -- an <img> here would fetch a remote URL from inside the wallet.
    p = _make_plugin()
    p.config.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = True
    p._plugin_version_cache = "0.1.14"
    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = '0.1.15'
    p.config.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = 'https://x/"><img src="http://tracker.invalid/p">'
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    line = next(t for t in _status_labels(p._tabs[wallet]) if "update available" in t)
    assert "<img" not in line
    assert "&lt;img" in line or "&quot;" in line


def _settings_layout_order(state) -> List[str]:
    """Ordered markers for the Settings tab's top-level layout rows.

    Labels contribute their text, nested rows contribute their button labels, so
    a test can assert *where* a section sits rather than merely that it exists.
    """
    settings = state.container.findChild(QTabWidget).widget(0)
    layout = settings.layout()
    out: List[str] = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if isinstance(widget, QLabel):          # QLabel before QFrame: it IS one
            out.append(f"label:{widget.text()}")
        elif isinstance(widget, QFrame):
            out.append("separator")
        elif widget is not None:
            out.append(f"widget:{type(widget).__name__}")
        elif item.layout() is not None:
            buttons = [item.layout().itemAt(j).widget().text()
                       for j in range(item.layout().count())
                       if isinstance(item.layout().itemAt(j).widget(), QPushButton)]
            out.append(f"row:{','.join(buttons)}")
        else:
            out.append("stretch")
    return out


def test_status_section_sits_at_the_bottom_of_the_settings_tab(qapp):
    # It is a footer, below the Apply button and after the stretch that pins it
    # to the bottom edge -- not a banner wedged between the automation switch
    # and the settings fields.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    order = _settings_layout_order(p._tabs[wallet])

    apply_row = next(i for i, m in enumerate(order) if m.startswith("row:") and "Apply" in m)
    header = order.index("label:Status")
    stretch = next(i for i, m in enumerate(order) if m == "stretch")

    assert apply_row < stretch < header, order
    # Status header, the status itself, the "since" line, the version/update
    # line (empty and hidden unless the update check is switched on), and the
    # Unlock button that appears with a "wallet locked" status (hidden otherwise).
    assert order[-5:] == ["label:Status", f"label:{STATUS_NOT_STARTED}",
                          "label:", "label:", "row:Unlock wallet…"], order
    assert order[header - 1] == "separator", order


def test_status_section_still_updates_from_its_new_position(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    p._on_status_changed_ui(wallet, "discovering swap providers")
    assert "discovering swap providers" in _status_labels(p._tabs[wallet])


# --- Log-tab capture options (moved here from the Advanced tab) -----------
# They govern what lands in the view directly above them, so they live next to
# it. No Apply button on this tab: each one applies immediately.
def _capture_cb(state, needle: str):
    from PyQt6.QtWidgets import QCheckBox
    return next(cb for cb in state.log_tab.findChildren(QCheckBox)
                if needle in cb.text())


def _buffer_edit(state) -> QLineEdit:
    return next(e for e in state.log_tab.findChildren(QLineEdit)
                if "substring" not in e.placeholderText())


def test_capture_options_moved_off_the_advanced_tab(qapp):
    from PyQt6.QtWidgets import QCheckBox, QLabel
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    cb_texts = [cb.text() for cb in advanced_tab.findChildren(QCheckBox)]
    assert not any("capture" in t.lower() for t in cb_texts), cb_texts
    labels = [lbl.text() for lbl in advanced_tab.findChildren(QLabel)]
    assert not any(t.startswith("Log tab buffer") for t in labels), labels


def test_capture_options_present_on_the_log_tab(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    assert _capture_cb(state, "Lightning logs").isChecked() is False
    assert _capture_cb(state, "debug-level").isChecked() is False
    assert _buffer_edit(state).text() == str(DEFAULT_LOG_BUFFER_LINES)


def test_capture_checkboxes_apply_immediately(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]

    _capture_cb(state, "Lightning logs").setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_LOG_CAPTURE_LN is True
    assert p.log_capture.capture_ln is True          # live handler reconfigured

    _capture_cb(state, "debug-level").setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG is True

    _capture_cb(state, "Lightning logs").setChecked(False)
    assert p.config.INBOUND_LIQUIDITY_LOG_CAPTURE_LN is False
    assert p.log_capture.capture_ln is False


def test_buffer_size_applies_and_clamps_on_edit(qapp):
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        MAX_LOG_BUFFER_LINES, MIN_LOG_BUFFER_LINES,
    )
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    edit = _buffer_edit(state)

    edit.setText("2500")
    edit.editingFinished.emit()
    assert p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES == 2500
    assert p.log_buffer.max_lines == 2500

    edit.setText("1")                                 # below the floor
    edit.editingFinished.emit()
    assert p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES == MIN_LOG_BUFFER_LINES
    assert edit.text() == str(MIN_LOG_BUFFER_LINES)   # box snaps to what is in force

    edit.setText("10" + "0" * 9)                      # above the ceiling
    edit.editingFinished.emit()
    assert p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES == MAX_LOG_BUFFER_LINES
    assert edit.text() == str(MAX_LOG_BUFFER_LINES)


def test_buffer_size_rejects_a_non_numeric_entry(qapp):
    # Garbage must not be persisted, and the box must snap back to the value the
    # ring is actually using rather than keep showing the bad text.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    state = p._tabs[wallet]
    edit = _buffer_edit(state)

    edit.setText("not a number")
    edit.editingFinished.emit()
    assert p.config.INBOUND_LIQUIDITY_LOG_BUFFER_LINES == DEFAULT_LOG_BUFFER_LINES
    assert edit.text() == str(DEFAULT_LOG_BUFFER_LINES)


# --- liquidity sink settings ----------------------------------------------
def _sink_edit(settings_tab):
    # Index 7: see the field order pinned in test_apply_persists_and_clamps.
    return _plain_line_edits(settings_tab)[7]


def _sink_only_checkbox(settings_tab):
    from PyQt6.QtWidgets import QCheckBox
    return next(cb for cb in settings_tab.findChildren(QCheckBox)
                if "Disable submarine swaps" in cb.text())


def _defer_sink_checkbox(settings_tab):
    from PyQt6.QtWidgets import QCheckBox
    return next(cb for cb in settings_tab.findChildren(QCheckBox)
                if "until inbound liquidity goal is met" in cb.text())


def _apply_button(settings_tab):
    from PyQt6.QtWidgets import QPushButton
    return next(b for b in settings_tab.findChildren(QPushButton)
                if b.text() == "Apply")


def _status_text(settings_tab):
    from PyQt6.QtWidgets import QLabel
    # The save/validation status label sits immediately above the Apply row.
    return [lbl.text() for lbl in settings_tab.findChildren(QLabel)]


def test_sink_address_accepts_a_lightning_address(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    _sink_edit(tab).setText("myname@strike.me")
    _apply_button(tab).click()
    assert p.config.INBOUND_LIQUIDITY_SINK_ADDRESS == "myname@strike.me"


def test_sink_address_that_cannot_be_resolved_is_refused(qapp):
    # Stored-but-unusable would read as "configured" on this tab while every
    # drain silently fell back to a reverse swap.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    _sink_edit(tab).setText("definitely not an address")
    _apply_button(tab).click()
    assert p.config.INBOUND_LIQUIDITY_SINK_ADDRESS == ""
    assert any("Not a Lightning address" in t for t in _status_text(tab))


def test_a_blank_sink_address_is_accepted_as_off(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_SINK_ADDRESS = "myname@strike.me"
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    _sink_edit(tab).setText("")
    _apply_button(tab).click()
    assert p.config.INBOUND_LIQUIDITY_SINK_ADDRESS == ""
    assert any(t == "Settings saved." for t in _status_text(tab))


def test_sink_only_checkbox_applies_immediately(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_SINK_ADDRESS = "myname@strike.me"
    p._add_liquidity_tab(window, wallet)
    cb = _sink_only_checkbox(_settings_tab(p, wallet))

    cb.setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS is True
    cb.setChecked(False)
    assert p.config.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS is False


def test_sink_only_without_a_sink_warns_that_nothing_can_drain(qapp):
    # Legal but inert: the engine honours the switch and declines. The tab must
    # say so rather than let the user believe automation is still working.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    _sink_only_checkbox(tab).setChecked(True)
    assert any("nothing can drain a channel" in t for t in _status_text(tab))


def test_the_warning_clears_once_a_sink_is_saved(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    _sink_only_checkbox(tab).setChecked(True)
    # ...and the sink must be free to run: held back until the goal (the shipped
    # default) it would still leave nothing able to drain a channel, which is a
    # warning of its own -- asserted below.
    _defer_sink_checkbox(tab).setChecked(False)
    _sink_edit(tab).setText("myname@strike.me")
    _apply_button(tab).click()
    assert any(t == "Settings saved." for t in _status_text(tab))
    assert not any("nothing can drain a channel" in t for t in _status_text(tab))


def test_the_checkbox_reflects_the_persisted_value_on_build(qapp):
    p = _make_plugin()
    p.config.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS = True
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert _sink_only_checkbox(_settings_tab(p, wallet)).isChecked()


# --- "don't use the sink until the goal is met" ---------------------------
def test_defer_sink_checkbox_is_checked_by_default(qapp):
    # It ships ON: a sink configured on a wallet still building its channels
    # would otherwise pay away the very balance those opens need.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert _defer_sink_checkbox(_settings_tab(p, wallet)).isChecked()


def test_defer_sink_checkbox_applies_immediately(qapp):
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p.config.INBOUND_LIQUIDITY_SINK_ADDRESS = "myname@strike.me"
    p._add_liquidity_tab(window, wallet)
    cb = _defer_sink_checkbox(_settings_tab(p, wallet))

    cb.setChecked(False)
    assert p.config.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL is False
    cb.setChecked(True)
    assert p.config.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL is True


def test_defer_sink_reflects_the_persisted_value_on_build(qapp):
    p = _make_plugin()
    p.config.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL = False
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    assert not _defer_sink_checkbox(_settings_tab(p, wallet)).isChecked()


def test_deferred_sink_with_swaps_disabled_warns_that_nothing_can_drain(qapp):
    # Both switches are honoured by the engine, and together they leave the
    # plugin with no way to drain a channel until the goal is met. Say so at the
    # moment the user creates the combination, not only in the decision log.
    p = _make_plugin()
    p.config.INBOUND_LIQUIDITY_SINK_ADDRESS = "myname@strike.me"
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    _sink_only_checkbox(tab).setChecked(True)
    assert any("held back until the liquidity goal is met" in t
               for t in _status_text(tab))
    # Letting the sink run again resolves it: the sink alone can drain.
    _defer_sink_checkbox(tab).setChecked(False)
    assert not any("nothing can drain a channel" in t for t in _status_text(tab))


def test_refresh_resyncs_both_sink_switches(qapp):
    # They can be changed from outside this tab (the cmdline plugin, a
    # hand-edited config); a stale tick-box would misreport whether the sink is
    # in use at all.
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tab = _settings_tab(p, wallet)

    p.config.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL = False
    p.config.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS = True
    p._tabs[wallet].refresh()
    assert not _defer_sink_checkbox(tab).isChecked()
    assert _sink_only_checkbox(tab).isChecked()
    # Re-syncing must not have written anything back through the toggle handlers.
    assert p.config.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL is False
    assert p.config.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS is True


def test_advanced_tab_exposes_and_persists_the_minimum_swap_size(qapp):
    """The floor is user-facing, so it needs a field -- and one that round-trips.

    Clamping is asserted too: a negative floor is meaningless and must land as 0
    (off) rather than as a negative bound nothing can satisfy.
    """
    from PyQt6.QtWidgets import QPushButton
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced_tab = p._tabs[wallet].container.findChild(QTabWidget).widget(3)
    edit = _edit_for(advanced_tab, "Minimum swap size")
    assert edit.text() == "25000", "the field does not show the shipped default"

    apply_btn = next(b for b in advanced_tab.findChildren(QPushButton)
                     if b.text() == "Apply")
    edit.setText("60000")
    apply_btn.click()
    assert p.config.INBOUND_LIQUIDITY_MIN_SWAP_SAT == 60_000

    edit.setText("-1")
    apply_btn.click()
    assert p.config.INBOUND_LIQUIDITY_MIN_SWAP_SAT == 0
