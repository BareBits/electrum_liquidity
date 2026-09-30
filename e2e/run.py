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
    PARTNER2,
    PARTNER3,
    PARTNER_FEE_MILLIONTHS,
    PARTNER2_FEE_MILLIONTHS,
    PARTNER3_FEE_MILLIONTHS,
    Endpoints,
    bitcoin_cli,
    disable_forwarding,
    discover_swap_provider,
    ensure_miner_wallet,
    electrum_cli,
    ensure_plugin_installed,
    fund_address,
    partner_lightning_invoice,
    partner2_lightning_invoice,
    set_client_channel_peer,
    set_client_dev_fee_address,
    set_gossip_seed_peers,
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
    probe_routed_payment,
    wait_gossip_graph,
    wait_lightning_ready,
    wait_nostr_relay,
    wait_onchain_funds,
    wait_wallet_height,
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

# --- gossip mode (--gossip) ----------------------------------------------
# The forwarding hop PARTNER -> PARTNER2, deliberately starved of outbound so a
# payment routed through it succeeds only below a known size. Sized against the
# client's own drainable balance (~960k sat on a 0.02 BTC 50/50 channel), so the
# liquidity-sink ladder must halve TWICE before a rung fits:
#
#   ~960k  no route at all (above this channel's announced capacity)
#    ~480k route exists on paper, HTLC fails on the hop's real balance
#    ~240k fits under the ~300k the partner can forward  -> settles
#
# This is the only way a multi-hop capacity failure can be produced here: on a
# DIRECT channel, Electrum's available_to_spend already folds in the reserve, the
# fee-spike buffer, remaining_max_inflight and HTLC slots, and the plugin
# subtracts more headroom still -- so the ladder's top rung is always sized to
# succeed and there is nothing left for halving to fix.
GOSSIP_HOP_CHANNEL_BTC = 0.006      # 600_000 sat capacity
GOSSIP_HOP_PUSH_BTC = 0.003         # 300_000 sat pushed -> that much forwardable

