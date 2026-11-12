# Anonymous usage statistics -- the opt-in report a wallet may send us so we
# know how many wallets run this plugin, which settings people actually choose,
# and how much liquidity the plugin has provisioned for them.
#
# This module is deliberately PURE, exactly like `liquidity_manager.py`: it
# imports nothing from Electrum and does no I/O (not even reading the clock --
# every function that needs "now" is handed it). It turns a snapshot of
# already-coarsened wallet state (gathered by the glue in `__init__.py`) into
# the exact JSON object that goes on the wire. Everything with a side effect --
# reading settings, counting channels, making the HTTP request, persisting the
# cadence stamps -- lives in the glue.
#
# That split is what makes the central promise testable: the consent screen's
# preview and the real report are produced by THE SAME function
# (`build_payload`), so "what we show you is what we send" is a structural
# property of the code rather than a claim in a dialog. `preview_json` is a
# thin pretty-printer over `build_payload`; there is no second code path that
# could drift from it.
#
# The privacy contract, in one place:
#
#   * NEVER sent, at any level: wallet addresses, transaction ids, channel ids,
#     channel-peer node ids, swap-provider npubs, the sink Lightning address,
#     the dev-fee address, wallet names, file paths, balances, or the user's IP
#     beyond what making an HTTPS request inherently discloses. Identifiers are
#     reduced to COUNTS and BOOLEANS before they reach a snapshot field --
#     `sink_configured: true`, never the address; `preferred_partners_count: 2`,
#     never the node ids.
#   * The install id is a random UUIDv4 minted the moment the user opts in. It
#     is derived from nothing -- not the wallet name, not a key, not the seed --
#     so it cannot be reversed into an identity, and a wallet that never opts in
#     never has one.
#   * `ALLOWED_PROPERTIES` below is the whole contract. `build_payload`
#     intersects its output with that allowlist as a belt-and-braces final step,
#     so a future edit that adds a field to a builder without adding it to the
#     allowlist silently sends nothing rather than silently sending it. The
#     receiver (`scripts/analytics-endpoint/report.php`) enforces its own copy of
#     the same list and rejects anything else, so a client bug cannot widen what
#     is collected even if it gets past this module.
#
# Levels are CUMULATIVE -- each includes everything below it:
#
#   0  Off        -- nothing is sent, ever. The shipped default.
#   1  Count me   -- that this wallet exists, and what it is running.
#   2  Settings   -- plus which settings the user chose (numbers and booleans).
#   3  Liquidity  -- plus lifetime provisioned totals and the current channel
#                    count. Lifetime sums and counts only; never a balance.
#
# Wire format (schema_version 1): one flat JSON object per report, shaped so a
# generic event store could ingest it as a single event with snake_case
# properties:
#
#   { "schema_version": 1, "event": "usage_report",
#     "client": "electrum-inbound-liquidity",
#     "install_id": "<uuid v4>", "sent_at": "<ISO-8601 UTC>", "level": 1|2|3,
#     "properties": { ... } }
from __future__ import annotations

import datetime
import json
import re
import uuid
from dataclasses import dataclass, fields
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

# --- wire constants --------------------------------------------------------

SCHEMA_VERSION: int = 1
EVENT: str = "usage_report"

# Identifies the software sending the report. Deliberately distinct from the
# sister software's marker (`barebits-payserver`): the two report to different
# endpoints and must stay separable even if a report is ever misrouted, and the
# receiver rejects a report whose marker is not this one.
CLIENT_MARKER: str = "electrum-inbound-liquidity"

LEVEL_OFF: int = 0
LEVEL_COUNT: int = 1
LEVEL_SETTINGS: int = 2
LEVEL_LIQUIDITY: int = 3

# The reporting levels, ascending. LEVEL_OFF is not in here: it is the absence
# of a report, not a kind of report.
LEVELS: Tuple[int, ...] = (LEVEL_COUNT, LEVEL_SETTINGS, LEVEL_LIQUIDITY)

# One report on opting in, then weekly; a failed attempt is retried in an hour
# rather than a week, so a wallet that was offline at its slot is not silenced
# for seven days.
SEND_INTERVAL_SEC: float = 7 * 86_400.0
RETRY_INTERVAL_SEC: float = 3_600.0

