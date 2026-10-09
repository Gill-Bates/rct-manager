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

  // Auto-dismiss delays: a warning or error is something the operator must be able to read and act on.
  const TOAST_TIMEOUT_MS = { success: 3500, info: 3500, warning: 12000, danger: 30000 };
  const NETWORK_ERROR_TEXT = 'The server did not respond. Check the connection and try again.';

  function toast(message, kind = 'success') {
    const region = $('toast-region');
    // An identical message already showing (or still sliding out) is suppressed instead of
    // stacking a visual duplicate, e.g. two call sites reporting the same failed request.
    const duplicate = [...region.children].some((node) => node.dataset.message === message && node.dataset.kind === kind);
    if (duplicate) return;
    const box = element('div', `alert alert-${kind} d-flex align-items-start gap-2`);
    box.dataset.message = message;
    box.dataset.kind = kind;
    const close = element('button', 'btn-close');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss notification');
    box.append(element('span', 'flex-grow-1', message), close);
    region.append(box);
    requestAnimationFrame(() => box.classList.add('is-visible'));
    const remove = () => box.remove();
    let timer = null;
    let started = 0;
    let remaining = TOAST_TIMEOUT_MS[kind] ?? TOAST_TIMEOUT_MS.success;
    const leave = () => {
      clearTimeout(timer);
      if (box.classList.contains('is-leaving')) return;
      box.classList.remove('is-visible');
      box.classList.add('is-leaving');
      box.addEventListener('transitionend', remove, { once: true });
      setTimeout(remove, 400); // fallback if the transition does not fire (e.g. display: none ancestor)
    };
    const arm = () => { started = Date.now(); timer = setTimeout(leave, remaining); };
    // Hover or keyboard focus pauses the countdown so a message can be read without racing it.
    const pause = () => {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
      remaining = Math.max(2000, remaining - (Date.now() - started));
    };
    const resume = () => { if (timer === null && !box.matches(':hover, :focus-within')) arm(); };
    box.addEventListener('mouseenter', pause);
    box.addEventListener('focusin', pause);
    box.addEventListener('mouseleave', resume);
    box.addEventListener('focusout', resume);
    close.addEventListener('click', leave);
    arm();
  }

  function messageFrom(error) {
    // fetch() rejects with a bare TypeError ("Failed to fetch" / "Load failed" / "NetworkError ...").
    if (error instanceof TypeError) return NETWORK_ERROR_TEXT;
    return error instanceof Error ? error.message : 'An unknown error occurred.';
  }

  // Themed replacement for window.confirm(): resolves true only through the explicit accept button;
  // focus starts on the safe (cancel) button.
  let confirmOpen = false;
  function confirmAction({ title, message, confirmLabel, danger = false }) {
    if (confirmOpen) return Promise.resolve(false);
    confirmOpen = true;
    const modalElement = $('confirm-modal');
    const modal = window.bootstrap.Modal.getOrCreateInstance(modalElement);
    const accept = $('confirm-accept');
    $('confirm-title').textContent = title;
    $('confirm-message').textContent = message;
    accept.textContent = confirmLabel;
    accept.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
    return new Promise((resolve) => {
      let accepted = false;
      const onAccept = () => { accepted = true; modal.hide(); };
      accept.addEventListener('click', onAccept, { once: true });
      modalElement.addEventListener('shown.bs.modal', () => $('confirm-cancel').focus(), { once: true });
      modalElement.addEventListener('hidden.bs.modal', () => {
        accept.removeEventListener('click', onAccept);
        confirmOpen = false;
        // Hiding this modal clears Bootstrap's body lock even when another modal (e.g. the inverter dialog) is still open.
        if (document.querySelector('.modal.show')) document.body.classList.add('modal-open');
        resolve(accepted);
      }, { once: true });
      modal.show();
    });
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
    let response;
    try {
      response = await fetch(`/admin/api/${path}`, {
        credentials: 'same-origin',
        ...options,
        headers: {
          ...(method !== 'GET' ? { 'X-CSRF-Token': csrfToken } : {}),
          ...(options.body ? { 'Content-Type': 'application/json' } : {}),
          ...options.headers,
        },
      });
    } catch (error) {
      // A genuine network failure opens the connection-lost modal at once; a caller-initiated
      // abort (supersede or timeout) says nothing about the link.
      const callerAbort = options.signal?.aborted || error?.name === 'AbortError' || error?.name === 'TimeoutError';
      if (!callerAbort) window.RCTReconnect?.start();
      throw error;
    }
    if (response.ok && window.RCTReconnect?.isActive()) window.RCTReconnect.stop();
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && !['login', 'change-password'].includes(page)) {
        location.assign('/login');
      }
      const detail = data.detail;
      // dispatch_capability_conflict (409) carries a structured detail {detail, operation_id, mode}
      // instead of a plain string; naming the blocking operation is what makes the refusal useful.
      // FastAPI's 422 detail is an array of {msg, ...}; only the documented conflict shape is formatted
      // as operation/mode.
      let message = `Request failed (${response.status}).`;
      if (typeof detail === 'string') message = detail;
      else if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') message = detail[0].msg;
      else if (detail && typeof detail === 'object' && 'operation_id' in detail && 'mode' in detail) {
        message = `${detail.detail || 'Conflict.'} (operation ${detail.operation_id}, mode ${detail.mode})`;
      }
      const error = new Error(message);
      error.status = response.status; // callers (e.g. the Energy poll) branch on 503 vs transient
      throw error;
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

  const AUTH_FIELD_IDS = ['username', 'password', 'current-password', 'new-password', 'confirm-password'];

  function clearFormError() {
    $('form-error').hidden = true;
    for (const id of AUTH_FIELD_IDS) { $(id)?.removeAttribute('aria-invalid'); $(id)?.removeAttribute('aria-describedby'); }
  }

  // Focus goes to the offending field (the alert itself is announced by role="alert"); without one
  // the alert box takes focus.
  function formError(message, fieldId) {
    clearFormError();
    const box = $('form-error');
    box.textContent = message;
    box.hidden = false;
    const field = fieldId ? $(fieldId) : null;
    if (field) {
      field.setAttribute('aria-invalid', 'true');
      field.setAttribute('aria-describedby', 'form-error');
      field.focus();
    } else box.focus();
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
    syncThemeToggle();
  }

  // Reflect the active theme on #theme-toggle: aria-pressed reports the state to assistive tech,
  // and the glyph plus the label name the action the button performs next (A11Y-05). Initialised
  // from the data-bs-theme attribute theme.js already applied before paint.
  function syncThemeToggle() {
    const button = $('theme-toggle');
    if (!button) return;
    const dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    button.setAttribute('aria-pressed', String(dark));
    const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
    button.setAttribute('aria-label', label);
    button.title = label;
    const icon = button.querySelector('.material-icons');
    if (icon) icon.textContent = dark ? 'light_mode' : 'dark_mode';
  }

  function initShell() {
    $('theme-toggle')?.addEventListener('click', toggleTheme);
    syncThemeToggle();
    const toggle = $('nav-toggle');
    const setNav = (open) => {
      const nav = $('mobile-nav');
      nav.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    };
    toggle?.addEventListener('click', () => setNav($('mobile-nav').hidden));
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !toggle || $('mobile-nav').hidden) return;
      setNav(false);
      toggle.focus();
    });
    document.addEventListener('click', (event) => {
      if (!toggle || $('mobile-nav').hidden || event.target.closest('#mobile-nav, #nav-toggle')) return;
      setNav(false);
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
    clearFormError();
    submitState(form, true);
    try {
      const result = await api('login', { method: 'POST', body: JSON.stringify({ username: $('username').value.trim(), password: $('password').value }) });
      location.assign(result.must_change_password ? '/change-password' : '/ui/dashboard');
    } catch (error) { formError(messageFrom(error), 'password'); submitState(form, false); }
  }

  async function submitPasswordChange(form) {
    clearFormError();
    const next = $('new-password').value;
    if (next !== $('confirm-password').value) { formError('The new passwords do not match.', 'confirm-password'); return; }
    submitState(form, true);
    try {
      await api('change-password', { method: 'POST', body: JSON.stringify({ current_password: $('current-password').value, new_password: next }) });
      location.assign('/ui/dashboard');
    } catch (error) {
      const message = messageFrom(error);
      formError(message, /current/i.test(message) ? 'current-password' : 'new-password');
      submitState(form, false);
    }
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
    // Whole days until the next battery calibration. power_mng_bat_next_calib_date is a UNIX
    // timestamp in SECONDS that belongs to the power manager, not a tower, so it is only mapped for
    // the first/primary tower - take it from the first device that actually reports it. Same
    // availability guard as the battery card's "Next calibration" cell: finite and > 0, and the
    // resulting Date must parse. Rounding: ceil, so a date later today still reads "1 day" rather
    // than "0 days" and only a date already in the past (clamped to 0) shows "0".
    const calibItem = all.find((item) => item.name === 'power_mng_bat_next_calib_date' &&
      Number.isFinite(Number(item.value)) && Number(item.value) > 0);
    const calibDate = calibItem ? new Date(Number(calibItem.value) * 1000) : null;
    const calibValid = Boolean(calibDate && !Number.isNaN(calibDate.getTime()));
    const daysToCalibration = calibValid
      ? Math.max(0, Math.ceil((calibDate.getTime() - Date.now()) / 86400000))
      : null;
    if (calibValid) setReading($('days-to-calibration'), String(daysToCalibration), daysToCalibration === 1 ? 'day' : 'days');
    else setReading($('days-to-calibration'), 'n/a');
  }

  // { name, label, icon, kind } per cell of the power card's grid, in reading order. The inverter
  // state is deliberately not a cell: the card header chip already shows it. 'label'
  // takes the server-decoded enum text instead of a number; the icon is decorative (aria-hidden).
  const POWER_CELLS = [
    { name: 'solar_a_power', label: 'PV A', icon: 'wb_sunny' },
    { name: 'solar_b_power', label: 'PV B', icon: 'wb_sunny' },
    { name: 'grid_power', label: 'Grid power', icon: 'factory' },
    { name: 'ac_power', label: 'AC power', icon: 'bolt' },
    { name: 'heat_sink_temperature', label: 'Heat sink', icon: 'device_thermostat', optional: true },
  ];
  // The battery card's fact grid below the charge level and its bar. Cells name a *role*, not a
  // metric: the server maps each role to the catalog name that belongs to this particular tower
  // (app/admin/api.py, _battery_metric_names), so two towers no longer render the same numbers. A
  // role the server reports no name for is dropped from that tower's grid - 'cycles', 'soc_target'
  // and 'next_calibration' have no per-tower register, so they are only mapped for the first tower
  // and the second tower simply does not show them.
  const BATTERY_CELLS = [
    { role: 'temperature', label: 'Temperature', icon: 'device_thermostat', optional: true },
    { role: 'cycles', label: 'Charge cycles', icon: 'loop', optional: true },
    { role: 'soc_target', label: 'SOC target', icon: 'flag', optional: true },
    { role: 'next_calibration', label: 'Next calibration', icon: 'event_repeat', optional: true },
  ];

  // Metric names that prove a battery system is attached; without any of them no battery card is
  // rendered at all rather than an empty one. Fallback only — used when the server's `batteries`
  // list (below) is empty, e.g. a registry without the module_sn_0..6 slots.
  const BATTERY_PRESENCE = ['battery_soc', 'battery_temperature', 'battery_cycles', 'battery_status2',
    'battery_placeholder_0_status2'];

  // Describes the battery cards to render for one device. /admin/api/devices (app/admin/api.py,
  // devices()) returns a `batteries` list, one entry per physical tower sharing the inverter. Each
  // entry carries its own title, its own role->metric-name map and its own module report, so every
  // tower is rendered from its own readings instead of from shared `battery_*` names.
  //
  // `module_count` is the number of modules *in that one tower*, not the number of towers, and it is
  // null whenever the server could not derive a trustworthy count; `module_count_status` says which
  // case it is ("ok" / "pending" / "anomaly", see _battery_module_report).
  //
  // A registry without any module_sn slots reports no `batteries` entries at all; BATTERY_PRESENCE
  // then falls back to a single descriptor so older/simpler registries still show one battery card.
  function batteryTowers(metrics, batteries) {
    if (Array.isArray(batteries) && batteries.length) {
      return batteries.map((battery, index) => ({
        id: battery.id,
        title: battery.title || `Battery ${index + 1}`,
        metrics: battery.metrics && typeof battery.metrics === 'object' ? battery.metrics : {},
        moduleCount: typeof battery.module_count === 'number' ? battery.module_count : null,
        moduleCountStatus: battery.module_count_status || (battery.module_count === null ? 'anomaly' : 'ok'),
        populatedModuleSlots: Array.isArray(battery.populated_module_slots) ? battery.populated_module_slots : [],
      }));
    }
    if (!BATTERY_PRESENCE.some((name) => metrics.has(name))) return [];
    return [{
      id: 'battery',
      title: 'Battery 1',
      metrics: {
        soc: 'battery_soc', temperature: 'battery_temperature', status: 'battery_status2',
        cycles: 'battery_cycles', soc_target: 'battery_soc_target',
        next_calibration: 'power_mng_bat_next_calib_date',
      },
      moduleCount: null, moduleCountStatus: 'pending', populatedModuleSlots: [],
    }];
  }

  // The three battery slices (app/admin/static/img/battery_{top,middle,bottom}.svg) share a 220-wide
  // viewBox and are drawn flush to their own top/bottom edges with no transparent margin, so
  // stacking top + middle*N + bottom at one common rendered width with no gap reproduces one
  // continuous case. Height per slice follows from its own viewBox aspect ratio at that width, so
  // slices never need matching/hardcoded heights. These viewBox heights must stay in sync with the
  // SVGs.
  const BATTERY_SLICE_VIEWBOX_WIDTH = 220;
  const BATTERY_SLICES = {
    top: { file: 'battery_top.svg', viewBoxHeight: 70 },
    middle: { file: 'battery_middle.svg', viewBoxHeight: 109 },
    bottom: { file: 'battery_bottom.svg', viewBoxHeight: 132 },
  };
  // Upper bound for the rendered tower, sized so a full 5-module tower is flush with the readings
  // column (charge bar plus two metric rows) beside it. In viewBox units an assembled tower is
  // 70 + N*109 + 132, i.e. 420 units at N=2 and 856 at N=6, so a fixed width makes a six-module
  // tower more than twice as tall as a two-module one and drags the whole card down. The width is
  // therefore derived from the cap for the given module count: a tall tower gets *narrower*, never
  // shorter and never fewer modules, so the module count stays visually apparent.
  const BATTERY_TOWER_MAX_HEIGHT = 207;
  // Width a short tower is allowed to reach; also the battery card's image column (5rem in admin.css).
  const BATTERY_SLICE_MAX_WIDTH = 80;

  function batteryTowerViewBoxHeight(moduleCount) {
    return BATTERY_SLICES.top.viewBoxHeight + moduleCount * BATTERY_SLICES.middle.viewBoxHeight
      + BATTERY_SLICES.bottom.viewBoxHeight;
  }

  // Shared slice width that keeps the assembled tower inside BATTERY_TOWER_MAX_HEIGHT.
  function batterySliceWidth(moduleCount) {
    const units = batteryTowerViewBoxHeight(moduleCount);
    const fitted = (BATTERY_TOWER_MAX_HEIGHT * BATTERY_SLICE_VIEWBOX_WIDTH) / units;
    return Math.max(1, Math.min(BATTERY_SLICE_MAX_WIDTH, Math.floor(fitted)));
  }

  function batterySliceImage(kind, width) {
    const { file, viewBoxHeight } = BATTERY_SLICES[kind];
    const image = element('img', 'device-battery-slice');
    image.src = `/admin/static/img/${file}`;
    image.alt = '';
    image.width = width;
    image.height = Math.round((width * viewBoxHeight) / BATTERY_SLICE_VIEWBOX_WIDTH);
    return image;
  }

  // Top once, N middles, bottom once — DOM order top-to-bottom, matching the visual stack order.
  function buildBatteryStack(moduleCount) {
    const width = batterySliceWidth(moduleCount);
    const nodes = [batterySliceImage('top', width)];
    for (let i = 0; i < moduleCount; i += 1) nodes.push(batterySliceImage('middle', width));
    nodes.push(batterySliceImage('bottom', width));
    return nodes;
  }

  // Hardware limit (server-side validation concept, used only for the diagnostic note text below):
  // the catalog's seven serial slots are the size of a data structure, the documented hardware
  // takes at most six modules per tower.
  const BATTERY_MAX_MODULES_PER_TOWER = 6;

  // GRAPHIC ceiling — a different concept from the hardware limit above. The drawn tower contract
  // is exactly 1 top cap + 1..5 battery segments + 1 bottom cap, so this is the hard cap on what
  // buildBatteryStack() is ever asked to draw, independent of how many modules the server trusts.
  const BATTERY_TOWER_MAX_SEGMENTS = 5;

  // Maps a trusted backend module count onto a drawn segment count: 1..5 modules map 1:1, the
  // hardware maximum of 6 is drawn with the 5 segments of the graphic ceiling (the note keeps the
  // true count); anything beyond the hardware maximum is not drawn.
  function renderableSegmentCount(moduleCount) {
    if (typeof moduleCount !== 'number' || moduleCount < 1) return null;
    if (moduleCount > BATTERY_MAX_MODULES_PER_TOWER) return null;
    // 1..5 modules map 1:1; the documented 6th module shares the 5th segment (graphic ceiling).
    return Math.min(moduleCount, BATTERY_TOWER_MAX_SEGMENTS);
  }

  // null -> nothing renderable (patchBatteryCard shows a neutral note instead of a tower). No
  // module tower is fabricated for "pending" (no complete serial scan yet) — see patchBatteryCard
  // for the "detecting modules…" placeholder text that covers that case instead of a drawn tower.
  function renderableModuleCount(tower) {
    if (tower.moduleCountStatus === 'pending') return null;
    return renderableSegmentCount(tower.moduleCount);
  }

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

  // One cell of a card's key-value grid: decorative icon, then a <dl> holding only the label/value
  // pair. The icon is a sibling of the <dl>, never inside the dt/dd group — a <dl> div-wrapper may
  // contain only dt and dd, so the grid container is a plain <div> and each pair gets its own <dl>
  // (A11Y-02).
  function createMetricCell({ label, icon }) {
    const node = element('div', 'device-metric-card');
    if (icon) {
      const iconNode = element('span', 'material-icons device-metric-card-icon', icon);
      iconNode.setAttribute('aria-hidden', 'true');
      node.append(iconNode);
    }
    const list = element('dl', 'device-metric-pair mb-0');
    const dd = element('dd', 'mb-0');
    list.append(element('dt', 'fw-normal', label), dd);
    node.append(list);
    return { node, dd };
  }

  // Header shared by the power card and every battery card: icon + title left, status chip right.
  // Both start on the same baseline, which is what keeps the cards reading as equal columns.
  function createCardHead(title, icon) {
    const head = element('div', 'device-subcard-head');
    const titleNode = element('h4', 'device-subcard-title mb-0');
    const iconNode = element('span', 'material-icons', icon);
    iconNode.setAttribute('aria-hidden', 'true');
    const titleText = element('span', null, title);
    titleNode.append(iconNode, titleText);
    const chip = element('span', 'device-chip');
    const dot = element('span', 'device-chip-dot');
    dot.setAttribute('aria-hidden', 'true');
    const text = element('span');
    chip.append(dot, text);
    chip.hidden = true;
    head.append(titleNode, chip);
    // titleText is exposed separately so a renamed tower ("Battery 1" -> "Battery 2") can be patched
    // without touching the decorative icon beside it.
    return { node: head, chip, chipText: text, titleText };
  }

  function cardImage(imageName, width, height) {
    const image = element('img', 'device-subcard-image');
    image.src = `/admin/static/img/${imageName}`;
    image.alt = '';
    image.width = width;
    image.height = height;
    return image;
  }

  // Power card: header, inverter image on the left, a fixed 2-column key-value grid on the right.
  function createPowerCard() {
    const card = element('section', 'device-subcard device-subcard-power');
    const head = createCardHead('Inverter / Power', 'bolt');
    // Plain <div>, not <dl>: each cell carries its own <dl> so the per-cell icon never sits inside
    // a dt/dd group (A11Y-02).
    const grid = element('div', 'device-metric-grid mb-0');
    const cells = POWER_CELLS.map((spec) => {
      const { node, dd } = createMetricCell(spec);
      grid.append(node);
      return { ...spec, dd, node };
    });
    const body = element('div', 'device-subcard-body');
    body.append(cardImage('rct-inverter.svg', 120, 127), grid);
    card.append(head.node, body);
    return { node: card, head, grid, cells, statusMetric: 'inverter_state' };
  }

  // Battery card: header, tower image on the left, and on the right the charge level with its bar
  // directly beneath the value, then a 2x2 fact grid. Same outer shape as the power card.
  // Identity of a tower descriptor for reuse purposes: same tower and same metric mapping means the
  // existing card can be patched; a different mapping needs a fresh grid, so the card is rebuilt.
  function batteryTowerKey(tower) {
    return `${tower.id}\u0000${BATTERY_CELLS.map((cell) => tower.metrics[cell.role] || '').join('\u0001')}`;
  }

  function createBatteryCard(tower) {
    const card = element('section', 'device-subcard device-subcard-battery');
    const head = createCardHead(tower.title, 'battery_charging_full');
    const charge = element('div', 'device-charge');
    // The dt/dd charge pair gets its own <dl> so it has a valid list ancestor; the decorative SoC
    // bar stays a sibling of that <dl>, not a member of the dt/dd group (A11Y-02).
    const chargeList = element('dl', 'device-charge-pair mb-0');
    const chargeValue = element('dd', 'mb-0 device-charge-value');
    chargeList.append(element('dt', 'fw-normal', 'Charge level'), chargeValue);
    charge.append(chargeList);
    const bar = element('div', 'device-soc-bar');
    bar.setAttribute('aria-hidden', 'true');  // the percentage is printed right above it
    bar.append(element('div', 'device-soc-fill'));
    charge.append(bar);
    const grid = element('div', 'device-metric-grid mb-0');
    // Only the roles this tower actually has a metric name for; a role the device reports per
    // device rather than per tower is absent from the second tower's map and gets no cell here.
    const cells = BATTERY_CELLS.filter((spec) => tower.metrics[spec.role]).map((spec) => {
      const { node, dd } = createMetricCell(spec);
      grid.append(node);
      return { ...spec, name: tower.metrics[spec.role], dd, node };
    });
    const readings = element('div', 'device-subcard-readings');
    readings.append(charge, grid);
    const stack = element('div', 'device-battery-stack');
    // Shown instead of the tower when no module count can be trusted; see patchBatteryCard.
    const note = element('p', 'device-battery-note mb-0');
    note.hidden = true;
    const column = element('div', 'device-battery-column');
    column.append(stack, note);
    const body = element('div', 'device-subcard-body');
    body.append(column, readings);
    card.append(head.node, body);
    return {
      node: card, head, grid, cells, chargeValue, bar, stack, note, renderedModules: -1,
      towerKey: batteryTowerKey(tower), socMetric: tower.metrics.soc, statusMetric: tower.metrics.status,
    };
  }

  // Main area of a device card: the flow graphic panel and the detail panel (power card plus a
  // grid of battery cards). Both panels are created once; polls only patch their contents.
  function createDeviceVisual() {
    const wrap = element('div', 'device-visual device-item-main');
    const flow = energyFlowGraphic();
    const flowBox = element('div', 'device-flow-graphic device-flow-panel');
    flowBox.hidden = true;
    flowBox.append(flow.svg, flow.badges);
    const cards = element('div', 'device-visual-cards device-detail-panel');
    const inverterCard = createPowerCard();
    const batteryGrid = element('div', 'device-battery-grid');
    cards.append(inverterCard.node, batteryGrid);
    wrap.append(flowBox, cards);
    // batteries grows to match batteryTowers() on the first patch; see patchDeviceVisual.
    return { node: wrap, flow, flowBox, cards, batteryGrid, inverterCard, batteries: [] };
  }

  // Shows the server-decoded status text as a compact chip. A status that needs an explanation
  // carries it in the title tooltip only, so no long prose sits permanently in the card.
  // No cross-tower fallback: a tower shows its own status or none. Falling back from
  // battery_status2 to battery_placeholder_0_status2 made a second tower's status appear on the
  // first tower's chip, which is exactly the kind of shared-value bug this card had.
  function patchCardStatus(head, metrics, statusMetric) {
    const reading = statusMetric ? metrics.get(statusMetric) : undefined;
    if (!reading?.label) { head.chip.hidden = true; return; }
    const text = reading.label;
    setText(head.chipText, text);
    const notice = BATTERY_NOTICES.find(([pattern]) => pattern.test(text));
    const title = notice ? `${text} — ${notice[1]}` : text;
    if (head.chip.title !== title) head.chip.title = title;
    setClass(head.chip, 'device-chip-warning', Boolean(notice));
    head.chip.hidden = false;
  }

  // Fills one key-value cell. Returns false when the device reports no reading for it, so an
  // optional cell can be dropped from the grid instead of showing a permanent placeholder.
  function patchMetricCell(cell, metrics) {
    const metric = metrics.get(cell.name);
    if (!metric && cell.optional) return false;
    if (cell.kind === 'label') {
      const available = Boolean(metric?.label);
      setReading(cell.dd, available ? metric.label : 'n/a');
      setClass(cell.dd, 'text-secondary', !available);
      return available;
    }
    const value = metric && metric.value !== null && metric.value !== undefined ? Number(metric.value) : NaN;
    const calibrationDate = cell.name === 'power_mng_bat_next_calib_date' && Number.isFinite(value) && value > 0 ? new Date(value * 1000) : null;
    const available = cell.name === 'power_mng_bat_next_calib_date'
      ? Boolean(calibrationDate && !Number.isNaN(calibrationDate.getTime()))
      : Number.isFinite(value);
    const isGrid = cell.name === 'grid_power';
    const [number, unit] = calibrationDate
      ? [formatDate(calibrationDate, { time: true }), '']
      : metricParts(isGrid ? Math.abs(value) : value, metric?.unit);
    if (available) setReading(cell.dd, number, unit, isGrid ? gridFlow(value) : null); else setReading(cell.dd, 'n/a');
    setClass(cell.dd, 'text-secondary', !available);
    return available;
  }

  function patchCardGrid(card, metrics) {
    const nodes = [];
    for (const cell of card.cells) {
      const available = patchMetricCell(cell, metrics);
      if (!cell.optional || available) nodes.push(cell.node);
    }
    syncChildren(card.grid, nodes);
  }

  function patchBatteryCard(card, metrics, tower) {
    const wanted = renderableModuleCount(tower);
    // Rebuilds the slice stack only when the module count actually changed, not on every poll —
    // same no-churn pattern as syncChildren/patchDeviceVisual elsewhere in this file.
    if (card.renderedModules !== wanted) {
      card.renderedModules = wanted;
      if (wanted === null) {
        // Either "pending" (no complete serial scan yet - a neutral, temporary state, not a
        // fabricated tower) or "anomaly" (a complete scan whose populated slots do not describe a
        // documented tower). Both show no tower; the note text below tells them apart.
        card.stack.replaceChildren();
        card.note.hidden = false;
      } else {
        card.stack.replaceChildren(...buildBatteryStack(wanted));
        // The stack's CSS width is a fixed 5rem; the height cap needs the narrower per-count width.
        card.stack.style.width = `${batterySliceWidth(wanted)}px`;
      }
    }
    if (wanted !== null) {
      // The graphic caps at BATTERY_TOWER_MAX_SEGMENTS; the true module count stays readable as text.
      const capped = tower.moduleCount > wanted;
      card.note.hidden = !capped;
      if (capped) {
        setText(card.note, `${tower.moduleCount} modules`);
        const detail = `This tower has ${tower.moduleCount} modules; the graphic draws at most `
          + `${BATTERY_TOWER_MAX_SEGMENTS} battery segments.`;
        if (card.note.title !== detail) card.note.title = detail;
      }
    }
    if (wanted === null) {
      if (tower.moduleCountStatus === 'pending') {
        setText(card.note, 'Detecting modules…');
        const detail = 'The battery has not finished reporting its module serials yet; the module tower will '
          + 'appear once the scan completes.';
        if (card.note.title !== detail) card.note.title = detail;
      } else if (tower.moduleCountStatus === 'ok') {
        // A trusted count above the graphic's drawn ceiling (BATTERY_TOWER_MAX_SEGMENTS) — real
        // hardware the tower graphic contract (1 top + 1..5 segments + 1 bottom) does not cover,
        // not a data anomaly, so it gets its own message rather than "layout unclear".
        setText(card.note, `${tower.moduleCount} modules`);
        const detail = `This tower has ${tower.moduleCount} modules; the tower graphic currently shows at most `
          + `${BATTERY_TOWER_MAX_SEGMENTS}.`;
        if (card.note.title !== detail) card.note.title = detail;
      } else {
        const slots = tower.populatedModuleSlots || [];
        setText(card.note, 'Module layout unclear');
        const detail = slots.length
          ? `The battery reports module serials in slots ${slots.join(', ')}, which does not describe a documented tower (at most ${BATTERY_MAX_MODULES_PER_TOWER} modules, numbered without gaps). No module tower is drawn for it.`
          : 'No usable module serials were reported, so no module tower is drawn.';
        if (card.note.title !== detail) card.note.title = detail;
      }
    }
    patchCardStatus(card.head, metrics, card.statusMetric);
    const metric = card.socMetric ? metrics.get(card.socMetric) : undefined;
    const value = metric && metric.value !== null && metric.value !== undefined ? Number(metric.value) : NaN;
    const available = Number.isFinite(value);
    // The catalog documents the unit "ratio" only on battery_soc; battery_placeholder_0_soc carries
    // an empty unit for the same 0..1 quantity, so the charge reading falls back to ratio instead of
    // printing a bare 0.42 for the second tower.
    const socUnit = metric?.unit || 'ratio';
    const [number, unit] = metricParts(value, socUnit);
    if (available) setReading(card.chargeValue, number, unit); else setReading(card.chargeValue, 'n/a');
    setClass(card.chargeValue, 'text-secondary', !available);
    const percent = available ? Math.min(100, Math.max(0, socUnit === 'ratio' ? value * 100 : value)) : 0;
    const width = `${percent}%`;
    if (card.bar.firstElementChild.style.width !== width) card.bar.firstElementChild.style.width = width;
    patchCardGrid(card, metrics);
  }

  function patchDeviceVisual(visual, device) {
    const metrics = new Map((device.metrics || []).map((item) => [item.name, item]));
    patchCardStatus(visual.inverterCard.head, metrics, visual.inverterCard.statusMetric);
    patchCardGrid(visual.inverterCard, metrics);
    const towers = batteryTowers(metrics, device.batteries);
    // Battery cards are created and dropped as the reported towers change; the surviving cards
    // keep their nodes, so a poll does not rebuild the whole card.
    while (visual.batteries.length > towers.length) visual.batteries.pop();
    towers.forEach((tower, index) => {
      // A card is reused only while it still describes the same tower with the same metric mapping;
      // the fact cells are built from that mapping, so a changed one needs a new card.
      const existing = visual.batteries[index];
      if (!existing || existing.towerKey !== batteryTowerKey(tower)) {
        visual.batteries[index] = createBatteryCard(tower);
      }
      setText(visual.batteries[index].head.titleText, tower.title);
      patchBatteryCard(visual.batteries[index], metrics, tower);
    });
    setClass(visual.node, 'has-many-batteries', towers.length > 1);
    syncChildren(visual.batteryGrid, visual.batteries.map((card) => card.node));
    patchDeviceFlow(visual, device);
    syncChildren(visual.node, [visual.flowBox, visual.cards]);
  }

  // Feeds the shared flow graphic from device.energy_flow on the /admin/api/devices response
  // (business-signed, server-normalized, dispatch-independent) — never from device.metrics, which
  // is device-signed and has no battery_power. Hidden when the device has no energy_flow or every
  // reading in it is absent, so a device without energy data shows no graphic.
  function patchDeviceFlow(visual, device) {
    const readings = device.energy_flow;
    const hasReading = readings && Object.values(readings).some((reading) => reading && reading.value != null);
    visual.flowBox.hidden = !hasReading;
    setClass(visual.node, 'has-flow', Boolean(hasReading));
    if (hasReading) visual.flow.update(readings);
  }

  // Collapsed device ids live in localStorage (survives logins); read once, then kept in memory
  // so blocked storage or malformed JSON only costs persistence, never the dashboard.
  const COLLAPSED_KEY = 'rct-admin.collapsedDevices';
  const EXPANDED_KEY = 'rct-admin.expandedDevices';
  const collapseChoices = { [COLLAPSED_KEY]: null, [EXPANDED_KEY]: null };
  let deviceCardCount = 0;

  function choiceSet(storageKey) {
    if (collapseChoices[storageKey]) return collapseChoices[storageKey];
    const ids = new Set();
    collapseChoices[storageKey] = ids;
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey));
      if (Array.isArray(stored)) stored.forEach((id) => { if (typeof id === 'string') ids.add(id); });
    } catch { /* storage blocked or JSON malformed: fall back to the default */ }
    return ids;
  }

  // An explicit user choice wins; without one a single inverter starts open, several start closed.
  function isCollapsed(key, deviceCount) {
    if (choiceSet(COLLAPSED_KEY).has(key)) return true;
    if (choiceSet(EXPANDED_KEY).has(key)) return false;
    return deviceCount > 1;
  }

  function rememberCollapsed(key, collapsed) {
    choiceSet(collapsed ? COLLAPSED_KEY : EXPANDED_KEY).add(key);
    choiceSet(collapsed ? EXPANDED_KEY : COLLAPSED_KEY).delete(key);
    for (const storageKey of [COLLAPSED_KEY, EXPANDED_KEY]) {
      try { localStorage.setItem(storageKey, JSON.stringify([...choiceSet(storageKey)])); } catch { /* keep in-memory state */ }
    }
  }

  function setCardCollapsed(ref, collapsed) {
    ref.visual.node.hidden = collapsed;
    setClass(ref.card, 'is-collapsed', collapsed);
    ref.toggle.setAttribute('aria-expanded', String(!collapsed));
  }

  function createDeviceCard(key, deviceCount) {
    const toggle = element('button', 'device-toggle');
    toggle.type = 'button';
    const icon = element('span', 'material-icons', 'expand_more');
    icon.setAttribute('aria-hidden', 'true');
    toggle.append(icon);
    const ref = {
      toggle,
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
    ref.visual.node.id = `device-body-${++deviceCardCount}`;
    toggle.setAttribute('aria-controls', ref.visual.node.id);
    let collapsedNow = isCollapsed(key, deviceCount);
    setCardCollapsed(ref, collapsedNow);
    toggle.addEventListener('click', () => {
      collapsedNow = !collapsedNow;
      rememberCollapsed(key, collapsedNow);
      setCardCollapsed(ref, collapsedNow);
    });
    return ref;
  }

  function patchDeviceCard(ref, device) {
    const [status, statusClass] = displayStatus(device.status);
    const connected = statusClass === 'online';
    setClass(ref.card, 'has-status-badge', connected);
    setText(ref.statusBadge, status);
    ref.dot.className = `status-dot ${statusClass}`;
    setText(ref.title, device.name || device.id || 'Inverter');
    ref.toggle.setAttribute('aria-label', `Show or hide details of ${ref.title.textContent}`);
    setText(ref.statusText, status);
    // Name, address, last connection and the connection state share two compact header lines.
    syncChildren(ref.top, connected ? [ref.toggle, ref.title, ref.statusBadge] : [ref.toggle, ref.dot, ref.title, ref.statusText]);
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
  // Last good snapshot: a failed poll keeps showing it (dimmed, with a banner) instead of blanking the page.
  const SNAPSHOT_KEY = 'rct.dashboard.snapshot';
  const SNAPSHOT_MAX_AGE_MS = 60 * 60 * 1000;
  let dashboardSnapshot = null;
  let dashboardBanner = null;

  function saveSnapshot(snapshot) {
    try { localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(snapshot)); } catch { /* storage full or blocked */ }
  }

  function loadSnapshot() {
    try {
      const stored = JSON.parse(localStorage.getItem(SNAPSHOT_KEY) || 'null');
      if (stored && Array.isArray(stored.devices) && Number.isFinite(stored.at) && Date.now() - stored.at < SNAPSHOT_MAX_AGE_MS) return stored;
    } catch { /* corrupt entry: ignore */ }
    return null;
  }

  function setDashboardStale(error) {
    const grid = $('dashboard-grid') || $('devices-list');
    if (!dashboardBanner) {
      // Inline above the grid (not fixed), so it never covers the footer or the last tile row.
      dashboardBanner = element('div', 'alert alert-warning dashboard-offline-banner mt-3 mb-0');
      dashboardBanner.setAttribute('role', 'status');
      grid.before(dashboardBanner);
    }
    const at = new Date(dashboardSnapshot.at);
    const minutes = Math.floor((Date.now() - at.getTime()) / 60000);
    const age = minutes < 1 ? 'less than a minute ago' : `${minutes} min ago`;
    const reason = error?.status >= 500 ? `Server error (${error.status})` : 'Connection to server lost';
    dashboardBanner.textContent = `${reason} \u2013 showing data from ${hhmm(at)} (${age})`;
    dashboardBanner.hidden = false;
    grid.classList.add('is-offline');
  }

  function clearDashboardStale() {
    if (dashboardBanner) dashboardBanner.hidden = true;
    ($('dashboard-grid') || $('devices-list')).classList.remove('is-offline');
  }

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
    if (!tsdb.export_enabled) {
      icon.textContent = 'pause_circle';
      icon.classList.add('text-secondary');
      label.textContent = '–';
    } else if (tsdb.healthy) {
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
      if (!ref) { ref = createDeviceCard(key, devices.length); dashboardCards.set(key, ref); }
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
      // One network leg: the flow graphic is fed from device.energy_flow on this same response, a
      // dispatch-independent projection, not from a second energy/devices fetch.
      const data = await api('devices', { signal: controller.signal });
      if (generation !== dashboardGeneration) return 'skipped';
      dashboardPollFailing = false;
      dashboardFailures = 0;
      renderDashboard(Array.isArray(data.devices) ? data.devices : [], data.tsdb || null);
      dashboardSnapshot = { devices: Array.isArray(data.devices) ? data.devices : [], tsdb: data.tsdb || null, at: Date.now() };
      saveSnapshot(dashboardSnapshot);
      clearDashboardStale();
      return 'ok';
    } catch (error) {
      if (generation !== dashboardGeneration) return 'skipped';
      dashboardFailures += 1;
      // Server unreachable or failing: keep the last known data on screen (a reload restores it from storage).
      if (!dashboardSnapshot) {
        dashboardSnapshot = loadSnapshot();
        if (dashboardSnapshot) renderDashboard(dashboardSnapshot.devices, dashboardSnapshot.tsdb);
      }
      if (dashboardSnapshot) setDashboardStale(error);
      else syncChildren($('devices-list'), [dashboardNotices.error]);
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
    return new Intl.DateTimeFormat(undefined, time ? { dateStyle: 'medium', timeStyle: 'medium' } : { dateStyle: 'medium' }).format(date);
  }

  function tokenEmptyRow() {
    const row = element('tr');
    const cell = element('td', 'text-secondary', 'No API tokens created yet.');
    cell.colSpan = 6;
    row.append(cell);
    return row;
  }

  function tokenRoleCell(role) {
    const write = role !== 'read';
    const cell = element('td');
    cell.append(element('span', `token-role-badge ${write ? 'token-role-write' : 'token-role-read'}`, write ? 'Read and write' : 'Read'));
    return cell;
  }

  let tokensRequest = 0;

  async function loadTokens() {
    const host = $('tokens-list');
    const request = ++tokensRequest;
    const data = await api('tokens');
    if (request !== tokensRequest) return; // a newer load owns the table
    host.replaceChildren();
    if (!data.tokens?.length) { host.append(tokenEmptyRow()); return; }
    for (const token of data.tokens) {
      const row = element('tr');
      const nowrap = (text, extra = '') => element('td', `token-nowrap${extra}`, text);
      const actions = element('td', 'text-end token-nowrap');
      const revoke = element('button', 'btn btn-outline-danger btn-sm d-inline-flex align-items-center gap-1');
      const icon = element('span', 'material-icons', 'delete');
      icon.setAttribute('aria-hidden', 'true');
      revoke.append(icon, 'Revoke');
      revoke.type = 'button';
      revoke.setAttribute('aria-label', `Revoke token ${token.name}`);
      revoke.addEventListener('click', async () => {
        if (!await confirmAction({
          title: 'Revoke token?',
          message: `Applications using "${token.name}" lose access immediately. This cannot be undone.`,
          confirmLabel: 'Revoke token',
          danger: true,
        })) return;
        revoke.disabled = true;
        try { await api(`tokens/${encodeURIComponent(token.id)}`, { method: 'DELETE' }); row.remove(); toast('Token revoked.'); if (!host.childElementCount) host.append(tokenEmptyRow()); }
        catch (error) { revoke.disabled = false; toast(messageFrom(error), 'danger'); }
      });
      actions.append(revoke);
      row.append(
        element('td', '', token.name),
        tokenRoleCell(token.role),
        nowrap(formatDate(token.created_at, { time: true })),
        nowrap(formatDate(token.last_used_at, { time: true, empty: 'Never' })),
        nowrap(formatDate(token.expires_at, { time: true })),
        actions,
      );
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
    const modal = $('add-token-modal');
    const form = $('token-form');
    const showResult = (shown) => {
      $('token-form-state').hidden = shown;
      $('new-token-result').hidden = !shown;
      // The dialog is named by whichever title is visible.
      modal.setAttribute('aria-labelledby', shown ? 'token-created-title' : 'add-token-title');
    };
    modal.addEventListener('shown.bs.modal', () => { if (!$('token-form-state').hidden) $('token-name').focus(); });
    // The token is shown once: a stray backdrop click or Escape must not discard it. Only the
    // Done and close buttons (explicit actions) dismiss the result step.
    let explicitClose = false;
    // Done and the close button stay disabled until a copy succeeded, so the token cannot be lost unseen.
    let copied = false;
    const setCopied = (value) => {
      copied = value;
      $('token-done').disabled = !value;
      $('token-close').disabled = !value;
    };
    // These two buttons close through our own handler, so the flag is set before Bootstrap's hide() runs.
    for (const button of modal.querySelectorAll('#new-token-result [data-bs-dismiss="modal"]')) {
      button.removeAttribute('data-bs-dismiss');
      button.addEventListener('click', () => {
        if (!copied) return;
        explicitClose = true;
        window.bootstrap.Modal.getInstance(modal)?.hide();
      });
    }
    modal.addEventListener('hide.bs.modal', (event) => {
      if ($('new-token-result').hidden || explicitClose) return;
      event.preventDefault();
      toast('Copy the token first, then choose Done. It will not be shown again.', 'warning');
    });
    // Closing by any path wipes the plaintext secret from the DOM and restores the form state.
    modal.addEventListener('hidden.bs.modal', () => {
      explicitClose = false;
      setCopied(false);
      $('new-token-value').textContent = '';
      showResult(false);
      form.reset();
      $('token-expires').value = EXPIRY_DEFAULT;
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submitState(form, true);
      const expiry = expiryFromPreset($('token-expires').value);
      const body = { name: $('token-name').value.trim(), role: $('token-role').value };
      if (expiry) body.expires_at = expiry;
      try {
        const result = await api('tokens', { method: 'POST', body: JSON.stringify(body) });
        $('new-token-value').textContent = result.token || '';
        setCopied(false);
        showResult(true);
        $('copy-token').focus();
        await loadTokens();
        toast('Token created. Please copy it now.');
      } catch (error) { toast(messageFrom(error), 'danger'); }
      finally { submitState(form, false); }
    });
    $('copy-token').addEventListener('click', async () => {
      if (await copyToClipboard($('new-token-value').textContent)) {
        setCopied(true);
        toast('Token copied.');
      } else toast('Copying is not available here. Please select and copy the token.', 'warning');
    });
  }

  // Business labels for the Energy Manager's own enums (app/energy/models.py); the admin API never
  // sends a register name or a raw dispatch mode, except in the Diagnostics section. These are
  // presentation-only — the REST action names (charge/discharge/hold/auto) are unchanged on the wire.
  const ENERGY_ACTION_LABELS = {
    charge: 'Charge battery', discharge: 'Discharge battery', hold: 'Keep battery idle',
    auto: 'Return to automatic',
  };
  const ENERGY_STATE_LABELS = {
    automatic: 'Automatic', starting: 'Starting', charging: 'Charging', discharging: 'Discharging',
    holding: 'Holding', stopping: 'Stopping', fault: 'Fault',
  };
  const ENERGY_REASON_LABELS = {
    mode_off: 'the inverter is switched off', external: 'controlled by an external app',
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
    off: 'Switched off; the inverter is back in automatic operation.',
    manual: 'Manual mode: you can operate this inverter from this page.',
    external: 'External mode: an app can now control this inverter through the API.',
  };
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
  const ENERGY_POLL_TIMEOUT_MS = 8000;
  const ENERGY_IDLE_WATTS = 20; // below this a flow counts as standing still
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const energyPanels = new Map();
  let energyPolling = false;
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
    if (!Number.isFinite(watts)) return '–';
    const abs = Math.abs(watts);
    return abs >= 1000 ? `${(abs / 1000).toFixed(2)} kW` : `${Math.round(abs)} W`;
  }

  // Operate and Setup show power in kW; the backend keeps transmitting/storing watts. Rounding
  // boundary is pinned (design §11): < 10000 W -> two decimals (e.g. "0.90 kW"), >= 10000 W -> one.
  function formatPowerKw(watts) {
    if (!Number.isFinite(watts)) return '–';
    const abs = Math.abs(watts);
    return `${(abs / 1000).toFixed(abs < 10000 ? 2 : 1)} kW`;
  }

  function formatPercent(value) {
    return Number.isFinite(value) ? `${Math.round(value)} %` : '–';
  }

  function energyFlowGraphic() {
    const svg = svgEl('svg', { viewBox: '0 0 420 270', class: 'energy-flow-svg', role: 'img' });
    const lines = {};
    // Each path runs in the direction of the "positive" flow; a negative value reverses the animation.
    const lineDefs = [
      ['pv', 'M210 114 V154', 210, 134, 90],
      ['grid', 'M94 190 H172', 133, 190, 0],
      ['battery', 'M326 190 H248', 287, 190, 180],
    ];
    for (const [key, d, mx, my, angle] of lineDefs) {
      const group = svgEl('g', { class: 'flow-line is-idle' });
      const track = svgEl('path', { class: 'flow-track', d });
      const dots = svgEl('path', { class: 'flow-dots', d });
      const arrow = svgEl('polygon', { class: 'flow-arrow', points: '-7,-6 7,0 -7,6' });
      group.append(track, dots, arrow);
      svg.append(group);
      lines[key] = { group, dots, arrow, mx, my, angle };
    }
    const nodes = {};
    const nodeDefs = [
      ['pv', 210, 78, 'wb_sunny', 'PV', 'top'],
      ['grid', 56, 190, 'factory', 'Grid', 'bottom'],
      ['house', 210, 190, 'home', 'House', 'bottom'],
      ['battery', 364, 190, 'battery_charging_full', 'Battery', 'bottom'],
    ];
    for (const [key, cx, cy, icon, label, place] of nodeDefs) {
      const group = svgEl('g', { class: 'flow-node' });
      group.append(svgEl('circle', { class: 'flow-node-ring', cx, cy, r: 34 }));
      group.append(svgEl('text', { class: 'flow-node-icon', x: cx, y: cy, 'aria-hidden': 'true' }, icon));
      const top = place === 'top';
      group.append(svgEl('text', { class: 'flow-node-label', x: cx, y: top ? 16 : cy + 50 }, label));
      const value = svgEl('text', { class: 'flow-node-value', x: cx, y: top ? 34 : cy + 68 }, '–');
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
      const state = element('span', 'flow-badge-state');
      node.append(iconNode, element('span', 'flow-badge-caption', caption), state);
      badges.append(node);
      badgeNodes[key] = { node, state, caption };
    }

    // state is [text, ok] or null when the reading is unknown (badge hidden, no invented state).
    function setBadge(key, state, stale) {
      const badge = badgeNodes[key];
      if (badge.node.hidden !== !state) badge.node.hidden = !state;
      if (!state) return;
      setText(badge.state, state[0]);
      setClass(badge.state, 'is-ok', state[1]);
      setClass(badge.state, 'is-bad', !state[1]);
      setClass(badge.node, 'is-stale', stale);
      const label = `${badge.caption}: ${state[0]}`;
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
      line.arrow.setAttribute('transform', `translate(${line.mx} ${line.my}) rotate(${line.angle + (reverse ? 180 : 0)})`);
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
      setLine('pv', pv, 'flow-pv', 'flow-pv');
      setLine('grid', grid, 'flow-import', 'flow-export');
      setLine('battery', battery, 'flow-discharge', 'flow-charge');

      const gridWord = grid === null ? '' : Math.abs(grid) < ENERGY_IDLE_WATTS ? 'Idle' : grid > 0 ? 'Import' : 'Export';
      const batteryWord = battery === null ? '' : Math.abs(battery) < ENERGY_IDLE_WATTS ? 'Idle' : battery > 0 ? 'Discharging' : 'Charging';
      setNode('pv', pv === null ? '–' : formatPower(pv), !!r.pv_power_w.stale);
      setNode('grid', grid === null ? '–' : formatPower(grid), !!r.grid_power_w.stale);
      setNode('house', house === null ? '–' : formatPower(house), !!r.house_load_w.stale);
      setNode('battery', battery === null ? '–' : formatPower(battery), !!r.battery_power_w.stale,
        soc === null ? '' : formatPercent(soc));
      const idle = ENERGY_IDLE_WATTS;
      setBadge('generation', pv === null ? null : pv >= idle ? ['Generating', true] : ['No generation', false], !!r.pv_power_w.stale);
      setBadge('consumption', grid === null ? null : grid < idle ? ['Independent', true] : ['Grid supplied', false], !!r.grid_power_w.stale);
      setBadge('grid', grid === null ? null : grid <= -idle ? ['Feed-in', true] : grid < idle ? ['Idle', true] : ['Import', false], !!r.grid_power_w.stale);
      setBadge('battery', battery === null ? null : battery <= -idle ? ['Charging', true] : battery >= idle ? ['Discharging', false] : ['Idle', false], !!r.battery_power_w.stale);
      svg.setAttribute('aria-label', [
        `PV ${pv === null ? 'unknown' : formatPower(pv)}`,
        `grid ${grid === null ? 'unknown' : `${formatPower(grid)} ${gridWord.toLowerCase()}`}`,
        `battery ${battery === null ? 'unknown' : `${formatPower(battery)} ${batteryWord.toLowerCase()}`}`,
        `house ${house === null ? 'unknown' : formatPower(house)}`,
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
  function energyControlState(device, poll503) {
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
      return { ready: false, kind: 'disabled', text: 'Manual control is off for this inverter. Choose Manual to operate it here.', detail: '' };
    }
    if (device.mode === 'external') {
      return { ready: false, kind: 'external', text: 'This inverter is controlled by an external app through the API (PAT required).', detail: '' };
    }
    return { ready: true, kind: 'ready', text: '', detail: '' };
  }

  // Current mode line + one plain sentence (design §10). While running, a non-stale battery reading
  // adds the measured rate in kW; a stale reading omits the rate rather than show a stale number.
  function energyModeSentence(device) {
    const target = device.target_soc_percent;
    const hasTarget = target !== null && target !== undefined;
    const reading = device.readings.battery_power_w;
    const live = liveValue(reading); // null when absent or stale
    const rate = live !== null && Math.abs(live) >= ENERGY_IDLE_WATTS ? ` ${formatPowerKw(live)}` : '';
    switch (device.state) {
      case 'automatic':
        return { mode: 'Automatic', sentence: 'Automatic — the inverter decides.' };
      case 'charging':
        return {
          mode: hasTarget ? `Charging to ${Math.round(target)} %` : 'Charging',
          sentence: `Charging${hasTarget ? ` to ${Math.round(target)} %` : ''}.${rate ? ` Charging at${rate}.` : ''}`
        };
      case 'discharging':
        return {
          mode: hasTarget ? `Discharging to ${Math.round(target)} %` : 'Discharging',
          sentence: `Discharging${hasTarget ? ` to ${Math.round(target)} %` : ''}.${rate ? ` Discharging at${rate}.` : ''}`
        };
      case 'holding':
        return { mode: 'Keeping battery idle', sentence: 'Keeping battery idle.' };
      case 'starting':
        return { mode: 'Starting', sentence: 'Starting…' };
      case 'stopping':
        return { mode: 'Stopping', sentence: 'Stopping…' };
      default:
        return { mode: ENERGY_STATE_LABELS[device.state] || device.state, sentence: '' };
    }
  }

  // The Operate freshness note (design §13): nothing when healthy; a soft note only on a problem.
  function energyFreshnessNote(device, pollFailed) {
    const shown = [device.readings.battery_soc_percent, device.readings.battery_power_w];
    const stale = shown.some((reading) => reading && reading.stale && reading.value !== null && reading.value !== undefined);
    if (stale) {
      const ages = shown.map((reading) => reading && reading.age_seconds).filter(Number.isFinite);
      const n = ages.length ? Math.round(Math.max(...ages)) : 0;
      return `Measurements are ${n} seconds old.`;
    }
    if (pollFailed) return 'Live data unavailable.';
    return '';
  }

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
    head.append(title, modeGroup);
    body.append(head);

    const layout = element('div', 'energy-body');
    const control = element('div', 'energy-control');
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
    setupBox.append(setupHeading, setupList, setupStep);
    control.append(setupBox);

    const statusBox = element('div', 'energy-status');
    const statusIcon = element('span', 'material-icons');
    statusIcon.setAttribute('aria-hidden', 'true');
    const statusText = element('div');
    const statusMain = element('div', 'fw-semibold');
    const statusDetail = element('div', 'small text-secondary');
    statusText.append(statusMain, statusDetail);
    statusBox.append(statusIcon, statusText);

    // One plain-language block: current mode + a single human sentence (design §10).
    const modeBox = element('div', 'energy-mode');
    const modeLine = element('div', 'small text-secondary');
    const modeSentence = element('div', 'fw-semibold');
    const socLine = element('div', 'small text-secondary energy-soc');
    const freshness = element('p', 'small text-secondary energy-freshness mb-0');
    freshness.hidden = true;
    modeBox.append(modeLine, modeSentence, socLine, freshness);

    const actions = element('div', 'energy-actions');
    const buttons = {};
    for (const action of ['charge', 'hold', 'discharge', 'auto']) {
      const button = element('button', action === 'auto' ? 'btn btn-outline-secondary' : 'btn btn-outline-primary', ENERGY_ACTION_LABELS[action]);
      button.type = 'button';
      if (action === 'auto') button.classList.add('energy-action-auto');
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

    control.append(statusBox, modeBox, actions, target);

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
        toast('Command sent.');
        selected = null;
        touched.charge = false;
        touched.discharge = false;
        await applyActionResult(result);
      } catch (error) { toast(messageFrom(error), 'danger'); }
      finally { setBusy(false); }
    }

    async function chooseMode(wanted) {
      // aria-disabled instead of disabled keeps the keyboard focus on the group while a PUT runs.
      if (busy || lastPoll503 || wanted === (pendingMode ?? device.mode)) return;
      pendingMode = wanted;
      setBusy(true);
      try {
        const result = await api(`${path}/mode`, { method: 'PUT', body: JSON.stringify({ mode: wanted }) });
        pendingMode = null;
        toast(ENERGY_MODE_TOASTS[wanted]);
        selected = null;
        await applyActionResult(result);
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

    buttons.hold.addEventListener('click', () => send({ action: 'hold' }));
    buttons.auto.addEventListener('click', () => send({ action: 'auto' }));
    for (const action of ['charge', 'discharge']) {
      buttons[action].addEventListener('click', () => { selected = selected === action ? null : action; render(); });
    }
    range.addEventListener('input', () => { if (selected) touched[selected] = true; renderTarget(); });
    confirm.addEventListener('click', () => {
      if (!selected) return;
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
        const row = element('li', `energy-setup-item ${met ? 'is-met' : 'is-unmet'}`);
        const icon = element('span', 'material-icons', met ? 'check_circle' : 'radio_button_unchecked');
        icon.setAttribute('aria-hidden', 'true');
        row.append(icon, element('span', null, label));
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
      const readyState = energyControlState(device, lastPoll503);
      nameNode.textContent = device.device_name;
      collapseToggle.setAttribute('aria-label', `Show or hide details of ${device.device_name}`);
      dot.className = `status-dot ${device.connected ? 'online' : 'offline'}`;
      connection.textContent = `${device.connected ? 'Connected' : 'Not connected'} · ${device.host}`;
      const shownMode = pendingMode ?? device.mode ?? 'off';
      modeGroup.setAttribute('aria-label', `Operating mode of ${device.device_name}`);
      for (const value of modeValues) {
        const button = modeButtons[value];
        const checked = value === shownMode;
        button.setAttribute('aria-checked', String(checked));
        button.setAttribute('aria-disabled', String(busy || lastPoll503));
        button.tabIndex = checked ? 0 : -1;
        setClass(button, 'is-selected', checked);
      }
      modeNote.hidden = !(device.write_support_enabled === false && shownMode === 'off');

      renderSetup(energyChecklist(device));

      statusBox.className = `energy-status ${readyState.ready ? 'is-ready' : 'is-blocked'}`;
      // Nothing when healthy; the Setup block replaces the "needs setup" line.
      statusBox.hidden = readyState.ready || readyState.kind === 'setup';
      statusIcon.textContent = 'info';
      statusMain.textContent = readyState.text;
      statusDetail.textContent = readyState.detail || '';
      statusDetail.hidden = !readyState.detail;

      // Mode + one plain sentence; the whole block is hidden until manual control is ready.
      // In External the controls stay visible but disabled, so the state is readable.
      const operable = readyState.ready;
      const external = readyState.kind === 'external';
      modeBox.hidden = !(operable || external);
      if (operable || external) {
        const mode = energyModeSentence(device);
        modeLine.textContent = `Current mode: ${mode.mode}`;
        modeSentence.textContent = mode.sentence;
        const soc = readingValue(device.readings.battery_soc_percent);
        socLine.textContent = soc === null ? '' : `Battery ${formatPercent(soc)}`;
        socLine.hidden = soc === null;
        const note = energyFreshnessNote(device, lastPollFailed);
        freshness.textContent = note;
        freshness.hidden = !note;
      }

      // Actions + target are usable only once ready.
      actions.hidden = !(operable || external || readyState.kind === 'disabled');
      for (const action of ['charge', 'hold', 'discharge', 'auto']) {
        const item = energyAvailability(device, action);
        const enabled = operable && item.available && !busy && device.connected;
        buttons[action].disabled = !enabled;
        const reason = external ? 'external' : item.reason;
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
      // False while an action runs or when the poll was requested before the last action finished.
      acceptsPoll(requestedAt) { return !busy && requestedAt > actionFinishedAt; },
      // Display only: no request, no stored value.
      setExpert(on) { expertOn = Boolean(on); render(); },
    };
    render();
    return panel;
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
    const guarded = (button, task, formKey) => async (event) => {
      event.preventDefault();
      button.disabled = true;
      saving += 1;
      try {
        await task();
        if (formKey) dirty[formKey] = false;
      } catch (error) {
        toast(messageFrom(error), 'danger');
        return;
      } finally { saving -= 1; button.disabled = false; }
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
    const maxCharge = numberInput(0.001, 100, 0.001);
    const maxDischarge = numberInput(0.001, 100, 0.001);
    const limitButton = element('button', 'btn btn-sm btn-primary', 'Save and continue');
    limitButton.type = 'submit';
    const limitButtonCol = element('div', 'col-auto');
    limitButtonCol.append(limitButton);
    limitForm.append(field('Maximum charging power (kW)', maxCharge), field('Maximum discharging power (kW)', maxDischarge), limitButtonCol);
    const putLimits = (chargeW, dischargeW, engineeringMode) => api(`dispatch/devices/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify({ max_charge_power_w: chargeW, max_discharge_power_w: dischargeW, engineering_mode: engineeringMode }),
    });
    limitForm.addEventListener('submit', guarded(limitButton, async () => {
      if ([maxCharge, maxDischarge].some((input) => input.value === '' || !input.checkValidity())) {
        throw new Error('Enter both maximum powers in kW.');
      }
      const current = getDevice().limits;
      await putLimits(Math.round(Number(maxCharge.value) * 1000), Math.round(Number(maxDischarge.value) * 1000),
        Boolean(current && current.engineering_mode));
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
      const policy = getDevice().soc_target_policy;
      await api(`${path}/soc-target-policy`, {
        method: 'PUT',
        body: JSON.stringify({ mode: policyMode.value, below_margin_percent: Number(policyMargin.value), note: policy.note || null }),
      });
      toast('SoC target policy saved.');
    }, 'policy'));

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
      const link = element('a', 'btn btn-sm btn-primary energy-step-link', text);
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
    // Opens the Expert form on an explicit click only; nothing is switched on by the setup step itself.
    const openVerifyButton = element('button', 'btn btn-sm btn-primary energy-step-link', 'Verify hardware');
    openVerifyButton.type = 'button';
    hardwareText.after(openVerifyButton);
    openVerifyButton.addEventListener('click', () => {
      const expertSwitch = $('energy-expert-mode');
      if (expertSwitch && !expertSwitch.checked) {
        expertSwitch.checked = true;
        expertSwitch.dispatchEvent(new Event('change')); // the page handler renders every panel
      }
      model.scrollIntoView({ block: 'center' });
      model.focus();
    });
    const renderHardwareText = (expertOn) => {
      hardwareText.textContent = 'Hardware control must be verified on this inverter before it can be controlled. '
        + (expertOn ? 'Enter the values you measured under Expert settings below.'
          : 'This needs the values you measured on the hardware and opens the Expert settings. Do not guess them.');
      openVerifyButton.hidden = expertOn;
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
      const mark = () => { dirty[key] = true; };
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
      if (!dirty.engineering) engineering.input.checked = Boolean(device.limits && device.limits.engineering_mode);
      if (!dirty.policy) {
        policyMode.value = device.soc_target_policy.mode;
        policyMargin.value = device.soc_target_policy.below_margin_percent;
      }
    }

    function update(device, expertOn, writeSupportOff = false) {
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
          [name], [Number.isFinite(reading && reading.age_seconds) ? Math.round(reading.age_seconds) : '–'],
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
      const link = element('a', 'btn btn-sm btn-primary energy-step-link', action.label);
      link.href = action.href;
      energyNote.replaceChildren(label, link);
    } else energyNote.replaceChildren(label);
    if (energyNote.parentNode !== host) host.append(energyNote);
  }

  async function pollEnergy() {
    if (energyPolling) return;
    energyPolling = true;
    const host = $('energy-list');
    const requestedAt = ++energyClock;
    // A hung fetch must not stop the polling for good; the abort lands in the failure branch.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(Object.assign(new Error('The energy request timed out.'), { name: 'TimeoutError' })), ENERGY_POLL_TIMEOUT_MS);
    try {
      const devices = await api('energy/devices', { signal: controller.signal });
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
        } else if (panel.acceptsPoll(requestedAt)) {
          panel.update(device, { poll503: false, pollFailed: false });
        }
      }
      if ([...energyPanels.keys()].join('\n') !== ids.join('\n')) {
        const ordered = new Map(ids.map((id) => [id, energyPanels.get(id)]));
        energyPanels.clear();
        for (const [id, panel] of ordered) { energyPanels.set(id, panel); host.append(panel.root); }
      }
      setEnergyNote(host, devices.length ? null : 'No inverters configured yet.');
      // The absolute timestamp lives in Diagnostics now (design §13), not a top-of-page line.
      const stamp = `Updated ${new Date().toLocaleTimeString('en-GB')}`;
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
      for (const panel of energyPanels.values()) panel.setPollState({ poll503, pollFailed: !poll503 });
      if (poll503 && !energyPanels.size) setEnergyNote(host, 'Manual battery control requires write support to be enabled.', { kind: 'warning', action: { label: 'Open Inverters page', href: '/ui/inverters' } });
    } finally { clearTimeout(timer); energyPolling = false; }
  }

  // Reads the cache only (server side), so the poll rate does not load the inverter.
  function initEnergy() {
    const expertSwitch = $('energy-expert-mode');
    if (expertSwitch) {
      expertSwitch.checked = energyExpertMode;
      expertSwitch.addEventListener('change', () => {
        energyExpertMode = expertSwitch.checked;
        $('energy-expert-state').textContent = energyExpertMode ? 'On' : 'Off';
        for (const panel of energyPanels.values()) panel.setExpert(energyExpertMode);
      });
    }
    pollEnergy();
    setInterval(() => { if (!document.hidden) pollEnergy(); }, ENERGY_POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollEnergy(); });
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

  // Field label for a settings key (the raw key is an implementation detail); unknown keys pass through.
  function settingLabel(key) {
    return [...settingFields, ...exportFields].find((field) => field.key === key)?.label || key;
  }

  // Changes that can lock the operator out of this UI or widen trust: confirmed before they autosave.
  // Returns the confirmation text, or null when the field is harmless or the value did not change.
  function riskyChange(key, next) {
    if (JSON.stringify(next) === JSON.stringify(settingsDraft[key])) return null;
    const target = () => `${key === 'bind_address' ? next : settingsDraft.bind_address}:${key === 'bind_port' ? next : settingsDraft.bind_port}`;
    if (key === 'bind_address' || key === 'bind_port') {
      return {
        title: 'Change listen address?',
        message: `After the next restart the service listens on ${target()}. If that address is not reachable from your network, you can no longer open this admin interface.`,
        confirmLabel: 'Save listen address',
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
  const pendingKeys = new Set();          // scalar keys
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
      if (entry.state === 'incomplete' && entry.message) {
        const hint = sectionNotice(id, 'hint');
        if (hint) hint.textContent = entry.message;
      } else removeNotice(id, 'hint');
    }
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

  // Derived on demand instead of a cached counter four writers had to keep correct. `failed` is
  // deliberately absent: `general` rolled its value back, and `export` failures keep
  // their keys queued, so genuine leftover work is already covered by hasPending(null).
  function outstandingWork() {
    return hasPending(null) || deviceDirty() || parameterDirty || parameterSending || anySection(['saving', 'incomplete']);
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
      showRestartNotice(result.restart_required);
      // Switching write access on makes the server approve the registers manual control needs.
      if (keys.includes('enable_write_support') && payload.enable_write_support) refreshWriteApprovals();
      if (page === 'prometheus') markPrometheusSaved();
      for (const id of sentSections) setSectionState(id, 'saved');
      const restartKeys = keys.filter((key) => result.restart_required?.includes(key));
      if (restartKeys.length) toast(`Saved, but not active yet. The service must be restarted to apply: ${restartKeys.map(settingLabel).join(', ')}.`, 'warning');
      else toast('Settings saved and active.');
      return true;
    } catch (error) {
      // A full renderSettings() would tear down and rebuild every section's DOM, including one the
      // operator was mid-edit on but that was never part of this save — losing focus and any input
      // in that unrelated section. Only the controls for the keys that actually failed are refreshed.
      for (const key of keys) {
        if (pendingKeys.has(key)) continue;  // re-queued during the request: that value wins
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
      if (field.type === 'secret') { control.autocomplete = 'new-password'; control.placeholder = settingsDraft[`${field.key}_configured`] ? SECRET_PLACEHOLDER : ''; }
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
        settingsDraft[field.key] = control.checked;
      } else settingsDraft[field.key] = control.value;
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

  // Mirrors the server-side host plausibility check `_HOST` in app/admin/api.py; keep the two in
  // step. This is the client-side pre-check only — the server validates again on save (JS-02).
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

  // A fresh, still empty row is ignored; every other row must be valid before anything is saved.
  function isBlankNewRow(device) { return !device.device_id && !String(device.host || '').trim(); }

  // Client-only stable identity. Handlers and the row DOM reference a device object, not an array
  // index: an index does not survive a splice or a re-render, and replacing the array on a failed
  // save would orphan every object the live handlers still hold.
  let deviceUid = 0;
  function withUid(device) { if (!device._uid) device._uid = `d${++deviceUid}`; return device; }
  // The draft becomes the server state; the baseline is what dirty-marking and Discard compare to.
  function adoptDevices(list) {
    settingsDraft.devices = (list || []).map(withUid);
    deviceBaseline = settingsDraft.devices.map((device) => ({ ...deviceSnapshot(device), display_name: device.display_name || null }));
    return settingsDraft.devices;
  }
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

  // The device list is applied explicitly: edits only change the draft, and one PUT (with the live
  // reconfiguration it triggers) is sent on Apply. Unlike the other settings it never autosaves.
  let deviceBaseline = [];                  // server state: [{ uid, host, port, network_id, device_id, display_name }]
  const deviceUi = { applying: false, error: '', notice: '' };

  function deviceSnapshot(device) {
    return {
      uid: device._uid,
      host: String(device.host || '').trim(),
      port: Number(device.port),
      network_id: networkId(device.network_id),
      device_id: device.device_id || null,
    };
  }

  // Dirty state is derived from draft vs baseline, so there is no flag a stale handler could leave behind.
  function deviceChanges() {
    const base = new Map(deviceBaseline.map((entry) => [entry.uid, entry]));
    const rows = new Map();   // uid -> 'New' | 'Changed'
    const risky = [];         // baseline entries whose live state is reset by an apply
    const seen = new Set();
    for (const device of settingsDraft.devices || []) {
      if (isBlankNewRow(device)) continue;
      const before = base.get(device._uid);
      if (!before) { rows.set(device._uid, 'New'); continue; }
      seen.add(device._uid);
      const now = deviceSnapshot(device);
      if (now.host !== before.host || now.port !== before.port || now.network_id !== before.network_id) rows.set(device._uid, 'Changed');
      if (now.host.toLowerCase() !== before.host.toLowerCase() || now.port !== before.port || now.network_id !== before.network_id) risky.push(before);
    }
    const removed = deviceBaseline.filter((entry) => !seen.has(entry.uid));
    risky.push(...removed);
    return { rows, removed, risky, count: rows.size + removed.length };
  }

  function deviceDirty() { return deviceChanges().count > 0; }

  const deviceLabel = (entry) => `${entry.host}:${entry.port}`;
  const RESET_NOTE = 'Verification evidence, Engineering Mode and operating mode of these inverters are reset';

  // The only owner of the row markers, the Apply bar and the controls' locked state. Returns true if
  // every non-blank row is valid; an invalid draft can never be applied.
  function refreshDeviceState(card) {
    let invalid = false;
    const changes = deviceChanges();
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
      const flag = changes.rows.get(device._uid) || '';
      row.classList.toggle('is-dirty', Boolean(flag));
      const flagNode = row.querySelector('.device-row-flag');
      flagNode.textContent = flag ? `${flag} — unsaved` : '';
      flagNode.hidden = !flag;
      if (message) invalid = true;
    }
    for (const control of card.querySelectorAll('.device-settings input, .device-settings button, #device-add-row')) control.disabled = deviceUi.applying;
    const apply = card.querySelector('#device-apply');
    if (apply) {
      apply.disabled = deviceUi.applying || invalid || changes.count === 0;
      apply.setAttribute('aria-busy', String(deviceUi.applying));
      card.querySelector('#device-discard').disabled = deviceUi.applying || (changes.count === 0 && !deviceUi.error);
      const count = card.querySelector('#device-change-count');
      count.hidden = changes.count === 0;
      count.textContent = `${changes.count} unsaved ${changes.count === 1 ? 'change' : 'changes'}`;
      const warning = card.querySelector('#device-reset-warning');
      warning.hidden = changes.risky.length === 0;
      warning.textContent = changes.risky.length ? `${RESET_NOTE} when you apply: ${changes.risky.map(deviceLabel).join(', ')}.` : '';
      let status = deviceUi.notice;
      if (deviceUi.applying) status = 'Applying — the inverter connections are being rebuilt …';
      else if (invalid) status = 'Fix the marked inverter rows — nothing is applied until they are valid.';
      else if (changes.count) status = 'Unsaved changes — nothing has been sent yet.';
      card.querySelector('#device-apply-status').textContent = status;
      const error = card.querySelector('#device-apply-error');
      error.hidden = !deviceUi.error;
      error.textContent = deviceUi.error;
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
    if (deviceUi.applying) return;
    adoptDevices(deviceBaseline.map(({ uid, ...rest }) => rest));
    deviceUi.error = '';
    deviceUi.notice = '';
    rebuildDeviceSection();
    toast('Inverter changes discarded.', 'info');
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
      // Ids derive from the stable row uid, not the loop index: preserveFocus() restores focus by
      // id after rebuildDeviceSection(), so an index-derived id would move focus to a different row
      // once a row above is removed (JS-01).
      hostInput.id = `device-${device._uid}-host`;
      hostInput.dataset.field = 'host';
      hostInput.setAttribute('aria-label', 'IP address or host name');
      hostInput.placeholder = 'IP address or host name';
      hostInput.autocomplete = 'off';
      hostInput.value = String(device.host ?? '');
      hostCol.append(hostInput);
      const portCol = element('div', 'col-6 col-sm-3');
      const portInput = element('input', 'form-control');
      portInput.id = `device-${device._uid}-port`;
      portInput.dataset.field = 'port';
      portInput.type = 'number';
      portInput.min = '1'; portInput.max = '65535'; portInput.inputMode = 'numeric';
      portInput.setAttribute('aria-label', 'Port');
      portInput.value = String(device.port ?? '');
      portCol.append(portInput);
      const networkCol = element('div', 'col-6 col-sm-3');
      // Visually hidden keeps the row aligned with the unlabelled host and port inputs.
      const networkLabel = element('label', 'visually-hidden', 'Network ID (optional, empty means directly attached)');
      networkLabel.htmlFor = `device-${device._uid}-network-id`;
      const networkInput = element('input', 'form-control');
      networkInput.id = `device-${device._uid}-network-id`;
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
      const flag = element('span', 'device-row-flag badge text-bg-warning mt-1');
      flag.hidden = true;
      const feedback = element('div', 'invalid-feedback');
      feedback.id = `device-${device._uid}-feedback`;
      row.append(grid, flag, feedback);
      host.append(row);
      // Draft only: no request is sent until Apply, so a spinner step cannot reconfigure anything.
      const onInput = () => {
        device.host = hostInput.value;
        device.port = portInput.value === '' ? null : Number(portInput.value);
        device.network_id = networkId(networkInput.value);
        deviceUi.notice = '';
        refreshDeviceState(card);
      };
      hostInput.addEventListener('input', onInput);
      portInput.addEventListener('input', onInput);
      networkInput.addEventListener('input', onInput);
      remove.addEventListener('click', () => {
        const restoreFocus = document.activeElement === remove;
        // Removing only edits the draft; the reset warning is shown before the removal is applied.
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
        deviceUi.notice = '';
        if (editor?.parentElement) refreshDeviceState(editor.parentElement);
      });
    }
    const networkHelp = element('p', 'text-secondary small mt-2 mb-0', 'Network ID: leave empty for a direct connection. Only set it for an inverter reached through the master in the plant network.');
    networkHelp.id = 'device-network-help';
    host.append(networkHelp);
    if (devices.every(isBlankNewRow)) host.append(element('p', 'text-secondary small', 'No inverters configured yet.'));
    const add = element('button', 'btn btn-outline-primary mt-3', 'Add another inverter');
    add.type = 'button';
    add.id = 'device-add-row';
    add.addEventListener('click', () => addDeviceRow(card));
    card.append(host, add, buildDeviceApplyBar(card));
    refreshDeviceState(card);
  }

  function buildDeviceApplyBar(card) {
    const bar = element('div', 'device-apply-bar mt-4');
    const actions = element('div', 'd-flex flex-wrap align-items-center gap-2');
    const apply = element('button', 'btn btn-primary', 'Apply changes');
    apply.type = 'button';
    apply.id = 'device-apply';
    apply.setAttribute('aria-describedby', 'device-apply-status');
    apply.addEventListener('click', () => { applyDevices(card).catch((error) => toast(messageFrom(error), 'danger')); });
    const discard = element('button', 'btn btn-outline-secondary', 'Discard');
    discard.type = 'button';
    discard.id = 'device-discard';
    discard.addEventListener('click', discardDeviceChanges);
    const count = element('span', 'badge text-bg-warning');
    count.id = 'device-change-count';
    actions.append(apply, discard, count);
    const warning = element('div', 'alert alert-warning small mt-3 mb-0');
    warning.id = 'device-reset-warning';
    const status = element('p', 'text-secondary small mt-2 mb-0');
    status.id = 'device-apply-status';
    status.setAttribute('role', 'status'); // polite live region: progress and result are announced
    const error = element('div', 'alert alert-danger mt-3 mb-0');
    error.id = 'device-apply-error';
    error.tabIndex = -1; // focus target after a failed apply; the toast carries the same text
    bar.append(actions, warning, status, error);
    return bar;
  }

  // Adds a draft row only; a still-empty row is reused instead of stacking blanks.
  function addDeviceRow(card) {
    const devices = settingsDraft.devices || (settingsDraft.devices = []);
    if (!devices.some(isBlankNewRow)) devices.push(withUid({ host: '', port: 8899, network_id: null }));
    rebuildDeviceSection();
    const blank = devices.find(isBlankNewRow);
    card.querySelector(`.device-settings-item[data-uid="${blank._uid}"] input[data-field="host"]`)?.focus();
  }

  function applyFailureMessage(error) {
    const reason = messageFrom(error);
    if (error?.status === 409) return `The inverter list was rejected and rolled back (${reason}). The previous configuration is still active; your changes are kept.`;
    if (error?.status === 504) return `Applying the inverter list timed out (${reason}). Check the dashboard before retrying; your changes are kept.`;
    return `The inverters were not applied (${reason}). Your changes are kept; apply again or discard them.`;
  }

  // The one place that sends the device list. Re-addressing or removing an inverter resets its
  // verification evidence, Engineering Mode and operating mode, so it needs an explicit confirmation.
  async function applyDevices(card) {
    if (deviceUi.applying) return;
    if (!refreshDeviceState(card)) {
      card.querySelector('.device-settings-item input.is-invalid')?.focus();
      return;
    }
    const changes = deviceChanges();
    if (!changes.count) return;
    if (changes.risky.length && !await confirmAction({
      title: 'Apply inverter change?',
      message: `${RESET_NOTE}: ${changes.risky.map(deviceLabel).join(', ')}.`,
      confirmLabel: 'Apply change',
      danger: true,
    })) return;
    deviceUi.applying = true;
    deviceUi.error = '';
    deviceUi.notice = '';
    refreshDeviceState(card);
    let failed = false;
    try {
      const payload = { devices: devicesPayload() };
      const result = await api('settings', { method: 'PUT', body: JSON.stringify(payload) });
      settingsCommitted = result.settings || { ...settingsCommitted, ...payload };
      // After a successful apply the draft is the server state, including newly assigned ids.
      adoptDevices(structuredClone(settingsCommitted.devices || payload.devices));
      deviceUi.notice = `Applied ${hhmm(new Date())}.`;
      showRestartNotice(result.restart_required);
      if (page === 'dashboard') { loadDashboard().catch((error) => toast(messageFrom(error), 'danger')); loadMetricCount(); }
      toast('Inverters applied.');
    } catch (error) {
      failed = true;
      deviceUi.error = applyFailureMessage(error);
      toast(deviceUi.error, 'danger');
    } finally {
      deviceUi.applying = false;
    }
    const host = document.getElementById('device-editor');
    if (!host?.parentElement) return;
    if (failed) refreshDeviceState(host.parentElement);
    else rebuildDeviceSection();
    if (failed) document.getElementById('device-apply-error')?.focus();
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
    notice.textContent = keys?.length ? `Saved, but not active yet. The service must be restarted to apply: ${keys.map(settingLabel).join(', ')}.` : '';
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

  // Dashboard only: defer the sensitive settings read until the operator first opens the inverters
  // modal, so a plain dashboard view never fetches it (SEC-06). Loads once; a failure is reported
  // and leaves the "Loading settings …" placeholder so a later open can retry.
  function initInvertersModalLazy() {
    const modal = $('inverters-modal');
    if (!modal) return;
    let loaded = false;
    modal.addEventListener('show.bs.modal', () => {
      if (loaded) return;
      loaded = true;
      initSettings().catch((error) => { loaded = false; toast(messageFrom(error), 'danger'); });
    });
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

  // Reloads the Writable parameters list and the Energy checklist after the server changed them.
  async function refreshWriteApprovals() {
    try {
      if ($('write-search') && !parameterDirty && !parameterSending) { parameterData = await api('parameters'); renderParameters(); }
      if ($('energy-list')) pollEnergy();
    } catch (error) { toast(messageFrom(error), 'danger'); }
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
      toast(result.restart_required?.length ? 'Parameters saved, but not active yet. The service must be restarted to apply them.' : 'Parameters saved.', result.restart_required?.length ? 'warning' : 'success');
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
    $('add-metrics-modal')?.addEventListener('shown.bs.modal', () => $('parameter-search').focus());
    $('add-metrics-modal')?.addEventListener('hidden.bs.modal', () => { addMetricSelection.clear(); $('parameter-search').value = ''; renderParameters(); });
    $('exposed-list')?.addEventListener('dragover', (event) => event.preventDefault());
    $('exposed-list')?.addEventListener('drop', (event) => {
      if (event.target.closest('.metric-row')) return;
      event.preventDefault();
      reorderParameter(event.dataTransfer.getData('text/plain'), '');
    });
  }

  // dashboard.js is a separate deferred script; wait for its ready event instead of assuming it ran.
  function startDashboardLayout() {
    if (window.RCTDashboard) return window.RCTDashboard.initLayout();
    return new Promise((resolve, reject) => {
      document.addEventListener('rct:dashboard-ready', () => {
        window.RCTDashboard.initLayout().then(resolve, reject);
      }, { once: true });
    });
  }

  // Lets dashboard.js hold the grid back until the first tile contents exist, so nothing resizes
  // after the first paint. Set as a flag too: the layout may finish before or after this runs.
  function markDashboardDataReady() {
    document.body.dataset.dashboardData = 'ready';
    document.dispatchEvent(new CustomEvent('rct:dashboard-data-ready'));
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
      if (page === 'dashboard') {
        initDashboardPolling();
        // dashboard.js owns the GridStack layout; it is independent of the polling above and of
        // data loading, so it is started without awaiting it.
        startDashboardLayout().catch((error) => toast(messageFrom(error), 'danger'));
        // The settings read (GET /admin/api/settings) is sensitive and only feeds the inverters
        // modal, which may never open. Load it lazily on first open instead of on every dashboard
        // load (SEC-06).
        initInvertersModalLazy();
        await Promise.all([loadDashboard().finally(markDashboardDataReady), loadMetricCount()]);
      }
      else if (page === 'tokens') initTokens();
      else if (page === 'energy') initEnergy();
      else if (['settings', 'inverters', 'prometheus', 'tsdb'].includes(page)) {
        await initSettings();
        if (page === 'inverters' || page === 'prometheus') await initParameters();
      }
    } catch (error) { toast(messageFrom(error), 'danger'); }
  }

  // dashboard.js loads after this file and needs the shared request/toast helpers; admin.js stays
  // the single owner of the CSRF token and the fetch wrapper (abort/401/toast handling included).
  window.RCTAdmin = Object.freeze({ api, toast, messageFrom, element, confirmAction });

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
