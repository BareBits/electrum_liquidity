"""Glue-level tests for the opt-in usage statistics: the three gates that must
all be open before anything is sent, snapshot building off a real-ish wallet,
the lifetime counters, the cadence stamps, and the POST itself.

The most important test in this file is the one that proves a negative:
`test_a_full_level_3_report_contains_none_of_the_wallets_secrets` builds a
report from a wallet stuffed with an address, a txid, channel ids, peer node
ids, provider npubs, a sink Lightning address and a balance, then sweeps the
serialised report for every one of them. That is the exact claim the consent
screen makes, so it gets a test that fails loudly if a future field leaks one.

Heavy Electrum objects are faked/monkeypatched; skipped outside the venv."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from conftest import seed_settings  # noqa: E402

pkg = pytest.importorskip("electrum.plugins.inbound_liquidity")

from electrum.plugins.inbound_liquidity import (  # type: ignore  # noqa: E402
    LiquidityPlugin,
    PLUGIN_OPENED_CHANNELS_DB_KEY,
    USAGE_STATS_ALLOW_REGTEST_ENV,
    USAGE_STATS_ENDPOINT_ENV,
    USAGE_STATS_URL,
    USAGE_TOTALS_DB_KEY,
    USAGE_TOTAL_FIELDS,
)
# The PACKAGE's usage_stats, not the top-level `import usage_stats` that
# conftest's sys.path hack also makes importable. Those are two distinct module
# objects, so their exception classes are distinct too -- and a
# `pytest.raises(us.UsageReportRejected)` against the wrong one silently fails
# to match the exception the plugin actually raises. The pure-engine tests
# (test_usage_stats.py) import the top-level copy, which is fine there because
# nothing in them crosses the boundary.
us = pkg.usage_stats  # type: ignore  # noqa: E402


# --- the secrets a report must never carry -------------------------------
#
# Every one of these is planted on the wallet the sweep test builds a report
# from, and then searched for in the result.
SECRETS = {
    "onchain_address": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
    "txid": "4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b",
    "channel_id": "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00",
    "peer_node_id": "02a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "provider_npub": "npub1swapproviderxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "banned_npub": "npub1bannedproviderxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "sink_address": "someone@example.com",
    "dev_fee_address": "electrum_liqhelper@example.com",
    "wallet_name": "my-secret-savings-wallet",
}
A_BALANCE_SAT = 123_456_789


class _FakeDB:
    def __init__(self) -> None:
        self._d: Dict[str, object] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def put(self, key, value):
        self._d[key] = value


class _FakeChannel:
    def __init__(self, cid_hex: str, closed: bool = False) -> None:
        self._cid = bytes.fromhex(cid_hex)
        self._closed = closed

    @property
    def channel_id(self):
        return self._cid

    def is_closed(self) -> bool:
        return self._closed


class _FakeWallet:
    def __init__(self, channels=None, proxy=None) -> None:
        self.db = _FakeDB()
        self.saved = 0
        self.lnworker = SimpleNamespace(channels=channels or {})
        self.network = SimpleNamespace(is_connected=lambda: True, proxy=proxy,
                                       asyncio_loop=None)

    def save_db(self) -> None:
        self.saved += 1

    def basename(self) -> str:
        return SECRETS["wallet_name"]


def _plugin(**config_overrides) -> LiquidityPlugin:
    p = object.__new__(LiquidityPlugin)
    p.logger = logging.getLogger("test.inbound_liquidity.usage")
    p._plugin_version_cache = "0.6.0-beta.1"
    cfg = dict(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_OFF)
    cfg.update(config_overrides)
    p.config = SimpleNamespace(**cfg)
    seed_settings(p, **config_overrides)
    return p


@pytest.fixture(autouse=True)
def _neutral_environment(monkeypatch):
    """Every test starts with the endpoint at its shipped default and the
    regtest override off, so a stray variable in the developer's shell cannot
    change what these tests are measuring."""
    monkeypatch.delenv(USAGE_STATS_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(USAGE_STATS_ALLOW_REGTEST_ENV, raising=False)
    # constants.net is process-wide; default these tests to mainnet.
    from electrum import constants
    monkeypatch.setattr(constants, "net",
                        SimpleNamespace(NET_NAME="mainnet"), raising=False)


# --- gate 1: the opt-in ---------------------------------------------------

def test_off_by_default():
    p, w = _plugin(), _FakeWallet()
    assert p.usage_stats_level(w) == us.LEVEL_OFF
    assert not p.usage_stats_prompted(w)
    assert p.usage_stats_install_id(w) is None


def test_a_wallet_that_never_opted_in_has_no_identifier():
    """Nothing to leak, nothing to correlate, nothing to explain away."""
    p, w = _plugin(), _FakeWallet()
    assert p.usage_stats_install_id(w) is None


def test_opting_in_mints_an_identifier_and_opting_out_keeps_it():
    p, w = _plugin(), _FakeWallet()
    p.set_usage_stats_choice(w, us.LEVEL_SETTINGS)
    minted = p.usage_stats_install_id(w)
    assert us.is_install_id(minted)
    # Opting out keeps it, so a later re-opt-in is the SAME install rather than
    # a phantom second one in the receiver's one-row-per-install view.
    p.set_usage_stats_choice(w, us.LEVEL_OFF)
    assert p.usage_stats_install_id(w) == minted
    p.set_usage_stats_choice(w, us.LEVEL_COUNT)
    assert p.usage_stats_install_id(w) == minted


def test_declining_is_remembered_so_the_screen_does_not_come_back():
    p, w = _plugin(), _FakeWallet()
    p.set_usage_stats_choice(w, us.LEVEL_OFF)
    assert p.usage_stats_level(w) == us.LEVEL_OFF
    assert p.usage_stats_prompted(w), \
        "a declined choice must be recorded, or the consent screen reappears"


@pytest.mark.parametrize("bad", [4, -1, "two", None, 1.5])
def test_an_invalid_choice_is_refused(bad):
    p, w = _plugin(), _FakeWallet()
    with pytest.raises(ValueError):
        p.set_usage_stats_choice(w, bad)


@pytest.mark.parametrize("stored", [4, -1, 99, None, "", "liquidity", [], {}])
def test_an_unparseable_stored_level_reads_as_off(stored):
    """A hand-edited wallet file, or a level from a future version. The safe
    answer for an unreadable consent record is "no consent"."""
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=stored)
    assert p.usage_stats_level(_FakeWallet()) == us.LEVEL_OFF


@pytest.mark.parametrize("stored,expected", [("2", 2), (3.0, 3), (1, 1)])
def test_a_level_stored_as_a_string_or_float_still_reads(stored, expected):
    """A hand-edited wallet file may quote the number, and JSON round-trips can
    turn it into a float. Both are unambiguous, so they are honoured rather than
    silently discarding a consent the user did give."""
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=stored)
    assert p.usage_stats_level(_FakeWallet()) == expected


def test_nothing_is_sent_while_off(monkeypatch):
    p, w = _plugin(), _FakeWallet()
    posted = []
    monkeypatch.setattr(p, "_post_usage_report",
                        lambda *a, **k: posted.append(a))
    assert asyncio.run(p._maybe_send_usage_report(w)) is False
    assert posted == []


def test_not_even_a_forced_report_goes_out_while_off(monkeypatch):
    """`force` skips the CADENCE, never the gates."""
    p, w = _plugin(), _FakeWallet()
    posted = []
    monkeypatch.setattr(p, "_post_usage_report",
                        lambda *a, **k: posted.append(a))
    assert asyncio.run(p._maybe_send_usage_report(w, force=True)) is False
    assert posted == []


# --- gate 2: the endpoint kill switch ------------------------------------

def test_the_shipped_endpoint_is_the_liquidity_specific_one():
    """A path of its own, so this plugin's reports can never land in the sister
    software's population."""
    assert USAGE_STATS_URL == "https://getbarebits.com/liqanalytics/report.php"
    assert "/analytics/" not in USAGE_STATS_URL
    assert _plugin().usage_stats_endpoint() == USAGE_STATS_URL


