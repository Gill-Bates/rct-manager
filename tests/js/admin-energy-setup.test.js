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
    pollDelay: (ms) => ms,
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

// A transient error during a background poll keeps the pending card and re-arms the poll timer,
// so the plan still appears once the API answers again.
test('a failing background poll keeps the pending card and keeps polling', async () => {
  let mode = 'pending';
  const h = harness({
    apiImpl: async (p) => {
      if (p === 'energy/planning-devices') return [];
      if (mode === 'throw') { mode = 'ok'; throw new Error('transient'); }
      if (mode === 'pending') return { status: 'spot_prices_unavailable', slots: [], window: {} };
      return { status: 'ok', slots: [], window: {} };
    },
  });
  const select = h.context.element('select');
  select.id = 'energy-plan-device';
  select.value = 'dev-1';
  const result = h.context.element('div');
  result.id = 'energy-plan-result';
  const failures = () => result.children.some((c) => (c.className || '').includes('text-danger'));

  await h.context.loadDryRunPlan();
  h.context.stopPlanPolling();
  mode = 'throw';
  await h.context.loadDryRunPlan({ poll: true });
  assert.equal(failures(), false, 'the transient error text does not replace the card');
  assert.ok(vm.runInContext('planPollTimer !== null', h.context), 'the next poll is armed');
  h.context.stopPlanPolling();
  await h.context.loadDryRunPlan({ poll: true });
  assert.equal(vm.runInContext('planLastStatus', h.context), 'ok', 'the next poll delivers the plan');
  h.context.stopPlanPolling();
  h.cleanup();
});

