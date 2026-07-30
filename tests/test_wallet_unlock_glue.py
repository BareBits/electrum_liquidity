"""Glue-level tests for the password/unlock gate.

The bug these pin down: the plugin used to raise "wallet requires password for
channel open" on every tick of a password-protected wallet. It reached that raise
*after* a full evaluation (snapshot, watchdogs, possibly a nostr session), the
error escaped into the generic handler, and nothing told the user what to do
about it. The promised GUI override that would supply a password never existed.

The behaviour now: the plugin reads Electrum's own unlock cache (the Lock/Unlock
button fills it), and a locked wallet is a *readiness block* -- the tick defers
before doing any work and comes to rest on a status that names the reason.

Heavy Electrum objects are faked; skipped outside the electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    STATUS_LOCKED,
    STATUS_SLEEPING,
    TERMINAL_STATUSES,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    BLOCK_LOCKED,
)

PASSWORD = "hunter2"


class _FakeWallet:
    """Just the slice of Abstract_Wallet the unlock gate touches.

    ``keystore_encrypted`` is Electrum's ``has_keystore_encryption`` (signing
    needs a password); ``storage_encrypted`` is the file-on-disk kind, which
    does NOT make signing require one -- the two are deliberately separable here
    because the gate must distinguish them.
    """

    def __init__(self, *, keystore_encrypted: bool = False,
                 storage_encrypted: bool = False,
                 unlocked_password: Optional[str] = None,
                 connected: bool = True) -> None:
        self._keystore_encrypted = keystore_encrypted
        self._storage_encrypted = storage_encrypted
        self._unlocked_password = unlocked_password
        self.network = SimpleNamespace(asyncio_loop=None,
                                       is_connected=lambda: connected)

    def basename(self) -> str:
        return "test-wallet"

    def is_up_to_date(self) -> bool:
        return True

    def has_keystore_encryption(self) -> bool:
        return self._keystore_encrypted

    def has_password(self) -> bool:
        return self._keystore_encrypted or self._storage_encrypted

    def get_unlocked_password(self) -> Optional[str]:
        return self._unlocked_password

    # The user pressing Unlock in the wallet window.
    def unlock(self, password: str) -> None:
        self._unlocked_password = password


def _plugin(**config) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.unlock")
    p._tick_status = {}
    p.wallets = {}
    p.seen: List[str] = []
    p.on_status_changed = lambda wallet, status: p.seen.append(status)
    p._started_at = {}
    p._startup_grace_sec = 0.0          # readiness turns on connection/sync/unlock only
    cfg = dict(INBOUND_LIQUIDITY_MANUAL_RUN_ONLY=False)
    cfg.update(config)
    p.config = SimpleNamespace(**cfg)
    return p


# --- _signing_unlocked ------------------------------------------------------
def test_unencrypted_wallet_is_always_signable() -> None:
    p = _plugin()
    assert p._signing_unlocked(_FakeWallet()) is True


def test_encrypted_and_unlocked_wallet_is_signable() -> None:
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True, unlocked_password=PASSWORD)
    assert p._signing_unlocked(w) is True


def test_encrypted_and_locked_wallet_is_not_signable() -> None:
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)     # never unlocked
    assert p._signing_unlocked(w) is False


def test_storage_only_encryption_does_not_block_signing() -> None:
    """A wallet whose *file* is encrypted but whose keystore is not -- a hardware
    wallet, say -- signs without a password. Gating on has_password() would jam
    the plugin on such a wallet forever, so the gate reads keystore encryption."""
    p = _plugin()
    w = _FakeWallet(storage_encrypted=True)
    assert w.has_password() is True
    assert p._signing_unlocked(w) is True


def test_wallet_without_the_password_api_is_treated_as_unlocked() -> None:
    # Same reasoning as _wallet_synced: never stall forever on a wallet type
    # that has no such concept.
    p = _plugin()
    assert p._signing_unlocked(SimpleNamespace(basename=lambda: "odd")) is True


def test_wallet_that_cannot_answer_is_treated_as_locked() -> None:
    # It HAS a keystore and then failed to report -- refuse, that direction is
    # recoverable.
    def _boom() -> bool:
        raise RuntimeError("db gone")

    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    w.has_keystore_encryption = _boom  # type: ignore[method-assign]
    assert p._signing_unlocked(w) is False


# --- _get_password ----------------------------------------------------------
def test_password_is_none_for_an_unencrypted_wallet() -> None:
    p = _plugin()
    assert p._get_password(_FakeWallet()) is None


def test_password_comes_from_electrums_unlock_cache() -> None:
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    w.unlock(PASSWORD)                            # the Lock/Unlock button
    assert p._get_password(w) == PASSWORD


def test_locked_wallet_raises_rather_than_signing_with_none() -> None:
    # Backstop for direct callers: readiness normally defers long before here,
    # but a locked wallet must never fall through to an unsigned action.
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    with pytest.raises(Exception) as excinfo:
        p._get_password(w)
    assert "locked" in str(excinfo.value).lower()


def test_the_locked_message_tells_the_user_what_to_do() -> None:
    # The old message ("wallet requires password for channel open") named the
    # problem but not the fix, and no GUI path ever supplied that password.
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    with pytest.raises(Exception) as excinfo:
        p._get_password(w)
    assert "unlock" in str(excinfo.value).lower()


# --- readiness and status ---------------------------------------------------
def test_locked_wallet_reports_the_locked_readiness_block() -> None:
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    p._started_at[w] = 0.0
    assert p._readiness_block(w) == BLOCK_LOCKED
    assert p._wallet_ready(w) is False


def test_unlocking_clears_the_readiness_block() -> None:
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    p._started_at[w] = 0.0
    assert p._readiness_block(w) == BLOCK_LOCKED
    w.unlock(PASSWORD)
    assert p._readiness_block(w) is None


def test_manual_run_cannot_bypass_the_lock() -> None:
    # "Run now" skips the startup window only; it cannot conjure a password.
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    p._started_at[w] = 0.0
    assert p._readiness_block(w, manual=True) == BLOCK_LOCKED


def test_locked_wallet_rests_on_its_own_status_not_warming_up() -> None:
    """"Warming up" forever would be a lie: no amount of waiting unlocks a
    wallet."""
    p = _plugin()
    w = _FakeWallet(keystore_encrypted=True)
    p._started_at[w] = 0.0
    status = p._terminal_status(w, SimpleNamespace(automation_enabled=True),
                                manual=False)
    assert status == STATUS_LOCKED
    assert STATUS_LOCKED in TERMINAL_STATUSES


# --- the whole tick ---------------------------------------------------------
def _wire_tick(p) -> None:
    """Stub the tick's collaborators so only the gate is under test; each
    records whether it ran, which is what "defers before doing any work" means."""
    p._enforce_min_funding_floor = lambda: 0
    p.read_config = lambda: SimpleNamespace(automation_enabled=True)
    p.ran: List[str] = []
    p._reconcile_pending_swaps = lambda wallet: p.ran.append("swaps")
    p._maybe_pay_dev_fee = lambda wallet: p.ran.append("dev_fee")
    p._scan_channel_health = lambda wallet: p.ran.append("health")
    p._scan_offline_autoclose = lambda wallet: p.ran.append("offline")
    p.build_snapshot = lambda wallet, transport=None: SimpleNamespace()
    p._swap_may_be_needed = lambda base, config: False

    async def _run_decision(wallet, snapshot, config, transport):
        p.ran.append("decision")
    p._run_decision = _run_decision


def test_tick_on_a_locked_wallet_does_no_work_and_logs_no_error(caplog) -> None:
    """The reported symptom: an error on every tick. A locked wallet should now
    defer quietly, before the expensive part of the tick."""
    p, w = _plugin(), _FakeWallet(keystore_encrypted=True)
    p.wallets[w] = asyncio.Lock()
    p._started_at[w] = 0.0
    _wire_tick(p)

    with caplog.at_level(logging.WARNING):
        asyncio.run(p._evaluate(w))

    assert p.ran == []                                   # nothing was attempted
    assert p.tick_status(w) == STATUS_LOCKED
    assert "liquidity evaluation failed" not in caplog.text


def test_tick_runs_normally_once_the_wallet_is_unlocked() -> None:
    p, w = _plugin(), _FakeWallet(keystore_encrypted=True)
    p.wallets[w] = asyncio.Lock()
    p._started_at[w] = 0.0
    _wire_tick(p)

    asyncio.run(p._evaluate(w))
    assert p.ran == []

    w.unlock(PASSWORD)
    asyncio.run(p._evaluate(w))
    assert "decision" in p.ran
    assert p.tick_status(w) == STATUS_SLEEPING


def test_locking_again_re_blocks_the_next_tick() -> None:
    # The unlock cache is cleared by wallet.lock_wallet(); the gate is re-read
    # every tick rather than latched at load.
    p, w = _plugin(), _FakeWallet(keystore_encrypted=True,
                                  unlocked_password=PASSWORD)
    p.wallets[w] = asyncio.Lock()
    p._started_at[w] = 0.0
    _wire_tick(p)

    asyncio.run(p._evaluate(w))
    assert "decision" in p.ran

    p.ran.clear()
    w._unlocked_password = None                          # user pressed Lock
    asyncio.run(p._evaluate(w))
    assert p.ran == []
    assert p.tick_status(w) == STATUS_LOCKED
