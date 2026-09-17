"""Glue tests for the automatic-evaluation rate limit and the offer-event mute.

The bug these pin down: in automatic mode the plugin evaluated continuously --
opening a nostr session against nine public relays every few seconds, forever,
with nothing in the wallet changing between runs. Two independent causes, both
covered here:

  * a *feedback loop* -- our own provider discovery makes Electrum emit
    ``swap_offers_changed`` once per announcement, which was treated as news and
    scheduled the next evaluation, which discovered again (``_on_offers_event``);
  * *no rate limit at all* on event-driven evaluation, so any steady trickle of
    wallet events produced back-to-back ticks (``_evaluate_rate_limited``).

The properties that matter, and that a future refactor must not quietly lose:

  * a deferred trigger is DELAYED, never DROPPED (a trailing run is queued);
  * a material change -- above all, a received Lightning payment -- bypasses the
    window, so the rate limit can never cost inbound liquidity;
  * the heartbeat and a manual "Run now" are never rate-limited, so the
    time-based watchdogs keep their cadence however deep the backoff is;
  * the backoff only grows on a tick that was provably a repeat, and resets the
    moment anything happens.

Heavy Electrum objects are faked; skipped outside the electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    AUTO_EVAL_BACKOFF_FACTOR,
    MAX_AUTO_EVAL_INTERVAL_SEC,
    MIN_AUTO_EVAL_INTERVAL_SEC,
    TRAILING_EVAL_RETRY_SEC,
    HEARTBEAT_INTERVAL_SEC,
    LiquidityPlugin,
    STATUS_SLEEPING,
    _OFFER_TRIGGER_EVENTS,
    _TRIGGER_EVENTS,
    format_duration,
    is_terminal_status,
    sleeping_status,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ChannelSnapshot,
    LiquidityConfig,
    LiquiditySnapshot,
)


# --- fakes ----------------------------------------------------------------
class _FakeWallet:
    def __init__(self) -> None:
        self.network = SimpleNamespace(asyncio_loop=None, is_connected=lambda: True)

    def basename(self) -> str:
        return "test-wallet"


def _config(**over) -> LiquidityConfig:
    base: Dict[str, Any] = dict(
        automation_enabled=True,
        min_onchain_to_open_sat=50_000,
        onchain_reserve_sat=10_000,
        max_channels=2,
        max_swap_fee_pct=0.6,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
    )
    base.update(over)
    return LiquidityConfig(**base)


def _channel(*, channel_id: str = "chan1", local: int = 100_000,
             remote: int = 100_000) -> ChannelSnapshot:
    return ChannelSnapshot(
        channel_id=channel_id, short_id="117x1x0", capacity_sat=local + remote,
        local_sat=local, remote_sat=remote, spendable_local_sat=local,
        is_active=True)


def _snapshot(*, onchain: int = 10_233, channels=(), **over) -> LiquiditySnapshot:
    fields: Dict[str, Any] = dict(
        onchain_spendable_sat=onchain,
        channels=tuple(channels),
        swap_percentage_fee=0.5,
        provider_max_reverse_sat=None,
        provider_min_amount_sat=None,
    )
    fields.update(over)
    return LiquiditySnapshot(**fields)


def _plugin() -> LiquidityPlugin:
    """A plugin with the rate-limit bookkeeping wired and everything else stubbed."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.ratelimit")
    p.wallets = {}
    p._tick_status = {}
    p._started_at = {}
    p._startup_grace_sec = 0.0
    p._eval_pending = {}
    p._trailing_retry_sec = 0.05
    p._last_eval_at = {}
    p._auto_eval_interval = {}
    p._last_state_sig = {}
    p._trailing_eval_tasks = {}
    p.config = SimpleNamespace(INBOUND_LIQUIDITY_MANUAL_RUN_ONLY=False)
    p.on_status_changed = lambda wallet, status: None
    return p


def _wire_tick(p, *, snapshot: LiquiditySnapshot, config: Optional[LiquidityConfig] = None,
               acted: bool = False) -> List[str]:
    """Stub a tick down to "it ran", and record each run. Returns the run log."""
    runs: List[str] = []
    cfg = config if config is not None else _config()
    p._enforce_min_funding_floor = lambda: 0
    p.read_config = lambda: cfg
    p._reconcile_pending_swaps = lambda wallet: None
    p._maybe_pay_dev_fee = lambda wallet: None
    p._scan_channel_health = lambda wallet: None
    p._scan_offline_autoclose = lambda wallet: None
    p._wallet_ready = lambda w, **kw: True
    p._readiness_block = lambda w, **kw: None
    p.build_snapshot = lambda wallet, transport=None: p._snap
    p._snap = snapshot
    p._swap_may_be_needed = lambda base, config: False

    async def _run_decision(wallet, snap, config, transport):
        runs.append("ran")
        return acted
    p._run_decision = _run_decision
    return runs