// A 429 is short-lived load shedding: the Energy poll keeps what is shown (no failure state on the
// panels) and waits at least as long as the shared rate-limit cooldown before the next poll.
test('a 429 on the Energy poll keeps the panels and defers the next poll to the cooldown', async () => {
  const limited = Object.assign(new Error('Too many requests'), { status: 429, retryAfter: 30, rateLimited: true });
  const h = harness({ apiImpl: async () => { throw limited; } });
  const pollStates = [];
  Object.assign(h.context, {
    energyPanels: new Map([['dev-1', { setPollState: (state) => pollStates.push(state) }]]),
    energyHardwareStrategies: new Map(), energyPolling: false, energyPollTimer: null, energyClock: 0,
    ENERGY_POLL_MS: 3000, ENERGY_POLL_BACKOFF_MS: 30000, ENERGY_POLL_TIMEOUT_MS: 8000,
    energyPollDelay: 3000, pollDelay: (ms) => Math.max(ms, 31_000),
  });
  h.context.element('div').id = 'energy-list';
  await h.context.pollEnergy();
  assert.deepEqual(pollStates, [], 'a 429 must not flag the panels as failed');
  assert.equal(h.context.energyPollDelay, 31_000, 'the next poll waits for the cooldown');
  clearTimeout(h.context.energyPollTimer);
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

// Battery remaining is a tertiary device-state line rendered outside the plan-KPI grid.
function planBatteryLine(plan) {
  const h = harness();
  h.context.formatPower = (w) => `${w} W`;
  const host = h.context.element('div');
  host.id = 'energy-plan-result';
  h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, slots: [], energy: {}, window: {}, ...plan });
  const line = flatten(host).find((n) => (n.className || '').split(/\s+/).includes('energy-plan-battery'));
  const strip = flatten(host).find((n) => (n.className || '').split(/\s+/).includes('energy-plan-highlights'));
  const inGrid = strip ? flatten(strip).includes(line) : false;
  // The harness's textContent getter does not concatenate children, so read the text span only
  // (the glyph span carries the Material Icon ligature, not label text).
  const textSpan = line ? flatten(line).find((n) => n.tagName === 'span' && (n.className || '').split(/\s+/).includes('energy-plan-battery-value')) : null;
  const text = textSpan ? textSpan.textContent : null;
  return { text, inGrid, isTile: line ? (line.className || '').split(/\s+/).includes('spot-market-highlight') : false };
}
test('Charging plan shows three plan KPI tiles with Battery remaining as a separate tertiary line', () => {
  const plan = {
    battery: { soc_percent: 33, usable_capacity_wh: 10370, remaining_energy_wh: 3422.1 },
    energy: { grid_charge_energy_wh: 4000 },
    slots: [{ action: 'charge', start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', max_power_w: 2000, target_soc_percent: 80 }],
  };
  const tiles = planTiles(plan);
  assert.equal(tiles.length, 3);
  assert.deepEqual(tiles.map((t) => t.caption), ['Target SoC', 'Charge power', 'Total plan grid energy']);
  const battery = planBatteryLine(plan);
  assert.equal(battery.inGrid, false, 'Battery remaining must not live inside the plan-KPI grid');
  assert.equal(battery.isTile, false, 'Battery remaining must not be a .spot-market-highlight tile');
  assert.match(battery.text, /33 % SoC · 3[.,]42 kWh remaining \/ 10[.,]37 kWh usable/);
});

test('Battery remaining shows a dash, never NaN or undefined, without a snapshot', () => {
  for (const battery of [undefined, null, { soc_percent: null }]) {
    const tiles = planTiles({ battery });
    assert.equal(tiles.length, 3, 'the plan-KPI grid keeps its three tiles');
    const line = planBatteryLine({ battery });
    assert.equal(line.text, 'Not available');
    assert.ok(!/NaN|undefined|null/.test(line.text));
  }
});

test('Charging plan announces no grid charge while the Energy Manager is off; the header shows the Energy Manager mode, never Disabled', () => {
  const slots = [{ action: 'charge', start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', max_power_w: 2000, target_soc_percent: 80 }];
  for (const [mode, expectOff] of [['off', true], ['manual', false], ['external', false]]) {
    const h = harness();
    h.context.formatPower = (w) => `${w} W`;
    const host = h.context.element('div');
    host.id = 'energy-plan-result';
    const select = h.context.element('select');
    select.id = 'energy-plan-device';
    select.value = 'main';
    const badge = h.context.element('span');
    badge.id = 'energy-plan-manager-state';
    vm.runInContext(`planDevices = [{ device_id: 'main', mode: ${JSON.stringify(mode)} }]`, h.context);
    h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, slots, energy: {}, window: {} });
    const text = flatten(host).map((n) => n.textContent || '').join(' ');
    assert.equal(/Next grid charge/.test(text), !expectOff, `mode ${mode}`);
    assert.ok(!/Energy Manager is off/.test(text), `mode ${mode}: no off card, the badge carries it`);
    assert.equal(badge.textContent, { off: 'Energy Manager off', manual: 'Manual control', external: 'External control' }[mode], `mode ${mode}`);
    assert.ok(!/Disabled/.test(badge.textContent), 'the plan itself is never called disabled');
    assert.equal(badge.hidden, false);
  }
});

// The coverage line is driven only by the already-fetched consumption history: days[] with
// their source. It is neutral text (no success state) in #energy-consumption-import-status and
// must degrade gracefully when fields are missing (never NaN, never a crash).
function importStatusHarness() {
  const h = harness();
  const slot = h.context.element('div');
  slot.id = 'energy-consumption-import-status';
  const text = (index) => slot.children[index]?.textContent ?? null;
  return { h, slot, text };
}

function consumptionDays(available, total, source = 'inverter_history', local = 0) {
  return Array.from({ length: total }, (_, i) => ({
    date: `2026-01-${String(i + 1).padStart(2, '0')}`,
    energy_wh: i < available ? 1000 + i : null,
    source: i >= available ? 'unavailable' : i < local ? 'local_measured' : source,
  }));
}

test('Coverage status shows "History coverage · N/60 days" with the source split, neutral', () => {
  const { h, slot, text } = importStatusHarness();
  try {
    h.context.renderInverterHistoryStatus({ days: consumptionDays(60, 60), recording_since: null });
    assert.equal(text(0), 'History coverage · 60/60 days');
    assert.equal(text(1), '0 local · 60 inverter');
    h.context.renderInverterHistoryStatus({ days: consumptionDays(60, 60, 'inverter_history', 42), recording_since: null });
    assert.equal(text(1), '42 local · 18 inverter');
    h.context.renderInverterHistoryStatus({ days: consumptionDays(42, 60), recording_since: '2025-11-01T00:00:00Z' });
    assert.equal(text(0), 'History coverage · 42/60 days');
    assert.ok(!/Live recording|text-bg-success/.test(slot.children.map((c) => `${c.textContent} ${c.className}`).join(' ')));
    assert.equal(slot.children.length, 2, 'a re-render replaces rather than appends');
  } finally { h.cleanup(); }
});

test('Coverage status: total follows days.length, not a hard-coded 60', () => {
  const { h, text } = importStatusHarness();
  try {
    h.context.renderInverterHistoryStatus({ days: consumptionDays(5, 30), recording_since: null });
    assert.equal(text(0), 'History coverage · 5/30 days');
  } finally { h.cleanup(); }
});

test('Coverage status: with no completed day but a valid recording_since shows "recording since"', () => {
  const { h, text, slot } = importStatusHarness();
  try {
    h.context.renderInverterHistoryStatus({ days: consumptionDays(0, 60), recording_since: '2026-01-05T00:00:00Z' });
    assert.match(text(0), /^History coverage · 0\/60 days · recording since /);
    assert.ok(!/NaN|undefined|Invalid/.test(text(0)), 'the date is formatted, never NaN');
    assert.equal(slot.children.length, 1, 'no source split without any available day');
  } finally { h.cleanup(); }
});

test('Coverage status degrades gracefully: missing days render 0/0, no crash', () => {
  const { h, slot, text } = importStatusHarness();
  try {
    h.context.renderInverterHistoryStatus({});
    assert.equal(text(0), 'History coverage · 0/0 days');
    assert.equal(h.context.renderInverterHistoryStatus(null), false);
    assert.equal(slot.children.length, 0, 'a null history leaves nothing');
  } finally { h.cleanup(); }
});

test('Charging plan shows forecast confidence from the plan fields and no preliminary-plan note', () => {
  const cases = [
    [{ forecast_basis: 'adaptive_history', forecast_confidence: 'low' }, 'Low confidence'],
    [{ forecast_basis: 'adaptive_history', forecast_confidence: 'normal' }, 'Normal confidence'],
    [{ forecast_basis: 'bootstrap_baseline', forecast_confidence: 'low' }, 'Baseline forecast'],
    [{}, ''],
  ];
  for (const [fields, label] of cases) {
    const h = harness();
    const host = h.context.element('div');
    host.id = 'energy-plan-result';
    const badge = h.context.element('span');
    badge.id = 'energy-plan-confidence';
    h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, slots: [], energy: {}, window: {}, ...fields });
    assert.equal(badge.textContent, label, JSON.stringify(fields));
    assert.equal(badge.hidden, !label);
    h.context.renderDryRunPlan({ status: 'spot_prices_unavailable', slots: [], ...fields });
    assert.equal(badge.hidden, true, 'no confidence without a plan');
  }
});

