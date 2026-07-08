# Qt GUI entry point: a persistent top-level "Liquidity" tab in the main
# window, with the settings on one sub-tab and the decision log (Actions /
# Declines) on two more. The automation itself lives in the base class
# (`__init__.py`); this file is purely the user-facing surface.
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from electrum.i18n import _
from electrum.plugin import hook

from electrum.gui.qt.util import read_QIcon

import asyncio
import time

from . import (
    LiquidityPlugin, MAX_LOG_RETENTION_DAYS, DEV_FEE_MAX_PCT,
    DEV_FEE_PAYOUT_THRESHOLD_SAT, DEV_FEE_DAILY_CAP_SAT,
    _parse_npub_set, _parse_partner_list, _parse_banned_partners,
)
from .liquidity_manager import normalize_node_id
from .qt_widgets import ToggleSwitch

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


def _wrapped_label(text: str) -> QLabel:
    """A QLabel whose (often multi-paragraph) text wraps to the panel width
    instead of being clipped at the right edge."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


class _Signals(QObject):
    activity = pyqtSignal(object, str)        # (wallet, message)
    log_changed = pyqtSignal(object)          # (wallet,)
    providers_changed = pyqtSignal(object)    # (wallet,) -- discovered provider list refreshed


class _TabState:
    """Per-window UI handles for one open wallet's Liquidity tab."""

    def __init__(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet',
                 container: QWidget, actions_tree: QTreeWidget,
                 declines_tree: QTreeWidget, refresh: Callable[[], None],
                 repopulate_providers: Callable[[], None],
                 repopulate_partners: Callable[[], None]) -> None:
        self.window = window
        self.wallet = wallet
        self.container = container
        self.actions_tree = actions_tree
        self.declines_tree = declines_tree
        self.refresh = refresh
        self.repopulate_providers = repopulate_providers
        self.repopulate_partners = repopulate_partners


def _fmt_sat(amount: Optional[int]) -> str:
    return f"{amount:,}" if isinstance(amount, int) else "—"


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


