"""The cooperative-close retry ladder, driven through the REAL
``_maybe_cooperative_close``.

Why this file exists. Every other test of the coop-before-force escalation
(test_offline_autoclose_glue, test_peer_reliability_glue, test_wedge_*) replaces
``_maybe_cooperative_close`` with a stub that always reports success. That
proves ``_request_close``'s *sequencing* -- window, attempt count, escalation --
but it steps over the three guards inside the real method: the daily close
ceiling, the in-flight set, and ``COOP_CLOSE_COOLDOWN_SEC``. None of them had a
test, and one of them was load-bearing:

  * ``_request_close`` decides another attempt is due from the configured
    cooperative WINDOW, while ``_maybe_cooperative_close`` decides whether to
    launch one from the COOLDOWN. Configure the window shorter than the cooldown
    and they disagree -- the window elapses, attempt 2 is asked for, the cooldown
    refuses it, and ``_try_coop`` returning False falls straight through to the
    force-close. ``COOP_MAX_ATTEMPTS`` (3) silently became 1.
  * The setting is exposed in minutes and clamped only at 0, so every value
    under 5 reached that state -- and the rig's own e2e tests set 1.

``_wedge_params`` now floors the window at the cooldown, so the two can no
longer disagree. The ladder tests below pin that: three attempts, each one
window apart, then the force-close -- at the shipped window AND at a 1-minute
one. ``test_a_window_under_the_cooldown_is_floored`` is the regression test for
the clamp itself.

The real ``_do_cooperative_close`` is used throughout, against an lnworker whose
``close_channel`` raises -- a peer that is reachable but will not cooperate,
which is the only situation in which the retries happen at all. That also
exercises the ``finally`` that clears the in-flight flag, so attempt N+1 is not
blocked by attempt N's bookkeeping.

Heavy Electrum objects are faked; skipped outside the Electrum venv."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from conftest import seed_settings  # noqa: E402

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    CLOSE_ATTEMPT_DB_KEY,
    COOP_CLOSE_COOLDOWN_SEC,
    LiquidityPlugin,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    COOP_MAX_ATTEMPTS,
)

NODE_A = "02" + "aa" * 32
CID = "aa" * 32
DAY = 86400.0
# The shipped cooperative window (10 minutes), comfortably above the cooldown.
SHIPPED_WINDOW_SEC = pkg.COOP_BEFORE_FORCE_WINDOW_SEC


class _Clock:
    """A fake clock standing in for BOTH wall and monotonic time.

    The two have to move together here: ``_request_close`` measures the
    cooperative window on the wall clock (``now`` is passed in by the watchdogs,
    which read ``time.time``) while ``_maybe_cooperative_close`` measures its
    cooldown on ``time.monotonic``. A test that advanced only one of them would
    be testing a machine whose clocks disagree, which is precisely the bug under
    test rather than a fixture.
    """

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    import electrum.plugins.inbound_liquidity as mod
    c = _Clock()
    monkeypatch.setattr(mod.time, "time", c)
    monkeypatch.setattr(mod.time, "monotonic", c)
    return c


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeWallet:
    def __init__(self, lnworker=None, *, loop=None) -> None:
        self.db = _FakeDB()
        self.lnworker = lnworker
        self.network = SimpleNamespace(asyncio_loop=loop, is_connected=lambda: True)

    def save_db(self) -> None:
        pass


class _Chan:
    def __init__(self, state, *, cid=CID, node_id=NODE_A, reachable=True) -> None:
        self.channel_id = bytes.fromhex(cid)
        self.node_id = bytes.fromhex(node_id)
        self.state = state
        self.reachable = reachable
        self.lnworker = None

    def get_state(self):
        return self.state

    def is_active(self):
        return self.reachable

    @property
    def peer_state(self):
        from electrum.lnchannel import PeerState
        return PeerState.GOOD if self.reachable else PeerState.DISCONNECTED


def _lnworker(chan, *, coop_raises: bool = True):
    """An lnworker for a peer that is reachable but will not cooperate.

    ``close_channel`` raising is what makes the retry ladder observable: a
    cooperative close that SUCCEEDS ends the story on attempt 1, so the only way
    to reach attempts 2 and 3 is a peer that keeps refusing. The real
    ``_do_cooperative_close`` catches this and leaves the close intent in place,
    exactly as it does for a peer that vanishes mid-negotiation.
    """
    coop: List[bytes] = []
    forced: List[bytes] = []

    async def _close(cid):
        coop.append(cid)
        if coop_raises:
            raise Exception("peer refused to cooperate")
        chan.state = _state("SHUTDOWN")
        return "coop-txid"

    def _force(cid):
        forced.append(cid)
        # Electrum moves the channel out of OPEN when a force-close is
        # scheduled, and ``_request_close``'s ``>= SHUTDOWN`` guard is what then
        # stops the watchdogs asking again. Without modelling that, a fake
        # channel stays OPEN and every later tick force-closes it afresh --
        # which would hide a double-escalation rather than expose it.
        chan.state = _state("FORCE_CLOSING")

    ln = SimpleNamespace(
        channels={chan.channel_id: chan},
        close_channel=_close,
        schedule_force_closing=_force,
        lnpeermgr=SimpleNamespace(
            get_peer_by_pubkey=lambda pk: object() if chan.reachable else None),
    )
    chan.lnworker = ln
    return ln, coop, forced


def _plugin(**overrides) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.coopladder")
    p._coop_closing = {}
    p._coop_close_cooldown_until = {}
    p._close_capped_logged = {}
    p._last_decline_sigs = {}
    cfg = dict(
        INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=5,
        INBOUND_LIQUIDITY_LOG_RETENTION_DAYS=30,
        INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN=int(SHIPPED_WINDOW_SEC // 60),
    )
    cfg.update(overrides)
    p.config = SimpleNamespace(**cfg)
    seed_settings(p, **overrides)
    return p


def _state(name: str):
    from electrum.lnchannel import ChannelState
    return getattr(ChannelState, name)


async def _tick(p, w, chan, clock, *, reason="offline channel (auto-close)") -> None:
    """One watchdog call, then let the launched close task actually run.

    ``_request_close`` is synchronous and schedules the cooperative close as a
    task, so without draining the loop the attempt would still be "in flight"
    on the next tick and the in-flight guard -- not the cooldown -- would be what
    refused it. Draining keeps each tick's bookkeeping settled, which is the
    state a real wallet is in a tick later.
    """
    p._request_close(w, chan, CID, NODE_A, clock.t, reason=reason,
                     detail="test")
    for _ in range(8):
        await asyncio.sleep(0)


def _attempts(w) -> int:
    return int((w.db.get(CLOSE_ATTEMPT_DB_KEY, {}).get(CID) or {})
               .get("coop_attempts", 0) or 0)


# --- the ladder, at the shipped window ------------------------------------
def test_three_cooperative_attempts_precede_the_force_close(clock) -> None:
    """The property COOP_MAX_ATTEMPTS states: a reachable peer that refuses to
    cooperate is asked three times, one window apart, and only then force-closed.

    Asserted against the real ``_maybe_cooperative_close``, so the cooldown is
    genuinely in the path rather than stubbed past.
    """
    chan = _Chan(_state("OPEN"))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)             # attempt 1
        assert len(coop) == 1 and forced == []
        for expected in (2, 3):
            clock.advance(SHIPPED_WINDOW_SEC)
            await _tick(p, w, chan, clock)
            assert len(coop) == expected, \
                f"attempt {expected} never launched; the cooldown refused it"
            assert forced == [], "force-closed with attempts still owed"
        # The allowance is spent: the next tick past the window escalates.
        clock.advance(SHIPPED_WINDOW_SEC)
        await _tick(p, w, chan, clock)
        assert len(coop) == COOP_MAX_ATTEMPTS, "a fourth attempt was launched"
        assert forced == [bytes.fromhex(CID)]

    asyncio.run(go())
    assert _attempts(w) == COOP_MAX_ATTEMPTS


def test_the_force_close_log_counts_the_attempts_actually_made(clock) -> None:
    """The close action has to say how many times cooperation was tried -- it is
    the justification for having spent a force-close at all."""
    chan = _Chan(_state("OPEN"))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        for _ in range(COOP_MAX_ATTEMPTS + 1):
            await _tick(p, w, chan, clock)
            clock.advance(SHIPPED_WINDOW_SEC)

    asyncio.run(go())
    assert forced == [bytes.fromhex(CID)]
    closes = [e for e in p.get_decision_log(w, "action") if e.get("kind") == "close"]
    assert any(f"{COOP_MAX_ATTEMPTS} cooperative close attempt(s)"
               in (e.get("detail") or "") for e in closes), \
        "the force-close did not report the attempts it made"


# --- the clamp: a window under the cooldown -------------------------------
def test_a_window_under_the_cooldown_does_not_collapse_the_ladder(clock) -> None:
    """The regression test for the collision, asserted on BEHAVIOUR.

    Ticking once a configured minute -- which is what a watchdog on a 1-minute
    window does -- the force-close must not arrive until cooperation has had all
    ``COOP_MAX_ATTEMPTS`` of its attempts. Unclamped this failed at the second
    tick: the 1-minute window said attempt 2 was due, the 5-minute cooldown
    refused to launch it, and the channel was force-closed having been asked
    exactly once.

    Deliberately not asserted by advancing the clock a cooldown at a time: that
    would satisfy both clocks at once and pass either way, which is how the
    collision survived the existing escalation tests.
    """
    p, w = _plugin(INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN=1), _FakeWallet()
    chan = _Chan(_state("OPEN"))
    ln, coop, forced = _lnworker(chan)
    w.lnworker = ln
    attempts_when_forced: Optional[int] = None

    async def go():
        nonlocal attempts_when_forced
        # 20 minutes of once-a-minute ticks: long enough for three floored
        # windows (15 minutes) plus the escalation.
        for _ in range(20):
            await _tick(p, w, chan, clock)
            if forced and attempts_when_forced is None:
                attempts_when_forced = len(coop)
            clock.advance(60.0)

    asyncio.run(go())
    assert forced == [bytes.fromhex(CID)], "the ladder never escalated at all"
    assert attempts_when_forced == COOP_MAX_ATTEMPTS, (
        f"force-closed after {attempts_when_forced} cooperative attempt(s); "
        f"{COOP_MAX_ATTEMPTS} were owed -- the window and the cooldown are out "
        f"of step")


def test_the_floored_window_is_what_the_params_report(clock) -> None:
    """The floor is applied where the window is READ, so every consumer sees the
    effective value rather than re-deriving it."""
    p, w = _plugin(INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN=1), _FakeWallet()
    assert p._wedge_params(w)["coop_window_sec"] == COOP_CLOSE_COOLDOWN_SEC


def test_a_zero_window_is_floored_too(clock) -> None:
    """0 used to mean "escalate as soon as possible" and now means the cooldown.

    Documented deliberately rather than special-cased: the invariant the whole
    coop-before-force machinery holds is that cooperation always gets a genuine
    chance, and a window of 0 cannot give it one. A user who wants a fast
    force-close is better served by the force-close deadline above this.
    """
    p, w = _plugin(INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN=0), _FakeWallet()
    assert p._wedge_params(w)["coop_window_sec"] == COOP_CLOSE_COOLDOWN_SEC


def test_a_window_above_the_cooldown_is_left_alone(clock) -> None:
    """The floor must not become a ceiling: a generous window is the user's to
    set, and is what the shipped default relies on."""
    p, w = _plugin(INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN=30), _FakeWallet()
    assert p._wedge_params(w)["coop_window_sec"] == 30 * 60.0


# --- the guards inside _maybe_cooperative_close ---------------------------
def test_the_cooldown_refuses_a_second_launch_within_the_window(clock) -> None:
    """The cooldown's own job, asked directly: two calls inside
    ``COOP_CLOSE_COOLDOWN_SEC`` launch one close, not two. This is what stops
    every tick piling another attempt onto an async close that is still running.
    """
    chan = _Chan(_state("OPEN"))
    ln, coop, _forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        assert p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t) is True
        for _ in range(8):
            await asyncio.sleep(0)
        clock.advance(COOP_CLOSE_COOLDOWN_SEC - 1)
        assert p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t) is False
        assert len(coop) == 1
        # One second later the cooldown has expired and a launch is allowed.
        clock.advance(1.0)
        assert p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t) is True
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(coop) == 2

    asyncio.run(go())


def test_a_close_already_negotiating_is_never_interrupted(clock) -> None:
    """The in-flight guard. A cooperative close takes minutes; relaunching one
    mid-negotiation would be a second close request for the same channel."""
    chan = _Chan(_state("OPEN"))
    ln, coop, _forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)
    p._coop_closing[w] = {CID}
    assert p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t) is False
    assert coop == []


def test_the_daily_close_ceiling_refuses_a_launch(clock) -> None:
    """The cap is re-checked here and not only in ``_request_close``, because
    ``_scan_offline_autoclose`` calls this method DIRECTLY on its pre-deadline
    path with no cap check in front of it."""
    chan = _Chan(_state("OPEN"))
    ln, coop, _forced = _lnworker(chan)
    p, w = _plugin(INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY=1), _FakeWallet(ln)
    p._record_action_event(w, "close")                 # the one close is spent
    assert p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t) is False
    assert coop == []


def test_an_unreachable_peer_is_never_asked_to_cooperate(clock) -> None:
    """``_coop_close_possible`` is the gate: with nobody to talk to, the window
    is spent waiting for the peer to come back and the escalation is a
    force-close with no attempts recorded."""
    chan = _Chan(_state("OPEN"), reachable=False)
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)
        assert coop == [] and forced == []
        clock.advance(SHIPPED_WINDOW_SEC + 1)
        await _tick(p, w, chan, clock)
        assert coop == [], "asked an unreachable peer to cooperate"
        assert forced == [bytes.fromhex(CID)]

    asyncio.run(go())
    closes = [e for e in p.get_decision_log(w, "action") if e.get("kind") == "close"]
    assert any("never reachable" in (e.get("detail") or "") for e in closes)


def test_a_refused_launch_still_escalates_rather_than_wedging(clock) -> None:
    """``_try_coop`` returning False must not leave the channel in limbo. If no
    further cooperative attempt can be launched -- here because the daily ceiling
    was reached after the first attempt -- the window having elapsed means the
    force-close is the only move left.

    The ceiling is raised before the escalating tick so the cap check in
    ``_request_close`` passes while the one inside the launch path still refuses:
    that is the ordering that used to drop straight through to a force-close
    after a single attempt, and it is the behaviour being pinned.
    """
    chan = _Chan(_state("OPEN"))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)
        assert len(coop) == 1
        # The cooldown is what refuses attempt 2 here: the window has elapsed on
        # the wall clock but the monotonic cooldown has not, which can only
        # happen if the two are configured out of step. Simulated directly so the
        # escalation path is covered even with the clamp in place.
        p._coop_close_cooldown_until[CID] = clock.t + 10 * COOP_CLOSE_COOLDOWN_SEC
        clock.advance(SHIPPED_WINDOW_SEC)
        await _tick(p, w, chan, clock)
        assert len(coop) == 1, "launched a close the cooldown should have refused"
        assert forced == [bytes.fromhex(CID)], \
            "no attempt could be launched and nothing escalated either"

    asyncio.run(go())


def test_a_negotiation_outlasting_the_window_is_not_force_closed_over(clock) -> None:
    """``_request_close``'s own in-flight guard, which sits ahead of the whole
    attempt/escalation decision.

    A cooperative close can legitimately take longer than the window it was
    given. When it does, the window elapsing must NOT be read as "cooperation
    failed, force-close it" -- there is a live negotiation that may yet settle,
    and force-closing over it would spend a mining fee and a CSV timelock to
    pre-empt the cheap close already under way.
    """
    chan = _Chan(_state("OPEN"))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)                 # attempt 1, then settled
        assert len(coop) == 1
        # That attempt is still negotiating as far as the plugin knows.
        p._coop_closing[w] = {CID}
        for _ in range(3):
            clock.advance(SHIPPED_WINDOW_SEC)
            await _tick(p, w, chan, clock)
            assert forced == [], "force-closed over a live negotiation"
            assert len(coop) == 1, "launched a second close over a live one"

    asyncio.run(go())


def test_waiting_out_the_window_asks_for_nothing(clock) -> None:
    """Inside the window, with an attempt already made, a tick is a no-op --
    neither another launch nor an escalation. This is the arm that keeps a
    once-per-tick watchdog from spending the whole ladder in one second."""
    chan = _Chan(_state("OPEN"))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)
        assert len(coop) == 1
        for _ in range(5):
            clock.advance(SHIPPED_WINDOW_SEC / 10)
            await _tick(p, w, chan, clock)
        assert len(coop) == 1 and forced == []

    asyncio.run(go())


# --- scheduling ------------------------------------------------------------
def test_the_close_runs_on_the_wallets_loop_when_there_is_one(clock) -> None:
    """With a wallet that has an asyncio loop, the close is handed to it through
    ``run_coroutine_threadsafe`` -- the watchdogs can run on the GUI thread, so
    the loop is not necessarily the caller's."""
    chan = _Chan(_state("OPEN"))
    ln, coop, _forced = _lnworker(chan)
    p = _plugin()

    async def go():
        w = _FakeWallet(ln, loop=asyncio.get_running_loop())
        assert p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t) is True
        for _ in range(8):
            await asyncio.sleep(0)
        assert coop == [chan.channel_id]

    asyncio.run(go())


