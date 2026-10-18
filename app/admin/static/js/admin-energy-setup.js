//
// app/admin/static/js/admin-energy-setup.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

// Watts-hours to the kWh text of an input; empty when unset.
function formatKwh(wh) {
  return Number.isFinite(wh) ? String(Number((wh / 1000).toFixed(3))) : '';
}

// Starting verification consents to the bounded hold and automatic save. The inverter's
// readings establish each direction whenever possible; only unclear readings need an answer.
function openVerificationAssistant({ id, deviceName, onDone, opener }) {
  const base = `energy/devices/${encodeURIComponent(id)}/verification-assistant`;
  const post = (action, body) => api(`${base}/${action}`, { method: 'POST', body: JSON.stringify(body || {}) });
  const uid = `energy-assistant-${++deviceCardCount}`;
  let state = null;
  let running = false;
  let problem = null;
  let finished = false;
  let testAttempted = false;
  let saveAttempted = false;
  const POLL_MS = 3000;
  let pollTimer = null;
  let epoch = 0;
  let renderedStage = -1;
  let liveNode = null;
  let renderedKey = '';
  const assistantError = (error) => error?.status === 404
    ? 'The verification service is unavailable. Reload this page and start the check again. The inverter may still be online.'
    : messageFrom(error);
  const power = (value) => (Number.isFinite(value) ? `${Math.round(Math.abs(value)).toLocaleString(undefined)} W` : 'unknown');

  const modalEl = element('div', 'modal fade energy-assistant');
  modalEl.tabIndex = -1;
  modalEl.setAttribute('aria-labelledby', `${uid}-title`);
  modalEl.dataset.bsBackdrop = 'static';
  modalEl.dataset.bsKeyboard = 'false';
  const content = element('div', 'modal-content');
  modalEl.append(element('div', 'modal-dialog modal-dialog-centered modal-dialog-scrollable'));
  modalEl.firstChild.append(content);
  const header = element('div', 'modal-header');
  const title = element('h2', 'modal-title h5', `Verify hardware: ${deviceName}`);
  title.id = `${uid}-title`;
  header.append(title);
  const body = element('div', 'modal-body');
  body.setAttribute('aria-live', 'polite');
  const footer = element('div', 'modal-footer');
  content.append(header, body, footer);
  document.body.append(modalEl);
  const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);

  const stageOf = () => {
    if (!state || !state.started || state.blockers.length) return 0;
    const [battery, grid, test] = state.steps;
    if (battery.status !== 'done') return 1;
    if (test.status !== 'done') return 3;
    if (grid.status !== 'done') return 2;
    return 4;
  };
  const button = (label, className, onClick) => {
    const node = element('button', `btn ${className}`, label);
    node.type = 'button';
    node.addEventListener('click', onClick);
    return node;
  };
  const guarded = (task) => async () => {
    problem = null;
    running = true;
    epoch += 1;
    render();
    try { await task(); } catch (error) { problem = assistantError(error); } finally { running = false; render(); }
  };
  const note = (text, kind) => { const n = element('div', `alert alert-${kind} small`, text); n.setAttribute('role', kind === 'danger' ? 'alert' : 'status'); return n; };
  // 'wait' while the quantity is (almost) zero or unreadable, 'ask' when it moves but the balance
  // could not prove the direction. Nothing is stored while waiting.
  const KINDS = [
    { kind: 'battery', subject: 'battery', field: 'battery_power_w' },
    { kind: 'grid', subject: 'grid', field: 'grid_power_w' },
  ];
  const directionMode = (index) => {
    const value = state && state.readings ? state.readings[KINDS[index].field] : null;
    return Number.isFinite(value) && Math.abs(value) >= state.min_direction_w ? 'ask' : 'wait';
  };
  const liveText = (index) => {
    const value = state && state.readings ? state.readings[KINDS[index].field] : null;
    return Number.isFinite(value) ? `Checking automatically · about ${power(value)}.` : 'Checking automatically · waiting for a reading.';
  };
  const choice = (index) => {
    const kind = KINDS[index].kind;
    const options = index === 0
      ? [['discharging', 'The battery is discharging (supplying the house)'], ['charging', 'The battery is charging (taking power in)'], ['idle', 'The battery is idle (neither charging nor discharging)']]
      : [['importing', 'The house is taking power from the grid (import)'], ['exporting', 'The house is feeding power into the grid (export)'], ['idle', 'No power flows to or from the grid (idle)']];
    const wrap = element('div', 'd-grid gap-2');
    wrap.append(element('p', 'mb-1', index === 0 ? 'Right now, the battery is:' : 'Right now, the house is:'));
    for (const [answer, label] of options) wrap.append(button(label, 'btn-primary', guarded(async () => { state = await post('direction', { kind, answer }); })));
    return wrap;
  };
  const waiting = (index) => {
    const box = element('div', 'alert alert-info small energy-assistant-waiting');
    box.setAttribute('role', 'status');
    const spin = element('span', 'spinner-border spinner-border-sm me-2'); spin.setAttribute('aria-hidden', 'true');
    liveNode = element('span', null, liveText(index));
    const line = element('p', 'mb-0'); line.append(spin, liveNode);
    box.append(line);
    return box;
  };
  const viewKey = () => {
    if (!state) return '';
    const parts = state.steps.map((step) => `${step.status}|${step.method}|${step.message}`);
    const stage = stageOf();
    return [stage, ...(stage === 1 || stage === 2 ? [directionMode(stage - 1)] : []), ...parts].join('#');
  };
  const save = guarded(async () => {
    const status = await post('commit', { confirm: true });
    finished = true;
    running = false; // hide.bs.modal refuses to close while a step runs
    onDone(status);
    modal.hide();
    toast('Hardware verified. Manual battery control is available.');
  });
  const runTest = guarded(async () => { state = await post('control-test', { confirm: true }); });
  const schedulePoll = () => {
    clearTimeout(pollTimer);
    pollTimer = null;
    const stage = stageOf();
    if (running || finished || problem) return;
    if (stage === 4 && !saveAttempted) {
      saveAttempted = true;
      pollTimer = setTimeout(save, 0);
    } else if (stage === 3 && !testAttempted && Number.isFinite(state.readings?.battery_power_w)
      && Math.abs(state.readings.battery_power_w) >= state.min_baseline_w) {
      testAttempted = true;
      pollTimer = setTimeout(runTest, 0);
    } else if (stage === 1 || stage === 2 || (stage === 3 && !testAttempted)) {
      pollTimer = setTimeout(poll, POLL_MS);
    }
  };
  async function poll() {
    pollTimer = null;
    if (running || finished || !modalEl.isConnected) return;
    const mine = epoch;
    try {
      const next = await post('auto-check');
      if (mine !== epoch || running || !modalEl.isConnected) return;
      state = next;
      if (viewKey() !== renderedKey) render();
      else { if (liveNode) liveNode.textContent = liveText(stageOf() === 3 ? 0 : stageOf() - 1); schedulePoll(); }
    } catch (error) {
      if (mine !== epoch || !modalEl.isConnected) return;
      if (error?.status === 409) {
        state = null;
        testAttempted = false;
        saveAttempted = false;
        problem = 'The check timed out. Start verification again when power is flowing.';
        render();
        return;
      }
      problem = assistantError(error);
      render();
    }
  }

  function render() {
    const stage = stageOf();
    liveNode = null;
    const main = element('div');
    const heading = (text) => { const h = element('h3', 'h6', text); h.tabIndex = -1; return h; };
    const stepMessage = (index) => (state && state.steps[index].message ? note(state.steps[index].message, 'warning') : null);
    const footerButtons = [];
    const cancel = button(finished ? 'Close' : 'Cancel', 'btn-outline-secondary', () => modal.hide());
    cancel.disabled = running;
    if (state && state.blockers.length) {
      main.append(heading('Verification cannot start'), element('p', 'small', 'Resolve these items, then retry:'));
      const list = element('ul', 'small');
      for (const text of state.blockers) list.append(element('li', null, text));
      main.append(list);
      footerButtons.push(button('Retry', 'btn-primary', guarded(async () => {
        state = await post('start');
        if (state.started && !state.blockers.length) state = await post('auto-check');
      })));
    } else if (stage === 0 && !(state && state.started)) {
      main.append(heading('Automatic verification'),
        element('p', 'small', 'Reads live values, briefly holds the battery at 0 W for up to 30 seconds, restores it and saves the result. The house may briefly draw power from the grid. Watch the first charge or discharge: this test only checks 0 W.'));
      footerButtons.push(button('Start verification', 'btn-primary', guarded(async () => {
        state = await post('start');
        if (state.started && !state.blockers.length) state = await post('auto-check');
      })));
    } else if (stage === 1 || stage === 2) {
      const index = stage - 1;
      const asking = directionMode(index) === 'ask';
      const titles = [['Battery direction?', 'Waiting for battery power'], ['Grid direction?', 'Waiting for grid power']];
      main.append(heading(titles[index][asking ? 0 : 1]), stepMessage(index) || '');
      if (asking) {
        main.append(element('p', 'small', 'The readings are unclear. Check the inverter display or app:'), choice(index));
      } else {
        if (stage === 2) main.append(element('p', 'small mb-2', 'Battery test passed. Waiting for measurable grid power.'));
        main.append(waiting(index));
      }
    } else if (stage === 3) {
      const ready = Number.isFinite(state.readings?.battery_power_w)
        && Math.abs(state.readings.battery_power_w) >= state.min_baseline_w;
      main.append(heading(testAttempted || ready ? 'Testing battery control' : 'Waiting for battery power'), stepMessage(2) || '');
      if (!testAttempted) main.append(waiting(0));
      if (testAttempted && state.steps[2].status === 'failed') footerButtons.push(button('Retry test', 'btn-primary', runTest));
    } else {
      main.append(heading('Saving verification'));
      if (problem) footerButtons.push(button('Retry save', 'btn-primary', () => { problem = null; save(); }));
    }
    if (running) {
      const progress = element('p', 'small text-secondary d-flex align-items-center gap-2 mb-0 energy-assistant-progress');
      const spinner = element('span', 'spinner-border spinner-border-sm');
      spinner.setAttribute('aria-hidden', 'true');
      progress.append(spinner, 'Checking…');
      main.append(progress);
    }
    if (problem) main.append(note(problem, 'danger'));
    if (problem && stage === 3 && testAttempted && !footerButtons.length)
      footerButtons.push(button('Retry test', 'btn-primary', () => { problem = null; runTest(); }));
    if (problem && (stage === 1 || stage === 2 || (stage === 3 && !testAttempted)) && !footerButtons.length)
      footerButtons.push(button('Retry', 'btn-primary', guarded(async () => { state = await post('auto-check'); })));
    for (const node of footerButtons) node.disabled = node.disabled || running;
    body.replaceChildren(main);
    footer.replaceChildren(cancel, ...footerButtons);
    renderedKey = viewKey();
    if (stage !== renderedStage) { renderedStage = stage; main.querySelector('h3')?.focus(); }
    schedulePoll();
  }

  modalEl.addEventListener('hide.bs.modal', (event) => { if (running) event.preventDefault(); });
  modalEl.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !running) modal.hide(); });
  modalEl.addEventListener('shown.bs.modal', () => body.querySelector('h3')?.focus());
  modalEl.addEventListener('hidden.bs.modal', () => {
    clearTimeout(pollTimer);
    epoch += 1;
    if (!finished) api(base, { method: 'DELETE' }).catch(() => { });
    modal.dispose();
    modalEl.remove();
    opener?.focus();
  });
  render();
  modal.show();
}

