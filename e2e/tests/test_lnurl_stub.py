"""Unit tests for the rig's local LNURL-pay stub (dev-fee payout target).

Exercises the real TLS + LNURL-pay round-trip end to end: generate the
self-signed cert, serve over HTTPS, trust it via the certifi-append path, then
fetch the well-known pay params and the callback invoice with strict TLS
verification (as Electrum does). A fake invoice provider stands in for the
partner daemon. No Electrum services launched; RAM-light.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rig.lnurl_stub import LnurlPayStub, _CA_BEGIN, _CA_END  # noqa: E402
from rig import ports  # noqa: E402


def _fake_invoice(sat: int) -> str:
    return f"lnbcrt{sat}n1fakeinvoice"


@pytest.fixture()
def stub(tmp_path):
    s = LnurlPayStub(ports.free_port(), _fake_invoice, cert_dir=tmp_path / "cert",
                     max_sendable_sat=50_000)
    s.start()
    yield s
    s.stop()


def _tls_get(url: str, ca_bundle: Path) -> dict:
    ctx = ssl.create_default_context(cafile=str(ca_bundle))
    with urllib.request.urlopen(url, context=ctx, timeout=10) as resp:
        return json.loads(resp.read().decode())


def test_wellknown_and_callback_over_verified_tls(stub, tmp_path) -> None:
    # A fresh CA bundle the stub appends its cert to (as the rig does to certifi).
    ca = tmp_path / "cacert.pem"
    ca.write_text("")
    stub.trust_in_certifi(ca)

    lightning_addr = stub.lightning_address
    assert lightning_addr.endswith(f"@127.0.0.1:{stub.port}")

    # LNURL-pay well-known: parseable pay params with an https callback.
    well_known = _tls_get(
        f"{stub.base_url}/.well-known/lnurlp/electrum_liqhelper", ca)
    assert well_known["tag"] == "payRequest"
    assert well_known["callback"].startswith("https://127.0.0.1")
    assert well_known["minSendable"] == 1000                 # 1 sat, in msat
    assert well_known["maxSendable"] == 50_000_000           # 50k sat, in msat
    meta = json.loads(well_known["metadata"])
    assert any(m[0] == "text/plain" for m in meta)

    # Callback: returns an invoice (pr) for the requested amount.
    cb = _tls_get(f"{well_known['callback']}?amount=3000000", ca)   # 3000 sat
    assert cb["pr"] == _fake_invoice(3000)


def test_callback_rejects_out_of_range_amount(stub, tmp_path) -> None:
    ca = tmp_path / "cacert.pem"
    ca.write_text("")
    stub.trust_in_certifi(ca)
    # 60k sat exceeds the 50k max -> ERROR response, no invoice.
    resp = _tls_get(f"{stub.base_url}/lnurlp/callback?amount=60000000", ca)
    assert resp.get("status") == "ERROR"
    assert "pr" not in resp


def test_certifi_append_is_idempotent(stub, tmp_path) -> None:
    ca = tmp_path / "cacert.pem"
    ca.write_text("# preexisting system cert\n")
    stub.trust_in_certifi(ca)
    stub.trust_in_certifi(ca)          # second launch must replace, not accumulate
    text = ca.read_text()
    assert text.count(_CA_BEGIN) == 1
    assert text.count(_CA_END) == 1
    assert text.startswith("# preexisting system cert")   # original content kept


def test_untrusted_cert_is_rejected(stub, tmp_path) -> None:
    # Without trusting the stub's cert, strict TLS verification must fail.
    empty_ca = tmp_path / "empty.pem"
    empty_ca.write_text("")
    with pytest.raises(Exception):
        _tls_get(f"{stub.base_url}/.well-known/lnurlp/electrum_liqhelper", empty_ca)