def test_a_refusing_peer_leaves_the_inflight_flag_clear(clock) -> None:
    """The real ``_do_cooperative_close``'s ``finally``: a close that raises must
    still clear the in-flight flag, or the channel could never be retried."""
    chan = _Chan(_state("OPEN"))
    ln, coop, _forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        p._maybe_cooperative_close(w, chan, CID, NODE_A, clock.t)
        for _ in range(8):
            await asyncio.sleep(0)
        assert coop == [chan.channel_id]
        assert CID not in p._coop_closing.get(w, set())

    asyncio.run(go())


# --- the states that stop a close before it starts ------------------------
@pytest.mark.parametrize("state_name", ["SHUTDOWN", "CLOSING", "FORCE_CLOSING"])
def test_an_already_closing_channel_is_left_alone(clock, state_name) -> None:
    chan = _Chan(_state(state_name))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)
        clock.advance(SHIPPED_WINDOW_SEC + 1)
        await _tick(p, w, chan, clock)

    asyncio.run(go())
    assert coop == [] and forced == []


def test_a_toxic_channel_is_never_touched(clock) -> None:
    """Our commitment tx is revoked; publishing it forfeits the balance. Only the
    remote may close this one."""
    chan = _Chan(_state("WE_ARE_TOXIC"))
    ln, coop, forced = _lnworker(chan)
    p, w = _plugin(), _FakeWallet(ln)

    async def go():
        await _tick(p, w, chan, clock)
        clock.advance(10 * SHIPPED_WINDOW_SEC)
        await _tick(p, w, chan, clock)

    asyncio.run(go())
    assert coop == [] and forced == []
