//
// tests/js/admin-spot-market.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused tests for the Spot Market tab's preview rendering and config fields. Run with Node's
// built-in runner (no npm/bundler): `node --test tests/js/*.test.js`. admin-spot-market.js is a
// plain script that relies on globals ($, element, api, toast, messageFrom, providerLabel,
// getComputedStyle); the harness supplies minimal stubs and a tiny DOM, runs the source in a VM
// context, and exposes its top-level functions. These tests pin the restored regression layout:
// the Lowest/Highest tiles always render (showing real numbers whenever yesterday/today data
// exists, independent of tomorrow, and "n/a" when no figure is available), Currency and Source unit
// stay in the preview context rather than the form, and the tomorrow-unavailable note is a warning.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-spot-market.js'), 'utf8');

// A minimal DOM node: children, classList, dataset, attributes and the handful of methods the
// source touches. querySelector/querySelectorAll walk the subtree by class / [data-field].
function makeNode(tag, byId) {
  let _id = '';
  const node = {
    tagName: tag, children: [], parentElement: null, attributes: {}, dataset: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    _text: '', hidden: false, value: '', type: '', readOnly: false, checked: false, disabled: false,
    set id(v) { _id = v; if (byId) byId.set(v, node); }, get id() { return _id; },
    set className(v) { this._class = v; }, get className() { return this._class || ''; },
    set textContent(v) { this._text = String(v); this.children = []; }, get textContent() { return this._text; },
    set htmlFor(v) { this._htmlFor = v; }, get htmlFor() { return this._htmlFor || ''; },
    append(...kids) { for (const k of kids) { k.parentElement = node; node.children.push(k); } },
    replaceChildren(...kids) { node.children = []; node.append(...kids); },
    setAttribute(k, v) { node.attributes[k] = String(v); }, getAttribute(k) { return node.attributes[k] ?? null; },
    removeAttribute(k) { delete node.attributes[k]; },
    hasAttribute(k) { return k in node.attributes; },
    addEventListener(type, handler) { (node._listeners ||= {})[type] = handler; },
    querySelectorAll(selector) {
      const field = selector.match(/^\[data-field="([^"]+)"\]$/);
      const tags = new Set(selector.split(',').map((part) => part.trim().toLowerCase()));
      const found = [];
      const visit = (current) => {
        for (const child of current.children || []) {
          if (field ? child.dataset.field === field[1] : tags.has(child.tagName.toLowerCase())) found.push(child);
          visit(child);
        }
      };
      visit(node);
      return found;
    },
    querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
    contains(other) { return other === node || node.children.some((c) => c.contains && c.contains(other)); },
    get firstChild() { return node.children[0] || null; },
  };
  return node;
}

// Collect every node in a subtree (including the roots) for assertions.
function flatten(nodes) {
  const out = [];
  const walk = (n) => { out.push(n); for (const c of n.children || []) walk(c); };
  for (const n of nodes) walk(n);
  return out;
}

const hasClass = (n, cls) => (n.className || '').split(/\s+/).includes(cls);

function harness() {
  const byId = new Map();
  const chartConfigs = [];
  makeNode('div', byId).id = 'spot-market-preview-status';
  const document = {
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => makeNode(tag, byId),
  };
  const context = {
    document,
    $: (id) => byId.get(id) || null,
    element: (tag, className, value) => {
      const n = makeNode(tag, byId);
      if (className) n.className = className;
      if (value !== undefined) n.textContent = String(value);
      return n;
    },
    api: async () => ({}),
    toast: () => { },
    messageFrom: (e) => e.message,
    providerLabel: (code) => code,
    getComputedStyle: () => ({ getPropertyValue: () => '#123456' }),
    lockFormControls: (targets) => {
      const locked = [];
      for (const control of targets.flatMap((target) => target?.querySelectorAll('input, select, button, textarea') || [])) {
        if (!control.disabled) { control.disabled = true; locked.push(control); }
      }
      return locked;
    },
    unlockFormControls: (locked) => { for (const control of locked) control.disabled = false; },
    Chart: class {
      constructor(_canvas, config) { chartConfigs.push(config); this.options = config.options; }
      destroy() { } update() { }
    },
    AbortController, setTimeout, clearTimeout, setInterval, clearInterval,
    Number, Array, Object, Boolean, String, Math, Date, Set, Intl,
  };
  context.window = context;
  vm.runInNewContext(SOURCE, context);
  return { context, byId, chartConfigs };
}

