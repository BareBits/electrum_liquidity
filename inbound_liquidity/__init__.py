# Inbound Liquidity Manager — plugin glue.
#
# This module defines the user-configurable settings (as Electrum ConfigVars)
# and the base plugin class that wires Electrum runtime state into the pure
# rules engine in `liquidity_manager.py`, then executes the actions it returns.
#
# GUI-specific subclasses live in `qt.py` (Qt settings dialog + status menu) and
# `cmdline.py` (headless). Electrum instantiates `<gui_name>.Plugin`.
from __future__ import annotations

import asyncio
import functools
import os
import sys
import time
from concurrent import futures
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, Dict, List, Optional, Tuple

from electrum import util
from electrum.i18n import _
from electrum.plugin import BasePlugin
from electrum.simple_config import ConfigVar, SimpleConfig
from electrum.util import log_exceptions, ignore_exceptions
from electrum.logging import get_logger

from .liquidity_manager import (
    Action,
    ChannelSnapshot,
    clean_npub,
    DAILY_WINDOW_SEC,
    DeclineRecord,
    LiquidityConfig,
    LiquiditySnapshot,
    MIN_FUNDING_SAT,
    OpenChannelAction,
    ProviderOffer,
    ProviderReliability,
    ReverseSwapAction,
    classify_peer_observation,
    clamp_dev_fee_pct,
    compute_dev_fee,
    count_within_window,
    daily_cap_reached,
    deadline_reached,
    decayed_penalty_pct,
    decide_dev_fee_payout,
    eligible_providers,
    evaluate,
    is_wallet_ready,
    normalize_node_id,
    order_channel_partners,
    record_uptime_sample,
    reliability_penalty_pct,
    scrub_text,
    should_auto_ban,
    should_commit_offline_close,
    uptime_ratio,
    validate_offer,
)
from .diag_log import DiagLog

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet
    from electrum.submarine_swaps import SwapServerTransport


def _parse_npub_set(raw: Optional[str]) -> frozenset:
    """Parse a comma/whitespace/newline-separated npub list (as stored in the
    preferred/banned ConfigVars) into a set of trimmed, non-empty npubs."""
    if not raw:
        return frozenset()
    parts = raw.replace("\n", ",").replace(" ", ",").split(",")
    return frozenset(p.strip() for p in parts if p.strip())


def _parse_partner_list(raw: Optional[str]) -> List[str]:
    """Parse a comma/newline-separated channel-partner list (``pubkey@host:port``
    connect strings, or bare pubkeys) into an *ordered*, de-duplicated list.

    Order is preserved because preferred partners are tried first-to-last;
    duplicates (by pubkey, ignoring any host) keep the first occurrence. Unlike
    npubs, connect strings are not split on spaces (an address has none)."""
    if not raw:
        return []
    out: List[str] = []
    seen: set = set()
    for part in raw.replace("\n", ",").split(","):
        entry = part.strip()
        if not entry:
            continue
        nid = normalize_node_id(entry)
        if nid in seen:
            continue
        seen.add(nid)
        out.append(entry)
    return out


def _parse_banned_partners(raw: Optional[str]) -> frozenset:
    """Parse a banned channel-partner list into a set of normalized (lowercased)
    pubkeys, so a partner is banned by identity regardless of any attached host."""
    return frozenset(
        nid for nid in (normalize_node_id(e) for e in _parse_partner_list(raw)) if nid)


_logger = get_logger(__name__)

# Events that mean "wallet funds may have changed" -- an inbound payment
# (on-chain or Lightning) arrived, a channel balance shifted, or the swap
# provider became known. These carry different argument shapes (some pass the
# wallet, some the AddressSynchronizer), so the handler ignores the args and
# re-evaluates every managed wallet; the per-wallet lock + idempotent rules
# engine make redundant triggers harmless.
_TRIGGER_EVENTS = [
    "wallet_updated",          # balance / state changes
    "channels_updated",        # channel open/close/balance
    "request_status",          # a payment request (invoice) was paid
    "adb_added_verified_tx",   # a new on-chain tx affecting our addresses confirmed
    "adb_set_up_to_date",      # address sync settled (catches on-chain receives)
    "payment_succeeded",       # a Lightning payment settled
    "swap_offers_changed",     # the swap provider / its fees became known
]

# After we initiate (or attempt) a reverse swap on a channel, ignore that
# channel for this long. A swap settling generates its own channel-update
# events; without a cooldown those would re-trigger evaluation and stack
# redundant swaps on the same channel before the first one is reflected.
SWAP_COOLDOWN_SEC = 180.0

# Coarse backstop (seconds) on a whole reverse-swap attempt. The precise hang --
# a provider that never replies to the createswap RPC -- is already bounded in
# TargetedNostrTransport (RPC_REPLY_TIMEOUT_SEC); this is a belt-and-suspenders
# guard around the entire ``sm.reverse_swap`` call so that NO reverse swap can
# hold the per-wallet evaluation lock indefinitely, whatever hangs inside it.
# Sized well above a healthy attempt -- the bounded createswap RPC (~60s) plus
# Electrum's own Lightning payment ceiling (PAYMENT_TIMEOUT = 120s, which
# reverse_swap races against funding detection) -- so it fires only on a genuine
# wedge, never on a slow-but-succeeding payment. Referenced via
# ``self._reverse_swap_timeout_sec`` so tests can shrink it.
REVERSE_SWAP_TIMEOUT_SEC = 300.0

# Substrings that identify an UNAMBIGUOUS provider cheat among the bare
# ``Exception``s Electrum's ``reverse_swap`` raises from its pre-payment sanity
# checks (submarine_swaps.py): a short-changed on-chain amount, an invoice whose
# payment hash does not match the RHASH we sent, or an invoice for the wrong
# amount. These fire BEFORE any Lightning payment, so no funds are ever at risk,
# but they are deterministic provider misbehaviour -- so we charge an escalating
# fault (repeat offenders sink toward a ban). The remaining bare Exceptions from
# that method are our-side conditions (a stale local tip, a too-close locktime
# relative to our height) or genuine bugs, which must NOT penalise the provider.
# Markers are matched against Electrum's own format strings, not provider-
# supplied text, so a hostile provider cannot forge them (and would only harm
# itself if it could). Revisit if the pinned Electrum version changes these
# messages. See the ``except Exception`` arm of ``_reverse_swap``.
_REVERSE_SWAP_CHEAT_MARKERS = (
    "onchain_amount is less",      # onchain_amount < expected_onchain_amount_sat
    "inconsistent RHASH",          # invoice payment hash != our RHASH
    "invoice_amount",              # invoice amount != what we requested
)

# Startup settle window. For this long after a wallet is loaded, the plugin takes
# NO automated action (open / swap / close) and does not judge any peer offline:
# the Lightning layer connects to its peers asynchronously after load, so a peer
# that is actually fine reads as "not connected" during this window. Acting then
# would fault a healthy peer or poison its uptime metric (see the readiness
# guards in liquidity_manager). Kept a fixed constant (not a ConfigVar) -- it is
# infrastructure timing, not a strategy knob -- but referenced via
# ``self._startup_grace_sec`` so tests can shrink it. A couple of minutes
# comfortably covers peer (re)connection without noticeably delaying automation.
STARTUP_GRACE_SEC = 120.0

# Heartbeat cadence. The plugin is otherwise entirely event-driven (see
# _TRIGGER_EVENTS), but every time-based watchdog -- the stuck channel-open
# timeout, the offline auto-close uptime sampling / force-close deadline, the
# stuck-swap reconciliation and freeze escape, the dev-fee retry backoff -- only
# advances when an evaluation runs. A dead peer on an otherwise-quiet wallet
# produces *fewer* events, so the very condition those watchdogs exist to detect
# is the one under which they would never run. A fixed periodic tick makes them
# fire on a predictable cadence regardless of wallet activity. Each tick just
# calls _evaluate, which is fully guarded (per-wallet lock, automation gate,
# startup-readiness), so a heartbeat tick is safe and idempotent. Referenced via
# ``self._heartbeat_interval_sec`` so tests can shrink it.
HEARTBEAT_INTERVAL_SEC = 600.0

# When opening a nostr swap session, how long to wait for relays to connect, and
# then for the first provider offers to arrive, before proceeding with whatever
# we have. Bounded so an evaluation never hangs on an unreachable network.
OFFER_CONNECT_TIMEOUT_SEC = 10.0
OFFER_DISCOVERY_TIMEOUT_SEC = 12.0

# Wallet-storage key under which the per-wallet decision log is persisted (a
# JSON-plain list of entry dicts; see `_log_entry`).
LOG_DB_KEY = "inbound_liquidity_decision_log"
# Per-wallet provider reliability history: npub -> {consecutive_faults,
# fault_count, success_count, last_fault_ts, last_success_ts, last_reason}.
RELIABILITY_DB_KEY = "inbound_liquidity_provider_reliability"
# Per-wallet channel-peer reliability history: node_id(hex) -> {consecutive_faults,
# fault_count, hard_fault_count, success_count, last_fault_ts, last_success_ts,
# last_reason}. ``hard_fault_count`` (force-close / repeated open failure) is the
# tally the auto-ban threshold reads; soft faults only feed the decaying penalty.
PEER_RELIABILITY_DB_KEY = "inbound_liquidity_peer_reliability"
# Reverse swaps we initiated and are still watching for on-chain funding, so a
# swap that is accepted but never funds can be charged to the right party: the
# *provider* if it accepted but never funded ("stuck"), or the channel *peer* if
# our Lightning payment for the swap failed (the peer couldn't route it). Keyed
# by payment_hash hex -> {npub, started_ts, node_id, channel_id}. Survives
# restarts so a swap stuck across a restart is still attributed.
PENDING_SWAPS_DB_KEY = "inbound_liquidity_pending_swap_providers"
# First time (epoch sec) each still-pending reverse swap was observed freezing
# automation: payment_hash hex -> first_seen_ts. A swap whose funding is broadcast
# but not yet swept freezes the engine (see the in-flight freeze); this lets a
# swap that has been pending far too long stop counting toward that freeze --
# mirroring the stuck channel-open escape -- so one wedged swap can't block all
# automation forever. Pruned to the currently-pending set on every snapshot, and
# persisted so a swap wedged across a restart still ages out. See
# ``INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN`` and ``_count_freezing_swaps``.
SWAP_FIRST_SEEN_DB_KEY = "inbound_liquidity_swap_first_seen"
# Retention bounds for the decision log (days). The default keeps a month;
# the operator can stretch it up to ~3 years from the settings dialog.
DEFAULT_LOG_RETENTION_DAYS = 30
MAX_LOG_RETENTION_DAYS = 999
# How many entries to keep regardless of age, as a hard backstop so a busy
# wallet can't grow the log without bound between prunes.
MAX_LOG_ENTRIES = 2000
# On-disk diagnostic log (opt-in; see INBOUND_LIQUIDITY_DIAG_LOG_ENABLED). Files
# live in this subdirectory of the Electrum data dir, one folder per wallet, one
# JSON-lines file per UTC day, retained for this many days.
DIAG_LOG_DIRNAME = "inbound_liquidity_logs"
DIAG_LOG_RETENTION_DAYS = 30
# Per-wallet store of recent action timestamps, for the rolling-24h daily
# ceilings: kind ("open" / "close") -> list of epoch-second timestamps. Pruned to
# the window (plus a small backstop) on every write, so it can't grow unbounded.
# Kept separate from the decision log so the ceilings are robust to the log's
# retention/size pruning. Survives restarts (the window must outlive one).
ACTION_TIMESTAMPS_DB_KEY = "inbound_liquidity_action_timestamps"
# Hard cap on stored timestamps per kind, regardless of age -- a backstop so a
# runaway can't bloat the store between window-prunes (far above any real cap).
MAX_ACTION_TIMESTAMPS = 1000
# Default daily ceilings (rolling 24h) on the two fund-spending action types, to
# bound a runaway automation loop. 0 = unlimited. Opens are gated in the engine;
# closes (the watchdog's wedged-open force-close) are gated in the glue.
DEFAULT_MAX_OPENS_PER_DAY = 5
DEFAULT_MAX_CLOSES_PER_DAY = 5

# --- channel-funding floor override ---------------------------------------
# Electrum's stock MIN_FUNDING_SAT (lnutil, 200_000) is a hard floor on new-
# channel funding. When the user's `min_onchain_to_open_sat` is set below it, we
# lower the floor to that configured value so the plugin (and manual opens) can
# create smaller channels. The override is re-asserted at startup and on every
# evaluation tick, so the user's value always wins even if some code path resets
# the constant.
#
# MIN_FUNDING_SAT is bound as an independent module-level name wherever it is
# `from ...lnutil import`ed, so every module that gates an open must be patched.
# We touch only modules already imported (never force-importing the GUI ones in
# headless mode), and capture the stock value once, before we ever lower it.
_MIN_FUNDING_CORE_MODULES = (
    'electrum.lnutil',
    'electrum.lnworker',
    'electrum.wallet',
    'electrum.gui.qt.main_window',
    'electrum.gui.qt.new_channel_dialog',
)
_stock_min_funding_sat: Optional[int] = None

# --- optional dev fee persistence -----------------------------------------
# Running total (whole sats) of dev fee accrued on plugin-initiated reverse
# swaps but not yet paid out. A single integer per wallet; incremented when a
# swap completes, decremented when a payout settles. Survives restarts so fees
# owed across a restart are still paid.
DEV_FEE_OWED_DB_KEY = "inbound_liquidity_dev_fee_owed_sat"
# Recent dev-fee *payout* timestamps+amounts, for the rolling-24h payout ceiling:
# a list of [epoch_sec, amount_sat]. Pruned to the window (plus a backstop) on
# every write, mirroring ACTION_TIMESTAMPS. Survives restarts.
DEV_FEE_PAYMENTS_DB_KEY = "inbound_liquidity_dev_fee_payments"
MAX_DEV_FEE_PAYMENTS = 1000
# The dev fee is paid out in batches: nothing is sent until at least this much
# has accrued (avoids dust-spamming the payout address with sub-sat swaps), and
# no more than the daily cap is sent in any trailing 24h (a runaway-spend guard;
# any excess stays owed and is paid on a later day). Fixed constants, not knobs.
DEV_FEE_PAYOUT_THRESHOLD_SAT = 1_000
DEV_FEE_DAILY_CAP_SAT = 10_000
# Upper bound on the user-configurable fee percentage (0-5%).
DEV_FEE_MAX_PCT = 5.0
# After a failed payout attempt, wait at least this long before retrying, so a
# persistently failing address (offline, no route) doesn't hammer the network on
# every evaluation tick. The fee stays owed across the backoff.
DEV_FEE_RETRY_BACKOFF_SEC = 300.0

# --- offline-channel auto-close persistence -------------------------------
# Channel ids (hex) the plugin itself opened -- the ONLY channels eligible for
# offline auto-close. Manually-opened channels are never auto-closed. A plain
# JSON list, appended on every successful plugin-initiated open.
PLUGIN_OPENED_CHANNELS_DB_KEY = "inbound_liquidity_plugin_opened_channels"
# Per-channel peer-uptime accumulators (see liquidity_manager.record_uptime_sample):
# channel_id(hex) -> {"buckets": {...}, "last_ts": float, "last_online": bool}.
CHANNEL_UPTIME_DB_KEY = "inbound_liquidity_channel_uptime"
# Per-channel close intent, once the peer looks gone: channel_id(hex) ->
# {"marked_ts": float, "reason": str}. The "trying to close since T" clock the
# force-close deadline is measured from; survives restarts.
CLOSE_INTENT_DB_KEY = "inbound_liquidity_close_intent"

# Offline auto-close defaults (all editable from the Settings tab). Times are in
# DAYS and stored as floats so tests can set sub-minute values.
DEFAULT_OFFLINE_AUTOCLOSE_ENABLED = True
# Trailing window over which peer uptime is measured (the "is this peer gone?"
# lookback). Shorter than the force-close deadline so a clearly-absent peer is
# committed to closing promptly, then given the deadline to close.
DEFAULT_OFFLINE_UPTIME_WINDOW_DAYS = 2.0
# Commit to closing when peer uptime over the window falls below this percent.
DEFAULT_OFFLINE_MIN_UPTIME_PCT = 10.0
# The "trying to close for N days" horizon: force-close this long after we
# committed, if the channel still hasn't closed (peer never agreed / stayed off).
DEFAULT_OFFLINE_FORCE_CLOSE_DAYS = 7.0
# Don't re-launch a cooperative close on the same channel more often than this
# (a cooperative close is async and can take minutes; each tick would otherwise
# pile on another attempt).
COOP_CLOSE_COOLDOWN_SEC = 300.0


# --- User-configurable settings -------------------------------------------
# All keys live under `plugins.inbound_liquidity.*` and are editable from the
# plugin's settings dialog (the "status menu").
SimpleConfig.INBOUND_LIQUIDITY_AUTOMATION_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.automation_enabled', default=False, type_=bool, plugin=__name__,
    short_desc=lambda: _("Automation enabled"),
    long_desc=lambda: _("Master switch (the ENABLED/DISABLED slider on the Settings tab). "
                        "Off by default: the plugin loads and can be configured, but takes no "
                        "action — it moves no funds and alters no channels — until enabled."))
SimpleConfig.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT = ConfigVar(
    'plugins.inbound_liquidity.min_onchain_to_open_sat', default=50_000, type_=int, plugin=__name__,
    short_desc=lambda: _("Min on-chain to open a channel (sat)"),
    long_desc=lambda: _("Never open a Lightning channel while on-chain spendable funds are below this. "
                        "When this is below Electrum's stock channel-funding floor (MIN_FUNDING_SAT), "
                        "the plugin lowers that floor to this value at startup so smaller channels can "
                        "be opened."))
SimpleConfig.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT = ConfigVar(
    'plugins.inbound_liquidity.onchain_reserve_sat', default=10_000, type_=int, plugin=__name__,
    short_desc=lambda: _("On-chain reserve when opening (sat)"),
    long_desc=lambda: _("When opening a channel, fund with the maximum available, leaving this much on-chain."))
SimpleConfig.INBOUND_LIQUIDITY_MAX_CHANNELS = ConfigVar(
    'plugins.inbound_liquidity.max_channels', default=2, type_=int, plugin=__name__,
    short_desc=lambda: _("Maximum number of channels"),
    long_desc=lambda: _("Never hold more than this many channels."))
# Daily action ceilings (rolling 24h) -- a runaway guard that bounds how many
# fund-spending actions the automation can take per day. Edited from the Advanced
# sub-tab. 0 disables a ceiling (unlimited). See DAILY_WINDOW_SEC.
SimpleConfig.INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY = ConfigVar(
    'plugins.inbound_liquidity.max_opens_per_day', default=DEFAULT_MAX_OPENS_PER_DAY,
    type_=int, plugin=__name__,
    short_desc=lambda: _("Max channel opens per day"),
    long_desc=lambda: _("Never open more than this many Lightning channels in any rolling "
                        "24-hour window, to bound a runaway automation loop. 0 = unlimited."))
