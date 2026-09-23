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
import dataclasses
import functools
import json
import math
import os
import pkgutil
import sys
import time
from concurrent import futures
from contextlib import asynccontextmanager
from enum import Enum, auto
from typing import (TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, List, Optional,
                    Sequence, Set, Tuple)

from electrum import util
from electrum.i18n import _
from electrum.plugin import BasePlugin
from electrum.simple_config import ConfigVar, SimpleConfig
from electrum.util import log_exceptions, ignore_exceptions, make_aiohttp_session
from electrum.logging import get_logger

# --- external-zip load compatibility --------------------------------------
# When Electrum installs this plugin as an external ZIP, its loader execs this
# __init__ with __name__/__package__ set to the bare package directory
# ("inbound_liquidity") while registering the module in sys.modules under a
# different key ("electrum_external_plugins.inbound_liquidity"). Python resolves
# the relative imports below (``from .liquidity_manager import ...``) against
# __package__, so under a zip install they would look for a top-level
# ``inbound_liquidity`` package that is not on sys.path -- raising
# ModuleNotFoundError and crashing Electrum on install. (Internal/symlinked
# loads are unaffected: there __name__ already equals the sys.modules key, so
# this is a no-op.) Adopt the real sys.modules key as this module's
# __name__/__package__ so the relative imports here -- and the qt submodule,
# which Electrum later loads under that same key -- all resolve to one
# consistent package object (avoiding duplicate submodule copies too).
_real_key = next((_k for _k, _m in sys.modules.items()
                  if getattr(_m, "__dict__", None) is globals()), None)
if _real_key and _real_key != __name__:
    __name__ = _real_key
    __package__ = _real_key

from .liquidity_manager import (
    BLOCK_LOCKED,
    BLOCK_NOT_MANAGED,
    Action,
    ChannelSnapshot,
    clean_npub,
    CloseChannelAction,
    DAILY_WINDOW_SEC,
    DeclineRecord,
    LiquidityConfig,
    LiquiditySinkAction,
    LiquiditySnapshot,
    MAX_SUGGESTED_PARTNERS,
    MAX_SWAP_PROVIDER_ATTEMPTS,
    MIN_FUNDING_SAT,
    OpenChannelAction,
    over_swap_trigger,
    PartnerResolution,
    ProviderAttempt,
    ProviderOffer,
    ProviderReliability,
    ReleaseInfo,
    ReverseSwapAction,
    SINK_MIN_PAYMENT_SAT,
    COOP_BEFORE_FORCE_WINDOW_SEC,
    COOP_MAX_ATTEMPTS,
    WEDGE_ABSOLUTE_CAP_SEC,
    WEDGE_MIN_SPREAD_SEC,
    WEDGE_REQUIRED_STRIKES,
    classify_peer_observation,
    classify_pending_freeze,
    clamp_dev_fee_pct,
    compute_dev_fee,
    coop_before_force_ready,
    count_within_window,
    daily_cap_reached,
    deadline_reached,
    decayed_penalty_pct,
    decide_dev_fee_payout,
    eligible_providers,
    evaluate,
    extract_release,
    is_channel_size_rejection,
    is_newer_version,
    liquidity_goal_met,
    normalize_node_id,
    order_channel_partners,
    record_uptime_sample,
    record_wedge_strike,
    reliability_penalty_pct,
    resolve_channel_partners,
    scrub_text,
    should_auto_ban,
    should_commit_offline_close,
    should_remediate_wedged_open,
    uptime_ratio,
    validate_offer,
    wallet_readiness_block,
)
from .diag_log import DiagLog
from .log_buffer import (
    DEFAULT_MAX_LINES as DEFAULT_LOG_BUFFER_LINES,
    LogCapture,
    LogLine,
    LogRingBuffer,
    MAX_MAX_LINES as MAX_LOG_BUFFER_LINES,
    MIN_MAX_LINES as MIN_LOG_BUFFER_LINES,
    clamp_max_lines,
)

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet
    from electrum.submarine_swaps import SwapOffer, SwapServerTransport
    from electrum.lnurl import LNURL6Data
    from electrum.invoices import Invoice


