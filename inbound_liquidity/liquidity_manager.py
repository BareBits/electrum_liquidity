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


# --- startup / shutdown readiness -----------------------------------------
# The watchdogs read a peer's live reachability (``chan.is_active()``) to decide
# it is offline -- faulting the peer and/or feeding an "it's gone" uptime metric.
# But at wallet startup the Lightning layer has not finished (re)connecting to
# peers yet, and at shutdown it is tearing those connections down; in both
# windows a perfectly healthy peer reads as "not connected". Acting on that would
# blame the peer for *our* transitional state -- an immediate hard fault or a
# poisoned uptime ratio that can force-close a channel to a peer that was fine.
#
# These two PURE predicates (no clock, no Electrum) encode the guard. The glue
# supplies the wall clock (as ``elapsed_sec`` since the wallet was loaded), the
# network-connected flag, and -- for the per-peer gate -- whether we have already
# seen *this* peer online at least once since load. Keeping them here keeps the
# race logic unit-testable in isolation.

def is_wallet_ready(network_connected: bool, elapsed_sec: float,
                    grace_sec: float) -> bool:
    """Whether the wallet has settled enough for the plugin to take *any*
    automated action.

    Ready only when we have a live server connection AND a bounded startup grace
    has elapsed since load, giving the Lightning layer a fair chance to connect
    to its peers first. Before that, every automated action (open / swap / close)
    is deferred -- the tick becomes a no-op and a later tick re-checks. A
    non-positive ``grace_sec`` disables the time gate (ready as soon as
    connected), which the tests use to exercise the connected/disconnected axis
    on its own.
    """
    if not network_connected:
        return False
    return elapsed_sec >= grace_sec


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
def normalize_node_id(connect_str: Optional[str]) -> str:
    """The node pubkey (lowercased) from a connect string ``pubkey@host:port``
    or a bare ``pubkey``. Used to match preferred/banned entries by identity,
    ignoring any attached address (so a partner can be banned by pubkey alone)."""
    if not connect_str:
        return ""
    return connect_str.strip().split("@", 1)[0].strip().lower()


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
    """
    out: List[str] = []
    seen: set = set()

    def consider(entries: Sequence[str]) -> None:
        for entry in entries:
            if not entry or not entry.strip():
                continue
            nid = normalize_node_id(entry)
            if not nid or nid in banned or nid in exclude or nid in seen:
                continue
            seen.add(nid)
            out.append(entry.strip())

    consider(preferred)
    if not strict:
        consider(suggested)
    if penalties:
        # Stable sort keeps the preferred-first order among equal penalties.
        out.sort(key=lambda e: penalties.get(normalize_node_id(e), 0.0))
    return out


@dataclass(frozen=True)
class ChannelSnapshot:
    """A single channel, as the engine needs to see it."""
    channel_id: str            # hex short-hand id, for logging / addressing actions
    short_id: str              # human-friendly short channel id ("117x1x0")
    capacity_sat: int
    local_sat: int             # our outbound balance
    remote_sat: int            # inbound liquidity (what we can still receive)
    spendable_local_sat: int   # available_to_spend(LOCAL), i.e. sendable after reserves
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
    max_swap_fee_pct: float        # skip reverse swaps whose effective all-in cost % exceeds this (rule: 0.6)
    swap_trigger_pct: float        # reverse-swap a channel at/over this % of capacity local (rule: 25)
    swap_trigger_sat: int          # ...OR once local balance exceeds this many sats (rule: 25_000)
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
    # Number of channel opens the glue has executed within the trailing 24h
    # (rolling window), for the ``max_opens_per_day`` ceiling. The glue counts
    # these from its persisted action-timestamp store; 0 in the pure tests unless
    # set explicitly.
    opens_last_24h: int = 0


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


Action = Union[OpenChannelAction, ReverseSwapAction]


@dataclass(frozen=True)
class DeclineRecord:
    """A decision *not* to act, with the reason. Surfaced in the decision log's
    "Declines" view. ``kind`` is one of:
      * "freeze" -- the whole tick was skipped because something is in flight,
      * "open"   -- a channel open was considered but a rule blocked it,
      * "swap"   -- a reverse swap was considered but a rule blocked it.
    Only *near-miss* declines are recorded (a candidate that was on the verge of
    acting but got blocked) -- idle "nothing to do" ticks produce no records.
    """
    kind: str
    reason: str
    channel_id: Optional[str] = None
    short_id: Optional[str] = None
    amount_sat: Optional[int] = None


@dataclass(frozen=True)
class DecisionResult:
    """The full outcome of one evaluation: what to do and why nothing else."""
    actions: Tuple[Action, ...] = ()
    declines: Tuple[DeclineRecord, ...] = ()
    frozen: Optional[str] = None   # non-None reason if the tick was frozen


def evaluate(snapshot: LiquiditySnapshot, config: LiquidityConfig) -> DecisionResult:
    """Map (state, thresholds) -> a DecisionResult.

    Order of reasoning:
      1. If automation is off, do nothing (and record nothing).
      2. GLOBAL FREEZE: if any plugin-relevant transaction is still in flight
         (a channel open not yet OPEN, or a reverse swap not yet swept), take
         *no* action this tick -- neither opens nor swaps. This stops the plugin
         re-firing every tick while, e.g., a channel open is still confirming.
      3. Otherwise consider a channel open first (so a cold wallet builds
         capacity before draining it), then reverse swaps. A freshly opened
         channel is not yet ``is_active`` and so is never reverse-swapped in the
         same cycle that opens it.
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

    open_action, open_decline = _decide_channel_open(snapshot, config)
    if open_action is not None:
        actions.append(open_action)
    if open_decline is not None:
        declines.append(open_decline)

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
        over_pct = chan.local_sat >= (config.swap_trigger_pct / 100.0) * chan.capacity_sat
        over_sat = chan.local_sat > config.swap_trigger_sat
        if not (over_pct or over_sat):
            # Below both triggers: nothing to do, and not a near miss.
            continue
        trigger = "pct" if over_pct else "sat"
        # The channel wants to be drained; from here on, anything that blocks it
        # is a near miss worth logging.
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
        desired = chan.spendable_local_sat
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
