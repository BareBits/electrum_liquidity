"""No settings pane may paint its text clipped, at any window size.

The bug these pin: a QTabWidget page given less height than its content needs
does NOT scroll. Qt distributes the shortfall by shrinking every widget below
its sizeHint, and a QLabel handed less height than its text paints that text
cut off -- usually by a few pixels per row, so every label on the pane looks
very slightly shaved. Electrum's default window is 840x400, which leaves roughly
290px of tab area, and the Advanced form alone wants 1033px; so the default
window was enough to clip 14 labels on Settings and 26 on Advanced, and to
squeeze the provider and partner tables down to a header strip with no visible
rows.

The fix is a scroll area per pane (see qt._scrolled). These tests assert the
property the user actually cares about -- no text is cut off -- rather than the
mechanism, so they keep holding if the mechanism changes. They also pin the
other end: a tall window must still hand the tables the space, because a scroll
area that stopped the tables growing would be a different regression.

Measuring requires a REAL layout pass at a chosen size, which needs some care:
  * the pane must be the current page of every tab bar above it, or QTabWidget
    never lays it out and every geometry read is of a stale default;
  * the offscreen platform refuses to resize a top-level window ("does not
    support propagateSizeHints"), pinning it to 640x480. WA_DontShowOnScreen
    plus show() gets a full layout pass at the size asked for, with no window
    manager involved.
"""
from __future__ import annotations

import os
from typing import List, Tuple

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QLabel, QScrollArea, QTabWidget, QTreeWidget, QWidget,
)

from test_qt_tab import (  # noqa: E402,F401
    ADVANCED_CONTENT_PANES, _FakeWallet, _FakeWindow, _is_inside, _make_plugin,
    _silence_log_tab_timer, qapp,
)

SETTINGS_TAB, PROVIDERS_TAB, PARTNERS_TAB, ADVANCED_TAB = 0, 1, 2, 3

# The settings panes, as (outer tab index, inner tab title or None).
SETTINGS_PANES = [
    (SETTINGS_TAB, None),
    (PROVIDERS_TAB, None),
    (PARTNERS_TAB, None),
    (ADVANCED_TAB, "General"),
    (ADVANCED_TAB, "Reliability"),
    (ADVANCED_TAB, "Channel recovery"),
    (ADVANCED_TAB, "Usage statistics"),
]

# Sizes a user actually runs. 840x400 is Electrum's own default geometry
# (gui/qt/main_window.py), 700x450 a narrow window on a small laptop, and
# 1280x1024 a roomy one where nothing should need to scroll at all.
TIGHT_SIZES = [(840, 400), (840, 500), (700, 450)]
ROOMY_SIZE = (1280, 1024)


def _laid_out(qapp, width: int, height: int):
    """A built Liquidity tab, laid out for real at ``width`` x ``height``.

    Returns ``(plugin, wallet, show_pane)`` where ``show_pane(index, title)``
    raises a pane and returns its body widget.
    """
    plugin = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    plugin._add_liquidity_tab(window, wallet)
    container = plugin._tabs[wallet].container
    tabs: QTabWidget = container.findChild(QTabWidget)

    host = window.tabs            # the fake ElectrumWindow's real QTabWidget
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    host.show()
    host.resize(width, height)

    def show_pane(index: int, title) -> QWidget:
        tabs.setCurrentIndex(index)
        page = tabs.widget(index)
        inner = page.findChild(QTabWidget)
        if title is not None:
            assert inner is not None, f"tab {index} has no inner bar"
            for i in range(inner.count()):
                if inner.tabText(i) == title:
                    inner.setCurrentIndex(i)
                    page = inner.widget(i)
                    break
            else:
                raise AssertionError(f"no {title!r} pane behind tab {index}")
        for _ in range(4):
            qapp.processEvents()
        return page.widget() if isinstance(page, QScrollArea) else page

    return plugin, wallet, show_pane


