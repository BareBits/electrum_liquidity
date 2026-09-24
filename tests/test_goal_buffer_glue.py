"""Glue-level tests for the liquidity-goal buffer.

The arithmetic is covered in ``test_goal_buffer_engine``; what is tested here is
the Electrum-facing half that the Qt layer actually calls:

  * ``goal_buffer_sat`` is 0 for a wallet the plugin is not MANAGING -- the
    safety property of the whole feature, since reserving "liquidity goal" sats
    on a wallet that can never hold a channel would be reserving them for
    nothing;
  * ``wallet_total_balance_sat`` reads Electrum's own pie-chart total, so the
    warning and the chart cannot disagree about the same wallet;
  * ``send_warning_text`` turns an amount into the exact string the Send tab
    shows (empty == no warning), so the Qt layer holds no policy; and
  * every one of these is TOTAL -- they run on each keystroke and each balance
    repaint, so a broken config or an exploding wallet must yield 0/"" rather
    than take out Electrum's status bar.

Heavy Electrum objects are faked. Skipped outside the electrum venv."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Dict, Optional

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    DEFAULT_LIQUIDITY_GOAL_SAT,
    LiquidityPlugin,
    format_goal_buffer_warning,
)


class _FakeConfig:
    def __init__(self, **overrides) -> None:
        base = dict(
            INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
            INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=60_000,
            INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=10_000,
            INBOUND_LIQUIDITY_MAX_CHANNELS=2,
            INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.9,
            INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
            INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT=25_000,
            INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT=0,
            INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY=True,
            INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY=5,
            INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
            INBOUND_LIQUIDITY_GOAL_SAT=100_000,
            INBOUND_LIQUIDITY_PREFERRED_NPUBS="",
            INBOUND_LIQUIDITY_BANNED_NPUBS="",
            INBOUND_LIQUIDITY_SINK_ADDRESS="",
            INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS=False,
            INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL=True,
        )
        base.update(overrides)
        for key, value in base.items():
            setattr(self, key, value)


class _FakePiechartBalance:
    """Mirrors electrum.wallet.PiechartBalance closely enough for total()."""

    def __init__(self, *, confirmed=0, unconfirmed=0, unmatured=0, frozen=0,
                 lightning=0, lightning_frozen=0) -> None:
        self.confirmed = confirmed
        self.unconfirmed = unconfirmed
        self.unmatured = unmatured
        self.frozen = frozen
        self.lightning = lightning
        self.lightning_frozen = lightning_frozen

    def total(self):
        return (self.confirmed + self.unconfirmed + self.unmatured
                + self.frozen + self.lightning + self.lightning_frozen)


class _FakeWallet:
    def __init__(self, balance: Optional[_FakePiechartBalance] = None) -> None:
        self._balance = balance or _FakePiechartBalance()
        self.balance_reads = 0

    def get_balances_for_piechart(self):
        self.balance_reads += 1
        return self._balance

    def basename(self) -> str:
        return "test-wallet"


def _plugin(*, managed: Optional[_FakeWallet] = None, **cfg) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.goal_buffer")
    p.config = _FakeConfig(**cfg)
    # ``self.wallets`` IS the managed set (start_wallet populates it, stop_wallet
    # pops it), so membership here is exactly what the plugin checks in production.
    p.wallets: Dict[object, object] = {}
    if managed is not None:
        p.wallets[managed] = object()
    return p


# --- how big is the buffer, per wallet ------------------------------------
def test_managed_wallet_gets_the_configured_goal_as_its_buffer() -> None:
    w = _FakeWallet()
    p = _plugin(managed=w)
    assert p.goal_buffer_sat(w) == 100_000


def test_unmanaged_wallet_gets_no_buffer() -> None:
    """start_wallet declines a wallet without Lightning, so it never lands in
    ``self.wallets``. Such a wallet can never hold a channel, so a liquidity-goal
    reserve on it would be reserving sats for nothing -- no slice, no warning."""
    w = _FakeWallet()
    p = _plugin()                    # w deliberately NOT managed
    assert p.goal_buffer_sat(w) == 0


def test_stopping_the_plugin_on_a_wallet_retires_its_buffer() -> None:
    """No separate bookkeeping: dropping out of ``self.wallets`` is all it takes,
    which is what stop_wallet already does."""
    w = _FakeWallet()
    p = _plugin(managed=w)
    assert p.goal_buffer_sat(w) == 100_000
    p.wallets.pop(w)
    assert p.goal_buffer_sat(w) == 0


def test_goal_of_zero_switches_the_buffer_off() -> None:
    w = _FakeWallet()
    p = _plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=0)
    assert p.goal_buffer_sat(w) == 0


def test_a_garbage_goal_falls_back_to_the_shipped_default() -> None:
    """``_liquidity_goal_sat`` already hardens this for the tick path; the buffer
    inherits it rather than re-reading the raw ConfigVar."""
    w = _FakeWallet()
    p = _plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT="not-a-number")
    assert p.goal_buffer_sat(w) == DEFAULT_LIQUIDITY_GOAL_SAT


def test_a_broken_config_yields_zero_rather_than_raising() -> None:
    """This runs inside a status-bar repaint. An exception here would take the
    main window down, so the buffer answers 0 and the chart stays stock."""
    w = _FakeWallet()
    p = _plugin(managed=w)

    class _Boom:
        def __contains__(self, item):
            raise RuntimeError("wallets dict is on fire")

    p.wallets = _Boom()
    assert p.goal_buffer_sat(w) == 0


# --- the split handed to the pie chart ------------------------------------
def test_split_takes_from_onchain_then_lightning() -> None:
    w = _FakeWallet()
    p = _plugin(managed=w)
    split = p.goal_buffer_split(w, onchain_sat=30_000, lightning_sat=500_000)
    assert (split.from_onchain, split.from_lightning) == (30_000, 70_000)
    assert split.total() == 100_000


def test_unmanaged_wallet_splits_to_nothing() -> None:
    """An all-zero split is what leaves Electrum's chart exactly as it drew it."""
    w = _FakeWallet()
    p = _plugin()
    split = p.goal_buffer_split(w, onchain_sat=500_000, lightning_sat=500_000)
    assert split.total() == 0
    assert split.onchain_remaining == 500_000
    assert split.lightning_remaining == 500_000


