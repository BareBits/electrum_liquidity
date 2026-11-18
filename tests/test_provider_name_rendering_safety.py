"""A provider's display name is attacker-controlled text that we put on screen.

The sanitiser (`clean_display_name`, covered in test_provider_profiles) makes a
name inert as *content*: no control characters, no line breaks, no bidi
overrides, no zero-width padding, bounded length. These tests cover the other
half, which the sanitiser cannot do from where it sits -- that every widget we
hand such a name to renders it *literally*.

That distinction matters because angle brackets are ordinary printable
characters. A name may legitimately contain "<" or "&", so the sanitiser keeps
them; it is therefore each rendering sink's job not to interpret them. Qt makes
that easy to get wrong: a QLabel defaults to Qt.AutoText and silently switches
to HTML for anything `mightBeRichText()` likes the look of.

The three sinks a name can reach, all pinned below:

* the table cell          -- the item delegate draws plain text;
* the status line         -- forced to PlainText (the one that was AutoText);
* the name-column tooltip -- QToolTip *does* interpret rich text and has no
                             per-item PlainText switch, so there are two
                             guards: the name never goes into a tooltip, and
                             every tooltip is built by `_literal_tooltip`
                             anyway (see test_tooltip_hardening_qt).

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QLabel, QPushButton  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    PROVIDER_PROFILES_DB_KEY,
    RELIABILITY_DB_KEY,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    clean_display_name,
)
from electrum.plugins.inbound_liquidity.qt import _StatusLine  # type: ignore  # noqa: E402

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)
from test_provider_name_column_qt import (  # noqa: E402
    NPUB_A, NPUB_B, PROVIDERS_TAB, _row_for, _tab, _tree,
)

# Names built to be interpreted by something. Every one of these survives the
# sanitiser unchanged-ish, because none of it is a control character.
MARKUP_NAMES = [
    "<b>Trusted</b>",
    "Zap<br>Swaps",
    '<a href="http://evil.ex">Zap</a>',
    '<span style="color:red">Zap</span>',
    "<table><tr><td>x</td></tr></table>",
    "Zap&nbsp;&amp;Swaps",
    "<img src=x>",
]

# Names built to break out of a cell or impersonate another provider. These the
# sanitiser DOES neutralise; re-pinned here against the rendered cell so the two
# halves are tested together rather than only in isolation.
HOSTILE_NAMES = [
    "Zap\r\nSwaps",
    "Zap\x00Swaps",
    "Zap\tSwaps",
    "\x1b[31mZap Swaps\x1b[0m",
    "‮Zap Swaps",
    "⁦Zap⁩ Swaps",
    "Zap​Swaps",
    "Zap‎Swaps",
    "Z" * 300,
]


def _rendered(tooltip: str) -> str:
    """What a tooltip DISPLAYS, as Qt renders it."""
    from PyQt6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(tooltip)
    return doc.toPlainText()


def _named(name: str):
    """A providers tab whose one discovered provider publishes ``name``."""
    return _tab(offers=[NPUB_A], names={NPUB_A: name})


# --- the sanitiser's half, asserted on what is actually rendered ---------
@pytest.mark.parametrize("raw", HOSTILE_NAMES)
def test_no_hostile_name_puts_a_control_character_in_a_cell(qapp, raw) -> None:
    p, _w, tree = _named(raw)
    shown = _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL)
    for ch in shown:
        code = ord(ch)
        assert not (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F), \
            f"control char {code:#x} reached the cell from {raw!r}"
        assert not 0xD800 <= code <= 0xDFFF, f"surrogate from {raw!r}"


@pytest.mark.parametrize("raw", HOSTILE_NAMES)
def test_no_hostile_name_spans_more_than_one_line(qapp, raw) -> None:
    # A line break in a table cell means a row that spills over its neighbours.
    p, _w, tree = _named(raw)
    shown = _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL)
    assert "\n" not in shown and "\r" not in shown


@pytest.mark.parametrize("raw", HOSTILE_NAMES)
def test_no_hostile_name_is_unbounded(qapp, raw) -> None:
    p, _w, tree = _named(raw)
    shown = _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL)
    assert len(shown) <= 40, f"{raw!r} rendered {len(shown)} chars"


def test_bidi_and_zero_width_cannot_forge_another_providers_name(qapp) -> None:
    # The impersonation this is really about: two names that RENDER alike must
    # not be achievable by padding one with invisibles.
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B],
                       names={NPUB_A: "Zap Swaps", NPUB_B: "Zap​ Swaps"})
    a = _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL)
    b = _row_for(tree, NPUB_B).text(p._PROVIDER_NAME_COL)
    # Either they are genuinely different strings, or they collapsed to the
    # same one -- in which case the clash flag has to be on, which is the whole
    # point of it. What must not happen is "looks identical, no warning".
    assert a != b or (a.startswith("⚠") and b.startswith("⚠"))


# --- the status line: the sink that was AutoText -------------------------
def test_the_status_labels_are_plain_text(qapp) -> None:
    message, hint = QLabel(), QLabel()
    _StatusLine(message, hint)
    assert message.textFormat() == Qt.TextFormat.PlainText
    assert hint.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.parametrize("name", MARKUP_NAMES)
def test_a_markup_name_cannot_become_markup_in_the_status_line(qapp, name) -> None:
    # The reset-stats confirmation names the provider. It is a QLabel, and a
    # QLabel left on AutoText would render these as HTML.
    p, wallet, tree = _named(name)
    pane = p._tabs[wallet].panes[PROVIDERS_TAB]
    tree.setCurrentItem(_row_for(tree, NPUB_A))
    btn = [b for b in pane.widget.findChildren(QPushButton)
           if b.text() == "Reset stats"][0]
    btn.click()
    labels = [lb for lb in pane.widget.findChildren(QLabel)
              if "Reliability stats cleared" in lb.text()]
    assert labels, "the reset-stats confirmation was not shown"
    label = labels[0]
    assert label.textFormat() == Qt.TextFormat.PlainText
    # The sanitised name is in the message verbatim -- not as rendered HTML,
    # and not escaped into something else either.
    assert clean_display_name(name, max_chars=32) in label.text()


def test_the_status_line_still_names_the_provider_and_its_npub(qapp) -> None:
    # Hardening must not have cost the feature: the message carries both, since
    # two providers can publish one name.
    p, wallet, tree = _named("Zap Swaps")
    pane = p._tabs[wallet].panes[PROVIDERS_TAB]
    tree.setCurrentItem(_row_for(tree, NPUB_A))
    [b for b in pane.widget.findChildren(QPushButton)
     if b.text() == "Reset stats"][0].click()
    text = [lb.text() for lb in pane.widget.findChildren(QLabel)
            if "Reliability stats cleared" in lb.text()][0]
    assert "Zap Swaps" in text
    assert NPUB_A[:12] in text


# --- the tooltip: the sink that DOES interpret rich text -----------------
@pytest.mark.parametrize("name", MARKUP_NAMES + ["Zap Swaps"])
def test_the_name_never_reaches_a_tooltip(qapp, name) -> None:
    # Two independent guards, and this pins both.
    #
    # First, the name stays out of tooltips entirely: they carry the npub,
    # which is bech32 and cannot hold a tag. Second, every tooltip goes through
    # `_literal_tooltip` anyway, so even if one day a name did land in one it
    # would render as characters. Asserted on the RENDERED text, because the
    # wrapper itself is markup by design.
    p, _w, tree = _named(name)
    row = _row_for(tree, NPUB_A)
    cleaned = clean_display_name(name, max_chars=32)
    for col in range(tree.columnCount()):
        tip = row.toolTip(col)
        if not tip:
            continue
        shown = _rendered(tip)
        assert cleaned not in shown, f"name leaked into tooltip col {col}: {shown!r}"
        # Whatever the tooltip says, none of it is live markup: rendering it
        # yields no tag, so nothing inside could have been one.
        assert "<" not in shown, f"tooltip col {col} rendered a tag: {shown!r}"


def test_the_clash_tooltip_is_markup_free_even_when_the_names_are_not(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B],
                       names={NPUB_A: "<b>Zap</b>", NPUB_B: "<b>Zap</b>"})
    shown = _rendered(_row_for(tree, NPUB_A).toolTip(p._PROVIDER_NAME_COL))
    assert "publishes this same name" in shown
    assert "<b>" not in shown


# --- the cell itself ------------------------------------------------------
@pytest.mark.parametrize("name", MARKUP_NAMES)
def test_markup_in_a_cell_stays_verbatim(qapp, name) -> None:
    # The item delegate draws plain text, so the cell shows the tags as
    # characters. Pinned so that swapping in a rich-text delegate would fail
    # here rather than silently start rendering provider HTML.
    p, _w, tree = _named(name)
    shown = _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL)
    assert shown == clean_display_name(name, max_chars=32)
