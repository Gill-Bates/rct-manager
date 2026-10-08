//
// app/admin/static/js/dashboard.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// GridStack-based layout layer for the overview page: position, size and visibility of the
// existing KPI/TSDB/inverter widgets. It owns none of the dashboard *data* — loadDashboard(),
// showHeroMetrics(), renderTsdbTile() and renderDashboard() in admin.js keep doing that unchanged.
(function () {
  'use strict';

  if (document.body.dataset.page !== 'dashboard') return;

  const { api, toast, messageFrom } = window.RCTAdmin;
  const $ = (id) => document.getElementById(id);

  // Mirrors app/admin/api.py's _DASHBOARD_WIDGETS allowlist and its default layout (order is the
  // "Add widget" list order and the fallback position for a widget newly added by an upgrade).
  const DEFAULT_LAYOUT = [
    { id: 'device-count', x: 0, y: 0, w: 3, h: 2, label: 'Inverters' },
    { id: 'connected-count', x: 3, y: 0, w: 3, h: 2, label: 'Connected inverters' },
    { id: 'metric-count', x: 6, y: 0, w: 3, h: 2, label: 'Exposed metrics' },
    { id: 'tsdb-status', x: 9, y: 0, w: 3, h: 2, label: 'TSDB status' },
    { id: 'pv-power', x: 0, y: 2, w: 3, h: 2, label: 'PV power' },
    { id: 'house-power', x: 3, y: 2, w: 3, h: 2, label: 'House consumption' },
    { id: 'grid-power', x: 6, y: 2, w: 3, h: 2, label: 'Grid power' },
    { id: 'battery-soc', x: 9, y: 2, w: 3, h: 2, label: 'Battery level' },
    { id: 'devices', x: 0, y: 4, w: 12, h: 8, label: 'Inverter overview' },
  ];
  const SAVE_DEBOUNCE_MS = 400;

  let grid = null;
  const widgetState = new Map(); // id -> { id, x, y, w, h, visible }
  let saveTimer = null;
  let savedToastShownThisSession = false;

  function widgetElement(id) {
    return document.querySelector(`.grid-stack-item[data-widget-id="${id}"]`);
  }

  // A broken/foreign/future-version stored layout must never make the dashboard unusable; any
  // problem here falls back to the known-good default instead of raising.
  function mergeWithDefault(stored) {
    const byId = new Map();
    if (stored && stored.version === 1 && Array.isArray(stored.widgets)) {
      for (const widget of stored.widgets) {
        if (!widget || typeof widget.id !== 'string') continue;
        byId.set(widget.id, widget); // unknown ids are dropped below: only DEFAULT_LAYOUT ids are read
      }
    }
    return DEFAULT_LAYOUT.map((def) => {
      const saved = byId.get(def.id);
      if (!saved) return { id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: true };
      const inRange = (value, max) => Number.isInteger(value) && value >= 0 && value <= max;
      const valid = inRange(saved.x, 11) && inRange(saved.y, 1000) &&
        Number.isInteger(saved.w) && saved.w >= 1 && saved.w <= 12 &&
        Number.isInteger(saved.h) && saved.h >= 1 && saved.h <= 100 && saved.x + saved.w <= 12;
      if (!valid) return { id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: true };
      return { id: def.id, x: saved.x, y: saved.y, w: saved.w, h: saved.h, visible: saved.visible !== false };
    });
  }

  function applyLayout(layout) {
    grid.batchUpdate();
    try {
      for (const entry of layout) {
        widgetState.set(entry.id, { ...entry });
        const el = widgetElement(entry.id);
        if (!el) continue;
        if (entry.visible) {
          el.classList.remove('d-none');
          if (!el.gridstackNode) grid.makeWidget(el);
          grid.update(el, { x: entry.x, y: entry.y, w: entry.w, h: entry.h });
        } else {
          if (el.gridstackNode) grid.removeWidget(el, false);
          el.classList.add('d-none');
        }
      }
    } finally {
      grid.commit();
    }
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveLayout, SAVE_DEBOUNCE_MS);
  }

  // Only the known structured props travel to the server; nothing from the DOM/HTML is ever sent.
  async function saveLayout() {
    const widgets = DEFAULT_LAYOUT.map((def) => {
      const state = widgetState.get(def.id);
      return { id: def.id, x: state.x, y: state.y, w: state.w, h: state.h, visible: state.visible };
    });
    try {
      await api('dashboard-layout', { method: 'PUT', body: JSON.stringify({ version: 1, widgets }) });
      if (!savedToastShownThisSession) {
        savedToastShownThisSession = true;
        toast('Dashboard layout saved.');
      }
    } catch (error) {
      toast(messageFrom(error), 'danger');
    }
  }

  function onGridChange(_event, items) {
    for (const item of items || []) {
      const id = item.el?.dataset?.widgetId;
      if (!id) continue;
      widgetState.set(id, { id, x: item.x, y: item.y, w: item.w, h: item.h, visible: true });
    }
    scheduleSave();
  }

  function setEditing(editing) {
    document.getElementById('dashboard-grid').classList.toggle('dashboard-editing', editing);
    document.querySelectorAll('.dashboard-widget-header').forEach((header) => header.classList.toggle('d-none', !editing));
    $('dashboard-edit-toggle').classList.toggle('d-none', editing);
    $('dashboard-add-widget').classList.toggle('d-none', !editing);
    $('dashboard-reset-layout').classList.toggle('d-none', !editing);
    $('dashboard-edit-done').classList.toggle('d-none', !editing);
    $('dashboard-edit-toggle').setAttribute('aria-pressed', String(editing));
    grid.enableMove(editing);
    grid.enableResize(editing);
    if (editing) savedToastShownThisSession = false; // one save-toast per editing session (req. 15)
  }

  function toggleWidgetVisibility(id, visible) {
    const state = widgetState.get(id) || DEFAULT_LAYOUT.find((def) => def.id === id);
    const el = widgetElement(id);
    if (!el) return;
    grid.batchUpdate();
    try {
      if (visible) {
        el.classList.remove('d-none');
        grid.makeWidget(el);
        grid.update(el, { x: state.x, y: state.y, w: state.w, h: state.h });
      } else if (el.gridstackNode) {
        grid.removeWidget(el, false);
        el.classList.add('d-none');
      }
    } finally {
      grid.commit();
    }
    widgetState.set(id, { ...state, visible });
    renderAddWidgetList();
    scheduleSave();
  }

  function renderAddWidgetList() {
    const list = $('add-widget-list');
    list.replaceChildren(
      ...DEFAULT_LAYOUT.map((def) => {
        const visible = widgetState.get(def.id)?.visible !== false;
        const item = document.createElement('li');
        item.className = 'list-group-item d-flex align-items-center justify-content-between gap-2';
        const label = document.createElement('span');
        label.textContent = def.label;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = visible ? 'btn btn-sm btn-outline-secondary' : 'btn btn-sm btn-primary';
        button.textContent = visible ? 'Remove' : 'Add';
        button.setAttribute('aria-label', `${visible ? 'Remove' : 'Add'} widget: ${def.label}`);
        button.addEventListener('click', () => toggleWidgetVisibility(def.id, !visible));
        item.append(label, button);
        return item;
      })
    );
  }

  async function resetLayout() {
    if (!confirm('Restore default layout?')) return;
    try {
      await api('dashboard-layout', { method: 'DELETE' });
    } catch (error) {
      toast(messageFrom(error), 'danger');
      return;
    }
    applyLayout(DEFAULT_LAYOUT.map((def) => ({ id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: true })));
    renderAddWidgetList();
    savedToastShownThisSession = true; // explicit action: show exactly this one toast, not a second autosave toast
    toast('Dashboard layout saved.');
  }

  async function initLayout() {
    grid = window.GridStack.init({
      column: 12,
      cellHeight: 72,
      margin: 8,
      float: false,
      animate: true,
      disableDrag: true,
      disableResize: true,
      handle: '.dashboard-drag-handle',
    }, '#dashboard-grid');

    let stored = null;
    try {
      const result = await api('dashboard-layout');
      stored = result.layout;
    } catch (error) {
      // A failed GET falls back to the template default like a missing/invalid stored layout.
      toast(messageFrom(error), 'danger');
    }
    applyLayout(mergeWithDefault(stored));
    renderAddWidgetList();

    grid.on('change', onGridChange);
    $('dashboard-edit-toggle').addEventListener('click', () => setEditing(true));
    $('dashboard-edit-done').addEventListener('click', () => setEditing(false));
    $('dashboard-reset-layout').addEventListener('click', () => { resetLayout().catch((error) => toast(messageFrom(error), 'danger')); });
  }

  window.RCTDashboard = Object.freeze({ initLayout });
})();
