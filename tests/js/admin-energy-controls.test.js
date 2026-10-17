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
