# Privacy Policy — Inbound Liquidity Manager

_Last updated: November 11, 2026_

The Inbound Liquidity Manager is an **Electrum plugin** you install and run on your own
machine. There is no hosted service, we have no access to your wallet, and we cannot read
anything it holds.

The plugin contains **no crash reporting and no install phone-home**. There are exactly three
circumstances in which it contacts a server of ours, and you control all three:

| What | Default | Contacts |
|---|---|---|
| The development fee | On, adjustable to 0% | A Lightning address, to pay accrued fees |
| The update check | **Off** until you say yes | `api.github.com` |
| Anonymous usage statistics | **Off** until you choose a level | `getbarebits.com/liqanalytics/` |

Everything else the plugin talks to — your Electrum server, swap providers over nostr, your
channel peers — it talks to because that is how it does the job you installed it for, using
the connections and the proxy Electrum is already configured with.

---

## 1. Anonymous usage statistics (off by default)

The plugin can send us a small report so we know how many people use it, which settings
matter, and how much liquidity it actually provisions. It is **off until you choose
otherwise**, and the plugin behaves identically whether you switch it on or not.

You are asked once, on a consent screen that shows **the exact report** each level would
send — built by the same code that builds the real report, so what you are shown is what
would be sent. You can change or revoke your choice at any time under
**Wallet → Inbound Liquidity → Advanced → Usage statistics**, which shows the same live
preview.

### We never send, at any level

- Your wallet addresses, or any address you have ever used.
- Your transactions, payments, invoices, or channel ids.
- Your on-chain or Lightning balances, or your channel capacities.
- Your sink Lightning address, your chosen channel peers' node ids, or swap providers' npubs.
- Your wallet name, your file paths, your seed, or any key material.

Where a setting *is* an identifier, the report carries only whether it is set, or how many
entries a list has — `sink_configured: true`, never the address; `preferred_npubs_count: 2`,
never the npubs.

### The levels

Each level includes the ones below it.

**Level 1 — Count me.** That this wallet runs the plugin, and what it is running: a random
identifier, the plugin/Electrum/Python versions, your operating-system family, whether you
run the desktop or command-line wallet, and which Bitcoin network you are on.

**Level 2 — Settings.** Plus the settings you chose, as numbers and booleans: your liquidity
goal, channel limits, fee ceiling, swap triggers, daily ceilings, dev-fee percentage, and
which features you switched on or off.

**Level 3 — Liquidity.** Plus lifetime totals of what the plugin has done for you: how many
channels it opened and closed, how many sats it funded into them, how many swaps it completed
and how many sats it moved, how many sink payments it made, and the dev fee paid. Plus how
many channels are open right now and how many of them the plugin manages.

Level 3 is **running totals and counts only**. A lifetime total says what the plugin did; a
balance would say what your wallet holds, and we do not report the latter.

### The identifier

Minted the moment you opt in, as a random UUIDv4. It is **not derived** from your wallet,
its name, its keys, its seed, its file path, or anything else — so it carries no information
about you and nobody, us included, can work backwards from it. A wallet that never opts in
never has one at all.

Opting back out keeps the identifier locally, so that if you later opt back in you are the
same install rather than a second one. Opting out stops reports; it does not delete what was
already received.

### Scope: per wallet

The choice, the identifier and the level are **properties of the wallet**, like every other
setting this plugin has. If you run several wallets you will be asked for each, and each
reports separately — so a three-wallet user appears in our data as three installs. We chose
accurate consent over an accurate user count.

### Cadence, and what our side sees

One report when you opt in, then one a week. Requests go through **Electrum's configured
proxy**, like every other HTTP call the wallet makes, so a Tor user's usage report is not the
one request that reveals their IP.

Because this is an HTTPS request, our server necessarily sees the IP address it came from.
That IP is used for two things and then forgotten:

- a **per-address rate limit**, for which it is held at most **two hours**; and
- resolving a **country**, which happens in memory at the moment the report arrives. Only
  the two-letter country code is stored with the report — never the address it came from,
  and nothing finer than the country.

The country comes from a CDN header if one is configured, otherwise from a local copy of the
free [DB-IP IP-to-Country Lite](https://db-ip.com) database. Your IP is never sent to DB-IP
or anyone else to do this; we hold the table and look it up ourselves.

Our side keeps **one row per identifier**: a new report replaces the previous one. We do not
keep a week-by-week history of any wallet.

### How this is enforced, not just promised

- The complete list of fields each level may contain is a single allowlist in
  [`inbound_liquidity/usage_stats.py`](inbound_liquidity/usage_stats.py). The report builder
  intersects its output with that list, so a field added by mistake is not sent.
- The receiver ([`scripts/analytics-endpoint/`](scripts/analytics-endpoint/)) enforces its own
  copy of the same list and **refuses** a report carrying anything else — so even a bug in the
  plugin could not widen what we collect.
- The test suite asserts the two lists agree field for field, and separately builds a level-3
  report from a wallet deliberately stuffed with addresses, txids, channel ids, node ids,
  npubs and balances, then fails if any of them appears anywhere in the result.

### Switching it off for a whole installation

Set the environment variable `ELECTRUM_INBOUND_LIQUIDITY_USAGE_ENDPOINT` to `disabled` (or
`off`, `none`, `0`). Nothing is then sent regardless of what any wallet has chosen — useful
for packagers and managed deployments. Reporting is also suppressed outright on **regtest**.

---

## 2. The development fee

The plugin charges an optional development fee (default 0.1%, adjustable down to 0) on the
reverse swaps and sink payments it performs. Accrued fees are paid out as a Lightning payment
to a Lightning address — by default `electrum_liqhelper@getbarebits.com`.

Your wallet fetches the invoice from that host over HTTPS, so the host necessarily learns your
wallet's **IP address** (unless you use a proxy), the **fee amount**, and the **time**. It
learns nothing about your balances, your channels, or what the fee was charged on.

This payment carries **no identifier and no comment**, and is not linked in any way to a usage
report: the two share no value, so a fee payment cannot be joined to a report. Set the fee to
`0` to switch it off entirely, or change the payout address to one of your own.

> Note that a Lightning payment inherently discloses to the payee that a payment arrived, and
> to the nodes that route it that *something* was routed. That is true of all Lightning
> payments, not a property of this plugin.

---

## 3. The update check (off by default)

If you say yes to the one-time prompt, once a day the plugin asks
`api.github.com/repos/BareBits/electrum_liquidity/releases/latest` whether a newer release
exists. GitHub therefore learns your wallet's IP address (through your proxy, if configured)
and that it is running — exactly what any HTTPS request to GitHub discloses.

Nothing is ever downloaded or installed; the plugin only tells you a newer version exists and
links to the release page. Change your mind under
**Advanced → General → "Check GitHub for plugin updates"**.

---

## 4. Services the plugin uses to do its job

These are not data flows to us. They are listed so you know what your wallet talks to.

- **Your Electrum server** — whichever you have configured. It sees your addresses and
  transactions, as it does for all Electrum use.
- **Swap providers, over nostr** — the plugin discovers providers via Electrum's nostr
  submarine-swap extension and negotiates swaps with them. A provider learns the swap amounts
  and the invoices involved, which is inherent to performing a swap.
- **Your channel peers** — a Lightning peer learns what any channel counterparty learns.
- **Your liquidity sink** — if you configure one, your wallet fetches invoices from that
  Lightning address, which learns your IP, the amounts, and the times.

The plugin writes diagnostic logs only if you switch them on
(**Advanced → General → "Write diagnostic log files"**, off by default). They stay on your
machine and contain no keys or seeds.

---

## Questions

Open an issue at <https://github.com/BareBits/electrum_liquidity/issues>.