SimpleConfig.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY = ConfigVar(
    'plugins.inbound_liquidity.max_closes_per_day', default=DEFAULT_MAX_CLOSES_PER_DAY,
    type_=int, plugin=__name__,
    short_desc=lambda: _("Max channel closes per day"),
    long_desc=lambda: _("Never close more than this many channels in any rolling 24-hour "
                        "window. This includes the emergency force-close of a channel whose "
                        "open got wedged; once the ceiling is reached, a wedged open is not "
                        "auto-freed until the window rolls over. 0 = unlimited."))
SimpleConfig.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT = ConfigVar(
    'plugins.inbound_liquidity.max_swap_fee_pct', default=0.6, type_=float, plugin=__name__,
    short_desc=lambda: _("Max fee to move LN → on-chain (%, all-in)"),
    long_desc=lambda: _("Do not swap Lightning -> on-chain if the effective all-in cost "
                        "(percentage fee + provider mining fee + on-chain claim fee, as a "
                        "share of the amount) exceeds this."))
SimpleConfig.INBOUND_LIQUIDITY_DEV_FEE_PCT = ConfigVar(
    'plugins.inbound_liquidity.dev_fee_pct', default=0.1, type_=float, plugin=__name__,
    short_desc=lambda: _("Dev fee (% of amount swapped)"),
    long_desc=lambda: _("Optional fee to support plugin development, charged on the on-chain "
                        "amount received from reverse swaps the plugin initiates. Accrues until "
                        "at least {thr} sat is owed, then is paid automatically to the payout "
                        "Lightning address (capped at {cap} sat/day). Range 0-{max}%; 0 disables "
                        "it.").format(thr=DEV_FEE_PAYOUT_THRESHOLD_SAT,
                                      cap=DEV_FEE_DAILY_CAP_SAT, max=int(DEV_FEE_MAX_PCT)))
SimpleConfig.INBOUND_LIQUIDITY_DEV_FEE_ADDRESS = ConfigVar(
    'plugins.inbound_liquidity.dev_fee_address', default='electrum_liqhelper@getbarebits.com',
    type_=str, plugin=__name__,
    short_desc=lambda: _("Dev fee payout address"),
    long_desc=lambda: _("Lightning address (user@domain) or LNURL-pay the accrued dev fee is "
                        "paid to. Leave at the default to support development of this plugin."))
SimpleConfig.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT = ConfigVar(
    'plugins.inbound_liquidity.swap_trigger_pct', default=25.0, type_=float, plugin=__name__,
    short_desc=lambda: _("Swap-out trigger (% of capacity)"),
    long_desc=lambda: _("Reverse-swap a channel once its local balance reaches this share of its capacity."))
SimpleConfig.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT = ConfigVar(
    'plugins.inbound_liquidity.swap_trigger_sat', default=25_000, type_=int, plugin=__name__,
    short_desc=lambda: _("Swap-out trigger (sat)"),
    long_desc=lambda: _("...or once a channel's local balance exceeds this many sats (whichever comes first)."))
SimpleConfig.INBOUND_LIQUIDITY_CHANNEL_PEER = ConfigVar(
    'plugins.inbound_liquidity.channel_peer', default='', type_=str, plugin=__name__,
    short_desc=lambda: _("Channel peer (node_id@host:port)"),
    long_desc=lambda: _("Node to open channels to. If empty, Electrum's suggested peer is used."))
# Channel-partner selection. Channels are opened to Electrum's suggested peer by
# default; these lists steer that choice (edited from the Channel partners tab).
# Unlike swap-provider npubs, these are Lightning node ids (pubkeys, optionally
# ``@host:port``). ``preferred_partners`` is an ORDERED try-first list (not a
# strict whitelist) -- the old single ``channel_peer`` setting is migrated into
# its front on load. ``partners_strict`` turns it into a whitelist (never fall
# back to suggestions). ``banned_partners`` are never opened to, matched by pubkey.
SimpleConfig.INBOUND_LIQUIDITY_PREFERRED_PARTNERS = ConfigVar(
    'plugins.inbound_liquidity.preferred_partners', default='', type_=str, plugin=__name__,
    short_desc=lambda: _("Preferred channel partners"),
    long_desc=lambda: _("Lightning nodes (node_id@host:port) to try opening channels to first, "
                        "in order, before falling back to Electrum's suggested peer."))
SimpleConfig.INBOUND_LIQUIDITY_BANNED_PARTNERS = ConfigVar(
    'plugins.inbound_liquidity.banned_partners', default='', type_=str, plugin=__name__,
    short_desc=lambda: _("Banned channel partners"),
    long_desc=lambda: _("Lightning nodes (by node id) never opened to, even if Electrum suggests them."))
SimpleConfig.INBOUND_LIQUIDITY_PARTNERS_STRICT = ConfigVar(
    'plugins.inbound_liquidity.partners_strict', default=False, type_=bool, plugin=__name__,
    short_desc=lambda: _("Only open channels to preferred partners"),
    long_desc=lambda: _("When set, channels are ONLY opened to preferred partners; if none can be "
                        "reached, no channel is opened (Electrum's suggestion is not used)."))
# One-channel-per-peer guard (ON by default). When set, the plugin never opens a
# second channel to a Lightning node it already has a (non-closed) channel with:
# such peers are excluded from the channel-open try-order, so capacity is spread
# across distinct peers rather than stacked on one. A peer becomes eligible again
# only once its existing channel is fully closed/redeemed. Governs the plugin's
# OWN automated opens only -- manual opens through Electrum's UI are untouched.
SimpleConfig.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER = ConfigVar(
    'plugins.inbound_liquidity.one_channel_per_peer', default=True, type_=bool, plugin=__name__,
    short_desc=lambda: _("Only one channel per peer"),
    long_desc=lambda: _("When set, the plugin will not open a second channel to a node it already "
                        "has a channel with (its automated opens go to new peers instead)."))
SimpleConfig.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS = ConfigVar(
    'plugins.inbound_liquidity.log_retention_days', default=DEFAULT_LOG_RETENTION_DAYS,
    type_=int, plugin=__name__,
    short_desc=lambda: _("Keep decision log for (days)"),
    long_desc=lambda: _("Decision-log entries older than this are pruned. "
                        "Default 30 days; up to 999."))
# Optional on-disk diagnostic log (OFF by default). When enabled, every decision
# already written to the in-wallet decision log -- plus operational errors -- is
# also appended to per-wallet daily JSON-lines files under the Electrum data dir
# (rotated daily, kept DIAG_LOG_RETENTION_DAYS). It carries no more than the GUI
# log: ids are abbreviated and no key material is ever written. See diag_log.py.
SimpleConfig.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.diag_log_enabled', default=False, type_=bool, plugin=__name__,
    short_desc=lambda: _("Write diagnostic log files"),
    long_desc=lambda: _("When set, the plugin writes a diagnostic log of its decisions and "
                        "errors to daily text files (one per wallet, kept 30 days) under the "
                        "Electrum data directory. Contains no private keys or seeds. Off by default."))
# Provider selection. The plugin discovers swap providers on nostr and uses the
# cheapest one per swap; these two lists (comma-separated npubs) constrain that
# choice. They are edited from the Providers sub-tab, not free-form normally.
SimpleConfig.INBOUND_LIQUIDITY_PREFERRED_NPUBS = ConfigVar(
    'plugins.inbound_liquidity.preferred_npubs', default='', type_=str, plugin=__name__,
    short_desc=lambda: _("Preferred swap providers (npubs)"),
    long_desc=lambda: _("If non-empty, ONLY these providers are ever used (the cheapest "
                        "among them). If none of them are currently available, no swap is made."))
SimpleConfig.INBOUND_LIQUIDITY_BANNED_NPUBS = ConfigVar(
    'plugins.inbound_liquidity.banned_npubs', default='', type_=str, plugin=__name__,
    short_desc=lambda: _("Banned swap providers (npubs)"),
    long_desc=lambda: _("Providers that are never used, even if they are the cheapest."))
