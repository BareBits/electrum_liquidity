# Qt GUI entry point: a persistent top-level "Liquidity" tab in the main
# window, with the settings on one sub-tab and the decision log (Actions /
# Declines) on two more. The automation itself lives in the base class
# (`__init__.py`); this file is purely the user-facing surface.
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from electrum.i18n import _
from electrum.plugin import hook

from electrum.gui.qt.util import read_QIcon

import asyncio
import logging
import re
import time

from . import (
    LiquidityPlugin, MAX_LOG_RETENTION_DAYS, DEV_FEE_MAX_PCT,
    DEV_FEE_PAYOUT_THRESHOLD_SAT, DEV_FEE_DAILY_CAP_SAT,
    DEFAULT_LOG_BUFFER_LINES, MAX_LOG_BUFFER_LINES, MIN_LOG_BUFFER_LINES,
    PLUGIN_OPENED_CHANNELS_DB_KEY, TERMINAL_STATUSES,
    _parse_npub_set, _parse_partner_list, _parse_banned_partners,
)
from .liquidity_manager import normalize_node_id
from .log_buffer import LEVEL_CHOICES, clamp_max_lines
from .qt_widgets import ToggleSwitch

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


# --- Electrum "Channels" tab: a "Managed by" column ------------------------
# The plugin drains outbound from channels; a user needs to see, on Electrum's
# own Channels tab, which channels the plugin opened (and so will manage) versus
# ones they opened by hand. Electrum's ChannelsList has no plugin hook for an
# extra column, so -- consistent with how this plugin already monkeypatches
# Electrum (it rebinds MIN_FUNDING_SAT and wraps lnworker methods) -- we extend
# the widget's column enum at load. The enum is a 0-based, contiguous IntEnum
# (see MyTreeView.BaseColumnsEnum); we rebuild it preserving every existing
# member's value and append MANAGED at the end, so all the existing
# ``items[self.Columns.X]`` index math keeps working. Idempotent and defensive:
# any failure leaves the stock Channels tab untouched.
def _managed_label_for_channel(wallet: 'Abstract_Wallet', chan) -> str:
    """"Plugin" if the plugin opened this channel, else "Manual". Read fresh from
    the wallet's persisted plugin-opened tag so it tracks new opens."""
    try:
        raw = wallet.db.get(PLUGIN_OPENED_CHANNELS_DB_KEY, [])
        ids = set(raw) if isinstance(raw, list) else set()
        return _("Plugin") if chan.channel_id.hex() in ids else _("Manual")
    except Exception:
        return ""


def _patch_channels_list_managed_column() -> bool:
    """Add a "Managed by" column to Electrum's Channels tab. Returns True once the
    class carries the column (already-patched is a no-op success)."""
    try:
        import enum
        from electrum.gui.qt.channels_list import ChannelsList
    except Exception:
        return False
    orig_cols = getattr(ChannelsList, "Columns", None)
    if orig_cols is None:
        return False
    if hasattr(orig_cols, "MANAGED"):
        return True  # already patched
    try:
        members = [(m.name, m.value) for m in orig_cols]
        members.append(("MANAGED", max(m.value for m in orig_cols) + 1))
        # Subclass the same BaseColumnsEnum via the functional API, preserving the
        # 0-based contiguous values so the widget's index math is unchanged.
        new_cols = orig_cols.__base__("Columns", members)
        orig_format_fields = ChannelsList.format_fields

        def format_fields(self, chan):
            fields = orig_format_fields(self, chan)
            fields[self.Columns.MANAGED] = _managed_label_for_channel(self.wallet, chan)
            return fields

        ChannelsList.Columns = new_cols
        ChannelsList.headers = {**ChannelsList.headers, new_cols.MANAGED: _("Managed by")}
        ChannelsList.format_fields = format_fields
        return True
    except Exception:
        return False