@pytest.mark.parametrize("word", ["disabled", "off", "0", "none", "no", "",
                                  "DISABLED", "  off  "])
def test_the_kill_switch_disables_reporting(monkeypatch, word):
    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, word)
    p = _plugin()
    assert p.usage_stats_endpoint_disabled()
    assert p.usage_stats_blocked_reason() is not None


def test_the_endpoint_can_be_pointed_elsewhere(monkeypatch):
    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, "http://127.0.0.1:9/report.php")
    p = _plugin()
    assert not p.usage_stats_endpoint_disabled()
    assert p.usage_stats_endpoint() == "http://127.0.0.1:9/report.php"


def test_an_opted_in_wallet_sends_nothing_when_the_endpoint_is_off(monkeypatch):
    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, "disabled")
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_LIQUIDITY)
    w = _FakeWallet()
    posted = []
    monkeypatch.setattr(p, "_post_usage_report", lambda *a, **k: posted.append(a))
    assert asyncio.run(p._maybe_send_usage_report(w, force=True)) is False
    assert posted == []


def test_the_status_reports_no_endpoint_when_switched_off(monkeypatch):
    monkeypatch.setenv(USAGE_STATS_ENDPOINT_ENV, "disabled")
    status = _plugin().usage_stats_status(_FakeWallet())
    assert status["endpoint"] is None, \
        "a disabled deployment must not advertise an endpoint in the UI"


