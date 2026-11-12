<?php
/**
 * Inbound Liquidity plugin anonymous usage statistics — operator summary.
 * Deployed at https://getbarebits.com/liqanalytics/summary.php
 *
 * Aggregates the one-row-per-install table that report.php maintains.
 * Operator-only: served only when the web server authenticated the caller
 * (HTTP basic auth, see .htaccess) or the configured ANALYTICS_ACCESS_TOKEN
 * was passed as ?token=. Without either it answers 403 — forgetting the
 * .htaccess never exposes the data.
 *
 *   summary.php                HTML tables
 *   summary.php?format=json    the same aggregates as JSON
 *   summary.php?format=csv     one row per install, flattened — for a
 *                              spreadsheet or a warehouse import
 *   &active_days=N             "active" = reported within N days (default 14)
 *
 * Everything rendered is escaped; the CSV neutralises formula-leading cells.
 * No shell, no includes beyond common.php.
 */
declare(strict_types=1);
require __DIR__ . '/common.php';

header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'");
header('Referrer-Policy: no-referrer');

if (!analytics_operator_authorized()) {
    http_response_code(403);
    header('Content-Type: text/plain; charset=utf-8');
    echo "forbidden: put summary.php behind HTTP basic auth or set ANALYTICS_ACCESS_TOKEN and pass ?token=\n";
    exit;
}