// A connected preview result. Overrides tailor each case (tomorrow present/absent, no figures).
function previewResult(overrides = {}) {
  return {
    status: 'connected',
    provider: 'energy_charts',
    bidding_zone: 'DE-LU',
    currency: 'EUR',
    last_update: '2026-10-12T10:00:00Z',
    min_price_eur_per_kwh: 0.05,
    max_price_eur_per_kwh: 0.30,
    min_price_at: '2026-10-12T03:00:00Z',
    max_price_at: '2026-10-12T18:00:00Z',
    yesterday_intervals: [{ start: '2026-10-11T00:00:00Z', end: '2026-10-11T00:15:00Z', price_eur_per_kwh: 0.10 }],
    today_intervals: [{ start: '2026-10-12T00:00:00Z', end: '2026-10-12T00:15:00Z', price_eur_per_kwh: 0.12 }],
    next_day_intervals: [{ start: '2026-10-13T00:00:00Z', end: '2026-10-13T00:15:00Z', price_eur_per_kwh: 0.15 }],
    yesterday_message: null,
    next_day_message: null,
    timezone: 'Europe/Berlin',
    ...overrides,
  };
}

// Locate the Lowest/Highest tiles grid and read the two value cells plus each tile's leading icon.
function highlights(nodes) {
  const grid = flatten(nodes).find((n) => hasClass(n, 'spot-market-highlights'));
  if (!grid) return null;
  const tiles = (grid.children || []).filter((n) => hasClass(n, 'spot-market-highlight'));
  const values = flatten([grid]).filter((n) => hasClass(n, 'spot-market-highlight-value')).map((n) => n.textContent);
  // Each tile's first child must be the Material Icons KPI glyph.
  const icons = tiles.map((tile) => tile.children[0]);
  return { grid, tiles, values, icons };
}

test('the Lowest/Highest tiles render when tomorrow is published', () => {
  const h = harness();
  const nodes = h.context.renderSpotMarketPreview(previewResult());
  const tiles = highlights(nodes);
  assert.ok(tiles, 'highlights grid is present');
  assert.equal(tiles.values.length, 2);
  assert.ok(tiles.values[0].includes('ct/kWh'));
  assert.ok(tiles.values[1].includes('ct/kWh'));
  // Each tile leads with a decorative Material Icons KPI glyph.
  assert.equal(tiles.icons.length, 2);
  for (const icon of tiles.icons) {
    assert.ok(icon && hasClass(icon, 'material-icons'), 'tile leads with a material-icons glyph');
    assert.equal(icon.getAttribute('aria-hidden'), 'true', 'tile icon is decorative');
    assert.ok(icon.textContent.length > 0, 'tile icon names a glyph');
  }
});

test('the tiles still render with real numbers when tomorrow is not yet published', () => {
  // The regression: tomorrow unpublished (empty next_day, a next_day_message set) but yesterday and
  // today exist, so the backend still provides min/max. The tiles must stay visible with figures.
  const h = harness();
  const nodes = h.context.renderSpotMarketPreview(previewResult({
    next_day_intervals: [],
    next_day_message: 'Energy-Charts next-day prices are not published yet',
  }));
  const tiles = highlights(nodes);
  assert.ok(tiles, 'highlights grid is present even without tomorrow');
  assert.ok(tiles.values[0].includes('ct/kWh'), 'Lowest shows a figure, not n/a');
  assert.ok(tiles.values[1].includes('ct/kWh'), 'Highest shows a figure, not n/a');
});

test('the tiles read n/a when no price figure is available', () => {
  const h = harness();
  const nodes = h.context.renderSpotMarketPreview(previewResult({
    min_price_eur_per_kwh: null,
    max_price_eur_per_kwh: null,
    min_price_at: null,
    max_price_at: null,
  }));
  const tiles = highlights(nodes);
  assert.ok(tiles, 'highlights grid is still present with no figures');
  assert.deepEqual(tiles.values, ['n/a', 'n/a']);
  // The leading icon stays even when the figure falls back to n/a.
  for (const icon of tiles.icons) {
    assert.ok(icon && hasClass(icon, 'material-icons'), 'tile keeps its icon with the n/a fallback');
  }
});

