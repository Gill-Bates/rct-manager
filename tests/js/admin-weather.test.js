//
// tests/js/admin-weather.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused tests for the Weather tab's location autocomplete and kWp handling. Run with Node's
// built-in runner (no npm/bundler): `node --test tests/js/*.test.js`. admin-weather.js is a
// plain script that relies on globals from admin-core.js ($, element, api, toast, messageFrom,
// scheduleWeatherPreview); the harness supplies minimal stubs and a tiny
// DOM, runs the source in a VM context, and exposes its top-level functions so each behaviour
// (debounce, abort/stale, keyboard navigation, selection, timezone handling, kWp conversion) is
// exercised against the real source.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-weather.js'), 'utf8');
const AXIS_SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-chart-axis.js'), 'utf8');

// A minimal DOM node: children, classList, dataset, attributes and the handful of methods the
// source touches. querySelector/querySelectorAll walk the subtree by [data-field] / [role].
// Setting .id registers the node in the shared ``byId`` map so getElementById/$ find it, mirroring
// how a real document resolves ids anywhere in the tree.
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
    append(...kids) { for (const k of kids) { k.parentElement = node; node.children.push(k); } },
    prepend(...kids) { for (const k of kids) k.parentElement = node; node.children.unshift(...kids); },
    replaceChildren(...kids) { node.children = []; node.append(...kids); },
    setAttribute(k, v) { node.attributes[k] = String(v); }, getAttribute(k) { return node.attributes[k] ?? null; },
    removeAttribute(k) { delete node.attributes[k]; },
    hasAttribute(k) { return k in node.attributes; },
    addEventListener(type, handler) { (node._listeners ||= {})[type] = handler; },
    contains(other) { return other === node || node.children.some((c) => c.contains && c.contains(other)); },
    scrollIntoView() { },
    get firstChild() { return node.children[0] || null; },
  };
  const descendants = () => {
    const out = [];
    const walk = (n) => { for (const c of n.children) { out.push(c); walk(c); } };
    walk(node);
    return out;
  };
  node.querySelector = (sel) => node.querySelectorAll(sel)[0] || null;
  node.querySelectorAll = (sel) => {
    const field = sel.match(/\[data-field="([^"]+)"\]/);
    if (field) return descendants().filter((n) => n.dataset.field === field[1]);
    if (sel === '[data-field]') return descendants().filter((n) => n.dataset.field != null);
    if (sel.includes('role="option"')) return descendants().filter((n) => n.attributes.role === 'option');
    const byId = sel.match(/^#(.+)$/);
    if (byId) return descendants().filter((n) => n.id === byId[1]);
    if (sel.includes('invalid-feedback')) return descendants().filter((n) => n.className.includes('invalid-feedback'));
    return [];
  };
  return node;
}

// The timezone is shown in the preview's context row; render it fresh and read it ('—' when absent).
function previewTimezone(h) {
  h.byId.delete('weather-timezone');
  h.context.renderWeatherPreview({ status: 'disabled' });
  return h.byId.get('weather-timezone')?.textContent ?? '—';
}

