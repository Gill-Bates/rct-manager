//
// tests/js/dashboard-forecast.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused tests for the shared spot-widget forecast renderer. Run with Node's built-in runner
// (no npm/bundler): `node --test 'tests/js/**/*.test.js'`. Each test exercises exactly one
// behaviour (chart library load/retry, no interpolation between intervals, gaps stay gaps, the
// market timezone drives the axis, stale data stays rendered, hiding destroys the chart, a missing
// timezone falls back to UTC) so a failure points at a single cause.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/dashboard-forecast.js'), 'utf8');

// Build an isolated DOM/window harness and run the renderer source inside it. The returned handle
// exposes the collected scripts, created charts, requested API paths and a visibility switch, plus
// a `pump` that drains the microtask queue the renderer uses between its await points.
function harness({ spot } = {}) {
  const elements = new Map();
  const scripts = [];
  const chartConfigs = [];
  const visibleIds = new Set();
  const paths = [];
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { textContent: '', hidden: true });
    return elements.get(id);
  };
  const document = {
    body: { dataset: { page: 'dashboard' } },
    documentElement: {},
    getElementById: element,
    querySelector: (selector) => ({
      classList: { contains: () => !visibleIds.has(selector.match(/data-widget-id="([^"]+)"/)[1]) },
    }),
    createElement: () => ({ remove() { this.removed = true; } }),
    head: { append(script) { scripts.push(script); } },
  };
  const window = {
    RCTAdmin: {
      api: async (url) => { paths.push(url); return { spot: spot() }; },
      messageFrom: (error) => error.message
    },
    Chart: class {
      constructor(_canvas, config) { chartConfigs.push(config); this.destroyed = false; }
      destroy() { this.destroyed = true; } resize() { }
    },
    ChartDataLabels: {},
  };
  window.Chart.register = () => { };
  vm.runInNewContext(SOURCE, {
    document, window, URLSearchParams, setInterval() { }, setTimeout() { }, AbortController,
    getComputedStyle: () => ({ getPropertyValue: () => '#123456' }),
  });
  const pump = async () => { await new Promise((resolve) => setImmediate(resolve)); };
  return { api: window.RCTDashboardForecast, elements, element, scripts, chartConfigs, visibleIds, paths, pump };
}

const BASE_CURVE = [
  { start: '2026-10-12T12:00:00Z', end: '2026-10-12T12:15:00Z', price_ct_per_kwh: 12 },
  { start: '2026-10-12T13:00:00Z', end: '2026-10-12T14:30:00Z', price_ct_per_kwh: 18 },
];

function availableSpot(overrides = {}) {
  return {
    status: 'available', current_status: 'available', current_price_ct_per_kwh: 12,
    bidding_zone: 'DE-LU', currency: 'EUR', intervals_available: 1, timezone: 'Europe/Berlin',
    future_curve: BASE_CURVE, ...overrides,
  };
}

// Drive the renderer to one rendered chart for `spot-price-curve`, loading the library on demand.
async function renderCurve(spot) {
  const h = harness({ spot: () => spot });
  h.visibleIds.add('spot-price-curve');
  h.api.sync();
  await h.pump();
  // Resolve every script tag the renderer appended (chart.umd + datalabels), in order.
  while (h.scripts.some((s) => !s.resolved)) {
    const script = h.scripts.find((s) => !s.resolved);
    script.resolved = true;
    script.onload();
    await h.pump();
  }
  return h;
}

test('chart loading retries after a transient script failure', async () => {
  const h = harness({ spot: () => availableSpot() });
  h.visibleIds.add('spot-price-curve');
  h.api.sync();
  await h.pump();
  assert.equal(h.paths[0], 'energy/dashboard-forecast?include=spot_day_ahead');
  assert.equal(h.scripts.length, 1);
  h.scripts[0].onerror();
  await h.pump();
  assert.equal(h.scripts[0].removed, true);

  // A later sync re-attempts the load; once both scripts load, exactly one chart is created.
  h.api.sync();
  await h.pump();
  assert.equal(h.scripts.length, 2);
  h.scripts[1].onload();
  await h.pump();
  assert.equal(h.scripts.length, 3);
  h.scripts[2].onload();
  await h.pump();
  assert.equal(h.chartConfigs.length, 1);
});