// Expert mode is a page-wide display switch, never persisted and never a backend setting. The
// required setup steps (write access, power limits, hardware verification) live in the guided
// Setup block; this function builds their editors plus the Expert-only section. The Expert section
// is attached to the page only while Expert mode is on; it holds the full hardware verification,
// engineering mode, the SoC-target policy and Diagnostics (the ONLY place raw reject_detail,
// capability names and write approvals appear). Raw register names are fine on this admin surface.
function energyAdvanced(id, getDevice, onChange) {
  const path = `energy/devices/${encodeURIComponent(id)}`;

  const expert = element('section', 'energy-expert mt-3');
  expert.append(element('h3', 'h5 mb-1', 'Expert settings'));
  expert.append(element('p', 'small text-warning-emphasis energy-expert-warning',
    'Advanced hardware settings. Incorrect values can prevent battery control from working correctly.'));
  const expertInner = element('div', 'energy-advanced-inner');
  expert.append(expertInner);

  const diagnostics = element('details', 'energy-diagnostics energy-advanced-section');
  diagnostics.append(element('summary', 'small', 'Diagnostics…'));
  const diagInner = element('div', 'energy-advanced-inner');
  diagnostics.append(diagInner);

  const field = (labelText, input, extra) => {
    const col = element('div', extra || 'col-auto');
    const label = element('label', 'form-label small mb-1', labelText);
    input.id = `energy-${id}-${labelText.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
    label.htmlFor = input.id;
    col.append(label, input);
    return col;
  };
  const numberInput = (min, max, step) => {
    const input = element('input', 'form-control form-control-sm');
    input.type = 'number';
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    return input;
  };
  const textInput = (maxLength) => {
    const input = element('input', 'form-control form-control-sm');
    input.type = 'text';
    input.maxLength = maxLength;
    return input;
  };
  const checkbox = (labelText) => {
    const wrap = element('div', 'form-check');
    const input = element('input', 'form-check-input');
    input.type = 'checkbox';
    input.id = `energy-${id}-${labelText.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
    const label = element('label', 'form-check-label small', labelText);
    label.htmlFor = input.id;
    wrap.append(input, label);
    return { wrap, input };
  };
  const section = (parent, heading, hint) => {
    const node = element('div', 'energy-advanced-section');
    node.append(element('h3', 'h6', heading));
    if (hint) node.append(element('p', 'small text-secondary', hint));
    parent.append(node);
    return node;
  };
  // While a save runs, the panel must not move forms or swap the Setup step under it.
  let saving = 0;
  const dirty = { verify: false, limits: false, engineering: false, policy: false };
  // Every manual edit on a form bumps its revision; a save only clears that form's dirty flag if
  // the revision did not change while the request was in flight, so a completing old request can
  // never discard input the user typed afterwards.
  const rev = { verify: 0, limits: 0, engineering: 0, policy: 0 };
  // All four setup/expert submit buttons are collected once they are created, so a single save can
  // lock the whole panel: concurrent saves across forms issue the same full-state PUT and would
  // otherwise clobber each other.
  const guardedButtons = [];
  const guarded = (button, task, formKey) => async (event) => {
    event.preventDefault();
    // One save at a time per device panel. A second submit on any form while a save is in flight
    // is refused rather than firing a competing full-state write.
    if (saving > 0) return;
    const startRev = formKey ? rev[formKey] : null;
    for (const node of guardedButtons) node.disabled = true;
    saving += 1;
    try {
      await task();
      if (formKey && rev[formKey] === startRev) dirty[formKey] = false;
    } catch (error) {
      toast(messageFrom(error), 'danger');
      return;
    } finally { saving -= 1; for (const node of guardedButtons) node.disabled = false; }
    // The save succeeded; a failed refresh is not an error, the next poll catches up.
    try { await onChange(await api('energy/devices').then((list) => list.find((item) => item.device_id === id) || getDevice())); }
    catch { /* next poll refreshes */ }
  };
  const markInvalid = (input, message) => {
    const col = input.parentNode;
    let note = col.querySelector('.invalid-feedback');
    if (!note) {
      note = element('div', 'invalid-feedback');
      note.id = `${input.id}-error`;
      col.append(note);
    }
    note.textContent = message;
    input.classList.add('is-invalid');
    input.setAttribute('aria-invalid', 'true');
    const previousDescribedBy = input.getAttribute('aria-describedby');
    input.setAttribute('aria-describedby', note.id);
    input.focus();
    input.addEventListener('change', () => {
      input.classList.remove('is-invalid');
      input.removeAttribute('aria-invalid');
      note.textContent = '';
      if (previousDescribedBy && previousDescribedBy !== note.id) input.setAttribute('aria-describedby', previousDescribedBy);
      else input.removeAttribute('aria-describedby');
    }, { once: true });
    throw new Error(message);
  };

  // 1. Gate detail (Diagnostics)
  const gateSection = section(diagInner, 'Gate detail', 'Raw decision per action; unverified capability names are listed as the dispatch layer reports them.');
  const gateHost = element('div', 'table-responsive');
  gateHost.tabIndex = 0;
  // role gives the aria-label a name to attach to on a focusable scroll container (A11Y-04).
  gateHost.setAttribute('role', 'region');
  gateHost.setAttribute('aria-label', 'Gate detail');
  gateSection.append(gateHost);

  // 2. Capabilities and sign conventions (Diagnostics)
  const capSection = section(diagInner, 'Hardware capabilities and sign conventions',
    'Charge, hold and discharge need all three capabilities verified; otherwise only the time-limited engineering mode releases them.');
  const capHost = element('div', 'table-responsive');
  capHost.tabIndex = 0;
  capHost.setAttribute('role', 'region');
  capHost.setAttribute('aria-label', 'Hardware capabilities');
  capSection.append(capHost);

  // 2b. Write approvals and freshness (Diagnostics)
  const writeSection = section(diagInner, 'Write approvals',
    'The live approved register set and what mode changes contributed.');
  const writeApproved = element('p', 'small mb-1');
  const writeAdded = element('p', 'small mb-0 text-secondary');
  writeSection.append(writeApproved, writeAdded);

  const freshSection = section(diagInner, 'Freshness', 'Per-reading age and staleness, and the last poll time.');
  const freshHost = element('div', 'table-responsive');
  freshHost.tabIndex = 0;
  freshHost.setAttribute('role', 'region');
  freshHost.setAttribute('aria-label', 'Reading freshness');
  const pollStamp = element('p', 'small text-secondary mb-0', 'Updated –');
  freshSection.append(freshHost, pollStamp);

  // 3. Hardware verification: one form that lives in the Expert section only. The Setup step of a
  // Basic user just points to it (strategy code and byte widths are protocol detail), and never
  // switches Expert mode on by itself. Never prefilled with guessed hardware values.
  const verifySection = section(expertInner, 'Hardware verification',
    'Enter what you measured on this inverter. The server stamps who verified it and when; a verification applies to this device only.');
  const verifyExpertSlot = element('div');
  const verifyForm = element('form', 'row g-2 align-items-end');
  const model = textInput(128);
  const firmware = textInput(64);
  const code = numberInput(0, 255, 1);
  const enumWidth = numberInput(1, 4, 1);
  const boolWidth = numberInput(1, 4, 1);
  const batterySign = element('select', 'form-select form-select-sm');
  for (const [value, text] of [['', 'Select…'], ['true', 'Positive value discharges the battery'], ['false', 'Positive value charges the battery']]) {
    const option = element('option', null, text);
    option.value = value;
    batterySign.append(option);
  }
  const gridSign = element('select', 'form-select form-select-sm');
  for (const [value, text] of [['', 'Select…'], ['true', 'Positive value is grid import'], ['false', 'Positive value is grid export']]) {
    const option = element('option', null, text);
    option.value = value;
    gridSign.append(option);
  }
  const note = textInput(200);
  const frame = checkbox('Write frame layout verified');
  const sequence = checkbox('Apply sequence verified');
  const attest = checkbox('I verified these values on the hardware');
  const verifyButton = element('button', 'btn btn-sm btn-primary', 'Mark hardware as verified');
  verifyButton.type = 'submit';
  const revokeButton = element('button', 'btn btn-sm btn-outline-secondary', 'Revoke verification');
  revokeButton.type = 'button';
  const checks = element('div', 'col-12');
  checks.append(frame.wrap, sequence.wrap, attest.wrap);
  const buttonRow = element('div', 'col-12 d-flex gap-2');
  buttonRow.append(verifyButton);
  verifyForm.append(
    field('Device model', model, 'col-sm-6 col-lg-3'), field('Firmware', firmware, 'col-sm-6 col-lg-3'),
    field('Strategy code', code), field('Enum byte width', enumWidth), field('Bool byte width', boolWidth),
    field('Battery power sign', batterySign, 'col-sm-6'), field('Grid power sign', gridSign, 'col-sm-6'),
    field('Evidence note', note, 'col-12'), checks, buttonRow,
  );
  verifyExpertSlot.append(verifyForm);
  verifySection.append(verifyExpertSlot, revokeButton);

  verifyForm.addEventListener('submit', guarded(verifyButton, async () => {
    if (!attest.input.checked) throw new Error('Confirm that the values were verified on the hardware.');
    if (!model.value.trim() || !firmware.value.trim()) throw new Error('Device model and firmware are required.');
    if (!note.value.trim()) throw new Error('An evidence note is required.');
    if (!frame.input.checked || !sequence.input.checked) throw new Error('Confirm the write frame layout and the apply sequence.');
    if (batterySign.value === '') markInvalid(batterySign, 'Select the battery power sign.');
    if (gridSign.value === '') markInvalid(gridSign, 'Select the grid power sign.');
    const numbers = [code, enumWidth, boolWidth];
    if (numbers.some((input) => input.value === '' || !input.checkValidity())) {
      throw new Error('Strategy code (0-255) and both byte widths (1-4) are required.');
    }
    await api(`${path}/hardware-verification`, {
      method: 'PUT',
      body: JSON.stringify({
        verified_device_model: model.value.trim(), verified_firmware: firmware.value.trim(), note: note.value.trim(),
        soc_strategy_external_code: Number(code.value), enum_byte_width: Number(enumWidth.value),
        bool_byte_width: Number(boolWidth.value), write_frame_layout_verified: frame.input.checked,
        apply_sequence_verified: sequence.input.checked, battery_discharge_positive: batterySign.value === 'true',
        grid_import_positive: gridSign.value === 'true',
      }),
    });
    attest.input.checked = false;
    toast('Hardware verification saved.');
  }, 'verify'));
  revokeButton.addEventListener('click', guarded(revokeButton, async () => {
    // Server side: status only, the recorded evidence stays.
    await api(`${path}/hardware-verification`, { method: 'DELETE' });
    toast('Verification revoked.');
  }));

  // 4. Power limits (Setup step or Expert section). Entered in kW, converted to watts on submit.
  // The saved request always carries the existing engineering_mode so Basic Setup never changes it.
  const limitSection = section(expertInner, 'Power limits');
  const limitExpertSlot = element('div');
  limitSection.append(limitExpertSlot);
  const limitForm = element('form', 'row g-2 align-items-end');
  // Step 0.001 from min 0.001, so whole-watt values like 3.00 kW validate (a 0.01 step rejected them).
  const maxCharge = numberInput(0.001, 50, 0.001);
  const maxDischarge = numberInput(0.001, 50, 0.001);
  // Optional and planner-only: it never gates manual control. An empty field keeps the stored value.
  const capacity = numberInput(0.1, 1000, 0.1);
  const capacityField = field('Usable battery capacity (kWh, expert override)', capacity);
  capacityField.hidden = true;
  const limitButton = element('button', 'btn btn-primary', 'Save and continue');
  limitButton.type = 'submit';
  const limitButtonCol = element('div', 'col-auto');
  limitButtonCol.append(limitButton);
  limitForm.append(field('Maximum charging power (kW)', maxCharge), field('Maximum discharging power (kW)', maxDischarge),
    capacityField, limitButtonCol);
  const putLimits = (chargeW, dischargeW, engineeringMode, capacityWh) => api(`dispatch/devices/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: JSON.stringify({
      max_charge_power_w: chargeW, max_discharge_power_w: dischargeW, engineering_mode: engineeringMode,
      ...(capacityWh === undefined ? {} : { usable_capacity_wh: capacityWh }), // omitted keeps the stored value
    }),
  });
  limitForm.addEventListener('submit', guarded(limitButton, async () => {
    if ([maxCharge, maxDischarge].some((input) => input.value === '' || !input.checkValidity())) {
      throw new Error('Enter both maximum powers in kW.');
    }
    const capacityOverrideEnabled = !capacityField.hidden;
    if (capacityOverrideEnabled && capacity.value !== '' && !capacity.checkValidity()) {
      throw new Error('Enter the usable capacity in kWh (0.1 to 1000).');
    }
    const current = getDevice().limits;
    await putLimits(Math.round(Number(maxCharge.value) * 1000), Math.round(Number(maxDischarge.value) * 1000),
      Boolean(current && current.engineering_mode), !capacityOverrideEnabled || capacity.value === ''
      ? undefined : Math.round(Number(capacity.value) * 1000));
    toast('Limits saved.');
  }, 'limits'));

  // 4b. Engineering mode (Expert only); keeps the saved power limits untouched.
  const engineeringSection = section(expertInner, 'Engineering mode',
    'Engineering mode releases unverified hardware for short, time-capped commands. It is not needed once the hardware is verified.');
  const engineeringForm = element('form', 'row g-2 align-items-end');
  const engineering = checkbox('Engineering mode');
  const engineeringButton = element('button', 'btn btn-sm btn-outline-primary', 'Save engineering mode');
  engineeringButton.type = 'submit';
  const engineeringCol = element('div', 'col-auto');
  engineeringCol.append(engineering.wrap);
  const engineeringButtonCol = element('div', 'col-auto');
  engineeringButtonCol.append(engineeringButton);
  engineeringForm.append(engineeringCol, engineeringButtonCol);
  engineeringSection.append(engineeringForm);
  engineeringForm.addEventListener('submit', guarded(engineeringButton, async () => {
    const current = getDevice().limits;
    if (!current) throw new Error('Set the power limits first.');
    await putLimits(current.max_charge_power_w, current.max_discharge_power_w, engineering.input.checked);
    toast('Engineering mode saved.');
  }, 'engineering'));

  // 5. SoC target policy (Expert)
  const policySection = section(expertInner, 'SoC target policy',
    'How the business target is translated into the inverter\'s own SoC target. "Below current SoC" is an unverified hypothesis.');
  const policyForm = element('form', 'row g-2 align-items-end');
  const policyMode = element('select', 'form-select form-select-sm');
  for (const [value, text] of ENERGY_POLICY_MODES) {
    const option = element('option', null, text);
    option.value = value;
    policyMode.append(option);
  }
  const policyMargin = numberInput(0, 50, 0.1);
  const policyButton = element('button', 'btn btn-sm btn-outline-primary', 'Save policy');
  policyButton.type = 'submit';
  const policyButtonCol = element('div', 'col-auto');
  policyButtonCol.append(policyButton);
  policyForm.append(field('Policy', policyMode), field('Margin %', policyMargin), policyButtonCol);
  policySection.append(policyForm);
  policyForm.addEventListener('submit', guarded(policyButton, async () => {
    // An empty field reads as Number('') === 0, which is a valid-looking margin; reject it (and any
    // out-of-range value) before building the body so clearing the field cannot silently send 0.
    if (policyMargin.value === '' || !policyMargin.checkValidity()) {
      throw new Error('Enter a margin between 0 and 50 %.');
    }
    const policy = getDevice().soc_target_policy;
    await api(`${path}/soc-target-policy`, {
      method: 'PUT',
      body: JSON.stringify({ mode: policyMode.value, below_margin_percent: Number(policyMargin.value), note: policy.note || null }),
    });
    toast('SoC target policy saved.');
  }, 'policy'));

  // The single in-flight save disables every submit control on the panel for the duration.
  guardedButtons.push(verifyButton, revokeButton, limitButton, engineeringButton, policyButton);

  expertInner.append(diagnostics);

  const simpleTable = (headers, rows) => {
    const table = element('table', 'table table-sm table-borderless mb-0');
    const headRow = element('tr');
    for (const label of headers) headRow.append(element('th', 'text-secondary small fw-normal', label));
    const thead = element('thead');
    thead.append(headRow);
    table.append(thead);
    const bodyEl = element('tbody');
    for (const cells of rows) {
      const row = element('tr');
      for (const [text, className] of cells) row.append(element('td', `small ${className || ''}`, text));
      bodyEl.append(row);
    }
    table.append(bodyEl);
    return table;
  };

  // Guided Setup steps. No register names here; the forms above are moved into their step.
  const inverterLink = (text, href) => {
    const link = element('a', 'btn btn-primary energy-step-link', text);
    link.href = href;
    return link;
  };
  const stepNode = (heading, text, ...rest) => {
    const node = element('div', 'energy-setup-step-body');
    node.append(element('h4', 'h6 mb-1', heading), element('p', 'small text-secondary', text), ...rest);
    return node;
  };
  const limitSetupSlot = element('div');
  const steps = {
    connection: stepNode('Inverter connection', 'The inverter is not connected. Check its connection settings.', inverterLink('Manage inverters', '/ui/dashboard?manage=inverters')),
    write_access: stepNode('Write access', '', inverterLink('Open Inverters page', '/ui/inverters')),
    limits: stepNode('Power limits', 'The most power the battery may be charged and discharged with, in kW.', limitSetupSlot),
    hardware: stepNode('Hardware verification', ''),
  };
  // Names what is missing; the global write switch is not part of the device payload, so it is
  // only reported when the poll answered 503 (write support disabled).
  const writeAccessText = steps.write_access.querySelector('p');
  // The values come from a real hardware and firmware verification, so the form is Expert-only; the
  // Basic step says so and never guesses or pre-fills them.
  const hardwareText = steps.hardware.querySelector('p');
  // Opens the guided check on an explicit click only; nothing is written by the setup step itself.
  const openVerifyButton = element('button', 'btn btn-primary energy-step-link', 'Verify hardware');
  openVerifyButton.type = 'button';
  hardwareText.after(openVerifyButton);
  openVerifyButton.addEventListener('click', () => {
    openVerificationAssistant({ id, deviceName: getDevice().device_name, onDone: onChange, opener: openVerifyButton });
  });
  const renderHardwareText = (expertOn) => {
    hardwareText.textContent = expertOn
      ? 'Verify this inverter to unlock manual control. Use the guided check or enter measured values below.'
      : 'Verify this inverter to unlock manual control. The check runs automatically and asks only when a measurement is unclear.';
    openVerifyButton.hidden = false;
  };
  // Plain wording first; the raw register names stay one click away for people who need them.
  const technicalNames = element('details', 'small mb-2');
  technicalNames.append(element('summary', null, 'Show technical names'), element('code', 'd-block text-break'));
  writeAccessText.after(technicalNames);
  function renderWriteAccessText(device, writeSupportOff) {
    const missing = missingRequiredWrites(device);
    const parts = [];
    if (writeSupportOff || device.write_support_enabled === false) parts.push('Write access is switched off. Turn on "Write access" on the Inverters page.');
    if (missing.length) {
      parts.push('Manual battery control needs the battery power control registers approved on the Inverters page (under "Writable parameters").');
    } else if (!writeSupportOff && device.write_support_enabled !== false) {
      parts.push('Write access is required for manual battery control.');
    }
    writeAccessText.textContent = parts.join(' ');
    technicalNames.hidden = !missing.length;
    technicalNames.querySelector('code').textContent = missing.join(', ');
  }
  const place = (node, slot) => { if (node.parentNode !== slot) slot.append(node); };
  function layout(firstUnmet) {
    place(limitForm, firstUnmet === 'limits' ? limitSetupSlot : limitExpertSlot);
    limitButton.textContent = firstUnmet === 'limits' ? 'Save and continue' : 'Save limits';
  }

  // A form is refilled from the server only while the user has not edited it, so a poll never
  // overwrites typing. Evidence checkboxes and the attestation are never prefilled.
  for (const [key, form] of Object.entries({ verify: verifyForm, limits: limitForm, engineering: engineeringForm, policy: policyForm })) {
    const mark = () => { dirty[key] = true; rev[key] += 1; };
    form.addEventListener('input', mark);
    form.addEventListener('change', mark);
  }
  function refill(device) {
    if (!dirty.verify) {
      const caps = Object.fromEntries(device.capabilities.map((item) => [item.name, item]));
      const isVerified = (name) => caps[name] && caps[name].status === 'verified';
      const write = isVerified('write_path_convention') ? caps.write_path_convention : null;
      model.value = (write && write.verified_device_model) || '';
      firmware.value = (write && write.verified_firmware) || '';
      code.value = write ? (write.soc_strategy_external_code ?? '') : '';
      enumWidth.value = write ? (write.enum_byte_width ?? '') : '';
      boolWidth.value = write ? (write.bool_byte_width ?? '') : '';
      note.value = (write && write.note) || '';
      batterySign.value = isVerified('battery_power_sign_convention') ? String(caps.battery_power_sign_convention.battery_discharge_positive) : '';
      gridSign.value = isVerified('grid_power_sign_convention') ? String(caps.grid_power_sign_convention.grid_import_positive) : '';
    }
    if (!dirty.limits) {
      maxCharge.value = device.limits ? (device.limits.max_charge_power_w / 1000).toFixed(device.limits.max_charge_power_w % 10 === 0 ? 2 : 3) : '';
      maxDischarge.value = device.limits ? (device.limits.max_discharge_power_w / 1000).toFixed(device.limits.max_discharge_power_w % 10 === 0 ? 2 : 3) : '';
    }
    if (!dirty.limits) capacity.value = device.limits ? formatKwh(device.limits.usable_capacity_wh) : '';
    if (!dirty.engineering) engineering.input.checked = Boolean(device.limits && device.limits.engineering_mode);
    if (!dirty.policy) {
      policyMode.value = device.soc_target_policy.mode;
      policyMargin.value = device.soc_target_policy.below_margin_percent;
    }
  }

  function update(device, expertOn, writeSupportOff = false) {
    capacityField.hidden = !expertOn;
    revokeButton.hidden = !energyChecklist(device).hardwareVerified;
    renderWriteAccessText(device, writeSupportOff);
    renderHardwareText(expertOn);
    refill(device);
    if (!expertOn) return; // the Expert section is not on the page; build its tables when it is
    gateHost.replaceChildren(simpleTable(
      ['Action', 'Allowed', 'Engineering mode', 'Unverified', 'Reject detail'],
      device.gates.map((gate) => [
        [ENERGY_ACTION_LABELS[gate.action] || gate.action], [gate.allowed ? 'Yes' : 'No', gate.allowed ? 'text-success' : 'text-danger'],
        [gate.engineering_mode ? 'Yes' : 'No'], [gate.unverified.join(', ') || '–'], [gate.reject_detail || '–'],
      ]),
    ));
    capHost.replaceChildren(simpleTable(
      ['Capability', 'Status', 'Model / firmware', 'Sign convention'],
      device.capabilities.map((item) => [
        [item.name], [item.status, item.status === 'verified' ? 'text-success' : 'text-danger'],
        [item.verified_device_model ? `${item.verified_device_model} / ${item.verified_firmware || '–'}` : '–'],
        [item.name === 'battery_power_sign_convention' ? (item.battery_discharge_positive ? 'positive = discharging' : 'positive = charging')
          : item.name === 'grid_power_sign_convention' ? (item.grid_import_positive ? 'positive = import' : 'positive = export') : '–'],
      ]),
    ));
    writeApproved.textContent = `Approved writes: ${(device.approved_write_names || []).join(', ') || '–'}`;
    writeAdded.textContent = `Added by mode changes: ${(device.added_write_names || []).join(', ') || '–'}`;
    freshHost.replaceChildren(simpleTable(
      ['Reading', 'Age (s)', 'Stale'],
      Object.entries(device.readings || {}).map(([name, reading]) => [
        [name], [Number.isFinite(reading && reading.age_seconds) ? Math.round(reading.age_seconds) : NOT_AVAILABLE],
        [reading && reading.stale ? 'Yes' : 'No', reading && reading.stale ? 'text-danger' : ''],
      ]),
    ));
  }

  // Relocated poll timestamp (design §13): fed from pollEnergy, shown only in Diagnostics.
  function setTimestamp(text) { pollStamp.textContent = text; }

  return { expert, update, setTimestamp, layout, busy: () => saving > 0, step: (name) => steps[name] || null };
}

// Shows one status paragraph in the list while there are no panels; null removes it.
// `action` adds a link button to the banner; `kind` is the Bootstrap alert colour.
function setEnergyNote(host, text, { kind = 'secondary', action = null } = {}) {
  if (!text) { energyNote?.remove(); energyNote = null; return; }
  if (!energyNote) energyNote = element('div', 'alert mb-0 d-flex flex-wrap align-items-center justify-content-between gap-2');
  energyNote.className = `alert alert-${kind} mb-0 d-flex flex-wrap align-items-center justify-content-between gap-2`;
  energyNote.setAttribute('role', 'status');
  const label = element('span', null, text);
  if (action) {
    const link = element('a', 'btn btn-primary energy-step-link', action.label);
    link.href = action.href;
    energyNote.replaceChildren(label, link);
  } else energyNote.replaceChildren(label);
  if (energyNote.parentNode !== host) host.append(energyNote);
}

// A mode change on the Inverter tab updates the Charging plan tab without waiting for its refresh.
function followPlanModes(devices) {
  const modes = new Map(devices.map((device) => [device.device_id, device.mode]));
  const changed = planDevices.some((device) => modes.has(device.device_id) && modes.get(device.device_id) !== device.mode);
  if (!changed) return;
  for (const device of planDevices) if (modes.has(device.device_id)) device.mode = modes.get(device.device_id);
  syncPlanSelection();
  if ($('energy-plan-device')?.value) void loadDryRunPlan();
}

async function pollEnergy() {
  if (energyPolling) return;
  energyPolling = true;
  clearTimeout(energyPollTimer); // one timer only: a manual or visibility poll restarts the cycle
  const host = $('energy-list');
  const requestedAt = ++energyClock;
  // A hung fetch must not stop the polling for good; the abort lands in the failure branch.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(Object.assign(new Error('The energy request timed out.'), { name: 'TimeoutError' })), ENERGY_POLL_TIMEOUT_MS);
  try {
    const devices = await api('energy/devices', { signal: controller.signal, background: true });
    energyPollDelay = ENERGY_POLL_MS;
    const ids = devices.map((item) => item.device_id);
    // Keep panels of devices that still exist (their unsaved input survives); add or drop the rest.
    for (const [id, panel] of [...energyPanels]) {
      if (!ids.includes(id)) { panel.root.remove(); energyPanels.delete(id); }
    }
    for (const device of devices) {
      const panel = energyPanels.get(device.device_id);
      if (!panel) {
        const created = createEnergyPanel(device, devices.length);
        energyPanels.set(device.device_id, created);
        host.append(created.root);
        if (energyHardwareStrategies.has(device.device_id)) {
          created.setHardwareStrategy(energyHardwareStrategies.get(device.device_id));
        }
      } else if (panel.acceptsPoll(requestedAt)) {
        panel.update(device, { poll503: false, pollFailed: false });
      }
    }
    if ([...energyPanels.keys()].join('\n') !== ids.join('\n')) {
      const ordered = new Map(ids.map((id) => [id, energyPanels.get(id)]));
      energyPanels.clear();
      for (const [id, panel] of ordered) { energyPanels.set(id, panel); host.append(panel.root); }
    }
    if (energyHardwareStrategies.size) renderHardwareSummary();
    followPlanModes(devices);
    setEnergyNote(host, devices.length ? null : 'No inverters configured yet.');
    // The absolute timestamp lives in Diagnostics now (design §13), not a top-of-page line.
    const stamp = `Updated ${new Date().toLocaleTimeString(undefined)}`;
    for (const device of devices) {
      const ages = Object.values(device.readings || {}).map((item) => item && item.age_seconds).filter(Number.isFinite);
      const oldest = ages.length ? ` · oldest measurement ${Math.round(Math.max(...ages))} s` : '';
      energyPanels.get(device.device_id).setTimestamp(`${stamp}${oldest}`);
    }
  } catch (error) {
    // 503 means write support/dispatch is disabled (design §9.2): render the config empty-state on
    // every panel; it clears on the next 200. Any other failure (including a timeout) is a
    // transient note (§13).
    const poll503 = error && error.status === 503;
    // 429 is short-lived load shedding, not an outage: keep what is shown and wait out Retry-After.
    const limited = error && error.status === 429;
    energyPollDelay = poll503 ? ENERGY_POLL_BACKOFF_MS : limited ? pollDelay(ENERGY_POLL_MS) : ENERGY_POLL_MS;
    if (!limited) for (const panel of energyPanels.values()) panel.setPollState({ poll503, pollFailed: !poll503 });
    if (!energyPanels.size) {
      if (poll503) {
        setEnergyNote(host, 'Manual battery control requires write support to be enabled.', { kind: 'warning', action: { label: 'Open Inverters page', href: '/ui/inverters' } });
      } else {
        setEnergyNote(host, 'Energy data unavailable. Retrying …', { kind: 'warning' });
      }
    }
  } finally {
    clearTimeout(timer);
    energyPolling = false;
    scheduleEnergyPoll();
  }
}

function scheduleEnergyPoll() {
  clearTimeout(energyPollTimer);
  energyPollTimer = setTimeout(() => {
    if (document.hidden) scheduleEnergyPoll(); else pollEnergy();
  }, energyPollDelay);
}

// One line per inverter. An unconnected inverter shows its configured host, never the internal
// device id (the inverter is known even while it does not answer), so the summary is redrawn after every energy poll as well (the connection state can change between).
function renderHardwareSummary() {
  const rows = [...energyHardwareStrategies.values()].map((device) => {
    const row = element('div');
    const code = Number.isInteger(device.code) ? device.code : null;
    const strategy = code === null ? NOT_AVAILABLE
      : device.label ? `${device.label} (code ${code})` : `Unknown (code ${code})`;
    const shownName = energyPanels.get(device.device_id)?.isConnected() ? device.device_name : device.host;
    row.textContent = `${shownName} · Inverter SOC strategy: ${strategy}${device.stale ? ' · stale' : ''}`;
    return row;
  });
  $('energy-hardware-strategies').replaceChildren(...rows);
}

let hardwareStrategyPolling = false;
async function pollHardwareStrategies() {
  if (hardwareStrategyPolling) return;
  hardwareStrategyPolling = true;
  const host = $('energy-hardware-strategies');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ENERGY_POLL_TIMEOUT_MS);
  try {
    const devices = await api('energy/hardware-strategies', { signal: controller.signal, background: true });
    energyHardwareStrategies.clear();
    for (const device of devices) {
      energyHardwareStrategies.set(device.device_id, device);
      energyPanels.get(device.device_id)?.setHardwareStrategy(device);
    }
    for (const [id, panel] of energyPanels) {
      if (!energyHardwareStrategies.has(id)) panel.setHardwareStrategy(null);
    }
    renderHardwareSummary();
  } catch (error) {
    if (error && error.status === 429) return; // keep the last strategies; the next 30 s tick retries
    host.textContent = 'Inverter SOC strategy: n/a (unavailable).';
    energyHardwareStrategies.clear();
    for (const panel of energyPanels.values()) panel.setHardwareStrategy(null);
  } finally {
    clearTimeout(timer);
    hardwareStrategyPolling = false;
  }
}

// Read-only Dry-Run preview of the dynamic charging plan (GET energy/devices/{id}/dynamic-plan).
// Non-"ok" statuses come back as HTTP 200 with a status and detail; only transport or 4xx/5xx
// errors reach the catch.
const PLAN_STATUS_LABELS = {
  ok: 'Plan available',
  insufficient_history: 'Consumption model is learning',
  spot_disabled: 'Spot market is disabled',
  spot_prices_unavailable: 'Spot prices are not available yet',
  solar_not_configured: 'Solar forecast is not configured',
  solar_forecast_unavailable: 'Solar forecast is not available yet',
  battery_state_unavailable: 'Usable battery capacity is needed',
};
const PLAN_ACTION_LABELS = { idle: 'No grid charging', charge: 'Grid charging', hold: 'Hold', discharge: 'Discharge', auto: 'Automatic' };
const PLAN_POLICY_LABELS = { gentle: 'Gentle (stay in the preferred power range)', meet_deadline: 'Meet deadline (raise power up to the hard limit if needed)' };
const LIMITING_REASON_LABELS = {
  reserve_not_securable: 'Reserve not securable',
  target_unreachable_in_preferred_range: 'Target not reachable in the preferred range',
  target_unreachable_at_hard_limit: 'Target not reachable at the hard limit',
  target_above_max_soc: 'Target above the maximum SoC',
  target_beyond_horizon: 'Target time beyond the planning horizon',
  preferred_range_exceeded_for_reserve: 'Preferred range exceeded to secure the reserve',
  preferred_range_exceeded_for_deadline: 'Preferred range exceeded to meet the deadline',
  preferred_range_clamped: 'Preferred range reduced to the permitted power',
  preferred_minimum_extra_energy: 'Extra energy charged to respect the preferred minimum power',
  preferred_range_not_confirmed: 'No confirmed preferred range',
  live_limit_unavailable: 'No verified live charge limit',
  live_limit_below_static: 'Verified live limit below the static limit',
};

function planKwh(wh) {
  return `${(Number(wh) / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 })} kWh`;
}

function planFlagLine(label, ok, text) {
  const line = element('p', 'small mb-1');
  line.append(`${label}: `, element('span', ok ? 'fw-semibold text-success-emphasis' : 'fw-semibold text-danger-emphasis', text));
  return line;
}

// Read-only plan details: policy, the independent reserve/target flags, power, energy domains,
// assumptions and limiting reasons. Absent fields (older payloads) are simply not shown.
function planDetails(plan) {
  const parts = [];
  if (plan.policy) parts.push(element('p', 'small mb-1', `Policy: ${PLAN_POLICY_LABELS[plan.policy] || plan.policy}`));
  if (plan.forecast_basis) {
    const labels = {
      bootstrap_baseline: 'configured baseline',
      history_fallback: 'stored history fallback',
      adaptive_history: 'adaptive stored history',
    };
    const history = plan.consumption_history_available === false ? '; usable measured history unavailable' : '';
    parts.push(element('p', 'small mb-1',
      `Consumption forecast: ${labels[plan.forecast_basis] || plan.forecast_basis} (${plan.forecast_confidence || 'unknown'} confidence${history})`));
  }
  if (typeof plan.reserve_satisfied === 'boolean') {
    const missing = plan.reserve_satisfied || !(plan.reserve_shortfall_wh > 0) ? '' : ` (${planKwh(plan.reserve_shortfall_wh)} of demand not covered)`;
    parts.push(planFlagLine('Reserve kept', plan.reserve_satisfied, `${plan.reserve_satisfied ? 'Yes' : 'No'}${missing}`));
  }
  if (typeof plan.target_reachable === 'boolean') {
    if (plan.target_soc_percent === null || plan.target_soc_percent === undefined) {
      parts.push(element('p', 'small mb-1', 'Target SoC: none set'));
    } else {
      const unserved = plan.target_reachable ? '' : ` (${planKwh(plan.unserved_target_energy_wh)} missing)`;
      parts.push(planFlagLine(`Target ${Math.round(plan.target_soc_percent)} % by ${planDateTime(plan.target_time)}`, plan.target_reachable,
        `${plan.target_reachable ? 'Reachable under the currently known limits' : 'Not reachable'}${unserved}`));
    }
  }
  if (plan.power) {
    const power = plan.power;
    const watts = (value) => (value === null || value === undefined ? NOT_AVAILABLE : formatPower(value));
    const preferred = power.preferred_max_w === null || power.preferred_max_w === undefined
      ? 'not confirmed' : `${watts(power.preferred_min_w)} – ${watts(power.preferred_max_w)}`;
    parts.push(element('p', 'small mb-1',
      `Charge power: static limit ${watts(power.static_charge_limit_w)}, live limit ${power.live_limit_available ? watts(power.live_charge_limit_w) : 'not available'}, `
      + `permitted ${watts(power.permitted_charge_power_w)}, preferred range ${preferred}`));
  }
  if (plan.energy) {
    const energy = plan.energy;
    parts.push(element('p', 'small mb-1',
      `Charge energy: commanded ${planKwh(energy.commanded_charge_energy_wh)}, at battery terminals ${planKwh(energy.battery_terminal_energy_wh)}, `
      + `stored ${planKwh(energy.stored_energy_delta_wh)}, from grid ${planKwh(energy.grid_charge_energy_wh)}`));
  }
  if (plan.assumptions) {
    const a = plan.assumptions;
    const list = element('ul', 'small mb-2');
    list.append(
      element('li', '', `Charge efficiency: ${Math.round(Number(a.charge_efficiency) * 100)} % (${a.charge_efficiency_source}), applied once between grid and battery terminals${a.charge_efficiency_note ? ` - ${a.charge_efficiency_note}` : ''}`),
      element('li', '', `Preferred range: ${a.preferred_range_status || 'unverified'}`),
      element('li', '', `Power reference: ${a.power_reference}${a.power_reference_verified ? '' : ' [unverified]'}`),
      element('li', '', `Live limit: ${a.live_limit_status}`),
      element('li', '', `Forecast basis: ${a.forecast_basis}`),
      element('li', '', `Battery-internal losses: ${a.battery_internal_losses}`),
    );
    parts.push(element('p', 'small fw-semibold mb-1', 'Assumptions'), list);
  }
  if (Array.isArray(plan.limiting_reasons) && plan.limiting_reasons.length) {
    const list = element('ul', 'small mb-2');
    for (const reason of plan.limiting_reasons) list.append(element('li', '', `${LIMITING_REASON_LABELS[reason.code] || reason.code}: ${reason.detail}`));
    parts.push(element('p', 'small fw-semibold mb-1', 'Limiting reasons'), list);
  }
  if (plan.replan?.triggers?.length) {
    parts.push(element('p', 'small text-secondary mb-2', `Replan when: ${plan.replan.triggers.map((trigger) => trigger.description.toLowerCase()).join('; ')}.`));
  }
  return parts;
}
let planGeneration = 0;

function planDateTime(iso) {
  if (!iso) return NOT_AVAILABLE;
  return new Date(iso).toLocaleString(undefined, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function planWindowRow(label, from, until) {
  const row = element('tr');
  row.append(element('th', '', label), element('td', '', from && until ? `${planDateTime(from)} – ${planDateTime(until)}` : NOT_AVAILABLE));
  row.firstChild.scope = 'row';
  return row;
}

function planWaitState(weatherSetupRequired = false) {
  const loading = element('div', 'spot-market-loading plan-loading text-center');
  loading.setAttribute('role', 'status');
  const icon = element('span', 'material-icons text-secondary plan-loading-icon', 'hourglass_empty');
  icon.setAttribute('aria-hidden', 'true');
  loading.append(icon, element('h3', 'h5 mb-0', 'Plan is being prepared'),
    element('p', 'text-secondary mb-0', 'Please check back in a few minutes.'));
  // The Energy Manager cannot start a plan until the Weather config carries a location (postal
  // code / coordinates) and the PV plant data. A single badge points the operator at the Weather
  // tab; it is additive to the hourglass, not a revived readiness list.
  if (weatherSetupRequired) {
    const badge = element('p', 'plan-setup-badge mb-0 mt-3');
    const chip = element('span', 'badge rounded-pill text-bg-warning',
      'Weather setup required: enter the postal code and PV system data on the Weather tab.');
    badge.append(chip);
    loading.append(badge);
  }
  return loading;
}

const PLAN_PENDING_STATUSES = new Set(['insufficient_history', 'spot_prices_unavailable', 'solar_forecast_unavailable']);
const PLAN_RETRY_STATUSES = new Set([...PLAN_PENDING_STATUSES, 'battery_state_unavailable']);

function planStatusCard(title, message) {
  const card = element('section', 'mb-3');
  card.append(element('h3', 'h5 mb-2', title), element('p', 'mb-0', message));
  return card;
}

function nextGridCharge(slots) {
  const first = slots.findIndex((slot) => slot.action === 'charge');
  if (first < 0) return null;
  const block = { ...slots[first] };
  for (let i = first + 1; i < slots.length && slots[i].action === 'charge' && slots[i].start === block.end && slots[i].max_power_w === block.max_power_w; i += 1) {
    block.end = slots[i].end;
    block.target_soc_percent = slots[i].target_soc_percent;
    block.max_power_w = slots[i].max_power_w;
  }
  return block;
}

// Plain-language explanations of the planner's machine-oriented slot reasons (app/optimizer/planner.py);
// an unknown reason is shown verbatim. PURPOSE is the short form for the schedule table.
const PLAN_REASON_LABELS = {
  'forecast PV covers demand': 'Solar covers forecast demand',
  'battery reserve covers demand': 'Battery can cover forecast demand',
  'forecast deficit, charged as late as permitted': 'Grid charging needed to protect the battery reserve',
  'charge as late as permitted for target SoC': 'Grid charging scheduled to reach the target SoC',
  'negative spot price, forecast deficit': 'Negative price: charge now for forecast demand',
  'no spot price for this slot': 'No market price available for this period',
};
const PLAN_PURPOSE_LABELS = {
  'forecast deficit, charged as late as permitted': 'Protect battery reserve',
  'charge as late as permitted for target SoC': 'Reach target SoC',
  'negative spot price, forecast deficit': 'Use negative price',
  'forecast PV covers demand': 'Solar covers demand',
  'battery reserve covers demand': 'Battery covers demand',
};

function planReasonLabel(reason) {
  return reason ? (PLAN_REASON_LABELS[reason] || reason) : '';
}

function planPurposeLabel(reason) {
  return reason ? (PLAN_PURPOSE_LABELS[reason] || reason) : NOT_AVAILABLE;
}

// Tooltip lines show only what applies: an idle slot has no power or target to report.
function planTooltipLines(slot) {
  const watts = planNumber(slot.max_power_w);
  const target = planNumber(slot.target_soc_percent);
  const lines = [];
  if (slot.action === 'charge') {
    lines.push(Number.isFinite(watts) ? `Grid charging · ${formatPower(watts)}` : 'Grid charging');
    if (Number.isFinite(target)) lines.push(`Target SoC ${Math.round(target)} %`);
  } else {
    lines.push(PLAN_ACTION_LABELS[slot.action] || slot.action);
  }
  if (slot.reason) lines.push(planReasonLabel(slot.reason));
  return lines;
}

function planSlotTime(slot) {
  const start = new Date(slot.start);
  const end = new Date(slot.end);
  const endText = start.toDateString() === end.toDateString()
    ? end.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : planDateTime(slot.end);
  return `${planDateTime(slot.start)} – ${endText}`;
}

let planChart = null;

function planNumber(value) {
  return value === null || value === undefined ? null : Number(value);
}

// One point at every slot start and end on a real time axis, so uneven slots keep their length;
// a gap between non-adjacent slots breaks the line instead of interpolating across it.
function planChartSeries(slots) {
  const power = [];
  const soc = [];
  let previousEnd = null;
  for (const slot of [...slots].sort((a, b) => new Date(a.start) - new Date(b.start))) {
    const start = new Date(slot.start).getTime();
    const end = new Date(slot.end).getTime();
    if (previousEnd !== null && start !== previousEnd) {
      power.push({ x: previousEnd, y: null });
      soc.push({ x: previousEnd, y: null });
    }
    const watts = planNumber(slot.max_power_w);
    const target = planNumber(slot.target_soc_percent);
    const powerKw = slot.action === 'charge' && Number.isFinite(watts) ? watts / 1000 : 0;
    const socValue = Number.isFinite(target) ? target : null;
    for (const x of [start, end]) {
      power.push({ x, y: powerKw, slot });
      soc.push({ x, y: socValue, slot });
    }
    previousEnd = end;
  }
  return { power, soc };
}

// Ticks on whole local hours with a step that keeps the axis readable at any plan length.
function planChartTicks(min, max) {
  const hour = 3600000;
  const step = [1, 2, 3, 6, 12, 24].find((h) => (max - min) / (h * hour) <= 8) || 24;
  const first = new Date(min);
  first.setMinutes(0, 0, 0);
  // Align to multiples of the step from local midnight so midnight itself is a labelled tick.
  while (first.getHours() % step !== 0 || first.getTime() < min) first.setTime(first.getTime() + hour);
  const ticks = [];
  for (let t = first.getTime(); t <= max; t += step * hour) ticks.push({ value: t });
  return ticks;
}

function planChartTickLabel(value) {
  const date = new Date(value);
  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (date.getHours() !== 0) return time;
  return [date.toLocaleDateString(undefined, { day: '2-digit', month: 'short' }), time];
}

// Axis unit titles match the legend size and the tick colour; the padding keeps them off the ticks.
function chartAxisTitle(text, color) {
  return { display: true, text, color, font: { size: 13, weight: 600 }, padding: { top: 4, bottom: 8 } };
}

function planChartColors(host) {
  const styles = getComputedStyle(host);
  return {
    label: styles.getPropertyValue('--bs-body-color').trim(),
    grid: styles.getPropertyValue('--rct-line').trim(),
    power: styles.getPropertyValue('--rct-plan-power').trim(),
    soc: styles.getPropertyValue('--rct-plan-soc').trim(),
  };
}

function syncPlanChartTheme() {
  const host = $('energy-plan-schedule');
  if (!planChart || !host) return;
  const { label, grid, power, soc } = planChartColors(host);
  planChart.options.plugins.legend.labels.color = label;
  for (const axis of Object.values(planChart.options.scales)) {
    axis.ticks.color = label;
    if (axis.grid.drawOnChartArea !== false) axis.grid.color = grid;
    if (axis.title) axis.title.color = label;
  }
  const [powerSet, socSet] = planChart.data.datasets;
  powerSet.borderColor = power;
  powerSet.backgroundColor = `${power}33`;
  socSet.borderColor = soc;
  socSet.backgroundColor = soc;
  planChart.update('none');
}

function clearPlanSchedule() {
  planChart?.destroy();
  planChart = null;
  $('energy-plan-schedule')?.replaceChildren();
}

function drawPlanChart(canvas, slots) {
  planChart?.destroy();
  planChart = null;
  const { power, soc } = planChartSeries(slots);
  const times = power.map((point) => point.x);
  const min = Math.min(...times);
  const max = Math.max(...times);
  const { label, grid, power: powerColor, soc: socColor } = planChartColors($('energy-plan-schedule'));
  const hasSoc = soc.some((point) => point.y !== null);
  planChart = new globalThis.Chart(canvas, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'Grid charge power (kW)', data: power, yAxisID: 'power', borderColor: powerColor,
          backgroundColor: `${powerColor}33`, fill: 'origin', pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: false,
        },
        {
          label: 'Target SoC (%)', data: soc, yAxisID: 'soc', borderColor: socColor, backgroundColor: socColor,
          borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: false, hidden: !hasSoc,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'nearest', intersect: false, axis: 'x' },
      plugins: {
        legend: { display: true, labels: { color: label, usePointStyle: true, pointStyle: 'line' } },
        tooltip: {
          // Both series describe the same slot, so one entry carries the whole tooltip.
          filter: (item, index, items) => items.findIndex((other) => Number.isFinite(other.parsed?.y)) === index,
          callbacks: {
            title: (items) => (items[0]?.raw?.slot ? planSlotTime(items[0].raw.slot) : ''),
            label: (item) => planTooltipLines(item.raw.slot),
          },
        },
      },
      scales: {
        x: {
          type: 'linear', min, max, grid: { color: grid },
          afterBuildTicks: (axis) => { axis.ticks = planChartTicks(axis.min, axis.max); },
          ticks: { color: label, maxRotation: 0, callback: planChartTickLabel },
        },
        power: {
          position: 'left', beginAtZero: true, grid: { color: grid }, ticks: { color: label, padding: 6 },
          title: chartAxisTitle('kW', label),
        },
        soc: {
          position: 'right', min: 0, max: 100, display: hasSoc, grid: { drawOnChartArea: false }, ticks: { color: label, padding: 6 },
          title: chartAxisTitle('%', label),
        },
      },
    },
  });
}

function planChartSummary(slots) {
  const charge = slots.filter((slot) => slot.action === 'charge');
  const sorted = [...slots].sort((a, b) => new Date(a.start) - new Date(b.start));
  const span = `${planDateTime(sorted[0].start)} – ${planDateTime(sorted[sorted.length - 1].end)}`;
  return `Charging schedule ${span}: ${charge.length} grid charge slot${charge.length === 1 ? '' : 's'} of ${slots.length}`;
}

// The chart is the time overview; the charging schedule table below it is its text alternative.
// No aria-describedby to that table: a screen reader would read the whole table twice.
function planSchedule(plan, detailsOpen = false) {
  const section = element('section', 'energy-plan-section');
  section.setAttribute('aria-labelledby', 'energy-plan-schedule-title');
  const title = element('h3', 'h6', 'Charging schedule');
  title.id = 'energy-plan-schedule-title';
  section.append(title);
  if (!plan.slots.length) {
    section.append(element('p', 'text-secondary small mt-2', 'No scheduled slots.'));
    return { section, canvas: null };
  }
  let canvas = null;
  if (globalThis.Chart) {
    const box = element('div', 'spot-market-chart-box');
    canvas = element('canvas');
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', planChartSummary(plan.slots));
    box.append(canvas);
    section.append(box);
  } else {
    section.append(element('p', 'text-danger small', 'Chart library could not be loaded.'));
  }
  section.append(planChargeSchedule(plan.slots), planScheduleDetails(plan.slots, detailsOpen));
  return { section, canvas };
}

// Stacked-card tables (data-table-stack, admin.css): every cell carries its column name in data-label.
function planTable(className, captionText, columns, rows) {
  const table = element('table', `table table-sm energy-plan-slot-table data-table-stack ${className}`);
  table.append(element('caption', 'visually-hidden', captionText));
  const head = element('tr');
  for (const [name, numeric] of columns) {
    const cell = element('th', numeric ? 'text-end' : '', name);
    cell.scope = 'col';
    head.append(cell);
  }
  const thead = element('thead');
  thead.append(head);
  const body = element('tbody');
  for (const cells of rows) {
    const row = element('tr');
    cells.forEach((cell, index) => { cell.dataset.label = columns[index][0]; });
    row.append(...cells);
    body.append(row);
  }
  table.append(thead, body);
  return table;
}

// Default view: only the planned grid-charging windows. Idle slots are hidden here, not removed from the plan.
function planChargeSchedule(slots) {
  const wrap = element('div', 'mt-2');
  wrap.id = 'energy-plan-charge-schedule';
  const charge = planSlotRuns(slots.filter((slot) => slot.action === 'charge'));
  if (!charge.length) {
    wrap.append(element('p', 'text-secondary small mb-0', 'No grid charging planned.'));
    return wrap;
  }
  const columns = [['Time'], ['Power', true], ['Target SoC', true], ['Purpose']];
  const rows = charge.map((slot) => {
    const watts = planNumber(slot.max_power_w);
    const target = planNumber(slot.target_soc_percent);
    return [
      element('td', '', planSlotTime(slot)),
      element('td', 'text-end', Number.isFinite(watts) ? formatPower(watts) : NOT_AVAILABLE),
      element('td', 'text-end', Number.isFinite(target) ? `${Math.round(target)} %` : NOT_AVAILABLE),
      element('td', 'data-table-stack-prose', planPurposeLabel(slot.reason)),
    ];
  });
  wrap.append(planTable('mb-0', 'Planned grid charging windows', columns, rows));
  return wrap;
}

// Only directly adjacent slots with the same action, power, target and reason merge; a gap or any change starts a new row.
function planSlotRuns(slots) {
  const runs = [];
  for (const slot of [...slots].sort((a, b) => new Date(a.start) - new Date(b.start))) {
    const last = runs[runs.length - 1];
    const same = last
      && last.action === slot.action && last.max_power_w === slot.max_power_w
      && last.target_soc_percent === slot.target_soc_percent && (last.reason || '') === (slot.reason || '')
      && new Date(last.end).getTime() === new Date(slot.start).getTime();
    if (same) last.end = slot.end;
    else runs.push({ ...slot });
  }
  return runs;
}

// Collapsed full technical slot history: keyboard and touch friendly, numbers right-aligned.
function planScheduleDetails(slots, open = false) {
  const details = element('details', 'energy-plan-slot-details mt-2');
  details.id = 'energy-plan-schedule-details';
  details.open = open;
  details.append(element('summary', '', 'Show all intervals'));
  // data-table-stack: below the phone breakpoint the five-column table would squeeze "Power"/"162 W"
  // character-by-character, so each row stacks into a labelled card (admin.css).
  const columns = [['Time'], ['Action'], ['Power', true], ['Target SoC', true], ['Reason']];
  const rows = planSlotRuns(slots).map((slot) => {
    const watts = planNumber(slot.max_power_w);
    const target = planNumber(slot.target_soc_percent);
    // The reason is free prose: it spans full width and wraps at word boundaries in the stacked card.
    return [
      element('td', '', planSlotTime(slot)),
      element('td', '', PLAN_ACTION_LABELS[slot.action] || slot.action),
      element('td', 'text-end', Number.isFinite(watts) && slot.action === 'charge' ? formatPower(watts) : NOT_AVAILABLE),
      element('td', 'text-end', Number.isFinite(target) ? `${Math.round(target)} %` : NOT_AVAILABLE),
      element('td', 'data-table-stack-prose', slot.reason ? planReasonLabel(slot.reason) : NOT_AVAILABLE),
    ];
  });
  details.append(planTable('mt-2 mb-0', 'All charging plan intervals', columns, rows));
  return details;
}

function planTechnicalDetails(plan) {
  const parts = [];
  if (plan.detail) parts.push(element('p', 'small text-secondary', plan.detail));
  if (plan.status !== 'ok') return parts;
  parts.push(element('p', 'small', `Horizon: ${Number(plan.horizon_hours).toLocaleString(undefined, { maximumFractionDigits: 1 })} h`
    + (plan.created_at ? ` · created ${planDateTime(plan.created_at)}` : '')
    + (plan.valid_until ? ` · valid until ${planDateTime(plan.valid_until)}` : '')));
  parts.push(...planDetails(plan));
  const windows = element('table', 'table table-sm w-auto');
  const windowBody = element('tbody');
  windowBody.append(
    planWindowRow('Consumption', plan.window?.consumption_from, plan.window?.consumption_until),
    planWindowRow('Solar', plan.window?.solar_from, plan.window?.solar_until),
    planWindowRow('Prices', plan.window?.prices_from, plan.window?.prices_until),
  );
  windows.append(element('caption', 'visually-hidden', 'Forecast inputs'), windowBody);
  parts.push(windows);
  if (plan.price_disclaimer) parts.push(element('p', 'small text-secondary mb-0', plan.price_disclaimer));
  return parts;
}

const PLAN_CONFIDENCE_LABELS = { low: 'Low confidence', normal: 'Normal confidence' };
// Every ForecastBasis value (app/optimizer/models.py) except the baseline shows its confidence.
const PLAN_CONFIDENCE_BASES = new Set(['adaptive_history', 'history_fallback']);

// Forecast confidence next to the execution state, from forecast_basis / forecast_confidence only;
// a missing field hides the part instead of inventing a state.
function renderPlanConfidence(plan) {
  const label = $('energy-plan-confidence');
  if (!label) return;
  const ok = plan.status === 'ok';
  const text = !ok ? '' : plan.forecast_basis === 'bootstrap_baseline' ? 'Baseline forecast'
    : PLAN_CONFIDENCE_BASES.has(plan.forecast_basis) ? (PLAN_CONFIDENCE_LABELS[plan.forecast_confidence] || '') : '';
  label.hidden = !text;
  label.textContent = text;
  const explanation = text === 'Low confidence' ? 'Limited consumption history'
    : text === 'Baseline forecast' ? 'Conservative household baseline while consumption history is collected' : '';
  if (explanation) {
    label.title = explanation;
    label.setAttribute('aria-label', `${text}: ${explanation}`);
  } else {
    label.removeAttribute('title');
    label.removeAttribute('aria-label');
  }
}

function renderDryRunPlan(plan) {
  const host = $('energy-plan-result');
  const technical = $('energy-plan-technical');
  if (technical) technical.replaceChildren(...planTechnicalDetails(plan));
  const forecast = $('energy-plan-forecast');
  if (forecast) forecast.hidden = plan.status !== 'ok';
  // Read before clearPlanSchedule() removes the element, so a refresh keeps an opened table open.
  const scheduleDetailsOpen = Boolean($('energy-plan-schedule-details')?.open);
  clearPlanSchedule();
  renderPlanConfidence(plan);
  if (plan.status === 'energy_manager_off') {
    // The inverter was switched off meanwhile: the tab disappears with the next device sync.
    host.replaceChildren();
    return;
  }
  if (plan.status !== 'ok') {
    // Every not-ready status renders the same hourglass wait card: the plan re-checks itself in the
    // background (PLAN_RETRY_STATUSES / PLAN_REFRESH_MS). The only variation is the setup badge for
    // 'solar_not_configured', which means the Weather config is enabled but still missing the
    // location (postal code -> coordinates) and the PV plant data the forecast needs.
    host.replaceChildren(planWaitState(plan.status === 'solar_not_configured'));
    return;
  }
  const next = nextGridCharge(plan.slots || []);
  const card = next
    ? planStatusCard('Next grid charge', `${planDateTime(next.start)} – ${planDateTime(next.end)}`)
    : planStatusCard('No grid charging planned', 'The current plan does not schedule grid charging.');
  // Three plan-KPI tiles (plan outputs) built from the plan's own snapshot (no second data source).
  // Battery remaining is a live device state, so it renders as a weaker tertiary line below the grid.
  const summary = element('div', 'spot-market-highlights energy-plan-highlights mt-3');
  const add = (caption, value, icon, sub) => {
    const block = element('div', 'spot-market-highlight');
    const glyph = element('span', 'material-icons spot-market-highlight-icon text-secondary', icon);
    glyph.setAttribute('aria-hidden', 'true');
    block.append(glyph, element('span', 'spot-market-highlight-caption', caption), element('span', 'spot-market-highlight-value', value));
    if (sub) block.append(element('span', 'spot-market-highlight-sub', sub));
    summary.append(block);
  };
  const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));
  const battery = plan.battery;
  const hasBattery = known(battery?.remaining_energy_wh) && known(battery?.soc_percent) && known(battery?.usable_capacity_wh);
  add('Target SoC', next && known(next.target_soc_percent) ? `${Math.round(next.target_soc_percent)} %` : '—', 'battery_charging_full');
  add('Charge power', next && known(next.max_power_w) ? formatPower(next.max_power_w) : '—', 'bolt');
  add('Total plan grid energy', known(plan.energy?.grid_charge_energy_wh) ? planKwh(plan.energy.grid_charge_energy_wh) : '—', 'electric_meter');
  card.append(summary);
  // The battery is an input of the plan, not an output: a quiet labelled line, not another KPI.
  const batteryLine = element('p', 'energy-plan-battery d-flex flex-wrap align-items-center gap-2 small text-secondary mt-3 mb-0');
  const batteryGlyph = element('span', 'material-icons text-secondary', 'battery_5_bar');
  batteryGlyph.setAttribute('aria-hidden', 'true');
  const batteryText = hasBattery
    ? `${Math.round(battery.soc_percent)} % SoC · ${planKwh(battery.remaining_energy_wh)} remaining / ${planKwh(battery.usable_capacity_wh)} usable`
    : 'Not available';
  batteryLine.append(batteryGlyph, element('span', 'energy-plan-battery-label', 'Current battery state'), element('span', 'energy-plan-battery-value', batteryText));
  card.append(batteryLine);
  host.replaceChildren(card);
  const scheduleHost = $('energy-plan-schedule');
  const { section, canvas } = planSchedule(plan, scheduleDetailsOpen);
  scheduleHost.replaceChildren(section);
  // Drawn after attaching: Chart.js sizes the canvas from its laid-out container.
  if (canvas) drawPlanChart(canvas, plan.slots);
}

let planDevices = [];
let calibrationHistoryGeneration = 0;
let calibrationHistoryRows = [];
let calibrationChart = null;

const CALIBRATION_BASELINE_NOTES = {
  inverter_serial_unknown: 'The inverter serial number is not known yet, so no calibration is recorded.',
  no_commissioning_date: 'No commissioning date is known yet, so the curve has no starting point. '
    + 'It is read from the inverter logs when they hold energy data; you can also enter it below.',
  no_measurement: 'The commissioning date is known; the curve starts once the first BMS measurement is recorded.',
  battery_replaced: 'The battery was replaced (new BMS serial). The derived commissioning date belongs to the '
    + 'first battery, so the new series has no starting point; enter the replacement date below to add one.',
};

// Baseline (datasheet capacity at commissioning, 0 cycles) first, then the dated BMS measurements.
function calibrationRows(history) {
  const rows = history.points.map((point) => ({
    at: point.measured_at, cycles: point.battery_cycles, wh: point.current_usable_capacity_wh,
    rated: point.rated_usable_capacity_wh, percent: point.remaining_capacity_percent, baseline: false,
  }));
  const base = history.baseline;
  if (base) {
    rows.unshift({
      at: base.date, cycles: 0, wh: base.current_usable_capacity_wh, rated: base.rated_usable_capacity_wh,
      percent: base.remaining_capacity_percent, baseline: true,
    });
  }
  return rows;
}

function drawBatteryCalibrationChart() {
  calibrationChart?.destroy();
  calibrationChart = null;
  const details = $('energy-plan-diagnostics');
  const canvas = $('energy-plan-calibration-chart');
  if (!details?.open || !canvas || !globalThis.Chart || !calibrationHistoryRows.length) return;
  const rows = calibrationHistoryRows;
  const { label, grid, power } = planChartColors($('energy-plan-calibration'));
  const times = rows.map((row) => Date.parse(row.at));
  const dateText = (value) => new Date(value).toLocaleDateString();
  calibrationChart = new globalThis.Chart(canvas, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'Usable capacity (kWh)', data: rows.map((row, index) => ({ x: times[index], y: row.wh / 1000 })),
          borderColor: power, backgroundColor: power, pointRadius: 4, tension: 0
        },
        {
          label: 'Datasheet capacity (100%)',
          data: [{ x: times[0], y: rows[0].rated / 1000 }, { x: times[times.length - 1], y: rows[rows.length - 1].rated / 1000 }],
          borderColor: label, backgroundColor: label, borderDash: [5, 4], pointRadius: 0, tension: 0
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { labels: { color: label } },
        tooltip: {
          callbacks: {
            title: (items) => (items[0] ? dateText(items[0].parsed?.x ?? times[items[0].dataIndex]) : ''),
            afterBody: (items) => {
              if (items[0]?.datasetIndex === 1) return [];
              const row = rows[items[0]?.dataIndex];
              return row ? [`Cycles: ${row.cycles ?? 'n/a'}`, `Remaining: ${row.percent} %`] : [];
            },
          }
        },
      },
      scales: {
        x: { type: 'linear', ticks: { color: label, callback: dateText, maxTicksLimit: 8 }, grid: { color: grid } },
        y: {
          beginAtZero: true, title: chartAxisTitle('kWh', label),
          ticks: { color: label }, grid: { color: grid }
        },
      },
    },
  });
}

function calibrationCommissioningForm(history, deviceId) {
  const form = element('form', 'd-flex flex-wrap align-items-end gap-2 mb-2');
  const field = element('div');
  const label = element('label', 'form-label small mb-1', 'Commissioning date');
  label.htmlFor = 'energy-plan-commissioning-date';
  const input = element('input', 'form-control form-control-sm');
  input.type = 'date';
  input.id = 'energy-plan-commissioning-date';
  input.min = '2010-01-01';
  input.value = history.commissioning ? history.commissioning.commissioned_at.slice(0, 10) : '';
  field.append(label, input);
  const save = element('button', 'btn btn-sm btn-outline-primary', 'Save date');
  save.type = 'submit';
  form.append(field, save);
  const submit = async (request) => {
    save.disabled = true;
    try {
      const result = await api(`energy/devices/${encodeURIComponent(deviceId)}/battery-commissioning`, request);
      toast('Commissioning date saved.');
      if ($('energy-plan-device')?.value === deviceId) renderBatteryCalibrationHistory(result);
    } catch (error) { toast(messageFrom(error), 'danger'); } finally { save.disabled = false; }
  };
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!input.value) return;
    void submit({ method: 'PUT', body: JSON.stringify({ commissioned_on: input.value }) });
  });
  if (history.commissioning?.source === 'manual') {
    const clear = element('button', 'btn btn-sm btn-outline-secondary', 'Use derived date');
    clear.type = 'button';
    clear.addEventListener('click', () => { void submit({ method: 'DELETE' }); });
    form.append(clear);
  }
  return form;
}

function renderBatteryCalibrationHistory(history) {
  const host = $('energy-plan-calibration');
  if (!host) return;
  calibrationChart?.destroy();
  calibrationChart = null;
  const rows = calibrationRows(history);
  calibrationHistoryRows = rows;
  const deviceId = $('energy-plan-device')?.value || history.device_id;
  const parts = [];
  if (history.inverter_serial) {
    const bms = history.battery_bms_sn ? `, BMS ${history.battery_bms_sn}` : '';
    parts.push(element('p', 'small text-secondary mb-2', `Inverter ${history.inverter_serial}${bms}`));
  }
  if (history.baseline_unavailable_reason) {
    parts.push(element('p', 'small text-secondary mb-2', CALIBRATION_BASELINE_NOTES[history.baseline_unavailable_reason] || ''));
  } else if (history.baseline?.source === 'logger_derived') {
    parts.push(element('p', 'small text-secondary mb-2',
      'The commissioning date was derived from the inverter energy logs and is not verified on hardware.'));
  }
  if (history.inverter_serial) parts.push(calibrationCommissioningForm(history, deviceId));
  if (!history.points.length) {
    parts.push(element('p', 'small text-secondary', 'No BMS calibration has been recorded yet.'));
    host.replaceChildren(...parts);
    return;
  }
  parts.push(element('p', 'small text-secondary mb-2',
    'The datasheet usable capacity is the fixed 100% reference. Dated points come from BMS measurements.'));
  const box = element('div', 'spot-market-chart-box mb-2');
  const canvas = element('canvas');
  canvas.id = 'energy-plan-calibration-chart';
  canvas.setAttribute('aria-hidden', 'true');
  box.append(canvas);
  const table = element('table', 'table table-sm small');
  const caption = element('caption', 'visually-hidden', 'Battery calibration history: date, cycles, usable capacity and remaining capacity');
  const header = element('thead');
  const headerRow = element('tr');
  for (const title of ['Date', 'Cycles', 'Usable kWh', 'Remaining %']) headerRow.append(element('th', '', title));
  header.append(headerRow);
  const body = element('tbody');
  for (const row of rows) {
    const tr = element('tr');
    const when = new Date(row.at);
    const values = [row.baseline ? `${when.toLocaleDateString()} (commissioning)` : when.toLocaleString(),
    row.cycles ?? 'n/a',
    (row.wh / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 }),
    row.percent.toLocaleString(undefined, { maximumFractionDigits: 2 })];
    for (const value of values) tr.append(element('td', '', String(value)));
    body.append(tr);
  }
  table.append(caption, header, body);
  parts.push(box, table);
  host.replaceChildren(...parts);
  drawBatteryCalibrationChart();
}

async function loadBatteryCalibrationHistory(deviceId) {
  const host = $('energy-plan-calibration');
  if (!host) return;
  const generation = ++calibrationHistoryGeneration;
  calibrationChart?.destroy();
  calibrationChart = null;
  calibrationHistoryRows = [];
  host.replaceChildren(element('p', 'small text-secondary', 'Loading BMS calibration history …'));
  try {
    const history = await api(`energy/devices/${encodeURIComponent(deviceId)}/battery-calibration-history`);
    if (generation !== calibrationHistoryGeneration || $('energy-plan-device')?.value !== deviceId) return;
    renderBatteryCalibrationHistory({ ...history, points: Array.isArray(history.points) ? history.points : [] });
  } catch (error) {
    if (generation === calibrationHistoryGeneration) {
      host.replaceChildren(element('p', 'small text-danger', `Could not load battery calibration history: ${messageFrom(error)}`));
    }
  }
}

// Only Manual inverters appear in the GUI charging-plan selector and unlock its planning tabs.
// External uses a separate control path and may still have a plan outside this GUI.
const planEligible = (device) => device.mode === 'manual';

// Charging plan, Spot Market and Weather are shown together while at least one inverter is Manual.
// Spot Market and Weather are site-wide provider settings, so their visibility follows the whole
// device set rather than the currently selected inverter; External alone keeps all three hidden.
function syncPlanSelection() {
  const select = $('energy-plan-device');
  if (!select) return;
  const choices = planDevices.filter(planEligible);
  const previous = select.value;
  select.replaceChildren(...choices.map((device) => {
    const option = element('option', '', device.device_name || device.device_id);
    option.value = device.device_id;
    return option;
  }));
  select.value = choices.some((device) => device.device_id === previous) ? previous : (choices[0]?.device_id ?? '');
  const inverterTab = $('energy-tab-inverter');
  const visible = Boolean(choices.length);
  let fellBack = false;
  for (const name of ['plan', 'spot-market', 'weather']) {
    const tab = $(`energy-tab-${name}`);
    const pane = $(`energy-pane-${name}`);
    if (!tab || !pane) continue;
    tab.parentElement.hidden = !visible;
    pane.hidden = !visible;
    if (!visible && tab.classList.contains('active')) fellBack = true;
  }
  if (fellBack && inverterTab && window.bootstrap) {
    window.bootstrap.Tab.getOrCreateInstance(inverterTab).show();
  }
}

// Read-only device list: independent of write support and of the dispatch controller.
async function refreshPlanDevices() {
  const select = $('energy-plan-device');
  if (!select) return;
  planDevices = await api('energy/planning-devices');
  syncPlanSelection();
  fillPlanLimits();
}

function fillPlanLimits() {
  const form = $('energy-plan-limits');
  if (!form) return;
  const device = planDevices.find((item) => item.device_id === $('energy-plan-device').value);
  const limits = device?.limits;
  const sourceLabels = { expert_override: 'Expert override', verified: 'Verified automatically' };
  const source = device ? sourceLabels[device.battery_capacity_source] || 'Not verified' : 'No inverter selected';
  const overridden = device?.battery_capacity_source === 'expert_override';
  const diagnosticRows = (id, rows) => {
    const list = $(id);
    if (!list) return;
    list.replaceChildren(...rows.flatMap(([label, value]) => [element('dt', 'col-5 col-sm-3 fw-normal text-secondary', label), element('dd', 'col-7 col-sm-9 mb-1', value)]));
  };
  const watts = (value) => (Number.isFinite(value) ? `${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 3 })} kW` : NOT_AVAILABLE);
  diagnosticRows('energy-plan-diag-battery', [
    ['Status', device ? (device.battery_capacity_source === 'unavailable' ? 'Unavailable' : 'Available') : NOT_AVAILABLE],
    ['Source', source],
    ['Override', overridden ? `${formatKwh(limits?.usable_capacity_wh)} kWh` : 'None'],
  ]);
  diagnosticRows('energy-plan-diag-power', [
    ['Charge', watts(limits?.max_charge_power_w)],
    ['Discharge', watts(limits?.max_discharge_power_w)],
  ]);
  const kw = (watts) => (Number.isFinite(watts) ? String(Number((watts / 1000).toFixed(3))) : '');
  form.elements.charge.value = kw(limits?.max_charge_power_w);
  form.elements.discharge.value = kw(limits?.max_discharge_power_w);
  form.elements.capacity.value = formatKwh(limits?.usable_capacity_wh);
}

// Saves the battery data the planner needs. Unlike the Setup form an empty capacity clears the
// stored value on purpose: this form shows the stored value, so an empty field means "remove it".
async function savePlanLimits(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const id = $('energy-plan-device').value;
  const button = form.querySelector('button[type=submit]');
  if (!id || !form.checkValidity()) { form.reportValidity(); return; }
  button.disabled = true;
  try {
    await api(`energy/devices/${encodeURIComponent(id)}/planning-limits`, {
      method: 'PUT',
      body: JSON.stringify({
        max_charge_power_w: Math.round(Number(form.elements.charge.value) * 1000),
        max_discharge_power_w: Math.round(Number(form.elements.discharge.value) * 1000),
        usable_capacity_wh: form.elements.capacity.value === '' ? null : Math.round(Number(form.elements.capacity.value) * 1000),
      }),
    });
    toast('Expert battery data override saved.');
    await refreshPlanDevices();
    await loadDryRunPlan();
  } catch (error) { toast(messageFrom(error), 'danger'); } finally { button.disabled = false; }
}

// Hide and empty the Expert forecast diagnostics so a new request never shows the previous
// inverter's forecast while B is calculating, nor leaves it beside an error or the no-device
// message. renderDryRunPlan reveals it again (status === 'ok') once a fresh plan renders.
function clearPlanForecast() {
  const forecast = $('energy-plan-forecast');
  const technical = $('energy-plan-technical');
  if (forecast) forecast.hidden = true;
  if (technical) technical.replaceChildren();
}

let consumptionChart = null;
let consumptionGeneration = 0;

// Bar colours per source; unavailable days stay empty instead of being drawn as zero.
const CONSUMPTION_SOURCES = {
  local_measured: { label: 'From live inverter readings', color: '--consumption-measured' },
  inverter_history: { label: 'From inverter daily history', color: '--consumption-history' },
};

function consumptionChartColors() {
  const styles = getComputedStyle($('energy-consumption'));
  return {
    styles,
    label: styles.getPropertyValue('--bs-body-color').trim(),
    grid: styles.getPropertyValue('--rct-line').trim(),
  };
}

function syncConsumptionChartTheme() {
  if (!consumptionChart) return;
  const { label, grid } = consumptionChartColors();
  for (const axis of Object.values(consumptionChart.options.scales)) {
    axis.ticks.color = label;
    axis.grid.color = grid;
    if (axis.title) axis.title.color = label;
  }
  consumptionChart.update('none');
}

// The whole section stays hidden while there is nothing to draw (no heading, no explanation).
function setConsumptionVisible(visible) {
  const section = $('energy-consumption-section');
  if (section) section.hidden = !visible;
}

// Data coverage of the window, neutral on purpose: a day counts once it has a value, whether it
// was measured locally or recovered from the inverter's day history. Only the already-fetched
// history is used. A null/undefined history clears the slot.
function renderInverterHistoryStatus(history) {
  const slot = $('energy-consumption-import-status');
  if (!slot) return false;
  if (!history) {
    slot.replaceChildren();
    return false;
  }
  const days = Array.isArray(history.days) ? history.days : [];
  const known = days.filter((day) => Number.isFinite(day.energy_wh));
  const since = Date.parse(history.recording_since);
  let coverage = `History coverage · ${known.length}/${days.length} days`;
  if (!known.length && Number.isFinite(since)) {
    coverage += ` · recording since ${new Date(since).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' })}`;
  }
  const parts = [element('span', '', coverage)];
  if (known.length) {
    const local = known.filter((day) => day.source === 'local_measured').length;
    const inverter = known.filter((day) => day.source === 'inverter_history').length;
    parts.push(element('span', 'energy-consumption-sources', `${local} local · ${inverter} inverter`));
  }
  slot.replaceChildren(...parts);
  return true;
}