function harness({ apiImpl } = {}) {
  const byId = new Map();
  const ensure = (id) => { if (!byId.has(id)) { const n = makeNode('div', byId); n.id = id; } return byId.get(id); };
  const fieldsContainer = ensure('weather-fields');
  const siteSummaryContainer = ensure('weather-site-summary');
  const providerFieldsContainer = ensure('weather-provider-fields');
  const pvFieldsContainer = ensure('weather-pv-fields');
  const paths = [];
  const document = {
    _docListeners: {},
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => makeNode(tag, byId),
    createDocumentFragment: () => makeNode('fragment', byId),
    addEventListener(type, handler) { this._docListeners[type] = handler; },
  };
  const context = {
    document,
    $: (id) => byId.get(id) || null,
    element: (tag, className, value) => { const n = makeNode(tag, byId); if (className) n.className = className; if (value !== undefined) n.textContent = String(value); return n; },
    api: apiImpl || (async (p) => { paths.push(p); return { status: 'ok', results: [] }; }),
    toast: () => { },
    messageFrom: (e) => e.message,
    spotMarketLoadingState: () => makeNode('div'),
    // The shared title-row badge helper lives in admin-spot-market.js; record what it is asked to show.
    setPreviewStatus: (slotId, badgeClass, text) => { context.__status = badgeClass ? { slotId, badgeClass, text } : null; },
    scheduleWeatherPreview: () => { context.__previewScheduled = (context.__previewScheduled || 0) + 1; },
    // admin-core.js provides these shared helpers; mirror their behaviour so saveWeather()'s
    // whole-form locking is exercised against the real source. lockFormControls disables every
    // [data-field] control in the given containers (plus any bare control node) and returns exactly
    // those it disabled; unlockFormControls re-enables them.
    lockFormControls: (targets) => {
      const controls = new Set();
      for (const target of targets || []) {
        if (!target) continue;
        if ('disabled' in target) controls.add(target);
        for (const node of target.querySelectorAll?.('[data-field]') || []) controls.add(node);
      }
      const locked = [];
      for (const control of controls) { if (!control.disabled) { control.disabled = true; locked.push(control); } }
      return locked;
    },
    unlockFormControls: (locked) => { for (const control of locked || []) control.disabled = false; },
    getComputedStyle: () => ({ getPropertyValue: () => '#123456' }),
    AbortController, setTimeout, clearTimeout, Number, Array, Object, Boolean, String, Math, Date,
    HTMLInputElement: class { },
    __previewScheduled: 0,
  };
  context.window = context;
  vm.runInNewContext(AXIS_SOURCE, context);
  vm.runInNewContext(SOURCE, context);
  return { context, byId, ensure, fieldsContainer, siteSummaryContainer, providerFieldsContainer, pvFieldsContainer, paths };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const HIT = { name: 'Remscheid', admin1: 'North Rhine-Westphalia', country: 'Germany', latitude: 51.1798, longitude: 7.1925, timezone: 'Europe/Berlin' };

// Build the rendered weather form via renderWeather so the field controls and the location section
// exist, then return the live search input and results container.
function renderForm(h, config = {}) {
  const base = { enabled: false, provider: 'open_meteo', provider_meta: { attribution: 'Weather data by Open-Meteo.com (CC BY 4.0)' }, ...config };
  h.context.renderWeather(base);
  return { search: h.byId.get('weather-location-search'), results: h.byId.get('weather-location-results') };
}

test('the search input carries combobox semantics', () => {
  const h = harness();
  const { search } = renderForm(h);
  assert.equal(search.getAttribute('role'), 'combobox');
  assert.equal(search.getAttribute('aria-autocomplete'), 'list');
  assert.equal(search.getAttribute('aria-controls'), 'weather-location-results');
  assert.equal(search.getAttribute('aria-expanded'), 'false');
});

test('a too-short query makes no request and keeps the dropdown closed', async () => {
  const h = harness();
  const { search, results } = renderForm(h);
  search.value = 'R';
  search._listeners.input();
  await flush();
  assert.equal(h.paths.length, 0);
  assert.equal(results.hidden, true);
});

test('a stale location response cannot reopen the dropdown after the input is cleared', async () => {
  // Request A is slow; the input is then cleared (which aborts + bumps generation). When A finally
  // resolves it must be discarded so the dropdown stays closed.
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness({ apiImpl: async (p) => { h2.paths.push(p); await gate; return { status: 'ok', results: [HIT] }; } });
  const h2 = h; // alias so the apiImpl closure can push paths
  h2.paths = h.paths;
  const { search, results } = renderForm(h);
  search.value = 'Remscheid';
  search._listeners.input();
  await flush();
  // Fire the debounced request, then clear the input before it resolves.
  await new Promise((r) => setTimeout(r, 320));
  search.value = '';
  search._listeners.input();
  await flush();
  release({ status: 'ok', results: [HIT] });
  await flush();
  assert.equal(results.hidden, true);
  assert.equal(results.children.length, 0);
});

test('arrow keys move the active option and Enter selects it', async () => {
  const h = harness({
    apiImpl: async (p) => (
      p.startsWith('energy/weather/timezone')
        ? { status: 'ok', timezone: 'Europe/Berlin' }
        : { status: 'ok', results: [HIT, { ...HIT, name: 'Remagen', latitude: 50.579, longitude: 7.227 }] }
    )
  });
  const { search, results } = renderForm(h);
  search.value = 'Rem';
  search._listeners.input();
  await flush();
  await new Promise((r) => setTimeout(r, 320));
  await flush();
  assert.equal(results.hidden, false);
  // ArrowDown highlights the first option and sets aria-activedescendant.
  search._listeners.keydown({ key: 'ArrowDown', preventDefault() { } });
  assert.equal(search.getAttribute('aria-activedescendant'), 'weather-location-option-0');
  search._listeners.keydown({ key: 'ArrowDown', preventDefault() { } });
  assert.equal(search.getAttribute('aria-activedescendant'), 'weather-location-option-1');
  // Enter selects the highlighted option, closes the dropdown and derives the zone from coordinates.
  search._listeners.keydown({ key: 'Enter', preventDefault() { } });
  await flush();
  assert.equal(results.hidden, true);
  assert.equal(h.fieldsContainer.querySelector('[data-field="latitude"]').value, '50.5790');
  assert.equal(previewTimezone(h), 'Europe/Berlin');
});

test('Escape closes the dropdown', async () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', results: [HIT] }) });
  const { search, results } = renderForm(h);
  search.value = 'Rem';
  search._listeners.input();
  await new Promise((r) => setTimeout(r, 320));
  await flush();
  assert.equal(results.hidden, false);
  search._listeners.keydown({ key: 'Escape', preventDefault() { } });
  assert.equal(results.hidden, true);
});

test('closing within the debounce window cancels the pending search so no late request reopens the list', async () => {
  // Type a query (schedules the 300 ms debounce timer), then close the dropdown before the timer
  // fires. The scheduled search must be cancelled: without clearing the timer it would fire later,
  // adopt the bumped generation and reopen the list.
  const h = harness({ apiImpl: async () => ({ status: 'ok', results: [HIT] }) });
  const { search, results } = renderForm(h);
  search.value = 'Remscheid';
  search._listeners.input();
  // Close well within WEATHER_LOCATION_DEBOUNCE_MS (300 ms), before the timer can fire.
  search._listeners.keydown({ key: 'Escape', preventDefault() { } });
  assert.equal(results.hidden, true);
  // Advance past the debounce window: a cancelled timer issues no request and leaves the list closed.
  await new Promise((r) => setTimeout(r, 360));
  await flush();
  assert.equal(h.paths.length, 0);
  assert.equal(results.hidden, true);
  assert.equal(results.children.length, 0);
});

test('selecting a hit fills coordinates, derives timezone and updates the selected-location label', async () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', timezone: 'Europe/Berlin' }) });
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation(HIT, search, results);
  await flush();
  assert.equal(h.fieldsContainer.querySelector('[data-field="latitude"]').value, '51.1798');
  assert.equal(h.fieldsContainer.querySelector('[data-field="longitude"]').value, '7.1925');
  assert.equal(previewTimezone(h), 'Europe/Berlin');
  assert.equal(h.byId.get('weather-selected-location').textContent, 'Remscheid');
  assert.equal(h.byId.get('weather-selected-region').textContent, 'North Rhine-Westphalia, Germany');
  assert.equal(h.context.weatherBody().location_region, 'North Rhine-Westphalia, Germany');
  assert.equal(search.value, 'Remscheid');
  assert.equal(results.hidden, true);
});

test('a provider-supplied timezone is ignored and coordinates are resolved server-side', async () => {
  const h = harness({
    apiImpl: async (p) => {
      h.paths.push(p);
      return { status: 'ok', timezone: 'Europe/Berlin' };
    }
  });
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation({ ...HIT, timezone: 'America/New_York' }, search, results);
  await flush();
  const call = h.paths.find((p) => p.startsWith('energy/weather/timezone'));
  assert.ok(call, 'the timezone endpoint was called even though the geocoder supplied a zone');
  assert.ok(call.includes('latitude=51.1798') && call.includes('longitude=7.1925'));
  assert.equal(previewTimezone(h), 'Europe/Berlin');
});