# --- gate 3: regtest -----------------------------------------------------

def test_regtest_is_suppressed(monkeypatch):
    """A regtest wallet is a test fixture, not a user. Counting rigs as
    installs would corrupt the very numbers this feature exists to produce."""
    from electrum import constants
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME="regtest"))
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_COUNT)
    assert p.usage_stats_network_name() == "regtest"
    assert p.usage_stats_network_suppressed()
    assert "regtest" in (p.usage_stats_blocked_reason() or "")


def test_an_opted_in_regtest_wallet_sends_nothing(monkeypatch):
    from electrum import constants
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME="regtest"))
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_LIQUIDITY)
    posted = []
    monkeypatch.setattr(p, "_post_usage_report", lambda *a, **k: posted.append(a))
    assert asyncio.run(p._maybe_send_usage_report(_FakeWallet(), force=True)) is False
    assert posted == []


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_the_rig_can_override_the_regtest_block(monkeypatch, value):
    """The rig's e2e test has to exercise a real send over a real daemon, and
    the rig is regtest. Env-only and ships off, so a shipped build cannot do
    this and the GUI cannot be made to."""
    from electrum import constants
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME="regtest"))
    monkeypatch.setenv(USAGE_STATS_ALLOW_REGTEST_ENV, value)
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_COUNT)
    assert not p.usage_stats_network_suppressed()
    assert p.usage_stats_blocked_reason() is None


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_a_non_affirmative_override_does_not_unblock_regtest(monkeypatch, value):
    from electrum import constants
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME="regtest"))
    monkeypatch.setenv(USAGE_STATS_ALLOW_REGTEST_ENV, value)
    assert _plugin().usage_stats_network_suppressed()


@pytest.mark.parametrize("net_name,expected", [
    ("mainnet", "mainnet"), ("testnet", "testnet"), ("testnet4", "testnet4"),
    ("signet", "signet"), ("regtest", "regtest"),
    ("simnet", "other"), ("mutinynet", "other"), ("", "other"),
])
def test_the_network_name_is_mapped_to_the_reported_enum(monkeypatch, net_name, expected):
    from electrum import constants
    monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME=net_name))
    assert _plugin().usage_stats_network_name() == expected


def test_only_regtest_is_suppressed(monkeypatch):
    """Testnet and signet users are real users running real software."""
    from electrum import constants
    for net_name in ("mainnet", "testnet", "testnet4", "signet"):
        monkeypatch.setattr(constants, "net", SimpleNamespace(NET_NAME=net_name))
        assert not _plugin().usage_stats_network_suppressed(), net_name


# --- the snapshot ---------------------------------------------------------

def _loaded_wallet() -> _FakeWallet:
    """A wallet carrying every secret in SECRETS, plus channels and balances."""
    channels = {
        bytes.fromhex(SECRETS["channel_id"]): _FakeChannel(SECRETS["channel_id"]),
        bytes.fromhex("ab" * 32): _FakeChannel("ab" * 32),
        bytes.fromhex("cd" * 32): _FakeChannel("cd" * 32, closed=True),
    }
    w = _FakeWallet(channels=channels)
    w.db.put(PLUGIN_OPENED_CHANNELS_DB_KEY, [SECRETS["channel_id"]])
    w.db.put(USAGE_TOTALS_DB_KEY, {
        "channels_opened_total": 7, "channels_closed_total": 3,
        "force_closes_total": 1, "sats_funded_total": 1_400_000,
        "swaps_completed_total": 12, "sats_swapped_total": 860_000,
        "sink_payments_total": 5, "sats_sunk_total": 45_000,
        "dev_fee_paid_total_sat": 860,
    })
    # Things a report must never touch, planted where a careless snapshot
    # builder might reach for them.
    w.get_addresses = lambda: [SECRETS["onchain_address"]]
    w.get_balance = lambda: A_BALANCE_SAT
    return w