# --- deep-hop mode (--deep-hop, implies --gossip) -------------------------
# The PARTNER2 <-> PARTNER3 channel, which puts a SECOND forwarding node between
# the client and a swap provider: client -> partner -> partner2 -> partner3.
#
# It is opened BY PARTNER3, not partner2, and that is load-bearing. Deep-hop mode
# runs partner2 with ``lightning_forward_payments=false`` so it refuses to relay
# and answers with a real onion error of its own (see _common_config_pairs), and
# ``channel_establishment_flow`` will not let a node with forwarding off OPEN an
# announced channel -- only accept one.
#
# Capacity is NOT the mechanism here, so this channel is simply balanced. Starving
# it could not work: a swapserver advertises max_forward = num_sats_can_receive(),
# so the largest swap partner3 will accept is by construction the most partner2
# could still have forwarded to it.
DEEP_HOP_CHANNEL_BTC = 0.02         # 2_000_000 sat, balanced 50/50
# In deep-hop mode the FIRST hop (partner -> partner2) must be able to carry the
# whole swap, or the payment dies at our own channel peer and the test proves the
# opposite of what it means to. The gossip-mode default is deliberately tiny
# (0.003 BTC forwardable) so the liquidity sink's halving ladder has something to
# fail against -- and a swap planned against partner3 is ~750k sat, which that hop
# cannot forward. Observed exactly once as a peer fault where a downstream
# suppression was expected. Sized well clear of partner3's advertised ceiling
# (~790k, itself bounded by DEEP_HOP_PUSH_BTC below).
DEEP_HOP_FIRST_HOP_BTC = 0.03       # 3_000_000 sat capacity
DEEP_HOP_FIRST_HOP_PUSH_BTC = 0.02  # 2_000_000 sat forwardable by the partner
# What partner3 pushes to partner2 on that channel. It is partner3's INBOUND, and
# therefore the ceiling its swapserver advertises -- so it must be comfortably
# above the swap the plugin will plan, or partner3 is never ranked at all.
DEEP_HOP_PUSH_BTC = 0.01            # 1_000_000 sat -> partner3 can receive ~1M
# A tiny payment made during bring-up to PROVE the graph can actually be routed
# over. Small enough not to meaningfully move the balances a test then reasons
# about (a third of a percent of the hop's forwardable balance).
GOSSIP_PROBE_SAT = 1_000


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
        self.partner2_address: Optional[str] = None
        self.client_nodeid: Optional[str] = None
        self.partner_nodeid: Optional[str] = None
        self.partner2_nodeid: Optional[str] = None
        self.swap_npub: Optional[str] = None
        self.swap_offer: Optional[dict] = None
        self.channels: list[dict] = []
        self.lnurl_stub: Optional[LnurlPayStub] = None
        self.lnurl_stub_port: Optional[int] = None
        # Gossip mode only: a SECOND LNURL endpoint whose invoices are minted by
        # partner2, so paying it is a routed two-hop payment rather than a direct
        # one. This is the liquidity sink the sink e2e points the plugin at.
        self.sink_stub: Optional[LnurlPayStub] = None
        self.sink_stub_port: Optional[int] = None
        self.hop_channel: Optional[dict] = None
        self.partner3_address: Optional[str] = None
        self.partner3_nodeid: Optional[str] = None
        self.deep_hop_channel: Optional[dict] = None
        self._stop = threading.Event()
        self._miner: Optional[threading.Thread] = None

    @property
    def needs_partner2(self) -> bool:
        """Whether a third Electrum node is required. ``--second-provider`` wants
        it as a competing swapserver (with a channel from the client);
        ``--gossip`` wants it as a routing DESTINATION, deliberately WITHOUT a
        client channel, so reaching it requires a real multi-hop route."""
        return bool(self.args.second_provider or self.args.gossip)

    @property
    def needs_partner3(self) -> bool:
        """Whether the FOURTH node is required. Only ``--deep-hop`` wants it: a
        provider two forwarding nodes away, so a failed payment can carry a
        ``sender_idx`` that is not our channel peer's."""
        return bool(self.args.deep_hop)

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
         ln_client, ln_partner, swapserver,
         ln_partner2, swapserver2,
         ln_partner3, swapserver3) = ports.free_ports(12)
        self.ep = Endpoints(
            btc_rpc=btc_rpc, btc_p2p=btc_p2p,
            fulcrum_tcp=fulcrum_tcp, fulcrum_admin=fulcrum_admin,
            nostr=nostr, ln_listen_client=ln_client,
            ln_listen_partner=ln_partner, swapserver_port=swapserver,
            ln_listen_partner2=ln_partner2, swapserver_port2=swapserver2,
            ln_listen_partner3=ln_partner3, swapserver_port3=swapserver3,
        )
        log(f"ports: bitcoind-rpc={btc_rpc} p2p={btc_p2p}  "
            f"fulcrum-tcp={fulcrum_tcp} admin={fulcrum_admin}")
        log(f"ports: nostr={nostr}  ln-client={ln_client} ln-partner={ln_partner}  "
            f"swapserver={swapserver}")
        if self.needs_partner2:
            log(f"ports: ln-partner2={ln_partner2} swapserver2={swapserver2}")
        if self.needs_partner3:
            log(f"ports: ln-partner3={ln_partner3} swapserver3={swapserver3}")
        # A separate free port for the local LNURL-pay stub (dev-fee payout target).
        self.lnurl_stub_port = ports.free_port()
        log(f"ports: lnurl-stub={self.lnurl_stub_port}")
        if self.args.gossip:
            self.sink_stub_port = ports.free_port()
            log(f"ports: sink-stub={self.sink_stub_port}")

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
        self.client_address = setup_wallet(ep, CLIENT, gossip=self.args.gossip)
        log(f"  client funding address: {self.client_address}")

        log("creating swap-partner wallet (electrum_liqtest_swap_partner) + config ...")
        self.partner_address = setup_wallet(ep, PARTNER, gossip=self.args.gossip)
        log(f"  partner funding address: {self.partner_address}")

        if self.needs_partner2:
            log("creating SECOND swap-provider wallet "
                "(electrum_liqtest_swap_partner2) + config ...")
            # Note partner2 comes up FORWARDING NORMALLY even in deep-hop mode.
            # It is the node that must refuse to relay, but it cannot be
            # configured that way from the start without falling out of the
            # gossip network -- see services.disable_forwarding. The flip happens
            # once the graph is built.
            self.partner2_address = setup_wallet(ep, PARTNER2, gossip=self.args.gossip)
            log(f"  partner2 funding address: {self.partner2_address}")

        if self.needs_partner3:
            log("creating FOURTH wallet (electrum_liqtest_swap_partner3) + config ...")
            self.partner3_address = setup_wallet(ep, PARTNER3, gossip=self.args.gossip)
            log(f"  partner3 funding address: {self.partner3_address}")

        log(f"funding each wallet with {self.args.funding} BTC ...")
        self._fund_wallet(self.client_address)
        self._fund_wallet(self.partner_address)
        if self.needs_partner2:
            self._fund_wallet(self.partner2_address)
        if self.needs_partner3:
            self._fund_wallet(self.partner3_address)
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
        if self.needs_partner2:
            self._bring_up_partner2()
        if self.needs_partner3:
            self._bring_up_partner3()
        peer = self.partner_nodeid
        log(f"pointing liquidity plugin preferred partner at partner: {peer[:24]}...")
        set_client_channel_peer(peer)

        if self.args.gossip:
            # The client's gossip node is separate from its wallet and, on
            # regtest, has no way to find anybody -- see set_gossip_seed_peers.
            # Point it at the partner, which serves gossip queries and holds both
            # announced channels.
            self._seed_gossip_peers()

        # Local LNURL-pay stub for the dev fee: mints invoices from the partner so
        # a dev-fee payout is a real Lightning payment over the rig's channels.
        # Started (and trusted, and pointed at) before the client comes up so the
        # client reads the payout address at load and validates the endpoint's TLS.
        self._bring_up_lnurl_stub()
        if self.args.gossip:
            self._bring_up_sink_stub()

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
        if self.needs_partner2:
            p2bal = wait_onchain_funds(PARTNER2, min_btc=self.args.funding * 0.9,
                                       mine_cb=mine_cb, log=log)
            log(f"  partner2 {p2bal} BTC confirmed")
        if self.needs_partner3:
            p3bal = wait_onchain_funds(PARTNER3, min_btc=self.args.funding * 0.9,
                                       mine_cb=mine_cb, log=log)
            log(f"  partner3 {p3bal} BTC confirmed")

        log("opening Lightning channels (client -> partner) ...")
        self.channels = open_channels(
            ep,
            opener=CLIENT, peer=PARTNER,
            peer_listen_port=ep.ln_listen_partner,
            num_channels=self.args.channels,
            capacity_btc=self.args.channel_btc,
            mine_cb=lambda n: mine(ep, self.miner_address, n),
            log=log,
            # In gossip mode every channel must be ANNOUNCED or the graph stays
            # empty and nothing can be routed through it.
            public=self.args.gossip,
        )
        log(f"  {len(self.channels)} channel(s) OPEN")
        for c in self.channels:
            log(f"    {c['short_channel_id']}  local={c['local_balance']} "
                f"remote={c['remote_balance']} sat")

        if self.args.gossip:
            self._open_forwarding_hop()

        if self.args.second_provider:
            # The second provider needs a channel from the client for two
            # reasons: its swapserver advertises max_forward from
            # num_sats_can_receive(), so with no channel it would advertise a
            # zero cap and the plugin would never rank it; and a reverse swap's
            # Lightning leg is pinned to one channel, which (with gossip off)
            # can only reach the peer it is open to.
            log("opening Lightning channel (client -> partner2) ...")
            self.channels = open_channels(
                ep,
                opener=CLIENT, peer=PARTNER2,
                peer_listen_port=ep.ln_listen_partner2,
                num_channels=1,
                capacity_btc=self.args.channel_btc,
                mine_cb=lambda n: mine(ep, self.miner_address, n),
                log=log,
                already_open=len(self.channels),
            )
            log(f"  {len(self.channels)} channel(s) OPEN in total")

        log("discovering swap provider over nostr ...")
        try:
            self.swap_npub, self.swap_offer = discover_swap_provider(
                CLIENT, expected_fee_fraction=SWAP_FEE_FRACTION, log=log,
                # Every swapserver must be on the relay before a test can rely on
                # which one the plugin ranks first: two for a failover test,
                # three in deep-hop mode (where the CHEAPEST, partner3, is the
                # one the swap has to be planned against).
                min_providers=(3 if self.args.deep_hop
                               else 2 if self.args.second_provider else 1))
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

    def _restart_client_for_gossip(self) -> None:
        """Bounce the headless client so its gossip node re-queries the graph.

        Only meaningful (and only possible) headless: under the GUI the client is
        an interactive process the rig must not kill out from under the user, so
        there we simply wait the peer's own 600s re-announcement cycle out --
        which is why ``wait_gossip_graph`` is given a timeout above it.
        """
        if self.args.gui:
            log("client is a GUI; waiting out the peer's own gossip cycle instead")
            return
        log("restarting client so its gossip node re-queries the graph ...")
        stop_daemon(CLIENT)
        start_electrum_daemon_ready(self.pm, CLIENT, log=log)
        wait_electrum_ready(CLIENT)
        wait_lightning_ready(CLIENT)
        log("  client back up")

    def _seed_gossip_peers(self) -> None:
        """Give the CLIENT's gossip node a peer to query, so the announced
        channel graph actually reaches it.

        Only the client needs this: it is the one that has to BUILD a route, and
        route-building is what needs the graph. Partner2 merely receives, and its
        channel to the partner is announced, so a payer can find it without
        partner2 knowing anything. (It is also the only node whose config we may
        still write -- the partner daemons are already running by now, and this
        has to be set offline, before load.)
        """
        assert self.ep is not None and self.partner_nodeid
        pubkey = self.partner_nodeid.split("@")[0]
        seeds = [("127.0.0.1", self.ep.ln_listen_partner, pubkey)]
        if self.needs_partner3:
            # Partner alone is not enough here. Routing needs a POLICY per
            # direction, and the one that matters for the deep hop is
            # partner2 -> partner3, which only partner2 advertises; partner3
            # advertises the direction pointing back at us, which no route out of
            # this wallet traverses.
            #
            # Learned the hard way: seeded with partner alone the graph held three
            # channels and three policies, all of them pointing the wrong way
            # across the deep hop, and every swap payment to partner3 died as
            # NoPathFound before an HTLC was ever sent -- no onion error, nothing
            # to attribute, and the test could not run. This is also why partner2
            # must still be forwarding at this point: LNGossip hangs up on a peer
            # that does not offer GOSSIP_QUERIES (see services.disable_forwarding).
            assert self.partner3_nodeid and self.partner2_nodeid
            for name, port, nodeid in (
                    ("partner2", self.ep.ln_listen_partner2, self.partner2_nodeid),
                    ("partner3", self.ep.ln_listen_partner3, self.partner3_nodeid)):
                pk = nodeid.split("@")[0]
                seeds.append(("127.0.0.1", port, pk))
                log(f"seeded client gossip peer -> {name} {pk[:16]}...")
        set_gossip_seed_peers(CLIENT, seeds)
        log(f"seeded client gossip peer -> partner {pubkey[:16]}...")

    def _first_hop_push_btc(self) -> float:
        """How much the partner can forward toward partner2. Deep-hop mode wants
        this roomy (the failure under study is partner2's, further on); plain
        gossip mode wants it tight (the sink's halving ladder must have something
        to fail against)."""
        return (DEEP_HOP_FIRST_HOP_PUSH_BTC if self.args.deep_hop
                else GOSSIP_HOP_PUSH_BTC)

    def _open_forwarding_hop(self) -> None:
        """Open the announced, deliberately under-funded PARTNER -> PARTNER2
        channel that makes multi-hop routing both possible and *limited*.

        The client holds no channel to partner2, so every payment to it must be
        routed through the partner -- and the partner can only forward what it
        holds on this hop. That is what lets a large payment fail on real
        capacity while a smaller one settles, which is the only honest way to
        exercise the liquidity sink's halving retry end to end.

        Deep-hop mode wants the opposite of "limited" HERE: the failure it studies
        belongs to partner2, one hop further on, so this hop must be roomy enough
        that our own channel peer never becomes the binding constraint.
        """
        assert self.ep is not None
        ep = self.ep
        capacity = (DEEP_HOP_FIRST_HOP_BTC if self.args.deep_hop
                    else GOSSIP_HOP_CHANNEL_BTC)
        push = self._first_hop_push_btc()
        existing = len(json.loads(electrum_cli("list_channels", inst=PARTNER)))
        log(f"opening forwarding hop (partner -> partner2, "
            f"{capacity} BTC, push {push}"
            f"{' -- roomy, deep-hop mode' if self.args.deep_hop else ''}) ...")
        hop = open_channels(
            ep,
            opener=PARTNER, peer=PARTNER2,
            peer_listen_port=ep.ln_listen_partner2,
            num_channels=1,
            capacity_btc=capacity,
            push_btc=push,
            public=True,
            mine_cb=lambda n: mine(ep, self.miner_address, n),
            log=log,
            already_open=existing,
        )
        self.hop_channel = next(
            (c for c in hop if c.get("short_channel_id")
             and c not in self.channels), hop[-1] if hop else None)
        log(f"  forwarding hop OPEN ({len(hop)} partner channel(s) in total)")

        if self.args.deep_hop:
            self._open_deep_hop()

        # A channel is only announced once its funding tx is 6 deep, and only
        # routable once its POLICIES have propagated. Bury everything, then wait
        # for the client's own channel_db to hold the full picture: our
        # client->partner channels plus this hop, both directions each.
        log("burying funding txs so channels can be announced ...")
        mine(ep, self.miner_address, 8)
        wait_wallet_height(CLIENT, ep)

        # Restart the client so its gossip node re-queries the channel range.
        #
        # Without this the graph takes TEN MINUTES to arrive, for a dull reason:
        # a node pushes its own announcements to a peer 10s after that peer
        # connects and then only every 600s (`Peer._send_own_gossip`). The
        # client's gossip node connected before these channels existed, so it
        # missed the first push and the next one is ten minutes out. A reconnect
        # re-runs `query_channel_range` over a span that now CONTAINS the
        # channels, so the graph lands in seconds instead.
        self._restart_client_for_gossip()

        expected_channels = self.args.channels + 1 + (1 if self.args.deep_hop else 0)
        # One policy per channel is the baseline (see wait_gossip_graph: only the
        # directions pointing AWAY from us ever arrive). Deep-hop mode needs one
        # more than that -- partner2's own update for partner2 -> partner3, the
        # forward direction across the deep hop, without which the path-finder
        # knows the channel exists but will not route over it.
        expected_policies = expected_channels + (1 if self.args.deep_hop else 0)
        log(f"waiting for the client's gossip graph "
            f"({expected_channels} channels, {expected_policies} policies) ...")
        wait_gossip_graph(
            CLIENT,
            min_channels=expected_channels,
            min_policies=expected_policies,
            mine_cb=lambda n: mine(ep, self.miner_address, n),
            log=log,
            # Headless this resolves in seconds (we just reconnected); under the
            # GUI we have to outlast the peer's 600s re-announcement cycle.
            timeout=180.0 if not self.args.gui else 780.0,
        )
        # Knowing the topology is not the same as being able to route over it.
        probe_routed_payment(payer=CLIENT, payee=PARTNER2,
                             amount_sat=GOSSIP_PROBE_SAT, log=log)

        if self.args.deep_hop:
            # Only NOW, with the graph collected and proven routable, does
            # partner2 stop relaying. Doing it any earlier costs us the gossip
            # (see services.disable_forwarding); doing it here leaves the route
            # to partner3 intact on paper and fails it at partner2 in practice,
            # which is exactly the shape the attribution test needs.
            log("disabling forwarding on partner2 (deep-hop mode): "
                "routes through it stay findable, but HTLCs now fail there")
            disable_forwarding(PARTNER2)

    def _open_deep_hop(self) -> None:
        """Open the announced PARTNER3 -> PARTNER2 channel that puts a second
        forwarding node between the client and a swap provider.

        Opened FROM PARTNER3, so the direction never depends on partner2's
        forwarding state: ``channel_establishment_flow`` raises "Cannot create
        public channels" for a node with forwarding off, and though partner2 is
        still forwarding at this point in bring-up, opening from the far side
        keeps this correct if that ever changes.

        The push goes to PARTNER2 because it is partner3's INBOUND, and a
        swapserver advertises ``max_forward = num_sats_can_receive()``: push too
        little and partner3 advertises a ceiling below the swap the plugin wants,
        is never ranked, and the whole path is never exercised.
        """
        assert self.ep is not None
        existing = len(json.loads(electrum_cli("list_channels", inst=PARTNER3)))
        log(f"opening deep hop (partner3 -> partner2, {DEEP_HOP_CHANNEL_BTC} BTC, "
            f"push {DEEP_HOP_PUSH_BTC}) ...")
        deep = open_channels(
            self.ep,
            opener=PARTNER3, peer=PARTNER2,
            peer_listen_port=self.ep.ln_listen_partner2,
            num_channels=1,
            capacity_btc=DEEP_HOP_CHANNEL_BTC,
            push_btc=DEEP_HOP_PUSH_BTC,
            public=True,
            mine_cb=lambda n: mine(self.ep, self.miner_address, n),
            log=log,
            already_open=existing,
        )
        self.deep_hop_channel = deep[-1] if deep else None
        log(f"  deep hop OPEN ({len(deep)} partner3 channel(s) in total)")

    def _bring_up_sink_stub(self) -> None:
        """Gossip mode only: a second LNURL-pay endpoint that mints its invoices
        from PARTNER2, so paying it is a routed two-hop payment. Pointed at by
        the sink e2e; the plugin's own sink address stays unset by default."""
        assert self.sink_stub_port is not None
        stub = LnurlPayStub(
            self.sink_stub_port,
            invoice_provider=partner2_lightning_invoice,
            cert_dir=paths.RUN_DIR / "sink-stub",
            username="liquidity_sink",
        )
        stub.start()
        stub.trust_in_certifi(paths.certifi_ca_bundle())
        self.sink_stub = stub
        log(f"LNURL liquidity-sink stub up at {stub.base_url} "
            f"(address {stub.lightning_address}; invoices minted by partner2)")
        self._log_if_rebound(stub, "sink-stub")

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
        self._log_if_rebound(stub, "lnurl-stub")

    @staticmethod
    def _log_if_rebound(stub: LnurlPayStub, label: str) -> None:
        """Note a stub that had to move off the port allocate() announced, so the
        two log lines can be reconciled without guessing."""
        if stub.rebound_from is not None:
            log(f"  note: {label} port {stub.rebound_from} was taken by then; "
                f"rebound to {stub.port}")

    def _bring_up_partner(self) -> None:
        log("starting headless swap-partner Electrum daemon ...")
        start_electrum_daemon_ready(self.pm, PARTNER, log=log)
        wait_electrum_ready(PARTNER)
        self.partner_nodeid = wait_lightning_ready(PARTNER)
        bal = wallet_balance(PARTNER)
        log(f"swap-partner online (nodeid {self.partner_nodeid[:16]}..., "
            f"balance {bal}); swapserver advertising at "
            f"{PARTNER_FEE_MILLIONTHS / 10000:.2f}%")

    def _bring_up_partner2(self) -> None:
        """Second swap provider. It undercuts PARTNER on fee, so the plugin's
        cheapest-first ranking always tries it FIRST -- which is what lets a test
        control which provider fails and which one the cascade falls back to."""
        log("starting headless SECOND swap-provider Electrum daemon ...")
        start_electrum_daemon_ready(self.pm, PARTNER2, log=log)
        wait_electrum_ready(PARTNER2)
        self.partner2_nodeid = wait_lightning_ready(PARTNER2)
        bal = wallet_balance(PARTNER2)
        log(f"swap-provider 2 online (nodeid {self.partner2_nodeid[:16]}..., "
            f"balance {bal}); swapserver advertising at "
            f"{PARTNER2_FEE_MILLIONTHS / 10000:.2f}% (undercuts partner)")

    def _bring_up_partner3(self) -> None:
        """Fourth node: a swap provider reachable only THROUGH partner2.

        It undercuts every other provider on fee, so the plugin's cheapest-first
        ranking plans its swap against this one -- which is what forces the swap's
        Lightning leg onto the two-forwarder path and lets the failure land
        somewhere other than our own channel peer."""
        log("starting headless FOURTH Electrum daemon (deep-hop provider) ...")
        start_electrum_daemon_ready(self.pm, PARTNER3, log=log)
        wait_electrum_ready(PARTNER3)
        self.partner3_nodeid = wait_lightning_ready(PARTNER3)
        bal = wallet_balance(PARTNER3)
        log(f"deep-hop provider online (nodeid {self.partner3_nodeid[:16]}..., "
            f"balance {bal}); swapserver advertising at "
            f"{PARTNER3_FEE_MILLIONTHS / 10000:.2f}% (undercuts everyone)")

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
        if self.needs_partner2:
            log(f"  partner2 wallet   : {paths.PARTNER2_WALLET_NAME}  (dir {PARTNER2.datadir})")
        if self.needs_partner3:
            log(f"  partner3 wallet   : {paths.PARTNER3_WALLET_NAME}  (dir {PARTNER3.datadir})")
        log(f"  channels open     : {len(self.channels)} x {self.args.channel_btc} BTC (50/50)")
        log(f"  swap provider npub: {self.swap_npub}")
        log(f"  swap offer        : {json.dumps(self.swap_offer)}")
        if self.lnurl_stub is not None:
            log(f"  dev-fee payout    : {self.lnurl_stub.lightning_address} "
                f"(local LNURL stub; invoices minted by the partner)")
        if self.sink_stub is not None:
            log(f"  liquidity sink    : {self.sink_stub.lightning_address} "
                f"(local LNURL stub; invoices minted by partner2, 2 hops away)")
            log(f"  forwarding hop    : partner -> partner2, "
                f"{self._first_hop_push_btc()} BTC forwardable (gossip mode)")
        if self.needs_partner3:
            log(f"  deep hop          : partner3 -> partner2, "
                f"{DEEP_HOP_CHANNEL_BTC} BTC (partner2 REFUSES to forward)")
            log(f"  deep-hop provider : partner3 at "
                f"{PARTNER3_FEE_MILLIONTHS / 10000:.2f}%, reachable only through "
                f"partner2 -> its swap payments fail at sender_idx 1")
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
        if self.sink_stub is not None:
            self.sink_stub.stop()
        stop_daemon(CLIENT)
        stop_daemon(PARTNER)
        if self.needs_partner2:
            stop_daemon(PARTNER2)
        if self.needs_partner3:
            stop_daemon(PARTNER3)
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
            "partner2_wallet": (paths.PARTNER2_WALLET_NAME
                                if self.args.second_provider else None),
            "partner2_datadir": (str(PARTNER2.datadir)
                                 if self.args.second_provider else None),
            "partner2_nodeid": self.partner2_nodeid,
            "channels": self.channels,
            "swap_provider_npub": self.swap_npub,
            "swap_offer": self.swap_offer,
            "dev_fee_payout_address": (
                self.lnurl_stub.lightning_address if self.lnurl_stub else None),
            "dev_fee_lnurl_stub_url": (
                self.lnurl_stub.base_url if self.lnurl_stub else None),
            "partner3_wallet": (paths.PARTNER3_WALLET_NAME
                                if self.needs_partner3 else None),
            "partner3_datadir": (str(PARTNER3.datadir)
                                 if self.needs_partner3 else None),
            "partner3_nodeid": self.partner3_nodeid,
            "gossip": bool(self.args.gossip),
            "deep_hop": bool(self.args.deep_hop),
            "deep_hop_channel": self.deep_hop_channel,
            "liquidity_sink_address": (
                self.sink_stub.lightning_address if self.sink_stub else None),
            "liquidity_sink_stub_url": (
                self.sink_stub.base_url if self.sink_stub else None),
            "forwarding_hop_push_sat": (
                int(self._first_hop_push_btc() * 1e8) if self.args.gossip else None),
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
    parser.add_argument("--second-provider", action="store_true",
                        help="bring up a SECOND swap provider (undercuts the "
                             "first on fee) so provider failover can be tested; "
                             "off by default -- the other e2e suites assume the "
                             "one-partner topology")
    parser.add_argument("--gossip", action="store_true",
                        help="use the real Lightning channel graph "
                             "(use_gossip=true, announced channels) instead of "
                             "trampoline mode, and add a deliberately "
                             "under-funded partner->partner2 forwarding hop so "
                             "MULTI-HOP payments can be routed -- and can fail "
                             "on capacity. Brings up partner2 as a routing "
                             "destination with no client channel, plus a second "
                             "LNURL stub minting from it. Off by default: it "
                             "changes the topology the other suites assert "
                             "against and slows bring-up with graph sync")
    parser.add_argument("--deep-hop", action="store_true",
                        help="implies --gossip. Bring up a FOURTH node "
                             "(partner3) one hop beyond partner2, undercutting "
                             "every other provider on fee, and run partner2 with "
                             "forwarding DISABLED so it refuses to relay and "
                             "answers with an onion error of its own. That makes "
                             "a swap's Lightning payment fail at a node which is "
                             "NOT the client's channel peer -- the only way to "
                             "prove the plugin does not fault the peer for it. "
                             "Off by default: a fourth daemon and another "
                             "announced channel to bury cost bring-up time")
    parser.add_argument("--no-gui", dest="gui", action="store_false",
                        help="run the client wallet headless too (no Qt GUI)")
    parser.add_argument("--display", default=None,
                        help="X DISPLAY for the GUI (default: inherit environment)")
    parser.add_argument("--ready-file", default=None,
                        help="also write the JSON readiness file here")
    parser.add_argument("--exit-when-ready", action="store_true",
                        help="bring everything up, signal readiness, then tear down (smoke test)")
    args = parser.parse_args(argv)
    # --deep-hop is a strictly deeper --gossip: it needs the real channel graph
    # (a route of more than one hop cannot be built in trampoline mode) and the
    # partner->partner2 hop it extends. Implied rather than required so callers
    # cannot get a half-configured rig by forgetting one of the two.
    if args.deep_hop:
        args.gossip = True
    return args


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