test('timezone is display-only and is never submitted', () => {
  const h = harness();
  renderForm(h, { timezone: 'Europe/Berlin' });
  assert.equal(h.fieldsContainer.querySelector('[data-field="timezone"]'), null);
  assert.equal(previewTimezone(h), 'Europe/Berlin');
  assert.equal(Object.hasOwn(h.context.weatherBody(), 'timezone'), false);
});

test('Weather fields render without documentation help text', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  for (const key of ['enabled', 'provider', 'latitude', 'longitude', 'pv_peak_power_kwp', 'tilt_deg', 'azimuth_deg', 'performance_ratio']) {
    const control = h.context.weatherControlByField(key);
    assert.equal(control.getAttribute('aria-describedby'), null);
    assert.ok(!control.parentElement.children.some((node) => node.className.includes('form-text')));
  }
});

test('weather source contains no manual Test forecast hook', () => {
  assert.ok(!SOURCE.includes('weather-test'));
});

test('selecting a hit without a provider zone derives the timezone from the coordinates', async () => {
  // Photon carries no zone; selecting the hit triggers a server-side derivation call, and the
  // resolved zone fills the read-only field.
  const h = harness({ apiImpl: async (p) => { h.paths.push(p); return { status: 'ok', timezone: 'Europe/Berlin' }; } });
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation({ ...HIT, timezone: undefined }, search, results);
  await flush();
  const call = h.paths.find((p) => p.startsWith('energy/weather/timezone'));
  assert.ok(call, 'the timezone endpoint was called');
  assert.ok(call.includes('latitude=51.1798') && call.includes('longitude=7.1925'), 'coordinates were passed');
  assert.equal(previewTimezone(h), 'Europe/Berlin');
});

test('a failed timezone derivation leaves the field blank rather than claiming a zone', async () => {
  const h = harness({ apiImpl: async (p) => { h.paths.push(p); return { status: 'error', message: 'Could not determine the timezone', timezone: null }; } });
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation({ ...HIT, timezone: undefined }, search, results);
  await flush();
  assert.equal(previewTimezone(h), '—');
});

test('provider renders in its own sibling tile container', () => {
  const h = harness();
  renderForm(h);
  assert.ok(h.providerFieldsContainer.querySelector('[data-field="provider"]'));
  assert.equal(h.fieldsContainer.querySelector('[data-field="provider"]'), null);
  assert.equal(h.pvFieldsContainer.querySelector('[data-field="provider"]'), null);
});

test('latitude and longitude render together in the compact coordinate grid', () => {
  const h = harness();
  renderForm(h);
  const grid = h.fieldsContainer.children.find((node) => node.className.includes('weather-coordinate-grid'));
  assert.ok(grid, 'coordinate grid exists');
  assert.ok(grid.querySelector('[data-field="latitude"]'));
  assert.ok(grid.querySelector('[data-field="longitude"]'));
});

test('selected location and timezone render in the preview summary', () => {
  const h = harness();
  renderForm(h, { location_name: 'Remscheid', location_region: 'Germany', timezone: 'Europe/Berlin' });
  assert.ok(h.siteSummaryContainer.querySelector('#weather-selected-location'));
  assert.equal(h.siteSummaryContainer.querySelector('#weather-timezone'), null, 'the timezone moved to the preview context row');
  assert.equal(h.fieldsContainer.querySelector('#weather-selected-location'), null);
  assert.equal(h.byId.get('weather-selected-location').textContent, 'Remscheid');
  assert.equal(h.byId.get('weather-selected-region').textContent, 'Germany');
  assert.equal(h.byId.get('weather-selected-region').hidden, false);
  const tile = h.siteSummaryContainer.children[0];
  assert.equal(tile.className, 'spot-market-highlight');
  assert.equal(tile.children[0].className, 'material-icons spot-market-highlight-icon text-info');
  assert.equal(tile.children[1].className, 'spot-market-highlight-caption');
  assert.equal(tile.children[2].className, 'spot-market-highlight-value');
  assert.equal(tile.children[3].className, 'spot-market-highlight-when');
});

test('the four PV System fields render in the separate PV tile container', () => {
  const h = harness();
  renderForm(h);
  for (const key of ['pv_peak_power_kwp', 'tilt_deg', 'azimuth_deg', 'performance_ratio']) {
    assert.ok(h.pvFieldsContainer.querySelector(`[data-field="${key}"]`), `${key} is in the PV tile`);
    assert.equal(h.fieldsContainer.querySelector(`[data-field="${key}"]`), null, `${key} is not in the site card`);
  }
});

test('the separate PV tile keeps field IDs, data-field and weatherBody serialization intact', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  // IDs and data-field survive the card wrapping, so the inline 422 mapping still resolves them.
  for (const key of ['pv_peak_power_kwp', 'tilt_deg', 'azimuth_deg', 'performance_ratio']) {
    const control = h.pvFieldsContainer.querySelector(`[data-field="${key}"]`);
    assert.equal(control.id, `weather-${key}`);
  }
  const body = h.context.weatherBody();
  // weatherBody() reads the kWp control from inside the card and still converts to Wp.
  assert.equal(body.pv_peak_power_wp, 10000);
  assert.equal(body.tilt_deg, 35);
  assert.equal(body.azimuth_deg, 0);
  assert.equal(body.performance_ratio, 0.85);
});

test('an inline 422 for a PV field still lands on the control inside the card', async () => {
  const error = Object.assign(new Error('bad'), {
    status: 422,
    detail: [{ type: 'greater_than', loc: ['tilt_deg'], msg: 'Input should be greater than 0' }],
  });
  const { h } = saveHarness(async () => { throw error; });
  renderForm(h, { enabled: true });
  await h.context.saveWeather();
  const control = h.pvFieldsContainer.querySelector('[data-field="tilt_deg"]');
  assert.ok(control.classList.contains('is-invalid'));
  assert.equal(h.byId.get('weather-tilt_deg-error').textContent, 'Input should be greater than 0');
});

