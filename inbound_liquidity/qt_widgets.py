# Reusable Qt widgets for the Inbound Liquidity plugin's GUI.
#
# `ToggleSwitch` is the large, obvious ENABLED/DISABLED slider that arms the
# plugin's automation. It is a plain checkable QAbstractButton, so callers use
# the standard `isChecked()` / `setChecked()` / `toggled(bool)` API; only its
# painting and the sliding-knob animation are custom.
#
# `primary_button` is the big green Apply that commits a settings form.
#
# `SortableTreeItem` / `sortable_header` turn a QTreeWidget's column headers
# into click-to-sort controls that sort on the underlying VALUES rather than on
# the formatted text.
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Tuple

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPaintEvent
from PyQt6.QtWidgets import (
    QAbstractButton, QPushButton, QTreeWidget, QTreeWidgetItem, QWidget,
)

# The plugin's one accent colour: the ToggleSwitch's "on" track, the green of
# the "saved" status lines, and the Apply button below all use it.
ACCENT_GREEN = "#2ea043"
ACCENT_GREEN_HOVER = "#3fb950"
ACCENT_GREEN_PRESSED = "#238636"

# A default QPushButton is roughly 80x30. "3x larger" is read as three times the
# AREA -- about 1.75x in each direction -- which reads as the primary action of
# the form without swamping the Reset/Refresh buttons sharing its row.
PRIMARY_BUTTON_MIN_WIDTH = 140
PRIMARY_BUTTON_MIN_HEIGHT = 54
PRIMARY_BUTTON_FONT_SCALE = 1.3


