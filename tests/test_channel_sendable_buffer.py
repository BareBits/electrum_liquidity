"""The swap amount must never exceed what Electrum will actually send.

``chan.available_to_spend(LOCAL)`` is a raw ceiling: balance minus the channel
reserve and the commitment/fee-spike buffer. It is NOT Electrum's answer to "how
much can I send", because a Lightning payment also needs a routing-fee budget on
top of the amount, taken from the same balance. Electrum's own max-send
(``lnworker.num_sats_can_send``) subtracts
``estimate_fee_reserve_for_total_amount()`` from that ceiling, and the estimator's
docstring names this exact case ("reliably allow doing a 'Max' amount lightning
send (e.g. for submarine swaps)").

The plugin used the raw ceiling, so with the default ``min_outbound_sat=0`` it
planned swaps for strictly more than Electrum believed it could send. That does
not fail loudly: the provider splits our ``lightning_amount_sat`` into a main hold
invoice plus a mining-fee prepayment invoice and only creates the on-chain funding
output once BOTH HTLCs arrive, so a leg that cannot be routed leaves the swap
accepted-but-never-funded until the stuck-swap timeout blames the provider.

These tests pin: the per-channel sendable subtracts the estimator's reserve; the
snapshot carries the reduced figure; a missing/raising estimator degrades to the
raw ceiling rather than blocking swaps; and the planned swap amount stays within
Electrum's own limit.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.lnutil import LOCAL, REMOTE  # type: ignore  # noqa: E402
from electrum.lnchannel import ChannelState  # type: ignore  # noqa: E402

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    SWAP_SIZING_HEADROOM_MIN_SAT,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    LiquidityConfig,
    ReverseSwapAction,
    evaluate,
)

# Electrum's defaults: 1% of the amount, floored at 10 sat
# (LIGHTNING_PAYMENT_FEE_MAX_MILLIONTHS / LIGHTNING_PAYMENT_FEE_CUTOFF_MSAT).
ONE_PERCENT_MILLIONTHS = 10_000
CUTOFF_MSAT = 10_000


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


# Regtest-scale commitment feerate (sat/kw); high enough that a second in-flight
# HTLC costs real money, which is the condition the sizing headroom exists for.
FEERATE_SAT_PER_KW = 37_500


class _Chan:
    def __init__(self, *, cid: bytes, short: str, capacity: int, local_sat: int,
                 spendable_sat: int, feerate: int = FEERATE_SAT_PER_KW) -> None:
        self.channel_id = cid
        self.short_channel_id = short
        self._capacity = capacity
        self._local_sat = local_sat
        self._spendable_sat = spendable_sat
        self._feerate = feerate
        self.hm = SimpleNamespace(htlcs=lambda subject: [])

    # Sizing prices the swap's second concurrent HTLC off the live commitment
    # feerate, so the fake has to answer these or it silently models a
    # zero-fee chain and hides the very cost under test.
    def get_feerate(self, subject, *, ctn: int) -> int:
        return self._feerate

    def get_next_ctn(self, subject) -> int:
        return 1

    def get_capacity(self) -> int:
        return self._capacity

    def get_state(self):
        return ChannelState.OPEN

    def has_unsettled_htlcs(self) -> bool:
        return False

    def balance(self, subject) -> int:
        return (self._local_sat * 1000 if subject == LOCAL
                else (self._capacity - self._local_sat) * 1000)

    def available_to_spend(self, subject) -> int:
        return self._spendable_sat * 1000

    def is_active(self) -> bool:
        return True


def _real_reserve(amount_sat: int) -> int:
    """Electrum's estimator, replicated for the expected value in assertions.

    Mirrors ``PaymentFeeBudget.reverse_from_total_amount`` at Electrum's default
    config: the fee share of a total that already includes it, floored at cutoff.
    """
    total_msat = amount_sat * 1000
    amount_minus_fees = (total_msat * 1_000_000) // (1_000_000 + ONE_PERCENT_MILLIONTHS)
    fees_msat = max(total_msat - amount_minus_fees, CUTOFF_MSAT)
    fees_msat = min(fees_msat, total_msat)
    return -(-fees_msat // 1000)     # ceil to sat


class _Lnworker:
    """lnworker exposing Electrum's real fee-reserve estimator semantics."""

    def __init__(self, channels, sm, *, raises=False) -> None:
        self.channels = {c.channel_id: c for c in channels}
        self.swap_manager = sm
        self._raises = raises

    def estimate_fee_reserve_for_total_amount(self, amount_sat) -> int:
        if self._raises:
            raise RuntimeError("no fee estimates")
        return _real_reserve(int(amount_sat))