test('the PV peak power field is kWp and the posted body converts to Wp', () => {
  const h = harness();
  renderForm(h, { pv_peak_power_wp: 10000 });
  const field = h.pvFieldsContainer.querySelector('[data-field="pv_peak_power_kwp"]');
  assert.ok(field, 'the kWp field exists');
  assert.equal(field.value, '10');  // 10000 Wp shown as 10 kWp
  // weatherBody reads the kWp control and multiplies back to Wp.
  h.fieldsContainer.querySelector('[data-field="enabled"]').checked = true;
  field.value = '7.5';
  const body = h.context.weatherBody();
  assert.equal(body.pv_peak_power_wp, 7500);
});

test('new input shows a Searching state and drops the stale hit list immediately', async () => {
  // Resolve the first query, then type a new query: before the debounce/request completes, the old
  // hit must be gone and a "Searching …" placeholder shown, so the stale list is not clickable.
  let answer = { status: 'ok', results: [HIT] };
  const h = harness({ apiImpl: async () => answer });
  const { search, results } = renderForm(h);
  search.value = 'Rem';
  search._listeners.input();
  await new Promise((r) => setTimeout(r, 320));
  await flush();
  assert.equal(results.children.length, 1);
  // New input: the old hit is cleared from the listbox and the pending state is announced in the
  // separate status live region (not as a fake option inside the listbox).
  answer = { status: 'ok', results: [{ ...HIT, name: 'Remagen' }] };
  search.value = 'Rema';
  search._listeners.input();
  assert.equal(results.children.length, 0);
  const status = h.byId.get('weather-location-status');
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, 'Searching …');
  assert.equal(status.getAttribute('role'), 'status');
  assert.equal(status.getAttribute('aria-live'), 'polite');
});

const US_HIT = { name: 'Titusville', admin1: 'Florida', admin2: 'Brevard County', district: 'North Brevard', country: 'United States', latitude: 28.6, longitude: -80.8, timezone: 'America/New_York', postal_code: '32781' };

async function runSearch(h, term) {
  const { search, results } = renderForm(h);
  search.value = term;
  search._listeners.input();
  await new Promise((r) => setTimeout(r, 320));
  await flush();
  return results;
}

test('a postcode query that matches the place shows exactly that code', async () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', results: [US_HIT] }) });
  const results = await runSearch(h, '32781');
  const option = results.children[0];
  // The matched code is shown, not the first code in the list.
  assert.equal(option.children[0].textContent, '32781 Titusville');
  assert.equal(option.children[1].textContent, 'Brevard County, North Brevard, Florida, United States');
});

test('a hit without a provider postcode prefixes none', async () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', results: [{ ...US_HIT, postal_code: null }] }) });
  const results = await runSearch(h, 'Titusville');
  assert.equal(results.children[0].children[0].textContent, 'Titusville');
});

test('a hit carrying a provider postcode prefixes exactly that code', async () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', results: [US_HIT] }) });
  const results = await runSearch(h, '32781');
  assert.equal(results.children[0].children[0].textContent, '32781 Titusville');
});

test('a no-match search announces it in the status region, not the listbox', async () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', results: [] }) });
  const results = await runSearch(h, '42857');
  assert.equal(results.children.length, 0);
  const status = h.byId.get('weather-location-status');
  assert.equal(status.textContent, 'No matching place found.');
  assert.equal(status.hidden, false);
});

test('a postcode hit labels as "PLZ City (District)" with region and country on a second line', () => {
  const h = harness();
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation({ name: 'Remscheid', city: 'Remscheid', district: 'Lüttringhausen', postal_code: '42899', admin1: 'North Rhine-Westphalia', country: 'Germany', latitude: 51.2, longitude: 7.2 }, search, results);
  assert.equal(h.byId.get('weather-selected-location').textContent, '42899 Remscheid (Lüttringhausen)');
  assert.equal(h.byId.get('weather-selected-region').textContent, 'North Rhine-Westphalia, Germany');
  assert.equal(h.context.weatherBody().location_name, '42899 Remscheid (Lüttringhausen)');
});

test('a saved config without location_region shows only the name line', () => {
  const h = harness();
  renderForm(h, { location_name: 'Remscheid, North Rhine-Westphalia, Germany' });
  assert.equal(h.byId.get('weather-selected-location').textContent, 'Remscheid, North Rhine-Westphalia, Germany');
  assert.equal(h.byId.get('weather-selected-region').hidden, true);
  assert.equal(h.context.weatherBody().location_region, null);
});

test('the Next 24 h tile renders beside the site tile, not in the preview container', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const nodes = h.context.renderWeatherPreview({ status: 'ok', connected: true, energy_next_24h_kwh: 12.34, covered_until: null, intervals: [] });
  assert.ok(h.byId.get('weather-next24').children.length > 0);
  assert.equal(h.byId.get('weather-next24').className, 'spot-market-highlight weather-next24');
  assert.equal(h.byId.get('weather-next24').children[1].className, 'spot-market-highlight-caption');
  assert.equal(h.byId.get('weather-next24').children[2].className, 'spot-market-highlight-value');
  assert.equal(h.byId.get('weather-next24').children[3].className, 'spot-market-highlight-when');
  assert.ok(!JSON.stringify(nodes.map((n) => n.className)).includes('spot-market-highlights'));
});

test('selecting a hit saves a label that includes admin2 for disambiguation', () => {
  const h = harness();
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation(US_HIT, search, results);
  assert.equal(h.byId.get('weather-selected-location').textContent, '32781 Titusville (Brevard County, North Brevard)');
  assert.equal(h.byId.get('weather-selected-region').textContent, 'Florida, United States');
});

test('provider attribution metadata is not rendered in the Weather form', () => {
  const h = harness();
  renderForm(h, { provider_meta: { attribution: 'Weather data by Open-Meteo.com (CC BY 4.0)', location_attribution: 'Location data from Photon © OpenStreetMap contributors' } });
  const texts = [...h.fieldsContainer.children].map((n) => n.textContent);
  assert.ok(!texts.some((t) => t.includes('OpenStreetMap')), 'the location attribution is not rendered');
  assert.ok(!texts.some((t) => t.includes('Open-Meteo.com')), 'the weather-data attribution is not rendered');
});