// One button per data series; each toggles exactly its own dataset (keyboard and touch operable)
// and reports the state through aria-pressed, which also drives the strike-through.
function consumptionLegend(chart, datasets) {
  const group = element('div', 'energy-consumption-legend');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', 'Data sources');
  datasets.forEach((dataset, index) => {
    const button = element('button', 'energy-consumption-legend-item');
    button.type = 'button';
    button.setAttribute('aria-pressed', 'true');
    const swatch = element('span', 'energy-consumption-legend-swatch');
    swatch.style.backgroundColor = dataset.backgroundColor;
    swatch.setAttribute('aria-hidden', 'true');
    button.append(swatch, element('span', '', dataset.label));
    button.addEventListener('click', () => {
      const visible = chart.isDatasetVisible(index);
      chart.setDatasetVisibility(index, !visible);
      button.setAttribute('aria-pressed', String(!visible));
      chart.update('none');
    });
    group.append(button);
  });
  return group;
}

function drawConsumptionChart(history) {
  consumptionChart?.destroy();
  consumptionChart = null;
  const host = $('energy-consumption');
  const status = $('energy-consumption-status');
  const days = Array.isArray(history?.days) ? history.days : [];
  const known = days.filter((day) => Number.isFinite(day.energy_wh));
  const hasImportStatus = renderInverterHistoryStatus(history);
  if (!known.length) {
    status.replaceChildren();
    host.replaceChildren();
    // Keep the section visible while the import status still has something to say (import off, or
    // on but no completed day yet), so it can explain the otherwise-empty chart.
    setConsumptionVisible(hasImportStatus);
    return;
  }
  setConsumptionVisible(true);
  if (!window.Chart) {
    host.replaceChildren(element('p', 'text-danger small', 'Chart library could not be loaded.'));
    return;
  }
  const average = known.reduce((sum, day) => sum + day.energy_wh, 0) / known.length / 1000;
  status.replaceChildren(element('span', '',
    `Ø ${average.toLocaleString(undefined, { maximumFractionDigits: 1 })} kWh/day`));
  const box = element('div', 'spot-market-chart-box');
  const canvas = element('canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', `Daily household consumption over the last ${days.length} days, average ${average.toFixed(1)} kWh per day`);
  box.append(canvas);
  host.replaceChildren(box);
  const { styles, label, grid } = consumptionChartColors();
  const labels = days.map((day) => new Date(`${day.date}T12:00:00`).toLocaleDateString(undefined, { day: '2-digit', month: 'short' }));
  const datasets = Object.entries(CONSUMPTION_SOURCES).map(([source, meta]) => {
    const color = styles.getPropertyValue(meta.color).trim();
    return {
      label: meta.label,
      data: days.map((day) => (day.source === source && Number.isFinite(day.energy_wh) ? day.energy_wh / 1000 : null)),
      backgroundColor: color, borderColor: color, stack: 'kwh', borderRadius: 2,
    };
  });
  const chart = new window.Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false, axis: 'x' },
      plugins: {
        // The legend is HTML (consumptionLegend): every entry is a real button for its own series.
        legend: { display: false },
        tooltip: {
          filter: (item) => Number.isFinite(item.parsed?.y),
          callbacks: { label: (item) => `${item.dataset.label}: ${item.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 2 })} kWh` },
        },
      },
      scales: {
        x: { stacked: true, grid: { color: grid }, ticks: { color: label, autoSkip: true, maxTicksLimit: 8, maxRotation: 0 } },
        y: { stacked: true, beginAtZero: true, grid: { color: grid }, ticks: { color: label, padding: 6 }, title: chartAxisTitle('kWh', label) },
      },
    },
  });
  consumptionChart = chart;
  host.prepend(consumptionLegend(chart, datasets));
}