class _Lnworker_NoEstimator:
    def __init__(self, channels, sm) -> None:
        self.channels = {c.channel_id: c for c in channels}
        self.swap_manager = sm


class _Wallet:
    def __init__(self, lnworker) -> None:
        self.db = _FakeDB()
        self.lnworker = lnworker

    def get_spendable_balance_sat(self) -> int:
        return 0

    def basename(self) -> str:
        return "liqtest"


def _swap_manager(*, max_forward=10_000_000, min_amount=20_000, pct=0.1, mining=0):
    return SimpleNamespace(
        get_pending_swaps=lambda: [],
        percentage=pct,
        mining_fee=mining,
        get_fee_for_txbatcher=lambda: 0,
        get_provider_max_forward_amount=lambda: max_forward,
        get_min_amount=lambda: min_amount,
        get_swap=lambda ph: None,
    )


def _plugin() -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.sendable")
    p._last_offers = {}
    p.config = SimpleNamespace()
    return p


def _config(**overrides) -> LiquidityConfig:
    base = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=1_000_000,
        onchain_reserve_sat=10_000,
        max_channels=5,
        max_swap_fee_pct=5.0,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
    )
    base.update(overrides)
    return LiquidityConfig(**base)


# --- the unit: per-channel sendable ---------------------------------------
def test_sendable_subtracts_electrums_fee_reserve_and_our_headroom() -> None:
    chan = _Chan(cid=b"\x01" * 32, short="1x1x1", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    sendable = LiquidityPlugin._channel_sendable_sat(ln, chan)
    reserve = _real_reserve(1_000_000)
    headroom = LiquidityPlugin._swap_sizing_headroom_sat(
        1_000_000, LiquidityPlugin._second_htlc_overhead_sat(chan))
    assert reserve > 0                                   # the estimator does bite
    assert headroom > 0                                  # ...and so does our slack
    assert sendable == 1_000_000 - reserve - headroom
    assert sendable < 1_000_000                          # strictly below the raw ceiling


def test_sendable_is_electrums_max_send_formula_minus_swap_headroom() -> None:
    """Electrum's own num_sats_can_send arithmetic (raw - reserve(raw)), and then
    our extra slack on top -- because a reverse swap is two concurrent payments
    down one channel and Electrum's reserve covers one. See
    ``_channel_sendable_sat``."""
    chan = _Chan(cid=b"\x02" * 32, short="1x1x2", capacity=4_000_000,
                 local_sat=2_000_000, spendable_sat=1_987_654)
    ln = _Lnworker([chan], _swap_manager())
    raw = chan.available_to_spend(LOCAL) // 1000
    electrum_max_send = raw - _real_reserve(raw)
    sendable = LiquidityPlugin._channel_sendable_sat(ln, chan)
    assert sendable == electrum_max_send - LiquidityPlugin._swap_sizing_headroom_sat(
        raw, LiquidityPlugin._second_htlc_overhead_sat(chan))
    assert sendable < electrum_max_send, "sizing at Electrum's ceiling is the bug"


def test_sendable_degrades_to_raw_when_estimator_absent() -> None:
    """An Electrum without the estimator must not stop swaps outright."""
    chan = _Chan(cid=b"\x03" * 32, short="1x1x3", capacity=2_000_000,
                 local_sat=900_000, spendable_sat=800_000)
    ln = _Lnworker_NoEstimator([chan], _swap_manager())
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == 800_000


def test_sendable_degrades_to_raw_when_estimator_raises() -> None:
    chan = _Chan(cid=b"\x04" * 32, short="1x1x4", capacity=2_000_000,
                 local_sat=900_000, spendable_sat=800_000)
    ln = _Lnworker([chan], _swap_manager(), raises=True)
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == 800_000


def test_sendable_never_negative() -> None:
    """A tiny balance whose reserve exceeds it clamps at 0, not below."""
    chan = _Chan(cid=b"\x05" * 32, short="1x1x5", capacity=2_000_000,
                 local_sat=5, spendable_sat=5)
    ln = _Lnworker([chan], _swap_manager())
    assert LiquidityPlugin._channel_sendable_sat(ln, chan) == 0


# --- through the real snapshot --------------------------------------------
def test_snapshot_carries_the_reduced_sendable() -> None:
    chan = _Chan(cid=b"\x06" * 32, short="1x1x6", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    p, w = _plugin(), _Wallet(ln)
    snap = p.build_snapshot(w, transport=None)
    (cs,) = snap.channels
    assert cs.local_sat == 1_000_000                     # balance untouched
    assert cs.spendable_local_sat == (
        1_000_000 - _real_reserve(1_000_000)
        - LiquidityPlugin._swap_sizing_headroom_sat(
            1_000_000, LiquidityPlugin._second_htlc_overhead_sat(chan)))


# --- the regression: planned amount stays within Electrum's limit ---------
def test_planned_swap_amount_fits_what_electrum_can_send() -> None:
    """With min_outbound_sat=0 (the default) the swap used to be planned at the
    raw ceiling -- more than Electrum would send, so one of the swap's two
    Lightning legs could not be routed and the swap hung unfunded."""
    chan = _Chan(cid=b"\x07" * 32, short="1x1x7", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    p, w = _plugin(), _Wallet(ln)

    npub = "npub1" + "a" * 58
    offer = SimpleNamespace(
        server_npub=npub, pow_bits=0,
        pairs=SimpleNamespace(percentage=0.1, mining_fee=0,
                              min_amount=20_000, max_forward=10_000_000))
    transport = SimpleNamespace(get_recent_offers=lambda: [offer])

    snap = p.build_snapshot(w, transport=transport)
    result = evaluate(snap, _config(min_outbound_sat=0))
    (swap,) = [a for a in result.actions if isinstance(a, ReverseSwapAction)]

    electrum_max_send = 1_000_000 - _real_reserve(1_000_000)
    assert swap.lightning_amount_sat <= electrum_max_send
    # And it is still a worthwhile swap, not shaved to nothing.
    assert swap.lightning_amount_sat > 0.9 * electrum_max_send


def test_outbound_floor_still_applies_on_top_of_the_reserve() -> None:
    """The reserve and the operator's min_outbound_sat floor compose; the floor
    is measured against the honest sendable, not the raw ceiling."""
    chan = _Chan(cid=b"\x08" * 32, short="1x1x8", capacity=2_000_000,
                 local_sat=1_000_000, spendable_sat=1_000_000)
    ln = _Lnworker([chan], _swap_manager())
    p, w = _plugin(), _Wallet(ln)
    npub = "npub1" + "b" * 58
    offer = SimpleNamespace(
        server_npub=npub, pow_bits=0,
        pairs=SimpleNamespace(percentage=0.1, mining_fee=0,
                              min_amount=20_000, max_forward=10_000_000))
    snap = p.build_snapshot(w, transport=SimpleNamespace(get_recent_offers=lambda: [offer]))
    floor = 100_000
    result = evaluate(snap, _config(min_outbound_sat=floor))
    (swap,) = [a for a in result.actions if isinstance(a, ReverseSwapAction)]
    sendable = (1_000_000 - _real_reserve(1_000_000)
                - LiquidityPlugin._swap_sizing_headroom_sat(
                    1_000_000, LiquidityPlugin._second_htlc_overhead_sat(chan)))
    assert swap.lightning_amount_sat == sendable - floor


# --- the swap-sizing headroom ---------------------------------------------
# Regression cover for a bug the rig found and the unit tests could not: a
# max-sized reverse swap failed roughly three runs in four. The plugin sized the
# swap at Electrum's own max-send figure, which reserves fees for ONE payment --
# but a reverse swap is TWO payments (main hold invoice + mining-fee prepayment)
# issued concurrently down the same channel by Electrum's reverse_swap, which
# does not even pin the prepayment to our chosen channel. On a real 804,515 sat
# channel that left a margin of ONE satoshi, so whichever HTLC committed first
# decided whether the other could route. The loser hangs unfunded and is later
# reconciled as a stuck swap -- faulting a provider that did nothing wrong.
def test_headroom_is_proportional_with_a_floor() -> None:
    small = LiquidityPlugin._swap_sizing_headroom_sat(1_000)
    assert small == SWAP_SIZING_HEADROOM_MIN_SAT, \
        "a small channel must still get real slack, not a rounding error"
    big = LiquidityPlugin._swap_sizing_headroom_sat(10_000_000)
    assert big == 100_000                              # 1% of the channel
    assert big > small, "headroom must scale with the channel"


def test_headroom_is_never_negative_or_absent() -> None:
    for raw in (0, 1, 999, 100_000, 50_000_000):
        assert LiquidityPlugin._swap_sizing_headroom_sat(raw) >= SWAP_SIZING_HEADROOM_MIN_SAT


def test_a_max_swap_leaves_room_for_both_legs_and_their_budgets() -> None:
    """The arithmetic that actually failed on the rig, as a unit assertion.

    A reverse swap of ``sendable`` is split by the provider into a prepayment
    plus a main invoice, and EACH gets its own ~1% routing-fee budget. All of
    that has to fit inside the channel's raw ceiling at the same time, because
    the provider only funds once both HTLCs have arrived. Pre-fix this balanced
    to within a satoshi; it must now clear comfortably.
    """
    raw = 804_515                       # the real channel from the failing run
    chan = _Chan(cid=b"\x09" * 32, short="1x1x9", capacity=2_000_000,
                 local_sat=raw, spendable_sat=raw)
    ln = _Lnworker([chan], _swap_manager())
    sendable = LiquidityPlugin._channel_sendable_sat(ln, chan)

    prepayment = 45_000                 # the rig provider's mining-fee invoice
    main = sendable - prepayment
    budget_pct = ONE_PERCENT_MILLIONTHS / 1_000_000
    required = sendable + int(main * budget_pct) + int(prepayment * budget_pct)

    assert required < raw, (
        f"a max swap still needs {required} of a {raw} sat channel; "
        f"this is the one-satoshi margin that made swaps fail")
    # Not merely positive -- comfortably clear, so an extra in-flight HTLC's
    # commitment cost cannot eat the margin.
    assert raw - required > 1_000, f"only {raw - required} sat of slack"


def test_the_swap_is_still_worth_doing() -> None:
    """Headroom must buy reliability without gutting the swap."""
    raw = 804_515
    chan = _Chan(cid=b"\x0a" * 32, short="1x1xa", capacity=2_000_000,
                 local_sat=raw, spendable_sat=raw)
    ln = _Lnworker([chan], _swap_manager())
    sendable = LiquidityPlugin._channel_sendable_sat(ln, chan)
    # 0.92, not 0.97: these fakes use a regtest-scale commitment feerate, where
    # a second in-flight HTLC genuinely costs ~2.4% of this channel. On a quiet
    # mainnet the same code reserves far less. The point of the bound is that the
    # headroom stays a slice, never a majority.
    assert sendable > 0.92 * raw, "headroom must not shave the swap to nothing"


def test_headroom_reserves_the_second_htlcs_overhead() -> None:
    """The second HTLC has to fit alongside the first.

    Electrum fires the mining-fee prepayment concurrently and does not pin it to
    our chosen channel, so it lands on that channel often enough to matter. The
    rig showed why reserving only a percentage was not enough: a 45,000 sat
    prepayment consumed 64,350 sat of capacity, because an in-flight HTLC also
    enlarges the commitment transaction.
    """
    raw = 804_515
    without = LiquidityPlugin._swap_sizing_headroom_sat(raw, 0)
    with_overhead = LiquidityPlugin._swap_sizing_headroom_sat(raw, 19_350)
    assert with_overhead == without + 19_350


def test_second_htlc_overhead_is_three_times_the_output_fee() -> None:
    """``available_to_spend`` already prices ONE added HTLC, including a
    fee-spike buffer at double the feerate. The swap's second concurrent HTLC
    therefore costs another output fee (1x) plus that buffer's matching growth
    (2x) -- 3x in total, from the channel's live feerate."""
    from electrum.lnutil import HTLC_OUTPUT_WEIGHT       # type: ignore
    feerate = 37_500                                     # sat/kw
    chan = SimpleNamespace(get_feerate=lambda subject, *, ctn: feerate,
                           get_next_ctn=lambda subject: 1)
    expected = -(-(3 * feerate * HTLC_OUTPUT_WEIGHT) // 1000)   # ceil to sat
    assert LiquidityPlugin._second_htlc_overhead_sat(chan) == expected


def test_the_overhead_scales_with_the_feerate() -> None:
    """A fixed constant would be wrong almost everywhere: this is mempool-driven,
    ~19,000 sat at regtest feerates and far smaller on a quiet mainnet."""
    def _chan(feerate):
        return SimpleNamespace(get_feerate=lambda subject, *, ctn: feerate,
                               get_next_ctn=lambda subject: 1)
    cheap = LiquidityPlugin._second_htlc_overhead_sat(_chan(300))
    busy = LiquidityPlugin._second_htlc_overhead_sat(_chan(37_500))
    assert 0 < cheap < busy
    assert busy > 15_000, "regtest-scale feerates must yield real headroom"


def test_an_unreadable_channel_does_not_break_sizing() -> None:
    """Sizing must never raise: fall back to the proportional headroom."""
    class _Broken:
        def get_feerate(self, subject, *, ctn):
            raise RuntimeError("no feerate")

        def get_next_ctn(self, subject):
            return 1
    assert LiquidityPlugin._second_htlc_overhead_sat(_Broken()) == 0


def test_a_max_swap_survives_the_prepayment_landing_first() -> None:
    """The exact failure from the rig, as an assertion.

    The prepayment lands on our channel first, taking its own value plus the
    commitment overhead with it; the main leg must still fit in what is left.
    """
    raw = 804_515
    prepayment = 45_000
    htlc_overhead = 19_350          # measured: 64,350 consumed by a 45,000 HTLC
    chan = _Chan(cid=b"\x0b" * 32, short="1x1xb", capacity=2_000_000,
                 local_sat=raw, spendable_sat=raw)
    ln = _Lnworker([chan], _swap_manager())
    swap = LiquidityPlugin._channel_sendable_sat(ln, chan)

    remaining = raw - prepayment - htlc_overhead
    main = swap - prepayment
    budget_pct = ONE_PERCENT_MILLIONTHS / 1_000_000
    assert main + int(main * budget_pct) < remaining, (
        f"main leg of {main} sat cannot fit in the {remaining} sat left after "
        f"the prepayment -- this is the NoPathFound the rig kept hitting")
