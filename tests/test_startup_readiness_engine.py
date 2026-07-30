"""Unit tests for the PURE startup/shutdown-readiness helpers in
liquidity_manager: ``wallet_readiness_block`` / ``is_wallet_ready`` (the
all-automation deferral gate) and ``classify_peer_observation`` (the per-peer
online/offline/not-observed gate that stops a not-yet-connected peer at startup
-- or a torn-down connection at shutdown -- from being mistaken for a real
outage). No Electrum, no clock."""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    BLOCK_LOCKED,
    BLOCK_NO_CONNECTION,
    BLOCK_STARTING_UP,
    BLOCK_SYNCING,
    classify_peer_observation,
    is_wallet_ready,
    wallet_readiness_block,
)

GRACE = 120.0

# Positional order: connected, synced, elapsed, grace.


# --- is_wallet_ready: the startup window ----------------------------------
def test_ready_once_the_startup_window_has_elapsed() -> None:
    assert is_wallet_ready(True, True, GRACE + 1, GRACE) is True
    assert is_wallet_ready(True, True, GRACE, GRACE) is True  # boundary inclusive


def test_not_ready_inside_the_startup_window() -> None:
    # Deliberately a plain stopwatch: ending this window early on a
    # "peers are connected" signal made reverse swaps fail (their Lightning leg
    # cannot route yet). See the note in wallet_readiness_block.
    assert is_wallet_ready(True, True, 0.0, GRACE) is False
    assert is_wallet_ready(True, True, GRACE - 0.001, GRACE) is False


# --- is_wallet_ready: the connection and sync limbs -----------------------
def test_not_ready_when_disconnected_regardless_of_time() -> None:
    # Covers both startup (server not connected yet) and shutdown (torn down).
    assert is_wallet_ready(False, True, 10_000.0, GRACE) is False


def test_not_ready_while_wallet_is_still_syncing() -> None:
    # A partial UTXO set would make a balance-driven decision wrong, so this
    # blocks even with the startup window long past.
    assert is_wallet_ready(True, False, 10_000.0, GRACE) is False


def test_zero_grace_ready_as_soon_as_connected_and_synced() -> None:
    assert is_wallet_ready(True, True, 0.0, 0.0) is True
    assert is_wallet_ready(False, True, 0.0, 0.0) is False
    assert is_wallet_ready(True, False, 0.0, 0.0) is False


# --- is_wallet_ready: the manual ("Run now") bypass ------------------------
def test_manual_run_skips_the_startup_window() -> None:
    # Freshly loaded: an automatic tick defers, a user-initiated run goes ahead.
    assert is_wallet_ready(True, True, 0.0, GRACE) is False
    assert is_wallet_ready(True, True, 0.0, GRACE, manual=True) is True


def test_manual_run_still_requires_connection_and_sync() -> None:
    assert is_wallet_ready(False, True, 10_000.0, GRACE, manual=True) is False
    assert is_wallet_ready(True, False, 10_000.0, GRACE, manual=True) is False


# --- wallet_readiness_block: the reported reason --------------------------
def test_block_reason_names_the_failing_limb() -> None:
    assert wallet_readiness_block(True, True, GRACE + 1, GRACE) is None
    assert wallet_readiness_block(False, True, 0.0, GRACE) == BLOCK_NO_CONNECTION
    assert wallet_readiness_block(True, False, 0.0, GRACE) == BLOCK_SYNCING
    assert wallet_readiness_block(True, True, 0.0, GRACE) == BLOCK_STARTING_UP


def test_block_reason_reports_connection_before_sync() -> None:
    # Both broken -> report the one the user must fix first.
    assert wallet_readiness_block(False, False, 0.0, GRACE) == BLOCK_NO_CONNECTION


# --- the locked-wallet limb ------------------------------------------------
def test_locked_wallet_blocks_all_automation() -> None:
    # Connected, synced, startup window long past: the only thing missing is the
    # password, and without it nothing that moves money can be signed.
    assert wallet_readiness_block(True, True, 10_000.0, GRACE,
                                  wallet_unlocked=False) == BLOCK_LOCKED
    assert is_wallet_ready(True, True, 10_000.0, GRACE, wallet_unlocked=False) is False


def test_locked_wallet_blocks_manual_runs_too() -> None:
    # Unlike the startup window, "Run now" cannot bypass this one -- the user
    # being present does not put a password in the cache.
    assert wallet_readiness_block(True, True, 10_000.0, GRACE, manual=True,
                                  wallet_unlocked=False) == BLOCK_LOCKED
    assert is_wallet_ready(True, True, 0.0, 0.0, manual=True,
                           wallet_unlocked=False) is False


def test_locked_is_reported_after_connection_and_sync() -> None:
    # A locked *and* disconnected wallet reports the connection first: unlocking
    # would not have helped, and the user should fix the outer problem first.
    assert wallet_readiness_block(False, True, 0.0, GRACE,
                                  wallet_unlocked=False) == BLOCK_NO_CONNECTION
    assert wallet_readiness_block(True, False, 0.0, GRACE,
                                  wallet_unlocked=False) == BLOCK_SYNCING


def test_locked_is_reported_before_the_startup_window() -> None:
    # Both apply inside the grace: name the one that will not clear on its own.
    assert wallet_readiness_block(True, True, 0.0, GRACE,
                                  wallet_unlocked=False) == BLOCK_LOCKED


def test_unlocked_defaults_to_true_so_the_gate_is_opt_in() -> None:
    # Callers that never pass the flag (and every wallet without a password)
    # behave exactly as before this limb existed.
    assert wallet_readiness_block(True, True, GRACE + 1, GRACE) is None
    assert wallet_readiness_block(True, True, GRACE + 1, GRACE,
                                  wallet_unlocked=True) is None


# --- classify_peer_observation --------------------------------------------
def test_active_peer_is_always_online() -> None:
    # Active reads online even inside the grace / never-seen-before.
    assert classify_peer_observation(True, False, True, 0.0, GRACE) is True
    assert classify_peer_observation(True, False, False, 0.0, GRACE) is True


def test_network_down_is_not_observed() -> None:
    # Inactive + no server -> we cannot attribute the outage to the peer.
    assert classify_peer_observation(False, True, False, 10_000.0, GRACE) is None
    assert classify_peer_observation(False, False, False, 10_000.0, GRACE) is None


def test_unseen_peer_within_grace_is_not_observed() -> None:
    # The core startup race: peer not yet dialed, still inside the grace.
    assert classify_peer_observation(False, False, True, 0.0, GRACE) is None
    assert classify_peer_observation(False, False, True, GRACE - 1, GRACE) is None


def test_unseen_peer_past_grace_is_offline() -> None:
    # Given a fair chance to connect and it never did -> genuine offline.
    assert classify_peer_observation(False, False, True, GRACE, GRACE) is False
    assert classify_peer_observation(False, False, True, GRACE + 100, GRACE) is False


def test_seen_peer_is_offline_immediately_even_within_grace() -> None:
    # Once we've seen it online this session, a later drop is a real outage --
    # no grace applies (the not-connected-yet excuse no longer holds).
    assert classify_peer_observation(False, True, True, 0.0, GRACE) is False


def test_zero_grace_unseen_inactive_is_offline() -> None:
    # With grace disabled, an inactive unseen peer is offline right away.
    assert classify_peer_observation(False, False, True, 0.0, 0.0) is False
