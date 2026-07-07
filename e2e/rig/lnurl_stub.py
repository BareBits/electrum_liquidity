"""A minimal, local LNURL-pay server for the rig.

The dev-fee feature pays accrued fees to a Lightning address over LNURL-pay. The
production address (``electrum_liqhelper@getbarebits.com``) lives on mainnet, so
to exercise the *real* payout path on regtest we stand up a tiny LNURL-pay
endpoint here that mints its invoices from the swap-partner Electrum daemon. A
dev-fee payout then flows as a genuine Lightning payment over the rig's channels.

Two wrinkles this module handles so the client trusts and reaches it:

* Electrum refuses non-``https`` LNURL endpoints (``_is_url_safe_enough_for_lnurl``),
  so the server speaks TLS. We generate a throwaway self-signed cert for
  ``127.0.0.1`` (SAN ``IP:127.0.0.1``) in-process via ``cryptography``.
* Electrum verifies LNURL TLS against the certifi CA bundle. We append our cert
  to the bundle *inside the rig's venv* (delimited so repeat launches replace,
  not accumulate) -- the client then validates the local endpoint. Nothing
  outside the venv is touched, and no Electrum code is modified.

The server is a stdlib ``http.server`` on a daemon thread: it is only hit during
manual testing, so a blocking handler that shells out to the partner daemon for
each invoice is entirely adequate.
"""

from __future__ import annotations

import datetime
import http.server
import json
import ssl
import threading
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import ipaddress

# Delimiters bounding the cert we append to the certifi bundle, so each launch
# strips the previous rig cert and appends a fresh one (no unbounded growth).
_CA_BEGIN = "# --- electrum_liqtest rig LNURL stub cert (auto-managed) ---\n"
_CA_END = "# --- end electrum_liqtest rig LNURL stub cert ---\n"

# The username portion of the payout Lightning address the client is pointed at.
STUB_USERNAME = "electrum_liqhelper"


def _generate_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Write a self-signed cert/key for 127.0.0.1 (SAN IP:127.0.0.1) to disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "electrum_liqtest-rig"),
    ])
    # Fixed validity window (no Date.now-style clock dependence at import time):
    # a wide range that comfortably covers any rig session.
    not_before = datetime.datetime(2020, 1, 1)
    not_after = datetime.datetime(2100, 1, 1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("127.0.0.1"),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


class LnurlPayStub:
    """A threaded HTTPS LNURL-pay endpoint that mints invoices via a callback.

    ``invoice_provider(amount_sat) -> bolt11`` is supplied by the caller (the rig
    wires it to the swap-partner daemon). ``min_sendable_sat``/``max_sendable_sat``
    bound what the endpoint will accept; the default max comfortably exceeds the
    dev-fee daily cap so a full day's payout fits in one invoice.
    """

    def __init__(self, port: int, invoice_provider: Callable[[int], str],
                 cert_dir: Path, *, username: str = STUB_USERNAME,
                 min_sendable_sat: int = 1, max_sendable_sat: int = 1_000_000) -> None:
        self.port = port
        self.username = username
        self.min_sendable_sat = min_sendable_sat
        self.max_sendable_sat = max_sendable_sat
        self._invoice_provider = invoice_provider
        self._cert_dir = cert_dir
        self._cert_path = cert_dir / "lnurl_stub_cert.pem"
        self._key_path = cert_dir / "lnurl_stub_key.pem"
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def lightning_address(self) -> str:
        return f"{self.username}@127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._cert_dir.mkdir(parents=True, exist_ok=True)
        _generate_self_signed_cert(self._cert_path, self._key_path)
        handler = self._make_handler()
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(self._cert_path), keyfile=str(self._key_path))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="lnurl-stub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def trust_in_certifi(self, ca_bundle_path: Path) -> None:
        """Append our cert to the venv's certifi bundle so the client validates
        the local endpoint. Idempotent across launches: any prior rig cert block
        is stripped first, so the bundle never grows without bound."""
        cert_pem = self._cert_path.read_text()
        existing = ca_bundle_path.read_text() if ca_bundle_path.exists() else ""
        existing = self._strip_managed_block(existing)
        block = _CA_BEGIN + cert_pem + ("" if cert_pem.endswith("\n") else "\n") + _CA_END
        ca_bundle_path.write_text(existing + block)

    @staticmethod
    def _strip_managed_block(text: str) -> str:
        start = text.find(_CA_BEGIN)
        if start == -1:
            return text
        end = text.find(_CA_END, start)
        if end == -1:
            return text[:start]
        return text[:start] + text[end + len(_CA_END):]

    def _lnurl6_metadata(self) -> str:
        return json.dumps([
            ["text/plain", "electrum_liquidity dev fee (rig)"],
            ["text/identifier", self.lightning_address],
        ])

    def _make_handler(self):
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence per-request logging
                pass

            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                if path.startswith("/.well-known/lnurlp/"):
                    self._send_json({
                        "tag": "payRequest",
                        "callback": f"{stub.base_url}/lnurlp/callback",
                        "minSendable": stub.min_sendable_sat * 1000,
                        "maxSendable": stub.max_sendable_sat * 1000,
                        "metadata": stub._lnurl6_metadata(),
                        "commentAllowed": 0,
                    })
                    return
                if path.startswith("/lnurlp/callback"):
                    qs = urllib.parse.parse_qs(parsed.query)
                    try:
                        amount_msat = int(qs["amount"][0])
                    except (KeyError, IndexError, ValueError):
                        self._send_json({"status": "ERROR",
                                         "reason": "missing/invalid amount"})
                        return
                    amount_sat = amount_msat // 1000
                    if not (stub.min_sendable_sat <= amount_sat <= stub.max_sendable_sat):
                        self._send_json({"status": "ERROR",
                                         "reason": "amount out of range"})
                        return
                    try:
                        bolt11 = stub._invoice_provider(amount_sat)
                    except Exception as e:  # noqa: BLE001
                        self._send_json({"status": "ERROR",
                                         "reason": f"invoice error: {e}"})
                        return
                    if not bolt11:
                        self._send_json({"status": "ERROR",
                                         "reason": "no invoice produced"})
                        return
                    self._send_json({"pr": bolt11, "routes": []})
                    return
                self._send_json({"status": "ERROR", "reason": "not found"}, status=404)

        return Handler