def _safe_int(value: object, default: int = 0) -> int:
    """``int(value)`` for anything that can be one, else ``default``. Used where a
    value is about to be written into a JSON-backed store and must be a plain
    number no matter what the caller handed us."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


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

# The bare plugin name ("inbound_liquidity"), independent of how this package was
# imported. Electrum's ConfigVar(plugin=...) enforces that the value has no dots:
# it strips the "electrum.plugins." prefix for INTERNAL plugins, but NOT the
# "electrum_external_plugins." prefix used when the plugin is installed as a zip.
# Passing the raw ``__name__`` therefore blows up with an AssertionError at import
# time under a zip install (``__name__`` = "electrum_external_plugins.<name>").
# Deriving the last path component works for both load paths.
_PLUGIN_NAME = __name__.rsplit(".", 1)[-1]

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
]

# Handled separately from _TRIGGER_EVENTS because this is the one trigger the
# plugin can fire at *itself*. Every evaluation that might swap opens a nostr
# session (``_swap_session``), and Electrum's nostr offer handler raises
# ``swap_offers_changed`` once per provider announcement it receives
# (submarine_swaps.py). So our own discovery re-entered this handler, which
# scheduled another evaluation, which opened another session -- a closed loop
# that re-connected every relay every few seconds for as long as a swap looked
# possible, with nothing in the wallet actually changing. Those events are
# suppressed while we hold a session of our own (see ``_on_offers_event``); the
# event stays subscribed so a provider discovered by Electrum's *own* swap
# machinery (the user opening the Swap dialog, say) still wakes us.
_OFFER_TRIGGER_EVENTS = [
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

# Wall-clock ceiling on one channel's whole provider cascade (chosen provider
# plus failovers). Checked BEFORE starting each attempt, never mid-attempt: once
# an attempt is under way it runs to its own REVERSE_SWAP_TIMEOUT_SEC backstop,
# because aborting a swap whose Lightning payment may already be in flight is
# exactly what we must not do. Any providers left untried are simply retried on
# the next evaluation cycle, by which time the failed ones have sunk in the
# ranking. Sized above one worst-case backstop (300s) so a single slow provider
# can never consume the budget before a second is even tried.
SWAP_CASCADE_DEADLINE_SEC = 500.0

# --- liquidity sink timing ------------------------------------------------
# Coarse backstop on ONE sink payment. Electrum gives up on a payment of its own
# accord at LNWallet.PAYMENT_TIMEOUT (120s), so this sits just above it: high
# enough never to cut short a payment Electrum still believes in, low enough that
# a wedge somewhere below pay_invoice cannot hold the evaluation lock. Referenced
# via ``self._sink_payment_timeout_sec`` so tests can shrink it.
SINK_PAYMENT_TIMEOUT_SEC = 150.0

# Wall-clock ceiling on one channel's whole sink ladder. Checked BEFORE starting
# each rung, never mid-payment -- the same rule the swap cascade follows, and for
# the same reason: abandoning a payment whose HTLC may already be in flight is
# precisely what must not happen.
#
# Deliberately a SEPARATE budget from SWAP_CASCADE_DEADLINE_SEC rather than a
# shared one. The swap fallback is the safety net for a sink that cannot deliver,
# so a slow ladder must not be able to consume the budget the fallback needs --
# which is exactly what one shared deadline would do. The cost is the worst case:
# a channel whose sink is dead slow AND whose providers are all slow can hold the
# evaluation lock for this plus the swap cascade (~800s). That is bounded, rare,
# and strictly better than silently skipping the fallback.
SINK_CASCADE_DEADLINE_SEC = 300.0

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

# Bare ``Exception``s from ``reverse_swap``'s pre-payment checks that are OUR
# condition, not the provider's, and that every other provider would hit
# identically -- so failing over would just burn the cascade budget on a
# guaranteed-identical failure. These abort the cascade outright.
#
# Note what is deliberately NOT here: "locktime too close" -- see
# _REVERSE_SWAP_RETRYABLE_MARKERS below.
_REVERSE_SWAP_OUR_SIDE_MARKERS = (
    "blockchain tip is stale",     # our local tip is behind; provider-independent
)

# The converse: bare ``Exception``s from those same pre-payment checks that are
# worth trying a DIFFERENT provider for. A too-close locktime is checked against
# OUR height, so it never proves provider misbehaviour (no fault is recorded) --
# but the locktime itself is the provider's choice (``timeoutBlockHeight`` in its
# createswap reply), so a provider with a tighter policy fails exactly where a
# more generous one succeeds. Like every marker here it fires before any payment.
#
# Anything matching NEITHER tuple is an unrecognised failure: we cannot show it
# left no funds committed, so the cascade stops rather than guessing.
_REVERSE_SWAP_RETRYABLE_MARKERS = (
    "locktime too close",          # provider-chosen timeoutBlockHeight vs our height
)


# What one provider attempt concluded, and hence what the cascade does next.
class _SwapAttempt(Enum):
    COMMITTED = auto()   # funds committed (funded, accepted, or possibly in flight): STOP
    NEXT = auto()        # failed with nothing committed: safe to try the next provider
    ABORT = auto()       # our-side condition or unknown bug: stop trying providers


# What one liquidity-sink payment attempt concluded, and hence what the ladder
# does next. The distinction that matters is NEXT vs ABORT: halving and retrying
# is only safe once we know the failed attempt left nothing committed on the
# channel (see ``_payment_still_inflight``).
class _SinkAttempt(Enum):
    PAID = auto()        # the payment settled: the channel is drained, STOP
    NEXT = auto()        # failed cleanly, nothing in flight: halve and retry
    UNUSABLE = auto()    # the ENDPOINT is the problem, not the amount: no smaller
                         # rung would fare better, so stop the ladder and let the
                         # swap fallback (if any) take over
    ABORT = auto()       # an HTLC may still be in flight: do not retry, and do
                         # NOT fall back to a swap -- draining twice is the one
                         # outcome worth giving up the whole tick to avoid


# Startup window. For this long after a wallet is loaded, the plugin takes NO
# *automatic* action (open / swap / close) and does not judge any peer offline:
# the Lightning layer connects to its peers asynchronously after load, so a peer
# that is actually fine reads as "not connected" during this window, and a
# channel whose peer has only just come up cannot necessarily route a payment
# yet. Acting then would fault a healthy peer, poison its uptime metric, or fire
# a reverse swap whose Lightning leg fails immediately (see the long note in
# ``wallet_readiness_block`` -- shortening this window was tried and measurably
# broke swaps). A user-initiated "Run now" skips it deliberately.
#
# Kept a fixed constant (not a ConfigVar) -- it is infrastructure timing, not a
# strategy knob -- but referenced via ``self._startup_grace_sec`` so tests can
# shrink it. A couple of minutes comfortably covers peer (re)connection without
# noticeably delaying automation. Also bounds the per-peer observation gate in
# ``classify_peer_observation``.
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

# --- automatic-evaluation rate limit --------------------------------------
# Floor on how often *automatic* (event-driven) evaluation may run, and the
# ceiling it backs off to when nothing is happening.
#
# The plugin is event-driven, and events are not paced: a settling wallet, a
# swap's own updates, or a burst of relay traffic can all fire many times a
# second. The 1s debounce coalesces a burst, but it does not bound the *rate* --
# so any steady trickle of events produced back-to-back evaluations, each of
# which opened and tore down a nostr session against nine public relays. With
# nothing in the wallet changing between them, that is pure waste and a good way
# to get rate-limited by a relay.
#
# So: an automatic trigger inside the window does not run. It is not dropped
# either -- a single trailing evaluation is queued for the end of the window
# (``_schedule_trailing_evaluation``), so the trigger is honoured, just later.
# Two deliberate exemptions keep this from costing responsiveness:
#
#   * A *material* change in the wallet (see ``_material_state_sig``) bypasses
#     the window entirely and evaluates at once. Receiving a Lightning payment
#     moves balance from remote to local -- exactly the thing that consumes
#     inbound liquidity -- so the case this plugin exists for is never delayed.
#   * The heartbeat, a manual "Run now", and the post-grace one-shot call
#     ``_evaluate`` directly and are not rate-limited at all. The heartbeat in
#     particular guarantees the time-based watchdogs keep their cadence no
#     matter how deep the backoff has grown.
#
# The backoff itself only ever applies to a tick that changed nothing: identical
# state, no action taken. Each such tick doubles the interval up to
# MAX_AUTO_EVAL_INTERVAL_SEC; any change, any action, or a manual run resets it
# to the floor. Capped at the heartbeat because beyond that the heartbeat is
# running anyway, so a larger value would buy nothing.
# Referenced via ``self._min_auto_eval_interval_sec`` /
# ``self._max_auto_eval_interval_sec`` so tests can shrink them.
MIN_AUTO_EVAL_INTERVAL_SEC = 60.0
MAX_AUTO_EVAL_INTERVAL_SEC = HEARTBEAT_INTERVAL_SEC
AUTO_EVAL_BACKOFF_FACTOR = 2.0

# How long a deferred evaluation waits before looking again when it wakes up to
# find an evaluation already in flight. ``_evaluate`` early-returns on the
# per-wallet lock, so calling it then would silently discard the trigger the
# deferred run is carrying -- and that trigger may be a change the running tick
# snapshotted too early to see. Re-arming instead costs only a timer (no network,
# no wallet read), so a long-running tick just gets looked at again once it lets
# go of the lock. Referenced via ``self._trailing_retry_sec`` so tests can shrink
# it.
TRAILING_EVAL_RETRY_SEC = 5.0

# --- reverse-swap sizing headroom -----------------------------------------
# Slack kept back when sizing a max reverse swap, over and above the routing-fee
# reserve Electrum already accounts for. See ``_channel_sendable_sat`` for the
# full reasoning; in short, a reverse swap is two concurrent payments down one
# channel and Electrum's reserve covers one, leaving a max-sized swap about a
# satoshi short of what it needs. 1% is far larger than the shortfall ever
# observed (single-digit sats plus one HTLC's commitment cost) while costing a
# user with a 0.02 BTC channel roughly 8,000 sat of swap size -- and a swap that
# is 1% smaller is strictly better than one that hangs and faults an innocent
# provider. The floor keeps the slack meaningful on small channels, where a
# percentage would round down to noise.
SWAP_SIZING_HEADROOM_PCT = 1.0
SWAP_SIZING_HEADROOM_MIN_SAT = 1_000

# When opening a nostr swap session, how long to wait for relays to connect, and
# then for the first provider offers to arrive, before proceeding with whatever
# we have. Bounded so an evaluation never hangs on an unreachable network.
OFFER_CONNECT_TIMEOUT_SEC = 10.0
OFFER_DISCOVERY_TIMEOUT_SEC = 12.0

# --- update check ---------------------------------------------------------
# The published-release endpoint for this plugin. GitHub's ``releases/latest``
# excludes drafts and pre-releases, needs no API token for a public repo (the
# unauthenticated limit is 60 requests/hour per IP, against a once-a-day check),
# and carries the release page URL we show the user. Everything about it is
# opt-in; see INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED.
UPDATE_CHECK_URL = (
    "https://api.github.com/repos/BareBits/electrum_liquidity/releases/latest")
UPDATE_RELEASES_URL = "https://github.com/BareBits/electrum_liquidity/releases"
# At most one lookup a day, and a short timeout: this is a background courtesy,
# never something an evaluation should wait on.
UPDATE_CHECK_INTERVAL_SEC = 86_400.0
UPDATE_CHECK_TIMEOUT_SEC = 15.0
# Cap on the response we will read at all, so a hostile/broken endpoint cannot
# make the wallet buffer an unbounded body.
UPDATE_CHECK_MAX_BYTES = 512 * 1024


def _plain_json_copy(value: Any) -> Any:
    """Rebuild ``value`` out of plain dicts/lists, recursively.

    Every ``_load_*`` helper below runs its wallet-db value through this, and
    the reason is not tidiness -- it is the difference between the plugin
    working and a mid-action crash.

    Electrum's ``JsonDB`` does not hand back the data it was given. ``put``
    stores into ``self.data``, a ``StoredDict`` whose ``__setitem__``
    recursively re-wraps nested plain dicts as ``StoredDict``s, each carrying
    ``_db`` (the ``JsonDB``, which owns a ``threading.RLock``), ``_lock`` and
    ``_parent`` back-references. So a store read back with a *shallow* copy
    (``dict(raw)``) looks like data but its values are live db objects, and
    handing one back to ``put`` -- which does ``copy.deepcopy(value)`` -- makes
    deepcopy walk ``StoredDict.__dict__ -> _db -> lock`` and raise
    ``TypeError: cannot pickle '_thread.RLock' object``.

    That is not a storage-layer inconvenience: the exception surfaces at the
    *call site*, so a routine "remember this peer faulted" write aborted the
    channel-open loop mid-candidate and took the whole evaluation down with it
    (observed 2026-07-31: opens stopped after 2 of 10 candidates). It hit only
    the second and later writes -- the first write of a fresh store passes,
    because until then there is nothing db-backed to read back.

    Values are JSON-plain by construction (these stores hold numbers, strings
    and nested dicts/lists of them), so a rebuild is faithful; tuples become
    lists, exactly as a save/load round-trip through the wallet file would.
    """
    if isinstance(value, dict):
        return {k: _plain_json_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_copy(v) for v in value]
    return value


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
# --- tick status ----------------------------------------------------------
# A single evaluation ("tick") can take a while: opening a nostr session waits up
# to OFFER_CONNECT_TIMEOUT_SEC + OFFER_DISCOVERY_TIMEOUT_SEC, a channel open
# walks its candidate list one peer at a time, and a reverse swap is bounded only
# by REVERSE_SWAP_TIMEOUT_SEC. From outside, all of that looks identical to a
# plugin doing nothing at all. These are the *terminal* states -- what the plugin
# shows when no tick is running -- and they are deliberately distinct so "armed
# and idle" never reads the same as "switched off" or "not started yet". The
# in-tick strings are built at each step boundary (see _set_status callers).
STATUS_NOT_STARTED = "not started"
STATUS_SLEEPING = "sleeping"
STATUS_DISABLED = "automation disabled"
STATUS_MANUAL_ONLY = "idle (manual run only)"
# Shown while the wallet is not yet ready (no server connection, still syncing,
# or the Lightning layer is still dialing its peers). Deliberately NOT phrased in
# terms of funds "settling" -- readiness has nothing to do with confirmations or
# unconfirmed balances, and the old wording read as "your coins aren't confirmed
# yet". The specific blocking reason goes to the log (see `_readiness_block`).
STATUS_SETTLING = "warming up"
# Shown when the only thing standing between the plugin and acting is the wallet
# password. Kept distinct from STATUS_SETTLING because it is not transient: no
# amount of waiting clears it, the user has to unlock the wallet, and "warming
# up" forever is exactly the kind of quiet lie the states above exist to avoid.
STATUS_LOCKED = "wallet locked"
# Terminal states, for the GUI (and tests): reaching any of these means the tick
# is over and nothing is in flight.
TERMINAL_STATUSES = frozenset({
    STATUS_NOT_STARTED, STATUS_SLEEPING, STATUS_DISABLED,
    STATUS_MANUAL_ONLY, STATUS_SETTLING, STATUS_LOCKED,
})


def format_duration(seconds: float) -> str:
    """A short, human "1m" / "10m" / "2h" for a wait, rounded up.

    Rounded *up* on purpose: this labels "the next check is no sooner than
    this", and a display that said "0m" while the plugin still had 40 seconds to
    wait would read as a stall.
    """
    secs = max(0, int(math.ceil(seconds)))
    if secs < 60:
        return f"{secs}s"
    minutes = int(math.ceil(secs / 60))
    if minutes < 60:
        return f"{minutes}m"
    return f"{int(math.ceil(minutes / 60))}h"


def sleeping_status(next_check_in_sec: Optional[float]) -> str:
    """The resting status, annotated with when the next automatic check is due.

    "sleeping" alone cannot distinguish "armed and waiting for the next tick"
    from "wedged": both show the same word forever. Naming the wait makes the
    rate limit's backoff visible -- the operator can see the plugin is
    deliberately taking a break, and for how long.
    """
    if next_check_in_sec is None or next_check_in_sec <= 0:
        return STATUS_SLEEPING
    return f"{STATUS_SLEEPING} (next check in ~{format_duration(next_check_in_sec)})"


def is_terminal_status(status: str) -> bool:
    """Whether ``status`` means "the tick is over, nothing is in flight".

    Prefer this over ``status in TERMINAL_STATUSES``: the sleeping state carries
    a variable "next check in ~Xm" suffix (see ``sleeping_status``), so exact
    membership would classify a resting plugin as busy.
    """
    return status in TERMINAL_STATUSES or status.startswith(STATUS_SLEEPING)

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
# Shipped default for the liquidity goal (sat): the channel size the plugin aims
# for. A plugin-opened channel smaller than this is replaced once it is blocking
# a bigger one -- see `_decide_undersized_closes` in the engine for the full gate
# list. Ships ACTIVE (not 0/off), so the rule applies to existing wallets on
# upgrade; the guards that make that safe are the channel-ceiling condition (the
# rule only fires when a small channel is actually in the way) and the swap
# trigger (a channel is only replaceable once the plugin has drained it).
DEFAULT_LIQUIDITY_GOAL_SAT = 100_000

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
# Per-channel wedged-open strike accumulators (see
# liquidity_manager.record_wedge_strike): channel_id(hex) ->
# {"strikes": int, "first_ts": float, "last_ts": float}. Persisted so the strike
# count and the wedge clock survive a restart -- otherwise every restart would
# reset the evidence and a genuinely wedged channel would never be remediated.
WEDGE_STRIKES_DB_KEY = "inbound_liquidity_wedge_strikes"
# Per-channel record of the plugin's attempts to close: channel_id(hex) ->
# {"first_ts": float, "coop_attempts": int, "last_coop_ts": float|None}.
# Drives the "cooperative close always precedes a force-close" invariant (see
# liquidity_manager.coop_before_force_ready) for EVERY close path, so the rule
# cannot be bypassed by adding a new caller.
CLOSE_ATTEMPT_DB_KEY = "inbound_liquidity_close_attempts"

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

# --- wedged channel-open remediation defaults -----------------------------
# How long a channel may look wedged before it is force-closed, measured from
# the FIRST observation that it is wedged -- not from channel creation. A
# funding tx that takes hours to confirm is not wedged and does not start this
# clock (see _wedge_eligible), so the whole budget is available for the part
# that is actually the peer's fault.
#
# Deliberately much larger than DEFAULT_STUCK_OPEN_TIMEOUT_MIN below: un-
# freezing automation is cheap and reversible, while force-closing costs a
# mining fee and locks funds behind a CSV timelock. The two clocks were once a
# single setting, which meant raising the safety margin on the irreversible
# action also stalled the plugin for hours. They are now independent.
DEFAULT_STUCK_OPEN_CLOSE_TIMEOUT_MIN = 360  # 6 hours
# How long a channel whose funding tx is still confirming may block a *further*
# channel open. Generous, because confirmation time is set by the fee market,
# not by us: a fee spike can legitimately delay a low-fee funding tx for days.
DEFAULT_CONFIRMING_FREEZE_DAYS = 3.0


# --- User-configurable settings -------------------------------------------
# All keys live under `plugins.inbound_liquidity.*` and are editable from the
# plugin's settings dialog (the "status menu").
SimpleConfig.INBOUND_LIQUIDITY_AUTOMATION_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.automation_enabled', default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Automation enabled"),
    long_desc=lambda: _("Master switch (the ENABLED/DISABLED slider on the Settings tab). "
                        "Off by default: the plugin loads and can be configured, but takes no "
                        "action — it moves no funds and alters no channels — until enabled."))
SimpleConfig.INBOUND_LIQUIDITY_MANUAL_RUN_ONLY = ConfigVar(
    'plugins.inbound_liquidity.manual_run_only', default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Manual run only"),
    long_desc=lambda: _("When on, the plugin never evaluates on its own — neither on the "
                        "post-load/heartbeat schedule nor in response to wallet activity "
                        "(incoming payments, channel updates, swap events). It acts only when "
                        "you press \"Run now\" on the Settings tab. Lets a cautious user keep the "
                        "master Automation switch armed while withholding all unattended action — "
                        "a way to use the plugin without trusting full automation. The master "
                        "Automation switch must still be enabled for a manual run to move funds."))
SimpleConfig.INBOUND_LIQUIDITY_MIN_ONCHAIN_TO_OPEN_SAT = ConfigVar(
    'plugins.inbound_liquidity.min_onchain_to_open_sat', default=60_000, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Min on-chain to open a channel (sat)"),
    long_desc=lambda: _("Never open a Lightning channel while on-chain spendable funds are below this. "
                        "When this is below Electrum's stock channel-funding floor (MIN_FUNDING_SAT), "
                        "the plugin lowers that floor to this value at startup so smaller channels can "
                        "be opened."))
SimpleConfig.INBOUND_LIQUIDITY_ONCHAIN_RESERVE_SAT = ConfigVar(
    'plugins.inbound_liquidity.onchain_reserve_sat', default=10_000, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("On-chain reserve when opening (sat)"),
    long_desc=lambda: _("When opening a channel, fund with the maximum available, leaving this much on-chain."))
SimpleConfig.INBOUND_LIQUIDITY_MAX_CHANNELS = ConfigVar(
    'plugins.inbound_liquidity.max_channels', default=2, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Maximum number of channels"),
    long_desc=lambda: _("Never hold more than this many channels."))
# Daily action ceilings (rolling 24h) -- a runaway guard that bounds how many
# fund-spending actions the automation can take per day. Edited from the Advanced
# sub-tab. 0 disables a ceiling (unlimited). See DAILY_WINDOW_SEC.
SimpleConfig.INBOUND_LIQUIDITY_MAX_OPENS_PER_DAY = ConfigVar(
    'plugins.inbound_liquidity.max_opens_per_day', default=DEFAULT_MAX_OPENS_PER_DAY,
    type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Max channel opens per day"),
    long_desc=lambda: _("Never open more than this many Lightning channels in any rolling "
                        "24-hour window, to bound a runaway automation loop. 0 = unlimited."))
SimpleConfig.INBOUND_LIQUIDITY_MAX_CLOSES_PER_DAY = ConfigVar(
    'plugins.inbound_liquidity.max_closes_per_day', default=DEFAULT_MAX_CLOSES_PER_DAY,
    type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Max channel closes per day"),
    long_desc=lambda: _("Never close more than this many channels in any rolling 24-hour "
                        "window. This includes the emergency force-close of a channel whose "
                        "open got wedged; once the ceiling is reached, a wedged open is not "
                        "auto-freed until the window rolls over. 0 = unlimited."))
SimpleConfig.INBOUND_LIQUIDITY_GOAL_SAT = ConfigVar(
    'plugins.inbound_liquidity.liquidity_goal_sat', default=DEFAULT_LIQUIDITY_GOAL_SAT,
    type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Liquidity goal"),
    long_desc=lambda: _("Automatically close small channels and re-open bigger channels "
                        "if we have sufficient funds to reach this goal"))
SimpleConfig.INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT = ConfigVar(
    'plugins.inbound_liquidity.max_swap_fee_pct', default=0.9, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Max fee to move LN → on-chain (%, all-in)"),
    long_desc=lambda: _("Do not swap Lightning -> on-chain if the effective all-in cost "
                        "(percentage fee + provider mining fee + on-chain claim fee, as a "
                        "share of the amount) exceeds this."))
SimpleConfig.INBOUND_LIQUIDITY_DEV_FEE_PCT = ConfigVar(
    'plugins.inbound_liquidity.dev_fee_pct', default=0.1, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Dev fee (% of amount drained)"),
    long_desc=lambda: _("Optional fee to support plugin development, charged on what the plugin "
                        "drains from a channel: the on-chain amount received from a reverse "
                        "swap, or the amount paid to your liquidity sink. Accrues until "
                        "at least {thr} sat is owed, then is paid automatically to the payout "
                        "Lightning address (capped at {cap} sat/day). Range 0-{max}%; 0 disables "
                        "it.").format(thr=DEV_FEE_PAYOUT_THRESHOLD_SAT,
                                      cap=DEV_FEE_DAILY_CAP_SAT, max=int(DEV_FEE_MAX_PCT)))
SimpleConfig.INBOUND_LIQUIDITY_DEV_FEE_ADDRESS = ConfigVar(
    'plugins.inbound_liquidity.dev_fee_address', default='electrum_liqhelper@getbarebits.com',
    type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Dev fee payout address"),
    long_desc=lambda: _("Lightning address (user@domain) or LNURL-pay the accrued dev fee is "
                        "paid to. Leave at the default to support development of this plugin."))
SimpleConfig.INBOUND_LIQUIDITY_SINK_ADDRESS = ConfigVar(
    'plugins.inbound_liquidity.sink_address', default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Liquidity sink (Lightning address)"),
    long_desc=lambda: _("Optional. A Lightning address (user@domain), LNURL-pay string or "
                        "LNURL-pay URL to send a channel's outbound balance to instead of "
                        "reverse-swapping it on-chain. Paying it frees exactly the same "
                        "inbound capacity, but settles in seconds with no on-chain claim "
                        "and no swap provider -- typically pointed at your own account "
                        "elsewhere. A payment that fails is retried at half the amount, "
                        "repeatedly, down to {floor} sat. If every attempt fails, a reverse "
                        "swap is used instead unless you disable submarine swaps. Leave "
                        "empty to reverse-swap as before.").format(floor=SINK_MIN_PAYMENT_SAT))
SimpleConfig.INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL = ConfigVar(
    'plugins.inbound_liquidity.defer_sink_until_goal', default=True, type_=bool,
    plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Don't use liquidity sink until inbound liquidity goal is met"),
    long_desc=lambda: _("On by default. A sink payment sends a channel's outbound to an "
                        "account outside this wallet, so while the wallet is still "
                        "building its channels it starves the on-chain balance those "
                        "opens need. With this on, a channel over its trigger is "
                        "reverse-swapped instead -- which brings the same balance back "
                        "as coins that can fund a bigger channel -- and the sink takes "
                        "over once the goal is met: the maximum number of channels is "
                        "held, and none of the ones the plugin opened is below the "
                        "liquidity goal. Note that with submarine swaps also disabled, "
                        "nothing drains a channel until the goal is met; the plugin says "
                        "so in its decision log rather than pay your outbound away "
                        "against your wishes."))
SimpleConfig.INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS = ConfigVar(
    'plugins.inbound_liquidity.disable_submarine_swaps', default=False, type_=bool,
    plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Disable submarine swaps (liquidity sink only)"),
    long_desc=lambda: _("When on, outbound is only ever drained by paying the liquidity "
                        "sink -- a reverse swap is never made, not even when the sink "
                        "fails. Note that turning this on WITHOUT a liquidity sink leaves "
                        "nothing able to drain a channel: the plugin will say so in its "
                        "decision log rather than swap against your wishes."))
SimpleConfig.INBOUND_LIQUIDITY_SWAP_TRIGGER_PCT = ConfigVar(
    'plugins.inbound_liquidity.swap_trigger_pct', default=25.0, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Swap-out trigger (% of capacity)"),
    long_desc=lambda: _("Reverse-swap a channel once its local balance reaches this share of its capacity."))
SimpleConfig.INBOUND_LIQUIDITY_SWAP_TRIGGER_SAT = ConfigVar(
    'plugins.inbound_liquidity.swap_trigger_sat', default=25_000, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Swap-out trigger (sat)"),
    long_desc=lambda: _("...or once a channel's local balance exceeds this many sats (whichever comes first)."))
# Outbound-liquidity preservation. By default the plugin drains all outbound to
# manufacture maximum inbound; these two knobs let a user hold some back.
SimpleConfig.INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT = ConfigVar(
    'plugins.inbound_liquidity.min_outbound_sat', default=0, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Keep outbound per channel (sat)"),
    long_desc=lambda: _("Never let a reverse swap drain a channel's outbound (local) "
                        "balance below this, so the wallet keeps some ability to send. "
                        "Applied per channel to the swappable amount; 0 (the default) "
                        "drains everything for maximum inbound liquidity."))
SimpleConfig.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY = ConfigVar(
    'plugins.inbound_liquidity.manage_plugin_opened_only', default=True, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Only manage channels the plugin opened"),
    long_desc=lambda: _("When on (the default), the plugin only reverse-swaps channels "
                        "it opened itself; channels you opened by hand are left entirely "
                        "alone (their outbound is never drained). When off, every channel "
                        "is managed. On by default so the plugin never touches a channel "
                        "you set up for your own purposes without being asked."))
SimpleConfig.INBOUND_LIQUIDITY_CHANNEL_PEER = ConfigVar(
    'plugins.inbound_liquidity.channel_peer', default='', type_=str, plugin=_PLUGIN_NAME,
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
    'plugins.inbound_liquidity.preferred_partners', default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Preferred channel partners"),
    long_desc=lambda: _("Lightning nodes (node_id@host:port) to try opening channels to first, "
                        "in order, before falling back to Electrum's suggested peer."))
SimpleConfig.INBOUND_LIQUIDITY_BANNED_PARTNERS = ConfigVar(
    'plugins.inbound_liquidity.banned_partners', default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Banned channel partners"),
    long_desc=lambda: _("Lightning nodes (by node id) never opened to, even if Electrum suggests them."))
SimpleConfig.INBOUND_LIQUIDITY_PARTNERS_STRICT = ConfigVar(
    'plugins.inbound_liquidity.partners_strict', default=False, type_=bool, plugin=_PLUGIN_NAME,
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
    'plugins.inbound_liquidity.one_channel_per_peer', default=True, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Only one channel per peer"),
    long_desc=lambda: _("When set, the plugin will not open a second channel to a node it already "
                        "has a channel with (its automated opens go to new peers instead)."))
SimpleConfig.INBOUND_LIQUIDITY_LOG_RETENTION_DAYS = ConfigVar(
    'plugins.inbound_liquidity.log_retention_days', default=DEFAULT_LOG_RETENTION_DAYS,
    type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Keep decision log for (days)"),
    long_desc=lambda: _("Decision-log entries older than this are pruned. "
                        "Default 30 days; up to 999."))
# Optional on-disk diagnostic log (OFF by default). When enabled, every decision
# already written to the in-wallet decision log -- plus operational errors -- is
# also appended to per-wallet daily JSON-lines files under the Electrum data dir
# (rotated daily, kept DIAG_LOG_RETENTION_DAYS). It carries no more than the GUI
# log: ids are abbreviated and no key material is ever written. See diag_log.py.
SimpleConfig.INBOUND_LIQUIDITY_DIAG_LOG_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.diag_log_enabled', default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Write diagnostic log files"),
    long_desc=lambda: _("When set, the plugin writes a diagnostic log of its decisions and "
                        "errors to daily text files (one per wallet, kept 30 days) under the "
                        "Electrum data directory. Contains no private keys or seeds. Off by default."))
# --- live log capture (the "Log" sub-tab) ---------------------------------
# The plugin's own logging is always captured into a bounded in-memory ring; the
# two toggles below only widen what is captured. They are separate on purpose:
# WHICH loggers to record and WHETHER to force DEBUG are independent questions,
# and the second one has a side effect the first does not (see below).
SimpleConfig.INBOUND_LIQUIDITY_LOG_BUFFER_LINES = ConfigVar(
    'plugins.inbound_liquidity.log_buffer_lines', default=DEFAULT_LOG_BUFFER_LINES,
    type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Log tab buffer (lines)"),
    long_desc=lambda: _("How many recent log lines the Log tab keeps in memory. Older lines are "
                        "discarded. The buffer is never written to disk — use the Log tab's "
                        "\"Save to file…\" button to keep a copy."))
SimpleConfig.INBOUND_LIQUIDITY_LOG_CAPTURE_LN = ConfigVar(
    'plugins.inbound_liquidity.log_capture_ln', default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Also capture Electrum Lightning logs"),
    long_desc=lambda: _("Include Electrum's own Lightning and swap logging (peer manager, node "
                        "rater, channels, routing, submarine swaps) in the Log tab. Useful when "
                        "the plugin reports that no channel partner is available — that decision "
                        "is made inside those subsystems. Noisy; off by default."))
SimpleConfig.INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG = ConfigVar(
    'plugins.inbound_liquidity.log_capture_debug', default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Capture debug-level logs"),
    long_desc=lambda: _("Force debug-level logging while the Log tab is open. Electrum normally "
                        "produces debug records already (they are just hidden), so this only "
                        "matters if you started Electrum with a reduced verbosity — in which case "
                        "turning it on also makes those records appear in Electrum's own log file. "
                        "The previous setting is restored when you turn it off."))
# Provider selection. The plugin discovers swap providers on nostr and uses the
# cheapest one per swap; these two lists (comma-separated npubs) constrain that
# choice. They are edited from the Providers sub-tab, not free-form normally.
SimpleConfig.INBOUND_LIQUIDITY_PREFERRED_NPUBS = ConfigVar(
    'plugins.inbound_liquidity.preferred_npubs', default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Preferred swap providers (npubs)"),
    long_desc=lambda: _("If non-empty, ONLY these providers are ever used (the cheapest "
                        "among them). If none of them are currently available, no swap is made."))
SimpleConfig.INBOUND_LIQUIDITY_BANNED_NPUBS = ConfigVar(
    'plugins.inbound_liquidity.banned_npubs', default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Banned swap providers (npubs)"),
    long_desc=lambda: _("Providers that are never used, even if they are the cheapest."))
# Reliability tracking. The plugin remembers each provider's recent faults
# (unreachable / RPC errors / stuck swaps) and adds a decaying penalty to that
# provider's cost *for ranking only* -- a flaky provider sinks behind reliable
# ones (soft de-prioritisation) but is still used when it is the only option.
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.reliability_enabled', default=True, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Track provider reliability"),
    long_desc=lambda: _("Remember providers that time out, return errors, or leave swaps stuck, "
                        "and prefer more reliable providers over them."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_BASE_PENALTY_PCT = ConfigVar(
    'plugins.inbound_liquidity.reliability_base_penalty_pct', default=0.5, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Reliability penalty per fault (%)"),
    long_desc=lambda: _("Cost penalty (percentage points) added to a provider's ranking after one "
                        "fault; it doubles for each consecutive fault and decays over time."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_PENALTY_CAP_PCT = ConfigVar(
    'plugins.inbound_liquidity.reliability_penalty_cap_pct', default=5.0, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Max reliability penalty (%)"),
    long_desc=lambda: _("Upper bound on the ranking penalty, so even a chronically failing "
                        "provider is only de-prioritised, never excluded outright."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_HALFLIFE_HOURS = ConfigVar(
    'plugins.inbound_liquidity.reliability_halflife_hours', default=6.0, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Reliability recovery half-life (hours)"),
    long_desc=lambda: _("How fast a provider's penalty fades after its last fault: it halves every "
                        "this many hours, so a provider that stops failing recovers automatically."))
SimpleConfig.INBOUND_LIQUIDITY_RELIABILITY_STUCK_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.reliability_stuck_timeout_min', default=60, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Stuck-swap timeout (minutes)"),
    long_desc=lambda: _("If a reverse swap is accepted but no on-chain funding appears within this "
                        "long, count it as a fault against the provider that accepted it."))
# Channel-peer reliability. Distinct from provider reliability above: this tracks
# the Lightning nodes we hold channels with. It reuses the same decaying-penalty
# tuning (base / cap / half-life) but applies it to the channel-partner try-order,
# and adds an auto-ban after enough *hard* faults (force-close / repeated open
# failure) plus a watchdog that force-closes a channel wedged opening too long.
SimpleConfig.INBOUND_LIQUIDITY_PEER_RELIABILITY_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.peer_reliability_enabled', default=True, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Track channel-peer reliability"),
    long_desc=lambda: _("Remember channel peers that fail to open, go offline, or force-close, and "
                        "prefer more reliable peers when opening new channels."))
SimpleConfig.INBOUND_LIQUIDITY_PEER_AUTOBAN_FAULTS = ConfigVar(
    'plugins.inbound_liquidity.peer_autoban_faults', default=3, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Auto-ban a peer after N hard faults"),
    long_desc=lambda: _("After this many hard faults (a force-close, or repeated channel-open "
                        "failures), a peer is added to the banned list automatically. 0 disables "
                        "auto-banning (peers are only de-prioritised, never banned)."))
SimpleConfig.INBOUND_LIQUIDITY_STUCK_OPEN_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.stuck_open_timeout_min', default=60, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Stuck channel-open timeout (minutes)"),
    long_desc=lambda: _("A channel that has been opening (not yet usable) for longer than this is "
                        "treated as wedged by an unresponsive peer and stops freezing automation. "
                        "This only un-freezes the plugin; force-closing such a channel is governed "
                        "separately (and much more conservatively) by the setting below."))
SimpleConfig.INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.stuck_open_close_timeout_min',
    default=DEFAULT_STUCK_OPEN_CLOSE_TIMEOUT_MIN, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Force-close a wedged open after (minutes)"),
    long_desc=lambda: _("How long a channel open must look wedged before it is force-closed, timed "
                        "from the first moment it looked wedged rather than from when it was "
                        "created. Time spent waiting for the funding transaction to confirm does "
                        "not count. The channel must also look wedged on several separate checks "
                        "spread over time, and a cooperative close is always attempted first."))
SimpleConfig.INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS = ConfigVar(
    'plugins.inbound_liquidity.wedge_required_checks',
    default=WEDGE_REQUIRED_STRIKES, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Wedged-open confirmations required"),
    long_desc=lambda: _("How many separate checks must all find a channel open wedged before it is "
                        "closed. Evaluation runs can happen in quick bursts, so these checks must "
                        "also be spread over the interval below."))
SimpleConfig.INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN = ConfigVar(
    'plugins.inbound_liquidity.wedge_check_spread_min',
    default=int(WEDGE_MIN_SPREAD_SEC // 60), type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Wedged-open checks must span (minutes)"),
    long_desc=lambda: _("Minimum time between the first and last check that found a channel open "
                        "wedged. Stops a burst of evaluations in quick succession from counting as "
                        "independent evidence."))
SimpleConfig.INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN = ConfigVar(
    'plugins.inbound_liquidity.coop_before_force_min',
    default=int(COOP_BEFORE_FORCE_WINDOW_SEC // 60), type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Wait for a cooperative close (minutes)"),
    long_desc=lambda: _("How long a cooperative close gets to complete before the plugin escalates "
                        "to a force-close, and how long an unreachable peer is given to come back "
                        "before we accept that cooperating with it is impossible. The plugin never "
                        "force-closes without first trying, or ruling out, a cooperative close."))
SimpleConfig.INBOUND_LIQUIDITY_CONFIRMING_FREEZE_DAYS = ConfigVar(
    'plugins.inbound_liquidity.confirming_freeze_days',
    default=DEFAULT_CONFIRMING_FREEZE_DAYS, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Confirming open blocks new opens for (days)"),
    long_desc=lambda: _("While a channel's funding transaction is still confirming, the plugin will "
                        "not open another channel, for up to this long. Reverse swaps on other "
                        "channels are never blocked by a confirming open. Generous by default, "
                        "because a fee spike can delay confirmation for days."))
SimpleConfig.INBOUND_LIQUIDITY_STUCK_SWAP_TIMEOUT_MIN = ConfigVar(
    'plugins.inbound_liquidity.stuck_swap_timeout_min', default=180, type_=int, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Stuck reverse-swap timeout (minutes)"),
    long_desc=lambda: _("A reverse swap whose funding has been broadcast but not swept for longer "
                        "than this is treated as wedged: it stops freezing automation so channel "
                        "opens and other swaps can resume. The swap is still tracked and its "
                        "provider still faulted separately. Give on-chain funding ample time to "
                        "confirm before escaping (default 3 hours)."))
SimpleConfig.INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN = ConfigVar(
    'plugins.inbound_liquidity.auto_remediate_stuck_open', default=True, type_=bool, plugin=_PLUGIN_NAME,
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
    default=DEFAULT_OFFLINE_AUTOCLOSE_ENABLED, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Auto-close channels whose peer stays offline"),
    long_desc=lambda: _("For channels this plugin opened, when the peer has been effectively offline "
                        "for a sustained period, try to close the channel cooperatively, and "
                        "force-close it if it still hasn't closed after the force-close deadline."))
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_UPTIME_WINDOW_DAYS = ConfigVar(
    'plugins.inbound_liquidity.offline_uptime_window_days',
    default=DEFAULT_OFFLINE_UPTIME_WINDOW_DAYS, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Peer-uptime window (days)"),
    long_desc=lambda: _("How far back the peer's uptime is measured when deciding whether it is gone. "
                        "A peer whose uptime over this window falls below the minimum is treated as "
                        "offline and the channel is closed."))
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_MIN_UPTIME_PCT = ConfigVar(
    'plugins.inbound_liquidity.offline_min_uptime_pct',
    default=DEFAULT_OFFLINE_MIN_UPTIME_PCT, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Minimum peer uptime (%)"),
    long_desc=lambda: _("If a channel peer is reachable less than this percent of the uptime window, "
                        "the peer is considered gone and the channel is closed. Lower = more tolerant "
                        "of a flaky peer before closing."))
SimpleConfig.INBOUND_LIQUIDITY_OFFLINE_FORCE_CLOSE_DAYS = ConfigVar(
    'plugins.inbound_liquidity.offline_force_close_days',
    default=DEFAULT_OFFLINE_FORCE_CLOSE_DAYS, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Force-close after trying to close for (days)"),
    long_desc=lambda: _("Once the plugin has been trying to close an offline channel for this many "
                        "days without success (the peer never agreed / stayed offline), it force-closes "
                        "the channel. This broadcasts a transaction, incurs a mining fee, and timelocks "
                        "your funds. Counts against the daily channel-close ceiling."))
# --- update check ---------------------------------------------------------
# Off until the user says otherwise. This is the plugin's only contact with a
# server that is not part of running the wallet, and a periodic request to
# github.com discloses this wallet's IP and a usage pattern to a third party --
# so it is opt-in, asked once via a dialog on first run (see qt.py), and the
# request goes through Electrum's configured proxy like every other HTTP call
# the wallet makes. A user who never answers, or who runs headless where there
# is no dialog, simply never checks.
SimpleConfig.INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED = ConfigVar(
    'plugins.inbound_liquidity.update_check_enabled',
    default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Check GitHub for plugin updates"),
    long_desc=lambda: _("Once a day, ask GitHub whether a newer release of this plugin has been "
                        "published, and show it in the plugin's status. This contacts github.com "
                        "(through your configured proxy, if any) and therefore reveals to GitHub "
                        "that this wallet is running. Nothing is ever downloaded or installed: "
                        "updating stays a manual step you take yourself."))
# Whether the opt-in question has been put to the user yet. Separate from the
# setting itself so "declined" and "not asked" stay distinguishable -- otherwise
# the dialog would reappear for a user who has already said no.
SimpleConfig.INBOUND_LIQUIDITY_UPDATE_CHECK_PROMPTED = ConfigVar(
    'plugins.inbound_liquidity.update_check_prompted',
    default=False, type_=bool, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Update-check opt-in has been offered"))
# Cache of the last successful lookup, so the answer survives a restart and the
# check runs at most once a day rather than once a tick.
SimpleConfig.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS = ConfigVar(
    'plugins.inbound_liquidity.update_last_check_ts',
    default=0.0, type_=float, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Last update check (epoch seconds)"))
SimpleConfig.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = ConfigVar(
    'plugins.inbound_liquidity.update_latest_version',
    default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Latest release seen on GitHub"))
SimpleConfig.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = ConfigVar(
    'plugins.inbound_liquidity.update_latest_url',
    default='', type_=str, plugin=_PLUGIN_NAME,
    short_desc=lambda: _("Release page for the latest release seen"))


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
        # wallet -> why the last suggested-peer lookup failed ("TypeName: msg"), or
        # absent if it succeeded. Asking Electrum for a peer suggestion can fail
        # outright (no trampoline nodes on this network, no rater yet, ...); that is
        # a different situation from "asked, got nothing", so it is remembered here
        # and named in the decline reason instead of vanishing into a bare except.
        self._suggest_errors: Dict['Abstract_Wallet', str] = {}
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
        # the startup window: until STARTUP_GRACE_SEC has elapsed the plugin
        # defers all automatic evaluation and treats not-yet-connected peers as
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
        self._sink_payment_timeout_sec: float = SINK_PAYMENT_TIMEOUT_SEC
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
        # --- automatic-evaluation rate limit (see MIN_AUTO_EVAL_INTERVAL_SEC) ---
        # wallet -> monotonic time of the last evaluation that actually ran past
        # the gates. The rate limit is measured from here, so a run triggered by
        # any path (event, heartbeat, manual) delays the next automatic one.
        self._last_eval_at: Dict['Abstract_Wallet', float] = {}
        # wallet -> the current minimum spacing between automatic evaluations.
        # Starts at the floor and doubles for every tick that changed nothing.
        self._auto_eval_interval: Dict['Abstract_Wallet', float] = {}
        # wallet -> the material state signature (see _material_state_sig) as of
        # the last evaluation, so the next one can tell "something happened" from
        # "identical to last time" and bypass / apply the rate limit accordingly.
        self._last_state_sig: Dict['Abstract_Wallet', tuple] = {}
        # wallet -> the queued trailing evaluation (an asyncio.Task), i.e. the one
        # run that an in-window trigger is deferred to. At most one per wallet:
        # further triggers inside the same window coalesce onto it.
        self._trailing_eval_tasks: Dict['Abstract_Wallet', "asyncio.Task"] = {}
        # Rate-limit bounds as instance attributes so tests can shrink them (the
        # module constants stay the production defaults).
        self._min_auto_eval_interval_sec: float = MIN_AUTO_EVAL_INTERVAL_SEC
        self._max_auto_eval_interval_sec: float = MAX_AUTO_EVAL_INTERVAL_SEC
        self._trailing_retry_sec: float = TRAILING_EVAL_RETRY_SEC
        # How many nostr swap sessions this plugin currently has open. Nonzero
        # means any `swap_offers_changed` we see is almost certainly our own
        # discovery echoing back, so it must not schedule an evaluation (see
        # _OFFER_TRIGGER_EVENTS). A counter rather than a flag because sessions
        # can overlap across wallets, and a plain flag would be cleared by
        # whichever session closed first.
        self._open_swap_sessions: int = 0
        # wallet -> what the current tick is doing right now (or a terminal state
        # when no tick is running). Surfaced by the Settings tab's Status section.
        self._tick_status: Dict['Abstract_Wallet', str] = {}
        # Live log capture backing the GUI's Log tab. Created here (not per
        # wallet) because Python logging is process-global: one bounded ring and
        # one handler serve every wallet this plugin instance manages.
        self.log_buffer = LogRingBuffer(
            clamp_max_lines(getattr(config, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES",
                                    DEFAULT_LOG_BUFFER_LINES)))
        self.log_capture = LogCapture(self.log_buffer)
        self.apply_log_capture_settings()
        util.register_callback(self._on_wallet_event, _TRIGGER_EVENTS)
        util.register_callback(self._on_offers_event, _OFFER_TRIGGER_EVENTS)

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
        self._set_status(wallet, STATUS_SETTLING)
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
        # Opt-in update check. Deliberately outside the automation/readiness
        # gates: whether a newer plugin exists has nothing to do with whether
        # this wallet is armed, synced, or funded. A no-op unless enabled.
        self._request_update_check(wallet)

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
                # Self-throttled to once a day; this is just the clock that gets
                # it re-tried on a long-running wallet.
                try:
                    await self._maybe_check_for_update(wallet)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    self.logger.info(f"update check tick error: {e!r}")
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
        "_eval_pending", "_last_decline_sigs", "_last_offers", "_suggest_errors",
        "_remediating_opens", "_tick_status",
        "_local_closes", "_wedged_faulted", "_close_capped_logged",
        "_known_chan_states", "_coop_closing", "_dev_fee_paying", "_dev_fee_retry_until",
        "_started_at", "_peer_seen_online", "_swap_freeze_escaped_logged",
        "_heartbeat_tasks",
        "_last_eval_at", "_auto_eval_interval", "_last_state_sig",
        "_trailing_eval_tasks",
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
        # Same for a deferred evaluation still waiting out the rate-limit window:
        # it would otherwise wake up and evaluate a wallet we no longer manage.
        # (It re-checks `self.wallets` on waking too -- belt and braces, because
        # this cancel may have to cross threads. See _cancel_trailing_evaluation.)
        self._cancel_trailing_evaluation(wallet)
        self._forget_wallet(wallet)

    def _per_wallet_store(self, name: str) -> dict:
        """The named per-wallet dict, created on first use if it is missing.

        Mirrors the tolerance ``_set_status`` / ``tick_status`` already have, and
        for the same reason: the glue test harnesses build a plugin via
        ``object.__new__`` without ``BasePlugin.__init__``, so bookkeeping that
        assumed its dict exists would ``AttributeError`` into the middle of an
        evaluation. Used by the rate-limit bookkeeping, which is an optimisation
        and must never be the thing that breaks a tick.
        """
        store = getattr(self, name, None)
        if not isinstance(store, dict):
            store = {}
            setattr(self, name, store)
        return store

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
        trailing_tasks = self._per_wallet_store("_trailing_eval_tasks")
        for trailing in list(trailing_tasks.values()):
            try:
                trailing.cancel()
            except Exception:
                pass
        trailing_tasks.clear()
        for callback in (self._on_wallet_event, self._on_offers_event):
            try:
                util.unregister_callback(callback)
            except Exception as e:
                self.logger.info(f"could not unregister event callback: {e!r}")
        # Remove our logging handler and undo any level override, so a disabled
        # plugin stops capturing and hands the user's verbosity back. Done last:
        # the lines above are worth capturing.
        try:
            self.log_capture.detach()
        except Exception:
            pass

    # --- tick status ------------------------------------------------------
    def tick_status(self, wallet: 'Abstract_Wallet') -> str:
        """What this wallet's evaluation is doing right now, or a terminal state
        when nothing is running. Read by the Settings tab's Status section."""
        store = getattr(self, "_tick_status", None)
        if not isinstance(store, dict):
            return STATUS_NOT_STARTED
        return store.get(wallet, STATUS_NOT_STARTED)

    def _set_status(self, wallet: 'Abstract_Wallet', status: str) -> None:
        """Record the current tick step and notify the GUI.

        Also logged (at INFO) so the Log tab doubles as a tick trace: the status
        line only ever shows the *current* step, but the log keeps the sequence
        that led to it, which is what you actually need when a tick stalls.
        Statuses are only stored for wallets we still manage, so a step that
        lands after ``stop_wallet`` cannot resurrect state we just dropped.

        Purely a display concern, so it is defensive on both ends: it tolerates a
        partially-constructed plugin (the unit-test harnesses build one without
        ``BasePlugin.__init__``) and swallows a failing GUI notification. A
        status update must never be able to break the action it is describing.
        """
        store = getattr(self, "_tick_status", None)
        if not isinstance(store, dict):
            return  # not fully constructed; nothing is displaying a status
        if store.get(wallet) == status:
            return  # unchanged; don't re-log or re-notify
        if wallet in getattr(self, "wallets", {}):
            store[wallet] = status
        self.logger.info(f"status: {status}")
        try:
            self.on_status_changed(wallet, status)
        except Exception as e:
            self.logger.info(f"status notification failed: {e!r}")

    def on_status_changed(self, wallet: 'Abstract_Wallet', status: str) -> None:
        """GUI hook: overridden in qt.py to push the status onto the GUI thread.
        No-op in the base/headless plugin."""

    def _terminal_status(self, wallet: 'Abstract_Wallet', config: Optional[LiquidityConfig],
                         *, manual: bool) -> str:
        """Which resting state to show now that a tick has finished.

        Distinguishing these matters: "sleeping" on a plugin whose master switch
        is off would be a lie, and "sleeping" during the startup grace would hide
        the fact that automation is deliberately deferred for a couple of minutes.
        """
        if config is not None and not config.automation_enabled:
            return STATUS_DISABLED
        if not manual and self._manual_run_only():
            return STATUS_MANUAL_ONLY
        # `manual` is threaded through so a "Run now" that actually ran does not
        # come to rest on "warming up" -- the state it would have been blocked by
        # but was deliberately allowed to skip. A later automatic tick that really
        # is still deferred will set it back, so the display stays honest.
        block = self._readiness_block(wallet, manual=manual)
        if block == BLOCK_LOCKED:
            # Not a transient state -- waiting will not clear it, so say what it
            # is rather than resting on "warming up" indefinitely.
            return STATUS_LOCKED
        if block is not None:
            return STATUS_SETTLING
        # Armed, ready, nothing in flight -- so say when we will next look. The
        # wait can be zero (the window has already elapsed, or this is the first
        # tick), in which case this reads as the plain "sleeping" as before.
        return sleeping_status(self._auto_eval_wait_sec(wallet))

    # --- version + update check -------------------------------------------
    # Opt-in, once a day, read-only: ask GitHub for the latest published release
    # and say so if this install is behind. Nothing is downloaded and nothing is
    # installed -- a plugin that can rewrite its own code turns a compromised
    # GitHub account or a MITM into code execution against a wallet holding
    # funds, so the result is a version string and a link the user acts on.
    def plugin_version(self) -> str:
        """The running plugin's version, read from ``manifest.json``.

        ``pkgutil.get_data`` rather than ``open()``: under an external ZIP
        install this package lives inside the archive, where a filesystem read
        of ``manifest.json`` fails. Cached because it cannot change while the
        process runs, and "" when it cannot be read at all -- an unknown running
        version simply disables the comparison instead of guessing.
        """
        cached = getattr(self, "_plugin_version_cache", None)
        if cached is not None:
            return cached
        version = ""
        try:
            raw = pkgutil.get_data(__package__ or __name__, "manifest.json")
            if raw:
                version = str(json.loads(raw.decode("utf-8")).get("version") or "")
        except Exception as e:
            self.logger.info(f"could not read the plugin version from manifest: {e!r}")
        self._plugin_version_cache = version
        return version

    def latest_known_version(self) -> str:
        """The newest release seen by the last successful check (cached in the
        config, so it survives a restart and is available before the first
        check of a session completes). "" if we have never looked."""
        return str(getattr(self.config, "INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION", "") or "")

    def latest_release_url(self) -> str:
        return (str(getattr(self.config, "INBOUND_LIQUIDITY_UPDATE_LATEST_URL", "") or "")
                or UPDATE_RELEASES_URL)

    def update_available(self) -> bool:
        return is_newer_version(self.latest_known_version(), self.plugin_version())

    def update_status(self) -> str:
        """One line for the Status footer / headless caller: what version is
        running, and whether something newer is published. "" when the check is
        switched off, so an opted-out user sees nothing about it anywhere."""
        if not bool(getattr(self.config, "INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED", False)):
            return ""
        running = self.plugin_version() or "unknown"
        if self.update_available():
            return (f"update available: {self.latest_known_version()} "
                    f"(running {running})")
        return f"version {running} (up to date)"

    def _request_update_check(self, wallet: 'Abstract_Wallet') -> None:
        """Fire the (self-throttling) update check on the network's asyncio loop.

        Fire-and-forget on purpose: this is a courtesy lookup against a third
        party, and neither wallet startup nor a heartbeat tick may wait on it or
        fail because of it."""
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._maybe_check_for_update(wallet), loop)
        except Exception as e:
            self.logger.info(f"could not schedule the update check: {e!r}")

    async def _maybe_check_for_update(self, wallet: 'Abstract_Wallet') -> None:
        """Check for a newer release, at most once per UPDATE_CHECK_INTERVAL_SEC.

        The timestamp is stamped BEFORE the request, so an endpoint that is down
        (or rate-limiting us) is retried tomorrow rather than on every heartbeat.
        """
        if not bool(getattr(self.config, "INBOUND_LIQUIDITY_UPDATE_CHECK_ENABLED", False)):
            return
        now = time.time()
        last = float(getattr(self.config, "INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS", 0.0) or 0.0)
        if 0.0 <= now - last < UPDATE_CHECK_INTERVAL_SEC:
            return
        self.config.INBOUND_LIQUIDITY_UPDATE_LAST_CHECK_TS = now
        release = await self._fetch_latest_release(wallet)
        if release is None:
            return
        self.config.INBOUND_LIQUIDITY_UPDATE_LATEST_VERSION = release.version
        self.config.INBOUND_LIQUIDITY_UPDATE_LATEST_URL = release.url or UPDATE_RELEASES_URL
        running = self.plugin_version()
        if not is_newer_version(release.version, running):
            self.logger.info(
                f"plugin update check: running {running or 'unknown'}, "
                f"latest published {release.version} -- up to date")
            return
        # Log the availability once per version per session: the Status footer
        # keeps showing it, so repeating it every day would only be noise.
        seen = getattr(self, "_update_logged", None)
        if not isinstance(seen, set):
            seen = set()
            self._update_logged = seen
        message = (f"a newer version of this plugin is available: {release.version} "
                   f"(running {running or 'unknown'}) -- {self.latest_release_url()}")
        if release.version not in seen:
            seen.add(release.version)
            self.logger.warning(message)
            self._diag_event(wallet, category="lifecycle", kind="update",
                             reason="plugin update available",
                             detail=message)
        # Nudge the Status footer to re-read (the update line lives there).
        # Guarded like `_set_status`: a GUI notification must never be able to
        # break the thing it is notifying about.
        try:
            self.on_status_changed(wallet, self.tick_status(wallet))
        except Exception as e:
            self.logger.info(f"status notification failed: {e!r}")

    @staticmethod
    async def _read_capped(stream, limit: int) -> bytes:
        """Read up to ``limit`` bytes from ``stream``, to EOF.

        NOT the same as ``await stream.read(limit)``, and the difference was a
        real bug: aiohttp's ``StreamReader.read(n)`` returns up to n bytes --
        whatever happens to be buffered -- so on a chunked response it hands
        back the FIRST CHUNK, not the body. The update check then fed a
        truncated document to ``json.loads`` and failed with a
        JSONDecodeError whose position was simply wherever the chunk ended.
        Intermittent by nature: it depended entirely on how the response was
        framed on the wire.

        Looping to EOF fixes that while keeping the read cap, which exists so a
        hostile or broken endpoint cannot make the wallet buffer an unbounded
        body. Returning ``limit`` bytes is the caller's signal that the cap was
        hit (it asks for one more than it will accept).
        """
        chunks: List[bytes] = []
        remaining = limit
        while remaining > 0:
            chunk = await stream.read(min(remaining, 64 * 1024))
            if not chunk:
                break          # EOF: the whole body is in hand
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    async def _fetch_latest_release(self, wallet: 'Abstract_Wallet') -> Optional[ReleaseInfo]:
        """GET the latest published release, or None on any failure.

        Routed through ``make_aiohttp_session`` so it honours the wallet's proxy
        (a Tor user's update check must not be the one request that leaks their
        IP), bounded by a short timeout and a read cap, and non-raising: a failed
        courtesy lookup is logged at INFO and forgotten."""
        proxy = getattr(getattr(wallet, "network", None), "proxy", None)
        headers = {"User-Agent": "Electrum-inbound-liquidity",
                   "Accept": "application/vnd.github+json"}
        try:
            async with make_aiohttp_session(proxy, headers=headers,
                                            timeout=UPDATE_CHECK_TIMEOUT_SEC) as session:
                async with session.get(UPDATE_CHECK_URL) as response:
                    if response.status != 200:
                        self.logger.info(
                            f"update check: GitHub replied {response.status}; skipping")
                        return None
                    raw = await self._read_capped(response.content,
                                                  UPDATE_CHECK_MAX_BYTES + 1)
            if len(raw) > UPDATE_CHECK_MAX_BYTES:
                self.logger.info("update check: response too large; skipping")
                return None
            release = extract_release(json.loads(raw.decode("utf-8")))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.info(f"update check failed: {e!r}")
            return None
        if release is None:
            self.logger.info("update check: no usable release in the reply; skipping")
        return release

    # --- live log capture -------------------------------------------------
    def log_buffer_lines(self) -> int:
        """Configured Log-tab buffer size, clamped to the supported range."""
        return clamp_max_lines(getattr(self.config, "INBOUND_LIQUIDITY_LOG_BUFFER_LINES",
                                       DEFAULT_LOG_BUFFER_LINES))

    def apply_log_capture_settings(self) -> None:
        """(Re)configure log capture from the current config. Idempotent, so the
        Advanced tab can just call it after any of the three settings change."""
        try:
            self.log_buffer.resize(self.log_buffer_lines())
            self.log_capture.configure(
                capture_ln=bool(getattr(self.config, "INBOUND_LIQUIDITY_LOG_CAPTURE_LN", False)),
                force_debug=bool(getattr(self.config, "INBOUND_LIQUIDITY_LOG_CAPTURE_DEBUG", False)),
            )
        except Exception as e:
            # Capture is a diagnostic convenience; never let it break plugin load.
            self.logger.info(f"could not configure log capture: {e!r}")

    def get_log_lines(self, *, min_level: int = 0,
                      text: Optional[str] = None) -> List[LogLine]:
        """Captured log lines, oldest first, optionally filtered."""
        return self.log_buffer.snapshot(min_level=min_level, text=text)

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

    def _wallet_synced(self, wallet: 'Abstract_Wallet') -> bool:
        """Whether the address synchronizer has caught up with the server.

        This is the one readiness limb that *is* about funds: until the wallet is
        up to date its UTXO set is partial, so a balance-driven decision (open /
        swap) would be taken on incomplete state.

        A wallet that cannot answer (no such method, or it raises) is treated as
        synced. Assuming "not synced" would stall the plugin forever on any
        wallet type that does not implement it, and the previous behaviour never
        consulted sync state at all -- so defaulting to True is the no-regression
        choice."""
        probe = getattr(wallet, "is_up_to_date", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:
            return True

    def _readiness_block(self, wallet: 'Abstract_Wallet', *,
                         manual: bool = False) -> Optional[str]:
        """Why ``wallet`` cannot take automated action yet, or None if it can.

        Gathers the live signals -- server connection, wallet sync, and how long
        since load -- and hands them to the pure predicate. A wallet we are not
        managing (no recorded load time) is never ready.

        ``manual=True`` marks a user-initiated "Run now", which skips the
        time-based startup window only; a disconnected, still-syncing or locked
        wallet is refused either way."""
        started = self._started_at.get(wallet)
        if started is None:
            return BLOCK_NOT_MANAGED
        network = getattr(wallet, "network", None)
        try:
            connected = bool(network is not None and network.is_connected())
        except Exception:
            connected = False
        return wallet_readiness_block(
            connected, self._wallet_synced(wallet), time.time() - started,
            self._startup_grace_sec, manual=manual,
            wallet_unlocked=self._signing_unlocked(wallet))

    def _wallet_ready(self, wallet: 'Abstract_Wallet', *,
                      manual: bool = False) -> bool:
        """Whether ``wallet`` has settled enough to take automated action."""
        return self._readiness_block(wallet, manual=manual) is None

    def request_evaluation(self, wallet: 'Abstract_Wallet', *, manual: bool = False) -> None:
        """Schedule one evaluation of `wallet` on the network's asyncio loop.

        Used both on wallet load and when the user arms the plugin from the UI
        (the ENABLED/DISABLED slider), so enabling takes effect immediately
        rather than waiting for the next wallet event. A no-op when the wallet is
        offline (no loop) or not managed; `_evaluate` re-reads config and returns
        early when automation is disabled, so a stray call is harmless.

        `manual=True` marks this as a user-initiated "Run now": it bypasses the
        "manual run only" gate (see `_evaluate`) so the plugin acts even when
        automatic evaluation is switched off. Callers on the automatic paths
        (wallet load, the arm-switch kick) leave it False.
        """
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(self._evaluate(wallet, manual=manual), loop)

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
            min_outbound_sat=max(0, int(c.INBOUND_LIQUIDITY_MIN_OUTBOUND_SAT)),
            manage_plugin_opened_only=bool(c.INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY),
            max_opens_per_day=self._max_opens_per_day(),
            liquidity_goal_sat=self._liquidity_goal_sat(),
            preferred_npubs=_parse_npub_set(c.INBOUND_LIQUIDITY_PREFERRED_NPUBS),
            banned_npubs=_parse_npub_set(c.INBOUND_LIQUIDITY_BANNED_NPUBS),
            liquidity_sink_address=self._liquidity_sink_address(),
            disable_submarine_swaps=self._submarine_swaps_disabled(),
            defer_sink_until_goal=self._defer_sink_until_goal(),
        )

    def _liquidity_sink_address(self) -> str:
        """The configured liquidity-sink address, trimmed. Empty means the
        feature is off and the plugin reverse-swaps as it always did. Read via
        getattr so a config predating this setting (or a stubbed one in tests)
        reads as "off" rather than raising on every tick."""
        return (getattr(self.config, "INBOUND_LIQUIDITY_SINK_ADDRESS", "") or "").strip()

    def _submarine_swaps_disabled(self) -> bool:
        """Whether the user has turned reverse swaps off entirely, leaving the
        liquidity sink as the only way to drain a channel. Honoured even with no
        sink configured -- see the note on ``LiquidityConfig.disable_submarine_swaps``."""
        return bool(getattr(
            self.config, "INBOUND_LIQUIDITY_DISABLE_SUBMARINE_SWAPS", False))

    def _defer_sink_until_goal(self) -> bool:
        """Whether the liquidity sink is held back until the channel build-out
        reaches the liquidity goal (see ``liquidity_goal_met``). Defaults to True
        -- the shipped default -- so a config predating this setting reads as ON,
        which is the funds-preserving answer: the sink keeps sending outbound out
        of the wallet only once the goal it would otherwise starve has been
        met."""
        return bool(getattr(
            self.config, "INBOUND_LIQUIDITY_DEFER_SINK_UNTIL_GOAL", True))

    def _manual_run_only(self) -> bool:
        """Whether the user has put the plugin in "manual run only" mode: no
        automatic evaluation fires (wallet events, the heartbeat, the post-grace
        one-shot, and the arm-switch kick are all no-ops); the plugin acts only on
        an explicit "Run now". A pure glue concern -- the rules engine never sees
        this flag, so a manual run still flows through the engine and acts
        normally."""
        return bool(getattr(self.config, "INBOUND_LIQUIDITY_MANUAL_RUN_ONLY", False))

    def _liquidity_goal_sat(self) -> int:
        """The configured liquidity goal in sat, clamped non-negative. A garbage
        or missing value reads as the shipped default rather than raising: this
        is read on every tick, and a hand-edited config must not be able to abort
        the whole evaluation."""
        try:
            return max(0, int(getattr(self.config, "INBOUND_LIQUIDITY_GOAL_SAT",
                                      DEFAULT_LIQUIDITY_GOAL_SAT)))
        except (TypeError, ValueError):
            return DEFAULT_LIQUIDITY_GOAL_SAT

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
        try:
            db.put(ACTION_TIMESTAMPS_DB_KEY, data)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist action timestamps: {e!r}", exc_info=True)

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
        try:
            db.put(DEV_FEE_OWED_DB_KEY, max(0, int(owed_sat)))
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist dev-fee ledger: {e!r}", exc_info=True)

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
        try:
            db.put(DEV_FEE_PAYMENTS_DB_KEY, data)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist dev-fee payment history: {e!r}", exc_info=True)

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

    async def _lnurl_pay_params(self, address: str, *,
                                purpose: str) -> Optional['LNURL6Data']:
        """Resolve a payout target to its LNURL-pay parameters (min/max sendable
        and the callback URL), or None with a logged reason.

        Shared by the two things this plugin pays over LNURL -- the dev fee and
        the liquidity sink -- so there is one implementation of "is this address
        usable at all". Callers own the amount policy that follows; this only
        answers whether there is an LNURL-pay endpoint at the other end.
        """
        from electrum.lnurl import request_lnurl, LNURL6Data
        url = self._resolve_lnurl_pay_url(address)
        if url is None:
            self.logger.warning(
                f"{purpose}: address {scrub_text(address, max_len=80)!r} is not a "
                f"valid Lightning address / LNURL; skipping")
            return None
        lnurl_data = await request_lnurl(url)
        if not isinstance(lnurl_data, LNURL6Data):
            self.logger.warning(f"{purpose}: address is not an LNURL-pay endpoint")
            return None
        return lnurl_data

    async def _lnurl_invoice(self, lnurl_data: 'LNURL6Data', pay_sat: int, *,
                             purpose: str) -> Optional['Invoice']:
        """Ask an LNURL-pay endpoint for an invoice for exactly ``pay_sat``, and
        verify that is what came back.

        The amount check is the security-relevant part and is why this is shared
        rather than duplicated: the endpoint chooses the invoice, so an endpoint
        that returns an invoice for MORE than we asked would otherwise be paid
        without complaint. Returns None (logged) on anything unusable; the caller
        decides whether that is worth a retry.
        """
        from electrum.lnurl import callback_lnurl
        from electrum.invoices import Invoice
        invoice_data = await callback_lnurl(lnurl_data.callback_url,
                                            params={"amount": pay_sat * 1000})
        bolt11 = invoice_data.get("pr")
        if not bolt11:
            self.logger.warning(f"{purpose}: LNURL callback returned no invoice")
            return None
        invoice = Invoice.from_bech32(bolt11)
        if invoice.get_amount_sat() != pay_sat:
            self.logger.warning(
                f"{purpose}: invoice amount {invoice.get_amount_sat()} sat != "
                f"requested {pay_sat} sat; refusing to pay")
            return None
        return invoice

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
        lnworker = getattr(wallet, "lnworker", None)
        ok = False
        try:
            if lnworker is None:
                self.logger.info("dev-fee payout skipped: wallet has no lnworker")
                return
            lnurl_data = await self._lnurl_pay_params(address, purpose="dev-fee payout")
            if lnurl_data is None:
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
            invoice = await self._lnurl_invoice(lnurl_data, pay_sat,
                                                purpose="dev-fee payout")
            if invoice is None:
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
        return _plain_json_copy(raw) if isinstance(raw, dict) else {}

    def _save_reliability(self, wallet: 'Abstract_Wallet', data: Dict[str, Dict]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        try:
            db.put(RELIABILITY_DB_KEY, data)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist provider reliability: {e!r}", exc_info=True)

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
        return _plain_json_copy(raw) if isinstance(raw, dict) else {}

    def _save_peer_reliability(self, wallet: 'Abstract_Wallet', data: Dict[str, Dict]) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        # Guarded like every other store: `JsonDB.put` deep-copies what it is
        # given and raises on anything it cannot copy, and an exception here
        # escapes into whatever action was mid-flight (this one aborted the
        # channel-open candidate loop). Bookkeeping failing must never sink the
        # action it is bookkeeping for -- log it and carry on.
        try:
            db.put(PEER_RELIABILITY_DB_KEY, data)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist peer reliability: {e!r}", exc_info=True)

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

    def _bookkeep(self, what: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Run a bookkeeping side effect that must never sink the action it is
        recording, swallowing (and loudly logging) anything it raises.

        The save helpers each guard their own write, which covers the storage
        layer -- but the recorders around them do more than write: they log,
        rank, auto-ban and notify the GUI. This is the second half of that belt:
        applied at the call sites in the executor, where the action has either
        already happened (a channel is open) or the loop still has candidates
        left to try, and neither outcome may be lost to a failure in the
        record-keeping about it. It is deliberately NOT applied to the watchdog's
        recorders, where a fault *is* the action."""
        try:
            fn(*args, **kwargs)
        except Exception as e:
            self.logger.error(f"could not record {what}: {e!r}", exc_info=True)

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
        return _plain_json_copy(raw) if isinstance(raw, dict) else {}

    def _save_pending_swaps(self, wallet: 'Abstract_Wallet', data: Dict[str, Dict]) -> None:
        """Persist the pending-swap tracking map.

        The whole write is guarded. ``JsonDB.put`` deep-copies what it is given
        and raises on anything it cannot copy; unguarded, that exception escaped
        all the way out of ``_reverse_swap`` and aborted the executor *after* a
        swap had already been created -- losing the very tracking record that
        exists to reconcile it. Persistence failing must never be able to sink an
        action that already happened, so it is logged and swallowed here. (See
        ``_track_pending_swap``, which also keeps non-primitives out of the map
        in the first place.)"""
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        try:
            db.put(PENDING_SWAPS_DB_KEY, data)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist pending-swap tracking: {e!r}",
                              exc_info=True)

    def _track_pending_swap(self, wallet: 'Abstract_Wallet', payment_hash_hex: str,
                            npub: str, node_id: str = "", channel_id: str = "",
                            fee_basis_sat: int = 0) -> None:
        """Remember a reverse swap we initiated, so one that is accepted but never
        funds can be charged to the right party: the ``npub`` provider if it never
        funded, or the channel ``node_id`` peer if our Lightning payment failed.
        ``fee_basis_sat`` (the expected net on-chain amount) is stashed so the dev
        fee can be accrued if and only if the swap later completes.

        Every field is coerced to a JSON primitive on the way in. This store is
        JSON-backed (``JsonDB.put`` deep-copies, and the wallet file must stay
        plain JSON), so a caller that hands us a rich object -- a SwapData, a
        Channel, an offer -- rather than its id must not be able to poison the
        write. That was observed in the wild as ``TypeError: cannot pickle
        '_thread.RLock' object`` raised from deep inside ``db.put``, which took
        the surrounding reverse swap down with it."""
        key = str(payment_hash_hex or "")
        if not key:
            return
        data = self._load_pending_swaps(wallet)
        data[key] = {
            "npub": str(npub or ""), "started_ts": float(time.time()),
            "node_id": str(node_id or ""), "channel_id": str(channel_id or ""),
            "fee_basis_sat": _safe_int(fee_basis_sat)}
        self._save_pending_swaps(wallet, data)

    def _ln_payment_failed(self, wallet: 'Abstract_Wallet', payment_hash_hex: str) -> bool:
        """Whether our outgoing Lightning payment for this swap has definitively
        failed (every route attempt exhausted) -- the signal that the fault is our
        channel peer's, not the swap provider's.

        ``PR_FAILED`` is never *stored* on a sent payment: ``get_payment_status``
        returns the persisted ``PaymentInfo.status``, and nothing in lnworker ever
        writes PR_FAILED there. Electrum only *derives* it, in
        ``lnworker.get_invoice_status``: a sent payment is failed when it is
        PR_UNPAID, is not in ``inflight_payments``, and has route-attempt logs in
        ``lnworker.logs`` (i.e. we tried and gave up). Comparing the raw status to
        PR_FAILED -- what this did before -- was therefore dead code: every swap
        that did not fund fell through to the stuck-swap timeout and was charged
        to the provider, even when the real cause was our own payment failing.
        So replicate that derivation instead.
        """
        from electrum.lnutil import Direction
        from electrum.invoices import PR_UNPAID, PR_FAILED
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return False
        try:
            status = lnworker.get_payment_status(
                bytes.fromhex(payment_hash_hex), direction=Direction.SENT)
        except Exception:
            return False
        # A future Electrum that does persist PR_FAILED keeps working unchanged.
        if status == PR_FAILED:
            return True
        if status != PR_UNPAID:
            return False
        # Still being retried -> not (yet) a failure.
        if payment_hash_hex in getattr(lnworker, "inflight_payments", ()):
            return False
        # Route attempts were logged and none is in flight: we gave up.
        return bool(getattr(lnworker, "logs", {}).get(payment_hash_hex))

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
        ``timeout_sec`` (by its persisted ``init_timestamp``).

        This is the FREEZE-ESCAPE clock only -- "has this been in flight long
        enough that it should stop blocking the plugin". It is deliberately
        plain wall-clock age from channel creation, with no confirmation-
        progress exemption, so that a channel ALWAYS un-freezes automation
        eventually. The separate, much more conservative force-close clock is
        the strike accumulator (see ``_wedge_eligible``).
        """
        try:
            init_ts = float(chan.storage.get("init_timestamp", 0) or 0)
        except Exception:
            return False
        if init_ts <= 0:
            return False
        # A creation timestamp in the future means the wall clock moved
        # backwards (NTP correction, wrong-clock boot). Treat it as brand new
        # rather than letting a skewed comparison declare it ancient.
        if init_ts > now:
            return False
        return (now - init_ts) > timeout_sec

    def _wedge_params(self) -> Dict[str, float]:
        """Wedged-open gate tuning, read fresh so edits take effect at once. The
        getattr defaults keep partial (unit-test) configs sane."""
        c = self.config
        return {
            "close_timeout_sec": max(1, int(getattr(
                c, "INBOUND_LIQUIDITY_STUCK_OPEN_CLOSE_TIMEOUT_MIN",
                DEFAULT_STUCK_OPEN_CLOSE_TIMEOUT_MIN))) * 60.0,
            "required_strikes": max(1, int(getattr(
                c, "INBOUND_LIQUIDITY_WEDGE_REQUIRED_CHECKS",
                WEDGE_REQUIRED_STRIKES))),
            "min_spread_sec": max(0.0, float(getattr(
                c, "INBOUND_LIQUIDITY_WEDGE_CHECK_SPREAD_MIN",
                WEDGE_MIN_SPREAD_SEC / 60.0)) * 60.0),
            "coop_window_sec": max(0.0, float(getattr(
                c, "INBOUND_LIQUIDITY_COOP_BEFORE_FORCE_MIN",
                COOP_BEFORE_FORCE_WINDOW_SEC / 60.0)) * 60.0),
        }

    def _funding_confirmations(self, chan) -> Optional[Tuple[int, int]]:
        """``(confirmations, required_depth)`` for the channel's funding tx, or
        ``None`` if either is unknowable. Read through the lnwatcher's address
        db, which is what Electrum itself uses to decide the channel has
        confirmed (``is_funding_tx_mined``)."""
        try:
            txid = chan.funding_outpoint.txid
            adb = chan.lnworker.lnwatcher.adb
            conf = int(adb.get_tx_height(txid).conf)
            depth = int(chan.funding_txn_minimum_depth())
        except Exception:
            return None
        return (conf, max(1, depth))

    def _is_confirming(self, chan) -> bool:
        """Whether a pre-OPEN channel is making legitimate confirmation
        progress -- i.e. nothing is wrong with it, it just needs more blocks.

        A channel that has reached FUNDED has all the confirmations it needs;
        anything keeping it from OPEN from there is the peer failing to complete
        the ``channel_ready`` exchange, which is exactly what remediation is
        for. Below FUNDED, it is confirming as long as its funding tx has not
        yet reached the depth the peer requires.
        """
        from electrum.lnchannel import ChannelState
        try:
            if chan.get_state() >= ChannelState.FUNDED:
                return False
        except Exception:
            return False
        confs = self._funding_confirmations(chan)
        if confs is None:
            # Can't tell. Treat as confirming: the cost of guessing wrong this
            # way is a delayed force-close, the cost of guessing wrong the other
            # way is force-closing a healthy channel. ``_wedge_eligible``'s
            # absolute cap still backstops a channel that never resolves.
            return True
        conf, depth = confs
        return conf < depth

    def _wedge_eligible(self, chan, now: float) -> bool:
        """Whether this channel currently looks WEDGED, as opposed to merely
        slow -- the first of the three gates on force-closing a stuck open.

        Eligible when the channel is pre-OPEN and is *not* making confirmation
        progress. The absolute cap overrides the confirmation exemption: a
        funding tx that never confirms would otherwise stay exempt forever,
        leaving the funds locked with nothing able to free them.
        """
        from electrum.lnchannel import ChannelState
        pending_states = (
            ChannelState.PREOPENING, ChannelState.OPENING, ChannelState.FUNDED)
        try:
            if chan.get_state() not in pending_states:
                return False
        except Exception:
            return False
        if self._open_age_exceeded(chan, WEDGE_ABSOLUTE_CAP_SEC, now):
            return True
        return not self._is_confirming(chan)

    def _scan_channel_health(self, wallet: 'Abstract_Wallet') -> None:
        """Watchdog over the wallet's channels, run each tick before snapshotting.
        Each is charged as a *hard* peer fault (feeding the auto-ban tally and the
        partner ordering):

          * a channel the **peer closed** -- cooperative or force -- the moment we
            observe the transition (rising edge), excluding closes *we* triggered;
          * a channel whose **peer is offline** (OPEN but not connected), at most
            once per 24h per peer (rate-limited so a long outage isn't double-counted);
          * a channel **wedged opening** -- not merely confirming -- on several
            separate checks spread over time, past the force-close timeout.
            Such a channel is also closed when auto-remediation is on, so its
            funds are freed; cooperatively if the peer can be reached at all.

        Channels already closed before we started managing (present on the very
        first scan) are seeded, not retroactively blamed on the peer.
        """
        from electrum.lnchannel import ChannelState
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return
        auto_remediate = bool(getattr(
            self.config, "INBOUND_LIQUIDITY_AUTO_REMEDIATE_STUCK_OPEN", True))
        # The force-close gates, independent of the freeze-escape timeout: un-
        # freezing is cheap and reversible, force-closing is neither, so they get
        # very different budgets.
        wedge = self._wedge_params()
        close_timeout_sec = wedge["close_timeout_sec"]
        closing_states = (
            ChannelState.SHUTDOWN, ChannelState.CLOSING, ChannelState.FORCE_CLOSING,
            ChannelState.REQUESTED_FCLOSE, ChannelState.CLOSED)
        force_states = (ChannelState.FORCE_CLOSING, ChannelState.REQUESTED_FCLOSE)
        now = time.time()
        remediating = self._remediating_opens.setdefault(wallet, set())
        local_closes = self._local_closes.setdefault(wallet, set())
        wedged_faulted = self._wedged_faulted.setdefault(wallet, set())
        close_capped_logged = self._close_capped_logged.setdefault(wallet, set())
        strikes = self._load_json_dict(wallet, WEDGE_STRIKES_DB_KEY)
        strikes_changed = False
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
            # Stuck-open watchdog. Three independent gates before we will close
            # anything (see liquidity_manager's "wedged channel-open
            # remediation" notes): eligibility (is it actually wedged, or just
            # confirming?), the wedge clock, and the strike count/spread. Every
            # tick folds one observation in -- including the negative ones,
            # which wipe the record when a channel recovers.
            eligible = self._wedge_eligible(chan, now)
            updated = record_wedge_strike(strikes.get(cid), now, eligible)
            if updated != strikes.get(cid):
                if updated is None:
                    strikes.pop(cid, None)
                    # The channel recovered: drop any close attempt history too,
                    # so a later, unrelated wedge starts its cooperative-close
                    # window from scratch rather than inheriting an expired one
                    # and escalating straight past cooperation.
                    self._clear_close_attempt(wallet, cid)
                    wedged_faulted.discard(cid)
                else:
                    strikes[cid] = updated
                strikes_changed = True
            if eligible and cid not in remediating and should_remediate_wedged_open(
                    updated, now, timeout_sec=close_timeout_sec,
                    required_strikes=int(wedge["required_strikes"]),
                    min_spread_sec=wedge["min_spread_sec"]):
                # Fault the peer exactly once for the wedged open (decoupled from
                # remediation, which may be deferred by the close ceiling below).
                if cid not in wedged_faulted:
                    wedged_faulted.add(cid)
                    self._record_peer_fault(
                        wallet, node_id,
                        f"channel open wedged > {int(close_timeout_sec // 60)} min",
                        hard=True)
                if auto_remediate:
                    self._request_close(
                        wallet, chan, cid, node_id, now,
                        reason="wedged channel open",
                        detail=(f"channel {cid[:12]}… wedged > "
                                f"{int(close_timeout_sec // 60)} min "
                                f"({updated['strikes']} checks over "
                                f"{int((updated['last_ts'] - updated['first_ts']) // 60)} min)"),
                        on_force_closed=lambda: remediating.add(cid))
        # Forget bookkeeping for channels that are gone (redeemed/removed), so a
        # later reused channel_id starts fresh.
        remediating &= live_ids
        local_closes &= live_ids
        wedged_faulted &= live_ids
        close_capped_logged &= live_ids
        for gone in [c for c in known if c not in live_ids]:
            del known[gone]
        for gone in [c for c in strikes if c not in live_ids]:
            del strikes[gone]
            strikes_changed = True
        if strikes_changed:
            self._save_json(wallet, WEDGE_STRIKES_DB_KEY, strikes)

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
        return _plain_json_copy(raw) if isinstance(raw, dict) else {}

    def _save_json(self, wallet: 'Abstract_Wallet', key: str, data) -> None:
        db = getattr(wallet, "db", None)
        if db is None:
            return  # db-less wallet (unit-test mock); nothing to persist
        try:
            db.put(key, data)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist {key}: {e!r}", exc_info=True)

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
                # Drop the close-attempt record with it. Otherwise, if the peer
                # goes offline again later and we re-commit, the stale
                # ``first_ts`` would already be past the cooperative window and
                # the very first tick would force-close without ever trying to
                # cooperate -- exactly the invariant this record exists to hold.
                self._clear_close_attempt(wallet, cid)
                self.logger.info(
                    f"cancelling pending close of {cid[:12]}…: peer recovered")

            # 3) Act on a committed intent that hasn't closed yet.
            intent = intents.get(cid)
            if intent is None:
                continue
            marked_ts = float(intent.get("marked_ts", now) or now)
            if deadline_reached(marked_ts, now, params["force_close_sec"]):
                # Past the deadline: hand over to the single close path, which
                # still tries cooperation first if the peer has become
                # reachable, and only force-closes once that has had its chance.
                self._request_close(
                    wallet, chan, cid, node_id, now,
                    reason="offline channel (auto-close)",
                    detail=f"peer never agreed within "
                           f"{params['force_close_sec'] / 86400.0:.2f}d of trying to close")
            elif state == ChannelState.OPEN:
                # Not yet at the deadline: attempt the cheaper cooperative close
                # whenever the peer is reachable (it requires the peer online).
                try:
                    peer_online = bool(chan.is_active())
                except Exception:
                    peer_online = False
                if peer_online:
                    self._maybe_cooperative_close(wallet, chan, cid, node_id, now,
                                                  reason="offline channel (auto-close)")

        if uptime_changed:
            self._save_json(wallet, CHANNEL_UPTIME_DB_KEY, uptime)
        # Forget close-attempt records for channels that no longer exist, so the
        # store stays bounded and a reused channel id never inherits one.
        attempts = self._load_json_dict(wallet, CLOSE_ATTEMPT_DB_KEY)
        stale_attempts = [c for c in attempts if c not in channels]
        if stale_attempts:
            for c in stale_attempts:
                attempts.pop(c, None)
            self._save_json(wallet, CLOSE_ATTEMPT_DB_KEY, attempts)
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

    # --- the single close path -------------------------------------------
    # INVARIANT: every close the plugin initiates goes through _request_close,
    # and _request_close never force-closes without first having tried -- or
    # established the impossibility of -- a cooperative close. A force-close
    # costs a mining fee, locks the funds behind a CSV timelock, and cannot be
    # undone; cooperation is cheap and immediate. Funnelling both watchdogs
    # through one function is what makes the rule impossible to bypass by
    # adding a caller.
    def _coop_close_possible(self, chan) -> bool:
        """Whether a cooperative close can even be attempted right now: we need a
        live, good-state connection to the peer.

        ``chan.is_active()`` is deliberately NOT used here -- it is hardcoded to
        ``state == OPEN`` (lnchannel.is_active), so it is False for every
        channel the wedged-open path deals with, which would make cooperation
        look permanently impossible on exactly the channels this matters for.
        Electrum permits a cooperative close from any state >= OPENING
        (``lnpeer.can_send_shutdown``), so the real precondition is just a peer
        we can talk to.
        """
        from electrum.lnchannel import ChannelState, PeerState
        try:
            if not (ChannelState.OPENING <= chan.get_state() < ChannelState.CLOSING):
                return False
            if chan.peer_state != PeerState.GOOD:
                return False
            return chan.lnworker.lnpeermgr.get_peer_by_pubkey(chan.node_id) is not None
        except Exception:
            return False

    def _load_close_attempt(self, wallet: 'Abstract_Wallet', cid: str) -> Optional[Dict]:
        rec = self._load_json_dict(wallet, CLOSE_ATTEMPT_DB_KEY).get(cid)
        return rec if isinstance(rec, dict) else None

    def _save_close_attempt(self, wallet: 'Abstract_Wallet', cid: str,
                            rec: Dict) -> None:
        attempts = self._load_json_dict(wallet, CLOSE_ATTEMPT_DB_KEY)
        attempts[cid] = rec
        self._save_json(wallet, CLOSE_ATTEMPT_DB_KEY, attempts)

    def _clear_close_attempt(self, wallet: 'Abstract_Wallet', cid: str) -> bool:
        """Forget a channel's close-attempt history. Returns whether anything was
        dropped. Called when a channel recovers, so a later unrelated close
        request starts its cooperative window fresh instead of inheriting a
        stale one and escalating straight to a force-close."""
        attempts = self._load_json_dict(wallet, CLOSE_ATTEMPT_DB_KEY)
        if cid not in attempts:
            return False
        attempts.pop(cid, None)
        self._save_json(wallet, CLOSE_ATTEMPT_DB_KEY, attempts)
        return True

    def _request_close(self, wallet: 'Abstract_Wallet', chan, cid: str,
                       node_id: str, now: float, *, reason: str, detail: str,
                       on_force_closed: Optional[Callable[[], None]] = None) -> None:
        """Ask for ``chan`` to be closed, cooperatively if at all possible.

        Called repeatedly (once per tick) by the watchdogs for as long as they
        still want the channel closed. Each call does the most cooperative thing
        available and returns; escalation to a force-close happens on a LATER
        call, once ``coop_before_force_ready`` agrees cooperation has had its
        chance. This staging is required because a cooperative close is an async
        negotiation taking minutes, while the watchdogs are synchronous scans --
        there is no way to await one inline.

        ``on_force_closed`` fires only on the force-close branch, so a caller can
        record that it has spent its one remediation.
        """
        from electrum.lnchannel import ChannelState
        try:
            state = chan.get_state()
        except Exception:
            return
        # Electrum is explicit that force-closing a WE_ARE_TOXIC channel is
        # unsafe -- we have lost state and the remote has proved it, so our
        # commitment tx is revoked and publishing it forfeits the balance. The
        # remote must close this one; we must not touch it. Checked explicitly
        # (the >= SHUTDOWN return below also covers it today, since WE_ARE_TOXIC
        # sorts above SHUTDOWN) so the guarantee does not rest on that ordering.
        if state == ChannelState.WE_ARE_TOXIC:
            self.logger.info(
                f"not closing {cid[:12]}…: channel is WE_ARE_TOXIC, only the "
                f"remote may close it safely")
            return
        # Already closing (by us, by the peer, or cooperatively in progress):
        # nothing left to ask for.
        if state >= ChannelState.SHUTDOWN:
            return
        # Stamp "we have wanted this closed since T" BEFORE the ceiling check, so
        # a close deferred by the daily ceiling does not also restart the
        # cooperative-close window once the ceiling clears.
        rec = self._load_close_attempt(wallet, cid)
        if rec is None:
            rec = {"first_ts": now, "coop_attempts": 0, "last_coop_ts": None}
            self._save_close_attempt(wallet, cid, rec)

        if not self._within_close_cap(wallet, now):
            if cid not in self._close_capped_logged.setdefault(wallet, set()):
                self._close_capped_logged[wallet].add(cid)
                self.logger.warning(
                    f"daily close ceiling ({self._max_closes_per_day()}/24h) "
                    f"reached; deferring close of {cid[:12]}… to peer "
                    f"{node_id[:12]}… ({reason})")
            return
        self._close_capped_logged.setdefault(wallet, set()).discard(cid)

        # A cooperative close is negotiating right now: never interrupt it.
        if cid in self._coop_closing.get(wallet, set()):
            return
        attempts = int(rec.get("coop_attempts", 0) or 0)
        coop_window_sec = self._wedge_params()["coop_window_sec"]

        def _try_coop() -> bool:
            if not self._coop_close_possible(chan):
                return False
            if not self._maybe_cooperative_close(wallet, chan, cid, node_id, now,
                                                 reason=reason, detail=detail):
                return False
            rec["coop_attempts"] = attempts + 1
            rec["last_coop_ts"] = now
            self._save_close_attempt(wallet, cid, rec)
            return True

        # 1) Still inside the current attempt's window. Launch the FIRST attempt
        #    here (nothing has been tried yet); otherwise just wait it out.
        if not coop_before_force_ready(rec, now, window_sec=coop_window_sec):
            if attempts == 0:
                _try_coop()
            return

        # 2) The window has elapsed without the channel closing. Retry
        #    cooperation while we still have attempts left and a peer to talk
        #    to; each retry gets its own window. The attempt cap is what makes
        #    this terminate -- a reachable peer that simply refuses to cooperate
        #    would otherwise renew the window forever and never be force-closed.
        if attempts < COOP_MAX_ATTEMPTS and _try_coop():
            return
        try:
            wallet.lnworker.schedule_force_closing(chan.channel_id)
        except Exception as e:
            self.logger.warning(
                f"could not force-close {cid[:12]}… ({reason}): {e!r}")
            return
        if on_force_closed is not None:
            on_force_closed()
        self._record_action_event(wallet, "close")
        attempted = int(rec.get("coop_attempts", 0) or 0)
        why = (f"after {attempted} cooperative close attempt(s)" if attempted
               else "peer was never reachable to close cooperatively")
        self.logger.warning(
            f"force-closing {cid[:12]}… to peer {node_id[:12]}… ({reason}); {why}")
        self._log_action(
            wallet, kind="close", amount_sat=None,
            source=self._abbrev(node_id) or node_id or None, dest=None,
            reason=f"force-closed {reason}",
            detail=f"{detail}; {why}", state=None)

    def _maybe_cooperative_close(self, wallet: 'Abstract_Wallet', chan, cid: str,
                                 node_id: str, now: float, *,
                                 reason: str = "offline channel",
                                 detail: Optional[str] = None) -> bool:
        """Launch a cooperative close on a channel whose peer is reachable, unless
        one is already in flight or we attempted one recently (cooldown).
        Returns whether an attempt was actually launched. The close is async and
        can take minutes, so it runs as a background task; the force-close
        escalation remains the backstop if it never completes."""
        if not self._within_close_cap(wallet, now):
            return False
        coop_inflight = self._coop_closing.setdefault(wallet, set())
        if cid in coop_inflight:
            return False
        mono = time.monotonic()
        if mono < self._coop_close_cooldown_until.get(cid, 0.0):
            return False
        self._coop_close_cooldown_until[cid] = mono + COOP_CLOSE_COOLDOWN_SEC
        coop_inflight.add(cid)
        loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
        coro = self._do_cooperative_close(wallet, chan.channel_id, cid, node_id,
                                          reason=reason, detail=detail)
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            asyncio.ensure_future(coro)
        return True

    async def _do_cooperative_close(self, wallet: 'Abstract_Wallet', chan_id: bytes,
                                    cid: str, node_id: str, *,
                                    reason: str = "offline channel",
                                    detail: Optional[str] = None) -> None:
        lnworker = getattr(wallet, "lnworker", None)
        try:
            self.logger.info(
                f"attempting cooperative close of {cid[:12]}… to peer "
                f"{node_id[:12]}… ({reason}; peer is currently reachable)")
            await lnworker.close_channel(chan_id)
            self._record_action_event(wallet, "close")
            self.logger.info(f"cooperatively closed channel {cid[:12]}…")
            self._log_action(
                wallet, kind="close", amount_sat=None,
                source=self._abbrev(node_id) or node_id or None, dest=None,
                reason=f"cooperatively closed {reason}",
                # The caller's own justification (e.g. the liquidity-goal
                # arithmetic) leads, so the entry explains WHY this channel was
                # closed and not merely how.
                detail="; ".join(filter(None, [
                    detail,
                    f"channel {cid[:12]}… closed with a reachable peer, "
                    f"avoiding a force-close"])),
                state=None)
        except Exception as e:
            # Peer went away mid-close, or refused: leave the intent in place so
            # the force-close deadline still escalates it.
            self.logger.info(
                f"cooperative close of {cid[:12]}… did not complete: {e!r}")
        finally:
            self._coop_closing.get(wallet, set()).discard(cid)

    def _close_undersized_channel(self, wallet: 'Abstract_Wallet',
                                  action: CloseChannelAction,
                                  state: Optional[Dict] = None) -> None:
        """Execute an engine-decided undersized-channel replacement.

        Deliberately thin: everything that makes a close safe -- cooperation
        first, the daily close ceiling, the force-close backstop, the
        WE_ARE_TOXIC refusal -- already lives in ``_request_close``, and routing
        through it is what keeps the "never force-close without first trying to
        cooperate" invariant true by construction rather than by review. The
        engine has already established that the peer is reachable, so the very
        first call here normally completes cooperatively; the escalation path
        only matters if the peer vanishes mid-negotiation.
        """
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            return
        cid = action.channel_id
        try:
            chan = lnworker.channels.get(bytes.fromhex(cid))
        except (ValueError, AttributeError):
            chan = None
        if chan is None:
            # Closed or forgotten between snapshot and execution. Nothing to do;
            # the next tick re-reads the world.
            self.logger.info(
                f"undersized channel {cid[:12]}… is no longer present; skipping close")
            return
        node_id = self._channel_peer_node_id(wallet, cid)
        goal = self._liquidity_goal_sat()
        self.logger.info(
            f"replacing undersized channel {cid[:12]}… ({action.capacity_sat} sat "
            f"capacity, below the {goal} sat liquidity goal)")
        # No decision-log entry is written here on purpose. _request_close may
        # legitimately do nothing this tick -- the daily close ceiling can defer
        # it, or a cooperative close may already be negotiating -- and an entry
        # written before the attempt would claim a close that never happened.
        # The close paths themselves log the real event (cooperative on success,
        # force-close on escalation), so the arithmetic is handed to them as
        # `detail` and appears on whichever one actually fires.
        self._request_close(
            wallet, chan, cid, node_id, time.time(),
            reason="undersized channel (liquidity goal)",
            detail=f"capacity {action.capacity_sat} sat is below the {goal} sat "
                   f"liquidity goal and was blocking a bigger channel")

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

    @staticmethod
    def _channel_sendable_sat(lnworker, chan) -> int:
        """How much of ``chan``'s local balance Electrum can actually *send*.

        ``available_to_spend(LOCAL)`` is the raw ceiling: the balance minus the
        channel reserve and the commitment/fee-spike buffer. It is NOT what
        Electrum will let you send, because a Lightning payment also needs a
        routing-fee budget on top of the amount, and that budget is paid from the
        same balance. Electrum's own "max sendable" (``lnworker.num_sats_can_send``)
        therefore subtracts ``estimate_fee_reserve_for_total_amount()`` from the
        raw ceiling -- whose docstring names this exact use case ("reliably allow
        doing a 'Max' amount lightning send (e.g. for submarine swaps)").

        We must mirror that, and per channel rather than wallet-wide, because a
        reverse swap's Lightning leg is pinned to one channel (see
        ``_reverse_swap``). Sizing a swap at the raw ceiling asks Electrum to send
        strictly more than it believes it can: the provider splits our
        ``lightning_amount_sat`` into a main hold invoice plus a mining-fee
        prepayment invoice, so two HTLCs must fit in that one channel, and the
        provider only creates the on-chain funding output once BOTH arrive. If
        either leg cannot be routed the swap is never funded and never fails --
        it just hangs until the stuck-swap timeout blames the provider.

        Electrum's reserve alone is NOT enough here, which is the bug this extra
        headroom fixes. That reserve is sized for ONE max-amount payment, and it
        is very nearly exact: on a 804,515 sat channel it left a swap of 796,549
        sat, against a requirement of 804,514 -- a margin of ONE satoshi. The
        swap is two payments, and they are issued CONCURRENTLY by Electrum
        (``submarine_swaps.reverse_swap`` fires the prepayment with
        ``ensure_future`` and, unlike the main payment, does not pin it to our
        chosen channel). So whether the main leg routes depends on whether the
        prepayment's HTLC has already been committed against the same channel --
        and once it has, the extra in-flight HTLC also enlarges the commitment
        transaction, eating what little slack was left. Measured on the regtest
        rig, a max-sized swap failed about three runs in four, identically with
        and without the evaluation rate limit.

        The failure is quiet and it blames the wrong party: the swap is accepted,
        never funded, and reconciliation later records a stuck-swap fault against
        a provider that did nothing wrong (repeat faults de-prioritise, then ban
        it).

        Rather than try to re-derive Electrum's per-HTLC commitment cost -- which
        is version-specific and would silently drift -- we simply stop sizing
        swaps at the ceiling, and keep SWAP_SIZING_HEADROOM_PCT of the channel
        back. A swap ~1% smaller costs the user nothing that matters; being one
        satoshi short costs the whole swap.

        Degrades to the raw ceiling if the estimator is unavailable (older
        Electrum / a stubbed lnworker in tests) rather than blocking swaps.
        """
        from electrum.lnutil import LOCAL
        raw_sat = int(chan.available_to_spend(LOCAL)) // 1000
        estimate = getattr(lnworker, "estimate_fee_reserve_for_total_amount", None)
        if not callable(estimate):
            return max(0, raw_sat)
        try:
            reserve_sat = int(estimate(raw_sat))
        except Exception as e:  # noqa: BLE001
            _logger.info(f"could not estimate LN fee reserve for channel: {e!r}")
            return max(0, raw_sat)
        headroom_sat = LiquidityPlugin._swap_sizing_headroom_sat(
            raw_sat, LiquidityPlugin._second_htlc_overhead_sat(chan))
        return max(0, raw_sat - max(0, reserve_sat) - headroom_sat)

    @staticmethod
    def _second_htlc_overhead_sat(chan) -> int:
        """What a SECOND concurrent in-flight HTLC costs this channel, on top of
        its own value. This is the whole gap being corrected.

        ``available_to_spend`` already prices in ONE added HTLC -- it adds
        ``fee_for_htlc_output`` and sizes its fee-spike buffer at
        ``num_htlcs + 1`` at double the feerate (see ``lnchannel.py``). A reverse
        swap puts TWO HTLCs through the channel at once (main invoice +
        mining-fee prepayment, issued concurrently by ``reverse_swap``), so the
        second one's cost is unaccounted for -- and it is charged twice over:
        once as another commitment output, and once as the matching growth of
        that 2x-feerate buffer.

        Hence ``1x + 2x = 3x`` the per-HTLC output fee. Computed from the
        channel's live feerate with Electrum's own helper and the BOLT-defined
        ``HTLC_OUTPUT_WEIGHT``, rather than a guessed constant: it scales with
        the mempool (it was ~19,000 sat at regtest feerates and will be far
        smaller on a quiet mainnet), so any fixed number would be wrong almost
        everywhere. Returns 0 if the channel cannot answer, leaving the
        proportional part of the headroom to carry it.
        """
        try:
            from electrum.lnutil import LOCAL, fee_for_htlc_output
            feerate = int(chan.get_feerate(LOCAL, ctn=chan.get_next_ctn(LOCAL)))
            # The extra commitment output, plus the fee-spike buffer's matching
            # growth (which available_to_spend reserves at 2x feerate).
            overhead_msat = (fee_for_htlc_output(feerate=feerate)
                             + fee_for_htlc_output(feerate=2 * feerate))
            return max(0, int(math.ceil(overhead_msat / 1000)))
        except Exception as e:  # noqa: BLE001  (sizing must never raise)
            _logger.info(f"could not price a second HTLC for this channel: {e!r}")
            return 0

    @staticmethod
    def _swap_sizing_headroom_sat(raw_sat: int, htlc_overhead_sat: int = 0) -> int:
        """Slack held back from a max-sized swap, on top of Electrum's reserve.

        ``htlc_overhead_sat`` is the real cost of the swap's second concurrent
        HTLC (see ``_second_htlc_overhead_sat``) -- the quantity Electrum's
        one-payment reserve misses.

        An earlier attempt reserved the prepayment's full principal as a proxy
        for this. It worked on ordinary channels but was ruinous on small ones:
        the principal does not shrink with the channel, so on a 0.005 BTC channel
        it swallowed 46,000 of 69,515 spendable sat and cut the swap to 22,826
        -- small enough that the provider's fixed mining fee made it a 197%
        all-in cost, which the engine then (correctly) declined. Pricing the
        actual overhead instead keeps small channels swappable.

        The proportional part remains as plain safety margin, with a floor so a
        small channel still gets real (not rounding-sized) slack.
        """
        proportional = max(SWAP_SIZING_HEADROOM_MIN_SAT,
                           int(math.ceil(raw_sat * SWAP_SIZING_HEADROOM_PCT / 100.0)))
        return proportional + max(0, htlc_overhead_sat)

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
        # A channel that is merely CONFIRMING is not wedged at all, and gets a
        # far longer -- but opens-only -- freeze: it must not stall reverse swaps
        # on unrelated channels while the fee market does its thing.
        confirming_freeze_sec = max(0.0, float(getattr(
            self.config, "INBOUND_LIQUIDITY_CONFIRMING_FREEZE_DAYS",
            DEFAULT_CONFIRMING_FREEZE_DAYS))) * 86400.0
        now = time.time()
        pending_channel_count = 0
        pending_open_freeze_count = 0
        # Closes under way but not yet mined. WE_ARE_TOXIC is excluded on
        # purpose: it is not a close in progress (only the remote may close such
        # a channel) and it can persist indefinitely, so counting it would wedge
        # the undersized-replacement rule for good.
        closing_states = (ChannelState.SHUTDOWN, ChannelState.CLOSING,
                          ChannelState.FORCE_CLOSING, ChannelState.REQUESTED_FCLOSE)
        closing_channel_count = 0
        # Set of channel ids (hex) the plugin opened itself, for the
        # manage-plugin-opened-only scope switch. Read once per snapshot.
        plugin_opened = self._plugin_opened_channels(wallet)
        for chan in lnworker.channels.values():
            # Skip dead channels. Electrum keeps CLOSED/REDEEMED channel records
            # in ``lnworker.channels`` indefinitely (they are only dropped when the
            # user forgets them), but such a channel holds no liquidity, can never
            # be reverse-swapped, and must NOT count toward ``max_channels`` --
            # otherwise, once the user closes their channels, the lingering dead
            # records permanently occupy the channel budget and the plugin never
            # opens replacements. Threshold mirrors the one-channel-per-peer guard
            # (`_current_peer_node_ids`), which already frees a peer at >= CLOSED.
            try:
                if chan.get_state() >= ChannelState.CLOSED:
                    continue
            except Exception:  # noqa: BLE001
                pass
            capacity = chan.get_capacity() or 0
            # Unguarded: the dead-channel filter above has already called
            # get_state() successfully for this channel, so a try/except here
            # would only defer the same failure to the `pending_states` check on
            # the next line.
            if chan.get_state() in closing_states:
                closing_channel_count += 1
            if chan.get_state() in pending_states:
                # Age is from channel creation for BOTH horizons: this is the
                # freeze-escape clock, and it must always run so a pre-OPEN
                # channel un-freezes the plugin eventually no matter what.
                try:
                    init_ts = float(chan.storage.get("init_timestamp", 0) or 0)
                except Exception:
                    init_ts = 0.0
                age_sec = max(0.0, now - init_ts) if 0 < init_ts <= now else 0.0
                freezes_opens, freezes_swaps = classify_pending_freeze(
                    self._is_confirming(chan), age_sec,
                    stuck_sec=stuck_open_sec,
                    confirming_freeze_sec=confirming_freeze_sec)
                if freezes_swaps:
                    # Blocks everything (the engine's global freeze).
                    pending_channel_count += 1
                elif freezes_opens:
                    # Blocks only a further open; swaps proceed.
                    pending_open_freeze_count += 1
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
                spendable_local_sat=self._channel_sendable_sat(lnworker, chan),
                is_active=chan.is_active(),
                has_unsettled_htlcs=has_unsettled,
                unsettled_is_swap=unsettled_is_swap,
                is_plugin_opened=chan.channel_id.hex() in plugin_opened,
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
            pending_open_freeze_count=pending_open_freeze_count,
            inflight_swap_count=inflight_swap_count,
            opens_last_24h=self._count_actions_last_24h(wallet, "open", now),
            closing_channel_count=closing_channel_count,
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

    @ignore_exceptions
    @log_exceptions
    async def _on_offers_event(self, *args) -> None:
        """``swap_offers_changed`` handler -- ignored while we are discovering.

        Split out of ``_on_wallet_event`` for one reason: this is the only
        trigger the plugin can raise at itself. Our own ``_swap_session`` makes
        Electrum emit one of these per provider announcement, so treating them
        as news meant every evaluation scheduled the next one and the plugin
        never rested (re-connecting nine relays every few seconds indefinitely).
        Events arriving while any of our sessions is open are therefore dropped;
        anything else is a genuine outside discovery and is handled as normal.
        """
        if self._open_session_count() > 0:
            return
        await self._on_wallet_event(*args)

    async def _debounced_evaluate(self, wallet: 'Abstract_Wallet') -> None:
        await asyncio.sleep(1.0)  # collect a burst of events before acting
        self._eval_pending[wallet] = False
        await self._evaluate_rate_limited(wallet)

    # --- automatic-evaluation rate limit ----------------------------------
    def _auto_eval_wait_sec(self, wallet: 'Abstract_Wallet') -> float:
        """Seconds until an automatic evaluation of ``wallet`` may next run.

        Purely the *time* limb of the rate limit: 0.0 means "the window has
        elapsed". A material state change bypasses a nonzero wait -- that is
        decided in ``_evaluate_rate_limited``, not here, so this stays a cheap
        clock read usable from the status display.
        """
        last = self._per_wallet_store("_last_eval_at").get(wallet)
        if last is None:
            return 0.0  # never evaluated; nothing to wait for
        interval = self._per_wallet_store("_auto_eval_interval").get(
            wallet, self._min_interval_sec())
        return max(0.0, (last + interval) - time.monotonic())

    def _min_interval_sec(self) -> float:
        return float(getattr(self, "_min_auto_eval_interval_sec",
                             MIN_AUTO_EVAL_INTERVAL_SEC))

    def _max_interval_sec(self) -> float:
        return float(getattr(self, "_max_auto_eval_interval_sec",
                             MAX_AUTO_EVAL_INTERVAL_SEC))

    def _retry_sec(self) -> float:
        return float(getattr(self, "_trailing_retry_sec", TRAILING_EVAL_RETRY_SEC))

    async def _evaluate_rate_limited(self, wallet: 'Abstract_Wallet') -> None:
        """Run an event-driven evaluation now, or defer it to the window's end.

        The deferral is a *delay*, never a drop: the trigger is honoured by a
        single trailing run, and further triggers inside the same window
        coalesce onto that one. Only event-driven triggers come through here --
        the heartbeat, a manual run, and the post-grace one-shot call
        ``_evaluate`` directly.
        """
        wait = self._auto_eval_wait_sec(wallet)
        if wait > 0 and not self._material_state_changed(wallet):
            self._schedule_trailing_evaluation(wallet, wait)
            return
        if self._evaluation_in_flight(wallet):
            # ``_evaluate`` early-returns when the per-wallet lock is held, so
            # calling it now would consume this trigger and do nothing with it --
            # and the running tick may have snapshotted before whatever we are
            # reacting to. Queue it instead, and leave any already-queued run be.
            self._schedule_trailing_evaluation(wallet, max(wait, self._retry_sec()))
            return
        # Running now supersedes anything queued: cancel it rather than letting
        # it fire a redundant second evaluation moments later.
        self._cancel_trailing_evaluation(wallet)
        await self._evaluate(wallet)

    def _evaluation_in_flight(self, wallet: 'Abstract_Wallet') -> bool:
        """Whether an evaluation currently holds this wallet's lock."""
        lock = self.wallets.get(wallet)
        return lock is not None and hasattr(lock, "locked") and lock.locked()

    def _material_state_changed(self, wallet: 'Abstract_Wallet') -> bool:
        """Whether the wallet's position differs from the last evaluation's.

        This is what keeps the rate limit from costing inbound liquidity: a
        received Lightning payment shifts a channel's balance from remote to
        local, which lands in the signature, so it evaluates immediately however
        long the backoff had grown. Only a tick that is *provably* a repeat gets
        deferred.

        Defensive on purpose -- it is consulted on every event burst and must
        never be the thing that stops the plugin evaluating. Any failure reads
        as "changed", which errs toward running.
        """
        sig = self._current_state_sig(wallet)
        return sig is None or sig != self._per_wallet_store("_last_state_sig").get(wallet)

    def _current_state_sig(self, wallet: 'Abstract_Wallet') -> Optional[tuple]:
        """This wallet's signature right now, or ``None`` if it cannot be read.

        Builds the snapshot *inside* the guard: reading wallet state can fail
        transiently (a db mid-write, a wallet being torn down), and a failure
        here must read as "assume it changed" rather than propagate into the
        caller and abort the evaluation it was only meant to schedule.
        """
        try:
            return self._safe_material_state_sig(self.build_snapshot(wallet, None),
                                                 self.read_config())
        except Exception as e:
            self.logger.debug(f"could not read state for the signature: {e!r}")
            return None

    def _safe_material_state_sig(self, snapshot, config) -> Optional[tuple]:
        """``_material_state_sig``, or ``None`` if it cannot be computed.

        The signature is an optimisation -- it decides whether a tick may be
        deferred -- so it must never be the thing that breaks a tick. Anything
        unexpected (a snapshot shape this version does not know, a config that
        is not a dataclass) reads as ``None``, which every caller treats as
        "assume it changed" and therefore evaluates.
        """
        try:
            return self._material_state_sig(snapshot, config)
        except Exception as e:
            self.logger.debug(f"could not compute state signature: {e!r}")
            return None

    def _safe_watchdogs_pending(self, snapshot) -> bool:
        """``_watchdogs_pending``, defaulting to True if it cannot be read.

        Errs toward keeping the check rate up: a snapshot we cannot interpret is
        a poor reason to start skipping ticks that watchdogs depend on.
        """
        try:
            return self._watchdogs_pending(snapshot)
        except Exception as e:
            self.logger.debug(f"could not read watchdog state: {e!r}")
            return True

    @staticmethod
    def _watchdogs_pending(snapshot: LiquiditySnapshot) -> bool:
        """Whether some time-based watchdog is currently counting down.

        Each of these is a state the plugin is actively waiting out, and each is
        advanced only by an evaluation running (see ``_note_evaluation_outcome``
        for why that matters):

          * a channel still opening -- the stuck/wedged-open watchdog, which
            needs several checks spread over time before it will remediate;
          * a channel closing -- tracked to completion;
          * a swap in flight -- the stuck-swap reconciliation and freeze escape;
          * a channel that is not active -- its peer may be gone, and the
            offline auto-close watchdog samples peer uptime once per tick, so
            starving ticks directly corrupts the uptime metric it decides on.

        Derived from the no-transport snapshot the tick already built, so this
        costs nothing extra.
        """
        if (snapshot.pending_channel_count or snapshot.pending_open_freeze_count
                or snapshot.closing_channel_count or snapshot.inflight_swap_count):
            return True
        return any(not ch.is_active for ch in snapshot.channels)

    @staticmethod
    def _material_state_sig(snapshot: LiquiditySnapshot,
                            config: LiquidityConfig) -> tuple:
        """A signature of everything that means "there may be something to do".

        Deliberately narrower than the whole snapshot. The fee-ish fields
        (``swap_percentage_fee``, ``swap_mining_fee_sat``, ``swap_claim_fee_sat``
        and the provider offers) are excluded because they drift on their own --
        the claim fee tracks live on-chain fee estimates and moves with the
        mempool -- so including them would mean "changed" on nearly every tick
        and the backoff would never engage. That is safe: a fee movement alone
        cannot create liquidity work, and the heartbeat re-prices everything on
        its own cadence regardless.

        The config *is* included, so editing a threshold in the settings dialog
        takes effect on the next tick instead of waiting out a backoff.
        """
        return (
            snapshot.onchain_spendable_sat,
            tuple((ch.channel_id, ch.capacity_sat, ch.local_sat, ch.remote_sat,
                   ch.spendable_local_sat, ch.is_active, ch.has_unsettled_htlcs,
                   ch.unsettled_is_swap, ch.is_plugin_opened)
                  for ch in snapshot.channels),
            snapshot.pending_channel_count,
            snapshot.pending_open_freeze_count,
            snapshot.inflight_swap_count,
            snapshot.closing_channel_count,
            snapshot.opens_last_24h,
            dataclasses.astuple(config),
        )

    def _schedule_trailing_evaluation(self, wallet: 'Abstract_Wallet',
                                      delay: float) -> None:
        """Queue the one evaluation a deferred trigger is honoured by.

        At most one per wallet: if a run is already queued, this trigger simply
        joins it (it would evaluate the same state at the same moment anyway).
        """
        tasks = self._per_wallet_store("_trailing_eval_tasks")
        existing = tasks.get(wallet)
        if existing is not None and not existing.done():
            return
        async def _wait_then_evaluate() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            # Clear our own slot *before* evaluating, so the run we are about to
            # start does not see itself as "a trailing run to cancel".
            self._per_wallet_store("_trailing_eval_tasks").pop(wallet, None)
            if wallet not in self.wallets:
                return  # stopped while we waited
            if self._evaluation_in_flight(wallet):
                # Still busy. Re-arm rather than call ``_evaluate``, which would
                # bounce off the lock and drop the trigger we are carrying.
                self._schedule_trailing_evaluation(wallet, self._retry_sec())
                return
            await self._evaluate(wallet)
        tasks[wallet] = asyncio.ensure_future(_wait_then_evaluate())

    def _cancel_trailing_evaluation(self, wallet: 'Abstract_Wallet') -> None:
        """Drop the queued trailing run, from whichever thread we are on.

        The heartbeat is a ``concurrent.futures.Future`` (from
        ``run_coroutine_threadsafe``) and can be cancelled from anywhere, but
        this is a plain ``asyncio.Task`` and ``Task.cancel()`` is NOT
        thread-safe. ``stop_wallet`` reaches here from the GUI thread via the
        ``close_wallet`` hook, so hop onto the loop that owns the task when we
        are not already running on it.
        """
        task = self._per_wallet_store("_trailing_eval_tasks").pop(wallet, None)
        if task is None or task.done():
            return
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False       # no running loop here => another thread
        if not on_loop:
            loop = getattr(getattr(wallet, "network", None), "asyncio_loop", None)
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                    return
                except RuntimeError:
                    pass          # loop already closed; fall through
        task.cancel()

    def _note_evaluation_outcome(self, wallet: 'Abstract_Wallet', *,
                                 changed: bool, acted: bool,
                                 manual: bool, watchdogs_pending: bool = False) -> None:
        """Fold this tick's outcome into the rate limit.

        A tick that found a changed wallet, took an action, or was asked for by
        hand resets the spacing to the floor -- the user is engaged, or the
        situation is moving, and either way the next check should be prompt. A
        tick that found the same state it found last time and did nothing about
        it doubles the spacing (capped), because repeating it sooner would
        produce the same nothing at the cost of another round of relay
        connections.

        ``watchdogs_pending`` pins the spacing to the floor even on an inert
        tick, and it is load-bearing. Every time-based watchdog -- the wedged /
        stuck open, the offline-peer uptime sampling, the stuck-swap
        reconciliation -- only advances *when a tick runs*, and several need
        several ticks spread over time before they will act. Those watchdogs
        also run against a wallet that is, by definition, quiet: a dead peer
        stops producing events, and a wedged open sits at identical balances
        tick after tick. That is precisely the state this backoff would read as
        "nothing is happening" and stretch toward the cap -- starving the
        watchdog exactly when it matters. So while one is counting down, we keep
        checking at the floor. The storm this rate limit exists to stop had no
        watchdog pending at all (healthy channels, no pending open, no swap in
        flight), so nothing about that case regresses.
        """
        intervals = self._per_wallet_store("_auto_eval_interval")
        floor = self._min_interval_sec()
        if changed or acted or manual or watchdogs_pending:
            intervals[wallet] = floor
            return
        current = intervals.get(wallet, floor)
        grown = min(current * AUTO_EVAL_BACKOFF_FACTOR, self._max_interval_sec())
        intervals[wallet] = grown
        if grown != current:
            self.logger.info(
                f"{wallet.basename()}: nothing changed and nothing to do; "
                f"next automatic check in ~{format_duration(grown)}")

    async def _evaluate(self, wallet: 'Abstract_Wallet', *, manual: bool = False) -> None:
        """Evaluate `wallet` and take any warranted action.

        Every automatic trigger (wallet events via `_debounced_evaluate`, the
        heartbeat, the post-grace one-shot, the arm-switch kick via
        `request_evaluation`) routes here with `manual=False`, so a single "manual
        run only" guard below gates them all. A user-initiated "Run now" passes
        `manual=True` to bypass that guard, and also to skip the *time-based* limb
        of the readiness gate -- it still respects the master switch, and still
        requires a server connection and a synced wallet."""
        lock = self.wallets.get(wallet)
        if lock is None:
            return
        if lock.locked():
            return  # an evaluation / action is already in flight; the next event re-checks
        async with lock:
            # Whatever happens below -- an early return through one of the gates,
            # an exception, or a full run -- the status must end up on a terminal
            # state, or the tab would sit forever on the step we died in. The
            # config is captured as we read it so the terminal state can name the
            # right reason (disabled / manual-only / settling / sleeping).
            config: Optional[LiquidityConfig] = None
            try:
                # Re-assert the channel-funding floor every tick so the user's
                # min_onchain value always wins, even if some code path reset the
                # constant. Done before the automation gate so a lowered floor also
                # applies to manual opens while automation is paused.
                self._enforce_min_funding_floor()
                config = self.read_config()
                if not config.automation_enabled:
                    return
                # "Manual run only": the user keeps the plugin armed but wants no
                # unattended action. Every automatic trigger is a no-op; only an
                # explicit "Run now" (manual=True) gets past here. Placed after the
                # master-switch gate so a manual run still needs automation enabled
                # to move funds (the rules engine also gates on that).
                if not manual and self._manual_run_only():
                    self.logger.debug(
                        f"{wallet.basename()} manual-run-only: skipping automatic evaluation")
                    return
                # Startup/shutdown race guard: until the wallet is ready
                # (server connected, wallet synced, and the startup window
                # elapsed), defer ALL automatic action. Acting earlier risks
                # faulting a healthy-but-not-yet-connected peer, opening/swapping
                # on a partial UTXO set, or firing a swap whose Lightning leg
                # cannot route yet. A later tick (events, the heartbeat, or the
                # scheduled post-grace evaluation) re-checks. A manual "Run now"
                # skips the startup window only.
                block = self._readiness_block(wallet, manual=manual)
                if block is not None:
                    self.logger.debug(
                        f"{wallet.basename()} not ready ({block}); deferring "
                        f"evaluation")
                    if block == BLOCK_LOCKED:
                        # The one deferral the user can act on immediately, and
                        # the one that never clears on its own. Tell the GUI so
                        # it can offer to unlock; headless ignores it.
                        self.on_wallet_locked(wallet)
                    return
                # Resolve any reverse swaps we were watching (funded -> success,
                # never funded -> stuck fault) before snapshotting, so the
                # penalties folded into this tick's offers are up to date.
                self._set_status(wallet, "reconciling pending swaps")
                self._reconcile_pending_swaps(wallet)
                # Pay out any dev fee that has accrued past the batch threshold
                # (runs as a guarded background task; never blocks this tick).
                self._maybe_pay_dev_fee(wallet)
                # Watchdog: fault/force-close force-closed or wedged-open channels
                # so a bad peer can't wedge automation and is de-prioritised next
                # time we pick a partner.
                self._set_status(wallet, "checking channel health")
                self._scan_channel_health(wallet)
                # Watchdog: sample peer uptime and cooperatively-/force-close
                # plugin-opened channels whose peer has gone offline for good.
                self._set_status(wallet, "checking for offline peers")
                self._scan_offline_autoclose(wallet)
                # First pass with no provider data: cheap, and tells us whether a
                # swap is even on the table. Only if one is do we pay to open a
                # nostr session (connect relays + discover providers); channel
                # opens and idle/frozen ticks need no provider at all.
                self._set_status(wallet, "reading wallet state")
                base = self.build_snapshot(wallet, None)
                # Record that this tick ran, and against which state, so the rate
                # limit has something to measure from. Done here rather than at
                # the end because the work below can take minutes (a swap, a
                # channel open), and the spacing we care about is between
                # evaluations *starting*, not between one finishing and the next
                # starting -- the latter would let a slow tick be followed
                # immediately by another.
                sig = self._safe_material_state_sig(base, config)
                sigs = self._per_wallet_store("_last_state_sig")
                changed = sig is None or sig != sigs.get(wallet)
                if sig is not None:
                    sigs[wallet] = sig
                self._per_wallet_store("_last_eval_at")[wallet] = time.monotonic()
                if self._swap_may_be_needed(base, config):
                    self._set_status(wallet, "discovering swap providers")
                    async with self._swap_session(wallet) as transport:
                        snapshot = self.build_snapshot(wallet, transport)
                        acted = await self._run_decision(wallet, snapshot, config, transport)
                else:
                    acted = await self._run_decision(wallet, base, config, None)
                self._note_evaluation_outcome(
                    wallet, changed=changed, acted=acted, manual=manual,
                    watchdogs_pending=self._safe_watchdogs_pending(base))
            except Exception as e:
                # Never let an evaluation error escape (it would surface as an
                # unhandled asyncio task exception); just log and wait for the
                # next trigger.
                self.logger.error(f"liquidity evaluation failed: {e!r}", exc_info=e)
                self._diag_event(wallet, category="error", kind="evaluation",
                                 reason="evaluation failed",
                                 detail=f"{type(e).__name__}: {e}")
            finally:
                self._set_status(wallet, self._terminal_status(wallet, config, manual=manual))

    async def _run_decision(self, wallet: 'Abstract_Wallet', snapshot: LiquiditySnapshot,
                            config: LiquidityConfig,
                            transport: Optional['SwapServerTransport']) -> bool:
        """Run the rules engine on a snapshot, log declines, and execute actions.

        Returns whether anything was actually executed. The rate limit uses that
        to tell a productive tick from an inert one: a tick that acted is
        followed promptly (its action changes state, which is worth re-reading),
        while one that did nothing feeds the idle backoff.
        """
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
                self._set_status(wallet, "resolving channel partners")
                res = await self._resolve_partners_detailed(wallet)
                # Always log the arithmetic, not only when it comes out empty:
                # "3 candidates, 2 suggestions dropped by the guard" is exactly
                # as useful as "0 candidates" when tuning the partner settings.
                self.logger.info(f"channel partners: {len(res.candidates)} candidate(s) "
                                 f"({self._partner_breakdown(wallet, res)})")
                if not res.candidates:
                    declines.append(await self._no_partner_decline(wallet, action, res))
                    continue
                executable.append((action, list(res.candidates)))
            elif isinstance(action, CloseChannelAction):
                # Closing a channel is irreversible and costs a mining fee, and
                # the ONLY reason to do it here is to reopen bigger. So verify a
                # partner would actually be there to reopen to -- otherwise the
                # close destroys inbound liquidity and buys nothing. The closing
                # channel's own peer is exempted from the one-channel-per-peer
                # guard: it is about to be freed by this very close, and leaving
                # it excluded would let the guard veto every replacement on a
                # small peer set.
                self._set_status(wallet, "resolving channel partners")
                peer = normalize_node_id(
                    self._channel_peer_node_id(wallet, action.channel_id))
                res = await self._resolve_partners_detailed(
                    wallet, ignore_peers=frozenset([peer]) if peer else frozenset())
                self.logger.info(
                    f"replacement partners for {action.short_id}: "
                    f"{len(res.candidates)} candidate(s) "
                    f"({self._partner_breakdown(wallet, res)})")
                if not res.candidates:
                    declines.append(DeclineRecord(
                        kind="close", channel_id=action.channel_id,
                        short_id=action.short_id, amount_sat=action.capacity_sat,
                        reason=(f"channel {action.short_id} is below the liquidity "
                                f"goal and blocking a bigger one, but no channel "
                                f"partner is available to reopen to; leaving it "
                                f"alone rather than losing its inbound for nothing"),
                        detail="partner resolution: "
                               + self._partner_breakdown(wallet, res)))
                    continue
                executable.append((action, None))
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
        return bool(executable)

    @staticmethod
    def _swap_may_be_needed(snapshot: LiquiditySnapshot, config: LiquidityConfig) -> bool:
        """True if some active channel is over a swap trigger (so it's worth
        opening a provider session). Opens, idle channels, and frozen ticks need
        no provider, so we skip the network round-trip for them.

        Shares the engine's ``over_swap_trigger`` rather than re-deriving the
        comparison: this pre-pass decides whether to pay for provider discovery
        at all, so if it disagreed with the engine about what "over a trigger"
        means, a channel the engine would swap could never get a provider."""
        if snapshot.pending_channel_count or snapshot.inflight_swap_count:
            return False
        return any(ch.is_active and over_swap_trigger(ch, config) is not None
                   for ch in snapshot.channels)

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
        # Mute `swap_offers_changed` for the lifetime of this session. Discovery
        # here makes Electrum emit one of those per provider announcement, and
        # treating our own echo as news is what made the plugin evaluate
        # continuously (see _on_offers_event). Incremented around BOTH transport
        # kinds and released in `finally`, so an exception mid-session cannot
        # leave the plugin permanently deaf to real offer events.
        self._open_swap_sessions = self._open_session_count() + 1
        try:
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
        finally:
            self._open_swap_sessions = max(0, self._open_session_count() - 1)

    def _open_session_count(self) -> int:
        """How many nostr swap sessions we currently hold open. Read through
        ``getattr`` for the same reason as ``_per_wallet_store``: a harness-built
        plugin has no ``__init__``, and muting an event must never be able to
        break the session it is muting for."""
        return int(getattr(self, "_open_swap_sessions", 0) or 0)

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
        elif isinstance(action, LiquiditySinkAction):
            await self._liquidity_sink_drain(wallet, action, state, transport)
        elif isinstance(action, ReverseSwapAction):
            await self._reverse_swap(wallet, action, state, transport)
        elif isinstance(action, CloseChannelAction):
            self._close_undersized_channel(wallet, action, state)

    # --- actions ----------------------------------------------------------
    async def _open_channel(self, wallet: 'Abstract_Wallet', action: OpenChannelAction,
                            state: Optional[Dict] = None,
                            candidates: Optional[List[str]] = None) -> None:
        lnworker = wallet.lnworker
        # Ordered candidates: preferred partners first (in the user's order), then
        # up to MAX_SUGGESTED_PARTNERS of Electrum's suggested peers (unless
        # strict). Banned peers -- and, under the one-channel-per-peer guard, peers
        # we already have a channel with -- are excluded. Normally pre-resolved by
        # the caller (_run_decision, which turns an empty result into a decline);
        # re-resolve as a fallback for direct/test callers.
        if candidates is None:
            candidates = await self._resolve_channel_partners(wallet)
        if not candidates:
            self.logger.warning(
                "no channel partner available (already have a channel with every "
                "reachable peer, or no preferred/suggested peer); skipping open")
            return
        password = self._get_password(wallet)
        last_error: Optional[Exception] = None
        total = len(candidates)
        for index, connect_str in enumerate(candidates, start=1):
            partner = self._abbrev(normalize_node_id(connect_str)) or connect_str[:16]
            self._set_status(
                wallet, f"connecting to partner {partner} ({index} of {total})")
            try:
                peer = await lnworker.lnpeermgr.add_peer(connect_str)
            except Exception as e:
                self.logger.info(
                    f"could not connect to partner {connect_str[:24]}…: {e!r}; trying next")
                # Couldn't even reach the peer: a *soft* fault (transient
                # unreachability) -- de-prioritise, but don't count toward auto-ban.
                self._bookkeep(
                    "peer fault", self._record_peer_fault,
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
            self._set_status(
                wallet, f"opening channel with {partner} ({funding_sat:,} sat)")
            try:
                chan, funding_tx = await lnworker.open_channel_with_peer(
                    peer, funding_sat, push_sat=0, password=password)
            except Exception as e:
                size_rejection = is_channel_size_rejection(str(e))
                self.logger.warning(
                    f"channel open to {connect_str[:24]}… failed: {e!r}; trying next"
                    + (" (channel-size rejection; not faulting the peer)"
                       if size_rejection else ""))
                # We connected but the open negotiation failed. Normally a *hard*
                # fault (counts toward auto-ban), charged to the peer we reached --
                # EXCEPT when the rejection was purely about the channel size
                # (our funding amount vs. a local bound or the peer's min/max
                # policy). That is not a reliability signal: the peer is healthy,
                # it just does not want a channel this size, so it keeps a clean
                # record and its place in the try-order. Walking up to
                # MAX_SUGGESTED_PARTNERS peers per tick makes this matter -- a
                # blanket hard fault would auto-ban swathes of the graph over a
                # few ticks for something that was never their fault.
                if not size_rejection:
                    self._bookkeep(
                        "peer fault", self._record_peer_fault,
                        wallet, peer.pubkey.hex(),
                        f"channel open failed: {type(e).__name__}", hard=True)
                self._diag_event(wallet, category="error", kind="open",
                                 reason=("channel open rejected on size"
                                         if size_rejection else "channel open failed"),
                                 dest=peer.pubkey.hex(),
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
            # The channel now EXISTS -- funds are committed on-chain. Everything
            # below is record-keeping about it, and none of it may be allowed to
            # raise past here: an exception would abort the executor after the
            # open, leaving the plugin with no record of a channel it just paid
            # for (and, in the loop's older shape, moving on to open another).
            # A clean open clears the peer's penalty at its source.
            self._bookkeep("peer success", self._record_peer_success,
                           wallet, peer.pubkey.hex())
            # Count this open toward the rolling-24h open ceiling.
            self._bookkeep("open against the daily ceiling",
                           self._record_action_event, wallet, "open")
            self._bookkeep(
                "open in the decision log", self._log_action,
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

    def _resolve_swap_attempts(self, action: ReverseSwapAction,
                               transport: Optional['SwapServerTransport'],
                               ) -> List[Tuple[ProviderAttempt, Optional['SwapOffer']]]:
        """The ordered providers to try for one swap: the engine's chosen provider
        followed by its ranked failovers, each paired with the live offer that
        addresses it.

        Providers that have stopped advertising since the decision was made are
        dropped here rather than wasting an attempt slot. An empty result means
        there is nobody left to try -- the caller then returns WITHOUT arming the
        cooldown, so a re-evaluation can pick this channel up as soon as offers
        come back, exactly as the single-provider path always did.

        In legacy/URL mode (empty npub) there is one attempt with no offer: the
        swap manager already points at the configured provider.
        """
        planned = [ProviderAttempt(npub=action.provider_npub,
                                   amount_sat=action.lightning_amount_sat)]
        planned.extend(action.alternates)
        out: List[Tuple[ProviderAttempt, Optional['SwapOffer']]] = []
        for attempt in planned:
            if not attempt.npub:
                # Legacy / URL mode: the single configured provider, no offer to
                # resolve. It has no failovers, so this is always the only entry.
                if not (self.config.SWAPSERVER_NPUB or self.config.SWAPSERVER_URL):
                    self.logger.warning("no swap provider configured; skipping reverse swap")
                    continue
                out.append((attempt, None))
                continue
            offer = transport.get_offer(attempt.npub) if transport is not None else None
            if offer is None:
                self.logger.warning(
                    f"provider {attempt.npub[:12]}… no longer advertising; "
                    f"skipping it for the swap on {action.short_id}")
                continue
            out.append((attempt, offer))
        return out

    async def _reverse_swap(self, wallet: 'Abstract_Wallet', action: ReverseSwapAction,
                            state: Optional[Dict] = None,
                            transport: Optional['SwapServerTransport'] = None) -> None:
        """Drain one channel via a reverse swap, failing over between providers.

        The engine ranks every provider that passes the cost gate; we try them in
        that order until one commits. "Commits" is the safety-critical line: an
        attempt that fails BEFORE Electrum's ``add_reverse_swap`` has moved no
        funds and can be safely retried elsewhere, while an attempt that may have
        started its Lightning payment must never be retried on this channel --
        that would drain it twice. See ``_attempt_reverse_swap`` for how each
        failure is classified.
        """
        from contextlib import nullcontext
        sm = wallet.lnworker.swap_manager
        # Cooldown: don't re-attempt a channel we just acted on.
        now = time.monotonic()
        until = self._swap_cooldown_until.get(action.channel_id, 0.0)
        if now < until:
            return
        attempts = self._resolve_swap_attempts(action, transport)
        if not attempts:
            # Every candidate vanished (or none configured): nothing was tried, so
            # leave the channel free to be re-evaluated immediately.
            return
        self._swap_cooldown_until[action.channel_id] = now + SWAP_COOLDOWN_SEC
        self.logger.info(
            f"reverse swap for {action.short_id}: {len(attempts)} provider(s) to try "
            f"(cap {MAX_SWAP_PROVIDER_ATTEMPTS}): {action.reason}")

        # Reuse the evaluation's open session when present; otherwise open a
        # transient transport (e.g. URL mode without a prior session).
        own_transport = transport is None
        session = sm.create_transport() if own_transport else nullcontext(transport)
        # Wall-clock budget for the whole cascade, checked between attempts only.
        deadline = now + SWAP_CASCADE_DEADLINE_SEC
        total = len(attempts)
        async with session as tr:
            for index, (attempt, offer) in enumerate(attempts, start=1):
                if time.monotonic() >= deadline:
                    remaining = total - index + 1
                    self.logger.warning(
                        f"swap cascade for {action.short_id} hit its "
                        f"{SWAP_CASCADE_DEADLINE_SEC:.0f}s deadline with {remaining} "
                        f"provider(s) untried; waiting for the next cycle")
                    self._diag_event(
                        wallet, category="error", kind="swap",
                        reason="swap provider cascade deadline reached",
                        source=attempt.npub,
                        detail=f"{remaining} provider(s) untried after {index - 1} attempt(s)")
                    break
                outcome = await self._attempt_reverse_swap(
                    wallet, action, attempt, offer, sm, tr, state,
                    index=index, total=total)
                if outcome is not _SwapAttempt.NEXT:
                    break
                if index < total:
                    self.logger.info(
                        f"failing over to the next provider for {action.short_id} "
                        f"({index + 1} of {total})")

    async def _attempt_reverse_swap(self, wallet: 'Abstract_Wallet',
                                    action: ReverseSwapAction,
                                    attempt: ProviderAttempt,
                                    offer: Optional['SwapOffer'],
                                    sm: Any, tr: Any, state: Optional[Dict],
                                    *, index: int, total: int) -> '_SwapAttempt':
        """One provider's attempt at the swap. Returns what the cascade should do
        next -- see :class:`_SwapAttempt`.

        Every failure arm below fires BEFORE Electrum's ``add_reverse_swap``
        (submarine_swaps.py), i.e. before any Lightning payment exists, so
        returning NEXT from them risks no funds. The one ambiguous arm is the
        coarse timeout backstop, which can fire either side of that line; it is
        resolved by asking whether a swap object actually appeared.
        """
        from electrum.util import UserFacingException
        from electrum.submarine_swaps import SwapServerError
        npub = attempt.npub
        provider_label = npub or "configured provider"
        of_n = f" ({index} of {total})" if total > 1 else ""
        cost_note = ("" if attempt.all_in_cost_pct is None
                     else f", all-in cost {attempt.all_in_cost_pct:.3f}%")
        self.logger.info(
            f"reverse swap via {provider_label[:20]}…{of_n}: "
            f"{attempt.amount_sat} sat{cost_note}")
        self._set_status(
            wallet,
            f"attempting swap with {self._abbrev(provider_label) or provider_label}"
            f"{of_n} ({attempt.amount_sat:,} sat from channel {action.short_id})")
        if offer is not None:
            if hasattr(tr, "target_pubkey"):
                tr.target_pubkey = offer.server_pubkey
                # Let the transport resolve the chosen provider before we trigger
                # its relay-following loop via update_pairs() just below.
                if hasattr(tr, "target_npub"):
                    tr.target_npub = npub or None
            # Make sm.get_recv_amount / the cost sanity checks use THIS provider's
            # advertised terms. Re-applied on every attempt, so a failover never
            # inherits the previous provider's economics. Also sets
            # sm.is_initialized, which the wait below depends on.
            sm.update_pairs(offer.pairs)
        try:
            await asyncio.wait_for(sm.is_initialized.wait(), timeout=15)
        except asyncio.TimeoutError:
            # Unreachable provider is a reliability fault (timeout signal).
            self.logger.warning(
                f"swap provider {provider_label[:20]}… not reachable; skipping it")
            self._record_provider_fault(wallet, npub, "not reachable (init timeout)")
            return _SwapAttempt.NEXT
        lightning_amount_sat = attempt.amount_sat
        expected_onchain_sat = sm.get_recv_amount(lightning_amount_sat, is_reverse=True)
        # get_recv_amount() returns None when this amount isn't swappable with
        # the chosen provider (outside its min/max bounds, or it nets below
        # dust after fees). The max_forward cap fix above should keep the
        # planner from picking such an amount, but guard regardless: feeding
        # None into reverse_swap() crashes its cost sanity-check (int - None).
        # This is "wait for the channel to grow / retry", NOT a provider fault.
        # It IS provider-specific, though -- another provider's bounds may well
        # host this amount -- so the cascade moves on rather than giving up.
        if expected_onchain_sat is None or sm.mining_fee is None:
            self.logger.info(
                f"skipping reverse swap of {lightning_amount_sat} sat: provider "
                f"{provider_label[:20]}… cannot host this amount right now "
                f"(no receivable amount)")
            self._diag_event(wallet, category="error", kind="swap",
                             reason="amount not swappable with provider", source=npub,
                             detail=f"{lightning_amount_sat} sat")
            return _SwapAttempt.NEXT
        prepayment_sat = 2 * sm.mining_fee
        # Pin the reverse swap's Lightning payment to the channel the engine
        # chose to drain. Without this, lnworker routes the payment over ANY
        # channel with enough outbound -- so when several channels share a
        # peer, a swap planned to drain channel X can instead drain channel Y,
        # leaving X permanently over its trigger while we keep re-issuing (and
        # mis-attributing) swaps for it. Resolve defensively: if the channel
        # can't be looked up, fall back to unpinned routing (old behaviour)
        # rather than abort the swap.
        target_channels = None
        try:
            if action.channel_id:
                chan = wallet.lnworker.get_channel_by_id(
                    bytes.fromhex(action.channel_id))
                if chan is not None:
                    target_channels = [chan]
        except Exception as e:  # noqa: BLE001
            self.logger.info(
                f"could not resolve channel {action.short_id} to pin the swap "
                f"payment ({e!r}); routing unpinned")
        reverse_swap_kwargs = dict(
            transport=tr,
            lightning_amount_sat=lightning_amount_sat,
            expected_onchain_amount_sat=expected_onchain_sat,
            prepayment_sat=prepayment_sat,
        )
        if target_channels is not None:
            reverse_swap_kwargs["channels"] = target_channels
        # Snapshot the swap set so we can identify the swap we are about to
        # create and, if it never funds, attribute the stall to this provider.
        # This also decides the ONE ambiguous failover question: a swap object
        # here means Electrum reached add_reverse_swap and fired the payment.
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
                sm.reverse_swap(**reverse_swap_kwargs),
                timeout=self._reverse_swap_timeout_sec,
            )
        except UserFacingException as e:
            # e.g. the provider deems the swap uneconomical for this amount.
            # This is a legitimate response, NOT a reliability fault. It is raised
            # by _sanity_check_swap_costs, the very first statement of
            # reverse_swap, so nothing has moved -- and since the verdict depends
            # on THIS provider's quoted amount, another one may well accept.
            self.logger.info(
                f"provider {provider_label[:20]}… declined reverse swap of "
                f"{lightning_amount_sat} sat: {scrub_text(e)}")
            self._diag_event(wallet, category="error", kind="swap",
                             reason="provider declined reverse swap", source=npub,
                             detail=f"{lightning_amount_sat} sat: {e}")
            return _SwapAttempt.NEXT
        except asyncio.TimeoutError as e:
            # Either the createswap RPC reply never arrived (bounded in the
            # transport) or the whole attempt exceeded the coarse backstop
            # while its Lightning payment was in flight. Both mean an
            # unresponsive provider -> a genuine reliability fault, so
            # escalate normally. If a swap object was already created (the
            # payment leg had started before the backstop fired), track it so
            # reconciliation still attributes its eventual outcome (funded ->
            # success, never funds -> stuck) instead of silently losing it.
            #
            # That same check decides whether we may fail over. A swap object
            # exists => Electrum passed add_reverse_swap and our Lightning
            # payment may be in flight; draining this channel again through
            # another provider could double-spend the outbound we just committed,
            # so the cascade STOPS. No swap object => the stall was before any
            # payment existed and the next provider is safe to try.
            committed = bool(set(getattr(sm, "_swaps", {}).keys()) - swaps_before)
            self._track_new_swaps(wallet, sm, swaps_before, npub, action,
                                  expected_onchain_sat)
            self.logger.warning(
                f"reverse swap timed out [DO NOT TRUST]: {e!r}"
                + (" (a swap was already created; not failing over)" if committed
                   else " (no swap created; safe to try the next provider)"))
            self._record_provider_fault(wallet, npub, f"RPC timeout: {type(e).__name__}")
            self._diag_event(wallet, category="error", kind="swap",
                             reason="reverse swap RPC timed out", source=npub,
                             detail=f"{type(e).__name__}: {e}")
            return _SwapAttempt.COMMITTED if committed else _SwapAttempt.NEXT
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
            #
            # createswap is rejected before any invoice is paid, and a rejection
            # is that provider's own capacity verdict -- precisely the case
            # failover exists for, so try the next one now rather than next cycle.
            self.logger.info(
                f"provider {provider_label[:20]}… rejected reverse swap of "
                f"{lightning_amount_sat} sat (likely transient capacity) "
                f"[DO NOT TRUST]: {e!r}")
            self._record_provider_fault(
                wallet, npub, "swap rejected (likely transient capacity)", soft=True)
            self._diag_event(wallet, category="error", kind="swap",
                             reason="provider rejected reverse swap (transient?)",
                             source=npub, detail=f"{type(e).__name__}: {e}")
            return _SwapAttempt.NEXT
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
                #
                # A cheat is the strongest possible reason to prefer somebody
                # else, and nothing was paid, so fail over immediately.
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
                return _SwapAttempt.NEXT
            # Any other exception is our-side, not the provider misbehaving: a
            # stale local tip or too-close locktime relative to OUR height, or a
            # genuine bug (e.g. a bad argument to reverse_swap). Log it with a
            # traceback and record it as an internal error -- do NOT penalise
            # the provider's reliability for our own condition (a healthy
            # provider must not be de-prioritised or banned because of it).
            #
            # Whether to fail over splits this arm in two. A marker from
            # _REVERSE_SWAP_OUR_SIDE_MARKERS is provider-independent (our tip is
            # stale): every other provider would fail identically, so abort and
            # let the next cycle retry once the condition clears. Everything else
            # here is either provider-influenced (a too-close locktime, which the
            # provider chose) or an unknown fault. A too-close locktime is worth
            # another provider; an unknown one is not -- we cannot show it left no
            # funds committed, and the conservative reading of an unrecognised
            # error is to stop touching this channel until we understand it.
            our_side = any(m in str(e) for m in _REVERSE_SWAP_OUR_SIDE_MARKERS)
            retryable = (not our_side) and any(
                m in str(e) for m in _REVERSE_SWAP_RETRYABLE_MARKERS)
            self.logger.error(
                f"reverse swap failed (internal/our-side): {e!r}"
                + ("; provider-independent, aborting the cascade" if our_side
                   else "; trying the next provider" if retryable
                   else "; unrecognised, aborting the cascade"),
                exc_info=True)
            self._diag_event(wallet, category="error", kind="swap",
                             reason="reverse swap internal error", source=npub,
                             detail=f"{type(e).__name__}: {e}")
            return _SwapAttempt.NEXT if retryable else _SwapAttempt.ABORT
        if funding_txid:
            # The provider created the on-chain funding output: it honoured the
            # swap. Count it as a success straight away (the claim is Electrum's
            # job from here). This is a confirmed completion, so accrue the dev fee
            # now, on the net on-chain amount received.
            self._record_provider_success(wallet, npub)
            self._accrue_dev_fee(wallet, expected_onchain_sat, source=action.short_id)
        else:
            # No funding txid. Electrum's reverse_swap races the Lightning payment
            # against funding detection and returns whichever finishes FIRST
            # (submarine_swaps.py: asyncio.wait(..., FIRST_COMPLETED) then
            # `return swap.funding_txid`). So None does not mean "accepted, funding
            # pending" -- it means the payment task won the race, and because
            # lnworker.pay_invoice RETURNS (success, log) rather than raising, the
            # dominant cause is a payment that FAILED (it gives up at
            # LNWallet.PAYMENT_TIMEOUT = 120s).
            #
            # Track it either way -- reconciliation still owns the eventual verdict
            # -- then ask which arm of the race we are in before deciding whether
            # the cascade may continue.
            tracked = self._track_new_swaps(wallet, sm, swaps_before, npub, action,
                                            expected_onchain_sat)
            if self._unfunded_swap_outcome(wallet, sm, tracked) is _SwapAttempt.NEXT:
                # The payment is definitively dead and nothing is committed on this
                # channel, so another provider is safe to try -- this is exactly
                # what the cascade exists for.
                #
                # Both sides are at fault here, but only the PROVIDER is charged
                # from this arm. The peer side is already owned by
                # ``_reconcile_pending_swaps``, which sees this very swap on the
                # next tick, recognises the failed payment and charges the peer
                # then; doing it here as well would count one failure twice
                # against the same peer. The provider had no such path -- that
                # branch resolves and drops the record without ever faulting it --
                # so this is the only place the provider's share can land.
                #
                # Soft, because attribution is genuinely ambiguous: our peer could
                # not route it, and the provider being offline or out of inbound
                # would look identical from here. A signal this ambiguous should
                # weigh on the ranking, not escalate anyone toward a ban.
                self.logger.warning(
                    f"reverse swap via {provider_label[:20]}… produced no funding: "
                    f"the Lightning payment failed (no funds committed); "
                    f"trying the next provider")
                self._record_provider_fault(
                    wallet, npub, "reverse-swap Lightning payment failed", soft=True)
                self._diag_event(
                    wallet, category="error", kind="swap",
                    reason="reverse-swap Lightning payment failed", source=npub,
                    detail=(f"{lightning_amount_sat} sat from channel "
                            f"{action.short_id} (attempt {index} of {total})"))
                return _SwapAttempt.NEXT
            # Either an HTLC may still be live, or we could not tell. Both mean the
            # channel may already be committed, so stop: draining it again through
            # another provider is the one outcome worth giving up the tick to avoid.
        self.logger.info(f"reverse swap funding txid: {funding_txid}")
        self._log_action(
            wallet, kind="swap", amount_sat=lightning_amount_sat,
            source=action.short_id, dest="on-chain",
            reason=action.reason,
            detail=(f"funding txid {funding_txid}; expected on-chain {expected_onchain_sat} sat; "
                    f"provider {provider_label}"
                    + (f" (attempt {index} of {total})" if index > 1 else "")),
            state=state)
        self.on_action_done(
            wallet, _("Reverse swap {} sat from {}").format(lightning_amount_sat, action.short_id))
        return _SwapAttempt.COMMITTED

    def _track_new_swaps(self, wallet: 'Abstract_Wallet', sm, swaps_before: set,
                         npub: str, action: ReverseSwapAction,
                         expected_onchain_sat: int) -> Set[str]:
        """Track any reverse swap the swap manager gained during this attempt
        (``sm._swaps`` keys not present in ``swaps_before``) for later
        reconciliation. Stashes the channel's peer so a failed Lightning payment
        can be attributed to it, and the expected on-chain amount so the dev fee
        is accrued iff the swap later completes. Used both on the unfunded path
        (``reverse_swap`` returned no txid, whatever the cause) and on a timeout
        that fired after the swap object was already created (its payment leg was
        in flight).

        Returns the payment hashes (hex) it tracked, so the caller can ask what
        became of their Lightning payments -- see ``_unfunded_swap_outcome``."""
        new_swaps = set(getattr(sm, "_swaps", {}).keys()) - swaps_before
        if not new_swaps:
            return set()
        peer_node_id = self._channel_peer_node_id(wallet, action.channel_id)
        for ph_hex in new_swaps:
            self._track_pending_swap(wallet, ph_hex, npub,
                                     node_id=peer_node_id, channel_id=action.channel_id,
                                     fee_basis_sat=expected_onchain_sat)
        return new_swaps

    def _unfunded_swap_outcome(self, wallet: 'Abstract_Wallet', sm,
                               tracked: Set[str]) -> '_SwapAttempt':
        """Whether a reverse swap that returned no funding txid left this channel
        free (NEXT) or possibly committed (COMMITTED).

        This is the swap-path analogue of the liquidity sink's in-flight gate, and
        the safety-critical half of the failover decision. ``reverse_swap``
        returning None means its Lightning payment task finished before any
        funding appeared; that is USUALLY a failed payment, but "the pay task
        returned" is not by itself proof that nothing is committed -- ``pay_invoice``
        can return after ``pay_to_node`` gave up while HTLCs it sent are still
        unresolved. Failing over then could drain the channel twice.

        So a swap only clears for failover when BOTH hold for every payment it
        involves: nothing is in ``inflight_payments``, and Electrum's own derived
        status says the payment definitively failed. Anything else -- in flight,
        undecided, unparseable, or no swap object to inspect at all -- answers
        COMMITTED, because the cost of a false "all clear" is a double drain while
        the cost of a false alarm is one skipped cascade.

        The prepayment (``minerFeeInvoice``) is checked alongside the main invoice:
        it is a separate fire-and-forget payment, and a live prepay HTLC is still
        something committed on this channel.
        """
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None or not tracked:
            # No swap object was created, or no wallet to ask. We cannot show the
            # channel is clear, so treat it as committed.
            return _SwapAttempt.COMMITTED
        for ph_hex in tracked:
            hashes: List[bytes] = []
            try:
                hashes.append(bytes.fromhex(ph_hex))
            except ValueError:
                return _SwapAttempt.COMMITTED
            # Include the prepayment hash when the swap carries one.
            try:
                swap = sm.get_swap(bytes.fromhex(ph_hex))
                prepay_hash = getattr(swap, "prepay_hash", None) if swap else None
                if prepay_hash:
                    hashes.append(prepay_hash)
            except Exception as e:  # noqa: BLE001
                self.logger.info(
                    f"could not read the prepay hash for swap {ph_hex[:10]}… "
                    f"({e!r}); not failing over")
                return _SwapAttempt.COMMITTED
            if any(self._payment_still_inflight(lnworker, h) for h in hashes):
                return _SwapAttempt.COMMITTED
            if not self._ln_payment_failed(wallet, ph_hex):
                # Not in flight, but not provably failed either -- e.g. it settled
                # and the funding txid simply has not landed yet. Do not touch it.
                return _SwapAttempt.COMMITTED
        return _SwapAttempt.NEXT

    # --- liquidity sink ---------------------------------------------------
    # Draining a channel by PAYING somebody, instead of reverse-swapping on-chain.
    # The liquidity effect is identical -- local balance leaves the channel and
    # inbound capacity comes back -- but it settles in seconds, needs no provider,
    # no on-chain claim tx and no cost gate, and the sats land in whatever account
    # the address belongs to rather than in this wallet's on-chain balance.
    #
    # The engine plans the ladder (``LiquiditySinkAction.amounts``); everything
    # here is the I/O: resolving the address, minting one invoice per rung, and
    # paying it pinned to the channel being drained.
    async def _liquidity_sink_drain(self, wallet: 'Abstract_Wallet',
                                    action: LiquiditySinkAction,
                                    state: Optional[Dict] = None,
                                    transport: Optional['SwapServerTransport'] = None) -> None:
        """Drain one channel into the liquidity sink, falling back to a reverse
        swap if the sink cannot do it.

        Cooldown ownership is the subtle part. ``_reverse_swap`` arms the
        per-channel cooldown itself and returns early if it is already armed, so
        arming it here before delegating would silently cancel the fallback --
        the exact bug that would make "swaps as a backstop" a lie. So the cooldown
        is armed here only on the paths that do NOT delegate; when we do delegate,
        the swap path owns it.
        """
        now = time.monotonic()
        if now < self._swap_cooldown_until.get(action.channel_id, 0.0):
            return
        self.logger.info(
            f"liquidity sink for {action.short_id}: {len(action.amounts)} amount(s) "
            f"to try, {action.amounts[0] if action.amounts else 0} sat first "
            f"({action.reason})")
        outcome = await self._pay_liquidity_sink(wallet, action, state)
        if outcome in (_SinkAttempt.PAID, _SinkAttempt.ABORT):
            # Drained, or possibly-committed and therefore untouchable: either
            # way this channel is done for now.
            self._swap_cooldown_until[action.channel_id] = time.monotonic() + SWAP_COOLDOWN_SEC
            return
        if action.swap_fallback is None:
            self.logger.info(
                f"liquidity sink could not drain {action.short_id} and no reverse "
                f"swap is available as a fallback; waiting for the next cycle")
            self._diag_event(
                wallet, category="error", kind="sink",
                reason="liquidity sink failed with no swap fallback",
                dest=action.address,
                detail=f"{len(action.amounts)} amount(s) tried on {action.short_id}")
            self._swap_cooldown_until[action.channel_id] = time.monotonic() + SWAP_COOLDOWN_SEC
            return
        self.logger.info(
            f"liquidity sink exhausted for {action.short_id}; falling back to a "
            f"reverse swap of {action.swap_fallback.lightning_amount_sat} sat")
        self._diag_event(
            wallet, category="error", kind="sink",
            reason="liquidity sink failed; falling back to a reverse swap",
            dest=action.address,
            detail=f"{len(action.amounts)} amount(s) tried on {action.short_id}")
        await self._reverse_swap(wallet, action.swap_fallback, state, transport)

    def _sink_rungs_within_bounds(self, amounts: Sequence[int],
                                  lnurl_data: 'LNURL6Data') -> List[int]:
        """The engine's ladder reshaped to what this endpoint will actually
        accept.

        The engine sizes the ladder against the CHANNEL; the endpoint has its own
        min/max sendable, which the engine cannot know (it is discovered over the
        network, and the engine does no I/O). So the top rung is clamped down to
        ``max_sendable`` and anything under ``min_sendable`` is dropped -- which
        can raise the effective floor above SINK_MIN_PAYMENT_SAT, or empty the
        ladder entirely (the caller then falls back to a swap).

        Clamping can make the top rung equal the one below it, so the result is
        deduplicated: paying the identical amount twice would be a wasted attempt
        that tells us nothing new.
        """
        lo = max(int(lnurl_data.min_sendable_sat), 1)
        hi = int(lnurl_data.max_sendable_sat)
        out: List[int] = []
        for amount in amounts:
            capped = min(int(amount), hi)
            if capped < lo or capped in out:
                continue
            out.append(capped)
        return out

    def _sink_fee_budget(self, amount_sat: int):
        """The routing-fee budget for a sink payment: the user's existing
        ``max_swap_fee_pct`` ceiling, applied to the amount being sent.

        The same knob governs both drain routes on purpose -- it is the answer to
        "how much am I willing to pay to convert outbound into inbound", and a
        sink payment converts exactly what a reverse swap does. Note the ceiling
        is strict: a route costing more is not taken, which surfaces as a payment
        failure and drops the ladder to its next rung. A ceiling of 0 therefore
        permits only zero-fee routes (a direct channel to the sink's node).
        """
        from electrum.lnutil import PaymentFeeBudget
        pct = max(0.0, float(getattr(self.config,
                                     "INBOUND_LIQUIDITY_MAX_SWAP_FEE_PCT", 0.9)))
        cap_msat = int(amount_sat * 1000 * pct / 100.0)
        return PaymentFeeBudget.from_invoice_amount(
            invoice_amount_msat=amount_sat * 1000, config=self.config,
            max_fee_msat=cap_msat)

    @staticmethod
    def _payment_still_inflight(lnworker, payment_hash: bytes) -> bool:
        """Whether a payment we just gave up on may still have an HTLC committed.

        This is the safety gate on the whole halving loop, and the direct analogue
        of the ``add_reverse_swap`` commit boundary on the swap path. A failed
        ``pay_invoice`` does NOT prove nothing was sent: ``pay_to_node`` raises
        PaymentFailure once it passes ``LNWallet.PAYMENT_TIMEOUT`` (120s) or runs
        out of attempts, and its ``finally`` clause deliberately keeps the
        paysession alive when HTLCs have not resolved. If we halved and paid again
        while the first HTLC were still live and it later settled, the channel
        would be drained by both -- past the outbound floor the user configured,
        and for more than the plugin ever decided to send.

        Best-effort by design: any lookup failure conservatively answers True
        (stop), because the cost of a false "all clear" is a double drain while the
        cost of a false alarm is one skipped tick.
        """
        try:
            return payment_hash in lnworker.get_payments(status='inflight')
        except Exception as e:  # noqa: BLE001
            _logger.info(
                f"could not determine whether the sink payment is still in "
                f"flight ({e!r}); assuming it is")
            return True

    async def _pay_liquidity_sink(self, wallet: 'Abstract_Wallet',
                                  action: LiquiditySinkAction,
                                  state: Optional[Dict]) -> '_SinkAttempt':
        """Walk the ladder until a payment settles. Returns what the caller should
        do next -- see :class:`_SinkAttempt`."""
        lnworker = getattr(wallet, "lnworker", None)
        if lnworker is None:
            self.logger.info("liquidity sink skipped: wallet has no lnworker")
            return _SinkAttempt.UNUSABLE
        # Resolve the endpoint ONCE for the whole ladder: every rung mints a fresh
        # invoice from the same callback URL, and re-resolving per rung would add a
        # DNS+TLS round trip to each retry for no new information.
        try:
            lnurl_data = await self._lnurl_pay_params(action.address,
                                                      purpose="liquidity sink")
        except Exception as e:  # noqa: BLE001
            # Unreachable endpoint, TLS failure, malformed reply. Halving cannot
            # fix any of these -- the amount was never the problem.
            self.logger.warning(
                f"liquidity sink {scrub_text(action.address, max_len=80)} could not "
                f"be resolved: {e!r}")
            self._diag_event(wallet, category="error", kind="sink",
                             reason="liquidity sink address could not be resolved",
                             dest=action.address, detail=f"{type(e).__name__}: {e}")
            return _SinkAttempt.UNUSABLE
        if lnurl_data is None:
            self._diag_event(wallet, category="error", kind="sink",
                             reason="liquidity sink is not a usable LNURL-pay endpoint",
                             dest=action.address, detail=None)
            return _SinkAttempt.UNUSABLE
        rungs = self._sink_rungs_within_bounds(action.amounts, lnurl_data)
        if not rungs:
            self.logger.info(
                f"liquidity sink cannot host any amount on this channel: ladder "
                f"{action.amounts[0] if action.amounts else 0}..."
                f"{action.amounts[-1] if action.amounts else 0} sat vs endpoint "
                f"bounds {lnurl_data.min_sendable_sat}..{lnurl_data.max_sendable_sat} sat")
            self._diag_event(
                wallet, category="error", kind="sink",
                reason="liquidity sink accepts no amount this channel can send",
                dest=action.address,
                detail=(f"endpoint accepts {lnurl_data.min_sendable_sat}.."
                        f"{lnurl_data.max_sendable_sat} sat"))
            return _SinkAttempt.UNUSABLE
        # Pin the payment to the channel the engine chose to drain -- without this
        # lnworker routes over ANY channel with enough outbound, so a drain planned
        # for channel X can empty channel Y instead, leaving X over its trigger
        # forever. Same reasoning (and same defensive fallback) as the swap path.
        chan = None
        try:
            if action.channel_id:
                chan = wallet.lnworker.get_channel_by_id(bytes.fromhex(action.channel_id))
        except Exception as e:  # noqa: BLE001
            self.logger.info(
                f"could not resolve channel {action.short_id} to pin the sink "
                f"payment ({e!r}); routing unpinned")
        deadline = time.monotonic() + SINK_CASCADE_DEADLINE_SEC
        total = len(rungs)
        for index, amount in enumerate(rungs, start=1):
            if time.monotonic() >= deadline:
                remaining = total - index + 1
                self.logger.warning(
                    f"liquidity sink ladder for {action.short_id} hit its "
                    f"{SINK_CASCADE_DEADLINE_SEC:.0f}s deadline with {remaining} "
                    f"amount(s) untried")
                self._diag_event(
                    wallet, category="error", kind="sink",
                    reason="liquidity sink ladder deadline reached",
                    dest=action.address,
                    detail=f"{remaining} amount(s) untried after {index - 1} attempt(s)")
                break
            outcome = await self._attempt_sink_payment(
                wallet, action, lnurl_data, amount, chan, lnworker, state,
                index=index, total=total)
            if outcome is not _SinkAttempt.NEXT:
                return outcome
            if index < total:
                self.logger.info(
                    f"halving the liquidity-sink payment for {action.short_id}: "
                    f"trying {rungs[index]} sat next ({index + 1} of {total})")
        return _SinkAttempt.NEXT

    async def _attempt_sink_payment(self, wallet: 'Abstract_Wallet',
                                    action: LiquiditySinkAction,
                                    lnurl_data: 'LNURL6Data', amount_sat: int,
                                    chan, lnworker, state: Optional[Dict],
                                    *, index: int, total: int) -> '_SinkAttempt':
        """One rung: mint an invoice for ``amount_sat`` and pay it."""
        of_n = f" ({index} of {total})" if total > 1 else ""
        self.logger.info(
            f"paying liquidity sink{of_n}: {amount_sat} sat from channel "
            f"{action.short_id}")
        self._set_status(
            wallet,
            f"paying liquidity sink{of_n} ({amount_sat:,} sat from channel "
            f"{action.short_id})")
        try:
            invoice = await self._lnurl_invoice(lnurl_data, amount_sat,
                                                purpose="liquidity sink")
        except Exception as e:  # noqa: BLE001
            self.logger.warning(
                f"liquidity sink would not issue an invoice for {amount_sat} sat: {e!r}")
            self._diag_event(wallet, category="error", kind="sink",
                             reason="liquidity sink would not issue an invoice",
                             dest=action.address,
                             detail=f"{amount_sat} sat: {type(e).__name__}: {e}")
            return _SinkAttempt.UNUSABLE
        if invoice is None:
            # No invoice, or one for the wrong amount. Both are the endpoint
            # misbehaving rather than the amount being unroutable, and an endpoint
            # that does this at one amount will do it at half that amount too.
            self._diag_event(wallet, category="error", kind="sink",
                             reason="liquidity sink returned no usable invoice",
                             dest=action.address, detail=f"{amount_sat} sat")
            return _SinkAttempt.UNUSABLE
        try:
            payment_hash = bytes.fromhex(invoice.rhash)
        except Exception:  # noqa: BLE001
            payment_hash = b""
        kwargs: Dict[str, Any] = {"budget": self._sink_fee_budget(amount_sat)}
        if chan is not None:
            kwargs["channels"] = [chan]
        try:
            success, _log = await asyncio.wait_for(
                lnworker.pay_invoice(invoice, **kwargs),
                timeout=self._sink_payment_timeout_sec)
        except asyncio.TimeoutError:
            # Past our coarse backstop, which sits above Electrum's own payment
            # timeout -- so this is a wedge below pay_invoice, not a slow route.
            # Whether we may halve is decided entirely by the in-flight check.
            self.logger.warning(
                f"liquidity-sink payment of {amount_sat} sat timed out after "
                f"{self._sink_payment_timeout_sec:.0f}s")
            self._diag_event(wallet, category="error", kind="sink",
                             reason="liquidity-sink payment timed out",
                             dest=action.address, detail=f"{amount_sat} sat")
            success = False
        except Exception as e:  # noqa: BLE001
            # An unrecognised failure out of pay_invoice. We cannot show it left
            # nothing committed, so the conservative reading applies: stop.
            self.logger.error(
                f"liquidity-sink payment of {amount_sat} sat failed: {e!r}",
                exc_info=True)
            self._diag_event(wallet, category="error", kind="sink",
                             reason="liquidity-sink payment raised", dest=action.address,
                             detail=f"{amount_sat} sat: {type(e).__name__}: {e}")
            return (_SinkAttempt.ABORT
                    if payment_hash and self._payment_still_inflight(lnworker, payment_hash)
                    else _SinkAttempt.UNUSABLE)
        if success:
            self.logger.info(
                f"liquidity-sink payment of {amount_sat} sat from {action.short_id} "
                f"settled to {action.address}")
            # A settled drain is a completed conversion of outbound into inbound,
            # which is what the dev fee is charged on -- here on the amount sent,
            # the sink's equivalent of a swap's net on-chain receipt.
            self._accrue_dev_fee(wallet, amount_sat, source=action.short_id)
            self._log_action(
                wallet, kind="sink", amount_sat=amount_sat,
                source=action.short_id, dest=action.address,
                reason=action.reason,
                detail=(f"paid {amount_sat} sat to {action.address}"
                        + (f" (attempt {index} of {total})" if index > 1 else "")),
                state=state)
            self.on_action_done(
                wallet, _("Paid liquidity sink {} sat from {}").format(
                    amount_sat, action.short_id))
            return _SinkAttempt.PAID
        # Failed. Halving is only safe once we know this attempt left nothing
        # committed on the channel -- see _payment_still_inflight.
        if payment_hash and self._payment_still_inflight(lnworker, payment_hash):
            self.logger.warning(
                f"liquidity-sink payment of {amount_sat} sat failed but an HTLC may "
                f"still be in flight; NOT retrying or swapping on {action.short_id}")
            self._diag_event(
                wallet, category="error", kind="sink",
                reason="liquidity-sink payment failed with an HTLC possibly in flight",
                dest=action.address,
                detail=f"{amount_sat} sat on {action.short_id}; cascade stopped")
            return _SinkAttempt.ABORT
        self.logger.info(
            f"liquidity-sink payment of {amount_sat} sat did not settle "
            f"(nothing in flight){of_n}")
        self._diag_event(wallet, category="error", kind="sink",
                         reason="liquidity-sink payment did not settle",
                         dest=action.address,
                         detail=f"{amount_sat} sat from {action.short_id}")
        return _SinkAttempt.NEXT

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

    async def _suggest_peers(self, wallet: 'Abstract_Wallet',
                             limit: int = MAX_SUGGESTED_PARTNERS) -> List[bytes]:
        """Up to ``limit`` DISTINCT peer suggestions from Electrum, best-effort.

        Must be awaited, and deliberately does its work in a worker thread: in
        gossip mode ``LNWallet.suggest_peer()`` reaches
        ``LNRater.maybe_analyze_graph()`` -> ``Network.run_from_another_thread()``,
        which asserts it is NOT called from the asyncio thread (and would deadlock
        on its own loop even without the assert). Every caller here runs inside the
        evaluation coroutine *on* that loop, so the call is offloaded with
        ``asyncio.to_thread``. Calling it inline is the bug this fixes: the
        AssertionError was swallowed and every open silently declined for "no
        reachable channel partner".

        ``suggest_peer()`` yields ONE node per call (rating-weighted random in
        gossip mode, a random trampoline node otherwise), so distinct suggestions
        are gathered by calling it repeatedly inside a single thread hop -- bounded
        by ``limit``, by an attempt ceiling, and by a run of consecutive duplicates
        (a small trampoline set repeats immediately; no point spinning).

        A failure is logged, remembered in ``_suggest_errors`` for the decline
        reason, and surfaced as a diagnostics event -- never silently discarded.
        """
        max_attempts = max(1, limit * 3)
        max_consecutive_dupes = 3

        def _collect() -> List[bytes]:
            # Runs OFF the asyncio loop -- see the docstring.
            out: List[bytes] = []
            seen: set = set()
            dupes = 0
            for _ in range(max_attempts):
                node_id = wallet.lnworker.suggest_peer()
                if not node_id:
                    break  # Electrum has nothing (more) to suggest.
                if node_id in seen:
                    dupes += 1
                    if dupes >= max_consecutive_dupes:
                        break
                    continue
                dupes = 0
                seen.add(node_id)
                out.append(node_id)
                if len(out) >= limit:
                    break
            return out

        try:
            suggestions = await asyncio.to_thread(_collect)
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            self.logger.warning(f"suggested-peer lookup failed: {e!r}", exc_info=e)
            self._suggest_errors[wallet] = detail
            self._diag_event(wallet, category="error", kind="partners",
                             reason="suggested-peer lookup failed", detail=detail)
            return []
        self._suggest_errors.pop(wallet, None)
        return suggestions

    def _routing_mode(self, wallet: 'Abstract_Wallet') -> str:
        """"trampoline" or "gossip" -- which of the two very different peer-
        suggestion paths ``lnworker.suggest_peer()`` will take (a random hardcoded
        trampoline node vs. an LNRater ranking of the gossip graph). Worth naming
        in the diagnostics: on a network with no hardcoded trampoline nodes
        (regtest) the first path returns nothing at all, while the second returns
        nothing until the graph has been analysed."""
        lnworker = getattr(wallet, "lnworker", None)
        try:
            return "trampoline" if lnworker.uses_trampoline() else "gossip"
        except Exception:
            return "unknown"

    async def _resolve_partners_detailed(self, wallet: 'Abstract_Wallet',
                                         *, apply_peer_guard: bool = True,
                                         ignore_peers: frozenset = frozenset()
                                         ) -> PartnerResolution:
        """:meth:`_resolve_channel_partners` plus the per-stage counts that
        explain the result (see :class:`PartnerResolution`).

        ``ignore_peers`` (normalized pubkeys) are dropped from the
        one-channel-per-peer exclusion set, i.e. treated as peers we do NOT have
        a channel with. Used by the undersized-channel replacement pre-check,
        which must ask "will a partner be available AFTER this close?" -- the
        channel being closed still exists while we decide, so its own peer would
        otherwise be excluded by the guard and could veto the very close that is
        about to free it. Only that peer is exempted; every other held peer stays
        excluded, so the guard is relaxed exactly as far as the close justifies.
        """
        preferred = _parse_partner_list(self.config.INBOUND_LIQUIDITY_PREFERRED_PARTNERS)
        banned = _parse_banned_partners(self.config.INBOUND_LIQUIDITY_BANNED_PARTNERS)
        strict = bool(self.config.INBOUND_LIQUIDITY_PARTNERS_STRICT)
        exclude: frozenset = frozenset()
        if apply_peer_guard and bool(self.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER):
            exclude = self._current_peer_node_ids(wallet) - frozenset(ignore_peers)
        suggested: List[str] = []
        if not strict:
            suggested = [node_id.hex() for node_id in await self._suggest_peers(wallet)]
        # Sink flaky peers in the try-order (soft de-prioritisation); banned peers
        # are already excluded above. Auto-banned serial offenders never get here.
        penalties = self._peer_penalties(wallet)
        return resolve_channel_partners(preferred, banned, suggested, strict=strict,
                                        penalties=penalties, exclude=exclude)

    def _partner_breakdown(self, wallet: 'Abstract_Wallet',
                           res: PartnerResolution) -> str:
        """One-line, human-readable arithmetic behind a partner resolution.

        This is the text that turns "no reachable channel partner is available"
        from a dead end into a diagnosis: it says how many partners you
        configured, how many Electrum offered, and which rule ate them."""
        parts = [
            f"preferred {res.preferred_kept}/{res.preferred_total}",
            (f"suggested {res.suggested_kept}/{res.suggested_total}"
             if not res.strict else "suggested n/a (strict mode: preferred only)"),
            f"routing {self._routing_mode(wallet)}",
        ]
        if res.banned_hits:
            parts.append(f"{res.banned_hits} banned")
        if res.guard_hits:
            parts.append(f"{res.guard_hits} already have a channel with")
        if res.duplicate_hits:
            parts.append(f"{res.duplicate_hits} duplicate")
        if res.malformed_hits:
            parts.append(f"{res.malformed_hits} unparseable")
        err = self._suggest_errors.get(wallet)
        if err:
            parts.append(f"suggestion lookup failed ({scrub_text(err)})")
        return ", ".join(parts)

    async def _resolve_channel_partners(self, wallet: 'Abstract_Wallet',
                                        *, apply_peer_guard: bool = True) -> List[str]:
        """Ordered connect strings to attempt a channel open against: preferred
        partners first (user order), then up to ``MAX_SUGGESTED_PARTNERS`` of
        Electrum's suggested peers unless strict mode is on. Banned partners (by
        pubkey) are excluded from both.

        Several suggestions -- not just one -- so a peer that refuses the open
        (its own channel-size policy, a stale address, ...) does not waste the
        whole tick: ``_open_channel`` walks the list until one accepts.

        When the one-channel-per-peer guard is on (default), peers we already
        hold a non-closed channel with are excluded too, so the plugin spreads
        capacity across distinct nodes. Pass ``apply_peer_guard=False`` to get
        the pre-guard ordering (used to tell "no partner at all" apart from
        "only the guard blocked us" when reporting a decline)."""
        res = await self._resolve_partners_detailed(wallet, apply_peer_guard=apply_peer_guard)
        return list(res.candidates)

    async def _no_partner_decline(self, wallet: 'Abstract_Wallet',
                                  action: OpenChannelAction,
                                  resolution: Optional[PartnerResolution] = None) -> DeclineRecord:
        """Why an engine-approved open could not proceed: the one-channel-per-peer
        guard eliminated every candidate, the suggestion lookup itself failed, or
        there is simply no reachable partner. Distinguished (by re-resolving
        without the guard, and by the remembered lookup error) so the decision log
        is actionable rather than a dead end.

        ``resolution`` is the (empty) result the caller already computed; pass it
        so the per-stage counts can be appended to the reason without resolving a
        third time. It is optional only for direct/test callers.
        """
        guard_on = bool(self.config.INBOUND_LIQUIDITY_ONE_CHANNEL_PER_PEER)
        if guard_on and await self._resolve_channel_partners(wallet, apply_peer_guard=False):
            reason = ("have funds and room to open, but already hold a channel with "
                      "every available peer (one-channel-per-peer guard); not opening")
        elif self._suggest_errors.get(wallet):
            # The lookup itself broke -- name it, so this reads as "something is
            # wrong" rather than "the network has nobody for you".
            reason = ("have funds and room to open, but asking Electrum for a "
                      f"suggested peer failed ({scrub_text(self._suggest_errors[wallet])}); "
                      "not opening")
        else:
            reason = ("have funds and room to open, but no reachable channel partner "
                      "is available (no preferred/suggested peer); not opening")
        if resolution is None:
            resolution = await self._resolve_partners_detailed(wallet)
        # The arithmetic goes in `detail` rather than the reason so the decline's
        # dedup signature (kind, channel_id, reason) is unchanged: a steady state
        # still logs once, not on every tick.
        detail = "partner resolution: " + self._partner_breakdown(wallet, resolution)
        return DeclineRecord(kind="open", reason=reason, amount_sat=action.funding_sat,
                             detail=detail)

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

    def _signing_unlocked(self, wallet: 'Abstract_Wallet') -> bool:
        """Whether we can sign for ``wallet`` right now.

        Gates on *keystore* encryption specifically, not ``has_password()``:
        only keystore encryption makes signing require a password (see
        ``Abstract_Wallet.has_keystore_encryption``). A wallet whose file is
        storage-encrypted but whose keystore is not -- a hardware wallet, say --
        signs without one, and must not be held back by this gate.

        The password itself comes from Electrum's own unlock cache, which the
        user fills via the Lock/Unlock button in the wallet window. We never
        prompt: this runs on a background evaluation tick the user did not
        initiate, and a modal appearing out of nowhere is both startling and, in
        a headless run, unanswerable. Electrum's own background automation reads
        the same cache (``txbatcher``, ``lnworker``, the swapserver plugin).

        A wallet object that does not implement the password API at all is
        treated as unlocked -- same reasoning as ``_wallet_synced``: assuming the
        blocking answer would stall the plugin forever on a wallet type that
        simply has no such concept. But one that *has* the API and then fails to
        answer is treated as locked, because there we know a keystore is in play
        and refusing to act is the recoverable direction."""
        probe = getattr(wallet, "has_keystore_encryption", None)
        if not callable(probe):
            return True
        try:
            if not probe():
                return True
            return wallet.get_unlocked_password() is not None
        except Exception as e:
            self.logger.info(f"could not read unlock state: {e!r}; treating as locked")
            return False

    def _get_password(self, wallet: 'Abstract_Wallet') -> Optional[str]:
        """The password to sign with, or ``None`` when signing needs none.

        Normally unreachable while locked -- ``_readiness_block`` turns that into
        an early deferral (``BLOCK_LOCKED``) before the tick does any work -- so
        the raise here is a backstop for direct callers, kept so a locked wallet
        can never silently fall through to an unsigned action."""
        if not self._signing_unlocked(wallet):
            self.logger.warning(
                f"{wallet.basename()} is password-protected and locked; "
                f"unlock it in Electrum to allow automated channel opens")
            raise Exception("wallet is locked; unlock it to open a channel")
        if not getattr(wallet, "has_password", lambda: False)():
            return None
        return wallet.get_unlocked_password()

    def on_action_done(self, wallet: 'Abstract_Wallet', message: str) -> None:
        """Hook for GUI subclasses to surface activity. No-op in headless."""
        pass

    def on_wallet_locked(self, wallet: 'Abstract_Wallet') -> None:
        """Fired on every tick that defers because the wallet is locked.

        Hook for GUI subclasses, which can offer to unlock. No-op in headless,
        where there is nobody to ask -- the deferral just repeats until the
        wallet is unlocked out of band (``electrum unlock``).

        Deliberately fired on *every* such tick rather than once: the GUI decides
        how often to actually ask (it rate-limits), and latching here would mean
        a wallet locked again mid-session never prompts."""
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
            "pending_open_freeze_count": snapshot.pending_open_freeze_count,
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
                "min_outbound_sat": config.min_outbound_sat,
                "liquidity_goal_sat": config.liquidity_goal_sat,
                "liquidity_goal_met": liquidity_goal_met(snapshot, config),
                # The sink address itself is an identity, so only whether one is
                # set is dumped; the pair below is what answers "why was my sink
                # not used on this tick".
                "liquidity_sink_configured": bool(config.liquidity_sink_address),
                "defer_sink_until_goal": config.defer_sink_until_goal,
                "disable_submarine_swaps": config.disable_submarine_swaps,
                "manage_plugin_opened_only": config.manage_plugin_opened_only,
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
                    "is_plugin_opened": c.is_plugin_opened,
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
            "detail": decline.detail,
            "state": state or {},
        }
        self._append_log(wallet, entry)

    def _load_log(self, wallet: 'Abstract_Wallet') -> List[Dict]:
        raw = wallet.db.get(LOG_DB_KEY, [])
        # `_plain_json_copy`, not `list(raw)`: entries must come back as plain
        # dicts before they are appended to and written back (see the helper).
        # This store survives a shallow copy today only by accident -- Electrum's
        # `StoredList` happens not to re-wrap its children the way `StoredDict`
        # does -- and that is not a property worth depending on.
        return _plain_json_copy(raw) if isinstance(raw, list) else []

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
        try:
            wallet.db.put(LOG_DB_KEY, entries)
            wallet.save_db()
        except Exception as e:
            self.logger.error(f"could not persist decision log: {e!r}", exc_info=True)
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
