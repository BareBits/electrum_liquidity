"""The transport must follow the *chosen* provider's relays.

Stock ``NostrTransport.update_relays`` resolves the provider as
``config.SWAPSERVER_NPUB`` only. The plugin is multi-provider and leaves that
config unset, so every time it pointed the swap math at a provider
(``sm.update_pairs``) the stock loop woke, looked up ``self._offers[None]``, found
nothing, and logged::

    pairs updated but no pair for self.config.SWAPSERVER_NPUB=None available?

with a stack trace -- once per swap evaluation. That noise was the visible half.
The silent half mattered more: ``_last_swapserver_relays`` never updated, so the
client never joined the relays the chosen provider announced (the client's relay
set is ``NOSTR_RELAYS | _last_swapserver_relays``). A provider whose reply DMs go
out on relays we are not subscribed to looks, from our side, exactly like a swap
that was accepted and then never funded.

``TargetedNostrTransport.update_relays`` resolves "chosen, else configured" and
stays quiet when neither is known.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import List, Optional, Sequence

import pytest

pytest.importorskip("electrum.submarine_swaps")

from electrum.plugins.inbound_liquidity.swap_transport import (  # type: ignore  # noqa: E402
    TargetedNostrTransport,
)

CHOSEN = "npubCHOSEN"
CONFIGURED = "npubCONFIGURED"


def _offer(relays: Sequence[str]) -> SimpleNamespace:
    return SimpleNamespace(relays=list(relays))


def _transport(*, offers, swapserver_npub=None, our_relays="wss://ours") -> TargetedNostrTransport:
    """A transport assembled field-by-field: the real __init__ wants a network,
    an ssl context and a running asyncio loop, none of which this loop touches."""
    t = object.__new__(TargetedNostrTransport)
    t.target_pubkey = None
    t.target_npub = None
    t.NO_PROVIDER_BACKOFF_SEC = 0.0     # keep the no-provider path instant in tests
    t.config = SimpleNamespace(SWAPSERVER_NPUB=swapserver_npub, NOSTR_RELAYS=our_relays)
    t.sm = SimpleNamespace(is_server=False, pairs_updated=asyncio.Event())
    t._offers = dict(offers)
    t._last_swapserver_relays = None
    t.logger = logging.getLogger("test.inbound_liquidity.relays")
    t.stored: List[Sequence[str]] = []
    t.updated: List[Sequence[str]] = []

    def _store(relays):
        t.stored.append(list(relays))
        t._last_swapserver_relays = list(relays)

    async def _update(relays):
        t.updated.append(list(relays))

    t._store_last_swapserver_relays = _store
    t.relay_manager = SimpleNamespace(update_relays=_update)
    return t


async def _run_one_tick(t: TargetedNostrTransport, *, ticks: int = 1) -> None:
    """Start the loop, let it service ``ticks`` pairs-updated events, stop it.

    The set/clear pair is deliberately synchronous with no await between the two,
    exactly as ``SwapManager.trigger_pairs_updated_threadsafe`` does it: ``set()``
    resolves the loop's waiter, ``clear()`` re-arms the event so the next
    iteration blocks again. Yielding in between would leave the event set and spin
    the loop.
    """
    task = asyncio.ensure_future(t.update_relays())
    await asyncio.sleep(0)                      # let the loop reach its first wait()
    try:
        for _ in range(ticks):
            t.sm.pairs_updated.set()
            t.sm.pairs_updated.clear()
            for _ in range(4):                  # let one iteration complete
                await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_follows_the_chosen_providers_relays() -> None:
    t = _transport(offers={CHOSEN: _offer(["wss://theirs"])})
    t.target_npub = CHOSEN
    asyncio.run(_run_one_tick(t))
    assert t.stored == [["wss://theirs"]]
    # The relay manager is handed the union of ours and theirs (the `relays` property).
    assert t.updated and set(t.updated[0]) == {"wss://ours", "wss://theirs"}


def test_falls_back_to_the_configured_provider() -> None:
    """Legacy single-provider setups keep stock behaviour."""
    t = _transport(offers={CONFIGURED: _offer(["wss://configured"])},
                   swapserver_npub=CONFIGURED)
    asyncio.run(_run_one_tick(t))
    assert t.stored == [["wss://configured"]]


def test_chosen_provider_wins_over_the_configured_one() -> None:
    t = _transport(offers={CHOSEN: _offer(["wss://chosen"]),
                          CONFIGURED: _offer(["wss://configured"])},
                   swapserver_npub=CONFIGURED)
    t.target_npub = CHOSEN
    asyncio.run(_run_one_tick(t))
    assert t.stored == [["wss://chosen"]]


def test_no_provider_known_is_quiet_and_harmless(caplog) -> None:
    """The reported symptom: no chosen npub and no configured npub. Nothing to
    follow -- so do nothing, and do not log a scary line about it."""
    t = _transport(offers={})
    with caplog.at_level(logging.DEBUG, logger=t.logger.name):
        asyncio.run(_run_one_tick(t, ticks=3))
    assert t.stored == [] and t.updated == []
    assert "no pair for" not in caplog.text
    assert "SWAPSERVER_NPUB" not in caplog.text


def test_chosen_provider_not_in_offers_is_quiet() -> None:
    """Its offer aged out of _offers between selection and this tick."""
    t = _transport(offers={})
    t.target_npub = CHOSEN
    asyncio.run(_run_one_tick(t))
    assert t.stored == [] and t.updated == []


def test_unchanged_relays_do_not_churn_the_relay_manager() -> None:
    t = _transport(offers={CHOSEN: _offer(["wss://theirs"])})
    t.target_npub = CHOSEN
    asyncio.run(_run_one_tick(t, ticks=3))
    assert len(t.stored) == 1        # stored once, then recognised as unchanged
    assert len(t.updated) == 1
