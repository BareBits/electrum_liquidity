# Inbound Liquidity Manager — Electrum plugin

> ## ⚠️ AS-IS SOFTWARE — DO NOT USE WITH SIGNIFICANT FUNDS ⚠️
>
> This moves
> money **automatically and without confirmation**: it opens Lightning channels
> and broadcasts on-chain / submarine-swap transactions on your behalf. While we are using this plugin in production, **Bugs, edge cases, or provider failures can cause partial or total LOSS OF
> FUNDS.** Use it **only** with amounts you are fully prepared to lose. There is **NO WARRANTY** of any kind. You alone are responsible for any funds you place under its control.
> For this reason, we suggest using this pattern: use this plugin on a wallet that has a dedicated purpose of receiving payments. Forward those funds to a cold wallet when they reach any significant amount.

An Electrum plugin that automatically manages **inbound Lightning liquidity** by
opening channels and performing **submarine (reverse) swaps**, on top of
Electrum's existing Nostr submarine-swap extension.

## What it does

The wallet is a *receiver*: to accept Lightning payments it needs inbound
liquidity (remote-side channel balance). This plugin keeps it topped up:

- A **reverse swap** (Lightning → on-chain) drains a channel's local/outbound
  balance out to on-chain coins, which restores that channel's *inbound*
  capacity. This is the core "make room to receive again" move, triggered once a
  channel's local balance grows past a threshold (i.e. after you've received).
