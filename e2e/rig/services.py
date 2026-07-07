"""Service controllers: bitcoind, Fulcrum, nostr relay, Electrum wallets,
Lightning channels and nostr swap-provider discovery.

All functions are pure helpers driven by ``run.py``. Every long-running process
is started through the :class:`ProcessManager` so it carries the rig marker and
is torn down with the rig (never touching unrelated processes).
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths
from .procman import ProcessManager


# ==========================================================================
# Endpoints
# ==========================================================================
@dataclass(frozen=True)
class Endpoints:
    """All randomly-allocated ports for one rig launch."""

    btc_rpc: int
    btc_p2p: int
    fulcrum_tcp: int
    fulcrum_admin: int
    nostr: int
    ln_listen_client: int
    ln_listen_partner: int
    swapserver_port: int

    @property
    def nostr_relay_url(self) -> str:
        return f"ws://127.0.0.1:{self.nostr}"

    @property
    def electrum_server(self) -> str:
        # Electrum ``server`` syntax: host:port:<t|s>; ``t`` == plain TCP.
        return f"127.0.0.1:{self.fulcrum_tcp}:t"


# ==========================================================================
# bitcoind (regtest)
# ==========================================================================
def _bitcoind_argv(ep: Endpoints) -> list[str]:
    return [
        "bitcoind",
        "-regtest",
        f"-datadir={paths.BITCOIN_DATADIR}",
        f"-rpcuser={paths.RPC_USER}",
        f"-rpcpassword={paths.RPC_PASSWORD}",
        f"-rpcport={ep.btc_rpc}",
        f"-port={ep.btc_p2p}",
        "-rpcbind=127.0.0.1",
        "-rpcallowip=127.0.0.1",
        "-server=1",
        "-listen=1",
        "-txindex=1",          # required by Fulcrum to serve tx/address history
        "-fallbackfee=0.0002",
        "-blockfilterindex=0",
        # Resource caps (keep the rig light).
        f"-dbcache={paths.BITCOIND_DBCACHE_MB}",
        f"-maxmempool={paths.BITCOIND_MAXMEMPOOL_MB}",
        "-par=1",
    ]


def start_bitcoind(pm: ProcessManager, ep: Endpoints) -> None:
    paths.BITCOIN_DATADIR.mkdir(parents=True, exist_ok=True)
    pm.spawn("bitcoind", _bitcoind_argv(ep))


def bitcoin_cli(ep: Endpoints, *args: str, wallet: Optional[str] = None,
                check: bool = True) -> str:
    argv = [
        "bitcoin-cli",
        "-regtest",
        f"-datadir={paths.BITCOIN_DATADIR}",
        f"-rpcuser={paths.RPC_USER}",
        f"-rpcpassword={paths.RPC_PASSWORD}",
        f"-rpcport={ep.btc_rpc}",
    ]
    if wallet is not None:
        argv.append(f"-rpcwallet={wallet}")
    argv.extend(args)
    result = subprocess.run(argv, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"bitcoin-cli {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def wait_bitcoind(ep: Endpoints, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no rpc"
    while time.monotonic() < deadline:
        out = bitcoin_cli(ep, "getblockchaininfo", check=False)
        if out:
            try:
                json.loads(out)
                return
            except json.JSONDecodeError:
                last = "invalid json"
        else:
            last = "rpc not up"
        time.sleep(0.4)
    raise TimeoutError(f"bitcoind never came up: {last}")


def ensure_miner_wallet(ep: Endpoints) -> str:
    """Create (or load) the bitcoind miner wallet; return a fresh address."""
    existing = json.loads(bitcoin_cli(ep, "listwallets"))
    if paths.MINER_WALLET not in existing:
        # createwallet is idempotent only if not already on disk; load first.
        out = bitcoin_cli(ep, "loadwallet", paths.MINER_WALLET, check=False)
        if "error" in out.lower() or not out:
            bitcoin_cli(ep, "createwallet", paths.MINER_WALLET)
    return bitcoin_cli(ep, "getnewaddress", wallet=paths.MINER_WALLET)


def mine(ep: Endpoints, address: str, blocks: int) -> None:
    bitcoin_cli(ep, "generatetoaddress", str(blocks), address)


def fund_address(ep: Endpoints, address: str, amount_btc: float) -> str:
    """Send ``amount_btc`` from the miner wallet to ``address``; return txid."""
    return bitcoin_cli(ep, "sendtoaddress", address, f"{amount_btc:.8f}",
                       wallet=paths.MINER_WALLET)


# ==========================================================================
# Fulcrum (ElectrumX server)
# ==========================================================================
def _write_fulcrum_conf(ep: Endpoints) -> Path:
    db_dir = paths.FULCRUM_DATADIR / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    conf = paths.FULCRUM_DATADIR / "fulcrum.conf"
    conf.write_text(
        f"datadir = {db_dir}\n"
        f"bitcoind = 127.0.0.1:{ep.btc_rpc}\n"
        f"rpcuser = {paths.RPC_USER}\n"
        f"rpcpassword = {paths.RPC_PASSWORD}\n"
        f"tcp = 127.0.0.1:{ep.fulcrum_tcp}\n"
        f"admin = 127.0.0.1:{ep.fulcrum_admin}\n"
        "polltime = 1\n"          # fast block pickup on regtest
        f"db_mem = {paths.FULCRUM_DB_MEM_MB}\n"  # rocksdb cache cap (MB)
        "peering = false\n"
        "announce = false\n"
    )
    return conf


def start_fulcrum(pm: ProcessManager, ep: Endpoints) -> None:
    conf = _write_fulcrum_conf(ep)
    pm.spawn("fulcrum", [str(paths.FULCRUM_BIN), str(conf)])


def _electrumx_height(host: str, port: int) -> int:
    """Query block height via the Electrum protocol handshake."""
    handshake = json.dumps(
        {"id": 0, "method": "server.version", "params": ["rig-healthcheck", "1.4"]}
    ).encode() + b"\n"
    query = json.dumps(
        {"id": 1, "method": "blockchain.headers.subscribe", "params": []}
    ).encode() + b"\n"
    with socket.create_connection((host, port), timeout=3) as sock:
        sock.sendall(handshake + query)
        sock.settimeout(3)
        buf = b""
        while True:
            line, sep, rest = buf.partition(b"\n")
            if sep:
                message = json.loads(line)
                if message.get("id") == 1:
                    return int(message["result"]["height"])
                buf = rest
                continue
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("Fulcrum closed connection during handshake")
            buf += chunk


def wait_fulcrum(ep: Endpoints, expected_height: int, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no connection"
    while time.monotonic() < deadline:
        try:
            height = _electrumx_height("127.0.0.1", ep.fulcrum_tcp)
            if height >= expected_height:
                return
            last = f"height {height} < {expected_height}"
        except OSError as exc:
            last = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"Fulcrum not ready: {last}")


# ==========================================================================
# nostr relay (nostr-rs-relay in Docker)
# ==========================================================================
def _write_nostr_conf() -> Path:
    paths.NOSTR_DATADIR.mkdir(parents=True, exist_ok=True)
    conf = paths.NOSTR_DATADIR / "config.toml"
    # Minimal local relay: bind all interfaces inside the container (we only
    # publish it on a loopback host port), no PoW / rate limits so the swap
    # announcements and encrypted DMs flow freely.
    conf.write_text(
        '[info]\n'
        'name = "electrum_liqtest local relay"\n'
        'description = "Local-only relay for the electrum_liquidity test rig"\n'
        '\n'
        '[network]\n'
        'address = "0.0.0.0"\n'
        'port = 8080\n'
        '\n'
        '[limits]\n'
        'messages_per_sec = 0\n'
        '\n'
        '[authorization]\n'
        '# no pubkey allowlist; accept everything\n'
    )
    conf.chmod(0o644)
    return conf


def start_nostr_relay(pm: ProcessManager, ep: Endpoints) -> None:
    """Run nostr-rs-relay in the foreground under the process manager.

    Running without ``-d`` keeps the ``docker run`` client as our marked child;
    on shutdown we SIGTERM it and ``--rm`` reaps the container. The fixed,
    rig-scoped ``--name`` lets preflight/teardown ``docker rm -f`` any straggler.
    """
    conf = _write_nostr_conf()
    argv = [
        "docker", "run", "--rm",
        "--name", paths.NOSTR_CONTAINER,
        f"--memory={paths.NOSTR_MEM_MB}m",
        "-p", f"127.0.0.1:{ep.nostr}:8080",
        "-v", f"{conf}:/usr/src/app/config.toml:ro",
        paths.NOSTR_IMAGE,
    ]
    pm.spawn("nostr-relay", argv)


def wait_nostr_relay(ep: Endpoints, timeout: float = 60.0) -> None:
    """Wait until the relay accepts a websocket upgrade on its loopback port."""
    deadline = time.monotonic() + timeout
    last = "no connection"
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", ep.nostr), timeout=2) as sock:
                req = (
                    "GET / HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{ep.nostr}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    "\r\n"
                )
                sock.sendall(req.encode())
                sock.settimeout(2)
                resp = sock.recv(256)
                if b"101" in resp or b"Switching Protocols" in resp:
                    return
                last = f"unexpected response: {resp[:40]!r}"
        except OSError as exc:
            last = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"nostr relay not ready: {last}")


# ==========================================================================
# Electrum wallets
# ==========================================================================
@dataclass(frozen=True)
class ElectrumInstance:
    """One Electrum daemon+wallet: its own datadir, wallet file and role.

    ``--dir`` gives each instance a distinct embedded-daemon RPC endpoint;
    ``electrum_cli(..., inst=...)`` routes a command to the right one. ``-w``
    pins the wallet on every invocation so create/load/GUI all agree.
    """

    name: str
    datadir: Path
    wallet_name: str


CLIENT = ElectrumInstance("client", paths.CLIENT_DATADIR, paths.CLIENT_WALLET_NAME)
PARTNER = ElectrumInstance("partner", paths.PARTNER_DATADIR, paths.PARTNER_WALLET_NAME)


def wallet_path(inst: ElectrumInstance) -> str:
    return str(inst.datadir / "regtest" / "wallets" / inst.wallet_name)


def _electrum_base(inst: ElectrumInstance) -> list[str]:
    return [
        str(paths.ELECTRUM_BIN),
        "--regtest",
        f"--dir={inst.datadir}",
        "-w",
        wallet_path(inst),
    ]


def electrum_cli(*args: str, inst: ElectrumInstance, offline: bool = False,
                 check: bool = True, timeout: float = 120.0) -> str:
    argv = _electrum_base(inst)
    if offline:
        argv.append("-o")
    argv.extend(args)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"electrum[{inst.name}] {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _common_config_pairs(ep: Endpoints) -> list[tuple[str, str]]:
    """Config shared by both wallets.

    * ``server``/``oneserver``/``auto_connect=false`` pin Electrum to our
      Fulcrum over plain TCP. ``auto_connect`` MUST be false or Electrum resets
      ``server`` at network startup.
    * ``check_updates``/``dont_show_testnet_warning`` suppress GUI dialogs.
    * Lightning: ``use_gossip=false`` -> trampoline mode, which is what two
      Electrum nodes use to channel directly (matches Electrum's own
      tests/regtest swapserver harness). ``test_force_disable_mpp`` keeps seeded
      flows single-part/deterministic.
    * Nostr swap extension: point both wallets at the in-rig relay and disable
      announcement proof-of-work (default target is 30 bits -> would hang). The
      client also enforces this target when filtering offers, so it must be 0 on
      both sides or the zero-PoW offer is rejected.
    """
    return [
        ("server", ep.electrum_server),
        ("oneserver", "true"),
        ("auto_connect", "false"),
        ("check_updates", "false"),
        ("dont_show_testnet_warning", "true"),
        ("log_to_file", "true"),
        ("use_gossip", "false"),
        ("lightning_to_self_delay", "144"),
        ("test_force_disable_mpp", "true"),
        ("nostr_relays", ep.nostr_relay_url),
        ("swapserver_pow_target", "0"),
    ]


def _client_config_pairs(ep: Endpoints) -> list[tuple[str, str]]:
    # The client wallet is the user-facing one and hosts the inbound-liquidity
    # plugin; enable it here (channel_peer is set later, once the partner's LN
    # node id is known -- see run.py).
    return _common_config_pairs(ep) + [
        ("lightning_listen", f"127.0.0.1:{ep.ln_listen_client}"),
        ("plugins.inbound_liquidity.enabled", "true"),
        # Start paused so the rig can open its own baseline channels before the
        # plugin acts (otherwise it races the rig's setup for the same coins).
        # Flip the ENABLED/DISABLED slider on (Liquidity tab -> Settings) to
        # exercise it. Disabled is also the shipped default now.
        ("plugins.inbound_liquidity.automation_enabled", "false"),
    ]


# Source-of-truth location of the plugin and the internal-plugins dir inside the
# Electrum checkout it gets linked into. The rig lives in the plugin repo's
# ``e2e/`` subfolder, so RIG_ROOT is ``<repo>/e2e`` and the plugin package is one
# level up at ``<repo>/inbound_liquidity``.
PLUGIN_SRC: Path = paths.RIG_ROOT.parent / "inbound_liquidity"
PLUGIN_LINK: Path = paths.ELECTRUM_SRC / "electrum" / "plugins" / "inbound_liquidity"


def ensure_plugin_installed() -> None:
    """Symlink the inbound-liquidity plugin into Electrum's internal plugins dir.

    Internal plugins are auto-authorized (external plugins require a signed zip),
    so a symlink is the friction-free way to load our own-repo plugin. Idempotent.
    """
    if not PLUGIN_SRC.is_dir():
        raise FileNotFoundError(f"plugin source not found: {PLUGIN_SRC}")
    if PLUGIN_LINK.is_symlink():
        if PLUGIN_LINK.resolve() == PLUGIN_SRC.resolve():
            return
        PLUGIN_LINK.unlink()
    elif PLUGIN_LINK.exists():
        raise FileExistsError(f"{PLUGIN_LINK} exists and is not a symlink; refusing to overwrite")
    PLUGIN_LINK.parent.mkdir(parents=True, exist_ok=True)
    PLUGIN_LINK.symlink_to(PLUGIN_SRC.resolve())


def set_client_channel_peer(connect_str: str) -> None:
    """Point the plugin's channel opens at a preferred partner (the swap provider,
    in the rig). Written offline before the client comes up so it's read at load.
    Uses the preferred-partners list (the old single channel_peer field was folded
    into it)."""
    electrum_cli("setconfig", "plugins.inbound_liquidity.preferred_partners", connect_str,
                 inst=CLIENT, offline=True)


def set_client_dev_fee_address(address: str) -> None:
    """Point the plugin's dev-fee payout at the local LNURL stub (rig only).
    Written offline before the client comes up so it's read at load."""
    electrum_cli("setconfig", "plugins.inbound_liquidity.dev_fee_address", address,
                 inst=CLIENT, offline=True)