# Shown in a preview before an install id exists (one is minted at opt-in, so
# the consent screen has none yet to show).
INSTALL_ID_PLACEHOLDER: str = "(random id, generated if you opt in)"

# Defensive bounds. Values reaching here come from a wallet file a user may have
# hand-edited, so every number is coerced into range rather than trusted: a
# garbage setting must not be able to break report building, and must not travel.
MAX_COUNT: int = 1_000_000_000
MAX_SATS: int = 2_100_000_000_000_000        # 21M BTC in sats

# Fixed enums for the environment strings. A closed set rather than the raw
# value: raw version/platform strings are both fingerprint-y and useless for
# counting, and an unrecognised one must land somewhere harmless.
OS_FAMILIES: Tuple[str, ...] = ("Linux", "Windows", "Darwin", "BSD", "other")
GUI_KINDS: Tuple[str, ...] = ("qt", "cmdline", "other")
# "regtest" is in the list because the rig's own e2e test legitimately sends one
# (see the ALLOW_REGTEST override in the glue); a shipped build suppresses
# reporting on regtest entirely, so it should never appear in real data.
NETWORKS: Tuple[str, ...] = (
    "mainnet", "testnet", "testnet4", "signet", "regtest", "other")

# A version string we are willing to put on the wire: digits, dots, and a short
# alphanumeric pre-release tail. Anything else becomes "unknown" -- a version is
# read from a manifest/module attribute, and neither is guaranteed well-formed.
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:-[0-9A-Za-z.]{1,20})?$")

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class UsageReportRejected(Exception):
    """The receiver did not accept the report. Carries a short, already-safe
    message for the status line; never the raw response body."""


# --- the snapshot ----------------------------------------------------------
#
# Everything a report can contain, gathered by the glue and already reduced to
# primitives. Identifiers are absent BY CONSTRUCTION: there is no field here
# that can hold an address, a node id, an npub or a balance, so a future glue
# change cannot accidentally route one into a payload -- it would have nowhere
# to put it. That is the point of making this an explicit frozen dataclass
# rather than passing a dict around.


@dataclass(frozen=True)
class UsageSnapshot:
    # --- level 1: what this is ---------------------------------------------
    plugin_version: str = "unknown"
    electrum_version: str = "unknown"
    python_version: str = "unknown"
    os_family: str = "other"
    gui_kind: str = "other"
    network: str = "mainnet"

    # --- level 2: which settings were chosen -------------------------------
    automation_enabled: bool = False
    manual_run_only: bool = False
    liquidity_goal_sat: int = 0
    max_channels: int = 0
    max_channel_size_sat: int = 0
    min_onchain_to_open_sat: int = 0
    onchain_reserve_sat: int = 0
    max_swap_fee_pct: float = 0.0
    swap_trigger_pct: float = 0.0
    swap_trigger_sat: int = 0
    min_swap_sat: int = 0
    min_outbound_sat: int = 0
    max_opens_per_day: int = 0
    max_closes_per_day: int = 0
    dev_fee_pct: float = 0.0
    # Booleans standing in for configured identifiers -- never the value itself.
    sink_configured: bool = False
    channel_peer_configured: bool = False
    defer_sink_until_goal: bool = False
    disable_submarine_swaps: bool = False
    manage_plugin_opened_only: bool = False
    one_channel_per_peer: bool = False
    partners_strict: bool = False
    reliability_enabled: bool = False
    peer_reliability_enabled: bool = False
    auto_remediate_stuck_open: bool = False
    offline_autoclose_enabled: bool = False
    update_check_enabled: bool = False
    show_goal_in_piechart: bool = False
    diag_log_enabled: bool = False
    # Sizes of the partner/provider preference lists -- counts, never members.
    preferred_partners_count: int = 0
    banned_partners_count: int = 0
    preferred_npubs_count: int = 0
    banned_npubs_count: int = 0

    # --- level 3: how much liquidity was provisioned -----------------------
    # Monotonic lifetime sums, plus two live counts. No sats-denominated
    # "right now" figure appears here: a lifetime total says what the plugin
    # did, whereas current capacity would say what the wallet holds, and the
    # consent screen promises we do not report the latter.
    channels_opened_total: int = 0
    channels_closed_total: int = 0
    force_closes_total: int = 0
    sats_funded_total: int = 0
    swaps_completed_total: int = 0
    sats_swapped_total: int = 0
    sink_payments_total: int = 0
    sats_sunk_total: int = 0
    dev_fee_paid_total_sat: int = 0
    channels_open_now: int = 0
    channels_managed_now: int = 0