async function loadConsumptionHistory(deviceId) {
  const host = $('energy-consumption');
  if (!host) return;
  const generation = ++consumptionGeneration;
  // Drop the previous device's chart right away so it never lingers while the new data loads.
  consumptionChart?.destroy();
  consumptionChart = null;
  $('energy-consumption-status')?.replaceChildren();
  $('energy-consumption-import-status')?.replaceChildren();
  host.replaceChildren();
  setConsumptionVisible(false);
  try {
    const history = await api(`energy/devices/${encodeURIComponent(deviceId)}/consumption-history`);
    if (generation === consumptionGeneration) drawConsumptionChart(history);
  } catch (error) {
    if (generation === consumptionGeneration) {
      setConsumptionVisible(true);
      host.replaceChildren(element('p', 'text-danger small', messageFrom(error)));
    }
  }
}

// A pending plan is re-requested every 10 s, at most PLAN_POLL_MAX times; one timer, cleared by
// every new load (refresh, device change) and when the page is hidden or left.
const PLAN_POLL_MS = 10_000;
const PLAN_POLL_MAX = 30;
const PLAN_REFRESH_MS = 5 * 60 * 1000;
let planPollTimer = null;
let planPollAttempts = 0;
let planLastStatus = null; // a status change restarts the wait; a background refresh does not