# Reliability tracking. The plugin remembers each provider's recent faults
# (unreachable / RPC errors / stuck swaps) and adds a decaying penalty to that
# provider's cost *for ranking only* -- a flaky provider sinks behind reliable
# ones (soft de-prioritisation) but is still used when it is the only option.
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.reliability_enabled', default=True, type_=bool, plugin=__name__,
    short_desc=lambda: _("Track provider reliability"),
    long_desc=lambda: _("Remember providers that time out, return errors, or leave swaps stuck, "
                        "and prefer more reliable providers over them."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_BASE_PENALTY_PCT = ConfigVar(
    'plugins.inbound_liquidity.reliability_base_penalty_pct', default=0.5, type_=float, plugin=__name__,
    short_desc=lambda: _("Reliability penalty per fault (%)"),
    long_desc=lambda: _("Cost penalty (percentage points) added to a provider's ranking after one "
                        "fault; it doubles for each consecutive fault and decays over time."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_PENALTY_CAP_PCT = ConfigVar(
    'plugins.inbound_liquidity.reliability_penalty_cap_pct', default=5.0, type_=float, plugin=__name__,
    short_desc=lambda: _("Max reliability penalty (%)"),
    long_desc=lambda: _("Upper bound on the ranking penalty, so even a chronically failing "
                        "provider is only de-prioritised, never excluded outright."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS = ConfigVar(
    'plugins.inbound_liquidity.reliability_halflife_hours', default=6.0, type_=float, plugin=__name__,
    short_desc=lambda: _("Reliability recovery half-life (hours)"),
    long_desc=lambda: _("How fast a provider's penalty fades after its last fault: it halves every "
                        "this many hours, so a provider that stops failing recovers automatically."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_STUCK_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.reliability_stuck_timeout_min', default=60, type_=int, plugin=__name__,
    short_desc=lambda: _("Stuck-swap timeout (minutes)"),
    long_desc=lambda: _("If a reverse swap is accepted but no on-chain funding appears within this "
                        "long, count it as a fault against the provider that accepted it."))
# Channel-peer reliability. Distinct from provider reliability above: this tracks
# the Lightning nodes we hold channels with. It reuses the same decaying-penalty
# tuning (base / cap / half-life) but applies it to the channel-partner try-order,
# and adds an auto-ban after enough *hard* faults (force-close / repeated open
# failure) plus a watchdog that force-closes a channel wedged opening too long.
SimpleConfig.INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.peer_reliability_enabled', default=True, type_=bool, plugin=__name__,
    short_desc=lambda: _("Track channel-peer reliability"),
    long_desc=lambda: _("Remember channel peers that fail to open, go offline, or force-close, and "
                        "prefer more reliable peers when opening new channels."))
SimpleConfig.INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS = ConfigVar(
    'plugins.inbound_liquidity.peer_autoban_faults', default=3, type_=int, plugin=__name__,
    short_desc=lambda: _("Auto-ban a peer after N hard faults"),
    long_desc=lambda: _("After this many hard faults (a force-close, or repeated channel-open "
                        "failures), a peer is added to the banned list automatically. 0 disables "
                        "auto-banning (peers are only de-prioritised, never banned)."))
SimpleConfig.INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.stuck_open_timeout_min', default=60, type_=int, plugin=__name__,
    short_desc=lambda: _("Stuck channel-open timeout (minutes)"),
    long_desc=lambda: _("A channel that has been opening (not yet usable) for longer than this is "
                        "treated as wedged by an unresponsive peer: it stops freezing automation and "
                        "is counted as a hard fault against the peer."))
SimpleConfig.INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.stuck_swap_timeout_min', default=180, type_=int, plugin=__name__,
    short_desc=lambda: _("Stuck reverse-swap timeout (minutes)"),
    long_desc=lambda: _("A reverse swap whose funding has been broadcast but not swept for longer "
                        "than this is treated as wedged: it stops freezing automation so channel "
                        "opens and other swaps can resume. The swap is still tracked and its "
                        "provider still faulted separately. Give on-chain funding ample time to "
                        "confirm before escaping (default 3 hours)."))
SimpleConfig.INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN = ConfigVar(
    'plugins.inbound_liquidity.auto_remediate_stuck_open', default=True, type_=bool, plugin=__name__,
    short_desc=lambda: _("Force-close wedged channel opens"),
    long_desc=lambda: _("When a channel open is wedged past the timeout, force-close it to free the "
                        "funds and resume automation. This broadcasts an on-chain transaction and "
                        "incurs a mining fee. When off, the open is only un-frozen and flagged."))
# Offline-channel auto-close. Applies ONLY to channels the plugin itself opened.
# A channel whose peer has been effectively gone (uptime over the window below
# the floor) is committed to closing: a cooperative close is attempted whenever
# the peer is reachable, and if it still hasn't closed after the force-close
# horizon it is force-closed. Enabled by default (like the wedged-open remedy).
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.offline_autoclose_enabled',
    default=DEFAULT_OFFLINE_AUTOCLOSE_ENABLED, type_=bool, plugin=__name__,
    short_desc=lambda: _("Auto-close channels whose peer stays offline"),
    long_desc=lambda: _("For channels this plugin opened, when the peer has been effectively offline "
                        "for a sustained period, try to close the channel cooperatively, and "
                        "force-close it if it still hasn't closed after the force-close deadline."))
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS = ConfigVar(
    'plugins.inbound_liquidity.offline_uptime_window_days',
    default=DEFAULT_OFFLINE_UPTIME_WINDOW_DAYS, type_=float, plugin=__name__,
    short_desc=lambda: _("Peer-uptime window (days)"),
    long_desc=lambda: _("How far back the peer's uptime is measured when deciding whether it is gone. "
                        "A peer whose uptime over this window falls below the minimum is treated as "
                        "offline and the channel is closed."))
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT = ConfigVar(
    'plugins.inbound_liquidity.offline_min_uptime_pct',
    default=DEFAULT_OFFLINE_MIN_UPTIME_PCT, type_=float, plugin=__name__,
    short_desc=lambda: _("Minimum peer uptime (%)"),
    long_desc=lambda: _("If a channel peer is reachable less than this percent of the uptime window, "
                        "the peer is considered gone and the channel is closed. Lower = more tolerant "
                        "of a flaky peer before closing."))
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS = ConfigVar(
    'plugins.inbound_liquidity.offline_force_close_days',
    default=DEFAULT_OFFLINE_FORCE_CLOSE_DAYS, type_=float, plugin=__name__,
    short_desc=lambda: _("Force-close after trying to close for (days)"),
    long_desc=lambda: _("Once the plugin has been trying to close an offline channel for this many "
                        "days without success (the peer never agreed / stayed offline), it force-closes "
                        "the channel. This broadcasts a transaction, incurs a mining fee, and timelocks "
                        "your funds. Counts against the daily channel-close ceiling."))


class LiquidityPlugin(BasePlugin):
    """Base plugin: snapshots wallet state, runs the rules engine, executes."""

    def __init__(self, parent, config: 'SimpleConfig', name: str) -> None:
        BasePlugin.__init__(self, parent, config, name)
        # Wallets we are managing -> a re-entrancy guard so we never run two
        # evaluation loops (or fire two actions) for the same wallet at once.
        self.wallets: Dict['Abstract_Wallet', asyncio.Lock] = {}
        # channel_id (hex) -> monotonic time until which we won't re-swap it.
        self._swap_cooldown_until: Dict[str, float] = {}
        # wallet -> whether an evaluation is already queued (event debounce).
        self._eval_pending: Dict['Abstract_Wallet', bool] = {}
        # wallet -> set of decline signatures from the previous evaluation, so a
        # tick that declines for the same reasons every event does not flood the
        # log with identical rows (only newly-appearing declines add a row).
        self._last_decline_sigs: Dict['Abstract_Wallet', set] = {}
        # wallet -> last set of providers discovered on nostr, so the Providers
        # settings tab has something to show between/without live transports.
        self._last_offers: Dict['Abstract_Wallet', List[ProviderOffer]] = {}
        # wallet -> channel_ids whose wedged open we have already remediated, so
        # the watchdog acts once and the force-close it triggers is not also
        # counted as a peer-initiated close fault.
        self._remediating_opens: Dict['Abstract_Wallet', set] = {}
        # wallet -> channel_ids for closes *we* initiated locally -- the user via
        # the GUI/CLI/console, or this plugin's own auto-close. Populated by the
        # close hooks installed on the lnworker (see _install_close_hooks), because
        # Electrum does not persist who initiated a cooperative close. The health
        # watchdog exempts these so our own close is never mis-blamed on the peer
        # as a "closed by peer" hard fault.
        self._local_closes: Dict['Abstract_Wallet', set] = {}
        # wallet -> channel_ids whose wedged open we have already faulted the peer
        # for. Decoupled from _remediating_opens so the fault is recorded exactly
        # once even when the remediating force-close is deferred by the daily
        # close ceiling (and thus retried on a later tick).
        self._wedged_faulted: Dict['Abstract_Wallet', set] = {}
        # wallet -> channel_ids for which we have already logged that the daily
        # close ceiling is blocking remediation, so the deferral is logged once
        # (not every tick) while the channel waits for the window to roll over.
        self._close_capped_logged: Dict['Abstract_Wallet', set] = {}
        # wallet -> {channel_id: was_closing} from the previous health scan, so a
        # *transition* into a closing/closed state (rising edge) faults the peer
        # exactly once -- and channels already closed before we started managing
        # (present on the very first scan) are seeded, not retroactively faulted.
        self._known_chan_states: Dict['Abstract_Wallet', Dict[str, bool]] = {}
        # wallet -> channel_ids with a cooperative close in flight (async), so the
        # offline auto-close watchdog does not launch a second one each tick while
        # the first is still negotiating.
        self._coop_closing: Dict['Abstract_Wallet', set] = {}
        # channel_id (hex) -> monotonic time until which we won't re-attempt a
        # cooperative close on it (a cooperative close can take minutes).
        self._coop_close_cooldown_until: Dict[str, float] = {}
        # wallet -> True while a dev-fee payout is in flight (async), so we launch
        # at most one payout at a time per wallet.
        self._dev_fee_paying: Dict['Abstract_Wallet', bool] = {}
        # wallet -> monotonic time until which we won't re-attempt a dev-fee payout
        # after a failure (backoff, so a persistently failing address doesn't
        # hammer the network every tick). The fee stays owed across the backoff.
        self._dev_fee_retry_until: Dict['Abstract_Wallet', float] = {}
        # wallet -> wall-clock time the wallet was loaded (start_wallet). Drives
        # the startup settle window: until STARTUP_GRACE_SEC has elapsed the
        # plugin defers all automation and treats not-yet-connected peers as
        # "not observed" rather than offline.
        self._started_at: Dict['Abstract_Wallet', float] = {}
        # wallet -> set of node_ids (hex) we have seen ONLINE at least once since
        # load. A peer in this set that later reads inactive is a genuine offline
        # (real outage), whereas one never yet seen online -- during the grace --
        # is just not-connected-yet and must not be faulted. Cleared on stop so a
        # reload re-earns the benefit of the doubt.
        self._peer_seen_online: Dict['Abstract_Wallet', set] = {}
        # Fixed startup grace, as an instance attribute so tests can shrink it
        # (the module constant stays the production default).
        self._startup_grace_sec: float = STARTUP_GRACE_SEC
        # Coarse backstop on a whole reverse-swap attempt, as an instance
        # attribute so tests can shrink it (the module constant is the default).
        self._reverse_swap_timeout_sec: float = REVERSE_SWAP_TIMEOUT_SEC
        # wallet -> payment_hash hexes of stuck swaps we have already logged as
        # having aged out of the freeze, so the escape is logged once per swap (not
        # every tick) while it stays pending. Pruned against the live pending set.
        self._swap_freeze_escaped_logged: Dict['Abstract_Wallet', set] = {}
        # wallet -> the running heartbeat task (a concurrent.futures.Future from
        # run_coroutine_threadsafe), so we can cancel it in stop_wallet and never
        # leave a periodic loop running for a wallet we no longer manage.
        self._heartbeat_tasks: Dict['Abstract_Wallet', "futures.Future"] = {}
        # Fixed heartbeat cadence, as an instance attribute so tests can shrink it
        # (the module constant stays the production default).
        self._heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC
        util.register_callback(self._on_wallet_event, _TRIGGER_EVENTS)

    # --- lifecycle --------------------------------------------------------
    def start_wallet(self, wallet: 'Abstract_Wallet') -> None:
        if not wallet.has_lightning() or wallet.lnworker is None:
            self.logger.info(f"{wallet.basename()} has no lightning; not managing")
            return
        if wallet.network is None:
            return  # offline mode
        self._migrate_channel_peer()
        # Lower Electrum's channel-funding floor to the configured min_onchain if
        # it is smaller, so the plugin can open sub-stock channels from startup.
        self._enforce_min_funding_floor()
        self._install_close_hooks(wallet, wallet.lnworker)
        self.wallets.setdefault(wallet, asyncio.Lock())
        self._started_at[wallet] = time.time()
        self._peer_seen_online.setdefault(wallet, set())
        self.logger.info(f"managing inbound liquidity for {wallet.basename()}")
        self._diag_event(wallet, category="lifecycle", kind="start",
                         reason="started managing wallet")
        # Evaluate once on load so we act on whatever state already exists. During
        # the startup grace this evaluation defers (see _wallet_ready); schedule a
        # follow-up just past the grace so automation actually kicks in once the
        # wallet has settled, even if no wallet event happens to fire by then.
        self.request_evaluation(wallet)
        self._schedule_post_grace_evaluation(wallet)
        # Periodic heartbeat so time-based watchdogs advance without depending on
        # wallet events (see HEARTBEAT_INTERVAL_SEC).
        self._start_heartbeat(wallet)

    def _start_heartbeat(self, wallet: 'Abstract_Wallet') -> None:
        """Launch the per-wallet heartbeat loop on the network's asyncio loop, if
        one is not already running. The loop re-evaluates the wallet every
        ``_heartbeat_interval_sec`` until the wallet is no longer managed; each
        tick is a guarded, idempotent ``_evaluate``. Stored so ``stop_wallet`` can
        cancel it."""
        existing = self._heartbeat_tasks.get(wallet)
        if existing is not None and not existing.done():
            return  # already beating for this wallet
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        if loop is None:
            return
        async def _heartbeat() -> None:
            while wallet in self.wallets:
                try:
                    await asyncio.sleep(self._heartbeat_interval_sec)
                except asyncio.CancelledError:
                    return  # cancelled by stop_wallet / on_close
                if wallet not in self.wallets:
                    return
                try:
                    await self._evaluate(wallet)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    # A heartbeat must never die on a transient error; log and let
                    # the next tick try again. (_evaluate already swallows its own
                    # errors, so this is only a backstop for anything outside it.)
                    self.logger.info(f"heartbeat tick error: {e!r}")
        self._heartbeat_tasks[wallet] = asyncio.run_coroutine_threadsafe(_heartbeat(), loop)

    def _enforce_min_funding_floor(self) -> int:
        """Lower Electrum's channel-funding floor (``MIN_FUNDING_SAT``) to the
        configured ``min_onchain_to_open_sat`` when it is below the stock floor,
        so the plugin (and manual opens) can create smaller channels. Only ever
        lowers, never raises above stock. Re-applied at startup and on every tick
        so the user's value always wins even if some code path resets it. Returns
        the floor now in force (in sat)."""
        global _stock_min_funding_sat
        # Capture the stock floor exactly once, before we ever lower it, so
        # re-assertion is always measured against the real default.
        if _stock_min_funding_sat is None:
            lnutil = sys.modules.get('electrum.lnutil')
            stock = getattr(lnutil, 'MIN_FUNDING_SAT', None) if lnutil is not None else None
            _stock_min_funding_sat = int(stock) if stock is not None else int(MIN_FUNDING_SAT)
        stock = _stock_min_funding_sat
        try:
            configured = int(getattr(self.config, 'INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT', stock))
        except (TypeError, ValueError):
            configured = stock
        desired = max(1, min(stock, configured))
        # Patch every already-imported binding: Electrum's core/GUI modules plus
        # this plugin's own engine mirror and glue import of the constant.
        names = list(_MIN_FUNDING_CORE_MODULES) + [__name__ + '.liquidity_manager', __name__]
        for name in names:
            mod = sys.modules.get(name)
            if mod is None or not hasattr(mod, 'MIN_FUNDING_SAT'):
                continue
            if getattr(mod, 'MIN_FUNDING_SAT') != desired:
                try:
                    setattr(mod, 'MIN_FUNDING_SAT', desired)
                except Exception:
                    self.logger.exception(f"could not lower MIN_FUNDING_SAT in {name}")
        return desired

    def _schedule_post_grace_evaluation(self, wallet: 'Abstract_Wallet') -> None:
        """Fire one evaluation shortly after the startup grace elapses, so a
        wallet that settles quietly (no further events) still starts automating
        instead of waiting for the next external trigger."""
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        if loop is None:
            return
        async def _wait_then_evaluate() -> None:
            await asyncio.sleep(self._startup_grace_sec + 1.0)
            if wallet in self.wallets:
                await self._evaluate(wallet)
        asyncio.run_coroutine_threadsafe(_wait_then_evaluate(), loop)

    # Every per-wallet dict, so stop_wallet can drop all of a wallet's state in
    # one place. Keeping this list next to the fields it mirrors means a new
    # per-wallet dict is a one-line add here, not a silent leak. NB: `wallets`
    # (the lock map) is popped first and separately in stop_wallet, because the
    # heartbeat loop watches it to know when to exit.
    _PER_WALLET_STATE_ATTRS = (
        "_eval_pending", "_last_decline_sigs", "_last_offers", "_remediating_opens",
        "_local_closes", "_wedged_faulted", "_close_capped_logged",
        "_known_chan_states", "_coop_closing", "_dev_fee_paying", "_dev_fee_retry_until",
        "_started_at", "_peer_seen_online", "_swap_freeze_escaped_logged",
        "_heartbeat_tasks",
    )

    def _forget_wallet(self, wallet: 'Abstract_Wallet') -> None:
        """Drop every scrap of per-wallet state, so a stopped wallet leaks nothing
        and a later reload of the same wallet object starts clean (no stale
        cooldown, no wedged _dev_fee_paying / _eval_pending guard flag)."""
        for attr_name in self._PER_WALLET_STATE_ATTRS:
            store = getattr(self, attr_name, None)
            if isinstance(store, dict):
                store.pop(wallet, None)

    def stop_wallet(self, wallet: 'Abstract_Wallet') -> None:
        # Remove from the managed set first so the heartbeat loop sees it gone and
        # exits, then cancel it outright so we don't wait out a whole interval.
        self.wallets.pop(wallet, None)
        hb = self._heartbeat_tasks.get(wallet)
        if hb is not None:
            hb.cancel()
        self._forget_wallet(wallet)

    def on_close(self) -> None:
        """Plugin teardown (Electrum calls BasePlugin.close -> on_close on disable/
        unload): stop every heartbeat and unregister our global event callback, so
        a disabled plugin leaves no running loops or live callbacks behind."""
        for hb in list(self._heartbeat_tasks.values()):
            try:
                hb.cancel()
            except Exception:
                pass
        self._heartbeat_tasks.clear()
        try:
            util.unregister_callback(self._on_wallet_event)
        except Exception as e:
            self.logger.info(f"could not unregister event callback: {e!r}")

    def _install_close_hooks(self, wallet: 'Abstract_Wallet', lnworker) -> None:
        """Wrap the lnworker's local close entry points so that a close *we*
        initiate -- the user via the GUI ("Cooperative close" / force close) or
        the CLI/console, and this plugin's own auto-close -- is recorded in
        ``self._local_closes`` at the call site. The channel-health watchdog then
        exempts those channels from the "closed by peer" hard fault.

        This is necessary because Electrum keeps no persisted record of who
        initiated a *cooperative* close (``is_local`` lives only transiently in
        ``lnpeer._shutdown``; ``lnchannel.who_closed`` distinguishes sides only
        for force-closes), so the only reliable signal is to capture it as the
        close is requested. All manual close paths (GUI ``channels_list`` and CLI
        ``commands``) route through these lnworker methods, so wrapping them here
        -- purely on the plugin side, no Electrum change -- covers every case.

        Idempotent per lnworker instance (guarded by a marker attribute) so a
        re-managed wallet does not stack wrappers.
        """
        if lnworker is None or getattr(lnworker, "_inbound_liquidity_close_hooked", False):
            return

        def _record(chan_id) -> None:
            try:
                cid = chan_id.hex() if isinstance(chan_id, (bytes, bytearray)) else str(chan_id)
            except Exception:
                return
            # Look the set up at call time (not captured once) so it stays the
            # live one even after stop_wallet cleared and start_wallet re-seeded it.
            self._local_closes.setdefault(wallet, set()).add(cid)
            self.logger.info(f"local close initiated for channel {cid[:12]}…; "
                             f"will not be blamed on the peer")

        def _wrap(name: str) -> None:
            orig = getattr(lnworker, name, None)
            if orig is None or not callable(orig):
                return
            if asyncio.iscoroutinefunction(orig):
                @functools.wraps(orig)
                async def _hooked(chan_id, *args, **kwargs):
                    _record(chan_id)
                    return await orig(chan_id, *args, **kwargs)
            else:
                @functools.wraps(orig)
                def _hooked(chan_id, *args, **kwargs):
                    _record(chan_id)
                    return orig(chan_id, *args, **kwargs)
            setattr(lnworker, name, _hooked)

        for _name in ("close_channel", "force_close_channel",
                      "request_force_close", "schedule_force_closing"):
            _wrap(_name)
        lnworker._inbound_liquidity_close_hooked = True

    def _wallet_ready(self, wallet: 'Abstract_Wallet') -> bool:
        """Whether ``wallet`` has settled enough to take automated action.

        Defers everything until we have a live server connection AND the startup
        grace has elapsed since load -- the window in which not-yet-connected
        peers masquerade as offline. A wallet we are not managing (no recorded
        load time) is never ready."""
        started = self._started_at.get(wallet)
        if started is None:
            return False
        network = getattr(wallet, "network", None)
        try:
            connected = bool(network is not None and network.is_connected())
        except Exception:
            connected = False
        return is_wallet_ready(connected, time.time() - started,
                               self._startup_grace_sec)

    def request_evaluation(self, wallet: 'Abstract_Wallet') -> None:
        """Schedule one evaluation of `wallet` on the network's asyncio loop.

        Used both on wallet load and when the user arms the plugin from the UI
        (the ENABLED/DISABLED slider), so enabling takes effect immediately
        rather than waiting for the next wallet event. A no-op when the wallet is
        offline (no loop) or not managed; `_evaluate` re-reads config and returns
        early when automation is disabled, so a stray call is harmless.
        """
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self._evaluate(wallet), loop)

    # --- config -> engine -------------------------------------------------
    def read_config(self) -> LiquidityConfig:
        c = self.config
        return LiquidityConfig(
            automation_enabled=bool(c.INBOUND_LIQUIDITY_AUTOMATION_ENABLED),
            min_onchain_to_open_sat=int(c.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT),
            onchain_reserve_sat=int(c.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT),
            max_channels=int(c.INBOUND_LIQUIDITY_MAX_CHANNELS),
            max_swap_fee_pct=float(c.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT),
            swap_trigger_pct=float(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT),
            swap_trigger_sat=int(c.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT),
            max_opens_per_day=self._max_opens_per_day(),
            preferred_npubs=_parse_npub_set(c.INBOUND_LIQUIDITY_PREFERRED_NPUBS),
            banned_npubs=_parse_npub_set(c.INBOUND_LIQUIDITY_BANNED_NPUBS),
        )

    # --- daily action ceilings (rolling 24h) ------------------------------
    # A runaway guard: at most N opens / N closes in any trailing 24h window.
    # Counts are kept in a dedicated per-wallet store (not derived from the
    # decision log, so they survive the log's retention/size pruning). The open
    # ceiling is enforced in the pure engine (it fed `opens_last_24h` into the
    # snapshot); the close ceiling is enforced here in the watchdog.
    def _max_opens_per_day(self) -> int:
        return max(0, int(getattr(
            self.config, "INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY", DEFAULT_MAX_OPENS_PER_DAY)))

    def _max_closes_per_day(self) -> int:
        return max(0, int(getattr(
            self.config, "INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY", DEFAULT_MAX_CLOSES_PER_DAY)))

    def _load_action_timestamps(self, wallet: 'Abstract_Wallet') -> Dict[str, List[float]]:
        db = getattr(wallet, "db", None)
        raw = db.get(ACTION_TIMESTAMPS_DB_KEY, {}) if db is not None else {}
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, List[float]] = {}
        for kind, tss in raw.items():
            if isinstance(tss, list):
                out[kind] = [float(t) for t in tss if isinstance(t, (int, float))]
        return out

    def _save_action_timestamps(self, wallet: 'Abstract_Wallet',
                                data: Dict[str, List[float]]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        db.put(ACTION_TIMESTAMPS_DB_KEY, data)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist action timestamps: {e!r}")

    def _count_actions_last_24h(self, wallet: 'Abstract_Wallet', kind: str,
                                now: Optional[float] = None) -> int:
        """How many ``kind`` actions ("open" / "close") were recorded in the
        trailing 24h. The rolling-window count behind the daily ceilings."""
        if now is None:
            now = time.time()
        data = self._load_action_timestamps(wallet)
        return count_within_window(data.get(kind, []), now, DAILY_WINDOW_SEC)

    def _record_action_event(self, wallet: 'Abstract_Wallet', kind: str) -> None:
        """Stamp that we just executed a ``kind`` action (an open or a close), so
        it counts toward that ceiling's rolling window. Prunes the kept history to
        the window (plus a bounded backstop) so the store can't grow unbounded."""
        now = time.time()
        data = self._load_action_timestamps(wallet)
        tss = list(data.get(kind, []))
        tss.append(now)
        cutoff = now - DAILY_WINDOW_SEC
        tss = [t for t in tss if t >= cutoff][-MAX_ACTION_TIMESTAMPS:]
        data[kind] = tss
        self._save_action_timestamps(wallet, data)

    def _within_close_cap(self, wallet: 'Abstract_Wallet',
                          now: Optional[float] = None) -> bool:
        """True if a channel close is still allowed under the rolling-24h close
        ceiling (or the ceiling is disabled)."""
        cap = self._max_closes_per_day()
        if cap <= 0:
            return True
        return not daily_cap_reached(self._count_actions_last_24h(wallet, "close", now), cap)

    # --- optional dev fee: ledger + accrual ------------------------------
    # The plugin can charge an optional development fee on the reverse swaps it
    # initiates (LN -> on-chain). The fee accrues into a per-wallet running total
    # (`fee owed`) the moment a swap is *confirmed complete* -- never on mere
    # provider acceptance, so a swap that is accepted but never funds is never
    # charged. Accrual is decoupled from payout: the owed total is drawn down by
    # `_maybe_pay_dev_fee` once it clears the batch threshold. All state lives in
    # `wallet.db` so it survives restarts (fees owed must outlive a restart).
    def _dev_fee_pct(self) -> float:
        """The configured dev-fee percentage, clamped into the allowed range so a
        hand-edited config can't charge an out-of-bounds fee. 0 disables it."""
        try:
            raw = float(getattr(self.config, "INBOUND_LIQUIDITY_DEV_FEE_PCT", 0.1))
        except (TypeError, ValueError):
            raw = 0.0
        return clamp_dev_fee_pct(raw, max_pct=DEV_FEE_MAX_PCT)

    def _dev_fee_address(self) -> str:
        return (getattr(self.config, "INBOUND_LIQUIDITY_DEV_FEE_ADDRESS", "") or "").strip()

    def _load_dev_fee_owed(self, wallet: 'Abstract_Wallet') -> int:
        db = getattr(wallet, "db", None)
        raw = db.get(DEV_FEE_OWED_DB_KEY, 0) if db is not None else 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _save_dev_fee_owed(self, wallet: 'Abstract_Wallet', owed_sat: int) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        db.put(DEV_FEE_OWED_DB_KEY, max(0, int(owed_sat)))
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist dev-fee ledger: {e!r}")

    def _accrue_dev_fee(self, wallet: 'Abstract_Wallet', basis_sat: int,
                        source: str = "") -> int:
        """Add the dev fee for one completed swap (of net on-chain ``basis_sat``)
        to the owed ledger. Returns the fee added (0 if the fee is disabled or the
        amount rounds to nothing). Idempotency is the caller's responsibility --
        this is invoked exactly once per swap, at its single confirmed-completion
        point."""
        fee = compute_dev_fee(int(basis_sat), self._dev_fee_pct())
        if fee <= 0:
            return 0
        owed = self._load_dev_fee_owed(wallet) + fee
        self._save_dev_fee_owed(wallet, owed)
        self.logger.info(
            f"dev fee accrued: +{fee} sat on {basis_sat} sat swapped "
            f"({source or 'reverse swap'}); {owed} sat now owed")
        return fee

    def _load_dev_fee_payments(self, wallet: 'Abstract_Wallet') -> List[List[float]]:
        db = getattr(wallet, "db", None)
        raw = db.get(DEV_FEE_PAYMENTS_DB_KEY, []) if db is not None else []
        if not isinstance(raw, list):
            return []
        out: List[List[float]] = []
        for item in raw:
            if (isinstance(item, (list, tuple)) and len(item) == 2
                    and all(isinstance(x, (int, float)) for x in item)):
                out.append([float(item[0]), float(item[1])])
        return out

    def _save_dev_fee_payments(self, wallet: 'Abstract_Wallet',
                               data: List[List[float]]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return
        db.put(DEV_FEE_PAYMENTS_DB_KEY, data)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist dev-fee payment history: {e!r}")

    def _dev_fee_paid_last_24h(self, wallet: 'Abstract_Wallet',
                               now: Optional[float] = None) -> int:
        """Total dev fee paid out in the trailing 24h -- the rolling window the
        daily payout ceiling is enforced against."""
        if now is None:
            now = time.time()
        cutoff = now - DAILY_WINDOW_SEC
        return int(sum(amt for ts, amt in self._load_dev_fee_payments(wallet)
                       if ts >= cutoff))

    def _record_dev_fee_payment(self, wallet: 'Abstract_Wallet', amount_sat: int) -> None:
        """Stamp a settled payout so it counts toward the rolling-24h ceiling, and
        prune the history to the window (plus a bounded backstop)."""
        now = time.time()
        data = self._load_dev_fee_payments(wallet)
        data.append([now, float(int(amount_sat))])
        cutoff = now - DAILY_WINDOW_SEC
        data = [row for row in data if row[0] >= cutoff][-MAX_DEV_FEE_PAYMENTS:]
        self._save_dev_fee_payments(wallet, data)

    def dev_fee_status(self, wallet: 'Abstract_Wallet') -> Dict[str, int]:
        """A small read-only summary for the UI: sats owed and sats paid in the
        trailing 24h (plus the remaining daily headroom)."""
        paid = self._dev_fee_paid_last_24h(wallet)
        return {
            "owed_sat": self._load_dev_fee_owed(wallet),
            "paid_last_24h_sat": paid,
            "daily_headroom_sat": max(0, DEV_FEE_DAILY_CAP_SAT - paid),
        }

    # --- optional dev fee: payout ----------------------------------------
    # Accrued fees are paid out in batches to the configured Lightning address
    # via LNURL-pay: resolve the address -> fetch pay params -> request an invoice
    # for the payable amount -> pay it. The whole thing runs as a guarded
    # background task (an LN payment can take a while and must not block
    # evaluation), and is resilient: on any failure the owed ledger is left
    # untouched and a backoff is set, so the fee is simply retried on a later tick.
    @staticmethod
    def _resolve_lnurl_pay_url(address: str) -> Optional[str]:
        """Turn a configured payout target into an LNURL-pay https URL: a
        ``user@domain`` Lightning address (LUD-16), a bech32 ``lnurl1…`` string
        (LUD-01), or an already-https/.onion URL. Returns None if unrecognisable."""
        from electrum.lnurl import lightning_address_to_url, decode_lnurl
        addr = (address or "").strip()
        if not addr:
            return None
        url = lightning_address_to_url(addr)
        if url:
            return url
        low = addr.lower()
        if low.startswith("lnurl"):
            try:
                return decode_lnurl(addr)
            except Exception:
                return None
        if low.startswith("https://") or ".onion" in low:
            return addr
        return None

    def _maybe_pay_dev_fee(self, wallet: 'Abstract_Wallet') -> None:
        """Cheap synchronous gate, run on each evaluation tick: if enough dev fee
        has accrued and the daily cap allows it, launch a background payout. A
        no-op when already paying, within the failure backoff, or nothing is due.
        Payout is intentionally independent of the current fee percentage: fees
        already accrued are paid out even if the operator has since set the fee to
        0 (the money is genuinely owed)."""
        address = self._dev_fee_address()
        if not address:
            return  # no payout target configured; keep accruing
        if self._dev_fee_paying.get(wallet):
            return  # a payout is already in flight
        now_mono = time.monotonic()
        if now_mono < self._dev_fee_retry_until.get(wallet, 0.0):
            return  # backing off after a recent failure
        owed = self._load_dev_fee_owed(wallet)
        paid = self._dev_fee_paid_last_24h(wallet)
        decision = decide_dev_fee_payout(
            owed, paid,
            threshold_sat=DEV_FEE_PAYOUT_THRESHOLD_SAT,
            daily_cap_sat=DEV_FEE_DAILY_CAP_SAT)
        if not decision.should_pay:
            return
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        if loop is None:
            return  # offline wallet; try again when it has a loop
        self._dev_fee_paying[wallet] = True
        coro = self._do_pay_dev_fee(wallet, address, decision.amount_sat)
        try:
            # We're already on the network loop (called from _evaluate); schedule
            # the payout as a sibling task rather than blocking this tick on it.
            asyncio.ensure_future(coro)
        except RuntimeError:
            asyncio.run_coroutine_threadsafe(coro, loop)

    async def _do_pay_dev_fee(self, wallet: 'Abstract_Wallet', address: str,
                              amount_sat: int) -> None:
        """Resolve the payout address, request an invoice for ``amount_sat`` and
        pay it. On success, draw the amount down from the owed ledger and stamp it
        against the rolling-24h cap. On any failure, leave the ledger untouched
        and set a backoff so the same fee is retried later."""
        from electrum.lnurl import request_lnurl, callback_lnurl, LNURL6Data
        from electrum.invoices import Invoice
        lnworker = getattr(wallet, "lnworker", None)
        ok = False
        try:
            if lnworker is None:
                self.logger.info("dev-fee payout skipped: wallet has no lnworker")
                return
            url = self._resolve_lnurl_pay_url(address)
            if url is None:
                self.logger.warning(
                    f"dev-fee payout address {address!r} is not a valid Lightning "
                    f"address / LNURL; skipping")
                return
            lnurl_data = await request_lnurl(url)
            if not isinstance(lnurl_data, LNURL6Data):
                self.logger.warning("dev-fee payout: address is not an LNURL-pay endpoint")
                return
            # Respect the endpoint's min/max sendable. If we owe less than the
            # minimum, wait for more to accrue; if more than the maximum, send the
            # maximum this round and carry the rest.
            pay_sat = amount_sat
            if pay_sat > lnurl_data.max_sendable_sat:
                pay_sat = lnurl_data.max_sendable_sat
            if pay_sat < max(lnurl_data.min_sendable_sat, 1):
                self.logger.info(
                    f"dev-fee payout deferred: {amount_sat} sat owed is below the "
                    f"address minimum of {lnurl_data.min_sendable_sat} sat")
                return
            params = {"amount": pay_sat * 1000}
            invoice_data = await callback_lnurl(lnurl_data.callback_url, params=params)
            bolt11 = invoice_data.get("pr")
            if not bolt11:
                self.logger.warning("dev-fee payout: LNURL callback returned no invoice")
                return
            invoice = Invoice.from_bech32(bolt11)
            if invoice.get_amount_sat() != pay_sat:
                self.logger.warning(
                    f"dev-fee payout: invoice amount {invoice.get_amount_sat()} sat "
                    f"!= requested {pay_sat} sat; refusing to pay")
                return
            success, log = await lnworker.pay_invoice(invoice)
            if not success:
                self.logger.warning(f"dev-fee payout of {pay_sat} sat did not settle")
                return
            ok = True
            # Draw the paid amount down from the ledger (never below 0) and stamp
            # it against the daily cap. Re-read owed so a concurrent accrual isn't
            # clobbered.
            remaining = max(0, self._load_dev_fee_owed(wallet) - pay_sat)
            self._save_dev_fee_owed(wallet, remaining)
            self._record_dev_fee_payment(wallet, pay_sat)
            self.logger.info(
                f"dev-fee payout of {pay_sat} sat settled to {address}; "
                f"{remaining} sat still owed")
            self._log_action(
                wallet, kind="dev_fee", amount_sat=pay_sat,
                source=None, dest=address,
                reason="dev fee payout",
                detail=f"paid {pay_sat} sat to {address}; {remaining} sat still owed",
                state=None)
        except Exception as e:
            self.logger.warning(f"dev-fee payout failed: {e!r}")
        finally:
            self._dev_fee_paying[wallet] = False
            if not ok:
                # Back off before the next attempt so a persistently failing
                # address doesn't retry every tick. The fee stays owed.
                self._dev_fee_retry_until[wallet] = time.monotonic() + DEV_FEE_RETRY_BACKOFF_SEC

    # --- provider reliability --------------------------------------------
    def _reliability_params(self) -> Dict[str, float]:
        """The reliability tuning, read fresh so edits take effect immediately.
        Uses getattr defaults so a partial config (e.g. in unit tests) still
        yields sane values."""
        c = self.config
        return {
            "enabled": bool(getattr(c, "INBOUND_LIQUIDITY_RELIABILITY_ENABLED", True)),
            "base_pct": float(getattr(c, "INBOUND_LIQUIDITY_RELIABILITY_BASE_PENALTY_PCT", 0.5)),
            "cap_pct": float(getattr(c, "INBOUND_LIQUIDITY_RELIABILITY_PENALTY_CAP_PCT", 5.0)),
            "halflife_sec": max(0.0, float(getattr(c, "INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS", 6.0))) * 3600.0,
            "stuck_timeout_sec": max(1, int(getattr(c, "INBOUND_LIQUIDITY_RELIABILITY_STUCK_TIMEOUT_MIN", 60))) * 60.0,
        }

    def _load_reliability(self, wallet: 'Abstract_Wallet') -> Dict[str, Dict]:
        db = getattr(wallet, "db", None)
        raw = db.get(RELIABILITY_DB_KEY, {}) if db is not None else {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _save_reliability(self, wallet: 'Abstract_Wallet', data: Dict[str, Dict]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        db.put(RELIABILITY_DB_KEY, data)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist provider reliability: {e!r}")

    def _provider_penalty(self, wallet: 'Abstract_Wallet', npub: str,
                          stats: Optional[Dict], params: Dict[str, float],
                          now: float) -> float:
        """Current decayed ranking penalty (percentage points) for one provider."""
        if not npub or not params["enabled"] or not stats:
            return 0.0
        faults = int(stats.get("consecutive_faults", 0))
        if faults <= 0:
            return 0.0
        last_fault_ts = float(stats.get("last_fault_ts", 0.0) or 0.0)
        rel = ProviderReliability(
            consecutive_faults=faults,
            age_since_last_fault_sec=max(0.0, now - last_fault_ts),
        )
        return reliability_penalty_pct(
            rel, base_pct=params["base_pct"], halflife_sec=params["halflife_sec"],
            cap_pct=params["cap_pct"])

    def _apply_reliability_penalties(self, wallet: 'Abstract_Wallet',
                                     offers: List[ProviderOffer]) -> List[ProviderOffer]:
        if not offers:
            return offers
        params = self._reliability_params()
        if not params["enabled"]:
            return offers
        import dataclasses
        data = self._load_reliability(wallet)
        now = time.time()
        out: List[ProviderOffer] = []
        for o in offers:
            penalty = self._provider_penalty(wallet, o.npub, data.get(o.npub), params, now)
            out.append(dataclasses.replace(o, reliability_penalty_pct=penalty))
        return out

    def _record_provider_fault(self, wallet: 'Abstract_Wallet', npub: str, reason: str,
                               *, soft: bool = False) -> None:
        """Record a provider reliability fault.

        A normal (hard) fault escalates: it increments ``consecutive_faults``, so
        the decaying penalty doubles with each one -- the right response to a
        provider we can pin real misbehaviour on (e.g. it went unreachable).

        A *soft* fault is for an ambiguous signal we cannot cleanly attribute --
        chiefly a swap-creation ``SwapServerError``, which the server masks as a
        generic "Internal Server Error" and is most often just transient capacity
        (frequently exhausted by our own concurrent swap; see the capacity
        budgeting in the engine). It is recorded for visibility (fault_count, the
        Faults log) but must NOT escalate: it only floors the penalty at one
        decaying level, so a one-off barely registers and even a run of them tops
        out at the base penalty rather than compounding a healthy provider into a
        ban-adjacent score."""
        if not npub:
            return  # single-provider / URL mode has no per-provider identity
        data = self._load_reliability(wallet)
        s = data.get(npub, {})
        if soft:
            # Floor at one decaying level; never escalate on repeats.
            s["consecutive_faults"] = max(int(s.get("consecutive_faults", 0)), 1)
        else:
            s["consecutive_faults"] = int(s.get("consecutive_faults", 0)) + 1
        s["fault_count"] = int(s.get("fault_count", 0)) + 1
        s["last_fault_ts"] = time.time()
        s["last_reason"] = reason
        data[npub] = s
        self._save_reliability(wallet, data)
        self.logger.info(
            f"provider {npub[:12]}… {'soft ' if soft else ''}fault "
            f"#{s['consecutive_faults']} (total {s['fault_count']}): {reason}")
        self._log_fault(wallet, kind="provider", ident=npub, reason=reason,
                        hard=False, soft=soft)
        self.on_log_changed(wallet)

    def _record_provider_success(self, wallet: 'Abstract_Wallet', npub: str) -> None:
        if not npub:
            return
        data = self._load_reliability(wallet)
        s = data.get(npub, {})
        had_faults = int(s.get("consecutive_faults", 0))
        s["consecutive_faults"] = 0  # a success clears the penalty at its source
        s["success_count"] = int(s.get("success_count", 0)) + 1
        s["last_success_ts"] = time.time()
        data[npub] = s
        self._save_reliability(wallet, data)
        if had_faults:
            self.logger.info(f"provider {npub[:12]}… recovered after {had_faults} fault(s)")
        self.on_log_changed(wallet)

    def clear_provider_reliability(self, wallet: 'Abstract_Wallet',
                                   npub: Optional[str] = None) -> None:
        """Manual override: wipe one provider's history (or all). Used by the
        Providers tab's reset control."""
        data = self._load_reliability(wallet)
        if npub is None:
            data = {}
        else:
            data.pop(npub, None)
        self._save_reliability(wallet, data)
        self.on_log_changed(wallet)

    def provider_reliability_rows(self, wallet: 'Abstract_Wallet') -> Dict[str, Dict]:
        """Per-provider reliability, decorated with the current decayed penalty,
        for the Providers settings tab. Keyed by npub."""
        params = self._reliability_params()
        data = self._load_reliability(wallet)
        now = time.time()
        rows: Dict[str, Dict] = {}
        for npub, stats in data.items():
            rows[npub] = {
                "consecutive_faults": int(stats.get("consecutive_faults", 0)),
                "fault_count": int(stats.get("fault_count", 0)),
                "success_count": int(stats.get("success_count", 0)),
                "last_fault_ts": float(stats.get("last_fault_ts", 0.0) or 0.0),
                "last_success_ts": float(stats.get("last_success_ts", 0.0) or 0.0),
                "last_reason": stats.get("last_reason"),
                "penalty_pct": self._provider_penalty(wallet, npub, stats, params, now),
            }
        return rows

    # --- channel-peer reliability ----------------------------------------
    # Mirrors the provider reliability store above, but keyed by Lightning node id
    # (hex) and applied to the channel-partner try-order. It reuses the provider
    # penalty tuning (base / cap / half-life) and adds an auto-ban threshold on
    # *hard* faults (force-close / repeated open failure).
    def _peer_reliability_params(self) -> Dict[str, float]:
        c = self.config
        base = self._reliability_params()  # reuse base/cap/halflife tuning
        return {
            "enabled": bool(getattr(c, "INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED", True)),
            "base_pct": base["base_pct"],
            "cap_pct": base["cap_pct"],
            "halflife_sec": base["halflife_sec"],
            "autoban_faults": int(getattr(c, "INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS", 3)),
        }

    def _load_peer_reliability(self, wallet: 'Abstract_Wallet') -> Dict[str, Dict]:
        db = getattr(wallet, "db", None)
        raw = db.get(PEER_RELIABILITY_DB_KEY, {}) if db is not None else {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _save_peer_reliability(self, wallet: 'Abstract_Wallet', data: Dict[str, Dict]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        db.put(PEER_RELIABILITY_DB_KEY, data)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist peer reliability: {e!r}")

    def _peer_penalty(self, node_id: str, stats: Optional[Dict],
                      params: Dict[str, float], now: float) -> float:
        """Current decayed ranking penalty (percentage points) for one peer."""
        if not node_id or not params["enabled"] or not stats:
            return 0.0
        faults = int(stats.get("consecutive_faults", 0))
        if faults <= 0:
            return 0.0
        last_fault_ts = float(stats.get("last_fault_ts", 0.0) or 0.0)
        return decayed_penalty_pct(
            faults, max(0.0, now - last_fault_ts),
            base_pct=params["base_pct"], halflife_sec=params["halflife_sec"],
            cap_pct=params["cap_pct"])

    def _record_peer_fault(self, wallet: 'Abstract_Wallet', node_id: str, reason: str,
                           *, hard: bool = False, rate_key: Optional[str] = None,
                           rate_limit_sec: float = 0.0) -> bool:
        """Record a channel-peer fault. ``hard`` faults (force-close / channel
        closed by peer / open failure / peer offline) bump the auto-ban tally;
        soft faults only feed the decaying penalty.

        ``rate_key`` + ``rate_limit_sec`` rate-limit a *recurring* condition (e.g.
        a peer that stays offline) so it faults at most once per window: if the
        same ``rate_key`` was stamped within ``rate_limit_sec``, the call is a
        no-op and returns False. Returns True if a fault was recorded.
        """
        node_id = normalize_node_id(node_id)
        if not node_id:
            return False
        now = time.time()
        data = self._load_peer_reliability(wallet)
        s = data.get(node_id, {})
        if rate_key and rate_limit_sec > 0:
            last = float(s.get(rate_key, 0.0) or 0.0)
            if now - last < rate_limit_sec:
                return False  # within the rate-limit window; don't re-count
            s[rate_key] = now
        s["consecutive_faults"] = int(s.get("consecutive_faults", 0)) + 1
        s["fault_count"] = int(s.get("fault_count", 0)) + 1
        if hard:
            s["hard_fault_count"] = int(s.get("hard_fault_count", 0)) + 1
        s["last_fault_ts"] = now
        s["last_reason"] = reason
        data[node_id] = s
        self._save_peer_reliability(wallet, data)
        self.logger.info(
            f"peer {node_id[:12]}… {'hard ' if hard else ''}fault "
            f"#{s['consecutive_faults']} (hard {int(s.get('hard_fault_count', 0))}): {reason}")
        self._log_fault(wallet, kind="peer", ident=node_id, reason=reason, hard=hard)
        self._maybe_auto_ban_peer(wallet, node_id, s)
        self.on_log_changed(wallet)
        return True

    def _record_peer_success(self, wallet: 'Abstract_Wallet', node_id: str) -> None:
        node_id = normalize_node_id(node_id)
        if not node_id:
            return
        data = self._load_peer_reliability(wallet)
        s = data.get(node_id, {})
        had_faults = int(s.get("consecutive_faults", 0))
        s["consecutive_faults"] = 0  # a success clears the penalty at its source
        s["success_count"] = int(s.get("success_count", 0)) + 1
        s["last_success_ts"] = time.time()
        data[node_id] = s
        self._save_peer_reliability(wallet, data)
        if had_faults:
            self.logger.info(f"peer {node_id[:12]}… recovered after {had_faults} fault(s)")
        self.on_log_changed(wallet)

    def _maybe_auto_ban_peer(self, wallet: 'Abstract_Wallet', node_id: str,
                             stats: Dict) -> None:
        """Add a peer to the banned-partners list once its hard-fault tally crosses
        the configured threshold. Operator-reversible from the Channel partners
        tab; a no-op if auto-banning is disabled or the peer is already banned."""
        params = self._peer_reliability_params()
        if not params["enabled"]:
            return
        if not should_auto_ban(int(stats.get("hard_fault_count", 0)),
                               params["autoban_faults"]):
            return
        banned = _parse_banned_partners(self.config.INBOUND_LIQUIDITY_BANNED_PARTNERS)
        if node_id in banned:
            return
        new_banned = sorted(banned | {node_id})
        self.config.INBOUND_LIQUIDITY_BANNED_PARTNERS = ", ".join(new_banned)
        self.logger.warning(
            f"auto-banned channel peer {node_id[:12]}… after "
            f"{int(stats.get('hard_fault_count', 0))} hard fault(s)")

    def clear_peer_reliability(self, wallet: 'Abstract_Wallet',
                               node_id: Optional[str] = None) -> None:
        """Manual override: wipe one peer's history (or all). Used by the Channel
        partners tab's reset control. Does not un-ban; that is a separate toggle."""
        data = self._load_peer_reliability(wallet)
        if node_id is None:
            data = {}
        else:
            data.pop(normalize_node_id(node_id), None)
        self._save_peer_reliability(wallet, data)
        self.on_log_changed(wallet)

    def _peer_penalties(self, wallet: 'Abstract_Wallet') -> Dict[str, float]:
        """node_id(hex) -> current decayed penalty, for the partner ordering."""
        params = self._peer_reliability_params()
        if not params["enabled"]:
            return {}
        data = self._load_peer_reliability(wallet)
        now = time.time()
        return {nid: self._peer_penalty(nid, stats, params, now)
                for nid, stats in data.items()}

    def peer_reliability_rows(self, wallet: 'Abstract_Wallet') -> Dict[str, Dict]:
        """Per-peer reliability, decorated with the current decayed penalty, for
        the Channel partners settings tab. Keyed by node_id (hex)."""
        params = self._peer_reliability_params()
        data = self._load_peer_reliability(wallet)
        now = time.time()
        rows: Dict[str, Dict] = {}
        for nid, stats in data.items():
            rows[nid] = {
                "consecutive_faults": int(stats.get("consecutive_faults", 0)),
                "fault_count": int(stats.get("fault_count", 0)),
                "hard_fault_count": int(stats.get("hard_fault_count", 0)),
                "success_count": int(stats.get("success_count", 0)),
                "last_fault_ts": float(stats.get("last_fault_ts", 0.0) or 0.0),
                "last_success_ts": float(stats.get("last_success_ts", 0.0) or 0.0),
                "last_reason": stats.get("last_reason"),
                "penalty_pct": self._peer_penalty(nid, stats, params, now),
            }
        return rows

    # --- stuck-swap tracking ---------------------------------------------
    def _load_pending_swaps(self, wallet: 'Abstract_Wallet') -> Dict[str, Dict]:
        db = getattr(wallet, "db", None)
        raw = db.get(PENDING_SWAPS_DB_KEY, {}) if db is not None else {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _save_pending_swaps(self, wallet: 'Abstract_Wallet', data: Dict[str, Dict]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        db.put(PENDING_SWAPS_DB_KEY, data)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist pending-swap tracking: {e!r}")

    def _track_pending_swap(self, wallet: 'Abstract_Wallet', payment_hash_hex: str,
                            npub: str, node_id: str = "", channel_id: str = "",
                            fee_basis_sat: int = 0) -> None:
        """Remember a reverse swap we initiated, so one that is accepted but never
        funds can be charged to the right party: the ``npub`` provider if it never
        funded, or the channel ``node_id`` peer if our Lightning payment failed.
        ``fee_basis_sat`` (the expected net on-chain amount) is stashed so the dev
        fee can be accrued if and only if the swap later completes."""
        if not payment_hash_hex:
            return
        data = self._load_pending_swaps(wallet)
        data[payment_hash_hex] = {
            "npub": npub, "started_ts": time.time(),
            "node_id": node_id, "channel_id": channel_id,
            "fee_basis_sat": int(fee_basis_sat)}
        self._save_pending_swaps(wallet, data)

    def _ln_payment_failed(self, wallet: 'Abstract_Wallet', payment_hash_hex: str) -> bool:
        """Whether our outgoing Lightning payment for this swap has definitively
        failed (every route attempt exhausted) -- the signal that the fault is our
        channel peer's, not the swap provider's."""
        from electrum.lnutil import Direction
        from electrum.invoices import PR_FAILED
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return False
        try:
            status = lnworker.get_payment_status(
                bytes.fromhex(payment_hash_hex), direction=Direction.SENT)
        except Exception:
            return False
        return status == PR_FAILED

    def _reconcile_pending_swaps(self, wallet: 'Abstract_Wallet') -> None:
        """Resolve tracked reverse swaps against the swap manager's state:
        funded/redeemed => the provider delivered (success); no funding within
        the stuck-timeout => the provider left it stuck (fault). Idempotent: each
        tracked swap is recorded once and then dropped."""
        data = self._load_pending_swaps(wallet)
        if not data:
            return
        sm = wallet.lnworker.swap_manager
        params = self._reliability_params()
        now = time.time()
        changed = False
        for ph_hex in list(data.keys()):
            info = data[ph_hex]
            npub = info.get("npub", "")
            node_id = info.get("node_id", "")
            started_ts = float(info.get("started_ts", 0.0) or 0.0)
            swap = None
            try:
                swap = sm.get_swap(bytes.fromhex(ph_hex))
            except Exception:
                swap = None
            if swap is not None and (getattr(swap, "is_redeemed", False)
                                     or getattr(swap, "funding_txid", None)):
                # Provider created the on-chain output (and we may already have
                # claimed it) -- it honoured the swap. This is the swap's confirmed
                # completion, so accrue its dev fee now (once -- the record is then
                # dropped).
                self._record_provider_success(wallet, npub)
                self._accrue_dev_fee(
                    wallet, int(info.get("fee_basis_sat", 0) or 0),
                    source=f"swap {ph_hex[:10]}…")
                del data[ph_hex]
                changed = True
            elif self._ln_payment_failed(wallet, ph_hex):
                # Our Lightning payment failed before any funding: the channel
                # peer couldn't route it. Charge the *peer*, not the provider --
                # the provider never got the chance to (mis)behave. Resolves at
                # once, without waiting out the provider stuck timeout.
                self._record_peer_fault(
                    wallet, node_id, "reverse-swap Lightning payment failed", hard=False)
                del data[ph_hex]
                changed = True
            elif now - started_ts > params["stuck_timeout_sec"]:
                self._record_provider_fault(
                    wallet, npub,
                    f"stuck: no on-chain funding within "
                    f"{int(params['stuck_timeout_sec'] // 60)} min")
                del data[ph_hex]
                changed = True
        if changed:
            self._save_pending_swaps(wallet, data)

    # --- channel-peer health watchdog ------------------------------------
    @staticmethod
    def _open_age_exceeded(chan, timeout_sec: float, now: float) -> bool:
        """True if a not-yet-open channel has been opening longer than
        ``timeout_sec`` (by its persisted ``init_timestamp``)."""
        try:
            init_ts = float(chan.storage.get("init_timestamp", 0) or 0)
        except Exception:
            return False
        return init_ts > 0 and (now - init_ts) > timeout_sec

    def _scan_channel_health(self, wallet: 'Abstract_Wallet') -> None:
        """Watchdog over the wallet's channels, run each tick before snapshotting.
        Each is charged as a *hard* peer fault (feeding the auto-ban tally and the
        partner ordering):

          * a channel the **peer closed** -- cooperative or force -- the moment we
            observe the transition (rising edge), excluding closes *we* triggered;
          * a channel whose **peer is offline** (OPEN but not connected), at most
            once per 24h per peer (rate-limited so a long outage isn't double-counted);
          * a channel **wedged opening** past the stuck-open timeout, which is also
            force-closed when auto-remediation is on so its funds are freed.

        Channels already closed before we started managing (present on the very
        first scan) are seeded, not retroactively blamed on the peer.
        """
        from electrum.lnchannel import ChannelState
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return
        auto_remediate = bool(getattr(
            self.config, "INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN", True))
        timeout_sec = max(1, int(getattr(
            self.config, "INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN", 60))) * 60.0
        pending_states = (
            ChannelState.PREOPENING, ChannelState.OPENING, ChannelState.FUNDED)
        closing_states = (
            ChannelState.SHUTDOWN, ChannelState.CLOSING, ChannelState.FORCE_CLOSING,
            ChannelState.REQUESTED_FCLOSE, ChannelState.CLOSED)
        force_states = (ChannelState.FORCE_CLOSING, ChannelState.REQUESTED_FCLOSE)
        now = time.time()
        remediating = self._remediating_opens.setdefault(wallet, set())
        local_closes = self._local_closes.setdefault(wallet, set())
        wedged_faulted = self._wedged_faulted.setdefault(wallet, set())
        close_capped_logged = self._close_capped_logged.setdefault(wallet, set())
        first_scan = wallet not in self._known_chan_states
        known = self._known_chan_states.setdefault(wallet, {})
        try:
            channels = list(lnworker.channels.values())
        except Exception:
            return
        live_ids = set()
        for chan in channels:
            cid = chan.channel_id.hex()
            live_ids.add(cid)
            try:
                node_id = chan.node_id.hex()
            except Exception:
                node_id = ""
            state = chan.get_state()
            # Closing/closed? A remote force-close leaves us <= OPEN but with an
            # unconfirmed closing txid (mirrors get_state_for_GUI).
            remote_fc = (state <= ChannelState.OPEN
                         and getattr(chan, "unconfirmed_closing_txid", None))
            is_closing = state in closing_states or bool(remote_fc)
            was_closing = known.get(cid, False)
            # Peer closed our channel (rising edge). Skip closes *we* initiated --
            # the user's own close (GUI/CLI, tracked in _local_closes) or our
            # stuck-open remediation -- and, on the first scan, channels already
            # closed before we started watching (we can't attribute their timing).
            if (is_closing and not was_closing and not first_scan
                    and cid not in remediating and cid not in local_closes):
                is_force = state in force_states or bool(remote_fc)
                self._record_peer_fault(
                    wallet, node_id,
                    "channel force-closed by peer" if is_force else "channel closed by peer",
                    hard=True)
            known[cid] = is_closing
            # Peer offline: an OPEN channel whose peer is not connected. Hard fault,
            # rate-limited to once per 24h per peer so a long outage counts once.
            # Startup/shutdown-guarded: _observe_peer returns None while the peer
            # may simply not have been dialed yet (startup grace) or the network
            # is down (shutdown), so a healthy peer is never faulted for our own
            # transitional state; only a genuine offline (False) faults.
            if state == ChannelState.OPEN:
                if self._observe_peer(wallet, chan, now) is False:
                    self._record_peer_fault(
                        wallet, node_id, "peer offline", hard=True,
                        rate_key="last_offline_fault_ts", rate_limit_sec=86400.0)
            # Stuck-open watchdog.
            if (state in pending_states and cid not in remediating
                    and self._open_age_exceeded(chan, timeout_sec, now)):
                # Fault the peer exactly once for the wedged open (decoupled from
                # remediation, which may be deferred by the close ceiling below).
                if cid not in wedged_faulted:
                    wedged_faulted.add(cid)
                    self._record_peer_fault(
                        wallet, node_id,
                        f"channel open wedged > {int(timeout_sec // 60)} min", hard=True)
                # Remediate by force-closing -- but only if the rolling-24h close
                # ceiling still allows it. When the ceiling is reached we defer
                # (the snapshot already stops freezing on a wedged open), and a
                # later tick retries once the window rolls over.
                if auto_remediate:
                    if not self._within_close_cap(wallet, now):
                        if cid not in close_capped_logged:
                            close_capped_logged.add(cid)
                            self.logger.warning(
                                f"daily close ceiling ({self._max_closes_per_day()}/24h) "
                                f"reached; deferring force-close of wedged open "
                                f"{cid[:12]}… to peer {node_id[:12]}…")
                    else:
                        close_capped_logged.discard(cid)
                        remediating.add(cid)
                        try:
                            lnworker.schedule_force_closing(chan.channel_id)
                            self._record_action_event(wallet, "close")
                            self.logger.warning(
                                f"force-closing wedged channel open {cid[:12]}… "
                                f"to peer {node_id[:12]}…")
                            self._log_action(
                                wallet, kind="close", amount_sat=None,
                                source=self._abbrev(node_id) or node_id or None,
                                dest=None, reason="force-closed wedged channel open",
                                detail=f"channel {cid[:12]}… wedged "
                                       f"> {int(timeout_sec // 60)} min", state=None)
                        except Exception as e:
                            remediating.discard(cid)  # scheduling failed; allow retry
                            self.logger.warning(
                                f"could not force-close wedged open {cid[:12]}…: {e!r}")
        # Forget bookkeeping for channels that are gone (redeemed/removed), so a
        # later reused channel_id starts fresh.
        remediating &= live_ids
        local_closes &= live_ids
        wedged_faulted &= live_ids
        close_capped_logged &= live_ids
        for gone in [c for c in known if c not in live_ids]:
            del known[gone]

    # --- offline-channel auto-close --------------------------------------
    # Applies ONLY to channels the plugin opened (see PLUGIN_OPENED_CHANNELS_DB_KEY).
    # Each tick we sample the peer's reachability into a rolling uptime accumulator;
    # once the peer's uptime over the window drops below the floor we commit to
    # closing the channel: a cooperative close is attempted whenever the peer is
    # reachable, and if the channel still hasn't closed after the force-close
    # horizon it is force-closed (offline-safe). The commit is metric-driven, so a
    # peer that recovers above the floor before we force-close cancels the close.
    def _load_json_dict(self, wallet: 'Abstract_Wallet', key: str) -> Dict:
        db = getattr(wallet, "db", None)
        raw = db.get(key, {}) if db is not None else {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _save_json(self, wallet: 'Abstract_Wallet', key: str, data) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        db.put(key, data)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist {key}: {e!r}")

    def _plugin_opened_channels(self, wallet: 'Abstract_Wallet') -> set:
        db = getattr(wallet, "db", None)
        raw = db.get(PLUGIN_OPENED_CHANNELS_DB_KEY, []) if db is not None else []
        return set(raw) if isinstance(raw, list) else set()

    def _tag_plugin_opened_channel(self, wallet: 'Abstract_Wallet', channel_id_hex: str) -> None:
        if not channel_id_hex:
            return
        ids = self._plugin_opened_channels(wallet)
        if channel_id_hex in ids:
            return
        ids.add(channel_id_hex)
        self._save_json(wallet, PLUGIN_OPENED_CHANNELS_DB_KEY, sorted(ids))

    def _offline_autoclose_params(self) -> Dict[str, float]:
        """Offline auto-close tuning, read fresh so edits take effect at once.
        Days are converted to seconds; getattr defaults keep partial (unit-test)
        configs sane."""
        c = self.config
        window_days = max(0.0, float(getattr(
            c, "INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS",
            DEFAULT_OFFLINE_UPTIME_WINDOW_DAYS)))
        force_days = max(0.0, float(getattr(
            c, "INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS",
            DEFAULT_OFFLINE_FORCE_CLOSE_DAYS)))
        return {
            "enabled": bool(getattr(
                c, "INBOUND_LIQUIDITY_OFFLINE_AUTOCLOSE_ENABLED",
                DEFAULT_OFFLINE_AUTOCLOSE_ENABLED)),
            # A zero/degenerate window would make every ratio "the whole window",
            # committing instantly; clamp to a tiny positive floor.
            "window_sec": max(1.0, window_days * 86400.0),
            "min_uptime_pct": max(0.0, float(getattr(
                c, "INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT",
                DEFAULT_OFFLINE_MIN_UPTIME_PCT))),
            "force_close_sec": force_days * 86400.0,
        }

    def _observe_peer(self, wallet: 'Abstract_Wallet', chan, now: float) -> Optional[bool]:
        """One startup-race-guarded reading of a channel peer's reachability.

        Returns ``True`` (online), ``False`` (genuinely offline) or ``None`` (not
        observed -- caller records nothing). Reads ``chan.is_active()`` and folds
        in whether the network is connected and whether we have already seen this
        peer online this session, so a not-yet-connected peer during the startup
        grace (or during a graceful shutdown, when the network is down) is never
        mistaken for a real outage. A peer read active is remembered so its next
        inactive reading counts as a real offline."""
        try:
            is_active = bool(chan.is_active())
        except Exception:
            # Can't tell -> don't manufacture an offline observation.
            return None
        try:
            node_id = chan.node_id.hex()
        except Exception:
            node_id = ""
        network = getattr(wallet, "network", None)
        try:
            connected = bool(network is not None and network.is_connected())
        except Exception:
            connected = False
        seen = self._peer_seen_online.setdefault(wallet, set())
        seen_before = node_id in seen
        started = self._started_at.get(wallet, now)
        result = classify_peer_observation(
            is_active, seen_before, connected, now - started,
            self._startup_grace_sec)
        if result is True and node_id:
            seen.add(node_id)
        return result

    def _scan_offline_autoclose(self, wallet: 'Abstract_Wallet') -> None:
        """Watchdog pass (run each tick after _scan_channel_health): sample peer
        uptime for plugin-opened channels, commit/cancel a close based on that
        uptime, then cooperatively close (peer reachable) or force-close (deadline
        passed) committed channels. All state is persisted so the "trying to close
        since T" clock survives restarts."""
        from electrum.lnchannel import ChannelState
        params = self._offline_autoclose_params()
        if not params["enabled"]:
            return
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return
        try:
            channels = {c.channel_id.hex(): c for c in lnworker.channels.values()}
        except Exception:
            return
        managed = self._plugin_opened_channels(wallet)
        uptime = self._load_json_dict(wallet, CHANNEL_UPTIME_DB_KEY)
        intents = self._load_json_dict(wallet, CLOSE_INTENT_DB_KEY)
        coop_inflight = self._coop_closing.setdefault(wallet, set())
        now = time.time()
        window_sec = params["window_sec"]
        uptime_changed = False
        intents_changed = False

        for cid in sorted(managed):
            chan = channels.get(cid)
            if chan is None:
                continue  # not loaded yet; handled by the cleanup pass below
            try:
                state = chan.get_state()
            except Exception:
                continue
            # A fully closed/redeemed channel is done; drop any close intent and
            # stop sampling it.
            if state >= ChannelState.CLOSED:
                if cid in intents:
                    intents.pop(cid, None)
                    intents_changed = True
                continue
            try:
                node_id = chan.node_id.hex()
            except Exception:
                node_id = ""
            already_closing = state >= ChannelState.SHUTDOWN

            # 1) Sample uptime while the channel is OPEN (pre-close). Once it is
            #    closing we stop sampling -- the metric only judges healthy-state
            #    availability.
            if state == ChannelState.OPEN:
                # Startup/shutdown-guarded reading: None means "not observed"
                # (peer not yet connected during the grace, or network down) --
                # record nothing so a transitional not-connected never poisons the
                # uptime ratio and force-closes a healthy channel.
                online = self._observe_peer(wallet, chan, now)
                if online is not None:
                    uptime[cid] = record_uptime_sample(
                        uptime.get(cid), now, online, window_sec=window_sec)
                    uptime_changed = True

            # 2) Commit / cancel the close based on the current uptime ratio.
            ratio = uptime_ratio(uptime.get(cid), now, window_sec)
            gone = should_commit_offline_close(
                ratio, min_uptime_pct=params["min_uptime_pct"])
            if gone and cid not in intents and not already_closing:
                pct = ratio[0] * 100.0 if ratio else 0.0
                intents[cid] = {"marked_ts": now,
                                "reason": f"peer uptime {pct:.1f}% over the last "
                                          f"{window_sec / 86400.0:.2f}d below "
                                          f"{params['min_uptime_pct']:.1f}%"}
                intents_changed = True
                self.logger.info(
                    f"committing to close plugin channel {cid[:12]}… to peer "
                    f"{node_id[:12]}…: {intents[cid]['reason']}")
            elif not gone and cid in intents and not already_closing:
                # Peer recovered above the floor before we closed -> cancel.
                intents.pop(cid, None)
                intents_changed = True
                self.logger.info(
                    f"cancelling pending close of {cid[:12]}…: peer recovered")

            # 3) Act on a committed intent that hasn't closed yet.
            intent = intents.get(cid)
            if intent is None:
                continue
            marked_ts = float(intent.get("marked_ts", now) or now)
            if deadline_reached(marked_ts, now, params["force_close_sec"]):
                # Escalate to a force-close (offline-safe, idempotent), gated by
                # the rolling-24h close ceiling shared with the wedged-open remedy.
                if state in (ChannelState.FORCE_CLOSING, ChannelState.REQUESTED_FCLOSE):
                    continue  # already force-closing
                if not self._within_close_cap(wallet, now):
                    self.logger.warning(
                        f"daily close ceiling reached; deferring force-close of "
                        f"offline channel {cid[:12]}…")
                    continue
                try:
                    lnworker.schedule_force_closing(chan.channel_id)
                    self._record_action_event(wallet, "close")
                    self.logger.warning(
                        f"force-closing offline channel {cid[:12]}… to peer "
                        f"{node_id[:12]}… after {params['force_close_sec'] / 86400.0:.2f}d")
                    self._log_action(
                        wallet, kind="close", amount_sat=None,
                        source=self._abbrev(node_id) or node_id or None, dest=None,
                        reason="auto-close: force-closed offline channel",
                        detail=f"peer never agreed within "
                               f"{params['force_close_sec'] / 86400.0:.2f}d of trying to close",
                        state=None)
                except Exception as e:
                    self.logger.warning(
                        f"could not force-close offline channel {cid[:12]}…: {e!r}")
            elif state == ChannelState.OPEN:
                # Not yet at the deadline: attempt the cheaper cooperative close
                # whenever the peer is reachable (it requires the peer online).
                try:
                    peer_online = bool(chan.is_active())
                except Exception:
                    peer_online = False
                if peer_online:
                    self._maybe_cooperative_close(wallet, chan, cid, node_id, now)

        if uptime_changed:
            self._save_json(wallet, CHANNEL_UPTIME_DB_KEY, uptime)
        # Cleanup: forget uptime/intent for channels that are gone (redeemed/
        # removed) or no longer tagged, so a reused id starts fresh.
        for store, key in ((uptime, CHANNEL_UPTIME_DB_KEY),
                           (intents, CLOSE_INTENT_DB_KEY)):
            stale = [c for c in store if c not in channels or c not in managed]
            if stale:
                for c in stale:
                    store.pop(c, None)
                self._save_json(wallet, key, store)
            elif key == CLOSE_INTENT_DB_KEY and intents_changed:
                self._save_json(wallet, key, store)
        coop_inflight &= set(channels)

    def _maybe_cooperative_close(self, wallet: 'Abstract_Wallet', chan, cid: str,
                                 node_id: str, now: float) -> None:
        """Launch a cooperative close on a committed channel whose peer is online,
        unless one is already in flight or we attempted one recently (cooldown).
        The close is async and can take minutes, so it runs as a background task;
        the force-close escalation remains the backstop if it never completes."""
        if not self._within_close_cap(wallet, now):
            return
        coop_inflight = self._coop_closing.setdefault(wallet, set())
        if cid in coop_inflight:
            return
        mono = time.monotonic()
        if mono < self._coop_close_cooldown_until.get(cid, 0.0):
            return
        self._coop_close_cooldown_until[cid] = mono + COOP_CLOSE_COOLDOWN_SEC
        coop_inflight.add(cid)
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        coro = self._do_cooperative_close(wallet, chan.channel_id, cid, node_id)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            asyncio.ensure_future(coro)

    async def _do_cooperative_close(self, wallet: 'Abstract_Wallet', chan_id: bytes,
                                    cid: str, node_id: str) -> None:
        lnworker = getattr(wallet, "lnworker", None)
        try:
            self.logger.info(
                f"attempting cooperative close of offline channel {cid[:12]}… "
                f"to peer {node_id[:12]}… (peer is currently reachable)")
            await lnworker.close_channel(chan_id)
            self._record_action_event(wallet, "close")
            self.logger.info(f"cooperatively closed channel {cid[:12]}…")
            self._log_action(
                wallet, kind="close", amount_sat=None,
                source=self._abbrev(node_id) or node_id or None, dest=None,
                reason="auto-close: cooperatively closed offline channel",
                detail=f"channel {cid[:12]}… closed with a now-reachable peer",
                state=None)
        except Exception as e:
            # Peer went away mid-close, or refused: leave the intent in place so
            # the force-close deadline still escalates it.
            self.logger.info(
                f"cooperative close of {cid[:12]}… did not complete: {e!r}")
        finally:
            self._coop_closing.get(wallet, set()).discard(cid)

    def _offers_from_transport(self, wallet: Optional['Abstract_Wallet'],
                               transport: Optional['SwapServerTransport']
                               ) -> List[ProviderOffer]:
        """Translate the transport's live nostr offers into the engine's pure
        ProviderOffer list. Empty for the HTTP/URL transport (single provider)
        or when no transport / no offers are available.

        Every field here is attacker-controlled (a provider's nostr
        announcement), so each offer is validated (see ``validate_offer``) rather
        than blindly coerced: a negative / NaN / infinite / absurd economic field
        would otherwise sail through the cost gate and win the cheapest-provider
        ranking, and one non-coercible field would raise and take out the whole
        evaluation. Each offer is handled independently so a single bad one can
        never poison discovery, and a provider that advertises a validly-signed
        identity but out-of-range terms is recorded as a (soft) reliability fault
        so a persistent offender sinks in the ranking. ``wallet`` may be ``None``
        (no fault attribution possible then; the offer is simply dropped)."""
        get_recent = getattr(transport, "get_recent_offers", None)
        if get_recent is None:
            return []
        try:
            raw_offers = list(get_recent())
        except Exception as e:
            self.logger.warning(f"could not read discovered offers: {e!r}")
            return []
        offers: List[ProviderOffer] = []
        faulted_this_pass: set = set()  # fault each bad provider at most once/pass
        for o in raw_offers:
            # ``max_forward`` (not ``max_reverse``) is the provider's cap on a
            # client reverse swap -- a client reverse swap is a *forward* swap
            # from the provider's side. Electrum core agrees:
            # SwapManager.client_max_amount_reverse_swap() reads max_forward, and
            # check_invoice_amount(is_reverse=True) validates against it. (Using
            # max_reverse let the planner request more than the provider accepts,
            # so get_recv_amount() returned None and the swap blew up with an
            # int - None TypeError.)
            npub = clean_npub(getattr(o, "server_npub", None))
            try:
                pairs = o.pairs
                offer = validate_offer(
                    npub,
                    getattr(pairs, "percentage", None),
                    getattr(pairs, "mining_fee", None),
                    getattr(pairs, "min_amount", None),
                    getattr(pairs, "max_forward", None),
                    getattr(o, "pow_bits", 0),
                )
            except Exception:
                offer = None
            if offer is not None:
                offers.append(offer)
            elif npub and wallet is not None and npub not in faulted_this_pass:
                # Identifiable provider, but its advertised terms were malformed
                # or out of range: a soft fault (de-prioritise, never exclude).
                # At most once per pass, so a provider spamming many bad offers
                # under one identity can't amplify into a burst of db writes.
                faulted_this_pass.add(npub)
                self._record_provider_fault(
                    wallet, npub, "advertised malformed/out-of-range offer", soft=True)
            elif not npub:
                self.logger.info("dropping offer with unusable provider identity")
        return offers

    @staticmethod
    def _chan_unsettled_is_swap(chan, sm) -> bool:
        """True if any of ``chan``'s currently-unsettled HTLCs belongs to a
        submarine swap known to the swap manager (matched by payment hash) -- i.e.
        the in-flight leg of a reverse swap this wallet issued, rather than a
        third-party payment stuck on the channel. Best-effort: any lookup failure
        conservatively returns False (fall back to the "possible stuck payment"
        treatment)."""
        try:
            from electrum.lnutil import LOCAL, REMOTE
            htlcs = list(chan.hm.htlcs(LOCAL)) + list(chan.hm.htlcs(REMOTE))
        except Exception:
            return False
        seen = set()
        for _direction, htlc in htlcs:
            ph = getattr(htlc, "payment_hash", None)
            if ph is None or ph in seen:
                continue
            seen.add(ph)
            try:
                if sm.get_swap(ph) is not None:
                    return True
            except Exception:
                continue
        return False

    def _stuck_swap_timeout_sec(self) -> float:
        """The stuck-reverse-swap freeze-escape timeout (seconds). A pending swap
        older than this stops counting toward the in-flight freeze."""
        return max(1, int(getattr(
            self.config, "INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN", 180))) * 60.0

    def _count_freezing_swaps(self, wallet: 'Abstract_Wallet', sm, now: float) -> int:
        """How many in-flight reverse swaps should still FREEZE automation.

        A reverse swap whose funding is broadcast but not yet swept
        (``sm.get_pending_swaps()``) freezes the engine until it settles -- we must
        stay online for it and shouldn't stack more actions on top. But a swap
        wedged far past the stuck-swap timeout stops counting, so one stuck swap
        can't block every open and swap forever (mirrors the stuck channel-open
        escape). The provider is still faulted separately by
        ``_reconcile_pending_swaps``; this governs only the freeze. First-seen
        times are persisted, so a swap wedged across a restart still ages out."""
        try:
            pending = sm.get_pending_swaps()
        except Exception:
            return 0
        pending_set: set = set()
        for swap in pending:
            try:
                ph = swap.payment_hash
            except Exception:
                ph = None
            if ph:
                pending_set.add(ph.hex())
        first_seen = self._load_json_dict(wallet, SWAP_FIRST_SEEN_DB_KEY)
        changed = False
        for ph_hex in pending_set:
            if ph_hex not in first_seen:
                first_seen[ph_hex] = now
                changed = True
        # Prune first-seen entries (and their once-logged marks) for swaps that are
        # no longer pending -- settled, swept, or refunded -- so a reused id is fresh.
        for ph_hex in [k for k in first_seen if k not in pending_set]:
            del first_seen[ph_hex]
            changed = True
        if changed:
            self._save_json(wallet, SWAP_FIRST_SEEN_DB_KEY, first_seen)
        # In-memory "already logged the escape" set, purely to avoid re-logging a
        # stuck swap every tick. Tolerate a partially-built plugin (tests that
        # bypass __init__): fall back to a throwaway set rather than crash the
        # snapshot on it.
        logged_map = getattr(self, "_swap_freeze_escaped_logged", None)
        escaped_logged = logged_map.setdefault(wallet, set()) if logged_map is not None else set()
        escaped_logged &= pending_set
        timeout = self._stuck_swap_timeout_sec()
        count = 0
        for ph_hex in pending_set:
            ts = float(first_seen.get(ph_hex, now) or now)
            if now - ts <= timeout:
                count += 1
            elif ph_hex not in escaped_logged:
                escaped_logged.add(ph_hex)
                self.logger.warning(
                    f"reverse swap {ph_hex[:10]}… still unswept after "
                    f"{int(timeout // 60)} min; no longer freezing automation")
        return count

    def build_snapshot(self, wallet: 'Abstract_Wallet',
                       transport: Optional['SwapServerTransport'] = None) -> LiquiditySnapshot:
        from electrum.lnutil import LOCAL, REMOTE
        from electrum.lnchannel import ChannelState
        lnworker = wallet.lnworker
        sm = lnworker.swap_manager
        channels: List[ChannelSnapshot] = []
        # States in which a channel's funding is broadcast/negotiated but the
        # channel is not yet usable (OPEN). Any such channel means an open is
        # still "in flight" and the engine should freeze.
        pending_states = (
            ChannelState.PREOPENING, ChannelState.OPENING, ChannelState.FUNDED)
        # A channel wedged in those states past the stuck-open timeout is treated
        # as abandoned by an unresponsive peer and NO LONGER freezes automation
        # (the watchdog records the fault / force-closes it separately). This is
        # what stops one flaky peer from wedging the plugin forever.
        stuck_open_sec = max(1, int(getattr(
            self.config, "INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN", 60))) * 60.0
        now = time.time()
        pending_channel_count = 0
        for chan in lnworker.channels.values():
            capacity = chan.get_capacity() or 0
            if chan.get_state() in pending_states and not self._open_age_exceeded(
                    chan, stuck_open_sec, now):
                pending_channel_count += 1
            try:
                has_unsettled = bool(chan.has_unsettled_htlcs())
            except Exception:
                has_unsettled = False
            # Distinguish an unsettled HTLC that is the in-flight leg of a reverse
            # swap WE issued from a genuine third-party stuck payment: if any of the
            # channel's pending HTLCs matches a swap in the swap manager's registry
            # (by payment hash), it is our own swap still settling. This is what
            # stops the engine from mislabelling our just-issued swap as a "possible
            # stuck payment" while its on-chain funding confirms.
            unsettled_is_swap = has_unsettled and self._chan_unsettled_is_swap(chan, sm)
            channels.append(ChannelSnapshot(
                channel_id=chan.channel_id.hex(),
                short_id=str(chan.short_channel_id) if chan.short_channel_id else chan.channel_id.hex()[:8],
                capacity_sat=int(capacity),
                local_sat=chan.balance(LOCAL) // 1000,
                remote_sat=chan.balance(REMOTE) // 1000,
                spendable_local_sat=chan.available_to_spend(LOCAL) // 1000,
                is_active=chan.is_active(),
                has_unsettled_htlcs=has_unsettled,
                unsettled_is_swap=unsettled_is_swap,
            ))
        # Reverse swaps with a broadcast-but-not-yet-swept funding tx; Electrum's
        # own "must stay online for these" set. Self-clears on settle/refund, so
        # a healthy swap won't freeze the plugin. A swap wedged past the stuck-swap
        # timeout is excluded here (freeze escape) so one stuck swap can't block
        # automation forever -- see _count_freezing_swaps.
        inflight_swap_count = self._count_freezing_swaps(wallet, sm, now)
        # Each provider-economics field is read defensively: the swap manager
        # populates them from the provider's advertised terms, so a not-yet-ready
        # or misbehaving provider can leave one unset or make its accessor raise.
        # Degrade that single field to None (the engine already treats None as
        # "unknown" and just declines the swap) rather than letting one bad field
        # abort the whole snapshot -- which would also drop the channel-open
        # decision this snapshot carries.
        try:
            percentage = float(sm.percentage) if sm.percentage is not None else None
        except Exception:
            percentage = None
        # Amount-independent reverse-swap costs, for the effective-cost gate.
        try:
            mining_fee = int(sm.mining_fee) if sm.mining_fee is not None else None
        except Exception:
            mining_fee = None
        try:
            claim_fee = int(sm.get_fee_for_txbatcher())
        except Exception:
            claim_fee = None
        try:
            provider_max_reverse = sm.get_provider_max_forward_amount() or None
        except Exception:
            provider_max_reverse = None
        try:
            provider_min_amount = sm.get_min_amount() or None
        except Exception:
            provider_min_amount = None
        offers = self._offers_from_transport(wallet, transport)
        # Fold each provider's reliability penalty (from persisted fault history,
        # decayed to now) onto its offer, so the pure selector ranks flaky
        # providers behind reliable ones. The penalty never changes the real cost
        # or the cost gate -- only the ranking (soft de-prioritisation).
        offers = self._apply_reliability_penalties(wallet, offers)
        # Keep the latest discovered set for the Providers settings tab, but only
        # when we actually queried a transport -- the no-transport pre-pass must
        # not clobber a previously discovered list with an empty one.
        if transport is not None:
            self._last_offers[wallet] = offers
        return LiquiditySnapshot(
            onchain_spendable_sat=int(wallet.get_spendable_balance_sat()),
            channels=tuple(channels),
            swap_percentage_fee=percentage,
            # Cap for a client reverse swap is the provider's max_forward (see the
            # note in _offers_from_transport); the single-provider path must use
            # the same field or it would overshoot exactly like the offers path.
            provider_max_reverse_sat=provider_max_reverse,
            provider_min_amount_sat=provider_min_amount,
            swap_mining_fee_sat=mining_fee,
            swap_claim_fee_sat=claim_fee,
            provider_offers=tuple(offers),
            pending_channel_count=pending_channel_count,
            inflight_swap_count=inflight_swap_count,
            opens_last_24h=self._count_actions_last_24h(wallet, "open", now),
        )

    # --- event handling ---------------------------------------------------
    @ignore_exceptions
    @log_exceptions
    async def _on_wallet_event(self, *args) -> None:
        # Trigger events have inconsistent argument shapes, so rather than try to
        # extract "the wallet" from each, just re-evaluate every wallet we manage.
        # Bursts of events (sync settling, a swap's own updates) are coalesced
        # into a single evaluation via a short debounce.
        for wallet in list(self.wallets):
            if self._eval_pending.get(wallet):
                continue
            self._eval_pending[wallet] = True
            asyncio.ensure_future(self._debounced_evaluate(wallet))

    async def _debounced_evaluate(self, wallet: 'Abstract_Wallet') -> None:
        await asyncio.sleep(1.0)  # collect a burst of events before acting
        self._eval_pending[wallet] = False
        await self._evaluate(wallet)

    async def _evaluate(self, wallet: 'Abstract_Wallet') -> None:
        lock = self.wallets.get(wallet)
        if lock is None:
            return
        if lock.locked():
            return  # an evaluation / action is already in flight; the next event re-checks
        async with lock:
            try:
                # Re-assert the channel-funding floor every tick so the user's
                # min_onchain value always wins, even if some code path reset the
                # constant. Done before the automation gate so a lowered floor also
                # applies to manual opens while automation is paused.
                self._enforce_min_funding_floor()
                config = self.read_config()
                if not config.automation_enabled:
                    return
                # Startup/shutdown race guard: until the wallet has settled (a
                # live server connection AND the startup grace elapsed), defer ALL
                # automation. The Lightning layer connects to peers asynchronously
                # after load, so acting now risks faulting a healthy-but-not-yet-
                # connected peer or opening/swapping on incomplete state. A later
                # tick (events, or the scheduled post-grace evaluation) re-checks.
                if not self._wallet_ready(wallet):
                    self.logger.debug(
                        f"{wallet.basename()} not settled yet; deferring evaluation")
                    return
                # Resolve any reverse swaps we were watching (funded -> success,
                # never funded -> stuck fault) before snapshotting, so the
                # penalties folded into this tick's offers are up to date.
                self._reconcile_pending_swaps(wallet)
                # Pay out any dev fee that has accrued past the batch threshold
                # (runs as a guarded background task; never blocks this tick).
                self._maybe_pay_dev_fee(wallet)
                # Watchdog: fault/force-close force-closed or wedged-open channels
                # so a bad peer can't wedge automation and is de-prioritised next
                # time we pick a partner.
                self._scan_channel_health(wallet)
                # Watchdog: sample peer uptime and cooperatively-/force-close
                # plugin-opened channels whose peer has gone offline for good.
                self._scan_offline_autoclose(wallet)
                # First pass with no provider data: cheap, and tells us whether a
                # swap is even on the table. Only if one is do we pay to open a
                # nostr session (connect relays + discover providers); channel
                # opens and idle/frozen ticks need no provider at all.
                base = self.build_snapshot(wallet, None)
                if self._swap_may_be_needed(base, config):
                    async with self._swap_session(wallet) as transport:
                        snapshot = self.build_snapshot(wallet, transport)
                        await self._run_decision(wallet, snapshot, config, transport)
                else:
                    await self._run_decision(wallet, base, config, None)
            except Exception as e:
                # Never let an evaluation error escape (it would surface as an
                # unhandled asyncio task exception); just log and wait for the
                # next trigger.
                self.logger.error(f"liquidity evaluation failed: {e!r}", exc_info=e)
                self._diag_event(wallet, category="error", kind="evaluation",
                                 reason="evaluation failed",
                                 detail=f"{type(e).__name__}: {e}")

    async def _run_decision(self, wallet: 'Abstract_Wallet', snapshot: LiquiditySnapshot,
                            config: LiquidityConfig,
                            transport: Optional['SwapServerTransport']) -> None:
        """Run the rules engine on a snapshot, log declines, and execute actions."""
        result = evaluate(snapshot, config)
        self.logger.info(
            f"{wallet.basename()}: onchain={snapshot.onchain_spendable_sat} "
            f"channels={len(snapshot.channels)} pending={snapshot.pending_channel_count} "
            f"inflight_swaps={snapshot.inflight_swap_count} "
            f"providers={len(snapshot.provider_offers)} "
            f"-> {len(result.actions)} action(s), {len(result.declines)} decline(s)"
            + (" [FROZEN]" if result.frozen else ""))
        state = self._state_dict(snapshot, config)
        declines = list(result.declines)
        # Pre-resolve channel-open candidates: an open the engine approved can
        # still be blocked here if no eligible partner remains -- all banned,
        # none suggested, or (with the one-channel-per-peer guard on) we already
        # hold a channel with every reachable peer. Surface that as a first-class
        # decline through the normal dedup path rather than a silent skip inside
        # _execute, and carry the resolved candidates into execution so partners
        # are resolved once, not twice.
        executable: List[Tuple[Action, Optional[List[str]]]] = []
        for action in result.actions:
            if isinstance(action, OpenChannelAction):
                candidates = self._resolve_channel_partners(wallet)
                if not candidates:
                    declines.append(self._no_partner_decline(wallet, action))
                    continue
                executable.append((action, candidates))
            else:
                executable.append((action, None))
        # Record declines (freeze events + near misses) first. Only those not
        # already present in the previous tick are logged, so a steady state
        # (e.g. the same freeze every tick) is logged once, not on every event.
        for decline in self._filter_new_declines(wallet, declines):
            self._log_decline(wallet, decline, state)
        # Then execute actions; each successful action logs its own entry
        # (enriched with the concrete peer / txid) inside _execute.
        for action, candidates in executable:
            await self._execute(wallet, action, state, transport, candidates)

    @staticmethod
    def _swap_may_be_needed(snapshot: LiquiditySnapshot, config: LiquidityConfig) -> bool:
        """True if some active channel is over a swap trigger (so it's worth
        opening a provider session). Opens, idle channels, and frozen ticks need
        no provider, so we skip the network round-trip for them."""
        if snapshot.pending_channel_count or snapshot.inflight_swap_count:
            return False
        for ch in snapshot.channels:
            if not ch.is_active:
                continue
            over_pct = ch.local_sat >= (config.swap_trigger_pct / 100.0) * ch.capacity_sat
            over_sat = ch.local_sat > config.swap_trigger_sat
            if over_pct or over_sat:
                return True
        return False

    @asynccontextmanager
    async def _swap_session(self, wallet: 'Abstract_Wallet') -> AsyncIterator[Optional['SwapServerTransport']]:
        """Open a swap transport for the duration of one evaluation.

        URL mode yields the stock single-provider HTTP transport. Otherwise it
        yields a :class:`TargetedNostrTransport` after giving relays a bounded
        moment to connect and provider offers to arrive, so the engine can pick
        the cheapest. Yields ``None`` if a transport cannot be opened.
        """
        from .swap_transport import TargetedNostrTransport
        from electrum.lnutil import generate_random_keypair
        sm = wallet.lnworker.swap_manager
        if self.config.SWAPSERVER_URL:
            # Single HTTP provider; provider selection does not apply.
            async with sm.create_transport() as transport:
                try:
                    await asyncio.wait_for(sm.is_initialized.wait(), timeout=15)
                except asyncio.TimeoutError:
                    self.logger.info("swap provider (URL) not reachable yet")
                yield transport
            return
        transport = TargetedNostrTransport(self.config, sm, generate_random_keypair())
        async with transport:
            try:
                await asyncio.wait_for(transport.is_connected.wait(),
                                       timeout=OFFER_CONNECT_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                self.logger.info("nostr relays did not connect; proceeding with no offers")
            await self._await_offers(transport)
            yield transport

    @staticmethod
    async def _await_offers(transport: 'SwapServerTransport',
                            timeout: float = OFFER_DISCOVERY_TIMEOUT_SEC) -> None:
        """Poll briefly for the first provider offers to arrive."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if transport.get_recent_offers():
                return
            await asyncio.sleep(0.5)

    async def refresh_providers(self, wallet: 'Abstract_Wallet') -> List[ProviderOffer]:
        """Open a session, discover providers, cache and return them. Used by the
        Providers settings tab's refresh button."""
        async with self._swap_session(wallet) as transport:
            offers = self._offers_from_transport(wallet, transport)
            self._last_offers[wallet] = offers
            return offers

    def discovered_providers(self, wallet: 'Abstract_Wallet') -> List[ProviderOffer]:
        """Last set of providers discovered on nostr (may be empty)."""
        return list(self._last_offers.get(wallet, []))

    @log_exceptions
    async def _execute(self, wallet: 'Abstract_Wallet', action: Action,
                       state: Optional[Dict] = None,
                       transport: Optional['SwapServerTransport'] = None,
                       candidates: Optional[List[str]] = None) -> None:
        if isinstance(action, OpenChannelAction):
            await self._open_channel(wallet, action, state, candidates)
        elif isinstance(action, ReverseSwapAction):
            await self._reverse_swap(wallet, action, state, transport)

    # --- actions ----------------------------------------------------------
    async def _open_channel(self, wallet: 'Abstract_Wallet', action: OpenChannelAction,
                            state: Optional[Dict] = None,
                            candidates: Optional[List[str]] = None) -> None:
        lnworker = wallet.lnworker
        # Ordered candidates: preferred partners first (in the user's order), then
        # Electrum's suggested peer (unless strict). Banned peers -- and, under the
        # one-channel-per-peer guard, peers we already have a channel with -- are
        # excluded. Normally pre-resolved by the caller (_run_decision, which turns
        # an empty result into a decline); re-resolve as a fallback for direct/test
        # callers.
        if candidates is None:
            candidates = self._resolve_channel_partners(wallet)
        if not candidates:
            self.logger.warning(
                "no channel partner available (already have a channel with every "
                "reachable peer, or no preferred/suggested peer); skipping open")
            return
        password = self._get_password(wallet)
        last_error: Optional[Exception] = None
        for connect_str in candidates:
            try:
                peer = await lnworker.lnpeermgr.add_peer(connect_str)
            except Exception as e:
                self.logger.info(
                    f"could not connect to partner {connect_str[:24]}…: {e!r}; trying next")
                # Couldn't even reach the peer: a *soft* fault (transient
                # unreachability) -- de-prioritise, but don't count toward auto-ban.
                self._record_peer_fault(
                    wallet, normalize_node_id(connect_str),
                    f"connect failed: {type(e).__name__}", hard=False)
                last_error = e
                continue
            # "Open with the maximum amount, leaving `reserve` on-chain": the
            # engine's funding_sat is the gross intent; here we deduct the funding
            # tx's mining fee so the transaction is actually feasible (a naive
            # max-minus-reserve leaves nothing for the fee -> NotEnoughFunds).
            funding_sat = self._max_funding_minus_reserve(wallet, peer.pubkey)
            if funding_sat is None or funding_sat < MIN_FUNDING_SAT:
                # A funds shortfall is peer-independent: trying other partners
                # would not help, so stop here rather than churn through them.
                self.logger.warning(
                    f"not enough on-chain to open a channel (feasible {funding_sat} < "
                    f"min {MIN_FUNDING_SAT}); skipping")
                return
            self.logger.info(f"opening channel: {funding_sat} sat -> {connect_str} ({action.reason})")
            try:
                chan, funding_tx = await lnworker.open_channel_with_peer(
                    peer, funding_sat, push_sat=0, password=password)
            except Exception as e:
                self.logger.warning(
                    f"channel open to {connect_str[:24]}… failed: {e!r}; trying next")
                # We connected but the open negotiation failed: a *hard* fault
                # (counts toward auto-ban), charged to the peer we reached.
                self._record_peer_fault(
                    wallet, peer.pubkey.hex(),
                    f"channel open failed: {type(e).__name__}", hard=True)
                self._diag_event(wallet, category="error", kind="open",
                                 reason="channel open failed", dest=peer.pubkey.hex(),
                                 detail=f"{type(e).__name__}: {e}")
                last_error = e
                continue
            self.logger.info(f"opened channel {chan.funding_outpoint.to_str()}")
            # Tag this channel as plugin-opened so the offline auto-close watchdog
            # may later manage it (it never touches channels we did not open). Never
            # let a tagging hiccup abort an open that actually succeeded.
            try:
                self._tag_plugin_opened_channel(wallet, chan.channel_id.hex())
            except Exception as e:
                self.logger.info(f"could not tag plugin-opened channel: {e!r}")
            # A clean open clears the peer's penalty at its source.
            self._record_peer_success(wallet, peer.pubkey.hex())
            # Count this open toward the rolling-24h open ceiling.
            self._record_action_event(wallet, "open")
            self._log_action(
                wallet, kind="open", amount_sat=funding_sat,
                source="on-chain", dest=peer.pubkey.hex(),
                reason=action.reason,
                detail=f"funding txid {chan.funding_outpoint.txid}",
                state=state)
            self.on_action_done(wallet, _("Opened channel: {} sat").format(funding_sat))
            return
        self.logger.warning(
            f"all {len(candidates)} channel partner(s) failed to open; "
            f"last error: {last_error!r}")

    def _max_funding_minus_reserve(self, wallet: 'Abstract_Wallet', node_id: bytes) -> Optional[int]:
        """Largest channel we can fund while leaving ~`onchain_reserve_sat` behind.

        Builds a trial max-spend funding tx (output value '!') to learn the exact
        fee at the current fee policy, then subtracts the configured reserve.
        """
        from electrum.fee_policy import FeePolicy
        from electrum.util import NotEnoughFunds
        lnworker = wallet.lnworker
        reserve = int(self.config.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT)
        coins = wallet.get_spendable_coins(None)
        try:
            trial = lnworker.mktx_for_open_channel(
                coins=coins, funding_sat='!', node_id=node_id,
                fee_policy=FeePolicy(self.config.FEE_POLICY))
        except NotEnoughFunds:
            return None
        # The channel output carries the maximised value (any extra output, e.g.
        # the recovery OP_RETURN, is 0-valued).
        max_fundable = max((o.value for o in trial.outputs() if isinstance(o.value, int)), default=0)
        funding_sat = max_fundable - reserve
        cap = int(self.config.LIGHTNING_MAX_FUNDING_SAT)
        return min(funding_sat, cap)

    async def _reverse_swap(self, wallet: 'Abstract_Wallet', action: ReverseSwapAction,
                            state: Optional[Dict] = None,
                            transport: Optional['SwapServerTransport'] = None) -> None:
        from contextlib import nullcontext
        from electrum.util import UserFacingException
        from electrum.submarine_swaps import SwapServerError
        sm = wallet.lnworker.swap_manager
        # Cooldown: don't re-attempt a channel we just acted on.
        now = time.monotonic()
        until = self._swap_cooldown_until.get(action.channel_id, 0.0)
        if now < until:
            return
        # Resolve which provider to swap with. A chosen npub (multi-provider /
        # nostr) points the swap math + RPC at that specific provider; an empty
        # npub keeps the legacy single-provider behaviour (config.SWAPSERVER_*).
        target_pubkey: Optional[str] = None
        provider_label = "configured provider"
        if action.provider_npub:
            offer = transport.get_offer(action.provider_npub) if transport is not None else None
            if offer is None:
                self.logger.warning(
                    f"chosen provider {action.provider_npub[:12]}… no longer "
                    f"advertising; skipping swap on {action.short_id}")
                return
            # Make sm.get_recv_amount / sanity checks use the chosen provider's
            # terms, and address the swap RPC to it.
            sm.update_pairs(offer.pairs)
            target_pubkey = offer.server_pubkey
            provider_label = action.provider_npub
        elif not (self.config.SWAPSERVER_NPUB or self.config.SWAPSERVER_URL):
            self.logger.warning("no swap provider configured; skipping reverse swap")
            return
        self._swap_cooldown_until[action.channel_id] = now + SWAP_COOLDOWN_SEC
        self.logger.info(f"reverse swap via {provider_label[:20]}…: {action.reason}")

        # Reuse the evaluation's open session when present; otherwise open a
        # transient transport (e.g. URL mode without a prior session).
        own_transport = transport is None
        session = sm.create_transport() if own_transport else nullcontext(transport)
        npub = action.provider_npub
        async with session as tr:
            if target_pubkey is not None and hasattr(tr, "target_pubkey"):
                tr.target_pubkey = target_pubkey
            try:
                await asyncio.wait_for(sm.is_initialized.wait(), timeout=15)
            except asyncio.TimeoutError:
                # Unreachable provider is a reliability fault (timeout signal).
                self.logger.warning("swap provider not reachable; skipping reverse swap")
                self._record_provider_fault(wallet, npub, "not reachable (init timeout)")
                return
            lightning_amount_sat = action.lightning_amount_sat
            expected_onchain_sat = sm.get_recv_amount(lightning_amount_sat, is_reverse=True)
            # get_recv_amount() returns None when this amount isn't swappable with
            # the chosen provider (outside its min/max bounds, or it nets below
            # dust after fees). The max_forward cap fix above should keep the
            # planner from picking such an amount, but guard regardless: feeding
            # None into reverse_swap() crashes its cost sanity-check (int - None).
            # This is "wait for the channel to grow / retry", NOT a provider fault.
            if expected_onchain_sat is None or sm.mining_fee is None:
                self.logger.info(
                    f"skipping reverse swap of {lightning_amount_sat} sat: provider "
                    f"cannot host this amount right now (no receivable amount)")
                self._diag_event(wallet, category="error", kind="swap",
                                 reason="amount not swappable with provider", source=npub,
                                 detail=f"{lightning_amount_sat} sat")
                return
            prepayment_sat = 2 * sm.mining_fee
            # Snapshot the swap set so we can identify the swap we are about to
            # create and, if it never funds, attribute the stall to this provider.
            swaps_before = set(getattr(sm, "_swaps", {}).keys())
            try:
                # Bounded so no single swap can hold the per-wallet evaluation
                # lock indefinitely. The precise hang (a provider that never
                # answers the createswap RPC) is already bounded inside the
                # transport; this coarse backstop covers anything else that could
                # stall (see REVERSE_SWAP_TIMEOUT_SEC). A timeout surfaces as
                # asyncio.TimeoutError, handled below like any unresponsive
                # provider.
                funding_txid = await asyncio.wait_for(
                    sm.reverse_swap(
                        transport=tr,
                        lightning_amount_sat=lightning_amount_sat,
                        expected_onchain_amount_sat=expected_onchain_sat,
                        prepayment_sat=prepayment_sat,
                    ),
                    timeout=self._reverse_swap_timeout_sec,
                )
            except UserFacingException as e:
                # e.g. the provider deems the swap uneconomical for this amount.
                # This is a legitimate response, NOT a reliability fault; just
                # wait for the channel to grow.
                self.logger.info(
                    f"provider declined reverse swap of {lightning_amount_sat} sat: "
                    f"{scrub_text(e)}")
                self._diag_event(wallet, category="error", kind="swap",
                                 reason="provider declined reverse swap", source=npub,
                                 detail=f"{lightning_amount_sat} sat: {e}")
                return
            except asyncio.TimeoutError as e:
                # Either the createswap RPC reply never arrived (bounded in the
                # transport) or the whole attempt exceeded the coarse backstop
                # while its Lightning payment was in flight. Both mean an
                # unresponsive provider -> a genuine reliability fault, so
                # escalate normally. If a swap object was already created (the
                # payment leg had started before the backstop fired), track it so
                # reconciliation still attributes its eventual outcome (funded ->
                # success, never funds -> stuck) instead of silently losing it.
                self._track_new_swaps(wallet, sm, swaps_before, npub, action,
                                      expected_onchain_sat)
                self.logger.warning(f"reverse swap timed out [DO NOT TRUST]: {e!r}")
                self._record_provider_fault(wallet, npub, f"RPC timeout: {type(e).__name__}")
                self._diag_event(wallet, category="error", kind="swap",
                                 reason="reverse swap RPC timed out", source=npub,
                                 detail=f"{type(e).__name__}: {e}")
                return
            except SwapServerError as e:
                # The server rejected createswap, but masks the real cause as a
                # generic "Internal Server Error" (see NostrTransport), so we cannot
                # cleanly attribute it. The overwhelmingly common cause is transient
                # capacity: the provider's advertised max_forward was drawn down
                # (often by our OWN earlier swap this cycle) between advertisement
                # and execution, so its create_normal_swap hits "no onchain amount".
                # The engine's per-provider capacity budgeting prevents most of
                # these, so a residual one is treated as a SOFT (non-escalating)
                # fault: recorded for visibility but not enough to poison a healthy
                # provider's ranking. It self-heals next cycle once the provider
                # re-advertises its reduced capacity.
                self.logger.info(
                    f"provider rejected reverse swap of {lightning_amount_sat} sat "
                    f"(likely transient capacity) [DO NOT TRUST]: {e!r}")
                self._record_provider_fault(
                    wallet, npub, "swap rejected (likely transient capacity)", soft=True)
                self._diag_event(wallet, category="error", kind="swap",
                                 reason="provider rejected reverse swap (transient?)",
                                 source=npub, detail=f"{type(e).__name__}: {e}")
                return
            except Exception as e:
                if any(m in str(e) for m in _REVERSE_SWAP_CHEAT_MARKERS):
                    # Electrum's pre-payment sanity checks caught an UNAMBIGUOUS
                    # provider cheat (short-changed on-chain amount, mismatched
                    # RHASH, or wrong invoice amount). No funds are at risk -- these
                    # fire before any Lightning payment -- but the provider tried to
                    # cheat, so charge an escalating (hard) fault: repeat offenders
                    # sink in the ranking toward a ban, and we stop wasting cycles
                    # re-picking them. The message carries provider-influenced
                    # numbers, so scrub it before logging/recording.
                    self.logger.warning(
                        f"provider failed reverse-swap sanity check (possible cheat) "
                        f"[DO NOT TRUST]: {scrub_text(e)}")
                    self._record_provider_fault(
                        wallet, npub,
                        f"failed swap sanity check: {scrub_text(e, max_len=80)}")
                    self._diag_event(
                        wallet, category="error", kind="swap",
                        reason="provider failed reverse-swap sanity check (possible cheat)",
                        source=npub, detail=scrub_text(e))
                    return
                # Any other exception is our-side, not the provider misbehaving: a
                # stale local tip or too-close locktime relative to OUR height, or a
                # genuine bug (e.g. a bad argument to reverse_swap). Log it with a
                # traceback and record it as an internal error -- do NOT penalise
                # the provider's reliability for our own condition (a healthy
                # provider must not be de-prioritised or banned because of it).
                self.logger.error(f"reverse swap failed (internal/our-side): {e!r}", exc_info=True)
                self._diag_event(wallet, category="error", kind="swap",
                                 reason="reverse swap internal error", source=npub,
                                 detail=f"{type(e).__name__}: {e}")
                return
        if funding_txid:
            # The provider created the on-chain funding output: it honoured the
            # swap. Count it as a success straight away (the claim is Electrum's
            # job from here). This is a confirmed completion, so accrue the dev fee
            # now, on the net on-chain amount received.
            self._record_provider_success(wallet, npub)
            self._accrue_dev_fee(wallet, expected_onchain_sat, source=action.short_id)
        else:
            # Accepted but no funding yet -- watch it; reconciliation records a
            # success once it funds, a provider stuck fault if it never does, or a
            # peer fault if our Lightning payment for it fails.
            self._track_new_swaps(wallet, sm, swaps_before, npub, action,
                                  expected_onchain_sat)
        self.logger.info(f"reverse swap funding txid: {funding_txid}")
        self._log_action(
            wallet, kind="swap", amount_sat=lightning_amount_sat,
            source=action.short_id, dest="on-chain",
            reason=action.reason,
            detail=(f"funding txid {funding_txid}; expected on-chain {expected_onchain_sat} sat; "
                    f"provider {provider_label}"),
            state=state)
        self.on_action_done(
            wallet, _("Reverse swap {} sat from {}").format(lightning_amount_sat, action.short_id))

    def _track_new_swaps(self, wallet: 'Abstract_Wallet', sm, swaps_before: set,
                         npub: str, action: ReverseSwapAction,
                         expected_onchain_sat: int) -> None:
        """Track any reverse swap the swap manager gained during this attempt
        (``sm._swaps`` keys not present in ``swaps_before``) for later
        reconciliation. Stashes the channel's peer so a failed Lightning payment
        can be attributed to it, and the expected on-chain amount so the dev fee
        is accrued iff the swap later completes. Used both on the accepted-but-
        not-yet-funded path and on a timeout that fired after the swap object was
        already created (its payment leg was in flight)."""
        new_swaps = set(getattr(sm, "_swaps", {}).keys()) - swaps_before
        if not new_swaps:
            return
        peer_node_id = self._channel_peer_node_id(wallet, action.channel_id)
        for ph_hex in new_swaps:
            self._track_pending_swap(wallet, ph_hex, npub,
                                     node_id=peer_node_id, channel_id=action.channel_id,
                                     fee_basis_sat=expected_onchain_sat)

    # --- helpers ----------------------------------------------------------
    def _migrate_channel_peer(self) -> None:
        """Fold the deprecated single ``channel_peer`` setting into the front of
        the preferred-partners list (one-time), so an existing config keeps its
        target after that field was removed from the UI. Idempotent: it clears
        ``channel_peer`` once migrated."""
        old = (self.config.INBOUND_LIQUIDITY_CHANNEL_PEER or '').strip()
        if not old:
            return
        preferred = _parse_partner_list(self.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS)
        if normalize_node_id(old) not in {normalize_node_id(p) for p in preferred}:
            preferred.insert(0, old)
        self.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS = ", ".join(preferred)
        self.config.INBOUND_LIQUIDITY_CHANNEL_PEER = ''
        self.logger.info(f"migrated channel_peer {old[:24]}… into preferred partners")

    def _resolve_channel_partners(self, wallet: 'Abstract_Wallet',
                                  *, apply_peer_guard: bool = True) -> List[str]:
        """Ordered connect strings to attempt a channel open against: preferred
        partners first (user order), then Electrum's suggested peer unless strict
        mode is on. Banned partners (by pubkey) are excluded from both.

        When the one-channel-per-peer guard is on (default), peers we already
        hold a non-closed channel with are excluded too, so the plugin spreads
        capacity across distinct nodes. Pass ``apply_peer_guard=False`` to get
        the pre-guard ordering (used to tell "no partner at all" apart from
        "only the guard blocked us" when reporting a decline)."""
        preferred = _parse_partner_list(self.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS)
        banned = _parse_banned_partners(self.config.INBOUND_LIQUIDITY_BANNED_PARTNERS)
        strict = bool(self.config.INBOUND_LIQUIDITY_PARTNERS_STRICT)
        exclude: frozenset = frozenset()
        if apply_peer_guard and bool(self.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER):
            exclude = self._current_peer_node_ids(wallet)
        suggested: List[str] = []
        if not strict:
            try:
                node_id = wallet.lnworker.suggest_peer()
            except Exception:
                node_id = None
            if node_id:
                suggested.append(node_id.hex())
        # Sink flaky peers in the try-order (soft de-prioritisation); banned peers
        # are already excluded above. Auto-banned serial offenders never get here.
        penalties = self._peer_penalties(wallet)
        return order_channel_partners(preferred, banned, suggested, strict=strict,
                                      penalties=penalties, exclude=exclude)

    def _no_partner_decline(self, wallet: 'Abstract_Wallet',
                            action: OpenChannelAction) -> DeclineRecord:
        """Why an engine-approved open could not proceed: the one-channel-per-peer
        guard eliminated every candidate, or there is simply no reachable partner.
        Distinguished (by re-resolving without the guard) so the decision log is
        actionable."""
        guard_on = bool(self.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER)
        if guard_on and self._resolve_channel_partners(wallet, apply_peer_guard=False):
            reason = ("have funds and room to open, but already hold a channel with "
                      "every available peer (one-channel-per-peer guard); not opening")
        else:
            reason = ("have funds and room to open, but no reachable channel partner "
                      "is available (no preferred/suggested peer); not opening")
        return DeclineRecord(kind="open", reason=reason, amount_sat=action.funding_sat)

    def _current_peer_node_ids(self, wallet: 'Abstract_Wallet') -> frozenset:
        """Normalized (lowercased) pubkeys of every peer we currently hold a
        NON-CLOSED channel with, for the one-channel-per-peer guard.

        "Non-closed" means the channel's state is below ``CLOSED`` -- so an
        opening/pending, OPEN, or still-closing/force-closing channel keeps its
        peer excluded, and only a fully closed/redeemed channel frees the peer up
        for a fresh open. Mirrors the user-chosen "any non-closed channel"
        scope."""
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return frozenset()
        try:
            from electrum.lnchannel import ChannelState
        except Exception:
            ChannelState = None
        out: set = set()
        try:
            channels = list(lnworker.channels.values())
        except Exception:
            return frozenset()
        for chan in channels:
            try:
                if ChannelState is not None and chan.get_state() >= ChannelState.CLOSED:
                    continue  # fully closed/redeemed: peer is free to reopen to
                nid = normalize_node_id(chan.node_id.hex())
            except Exception:
                continue
            if nid:
                out.add(nid)
        return frozenset(out)

    def current_channel_partners(self, wallet: 'Abstract_Wallet') -> List[Dict]:
        """Nodes we already have channels with (deduplicated by node id), for the
        Channel partners settings tab's quick prefer/ban toggles."""
        out: List[Dict] = []
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return out
        seen: set = set()
        try:
            channels = list(lnworker.channels.values())
        except Exception:
            return out
        for chan in channels:
            try:
                node_id = chan.node_id.hex()
            except Exception:
                continue
            if node_id in seen:
                continue
            seen.add(node_id)
            out.append({"node_id": node_id})
        return out

    def _channel_peer_node_id(self, wallet: 'Abstract_Wallet', channel_id_hex: str) -> str:
        """The peer node id (hex) for a channel id, or '' if it can't be resolved.
        Used to attribute a failed reverse-swap Lightning payment to the peer."""
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None or not channel_id_hex:
            return ""
        try:
            chan = lnworker.get_channel_by_id(bytes.fromhex(channel_id_hex))
            return chan.node_id.hex() if chan is not None else ""
        except Exception:
            return ""

    def _get_password(self, wallet: 'Abstract_Wallet') -> Optional[str]:
        # The rig wallet is unencrypted. Encrypted wallets are handled by the
        # GUI subclass, which can prompt / cache; here we only proceed if the
        # wallet does not require a password.
        if wallet.has_keystore_encryption():
            self.logger.warning(f"{wallet.basename()} is password-protected; cannot auto-open channel")
            raise Exception("wallet requires password for channel open")
        return None

    def on_action_done(self, wallet: 'Abstract_Wallet', message: str) -> None:
        """Hook for GUI subclasses to surface activity. No-op in headless."""
        pass

    def on_log_changed(self, wallet: 'Abstract_Wallet') -> None:
        """Hook for GUI subclasses to refresh the decision-log view. No-op in
        headless."""
        pass

    # --- decision log -----------------------------------------------------
    @staticmethod
    def _abbrev(value: Optional[str], head: int = 6, tail: int = 4) -> Optional[str]:
        """Shorten a long id/address for the log's compact columns."""
        if not value:
            return value
        if len(value) <= head + tail + 1:
            return value
        return f"{value[:head]}…{value[-tail:]}"

    def _retention_days(self) -> int:
        try:
            days = int(getattr(self.config, "INBOUND_LIQUIDITY_LOG_RETENTION_DAYS",
                               DEFAULT_LOG_RETENTION_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_LOG_RETENTION_DAYS
        return max(1, min(days, MAX_LOG_RETENTION_DAYS))

    def _log_fault(self, wallet: 'Abstract_Wallet', *, kind: str, ident: str,
                   reason: str, hard: bool, soft: bool = False) -> None:
        """Append a reliability fault to the decision log (category "fault") so its
        reason is surfaced to the user in the Faults view. ``kind`` is "peer" or
        "provider"; ``ident`` is the node id / npub. ``soft`` marks an ambiguous,
        non-escalating fault (see :meth:`_record_provider_fault`) so the Faults
        view can show it is a low-severity/transient signal, not a real fault. A
        no-op for db-less wallets (unit-test mocks)."""
        if getattr(wallet, "db", None) is None:
            return
        prefix = "soft fault: " if soft else ("hard fault: " if hard else "fault: ")
        entry = {
            "ts": time.time(),
            "category": "fault",
            "kind": kind,
            "amount_sat": None,
            "source": self._abbrev(ident),
            "dest": None,
            "reason": prefix + reason,
            "detail": None,
            "state": {},
        }
        self._append_log(wallet, entry)

    def _state_dict(self, snapshot: LiquiditySnapshot, config: LiquidityConfig) -> Dict:
        """The state variables behind a decision, for the log's expand view.
        Plain JSON-serialisable types only (persisted in wallet.db)."""
        return {
            "onchain_spendable_sat": snapshot.onchain_spendable_sat,
            "num_channels": len(snapshot.channels),
            "active_channels": sum(1 for c in snapshot.channels if c.is_active),
            "pending_channel_count": snapshot.pending_channel_count,
            "inflight_swap_count": snapshot.inflight_swap_count,
            "swap_percentage_fee": snapshot.swap_percentage_fee,
            "provider_min_amount_sat": snapshot.provider_min_amount_sat,
            "provider_max_reverse_sat": snapshot.provider_max_reverse_sat,
            "swap_mining_fee_sat": snapshot.swap_mining_fee_sat,
            "swap_claim_fee_sat": snapshot.swap_claim_fee_sat,
            "providers_discovered": len(snapshot.provider_offers),
            "providers_eligible": len(eligible_providers(snapshot.provider_offers, config)),
            "opens_last_24h": snapshot.opens_last_24h,
            "config": {
                "max_channels": config.max_channels,
                "min_onchain_to_open_sat": config.min_onchain_to_open_sat,
                "onchain_reserve_sat": config.onchain_reserve_sat,
                "max_swap_fee_pct": config.max_swap_fee_pct,
                "swap_trigger_pct": config.swap_trigger_pct,
                "swap_trigger_sat": config.swap_trigger_sat,
                "max_opens_per_day": config.max_opens_per_day,
                "max_closes_per_day": self._max_closes_per_day(),
                "stuck_swap_timeout_min": int(self._stuck_swap_timeout_sec() // 60),
                "preferred_npubs": sorted(config.preferred_npubs),
                "banned_npubs": sorted(config.banned_npubs),
            },
            "channels": [
                {
                    "short_id": c.short_id,
                    "capacity_sat": c.capacity_sat,
                    "local_sat": c.local_sat,
                    "remote_sat": c.remote_sat,
                    "spendable_local_sat": c.spendable_local_sat,
                    "is_active": c.is_active,
                }
                for c in snapshot.channels
            ],
        }

    def _log_action(self, wallet: 'Abstract_Wallet', *, kind: str, amount_sat: Optional[int],
                    source: Optional[str], dest: Optional[str], reason: str,
                    detail: Optional[str], state: Optional[Dict]) -> None:
        entry = {
            "ts": time.time(),
            "category": "action",
            "kind": kind,
            "amount_sat": amount_sat,
            "source": self._abbrev(source) if source and source != "on-chain" else source,
            "dest": self._abbrev(dest) if dest and dest != "on-chain" else dest,
            "reason": reason,
            "detail": detail,
            "state": state or {},
        }
        self._append_log(wallet, entry)
        # An action changes wallet state, so the next tick's declines (if any)
        # should be logged afresh rather than deduped against the pre-action set.
        self._last_decline_sigs.pop(wallet, None)

    @staticmethod
    def _decline_sig(decline: 'DeclineRecord') -> tuple:
        return (decline.kind, decline.channel_id, decline.reason)

    def _filter_new_declines(self, wallet: 'Abstract_Wallet',
                             declines) -> List['DeclineRecord']:
        """Return only the declines not present in the previous evaluation, and
        record the current set for next time. Tick-level (not per-row) dedupe, so
        a rotating set of declines that recurs every tick is still collapsed."""
        current = {self._decline_sig(d) for d in declines}
        previous = self._last_decline_sigs.get(wallet, set())
        self._last_decline_sigs[wallet] = current
        return [d for d in declines if self._decline_sig(d) not in previous]

    def _log_decline(self, wallet: 'Abstract_Wallet', decline: 'DeclineRecord',
                     state: Optional[Dict]) -> None:
        entry = {
            "ts": time.time(),
            "category": "decline",
            "kind": decline.kind,
            "amount_sat": decline.amount_sat,
            "source": self._abbrev(decline.short_id),
            "dest": None,
            "reason": decline.reason,
            "detail": None,
            "state": state or {},
        }
        self._append_log(wallet, entry)

    def _load_log(self, wallet: 'Abstract_Wallet') -> List[Dict]:
        raw = wallet.db.get(LOG_DB_KEY, [])
        return list(raw) if isinstance(raw, list) else []

    def _prune(self, entries: List[Dict]) -> List[Dict]:
        cutoff = time.time() - self._retention_days() * 86400
        kept = [e for e in entries if float(e.get("ts", 0)) >= cutoff]
        if len(kept) > MAX_LOG_ENTRIES:
            kept = kept[-MAX_LOG_ENTRIES:]
        return kept

    def _append_log(self, wallet: 'Abstract_Wallet', entry: Dict) -> None:
        entries = self._load_log(wallet)
        entries.append(entry)
        entries = self._prune(entries)
        wallet.db.put(LOG_DB_KEY, entries)
        try:
            wallet.save_db()
        except Exception as e:
            self.logger.info(f"could not persist decision log: {e!r}")
        # Mirror the same already-scrubbed entry to the on-disk diagnostic log
        # (a no-op unless the operator has enabled it).
        self._diag_write(wallet, entry)
        self.on_log_changed(wallet)

    # --- diagnostic log file (opt-in) -------------------------------------
    def _diag_logger(self) -> Optional[DiagLog]:
        """A DiagLog rooted under the Electrum data dir, or None when the
        feature is disabled (or no data dir is available, e.g. test mocks)."""
        if not bool(getattr(self.config, "INBOUND_LIQUIDITY_DIAG_LOG_ENABLED", False)):
            return None
        base = getattr(self.config, "path", None)
        if not base:
            return None
        return DiagLog(os.path.join(base, DIAG_LOG_DIRNAME),
                       retention_days=DIAG_LOG_RETENTION_DAYS)

    def _diag_write(self, wallet: 'Abstract_Wallet', entry: Dict) -> None:
        """Append one already-built decision-log entry to the diagnostic file."""
        logger = self._diag_logger()
        if logger is None:
            return
        name = None
        try:
            name = wallet.basename()
        except Exception:
            name = "wallet"
        logger.write(name, entry)

    def _diag_event(self, wallet: 'Abstract_Wallet', *, category: str, kind: str,
                    reason: str, detail: Optional[str] = None,
                    source: Optional[str] = None, dest: Optional[str] = None) -> None:
        """Record a file-only operational event (errors, lifecycle) in the same
        entry shape as a decision. Not added to the in-wallet decision log, so
        the GUI tabs are unchanged; visible only in the diagnostic files."""
        if self._diag_logger() is None:
            return
        entry = {
            "ts": time.time(),
            "category": category,
            "kind": kind,
            "amount_sat": None,
            "source": self._abbrev(source),
            "dest": self._abbrev(dest),
            "reason": reason,
            "detail": detail,
            "state": {},
        }
        self._diag_write(wallet, entry)

    def get_decision_log(self, wallet: 'Abstract_Wallet',
                         category: Optional[str] = None) -> List[Dict]:
        """Most-recent-first log entries, optionally filtered by category
        ("action" / "decline"). Pruned to the retention window on read."""
        entries = self._prune(self._load_log(wallet))
        if category is not None:
            entries = [e for e in entries if e.get("category") == category]
        return list(reversed(entries))