# --- the allowlist ---------------------------------------------------------
#
# The complete property allowlist per level: the contract between this module,
# the consent preview, and the receiver. Keep all three in lockstep --
# scripts/analytics-endpoint/common.php holds the server's copy and rejects a
# report carrying a key that is not in it.

ALLOWED_PROPERTIES: Mapping[int, Tuple[str, ...]] = {
    LEVEL_COUNT: (
        "plugin_version", "electrum_version", "python_version",
        "os_family", "gui_kind", "network",
    ),
    LEVEL_SETTINGS: (
        "automation_enabled", "manual_run_only",
        "liquidity_goal_sat", "max_channels", "max_channel_size_sat",
        "min_onchain_to_open_sat", "onchain_reserve_sat",
        "max_swap_fee_pct", "swap_trigger_pct", "swap_trigger_sat",
        "min_swap_sat", "min_outbound_sat",
        "max_opens_per_day", "max_closes_per_day", "dev_fee_pct",
        "sink_configured", "channel_peer_configured",
        "defer_sink_until_goal", "disable_submarine_swaps",
        "manage_plugin_opened_only", "one_channel_per_peer", "partners_strict",
        "reliability_enabled", "peer_reliability_enabled",
        "auto_remediate_stuck_open", "offline_autoclose_enabled",
        "update_check_enabled", "show_goal_in_piechart", "diag_log_enabled",
        "preferred_partners_count", "banned_partners_count",
        "preferred_npubs_count", "banned_npubs_count",
    ),
    LEVEL_LIQUIDITY: (
        "channels_opened_total", "channels_closed_total", "force_closes_total",
        "sats_funded_total", "swaps_completed_total", "sats_swapped_total",
        "sink_payments_total", "sats_sunk_total", "dev_fee_paid_total_sat",
        "channels_open_now", "channels_managed_now",
    ),
}

# How each allowed property is coerced before it travels: the shape the receiver
# also enforces. ("int", lo, hi) / ("float", lo, hi, ndigits) / ("bool",) /
# ("enum", allowed...). Built once and asserted complete at import, so a new
# allowlist entry without a rule is an immediate, loud failure rather than a
# field that silently stops being sent.
_SATS_FIELDS = frozenset({
    "liquidity_goal_sat", "max_channel_size_sat", "min_onchain_to_open_sat",
    "onchain_reserve_sat", "swap_trigger_sat", "min_swap_sat",
    "min_outbound_sat", "sats_funded_total", "sats_swapped_total",
    "sats_sunk_total", "dev_fee_paid_total_sat",
})
_PCT_FIELDS = frozenset({"max_swap_fee_pct", "swap_trigger_pct", "dev_fee_pct"})
_ENUM_FIELDS: Mapping[str, Tuple[str, ...]] = {
    "os_family": OS_FAMILIES,
    "gui_kind": GUI_KINDS,
    "network": NETWORKS,
}
# An unrecognised value falls back to "other" in every case -- never to a real
# member. Defaulting an unknown network to "mainnet" (the obvious shortcut)
# would file junk under the one value we most need to be able to trust.
_ENUM_DEFAULTS: Mapping[str, str] = {name: "other" for name in _ENUM_FIELDS}
_VERSION_FIELDS = frozenset({"plugin_version", "electrum_version", "python_version"})


def all_allowed_properties() -> Tuple[str, ...]:
    """Every property name any level may carry, in level then declaration
    order."""
    out: list = []
    for level in LEVELS:
        out.extend(ALLOWED_PROPERTIES[level])
    return tuple(out)


