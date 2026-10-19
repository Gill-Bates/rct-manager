//
// app/admin/static/js/admin-energy-controls.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  // Business labels for the Energy Manager's own enums (app/energy/models.py); the admin API never
  // sends a register name or a raw dispatch mode, except in the Diagnostics section. These are
  // presentation-only — the REST action names are unchanged on the wire.
  const ENERGY_ACTION_LABELS = {
    charge: 'Charge battery', discharge: 'Discharge battery', hold: 'Keep battery idle',
  };
  const ENERGY_REASON_LABELS = {
    mode_off: 'Energy Manager is off. Select Manual to use battery actions.', external: 'controlled by an external app',
    write_not_permitted: 'write access was revoked',
    limits_missing: 'power limits are not configured', hardware_not_verified: 'hardware is not verified',
    restore_required: 'the inverter must be handed back first',
  };
  // The operating mode per inverter (app/energy/models.py EnergyMode): who may command it.
  const ENERGY_MODES = [
    ['off', 'Off', 'power_settings_new', 'the inverter runs on its own'],
    ['manual', 'Manual', 'touch_app', 'you operate it from this page'],
    ['external', 'External', 'api', 'an external app controls it through the API'],
  ];
  const ENERGY_MODE_TOASTS = {
    off: 'Energy Manager off',
    manual: 'Manual mode: you can operate this inverter from this page.',
    external: 'External mode: an app can now control this inverter through the API.',
  };
  const ENERGY_MODE_TOAST_MANUAL_LOCKED = 'Manual mode is selected, but control stays locked until the setup steps below are complete.';
  // The Setup checklist's "Write access enabled" row is required_write_names (served by the
  // backend, RctDispatchGateway.REQUIRED_WRITES) ⊆ approved_write_names.
  const missingRequiredWrites = (device) => {
    const approved = new Set(device.approved_write_names || []);
    return (device.required_write_names || []).filter((name) => !approved.has(name));
  };
  // The three capabilities that must all be verified before manual control is released.
  const ENERGY_REQUIRED_CAPABILITIES = ['write_path_convention', 'battery_power_sign_convention', 'grid_power_sign_convention'];
  const ENERGY_POLICY_MODES = [['business_target', 'Business target'], ['below_current_soc', 'Below current SoC']];
  const ENERGY_POLL_MS = 3000;
  const ENERGY_POLL_BACKOFF_MS = 30000; // write support off (503) is a steady state: poll rarely
  const ENERGY_POLL_TIMEOUT_MS = 8000;
  const ENERGY_IDLE_WATTS = 20; // below this a flow counts as standing still
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const energyPanels = new Map();
  const energyHardwareStrategies = new Map();
  let energyPolling = false;
  let energyPollDelay = ENERGY_POLL_MS;
  let energyPollTimer = null;
  // Logical clock ordering poll requests against finished panel actions, so a poll requested before
  // an action completed never overwrites its result.
  let energyClock = 0;
  let energyNote = null;
  let energyExpertMode = false; // page-wide display switch; never persisted, resets on every load

  function svgEl(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function readingValue(reading) {
    return reading && reading.value !== null && reading.value !== undefined && Number.isFinite(Number(reading.value))
      ? Number(reading.value) : null;
  }

  // A stale or absent figure is treated as unknown (used by the Energy Manager panel).
  function liveValue(reading) {
    return reading && !reading.stale ? readingValue(reading) : null;
  }

  function formatPower(watts) {
    if (!Number.isFinite(watts)) return NOT_AVAILABLE;
    const abs = Math.abs(watts);
    return abs >= 1000 ? `${(abs / 1000).toFixed(2)} kW` : `${Math.round(abs)} W`;
  }

  function formatPercent(value) {
    return Number.isFinite(value) ? `${Math.round(value)} %` : NOT_AVAILABLE;
  }

  const FLOW_KINDS = ['flow-pv', 'flow-export', 'flow-discharge', 'flow-import', 'flow-charge'];

  // The single definition of "active" for the flow graphic: node icon, legend icon and line all
  // follow it. Each unit is a colour kind (see .flow-line.flow-* in admin.css) or null = inactive/unknown.
  function energyFlowActivity(readings) {
    const idle = ENERGY_IDLE_WATTS;
    const pv = readingValue(readings.pv_power_w);
    const grid = readingValue(readings.grid_power_w);
    const battery = readingValue(readings.battery_power_w);
    const house = readingValue(readings.house_load_w);
    return {
      pv: pv !== null && pv >= idle ? 'flow-pv' : null,
      grid: grid === null || Math.abs(grid) < idle ? null : grid > 0 ? 'flow-import' : 'flow-export',
      battery: battery === null || Math.abs(battery) < idle ? null : battery > 0 ? 'flow-discharge' : 'flow-charge',
      house: house !== null && house >= idle ? (grid !== null && grid >= idle ? 'flow-import' : 'flow-pv') : null,
    };
  }

  function energyFlowGraphic({ lite = false } = {}) {
    const svg = svgEl('svg', { viewBox: lite ? '0 145 420 125' : '0 0 420 270', class: 'energy-flow-svg', role: 'img' });
    const lines = {};
    // Each path runs in the direction of the "positive" flow; a negative value reverses the animation.
    const lineDefs = [
      ['pv', 'M210 114 V154', 210, 149, 210, 119, 90],
      ['grid', 'M94 190 H172', 167, 190, 99, 190, 0],
      ['battery', 'M326 190 H248', 253, 190, 321, 190, 180],
    ];
    for (const [key, d, forwardX, forwardY, reverseX, reverseY, angle] of lineDefs.filter(([key]) => !lite || key !== 'pv')) {
      const group = svgEl('g', { class: 'flow-line is-idle' });
      const track = svgEl('path', { class: 'flow-track', d });
      const dots = svgEl('path', { class: 'flow-dots', d });
      const arrow = svgEl('polygon', { class: 'flow-arrow', points: '-4,-3 4,0 -4,3' });
      group.append(track, dots, arrow);
      svg.append(group);
      lines[key] = { group, dots, arrow, forwardX, forwardY, reverseX, reverseY, angle };
    }
    const nodes = {};
    // Single private-use codepoints, not ligature names: SVG text-anchor measures the unshaped
    // string in WebKit and would shift a multi-letter ligature off its circle.
    const nodeDefs = [
      ['pv', 210, 78, '\ue430', 'PV', 'top'],
      ['grid', 56, 190, '\uebbc', 'Grid', 'bottom'],
      ['house', 210, 190, '\ue88a', 'House', 'bottom'],
      ['battery', 364, 190, '\ue1a3', 'Battery', 'bottom'],
    ];
    for (const [key, cx, cy, icon, label, place] of nodeDefs.filter(([key]) => !lite || key !== 'pv')) {
      const group = svgEl('g', { class: 'flow-node' });
      group.append(svgEl('circle', { class: 'flow-node-ring', cx, cy, r: 34 }));
      group.append(svgEl('text', { class: 'flow-node-icon', x: cx, y: cy, 'aria-hidden': 'true' }, icon));
      const top = place === 'top';
      group.append(svgEl('text', { class: 'flow-node-label', x: cx, y: top ? 16 : cy + 50 }, label));
      const value = svgEl('text', { class: 'flow-node-value', x: cx, y: top ? 38 : cy + 74 }, NOT_AVAILABLE);
      group.append(value);
      svg.append(group);
      nodes[key] = { group, label: group.querySelector('.flow-node-label'), baseLabel: label, value };
    }

    // Four state badges under the graphic; derived in update() from the same readings and idle threshold.
    const badges = element('div', 'flow-badges');
    badges.setAttribute('role', 'group');
    badges.setAttribute('aria-label', 'Energy status');
    const badgeNodes = {};
    for (const [key, icon, caption] of [
      ['generation', 'wb_sunny', 'Generation'], ['consumption', 'home', 'Consumption'],
      ['grid', 'factory', 'Grid'], ['battery', 'battery_charging_full', 'Battery'],
    ]) {
      const node = element('div', 'flow-badge');
      node.hidden = true;
      const iconNode = element('span', 'material-icons flow-badge-icon', icon);
      iconNode.setAttribute('aria-hidden', 'true');
      const state = element('span', 'flow-badge-state status-badge status-badge-neutral');
      node.append(iconNode, element('span', 'flow-badge-caption', caption), state);
      badges.append(node);
      badgeNodes[key] = { node, state, caption };
    }

    // state is [text, favourable] or null when the reading is unknown (badge hidden, no invented state).
    // Tone: favourable -> success, everything else -> neutral; flow states are never faults.
    //   Generation:  Generating success | No generation neutral
    //   Consumption: Independent success | Grid supplied neutral
    //   Grid:        Feed-in success | Idle neutral | Import neutral
    //   Battery:     Charging success | Discharging neutral | Idle neutral
    //   Any badge:   n/a (reading expired) neutral
    // `expired`: the reading ran out, so the badge reads n/a instead of vanishing (the row keeps its height).
    function setBadge(key, state, stale, expired = false) {
      const badge = badgeNodes[key];
      const shown = Boolean(state) || expired;
      if (badge.node.hidden !== !shown) badge.node.hidden = !shown;
      if (!shown) return;
      const [text, favourable] = state || ['n/a', false];
      setText(badge.state, text);
      setClass(badge.state, 'status-badge-success', favourable === true);
      setClass(badge.state, 'status-badge-neutral', favourable !== true);
      const label = `${badge.caption}: ${text}`;
      if (badge.node.getAttribute('aria-label') !== label) badge.node.setAttribute('aria-label', label);
    }

    // Dot speed is the same constant on every line (the energy-flow-dash animation in admin.css); only direction and colour follow the power.
    function setLine(key, watts, positiveKind, negativeKind) {
      const line = lines[key];
      const active = Number.isFinite(watts) && Math.abs(watts) >= ENERGY_IDLE_WATTS;
      setClass(line.group, 'is-idle', !active);
      if (!active) return;
      const reverse = watts < 0;
      setClass(line.group, 'is-reverse', reverse);
      for (const kind of new Set([positiveKind, negativeKind])) setClass(line.group, kind, kind === (reverse ? negativeKind : positiveKind));
      const x = reverse ? line.reverseX : line.forwardX;
      const y = reverse ? line.reverseY : line.forwardY;
      line.arrow.setAttribute('transform', `translate(${x} ${y}) rotate(${line.angle + (reverse ? 180 : 0)})`);
    }

    // Active units show their colour kind; inactive ones fall back to the neutral grey in admin.css.
    function setActive(node, kind) {
      setClass(node, 'is-active', Boolean(kind));
      for (const k of FLOW_KINDS) setClass(node, k, k === kind);
    }

    function setNode(key, text, stale, labelSuffix = '') {
      const node = nodes[key];
      node.value.textContent = text;
      node.label.textContent = labelSuffix ? `${node.baseLabel} · ${labelSuffix}` : node.baseLabel;
      setClass(node.group, 'is-stale', stale);
    }

    function update(readings) {
      const r = readings;
      const pv = readingValue(r.pv_power_w);
      const grid = readingValue(r.grid_power_w);
      const battery = readingValue(r.battery_power_w);
      const house = readingValue(r.house_load_w);
      const soc = readingValue(r.battery_soc_percent);

      // Same value and threshold as the node labels below; a stale reading is only dimmed, never
      // dropped, so the animation cannot disagree with a "Charging" or "Import" label.
      const activity = energyFlowActivity(r);
      if (!lite) setLine('pv', activity.pv ? pv : 0, 'flow-pv', 'flow-pv');
      setLine('grid', grid, 'flow-import', 'flow-export');
      setLine('battery', battery, 'flow-discharge', 'flow-charge');

      const gridWord = grid === null ? '' : Math.abs(grid) < ENERGY_IDLE_WATTS ? 'Idle' : grid > 0 ? 'Import' : 'Export';
      const batteryWord = battery === null ? '' : Math.abs(battery) < ENERGY_IDLE_WATTS ? 'Idle' : battery > 0 ? 'Discharging' : 'Charging';
      if (!lite) setNode('pv', formatPower(pv), !!r.pv_power_w.stale);
      setNode('grid', formatPower(grid), !!r.grid_power_w.stale);
      setNode('house', formatPower(house), !!r.house_load_w.stale);
      setNode('battery', formatPower(battery), !!r.battery_power_w.stale,
        soc === null ? '' : formatPercent(soc));
      for (const key of Object.keys(nodes)) setActive(nodes[key].group, activity[key]);
      const idle = ENERGY_IDLE_WATTS;
      if (!lite) {
        for (const [key, unit] of [['generation', 'pv'], ['consumption', 'house'], ['grid', 'grid'], ['battery', 'battery']]) {
          setActive(badgeNodes[key].node, activity[unit]);
        }
        setBadge('generation', pv === null ? null : pv >= idle ? ['Generating', true] : ['No generation', false], !!r.pv_power_w.stale, !!r.pv_power_w.expired);
        setBadge('consumption', grid === null ? null : grid < idle ? ['Independent', true] : ['Grid supplied', false], !!r.grid_power_w.stale, !!r.grid_power_w.expired);
        setBadge('grid', grid === null ? null : grid <= -idle ? ['Feed-in', true] : grid < idle ? ['Idle', true] : ['Import', false], !!r.grid_power_w.stale, !!r.grid_power_w.expired);
        setBadge('battery', battery === null ? null : battery <= -idle ? ['Charging', true] : battery >= idle ? ['Discharging', false] : ['Idle', false], !!r.battery_power_w.stale, !!r.battery_power_w.expired);
      }
      svg.setAttribute('aria-label', [
        ...(!lite ? [`PV ${formatPower(pv)}`] : []),
        `grid ${grid === null ? NOT_AVAILABLE : `${formatPower(grid)} ${gridWord.toLowerCase()}`}`,
        `battery ${battery === null ? NOT_AVAILABLE : `${formatPower(battery)} ${batteryWord.toLowerCase()}`}`,
        `house ${formatPower(house)}`,
      ].join(', '));
    }

    return { svg, badges, update };
  }

  function energyAvailability(device, action) {
    return (device.actions || []).find((item) => item.action === action) || { available: false, reason: null };
  }

  // The state-independent readiness checklist (design §12). Every row is derived from a field that
  // is authoritative in every mode, so a device that is switched off still reports it correctly
  // (unlike device.actions[].reason, which collapses to mode_off while it is off). Shared by the
  // Operate "needs setup" banner (§9) and the Setup block renderer (§12).
  function energyChecklist(device) {
    const writeAccess = device.write_support_enabled !== false && missingRequiredWrites(device).length === 0;
    const verified = ENERGY_REQUIRED_CAPABILITIES.every((name) => {
      const cap = (device.capabilities || []).find((item) => item.name === name);
      return cap && cap.status === 'verified';
    });
    return {
      connected: Boolean(device.connected),
      limits: device.limits !== null && device.limits !== undefined,
      writeAccess,
      hardwareVerified: verified,
    };
  }

  // The first unmet checklist item in the fixed order the Setup CTA targets (design §12).
  function energyFirstUnmet(checklist) {
    if (!checklist.connected) return 'connection';
    if (!checklist.writeAccess) return 'write_access';
    if (!checklist.limits) return 'limits';
    if (!checklist.hardwareVerified) return 'hardware';
    return null;
  }

  // "Needs setup" is any unmet item among write-access / power-limits / hardware-verified (connection
  // is handled by its own branch). Used to place the §9 banner above the mode branches.
  function energyNeedsSetup(checklist) {
    return !checklist.limits || !checklist.writeAccess || !checklist.hardwareVerified;
  }

  // The one human-readable Operate status (design §9). No raw register/capability text or
  // reject_detail — those live only in Diagnostics. `poll503` renders the config empty-state.
  function energyControlState(device, poll503, offView) {
    if (poll503) {
      return { ready: false, kind: 'config', text: 'Manual battery control requires write support to be enabled.', detail: '' };
    }
    if (!device.connected) {
      return { ready: false, kind: 'blocked', text: 'Not connected.', detail: '' };
    }
    if (device.state === 'fault') {
      return { ready: false, kind: 'blocked', text: 'The inverter reports a fault.', detail: '' };
    }
    const checklist = energyChecklist(device);
    if (energyNeedsSetup(checklist)) {
      return { ready: false, kind: 'setup', text: 'Manual control needs setup.', detail: '', firstUnmet: energyFirstUnmet(checklist) };
    }
    if (device.mode === 'off') {
      if (!device.mode_recorded) {
        return { ready: false, kind: 'disabled', text: 'No Energy Manager mode has been confirmed for this inverter. Select Off to enable independent operation.', detail: '' };
      }
      if (offView.mismatch) {
        return { ready: false, kind: 'disabled', text: 'The inverter does not report the Internal strategy. Select Off again to restore independent operation.', detail: '' };
      }
      return { ready: false, kind: 'disabled', text: '', detail: '' };
    }
    if (device.mode === 'external') {
      return { ready: false, kind: 'external', text: 'This inverter is controlled by an external app through the API (PAT required).', detail: '' };
    }
    return { ready: true, kind: 'ready', text: '', detail: '' };
  }

  // Splits the recorded control state (application truth) from the live inverter telemetry. The
  // /hardware-strategies endpoint serves only fresh cache values, so a missing value (code null)
  // means "unknown right now" and must never read as a mismatch.
  function energyOffView(device, strategy) {
    const recorded = device.mode === 'off' && device.mode_recorded === true;
    const fresh = Boolean(strategy) && strategy.stale === false && Number.isInteger(strategy.code);
    return { recorded, observed: recorded && fresh && strategy.code === 4, mismatch: recorded && fresh && strategy.code !== 4 };
  }

  // A failed live poll is actionable; the normal cache age is intentionally not shown here.
  const energyFreshnessNote = (pollFailed) => pollFailed ? 'Live data unavailable.' : '';

  function energyTargetRange(device, action) {
    // A stale SoC must not narrow the slider; only the server window applies then.
    const soc = liveValue(device.readings.battery_soc_percent);
    const window = device.target_soc_window;
    let lo = Math.ceil(window.min);
    let hi = Math.floor(window.max);
    if (soc !== null && action === 'charge') lo = Math.max(lo, Math.floor(soc) + 1);
    if (soc !== null && action === 'discharge') hi = Math.min(hi, Math.ceil(soc) - 1);
    return { lo, hi, valid: lo <= hi };
  }

  function createEnergyPanel(first, deviceCount) {
    const id = first.device_id;
    const path = `energy/devices/${encodeURIComponent(id)}`;
    let device = first;
    let selected = null; // 'charge' | 'discharge' while its target slider is open
    let busy = false;
    let pendingMode = null;    // the user's mode choice while its PUT is in flight
    let actionFinishedAt = 0;  // energyClock tick of the last finished action
    let lastPoll503 = false;   // the Operate poll returned 503 (write support disabled) — §9.2
    let lastPollFailed = false; // a non-503 transient poll failure — §13 freshness note
    let hardwareStrategy = null;
    const touched = { charge: false, discharge: false };

    const card = element('section', 'card energy-panel');
    const body = element('div', 'card-body');
    card.append(body);

    // Header: name, connection, master switch.
    const head = element('div', 'energy-head mb-3');
    const title = element('div', 'energy-head-title');
    const nameNode = element('h2', 'h5 mb-0');
    const dot = element('span', 'status-dot');
    const connection = element('span', 'small text-secondary');
    const collapseKey = `energy:${id}`; // own namespace: collapsing here must not collapse the dashboard card
    const collapseToggle = element('button', 'device-toggle');
    collapseToggle.type = 'button';
    const collapseIcon = element('span', 'material-icons', 'expand_more');
    collapseIcon.setAttribute('aria-hidden', 'true');
    collapseToggle.append(collapseIcon);
    title.append(collapseToggle, nameNode, dot, connection);
    // Three-state radio group; arrow keys only move the focus, Space/Enter/click selects, so
    // passing over a mode never switches the inverter.
    const modeGroup = element('div', 'energy-mode-switch');
    modeGroup.setAttribute('role', 'radiogroup');
    const modeEvidence = element('p', 'small text-secondary energy-mode-evidence mb-0 w-100');
    modeEvidence.setAttribute('role', 'status');
    const modeButtons = {};
    for (const [value, label, icon, hint] of ENERGY_MODES) {
      const button = element('button', 'energy-mode-option');
      button.type = 'button';
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-label', `${label}: ${hint}`);
      button.title = hint;
      const glyph = element('span', 'material-icons', icon);
      glyph.setAttribute('aria-hidden', 'true');
      button.append(glyph, element('span', null, label));
      modeButtons[value] = button;
      modeGroup.append(button);
    }
    // Visible reason why the mode buttons are disabled while the inverter is not connected.
    const modeOffline = element('p', 'small text-secondary energy-mode-offline mb-0 w-100',
      'The inverter is not connected. Operating mode and battery actions are unavailable until it reconnects.');
    modeOffline.setAttribute('role', 'status');
    modeOffline.hidden = true;
    head.append(title, modeGroup, modeOffline, modeEvidence);
    body.append(head);

    const layout = element('div', 'energy-body');
    const control = element('div', 'energy-control');
    const liteFlow = energyFlowGraphic({ lite: true });
    const liteBox = element('div', 'energy-flow-lite');
    liteBox.append(liteFlow.svg);
    layout.append(control);
    const content = element('div', 'energy-content'); // everything below the header folds away
    content.id = `energy-content-${++deviceCardCount}`;
    content.append(layout);
    body.append(content);
    collapseToggle.setAttribute('aria-controls', content.id);
    function setCollapsed(collapsed) {
      content.hidden = collapsed;
      setClass(card, 'is-collapsed', collapsed);
      collapseToggle.setAttribute('aria-expanded', String(!collapsed));
    }
    let collapsedNow = isCollapsed(collapseKey, deviceCount);
    setCollapsed(collapsedNow);
    collapseToggle.addEventListener('click', () => {
      collapsedNow = !collapsedNow;
      rememberCollapsed(collapseKey, collapsedNow);
      setCollapsed(collapsedNow);
    });

    // The state-independent Setup checklist (design §12), shown only when a prerequisite is unmet.
    const modeNote = element('p', 'small text-secondary energy-mode-note');
    const modeLink = element('a', null, 'Inverters page');
    modeLink.href = '/ui/inverters';
    modeNote.append('Write access is switched off, so Manual and External cannot be selected yet. Turn it on on the ', modeLink, '.');
    modeNote.hidden = true;
    control.append(modeNote);

    const setupBox = element('div', 'energy-setup');
    setupBox.hidden = true;
    const setupHeading = element('h3', 'h6 mb-2', 'Manual battery control setup');
    const setupList = element('ul', 'energy-setup-list');
    const setupStep = element('div', 'energy-setup-step');
    setupStep.setAttribute('aria-live', 'polite');
    // States that the selected mode is not in effect yet, so the switch is not read as active.
    const setupLead = element('p', 'small fw-semibold energy-setup-lead mb-2');
    setupLead.setAttribute('role', 'status');
    setupLead.hidden = true;
    setupBox.append(setupHeading, setupLead, setupList, setupStep);
    control.append(setupBox);

    const statusBox = element('div', 'energy-status');
    const statusIcon = element('span', 'material-icons');
    statusIcon.setAttribute('aria-hidden', 'true');
    const statusText = element('div');
    const statusMain = element('div', 'fw-semibold');
    const statusDetail = element('div', 'small text-secondary');
    statusText.append(statusMain, statusDetail);
    statusBox.append(statusIcon, statusText);

    const modeBox = element('div', 'energy-mode');
    const freshness = element('p', 'small text-secondary energy-freshness mb-0');
    freshness.hidden = true;
    modeBox.append(freshness);

    const actions = element('div', 'energy-actions');
    const buttons = {};
    for (const action of ['charge', 'hold', 'discharge']) {
      const button = element('button', 'btn btn-primary', ENERGY_ACTION_LABELS[action]);
      button.type = 'button';
      buttons[action] = button;
      actions.append(button);
    }

    const target = element('div', 'energy-target');
    target.hidden = true;
    const targetLabel = element('label', 'form-label d-flex justify-content-between mb-1');
    const targetName = element('span', null, 'Target SoC');
    const targetOutput = element('output', 'fw-semibold');
    targetLabel.append(targetName, targetOutput);
    const range = element('input', 'form-range');
    range.type = 'range';
    range.step = '1';
    range.id = `energy-target-${id}`;
    targetLabel.htmlFor = range.id;
    const scale = element('div', 'energy-target-scale');
    const scaleLo = element('span');
    const scaleHi = element('span');
    scale.append(scaleLo, scaleHi);
    const targetNote = element('p', 'small text-secondary mb-0');
    targetNote.hidden = true;
    const confirm = element('button', 'btn btn-primary mt-2 w-100');
    confirm.type = 'button';
    target.append(targetLabel, range, scale, targetNote, confirm);

    const operate = element('div', 'energy-operate');
    const operateMain = element('div', 'energy-operate-main');
    operateMain.append(modeBox, actions);
    operate.append(operateMain, liteBox);
    control.append(statusBox, operate, target);

    const advanced = energyAdvanced(id, () => device, (next) => panel.update(next));
    let expertOn = energyExpertMode;

    function setBusy(value) {
      busy = value;
      if (!value) actionFinishedAt = ++energyClock;
      render();
    }

    // 202 {status_available: false}: the action ran but the status projection failed, so the body
    // carries no panel state. Poll the status instead of rendering it.
    async function applyActionResult(result) {
      if (result && result.status_available === false) {
        try {
          const list = await api('energy/devices');
          const fresh = list.find((item) => item.device_id === id);
          if (fresh) panel.update(fresh);
        } catch (error) { /* the regular poll refreshes the panel */ }
        return;
      }
      panel.update(result);
    }

    async function send(payload) {
      setBusy(true);
      try {
        const result = await api(`${path}/command`, { method: 'POST', body: JSON.stringify(payload) });
        selected = null;
        touched.charge = false;
        touched.discharge = false;
        await applyActionResult(result);
        toast('Command sent.');
      } catch (error) { toast(messageFrom(error), 'danger'); }
      finally { setBusy(false); }
    }

    function offView() { return energyOffView(device, hardwareStrategy); }

    async function chooseMode(wanted) {
      // aria-disabled instead of disabled keeps the keyboard focus on the group while a PUT runs.
      if (busy || lastPoll503 || !device.connected || (wanted === device.mode && (wanted !== 'off' || (offView().recorded && !offView().mismatch)))) return;
      pendingMode = wanted;
      setBusy(true);
      try {
        const result = await api(`${path}/mode`, { method: 'PUT', body: JSON.stringify({ mode: wanted }) });
        pendingMode = null;
        // Manual with an unfinished setup stays locked; the toast must not promise otherwise.
        const locked = wanted === 'manual' && energyNeedsSetup(energyChecklist(device));
        toast(locked ? ENERGY_MODE_TOAST_MANUAL_LOCKED : ENERGY_MODE_TOASTS[wanted]);
        selected = null;
        if (wanted === 'off') hardwareStrategy = { code: 4, label: 'Internal', stale: false };
        await applyActionResult(result);
        if (wanted === 'off') pollHardwareStrategies();
      } catch (error) { toast(messageFrom(error), 'danger'); }
      finally { pendingMode = null; setBusy(false); }
    }
    const modeValues = ENERGY_MODES.map(([value]) => value);
    for (const value of modeValues) {
      modeButtons[value].addEventListener('click', () => chooseMode(value));
      modeButtons[value].addEventListener('keydown', (event) => {
        const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
        let next = null;
        if (step !== undefined) next = modeValues[(modeValues.indexOf(value) + step + modeValues.length) % modeValues.length];
        else if (event.key === 'Home') next = modeValues[0];
        else if (event.key === 'End') next = modeValues[modeValues.length - 1];
        if (next === null) return;
        event.preventDefault();
        modeButtons[next].focus();
      });
    }

    // Off is operational rest: a disabled button already blocks the click, this guards synthetic events.
    const actionsLocked = () => !device.connected || device.mode === 'off';
    buttons.hold.addEventListener('click', () => { if (!actionsLocked()) send({ action: 'hold' }); });
    for (const action of ['charge', 'discharge']) {
      buttons[action].addEventListener('click', () => {
        if (actionsLocked()) return;
        selected = selected === action ? null : action;
        render();
      });
    }
    range.addEventListener('input', () => { if (selected) touched[selected] = true; renderTarget(); });
    confirm.addEventListener('click', () => {
      if (!selected || actionsLocked()) return;
      send({ action: selected, target_soc_percent: Number(range.value) });
    });

    function renderTarget() {
      target.hidden = selected === null;
      if (selected === null) return;
      const limits = energyTargetRange(device, selected);
      const soc = readingValue(device.readings.battery_soc_percent);
      range.hidden = scale.hidden = !limits.valid;
      targetNote.hidden = limits.valid;
      confirm.hidden = !limits.valid;
      targetName.textContent = selected === 'charge' ? 'Charge up to' : 'Discharge down to';
      if (!limits.valid) {
        targetOutput.textContent = '';
        targetNote.textContent = selected === 'charge'
          ? `The battery (${formatPercent(soc)}) is already at the highest allowed target.`
          : `The battery (${formatPercent(soc)}) is already at the lowest allowed target.`;
        return;
      }
      range.min = String(limits.lo);
      range.max = String(limits.hi);
      const current = Number(range.value);
      if (!touched[selected] || !(current >= limits.lo && current <= limits.hi)) {
        const wanted = selected === 'charge' ? 80 : 20;
        range.value = String(Math.min(Math.max(wanted, limits.lo), limits.hi));
      }
      scaleLo.textContent = `${limits.lo} %`;
      scaleHi.textContent = `${limits.hi} %`;
      targetOutput.textContent = `${range.value} %`;
      confirm.textContent = `${ENERGY_ACTION_LABELS[selected]} to ${range.value} %`;
      confirm.disabled = busy;
    }

    // Which Setup rows the checklist renders, in display order.
    const SETUP_ROWS = [
      ['connected', 'Inverter connected'],
      ['writeAccess', 'Write access'],
      ['limits', 'Power limits'],
      ['hardwareVerified', 'Hardware verification'],
    ];

    // Shows only the editor of the first unmet step; the rest of the checklist stays a status list.
    function renderSetup(checklist) {
      const unmet = SETUP_ROWS.some(([key]) => !checklist[key]);
      setupBox.hidden = !unmet;
      if (!unmet) { if (!advanced.busy()) advanced.layout(null); return; }
      setupList.replaceChildren();
      for (const [key, label] of SETUP_ROWS) {
        const met = checklist[key];
        const verification = key === 'hardwareVerified' ? device.verification_state : null;
        const stateLabel = verification === 'checking' ? ' · checking automatically'
          : verification === 'retrying' ? ' · retry scheduled'
            : verification === 'blocked' ? ' · needs attention'
              : verification === 'pending' ? ' · waiting for a safe time' : '';
        const row = element('li', `energy-setup-item ${met ? 'is-met' : 'is-unmet'}`);
        const icon = element('span', 'material-icons', met ? 'check_circle'
          : verification === 'checking' || verification === 'retrying' || verification === 'pending'
            ? 'hourglass_empty' : 'radio_button_unchecked');
        icon.setAttribute('aria-hidden', 'true');
        row.append(icon, element('span', null, `${label}${stateLabel}`));
        setupList.append(row);
      }
      if (advanced.busy()) return; // a save is running; the step moves on once it finished
      const first = energyFirstUnmet(checklist);
      advanced.layout(first);
      const node = advanced.step(first);
      if (setupStep.firstChild !== node) {
        const moved = setupStep.firstChild !== null;
        setupStep.replaceChildren(...(node ? [node] : []));
        // Keyboard and screen-reader users land on the new step instead of a vanished form.
        const heading = moved && node && node.querySelector('h4');
        if (heading) { heading.tabIndex = -1; heading.focus(); }
      }
    }

    function render() {
      const readyState = energyControlState(device, lastPoll503, offView());
      // When the inverter is not connected its name is shown as n/a (the real name still backs the
      // aria-labels so assistive tech keeps the device identity; the status is announced separately).
      nameNode.textContent = device.connected ? device.device_name : NOT_AVAILABLE;
      collapseToggle.setAttribute('aria-label', `Show or hide details of ${device.device_name}`);
      dot.className = `status-dot ${device.connected ? 'online' : 'offline'}`;
      connection.textContent = `${device.connected ? 'Connected' : 'Not connected'} · ${device.host}`;
      const shownMode = pendingMode === 'off' ? null
        : pendingMode ?? (device.mode === 'off' && !offView().recorded ? null : device.mode);
      modeGroup.setAttribute('aria-label', `Operating mode of ${device.device_name}`);
      modeOffline.hidden = device.connected;
      for (const value of modeValues) {
        const button = modeButtons[value];
        const checked = value === shownMode;
        button.disabled = !device.connected;
        button.title = device.connected ? ENERGY_MODES.find(([key]) => key === value)[3] : 'The inverter is not connected.';
        button.setAttribute('aria-checked', String(checked));
        button.setAttribute('aria-disabled', String(busy || lastPoll503));
        button.tabIndex = checked || (shownMode === null && value === 'off') ? 0 : -1;
        setClass(button, 'is-selected', checked);
      }
      const strategyName = hardwareStrategy?.label || (Number.isInteger(hardwareStrategy?.code)
        ? `Unknown (code ${hardwareStrategy.code})` : null);
      const off = offView();
      if (device.mode === 'off' && !device.mode_recorded) {
        modeEvidence.textContent = `Not yet managed. Inverter SOC strategy: ${strategyName || 'unavailable'}.`;
      } else if (off.mismatch) {
        modeEvidence.textContent = `Off is recorded, but the inverter reports SOC strategy ${strategyName}. Select Off again to hand control back.`;
      } else if (off.observed) {
        modeEvidence.textContent = 'Internal strategy currently observed.';
      } else {
        modeEvidence.textContent = '';
      }
      modeEvidence.hidden = !modeEvidence.textContent;
      modeNote.hidden = !(device.write_support_enabled === false && device.mode === 'off');

      renderSetup(energyChecklist(device));
      // Selected but blocked by an unfinished setup: show it as pending, not as effective.
      const pendingSetup = (shownMode === 'manual' || shownMode === 'external')
        && device.connected && !lastPoll503 && energyNeedsSetup(energyChecklist(device));
      for (const value of modeValues) setClass(modeButtons[value], 'is-pending', pendingSetup && value === shownMode);
      setupLead.hidden = !pendingSetup;
      if (pendingSetup) {
        const modeLabel = ENERGY_MODES.find(([value]) => value === shownMode)[1];
        setupLead.textContent = readyState.firstUnmet === 'hardware'
          ? `${modeLabel} is selected. Hardware verification runs in the background; battery control unlocks when it completes.`
          : `${modeLabel} is selected but not active yet. Battery control stays locked until every step below is done.`;
      }

      statusBox.className = `energy-status ${readyState.ready ? 'is-ready' : 'is-blocked'}`;
      // Nothing when healthy; the Setup block replaces the "needs setup" line.
      statusBox.hidden = readyState.ready || readyState.kind === 'setup' || !readyState.text;
      statusIcon.textContent = 'info';
      statusMain.textContent = readyState.text;
      statusDetail.textContent = readyState.detail || '';
      statusDetail.hidden = !readyState.detail;

      const operable = readyState.ready;
      const external = readyState.kind === 'external';
      modeBox.hidden = true;
      if (operable || external) {
        const note = energyFreshnessNote(lastPollFailed);
        freshness.textContent = note;
        freshness.hidden = !note;
        modeBox.hidden = !note;
      }

      // Actions + target are usable only once ready.
      actions.hidden = !(operable || external || readyState.kind === 'disabled');
      operate.hidden = actions.hidden;
      liteBox.hidden = actions.hidden;
      if (!liteBox.hidden) liteFlow.update(device.readings);
      for (const action of ['charge', 'hold', 'discharge']) {
        const item = energyAvailability(device, action);
        const enabled = operable && item.available && !busy && device.connected && device.mode !== 'off';
        buttons[action].disabled = !enabled;
        setClass(buttons[action], 'is-inactive', !enabled);
        const reason = device.mode === 'off' ? 'mode_off' : external ? 'external' : item.reason;
        buttons[action].title = enabled ? '' : (ENERGY_REASON_LABELS[reason] || '');
        setClass(buttons[action], 'is-selected', selected === action);
        buttons[action].setAttribute('aria-pressed', action === 'charge' || action === 'discharge' ? String(selected === action) : 'false');
      }
      if (!operable && selected) selected = null;
      if (selected && buttons[selected].disabled) selected = null;
      renderTarget();
      advanced.update(device, expertOn, lastPoll503);
      if (expertOn !== (advanced.expert.parentNode === content)) {
        if (expertOn) content.append(advanced.expert); else advanced.expert.remove();
      }
    }

    const panel = {
      root: card,
      update(next, flags) {
        device = next;
        if (flags) { lastPoll503 = Boolean(flags.poll503); lastPollFailed = Boolean(flags.pollFailed); }
        render();
      },
      // Lets pollEnergy signal a 503/transient failure without a fresh device payload.
      setPollState(flags) { lastPoll503 = Boolean(flags.poll503); lastPollFailed = Boolean(flags.pollFailed); render(); },
      setTimestamp(text) { advanced.setTimestamp(text); },
      setHardwareStrategy(value) { hardwareStrategy = value; render(); },
      isConnected() { return Boolean(device.connected); },
      // False while an action runs or when the poll was requested before the last action finished.
      acceptsPoll(requestedAt) { return !busy && requestedAt > actionFinishedAt; },
      // Display only: no request, no stored value.
      setExpert(on) { expertOn = Boolean(on); render(); },
    };
    render();
    return panel;
  }

