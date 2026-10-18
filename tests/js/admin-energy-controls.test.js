//
// tests/js/admin-energy-controls.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Battery action buttons of the Energy Manager panel: Off is operational rest, so Charge, Keep idle
// and Discharge are really disabled and can trigger no command; the mode choice stays operable.
// Run: node --test tests/js/admin-energy-controls.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// The flow graphic and the advanced section are drawing code unrelated to the action buttons.
const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-energy-controls.js'), 'utf8')
  .replace('function energyFlowGraphic(', 'function realEnergyFlowGraphic(')
  .replace('function energyAdvanced(', 'function realEnergyAdvanced(');

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

function flatten(root) {
  const out = [];
  const walk = (n) => { out.push(n); for (const c of n.children || []) walk(c); };
  walk(root);
  return out;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness(apiImpl) {
  const byId = new Map();
  const calls = [];
  const toasts = [];
  const context = {
    document: { createElement: (tag) => makeNode(tag, byId), getElementById: (id) => byId.get(id) || null, addEventListener() { } },
    element: (tag, className, value) => {
      const n = makeNode(tag, byId);
      if (className) n.className = className;
      if (value !== undefined) n.textContent = String(value);
      return n;
    },
    setClass: (node, name, on) => node.classList.toggle(name, Boolean(on)),
    setText: (node, text) => { node.textContent = text; },
    api: async (p, options) => { calls.push({ path: p, options }); return apiImpl ? apiImpl(p, options) : {}; },
    toast: (message, kind = 'success') => { toasts.push({ message, kind }); },
    messageFrom: (e) => e.message,
    NOT_AVAILABLE: 'n/a',
    deviceCardCount: 0,
    isCollapsed: () => false,
    rememberCollapsed() { },
    pollHardwareStrategies() { },
    energyFlowGraphic: () => ({ svg: makeNode('svg', byId), update() { } }),
    energyAdvanced: () => ({ expert: makeNode('div', byId), update() { }, setTimestamp() { }, busy() { }, layout() { }, step() { } }),
    setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() { },
    Number, Array, Object, Boolean, String, Math, Date, Set, Map, Intl,
  };
  context.window = context;
  vm.runInNewContext(SOURCE, context);
  return { context, calls, toasts };
}

const VERIFIED = ['write_path_convention', 'battery_power_sign_convention', 'grid_power_sign_convention']
  .map((name) => ({ name, status: 'verified' }));
const ACTIONS = ['charge', 'hold', 'discharge'].map((action) => ({ action, available: true, reason: null }));

function device(mode, overrides = {}) {
  return {
    device_id: 'inv', device_name: 'Inverter', host: 'h', connected: true, mode, mode_recorded: true,
    write_support_enabled: true, required_write_names: [], approved_write_names: [], limits: { max_charge_power_w: 3000 },
    capabilities: VERIFIED, actions: ACTIONS, readings: {}, target_soc_window: { min: 10, max: 100 }, ...overrides,
  };
}

function actionButtons(panel) {
  return flatten(panel.root).filter((n) => n.tagName === 'button' && ['Charge battery', 'Keep battery idle', 'Discharge battery'].includes(n.textContent));
}
const modeButtons = (panel) => flatten(panel.root).filter((n) => n.attributes.role === 'radio');

test('Off: Charge, Keep idle and Discharge are disabled and a click sends no command', async () => {
  const h = harness();
  const panel = h.context.createEnergyPanel(device('off'), 1);
  const buttons = actionButtons(panel);
  assert.equal(buttons.length, 3);
  for (const button of buttons) {
    assert.equal(button.disabled, true, `${button.textContent} is disabled`);
    assert.ok(button.classList.contains('is-inactive'), 'carries the inactive marker the style keys on');
    button._listeners.click();
  }
  await flush();
  assert.equal(h.calls.length, 0, 'no request is made');
  for (const button of modeButtons(panel)) assert.equal(button.disabled, false, 'the mode choice stays selectable');
});

test('Manual with a complete setup enables the actions as the availability says; External keeps them disabled', () => {
  const h = harness();
  const manual = h.context.createEnergyPanel(device('manual', { actions: [
    { action: 'charge', available: true, reason: null },
    { action: 'hold', available: false, reason: 'limits_missing' },
    { action: 'discharge', available: true, reason: null },
  ] }), 1);
  assert.deepEqual(actionButtons(manual).map((b) => b.disabled), [false, true, false]);
  const external = h.context.createEnergyPanel(device('external'), 1);
  assert.deepEqual(actionButtons(external).map((b) => b.disabled), [true, true, true]);
});

test('Off to Manual enables the actions only after the backend answered; Manual to Off disables them and closes the target slider', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness(async (p, options) => {
    if (options?.method === 'PUT') {
      const mode = JSON.parse(options.body).mode;
      await gate;
      return device(mode);
    }
    return {};
  });
  const panel = h.context.createEnergyPanel(device('off'), 1);
  const buttons = actionButtons(panel);
  const manualOption = modeButtons(panel).find((b) => (b.attributes['aria-label'] || '').startsWith('Manual'));
  manualOption._listeners.click();
  await flush();
  assert.deepEqual(buttons.map((b) => b.disabled), [true, true, true], 'still disabled while the PUT is in flight');
  release();
  await flush(); await flush();
  assert.deepEqual(buttons.map((b) => b.disabled), [false, false, false], 'enabled after the successful answer');

  buttons[0]._listeners.click(); // opens the Charge target slider
  const target = flatten(panel.root).find((n) => (n.className || '').split(/\s+/).includes('energy-target'));
  assert.equal(target.hidden, false, 'the slider is open');
  const offOption = modeButtons(panel).find((b) => (b.attributes['aria-label'] || '').startsWith('Off'));
  offOption._listeners.click();
  await flush(); await flush();
  assert.deepEqual(buttons.map((b) => b.disabled), [true, true, true], 'disabled right after the successful Off');
  assert.equal(target.hidden, true, 'the target slider is closed');
});