def allowed_properties_for_level(level: int) -> Tuple[str, ...]:
    """Every property a report at ``level`` may carry -- that level's own keys
    and every lower level's, because the levels are cumulative. Empty for
    LEVEL_OFF and for anything out of range."""
    out: list = []
    for candidate in LEVELS:
        if candidate > level:
            break
        out.extend(ALLOWED_PROPERTIES[candidate])
    return tuple(out)


def is_valid_level(level: Any) -> bool:
    """Whether ``level`` is one of the three reporting levels. LEVEL_OFF is
    deliberately NOT valid here: it is a valid *choice* but not a level a report
    can be built at."""
    return isinstance(level, int) and not isinstance(level, bool) and level in LEVELS


def is_valid_choice(level: Any) -> bool:
    """Whether ``level`` is a storable user choice: off, or a reporting level."""
    return (isinstance(level, int) and not isinstance(level, bool)
            and (level == LEVEL_OFF or level in LEVELS))


# --- defensive coercion ----------------------------------------------------


def _as_bool(value: Any) -> bool:
    return bool(value)


def _as_int(value: Any, *, lo: int = 0, hi: int = MAX_COUNT) -> int:
    """``value`` as an int clamped to [lo, hi]; ``lo`` for anything unusable.

    Floats are truncated rather than rejected (a setting may legitimately hold
    60000.0), but bools are not ints here -- a bool reaching an int field means
    the glue mapped a field wrongly, and 0/1 would hide that.
    """
    if isinstance(value, bool):
        return lo
    try:
        number = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, number))


def _as_float(value: Any, *, lo: float = 0.0, hi: float = 100.0,
              ndigits: int = 4) -> float:
    """``value`` as a rounded float clamped to [lo, hi]; ``lo`` if unusable.

    Rounded because these are percentages read from a config: an unrounded
    0.6000000000000001 is noise on the wire and makes two identical
    configurations look different in the data.
    """
    if isinstance(value, bool):
        return lo
    try:
        number = float(value)
    except (TypeError, ValueError):
        return lo
    if number != number or number in (float("inf"), float("-inf")):
        return lo                      # NaN / inf are not JSON values
    return round(max(lo, min(hi, number)), ndigits)


