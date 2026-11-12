"""The PHP receiver enforces the same privacy contract as the Python client.

`scripts/analytics-endpoint/common.php` holds the server's copy of the
per-level field allowlist. Two copies of one contract is a thing that drifts,
and the consequence of drift is specific and bad in both directions:

  * a field the client sends but the server does not know about makes EVERY
    report fail validation -- reporting silently dies for everyone;
  * a field the server accepts but the client never sends is a hole the
    contract no longer closes.

So this suite reads the rules out of the real PHP (by running it, not by
parsing it with a regex) and asserts they agree with
`usage_stats.ALLOWED_PROPERTIES`, field for field and level for level. It then
drives the real `analytics_validate()` over genuine client payloads and over
the attacks the allowlist exists to stop.

No database and no web server here -- common.php touches sqlite only inside
analytics_db(), which validation never calls -- so these run anywhere PHP
does. The full path (a real daemon POSTing to the real report.php over HTTP,
stored in real sqlite) is e2e/tests/test_usage_stats_e2e.py.

Skipped when the php binary is unavailable."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap

import pytest

import usage_stats as us

PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(PHP is None, reason="the php binary is not installed")

ENDPOINT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "analytics-endpoint")
COMMON_PHP = os.path.join(ENDPOINT_DIR, "common.php")

AN_ID = "0e2a1c7e-5b4d-4f1a-9c3e-2b6d8a4f0e11"
NOW = 1_762_819_200.0


def _php(snippet: str, stdin: str = "") -> str:
    """Run a PHP snippet with common.php loaded; return its stdout.

    The snippet is handed the endpoint's real functions, so what these tests
    exercise is the code that will be deployed, not a transcription of it.
    """
    code = f"<?php require {json.dumps(COMMON_PHP)};\n{textwrap.dedent(snippet)}"
    result = subprocess.run(
        [PHP, "-r", code[len("<?php "):]],
        input=stdin, capture_output=True, text=True, timeout=60,
        cwd=ENDPOINT_DIR)
    assert result.returncode == 0, (
        f"php exited {result.returncode}\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}")
    return result.stdout


def _php_json(snippet: str, stdin: str = "") -> object:
    out = _php(snippet, stdin)
    try:
        return json.loads(out)
    except json.JSONDecodeError:  # pragma: no cover - diagnostic path
        raise AssertionError(f"php did not print JSON:\n{out}")


@pytest.fixture(scope="module")
def php_rules() -> dict:
    """The receiver's per-level field rules, as the receiver itself sees them."""
    rules = _php_json(
        'echo json_encode(analytics_rules());',
    )
    assert isinstance(rules, dict)
    return rules


