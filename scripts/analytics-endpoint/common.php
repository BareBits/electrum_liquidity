<?php
/**
 * Shared pieces of the Inbound Liquidity plugin's anonymous-usage-statistics
 * endpoint (report.php receives, summary.php aggregates). Deployed together to
 * getbarebits.com/liqanalytics/.
 *
 * A deliberate sibling of the sister software's endpoint
 * (BareBits/cashupayserver, scripts/analytics-endpoint/) rather than a shared
 * deployment: its own path, its own database file, and its own client marker,
 * so the two populations can never be conflated and neither can pollute the
 * other's data. The structure is intentionally the same so one set of
 * operational habits covers both.
 *
 * This file is the SERVER HALF OF THE PRIVACY CONTRACT. The per-level field
 * rules in analytics_rules() mirror `ALLOWED_PROPERTIES` in the plugin's
 * inbound_liquidity/usage_stats.py, and a report carrying anything else is
 * REFUSED rather than stored -- so a future client bug (or a forged request)
 * cannot widen what is collected here. Keep the two in lockstep; the plugin's
 * tests/test_usage_stats_receiver.py asserts that they agree, field for field.
 *
 * Settings live in the constants below; edit them in place on the host.
 * Requires PHP >= 8.0 with pdo_sqlite. No other dependencies, no shell use.
 */
declare(strict_types=1);

const ANALYTICS_DB_FILE             = __DIR__ . '/liquidity-reports.sqlite';
// Must match usage_stats.CLIENT_MARKER in the plugin. A report with any other
// marker is refused -- including a well-formed report from the sister software.
const ANALYTICS_CLIENT_MARKER       = 'electrum-inbound-liquidity';
const ANALYTICS_EVENT               = 'usage_report';
const ANALYTICS_SCHEMA_VERSION      = 1;
const ANALYTICS_MAX_BODY_BYTES      = 16384;   // a level-3 report is ~1.5 KB
const ANALYTICS_RATE_LIMIT_PER_HOUR = 120;     // many wallets may share one egress IP
const ANALYTICS_RATE_WINDOW_SEC     = 3600;
const ANALYTICS_IP_RETENTION_SEC    = 7200;    // rate-limit rows purged after this
// Set to the $_SERVER key carrying the real client IP when the site sits
// behind a proxy or CDN you trust (e.g. 'HTTP_CF_CONNECTING_IP' on
// Cloudflare). Leave empty to use REMOTE_ADDR directly.
const ANALYTICS_TRUSTED_PROXY_IP_HEADER = '';
// A $_SERVER key that already carries the caller's ISO country code, when the
// host provides one ('HTTP_CF_IPCOUNTRY' on Cloudflare, 'GEOIP_COUNTRY_CODE'
// with mod_geoip). Checked BEFORE the local geo table. Leave empty if none.
const ANALYTICS_COUNTRY_HEADER = '';
// Secret for summary.php when it is not behind HTTP basic auth: pass it as
// ?token=... . Empty = token access disabled (then basic auth is the only way
// in). Use a long random string.
const ANALYTICS_ACCESS_TOKEN = '';

// PHP 8.0 hosts: array_is_list() arrived in 8.1.
if (!function_exists('array_is_list')) {
    function array_is_list(array $a): bool {
        return $a === [] || array_keys($a) === range(0, count($a) - 1);
    }
}

// --------------------------------------------------------------------------
// Database
// --------------------------------------------------------------------------

