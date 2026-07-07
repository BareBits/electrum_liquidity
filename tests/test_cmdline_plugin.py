"""Glue test for the headless (daemon / cmdline) entry point.

``cmdline.Plugin`` is what Electrum instantiates for a wallet loaded under the
daemon (the swap-partner wallet, and any ``--no-gui`` run). It carries no UI:
the two Electrum hooks just forward to the base plugin's start/stop. Trivial,
but wholly untested until now (0% coverage) -- and it is the ONLY code path that
attaches the manager to a wallet in headless mode, so a regression here silently
disables all automation for daemon users. Skipped outside the Electrum venv.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import cmdline  # type: ignore  # noqa: E402


def _plugin() -> "cmdline.Plugin":
    """A cmdline.Plugin without BasePlugin.__init__ (no network/config)."""
    return object.__new__(cmdline.Plugin)


def test_load_wallet_starts_manager() -> None:
    p = _plugin()
    started: list = []
    p.start_wallet = lambda wallet: started.append(wallet)   # type: ignore[method-assign]
    wallet = SimpleNamespace(name="w")
    # The daemon passes (wallet, window); window is unused headless.
    p.load_wallet(wallet, window=None)
    assert started == [wallet]


def test_close_wallet_stops_manager() -> None:
    p = _plugin()
    stopped: list = []
    p.stop_wallet = lambda wallet: stopped.append(wallet)    # type: ignore[method-assign]
    wallet = SimpleNamespace(name="w")
    p.close_wallet(wallet)
    assert stopped == [wallet]
