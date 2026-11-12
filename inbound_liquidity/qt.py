# Qt GUI entry point: a persistent top-level "Liquidity" tab in the main
# window, with the settings on one sub-tab and the decision log (Actions /
# Declines) on two more. The automation itself lives in the base class
# (`__init__.py`); this file is purely the user-facing surface.
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QRadioButton, QScrollArea, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from electrum.i18n import _
from electrum.plugin import hook

from electrum.gui.qt.util import read_QIcon
from electrum.gui.qt.amountedit import AmountEdit, BTCAmountEdit

import asyncio
import contextlib
import html
import logging
import re
import time

from . import (
    LiquidityPlugin, MAX_LOG_RETENTION_DAYS, DEV_FEE_MAX_PCT,
    DEV_FEE_PAYOUT_THRESHOLD_SAT, DEV_FEE_DAILY_CAP_SAT,
    DEFAULT_LOG_BUFFER_LINES, MAX_LOG_BUFFER_LINES, MIN_LOG_BUFFER_LINES,
    DEFAULT_MAX_CHANNEL_SIZE_SAT,
    PLUGIN_OPENED_CHANNELS_DB_KEY, is_terminal_status,
    _parse_npub_set, _parse_partner_list, _parse_banned_partners,
)
from .liquidity_manager import (
    BufferSplit, normalize_node_id, scrub_text, split_goal_buffer,
)
from .log_buffer import LEVEL_CHOICES, LogCapture, clamp_max_lines
from .qt_widgets import (
    SortableHeader, SortableTreeItem, ToggleSwitch, add_sortable_row,
    primary_button, rows_in_insertion_order,
)

if TYPE_CHECKING:
    from electrum.gui.qt.main_window import ElectrumWindow
    from electrum.wallet import Abstract_Wallet


# How long to leave a locked wallet alone after the user dismisses the unlock
# prompt. Automation re-evaluates on the heartbeat (10 min) and on wallet
# activity, and every one of those ticks re-reports "locked" -- so without a
# cooldown, "not now" would be answered by another dialog minutes later. An hour
# is long enough not to nag and short enough that a user who meant to unlock is
# reminded the same day. The prompt is NOT suppressed permanently: automation
# silently doing nothing is exactly the state this whole gate exists to make
# visible.
UNLOCK_PROMPT_COOLDOWN_SEC = 3600.0


# How long teardown may block silently before the "finishing a swap" dialog is
# put up. The overwhelmingly common close has no attempt in flight at all and
# returns in microseconds; the next most common has one that is nearly done.
# Flashing a dialog for either would be worse than showing nothing, so the wait
# starts silent and only becomes visible once it is clearly going to be a wait.
SHUTDOWN_DIALOG_DELAY_SEC = 2.0

# How often the shutdown wait re-paints and re-checks. Small enough that the
# countdown looks live, large enough not to spin the GUI thread.
SHUTDOWN_POLL_INTERVAL_SEC = 0.1


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


# --- Electrum's balance pie chart: a "liquidity goal buffer" slice ---------
# The user's liquidity goal is sats they intend NOT to spend, but Electrum draws
# them as ordinary balance -- so the chart says "you have 1M" when 100k of it is
# spoken for. We split a slice off for the buffer: out of on-chain first (that is
# what funds a channel open), spilling into Lightning for whatever on-chain
# cannot cover.
#
# Electrum offers no hook for this, so -- as with the Channels column above -- we
# monkeypatch, but only by WRAPPING: each patch lets Electrum build its own
# entry list exactly as it always did, then rewrites the finished list. Nothing
# about how the chart is computed or painted is duplicated here, so an Electrum
# that changes its slices keeps working; we only ever look for the one entry we
# need and leave the rest untouched.
#
# Both patches are installed once, globally, and read the live buffer through
# ``_BUFFER_PROVIDER`` rather than capturing a plugin instance: a class patch
# outlives any one wallet, and a stale captured reference would keep a closed
# wallet's plugin alive (and keep reserving against it). A None provider -- the
# plugin unloaded -- means every patch falls through to stock behaviour.

# The blues asked for. NOT ColorScheme.BLUE, which Electrum already spends on the
# "Frozen" slice (a pale #8cb3f2): two blues in one pie is two slices the user
# cannot tell apart. These are a lighter, more saturated blue and a deep blue,
# both distinct from frozen-blue, from cyan (frozen Lightning) and from green
# (on-chain) in Electrum's light and dark themes alike.
#
# Two shades rather than two unrelated hues because the two wedges are one thing
# split by where it is held: the goal reads as a single blue region of the pie,
# and the shade says which rail is backing it.
COLOR_GOAL_BUFFER_ONCHAIN = QColor(64, 148, 255)
COLOR_GOAL_BUFFER_LIGHTNING = QColor(20, 80, 180)

# Both goal wedges, for the "is this list one of ours?" checks.
COLOR_GOAL_BUFFER_ALL = (COLOR_GOAL_BUFFER_ONCHAIN, COLOR_GOAL_BUFFER_LIGHTNING)


def _goal_buffer_onchain_label() -> str:
    """The on-chain goal slice's label, for both the pie entry and the Balance
    dialog legend.

    A function rather than a module constant so the string is translated at the
    moment it is shown -- a constant would bake in whatever language was active
    when this module was first imported.
    """
    return _("Liquidity goal buffer (on-chain)")


def _goal_buffer_lightning_label() -> str:
    """The Lightning goal slice's label. Only ever shown when the goal actually
    reaches into Lightning -- see :func:`_entries_with_goal_slices`."""
    return _("Liquidity goal buffer (Lightning)")


def _show_goal_checkbox_label() -> str:
    return _("Show liquidity goal")


def _show_goal_checkbox_tooltip() -> str:
    return _("Split the sats reserved for your liquidity goal out of the balance "
             "as their own slices, on this chart and in the status bar. "
             "Unchecked, Electrum's own chart is shown instead.\n\n"
             "Display only: it changes nothing about what the plugin reserves.")

# Callable[[Abstract_Wallet], int] -- the live plugin's goal_buffer_sat, or None
# when no plugin is loaded. Set by Plugin.load_wallet, cleared by close_wallet.
_BUFFER_PROVIDER: Optional[Callable[['Abstract_Wallet'], int]] = None

# Fired from the patched update_status so the Send tab's warning tracks the
# BALANCE, not just the amount field. The warning threshold is
# "total balance - buffer", so a label refreshed only on keystrokes would go
# stale the moment a payment arrived while the user sat on the Send tab.
_SEND_WARNING_REFRESH: Optional[Callable[[], None]] = None


class _GoalVisibility:
    """Reader and writer for the "show the liquidity goal on the chart"
    checkbox, which is persisted per wallet under
    ``INBOUND_LIQUIDITY_SHOW_GOAL_IN_PIECHART``.

    One object rather than two module globals so the getter and the setter can
    never be installed out of step -- a chart that could be read but not written
    would give the user a checkbox that forgets itself.

    Both take the wallet whose chart is being drawn, because the setting is that
    wallet's: with two wallets open, unticking the box on one must not strip the
    slices off the other's chart.
    """

    def __init__(self, get: Callable[['Abstract_Wallet'], bool],
                 set_: Callable[['Abstract_Wallet', bool], None]) -> None:
        self._get = get
        self._set = set_

    def get(self, wallet: 'Abstract_Wallet') -> bool:
        return bool(self._get(wallet))

    def set(self, wallet: 'Abstract_Wallet', show: bool) -> None:
        self._set(wallet, bool(show))


# Set by Plugin.load_wallet, cleared by close_wallet -- same lifetime, and for
# the same reason, as _BUFFER_PROVIDER above.
_GOAL_VISIBILITY: Optional[_GoalVisibility] = None


def _set_buffer_provider(provider: Optional[Callable[['Abstract_Wallet'], int]]) -> None:
    global _BUFFER_PROVIDER
    _BUFFER_PROVIDER = provider


def _set_send_warning_refresher(refresher: Optional[Callable[[], None]]) -> None:
    global _SEND_WARNING_REFRESH
    _SEND_WARNING_REFRESH = refresher


def _set_goal_visibility(visibility: Optional[_GoalVisibility]) -> None:
    global _GOAL_VISIBILITY
    _GOAL_VISIBILITY = visibility


def _goal_is_shown(wallet: 'Abstract_Wallet') -> bool:
    """Whether the goal slices are wanted on the chart right now.

    Defaults to True when nothing is installed or the read fails -- that is the
    ConfigVar's own default, and it keeps a wallet with no plugin loaded (where
    the buffer is 0 anyway, so there are no slices to show) behaving exactly as
    an unchecked box would. Never raises: this runs inside a status-bar repaint.
    """
    visibility = _GOAL_VISIBILITY
    if visibility is None:
        return True
    try:
        return visibility.get(wallet)
    except Exception:
        return True


def _set_goal_shown(wallet: 'Abstract_Wallet', show: bool) -> None:
    """Persist the checkbox. A failure here costs the user the *memory* of their
    choice, not the choice itself -- the chart is repainted either way."""
    visibility = _GOAL_VISIBILITY
    if visibility is None:
        return
    try:
        visibility.set(wallet, show)
    except Exception:
        pass


def _buffer_split_for(wallet: 'Abstract_Wallet', *, onchain_sat: int,
                      lightning_sat: int) -> BufferSplit:
    """The buffer split for one wallet, or an all-zero split when no plugin is
    loaded / the wallet is unmanaged / the goal is 0. Never raises: this runs
    inside Electrum's status-bar repaint."""
    provider = _BUFFER_PROVIDER
    buffer_sat = 0
    if provider is not None:
        try:
            buffer_sat = int(provider(wallet))
        except Exception:
            buffer_sat = 0
    return split_goal_buffer(onchain_sat=onchain_sat, lightning_sat=lightning_sat,
                             buffer_sat=buffer_sat)


# Where the status-bar button keeps Electrum's own entry list, so a rewrite can
# always start from the stock slices rather than from its own last output.
_STOCK_ENTRIES_ATTR = "_inbound_liquidity_stock_entries"


def _carries_buffer_slice(entries) -> bool:
    """Whether this entry list is one we already rewrote."""
    return any(color in COLOR_GOAL_BUFFER_ALL for _name, color, _amount in entries)


def _balance_slice_colors():
    """Electrum's On-chain and Lightning slice colours, or None if its module
    moved.

    Matching is by COLOR, not by the translated slice name: the names are
    user-language strings, so a German Electrum would silently never match. The
    colours are module constants in Electrum's balance_dialog and are what the
    chart is actually keyed on.

    Looked up at call time rather than at import so a failure degrades one
    repaint to the stock chart instead of breaking the whole plugin's GUI.
    """
    try:
        from electrum.gui.qt.balance_dialog import COLOR_CONFIRMED, COLOR_LIGHTNING
        return COLOR_CONFIRMED, COLOR_LIGHTNING
    except Exception:
        return None


def _goal_buffer_split(entries, wallet: 'Abstract_Wallet') -> Optional[BufferSplit]:
    """The buffer split implied by Electrum's ``[(name, color, amount)]`` pie
    entries, or None when there is no buffer worth drawing.

    Deliberately independent of the checkbox: the Balance dialog has to know a
    goal EXISTS even while it is hidden, or the checkbox that would unhide it
    could never be offered -- and a user who unticked it once would have no way
    back.
    """
    colors = _balance_slice_colors()
    if colors is None:
        return None
    color_confirmed, color_lightning = colors
    onchain_sat = 0
    lightning_sat = 0
    for _name, color, amount in entries:
        if color == color_confirmed:
            onchain_sat = int(amount)
        elif color == color_lightning:
            lightning_sat = int(amount)
    split = _buffer_split_for(wallet, onchain_sat=onchain_sat,
                              lightning_sat=lightning_sat)
    return split if split.total() > 0 else None


def _entries_with_goal_slices(entries, split: BufferSplit):
    """``entries`` with ``split`` carved out of the On-chain and Lightning
    slices and re-added as its own wedges. Returns the input unchanged (same
    object) if the slice colours could not be looked up.

    The goal gets TWO wedges, one per rail, so the chart says not just how much
    is spoken for but where it is currently held -- an on-chain wedge is sats
    that can fund a channel open today, a Lightning wedge is sats that would
    have to be swapped out first. The Lightning wedge is omitted entirely when
    on-chain covers the whole goal, which is the ordinary case: a zero-width
    wedge is invisible anyway, and its legend row would read "0".
    """
    colors = _balance_slice_colors()
    if colors is None:
        return entries
    color_confirmed, color_lightning = colors
    out = []
    for name, color, amount in entries:
        if color == color_confirmed:
            out.append((name, color, split.onchain_remaining))
        elif color == color_lightning:
            out.append((name, color, split.lightning_remaining))
        else:
            out.append((name, color, amount))
    # Appended last so the existing slices keep the order (and therefore the
    # start angles) Electrum gave them; the goal takes the wedges at the end.
    for label, color, amount in _goal_slices(split):
        out.append((label, color, amount))
    return out


def _goal_slices(split: BufferSplit) -> List[Tuple[str, QColor, int]]:
    """The goal's own ``(label, color, amount)`` entries, on-chain first, with
    empty rails left out. Shared by the pie and the dialog's legend so the two
    can never disagree about which rails are on screen."""
    slices: List[Tuple[str, QColor, int]] = []
    if split.from_onchain > 0:
        slices.append((_goal_buffer_onchain_label(), COLOR_GOAL_BUFFER_ONCHAIN,
                       split.from_onchain))
    if split.from_lightning > 0:
        slices.append((_goal_buffer_lightning_label(), COLOR_GOAL_BUFFER_LIGHTNING,
                       split.from_lightning))
    return slices


def _rewrite_piechart_entries(entries, wallet: 'Abstract_Wallet'):
    """Take Electrum's ``[(name, color, amount)]`` pie entries and return a new
    list with the goal buffer split out of the On-chain and Lightning slices.

    Returns the input unchanged (same object) when there is no buffer to show, or
    when the user has unticked "Show liquidity goal" -- so both the common case
    and the switched-off case cost no allocation, and the caller's ``is`` check
    is enough to decide whether anything needs repainting.
    """
    if not _goal_is_shown(wallet):
        return entries
    split = _goal_buffer_split(entries, wallet)
    if split is None:
        return entries
    return _entries_with_goal_slices(entries, split)


def _patch_status_bar_piechart() -> bool:
    """Add the buffer slice to the pie in Electrum's status bar.

    Wraps ``ElectrumWindow.update_status``: the original runs and populates the
    BalanceToolButton exactly as usual, then we rewrite the list it just stored.
    Deliberately NOT a reimplementation of update_status -- that method decides
    between several network states and only paints a pie in one of them, and
    duplicating that condition would be a bug waiting for the next Electrum.

    When Electrum is showing its low-reserve warning the button draws a warning
    icon instead of a pie, so there is nothing to restyle and we leave it alone.
    """
    try:
        from electrum.gui.qt.main_window import ElectrumWindow
    except Exception:
        return False
    if getattr(ElectrumWindow.update_status, "_inbound_liquidity_patched", False):
        return True
    orig_update_status = ElectrumWindow.update_status

    def update_status(self) -> None:
        orig_update_status(self)
        try:
            button = getattr(self, "balance_label", None)
            current = getattr(button, "_list", None)
            if current and not getattr(button, "_warning", False):
                # Only the CONNECTED branch of update_status rebuilds the entry
                # list; offline / not-connected / synchronizing all leave
                # whatever was on the button. So on those passes what we are
                # handed is our own previous output -- and rewriting that would
                # subtract the buffer from an already-reduced on-chain slice and
                # append a second wedge, shrinking the pie a little more on every
                # repaint. Re-derive from the stock list stashed alongside it.
                stock = (getattr(button, _STOCK_ENTRIES_ATTR, None)
                         if _carries_buffer_slice(current) else current)
                if stock is not None:
                    rewritten = _rewrite_piechart_entries(stock, self.wallet)
                    # Compared by VALUE, not identity: re-deriving builds a new
                    # list every pass, and repainting an unchanged chart on every
                    # status tick is wasted work. This is also what puts the
                    # stock slices back when the goal is set to 0 -- the rewrite
                    # then returns the stock list, which differs from the
                    # buffered one still on the button.
                    if rewritten != current:
                        setattr(button, _STOCK_ENTRIES_ATTR, stock)
                        button.update_list(rewritten, getattr(button, "_warning", False))
        except Exception:
            # A repaint must never take the main window down.
            pass
        # The balance just moved, so the Send tab's threshold did too.
        refresher = _SEND_WARNING_REFRESH
        if refresher is not None:
            try:
                refresher()
            except Exception:
                pass

    update_status._inbound_liquidity_patched = True
    # Kept reachable so a caller can still get at Electrum's own implementation
    # once the class attribute is ours -- the assumption guards read its source
    # to notice if Electrum ever stops building the pie the way we match on.
    update_status._inbound_liquidity_orig = orig_update_status
    ElectrumWindow.update_status = update_status
    return True


def _restate_legend_amount(grid: QGridLayout, color, amount_sat: int,
                           config, fx) -> bool:
    """Rewrite the Balance dialog legend row whose swatch is ``color`` to read
    ``amount_sat``. Returns whether a row was found and changed.

    The row is located by its ``LegendWidget``'s colour rather than by index or
    by label text: Electrum only emits a row for a balance that is non-zero, so
    row numbers shift with the wallet, and the labels are translated.

    Column layout is Electrum's: 0 swatch, 1 name, 2 amount, 3 fiat.
    """
    try:
        from electrum.gui.qt.balance_dialog import LegendWidget
    except Exception:
        return False
    for row in range(grid.rowCount()):
        swatch = grid.itemAtPosition(row, 0)
        widget = swatch.widget() if swatch is not None else None
        if not isinstance(widget, LegendWidget) or widget.color != color:
            continue
        amount_item = grid.itemAtPosition(row, 2)
        if amount_item is None or amount_item.widget() is None:
            return False
        amount_item.widget().setText(config.format_amount_and_units(amount_sat))
        fiat_item = grid.itemAtPosition(row, 3)
        if fiat_item is not None and fiat_item.widget() is not None and fx:
            fiat_item.widget().setText(fx.format_amount_and_units(amount_sat))
        return True
    return False


def _add_goal_legend_rows(grid: QGridLayout, split: BufferSplit, config, fx,
                          ) -> List[List[QWidget]]:
    """Append one legend row per goal rail and return the rows' widgets, so the
    checkbox can hide and re-show them without rebuilding the dialog.

    Rows start below every stock row: ``rowCount()`` is the grid's own idea of
    its extent, so this cannot land on top of an existing legend entry however
    many of them Electrum decided to show.
    """
    try:
        from electrum.gui.qt.balance_dialog import LegendWidget
        from electrum.gui.qt.util import AmountLabel
    except Exception:
        return []
    rows: List[List[QWidget]] = []
    for label, color, amount in _goal_slices(split):
        row = grid.rowCount()
        fiat_str = fx.format_amount_and_units(amount) if fx else ''
        widgets = [
            LegendWidget(color),
            QLabel(label + ':'),
            AmountLabel(config.format_amount_and_units(amount)),
            AmountLabel(fiat_str),
        ]
        grid.addWidget(widgets[0], row, 0)
        grid.addWidget(widgets[1], row, 1)
        grid.addWidget(widgets[2], row, 2, alignment=Qt.AlignmentFlag.AlignRight)
        grid.addWidget(widgets[3], row, 3, alignment=Qt.AlignmentFlag.AlignRight)
        rows.append(widgets)
    return rows


