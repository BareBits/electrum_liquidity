"""Unit tests for the PURE startup/shutdown-readiness helpers in
liquidity_manager: ``wallet_readiness_block`` / ``is_wallet_ready`` (the
all-automation deferral gate) and ``classify_peer_observation`` (the per-peer
online/offline/not-observed gate that stops a not-yet-connected peer at startup
-- or a torn-down connection at shutdown -- from being mistaken for a real
outage). No Electrum, no clock."""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    BLOCK_NO_CONNECTION,
    BLOCK_PEERS,
    BLOCK_SYNCING,
    classify_peer_observation,
    is_wallet_ready,
    wallet_readiness_block,
)

GRACE = 120.0

# Positional order: connected, synced, all_peers_observed, elapsed, grace.


# --- is_wallet_ready: the peer/time limb ----------------------------------
def test_ready_once_every_peer_is_observed_without_waiting() -> None:
    # The point of the fix: peers dialed 1s after load -> ready immediately,
    # no need to sit out the grace ceiling.
    assert is_wallet_ready(True, True, True, 1.0, GRACE) is True


def test_ready_past_grace_even_with_peers_unobserved() -> None:
    # The ceiling: a peer that never comes back must not defer forever.
    assert is_wallet_ready(True, True, False, GRACE + 1, GRACE) is True
    assert is_wallet_ready(True, True, False, GRACE, GRACE) is True  # inclusive


def test_not_ready_within_grace_while_peers_unobserved() -> None:
    assert is_wallet_ready(True, True, False, 0.0, GRACE) is False
    assert is_wallet_ready(True, True, False, GRACE - 0.001, GRACE) is False


# --- is_wallet_ready: the connection and sync limbs -----------------------
def test_not_ready_when_disconnected_regardless_of_everything_else() -> None:
    # Covers both startup (server not connected yet) and shutdown (torn down).
    assert is_wallet_ready(False, True, True, 10_000.0, GRACE) is False


def test_not_ready_while_wallet_is_still_syncing() -> None:
    # A partial UTXO set would make a balance-driven decision wrong, so this
    # blocks even with peers observed and the grace long past.
    assert is_wallet_ready(True, False, True, 10_000.0, GRACE) is False


def test_zero_grace_ready_as_soon_as_connected_and_synced() -> None:
    assert is_wallet_ready(True, True, False, 0.0, 0.0) is True
    assert is_wallet_ready(False, True, False, 0.0, 0.0) is False
    assert is_wallet_ready(True, False, False, 0.0, 0.0) is False


# --- is_wallet_ready: the manual ("Run now") bypass ------------------------
def test_manual_run_skips_the_peer_and_time_limb() -> None:
    # Freshly loaded, peers not dialed yet: an automatic tick defers, but a
    # user-initiated run goes ahead.
    assert is_wallet_ready(True, True, False, 0.0, GRACE) is False
    assert is_wallet_ready(True, True, False, 0.0, GRACE, manual=True) is True


def test_manual_run_still_requires_connection_and_sync() -> None:
    assert is_wallet_ready(False, True, True, 10_000.0, GRACE, manual=True) is False
    assert is_wallet_ready(True, False, True, 10_000.0, GRACE, manual=True) is False


# --- wallet_readiness_block: the reported reason --------------------------
def test_block_reason_names_the_failing_limb() -> None:
    assert wallet_readiness_block(True, True, True, 0.0, GRACE) is None
    assert wallet_readiness_block(False, True, True, 0.0, GRACE) == BLOCK_NO_CONNECTION
    assert wallet_readiness_block(True, False, True, 0.0, GRACE) == BLOCK_SYNCING
    assert wallet_readiness_block(True, True, False, 0.0, GRACE) == BLOCK_PEERS


def test_block_reason_reports_connection_before_sync() -> None:
    # Both broken -> report the one the user must fix first.
    assert wallet_readiness_block(False, False, False, 0.0, GRACE) == BLOCK_NO_CONNECTION


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
