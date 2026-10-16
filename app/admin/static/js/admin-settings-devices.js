//
// app/admin/static/js/admin-settings-devices.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  const HOST_PATTERN = /^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]+)$/;

  // Mirrors the server network-id upper bound in app/admin/api.py (`_network_id`, 2**32 - 1).
  const MAX_NETWORK_ID = 2 ** 32 - 1;

  // Empty means "directly attached", exactly as the server reads a missing network id.
  function networkId(value) {
    return value === '' || value === null || value === undefined ? null : Number(value);
  }

  // Reports the offending input together with the message, so the row marks and focuses the field
  // that actually failed instead of always blaming the host.
  function deviceProblem(device) {
    const host = String(device.host || '').trim();
    if (!host) return { field: 'host', message: 'Enter an IP address or host name.' };
    if (!HOST_PATTERN.test(host)) return { field: 'host', message: 'Enter a plain IP address or host name, without scheme or path.' };
    const port = Number(device.port);
    // Port and network-id bounds mirror app/admin/api.py (`_normalize_devices`: 1..65535, and
    // `_network_id`: 0..MAX_NETWORK_ID); keep the two in step (JS-02).
    if (!Number.isInteger(port) || port < 1 || port > 65535) return { field: 'port', message: 'The port must be between 1 and 65535.' };
    const network = networkId(device.network_id);
    if (network !== null && (!Number.isInteger(network) || network < 0 || network > MAX_NETWORK_ID)) {
      return { field: 'network_id', message: `The network ID must be between 0 and ${MAX_NETWORK_ID}.` };
    }
    // Slaves share the master's endpoint, so the key is host, port and network ID together.
    const twin = settingsDraft.devices.find((other) => other !== device
      && String(other.host || '').trim().toLowerCase() === host.toLowerCase()
      && Number(other.port) === port
      && networkId(other.network_id) === network);
    return twin ? { field: 'host', message: 'This inverter address is already listed with the same network ID.' } : { field: '', message: '' };
  }

  // Client-only stable identity. Row DOM and handlers reference a device by uid, never by array
  // index, because the list is replaced by the server's answer after every add and delete.
  let deviceUid = 0;
  function withUid(device) { if (!device._uid) device._uid = `d${++deviceUid}`; return device; }
  // The list always mirrors the server state: there is no draft, each add or delete is saved at once.
  function adoptDevices(list) {
    settingsDraft.devices = (list || []).map(withUid);
    return settingsDraft.devices;
  }
  function deviceByUid(uid) { return (settingsDraft.devices || []).find((device) => device._uid === uid) || null; }

  // Explicit fields instead of a spread, so the payload stays self-documenting and independent of
  // the server dropping unknown per-device keys (_normalize_devices rebuilds from a whitelist).
  function devicesPayload(list) {
    return list.map((device) => ({
      host: String(device.host).trim(),
      port: Number(device.port),
      network_id: networkId(device.network_id),
      device_id: device.device_id || null,
      display_name: device.display_name || null,
    }));
  }

  const deviceUi = { busy: false };
  // Set on a successful add, read when the inverters modal closes: the on-close success toast the
  // operator asked for. Reset on open ('shown.bs.modal') so a later open starts clean.
  let inverterAddedSinceOpen = false;
  const deviceLabel = (entry) => `${String(entry.host).trim()}:${entry.port}`;
  const RESET_NOTE = 'its verification evidence, Engineering Mode and operating mode are reset and its connection is closed';

  // The one place that sends the device list: a full-list PUT that reconfigures the live
  // connections without a restart. Throws the api() error; the caller reports it inline.
  async function commitDevices(next) {
    const payload = { devices: devicesPayload(next) };
    const result = await api('settings', { method: 'PUT', body: JSON.stringify(payload) });
    settingsCommitted = result.settings || { ...settingsCommitted, ...payload };
    adoptDevices(structuredClone(settingsCommitted.devices || payload.devices));
    if (page === 'dashboard') {
      $('inverter-onboarding-hint').hidden = settingsDraft.devices.length !== 0;
      loadDashboard().catch((error) => toast(messageFrom(error), 'danger'));
      loadMetricCount();
    }
  }

  function deviceFailureMessage(error, action) {
    const reason = messageFrom(error);
    if (error?.status === 409) return `The change was rejected and rolled back (${reason}). The previous configuration is still active.`;
    if (error?.status === 504) return `${action} timed out (${reason}). Check the dashboard before retrying.`;
    return `${action} failed (${reason}).`;
  }

  // Outcome texts live in two persistent nodes (never rebuilt, so screen readers announce them):
  // #device-status for results, #device-error (role=alert) for problems.
  function setDeviceMessage(kind, text) {
    const error = document.getElementById('device-error');
    const status = document.getElementById('device-status');
    if (error) { error.textContent = kind === 'error' ? text : ''; error.hidden = kind !== 'error' || !text; }
    if (status) status.textContent = kind === 'status' ? text : '';
  }

  function setDeviceBusy(busy) {
    deviceUi.busy = busy;
    const editor = document.getElementById('device-editor');
    if (!editor) return;
    for (const control of editor.querySelectorAll('input, button')) control.disabled = busy;
    editor.querySelector('#device-add')?.setAttribute('aria-busy', String(busy));
  }

  function markDeviceField(field) {
    const form = document.getElementById('device-add-form');
    for (const input of form?.querySelectorAll('input[data-field]') || []) {
      const offending = input.dataset.field === field;
      input.classList.toggle('is-invalid', offending);
      if (offending) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
      const described = offending ? 'device-error' : (input.dataset.help || '');
      if (described) input.setAttribute('aria-describedby', described);
      else input.removeAttribute('aria-describedby');
    }
  }

  // `scope` is the editor itself while it is still detached (first render), else the document.
  function renderDeviceList(scope = document) {
    const list = scope.querySelector('#device-list');
    if (!list) return;
    const devices = settingsDraft.devices || [];
    list.replaceChildren(...devices.map((device) => deviceItem(device)));
    list.hidden = devices.length === 0; // an empty <ul> would still take a grid gap
    scope.querySelector('#device-empty').hidden = devices.length > 0;
  }

  // A saved inverter is shown, not edited: re-addressing would be a fourth function of the dialog.
  function deviceItem(device) {
    const row = element('li', 'device-settings-item');
    row.dataset.uid = device._uid;
    const grid = element('div', 'row g-2 align-items-center');
    const hostCol = element('div', 'col-12 col-sm');
    hostCol.append(element('span', 'device-saved-host text-break', String(device.host)));
    const portCol = element('div', 'col-6 col-sm-3', `Port ${device.port}`);
    const network = networkId(device.network_id);
    const networkCol = element('div', 'col-6 col-sm-3', network === null ? 'Direct connection' : `Network ID ${network}`);
    const removeCol = element('div', 'col-auto ms-auto');
    const remove = element('button', 'btn btn-outline-danger');
    remove.type = 'button';
    remove.title = 'Remove inverter';
    remove.setAttribute('aria-label', `Remove inverter ${deviceLabel(device)}`);
    const icon = element('span', 'material-icons', 'delete_outline');
    icon.setAttribute('aria-hidden', 'true');
    remove.append(icon);
    remove.addEventListener('click', () => { removeDevice(device._uid, remove).catch((error) => toast(messageFrom(error), 'danger')); });
    removeCol.append(remove);
    grid.append(hostCol, portCol, networkCol, removeCol);
    row.append(grid);
    return row;
  }

  // The dashboard never does the sensitive settings read on first paint (SEC-06), so the draft can
  // be empty when a card's trash icon is used. Load it once on demand before resolving the device.
  async function ensureDeviceDraft() {
    if (Array.isArray(settingsDraft.devices)) return;
    const result = await api('settings');
    settingsCommitted = structuredClone(result.settings || {});
    settingsDraft = structuredClone(settingsCommitted);
    runningInContainer = result.in_container === true;
    if (Object.hasOwn(settingsDraft, 'devices')) adoptDevices(settingsDraft.devices);
  }

  // The dashboard card's trash icon reuses the modal's delete flow. It maps the GET /devices card to
  // the draft inverter by its server-assigned id (device.id === draft.device_id) and then hands off
  // to removeDevice, so the confirm dialog, busy-guard, PUT /settings and refresh cannot drift.
  async function removeDashboardDevice(ref, button) {
    if (deviceUi.busy) return;
    await ensureDeviceDraft();
    const device = (settingsDraft.devices || []).find((entry) => entry.device_id === ref.removeId);
    if (!device) return;
    await removeDevice(device._uid, button);
  }

  // Deleting reconfigures the live connections and resets the inverter's evidence and modes, so it
  // asks first (the shared confirm dialog) and then saves at once.
  async function removeDevice(uid, button) {
    if (deviceUi.busy) return;
    const device = deviceByUid(uid);
    if (!device) return;
    const label = deviceLabel(device);
    const accepted = await confirmAction({
      title: 'Remove inverter?',
      message: `${label}: ${RESET_NOTE}.`,
      confirmLabel: 'Remove inverter',
      danger: true,
    });
    if (!accepted) { if (button.isConnected) button.focus(); return; }
    setDeviceMessage('status', '');
    setDeviceBusy(true);
    let failed = false;
    try {
      await commitDevices(settingsDraft.devices.filter((other) => other !== device));
      renderDeviceList();
      setDeviceMessage('status', `Inverter ${label} removed.`);
    } catch (error) {
      failed = true;
      setDeviceMessage('error', deviceFailureMessage(error, 'Removing the inverter'));
    } finally {
      setDeviceBusy(false);
    }
    const editor = document.getElementById('device-editor');
    if (!editor) return;
    const target = failed && button.isConnected ? button : editor.querySelector('.device-settings-item button') || editor.querySelector('#device-new-host');
    target?.focus();
  }

  // Validates the new row and saves the extended list at once. The input stays on any failure.
  async function addDevice(card) {
    if (deviceUi.busy) return;
    const form = card.querySelector('#device-add-form');
    const input = (field) => form.querySelector(`input[data-field="${field}"]`);
    const candidate = withUid({
      host: input('host').value.trim(),
      port: input('port').value === '' ? null : Number(input('port').value),
      network_id: networkId(input('network_id').value),
    });
    const problem = deviceProblem(candidate);
    markDeviceField(problem.field);
    if (problem.message) {
      setDeviceMessage('error', problem.message);
      input(problem.field).focus();
      return;
    }
    setDeviceMessage('error', '');
    setDeviceMessage('status', '');
    setDeviceBusy(true);
    let failed = false;
    try {
      await commitDevices([...settingsDraft.devices, candidate]);
      renderDeviceList();
      input('host').value = '';
      input('port').value = '8899';
      input('network_id').value = '';
      inverterAddedSinceOpen = true;
      setDeviceMessage('status', `Inverter ${deviceLabel(candidate)} added.`);
    } catch (error) {
      failed = true;
      setDeviceMessage('error', deviceFailureMessage(error, 'Adding the inverter'));
    } finally {
      setDeviceBusy(false);
    }
    (failed ? card.querySelector('#device-add') : input('host'))?.focus();
  }

  function renderDevicesSettings(card) {
    const host = element('div', 'device-settings');
    host.id = 'device-editor'; // the anchor the persistent devices alert is inserted before
    const list = element('ul', 'list-unstyled device-list mb-0');
    list.id = 'device-list';
    list.setAttribute('aria-label', 'Configured inverters');
    const empty = element('p', 'text-secondary small mb-0', 'No inverters configured yet.');
    empty.id = 'device-empty';
    const form = element('form', 'device-add');
    form.id = 'device-add-form';
    form.noValidate = true; // deviceProblem() reports the problem inline, not as a browser bubble
    const row = element('div', 'device-settings-item');
    const grid = element('div', 'row g-2 align-items-start');
    const hostCol = element('div', 'col-12 col-sm');
    const hostInput = element('input', 'form-control');
    hostInput.id = 'device-new-host';
    hostInput.dataset.field = 'host';
    hostInput.setAttribute('aria-label', 'IP address or host name');
    hostInput.placeholder = 'IP address or host name';
    hostInput.autocomplete = 'off';
    hostCol.append(hostInput);
    const portCol = element('div', 'col-6 col-sm-3');
    const portInput = element('input', 'form-control');
    portInput.id = 'device-new-port';
    portInput.dataset.field = 'port';
    portInput.type = 'number';
    portInput.min = '1'; portInput.max = '65535'; portInput.inputMode = 'numeric';
    portInput.setAttribute('aria-label', 'Port');
    portInput.value = '8899';
    portCol.append(portInput);
    const networkCol = element('div', 'col-6 col-sm-3');
    // Visually hidden keeps the row aligned with the unlabelled host and port inputs.
    const networkLabel = element('label', 'visually-hidden', 'Network ID (optional, empty means directly attached)');
    networkLabel.htmlFor = 'device-new-network-id';
    const networkInput = element('input', 'form-control');
    networkInput.id = 'device-new-network-id';
    networkInput.dataset.field = 'network_id';
    networkInput.type = 'number';
    networkInput.min = '0'; networkInput.max = String(MAX_NETWORK_ID); networkInput.inputMode = 'numeric';
    networkInput.placeholder = 'Network ID (optional)';
    networkInput.dataset.help = 'device-network-help';
    networkInput.setAttribute('aria-describedby', 'device-network-help');
    networkCol.append(networkLabel, networkInput);
    grid.append(hostCol, portCol, networkCol);
    const error = element('div', 'invalid-feedback d-block');
    error.id = 'device-error';
    error.setAttribute('role', 'alert');
    error.hidden = true;
    row.append(grid, error);
    const networkHelp = element('p', 'text-secondary small mt-2 mb-0', 'Network ID: leave empty for a direct connection. Only set it for an inverter reached through the master in the plant network.');
    networkHelp.id = 'device-network-help';
    const add = element('button', 'btn btn-primary mt-3', 'Add inverter');
    add.type = 'submit';
    add.id = 'device-add';
    const status = element('p', 'text-secondary small mt-2 mb-0');
    status.id = 'device-status';
    status.setAttribute('role', 'status');
    form.append(row, networkHelp, add, status);
    for (const input of [hostInput, portInput, networkInput]) {
      input.addEventListener('input', () => { markDeviceField(''); setDeviceMessage('error', ''); });
    }
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      addDevice(card).catch((failure) => toast(messageFrom(failure), 'danger'));
    });
    host.append(list, empty, form);
    card.append(host);
    renderDeviceList(host);
  }

  // The shared action bar of every explicit-apply form: the Apply button alone. Its native disabled
  // state tells assistive technology that there is nothing (valid) to apply; `prefix` namespaces the id.
