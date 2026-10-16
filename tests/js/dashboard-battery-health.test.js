//
// tests/js/dashboard-battery-health.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused tests for the battery health dashboard widgets: per-inverter (serial) tiles, the
// degradation chart with and without a commissioning baseline, and the empty states.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/dashboard-battery-health.js'), 'utf8');

function harness(views, { devices, visibleIds } = {}) {
  const elements = new Map();
  const configs = [];
  const paths = [];
  const state = { hidden: new Set() };
  const hiddenFor = (id) => (visibleIds ? !visibleIds.includes(id) : state.hidden.has(id));
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { textContent: '', hidden: true, setAttribute(k, v) { this[k] = v; } });
    return elements.get(id);
  };
  const document = {
    body: { dataset: { page: 'dashboard' } },
    documentElement: {},
    getElementById: element,
    querySelector: (selector) => {
      const id = /data-widget-id="([\w-]+)"/.exec(selector)[1];
      return { classList: { contains: () => hiddenFor(id) } };
    },
  };
  class FakeChart {
    constructor(_canvas, config) { configs.push(config); this.destroyed = false; }
    destroy() { this.destroyed = true; }
    resize() { }
  }
  const list = devices || Object.keys(views).map((id) => ({ device_id: id, device_name: `Inv ${id}` }));
  const window = {
    RCTAdmin: {
      api: async (url) => {
        paths.push(url);
        if (url === 'energy/planning-devices') return list;
        return views[decodeURIComponent(/devices\/([^/]+)\//.exec(url)[1])];
      },
      messageFrom: (error) => error.message,
    },
    RCTDashboardForecast: { chartLibrary: async () => FakeChart },
  };
  vm.runInNewContext(SOURCE, {
    document, window, AbortController, setInterval() { }, encodeURIComponent, Date, Number, Math, Array, Object,
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
  });
  const pump = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
  return { api: window.RCTDashboardBatteryHealth, element, configs, paths, state, pump };
}

const point = (at, cycles, wh, percent) => ({
  measured_at: at, battery_cycles: cycles, current_usable_capacity_wh: wh, rated_usable_capacity_wh: 10368,
  remaining_capacity_percent: percent,
});

const VIEW_A = {
  inverter_serial: 'SN-A', battery_bms_sn: 'B1', battery_replaced: false,
  commissioning: { commissioned_at: '2021-05-01T00:00:00Z', source: 'logger_derived', determined_at: '2026-10-16T00:00:00Z' },
  baseline: { date: '2021-05-01T00:00:00Z', battery_cycles: 0, rated_usable_capacity_wh: 10368,
    current_usable_capacity_wh: 10368, remaining_capacity_percent: 100, source: 'logger_derived' },
  baseline_unavailable_reason: null,
  points: [point('2026-01-10T12:00:00Z', 100, 9000, 86.81), point('2026-10-16T12:00:00Z', 128, 8538.35, 82.35)],
};

test('tiles show the latest measurement per inverter and mark a derived commissioning date unverified', async () => {
  const h = harness({ a: VIEW_A });
  h.api.sync();
  await h.pump();
  assert.deepEqual(h.paths, ['energy/planning-devices', 'energy/devices/a/battery-calibration-history']);
  assert.match(h.element('battery-usable-kwh-value').textContent, /8[.,]54 kWh/);
  assert.match(h.element('battery-remaining-percent-value').textContent, /82[.,]4 %|82[.,]3 %/);
  assert.equal(h.element('battery-cycles-value').textContent, '128');
  assert.equal(h.element('battery-last-calibration-value').textContent, new Date('2026-10-16T12:00:00Z').toLocaleDateString());
  assert.equal(h.element('battery-commissioning-date-value').textContent, new Date('2021-05-01T00:00:00Z').toLocaleDateString());
  assert.equal(h.element('battery-commissioning-date-detail').textContent, 'derived, unverified');
  assert.equal(h.element('battery-usable-kwh-detail').textContent, 'SN-A');
});

test('the chart starts at the commissioning baseline and plots remaining percent over time', async () => {
  const h = harness({ a: VIEW_A });
  h.api.sync();
  await h.pump();
  const [config] = h.configs;
  const [series] = config.data.datasets;
  assert.equal(series.label, 'SN-A');
  assert.deepEqual(Array.from(series.data, (p) => [p.y, p.cycles]), [[100, 0], [86.81, 100], [82.35, 128]]);
  assert.equal(series.data[0].x, Date.parse('2021-05-01T00:00:00Z'));
  assert.equal(h.element('battery-degradation-state').textContent, '');
  const lines = config.options.plugins.tooltip.callbacks.label({ dataset: series, raw: series.data[2] });
  assert.deepEqual(Array.from(lines).slice(0, 2), ['SN-A', 'Cycles: 128']);
});

test('without a commissioning date the chart starts at the first measurement and says so', async () => {
  const view = { ...VIEW_A, commissioning: null, baseline: null, baseline_unavailable_reason: 'no_commissioning_date' };
  const h = harness({ a: view });
  h.api.sync();
  await h.pump();
  assert.equal(h.configs[0].data.datasets[0].data.length, 2);
  assert.match(h.element('battery-degradation-state').textContent, /no commissioning date/);
  assert.equal(h.element('battery-commissioning-date-value').textContent, 'Unknown');
});

test('each inverter is its own series and tile entry, keyed by serial', async () => {
  const other = { ...VIEW_A, inverter_serial: 'SN-B', points: [point('2026-10-01T12:00:00Z', 50, 9800, 94.52)] };
  const h = harness({ a: VIEW_A, b: other });
  h.api.sync();
  await h.pump();
  assert.deepEqual(Array.from(h.configs[0].data.datasets, (d) => d.label), ['SN-A', 'SN-B']);
  assert.match(h.element('battery-cycles-value').textContent, /SN-A: 128 · SN-B: 50/);
});

test('empty states: no inverter, unknown serial, and no recorded calibration', async () => {
  let h = harness({}, { devices: [] });
  h.api.sync();
  await h.pump();
  assert.equal(h.element('battery-usable-kwh-value').textContent, 'No data yet');
  assert.equal(h.element('battery-degradation-state').textContent, 'No inverter available');
  assert.equal(h.configs.length, 0);

  h = harness({ a: { inverter_serial: null, points: [], baseline: null, commissioning: null } });
  h.api.sync();
  await h.pump();
  assert.equal(h.element('battery-cycles-value').textContent, 'Serial unknown');
  assert.equal(h.element('battery-degradation-state').textContent, 'Inverter serial not known yet');

  h = harness({ a: { ...VIEW_A, points: [], baseline: null } });
  h.api.sync();
  await h.pump();
  assert.equal(h.element('battery-usable-kwh-value').textContent, 'No data yet');
  assert.equal(h.element('battery-degradation-state').textContent, 'No calibration recorded yet');
  assert.equal(h.configs.length, 0);
});

test('hidden widgets trigger no requests, and a tile-only dashboard draws no chart', async () => {
  let h = harness({ a: VIEW_A }, { visibleIds: [] });
  h.api.sync();
  await h.pump();
  assert.deepEqual(h.paths, []);

  h = harness({ a: VIEW_A }, { visibleIds: ['battery-cycles'] });
  h.api.sync();
  await h.pump();
  assert.equal(h.element('battery-cycles-value').textContent, '128');
  assert.equal(h.configs.length, 0); // only the tile is on the dashboard: no chart is drawn
});
