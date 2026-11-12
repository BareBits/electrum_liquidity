# Anonymous usage statistics — the receiving end

The server half of the plugin's opt-in usage reporting. The client half is
`inbound_liquidity/usage_stats.py` (what a report contains) plus the sender in
`inbound_liquidity/__init__.py`.

Deployed at **`https://getbarebits.com/liqanalytics/`** — a path of its own,
with its own database, deliberately separate from the sister software's
`/analytics/` endpoint (BareBits/cashupayserver) so the two populations can
never be conflated. A report from the sister software is *refused* here, and
vice versa: each checks the other's `client` marker and rejects it.

## Files

| File | What it does |
|---|---|
| `report.php` | Receives one report. The only endpoint a wallet ever talks to. |
| `common.php` | Settings, database schema, and the per-level field rules. |
| `summary.php` | Operator-only aggregates (HTML / JSON / CSV). |
| `import-geo.php` | Loads the DB-IP IP-to-country table. Run at deploy, then monthly. |

## Install

1. Copy all three files to the document root path above.
2. `chown` the directory to the web server user — `report.php` creates
   `liquidity-reports.sqlite` (plus WAL files) next to itself on first request.
3. Protect `summary.php`. Either put the directory behind HTTP basic auth:

   ```apache
   # .htaccess
   <Files "summary.php">
       AuthType Basic
       AuthName "analytics"
       AuthUserFile /path/outside/webroot/.htpasswd
       Require valid-user
   </Files>
   <FilesMatch "\.(sqlite|sqlite-wal|sqlite-shm)$">
       Require all denied
   </FilesMatch>
   ```

   …or set `ANALYTICS_ACCESS_TOKEN` in `common.php` to a long random string and
   fetch `summary.php?token=…`. **It fails closed**: with neither configured,
   `summary.php` answers 403. Deny direct access to the `.sqlite*` files either
   way.
4. Requires PHP ≥ 8.0 with `pdo_sqlite`. Nothing else — no Composer, no shell.
5. Check it: `curl https://getbarebits.com/liqanalytics/report.php` → `{"ok":true}`.
   Behind a CDN, `?whoami=1` shows which IP the rate limiter sees.

## Settings (`common.php`)

| Constant | Default | Notes |
|---|---|---|
| `ANALYTICS_RATE_LIMIT_PER_HOUR` | 120 | Per IP. Generous because many wallets may share one egress IP (a VPN, a university, Tor exits). |
| `ANALYTICS_IP_RETENTION_SEC` | 7200 | How long a client IP survives in the rate-limit table. Nothing else ever stores one. |
| `ANALYTICS_TRUSTED_PROXY_IP_HEADER` | `''` | Set to e.g. `HTTP_CF_CONNECTING_IP` behind Cloudflare, else the rate limit sees only the CDN. |
| `ANALYTICS_COUNTRY_HEADER` | `''` | e.g. `HTTP_CF_IPCOUNTRY`. Checked *before* the local table. |
| `ANALYTICS_ACCESS_TOKEN` | `''` | `summary.php` / `import-geo.php` access when not behind basic auth. |

## Country data

A report is tagged with the country the connection came from, and the IP is then
discarded. Two sources, in order: a CDN header if `ANALYTICS_COUNTRY_HEADER` is
configured, then the local DB-IP table. With neither, reports simply carry no
country — nothing fails.

```bash
php import-geo.php                       # download this month's CSV and load it
php import-geo.php /path/to/file.csv.gz  # load one you fetched yourself
```

Or over HTTP on a host without SSH, behind the same auth as `summary.php`:
`https://.../liqanalytics/import-geo.php?token=…`. Run it at deploy and then
roughly monthly; it takes seconds to a minute for ~700k ranges.

The load is **atomic**: rows stream into staging tables that are swapped in
under one transaction, so a lookup never sees a half-built table. An import
carrying fewer than 1000 IPv4 ranges is **refused** rather than applied — that
is the truncated-download guard, and the previous table survives untouched.
`ZZ` rows (DB-IP's "unknown") are skipped so "we don't know" never becomes a
country on the summary page.

The data is the free **DB-IP IP-to-Country Lite** database under CC-BY 4.0. The
licence requires attribution — `summary.php` prints "IP Geolocation by DB-IP"
wherever countries are shown, and it must stay there.

Nothing in this path touches the reports table, and no IP from a report is
stored: the lookup happens in memory and only its two-letter answer is kept.

## The privacy contract

`analytics_rules()` in `common.php` mirrors `ALLOWED_PROPERTIES` in
`usage_stats.py`, and a report must carry **exactly** the keys its level
allows — unknown keys *and* missing keys are both refused. So a future client
bug cannot widen what is collected, and a forged request cannot smuggle a field
past it. `tests/test_usage_stats_receiver.py` asserts the two lists agree field
for field, so they cannot drift apart silently.

What that means in practice, all verified by that test suite:

- A report with an extra property → `400 unknown_field`.
- A report missing one → `400 missing_field`.
- A value of the wrong type, out of range, or outside an enum → `400 invalid_value`.
- A body over 16 KB → `413`, refused before it is read.

Storage is **one row per install id**: a newer report replaces the older one, so
the table is the current population and never a week-by-week history of any one
wallet. Client IPs are written only to `recent_requests`, only for the rate
limit, and are purged on every request past the retention window.

## Reading the data

```
summary.php                      # HTML tables
summary.php?format=json          # the same aggregates as JSON
summary.php?format=csv           # one row per install, flattened
summary.php?active_days=30       # "active" = reported within N days (default 14)
```

Counts are broken down by level, plugin and Electrum version, network, front
end, OS family and country; then feature flags, chosen settings (mean / median /
max), and the liquidity totals for installs sharing at level 3.

## Local testing

```bash
cd scripts/analytics-endpoint
php -S 127.0.0.1:8731 -t .
curl http://127.0.0.1:8731/report.php          # {"ok":true}
```

The plugin's rig e2e test (`e2e/tests/test_usage_stats_e2e.py`) stands this up
automatically and points a real Electrum daemon at it, so the full path —
consent, build, POST, validate, store — is exercised against *this* code rather
than a stub.