function summary_h(?string $s): string {
    return htmlspecialchars((string)$s, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

/** Spreadsheet formula injection guard for CSV cells. */
function summary_csv_cell(mixed $v): string {
    if (is_bool($v)) {
        return $v ? 'true' : 'false';
    }
    if ($v === null) {
        return '';
    }
    if (is_array($v)) {
        $v = implode('|', array_map('strval', $v));
    }
    $s = (string)$v;
    if ($s !== '' && strpbrk($s[0], "=+-@\t\r") !== false) {
        $s = "'" . $s;
    }
    return $s;
}

/** count of each distinct value of $key, descending. */
function summary_group(array $rows, string $key): array {
    $out = [];
    foreach ($rows as $r) {
        $v = $r[$key] ?? null;
        $label = ($v === null || $v === '') ? '(unknown)' : (string)$v;
        $out[$label] = ($out[$label] ?? 0) + 1;
    }
    arsort($out);
    return $out;
}

/** Sum / mean / median of one numeric property across the reports that carry it. */
function summary_stats(array $rows, string $prop): array {
    $values = [];
    foreach ($rows as $r) {
        $props = $r['_props'];
        if (array_key_exists($prop, $props) && is_numeric($props[$prop])) {
            $values[] = (float)$props[$prop];
        }
    }
    if (!$values) {
        return ['n' => 0, 'sum' => 0, 'mean' => null, 'median' => null, 'max' => null];
    }
    sort($values);
    $n = count($values);
    $mid = intdiv($n, 2);
    return [
        'n'      => $n,
        'sum'    => array_sum($values),
        'mean'   => array_sum($values) / $n,
        'median' => $n % 2 ? $values[$mid] : ($values[$mid - 1] + $values[$mid]) / 2,
        'max'    => $values[$n - 1],
    ];
}

/** How many reports have this boolean property set. */
function summary_flag_counts(array $rows, array $flags): array {
    $out = [];
    foreach ($flags as $flag) {
        $on = 0;
        $known = 0;
        foreach ($rows as $r) {
            if (array_key_exists($flag, $r['_props'])) {
                $known++;
                if ($r['_props'][$flag] === true) {
                    $on++;
                }
            }
        }
        $out[$flag] = ['on' => $on, 'of' => $known];
    }
    return $out;
}

$activeDays = max(1, min(365, (int)($_GET['active_days'] ?? 14)));
$format = (string)($_GET['format'] ?? 'html');
$now = time();

$pdo = analytics_db();
$geoLoadedAt = analytics_meta_get($pdo, 'geo_loaded_at');
$rows = $pdo->query('SELECT * FROM reports ORDER BY received_at DESC')->fetchAll();
foreach ($rows as &$r) {
    $decoded = json_decode((string)$r['payload'], true);
    $r['_props'] = (is_array($decoded) && is_array($decoded['properties'] ?? null))
        ? $decoded['properties'] : [];
    $r['_active'] = ((int)$r['received_at'] >= $now - $activeDays * 86400);
}
unset($r);

$active = array_values(array_filter($rows, static fn($r) => $r['_active']));

// Level 2+ reports only, for the settings breakdowns; level 3 only for totals.
$withSettings = array_values(array_filter($active, static fn($r) => (int)$r['level'] >= 2));
$withLiquidity = array_values(array_filter($active, static fn($r) => (int)$r['level'] >= 3));

const SUMMARY_FLAGS = [
    'automation_enabled', 'manual_run_only', 'sink_configured',
    'channel_peer_configured', 'disable_submarine_swaps',
    'manage_plugin_opened_only', 'one_channel_per_peer', 'partners_strict',
    'reliability_enabled', 'peer_reliability_enabled',
    'auto_remediate_stuck_open', 'offline_autoclose_enabled',
    'update_check_enabled', 'show_goal_in_piechart', 'diag_log_enabled',
    'defer_sink_until_goal',
];
const SUMMARY_NUMERICS = [
    'liquidity_goal_sat', 'max_channels', 'max_channel_size_sat',
    'min_onchain_to_open_sat', 'max_swap_fee_pct', 'swap_trigger_pct',
    'dev_fee_pct',
];
const SUMMARY_TOTALS = [
    'channels_opened_total', 'channels_closed_total', 'force_closes_total',
    'sats_funded_total', 'swaps_completed_total', 'sats_swapped_total',
    'sink_payments_total', 'sats_sunk_total', 'dev_fee_paid_total_sat',
    'channels_open_now', 'channels_managed_now',
];

$aggregates = [
    'generated_at'       => gmdate('Y-m-d\TH:i:s\Z', $now),
    'geo_loaded_at'      => $geoLoadedAt,
    'active_days'        => $activeDays,
    'installs_total'     => count($rows),
    'installs_active'    => count($active),
    'reporting_settings' => count($withSettings),
    'reporting_liquidity' => count($withLiquidity),
    'by_level'           => summary_group($active, 'level'),
    'by_plugin_version'  => summary_group($active, 'plugin_version'),
    'by_electrum_version' => summary_group($active, 'electrum_version'),
    'by_network'         => summary_group($active, 'network'),
    'by_gui_kind'        => summary_group($active, 'gui_kind'),
    'by_os_family'       => summary_group($active, 'os_family'),
    'by_country'         => summary_group($active, 'country'),
    'flags'              => summary_flag_counts($withSettings, SUMMARY_FLAGS),
    'settings'           => [],
    'liquidity'          => [],
];
foreach (SUMMARY_NUMERICS as $prop) {
    $aggregates['settings'][$prop] = summary_stats($withSettings, $prop);
}
foreach (SUMMARY_TOTALS as $prop) {
    $aggregates['liquidity'][$prop] = summary_stats($withLiquidity, $prop);
}

if ($format === 'json') {
    header('Content-Type: application/json');
    echo json_encode($aggregates, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
    exit;
}

if ($format === 'csv') {
    header('Content-Type: text/csv; charset=utf-8');
    header('Content-Disposition: attachment; filename="liquidity-installs.csv"');
    $columns = array_merge(
        ['install_id', 'first_seen_at', 'received_at', 'level', 'country'],
        array_keys(analytics_rules_for_level(3))
    );
    $out = fopen('php://output', 'w');
    fputcsv($out, $columns);
    foreach ($rows as $r) {
        $line = [];
        foreach ($columns as $col) {
            if (in_array($col, ['install_id', 'first_seen_at', 'received_at', 'level', 'country'], true)) {
                $line[] = summary_csv_cell($r[$col] ?? null);
            } else {
                $line[] = summary_csv_cell($r['_props'][$col] ?? null);
            }
        }
        fputcsv($out, $line);
    }
    fclose($out);
    exit;
}

header('Content-Type: text/html; charset=utf-8');
?>
<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inbound Liquidity — usage statistics</title>
<style>
  body { font: 14px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 70rem; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  table { border-collapse: collapse; margin: 0.5rem 0 1.5rem; }
  th, td { border: 1px solid #ccc; padding: 0.25rem 0.6rem; text-align: left; }
  th { background: #f3f3f3; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; }
  .muted { color: #666; }
  .grid { display: flex; flex-wrap: wrap; gap: 2rem; }
</style>
<h1>Inbound Liquidity — anonymous usage statistics</h1>
<p class="muted">
  Generated <?= summary_h($aggregates['generated_at']) ?>.
  <strong><?= (int)$aggregates['installs_active'] ?></strong> wallets reported in
  the last <?= (int)$activeDays ?> days, of <?= (int)$aggregates['installs_total'] ?>
  ever seen.
  <a href="?format=json&amp;active_days=<?= (int)$activeDays ?>">JSON</a> ·
  <a href="?format=csv&amp;active_days=<?= (int)$activeDays ?>">CSV</a>
</p>

<div class="grid">
<?php foreach ([
    'by_level' => 'Sharing level',
    'by_plugin_version' => 'Plugin version',
    'by_electrum_version' => 'Electrum version',
    'by_network' => 'Network',
    'by_gui_kind' => 'Front end',
    'by_os_family' => 'OS family',
    'by_country' => 'Country',
] as $key => $title): ?>
  <div>
    <h2><?= summary_h($title) ?></h2>
    <table>
      <tr><th><?= summary_h($title) ?></th><th>Wallets</th></tr>
      <?php foreach ($aggregates[$key] as $label => $count): ?>
      <tr><td><?= summary_h((string)$label) ?></td><td class="n"><?= (int)$count ?></td></tr>
      <?php endforeach; ?>
      <?php if (!$aggregates[$key]): ?><tr><td colspan="2" class="muted">no data</td></tr><?php endif; ?>
    </table>
  </div>
<?php endforeach; ?>
</div>

<h2>Features in use <span class="muted">(of <?= count($withSettings) ?> sharing settings)</span></h2>
<table>
  <tr><th>Setting</th><th>On</th><th>Of</th></tr>
  <?php foreach ($aggregates['flags'] as $flag => $counts): ?>
  <tr>
    <td><?= summary_h($flag) ?></td>
    <td class="n"><?= (int)$counts['on'] ?></td>
    <td class="n"><?= (int)$counts['of'] ?></td>
  </tr>
  <?php endforeach; ?>
</table>

<h2>Chosen settings <span class="muted">(of <?= count($withSettings) ?> sharing settings)</span></h2>
<table>
  <tr><th>Setting</th><th>n</th><th>Mean</th><th>Median</th><th>Max</th></tr>
  <?php foreach ($aggregates['settings'] as $prop => $s): ?>
  <tr>
    <td><?= summary_h($prop) ?></td>
    <td class="n"><?= (int)$s['n'] ?></td>
    <td class="n"><?= $s['mean'] === null ? '—' : summary_h(number_format($s['mean'], 2)) ?></td>
    <td class="n"><?= $s['median'] === null ? '—' : summary_h(number_format($s['median'], 2)) ?></td>
    <td class="n"><?= $s['max'] === null ? '—' : summary_h(number_format($s['max'], 2)) ?></td>
  </tr>
  <?php endforeach; ?>
</table>

<h2>Liquidity provisioned <span class="muted">(of <?= count($withLiquidity) ?> sharing liquidity)</span></h2>
<table>
  <tr><th>Total</th><th>n</th><th>Sum</th><th>Mean</th><th>Median</th><th>Max</th></tr>
  <?php foreach ($aggregates['liquidity'] as $prop => $s): ?>
  <tr>
    <td><?= summary_h($prop) ?></td>
    <td class="n"><?= (int)$s['n'] ?></td>
    <td class="n"><?= summary_h(number_format((float)$s['sum'])) ?></td>
    <td class="n"><?= $s['mean'] === null ? '—' : summary_h(number_format($s['mean'], 1)) ?></td>
    <td class="n"><?= $s['median'] === null ? '—' : summary_h(number_format($s['median'], 1)) ?></td>
    <td class="n"><?= $s['max'] === null ? '—' : summary_h(number_format($s['max'])) ?></td>
  </tr>
  <?php endforeach; ?>
</table>

<p class="muted">
  <?php if ($geoLoadedAt !== null): ?>
    Country data: IP Geolocation by <a href="https://db-ip.com">DB-IP</a>
    (CC-BY 4.0), loaded <?= summary_h($geoLoadedAt) ?>.
  <?php else: ?>
    No IP-to-country table is loaded, so countries come only from a CDN header
    if one is configured. Run <code>import-geo.php</code> to load one.
  <?php endif; ?>
</p>