def _enclosing_scroll_area(widget: QWidget):
    """The QScrollArea ``widget`` scrolls inside, or the one it contains.

    Handles both shapes show_pane() can hand back: an Advanced inner pane IS a
    scroll area's body (so look upwards: body -> viewport -> QScrollArea),
    while a top-level settings tab CONTAINS the scroll area alongside its
    pinned buttons (so look downwards).
    """
    node = widget.parentWidget()
    while node is not None:
        if isinstance(node, QScrollArea):
            return node
        node = node.parentWidget()
    return widget.findChild(QScrollArea)


def _clipped(pane: QWidget) -> List[Tuple[int, str]]:
    """Every visible label on ``pane`` whose text needs more vertical room than
    the layout gave it, worst first, as ``(pixels_short, text)``.

    A wrapped label's required height depends on its width, which is what
    heightForWidth answers; an unwrapped one simply needs its sizeHint. One
    pixel of slack absorbs rounding.
    """
    short = []
    for label in pane.findChildren(QLabel):
        if not label.isVisibleTo(pane):
            continue
        if label.wordWrap():
            needed = label.heightForWidth(max(1, label.width()))
        else:
            needed = label.sizeHint().height()
        if needed > label.height() + 1:
            short.append((needed - label.height(),
                          (label.text() or "")[:60].replace("\n", " ")))
    return sorted(short, reverse=True)


@pytest.mark.parametrize("width,height", TIGHT_SIZES)
@pytest.mark.parametrize("index,title", SETTINGS_PANES)
def test_no_settings_pane_clips_its_text(qapp, index, title, width, height):
    p, wallet, show_pane = _laid_out(qapp, width, height)
    pane = show_pane(index, title)
    clipped = _clipped(pane)
    assert not clipped, (
        f"{title or f'tab {index}'} at {width}x{height}: "
        f"{len(clipped)} clipped label(s); worst {clipped[0][0]}px short: "
        f"{clipped[0][1]!r}")


@pytest.mark.parametrize("index,title", SETTINGS_PANES)
def test_no_settings_pane_clips_its_text_on_a_roomy_window(qapp, index, title):
    # The control: a window big enough that nothing is under pressure. A failure
    # here is a plain layout error, not a lack of room.
    p, wallet, show_pane = _laid_out(qapp, *ROOMY_SIZE)
    clipped = _clipped(show_pane(index, title))
    assert not clipped, clipped


@pytest.mark.parametrize("index", [PROVIDERS_TAB, PARTNERS_TAB])
def test_the_tables_stay_usable_on_a_short_window(qapp, index):
    """The second half of the same bug: at 840x400 the squeeze landed on the
    tables, leaving 26-51px -- a header and no rows. The page must scroll
    instead of crushing them."""
    p, wallet, show_pane = _laid_out(qapp, 840, 400)
    tree = show_pane(index, None).findChild(QTreeWidget)
    assert tree is not None
    assert tree.height() >= 150, (
        f"the table is {tree.height()}px tall; it was squeezed, not scrolled")


@pytest.mark.parametrize("index", [PROVIDERS_TAB, PARTNERS_TAB])
def test_the_tables_still_fill_a_tall_window(qapp, index):
    """A scroll area must not cap what it holds. These tables expanded to fill
    the tab before, and a fix for short windows that stopped them growing on
    tall ones would trade one complaint for another."""
    p, wallet, show_pane = _laid_out(qapp, 840, 1200)
    tree = show_pane(index, None).findChild(QTreeWidget)
    assert tree.height() >= 500, (
        f"the table is only {tree.height()}px tall on a 1200px window")