def _loaded_plugin() -> LiquidityPlugin:
    return _plugin(
        INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_LIQUIDITY,
        INBOUND_LIQUIDITY_AUTOMATION_ENABLED=True,
        INBOUND_LIQUIDITY_GOAL_SAT=100_000,
        INBOUND_LIQUIDITY_MAX_CHANNELS=2,
        INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT=0.9,
        INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT=25.0,
        INBOUND_LIQUIDITY_DEV_FEE_PCT=0.1,
        INBOUND_LIQUIDITY_SINK_ADDRESS=SECRETS["sink_address"],
        INBOUND_LIQUIDITY_DEV_FEE_ADDRESS=SECRETS["dev_fee_address"],
        INBOUND_LIQUIDITY_CHANNEL_PEER=SECRETS["peer_node_id"],
        INBOUND_LIQUIDITY_PREFERRED_NPUBS=SECRETS["provider_npub"],
        INBOUND_LIQUIDITY_BANNED_NPUBS=SECRETS["banned_npub"],
        INBOUND_LIQUIDITY_PREFERRED_PARTNERS=SECRETS["peer_node_id"],
    )


def test_a_full_level_3_report_contains_none_of_the_wallets_secrets():
    """THE test for this feature.

    A wallet with an address, a txid, channel ids, a peer node id, provider
    npubs, a sink Lightning address, a dev-fee address, a wallet name and a
    balance -- and a level-3 report (the most inclusive there is) built from
    it. Not one of those values may appear anywhere in the serialised report.

    Searched over the raw JSON rather than field by field, so a value
    reappearing somewhere unexpected -- nested, concatenated into a string,
    hiding in a version field -- is caught too.
    """
    p, w = _loaded_plugin(), _loaded_wallet()
    report = us.build_payload(us.LEVEL_LIQUIDITY, p.build_usage_snapshot(w),
                              now=1_762_819_200.0, install_id=us.new_install_id())
    body = json.dumps(report)

    for name, secret in SECRETS.items():
        assert secret not in body, f"the report leaked the {name}: {secret!r}"
        # ...and not a recognisable prefix of it either, which is how an
        # abbreviated identifier ("02a1b2c3…") would slip through.
        assert secret[:12] not in body, f"the report leaked part of the {name}"
    assert str(A_BALANCE_SAT) not in body, "the report leaked the wallet balance"


def test_the_sink_and_peer_are_reported_as_booleans_not_values():
    """The designed reduction: we report THAT a sink is configured, never what
    it is. This is what makes level 2 useful without being invasive."""
    p, w = _loaded_plugin(), _loaded_wallet()
    snapshot = p.build_usage_snapshot(w)
    assert snapshot.sink_configured is True
    assert snapshot.channel_peer_configured is True
    assert SECRETS["sink_address"] not in json.dumps(
        us.build_payload(2, snapshot, now=0.0, install_id=None))


def test_an_unconfigured_sink_reports_false():
    p = _plugin(INBOUND_LIQUIDITY_SINK_ADDRESS="",
                INBOUND_LIQUIDITY_CHANNEL_PEER="   ")
    snapshot = p.build_usage_snapshot(_FakeWallet())
    assert snapshot.sink_configured is False
    assert snapshot.channel_peer_configured is False


def test_preference_lists_are_reported_as_counts():
    p, w = _loaded_plugin(), _loaded_wallet()
    snapshot = p.build_usage_snapshot(w)
    assert snapshot.preferred_npubs_count == 1
    assert snapshot.banned_npubs_count == 1
    assert snapshot.preferred_partners_count == 1


@pytest.mark.parametrize("raw,expected", [
    ("", 0), ("   ", 0), ("npub1a", 1), ("npub1a,npub1b", 2),
    ("npub1a, npub1b , npub1c", 3), ("npub1a\nnpub1b", 2),
    (",,,", 0), ("npub1a,,npub1b", 2),
])
def test_counting_a_preference_list_is_robust_to_formatting(raw, expected):
    p = _plugin(INBOUND_LIQUIDITY_PREFERRED_NPUBS=raw)
    assert p.build_usage_snapshot(_FakeWallet()).preferred_npubs_count == expected


def test_channels_are_counted_not_identified():
    """Two open channels, one of them plugin-managed, one closed."""
    p, w = _loaded_plugin(), _loaded_wallet()
    snapshot = p.build_usage_snapshot(w)
    assert snapshot.channels_open_now == 2, "the closed channel must not count"
    assert snapshot.channels_managed_now == 1


def test_a_wallet_without_lightning_snapshots_cleanly():
    """A wallet with no lnworker at all must degrade to zeros, not raise: a
    report is a courtesy and must never be able to break a tick."""
    p = _loaded_plugin()
    w = _FakeWallet()
    w.lnworker = None
    snapshot = p.build_usage_snapshot(w)
    assert snapshot.channels_open_now == 0
    assert snapshot.channels_managed_now == 0


