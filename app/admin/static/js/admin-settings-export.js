//
// app/admin/static/js/admin-settings-export.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  const exportFields = [
    { key: 'db_type', label: 'Database type', help: 'Target of the metrics export. The Prometheus endpoint (/metrics) is independent of it.', type: 'select', options: [['', 'Select ...'], ['influxdb_v2', 'InfluxDB 2'], ['questdb', 'QuestDB (Open Source)']] },
    { key: 'metrics_export_enabled', label: 'Enable export', help: 'Pause pushing without losing the connection settings below.', type: 'toggle' },
    { key: 'metrics_export_interval_seconds', label: 'Export interval (seconds)', help: '5 to 3600.', type: 'number', min: 5, max: 3600, any: true },
    { key: 'influxdb_hostname', label: 'Hostname or URL', help: 'Host or http(s) URL without a path.', type: 'text', backend: 'influxdb_v2' },
    { key: 'influxdb_port', label: 'Port', help: 'Empty: default 8086 or the port of the URL.', type: 'number', min: 1, max: 65535, nullable: true, backend: 'influxdb_v2' },
    { key: 'influxdb_tls_enabled', label: 'Enable TLS', help: 'Use HTTPS for a plain host name.', type: 'toggle', backend: 'influxdb_v2' },
    { key: 'influxdb_verify_tls', label: 'Verify certificate', help: 'Verify the TLS certificate of the server.', type: 'toggle', backend: 'influxdb_v2' },
    { key: 'influxdb_measurement_name', label: 'Measurement', help: 'Letters, digits and underscores.', type: 'text', backend: 'influxdb_v2' },
    { key: 'influxdb_allow_plaintext_credentials', label: 'Allow plaintext credentials', help: 'Send the token over unencrypted HTTP to a remote host.', type: 'toggle', backend: 'influxdb_v2' },
    { key: 'influxdb_organization', label: 'Organization', help: 'Required.', type: 'text', backend: 'influxdb_v2' },
    { key: 'influxdb_bucket', label: 'Bucket', help: 'Required.', type: 'text', backend: 'influxdb_v2' },
    { key: 'influxdb_token', label: 'Token', help: 'Required. Never displayed; leave empty to keep the current value.', type: 'secret', backend: 'influxdb_v2' },
    { key: 'questdb_hostname', label: 'Hostname or URL', help: 'Host or http(s) URL without a path.', type: 'text', backend: 'questdb' },
    { key: 'questdb_port', label: 'Port', help: 'Empty: default 9000 or the port of the URL.', type: 'number', min: 1, max: 65535, nullable: true, backend: 'questdb' },
    { key: 'questdb_tls_enabled', label: 'Enable TLS', help: 'Use HTTPS for a plain host name.', type: 'toggle', backend: 'questdb' },
    { key: 'questdb_verify_tls', label: 'Verify certificate', help: 'Verify the TLS certificate of the server.', type: 'toggle', backend: 'questdb' },
    { key: 'questdb_measurement_name', label: 'Table name', help: 'Letters, digits and underscores.', type: 'text', backend: 'questdb' },
    { key: 'questdb_allow_plaintext_credentials', label: 'Allow plaintext credentials', help: 'Send the credentials over unencrypted HTTP to a remote host.', type: 'toggle', backend: 'questdb' },
    { key: 'questdb_username', label: 'Username', help: 'HTTP Basic Auth; only together with a password.', type: 'text', backend: 'questdb' },
    { key: 'questdb_password', label: 'Password', help: 'Never displayed; leave empty to keep the current value.', type: 'secret', backend: 'questdb' },
    { key: 'questdb_downsampling', label: 'Downsampling', help: 'Off keeps raw data for the total retention; Manual uses only the raw retention without a rollup.', type: 'select', options: [['off', 'Off'], ['manual', 'Manual (no rollup)'], ['low', 'Low (1 min, 30 days raw)'], ['medium', 'Medium (1 min, 7 days raw)'], ['high', 'High (5 min, 1 day raw)']], backend: 'questdb' },
    { key: 'questdb_raw_retention_days', label: 'Raw data retention (days)', help: 'Required in Manual; empty uses the preset default with a rollup. At most the total retention with a rollup.', type: 'number', min: 1, max: 36500, nullable: true, backend: 'questdb' },
    { key: 'questdb_retention_days', label: 'Total retention (days)', help: '0 keeps all data. An existing TTL in QuestDB is not overwritten.', type: 'number', min: 0, max: 36500, backend: 'questdb' },
  ];

  async function saveExportEnabled(next, onChange) {
    exportEnabledSaving = true;
    settingsDraft.metrics_export_enabled = next;
    onChange(); // rebuild dependants and replace the switch with its disabled in-flight state
    pendingExportGroup = false;
    pendingKeys.add('metrics_export_enabled');
    setSectionState('export', 'unsaved');
    try {
      const saved = await flushSettings({ sections: ['export'] });
      if (!saved) settingsDraft.metrics_export_enabled = Boolean(settingsCommitted.metrics_export_enabled);
    } finally {
      exportEnabledSaving = false;
      onChange();
      queueExportSettings(); // any other edited export fields still belong to Apply changes
    }
  }

  function exportControl(field, onChange) {
    // The shared setting-row (label, control, .form-text) is used as built, like on every other page.
    const { wrap, control } = buildFieldControl(field, settingsDraft[field.key]);
    // A secret left empty keeps its current value server-side (the PUT handler drops ""), so an
    // empty box must not overwrite settingsDraft and must not autosave on every blur.
    if (field.key === 'metrics_export_enabled') control.disabled = exportEnabledSaving;
    control.addEventListener('change', async () => {
      if (field.type === 'secret') {
        if (control.value === '') return;          // an empty box keeps the stored secret; no queueing
        settingsDraft[field.key] = control.value;
        secretRevision.set(field.key, (secretRevision.get(field.key) || 0) + 1);
      } else if (field.type === 'number') {
        const invalid = numberFieldInvalid(control, field);
        if (invalid) { reportInvalid(control, invalid); return; }
        clearInvalid(control);
        settingsDraft[field.key] = control.value === '' ? null : Number(control.value);
      } else if (field.type === 'toggle') {
        const risk = riskyChange(field.key, control.checked);
        if (risk && !(await confirmAction(risk))) { restoreControl(control, settingsDraft[field.key]); return; }
        if (field.key === 'metrics_export_enabled') {
          saveExportEnabled(control.checked, onChange).catch((error) => toast(messageFrom(error), 'danger'));
          return;
        }
        settingsDraft[field.key] = control.checked;
      } else settingsDraft[field.key] = control.value;
      if (['db_type', 'metrics_export_enabled', 'questdb_downsampling'].includes(field.key)) onChange();
      control.dataset.touched = '1';
      queueExportSettings();
    });
    return wrap;
  }

  // _settings_view() never exposes a secret's raw value, only its "<key>_configured" flag, so
  // hasOwn(settingsDraft, field.key) alone would hide the password/token inputs entirely.
  function exportFieldKnown(field) {
    return Object.hasOwn(settingsDraft, field.key)
      || (field.type === 'secret' && Object.hasOwn(settingsDraft, `${field.key}_configured`));
  }

  function exportGroup(key) {
    if (key === 'metrics_export_enabled') return 'Control';
    if (key === 'db_type') return 'Selector';
    if (key === 'metrics_export_interval_seconds' || /_(downsampling|raw_retention_days|retention_days)$/.test(key)) return 'Export';
    if (/_(hostname|port|tls_enabled|verify_tls|allow_plaintext_credentials)$/.test(key)) return 'Connection';
    return 'Database';
  }

  // Mirrors Settings._check_export: these fields must exist together before the backend will
  // accept the group, so autosave withholds the request (no error toast) until they are filled.
  function exportRequiredKeys(type) {
    if (type === 'influxdb_v2') return ['influxdb_hostname', 'influxdb_organization', 'influxdb_bucket', 'influxdb_token'];
    if (type === 'questdb') return settingsDraft.questdb_downsampling === 'manual'
      ? ['questdb_hostname', 'questdb_raw_retention_days'] : ['questdb_hostname'];
    return [];
  }

  // Mirrors Settings._check_export's `bool(a) != bool(b)` pairing checks: these fields are
  // optional, but once one of a pair is filled the other must be too, or the PUT is rejected
  // with a 400 (QUESTDB_USERNAME/QUESTDB_PASSWORD "must be set together or not at all").
  const EXPORT_PAIRED_FIELDS = { questdb: [['questdb_username', 'questdb_password']] };

  function exportFieldFilled(key) {
    if (isSecretKey(key)) return Boolean(settingsDraft[key]) || Boolean(settingsDraft[`${key}_configured`]);
    const value = settingsDraft[key];
    return value !== undefined && value !== null && String(value).trim() !== '';
  }

  function exportPairsReady(type) {
    return (EXPORT_PAIRED_FIELDS[type] || []).every(([a, b]) => exportFieldFilled(a) === exportFieldFilled(b));
  }

  function exportReady(type) {
    return exportRequiredKeys(type).every(exportFieldFilled) && exportPairsReady(type);
  }

  // Single visibility rule for rendering, dirty detection and the payload. Hidden fields keep their
  // unsaved draft (re-showing restores it) but are neither compared nor sent, so a field the user
  // cannot see can never leave Apply enabled with nothing to resolve it. While the export is off
  // only the switch itself counts, because applying a disabled export sends nothing else.
  function exportFieldVisible(field, type) {
    if (field.key !== 'metrics_export_enabled' && !settingsDraft.metrics_export_enabled) return false;
    if ((field.backend && field.backend !== type) || (field.any && !type)) return false;
    if (field.key === 'questdb_raw_retention_days' && settingsDraft.questdb_downsampling !== 'manual') return false;
    if (field.key === 'questdb_retention_days' && settingsDraft.questdb_downsampling === 'manual') return false;
    return true;
  }

  // The fields currently shown for the active backend.
  function exportPayloadKeys(type) {
    return exportFields.filter((field) => exportFieldKnown(field) && exportFieldVisible(field, type)).map((field) => field.key);
  }

  function labelOf(key) { return exportFields.find((field) => field.key === key)?.label || key; }

  function listPhrase(items) {
    if (items.length <= 1) return items[0] || '';
    return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
  }

  function backendLabel(type) {
    const options = exportFields.find((field) => field.key === 'db_type')?.options || [];
    const hit = options.find(([value]) => value === type);
    return hit ? hit[1] : (type || 'the selected backend');
  }

  // exportReady() fails for two structurally different reasons, and the pairing one leaves every
  // required key present — a single "… are required" message would then name nothing missing.
  function missingExportHint(type) {
    const missing = exportRequiredKeys(type).filter((key) => !exportFieldFilled(key)).map(labelOf);
    if (missing.length) {
      return `Not saved yet: ${listPhrase(missing)} ${missing.length > 1 ? 'are' : 'is'} required for ${backendLabel(type)}.`;
    }
    const pair = (EXPORT_PAIRED_FIELDS[type] || []).find(([a, b]) => exportFieldFilled(a) !== exportFieldFilled(b));
    // Mirrors the server's own 400 text, so the two surfaces cannot contradict each other.
    if (pair) return `Not saved yet: ${labelOf(pair[0])} and ${labelOf(pair[1])} must be set together or left empty.`;
    return 'Not saved yet.';
  }

  // The export group is applied explicitly, as one coherent group: nothing is sent while typing.
  // Apply stays disabled until the active backend's required fields are filled, and the group is
  // re-validated again at send time (sendSettings), so the server never sees a half-entered combination.
  function exportChangedKeys() {
    const type = settingsDraft.db_type || '';
    return exportPayloadKeys(type).filter((key) => (isSecretKey(key)
      ? Boolean(settingsDraft[key])
      : JSON.stringify(blankToNull(settingsDraft[key])) !== JSON.stringify(blankToNull(settingsCommitted[key]))));
  }

  const blankToNull = (value) => (value === '' || value === undefined ? null : value); // an emptied field equals an unset one

  function exportDirty() { return page === 'tsdb' && exportChangedKeys().length > 0; }

  // A required field that was edited and is still empty is flagged on the field itself.
  function markMissingExportFields() {
    const type = settingsDraft.db_type || '';
    const missing = new Set(settingsDraft.metrics_export_enabled ? exportRequiredKeys(type).filter((key) => !exportFieldFilled(key)) : []);
    for (const key of exportRequiredKeys(type)) {
      const control = $(`setting-${key}`);
      if (!control) continue;
      const flagged = missing.has(key) && control.dataset.touched === '1';
      control.classList.toggle('is-invalid', flagged);
      if (flagged) control.setAttribute('aria-invalid', 'true'); else control.removeAttribute('aria-invalid');
    }
  }

  // Marks the draft state; never starts a timer and never sends.
  function queueExportSettings() {
    const type = settingsDraft.db_type || '';
    if (!exportDirty()) setSectionState('export', 'idle');
    else if (settingsDraft.metrics_export_enabled && !exportReady(type)) setSectionState('export', 'incomplete', missingExportHint(type));
    else setSectionState('export', 'unsaved');
    markMissingExportFields();
    refreshExportBar();
  }

  // The button is the only visible element of the form's action bar; its native disabled state
  // tells assistive technology that there is nothing (valid) to apply.
  function refreshExportBar() {
    const apply = $('export-apply');
    if (!apply) return;
    const incomplete = Boolean(settingsDraft.metrics_export_enabled) && !exportReady(settingsDraft.db_type || '');
    apply.disabled = settingsSending || !exportDirty() || incomplete;
    apply.setAttribute('aria-busy', String(settingsSending));
  }

  async function applyExport() {
    if (settingsSending) return;
    const type = settingsDraft.db_type || '';
    if (settingsDraft.metrics_export_enabled) {
      if (!exportReady(type)) { queueExportSettings(); return; }
      pendingExportGroup = true;
    } else {
      pendingExportGroup = false;
      pendingKeys.add('metrics_export_enabled'); // a disabled export sends only the switch
    }
    setSectionState('export', 'unsaved');
    await flushSettings({ sections: ['export'] });
    refreshExportBar();
  }

  // Keep at most this many lines in the console DOM so a long-running tail never grows without
  // bound; the oldest lines are dropped, matching the server-side ring buffer behaviour.
  const TSDB_CONSOLE_MAX_LINES = 500;

  function appendTsdbConsoleLine(consoleNode, entry) {
    const line = element('div', 'tsdb-console-line');
    // The log level is a colour hook only (no new class, so the TSDB CSS stays console-scoped):
    // `.tsdb-console-line[data-level="ERROR"]` colours the line red, WARNING amber, DEBUG dim.
    line.dataset.level = entry.level || 'INFO';
    const when = entry.timestamp ? new Date(entry.timestamp) : new Date();
    const timestamp = element('time', 'tsdb-console-time', fullTimestamp(when));
    // The logger name is the dimmed/accent column; data-role keeps it stylable without a new class.
    const logger = element('span', '', entry.logger || '');
    logger.dataset.role = 'logger';
    line.append(timestamp, logger, element('span', '', entry.message || ''));
    consoleNode.append(line);
    while (consoleNode.childElementCount > TSDB_CONSOLE_MAX_LINES) consoleNode.firstElementChild.remove();
    consoleNode.scrollTop = consoleNode.scrollHeight; // auto-scroll to the newest line
  }

  // Wire a freshly rendered console node to the live tsdb.log stream. The node fills from real
  // backend log records (replayed backlog first, then live) instead of browser-fabricated status
  // text; registering on the central dispatcher means reconnect and connection state are not our
  // concern. The returned unsubscribe is called before the next render so a replaced node stops
  // receiving (and the stream closes once no console is listening).
  function startTsdbConsoleStream(consoleNode) {
    return events.on('tsdb.log', (entry) => appendTsdbConsoleLine(consoleNode, entry));
  }

  async function refreshTsdbRuntimeStatus() {
    const data = await api('devices');
    tsdbRuntimeStatus = data.tsdb || null;
  }

  // Export fields autosave through the shared queueSettings/flushSettings path. The cards are plain
  // settings cards (h2.h5 + setting rows) placed straight into the shared .settings-grid.
  function renderExportSettings(host) {
    const rerender = () => preserveFocus(render);
    const knownField = (key) => exportFields.find((field) => field.key === key && exportFieldKnown(field));
    const card = (name, title, nodes, extraClass = '') => {
      const section = element('section', `card export-card export-card-${name} ${extraClass}`.trim());
      const body = element('div', 'card-body');
      body.append(element('h2', 'h5 mb-2', title), ...nodes);
      section.append(body);
      return section;
    };
    const groupCard = (group, title, type) => card(group.toLowerCase(), title,
      exportFields.filter((field) => exportGroup(field.key) === group && exportFieldKnown(field) && exportFieldVisible(field, type))
        .map((field) => exportControl(field, rerender)));
    // The live log stream is tied to the console node currently on screen. Each render replaces that
    // node, so the previous node's subscription is torn down first; when the console is not shown
    // (export off or unconfigured) nothing stays subscribed and the EventSource closes.
    let consoleUnsubscribe = null;
    const render = () => {
      if (consoleUnsubscribe) { consoleUnsubscribe(); consoleUnsubscribe = null; }
      const type = settingsDraft.db_type || '';
      const enabled = Boolean(settingsDraft.metrics_export_enabled);
      const cards = [card('control', 'Export to time-series database',
        [knownField('metrics_export_enabled'), enabled ? knownField('db_type') : null]
          .filter(Boolean).map((field) => exportControl(field, rerender)))];
      if (enabled && type) {
        const consoleNode = element('div', 'tsdb-console');
        consoleNode.id = 'tsdb-mini-console';
        consoleNode.setAttribute('role', 'log');
        consoleNode.setAttribute('aria-live', 'polite');
        // The console scrolls its own overflow; make it keyboard-focusable so a keyboard user can
        // reach and scroll it, and so the :focus-visible ring in admin.css has something to show.
        consoleNode.tabIndex = 0;
        consoleUnsubscribe = startTsdbConsoleStream(consoleNode);
        cards.push(
          groupCard('Database', 'Target database settings', type),
          groupCard('Connection', 'Connection', type),
          groupCard('Export', 'Export settings', type),
          card('console', 'Status & Log', [consoleNode], 'card-span-all'),
        );
      }
      host.replaceChildren(...cards);
      refreshExportBar();
    };
    $('export-actions')?.replaceChildren(buildApplyBar('export', {
      onApply: () => { applyExport().catch((error) => toast(messageFrom(error), 'danger')); },
    }));
    render();
  }

  // Mirrors the server-side host plausibility check `_HOST` in app/admin/api.py; keep the two in
  // step. This is the client-side pre-check only — the server validates again on save (JS-02).
