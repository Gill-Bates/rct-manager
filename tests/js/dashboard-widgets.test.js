//
// tests/js/dashboard-widgets.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Catalog guards for the dashboard widget registry. Run with Node's built-in
// runner (no npm/bundler): `node --test 'tests/js/**/*.test.js'`. The original ten defaults remain a
// visual no-op: their derived layout deep-equals the former literal, the array order is the
// ordering contract, the JS registry and widget_ids.json agree, and a persisted layout retains
// the first ten entries while new widgets stay hidden.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { registry } = require('../../app/admin/static/js/dashboard-widgets.js');

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
];

// --- Derivation and the real merge logic (copied from dashboard.js) -------------------------------

// The DEFAULT_LAYOUT derivation dashboard.js now performs over the registry.
function deriveDefaultLayout(defs) {
  return defs.map((def) => ({
    id: def.id, x: def.defaultX, y: def.defaultY,
    w: def.defaultSize.w, h: def.defaultSize.h,
    visible: def.defaultVisible, label: def.label,
  }));
}

// defaultEntry / mergeWithDefault / separateOverlaps copied verbatim from dashboard.js so the no-op
// migration test exercises the real six-key projection shape. MAX_Y matches the server bound.
const MAX_Y = 1000;
const defaultEntry = (def) => ({ id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: def.visible });

function mergeWithDefault(defaultLayout, stored) {
  const byId = new Map();
  if (stored && stored.version === 1 && Array.isArray(stored.widgets)) {
    for (const widget of stored.widgets) {
      if (!widget || typeof widget.id !== 'string') continue;
      byId.set(widget.id, widget);
    }
  }
  return separateOverlaps(defaultLayout.map((def) => {
    const saved = byId.get(def.id);
    if (!saved) return defaultEntry(def);
    const inRange = (value, max) => Number.isInteger(value) && value >= 0 && value <= max;
    const valid = inRange(saved.x, 11) && inRange(saved.y, MAX_Y) &&
      Number.isInteger(saved.w) && saved.w >= 1 && saved.w <= 12 &&
      Number.isInteger(saved.h) && saved.h >= 1 && saved.h <= 100 && saved.x + saved.w <= 12;
    if (!valid) return defaultEntry(def);
    return { id: def.id, x: saved.x, y: saved.y, w: saved.w, h: saved.h, visible: saved.visible !== false };
  }));
}

function separateOverlaps(layout) {
  const placed = [];
  const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  const byPosition = layout.filter((entry) => entry.visible).sort((a, b) => a.y - b.y || a.x - b.x);
  for (const entry of byPosition) {
    let blocker;
    while ((blocker = placed.find((other) => overlaps(entry, other)))) {
      entry.y = Math.min(blocker.y + blocker.h, MAX_Y);
    }
    placed.push(entry);
  }
  return layout;
}

// --- Tests ----------------------------------------------------------------------------------------

test('derived DEFAULT_LAYOUT deep-equals today\'s literal seven-key array (no-op guard)', () => {
  assert.deepStrictEqual(deriveDefaultLayout(registry).slice(0, 10), LITERAL_DEFAULT_LAYOUT);
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

test('no-op migration: merging a persisted layout over the derived default equals merging over the literal', () => {
  // A representative persisted layout: a moved/resized visible tile, a hidden tile, and a widget
  // absent from the store (falls back to its default). version 1, six-key stored widgets.
  const stored = {
    version: 1,
    widgets: [
      { id: 'pv-power', x: 4, y: 0, w: 2, h: 2, visible: true },
      { id: 'devices', x: 0, y: 2, w: 12, h: 10, visible: true },
      { id: 'metric-count', x: 10, y: 0, w: 2, h: 2, visible: false },
      { id: 'device-count', x: 0, y: 0, w: 2, h: 2, visible: true },
    ],
  };

  const overDerived = mergeWithDefault(deriveDefaultLayout(registry), stored);
  const overLiteral = mergeWithDefault(LITERAL_DEFAULT_LAYOUT, stored);

  // Both outputs are six-key {id,x,y,w,h,visible} entries; derived vs literal defaults yield the
  // identical merged result -> the catalogization migration is a no-op.
  assert.deepStrictEqual(overDerived.slice(0, 10), overLiteral);
  assert.ok(overDerived.slice(10).every((entry) => entry.visible === false));
});
