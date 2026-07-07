#!/usr/bin/env python3
"""Launch the electrum_liquidity regtest test rig.

Brings up, on Bitcoin regtest:
  0. bitcoind (regtest) + a Fulcrum ElectrumX server, mining one block / N sec,
  1. Electrum wallet A ``electrum_liqtest`` (client; GUI by default),
  2. Electrum wallet B ``electrum_liqtest_swap_partner`` (headless daemon)
     advertising LN->onchain swaps at 0.5% over the nostr swap extension,
  3. a local-only nostr relay (nostr-rs-relay in Docker) both wallets point at,
  4. two balanced (50/50) 0.02 BTC Lightning channels between A and B, plus
     >=5 BTC of spendable on-chain funds in each wallet.

Each launch:
  * kills only *this rig's* leftover processes (marker-scoped; never touches
    other bitcoind/Electrum/Fulcrum instances) and wipes prior run state,
  * picks random free high ports for every service,
  * on Ctrl+C, gracefully tears down everything it started -- and nothing else.

Run ``python run.py --help`` for options.

Note: the swap PARTNER must run headless because Electrum's swapserver plugin is
``available_for: [cmdline]`` only -- it does not load under the Qt GUI. The
client wallet needs no plugin (client-side swap logic lives in Electrum core),
so it is the one shown in the GUI by default.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
import time
from types import FrameType
from typing import Optional

from rig import paths, ports
from rig.lnurl_stub import LnurlPayStub
from rig.procman import ProcessManager
from rig.services import (
    CLIENT,
    PARTNER,
    Endpoints,
    bitcoin_cli,
    discover_swap_provider,
    ensure_miner_wallet,
    ensure_plugin_installed,
    fund_address,
    partner_lightning_invoice,
    set_client_channel_peer,
    set_client_dev_fee_address,
    mine,
    open_channels,
    setup_wallet,
    start_bitcoind,
    start_electrum_daemon_ready,
    start_electrum_gui,
    start_fulcrum,
    start_nostr_relay,
    stop_daemon,
    wait_bitcoind,
    wait_electrum_ready,
    wait_fulcrum,
    wait_lightning_ready,
    wait_nostr_relay,
    wait_onchain_funds,
    wallet_balance,
)

# Coinbase outputs mature after 100 blocks; mine extra so each wallet has
# spendable funds immediately after the initial sends are confirmed.
DEFAULT_INITIAL_BLOCKS = 110
DEFAULT_MINE_INTERVAL = 30.0
DEFAULT_WALLET_FUNDING_BTC = 12.0   # per wallet, >=5 BTC after channels
DEFAULT_NUM_CHANNELS = 2
DEFAULT_CHANNEL_BTC = 0.02
SWAP_FEE_FRACTION = 0.005           # 0.5%


def log(msg: str) -> None:
    print(f"\033[1;36m[rig]\033[0m {msg}", flush=True)


class Rig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.pm = ProcessManager()
        self.ep: Optional[Endpoints] = None
        self.miner_address: Optional[str] = None
        self.client_address: Optional[str] = None
        self.partner_address: Optional[str] = None
        self.client_nodeid: Optional[str] = None
        self.partner_nodeid: Optional[str] = None
        self.swap_npub: Optional[str] = None
        self.swap_offer: Optional[dict] = None
        self.channels: list[dict] = []
        self.lnurl_stub: Optional[LnurlPayStub] = None
        self.lnurl_stub_port: Optional[int] = None
        self._stop = threading.Event()
        self._miner: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------
    def preflight(self) -> None:
        killed = self.pm.kill_previous()
        if killed:
            log(f"killed {len(killed)} leftover process(es) from a previous run: {killed}")
        if paths.RUN_DIR.exists():
            shutil.rmtree(paths.RUN_DIR)
        paths.RUN_DIR.mkdir(parents=True)
        log(f"fresh state at {paths.RUN_DIR}")

    def allocate(self) -> None:
        (btc_rpc, btc_p2p, fulcrum_tcp, fulcrum_admin, nostr,
         ln_client, ln_partner, swapserver) = ports.free_ports(8)
        self.ep = Endpoints(
            btc_rpc=btc_rpc, btc_p2p=btc_p2p,
            fulcrum_tcp=fulcrum_tcp, fulcrum_admin=fulcrum_admin,
            nostr=nostr, ln_listen_client=ln_client,
            ln_listen_partner=ln_partner, swapserver_port=swapserver,
        )
        log(f"ports: bitcoind-rpc={btc_rpc} p2p={btc_p2p}  "
            f"fulcrum-tcp={fulcrum_tcp} admin={fulcrum_admin}")
        log(f"ports: nostr={nostr}  ln-client={ln_client} ln-partner={ln_partner}  "
            f"swapserver={swapserver}")
        # A separate free port for the local LNURL-pay stub (dev-fee payout target).
        self.lnurl_stub_port = ports.free_port()
        log(f"ports: lnurl-stub={self.lnurl_stub_port}")

    def bring_up(self) -> None:
        assert self.ep is not None
        ep = self.ep

        log("starting bitcoind (regtest) ...")
        start_bitcoind(self.pm, ep)
        wait_bitcoind(ep)
        self.miner_address = ensure_miner_wallet(ep)
        log("bitcoind ready")

        blocks = self.args.blocks
        log(f"mining {blocks} initial blocks (coinbase maturity) ...")
        mine(ep, self.miner_address, blocks)

        log("starting nostr relay (docker) ...")
        start_nostr_relay(self.pm, ep)
        wait_nostr_relay(ep)
        log(f"nostr relay ready ({ep.nostr_relay_url})")

        log("creating client wallet (electrum_liqtest) + config ...")
        self.client_address = setup_wallet(ep, CLIENT)
        log(f"  client funding address: {self.client_address}")

        log("creating swap-partner wallet (electrum_liqtest_swap_partner) + config ...")
        self.partner_address = setup_wallet(ep, PARTNER)
        log(f"  partner funding address: {self.partner_address}")

        log(f"funding each wallet with {self.args.funding} BTC ...")
        self._fund_wallet(self.client_address)
        self._fund_wallet(self.partner_address)
        mine(ep, self.miner_address, 6)   # confirm funding

        log("starting Fulcrum ...")
        start_fulcrum(self.pm, ep)
        wait_fulcrum(ep, expected_height=blocks + 6)
        log("Fulcrum synced")

        # Install the inbound-liquidity plugin (symlink into Electrum's internal
        # plugins dir) before the client starts so it loads on launch.
        log("installing inbound-liquidity plugin ...")
        ensure_plugin_installed()

        # Bring up the swap partner FIRST: the client's liquidity plugin opens
        # channels to the swap provider (= partner), so we must know the
        # partner's LN node id before the client comes up.
        self._bring_up_partner()
        peer = self.partner_nodeid
        log(f"pointing liquidity plugin preferred partner at partner: {peer[:24]}...")
        set_client_channel_peer(peer)

        # Local LNURL-pay stub for the dev fee: mints invoices from the partner so
        # a dev-fee payout is a real Lightning payment over the rig's channels.
        # Started (and trusted, and pointed at) before the client comes up so the
        # client reads the payout address at load and validates the endpoint's TLS.
        self._bring_up_lnurl_stub()

        # Client wallet: GUI by default (the user-facing wallet that hosts the
        # liquidity plugin).
        self._bring_up_client()

        # Address-history sync can lag header sync, so confirm both wallets
        # actually see their on-chain funds before spending them on channels.
        log("waiting for on-chain funds to settle in both wallets ...")
        mine_cb = lambda n: mine(ep, self.miner_address, n)  # noqa: E731
        cbal = wait_onchain_funds(CLIENT, min_btc=self.args.funding * 0.9,
                                  mine_cb=mine_cb, log=log)
        pbal = wait_onchain_funds(PARTNER, min_btc=self.args.funding * 0.9,
                                  mine_cb=mine_cb, log=log)
        log(f"  client {cbal} BTC, partner {pbal} BTC confirmed")

        log("opening Lightning channels (client -> partner) ...")
        self.channels = open_channels(
            ep,
            opener=CLIENT, peer=PARTNER,
            peer_listen_port=ep.ln_listen_partner,
            num_channels=self.args.channels,
            capacity_btc=self.args.channel_btc,
            mine_cb=lambda n: mine(ep, self.miner_address, n),
            log=log,
        )
        log(f"  {len(self.channels)} channel(s) OPEN")
        for c in self.channels:
            log(f"    {c['short_channel_id']}  local={c['local_balance']} "
                f"remote={c['remote_balance']} sat")

        log("discovering swap provider over nostr ...")
        try:
            self.swap_npub, self.swap_offer = discover_swap_provider(
                CLIENT, expected_fee_fraction=SWAP_FEE_FRACTION, log=log)
        except TimeoutError as exc:
            # Non-fatal: the core rig (chain, server, relay, channels) is up; the
            # client can still be pointed at the partner manually. Surface it.
            log(f"  WARNING: swap-provider discovery did not complete: {exc}")

        self._summary()

    def _fund_wallet(self, address: str) -> None:
        """Fund an Electrum address with a few UTXOs (gives coin-selection room
        for opening multiple channels)."""
        assert self.ep is not None
        total = self.args.funding
        # Split into 3 utxos: 40% / 40% / 20%.
        for frac in (0.4, 0.4, 0.2):
            fund_address(self.ep, address, round(total * frac, 8))

    def _bring_up_client(self) -> None:
        assert self.ep is not None
        if self.args.gui:
            log("launching client Electrum GUI ...")
            start_electrum_gui(self.pm, self.ep, CLIENT, display=self.args.display)
        else:
            log("starting headless client Electrum daemon ...")
            start_electrum_daemon_ready(self.pm, CLIENT, log=log)
        wait_electrum_ready(CLIENT)
        self.client_nodeid = wait_lightning_ready(CLIENT)
        bal = wallet_balance(CLIENT)
        log(f"client online (nodeid {self.client_nodeid[:16]}..., "
            f"balance {bal}) ")

    def _bring_up_lnurl_stub(self) -> None:
        """Start the local LNURL-pay endpoint the dev fee is paid to, trust its
        self-signed cert in the venv's certifi bundle, and point the client's
        dev-fee payout address at it."""
        assert self.lnurl_stub_port is not None
        stub = LnurlPayStub(
            self.lnurl_stub_port,
            invoice_provider=partner_lightning_invoice,
            cert_dir=paths.RUN_DIR / "lnurl-stub",
        )
        stub.start()
        stub.trust_in_certifi(paths.certifi_ca_bundle())
        set_client_dev_fee_address(stub.lightning_address)
        self.lnurl_stub = stub
        log(f"LNURL dev-fee stub up at {stub.base_url} "
            f"(payout address {stub.lightning_address})")

    def _bring_up_partner(self) -> None:
        log("starting headless swap-partner Electrum daemon ...")
        start_electrum_daemon_ready(self.pm, PARTNER, log=log)
        wait_electrum_ready(PARTNER)
        self.partner_nodeid = wait_lightning_ready(PARTNER)
        bal = wallet_balance(PARTNER)
        log(f"swap-partner online (nodeid {self.partner_nodeid[:16]}..., "
            f"balance {bal}); swapserver advertising at 0.5%")

    def _summary(self) -> None:
        assert self.ep is not None
        ep = self.ep
        bar = "=" * 64
        log(bar)
        log("RIG UP")
        log(f"  bitcoind rpc      : 127.0.0.1:{ep.btc_rpc} (user/pass {paths.RPC_USER}/{paths.RPC_PASSWORD})")
        log(f"  fulcrum electrumx : 127.0.0.1:{ep.fulcrum_tcp} (tcp)")
        log(f"  nostr relay       : {ep.nostr_relay_url}")
        log(f"  client wallet     : {paths.CLIENT_WALLET_NAME}  (dir {CLIENT.datadir})")
        log(f"  partner wallet    : {paths.PARTNER_WALLET_NAME}  (dir {PARTNER.datadir})")
        log(f"  channels open     : {len(self.channels)} x {self.args.channel_btc} BTC (50/50)")
        log(f"  swap provider npub: {self.swap_npub}")
        log(f"  swap offer        : {json.dumps(self.swap_offer)}")
        if self.lnurl_stub is not None:
            log(f"  dev-fee payout    : {self.lnurl_stub.lightning_address} "
                f"(local LNURL stub; invoices minted by the partner)")
        log(bar)
        log("Drive a wallet, e.g.:")
        log(f"  {paths.ELECTRUM_BIN} --regtest --dir {CLIENT.datadir} "
            f"-w {CLIENT.datadir}/regtest/wallets/{paths.CLIENT_WALLET_NAME} list_channels")

    # -- miner --------------------------------------------------------------
    def start_miner(self) -> None:
        assert self.ep is not None and self.miner_address is not None
        interval = self.args.interval

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    mine(self.ep, self.miner_address, 1)  # type: ignore[arg-type]
                    log("mined 1 block")
                except Exception as exc:
                    log(f"mining error (continuing): {exc}")

        self._miner = threading.Thread(target=loop, name="miner", daemon=True)
        self._miner.start()
        log(f"miner running: 1 block / {interval:g}s")

    def shutdown(self) -> None:
        self._stop.set()
        log("shutting down ...")
        if self.lnurl_stub is not None:
            self.lnurl_stub.stop()
        stop_daemon(CLIENT)
        stop_daemon(PARTNER)
        self.pm.shutdown()
        log("all rig processes stopped")

    # -- readiness signalling ----------------------------------------------
    def signal_ready(self) -> None:
        assert self.ep is not None
        ep = self.ep
        payload = {
            "ready": True,
            "orchestrator_pid": os.getpid(),
            "bitcoind_rpc": ep.btc_rpc,
            "fulcrum_tcp": ep.fulcrum_tcp,
            "nostr_relay": ep.nostr_relay_url,
            "client_wallet": paths.CLIENT_WALLET_NAME,
            "client_datadir": str(CLIENT.datadir),
            "client_address": self.client_address,
            "client_nodeid": self.client_nodeid,
            "partner_wallet": paths.PARTNER_WALLET_NAME,
            "partner_datadir": str(PARTNER.datadir),
            "partner_address": self.partner_address,
            "partner_nodeid": self.partner_nodeid,
            "channels": self.channels,
            "swap_provider_npub": self.swap_npub,
            "swap_offer": self.swap_offer,
            "dev_fee_payout_address": (
                self.lnurl_stub.lightning_address if self.lnurl_stub else None),
            "dev_fee_lnurl_stub_url": (
                self.lnurl_stub.base_url if self.lnurl_stub else None),
            "blocks": self.args.blocks,
        }
        paths.READY_FILE.write_text(json.dumps(payload, indent=2))
        if self.args.ready_file:
            with open(self.args.ready_file, "w") as handle:
                json.dump(payload, handle, indent=2)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="electrum_liquidity regtest test rig")
    parser.add_argument("--blocks", type=int, default=DEFAULT_INITIAL_BLOCKS,
                        help="initial blocks to mine (>=101 for spendable coins)")
    parser.add_argument("--interval", type=float, default=DEFAULT_MINE_INTERVAL,
                        help="seconds between mined blocks")
    parser.add_argument("--funding", type=float, default=DEFAULT_WALLET_FUNDING_BTC,
                        help="BTC to fund each Electrum wallet with")
    parser.add_argument("--channels", type=int, default=DEFAULT_NUM_CHANNELS,
                        help="number of channels between the two wallets")
    parser.add_argument("--channel-btc", type=float, default=DEFAULT_CHANNEL_BTC,
                        help="capacity of each channel (BTC)")
    parser.add_argument("--no-gui", dest="gui", action="store_false",
                        help="run the client wallet headless too (no Qt GUI)")
    parser.add_argument("--display", default=None,
                        help="X DISPLAY for the GUI (default: inherit environment)")
    parser.add_argument("--ready-file", default=None,
                        help="also write the JSON readiness file here")
    parser.add_argument("--exit-when-ready", action="store_true",
                        help="bring everything up, signal readiness, then tear down (smoke test)")
    return parser.parse_args(argv)


def _ensure_marked() -> None:
    """Re-exec once so this process's /proc/environ carries the rig marker, so a
    future launch can discover and kill it. execv preserves the PID."""
    if os.environ.get(paths.MARKER_ENV) == paths.MARKER_VALUE:
        return
    os.environ[paths.MARKER_ENV] = paths.MARKER_VALUE
    os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]])


def main(argv: Optional[list[str]] = None) -> int:
    _ensure_marked()
    args = parse_args(argv)
    if args.blocks < 101:
        log("warning: --blocks < 101 means coinbase coins will not be spendable yet")

    rig = Rig(args)

    def handle_signal(signum: int, _frame: Optional[FrameType]) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        rig.preflight()
        rig.allocate()
        rig.bring_up()
        rig.signal_ready()

        if args.exit_when_ready:
            log("readiness reached; exiting (smoke mode)")
            rig.shutdown()
            return 0

        rig.start_miner()
        log("rig is up. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        rig.shutdown()
        return 0
    except Exception as exc:
        log(f"error: {exc!r}")
        rig.shutdown()
        return 1


if __name__ == "__main__":
    sys.exit(main())
