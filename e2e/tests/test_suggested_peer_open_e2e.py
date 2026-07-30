"""End-to-end test for the SUGGESTED-peer channel-open path, exercised through the
REAL rig: bitcoind + Fulcrum + nostr + two headless Electrum daemons, a funded
wallet and real Lightning channels.

Every other e2e test drives the plugin's *preferred partners* path -- the rig
points ``plugins.inbound_liquidity.preferred_partners`` at the swap partner during
bring-up. This one covers the other branch: with NO preferred partner configured,
the plugin's only candidate source is Electrum's own ``LNWallet.suggest_peer()``.

That path was completely broken. ``_resolve_channel_partners`` called
``suggest_peer()`` inline from the evaluation coroutine (i.e. ON the asyncio loop)
inside a bare ``except Exception: node_id = None``:

  * on regtest in trampoline mode, ``hardcoded_trampoline_nodes()`` is empty, so
    Electrum's ``random.choice([])`` raised ``IndexError``;
  * in gossip mode, ``LNRater`` reaches ``Network.run_from_another_thread()``,
    which asserts it is NOT called from the asyncio thread.

Either way the exception was swallowed and every open was declined with "no
reachable channel partner is available", forever. The fix awaits the lookup off
the loop, collects up to ``MAX_SUGGESTED_PARTNERS`` distinct suggestions instead
of one, and reports failures instead of hiding them.

Regtest has no trampoline nodes and cannot have a gossip graph, so the rig
supplies the missing suggestion source: ``rig/trampoline_stub`` makes
``hardcoded_trampoline_nodes()`` advertise the rig's own swap partner (see that
module for how and why). Electrum then suggests it exactly as it would suggest a
real trampoline node on mainnet, and the plugin has to do the rest for real --
resolve the address, connect, negotiate and fund the channel.

What this proves: with ``preferred_partners`` empty, a live Electrum + plugin opens
a channel to a peer it learned about *only* from ``suggest_peer()``.

Heavy and slow (~4-6 min) and needs the electrum venv + docker. Function-scoped
rig; it wipes ``.run`` and kills any previous rig, so it must NOT run while a
manual ``run.py`` rig is up. Gated behind ``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_suggested_peer_open_e2e.py -q -s
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from typing import Callable, Dict, List, Set

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig import paths, trampoline_stub  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    electrum_cli,
    mine,
    wait_wallet_height,
    wallet_path,
)

# Cap each plugin open small so the plugin opens exactly one modest channel.
FUND_CAP_SAT = 1_000_000
CLOSING_STATES = {
    "SHUTDOWN", "CLOSING", "FORCE_CLOSING", "REQUESTED_FCLOSE", "CLOSED", "REDEEMED"}


def _setcfg(key: str, value: str) -> None:
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _getcfg(key: str) -> str:
    return electrum_cli("getconfig", key, inst=CLIENT).strip()


def _channels() -> List[Dict]:
    return json.loads(electrum_cli("list_channels", inst=CLIENT))


def _live_channels() -> List[Dict]:
    return [c for c in _channels() if c.get("state") not in CLOSING_STATES]


def _channel_ids() -> Set[str]:
    return {c["channel_id"] for c in _channels()}


def _wallet_db() -> Dict:
    with open(wallet_path(CLIENT)) as fh:
        return json.load(fh)


def _plugin_opened() -> List[str]:
    raw = _wallet_db().get("inbound_liquidity_plugin_opened_channels", [])
    return raw if isinstance(raw, list) else []


def _decision_log() -> List[Dict]:
    raw = _wallet_db().get("inbound_liquidity_decision_log", [])
    return raw if isinstance(raw, list) else []


def _open_actions() -> List[Dict]:
    return [e for e in _decision_log()
            if e.get("category") == "action" and e.get("kind") == "open"]


def _declines() -> List[str]:
    return [str(e.get("reason") or "") for e in _decision_log()
            if e.get("category") == "decline"]


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    if not logs:
        return ""
    with open(logs[-1], errors="replace") as fh:
        return fh.read()


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _wait_until(cond: Callable[[], bool], *, rig, timeout: float,
                period: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        _mine(rig, 1)
        time.sleep(period)
    return cond()


def _arm_open_config() -> None:
    """Config that lets the plugin open exactly one modest channel and do no swaps.
    ``one_channel_per_peer`` must be off: the rig's baseline channels already go to
    the partner, which is the only node the network can suggest here, and the guard
    would (correctly) veto a second channel to it."""
    _setcfg("plugins.inbound_liquidity.one_channel_per_peer", "false")
    _setcfg("plugins.inbound_liquidity.max_channels", "3")
    _setcfg("lightning_max_funding_sat", str(FUND_CAP_SAT))
    _setcfg("plugins.inbound_liquidity.max_opens_per_day", "1")
    _setcfg("plugins.inbound_liquidity.swap_trigger_sat", "9999999999")
    _setcfg("plugins.inbound_liquidity.swap_trigger_pct", "100")
    _setcfg("plugins.inbound_liquidity.offline_autoclose_enabled", "false")
    _setcfg("plugins.inbound_liquidity.diag_log_enabled", "true")


@pytest.fixture
def rig():
    run_mod._ensure_marked()
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    # Put the trampoline-injection hook on PYTHONPATH BEFORE anything is spawned:
    # children inherit the environment as it stands at spawn time, and the hook has
    # to be importable by the client daemon's interpreter at startup. The node list
    # itself is published later (advertise()), once the partner's node id is known.
    saved_env = dict(os.environ)
    os.environ.update(trampoline_stub.env_for(os.environ))
    r.allocate()
    try:
        r.bring_up()   # opens 2 baseline channels itself; client loads the plugin
        yield r
    finally:
        try:
            r.shutdown()
        finally:
            os.environ.clear()
            os.environ.update(saved_env)


def test_plugin_opens_to_a_suggested_peer(rig):
    baseline = _channel_ids()
    assert len(baseline) == 2, f"expected 2 baseline channels, got {len(baseline)}"

    # Strip the rig's preferred partner: from here the plugin has no configured
    # partner at all, so anything it opens to must have come from suggest_peer().
    _setcfg("plugins.inbound_liquidity.preferred_partners", "")
    assert _getcfg("plugins.inbound_liquidity.preferred_partners") in ("", '""', "null")
    assert _getcfg("plugins.inbound_liquidity.partners_strict").lower() not in ("true", "1"), \
        "strict mode would (by design) refuse to use a suggestion"
    _arm_open_config()

    # Make the rig's swap partner the network's trampoline node, which is what
    # Electrum's suggest_peer() returns in trampoline mode. The plugin still has to
    # resolve its address, connect and negotiate for real.
    # rig.partner_nodeid is a full connect string; the comparison below needs the
    # bare pubkey (advertise() strips the address itself).
    partner_pubkey = rig.partner_nodeid.split("@", 1)[0].lower()
    trampoline_stub.advertise(rig.partner_nodeid, "127.0.0.1", rig.ep.ln_listen_partner)

    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: len(_live_channels()) >= 3, rig=rig, timeout=180), (
        "plugin never opened a channel to the suggested peer; "
        f"declines={_declines()!r}"
    )
    assert _wait_until(lambda: len(_plugin_opened()) >= 1, rig=rig, timeout=60), \
        "the plugin's channel was not tagged in wallet.db"

    # The new channel is the plugin's, and it went to the suggested node.
    new_ids = _channel_ids() - baseline
    assert len(new_ids) == 1, f"expected exactly one new channel, got {new_ids}"
    new_id = next(iter(new_ids))
    assert new_id in _plugin_opened()
    new_chan = next(c for c in _channels() if c["channel_id"] == new_id)
    assert new_chan["remote_pubkey"].lower() == partner_pubkey

    # It was logged as an open action, and the config it came from is still empty --
    # so the candidate cannot have come from the preferred list.
    assert any(e.get("kind") == "open" for e in _open_actions())
    assert _getcfg("plugins.inbound_liquidity.preferred_partners") in ("", '""', "null")

    # The old bug's decline must never appear, and neither must a swallowed lookup
    # failure (the fix reports those explicitly).
    joined = " | ".join(_declines())
    assert "no reachable channel partner" not in joined, \
        f"suggested-peer resolution still failing: {joined}"
    assert "asking Electrum for a suggested peer failed" not in joined, joined
    assert "suggested-peer lookup failed" not in _client_log_text()


def test_failed_suggestion_lookup_is_reported_not_swallowed(rig):
    """The reported-bug case, reproduced and then diagnosed.

    With no preferred partner and no trampoline nodes advertised -- regtest's real
    state -- Electrum's ``suggest_peer()`` does not return "nothing": it *raises*,
    because ``random.choice(list({}.values()))`` cannot pick from an empty
    sequence. That exception used to vanish into a bare ``except``, leaving the
    user with "no reachable channel partner is available" and no way to tell a
    broken lookup from an empty network.

    Now the failure must name itself in the decline the user reads AND in the
    diagnostics log -- while still opening nothing, which was always correct.
    """
    _setcfg("plugins.inbound_liquidity.preferred_partners", "")
    _arm_open_config()
    trampoline_stub.clear()          # regtest as Electrum really finds it: no nodes
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")

    assert _wait_until(lambda: any("have funds and room to open" in r
                                   for r in _declines()), rig=rig, timeout=180), \
        f"expected an open decline; got {_declines()!r}"

    joined = " | ".join(_declines())
    assert "asking Electrum for a suggested peer failed" in joined, (
        "the swallowed lookup failure is back: the decline should name it, "
        f"got {joined!r}"
    )
    assert "IndexError" in joined, joined

    # Same story in the diagnostics log, as a first-class error event.
    diag = " ".join(
        open(path, errors="replace").read()
        for path in glob.glob(str(paths.CLIENT_DATADIR / "regtest"
                                  / "inbound_liquidity_logs" / "*.log")))
    assert "suggested-peer lookup failed" in diag, diag

    # Nothing opened: there was genuinely no partner to open to.
    assert len(_live_channels()) == 2
    assert _plugin_opened() == []
