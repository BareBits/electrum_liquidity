"""End-to-end test for the opt-in usage statistics, driven through the REAL
rig -- bitcoind + Fulcrum + nostr + a headless Electrum daemon actually running
the plugin -- POSTing to the REAL receiver (`scripts/analytics-endpoint/
report.php` on a local `php -S`, against real sqlite).

Nothing here is stubbed on either side. The plugin builds the report, makes the
HTTP request through Electrum's own network stack, and the deployed PHP
validates and stores it; the test then reads the receiver's sqlite file. So what
is proved is that the two halves of the contract agree in practice, not just
that each agrees with a mock of the other.

The claims, in descending order of how much they matter:

  * **Opted out, nothing is sent.** The shipped default is off, and a wallet
    that was never opted in must not contact the endpoint at all -- no request,
    no stamped timestamp, no install id minted. This is what a user relies on,
    and the only place it can be checked honestly is against a real daemon with
    a real endpoint listening to see whether anything turns up.
  * **Regtest is suppressed, and the kill switch works.** Two independent gates
    that must each be sufficient on their own, checked by opting the wallet IN
    and then closing one gate at a time.
  * **What lands is what the preview showed.** The report stored by the
    receiver is compared field for field against the preview the user would
    have been shown, which is the promise the consent screen makes.
  * **No secrets travel.** The stored payload is swept for this wallet's real
    addresses, channel ids and peer node ids -- values the test reads out of
    the live wallet, not fixtures.
  * **One row per install, and a weekly cadence.** A second report replaces the
    first rather than accumulating, and a restart inside the week sends nothing.

Heavy and slow (~6-10 min) and needs the electrum venv + docker + php with
pdo_sqlite. Function-scoped rig; it wipes ``.run`` and kills any previous rig,
so it must NOT run while a manual ``run.py`` rig is up. Gated behind
``RUN_RIG_E2E=1``.

Run:  RUN_RIG_E2E=1 .venv-electrum/bin/python -m pytest tests/test_usage_stats_e2e.py -q -s
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from typing import Any, Dict

import pytest

if os.environ.get("RUN_RIG_E2E") != "1":
    pytest.skip("set RUN_RIG_E2E=1 to run the heavy rig-based e2e test",
                allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run as run_mod  # noqa: E402
from rig import paths  # noqa: E402
from rig.analytics_endpoint import AnalyticsEndpoint, available  # noqa: E402
from rig.services import (  # noqa: E402
    CLIENT,
    electrum_cli,
    kill_inst_daemon,
    mine,
    set_client_plugin_setting,
    start_electrum_daemon_ready,
    wait_electrum_ready,
    wait_lightning_ready,
    wait_wallet_height,
)

# Re-exec'd (if needed) at IMPORT time rather than inside a fixture: this
# helper may replace the process so a later launch can find and kill the rig,
# and doing that after the endpoint fixture had already started a php server
# would strand it and lose the fixture's state with it.
run_mod._ensure_marked()

ENDPOINT_ENV = "ELECTRUM_INBOUND_LIQUIDITY_USAGE_ENDPOINT"
ALLOW_REGTEST_ENV = "ELECTRUM_INBOUND_LIQUIDITY_USAGE_ALLOW_REGTEST"


def _setcfg(key: str, value: str) -> None:
    """Set a config key -- or, for one of the plugin's settings, the CLIENT
    WALLET's value of it. The plugin's settings live in the wallet, so
    `setconfig plugins.inbound_liquidity.<key>` configures nothing."""
    prefix = "plugins.inbound_liquidity."
    if key.startswith(prefix):
        set_client_plugin_setting(key[len(prefix):], value)
        return
    electrum_cli("setconfig", key, value, inst=CLIENT)


def _cli_json(*args: str) -> Any:
    """Run a client CLI command and decode its JSON answer.

    Electrum prints a string result as a JSON string, so a command whose answer
    is itself JSON (the preview) comes back double-encoded; unwrap that once.
    """
    out = electrum_cli(*args, inst=CLIENT).strip()
    value = json.loads(out)
    if isinstance(value, str):
        value = json.loads(value)
    return value


def _usage_status() -> Dict[str, Any]:
    return _cli_json("inbound_liquidity_usage_stats_status")


def _preview(level: int) -> Dict[str, Any]:
    return _cli_json("inbound_liquidity_usage_stats_preview", str(level))


def _client_log_text() -> str:
    logs = sorted(glob.glob(str(
        paths.CLIENT_DATADIR / "regtest" / "logs" / "electrum_log_*.log")))
    return "".join(open(path, errors="replace").read() for path in logs)


def _mine(rig, n: int = 1) -> None:
    mine(rig.ep, rig.miner_address, n)
    wait_wallet_height(CLIENT, rig.ep)


def _restart_client(rig) -> None:
    """Bounce the client daemon, so it re-reads the environment and fires the
    report from ``start_wallet`` -- which is also how a user's next launch would
    pick up a freshly-changed setting."""
    kill_inst_daemon(CLIENT)
    start_electrum_daemon_ready(rig.pm, CLIENT, log=run_mod.log)
    wait_electrum_ready(CLIENT)
    wait_lightning_ready(CLIENT)


def _wallet_secrets() -> Dict[str, str]:
    """Values from the LIVE wallet that no report may ever contain.

    Read off the running daemon rather than hard-coded, so this checks the real
    wallet's real identifiers -- the things that would actually matter if they
    leaked.
    """
    secrets: Dict[str, str] = {}
    try:
        addresses = _cli_json("listaddresses")
        if addresses:
            secrets["an address"] = str(addresses[0])
    except Exception:
        pass
    try:
        for index, channel in enumerate(_cli_json("list_channels")):
            cid = channel.get("channel_id")
            node = channel.get("remote_pubkey") or channel.get("node_id")
            if cid:
                secrets[f"channel id {index}"] = str(cid)
            if node:
                secrets[f"peer node id {index}"] = str(node)
    except Exception:
        pass
    try:
        info = _cli_json("getinfo")
        if info.get("nodeid"):
            secrets["our node id"] = str(info["nodeid"])
    except Exception:
        pass
    return secrets


@pytest.fixture
def endpoint():
    reason = available()
    if reason is not None:
        pytest.skip(f"the real receiver cannot run here: {reason}")
    ep = AnalyticsEndpoint()
    ep.start()
    try:
        yield ep
    finally:
        ep.cleanup()


@pytest.fixture
def rig(endpoint, monkeypatch):
    """A rig whose client daemon points at the local receiver.

    Both variables are set BEFORE the daemon is spawned, because the rig's
    process manager hands children a copy of this process's environment. The
    regtest override is what makes a real send possible at all here: the plugin
    refuses to report from regtest otherwise, which is the behaviour one of the
    phases below then checks by taking the override away.
    """
    monkeypatch.setenv(ENDPOINT_ENV, endpoint.url)
    monkeypatch.setenv(ALLOW_REGTEST_ENV, "1")
    r = run_mod.Rig(run_mod.parse_args(["--no-gui"]))
    r.preflight()
    r.allocate()
    r.bring_up()   # opens 2 baseline channels itself; client loads the plugin
    try:
        yield r
    finally:
        r.shutdown()


def test_usage_statistics_are_silent_until_opted_in_then_report(rig, endpoint):
    # --- phase 1: opted out (the shipped default) ------------------------
    status = _usage_status()
    assert status["level"] == 0, "usage statistics must ship switched off"
    assert status["install_id"] is None, \
        "a wallet that never opted in must not have an identifier"
    assert status["endpoint"] == endpoint.url, \
        "the rig should be pointed at the local receiver"

    # Let the plugin actually run for a few ticks with the default config.
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    for _ in range(4):
        _mine(rig, 1)
        time.sleep(2)
    _restart_client(rig)
    time.sleep(5)

    endpoint.assert_nothing_arrived()
    status = _usage_status()
    assert status["last_sent_ts"] == 0, status
    assert status["last_attempt_ts"] == 0, \
        "an opted-out wallet must not even attempt a request"
    assert status["install_id"] is None

    # --- phase 2: opt in at level 3, the most inclusive ------------------
    # What the user would have been shown before agreeing. Captured BEFORE the
    # report goes out so the comparison below is against a genuine preview.
    shown = _preview(3)
    assert shown["level"] == 3
    assert shown["install_id"] == "(random id, generated if you opt in)", \
        "previewing must not mint an identifier"

    secrets = _wallet_secrets()
    assert secrets, "could not read anything off the wallet to check against"

    _setcfg("plugins.inbound_liquidity.usage_stats_level", "3")
    _restart_client(rig)

    reports = endpoint.wait_for_report(timeout=90)
    assert len(reports) == 1, reports
    stored = reports[0]

    # --- the report is the preview --------------------------------------
    payload = stored["payload"]
    assert payload["level"] == 3
    assert payload["client"] == "electrum-inbound-liquidity"
    assert payload["schema_version"] == 1
    assert set(payload["properties"]) == set(shown["properties"]), (
        "the report's fields differ from the preview's: "
        f"only in report {set(payload['properties']) - set(shown['properties'])}, "
        f"only in preview {set(shown['properties']) - set(payload['properties'])}")
    # Values too, bar the ones that legitimately move between the two calls.
    moving = {"channels_open_now", "channels_managed_now"}
    for key, value in shown["properties"].items():
        if key in moving:
            continue
        assert payload["properties"][key] == value, (
            f"{key}: preview showed {value!r}, report carried "
            f"{payload['properties'][key]!r}")

    # --- the identifier is random, and is the one the wallet kept --------
    install_id = payload["install_id"]
    assert install_id != "(random id, generated if you opt in)"
    assert len(install_id) == 36 and install_id[14] == "4", install_id
    status = _usage_status()
    assert status["install_id"] == install_id
    assert status["last_sent_ts"] > 0
    assert not status["last_error"], status["last_error"]

    # --- and it carries none of the wallet's secrets ---------------------
    body = json.dumps(payload)
    for name, secret in secrets.items():
        assert secret not in body, f"the report leaked {name}: {secret}"
        assert secret[:12] not in body, f"the report leaked part of {name}"

    # The receiver's own columns agree with the payload it stored.
    assert stored["network"] == "regtest"
    assert stored["gui_kind"] == "cmdline", \
        "the rig's client runs headless, so the report should say so"

    # --- phase 3: one row per install, not a history --------------------
    # A second report from the same wallet replaces the first.
    first_received = stored["received_at"]
    time.sleep(1.5)
    _setcfg("plugins.inbound_liquidity.usage_stats_level", "1")
    _restart_client(rig)
    # Not due yet (it reported moments ago), so force the issue the way the
    # GUI's Apply does -- by clearing the cadence stamps.
    _setcfg("plugins.inbound_liquidity.usage_stats_last_sent_ts", "0")
    _setcfg("plugins.inbound_liquidity.usage_stats_last_attempt_ts", "0")
    _restart_client(rig)

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        rows = endpoint.reports()
        if rows and rows[0]["received_at"] > first_received:
            break
        time.sleep(1)
    rows = endpoint.reports()
    assert len(rows) == 1, (
        f"the endpoint kept {len(rows)} rows; it must keep one per install")
    assert rows[0]["install_id"] == install_id, \
        "a re-report must not create a second install"
    assert rows[0]["payload"]["level"] == 1, "the stored row should be the newer one"
    # Narrowing the level actually narrows what is stored.
    assert "channels_opened_total" not in rows[0]["payload"]["properties"]
    assert "liquidity_goal_sat" not in rows[0]["payload"]["properties"]

    # --- phase 4: the weekly cadence holds ------------------------------
    before = endpoint.reports()[0]["received_at"]
    _restart_client(rig)
    time.sleep(8)
    after = endpoint.reports()[0]["received_at"]
    assert after == before, \
        "a restart inside the week sent another report; the cadence is not holding"

    # The receiver holds IPs only for the rate limiter, and nothing else.
    assert endpoint.retained_ips() >= 1


def test_regtest_and_the_kill_switch_each_stop_an_opted_in_wallet(rig, endpoint,
                                                                  monkeypatch):
    """Both gates, checked the only way that means anything: opt the wallet IN
    at the most inclusive level, confirm it really does report, then close one
    gate at a time and confirm it goes quiet."""
    _setcfg("plugins.inbound_liquidity.automation_enabled", "true")
    _setcfg("plugins.inbound_liquidity.usage_stats_level", "3")
    _restart_client(rig)
    endpoint.wait_for_report(timeout=90)
    baseline = endpoint.reports()[0]["received_at"]

    def _reset_cadence_and_restart() -> None:
        """Make a report due again, so "nothing arrived" can only be the gate
        and never the cadence."""
        _setcfg("plugins.inbound_liquidity.usage_stats_last_sent_ts", "0")
        _setcfg("plugins.inbound_liquidity.usage_stats_last_attempt_ts", "0")
        _restart_client(rig)

    # --- gate: regtest, with the override taken away ---------------------
    monkeypatch.delenv(ALLOW_REGTEST_ENV)
    _reset_cadence_and_restart()
    time.sleep(10)
    assert endpoint.reports()[0]["received_at"] == baseline, \
        "a regtest wallet reported without the override"
    status = _usage_status()
    assert status["level"] == 3, "the opt-in itself should be untouched"
    assert "regtest" in (status["blocked_reason"] or "").lower(), status

    # --- gate: the endpoint kill switch ---------------------------------
    monkeypatch.setenv(ALLOW_REGTEST_ENV, "1")          # regtest allowed again
    monkeypatch.setenv(ENDPOINT_ENV, "disabled")
    _reset_cadence_and_restart()
    time.sleep(10)
    assert endpoint.reports()[0]["received_at"] == baseline, \
        "an opted-in wallet reported with the endpoint switched off"
    status = _usage_status()
    assert status["endpoint"] is None
    assert status["level"] == 3

    # --- and with both gates open again, it reports -----------------------
    # The control for the two negatives above: proves they were the gates
    # talking and not something else having quietly broken reporting.
    monkeypatch.setenv(ENDPOINT_ENV, endpoint.url)
    _reset_cadence_and_restart()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if endpoint.reports()[0]["received_at"] > baseline:
            break
        time.sleep(1)
    assert endpoint.reports()[0]["received_at"] > baseline, \
        "reporting did not resume once both gates were open"
