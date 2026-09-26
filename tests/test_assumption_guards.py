"""Regression guards for load-bearing assumptions the mocked suite otherwise
takes for granted -- the ones that, if they silently drift, would break real
users without failing a test:

  1. msat -> sat units: build_snapshot divides channel balances/spendable by
     1000 (they are msat) but NOT capacity (already sat). A dropped or extra
     division is a 1000x error that wrecks every trigger and cost gate.
  2. funding_sat='!' output extraction fails SAFE: if the trial tx's outputs
     ever stop carrying an int-valued channel output, _max_funding_minus_reserve
     must yield a value below the funding floor (-> decline), never a bogus
     amount that opens the wrong-sized channel.
  3. The provider offer terms the plugin feeds to sm.update_pairs (SwapFees)
     still carry max_reverse -- our own offer validation only checks max_forward,
     so we rely on Electrum always supplying max_reverse.
  4. The liquidity-goal buffer's four footholds in Electrum's own GUI: the
     `create_send_tab` hook, the free grid row the warning lands on, the colours
     the pie-chart rewrite matches on, and the widgets the Balance dialog patch
     looks for. All four are places where a stock-Electrum change would leave the
     plugin silently doing nothing rather than failing -- the buffer would simply
     stop appearing, and every mocked test would still pass.

Skipped outside the Electrum venv.
"""
from __future__ import annotations

import inspect
import re

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    MIN_FUNDING_SAT,
)
# Reuse the vetted build_snapshot fakes (they encode msat correctly).
from test_build_snapshot_glue import (  # type: ignore  # noqa: E402
    LOCAL, _FakeChan, _plugin as _snap_plugin, _swap_manager, _wallet,
)
from test_min_funding_floor import (  # type: ignore  # noqa: E402
    _Out, _TrialTx, _Wallet, _reserve_plugin,
)


# --- 1. msat -> sat units --------------------------------------------------
def test_build_snapshot_divides_msat_balances_not_capacity() -> None:
    # local balance in msat with a sub-1000 remainder: a MISSING /1000 would
    # surface the raw msat (1000x too big); the guard pins the floored sat value.
    cap_sat = 2_000_000
    local_msat = 1_234_567          # -> 1234 sat (floored), not 1_234_567
    spendable_sat = 10_000
    chan = _FakeChan(cid=b"\x01" * 32, short="1x1x1", capacity=cap_sat,
                     local_msat=local_msat, spendable=spendable_sat)
    p = _snap_plugin()
    snap = p.build_snapshot(_wallet([chan], _swap_manager()), transport=None)

    c = snap.channels[0]
    assert c.local_sat == local_msat // 1000 == 1234        # divided (msat -> sat)
    assert c.local_sat != local_msat                         # NOT the raw msat
    # remote balance = capacity_msat - local_msat, then /1000.
    assert c.remote_sat == (cap_sat * 1000 - local_msat) // 1000
    # available_to_spend returns spendable*1000 (msat); /1000 recovers the sat.
    assert c.spendable_local_sat == spendable_sat
    # capacity comes from get_capacity() which is ALREADY sat -> must NOT be /1000.
    assert c.capacity_sat == cap_sat


# --- 2. funding_sat='!' extraction fails safe -----------------------------
class _Lnworker:
    def __init__(self, outs) -> None:
        self._outs = outs
        self.calls = []

    def mktx_for_open_channel(self, *, coins, funding_sat, node_id, fee_policy):
        self.calls.append(funding_sat)
        return _TrialTx(self._outs)


def test_funding_no_int_output_fails_safe_below_floor() -> None:
    # If the '!' sentinel is ever left unresolved on the channel output (no
    # positive int output remains, only a 0-valued OP_RETURN), the extraction
    # must yield a value BELOW the funding floor so the caller declines -- never
    # a spurious large amount that would open a mis-sized channel.
    lnw = _Lnworker([_Out("!"), _Out(0)])       # no positive int output
    p = _reserve_plugin(reserve=10_000)
    got = p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33)
    assert got is not None
    assert got < MIN_FUNDING_SAT                # safe: caller will decline the open
    assert got <= 0                             # concretely, 0 (max int) - reserve


def test_funding_extraction_ignores_non_int_and_picks_channel_output() -> None:
    # The channel output (largest int) is chosen; a non-int sentinel and the
    # 0-valued OP_RETURN are ignored. Pins the load-bearing assumption that the
    # max int output IS the channel output (true for a max-spend '!' tx, which
    # has no larger change output).
    lnw = _Lnworker([_Out("!"), _Out(500_000), _Out(0)])
    p = _reserve_plugin(reserve=10_000)
    got = p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33)
    assert got == 490_000                        # 500_000 chosen, minus reserve