def partner_lightning_invoice(sat: int, *, memo: str = "electrum_liquidity dev fee") -> str:
    """Mint a bolt11 invoice for ``sat`` from the (running) swap-partner daemon.
    Used by the rig's LNURL stub to answer the client's payout invoice request, so
    a dev-fee payout flows as a real Lightning payment client -> partner."""
    btc = f"{sat / 1e8:.8f}"
    out = electrum_cli("add_request", btc, "--memo", memo, "--lightning",
                       inst=PARTNER)
    data = json.loads(out)
    invoice = data.get("lightning_invoice")
    if not invoice:
        raise RuntimeError(f"partner produced no lightning invoice: {out[:200]}")
    return invoice


def _partner_config_pairs(ep: Endpoints) -> list[tuple[str, str]]:
    """Swap-partner extras: enable the (cmdline-only) swapserver plugin, give it
    an HTTP port, and advertise LN<->onchain swaps at 0.5% (5000 millionths)."""
    return _common_config_pairs(ep) + [
        ("lightning_listen", f"127.0.0.1:{ep.ln_listen_partner}"),
        ("plugins.swapserver.enabled", "true"),
        ("plugins.swapserver.port", str(ep.swapserver_port)),
        ("plugins.swapserver.fee_millionths", "5000"),
    ]