// ---------------------------------------------------------------------------
// Enable-toggle UX: a save that fails validation must surface inline per-field
// feedback through the is-invalid / invalid-feedback convention, never a single
// opaque toast; a complete config must save cleanly.
// ---------------------------------------------------------------------------

const COMPLETE_CONFIG = {
  enabled: true, provider: 'open_meteo',
  location_name: 'Remscheid, North Rhine-Westphalia, Germany',
  latitude: 51.1798, longitude: 7.1925, timezone: 'Europe/Berlin',
  pv_peak_power_wp: 10000, tilt_deg: 35, azimuth_deg: 0, performance_ratio: 0.85,
  provider_meta: { attribution: 'Weather data by Open-Meteo.com (CC BY 4.0)' },
};

test('an unavailable forecast preview retries promptly and backs off', async () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  h.ensure('weather-preview');
  const delays = [];
  h.context.setTimeout = (_run, ms) => { delays.push(ms); return delays.length; };
  h.context.clearTimeout = () => { };
  h.context.api = async () => ({ status: 'unavailable', provider: 'open_meteo', source: 'Open-Meteo', timezone: 'Europe/Berlin' });

  await h.context.previewWeather();
  h.context.scheduleWeatherPreview({ immediate: true, resetRetry: false });
  await flush();
  assert.deepEqual(delays, [5000, 10000]);
  h.context.scheduleWeatherPreview({ immediate: true });
  await flush();
  assert.equal(delays.at(-1), 5000, 'a user action resets the retry delay');
});

// Count how often the preview container is rewritten, so a test can tell a stable card from a
// flickering one.
function countPreviewWrites(target) {
  const writes = { count: 0 };
  const original = target.replaceChildren;
  target.replaceChildren = (...kids) => { writes.count += 1; original(...kids); };
  return writes;
}

test('the status badge goes to the title-row slot and not into the preview body', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const nodes = h.context.renderWeatherPreview({ status: 'unavailable', source: 'Open-Meteo', message: 'Loading.' });
  assert.deepEqual(JSON.parse(JSON.stringify(h.context.__status)), { slotId: 'weather-preview-status', badgeClass: 'status-badge-neutral', text: 'Unavailable' });
  assert.ok(!nodes.some((n) => n.className.includes('status-badge') || n.className.includes('preview-header')));
});

test('a failed refresh only marks the forecast stale when the backend says the data is outdated', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const base = { status: 'connected', source: 'Open-Meteo', refresh_error: 'Open-Meteo is unavailable (HTTP 503)', refresh_error_at: '2026-01-02T10:30:00Z', energy_next_24h_kwh: 1, intervals: [] };
  let nodes = h.context.renderWeatherPreview({ ...base, stale: false });
  assert.equal(h.context.__status.text, 'Connected');
  assert.ok(!nodes.some((n) => n.className.includes('notice-warning')), 'no warning for data that still covers the present');
  nodes = h.context.renderWeatherPreview({ ...base, stale: true });
  assert.equal(h.context.__status.text, 'Stale');
  const notice = nodes.find((n) => n.className.includes('notice-warning'));
  assert.equal(notice.title, base.refresh_error);
  assert.ok(!notice.children.some((n) => /503|HTTP/.test(n.textContent)));
});

test('an unsaved configuration renders no status badge, context row or message and no retry', async () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const target = h.ensure('weather-preview');
  const delays = [];
  h.context.setTimeout = (_run, ms) => { delays.push(ms); return delays.length; };
  h.context.clearTimeout = () => { };
  h.context.api = async () => ({ status: 'unsaved', provider: 'open_meteo', source: 'Open-Meteo', timezone: null });

  await h.context.previewWeather();

  assert.equal(target.children.length, 0, 'the preview shows nothing before the configuration is saved');
  assert.equal(h.context.__status, null, 'no status badge before the configuration is saved');
  assert.equal(h.byId.get('weather-next24').children.length, 0);
  assert.deepEqual(delays, [], 'an unsaved state cannot resolve by itself, so it is not polled');
});

test('a refresh keeps the shown preview in place and an identical payload does not re-render it', async () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const target = h.ensure('weather-preview');
  target.append(h.context.element('div', 'spot-market-loading'));
  h.context.setTimeout = () => 1;
  h.context.clearTimeout = () => { };
  const writes = countPreviewWrites(target);
  let pending = null;
  h.context.api = () => new Promise((resolve) => { pending = resolve; });
  const result = { status: 'unavailable', provider: 'open_meteo', source: 'Open-Meteo', message: 'Loading in the background.', timezone: 'Europe/Berlin' };

  const first = h.context.previewWeather();
  assert.equal(writes.count, 0, 'no loading placeholder replaces the current content while the request runs');
  pending({ ...result });
  await first;
  assert.equal(writes.count, 1);

  const second = h.context.previewWeather();
  assert.equal(writes.count, 1);
  pending({ ...result });
  await second;
  assert.equal(writes.count, 1, 'a poll or retry returning the same payload leaves the card untouched');

  const third = h.context.previewWeather();
  pending({ ...result, message: 'Changed.' });
  await third;
  assert.equal(writes.count, 2, 'a changed payload re-renders once');
});

// Capture every toast so a test can assert what the operator was actually told; the save button and
// its state node must exist because saveWeather() drives them.
function saveHarness(apiImpl) {
  const toasts = [];
  const h = harness({ apiImpl });
  h.ensure('weather-save');
  h.ensure('weather-save-state');
  h.context.toast = (message, kind = 'success') => toasts.push({ message, kind });
  return { h, toasts };
}

