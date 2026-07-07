"""Filesystem layout and run-scoped constants for the rig.

Everything ephemeral lives under ``RUN_DIR`` (``.run/``) so a single launch can
wipe it for a clean slate. ``MARKER_VALUE`` (the absolute run dir) is stamped
into the environment of every child process and is the *only* thing used to
discover/kill this rig's processes -- so unrelated bitcoind / Electrum / Fulcrum
instances on the machine are never touched.
"""

from __future__ import annotations

from pathlib import Path

# ---- Source trees / binaries ---------------------------------------------
RIG_ROOT: Path = Path(__file__).resolve().parent.parent
ELECTRUM_SRC: Path = RIG_ROOT / "electrum"
VENV_ELECTRUM: Path = RIG_ROOT / ".venv-electrum"
ELECTRUM_BIN: Path = VENV_ELECTRUM / "bin" / "electrum"
VENV_PYTHON: Path = VENV_ELECTRUM / "bin" / "python"
FULCRUM_BIN: Path = RIG_ROOT / "bin" / "Fulcrum"


def certifi_ca_bundle() -> Path:
    """The certifi CA bundle the venv's Electrum trusts for TLS (incl. LNURL).
    The rig appends the local LNURL-stub cert here so the client validates it
    (see rig/lnurl_stub.py). Queried from the *venv* python -- not this process's
    certifi -- since run.py may run under a different interpreter than Electrum."""
    import subprocess  # noqa: PLC0415 -- lazy; only needed when wiring the stub
    out = subprocess.run(
        [str(VENV_PYTHON), "-c", "import certifi; print(certifi.where())"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(out)

# ---- Nostr relay (Docker) -------------------------------------------------
NOSTR_IMAGE: str = "scsibug/nostr-rs-relay:latest"
# Fixed, rig-scoped container name. Only this rig ever creates it, so it is
# safe to ``docker rm -f`` on preflight/teardown without touching other work.
NOSTR_CONTAINER: str = "electrum_liqtest_nostr"

# ---- Ephemeral run state (wiped every launch) -----------------------------
RUN_DIR: Path = RIG_ROOT / ".run"
BITCOIN_DATADIR: Path = RUN_DIR / "bitcoin"
FULCRUM_DATADIR: Path = RUN_DIR / "fulcrum"
NOSTR_DATADIR: Path = RUN_DIR / "nostr"
LOG_DIR: Path = RUN_DIR / "logs"
STATE_FILE: Path = RUN_DIR / "state.json"
READY_FILE: Path = RUN_DIR / "ready.json"

# ---- Electrum wallet instances -------------------------------------------
# Each instance gets its own ``--dir`` so its embedded daemon has a distinct
# RPC endpoint; ``electrum_cli(..., inst=...)`` routes a command to the right
# one. The wallet file name is pinned via ``-w`` on every invocation.
CLIENT_DATADIR: Path = RUN_DIR / "electrum-client"
CLIENT_WALLET_NAME: str = "electrum_liqtest"
PARTNER_DATADIR: Path = RUN_DIR / "electrum-partner"
PARTNER_WALLET_NAME: str = "electrum_liqtest_swap_partner"

# ---- Bitcoind miner wallet ------------------------------------------------
MINER_WALLET: str = "rigminer"

# ---- Process-scoping marker ----------------------------------------------
# The env var stamped onto every child (and the orchestrator itself). Its value
# is the absolute RUN_DIR, unique to this rig checkout, so a marker match
# unambiguously means "spawned by this rig".
MARKER_ENV: str = "ELECTRUM_LIQTEST_RIG"
MARKER_VALUE: str = str(RUN_DIR)

# ---- Local-only regtest RPC credentials -----------------------------------
RPC_USER: str = "rig"
RPC_PASSWORD: str = "rig"

# ---- Resource caps (keep the rig light; RAM-cap preference) ---------------
BITCOIND_DBCACHE_MB: int = 64
BITCOIND_MAXMEMPOOL_MB: int = 64
FULCRUM_DB_MEM_MB: int = 64
NOSTR_MEM_MB: int = 128
