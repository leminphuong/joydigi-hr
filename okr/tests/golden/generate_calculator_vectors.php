<?php
// Regenerates joyokr_calculator.json from JOYOKR v1.6.45's own calculator.
//
// JOYOKR's code is not part of this repository. Copy
// inc/class-joyokr-calculator.php from the JOYOKR v1.6.45 theme next to this
// file as calculator.php (its SHA-256 must equal "source_sha256" in the JSON),
// then, in an empty scratch directory with no network access:
//
//   php -n generate_calculator_vectors.php > vectors.json
//
// and reformat with one vector per line. Inputs are random but seeded, and
// fake. Nothing else from JOYOKR is loaded.
// Golden-vector generator: runs ONLY JOYOKR's pure calculator class
// (inc/class-joyokr-calculator.php, copied here) with minimal WordPress
// stubs, and prints JSON vectors for the Django port's tests.
// Fake inputs only; no network, no database, no other theme code.

define('ABSPATH', __DIR__ . '/');
function wp_timezone(): DateTimeZone { return new DateTimeZone('Asia/Ho_Chi_Minh'); }
function wp_date(string $format) { return (new DateTimeImmutable('now', wp_timezone()))->format($format); }

require __DIR__ . '/calculator.php';

mt_srand(20261104);
function dec4(): string { return number_format(mt_rand(-50000000, 150000000) / 10000, 4, '.', ''); }
function dec2pos(int $max = 10000): string { return number_format(mt_rand(0, $max) / 100, 2, '.', ''); }

$out = ['php_version' => PHP_VERSION, 'progress' => [], 'weighted' => [], 'expected' => [], 'status' => [], 'rating' => [], 'weight_total_ok' => []];

// KR progress: DB values are DECIMAL(18,4) strings cast to float, as wpdb does.
$fixed = [
  ['0','800','1'], ['0','3','1'], ['100','0','65'], ['-50','50','0'], ['0','100','120'], ['0','100','-5'],
  ['5','5','5'], ['5','5','4'], ['9','4','5.2'], ['9','4','4.8'], ['40','80','46'], ['0','1','0.125'],
  ['0','100','2.675'], ['0','100','1.005'], ['0','100','0.285'], ['0','1000','1.115'], ['10','20','15'],
  ['100','20','60'], ['0','12','12'], ['0.0001','0.0002','0.00015'], ['99999999999999.9999','0','1'],
];
foreach ($fixed as [$s, $t, $c]) {
  $out['progress'][] = ['start' => $s, 'target' => $t, 'current' => $c, 'result' => JoyOKR_Calculator::progress((float)$s, (float)$t, (float)$c)];
}
for ($i = 0; $i < 400; $i++) {
  [$s, $t, $c] = [dec4(), dec4(), dec4()];
  if ($i % 7 === 0) { $t = $s; }
  $out['progress'][] = ['start' => $s, 'target' => $t, 'current' => $c, 'result' => JoyOKR_Calculator::progress((float)$s, (float)$t, (float)$c)];
}

// Weighted progress: progress and weight are DECIMAL(7,2) strings.
$wfixed = [
  [['progress' => '35', 'weight' => '50'], ['progress' => '55', 'weight' => '50']],
  [['progress' => '76', 'weight' => '50'], ['progress' => '15', 'weight' => '20'], ['progress' => '100', 'weight' => '30']],
  [['progress' => '0.25', 'weight' => '50'], ['progress' => '0', 'weight' => '50']],
  [['progress' => '50', 'weight' => '0'], ['progress' => '70', 'weight' => '0']],
  [],
  [['progress' => '120', 'weight' => '50'], ['progress' => '-10', 'weight' => '50']],
  [['progress' => '33.33', 'weight' => '33.33'], ['progress' => '66.67', 'weight' => '33.33'], ['progress' => '100', 'weight' => '33.34']],
];
foreach ($wfixed as $items) {
  $out['weighted'][] = ['items' => $items, 'result' => JoyOKR_Calculator::weighted_progress($items)];
}
for ($i = 0; $i < 250; $i++) {
  $items = [];
  $n = mt_rand(1, 5);
  for ($k = 0; $k < $n; $k++) { $items[] = ['progress' => dec2pos(), 'weight' => dec2pos()]; }
  $out['weighted'][] = ['items' => $items, 'result' => JoyOKR_Calculator::weighted_progress($items)];
}