def _run(coro):
    return asyncio.run(coro)


# --- the feedback loop ----------------------------------------------------
def test_offer_events_are_registered_separately_from_fund_events() -> None:
    """The split is the fix: `swap_offers_changed` needs its own handler so it
    can be muted while we discover, without muting real wallet activity."""
    assert "swap_offers_changed" not in _TRIGGER_EVENTS
    assert _OFFER_TRIGGER_EVENTS == ["swap_offers_changed"]
    # The fund events -- the ones that mean inbound liquidity moved -- are all
    # still subscribed, unconditionally.
    for event in ("wallet_updated", "channels_updated", "request_status",
                  "payment_succeeded", "adb_added_verified_tx",
                  "adb_set_up_to_date"):
        assert event in _TRIGGER_EVENTS


def test_offer_event_during_our_own_session_is_ignored() -> None:
    """The exact loop from the bug report: our discovery echoes back as an
    offer event and must NOT schedule another evaluation."""
    p = _plugin()
    seen: List[Tuple] = []

    async def _on_wallet_event(*args):
        seen.append(args)
    p._on_wallet_event = _on_wallet_event

    p._open_swap_sessions = 1                     # a session is open (we are it)
    _run(p._on_offers_event("swap_offers_changed", []))
    assert seen == [], "our own discovery must not re-trigger evaluation"


def test_offer_event_outside_a_session_still_wakes_us() -> None:
    """Muting is scoped to our own sessions: a provider discovered by Electrum's
    own swap machinery is real news and must still be handled."""
    p = _plugin()
    seen: List[Tuple] = []

    async def _on_wallet_event(*args):
        seen.append(args)
    p._on_wallet_event = _on_wallet_event

    p._open_swap_sessions = 0
    _run(p._on_offers_event("swap_offers_changed", []))
    assert len(seen) == 1


def test_session_counter_is_released_even_when_the_session_raises() -> None:
    """A session that dies mid-flight must not leave the plugin permanently deaf
    to offer events -- that would silently disable a real trigger forever."""
    p = _plugin()
    p.config = SimpleNamespace(SWAPSERVER_URL="http://swap.example")

    class _Boom(Exception):
        pass

    class _FakeTransport:
        async def __aenter__(self):
            raise _Boom()

        async def __aexit__(self, *exc):
            return False

    wallet = _FakeWallet()
    wallet.lnworker = SimpleNamespace(swap_manager=SimpleNamespace(
        create_transport=lambda: _FakeTransport()))

    async def main() -> None:
        with pytest.raises(_Boom):
            async with p._swap_session(wallet):
                pass
    _run(main())
    assert p._open_session_count() == 0


def test_overlapping_sessions_do_not_unmute_each_other() -> None:
    """Why the mute is a counter and not a flag: two wallets can hold sessions at
    once, and with a flag whichever finished first would unmute while the other
    was still discovering -- letting its offers restart the loop."""
    p = _plugin()
    p.config = SimpleNamespace(SWAPSERVER_URL="http://swap.example")

    class _FakeTransport:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def _wallet():
        w = _FakeWallet()
        w.lnworker = SimpleNamespace(swap_manager=SimpleNamespace(
            create_transport=lambda: _FakeTransport(),
            is_initialized=asyncio.Event()))
        w.lnworker.swap_manager.is_initialized.set()
        return w

    w1, w2 = _wallet(), _wallet()

    async def main() -> None:
        async with p._swap_session(w1):
            assert p._open_session_count() == 1
            async with p._swap_session(w2):
                assert p._open_session_count() == 2
            # w2's session closed, but w1 is STILL discovering: stay muted.
            assert p._open_session_count() == 1, \
                "the inner session's exit must not unmute the outer one"
        assert p._open_session_count() == 0, "fully unmuted once all are done"
    _run(main())


def test_a_normal_session_exit_releases_the_mute() -> None:
    """The happy path, stated separately from the exception path: a plugin left
    muted after a clean session would ignore real offer events forever."""
    p = _plugin()
    p.config = SimpleNamespace(SWAPSERVER_URL="http://swap.example")

    class _FakeTransport:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    wallet = _FakeWallet()
    wallet.lnworker = SimpleNamespace(swap_manager=SimpleNamespace(
        create_transport=lambda: _FakeTransport(),
        is_initialized=asyncio.Event()))
    wallet.lnworker.swap_manager.is_initialized.set()

    async def main() -> None:
        async with p._swap_session(wallet):
            pass
        assert p._open_session_count() == 0
    _run(main())


