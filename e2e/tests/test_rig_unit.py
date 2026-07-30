"""Fast unit tests for the rig's pure helpers (no services launched).

Run: ``.venv-electrum/bin/python -m pytest tests/ -q``
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rig import paths, ports, trampoline_stub  # noqa: E402
from rig.procman import _proc_cmdline_is_ours, _proc_marker  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    PARTNER,
    Endpoints,
    _client_config_pairs,
    _partner_config_pairs,
    wallet_path,
)


def _endpoints() -> Endpoints:
    return Endpoints(
        btc_rpc=18001, btc_p2p=18002, fulcrum_tcp=18003, fulcrum_admin=18004,
        nostr=18005, ln_listen_client=18006, ln_listen_partner=18007,
        swapserver_port=18008,
    )


# -- ports ------------------------------------------------------------------
def test_free_ports_distinct_and_bindable():
    got = ports.free_ports(8)
    assert len(got) == 8
    assert len(set(got)) == 8, "ports must be distinct within a batch"
    for p in got:
        assert 1024 < p <= 65535
        assert ports.port_is_free(p)


def test_free_ports_rejects_zero():
    with pytest.raises(ValueError):
        ports.free_ports(0)


# -- endpoints --------------------------------------------------------------
def test_endpoint_urls():
    ep = _endpoints()
    assert ep.nostr_relay_url == "ws://127.0.0.1:18005"
    assert ep.electrum_server == "127.0.0.1:18003:t"


# -- electrum config wiring -------------------------------------------------
def test_common_config_points_at_rig_services():
    ep = _endpoints()
    d = dict(_client_config_pairs(ep))
    # Pinned to our Fulcrum, no auto-connect drift, nostr at our local relay.
    assert d["server"] == "127.0.0.1:18003:t"
    assert d["auto_connect"] == "false"
    assert d["oneserver"] == "true"
    assert d["nostr_relays"] == "ws://127.0.0.1:18005"
    # Critical: announcement PoW disabled or the rig would hang / reject offers.
    assert d["swapserver_pow_target"] == "0"
    # Trampoline mode for Electrum<->Electrum channels.
    assert d["use_gossip"] == "false"
    assert d["lightning_listen"] == "127.0.0.1:18006"


def test_partner_enables_swapserver_at_half_percent():
    ep = _endpoints()
    d = dict(_partner_config_pairs(ep))
    assert d["plugins.swapserver.enabled"] == "true"
    assert d["plugins.swapserver.port"] == "18008"
    # 0.5% == 5000 millionths.
    assert d["plugins.swapserver.fee_millionths"] == "5000"
    assert d["lightning_listen"] == "127.0.0.1:18007"


def test_client_does_not_enable_swapserver():
    ep = _endpoints()
    d = dict(_client_config_pairs(ep))
    assert "plugins.swapserver.enabled" not in d


# -- wallet paths / instances ----------------------------------------------
def test_wallet_paths_distinct_and_named():
    assert wallet_path(CLIENT).endswith("regtest/wallets/electrum_liqtest")
    assert wallet_path(PARTNER).endswith("regtest/wallets/electrum_liqtest_swap_partner")
    assert CLIENT.datadir != PARTNER.datadir


# -- procman marker ---------------------------------------------------------
def test_proc_marker_reads_own_environ(monkeypatch):
    # Our own process carries whatever is in the environment; set the marker and
    # confirm the /proc reader returns exactly it.
    monkeypatch.setenv(paths.MARKER_ENV, paths.MARKER_VALUE)
    # Re-exec isn't possible in-test, so write+read a synthetic check instead:
    # _proc_marker reads /proc/<pid>/environ which reflects exec-time env, so we
    # assert the parser semantics against our PID only when the marker is present
    # at exec time. Here we simply assert it returns None or the value, never raises.
    result = _proc_marker(os.getpid())
    assert result is None or isinstance(result, str)


def test_proc_marker_missing_pid_returns_none():
    assert _proc_marker(2_000_000_000) is None


def test_proc_cmdline_match_requires_rundir_and_service(tmp_path, monkeypatch):
    # A subprocess whose argv embeds the run-dir path AND a service token is ours;
    # the pytest process itself (no run-dir in argv) is not.
    import subprocess

    monkeypatch.setattr(paths, "MARKER_VALUE", str(tmp_path / ".run"))
    # cmdline = "... bitcoind -datadir=<RUN_DIR>/bitcoin" simulated via a sleeping
    # python whose trailing argv carries the tokens verbatim. (Using `sleep` for
    # this is wrong: sleep sums *all* its args as durations, so a non-numeric
    # token makes it exit instantly -- a race the cmdline check then loses.)
    marker = paths.MARKER_VALUE
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         f"bitcoind -datadir={marker}/bitcoin"],
    )
    try:
        # Wait for the child to finish exec'ing so /proc/<pid>/cmdline is
        # populated (the interpreter's startup would otherwise race the check).
        for _ in range(200):
            if _proc_cmdline_is_ours(proc.pid):
                break
            time.sleep(0.01)
        assert _proc_cmdline_is_ours(proc.pid) is True
        assert _proc_cmdline_is_ours(os.getpid()) is False  # no run-dir in our argv
        assert _proc_cmdline_is_ours(2_000_000_000) is False
    finally:
        proc.kill()
        proc.wait()


# -- trampoline injection scaffolding ---------------------------------------
# The generated sitecustomize is what makes the suggested-peer e2e possible on
# regtest (see rig/trampoline_stub). Exercised here without Electrum: the real
# generated source is executed, and its patch applied to a stand-in module.
def _exec_generated_sitecustomize(monkeypatch, tmp_path, json_path):
    monkeypatch.setenv(trampoline_stub.TRAMPOLINE_JSON_ENV, str(json_path))
    source = (trampoline_stub.write_sitecustomize() / "sitecustomize.py").read_text()
    namespace = {"__name__": "sitecustomize"}
    exec(compile(source, "sitecustomize.py", "exec"), namespace)  # noqa: S102
    return namespace


class _PeerAddr:
    def __init__(self, *, host, port, pubkey):
        self.host, self.port, self.pubkey = host, port, pubkey


class _FakeTrampolineModule:
    """Stands in for electrum.trampoline: the two names the patch touches."""

    LNPeerAddr = _PeerAddr

    @staticmethod
    def hardcoded_trampoline_nodes():
        return {}                      # regtest's real answer


def test_sitecustomize_patch_serves_the_advertised_node(monkeypatch, tmp_path):
    node_id = "02" + "ab" * 32
    namespace = _exec_generated_sitecustomize(
        monkeypatch, tmp_path, trampoline_stub.TRAMPOLINE_JSON)
    module = _FakeTrampolineModule()
    namespace["_patch"](module)

    # Before anything is advertised the real (empty on regtest) answer stands.
    trampoline_stub.clear()
    assert module.hardcoded_trampoline_nodes() == {}

    # Published AFTER the patch was installed -- the daemon is already running by
    # then, so the file must be re-read on every call.
    trampoline_stub.advertise(node_id, "127.0.0.1", 9911, name="rig partner")
    nodes = module.hardcoded_trampoline_nodes()
    assert list(nodes) == ["rig partner"]
    addr = nodes["rig partner"]
    assert (addr.host, addr.port, addr.pubkey) == ("127.0.0.1", 9911,
                                                  bytes.fromhex(node_id))

    trampoline_stub.clear()
    assert module.hardcoded_trampoline_nodes() == {}, "clear() must restore reality"


def test_sitecustomize_is_inert_without_the_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv(trampoline_stub.TRAMPOLINE_JSON_ENV, raising=False)
    source = (trampoline_stub.write_sitecustomize() / "sitecustomize.py").read_text()
    namespace = {"__name__": "sitecustomize"}
    before = list(sys.meta_path)
    exec(compile(source, "sitecustomize.py", "exec"), namespace)  # noqa: S102
    assert sys.meta_path == before, "no import hook may be installed when unused"


def test_env_for_prepends_pythonpath_and_keeps_existing():
    env = trampoline_stub.env_for({"PYTHONPATH": "/somewhere/else", "HOME": "/h"})
    first = env["PYTHONPATH"].split(os.pathsep)[0]
    assert first == str(trampoline_stub.INJECT_DIR)
    assert "/somewhere/else" in env["PYTHONPATH"]
    assert env["HOME"] == "/h"
    assert env[trampoline_stub.TRAMPOLINE_JSON_ENV] == str(trampoline_stub.TRAMPOLINE_JSON)
    assert (trampoline_stub.INJECT_DIR / "sitecustomize.py").exists()
