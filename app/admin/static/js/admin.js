//
// app/admin/static/js/admin.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
  'use strict';

  const page = document.body.dataset.page;
  const $ = (id) => document.getElementById(id);
  let csrfToken = '';

  // 'en-GB' matches the e2e harness locale, so "Saved HH:MM" / "Updated HH:MM" stay assertable.
  const hhmm = (date) => date.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  function toast(message, kind = 'success') {
    const region = $('toast-region');
    // An identical message already showing (or still sliding out) is suppressed instead of
    // stacking a visual duplicate, e.g. two call sites reporting the same failed request.
    const duplicate = [...region.children].some((node) => node.dataset.message === message && node.dataset.kind === kind);
    if (duplicate) return;
    const box = element('div', `alert alert-${kind}`, message);
    box.dataset.message = message;
    box.dataset.kind = kind;
    region.append(box);
    requestAnimationFrame(() => box.classList.add('is-visible'));
    const remove = () => box.remove();
    const leave = () => {
      box.classList.remove('is-visible');
      box.classList.add('is-leaving');
      box.addEventListener('transitionend', remove, { once: true });
      setTimeout(remove, 400); // fallback if the transition does not fire (e.g. display: none ancestor)
    };
    setTimeout(leave, kind === 'danger' ? 8500 : 3500);
  }

  function messageFrom(error) {
    return error instanceof Error ? error.message : 'An unknown error occurred.';
  }

  // navigator.clipboard requires a secure context (HTTPS or localhost) and is undefined on a
  // plain-HTTP LAN deployment, so fall back to the classic textarea + execCommand('copy') trick.
  async function copyToClipboard(text) {
    if (navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch { /* fall through to the legacy fallback below */ }
    }
    const textarea = element('textarea');
    textarea.value = text;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '0';
    textarea.style.opacity = '0';
    document.body.append(textarea);
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    let copied = false;
    try { copied = document.execCommand('copy'); } catch { copied = false; }
    textarea.remove();
    return copied;
  }

  async function api(path, options = {}) {
    const method = options.method || 'GET';
    const response = await fetch(`/admin/api/${path}`, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...(method !== 'GET' ? { 'X-CSRF-Token': csrfToken } : {}),
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && !['login', 'change-password'].includes(page)) {
        location.assign('/login');
      }
      const detail = data.detail;
      throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}).`);
    }
    return data;
  }

  async function session() {
    const data = await api('session');
    csrfToken = data.csrf_token || '';
    authInitReported = false; // a later failure in this page session must announce itself again
    return data;
  }

  let sessionPromise = null;

  function ensureSession() {
    if (!sessionPromise) {
      // Cache the in-flight request only. A rejected promise is truthy, so caching it would make
      // every later submit await the same failure and never issue a second request.
      sessionPromise = session().catch((error) => { sessionPromise = null; throw error; });
    }
    return sessionPromise;
  }

  function submitState(form, busy) {
    form.querySelector('button[type="submit"]').disabled = busy;
  }

  function formError(message) {
    const box = $('form-error');
    box.textContent = message;
    box.hidden = false;
    box.focus();
  }

  // The status line of both auth pages. Only JS writes into it, and only to report a problem.
  const AUTH_INIT_ID = 'auth-init-state';
  let authInitReported = false;

  // The first failure focuses #form-error (a blocking error the operator just triggered); repeat
  // attempts only refresh the status line, so focus stays in the password field for another Enter.
  function setAuthInitError(message) {
    const line = document.getElementById(AUTH_INIT_ID);
    if (line) line.textContent = message;
    if (!authInitReported) { authInitReported = true; formError(message); }
  }

  // Restores focus (and the caret) after a section was rebuilt, addressed by the element id.
  function preserveFocus(render) {
    const active = document.activeElement;
    const id = active?.id;
    const start = active?.selectionStart;
    const end = active?.selectionEnd;
    render();
    const next = id ? document.getElementById(id) : null;
    if (!next) return;
    next.focus();
    if (start != null && typeof next.setSelectionRange === 'function' && /text|search|url|tel|password/.test(next.type)) {
      next.setSelectionRange(start, end);
    }
  }

  function toggleTheme() {
    const next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-bs-theme', next);
    localStorage.setItem('rct-admin-theme', next);
  }

  function initShell() {
    $('theme-toggle')?.addEventListener('click', toggleTheme);
    const toggle = $('nav-toggle');
    toggle?.addEventListener('click', () => {
      const nav = $('mobile-nav');
      nav.hidden = !nav.hidden;
      toggle.setAttribute('aria-expanded', String(!nav.hidden));
      toggle.setAttribute('aria-label', nav.hidden ? 'Open menu' : 'Close menu');
    });
    $('logout-button')?.addEventListener('click', async () => {
      try {
        await api('logout', { method: 'POST' });
        location.assign('/login');
      } catch (error) { toast(messageFrom(error), 'danger'); }
    });
    window.addEventListener('beforeunload', (event) => {
      if (outstandingWork()) { event.preventDefault(); event.returnValue = ''; }
    });
  }

  // Registered during page parse, before any await: event.preventDefault() is the only thing that
  // suppresses the native submit, and it can only be missing when JS is dead — which is exactly the
  // state the form's method="post" exists for, so no password can ever land in a URL.
  function initAuthForm(formId, submit) {
    const form = document.getElementById(formId);
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        if (!csrfToken) await ensureSession(); // lazy recovery, single in-flight request
      } catch {
        setAuthInitError('The login form could not be prepared. Please reload the page.');
        return;
      }
      await submit(form);
    });
  }

  async function submitLogin(form) {
    $('form-error').hidden = true;
    submitState(form, true);
    try {
      const result = await api('login', { method: 'POST', body: JSON.stringify({ username: $('username').value.trim(), password: $('password').value }) });
      location.assign(result.must_change_password ? '/change-password' : '/ui/dashboard');
    } catch (error) { formError(messageFrom(error)); submitState(form, false); }
  }

  async function submitPasswordChange(form) {
    $('form-error').hidden = true;
    const next = $('new-password').value;
    if (next !== $('confirm-password').value) { formError('The new passwords do not match.'); return; }
    submitState(form, true);
    try {
      await api('change-password', { method: 'POST', body: JSON.stringify({ current_password: $('current-password').value, new_password: next }) });
      location.assign('/ui/dashboard');
    } catch (error) { formError(messageFrom(error)); submitState(form, false); }
  }

  function displayStatus(status) {
    const key = String(status).toLowerCase();
    if (status === true || ['ok', 'connected', 'online', 'healthy'].includes(key)) return ['Connected', 'online'];
    if (key === 'degraded') return ['Degraded', 'offline'];
    if (key === 'starting') return ['Connecting', ''];
    if (key === 'maintenance') return ['Maintenance', ''];
    if (status === false || ['disconnected', 'offline', 'unreachable', 'error'].includes(key)) return ['Disconnected', 'offline'];
    return [String(status || 'Unknown'), ''];
  }

  // Splits a reading into number and unit so the card can render the unit smaller than the number.
  function metricParts(value, unit) {
    if (!Number.isFinite(value)) return ['–', ''];
    if (unit === 'ratio') { value *= 100; unit = '%'; }
    // `|| 0` turns a rounded negative zero into 0 so the card never shows "-0 W".
    return [new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(Math.round(value) || 0), unit ? ` ${unit}` : ''];
  }

  // grid_power is signed: negative = feed-in (Einspeisung), positive = draw (Bezug), as the RCT
  // g_sync.p_ac_sc_sum object reports it. The UI shows the magnitude plus a direction indicator.
  function gridFlow(value) {
    if (!Number.isFinite(value) || Math.round(value) === 0) return null;
    return value < 0
      ? { dir: 'feed', icon: 'arrow_upward', label: 'Feeding into the grid' }
      : { dir: 'draw', icon: 'arrow_downward', label: 'Drawing from the grid' };
  }

  function flowNode(flow) {
    const node = element('span', `device-flow device-flow-${flow.dir}`);
    const icon = element('span', 'material-icons', flow.icon);  // locally bundled icon font
    icon.setAttribute('aria-hidden', 'true');
    node.append(icon);
    node.setAttribute('role', 'img');
    node.setAttribute('aria-label', flow.label);
    node.title = flow.label;
    return node;
  }

  function formatMetric(value, unit) {
    return metricParts(value, unit).join('');
  }

  function showHeroMetrics(devices) {
    const all = devices.flatMap((device) => Array.isArray(device.metrics) ? device.metrics : []);
    const valid = (item) => item.value !== null && item.value !== undefined && Number.isFinite(Number(item.value));
    const sum = (name) => all.filter((item) => item.name === name && valid(item)).reduce((total, item) => total + Number(item.value), 0);
    const present = (name) => all.some((item) => item.name === name && valid(item));
    const pv = present('solar_a_power') || present('solar_b_power') ? sum('solar_a_power') + sum('solar_b_power') : null;
    const grid = present('grid_power') ? sum('grid_power') : null;
    const house = present('household_load_power') ? sum('household_load_power') : null;
    const socItems = all.filter((item) => item.name === 'battery_soc' && valid(item));
    const soc = socItems.length ? socItems.reduce((total, item) => total + Number(item.value), 0) / socItems.length : null;
    $('pv-power').textContent = formatMetric(pv, all.find((item) => item.name === 'solar_a_power' || item.name === 'solar_b_power')?.unit || 'W');
    const gridUnit = all.find((item) => item.name === 'grid_power')?.unit || 'W';
    const gridNode = $('grid-power');
    const gridKey = `${grid}|${gridUnit}`;
    if (gridNode.dataset.reading !== gridKey) {
      gridNode.dataset.reading = gridKey;
      gridNode.textContent = formatMetric(grid === null ? NaN : Math.abs(grid), gridUnit);
      const flow = grid === null ? null : gridFlow(grid);
      if (flow) gridNode.prepend(flowNode(flow), ' ');
    }
    $('house-power').textContent = formatMetric(house, all.find((item) => item.name === 'household_load_power')?.unit || 'W');
    $('battery-soc').textContent = formatMetric(soc, all.find((item) => item.name === 'battery_soc')?.unit || 'ratio');
  }

  const INVERTER_METRICS = [['solar_a_power', 'PV A'], ['solar_b_power', 'PV B'], ['grid_power', 'Grid'], ['ac_power', 'AC power']];
  const BATTERY_METRICS = [['battery_soc', 'Charge level']];
  const INVERTER_FACTS = [['heat_sink_temperature', 'Heat sink']];
  const BATTERY_FACTS = [
    ['battery_temperature', 'Battery temperature'],
    ['battery_cycles', 'Charge cycles'],
    ['battery_soc_target', 'SOC target'],
    ['power_mng_bat_next_calib_date', 'Next calibration'],
  ];

  // Reconciles a parent's children with the wanted nodes in order; untouched nodes are not re-inserted.
  function syncChildren(parent, nodes) {
    nodes.forEach((node, index) => {
      if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
    });
    while (parent.children.length > nodes.length) parent.lastElementChild.remove();
  }

  function setText(node, value) {
    const text = String(value);
    if (node.textContent !== text) node.textContent = text;
  }

  // Renders number plus a smaller unit span; the key avoids touching the DOM when nothing changed.
  function setReading(node, number, unit = '', flow = null) {
    const key = `${number}\u0000${unit}\u0000${flow ? flow.dir : ''}`;
    if (node.dataset.reading === key) return;
    node.dataset.reading = key;
    node.textContent = number;
    if (flow) node.prepend(flowNode(flow), ' ');
    if (unit) node.append(element('span', 'device-unit', unit));
  }

  function setClass(node, className, on) {
    if (node.classList.contains(className) !== on) node.classList.toggle(className, on);
  }

  // Explains statuses that deserve more than a label; matched against the server-provided text.
  const BATTERY_NOTICES = [
    [/balancing required/i, 'The battery will balance its cells automatically; no action is needed.'],
    [/balancing active/i, 'The battery is balancing its cells right now; this finishes on its own.'],
  ];

  function createDeviceHalf(className, title, imageName, badgeMetric, badgeLabel, metricDefs, factDefs) {
    const half = element('section', `device-half ${className}`);
    half.append(element('h4', 'device-half-title', title));
    const cell = (label) => {
      const node = element('div', 'device-metric');
      const dd = element('dd', 'mb-0 fw-semibold');
      node.append(element('dt', 'fw-normal text-secondary', label), dd);
      return { node, dd };
    };
    const list = element('dl', 'device-metrics mb-0');
    const facts = element('dl', 'device-facts mb-0');
    const rows = [];
    for (const [name, label] of metricDefs) {
      const { node, dd } = cell(label);
      const row = { name, dd, cell: node, optional: false, bar: null };
      if (name === 'battery_soc') {
        // Decorative: the percentage is already printed above the bar.
        row.bar = element('div', 'device-soc-bar');
        row.bar.setAttribute('aria-hidden', 'true');
        row.bar.append(element('div', 'device-soc-fill'));
        node.append(row.bar);
      }
      list.append(node);
      rows.push(row);
    }
    const { node: badgeNode, dd: badgeDd } = cell(badgeLabel);
    badgeDd.classList.add('device-fact-badge');
    badgeNode.classList.add('device-status-metric');
    const note = element('p', 'device-notice-text mb-0');
    note.hidden = true;
    badgeNode.append(note);
    const badgeCell = { badgeMetric, dd: badgeDd, cell: badgeNode, note };
    for (const [name, label] of factDefs) {
      const { node, dd } = cell(label);
      rows.push({ name, dd, cell: node, optional: true, bar: null });
    }
    const readings = element('div', 'device-readings');
    readings.append(list, facts);
    const image = element('img', 'device-half-image');
    image.src = `/admin/static/img/${imageName}`;
    image.alt = '';
    image.width = 152;
    image.height = 200;
    const content = element('div', 'device-half-content');
    content.append(image, readings);
    half.append(content);
    return { node: half, rows, badgeCell, facts };
  }

  function createDeviceVisual() {
    const wrap = element('div', 'device-visual');
    const inverterHalf = createDeviceHalf(
      'device-half-inverter', 'Power', 'rct-inverter.svg', 'inverter_state', 'Inverter status', INVERTER_METRICS, INVERTER_FACTS,
    );
    const batteryHalf = createDeviceHalf(
      'device-half-battery', 'Battery', 'rct-batterystack.svg', 'battery_status2', 'Battery status', BATTERY_METRICS, BATTERY_FACTS,
    );
    const divider = element('div', 'device-divider');
    divider.setAttribute('aria-hidden', 'true');
    wrap.append(inverterHalf.node, divider, batteryHalf.node);
    return { node: wrap, inverterHalf, batteryHalf };
  }

  // Patches one half's rows and badge cell in place, same reconciliation pattern for both halves.
  function patchDeviceHalf(half, metrics) {
    const factNodes = [];
    const { badgeMetric, dd: badgeDd, cell: badgeCell, note } = half.badgeCell || {};
    // Falls back to the second battery stack's status when the first is not present.
    const badgeReading = badgeMetric ? metrics.get(badgeMetric) || (badgeMetric === 'battery_status2' ? metrics.get('battery_placeholder_0_status2') : undefined) : undefined;
    if (badgeReading?.label) {
      const badgeText = badgeReading.label;
      setText(badgeDd, badgeText);
      if (badgeDd.title !== badgeText) badgeDd.title = badgeText;
      const notice = BATTERY_NOTICES.find(([pattern]) => pattern.test(badgeText));
      setClass(badgeCell, 'is-notice', Boolean(notice));
      setText(note, notice ? notice[1] : '');
      note.hidden = !notice;
      factNodes.push(badgeCell);
    }
    for (const { name, dd, cell, optional, bar } of half.rows) {
      const metric = metrics.get(name);
      if (optional) {
        if (!metric) continue;  // no reading for this device: leave the fact out
        factNodes.push(cell);
      }
      const value = metric && metric.value !== null && metric.value !== undefined ? Number(metric.value) : NaN;
      const calibrationDate = name === 'power_mng_bat_next_calib_date' && Number.isFinite(value) && value > 0 ? new Date(value * 1000) : null;
      const available = name === 'power_mng_bat_next_calib_date' ? Boolean(calibrationDate && !Number.isNaN(calibrationDate.getTime())) : Number.isFinite(value);
      const isGrid = name === 'grid_power';
      const [number, unit] = calibrationDate ? [formatDate(calibrationDate, { time: true }), ''] : metricParts(isGrid ? Math.abs(value) : value, metric?.unit);
      if (available) setReading(dd, number, unit, isGrid ? gridFlow(value) : null); else setReading(dd, 'n/a');
      setClass(dd, 'text-secondary', !available);
      if (bar) {
        const percent = available ? Math.min(100, Math.max(0, metric.unit === 'ratio' ? value * 100 : value)) : 0;
        const width = `${percent}%`;
        if (bar.firstElementChild.style.width !== width) bar.firstElementChild.style.width = width;
      }
    }
    syncChildren(half.facts, factNodes);
  }

  function patchDeviceVisual(visual, device) {
    const metrics = new Map((device.metrics || []).map((item) => [item.name, item]));
    patchDeviceHalf(visual.inverterHalf, metrics);
    patchDeviceHalf(visual.batteryHalf, metrics);
  }

  function createDeviceCard() {
    const ref = {
      card: element('article', 'device-item'),
      head: element('div', 'device-head'),
      statusBadge: element('span', 'device-status-badge device-status-badge-online'),
      top: element('div', 'device-head-main'),
      meta: element('div', 'device-head-meta'),
      dot: element('span', 'status-dot'),
      title: element('h3', 'mb-0 me-auto'),
      statusText: element('div', 'fw-medium small'),
      address: element('small', 'text-secondary'),
      last: element('small', 'text-secondary'),
      visual: createDeviceVisual(),
    };
    return ref;
  }

  function patchDeviceCard(ref, device) {
    const [status, statusClass] = displayStatus(device.status);
    const connected = statusClass === 'online';
    setClass(ref.card, 'has-status-badge', connected);
    setText(ref.statusBadge, status);
    ref.dot.className = `status-dot ${statusClass}`;
    setText(ref.title, device.name || device.id || 'Inverter');
    setText(ref.statusText, status);
    // Name, address, last connection and the connection state share two compact header lines.
    syncChildren(ref.top, connected ? [ref.title, ref.statusBadge] : [ref.dot, ref.title, ref.statusText]);
    setText(ref.address, `${device.host || '–'}${device.port ? `:${device.port}` : ''}`);
    if (device.last_success_at) setText(ref.last, `Last connection: ${formatDate(device.last_success_at, { time: true })}`);
    syncChildren(ref.meta, device.last_success_at ? [ref.address, ref.last] : [ref.address]);
    syncChildren(ref.head, [ref.top, ref.meta]);
    patchDeviceVisual(ref.visual, device);
    syncChildren(ref.card, [ref.head, ref.visual.node]);
    return { connected };
  }

  const DASHBOARD_INTERVAL_MS = 10000;
  const DASHBOARD_MAX_INTERVAL_MS = 60000;
  const DASHBOARD_TIMEOUT_MS = 8000;
  const dashboardCards = new Map();
  const dashboardNotices = { empty: element('p', 'text-secondary mb-0', 'No inverters configured yet.'), error: element('p', 'text-danger mb-0', 'Inverters could not be loaded.') };
  let dashboardPollFailing = false;
  let dashboardFailures = 0;
  let dashboardTimer = null;
  let dashboardController = null;
  let dashboardGeneration = 0;
  let dashboardLastSuccess = null;

  function dashboardDelay() {
    const base = Math.min(DASHBOARD_INTERVAL_MS * 2 ** dashboardFailures, DASHBOARD_MAX_INTERVAL_MS);
    return Math.round(base * (0.9 + Math.random() * 0.2)); // jitter keeps several tabs from polling in lockstep
  }

  function scheduleDashboard() {
    clearTimeout(dashboardTimer);
    dashboardTimer = null;
    // A hidden tab stops polling; the visibilitychange handler resumes it immediately.
    if (document.hidden) return;
    dashboardTimer = setTimeout(() => { dashboardTimer = null; loadDashboard({ automatic: true }).catch(() => { }); }, dashboardDelay());
  }

  async function loadMetricCount() {
    try {
      const data = await api('parameters');
      $('metric-count').textContent = String(data.exposed_names?.length || 0);
    } catch { /* the count is informational; keep the last value */ }
  }

  function renderTsdbTile(tsdb) {
    const icon = $('tsdb-status-icon');
    const label = $('tsdb-status-label');
    const time = $('tsdb-status-time');
    icon.classList.remove('text-success', 'text-danger', 'text-secondary');
    if (!tsdb || !tsdb.configured) {
      icon.textContent = 'cloud_off';
      icon.classList.add('text-secondary');
      label.textContent = 'Not configured';
      time.textContent = '';
      return;
    }
    if (tsdb.healthy) {
      icon.textContent = 'cloud_done';
      icon.classList.add('text-success');
      label.textContent = 'Sending';
    } else {
      icon.textContent = 'sync_problem';
      icon.classList.add('text-danger');
      label.textContent = 'Failing';
    }
    time.textContent = tsdb.last_success_at ? `Last sent ${hhmm(new Date(tsdb.last_success_at))}` : '';
  }

  function renderDashboard(devices, tsdb) {
    const list = $('devices-list');
    showHeroMetrics(devices);
    renderTsdbTile(tsdb);
    $('device-count').textContent = String(devices.length);
    $('connected-count').textContent = String(devices.filter((item) => displayStatus(item.status)[1] === 'online').length);
    const seen = new Set();
    const nodes = devices.map((device, index) => {
      let key = String(device.id ?? `index-${index}`);
      while (seen.has(key)) key += '+';
      seen.add(key);
      let ref = dashboardCards.get(key);
      if (!ref) { ref = createDeviceCard(); dashboardCards.set(key, ref); }
      patchDeviceCard(ref, device);
      return ref.card;
    });
    for (const key of [...dashboardCards.keys()]) if (!seen.has(key)) dashboardCards.delete(key);
    syncChildren(list, nodes.length ? nodes : [dashboardNotices.empty]);
  }

  // Returns 'ok', 'failed' or 'skipped' (an automatic poll found one already running).
  async function loadDashboard({ automatic = false } = {}) {
    // No overlapping automatic polls; a non-automatic call (initial load, after a settings save,
    // or a visibilitychange refresh) supersedes the running request instead.
    if (automatic && dashboardController) return 'skipped';
    if (dashboardController) dashboardController.abort(Object.assign(new Error('Superseded by a newer request.'), { name: 'AbortError' }));
    const controller = new AbortController();
    const generation = ++dashboardGeneration;
    dashboardController = controller;
    const timer = setTimeout(() => controller.abort(Object.assign(new Error('The dashboard request timed out.'), { name: 'TimeoutError' })), DASHBOARD_TIMEOUT_MS);
    try {
      const data = await api('devices', { signal: controller.signal });
      if (generation !== dashboardGeneration) return 'skipped';
      dashboardPollFailing = false;
      dashboardFailures = 0;
      renderDashboard(Array.isArray(data.devices) ? data.devices : [], data.tsdb || null);
      dashboardLastSuccess = new Date();
      const updated = $('dashboard-updated');
      if (updated) updated.textContent = `Updated ${hhmm(dashboardLastSuccess)}`;
      return 'ok';
    } catch (error) {
      if (generation !== dashboardGeneration) return 'skipped';
      dashboardFailures += 1;
      syncChildren($('devices-list'), [dashboardNotices.error]);
      // Automatic background polls toast only on the first failure after a success streak, so a
      // sustained outage does not spam one alert per poll; a non-automatic load always toasts.
      if (!automatic || !dashboardPollFailing) toast(messageFrom(error), 'danger');
      dashboardPollFailing = true;
      return 'failed';
    } finally {
      clearTimeout(timer);
      if (generation === dashboardGeneration) { dashboardController = null; scheduleDashboard(); }
    }
  }

  function initDashboardPolling() {
    // Refresh at once when the tab becomes visible instead of waiting for the next tick.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(dashboardTimer); dashboardTimer = null; return; }
      clearTimeout(dashboardTimer);
      loadDashboard({ automatic: true }).catch(() => { });
    });
  }

  function formatDate(value, { time = false, empty = 'No expiry' } = {}) {
    if (!value) return empty;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    // Locale comes from the browser (undefined), never a hardcoded region.
    return new Intl.DateTimeFormat(undefined, time ? { dateStyle: 'medium', timeStyle: 'short' } : { dateStyle: 'medium' }).format(date);
  }

  async function loadTokens() {
    const host = $('tokens-list');
    const data = await api('tokens');
    host.replaceChildren();
    if (!data.tokens?.length) { host.append(element('p', 'text-secondary mb-0', 'No tokens created yet.')); return; }
    for (const token of data.tokens) {
      const row = element('div', 'token-row');
      const details = element('div');
      details.append(element('strong', 'd-block', token.name), element('small', 'd-block text-secondary', `${token.role === 'read' ? 'Read' : 'Read and write'} · Created: ${formatDate(token.created_at)} · Last used: ${formatDate(token.last_used_at, { time: true, empty: 'Never' })} · Expires: ${formatDate(token.expires_at)}`));
      const revoke = element('button', 'btn btn-outline-danger btn-sm', 'Revoke');
      revoke.type = 'button';
      revoke.setAttribute('aria-label', `Revoke token ${token.name}`);
      revoke.addEventListener('click', async () => {
        if (!confirm(`Really revoke token "${token.name}"?`)) return;
        revoke.disabled = true;
        try { await api(`tokens/${encodeURIComponent(token.id)}`, { method: 'DELETE' }); row.remove(); toast('Token revoked.'); if (!host.childElementCount) host.append(element('p', 'text-secondary mb-0', 'No tokens created yet.')); }
        catch (error) { revoke.disabled = false; toast(messageFrom(error), 'danger'); }
      });
      row.append(details, revoke);
      host.append(row);
    }
  }

  // Expiry presets instead of a free date; 'never' sends no expires_at, as an empty date did.
  const EXPIRY_DEFAULT = '90d';

  function expiryFromPreset(preset) {
    const date = new Date();
    if (preset === '30d') date.setDate(date.getDate() + 30);
    else if (preset === '90d') date.setDate(date.getDate() + 90);
    // Calendar arithmetic, so a leap day shifts to the real anniversary instead of a fixed 365 days.
    else if (preset === '1y') date.setFullYear(date.getFullYear() + 1);
    else return null;
    return date.toISOString();
  }

  function initTokens() {
    loadTokens().catch((error) => toast(messageFrom(error), 'danger'));
    $('token-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      submitState(form, true);
      $('new-token-box').hidden = true;
      $('new-token-value').textContent = '';
      const expiry = expiryFromPreset($('token-expires').value);
      const body = { name: $('token-name').value.trim(), role: $('token-role').value };
      if (expiry) body.expires_at = expiry;
      try {
        const result = await api('tokens', { method: 'POST', body: JSON.stringify(body) });
        $('new-token-value').textContent = result.token || '';
        $('new-token-box').hidden = false;
        form.reset();
        // reset() restores the markup default, which is the 90-day option; set it explicitly so a
        // later markup reorder cannot silently default new tokens to "Never expires".
        $('token-expires').value = EXPIRY_DEFAULT;
        await loadTokens();
        toast('Token created. Please copy it now.');
      } catch (error) { toast(messageFrom(error), 'danger'); }
      finally { submitState(form, false); }
    });
    $('copy-token').addEventListener('click', async () => {
      if (await copyToClipboard($('new-token-value').textContent)) toast('Token copied.');
      else toast('Copying is not available here. Please select and copy the token.', 'warning');
    });
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

  let settingsCommitted = {};
  let settingsDraft = {};
  const pendingKeys = new Set();          // scalar + devices keys
  let pendingExportGroup = false;         // the export fields are queued as a group, never per key
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

  const SAVE_STATES = { idle: 0, saved: 1, incomplete: 2, unsaved: 3, saving: 4, failed: 5 };
  const SAVE_STATE_TEXT = {
    failed: ['Save failed', 'text-danger'],
    saving: ['Saving …', 'text-secondary'],
    unsaved: ['Unsaved changes', 'text-warning'],
    incomplete: ['Incomplete — not saved yet', 'text-warning'],
    saved: ['Saved', 'text-success'],
    idle: ['', 'text-secondary'],
  };
  const SAVE_STATE_CLASSES = ['text-danger', 'text-secondary', 'text-warning', 'text-success'];
  const saveSections = new Map();         // id -> { state, message, lastSavedAt }
  const anySection = (states) => [...saveSections.values()].some((entry) => states.includes(entry.state));

  const DEVICES_INCOMPLETE_MSG = 'Fix the marked inverter rows — nothing was saved yet.';
  const FAILURE_MESSAGES = {
    general: (reason) => `Not saved (${reason}). The value was restored — enter it again.`,
    devices: (reason) => `The inverters were not saved (${reason}). Retry, or discard the changes.`,
    export: (reason) => `The export settings were not saved (${reason}).`,
    parameters: (reason) => `Not saved (${reason}). The list was reloaded from the server — apply your change again.`,
  };
  function failureMessage(id, reason) { return (FAILURE_MESSAGES[id] || FAILURE_MESSAGES.general)(reason); }

  function sectionOf(key) {
    if (key === 'devices') return 'devices';
    return exportFields.some((field) => field.key === key) ? 'export' : 'general';
  }

  // Every anchor is block level, and null when the section has no surface on this page.
  // #exposed-list is a <tbody>, so the parameters anchor on prometheus is its table wrapper.
  function anchorFor(id) {
    if (id === 'devices') return document.getElementById('device-editor');
    if (id === 'export') return document.querySelector('.export-layout');
    if (id === 'parameters') return document.querySelector(page === 'prometheus' ? '.prometheus-table-wrap' : '#writable-list');
    return document.getElementById('settings-sections');
  }

  // A section notice is a sibling *before* its anchor: aria-live="off" plus that placement avoids
  // the double announcement a node inside the aria-live="polite" #settings-sections would cause, and
  // keeps the export notice outside .export-layout, which the e2e "no Save button" checks count in.
  // `kind` is 'error' (a rejected save) or 'hint' (an incomplete group that is being withheld).
  const NOTICE_IDS = { error: 'save-error', hint: 'save-hint' };

  function sectionNotice(id, kind) {
    const anchor = anchorFor(id);
    if (!anchor) return null;
    const modal = anchor.closest('#inverters-modal');
    if (modal && !modal.classList.contains('show')) return null; // toast-only while the dialog is hidden
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
    export: () => { queueExportSettings(); flushSettings({ sections: ['export'] }).catch(() => { }); },
    devices: () => { queueSettings('devices'); flushSettings({ sections: ['devices'] }).catch(() => { }); },
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
    if (id === 'devices') {
      const discard = element('button', 'btn btn-outline-secondary btn-sm', 'Discard changes');
      discard.type = 'button';
      discard.addEventListener('click', discardDeviceChanges);
      actions.append(discard);
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
      lastSavedAt: state === 'saved' ? new Date() : previous?.lastSavedAt || null,
    });
    renderSaveState();
  }

  // The single writer of #save-state and of the per-section alerts; four independent writers are
  // what produced the "Saving …" lie an incomplete row used to show.
  function renderSaveState() {
    for (const [id, entry] of saveSections) {
      if (entry.failure) renderSectionAlert(id, entry.failure);
      else document.getElementById(`save-error-${id}`)?.remove();
    }
    const label = $('save-state');
    if (!label) return; // absent on tokens/about/login/change-password; the alerts above are independent
    let top = 'idle';
    let savedAt = null;
    for (const entry of saveSections.values()) {
      if (SAVE_STATES[entry.state] > SAVE_STATES[top]) top = entry.state;
      if (entry.lastSavedAt && (!savedAt || entry.lastSavedAt > savedAt)) savedAt = entry.lastSavedAt;
    }
    const [text, className] = SAVE_STATE_TEXT[top];
    label.textContent = top === 'saved' && savedAt ? `Saved ${hhmm(savedAt)}` : text;
    label.classList.remove(...SAVE_STATE_CLASSES);
    label.classList.add(className);
  }

  // Derived on demand instead of a cached counter four writers had to keep correct. `failed` is
  // deliberately absent: `general` rolled its value back, and `devices`/`export` failures keep
  // their keys queued, so genuine leftover work is already covered by hasPending(null).
  function outstandingWork() {
    return hasPending(null) || parameterDirty || parameterSending || anySection(['saving', 'incomplete']);
  }

  function reportSaveFailure(section, message) {
    setSectionState(section, 'failed', message); // renders the persistent alert
    toast(message, 'danger');                    // unchanged visibility floor, also when the alert is absent
    return sectionAlert(section);
  }

  function restartDebounce() { clearTimeout(settingsTimer); settingsTimer = setTimeout(() => { flushSettings().catch(() => { }); }, 450); }

  function queueSettings(key) {
    pendingKeys.add(key);
    setSectionState(sectionOf(key), 'unsaved');
    restartDebounce();
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
    // The symmetric devices re-check: the draft can be mutated without a render (syncDeviceInputs).
    if (pendingKeys.has('devices') && !devicesValid()) {
      pendingKeys.delete('devices');
      setSectionState('devices', 'incomplete', DEVICES_INCOMPLETE_MSG);
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
    const sentDevices = keys.includes('devices') ? settingsDraft.devices.filter((device) => !isBlankNewRow(device)) : [];
    const payload = Object.fromEntries(keys.map((key) => [key, key === 'devices' ? devicesPayload() : structuredClone(settingsDraft[key])]));
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
        if (control) { control.value = ''; control.placeholder = 'set'; }
      }
      // Only the server-assigned id is adopted; copying back the whole entry would overwrite
      // edits the operator made while this request was still in flight. Matched by the same
      // (host, port, network_id) tuple deviceProblem() uses, not by array index, since the server
      // is not guaranteed to echo devices back in exactly the order they were sent.
      sentDevices.forEach((device) => {
        const assigned = (result.settings?.devices || []).find((candidate) =>
          String(candidate.host || '').trim().toLowerCase() === String(device.host || '').trim().toLowerCase()
          && Number(candidate.port) === Number(device.port)
          && networkId(candidate.network_id) === networkId(device.network_id));
        if (assigned && !device.device_id && assigned.device_id) device.device_id = assigned.device_id;
      });
      showRestartNotice(result.restart_required);
      if (page === 'prometheus') markPrometheusSaved();
      if (page === 'dashboard' && keys.includes('devices')) { loadDashboard().catch((error) => toast(messageFrom(error), 'danger')); loadMetricCount(); }
      for (const id of sentSections) setSectionState(id, 'saved');
      if (keys.some((key) => result.restart_required?.includes(key))) toast('Saved. A restart is required for this setting.', 'warning');
      else toast('Settings saved and active.');
      return true;
    } catch (error) {
      // A full renderSettings() would tear down and rebuild every section's DOM, including one the
      // operator was mid-edit on but that was never part of this save — losing focus and any input
      // in that unrelated section. Only the controls for the keys that actually failed are refreshed.
      for (const key of keys) {
        if (pendingKeys.has(key)) continue;  // re-queued during the request: that value wins
        if (key === 'devices') continue;     // the draft is kept; a rollback would orphan the row objects
        if (isSecretKey(key)) continue;      // the committed view holds no raw secret, only _configured
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
      if (hasPending(null)) restartDebounce(); // re-arm the 450 ms timer, no recursion
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
      if (field.type === 'secret') { control.autocomplete = 'new-password'; control.placeholder = settingsDraft[`${field.key}_configured`] ? 'set' : ''; }
      // The bounds come from the field definition so the browser signals what the server accepts.
      if (field.type === 'number') { control.min = String(field.min); control.max = String(field.max); control.inputMode = 'numeric'; }
    }
    control.id = id;
    const help = element('div', 'form-text', field.help);
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
    const onChange = () => {
      if (field.type === 'number') {
        const invalid = numberFieldInvalid(control, field);
        if (invalid) { reportInvalid(control, invalid); return; }
        clearInvalid(control);
        settingsDraft[field.key] = control.value === '' ? null : Number(control.value);
      } else if (field.type === 'toggle') settingsDraft[field.key] = control.checked;
      else if (field.type === 'list') settingsDraft[field.key] = control.value.split(',').map((item) => item.trim()).filter(Boolean);
      else settingsDraft[field.key] = control.value;
      queueSettings(field.key);
      if (page === 'prometheus') renderPrometheusSummary();
      // A master toggle decides which dependent fields exist, so only its own group is rebuilt
      // live — and the toggle keeps the focus it had when it was operated.
      if (gatesOtherFields(field.key)) preserveFocus(() => rerenderGroup(field.group));
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

  const exportFields = [
    { key: 'db_type', label: 'Database type', help: 'Target of the metrics export. The Prometheus endpoint (/metrics) is independent of it.', type: 'select', options: [['', 'Disabled'], ['influxdb_v2', 'InfluxDB 2'], ['questdb', 'QuestDB (Open Source)']] },
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

  function exportControl(field, onChange) {
    const { wrap, control } = buildFieldControl(field, settingsDraft[field.key]);
    // A secret left empty keeps its current value server-side (the PUT handler drops ""), so an
    // empty box must not overwrite settingsDraft and must not autosave on every blur.
    control.addEventListener('change', () => {
      if (field.type === 'secret') {
        if (control.value === '') return;          // an empty box keeps the stored secret; no queueing
        settingsDraft[field.key] = control.value;
        secretRevision.set(field.key, (secretRevision.get(field.key) || 0) + 1);
      } else if (field.type === 'number') {
        const invalid = numberFieldInvalid(control, field);
        if (invalid) { reportInvalid(control, invalid); return; }
        clearInvalid(control);
        settingsDraft[field.key] = control.value === '' ? null : Number(control.value);
      } else if (field.type === 'toggle') settingsDraft[field.key] = control.checked;
      else settingsDraft[field.key] = control.value;
      if (field.key === 'db_type' || field.key === 'questdb_downsampling') onChange();
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

  // The export fields depend on each other (required per backend), so they are saved together.
  // Order matches the 2-column grid's row-major placement: Export/Target share the top row,
  // Connection/Retention the bottom row (see .export-grid in admin.css).
  const EXPORT_GROUPS = ['Export', 'Target', 'Connection', 'Retention'];

  function exportGroup(key) {
    if (key === 'db_type' || key === 'metrics_export_enabled' || key === 'metrics_export_interval_seconds') return 'Export';
    if (/_(hostname|port|tls_enabled|verify_tls|allow_plaintext_credentials)$/.test(key)) return 'Connection';
    if (/_(downsampling|raw_retention_days|retention_days)$/.test(key)) return 'Retention';
    return 'Target';
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

  // The fields currently shown for the active backend; mirrors the filter in renderExportSettings.
  function exportPayloadKeys(type) {
    return exportFields
      .filter((field) => exportFieldKnown(field) && !((field.backend && field.backend !== type) || (field.any && !type)))
      .map((field) => field.key);
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

  // Export fields autosave as one coherent group: until the active backend's required fields are
  // all filled, nothing is sent, so the server never has to reject a half-entered combination.
  // Becoming incomplete again retracts the whole group *and* stops the debounce timer a complete
  // state had started, so a configuration that regressed inside the window is never sent.
  function queueExportSettings() {
    const type = settingsDraft.db_type || '';
    if (!exportReady(type)) {
      pendingExportGroup = false;
      setSectionState('export', 'incomplete', missingExportHint(type));
      if (!hasPending(null)) clearTimeout(settingsTimer);
      return;
    }
    pendingExportGroup = true;
    setSectionState('export', 'unsaved');
    restartDebounce();
  }

  // Export fields autosave through the shared queueSettings/flushSettings path, same as every
  // other settings page; db_type changes still re-render because the field set depends on it.
  function renderExportSettings(host) {
    const layout = element('div', 'export-layout');
    host.append(layout);
    // The Export group never becomes a <section>, so it is not routed through rerenderGroup();
    // this internal render already replaces only .export-layout's children.
    const rerender = () => preserveFocus(render);
    const render = () => {
      const grid = element('div', 'export-grid');
      const type = settingsDraft.db_type || '';
      for (const group of EXPORT_GROUPS) {
        const body = element('div', 'card-body');
        body.append(element('h2', 'h5 mb-3', group === 'Export' ? 'Metrics export' : group));
        for (const field of exportFields) {
          if (exportGroup(field.key) !== group || !exportFieldKnown(field)) continue;
          if ((field.backend && field.backend !== type) || (field.any && !type)) continue;
          if (field.key === 'questdb_raw_retention_days' && settingsDraft.questdb_downsampling !== 'manual') continue;
          if (field.key === 'questdb_retention_days' && settingsDraft.questdb_downsampling === 'manual') continue;
          body.append(exportControl(field, rerender));
        }
        if (body.children.length < 2) continue;
        const section = element('section', 'card');
        section.append(body);
        grid.append(section);
      }
      layout.replaceChildren(grid);
    };
    render();
  }

  const HOST_PATTERN = /^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]+)$/;

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

  // A fresh, still empty row is ignored; every other row must be valid before anything is saved.
  function isBlankNewRow(device) { return !device.device_id && !String(device.host || '').trim(); }

  // Client-only stable identity. Handlers and the row DOM reference a device object, not an array
  // index: an index does not survive a splice or a re-render, and replacing the array on a failed
  // save would orphan every object the live handlers still hold.
  let deviceUid = 0;
  function withUid(device) { if (!device._uid) device._uid = `d${++deviceUid}`; return device; }
  function adoptDevices(list) { settingsDraft.devices = (list || []).map(withUid); return settingsDraft.devices; }
  function deviceByUid(uid) { return (settingsDraft.devices || []).find((device) => device._uid === uid) || null; }

  // Explicit fields instead of a spread, so the payload stays self-documenting and independent of
  // the server dropping unknown per-device keys (_normalize_devices rebuilds from a whitelist).
  function devicesPayload() {
    return settingsDraft.devices.filter((device) => !isBlankNewRow(device)).map((device) => ({
      host: String(device.host).trim(),
      port: Number(device.port),
      network_id: networkId(device.network_id),
      device_id: device.device_id || null,
      display_name: device.display_name || null,
    }));
  }

  // Pure predicate, no DOM: the send-time re-check in sendSettings() needs it for a draft that was
  // mutated straight from the inputs (syncDeviceInputs) without a render in between.
  function devicesValid() {
    return (settingsDraft.devices || []).every((device) => isBlankNewRow(device) || !deviceProblem(device).field);
  }

  // The only owner of the `devices` section state and of whether the `devices` key may stay queued.
  function refreshDeviceState(card) {
    let invalid = false;
    for (const row of card.querySelectorAll('.device-settings-item')) {
      const device = deviceByUid(row.dataset.uid);
      if (!device) continue; // the row belongs to a draft generation that is already gone
      const { field, message } = isBlankNewRow(device) ? { field: '', message: '' } : deviceProblem(device);
      const feedback = row.querySelector('.invalid-feedback');
      feedback.textContent = message;
      // The feedback sits at row level, so it needs d-block instead of Bootstrap's sibling rule.
      feedback.classList.toggle('d-block', Boolean(message));
      for (const input of row.querySelectorAll('input[data-field]')) {
        const offending = Boolean(message) && input.dataset.field === field;
        input.classList.toggle('is-invalid', offending);
        // Rewritten on every render, so no stale reference survives a splice or a re-render.
        if (offending) input.setAttribute('aria-invalid', 'true');
        else input.removeAttribute('aria-invalid');
        const described = [input.dataset.help || '', offending ? feedback.id : ''].filter(Boolean).join(' ');
        if (described) input.setAttribute('aria-describedby', described);
        else input.removeAttribute('aria-describedby');
      }
      if (message) invalid = true;
    }
    if (invalid) {
      pendingKeys.delete('devices');                 // an invalid row is never sent …
      setSectionState('devices', 'incomplete', DEVICES_INCOMPLETE_MSG);
    } else if (saveSections.get('devices')?.state === 'incomplete') {
      // … and leaving the invalid state must not claim success. 'failed' and 'saving' are left
      // alone — they are not this function's to clear.
      setSectionState('devices', pendingKeys.has('devices') ? 'unsaved' : 'idle');
    } else {
      renderSaveState();
    }
    return !invalid;
  }

  // Rebuilds exactly the device card, so every change handler is re-bound to the current draft
  // objects. Used after a successful add, after Discard changes, and after a removal.
  function rebuildDeviceSection() {
    const host = document.getElementById('device-editor') || $('settings-sections')?.querySelector('.device-settings');
    const body = host?.parentElement;
    if (!body) return; // not on this page, or the editor was never rendered
    preserveFocus(() => {
      body.replaceChildren(element('h2', 'h5 mb-3', 'Inverters'));
      renderDevicesSettings(body);
    });
  }

  // The "rebuild consistently" branch: only ever on the operator's explicit request.
  function discardDeviceChanges() {
    adoptDevices(structuredClone(settingsCommitted.devices || []));
    pendingKeys.delete('devices');
    setSectionState('devices', 'idle');
    rebuildDeviceSection();
  }

  function renderDevicesSettings(card) {
    const host = element('div', 'device-settings');
    host.id = 'device-editor'; // the anchor the persistent devices alert is inserted before
    const devices = settingsDraft.devices || (settingsDraft.devices = []);
    // One empty row is always offered, because "Add inverter" now confirms a row instead of
    // creating one; without it the dialog would open with nowhere to type.
    if (!devices.some(isBlankNewRow)) devices.push(withUid({ host: '', port: 8899, network_id: null }));
    for (const [index, device] of devices.entries()) {
      const row = element('div', 'device-settings-item');
      row.dataset.uid = device._uid;  // identity
      row.dataset.index = String(index); // rendering position only, never an identity
      const grid = element('div', 'row g-2 align-items-start');
      const hostCol = element('div', 'col-12 col-sm');
      const hostInput = element('input', 'form-control');
      hostInput.id = `device-${index}-host`;
      hostInput.dataset.field = 'host';
      hostInput.setAttribute('aria-label', 'IP address or host name');
      hostInput.placeholder = 'IP address or host name';
      hostInput.autocomplete = 'off';
      hostInput.value = String(device.host ?? '');
      hostCol.append(hostInput);
      const portCol = element('div', 'col-6 col-sm-3');
      const portInput = element('input', 'form-control');
      portInput.id = `device-${index}-port`;
      portInput.dataset.field = 'port';
      portInput.type = 'number';
      portInput.min = '1'; portInput.max = '65535'; portInput.inputMode = 'numeric';
      portInput.setAttribute('aria-label', 'Port');
      portInput.value = String(device.port ?? '');
      portCol.append(portInput);
      const networkCol = element('div', 'col-6 col-sm-3');
      // Visually hidden keeps the row aligned with the unlabelled host and port inputs.
      const networkLabel = element('label', 'visually-hidden', 'Network ID (optional, empty means directly attached)');
      networkLabel.htmlFor = `device-${index}-network-id`;
      const networkInput = element('input', 'form-control');
      networkInput.id = `device-${index}-network-id`;
      networkInput.dataset.field = 'network_id';
      networkInput.type = 'number';
      networkInput.min = '0'; networkInput.max = String(MAX_NETWORK_ID); networkInput.inputMode = 'numeric';
      networkInput.placeholder = 'Network ID (optional)';
      // refreshDeviceState() composes aria-describedby from this plus the row's error node.
      networkInput.dataset.help = 'device-network-help';
      networkInput.setAttribute('aria-describedby', 'device-network-help');
      networkInput.value = device.network_id === null || device.network_id === undefined ? '' : String(device.network_id);
      networkCol.append(networkLabel, networkInput);
      const removeCol = element('div', 'col-6 col-sm-auto');
      const remove = element('button', 'btn btn-outline-danger');
      remove.type = 'button';
      remove.title = 'Remove inverter';
      remove.setAttribute('aria-label', `Remove inverter ${device.host || index + 1}`);
      const removeIcon = element('span', 'material-icons', 'delete_outline');
      removeIcon.setAttribute('aria-hidden', 'true');
      remove.append(removeIcon);
      // The trailing empty row has nothing to remove; it is re-created on every render anyway.
      if (!isBlankNewRow(device)) removeCol.append(remove);
      grid.append(hostCol, portCol, networkCol, removeCol);
      const feedback = element('div', 'invalid-feedback');
      feedback.id = `device-${index}-feedback`;
      row.append(grid, feedback);
      host.append(row);
      const onChange = () => {
        device.host = hostInput.value;
        device.port = portInput.value === '' ? null : Number(portInput.value);
        device.network_id = networkId(networkInput.value);
        if (refreshDeviceState(card) && !isBlankNewRow(device)) queueSettings('devices');
      };
      hostInput.addEventListener('change', onChange);
      portInput.addEventListener('change', onChange);
      networkInput.addEventListener('change', onChange);
      remove.addEventListener('click', () => {
        const restoreFocus = document.activeElement === remove;
        const wasSaved = !isBlankNewRow(device);
        const label = device.host || '';
        // confirm() is the established pattern here (token revocation uses it); an unsaved row is
        // removed silently, as before, because there is nothing to lose.
        if (wasSaved && !confirm(`Really remove inverter "${label}"?`)) return;
        const at = devices.indexOf(device);
        if (at < 0) return; // a stale closure must not delete a different row
        devices.splice(at, 1);
        rebuildDeviceSection();  // rebinds every handler to the current draft objects
        const editor = document.getElementById('device-editor');
        // Not delegated to preserveFocus(): the remove buttons carry no id, so it cannot restore them.
        if (restoreFocus) {
          const rows = editor?.querySelectorAll('.device-settings-item');
          (rows?.[Math.min(at, rows.length - 1)]?.querySelector('button')
            || editor?.parentElement?.querySelector('.device-settings ~ button'))?.focus();
        }
        if (editor?.parentElement && refreshDeviceState(editor.parentElement) && wasSaved) queueSettings('devices');
      });
    }
    const networkHelp = element('p', 'text-secondary small mt-2 mb-0', 'Network ID: leave empty for a direct connection. Only set it for an inverter reached through the master in the plant network.');
    networkHelp.id = 'device-network-help';
    host.append(networkHelp);
    if (devices.every(isBlankNewRow)) host.append(element('p', 'text-secondary small', 'No inverters configured yet.'));
    const add = element('button', 'btn btn-outline-primary mt-3', 'Add inverter');
    add.type = 'button';
    add.addEventListener('click', () => { addInverter(card, add); });
    card.append(host, add);
    refreshDeviceState(card);
  }

  // The click can land while the cursor still sits in a field, where no change event has fired yet,
  // so the live input values are read straight from the DOM. Returns true if the draft moved.
  function syncDeviceInputs(card) {
    let changed = false;
    for (const row of card.querySelectorAll('.device-settings-item')) {
      const device = deviceByUid(row.dataset.uid);
      if (!device) continue;
      const host = row.querySelector('input[data-field="host"]').value;
      const portValue = row.querySelector('input[data-field="port"]').value;
      const port = portValue === '' ? null : Number(portValue);
      const network = networkId(row.querySelector('input[data-field="network_id"]').value);
      if (device.host !== host || device.port !== port || device.network_id !== network) changed = true;
      device.host = host;
      device.port = port;
      device.network_id = network;
    }
    return changed;
  }

  // Bootstrap's JS bundle is loaded in base.html; the dismiss button is the fallback if it is not.
  function closeInvertersModal() {
    const modal = $('inverters-modal');
    if (!modal) return;
    const instance = window.bootstrap?.Modal?.getOrCreateInstance(modal);
    if (instance) instance.hide();
    else modal.querySelector('[data-bs-dismiss="modal"]')?.click();
  }

  // "Add inverter" confirms the row the operator just typed: save, then close. An invalid row keeps
  // the dialog open with the existing validation markers; a rejected save does too.
  async function addInverter(card, button) {
    const dirty = syncDeviceInputs(card);
    if (!refreshDeviceState(card)) {
      // Best effort: the marked field is where the operator has to go. Moving the focus there does
      // not take effect in headless Chromium, so the marker and the message carry the information.
      const invalid = $('settings-sections')?.querySelector('.device-settings-item input.is-invalid');
      invalid?.focus();
      return;
    }
    if (dirty && settingsDraft.devices.some((device) => !isBlankNewRow(device))) queueSettings('devices');
    button.disabled = true;
    clearTimeout(settingsTimer); // the click supersedes the pending debounce
    try {
      // Scoped to the devices section: a foreign key's rejection must not decide this dialog, and
      // a rejected devices save must keep it open (the alert and the toast say why).
      if (!(await flushSettings({ sections: ['devices'] }))) return;
    } finally {
      button.disabled = false;
    }
    // A fresh empty row for the next inverter, and the saved row now carries its server-side id.
    rebuildDeviceSection();
    closeInvertersModal();
  }

  // One builder for both renderSettings() and rerenderGroup(), so the heading suppression and the
  // Account relabel cannot drift and a group re-render cannot emit a second <h2>.
  function buildGroupBody(group) {
    const body = element('div', 'card-body');
    if (!(page === 'prometheus' && group === 'Prometheus')) body.append(element('h2', 'h5 mb-3', { Account: 'Administrator account' }[group] || group));
    if (group === 'Account') renderAccount(body);
    else if (group === 'Inverters') renderDevicesSettings(body);
    else for (const field of settingFields.filter((item) => item.group === group && Object.hasOwn(settingsDraft, item.key) && settingVisible(item))) body.append(settingControl(field));
    return body;
  }

  function groupSectionId(group) { return `settings-group-${group.toLowerCase().replace(/\s+/g, '-')}`; }

  // Replaces only the affected group, so unrelated sections — and their focus — are never touched.
  function rerenderGroup(group) {
    const section = document.getElementById(groupSectionId(group));
    if (!section) { renderSettings(); return; } // not on this page / never rendered as a <section>
    section.replaceChildren(buildGroupBody(group));
  }

  function renderSettings() {
    const host = $('settings-sections');
    host.replaceChildren();
    const groups = {
      settings: ['Access', 'Server', 'Network', 'Account'],
      dashboard: ['Inverters'],
      inverters: ['Write access'],
      prometheus: ['Prometheus'],
      tsdb: ['Export'],
    }[page] || [];
    for (const group of groups) {
      if (group === 'Export') { renderExportSettings(host); continue; }
      const section = element('section', 'card');
      section.id = groupSectionId(group);
      section.append(buildGroupBody(group));
      host.append(section);
    }
  }

  function renderAccount(body) {
    const change = element('a', 'btn btn-outline-secondary', 'Change password');
    change.href = '/change-password';
    body.append(change);
  }

  function showRestartNotice(keys) {
    let notice = $('restart-notice');
    if (!notice) {
      notice = element('div', 'alert alert-warning mt-3 mb-0');
      notice.id = 'restart-notice';
      notice.setAttribute('role', 'status');
      $('settings-sections').before(notice);
    }
    notice.hidden = !keys?.length;
    notice.textContent = keys?.length ? `Saved, but active only after a restart: ${keys.join(', ')}` : '';
  }

  async function initSettings() {
    const result = await api('settings');
    settingsCommitted = structuredClone(result.settings || {});
    settingsDraft = structuredClone(settingsCommitted);
    // The one place that mints a device identity, besides the blank trailing row.
    if (Object.hasOwn(settingsDraft, 'devices')) adoptDevices(settingsDraft.devices);
    renderSettings();
    showRestartNotice(result.restart_required);
    renderPrometheusSummary();
  }

  let parameterData = { available: [], exposed_names: [], write_names: [] };
  let parameterDirty = false;
  let parameterSending = false;
  let parameterTimer = null;
  let exposedPage = 0;
  const addMetricSelection = new Set();
  const EXPOSED_PAGE_SIZE = 15;

  function renderPrometheusSummary() {
    if (page !== 'prometheus') return;
    const enabled = Boolean(settingsDraft?.enable_metrics_endpoint);
    $('prometheus-status').textContent = enabled ? '● Active' : '○ Inactive';
    $('prometheus-status').className = enabled ? 'text-success' : 'text-secondary';
    $('prometheus-count').textContent = `${parameterData.exposed_names.length} metrics`;
  }

  function markPrometheusSaved() {
    if (page === 'prometheus') $('prometheus-last-saved').textContent = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date());
  }

  function queueParameters() {
    parameterDirty = true;
    setSectionState('parameters', 'unsaved');
    clearTimeout(parameterTimer);
    parameterTimer = setTimeout(flushParameters, 400);
  }

  async function flushParameters() {
    if (parameterSending || !parameterDirty) return;
    const payload = { exposed_names: [...parameterData.exposed_names], write_names: [...parameterData.write_names] };
    parameterDirty = false;
    parameterSending = true;
    setSectionState('parameters', 'saving');
    try {
      const result = await api('parameters', { method: 'PUT', body: JSON.stringify(payload) });
      markPrometheusSaved();
      setSectionState('parameters', 'saved');
      toast(result.restart_required?.length ? 'Parameters saved. A restart is required.' : 'Parameters saved.', result.restart_required?.length ? 'warning' : 'success');
    } catch (error) {
      // The reload re-establishes server truth, and it is exactly why this section gets no Retry:
      // by then parameterData holds the server's list, so a retry would re-PUT the server's own
      // state and could silently overwrite a concurrent change from another session.
      reportSaveFailure('parameters', failureMessage('parameters', messageFrom(error)));
      if (!parameterDirty) {
        try { parameterData = await api('parameters'); renderParameters(); }
        catch (loadError) { toast(messageFrom(loadError), 'danger'); }
      }
    } finally {
      parameterSending = false;
      renderSaveState();
      if (parameterDirty) flushParameters();
    }
  }

  function exposedMetricRow(parameter, index) {
    const row = element('tr', 'metric-row');
    row.dataset.name = parameter.name;
    const dragCell = element('td');
    const handle = element('span', 'material-icons metric-drag-handle', 'drag_indicator');
    handle.draggable = true;
    handle.title = `Drag to reorder ${parameter.name}`;
    handle.setAttribute('aria-hidden', 'true');
    handle.addEventListener('dragstart', (event) => { event.dataTransfer.setData('text/plain', parameter.name); event.dataTransfer.effectAllowed = 'move'; });
    dragCell.append(handle);
    const nameCell = element('th', 'metric-name');
    nameCell.scope = 'row';
    nameCell.title = parameter.name;
    const nameParts = parameter.name.split('_');
    nameParts.forEach((part, partIndex) => {
      nameCell.append(document.createTextNode(partIndex < nameParts.length - 1 ? `${part}_` : part));
      if (partIndex < nameParts.length - 1) nameCell.append(document.createElement('wbr'));
    });
    // Below 768px the description column is hidden, so the description moves into an expandable
    // native <details> inside the name cell instead of being unavailable there.
    if (parameter.description) {
      const mobile = element('details', 'metric-description-mobile');
      mobile.append(element('summary', '', 'Description'), element('span', '', parameter.description));
      nameCell.append(mobile);
    }
    const descriptionCell = element('td', 'text-secondary');
    descriptionCell.append(element('span', 'metric-description', parameter.description || '—'));
    if (parameter.description) descriptionCell.title = parameter.description;
    const actionCell = element('td', 'text-end');
    const menu = element('div', 'dropdown');
    const toggle = element('button', 'btn btn-outline-secondary btn-sm metric-menu-toggle');
    toggle.type = 'button'; toggle.dataset.bsToggle = 'dropdown'; toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', `Actions for ${parameter.name}`);
    toggle.append(element('span', 'material-icons', 'more_horiz'));
    const options = element('ul', 'dropdown-menu dropdown-menu-end');
    const action = (label, callback, disabled, key) => {
      const item = element('li');
      const button = element('button', 'dropdown-item', label);
      button.type = 'button'; button.disabled = disabled; button.dataset.action = key;
      button.setAttribute('aria-label', `${label}: ${parameter.name}`);
      button.addEventListener('click', callback);
      item.append(button); options.append(item);
    };
    action('Move up', () => moveParameter(parameter.name, -1), index === 0, 'up');
    action('Move down', () => moveParameter(parameter.name, 1), index === parameterData.exposed_names.length - 1, 'down');
    action('Remove', () => {
      parameterData.exposed_names = parameterData.exposed_names.filter((name) => name !== parameter.name);
      queueParameters(); renderParameters();
      $('exposed-search').focus();
    }, false, 'remove');
    menu.append(toggle, options); actionCell.append(menu);
    row.append(dragCell, nameCell, descriptionCell, actionCell);
    row.addEventListener('dragover', (event) => { event.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', (event) => { event.preventDefault(); row.classList.remove('drag-over'); reorderParameter(event.dataTransfer.getData('text/plain'), parameter.name); });
    return row;
  }

  function availableMetricRow(parameter) {
    const label = element('label', 'parameter-item');
    const box = element('input', 'form-check-input');
    box.type = 'checkbox'; box.value = parameter.name; box.checked = addMetricSelection.has(parameter.name);
    box.setAttribute('aria-label', `Add ${parameter.name}`);
    box.addEventListener('change', () => {
      if (box.checked) addMetricSelection.add(parameter.name);
      else addMetricSelection.delete(parameter.name);
      updateAddMetricsCount();
    });
    const main = element('span', 'parameter-main');
    main.append(element('strong', '', parameter.name));
    if (parameter.description) main.append(element('small', 'text-secondary', parameter.description));
    label.append(box, main);
    return label;
  }

  function updateAddMetricsCount() {
    if (page !== 'prometheus') return;
    const count = addMetricSelection.size;
    const overLimit = parameterData.exposed_names.length + count > 64;
    $('add-metrics-count').textContent = overLimit ? `${count} selected · maximum 64 exposed` : `${count} selected`;
    $('confirm-add-metrics').textContent = count ? `Add ${count} metrics` : 'Add metrics';
    $('confirm-add-metrics').disabled = !count || overLimit;
  }

  function moveParameter(name, step) {
    const index = parameterData.exposed_names.indexOf(name);
    const next = index + step;
    if (index < 0 || next < 0 || next >= parameterData.exposed_names.length) return;
    parameterData.exposed_names.splice(index, 1);
    parameterData.exposed_names.splice(next, 0, name);
    queueParameters();
    renderParameters();
    const row = [...$('exposed-list').children].find((item) => item.dataset.name === name);
    (row?.querySelector('.metric-menu-toggle') || $('exposed-search'))?.focus();
  }

  function reorderParameter(source, target) {
    if (!parameterData.available.some((item) => item.name === source && item.exportable !== false) || source === target) return;
    if (!parameterData.exposed_names.includes(source) && parameterData.exposed_names.length >= 64) { toast('At most 64 metrics can be exposed.', 'warning'); return; }
    parameterData.exposed_names = parameterData.exposed_names.filter((name) => name !== source);
    const targetIndex = parameterData.exposed_names.indexOf(target);
    parameterData.exposed_names.splice(targetIndex < 0 ? parameterData.exposed_names.length : targetIndex, 0, source);
    queueParameters();
    renderParameters();
  }

  function renderParameters() {
    const names = new Map(parameterData.available.map((item) => [item.name, item]));
    const exposedSearch = ($('exposed-search')?.value || '').trim().toLocaleLowerCase();
    const availableSearch = ($('parameter-search')?.value || '').trim().toLocaleLowerCase();
    const writeSearch = ($('write-search')?.value || '').trim().toLocaleLowerCase('en-GB');
    const matches = (item, term) => `${item.name} ${item.description || ''} ${item.help_text || ''}`.toLocaleLowerCase('en-GB').includes(term);
    const exposed = $('exposed-list') || element('tbody');
    const available = $('available-list') || element('div');
    const writable = $('writable-list') || element('div');
    exposed.replaceChildren(); available.replaceChildren(); writable.replaceChildren();
    if ($('exposed-list')) {
      const filtered = parameterData.exposed_names
        .map((name, index) => ({ item: names.get(name), index }))
        .filter(({ item }) => item?.exportable !== false && item && matches(item, exposedSearch));
      const pages = Math.max(1, Math.ceil(filtered.length / EXPOSED_PAGE_SIZE));
      exposedPage = Math.min(exposedPage, pages - 1);
      for (const { item, index } of filtered.slice(exposedPage * EXPOSED_PAGE_SIZE, (exposedPage + 1) * EXPOSED_PAGE_SIZE)) exposed.append(exposedMetricRow(item, index));
      if (!filtered.length) {
        const empty = element('tr');
        const cell = element('td', 'text-secondary py-4', exposedSearch ? 'No matching metrics.' : 'No metrics selected.');
        cell.colSpan = 4; empty.append(cell); exposed.append(empty);
      }
      $('exposed-range').textContent = filtered.length ? `${exposedPage * EXPOSED_PAGE_SIZE + 1}–${Math.min((exposedPage + 1) * EXPOSED_PAGE_SIZE, filtered.length)} of ${filtered.length}` : '0 metrics';
      $('exposed-prev').disabled = exposedPage === 0;
      $('exposed-next').disabled = exposedPage >= pages - 1;
      renderPrometheusSummary();
    }
    let availableMatches = 0;
    for (const item of parameterData.available) {
      if (page === 'prometheus' && item.exportable !== false && !parameterData.exposed_names.includes(item.name) && matches(item, availableSearch)) {
        availableMatches += 1;
        if (availableMatches <= 100) available.append(availableMetricRow(item));
      }
      if (page !== 'inverters') continue;
      if (!item.writable || !matches(item, writeSearch)) continue;
      const row = element('label', 'parameter-item');
      const box = element('input', 'form-check-input');
      box.type = 'checkbox';
      box.checked = parameterData.write_names.includes(item.name);
      box.setAttribute('aria-label', `Write access for ${item.name}`);
      box.addEventListener('change', () => {
        parameterData.write_names = box.checked ? [...parameterData.write_names, item.name] : parameterData.write_names.filter((name) => name !== item.name);
        queueParameters();
      });
      const description = element('span', 'parameter-main');
      description.append(element('strong', '', item.name));
      if (item.description) description.append(element('small', 'text-secondary', item.description));
      if (item.help_text) {
        // Visible text rather than a tooltip, and tied to the checkbox so a screen reader reads it
        // with the control. Parameter names match [a-z][a-z0-9_]{0,63}, so the id stays unique.
        const help = element('small', 'parameter-help', item.help_text);
        help.id = `write-help-${item.name}`;
        box.setAttribute('aria-describedby', help.id);
        description.append(help);
      }
      row.append(box, description);
      writable.append(row);
    }
    if (availableMatches > 100) available.append(element('p', 'text-secondary small', `${availableMatches - 100} more metrics. Refine the search to select them.`));
    if (page === 'prometheus' && !available.childElementCount) available.append(element('p', 'text-secondary small', availableSearch ? 'No matching metrics.' : 'All metrics are exposed.'));
    if (page === 'inverters' && !writable.childElementCount) writable.append(element('p', 'text-secondary small', writeSearch ? 'No matching parameters.' : 'No writable parameters available.'));
    updateAddMetricsCount();
  }

  async function initParameters() {
    parameterData = await api('parameters');
    renderParameters();
    $('parameter-search')?.addEventListener('input', renderParameters);
    $('copy-metrics-endpoint')?.addEventListener('click', async () => {
      if (await copyToClipboard(`${location.origin}/metrics`)) toast('Metrics endpoint copied.');
      else toast('Could not copy the metrics endpoint.', 'danger');
    });
    $('exposed-search')?.addEventListener('input', () => { exposedPage = 0; renderParameters(); });
    $('exposed-prev')?.addEventListener('click', () => { exposedPage -= 1; renderParameters(); });
    $('exposed-next')?.addEventListener('click', () => { exposedPage += 1; renderParameters(); });
    $('write-search')?.addEventListener('input', renderParameters);
    $('confirm-add-metrics')?.addEventListener('click', () => {
      const additions = parameterData.available.filter((item) => addMetricSelection.has(item.name) && item.exportable !== false && !parameterData.exposed_names.includes(item.name)).map((item) => item.name);
      if (!additions.length) return;
      if (parameterData.exposed_names.length + additions.length > 64) { toast('At most 64 metrics can be exposed.', 'warning'); return; }
      parameterData.exposed_names.push(...additions);
      addMetricSelection.clear();
      queueParameters(); renderParameters();
      window.bootstrap.Modal.getInstance($('add-metrics-modal'))?.hide();
    });
    // sectionNotice() refuses to build the devices alert while the dialog is hidden (the failure is
    // a toast then), so nothing holds the alert for a save that failed with the dialog closed.
    // Re-running the single writer on show rebuilds it for a section whose failure is still open.
    $('inverters-modal')?.addEventListener('shown.bs.modal', renderSaveState);
    $('add-metrics-modal')?.addEventListener('shown.bs.modal', () => $('parameter-search').focus());
    $('add-metrics-modal')?.addEventListener('hidden.bs.modal', () => { addMetricSelection.clear(); $('parameter-search').value = ''; renderParameters(); });
    $('exposed-list')?.addEventListener('dragover', (event) => event.preventDefault());
    $('exposed-list')?.addEventListener('drop', (event) => {
      if (event.target.closest('.metric-row')) return;
      event.preventDefault();
      reorderParameter(event.dataTransfer.getData('text/plain'), '');
    });
  }

  async function bootstrap() {
    if (['login', 'change-password'].includes(page)) {
      // The handler is already registered; the session only fetches the CSRF token up front.
      try { await ensureSession(); }
      catch { setAuthInitError('The login form could not be prepared. Please reload the page.'); }
      return;
    }
    try {
      await session();
      if (page === 'dashboard') { initDashboardPolling(); await Promise.all([loadDashboard(), loadMetricCount(), initSettings()]); }
      else if (page === 'tokens') initTokens();
      else if (['settings', 'inverters', 'prometheus', 'tsdb'].includes(page)) {
        await initSettings();
        if (page === 'inverters' || page === 'prometheus') await initParameters();
      }
    } catch (error) { toast(messageFrom(error), 'danger'); }
  }

  function start() {
    initShell();
    // Registered during page parse, before any await: without JS the form falls back to its own
    // method="post", and with JS the handler is there from the first keystroke on.
    if (page === 'login') initAuthForm('login-form', submitLogin);
    else if (page === 'change-password') initAuthForm('change-password-form', submitPasswordChange);
    bootstrap().catch((error) => toast(messageFrom(error), 'danger')); // last-resort net
  }

  start();
})();