# --- the rate limit: defer, but never drop --------------------------------
def test_wallet_events_are_routed_through_the_rate_limit() -> None:
    """The single line that makes the whole fix work.

    ``_debounced_evaluate`` must hand off to ``_evaluate_rate_limited``, not call
    ``_evaluate`` directly. Pointing it back at ``_evaluate`` restores the storm
    exactly, and every other test here would still pass -- they drive the rate
    limiter directly. So assert the wiring itself.
    """
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    p._eval_pending[wallet] = True
    seen: List[str] = []

    async def _rate_limited(w):
        seen.append("rate-limited")
    p._evaluate_rate_limited = _rate_limited

    async def _direct(w, **kw):
        seen.append("direct")
    p._evaluate = _direct

    async def main() -> None:
        await p._debounced_evaluate(wallet)
    _run(main())
    assert seen == ["rate-limited"], \
        "event-driven evaluation must go through the rate limit"
    assert p._eval_pending[wallet] is False, "the debounce flag must be released"


def test_arming_the_switch_is_not_rate_limited() -> None:
    """``request_evaluation`` (wallet load, and the ENABLED slider) calls
    ``_evaluate`` directly so flipping the switch acts at once rather than
    waiting out a window the previous session left behind."""
    p = _plugin()
    wallet = _FakeWallet()
    seen: List[str] = []

    async def _direct(w, *, manual=False):
        seen.append(f"direct manual={manual}")

    async def _rate_limited(w):
        seen.append("rate-limited")
    p._evaluate = _direct
    p._evaluate_rate_limited = _rate_limited

    wallet.network = SimpleNamespace(asyncio_loop=object())

    import electrum.plugins.inbound_liquidity as pkg
    original = pkg.asyncio.run_coroutine_threadsafe
    scheduled: List[str] = []

    def _capture(coro, loop):
        # Name the coroutine function before closing it: that is what says
        # whether the kick went to _evaluate or through the rate limiter.
        scheduled.append(coro.cr_code.co_name)
        coro.close()
        return None
    pkg.asyncio.run_coroutine_threadsafe = _capture
    try:
        p.wallets[wallet] = asyncio.Lock()
        p.request_evaluation(wallet)
        p.request_evaluation(wallet, manual=True)
    finally:
        pkg.asyncio.run_coroutine_threadsafe = original

    assert scheduled == ["_direct", "_direct"], (
        f"the arm-switch kick must call _evaluate directly, not the rate "
        f"limiter; scheduled {scheduled!r}")
    assert seen == [], "the coroutines were captured, not awaited"



def test_an_event_inside_the_window_is_deferred_not_dropped() -> None:
    """The trigger is honoured by a queued trailing run, not thrown away."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    snap = _snapshot(channels=[_channel()])
    runs = _wire_tick(p, snapshot=snap)

    async def main() -> None:
        p._min_auto_eval_interval_sec = 0.05
        p._max_auto_eval_interval_sec = 0.05
        await p._evaluate_rate_limited(wallet)          # first: runs at once
        assert runs == ["ran"]
        await p._evaluate_rate_limited(wallet)          # second: inside window
        assert runs == ["ran"], "a second tick inside the window must not run"
        assert wallet in p._trailing_eval_tasks, "the trigger must be queued"
        await asyncio.sleep(0.15)                       # let the window elapse
        assert runs == ["ran", "ran"], "the deferred trigger must still run"
    _run(main())


def test_many_events_inside_one_window_coalesce_onto_one_trailing_run() -> None:
    """The storm case: a burst of triggers must cost exactly one extra run."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 0.05
        p._max_auto_eval_interval_sec = 0.05
        await p._evaluate_rate_limited(wallet)
        for _ in range(20):
            await p._evaluate_rate_limited(wallet)
        assert runs == ["ran"]
        await asyncio.sleep(0.2)
        assert runs == ["ran", "ran"], "20 triggers must not mean 20 runs"
    _run(main())


