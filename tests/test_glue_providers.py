"""Glue-level tests for multi-provider selection (the Electrum-facing layer).

These exercise the plugin's translation of live nostr offers into the engine's
ProviderOffer list, the npub parsing, the "is a provider even needed" gate, and
that an executed swap is pointed at the chosen provider (update_pairs + the
transport's target pubkey). Heavy Electrum objects are faked; the test is
skipped if the plugin package cannot be imported (outside the electrum venv).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")
swap_transport = pytest.importorskip("electrum.plugins.inbound_liquidity.swap_transport")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    _parse_npub_set,
)
from electrum.plugins.inbound_liquidity.liquidity_manager import (  # type: ignore  # noqa: E402
    ChannelSnapshot,
    LiquidityConfig,
    LiquiditySnapshot,
    ReverseSwapAction,
)
from electrum.plugins.inbound_liquidity.swap_transport import (  # type: ignore  # noqa: E402
    TargetedNostrTransport,
)


def _plugin() -> LiquidityPlugin:
    """A LiquidityPlugin instance without BasePlugin.__init__ (no network)."""
    import logging
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.glue")
    p._last_offers = {}
    p._swap_cooldown_until = {}
    p._reverse_swap_timeout_sec = 30.0
    return p


def _config(**over) -> LiquidityConfig:
    base = dict(
        automation_enabled=True, min_onchain_to_open_sat=1_000_000,
        onchain_reserve_sat=10_000, max_channels=2, max_swap_fee_pct=0.6,
        swap_trigger_pct=25.0, swap_trigger_sat=25_000)
    base.update(over)
    return LiquidityConfig(**base)


def _channel(**over) -> ChannelSnapshot:
    base = dict(channel_id="aa" * 32, short_id="1x1x1", capacity_sat=2_000_000,
                local_sat=10_000, remote_sat=1_990_000, spendable_local_sat=10_000,
                is_active=True)
    base.update(over)
    return ChannelSnapshot(**base)


def _snapshot(channels, **over) -> LiquiditySnapshot:
    base = dict(onchain_spendable_sat=0, channels=tuple(channels),
                swap_percentage_fee=None, provider_max_reverse_sat=None,
                provider_min_amount_sat=None, swap_mining_fee_sat=0,
                swap_claim_fee_sat=0)
    base.update(over)
    return LiquiditySnapshot(**base)


# --- npub parsing ---------------------------------------------------------
def test_parse_npub_set_variants() -> None:
    assert _parse_npub_set("npub1, npub2") == frozenset({"npub1", "npub2"})
    assert _parse_npub_set("npub1\n npub2  npub3") == frozenset({"npub1", "npub2", "npub3"})
    assert _parse_npub_set("") == frozenset()
    assert _parse_npub_set(None) == frozenset()
    assert _parse_npub_set("  , npub1 ,, ") == frozenset({"npub1"})


# --- offer translation ----------------------------------------------------
def test_offers_from_transport_translation() -> None:
    # A client reverse swap is capped by the provider's max_forward, so
    # ProviderOffer.max_reverse_sat must be sourced from pairs.max_forward. The
    # decoy pairs.max_reverse below (a different value) must be ignored -- if the
    # glue regresses to reading it, this test fails.
    npub = "npub1" + "q" * 58  # valid npub shape (see clean_npub)
    offer = SimpleNamespace(
        server_npub=npub,
        pairs=SimpleNamespace(percentage=0.4, mining_fee=1234, min_amount=20_000,
                              max_forward=1_800_000, max_reverse=999_999),
        pow_bits=18)
    transport = SimpleNamespace(get_recent_offers=lambda: [offer])
    out = _plugin()._offers_from_transport(None, transport)
    assert len(out) == 1
    o = out[0]
    assert (o.npub, o.percentage_fee, o.mining_fee_sat, o.min_amount_sat,
            o.max_reverse_sat, o.pow_bits) == (npub, 0.4, 1234, 20_000, 1_800_000, 18)


def test_offers_from_transport_none_or_http() -> None:
    # None transport, or an HTTP transport with no get_recent_offers, yields [].
    p = _plugin()
    assert p._offers_from_transport(None, None) == []
    assert p._offers_from_transport(None, SimpleNamespace()) == []


def test_offers_from_transport_drops_and_faults_bad_offers() -> None:
    # A hostile provider cannot poison discovery: malformed / out-of-range offers
    # are dropped (never crash the batch), and an *identifiable* offender is
    # recorded as a soft reliability fault; an offer with an unusable identity is
    # dropped silently (nothing to attribute).
    good_npub = "npub1" + "q" * 58
    bad_npub_1 = "npub1" + "p" * 58
    bad_npub_2 = "npub1" + "r" * 58

    def _offer(npub, *, pct=0.3, mining=0, lo=200_000, mx=1_000_000):
        return SimpleNamespace(
            server_npub=npub, pow_bits=0,
            pairs=SimpleNamespace(percentage=pct, mining_fee=mining,
                                  min_amount=lo, max_forward=mx))

    good = _offer(good_npub)
    negative_fee = _offer(bad_npub_1, pct=-5.0)             # would look "free"
    non_numeric = _offer(bad_npub_2, pct="not-a-number")    # would raise on int/float
    dup_bad = _offer(bad_npub_1, mining=-1)                 # same id, still bad
    no_identity = _offer("bad id", pct=0.3)                # unusable npub

    transport = SimpleNamespace(
        get_recent_offers=lambda: [good, negative_fee, non_numeric, dup_bad, no_identity])

    p = _plugin()
    faults = []
    p._record_provider_fault = lambda wallet, npub, reason, **kw: faults.append((npub, kw))
    wallet = object()

    out = p._offers_from_transport(wallet, transport)

    # Only the sane offer survives; a single bad offer never takes out the rest.
    assert [o.npub for o in out] == [good_npub]
    # Each identifiable-but-bad provider is faulted exactly once per pass (the
    # duplicate bad offer under bad_npub_1 does not amplify into a second fault);
    # the good provider is not faulted, and the unidentifiable offer records none.
    faulted_npubs = [npub for npub, _ in faults]
    assert sorted(faulted_npubs) == sorted([bad_npub_1, bad_npub_2])
    assert good_npub not in faulted_npubs
    assert all(kw.get("soft") for _, kw in faults)


def test_offers_from_transport_survives_raising_feed() -> None:
    # A transport whose offer feed itself raises must not propagate the error.
    def _boom():
        raise RuntimeError("relay exploded")
    transport = SimpleNamespace(get_recent_offers=_boom)
    assert _plugin()._offers_from_transport(None, transport) == []


# --- provider-needed gate -------------------------------------------------
def test_swap_may_be_needed_gate() -> None:
    cfg = _config()
    over = _channel(local_sat=900_000, spendable_local_sat=900_000)
    under = _channel(local_sat=1_000, spendable_local_sat=1_000)
    assert LiquidityPlugin._swap_may_be_needed(_snapshot([over]), cfg) is True
    assert LiquidityPlugin._swap_may_be_needed(_snapshot([under]), cfg) is False
    # Inactive channels never need a provider.
    assert LiquidityPlugin._swap_may_be_needed(
        _snapshot([_channel(local_sat=900_000, spendable_local_sat=900_000, is_active=False)]),
        cfg) is False
    # Frozen (pending/inflight) suppresses the need.
    assert LiquidityPlugin._swap_may_be_needed(
        _snapshot([over], pending_channel_count=1), cfg) is False


# --- read_config parses the lists -----------------------------------------
def test_read_config_parses_provider_lists() -> None:
    p = _plugin()
    p.config = SimpleNamespace(
        INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
        INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT=1_000_000,
        INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT=10_000,
        INBOUND_LIQUIDITY_MAX_CHANNELS=2,
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.6,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT=25_000,
        INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT=0,
        INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY=False,
        INBOUND_LIQUIDITY_PREFERRED_NPUBS="npubA, npubB",
        INBOUND_LIQUIDITY_BANNED_NPUBS="npubC",
    )
    cfg = p.read_config()
    assert cfg.preferred_npubs == frozenset({"npubA", "npubB"})
    assert cfg.banned_npubs == frozenset({"npubC"})
    assert cfg.min_outbound_sat == 0
    assert cfg.manage_plugin_opened_only is False


# --- _reverse_swap points at the chosen provider --------------------------
def test_reverse_swap_targets_chosen_provider() -> None:
    p = _plugin()
    p.config = SimpleNamespace(SWAPSERVER_NPUB=None, SWAPSERVER_URL=None)

    update_pairs_calls = []
    reverse_swap_calls = []

    sm = SimpleNamespace(
        mining_fee=1_000,
        is_initialized=asyncio.Event(),
        update_pairs=lambda pairs: update_pairs_calls.append(pairs),
        get_recv_amount=lambda amt, *, is_reverse: amt - 500,
    )
    sm.is_initialized.set()

    async def _reverse_swap(*, transport, lightning_amount_sat,
                            expected_onchain_amount_sat, prepayment_sat):
        reverse_swap_calls.append(dict(
            transport=transport, lightning_amount_sat=lightning_amount_sat,
            expected_onchain_amount_sat=expected_onchain_amount_sat,
            prepayment_sat=prepayment_sat))
        return "funding-txid-123"
    sm.reverse_swap = _reverse_swap

    wallet = SimpleNamespace(lnworker=SimpleNamespace(swap_manager=sm))

    offer = SimpleNamespace(server_pubkey="abcd1234", pairs=SimpleNamespace())
    transport = SimpleNamespace(
        target_pubkey=None,
        get_offer=lambda npub: offer if npub == "npubCHOSEN" else None)

    # Capture the logged action rather than touching wallet.db.
    logged = {}
    p._log_action = lambda wallet, **kw: logged.update(kw)
    p.on_action_done = lambda wallet, msg: None

    action = ReverseSwapAction(channel_id="aa" * 32, short_id="1x1x1",
                               lightning_amount_sat=400_000, reason="drain",
                               provider_npub="npubCHOSEN")
    asyncio.run(p._reverse_swap(wallet, action, state={}, transport=transport))

    # The chosen provider's terms were loaded and the RPC aimed at its pubkey.
    assert update_pairs_calls == [offer.pairs]
    assert transport.target_pubkey == "abcd1234"
    assert len(reverse_swap_calls) == 1
    call = reverse_swap_calls[0]
    assert call["transport"] is transport
    assert call["lightning_amount_sat"] == 400_000
    assert call["expected_onchain_amount_sat"] == 399_500   # get_recv_amount
    assert call["prepayment_sat"] == 2_000                  # 2 * mining_fee
    assert "npubCHOSEN" in logged["detail"]


def test_reverse_swap_skips_when_offer_gone() -> None:
    p = _plugin()
    p.config = SimpleNamespace(SWAPSERVER_NPUB=None, SWAPSERVER_URL=None)
    called = []
    sm = SimpleNamespace(reverse_swap=lambda **kw: called.append(kw))
    wallet = SimpleNamespace(lnworker=SimpleNamespace(swap_manager=sm))
    transport = SimpleNamespace(get_offer=lambda npub: None)  # provider vanished
    p._log_action = lambda *a, **k: called.append("logged")
    action = ReverseSwapAction(channel_id="bb" * 32, short_id="2x2x2",
                               lightning_amount_sat=400_000, reason="drain",
                               provider_npub="gone")
    asyncio.run(p._reverse_swap(wallet, action, state={}, transport=transport))
    assert called == []  # nothing executed, nothing logged


# --- transport routing ----------------------------------------------------
def test_targeted_transport_routes_to_target_pubkey() -> None:
    # Bypass NostrTransport.__init__ (needs a config/keypair); set just what
    # send_request_to_server touches.
    t = object.__new__(TargetedNostrTransport)
    import logging
    t.logger = logging.getLogger("test.targeted")
    t.dm_replies = {}
    # A 64-hex pubkey (the real shape of SwapOffer.server_pubkey); the transport
    # now refuses to address a DM to anything else (see looks_like_hex_pubkey).
    target = "ab" * 32
    t.target_pubkey = target

    sent = {}

    async def fake_send_dm(pubkey, content, *, retries=0):
        sent["pubkey"] = pubkey
        sent["content"] = content
        return "evt-1"
    t.send_direct_message = fake_send_dm

    async def run():
        task = asyncio.ensure_future(
            t.send_request_to_server("createswap", {"foo": "bar"}))
        # Let the request register its reply future, then resolve it.
        await asyncio.sleep(0)
        fut = t.dm_replies[(target, "evt-1")]
        fut.set_result({"ok": True})
        return await task

    resp = asyncio.run(run())
    assert resp == {"ok": True}
    assert sent["pubkey"] == target           # addressed to the chosen provider
    assert '"method": "createswap"' in sent["content"]


def test_targeted_transport_refuses_malformed_pubkey() -> None:
    # A provider whose advertised server_pubkey is not a 64-hex value must never
    # be addressed: the RPC fails cleanly (SwapServerError) and no DM is sent.
    from electrum.submarine_swaps import SwapServerError  # type: ignore

    t = object.__new__(TargetedNostrTransport)
    import logging
    t.logger = logging.getLogger("test.targeted.bad")
    t.dm_replies = {}
    t.target_pubkey = "not-a-hex-pubkey"

    sent = []

    async def fake_send_dm(pubkey, content, *, retries=0):
        sent.append(pubkey)
        return "evt-x"
    t.send_direct_message = fake_send_dm

    with pytest.raises(SwapServerError):
        asyncio.run(t.send_request_to_server("createswap", {"foo": "bar"}))
    assert sent == []  # never addressed a DM to the malformed pubkey