- A **liquidity sink** (optional) does the same job by *paying* a Lightning
  address instead: the outbound leaves the channel over Lightning, freeing
  exactly the same inbound capacity, but it settles in seconds with no on-chain
  claim, no swap provider and no cost gate. Point it at an account you control
  (`myname@strike.me`) and the sats land there rather than in this wallet's
  on-chain balance. See [The liquidity sink](#the-liquidity-sink).
- **Opening a channel** (funded from on-chain coins) creates new capacity; once
  its local balance is drained out, that capacity becomes pure inbound.

A debounced main loop runs whenever an inbound payment (on-chain or Lightning)
arrives — or any wallet/channel/swap-provider event fires — snapshots the
wallet, applies the rules below, and executes the resulting actions.

## Rules (configurable from the Liquidity tab)

The everyday settings live on the **Settings** sub-tab; power-user knobs live on
the **Advanced** sub-tab, and the partner/provider lists on their own sub-tabs.
This table lists the most-used settings and their tab — the tabs also carry more
knobs not shown here (reliability tuning, offline auto-close, daily action
ceilings, diagnostics, etc.).

| Setting | Tab | Meaning | Default |
|---|---|---|---|
| `automation_enabled` | Settings | Master on/off switch — the large **ENABLED/DISABLED** slider at the top of the Settings tab (applied immediately). Off by default so you can review every setting before the plugin moves any funds | `false` |
| `min_onchain_to_open_sat` | Settings | Never open a channel while on-chain spendable is below this. When it is below Electrum's stock channel-funding floor `MIN_FUNDING_SAT` (200 000), the plugin lowers that floor to this value at startup — re-asserted every tick so the configured value always wins — so smaller channels can be opened | `60_000` |
| `max_channels` | Settings | Never hold more than this many channels | `2` |
| `liquidity_goal_sat` | Settings | **Liquidity goal** — the channel size to aim for. *Automatically close small channels and re-open bigger channels if we have sufficient funds to reach this goal.* Entered in whichever unit you have Electrum set to (BTC / mBTC / bits / sat), with the fiat equivalent alongside when exchange rates are on; stored as sats. `0` turns the rule off. See [Replacing undersized channels](#replacing-undersized-channels-the-liquidity-goal) | `100_000` |
| `max_swap_fee_pct` | Settings | **Max fee to move LN → on-chain** — don't reverse-swap if the **effective all-in cost %** (percentage fee + provider mining fee + on-chain claim fee, as a share of the amount) exceeds this | `0.9` |
| `swap_trigger_pct` | Settings | Reverse-swap a channel at/above this % of capacity (local) | `25` |
| `swap_trigger_sat` | Settings | …or once local balance exceeds this many sats | `25_000` |
| `dev_fee_pct` | Settings | Optional contribution to plugin development, charged on what the plugin drains from a channel — the on-chain amount received from a reverse swap, or the amount paid to your liquidity sink (0 = off). Paid automatically to a fixed payout address | `0.1` |
| `sink_address` | Settings | **Liquidity sink** — a Lightning address (`user@domain`), LNURL-pay string or LNURL-pay URL to drain outbound into *instead of* reverse-swapping it on-chain. A payment that fails is retried at half the amount, down to 10 000 sat; if every attempt fails, a reverse swap is used instead. Empty = off (swap only). See [The liquidity sink](#the-liquidity-sink) | `""` (off) |
| `disable_submarine_swaps` | Settings | **Use the liquidity sink only** — never make a reverse swap, not even when the sink fails. Honoured even with no sink set, which leaves nothing able to drain a channel: the plugin says so in its decision log rather than swapping against your wishes | `false` |
| `manage_plugin_opened_only` | Settings | **Only manage channels the plugin opened** — when on, the plugin only reverse-swaps channels it opened itself; a channel you opened by hand is left entirely alone and its outbound is never drained. When off, every channel is managed. On by default, so an untouched install never touches a channel you set up yourself | `true` |
| `onchain_reserve_sat` | Advanced | Always leave this much on-chain when opening | `10_000` |
| `min_outbound_sat` | Advanced | **Keep outbound per channel** — never let a reverse swap drain a channel's outbound (local) balance below this, so the wallet keeps some ability to send. Applied per channel to the swappable amount; `0` drains everything for maximum inbound | `0` |
| `log_retention_days` | Advanced | How long to keep decision-log entries (1–999) | `30` |
| `log_buffer_lines` | Log | How many recent log lines the **Log** sub-tab keeps in memory (100–100 000). Never written to disk | `2000` |
| `log_capture_ln` | Log | Also capture Electrum's own Lightning/swap logging (peer manager, node rater, channels, routing, submarine swaps) in the **Log** sub-tab. Noisy, but it is where "no channel partner available" is actually decided | `false` |
| `log_capture_debug` | Log | Force debug-level logging while on. Electrum normally produces debug records already (just hidden), so this matters only if you started Electrum with a reduced verbosity — in which case it also makes those records appear in Electrum's own log file. The previous level is restored when you turn it off | `false` |
| `preferred_partners` | Channel partners | Ordered list of channel partners (`node_id@host:port`) to try opening to **first**, before the peers Electrum suggests (up to 50 suggestions are tried in turn if one refuses the open) | `""` |
| `banned_partners` | Channel partners | Channel partners (by node id) never opened to | `""` |
| `partners_strict` | Channel partners | Only ever open to preferred partners (never fall back to a suggestion) | `false` |
| `update_check_enabled` | Advanced | Ask GitHub once a day whether a newer release of this plugin exists, and show it in the **Status** footer. Off until you say otherwise — you are asked once, by dialog, the first time the plugin loads. Nothing is ever downloaded or installed | `false` |

When a reverse swap fires it swaps out **the maximum the provider allows**
(bounded by what Electrum can actually *send* over the channel, minus any
`min_outbound_sat` floor). That send ceiling is the channel's spendable balance
less the routing-fee budget Electrum reserves for a max-amount payment — the
same figure Electrum's own "Max" button uses — because a reverse swap's
Lightning side is two payments (the swap invoice and the provider's mining-fee
prepayment) and both have to fit. Opening a channel funds with the **maximum
minus the on-chain reserve**, with the mining fee deducted so the transaction is
feasible.

If that swap **fails**, the plugin does not give up on the cycle: it walks the
other eligible providers in cost order (up to 3 attempts in total, within a 500
second budget) and retries with each in turn, so one unreachable or unwilling
provider no longer costs you a whole 3-minute channel cooldown. Every provider
in the cascade must still pass the `max_swap_fee_pct` gate on its **own** real
all-in cost — a failover is never an excuse to overpay — and because providers
advertise different capacities, each retry is sized to what *that* provider will
actually host.

The cascade stops immediately, and never retries, once funds may have moved.
Electrum's pre-payment checks (cost sanity, the provider's `createswap` reply,
script/RHASH/invoice verification, locktime) all raise *before* the Lightning
payment is issued, so those failures are safe to retry elsewhere. Past that
point — or on any error the plugin cannot positively classify — the swap is left
alone for reconciliation to resolve, because retrying could drain the same
channel twice. A provider-independent condition on our side (a stale local tip)
also ends the cascade rather than burning attempts on a guaranteed repeat.
Failures are still recorded against the provider's reliability exactly as
before, so a repeatedly-failing provider sinks in the ranking and stops being
tried first.

By default the plugin manages **only the channels it opened itself**
(`manage_plugin_opened_only`, on the Settings tab, is on out of the box), so a
channel you set up by hand is never drained unless you turn that switch off.
Which channels the plugin opened is shown in the **Managed by** column the plugin
adds to Electrum's own **Channels** tab (`Plugin` vs `Manual`).

To preserve some outbound (send) capacity within the channels it *does* manage,
`min_outbound_sat` holds back a per-channel floor.

### The liquidity sink

A reverse swap is not the only way to free inbound capacity. Paying *anyone* over
Lightning moves local balance out of a channel and turns it into inbound, and if
you pay an account you control, the sats are not spent so much as moved. Set
`sink_address` to a Lightning address (`myname@strike.me`), an `lnurl1…` string
or an LNURL-pay URL, and the plugin drains through it in preference to swapping.

Compared with a reverse swap, a sink payment:

* settles in **seconds** rather than waiting on an on-chain claim,
* needs **no swap provider** — no nostr discovery, no provider ranking, no
  prepayment, and no reliability bookkeeping,
* clears **no cost gate** — `max_swap_fee_pct` still bounds it, but as the
  routing-fee budget rather than as a veto, so a channel no provider would
  swap cheaply can still be drained,
* sends the sats **to that address**, not to this wallet's on-chain balance.
  This is the real trade-off, and the reason the feature is off by default.

**Halving on failure.** A large Lightning payment can fail where a smaller one
succeeds — no single route with enough liquidity, or an MPP split that cannot be
assembled. So a failed attempt is retried at **half** the amount, repeatedly,
down to a floor of **10 000 sat**, with one final attempt clamped to exactly the
floor. A 200 000 sat drain is therefore attempted at:

```
200_000 → 100_000 → 50_000 → 25_000 → 12_500 → 10_000
```

Each rung is a genuinely different payment (its own invoice from the sink), and
the first one that settles ends the ladder. A drain below 10 000 sat is not
attempted at all.

**Falling back to a swap.** If every rung fails, the plugin falls back to the
reverse swap it had planned all along — provider ranking, failover and all —
unless you turn `disable_submarine_swaps` on. Failures that are the *endpoint's*
fault rather than the amount's (an address that will not resolve, something that
is not an LNURL-pay endpoint, an invoice for the wrong amount) skip the ladder
entirely and go straight to the fallback: no smaller amount would fare better.

**The one case that stops everything.** A failed `pay_invoice` does not prove
nothing was sent — Electrum gives up on a payment at its own 120s timeout while
HTLCs may still be unresolved. So after every failure the plugin checks whether a
payment with that hash is still in flight, and if it is, it stops: no halving,
and no swap fallback either. Draining a channel twice — past the outbound floor
you configured, for more than the plugin ever decided to send — is the one
outcome worth giving up a whole tick to avoid. This is the same commit-boundary
reasoning the swap-provider cascade uses around `add_reverse_swap`.

**Sink-only mode.** `disable_submarine_swaps` means what it says: no reverse swap
is ever made, not even as a backstop. It is honoured even when no sink address is
set, which leaves nothing able to drain a channel — the plugin records that in the
decision log rather than quietly swapping. Silently moving your funds on-chain
through a third party after you asked for no swaps would be the worse failure.

### Replacing undersized channels (the liquidity goal)

Without this rule a wallet gets permanently capped by the size of the channels it
happened to open first: the plugin opens its `max_channels`, later accumulates
enough on-chain to fund something far bigger, and refuses to — because it is
already at the ceiling. The **liquidity goal** lets a channel be *replaced*
rather than merely counted.

A channel is closed to make room for a bigger one only when **all** of these
hold:

1. a goal is configured (`liquidity_goal_sat`, `0` = off);
2. we are **at** `max_channels` — below it a bigger channel can simply be opened
   alongside, so closing anything would be gratuitous;
3. on-chain spendable minus `onchain_reserve_sat` could fund a channel that
   actually **reaches** the goal (and clears the funding floor);
4. the channel was **opened by the plugin** — a channel you opened by hand is
   never closed by this rule, whatever `manage_plugin_opened_only` is set to;
5. its **capacity** is below the goal;
6. it is **not over a swap trigger** — it holds no outbound the plugin is about
   to convert into inbound;
7. no other close is already **settling** — see below;
8. it has no unsettled HTLCs; and
9. its peer is reachable, so the close can be **cooperative**. A channel whose
   peer has gone is left to the offline auto-close watchdog, which has uptime
   evidence to justify the cost of a force-close — this rule will never pay a
   force-close fee and a CSV timelock merely to resize.

Before closing, the plugin also checks that a channel partner would actually be
available to reopen to; if none is, it logs a decline and leaves the channel
alone rather than losing its inbound for nothing. (The closing channel's own peer
is exempted from the one-channel-per-peer guard for that check — it is about to
be freed by this very close.)

Condition 6 is what stops the rule churning: the plugin funds its channels with
no push, so a freshly opened channel has local == capacity and is over the
percentage trigger by construction. It cannot be opened and immediately closed
again — it only becomes replaceable once the plugin has genuinely drained it into
inbound liquidity.

At most **one** channel is replaced per evaluation (the smallest qualifier) —
and, via condition 7, one at a time *across* evaluations too. That second half
matters: a channel being closed stays in the wallet until its closing tx is mined,
and by then it just reads as "not active", so without it the next tick would find
the next undersized channel eligible and close that as well, on the strength of
the same on-chain balance. One funding amount buys exactly one replacement.

The close goes through the same path as every other close the plugin makes:
cooperative first, force-close only as a documented backstop, and counted against
`max_closes_per_day`.

> **Upgrading:** unlike most of the plugin's rules this one ships **active**
> (`100_000` sat), so it applies to existing wallets on upgrade. A plugin-opened
> channel smaller than the goal that has already been drained will be replaced
> the first time you are at your channel ceiling with the funds to do better. Set
> `liquidity_goal_sat` to `0` on the Settings tab to turn it off.

### In-flight freeze

Before doing anything, each tick checks whether any plugin-relevant transaction
is still **in flight** and, if so, takes **no action at all** that tick (no
opens, no swaps). "In flight" is derived purely from wallet state, so it counts
ops regardless of who started them:

- a channel whose funding is not yet `OPEN` (`ChannelState` PREOPENING / OPENING
  / FUNDED), and
- a reverse swap whose funding tx is broadcast but not yet swept
  (`SwapManager.get_pending_swaps()`, which self-clears on settle/refund).

This stops the plugin from, e.g., opening a second channel every tick while the
first open is still confirming.

A channel that is merely **still confirming** (its funding tx has not yet reached
the depth the peer requires) is treated differently: it blocks only a further
channel *open* — for up to the **confirming-open freeze** (Advanced tab, default
3 days) — and never blocks reverse swaps on other, already-open channels. In a
high-fee environment a funding tx can legitimately take days to confirm, and
stalling unrelated channels for that long is pure lost value.

Two exceptions keep one genuinely wedged operation from freezing automation
forever:

- a channel stuck *opening* past the **stuck channel-open timeout** (Advanced
  tab, default 60 min) is treated as wedged by an unresponsive peer — it no
  longer counts toward the freeze, so automation can resume, and
- a reverse swap whose funding is broadcast but still unswept past the **stuck
  reverse-swap timeout** (Advanced tab, default 3 h) likewise stops counting
  toward the freeze. The swap is still tracked and its provider still faulted
  separately — this only releases the freeze so other opens/swaps can proceed.

### Closing a wedged channel open

Releasing the freeze (above) is cheap and reversible. *Closing* the channel is
neither — a force-close costs a mining fee and locks the funds behind a CSV
timelock — so it is governed by its own, far more conservative set of gates, all
of which must pass:

1. **Not merely confirming.** While the funding tx is still accruing
   confirmations, nothing is wrong and the clock does not even start.
2. **The wedge clock**, timed from the first moment the channel looked wedged —
   *not* from when it was created, so time spent waiting for confirmations never
   counts against it. Default 6 h (Advanced tab).
3. **Repeated, spread-out evidence**: the channel must look wedged on several
   separate checks (default 5) spanning at least a minimum interval (default
   60 min). Evaluation ticks are event-driven and can fire in bursts, so a count
   alone would be nearly free to satisfy.

An absolute 7-day backstop from channel creation overrides gate 1, so a funding
tx that never confirms cannot keep the funds locked indefinitely.

### Cooperative close always comes first

**The plugin never force-closes a channel without first attempting — or ruling
out — a cooperative close.** Both watchdogs (wedged opens and offline
auto-close) funnel through a single close path that:

- attempts a cooperative close whenever the peer can be reached at all, retrying
  up to 3 times, each with its own window;
- force-closes only once cooperation has had that window (default 10 min) and
  either failed or was impossible because no peer connection existed;
- never touches a `WE_ARE_TOXIC` channel, where Electrum states that
  force-closing by us is unsafe.

The close action logged in the Actions view records which of these applied
("after N cooperative close attempt(s)" vs "peer was never reachable to close
cooperatively").

### Heartbeat

The main loop is event-driven, but a **periodic heartbeat** (every 10 minutes
per wallet) also re-evaluates, so time-based checks — the stuck-open / stuck-swap
timeouts, offline auto-close uptime sampling and force-close deadline, stuck-swap
reconciliation, and the dev-fee retry backoff — keep advancing even on a quiet
wallet that emits no events (exactly the situation a dead peer creates). Each
heartbeat tick is the same guarded, idempotent evaluation as an event-driven one.

### Status

One evaluation ("tick") can run for minutes — opening a nostr provider session,
walking a channel-open candidate list one peer at a time, or waiting out a
reverse swap — and from outside that is indistinguishable from a plugin doing
nothing. The **Settings** sub-tab therefore carries a **Status** footer at the
bottom, naming the step in flight (`resolving channel partners`, `connecting to partner 02b2a9…6501
(2 of 4)`, `attempting swap with npub1qx…8h2 (250,000 sat)`, …) together with
when it started.

Between ticks it shows a *resting* state, and these are deliberately distinct so
"armed and idle" never reads the same as "switched off": `sleeping`, `automation
disabled`, `idle (manual run only)`, `warming up` (see below), `wallet locked`
(see below) and `not started`. Every exit path — including an error mid-tick —
lands on one of them, so the line can never stick on a step that finished long
ago.

`warming up` is not about your funds: it means the plugin is waiting for a server
connection, for the wallet to finish syncing, or for its startup window (2
minutes after a wallet loads) to elapse. That window exists because Lightning
reconnects to peers asynchronously — acting inside it could blame a healthy peer
for being "offline" when it simply has not been dialed yet, or fire a reverse
swap whose Lightning leg cannot route yet. The log line names whichever of the
three is actually blocking.

`wallet locked` means your wallet is password-protected and currently locked, so
nothing can be signed. Note that this is *not* the same password prompt you get
when opening the wallet: that one only decrypts the wallet file, and on desktop
Electrum it does not unlock the keystore. Unlocking is a separate, deliberate
step — **Wallet → Unlock**, or the **Unlock wallet…** button the plugin shows in
the Status footer while it is locked.

When an automatic run finds the wallet locked, the plugin offers to unlock it,
and the dialog says it is the plugin asking. Dismissing it is fine: automation
stays switched on, the plugin keeps deferring, and it will not ask again for an
hour — the Unlock button stays there in the meantime. Unlocking by any route
resumes the plugin immediately, without waiting for the next heartbeat. The
password is Electrum's own in-memory unlock, exactly what its other background
automation uses; the plugin never stores a password itself.

Unlike `warming up`, waiting does not clear this one, which is why it has its own
name. "Run now" cannot bypass it either. Automation on a password-protected
wallet therefore only runs while the wallet is unlocked.

Pressing **Run now** skips the startup window deliberately — you are there and
watching, and can retry — but it still requires a connected, fully synced
wallet.

The plugin writes **nothing** into Electrum's own status bar (the area beside
your balance). Everything it has to say lives inside the **Liquidity** tab: this
footer for what a tick is doing now, and the log sub-tabs below for what it did.

### Update check (opt-in)

The Status footer can also tell you whether the plugin itself is out of date —
`version 0.1.14 (up to date)`, or `update available: 0.1.15 (running 0.1.14)`
next to a link to the release notes.

It is **off until you say otherwise**. The first time the plugin loads it asks,
once, with a dialog; whichever way you answer is remembered, and you can change
it later from **Advanced → Check GitHub for plugin updates**. Declining is
final — you are not asked again — and a headless (`cmdline`) install, where
there is no dialog, never checks unless the setting is turned on explicitly.

What it does when enabled: once a day, a single `GET` to GitHub's
`releases/latest` endpoint for this repository, through Electrum's configured
proxy (so a Tor setup stays a Tor setup), with a short timeout. A failed or
rate-limited lookup is logged and retried tomorrow. The reason it is opt-in is
that it discloses your IP and the fact that this wallet is running to a third
party who otherwise has no part in operating it.

What it never does is download or install anything. A plugin that can rewrite
its own code turns a compromised GitHub account — or one intercepted response —
into code execution against a wallet holding funds. You get a version number and
a link; updating stays a step you take deliberately.

### Decision log

Every evaluation is recorded so you can see **what the plugin did and why**. The
**Liquidity** tab has three log sub-tabs (alongside **Settings**, **Swap
providers**, **Channel partners** and **Advanced**):

- **Actions** — each executed open / reverse swap, with the amount and
  abbreviated source → destination (e.g. `on-chain → 02b2a9…6501`, or
  `117x1x0 → on-chain`).
- **Declines** — decisions *not* to act: freeze events and "near misses" (a swap
  over its trigger that was blocked by cost/provider-min/inactivity, or an open
  blocked at max-channels / the funding floor). Consecutive identical declines
  are de-duplicated so a steady frozen state logs once, not every tick.
- **Faults** — provider and channel-peer faults (timeouts, RPC errors, stuck
  swaps, failed opens, force-closes) that feed the decaying reliability penalties
  used to rank providers and channel partners.

Expanding a row (the disclosure triangle) shows the **state behind the
decision**: on-chain spendable, channel counts (active / pending), in-flight
swaps, provider economics, the thresholds in force, and every channel's
balances. Entries are persisted per-wallet in `wallet.db` and pruned to
`log_retention_days`.

### Log tab

The three views above answer "what did the plugin decide". The **Log** sub-tab
answers "and what did it see while deciding": it merges the decision log with the
plugin's own Python logging, captured live into a bounded in-memory ring
(`log_buffer_lines`, default 2000). Filter by level or by substring, follow the
tail, and **Copy** / **Save to file…** what you are looking at.

This is the view to open when an open is declined for having no reachable
channel partner. That decision is made partly inside Electrum's own Lightning
subsystems, so tick `log_capture_ln` in the capture options on the **Log** tab
itself to include them (and `log_capture_debug` if you launched Electrum with
a reduced verbosity).

The buffer is **memory only** — nothing is written to disk, and it is cleared
when Electrum restarts; "Save to file…" is the deliberate way to keep a copy.
**Clear** discards the captured lines only and never touches your decision log.
For persistent on-disk records, enable **Write diagnostic log files** instead.

### Why is there no channel partner?

An empty channel-open candidate list collapses several different situations into
one sentence, so the decline now carries the arithmetic behind it as an
expandable detail line — for example:

```
partner resolution: preferred 0/1, suggested 3/3, routing gossip, 1 banned
```

`kept/total` for each list, the routing mode (`trampoline` vs `gossip` — entirely
different suggestion paths, each of which comes up empty for its own reason), and
a count per rule that dropped a candidate: banned, already-have-a-channel-with
(the one-channel-per-peer guard), duplicate, or unparseable (a typo'd preferred
partner). If asking Electrum for a suggestion failed outright, that is named too
rather than reported as "the network has nobody for you". The same line is
written to the log, so it shows up in the **Log** sub-tab as well.

## Layout

```
inbound_liquidity/
  manifest.json         plugin metadata (available_for: qt, cmdline)
  __init__.py           ConfigVars + base plugin: event wiring, snapshot, executor, decision-log
                        store, dev fee, daily ceilings, offline auto-close, MIN_FUNDING_SAT floor override
  liquidity_manager.py  PURE rules engine (no Electrum imports) — evaluate(snapshot, config) -> DecisionResult
  qt.py                 top-level "Liquidity" main-window tab (Settings / Swap providers /
                        Channel partners / Advanced / Actions / Declines / Faults / Log sub-tabs)
  qt_widgets.py         custom Qt widgets (the ENABLED/DISABLED ToggleSwitch)
  swap_transport.py     nostr swap-provider discovery / transport helper
  diag_log.py           optional on-disk diagnostic log (opt-in; no key material)
  log_buffer.py         bounded in-memory log capture backing the Log sub-tab (no Electrum imports)
  cmdline.py            headless entry point
tests/                  pure-engine unit tests + Electrum-glue tests covering the rules engine,
                        decision log, provider/peer reliability, dev fee, daily caps, offline
                        auto-close, startup readiness, diagnostics, the Qt tab, and the funding floor
```

The decision logic lives in `liquidity_manager.py` as a pure function
(`evaluate()` returns a `DecisionResult` of actions + declines + an optional
freeze reason; `decide()` remains as a thin actions-only wrapper), so it is
fully unit-testable without a running Electrum. The plugin glue (`__init__.py`)
snapshots Electrum state, calls `evaluate()`, executes the returned actions
against `lnworker` / the swap manager, and records every decision to the
per-wallet decision log.

## Install / run

Internal Electrum plugins are auto-authorized, so the plugin is **symlinked**
into the Electrum checkout's `electrum/plugins/` and enabled with
`setconfig plugins.inbound_liquidity.enabled true`. In the regtest rig this is
done automatically (`e2e/rig/services.ensure_plugin_installed()` +
`e2e/run.py`); the rig starts it paused (`automation_enabled=false`, which is also
the shipped default) so it doesn't race the rig's own channel setup — flip the
large **ENABLED/DISABLED** slider at the top of the **Liquidity** tab's Settings
sub-tab to arm it (it applies immediately, no **Apply** needed).

Note: on the rig's small (0.02 BTC) channels a reverse swap's **effective all-in
cost** (provider percentage + provider mining fee + on-chain claim fee, as a
share of the amount) is several percent, because the fixed provider/mining fees
dominate at small amounts. With the default `max_swap_fee_pct = 0.9` the plugin
will correctly **decline** those as uneconomical; raise **"Max fee to move LN →
on-chain"** on the **Liquidity → Settings** tab (e.g. to 10, then **Apply**) to
watch swaps actually execute against the rig provider.

## Tests

Two tiers. The fast **unit + Electrum-glue** tests live in `tests/`; the heavy
**regtest end-to-end** rig lives in `e2e/` (see [`e2e/README.md`](e2e/README.md)).

The tests import from a real Electrum, so they need a venv with Electrum
installed editable. `e2e/setup.sh` builds one at `e2e/.venv-electrum`.

```bash
# unit (pure rules engine) + Electrum-glue tests — from this plugin directory
e2e/.venv-electrum/bin/python -m pytest tests/ -q

# end-to-end (heavy; needs bitcoind + Fulcrum + docker):
cd e2e && ./setup.sh                        # one-time: builds the venv, clones Electrum
python e2e/run.py --exit-when-ready         # smoke: bring the whole stack up
RUN_RIG_E2E=1 e2e/.venv-electrum/bin/python -m pytest e2e/tests -q -s   # full e2e suite
```
