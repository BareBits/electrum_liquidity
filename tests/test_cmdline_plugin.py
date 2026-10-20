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

from conftest import seed_settings  # noqa: E402

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import cmdline  # type: ignore  # noqa: E402


def _plugin() -> "cmdline.Plugin":
    """A cmdline.Plugin without BasePlugin.__init__ (no network/parent).

    It does get a logger and a (bare) config: closing a wallet re-derives the
    settings whose effect is process-wide, which reads the installation-wide
    config and logs if it cannot.
    """
    import logging
    p = object.__new__(cmdline.Plugin)
    p.logger = logging.getLogger("test.inbound_liquidity.cmdline")
    p.config = SimpleNamespace()
    # Settings are per wallet now; hand this plugin's wallets what its
    # config carries -- see conftest.seed_settings.
    seed_settings(p)
    return p


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


def test_closing_a_wallet_stops_it_setting_the_process_wide_answers() -> None:
    """Headless counterpart of the Qt plugin's close: log capture is one handler
    set per process, drawn from the open wallets, so a closed wallet must stop
    contributing."""
    p = _plugin()

    class _DB:
        def __init__(self): self._d = {}
        def get(self, k, d=None): return self._d.get(k, d)
        def put(self, k, v): self._d[k] = v

    class _Wallet:
        def __init__(self): self.db = _DB()
        def save_db(self): pass

    wallet = _Wallet()
    p.set_setting(wallet, "INBOUND_LIQUIDITY_LOG_CAPTURE_LN", True)
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_LN") is True

    # Only the part of the teardown this is about (the full stop_wallet wants
    # evaluation/heartbeat state this hand-built plugin does not have).
    p.stop_wallet = p._forget_wallet
    p.close_wallet(wallet)
    assert p._any_wallet_wants("INBOUND_LIQUIDITY_LOG_CAPTURE_LN") is False