def setup_wallet(ep: Endpoints, inst: ElectrumInstance) -> str:
    """Create a fresh (unencrypted) wallet offline, write config, return a
    funding address."""
    Path(wallet_path(inst)).parent.mkdir(parents=True, exist_ok=True)
    created = electrum_cli("create", inst=inst, offline=True)
    try:
        seed = json.loads(created).get("seed")
        if seed:
            (paths.RUN_DIR / f"wallet-seed-{inst.name}.txt").write_text(seed + "\n")
    except json.JSONDecodeError:
        pass

    pairs = _partner_config_pairs(ep) if inst is PARTNER else _client_config_pairs(ep)
    for key, value in pairs:
        electrum_cli("setconfig", key, value, inst=inst, offline=True)

    return electrum_cli("getunusedaddress", inst=inst, offline=True)


# -- daemon / GUI lifecycle -------------------------------------------------
def start_electrum_daemon(pm: ProcessManager, inst: ElectrumInstance) -> None:
    """Start a headless Electrum daemon (cmdline interface).

    The swapserver plugin is ``available_for: [cmdline]`` only, so the swap
    PARTNER must run this way for its plugin to load and advertise over nostr.
    """
    pm.spawn(f"electrum-daemon-{inst.name}", _electrum_base(inst) + ["daemon"])