def test_php_is_syntactically_valid():
    """A deployed file with a syntax error is a dead endpoint, and the client
    treats a dead endpoint as a transient failure -- so it would retry weekly
    forever rather than telling anyone."""
    for name in ("common.php", "report.php", "summary.php"):
        result = subprocess.run([PHP, "-l", os.path.join(ENDPOINT_DIR, name)],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"{name}: {result.stdout}{result.stderr}"


# --- the two allowlists agree --------------------------------------------

def test_the_receiver_knows_the_same_three_levels(php_rules):
    assert sorted(int(k) for k in php_rules) == sorted(us.LEVELS)


@pytest.mark.parametrize("level", list(us.LEVELS))
def test_each_levels_fields_match_exactly(php_rules, level):
    """The core of this suite. Set equality in BOTH directions, so neither side
    can gain or lose a field unnoticed."""
    php_fields = set(php_rules[str(level)])
    python_fields = set(us.ALLOWED_PROPERTIES[level])
    missing_server = python_fields - php_fields
    missing_client = php_fields - python_fields
    assert not missing_server, (
        f"level {level}: the client sends these but the receiver would REFUSE "
        f"the whole report: {sorted(missing_server)}")
    assert not missing_client, (
        f"level {level}: the receiver accepts these but the client never sends "
        f"them, so every report would be refused as incomplete: "
        f"{sorted(missing_client)}")


def test_the_total_field_count_matches():
    php_count = len(_php_json('echo json_encode(analytics_rules_for_level(3));'))
    assert php_count == len(us.all_allowed_properties())


def test_the_markers_and_schema_version_match():
    constants = _php_json(
        'echo json_encode(["client" => ANALYTICS_CLIENT_MARKER,'
        ' "event" => ANALYTICS_EVENT,'
        ' "schema" => ANALYTICS_SCHEMA_VERSION,'
        ' "max_body" => ANALYTICS_MAX_BODY_BYTES]);')
    assert constants["client"] == us.CLIENT_MARKER
    assert constants["event"] == us.EVENT
    assert constants["schema"] == us.SCHEMA_VERSION


def test_the_receivers_enums_match_the_clients():
    enums = _php_json(
        'echo json_encode(["os" => ANALYTICS_OS_FAMILIES,'
        ' "gui" => ANALYTICS_GUI_KINDS, "net" => ANALYTICS_NETWORKS]);')
    assert set(enums["os"]) == set(us.OS_FAMILIES)
    assert set(enums["gui"]) == set(us.GUI_KINDS)
    assert set(enums["net"]) == set(us.NETWORKS)


def test_the_receiver_does_not_share_the_sister_softwares_marker():
    """Each endpoint refuses the other's reports, which is what keeps the two
    populations separate even if a report is ever misrouted."""
    marker = _php_json('echo json_encode(ANALYTICS_CLIENT_MARKER);')
    assert marker == "electrum-inbound-liquidity"
    assert marker != "barebits-payserver"


# --- the receiver accepts what the client actually builds -----------------

def _snapshot() -> us.UsageSnapshot:
    return us.UsageSnapshot(
        plugin_version="0.6.0-beta.1", electrum_version="4.7.2",
        python_version="3.12", os_family="Linux", gui_kind="qt",
        network="mainnet", automation_enabled=True, liquidity_goal_sat=100_000,
        max_channels=2, max_channel_size_sat=1_000_000,
        min_onchain_to_open_sat=60_000, onchain_reserve_sat=10_000,
        max_swap_fee_pct=0.9, swap_trigger_pct=25.0, swap_trigger_sat=25_000,
        min_swap_sat=50_000, min_outbound_sat=1_000, max_opens_per_day=5,
        max_closes_per_day=5, dev_fee_pct=0.1, sink_configured=True,
        channel_peer_configured=True, defer_sink_until_goal=True,
        manage_plugin_opened_only=True, one_channel_per_peer=True,
        reliability_enabled=True, peer_reliability_enabled=True,
        auto_remediate_stuck_open=True, offline_autoclose_enabled=True,
        update_check_enabled=True, show_goal_in_piechart=True,
        preferred_partners_count=2, banned_partners_count=1,
        preferred_npubs_count=3, banned_npubs_count=4,
        channels_opened_total=7, channels_closed_total=3, force_closes_total=1,
        sats_funded_total=1_400_000, swaps_completed_total=12,
        sats_swapped_total=860_000, sink_payments_total=5, sats_sunk_total=45_000,
        dev_fee_paid_total_sat=860, channels_open_now=2, channels_managed_now=2)


def _validate(payload: object) -> dict:
    """Run the receiver's real validator over a payload handed in on stdin."""
    return _php_json(
        'echo json_encode(analytics_validate('
        'json_decode(stream_get_contents(STDIN), true)));',
        stdin=json.dumps(payload))


@pytest.mark.parametrize("level", list(us.LEVELS))
def test_a_genuine_client_payload_is_accepted(level):
    """The end of the contract that matters most: what the client really builds
    -- not a hand-written sample -- passes the real validator."""
    payload = us.build_payload(level, _snapshot(), now=NOW, install_id=AN_ID)
    result = _validate(payload)
    assert result["ok"] is True, result
    assert result["report"]["level"] == level
    assert set(result["report"]["properties"]) == set(payload["properties"])


def test_a_default_snapshot_is_accepted():
    """A wallet that has done nothing at all: every counter zero, every flag
    false. The zeros must not look like missing fields."""
    payload = us.build_payload(3, us.UsageSnapshot(), now=NOW, install_id=AN_ID)
    assert _validate(payload)["ok"] is True


def test_the_values_survive_the_round_trip_unchanged():
    payload = us.build_payload(3, _snapshot(), now=NOW, install_id=AN_ID)
    accepted = _validate(payload)["report"]["properties"]
    original = payload["properties"]
    for key, value in original.items():
        assert accepted[key] == value, f"{key}: {value!r} became {accepted[key]!r}"


def test_clamped_extreme_values_are_still_accepted():
    """The client clamps to MAX_SATS; the receiver's ceiling must not be lower,
    or a wallet with absurd stored settings would have reports refused."""
    import dataclasses
    snapshot = dataclasses.replace(_snapshot(), sats_funded_total=10 ** 30,
                                   liquidity_goal_sat=10 ** 30)
    payload = us.build_payload(3, snapshot, now=NOW, install_id=AN_ID)
    assert payload["properties"]["sats_funded_total"] == us.MAX_SATS
    assert _validate(payload)["ok"] is True


# --- and refuses everything else -----------------------------------------

def test_a_smuggled_address_field_is_refused():
    """The guarantee the whole two-sided design exists for: even if a future
    client bug put an address in a report, it would not be stored."""
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["properties"]["wallet_address"] = "bc1qsmuggled"
    result = _validate(payload)
    assert result["ok"] is False
    assert result["error"] == "unknown_field"
    assert result["field"] == "properties.wallet_address"


def test_a_missing_field_is_refused():
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    del payload["properties"]["network"]
    result = _validate(payload)
    assert result["ok"] is False
    assert result["error"] == "missing_field"


def test_claiming_a_higher_level_than_the_fields_support_is_refused():
    """A forged report cannot label itself level 3 to get into the liquidity
    tables without actually carrying those fields."""
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["level"] = 3
    assert _validate(payload)["ok"] is False


def test_the_sister_softwares_marker_is_refused():
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["client"] = "barebits-payserver"
    result = _validate(payload)
    assert result["ok"] is False
    assert result["field"] == "client"


@pytest.mark.parametrize("install_id", [
    "not-a-uuid", "", us.INSTALL_ID_PLACEHOLDER,
    "0E2A1C7E-5B4D-4F1A-9C3E-2B6D8A4F0E11",
    "'; DROP TABLE reports; --",
])
def test_a_malformed_install_id_is_refused(install_id):
    """Including the client's own preview placeholder: a preview must never be
    storable as a report."""
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["install_id"] = install_id
    result = _validate(payload)
    assert result["ok"] is False
    assert result["field"] == "install_id"


@pytest.mark.parametrize("field,value", [
    ("network", "mainnet; DROP TABLE reports"),
    ("os_family", "<script>alert(1)</script>"),
    ("gui_kind", "qt\nX-Injected: yes"),
    ("plugin_version", "0.6.0; rm -rf /"),
    ("python_version", "../../etc/passwd"),
])
def test_an_injection_attempt_in_a_string_field_is_refused(field, value):
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["properties"][field] = value
    result = _validate(payload)
    assert result["ok"] is False
    assert result["field"] == f"properties.{field}"


@pytest.mark.parametrize("field,value", [
    ("max_channels", -1),
    ("max_channels", "2"),
    ("max_channels", True),
    ("sats_funded_total", 10 ** 30),
    ("max_swap_fee_pct", 101),
    ("max_swap_fee_pct", -0.5),
    ("max_swap_fee_pct", "0.9"),
    ("automation_enabled", 1),
    ("automation_enabled", "true"),
    ("channels_open_now", None),
])
def test_a_wrong_type_or_out_of_range_value_is_refused(field, value):
    """A wrong type is an error, not something to coerce: coercion on the
    server is how a contract quietly stops being one."""
    payload = us.build_payload(3, _snapshot(), now=NOW, install_id=AN_ID)
    payload["properties"][field] = value
    result = _validate(payload)
    assert result["ok"] is False, f"{field}={value!r} was accepted"


def test_an_extra_envelope_key_is_refused():
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["tracking_pixel"] = "https://example.com/x.gif"
    result = _validate(payload)
    assert result["ok"] is False
    assert result["error"] == "unknown_field"


def test_a_nested_object_in_properties_is_refused():
    """Nothing in the contract is nested, so a nested value is either a bug or
    an attempt to hide something inside a legal-looking field."""
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["properties"]["network"] = {"secret": "bc1qsmuggled"}
    assert _validate(payload)["ok"] is False


@pytest.mark.parametrize("bad", [[], "a string", 42, None])
def test_a_report_that_is_not_an_object_is_refused(bad):
    result = _validate(bad)
    assert result["ok"] is False


def test_a_future_schema_version_is_refused():
    """Better to refuse than to store a report whose meaning we do not know."""
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["schema_version"] = 2
    result = _validate(payload)
    assert result["ok"] is False
    assert result["error"] == "unsupported_schema_version"


@pytest.mark.parametrize("sent_at", [
    "2025-13-45T99:99:99Z", "not a date", "", "2025-11-11 00:00:00",
    "2025-11-11T00:00:00+01:00",
])
def test_a_malformed_timestamp_is_refused(sent_at):
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    payload["sent_at"] = sent_at
    result = _validate(payload)
    assert result["ok"] is False
    assert result["field"] == "sent_at"


def test_the_clients_own_timestamp_format_is_accepted():
    """The other half of the above: the format the client actually emits must
    pass the strict regex."""
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    assert payload["sent_at"] == "2025-11-11T00:00:00Z"
    assert _validate(payload)["ok"] is True


# --- the IP-to-country importer ------------------------------------------
#
# These are the only tests here that need a database, so they run against a
# COPY of the endpoint in a tmp dir. Running them in place would create
# `liquidity-reports.sqlite` inside the repo -- which is also why .gitignore
# covers it, but not creating it at all is better.


@pytest.fixture
def endpoint_copy(tmp_path):
    """A throwaway copy of the endpoint, with its own database."""
    for name in ("common.php", "report.php", "summary.php", "import-geo.php"):
        shutil.copy2(os.path.join(ENDPOINT_DIR, name), tmp_path / name)
    return tmp_path


def _php_in(workdir, snippet: str) -> str:
    code = f'require {json.dumps(str(workdir / "common.php"))};\n' + textwrap.dedent(snippet)
    result = subprocess.run([PHP, "-r", code], capture_output=True, text=True,
                            timeout=120, cwd=str(workdir))
    assert result.returncode == 0, (
        f"php exited {result.returncode}\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}")
    return result.stdout


def _import_geo(workdir, csv_text: str, name: str = "geo.csv"):
    (workdir / name).write_text(csv_text)
    return subprocess.run([PHP, "import-geo.php", name], capture_output=True,
                          text=True, timeout=300, cwd=str(workdir))


def _a_real_sized_csv() -> str:
    """Enough IPv4 ranges to clear the importer's sanity floor, plus the few
    rows the assertions actually care about."""
    rows = [f"{10 + i // 256}.{i % 256}.0.0,{10 + i // 256}.{i % 256}.255.255,GB"
            for i in range(1200)]
    rows += [
        "198.51.100.0,198.51.100.255,DE",
        "203.0.113.0,203.0.113.255,ZZ",        # DB-IP's "unknown"
        "2001:db8::,2001:db8::ffff,FR",
    ]
    return "\n".join(rows) + "\n"


def test_the_geo_importer_loads_v4_and_v6_ranges(endpoint_copy):
    done = _import_geo(endpoint_copy, _a_real_sized_csv())
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1201 IPv4 ranges, 1 IPv6 ranges" in done.stdout, done.stdout
    # The CC-BY licence the data ships under requires the attribution.
    assert "DB-IP" in done.stdout


@pytest.mark.parametrize("ip,expected", [
    ("198.51.100.42", "'DE'"),
    ("10.0.0.1", "'GB'"),
    ("2001:db8::1", "'FR'"),          # IPv6, compared as a blob
    ("203.0.113.5", "NULL"),          # was ZZ: unknown stays unknown
    ("8.8.8.8", "NULL"),              # outside every loaded range
    ("2001:dead::1", "NULL"),
    ("not-an-ip", "NULL"),            # must not throw
])
def test_the_country_lookup_resolves_an_address(endpoint_copy, ip, expected):
    _import_geo(endpoint_copy, _a_real_sized_csv())
    out = _php_in(endpoint_copy,
                  f'$pdo = analytics_db(); '
                  f'var_export(analytics_country($pdo, {json.dumps(ip)}));')
    assert out.strip() == expected


def test_an_unknown_country_row_is_not_stored_as_a_country(endpoint_copy):
    """DB-IP uses ZZ for "we don't know". Loading it would turn "unknown" into
    a country on the summary page."""
    _import_geo(endpoint_copy, _a_real_sized_csv())
    out = _php_in(endpoint_copy,
                  '$pdo = analytics_db(); '
                  'echo (int)$pdo->query("SELECT COUNT(*) FROM geo_v4 '
                  'WHERE cc = \'ZZ\'")->fetchColumn();')
    assert out.strip() == "0"


def test_a_truncated_download_does_not_destroy_a_good_table(endpoint_copy):
    """The failure mode this guard exists for: a half-downloaded or wrong file
    replacing a working table with a useless one."""
    _import_geo(endpoint_copy, _a_real_sized_csv())
    refused = _import_geo(endpoint_copy, "1.2.3.0,1.2.3.255,NL\n", name="tiny.csv")

    assert refused.returncode != 0
    assert "refusing to replace the table" in refused.stdout

    state = _php_in(endpoint_copy, '''
        $pdo = analytics_db();
        echo $pdo->query("SELECT COUNT(*) FROM geo_v4")->fetchColumn(), "|";
        var_export(analytics_country($pdo, "198.51.100.42"));
        echo "|", $pdo->query("SELECT COUNT(*) FROM sqlite_master
                               WHERE name LIKE 'geo_%_new'")->fetchColumn();
    ''')
    rows, lookup, staging = state.strip().split("|")
    assert rows == "1201", "the good table was replaced"
    assert lookup == "'DE'", "the lookup stopped working"
    # The staging tables are created before the transaction opens, so the
    # rollback alone does not remove them -- and the rollback itself only runs
    # because it is NOT guarded by PDO::inTransaction(), which does not see a
    # transaction opened with exec('BEGIN').
    assert staging == "0", "a refused import left orphan tables behind"


def test_a_reimport_replaces_rather_than_appends(endpoint_copy):
    _import_geo(endpoint_copy, _a_real_sized_csv())
    _import_geo(endpoint_copy, _a_real_sized_csv(), name="again.csv")
    out = _php_in(endpoint_copy,
                  '$pdo = analytics_db(); '
                  'echo $pdo->query("SELECT COUNT(*) FROM geo_v4")->fetchColumn();')
    assert out.strip() == "1201", "a second import doubled the table"


def test_the_cdn_header_wins_over_the_table(endpoint_copy):
    """A CDN that already resolved the country is both cheaper and more
    accurate than our monthly snapshot, so it is checked first."""
    _import_geo(endpoint_copy, _a_real_sized_csv())
    # ANALYTICS_COUNTRY_HEADER is a constant, so point it at a header and set
    # that header, all before common.php is loaded.
    code = (
        'define("ANALYTICS_COUNTRY_HEADER", "HTTP_CF_IPCOUNTRY");'
        '$_SERVER["HTTP_CF_IPCOUNTRY"] = "JP";'
        f'require {json.dumps(str(endpoint_copy / "common.php"))};'
        '$pdo = analytics_db();'
        'var_export(analytics_country($pdo, "198.51.100.42"));'
    )
    result = subprocess.run([PHP, "-r", code], capture_output=True, text=True,
                            timeout=60, cwd=str(endpoint_copy))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "'JP'", result.stdout


def test_a_report_is_stored_with_the_resolved_country(endpoint_copy):
    """End of the chain: the country reaches the reports table, and the IP does
    not."""
    _import_geo(endpoint_copy, _a_real_sized_csv())
    payload = us.build_payload(1, _snapshot(), now=NOW, install_id=AN_ID)
    code = (
        f'require {json.dumps(str(endpoint_copy / "common.php"))};'
        '$pdo = analytics_db();'
        '$cc = analytics_country($pdo, "198.51.100.42");'
        '$pdo->prepare("INSERT INTO reports (install_id, first_seen_at,'
        ' received_at, schema_version, level, country, plugin_version,'
        ' electrum_version, network, gui_kind, os_family, payload)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")'
        '->execute(["' + AN_ID + '", 1, 1, 1, 1, $cc, "0.6.0", "4.7.2",'
        ' "mainnet", "qt", "Linux", ' + json.dumps(json.dumps(payload)) + ']);'
        'echo $pdo->query("SELECT country FROM reports")->fetchColumn();'
        'echo "|", (int)$pdo->query("SELECT COUNT(*) FROM reports'
        ' WHERE payload LIKE \'%198.51.100%\'")->fetchColumn();'
    )
    result = subprocess.run([PHP, "-r", code], capture_output=True, text=True,
                            timeout=60, cwd=str(endpoint_copy))
    assert result.returncode == 0, result.stderr
    country, ip_in_payload = result.stdout.strip().split("|")
    assert country == "DE"
    assert ip_in_payload == "0", "the caller's IP reached the stored report"


def test_a_missing_geo_table_is_not_an_error(endpoint_copy):
    """A deployment that never runs the importer records no country, rather
    than failing to accept reports."""
    out = _php_in(endpoint_copy, '''
        $pdo = analytics_db();
        $pdo->exec("DROP TABLE IF EXISTS geo_v4");
        $pdo->exec("DROP TABLE IF EXISTS geo_v6");
        var_export(analytics_country($pdo, "198.51.100.42"));
    ''')
    assert out.strip() == "NULL"


def test_a_real_wire_body_fits_the_receivers_size_cap():
    """The client caps its read at 16 KB and so does the receiver; a level-3
    report has to be comfortably inside both."""
    cap = _php_json('echo json_encode(ANALYTICS_MAX_BODY_BYTES);')
    body = us.encode_payload(
        us.build_payload(3, _snapshot(), now=NOW, install_id=AN_ID))
    assert len(body) < cap / 2, f"a level-3 report is {len(body)} of {cap} bytes"