function stopPlanPolling() {
  clearTimeout(planPollTimer);
  planPollTimer = null;
}

function armPlanPolling() {
  stopPlanPolling();
  planPollTimer = setTimeout(() => {
    planPollTimer = null;
    if (document.hidden) { armPlanPolling(); return; }
    planPollAttempts += 1;
    void loadDryRunPlan({ poll: true });
  }, pollDelay(PLAN_POLL_MS));
}

async function loadDryRunPlan({ poll = false } = {}) {
  const select = $('energy-plan-device');
  const host = $('energy-plan-result');
  stopPlanPolling();
  if (!poll) { planPollAttempts = 0; planLastStatus = null; }
  if (!poll) {
    clearPlanForecast();
    clearPlanSchedule();
  }
  if (!select || !select.value) {
    host.replaceChildren(element('p', 'text-secondary', 'No inverter is configured.'));
    return;
  }
  const generation = ++planGeneration; // a slower earlier response must not overwrite a newer one
  if (typeof energyExpertMode !== 'undefined' && energyExpertMode) void loadBatteryCalibrationHistory(select.value);
  if (!poll) {
    void loadConsumptionHistory(select.value);
    host.replaceChildren(planWaitState());
  }
  try {
    // Keep the Energy Manager mode current; Off inverters drop out of the plan select.
    // Same endpoint as refreshPlanDevices(): adopting the list also drops removed inverters.
    await api('energy/planning-devices', { background: poll }).then((list) => {
      if (Array.isArray(list)) planDevices = list;
    }).catch(() => { });
    if (generation !== planGeneration) return;
    const before = select.value;
    syncPlanSelection();
    if (!select.value) return; // no active inverter: the tab is hidden and nothing is requested
    if (select.value !== before) { fillPlanLimits(); void loadDryRunPlan(); return; }
    const plan = await api(`energy/devices/${encodeURIComponent(select.value)}/dynamic-plan`, { background: poll });
    if (generation !== planGeneration) return;
    if (plan.status !== planLastStatus) { planLastStatus = plan.status; planPollAttempts = 0; }
    const exhausted = planPollAttempts >= PLAN_POLL_MAX;
    renderDryRunPlan(plan);
    if (PLAN_RETRY_STATUSES.has(plan.status) && !exhausted) armPlanPolling();
  } catch (error) {
    if (generation !== planGeneration) return;
    // A background poll that fails transiently keeps what is shown and keeps a pending plan polling.
    if (poll && planLastStatus !== null) {
      if (PLAN_RETRY_STATUSES.has(planLastStatus) && planPollAttempts < PLAN_POLL_MAX) armPlanPolling();
      return;
    }
    host.replaceChildren(element('p', 'text-danger', messageFrom(error)));
  }
}

