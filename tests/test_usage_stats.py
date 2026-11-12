"""The anonymous-usage-statistics engine: levels, the allowlist, the payload,
the cadence, and the receiver's reply.

Pure-module tests -- no Electrum, no Qt, no network. What they are really
pinning is the privacy contract, which is why the first section is a list of
things that must NOT be possible:

  * no payload may carry a key outside its level's allowlist;
  * no snapshot field may exist that is never reported (dead weight) and no
    allowlist entry may exist with no field behind it (a permanent null);
  * the consent screen's preview and the real report must be the same bytes,
    because "what we show you is what we send" is the promise the whole feature
    rests on.

The companion suites are test_usage_stats_glue.py (the Electrum-facing half,
including the forbidden-substring sweep over a fully populated report),
test_usage_stats_qt.py (the consent screen and settings pane) and
test_usage_stats_receiver.py (that the PHP endpoint enforces the same list)."""
from __future__ import annotations

import json
import re

import pytest

import usage_stats as us


# A snapshot with every field set to something distinctive and non-default, so
# a test asserting a value appears cannot pass by accident on a zero.
def _full_snapshot() -> us.UsageSnapshot:
    return us.UsageSnapshot(
        plugin_version="0.6.0-beta.1",
        electrum_version="4.7.2",
        python_version="3.12",
        os_family="Linux",
        gui_kind="qt",
        network="mainnet",
        automation_enabled=True,
        manual_run_only=False,
        liquidity_goal_sat=100_000,
        max_channels=2,
        max_channel_size_sat=1_000_000,
        min_onchain_to_open_sat=60_000,
        onchain_reserve_sat=10_000,
        max_swap_fee_pct=0.9,
        swap_trigger_pct=25.0,
        swap_trigger_sat=25_000,
        min_swap_sat=50_000,
        min_outbound_sat=1_000,
        max_opens_per_day=5,
        max_closes_per_day=5,
        dev_fee_pct=0.1,
        sink_configured=True,
        channel_peer_configured=True,
        defer_sink_until_goal=True,
        disable_submarine_swaps=False,
        manage_plugin_opened_only=True,
        one_channel_per_peer=True,
        partners_strict=False,
        reliability_enabled=True,
        peer_reliability_enabled=True,
        auto_remediate_stuck_open=True,
        offline_autoclose_enabled=True,
        update_check_enabled=True,
        show_goal_in_piechart=True,
        diag_log_enabled=False,
        preferred_partners_count=2,
        banned_partners_count=1,
        preferred_npubs_count=3,
        banned_npubs_count=4,
        channels_opened_total=7,
        channels_closed_total=3,
        force_closes_total=1,
        sats_funded_total=1_400_000,
        swaps_completed_total=12,
        sats_swapped_total=860_000,
        sink_payments_total=5,
        sats_sunk_total=45_000,
        dev_fee_paid_total_sat=860,
        channels_open_now=2,
        channels_managed_now=2,
    )


NOW = 1_762_819_200.0          # a fixed instant, so payloads are comparable
AN_ID = "0e2a1c7e-5b4d-4f1a-9c3e-2b6d8a4f0e11"


# --- the contract ---------------------------------------------------------

def test_every_allowlisted_property_has_a_snapshot_field_and_vice_versa():
    """The engine asserts this at import; this is the test that says so out
    loud, so the reason is findable when it fires."""
    us._check_contract()        # raises AssertionError on any drift


def test_levels_are_cumulative_and_disjoint():
    one = set(us.allowed_properties_for_level(us.LEVEL_COUNT))
    two = set(us.allowed_properties_for_level(us.LEVEL_SETTINGS))
    three = set(us.allowed_properties_for_level(us.LEVEL_LIQUIDITY))
    assert one < two < three, "each level must be a strict superset of the last"
    # ...and no property is declared at two levels (which would make
    # allowed_properties_for_level double-count it).
    declared = [p for level in us.LEVELS for p in us.ALLOWED_PROPERTIES[level]]
    assert len(declared) == len(set(declared))


def test_level_off_allows_nothing():
    assert us.allowed_properties_for_level(us.LEVEL_OFF) == ()