function analytics_db(): PDO {
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }
    $pdo = new PDO('sqlite:' . ANALYTICS_DB_FILE, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    $pdo->exec('PRAGMA journal_mode = WAL');
    $pdo->exec('PRAGMA busy_timeout = 5000');
    // One row per install: a newer report REPLACES the older one, so this table
    // is the current population and never a history. That is a privacy
    // property, not an optimisation -- we keep what is true now, not a
    // longitudinal record of one wallet's week-by-week behaviour.
    $pdo->exec('CREATE TABLE IF NOT EXISTS reports (
        install_id       TEXT PRIMARY KEY,
        first_seen_at    INTEGER NOT NULL,
        received_at      INTEGER NOT NULL,
        schema_version   INTEGER NOT NULL,
        level            INTEGER NOT NULL,
        country          TEXT,
        plugin_version   TEXT NOT NULL,
        electrum_version TEXT NOT NULL,
        network          TEXT NOT NULL,
        gui_kind         TEXT NOT NULL,
        os_family        TEXT NOT NULL,
        payload          TEXT NOT NULL
    )');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_reports_received ON reports (received_at)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_reports_version ON reports (plugin_version)');
    // Client IPs live ONLY here, only for the per-IP rate limit, and are
    // purged after ANALYTICS_IP_RETENTION_SEC on every request.
    $pdo->exec('CREATE TABLE IF NOT EXISTS recent_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        at INTEGER NOT NULL
    )');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_recent_ip ON recent_requests (ip, at)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_recent_at ON recent_requests (at)');
    // IP-to-country ranges (DB-IP Lite), loaded by import-geo.php. v4 as
    // integers, v6 as 16-byte blobs (SQLite compares blobs bytewise). Empty
    // until the importer is run; an empty table simply means no country.
    $pdo->exec('CREATE TABLE IF NOT EXISTS geo_v4 (start INTEGER NOT NULL, end INTEGER NOT NULL, cc TEXT NOT NULL)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_geo_v4_start ON geo_v4 (start)');
    $pdo->exec('CREATE TABLE IF NOT EXISTS geo_v6 (start BLOB NOT NULL, end BLOB NOT NULL, cc TEXT NOT NULL)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_geo_v6_start ON geo_v6 (start)');
    $pdo->exec('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
    return $pdo;
}

function analytics_meta_get(PDO $pdo, string $key): ?string {
    $stmt = $pdo->prepare('SELECT value FROM meta WHERE key = ?');
    $stmt->execute([$key]);
    $v = $stmt->fetchColumn();
    return $v === false ? null : (string)$v;
}

function analytics_meta_set(PDO $pdo, string $key, string $value): void {
    $pdo->prepare('INSERT INTO meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value')->execute([$key, $value]);
}

// --------------------------------------------------------------------------
// Request helpers
// --------------------------------------------------------------------------

function analytics_client_ip(): string {
    if (ANALYTICS_TRUSTED_PROXY_IP_HEADER !== '' && !empty($_SERVER[ANALYTICS_TRUSTED_PROXY_IP_HEADER])) {
        $ip = trim(explode(',', (string)$_SERVER[ANALYTICS_TRUSTED_PROXY_IP_HEADER])[0]);
        if (filter_var($ip, FILTER_VALIDATE_IP) !== false) {
            return $ip;
        }
    }
    return (string)($_SERVER['REMOTE_ADDR'] ?? '0.0.0.0');
}

function analytics_json(int $status, array $payload): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($payload), "\n";
}

/** Drop rate-limit rows past the retention window. Runs on every request. */
function analytics_purge_ips(PDO $pdo, int $now): void {
    $pdo->prepare('DELETE FROM recent_requests WHERE at < ?')->execute([$now - ANALYTICS_IP_RETENTION_SEC]);
}

/**
 * Is the operator allowed to read the aggregate page? Either the web server
 * authenticated them (HTTP basic auth via .htaccess) or they passed the
 * configured access token. Fails closed when neither is set up.
 */
function analytics_operator_authorized(): bool {
    foreach (['PHP_AUTH_USER', 'REMOTE_USER', 'REDIRECT_REMOTE_USER'] as $k) {
        if (!empty($_SERVER[$k])) {
            return true;
        }
    }
    if (ANALYTICS_ACCESS_TOKEN !== '' && strlen(ANALYTICS_ACCESS_TOKEN) >= 16) {
        $given = (string)($_GET['token'] ?? '');
        if ($given !== '' && hash_equals(ANALYTICS_ACCESS_TOKEN, $given)) {
            return true;
        }
    }
    return false;
}