function initDryRunPlan() {
  const select = $('energy-plan-device');
  if (!select) return;
  $('energy-plan-diagnostics')?.addEventListener('toggle', drawBatteryCalibrationChart);
  select.addEventListener('change', () => { fillPlanLimits(); void loadDryRunPlan(); });
  $('energy-plan-limits')?.addEventListener('submit', (event) => { void savePlanLimits(event); });
  const planExpertSwitch = $('energy-plan-expert-mode');
  if (planExpertSwitch) {
    planExpertSwitch.checked = energyExpertMode;
    $('energy-plan-advanced').hidden = !energyExpertMode;
    $('energy-plan-limits').hidden = !energyExpertMode;
    $('energy-plan-limits').classList.toggle('d-none', !energyExpertMode);
    planExpertSwitch.addEventListener('change', () => setEnergyExpertMode(planExpertSwitch.checked));
  }
  window.addEventListener('pagehide', stopPlanPolling);
  // The plan follows spot prices and forecasts, so it reloads by itself; no manual refresh needed.
  setInterval(() => { if (!document.hidden && select.value) void loadDryRunPlan({ poll: true }); }, PLAN_REFRESH_MS);
  // Staggered with the other visibility refreshes so returning to the tab is not a request burst.
  document.addEventListener('visibilitychange', () => { if (!document.hidden && select.value) setTimeout(() => void loadDryRunPlan({ poll: true }), 600 + Math.random() * 600); });
  select.addEventListener('focus', () => { refreshPlanDevices().catch(() => { }); });
  refreshPlanDevices().then(() => loadDryRunPlan())
    .catch((error) => $('energy-plan-result').replaceChildren(element('p', 'text-danger', messageFrom(error))));
}

