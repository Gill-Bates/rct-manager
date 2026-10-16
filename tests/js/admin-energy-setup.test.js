//
// tests/js/admin-energy-setup.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Focused tests for the Energy setup/expert panel's save discipline. Run with Node's built-in
// runner (no npm/bundler): `node --test tests/js/*.test.js`. admin-energy-setup.js is a plain
// script relying on globals from admin-core.js ($, element, api, toast, messageFrom, and the
// energy constants); the harness supplies minimal stubs and a tiny DOM, runs the source in a VM
// context, and drives the panel returned by energyAdvanced() directly (initEnergy() is never
// called, so no pollers/intervals start). These tests pin the two full-state-lost-update fixes
// (Finding 1: a single in-flight save that locks all four forms, and a revision-guarded dirty
// clear) and the empty-margin guard (Finding 3), all deterministically — no sleeps or real races.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-energy-setup.js'), 'utf8');

// A minimal DOM node: children, classList, dataset, attributes, event listeners (captured in
// _listeners), and the handful of methods the source touches (append, after, replaceChildren,
// querySelector[All], checkValidity). Setting .id registers the node in the shared byId map.
function makeNode(tag, byId) {
  let _id = '';
  const node = {
    tagName: tag, children: [], parentNode: null, parentElement: null, attributes: {}, dataset: {},
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    _text: '', hidden: false, value: '', type: '', readOnly: false, checked: false, disabled: false,
    min: '', max: '', step: '', maxLength: 0, tabIndex: 0,
    set id(v) { _id = v; if (byId) byId.set(v, node); }, get id() { return _id; },
    set className(v) { this._class = v; }, get className() { return this._class || ''; },
    set textContent(v) { this._text = String(v); this.children = []; }, get textContent() { return this._text; },
    set htmlFor(v) { this._htmlFor = v; }, get htmlFor() { return this._htmlFor || ''; },
    append(...kids) { for (let k of kids) { if (typeof k === 'string') { const text = k; k = makeNode('#text', byId); k._text = text; } k.parentNode = node; k.parentElement = node; node.children.push(k); } },
    replaceChildren(...kids) { node.children = []; node.append(...kids); },
    after(...sibs) { const parent = node.parentNode; if (!parent) return; const at = parent.children.indexOf(node); for (let i = 0; i < sibs.length; i += 1) { sibs[i].parentNode = parent; sibs[i].parentElement = parent; parent.children.splice(at + 1 + i, 0, sibs[i]); } },
    setAttribute(k, v) { node.attributes[k] = String(v); }, getAttribute(k) { return node.attributes[k] ?? null; },
    removeAttribute(k) { delete node.attributes[k]; },
    hasAttribute(k) { return k in node.attributes; },
    addEventListener(type, handler) { (node._listeners ||= {})[type] = handler; },
    checkValidity() { return node._valid !== false; },
    focus() { },
    querySelectorAll(selector) {
      const isClass = selector.startsWith('.');
      const needle = selector.replace(/^\./, '');
      const matches = (child) => (isClass
        ? (child.className || '').split(/\s+/).includes(needle)
        : child.tagName.toLowerCase() === needle.toLowerCase());
      const out = [];
      const visit = (current) => {
        for (const child of current.children || []) {
          if (matches(child)) out.push(child);
          visit(child);
        }
      };
      visit(node);
      return out;
    },
    querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
    contains(other) { return other === node || node.children.some((c) => c.contains && c.contains(other)); },
    get firstChild() { return node.children[0] || null; },
  };
  return node;
}

// Collect every node in a subtree (including the root).
function flatten(root) {
  const out = [];
  const walk = (n) => { out.push(n); for (const c of n.children || []) walk(c); };
  walk(root);
  return out;
}

