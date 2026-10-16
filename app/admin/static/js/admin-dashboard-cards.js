//
// app/admin/static/js/admin-dashboard-cards.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  function displayStatus(status) {
    const key = String(status).toLowerCase();
    if (status === true || ['ok', 'connected', 'online', 'healthy'].includes(key)) return ['Connected', 'online'];
    if (key === 'degraded') return ['Degraded', 'offline'];
    if (key === 'starting') return ['Connecting', ''];
    if (key === 'maintenance') return ['Maintenance', ''];
    if (status === false || ['disconnected', 'offline', 'unreachable', 'error'].includes(key)) return ['Disconnected', 'offline'];
    return [String(status || 'Unknown'), ''];
  }

  // The one text for a metric without a value, whatever the reason; a reason belongs in a tooltip.
  const NOT_AVAILABLE = 'n/a';

  // Splits a reading into number and unit so the card can render the unit smaller than the number.
  function metricParts(value, unit) {
    if (!Number.isFinite(value)) return [NOT_AVAILABLE, ''];
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
    return Number.isFinite(value) ? metricParts(value, unit).join('') : 'n/a';
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

  // One descriptor per physical tower from /admin/api/devices (`batteries`). module_count is per tower and
  // null when untrustworthy (module_count_status: ok / pending / anomaly, see _battery_module_report).
  // Without module_sn slots there are no `batteries` entries; BATTERY_PRESENCE falls back to one card.
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
  // stacking top + middle*(N-1) + bottom at one common rendered width with no gap reproduces one
  // continuous case. Height per slice follows from its own viewBox aspect ratio at that width, so
  // slices never need matching/hardcoded heights. These viewBox heights must stay in sync with the
  // SVGs.
  const BATTERY_SLICE_VIEWBOX_WIDTH = 220;
  const BATTERY_SLICES = {
    top: { file: 'battery_top.svg', viewBoxHeight: 70 },
    middle: { file: 'battery_middle.svg', viewBoxHeight: 109 },
    bottom: { file: 'battery_bottom.svg', viewBoxHeight: 132 },
  };
  // Upper bound for the rendered tower, sized so a full 6-module tower is flush with the readings
  // column (charge bar plus two metric rows) beside it. In viewBox units an assembled tower is
  // 70 + (N-1)*109 + 132, i.e. 311 units at N=2 and 747 at N=6, so a fixed width makes a six-module
  // tower more than twice as tall as a two-module one and drags the whole card down. The width is
  // therefore derived from the cap for the given module count: a tall tower gets *narrower*, never
  // shorter and never fewer modules, so the module count stays visually apparent.
  const BATTERY_TOWER_MAX_HEIGHT = 207;
  // Width a short tower is allowed to reach; also the battery card's image column (5rem in admin.css).
  const BATTERY_SLICE_MAX_WIDTH = 80;

  function batteryTowerViewBoxHeight(moduleCount) {
    return BATTERY_SLICES.top.viewBoxHeight + (moduleCount - 1) * BATTERY_SLICES.middle.viewBoxHeight
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

  // The bottom slice is itself a battery module, so N modules are the top cap, N-1 middles and the
  // bottom — DOM order top-to-bottom, matching the visual stack order.
  function buildBatteryStack(moduleCount) {
    const width = batterySliceWidth(moduleCount);
    const nodes = [batterySliceImage('top', width)];
    for (let i = 1; i < moduleCount; i += 1) nodes.push(batterySliceImage('middle', width));
    nodes.push(batterySliceImage('bottom', width));
    return nodes;
  }

  // Hardware limit: the catalog's seven serial slots are the size of a data structure, the
  // documented hardware takes at most six modules per tower. Every count 1..6 is drawn 1:1.
  const BATTERY_MAX_MODULES_PER_TOWER = 6;

  function renderableSegmentCount(moduleCount) {
    if (typeof moduleCount !== 'number' || moduleCount < 1) return null;
    if (moduleCount > BATTERY_MAX_MODULES_PER_TOWER) return null;
    return moduleCount;
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
    const chip = element('span', 'status-badge status-badge-neutral');
    const text = element('span');
    chip.append(text);
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
    setClass(head.chip, 'status-badge-warning', Boolean(notice));
    setClass(head.chip, 'status-badge-neutral', !notice);
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
      // An optional cell whose reading only ran out stays, as n/a, so the card keeps its shape.
      if (!cell.optional || available || metrics.get(cell.name)?.expired) nodes.push(cell.node);
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
    if (wanted !== null) card.note.hidden = true;
    if (wanted === null) {
      if (tower.moduleCountStatus === 'pending') {
        setText(card.note, 'Detecting modules…');
        const detail = 'The battery has not finished reporting its module serials yet; the module tower will '
          + 'appear once the scan completes.';
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
    const shown = Boolean(hasReading || device.flow_known);
    visual.flowBox.hidden = !shown;
    setClass(visual.node, 'has-flow', shown);
    if (shown) visual.flow.update(readings);
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
    // Same trash action as the inverters modal, surfaced on the card: delete this inverter from the
    // dashboard without opening the dialog. The device identity to delete is set in patchDeviceCard.
    const remove = element('button', 'device-remove icon-button btn btn-outline-danger');
    remove.type = 'button';
    const removeIcon = element('span', 'material-icons', 'delete_outline');
    removeIcon.setAttribute('aria-hidden', 'true');
    remove.append(removeIcon);
    const ref = {
      toggle,
      remove,
      card: element('article', 'device-item'),
      head: element('div', 'device-head'),
      statusBadge: element('span', 'status-badge status-badge-success'),
      top: element('div', 'device-head-main'),
      meta: element('div', 'device-head-meta'),
      dot: element('span', 'status-dot'),
      title: element('h3', 'mb-0 me-auto'),
      statusText: element('div', 'fw-medium small'),
      address: element('small', 'text-secondary'),
      serial: element('small', 'text-secondary device-serial'),
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
    remove.addEventListener('click', () => { removeDashboardDevice(ref, remove).catch((error) => toast(messageFrom(error), 'danger')); });
    return ref;
  }

  function patchDeviceCard(ref, device) {
    const [status, statusClass] = displayStatus(device.status);
    const connected = statusClass === 'online';
    setClass(ref.card, 'has-status-badge', connected);
    setText(ref.statusBadge, status);
    ref.dot.className = `status-dot ${statusClass}`;
    setText(ref.title, !connected && device.name_is_fallback ? NOT_AVAILABLE : (device.name || device.id || NOT_AVAILABLE));
    ref.toggle.setAttribute('aria-label', `Show or hide details of ${ref.title.textContent}`);
    setText(ref.statusText, status);
    // The trash button deletes by the server-assigned device id (same identity the draft carries as
    // device_id), so a dashboard card can be removed without the sensitive settings read on first paint.
    ref.removeId = device.id;
    const removeLabel = `Remove inverter ${device.name || device.id || device.host || NOT_AVAILABLE}`;
    ref.remove.title = removeLabel;
    ref.remove.setAttribute('aria-label', removeLabel);
    // Name, address, last connection and the connection state share two compact header lines.
    syncChildren(ref.top, connected ? [ref.toggle, ref.title, ref.statusBadge, ref.remove] : [ref.toggle, ref.dot, ref.title, ref.statusText, ref.remove]);
    setText(ref.address, `${device.host || NOT_AVAILABLE}${device.port ? `:${device.port}` : ''}`);
    if (device.last_success_at) setText(ref.last, `Last connection: ${formatDate(device.last_success_at, { time: true })}`);
    const serial = typeof device.serial_number === 'string' ? device.serial_number.trim() : '';
    if (serial) setText(ref.serial, `S/N: ${serial}`);
    syncChildren(ref.meta, [ref.address, ...(serial ? [ref.serial] : []), ...(device.last_success_at ? [ref.last] : [])]);
    syncChildren(ref.head, [ref.top, ref.meta]);
    patchDeviceVisual(ref.visual, device);
    syncChildren(ref.card, [ref.head, ref.visual.node]);
    return { connected };
  }