/**
 * ISO 3166-1 alpha-2 country for the caller, or null. The CDN header first
 * (when the host provides one), then the local DB-IP table loaded by
 * import-geo.php.
 *
 * The IP itself is never written anywhere except the transient rate-limit
 * table: the lookup happens here, in memory, and only its two-letter answer is
 * stored with the report. A deployment that runs neither a CDN nor the
 * importer simply records no country, which is a perfectly good outcome.
 */
function analytics_country(PDO $pdo, string $ip): ?string {
    if (ANALYTICS_COUNTRY_HEADER !== '' && !empty($_SERVER[ANALYTICS_COUNTRY_HEADER])) {
        $cc = strtoupper(trim((string)$_SERVER[ANALYTICS_COUNTRY_HEADER]));
        if (preg_match('/^[A-Z]{2}$/', $cc) && $cc !== 'XX' && $cc !== 'T1') {
            return $cc;
        }
    }
    $packed = @inet_pton($ip);
    if ($packed === false) {
        return null;
    }
    try {
        // Ranges do not overlap, so the candidate is the last range STARTING at
        // or below the address; it matches only if the address is also within
        // its end. That is one indexed seek rather than a scan.
        if (strlen($packed) === 4) {
            $n = (int)sprintf('%u', unpack('N', $packed)[1]);
            $stmt = $pdo->prepare('SELECT cc, end FROM geo_v4 WHERE start <= ? ORDER BY start DESC LIMIT 1');
            $stmt->execute([$n]);
            $row = $stmt->fetch();
            return ($row && (int)$row['end'] >= $n) ? (string)$row['cc'] : null;
        }
        $stmt = $pdo->prepare('SELECT cc, end FROM geo_v6 WHERE start <= ? ORDER BY start DESC LIMIT 1');
        $stmt->bindValue(1, $packed, PDO::PARAM_LOB);
        $stmt->execute();
        $row = $stmt->fetch();
        return ($row && strcmp((string)$row['end'], $packed) >= 0) ? (string)$row['cc'] : null;
    } catch (Throwable $e) {
        // A missing or half-built geo table must never cost us a report.
        return null;
    }
}

// --------------------------------------------------------------------------
// Report validation — the privacy contract, enforced server-side
// --------------------------------------------------------------------------

const ANALYTICS_MAX_COUNT = 1000000000;       // counts: 1e9
const ANALYTICS_MAX_SATS  = 2100000000000000; // 21M BTC in sats

// Mirrors usage_stats.OS_FAMILIES / GUI_KINDS / NETWORKS.
const ANALYTICS_OS_FAMILIES = ['Linux', 'Windows', 'Darwin', 'BSD', 'other'];
const ANALYTICS_GUI_KINDS   = ['qt', 'cmdline', 'other'];
const ANALYTICS_NETWORKS    = ['mainnet', 'testnet', 'testnet4', 'signet', 'regtest', 'other'];

// A version, or the literal "unknown" the client sends when it cannot read one
// (usage_stats._as_version). Both halves matter: without "unknown" a wallet
// whose manifest could not be read would have every report refused.
const ANALYTICS_VERSION_RE = '/^(unknown|[0-9]{1,4}(\.[0-9]{1,4}){0,3}(-[A-Za-z0-9.]{1,24})?)$/';

/**
 * Field rules per level. Each entry: [type, constraint]. Types:
 *   enum  -> constraint is the allowed values
 *   regex -> constraint is the pattern (strings only)
 *   int   -> constraint is [min, max]
 *   num   -> int or float in [min, max] (the percentage settings)
 *   bool  -> no constraint
 *
 * A report must carry EXACTLY the keys of its level and every lower level:
 * unknown keys are refused AND missing keys are refused, so a client change
 * that widens -- or quietly narrows -- the payload fails loudly here instead
 * of being stored.
 */
