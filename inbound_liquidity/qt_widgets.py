# Reusable Qt widgets for the Inbound Liquidity plugin's GUI.
#
# `ToggleSwitch` is the large, obvious ENABLED/DISABLED slider that arms the
# plugin's automation. It is a plain checkable QAbstractButton, so callers use
# the standard `isChecked()` / `setChecked()` / `toggled(bool)` API; only its
# painting and the sliding-knob animation are custom.
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, pyqtProperty,
)
from PyQt6.QtGui import QColor, QPainter, QPaintEvent
from PyQt6.QtWidgets import QAbstractButton, QWidget


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