# --- 3. provider SwapFees still carry max_reverse -------------------------
def test_swapfees_still_carries_max_reverse() -> None:
    # The plugin feeds a live offer's `pairs` (a SwapFees) straight into
    # sm.update_pairs, which reads max_reverse -- but our _offers_from_transport
    # only validates max_forward. So we depend on Electrum always supplying
    # max_reverse; if this field is renamed/removed, update_pairs would blow up
    # in production and no mocked test would catch it. This guard makes that
    # dependency explicit.
    import attr
    from electrum.submarine_swaps import SwapFees  # type: ignore

    fields = {a.name for a in attr.fields(SwapFees)}
    assert {"percentage", "mining_fee", "min_amount",
            "max_forward", "max_reverse"} <= fields


# --- 4. the liquidity-goal buffer's footholds in Electrum's GUI -----------
def _original(func):
    """Electrum's own implementation of a method the plugin may have wrapped.

    The pie-chart patches are installed on the real classes by the Qt suite, and
    test order is not guaranteed -- so reading a patched method's source would
    inspect the PLUGIN's wrapper and pass no matter what Electrum did. The
    wrappers keep the original reachable for exactly this.
    """
    return getattr(func, "_inbound_liquidity_orig", func)


def test_send_tab_still_offers_the_create_send_tab_hook() -> None:
    # The Send tab's inline buffer warning is installed through this hook rather
    # than a monkeypatch. If Electrum ever drops the call, the warning silently
    # never appears and no mocked test notices -- they all drive the installer
    # directly.
    pytest.importorskip("PyQt6.QtWidgets")
    from electrum.gui.qt.send_tab import SendTab

    src = inspect.getsource(SendTab.__init__)
    assert "run_hook('create_send_tab', grid)" in src


def test_send_grid_still_leaves_the_warning_row_free() -> None:
    # The warning lands on row 4 so it sits directly under the Amount field
    # (row 3) rather than below the buttons (row 6). Two widgets in one
    # QGridLayout cell OVERLAP rather than raise, so a future Electrum claiming
    # row 4 would draw its widget and ours on top of each other. The installer
    # falls back to the bottom of the grid when the row is taken -- this guard
    # is what tells us the fallback has started being used, and that the
    # preferred row should be moved.
    pytest.importorskip("PyQt6.QtWidgets")
    from electrum.gui.qt.send_tab import SendTab
    from electrum.plugins.inbound_liquidity.qt import (  # type: ignore
        SEND_WARNING_GRID_ROW,
    )

    src = inspect.getsource(SendTab.__init__)
    rows = {int(r) for r in re.findall(
        r"grid\.add(?:Widget|Layout)\(\s*[^,]+,\s*(\d+)\s*,", src)}
    assert rows, "could not parse any rows out of SendTab's grid"
    assert SEND_WARNING_GRID_ROW not in rows


def test_status_bar_pie_is_still_built_the_way_the_patch_rewrites() -> None:
    # The patch wraps update_status and rewrites the list it stored on
    # balance_label, keying on the two colour constants. If Electrum stops
    # calling update_list, or stops using these colours for the on-chain and
    # Lightning slices, the rewrite matches nothing and the buffer slice quietly
    # never appears.
    pytest.importorskip("PyQt6.QtWidgets")
    from electrum.gui.qt.main_window import ElectrumWindow

    src = inspect.getsource(_original(ElectrumWindow.update_status))
    assert "balance_label.update_list" in src
    assert "COLOR_CONFIRMED" in src
    assert "COLOR_LIGHTNING" in src


def test_balance_dialog_still_builds_the_widgets_the_patch_looks_for() -> None:
    # The dialog patch finds the pie by type (findChild(PieChartWidget)) and the
    # legend by type (findChild(QGridLayout)). Both have to exist, and the pie
    # has to be the only chart in the dialog for findChild to be unambiguous.
    pytest.importorskip("PyQt6.QtWidgets")
    from electrum.gui.qt.balance_dialog import BalanceDialog

    src = inspect.getsource(_original(BalanceDialog.__init__))
    assert "PieChartWidget(" in src
    assert src.count("PieChartWidget(") == 1
    assert "QGridLayout()" in src
    assert src.count("QGridLayout()") == 1


def test_piechart_balance_still_reports_both_rails() -> None:
    # wallet_total_balance_sat measures the send threshold against
    # PiechartBalance.total(), so that the warning and the chart cannot disagree
    # about the same wallet. If the shape of that total changes, the threshold
    # silently starts measuring something else.
    import dataclasses
    from electrum.wallet import PiechartBalance  # type: ignore

    fields = {f.name for f in dataclasses.fields(PiechartBalance)}
    assert {"confirmed", "unconfirmed", "unmatured", "frozen",
            "lightning", "lightning_frozen"} == fields
    assert PiechartBalance(confirmed=1, unconfirmed=2, unmatured=4, frozen=8,
                           lightning=16, lightning_frozen=32).total() == 63
