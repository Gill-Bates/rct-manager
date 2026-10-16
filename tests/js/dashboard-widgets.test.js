//
// tests/js/dashboard-widgets.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Catalog guards for the dashboard widget registry. Run with Node's built-in runner:
// `node --test tests/js/*.test.js`. Layout behavior is exercised from the shipped dashboard.js source.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { registry } = require('../../app/admin/static/js/dashboard-widgets.js');
const DASHBOARD_SOURCE = fs.readFileSync(
  path.join(__dirname, '../../app/admin/static/js/dashboard.js'),
  'utf8',
);

// --- Fixtures -------------------------------------------------------------------------------------

// The former hand-written literal from dashboard.js (lines 21-31), inlined verbatim as the baseline.
const LITERAL_DEFAULT_LAYOUT = [
  { id: 'pv-power', x: 0, y: 0, w: 2, h: 2, visible: true, label: 'PV power' },
  { id: 'grid-power', x: 2, y: 0, w: 2, h: 2, visible: true, label: 'Grid power' },
  { id: 'house-power', x: 4, y: 0, w: 2, h: 2, visible: true, label: 'House consumption' },
  { id: 'battery-soc', x: 6, y: 0, w: 2, h: 2, visible: true, label: 'Battery level' },
  { id: 'tsdb-status', x: 8, y: 0, w: 2, h: 2, visible: true, label: 'TSDB export' },
  { id: 'metric-count', x: 10, y: 0, w: 2, h: 2, visible: true, label: 'Exposed metrics' },
  { id: 'devices', x: 0, y: 2, w: 12, h: 8, visible: true, label: 'Inverter overview' },
  { id: 'device-count', x: 0, y: 0, w: 2, h: 2, visible: false, label: 'Inverters' },
  { id: 'connected-count', x: 2, y: 0, w: 2, h: 2, visible: false, label: 'Connected' },
  { id: 'days-to-calibration', x: 4, y: 0, w: 2, h: 2, visible: false, label: 'Days to calibration' },
];

const EXPECTED_ID_ORDER = [
  'pv-power', 'grid-power', 'house-power', 'battery-soc', 'tsdb-status',
  'metric-count', 'devices', 'device-count', 'connected-count', 'days-to-calibration',
  'spot-price-current', 'spot-market-status', 'spot-price-curve',
  'solar-forecast-status', 'pv-forecast-curve', 'charging-schedule',
];

// --- Production layout helpers -------------------------------------------------------------------

function productionLayoutHelpers() {
  const start = DASHBOARD_SOURCE.indexOf('  const { registry } = window.RCTDashboardRegistry;');
  const end = DASHBOARD_SOURCE.indexOf('  // Two passes:', start);
  assert.ok(start >= 0 && end > start, 'dashboard layout helper block must remain discoverable');

  const source = `${DASHBOARD_SOURCE.slice(start, end)}
globalThis.__layoutHelpers = { DEFAULT_LAYOUT, defaultEntry, MAX_Y, mergeWithDefault, separateOverlaps };`;
  const context = { window: { RCTDashboardRegistry: { registry } } };
  vm.runInNewContext(source, context);
  return context.__layoutHelpers;
}

const { DEFAULT_LAYOUT, defaultEntry, MAX_Y, mergeWithDefault, separateOverlaps } = productionLayoutHelpers();
const plain = (value) => JSON.parse(JSON.stringify(value));

// --- Tests ----------------------------------------------------------------------------------------

test('production DEFAULT_LAYOUT deep-equals the former literal seven-key array', () => {
  assert.deepStrictEqual(plain(DEFAULT_LAYOUT.slice(0, 10)), LITERAL_DEFAULT_LAYOUT);
});

test('registry array order deep-equals today\'s DEFAULT_LAYOUT id order (ordering contract)', () => {
  assert.deepStrictEqual(registry.map((def) => def.id), EXPECTED_ID_ORDER);
});

test('registry ids set-equal widget_ids.json and are unique', () => {
  const jsonPath = path.join(__dirname, '..', '..', 'app', 'admin', 'widget_ids.json');
  const jsonIds = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  const registryIds = registry.map((def) => def.id);

  assert.equal(registryIds.length, new Set(registryIds).size, 'registry ids must be unique');
  assert.equal(jsonIds.length, new Set(jsonIds).size, 'widget_ids.json ids must be unique');
  assert.deepStrictEqual(new Set(registryIds), new Set(jsonIds));
});