test('spotMarketFields leaves currency and source unit in the preview context only', () => {
  const h = harness();
  const fields = h.context.spotMarketFields(
    { enabled: true, provider: 'energy_charts', bidding_zone: 'DE-LU', currency: 'EUR', price_unit: 'EUR/MWh' },
    { zones: ['DE-LU'] },
  );
  assert.deepEqual(Array.from(fields, (field) => field.key), ['enabled', 'provider', 'bidding_zone']);
});

test('saving Spot Market configuration locks its fields until the request completes', async () => {
  const h = harness();
  const fields = h.context.element('div', '');
  fields.id = 'spot-market-fields';
  const provider = h.context.element('select', '');
  const enabled = h.context.element('input', '');
  fields.append(provider, enabled);
  const save = h.context.element('button', '');
  save.id = 'spot-market-save';
  const state = h.context.element('span', '');
  state.id = 'spot-market-save-state';
  h.context.spotMarketBody = () => ({});
  h.context.renderSpotMarket = () => { };
  h.context.scheduleSpotMarketPreview = () => { };
  let resolveRequest;
  h.context.api = () => new Promise((resolve) => { resolveRequest = resolve; });

  const saving = h.context.saveSpotMarket();
  assert.equal(provider.disabled, true);
  assert.equal(enabled.disabled, true);
  assert.equal(save.disabled, true);
  resolveRequest({});
  await saving;
  assert.equal(provider.disabled, false);
  assert.equal(enabled.disabled, false);
  assert.equal(save.disabled, false);
});

