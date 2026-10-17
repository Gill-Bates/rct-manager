//
// app/admin/static/js/dashboard-charging-schedule.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// "Charging schedule" dashboard widget: the read-only Dry-Run charging plan of the first planning
// device as a time series (grid charge power in kW, target SoC in %). It follows the lifecycle of
// dashboard-forecast.js (same 5-minute cadence, driven by its sync/resize hooks) and reuses its
// lazily loaded Chart.js. Non-"ok" plan statuses render as a short text state instead of a chart.
(function () {
  'use strict';
  if (document.body.dataset.page !== 'dashboard') return;

  const ID = 'charging-schedule';
  const REFRESH_MS = 5 * 60 * 1000;
  const MAX_SLOTS = 1000;
  const STATUS_TEXT = {
    insufficient_history: 'Plan not available yet: not enough consumption history',
    spot_disabled: 'Spot market disabled',
    spot_prices_unavailable: 'Plan not available yet: no spot prices',
    solar_not_configured: 'Solar forecast not configured',
    solar_forecast_unavailable: 'Plan not available yet: no solar forecast',
    battery_state_unavailable: 'Plan not available yet: battery state unknown',
  };
  const ACTION_LABELS = { charge: 'Grid charge', idle: 'No grid charge' };
  const REASON_LABELS = {
    'forecast PV covers demand': 'Solar forecast covers the household consumption',
    'battery reserve covers demand': 'Battery charge covers the consumption, no grid charge needed',
    'forecast deficit, charged as late as permitted': 'Expected shortfall: charge from the grid, as late as possible',
    'charge as late as permitted for target SoC': 'Reach the target SoC: charge as late as possible',
    'negative spot price, forecast deficit': 'Negative spot price: charge now to cover the expected shortfall',
    'no spot price for this slot': 'No spot price known for this period',
  };

  let chart = null;
  let controller = null;
  let generation = 0;

  const node = (id) => document.getElementById(id);
  const visible = () => !document.querySelector(`[data-widget-id="${ID}"]`)?.classList.contains('d-none');
  const dateTime = (value) => new Date(value).toLocaleString(undefined,
    { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  const timeOnly = (value) => new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

  function colors() {
    const styles = getComputedStyle(node(`${ID}-box`) || document.documentElement);
    const read = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
    return {
      label: read('--bs-body-color', '#212529'), grid: read('--rct-line', '#dee2e6'),
      power: read('--rct-plan-power', '#0d6efd'), soc: read('--rct-plan-soc', '#fd7e14'),
    };
  }

  function setState(text) { node(`${ID}-state`).textContent = text; }

  function destroy() {
    chart?.destroy();
    chart = null;
    const box = node(`${ID}-box`);
    if (box) box.hidden = true;
  }

  function validSlots(slots) {
    return Array.isArray(slots) && slots.length > 0 && slots.length <= MAX_SLOTS && slots.every((slot) =>
      slot && Number.isFinite(Date.parse(slot.start)) && Number.isFinite(Date.parse(slot.end)) &&
      Date.parse(slot.end) > Date.parse(slot.start));
  }

  // One point at every slot start and end, so uneven slots keep their length on the linear time
  // axis; a gap between non-adjacent slots breaks the line.
  function series(slots) {
    const power = [];
    const soc = [];
    let previousEnd = null;
    for (const slot of [...slots].sort((a, b) => Date.parse(a.start) - Date.parse(b.start))) {
      const start = Date.parse(slot.start);
      const end = Date.parse(slot.end);
      if (previousEnd !== null && start !== previousEnd) {
        power.push({ x: previousEnd, y: null });
        soc.push({ x: previousEnd, y: null });
      }
      const watts = Number(slot.max_power_w);
      const target = slot.target_soc_percent === null || slot.target_soc_percent === undefined
        ? NaN : Number(slot.target_soc_percent);
      const powerKw = slot.action === 'charge' && Number.isFinite(watts) ? watts / 1000 : 0;
      for (const x of [start, end]) {
        power.push({ x, y: powerKw, slot });
        soc.push({ x, y: Number.isFinite(target) ? target : null, slot });
      }
      previousEnd = end;
    }
    return { power, soc };
  }

  function ticks(min, max) {
    const hour = 3600000;
    const step = [1, 2, 3, 6, 12, 24].find((h) => (max - min) / (h * hour) <= 6) || 24;
    const first = new Date(min);
    first.setMinutes(0, 0, 0);
    while (first.getHours() % step !== 0 || first.getTime() < min) first.setTime(first.getTime() + hour);
    const result = [];
    for (let t = first.getTime(); t <= max; t += step * hour) result.push({ value: t });
    return result;
  }

  function tickLabel(value) {
    const date = new Date(value);
    if (date.getHours() !== 0) return timeOnly(value);
    return [date.toLocaleDateString(undefined, { day: '2-digit', month: 'short' }), timeOnly(value)];
  }

  function summary(slots) {
    const sorted = [...slots].sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
    const charge = slots.filter((slot) => slot.action === 'charge').length;
    return `Charging schedule ${dateTime(sorted[0].start)} – ${dateTime(sorted[sorted.length - 1].end)}: ` +
      `${charge} grid charge slot${charge === 1 ? '' : 's'} of ${slots.length}`;
  }

  function slotLines(slot) {
    const watts = Number(slot.max_power_w);
    const target = slot.target_soc_percent === null || slot.target_soc_percent === undefined
      ? NaN : Number(slot.target_soc_percent);
    return [
      `Action: ${ACTION_LABELS[slot.action] || slot.action}`,
      `Power: ${Number.isFinite(watts) ? `${(watts / 1000).toFixed(2)} kW` : 'n/a'}`,
      `Target SoC: ${Number.isFinite(target) ? `${Math.round(target)} %` : 'n/a'}`,
      ...(slot.reason ? [`Reason: ${REASON_LABELS[slot.reason] || slot.reason}`] : []),
    ];
  }

  function draw(Chart, slots) {
    destroy();
    const { power, soc } = series(slots);
    const times = power.map((point) => point.x);
    const min = Math.min(...times);
    const max = Math.max(...times);
    const c = colors();
    const hasSoc = soc.some((point) => point.y !== null);
    const box = node(`${ID}-box`);
    box.hidden = false;
    const canvas = node(`${ID}-canvas`);
    canvas.setAttribute('aria-label', summary(slots));
    chart = new Chart(canvas, {
      type: 'line',
      data: {
        datasets: [
          {
            label: 'Grid charge power (kW)', data: power, yAxisID: 'power', borderColor: c.power,
            backgroundColor: `${c.power}33`, fill: 'origin', pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: false,
          },
          {
            label: 'Target SoC (%)', data: soc, yAxisID: 'soc', borderColor: c.soc, backgroundColor: c.soc,
            borderDash: [6, 4], pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: false, hidden: !hasSoc,
          },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'nearest', intersect: false, axis: 'x' },
        plugins: {
          datalabels: { display: false },
          legend: { display: true, labels: { color: c.label, usePointStyle: true, pointStyle: 'line' } },
          tooltip: {
            // Both series describe the same slot, so one entry carries the whole tooltip.
            filter: (item, index, items) => items.findIndex((other) => Number.isFinite(other.parsed?.y)) === index,
            callbacks: {
              title: (items) => {
                const slot = items[0]?.raw?.slot;
                if (!slot) return '';
                const sameDay = new Date(slot.start).toDateString() === new Date(slot.end).toDateString();
                return `${dateTime(slot.start)} – ${sameDay ? timeOnly(slot.end) : dateTime(slot.end)}`;
              },
              label: (item) => (item.raw?.slot ? slotLines(item.raw.slot) : ''),
            },
          },
        },
        scales: {
          x: {
            type: 'linear', min, max, grid: { color: c.grid },
            afterBuildTicks: (axis) => { axis.ticks = ticks(axis.min, axis.max); },
            ticks: { color: c.label, maxRotation: 0, callback: tickLabel },
          },
          power: {
            position: 'left', beginAtZero: true, grid: { color: c.grid }, ticks: { color: c.label },
            title: { display: true, text: 'kW', color: c.label },
          },
          soc: {
            position: 'right', min: 0, max: 100, display: hasSoc, grid: { drawOnChartArea: false },
            ticks: { color: c.label }, title: { display: true, text: '%', color: c.label },
          },
        },
      },
    });
  }

  function syncTheme() {
    if (!chart) return;
    const c = colors();
    chart.options.plugins.legend.labels.color = c.label;
    for (const axis of Object.values(chart.options.scales)) {
      axis.ticks.color = c.label;
      if (axis.grid.drawOnChartArea !== false) axis.grid.color = c.grid;
      if (axis.title) axis.title.color = c.label;
    }
    const [powerSet, socSet] = chart.data.datasets;
    powerSet.borderColor = c.power;
    powerSet.backgroundColor = `${c.power}33`;
    socSet.borderColor = c.soc;
    socSet.backgroundColor = c.soc;
    chart.update('none');
  }

  async function load(signal) {
    const devices = await window.RCTAdmin.api('energy/planning-devices', { signal, background: true });
    if (!Array.isArray(devices) || !devices.length) return { empty: true };
    const device = devices[0];
    const plan = await window.RCTAdmin.api(
      `energy/devices/${encodeURIComponent(device.device_id)}/dynamic-plan`, { signal, background: true });
    return { device, plan };
  }

  async function refresh() {
    if (controller || !visible()) return;
    controller = new AbortController();
    const mine = ++generation;
    try {
      const { empty, device, plan } = await load(controller.signal);
      if (generation !== mine || !visible()) return;
      destroy();
      if (empty) { setState('No inverter available'); return; }
      const name = device.device_name || device.device_id;
      if (plan.status !== 'ok') {
        setState(STATUS_TEXT[plan.status] || 'Plan not available yet');
        return;
      }
      if (!validSlots(plan.slots)) { setState(plan.slots?.length ? 'Plan data is invalid' : 'Plan not available yet'); return; }
      setState(`Device: ${name}`);
      const Chart = await window.RCTDashboardForecast.chartLibrary();
      if (generation !== mine || !visible()) return;
      draw(Chart, plan.slots);
    } catch (error) {
      if (generation !== mine) return;
      if (error?.rateLimited) { // keep what is drawn and retry once the rate-limit cooldown ended
        setTimeout(() => { if (!document.hidden && visible()) void refresh(); }, window.RCTAdmin.pollDelay(5000));
        return;
      }
      destroy();
      setState(window.RCTAdmin.messageFrom(error) || 'Charging schedule unavailable');
    } finally {
      if (generation === mine) controller = null;
    }
  }

  function sync() {
    if (!visible()) {
      controller?.abort(); controller = null; generation++;
      destroy();
      return;
    }
    if (!chart) void refresh();
  }

  window.RCTDashboardChargingSchedule = Object.freeze({ sync, resize: () => chart?.resize() });
  setInterval(() => { if (!document.hidden && visible()) void refresh(); }, REFRESH_MS);
  if (typeof MutationObserver !== 'undefined') {
    new MutationObserver(syncTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] });
  }
  sync();
})();