function analytics_rules(): array {
    $count = ['int', [0, ANALYTICS_MAX_COUNT]];
    $sats  = ['int', [0, ANALYTICS_MAX_SATS]];
    $pct   = ['num', [0, 100]];
    $flag  = ['bool', null];

    return [
        1 => [
            'plugin_version'   => ['regex', ANALYTICS_VERSION_RE],
            'electrum_version' => ['regex', ANALYTICS_VERSION_RE],
            'python_version'   => ['regex', '/^(unknown|[0-9]{1,2}\.[0-9]{1,3})$/'],
            'os_family'        => ['enum', ANALYTICS_OS_FAMILIES],
            'gui_kind'         => ['enum', ANALYTICS_GUI_KINDS],
            'network'          => ['enum', ANALYTICS_NETWORKS],
        ],
        2 => [
            'automation_enabled'        => $flag,
            'manual_run_only'           => $flag,
            'liquidity_goal_sat'        => $sats,
            'max_channels'              => $count,
            'max_channel_size_sat'      => $sats,
            'min_onchain_to_open_sat'   => $sats,
            'onchain_reserve_sat'       => $sats,
            'max_swap_fee_pct'          => $pct,
            'swap_trigger_pct'          => $pct,
            'swap_trigger_sat'          => $sats,
            'min_swap_sat'              => $sats,
            'min_outbound_sat'          => $sats,
            'max_opens_per_day'         => $count,
            'max_closes_per_day'        => $count,
            'dev_fee_pct'               => $pct,
            'sink_configured'           => $flag,
            'channel_peer_configured'   => $flag,
            'defer_sink_until_goal'     => $flag,
            'disable_submarine_swaps'   => $flag,
            'manage_plugin_opened_only' => $flag,
            'one_channel_per_peer'      => $flag,
            'partners_strict'           => $flag,
            'reliability_enabled'       => $flag,
            'peer_reliability_enabled'  => $flag,
            'auto_remediate_stuck_open' => $flag,
            'offline_autoclose_enabled' => $flag,
            'update_check_enabled'      => $flag,
            'show_goal_in_piechart'     => $flag,
            'diag_log_enabled'          => $flag,
            'preferred_partners_count'  => $count,
            'banned_partners_count'     => $count,
            'preferred_npubs_count'     => $count,
            'banned_npubs_count'        => $count,
        ],
        3 => [
            'channels_opened_total'  => $count,
            'channels_closed_total'  => $count,
            'force_closes_total'     => $count,
            'sats_funded_total'      => $sats,
            'swaps_completed_total'  => $count,
            'sats_swapped_total'     => $sats,
            'sink_payments_total'    => $count,
            'sats_sunk_total'        => $sats,
            'dev_fee_paid_total_sat' => $sats,
            'channels_open_now'      => $count,
            'channels_managed_now'   => $count,
        ],
    ];
}

/** @return array<string, array> rules for every key a report at $level must carry */
function analytics_rules_for_level(int $level): array {
    $out = [];
    foreach (analytics_rules() as $l => $rules) {
        if ($l <= $level) {
            $out += $rules;
        }
    }
    return $out;
}

function analytics_is_uuid_v4(string $v): bool {
    return preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/', $v) === 1;
}

/**
 * Validate a decoded report. Returns ['ok' => true, 'report' => <normalized>]
 * or ['ok' => false, 'error' => <code>, 'field' => <name>]. Normalization
 * casts ints that arrived as floats with no fraction (json round-trips) and
 * nothing else -- a wrong type is an error, not a coercion.
 */