def test_running_now_cancels_a_queued_trailing_run() -> None:
    """A material change that runs immediately supersedes the queued run, rather
    than letting it fire a redundant second evaluation moments later."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 0.05
        p._max_auto_eval_interval_sec = 0.05
        await p._evaluate_rate_limited(wallet)
        await p._evaluate_rate_limited(wallet)          # deferred + queued
        queued = p._trailing_eval_tasks[wallet]
        # A payment lands: the snapshot changes, so the next trigger runs now.
        p._snap = _snapshot(channels=[_channel(local=150_000, remote=50_000)])
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran", "ran"]
        await asyncio.sleep(0.2)                        # past when it would have fired
        assert queued.cancelled() or queued.done()
        assert runs == ["ran", "ran"], "the superseded run must not also fire"
    _run(main())


# --- the change bypass: never cost inbound liquidity ----------------------
def test_a_received_lightning_payment_bypasses_the_window() -> None:
    """The case the plugin exists for. Receiving over Lightning moves balance
    from remote to local -- consuming inbound liquidity -- so it must evaluate
    immediately, however deep the backoff has grown."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel(local=50_000, remote=150_000)]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 3600.0      # an hour: nothing gets through on time
        p._max_auto_eval_interval_sec = 3600.0
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran"]
        await p._evaluate_rate_limited(wallet)      # unchanged -> deferred
        assert runs == ["ran"]
        # 100k sat received: remote balance drops, local rises.
        p._snap = _snapshot(channels=[_channel(local=150_000, remote=50_000)])
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran", "ran"], "a received payment must not wait out the window"
    _run(main())


def test_an_onchain_receive_bypasses_the_window() -> None:
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(onchain=10_233))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 3600.0
        await p._evaluate_rate_limited(wallet)
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran"]
        p._snap = _snapshot(onchain=500_000)        # funded on-chain
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran", "ran"]
    _run(main())


def test_editing_a_config_threshold_bypasses_the_window() -> None:
    """Raising the fee ceiling in the dialog should take effect on the next tick,
    not after a backoff -- the user is watching."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 3600.0
        await p._evaluate_rate_limited(wallet)
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran"]
        p.read_config = lambda: _config(max_swap_fee_pct=5.0)
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran", "ran"]
    _run(main())


def test_an_unreadable_snapshot_errs_toward_running() -> None:
    """The signature is an optimisation; if it cannot be computed the tick must
    happen anyway. Failing closed here would stall automation outright."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 3600.0
        await p._evaluate_rate_limited(wallet)
        assert runs == ["ran"]

        def _boom(wallet, transport=None):
            raise RuntimeError("snapshot exploded")
        p.build_snapshot = _boom
        # Unreadable state reads as "changed", so the trigger is let through
        # rather than parked for an hour behind a signature we cannot compute.
        assert p._current_state_sig(wallet) is None
        assert p._material_state_changed(wallet) is True
        await p._evaluate_rate_limited(wallet)
        assert wallet not in p._trailing_eval_tasks, "must evaluate, not defer"
    _run(main())


def test_the_signature_ignores_drifting_fee_fields() -> None:
    """Fee estimates move with the mempool on their own. If they counted as a
    change the backoff would never engage and the storm would be back."""
    p = _plugin()
    quiet = _snapshot(channels=[_channel()])
    noisy = _snapshot(
        channels=quiet.channels,
        swap_percentage_fee=0.9,
        provider_max_reverse_sat=1_000_000,
        provider_min_amount_sat=10_000,
        swap_mining_fee_sat=12_345,
        swap_claim_fee_sat=6_789,
    )
    cfg = _config()
    assert p._material_state_sig(quiet, cfg) == p._material_state_sig(noisy, cfg)


def test_the_signature_catches_every_balance_and_count_field() -> None:
    """Each field that can mean "there is work to do" must move the signature."""
    p = _plugin()
    cfg = _config()
    base = _snapshot(channels=[_channel()])
    base_sig = p._material_state_sig(base, cfg)
    variants = [
        _snapshot(onchain=999_999, channels=[_channel()]),
        _snapshot(channels=[_channel(local=1)]),
        _snapshot(channels=[_channel(remote=1)]),
        _snapshot(channels=[_channel(channel_id="other")]),
        _snapshot(channels=[]),
        _snapshot(channels=base.channels, pending_channel_count=1),
        _snapshot(channels=base.channels, inflight_swap_count=1),
        _snapshot(channels=base.channels, closing_channel_count=1),
        _snapshot(channels=base.channels, pending_open_freeze_count=1),
        _snapshot(channels=base.channels, opens_last_24h=1),
    ]
    for variant in variants:
        assert p._material_state_sig(variant, cfg) != base_sig, variant


