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

  const { api, toast, messageFrom, confirmAction } = window.RCTAdmin;
  const $ = (id) => document.getElementById(id);

  // Mirrors app/admin/api.py's _DASHBOARD_WIDGETS allowlist and its default layout (order is the
  // "Add widget" list order and the fallback position for a widget newly added by an upgrade).
  const DEFAULT_LAYOUT = [
    { id: 'device-count', x: 0, y: 0, w: 3, h: 2, label: 'Inverters' },
    { id: 'connected-count', x: 3, y: 0, w: 3, h: 2, label: 'Connected' },
    { id: 'metric-count', x: 6, y: 0, w: 3, h: 2, label: 'Exposed metrics' },
    { id: 'tsdb-status', x: 9, y: 0, w: 3, h: 2, label: 'TSDB export' },
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
  const AUTO_HEIGHT_ID = 'devices'; // height follows content, is never a user layout value
  const CELL_HEIGHT = 72;
  const GRID_MARGIN = 10;
  const MOBILE_QUERY = '(max-width: 767.98px)'; // keep in sync with the stacked block in dashboard.css
  let fitFrame = 0;
  const fitRows = new Map(); // id -> rows the last fit set; such a height is content-driven, not stored
  let fitting = false; // true while fit-driven updates run: they are not user layout changes

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
    return separateOverlaps(DEFAULT_LAYOUT.map((def) => {
      const saved = byId.get(def.id);
      if (!saved) return { id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: true };
      const inRange = (value, max) => Number.isInteger(value) && value >= 0 && value <= max;
      const valid = inRange(saved.x, 11) && inRange(saved.y, 1000) &&
        Number.isInteger(saved.w) && saved.w >= 1 && saved.w <= 12 &&
        Number.isInteger(saved.h) && saved.h >= 1 && saved.h <= 100 && saved.x + saved.w <= 12;
      if (!valid) return { id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: true };
      return { id: def.id, x: saved.x, y: saved.y, w: saved.w, h: saved.h, visible: saved.visible !== false };
    }));
  }

  // A stored layout can hold overlapping boxes (e.g. a stale push). Applying it widget by widget
  // would let the last one shove the earlier ones below it, so the upper/left box keeps its place
  // and the later one moves down instead.
  function separateOverlaps(layout) {
    const placed = [];
    const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
    const byPosition = layout.filter((entry) => entry.visible).sort((a, b) => a.y - b.y || a.x - b.x);
    for (const entry of byPosition) {
      let blocker;
      while ((blocker = placed.find((other) => overlaps(entry, other)))) entry.y = blocker.y + blocker.h;
      placed.push(entry);
    }
    return layout;
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
      grid.batchUpdate(false); // commits the batch (GridStack has no separate commit() call)
    }
  }

  // Content height without the tile's stretch (cards use h-100), so the measure can shrink again.
  function naturalHeight(inner) {
    const { height, alignSelf } = inner.style;
    inner.style.setProperty('height', 'auto', 'important'); // Bootstrap's h-100 is !important
    inner.style.alignSelf = 'flex-start';
    // offsetHeight, not getBoundingClientRect: the overview uses CSS zoom, which scales rects but not rows.
    const natural = inner.offsetHeight + 1; // +1 covers the rounding of offsetHeight
    inner.style.height = height;
    inner.style.alignSelf = alignSelf;
    return natural;
  }

  // Rows so the content fits without a scrollbar: the stored height is the minimum, the content
  // only ever grows a tile. The item is rows * cell tall; the content box is smaller by the frame.
  function fitWidget(el) {
    const node = el.gridstackNode;
    const content = el.querySelector('.grid-stack-item-content');
    const inner = content?.firstElementChild;
    if (!node || !inner || el.classList.contains('d-none')) return;
    const id = el.dataset.widgetId;
    const frame = el.offsetHeight - content.offsetHeight; // GridStack's margin/inset around the content box
    const needed = Math.ceil((naturalHeight(inner) + frame) / CELL_HEIGHT);
    const minimum = id === AUTO_HEIGHT_ID ? 1 : (widgetState.get(id)?.h ?? 1);
    const rows = Math.max(minimum, needed);
    fitRows.set(id, rows);
    if (node.h !== rows) grid.update(el, { h: rows });
  }

  function fitLayout() {
    fitFrame = 0;
    // Below the breakpoint the tiles are stacked with natural height; rows are irrelevant there.
    if (!grid || window.matchMedia(MOBILE_QUERY).matches) return;
    // A fit batch during a drag or resize would swallow the user's own change event.
    if (document.querySelector('.ui-draggable-dragging, .ui-resizable-resizing')) return;
    fitting = true;
    try {
      grid.batchUpdate();
      document.querySelectorAll('.grid-stack-item[data-widget-id]').forEach(fitWidget);
    } finally {
      grid.batchUpdate(false);
      fitting = false;
    }
  }

  function scheduleFit() {
    if (!fitFrame) fitFrame = requestAnimationFrame(fitLayout);
  }

  // Anything that changes a tile's content size re-runs the fit: window and sidebar width (grid
  // observer), text and card collapse (mutations), and the icon font arriving late.
  function observeContent() {
    const root = $('dashboard-grid');
    window.addEventListener('resize', scheduleFit);
    document.fonts?.ready.then(scheduleFit);
    new MutationObserver(scheduleFit).observe(root, {
      subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['class', 'hidden'],
    });
    if (!('ResizeObserver' in window)) return;
    const observer = new ResizeObserver(scheduleFit);
    observer.observe(root);
    root.querySelectorAll('.grid-stack-item-content > *').forEach((inner) => observer.observe(inner));
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { saveTimer = null; saveLayout(); }, SAVE_DEBOUNCE_MS);
  }

  // Change events can report a position before GridStack's final compaction or miss pushed
  // neighbours, so the saved layout is read from the settled grid, not from the event deltas.
  function syncStateFromGrid() {
    for (const def of DEFAULT_LAYOUT) {
      const node = widgetElement(def.id)?.gridstackNode;
      const prev = widgetState.get(def.id);
      if (!node || !prev || prev.visible === false) continue;
      const h = fitRows.get(def.id) === node.h ? prev.h : node.h;
      widgetState.set(def.id, { id: def.id, x: node.x, y: node.y, w: node.w, h, visible: true });
    }
  }

  // Only the known structured props travel to the server; nothing from the DOM/HTML is ever sent.
  async function saveLayout() {
    syncStateFromGrid();
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
    if (fitting) return; // content-driven rows and the pushes they cause are not the user's layout
    for (const item of items || []) {
      const id = item.el?.dataset?.widgetId;
      if (!id) continue;
      const prev = widgetState.get(id);
      const h = prev && fitRows.get(id) === item.h ? prev.h : item.h;
      widgetState.set(id, { id, x: item.x, y: item.y, w: item.w, h, visible: true });
    }
    scheduleSave();
  }

  let editingNow = false;

  function setEditing(editing) {
    editingNow = editing;
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
    // The button that held focus is hidden now; hand focus to its counterpart (not when a viewport change hid both).
    const next = $(editing ? 'dashboard-edit-done' : 'dashboard-edit-toggle');
    if (next.offsetParent) next.focus();
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
      grid.batchUpdate(false);
    }
    widgetState.set(id, { ...state, visible });
    scheduleFit();
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
    if (!await confirmAction({
      title: 'Restore default layout?',
      message: 'All widgets return to their default position and size. Your current arrangement is replaced.',
      confirmLabel: 'Restore default layout',
      danger: true,
    })) return;
    try {
      await api('dashboard-layout', { method: 'DELETE' });
    } catch (error) {
      toast(messageFrom(error), 'danger');
      return;
    }
    applyLayout(DEFAULT_LAYOUT.map((def) => ({ id: def.id, x: def.x, y: def.y, w: def.w, h: def.h, visible: true })));
    scheduleFit();
    renderAddWidgetList();
    savedToastShownThisSession = true; // explicit action: show exactly this one toast, not a second autosave toast
    toast('Dashboard layout restored.');
  }

  async function initLayout() {
    grid = window.GridStack.init({
      column: 12,
      cellHeight: CELL_HEIGHT,
      margin: GRID_MARGIN,
      float: false,
      animate: true,
      disableDrag: true,
      disableResize: true,
      handle: '.dashboard-widget-header', // grab a tile by its header strip, like a Grafana panel
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

    fitLayout();
    observeContent();

    grid.on('change', onGridChange);
    grid.on('dragstop resizestop', scheduleFit);
    $('dashboard-edit-toggle').addEventListener('click', () => setEditing(true));
    $('dashboard-edit-done').addEventListener('click', () => {
      setEditing(false);
      if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; saveLayout(); } // an immediate reload must not lose the last move
    });
    // Positions are ignored by the stacked phone layout, so editing ends when the viewport shrinks to it.
    window.matchMedia(MOBILE_QUERY).addEventListener('change', (event) => { if (event.matches && editingNow) setEditing(false); });
    // Deep link from the Inverters page.
    if (new URLSearchParams(location.search).get('manage') === 'inverters') {
      window.bootstrap.Modal.getOrCreateInstance($('inverters-modal')).show();
    }
    $('dashboard-reset-layout').addEventListener('click', () => { resetLayout().catch((error) => toast(messageFrom(error), 'danger')); });
  }

  window.RCTDashboard = Object.freeze({ initLayout });
  document.dispatchEvent(new CustomEvent('rct:dashboard-ready'));
})();
