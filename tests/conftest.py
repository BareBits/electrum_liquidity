"""Put the plugin package dir on sys.path so `liquidity_manager` (the pure rules
engine) imports without pulling in the package __init__ (which needs Electrum)."""
import os
import sys

_PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "inbound_liquidity")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)