// Expected progress (dates in site timezone, as_of given explicitly).
$cycles = [['2026-10-01', '2026-12-31'], ['2026-01-01', '2026-01-01'], ['2024-02-01', '2024-02-29'], ['2026-12-31', '2026-10-01'], ['2026-09-01', '2026-09-30']];
foreach ($cycles as [$a, $b]) {
  $start = new DateTimeImmutable($a);
  foreach ([-1, 0, 1, 15, 33, 45, 89, 90, 91, 92, 120] as $offset) {
    $asOf = $start->modify(($offset >= 0 ? '+' : '') . $offset . ' days')->format('Y-m-d');
    $out['expected'][] = ['start' => $a, 'end' => $b, 'as_of' => $asOf, 'result' => JoyOKR_Calculator::expected_progress($a, $b, $asOf)];
  }
}
for ($i = 0; $i < 150; $i++) {
  $s = (new DateTimeImmutable('2024-01-01'))->modify('+' . mt_rand(0, 900) . ' days');
  $e = $s->modify('+' . mt_rand(0, 400) . ' days');
  $o = $s->modify('+' . mt_rand(-30, 450) . ' days');
  $out['expected'][] = ['start' => $s->format('Y-m-d'), 'end' => $e->format('Y-m-d'), 'as_of' => $o->format('Y-m-d'), 'result' => JoyOKR_Calculator::expected_progress($s->format('Y-m-d'), $e->format('Y-m-d'), $o->format('Y-m-d'))];
}

// Health status.
$sfixed = [[100, 50, 15], [99.99, 100, 15], [37, 37, 15], [22, 37, 15], [21.99, 37, 15], [21.98, 37, 15], [0, 0, 15], [50, 37, 0], [36, 37, 0], [120, 10, 15], [10, 120, 150]];
foreach ($sfixed as [$a, $e, $t]) {
  $out['status'][] = ['actual' => $a, 'expected' => $e, 'threshold' => $t, 'result' => JoyOKR_Calculator::status((float)$a, (float)$e, (float)$t)];
}
for ($i = 0; $i < 150; $i++) {
  [$a, $e, $t] = [dec2pos(), dec2pos(), dec2pos(3000)];
  $out['status'][] = ['actual' => $a, 'expected' => $e, 'threshold' => $t, 'result' => JoyOKR_Calculator::status((float)$a, (float)$e, (float)$t)];
}

// Rating with JOYOKR's default ranges.
$ranges = [['min' => 0, 'max' => 0.3, 'label' => 'Failed'], ['min' => 0.3, 'max' => 0.6, 'label' => 'Below Expectation'], ['min' => 0.6, 'max' => 0.8, 'label' => 'Good'], ['min' => 0.8, 'max' => 1, 'label' => 'Excellent']];
foreach ([-0.1, 0, 0.29, 0.3, 0.5999, 0.6, 0.79, 0.8, 0.99, 1, 1.5] as $score) {
  $out['rating'][] = ['score' => $score, 'result' => JoyOKR_Calculator::rating((float)$score, $ranges)];
}

// round($x, 2) on doubles at and next to x.xx5 midpoints, produced by the
// same kinds of arithmetic the calculator does. '%.17g' identifies the
// exact double so Python can rebuild it bit for bit.
$out['round2'] = [];
for ($i = 0; $i < 10000; $i += 37) {
  foreach ([($i + 0.5) / 100, $i / 100 + 0.005, ($i * 10 + 5) / 1000, (($i + 0.5) / 10000) * 100.0, ($i / 3) * 0.03] as $v) {
    $out['round2'][] = ['value' => sprintf('%.17g', $v), 'result' => round($v, 2)];
  }
}
foreach (['1.0049999999999999', '0.28499999999999998', '28.499999999999996', '2.6749999999999998', '1.0050000000000001', '99.995000000000005', '99.994999999999990', '0.0049999999999999992', '33.334999999999994'] as $s) {
  $out['round2'][] = ['value' => sprintf('%.17g', (float)$s), 'result' => round((float)$s, 2)];
}

// Submit/approve weight check (okr-service.php:100,142): SQL SUM of
// DECIMAL(7,2) arrives as a decimal string, cast to float, then compared.
foreach (['100.00', '99.99', '100.01', '99.995', '100.005', '0.00', '200.00', '99.98', '100.02'] as $total) {
  $out['weight_total_ok'][] = ['total' => $total, 'result' => !(abs((float)$total - 100.0) > 0.01)];
}

echo json_encode($out, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION);