def test_the_snapshot_survives_a_wallet_whose_channels_explode():
    p = _loaded_plugin()
    w = _FakeWallet()

    class _Exploding:
        @property
        def channels(self):
            raise RuntimeError("lnworker is starting up")

    w.lnworker = _Exploding()
    snapshot = p.build_usage_snapshot(w)       # must not raise
    assert snapshot.channels_open_now == 0


def test_the_snapshot_reflects_the_chosen_settings():
    p, w = _loaded_plugin(), _loaded_wallet()
    snapshot = p.build_usage_snapshot(w)
    assert snapshot.automation_enabled is True
    assert snapshot.liquidity_goal_sat == 100_000
    assert snapshot.max_channels == 2
    assert snapshot.max_swap_fee_pct == 0.9
    assert snapshot.dev_fee_pct == 0.1


def test_the_snapshot_carries_the_environment():
    p, w = _loaded_plugin(), _loaded_wallet()
    snapshot = p.build_usage_snapshot(w)
    assert snapshot.plugin_version == "0.6.0-beta.1"
    assert snapshot.electrum_version not in ("", None)
    assert snapshot.python_version.count(".") == 1
    assert snapshot.os_family in us.OS_FAMILIES
    assert snapshot.gui_kind in us.GUI_KINDS
    assert snapshot.network == "mainnet"


def test_a_garbage_setting_does_not_break_the_snapshot():
    """Several wallet-file values are deliberately seeded as garbage elsewhere
    in this suite to prove a hand-edited file cannot abort a tick; the same has
    to hold for building a report."""
    p = _plugin(INBOUND_LIQUIDITY_GOAL_SAT="not-a-number",
                INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT="free",
                INBOUND_LIQUIDITY_MAX_CHANNELS=None)
    snapshot = p.build_usage_snapshot(_FakeWallet())
    assert snapshot.liquidity_goal_sat == 0
    assert snapshot.max_swap_fee_pct == 0.0
    payload = us.build_payload(2, snapshot, now=0.0, install_id=None)
    json.dumps(payload)        # and it still serialises


# --- the preview ----------------------------------------------------------

def test_the_preview_uses_this_wallets_real_values():
    """Not a sample: the user is agreeing to THEIR numbers being sent."""
    p, w = _loaded_plugin(), _loaded_wallet()
    text = p.usage_stats_preview(w, us.LEVEL_LIQUIDITY)
    shown = json.loads(text)
    assert shown["properties"]["liquidity_goal_sat"] == 100_000
    assert shown["properties"]["channels_opened_total"] == 7
    assert shown["level"] == 3


def test_the_preview_shows_the_placeholder_before_opting_in():
    p, w = _plugin(), _FakeWallet()
    shown = json.loads(p.usage_stats_preview(w, us.LEVEL_COUNT))
    assert shown["install_id"] == us.INSTALL_ID_PLACEHOLDER
    assert p.usage_stats_install_id(w) is None, \
        "previewing must not mint an identifier"


def test_the_preview_shows_the_real_identifier_once_opted_in():
    p, w = _plugin(), _FakeWallet()
    p.set_usage_stats_choice(w, us.LEVEL_COUNT)
    shown = json.loads(p.usage_stats_preview(w, us.LEVEL_COUNT))
    assert shown["install_id"] == p.usage_stats_install_id(w)


def test_the_preview_matches_what_would_actually_be_sent():
    """End-to-end version of the engine's preview-is-payload test, through the
    glue, with a real snapshot."""
    p, w = _loaded_plugin(), _loaded_wallet()
    now = 1_762_819_200.0
    p.set_usage_stats_choice(w, us.LEVEL_LIQUIDITY)
    shown = json.loads(p.usage_stats_preview(w, us.LEVEL_LIQUIDITY, now=now))
    would_send = us.build_payload(
        us.LEVEL_LIQUIDITY, p.build_usage_snapshot(w), now=now,
        install_id=p.usage_stats_install_id(w))
    assert shown == would_send


# --- lifetime counters ----------------------------------------------------

def test_the_counters_start_at_zero_on_a_wallet_that_has_never_reported():
    p, w = _plugin(), _FakeWallet()
    totals = p._load_usage_totals(w)
    assert set(totals) == set(USAGE_TOTAL_FIELDS)
    assert all(v == 0 for v in totals.values())