def _as_enum(value: Any, allowed: Sequence[str], *, default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _as_version(value: Any) -> str:
    """A version string, or ``"unknown"``. Validated against a strict shape
    because this is one of the few free-text values on the wire, and an
    unvalidated one is a place for junk (or an injected newline) to ride
    along."""
    if isinstance(value, str) and _VERSION_RE.match(value):
        return value
    return "unknown"


# ``f.type`` is a STRING under `from __future__ import annotations`, so both
# spellings are accepted rather than assuming either.
_SNAPSHOT_TYPES: Mapping[str, Any] = {
    f.name: (bool if f.type in ("bool", bool)
             else float if f.type in ("float", float)
             else int if f.type in ("int", int) else str)
    for f in fields(UsageSnapshot)
}


def _coerce(name: str, value: Any) -> Any:
    """Coerce one snapshot field to the shape the wire (and the receiver)
    expect."""
    if name in _ENUM_FIELDS:
        return _as_enum(value, _ENUM_FIELDS[name], default=_ENUM_DEFAULTS[name])
    if name in _VERSION_FIELDS:
        return _as_version(value)
    if name in _PCT_FIELDS:
        return _as_float(value, lo=0.0, hi=100.0, ndigits=4)
    if name in _SATS_FIELDS:
        return _as_int(value, lo=0, hi=MAX_SATS)
    # Everything else is a count or a flag; the dataclass annotation decides.
    if _SNAPSHOT_TYPES.get(name) is bool:
        return _as_bool(value)
    return _as_int(value, lo=0, hi=MAX_COUNT)


# Every allowed property must exist on the snapshot, and every snapshot field
# must be allowed at some level. Checked at import because the two drifting
# apart is the exact failure this design exists to prevent: a snapshot field
# nobody sends is dead weight, and an allowlist entry with no field behind it
# would send `null` forever.
def _check_contract() -> None:
    from collections import Counter

    snapshot_names = {f.name for f in fields(UsageSnapshot)}
    ordered = all_allowed_properties()
    allowed = set(ordered)
    missing = allowed - snapshot_names
    if missing:
        raise AssertionError(
            f"allowlisted properties with no UsageSnapshot field: {sorted(missing)}")
    unsent = snapshot_names - allowed
    if unsent:
        raise AssertionError(
            f"UsageSnapshot fields in no level's allowlist: {sorted(unsent)}")
    duplicated = sorted(n for n, count in Counter(ordered).items() if count > 1)
    if duplicated:
        raise AssertionError(
            f"properties allowlisted at more than one level: {duplicated}")


_check_contract()


# --- building the report ---------------------------------------------------


def format_sent_at(now: float) -> str:
    """``now`` (epoch seconds) as ISO-8601 UTC to whole seconds."""
    moment = datetime.datetime.fromtimestamp(float(now), tz=datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(level: int, snapshot: UsageSnapshot, *, now: float,
                  install_id: Optional[str] = None) -> Dict[str, Any]:
    """The exact report for ``level`` -- the object that goes on the wire, and
    the object the consent screen previews.

    Deterministic for a given (level, snapshot, now, install_id), which is what
    lets a test pin the preview against the report byte for byte.

    ``install_id`` None means "no id yet": the preview placeholder is used, so
    the consent screen can show a complete report before any id exists. An id
    that is not a well-formed UUIDv4 is replaced by the placeholder rather than
    travelling -- a malformed id in a wallet file must not become a free-text
    field on the wire.
    """
    if not is_valid_level(level):
        raise ValueError(f"not a reporting level: {level!r}")

    properties: Dict[str, Any] = {}
    for name in allowed_properties_for_level(level):
        properties[name] = _coerce(name, getattr(snapshot, name, None))

    # Belt and braces. `properties` is already built strictly FROM the
    # allowlist, so this cannot currently drop anything -- it is here so that
    # if someone later builds properties some other way, the allowlist still
    # has the last word.
    allowed = set(allowed_properties_for_level(level))
    properties = {k: v for k, v in properties.items() if k in allowed}

    return {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "client": CLIENT_MARKER,
        "install_id": install_id if is_install_id(install_id) else INSTALL_ID_PLACEHOLDER,
        "sent_at": format_sent_at(now),
        "level": int(level),
        "properties": properties,
    }


def preview_json(level: int, snapshot: UsageSnapshot, *, now: float,
                 install_id: Optional[str] = None) -> str:
    """Pretty JSON of :func:`build_payload`, for the consent screen and the
    settings tab. A thin formatter over the real builder on purpose: there is no
    separate "preview" code path that could drift from what is sent."""
    return json.dumps(build_payload(level, snapshot, now=now, install_id=install_id),
                      indent=2, sort_keys=False)


def encode_payload(payload: Mapping[str, Any]) -> bytes:
    """The compact UTF-8 body actually POSTed."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=False).encode("utf-8")


# --- install id ------------------------------------------------------------


def new_install_id() -> str:
    """A fresh random UUIDv4.

    Random, not derived: nothing about the wallet (its name, its keys, its
    xpub, its file path) feeds this, so it carries no information about the user
    and cannot be recomputed by anyone -- including us -- from anything they
    hold. This is the only identifier a report contains.
    """
    return str(uuid.uuid4())


def is_install_id(value: Any) -> bool:
    """Whether ``value`` is a well-formed UUIDv4 we would put on the wire."""
    return isinstance(value, str) and _UUID4_RE.match(value) is not None


# --- cadence ---------------------------------------------------------------


def is_due(*, now: float, last_sent_ts: float, last_attempt_ts: float) -> bool:
    """Whether a report is due: nothing sent within the interval, and no attempt
    within the retry back-off.

    Both stamps are guarded against being in the FUTURE, which a clock change
    (or a hand-edited wallet file) can easily produce. A naive
    ``now - last > interval`` test reads a future stamp as "not due" and
    silences reporting until the clock catches up -- so a stamp ahead of now is
    treated as "no idea when, send now" instead.
    """
    try:
        now_f = float(now)
        sent = float(last_sent_ts or 0.0)
        attempt = float(last_attempt_ts or 0.0)
    except (TypeError, ValueError):
        return True
    if 0.0 < sent <= now_f and (now_f - sent) < SEND_INTERVAL_SEC:
        return False
    if 0.0 < attempt <= now_f and (now_f - attempt) < RETRY_INTERVAL_SEC:
        return False
    return True


# --- the receiver's reply --------------------------------------------------


def parse_response(status: int, body: bytes) -> None:
    """Accept only an explicit ``{"ok": true}``; raise
    :class:`UsageReportRejected` otherwise.

    Strict on purpose: anything else -- a captive portal's login page, a proxy
    error, an HTML 200 from a misconfigured host -- is a report that did not
    land, and treating it as success would stamp the weekly cadence and lose the
    report for a week.

    Never puts the response body in the exception. The body is attacker-
    controlled (we may be talking to whatever intercepted the connection) and
    this message reaches a log line and a GUI label.
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        raise UsageReportRejected("no response from the endpoint")
    if code == 0:
        # Not reachable from the plugin's own sender (aiohttp raises rather
        # than handing back a status of 0), but this function is the one place
        # that decides what "accepted" means, so it answers for that case too.
        raise UsageReportRejected("no response from the endpoint")
    if code == 429:
        raise UsageReportRejected("the endpoint is rate-limiting this address")
    if code != 200:
        raise UsageReportRejected(f"the endpoint returned HTTP {code}")
    try:
        decoded = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UsageReportRejected("the endpoint returned an unreadable reply")
    if not isinstance(decoded, dict) or decoded.get("ok") is not True:
        raise UsageReportRejected("the endpoint did not accept the report")


# --- operator-facing copy --------------------------------------------------


@dataclass(frozen=True)
class LevelDescription:
    """What one level is called and what it shares, in the user's words."""
    title: str
    summary: str
    items: Tuple[str, ...]


def level_descriptions() -> Mapping[int, LevelDescription]:
    """Copy for the three levels, shared by the consent screen and the settings
    tab so the two can never describe a level differently.

    Plain strings, not translated here: the GUI wraps each through ``_()`` at
    the point of display, which is where this plugin's translations happen.
    """
    return {
        LEVEL_COUNT: LevelDescription(
            title="Count me",
            summary="Just that this wallet runs the plugin, and what it is running.",
            items=(
                "A random identifier, generated only if you opt in. It is not "
                "derived from your wallet, your keys or your seed, and nobody "
                "can work backwards from it.",
                "The plugin, Electrum and Python versions, your operating-system "
                "family, and whether you run the desktop or command-line wallet.",
                "Which Bitcoin network this wallet is on.",
            ),
        ),
        LEVEL_SETTINGS: LevelDescription(
            title="Settings",
            summary="Everything in Count me, plus which settings you chose.",
            items=(
                "Your liquidity goal, channel limits, fee ceiling and swap "
                "triggers, and which features you switched on or off.",
                "Whether a sink address or a preferred channel peer is "
                "configured, and how many partners or providers you listed -- "
                "as yes/no and counts only, never the addresses, node ids or "
                "npubs themselves.",
            ),
        ),
        LEVEL_LIQUIDITY: LevelDescription(
            title="Liquidity",
            summary="Everything in Settings, plus how much liquidity the plugin "
                    "has provisioned for you.",
            items=(
                "Lifetime totals: how many channels the plugin opened and "
                "closed, how many sats it funded into them, how many swaps it "
                "completed and how many sats it moved, and the dev fee paid.",
                "How many channels are open right now, and how many of them the "
                "plugin manages.",
                "Running totals and counts only -- never a balance, never a "
                "transaction, never an address.",
            ),
        ),
    }


def never_sent_items() -> Tuple[str, ...]:
    """The explicit "we never send this" list, for the consent screen.

    Kept next to the allowlist deliberately: these two lists are the promise and
    its enforcement, and a reviewer should see them together.
    """
    return (
        "Your wallet addresses, or any address you have ever used.",
        "Your transactions, payments, invoices or channel ids.",
        "Your on-chain or Lightning balances, or your channel capacities.",
        "Your sink Lightning address, your chosen channel peers' node ids, or "
        "swap providers' npubs.",
        "Your wallet name, your file paths, your seed or any key material.",
    )