@pytest.mark.parametrize("level", list(us.LEVELS))
def test_a_payload_carries_exactly_its_levels_properties(level):
    payload = us.build_payload(level, _full_snapshot(), now=NOW, install_id=AN_ID)
    assert set(payload["properties"]) == set(us.allowed_properties_for_level(level))


def test_a_lower_level_cannot_leak_a_higher_levels_field():
    """The point of the levels: choosing 1 must not send settings, and choosing
    2 must not send liquidity totals."""
    level1 = us.build_payload(1, _full_snapshot(), now=NOW, install_id=AN_ID)
    assert "liquidity_goal_sat" not in level1["properties"]
    assert "channels_opened_total" not in level1["properties"]
    level2 = us.build_payload(2, _full_snapshot(), now=NOW, install_id=AN_ID)
    assert "liquidity_goal_sat" in level2["properties"]
    assert "channels_opened_total" not in level2["properties"]
    assert "sats_funded_total" not in level2["properties"]


def test_the_envelope_is_exactly_the_seven_agreed_keys():
    """The receiver refuses an unknown envelope key, so an extra one here would
    make every report fail -- and a missing one would do the same."""
    payload = us.build_payload(1, _full_snapshot(), now=NOW, install_id=AN_ID)
    assert set(payload) == {"schema_version", "event", "client", "install_id",
                            "sent_at", "level", "properties"}
    assert payload["schema_version"] == 1
    assert payload["event"] == "usage_report"
    assert payload["client"] == "electrum-inbound-liquidity"


def test_the_client_marker_is_not_the_sister_softwares():
    """The two endpoints reject each other's reports by this marker, so if it
    ever matched, a misrouted report could land in the wrong population."""
    assert us.CLIENT_MARKER != "barebits-payserver"


# --- the preview IS the payload ------------------------------------------

@pytest.mark.parametrize("level", list(us.LEVELS))
def test_the_preview_is_the_payload(level):
    """The promise the consent screen makes. Not "the preview resembles the
    payload" -- the same object, which is why preview_json is a formatter over
    build_payload rather than a second renderer."""
    snapshot = _full_snapshot()
    shown = json.loads(us.preview_json(level, snapshot, now=NOW, install_id=AN_ID))
    sent = us.build_payload(level, snapshot, now=NOW, install_id=AN_ID)
    assert shown == sent


def test_the_preview_is_readable_json():
    text = us.preview_json(3, _full_snapshot(), now=NOW, install_id=AN_ID)
    assert text.startswith("{\n"), "the preview should be indented, not minified"
    assert '"install_id": "' + AN_ID + '"' in text


def test_the_wire_body_is_compact_and_small():
    """A level-3 report has to fit well inside the receiver's 16 KB cap."""
    body = us.encode_payload(
        us.build_payload(3, _full_snapshot(), now=NOW, install_id=AN_ID))
    assert b", " not in body, "the wire body should be compact, not pretty"
    assert len(body) < 4096, f"a level-3 report is {len(body)} bytes"
    assert json.loads(body.decode("utf-8"))["level"] == 3


def test_a_payload_is_deterministic():
    """What lets a test -- and the e2e suite -- compare a preview a user saw
    against the report that actually landed."""
    snapshot = _full_snapshot()
    first = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)
    second = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)
    assert first == second


# --- install id -----------------------------------------------------------

def test_a_new_install_id_is_a_random_uuid4():
    ids = {us.new_install_id() for _ in range(50)}
    assert len(ids) == 50, "install ids must not repeat"
    for value in ids:
        assert us.is_install_id(value)
        assert value[14] == "4", "must be version 4 (random), not time-based"


@pytest.mark.parametrize("value", [
    "", "not-a-uuid", None, 42, [],
    "0e2a1c7e-5b4d-1f1a-9c3e-2b6d8a4f0e11",     # version 1, not 4
    "0E2A1C7E-5B4D-4F1A-9C3E-2B6D8A4F0E11",     # uppercase
    "0e2a1c7e-5b4d-4f1a-9c3e-2b6d8a4f0e11 ",    # trailing space
    "0e2a1c7e-5b4d-4f1a-cc3e-2b6d8a4f0e11",     # bad variant nibble
])
def test_a_malformed_install_id_is_rejected(value):
    assert not us.is_install_id(value)


