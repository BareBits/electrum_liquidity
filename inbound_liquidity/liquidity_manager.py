# Inbound-liquidity rules engine.
#
# This module is deliberately PURE: it imports nothing from Electrum and does no
# I/O (not even reading the clock). It turns a snapshot of wallet state + the
# user's configured thresholds into a decision: a list of intended actions (open
# a channel / do a reverse swap) plus a record of *why* nothing else was done
# (the "declines"). All the side effects (actually opening channels, talking to
# the swap provider, timestamping/persisting the decision log) live in the
# plugin glue (`__init__.py`). Keeping the decision logic isolated makes it
# fully unit-testable without a running Electrum.
#
# Strategy recap (why these rules manufacture *inbound* liquidity):
#   * A reverse swap (Lightning -> on-chain) drains a channel's LOCAL/outbound
#     balance out to on-chain coins, which restores that channel's *inbound*
#     capacity. This is the core "top up my ability to receive" move.
#   * Opening a channel spends on-chain coins into new capacity; once its local
#     balance is later reverse-swapped out, that capacity becomes pure inbound.
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

# Electrum's hard floor for funding a new channel (lnutil.MIN_FUNDING_SAT).
# Mirrored here so the engine stays import-free; asserted against the real value
# in the e2e wiring.
MIN_FUNDING_SAT: int = 200_000

# Length of the "per day" window for the daily action ceilings. The ceilings are
# enforced as a *rolling* 24h window (no more than N actions in any trailing
# 24 hours), not a calendar day -- so they cannot be sidestepped by a burst
# straddling midnight, and there is no reset boundary to reason about.
DAILY_WINDOW_SEC: float = 86_400.0


# --- untrusted-input hardening --------------------------------------------
# Strings that originate from parties we do not control -- a swap provider's
# nostr announcement, its RPC error replies, a channel peer's id -- must never
# reach a log line, a decision-log ``reason``/``detail`` field, or a terminal
# verbatim: an embedded CR/LF can forge a second log entry, and an ANSI escape
# can rewrite an operator's terminal. These pure helpers (no I/O, no Electrum)
# neutralise such strings and enforce a safe shape on provider/peer identities,
# so the glue can scrub-on-ingest and scrub-on-log. See the write-ups in the
# plugin's SECURITY notes and the callers in ``__init__.py``/``swap_transport.py``.

# Default cap on a scrubbed string's length. Provider error text and the like are
# for human diagnosis only; an unbounded field is itself a (log-flooding) vector.
SCRUB_MAX_LEN: int = 200
_SCRUB_NAMED = {ord("\n"): r"\n", ord("\r"): r"\r", ord("\t"): r"\t"}


def scrub_text(s: Optional[object], *, max_len: int = SCRUB_MAX_LEN) -> str:
    """Return ``s`` with every control character escaped to a visible, inert form
    (``\\n``/``\\r``/``\\t`` for those three, ``\\xNN`` for any other C0/C1 or
    DEL byte), and truncated to ``max_len``.

    Escaping (rather than stripping) preserves the forensic fact that the bytes
    were present while making them harmless: the result can be safely written to
    a shared log file or printed to a terminal. Ordinary printable text --
    including non-ASCII/Unicode -- passes through unchanged. ``None`` becomes
    ``""``; non-strings are coerced via ``str`` first (so a provider exception
    object is scrubbed too)."""
    if s is None:
        return ""
    text = s if isinstance(s, str) else str(s)
    out: List[str] = []
    for ch in text:
        code = ord(ch)
        if code in _SCRUB_NAMED:
            out.append(_SCRUB_NAMED[code])
        elif code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    result = "".join(out)
    if len(result) > max_len:
        result = result[:max_len] + "…(truncated)"
    return result


# A 32-byte value as 64 hex chars: a raw nostr/secp256k1 pubkey (the shape of
# ``SwapOffer.server_pubkey``, which we hand to the transport as a DM address).
_HEX64_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
# A bech32 npub identity: ``npub1`` + a bounded run of the bech32 alphabet. We do
# NOT do full bech32 checksum validation here (that is Electrum/aionostr's job on
# use) -- this is purely a safe-shape gate so a provider-supplied identity used as
# a dict key / log token / list-match cannot smuggle control chars, whitespace, or
# unbounded length. Case is preserved (matching semantics are unchanged); real
# npubs are lowercase.
_NPUB_RE = re.compile(r"\Anpub1[a-zA-Z0-9]{20,120}\Z")


def looks_like_hex_pubkey(s: Optional[str]) -> bool:
    """True iff ``s`` is exactly 64 hex characters (a raw 32-byte pubkey)."""
    return bool(s) and bool(_HEX64_RE.match(s))


def clean_npub(raw: Optional[str]) -> str:
    """A provider's nostr identity if it has a safe npub shape, else ``""``.

    Trims surrounding whitespace and requires the value to match a bounded
    ``npub1…`` bech32 pattern. An unrecognisable identity yields ``""`` so the
    caller can drop the offer without attributing anything to it."""
    s = (raw or "").strip()
    return s if _NPUB_RE.match(s) else ""


def count_within_window(timestamps: Sequence[float], now: float,
                        window_sec: float = DAILY_WINDOW_SEC) -> int:
    """How many of ``timestamps`` fall within the trailing ``window_sec`` ending
    at ``now`` (i.e. ``ts >= now - window_sec``). Pure/clock-free: the caller
    supplies ``now``, so the rolling-window math stays unit-testable."""
    cutoff = now - window_sec
    return sum(1 for ts in timestamps if ts >= cutoff)


def daily_cap_reached(count: int, cap: int) -> bool:
    """Whether a daily action ceiling has been reached.

    ``cap <= 0`` means *unlimited* (the ceiling is disabled), mirroring the
    ``peer_autoban_faults`` convention -- so a user can opt out of a ceiling by
    setting it to 0. A positive cap is reached once ``count`` meets or exceeds it.
    """
    return cap > 0 and count >= cap


# --- optional dev fee -----------------------------------------------------
# The plugin can charge an optional fee to support development, assessed on the
# reverse swaps *it* initiates (LN -> on-chain). The fee is accrued into a
# per-wallet running ledger and paid out to a Lightning address in batches once
# enough has accumulated. These two helpers are the PURE core of that mechanism
# (no clock, no I/O, no Electrum): the glue supplies the amounts and persists the
# ledger. See the accrual/payout wiring in ``__init__.py``.
#
# Rounding: the fee is floored, so we never charge more than the configured
# percentage of the swapped amount (a sub-sat fraction rounds *down*, in the
# operator's favour). At the 0.1% default a 900k-sat swap accrues 900 sat.
def compute_dev_fee(basis_sat: int, pct: float) -> int:
    """The dev fee owed on a single completed swap of ``basis_sat`` (the net
    on-chain amount received) at ``pct`` percent. Floored to whole sats and
    never negative; ``pct <= 0`` (fee disabled) yields 0."""
    if basis_sat <= 0 or pct <= 0:
        return 0
    return int(math.floor(basis_sat * pct / 100.0))


def clamp_dev_fee_pct(pct: float, *, max_pct: float = 5.0) -> float:
    """Clamp a configured dev-fee percentage into the allowed ``[0, max_pct]``
    range, so a hand-edited config can't charge an out-of-bounds fee."""
    if pct < 0.0:
        return 0.0
    if pct > max_pct:
        return max_pct
    return pct


@dataclass(frozen=True)
class DevFeePayoutDecision:
    """Whether to send a dev-fee payout right now, and how much.

    ``amount_sat`` is meaningful only when ``should_pay`` is True. ``reason`` is a
    short human-readable explanation for logs / the decision trail.
    """
    should_pay: bool
    amount_sat: int
    reason: str


def decide_dev_fee_payout(owed_sat: int, paid_last_24h_sat: int, *,
                          threshold_sat: int, daily_cap_sat: int) -> DevFeePayoutDecision:
    """Decide whether to pay out accrued dev fees now.

    Rules (all rolling-24h, matching the action ceilings):
      * Pay only once at least ``threshold_sat`` has accrued (batch small fees
        into one payment rather than dust-spamming the address).
      * Never let payouts in the trailing 24h exceed ``daily_cap_sat`` -- a
        runaway-spend guard. The payable amount is capped to the remaining daily
        headroom; any excess simply stays owed and is paid on a later day
        (carry-forward -- nothing is lost).
      * ``daily_cap_sat <= 0`` disables the cap (unlimited), and
        ``threshold_sat <= 0`` pays as soon as anything is owed.
    """
    if owed_sat < max(threshold_sat, 1):
        return DevFeePayoutDecision(False, 0, "below payout threshold")
    if daily_cap_sat > 0:
        headroom = daily_cap_sat - paid_last_24h_sat
        if headroom <= 0:
            return DevFeePayoutDecision(False, 0, "daily payout cap reached")
        amount = min(owed_sat, headroom)
    else:
        amount = owed_sat
    return DevFeePayoutDecision(True, amount, f"pay {amount} sat of {owed_sat} owed")


# --- offline-channel auto-close: peer-uptime metric -----------------------
# The auto-close watchdog (glue side) closes a *plugin-opened* channel once its
# peer has been effectively gone for a while, escalating to a force-close if the
# channel still hasn't closed after a deadline. "Effectively gone" is judged from
# a rolling, time-weighted **uptime ratio**: the fraction of a trailing window
# during which the peer was reachable. A ratio-over-a-window (rather than a
# single "offline for N hours" edge) is deliberately robust to a flaky peer that
# reconnects for a moment now and then -- such a peer still shows a low uptime
# ratio and is closed, whereas a healthy peer with a brief blip is spared.
#
# The metric is accumulated into coarse (hourly) buckets so it stays bounded and
# JSON-persistable: bucket index -> [online_sec, total_sec]. These helpers are
# PURE (no clock, no I/O) -- the glue reads the wall clock, passes ``now`` in, and
# persists the returned accumulator. A plain-dict representation is used (not a
# dataclass) because the glue round-trips it through ``wallet.db`` as JSON.
UPTIME_BUCKET_SEC: float = 3600.0
# Cap on the time attributed to a single sample interval. Samples are taken on
# the plugin's event ticks, which are frequent while anything is happening but
# can have long gaps when the wallet/daemon is idle or was shut down. Attributing
# a multi-hour gap to the peer's last-seen state would blame the peer for *our*
# downtime, so each interval is capped -- a gap longer than this simply is not
# counted (neither online nor offline), keeping the ratio a fair measure of the
# peer's availability while we were actually watching.
UPTIME_MAX_SAMPLE_GAP_SEC: float = 900.0
# The uptime ratio is only actionable once we have observed at least this
# fraction of the window; below it we return "unknown" so a freshly-opened
# channel (or one watched only briefly) is never closed on thin evidence.
UPTIME_MIN_OBSERVED_FRAC: float = 0.25


