# Inbound Liquidity Manager — Electrum plugin

> ## ⚠️ EXPERIMENTAL SOFTWARE — DO NOT USE WITH REAL FUNDS ⚠️
>
> This plugin is **experimental, unaudited, alpha-quality software**. It moves
> money **automatically and without confirmation**: it opens Lightning channels
> and broadcasts on-chain / submarine-swap transactions on your behalf.
>
> **Bugs, edge cases, or provider failures can cause partial or total LOSS OF
> FUNDS.** Use it **only** on regtest / testnet, or with amounts you are fully
> prepared to lose. There is **NO WARRANTY** of any kind. You alone are
> responsible for any funds you place under its control.

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
- **Opening a channel** (funded from on-chain coins) creates new capacity; once
  its local balance is reverse-swapped out, that capacity becomes pure inbound.

A debounced main loop runs whenever an inbound payment (on-chain or Lightning)
arrives — or any wallet/channel/swap-provider event fires — snapshots the
wallet, applies the rules below, and executes the resulting actions.

## Rules (all configurable from the Liquidity tab)

| Setting | Meaning | Default |
|---|---|---|
| `automation_enabled` | Master on/off switch — the large **ENABLED/DISABLED** slider at the top of the Settings tab (applied immediately). Off by default so you can review every setting before the plugin moves any funds | `false` |
| `min_onchain_to_open_sat` | Never open a channel below this much on-chain | `1_000_000` |
| `onchain_reserve_sat` | Always leave this much on-chain when opening | `10_000` |
| `max_channels` | Never hold more than this many channels | `2` |
| `max_swap_fee_pct` | Don't reverse-swap if the **effective all-in cost %** (percentage fee + provider mining fee + on-chain claim fee, as a share of the amount) exceeds this | `0.6` |
| `swap_trigger_pct` | Reverse-swap a channel at/above this % of capacity (local) | `25` |
| `swap_trigger_sat` | …or once local balance exceeds this many sats | `25_000` |
| `preferred_partners` | Ordered list of channel partners (`node_id@host:port`) to try opening to **first**, before Electrum's suggested peer | `""` |
| `banned_partners` | Channel partners (by node id) never opened to | `""` |
| `partners_strict` | Only ever open to preferred partners (never fall back to a suggestion) | `false` |
| `log_retention_days` | How long to keep decision-log entries (1–999) | `30` |

When a reverse swap fires it swaps out **the maximum the provider allows**
(bounded by the channel's spendable balance). Opening a channel funds with the
**maximum minus the on-chain reserve**, with the mining fee deducted so the
transaction is feasible.

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

### Decision log

Every evaluation is recorded so you can see **what the plugin did and why**. The
**Liquidity** tab has two log sub-tabs (alongside **Settings**):

- **Actions** — each executed open / reverse swap, with the amount and
  abbreviated source → destination (e.g. `on-chain → 02b2a9…6501`, or
  `117x1x0 → on-chain`).
- **Declines** — decisions *not* to act: freeze events and "near misses" (a swap
  over its trigger that was blocked by cost/provider-min/inactivity, or an open
  blocked at max-channels / the funding floor). Consecutive identical declines
  are de-duplicated so a steady frozen state logs once, not every tick.

Expanding a row (the disclosure triangle) shows the **state behind the
decision**: on-chain spendable, channel counts (active / pending), in-flight
swaps, provider economics, the thresholds in force, and every channel's
balances. Entries are persisted per-wallet in `wallet.db` and pruned to
`log_retention_days`.

## Layout

```
inbound_liquidity/
  manifest.json         plugin metadata (available_for: qt, cmdline)
  __init__.py           ConfigVars + base plugin: event wiring, snapshot, executor, decision-log store
  liquidity_manager.py  PURE rules engine (no Electrum imports) — evaluate(snapshot, config) -> DecisionResult
  qt.py                 top-level "Liquidity" main-window tab (Settings / Swap providers / Channel partners / Actions / Declines sub-tabs)
  cmdline.py            headless entry point
tests/
  test_liquidity_manager.py   unit tests for the rules engine (incl. freeze + declines)
  test_decision_log.py        glue tests: log store, retention, dedupe, build_snapshot wiring
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
done automatically (`rig/services.ensure_plugin_installed()` +
`run.py`); the rig starts it paused (`automation_enabled=false`, which is also
the shipped default) so it doesn't race the rig's own channel setup — flip the
large **ENABLED/DISABLED** slider at the top of the **Liquidity** tab's Settings
sub-tab to arm it (it applies immediately, no **Apply** needed).

Note: the rig's swap provider charges a fixed ~45,000 sat prepayment, so on the
rig's small (0.02 BTC) channels a reverse swap's **all-in** cost is several
percent. With the default `max_swap_fee_pct = 0.6` the plugin will correctly
**decline** those as uneconomical; raise the "Max reverse-swap cost" in the
**Liquidity** tab (e.g. to 10, then **Apply**) to watch swaps actually execute
against the rig provider.

## Tests

```bash
# unit (pure rules engine)
.venv-electrum/bin/python -m pytest tests/ -q

# end-to-end: launch the regtest rig, which installs + loads the plugin
python ../run.py --exit-when-ready
```