def test_the_signature_catches_the_per_channel_flags() -> None:
    """The flags the engine's swap decision turns on. A channel that gains an
    unsettled HTLC, goes inactive, or changes scope is a different situation --
    deferring those behind a backoff would act on a stale picture."""
    p = _plugin()
    cfg = _config()
    base = _snapshot(channels=[_channel()])
    base_sig = p._material_state_sig(base, cfg)
    flags = [
        dict(is_active=False),
        dict(has_unsettled_htlcs=True),
        dict(unsettled_is_swap=True),
        dict(is_plugin_opened=False),
        dict(spendable_local_sat=1),
        dict(capacity_sat=999),
        dict(short_id="999x9x9"),
    ]
    for over in flags:
        fields = dict(channel_id="chan1", short_id="117x1x0", capacity_sat=200_000,
                      local_sat=100_000, remote_sat=100_000,
                      spendable_local_sat=100_000, is_active=True)
        fields.update(over)
        variant = _snapshot(channels=[ChannelSnapshot(**fields)])
        changed = p._material_state_sig(variant, cfg) != base_sig
        # short_id is cosmetic (a display label for the same channel), so it is
        # deliberately NOT part of the signature; everything else must move it.
        if "short_id" in over:
            assert not changed, "a cosmetic relabel must not count as a change"
        else:
            assert changed, f"{over} did not move the signature"


def test_the_signature_is_stable_for_an_unchanged_wallet() -> None:
    """The property the whole backoff rests on: recomputing over an identical
    wallet must produce an identical signature. Any instability here (an
    unordered set, an object identity, a timestamp) and the plugin would read
    every tick as "changed" and never rest -- the original bug, back again."""
    p = _plugin()
    cfg = _config()
    snap = _snapshot(channels=[_channel(channel_id="a"), _channel(channel_id="b")])
    first = p._material_state_sig(snap, cfg)
    for _ in range(5):
        assert p._material_state_sig(snap, cfg) == first
    # ...including across separately-built but equal snapshots and configs.
    rebuilt = _snapshot(channels=[_channel(channel_id="a"), _channel(channel_id="b")])
    assert p._material_state_sig(rebuilt, _config()) == first


def test_the_signature_is_hashable_and_comparable() -> None:
    """It is stored and compared across ticks, so it has to be a plain value --
    not something carrying a live wallet-db object (the reason the rest of this
    plugin rebuilds db reads as plain data)."""
    p = _plugin()
    sig = p._material_state_sig(_snapshot(channels=[_channel()]), _config())
    assert isinstance(sig, tuple)
    assert {sig}, "signature must be hashable"


# --- the idle backoff -----------------------------------------------------
def test_identical_idle_ticks_grow_the_interval_up_to_the_cap() -> None:
    """The log in the bug report, tick after tick: same state, 0 actions. Each
    repeat should buy a longer break, up to the heartbeat."""
    p = _plugin()
    wallet = _FakeWallet()
    p._min_auto_eval_interval_sec = 10.0
    p._max_auto_eval_interval_sec = 40.0

    p._note_evaluation_outcome(wallet, changed=False, acted=False, manual=False)
    assert p._auto_eval_interval[wallet] == 20.0
    p._note_evaluation_outcome(wallet, changed=False, acted=False, manual=False)
    assert p._auto_eval_interval[wallet] == 40.0
    p._note_evaluation_outcome(wallet, changed=False, acted=False, manual=False)
    assert p._auto_eval_interval[wallet] == 40.0, "must not grow past the cap"


@pytest.mark.parametrize("changed,acted,manual", [
    (True, False, False),      # the wallet moved
    (False, True, False),      # we opened / swapped / closed
    (False, False, True),      # the user pressed "Run now"
])
def test_anything_happening_resets_the_backoff(changed, acted, manual) -> None:
    p = _plugin()
    wallet = _FakeWallet()
    p._min_auto_eval_interval_sec = 10.0
    p._max_auto_eval_interval_sec = 640.0
    p._auto_eval_interval[wallet] = 640.0          # fully backed off

    p._note_evaluation_outcome(wallet, changed=changed, acted=acted, manual=manual)
    assert p._auto_eval_interval[wallet] == 10.0


def test_the_backoff_cap_never_exceeds_the_heartbeat() -> None:
    """Beyond the heartbeat a bigger cap buys nothing -- the heartbeat is running
    anyway -- and it would start starving the time-based watchdogs."""
    assert MAX_AUTO_EVAL_INTERVAL_SEC <= HEARTBEAT_INTERVAL_SEC
    assert MIN_AUTO_EVAL_INTERVAL_SEC < MAX_AUTO_EVAL_INTERVAL_SEC
    assert AUTO_EVAL_BACKOFF_FACTOR > 1.0