test('late bidding-zone metadata keeps fields locked through an in-flight save', async () => {
  const h = harness();
  const config = { enabled: true, provider: 'energy_charts', bidding_zone: 'DE-LU', fee_region: { code: '', label: '' } };
  const form = h.context.element('form', '');
  form.id = 'spot-market-form';
  const fields = h.context.element('div', '');
  fields.id = 'spot-market-fields';
  const save = h.context.element('button', '');
  save.id = 'spot-market-save';
  const state = h.context.element('span', '');
  state.id = 'spot-market-save-state';
  h.context.setInterval = () => 0;
  h.context.scheduleSpotMarketPreview = () => { };
  let resolveZones;
  let resolveSave;
  h.context.api = (path, options) => {
    if (path === 'energy/spot-market/bidding-zones') return new Promise((resolve) => { resolveZones = resolve; });
    if (options?.method === 'PUT') return new Promise((resolve) => { resolveSave = resolve; });
    return Promise.resolve(config);
  };

  await h.context.initSpotMarket();
  const beforeSave = fields.querySelectorAll('input, select');
  const saving = h.context.saveSpotMarket();
  assert.ok(beforeSave.every((control) => control.disabled));
  resolveZones({ zones: ['DE-LU', 'AT'] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(beforeSave.every((control) => control.disabled));
  assert.ok(beforeSave.every((control) => fields.contains(control)), 'the locked controls remain in the form');
  resolveSave(config);
  await saving;
  assert.ok(fields.querySelectorAll('input, select').every((control) => !control.disabled));
  const zone = fields.querySelector('[data-field="bidding_zone"]');
  assert.ok(zone.children.some((option) => option.value === 'AT'));
});

test('an unavailable preview retries promptly and backs off', async () => {
  const h = harness();
  const preview = h.context.element('div', '');
  preview.id = 'spot-market-preview';
  const delays = [];
  h.context.setTimeout = (_run, ms) => { delays.push(ms); return delays.length; };
  h.context.clearTimeout = () => { };
  h.context.spotMarketBody = () => ({});
  h.context.api = async () => ({ status: 'unavailable', provider: 'energy_charts', bidding_zone: 'DE-LU', currency: 'EUR' });

  await h.context.previewSpotMarket();
  h.context.scheduleSpotMarketPreview({ immediate: true, resetRetry: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(delays, [5000, 10000]);
  h.context.scheduleSpotMarketPreview({ immediate: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(delays.at(-1), 5000, 'a user action resets the retry delay');
});

test('the tomorrow-unavailable note is a warning callout with a triangle icon', () => {
  const h = harness();
  const nodes = h.context.renderSpotMarketPreview(previewResult({
    next_day_intervals: [],
    next_day_message: 'Energy-Charts next-day prices are not published yet',
  }));
  const all = flatten(nodes);
  const notice = all.find((n) => hasClass(n, 'spot-market-notice-warning'));
  assert.ok(notice, 'warning callout is emitted');
  assert.ok(!hasClass(notice, 'text-secondary'), 'no longer the plain muted paragraph');
  const icon = flatten([notice]).find((n) => (n.textContent === 'warning') && hasClass(n, 'material-icons'));
  assert.ok(icon, 'material-icons warning triangle glyph is present');
  assert.equal(icon.getAttribute('aria-hidden'), 'true', 'icon is decorative');
  const textNode = flatten([notice]).find((n) => n.textContent.includes('Tomorrow'));
  assert.ok(textNode, 'the hint text node is present');
  assert.ok(textNode.textContent.startsWith("Tomorrow's prices are not available yet: "));
  assert.ok(textNode.textContent.includes('Energy-Charts next-day prices are not published yet'));
});

test('a stale result shows the badge and a calm notice with the technical error only as a tooltip', () => {
  const h = harness();
  const nodes = h.context.renderSpotMarketPreview(previewResult({
    stale: true,
    refresh_error: 'provider down (HTTP 503)',
    refresh_error_at: '2026-01-02T10:30:00Z',
  }));
  const all = flatten(nodes);
  assert.ok(!all.some((n) => hasClass(n, 'status-badge')));
  const badge = h.byId.get('spot-market-preview-status').children[0];
  assert.ok(hasClass(badge, 'status-badge-warning'));
  assert.equal(badge.textContent, 'Stale');
  const notice = all.find((n) => hasClass(n, 'spot-market-notice-warning'));
  assert.ok(notice, 'the notice is emitted for really outdated data');
  assert.equal(notice.title, 'provider down (HTTP 503)');
  assert.ok(!flatten([notice]).some((n) => /503|attempts|HTTP/.test(n.textContent)), 'no technical text in the body');
});

test('a failed refresh behind data that still covers the present stays calm', () => {
  const h = harness();
  const nodes = h.context.renderSpotMarketPreview(previewResult({
    stale: false,
    refresh_error: 'provider down (HTTP 503)',
    refresh_error_at: '2026-01-02T10:30:00Z',
  }));
  const badge = h.byId.get('spot-market-preview-status').children[0];
  assert.ok(hasClass(badge, 'status-badge-success'));
  assert.equal(badge.textContent, 'Connected');
  assert.ok(!flatten(nodes).some((n) => hasClass(n, 'spot-market-notice-warning') && n.title));
});

test('an unchanged status keeps the same badge node, a changed one replaces it, null clears it', () => {
  const h = harness();
  const slot = h.byId.get('spot-market-preview-status');
  h.context.setPreviewStatus('spot-market-preview-status', 'status-badge-success', 'Connected');
  const first = slot.children[0];
  h.context.setPreviewStatus('spot-market-preview-status', 'status-badge-success', 'Connected');
  assert.equal(slot.children[0], first, 'no rebuild for an unchanged status');
  h.context.setPreviewStatus('spot-market-preview-status', 'status-badge-danger', 'Error');
  assert.notEqual(slot.children[0], first);
  assert.equal(slot.children[0].textContent, 'Error');
  h.context.setPreviewStatus('spot-market-preview-status', null);
  assert.equal(slot.children.length, 0);
});

// 96 quarter-hour HH:MM labels, matching drawSpotMarketChart's own axis construction.
function quarterHourIntervals(dateIso, prices) {
  return prices.map((price, index) => {
    const hh = String(Math.floor(index / 4)).padStart(2, '0');
    const mm = String((index % 4) * 15).padStart(2, '0');
    const start = `${dateIso}T${hh}:${mm}:00Z`;
    const endIndex = index + 1;
    const eh = String(Math.floor(endIndex / 4)).padStart(2, '0');
    const em = String((endIndex % 4) * 15).padStart(2, '0');
    const end = `${dateIso}T${eh === '24' ? '23' : eh}:${eh === '24' ? '59' : em}:00Z`;
    return { start, end, price_eur_per_kwh: price };
  });
}

test('both Yesterday and Today datasets are drawn when tomorrow is unpublished', () => {
  const h = harness();
  // Pre-register the chart canvas and the preview container drawSpotMarketChart looks up by id.
  const preview = makeNode('div', h.byId); preview.id = 'spot-market-preview';
  const canvas = makeNode('canvas', h.byId); canvas.id = 'spot-market-chart';
  canvas.parentElement = makeNode('div', h.byId);

  const result = previewResult({
    timezone: 'UTC',
    yesterday_intervals: quarterHourIntervals('2026-01-01', [0.10, 0.11, 0.12]),
    today_intervals: quarterHourIntervals('2026-01-02', [0.20, 0.08, 0.25]),
    next_day_intervals: [],
    next_day_message: 'not published yet',
  });
  h.context.drawSpotMarketChart(result);

  assert.equal(h.chartConfigs.length, 1, 'a chart is drawn (series not invalid/empty)');
  const datasets = h.chartConfigs[0].data.datasets;
  const byLabel = Object.fromEntries(datasets.map((d) => [d.label, d]));
  assert.ok(byLabel.Yesterday, 'a Yesterday dataset exists');
  assert.ok(byLabel.Today, 'a Today dataset exists');
  assert.ok(byLabel.Tomorrow, 'a Tomorrow dataset exists (all-null when unpublished)');
  // Yesterday and Today each carry real points; Tomorrow is all null.
  assert.ok(byLabel.Yesterday.data.some((v) => v !== null), 'Yesterday has points');
  assert.ok(byLabel.Today.data.some((v) => v !== null), 'Today has points');
  assert.ok(byLabel.Tomorrow.data.every((v) => v === null), 'Tomorrow is empty');
  // The legend stays visible so all available series are labelled.
  assert.equal(h.chartConfigs[0].options.plugins.legend.display, true);
  assert.equal(h.chartConfigs[0].options.interaction.mode, 'index');
  assert.equal(h.chartConfigs[0].options.interaction.intersect, false);
  assert.equal(h.chartConfigs[0].options.plugins.tooltip.filter({ parsed: { y: Number.NaN } }), false);
});

test('a DST fall-back day keeps both duplicated-wall-clock intervals instead of overwriting one', () => {
  const h = harness();
  const preview = makeNode('div', h.byId); preview.id = 'spot-market-preview';
  const canvas = makeNode('canvas', h.byId); canvas.id = 'spot-market-chart';
  canvas.parentElement = makeNode('div', h.byId);

  // 2026-10-25 is the European DST fall-back day. Local Europe/Berlin 02:00 occurs twice: once at
  // 00:00Z (CEST, +02:00) and again at 01:00Z (CET, +01:00). Both format to the same '02:00'
  // wall-clock key, which previously collided to a single chart slot. The two prices are distinct
  // so a dropped point is observable.
  const today = [
    { start: '2026-10-25T00:00:00Z', end: '2026-10-25T01:00:00Z', price_eur_per_kwh: 0.11 },
    { start: '2026-10-25T01:00:00Z', end: '2026-10-25T02:00:00Z', price_eur_per_kwh: 0.22 },
  ];
  const result = previewResult({
    timezone: 'Europe/Berlin',
    yesterday_intervals: [],
    today_intervals: today,
    next_day_intervals: [],
  });
  h.context.drawSpotMarketChart(result);

  assert.equal(h.chartConfigs.length, 1, 'a chart is drawn (series not invalid/empty)');
  const byLabel = Object.fromEntries(h.chartConfigs[0].data.datasets.map((d) => [d.label, d]));
  const todayPoints = byLabel.Today.data.filter((v) => v !== null);
  assert.equal(todayPoints.length, 2, 'both duplicated-hour intervals are kept, neither overwritten');
  assert.deepEqual(todayPoints, [11, 22], 'both distinct prices survive in input order (ct/kWh)');
  // Both carry their own 02:00 wall-clock label for the tooltip.
  assert.deepEqual(byLabel.Today.times.slice(0, 2), ['02:00', '02:00']);
  assert.equal(h.chartConfigs[0].options.plugins.legend.display, true);
});
