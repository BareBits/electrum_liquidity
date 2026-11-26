"""The Swap providers tab's three buttons: Apply, Reset stats, Refresh.

Every other test of this tab asserts what it RENDERS. These assert what its
buttons DO, which was untested -- all three handlers were uncovered -- even
though two of them write wallet state and one of those is destructive:

  * Apply persists the preferred/banned lists. It is the only way the
    per-row checkboxes reach the wallet, and it has a rule the text fields
    cannot express: a banned provider is removed from preferred, so the two
    lists can never disagree about the same npub.
  * Reset stats clears reliability history -- all of it, if no row is selected.
    That throws away the fault tallies the auto-penalty and auto-ban are
    computed from, so "which rows did it clear" is the whole question.
  * Refresh schedules discovery on the wallet's asyncio loop, and must say so
    rather than appear to do nothing when the wallet is offline.

Driven by clicking the real QPushButtons found in the built tab, so the wiring
(`clicked.connect`) is covered too rather than just the handler bodies.

Shares the Qt scaffolding with test_qt_tab.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("electrum.plugins.inbound_liquidity")
pytest.importorskip("electrum.gui.qt.util")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QLabel, QPushButton, QTreeWidget  # noqa: E402

from electrum.plugins.inbound_liquidity import RELIABILITY_DB_KEY  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ProviderOffer,
)

from test_qt_tab import (  # noqa: E402,F401
    _FakeWallet, _FakeWindow, _make_plugin,
    _silence_log_tab_timer, qapp,
)
from test_sortable_columns_qt import PROVIDERS_TAB, _sub_tab  # noqa: E402

NPUB_A = "npub1" + "a" * 58
NPUB_B = "npub1" + "b" * 58
NPUB_C = "npub1" + "c" * 58


def _offer(npub: str) -> ProviderOffer:
    return ProviderOffer(npub=npub, percentage_fee=0.5, mining_fee_sat=100,
                         min_amount_sat=1_000, max_reverse_sat=1_000_000,
                         pow_bits=0)


def _tab_with_providers(npubs=(NPUB_A, NPUB_B, NPUB_C), *, reliability=None,
                        wallet=None):
    """A built providers tab for the given live providers."""
    p = _make_plugin()
    window = _FakeWindow()
    wallet = wallet if wallet is not None else _FakeWallet()
    p._last_offers[wallet] = [_offer(n) for n in npubs]
    if reliability:
        wallet.db.put(RELIABILITY_DB_KEY, dict(reliability))
    p._add_liquidity_tab(window, wallet)
    return p, wallet, _sub_tab(p, wallet, PROVIDERS_TAB)


def _button(tab, label: str) -> QPushButton:
    for btn in tab.findChildren(QPushButton):
        if btn.text().replace("&", "") == label:
            return btn
    raise AssertionError(
        f"no {label!r} button; found "
        f"{[b.text() for b in tab.findChildren(QPushButton)]}")


def _tree(tab) -> QTreeWidget:
    return tab.findChild(QTreeWidget)


def _row(tree: QTreeWidget, npub: str):
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        if item.data(0, Qt.ItemDataRole.UserRole) == npub:
            return item
    raise AssertionError(f"no row for {npub[:12]}…")


def _saved(plugin, wallet) -> Dict[str, str]:
    c = plugin.settings(wallet)
    return {
        "preferred": c.INBOUND_LIQUIDITY_PREFERRED_NPUBS or "",
        "banned": c.INBOUND_LIQUIDITY_BANNED_NPUBS or "",
    }


# --- Apply ----------------------------------------------------------------
def test_apply_persists_the_checked_rows(qapp) -> None:
    """Ticking a row's Preferred box and pressing Apply is the whole path from
    a checkbox to the wallet; nothing else writes those lists."""
    p, wallet, tab = _tab_with_providers()
    tree = _tree(tab)
    _row(tree, NPUB_A).setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    _row(tree, NPUB_B).setCheckState(p._BAN_COL, Qt.CheckState.Checked)

    _button(tab, "Apply").click()

    saved = _saved(p, wallet)
    assert NPUB_A in saved["preferred"], saved
    assert NPUB_B in saved["banned"], saved
    assert NPUB_B not in saved["preferred"], saved


def test_apply_never_leaves_a_provider_both_preferred_and_banned(qapp) -> None:
    """``pref -= ban``. A provider ticked in both columns must come out banned
    only -- otherwise selection would rank a provider the user has forbidden,
    which is the one outcome neither tick can have been asking for."""
    p, wallet, tab = _tab_with_providers()
    tree = _tree(tab)
    row = _row(tree, NPUB_A)
    row.setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    row.setCheckState(p._BAN_COL, Qt.CheckState.Checked)

    _button(tab, "Apply").click()

    saved = _saved(p, wallet)
    assert NPUB_A in saved["banned"]
    assert NPUB_A not in saved["preferred"], \
        "a banned provider was also saved as preferred"


def test_apply_unticking_removes_a_provider_from_the_list(qapp) -> None:
    """Apply is a full rewrite of both lists from the visible state, not an
    append -- so unticking has to actually remove."""
    p, wallet, tab = _tab_with_providers()
    tree = _tree(tab)
    _row(tree, NPUB_A).setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    _button(tab, "Apply").click()
    assert NPUB_A in _saved(p, wallet)["preferred"]

    _row(_tree(tab), NPUB_A).setCheckState(p._PREF_COL, Qt.CheckState.Unchecked)
    _button(tab, "Apply").click()
    assert NPUB_A not in _saved(p, wallet)["preferred"], \
        "unticking Preferred did not remove the provider"


def test_apply_keeps_an_offline_provider_typed_into_the_text_box(qapp) -> None:
    """The lists are seeded from the free-text fields before the checkboxes are
    folded in, so an npub that is not currently advertising -- and therefore has
    no row to tick -- is not silently dropped by pressing Apply.

    This is the reason the handler reads the text fields at all, and it is the
    kind of thing that would be noticed only by whoever lost their entry.
    """
    offline = "npub1" + "d" * 58
    wallet = _FakeWallet()
    p = _make_plugin()
    p.settings(wallet).INBOUND_LIQUIDITY_PREFERRED_NPUBS = offline
    # Build the tab only AFTER the setting is stored, so its text box renders
    # from it -- that text box is the only place the offline npub still exists.
    p._last_offers[wallet] = [_offer(NPUB_A)]
    p._add_liquidity_tab(_FakeWindow(), wallet)
    tab = _sub_tab(p, wallet, PROVIDERS_TAB)

    _row(_tree(tab), NPUB_A).setCheckState(p._PREF_COL, Qt.CheckState.Checked)
    _button(tab, "Apply").click()

    saved = _saved(p, wallet)
    assert offline in saved["preferred"], \
        f"an offline preferred provider was lost on Apply: {saved}"
    assert NPUB_A in saved["preferred"], saved


# --- Reset stats ----------------------------------------------------------
_HISTORY = {
    NPUB_A: {"fault_count": 3, "consecutive_faults": 2, "last_fault_ts": 1.0},
    NPUB_B: {"success_count": 5, "last_success_ts": 2.0},
}


def test_reset_stats_with_no_selection_clears_everything(qapp) -> None:
    """No row selected means all providers, which is destructive and so is
    worth pinning explicitly."""
    p, wallet, tab = _tab_with_providers(reliability=_HISTORY)
    assert p.provider_reliability_rows(wallet), "no history to clear"

    _tree(tab).clearSelection()
    _button(tab, "Reset stats").click()

    assert not p.provider_reliability_rows(wallet), \
        "Reset stats left reliability history behind"


def test_reset_stats_with_a_selection_clears_only_that_provider(qapp) -> None:
    """The other providers' tallies are what the auto-penalty and auto-ban are
    computed from; clearing a neighbour's would quietly forgive it."""
    p, wallet, tab = _tab_with_providers(reliability=_HISTORY)
    tree = _tree(tab)
    tree.setCurrentItem(_row(tree, NPUB_A))

    _button(tab, "Reset stats").click()

    rows = p.provider_reliability_rows(wallet)
    assert NPUB_A not in rows, "the selected provider's history was not cleared"
    assert NPUB_B in rows, "a provider that was not selected lost its history"