def _wrapped_label(text: str) -> QLabel:
    """A QLabel whose (often multi-paragraph) text wraps to the panel width
    instead of being clipped at the right edge."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


class _Signals(QObject):
    # NB: there is deliberately no "activity" signal. The plugin used to push
    # transient notices into Electrum's main status bar (next to the balance);
    # it no longer writes there at all. Everything it has to say now lives
    # inside the Liquidity tab: the Status footer for what a tick is doing, and
    # the Actions / Log sub-tabs for what it did.
    log_changed = pyqtSignal(object)          # (wallet,)
    providers_changed = pyqtSignal(object)    # (wallet,) -- discovered provider list refreshed
    status_changed = pyqtSignal(object, str)  # (wallet, tick status) -- from the asyncio thread


class _TabState:
    """Per-window UI handles for one open wallet's Liquidity tab.

    Built from the handles dict :meth:`Plugin._build_liquidity_tab` returns, so
    adding a sub-tab does not mean growing a positional tuple through three call
    sites. Unknown keys are simply set as attributes.
    """

    def __init__(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet',
                 container: QWidget, handles: Dict[str, object]) -> None:
        self.window = window
        self.wallet = wallet
        self.container = container
        # Declared for the type checker / readability; overwritten from handles.
        self.actions_tree: Optional[QTreeWidget] = None
        self.declines_tree: Optional[QTreeWidget] = None
        self.refresh: Callable[[], None] = lambda: None
        self.repopulate_providers: Callable[[], None] = lambda: None
        self.repopulate_partners: Callable[[], None] = lambda: None
        self.set_status: Callable[[str], None] = lambda status: None
        for key, value in handles.items():
            setattr(self, key, value)


def _fmt_sat(amount: Optional[int]) -> str:
    return f"{amount:,}" if isinstance(amount, int) else "—"


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(wallet: 'Abstract_Wallet') -> str:
    """The wallet's name reduced to a single safe path component, for the
    suggested filename of a saved log. A wallet name is user-controlled and can
    contain separators, so it never goes into a path unfiltered."""
    try:
        name = wallet.basename()
    except Exception:
        name = ""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name or "").strip("._")
    return cleaned or "wallet"


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
        # thread; the plugin's callbacks (asyncio thread) then emit to it via
        # queued connections.
        self.signals: Optional[_Signals] = None
        # One Liquidity tab per open wallet (Electrum opens one window/wallet).
        self._tabs: Dict['Abstract_Wallet', _TabState] = {}

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow') -> None:
        if self.signals is None:
            self.signals = _Signals()
            self.signals.log_changed.connect(self._on_log_changed_ui)
            self.signals.providers_changed.connect(self._on_providers_changed_ui)
            self.signals.status_changed.connect(self._on_status_changed_ui)
        self._add_liquidity_tab(window, wallet)
        # Add the "Managed by" column to Electrum's Channels tab (once, globally),
        # then refresh this window's already-built list so the column appears now.
        if _patch_channels_list_managed_column():
            try:
                window.channels_list.update_rows.emit(wallet)
            except Exception:
                self.logger.debug("could not refresh channels list after column patch")
        self.start_wallet(wallet)

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet') -> None:
        self.stop_wallet(wallet)
        self._remove_liquidity_tab(wallet)

    def requires_settings(self) -> bool:
        # Settings now live in the Liquidity tab rather than a settings dialog.
        return False

    # NB: on_action_done is deliberately NOT overridden here. The base class's
    # no-op is what we want: completed actions are recorded in the decision log
    # (Actions sub-tab) and the Log sub-tab, and the plugin no longer pushes
    # anything into Electrum's main status bar.

    def on_log_changed(self, wallet: 'Abstract_Wallet') -> None:
        # Called from the asyncio thread whenever the decision log grows; emit a
        # queued signal so an open tab can refresh on the GUI thread.
        if self.signals is not None:
            self.signals.log_changed.emit(wallet)

    def _on_log_changed_ui(self, wallet: 'Abstract_Wallet') -> None:
        state = self._tabs.get(wallet)
        if state is not None:
            state.refresh()

    def _on_providers_changed_ui(self, wallet: 'Abstract_Wallet') -> None:
        state = self._tabs.get(wallet)
        if state is not None:
            state.repopulate_providers()

    def on_status_changed(self, wallet: 'Abstract_Wallet', status: str) -> None:
        # Called from the asyncio thread on every tick step; emit a queued signal
        # so the Settings tab's Status section updates on the GUI thread.
        if self.signals is not None:
            self.signals.status_changed.emit(wallet, status)

    def _on_status_changed_ui(self, wallet: 'Abstract_Wallet', status: str) -> None:
        state = self._tabs.get(wallet)
        if state is None:
            return
        try:
            state.set_status(status)
        except RuntimeError:
            # The tab's widgets were deleted between the emit and its delivery
            # (close_wallet races a queued signal). Nothing to update.
            pass

    # --- tab lifecycle ----------------------------------------------------
    def _add_liquidity_tab(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet') -> None:
        if wallet in self._tabs:
            return
        container, handles = self._build_liquidity_tab(window, wallet)
        state = self._tabs[wallet] = _TabState(window, wallet, container, handles)
        state.refresh()
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

    # --- full log view ----------------------------------------------------
    # How often the Log tab looks for new lines. Captured logging arrives with no
    # Electrum event behind it, so the view has to poll -- but it polls a single
    # integer (the ring's revision counter) and only re-renders when that moved,
    # so an idle plugin costs one comparison twice a second and a debug firehose
    # cannot flood the Qt event loop with one repaint per record.
    _LOG_REFRESH_MS = 500

    @staticmethod
    def _decision_level(entry: Dict) -> int:
        """Map a decision-log entry onto a logging level so one level filter can
        govern both halves of the merged view. Faults are a warning by nature;
        the file-only "error" category is an error; decisions are informational."""
        category = str(entry.get("category") or "")
        if category == "error":
            return logging.ERROR
        if category == "fault":
            return logging.WARNING
        return logging.INFO

    @staticmethod
    def _fmt_log_row(ts: float, level: str, source: str, message: str) -> str:
        """One fixed-width row. Multi-line messages (tracebacks) get their
        continuation lines indented under the message column, so a row is still
        visually one event."""
        head = f"{_fmt_time(ts)}  {level:<8} {source:<34}  "
        lines = str(message).splitlines() or [""]
        pad = " " * 24
        return "\n".join([head + lines[0]] + [pad + line for line in lines[1:]])

    def _merged_log_rows(self, wallet: 'Abstract_Wallet', *, min_level: int,
                         needle: str) -> List[str]:
        """The Log tab's content: captured logging plus this wallet's decision
        log, in one chronological, filtered list.

        The captured half is process-global (a Python logger has no idea which
        wallet it is talking about), the decision half is per wallet -- which is
        exactly right: the decisions are this wallet's, the surrounding evidence
        is whatever the plugin was doing at the time.
        """
        rows: List[tuple] = []
        for line in self.log_buffer.snapshot(min_level=min_level):
            rows.append((line.ts, self._fmt_log_row(
                line.ts, line.level_name, line.source, line.message)))
        for entry in self.get_decision_log(wallet):
            if self._decision_level(entry) < min_level:
                continue
            source = f"decision/{entry.get('category', '')}"
            message = str(entry.get("reason") or "")
            kind = entry.get("kind")
            if kind:
                message = f"[{kind}] {message}"
            detail = entry.get("detail")
            if detail:
                message += f"\n{detail}"
            ts = float(entry.get("ts") or 0.0)
            rows.append((ts, self._fmt_log_row(ts, "DECISION", source, message)))
        rows.sort(key=lambda row: row[0])
        out = [text for _ts, text in rows]
        if needle:
            folded = needle.casefold()
            out = [text for text in out if folded in text.casefold()]
        return out

    def _build_log_tab(self, wallet: 'Abstract_Wallet'):
        """Build the Log sub-tab. Returns (widget, refresh_fn)."""
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.addWidget(_wrapped_label(_(
            "Everything this plugin logged since Electrum started, merged with "
            "this wallet's decision log. Held in memory only — nothing here is "
            "written to disk. Use \"Save to file…\" to keep a copy, and the "
            "capture options below to widen what is recorded or change how many "
            "lines are kept.")))

        controls = QHBoxLayout()
        controls.addWidget(QLabel(_("Level")))
        level_combo = QComboBox()
        for name, value in LEVEL_CHOICES:
            level_combo.addItem(_(name), value)
        level_combo.setCurrentIndex(len(LEVEL_CHOICES) - 1)   # everything, by default
        controls.addWidget(level_combo)
        controls.addWidget(QLabel(_("Filter")))
        filter_edit = QLineEdit()
        filter_edit.setPlaceholderText(_("substring, e.g. partner"))
        controls.addWidget(filter_edit, 1)
        follow_cb = QCheckBox(_("Follow"))
        follow_cb.setChecked(True)
        follow_cb.setToolTip(_("Keep the view scrolled to the newest line. Untick to read "
                               "back through the log while it keeps growing."))
        controls.addWidget(follow_cb)
        v.addLayout(controls)

        # --- capture options (moved here from the Advanced tab) --------------
        # These govern what lands in the view directly above them, so they belong
        # next to it rather than three tabs away. Applied immediately -- there is
        # no Apply button on this tab, and each one only re-configures the live
        # capture handler / ring buffer.
        #
        # The first two are separate switches on purpose: WHICH loggers to record,
        # and WHETHER to force debug level, are independent questions -- and only
        # the second has a side effect outside this plugin (see its tooltip).
        c = self.config
        capture_ln_cb = QCheckBox(_("Also capture Electrum Lightning logs"))
        capture_ln_cb.setToolTip(_("Include Electrum's own Lightning and swap logging (peer "
                                   "manager, node rater, channels, routing, submarine swaps) in "
                                   "this view. Useful when the plugin reports that no channel "
                                   "partner is available — that decision is made inside those "
                                   "subsystems. Noisy; off by default."))
        capture_ln_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_LN', False)))

        capture_debug_cb = QCheckBox(_("Capture debug-level logs"))
        capture_debug_cb.setToolTip(_("Force debug-level logging while this is on. Electrum "
                                      "normally produces debug records already (they are just "
                                      "hidden), so this matters only if you started Electrum with "
                                      "a reduced verbosity — in which case it also makes those "
                                      "records appear in Electrum's own log file. The previous "
                                      "setting is restored when you turn it off."))
        capture_debug_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG', False)))

        buffer_edit = QLineEdit(str(getattr(c, 'INBOUND_LIQUIDITY_LOG_BUFFER_LINES',
                                            DEFAULT_LOG_BUFFER_LINES)))
        buffer_edit.setMaximumWidth(90)
        buffer_edit.setToolTip(_("How many captured lines to keep in memory. Values outside "
                                 "{}–{} are clamped. Applied when you finish editing.").format(
            MIN_LOG_BUFFER_LINES, MAX_LOG_BUFFER_LINES))

        capture_row = QHBoxLayout()
        capture_row.addWidget(capture_ln_cb)
        capture_row.addWidget(capture_debug_cb)
        capture_row.addStretch(1)
        capture_row.addWidget(QLabel(_("Buffer (lines)")))
        capture_row.addWidget(buffer_edit)
        v.addLayout(capture_row)

        def _apply_capture() -> None:
            setattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_LN', capture_ln_cb.isChecked())
            setattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG', capture_debug_cb.isChecked())
            self.apply_log_capture_settings()

        def _apply_buffer() -> None:
            """Persist the buffer size, clamped. A non-numeric entry is rejected
            by snapping the box back to what is actually in force, so the field
            never shows a value the ring is not using."""
            try:
                setattr(c, 'INBOUND_LIQUIDITY_LOG_BUFFER_LINES',
                        clamp_max_lines(int(buffer_edit.text().strip())))
            except ValueError:
                pass
            self.apply_log_capture_settings()
            buffer_edit.setText(str(self.log_buffer_lines()))

        capture_ln_cb.toggled.connect(_apply_capture)
        capture_debug_cb.toggled.connect(_apply_capture)
        buffer_edit.editingFinished.connect(_apply_buffer)

        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        view.setFont(font)
        v.addWidget(view, 1)

        summary = QLabel("")
        summary.setStyleSheet("color: gray;")
        # Transient feedback for the buttons below. Separate from `summary`
        # because the refresh timer rewrites that one out from under it.
        feedback = QLabel("")
        feedback.setStyleSheet("color: gray;")

        copy_btn = QPushButton(_("Copy"))
        save_btn = QPushButton(_("Save to file…"))
        clear_btn = QPushButton(_("Clear"))
        clear_btn.setToolTip(_("Discard the captured log lines held in memory. Your decision "
                               "log (Actions / Declines / Faults) is not affected."))
        btn_row = QHBoxLayout()
        btn_row.addWidget(summary, 1)
        btn_row.addWidget(feedback)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(clear_btn)
        v.addLayout(btn_row)

        # Cheap change-detection: re-render only when the ring moved, the decision
        # log grew, or the user changed a filter. `-1` forces the first render.
        seen = {"revision": -1, "decisions": -1, "level": None, "needle": None}

        def _current_rows() -> List[str]:
            return self._merged_log_rows(
                wallet,
                min_level=int(level_combo.currentData() or 0),
                needle=filter_edit.text().strip())

        def refresh(force: bool = False) -> None:
            try:
                revision = self.log_buffer.revision
                decisions = len(self.get_decision_log(wallet))
                level = level_combo.currentData()
                needle = filter_edit.text().strip()
                if not force and (revision, decisions, level, needle) == (
                        seen["revision"], seen["decisions"], seen["level"], seen["needle"]):
                    return
                seen.update(revision=revision, decisions=decisions,
                            level=level, needle=needle)
                rows = _current_rows()
                bar = view.verticalScrollBar()
                at_bottom = follow_cb.isChecked()
                previous = bar.value()
                view.setPlainText("\n".join(rows))
                bar.setValue(bar.maximum() if at_bottom else min(previous, bar.maximum()))
                stats = self.log_buffer.stats()
                summary.setText(_("{} lines shown · {} captured (limit {}) · {} dropped").format(
                    f"{len(rows):,}", f"{stats['count']:,}",
                    f"{stats['max_lines']:,}", f"{stats['dropped']:,}"))
            except RuntimeError:
                # Widgets deleted underneath us (tab torn down mid-timer).
                pass

        def _force_refresh() -> None:
            refresh(force=True)

        def _refresh_all() -> None:
            """The refresh the outer tab calls: re-read the capture options from
            config (something else may have changed them) as well as the view."""
            for cb, attr in ((capture_ln_cb, 'INBOUND_LIQUIDITY_LOG_CAPTURE_LN'),
                             (capture_debug_cb, 'INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG')):
                cb.blockSignals(True)
                cb.setChecked(bool(getattr(c, attr, False)))
                cb.blockSignals(False)
            if not buffer_edit.hasFocus():      # don't fight a half-typed value
                buffer_edit.setText(str(self.log_buffer_lines()))
            refresh(force=True)

        level_combo.currentIndexChanged.connect(_force_refresh)
        filter_edit.textChanged.connect(_force_refresh)
        follow_cb.toggled.connect(_force_refresh)

        def on_copy() -> None:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(view.toPlainText())
                feedback.setStyleSheet("color: gray;")
                feedback.setText(_("Copied to clipboard."))

        def on_save() -> None:
            default = f"inbound_liquidity_{_safe_filename(wallet)}_{int(time.time())}.log"
            path, _sel = QFileDialog.getSaveFileName(tab, _("Save log"), default)
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(view.toPlainText())
            except OSError as e:
                feedback.setStyleSheet("color: red;")
                feedback.setText(_("Could not save: {}").format(e))
                return
            feedback.setStyleSheet("color: gray;")
            feedback.setText(_("Saved to {}").format(path))

        def on_clear() -> None:
            self.log_buffer.clear()
            feedback.setText("")
            refresh(force=True)

        copy_btn.clicked.connect(on_copy)
        save_btn.clicked.connect(on_save)
        clear_btn.clicked.connect(on_clear)

        # Parented to the tab, so it stops (and is destroyed) with it -- a timer
        # outliving its widgets would fire into deleted C++ objects.
        timer = QTimer(tab)
        timer.setInterval(self._LOG_REFRESH_MS)
        timer.timeout.connect(refresh)
        timer.start()

        refresh(force=True)
        return tab, _refresh_all

    # --- tab construction -------------------------------------------------
    def _build_liquidity_tab(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet'):
        """Build the top-level Liquidity tab widget and return
        ``(container, handles)`` — the handles dict becomes the :class:`_TabState`
        attributes the plugin later calls back into (refresh, set_status, …)."""
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
            # blockSignals also muted the toggled signal that slides the knob, so
            # snap it to the freshly-read state -- otherwise the knob can lag the
            # label (e.g. a green ENABLED over a knob still parked off on first
            # show).
            toggle.sync_knob()
            _sync_toggle_label()

        def on_toggle(checked: bool) -> None:
            setattr(c, 'INBOUND_LIQUIDITY_AUTOMATION_ENABLED', bool(checked))
            _sync_toggle_label()
            if checked:
                # Start acting on current state right away rather than waiting
                # for the next wallet event.
                self.request_evaluation(wallet)
            # No notice needed: the ENABLED/DISABLED label beside the switch
            # already says what happened, and the Status footer picks up the
            # tick this just kicked off.

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

        # --- Status (widgets built here, placed at the BOTTOM of the tab) ----
        # One evaluation can run for minutes (a nostr provider session, a channel
        # open walking its candidate list, a reverse swap), and from outside that
        # is indistinguishable from a plugin doing nothing. This says which step
        # is running right now, and "sleeping" once the tick is done.
        #
        # The widgets are created here, before the settings fields, because the
        # refresh/handles closures below capture ``_set_tick_status``; they are
        # added to the layout at the very end (see "status footer"), so the
        # section renders as a footer under the Apply button rather than pushing
        # the settings down.
        status_header = QLabel(_("Status"))
        _sf = status_header.font()
        _sf.setBold(True)
        status_header.setFont(_sf)

        tick_status_label = _wrapped_label("")
        tick_since_label = QLabel("")
        tick_since_label.setStyleSheet("color: gray;")

        def _set_tick_status(status: str) -> None:
            """Render one tick status. Terminal states are muted, an in-flight
            step is emphasised, so a glance tells you whether anything is
            happening without reading the words."""
            tick_status_label.setText(status)
            resting = status in TERMINAL_STATUSES
            tick_status_label.setStyleSheet(
                "color: gray;" if resting else "color: #2ea043; font-weight: bold;")
            tick_since_label.setText(
                "" if resting else _("since {}").format(_fmt_time(time.time())))

        _set_tick_status(self.tick_status(wallet))

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

        # --- scope switch: which channels the plugin may touch ---------------
        # On the main tab (not Advanced) because it answers the first question a
        # new user has -- "will this thing touch the channel I set up myself?" --
        # and it defaults ON, so the answer is no until they say otherwise.
        # Applied immediately, like the switches above: it changes what the very
        # next tick is allowed to do, rather than being a tunable awaiting Apply.
        plugin_only_cb = QCheckBox(_("Only manage channels the plugin opened"))
        plugin_only_cb.setToolTip(_(
            "When on (the default), the plugin only reverse-swaps channels it "
            "opened itself; a channel you opened by hand is left entirely alone "
            "and its outbound is never drained. Turn it off to let the plugin "
            "manage every channel in this wallet."))
        plugin_only_cb.setChecked(
            bool(getattr(c, 'INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY', True)))

        def on_plugin_only(checked: bool) -> None:
            setattr(c, 'INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY', bool(checked))

        plugin_only_cb.toggled.connect(on_plugin_only)
        vbox.addWidget(plugin_only_cb)

        run_now_btn = QPushButton(_("Run now"))
        run_now_btn.setToolTip(_(
            "Evaluate once right now and take any warranted action, regardless of "
            "the \"Manual run only\" setting, and without waiting for the startup "
            "window that automatic runs observe. Requires the Automation switch to "
            "be enabled, a server connection, and a fully synced wallet."))

        def on_run_now() -> None:
            self.request_evaluation(wallet, manual=True)
            # The Status footer shows the run starting, so there is nothing
            # useful to announce separately.

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

        # --- status footer -------------------------------------------------
        # The stretch goes FIRST so the section is pushed to the bottom edge of
        # the tab and stays there as the panel is resized, rather than floating
        # directly under the Apply button on a tall window.
        vbox.addStretch(1)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        vbox.addWidget(separator)
        vbox.addWidget(status_header)
        vbox.addWidget(tick_status_label)
        vbox.addWidget(tick_since_label)
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

        # --- Full log sub-tab (last: it is the deepest / noisiest view) ----
        log_tab, refresh_log = self._build_log_tab(wallet)
        tabs.addTab(log_tab, _("Log"))

        def refresh() -> None:
            _sync_toggle_from_config()
            # Re-read the manual-run-only checkbox without re-firing its handler.
            manual_only_cb.blockSignals(True)
            manual_only_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_MANUAL_RUN_ONLY', False)))
            manual_only_cb.blockSignals(False)
            plugin_only_cb.blockSignals(True)
            plugin_only_cb.setChecked(
                bool(getattr(c, 'INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY', True)))
            plugin_only_cb.blockSignals(False)
            _set_tick_status(self.tick_status(wallet))
            self._populate_log_tree(actions_tree, self.get_decision_log(wallet, "action"))
            self._populate_log_tree(declines_tree, self.get_decision_log(wallet, "decline"))
            self._populate_log_tree(faults_tree, self.get_decision_log(wallet, "fault"))
            _refresh_dev_fee_label()
            repopulate_providers()
            repopulate_partners()
            repopulate_advanced()
            refresh_log()

        return container, {
            "actions_tree": actions_tree,
            "declines_tree": declines_tree,
            "faults_tree": faults_tree,
            "log_tab": log_tab,
            "refresh": refresh,
            "refresh_log": refresh_log,
            "repopulate_providers": repopulate_providers,
            "repopulate_partners": repopulate_partners,
            "set_status": _set_tick_status,
        }

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

        # NB: the scope switch ("Only manage channels the plugin opened") lives on
        # the main Settings tab, and the Log-tab capture switches live on the Log
        # tab itself — each next to what it affects.
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
            (_("Keep outbound per channel (sat, 0 = drain all)"),
             'INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT', max(0, int(val)))),
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
            "Channels are opened to peers Electrum suggests by default (several are "
            "tried in turn if one refuses). List “Preferred” partners "
            "(node_id@host:port, one per line) to try them first, in order, before "
            "falling back to those suggestions. “Banned” partners (node id, or "
            "node_id@host:port) are never opened to.")))
        v.addWidget(_wrapped_label(_(
            "The rows below are the peers you ALREADY have a channel with (plus any "
            "with a fault history) — they are not a list of candidates. Tick a row to "
            "prefer/ban that node; a node you are not connected to yet goes in the "
            "“Preferred partners” box.")))

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