def test_every_settings_pane_scrolls(qapp):
    """The mechanism, asserted once: each settings pane is wrapped in a scroll
    area that follows the panel width."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tabs: QTabWidget = p._tabs[wallet].container.findChild(QTabWidget)

    for index, title in SETTINGS_PANES:
        page = tabs.widget(index)
        if title is not None:
            inner = page.findChild(QTabWidget)
            page = next(inner.widget(i) for i in range(inner.count())
                        if inner.tabText(i) == title)
        scroll = page if isinstance(page, QScrollArea) else page.findChild(QScrollArea)
        name = title or tabs.tabText(index)
        assert scroll is not None, f"{name} does not scroll"
        assert scroll.widgetResizable(), f"{name} would not follow the panel width"
        # Neither bar may be switched off: a pane with a minimum width of its
        # own (Channel partners needs 571px) really does overflow on a narrow
        # window, and AlwaysOff would hide that content with no way to reach it.
        for axis, policy in (("vertical", scroll.verticalScrollBarPolicy()),
                             ("horizontal", scroll.horizontalScrollBarPolicy())):
            assert policy != Qt.ScrollBarPolicy.ScrollBarAlwaysOff, \
                f"{name}: {axis} scrolling is switched off"


@pytest.mark.parametrize("width", [1280, 840, 700, 520, 420])
def test_no_pane_hides_content_sideways(qapp, width):
    """Whatever the window width, content that does not fit across must still be
    reachable: either the body fits the viewport, or there is a horizontal
    scrollbar with range to recover the overflow.

    Both halves are real. At ordinary widths the body is resized to the
    viewport and no horizontal bar appears at all -- which is what keeps the
    provider table the only thing scrolling sideways. Below a pane's own
    minimum width the body cannot shrink further and genuinely overflows, and
    then the bar has to be there.
    """
    p, wallet, show_pane = _laid_out(qapp, width, 700)
    for index, title in SETTINGS_PANES:
        # show_pane() is what forces the layout pass; the body to measure comes
        # from the scroll area itself, because show_pane hands back the tab for
        # a top-level pane and the body for an Advanced inner one.
        scroll = _enclosing_scroll_area(show_pane(index, title))
        assert scroll is not None, title or index
        body = scroll.widget()
        name = title or f"tab {index}"
        overflow = body.width() - scroll.viewport().width()
        if overflow <= 1:
            continue
        bar = scroll.horizontalScrollBar()
        # Range alone is not enough: with AlwaysOff Qt still gives the bar a
        # range, it simply never shows it -- so the content would be just as
        # unreachable. What matters is that the bar is actually on screen.
        assert bar.isVisible(), (
            f"{name} at {width}px: body overflows by {overflow}px with no "
            f"horizontal scrollbar shown")
        assert bar.maximum() >= overflow - 1, (
            f"{name} at {width}px: body overflows by {overflow}px but the "
            f"horizontal scrollbar only reaches {bar.maximum()}px")


def test_the_apply_buttons_are_not_inside_the_scrolling_area(qapp):
    """An Apply below the fold, on a pane whose every edit is Apply-gated, is an
    Apply the user cannot find. Same argument the usage-consent dialog makes for
    keeping its buttons outside its scroll area."""
    from PyQt6.QtWidgets import QPushButton
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    tabs: QTabWidget = p._tabs[wallet].container.findChild(QTabWidget)

    for index in (SETTINGS_TAB, PROVIDERS_TAB, PARTNERS_TAB, ADVANCED_TAB):
        page = tabs.widget(index)
        scroll = page.findChild(QScrollArea)
        assert scroll is not None
        body = scroll.widget()
        applies = [b for b in page.findChildren(QPushButton) if b.text() == "Apply"]
        assert applies, f"tab {tabs.tabText(index)} has no Apply"
        for btn in applies:
            # The Advanced tab also holds the usage pane's Apply, which lives in
            # its own scroll area; what matters is that no Apply is inside the
            # scroll area belonging to the form it commits.
            assert not _is_inside(btn, body), \
                f"{tabs.tabText(index)}: Apply scrolls away with the form"


def test_the_advanced_form_has_exactly_one_apply(qapp):
    """Its three content panes are one form in three views: one tracker, one
    Apply, one Reset, pinned below the inner bar where all three can reach
    them."""
    from PyQt6.QtWidgets import QPushButton
    from test_qt_tab import _advanced_apply, _advanced_pane, _advanced_tab
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    advanced = _advanced_tab(p, wallet)
    inner = advanced.findChild(QTabWidget)
    # One Apply and one Reset outside the inner bar...
    pinned = [b.text() for b in advanced.findChildren(QPushButton)
              if not _is_inside(b, inner)]
    assert sorted(pinned) == ["Apply", "Reset this wallet to defaults"], pinned
    # ...and none of the three content panes carries one of its own.
    for title in ADVANCED_CONTENT_PANES:
        pane = _advanced_pane(p, wallet, title)
        assert [b.text() for b in pane.findChildren(QPushButton)] == [], title
    assert _advanced_apply(p, wallet) is not None


def test_the_advanced_fields_are_split_across_the_three_panes(qapp):
    """Where each knob was filed. Asserted because the routing in
    _build_advanced_tab keys off the field that OPENS each group, so a rename
    there would silently move a whole group onto the previous pane.
    """
    from test_qt_tab import _advanced_pane
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)

    def labels(title: str) -> List[str]:
        return [lbl.text() for lbl in
                _advanced_pane(p, wallet, title).findChildren(QLabel)]

    general = labels("General")
    assert any(t.startswith("Max channel opens per day") for t in general)
    assert any(t.startswith("Max channel closes per day") for t in general)
    assert any(t.startswith("On-chain reserve") for t in general)
    assert any(t.startswith("Keep decision log") for t in general)
    assert any(t.startswith("Minimum swap size") for t in general)

    reliability = labels("Reliability")
    assert any(t.startswith("Reliability penalty per fault") for t in reliability)
    assert any(t.startswith("Max reliability penalty") for t in reliability)
    assert any(t.startswith("Auto-ban a peer") for t in reliability)

    recovery = labels("Channel recovery")
    assert any(t.startswith("Stuck channel-open timeout") for t in recovery)
    assert any(t.startswith("Wedged-open:") for t in recovery)
    assert any(t.startswith("Offline auto-close:") for t in recovery)
    assert any(t.startswith("Extra channels while replacing") for t in recovery)

    # Each knob on exactly one pane, so nothing was duplicated by the routing.
    for a, b in ((general, reliability), (general, recovery),
                 (reliability, recovery)):
        overlap = {t for t in a if t and t in set(b)}
        assert not overlap, overlap


def test_a_rejected_advanced_apply_shows_the_offending_pane(qapp):
    """One Apply covering three panes has to be able to say WHERE the bad value
    is: the message names the field, but the field may be on a pane the user is
    not looking at."""
    from test_qt_tab import _advanced_apply, _advanced_field, _advanced_pane
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._add_liquidity_tab(window, wallet)
    advanced = p._tabs[wallet].container.findChild(QTabWidget).widget(ADVANCED_TAB)
    inner = advanced.findChild(QTabWidget)
    pane = [p_ for p_ in p._tabs[wallet].panes if p_.name == "Advanced"][0]

    # A bad value on the LAST content pane, with the first one showing.
    _advanced_field(p, wallet, "Stuck channel-open timeout").setText("not-a-number")
    inner.setCurrentIndex(0)
    assert not _advanced_apply(p, wallet).click()    # click() returns None
    pane.bring_into_view()
    qapp.processEvents()
    assert inner.tabText(inner.currentIndex()) == "Channel recovery", \
        "the refusal left the user on a pane with no bad value on it"

    # And with nothing to single out, it falls back to General rather than
    # leaving the user wherever they were.
    _advanced_field(p, wallet, "Stuck channel-open timeout").setText("30")
    _advanced_apply(p, wallet).click()
    inner.setCurrentIndex(1)
    pane.bring_into_view()
    qapp.processEvents()
    assert inner.tabText(inner.currentIndex()) == "General"