# --- Refresh --------------------------------------------------------------
def test_refresh_schedules_discovery_on_the_wallets_loop(qapp) -> None:
    """The button's whole job. Asserted by capturing what it hands the loop
    rather than by running discovery, which would need a network."""
    p, wallet, tab = _tab_with_providers()
    scheduled: List[object] = []
    wallet.network = SimpleNamespace(
        asyncio_loop=SimpleNamespace(name="loop"), is_connected=lambda: True)

    def _fake_schedule(coro, loop):
        coro.close()                      # we never run it; don't warn
        scheduled.append(loop)
        return SimpleNamespace(add_done_callback=lambda cb: None)

    import electrum.plugins.inbound_liquidity.qt as qt_mod  # type: ignore
    orig = qt_mod.asyncio.run_coroutine_threadsafe
    qt_mod.asyncio.run_coroutine_threadsafe = _fake_schedule
    try:
        _button(tab, "Refresh").click()
    finally:
        qt_mod.asyncio.run_coroutine_threadsafe = orig

    assert scheduled == [wallet.network.asyncio_loop], \
        "Refresh did not schedule discovery on the wallet's own loop"


def test_refresh_on_an_offline_wallet_says_so(qapp) -> None:
    """There is no loop to schedule on. The button must report that, not
    silently do nothing -- a dead button is indistinguishable from a broken
    plugin."""
    p, wallet, tab = _tab_with_providers()
    wallet.network = None

    _button(tab, "Refresh").click()

    # The handler reports through the tab's status line, which is a QLabel.
    shown = " ".join(lbl.text() for lbl in tab.findChildren(QLabel))
    assert "offline" in shown.lower(), \
        f"Refresh on an offline wallet said nothing about it: {shown!r}"
