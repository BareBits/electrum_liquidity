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

Skipped outside the Electrum venv.
"""
from __future__ import annotations

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