def kill_inst_daemon(inst: ElectrumInstance) -> None:
    """Force-kill any Electrum daemon process for ``inst`` (by its datadir).

    Used to recover from a daemon that started its command server but stalled
    before becoming RPC-ready (observed occasionally under heavy machine load).
    Scoped strictly to this instance's ``--dir`` so nothing else is touched.
    """
    electrum_cli("stop", inst=inst, check=False, timeout=15)
    needle = f"--dir={inst.datadir}".encode()
    import os
    import signal as _signal
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if needle in cmd and b"daemon" in cmd:
            try:
                os.kill(pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass


def start_electrum_daemon_ready(pm: ProcessManager, inst: ElectrumInstance, *,
                                log, attempts: int = 3,
                                per_attempt_timeout: float = 45.0) -> None:
    """Start the daemon and wait until it is RPC-ready, restarting if it stalls.

    A freshly started Electrum daemon occasionally opens its RPC socket but
    never becomes ready (``getinfo`` keeps returning empty) under heavy load.
    Rather than fail the whole launch, kill and respawn it up to ``attempts``.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        start_electrum_daemon(pm, inst)
        try:
            wait_electrum_daemon_up(inst, timeout=per_attempt_timeout)
            electrum_load_wallet(inst)
            return
        except TimeoutError as exc:
            last_exc = exc
            log(f"  {inst.name} daemon not ready (attempt {attempt}/{attempts}); "
                f"restarting it")
            kill_inst_daemon(inst)
    raise TimeoutError(f"Electrum[{inst.name}] daemon never became ready: {last_exc}")


def start_electrum_gui(pm: ProcessManager, ep: Endpoints, inst: ElectrumInstance,
                       display: Optional[str] = None) -> None:
    """Launch the Qt GUI with an embedded daemon, pre-loading the wallet."""
    import os

    env = None
    if display is not None:
        env = dict(os.environ)
        env["DISPLAY"] = display
    argv = _electrum_base(inst) + [
        "gui", "--daemon", "-1", "-s", ep.electrum_server,
    ]
    pm.spawn(f"electrum-gui-{inst.name}", argv, env=env)


def wait_electrum_daemon_up(inst: ElectrumInstance, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = "no daemon"
    while time.monotonic() < deadline:
        try:
            json.loads(electrum_cli("getinfo", inst=inst))
            return
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        time.sleep(0.4)
    raise TimeoutError(f"Electrum[{inst.name}] daemon never came up: {last}")


def electrum_load_wallet(inst: ElectrumInstance) -> None:
    electrum_cli("load_wallet", inst=inst)


def wait_electrum_ready(inst: ElectrumInstance, timeout: float = 90.0) -> None:
    """Wait until the daemon is up, connected to Fulcrum and synced."""
    deadline = time.monotonic() + timeout
    last = "no daemon"
    while time.monotonic() < deadline:
        try:
            info = json.loads(electrum_cli("getinfo", inst=inst))
            if info.get("connected"):
                electrum_cli("wait_for_sync", inst=inst)
                return
            last = "not connected"
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"Electrum[{inst.name}] not ready: {last}")


def wait_lightning_ready(inst: ElectrumInstance, timeout: float = 30.0) -> str:
    """Wait until the wallet's Lightning node is up; return its node id."""
    deadline = time.monotonic() + timeout
    last = "lightning not up"
    while time.monotonic() < deadline:
        try:
            node_id = electrum_cli("nodeid", inst=inst)
            if node_id:
                return node_id
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise TimeoutError(f"Electrum[{inst.name}] lightning not ready: {last}")


def wallet_balance(inst: ElectrumInstance) -> dict:
    return json.loads(electrum_cli("getbalance", inst=inst))


def wait_onchain_funds(inst: ElectrumInstance, *, min_btc: float, mine_cb, log,
                       timeout: float = 90.0) -> float:
    """Wait until ``inst`` sees at least ``min_btc`` confirmed on-chain.

    ``wait_for_sync`` can return before Fulcrum has delivered the wallet's
    address histories, so the balance reads 0 immediately after sync. Poll
    ``getbalance`` (nudging a block out periodically) until the funds appear.
    Returns the confirmed balance in BTC.
    """
    from decimal import Decimal

    deadline = time.monotonic() + timeout
    last = "0"
    while time.monotonic() < deadline:
        bal = wallet_balance(inst)
        confirmed = Decimal(str(bal.get("confirmed", "0") or "0"))
        if confirmed >= Decimal(str(min_btc)):
            return float(confirmed)
        last = str(confirmed)
        mine_cb(1)
        time.sleep(1.0)
    raise TimeoutError(
        f"Electrum[{inst.name}] never saw {min_btc} BTC confirmed (last={last})")


def stop_daemon(inst: ElectrumInstance) -> None:
    """Best-effort graceful daemon stop (ignored if already down)."""
    electrum_cli("stop", inst=inst, check=False)


# ==========================================================================
# Lightning channels
# ==========================================================================
def wallet_height(inst: ElectrumInstance) -> int:
    """The opener wallet's local blockchain height (from ``getinfo``)."""
    info = json.loads(electrum_cli("getinfo", inst=inst))
    return int(info.get("blockchain_height", 0) or 0)


def wait_wallet_height(inst: ElectrumInstance, ep: Endpoints,
                       timeout: float = 60.0) -> int:
    """Block until ``inst`` has synced to bitcoind's current chain tip."""
    target = int(bitcoin_cli(ep, "getblockcount"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wallet_height(inst) >= target:
            return target
        time.sleep(0.5)
    raise TimeoutError(f"Electrum[{inst.name}] did not reach height {target}")


def open_channels(
    ep: Endpoints,
    *,
    opener: ElectrumInstance,
    peer: ElectrumInstance,
    peer_listen_port: int,
    num_channels: int,
    capacity_btc: float,
    mine_cb,
    log,
) -> list[dict]:
    """Open ``num_channels`` balanced channels from ``opener`` to ``peer``.

    Each channel is funded with ``capacity_btc`` and ``push_amount`` of half
    that, giving a 50/50 inbound/outbound split. Funding txs are confirmed by
    ``mine_cb(n)`` and the call blocks until every channel reaches state OPEN.

    Electrum derives a channel's multisig funding key from the funding tx's
    ``nlocktime`` (== current block height). Two channels to the *same* peer at
    the same height would reuse that key (Electrum rejects this), so between
    opens we mine a block and wait for the opener to *sync* to the new tip --
    advancing the height it stamps into the next funding tx.
    """
    # ``nodeid`` already returns ``<pubkey>@<host>:<port>`` when the peer has a
    # lightning_listen address configured; only append one if it is bare.
    peer_nodeid = electrum_cli("nodeid", inst=peer)
    conn = peer_nodeid if "@" in peer_nodeid else f"{peer_nodeid}@127.0.0.1:{peer_listen_port}"
    electrum_cli("add_peer", conn, inst=opener)

    push_btc = capacity_btc / 2.0
    for i in range(num_channels):
        if i > 0:
            # New height (=> new funding nlocktime => new multisig key) before
            # the next open; the opener must have *seen* it.
            mine_cb(1)
            wait_wallet_height(opener, ep)
        log(f"  opening channel {i + 1}/{num_channels} "
            f"({capacity_btc} BTC, push {push_btc}) -> {peer.name}")
        electrum_cli("open_channel", conn, f"{capacity_btc:.8f}",
                     "--push_amount", f"{push_btc:.8f}", inst=opener)
        # Confirm this funding tx before opening the next so coin selection
        # always has a settled input to choose.
        mine_cb(3)
        wait_wallet_height(opener, ep)

    return wait_channels_open(opener, expected=num_channels, mine_cb=mine_cb, log=log)


def wait_channels_open(inst: ElectrumInstance, *, expected: int, mine_cb,
                       log, timeout: float = 120.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    last = "no channels"
    while time.monotonic() < deadline:
        channels = json.loads(electrum_cli("list_channels", inst=inst))
        states = [c["state"] for c in channels]
        if len(channels) >= expected and all(s == "OPEN" for s in states):
            return channels
        last = f"states={states}"
        mine_cb(1)
        time.sleep(1.0)
    raise TimeoutError(f"channels not all OPEN: {last}")


# ==========================================================================
# Nostr swap-provider discovery
# ==========================================================================
def discover_swap_provider(
    client: ElectrumInstance,
    *,
    expected_fee_fraction: float,
    log,
    attempts: int = 12,
    query_time: int = 12,
) -> tuple[str, dict]:
    """Poll the client for swap providers over nostr until the partner's offer
    appears, set ``swapserver_npub`` to it, and return ``(npub, offer)``."""
    last = "no offers"
    for attempt in range(1, attempts + 1):
        # ``query_time`` is an *optional* Electrum CLI arg, so it must be passed
        # as ``--query_time N`` (a bare positional is rejected as unrecognized).
        out = electrum_cli("get_submarine_swap_providers", "--query_time", str(query_time),
                           inst=client, timeout=query_time + 30, check=False)
        try:
            providers = json.loads(out) if out else {}
        except json.JSONDecodeError:
            providers = {}
        if providers:
            npub, offer = next(iter(providers.items()))
            electrum_cli("setconfig", "swapserver_npub", npub, inst=client)
            log(f"  discovered swap provider {npub} "
                f"(fee {offer.get('percentage_fee')}%) on attempt {attempt}")
            return npub, offer
        last = f"attempt {attempt}: none yet"
        log(f"  {last}")
    raise TimeoutError(f"no swap provider discovered over nostr: {last}")
