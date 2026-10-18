//
// tests/js/admin-dashboard-poll.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Failure handling of the dashboard poll (loadDashboard): HTTP 429 on an automatic poll is load
// shedding, not a user action, so it marks the dashboard stale without a toast; other automatic
// failures toast once per outage; a non-automatic load always toasts. Run with
// `node --test tests/js/*.test.js`; admin-dashboard-data.js is executed unmodified in a VM context.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-dashboard-data.js'), 'utf8');

function failure(status, message) {
  const error = new Error(message);
  error.status = status;
  if (status === 429) { error.rateLimited = true; error.retryAfter = 8; }
  return error;
}

function harness() {
  const toasts = [];
  const banner = { hidden: true, textContent: '', setAttribute() { } };
  const fakeNode = () => ({ classList: { add() { }, remove() { } }, before() { }, after() { }, hidden: false });
  const context = {
    toasts, banner,
    element: () => banner,
    $: () => fakeNode(),
    syncChildren() { },
    hhmm: () => '12:00',
    messageFrom: (error) => error.message,
    toast: (message, kind) => toasts.push({ message, kind }),
    pollDelay: (ms) => ms,
    api: async () => { throw new Error('api outcome not set'); },
    document: { hidden: false, querySelector: () => fakeNode(), addEventListener() { } },
    localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
    AbortController,
    setTimeout: () => 1,
    clearTimeout() { },
    console,
  };
  vm.runInNewContext(`${SOURCE}
renderDashboard = () => { }; // card/tile rendering is out of scope here
globalThis.__t = {
  load: loadDashboard,
  seed() { dashboardSnapshot = { at: Date.now() }; },
};`, context);
  return { ...context.__t, context, toasts, banner };
}

test('automatic poll + 429: no toast, stale banner shown, outage state kept', async () => {
  const h = harness();
  h.seed();
  h.context.api = async () => { throw failure(429, 'Too many requests'); };
  assert.equal(await h.load({ automatic: true }), 'failed');
  assert.equal(await h.load({ automatic: true }), 'failed');
  assert.deepEqual(h.toasts, []);
  assert.equal(h.banner.hidden, false);
  assert.match(h.banner.textContent, /^Too many requests, retrying/);
});

test('automatic poll + non-429 error: one toast per outage, no stacking', async () => {
  const h = harness();
  h.seed();
  h.context.api = async () => { throw failure(503, 'Server unavailable'); };
  await h.load({ automatic: true });
  await h.load({ automatic: true });
  await h.load({ automatic: true });
  assert.deepEqual(h.toasts, [{ message: 'Server unavailable', kind: 'danger' }]);
  assert.match(h.banner.textContent, /^Server error \(503\)/);
});

test('a success ends the outage: the next failure toasts again and the banner clears', async () => {
  const h = harness();
  h.seed();
  h.context.api = async () => { throw failure(503, 'Server unavailable'); };
  await h.load({ automatic: true });
  h.context.api = async () => ({ devices: [], tsdb: null });
  assert.equal(await h.load({ automatic: true }), 'ok');
  assert.equal(h.banner.hidden, true);
  h.context.api = async () => { throw failure(503, 'Server unavailable'); };
  await h.load({ automatic: true });
  assert.equal(h.toasts.length, 2);
});

test('non-automatic load + 429 still toasts', async () => {
  const h = harness();
  h.seed();
  h.context.api = async () => { throw failure(429, 'Too many requests'); };
  assert.equal(await h.load(), 'failed');
  assert.deepEqual(h.toasts, [{ message: 'Too many requests', kind: 'danger' }]);
});
