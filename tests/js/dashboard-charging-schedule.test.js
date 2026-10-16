//
// tests/js/dashboard-charging-schedule.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused tests for the "Charging schedule" dashboard widget: plan status states, gap-free uneven
// slots, tooltip content, and the destroy-on-hide / destroy-on-rerender lifecycle.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/dashboard-charging-schedule.js'), 'utf8');

function harness(plan) {
  const elements = new Map();
  const configs = [];
  const charts = [];
  const paths = [];
  const state = { shown: true, plan };
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { textContent: '', hidden: true, setAttribute(k, v) { this[k] = v; } });
    return elements.get(id);
  };
  const document = {
    body: { dataset: { page: 'dashboard' } },
    documentElement: {},
    getElementById: element,
    querySelector: () => ({ classList: { contains: () => !state.shown } }),
  };
  class FakeChart {
    constructor(_canvas, config) { configs.push(config); charts.push(this); this.options = config.options; this.data = config.data; }
    destroy() { this.destroyed = true; }
    resize() { }
    update() { }
  }
  const window = {
    RCTAdmin: {
      api: async (url) => { paths.push(url); return url.endsWith('planning-devices') ? [{ device_id: 'a/b', device_name: 'Inv' }] : state.plan; },
      messageFrom: (error) => error.message,
    },
    RCTDashboardForecast: { chartLibrary: async () => FakeChart },
  };
  vm.runInNewContext(SOURCE, {
    document, window, AbortController, setInterval() { }, encodeURIComponent,
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
  });
  const pump = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve)); };
  return { api: window.RCTDashboardChargingSchedule, element, configs, charts, paths, state, pump };
}

const SLOTS = [
  { start: '2026-10-12T21:00:00Z', end: '2026-10-12T22:00:00Z', action: 'idle', target_soc_percent: 40, max_power_w: null, reason: 'forecast PV covers demand' },
  { start: '2026-10-12T22:00:00Z', end: '2026-10-13T00:30:00Z', action: 'charge', target_soc_percent: 80, max_power_w: 3000, reason: 'forecast deficit, charged as late as permitted' },
];

test('an ok plan draws one chart with uneven slots, a summary label and a plain-language tooltip', async () => {
  const h = harness({ status: 'ok', slots: SLOTS });
  h.api.sync();
  await h.pump();
  assert.deepEqual(h.paths, ['energy/planning-devices', 'energy/devices/a%2Fb/dynamic-plan']);
  assert.equal(h.configs.length, 1);
  const [power] = h.configs[0].data.datasets;
  assert.equal(JSON.stringify(power.data.map((p) => p.y)), "[0,0,3,3]");
  assert.equal(h.element('charging-schedule-canvas')['aria-label'].startsWith('Charging schedule'), true);
  const lines = h.configs[0].options.plugins.tooltip.callbacks.label({ raw: power.data[2] });
  assert.ok(lines.includes('Power: 3.00 kW'));
  assert.ok(lines.some((line) => line.startsWith('Reason: Expected shortfall')));
});

test('a non-ok status shows a text state and no chart', async () => {
  const h = harness({ status: 'spot_disabled', slots: [] });
  h.api.sync();
  await h.pump();
  assert.equal(h.configs.length, 0);
  assert.equal(h.element('charging-schedule-state').textContent, 'Spot market disabled');
});

test('hiding the widget destroys the chart and a later refresh creates a new one', async () => {
  const h = harness({ status: 'ok', slots: SLOTS });
  h.api.sync();
  await h.pump();
  h.state.shown = false;
  h.api.sync();
  assert.equal(h.charts[0].destroyed, true);
  h.state.shown = true;
  h.api.sync();
  await h.pump();
  assert.equal(h.charts.length, 2);
});

test('an API failure shows an error state', async () => {
  const h = harness();
  h.state.plan = null;
  h.api.sync();
  await h.pump();
  assert.equal(h.configs.length, 0);
  assert.notEqual(h.element('charging-schedule-state').textContent, '');
});