def test_a_malformed_stored_install_id_never_reaches_the_wire():
    """A hand-edited wallet file could hold anything here. It must not become a
    free-text field in the report -- the placeholder travels instead, and the
    receiver then refuses it rather than storing junk."""
    payload = us.build_payload(1, _full_snapshot(), now=NOW,
                               install_id="'; DROP TABLE reports; --")
    assert payload["install_id"] == us.INSTALL_ID_PLACEHOLDER


def test_no_install_id_yet_shows_the_placeholder():
    """The consent screen previews a complete report before any id exists."""
    payload = us.build_payload(1, _full_snapshot(), now=NOW, install_id=None)
    assert payload["install_id"] == us.INSTALL_ID_PLACEHOLDER
    assert not us.is_install_id(payload["install_id"])


# --- levels as user choices ----------------------------------------------

@pytest.mark.parametrize("level", list(us.LEVELS))
def test_each_reporting_level_is_valid(level):
    assert us.is_valid_level(level)
    assert us.is_valid_choice(level)


def test_off_is_a_valid_choice_but_not_a_reporting_level():
    """Off is something a user can choose and we must store; it is not
    something a report can be built at."""
    assert us.is_valid_choice(us.LEVEL_OFF)
    assert not us.is_valid_level(us.LEVEL_OFF)
    with pytest.raises(ValueError):
        us.build_payload(us.LEVEL_OFF, _full_snapshot(), now=NOW)


@pytest.mark.parametrize("value", [4, -1, 99, "2", 2.0, None, True, False])
def test_a_bogus_level_is_not_a_level(value):
    """`True` matters: bool is an int subclass in Python, so without the
    explicit check `is_valid_level(True)` would be True and report at level 1."""
    assert not us.is_valid_level(value)


@pytest.mark.parametrize("level", [4, -1, "2", None, True])
def test_building_at_a_bogus_level_raises(level):
    with pytest.raises(ValueError):
        us.build_payload(level, _full_snapshot(), now=NOW)


# --- defensive coercion ---------------------------------------------------
#
# Settings come from a wallet file a user may have hand-edited, so the engine
# coerces rather than trusts. A garbage value must neither break report
# building nor travel.

def test_a_non_numeric_setting_becomes_zero_rather_than_travelling():
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), liquidity_goal_sat="not-a-number")
    props = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["liquidity_goal_sat"] == 0


def test_a_negative_count_is_clamped_to_zero():
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), max_channels=-5)
    props = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["max_channels"] == 0


def test_an_absurd_amount_is_clamped_to_the_bitcoin_supply():
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), sats_funded_total=10 ** 30)
    props = us.build_payload(3, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["sats_funded_total"] == us.MAX_SATS


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_percentage_becomes_zero(bad):
    """NaN and infinity are not JSON values: json.dumps emits bare NaN/Infinity,
    which the receiver's json_decode refuses -- so the report would be lost."""
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), max_swap_fee_pct=bad)
    payload = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)
    assert payload["properties"]["max_swap_fee_pct"] == 0.0
    body = us.encode_payload(payload).decode("utf-8")
    assert "NaN" not in body and "Infinity" not in body
    json.loads(body)        # and it is parseable, which is the real assertion


def test_a_percentage_is_rounded():
    """An unrounded float is noise on the wire and makes two identical
    configurations look different in the data."""
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), dev_fee_pct=0.1 + 0.2)
    props = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["dev_fee_pct"] == 0.3