test('production merge preserves valid stored values and hides newly introduced widgets', () => {
  const stored = {
    version: 1,
    widgets: [
      { id: 'pv-power', x: 4, y: 0, w: 2, h: 2, visible: true },
      { id: 'devices', x: 0, y: 2, w: 12, h: 10, visible: true },
      { id: 'metric-count', x: 10, y: 0, w: 2, h: 2, visible: false },
    ],
  };

  const result = plain(mergeWithDefault(stored));
  assert.equal(result.find((entry) => entry.id === 'pv-power').x, 4);
  assert.equal(result.find((entry) => entry.id === 'devices').h, 10);
  assert.equal(result.find((entry) => entry.id === 'metric-count').visible, false);
  assert.ok(result.slice(10).every((entry) => entry.visible === false));
});

test('two overlapping widgets at the bottom edge resolve to the default layout, not an infinite loop', () => {
  // Regression (JS-03 / dashboard-layout P2): a saved layout with two visible, overlapping widgets
  // pinned at y = MAX_Y cannot be pushed further down. The resolver must report this as
  // unresolvable (no forward progress / past MAX_Y) and the merge must fall back to the known-good
  // default rather than spinning the while loop forever.
  const stored = {
    version: 1,
    widgets: [
      { id: 'pv-power', x: 0, y: MAX_Y, w: 2, h: 2, visible: true },
      { id: 'grid-power', x: 1, y: MAX_Y, w: 2, h: 2, visible: true }, // overlaps pv-power at the bottom edge
    ],
  };

  // separateOverlaps alone reports the unresolvable case as null (no infinite loop, forward progress enforced).
  const unresolvable = separateOverlaps([
    { id: 'a', x: 0, y: MAX_Y, w: 2, h: 2, visible: true },
    { id: 'b', x: 1, y: MAX_Y, w: 2, h: 2, visible: true },
  ]);
  assert.equal(unresolvable, null);

  // The merge terminates and yields the factory default (which has no overlaps at the bottom edge).
  const result = mergeWithDefault(stored);
  const expectedDefault = DEFAULT_LAYOUT.map(defaultEntry);
  assert.deepStrictEqual(plain(result), plain(expectedDefault));
  // No widget sits at or past the vertical bound in the fallback.
  assert.ok(result.every((entry) => entry.y + entry.h <= MAX_Y));
});

test('three bottom-edge widgets (y=999, h=1) resolve to the default layout without looping', () => {
  // Regression for the reviewer's exact repro: three visible 1x1 widgets at y = MAX_Y - 1 all fit
  // the per-widget bound (y + h = MAX_Y), but the second resolves to y = MAX_Y and the third can no
  // longer be pushed below it (nextY = MAX_Y + 1 > MAX_Y). The resolver must report this rather than
  // spinning, and the merge must fall back to the known-good default.
  const stored = {
    version: 1,
    widgets: [
      { id: 'pv-power', x: 0, y: MAX_Y - 1, w: 1, h: 1, visible: true },
      { id: 'grid-power', x: 0, y: MAX_Y - 1, w: 1, h: 1, visible: true },
      { id: 'house-power', x: 0, y: MAX_Y - 1, w: 1, h: 1, visible: true },
    ],
  };

  const unresolvable = separateOverlaps([
    { id: 'a', x: 0, y: MAX_Y - 1, w: 1, h: 1, visible: true },
    { id: 'b', x: 0, y: MAX_Y - 1, w: 1, h: 1, visible: true },
    { id: 'c', x: 0, y: MAX_Y - 1, w: 1, h: 1, visible: true },
  ]);
  assert.equal(unresolvable, null);

  const result = mergeWithDefault(stored);
  assert.deepStrictEqual(plain(result), plain(DEFAULT_LAYOUT.map(defaultEntry)));
  assert.ok(result.every((entry) => entry.y + entry.h <= MAX_Y));
});

test('a single bottom-pinned widget that overflows the bound (y=MAX_Y, h=2) falls back to default', () => {
  // The y+h<=MAX_Y guard in mergeWithDefault: a lone, non-overlapping widget at the bound would
  // otherwise pass separateOverlaps unchanged and cross MAX_Y. It must be rejected as invalid and
  // replaced by its default entry.
  const stored = {
    version: 1,
    widgets: [{ id: 'pv-power', x: 0, y: MAX_Y, w: 2, h: 2, visible: true }],
  };

  const result = mergeWithDefault(stored);
  const pv = result.find((entry) => entry.id === 'pv-power');
  const pvDefault = DEFAULT_LAYOUT.find((def) => def.id === 'pv-power');
  assert.deepStrictEqual(plain(pv), plain(defaultEntry(pvDefault)));
  assert.ok(result.every((entry) => entry.y + entry.h <= MAX_Y));
});