test('enabling with incomplete fields shows inline per-field errors, not a raw generic toast', async () => {
  // The backend rejects an enabled-but-incomplete config with the real pydantic 422 body: a single
  // model-level completeness error naming the missing fields.
  const error = Object.assign(new Error('Value error, an enabled solar forecast requires azimuth_deg, latitude, longitude, pv_peak_power_wp, tilt_deg'), {
    status: 422,
    detail: [{ type: 'value_error', loc: [], msg: 'Value error, an enabled solar forecast requires azimuth_deg, latitude, longitude, pv_peak_power_wp, tilt_deg' }],
  });
  const { h, toasts } = saveHarness(async () => { throw error; });
  renderForm(h); // enabled:false, every value field empty
  h.fieldsContainer.querySelector('[data-field="enabled"]').checked = true;

  await h.context.saveWeather();

  // Each missing forecast field carries the Bootstrap invalid marker and an inline message.
  for (const key of ['latitude', 'longitude', 'pv_peak_power_kwp', 'tilt_deg', 'azimuth_deg']) {
    const control = h.context.weatherControlByField(key);
    assert.ok(control.classList.contains('is-invalid'), `${key} is flagged invalid`);
    assert.equal(control.getAttribute('aria-invalid'), 'true');
    const node = h.byId.get(`weather-${key}-error`);
    assert.ok(node && node.textContent, `${key} has an inline message`);
  }
  // The operator is pointed at the fields, not shown the raw generic model-error string.
  assert.ok(toasts.some((t) => t.kind === 'warning' && /highlighted fields/i.test(t.message)), 'a field-pointer warning toast is shown');
  assert.ok(!toasts.some((t) => /Invalid weather configuration|Value error/i.test(t.message)), 'no raw generic error toast');
});

test('a per-field 422 from the problem-document shape lands on the exact field it names', async () => {
  // An out-of-range single field is rejected at the request-body boundary: the problem document
  // carries per-field entries in `errors` ({parameter, detail}), with a generic string `detail`.
  const error = Object.assign(new Error('The request body or a parameter is invalid.'), {
    status: 422,
    detail: 'The request body or a parameter is invalid.',
    fieldErrors: [{ parameter: 'performance_ratio', code: 'invalid_request', detail: 'Input should be less than or equal to 1' }],
  });
  const { h, toasts } = saveHarness(async () => { throw error; });
  renderForm(h, { enabled: true });
  await h.context.saveWeather();
  const control = h.pvFieldsContainer.querySelector('[data-field="performance_ratio"]');
  assert.ok(control.classList.contains('is-invalid'));
  assert.equal(h.byId.get('weather-performance_ratio-error').textContent, 'Input should be less than or equal to 1');
  // The operator is not shown the generic problem-document string as the only feedback.
  assert.ok(!toasts.some((t) => /request body or a parameter is invalid/i.test(t.message)));
});

test('weatherBody cannot carry a timezone override', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  assert.equal(Object.hasOwn(h.context.weatherBody(), 'timezone'), false);
});

test('a complete enabled config saves cleanly with a success toast and no field errors', async () => {
  const { h, toasts } = saveHarness(async () => COMPLETE_CONFIG);
  renderForm(h, COMPLETE_CONFIG);
  await h.context.saveWeather();
  assert.equal(h.byId.get('weather-save-state').textContent, 'Saved');
  assert.ok(toasts.some((t) => t.kind === 'success' && /saved/i.test(t.message)), 'a success toast is shown');
  for (const key of ['latitude', 'longitude', 'pv_peak_power_kwp', 'tilt_deg', 'azimuth_deg', 'performance_ratio']) {
    assert.ok(!h.context.weatherControlByField(key).classList.contains('is-invalid'), `${key} is not flagged`);
  }
});

test('an empty required performance ratio does not trigger a preview', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const ratio = h.context.weatherControlByField('performance_ratio');
  ratio.value = '';
  assert.equal(h.context.canPreviewWeather(), false);
  ratio.value = '0.85';
  assert.equal(h.context.canPreviewWeather(), true);
});

test('an in-flight weather save locks the site, provider, PV, and search controls', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { h } = saveHarness(async () => { await gate; return COMPLETE_CONFIG; });
  const { search } = renderForm(h, COMPLETE_CONFIG);
  const controls = [
    ...h.fieldsContainer.querySelectorAll('[data-field]'),
    ...h.providerFieldsContainer.querySelectorAll('[data-field]'),
    ...h.pvFieldsContainer.querySelectorAll('[data-field]'),
    search,
  ];
  const pending = h.context.saveWeather();
  assert.ok(controls.every((control) => control.disabled === true));
  release();
  await pending;
  assert.ok(controls.every((control) => control.disabled === false));
});

test('editing a flagged field clears its inline error', async () => {
  const error = Object.assign(new Error('bad'), {
    status: 422,
    detail: [{ type: 'greater_than', loc: ['pv_peak_power_wp'], msg: 'Input should be greater than 0' }],
  });
  const { h } = saveHarness(async () => { throw error; });
  renderForm(h, { enabled: true });
  await h.context.saveWeather();
  const control = h.pvFieldsContainer.querySelector('[data-field="pv_peak_power_kwp"]');
  assert.ok(control.classList.contains('is-invalid'));
  // The real form wires this through a delegated 'input' listener; invoke the per-control clearer
  // the listener calls to prove a later edit drops the stale error.
  control.value = '7.5';
  h.context.clearWeatherFieldErrorFor(control);
  assert.ok(!control.classList.contains('is-invalid'));
  assert.equal(h.byId.get('weather-pv_peak_power_kwp-error').textContent, '');
});

test('canPreviewWeather() is false when the required performance_ratio is empty and true when filled', () => {
  const h = harness();
  renderForm(h, COMPLETE_CONFIG);
  const ratio = h.pvFieldsContainer.querySelector('[data-field="performance_ratio"]');
  // An empty required performance_ratio is not preview-eligible: posting null would 422 before the
  // handler because the backend field is non-nullable.
  ratio.value = '';
  assert.equal(h.context.canPreviewWeather(), false);
  // A filled value is preview-eligible again.
  ratio.value = '0.85';
  assert.equal(h.context.canPreviewWeather(), true);
});

test('saveWeather() disables the form controls during the in-flight PUT and re-enables them afterwards', async () => {
  // Gate the PUT so the save stays in flight while we assert the whole form is locked, then release
  // it and assert the controls are usable again (here via the error path; the success path rebuilds
  // fresh, enabled controls).
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { h } = saveHarness(async () => { await gate; throw Object.assign(new Error('boom'), { status: 500 }); });
  renderForm(h, COMPLETE_CONFIG);
  const controls = [...h.fieldsContainer.querySelectorAll('[data-field]')];
  assert.ok(controls.length > 0, 'the form has controls to lock');

  const pending = h.context.saveWeather().catch(() => { });
  await flush();
  // Mid-save: every form control is disabled, not just the save button.
  for (const control of controls) assert.equal(control.disabled, true, `${control.dataset.field} is disabled during save`);

  release();
  await pending;
  // After the save settles the controls are re-enabled.
  for (const control of controls) assert.equal(control.disabled, false, `${control.dataset.field} is re-enabled after save`);
});