def test_a_full_tick_records_its_outcome_against_the_rate_limit() -> None:
    """End to end through the real _evaluate: an inert tick starts the window and
    grows the backoff; the next identical trigger is deferred."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    p._min_auto_eval_interval_sec = 30.0
    p._max_auto_eval_interval_sec = 600.0
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        await p._evaluate(wallet)
        assert runs == ["ran"]
        assert p._auto_eval_interval[wallet] == 30.0   # first run: state was "new"
        await p._evaluate(wallet)                      # identical -> inert
        assert runs == ["ran", "ran"]
        assert p._auto_eval_interval[wallet] == 60.0
        assert p._auto_eval_wait_sec(wallet) > 0
    _run(main())


def test_a_tick_that_died_does_not_back_off() -> None:
    """A tick that raised did not establish that there is nothing to do, so it
    must not buy itself a longer break. The window still applies (the tick did
    start, and retrying instantly would just re-raise), but the interval stays
    where it was rather than doubling on the strength of a failure."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    p._min_auto_eval_interval_sec = 30.0
    p._max_auto_eval_interval_sec = 600.0
    _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))
    p._diag_event = lambda wallet, **kw: None

    async def _boom(wallet, snap, config, transport):
        raise RuntimeError("decision exploded")
    p._run_decision = _boom

    async def main() -> None:
        await p._evaluate(wallet)               # _evaluate swallows the error
        assert wallet not in p._auto_eval_interval, \
            "a failed tick must not record a backoff"
        assert p._auto_eval_wait_sec(wallet) > 0, \
            "but it did run, so the window still applies"
    _run(main())


def test_the_retry_interval_has_a_sane_default() -> None:
    p = object.__new__(LiquidityPlugin)
    assert p._retry_sec() == TRAILING_EVAL_RETRY_SEC
    assert 0 < TRAILING_EVAL_RETRY_SEC < MIN_AUTO_EVAL_INTERVAL_SEC


def test_a_tick_that_acted_keeps_the_interval_at_the_floor() -> None:
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    p._min_auto_eval_interval_sec = 30.0
    p._max_auto_eval_interval_sec = 600.0
    _wire_tick(p, snapshot=_snapshot(channels=[_channel()]), acted=True)

    async def main() -> None:
        await p._evaluate(wallet)
        await p._evaluate(wallet)
        assert p._auto_eval_interval[wallet] == 30.0
    _run(main())


# --- the exempt paths -----------------------------------------------------
def test_the_heartbeat_is_never_rate_limited() -> None:
    """The time-based watchdogs (stuck opens, offline peers, stuck swaps) only
    advance when a tick runs, so the heartbeat must get through regardless of
    how deep the backoff is."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 3600.0
        p._max_auto_eval_interval_sec = 3600.0
        await p._evaluate(wallet)
        p._auto_eval_interval[wallet] = 3600.0
        # The heartbeat calls _evaluate directly, not _evaluate_rate_limited.
        await p._evaluate(wallet)
        assert runs == ["ran", "ran"], "the heartbeat must not be throttled"
    _run(main())


def test_a_manual_run_is_never_rate_limited() -> None:
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 3600.0
        await p._evaluate(wallet)
        await p._evaluate(wallet, manual=True)
        assert runs == ["ran", "ran"]
        # ...and it resets the backoff: the user is engaged, so be responsive.
        assert p._auto_eval_interval[wallet] == p._min_auto_eval_interval_sec
    _run(main())


def test_a_deferred_run_is_dropped_if_the_wallet_stops() -> None:
    """A queued run must not wake up and evaluate a wallet we no longer manage."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 0.05
        p._max_auto_eval_interval_sec = 0.05
        await p._evaluate_rate_limited(wallet)
        await p._evaluate_rate_limited(wallet)
        assert wallet in p._trailing_eval_tasks
        p.wallets.pop(wallet)                      # wallet closed mid-wait
        await asyncio.sleep(0.15)
        assert runs == ["ran"], "a stopped wallet must not be evaluated"
    _run(main())


