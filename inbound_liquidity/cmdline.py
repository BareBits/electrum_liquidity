# Headless (daemon / cmdline) entry point. No UI; the base class does all the
# work and reads its settings from config. `load_wallet` fires in the daemon via
# commands.py, so the manager attaches to the wallet here too.
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
