"""End-to-end tests that a swap provider's human-readable name is read off a
REAL nostr relay and attributed to the right provider.

The unit/glue tests fake the relay manager, so what they cannot prove is the
part that has to interoperate: that a kind-0 metadata event published by the
provider itself -- signed with the very key it signs its swap offers with -- is
found by the plugin's query on a live relay, parsed, and landed on that
provider's row and no one else's.

So these drive the rig (bitcoind + Fulcrum + nostr-rs-relay + the client daemon
with the plugin + two partners running real swapservers) and publish a profile
as each partner. The providers' nostr identities are derived the way Electrum
derives them -- BIP32 ``LnKeyFamily.NOSTR_KEY`` over the wallet's lightning
xprv, see ``rig.services.provider_nostr_keys`` -- so these really are the
providers' own profiles rather than a third party's.

TWO providers throughout, because the interesting failure is a lookup that
returns A's name for B; with one provider on the relay that bug is invisible.

Two levels of proof:

* ``_lookup_names`` runs the plugin's own ``_refresh_provider_profiles``
  in-process against the rig's live relay. Fast and deterministic: it exercises
  the real query, aionostr's real signature verification, the real event
  handling and the real cache write.
* ``test_the_daemons_own_tick_caches_the_names`` asserts the plugin running
  INSIDE the client daemon writes those names to its wallet file on an ordinary
  tick -- the call site, not just the function.

Needs the electrum venv + docker. Function-scoped rig (wipes .run, kills any
previous rig), so it must NOT run while a manual run.py rig is up. Gated behind
RUN_RIG_E2E=1.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_provider_profile_e2e.py -q -s
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from types import SimpleNamespace
from typing import Callable, Dict, Sequence

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    PARTNER,
    PARTNER2,
    discover_swap_providers,
    electrum_cli,
    mine,
    provider_nostr_keys,
    publish_provider_profile,
    set_client_plugin_setting,
    wait_wallet_height,
    wallet_path,
)

# What each partner publishes. PARTNER2's carries hostile padding on purpose:
# the sanitiser has its own unit tests, but only this proves a hostile name
# survives the whole round trip in its CLEANED form rather than reaching the
# wallet file raw. U+202E is a bidi override (it renders what follows reversed)
# and U+200B a zero-width space -- the two tricks for making one provider's name
# render as another's. The zero-width one sits INSIDE a word deliberately: that
# is the attack (two names that render alike but differ in their bytes), and
# deleting it rather than turning it into a space is what the sanitiser owes us.
PARTNER_NAME = "Rig Swap Partner"
PARTNER2_RAW_NAME = "Second‮ Rig​Provider\n"
PARTNER2_CLEAN_NAME = "Second RigProvider"

PROFILES_DB_KEY = "inbound_liquidity_provider_profiles"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _setcfg(key: str, value: str) -> None:
    """Configure the plugin (its settings live in the client WALLET now) or, for
    one of Electrum's own keys, the client config."""
    prefix = "plugins.inbound_liquidity."
    if key.startswith(prefix):
        set_client_plugin_setting(key[len(prefix):], value)
        return
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _npub_of(inst) -> str:
    """``inst``'s provider npub -- the identity the client discovers it by."""
    from electrum_aionostr.util import to_nip19
    pubkey_hex, _nsec = provider_nostr_keys(inst)
    return to_nip19('npub', pubkey_hex)


def _relay_url(rig) -> str:
    """The rig's nostr relay URL.

    Taken off the live rig object rather than ``.run/ready.json``: that manifest
    is written by ``run.py``'s own main loop, and these tests drive ``Rig``
    directly (``preflight``/``allocate``/``bring_up``), so it does not exist.
    """
    return rig.ep.nostr_relay_url


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)
        time.sleep(period)
    return cond()


def _lookup_names(rig, npubs: Sequence[str], *,
                  timeout: float = 60.0) -> Dict[str, str]:
    """Run the plugin's real profile lookup against the rig's live relay.

    The daemon holds the client wallet open, so instead of competing with it for
    that file this drives ``_refresh_provider_profiles`` over a local
    wallet-shaped object. Everything that matters is the real thing: the relay,
    the kind-0 query, aionostr's signature verification, the event handling, the
    sanitiser and the cache write. Only the wallet underneath is local.
    """
    import electrum_aionostr as aionostr
    from electrum.plugins.inbound_liquidity import LiquidityPlugin  # type: ignore

    class _DB:
        def __init__(self) -> None:
            self.data: Dict[str, object] = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def put(self, key, value) -> None:
            self.data[key] = value

    plugin = object.__new__(LiquidityPlugin)
    plugin.logger = logging.getLogger("e2e.provider_profiles")
    plugin._last_offers = {}
    wallet = SimpleNamespace(db=_DB())
    # The shape `_offers_from_transport` reads: a nostr SwapOffer per provider.
    fake_offers = [
        SimpleNamespace(
            server_npub=npub, pow_bits=0,
            pairs=SimpleNamespace(percentage=0.5, mining_fee=1_000,
                                  min_amount=20_000, max_forward=500_000))
        for npub in npubs]

    async def _go() -> None:
        manager = aionostr.Manager([_relay_url(rig)])
        await manager.connect()
        try:
            transport = SimpleNamespace(
                relay_manager=manager,
                get_recent_offers=lambda: list(fake_offers))
            await plugin._refresh_provider_profiles(wallet, transport, force=True)
        finally:
            await manager.close()

    asyncio.run(asyncio.wait_for(_go(), timeout=timeout))
    return plugin.provider_names(wallet)