function harness({ apiImpl } = {}) {
  const byId = new Map();
  const calls = [];
  const toasts = [];
  const timers = new Set();
  const document = {
    body: makeNode('body', byId),
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => makeNode(tag, byId),
    addEventListener() { },
    hidden: false,
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
    api: apiImpl || (async (p, options) => { calls.push({ path: p, options }); return []; }),
    toast: (message, kind = 'success') => { toasts.push({ message, kind }); },
    messageFrom: (e) => e.message,
    NOT_AVAILABLE: 'n/a',
    ENERGY_ACTION_LABELS: {},
    energyExpertMode: false, // lives in admin-energy-controls.js, which this harness does not load
    // Iterated as [value, text] pairs when the policy <select> is built.
    ENERGY_POLICY_MODES: [['target', 'Target'], ['below', 'Below current SoC']],
    missingRequiredWrites: () => [],
    energyChecklist: () => ({ hardwareVerified: true }),
    setTimeout(callback, delay) {
      const timer = setTimeout(() => { timers.delete(timer); callback(); }, delay);
      timers.add(timer);
      return timer;
    },
    clearTimeout(timer) { timers.delete(timer); clearTimeout(timer); },
    setInterval(callback, delay) {
      const timer = setInterval(callback, delay);
      timers.add(timer);
      return timer;
    },
    clearInterval(timer) { timers.delete(timer); clearInterval(timer); },
    AbortController, Number, Array, Object, Boolean, String, Math, Date, Set, Intl,
  };
  context.window = context;
  context.addEventListener = () => { }; // initDryRunPlan stops polling on pagehide
  vm.runInNewContext(SOURCE, context);
  context.element('div').id = 'energy-plan-schedule'; // the full-width chart host of the Charging plan tab
  return {
    context, byId, calls, toasts, cleanup: () => {
      for (const timer of timers) { clearTimeout(timer); clearInterval(timer); }
      timers.clear();
    }
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function calibrationView(overrides = {}) {
  return {
    device_id: 'dev-1', inverter_serial: 'SN-A', battery_bms_sn: 'BMS-1', battery_replaced: false,
    commissioning: { commissioned_at: '2021-05-01T00:00:00Z', source: 'logger_derived', determined_at: '2026-10-16T00:00:00Z' },
    baseline: {
      date: '2021-05-01T00:00:00Z', battery_cycles: 0, rated_usable_capacity_wh: 10368,
      current_usable_capacity_wh: 10368, remaining_capacity_percent: 100, source: 'logger_derived',
    },
    baseline_unavailable_reason: null,
    points: [{
      measured_at: '2026-10-16T12:00:00+00:00', battery_cycles: 128,
      current_usable_capacity_wh: 8538.35, rated_usable_capacity_wh: 10368, remaining_capacity_percent: 82.35,
    }],
    ...overrides,
  };
}

function calibrationHarness() {
  const h = harness();
  const host = h.context.element('div');
  host.id = 'energy-plan-calibration';
  const details = h.context.element('details');
  details.id = 'energy-plan-diagnostics';
  details.open = true;
  const charts = [];
  h.context.getComputedStyle = () => ({ getPropertyValue: () => '#456789' });
  h.context.Chart = class {
    constructor(canvas, config) { charts.push(config); }
    destroy() { }
  };
  const text = () => flatten(host).map((node) => node.textContent).join(' ');
  return { h, host, charts, text };
}

test('BMS calibration history starts at the commissioning baseline and shows date, cycles, kWh and percent', () => {
  const { h, host, charts, text } = calibrationHarness();
  try {
    h.context.renderBatteryCalibrationHistory(calibrationView());
    const table = flatten(host).find((node) => node.tagName === 'table');
    assert.ok(table);
    const tableText = flatten(table).map((node) => node.textContent).join(' ');
    assert.match(tableText, /commissioning/);
    assert.match(tableText, /128/);
    assert.match(tableText, /82[.,]35/);
    assert.match(text(), /SN-A/);
    assert.match(text(), /not verified on hardware/);
    const [measured, datasheet] = charts[0].data.datasets;
    // Wh / 1000 carries binary float noise; compare the kWh values with a tolerance.
    const measuredPoints = Array.from(measured.data, (p) => [p.x, p.y]);
    assert.deepEqual(measuredPoints.map(([x]) => x), [Date.parse('2021-05-01T00:00:00Z'), Date.parse('2026-10-16T12:00:00+00:00')]);
    assert.ok(Math.abs(measuredPoints[0][1] - 10.368) < 1e-9);
    assert.ok(Math.abs(measuredPoints[1][1] - 8.53835) < 1e-9);
    assert.equal(datasheet.data[0].y, 10.368);
    assert.equal(charts[0].options.scales.x.type, 'linear');
    const callbacks = charts[0].options.plugins.tooltip.callbacks;
    assert.deepEqual(Array.from(callbacks.afterBody([{ dataIndex: 0, datasetIndex: 0 }])), ['Cycles: 0', 'Remaining: 100 %']);
    assert.deepEqual(Array.from(callbacks.afterBody([{ dataIndex: 1, datasetIndex: 0 }])), ['Cycles: 128', 'Remaining: 82.35 %']);
    assert.deepEqual(Array.from(callbacks.afterBody([{ dataIndex: 0, datasetIndex: 1 }])), []);
  } finally { h.cleanup(); }
});

test('BMS calibration history without a commissioning date has no baseline point and says so', () => {
  const { h, host, charts, text } = calibrationHarness();
  try {
    h.context.renderBatteryCalibrationHistory(calibrationView({
      commissioning: null, baseline: null, baseline_unavailable_reason: 'no_commissioning_date',
    }));
    assert.equal(charts[0].data.datasets[0].data.length, 1);
    assert.match(text(), /No commissioning date is known/);
    assert.ok(flatten(host).some((node) => node.tagName === 'input' && node.type === 'date'));
  } finally { h.cleanup(); }
});

test('BMS calibration history empty states: no measurement, unknown serial', () => {
  const { h, text } = calibrationHarness();
  try {
    h.context.renderBatteryCalibrationHistory(calibrationView({ points: [], baseline: null, baseline_unavailable_reason: 'no_measurement' }));
    assert.match(text(), /No BMS calibration/);
    h.context.renderBatteryCalibrationHistory(calibrationView({
      inverter_serial: null, commissioning: null, baseline: null, points: [], baseline_unavailable_reason: 'inverter_serial_unknown',
    }));
    assert.match(text(), /serial number is not known yet/);
    assert.ok(!/Save date/.test(text()));
  } finally { h.cleanup(); }
});

test('saving the commissioning date PUTs it for the selected device and re-renders the result', async () => {
  const calls = [];
  const h = harness({ apiImpl: async (p, options) => { calls.push({ path: p, options }); return calibrationView(); } });
  try {
    const host = h.context.element('div');
    host.id = 'energy-plan-calibration';
    const select = h.context.element('select');
    select.id = 'energy-plan-device';
    select.value = 'dev-1';
    h.context.renderBatteryCalibrationHistory(calibrationView({ commissioning: null, baseline: null }));
    const form = flatten(host).find((node) => node.tagName === 'form');
    const input = flatten(form).find((node) => node.tagName === 'input');
    input.value = '2021-06-15';
    form._listeners.submit({ preventDefault() { } });
    await flush();
    assert.equal(calls[0].path, 'energy/devices/dev-1/battery-commissioning');
    assert.equal(calls[0].options.method, 'PUT');
    assert.deepEqual(JSON.parse(calls[0].options.body), { commissioned_on: '2021-06-15' });
  } finally { h.cleanup(); }
});

// A device payload good enough for refill()/the submit tasks.
function device(overrides = {}) {
  return {
    device_id: 'dev-1',
    device_name: 'RCT DC 10.0',
    capabilities: [],
    limits: { max_charge_power_w: 3000, max_discharge_power_w: 3000, engineering_mode: false },
    soc_target_policy: { mode: 'target', below_margin_percent: 10, note: null },
    ...overrides,
  };
}

// Build the panel and attach the limit form into the Expert section (layout('expert')) so all four
// forms are reachable in the `expert` subtree. Return the panel plus located forms/buttons/inputs.
function buildPanel(h, { apiImplOverride } = {}) {
  let current = device();
  const getDevice = () => current;
  const onChange = (next) => { if (next) current = next; };
  if (apiImplOverride) h.context.api = apiImplOverride;
  const panel = h.context.energyAdvanced('dev-1', getDevice, onChange);
  panel.layout('expert');

  const forms = flatten(panel.expert).filter((n) => n.tagName === 'form');
  const buttons = flatten(panel.expert).filter((n) => n.tagName === 'button');
  const buttonByText = (text) => buttons.find((b) => b.textContent === text);
  const formOf = (button) => forms.find((f) => f.contains(button));

  const verifyButton = buttonByText('Mark hardware as verified');
  const limitButton = buttonByText('Save limits');
  const engineeringButton = buttonByText('Save engineering mode');
  const policyButton = buttonByText('Save policy');
  const revokeButton = buttonByText('Revoke verification');

  const limitForm = formOf(limitButton);
  const engineeringForm = formOf(engineeringButton);
  const policyForm = formOf(policyButton);

  // policyMargin is the number input inside the policy form.
  const policyMargin = flatten(policyForm).find((n) => n.tagName === 'input' && n.type === 'number');
  // The limit form carries two number inputs: max charge then max discharge.
  const limitNumbers = flatten(limitForm).filter((n) => n.tagName === 'input' && n.type === 'number');
  const [maxCharge, maxDischarge] = limitNumbers;

  const submit = (form) => form._listeners.submit({ preventDefault() { } });

  return {
    panel, getDevice, setDevice: (d) => { current = d; },
    verifyButton, limitButton, engineeringButton, policyButton, revokeButton,
    limitForm, engineeringForm, policyForm,
    policyMargin, maxCharge, maxDischarge, submit,
    allButtons: [verifyButton, revokeButton, limitButton, engineeringButton, policyButton],
  };
}

test('Finding 1: a second submit is refused and all four forms lock while a save is in flight', async () => {
  const h = harness();
  let putCalls = 0;
  let resolvePut;
  const panel = buildPanel(h, {
    apiImplOverride: (p, options) => {
      if (options?.method === 'PUT') { putCalls += 1; return new Promise((resolve) => { resolvePut = resolve; }); }
      return Promise.resolve([]); // the post-success energy/devices refresh
    },
  });

  // Start the limits save with valid powers; its PUT never resolves yet.
  panel.maxCharge.value = '5';
  panel.maxDischarge.value = '5';
  panel.submit(panel.limitForm);
  await flush();
  assert.equal(putCalls, 1, 'the limits PUT fired once');
  assert.equal(panel.panel.busy(), true, 'the panel reports a save in flight');
  for (const button of panel.allButtons) {
    assert.equal(button.disabled, true, `${button.textContent} is disabled during the save`);
  }

  // A second submit on another form while saving must be refused (no second PUT, no new task).
  panel.submit(panel.engineeringForm);
  await flush();
  assert.equal(putCalls, 1, 'a concurrent submit on another form issues no second PUT');

  // Resolve the in-flight PUT; after the refresh settles every button is usable again.
  resolvePut([]);
  await flush();
  await flush();
  assert.equal(panel.panel.busy(), false, 'the panel is idle after the save settles');
  for (const button of panel.allButtons) {
    assert.equal(button.disabled, false, `${button.textContent} is re-enabled after the save`);
  }
});

test('Finding 1: a stale save does not clear the dirty flag of a form edited while it was in flight', async () => {
  const h = harness();
  let resolvePut;
  const panel = buildPanel(h, {
    apiImplOverride: (p, options) => {
      if (options?.method === 'PUT') return new Promise((resolve) => { resolvePut = resolve; });
      return Promise.resolve([]);
    },
  });

  // Start a limits save (PUT pending) with valid powers.
  panel.maxCharge.value = '5';
  panel.maxDischarge.value = '5';
  panel.submit(panel.limitForm);
  await flush();

  // While the save is in flight the user types in the limits form: this bumps the form revision.
  panel.limitForm._listeners.input();
  // The typed value the refill must NOT overwrite afterwards.
  panel.maxCharge.value = '9.999';

  // Resolve the save; because the revision changed during the request, dirty.limits stays true.
  resolvePut([]);
  await flush();
  await flush();

  // A later refill (via update) must skip the still-dirty limits field, preserving the typed value.
  panel.panel.update(device({ limits: { max_charge_power_w: 3000, max_discharge_power_w: 3000, engineering_mode: false } }), false);
  assert.equal(panel.maxCharge.value, '9.999', 'the freshly-typed value is preserved (dirty flag not cleared by the stale save)');
});

test('Finding 1: a clean save clears the dirty flag so a later refresh refills the form', async () => {
  const h = harness();
  const panel = buildPanel(h, {
    apiImplOverride: (p, options) => Promise.resolve(options?.method === 'PUT' ? {} : []),
  });

  // Mark the limits form dirty (user edited it earlier), then save without editing during flight.
  panel.limitForm._listeners.input();
  panel.maxCharge.value = '7.000';
  panel.maxDischarge.value = '7.000';
  panel.submit(panel.limitForm);
  await flush();
  await flush();

  // dirty.limits was cleared on the clean save, so a refresh now refills the field from the server.
  panel.panel.update(device({ limits: { max_charge_power_w: 4000, max_discharge_power_w: 4000, engineering_mode: false } }), false);
  assert.equal(panel.maxCharge.value, '4.00', 'a clean save lets a later refill update the field');
});

test('Finding 3: an empty SoC margin is rejected with a toast and no PUT is issued', async () => {
  const h = harness();
  let putCalls = 0;
  const panel = buildPanel(h, {
    apiImplOverride: (p, options) => {
      if (options?.method === 'PUT') putCalls += 1;
      return Promise.resolve(options?.method === 'PUT' ? {} : []);
    },
  });

  panel.policyMargin.value = '';
  panel.submit(panel.policyForm);
  await flush();

  assert.equal(putCalls, 0, 'no soc-target-policy PUT is issued for an empty margin');
  assert.ok(
    h.toasts.some((t) => t.kind === 'danger' && t.message === 'Enter a margin between 0 and 50 %.'),
    'the operator is told to enter a margin between 0 and 50 %',
  );
});

test('Finding 3: a valid SoC margin issues the PUT with the parsed number', async () => {
  const h = harness();
  const puts = [];
  const panel = buildPanel(h, {
    apiImplOverride: (p, options) => {
      if (options?.method === 'PUT') puts.push({ path: p, body: JSON.parse(options.body) });
      return Promise.resolve(options?.method === 'PUT' ? {} : []);
    },
  });

  panel.policyMargin.value = '5';
  panel.submit(panel.policyForm);
  await flush();
  await flush();

  const put = puts.find((call) => call.path.includes('soc-target-policy'));
  assert.ok(put, 'the soc-target-policy PUT was issued for a valid margin');
  assert.equal(put.body.below_margin_percent, 5, 'the margin is sent as the parsed number');
});

test('first-run plan details expose the baseline and missing history', () => {
  const h = harness();
  const details = h.context.planDetails({
    forecast_basis: 'bootstrap_baseline',
    consumption_history_available: false,
    forecast_confidence: 'low',
  });
  assert.ok(details.some((part) => part.textContent.includes('configured baseline')
    && part.textContent.includes('low confidence')
    && part.textContent.includes('usable measured history unavailable')));
});

// P2.2: the Charging-plan Expert toggle must show #energy-plan-advanced when enabled and hide it
// again when disabled. energy-plan-advanced ships hidden; before the fix setEnergyExpertMode()
// never touched it. These globals (energyExpertMode, energyPanels) live in admin-energy-controls.js,
// which this harness does not load, so seed them on the VM context before driving the toggle.
test('P2.2: Expert toggle shows and hides #energy-plan-advanced across OFF->ON->OFF', () => {
  const h = harness();
  h.context.energyExpertMode = false;
  h.context.energyPanels = new Map(); // setEnergyExpertMode iterates this; keep it empty
  // setEnergyExpertMode scans #energy-plan-result for [data-expert-only]; the harness document has
  // no querySelectorAll, so stub it to return nothing (this test seeds no expert-only nodes).
  h.context.document.querySelectorAll = () => [];
  const advanced = h.context.element('div');
  advanced.id = 'energy-plan-advanced';
  advanced.hidden = true; // matches the template's initial hidden state
  const result = h.context.element('div');
  result.id = 'energy-plan-result'; // querySelectorAll('#energy-plan-result ...') targets touch this

  h.context.setEnergyExpertMode(true);
  assert.equal(h.context.$('energy-plan-advanced').hidden, false, 'ON reveals the advanced panel');
  h.context.setEnergyExpertMode(false);
  assert.equal(h.context.$('energy-plan-advanced').hidden, true, 'OFF hides it again');
  // A second cycle must behave identically (no sticky state).
  h.context.setEnergyExpertMode(true);
  assert.equal(h.context.$('energy-plan-advanced').hidden, false, 'ON reveals it on the second cycle');
  h.context.setEnergyExpertMode(false);
  assert.equal(h.context.$('energy-plan-advanced').hidden, true, 'OFF hides it on the second cycle');
});

// P2.3: changing the plan device must reload the dry-run plan, not just refill the limit fields.
// Before the fix the change listener called fillPlanLimits() only, so a device switch kept showing
// the previous inverter's plan. Omitting #energy-plan-limits makes fillPlanLimits a no-op (its
// `if (!form) return` guard), isolating the reload path. loadDryRunPlan fetches a path containing
// `dynamic-plan`, so we assert that request fires when the device select changes.
test('P2.3: switching the plan device requests a fresh dynamic-plan', async () => {
  const paths = [];
  const h = harness({
    apiImpl: async (p) => {
      paths.push(p);
      if (p === 'energy/planning-devices') return [{ device_id: 'dev-1', device_name: 'RCT DC 10.0' }];
      return {};
    },
  });
  const select = h.context.element('select');
  select.id = 'energy-plan-device';
  select.value = 'dev-1'; // non-empty so loadDryRunPlan passes its empty-value guard
  const load = h.context.element('button');
  load.id = 'energy-plan-load';
  const result = h.context.element('div');
  result.id = 'energy-plan-result';

  h.context.initDryRunPlan();
  await flush();
  const before = paths.filter((p) => p.includes('dynamic-plan')).length;

  select._listeners.change();
  await flush();

  const after = paths.filter((p) => p.includes('dynamic-plan')).length;
  assert.ok(after > before, 'the device-change handler requests the dynamic plan');
  h.context.stopPlanPolling();
  h.cleanup();
});

// A pending plan that stayed pending past the poll limit keeps its readiness card: the periodic
// background refresh must not swap it back to the spinner; only a status change restarts the wait.
test('an exhausted pending plan is not replaced by the spinner on a background refresh', async () => {
  let status = 'spot_prices_unavailable';
  const h = harness({ apiImpl: async () => ({ status, slots: [], window: {} }) });
  const select = h.context.element('select');
  select.id = 'energy-plan-device';
  select.value = 'dev-1';
  const result = h.context.element('div');
  result.id = 'energy-plan-result';
  const spinners = () => result.children.filter((c) => (c.className || '').includes('plan-loading')).length;

  await h.context.loadDryRunPlan();
  assert.equal(spinners(), 1, 'the first wait shows the spinner');
  vm.runInContext('planPollAttempts = PLAN_POLL_MAX', h.context);
  await h.context.loadDryRunPlan({ poll: true });
  assert.equal(spinners(), 0, 'the readiness card replaces the spinner and stays');
  await h.context.loadDryRunPlan({ poll: true });
  assert.equal(spinners(), 0, 'a later background refresh keeps the card');
  status = 'solar_forecast_unavailable';
  await h.context.loadDryRunPlan({ poll: true });
  assert.equal(spinners(), 1, 'a status change restarts the wait');
  h.context.stopPlanPolling(); // the armed 10 s timer would keep the runner alive
  h.cleanup();
});

// P3.1: nextGridCharge must only merge adjacent charge slots of EQUAL power, so the "Next grid
// charge" summary never reports one slot's power for a window charged at another. The planner keeps
// adjacent charge slots with different power separate (test_dynamic_optimizer::
// test_finding_3_adjacent_charge_slots_with_different_power_stay_separate: 400 W then 2000 W).
test('P3.1: nextGridCharge does not merge adjacent charge slots of different power', () => {
  const h = harness();
  const block = h.context.nextGridCharge([
    { action: 'charge', start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', max_power_w: 400, target_soc_percent: 50 },
    { action: 'charge', start: '2026-01-01T10:15:00Z', end: '2026-01-01T10:30:00Z', max_power_w: 2000, target_soc_percent: 60 },
    { action: 'idle', start: '2026-01-01T10:30:00Z', end: '2026-01-01T10:45:00Z', max_power_w: 0 },
  ]);
  assert.equal(block.end, '2026-01-01T10:15:00Z', 'the block stops at the first power change');
  assert.equal(block.max_power_w, 400, 'the reported power stays truthful for the emitted window');
});

// loadDryRunPlan calls clearPlanForecast() first, so a previous device's forecast never lingers.
test('clearPlanForecast hides and empties the previous forecast diagnostics', () => {
  const h = harness();
  const forecast = h.context.element('section');
  forecast.id = 'energy-plan-forecast';
  forecast.hidden = true;
  const technical = h.context.element('div');
  technical.id = 'energy-plan-technical';
  const host = h.context.element('div');
  host.id = 'energy-plan-result';

  h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, slots: [], energy: {}, window: {} });
  assert.equal(forecast.hidden, false, 'an ok plan reveals the forecast section');
  assert.ok(technical.children.length > 0, 'an ok plan populates the technical diagnostics');

  h.context.clearPlanForecast();
  assert.equal(forecast.hidden, true);
  assert.equal(technical.children.length, 0);
});

test('P3.1: nextGridCharge does not merge adjacent charge slots of different power', () => {
  const h = harness();
  const block = h.context.nextGridCharge([
    { action: 'charge', start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', max_power_w: 400, target_soc_percent: 50 },
    { action: 'charge', start: '2026-01-01T10:15:00Z', end: '2026-01-01T10:30:00Z', max_power_w: 2000, target_soc_percent: 60 },
    { action: 'idle', start: '2026-01-01T10:30:00Z', end: '2026-01-01T10:45:00Z', max_power_w: 0 },
  ]);
  assert.equal(block.end, '2026-01-01T10:15:00Z', 'the block stops at the first power change');
  assert.equal(block.max_power_w, 400, 'the reported power stays truthful for the emitted window');
});

// P3: the Expert "Forecast inputs" diagnostics must never attribute a forecast to the wrong device.
// renderDryRunPlan only toggles #energy-plan-forecast once a plan renders, so loadDryRunPlan now
// calls clearPlanForecast() at the very start — before the no-device guard and before the request —
// hiding the section and emptying #energy-plan-technical. That covers the start-of-request pending
// window, the catch error path (clearPlanForecast runs before the try), and the no-device path.
// Here we render an `ok` plan for device A (section visible, technical populated), then exercise
// clearPlanForecast() the way the new load path does and assert A's forecast is gone.
test('P3: clearPlanForecast hides and empties A\'s forecast before B is calculated / on error / no device', () => {
  const h = harness();
  const forecast = h.context.element('section');
  forecast.id = 'energy-plan-forecast';
  const technical = h.context.element('div');
  technical.id = 'energy-plan-technical';
  const host = h.context.element('div');
  host.id = 'energy-plan-result';

  // Device A: an ok plan makes the forecast section visible and populates the technical block.
  h.context.renderDryRunPlan({
    status: 'ok', horizon_hours: 24, slots: [], energy: {}, window: {},
  });
  assert.equal(forecast.hidden, false, 'an ok plan reveals A\'s forecast section');
  assert.ok(technical.children.length > 0, 'an ok plan populates A\'s technical diagnostics');

  // Switching to B (start of request), a failed B request, and the no-device path all funnel
  // through clearPlanForecast() first: A's forecast must no longer be visible or present.
  h.context.clearPlanForecast();
  assert.equal(forecast.hidden, true, 'the forecast section is hidden while B is pending / on error / no device');
  assert.equal(technical.children.length, 0, 'A\'s technical diagnostics are emptied');
});

// renderDryRunPlan stays the authority for the success case: for an ok plan it reveals the section
// (forecast.hidden === false) and populates the technical block. (The non-ok hide is asserted via
// clearPlanForecast above, which is the mechanism loadDryRunPlan now runs before every request and
// on the no-device/error paths; the minimal harness cannot drive planReadinessCard's raw-string
// append on the non-ok render branch, so the non-ok hide is pinned through clearPlanForecast.)
test('P3: renderDryRunPlan reveals and populates the forecast section for an ok plan', () => {
  const h = harness();
  const forecast = h.context.element('section');
  forecast.id = 'energy-plan-forecast';
  forecast.hidden = true; // starts hidden like the template now does
  const technical = h.context.element('div');
  technical.id = 'energy-plan-technical';
  const host = h.context.element('div');
  host.id = 'energy-plan-result';

  h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, slots: [], energy: {}, window: {} });
  assert.equal(forecast.hidden, false, 'ok reveals the forecast section');
  assert.ok(technical.children.length > 0, 'ok populates the technical diagnostics');
});