def test_an_unrecognised_enum_becomes_other_not_a_real_value():
    """Specifically NOT "mainnet": filing an unknown network under the one
    value we most need to trust would corrupt exactly the wrong number."""
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), network="fantasynet",
                                   os_family="TempleOS", gui_kind="kivy")
    props = us.build_payload(1, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["network"] == "other"
    assert props["os_family"] == "other"
    assert props["gui_kind"] == "other"


@pytest.mark.parametrize("bad", [
    "0.6.0; rm -rf /",
    "<img src=x onerror=alert(1)>",
    "1.0\nX-Injected: yes",
    "a" * 200,
    None,
    42,
])
def test_a_malformed_version_becomes_unknown(bad):
    """One of the few free-text values on the wire, so it is validated to a
    strict shape rather than passed through."""
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), plugin_version=bad)
    props = us.build_payload(1, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["plugin_version"] == "unknown"


@pytest.mark.parametrize("good", ["0.6.0", "1.2.3.4", "0.6.0-beta.1", "4.7.2", "12"])
def test_a_well_formed_version_travels_unchanged(good):
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), plugin_version=good)
    props = us.build_payload(1, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["plugin_version"] == good


def test_a_bool_in_an_int_field_does_not_become_one():
    """A bool reaching a count means the glue mapped a field wrongly, and
    silently reporting 1 would hide that."""
    import dataclasses
    snapshot = dataclasses.replace(_full_snapshot(), max_channels=True)
    props = us.build_payload(2, snapshot, now=NOW, install_id=AN_ID)["properties"]
    assert props["max_channels"] == 0


def test_every_reported_value_is_a_json_primitive():
    """Nothing nested, nothing exotic: the receiver's rules only accept
    strings, ints, floats and bools."""
    props = us.build_payload(3, _full_snapshot(), now=NOW, install_id=AN_ID)["properties"]
    for key, value in props.items():
        assert isinstance(value, (str, int, float, bool)), f"{key} is {type(value)}"


# --- timestamps -----------------------------------------------------------

def test_sent_at_is_iso_8601_utc_to_the_second():
    """The receiver matches this with a strict regex, so the format is part of
    the contract, not a cosmetic choice."""
    payload = us.build_payload(1, _full_snapshot(), now=NOW, install_id=AN_ID)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", payload["sent_at"])
    assert payload["sent_at"] == "2025-11-11T00:00:00Z"


def test_sent_at_is_utc_not_local():
    """A naive fromtimestamp would stamp local time and label it Z."""
    assert us.format_sent_at(0.0) == "1970-01-01T00:00:00Z"


# --- cadence --------------------------------------------------------------

def test_a_fresh_wallet_is_due():
    assert us.is_due(now=NOW, last_sent_ts=0.0, last_attempt_ts=0.0)


def test_not_due_within_the_week():
    assert not us.is_due(now=NOW, last_sent_ts=NOW - 86_400, last_attempt_ts=0.0)


def test_due_once_the_week_has_passed():
    assert us.is_due(now=NOW, last_sent_ts=NOW - 8 * 86_400, last_attempt_ts=0.0)


def test_a_failed_attempt_backs_off_for_an_hour_not_a_week():
    """An endpoint that was down must be retried soon -- but not on every
    heartbeat, which is what the attempt stamp prevents."""
    assert not us.is_due(now=NOW, last_sent_ts=0.0, last_attempt_ts=NOW - 60)
    assert us.is_due(now=NOW, last_sent_ts=0.0, last_attempt_ts=NOW - 7200)


def test_a_future_stamp_does_not_silence_reporting_forever():
    """A clock change (or a hand-edited file) can put a stamp in the future. A
    naive `now - last < interval` test reads that as "not due" and goes quiet
    until the clock catches up -- potentially years."""
    assert us.is_due(now=NOW, last_sent_ts=NOW + 10 * 86_400, last_attempt_ts=0.0)
    assert us.is_due(now=NOW, last_sent_ts=0.0, last_attempt_ts=NOW + 10 * 86_400)


@pytest.mark.parametrize("bad", ["soon", None, [], {}])
def test_an_unparseable_stamp_is_treated_as_due(bad):
    assert us.is_due(now=NOW, last_sent_ts=bad, last_attempt_ts=bad)


def test_the_interval_is_a_week_and_the_retry_an_hour():
    assert us.SEND_INTERVAL_SEC == 7 * 86_400
    assert us.RETRY_INTERVAL_SEC == 3_600


# --- the receiver's reply -------------------------------------------------

def test_an_explicit_ok_is_accepted():
    us.parse_response(200, b'{"ok":true}')          # does not raise


@pytest.mark.parametrize("status,body,expected", [
    (0, b"", "no response"),
    (429, b'{"error":"rate_limited"}', "rate-limiting"),
    (400, b'{"error":"unknown_field"}', "HTTP 400"),
    (500, b"", "HTTP 500"),
    (200, b"not json", "unreadable"),
    (200, b'{"ok":false}', "did not accept"),
    (200, b"{}", "did not accept"),
    (200, b'[]', "did not accept"),
    (200, b'"ok"', "did not accept"),
    (200, b'{"ok":"true"}', "did not accept"),
])
def test_anything_other_than_ok_true_is_a_rejection(status, body, expected):
    """Strict on purpose: a captive portal's login page, a proxy error or an
    HTML 200 from a misconfigured host are all reports that did not land, and
    treating one as success would stamp the weekly cadence and lose it."""
    with pytest.raises(us.UsageReportRejected) as caught:
        us.parse_response(status, body)
    assert expected in str(caught.value)


def test_a_rejection_never_quotes_the_response_body():
    """The body is attacker-controlled -- we may be talking to whatever
    intercepted the connection -- and this message reaches a log line and a GUI
    label."""
    hostile = b'{"error":"\\u001b[2JHACKED\\nX: y","ok":false}'
    with pytest.raises(us.UsageReportRejected) as caught:
        us.parse_response(400, hostile)
    message = str(caught.value)
    assert "HACKED" not in message
    assert "\n" not in message and "\x1b" not in message


def test_an_undecodable_body_is_a_rejection_not_a_crash():
    with pytest.raises(us.UsageReportRejected):
        us.parse_response(200, b"\xff\xfe\x00bad utf-8")


# --- operator-facing copy -------------------------------------------------

def test_every_level_has_copy_for_the_consent_screen():
    descriptions = us.level_descriptions()
    assert set(descriptions) == set(us.LEVELS)
    for level, description in descriptions.items():
        assert description.title and description.summary
        assert description.items, f"level {level} lists nothing it shares"


def test_the_never_sent_list_names_the_things_users_care_about():
    """The consent screen's explicit promise. Pinned so the list cannot quietly
    lose an item -- each of these is something the report genuinely never
    contains, enforced by the allowlist and by the glue's reduction of
    identifiers to counts and booleans."""
    text = " ".join(us.never_sent_items()).lower()
    for subject in ("address", "transaction", "balance", "node id", "npub",
                    "seed", "wallet name"):
        assert subject in text, f"the never-sent list does not mention {subject}"


def test_no_allowlisted_property_can_carry_an_identifier_value():
    """We say we never send balances, addresses or identifiers. The allowlist is
    where that is true or not, so this checks the names.

    Identifier CONCEPTS are allowed to appear -- that is the whole design:
    `sink_configured` and `preferred_npubs_count` are how the report describes
    a sink address and a provider list without carrying either. So the rule is
    not "never mention an npub", it is: any property whose name touches an
    identifier must be reduced to a count or a flag, which the `_count` /
    `_configured` suffix is the visible marker of. A field called
    `preferred_npubs` -- no suffix -- would fail here, which is the point.
    """
    identifier_words = ("address", "node_id", "npub", "pubkey", "peer",
                        "partners", "xpub", "wallet_name", "txid", "invoice")
    # These may never appear at all, in any form: there is no count or flag
    # shape that would make reporting a balance acceptable.
    never = ("balance", "seed", "privkey", "capacity_now", "spendable")

    for prop in us.all_allowed_properties():
        for word in never:
            assert word not in prop, f"{prop!r} looks like it carries a {word}"
        if not any(word in prop for word in identifier_words):
            continue
        # It touches an identifier concept, so it must be a NUMBER or a FLAG.
        # The type is the real guarantee: a bool cannot hold an address and an
        # int cannot hold an npub, whatever the field is called. A `str` field
        # mentioning any of these words would fail here -- which is what stops
        # someone adding `channel_peer` alongside `channel_peer_configured`.
        declared = us._SNAPSHOT_TYPES[prop]
        assert declared in (bool, int), (
            f"{prop!r} mentions an identifier and is typed {declared.__name__}; "
            f"it must be an int or a bool so it cannot carry one")
