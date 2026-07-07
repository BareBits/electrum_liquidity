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
from typing import Optional

import electrum_aionostr as aionostr
import electrum_aionostr.util  # noqa: F401  (ensures aionostr.util is bound)

from electrum.submarine_swaps import NostrTransport, SwapServerError


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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.target_pubkey: Optional[str] = None

    async def send_request_to_server(self, method: str, request_data: dict) -> dict:
        # Address the chosen provider when one is set, else the single configured
        # provider (stock ``config.SWAPSERVER_NPUB`` behaviour). Either way we
        # bound the reply wait below -- unlike the stock transport, which awaits
        # it unbounded (the reason this override reimplements both paths rather
        # than delegating the untargeted one to ``super()``).
        request_data['method'] = method
        if self.target_pubkey is not None:
            server_pubkey = self.target_pubkey
            self.logger.debug(f"swapserver req -> {server_pubkey[:8]}: method={method}")
        else:
            server_npub = self.config.SWAPSERVER_NPUB
            server_pubkey = aionostr.util.from_nip19(server_npub)['object'].hex()
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
            self.logger.warning(f"error from swap server [DO NOT TRUST THIS MESSAGE]: {response['error']}")
            raise SwapServerError()
        return response