# --- the backoff must never starve a time-based watchdog ------------------
# Caught by the rig e2e suite, not by the unit tests: backing off on an inert
# wallet starved the wedged-open and offline-peer watchdogs, which only advance
# when a tick runs and several of which need several ticks spread over time. The
# wallets those watchdogs run against are quiet BY DEFINITION -- a dead peer
# stops producing events, a wedged open sits at identical balances -- so the
# backoff read "nothing is happening" at exactly the wrong moment.
@pytest.mark.parametrize("snapshot_kwargs", [
    dict(pending_channel_count=1),        # wedged / stuck open watchdog
    dict(pending_open_freeze_count=1),
    dict(closing_channel_count=1),        # close tracked to completion
    dict(inflight_swap_count=1),          # stuck-swap reconciliation
])
def test_a_counting_watchdog_holds_the_interval_at_the_floor(snapshot_kwargs) -> None:
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    p._min_auto_eval_interval_sec = 30.0
    p._max_auto_eval_interval_sec = 600.0
    snap = _snapshot(channels=[_channel()], **snapshot_kwargs)
    assert p._watchdogs_pending(snap) is True
    _wire_tick(p, snapshot=snap)

    async def main() -> None:
        await p._evaluate(wallet)
        await p._evaluate(wallet)          # identical, inert -- but a watchdog pends
        await p._evaluate(wallet)
        assert p._auto_eval_interval[wallet] == 30.0, \
            "backing off here starves the watchdog that is counting down"
    _run(main())


def test_an_inactive_channel_holds_the_interval_at_the_floor() -> None:
    """The offline auto-close watchdog samples peer uptime once per tick, so
    skipping ticks does not just delay it -- it corrupts the metric it decides
    on. An inactive channel is the whole reason that watchdog exists."""
    p = _plugin()
    wallet = _FakeWallet()
    p.wallets[wallet] = asyncio.Lock()
    p._min_auto_eval_interval_sec = 30.0
    p._max_auto_eval_interval_sec = 600.0
    dead = ChannelSnapshot(
        channel_id="chan1", short_id="117x1x0", capacity_sat=200_000,
        local_sat=100_000, remote_sat=100_000, spendable_local_sat=100_000,
        is_active=False)
    snap = _snapshot(channels=[dead])
    assert p._watchdogs_pending(snap) is True
    _wire_tick(p, snapshot=snap)

    async def main() -> None:
        await p._evaluate(wallet)
        await p._evaluate(wallet)
        assert p._auto_eval_interval[wallet] == 30.0
    _run(main())


def test_a_healthy_idle_wallet_still_backs_off() -> None:
    """The other half of the contract: the storm case from the bug report had no
    watchdog pending (healthy channels, no pending open, no swap in flight), so
    the watchdog guard must not quietly disable the backoff everywhere."""
    p = _plugin()
    assert p._watchdogs_pending(_snapshot(channels=[_channel()])) is False
    assert p._watchdogs_pending(_snapshot(channels=[])) is False


def test_an_unreadable_snapshot_keeps_the_check_rate_up() -> None:
    p = _plugin()
    assert p._safe_watchdogs_pending(SimpleNamespace()) is True


# --- never lose a trigger to an in-flight tick ----------------------------
def test_a_trigger_arriving_mid_tick_is_queued_not_discarded() -> None:
    """``_evaluate`` bounces off the per-wallet lock, so a trigger handed to it
    mid-tick is consumed and lost. The running tick may have snapshotted before
    whatever just changed, so the trigger has to be carried forward instead."""
    p = _plugin()
    wallet = _FakeWallet()
    lock = asyncio.Lock()
    p.wallets[wallet] = lock
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 0.05
        p._max_auto_eval_interval_sec = 0.05
        await lock.acquire()                        # a tick is in flight
        try:
            await p._evaluate_rate_limited(wallet)
            assert runs == [], "must not run while the lock is held"
            assert wallet in p._trailing_eval_tasks, "the trigger must be carried"
        finally:
            lock.release()
        await asyncio.sleep(0.2)
        assert runs == ["ran"], "the carried trigger must run once the lock frees"
    _run(main())


def test_a_deferred_run_that_wakes_mid_tick_re_arms() -> None:
    """The same hole from the other side: a queued run that wakes up to a busy
    wallet must re-arm, not bounce off the lock and vanish."""
    p = _plugin()
    wallet = _FakeWallet()
    lock = asyncio.Lock()
    p.wallets[wallet] = lock
    runs = _wire_tick(p, snapshot=_snapshot(channels=[_channel()]))

    async def main() -> None:
        p._min_auto_eval_interval_sec = 0.05
        p._max_auto_eval_interval_sec = 0.05
        p._schedule_trailing_evaluation(wallet, 0.05)
        await lock.acquire()                        # busy when it wakes
        await asyncio.sleep(0.15)
        assert runs == []
        assert wallet in p._trailing_eval_tasks, "must have re-armed"
        lock.release()
        await asyncio.sleep(0.15)
        assert runs == ["ran"]
    _run(main())


