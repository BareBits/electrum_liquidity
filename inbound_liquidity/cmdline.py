# Headless (daemon / cmdline) entry point. No UI; the base class does all the
# work and reads each wallet's settings from that wallet (falling back to the
# installation-wide config -- see the per-wallet settings note in __init__.py,
# and the `inbound_liquidity_*` commands for setting them from here).
# `load_wallet` fires in the daemon via commands.py, so the manager attaches to
# the wallet here too.
from __future__ import annotations

from electrum.plugin import hook

from . import LiquidityPlugin


class Plugin(LiquidityPlugin):

    @hook
    def load_wallet(self, wallet, window) -> None:
        self.start_wallet(wallet)

    @hook
    def close_wallet(self, wallet) -> None:
        self.stop_wallet(wallet)
        # The settings whose effect is process-wide are derived from the open
        # wallets, so a closing wallet has to stop counting (same reason as the
        # Qt plugin's close_wallet).
        self.apply_process_wide_settings()