test('a thrown location-search error is announced in the status region, leaving the listbox option-only', async () => {
  const h = harness({ apiImpl: async () => { throw new Error('The server did not respond.'); } });
  const { search, results } = renderForm(h);
  search.value = 'Rem';
  search._listeners.input();
  await new Promise((r) => setTimeout(r, 320));
  await flush();
  const status = h.byId.get('weather-location-status');
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, 'The server did not respond.');
  // The listbox holds only real option elements: a thrown error must not inject a stray div.
  assert.equal(results.children.filter((n) => n.attributes.role === 'option').length, 0);
  assert.equal(results.children.length, 0);
});

test('Weather chart mirrors Spot Market with three day series on one 24h quarter-hour axis', () => {
  const h = harness();
  const preview = h.ensure('weather-preview');
  const box = makeNode('div', h.byId);
  const canvas = makeNode('canvas', h.byId);
  canvas.id = 'weather-chart';
  box.append(canvas);
  preview.append(box);
  let chartConfig = null;
  h.context.Chart = class {
    constructor(_canvas, config) { chartConfig = config; this.options = config.options; }
    destroy() { }
    update() { }
  };
  h.context.getComputedStyle = () => ({
    getPropertyValue(name) {
      return {
        '--bs-body-color': '#eee',
        '--rct-line': '#444',
        '--spot-market-yesterday-line': '#777',
        '--spot-market-today-line': '#0a6',
        '--spot-market-tomorrow-line': '#08f',
      }[name] || '';
    },
  });
  const interval = (start, power) => ({
    start, end: new Date(Date.parse(start) + 15 * 60 * 1000).toISOString(), power_kw: power,
  });
  h.context.drawWeatherChart({
    timezone: 'Europe/Berlin',
    yesterday_intervals: [interval('2026-07-20T10:00:00Z', 1)],
    today_intervals: [interval('2026-07-21T10:00:00Z', 2)],
    next_day_intervals: [interval('2026-07-22T10:00:00Z', 3)],
  });
  assert.equal(chartConfig.data.labels.length, 96);
  assert.equal(chartConfig.data.labels[0], '00:00');
  assert.equal(chartConfig.data.labels.at(-1), '23:45');
  assert.equal(JSON.stringify(chartConfig.data.datasets.map((dataset) => dataset.label)), JSON.stringify(['Yesterday', 'Today', 'Tomorrow']));
  assert.equal(JSON.stringify(chartConfig.data.datasets[2].borderDash), JSON.stringify([6, 4]));
  assert.equal(chartConfig.options.plugins.legend.display, true);
  assert.equal(chartConfig.options.interaction.mode, 'index');
  assert.equal(chartConfig.options.interaction.intersect, false);
  assert.equal(chartConfig.options.plugins.tooltip.filter({ parsed: { y: null } }), false);
  assert.equal(chartConfig.options.plugins.tooltip.filter({ parsed: { y: 1.5 } }), true);
  assert.equal(chartConfig.options.scales.x.type, 'category');
  assert.equal(chartConfig.data.datasets.every((dataset) => dataset.spanGaps === false), true);
});

// Shared harness for the partial-series regression: stub Chart + getComputedStyle and the DOM the
// drawer touches, then return the captured chart config (null when none was constructed) and the
// chart canvas' parent so a test can assert on the "invalid or empty" fallback paragraph.
function chartHarness() {
  const h = harness();
  const preview = h.ensure('weather-preview');
  const box = makeNode('div', h.byId);
  const canvas = makeNode('canvas', h.byId);
  canvas.id = 'weather-chart';
  box.append(canvas);
  preview.append(box);
  const captured = { config: null };
  h.context.Chart = class {
    constructor(_canvas, config) { captured.config = config; this.options = config.options; }
    destroy() { }
    update() { }
  };
  h.context.getComputedStyle = () => ({
    getPropertyValue(name) {
      return {
        '--bs-body-color': '#eee',
        '--rct-line': '#444',
        '--spot-market-yesterday-line': '#777',
        '--spot-market-today-line': '#0a6',
        '--spot-market-tomorrow-line': '#08f',
      }[name] || '';
    },
  });
  const interval = (start, power) => ({
    start, end: new Date(Date.parse(start) + 15 * 60 * 1000).toISOString(), power_kw: power,
  });
  return { h, captured, box, interval };
}

test('an empty day-bucket still draws the chart from the remaining usable series', () => {
  // Yesterday has no data (empty bucket, as the backend returns for a day the forecast window does
  // not cover); Today and Tomorrow carry real readings. The chart must still be drawn with only the
  // usable days, not blanked by the old all-or-nothing guard.
  const { h, captured, interval } = chartHarness();
  h.context.drawWeatherChart({
    timezone: 'Europe/Berlin',
    yesterday_intervals: [],
    today_intervals: [interval('2026-07-21T10:00:00Z', 2)],
    next_day_intervals: [interval('2026-07-22T10:00:00Z', 3)],
  });
  assert.ok(captured.config, 'a chart was constructed from the usable series');
  assert.equal(
    JSON.stringify(captured.config.data.datasets.map((dataset) => dataset.label)),
    JSON.stringify(['Today', 'Tomorrow']),
  );
  // Tomorrow keeps its dashed identity even when it is no longer the third dataset.
  const tomorrow = captured.config.data.datasets.find((dataset) => dataset.label === 'Tomorrow');
  assert.equal(JSON.stringify(tomorrow.borderDash), JSON.stringify([6, 4]));
});