def _daemon_cached_profiles() -> Dict[str, Dict]:
    """The client wallet's provider-name cache, as the daemon persisted it."""
    with open(wallet_path(CLIENT)) as fh:
        raw = json.load(fh).get(PROFILES_DB_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _both_npubs_discovered() -> tuple[str, str]:
    """Assert the rig's two providers are advertising, and return their npubs.

    The premise of every test here: a name is only interesting if it attaches to
    an identity the client actually discovered over nostr.
    """
    providers = discover_swap_providers(CLIENT, log=_log, min_providers=2)
    npub_a, npub_b = _npub_of(PARTNER), _npub_of(PARTNER2)
    assert npub_a in providers, (npub_a, list(providers))
    assert npub_b in providers, (npub_b, list(providers))
    return npub_a, npub_b


@pytest.fixture
def rig():
    """A rig with TWO swap providers, so a name can be misattributed."""
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui", "--second-provider"]))
    r.preflight()
    r.allocate()
    r.bring_up()
    try:
        yield r
    finally:
        r.shutdown()


def test_each_providers_published_name_lands_on_that_provider(rig) -> None:
    npub_a, npub_b = _both_npubs_discovered()

    # Nothing has published a profile yet. This is the state the plugin meets on
    # a fresh relay, and it must be a clean "no names" rather than an error.
    assert _lookup_names(rig, [npub_a, npub_b]) == {}

    publish_provider_profile(
        rig.ep, PARTNER, {"display_name": PARTNER_NAME}, log=_log)
    publish_provider_profile(
        rig.ep, PARTNER2, {"display_name": PARTNER2_RAW_NAME}, log=_log)

    # nostr-rs-relay indexes a replaceable kind-0 promptly, but poll rather than
    # race its write.
    names: Dict[str, str] = {}

    def _both_named() -> bool:
        names.update(_lookup_names(rig, [npub_a, npub_b]))
        return npub_a in names and npub_b in names

    assert _wait_until(_both_named, rig=rig, timeout=90), \
        f"names never resolved off the relay: {names!r}"
    assert names[npub_a] == PARTNER_NAME, names
    assert names[npub_b] == PARTNER2_CLEAN_NAME, names


def test_a_hostile_name_is_sanitised_before_it_is_cached(rig) -> None:
    _npub_a, npub_b = _both_npubs_discovered()
    publish_provider_profile(
        rig.ep, PARTNER2, {"display_name": PARTNER2_RAW_NAME}, log=_log)

    resolved: Dict[str, str] = {}

    def _named() -> bool:
        resolved.update(_lookup_names(rig, [npub_b]))
        return npub_b in resolved

    assert _wait_until(_named, rig=rig, timeout=90), "name never resolved"
    name = resolved[npub_b]
    # Asserted on the value as STORED, because that is what the table renders.
    assert name == PARTNER2_CLEAN_NAME, repr(name)
    assert "‮" not in name          # bidi override gone
    assert "​" not in name          # zero-width space gone
    assert "\n" not in name and name == name.strip()


def test_a_provider_with_no_profile_does_not_inherit_the_other_name(rig) -> None:
    # Only PARTNER publishes. PARTNER2 must come back nameless rather than pick
    # up the one name sitting on the relay.
    npub_a, npub_b = _both_npubs_discovered()
    publish_provider_profile(
        rig.ep, PARTNER, {"display_name": PARTNER_NAME}, log=_log)

    names: Dict[str, str] = {}

    def _a_named() -> bool:
        names.clear()
        names.update(_lookup_names(rig, [npub_a, npub_b]))
        return npub_a in names

    assert _wait_until(_a_named, rig=rig, timeout=90), f"no name resolved: {names!r}"
    assert names[npub_a] == PARTNER_NAME, names
    assert npub_b not in names, names


def test_the_daemons_own_tick_caches_the_names(rig) -> None:
    """The plugin inside the daemon fills the cache on an ordinary tick.

    This is the call site rather than the function: the gap-fill runs whenever an
    evaluation opens a nostr session, which happens as soon as a channel is over
    a swap trigger -- independently of whether the swap then goes ahead. So the
    trigger is set permissively and the fee ceiling tightly: a session opens
    every tick, the names get filled, and the swap itself is declined on cost.
    That keeps this test about the names instead of about completing a swap.
    """
    npub_a, npub_b = _both_npubs_discovered()
    publish_provider_profile(
        rig.ep, PARTNER, {"display_name": PARTNER_NAME}, log=_log)
    publish_provider_profile(
        rig.ep, PARTNER2, {"display_name": PARTNER2_RAW_NAME}, log=_log)

    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "1000")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "1")
    _setcfg("plugins.inbound_liquidity.max_swap_fee_pct", "0.001")  # decline
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "0")
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(
        lambda: {npub_a, npub_b} <= set(_daemon_cached_profiles()),
        rig=rig, timeout=300), \
        f"daemon never cached both providers: {_daemon_cached_profiles()!r}"

    cached = _daemon_cached_profiles()
    assert cached[npub_a]["name"] == PARTNER_NAME, cached
    assert cached[npub_b]["name"] == PARTNER2_CLEAN_NAME, cached
    # Every entry is stamped, which is what stops the next tick re-querying.
    assert all(row["checked_ts"] > 0 for row in cached.values()), cached
