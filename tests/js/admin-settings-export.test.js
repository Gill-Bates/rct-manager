//
// tests/js/admin-settings-export.test.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Visibility, dirty detection and payload of the TSDB export group must share one rule. Run with
// `node --test tests/js/*.test.js`; admin-settings-export.js is executed in a VM context with the
// few settings globals it reads stubbed.

'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '../../app/admin/static/js/admin-settings-export.js'), 'utf8');

function harness(draft, committed) {
  const context = {
    settingsDraft: { ...draft },
    settingsCommitted: { ...committed },
    isSecretKey: (key) => /token|password/.test(key),
    page: 'tsdb',
    JSON, Object, Boolean, String, Array,
  };
  vm.runInNewContext(`${SOURCE}\nglobalThis.__t = { exportPayloadKeys, exportChangedKeys, exportDirty };`, context);
  return { ...context.__t, draft: context.settingsDraft };
}

const BASE = {
  db_type: 'questdb', metrics_export_enabled: true, metrics_export_interval_seconds: 10,
  questdb_hostname: 'h', questdb_port: null, questdb_downsampling: 'off',
  questdb_raw_retention_days: null, questdb_retention_days: 30,
};

test('retention fields follow the downsampling mode in payload and dirty detection', () => {
  const h = harness(BASE, BASE);
  assert.ok(h.exportPayloadKeys('questdb').includes('questdb_retention_days'));
  assert.ok(!h.exportPayloadKeys('questdb').includes('questdb_raw_retention_days'));
  // A hidden raw-retention draft neither marks the group dirty nor is sent.
  h.draft.questdb_raw_retention_days = 7;
  assert.equal(h.exportDirty(), false);
  h.draft.questdb_downsampling = 'manual';
  assert.ok(h.exportPayloadKeys('questdb').includes('questdb_raw_retention_days'));
  assert.ok(!h.exportPayloadKeys('questdb').includes('questdb_retention_days'));
  assert.deepEqual([...h.exportChangedKeys()].sort(), ['questdb_downsampling', 'questdb_raw_retention_days']);
  // Switching back hides it again; the draft value survives but cannot block Apply.
  h.draft.questdb_downsampling = 'off';
  assert.equal(h.exportDirty(), false);
  assert.equal(h.draft.questdb_raw_retention_days, 7);
});

test('a disabled export is dirty only through its switch, never through hidden field edits', () => {
  const h = harness({ ...BASE, metrics_export_enabled: false, questdb_hostname: 'edited' }, { ...BASE, metrics_export_enabled: false });
  assert.equal(h.exportDirty(), false, 'edits of hidden fields cannot leave an unresolvable Apply state');
  assert.deepEqual([...h.exportPayloadKeys('questdb')], ['metrics_export_enabled']);
  h.draft.metrics_export_enabled = true;
  assert.ok(h.exportChangedKeys().includes('metrics_export_enabled'));
  assert.ok(h.exportChangedKeys().includes('questdb_hostname'));
});