# --- the balance the threshold is measured against ------------------------
def test_total_balance_is_electrums_own_piechart_total() -> None:
    """Frozen and unmatured coins included, on purpose: this has to be the same
    number the user reads off the status bar next to the pie."""
    w = _FakeWallet(_FakePiechartBalance(
        confirmed=400_000, unconfirmed=50_000, unmatured=25_000,
        frozen=10_000, lightning=300_000, lightning_frozen=15_000))
    p = _plugin(managed=w)
    assert p.wallet_total_balance_sat(w) == 800_000


def test_total_balance_of_an_exploding_wallet_reads_as_zero() -> None:
    class _BoomWallet:
        def get_balances_for_piechart(self):
            raise RuntimeError("no network")

    p = _plugin()
    assert p.wallet_total_balance_sat(_BoomWallet()) == 0


# --- the string the Send tab shows ----------------------------------------
def test_warning_text_matches_the_specified_wording() -> None:
    """Both blanks are the GOAL: the amount that is impossible to reach and the
    amount to keep back are the same number."""
    assert format_goal_buffer_warning(100_000) == (
        "Spending this much will make it impossible to hit your liquidity goal "
        "of 100,000 sats, keep 100,000 sats as a buffer")


def test_warning_text_groups_the_digits() -> None:
    """A bare 1000000 in a warning about money is digits the eye has to count."""
    assert "1,000,000 sats" in format_goal_buffer_warning(1_000_000)


def test_no_warning_below_the_threshold() -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _plugin(managed=w)                    # 1,000,000 total, 100,000 buffer
    assert p.send_warning_text(w, 900_000) == ""


def test_warning_once_the_send_dips_into_the_buffer() -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=900_000, lightning=100_000))
    p = _plugin(managed=w)
    assert p.send_warning_text(w, 900_001) == format_goal_buffer_warning(100_000)


def test_lightning_balance_counts_toward_the_threshold() -> None:
    """The threshold is total balance, both rails. The same 600k send is fine on
    a wallet holding 400k in channels and over the line without them."""
    p_with_ln = _plugin(managed=(w1 := _FakeWallet(
        _FakePiechartBalance(confirmed=300_000, lightning=400_000))))
    assert p_with_ln.send_warning_text(w1, 600_000) == ""
    p_no_ln = _plugin(managed=(w2 := _FakeWallet(
        _FakePiechartBalance(confirmed=300_000))))
    assert p_no_ln.send_warning_text(w2, 600_000) != ""


def test_no_warning_on_an_empty_amount_field() -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=1_000))
    p = _plugin(managed=w)
    assert p.send_warning_text(w, None) == ""


def test_no_warning_on_an_unmanaged_wallet_however_large_the_send() -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=1_000_000))
    p = _plugin()
    assert p.send_warning_text(w, 1_000_000) == ""


def test_no_warning_when_the_goal_is_switched_off() -> None:
    w = _FakeWallet(_FakePiechartBalance(confirmed=1_000_000))
    p = _plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=0)
    assert p.send_warning_text(w, 1_000_000) == ""


def test_no_buffer_means_the_balance_is_never_read() -> None:
    """``send_warning_text`` runs on every keystroke in the Send tab. With the
    feature off -- goal 0, or a wallet the plugin does not manage -- there is no
    warning to give, so it must short-circuit BEFORE touching the balance rather
    than compute a threshold it will not use."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=1_000_000))
    off = _plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=0)
    unmanaged = _plugin()
    for _ in range(5):
        assert off.send_warning_text(w, 999_999) == ""
        assert unmanaged.send_warning_text(w, 999_999) == ""
    assert w.balance_reads == 0


def test_an_active_buffer_does_read_the_balance() -> None:
    """The other half of the short-circuit: when there IS a buffer, the
    threshold genuinely depends on the balance and it has to be read."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=1_000_000))
    p = _plugin(managed=w, INBOUND_LIQUIDITY_GOAL_SAT=100_000)
    p.send_warning_text(w, 1)
    assert w.balance_reads == 1


def test_an_underfunded_wallet_warns_on_any_send() -> None:
    """Balance below the goal: there is no headroom, so every amount dips in."""
    w = _FakeWallet(_FakePiechartBalance(confirmed=50_000))
    p = _plugin(managed=w)
    assert p.send_warning_text(w, 1) == format_goal_buffer_warning(100_000)
