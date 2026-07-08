"""Glue-level tests for the "Manual run only" mode.

When ``plugins.inbound_liquidity.manual_run_only`` is set, every *automatic*
evaluation trigger -- wallet events (``_debounced_evaluate``), the heartbeat, the
post-grace one-shot, and the arm-switch kick (``request_evaluation``) -- must be a
no-op: they all funnel through ``_evaluate`` with ``manual=False``, which the
guard catches. A user-initiated "Run now" passes ``manual=True`` and must still
run. Unchecked, behaviour is unchanged (fully automated).

Heavy Electrum objects are faked; ``_wallet_ready`` is stubbed True so the guard
-- not the startup grace -- is what decides whether automation runs. Skipped
outside the electrum venv (needs the package ``__init__``, which imports
Electrum)."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import List

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
)


class _FakeWallet:
    def __init__(self) -> None:
        self.network = SimpleNamespace(asyncio_loop=None, is_connected=lambda: True)

    def basename(self) -> str:
        return "test-wallet"


def _plugin(*, manual_run_only: bool = False,
            automation_enabled: bool = True) -> LiquidityPlugin:
    """A plugin whose ``_evaluate`` sub-steps are all stubbed, so a run is
    observable purely by which stubs fire. ``config`` carries only the flag the
    guard reads; ``read_config`` returns the master-switch state."""
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.manual_run_only")
    p.config = SimpleNamespace(
        INBOUND_LIQUIDITY_MANUAL_RUN_ONLY=manual_run_only)
    p.read_config = lambda: SimpleNamespace(automation_enabled=automation_enabled)
    # Past the startup grace / connected: isolate the guard from the readiness gate.
    p._wallet_ready = lambda wal: True
    # Stub every side-effecting sub-step so a run is a list of tags.
    called: List[str] = []
    p._enforce_min_funding_floor = lambda: called.append("floor")
    p._reconcile_pending_swaps = lambda wal: called.append("reconcile")
    p._maybe_pay_dev_fee = lambda wal: called.append("dev_fee")
    p._scan_channel_health = lambda wal: called.append("health")
    p._scan_offline_autoclose = lambda wal: called.append("autoclose")
    p.build_snapshot = lambda wal, t=None: called.append("snapshot") or SimpleNamespace()
    p._swap_may_be_needed = lambda base, config: False

    async def _run_decision(wal, snap, config, transport):
        called.append("decision")

    p._run_decision = _run_decision
    p._called = called  # type: ignore[attr-defined]
    return p


def _run(p: LiquidityPlugin, w: _FakeWallet, *, manual: bool) -> List[str]:
    p.wallets = {w: asyncio.Lock()}
    asyncio.run(p._evaluate(w, manual=manual))
    return p._called  # type: ignore[attr-defined]


# --- the guard helper -----------------------------------------------------
def test_manual_run_only_helper_reads_config() -> None:
    assert _plugin(manual_run_only=True)._manual_run_only() is True
    assert _plugin(manual_run_only=False)._manual_run_only() is False
    # Absent key -> defaults False (a wallet configured before the feature shipped).
    p = object.__new__(LiquidityPlugin)
    p.config = SimpleNamespace()
    assert p._manual_run_only() is False


# --- the guard in _evaluate -----------------------------------------------
def test_automatic_tick_is_noop_when_manual_run_only() -> None:
    # An automatic trigger (manual=False) must take NO action: no health scan,
    # no snapshot, no decision -- the guard returns before all of them.
    p, w = _plugin(manual_run_only=True), _FakeWallet()
    called = _run(p, w, manual=False)
    assert "decision" not in called
    assert "health" not in called
    assert "snapshot" not in called
    # The pre-gate floor re-assertion still runs (it precedes every gate, so a
    # lowered funding floor applies to manual opens even while paused).
    assert called == ["floor"]


def test_manual_run_still_runs_when_manual_run_only() -> None:
    # "Run now" (manual=True) bypasses the guard and evaluates fully.
    p, w = _plugin(manual_run_only=True), _FakeWallet()
    called = _run(p, w, manual=True)
    assert "decision" in called
    assert "health" in called and "autoclose" in called


def test_automatic_tick_runs_when_not_manual_run_only() -> None:
    # Unchecked: current behaviour unchanged -- an automatic tick evaluates.
    p, w = _plugin(manual_run_only=False), _FakeWallet()
    called = _run(p, w, manual=False)
    assert "decision" in called
    assert "health" in called and "autoclose" in called


def test_manual_run_still_needs_master_switch() -> None:
    # Manual bypasses the manual-run-only guard but NOT the master switch: with
    # automation disabled, even "Run now" takes no action (option A semantics).
    p, w = _plugin(manual_run_only=True, automation_enabled=False), _FakeWallet()
    called = _run(p, w, manual=True)
    assert called == ["floor"]


# --- request_evaluation forwards the manual flag --------------------------
def test_request_evaluation_forwards_manual_flag() -> None:
    # The "Run now" button calls request_evaluation(manual=True); the automatic
    # callers (start_wallet, arm-switch) leave it False. Verify the flag reaches
    # _evaluate. A real asyncio loop runs the scheduled coroutine.
    seen: List[bool] = []

    async def _drive(manual_arg: bool) -> None:
        p = object.__new__(LiquidityPlugin)

        async def _fake_evaluate(wal, *, manual: bool = False) -> None:
            seen.append(manual)

        p._evaluate = _fake_evaluate  # type: ignore[assignment]
        loop = asyncio.get_running_loop()
        w = SimpleNamespace(network=SimpleNamespace(asyncio_loop=loop))
        p.request_evaluation(w, manual=manual_arg)
        await asyncio.sleep(0)  # let the scheduled coroutine run

    asyncio.run(_drive(True))
    asyncio.run(_drive(False))
    assert seen == [True, False]
