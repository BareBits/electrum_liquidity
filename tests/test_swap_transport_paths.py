"""Path coverage for ``TargetedNostrTransport.send_request_to_server``.

The RPC-timeout path is covered in test_reverse_swap_timeout.py; here we cover
the remaining branches that a real swap exercises but the mocked executor tests
skip:

  * the STOCK single-provider path (``target_pubkey`` unset) -- decodes
    ``config.SWAPSERVER_NPUB`` and addresses the DM to it;
  * a malformed ``SWAPSERVER_NPUB`` -> clean ``SwapServerError`` (not an opaque
    decode crash);
  * ``send_direct_message`` returning no event id -> ``SwapServerError``;
  * a provider reply carrying an ``error`` field -> ``SwapServerError`` (and the
    attacker-controlled text is scrubbed before logging).

Heavy NostrTransport.__init__ is bypassed; skipped outside the Electrum venv.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.submarine_swaps import SwapServerError  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity import swap_transport  # type: ignore  # noqa: E402
from electrum.plugins.inbound_liquidity.swap_transport import (  # type: ignore  # noqa: E402
    TargetedNostrTransport,
)

DECODED_PUBKEY = "cd" * 32   # what a valid npub decodes to (hex)


def _transport(*, target_pubkey=None, npub=None) -> TargetedNostrTransport:
    t = object.__new__(TargetedNostrTransport)
    t.logger = logging.getLogger("test.inbound_liquidity.transport_paths")
    t.dm_replies = {}
    t.target_pubkey = target_pubkey
    # `relays` is a read-only property over config.NOSTR_RELAYS + self.sm (touched
    # only by the stock-path debug log); satisfy it via config + a minimal sm
    # rather than a direct set.
    t.config = SimpleNamespace(SWAPSERVER_NPUB=npub, NOSTR_RELAYS="")
    t.sm = SimpleNamespace(is_server=True)
    return t


def _resolve_reply(t: TargetedNostrTransport, server_pubkey: str, event_id: str,
                   payload: dict):
    """Register-then-resolve the DM reply future the RPC awaits."""
    async def run():
        task = asyncio.ensure_future(
            t.send_request_to_server("createswap", {"foo": "bar"}))
        # Let the request run up to the point it registers its reply slot and
        # parks on the reply future. It may take more than one scheduler tick, so
        # poll rather than assume a single sleep(0) is enough.
        for _ in range(50):
            if (server_pubkey, event_id) in t.dm_replies:
                break
            await asyncio.sleep(0)
        t.dm_replies[(server_pubkey, event_id)].set_result(payload)
        return await task
    return asyncio.run(run())


# --- stock single-provider path -------------------------------------------
def test_stock_npub_path_addresses_configured_provider(monkeypatch) -> None:
    # target_pubkey unset -> the transport decodes config.SWAPSERVER_NPUB and
    # addresses the DM there (the legacy single-provider behaviour).
    monkeypatch.setattr(swap_transport.aionostr.util, "from_nip19",
                        lambda npub: {"object": bytes.fromhex(DECODED_PUBKEY)})
    t = _transport(target_pubkey=None, npub="npub1validlooking")

    sent = {}

    async def fake_send_dm(pubkey, content, *, retries=0):
        sent["pubkey"] = pubkey
        return "evt-stock"
    t.send_direct_message = fake_send_dm

    resp = _resolve_reply(t, DECODED_PUBKEY, "evt-stock", {"ok": True})
    assert resp == {"ok": True}
    assert sent["pubkey"] == DECODED_PUBKEY      # addressed to the decoded npub


def test_stock_npub_malformed_raises_cleanly(monkeypatch) -> None:
    # A hand-edited / invalid SWAPSERVER_NPUB must surface as SwapServerError,
    # not an opaque bech32 decode error, and no DM is sent.
    def _boom(npub):
        raise ValueError("bad bech32")
    monkeypatch.setattr(swap_transport.aionostr.util, "from_nip19", _boom)
    t = _transport(target_pubkey=None, npub="not-an-npub")

    sent = []
    t.send_direct_message = lambda *a, **k: sent.append(a)  # type: ignore[assignment]

    with pytest.raises(SwapServerError):
        asyncio.run(t.send_request_to_server("createswap", {"foo": "bar"}))
    assert sent == []


# --- reply-side failures ---------------------------------------------------
def test_no_event_id_raises() -> None:
    # send_direct_message returning a falsy event id (publish failed) -> the RPC
    # cannot be tracked, so it fails fast with SwapServerError.
    t = _transport(target_pubkey="ab" * 32)

    async def fake_send_dm(pubkey, content, *, retries=0):
        return ""                       # publish did not yield an event id
    t.send_direct_message = fake_send_dm

    with pytest.raises(SwapServerError):
        asyncio.run(t.send_request_to_server("createswap", {"foo": "bar"}))


def test_provider_error_reply_raises_and_scrubs(caplog) -> None:
    # A reply with an 'error' field is an attacker-controlled string: the RPC
    # raises SwapServerError, and the CR/LF-laden text is scrubbed before it
    # reaches the log (no forged second log line).
    target = "ab" * 32
    t = _transport(target_pubkey=target)

    async def fake_send_dm(pubkey, content, *, retries=0):
        return "evt-err"
    t.send_direct_message = fake_send_dm

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SwapServerError):
            _resolve_reply(t, target, "evt-err",
                           {"error": "boom\r\nFORGED: trust me"})
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "boom" in logged                     # the message is surfaced...
    assert "\r" not in logged and "\nFORGED" not in logged  # ...but control chars scrubbed
