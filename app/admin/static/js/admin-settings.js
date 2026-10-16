//
// app/admin/static/js/admin-settings.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  function providerLabel(code) {
    return (SPOT_MARKET_PROVIDERS.find(([value]) => value === code) || [code, code])[1];
  }

  const settingFields = [
    { key: 'docs_public', label: 'API documentation', help: 'Serve the public API documentation at /docs.', group: 'Access', type: 'toggle' },
    { key: 'auth_required', label: 'Authentication required', help: 'Protects REST API requests with a bearer token. Does not affect this admin panel, which always requires login.', group: 'Access', type: 'toggle' },
    { key: 'enable_metrics_endpoint', label: 'Enable Metrics Endpoint', help: 'Serve the metrics at the Prometheus endpoint (/metrics).', group: 'Prometheus', type: 'toggle' },
    { key: 'metrics_require_token', label: 'Require token for metrics', help: 'Require a read token to scrape /metrics, except for trusted scrape peers.', group: 'Prometheus', type: 'toggle', requires: 'enable_metrics_endpoint' },
    { key: 'metrics_trusted_sources', label: 'Trusted scrape peers', help: 'IP addresses or CIDR networks, comma separated. These peers bypass the metrics token requirement.', group: 'Prometheus', type: 'list', requires: 'enable_metrics_endpoint' },
    { key: 'metrics_rate_limit_requests', label: 'Scrape limit', help: 'Maximum 1 to 10000 scrapes per window.', group: 'Prometheus', type: 'number', min: 1, max: 10000, requires: 'enable_metrics_endpoint' },
    { key: 'metrics_rate_limit_window_seconds', label: 'Scrape window (seconds)', help: 'Window length, 1 to 3600 seconds.', group: 'Prometheus', type: 'number', min: 1, max: 3600, requires: 'enable_metrics_endpoint' },
    { key: 'enable_write_support', label: 'Write access', help: 'Allow write requests for the approved parameters.', group: 'Write access', type: 'toggle' },
    { key: 'bind_address', label: 'Bind address', help: 'Address the service listens on.', group: 'Server', type: 'text' },
    { key: 'bind_port', label: 'Port', help: 'Port of the HTTP service, 1024 to 65535.', group: 'Server', type: 'number', min: 1024, max: 65535 },
    { key: 'log_level', label: 'Log level', help: 'Verbosity of the server logs.', group: 'Server', type: 'select', options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'] },
    { key: 'behind_reverse_proxy', label: 'Reverse proxy', help: 'Enable when a trusted proxy sits in front of the server.', group: 'Network', type: 'toggle' },
    { key: 'trusted_proxies', label: 'Trusted proxies', help: 'IP addresses or networks, comma separated.', group: 'Network', type: 'list' },
    { key: 'forwarded_header', label: 'Forwarded header', help: 'HTTP header carrying the original client address.', group: 'Network', type: 'text' },
  ];

  // The service switches its listener in the background. The connection monitor shows a calm
  // status and reloads once the service answers, on the new port if the port changed.
  function followListenerChange(saved) {
    const port = Number(saved.bind_port);
    const current = Number(location.port || (location.protocol === 'https:' ? 443 : 80));
    const moved = !saved.behind_reverse_proxy && Number.isInteger(port) && port !== current;
    window.RCTReconnect?.applying(moved ? `${location.protocol}//${location.hostname}:${port}` : null);
  }

  // Inside a container the listener port is internal; see CONTAINER_PORT_NOTE.
  let runningInContainer = false;
  const CONTAINER_PORT_NOTE = 'This service runs in a container: changing the internal port changes neither the published Docker port mapping nor the BIND_PORT used by the container health check.';
  const CONTAINER_PORT_CONSEQUENCE = ' The container reports unhealthy, and the published port no longer reaches the service, until BIND_PORT in the environment or compose file matches and the port mapping is updated.';

  // Changes that can lock the operator out of this UI or widen trust: confirmed before they autosave.
  // Returns the confirmation text, or null when the field is harmless or the value did not change.
  function riskyChange(key, next) {
    if (JSON.stringify(next) === JSON.stringify(settingsDraft[key])) return null;
    const target = () => `${key === 'bind_address' ? next : settingsDraft.bind_address}:${key === 'bind_port' ? next : settingsDraft.bind_port}`;
    if (key === 'bind_address' || key === 'bind_port') {
      return {
        title: 'Change listen address?',
        message: `The service switches to ${target()} in the background. If that address is not reachable from your network, you can no longer open this admin interface.`
          + (runningInContainer && key === 'bind_port' ? ` ${CONTAINER_PORT_NOTE}${CONTAINER_PORT_CONSEQUENCE}` : ''),
        confirmLabel: 'Switch listen address',
      };
    }
    if (key === 'behind_reverse_proxy' || key === 'trusted_proxies') {
      return {
        title: 'Change proxy trust?',
        message: 'Client addresses are taken from the forwarded header of trusted proxies. A wrong value can lock you out or let clients spoof their address.',
        confirmLabel: 'Save proxy setting',
      };
    }
    if (key.endsWith('_allow_plaintext_credentials') && next === true) {
      return {
        title: 'Allow plaintext credentials?',
        message: 'The export credentials are sent over unencrypted HTTP to a remote host, where anyone on the network path can read them.',
        confirmLabel: 'Allow plaintext',
        danger: true,
      };
    }
    return null;
  }

  // Puts a control back to the draft value after a declined confirmation (no change event fires).
  function restoreControl(control, value) {
    if (control.type === 'checkbox') control.checked = Boolean(value);
    else control.value = Array.isArray(value) ? value.join(', ') : String(value ?? '');
  }

  let settingsCommitted = {};
  let settingsDraft = {};
  let tsdbRuntimeStatus = null;
  const pendingKeys = new Set();          // scalar keys
  let pendingExportGroup = false;         // the export fields are queued as a group, never per key
  let exportEnabledSaving = false;        // the master switch persists independently of the group
  const secretRevision = new Map();       // secret key -> monotonically increasing edit counter
  const sendRevisions = new Map();        // secret key -> revision snapshot of the in-flight request
  let settingsTimer = null;
  let settingsSending = false;
  let settingsInFlight = null;            // { promise, keys: Set<string>, sections: Set<string> }

  // Mirrors api.py:97 _SECRET_EDITABLE. NOT a suffix test: `metrics_require_token` is a boolean
  // toggle whose key ends in `_token`, and a suffix test classifies it as a secret — which both
  // drops it from the payload when off and blanks the checkbox when on.
  const SECRET_KEYS = new Set(['influxdb_token', 'questdb_password']);
  function isSecretKey(key) { return SECRET_KEYS.has(key); }
  const SECRET_PLACEHOLDER = '•••••••• (stored)';

  const SAVE_STATES = { idle: 0, saved: 1, incomplete: 2, unsaved: 3, saving: 4, failed: 5 };
  const SAVE_STATE_TEXT = {
    failed: ['Save failed', 'text-danger'],
    saving: ['Saving …', 'text-secondary'],
    unsaved: ['Unsaved changes', 'text-warning-emphasis'],
    incomplete: ['Incomplete — not saved yet', 'text-warning-emphasis'],
    saved: ['', 'text-secondary'], // success is announced by the toast, not by a second label
    idle: ['', 'text-secondary'],
  };
  const SAVE_STATE_CLASSES = ['text-danger', 'text-secondary', 'text-warning-emphasis', 'text-success'];
  const lastIncompleteToast = new Map();  // id -> last incomplete message already toasted (no repeats)
  const saveSections = new Map();         // id -> { state, message, failure }
  const anySection = (states) => [...saveSections.values()].some((entry) => states.includes(entry.state));

  const FAILURE_MESSAGES = {
    general: (reason) => `Not saved (${reason}). The value was restored — enter it again.`,
    export: (reason) => `The export settings were not saved (${reason}).`,
    parameters: (reason) => `Not saved (${reason}). The list was reloaded from the server — apply your change again.`,
  };
  function failureMessage(id, reason) { return (FAILURE_MESSAGES[id] || FAILURE_MESSAGES.general)(reason); }

  function sectionOf(key) {
    return exportFields.some((field) => field.key === key) ? 'export' : 'general';
  }

  // Every anchor is block level, and null when the section has no surface on this page.
  // #exposed-list is a <tbody>, so the parameters anchor on prometheus is its table wrapper.
  function anchorFor(id) {
    if (id === 'parameters') return document.querySelector('.param-table-wrap');
    return document.getElementById('settings-sections');
  }

  // A section notice is a sibling *before* its anchor: aria-live="off" plus that placement avoids
  // the double announcement a node inside the aria-live="polite" #settings-sections would cause.
  // `kind` is 'error' (a rejected save) or 'hint' (an incomplete group that is being withheld).
  const NOTICE_IDS = { error: 'save-error', hint: 'save-hint' };

  function sectionNotice(id, kind) {
    const anchor = anchorFor(id);
    if (!anchor) return null;
    const host = anchor.closest('details') || anchor; // never insert into a folded disclosure
    if (host.tagName === 'DETAILS' && kind === 'error') host.open = true; // the error must be on screen
    const nodeId = `${NOTICE_IDS[kind]}-${id}`;
    let node = document.getElementById(nodeId);
    if (!node) {
      node = element('div', `alert save-alert alert-${kind === 'error' ? 'danger' : 'warning'}`);
      node.id = nodeId;
      node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      node.setAttribute('aria-live', 'off');
    }
    if (node.nextElementSibling !== host) host.parentElement.insertBefore(node, host);
    return node;
  }

  function sectionAlert(id) { return sectionNotice(id, 'error'); }

  function removeNotice(id, kind) { document.getElementById(`${NOTICE_IDS[kind]}-${id}`)?.remove(); }

  // Retry only where a retry can do something. `general` rolled the value back, so there is
  // nothing left to resend; `parameters` reloaded the server's list, so a retry would re-PUT the
  // server's own state and could overwrite a concurrent change from another session.
  const RETRY_ACTIONS = {
    export: () => { applyExport().catch(() => { }); },
  };

  function renderSectionAlert(id, message) {
    const node = sectionAlert(id);
    if (!node || node.dataset.message === message) return; // rebuilding would drop focus on Retry
    node.dataset.message = message;
    node.replaceChildren(element('p', 'mb-0', message));
    const actions = element('div', 'd-flex flex-wrap gap-2 mt-2');
    if (RETRY_ACTIONS[id]) {
      const retry = element('button', 'btn btn-outline-danger btn-sm', 'Retry');
      retry.type = 'button';
      retry.addEventListener('click', RETRY_ACTIONS[id]);
      actions.append(retry);
    }
    if (actions.childElementCount) node.append(actions);
  }

  function setSectionState(id, state, message = '') {
    const previous = saveSections.get(id);
    // `failure` outlives `state`. Editing a row after a rejected save moves the section to
    // `unsaved`, but the rejection is not resolved by typing, so the alert (and its Retry) has to
    // survive until a save actually succeeds or the edits are discarded. Only `saved` and `idle`
    // clear it; `unsaved`/`saving`/`incomplete` carry it forward.
    const resolved = state === 'saved' || state === 'idle';
    const failure = state === 'failed' ? message : (resolved ? '' : previous?.failure || '');
    saveSections.set(id, {
      state, message, failure,
    });
    renderSaveState();
  }

  // The single writer of #save-state and of the per-section alerts; four independent writers are
  // what produced the "Saving …" lie an incomplete row used to show.
  function renderSaveState() {
    for (const [id, entry] of saveSections) {
      if (entry.failure) renderSectionAlert(id, entry.failure);
      else document.getElementById(`save-error-${id}`)?.remove();
      // An incomplete group is reported once per distinct message, as a warning toast; the fields
      // themselves carry aria-invalid and their "Required." help text.
      if (entry.state === 'incomplete' && entry.message) {
        if (lastIncompleteToast.get(id) !== entry.message) { lastIncompleteToast.set(id, entry.message); toast(entry.message, 'warning'); }
      } else lastIncompleteToast.delete(id);
    }
    refreshExportBar();
    const label = $('save-state');
    if (!label) return; // absent on tokens/about/login/change-password; the alerts above are independent
    let top = 'idle';
    for (const entry of saveSections.values()) {
      if (SAVE_STATES[entry.state] > SAVE_STATES[top]) top = entry.state;
    }
    const [text, className] = SAVE_STATE_TEXT[top];
    label.textContent = text;
    label.dataset.state = top;
    label.classList.remove(...SAVE_STATE_CLASSES);
    label.classList.add(className);
  }

  function reportSaveFailure(section, message) {
    setSectionState(section, 'failed', message); // renders the persistent alert
    toast(message, 'danger');                    // unchanged visibility floor, also when the alert is absent
    return sectionAlert(section);
  }

  // Only the autosaving 'general' settings use the debounce; the export group is sent by its Apply button.
  function scheduleSettingsFlush() { clearTimeout(settingsTimer); settingsTimer = setTimeout(() => { flushSettings({ sections: ['general'] }).catch(() => { }); }, 450); }

  function queueSettings(key) {
    pendingKeys.add(key);
    setSectionState(sectionOf(key), 'unsaved');
    scheduleSettingsFlush();
  }

  function incompleteSections() {
    return new Set([...saveSections].filter(([, entry]) => entry.state === 'incomplete').map(([id]) => id));
  }

  // null => everything pending; otherwise only the keys whose section is in `sections`. An
  // `incomplete` section is withheld per section, never by failing the whole flush.
  function keysFor(sections) {
    const type = settingsDraft.db_type || '';
    const wanted = sections ? new Set(sections) : null;
    const blocked = incompleteSections();
    const allowed = (id) => (!wanted || wanted.has(id)) && !blocked.has(id);
    const keys = [...pendingKeys].filter((key) => allowed(sectionOf(key)));
    if (pendingExportGroup && allowed('export')) keys.push(...exportPayloadKeys(type));
    return keys.filter((key) => !(isSecretKey(key) && !settingsDraft[key])); // an empty secret is never sent
  }

  function hasPending(sections) { return keysFor(sections).length > 0; }

  // Only a section the caller explicitly named can make its flush fail. A debounced autosave
  // (sections === null) must not report failure because some other part of the page is incomplete.
  function requestedIncomplete(sections) {
    if (!sections) return false;
    const blocked = incompleteSections();
    return sections.some((id) => blocked.has(id));
  }

  const FLUSH_ROUNDS = 8; // guard against a pathological queue/fail loop

  // Resolves true only if every key belonging to `sections` was confirmed by the server. Keys
  // outside `sections` stay queued and do not influence the result; `sections: null` is the
  // debounced autosave, whose boolean is ignored.
  async function flushSettings({ sections = null } = {}) {
    let ok = true;
    for (let round = 0; round < FLUSH_ROUNDS; round += 1) {
      if (settingsSending) {
        const inFlight = settingsInFlight;
        const result = await inFlight.promise.catch(() => false);
        // only a save that actually carried one of our sections can make our result false
        if (!sections || sections.some((id) => inFlight.sections.has(id))) ok = result && ok;
        continue;
      }
      if (requestedIncomplete(sections)) return false; // a section the caller asked for is withheld
      if (!hasPending(sections)) return ok;            // nothing (left) to send for this scope
      clearTimeout(settingsTimer);
      ok = (await sendSettings(sections).catch(() => false)) && ok;
    }
    console.warn('flushSettings: round cap reached');
    return false;
  }

  // Synchronous wrapper: an async function cannot publish its own promise before its first await,
  // and "settingsSending is true the moment the caller returns" must hold without a microtask gap.
  function sendSettings(sections = null) {
    // The export group is re-validated here, at send time, so a configuration that became
    // incomplete inside the 450 ms window is never sent and a db_type switch cannot leak the
    // previous backend's fields.
    const type = settingsDraft.db_type || '';
    if (pendingExportGroup && !exportReady(type)) {
      pendingExportGroup = false;
      setSectionState('export', 'incomplete', missingExportHint(type));
    }
    const keys = keysFor(sections);
    if (!keys.length) {
      renderSaveState();
      // Nothing to send is success — unless a re-check above just withheld a section the caller
      // explicitly asked for; then "nothing to send" means "not confirmed".
      return Promise.resolve(!requestedIncomplete(sections));
    }
    // The keys this request carries leave the queue now. Without it hasPending() stays true after a
    // successful send, flushSettings() re-sends until the round cap, and the rollback guard below
    // could never distinguish "re-queued during the request" from "still queued".
    for (const key of keys) pendingKeys.delete(key);
    if (keys.some((key) => sectionOf(key) === 'export')) pendingExportGroup = false;
    settingsSending = true;
    const promise = sendSettingsInner(keys);
    settingsInFlight = { promise, keys: new Set(keys), sections: new Set(keys.map(sectionOf)) };
    for (const id of settingsInFlight.sections) setSectionState(id, 'saving');
    return promise;
  }

  function secretKeysIn(payload) { // only secrets the server actually stored
    return Object.keys(payload).filter((key) => isSecretKey(key) && payload[key]);
  }

  async function sendSettingsInner(keys) {
    const sentSections = new Set(keys.map(sectionOf));
    const payload = Object.fromEntries(keys.map((key) => [key, structuredClone(settingsDraft[key])]));
    for (const key of keys) if (isSecretKey(key)) sendRevisions.set(key, secretRevision.get(key) || 0);
    try {
      const result = await api('settings', { method: 'PUT', body: JSON.stringify(payload) });
      settingsCommitted = result.settings || { ...settingsCommitted, ...payload };
      // A saved secret is dropped from the draft right away; otherwise the raw value stays in page
      // memory and keeps being resent on every later unrelated export-settings change. Gated by the
      // revision, so an older response cannot clear a value typed while the request was in flight.
      for (const key of secretKeysIn(payload)) {
        settingsDraft[`${key}_configured`] = true;
        if (secretRevision.get(key) !== sendRevisions.get(key)) continue; // a newer value was typed meanwhile
        settingsDraft[key] = '';
        const control = $(`setting-${key}`);
        if (control) { control.value = ''; control.placeholder = SECRET_PLACEHOLDER; }
      }
      // Switching write access on makes the server approve the registers manual control needs.
      if (keys.includes('enable_write_support') && payload.enable_write_support) refreshWriteApprovals();
      if (page === 'prometheus') markPrometheusSaved();
      for (const id of sentSections) setSectionState(id, 'saved');
      if (page === 'tsdb' && sentSections.has('export')) {
        refreshTsdbRuntimeStatus().catch((error) => console.warn('Could not refresh TSDB status:', messageFrom(error)));
      }
      if (result.applying) followListenerChange(result.settings || settingsCommitted);
      else toast('Settings saved and active.');
      return true;
    } catch (error) {
      // A full renderSettings() would tear down and rebuild every section's DOM, including one the
      // operator was mid-edit on but that was never part of this save — losing focus and any input
      // in that unrelated section. Only the controls for the keys that actually failed are refreshed.
      // A failed save can still have stored part of the request, so the server state is the baseline.
      try {
        const fresh = await api('settings');
        if (fresh.settings) settingsCommitted = structuredClone(fresh.settings);
      } catch { /* keep the last known state */ }
      for (const key of keys) {
        if (pendingKeys.has(key)) continue;  // re-queued during the request: that value wins
        if (isSecretKey(key)) continue;      // the committed view holds no raw secret, only _configured
        if (sectionOf(key) === 'export') continue; // the Apply form keeps what was typed so it can be corrected
        settingsDraft[key] = structuredClone(settingsCommitted[key]);
        const control = $(`setting-${key}`);
        if (!control) continue;
        if (control.type === 'checkbox') control.checked = Boolean(settingsDraft[key]);
        else control.value = Array.isArray(settingsDraft[key]) ? settingsDraft[key].join(', ') : String(settingsDraft[key] ?? '');
      }
      const reason = messageFrom(error);
      for (const id of sentSections) reportSaveFailure(id, failureMessage(id, reason));
      return false;
    } finally {
      settingsSending = false;
      settingsInFlight = null;
      sendRevisions.clear();               // the map's lifetime is exactly this request
      renderSaveState();
      if (hasPending(['general'])) scheduleSettingsFlush(); // re-arm the 450 ms timer, no recursion
    }
  }

  // Shared by settingControl() and exportControl(): builds the label/control/help structure for
  // the common field types. `options` accepts either a flat value list (settingFields' log_level)
  // or [value, text] pairs (exportFields); 'secret' only occurs in exportFields, 'list' only in
  // settingFields, and both pass through harmlessly for the function that never uses them.
  function buildFieldControl(field, value) {
    const wrap = element('div', 'setting-row');
    const label = element('label', field.type === 'toggle' ? 'form-check-label' : 'form-label', field.label);
    const id = `setting-${field.key}`;
    label.htmlFor = id;
    let control;
    if (field.type === 'select') {
      control = element('select', 'form-select');
      for (const option of field.options) {
        const [optionValue, text] = Array.isArray(option) ? option : [option, option];
        const item = element('option', '', text);
        item.value = optionValue;
        control.append(item);
      }
      // log_level is the only settingFields select and has no explicit "unset" option, so an
      // absent value falls back to its documented default instead of selecting nothing.
      control.value = field.key === 'log_level' ? String(value || 'INFO').toUpperCase() : String(value ?? '');
    } else {
      control = element('input', field.type === 'toggle' ? 'form-check-input' : 'form-control');
      control.type = field.type === 'toggle' ? 'checkbox' : field.type === 'number' ? 'number' : field.type === 'secret' ? 'password' : 'text';
      if (field.type === 'toggle') control.checked = Boolean(value);
      else if (field.type === 'list') control.value = Array.isArray(value) ? value.join(', ') : '';
      else if (field.type !== 'secret') control.value = String(value ?? '');
      if (field.type === 'secret') { control.autocomplete = 'new-password'; control.placeholder = settingsDraft[`${field.key}_configured`] ? SECRET_PLACEHOLDER : ''; }
      // The bounds come from the field definition so the browser signals what the server accepts.
      if (field.type === 'number') { control.min = String(field.min); control.max = String(field.max); control.inputMode = 'numeric'; }
    }
    control.id = id;
    const help = element('div', 'form-text', runningInContainer && field.key === 'bind_port' ? `${field.help} ${CONTAINER_PORT_NOTE}` : field.help);
    help.id = `${id}-help`;
    control.setAttribute('aria-describedby', help.id);
    if (field.type === 'toggle') {
      const check = element('div', 'form-check form-switch d-flex align-items-center gap-2');
      check.append(control, label);
      wrap.append(check, help);
    } else wrap.append(label, control, help);
    return { wrap, control, label, help };
  }

  function settingControl(field) {
    const { wrap, control } = buildFieldControl(field, settingsDraft[field.key]);
    const onChange = async () => {
      let next;
      if (field.type === 'number') {
        const invalid = numberFieldInvalid(control, field);
        if (invalid) { reportInvalid(control, invalid); return; }
        clearInvalid(control);
        next = control.value === '' ? null : Number(control.value);
      } else if (field.type === 'toggle') next = control.checked;
      else if (field.type === 'list') next = control.value.split(',').map((item) => item.trim()).filter(Boolean);
      else next = control.value;
      const risk = riskyChange(field.key, next);
      if (risk && !(await confirmAction(risk))) { restoreControl(control, settingsDraft[field.key]); return; }
      settingsDraft[field.key] = next;
      queueSettings(field.key);
      if (page === 'prometheus') renderPrometheusSummary();
      // A master toggle decides which dependent fields exist, so only the dependents are rebuilt
      // live — the toggle keeps the focus it had when it was operated. On the Prometheus page the
      // master lives in the top card and its dependents in #settings-sections, so only that host is
      // rebuilt; elsewhere the master and its dependents share one group card.
      if (gatesOtherFields(field.key)) {
        if (page === 'prometheus') preserveFocus(rerenderPrometheusDependents);
        else preserveFocus(() => rerenderGroup(field.group));
      }
    };
    control.addEventListener('change', onChange);
    return wrap;
  }

  // A field with `requires` is shown only while its master setting is on; the master itself
  // re-renders on change so the group follows without a reload.
  function gatesOtherFields(key) {
    return settingFields.some((field) => field.requires === key);
  }

  function settingVisible(field) {
    return !field.requires || Boolean(settingsDraft[field.requires]);
  }

  // Shared by settingControl() and exportControl(): an empty value is only valid for a field
  // explicitly marked nullable (e.g. influxdb_port); everything else (min/max/step/required) is
  // left to the native checkValidity(), which already reads the min/max set from field.min/max.
  // Returns a validation message, or '' when the value is fine.
  function numberFieldInvalid(control, field) {
    if (control.value === '' && !field.nullable) return 'This field is required.';
    if (!control.checkValidity()) return control.validationMessage || 'The value is out of range.';
    return '';
  }

  // The native reportValidity() popup stays — it is what sighted users and the e2e harness see —
  // but it is no longer the only channel: the message also lands in a referenced error node.
  function reportInvalid(control, message) {
    control.setAttribute('aria-invalid', 'true');
    const errorId = `${control.id}-error`;
    let node = document.getElementById(errorId);
    if (!node) {
      node = element('div', 'invalid-feedback d-block');
      node.id = errorId;
      control.parentElement?.append(node);
    }
    node.textContent = message;
    control.setAttribute('aria-describedby', `${control.id}-help ${errorId}`);
    control.setCustomValidity(message);
    control.reportValidity();
    control.setCustomValidity(''); // otherwise the native state stays stuck invalid for a later valid edit
  }

  function clearInvalid(control) {
    if (!control.hasAttribute('aria-invalid')) return;
    control.removeAttribute('aria-invalid');
    const node = document.getElementById(`${control.id}-error`);
    if (node) node.textContent = '';
    control.setAttribute('aria-describedby', `${control.id}-help`);
  }

