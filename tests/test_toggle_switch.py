"""Unit tests for the ToggleSwitch widget (the ENABLED/DISABLED slider).

Runs a headless (offscreen) QApplication; skipped when PyQt6 / the plugin are
not importable (e.g. outside the electrum venv).
"""
from __future__ import annotations

import os
from typing import List

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from electrum.plugins.inbound_liquidity.qt_widgets import ToggleSwitch  # type: ignore  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_defaults_to_off(qapp):
    sw = ToggleSwitch()
    assert sw.isCheckable()
    assert sw.isChecked() is False
    # Knob starts fully left when off.
    assert sw.offset == 0.0


def test_toggled_signal_fires_with_state(qapp):
    sw = ToggleSwitch()
    seen: List[bool] = []
    sw.toggled.connect(seen.append)
    sw.setChecked(True)
    sw.setChecked(False)
    assert seen == [True, False]


def test_set_checked_reflects_in_ischecked(qapp):
    sw = ToggleSwitch()
    sw.setChecked(True)
    assert sw.isChecked() is True


def test_size_hint_is_large_and_pill_shaped(qapp):
    sw = ToggleSwitch()
    hint = sw.sizeHint()
    # Comfortably clickable and clearly a horizontal slider.
    assert hint.width() >= 48
    assert hint.height() >= 24
    assert hint.width() > hint.height()
    assert sw.minimumSizeHint() == hint


def test_offset_property_animates_toward_target(qapp):
    # Setting the animated `offset` property directly (what QPropertyAnimation
    # drives) must move the knob and not raise while painting.
    sw = ToggleSwitch()
    sw.resize(sw.sizeHint())
    sw.offset = 0.5
    assert sw.offset == 0.5
    sw.grab()  # force a paintEvent; must not raise
