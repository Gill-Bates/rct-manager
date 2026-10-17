//
// app/admin/static/js/dashboard-forecast.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Spot and solar forecast widgets share one projection request and one Chart.js lifecycle. Spot and
// solar are requested and rendered independently: a dashboard that shows only a solar widget asks
// for include=solar alone and renders without data.spot, and the reverse holds for spot-only.
(function () {
  'use strict';
  if (document.body.dataset.page !== 'dashboard') return;

  const spotIds = ['spot-price-current', 'spot-market-status', 'spot-price-curve'];
  const solarIds = ['solar-forecast-status', 'pv-forecast-curve'];
  const ids = [...spotIds, ...solarIds];
  const chartIds = ['spot-price-curve', 'pv-forecast-curve'];
  const charts = new Map();
  const chartGeneration = new Map();
  let controller = null;
  let selection = '';
  let generation = 0;
  let chartLibrary = null;

  const node = (id) => document.getElementById(id);
  const visible = (id) => !document.querySelector(`[data-widget-id="${id}"]`)?.classList.contains('d-none');
  const formattedTime = (value, timeZone = 'UTC') => value ? new Date(value).toLocaleTimeString(undefined,
    { hour: '2-digit', minute: '2-digit', timeZone, timeZoneName: 'shortOffset' }) : null;
  const stateText = (status) => ({
    disabled: 'Disabled', unavailable: 'No forecast available',
    stale: 'Data stale', error: 'Data unavailable'
  })[status] || 'No forecast available';

  function loadScript(path) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = path;
      script.onload = resolve;
      script.onerror = () => {
        script.remove();
        reject(new Error('Chart library could not be loaded'));
      };
      document.head.append(script);
    });
  }

  function ensureChartLibrary() {
    if (!chartLibrary) {
      chartLibrary = loadScript('/admin/static/vendor/chart.umd.min.js')
        .then(() => loadScript('/admin/static/vendor/chartjs-plugin-datalabels.min.js'))
        .then(() => {
          window.Chart.register(window.ChartDataLabels);
          return window.Chart;
        })
        .catch((error) => {
          chartLibrary = null;
          throw error;
        });
    }
    return chartLibrary;
  }

  function destroy(id) {
    chartGeneration.set(id, (chartGeneration.get(id) || 0) + 1);
    charts.get(id)?.destroy();
    charts.delete(id);
    const box = node(`${id}-box`);
    if (box) box.hidden = true;
  }

  function setKpi(id, value, detail) {
    if (!visible(id)) return;
    node(`${id}-value`).textContent = value;
    node(`${id}-detail`).textContent = detail;
  }

  function intervalPoints(series, valueKey) {
    const points = [];
    let previousEnd = null;
    for (const interval of series) {
      const start = Date.parse(interval.start);
      const end = Date.parse(interval.end);
      if (previousEnd !== null && start > previousEnd) points.push({ x: previousEnd, y: null });
      points.push({ x: start, y: interval[valueKey], interval });
      points.push({ x: end, y: interval[valueKey], interval });
      previousEnd = end;
    }
    return points;
  }

  async function renderChart(id, series, valueKey, unit, status, timeZone, sourceLabel) {
    if (!visible(id)) return;
    destroy(id);
    const renderGeneration = chartGeneration.get(id);
    const state = node(`${id}-state`);
    if (!['available', 'stale'].includes(status) || !Array.isArray(series) || !series.length) {
      state.textContent = ['available', 'stale'].includes(status) ? 'No future intervals available' : stateText(status);
      return;
    }
    if (series.length > 1000 || series.some((point) => !point ||
      !Number.isFinite(Date.parse(point.start)) || !Number.isFinite(Date.parse(point.end)) ||
      Date.parse(point.end) <= Date.parse(point.start) || !Number.isFinite(point[valueKey]))) {
      state.textContent = 'Forecast data is invalid';
      return;
    }
    state.textContent = 'Loading chart …';
    const Chart = await ensureChartLibrary();
    if (!visible(id) || chartGeneration.get(id) !== renderGeneration) return;
    const box = node(`${id}-box`);
    box.hidden = false;
    state.textContent = `${status === 'stale' ? 'Last available data' : ''}${sourceLabel ? `${status === 'stale' ? ' · ' : ''}Source: ${sourceLabel}` : ''}`;
    const color = getComputedStyle(document.documentElement).getPropertyValue('--bs-primary').trim() || '#0d6efd';
    charts.set(id, new Chart(node(`${id}-canvas`), {
      type: 'line',
      data: {
        datasets: [{
          label: unit, data: intervalPoints(series, valueKey),
          borderColor: color, backgroundColor: color, pointRadius: 0, borderWidth: 2, tension: 0, spanGaps: false
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false, axis: 'x' },
        plugins: {
          legend: { display: false }, datalabels: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => {
                const interval = items[0].raw.interval;
                return interval ? `${formattedTime(interval.start, timeZone)} – ${formattedTime(interval.end, timeZone)}` : '';
              },
              label: (item) => `${Number(item.parsed.y).toFixed(2)} ${unit}`
            }
          }
        },
        scales: {
          x: {
            type: 'linear', ticks: {
              maxTicksLimit: 7, maxRotation: 0,
              callback: (value) => formattedTime(value, timeZone)
            }
          },
          y: { title: { display: true, text: unit } }
        },
      },
    }));
  }

  function renderSpot(spot) {
    const timeZone = spot.timezone || 'UTC';
    const spotUpdate = formattedTime(spot.source_generated_at || spot.fetched_at, timeZone);
    const currentUpdate = formattedTime(spot.current_fetched_at, timeZone);
    setKpi('spot-price-current', Number.isFinite(spot.current_price_ct_per_kwh)
      ? `${spot.current_price_ct_per_kwh.toFixed(2)} ct/kWh` : stateText(spot.current_status),
      `${spot.bidding_zone} · ${spot.currency}${currentUpdate ? ` · Fetched ${currentUpdate}` : ''}${spot.current_status === 'stale' && Number.isFinite(spot.current_price_ct_per_kwh) ? ' · Refresh delayed' : ''}`);
    setKpi('spot-market-status', spot.status === 'available' ? 'Available' : stateText(spot.status),
      `${spotUpdate ? `${spot.source_generated_at ? 'Source updated' : 'Fetched'} ${spotUpdate} · ` : ''}${spot.intervals_available} future intervals · Source: Energy-Charts.info`);
    void renderChart('spot-price-curve', spot.future_curve, 'price_ct_per_kwh', 'ct/kWh', spot.status, timeZone, 'Energy-Charts.info')
      .catch((error) => { destroy('spot-price-curve'); node('spot-price-curve-state').textContent = error.message; });
  }

  function renderSolar(solar) {
    const source = solar.source || 'Open-Meteo';
    // Label times in the site's timezone when the projection carries one, falling back to UTC.
    const timeZone = solar.timezone || 'UTC';
    // The KPI headline is the forecast energy of the next 24 hours; a disabled/error/unavailable
    // state shows a word, never an invented figure. energy_next_24h_wh is in Wh -> kWh.
    const energyKwh = Number.isFinite(solar.energy_next_24h_wh) ? solar.energy_next_24h_wh / 1000 : null;
    const available = solar.status === 'available' || solar.status === 'stale';
    const covered = formattedTime(solar.covered_until, timeZone);
    setKpi('solar-forecast-status',
      energyKwh !== null ? `${energyKwh.toFixed(1)} kWh` : stateText(solar.status),
      `${available ? (solar.status === 'stale' ? 'Stale' : 'Available') : stateText(solar.status)}${covered ? ` · Until ${covered}` : ''} · Source: ${source}`);
    // The forecast curve is average PV power per interval, in kW; gaps stay gaps and a stale
    // last-known-good curve is still drawn and marked stale.
    void renderChart('pv-forecast-curve', solar.future_curve, 'power_kw', 'kW', solar.status, timeZone, source)
      .catch((error) => { destroy('pv-forecast-curve'); node('pv-forecast-curve-state').textContent = error.message; });
  }

  function render(data) {
    // Spot and solar are independent: render each only when its projection is present, so a
    // solar-only dashboard works without data.spot and vice versa.
    if (data?.spot) renderSpot(data.spot);
    if (data?.solar) renderSolar(data.solar);
  }

  function requestedParts() {
    const parts = [];
    if (visible('spot-price-current')) parts.push('spot_current');
    if (visible('spot-market-status') || visible('spot-price-curve')) parts.push('spot_day_ahead');
    if (visible('solar-forecast-status') || visible('pv-forecast-curve')) parts.push('solar');
    return parts;
  }

  async function refresh() {
    const parts = requestedParts();
    if (!parts.length || controller) return;
    controller = new AbortController();
    selection = parts.join(',');
    const thisGeneration = ++generation;
    try {
      const query = new URLSearchParams();
      for (const part of parts) query.append('include', part);
      const data = await window.RCTAdmin.api(`energy/dashboard-forecast?${query}`, { signal: controller.signal, background: true });
      if (generation === thisGeneration && ids.some(visible)) {
        render(data);
        // Retry only the parts the server actually reported as still spooling up. Each projection
        // is probed independently so a missing one never throws.
        const retryable =
          (parts.includes('spot_day_ahead') && data.spot && data.spot.status === 'unavailable') ||
          (parts.includes('spot_current') && data.spot && data.spot.current_status === 'unavailable') ||
          (parts.includes('solar') && data.solar && data.solar.status === 'unavailable');
        if (retryable) setTimeout(() => { if (ids.some(visible)) void refresh(); }, 2000);
      }
    } catch (error) {
      if (generation !== thisGeneration) return;
      if (error?.rateLimited) { // keep what is drawn and retry once the rate-limit cooldown ended
        setTimeout(() => { if (!document.hidden && ids.some(visible)) void refresh(); }, window.RCTAdmin.pollDelay(5000));
        return;
      }
      const message = window.RCTAdmin.messageFrom(error);
      for (const id of ids.filter(visible)) {
        if (chartIds.includes(id)) { destroy(id); node(`${id}-state`).textContent = message; }
        else setKpi(id, 'Data unavailable', message);
      }
    } finally {
      if (generation === thisGeneration) controller = null;
    }
  }

  function sync() {
    window.RCTDashboardChargingSchedule?.sync();
    window.RCTDashboardBatteryHealth?.sync();
    for (const id of chartIds) if (!visible(id)) destroy(id);
    if (controller && requestedParts().join(',') !== selection) {
      controller.abort(); controller = null; generation++;
    }
    if (!ids.some(visible)) return;
    void refresh();
  }

  window.RCTDashboardForecast = Object.freeze({
    sync, chartLibrary: ensureChartLibrary,
    resize: () => { charts.forEach((chart) => chart.resize()); window.RCTDashboardChargingSchedule?.resize(); window.RCTDashboardBatteryHealth?.resize(); },
  });
  setInterval(() => { if (!document.hidden && ids.some(visible)) void refresh(); }, 5 * 60 * 1000);
  sync();
})();