def _install_goal_view(dialog, wallet: 'Abstract_Wallet') -> bool:
    """Amend an already-built Wallet Balance dialog with the goal slices and the
    checkbox that turns them off. Returns whether anything was added.

    The dialog is built in full by Electrum first and only then amended, so
    nothing about how the chart is computed or painted is duplicated here.
    """
    try:
        from electrum.gui.qt.balance_dialog import PieChartWidget
    except Exception:
        return False
    piechart = dialog.findChild(PieChartWidget)
    if piechart is None:
        return False
    # Electrum's own entries, kept as the reference rendering: unticking the box
    # has to restore exactly this, and re-ticking it has to re-derive from it
    # rather than from an already-reduced list.
    stock = list(getattr(piechart, "_list", None) or [])
    if not stock:
        return False
    # Read regardless of the checkbox: a goal that exists but is hidden still
    # needs its checkbox on screen, or there would be no way to bring it back.
    split = _goal_buffer_split(stock, wallet)
    if split is None:
        return False
    grid = dialog.findChild(QGridLayout)
    if grid is None:
        return False
    config = dialog.config
    fx = getattr(dialog, "fx", None)
    goal_rows = _add_goal_legend_rows(grid, split, config, fx)

    def render(show: bool) -> None:
        entries = _entries_with_goal_slices(stock, split) if show else stock
        piechart.update_list(entries)
        # The legend rows have to follow the wedges. Electrum builds them
        # straight from the raw PiechartBalance, so without this the dialog
        # would show a shrunken on-chain wedge beside a legend still quoting the
        # full on-chain balance -- and the rows would no longer add up to the
        # total. Rewritten in place, keyed on the swatch's colour (the labels
        # are translated). Slicing to len(stock) drops the appended goal wedges,
        # which have legend rows of their own.
        for _name, color, amount in entries[:len(stock)]:
            _restate_legend_amount(grid, color, amount, config, fx)
        for widgets in goal_rows:
            for widget in widgets:
                widget.setVisible(show)

    checkbox = QCheckBox(_show_goal_checkbox_label())
    checkbox.setToolTip(_show_goal_checkbox_tooltip())
    # Checked BEFORE the signal is connected: setChecked would otherwise write
    # the stored setting straight back and repaint the status bar just from
    # opening the dialog.
    checkbox.setChecked(_goal_is_shown(wallet))

    def on_toggled(show: bool) -> None:
        _set_goal_shown(wallet, show)
        render(show)
        # The status-bar pie is a different widget built by a different patch,
        # and the user asked for one consistent view -- so push it through
        # Electrum's own refresh rather than reaching into the button.
        try:
            dialog.window.update_status()
        except Exception:
            pass

    checkbox.toggled.connect(on_toggled)
    layout = dialog.layout()
    if layout is not None and hasattr(layout, "insertWidget"):
        index = layout.indexOf(piechart)
        layout.insertWidget(index + 1 if index >= 0 else 0, checkbox,
                            alignment=Qt.AlignmentFlag.AlignHCenter)
    render(checkbox.isChecked())
    return True


def _patch_balance_dialog() -> bool:
    """Add the goal slices, their legend rows and the show/hide checkbox to the
    Wallet Balance dialog.

    Wraps ``BalanceDialog.__init__``: the dialog builds itself in full, then we
    find the pie widget by type and the legend by layout type and amend both.
    ``findChild`` rather than an index walk, so re-ordering the dialog's layout
    does not silently put the legend row in the wrong place.
    """
    try:
        from electrum.gui.qt.balance_dialog import BalanceDialog
    except Exception:
        return False
    if getattr(BalanceDialog.__init__, "_inbound_liquidity_patched", False):
        return True
    orig_init = BalanceDialog.__init__

    def __init__(self, parent, *, wallet, **kwargs) -> None:
        orig_init(self, parent, wallet=wallet, **kwargs)
        try:
            _install_goal_view(self, wallet)
        except Exception:
            # An amended legend is a nicety; a dialog that cannot open is not.
            pass

    __init__._inbound_liquidity_patched = True
    __init__._inbound_liquidity_orig = orig_init
    BalanceDialog.__init__ = __init__
    return True


def _patch_balance_piechart() -> bool:
    """Install both pie-chart patches. True only if BOTH took, so a caller can
    tell "the chart now shows the buffer" from "it silently does not"."""
    status_ok = _patch_status_bar_piechart()
    dialog_ok = _patch_balance_dialog()
    return bool(status_ok and dialog_ok)


# --- Electrum's Send tab: the inline liquidity-buffer warning --------------
# Deliberately an inline label and nothing else. No dialog, no confirmation, no
# extra click: the warning appears under the Amount field as the user types past
# what they can spend and disappears when they come back under it. It never
# blocks the send and never interposes itself between the Pay button and the
# payment -- it is the user's money, and the buffer is a goal they set, not a
# rule they agreed to be held to.
#
# It also covers BOTH payment rails for free, because on-chain and Lightning
# sends are entered into the same ``amount_e`` widget. Intercepting the send
# calls instead would have meant patching two methods and un-picking the
# Lightning-falls-back-to-on-chain path that calls one from the other.
#
# The row the label lands on: Electrum's send grid uses rows 0-3 (pay-to,
# description, comment, amount) and row 6 (the buttons), leaving 4 and 5 free.
# Row 4 puts the warning directly under the amount it is about, which is where it
# has to be to read as being about that amount -- but a future Electrum may claim
# that row, and two widgets in one QGridLayout cell overlap rather than error. So
# the row is checked for occupants first and we fall to the bottom if it is taken.
SEND_WARNING_GRID_ROW = 4


def _free_grid_row(grid: QGridLayout, preferred_row: int) -> int:
    """``preferred_row`` if every cell in it is empty, else a fresh row past the
    end of the grid."""
    try:
        for col in range(max(1, grid.columnCount())):
            if grid.itemAtPosition(preferred_row, col) is not None:
                return grid.rowCount()
        return preferred_row
    except Exception:
        return grid.rowCount()


def _send_tab_amount_sat(send_tab) -> Optional[int]:
    """What the Send tab is currently set to send, in sat, or None if there is no
    usable amount on screen.

    ``amount_e`` is the single source for both rails and for the Max button
    (``spend_max`` writes its computed maximum back into the field), so one read
    covers on-chain, Lightning and max sends alike.

    Pay-to-many is the exception: its per-line amounts live in the pay-to field
    and ``amount_e`` stays empty, so the outputs are summed instead. A '!' line
    among them makes the total unknowable until a tx is built, and is reported as
    "no amount" rather than guessed at -- a warning invented from a wrong number
    is worse than no warning.
    """
    try:
        amount = send_tab.amount_e.get_amount()
    except Exception:
        amount = None
    if amount is not None:
        return int(amount)
    try:
        pi = send_tab.payto_e.payment_identifier
        if pi is None or not pi.is_multiline():
            return None
        # The argument is the amount to use for a single-output identifier and is
        # ignored on the multiline branch (which returns its parsed per-line
        # outputs), so the 0 here is never spent -- it only satisfies the
        # signature. The multiline guard above is what makes that safe.
        outputs = pi.get_onchain_outputs(0)
        total = 0
        for out in outputs:
            value = out.value
            if not isinstance(value, int):
                return None      # a '!' weight: not a knowable amount
            total += value
        return total or None
    except Exception:
        return None


def _install_send_tab_warning(grid: QGridLayout,
                              warning_text: Callable[['Abstract_Wallet', Optional[int]], str],
                              ) -> Optional[Callable[[], None]]:
    """Add the inline buffer warning to a Send tab, given its grid.

    Returns a refresh callable the plugin can fire when the BALANCE moves (the
    threshold is balance-dependent, so a label refreshed only on keystrokes would
    go stale while the user sits on the tab), or None if the tab could not be
    identified. Uses Electrum's ``create_send_tab`` hook rather than a
    monkeypatch -- this is the one part of the feature Electrum has a real
    extension point for.
    """
    try:
        from electrum.gui.qt.util import ColorScheme
    except Exception:
        return None
    send_tab = grid.parentWidget()
    wallet = getattr(send_tab, "wallet", None)
    amount_e = getattr(send_tab, "amount_e", None)
    if wallet is None or amount_e is None:
        return None

    label = QLabel("")
    label.setWordWrap(True)
    label.setStyleSheet(ColorScheme.RED.as_stylesheet())
    # Hidden until there is something to say, so an ordinary send shows no trace
    # of the feature and the tab does not reserve a blank strip for it.
    label.setVisible(False)
    row = _free_grid_row(grid, SEND_WARNING_GRID_ROW)
    grid.addWidget(label, row, 1, 1, 4)

    def refresh() -> None:
        try:
            text = warning_text(wallet, _send_tab_amount_sat(send_tab))
            label.setText(text)
            label.setVisible(bool(text))
        except RuntimeError:
            pass    # the C++ widget went away with its window; nothing to do
        except Exception:
            pass    # never let the Send tab break over an advisory label

    amount_e.textChanged.connect(lambda _text: refresh())
    # Pay-to-many keeps its amounts in the pay-to field, which leaves amount_e
    # silent -- so the multiline case needs its own trigger.
    payto_e = getattr(send_tab, "payto_e", None)
    if payto_e is not None:
        try:
            payto_e.textChanged.connect(refresh)
        except Exception:
            pass
    refresh()
    return refresh


# --- unsaved changes on an Apply-gated settings tab ------------------------
# Four of the Liquidity tab's sub-tabs (Settings, Swap providers, Channel
# partners, Advanced) hold their edits until an Apply button is pressed. That is
# the right design -- several of these fields are cross-validated against each
# other, and half of them move money -- but it has the failure mode every such
# form has: a user types a value, reads it back on screen, switches tab, and the
# wallet never saw it. Nothing on screen distinguished "20000, saved" from
# "20000, typed and about to be lost".
#
# So the three pieces below. A tab declares what its Apply-gated widgets are
# (_DirtyTracker), the hint appears under its status label the moment any of them
# diverges from what is stored (_StatusLine), and leaving the tab with edges
# outstanding asks what to do about them (_UnsavedGuard, on the Plugin).
#
# Deliberately NOT covered: the controls that apply the instant they are touched
# -- the Automation switch, "Manual run only", "Only manage channels the plugin
# opened", the two liquidity-sink switches, and the Log tab's capture switches.
# They are already saved, so showing "click APPLY" next to one of them would be
# a lie, and asking to save it on the way out would be asking about nothing.
_APPLY_HINT_COLOR = "#d29922"

# Where the usage-statistics consent screen and settings pane send a user who
# wants the full story. The repo's own PRIVACY.md, on the branch the releases
# are cut from, so it matches the running version rather than whatever `main`
# says today.
_USAGE_PRIVACY_URL = (
    "https://github.com/BareBits/electrum_liquidity/blob/beta/PRIVACY.md")


class _StatusLine:
    """One settings tab's status area: the tab's own message, plus an "unsaved
    changes" hint beneath it.

    Two labels rather than one. A red validation error or configuration warning
    and the amber hint can then be on screen together without either having to
    hide the other, and -- since each label still carries a plain string and a
    stylesheet colour -- without turning every caller's message into rich text
    that would have to be escaped on the way in and parsed on the way out.

    The two kinds of message are treated differently when the tab goes dirty:

      * a SUCCESS ("Settings saved.") is cleared. It stopped being true the
        moment the user typed again, and leaving it sitting above "click APPLY to
        save changes" would put two contradictory sentences on screen.
      * a WARNING is kept, above the hint. It describes a configuration that is
        still in force -- "nothing can drain a channel until you set a sink" does
        not stop being the case because there is now also something unsaved --
        and it is the more important of the two.
    """

    HINT_TEXT = _("Click APPLY to save changes")

    def __init__(self, message_label: QLabel, hint_label: QLabel) -> None:
        self._message = message_label
        self._hint = hint_label
        # Both labels render their text LITERALLY. A QLabel defaults to
        # Qt.AutoText, which sniffs each string with `mightBeRichText()` and
        # silently switches to HTML for anything that looks like markup -- so a
        # message is plain text only for as long as no caller interpolates
        # something containing a tag.
        #
        # One now does: the Providers tab names the provider whose stats were
        # just cleared, and a provider's display name comes from its own nostr
        # profile. "<b>", "<br>", and "<a href=…>" all survive the name
        # sanitiser (they are ordinary printable characters, and a legitimate
        # name may contain "<" or "&"), so an AutoText label would let a
        # provider style, re-link or restructure a sentence that is confirming a
        # destructive action. Sanitising guarantees the text is inert; this
        # guarantees it is *displayed* inert, which is the half the sanitiser
        # cannot do from where it sits.
        #
        # Set here rather than at that one call site so it holds for all thirty
        # callers and for the next one, and so the class's promise of "a plain
        # string and a stylesheet colour" is enforced instead of assumed.
        for label in (self._message, self._hint):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self._sticky = False        # is the current message a warning?
        # Tracked here rather than read back off the widget: Qt's isVisible() is
        # "actually on screen", which is False for every widget whose window has
        # not been shown -- so a label we have explicitly put up would report
        # itself hidden while the tab is being built, and in any offscreen
        # rendering of it.
        self._showing_hint = False
        self._hint.setStyleSheet(f"color: {_APPLY_HINT_COLOR};")
        self._hint.setVisible(False)

    def set_message(self, text: str, *, color: str = "", sticky: bool = False) -> None:
        """Show one of the tab's own messages. ``sticky`` marks it as a warning,
        i.e. one that survives the tab going dirty."""
        self._message.setStyleSheet(f"color: {color};" if color else "")
        self._message.setText(text)
        self._sticky = bool(sticky and text)

    def set_saved(self, text: str) -> None:
        """Report a successful save: green, and cleared if the user edits again."""
        self.set_message(text, color="green", sticky=False)

    def set_error(self, text: str) -> None:
        """Report a refused value: red, and kept if the user edits again (they are
        about to -- the error is telling them what to change)."""
        self.set_message(text, color="red", sticky=True)

    def set_dirty(self, dirty: bool) -> None:
        self._showing_hint = bool(dirty)
        self._hint.setVisible(self._showing_hint)
        if dirty and not self._sticky:
            # Drop a stale "saved" line; keep a warning (see the class docstring).
            self._message.setText("")
            self._message.setStyleSheet("")

    def text(self) -> str:
        """Everything the status area currently says, hint included. The one
        reader a test (or a screenshot caption) wants; the two labels are an
        implementation detail of how it is painted."""
        parts = [self._message.text()]
        if self._showing_hint:
            parts.append(self._hint.text())
        return "\n".join(p for p in parts if p)


class _DirtyTracker:
    """Whether one Apply-gated settings tab has edits the wallet has not seen.

    The tab hands over a ``snapshot`` callable returning a hashable summary of
    every Apply-gated widget on it, and the signals that mean "one of those may
    have changed". Dirty is then simply ``snapshot() != baseline``, where the
    baseline is re-taken whenever the tab is known to agree with storage (after a
    build, after a successful Apply, after a repopulate that was allowed to
    overwrite the fields).

    Comparing a snapshot against a baseline, rather than setting a flag from the
    change signals, is what makes this honest in the cases that matter: typing a
    character and deleting it again leaves the tab clean, and a repopulate that
    re-renders the same stored values does not invent a change.

    ``acknowledge`` records the exact snapshot the user chose to keep unsaved, so
    that answering "Ignore" once is not re-asked on every subsequent tab switch
    -- while any *further* edit produces a different snapshot and asks again.
    """

    def __init__(self, name: str, snapshot: Callable[[], Tuple],
                 status: Optional[_StatusLine] = None) -> None:
        self.name = name
        self._snapshot = snapshot
        # Public: the tracker is what decides when the hint goes up, so it owns
        # this tab's status area as far as anything outside is concerned.
        self.status = status
        self._baseline: Tuple = ()
        self._acked: Optional[Tuple] = None
        self._muted = False
        self.reset()

    # --- the tab tells us when it agrees with storage -------------------
    def reset(self) -> None:
        """Declare the widgets to match what is stored: re-baseline, forget any
        acknowledgement, and clear the hint."""
        try:
            self._baseline = self._snapshot()
        except RuntimeError:
            return      # the tab's widgets are gone (wallet closed mid-refresh)
        self._acked = None
        self._render()

    @contextlib.contextmanager
    def muted(self):
        """Suppress re-checks for the duration of a block that rewrites widgets
        itself (a repopulate, a reload). Without this, every ``setText`` inside
        would fire a change signal and be measured against a baseline that the
        same block is in the middle of replacing."""
        before, self._muted = self._muted, True
        try:
            yield
        finally:
            self._muted = before

    # --- queries --------------------------------------------------------
    def is_dirty(self) -> bool:
        try:
            return self._snapshot() != self._baseline
        except RuntimeError:
            return False        # widgets gone; nothing to save
        except Exception:
            # A snapshot must never be the reason a tab or a tab switch breaks.
            return False

    def needs_prompt(self) -> bool:
        """Dirty, and not something the user has already said to leave unsaved."""
        if not self.is_dirty():
            return False
        try:
            return self._snapshot() != self._acked
        except Exception:
            return False

    def acknowledge(self) -> None:
        """Remember this exact set of pending edits as one the user chose to keep
        unsaved, so leaving the tab again does not re-ask about it."""
        try:
            self._acked = self._snapshot()
        except Exception:
            self._acked = None

    # --- plumbing -------------------------------------------------------
    def watch(self, *signals) -> None:
        """Re-check whenever any of these signals fires."""
        for signal in signals:
            if signal is None:
                continue
            try:
                signal.connect(self._on_changed)
            except Exception:
                # A widget whose signal we cannot connect simply does not drive
                # the live hint; the snapshot comparison still catches it on the
                # way out of the tab.
                continue

    def _on_changed(self, *_args) -> None:
        if self._muted:
            return
        self._render()

    def refresh_hint(self) -> None:
        """Re-render the hint from the current snapshot. Needed after a block that
        ran under :meth:`muted` and left the tab dirty -- the change signals that
        would normally have driven the hint were suppressed."""
        self._render()

    def _render(self) -> None:
        if self.status is not None:
            self.status.set_dirty(self.is_dirty())


class _ApplyPane:
    """One Apply-gated settings pane, as the unsaved-changes guard sees it: a
    name to put in a dialog, the widget that identifies it, a tracker that says
    whether it has pending edits, and the Apply the dialog can press on the
    user's behalf (returning whether the save went through).

    ``host`` and ``focus`` exist for NESTED panes. The Advanced tab carries an
    inner tab bar (General / Usage statistics), so two panes live inside one
    entry of the outer tab bar. The guard identifies a pane by the outer tab it
    is reached through -- its ``host`` -- because that is the only widget the
    outer QTabWidget knows about; a nested pane's own widget is not
    ``tabs.widget(i)`` for any ``i``, so keying on it would make the guard
    silently blind to those edits (it would look up the pane, find nothing, and
    conclude there was nothing to save). ``focus`` brings a nested pane back
    into view when its Apply is refused, since that is where the message
    explaining the refusal is.

    For an ordinary top-level pane both are omitted and ``host`` is the widget
    itself, which is exactly the old behaviour.
    """

    def __init__(self, name: str, widget: QWidget, tracker: _DirtyTracker,
                 apply: Callable[[], bool], *,
                 host: Optional[QWidget] = None,
                 focus: Optional[Callable[[], None]] = None) -> None:
        self.name = name
        self.widget = widget
        self.tracker = tracker
        self.apply = apply
        self.host = host if host is not None else widget
        self.focus = focus

    def bring_into_view(self) -> None:
        """Show this pane, if it needs an extra step to be visible. Never
        raises: this is a courtesy on an error path."""
        if self.focus is None:
            return
        try:
            self.focus()
        except Exception:
            pass


def _ask_two_choices(window: 'ElectrumWindow', *, title: str, text: str,
                     accept_label: str, reject_label: str) -> Optional[bool]:
    """Ask a two-button question whose buttons are not "Yes" and "No".

    Electrum's ``question()`` hard-codes those two words, and neither question
    this plugin asks here reads as a yes/no: the answers are "Apply changes" vs
    "Ignore", and "switch to gossip" vs "leave it alone". ``msg_box`` does take
    custom buttons, so this wraps that.

    The reject button is given Qt's RejectRole, which makes it what Escape and
    the window's close button resolve to. That is the safe answer for both
    callers: Escape keeps your unsaved edits (Ignore does not discard them), and
    Escape leaves your Lightning routing exactly as you had it.

    Returns True for accept, False for reject, or None if no dialog could be put
    up at all -- which the callers treat as "do not act", never as consent.
    """
    from PyQt6.QtWidgets import QMessageBox, QPushButton
    msg_box = getattr(window, "msg_box", None)
    if msg_box is None:
        # A window too old to have msg_box still gets asked the question, in the
        # wrong words, rather than silently not being asked at all.
        ask = getattr(window, "question", None)
        if ask is None:
            return None
        try:
            return bool(ask(text, title=title))
        except Exception:
            return None
    accept_btn = QPushButton(accept_label)
    reject_btn = QPushButton(reject_label)
    try:
        answer = msg_box(
            QMessageBox.Icon.Question, None, title, text,
            buttons=[(accept_btn, QMessageBox.ButtonRole.AcceptRole, 1),
                     (reject_btn, QMessageBox.ButtonRole.RejectRole, 0)])
    except Exception:
        return None
    return answer == 1


