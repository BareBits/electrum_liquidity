"""Glue tests for the channel-funding floor override and the max-minus-reserve
funding calculation.

The plugin lowers Electrum's stock ``MIN_FUNDING_SAT`` to the configured
``min_onchain_to_open_sat`` when it is smaller, re-asserting it so the user's
value always wins. Skipped if the plugin package (and Electrum) can't be
imported (e.g. running outside the electrum venv)."""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

IL = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore  # noqa: E402


def _plugin(**config) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.floor")
    p.config = SimpleNamespace(**config)
    return p


# --- MIN_FUNDING_SAT floor override --------------------------------------
@pytest.fixture
def restore_floor():
    """Snapshot every patched MIN_FUNDING_SAT binding (and the captured stock)
    and restore them after the test, so lowering the floor here can't leak into
    other tests running in the same process."""
    names = list(IL._MIN_FUNDING_CORE_MODULES) + [
        "electrum.plugins.inbound_liquidity.liquidity_manager",
        "electrum.plugins.inbound_liquidity",
    ]
    saved = {n: getattr(sys.modules[n], "MIN_FUNDING_SAT")
             for n in names
             if sys.modules.get(n) is not None and hasattr(sys.modules[n], "MIN_FUNDING_SAT")}
    saved_stock = IL._stock_min_funding_sat
    yield
    for n, v in saved.items():
        setattr(sys.modules[n], "MIN_FUNDING_SAT", v)
    IL._stock_min_funding_sat = saved_stock


def _reset_to_stock(stock: int = 200_000) -> None:
    import electrum.lnutil as lnutil
    IL._stock_min_funding_sat = None
    lnutil.MIN_FUNDING_SAT = stock


def test_floor_lowered_when_min_onchain_below_stock(restore_floor):
    import electrum.lnutil as lnutil
    from electrum.plugins.inbound_liquidity import liquidity_manager as engine
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=50_000)

    floor = p._enforce_min_funding_floor()

    assert floor == 50_000
    assert lnutil.MIN_FUNDING_SAT == 50_000      # Electrum's core validate floor
    assert engine.MIN_FUNDING_SAT == 50_000      # engine mirror used by decide()
    assert IL.MIN_FUNDING_SAT == 50_000          # glue import used in _open_channel


def test_floor_not_raised_above_stock(restore_floor):
    import electrum.lnutil as lnutil
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=1_000_000)

    floor = p._enforce_min_funding_floor()

    # min_onchain above the stock floor => never raise it; stock stands.
    assert floor == 200_000
    assert lnutil.MIN_FUNDING_SAT == 200_000


def test_floor_reasserts_after_external_reset(restore_floor):
    """The user's value wins even if some code path resets the constant."""
    import electrum.lnutil as lnutil
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=50_000)
    p._enforce_min_funding_floor()
    assert lnutil.MIN_FUNDING_SAT == 50_000

    # Simulate Electrum restoring the stock constant, then a periodic re-assert.
    lnutil.MIN_FUNDING_SAT = 200_000
    p._enforce_min_funding_floor()
    assert lnutil.MIN_FUNDING_SAT == 50_000


def test_floor_handles_bad_config_value(restore_floor):
    import electrum.lnutil as lnutil
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT="not-an-int")

    # A non-integer config must not raise; it falls back to the stock floor.
    floor = p._enforce_min_funding_floor()
    assert floor == 200_000
    assert lnutil.MIN_FUNDING_SAT == 200_000


# --- _max_funding_minus_reserve ------------------------------------------
class _Out:
    def __init__(self, value) -> None:
        self.value = value


class _TrialTx:
    def __init__(self, outs) -> None:
        self._outs = outs

    def outputs(self):
        return self._outs


class _Lnworker:
    """Fake lnworker whose max-spend ('!') trial tx nets the mining fee into the
    channel output, mirroring Electrum's real mktx_for_open_channel."""
    def __init__(self, max_fundable=None, raises=None) -> None:
        self._max_fundable = max_fundable
        self._raises = raises
        self.calls = []

    def mktx_for_open_channel(self, *, coins, funding_sat, node_id, fee_policy):
        self.calls.append(funding_sat)
        if self._raises is not None:
            raise self._raises
        # A 0-valued extra output (e.g. the recovery OP_RETURN) is also present.
        return _TrialTx([_Out(self._max_fundable), _Out(0)])


class _Wallet:
    def __init__(self, lnworker) -> None:
        self.lnworker = lnworker

    def get_spendable_coins(self, domain):
        return []


def _reserve_plugin(reserve, cap=10 ** 9) -> LiquidityPlugin:
    return _plugin(
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=reserve,
        FEE_POLICY="feerate:1000",
        LIGHTNING_MAX_FUNDING_SAT=cap,
    )


def test_max_funding_deducts_reserve_from_max_spend():
    lnw = _Lnworker(max_fundable=500_000)
    p = _reserve_plugin(reserve=10_000)

    got = p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33)

    # The mining fee is already netted by the '!' max-spend trial; we then just
    # subtract the configured on-chain reserve.
    assert lnw.calls == ["!"]
    assert got == 490_000


def test_max_funding_capped_by_lightning_max():
    lnw = _Lnworker(max_fundable=500_000)
    p = _reserve_plugin(reserve=10_000, cap=100_000)

    got = p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33)
    assert got == 100_000


def test_max_funding_returns_none_on_not_enough_funds():
    from electrum.util import NotEnoughFunds
    lnw = _Lnworker(raises=NotEnoughFunds())
    p = _reserve_plugin(reserve=10_000)

    assert p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33) is None
