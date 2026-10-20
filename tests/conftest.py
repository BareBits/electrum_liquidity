"""Put the plugin package dir on sys.path so `liquidity_manager` (the pure rules
engine) imports without pulling in the package __init__ (which needs Electrum)."""
import os
import sys

import pytest

_PKG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "inbound_liquidity")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)


# --- configuring a plugin's wallets in a test -----------------------------
#
# Settings are per wallet: a wallet reads its own stored values, and the shipped
# default for anything it was never given. Nothing arrives from the process-wide
# config. But these tests are written to configure a PLUGIN -- one per test,
# handed a throwaway wallet or two -- so `p.config.INBOUND_LIQUIDITY_X = v` no
# longer configures anything.
#
# `seed_settings` bridges that: it makes every wallet the plugin sees start with
# the given values, which is the test equivalent of having set them on each
# wallet. One call in a test module's plugin factory therefore keeps every test
# in it configured as it was written.
#
# Values are written raw, without the type-checking `set_setting` does, because
# several tests deliberately seed garbage ("not-a-number") to pin that a
# hand-edited wallet file cannot abort a tick.
class SeededSettings(dict):
    """A plugin's settings cache in which every wallet starts seeded.

    Keyed internally by ``id(wallet)`` so it works for the unhashable
    ``SimpleNamespace`` wallets several of these modules use. (The production
    cache is keyed by the wallet itself and simply declines to cache an
    unhashable one -- which would bypass the seed and leave the test reading
    shipped defaults.)
    """

    def __init__(self, store) -> None:
        super().__init__()
        self._store = store

    def get(self, wallet, default=None):
        key = id(wallet)
        if key not in self:
            dict.__setitem__(self, key, dict(self._store))
        return dict.__getitem__(self, key)

    def __setitem__(self, wallet, value) -> None:
        dict.__setitem__(self, id(wallet), value)

    def pop(self, wallet, *default):
        return dict.pop(self, id(wallet), *default)


def seed_settings(plugin, **overrides):
    """Give every wallet of ``plugin`` the settings its fake config carries.

    Harvests every ``INBOUND_LIQUIDITY_*`` value off ``plugin.config`` -- which
    is how these tests express "the plugin is configured thus", baseline and
    overrides together -- and installs them as the store each of the plugin's
    wallets starts from. Extra keyword arguments are applied on top.

    Fake configs only (every caller builds one): on a real SimpleConfig this
    would harvest all 57 declared settings and hand them to every wallet, which
    is precisely the inheritance the production code no longer does.

    Names may be given with or without the ``INBOUND_LIQUIDITY_`` prefix.
    Anything that is not one of this plugin's settings is left alone -- the
    factories pass Electrum's own keys (FEE_POLICY, SWAPSERVER_URL, ...) through
    the same kwargs, and those still belong on the config. A name that LOOKS
    like one of ours but is not is rejected, though: a typo'd setting in a test
    is a test that proves nothing.
    """
    # Lazy: conftest is imported for the pure-engine tests too, which run
    # without Electrum.
    from electrum.plugins.inbound_liquidity import (  # type: ignore
        SETTINGS_STORE_VERSION, _CONFIG_PREFIX, _plugin_config_vars,
        _settings_field,
    )
    known = {_settings_field(name) for name in _plugin_config_vars()}
    store = {"_v": SETTINGS_STORE_VERSION}
    config = getattr(plugin, "config", None)
    for name in dir(config):
        if not name.startswith(_CONFIG_PREFIX):
            continue
        field = _settings_field(name)
        if field in known:
            store[field] = getattr(config, name)
    for name, value in overrides.items():
        field = _settings_field(name)
        if field not in known:
            assert not name.startswith(_CONFIG_PREFIX), \
                f"no such inbound-liquidity setting: {name!r}"
            continue        # one of Electrum's own; the factory set it already
        store[field] = value
    plugin._settings_cache = SeededSettings(store)
    return plugin


@pytest.fixture(autouse=True)
def _restore_min_funding_floor():
    """Put Electrum's channel-funding floor back after every test.

    ``MIN_FUNDING_SAT`` is a module constant -- one per process -- and the
    plugin lowers it to match the lowest ``min_onchain_to_open_sat`` across the
    open wallets. Any test that opens a wallet or writes that setting therefore
    moves a global, and before this each one leaked into whatever ran next:
    test_shipped_defaults asserting the shipped default is *below* the stock
    floor would fail because an earlier test had already lowered the floor to
    it.

    Autouse and global rather than opt-in per module, because the leak is not a
    property of the tests that notice it. ``_stock_min_funding_sat`` is restored
    too: it is the plugin's one-time capture of the pre-override value, and a
    test that reset it would otherwise leave the next one measuring against a
    lowered "stock".
    """
    try:
        import electrum.plugins.inbound_liquidity as IL  # type: ignore
    except Exception:
        yield                   # no Electrum here (the pure-engine tests)
        return
    names = list(IL._MIN_FUNDING_CORE_MODULES) + [
        "electrum.plugins.inbound_liquidity.liquidity_manager",
        "electrum.plugins.inbound_liquidity",
    ]
    saved = {n: getattr(sys.modules[n], "MIN_FUNDING_SAT")
             for n in names
             if sys.modules.get(n) is not None
             and hasattr(sys.modules[n], "MIN_FUNDING_SAT")}
    saved_stock = IL._stock_min_funding_sat
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(sys.modules[name], "MIN_FUNDING_SAT", value)
        IL._stock_min_funding_sat = saved_stock