test('Show all intervals offers collapsed details with merged consecutive slots', () => {
  const h = harness();
  h.context.formatPower = (w) => `${w} W`;
  h.context.element('div').id = 'energy-plan-result';
  const schedule = h.context.element('div');
  schedule.id = 'energy-plan-schedule';
  const slot = (start, end, action, power) => ({ start, end, action, max_power_w: power, target_soc_percent: 80, reason: 'forecast PV covers demand' });
  h.context.renderDryRunPlan({
    status: 'ok', horizon_hours: 24, energy: {}, window: {},
    slots: [
      slot('2026-01-01T10:00:00Z', '2026-01-01T10:15:00Z', 'charge', 2000),
      slot('2026-01-01T10:15:00Z', '2026-01-01T10:30:00Z', 'charge', 2000),
      slot('2026-01-01T10:30:00Z', '2026-01-01T11:00:00Z', 'idle', null),
    ],
  });
  const nodes = flatten(schedule);
  const details = nodes.find((n) => n.tagName === 'details');
  assert.ok(details, 'a native details element exists');
  assert.notEqual(details.open, true, 'collapsed by default');
  assert.equal(nodes.find((n) => n.tagName === 'summary').textContent, 'Show all intervals');
  const detailTable = flatten(details).find((n) => n.tagName === 'table');
  assert.equal(flatten(detailTable).filter((n) => n.tagName === 'tr' && flatten(n).some((c) => c.tagName === 'td')).length, 2);
  // Responsive stacked layout: the table opts into .data-table-stack and every data cell carries
  // its column name in data-label, so the phone card keeps each value tied to its field. Without
  // this the mobile layout (admin.css) shows values with no labels, the bug this guards against.
  const table = detailTable;
  assert.ok((table.className || '').split(/\s+/).includes('data-table-stack'), 'table opts into the stacked layout');
  const labels = ['Time', 'Action', 'Power', 'Target SoC', 'Reason'];
  const dataRows = flatten(detailTable).filter((n) => n.tagName === 'tr' && flatten(n).some((c) => c.tagName === 'td'));
  for (const dataRow of dataRows) {
    const cells = dataRow.children.filter((c) => c.tagName === 'td');
    assert.deepEqual(cells.map((c) => c.dataset.label), labels, 'each cell labelled with its column');
  }
});