def test_bumping_a_counter_persists_it():
    p, w = _plugin(), _FakeWallet()
    p._bump_usage_totals(w, channels_opened_total=1, sats_funded_total=200_000)
    p._bump_usage_totals(w, channels_opened_total=1, sats_funded_total=300_000)
    totals = p._load_usage_totals(w)
    assert totals["channels_opened_total"] == 2
    assert totals["sats_funded_total"] == 500_000
    assert w.saved >= 2


def test_counters_are_monotonic():
    """They are lifetime totals. A negative delta is a bug upstream, and
    applying it would make the figures meaningless."""
    p, w = _plugin(), _FakeWallet()
    p._bump_usage_totals(w, channels_opened_total=5)
    p._bump_usage_totals(w, channels_opened_total=-3)
    assert p._load_usage_totals(w)["channels_opened_total"] == 5


def test_an_unknown_counter_is_refused_loudly(caplog):
    p, w = _plugin(), _FakeWallet()
    with caplog.at_level(logging.ERROR):
        p._bump_usage_totals(w, not_a_counter=1, channels_opened_total=1)
    assert "unknown usage-total counters" in caplog.text
    totals = p._load_usage_totals(w)
    assert "not_a_counter" not in totals
    assert totals["channels_opened_total"] == 1, \
        "a bad name must not discard the good ones alongside it"


def test_a_corrupt_counter_reads_as_zero_without_losing_the_others():
    p, w = _plugin(), _FakeWallet()
    w.db.put(USAGE_TOTALS_DB_KEY, {
        "channels_opened_total": "seven", "sats_funded_total": 400_000})
    totals = p._load_usage_totals(w)
    assert totals["channels_opened_total"] == 0
    assert totals["sats_funded_total"] == 400_000


def test_a_counter_failure_never_propagates():
    """Every caller sits on a path that has already committed funds. An
    exception there would lose the record of what just happened."""
    p, w = _plugin(), _FakeWallet()

    def _explode(key, value):
        raise RuntimeError("disk full")

    w.db.put = _explode
    p._bump_usage_totals(w, channels_opened_total=1)       # must not raise


def test_a_db_less_wallet_is_tolerated():
    p = _plugin()
    w = _FakeWallet()
    w.db = None
    p._bump_usage_totals(w, channels_opened_total=1)       # must not raise
    assert p._load_usage_totals(w)["channels_opened_total"] == 0


def test_the_counter_names_match_the_reported_field_names():
    """No translation table between what is counted and what is reported --
    which is only true while the two lists agree."""
    for field in USAGE_TOTAL_FIELDS:
        assert field in us.ALLOWED_PROPERTIES[us.LEVEL_LIQUIDITY], field


# --- cadence and the send path -------------------------------------------

def test_a_first_report_is_sent_and_stamped(monkeypatch):
    p, w = _loaded_plugin(), _loaded_wallet()
    sent: List[dict] = []

    async def _fake_post(wallet, payload):
        sent.append(payload)

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    assert asyncio.run(p._maybe_send_usage_report(w)) is True
    assert len(sent) == 1
    assert sent[0]["level"] == us.LEVEL_LIQUIDITY
    status = p.usage_stats_status(w)
    assert status["last_sent_ts"] > 0
    assert status["last_error"] == ""


def test_a_second_report_is_not_due_the_same_day(monkeypatch):
    p, w = _loaded_plugin(), _loaded_wallet()
    calls = []

    async def _fake_post(wallet, payload):
        calls.append(payload)

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    assert asyncio.run(p._maybe_send_usage_report(w)) is True
    assert asyncio.run(p._maybe_send_usage_report(w)) is False
    assert len(calls) == 1


def test_a_report_is_due_again_after_a_week(monkeypatch):
    p, w = _loaded_plugin(), _loaded_wallet()
    calls = []

    async def _fake_post(wallet, payload):
        calls.append(payload)

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    asyncio.run(p._maybe_send_usage_report(w))
    c = p.settings(w)
    c.INBOUND_LIQUIDITY_USAGE_STATS_LAST_SENT_TS = time.time() - 8 * 86_400
    c.INBOUND_LIQUIDITY_USAGE_STATS_LAST_ATTEMPT_TS = time.time() - 8 * 86_400
    assert asyncio.run(p._maybe_send_usage_report(w)) is True
    assert len(calls) == 2


def test_the_attempt_is_stamped_before_the_request(monkeypatch):
    """So an endpoint that is down backs off for an hour instead of being
    retried on every heartbeat."""
    p, w = _loaded_plugin(), _loaded_wallet()
    seen = {}

    async def _fake_post(wallet, payload):
        seen["attempt_at_request_time"] = float(
            p.settings(w).INBOUND_LIQUIDITY_USAGE_STATS_LAST_ATTEMPT_TS)
        raise us.UsageReportRejected("endpoint is down")

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    assert asyncio.run(p._maybe_send_usage_report(w)) is False
    assert seen["attempt_at_request_time"] > 0
    # ...and it is now backed off rather than immediately retrying.
    assert asyncio.run(p._maybe_send_usage_report(w)) is False