def _wrapped_label(text: str) -> QLabel:
    """A QLabel whose (often multi-paragraph) text wraps to the panel width
    instead of being clipped at the right edge."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _fit_to_screen(widget: QWidget, *, margin: int = 80) -> None:
    """Shrink ``widget`` so it fits the screen it will appear on.

    Qt will happily give a dialog whatever size its layout asks for, including
    one taller than the display -- and a modal whose buttons are below the
    bottom edge cannot be dismissed at all on a window manager without
    keyboard-driven moves. Best-effort and silent: a screen we cannot measure
    just leaves the requested size alone.
    """
    try:
        screen = widget.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        widget.resize(min(widget.width(), max(320, available.width() - margin)),
                      min(widget.height(), max(320, available.height() - margin)))
    except Exception:
        pass


def _bullet_labels(items: Sequence[str], *, indent: int = 18,
                   color: str = "gray") -> List[QLabel]:
    """One wrapped, indented "• item" label per item.

    One label per bullet rather than a single label holding "\\n".join(...):
    a QLabel wraps to its own left edge, so in the joined form the second line
    of a long bullet starts back at column zero and the list stops reading as a
    list. ``setIndent`` applies to every line of a left-aligned label, so each
    bullet's continuation stays under its own text.

    Used by both the usage-statistics consent dialog and the settings pane, so
    the same list cannot end up formatted two different ways.
    """
    labels: List[QLabel] = []
    for item in items:
        label = _wrapped_label(f"• {item}")
        label.setIndent(indent)
        if color:
            label.setStyleSheet(f"color: {color};")
        labels.append(label)
    return labels


class _Signals(QObject):
    # NB: there is deliberately no "activity" signal. The plugin used to push
    # transient notices into Electrum's main status bar (next to the balance);
    # it no longer writes there at all. Everything it has to say now lives
    # inside the Liquidity tab: the Status footer for what a tick is doing, and
    # the Actions / Log sub-tabs for what it did.
    log_changed = pyqtSignal(object)          # (wallet,)
    providers_changed = pyqtSignal(object)    # (wallet,) -- discovered provider list refreshed
    status_changed = pyqtSignal(object, str)  # (wallet, tick status) -- from the asyncio thread
    unlock_requested = pyqtSignal(object)     # (wallet,) -- a tick deferred on a locked wallet


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
        self.set_locked: Callable[[bool], None] = lambda locked: None
        self.drop_unsaved_guard: Callable[[], None] = lambda: None
        self.panes: List['_ApplyPane'] = []
        for key, value in handles.items():
            setattr(self, key, value)


def _fmt_sat(amount: Optional[int]) -> str:
    return f"{amount:,}" if isinstance(amount, int) else "—"


def _fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


def _wallet_name(wallet: 'Abstract_Wallet') -> str:
    """The wallet's file name, for log lines. Falls back to a placeholder rather
    than raising: every caller is on an error path or a lifecycle log, where
    failing to name the wallet must not become a second failure."""
    try:
        return wallet.basename()
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


# Sort key for a provider that publishes no name, so those rows land after every
# named one however the column is ordered. `_sort_tuple` compares string keys
# casefolded, and no real name can reach here containing U+FFFF: it is not
# printable, so `clean_display_name` strips it out.
_NO_NAME_SORT_KEY = "\uffff"

# Longest tooltip we will build out of outside text. A tooltip is a floating
# window sized to its content, so an unbounded one is a screenful.
MAX_TOOLTIP_CHARS = 400


def _literal_tooltip(text: object) -> str:
    """A tooltip that renders ``text`` exactly as written.

    Needed because QToolTip interprets rich text and -- unlike QLabel -- offers
    no per-item PlainText switch: Qt decides per string, via
    ``mightBeRichText()``. Every tooltip the plugin builds from outside data
    (a peer's gossip alias, a node id, a provider's fault reason) would
    otherwise become markup the moment that data contained a tag, which is how
    "02aa\u2026" becomes a clickable link to somewhere else.

    So the text is escaped and the result deliberately IS rich text -- markup we
    wrote, wrapping characters that can no longer be markup. ``white-space:
    pre-wrap`` keeps the newlines that separate a tooltip's lines, which plain
    rich text would collapse to spaces.
    """
    body = "" if text is None else str(text)
    if len(body) > MAX_TOOLTIP_CHARS:
        body = body[:MAX_TOOLTIP_CHARS - 1] + "\u2026"
    return f"<div style='white-space:pre-wrap'>{html.escape(body)}</div>"


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
        # When we last asked about a locked wallet and were turned down, per
        # wallet. Automation ticks every few minutes, so without this a
        # dismissed prompt would come straight back; see UNLOCK_PROMPT_COOLDOWN_SEC.
        self._unlock_declined_at: Dict['Abstract_Wallet', float] = {}
        # Wallets with an unlock dialog on screen right now. The dialog is modal
        # but Qt keeps delivering queued signals while it runs, so without this a
        # second tick could stack a second dialog on top of the first.
        self._unlock_prompting: set = set()
        # Instance attribute so tests can shrink it.
        self._unlock_prompt_cooldown_sec: float = UNLOCK_PROMPT_COOLDOWN_SEC
        # Whether the update-check opt-in has been put on screen in this session.
        # The persisted flag records the *answer*; this guards against opening a
        # second wallet stacking a second dialog before the first is answered.
        self._update_opt_in_asked: bool = False
        # Same guard for the usage-statistics consent screen, for the same
        # reason -- and more so here, since that dialog is a long one to find
        # stacked twice.
        self._usage_consent_asked: bool = False
        # One Send-tab buffer-warning refresher per open wallet. Keyed by wallet
        # rather than window because that is the key the buffer itself is looked
        # up by, and Electrum opens one window per wallet.
        self._send_warning_refreshers: Dict['Abstract_Wallet', Callable[[], None]] = {}

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow') -> None:
        if self.signals is None:
            self.signals = _Signals()
            self.signals.log_changed.connect(self._on_log_changed_ui)
            self.signals.providers_changed.connect(self._on_providers_changed_ui)
            self.signals.status_changed.connect(self._on_status_changed_ui)
            self.signals.unlock_requested.connect(self._on_unlock_requested_ui)
        self._add_liquidity_tab(window, wallet)
        # Add the "Managed by" column to Electrum's Channels tab (once, globally),
        # then refresh this window's already-built list so the column appears now.
        if _patch_channels_list_managed_column():
            try:
                window.channels_list.update_rows.emit(wallet)
            except Exception:
                self.logger.debug("could not refresh channels list after column patch")
        # The liquidity-goal buffer slice on the balance pie chart. Published
        # BEFORE start_wallet so the first repaint after the wallet becomes
        # managed already carries the slice.
        _set_buffer_provider(self.goal_buffer_sat)
        _set_goal_visibility(_GoalVisibility(self.show_goal_in_piechart,
                                             self.set_show_goal_in_piechart))
        _set_send_warning_refresher(self._refresh_send_warnings)
        if not _patch_balance_piechart():
            self.logger.debug("could not add the liquidity-buffer slice to the balance chart")
        self.start_wallet(wallet)
        # start_wallet decides whether this wallet is managed at all, and the
        # Send tab was built (and first refreshed) back in the window's
        # constructor, long before that -- so re-ask now that the answer is known.
        self._refresh_send_warnings()
        try:
            window.update_status()
        except Exception:
            self.logger.debug("could not refresh the balance chart after load")
        self._register_closing_warning(window, wallet)
        self._maybe_prompt_update_opt_in(window, wallet)
        self._maybe_warn_trampoline(window, wallet)
        # Last of the one-time prompts: it is the longest, and it is the one
        # whose honest default (off) costs the user nothing if they close it.
        self._maybe_prompt_usage_consent(window, wallet)

    def _register_closing_warning(self, window: 'ElectrumWindow',
                                  wallet: 'Abstract_Wallet') -> None:
        """Ask Electrum to warn before closing while we have a swap attempt live.

        Electrum already warns about swaps whose funding tx is on-chain but
        unconfirmed (``_check_ongoing_submarine_swaps_callback``, which reads
        ``get_pending_swaps()``). That check cannot see the window this plugin
        actually creates: a reverse swap whose Lightning payment is committed but
        whose funding output does not exist yet. The swap is absent from
        ``get_pending_swaps()`` for the whole of it, so without this the riskiest
        moment of the whole operation is the one moment the user is told nothing.

        Registration is best-effort and version-tolerant: the hook is a recent
        addition to Electrum's main window, and a build without it should cost us
        a debug line, not a wallet that will not load.
        """
        register = getattr(window, "register_closing_warning_callback", None)
        if register is None:
            self.logger.debug(
                "this Electrum build has no closing-warning hook; not registering")
            return
        try:
            register(lambda: self._closing_warning(wallet))
        except Exception:
            self.logger.debug("could not register the closing warning")

    def _closing_warning(self, wallet: 'Abstract_Wallet') -> Optional[str]:
        """The warning text while a reverse-swap attempt is in flight, else None.

        Deliberately says what will happen if they go ahead -- we stop cascading,
        wait for this one attempt, then close -- because the honest answer to "is
        it safe to quit now?" is "nearly, and here is the bit you are waiting
        for". Returning None is the normal case and shows no dialog at all.
        """
        attempt = self.inflight_swap_attempt(wallet)
        if attempt is None:
            return None
        return "\n\n".join((
            _("Inbound Liquidity: a swap is in progress"),
            _("A reverse swap of {amount} sat from channel {channel} has a "
              "Lightning payment in flight. Its funds are not at rest: the "
              "payment is committed but the on-chain output does not exist "
              "yet.").format(amount=f"{attempt.amount_sat:,}",
                             channel=attempt.short_id),
            _("If you close now, no further swap providers will be tried and "
              "Electrum will wait for this attempt to finish (up to {seconds} "
              "more seconds) before shutting down.").format(
                  seconds=int(attempt.remaining_sec())),
        ))

    def _await_shutdown(self, wallet: 'Abstract_Wallet') -> None:
        """Wait out the in-flight attempt without freezing the window.

        ``stop_wallet`` runs on the GUI thread (closeEvent -> clean_up ->
        run_hook('close_wallet')), while the attempt runs on the asyncio loop, so
        the base class's plain ``Event.wait`` would block Qt's event loop for up
        to a couple of minutes. To the user that is indistinguishable from a
        hang, and the reflex it produces -- force-killing Electrum -- is the exact
        outcome this whole handshake exists to prevent.

        So the wait is driven from here instead: poll the event in short slices,
        pumping Qt between them, and put up a modal dialog once it is clear this
        is going to take a moment (see SHUTDOWN_DIALOG_DELAY_SEC). Closing the
        dialog is not offered: this is the wait the user already agreed to in the
        closing warning, and its budget is bounded by the attempt's own backstop.
        """
        attempt = self.inflight_swap_attempt(wallet)
        if attempt is None:
            return
        event = self._attempt_done_event(wallet)
        if event is None:
            return
        budget = attempt.remaining_sec()
        started = time.monotonic()
        deadline = started + budget
        dialog: Optional[QWidget] = None
        label: Optional[QLabel] = None
        finished = False
        try:
            while time.monotonic() < deadline:
                if event.wait(SHUTDOWN_POLL_INTERVAL_SEC):
                    finished = True
                    break
                waited = time.monotonic() - started
                if dialog is None and waited >= SHUTDOWN_DIALOG_DELAY_SEC:
                    dialog, label = self._build_shutdown_dialog(attempt, deadline)
                if label is not None:
                    label.setText(self._shutdown_dialog_text(attempt, deadline))
                try:
                    QApplication.processEvents()
                except Exception:
                    # No usable event loop (headless tests, or Qt already torn
                    # down): keep waiting, just without repainting. The wait is
                    # what matters; the dialog is a courtesy.
                    pass
            else:
                # The budget ran out. One last non-blocking ask, so an attempt
                # that finished inside the final slice is not reported as a
                # timeout it never hit.
                finished = event.is_set()
        finally:
            if dialog is not None:
                try:
                    dialog.close()
                except Exception:
                    pass
        self.log_shutdown_wait_outcome(attempt, finished, budget)

    @staticmethod
    def _shutdown_dialog_text(attempt, deadline: float) -> str:
        remaining = max(0, int(deadline - time.monotonic()))
        return "\n".join((
            _("Finishing a swap of {amount} sat from channel {channel}.").format(
                amount=f"{attempt.amount_sat:,}", channel=attempt.short_id),
            _("No further providers will be tried."),
            _("Closing in up to {seconds}s.").format(seconds=remaining),
        ))

    def _build_shutdown_dialog(self, attempt, deadline: float,
                               ) -> Tuple[Optional[QWidget], Optional[QLabel]]:
        """The "please wait" window, or (None, None) if Qt cannot show one.

        Button-less, and with the title-bar close button removed: there is
        nothing here for the user to decide. It exists to say "still working, not
        hung", which is the only question they have at this point. Answers
        (None, None) rather than raising, because failing to show a progress
        window is a cosmetic loss while aborting the close path is not.
        """
        try:
            from electrum.gui.qt.util import WindowModalDialog
            dialog = WindowModalDialog(None, _("Closing Electrum"))
            layout = QVBoxLayout(dialog)
            label = QLabel(self._shutdown_dialog_text(attempt, deadline))
            layout.addWidget(label)
            dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
            dialog.show()
            return dialog, label
        except Exception:
            self.logger.debug("could not show the shutdown-wait dialog")
            return None, None

    def _maybe_prompt_update_opt_in(self, window: 'ElectrumWindow',
                                    wallet: 'Abstract_Wallet') -> None:
        """Ask ONCE whether the plugin may check GitHub for new releases.

        Asked rather than assumed: the check is the plugin's only contact with a
        server that has nothing to do with running the wallet, and a daily
        request to github.com tells a third party this wallet's IP and that it
        is running. So the question is put plainly, once, and both answers are
        remembered -- "prompted" is stored separately from "enabled" so that
        declining is final rather than being re-asked at every startup.

        Never blocks or breaks wallet load: a failure to show the dialog leaves
        the flag unset (it will be offered again next time) and nothing else.
        """
        c = self.settings(wallet)
        if bool(c.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED):
            return
        if bool(getattr(c, 'INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED', False)):
            return          # already switched on from Advanced; nothing to ask
        if self._update_opt_in_asked:
            return          # a second wallet opening in the same session
        self._update_opt_in_asked = True
        try:
            enable = bool(window.question(
                ' '.join([
                    _("Should the Inbound Liquidity plugin check GitHub once a day "
                      "for new versions of itself?"),
                    _("This contacts github.com (through your proxy, if you have one "
                      "configured), which tells GitHub that this wallet is running."),
                    _("Nothing is ever downloaded or installed — if a newer version "
                      "exists, the plugin only says so and links to the release."),
                    _("You can change this later in the plugin's Advanced settings."),
                ]),
                title=_("Check for plugin updates?")))
        except Exception:
            # A prompt is a convenience; never let it break loading a wallet.
            self.logger.exception("could not show the update-check opt-in prompt")
            self._update_opt_in_asked = False
            return
        c.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED = True
        c.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = enable
        self.logger.info(
            f"update check {'enabled' if enable else 'declined'} by the user")
        if enable:
            # Don't make the user wait for the next heartbeat to learn the answer.
            self._request_update_check(wallet)

    def _maybe_prompt_usage_consent(self, window: 'ElectrumWindow',
                                    wallet: 'Abstract_Wallet') -> None:
        """Offer the anonymous-usage-statistics choice ONCE, with previews.

        Everything about this is built around not manufacturing consent:

          * The dialog opens on **Off**. The only way to end up sharing anything
            is to deliberately select a level and press Save, so an impatient
            Return or a reflexive click leaves reporting off.
          * The **preview is live** and shows THIS wallet's real values, built by
            the same function that builds the real report. Clicking through the
            levels shows exactly what each one would send, before committing.
          * **"Ask me later" stores nothing**, so closing the dialog (or
            pressing Escape, which resolves to it) is not recorded as a refusal
            and is not treated as agreement either. Pressing Save IS recorded,
            in both directions, so a deliberate "no" is final.
          * Skipped entirely when nothing could be sent anyway -- reporting
            switched off for the installation, or a regtest wallet. Asking for
            permission we could not act on is just noise.

        Never blocks or breaks wallet load: any failure leaves the flag unset
        (so the question comes back next time) and nothing else.
        """
        from . import usage_stats

        if self.usage_stats_prompted(wallet):
            return
        if self.usage_stats_level(wallet) != usage_stats.LEVEL_OFF:
            return      # already switched on from Advanced; nothing to ask
        if self.usage_stats_blocked_reason() is not None:
            return      # nothing could be sent regardless; see the docstring
        if self._usage_consent_asked:
            return      # a second wallet opening in the same session
        self._usage_consent_asked = True
        try:
            chosen = self._ask_usage_consent(window, wallet)
        except Exception:
            # A prompt is a convenience; never let it break loading a wallet.
            self.logger.exception("could not show the usage-statistics consent screen")
            self._usage_consent_asked = False
            return
        if chosen is None:
            self.logger.info("usage-statistics consent deferred ('ask me later')")
            return
        self.set_usage_stats_choice(wallet, chosen)
        if chosen != usage_stats.LEVEL_OFF:
            # Don't make the user wait a week to see that it works.
            self._request_usage_report(wallet, force=True)
        # The Advanced tab's usage pane may already be built and showing the old
        # value, so bring it in line with what was just chosen.
        state = self._tabs.get(wallet)
        if state is not None:
            try:
                state.refresh()
            except Exception:
                self.logger.debug("could not refresh the tab after the consent choice")

    def _ask_usage_consent(self, window: 'ElectrumWindow',
                           wallet: 'Abstract_Wallet') -> Optional[int]:
        """Put the consent dialog up. Returns the chosen level (0 for "no
        thanks"), or None for "ask me later" / no answer."""
        from electrum.gui.qt.util import WindowModalDialog
        from . import usage_stats

        descriptions = usage_stats.level_descriptions()
        dialog = WindowModalDialog(window, _("Help improve this plugin?"))
        # Wide enough that a 50-field JSON preview is readable without the user
        # having to resize the dialog to find out what they are agreeing to.
        dialog.setMinimumWidth(620)

        # The content -- four options with their bullets, plus a preview of a
        # 50-field report -- is taller than a short laptop screen. So it lives
        # in a scroll area, with the BUTTONS OUTSIDE it: a consent dialog whose
        # "No thanks" has fallen off the bottom of the screen is not one, and
        # a modal the user cannot dismiss is worse than the feature is worth.
        outer = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        layout.addWidget(_wrapped_label(_(
            "Would you help us understand how many people run the Inbound "
            "Liquidity plugin, which settings matter, and how much liquidity it "
            "actually provisions?\n\n"
            "This is entirely optional and OFF unless you choose otherwise. The "
            "plugin behaves exactly the same either way. If you say yes, this "
            "wallet sends one small report now and then one a week.")))

        layout.addWidget(_wrapped_label(_("We never send, at any level:")))
        for label in _bullet_labels([_(item) for item in usage_stats.never_sent_items()]):
            layout.addWidget(label)

        privacy = _wrapped_label("")
        privacy.setTextFormat(Qt.TextFormat.RichText)
        privacy.setOpenExternalLinks(True)
        privacy.setText(
            f'<a href="{_USAGE_PRIVACY_URL}">{html.escape(_("Read the privacy policy"))}</a>')
        layout.addWidget(privacy)

        layout.addWidget(QLabel(_("Choose how much to share:")))
        group = QButtonGroup(dialog)
        group.setExclusive(True)
        buttons: List[Tuple[QRadioButton, int]] = []

        off_button = QRadioButton(_("No thanks — send nothing"))
        off_button.setChecked(True)          # the dialog opens on "off"
        group.addButton(off_button)
        buttons.append((off_button, usage_stats.LEVEL_OFF))
        layout.addWidget(off_button)

        for level in usage_stats.LEVELS:
            description = descriptions[level]
            button = QRadioButton(f"{level} · {_(description.title)}")
            button.setToolTip(_(description.summary))
            group.addButton(button)
            buttons.append((button, level))
            layout.addWidget(button)
            summary = _wrapped_label(_(description.summary))
            summary.setIndent(18)
            summary.setStyleSheet("color: gray;")
            layout.addWidget(summary)
            for label in _bullet_labels([_(item) for item in description.items],
                                        indent=34):
                layout.addWidget(label)

        layout.addWidget(QLabel(_("Exactly what would be sent:")))
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        preview_font = QFont("monospace")
        preview_font.setStyleHint(QFont.StyleHint.Monospace)
        preview.setFont(preview_font)
        preview.setMinimumHeight(200)
        layout.addWidget(preview)

        def _selected_level() -> int:
            for button, level in buttons:
                if button.isChecked():
                    return level
            return usage_stats.LEVEL_OFF

        def _refresh_preview() -> None:
            level = _selected_level()
            if level == usage_stats.LEVEL_OFF:
                preview.setPlainText(_("Nothing is sent."))
                return
            try:
                preview.setPlainText(self.usage_stats_preview(wallet, level))
            except Exception as e:
                self.logger.exception("could not build the consent-screen preview")
                preview.setPlainText(_("Could not build the preview: {}").format(
                    scrub_text(f"{type(e).__name__}: {e}")[:200]))

        for button, _level in buttons:
            button.toggled.connect(lambda _checked: _refresh_preview())
        _refresh_preview()

        layout.addWidget(_wrapped_label(_(
            "You can change this at any time under the plugin's Advanced "
            "settings, on the Usage statistics tab.")))

        # "Save" is the accept role, "Ask me later" the reject role -- so
        # Escape and the title-bar close both resolve to deferring, never to a
        # recorded answer in either direction.
        save_button = primary_button(_("Save my choice"))
        later_button = QPushButton(_("Ask me later"))
        save_button.clicked.connect(dialog.accept)
        later_button.clicked.connect(dialog.reject)
        row = QHBoxLayout()
        row.addWidget(later_button)
        row.addStretch(1)
        row.addWidget(save_button)
        outer.addLayout(row)      # outside the scroll area: always reachable

        # Open at the content's natural size, but never taller than the screen
        # it has to fit on (the scroll area absorbs the difference).
        dialog.resize(680, 900)
        _fit_to_screen(dialog)

        if not dialog.exec():
            return None
        return _selected_level()

    def _maybe_warn_trampoline(self, window: 'ElectrumWindow',
                               wallet: 'Abstract_Wallet') -> None:
        """Tell the user ONCE that trampoline routing undercuts this plugin, and
        offer to switch to gossip.

        Why it is worth a dialog rather than only a line of documentation: on
        trampoline (Electrum's default) every swap and every sink payment carries
        a trampoline node's fee on top of the route's, a channel can only be
        opened to one of a handful of central nodes, and the node aliases this
        plugin shows come from gossip the wallet is not downloading. A user can
        reasonably run the plugin for weeks without discovering any of that.

        Asked once per wallet and then never again, however it was answered: the
        flag records that the conversation happened, not what was decided, so a
        user who wants trampoline is not re-asked every time they open the wallet.
        The standing banner on the Settings tab is what remains afterwards.

        Never blocks or breaks wallet load -- a failure to show the dialog leaves
        the flag unset, so the question is simply put again next time.
        """
        if wallet not in self.wallets:
            # Not a wallet this plugin manages (no Lightning, or offline). Its
            # routing mode is then none of our business.
            return
        c = self.settings(wallet)
        if bool(getattr(c, 'INBOUND_LIQUIDITY_TRAMPOLINE_WARNED', False)):
            return
        if not self.trampoline_routing_active():
            # Already on gossip. Deliberately NOT marked as warned: if they
            # switch to trampoline later, that is the moment this is worth
            # saying, and it should still be sayable then.
            return
        answer = _ask_two_choices(
            window,
            title=_("Trampoline routing is enabled"),
            text=_(
                "Warning: Trampoline routing is enabled (and gossip is "
                "disabled). Trampoline routing is more expensive, more "
                "centralized, and makes this plugin less useful. Turn it off and "
                "turn on gossip?"),
            accept_label=_("Yes, switch to full gossip mode (suggested)"),
            reject_label=_("No"))
        if answer is None:
            self.logger.debug(
                "could not show the trampoline-routing warning; will re-ask")
            return
        c.INBOUND_LIQUIDITY_TRAMPOLINE_WARNED = True
        if not answer:
            self.logger.info("user kept trampoline routing")
            return
        if self.switch_to_gossip_routing(wallet):
            # The banner reads the config, so it has to be told to look again.
            state = self._tabs.get(wallet)
            refresh_banner = getattr(state, "refresh_trampoline_banner", None)
            if refresh_banner is not None:
                try:
                    refresh_banner()
                except Exception:
                    self.logger.debug("could not refresh the trampoline banner")

    @hook
    def create_send_tab(self, grid: QGridLayout) -> None:
        """Add the inline liquidity-buffer warning to a Send tab as it is built.

        Fires from ``SendTab.__init__``, which Electrum runs in the window
        constructor -- BEFORE ``load_wallet``, and so before ``start_wallet`` has
        decided whether this wallet is managed. That is why the label starts
        hidden and is re-asked on every refresh rather than being decided once
        here: at this instant the honest answer is always "no buffer".

        Returns None unconditionally. ``run_hook`` asserts that at most one
        plugin returns a truthy value, so a stray return here would break any
        other plugin that legitimately answers this hook.
        """
        try:
            refresh = _install_send_tab_warning(grid, self.send_warning_text)
        except Exception:
            self.logger.exception("could not add the liquidity-buffer warning to the Send tab")
            return
        if refresh is None:
            return
        wallet = getattr(grid.parentWidget(), "wallet", None)
        if wallet is not None:
            self._send_warning_refreshers[wallet] = refresh

    def _refresh_send_warnings(self) -> None:
        """Re-evaluate every open Send tab's buffer warning. Called whenever the
        balance moves (from the patched ``update_status``) and once the wallet is
        known to be managed."""
        for refresh in list(self._send_warning_refreshers.values()):
            try:
                refresh()
            except Exception:
                pass

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet') -> None:
        self.stop_wallet(wallet)
        self._remove_liquidity_tab(wallet)
        # Reopening a wallet is a fresh session: a decline made an hour ago
        # should not silence the prompt for a wallet the user just opened.
        self._unlock_declined_at.pop(wallet, None)
        self._unlock_prompting.discard(wallet)
        # Drop the Send tab's refresher with its window; the class patches stay
        # installed (they are global and idempotent) but go inert once the last
        # wallet closes and the provider is cleared.
        self._send_warning_refreshers.pop(wallet, None)
        if not self._send_warning_refreshers:
            _set_send_warning_refresher(None)
        if not self.wallets:
            _set_buffer_provider(None)
            _set_goal_visibility(None)
        # This wallet is no longer one of the open ones, so the process-wide
        # answers drawn from all of them have to drop it (see
        # apply_process_wide_settings): its capture preference should not outlive
        # it, and the funding floor should rise back if it was the lowest.
        self.apply_process_wide_settings()

    @hook
    def on_close_window(self, window: 'ElectrumWindow') -> None:
        """Drop the UI handle for a window Electrum has finished with.

        In the ordinary close, ``clean_up`` runs ``close_wallet`` before
        ``close_window``, so this finds nothing left to do -- which is the point:
        it is the backstop for the paths that reach window teardown WITHOUT the
        wallet being closed, where a kept handle would make the next
        ``_add_liquidity_tab`` for that wallet a no-op (see :meth:`_tab_is_live`).

        Deliberately narrower than ``close_wallet``: the window is going, the
        wallet may well not be (``should_stop_wallet_on_close`` is False for a
        reload), so this touches the tab handle only and leaves automation,
        heartbeat and per-wallet state alone.
        """
        wallet = getattr(window, "wallet", None)
        if wallet is None:
            return
        state = self._tabs.get(wallet)
        if state is not None and state.window is window:
            self._remove_liquidity_tab(wallet)

    def show_goal_in_piechart(self, wallet: 'Abstract_Wallet') -> bool:
        """Whether the user wants the liquidity-goal slices on this wallet's
        balance chart.

        A display setting, so it is read straight off the wallet's settings
        rather than going through ``read_config()`` -- the rules engine has no
        business knowing about it, and defaulting to True keeps the chart as it
        was for anyone upgrading.
        """
        return bool(self.settings(wallet).INBOUND_LIQUIDITY_SHOW_GOAL_IN_PIECHART)

    def set_show_goal_in_piechart(self, wallet: 'Abstract_Wallet',
                                  show: bool) -> None:
        self.settings(wallet).INBOUND_LIQUIDITY_SHOW_GOAL_IN_PIECHART = bool(show)

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

    # --- locked wallet: offering to unlock ---------------------------------
    def on_wallet_locked(self, wallet: 'Abstract_Wallet') -> None:
        # Called from the asyncio thread by every tick that defers on a locked
        # wallet; emit a queued signal so the dialog opens on the GUI thread.
        # Rate limiting lives on the UI side, not here.
        if self.signals is not None:
            self.signals.unlock_requested.emit(wallet)

    def _on_unlock_requested_ui(self, wallet: 'Abstract_Wallet') -> None:
        """An automatic tick found the wallet locked. Ask -- unless we asked
        recently and were turned down."""
        if self._signing_unlocked(wallet):
            # Unlocked between the tick and the delivery of this signal, or by
            # another route (Wallet > Unlock) since we last asked. Checked ahead
            # of the cooldown so a decline cannot outlive the lock it was about:
            # otherwise unlocking by hand, then locking again, would sit out the
            # remainder of a cooldown that no longer refers to anything.
            self._unlock_declined_at.pop(wallet, None)
            self._refresh_unlock_affordance(wallet)
            return
        declined_at = self._unlock_declined_at.get(wallet)
        if declined_at is not None:
            if time.time() - declined_at < self._unlock_prompt_cooldown_sec:
                return
            # Cooldown served; ask again from a clean slate.
            self._unlock_declined_at.pop(wallet, None)
        self._prompt_unlock(wallet)

    def _prompt_unlock(self, wallet: 'Abstract_Wallet') -> None:
        """Ask Electrum to unlock ``wallet``, saying who is asking and why.

        Routed through the window's own ``unlock_wallet`` rather than
        ``wallet.unlock``: that is what hands the password to the txbatcher,
        refreshes the padlock icon and the Wallet menu's Lock/Unlock item, and
        shows the standard confirmation. Doing it by hand would leave Electrum's
        own UI out of step with the wallet.

        Cancelling is a real answer -- it starts the cooldown and nothing else.
        Automation stays armed and resumes the moment the wallet is unlocked by
        any route (this prompt, the tab's button, or Wallet > Unlock)."""
        state = self._tabs.get(wallet)
        if state is None or state.window is None:
            return          # no window for this wallet (closing, or headless)
        if wallet in self._unlock_prompting:
            return          # a dialog for this wallet is already up
        if self._signing_unlocked(wallet):
            # Unlocked between the tick and the delivery of this signal.
            self._unlock_declined_at.pop(wallet, None)
            return
        self._unlock_prompting.add(wallet)
        try:
            state.window.unlock_wallet(message=' '.join([
                _("The Inbound Liquidity plugin needs your wallet unlocked to "
                  "open channels and sign swaps."),
                _("Automation is paused until you unlock it."),
                _("Enter your password to unlock your wallet:"),
            ]))
        except Exception:
            # A prompt is a convenience; never let it break the tab or the tick.
            self.logger.exception("could not show the unlock prompt")
        finally:
            self._unlock_prompting.discard(wallet)
        if self._signing_unlocked(wallet):
            self._unlock_declined_at.pop(wallet, None)
            # Do not make the user wait out the heartbeat for the automation
            # they just unlocked for.
            self.request_evaluation(wallet)
        else:
            self._unlock_declined_at[wallet] = time.time()
        self._refresh_unlock_affordance(wallet)

    def _refresh_unlock_affordance(self, wallet: 'Abstract_Wallet') -> None:
        """Show or hide the Settings tab's Unlock button to match the wallet."""
        state = self._tabs.get(wallet)
        if state is None:
            return
        try:
            state.set_locked(not self._signing_unlocked(wallet))
        except RuntimeError:
            pass    # widgets deleted between the emit and its delivery

    # --- tab lifecycle ----------------------------------------------------
    @staticmethod
    def _tab_is_live(state: _TabState, window: 'ElectrumWindow') -> bool:
        """Whether ``state`` still describes a tab the user can actually see in
        ``window``.

        A bookkeeping entry is not proof of a tab on screen. Electrum replaces a
        window without necessarily firing ``close_wallet`` -- ``reload_window``
        (used by the Plugins dialog) calls ``window.close()`` and then builds a
        new window regardless of whether the close was accepted, and
        ``closeEvent`` is free to ``event.ignore()`` on a closing warning. Either
        way the plugin can be left holding a :class:`_TabState` for a window that
        is gone or no longer the one being loaded, and keying the add off mere
        presence in ``_tabs`` would then skip the rebuild and leave that wallet
        with no Liquidity tab at all, for the rest of the session.

        So the entry has to answer two questions, not one: is it for THIS window,
        and is its container still in that window's tab bar.
        """
        if state.window is not window:
            return False
        try:
            return window.tabs.indexOf(state.container) != -1
        except Exception:
            return False    # the C++ window went away under us

    def _add_liquidity_tab(self, window: 'ElectrumWindow', wallet: 'Abstract_Wallet') -> None:
        existing = self._tabs.get(wallet)
        if existing is not None:
            if self._tab_is_live(existing, window):
                return      # already on screen in this window; nothing to do
            # Stale: the window was replaced, or the tab was taken out from under
            # us. Drop the dead handle so the rebuild below is not skipped.
            self.logger.info(
                f"replacing a stale Liquidity tab handle for {_wallet_name(wallet)}")
            self._remove_liquidity_tab(wallet)
        # Every step from here is guarded, because this runs inside the
        # `load_wallet` hook: an exception would propagate into Electrum's
        # run_hook, which logs it and moves on -- costing this wallet its tab
        # silently. One wallet's bad data must never cost another wallet its tab.
        try:
            container, handles = self._build_liquidity_tab(window, wallet)
        except Exception:
            self.logger.exception(
                f"could not build the Liquidity tab for {_wallet_name(wallet)}")
            return
        state = self._tabs[wallet] = _TabState(window, wallet, container, handles)
        # Add the tab BEFORE the first refresh. The refresh reads this wallet's
        # stored history (decision log, providers, partners, uptime), and a single
        # malformed record in there must cost the user stale contents, not the
        # whole tab.
        try:
            window.tabs.addTab(container, read_QIcon("lightning.png"), _("Liquidity"))
        except Exception:
            self.logger.exception(
                f"could not add the Liquidity tab for {_wallet_name(wallet)}")
            self._tabs.pop(wallet, None)
            return
        self.logger.info(f"Liquidity tab added for {_wallet_name(wallet)} "
                         f"({len(self._tabs)} open with a tab)")
        # This wallet is now one of the open ones, so the settings whose effect
        # is process-wide (log capture, the funding floor) have to be re-derived
        # to include it -- even if it is never managed.
        self.apply_process_wide_settings()
        try:
            state.refresh()
        except Exception:
            self.logger.exception(
                f"could not populate the Liquidity tab for {_wallet_name(wallet)}")

    def _remove_liquidity_tab(self, wallet: 'Abstract_Wallet') -> None:
        state = self._tabs.pop(wallet, None)
        if state is None:
            return
        # The unsaved-changes guard is connected to the MAIN window's tab bar,
        # which outlives this tab. Left connected, it would keep a dead tab's
        # widgets alive and ask about them on every tab switch for the rest of the
        # session. (The sub-tab connection needs no such care -- it dies with the
        # QTabWidget it belongs to.)
        try:
            state.drop_unsaved_guard()
        except Exception:
            self.logger.debug("could not disconnect the unsaved-changes guard")
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
        # Only worth a line when it differs from the raw count, which is exactly
        # when it explains something ("at max channels, yet it opened another").
        effective = state.get("effective_channels")
        total = state.get("num_channels")
        if effective is not None and effective != total:
            lines.append(_("counted against max_channels: {} ({} offline "
                           "channel(s) discounted)").format(
                effective, (total - effective) if isinstance(total, int) else "?"))
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
            # "sendable" (not "spendable"): the figure is what Electrum will let
            # us send over this channel, i.e. after its routing-fee reserve, so it
            # reads lower than the channel's local balance by design.
            lines.append(_("chan {}: cap {} local {} remote {} sendable {} active={}").format(
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
                         needle: str, since_ts: float = 0.0) -> Tuple[List[str], int]:
        """The Log tab's content: captured logging plus this wallet's decision
        log, in one chronological, filtered list. Returns ``(rows, hidden)``.

        The captured half is process-global (a Python logger has no idea which
        wallet it is talking about), the decision half is per wallet -- which is
        exactly right: the decisions are this wallet's, the surrounding evidence
        is whatever the plugin was doing at the time.

        ``since_ts`` is the tab's "Clear" watermark: rows at or before it are
        dropped from the VIEW only. It is applied to both halves, because "Clear"
        has to mean the whole view went empty -- clearing the ring alone left every
        decision row on screen (they are re-read from ``wallet.db`` on each
        refresh, so they cannot be cleared by forgetting anything) and read as a
        button that did nothing.

        ``hidden`` counts the decision entries the watermark suppressed, so the
        summary line can say the audit trail was hidden rather than destroyed.
        Captured lines are not counted: ``on_clear`` also empties the ring, so
        there is nothing left of them to have a count of.

        Note a decision entry carrying no usable ``ts`` reads as epoch 0 and so
        sorts -- and hides -- as the oldest thing there is. That is the same
        ordering the merge has always given it; the watermark just makes it
        visible.
        """
        rows: List[tuple] = []
        hidden = 0
        for line in self.log_buffer.snapshot(min_level=min_level):
            if line.ts <= since_ts:
                continue
            rows.append((line.ts, self._fmt_log_row(
                line.ts, line.level_name, line.source, line.message)))
        for entry in self.get_decision_log(wallet):
            if float(entry.get("ts") or 0.0) <= since_ts:
                # Counted before the level filter on purpose: this is "how much of
                # your decision log is hidden right now", not "how much of it
                # would this level have shown".
                hidden += 1
                continue
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
        return out, hidden

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
        c = self.settings(wallet)
        capture_ln_cb = QCheckBox(_("Also capture Electrum Lightning logs"))
        capture_ln_cb.setToolTip(_("Include Electrum's own Lightning and swap logging (peer "
                                   "manager, node rater, channels, routing, submarine swaps) in "
                                   "this view. Useful when the plugin reports that no channel "
                                   "partner is available — that decision is made inside those "
                                   "subsystems. Noisy; off by default."))
        capture_ln_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_LN', False)))

        capture_debug_cb = QCheckBox(_("Capture debug-level logs"))
        capture_debug_cb.setToolTip(_("Force debug-level logging while this is on, including for "
                                      "subsystems that quieten themselves (Electrum pins its "
                                      "nostr client to info level, which hides whether a swap "
                                      "provider ever replied). Electrum's main logger is normally "
                                      "at debug already, so if the hint beside this box says so, "
                                      "ticking it only affects those pinned subsystems. The "
                                      "previous levels are restored when you turn it off."))
        capture_debug_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG', False)))

        # Say out loud that Electrum is already emitting debug records, so the
        # checkbox above does not read as broken when ticking it adds nothing.
        capture_hint = QLabel("")
        capture_hint.setStyleSheet("color: gray;")

        def _refresh_capture_hint() -> None:
            if LogCapture.already_emits_debug():
                capture_hint.setText(_("(Electrum already logs at debug level; the filter above "
                                       "controls what you see)"))
            else:
                capture_hint.setText("")

        buffer_edit = QLineEdit(str(getattr(c, 'INBOUND_LIQUIDITY_LOG_BUFFER_LINES',
                                            DEFAULT_LOG_BUFFER_LINES)))
        buffer_edit.setMaximumWidth(90)
        buffer_edit.setToolTip(_("How many captured lines to keep in memory. Values outside "
                                 "{}–{} are clamped. Applied when you finish editing.").format(
            MIN_LOG_BUFFER_LINES, MAX_LOG_BUFFER_LINES))

        capture_row = QHBoxLayout()
        capture_row.addWidget(capture_ln_cb)
        capture_row.addWidget(capture_debug_cb)
        capture_row.addWidget(capture_hint)
        capture_row.addStretch(1)
        capture_row.addWidget(QLabel(_("Buffer (lines)")))
        capture_row.addWidget(buffer_edit)
        v.addLayout(capture_row)
        _refresh_capture_hint()

        def _apply_capture() -> None:
            setattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_LN', capture_ln_cb.isChecked())
            setattr(c, 'INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG', capture_debug_cb.isChecked())
            self.apply_log_capture_settings()
            _refresh_capture_hint()
            # Re-render now. Without this the view only changes on the next timer
            # tick, and then only if the ring happened to move -- so a toggle that
            # did take effect still looked like it had done nothing.
            _force_refresh()

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
        clear_btn.setToolTip(_("Empty this view. Captured log lines are discarded from memory; "
                               "earlier decision-log entries are only hidden here — nothing is "
                               "deleted, they stay in Actions / Declines / Faults and come back "
                               "in this view when the wallet is reopened. New lines keep "
                               "arriving as they happen."))
        btn_row = QHBoxLayout()
        btn_row.addWidget(summary, 1)
        btn_row.addWidget(feedback)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(clear_btn)
        v.addLayout(btn_row)

        # The "Clear" watermark: rows at or before this epoch are hidden from this
        # view. Deliberately per-tab closure state and NOT persisted -- "Clear" is
        # a thing you did to a window, not a preference, so reopening the wallet
        # gives the decision half back (the only copy of it, in wallet.db, is never
        # touched). Part of `seen` below so the watermark is itself a render input.
        watermark = {"since": 0.0}

        # Cheap change-detection: re-render only when the ring moved, the decision
        # log grew, or the user changed a filter. `-1` forces the first render.
        seen = {"revision": -1, "decisions": -1, "level": None, "needle": None,
                "since": None}

        def _current_rows() -> Tuple[List[str], int]:
            return self._merged_log_rows(
                wallet,
                min_level=int(level_combo.currentData() or 0),
                needle=filter_edit.text().strip(),
                since_ts=watermark["since"])

        def refresh(force: bool = False) -> None:
            try:
                revision = self.log_buffer.revision
                decisions = len(self.get_decision_log(wallet))
                level = level_combo.currentData()
                needle = filter_edit.text().strip()
                since = watermark["since"]
                if not force and (revision, decisions, level, needle, since) == (
                        seen["revision"], seen["decisions"], seen["level"],
                        seen["needle"], seen["since"]):
                    return
                seen.update(revision=revision, decisions=decisions,
                            level=level, needle=needle, since=since)
                rows, hidden = _current_rows()
                bar = view.verticalScrollBar()
                at_bottom = follow_cb.isChecked()
                previous = bar.value()
                view.setPlainText("\n".join(rows))
                bar.setValue(bar.maximum() if at_bottom else min(previous, bar.maximum()))
                stats = self.log_buffer.stats()
                text = _("{} lines shown · {} captured (limit {}) · {} dropped").format(
                    f"{len(rows):,}", f"{stats['count']:,}",
                    f"{stats['max_lines']:,}", f"{stats['dropped']:,}")
                if hidden:
                    # Say it plainly: those entries still exist. A count with no
                    # explanation would read as data loss.
                    text += _(" · {} earlier decision entr{} hidden by Clear "
                              "(still kept)").format(
                        f"{hidden:,}", "y" if hidden == 1 else "ies")
                summary.setText(text)
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
            _refresh_capture_hint()
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
            """Empty the view.

            Two different mechanisms, because the two halves are different kinds of
            thing. The captured ring is ours and memory-only, so it is genuinely
            discarded (the tooltip promises that). The decision log lives in
            wallet.db and is re-read on every refresh, so it cannot be "cleared" at
            all -- it is hidden behind the watermark instead, which is what keeps
            this button non-destructive.

            The watermark is taken AFTER clearing the ring so that a record landing
            between the two lines is hidden rather than left stranded on screen.
            """
            self.log_buffer.clear()
            watermark["since"] = time.time()
            feedback.setStyleSheet("color: gray;")
            feedback.setText(_("View cleared."))
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
    # --- unsaved-changes guard --------------------------------------------
    def _resolve_unsaved(self, window: 'ElectrumWindow',
                         panes: List['_ApplyPane']) -> Optional['_ApplyPane']:
        """Ask what to do about panes holding edits, and do it.

        Returns the pane whose Apply was REFUSED (so the caller can put the user
        back in front of it, where the error message is), or None when it is fine
        to leave -- which covers "applied cleanly", "the user chose to ignore it",
        and "no dialog could be shown".

        Nothing here discards an edit. "Ignore" means exactly that: the typed
        values stay in the widgets, and the tracker records this particular set as
        one the user has already answered for, so switching tabs again does not
        re-ask. Editing further produces a different snapshot and asks again.
        """
        pending = [p for p in panes if p.tracker.needs_prompt()]
        if not pending:
            return None
        names = ", ".join(p.name for p in pending)
        if len(pending) == 1:
            text = _("You have unsaved changes on the {name} tab.").format(
                name=pending[0].name)
        else:
            text = _("You have unsaved changes on these tabs: {names}.").format(
                names=names)
        answer = _ask_two_choices(
            window,
            title=_("Unsaved changes"),
            text=text + "\n\n" + _(
                "Apply them now, or ignore them and leave them unsaved?"),
            accept_label=_("Apply changes"),
            reject_label=_("Ignore"))
        if answer is None:
            # No dialog could be put up. Letting the tab change go through is the
            # only option that does not trap the user on this tab; the edits are
            # still in the widgets, and the hint is still under them.
            self.logger.debug("could not ask about unsaved changes; leaving them")
            return None
        if not answer:
            for pane in pending:
                pane.tracker.acknowledge()
            return None
        for pane in pending:
            try:
                saved = pane.apply()
            except Exception:
                self.logger.exception(f"could not apply the {pane.name} tab")
                return pane
            if not saved:
                # A value was refused. Its own status label now says which and
                # why, so send the user there rather than reporting it twice.
                return pane
        return None

    def _install_sub_tab_guard(self, window: 'ElectrumWindow', tabs: QTabWidget,
                               panes: List['_ApplyPane']) -> Dict[str, Any]:
        """Ask about unsaved edits when a tab of ``tabs`` is navigated away from.

        Factored out of :meth:`_install_unsaved_guard` so the Advanced tab's
        INNER tab bar gets the same treatment: moving between General and Usage
        statistics has to ask, exactly as moving between the outer tabs does.
        The outer call additionally watches the main window; this half only
        watches one QTabWidget.

        Panes are keyed by ``host`` -- the tab they are reached through -- and a
        host may hold SEVERAL panes (that is what nesting means), so every pane
        behind the tab being left is checked together.

        Returns the mutable ``{"index", "busy"}` state, so the caller's own
        corrective navigation can set ``busy`` and not be mistaken for the user
        navigating. The signal connection dies with the widget.
        """
        sub: Dict[str, Any] = {"index": tabs.currentIndex(), "busy": False}
        by_host: Dict[QWidget, List['_ApplyPane']] = {}
        for pane in panes:
            by_host.setdefault(pane.host, []).append(pane)

        def panes_at(index: int) -> List['_ApplyPane']:
            if index < 0:
                return []
            return by_host.get(tabs.widget(index), [])

        def on_sub_tab_changed(index: int) -> None:
            prev, sub["index"] = sub["index"], index
            if sub["busy"] or prev == index:
                return
            leaving = panes_at(prev)
            if not leaving:
                return      # left a read-only tab (a log view); nothing to save
            sub["busy"] = True
            try:
                refused = self._resolve_unsaved(window, leaving)
                if refused is not None:
                    tabs.setCurrentIndex(prev)
                    sub["index"] = prev
                    refused.bring_into_view()
            except Exception:
                self.logger.exception("the unsaved-changes guard failed")
            finally:
                sub["busy"] = False

        tabs.currentChanged.connect(on_sub_tab_changed)
        return sub

    def _install_unsaved_guard(self, window: 'ElectrumWindow',
                               wallet: 'Abstract_Wallet', container: QWidget,
                               tabs: QTabWidget,
                               panes: List['_ApplyPane']) -> Callable[[], None]:
        """Warn about unsaved edits when a tab carrying them is navigated away
        from -- both between the plugin's own sub-tabs and when the whole
        Liquidity tab is left for one of Electrum's.

        Hung off ``currentChanged`` rather than filtering the click or key that
        caused it: that signal is the one place every route into a tab converges
        (mouse, keyboard, ``setCurrentIndex`` from our own code), and anything
        earlier would have to be reimplemented for each. The cost is that the
        switch has already happened by the time we ask -- which is why "Ignore"
        keeps the edits and why only a REFUSED apply navigates back.

        Returns a callable that undoes the main-window connection; the sub-tab
        one dies with the widget.
        """
        # `busy` keeps our own corrective setCurrentIndex from re-entering this.
        sub = self._install_sub_tab_guard(window, tabs, panes)

        # --- and the same question when the Liquidity tab itself is left ---
        main_tabs = getattr(window, "tabs", None)
        if main_tabs is None:
            return lambda: None
        main = {"index": -1, "busy": False}

        def on_main_tab_changed(index: int) -> None:
            prev, main["index"] = main["index"], index
            if main["busy"] or prev == index:
                return
            try:
                left_us = prev >= 0 and main_tabs.widget(prev) is container
            except RuntimeError:
                return      # the window is going away
            if not left_us:
                return
            # Every sub-tab is checked, not just the visible one: a user can leave
            # edits on Advanced, switch to Settings, and then leave the plugin
            # entirely, and all of it is about to go unsaved together.
            main["busy"] = True
            try:
                refused = self._resolve_unsaved(window, panes)
                if refused is not None:
                    main_tabs.setCurrentIndex(prev)
                    main["index"] = prev
                    # ...and put the refused tab in front, since that is where the
                    # message explaining the refusal is.
                    #
                    # Under the SUB-tab guard's busy flag as well: this is our own
                    # corrective navigation, and it must not be mistaken for the
                    # user leaving whichever sub-tab happened to be on screen.
                    #
                    # By HOST, not by widget: a nested pane (Advanced's inner
                    # tabs) is not an entry in this tab bar, so indexOf on its
                    # own widget would return -1 and leave the user looking at
                    # the wrong tab. The host is the outer tab; bring_into_view
                    # then selects the right inner one.
                    i = tabs.indexOf(refused.host)
                    if i != -1:
                        sub["busy"] = True
                        try:
                            tabs.setCurrentIndex(i)
                            sub["index"] = i
                        finally:
                            sub["busy"] = False
                    refused.bring_into_view()
            except Exception:
                self.logger.exception("the unsaved-changes guard failed")
            finally:
                main["busy"] = False

        main_tabs.currentChanged.connect(on_main_tab_changed)

        def disconnect() -> None:
            try:
                main_tabs.currentChanged.disconnect(on_main_tab_changed)
            except Exception:
                # Already gone (window closing), which is the outcome we wanted.
                pass

        return disconnect

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

        c = self.settings(wallet)

        # --- trampoline-routing banner (above everything else) ------------
        # The one-time dialog (see _maybe_warn_trampoline) is asked once per
        # wallet and then never again, whichever way it was answered. That is what
        # was asked for, and it leaves a gap: a user who clicked "No" -- or who
        # dismissed the dialog, or who switched back to trampoline afterwards --
        # has no indication that the plugin is working with one hand tied. So the
        # standing banner, which is not a nag (no dialog, no repetition) but is
        # always true while the condition holds, and carries the remedy next to
        # the diagnosis exactly as the "Unlock wallet…" button does.
        trampoline_banner = _wrapped_label(_(
            "Trampoline routing is on and gossip is off, so this plugin is less "
            "effective: swaps and sink payments cost more, channels can only be "
            "opened to a few central nodes, and peer names are unavailable."))
        trampoline_banner.setStyleSheet(f"color: {_APPLY_HINT_COLOR};")
        trampoline_btn = QPushButton(_("Switch to full gossip mode"))
        trampoline_btn.setToolTip(_(
            "Turn off trampoline routing and sync the Lightning network gossip, "
            "so routes are found locally. This is Electrum's own setting (the "
            "same one in Preferences); it applies to every wallet, and the first "
            "sync uses some bandwidth and disk."))

        def _refresh_trampoline_banner() -> None:
            on = self.trampoline_routing_active()
            trampoline_banner.setVisible(on)
            trampoline_btn.setVisible(on)

        def on_switch_to_gossip() -> None:
            if not self.switch_to_gossip_routing(wallet):
                status.set_error(_("Could not switch on gossip routing."))
                return
            _refresh_trampoline_banner()
            status.set_saved(_("Gossip routing enabled; trampoline is off."))

        trampoline_btn.clicked.connect(on_switch_to_gossip)
        trampoline_row = QHBoxLayout()
        trampoline_row.addWidget(trampoline_btn)
        trampoline_row.addStretch(1)
        vbox.addWidget(trampoline_banner)
        vbox.addLayout(trampoline_row)
        _refresh_trampoline_banner()

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

        # Shown only while the wallet is locked. "wallet locked" is the one
        # resting state the user can clear on the spot, and the control that
        # clears it lives in Electrum's Wallet menu -- not somewhere you would
        # look while reading this tab. So the remedy sits next to the diagnosis.
        unlock_btn = QPushButton(_("Unlock wallet…"))
        unlock_btn.setToolTip(_(
            "Your wallet is password-protected and locked, so nothing can be "
            "signed and automation is paused. Unlocking keeps the password in "
            "memory for this session — the same thing Wallet > Unlock does."))
        unlock_btn.setVisible(False)
        unlock_btn.clicked.connect(lambda: self._prompt_unlock(wallet))

        def _set_locked(locked: bool) -> None:
            unlock_btn.setVisible(bool(locked))

        # Version / update line. Sits with the status rather than in its own
        # section: "am I running something current" is the same kind of question
        # as "is anything happening", and both are read at a glance. Empty (and
        # invisible) when the update check is switched off, so a user who
        # declined it never sees the feature again.
        update_label = _wrapped_label("")
        update_label.setOpenExternalLinks(True)
        update_label.setTextFormat(Qt.TextFormat.RichText)

        def _refresh_update_label() -> None:
            text = self.update_status(wallet)
            update_label.setVisible(bool(text))
            if not text:
                update_label.setText("")
                return
            # Escaped even though both halves are already sanitised upstream
            # (`extract_release` rebuilds the version from digits and accepts
            # only https URLs): this label renders rich text, and the strings
            # originate off-machine. Two independent guards, not one.
            safe_text = html.escape(text)
            if self.update_available(wallet):
                url = html.escape(self.latest_release_url(wallet), quote=True)
                # The link goes to the release page and nothing is downloaded
                # here: updating stays something the user does deliberately.
                update_label.setText(
                    f'<span style="color:#d29922;">{safe_text}</span> '
                    f'&nbsp;<a href="{url}">{_("Release notes")}</a>')
            else:
                update_label.setText(f'<span style="color:gray;">{safe_text}</span>')

        def _set_tick_status(status: str) -> None:
            """Render one tick status. Terminal states are muted, an in-flight
            step is emphasised, so a glance tells you whether anything is
            happening without reading the words."""
            tick_status_label.setText(status)
            # Not a plain TERMINAL_STATUSES membership test: the sleeping state
            # carries a "(next check in ~Xm)" suffix, and exact matching would
            # paint a resting plugin as busy (and stamp it with a "since" time).
            resting = is_terminal_status(status)
            tick_status_label.setStyleSheet(
                "color: gray;" if resting else "color: #2ea043; font-weight: bold;")
            tick_since_label.setText(
                "" if resting else _("since {}").format(_fmt_time(time.time())))
            # Every status change is also a chance for the lock state to have
            # changed (unlocked elsewhere, or locked again mid-session), so the
            # button tracks it here rather than needing its own timer.
            _set_locked(not self._signing_unlocked(wallet))
            # The base plugin re-emits the current status when a check completes,
            # so this is where a newly-found update reaches the tab.
            _refresh_update_label()

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
        sink_label_text = _("Liquidity sink (Lightning address; blank = off)")
        # Cross-validated against each other (and against the goal) in on_apply,
        # which looks the typed values up by label -- so these two are named.
        min_onchain_label_text = _("Min on-chain to open a channel (sat)")
        max_size_label_text = _("Max channel size (sat, 0 = no limit)")
        # Named for the same reason the three above are: the tooltip table below
        # is keyed by label text, so any row that wants one needs a name.
        dev_fee_label_text = _("Dev fee (%, 0–{:g}; 0 = off)").format(DEV_FEE_MAX_PCT)
        # (label, current value as text, parser, setter) for each tunable kept on
        # the main Settings tab. Power-user knobs (on-chain reserve, reliability
        # tuning, offline auto-close, log retention, diagnostics, daily ceilings)
        # and the feature on/off toggles live on the Advanced sub-tab instead.
        fields = [
            (min_onchain_label_text,
             str(c.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT), int,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT', v)),
            (_("Maximum number of channels"),
             str(c.INBOUND_LIQUIDITY_MAX_CHANNELS), int,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_MAX_CHANNELS', v)),
            # Sits with the other two channel-shape knobs (how many, how big, what
            # size to aim for) rather than on Advanced: without it the plugin funds
            # a channel with the whole on-chain balance, so it is a first-order
            # safety bound, not a tuning knob. Third in the list on purpose --
            # index 0/1 of the tab's line-edits stay min-on-chain and max-channels.
            (max_size_label_text,
             str(getattr(c, 'INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT',
                         DEFAULT_MAX_CHANNEL_SIZE_SAT)), int,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT', max(0, int(v)))),
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
            (dev_fee_label_text,
             str(c.INBOUND_LIQUIDITY_DEV_FEE_PCT), float,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_DEV_FEE_PCT',
                               max(0.0, min(float(v), DEV_FEE_MAX_PCT)))),
            # The liquidity sink: pay outbound away over Lightning instead of
            # reverse-swapping it on-chain. Validated in on_apply -- an address
            # that cannot be resolved is refused rather than saved, because a
            # silently-bad sink would look configured while every drain quietly
            # fell back to a swap.
            (sink_label_text,
             str(c.INBOUND_LIQUIDITY_SINK_ADDRESS or ''), str,
             lambda v: setattr(c, 'INBOUND_LIQUIDITY_SINK_ADDRESS', (v or '').strip())),
        ]
        tooltips = {
            # Hovering the dev-fee row should answer "what is this charging me,
            # on what, and where does it go" without sending anyone to the
            # README. Condensed from the ConfigVar's long_desc (see __init__.py),
            # which is the canonical wording.
            dev_fee_label_text: _(
                "An optional contribution to this plugin's development, taken as "
                "a share of what the plugin drains out of a channel.\n\n"
                "It is charged only on drains the plugin performs itself — the "
                "on-chain amount a reverse swap returns, or the amount paid to "
                "your liquidity sink. Your own payments are never touched, and "
                "nothing is charged when the plugin does nothing.\n\n"
                "The fee accrues until at least {thr} sat is owed, then is paid "
                "automatically over Lightning to the payout address, at most "
                "{cap} sat per rolling 24 hours. Range 0–{max:g}%; set it to 0 to "
                "switch it off entirely.").format(
                    thr=f"{DEV_FEE_PAYOUT_THRESHOLD_SAT:,}",
                    cap=f"{DEV_FEE_DAILY_CAP_SAT:,}", max=DEV_FEE_MAX_PCT),
            sink_label_text: _(
                "Instead of doing a swap-out to obtain inbound liquidity, "
                "utilize a liquidity sink instead"),
            max_size_label_text: _(
                "Never fund a single channel with more than this.\n\n"
                "The plugin opens channels with the maximum available on-chain "
                "balance, so without a ceiling a wallet holding significant "
                "on-chain funds would put all of it into one very large channel "
                "with a single peer. With a ceiling, a large balance is spread "
                "over several channels of a sane size instead, up to the maximum "
                "number of channels above.\n\n"
                "Applies to new opens only: a channel that is already larger than "
                "this is never closed for being oversized.\n\n"
                "Must be at least the liquidity goal — a channel below the goal "
                "would be replaced as soon as it was opened. 0 = no ceiling, "
                "bounded only by Electrum's own maximum funding amount."),
        }
        edits = []
        for row, (label, value, parser, setter) in enumerate(fields):
            label_w = QLabel(label)
            edit = QLineEdit(value)
            if label in tooltips:
                label_w.setToolTip(tooltips[label])
                edit.setToolTip(tooltips[label])
            grid.addWidget(label_w, row, 0)
            grid.addWidget(edit, row, 1)
            edits.append((edit, parser, setter, label))

        # --- liquidity goal ------------------------------------------------
        # The one amount on this tab that is entered as MONEY rather than as a
        # bare sat count, so it gets Electrum's own amount widget: it renders and
        # parses in whichever base unit the user has selected (BTC / mBTC / bits
        # / sat), with an optional fiat companion wired through the main window's
        # connect_fields -- exactly the pairing the Send and Receive tabs use.
        # The stored ConfigVar is always sats regardless of what is displayed.
        goal_label_text = _("Liquidity goal")
        goal_tooltip = _("Automatically close small channels and re-open bigger "
                         "channels if we have sufficient funds to reach this goal")
        goal_row = len(fields)
        goal_label = QLabel(goal_label_text)
        goal_label.setToolTip(goal_tooltip)
        grid.addWidget(goal_label, goal_row, 0)
        # Fall back to BTC if the window predates get_decimal_point: a settings
        # tab that fails to build is far worse than one showing the wrong unit.
        get_decimal_point = getattr(window, "get_decimal_point", None) or (lambda: 8)
        goal_e = BTCAmountEdit(get_decimal_point)
        goal_e.setToolTip(goal_tooltip)
        goal_e.setAmount(self._liquidity_goal_sat(wallet))
        grid.addWidget(goal_e, goal_row, 1)
        fx = getattr(window, "fx", None)
        goal_fiat_e = None
        if fx is not None and hasattr(window, "connect_fields"):
            goal_fiat_e = AmountEdit(fx.get_currency)
            goal_fiat_e.setToolTip(goal_tooltip)
            goal_fiat_e.setVisible(bool(fx.is_enabled()))
            grid.addWidget(goal_fiat_e, goal_row, 2)
            window.connect_fields(goal_e, goal_fiat_e)

        def _reload_goal() -> None:
            """Re-render the goal field from the PERSISTED sat value.

            Deliberately not Electrum's ``refresh_amount_edits`` behaviour, which
            re-reads the widget's own text and so *reinterprets* the typed number
            under the new unit (1 BTC becomes 1 sat). That is defensible for a
            transient send amount, but this field mirrors a stored setting: the
            amount does not change because the user changed how amounts are
            displayed, so it is re-rendered from config instead. (This also has to
            be wired up by hand -- ``refresh_amount_edits`` only knows about the
            Send and Receive tabs' own fields.)

            Guarded because this is also a signal slot on the long-lived QApplication
            while the widgets belong to one wallet's tab. The slot is a plain
            closure, not a QObject, so Qt cannot auto-disconnect it when the tab
            goes away -- and touching a deleted widget raises RuntimeError. Closing
            a wallet and then changing the base unit must not put a traceback in the
            user's log."""
            try:
                goal_e.setAmount(self._liquidity_goal_sat(wallet))
                if goal_fiat_e is not None:
                    goal_fiat_e.setVisible(bool(fx.is_enabled()))
            except RuntimeError:
                return      # the tab (and its widgets) are gone

        app = getattr(window, "app", None)
        for _signal_name in ("refresh_amount_edits_signal", "update_fiat_signal"):
            _signal = getattr(app, _signal_name, None)
            if _signal is not None:
                _signal.connect(_reload_goal)
        vbox.addLayout(grid)

        # The two switches that qualify the sink address. Both sit directly under
        # it and apply immediately on toggle (like the plugin-opened-only switch
        # above) rather than waiting for Apply.
        #
        # Defer-until-goal first, because it is the one that ships ON: until the
        # wallet holds its full complement of channels with none below the
        # liquidity goal, a drain is reverse-swapped (the balance comes back
        # on-chain, where it can fund the next channel) instead of being paid
        # away to the sink.
        defer_sink_cb = QCheckBox(
            _("Don't use liquidity sink until inbound liquidity goal is met"))
        defer_sink_cb.setToolTip(_(
            "On by default. Paying the sink sends a channel's outbound to an "
            "account outside this wallet, which starves the on-chain balance the "
            "plugin needs to open channels that reach your liquidity goal.\n\n"
            "While this is on, a channel over its trigger is reverse-swapped "
            "instead -- the same balance comes back as on-chain coins -- and the "
            "sink takes over once the goal is met: the maximum number of channels "
            "is held, and none of the ones the plugin opened is below the goal.\n\n"
            "With submarine swaps also disabled, nothing drains a channel until "
            "the goal is met. The plugin says so in its decision log rather than "
            "pay your outbound away against your wishes."))
        defer_sink_cb.setChecked(self._defer_sink_until_goal(wallet))

        sink_only_cb = QCheckBox(_("Disable submarine swaps (use the liquidity sink only)"))
        sink_only_cb.setToolTip(_(
            "When on, outbound is only ever drained by paying the liquidity sink; "
            "a reverse swap is never made, not even when the sink fails.\n\n"
            "Turning this on without a liquidity sink leaves nothing able to drain "
            "a channel. The plugin will say so in its decision log rather than "
            "reverse-swap against your wishes."))
        sink_only_cb.setChecked(
            bool(getattr(c, 'INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS', False)))

        def _sink_only_warning() -> str:
            """The warning text for the current combination of sink settings, or
            "". Both cases it catches are legal but leave the plugin with nothing
            able to drain a channel, which is worth saying out loud at the moment
            the user creates the situation rather than only in the decision log.
            """
            if not bool(getattr(c, 'INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS', False)):
                return ""
            if not self._liquidity_sink_address(wallet):
                return _("Submarine swaps are disabled but no liquidity sink is set: "
                         "nothing can drain a channel until you set one.")
            if self._defer_sink_until_goal(wallet):
                return _("Submarine swaps are disabled and the liquidity sink is held "
                         "back until the liquidity goal is met: nothing can drain a "
                         "channel until then.")
            return ""

        def _show_sink_warning() -> None:
            warning = _sink_only_warning()
            if warning:
                # Sticky: a configuration that leaves nothing able to drain a
                # channel stays on screen even once there are also unsaved edits
                # below it (see _StatusLine).
                status.set_error(warning)
            else:
                status.set_message("")

        def on_defer_sink(checked: bool) -> None:
            setattr(c, 'INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL', bool(checked))
            _show_sink_warning()

        def on_sink_only(checked: bool) -> None:
            setattr(c, 'INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS', bool(checked))
            _show_sink_warning()

        defer_sink_cb.toggled.connect(on_defer_sink)
        sink_only_cb.toggled.connect(on_sink_only)
        vbox.addWidget(defer_sink_cb)
        vbox.addWidget(sink_only_cb)

        # Live read-out of the dev-fee ledger: sats owed (accrued, not yet paid)
        # and sats paid out in the trailing 24h (against the daily cap).
        dev_fee_label = QLabel("")
        # The same explanation as the dev-fee input row above: the two widgets are
        # one section as far as a user hovering over it is concerned, and they
        # should not have to find the right half of it to get an answer.
        dev_fee_label.setToolTip(tooltips[dev_fee_label_text])

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
        hint_label = QLabel(_StatusLine.HINT_TEXT)
        status = _StatusLine(status_label, hint_label)
        vbox.addWidget(status_label)
        vbox.addWidget(hint_label)

        # --- unsaved-changes tracking --------------------------------------
        # The goal is snapshotted as its PARSED sat amount rather than as the text
        # in the box, because that box is re-rendered from storage whenever the
        # user changes Electrum's base unit (see _reload_goal): 0.0002 BTC and
        # 20000 sat are the same stored setting, and switching between the two
        # displays is not an edit. Every other field is its text, which is exactly
        # what Apply will parse.
        def _settings_snapshot() -> Tuple:
            return (tuple(edit.text() for edit, _p, _s, _l in edits),
                    goal_e.get_amount())

        dirty = _DirtyTracker(_("Settings"), _settings_snapshot, status)
        dirty.watch(*(edit.textChanged for edit, _p, _s, _l in edits))
        dirty.watch(goal_e.textChanged)

        apply_btn = primary_button(_("Apply"))

        def on_apply() -> bool:
            """Validate and persist the form. Returns whether anything was saved,
            so the unsaved-changes guard can tell a refused value (stay on this
            tab and show why) from a successful save."""
            # Parse and validate everything before persisting anything.
            parsed = []
            by_label = {}    # label -> parsed value, for the cross-field checks
            for edit, parser, setter, label in edits:
                text = edit.text().strip()
                try:
                    value = parser(text) if parser is not str else text
                except ValueError:
                    status.set_error(_("Invalid value for: {}").format(label))
                    return False
                parsed.append((setter, value))
                by_label[label] = value
                # A sink address that cannot be resolved to an LNURL-pay target is
                # refused rather than saved: stored but unusable, it would read as
                # "configured" on this tab while every drain silently fell back to
                # a reverse swap. Blank is fine -- that is how the feature is
                # turned off.
                if label == sink_label_text and text and \
                        self._resolve_lnurl_pay_url(text) is None:
                    status.set_error(_(
                        "Not a Lightning address, LNURL or LNURL-pay URL: {}").format(
                            text[:60]))
                    return False
            # The goal comes from the amount widget (already in sat). An empty or
            # unparseable field is an error rather than "0": silently reading a
            # cleared box as 0 would DISABLE the feature, which is not something a
            # user should be able to do by accident. Typing an explicit 0 still
            # turns it off.
            goal_amount = goal_e.get_amount()
            if goal_amount is None or int(goal_amount) < 0:
                status.set_error(_("Invalid value for: {}").format(goal_label_text))
                return False
            parsed.append((lambda v: setattr(c, 'INBOUND_LIQUIDITY_GOAL_SAT', v),
                           int(goal_amount)))
            # --- max channel size, cross-checked against the rest of the form --
            # Validated against the values being applied in THIS click, not the
            # stored ones, so a user raising the goal and the ceiling together is
            # judged on the pair they actually typed.
            max_size = int(by_label.get(max_size_label_text, 0))
            if max_size < 0:
                status.set_error(_("Value cannot be negative: {}").format(
                    max_size_label_text))
                return False
            if max_size > 0:
                # Below the goal, every channel the plugin opened would be
                # undersized the moment it existed. The engine declines to act on
                # that rather than churning, so saving it would leave the plugin
                # visibly configured and quietly stuck -- refuse it here, where the
                # user can see both numbers at once.
                goal_sat = int(goal_amount)
                if 0 < goal_sat and max_size < goal_sat:
                    status.set_error(_(
                        "Max channel size ({size} sat) is below the liquidity goal "
                        "({goal} sat): every channel opened would immediately count "
                        "as undersized. Raise the max channel size or lower the "
                        "goal.").format(size=max_size, goal=goal_sat))
                    return False
                # Below the funding floor, no channel can be opened at all. The
                # floor tracks min-on-chain-to-open, which is also on this form, so
                # ask what it will be once this click is applied.
                floor = self.effective_min_funding_sat(
                    wallet, by_label.get(min_onchain_label_text))
                if max_size < floor:
                    status.set_error(_(
                        "Max channel size ({size} sat) is below the channel-funding "
                        "floor ({floor} sat): no channel could be opened at all.").format(
                            size=max_size, floor=floor))
                    return False
            # (Automation on/off is owned by the slider above and applied
            # immediately, so the Apply button never touches it. The feature
            # toggles and tuning knobs live on the Advanced sub-tab.)
            #
            # One batch, so this form is one write to the wallet file rather
            # than one per field -- and so a crash cannot leave it half applied.
            with self.settings_batch(wallet):
                for setter, value in parsed:
                    setter(value)
            # min_onchain may have changed: re-assert the channel-funding floor so
            # a lowered floor keeps matching the (new) configured value at once.
            # (The batch already did this; kept for a caller that bypasses it.)
            self._enforce_min_funding_floor()
            # Muted: both of these rewrite the very widgets the tracker is
            # watching, and they are writing the values just persisted -- so the
            # re-baseline below is the only verdict that should come out of it.
            with dirty.muted():
                self._reload_settings_fields(wallet, edits, _sync_toggle_from_config)
                _reload_goal()
            _refresh_dev_fee_label()
            # The form now matches the wallet, so there is nothing outstanding --
            # including any edit the user had previously chosen to leave unsaved.
            dirty.reset()
            # Saving a sink setting that leaves nothing able to drain a channel
            # (no sink at all, or a sink held back until the goal, with swaps
            # disabled either way) is legal but inert; say so instead of
            # reporting a bare success.
            warning = _sink_only_warning()
            if warning:
                status.set_error(_("Settings saved.") + " " + warning)
                return True
            status.set_saved(_("Settings saved."))
            return True

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
        vbox.addWidget(update_label)
        unlock_row = QHBoxLayout()
        unlock_row.addWidget(unlock_btn)
        unlock_row.addStretch(1)
        vbox.addLayout(unlock_row)
        tabs.addTab(settings_tab, _("Settings"))

        # --- Swap providers sub-tab ---------------------------------------
        providers_tab, repopulate_providers, providers_pane = \
            self._build_providers_tab(wallet)
        tabs.addTab(providers_tab, _("Swap providers"))

        # --- Channel partners sub-tab -------------------------------------
        partners_tab, repopulate_partners, partners_pane = \
            self._build_channel_partners_tab(wallet)
        tabs.addTab(partners_tab, _("Channel partners"))

        # --- Advanced sub-tab (last config tab, before the read-only logs) -
        # Two panes, not one: Advanced hosts an inner tab bar (General / Usage
        # statistics) and each inner tab is separately Apply-gated.
        advanced_tab, repopulate_advanced, advanced_panes = \
            self._build_advanced_tab(window, wallet)
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

        # --- unsaved-changes guard over every Apply-gated pane -------------
        # Every pane in one list, in tab order, so the guard below can ask "which
        # of these has outstanding edits" without knowing anything about them.
        # Advanced contributes two (its inner tabs), both hosted by the one
        # outer tab -- which the guard handles via _ApplyPane.host.
        panes = [
            _ApplyPane(_("Settings"), settings_tab, dirty, on_apply),
            providers_pane, partners_pane, *advanced_panes,
        ]
        drop_unsaved_guard = self._install_unsaved_guard(
            window, wallet, container, tabs, panes)

        def refresh() -> None:
            _sync_toggle_from_config()
            _refresh_trampoline_banner()
            # Re-read the manual-run-only checkbox without re-firing its handler.
            manual_only_cb.blockSignals(True)
            manual_only_cb.setChecked(bool(getattr(c, 'INBOUND_LIQUIDITY_MANUAL_RUN_ONLY', False)))
            manual_only_cb.blockSignals(False)
            plugin_only_cb.blockSignals(True)
            plugin_only_cb.setChecked(
                bool(getattr(c, 'INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY', True)))
            plugin_only_cb.blockSignals(False)
            # The two sink switches, same treatment: they can be changed from
            # outside this tab (the cmdline plugin, a hand-edited config), and a
            # stale tick-box next to the sink address would misreport whether the
            # sink is actually in use.
            defer_sink_cb.blockSignals(True)
            defer_sink_cb.setChecked(self._defer_sink_until_goal(wallet))
            defer_sink_cb.blockSignals(False)
            sink_only_cb.blockSignals(True)
            sink_only_cb.setChecked(
                bool(getattr(c, 'INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS', False)))
            sink_only_cb.blockSignals(False)
            _set_tick_status(self.tick_status(wallet))
            _refresh_update_label()
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
            "set_locked": _set_locked,
            "unlock_button": unlock_btn,
            "sub_tabs": tabs,
            "panes": panes,
            "drop_unsaved_guard": drop_unsaved_guard,
            "refresh_trampoline_banner": _refresh_trampoline_banner,
            "trampoline_banner": trampoline_banner,
            "trampoline_button": trampoline_btn,
        }

    # --- advanced sub-tab -------------------------------------------------
    def _build_advanced_tab(self, window: 'ElectrumWindow',
                            wallet: 'Abstract_Wallet'):
        """Build the Advanced sub-tab, which hosts an inner tab bar:

          * **General** -- the feature on/off toggles, the daily (rolling-24h)
            action ceilings (a runaway guard), the power-user tuning knobs
            (on-chain reserve, reliability tuning, offline auto-close, log
            retention) moved off the main Settings tab, and the
            reset-to-defaults button. Everything that was on Advanced before.
          * **Usage statistics** -- the opt-in anonymous reporting controls, and
            the live preview of exactly what the chosen level would send.

        Returns ``(widget, repopulate, panes)``. Note the plural: this tab
        contributes TWO entries to the unsaved-changes guard, one per inner tab,
        both declaring the returned widget as their ``host`` (see
        :class:`_ApplyPane` for why the guard needs that).

        General is added to the inner bar FIRST, which is load-bearing beyond
        appearance: several tests reach this tab's daily-ceiling line edits as
        ``findChildren(QLineEdit)[:2]``, and that index only holds while
        General's widgets are constructed and parented ahead of the usage
        pane's. The usage pane also uses radio buttons rather than check boxes,
        so ``findChildren(QCheckBox)`` on this tab still returns exactly the
        feature toggles -- one test flips every one of them off and would
        otherwise be silently switching off reporting too.

        Takes the window because the reset asks for confirmation first, and the
        confirmation is a modal on the window that owns this tab.
        """
        from . import DEFAULT_MAX_OPENS_PER_DAY, DEFAULT_MAX_CLOSES_PER_DAY

        c = self.settings(wallet)
        general = QWidget()
        v = QVBoxLayout(general)
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

        auto_remediate_cb = QCheckBox(_("Close wedged channel opens"))
        auto_remediate_cb.setToolTip(_("When a channel open stays wedged, close it to free the funds. "
                                       "Time spent waiting for the funding transaction to confirm "
                                       "does not count, the channel must look wedged on several "
                                       "checks spread over time, and a cooperative close is always "
                                       "attempted first; a force-close (which broadcasts a tx and "
                                       "incurs a mining fee) is the last resort."))
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

        # The other side of the first-run opt-in dialog: where a user goes to
        # change their mind either way.
        update_check_cb = QCheckBox(_("Check GitHub for plugin updates"))
        update_check_cb.setToolTip(_("Once a day, ask GitHub whether a newer release of this plugin "
                                     "has been published, and show it in the Status footer. Contacts "
                                     "github.com through your configured proxy, which tells GitHub "
                                     "this wallet is running. Nothing is downloaded or installed."))
        v.addWidget(update_check_cb)

        # NB: the scope switch ("Only manage channels the plugin opened") lives on
        # the main Settings tab, and the Log-tab capture switches live on the Log
        # tab itself — each next to what it affects.
        checkboxes = [
            (reliability_cb, 'INBOUND_LIQUIDITY_RELIABILITY_ENABLED'),
            (peer_reliability_cb, 'INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED'),
            (auto_remediate_cb, 'INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN'),
            (autoclose_cb, 'INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED'),
            (diag_log_cb, 'INBOUND_LIQUIDITY_DIAG_LOG_ENABLED'),
            (update_check_cb, 'INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED'),
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
            (_("Minimum swap size (sat, 0 = no minimum)"),
             'INBOUND_LIQUIDITY_MIN_SWAP_SAT', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_MIN_SWAP_SAT', max(0, int(val)))),
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
            (_("Force-close a wedged channel open after (minutes)"),
             'INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN',
                                 max(1, int(val)))),
            (_("Wedged-open: checks required before closing"),
             'INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS', max(1, int(val)))),
            (_("Wedged-open: those checks must span (minutes)"),
             'INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN', max(0, int(val)))),
            (_("Wait for a cooperative close before forcing (minutes)"),
             'INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN', max(0, int(val)))),
            (_("Confirming open blocks new opens for (days)"),
             'INBOUND_LIQUIDITY_CONFIRMING_FREEZE_DAYS', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_CONFIRMING_FREEZE_DAYS', max(0.0, val))),
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
            # How long a peer must be gone before its channel stops occupying a
            # slot. Sits immediately above the allowance, which bounds what that
            # discount is allowed to buy.
            (_("Treat a channel as offline after (hours, 0 = off)"),
             'INBOUND_LIQUIDITY_OFFLINE_DISCOUNT_AFTER_HOURS', float,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_OFFLINE_DISCOUNT_AFTER_HOURS',
                                 max(0.0, float(val)))),
            # How far the offline discount may push the real channel count over
            # the maximum. 0 switches the discount off, so an offline channel
            # keeps occupying its slot as it did before the feature existed.
            (_("Extra channels while replacing offline ones (0 = off)"),
             'INBOUND_LIQUIDITY_OFFLINE_OPEN_ALLOWANCE', int,
             lambda val: setattr(c, 'INBOUND_LIQUIDITY_OFFLINE_OPEN_ALLOWANCE',
                                 max(0, int(val)))),
        ]
        field_rows = []  # (edit, attr, parser, setter, label)
        for (label, attr, parser, setter) in fields:
            grid.addWidget(QLabel(label), row, 0)
            edit = QLineEdit(str(getattr(c, attr)))
            grid.addWidget(edit, row, 1)
            field_rows.append((edit, attr, parser, setter, label))
            row += 1
        v.addLayout(grid)

        status_label = QLabel("")
        hint_label = QLabel(_StatusLine.HINT_TEXT)
        status = _StatusLine(status_label, hint_label)
        v.addWidget(status_label)
        v.addWidget(hint_label)

        # --- unsaved-changes tracking --------------------------------------
        # Every widget on this tab waits for Apply, so the snapshot is simply all
        # of them.
        def _advanced_snapshot() -> Tuple:
            return (tuple(cb.isChecked() for cb, _a in checkboxes),
                    tuple(edit.text() for edit, _a, _d, _u, _k in ceiling_rows),
                    tuple(edit.text() for edit, _a, _p, _s, _l in field_rows))

        dirty = _DirtyTracker(_("Advanced"), _advanced_snapshot, status)
        dirty.watch(*(cb.toggled for cb, _a in checkboxes))
        dirty.watch(*(edit.textChanged for edit, _a, _d, _u, _k in ceiling_rows))
        dirty.watch(*(edit.textChanged for edit, _a, _p, _s, _l in field_rows))

        def repopulate() -> None:
            # This runs on ordinary wallet events (see the Liquidity tab's
            # refresh), not only when the tab is opened -- so while the user has
            # edits in flight it must not reach into the fields and overwrite
            # them. A channel update arriving mid-sentence used to silently revert
            # whatever was being typed here.
            #
            # The read-only 24h usage read-outs are refreshed either way: they
            # report what the wallet has done, which has nothing to do with what
            # is pending in the form.
            keep_edits = dirty.is_dirty()
            with dirty.muted():
                if not keep_edits:
                    for cb, attr in checkboxes:
                        cb.setChecked(bool(getattr(c, attr)))
                for edit, attr, default, usage, kind in ceiling_rows:
                    if not keep_edits:
                        edit.setText(str(getattr(c, attr, default)))
                    used = self._count_actions_last_24h(wallet, kind)
                    usage.setText(_("{} in last 24h").format(used))
                if not keep_edits:
                    for edit, attr, parser, setter, label in field_rows:
                        edit.setText(str(getattr(c, attr)))
            if not keep_edits:
                # The fields were just re-rendered from storage, so they agree
                # with it by construction.
                dirty.reset()

        def on_apply() -> bool:
            """Validate and persist this tab. Returns whether anything was saved
            -- see the Settings tab's on_apply."""
            # Validate everything before persisting anything.
            parsed_ceilings = []
            for edit, attr, default, usage, kind in ceiling_rows:
                text = edit.text().strip()
                try:
                    value = int(text)
                except ValueError:
                    status.set_error(_("Invalid value: {}").format(text))
                    return False
                if value < 0:
                    status.set_error(_("Value cannot be negative: {}").format(text))
                    return False
                parsed_ceilings.append((attr, value))
            parsed_fields = []
            for edit, attr, parser, setter, label in field_rows:
                text = edit.text().strip()
                try:
                    parsed_fields.append((setter, parser(text)))
                except ValueError:
                    status.set_error(_("Invalid value for: {}").format(label))
                    return False
            # One batch for the whole form: see the note on the Settings tab's
            # Apply. This tab carries two dozen fields, so it matters most here.
            with self.settings_batch(wallet):
                for attr, value in parsed_ceilings:
                    setattr(c, attr, value)
                for cb, attr in checkboxes:
                    setattr(c, attr, cb.isChecked())
                for setter, value in parsed_fields:
                    setter(value)
            # Clean by the time repopulate runs, so it re-renders every field from
            # the clamped stored values (which is how a user sees their 999-day log
            # retention come back as 90) and re-baselines.
            dirty.reset()
            repopulate()
            status.set_saved(_("Advanced settings saved."))
            return True

        # --- back to the defaults -----------------------------------------
        # Settings are this wallet's own, which raises a question the plugin
        # owes an answer to: how do I undo them? Clearing the wallet's values
        # is one action (they live in one store), and it is the only way back
        # for a wallet that inherited a tuned installation default it did not
        # want -- a newly created wallet on an installation configured before
        # this version, say.
        reset_btn = QPushButton(_("Reset this wallet to defaults"))
        reset_btn.setToolTip(_(
            "Discard every Inbound Liquidity setting stored for THIS wallet, so "
            "all of them go back to the defaults. Other wallets are not "
            "touched, and nothing about your channels, swaps or funds changes."))

        def on_reset() -> None:
            try:
                confirmed = bool(window.question(
                    _("Discard every Inbound Liquidity setting stored for this "
                      "wallet ({name}) and go back to the defaults?\n\n"
                      "No other wallet is affected. Your channels, swaps and "
                      "funds are not touched.").format(name=_wallet_name(wallet)),
                    title=_("Reset this wallet's settings?")))
            except Exception:
                self.logger.exception("could not ask about resetting the settings")
                return
            if not confirmed:
                return
            self.reset_settings(wallet)
            # Cleared BEFORE the refresh below, not after: the refresh runs this
            # tab's repopulate, which deliberately leaves the fields alone while
            # they are dirty. The user has just asked for the defaults, so any
            # pending edit is exactly what must NOT be preserved here.
            dirty.reset()
            # Every field on every sub-tab is now stale, so refresh the whole
            # tab rather than this one's fields: the Settings tab's slider and
            # amount box read the same store.
            state = self._tabs.get(wallet)
            if state is not None:
                try:
                    state.refresh()
                except Exception:
                    self.logger.exception("could not refresh the tab after a reset")
            else:
                repopulate()
            status.set_saved(_("Settings reset to the defaults."))

        reset_btn.clicked.connect(on_reset)

        apply_btn = primary_button(_("Apply"))
        apply_btn.clicked.connect(on_apply)
        btn_row = QHBoxLayout()
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        v.addLayout(btn_row)
        v.addStretch(1)

        repopulate()
        dirty.reset()

        # --- the inner tab bar ---------------------------------------------
        # General is parented into the inner bar BEFORE the usage pane is even
        # built, so it is unambiguously the first subtree a findChildren() walk
        # over this tab reaches. See the docstring: tests address General's
        # widgets positionally, and that only holds while this order does.
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)
        inner = QTabWidget()
        inner.addTab(general, _("General"))
        outer.addWidget(inner)

        usage_widget, repopulate_usage, usage_pane_parts = \
            self._build_usage_stats_pane(window, wallet)
        inner.addTab(usage_widget, _("Usage statistics"))

        panes = [
            _ApplyPane(_("Advanced"), general, dirty, on_apply, host=tab,
                       focus=lambda: inner.setCurrentWidget(general)),
            _ApplyPane(_("Usage statistics"), usage_widget,
                       usage_pane_parts["tracker"], usage_pane_parts["apply"],
                       host=tab,
                       focus=lambda: inner.setCurrentWidget(usage_widget)),
        ]
        # Moving between the two inner tabs asks about unsaved edits too, the
        # same way moving between the outer tabs does.
        self._install_sub_tab_guard(window, inner, panes)

        def repopulate_all() -> None:
            repopulate()
            repopulate_usage()

        return tab, repopulate_all, panes

    # --- usage-statistics pane (inside Advanced) --------------------------
    #
    # The settings half of the opt-in analytics: where a user who answered the
    # consent screen goes to change their mind, in either direction.
    #
    # Deliberate choices here, all for the same reason -- a reporting control
    # the user does not fully believe is worse than no reporting at all:
    #
    #   * RADIO BUTTONS, including an explicit "Off". A level is one choice out
    #     of four, not three independent switches, and "Off" being a visible
    #     option (rather than "none of the boxes ticked") makes the current
    #     state unambiguous. They also keep this pane out of
    #     ``findChildren(QCheckBox)`` on the Advanced tab -- see the note in
    #     _build_advanced_tab.
    #   * The PREVIEW UPDATES AS THE SELECTION MOVES, before anything is saved,
    #     and shows this wallet's real values rather than a sample. It is
    #     rendered by `usage_stats_preview`, i.e. by the same builder that makes
    #     the real report, so it cannot drift from what would actually be sent.
    #   * Apply-gated like every other settings pane, so moving the radio does
    #     not silently start reporting; and the unsaved-changes guard covers it,
    #     so the choice cannot be lost by navigating away.
    def _build_usage_stats_pane(self, window: 'ElectrumWindow',
                                wallet: 'Abstract_Wallet'):
        """Build the Usage statistics pane. Returns ``(widget, repopulate,
        parts)``, where ``parts`` carries the dirty tracker and the apply
        callable the surrounding tab needs to register it with the guard."""
        from . import usage_stats

        descriptions = usage_stats.level_descriptions()

        # Scrollable: the four options with their bullets, plus a preview of a
        # 50-field report, is taller than the tab at most window sizes, and a
        # consent control the user has to guess the bottom of is not one.
        pane = QWidget()
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        v.addWidget(_wrapped_label(_(
            "Help us understand how many people run this plugin, which settings "
            "matter, and how much liquidity it actually provisions. This is OFF "
            "by default and entirely optional — the plugin works exactly the "
            "same either way. If you switch it on, this wallet sends one small "
            "report now and then one a week.")))

        v.addWidget(_wrapped_label(_("We never send, at any level:")))
        for label in _bullet_labels([_(item) for item in usage_stats.never_sent_items()]):
            v.addWidget(label)

        privacy_label = _wrapped_label("")
        privacy_label.setTextFormat(Qt.TextFormat.RichText)
        privacy_label.setOpenExternalLinks(True)
        privacy_label.setText(
            f'<a href="{_USAGE_PRIVACY_URL}">{html.escape(_("Read the privacy policy"))}</a>')
        v.addWidget(privacy_label)

        # Why nothing can be sent right now, when that is the case (the endpoint
        # kill switch, or a regtest wallet). Shown even when the user has opted
        # in, because otherwise this pane would claim to be reporting when it
        # provably is not.
        blocked_label = _wrapped_label("")
        blocked_label.setStyleSheet(f"color: {_APPLY_HINT_COLOR};")
        v.addWidget(blocked_label)

        # --- the four choices ----------------------------------------------
        v.addWidget(QLabel(_("How much to share:")))
        group = QButtonGroup(pane)
        group.setExclusive(True)
        buttons: List[Tuple[QRadioButton, int]] = []

        off_button = QRadioButton(_("Off — send nothing (the default)"))
        off_button.setToolTip(_("No report ever leaves this wallet."))
        group.addButton(off_button)
        buttons.append((off_button, usage_stats.LEVEL_OFF))
        v.addWidget(off_button)

        for level in usage_stats.LEVELS:
            description = descriptions[level]
            button = QRadioButton(f"{level} · {_(description.title)}")
            button.setToolTip(_(description.summary))
            group.addButton(button)
            buttons.append((button, level))
            v.addWidget(button)
            for label in _bullet_labels([_(item) for item in description.items],
                                        indent=34):
                v.addWidget(label)

        # --- the preview ---------------------------------------------------
        v.addWidget(QLabel(_("Exactly what would be sent:")))
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        preview_font = QFont("monospace")
        preview_font.setStyleHint(QFont.StyleHint.Monospace)
        preview.setFont(preview_font)
        preview.setMinimumHeight(220)
        v.addWidget(preview)

        status_label = QLabel("")
        hint_label = QLabel(_StatusLine.HINT_TEXT)
        status = _StatusLine(status_label, hint_label)
        report_label = _wrapped_label("")
        report_label.setStyleSheet("color: gray;")
        v.addWidget(report_label)
        v.addWidget(status_label)
        v.addWidget(hint_label)

        def _selected_level() -> int:
            for button, level in buttons:
                if button.isChecked():
                    return level
            return usage_stats.LEVEL_OFF

        def _refresh_preview() -> None:
            """Render the preview for the CURRENTLY SELECTED level, saved or
            not. Never raises: a preview that cannot be built must not take the
            settings tab down with it."""
            level = _selected_level()
            if level == usage_stats.LEVEL_OFF:
                preview.setPlainText(
                    _("Nothing is sent while this is off."))
                return
            try:
                preview.setPlainText(self.usage_stats_preview(wallet, level))
            except Exception as e:
                self.logger.exception("could not build the usage-report preview")
                preview.setPlainText(_("Could not build the preview: {}").format(
                    scrub_text(f"{type(e).__name__}: {e}")[:200]))

        def _refresh_report_label() -> None:
            info = self.usage_stats_status(wallet)
            parts: List[str] = []
            if info["last_sent_ts"]:
                parts.append(_("Last report sent {}.").format(
                    _fmt_time(info["last_sent_ts"])))
            elif info["level"]:
                parts.append(_("No report has been sent yet."))
            if info["last_error"]:
                parts.append(_("Last attempt failed: {}").format(info["last_error"]))
            if info["install_id"]:
                parts.append(_("This wallet's random identifier: {}").format(
                    info["install_id"]))
            report_label.setText("  ".join(parts))
            report_label.setVisible(bool(parts))

        def _refresh_blocked() -> None:
            reason = self.usage_stats_blocked_reason()
            blocked_label.setText(reason or "")
            blocked_label.setVisible(bool(reason))

        for button, _level in buttons:
            button.toggled.connect(lambda _checked: _refresh_preview())

        def _usage_snapshot() -> Tuple:
            return (_selected_level(),)

        dirty = _DirtyTracker(_("Usage statistics"), _usage_snapshot, status)
        dirty.watch(*(button.toggled for button, _level in buttons))

        def repopulate() -> None:
            """Re-read the stored level into the radios.

            Leaves the selection alone while the pane is dirty, like every other
            repopulate here: a background refresh must not discard a choice the
            user has made but not yet applied.
            """
            _refresh_blocked()
            _refresh_report_label()
            if dirty.is_dirty():
                return
            stored = self.usage_stats_level(wallet)
            for button, level in buttons:
                if level == stored:
                    # blockSignals so re-rendering the stored value does not
                    # look like an edit to the tracker.
                    button.blockSignals(True)
                    button.setChecked(True)
                    button.blockSignals(False)
            _refresh_preview()
            dirty.reset()

        def on_apply() -> bool:
            level = _selected_level()
            try:
                self.set_usage_stats_choice(wallet, level)
            except Exception as e:
                self.logger.exception("could not save the usage-statistics choice")
                status.set_error(_("Could not save: {}").format(
                    scrub_text(f"{type(e).__name__}: {e}")[:200]))
                return False
            dirty.reset()
            _refresh_blocked()
            _refresh_report_label()
            _refresh_preview()
            if level == usage_stats.LEVEL_OFF:
                status.set_saved(_("Usage statistics are off. Nothing will be sent."))
                return True
            # Opting in sends one report straight away rather than at the next
            # weekly slot, so the choice visibly does something -- and so the
            # status line below can report a real outcome instead of "no report
            # has been sent yet" for a week.
            if self.usage_stats_blocked_reason() is None:
                self._request_usage_report(wallet, force=True)
                status.set_saved(_("Sharing at level {}. Sending the first report…"
                                   ).format(level))
            else:
                status.set_saved(_("Saved. Nothing will be sent: {}").format(
                    self.usage_stats_blocked_reason()))
            return True

        apply_button = primary_button(_("Apply"))
        apply_button.clicked.connect(on_apply)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(apply_button)
        v.addLayout(button_row)
        v.addStretch(1)

        repopulate()
        dirty.reset()
        return pane, repopulate, {"tracker": dirty, "apply": on_apply}

    # --- providers sub-tab ------------------------------------------------
    # Every column is named, and nothing below addresses one by literal number.
    # That matters more here than tidiness: the Preferred/Banned check states are
    # read and written BY INDEX, so an off-by-one bans the provider the user
    # meant to prefer. Adding the name column at 0 shifted all twelve of these,
    # which is exactly the hazard the Channel partners tab already warns about.
    _PROVIDER_NAME_COL = 0
    _NPUB_COL = 1
    _FEE_COL = 2
    _MINING_COL = 3
    _MIN_COL = 4
    _MAX_COL = 5
    _POW_COL = 6
    _PREF_COL = 7
    _BAN_COL = 8
    # Successes and Attempts answer two different questions and are easy to
    # conflate, so they sit side by side: how many swaps this provider actually
    # completed, and how many times we turned to it at all (once per cascade --
    # see _record_provider_attempt). Successes <= Attempts always holds.
    _SUCCESS_COL = 9
    _ATTEMPTS_COL = 10
    _FAULTS_COL = 11
    _PENALTY_COL = 12
    _LAST_FAULT_COL = 13

    def _build_providers_tab(self, wallet: 'Abstract_Wallet'):
        """Build the Providers sub-tab: a live list of nostr-discovered swap
        providers with Preferred / Banned toggles, plus free-text npub fields for
        providers that are not currently online. Returns
        ``(widget, repopulate_fn, pane)`` -- the pane being this tab's entry in
        the unsaved-changes guard (see :class:`_ApplyPane`).
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
            "it is the only option). “Reset stats” clears a provider's history.\n"
            "“Attempts” counts the swaps we turned to a provider for; "
            "“Successes” counts the ones it actually delivered.")))
        v.addWidget(_wrapped_label(_(
            "“Provider name” is the name a provider publishes for itself in its "
            "nostr profile. It reads “—” for a provider that publishes none, "
            "which is most of them — a swap offer carries no name, so the name "
            "is a separate, optional thing a provider may advertise.\n"
            "Treat it as a label, never as proof of identity: nothing verifies "
            "it, and anyone can publish any name, including one already used by "
            "a provider you trust. A name shared by two providers on this list "
            "is marked ⚠. The npub beside it is the real identity, and it is "
            "what the preferred and banned lists match on.")))

        tree = QTreeWidget()
        tree.setHeaderLabels([
            _("Provider name"), _("Provider (npub)"), _("Fee %"),
            _("Mining (sat)"), _("Min (sat)"), _("Max (sat)"), _("PoW"),
            _("Preferred"), _("Banned"), _("Successes"), _("Attempts"),
            _("Faults"), _("Penalty %"), _("Last fault")])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(self._PROVIDER_NAME_COL, 160)
        tree.setColumnWidth(self._NPUB_COL, 180)
        tree.setToolTip(_("Click a column header to sort by it; click the same "
                          "header again to reverse the order."))
        sorter = SortableHeader(tree)
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

        status_label = QLabel("")
        hint_label = QLabel(_StatusLine.HINT_TEXT)
        status = _StatusLine(status_label, hint_label)
        v.addWidget(status_label)
        v.addWidget(hint_label)

        # --- unsaved-changes tracking --------------------------------------
        # The tab's state is the two text boxes plus which rows are ticked. Only
        # the TICKED npubs are snapshotted, not every row: the provider list is
        # live (nostr discovery adds and drops rows under the user), and a row
        # appearing is not an edit the user made.
        def _row_npubs(col: int) -> Tuple[str, ...]:
            out = []
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                npub = it.data(0, Qt.ItemDataRole.UserRole)
                if npub and it.checkState(col) == Qt.CheckState.Checked:
                    out.append(str(npub))
            return tuple(sorted(out))

        def _listed_npubs() -> set:
            """Every npub currently on screen, ticked or not."""
            out = set()
            for i in range(tree.topLevelItemCount()):
                npub = tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                if npub:
                    out.add(str(npub))
            return out

        def _ticks_to_carry(col: int, typed: set) -> set:
            """Which npubs should come out of a dirty rebuild ticked in ``col``.

            The row's OWN state wins for any provider already on screen, which is
            what makes unticking survive: a union with the text box would read the
            stored list -- which still names the provider the user just unticked --
            and silently put the tick back. The text box only decides rows that
            are not listed yet, i.e. ones this rebuild is about to add.
            """
            listed = _listed_npubs()
            return set(_row_npubs(col)) | {n for n in typed if n not in listed}

        def _providers_snapshot() -> Tuple:
            return (pref_edit.toPlainText(), ban_edit.toPlainText(),
                    _row_npubs(self._PREF_COL), _row_npubs(self._BAN_COL))

        dirty = _DirtyTracker(_("Swap providers"), _providers_snapshot, status)
        dirty.watch(pref_edit.textChanged, ban_edit.textChanged, tree.itemChanged)

        def load_text_from_config() -> None:
            c = self.settings(wallet)
            pref_edit.setPlainText(", ".join(sorted(_parse_npub_set(c.INBOUND_LIQUIDITY_PREFERRED_NPUBS))))
            ban_edit.setPlainText(", ".join(sorted(_parse_npub_set(c.INBOUND_LIQUIDITY_BANNED_NPUBS))))

        def repopulate() -> None:
            # Called on ordinary wallet events and after every provider refresh,
            # so -- as on the Advanced tab -- it must not overwrite edits in
            # flight. The rows are rebuilt either way (their fee/penalty/attempt
            # columns are live data the user wants current); what changes while
            # dirty is where the tick-boxes get their state from: the user's own
            # current ticks, carried across the rebuild, rather than storage.
            keep_edits = dirty.is_dirty()
            if keep_edits:
                pref = _ticks_to_carry(self._PREF_COL,
                                       set(_parse_npub_set(pref_edit.toPlainText())))
                ban = _ticks_to_carry(self._BAN_COL,
                                      set(_parse_npub_set(ban_edit.toPlainText())))
            else:
                load_text_from_config()
                pref = set(_parse_npub_set(pref_edit.toPlainText()))
                ban = set(_parse_npub_set(ban_edit.toPlainText()))
            rel = self.provider_reliability_rows(wallet)
            # Muted for the whole rebuild: clearing and refilling the tree fires
            # itemChanged for every row, and measuring dirtiness halfway through
            # a rebuild reads a half-empty tree.
            with dirty.muted():
                _rebuild_rows(rel, pref, ban)
            if keep_edits:
                # Nothing was saved and the ticks were carried across, so the tab
                # is still dirty -- but the tracker's change signals were muted
                # through the rebuild, so ask it to re-render the hint.
                dirty.refresh_hint()
            else:
                dirty.reset()

        def _rebuild_rows(rel, pref, ban) -> None:
            tree.clear()
            checked = Qt.CheckState.Checked
            unchecked = Qt.CheckState.Unchecked
            # Self-published names, and which of them more than one provider on
            # this list claims. A name is not an identity (see the help text):
            # the clash is computed over every row about to be shown, so an
            # impersonator is flagged whether it is advertising right now or
            # only present as fault history.
            names = self.provider_names(wallet)
            shown_npubs = {o.npub for o in self.discovered_providers(wallet)}
            shown_npubs.update(rel)
            claim_counts: Dict[str, int] = {}
            for npub in shown_npubs:
                name = names.get(npub)
                if name:
                    claim_counts[name] = claim_counts.get(name, 0) + 1

            def add_row(npub, cells, *, checkable, r, offer=None):
                pen = r.get("penalty_pct", 0.0)
                name = names.get(npub, "")
                disputed = claim_counts.get(name, 0) > 1
                item = SortableTreeItem(
                    [f"⚠ {name}" if disputed else (name or "—")] + cells + [
                        str(r.get("success_count", 0)),
                        str(r.get("attempt_count", 0)),
                        str(r.get("fault_count", 0)),
                        f"{pen:.2f}", _fmt_age(r.get("last_fault_ts", 0.0))],
                    # Declared even for the offline rows, which carry no box:
                    # they then sort as "not ticked" rather than as text.
                    checkbox_cols=(self._PREF_COL, self._BAN_COL))
                item.setData(0, Qt.ItemDataRole.UserRole, npub)
                # Sort on the values, not on their formatted text: "1,900,000"
                # compares as a string between "12" and "2".
                item.set_sort_key(self._NPUB_COL, npub)
                # Sort by the name itself, so the ⚠ marker does not reorder the
                # column and the clashing rows land next to each other. Unnamed
                # providers sort last (see _NO_NAME_SORT_KEY).
                item.set_sort_key(self._PROVIDER_NAME_COL,
                                  name.casefold() if name else _NO_NAME_SORT_KEY)
                # Through `_literal_tooltip` like every other tooltip built
                # from data we did not author. An npub that came off an offer
                # is `clean_npub`-gated and cannot hold a tag -- but one of
                # these rows is keyed by the reliability store instead, which
                # is a wallet-file field and so is only as well-formed as the
                # file. Not worth depending on a regex two modules away.
                item.setToolTip(self._NPUB_COL, _literal_tooltip(npub))
                if disputed:
                    item.setToolTip(self._PROVIDER_NAME_COL, _literal_tooltip(_(
                        "Another provider on this list publishes this same "
                        "name. A nostr profile name is self-assigned and "
                        "unverified — check the npub before preferring one.")))
                elif name:
                    item.setToolTip(self._PROVIDER_NAME_COL, _literal_tooltip(_(
                        "Published by the provider itself in its nostr "
                        "profile; nothing verifies it.\n{}").format(npub)))
                else:
                    item.setToolTip(self._PROVIDER_NAME_COL, _literal_tooltip(_(
                        "This provider publishes no nostr profile name.\n{}"
                    ).format(npub)))
                if offer is not None:
                    item.set_sort_key(self._FEE_COL, float(offer.percentage_fee))
                    item.set_sort_key(self._MINING_COL, int(offer.mining_fee_sat))
                    item.set_sort_key(self._MIN_COL, int(offer.min_amount_sat))
                    item.set_sort_key(self._MAX_COL, int(offer.max_reverse_sat))
                    item.set_sort_key(self._POW_COL, int(offer.pow_bits))
                else:
                    # An offline provider advertises no terms. Sort it to the
                    # WORST end of each of them, so ordering by any term keeps
                    # the usable providers together at the top: worst is the
                    # largest fee/mining/min/PoW, and the smallest max.
                    for col in (self._FEE_COL, self._MINING_COL,
                                self._MIN_COL, self._POW_COL):
                        item.set_sort_key(col, float("inf"))
                    item.set_sort_key(self._MAX_COL, float("-inf"))
                item.set_sort_key(self._SUCCESS_COL, int(r.get("success_count", 0) or 0))
                item.set_sort_key(self._ATTEMPTS_COL, int(r.get("attempt_count", 0) or 0))
                item.set_sort_key(self._FAULTS_COL, int(r.get("fault_count", 0) or 0))
                item.set_sort_key(self._PENALTY_COL, float(pen or 0.0))
                # The age column reads "3h ago"/"—"; sort it by the timestamp
                # it was rendered from, with "never faulted" (0) sorting first.
                item.set_sort_key(self._LAST_FAULT_COL,
                                  float(r.get("last_fault_ts", 0.0) or 0.0))
                if checkable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(self._PREF_COL, checked if npub in pref else unchecked)
                    item.setCheckState(self._BAN_COL, checked if npub in ban else unchecked)
                reason = r.get("last_reason")
                if reason:
                    item.setToolTip(self._FAULTS_COL, _literal_tooltip(reason))
                add_sortable_row(tree, item)

            discovered = set()
            for o in self.discovered_providers(wallet):
                discovered.add(o.npub)
                add_row(o.npub, [
                    self._abbrev(o.npub, 12, 6) or o.npub,
                    f"{o.percentage_fee:g}", f"{o.mining_fee_sat:,}",
                    f"{o.min_amount_sat:,}", f"{o.max_reverse_sat:,}",
                    str(o.pow_bits), "", ""],
                    checkable=True, r=rel.get(o.npub, {}), offer=o)
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
            # The rows were just rebuilt from scratch; put them back in whatever
            # order the user last clicked a header for.
            sorter.reapply()

        def on_apply() -> bool:
            """Persist the preferred/banned lists. Returns whether anything was
            saved -- see the Settings tab's on_apply. Nothing here can fail
            validation (an unparseable npub is simply not a member of the set), so
            this one always reports success."""
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
            c = self.settings(wallet)
            with self.settings_batch(wallet):       # one write for both lists
                c.INBOUND_LIQUIDITY_PREFERRED_NPUBS = ", ".join(sorted(pref))
                c.INBOUND_LIQUIDITY_BANNED_NPUBS = ", ".join(sorted(ban))
            # Clean before repopulate, so it re-renders the text boxes from the
            # just-persisted (normalised, deduplicated) lists and re-baselines.
            dirty.reset()
            repopulate()
            status.set_saved(_("Providers saved."))
            return True

        def on_refresh() -> None:
            loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
            if loop is None:
                status.set_error(_("Cannot refresh: wallet is offline."))
                return
            status.set_message(_("Refreshing providers…"))

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
                # Name the provider the way its row does, but never *instead* of
                # the npub: this message confirms a destructive action, and two
                # providers can publish the same name.
                shown = self._abbrev(npub, 12, 6) or npub
                name = self.provider_name(wallet, npub)
                status.set_saved(_("Reliability stats cleared for {}.").format(
                    f"{name} ({shown})" if name else shown))
            else:
                self.clear_provider_reliability(wallet)
                status.set_saved(_("All reliability stats cleared."))
            repopulate()

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton(_("Refresh"))
        refresh_btn.clicked.connect(on_refresh)
        reset_btn = QPushButton(_("Reset stats"))
        reset_btn.setToolTip(_("Clear the selected provider's reliability history "
                               "(or all providers' if none is selected)."))
        reset_btn.clicked.connect(on_reset_stats)
        apply_btn = primary_button(_("Apply"))
        apply_btn.clicked.connect(on_apply)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        v.addLayout(btn_row)

        repopulate()
        dirty.reset()
        return tab, repopulate, _ApplyPane(_("Swap providers"), tab, dirty, on_apply)

    # --- channel partners sub-tab -----------------------------------------
    # Column 0 is the human-readable node name; the node id follows it. The
    # row's identity still rides on column 0's UserRole, so every
    # `data(0, UserRole)` reader below is unaffected by that ordering.
    _PARTNER_NAME_COL = 0
    _PARTNER_NODE_ID_COL = 1
    _PARTNER_PREF_COL = 2
    _PARTNER_BAN_COL = 3
    _PARTNER_FAULTS_COL = 5
    _PARTNER_REASON_COL = 9

    def _build_channel_partners_tab(self, wallet: 'Abstract_Wallet'):
        """Build the Channel partners sub-tab: a strict-mode toggle, a list of the
        peers we have seen or tried (each with Preferred / Banned toggles), and
        free-text fields for partners that are not listed. Returns
        ``(widget, repopulate_fn, pane)`` -- the pane being this tab's entry in
        the unsaved-changes guard (see :class:`_ApplyPane`).

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
            "These rows below are peers we have seen or tried — they are not a list "
            "of candidates. Tick a row to prefer/ban that node; a node that is not "
            "listed goes in the “Preferred partners” box.")))
        v.addWidget(_wrapped_label(_(
            "“Node name” is the alias the peer announces over Lightning gossip, the "
            "same name Electrum's own Channels tab shows. It reads “—” when this "
            "wallet has not been told one — which is the case for most peers while "
            "gossip is off (the default), since trampoline routing downloads no "
            "node announcements.")))

        strict_cb = QCheckBox(_("Only open channels to preferred partners (never fall back to suggestions)"))
        strict_cb.setChecked(bool(
            self.settings(wallet).INBOUND_LIQUIDITY_PARTNERS_STRICT))
        v.addWidget(strict_cb)

        one_per_peer_cb = QCheckBox(_("Only one channel per peer (don't open a second channel to a node "
                                      "you already have one with)"))
        one_per_peer_cb.setChecked(bool(
            self.settings(wallet).INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER))
        v.addWidget(one_per_peer_cb)

        v.addWidget(_wrapped_label(_(
            "Peers that fail to open, go offline, or force-close earn a decaying "
            "reliability penalty (they sink in the try-order but are still used if "
            "needed); after enough hard faults a peer is auto-banned. “Reset "
            "stats” clears a peer's history (it does not un-ban — untick Banned "
            "for that).")))

        tree = QTreeWidget()
        tree.setHeaderLabels([
            _("Node name"), _("Channel peer (node id)"), _("Preferred"), _("Banned"),
            _("OK"), _("Faults"), _("Hard"), _("Penalty %"), _("Last fault"),
            _("Last fault reason")])
        tree.setRootIsDecorated(False)
        tree.setColumnWidth(self._PARTNER_NAME_COL, 160)
        tree.setColumnWidth(self._PARTNER_NODE_ID_COL, 300)
        tree.setColumnWidth(self._PARTNER_REASON_COL, 240)
        tree.setToolTip(_("Click a column header to sort by it; click the same "
                          "header again to reverse the order. Sorting changes "
                          "only the view — the order preferred partners are "
                          "tried in is unaffected."))
        sorter = SortableHeader(tree)
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

        status_label = QLabel("")
        hint_label = QLabel(_StatusLine.HINT_TEXT)
        status = _StatusLine(status_label, hint_label)
        v.addWidget(status_label)
        v.addWidget(hint_label)

        # --- unsaved-changes tracking --------------------------------------
        # As on the Providers tab: the two text boxes, the two switches, and which
        # rows are ticked -- ticked node ids only, since the row set itself grows
        # as peers are met and that is not an edit.
        def _row_ids(col: int) -> Tuple[str, ...]:
            out = []
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                nid = it.data(0, Qt.ItemDataRole.UserRole)
                if nid and it.checkState(col) == Qt.CheckState.Checked:
                    out.append(str(nid).lower())
            return tuple(sorted(out))

        def _listed_ids() -> set:
            """Every node id currently on screen, ticked or not."""
            out = set()
            for i in range(tree.topLevelItemCount()):
                nid = tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                if nid:
                    out.add(str(nid).lower())
            return out

        def _ticks_to_carry(col: int, typed: set) -> set:
            """Which node ids should come out of a dirty rebuild ticked in ``col``.
            See the Providers tab's twin: the row's own state wins, so unticking a
            listed peer is not undone by the stored list that still names it."""
            listed = _listed_ids()
            return set(_row_ids(col)) | {n for n in typed if n not in listed}

        def _partners_snapshot() -> Tuple:
            return (pref_edit.toPlainText(), ban_edit.toPlainText(),
                    strict_cb.isChecked(), one_per_peer_cb.isChecked(),
                    _row_ids(self._PARTNER_PREF_COL),
                    _row_ids(self._PARTNER_BAN_COL))

        dirty = _DirtyTracker(_("Channel partners"), _partners_snapshot, status)
        dirty.watch(pref_edit.textChanged, ban_edit.textChanged,
                    strict_cb.toggled, one_per_peer_cb.toggled, tree.itemChanged)

        def load_text_from_config() -> None:
            c = self.settings(wallet)
            pref_edit.setPlainText("\n".join(_parse_partner_list(c.INBOUND_LIQUIDITY_PREFERRED_PARTNERS)))
            ban_edit.setPlainText("\n".join(sorted(_parse_banned_partners(c.INBOUND_LIQUIDITY_BANNED_PARTNERS))))
            strict_cb.setChecked(bool(c.INBOUND_LIQUIDITY_PARTNERS_STRICT))
            one_per_peer_cb.setChecked(bool(c.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER))

        def repopulate() -> None:
            # See the Providers tab's repopulate: this fires on ordinary wallet
            # events, so while there are edits in flight the rows are still
            # rebuilt (names, penalties and fault ages are live data) but the
            # tick-boxes keep the user's own state rather than reverting to
            # storage.
            keep_edits = dirty.is_dirty()
            if keep_edits:
                pref_ids = _ticks_to_carry(
                    self._PARTNER_PREF_COL,
                    {normalize_node_id(p)
                     for p in _parse_partner_list(pref_edit.toPlainText())})
                ban_ids = _ticks_to_carry(
                    self._PARTNER_BAN_COL,
                    set(_parse_banned_partners(ban_edit.toPlainText())))
            else:
                load_text_from_config()
                pref_ids = {normalize_node_id(p)
                            for p in _parse_partner_list(pref_edit.toPlainText())}
                ban_ids = set(_parse_banned_partners(ban_edit.toPlainText()))
            rel = self.peer_reliability_rows(wallet)
            with dirty.muted():
                _rebuild_rows(rel, pref_ids, ban_ids)
            if keep_edits:
                dirty.refresh_hint()
            else:
                dirty.reset()

        def _rebuild_rows(rel, pref_ids, ban_ids) -> None:
            tree.clear()
            checked = Qt.CheckState.Checked
            unchecked = Qt.CheckState.Unchecked

            def add_row(nid, *, checkable, alias=None):
                low = nid.lower()
                r = rel.get(low, {})
                pen = r.get("penalty_pct", 0.0)
                reason = r.get("last_reason") or ""
                # The name is only as good as what gossip has told this wallet
                # (see Plugin.node_alias): in trampoline mode -- Electrum's
                # default -- an ordinary peer has none, and the column reads "—".
                if alias is None:
                    alias = self.node_alias(wallet, nid)
                item = SortableTreeItem([
                    alias or "—", self._abbrev(nid, 12, 6) or nid, "", "",
                    str(r.get("success_count", 0)), str(r.get("fault_count", 0)),
                    str(r.get("hard_fault_count", 0)), f"{pen:.2f}",
                    _fmt_age(r.get("last_fault_ts", 0.0)), str(reason)],
                    checkbox_cols=(self._PARTNER_PREF_COL, self._PARTNER_BAN_COL))
                item.setData(0, Qt.ItemDataRole.UserRole, nid)
                if alias:
                    # The cell is elided at the column width, and the full node
                    # id is abbreviated in the next one; show both in full here.
                    item.setToolTip(self._PARTNER_NAME_COL,
                                    _literal_tooltip(f"{alias}\n{nid}"))
                # Sort on the values, not on their formatted text. An unnamed
                # peer sorts after every named one either way round: "—" would
                # otherwise interleave with the names by codepoint.
                item.set_sort_key(self._PARTNER_NAME_COL,
                                  alias.casefold() if alias else "￿")
                item.set_sort_key(self._PARTNER_NODE_ID_COL, low)
                item.set_sort_key(4, int(r.get("success_count", 0) or 0))
                item.set_sort_key(self._PARTNER_FAULTS_COL, int(r.get("fault_count", 0) or 0))
                item.set_sort_key(6, int(r.get("hard_fault_count", 0) or 0))
                item.set_sort_key(7, float(pen or 0.0))
                # "3h ago"/"—" sorts by the timestamp behind it; never-faulted
                # (0) sorts first.
                item.set_sort_key(8, float(r.get("last_fault_ts", 0.0) or 0.0))
                item.set_sort_key(self._PARTNER_REASON_COL, str(reason))
                if checkable:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(self._PARTNER_PREF_COL, checked if low in pref_ids else unchecked)
                    item.setCheckState(self._PARTNER_BAN_COL, checked if low in ban_ids else unchecked)
                if reason:
                    item.setToolTip(self._PARTNER_FAULTS_COL, _literal_tooltip(reason))
                add_sortable_row(tree, item)

            current = set()
            for partner in self.current_channel_partners(wallet):
                nid = partner["node_id"]
                current.add(nid.lower())
                add_row(nid, checkable=True, alias=partner.get("alias", ""))
            # Also surface peers we have a reliability history for but no current
            # channel with (e.g. an auto-banned or force-closed peer), so their
            # stats stay visible and clearable.
            for low in rel:
                if low not in current:
                    add_row(low, checkable=True)
            # The rows were just rebuilt from scratch; put them back in whatever
            # order the user last clicked a header for.
            sorter.reapply()

        def on_apply() -> bool:
            """Persist the partner lists and the two switches. Returns whether
            anything was saved -- see the Settings tab's on_apply. Always True:
            an unparseable partner string simply does not join the list."""
            # Seed from the text fields (so not-yet-connected partners survive),
            # then fold in the per-row checkbox states for current peers.
            preferred = _parse_partner_list(pref_edit.toPlainText())
            pref_ids = {normalize_node_id(p) for p in preferred}
            banned = set(_parse_banned_partners(ban_edit.toPlainText()))
            # Insertion order, NOT the order on screen. `preferred` is a
            # try-order -- the plugin opens to the first partner in it that
            # accepts -- so re-sorting the table to read a penalty column must
            # not re-prioritise which peer gets the next channel.
            for it in rows_in_insertion_order(tree):
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
            c = self.settings(wallet)
            with self.settings_batch(wallet):       # one write for the form
                c.INBOUND_LIQUIDITY_PREFERRED_PARTNERS = ", ".join(preferred)
                c.INBOUND_LIQUIDITY_BANNED_PARTNERS = ", ".join(sorted(banned))
                c.INBOUND_LIQUIDITY_PARTNERS_STRICT = strict_cb.isChecked()
                c.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER = one_per_peer_cb.isChecked()
            # Clean before repopulate, so it re-renders from what was persisted
            # (normalised, deduplicated, banned-minus-preferred) and re-baselines.
            dirty.reset()
            repopulate()
            status.set_saved(_("Channel partners saved."))
            return True

        def on_reset_stats() -> None:
            # Clear the selected peer's reliability history, or all if none is
            # selected -- a manual override of the auto-penalty / fault tally.
            sel = tree.selectedItems()
            if sel:
                nid = sel[0].data(0, Qt.ItemDataRole.UserRole)
                self.clear_peer_reliability(wallet, nid)
                status.set_saved(_("Reliability stats cleared for {}.").format(
                    self._abbrev(nid, 12, 6) or nid))
            else:
                self.clear_peer_reliability(wallet)
                status.set_saved(_("All channel-peer reliability stats cleared."))
            repopulate()

        btn_row = QHBoxLayout()
        reset_btn = QPushButton(_("Reset stats"))
        reset_btn.setToolTip(_("Clear the selected peer's reliability history "
                               "(or all peers' if none is selected)."))
        reset_btn.clicked.connect(on_reset_stats)
        apply_btn = primary_button(_("Apply"))
        apply_btn.clicked.connect(on_apply)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(apply_btn)
        v.addLayout(btn_row)

        repopulate()
        dirty.reset()
        return tab, repopulate, _ApplyPane(_("Channel partners"), tab, dirty, on_apply)

    def _reload_settings_fields(self, wallet: 'Abstract_Wallet', edits,
                                sync_toggle: Optional[Callable[[], None]] = None) -> None:
        """Re-read this wallet's persisted settings back into the editable
        Settings-tab fields, so any clamping/normalisation done on save is
        visible to the user. (The Advanced tab reloads its own fields via its
        repopulate().)"""
        c = self.settings(wallet)
        if sync_toggle is not None:
            sync_toggle()
        # Positional: this list must stay in the same order as the `fields` list
        # in _build_settings_tab, which is what `edits` was built from.
        values = [
            str(c.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT),
            str(c.INBOUND_LIQUIDITY_MAX_CHANNELS),
            str(getattr(c, 'INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT',
                        DEFAULT_MAX_CHANNEL_SIZE_SAT)),
            str(c.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT),
            str(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT),
            str(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT),
            str(c.INBOUND_LIQUIDITY_DEV_FEE_PCT),
            str(c.INBOUND_LIQUIDITY_SINK_ADDRESS or ''),
        ]
        for (edit, _parser, _setter, _label), value in zip(edits, values):
            edit.setText(value)