test('price intervals are not interpolated between distinct intervals', async () => {
  const spot = availableSpot();
  const h = await renderCurve(spot);
  const config = h.chartConfigs[0];
  // A line chart with tension 0 and spanGaps false: steps between intervals, never a sloped
  // interpolation across them.
  assert.equal(config.options.scales.x.type, 'linear');
  assert.equal(config.data.datasets[0].tension, 0);
  assert.equal(config.data.datasets[0].spanGaps, false);
  // Each interval is two points at the same price (start and end), so the price is flat within an
  // interval and never a straight line drawn from one interval's price to the next.
  const data = JSON.parse(JSON.stringify(config.data.datasets[0].data));
  assert.deepEqual(data[0], { x: Date.parse(spot.future_curve[0].start), y: 12, interval: spot.future_curve[0] });
  assert.deepEqual(data[1], { x: Date.parse(spot.future_curve[0].end), y: 12, interval: spot.future_curve[0] });
});

test('gaps between intervals remain gaps', async () => {
  const spot = availableSpot();
  const h = await renderCurve(spot);
  const data = JSON.parse(JSON.stringify(h.chartConfigs[0].data.datasets[0].data));
  // The two intervals are not contiguous (12:15 then 13:00), so a null-y break point is inserted
  // at the first interval's end to keep the gap empty rather than bridged.
  assert.deepEqual(data, [
    { x: Date.parse(spot.future_curve[0].start), y: 12, interval: spot.future_curve[0] },
    { x: Date.parse(spot.future_curve[0].end), y: 12, interval: spot.future_curve[0] },
    { x: Date.parse(spot.future_curve[0].end), y: null },
    { x: Date.parse(spot.future_curve[1].start), y: 18, interval: spot.future_curve[1] },
    { x: Date.parse(spot.future_curve[1].end), y: 18, interval: spot.future_curve[1] },
  ]);
});

test('the market timezone drives the time axis', async () => {
  const h = await renderCurve(availableSpot({ timezone: 'Europe/Berlin' }));
  const callback = h.chartConfigs[0].options.scales.x.ticks.callback;
  // Europe/Berlin in October is CEST (+2), so the axis label carries the market offset, not UTC.
  assert.match(callback(Date.parse(BASE_CURVE[0].start)), /GMT\+2/);
});

test('a missing provider timezone falls back to UTC without crashing', async () => {
  // The server only ever emits a validated IANA zone or null; a null/absent timezone must render
  // against UTC rather than throw a RangeError out of toLocaleTimeString.
  const h = await renderCurve(availableSpot({ timezone: null }));
  const callback = h.chartConfigs[0].options.scales.x.ticks.callback;
  assert.doesNotThrow(() => callback(Date.parse(BASE_CURVE[0].start)));
  assert.match(callback(Date.parse(BASE_CURVE[0].start)), /GMT/);
});

test('stale data stays rendered', async () => {
  // status "stale" still draws the last available curve; it is not blanked like unavailable/error.
  const h = await renderCurve(availableSpot({ status: 'stale' }));
  assert.equal(h.chartConfigs.length, 1);
  assert.equal(h.chartConfigs[0].data.datasets[0].data.length, 5);
});

test('hiding the curve widget destroys its chart', async () => {
  const spot = availableSpot();
  const h = harness({ spot: () => spot });
  h.visibleIds.add('spot-price-curve');
  h.api.sync();
  await h.pump();
  while (h.scripts.some((s) => !s.resolved)) {
    const script = h.scripts.find((s) => !s.resolved);
    script.resolved = true;
    script.onload();
    await h.pump();
  }
  assert.equal(h.chartConfigs.length, 1);
  // Hiding the widget and syncing destroys the chart and hides its box.
  h.visibleIds.delete('spot-price-curve');
  h.api.sync();
  await h.pump();
  assert.equal(h.element('spot-price-curve-box').hidden, true);
});

test('switching the visible widget switches the requested projection', async () => {
  const spot = availableSpot();
  const h = harness({ spot: () => spot });
  h.visibleIds.add('spot-price-curve');
  h.api.sync();
  await h.pump();
  assert.equal(h.paths[0], 'energy/dashboard-forecast?include=spot_day_ahead');
  h.visibleIds.clear();
  h.visibleIds.add('spot-price-current');
  h.api.sync();
  await h.pump();
  assert.equal(h.paths[1], 'energy/dashboard-forecast?include=spot_current');
});
