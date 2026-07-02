"""Unit tests for the PURE startup/shutdown-readiness helpers in
liquidity_manager: ``is_wallet_ready`` (the all-automation deferral gate) and
``classify_peer_observation`` (the per-peer online/offline/not-observed gate that
stops a not-yet-connected peer at startup -- or a torn-down connection at
shutdown -- from being mistaken for a real outage). No Electrum, no clock."""
from __future__ import annotations

from liquidity_manager import (  # type: ignore  (added to sys.path by conftest)
    classify_peer_observation,
    is_wallet_ready,
)

GRACE = 120.0


# --- is_wallet_ready ------------------------------------------------------
def test_ready_only_when_connected_and_past_grace() -> None:
    assert is_wallet_ready(True, GRACE + 1, GRACE) is True
    assert is_wallet_ready(True, GRACE, GRACE) is True  # boundary is inclusive


def test_not_ready_within_grace() -> None:
    assert is_wallet_ready(True, 0.0, GRACE) is False
    assert is_wallet_ready(True, GRACE - 0.001, GRACE) is False


def test_not_ready_when_disconnected_regardless_of_time() -> None:
    # Covers both startup (server not connected yet) and shutdown (torn down).
    assert is_wallet_ready(False, 10_000.0, GRACE) is False


def test_zero_grace_ready_as_soon_as_connected() -> None:
    assert is_wallet_ready(True, 0.0, 0.0) is True
    assert is_wallet_ready(False, 0.0, 0.0) is False


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
