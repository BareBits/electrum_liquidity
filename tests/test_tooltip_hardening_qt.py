"""Every tooltip built from outside data renders that data literally.

QToolTip interprets rich text, and -- unlike QLabel -- there is no per-item
PlainText switch: Qt decides per string via `mightBeRichText()`. So a tooltip
assembled from a peer's gossip alias, a node id, or a provider's fault reason
would silently become markup the moment that text contained a tag, which is how
a node called "ACINQ" turns into a link somewhere else.

`_literal_tooltip` is the gate: it escapes the text and wraps it in markup we
wrote, so what the user sees is the characters the remote party actually sent.

These tests read tooltips back through the renderer Qt uses, so they assert on
what is DISPLAYED rather than on the wrapper.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QTextDocument  # noqa: E402

from electrum.plugins.inbound_liquidity.qt import (  # type: ignore  # noqa: E402
    MAX_TOOLTIP_CHARS,
    _literal_tooltip,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)
from test_partners_name_column_qt import (  # noqa: E402
    NODE_A, _build, _partners_tree, _with_channels,
)

MARKUP = [
    '<a href="http://evil.ex">ACINQ</a>',
    "<b>ACINQ</b>",
    "ACINQ<br>spoofed",
    '<img src="http://evil.ex/track.png">',
    "<span style='color:red'>ACINQ</span>",
    "a &amp; b",
    "<table><tr><td>x</td></tr></table>",
]


def _rendered(tooltip: str) -> str:
    doc = QTextDocument()
    doc.setHtml(tooltip)
    return doc.toPlainText()


# --- the helper itself ----------------------------------------------------
@pytest.mark.parametrize("text", MARKUP)
def test_markup_survives_as_characters_not_as_markup(text) -> None:
    assert _rendered(_literal_tooltip(text)) == text


def test_newlines_are_preserved() -> None:
    # A tooltip's lines are its structure; plain rich text would collapse them.
    out = _rendered(_literal_tooltip("ACINQ\n02aa"))
    assert out == "ACINQ\n02aa"


def test_the_wrapper_is_the_only_markup() -> None:
    tip = _literal_tooltip("<b>x</b>")
    assert tip.startswith("<div style='white-space:pre-wrap'>")
    assert tip.endswith("</div>")
    assert "&lt;b&gt;" in tip          # the payload is escaped
    assert "<b>" not in tip            # ...and not live


def test_an_unbounded_tooltip_is_cut() -> None:
    # A tooltip is a floating window sized to its content.
    out = _rendered(_literal_tooltip("A" * (MAX_TOOLTIP_CHARS * 3)))
    assert len(out) == MAX_TOOLTIP_CHARS
    assert out.endswith("…")


def test_degenerate_input_does_not_raise() -> None:
    for value in (None, "", 0, 42, [], {"a": 1}):
        assert isinstance(_literal_tooltip(value), str)
    assert _rendered(_literal_tooltip(None)) == ""


# --- the partners tab's name tooltip (gossip alias + node id) ------------
@pytest.mark.parametrize("alias", MARKUP)
def test_a_hostile_peer_alias_cannot_become_markup_in_its_tooltip(qapp, alias) -> None:
    # The alias is chosen by the REMOTE node and arrives over gossip.
    p, _w, tree = _build({NODE_A: alias})
    shown = _rendered(tree.topLevelItem(0).toolTip(p._PARTNER_NAME_COL))
    cell = tree.topLevelItem(0).text(p._PARTNER_NAME_COL)
    # The tooltip shows the sanitised alias and the full node id, both literal.
    assert cell in shown
    assert NODE_A in shown
    assert "<a href=" not in tree.topLevelItem(0).toolTip(p._PARTNER_NAME_COL) \
        .replace("<div style='white-space:pre-wrap'>", "").replace("</div>", "")


def test_the_name_tooltip_still_shows_both_values(qapp) -> None:
    # Hardening must not have cost the tooltip its job.
    p, _w, tree = _build({NODE_A: "ACINQ"})
    assert _rendered(tree.topLevelItem(0).toolTip(p._PARTNER_NAME_COL)) \
        == f"ACINQ\n{NODE_A}"


# --- fault-reason tooltips ------------------------------------------------
@pytest.mark.parametrize("reason", MARKUP)
def test_a_hostile_peer_fault_reason_cannot_become_markup(qapp, reason) -> None:
    p = _make_plugin()
    window = _FakeWindow()
    wallet = _with_channels(_FakeWallet(), {NODE_A: "ACINQ"})
    p._add_liquidity_tab(window, wallet)
    p._record_peer_fault(wallet, NODE_A, reason, hard=False)
    p._tabs[wallet].refresh()
    row = _partners_tree(p, wallet).topLevelItem(0)
    assert _rendered(row.toolTip(p._PARTNER_FAULTS_COL)) == reason


def test_a_control_character_in_a_reason_is_escaped_not_dropped(qapp) -> None:
    # Reasons are diagnostic text: a CR/LF is made visible ("\\n") rather than
    # removed, so the forensic fact that it was there survives while it can no
    # longer forge a line.
    p = _make_plugin()
    window = _FakeWindow()
    wallet = _with_channels(_FakeWallet(), {NODE_A: "ACINQ"})
    p._add_liquidity_tab(window, wallet)
    p._record_peer_fault(wallet, NODE_A, "timed out\nFAKE: all good", hard=False)
    p._tabs[wallet].refresh()
    row = _partners_tree(p, wallet).topLevelItem(0)
    shown = _rendered(row.toolTip(p._PARTNER_FAULTS_COL))
    assert "\n" not in shown
    assert "\\n" in shown


def test_an_unbounded_reason_is_bounded_on_the_way_out(qapp) -> None:
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        MAX_FAULT_REASON_CHARS,
    )
    p = _make_plugin()
    window = _FakeWindow()
    wallet = _with_channels(_FakeWallet(), {NODE_A: "ACINQ"})
    p._add_liquidity_tab(window, wallet)
    p._record_peer_fault(wallet, NODE_A, "X" * 5000, hard=False)
    p._tabs[wallet].refresh()
    rows = p.peer_reliability_rows(wallet)
    assert len(rows[NODE_A]["last_reason"]) <= MAX_FAULT_REASON_CHARS + 20


def test_a_reason_that_scrubs_to_nothing_reads_as_none(qapp) -> None:
    p = _make_plugin()
    window = _FakeWindow()
    wallet = _with_channels(_FakeWallet(), {NODE_A: "ACINQ"})
    p._add_liquidity_tab(window, wallet)
    p._record_peer_fault(wallet, NODE_A, "", hard=False)
    assert p.peer_reliability_rows(wallet)[NODE_A]["last_reason"] is None