function analytics_validate(mixed $decoded): array {
    $fail = static fn(string $error, string $field = '') => ['ok' => false, 'error' => $error, 'field' => $field];
    if (!is_array($decoded) || array_is_list($decoded)) {
        return $fail('not_an_object');
    }
    $envelopeKeys = ['schema_version', 'event', 'client', 'install_id', 'sent_at', 'level', 'properties'];
    foreach (array_keys($decoded) as $k) {
        if (!in_array($k, $envelopeKeys, true)) {
            return $fail('unknown_field', (string)$k);
        }
    }
    foreach ($envelopeKeys as $k) {
        if (!array_key_exists($k, $decoded)) {
            return $fail('missing_field', $k);
        }
    }
    if ($decoded['schema_version'] !== ANALYTICS_SCHEMA_VERSION) {
        return $fail('unsupported_schema_version', 'schema_version');
    }
    if ($decoded['event'] !== ANALYTICS_EVENT) {
        return $fail('invalid_value', 'event');
    }
    if ($decoded['client'] !== ANALYTICS_CLIENT_MARKER) {
        return $fail('not_a_liquidity_plugin_request', 'client');
    }
    if (!is_string($decoded['install_id']) || !analytics_is_uuid_v4($decoded['install_id'])) {
        return $fail('invalid_value', 'install_id');
    }
    if (!is_string($decoded['sent_at'])
        || !preg_match('/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/', $decoded['sent_at'])
        || strtotime($decoded['sent_at']) === false) {
        return $fail('invalid_value', 'sent_at');
    }
    $level = $decoded['level'];
    if (!is_int($level) || $level < 1 || $level > 3) {
        return $fail('invalid_value', 'level');
    }
    $props = $decoded['properties'];
    if (!is_array($props) || (count($props) > 0 && array_is_list($props))) {
        return $fail('invalid_value', 'properties');
    }
    $rules = analytics_rules_for_level($level);
    foreach (array_keys($props) as $k) {
        if (!isset($rules[$k])) {
            return $fail('unknown_field', 'properties.' . (string)$k);
        }
    }
    $normalized = [];
    foreach ($rules as $key => [$type, $constraint]) {
        if (!array_key_exists($key, $props)) {
            return $fail('missing_field', 'properties.' . $key);
        }
        $v = $props[$key];
        switch ($type) {
            case 'enum':
                if (!is_string($v) || !in_array($v, $constraint, true)) {
                    return $fail('invalid_value', 'properties.' . $key);
                }
                break;
            case 'regex':
                if (!is_string($v) || strlen($v) > 64 || !preg_match($constraint, $v)) {
                    return $fail('invalid_value', 'properties.' . $key);
                }
                break;
            case 'int':
                if (is_float($v) && floor($v) === $v && abs($v) < 9.0e15) {
                    $v = (int)$v;
                }
                if (is_bool($v) || !is_int($v) || $v < $constraint[0] || $v > $constraint[1]) {
                    return $fail('invalid_value', 'properties.' . $key);
                }
                break;
            case 'num':
                // The percentage settings. Accepts int or float because JSON
                // does not distinguish 0 from 0.0 reliably across encoders.
                if (is_bool($v) || !(is_int($v) || is_float($v))
                    || !is_finite((float)$v)
                    || (float)$v < $constraint[0] || (float)$v > $constraint[1]) {
                    return $fail('invalid_value', 'properties.' . $key);
                }
                $v = (float)$v;
                break;
            case 'bool':
                if (!is_bool($v)) {
                    return $fail('invalid_value', 'properties.' . $key);
                }
                break;
            default:
                return $fail('internal_rule_error', $key);
        }
        $normalized[$key] = $v;
    }
    return ['ok' => true, 'report' => [
        'schema_version' => ANALYTICS_SCHEMA_VERSION,
        'event'          => ANALYTICS_EVENT,
        'client'         => ANALYTICS_CLIENT_MARKER,
        'install_id'     => $decoded['install_id'],
        'sent_at'        => $decoded['sent_at'],
        'level'          => $level,
        'properties'     => $normalized,
    ]];
}