def _fmt_age(ts: float) -> str:
    """Compact 'time since' for the Providers tab's last-fault column."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    secs = max(0, int(time.time() - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return _("just now")


class Plugin(LiquidityPlugin):

    def __init__(self, *args) -> None:
        LiquidityPlugin.__init__(self, *args)
        # Created lazily in load_wallet so the QObject is affined to the GUI
        # thread; on_action_done / on_log_changed (asyncio thread) then emit to
        # it via queued connections.
        self.signals: Optional[_Signals] = None
        # One Liquidity tab per open wallet (Electrum opens one window/wallet).
        self._tabs: Dict['Abstract_Wallet', _TabState] = {}

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow') -> None:
        if self.signals is None:
            self.signals = _Signals()
            self.signals.activity.connect(self._show_activity)
            self.signals.log_changed.connect(self._on_log_changed_ui)
            self.signals.providers_changed.connect(self._on_providers_changed_ui)
        self._add_liquidity_tab(window, wallet)
        self.start_wallet(wallet)

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet') -> None:
        self.stop_wallet(wallet)
        self._remove_liquidity_tab(wallet)

    def requires_settings(self) -> bool:
        # Settings now live in the Liquidity tab rather than a settings dialog.
        return False

    def on_action_done(self, wallet: 'Abstract_Wallet', message: str) -> None:
        # Called from the asyncio thread; emit a queued signal so the status
        # update runs on the GUI thread.
        if self.signals is not None:
            self.signals.activity.emit(wallet, message)

    def on_log_changed(self, wallet: 'Abstract_Wallet') -> None:
        # Called from the asyncio thread whenever the decision log grows; emit a
        # queued signal so an open tab can refresh on the GUI thread.
        if self.signals is not None:
            self.signals.log_changed.emit(wallet)

    def _show_activity(self, wallet: 'Abstract_Wallet', message: str) -> None:
        state = self._tabs.get(wallet)
        if state is None:
            return
        try:
            state.window.statusBar().showMessage(
                _("Inbound Liquidity") + ": " + message, 12000)
        except Exception:
            pass

    def _on_log_changed_ui(self, wallet: 'Abstract_Wallet') -> None:
        state = self._tabs.get(wallet)
        if state is not None:
            state.refresh()

    def _on_providers_changed_ui(self, wallet: 'Abstract_Wallet') -> None:
        state = self._tabs.get(wallet)
        if state is not None:
            state.repopulate_providers()

    # --- tab lifecycle ----------------------------------------------------
    def _add_liquidity_tab(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet') -> None:
        if wallet in self._tabs:
            return
        (container, actions_tree, declines_tree, refresh, repopulate_providers,
         repopulate_partners) = self._build_liquidity_tab(window, wallet)
        self._tabs[wallet] = _TabState(
            window, wallet, container, actions_tree, declines_tree, refresh,
            repopulate_providers, repopulate_partners)
        refresh()
        try:
            window.tabs.addTab(container, read_QIcon("lightning.png"), _("Liquidity"))
        except Exception:
            self.logger.exception("could not add Liquidity tab")

    def _remove_liquidity_tab(self, wallet: 'Abstract_Wallet') -> None:
        state = self._tabs.pop(wallet, None)
        if state is None:
            return
        try:
            i = state.window.tabs.indexOf(state.container)
            if i != -1:
                state.window.tabs.removeTab(i)
        except Exception:
            pass

    # --- decision-log views -----------------------------------------------
    def _make_log_tab(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderLabels([
            _("Time"), _("Type"), _("Amount (sat)"), _("Source → Dest"), _("Reason")])
        tree.setColumnWidth(0, 150)
        tree.setColumnWidth(1, 60)
        tree.setColumnWidth(2, 100)
        tree.setColumnWidth(3, 150)
        tree.setRootIsDecorated(True)   # show the expand triangle
        tree.setUniformRowHeights(False)
        tree.setAlternatingRowColors(True)
        return tree

    def _state_detail_lines(self, state: Dict) -> List[str]:
        """Human-readable lines describing the state behind a decision, shown as
        the expandable children of a log row."""
        if not state:
            return [_("(no state captured)")]
        lines: List[str] = []
        lines.append(_("on-chain spendable: {} sat").format(_fmt_sat(state.get("onchain_spendable_sat"))))
        lines.append(_("channels: {} total, {} active, {} pending open").format(
            state.get("num_channels", "?"), state.get("active_channels", "?"),
            state.get("pending_channel_count", "?")))
        lines.append(_("reverse swaps in flight: {}").format(state.get("inflight_swap_count", "?")))
        fee = state.get("swap_percentage_fee")
        lines.append(_("provider: fee {}%, min {} sat, max {} sat, mining {} sat, claim {} sat").format(
            fee if fee is not None else "?",
            _fmt_sat(state.get("provider_min_amount_sat")),
            _fmt_sat(state.get("provider_max_reverse_sat")),
            _fmt_sat(state.get("swap_mining_fee_sat")),
            _fmt_sat(state.get("swap_claim_fee_sat"))))
        if "providers_discovered" in state:
            lines.append(_("swap providers: {} discovered, {} eligible after preferred/banned").format(
                state.get("providers_discovered", "?"), state.get("providers_eligible", "?")))
        cfg = state.get("config") or {}
        if cfg:
            lines.append(_("config: max_channels={}, min_open={}, reserve={}, "
                           "max_fee%={}, trigger%={}, trigger_sat={}").format(
                cfg.get("max_channels"), cfg.get("min_onchain_to_open_sat"),
                cfg.get("onchain_reserve_sat"), cfg.get("max_swap_fee_pct"),
                cfg.get("swap_trigger_pct"), cfg.get("swap_trigger_sat")))
            if "max_opens_per_day" in cfg or "max_closes_per_day" in cfg:
                lines.append(_("daily ceilings: opens {}/day ({} in last 24h), "
                               "closes {}/day (0 = unlimited)").format(
                    cfg.get("max_opens_per_day"), state.get("opens_last_24h", "?"),
                    cfg.get("max_closes_per_day")))
        for ch in state.get("channels", []):
            lines.append(_("chan {}: cap {} local {} remote {} spendable {} active={}").format(
                ch.get("short_id"), _fmt_sat(ch.get("capacity_sat")),
                _fmt_sat(ch.get("local_sat")), _fmt_sat(ch.get("remote_sat")),
                _fmt_sat(ch.get("spendable_local_sat")), ch.get("is_active")))
        return lines

    def _populate_log_tree(self, tree: QTreeWidget, entries: List[Dict]) -> None:
        tree.clear()
        for e in entries:
            src = e.get("source")
            dest = e.get("dest")
            if src and dest:
                src_dest = f"{src} → {dest}"
            else:
                src_dest = src or dest or "—"
            top = QTreeWidgetItem([
                _fmt_time(e.get("ts", 0)),
                str(e.get("kind", "")),
                _fmt_sat(e.get("amount_sat")),
                src_dest,
                e.get("reason", ""),
            ])
            detail = e.get("detail")
            if detail:
                child = QTreeWidgetItem([str(detail)])
                child.setFirstColumnSpanned(True)
                top.addChild(child)
            for line in self._state_detail_lines(e.get("state") or {}):
                child = QTreeWidgetItem([line])
                child.setFirstColumnSpanned(True)
                top.addChild(child)
            tree.addTopLevelItem(top)

    # --- tab construction -------------------------------------------------
    def _build_liquidity_tab(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet'):
        """Build the top-level Liquidity tab widget and return
        (container, actions_tree, declines_tree, refresh_fn)."""
        container = QWidget()
        outer = QVBoxLayout(container)

        tabs = QTabWidget()
        outer.addWidget(tabs)

        # --- Settings sub-tab ---------------------------------------------
        settings_tab = QWidget()
        vbox = QVBoxLayout(settings_tab)

        c = self.config

        # --- master ENABLED/DISABLED slider -------------------------------
        # The big, obvious arm switch for all automation. It is DISABLED by
        # default so the user can review every setting before the plugin ever
        # moves funds or alters a channel. Unlike the other fields (which need
        # the Apply button), flipping this takes effect immediately.
        toggle = ToggleSwitch()
        toggle.setChecked(bool(c.INBOUND_LIQUIDITY_AUTOMATION_ENABLED))
        toggle_status = QLabel()
        _f = toggle_status.font()
        _f.setPointSize(max(13, _f.pointSize() + 3))
        _f.setBold(True)
        toggle_status.setFont(_f)

        def _sync_toggle_label() -> None:
            on = toggle.isChecked()
            toggle_status.setText(_("ENABLED") if on else _("DISABLED"))
            toggle_status.setStyleSheet("color: #2ea043;" if on else "color: #cc3333;")

        def _sync_toggle_from_config() -> None:
            # Re-read config into the switch without re-firing on_toggle (which
            # would kick a fresh evaluation).
            toggle.blockSignals(True)
            toggle.setChecked(bool(c.INBOUND_LIQUIDITY_AUTOMATION_ENABLED))
            toggle.blockSignals(False)
            _sync_toggle_label()

        def on_toggle(checked: bool) -> None:
            setattr(c, 'INBOUND_LIQUIDITY_AUTOMATION_ENABLED', bool(checked))
            _sync_toggle_label()
            if checked:
                # Start acting on current state right away rather than waiting
                # for the next wallet event.
                self.request_evaluation(wallet)
                self.on_action_done(wallet, _("Automation enabled."))
            else:
                self.on_action_done(wallet, _("Automation disabled."))

        toggle.toggled.connect(on_toggle)
        _sync_toggle_label()

        header = QHBoxLayout()
        header.addWidget(QLabel(_("Automation")))
        header.addWidget(toggle)
        header.addWidget(toggle_status)
        header.addStretch(1)
        vbox.addLayout(header)

        vbox.addWidget(_wrapped_label(
            _("When enabled, automatically opens channels and reverse-swaps "
              "Lightning funds out to on-chain to keep inbound liquidity "
              "available. Disabled by default — review the settings below first.")))

        # --- "Manual run only" mode + the manual trigger ------------------
        # A middle ground for a cautious user: keep the master switch armed but
        # let the plugin act ONLY when "Run now" is pressed -- never on a wallet
        # event, the heartbeat, or the post-load timer. Applied immediately (like
        # the master switch), since it changes runtime behaviour rather than a
        # tunable that waits for Apply.
        manual_only_cb = QCheckBox(_("Manual run only (never act on its own)"))
        manual_only_cb.setToolTip(_(
            "Let the plugin evaluate and act only when you press \"Run now\" — "
            "never on its own, not on a timer and not in response to incoming "
            "payments or channel updates. Use this to try the plugin without "
            "trusting full automation. The Automation switch above must still be "
            "enabled for a manual run to move any funds."))
        manual_only_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_MANUAL_RUN_ONLY', False)))

        def on_manual_only(checked: bool) -> None:
            setattr(c, 'INBOUND_LIQUIDITY_MANUAL_RUN_ONLY', bool(checked))

        manual_only_cb.toggled.connect(on_manual_only)
        vbox.addWidget(manual_only_cb)

        run_now_btn = QPushButton(_("Run now"))
        run_now_btn.setToolTip(_(
            "Evaluate once right now and take any warranted action, regardless of "
            "the \"Manual run only\" setting. Requires the Automation switch to be "
            "enabled."))

        def on_run_now() -> None:
            self.request_evaluation(wallet, manual=True)
            self.on_action_done(wallet, _("Manual evaluation triggered."))

        run_now_btn.clicked.connect(on_run_now)
        run_now_row = QHBoxLayout()
        run_now_row.addWidget(run_now_btn)
        run_now_row.addStretch(1)
        vbox.addLayout(run_now_row)

        grid = QGridLayout()
        # (label, current value as text, parser, setter) for each tunable kept on
        # the main Settings tab. Power-user knobs (on-chain reserve, reliability
        # tuning, offline auto-close, log retention, diagnostics, daily ceilings)
        # and the feature on/off toggles live on the Advanced sub-tab instead.
        fields = [
            (_("Min on-chain to open a channel (sat)"),
             str(c.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT), int,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT', v)),
            (_("Maximum number of channels"),
             str(c.INBOUND_LIQUIDITY_MAX_CHANNELS), int,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_MAX_CHANNELS', v)),
            (_("Max fee to move LN → on-chain (%, all-in)"),
             str(c.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT), float,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT', v)),
            (_("Swap-out trigger (% of capacity)"),
             str(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT), float,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT', v)),
            (_("Swap-out trigger (sat)"),
             str(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT), int,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT', v)),
            # Optional development fee, charged on the on-chain amount received
            # from plugin-initiated reverse swaps (clamped to 0..DEV_FEE_MAX_PCT).
            # The payout address is fixed (not user-editable) — see __init__.py.
            (_("Dev fee (%, 0–{:g}; 0 = off)").format(DEV_FEE_MAX_PCT),
             str(c.INBOUND_LIQUIDITY_DEV_FEE_PCT), float,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_DEV_FEE_PCT',
                               max(0.0, min(float(v), DEV_FEE_MAX_PCT)))),
        ]
        edits = []
        for row, (label, value, parser, setter) in enumerate(fields):
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(value)
            grid.addWidget(edit, row, 1)
            edits.append((edit, parser, setter, label))
        vbox.addLayout(grid)

        # Live read-out of the dev-fee ledger: sats owed (accrued, not yet paid)
        # and sats paid out in the trailing 24h (against the daily cap).
        dev_fee_label = QLabel("")
        dev_fee_label.setToolTip(_(
            "Dev fee accrues on the on-chain amount received from reverse swaps "
            "the plugin initiates. It is paid automatically to the payout address "
            "once at least {thr} sat is owed, capped at {cap} sat per rolling "
            "24 hours.").format(thr=DEV_FEE_PAYOUT_THRESHOLD_SAT, cap=DEV_FEE_DAILY_CAP_SAT))

        def _refresh_dev_fee_label() -> None:
            try:
                st = self.dev_fee_status(wallet)
            except Exception:
                dev_fee_label.setText("")
                return
            dev_fee_label.setText(_("Dev fee: {owed} sat owed, {paid} sat paid in last 24h "
                                    "({head} sat left today).").format(
                owed=_fmt_sat(st["owed_sat"]), paid=_fmt_sat(st["paid_last_24h_sat"]),
                head=_fmt_sat(st["daily_headroom_sat"])))

        _refresh_dev_fee_label()
        vbox.addWidget(dev_fee_label)

        status_label = QLabel("")
        vbox.addWidget(status_label)

        apply_btn = QPushButton(_("Apply"))

        def on_apply() -> None:
            # Parse and validate everything before persisting anything.
            parsed = []
            for edit, parser, setter, label in edits:
                text = edit.text().strip()
                try:
                    parsed.append((setter, parser(text) if parser is not str else text))
                except ValueError:
                    status_label.setStyleSheet("color: red;")
                    status_label.setText(_("Invalid value for: {}").format(label))
                    return
            # (Automation on/off is owned by the slider above and applied
            # immediately, so the Apply button never touches it. The feature
            # toggles and tuning knobs live on the Advanced sub-tab.)
            for setter, value in parsed:
                setter(value)
            # min_onchain may have changed: re-assert the channel-funding floor so
            # a lowered floor keeps matching the (new) configured value at once.
            self._enforce_min_funding_floor()
            self._reload_settings_fields(edits, _sync_toggle_from_config)
            _refresh_dev_fee_label()
            status_label.setStyleSheet("color: green;")
            status_label.setText(_("Settings saved."))

        apply_btn.clicked.connect(on_apply)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        vbox.addLayout(btn_row)
        vbox.addStretch(1)
        tabs.addTab(settings_tab, _("Settings"))

        # --- Swap providers sub-tab ---------------------------------------
        providers_tab, repopulate_providers = self._build_providers_tab(wallet)
        tabs.addTab(providers_tab, _("Swap providers"))

        # --- Channel partners sub-tab -------------------------------------
        partners_tab, repopulate_partners = self._build_channel_partners_tab(wallet)
        tabs.addTab(partners_tab, _("Channel partners"))

        # --- Advanced sub-tab (last config tab, before the read-only logs) -
        advanced_tab, repopulate_advanced = self._build_advanced_tab(wallet)
        tabs.addTab(advanced_tab, _("Advanced"))

        # --- Decision-log sub-tabs ----------------------------------------
        actions_tree = self._make_log_tab()
        declines_tree = self._make_log_tab()
        faults_tree = self._make_log_tab()
        tabs.addTab(actions_tree, _("Actions"))
        tabs.addTab(declines_tree, _("Declines"))
        tabs.addTab(faults_tree, _("Faults"))

        def refresh() -> None:
            _sync_toggle_from_config()
            # Re-read the manual-run-only checkbox without re-firing its handler.
            manual_only_cb.blockSignals(True)
            manual_only_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_MANUAL_RUN_ONLY', False)))
            manual_only_cb.blockSignals(False)
            self._populate_log_tree(actions_tree, self.get_decision_log(wallet, "action"))
            self._populate_log_tree(declines_tree, self.get_decision_log(wallet, "decline"))
            self._populate_log_tree(faults_tree, self.get_decision_log(wallet, "fault"))
            _refresh_dev_fee_label()
            repopulate_providers()
            repopulate_partners()
            repopulate_advanced()

        return (container, actions_tree, declines_tree, refresh,
                repopulate_providers, repopulate_partners)

    # --- advanced sub-tab -------------------------------------------------
    def _build_advanced_tab(self, wallet: 'Abstract_Wallet'):
        """Build the Advanced sub-tab: the feature on/off toggles, the daily
        (rolling-24h) action ceilings (a runaway guard), and the power-user tuning
        knobs (on-chain reserve, reliability tuning, offline auto-close, log
        retention) moved off the main Settings tab. Returns (widget, repopulate).
        """
        from . import DEFAULT_MAX_OPENS_PER_DAY, DEFAULT_MAX_CLOSES_PER_DAY

        c = self.config
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.addWidget(_wrapped_label(_(
            "Advanced settings — feature toggles, runaway-guard ceilings, and "
            "tuning knobs moved off the main Settings tab. The defaults are "
            "sensible; change them only if you understand the effect.")))

        # --- feature on/off toggles (moved from the Settings tab) ----------
        reliability_cb = QCheckBox(_("Track provider reliability"))
        reliability_cb.setToolTip(_("Penalise providers that time out, error, or leave swaps "
                                    "stuck, so reliable providers are preferred."))
        v.addWidget(reliability_cb)

        peer_reliability_cb = QCheckBox(_("Track channel-peer reliability"))
        peer_reliability_cb.setToolTip(_("Penalise channel peers that fail to open, go offline, or "
                                         "force-close, and auto-ban serial offenders."))
        v.addWidget(peer_reliability_cb)

        auto_remediate_cb = QCheckBox(_("Force-close wedged channel opens"))
        auto_remediate_cb.setToolTip(_("When a channel open is wedged past the timeout, force-close "
                                       "it to free the funds and resume automation (broadcasts a tx "
                                       "and incurs a mining fee)."))
        v.addWidget(auto_remediate_cb)

        autoclose_cb = QCheckBox(_("Auto-close channels whose peer stays offline"))
        autoclose_cb.setToolTip(_("For channels this plugin opened, when the peer has been effectively "
                                  "offline for a sustained period, close the channel cooperatively, "
                                  "and force-close it after the deadline if it still hasn't closed "
                                  "(broadcasts a tx and incurs a mining fee)."))
        v.addWidget(autoclose_cb)

        diag_log_cb = QCheckBox(_("Write diagnostic log files"))
        diag_log_cb.setToolTip(_("Append this plugin's decisions and errors to daily text files "
                                 "(one folder per wallet, kept 30 days) under the Electrum data "
                                 "directory. Contains no private keys or seeds. Off by default."))
        v.addWidget(diag_log_cb)

        checkboxes = [
            (reliability_cb, 'INBOUND_LIQUIDITY_RELIABILITY_ENABLED'),
            (peer_reliability_cb, 'INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED'),
            (auto_remediate_cb, 'INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN'),
            (autoclose_cb, 'INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED'),
            (diag_log_cb, 'INBOUND_LIQUIDITY_DIAG_LOG_ENABLED'),
        ]

        grid = QGridLayout()
        row = 0
        # --- daily action ceilings (with a live 24h usage read-out) --------
        # Kept as the first two grid rows so their line-edits stay at
        # findChildren(QLineEdit) index 0/1 (the Advanced-tab tests rely on it).
        ceiling_specs = [
            (_("Max channel opens per day (0 = unlimited)"),
             'INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY', DEFAULT_MAX_OPENS_PER_DAY, "open"),
            (_("Max channel closes per day (0 = unlimited)"),
             'INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY', DEFAULT_MAX_CLOSES_PER_DAY, "close"),
        ]
        ceiling_rows = []  # (edit, attr, default, usage_label, kind)
        for (label, attr, default, kind) in ceiling_specs:
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(str(getattr(c, attr, default)))
            grid.addWidget(edit, row, 1)
            usage = QLabel("")
            usage.setStyleSheet("color: gray;")
            grid.addWidget(usage, row, 2)
            ceiling_rows.append((edit, attr, default, usage, kind))
            row += 1

        # --- tuning knobs (moved from the Settings tab) --------------------
        # (label, reload-attr, parser, setter) — the setter embeds any clamping.
        fields = [
            (_("On-chain reserve when opening (sat)"),
             'INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT', val)),
            (_("Keep decision log for (days, 1–{})").format(MAX_LOG_RETENTION_DAYS),
             'INBOUND_LIQUIDITY_LOG_RETENTION_DAYS', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_LOG_RETENTION_DAYS',
                                 max(1, min(int(val), MAX_LOG_RETENTION_DAYS)))),
            (_("Reliability penalty per fault (%)"),
             'INBOUND_LIQUIDITY_RELIABILITY_BASE_PENALTY_PCT', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_RELIABILITY_BASE_PENALTY_PCT', max(0.0, val))),
            (_("Max reliability penalty (%)"),
             'INBOUND_LIQUIDITY_RELIABILITY_PENALTY_CAP_PCT', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_RELIABILITY_PENALTY_CAP_PCT', max(0.0, val))),
            (_("Reliability recovery half-life (hours)"),
             'INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS', max(0.0, val))),
            (_("Stuck-swap timeout (minutes)"),
             'INBOUND_LIQUIDITY_RELIABILITY_STUCK_TIMEOUT_MIN', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_RELIABILITY_STUCK_TIMEOUT_MIN', max(1, int(val)))),
            (_("Auto-ban a peer after N hard faults (0 = off)"),
             'INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS', max(0, int(val)))),
            (_("Stuck channel-open timeout (minutes)"),
             'INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN', max(1, int(val)))),
            (_("Stuck reverse-swap timeout (minutes)"),
             'INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN', max(1, int(val)))),
            (_("Offline auto-close: peer-uptime window (days)"),
             'INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS', max(0.0, val))),
            (_("Offline auto-close: minimum peer uptime (%)"),
             'INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT', max(0.0, val))),
            (_("Offline auto-close: force-close after trying to close (days)"),
             'INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS', max(0.0, val))),
        ]
        field_rows = []  # (edit, attr, parser, setter, label)
        for (label, attr, parser, setter) in fields:
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(str(getattr(c, attr)))
            grid.addWidget(edit, row, 1)
            field_rows.append((edit, attr, parser, setter, label))
            row += 1
        v.addLayout(grid)

        status = QLabel("")
        v.addWidget(status)

        def repopulate() -> None:
            for cb, attr in checkboxes:
                cb.setChecked(bool(getattr(c, attr)))
            for edit, attr, default, usage, kind in ceiling_rows:
                edit.setText(str(getattr(c, attr, default)))
                used = self._count_actions_last_24h(wallet, kind)
                usage.setText(_("{} in last 24h").format(used))
            for edit, attr, parser, setter, label in field_rows:
                edit.setText(str(getattr(c, attr)))

        def on_apply() -> None:
            # Validate everything before persisting anything.
            parsed_ceilings = []
            for edit, attr, default, usage, kind in ceiling_rows:
                text = edit.text().strip()
                try:
                    value = int(text)
                except ValueError:
                    status.setStyleSheet("color: red;")
                    status.setText(_("Invalid value: {}").format(text))
                    return
                if value < 0:
                    status.setStyleSheet("color: red;")
                    status.setText(_("Value cannot be negative: {}").format(text))
                    return
                parsed_ceilings.append((attr, value))
            parsed_fields = []
            for edit, attr, parser, setter, label in field_rows:
                text = edit.text().strip()
                try:
                    parsed_fields.append((setter, parser(text)))
                except ValueError:
                    status.setStyleSheet("color: red;")
                    status.setText(_("Invalid value for: {}").format(label))
                    return
            for attr, value in parsed_ceilings:
                setattr(c, attr, value)
            for cb, attr in checkboxes:
                setattr(c, attr, cb.isChecked())
            for setter, value in parsed_fields:
                setter(value)
            repopulate()
            status.setStyleSheet("color: green;")
            status.setText(_("Advanced settings saved."))

        apply_btn = QPushButton(_("Apply"))
        apply_btn.clicked.connect(on_apply)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        v.addLayout(btn_row)
        v.addStretch(1)

        repopulate()
        return tab, repopulate

    # --- providers sub-tab ------------------------------------------------
    _PREF_COL = 6
    _BAN_COL = 7
    _FAULTS_COL = 9

    def _build_providers_tab(self, wallet: 'Abstract_Wallet'):
        """Build the Providers sub-tab: a live list of nostr-discovered swap
        providers with Preferred / Banned toggles, plus free-text npub fields for
        providers that are not currently online. Returns (widget, repopulate_fn).
        """
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.addWidget(_wrapped_label(_(
            "Swaps use the cheapest provider discovered on nostr. Tick "
            "“Preferred” to restrict swaps to only those providers, or "
            "“Banned” to never use one. Use the boxes below to add a "
            "provider that is not currently online.\n"
            "Providers that time out, error, or leave swaps stuck earn a "
            "decaying reliability penalty (added to their cost for ranking only, "
            "so a flaky provider falls behind reliable ones but is still used if "
            "it is the only option). “Reset stats” clears a provider's history.")))

        tree = QTreeWidget()
        tree.setHeaderLabels([
            _("Provider (npub)"), _("Fee %"), _("Mining (sat)"), _("Min (sat)"),
            _("Max (sat)"), _("PoW"), _("Preferred"), _("Banned"),
            _("OK"), _("Faults"), _("Penalty %"), _("Last fault")])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(0, 180)
        v.addWidget(tree)

        grid = QGridLayout()
        pref_edit = QPlainTextEdit()
        pref_edit.setFixedHeight(48)
        pref_edit.setPlaceholderText("npub1…, npub1…")
        ban_edit = QPlainTextEdit()
        ban_edit.setFixedHeight(48)
        ban_edit.setPlaceholderText("npub1…, npub1…")
        grid.addWidget(QLabel(_("Preferred npubs")), 0, 0)
        grid.addWidget(pref_edit, 0, 1)
        grid.addWidget(QLabel(_("Banned npubs")), 1, 0)
        grid.addWidget(ban_edit, 1, 1)
        v.addLayout(grid)

        status = QLabel("")
        v.addWidget(status)

        def load_text_from_config() -> None:
            c = self.config
            pref_edit.setPlainText(", ".join(sorted(_parse_npub_set(c.INBOUND_LIQUIDITY_PREFERRED_NPUBS))))
            ban_edit.setPlainText(", ".join(sorted(_parse_npub_set(c.INBOUND_LIQUIDITY_BANNED_NPUBS))))

        def repopulate() -> None:
            load_text_from_config()
            pref = _parse_npub_set(pref_edit.toPlainText())
            ban = _parse_npub_set(ban_edit.toPlainText())
            rel = self.provider_reliability_rows(wallet)
            tree.clear()
            checked = Qt.CheckState.Checked
            unchecked = Qt.CheckState.Unchecked

            def add_row(npub, cells, *, checkable, r):
                pen = r.get("penalty_pct", 0.0)
                item = QTreeWidgetItem(cells + [
                    str(r.get("success_count", 0)), str(r.get("fault_count", 0)),
                    f"{pen:.2f}", _fmt_age(r.get("last_fault_ts", 0.0))])
                item.setData(0, Qt.ItemDataRole.UserRole, npub)
                if checkable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(self._PREF_COL, checked if npub in pref else unchecked)
                    item.setCheckState(self._BAN_COL, checked if npub in ban else unchecked)
                reason = r.get("last_reason")
                if reason:
                    item.setToolTip(self._FAULTS_COL, str(reason))
                tree.addTopLevelItem(item)

            discovered = set()
            for o in self.discovered_providers(wallet):
                discovered.add(o.npub)
                add_row(o.npub, [
                    self._abbrev(o.npub, 12, 6) or o.npub,
                    f"{o.percentage_fee:g}", f"{o.mining_fee_sat:,}",
                    f"{o.min_amount_sat:,}", f"{o.max_reverse_sat:,}",
                    str(o.pow_bits), "", ""],
                    checkable=True, r=rel.get(o.npub, {}))
            # Also surface providers that have a reliability history but are not
            # currently advertising, so a penalised/offline provider is still
            # visible (and its stats clearable).
            for npub, r in rel.items():
                if npub in discovered:
                    continue
                add_row(npub, [
                    self._abbrev(npub, 12, 6) or npub,
                    _("(offline)"), "", "", "", "", "", ""],
                    checkable=False, r=r)

        def on_apply() -> None:
            # Seed from the text fields (so offline npubs survive), then fold in
            # the per-row checkbox states for the discovered providers.
            pref = set(_parse_npub_set(pref_edit.toPlainText()))
            ban = set(_parse_npub_set(ban_edit.toPlainText()))
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                npub = it.data(0, Qt.ItemDataRole.UserRole)
                if not npub:
                    continue
                if it.checkState(self._PREF_COL) == Qt.CheckState.Checked:
                    pref.add(npub)
                else:
                    pref.discard(npub)
                if it.checkState(self._BAN_COL) == Qt.CheckState.Checked:
                    ban.add(npub)
                else:
                    ban.discard(npub)
            pref -= ban  # a banned provider can't also be preferred
            self.config.INBOUND_LIQUIDITY_PREFERRED_NPUBS = ", ".join(sorted(pref))
            self.config.INBOUND_LIQUIDITY_BANNED_NPUBS = ", ".join(sorted(ban))
            repopulate()
            status.setStyleSheet("color: green;")
            status.setText(_("Providers saved."))

        def on_refresh() -> None:
            loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
            if loop is None:
                status.setStyleSheet("color: red;")
                status.setText(_("Cannot refresh: wallet is offline."))
                return
            status.setStyleSheet("")
            status.setText(_("Refreshing providers…"))

            def done(_fut) -> None:
                if self.signals is not None:
                    self.signals.providers_changed.emit(wallet)

            try:
                fut = asyncio.run_coroutine_threadsafe(self.refresh_providers(wallet), loop)
                fut.add_done_callback(done)
            except Exception:
                self.logger.exception("provider refresh failed to schedule")

        def on_reset_stats() -> None:
            # Clear the selected provider's reliability history, or all if none
            # is selected -- a manual override of the auto-penalty.
            sel = tree.selectedItems()
            if sel:
                npub = sel[0].data(0, Qt.ItemDataRole.UserRole)
                self.clear_provider_reliability(wallet, npub)
                status.setText(_("Reliability stats cleared for {}.").format(
                    self._abbrev(npub, 12, 6) or npub))
            else:
                self.clear_provider_reliability(wallet)
                status.setText(_("All reliability stats cleared."))
            status.setStyleSheet("color: green;")
            repopulate()

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton(_("Refresh"))
        refresh_btn.clicked.connect(on_refresh)
        reset_btn = QPushButton(_("Reset stats"))
        reset_btn.setToolTip(_("Clear the selected provider's reliability history "
                               "(or all providers' if none is selected)."))
        reset_btn.clicked.connect(on_reset_stats)
        apply_btn = QPushButton(_("Apply"))
        apply_btn.clicked.connect(on_apply)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        v.addLayout(btn_row)

        return tab, repopulate

    # --- channel partners sub-tab -----------------------------------------
    _PARTNER_PREF_COL = 1
    _PARTNER_BAN_COL = 2
    _PARTNER_FAULTS_COL = 4

    def _build_channel_partners_tab(self, wallet: 'Abstract_Wallet'):
        """Build the Channel partners sub-tab: a strict-mode toggle, a list of the
        nodes we already have channels with (each with Preferred / Banned toggles),
        and free-text fields for partners we are not yet connected to. Returns
        (widget, repopulate_fn).

        Channel partners are Lightning node ids (not nostr npubs): preferred ones
        are tried first, in order, before Electrum's suggested peer; banned ones
        are never opened to.
        """
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.addWidget(_wrapped_label(_(
            "Channels are opened to Electrum's suggested peer by default. List "
            "“Preferred” partners (node_id@host:port, one per line) to try them "
            "first, in order, before falling back to a suggestion. “Banned” "
            "partners (node id, or node_id@host:port) are never opened to. Tick "
            "the rows below to prefer/ban a node you already have a channel with.")))

        strict_cb = QCheckBox(_("Only open channels to preferred partners (never fall back to suggestions)"))
        strict_cb.setChecked(bool(self.config.INBOUND_LIQUIDITY_PARTNERS_STRICT))
        v.addWidget(strict_cb)

        one_per_peer_cb = QCheckBox(_("Only one channel per peer (don't open a second channel to a node "
                                      "you already have one with)"))
        one_per_peer_cb.setChecked(bool(self.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER))
        v.addWidget(one_per_peer_cb)

        v.addWidget(_wrapped_label(_(
            "Peers that fail to open, go offline, or force-close earn a decaying "
            "reliability penalty (they sink in the try-order but are still used if "
            "needed); after enough hard faults a peer is auto-banned. “Reset "
            "stats” clears a peer's history (it does not un-ban — untick Banned "
            "for that).")))

        tree = QTreeWidget()
        tree.setHeaderLabels([
            _("Channel peer (node id)"), _("Preferred"), _("Banned"),
            _("OK"), _("Faults"), _("Hard"), _("Penalty %"), _("Last fault"),
            _("Last fault reason")])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(0, 300)
        tree.setColumnWidth(8, 240)
        v.addWidget(tree)

        grid = QGridLayout()
        pref_edit = QPlainTextEdit()
        pref_edit.setFixedHeight(60)
        pref_edit.setPlaceholderText("02abc…@host:port")
        ban_edit = QPlainTextEdit()
        ban_edit.setFixedHeight(60)
        ban_edit.setPlaceholderText("02def…  or  02def…@host:port")
        grid.addWidget(QLabel(_("Preferred partners")), 0, 0)
        grid.addWidget(pref_edit, 0, 1)
        grid.addWidget(QLabel(_("Banned partners")), 1, 0)
        grid.addWidget(ban_edit, 1, 1)
        v.addLayout(grid)

        status = QLabel("")
        v.addWidget(status)

        def load_text_from_config() -> None:
            c = self.config
            pref_edit.setPlainText("\n".join(_parse_partner_list(c.INBOUND_LIQUIDITY_PREFERRED_PARTNERS)))
            ban_edit.setPlainText("\n".join(sorted(_parse_banned_partners(c.INBOUND_LIQUIDITY_BANNED_PARTNERS))))
            strict_cb.setChecked(bool(c.INBOUND_LIQUIDITY_PARTNERS_STRICT))
            one_per_peer_cb.setChecked(bool(c.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER))

        def repopulate() -> None:
            load_text_from_config()
            pref_ids = {normalize_node_id(p) for p in _parse_partner_list(pref_edit.toPlainText())}
            ban_ids = _parse_banned_partners(ban_edit.toPlainText())
            rel = self.peer_reliability_rows(wallet)
            tree.clear()
            checked = Qt.CheckState.Checked
            unchecked = Qt.CheckState.Unchecked

            def add_row(nid, *, checkable):
                low = nid.lower()
                r = rel.get(low, {})
                pen = r.get("penalty_pct", 0.0)
                reason = r.get("last_reason") or ""
                item = QTreeWidgetItem([
                    self._abbrev(nid, 12, 6) or nid, "", "",
                    str(r.get("success_count", 0)), str(r.get("fault_count", 0)),
                    str(r.get("hard_fault_count", 0)), f"{pen:.2f}",
                    _fmt_age(r.get("last_fault_ts", 0.0)), str(reason)])
                item.setData(0, Qt.ItemDataRole.UserRole, nid)
                if checkable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(self._PARTNER_PREF_COL, checked if low in pref_ids else unchecked)
                    item.setCheckState(self._PARTNER_BAN_COL, checked if low in ban_ids else unchecked)
                reason = r.get("last_reason")
                if reason:
                    item.setToolTip(self._PARTNER_FAULTS_COL, str(reason))
                tree.addTopLevelItem(item)

            current = set()
            for partner in self.current_channel_partners(wallet):
                nid = partner["node_id"]
                current.add(nid.lower())
                add_row(nid, checkable=True)
            # Also surface peers we have a reliability history for but no current
            # channel with (e.g. an auto-banned or force-closed peer), so their
            # stats stay visible and clearable.
            for low in rel:
                if low not in current:
                    add_row(low, checkable=True)

        def on_apply() -> None:
            # Seed from the text fields (so not-yet-connected partners survive),
            # then fold in the per-row checkbox states for current peers.
            preferred = _parse_partner_list(pref_edit.toPlainText())
            pref_ids = {normalize_node_id(p) for p in preferred}
            banned = set(_parse_banned_partners(ban_edit.toPlainText()))
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                nid = it.data(0, Qt.ItemDataRole.UserRole)
                if not nid:
                    continue
                low = nid.lower()
                if it.checkState(self._PARTNER_BAN_COL) == Qt.CheckState.Checked:
                    banned.add(low)
                else:
                    banned.discard(low)
                if it.checkState(self._PARTNER_PREF_COL) == Qt.CheckState.Checked:
                    if low not in pref_ids:
                        preferred.append(nid)
                        pref_ids.add(low)
                else:
                    preferred = [p for p in preferred if normalize_node_id(p) != low]
                    pref_ids.discard(low)
            # A banned partner can't also be preferred.
            preferred = [p for p in preferred if normalize_node_id(p) not in banned]
            self.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS = ", ".join(preferred)
            self.config.INBOUND_LIQUIDITY_BANNED_PARTNERS = ", ".join(sorted(banned))
            self.config.INBOUND_LIQUIDITY_PARTNERS_STRICT = strict_cb.isChecked()
            self.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER = one_per_peer_cb.isChecked()
            repopulate()
            status.setStyleSheet("color: green;")
            status.setText(_("Channel partners saved."))

        def on_reset_stats() -> None:
            # Clear the selected peer's reliability history, or all if none is
            # selected -- a manual override of the auto-penalty / fault tally.
            sel = tree.selectedItems()
            if sel:
                nid = sel[0].data(0, Qt.ItemDataRole.UserRole)
                self.clear_peer_reliability(wallet, nid)
                status.setText(_("Reliability stats cleared for {}.").format(
                    self._abbrev(nid, 12, 6) or nid))
            else:
                self.clear_peer_reliability(wallet)
                status.setText(_("All channel-peer reliability stats cleared."))
            status.setStyleSheet("color: green;")
            repopulate()

        btn_row = QHBoxLayout()
        reset_btn = QPushButton(_("Reset stats"))
        reset_btn.setToolTip(_("Clear the selected peer's reliability history "
                               "(or all peers' if none is selected)."))
        reset_btn.clicked.connect(on_reset_stats)
        apply_btn = QPushButton(_("Apply"))
        apply_btn.clicked.connect(on_apply)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        v.addLayout(btn_row)

        return tab, repopulate

    def _reload_settings_fields(self, edits,
                                sync_toggle: Optional[Callable[[], None]] = None) -> None:
        """Re-read persisted config back into the editable Settings-tab fields, so
        any clamping/normalisation done on save is visible to the user. (The
        Advanced tab reloads its own fields via its repopulate().)"""
        c = self.config
        if sync_toggle is not None:
            sync_toggle()
        values = [
            str(c.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT),
            str(c.INBOUND_LIQUIDITY_MAX_CHANNELS),
            str(c.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT),
            str(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT),
            str(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT),
            str(c.INBOUND_LIQUIDITY_DEV_FEE_PCT),
        ]
        for (edit, _parser, _setter, _label), value in zip(edits, values):
            edit.setText(value)
