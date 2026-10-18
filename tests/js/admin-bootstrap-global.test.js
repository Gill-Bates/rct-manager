//
// tests/js/admin-bootstrap-global.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin.js'), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('admin startup preserves the Bootstrap library global', async () => {
  const bootstrapLibrary = {
    Tab: { getOrCreateInstance: () => ({ show() {} }) },
    Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) },
  };
  const toasts = [];
  let initEnergyCalls = 0;
  const context = {
    page: 'energy',
    bootstrap: bootstrapLibrary,
    document: { body: { dataset: {} }, addEventListener() {}, dispatchEvent() {} },
    CustomEvent: class {},
    api() {},
    pollDelay: (ms) => ms,
    element() {},
    confirmAction() {},
    initShell() {},
    messageFrom: (error) => error.message,
    toast: (message, kind) => toasts.push({ message, kind }),
    session: async () => ({}),
    initEnergy: () => {
      initEnergyCalls += 1;
      context.window.bootstrap.Tab.getOrCreateInstance({}).show();
    },
    ensureSession: async () => ({}),
    setAuthInitError() {},
    initAuthForm() {},
    submitLogin() {},
    submitPasswordChange() {},
    initDashboardPolling() {},
    initInvertersModalLazy() {},
    loadDashboard: async () => {},
    loadMetricCount: async () => {},
    initTokens() {},
    initSettings: async () => {},
    initParameters: async () => {},
    Promise,
    Object,
    Array,
    Error,
  };
  context.window = context;

  vm.runInNewContext(SOURCE, context);
  await flush();
  await flush();

  assert.equal(initEnergyCalls, 1);
  assert.equal(context.bootstrap, bootstrapLibrary);
  assert.deepEqual(toasts, []);
});