def primary_button(text: str, *, parent: Optional[QWidget] = None) -> QPushButton:
    """A large green "commit this form" button (the plugin's Apply).

    Note on the stylesheet: giving a QPushButton a `background-color` makes Qt
    abandon the native frame for it entirely, so the sheet has to supply the
    border, radius and padding too -- otherwise the result is a flat green
    rectangle with no affordance at all. Hover/pressed/disabled are spelled out
    for the same reason: without them a styled button looks inert under the
    cursor and, worse, looks enabled while disabled.
    """
    btn = QPushButton(text, parent)
    btn.setMinimumSize(PRIMARY_BUTTON_MIN_WIDTH, PRIMARY_BUTTON_MIN_HEIGHT)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    font = QFont(btn.font())
    base_pt = font.pointSizeF()
    if base_pt > 0:
        font.setPointSizeF(base_pt * PRIMARY_BUTTON_FONT_SCALE)
    else:                               # a pixel-sized font (no point size set)
        font.setPixelSize(max(1, round(font.pixelSize() * PRIMARY_BUTTON_FONT_SCALE)))
    font.setBold(True)
    btn.setFont(font)
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: {ACCENT_GREEN};
            color: #ffffff;
            border: 1px solid {ACCENT_GREEN_PRESSED};
            border-radius: 6px;
            padding: 8px 20px;
        }}
        QPushButton:hover    {{ background-color: {ACCENT_GREEN_HOVER}; }}
        QPushButton:pressed  {{ background-color: {ACCENT_GREEN_PRESSED}; }}
        QPushButton:disabled {{ background-color: #6e7681; color: #d0d7de;
                                border-color: #6e7681; }}
    """)
    return btn


# --- sortable trees -------------------------------------------------------
# Column 0 of both sortable trees already carries the row's identity (npub /
# node id) at Qt.ItemDataRole.UserRole, so the sort machinery uses roles above
# it rather than fighting for that one.
SORT_KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 1
INSERT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class SortableTreeItem(QTreeWidgetItem):
    """A tree row that sorts on the values behind its cells, not on their text.

    The columns in these tables are formatted for reading -- "1,900,000",
    "0.50", "3h ago" -- and sorting those strings gives nonsense orders
    ("1,900,000" lands between "12" and "2"). Callers therefore hand each cell
    the value it was rendered from via :meth:`set_sort_key`, and the comparison
    uses that.

    Columns named in ``checkbox_cols`` compare on their LIVE check state, so a
    box ticked after the row was built still sorts where the user can see it is.
    Declare those columns on EVERY row of the tree, including rows that carry no
    box at all: an un-checkable row reports ``Unchecked``, which is both true
    (it is not preferred) and a number, so it sorts among the others. Leave it
    out and that row's empty cell compares as *text* instead, which lands it
    past every ticked and unticked row alike.
    """

    def __init__(self, cells: Iterable[str], *,
                 checkbox_cols: Iterable[int] = ()) -> None:
        super().__init__(list(cells))
        self._checkbox_cols = frozenset(checkbox_cols)

    def set_sort_key(self, column: int, value: Any) -> None:
        self.setData(column, SORT_KEY_ROLE, value)

    def sort_value(self, column: int) -> Any:
        if column in self._checkbox_cols:
            return 1 if self.checkState(column) == Qt.CheckState.Checked else 0
        key = self.data(column, SORT_KEY_ROLE)
        return self.text(column) if key is None else key

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, QTreeWidgetItem):
            return NotImplemented
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        mine = self.sort_value(column)
        theirs = (other.sort_value(column) if isinstance(other, SortableTreeItem)
                  else other.text(column))
        return _sort_tuple(mine) < _sort_tuple(theirs)


def _sort_tuple(value: Any) -> Tuple[int, Any]:
    """Tag a sort key with its kind so numbers and strings never meet in a
    comparison. A mixed column (a number in one row, a placeholder in another)
    would otherwise raise TypeError inside Qt's sort and take the whole repaint
    with it. Numbers sort before text; everything else sorts last."""
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, str):
        return (1, value.casefold())
    return (2, str(value))


class SortableHeader:
    """Click-to-sort for a QTreeWidget's header.

    Deliberately NOT ``QTreeWidget.setSortingEnabled(True)``:

    * these lists ship in a meaningful order (peers with a live channel first,
      then fault-history-only ones), and auto-sorting would throw that away
      before the user asked for any sort at all;
    * every refresh tick calls ``tree.clear()`` and re-adds, and with Qt's
      sorting on, each insert re-sorts -- so the rows shuffle mid-populate.

    So sorting is applied explicitly: a header click picks the column (and
    toggles ascending/descending on a repeat click), and the choice is
    remembered so :meth:`reapply` can restore it after a repopulate.
    """

    def __init__(self, tree: QTreeWidget) -> None:
        self.tree = tree
        self.column: Optional[int] = None
        self.order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
        header = tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.on_section_clicked)

    def on_section_clicked(self, column: int) -> None:
        if column == self.column:
            self.order = (Qt.SortOrder.DescendingOrder
                          if self.order == Qt.SortOrder.AscendingOrder
                          else Qt.SortOrder.AscendingOrder)
        else:
            self.column = column
            self.order = Qt.SortOrder.AscendingOrder
        self.reapply()

    def reapply(self) -> None:
        """Re-apply the remembered sort. Called at the end of every repopulate,
        so a refresh tick does not silently drop the user's chosen order."""
        if self.column is None:
            return
        self.tree.header().setSortIndicator(self.column, self.order)
        self.tree.sortItems(self.column, self.order)


def rows_in_insertion_order(tree: QTreeWidget) -> List[QTreeWidgetItem]:
    """The tree's rows in the order they were added, whatever the view is
    currently sorted by.

    Sorting is a way to READ the table; it must not quietly rewrite anything
    the order of these rows feeds into (the preferred channel partners are
    tried in list order, so a sort done to inspect a penalty column would
    otherwise re-prioritise which peer gets the next channel).
    """
    items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    return sorted(
        items,
        key=lambda it: (it.data(0, INSERT_INDEX_ROLE)
                        if it is not None and it.data(0, INSERT_INDEX_ROLE) is not None
                        else 0))


def add_sortable_row(tree: QTreeWidget, item: SortableTreeItem) -> None:
    """Append a row, stamping it with its insertion index."""
    item.setData(0, INSERT_INDEX_ROLE, tree.topLevelItemCount())
    tree.addTopLevelItem(item)


class ToggleSwitch(QAbstractButton):
    """An iOS-style sliding on/off switch.

    Off: grey track, knob to the left. On: green track, knob to the right. The
    knob glides between the two on toggle. Checkable, so it behaves like a big
    QCheckBox to the rest of the code (emits `toggled(bool)`).
    """

    # Palette (kept module-level-ish as attributes so a theme could override).
    _TRACK_ON = QColor(46, 160, 67)      # green
    _TRACK_OFF = QColor(150, 150, 150)   # grey
    _KNOB = QColor(255, 255, 255)

    def __init__(self, parent: Optional[QWidget] = None,
                 *, track_width: int = 72, track_height: int = 34,
                 margin: int = 3) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._track_width = track_width
        self._track_height = track_height
        self._margin = margin
        # 0.0 == knob fully left (off), 1.0 == knob fully right (on).
        self._offset: float = 1.0 if self.isChecked() else 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate_to_state)

    # --- animated property -----------------------------------------------
    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    # QPropertyAnimation drives this named Qt property.
    offset = pyqtProperty(float, fget=_get_offset, fset=_set_offset)

    def _animate_to_state(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def sync_knob(self) -> None:
        """Snap the knob to the current checked state at once, cancelling any
        in-flight slide.

        The slide is normally driven by the ``toggled`` signal. A caller that
        pushes a new state in with ``blockSignals(True)`` -- e.g. to re-read the
        config into the switch WITHOUT re-firing its ``toggled`` handler --
        suppresses that signal too, so the knob would otherwise stay put and
        disagree with ``isChecked()`` (green ``ENABLED`` label over a knob still
        parked in the off position). Call this right after such a change so the
        knob matches the checked state on the first paint.
        """
        self._anim.stop()
        self._set_offset(1.0 if self.isChecked() else 0.0)

    # --- sizing -----------------------------------------------------------
    def sizeHint(self) -> QSize:
        return QSize(self._track_width, self._track_height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # --- painting ---------------------------------------------------------
    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h, m = self._track_width, self._track_height, self._margin
        radius = h / 2.0

        # Track: interpolate grey -> green across the knob travel so the colour
        # follows the animation rather than snapping at the ends.
        track = self._blend(self._TRACK_OFF, self._TRACK_ON, self._offset)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)

        # Knob glides between left and right insets.
        knob_d = h - 2 * m
        x_min = m
        x_max = w - m - knob_d
        x = x_min + (x_max - x_min) * self._offset
        p.setBrush(self._KNOB)
        p.drawEllipse(QRectF(x, m, knob_d, knob_d))

        p.end()

    @staticmethod
    def _blend(a: QColor, b: QColor, t: float) -> QColor:
        t = max(0.0, min(1.0, t))
        return QColor(
            round(a.red() + (b.red() - a.red()) * t),
            round(a.green() + (b.green() - a.green()) * t),
            round(a.blue() + (b.blue() - a.blue()) * t),
        )
