//
// app/admin/static/js/dashboard-battery-health.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Battery health dashboard widgets: five tiles (usable kWh, remaining % vs datasheet, cycles, last
// calibration, commissioning date) and the degradation chart. All read the local calibration
// history per inverter serial (energy/devices/{id}/battery-calibration-history), which is recorded
// whether or not the Energy Manager is enabled. The commissioning date derived from the inverter
// logs is not verified on hardware; the tile says so.
(function () {
  'use strict';
  if (document.body.dataset.page !== 'dashboard') return;

  const TILES = ['battery-usable-kwh', 'battery-remaining-percent', 'battery-cycles',
    'battery-last-calibration', 'battery-commissioning-date'];
  const CHART = 'battery-degradation';
  const IDS = [...TILES, CHART];
  const REFRESH_MS = 5 * 60 * 1000;
  const MAX_DEVICES = 8;
  const NO_DATA = 'No data yet';

  let chart = null;
  let controller = null;
  let generation = 0;

  const node = (id) => document.getElementById(id);
  const visible = (id) => !document.querySelector(`[data-widget-id="${id}"]`)?.classList.contains('d-none');
  const anyVisible = () => IDS.some(visible);
  const dayText = (value) => new Date(value).toLocaleDateString();
  const fixed = (value, digits) => Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });

  // One inverter's figures, or null with the reason why there is nothing to show.
  function facts(view) {
    if (!view.inverter_serial) return { empty: 'Serial unknown' };
    const points = Array.isArray(view.points) ? view.points : [];
    const last = points[points.length - 1];
    return {
      serial: view.inverter_serial,
      last,
      commissioning: view.commissioning || null,
      empty: last ? null : NO_DATA,
    };
  }

  const TILE_TEXT = {
    'battery-usable-kwh': (f) => `${fixed(f.last.current_usable_capacity_wh / 1000, 2)} kWh`,
    'battery-remaining-percent': (f) => `${fixed(f.last.remaining_capacity_percent, 1)} %`,
    'battery-cycles': (f) => (f.last.battery_cycles === null || f.last.battery_cycles === undefined
      ? 'n/a' : fixed(f.last.battery_cycles, 0)),
    'battery-last-calibration': (f) => dayText(f.last.measured_at),
    'battery-commissioning-date': (f) => (f.commissioning ? dayText(f.commissioning.commissioned_at) : 'Unknown'),
  };

  // value is a string (one line) or an array of { label, text, title } rows, one per inverter.
  function setTile(id, value, detail) {
    const valueNode = node(`${id}-value`);
    const detailNode = node(`${id}-detail`);
    if (valueNode && Array.isArray(value)) {
      valueNode.replaceChildren(...value.map((row) => {
        const line = document.createElement('span');
        line.className = 'battery-tile-row';
        line.title = row.title;
        const label = document.createElement('span');
        label.className = 'battery-tile-label';
        label.textContent = `${row.label}:`;
        const text = document.createElement('span');
        text.className = 'battery-tile-text';
        text.textContent = row.text;
        line.append(label, ' ', text);
        return line;
      }));
    } else if (valueNode) {
      valueNode.textContent = value;
    }
    if (detailNode) detailNode.textContent = detail;
  }

  function renderTiles(entries) {
    for (const id of TILES) {
      if (!visible(id)) continue;
      if (!entries.length) { setTile(id, NO_DATA, 'No inverter available'); continue; }
      const lines = entries.map(({ name, label, f }) => {
        // The commissioning date needs a known serial but no measurement; every other tile needs one.
        if (id === 'battery-commissioning-date') {
          if (!f.serial) return { name, label, text: f.empty, note: '' };
          return { name, label, text: TILE_TEXT[id](f), note: f.commissioning?.source === 'logger_derived' ? 'derived, unverified' : '' };
        }
        return f.empty ? { name, label, text: f.empty, note: '' } : { name, label, text: TILE_TEXT[id](f), note: '' };
      });
      if (lines.length === 1) {
        setTile(id, lines[0].text, lines[0].note || entries[0].name);
        continue;
      }
      // One row per inverter: short display name, the full serial in the tooltip.
      setTile(id,
        lines.map((line) => ({ label: line.label, text: line.text, title: line.name === line.label ? line.name : `${line.label} (${line.name})` })),
        lines.map((l) => l.note).filter(Boolean).join(' · '));
    }
  }

  function destroy() {
    chart?.destroy();
    chart = null;
    const box = node(`${CHART}-box`);
    if (box) box.hidden = true;
  }

  function colors() {
    const styles = getComputedStyle(node(`${CHART}-box`) || document.documentElement);
    const read = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    return {
      label: read('--bs-body-color', '#212529'), grid: read('--rct-line', '#dee2e6'),
      series: [read('--rct-plan-power', '#0d6efd'), read('--rct-plan-soc', '#fd7e14'), '#198754', '#6f42c1']
    };
  }

  // The baseline (commissioning, 0 cycles, 100 %) is the first point of a series, when known.
  function seriesRows(view) {
    const rows = (view.points || []).map((p) => ({
      x: Date.parse(p.measured_at), y: p.remaining_capacity_percent, cycles: p.battery_cycles,
      kwh: p.current_usable_capacity_wh / 1000,
    }));
    if (view.baseline && rows.length) {
      rows.unshift({
        x: Date.parse(view.baseline.date), y: view.baseline.remaining_capacity_percent, cycles: 0,
        kwh: view.baseline.current_usable_capacity_wh / 1000
      });
    }
    return rows;
  }

  function draw(Chart, entries) {
    destroy();
    const c = colors();
    const datasets = entries.map(({ name, view }, index) => ({
      label: name, data: seriesRows(view), borderColor: c.series[index % c.series.length],
      backgroundColor: c.series[index % c.series.length], pointRadius: 4, tension: 0,
    }));
    node(`${CHART}-box`).hidden = false;
    const canvas = node(`${CHART}-canvas`);
    canvas.setAttribute('aria-label', `Battery degradation: ${datasets.map((d) => `${d.label} ${d.data.length} points`).join(', ')}`);
    chart = new Chart(canvas, {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        plugins: {
          datalabels: { display: false },
          legend: { labels: { color: c.label } },
          tooltip: {
            callbacks: {
              title: (items) => (items[0] ? dayText(items[0].raw.x) : ''),
              label: (item) => [`${item.dataset.label}`, `Cycles: ${item.raw.cycles ?? 'n/a'}`,
              `Usable: ${fixed(item.raw.kwh, 2)} kWh`, `Remaining: ${fixed(item.raw.y, 1)} %`],
            }
          },
        },
        scales: {
          x: { type: 'linear', ticks: { color: c.label, callback: dayText, maxTicksLimit: 6 }, grid: { color: c.grid } },
          y: {
            max: 100, title: { display: true, text: '%', color: c.label }, ticks: { color: c.label },
            grid: { color: c.grid }
          },
        },
      },
    });
  }

  async function load(signal) {
    const devices = await window.RCTAdmin.api('energy/planning-devices', { signal });
    const list = Array.isArray(devices) ? devices.slice(0, MAX_DEVICES) : [];
    const entries = [];
    for (const device of list) {
      const view = await window.RCTAdmin.api(
        `energy/devices/${encodeURIComponent(device.device_id)}/battery-calibration-history`, { signal });
      const name = view.inverter_serial || device.device_name || device.device_id;
      // device_name falls back to the device id when no display name is set; the serial reads better then.
      const named = device.device_name && device.device_name !== device.device_id ? device.device_name : '';
      entries.push({ view, f: facts(view), name, label: named || name });
    }
    return entries;
  }

  async function refresh() {
    if (controller || !anyVisible()) return;
    controller = new AbortController();
    const mine = ++generation;
    try {
      const entries = await load(controller.signal);
      if (generation !== mine || !anyVisible()) return;
      renderTiles(entries);
      if (!visible(CHART)) { destroy(); return; }
      const withData = entries.filter(({ view }) => seriesRows(view).length);
      destroy();
      if (!entries.length) { node(`${CHART}-state`).textContent = 'No inverter available'; return; }
      if (!withData.length) {
        node(`${CHART}-state`).textContent = entries.every(({ view }) => !view.inverter_serial)
          ? 'Inverter serial not known yet' : 'No calibration recorded yet';
        return;
      }
      node(`${CHART}-state`).textContent = withData.every(({ view }) => view.baseline)
        ? '' : 'Some inverters have no commissioning date, their curve starts at the first measurement';
      const Chart = await window.RCTDashboardForecast.chartLibrary();
      if (generation !== mine || !visible(CHART)) return;
      draw(Chart, withData);
    } catch (error) {
      if (generation !== mine) return;
      destroy();
      const message = window.RCTAdmin.messageFrom(error) || 'Battery data unavailable';
      node(`${CHART}-state`).textContent = message;
      for (const id of TILES) if (visible(id)) setTile(id, 'Data unavailable', message);
    } finally {
      if (generation === mine) controller = null;
    }
  }

  function sync() {
    if (!anyVisible()) {
      controller?.abort(); controller = null; generation++;
      destroy();
      return;
    }
    if (!visible(CHART)) destroy();
    if (!controller) void refresh();
  }

  window.RCTDashboardBatteryHealth = Object.freeze({ sync, resize: () => chart?.resize() });
  setInterval(() => { if (anyVisible()) void refresh(); }, REFRESH_MS);
  sync();
})();