def test_cancelling_a_queued_run_from_another_thread_goes_via_the_loop() -> None:
    """``stop_wallet`` arrives on the GUI thread (the ``close_wallet`` hook), and
    ``asyncio.Task.cancel()`` is not thread-safe. Off-loop cancels must hop onto
    the loop that owns the task instead of touching it directly."""
    p = _plugin()
    wallet = _FakeWallet()
    hops: List[str] = []

    class _FakeTask:
        def __init__(self) -> None:
            self.cancel_called = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancel_called = True

    task = _FakeTask()
    p._trailing_eval_tasks[wallet] = task
    wallet.network = SimpleNamespace(asyncio_loop=SimpleNamespace(
        call_soon_threadsafe=lambda fn: hops.append("threadsafe")))

    # No running loop here, so this stands in for the GUI thread.
    p._cancel_trailing_evaluation(wallet)
    assert hops == ["threadsafe"], "an off-loop cancel must go through the loop"
    assert not task.cancel_called, "must not touch the task directly"
    assert wallet not in p._trailing_eval_tasks


def test_cancelling_from_the_loop_thread_cancels_directly() -> None:
    p = _plugin()
    wallet = _FakeWallet()

    class _FakeTask:
        def __init__(self) -> None:
            self.cancel_called = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancel_called = True

    task = _FakeTask()

    async def main() -> None:
        p._trailing_eval_tasks[wallet] = task
        p._cancel_trailing_evaluation(wallet)
    _run(main())
    assert task.cancel_called


def test_a_dead_loop_does_not_break_wallet_teardown() -> None:
    """Shutdown order is not guaranteed: the loop can already be closed when the
    wallet is stopped. Teardown must not raise then."""
    p = _plugin()
    wallet = _FakeWallet()

    class _FakeTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            pass

    def _boom(fn):
        raise RuntimeError("Event loop is closed")

    p._trailing_eval_tasks[wallet] = _FakeTask()
    wallet.network = SimpleNamespace(asyncio_loop=SimpleNamespace(
        call_soon_threadsafe=_boom))
    p._cancel_trailing_evaluation(wallet)        # must not raise
    assert wallet not in p._trailing_eval_tasks


def test_each_wallet_gets_its_own_backoff() -> None:
    """The rate limit is per-wallet: a quiet wallet backing off must not slow
    down a busy one managed by the same plugin instance."""
    p = _plugin()
    quiet, busy = _FakeWallet(), _FakeWallet()
    p._min_auto_eval_interval_sec = 10.0
    p._max_auto_eval_interval_sec = 640.0

    for _ in range(4):
        p._note_evaluation_outcome(quiet, changed=False, acted=False, manual=False)
    p._note_evaluation_outcome(busy, changed=True, acted=False, manual=False)

    assert p._auto_eval_interval[quiet] == 160.0
    assert p._auto_eval_interval[busy] == 10.0


def test_rate_limit_bookkeeping_tolerates_a_partially_constructed_plugin() -> None:
    """Same contract the tick status already keeps: bookkeeping that assumed its
    dicts exist would AttributeError into the middle of an evaluation."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.ratelimit")
    wallet = _FakeWallet()

    assert p._auto_eval_wait_sec(wallet) == 0.0     # nothing recorded -> run now
    assert p._open_session_count() == 0
    assert p._min_interval_sec() == MIN_AUTO_EVAL_INTERVAL_SEC
    assert p._max_interval_sec() == MAX_AUTO_EVAL_INTERVAL_SEC
    p._note_evaluation_outcome(wallet, changed=False, acted=False, manual=False)
    assert p._auto_eval_interval[wallet] > MIN_AUTO_EVAL_INTERVAL_SEC
    p._cancel_trailing_evaluation(wallet)           # must not raise


# --- what the operator sees -----------------------------------------------
def test_the_resting_status_names_the_next_check() -> None:
    """"sleeping" alone cannot tell "waiting deliberately" from "wedged"."""
    assert sleeping_status(None) == STATUS_SLEEPING
    assert sleeping_status(0) == STATUS_SLEEPING
    assert sleeping_status(60) == "sleeping (next check in ~1m)"
    assert sleeping_status(600) == "sleeping (next check in ~10m)"
    # ...and it still reads as a resting state to the GUI.
    for seconds in (None, 0, 60, 600, 7200):
        assert is_terminal_status(sleeping_status(seconds))


def test_duration_formatting_rounds_up() -> None:
    """Rounding down would print "0m" while the plugin still had 40s to wait --
    which reads as a stall."""
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(61) == "2m"
    assert format_duration(600) == "10m"
    assert format_duration(3600) == "1h"
    assert format_duration(-5) == "0s"