def test_a_rejection_is_recorded_for_the_settings_pane(monkeypatch):
    p, w = _loaded_plugin(), _loaded_wallet()

    async def _fake_post(wallet, payload):
        raise us.UsageReportRejected("the endpoint returned HTTP 400")

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    assert asyncio.run(p._maybe_send_usage_report(w)) is False
    status = p.usage_stats_status(w)
    assert "400" in status["last_error"]
    assert status["last_sent_ts"] == 0, "a refused report is not a sent report"


def test_an_unexpected_error_is_scrubbed_before_being_stored(monkeypatch):
    """The message reaches a log line and a GUI label, and can carry text from
    whatever answered the connection."""
    p, w = _loaded_plugin(), _loaded_wallet()

    async def _fake_post(wallet, payload):
        raise RuntimeError("boom\r\nX-Injected: yes\x1b[2J")

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    assert asyncio.run(p._maybe_send_usage_report(w)) is False
    stored = p.usage_stats_status(w)["last_error"]
    assert "\n" not in stored and "\r" not in stored and "\x1b" not in stored


def test_a_cancellation_is_not_swallowed(monkeypatch):
    """asyncio.CancelledError means the loop is going away, and swallowing it
    would make shutdown hang."""
    p, w = _loaded_plugin(), _loaded_wallet()

    async def _fake_post(wallet, payload):
        raise asyncio.CancelledError()

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(p._maybe_send_usage_report(w))


def test_a_forced_report_skips_the_cadence(monkeypatch):
    """What opting in does, so the user does not wait a week to see it work."""
    p, w = _loaded_plugin(), _loaded_wallet()
    calls = []

    async def _fake_post(wallet, payload):
        calls.append(payload)

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    asyncio.run(p._maybe_send_usage_report(w))
    assert asyncio.run(p._maybe_send_usage_report(w, force=True)) is True
    assert len(calls) == 2


def test_the_level_sent_is_the_level_stored(monkeypatch):
    """Switching down from 3 to 1 must actually narrow the next report."""
    p, w = _loaded_plugin(), _loaded_wallet()
    sent = []

    async def _fake_post(wallet, payload):
        sent.append(payload)

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    asyncio.run(p._maybe_send_usage_report(w, force=True))
    p.set_usage_stats_choice(w, us.LEVEL_COUNT)
    asyncio.run(p._maybe_send_usage_report(w, force=True))
    assert sent[0]["level"] == 3
    assert sent[1]["level"] == 1
    assert "liquidity_goal_sat" not in sent[1]["properties"]
    assert "channels_opened_total" not in sent[1]["properties"]


def test_sending_mints_the_identifier_if_a_headless_opt_in_skipped_it(monkeypatch):
    """A headless operator opts in with `set_setting usage_stats_level 2`,
    which does not go through set_usage_stats_choice -- so the id has to be
    minted on the way out, or the receiver would refuse the placeholder."""
    p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_SETTINGS)
    w = _loaded_wallet()
    assert p.usage_stats_install_id(w) is None
    sent = []

    async def _fake_post(wallet, payload):
        sent.append(payload)

    monkeypatch.setattr(p, "_post_usage_report", _fake_post)
    assert asyncio.run(p._maybe_send_usage_report(w)) is True
    assert us.is_install_id(sent[0]["install_id"])
    assert p.usage_stats_install_id(w) == sent[0]["install_id"]


def test_the_status_is_shaped_for_the_ui():
    p, w = _loaded_plugin(), _loaded_wallet()
    status = p.usage_stats_status(w)
    for key in ("level", "prompted", "install_id", "endpoint", "blocked_reason",
                "network", "last_sent_ts", "last_attempt_ts", "last_error",
                "interval_days"):
        assert key in status, key
    assert status["interval_days"] == 7


# --- the POST itself ------------------------------------------------------