function setEnergyExpertMode(enabled) {
  energyExpertMode = enabled;
  const inverterSwitch = $('energy-expert-mode');
  const planSwitch = $('energy-plan-expert-mode');
  if (inverterSwitch) inverterSwitch.checked = enabled;
  if (planSwitch) planSwitch.checked = enabled;
  const state = $('energy-expert-state');
  if (state) state.textContent = enabled ? 'On' : 'Off';
  const advanced = $('energy-plan-advanced');
  if (advanced) advanced.hidden = !enabled;
  if (enabled && $('energy-plan-device')?.value) void loadBatteryCalibrationHistory($('energy-plan-device').value);
  for (const node of document.querySelectorAll('#energy-plan-result [data-expert-only]')) node.hidden = !enabled;
  const form = $('energy-plan-limits');
  if (form) {
    form.hidden = !enabled;
    form.classList.toggle('d-none', !enabled);
  }
  for (const panel of energyPanels.values()) panel.setExpert(enabled);
}

// The hardware strategy read has its own slower cadence; the main panel stays cache-only.
function initEnergy() {
  let spotMarketInitialized = false;
  let weatherInitialized = false;
  initEnergyTabs((tab) => {
    if (tab === 'spot-market' && !spotMarketInitialized) {
      spotMarketInitialized = true;
      initSpotMarket().catch((error) => toast(messageFrom(error), 'danger'));
    } else if (tab === 'weather' && !weatherInitialized) {
      weatherInitialized = true;
      initWeather().catch((error) => toast(messageFrom(error), 'danger'));
    }
  });
  const expertSwitch = $('energy-expert-mode');
  if (expertSwitch) {
    expertSwitch.checked = energyExpertMode;
    expertSwitch.addEventListener('change', () => setEnergyExpertMode(expertSwitch.checked));
  }
  initDryRunPlan();
  pollEnergy();
  pollHardwareStrategies();
  setInterval(() => { if (!document.hidden) pollHardwareStrategies(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pollEnergy(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) setTimeout(pollHardwareStrategies, 300 + Math.random() * 600); });
}