test('Charging schedule details stay open across a plan refresh', () => {
  const h = harness();
  h.context.formatPower = (w) => `${w} W`;
  h.context.element('div').id = 'energy-plan-result';
  const plan = { status: 'ok', horizon_hours: 24, energy: {}, window: {}, slots: [{ start: '2026-01-01T10:00:00Z', end: '2026-01-01T10:15:00Z', action: 'charge', max_power_w: 2000, target_soc_percent: 80, reason: 'x' }] };
  h.context.renderDryRunPlan(plan);
  h.context.renderDryRunPlan(plan);
  assert.notEqual(h.byId.get('energy-plan-schedule-details').open, true, 'closed stays closed');
  h.byId.get('energy-plan-schedule-details').open = true;
  h.context.renderDryRunPlan(plan);
  assert.equal(h.byId.get('energy-plan-schedule-details').open, true, 'opened details survive the re-render');
});

function renderPlan(slots) {
  const h = harness();
  h.context.formatPower = (w) => `${(w / 1000).toFixed(2)} kW`;
  h.context.element('div').id = 'energy-plan-result';
  const schedule = h.context.element('div');
  schedule.id = 'energy-plan-schedule';
  h.context.renderDryRunPlan({ status: 'ok', horizon_hours: 24, energy: {}, window: {}, slots });
  return { h, nodes: flatten(schedule) };
}

const REASON_CHARGE = 'forecast deficit, charged as late as permitted';
const planSlot = (start, end, action, power, reason = REASON_CHARGE, target = 95) => (
  { start, end, action, max_power_w: power, target_soc_percent: target, reason });
const bodyRows = (root) => flatten(root).filter((n) => n.tagName === 'tr' && flatten(n).some((c) => c.tagName === 'td'));
const rowText = (row) => row.children.filter((c) => c.tagName === 'td').map((c) => c.textContent);

test('Plan reasons map to user-facing labels and unknown reasons stay verbatim', () => {
  const { h } = renderPlan([]);
  const ctx = h.context;
  const expected = {
    'forecast PV covers demand': ['Solar covers forecast demand', 'Solar covers demand'],
    'battery reserve covers demand': ['Battery can cover forecast demand', 'Battery covers demand'],
    'forecast deficit, charged as late as permitted': ['Grid charging needed to protect the battery reserve', 'Protect battery reserve'],
    'charge as late as permitted for target SoC': ['Grid charging scheduled to reach the target SoC', 'Reach target SoC'],
    'negative spot price, forecast deficit': ['Negative price: charge now for forecast demand', 'Use negative price'],
    'no spot price for this slot': ['No market price available for this period', undefined],
  };
  for (const [reason, [long, short]] of Object.entries(expected)) {
    assert.equal(ctx.planReasonLabel(reason), long);
    if (short) assert.equal(ctx.planPurposeLabel(reason), short);
  }
  assert.equal(ctx.planReasonLabel('something new'), 'something new');
  assert.equal(ctx.planPurposeLabel('something new'), 'something new');
});

test('Plan tooltip shows only relevant fields per action', () => {
  const { h } = renderPlan([]);
  const idle = h.context.planTooltipLines(planSlot('2026-01-01T10:00:00Z', '2026-01-01T10:15:00Z', 'idle', null, 'battery reserve covers demand', null)).join('|');
  assert.match(idle, /No grid charging/);
  assert.match(idle, /Battery can cover forecast demand/);
  assert.doesNotMatch(idle, /n\/a|Power:|Target SoC/);
  const charge = h.context.planTooltipLines(planSlot('2026-01-01T10:00:00Z', '2026-01-01T10:15:00Z', 'charge', 1510)).join('|');
  assert.match(charge, /Grid charging · 1\.51 kW/);
  assert.match(charge, /Target SoC 95 %/);
  assert.match(charge, /protect the battery reserve/);
});