def test_the_post_goes_through_the_wallets_proxy(monkeypatch):
    """A Tor user's usage report must not be the one request that leaks their
    IP, so the session is built the same way the update check builds one."""
    proxy = {"mode": "socks5", "host": "127.0.0.1", "port": 9050}
    p = _loaded_plugin()
    w = _FakeWallet(proxy=proxy)
    seen = {}

    class _FakeResponse:
        status = 200
        content = SimpleNamespace()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def post(self, url, data=None):
            seen["url"] = url
            seen["body"] = data
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _fake_session(got_proxy, headers=None, timeout=None):
        seen["proxy"] = got_proxy
        seen["headers"] = headers
        return _FakeSession()

    monkeypatch.setattr(pkg, "make_aiohttp_session", _fake_session)
    monkeypatch.setattr(type(p), "_read_capped",
                        staticmethod(lambda stream, limit: _ok_body()))
    payload = us.build_payload(1, p.build_usage_snapshot(w), now=0.0,
                               install_id=us.new_install_id())
    asyncio.run(p._post_usage_report(w, payload))

    assert seen["proxy"] == proxy, "the report bypassed the configured proxy"
    assert seen["url"] == USAGE_STATS_URL
    assert seen["headers"]["Content-Type"] == "application/json"
    assert json.loads(seen["body"].decode("utf-8"))["level"] == 1


async def _ok_body() -> bytes:
    return b'{"ok":true}'


def test_an_oversize_reply_is_refused(monkeypatch):
    """A hostile or broken endpoint must not make the wallet buffer an
    unbounded body, and hitting the cap is a rejection rather than a parse."""
    from electrum.plugins.inbound_liquidity import USAGE_STATS_MAX_BYTES
    p = _loaded_plugin()
    w = _FakeWallet()

    class _FakeResponse:
        status = 200
        content = SimpleNamespace()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def post(self, url, data=None):
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _huge(stream, limit):
        return b"x" * (USAGE_STATS_MAX_BYTES + 1)

    monkeypatch.setattr(pkg, "make_aiohttp_session",
                        lambda *a, **k: _FakeSession())
    monkeypatch.setattr(type(p), "_read_capped", staticmethod(_huge))
    payload = us.build_payload(1, p.build_usage_snapshot(w), now=0.0,
                               install_id=us.new_install_id())
    with pytest.raises(us.UsageReportRejected):
        asyncio.run(p._post_usage_report(w, payload))


def test_scheduling_a_report_without_a_network_is_a_no_op():
    """Called at wallet start, which can precede the network being up."""
    p = _loaded_plugin()
    w = _FakeWallet()
    w.network = None
    p._request_usage_report(w)          # must not raise


# --- the report is actually wired to something ---------------------------
#
# These two exist because the first version of this feature was NOT wired in.
# Every other test in this file calls `_maybe_send_usage_report` directly, so
# all of them passed while the plugin had no code path that would ever call it
# outside the GUI's "you just picked a level" button -- meaning a headless
# install never reported, and no wallet ever reached its weekly slot. The rig
# e2e caught it; these catch it in milliseconds.

def test_starting_a_wallet_schedules_a_usage_report(monkeypatch):
    """`start_wallet` is the ONLY place a report is scheduled on its own."""
    import inspect
    source = inspect.getsource(pkg.LiquidityPlugin.start_wallet)
    assert "_request_usage_report" in source, (
        "start_wallet does not schedule a usage report, so a headless install "
        "-- and any wallet between restarts -- would never send one")


def test_the_heartbeat_ticks_the_usage_report():
    """And the heartbeat is what lets a wallet that is never restarted reach
    its next weekly slot."""
    async def main() -> None:
        p = _plugin(INBOUND_LIQUIDITY_USAGE_STATS_LEVEL=us.LEVEL_COUNT)
        p._heartbeat_interval_sec = 0.02
        p._heartbeat_tasks = {}
        p.wallets = {}
        for attr in pkg.LiquidityPlugin._PER_WALLET_STATE_ATTRS:
            setattr(p, attr, {})
        ticks: List[Any] = []

        async def _fake_report(wallet, **kwargs):
            ticks.append(wallet)
            return False

        async def _fake_eval(wallet) -> None:
            pass

        async def _fake_update(wallet) -> None:
            pass

        p._maybe_send_usage_report = _fake_report      # type: ignore[assignment]
        p._evaluate = _fake_eval                       # type: ignore[assignment]
        p._maybe_check_for_update = _fake_update       # type: ignore[assignment]

        wallet = _FakeWallet()
        wallet.network = SimpleNamespace(
            is_connected=lambda: True, proxy=None,
            asyncio_loop=asyncio.get_running_loop())
        p.wallets[wallet] = asyncio.Lock()
        p._start_heartbeat(wallet)
        await asyncio.sleep(0.12)
        p.stop_wallet(wallet)
        assert len(ticks) >= 2, (
            "the heartbeat does not tick the usage report, so an always-on "
            "wallet would never reach its weekly slot")

    asyncio.run(main())
