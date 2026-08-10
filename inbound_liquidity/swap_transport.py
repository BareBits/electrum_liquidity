# A NostrTransport that can direct a swap to a *chosen* provider, instead of
# the single ``config.SWAPSERVER_NPUB`` the stock transport hardcodes.
#
# Electrum's ``SwapManager.reverse_swap`` routes its RPCs through the
# transport's ``send_request_to_server`` (see submarine_swaps.py). By passing
# one of these (with ``target_pubkey`` set) we send the swap to the provider the
# rules engine selected as cheapest -- with no change to core Electrum. Offer
# discovery (``get_recent_offers`` / ``_offers``) is inherited unchanged.
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import electrum_aionostr as aionostr
import electrum_aionostr.util  # noqa: F401  (ensures aionostr.util is bound)

from electrum.submarine_swaps import NostrTransport, SwapServerError

from .liquidity_manager import looks_like_hex_pubkey, scrub_text


class TargetedNostrTransport(NostrTransport):
    """A ``NostrTransport`` whose swap RPCs go to ``target_pubkey`` when set.

    ``target_pubkey`` is the provider's 32-byte nostr pubkey as hex (the value
    stored in ``SwapOffer.server_pubkey``). Leave it ``None`` to fall back to
    the stock single-provider behaviour (``config.SWAPSERVER_NPUB``).
    """

    # Upper bound (seconds) on how long we wait for a provider's reply to a swap
    # RPC (chiefly ``createswap``). The stock transport awaits the reply Future
    # with NO timeout, so a provider that ACKs our request over Nostr but never
    # sends the reply hangs the caller forever. In the plugin that await happens
    # while the per-wallet evaluation lock is held, so an unresponsive provider
    # would freeze ALL automation for that wallet indefinitely. Bounding it lets
    # ``_reverse_swap``'s existing ``asyncio.TimeoutError`` handler record a
    # provider reliability fault and move on. Sized to match the stock HTTP swap
    # transport's own 30s RPC timeout, with headroom for slower Nostr relays;
    # a class attribute so tests can shrink it.
    RPC_REPLY_TIMEOUT_SEC: float = 60.0

    # Yield this long on the "no provider to follow" path of update_relays; see
    # the comment there. A class attribute so tests can shrink it.
    NO_PROVIDER_BACKOFF_SEC: float = 0.05

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.target_pubkey: Optional[str] = None
        # npub form of the same provider, used to follow its announced relays.
        self.target_npub: Optional[str] = None

    def get_relay_manager(self):
        """As stock, but honour the Log tab's "capture debug-level logs" switch.

        ``NostrTransport.get_relay_manager`` pins its ``aionostr`` child logger to
        INFO every time a transport is built ("DEBUG is very verbose"). Those are
        precisely the records that show whether a swap provider ever answered our
        DM, and because the pin is re-applied per transport, lifting it once when
        the operator ticks the switch would be undone on the next evaluation. So
        re-lift it here, after super() has pinned it.
        """
        manager = super().get_relay_manager()
        if bool(getattr(self.config, "INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG", False)):
            try:
                self.logger.getChild("aionostr").setLevel(logging.DEBUG)
            except Exception:  # noqa: BLE001  (diagnostics must never break swaps)
                pass
        return manager

    async def update_relays(self) -> None:
        """Follow the *chosen* provider's announced relays.

        The stock loop resolves the provider as ``config.SWAPSERVER_NPUB`` only.
        In multi-provider mode we leave that config unset, so every time the
        plugin points the swap math at a provider (``sm.update_pairs``) the stock
        loop woke up, looked up ``self._offers[None]``, found nothing, and logged
        ``pairs updated but no pair for self.config.SWAPSERVER_NPUB=None
        available`` with a stack trace -- once per swap evaluation. That was the
        visible symptom; the real cost was silent: ``_last_swapserver_relays``
        never got updated, so unlike stock Electrum we never joined the relays
        the chosen provider announced. A provider whose reply DMs go out on
        relays we are not subscribed to looks, from our side, exactly like a swap
        that was accepted and then never funded.

        So resolve the provider as "the one we chose, else the configured one",
        and when neither is known just wait for the next tick instead of logging.
        Everything else mirrors the stock loop.
        """
        while True:
            previous_relays = self._last_swapserver_relays
            await self.sm.pairs_updated.wait()
            npub = self.target_npub or self.config.SWAPSERVER_NPUB
            offer = self._offers.get(npub) if npub else None
            if offer is None:
                # No provider chosen yet (or its offer has aged out of _offers):
                # there is nothing to follow. Not an error -- normal in
                # multi-provider mode before the first swap. Yield briefly before
                # looping: `pairs_updated` is set and cleared synchronously by
                # SwapManager, so wait() returns once per trigger and this cannot
                # spin -- but a future caller that leaves the event set would
                # otherwise starve the asyncio thread the whole wallet runs on.
                await asyncio.sleep(self.NO_PROVIDER_BACKOFF_SEC)
                continue
            latest_known_relays = offer.relays
            if latest_known_relays != previous_relays:
                self.logger.debug(
                    "swap provider relays changed, updating relay list.")
                self._store_last_swapserver_relays(latest_known_relays)
                await self.relay_manager.update_relays(self.relays)

    async def send_request_to_server(self, method: str, request_data: dict) -> dict:
        # Address the chosen provider when one is set, else the single configured
        # provider (stock ``config.SWAPSERVER_NPUB`` behaviour). Either way we
        # bound the reply wait below -- unlike the stock transport, which awaits
        # it unbounded (the reason this override reimplements both paths rather
        # than delegating the untargeted one to ``super()``).
        request_data['method'] = method
        if self.target_pubkey is not None:
            # ``target_pubkey`` comes from a provider's untrusted nostr offer
            # (``SwapOffer.server_pubkey``). Require the exact 64-hex shape of a
            # 32-byte pubkey before we address a DM to it: a malformed value must
            # fail cleanly here (surfacing as a provider fault upstream) rather
            # than reaching aionostr's addressing / our reply-slot dict as an
            # arbitrary string.
            if not looks_like_hex_pubkey(self.target_pubkey):
                self.logger.warning(
                    f"refusing swap RPC to malformed provider pubkey "
                    f"{scrub_text(self.target_pubkey, max_len=80)!r}")
                raise SwapServerError()
            server_pubkey = self.target_pubkey
            self.logger.debug(f"swapserver req -> {server_pubkey[:8]}: method={method}")
        else:
            server_npub = self.config.SWAPSERVER_NPUB
            try:
                server_pubkey = aionostr.util.from_nip19(server_npub)['object'].hex()
            except Exception:
                # A hand-edited / malformed SWAPSERVER_NPUB must not crash the
                # RPC path with an opaque decode error.
                self.logger.warning(
                    f"configured SWAPSERVER_NPUB is not a valid npub "
                    f"({scrub_text(server_npub, max_len=80)!r}); cannot send swap RPC")
                raise SwapServerError()
            self.logger.debug(f"swapserver req: method: {method} relays: {self.relays}")
        event_id = await self.send_direct_message(server_pubkey, json.dumps(request_data), retries=1)
        if not event_id:
            raise SwapServerError()
        key = (server_pubkey, event_id)
        self.dm_replies[key] = fut = asyncio.Future()
        try:
            response = await asyncio.wait_for(fut, timeout=self.RPC_REPLY_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            # The provider never replied. Drop the dangling reply slot first, so a
            # late reply can't try to resolve a Future nobody is awaiting (the DM
            # handler skips when the slot is gone -- see NostrTransport's reply
            # dispatch), then surface the timeout. ``_reverse_swap`` treats it as a
            # provider reliability fault.
            self.dm_replies.pop(key, None)
            self.logger.warning(
                f"swap server {server_pubkey[:8]} did not reply within "
                f"{self.RPC_REPLY_TIMEOUT_SEC:.0f}s (method={method})")
            raise
        assert isinstance(response, dict)
        if 'error' in response:
            # The error text is an attacker-controlled string from the provider's
            # DM reply. Scrub control characters (CR/LF/ANSI) before logging so it
            # cannot forge a second log line or rewrite the operator's terminal.
            self.logger.warning(
                f"error from swap server [DO NOT TRUST THIS MESSAGE]: "
                f"{scrub_text(response['error'])}")
            raise SwapServerError()
        return response