test('P3.1: nextGridCharge still merges adjacent charge slots of equal power', () => {
  const h = harness();
  const block = h.context.nextGridCharge([
    { action: 'charge', start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', max_power_w: 400, target_soc_percent: 50 },
    { action: 'charge', start: '2026-01-01T10:15:00Z', end: '2026-01-01T10:30:00Z', max_power_w: 400, target_soc_percent: 60 },
    { action: 'idle', start: '2026-01-01T10:30:00Z', end: '2026-01-01T10:45:00Z', max_power_w: 0 },
  ]);
  assert.equal(block.end, '2026-01-01T10:30:00Z', 'equal-power slots merge into one block');
  assert.equal(block.max_power_w, 400, 'the merged block keeps the shared power');
});

// Off banner: the recorded control state (mode/mode_recorded) and the live inverter telemetry are
// separate truths. admin-energy-controls.js is loaded alone for its pure helpers.
const CONTROLS = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-energy-controls.js'), 'utf8');

function controlsContext() {
  const context = { Map, Set, Number, Array, Object, Boolean, String, Math, Date, Intl, document: {}, window: {} };
  vm.runInNewContext(CONTROLS, context);
  return context;
}

test('Off recorded: fresh Internal is observed, no fresh value is neutral, other fresh code is a mismatch', () => {
  const c = controlsContext();
  const off = { mode: 'off', mode_recorded: true };
  const fresh = (code) => ({ code, label: null, stale: false });
  assert.deepEqual({ ...c.energyOffView(off, fresh(4)) }, { recorded: true, observed: true, mismatch: false });
  // The backend serves code null when its cache entry expired; that is "unknown", not a mismatch.
  for (const none of [null, undefined, { code: null, label: null, stale: false }, { code: 4, stale: true }]) {
    assert.deepEqual({ ...c.energyOffView(off, none) }, { recorded: true, observed: false, mismatch: false });
  }
  assert.deepEqual({ ...c.energyOffView(off, fresh(1)) }, { recorded: true, observed: false, mismatch: true });
});

test('Off banner text appears only for a real mismatch or an unrecorded Off', () => {
  const c = controlsContext();
  const base = { connected: true, state: 'automatic', write_support_enabled: true };
  const text = (device, strategy) => c.energyControlState({ ...base, ...device }, false, c.energyOffView({ ...base, ...device }, strategy));
  // Skip the setup gate: the checklist is a global of admin-energy-setup.js; stub it as complete.
  c.energyChecklist = () => ({ hardwareVerified: true });
  c.energyNeedsSetup = () => false;
  const recorded = { mode: 'off', mode_recorded: true };
  assert.equal(text(recorded, { code: 4, stale: false }).text, '');
  assert.equal(text(recorded, null).text, '', 'no fresh value must not ask to select Off');
  assert.equal(text(recorded, { code: null, stale: false }).text, '');
  assert.match(text(recorded, { code: 1, stale: false }).text, /Select Off again/);
  assert.match(text({ mode: 'off', mode_recorded: false }, null).text, /No Energy Manager mode has been confirmed/);
  // Other modes ignore the telemetry entirely.
  assert.equal(text({ mode: 'manual', mode_recorded: true }, { code: 1, stale: false }).ready, true);
  assert.equal(c.energyOffView({ mode: 'manual', mode_recorded: true }, { code: 4, stale: false }).recorded, false);
  assert.equal(c.energyOffView({ mode: 'off', mode_recorded: false }, { code: 4, stale: false }).recorded, false);
});

// The KPI strip is built from the plan's own battery snapshot; no second data source.
function planTiles(plan) {
  const h = harness();
  h.context.formatPower = (w) => `${w} W`; // defined in a script this harness does not load
  const host = h.context.element('div');
  host.id = 'energy-plan-result';
  h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, slots: [], energy: {}, window: {}, ...plan });
  const nodes = flatten(host);
  const strip = nodes.find((n) => (n.className || '').split(/\s+/).includes('energy-plan-highlights'));
  const tiles = strip ? strip.children : [];
  return tiles.map((tile) => Object.fromEntries(flatten(tile)
    .filter((n) => /spot-market-highlight-(caption|value|sub)$/.test(n.className || ''))
    .map((n) => [n.className.split('-').pop(), n.textContent])));
}

test('Charging plan shows four KPI tiles with Battery remaining first', () => {
  const tiles = planTiles({
    battery: { soc_percent: 33, usable_capacity_wh: 10370, remaining_energy_wh: 3422.1 },
    energy: { grid_charge_energy_wh: 4000 },
    slots: [{ action: 'charge', start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', max_power_w: 2000, target_soc_percent: 80 }],
  });
  assert.equal(tiles.length, 4);
  assert.deepEqual(tiles.map((t) => t.caption), ['Battery remaining', 'Target SoC', 'Charge power', 'Total plan grid energy']);
  assert.match(tiles[0].value, /^3[.,]42 kWh$/);
  assert.match(tiles[0].sub, /^33 % SoC · 10[.,]37 kWh usable$/);
});

test('Battery remaining shows a dash, never NaN or undefined, without a snapshot', () => {
  for (const battery of [undefined, null, { soc_percent: null }]) {
    const tiles = planTiles({ battery });
    assert.equal(tiles.length, 4, 'the strip keeps its structure');
    assert.equal(tiles[0].value, '—');
    assert.ok(tiles.every((t) => !/NaN|undefined|null/.test(`${t.value} ${t.sub || ''}`)));
  }
});