test('a single usable day draws a one-series chart without the invalid-or-empty message', () => {
  const { h, captured, box, interval } = chartHarness();
  h.context.drawWeatherChart({
    timezone: 'Europe/Berlin',
    yesterday_intervals: [],
    today_intervals: [interval('2026-07-21T10:00:00Z', 2)],
    next_day_intervals: [],
  });
  assert.ok(captured.config, 'a chart was constructed from the single usable series');
  assert.equal(
    JSON.stringify(captured.config.data.datasets.map((dataset) => dataset.label)),
    JSON.stringify(['Today']),
  );
  assert.ok(!box.children.some((node) => node.className?.includes('text-danger')));
});

test('all day-series empty shows the invalid-or-empty message and draws no chart', () => {
  const { h, captured, box } = chartHarness();
  h.context.drawWeatherChart({
    timezone: 'Europe/Berlin',
    yesterday_intervals: [],
    today_intervals: [],
    next_day_intervals: [],
  });
  assert.equal(captured.config, null, 'no chart is constructed when nothing is usable');
  const message = box.children.find((node) => node.className?.includes('text-danger'));
  assert.ok(message, 'the fallback paragraph was rendered into the chart container');
  assert.equal(message.textContent, 'Forecast series is invalid or empty.');
});

test('a DST fall-back day keeps both duplicated-wall-clock forecast intervals', () => {
  // Mirrors the spot-market DST regression: on 2026-10-25 local Europe/Berlin 02:00 occurs twice
  // (00:00Z at +02:00 and 01:00Z at +01:00). Both format to '02:00'; the old HH:MM bucketing
  // overwrote the first with the second. The ordinal axis must keep both.
  const { h, captured } = chartHarness();
  h.context.drawWeatherChart({
    timezone: 'Europe/Berlin',
    yesterday_intervals: [],
    today_intervals: [
      { start: '2026-10-25T00:00:00Z', end: '2026-10-25T01:00:00Z', power_kw: 1.5 },
      { start: '2026-10-25T01:00:00Z', end: '2026-10-25T02:00:00Z', power_kw: 2.5 },
    ],
    next_day_intervals: [],
  });
  assert.ok(captured.config, 'a chart was constructed');
  const today = captured.config.data.datasets.find((dataset) => dataset.label === 'Today');
  const points = today.data.filter((value) => value !== null);
  assert.equal(points.length, 2, 'both duplicated-hour intervals are kept, neither overwritten');
  assert.deepEqual(points, [1.5, 2.5], 'both distinct kW values survive in input order');
  const labels = captured.config.data.labels;
  assert.deepEqual([today.data[labels.indexOf('02:00')], today.data[labels.indexOf('02:00 (2)')]], [1.5, 2.5], 'each sits in its own 02:00 occurrence slot');
});

test('a manual coordinate edit clears the stale location name and region', () => {
  const h = harness();
  renderForm(h, {
    location_name: 'Remscheid',
    location_region: 'North Rhine-Westphalia, Germany',
    latitude: 51.1798,
    longitude: 7.1925,
  });
  // Precondition: the selected-location label and weatherBody carry the saved name/region.
  assert.equal(h.byId.get('weather-selected-location').textContent, 'Remscheid');
  assert.equal(h.context.weatherBody().location_name, 'Remscheid');

  // A manual latitude edit no longer matches the chosen location, so its name/region must drop.
  const lat = h.fieldsContainer.querySelector('[data-field="latitude"]');
  lat.value = '48.1372';
  h.context.maybeResolveTimezoneFromEdit(lat);

  assert.equal(h.context.weatherBody().location_name, null, 'the stale name is cleared');
  assert.equal(h.context.weatherBody().location_region, null, 'the stale region is cleared');
  assert.equal(h.byId.get('weather-selected-location').textContent, '—', 'the label falls back to the em dash');
  assert.equal(h.byId.get('weather-selected-region').hidden, true, 'the region line is hidden');
});

test('a programmatic selectWeatherLocation still sets the name (reset fires only on manual edits)', () => {
  const h = harness({ apiImpl: async () => ({ status: 'ok', timezone: 'Europe/Berlin' }) });
  const { search, results } = renderForm(h);
  h.context.selectWeatherLocation(HIT, search, results);
  assert.equal(h.byId.get('weather-selected-location').textContent, 'Remscheid');
  assert.equal(h.context.weatherBody().location_name, 'Remscheid');
});

function drawWeatherDay(count, startIso) {
  const h = harness();
  const box = makeNode('div', h.byId);
  const canvas = makeNode('canvas', h.byId);
  canvas.id = 'weather-chart';
  box.append(canvas);
  h.ensure('weather-preview').append(box);
  let config = null;
  h.context.Chart = class { constructor(_c, cfg) { config = cfg; this.options = cfg.options; } destroy() { } update() { } };
  h.context.getComputedStyle = () => ({ getPropertyValue: () => '#123456' });
  const intervals = Array.from({ length: count }, (_, index) => {
    const start = Date.parse(startIso) + index * 900_000;
    return { start: new Date(start).toISOString(), end: new Date(start + 900_000).toISOString(), power_kw: index };
  });
  h.context.drawWeatherChart({ timezone: 'Europe/Berlin', yesterday_intervals: [], today_intervals: intervals, next_day_intervals: [] });
  return { labels: config.data.labels, today: config.data.datasets.find((d) => d.label === 'Today').data };
}

test('Weather chart on a DST fall-back day (100 slots) keeps values under their real local time', () => {
  const { labels, today } = drawWeatherDay(100, '2026-10-24T22:00:00Z');
  assert.equal(labels.length, 100);
  assert.deepEqual(labels.slice(8, 16), ['02:00', '02:15', '02:30', '02:45', '02:00 (2)', '02:15 (2)', '02:30 (2)', '02:45 (2)']);
  assert.equal(today[labels.indexOf('03:00')], 16);
  assert.equal(today[labels.indexOf('02:00 (2)')], 12);
});

test('Weather chart on a DST spring-forward day (92 slots) leaves the skipped hour empty', () => {
  const { labels, today } = drawWeatherDay(92, '2026-03-28T23:00:00Z');
  assert.equal(labels.length, 96);
  assert.deepEqual(today.slice(labels.indexOf('02:00'), labels.indexOf('03:00')), [null, null, null, null]);
  assert.equal(today[labels.indexOf('03:00')], 8);
});