def _uptime_window_start_bucket(now: float, window_sec: float,
                                bucket_sec: float) -> int:
    """Index of the oldest bucket still inside the trailing window ending at
    ``now``. Buckets older than this are outside the window and pruned/ignored."""
    return int((now - window_sec) // bucket_sec)


def record_uptime_sample(acc: Optional[Mapping], now: float, online: bool, *,
                         window_sec: float,
                         bucket_sec: float = UPTIME_BUCKET_SEC,
                         max_gap_sec: float = UPTIME_MAX_SAMPLE_GAP_SEC) -> dict:
    """Fold one peer-reachability observation into the uptime accumulator.

    The interval since the previous sample is attributed to the *previous*
    observed state (which held over that interval), capped at ``max_gap_sec`` so
    our own idle/downtime is not charged to the peer, and added into the hourly
    bucket it falls in. Returns a NEW accumulator dict (buckets pruned to the
    trailing window); the input is not mutated. ``acc`` may be ``None``/empty for
    the first sample, which only records the timestamp and state (no interval
    yet).

    Accumulator shape (all JSON-plain): ``{"buckets": {bucket_index_str:
    [online_sec, total_sec]}, "last_ts": float, "last_online": bool}``.
    """
    buckets: dict = {}
    if acc:
        for k, v in (acc.get("buckets") or {}).items():
            try:
                on, tot = float(v[0]), float(v[1])
            except (TypeError, ValueError, IndexError):
                continue
            buckets[int(k)] = [on, tot]
    last_ts = None
    if acc and acc.get("last_ts") is not None:
        try:
            last_ts = float(acc["last_ts"])
        except (TypeError, ValueError):
            last_ts = None
    last_online = bool(acc.get("last_online")) if acc else False

    if last_ts is not None and now > last_ts:
        elapsed = min(now - last_ts, max_gap_sec)
        idx = int(last_ts // bucket_sec)
        on, tot = buckets.get(idx, [0.0, 0.0])
        tot += elapsed
        if last_online:
            on += elapsed
        buckets[idx] = [on, tot]

    # Prune buckets fully outside the trailing window.
    start = _uptime_window_start_bucket(now, window_sec, bucket_sec)
    buckets = {k: v for k, v in buckets.items() if k >= start}
    return {
        "buckets": {str(k): v for k, v in buckets.items()},
        "last_ts": now,
        "last_online": bool(online),
    }


def uptime_ratio(acc: Optional[Mapping], now: float, window_sec: float,
                 bucket_sec: float = UPTIME_BUCKET_SEC
                 ) -> Optional[Tuple[float, float]]:
    """``(online_fraction, observed_fraction)`` over the trailing window, or
    ``None`` if nothing has been observed.

    ``online_fraction`` = online_sec / observed_sec (the peer's availability);
    ``observed_fraction`` = observed_sec / window_sec (how much of the window we
    actually watched -- the confidence gate). Buckets outside the window are
    ignored, so the ratio slides forward with ``now``.
    """
    if not acc:
        return None
    start = _uptime_window_start_bucket(now, window_sec, bucket_sec)
    online_sec = 0.0
    total_sec = 0.0
    for k, v in (acc.get("buckets") or {}).items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if idx < start:
            continue
        try:
            online_sec += float(v[0])
            total_sec += float(v[1])
        except (TypeError, ValueError, IndexError):
            continue
    if total_sec <= 0:
        return None
    observed_frac = min(1.0, total_sec / window_sec) if window_sec > 0 else 0.0
    return (online_sec / total_sec, observed_frac)


def should_commit_offline_close(ratio: Optional[Tuple[float, float]], *,
                                min_uptime_pct: float,
                                min_observed_frac: float = UPTIME_MIN_OBSERVED_FRAC
                                ) -> bool:
    """Whether the peer looks *genuinely gone* -- so the watchdog should commit to
    closing the channel. True only when we have enough observation
    (``observed_fraction >= min_observed_frac``) AND the peer's uptime over the
    window is below ``min_uptime_pct`` percent. ``None`` (unknown) is never a
    commit. Being purely a function of the *current* ratio, it also drives the
    inverse: a recovered peer whose ratio climbs back above the floor stops
    qualifying, so the glue can cancel a not-yet-executed close."""
    if ratio is None:
        return False
    online_frac, observed_frac = ratio
    if observed_frac < min_observed_frac:
        return False
    return online_frac * 100.0 < min_uptime_pct


def deadline_reached(marked_ts: Optional[float], now: float,
                     deadline_sec: float) -> bool:
    """Whether ``deadline_sec`` has elapsed since ``marked_ts`` (the instant the
    close was committed). ``marked_ts`` of ``None`` (not committed) is never
    reached; a non-positive ``deadline_sec`` is reached immediately once
    committed (force-close as soon as we commit)."""
    if marked_ts is None:
        return False
    return (now - marked_ts) >= deadline_sec


# --- wedged channel-open remediation --------------------------------------
# A channel stuck in a pre-OPEN state (PREOPENING/OPENING/FUNDED) may simply be
# waiting for its funding tx to confirm, or its peer may have vanished mid-
# handshake. Only the latter deserves remediation, and remediation is a
# force-close -- irreversible and it costs an on-chain fee -- so the bar is
# deliberately high and made of three independent gates:
#
#   1. ELIGIBILITY: the channel is not making legitimate confirmation progress
#      (see the glue's ``_wedge_eligible``). While the funding tx is still
#      accruing confirmations toward the peer's required depth, nothing is
#      wrong and the wedge clock does not even start.
#   2. AGE: the wedge clock -- measured from the FIRST eligible observation,
#      not from channel creation -- has run past the close timeout. Starting it
#      at first-eligible is what stops a slow-confirming funding tx from
#      consuming the entire budget before the peer is even involved.
#   3. STRIKES: we have seen the channel wedged on several separate
#      observations, spread over real time. Evaluation ticks are event-driven
#      and can fire in bursts (several within a second), so a bare count is
#      nearly free to satisfy; requiring a minimum spread between the first and
#      last strike means only a genuinely persistent wedge qualifies.
#
# Any single non-wedged observation resets the accumulator to nothing, so a peer
# that reconnects and completes the handshake wipes its record entirely.
#
# These helpers are PURE (no clock, no I/O): the glue reads the wall clock,
# passes ``now`` in, and persists the returned accumulator as JSON in wallet.db.

# Number of separate eligible observations required before remediating.
WEDGE_REQUIRED_STRIKES: int = 5
# Minimum wall-clock spread between the first and last strike. Defeats tick
# bursts: five observations one second apart do not clear this.
WEDGE_MIN_SPREAD_SEC: float = 3600.0
# Absolute backstop measured from channel creation. A funding tx that never
# confirms would otherwise stay "legitimately confirming" -- and therefore
# exempt -- forever, leaving the funds locked with nothing to free them.
WEDGE_ABSOLUTE_CAP_SEC: float = 7 * 86400.0


def record_wedge_strike(acc: Optional[Mapping], now: float,
                        eligible: bool) -> Optional[dict]:
    """Fold one wedge observation into the strike accumulator.

    ``eligible`` is the glue's verdict that this channel currently looks wedged
    (pre-OPEN and not making confirmation progress). A False observation clears
    the record entirely by returning ``None`` -- the channel recovered, so its
    history is worthless and must not carry over to a later, unrelated wedge.

    Returns a NEW accumulator (the input is never mutated), shaped
    ``{"strikes": int, "first_ts": float, "last_ts": float}``. ``first_ts`` is
    the wedge clock's origin; ``last_ts`` is only advanced by this call, so the
    spread grows with real time rather than with tick frequency.
    """
    if not eligible:
        return None
    strikes = 0
    first_ts = now
    if acc:
        try:
            strikes = max(0, int(acc.get("strikes", 0) or 0))
        except (TypeError, ValueError):
            strikes = 0
        try:
            raw_first = acc.get("first_ts")
            if raw_first is not None:
                first_ts = float(raw_first)
        except (TypeError, ValueError):
            first_ts = now
        # A stored first_ts in the future means the wall clock moved backwards
        # (NTP correction, a wrong-clock boot). Trusting it would make the
        # channel look arbitrarily young or old; re-anchor on now instead.
        if first_ts > now:
            first_ts = now
    return {"strikes": strikes + 1, "first_ts": first_ts, "last_ts": now}


def wedge_strikes_satisfied(acc: Optional[Mapping], *,
                            required_strikes: int = WEDGE_REQUIRED_STRIKES,
                            min_spread_sec: float = WEDGE_MIN_SPREAD_SEC) -> bool:
    """Whether the strike gate (count AND spread) is cleared. Both conditions
    must hold: enough separate observations, and enough real time between the
    first and the last of them."""
    if not acc:
        return False
    try:
        strikes = int(acc.get("strikes", 0) or 0)
        first_ts = float(acc.get("first_ts", 0.0) or 0.0)
        last_ts = float(acc.get("last_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if strikes < required_strikes:
        return False
    return (last_ts - first_ts) >= min_spread_sec


def wedge_clock_expired(acc: Optional[Mapping], now: float,
                        timeout_sec: float) -> bool:
    """Whether the wedge clock -- running from the first eligible observation --
    has passed ``timeout_sec``. Never true without an accumulator (the channel
    has not been seen wedged even once)."""
    if not acc:
        return False
    try:
        first_ts = float(acc.get("first_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if first_ts <= 0:
        return False
    return (now - first_ts) >= timeout_sec


def should_remediate_wedged_open(acc: Optional[Mapping], now: float, *,
                                 timeout_sec: float,
                                 required_strikes: int = WEDGE_REQUIRED_STRIKES,
                                 min_spread_sec: float = WEDGE_MIN_SPREAD_SEC
                                 ) -> bool:
    """The full remediation verdict: the wedge clock has expired AND the strike
    gate is cleared. Eligibility (gate 1) is the caller's job -- it is what
    decides whether an observation is folded in at all."""
    return (wedge_clock_expired(acc, now, timeout_sec)
            and wedge_strikes_satisfied(acc, required_strikes=required_strikes,
                                        min_spread_sec=min_spread_sec))


# --- cooperative-close-before-force-close ---------------------------------
# INVARIANT: the plugin never force-closes a channel without first giving a
# cooperative close a genuine chance. A force-close is expensive (a mining fee),
# slow to redeem (CSV timelocks) and irreversible, whereas a cooperative close
# is cheap and immediate -- so a force-close is only ever correct when
# cooperation was tried and did not work, or was impossible because the peer
# could not be reached at all.
#
# The glue cannot simply await a cooperative close inline: it is an async
# negotiation that can take minutes, while the watchdogs are synchronous scans.
# So the attempt is launched, recorded, and the force-close escalation is
# deferred to a later tick -- gated on this predicate.

# How long cooperation gets before a force-close may escalate. Also the window a
# never-reachable peer gets to come back before we accept that cooperation is
# impossible.
COOP_BEFORE_FORCE_WINDOW_SEC: float = 600.0
# How many cooperative closes to attempt before accepting that the peer will not
# cooperate. Each attempt gets its own full window, so this bounds the total
# cooperation phase at roughly COOP_MAX_ATTEMPTS * COOP_BEFORE_FORCE_WINDOW_SEC.
# A bound is essential: without one, a peer that stays reachable but refuses to
# cooperate would earn a fresh attempt (and a fresh window) on every tick, and
# the force-close backstop would never fire at all.
COOP_MAX_ATTEMPTS: int = 3


def coop_before_force_ready(attempt: Optional[Mapping], now: float, *,
                            window_sec: float = COOP_BEFORE_FORCE_WINDOW_SEC
                            ) -> bool:
    """Whether a cooperative close has had its fair chance, so a force-close may
    now proceed. Two ways to qualify, both requiring ``window_sec`` to elapse:

      * we attempted a cooperative close and the channel still has not closed
        (``last_coop_ts`` set) -- the peer refused, stalled, or vanished mid-
        negotiation; or
      * we have wanted to close since ``first_ts`` and never once found the peer
        reachable enough to even attempt cooperation -- there is nobody to
        cooperate with.

    Returns False without a record: no close has been requested yet, so nothing
    has earned an escalation.
    """
    if not attempt:
        return False
    try:
        raw_last = attempt.get("last_coop_ts")
        if raw_last is not None:
            return (now - float(raw_last)) >= window_sec
    except (TypeError, ValueError):
        return False
    try:
        first_ts = float(attempt.get("first_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if first_ts <= 0:
        return False
    return (now - first_ts) >= window_sec


# --- pending-channel freeze classification --------------------------------
# A channel open that has not reached OPEN yet freezes plugin automation, so the
# plugin does not re-fire while it settles. But "not yet OPEN" covers two very
# different situations, and they deserve different treatment:
#
#   * still CONFIRMING -- the funding tx is accruing confirmations normally.
#     Nothing is wrong; it just needs blocks. In a high-fee environment this can
#     legitimately take days. Such a channel must not block reverse swaps on
#     OTHER, already-open channels -- those are unrelated operations and
#     stalling them for days is pure lost value. It does still block a second
#     channel open, so on-chain funds are not committed twice over, but even
#     that gives up after ``confirming_freeze_sec``.
#   * WEDGED -- confirmed (or not progressing) and waiting on a peer that is not
#     responding. This freezes everything, as before, until it ages out at
#     ``stuck_sec`` and the watchdog takes over.


def classify_pending_freeze(is_confirming: bool, age_sec: float, *,
                            stuck_sec: float,
                            confirming_freeze_sec: float) -> Tuple[bool, bool]:
    """``(freezes_opens, freezes_swaps)`` for one pre-OPEN channel.

    ``age_sec`` is measured from channel creation for both horizons -- this is
    the freeze-escape clock, deliberately kept on plain wall-clock age so a
    channel ALWAYS un-freezes automation eventually, whatever its confirmation
    state. (The force-close clock is the one that starts later; see
    ``record_wedge_strike``. Conflating the two would let a slow-confirming
    channel freeze the plugin indefinitely.)
    """
    if is_confirming:
        return (age_sec < confirming_freeze_sec, False)
    return (age_sec < stuck_sec, age_sec < stuck_sec)


# --- startup / shutdown readiness -----------------------------------------
# The watchdogs read a peer's live reachability (``chan.is_active()``) to decide
# it is offline -- faulting the peer and/or feeding an "it's gone" uptime metric.
# But at wallet startup the Lightning layer has not finished (re)connecting to
# peers yet, and at shutdown it is tearing those connections down; in both
# windows a perfectly healthy peer reads as "not connected". Acting on that would
# blame the peer for *our* transitional state -- an immediate hard fault or a
# poisoned uptime ratio that can force-close a channel to a peer that was fine.
#
# These PURE predicates (no clock, no Electrum) encode the guard. The glue
# supplies the wall clock (as ``elapsed_sec`` since the wallet was loaded), the
# network-connected flag, whether the wallet has finished syncing, whether every
# open channel's peer has yielded a trustworthy reading, and -- for the per-peer
# gate -- whether we have already seen *this* peer online at least once since
# load. Keeping them here keeps the race logic unit-testable in isolation.

# Reasons a wallet is not yet ready. Returned by ``wallet_readiness_block`` and
# used verbatim in the deferral log line, so the log names the limb that is
# actually blocking instead of an opaque "not settled yet".
BLOCK_NOT_MANAGED = "wallet is not being managed"
BLOCK_NO_CONNECTION = "no server connection"
BLOCK_SYNCING = "wallet is still syncing with the server"
BLOCK_LOCKED = "wallet is locked (unlock it in Electrum to allow automation)"
BLOCK_STARTING_UP = "still in the startup window"


def wallet_readiness_block(network_connected: bool, wallet_synced: bool,
                           elapsed_sec: float, grace_sec: float, *,
                           manual: bool = False,
                           wallet_unlocked: bool = True) -> Optional[str]:
    """Why the wallet cannot take automated action yet, or ``None`` if it can.

    Four independent conditions, checked in the order the user would want them
    reported:

      * **server connection** -- without one we can neither read fresh chain
        state nor broadcast, so nothing may proceed (manual runs included);
      * **wallet sync** -- an address-synchronizer that is still catching up
        reports a partial UTXO set, so a balance-driven decision (open / swap)
        would be taken on incomplete state. Also blocks manual runs;
      * **unlocked** -- a password-protected wallet that the user has not
        unlocked cannot sign, so no action that moves money can complete. This
        gate is what keeps the tick from doing its (expensive) work and then
        failing at the signing step: a channel open raises outright, and a
        reverse swap is worse -- it starts, pays its Lightning leg, and only
        then finds it cannot sign the on-chain claim. Blocks manual runs too:
        "Run now" cannot conjure a password either, and the honest answer is to
        tell the user to unlock. See ``LiquidityPlugin._get_password``;
      * **startup window** -- for ``grace_sec`` after load the plugin takes no
        automated action at all. The Lightning layer is still (re)connecting: a
        healthy peer reads as offline, and -- learned the hard way, see below --
        a channel whose peer has just come up still cannot necessarily *route*.

    Only the last is time-based, and only it is skipped for a user-initiated
    "Run now" (``manual=True``): the user is present, watching, and can retry.

    Why this stayed a plain stopwatch. An earlier version of this gate ended the
    window as soon as every open channel's peer had been observed, which normally
    happened within seconds. It was measurably worse: ``chan.is_active()`` only
    means the peer connection is up, NOT that the channel can carry a large
    payment yet, so reverse swaps fired in that window had their Lightning
    payment fail fast (``NoPathFound``) and returned no funding txid. The rig's
    reverse-swap e2e failed 2 runs in 3 that way. "Ready to observe a peer" is
    not "ready to move money", and we had no trustworthy signal for the latter --
    so automatic action waits out the full window, as it always has.

    A non-positive ``grace_sec`` disables the time gate (ready as soon as
    connected and synced), which the tests use to exercise the other axes on
    their own.
    """
    if not network_connected:
        return BLOCK_NO_CONNECTION
    if not wallet_synced:
        return BLOCK_SYNCING
    if not wallet_unlocked:
        return BLOCK_LOCKED
    if manual or elapsed_sec >= grace_sec:
        return None
    return BLOCK_STARTING_UP


def is_wallet_ready(network_connected: bool, wallet_synced: bool,
                    elapsed_sec: float, grace_sec: float, *,
                    manual: bool = False, wallet_unlocked: bool = True) -> bool:
    """Whether the wallet has settled enough for the plugin to take *any*
    automated action -- the boolean face of :func:`wallet_readiness_block`."""
    return wallet_readiness_block(network_connected, wallet_synced, elapsed_sec,
                                  grace_sec, manual=manual,
                                  wallet_unlocked=wallet_unlocked) is None


def classify_peer_observation(is_active: bool, seen_online_before: bool,
                              network_connected: bool, elapsed_sec: float,
                              grace_sec: float) -> Optional[bool]:
    """Classify one peer-reachability reading as online / offline / *not
    observed*, applying the startup-race guard.

    Returns ``True`` (observed online), ``False`` (observed offline), or ``None``
    (**not observed** -- neither state is trustworthy, so the caller must record
    nothing: no fault, no uptime sample). The "not observed" verdict is returned
    when the reading is a not-yet-connected artefact rather than a real outage:

      * the peer reads active -> always ``True`` (and, per the seen-once rule,
        marks the peer as having connected this session);
      * the network is down (no server) -> ``None`` -- covers both startup
        (not connected yet) and a graceful shutdown (connection torn down);
      * the peer reads inactive but we have *never* seen it online this session
        and are still inside the startup grace -> ``None`` (it may simply not
        have been dialed yet);
      * otherwise (inactive, and either seen online earlier this session or past
        the grace) -> ``False``, a genuine offline observation.
    """
    if is_active:
        return True
    if not network_connected:
        return None
    if not seen_online_before and elapsed_sec < grace_sec:
        return None
    return False


# --- channel-partner selection --------------------------------------------
# Channel partners are Lightning nodes (identified by their pubkey/node id),
# unlike swap providers (nostr npubs). There is no live "discovery feed": the
# user lists preferred partners to try first and banned partners to avoid, and
# Electrum's own peer suggestion is the fallback. These two helpers are pure so
# the ordering logic stays unit-testable; the glue gathers the candidates and
# the wall-clock-free ordering lives here.
# A Lightning node id: a 33-byte compressed pubkey as 66 hex chars. Used only to
# *flag* an entry that cannot be one (see PartnerResolution.malformed_hits); it
# is deliberately not a filter, so partner selection behaves exactly as before.
_NODE_ID_RE = re.compile(r"^[0-9a-f]{66}$")


def looks_like_node_id(normalized: Optional[str]) -> bool:
    """True iff ``normalized`` (output of :func:`normalize_node_id`) has the
    shape of a Lightning node id."""
    return bool(normalized) and bool(_NODE_ID_RE.match(normalized))


def normalize_node_id(connect_str: Optional[str]) -> str:
    """The node pubkey (lowercased) from a connect string ``pubkey@host:port``
    or a bare ``pubkey``. Used to match preferred/banned entries by identity,
    ignoring any attached address (so a partner can be banned by pubkey alone)."""
    if not connect_str:
        return ""
    return connect_str.strip().split("@", 1)[0].strip().lower()


# How many of Electrum's peer suggestions to collect (and therefore try) for one
# channel open. Electrum's ``suggest_peer()`` returns ONE rating-weighted random
# node per call, so N distinct suggestions means calling it N-ish times; this cap
# bounds both that polling and how many peers a single open walks through before
# giving up. Preferred partners are NOT counted against it -- they are always all
# tried first (see :func:`order_channel_partners`).
#
# Raised from 10 to 50 because a wallet whose funding amount is below the common
# minimum channel size can burn the whole list on size rejections without ever
# reaching a peer that would take it -- a real run declined 10-for-10 that way.
# The cost is wall-clock: the walk is serial and each candidate can hold it for
# Electrum's LN_P2P_NETWORK_TIMEOUT (20s), so a tick where every partner fails
# can now run for minutes rather than seconds. That is bounded work, not a hang
# (the walk stops at the first success, and each attempt is individually
# timed out), but it is why this number is not simply "all of them".
MAX_SUGGESTED_PARTNERS: int = 50

# Substrings identifying a channel-open failure caused purely by the CHANNEL SIZE
# being unacceptable -- either our own funding amount tripping a local BOLT/config
# bound, or the peer rejecting it against *their* min/max channel policy. Such a
# rejection says nothing about the peer's reliability (a node that only accepts
# channels above our funding amount is perfectly healthy), so the glue records NO
# fault for it: see the caller in ``__init__.py``'s ``_open_channel``.
#
# Two sources, both surfacing as free text:
#   * local -- Electrum's own ``ChannelConfig.validate_params`` /
#     ``cross_validate_params`` raise plain ``Exception``s with these messages
#     before or just after ``accept_channel``;
#   * remote -- the peer's BOLT ``error`` message, relayed by Electrum as
#     ``GracefulDisconnect("remote peer sent error ...: <their text>")``. That
#     text is implementation-specific, so the markers below cover the phrasings
#     used by lnd / Core Lightning / Eclair. Matching is a heuristic on purpose:
#     a miss just falls back to today's behaviour (a hard fault), and a false
#     positive only means a genuinely bad peer escapes one fault.
_CHANNEL_SIZE_REJECTION_MARKERS: Tuple[str, ...] = (
    # Electrum-local (lnutil.ChannelConfig.validate_params / cross_validate_params)
    "funding_sat too low",
    "funding_sat too high",
    "reserve too high",
    "max_htlc_value_in_flight_msat is too small",
    "insane initial_msat",
    # lnd
    "channel too small",
    "channel too large",
    "below min chan size",
    "exceeds maximum channel size",
    "exceeds the maximum channel size",
    # lnd's `--minchansize` gate, which reports the *limit* without ever saying
    # "channel size": "Funding satoshis (60647) is less than the user specified
    # limit (1000000)". Observed in a real run on 2026-07-31, where it (and the
    # custom-acceptor phrasing below) were the only two size rejections that
    # slipped through and hard-faulted healthy peers.
    "less than the user specified limit",
    # Core Lightning
    "funding_satoshis is too small",
    "amount too small",
    "amount too large",
    # Eclair
    "funding amount too small",
    "funding amount is too small",
    "funding amount too large",
    "funding amount is too high",
    "invalid funding amount",
    # implementation-agnostic phrasings seen in the wild
    "capacity too small",
    "capacity too large",
    "channel size",
    "min_chan_size",
    # Hand-written channel acceptors (lnd/CLN plugins, LSP front-ends) phrase
    # their own minimum freely, e.g. "We do not accept channels smaller than
    # 1M sats". The operator-facing shape is stable enough to match on.
    "channels smaller than",
    "channels larger than",
)


def is_channel_size_rejection(error_text: Optional[object]) -> bool:
    """True if ``error_text`` describes a channel-open failure that is purely
    about the CHANNEL SIZE (our funding amount vs. a local bound or the peer's
    min/max channel policy).

    Used to decide whether a failed open should count against the peer at all.
    A size mismatch is not a reliability signal, so the caller records neither a
    hard nor a soft fault for it -- the peer keeps a clean record and its place in
    the try-order, and the open simply moves on to the next candidate.

    Matching is case-insensitive substring matching over
    :data:`_CHANNEL_SIZE_REJECTION_MARKERS`; ``None``/empty text is not a match.
    """
    if not error_text:
        return False
    lowered = str(error_text).lower()
    return any(marker in lowered for marker in _CHANNEL_SIZE_REJECTION_MARKERS)


@dataclass(frozen=True)
class PartnerResolution:
    """The outcome of building the channel-open try-order, *with its arithmetic*.

    An empty ``candidates`` is the single most confusing state the plugin can be
    in ("have funds and room to open, but no reachable channel partner"), because
    half a dozen different situations collapse into it. Carrying the per-stage
    counts alongside the result turns that dead end into an explanation: how many
    partners the user configured, how many Electrum suggested, and exactly how
    many were dropped by each rule.

    ``*_total`` counts entries that named a pubkey at all; ``*_kept`` counts
    those that survived every filter. The ``*_hits`` fields attribute each drop
    to one rule, checked in the order banned -> guard -> duplicate, so the three
    numbers always sum to ``total - kept`` across both lists.

    ``malformed_hits`` is reported *alongside* those rather than as a filter: an
    entry that does not look like a node id is still tried (unchanged behaviour
    -- the connect attempt is where it fails), but a typo'd preferred partner is
    named in the breakdown instead of looking like a peer that simply refused.
    """
    candidates: Tuple[str, ...]
    preferred_total: int
    preferred_kept: int
    suggested_total: int
    suggested_kept: int
    banned_hits: int
    guard_hits: int
    duplicate_hits: int
    malformed_hits: int
    strict: bool

    @property
    def total_kept(self) -> int:
        return len(self.candidates)


def resolve_channel_partners(
    preferred: Sequence[str],
    banned: FrozenSet[str],
    suggested: Sequence[str],
    *,
    strict: bool,
    penalties: Optional[Mapping[str, float]] = None,
    exclude: FrozenSet[str] = frozenset(),
) -> PartnerResolution:
    """:func:`order_channel_partners`, but returning the full
    :class:`PartnerResolution` (ordered candidates *plus* the counts that explain
    them). See that function for the ordering and filtering rules."""
    out: List[str] = []
    seen: set = set()
    counts = {"banned": 0, "guard": 0, "duplicate": 0, "malformed": 0}
    totals = {"preferred": 0, "suggested": 0}
    kept = {"preferred": 0, "suggested": 0}

    def consider(entries: Sequence[str], bucket: str) -> None:
        for entry in entries:
            if not entry or not entry.strip():
                continue
            nid = normalize_node_id(entry)
            if not nid:
                continue
            if not looks_like_node_id(nid):
                # Counted, not dropped -- see PartnerResolution's docstring.
                counts["malformed"] += 1
            totals[bucket] += 1
            if nid in banned:
                counts["banned"] += 1
                continue
            if nid in exclude:
                counts["guard"] += 1
                continue
            if nid in seen:
                counts["duplicate"] += 1
                continue
            seen.add(nid)
            kept[bucket] += 1
            out.append(entry.strip())

    consider(preferred, "preferred")
    if not strict:
        consider(suggested, "suggested")
    if penalties:
        # Stable sort keeps the preferred-first order among equal penalties.
        out.sort(key=lambda e: penalties.get(normalize_node_id(e), 0.0))
    return PartnerResolution(
        candidates=tuple(out),
        preferred_total=totals["preferred"],
        preferred_kept=kept["preferred"],
        suggested_total=totals["suggested"],
        suggested_kept=kept["suggested"],
        banned_hits=counts["banned"],
        guard_hits=counts["guard"],
        duplicate_hits=counts["duplicate"],
        malformed_hits=counts["malformed"],
        strict=bool(strict),
    )


def order_channel_partners(
    preferred: Sequence[str],
    banned: FrozenSet[str],
    suggested: Sequence[str],
    *,
    strict: bool,
    penalties: Optional[Mapping[str, float]] = None,
    exclude: FrozenSet[str] = frozenset(),
) -> List[str]:
    """Ordered connect strings to attempt a channel open against.

    Preferred partners (in the user's order) come first; Electrum's own
    suggestions follow unless ``strict`` is set (then only preferred are used).
    Banned node ids — matched by pubkey, ignoring any host — are dropped from
    both lists, and duplicates by pubkey are removed keeping the earliest
    occurrence. ``banned`` holds already-normalized (lowercased) pubkeys.

    ``exclude`` holds already-normalized pubkeys of peers we ALREADY have a
    channel with; when the "one channel per peer" guard is on, the glue passes
    the current peers here so they are dropped from both lists (never open a
    second channel to a node we already hold one with). Kept distinct from
    ``banned`` so the two reasons stay separable in logging: exclusion is a
    transient state (it lifts once the existing channel closes), banning is a
    persistent user/auto policy.

    ``penalties`` maps a normalized pubkey to its reliability penalty (percentage
    points; see :func:`decayed_penalty_pct`). When supplied, the result is
    *stably* re-sorted by ascending penalty, so a flaky peer sinks in the
    try-order while peers of equal penalty keep their preferred-first ordering.
    A heavily-penalised preferred peer can therefore fall behind a cleaner
    suggestion -- soft de-prioritisation, never an outright exclusion (banning is
    the engine-external hard stop).

    A thin wrapper over :func:`resolve_channel_partners` for the callers that
    only need the ordered list; use that one when you also need to explain an
    empty result.
    """
    return list(resolve_channel_partners(
        preferred, banned, suggested, strict=strict, penalties=penalties,
        exclude=exclude).candidates)


@dataclass(frozen=True)
class ChannelSnapshot:
    """A single channel, as the engine needs to see it."""
    channel_id: str            # hex short-hand id, for logging / addressing actions
    short_id: str              # human-friendly short channel id ("117x1x0")
    capacity_sat: int
    local_sat: int             # our outbound balance
    remote_sat: int            # inbound liquidity (what we can still receive)
    # What Electrum will actually let us SEND over this channel: its
    # available_to_spend(LOCAL) minus the routing-fee budget Electrum reserves for
    # a max-amount send (see LiquidityPlugin._channel_sendable_sat). Not the raw
    # balance, and not the raw available_to_spend either -- a swap sized at the
    # latter cannot have both of its Lightning legs routed.
    spendable_local_sat: int
    is_active: bool            # OPEN and usable right now (can route an HTLC)
    # Whether the channel currently has HTLCs that have not yet settled in either
    # direction. A reverse swap pays an LN HTLC through this channel, so swapping
    # over an already-congested/stuck channel risks piling onto a stuck payment;
    # the engine declines such a channel until it clears. Defaults False so the
    # pure tests (and any caller that does not populate it) keep prior behaviour.
    has_unsettled_htlcs: bool = False
    # Whether those unsettled HTLCs belong to a submarine swap *we* initiated
    # (matched by payment hash against the swap manager's own swap registry in the
    # glue). When true, the unsettled HTLC is the in-flight leg of a reverse swap
    # the plugin issued -- expected, not a stuck third-party payment -- so the
    # engine logs it as "our swap is still settling" rather than raising a
    # "possible stuck payment" near-miss. Defaults False so the pure tests and any
    # non-populating caller keep prior behaviour.
    unsettled_is_swap: bool = False
    # Whether this channel was opened by the plugin itself (matched against the
    # glue's persisted plugin-opened tag). Consulted only when
    # ``LiquidityConfig.manage_plugin_opened_only`` is set: a user-opened channel
    # (False) is then never reverse-swapped. Defaults True so the pure tests and
    # any non-populating caller keep the "manage every channel" behaviour when the
    # scope switch is off (where the flag is irrelevant anyway).
    is_plugin_opened: bool = True


@dataclass(frozen=True)
class ProviderOffer:
    """A single swap provider's advertised terms, as the engine needs them.

    Mirrors the fields of Electrum's ``SwapOffer`` / ``SwapFees`` (see
    ``submarine_swaps.py``) but is plain/import-free so the engine stays pure.
    ``npub`` is the provider's stable nostr identity, used for the preferred /
    banned lists and to address the swap to the chosen provider.
    """
    npub: str
    percentage_fee: float    # provider's % fee (e.g. 0.5 for 0.5%)
    mining_fee_sat: int      # provider's fixed base/mining fee
    min_amount_sat: int      # smallest swap this provider accepts
    # Largest client reverse swap (LN->on-chain) this provider accepts. NB: this
    # is sourced from the provider's `max_forward` capacity, because a client
    # reverse swap is a forward swap from the provider's side (see the glue in
    # __init__.py). It is NOT the provider's `max_reverse`.
    max_reverse_sat: int
    pow_bits: int = 0        # nostr announcement proof-of-work (tie-breaker; higher = more established)
    # Reliability penalty (percentage points) added to this provider's all-in
    # cost *for ranking only* -- it pushes a flaky provider behind reliable ones
    # without ever excluding it (soft de-prioritisation). The glue computes this
    # from persisted fault/success history (see ``reliability_penalty_pct``); it
    # is always 0.0 in the pure tests unless explicitly set.
    reliability_penalty_pct: float = 0.0


# Bounds a sane provider offer must fall inside. All-sat fields are capped at the
# total bitcoin supply; the percentage fee at 100%; the announcement PoW at a
# 32-byte hash's worth of bits. These reject a hostile provider's negative /
# NaN / infinite / absurd economics (which would otherwise game the cost gate and
# the cheapest-provider ranking) before they reach the engine.
_MAX_SATS: int = 21_000_000 * 100_000_000
_MAX_PERCENTAGE_FEE: float = 100.0
_MAX_POW_BITS: int = 256


def validate_offer(npub: str, percentage_fee: object, mining_fee_sat: object,
                   min_amount_sat: object, max_reverse_sat: object,
                   pow_bits: object, *,
                   reliability_penalty_pct: float = 0.0) -> Optional[ProviderOffer]:
    """Coerce + validate one provider's advertised terms into a ``ProviderOffer``,
    or ``None`` if anything is untrustworthy.

    Every field arrives from an untrusted nostr announcement. This returns
    ``None`` unless: ``npub`` is a already-validated non-empty identity (see
    :func:`clean_npub`); every numeric field coerces cleanly; the percentage is
    finite and within ``[0, 100]``; and each sat field / the PoW is a
    non-negative integer within a sane cap. Rejecting here (rather than trusting
    ``float()``/``int()``) stops a negative or NaN fee from passing the cost gate
    or corrupting the cheapest-provider ordering, and stops one malformed field
    from raising and taking out the whole evaluation. Pure and unit-testable."""
    if not npub:
        return None
    try:
        pct = float(percentage_fee)
        mining = int(mining_fee_sat)
        min_amt = int(min_amount_sat)
        max_rev = int(max_reverse_sat)
        pow_b = int(pow_bits)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(pct) or not (0.0 <= pct <= _MAX_PERCENTAGE_FEE):
        return None
    if not (0 <= mining <= _MAX_SATS):
        return None
    if not (0 <= min_amt <= _MAX_SATS):
        return None
    if not (0 <= max_rev <= _MAX_SATS):
        return None
    if not (0 <= pow_b <= _MAX_POW_BITS):
        return None
    return ProviderOffer(
        npub=npub, percentage_fee=pct, mining_fee_sat=mining,
        min_amount_sat=min_amt, max_reverse_sat=max_rev, pow_bits=pow_b,
        reliability_penalty_pct=reliability_penalty_pct)


@dataclass(frozen=True)
class ProviderReliability:
    """A provider's fault/success history, as the penalty function needs it.

    Plain/clock-free: the glue reads the wall clock and passes
    ``age_since_last_fault_sec`` in, so the penalty math stays pure and unit
    testable. ``consecutive_faults`` is the run of faults since the last
    success (a success resets it to 0).
    """
    consecutive_faults: int = 0
    age_since_last_fault_sec: float = 0.0
    success_count: int = 0
    fault_count: int = 0


def decayed_penalty_pct(consecutive_faults: int, age_since_last_fault_sec: float,
                        *, base_pct: float, halflife_sec: float, cap_pct: float) -> float:
    """Decaying reliability penalty (percentage points) shared by the provider and
    channel-peer rankings.

    Grows exponentially with consecutive faults (``base · 2^(faults-1)``) so a
    repeatedly-failing party sinks fast, and decays with a half-life since the
    last fault (``· 0.5^(age/halflife)``) so one that stops failing climbs back
    automatically (auto-recover). A success resets ``consecutive_faults`` to 0
    (penalty 0) at the source. Capped at ``cap_pct``.
    """
    if consecutive_faults <= 0 or base_pct <= 0:
        return 0.0
    raw = base_pct * (2.0 ** (consecutive_faults - 1))
    if halflife_sec > 0 and age_since_last_fault_sec > 0:
        raw *= 0.5 ** (age_since_last_fault_sec / halflife_sec)
    return min(raw, cap_pct)


def reliability_penalty_pct(rel: ProviderReliability, *, base_pct: float,
                            halflife_sec: float, cap_pct: float) -> float:
    """Ranking penalty (percentage points) for a *provider's* recent
    unreliability. Thin wrapper over :func:`decayed_penalty_pct`."""
    return decayed_penalty_pct(
        rel.consecutive_faults, rel.age_since_last_fault_sec,
        base_pct=base_pct, halflife_sec=halflife_sec, cap_pct=cap_pct)


def should_auto_ban(hard_fault_count: int, threshold: int) -> bool:
    """Whether a channel peer has earned an automatic ban.

    Only *hard* faults count (a peer that force-closed on us, or repeatedly
    failed channel-open negotiation) -- transient unreachability is soft and only
    feeds the decaying penalty. ``threshold <= 0`` disables auto-banning.
    """
    return threshold > 0 and hard_fault_count >= threshold


@dataclass(frozen=True)
class LiquidityConfig:
    """User-tunable thresholds. Every field maps 1:1 to a plugin ConfigVar."""
    automation_enabled: bool
    min_onchain_to_open_sat: int   # never open a channel with less than this on-chain (rule: X)
    onchain_reserve_sat: int       # always leave this much on-chain when opening (rule: 10_000)
    max_channels: int              # never hold more than this many channels (rule: 2)
    max_swap_fee_pct: float        # skip reverse swaps whose effective all-in cost % exceeds this (rule: 0.9)
    swap_trigger_pct: float        # reverse-swap a channel at/over this % of capacity local (rule: 25)
    swap_trigger_sat: int          # ...OR once local balance exceeds this many sats (rule: 25_000)
    # Outbound-preservation floor (sat), per channel. A reverse swap never drains a
    # channel's outbound (local) balance below this, so the wallet retains some
    # ability to *send*. Enforced against the swappable amount: the engine plans
    # ``spendable_local_sat - min_outbound_sat`` (never below the provider minimum,
    # and never at all once the floor covers the whole spendable balance). 0 (the
    # default) preserves the original "drain everything" behaviour.
    min_outbound_sat: int = 0
    # Scope switch. When True, the engine only ever reverse-swaps channels the
    # plugin itself opened (``ChannelSnapshot.is_plugin_opened``); channels the
    # user opened by hand are left entirely alone (their outbound is never
    # drained). When False, every channel is managed. The glue supplies the
    # per-channel flag from its persisted plugin-opened tag; the engine stays
    # pure.
    #
    # NB: this dataclass default is False, but the SHIPPED default is ON -- see
    # ``INBOUND_LIQUIDITY_MANAGE_PLUGIN_OPENED_ONLY``. They differ on purpose:
    # ``read_config`` always passes the user's value explicitly, so this default
    # is only ever seen by pure tests and non-populating callers, where False
    # keeps the older "manage every channel" behaviour they were written against.
    manage_plugin_opened_only: bool = False
    # Runaway guard: at most this many channel opens in any rolling 24h window
    # (0 = unlimited). Counted from the executed-open history the glue passes in
    # via ``LiquiditySnapshot.opens_last_24h``. The matching close ceiling is
    # enforced entirely in the glue (closes are not engine-decided actions).
    max_opens_per_day: int = 0
    # Provider selection over the nostr-discovered offers:
    #   * preferred_npubs -- if non-empty, ONLY these providers are ever used
    #     (a strict whitelist; if none are currently available, no swap is made).
    #   * banned_npubs    -- providers that are never used, even if cheapest.
    # Empty preferred set means "use whichever discovered provider is cheapest".
    preferred_npubs: FrozenSet[str] = field(default_factory=frozenset)
    banned_npubs: FrozenSet[str] = field(default_factory=frozenset)
    # Target size (sat) for a channel. Drives the undersized-channel replacement
    # rule (see :func:`_decide_undersized_closes`): a plugin-opened channel whose
    # CAPACITY is below this is closed -- freeing its slot -- once we are at the
    # channel ceiling and hold enough on-chain to fund a replacement that would
    # actually reach the goal. 0 (this dataclass's default) disables the rule
    # entirely.
    #
    # NB: as with ``manage_plugin_opened_only``, the dataclass default and the
    # SHIPPED default differ on purpose -- the ConfigVar ships at
    # ``DEFAULT_LIQUIDITY_GOAL_SAT``. ``read_config`` always passes the user's
    # value explicitly, so 0 here only affects pure tests and non-populating
    # callers, for which "off" preserves the pre-feature behaviour they were
    # written against.
    liquidity_goal_sat: int = 0


@dataclass(frozen=True)
class LiquiditySnapshot:
    """Everything the engine reads to make a decision, captured at one instant."""
    onchain_spendable_sat: int
    channels: Sequence[ChannelSnapshot]
    swap_percentage_fee: Optional[float]      # provider's % fee, None if no provider known
    provider_max_reverse_sat: Optional[int]   # largest client reverse swap the provider accepts (its max_forward; see ProviderOffer.max_reverse_sat)
    provider_min_amount_sat: Optional[int]    # smallest swap the provider accepts
    # Amount-independent reverse-swap costs (sat), used to compute the effective
    # all-in cost of a swap. Mirror SwapManager: the provider's mining/base fee
    # and the on-chain claim tx fee.
    swap_mining_fee_sat: Optional[int] = None
    swap_claim_fee_sat: Optional[int] = None
    # All swap providers currently discovered on nostr. When non-empty, the
    # engine picks the cheapest eligible one (honouring preferred/banned) per
    # swap rather than using the single ``swap_percentage_fee`` provider. When
    # empty, the single-provider fields above are used (URL mode / legacy), by
    # synthesising one offer from them. The on-chain claim fee
    # (``swap_claim_fee_sat``) is provider-independent and added on top.
    provider_offers: Sequence[ProviderOffer] = ()
    # In-flight tracking (rule: never act while a plugin-relevant transaction is
    # still awaiting confirmation). Derived purely from wallet state, so any
    # pending op counts regardless of who initiated it:
    #   * channels whose funding is not yet OPEN (ChannelState PREOPENING/
    #     OPENING/FUNDED), and
    #   * reverse swaps whose funding tx is broadcast but not yet swept.
    pending_channel_count: int = 0
    inflight_swap_count: int = 0
    # Pre-OPEN channels that are still legitimately CONFIRMING (funding tx
    # accruing confirmations). Counted separately because they block only a
    # further channel open -- never reverse swaps on other channels, which have
    # nothing to do with this funding tx confirming. See
    # ``classify_pending_freeze``. Channels counted here are NOT in
    # ``pending_channel_count``; the two are disjoint.
    pending_open_freeze_count: int = 0
    # Number of channel opens the glue has executed within the trailing 24h
    # (rolling window), for the ``max_opens_per_day`` ceiling. The glue counts
    # these from its persisted action-timestamp store; 0 in the pure tests unless
    # set explicitly.
    opens_last_24h: int = 0
    # Channels whose close is under way but not yet mined (ChannelState SHUTDOWN
    # / CLOSING / FORCE_CLOSING / REQUESTED_FCLOSE). Such a channel is still in
    # ``channels`` -- it only drops out at CLOSED -- so without counting it
    # separately the engine cannot tell "already being closed" from "peer is
    # unreachable"; both merely read as ``is_active == False``.
    #
    # This is what makes the undersized-replacement rule one-at-a-time ACROSS
    # ticks and not just within one: the tick after a replacement close is
    # requested, that channel is excluded (not active) while any OTHER undersized
    # channel still qualifies -- and would be closed on the strength of the same
    # on-chain balance that justified the first. See
    # ``_decide_undersized_closes``. WE_ARE_TOXIC is deliberately NOT counted: it
    # is not a close in progress and can persist indefinitely, so counting it
    # would wedge the rule forever.
    closing_channel_count: int = 0


def swap_cost_sat(percentage_fee: float, mining_fee_sat: int, claim_fee_sat: int,
                  amount_sat: int) -> int:
    """All-in cost of a reverse swap in sat: percentage fee (ceil'd, as the
    provider computes it) + the provider's fixed mining/base fee + the
    provider-independent on-chain claim fee. Single source of truth for the
    cost arithmetic, shared by the cost gate and the provider selector."""
    return math.ceil(percentage_fee / 100.0 * amount_sat) + mining_fee_sat + claim_fee_sat


def effective_swap_cost_pct(amount_sat: int, snapshot: LiquiditySnapshot) -> Optional[float]:
    """All-in cost of a reverse swap as a percentage of the amount sent, for the
    single configured provider (``swap_percentage_fee`` / fixed fees).

    Mirrors Electrum's ``SwapManager._sanity_check_swap_costs`` /
    ``get_recv_amount``. Returns None if the provider's economics are not yet
    known. The multi-provider path uses :func:`select_provider` instead.
    """
    pct = snapshot.swap_percentage_fee
    if pct is None or amount_sat <= 0:
        return None
    fixed = (snapshot.swap_mining_fee_sat or 0) + (snapshot.swap_claim_fee_sat or 0)
    return swap_cost_sat(pct, snapshot.swap_mining_fee_sat or 0,
                         snapshot.swap_claim_fee_sat or 0, amount_sat) / amount_sat * 100.0


@dataclass(frozen=True)
class ProviderSelection:
    """The provider chosen for one swap, the amount to swap with it, and the
    resulting all-in cost as a percentage of that amount.

    ``all_in_cost_pct`` is the *real* cost (what the gate checks and the user
    pays). ``rank_cost_pct`` is that plus the provider's reliability penalty --
    the value the selection was actually ordered by -- surfaced for the log so a
    "why this provider" decision is explainable.
    """
    offer: ProviderOffer
    amount_sat: int
    all_in_cost_pct: float
    rank_cost_pct: float


def eligible_providers(offers: Sequence[ProviderOffer],
                       config: LiquidityConfig) -> List[ProviderOffer]:
    """Filter discovered offers by the user's preferred/banned lists.

    Banned providers are always dropped. If a preferred list is set, only those
    providers survive (a strict whitelist) -- so if none of them are currently
    advertising, the result is empty and no swap will be made.
    """
    out = [o for o in offers if o.npub not in config.banned_npubs]
    if config.preferred_npubs:
        out = [o for o in out if o.npub in config.preferred_npubs]
    return out


def _offer_available_sat(offer: ProviderOffer,
                         consumed: Optional[Mapping[str, int]]) -> int:
    """The provider's remaining reverse-swap capacity after subtracting the
    Lightning amount already committed to it *earlier in this same decision pass*
    (``consumed`` maps npub -> sat committed). This is how the engine avoids
    planning a second swap that a provider's advertised ``max_forward`` cannot
    host once an earlier swap this cycle has drawn it down -- which the provider
    would reject server-side (its capacity only re-advertises every ~30s). Absent
    a budget it is just the advertised max."""
    if not consumed:
        return offer.max_reverse_sat
    return max(0, offer.max_reverse_sat - int(consumed.get(offer.npub, 0)))


def select_provider(offers: Sequence[ProviderOffer], desired_amount_sat: int,
                    claim_fee_sat: int, config: LiquidityConfig,
                    consumed: Optional[Mapping[str, int]] = None) -> Optional[ProviderSelection]:
    """Pick the best eligible provider for a swap of up to ``desired_amount_sat``.

    Each provider would swap ``min(desired, its remaining capacity)`` (and must
    clear its own minimum). Only providers whose *real* all-in cost passes the
    ``max_swap_fee_pct`` gate are considered. Among those, providers are ordered
    by all-in cost *plus* their reliability penalty (so a flaky provider sinks
    behind reliable ones -- soft de-prioritisation, never an outright exclusion);
    ties break in favour of the higher proof-of-work (more established) provider.
    Returns None if no eligible provider both can host the swap and passes the
    gate -- use :func:`cheapest_hosting_cost` to tell those two cases apart for
    logging.

    ``consumed`` (npub -> sat) lets a caller draining several channels in one pass
    subtract capacity already committed to each provider, so the engine never
    plans two swaps that together exceed a provider's advertised ``max_forward``.
    """
    best: Optional[ProviderSelection] = None
    best_key: Optional[Tuple[float, int]] = None
    for offer in eligible_providers(offers, config):
        amount = min(desired_amount_sat, _offer_available_sat(offer, consumed))
        if amount <= 0 or amount < offer.min_amount_sat:
            continue
        cost_pct = swap_cost_sat(offer.percentage_fee, offer.mining_fee_sat,
                                 claim_fee_sat, amount) / amount * 100.0
        # The gate is on the REAL cost; the reliability penalty only reorders.
        if cost_pct > config.max_swap_fee_pct:
            continue
        rank_cost_pct = cost_pct + max(0.0, offer.reliability_penalty_pct)
        # Lower rank cost wins; on a tie prefer higher PoW (negate for ascending sort).
        key = (rank_cost_pct, -offer.pow_bits)
        if best_key is None or key < best_key:
            best_key = key
            best = ProviderSelection(offer=offer, amount_sat=amount,
                                     all_in_cost_pct=cost_pct, rank_cost_pct=rank_cost_pct)
    return best


def cheapest_hosting_cost(offers: Sequence[ProviderOffer], desired_amount_sat: int,
                          claim_fee_sat: int, config: LiquidityConfig,
                          consumed: Optional[Mapping[str, int]] = None) -> Optional[Tuple[int, float]]:
    """Among eligible providers that can host the amount (ignoring the cost
    gate), the (amount, real all-in cost %) of the cheapest. Used only to build
    an actionable decline reason: ``None`` means "below every provider's
    minimum"; a value whose cost exceeds the ceiling means "over ceiling".

    ``consumed`` applies the same per-provider capacity budget as
    :func:`select_provider`; pass it (vs. omit it) to tell "no provider can host
    this at its *remaining* capacity" apart from "below every provider's minimum".
    """
    best: Optional[Tuple[int, float]] = None
    for offer in eligible_providers(offers, config):
        amount = min(desired_amount_sat, _offer_available_sat(offer, consumed))
        if amount <= 0 or amount < offer.min_amount_sat:
            continue
        cost_pct = swap_cost_sat(offer.percentage_fee, offer.mining_fee_sat,
                                 claim_fee_sat, amount) / amount * 100.0
        if best is None or cost_pct < best[1]:
            best = (amount, cost_pct)
    return best


def _candidate_offers(snapshot: LiquiditySnapshot) -> List[ProviderOffer]:
    """The offers the engine selects among. Prefers the live nostr-discovered
    list; falls back to synthesising a single offer from the snapshot's
    single-provider fields (URL mode / legacy), so existing behaviour is
    preserved when no offer list is supplied."""
    if snapshot.provider_offers:
        return list(snapshot.provider_offers)
    if snapshot.swap_percentage_fee is None:
        return []
    return [ProviderOffer(
        npub="",  # legacy single provider: execution falls back to config.SWAPSERVER_NPUB
        percentage_fee=snapshot.swap_percentage_fee,
        mining_fee_sat=snapshot.swap_mining_fee_sat or 0,
        min_amount_sat=snapshot.provider_min_amount_sat or 0,
        # No advertised max in legacy mode means "unbounded"; a huge sentinel so
        # min(desired, max) is just `desired`.
        max_reverse_sat=snapshot.provider_max_reverse_sat
        if snapshot.provider_max_reverse_sat is not None else 21_000_000 * 100_000_000,
    )]


@dataclass(frozen=True)
class OpenChannelAction:
    funding_sat: int
    reason: str


@dataclass(frozen=True)
class ReverseSwapAction:
    channel_id: str
    short_id: str
    lightning_amount_sat: int
    reason: str
    # Chosen provider's nostr identity. Empty string means "use the single
    # configured provider" (URL mode / legacy), preserving old behaviour.
    provider_npub: str = ""


@dataclass(frozen=True)
class CloseChannelAction:
    """Close an undersized channel so its slot can be reused by a bigger one.

    Emitted only by :func:`_decide_undersized_closes`, and executed by the glue
    through its ONE close path (``_request_close``), which tries a cooperative
    close first and respects the daily close ceiling. ``capacity_sat`` is carried
    for the log line -- "closing a 150k channel to reopen at >=500k" is the whole
    justification for the action, so it should not require a second lookup.
    """
    channel_id: str
    short_id: str
    capacity_sat: int
    reason: str


Action = Union[OpenChannelAction, ReverseSwapAction, CloseChannelAction]


@dataclass(frozen=True)
class DeclineRecord:
    """A decision *not* to act, with the reason. Surfaced in the decision log's
    "Declines" view. ``kind`` is one of:
      * "freeze" -- the whole tick was skipped because something is in flight,
      * "open"   -- a channel open was considered but a rule blocked it,
      * "swap"   -- a reverse swap was considered but a rule blocked it,
      * "close"  -- an undersized channel could have been replaced to make room
                    for a bigger one, but a rule blocked it (see
                    ``_decide_undersized_closes``).
    Only *near-miss* declines are recorded (a candidate that was on the verge of
    acting but got blocked) -- idle "nothing to do" ticks produce no records.
    """
    kind: str
    reason: str
    channel_id: Optional[str] = None
    short_id: Optional[str] = None
    amount_sat: Optional[int] = None
    # Optional supporting arithmetic, shown as an expandable child of the log
    # row. Deliberately NOT part of the decline's dedup signature (kind,
    # channel_id, reason), so detail that varies tick to tick -- e.g. how many
    # peers were suggested this time -- cannot defeat the "log a steady state
    # once" rule.
    detail: Optional[str] = None


@dataclass(frozen=True)
class DecisionResult:
    """The full outcome of one evaluation: what to do and why nothing else."""
    actions: Tuple[Action, ...] = ()
    declines: Tuple[DeclineRecord, ...] = ()
    frozen: Optional[str] = None   # non-None reason if the tick was frozen


def over_swap_trigger(chan: ChannelSnapshot,
                      config: LiquidityConfig) -> Optional[str]:
    """Which swap-out trigger this channel is over -- ``"pct"``, ``"sat"``, or
    ``None`` if neither.

    Single source of truth for "this channel holds outbound the plugin intends
    to drain into inbound". Used both by :func:`_decide_reverse_swaps` (to decide
    whether to plan a swap) and by :func:`_decide_undersized_closes` (where being
    over a trigger PROTECTS the channel: closing it would throw away value the
    plugin is about to convert). "pct" wins the label when both are over, matching
    the order the rules were specified in.
    """
    if chan.local_sat >= (config.swap_trigger_pct / 100.0) * chan.capacity_sat:
        return "pct"
    if chan.local_sat > config.swap_trigger_sat:
        return "sat"
    return None


def evaluate(snapshot: LiquiditySnapshot, config: LiquidityConfig) -> DecisionResult:
    """Map (state, thresholds) -> a DecisionResult.

    Order of reasoning:
      1. If automation is off, do nothing (and record nothing).
      2. GLOBAL FREEZE: if a plugin-relevant transaction is in flight in a way
         that blocks everything (a channel open that is NOT simply confirming,
         or a reverse swap not yet swept), take *no* action this tick -- neither
         opens nor swaps.
      3. PARTIAL FREEZE: a channel open that is merely waiting for
         confirmations blocks only a further open (so on-chain funds are not
         committed twice), while reverse swaps on other, already-open channels
         proceed normally. Confirmation can legitimately take days in a high-fee
         environment, and stalling unrelated channels for that long is pure lost
         value.
      4. Otherwise consider a channel open first (so a cold wallet builds
         capacity before draining it), then the undersized-channel replacement
         rule (which can only fire when an open was blocked by the channel
         ceiling), then reverse swaps. A freshly opened channel is not yet
         ``is_active`` and so is never reverse-swapped in the same cycle that
         opens it.
    """
    if not config.automation_enabled:
        return DecisionResult()

    pending = snapshot.pending_channel_count
    inflight = snapshot.inflight_swap_count
    if pending > 0 or inflight > 0:
        reason = (
            f"frozen: {pending} channel open(s) and {inflight} reverse swap(s) "
            f"still in flight; no action until they confirm"
        )
        return DecisionResult(
            actions=(),
            declines=(DeclineRecord(kind="freeze", reason=reason),),
            frozen=reason,
        )

    actions: List[Action] = []
    declines: List[DeclineRecord] = []

    confirming = snapshot.pending_open_freeze_count
    if confirming > 0:
        # Partial freeze: opens only. Recorded as an "open" decline rather than
        # a "freeze" one, because the tick is not frozen -- swaps below still
        # run, and labelling it a freeze would misreport that in the log.
        declines.append(DeclineRecord(
            kind="open",
            reason=(
                f"{confirming} channel open(s) still confirming on-chain; not "
                f"opening another until they confirm (reverse swaps unaffected)"
            ),
        ))
    else:
        open_action, open_decline = _decide_channel_open(snapshot, config)
        # Undersized-channel replacement. Sits in this branch (not outside it) on
        # purpose: a channel open that is still confirming has already committed
        # the on-chain funds this rule's "we can afford a replacement" test is
        # counting, so closing on the strength of them would be double-spending
        # the same balance across two decisions.
        close_action, close_declines = _decide_undersized_closes(snapshot, config)
        if open_action is not None:
            actions.append(open_action)
        if close_action is not None:
            actions.append(close_action)
        if open_decline is not None:
            # When we are closing a channel *because* we are at the ceiling, the
            # open's "at max channels; not opening" decline is the very thing the
            # close resolves. Logging both would read as the plugin contradicting
            # itself, so the close action speaks for the pair.
            if close_action is None:
                declines.append(open_decline)
        declines.extend(close_declines)

    swap_actions, swap_declines = _decide_reverse_swaps(snapshot, config)
    actions.extend(swap_actions)
    declines.extend(swap_declines)

    return DecisionResult(actions=tuple(actions), declines=tuple(declines))


def decide(snapshot: LiquiditySnapshot, config: LiquidityConfig) -> List[Action]:
    """Backwards-compatible thin wrapper: just the actions from ``evaluate``."""
    return list(evaluate(snapshot, config).actions)


def _decide_channel_open(
    snapshot: LiquiditySnapshot, config: LiquidityConfig
) -> Tuple[Optional[OpenChannelAction], Optional[DeclineRecord]]:
    # Rule: never open more than `max_channels` (count everything not yet closed).
    # Only a *near miss* (we had the funds to open) is worth logging as a decline.
    if len(snapshot.channels) >= config.max_channels:
        if snapshot.onchain_spendable_sat >= config.min_onchain_to_open_sat:
            return None, DeclineRecord(
                kind="open",
                reason=(
                    f"at max channels ({len(snapshot.channels)} >= "
                    f"{config.max_channels}); not opening despite "
                    f"{snapshot.onchain_spendable_sat} sat on-chain"
                ),
                amount_sat=snapshot.onchain_spendable_sat,
            )
        return None, None
    # Rule: never open a channel with < X on-chain funds. This is just "waiting
    # for funds", not a near miss, so it is not logged as a decline.
    if snapshot.onchain_spendable_sat < config.min_onchain_to_open_sat:
        return None, None
    # Rule: open with the maximum amount, leaving `onchain_reserve_sat` on-chain.
    funding_sat = snapshot.onchain_spendable_sat - config.onchain_reserve_sat
    if funding_sat < MIN_FUNDING_SAT:
        # We passed the min-to-open gate but cannot clear Electrum's funding
        # floor after the reserve -- a genuine near miss worth recording.
        return None, DeclineRecord(
            kind="open",
            reason=(
                f"on-chain {snapshot.onchain_spendable_sat} minus reserve "
                f"{config.onchain_reserve_sat} = {funding_sat} < funding floor "
                f"{MIN_FUNDING_SAT}"
            ),
            amount_sat=funding_sat,
        )
    # Runaway guard: we have the funds and the room to open, but refuse if the
    # rolling-24h open ceiling is reached. Checked last so it only fires as a
    # genuine near miss (an open that every other rule would have allowed).
    if daily_cap_reached(snapshot.opens_last_24h, config.max_opens_per_day):
        return None, DeclineRecord(
            kind="open",
            reason=(
                f"daily open ceiling reached ({snapshot.opens_last_24h} >= "
                f"{config.max_opens_per_day} opens in the last 24h); not opening "
                f"despite {snapshot.onchain_spendable_sat} sat on-chain"
            ),
            amount_sat=funding_sat,
        )
    return (
        OpenChannelAction(
            funding_sat=funding_sat,
            reason=(
                f"on-chain spendable {snapshot.onchain_spendable_sat} >= "
                f"min {config.min_onchain_to_open_sat} and "
                f"{len(snapshot.channels)} < {config.max_channels} channels"
            ),
        ),
        None,
    )


# --- undersized-channel replacement (the "liquidity goal") ----------------
# The problem this solves: the plugin opens channels for inbound liquidity, but
# once it is holding ``max_channels`` of them it will not open another -- even
# when it has since accumulated enough on-chain to fund a channel several times
# bigger than the small ones it started with. The wallet ends up capped by the
# size of its earliest channels with no way to grow.
#
# The fix is to let a channel be REPLACED rather than merely counted: a
# plugin-opened channel whose capacity is below the user's liquidity goal is
# closed, freeing its slot, but ONLY when closing it actually unblocks something
# -- i.e. we are at the ceiling and already hold enough on-chain to fund a
# replacement that would genuinely reach the goal.
#
# A close is irreversible and costs a mining fee, so the rule is deliberately
# hedged about with conditions; see the gate list in the function below. Two of
# them deserve highlighting because they are what make the rule safe rather than
# merely cautious:
#
#   * OVER A SWAP TRIGGER PROTECTS THE CHANNEL. The plugin funds its channels
#     with no push, so a freshly opened channel has local == capacity and is over
#     the percentage trigger by construction. It therefore CANNOT be opened and
#     immediately re-closed: it only becomes replaceable once the plugin has
#     actually drained it into inbound liquidity. This is the churn guard, and it
#     costs nothing because the condition was already required for a different
#     reason (never throw away outbound we are about to convert).
#   * ONE PER EVALUATION. Several undersized channels may qualify at once, but
#     the funds test is a test for ONE replacement -- closing three channels on
#     the strength of a single funding amount would destroy more inbound than can
#     be rebuilt. The smallest qualifier goes first and the rest wait for a later
#     evaluation, by which time the state has been re-read.
def _decide_undersized_closes(
    snapshot: LiquiditySnapshot, config: LiquidityConfig
) -> Tuple[Optional[CloseChannelAction], List[DeclineRecord]]:
    """Decide whether to close ONE undersized channel to make room for a bigger
    one. Returns ``(action_or_None, declines)``.

    Every one of these must hold, and each is here for its own reason:

      1. a liquidity goal is configured (0 = rule disabled);
      2. we are AT the channel ceiling -- below it, a bigger channel can simply
         be opened alongside, so closing anything would be gratuitous;
      3. on-chain spendable minus the reserve can fund a channel that reaches the
         goal (and clears the funding floor) -- so the replacement is real, not
         hypothetical;
      4. the channel was opened by the plugin -- a channel the user opened by
         hand is never closed by this rule, whatever the scope switch says;
      5. its capacity is below the goal -- it is the thing holding us back;
      6. it is NOT over a swap trigger -- it holds no outbound we are about to
         convert into inbound (and, per the note above, it is not brand new);
      7. no OTHER close is already settling -- one replacement at a time, or the
         same on-chain balance would justify closing several channels over
         consecutive ticks (see ``LiquiditySnapshot.closing_channel_count``);
      8. it has no unsettled HTLCs -- never close over an in-flight payment;
      9. its peer is reachable (``is_active``) -- so the close can be
         COOPERATIVE. A channel whose peer is gone would have to be force-closed,
         paying a mining fee and a CSV timelock purely to resize; that case
         belongs to the offline auto-close watchdog, which has uptime evidence to
         justify the expense.

    Declines are recorded only for a genuine near miss: gates 1-3 passed (we are
    blocked, with the funds to fix it) and at least one channel is undersized,
    but nothing cleared the remaining gates.
    """
    if config.liquidity_goal_sat <= 0:
        return None, []
    if len(snapshot.channels) < config.max_channels:
        return None, []
    # What a replacement channel would actually be funded with -- mirrors
    # _decide_channel_open, so "we could fund it" here means the same arithmetic
    # that will run when the open is decided.
    funding_sat = snapshot.onchain_spendable_sat - config.onchain_reserve_sat
    # The replacement must both REACH THE GOAL (else the close buys nothing) and
    # clear Electrum's funding floor (else it cannot be opened at all). The floor
    # is the module global, which the glue lowers to the user's
    # min_onchain_to_open_sat at startup -- so this tracks the effective floor.
    required_sat = max(config.liquidity_goal_sat, MIN_FUNDING_SAT)
    if funding_sat < required_sat:
        # Simply waiting for funds; not a near miss, so nothing is logged (the
        # same treatment _decide_channel_open gives its min-on-chain gate).
        return None, []

    undersized = [c for c in snapshot.channels
                  if c.is_plugin_opened and c.capacity_sat < config.liquidity_goal_sat]
    if not undersized:
        return None, []

    # One replacement at a time, across ticks as well as within one. A channel we
    # asked to close stays in ``channels`` until its closing tx is mined, and by
    # then it reads as merely not-active -- so without this gate the very next
    # tick would find the NEXT undersized channel still eligible and close it
    # too, on the strength of the same on-chain balance that justified the first.
    # Waiting for the close to settle also means the next decision is made
    # against the funds that close actually returned.
    #
    # Failure mode, accepted deliberately: a cooperative close that stalls after
    # shutdown was sent (peer vanished mid-negotiation) leaves the channel in
    # SHUTDOWN, and this gate then pauses the rule until Electrum completes the
    # close on reconnect. That is a visible pause with a decline explaining it --
    # no funds at risk -- and the alternative (re-driving the close until it
    # escalates to a force-close) would spend a mining fee and a CSV timelock to
    # resize a channel, which is exactly what gate 9 exists to prevent. A close
    # that fails BEFORE shutdown is sent leaves the channel OPEN and is retried
    # normally, so only the half-closed case pauses anything.
    if snapshot.closing_channel_count > 0:
        return None, [DeclineRecord(
            kind="close",
            reason=(f"a channel close is already settling; not replacing another "
                    f"undersized channel until it completes"),
            detail=f"{snapshot.closing_channel_count} channel(s) closing; "
                   f"{len(undersized)} undersized channel(s) waiting",
        )]

    candidates: List[ChannelSnapshot] = []
    blocked: List[str] = []
    for chan in undersized:
        trigger = over_swap_trigger(chan, config)
        if trigger is not None:
            blocked.append(f"{chan.short_id} still holds {chan.local_sat} sat of "
                           f"outbound to swap out first (over the {trigger} trigger)")
        elif chan.has_unsettled_htlcs:
            blocked.append(f"{chan.short_id} has unsettled HTLCs")
        elif not chan.is_active:
            blocked.append(f"{chan.short_id} peer is offline, so it could only be "
                           f"force-closed")
        else:
            candidates.append(chan)

    if not candidates:
        # The reason deliberately carries only STABLE facts (channel count, the
        # configured goal): it is the decline's dedup key, so folding in the
        # funding amount -- which drifts with every block -- would re-log this
        # steady state on every tick. The varying arithmetic goes in `detail`,
        # which is excluded from the signature by design.
        return None, [DeclineRecord(
            kind="close",
            amount_sat=funding_sat,
            reason=(f"at max channels ({len(snapshot.channels)}) with funds to "
                    f"reach the {config.liquidity_goal_sat} sat liquidity goal, "
                    f"but no undersized channel can be replaced right now"),
            detail="; ".join([f"{funding_sat} sat available to fund a replacement"]
                             + blocked),
        )]

    # Smallest first -- it is the one furthest from the goal, so replacing it
    # buys the most. channel_id breaks ties so the choice is deterministic (the
    # same tick re-run picks the same channel).
    chan = min(candidates, key=lambda c: (c.capacity_sat, c.channel_id))
    return CloseChannelAction(
        channel_id=chan.channel_id,
        short_id=chan.short_id,
        capacity_sat=chan.capacity_sat,
        reason=(f"channel {chan.short_id} capacity {chan.capacity_sat} is below the "
                f"{config.liquidity_goal_sat} sat liquidity goal, and at "
                f"{len(snapshot.channels)}/{config.max_channels} channels it is "
                f"blocking a bigger one; closing it to reopen with {funding_sat} sat"),
    ), []


def _decide_reverse_swaps(
    snapshot: LiquiditySnapshot, config: LiquidityConfig
) -> Tuple[List[ReverseSwapAction], List[DeclineRecord]]:
    actions: List[ReverseSwapAction] = []
    declines: List[DeclineRecord] = []
    offers = _candidate_offers(snapshot)
    eligible = eligible_providers(offers, config)
    claim_fee = snapshot.swap_claim_fee_sat or 0
    # Lightning sat already committed to each provider (npub -> sat) by swaps
    # planned earlier in THIS pass, so a later channel doesn't plan a swap the
    # provider can no longer host (its advertised max_forward is drawn down by the
    # earlier swap but only re-advertises every ~30s). Keyed by npub; the legacy
    # single provider uses "" as its key.
    consumed: Dict[str, int] = {}
    for chan in snapshot.channels:
        trigger = over_swap_trigger(chan, config)
        if trigger is None:
            # Below both triggers: nothing to do, and not a near miss.
            continue
        # The channel wants to be drained; from here on, anything that blocks it
        # is a near miss worth logging.
        # Scope switch: in plugin-opened-only mode, a channel the user opened by
        # hand is left entirely alone -- we never touch its outbound. Logged as a
        # near miss (it was over the trigger and would otherwise have been drained)
        # so the decision trail explains why an eligible-looking channel was spared.
        if config.manage_plugin_opened_only and not chan.is_plugin_opened:
            declines.append(DeclineRecord(
                kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                reason=(f"channel {chan.short_id} over {trigger} trigger but was not "
                        f"opened by the plugin (manage-plugin-opened-only is on); "
                        f"leaving its outbound untouched"),
            ))
            continue
        if not chan.is_active:
            declines.append(DeclineRecord(
                kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                reason=(f"channel {chan.short_id} over {trigger} trigger but not "
                        f"active (peer offline / not yet OPEN); skipping"),
            ))
            continue
        # Rule: don't add an HTLC to a channel that still has unsettled HTLCs.
        # But distinguish *why* it is unsettled:
        #   * If the unsettled HTLC is the in-flight leg of a reverse swap WE
        #     initiated (matched by payment hash in the glue), this is the
        #     expected, healthy state between issuing the swap and its on-chain
        #     funding confirming. Note it plainly -- it is not a near miss, and
        #     emphatically not a "stuck payment".
        #   * Otherwise it is a possible third-party stuck payment; surface it as a
        #     near miss rather than piling a swap HTLC on top.
        if chan.has_unsettled_htlcs:
            if chan.unsettled_is_swap:
                reason = (f"channel {chan.short_id} over {trigger} trigger but a "
                          f"reverse swap we initiated is still in flight on it "
                          f"(waiting for it to settle); skipping")
            else:
                reason = (f"channel {chan.short_id} over {trigger} trigger but has "
                          f"unsettled HTLCs (possible stuck payment); skipping")
            declines.append(DeclineRecord(
                kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                reason=reason,
            ))
            continue
        # Rule: don't swap LN -> on-chain unless an *eligible* provider is known.
        # Distinguish "none discovered yet" from "discovered but all filtered out
        # by the preferred/banned lists" so the decline log is actionable.
        if not eligible:
            if not offers:
                why = "no swap provider is known yet"
            elif config.preferred_npubs:
                why = "none of your preferred swap providers are currently available"
            else:
                why = "every available swap provider is banned"
            declines.append(DeclineRecord(
                kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                reason=(f"channel {chan.short_id} over {trigger} trigger but "
                        f"{why}; skipping"),
            ))
            continue
        # Rule: swap out the maximum a provider allows (bounded by what this
        # channel can actually send after its reserves), via the best eligible
        # provider for that amount. "Best" = lowest all-in cost plus reliability
        # penalty (flaky providers sink behind reliable ones); ties favour higher
        # PoW. The cost gate (below) is enforced inside select_provider on the
        # REAL cost, so the penalty only reorders -- it never blocks a swap.
        # Outbound-preservation floor: never drain below `min_outbound_sat` of
        # local balance. We can only move the *spendable* portion, so the amount we
        # are willing to swap is the spendable balance minus the floor. Because
        # local_sat >= spendable_local_sat, swapping this much leaves at least
        # `min_outbound_sat` of outbound behind (usually more -- the channel
        # reserve on top). If the floor already covers the whole spendable balance,
        # there is nothing to drain: a near miss worth logging (the channel was
        # over the trigger but the floor protects it).
        desired = max(0, chan.spendable_local_sat - config.min_outbound_sat)
        if desired <= 0:
            declines.append(DeclineRecord(
                kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                amount_sat=0,
                reason=(f"channel {chan.short_id} over {trigger} trigger but its "
                        f"spendable {chan.spendable_local_sat} is within the "
                        f"{config.min_outbound_sat} sat outbound floor; keeping it as "
                        f"outbound liquidity"),
            ))
            continue
        selection = select_provider(offers, desired, claim_fee, config, consumed)
        if selection is None:
            # No eligible provider both hosts the amount AND passes the cost gate,
            # at the capacity that remains after swaps already planned this pass.
            # cheapest_hosting_cost tells the cases apart for an actionable decline.
            host = cheapest_hosting_cost(offers, desired, claim_fee, config, consumed)
            if host is None:
                # Nobody can host it at their *remaining* capacity. Before blaming
                # the amount for being below every provider's minimum, check whether
                # a provider *could* have hosted it absent this pass's own earlier
                # swaps -- if so, it is simply that we have already committed that
                # provider's capacity this cycle, and this channel should just wait
                # for the next cycle (after those swaps settle / it re-advertises).
                # That is expected batching, NOT a fault or a real near miss.
                host_full = cheapest_hosting_cost(offers, desired, claim_fee, config)
                if host_full is not None:
                    amount, _cost = host_full
                    declines.append(DeclineRecord(
                        kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                        amount_sat=amount,
                        reason=(f"channel {chan.short_id} over {trigger} trigger but "
                                f"the eligible provider(s)' capacity is already "
                                f"committed to earlier swaps this cycle; will retry "
                                f"next cycle"),
                    ))
                else:
                    min_amount = min((o.min_amount_sat for o in eligible), default=0)
                    amount = min(desired, max((o.max_reverse_sat for o in eligible), default=desired))
                    declines.append(DeclineRecord(
                        kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                        amount_sat=amount,
                        reason=(f"channel {chan.short_id} swap amount {amount} below "
                                f"provider minimum {min_amount}; skipping"),
                    ))
            else:
                amount, cost_pct = host
                declines.append(DeclineRecord(
                    kind="swap", channel_id=chan.channel_id, short_id=chan.short_id,
                    amount_sat=amount,
                    reason=(f"channel {chan.short_id} swap of {amount} all-in cost "
                            f"{cost_pct:.3f}% > ceiling {config.max_swap_fee_pct}% "
                            f"(cheapest of {len(eligible)} provider(s)); skipping"),
                ))
            continue
        # Reserve the chosen provider's capacity so a later channel in this pass
        # plans against what actually remains (see `consumed` above).
        consumed[selection.offer.npub] = (
            consumed.get(selection.offer.npub, 0) + selection.amount_sat)
        amount = selection.amount_sat
        cost_pct = selection.all_in_cost_pct
        provider_desc = (f"provider {selection.offer.npub[:12]}…"
                         if selection.offer.npub else "configured provider")
        # Note the penalty in the reason only when it actually moved the ranking,
        # so the log explains a non-cheapest pick.
        penalty = selection.rank_cost_pct - cost_pct
        rank_note = (f", rank cost {selection.rank_cost_pct:.3f}% incl. "
                     f"{penalty:.3f}% reliability penalty" if penalty > 0 else "")
        actions.append(
            ReverseSwapAction(
                channel_id=chan.channel_id,
                short_id=chan.short_id,
                lightning_amount_sat=amount,
                provider_npub=selection.offer.npub,
                reason=(
                    f"channel {chan.short_id} local {chan.local_sat} over "
                    f"{trigger} trigger; swapping {amount} via {provider_desc} "
                    f"(all-in cost {cost_pct:.3f}% <= {config.max_swap_fee_pct}%{rank_note}, "
                    f"best of {len(eligible)} eligible)"
                ),
            )
        )
    return actions, declines


# --- release/version comparison -------------------------------------------
# Pure half of the update check: the glue fetches the GitHub release payload
# (network, proxy, config -- all I/O), and everything that decides what the
# answer MEANS lives here, where it is testable without a network.
#
# Ordering is numeric-component-wise: "0.1.9" < "0.1.10" (a string compare would
# get that backwards, which is the whole reason this is not a one-liner at the
# call site). A tag's leading "v" is accepted because that is how the releases
# are tagged, and a pre-release suffix ("0.2.0-rc1") is ignored for ordering --
# GitHub's ``releases/latest`` already excludes pre-releases, and the glue
# double-checks the flag, so a suffix reaching here is an oddity, not a version
# we should nag the user about being "behind".
_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)", re.IGNORECASE)


def parse_version(text: Optional[object]) -> Optional[Tuple[int, ...]]:
    """The numeric components of a version string: ``"v0.1.14"`` -> ``(0, 1, 14)``.

    Returns None for anything that does not start with a dotted number, so a
    surprise payload (an HTML error page, a renamed tag scheme) reads as "no
    usable answer" rather than as a version to compare against.
    """
    if text is None:
        return None
    match = _VERSION_RE.match(str(text))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer_version(latest: Optional[object], current: Optional[object]) -> bool:
    """Whether ``latest`` is a strictly newer release than ``current``.

    Components are compared left to right, shorter versions zero-padded, so
    "0.2" == "0.2.0" and "0.1.10" > "0.1.9". Either side failing to parse is
    False: an unreadable answer must never be reported as an available update.
    """
    latest_parts, current_parts = parse_version(latest), parse_version(current)
    if latest_parts is None or current_parts is None:
        return False
    width = max(len(latest_parts), len(current_parts))
    pad = lambda parts: parts + (0,) * (width - len(parts))  # noqa: E731
    return pad(latest_parts) > pad(current_parts)


@dataclass(frozen=True)
class ReleaseInfo:
    """One published release, as the update check understands it.

    Both fields are SANITISED, not merely copied: they come from a remote
    party's JSON and end up in the plugin's status line and in a clickable link,
    so ``version`` is rebuilt from the parsed numbers ("0.1.15" -- digits and
    dots, nothing else) and ``url`` is either an ``https://`` link from the
    payload or empty. A tag of ``0.1.15<img src=http://tracker/x>`` would
    otherwise render as markup in a Qt label and fetch a remote image from
    inside the wallet.
    """
    version: str            # normalised numeric version ("0.1.15")
    url: str                # the release page to send the user to, or ""


def extract_release(payload: Optional[Mapping]) -> Optional[ReleaseInfo]:
    """Read a GitHub ``releases/latest`` payload into a :class:`ReleaseInfo`.

    Returns None when the payload is not a usable release: missing/garbage tag,
    or explicitly a draft or pre-release. ``releases/latest`` is documented to
    exclude both, but this is a remote party's JSON -- the plugin should not
    start advertising an unreleased build because an endpoint changed its mind.
    The URL is taken from the payload rather than constructed, and dropped
    unless it is an ``https://`` link, so a doctored payload cannot put an
    arbitrary scheme in front of the user.
    """
    if not isinstance(payload, Mapping):
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name") or payload.get("name")
    parts = parse_version(tag)
    if parts is None:
        return None
    # Rebuilt from the parsed components rather than trimmed: `parse_version`
    # matches a PREFIX (so a suffix cannot break ordering), which means the raw
    # tag can carry arbitrary trailing text. Anything not part of the number is
    # dropped here, at the boundary, so nothing downstream has to wonder.
    version = ".".join(str(part) for part in parts)
    url = payload.get("html_url")
    url = str(url) if isinstance(url, str) and url.startswith("https://") else ""
    return ReleaseInfo(version=version, url=url)
