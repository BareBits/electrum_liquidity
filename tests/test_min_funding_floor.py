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


def _reserve_plugin(reserve, cap=10 ** 9, max_size=0) -> LiquidityPlugin:
    return _plugin(
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=reserve,
        FEE_POLICY="feerate:1000",
        LIGHTNING_MAX_FUNDING_SAT=cap,
        INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT=max_size,
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


# --- the max-channel-size ceiling ----------------------------------------
# The plugin's own ceiling on a single channel, applied here alongside
# Electrum's LIGHTNING_MAX_FUNDING_SAT. This is where the ceiling reaches the
# funds: the engine's amount is a gross intent, but THIS number is what
# open_channel_with_peer is actually called with.
def test_max_funding_capped_by_the_max_channel_size():
    lnw = _Lnworker(max_fundable=5_000_000)
    p = _reserve_plugin(reserve=10_000, max_size=1_000_000)

    assert p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33) == 1_000_000


def test_a_max_channel_size_above_the_balance_is_inert():
    lnw = _Lnworker(max_fundable=500_000)
    p = _reserve_plugin(reserve=10_000, max_size=1_000_000)

    assert p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33) == 490_000


def test_zero_max_channel_size_does_not_zero_the_funding():
    # 0 means "no ceiling". A three-way min() would read it as "fund nothing",
    # which would silently stop the plugin opening channels at all.
    lnw = _Lnworker(max_fundable=500_000)
    p = _reserve_plugin(reserve=10_000, max_size=0)

    assert p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33) == 490_000


def test_the_lower_of_the_two_ceilings_wins():
    """Ours is a preference; Electrum's is a limit that would reject the open
    outright. Neither may be used to escape the other, so each is checked as the
    binding one in turn."""
    lnw = _Lnworker(max_fundable=5_000_000)
    # Electrum's is lower.
    p = _reserve_plugin(reserve=10_000, cap=600_000, max_size=1_000_000)
    assert p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33) == 600_000
    # Ours is lower.
    p = _reserve_plugin(reserve=10_000, cap=2_000_000, max_size=1_000_000)
    assert p._max_funding_minus_reserve(_Wallet(lnw), b"\x02" * 33) == 1_000_000


def test_max_channel_size_reader_survives_a_hand_edited_config():
    """Read on every tick, so a garbage value must answer rather than abort the
    evaluation -- and it must fall back to the SHIPPED ceiling, not to 0: 0 means
    "no ceiling", and silently returning to unbounded channel sizes is the one
    answer a user who set this setting definitely did not want."""
    from electrum.plugins.inbound_liquidity import DEFAULT_MAX_CHANNEL_SIZE_SAT  # noqa
    assert _plugin(INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT="nonsense") \
        ._max_channel_size_sat() == DEFAULT_MAX_CHANNEL_SIZE_SAT
    assert _plugin(INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT=None) \
        ._max_channel_size_sat() == DEFAULT_MAX_CHANNEL_SIZE_SAT
    # Missing entirely (a config predating the setting) reads as the default too.
    assert _plugin()._max_channel_size_sat() == DEFAULT_MAX_CHANNEL_SIZE_SAT
    # A negative value is clamped to "off" rather than becoming a negative amount.
    assert _plugin(INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT=-1)._max_channel_size_sat() == 0


def test_read_config_passes_the_ceiling_to_the_engine():
    """The engine's dataclass default is 0 (off) so pure tests keep pre-feature
    behaviour; the live path must therefore pass the user's value explicitly, or
    the ceiling would be silently inert in production."""
    p = _plugin(
        INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
        INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=60_000,
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=10_000,
        INBOUND_LIQUIDITY_MAX_CHANNELS=2,
        INBOUND_LIQUIDITY_MAX_CHANNEL_SIZE_SAT=777_000,
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.9,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT=25_000,
        INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT=0,
        INBOUND_LIQUIDITY_MIN_SWAP_SAT=25_000,
        INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY=True,
        INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY=5,
        INBOUND_LIQUIDITY_GOAL_SAT=100_000,
        INBOUND_LIQUIDITY_PREFERRED_NPUBS="",
        INBOUND_LIQUIDITY_BANNED_NPUBS="",
        INBOUND_LIQUIDITY_SINK_ADDRESS="",
        INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS=False,
        INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL=True,
    )
    assert p.read_config().max_channel_size_sat == 777_000


# --- effective_min_funding_sat -------------------------------------------
# Backs the settings-tab validation: a ceiling below the floor in force can never
# be satisfied, and the floor tracks min_onchain_to_open_sat.
def test_effective_floor_follows_the_configured_min_onchain(restore_floor):
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=50_000)
    assert p.effective_min_funding_sat() == 50_000


def test_effective_floor_never_exceeds_the_stock_floor(restore_floor):
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=1_000_000)
    assert p.effective_min_funding_sat() == 200_000


def test_effective_floor_accepts_an_unsaved_override(restore_floor):
    """The settings tab validates the values being applied in this click, so it
    has to be able to ask what the floor WOULD be for a typed-but-unsaved
    min-on-chain."""
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=150_000)
    assert p.effective_min_funding_sat(30_000) == 30_000
    assert p.effective_min_funding_sat(500_000) == 200_000


def test_effective_floor_survives_a_hand_edited_min_onchain(restore_floor):
    _reset_to_stock(200_000)
    p = _plugin(INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT="rubbish")
    assert p.effective_min_funding_sat() == 200_000
