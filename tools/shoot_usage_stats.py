#!/usr/bin/env python
"""Render the usage-statistics screens to PNGs, for review.

Builds the real widgets -- the real consent dialog and the real Advanced tab --
offscreen, with a wallet seeded so the previews show plausible numbers rather
than a wall of zeros, and saves a PNG per screen.

Not a test and not collected as one (no `test_` prefix, and it lives outside
tests/). It exists so a change to either screen can be reviewed as a picture
instead of as a diff, and so those pictures are reproducible rather than
hand-captured.

Run from the repo root:

    QT_QPA_PLATFORM=offscreen e2e/.venv-electrum/bin/python \\
        tools/shoot_usage_stats.py --out /tmp/shots
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parent.parent
# The Qt scaffolding these screens need (a fake window, a fake wallet, a plugin
# built without Electrum's constructor) already exists for the tests; reusing it
# keeps this script to the part that is actually about screenshots.
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "inbound_liquidity"))

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QPlainTextEdit, QRadioButton, QTabWidget, QWidget,
)

from electrum.gui.qt.util import WindowModalDialog  # noqa: E402
import electrum.plugins.inbound_liquidity as pkg  # type: ignore  # noqa: E402

import test_qt_tab as scaffold  # noqa: E402

us = pkg.usage_stats

# A wallet that has actually been used, so level 2 and 3 previews show real
# figures. These are the numbers that appear in the screenshots.
SEEDED_SETTINGS = dict(
    INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
    INBOUND_LIQUIDITY_GOAL_SAT=100_000,
    INBOUND_LIQUIDITY_MAX_CHANNELS=2,
    INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.9,
    INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
    INBOUND_LIQUIDITY_DEV_FEE_PCT=0.1,
    INBOUND_LIQUIDITY_SINK_ADDRESS="someone@example.com",
    INBOUND_LIQUIDITY_PREFERRED_NPUBS="npub1aaa,npub1bbb",
)
SEEDED_TOTALS = {
    "channels_opened_total": 7,
    "channels_closed_total": 3,
    "force_closes_total": 1,
    "sats_funded_total": 1_400_000,
    "swaps_completed_total": 12,
    "sats_swapped_total": 860_000,
    "sink_payments_total": 5,
    "sats_sunk_total": 45_000,
    "dev_fee_paid_total_sat": 860,
}


class _Window(QWidget):
    """A real QWidget window: the consent dialog is parented to it, and Qt needs
    a real parent."""

    def __init__(self) -> None:
        super().__init__()
        self.tabs = QTabWidget()
        self.resize(1100, 950)

    def question(self, msg, **kwargs) -> bool:
        return False

    def statusBar(self):
        from types import SimpleNamespace
        return SimpleNamespace(showMessage=lambda *a, **k: None,
                               addPermanentWidget=lambda *a, **k: None,
                               removeWidget=lambda *a, **k: None)

    def get_decimal_point(self) -> int:
        return 8


def _plugin_and_wallet():
    plugin = scaffold._make_plugin()
    for name, value in SEEDED_SETTINGS.items():
        setattr(plugin.config, name, value)
    from conftest import seed_settings
    seed_settings(plugin, **SEEDED_SETTINGS)
    wallet = scaffold._FakeWallet()
    wallet.db.put(pkg.USAGE_TOTALS_DB_KEY, dict(SEEDED_TOTALS))
    return plugin, wallet


def _save(widget: QWidget, path: Path, *, size=None) -> None:
    if size is not None:
        widget.resize(*size)
    widget.show()
    QApplication.processEvents()
    widget.repaint()
    QApplication.processEvents()
    pixmap = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    assert pixmap.save(str(path)), f"could not write {path}"
    print(f"  {path.name}  {pixmap.width()}x{pixmap.height()}")


def _radio(widget: QWidget, level: int, *, off_label: str) -> QRadioButton:
    for button in widget.findChildren(QRadioButton):
        text = button.text()
        if level == us.LEVEL_OFF and text.startswith(off_label):
            return button
        if level != us.LEVEL_OFF and text.startswith(f"{level} "):
            return button
    raise AssertionError(f"no radio for level {level}")


def shoot_consent(out: Path) -> None:
    """The one-time consent screen, at each level, with its live preview."""
    print("consent screen:")
    plugin, wallet = _plugin_and_wallet()
    window = _Window()
    captured: List[WindowModalDialog] = []

    def _fake_exec(dialog):
        captured.append(dialog)
        dialog.resize(720, 1000)
        for level, name in ((us.LEVEL_OFF, "off"), (1, "level1"),
                            (2, "level2"), (3, "level3")):
            _radio(dialog, level, off_label="No thanks").setChecked(True)
            QApplication.processEvents()
            _save(dialog, out / f"consent-{name}.png")
        return 0        # "Ask me later": record nothing

    original = WindowModalDialog.exec
    WindowModalDialog.exec = _fake_exec
    try:
        plugin._maybe_prompt_usage_consent(window, wallet)
    finally:
        WindowModalDialog.exec = original
    assert captured, "the consent dialog was never shown"


def shoot_advanced(out: Path) -> None:
    """The Advanced tab's two inner panes."""
    print("advanced tab:")
    plugin, wallet = _plugin_and_wallet()
    window = scaffold._FakeWindow()
    plugin._add_liquidity_tab(window, wallet)

    advanced = plugin._tabs[wallet].container.findChild(QTabWidget).widget(3)
    inner = advanced.findChild(QTabWidget)

    def pane(title: str) -> QWidget:
        for index in range(inner.count()):
            if inner.tabText(index) == title:
                inner.setCurrentIndex(index)
                return inner.widget(index)
        raise AssertionError(f"no {title!r} pane")

    # General first, so the screenshot shows it is unchanged.
    pane("General")
    _save(advanced, out / "advanced-general.png", size=(960, 760))

    usage = pane("Usage statistics")
    _save(advanced, out / "advanced-usage-off.png", size=(960, 760))

    # ...then with a level selected, so the preview is populated. Scrolled to
    # the preview, which is the part of this pane worth looking at.
    for level, name in ((1, "level1"), (3, "level3")):
        _radio(usage, level, off_label="Off").setChecked(True)
        QApplication.processEvents()
        _save(advanced, out / f"advanced-usage-{name}.png", size=(960, 760))

    # The bottom of the pane: the status line and the Apply, which sit below
    # the fold on a tab-sized window because the options and the preview are
    # tall. Worth a shot of its own so a reviewer can see the pane IS
    # Apply-gated rather than saving on click.
    from PyQt6.QtWidgets import QScrollArea
    scrolls = usage.findChildren(QScrollArea)
    if scrolls:
        bar = scrolls[0].verticalScrollBar()
        bar.setValue(bar.maximum())
        QApplication.processEvents()
        _save(advanced, out / "advanced-usage-level3-bottom.png", size=(960, 760))

    # LAST, because grabbing the preview on its own means resizing it, which
    # mutates the live widget and would wreck the layout of any shot taken
    # after it (the Apply ended up floating over the preview the first time
    # this ran in the other order).
    boxes = usage.findChildren(QPlainTextEdit)
    if boxes:
        box = boxes[0]
        box.resize(900, 620)
        _save(box, out / "advanced-usage-level3-preview.png")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/usage-stats-shots",
                        help="directory to write the PNGs to")
    args = parser.parse_args(argv)
    out = Path(args.out)

    app = QApplication.instance() or QApplication([])
    shoot_consent(out)
    shoot_advanced(out)
    print(f"\nwrote {len(list(out.glob('*.png')))} screenshots to {out}")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