test('Charging schedule lists only charge windows; Show all intervals lists every interval', () => {
  const { nodes } = renderPlan([
    planSlot('2026-01-01T01:00:00Z', '2026-01-01T01:15:00Z', 'charge', 1510),
    planSlot('2026-01-01T01:15:00Z', '2026-01-01T01:30:00Z', 'idle', null, 'battery reserve covers demand'),
    planSlot('2026-01-01T01:30:00Z', '2026-01-01T01:45:00Z', 'charge', 1510),
    planSlot('2026-01-01T01:45:00Z', '2026-01-01T02:00:00Z', 'idle', null, 'battery reserve covers demand'),
  ]);
  const details = nodes.find((n) => n.tagName === 'details');
  const main = nodes.find((n) => n.id === 'energy-plan-charge-schedule');
  const mainRows = bodyRows(main);
  assert.equal(mainRows.length, 2, 'idle gap keeps two charging events');
  assert.deepEqual(rowText(mainRows[0]).slice(1), ['1.51 kW', '95 %', 'Protect battery reserve']);
  assert.equal(flatten(main).filter((n) => n.tagName === 'th').map((n) => n.textContent).join(','), 'Time,Power,Target SoC,Purpose');
  assert.equal(bodyRows(details).length, 4);
  assert.equal(flatten(details).filter((n) => n.tagName === 'th').map((n) => n.textContent).join(','), 'Time,Action,Power,Target SoC,Reason');
});

test('Charging schedule merges only identical adjacent charge slots', () => {
  const merged = renderPlan([
    planSlot('2026-01-01T01:00:00Z', '2026-01-01T01:15:00Z', 'charge', 1510),
    planSlot('2026-01-01T01:15:00Z', '2026-01-01T01:30:00Z', 'charge', 1510),
  ]);
  assert.equal(bodyRows(merged.nodes.find((n) => n.id === 'energy-plan-charge-schedule')).length, 1);
  const power = renderPlan([
    planSlot('2026-01-01T01:00:00Z', '2026-01-01T01:15:00Z', 'charge', 1510),
    planSlot('2026-01-01T01:15:00Z', '2026-01-01T01:30:00Z', 'charge', 2500),
  ]);
  assert.equal(bodyRows(power.nodes.find((n) => n.id === 'energy-plan-charge-schedule')).length, 2);
});

test('Charging schedule without charge slots says so', () => {
  const { nodes } = renderPlan([planSlot('2026-01-01T01:00:00Z', '2026-01-01T01:15:00Z', 'idle', null, 'battery reserve covers demand')]);
  assert.ok(flatten(nodes.find((n) => n.id === 'energy-plan-charge-schedule')).some((n) => n.textContent === 'No grid charging planned.'));
});

test('Charging plan of an Off inverter shows a compact inactive notice and requests no plan', async () => {
  const paths = [];
  const h = harness({
    apiImpl: async (p) => {
      paths.push(p);
      if (p === 'energy/planning-devices') return [{ device_id: 'dev-1', device_name: 'RCT', mode: 'off' }];
      return { status: 'ok', slots: [], window: {} };
    },
  });
  const select = h.context.element('select');
  select.id = 'energy-plan-device';
  select.value = 'dev-1';
  const result = h.context.element('div');
  result.id = 'energy-plan-result';
  vm.runInContext("planDevices = [{ device_id: 'dev-1', mode: 'off' }]", h.context);

  await h.context.loadDryRunPlan();
  const text = flatten(result).map((n) => n.textContent).join(' ');
  assert.match(text, /Energy Manager off/);
  assert.match(text, /Charging plan is inactive/);
  assert.equal(flatten(result).some((n) => (n.className || '').includes('plan-loading')), false, 'no spinner');
  assert.equal(paths.some((p) => p.includes('dynamic-plan')), false, 'no plan is requested while off');
  h.context.renderDryRunPlan({ status: 'energy_manager_off', slots: [], window: {}, energy: null }, { waiting: true });
  assert.equal(vm.runInContext('planPollTimer', h.context), null, 'no retry polling is armed');
  h.context.stopPlanPolling();
  h.cleanup();
});
