//
// app/admin/static/js/admin-dashboard-data.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

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
  // Last known state per device and for the TSDB tile. A poll only refreshes what it actually
  // delivered; everything else keeps its last value until it is older than VALUE_MAX_AGE_MS (the
  // next expected poll plus a tolerance) and then shows n/a. Structure (towers, flow graphic)
  // outlives values, so a failed, empty or "connecting" answer never rebuilds the layout.
  const SNAPSHOT_KEY = 'rct.dashboard.snapshot';
  const SNAPSHOT_VERSION = 2;
  const SNAPSHOT_MAX_AGE_MS = 60 * 60 * 1000;
  let dashboardSnapshotInvalidated = false;
  const VALUE_MAX_AGE_MS = DASHBOARD_INTERVAL_MS + DASHBOARD_TIMEOUT_MS + 2000;
  const FLOW_FIELDS = ['pv_power_w', 'grid_power_w', 'house_load_w', 'battery_power_w', 'battery_soc_percent'];
  let dashboardView = new Map(); // key -> { device, metrics: Map(name -> { metric, at }), flow: Map(field -> { reading, at }), batteries }
  let dashboardTsdb = { raw: null, good: null, goodAt: 0 };
  let dashboardSnapshot = null; // { at } of the last successful poll (or of the restored snapshot)
  let dashboardStaleTimer = null;
  let dashboardBanner = null;

  function deviceKeys(devices) {
    const seen = new Set();
    return devices.map((device, index) => {
      let key = String(device.id ?? `index-${index}`);
      while (seen.has(key)) key += '+';
      seen.add(key);
      return key;
    });
  }

  // Whether a card cell shows this reading as a value (not n/a); only such a cell can run out.
  function metricShown(metric) {
    const value = metric.value !== null && metric.value !== undefined ? Number(metric.value) : NaN;
    return Number.isFinite(value) && (metric.name !== 'power_mng_bat_next_calib_date' || value > 0);
  }

  // A TSDB status is "settled" when it is not configured or exports healthily; a restart reports an
  // unsettled one for a moment, which must not replace the last settled status before it expires.
  const tsdbSettled = (tsdb) => Boolean(tsdb) && (!tsdb.configured || (tsdb.export_enabled && tsdb.healthy));

  function absorbDashboard(devices, tsdb, now) {
    const keys = deviceKeys(devices);
    const next = new Map();
    devices.forEach((incoming, index) => {
      const prev = dashboardView.get(keys[index]);
      const entry = prev || { metrics: new Map(), flow: new Map(), batteries: [] };
      for (const metric of Array.isArray(incoming.metrics) ? incoming.metrics : []) entry.metrics.set(metric.name, { metric, at: now });
      for (const field of FLOW_FIELDS) {
        const reading = incoming.energy_flow?.[field];
        if (reading && reading.value !== null && reading.value !== undefined) entry.flow.set(field, { reading, at: now });
      }
      // Towers merge by id: one that is missing from an answer (module scan after a restart) stays and
      // runs out by its values; a tower still detecting its modules keeps its last module count.
      const reported = new Map((Array.isArray(incoming.batteries) ? incoming.batteries : []).map((tower) => [tower.id, tower]));
      const merged = [];
      for (const known of entry.batteries) {
        const tower = reported.get(known.id);
        reported.delete(known.id);
        if (!tower) { if (now - known.seen_at <= SNAPSHOT_MAX_AGE_MS) merged.push(known); continue; }
        merged.push(tower.module_count_status !== 'ok' && known.module_count_status === 'ok'
          ? { ...tower, module_count: known.module_count, module_count_status: 'ok', populated_module_slots: known.populated_module_slots, seen_at: now }
          : { ...tower, seen_at: now });
      }
      for (const tower of reported.values()) merged.push({ ...tower, seen_at: now });
      entry.batteries = merged;
      const { metrics, batteries, energy_flow: flow, ...info } = incoming; // the parts above are kept per value
      entry.device = { ...info, last_success_at: info.last_success_at || prev?.device.last_success_at || null };
      next.set(keys[index], entry);
    });
    dashboardView = next;
    dashboardTsdb.raw = tsdb;
    if (tsdbSettled(tsdb)) { dashboardTsdb.good = tsdb; dashboardTsdb.goodAt = now; }
  }

  // The devices as shown at `now`: expired values become null (n/a), expired structure is dropped.
  function viewDevices(now) {
    return [...dashboardView.values()].map((entry) => {
      const flow = {};
      for (const field of FLOW_FIELDS) {
        const known = entry.flow.get(field);
        flow[field] = known && now - known.at <= VALUE_MAX_AGE_MS
          ? known.reading : { value: null, stale: true, age_seconds: null, expired: Boolean(known) };
      }
      return {
        ...entry.device,
        metrics: [...entry.metrics.values()].map(({ metric, at }) => (now - at <= VALUE_MAX_AGE_MS ? metric : { ...metric, value: null, label: undefined, expired: metricShown(metric) })),
        batteries: entry.batteries.filter((tower) => now - tower.seen_at <= SNAPSHOT_MAX_AGE_MS),
        energy_flow: flow,
        flow_known: [...entry.flow.values()].some((known) => now - known.at <= SNAPSHOT_MAX_AGE_MS),
      };
    });
  }

  function viewTsdb(now) {
    const { raw, good, goodAt } = dashboardTsdb;
    return !tsdbSettled(raw) && good && now - goodAt <= VALUE_MAX_AGE_MS ? good : raw;
  }

  // Re-renders from the last known state and arms a timer for the next value that runs out.
  function refreshDashboard() {
    clearTimeout(dashboardStaleTimer);
    dashboardStaleTimer = null;
    const now = Date.now();
    renderDashboard(viewDevices(now), viewTsdb(now));
    const ages = [dashboardTsdb.goodAt];
    for (const entry of dashboardView.values()) {
      for (const { at } of entry.metrics.values()) ages.push(at);
      for (const { at } of entry.flow.values()) ages.push(at);
    }
    const live = ages.filter((at) => at && now - at < VALUE_MAX_AGE_MS);
    if (live.length) dashboardStaleTimer = setTimeout(refreshDashboard, Math.min(...live) + VALUE_MAX_AGE_MS - now + 50);
  }

  function saveSnapshot() {
    if (dashboardSnapshotInvalidated) return;
    const snapshot = {
      v: SNAPSHOT_VERSION, at: dashboardSnapshot.at, tsdb: dashboardTsdb,
      devices: [...dashboardView].map(([key, entry]) => ({
        key, device: entry.device, metrics: [...entry.metrics.values()], flow: [...entry.flow], batteries: entry.batteries,
      })),
    };
    try { localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(snapshot)); } catch { /* storage full or blocked */ }
  }

  // Clear user-scoped telemetry at every auth boundary; keep its storage key owned here.
  function clearDashboardSnapshot() {
    dashboardSnapshotInvalidated = true;
    try { localStorage.removeItem(SNAPSHOT_KEY); } catch { /* storage blocked: nothing to clear */ }
  }

  // After a reload (the connection-lost modal reloads once the server is back) the page starts from
  // the stored state instead of from an empty one; values still expire by their own age.
  function restoreSnapshot() {
    try {
      const stored = JSON.parse(localStorage.getItem(SNAPSHOT_KEY) || 'null');
      if (!stored || stored.v !== SNAPSHOT_VERSION || !Array.isArray(stored.devices) || !Number.isFinite(stored.at) || Date.now() - stored.at >= SNAPSHOT_MAX_AGE_MS) return false;
      const view = new Map();
      for (const item of stored.devices) {
        view.set(item.key, {
          device: item.device, metrics: new Map(item.metrics.map((known) => [known.metric.name, known])),
          flow: new Map(item.flow), batteries: item.batteries,
        });
      }
      dashboardTsdb = { raw: stored.tsdb.raw, good: stored.tsdb.good, goodAt: stored.tsdb.goodAt };
      dashboardView = view;
      dashboardSnapshot = { at: stored.at };
      return true;
    } catch { return false; /* corrupt or foreign entry: start empty */ }
  }

  // The note takes the place of the page subtitle, so showing it never moves the grid.
  function setDashboardStale(error) {
    const grid = $('dashboard-grid') || $('devices-list');
    const subtitle = document.querySelector('.page-heading p.text-secondary');
    if (!dashboardBanner) {
      dashboardBanner = element('p', 'dashboard-offline-banner fw-medium text-warning-emphasis mb-0');
      dashboardBanner.setAttribute('role', 'status');
      if (subtitle) subtitle.after(dashboardBanner); else grid.before(dashboardBanner);
    }
    const reason = error?.status >= 500 ? `Server error (${error.status})`
      : error?.status === 429 ? 'Too many requests, retrying' : 'Connection to server lost';
    const text = `${reason} – data from ${hhmm(new Date(dashboardSnapshot.at))}`;
    if (dashboardBanner.textContent !== text) dashboardBanner.textContent = text;
    dashboardBanner.hidden = false;
    if (subtitle) subtitle.hidden = true;
    grid.classList.add('is-offline');
  }

  function clearDashboardStale() {
    if (dashboardBanner) dashboardBanner.hidden = true;
    const subtitle = document.querySelector('.page-heading p.text-secondary');
    if (subtitle) subtitle.hidden = false;
    ($('dashboard-grid') || $('devices-list')).classList.remove('is-offline');
  }

  function dashboardDelay() {
    const base = Math.min(DASHBOARD_INTERVAL_MS * 2 ** dashboardFailures, DASHBOARD_MAX_INTERVAL_MS);
    return pollDelay(Math.round(base * (0.9 + Math.random() * 0.2))); // jitter keeps several tabs from polling in lockstep
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
    icon.classList.remove('text-success', 'text-danger', 'text-secondary');
    if (!tsdb || !tsdb.configured) {
      icon.textContent = 'cloud_off';
      icon.classList.add('text-secondary');
      label.textContent = NOT_AVAILABLE;
      label.title = 'Not configured'; // the reason stays secondary; the value reads n/a
      return;
    }
    label.title = '';
    if (!tsdb.export_enabled) {
      icon.textContent = 'pause_circle';
      icon.classList.add('text-secondary');
      label.textContent = NOT_AVAILABLE;
      label.title = 'Export disabled';
    } else if (tsdb.healthy) {
      icon.textContent = 'cloud_done';
      icon.classList.add('text-success');
      label.textContent = 'Sending';
    } else {
      icon.textContent = 'sync_problem';
      icon.classList.add('text-danger');
      label.textContent = 'Failing';
    }
    // The last-sent time is a tooltip, not a third line in the tile.
    if (tsdb.export_enabled && tsdb.last_success_at) label.title = `Last sent ${hhmm(new Date(tsdb.last_success_at))}`;
  }

  function renderDashboard(devices, tsdb) {
    const list = $('devices-list');
    showHeroMetrics(devices);
    renderTsdbTile(tsdb);
    $('device-count').textContent = String(devices.length);
    $('inverter-onboarding-hint').hidden = devices.length !== 0;
    $('connected-count').textContent = String(devices.filter((item) => displayStatus(item.status)[1] === 'online').length);
    const keys = deviceKeys(devices);
    const seen = new Set(keys);
    const nodes = devices.map((device, index) => {
      const key = keys[index];
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
      const data = await api('devices', { signal: controller.signal, background: automatic });
      if (generation !== dashboardGeneration) return 'skipped';
      dashboardPollFailing = false;
      dashboardFailures = 0;
      const now = Date.now();
      absorbDashboard(Array.isArray(data.devices) ? data.devices : [], data.tsdb || null, now);
      dashboardSnapshot = { at: now };
      refreshDashboard();
      saveSnapshot();
      clearDashboardStale();
      return 'ok';
    } catch (error) {
      if (generation !== dashboardGeneration) return 'skipped';
      dashboardFailures += 1;
      // Server unreachable or failing: the last known state stays on screen and expires value by value.
      if (dashboardSnapshot) setDashboardStale(error);
      else syncChildren($('devices-list'), [dashboardNotices.error]);
      // Automatic background polls toast only on the first failure after a success streak, so a
      // sustained outage does not spam one alert per poll; a non-automatic load always toasts.
      if (!automatic || (!dashboardPollFailing && !error?.rateLimited)) toast(messageFrom(error), 'danger');
      dashboardPollFailing = true;
      return 'failed';
    } finally {
      clearTimeout(timer);
      if (generation === dashboardGeneration) { dashboardController = null; scheduleDashboard(); }
    }
  }

  function initDashboardPolling() {
    if (restoreSnapshot()) refreshDashboard();
    // Refresh at once when the tab becomes visible instead of waiting for the next tick.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(dashboardTimer); dashboardTimer = null; return; }
      clearTimeout(dashboardTimer);
      loadDashboard({ automatic: true }).catch(() => { });
    });
  }

