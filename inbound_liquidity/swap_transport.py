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

from electrum.submarine_swaps import NostrTransport, SwapServerError


class TargetedNostrTransport(NostrTransport):
    """A ``NostrTransport`` whose swap RPCs go to ``target_pubkey`` when set.

    ``target_pubkey`` is the provider's 32-byte nostr pubkey as hex (the value
    stored in ``SwapOffer.server_pubkey``). Leave it ``None`` to fall back to
    the stock single-provider behaviour (``config.SWAPSERVER_NPUB``).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.target_pubkey: Optional[str] = None

    async def send_request_to_server(self, method: str, request_data: dict) -> dict:
        if self.target_pubkey is None:
            # No explicit target: behave exactly like the stock transport.
            return await super().send_request_to_server(method, request_data)
        # Mirror NostrTransport.send_request_to_server, but address the chosen
        # provider instead of config.SWAPSERVER_NPUB.
        self.logger.debug(f"swapserver req -> {self.target_pubkey[:8]}: method={method}")
        request_data['method'] = method
        server_pubkey = self.target_pubkey
        event_id = await self.send_direct_message(server_pubkey, json.dumps(request_data), retries=1)
        if not event_id:
            raise SwapServerError()
        self.dm_replies[(server_pubkey, event_id)] = fut = asyncio.Future()
        response = await fut
        assert isinstance(response, dict)
        if 'error' in response:
            self.logger.warning(f"error from swap server [DO NOT TRUST THIS MESSAGE]: {response['error']}")
            raise SwapServerError()
        return response
