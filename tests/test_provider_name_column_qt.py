"""The Swap providers table's human-readable "Provider name" column.

Column 0 is the name a provider publishes for itself in its nostr profile; the
npub moved to column 1 and every column after it shifted by one. That shift is
the risk in this change -- the Preferred/Banned check states are addressed BY
INDEX, so getting it wrong bans the provider the user meant to prefer -- so
these tests pin the header layout, the checkbox columns, and that the row's
identity still rides on column 0's UserRole where every reader expects it.

The rest is about a name being a LABEL and not an identity: a provider that
publishes none reads "—", two providers claiming one name are both flagged, and
the npub is on screen either way.

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

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    PROVIDER_PROFILES_DB_KEY,
    RELIABILITY_DB_KEY,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
)
from electrum.plugins.inbound_liquidity.qt import _NO_NAME_SORT_KEY  # type: ignore  # noqa: E402

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)
from test_sortable_columns_qt import (  # noqa: E402
    PROVIDERS_TAB, _click_header, _column, _ids, _tree,
)

NPUB_A = "npub1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NPUB_B = "npub1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NPUB_C = "npub1cccccccccccccccccccccccccccccccccc"


def _tab(*, offers=(), names=None, history=None):
    """A providers tab for the given live providers, with ``names`` already in
    the profile cache and ``history`` in the reliability store."""
    p = _make_plugin()
    window, wallet = _FakeWindow(), _FakeWallet()
    p._last_offers[wallet] = [
        ProviderOffer(npub=npub, percentage_fee=0.5, mining_fee_sat=100,
                      min_amount_sat=1_000, max_reverse_sat=1_000_000)
        for npub in offers
    ]
    if names:
        now = time.time()
        wallet.db.put(PROVIDER_PROFILES_DB_KEY, {
            npub: {"name": name, "checked_ts": now}
            for npub, name in names.items()})
    if history:
        wallet.db.put(RELIABILITY_DB_KEY, dict(history))
    p._add_liquidity_tab(window, wallet)
    return p, wallet, _tree(p, wallet, PROVIDERS_TAB)


def _headers(tree):
    header = tree.headerItem()
    return [header.text(i) for i in range(tree.columnCount())]


def _tooltip_text(item, col: int) -> str:
    """What a tooltip DISPLAYS. They are built by `_literal_tooltip`, which
    escapes the content so a tag inside it cannot become markup; these tests
    care about the characters shown, so they render it back."""
    from PyQt6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(item.toolTip(col))
    return doc.toPlainText()


def _row_for(tree, npub):
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        if item.data(0, Qt.ItemDataRole.UserRole) == npub:
            return item
    raise AssertionError(f"no row for {npub}")


# --- the header layout ---------------------------------------------------
def test_the_name_column_comes_first_and_the_npub_follows_it(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A])
    headers = _headers(tree)
    assert headers[p._PROVIDER_NAME_COL] == "Provider name"
    assert headers[p._NPUB_COL] == "Provider (npub)"


def test_every_column_still_has_a_header(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A])
    headers = _headers(tree)
    assert tree.columnCount() == p._LAST_FAULT_COL + 1
    assert all(headers)
    # Spot-check the far end: a cell list one short of the headers would
    # silently misalign everything to the right of the insertion.
    assert headers[p._PREF_COL] == "Preferred"
    assert headers[p._BAN_COL] == "Banned"
    assert headers[p._LAST_FAULT_COL] == "Last fault"


def test_the_terms_columns_are_where_their_constants_say(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A])
    headers = _headers(tree)
    assert headers[p._FEE_COL] == "Fee %"
    assert headers[p._MINING_COL] == "Mining (sat)"
    assert headers[p._MIN_COL] == "Min (sat)"
    assert headers[p._MAX_COL] == "Max (sat)"
    assert headers[p._POW_COL] == "PoW"


# --- the identity did not move -------------------------------------------
def test_the_row_identity_still_rides_on_column_zero(qapp) -> None:
    # Every `data(0, UserRole)` reader in the tab depends on this, including
    # the one that decides what gets saved to the banned list.
    _p, _w, tree = _tab(offers=[NPUB_A], names={NPUB_A: "Fast Swaps"})
    assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == NPUB_A


def test_ticking_preferred_saves_the_npub_not_the_name(qapp) -> None:
    p, wallet, tree = _tab(offers=[NPUB_A], names={NPUB_A: "Fast Swaps"})
    _row_for(tree, NPUB_A).setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    p._tabs[wallet].panes[PROVIDERS_TAB].apply()
    assert p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS == NPUB_A


def test_ticking_banned_saves_the_npub_not_the_name(qapp) -> None:
    # The index shift's worst failure mode is writing the wrong one of these.
    p, wallet, tree = _tab(offers=[NPUB_A], names={NPUB_A: "Fast Swaps"})
    _row_for(tree, NPUB_A).setCheckState(p._BAN_COL, Qt.CheckState.Checked)
    p._tabs[wallet].panes[PROVIDERS_TAB].apply()
    assert p.settings(wallet).INBOUND_LIQUIDITY_BANNED_NPUBS == NPUB_A
    assert p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS == ""


# --- the cell itself -----------------------------------------------------
def test_a_published_name_is_shown(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A], names={NPUB_A: "Fast Swaps"})
    assert _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL) == "Fast Swaps"


def test_a_provider_with_no_name_reads_as_a_dash(qapp) -> None:
    # "" would read as a rendering bug; "—" says "there is no name here".
    p, _w, tree = _tab(offers=[NPUB_A])
    assert _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL) == "—"


def test_the_npub_is_still_on_screen_beside_the_name(qapp) -> None:
    # The name never REPLACES the identity.
    p, _w, tree = _tab(offers=[NPUB_A], names={NPUB_A: "Fast Swaps"})
    row = _row_for(tree, NPUB_A)
    assert row.text(p._NPUB_COL)
    assert row.text(p._NPUB_COL) != "Fast Swaps"
    # ...and the full npub is a tooltip away, since the cell is abbreviated.
    assert _tooltip_text(row, p._NPUB_COL) == NPUB_A


def test_an_offline_provider_still_shows_its_cached_name(qapp) -> None:
    # The whole reason the cache is persisted: a provider with fault history but
    # no current offer advertises nothing, so its name can only come from here.
    p, _w, tree = _tab(names={NPUB_A: "Long Gone"},
                       history={NPUB_A: {"fault_count": 2}})
    row = _row_for(tree, NPUB_A)
    assert row.text(p._PROVIDER_NAME_COL) == "Long Gone"
    assert row.text(p._FEE_COL) == "(offline)"


def test_the_name_cell_explains_itself_in_a_tooltip(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B], names={NPUB_A: "Fast Swaps"})
    named = _tooltip_text(_row_for(tree, NPUB_A), p._PROVIDER_NAME_COL)
    assert "nothing verifies it" in named
    assert NPUB_A in named
    unnamed = _tooltip_text(_row_for(tree, NPUB_B), p._PROVIDER_NAME_COL)
    assert "no nostr profile name" in unnamed
    assert NPUB_B in unnamed


# --- impersonation -------------------------------------------------------
def test_two_providers_claiming_one_name_are_both_flagged(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B],
                       names={NPUB_A: "Fast Swaps", NPUB_B: "Fast Swaps"})
    for npub in (NPUB_A, NPUB_B):
        assert _row_for(tree, npub).text(p._PROVIDER_NAME_COL) == "⚠ Fast Swaps"


def test_a_flagged_row_says_why_in_its_tooltip(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B],
                       names={NPUB_A: "Fast Swaps", NPUB_B: "Fast Swaps"})
    tip = _tooltip_text(_row_for(tree, NPUB_A), p._PROVIDER_NAME_COL)
    assert "publishes this same name" in tip


def test_an_unshared_name_is_not_flagged(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B],
                       names={NPUB_A: "Fast Swaps", NPUB_B: "Bargain Bin"})
    assert _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL) == "Fast Swaps"
    assert _row_for(tree, NPUB_B).text(p._PROVIDER_NAME_COL) == "Bargain Bin"


def test_several_unnamed_providers_are_not_treated_as_a_clash(qapp) -> None:
    # "no name" is not a name two providers share; three dashes is normal.
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B, NPUB_C])
    assert _column(tree, p._PROVIDER_NAME_COL) == ["—", "—", "—"]


def test_an_offline_impersonator_is_still_flagged(qapp) -> None:
    # The clash is computed over every row on screen, not just the live ones --
    # a provider that has stopped advertising can still be the impersonator.
    p, _w, tree = _tab(offers=[NPUB_A],
                       names={NPUB_A: "Fast Swaps", NPUB_B: "Fast Swaps"},
                       history={NPUB_B: {"fault_count": 1}})
    assert _row_for(tree, NPUB_A).text(p._PROVIDER_NAME_COL) == "⚠ Fast Swaps"
    assert _row_for(tree, NPUB_B).text(p._PROVIDER_NAME_COL) == "⚠ Fast Swaps"


# --- sorting -------------------------------------------------------------
def test_sorting_by_name_orders_alphabetically(qapp) -> None:
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B, NPUB_C],
                       names={NPUB_A: "Zeta Swaps", NPUB_B: "alpha swaps",
                              NPUB_C: "Mid Swaps"})
    _click_header(tree, p._PROVIDER_NAME_COL)
    assert _ids(tree) == [NPUB_B, NPUB_C, NPUB_A]   # case-insensitive


def test_an_unnamed_provider_sorts_last_either_way(qapp) -> None:
    # Ascending it must not head the list just because "—" or "" happens to
    # compare low; descending it must not either.
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B, NPUB_C],
                       names={NPUB_A: "Zeta Swaps", NPUB_C: "alpha swaps"})
    _click_header(tree, p._PROVIDER_NAME_COL)
    assert _ids(tree)[-1] == NPUB_B
    _click_header(tree, p._PROVIDER_NAME_COL)       # reverse
    assert _ids(tree)[0] == NPUB_B


def test_the_warning_marker_does_not_reorder_the_column(qapp) -> None:
    # Sorting is on the name, not on the rendered "⚠ ..." text, so the two
    # rows claiming one name stay adjacent rather than being pushed together
    # with every other flagged row.
    p, _w, tree = _tab(offers=[NPUB_A, NPUB_B, NPUB_C],
                       names={NPUB_A: "Bravo", NPUB_B: "Alpha",
                              NPUB_C: "Alpha"})
    _click_header(tree, p._PROVIDER_NAME_COL)
    assert _ids(tree)[-1] == NPUB_A                 # "Bravo" last
    assert set(_ids(tree)[:2]) == {NPUB_B, NPUB_C}  # the two "Alpha"s first


def test_the_no_name_sort_key_cannot_collide_with_a_real_name(qapp) -> None:
    # The sentinel sorts unnamed rows last by being above every character a
    # sanitised name can contain. It is unprintable, so the sanitiser strips
    # it -- that is what makes the collision impossible rather than unlikely.
    from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore
        clean_display_name,
    )
    assert not _NO_NAME_SORT_KEY.isprintable()
    assert clean_display_name(_NO_NAME_SORT_KEY) == ""
    assert clean_display_name(f"Fast{_NO_NAME_SORT_KEY}Swaps") == "FastSwaps"