// Flow graphic: a unit is coloured exactly when it is active; everything else stays neutral grey.
const reading = (value) => ({ value, stale: false });
const readings = (pv, grid, battery, house, soc = 50) => ({
  pv_power_w: reading(pv), grid_power_w: reading(grid), battery_power_w: reading(battery),
  house_load_w: reading(house), battery_soc_percent: reading(soc),
});

test('flow activity: no, single and all units active', () => {
  const { context } = harness();
  const activity = (...args) => ({ ...context.energyFlowActivity(readings(...args)) });
  const none = { pv: null, grid: null, battery: null, house: null };
  assert.deepEqual(activity(0, 0, 0, 0), none);
  assert.deepEqual(activity(2, 19, -7, 7), none, 'below the idle threshold is inactive (PV 2 W, battery 7 W)');
  assert.deepEqual(activity(2400, 0, 0, 0), { ...none, pv: 'flow-pv' });
  assert.deepEqual(activity(0, 830, 0, 0), { ...none, grid: 'flow-import' });
  assert.deepEqual(activity(0, -500, 0, 0), { ...none, grid: 'flow-export' });
  assert.deepEqual(activity(0, 0, -900, 0), { ...none, battery: 'flow-charge' });
  assert.deepEqual(activity(0, 0, 900, 0), { ...none, battery: 'flow-discharge' });
  assert.deepEqual(activity(0, 0, 0, 852), { ...none, house: 'flow-pv' });
  assert.deepEqual(activity(0, 830, 0, 852), { ...none, grid: 'flow-import', house: 'flow-import' });
  assert.deepEqual(activity(3000, -500, -900, 1600), { pv: 'flow-pv', grid: 'flow-export', battery: 'flow-charge', house: 'flow-pv' });
  assert.deepEqual(activity(-5, 20, 20, 20), { pv: null, grid: 'flow-import', battery: 'flow-discharge', house: 'flow-import' }, 'threshold is inclusive; negative PV is not generation');
});

test('flow activity: unknown readings are inactive', () => {
  const { context } = harness();
  const unknown = { pv_power_w: null, grid_power_w: { value: null }, battery_power_w: {}, house_load_w: undefined };
  assert.deepEqual({ ...context.energyFlowActivity(unknown) }, { pv: null, grid: null, battery: null, house: null });
});

test('flow graphic: icon colour classes follow the status pills and the line animation', () => {
  const byId = new Map();
  const { context } = harness();
  context.document.createElementNS = (_ns, tag) => {
    const node = makeNode(tag, byId);
    const set = node.setAttribute;
    node.setAttribute = (k, v) => { set(k, v); if (k === 'class') node.className = String(v); };
    return node;
  };
  const graphic = context.realEnergyFlowGraphic();
  const all = (root, cls) => flatten(root).filter((n) => (n.className || '').split(/\s+/).includes(cls) || n.classList._set.has(cls));
  const state = () => {
    const nodes = Object.fromEntries(all(graphic.svg, 'flow-node').map((n) => [n.querySelector('.flow-node-label').textContent.split(' ')[0], n.classList._set.has('is-active')]));
    const badges = Object.fromEntries(all(graphic.badges, 'flow-badge').map((b) => [
      b.querySelector('.flow-badge-caption').textContent,
      { active: b.classList._set.has('is-active'), pill: b.querySelector('.flow-badge-state').textContent },
    ]));
    const lines = all(graphic.svg, 'flow-line').map((l) => !l.classList._set.has('is-idle'));
    return { nodes, badges, lines };
  };
  graphic.update(readings(2, 830, 7, 852, 7));
  let s = state();
  assert.equal(s.badges.Generation.pill, 'No generation');
  assert.equal(s.badges.Battery.pill, 'Idle');
  assert.deepEqual([s.nodes.PV, s.nodes.Grid, s.nodes.House, s.nodes.Battery], [false, true, true, false]);
  assert.deepEqual([s.badges.Generation.active, s.badges.Consumption.active, s.badges.Grid.active, s.badges.Battery.active], [false, true, true, false]);
  assert.deepEqual(s.lines, [false, true, false]);
  graphic.update(readings(0, 0, 0, 0, 7));
  s = state();
  assert.ok(!Object.values(s.nodes).some(Boolean) && !Object.values(s.badges).some((b) => b.active) && !s.lines.some(Boolean));
  graphic.update(readings(3000, -500, -900, 1600));
  s = state();
  assert.ok(Object.values(s.nodes).every(Boolean) && Object.values(s.badges).every((b) => b.active) && s.lines.every(Boolean));
});
