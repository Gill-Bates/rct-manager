//
// app/admin/static/js/admin-spot-market.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

// Tab <-> URL fragment. Bootstrap owns the show/hide and the keyboard roving tabindex; this only
// opens the tab named by location.hash on load (defaulting to Inverter) and writes the active
// tab back to the fragment, so a reload or a shared /ui/energy#spot-market link reopens it.
const ENERGY_TABS = ['inverter', 'plan', 'spot-market', 'weather'];

function initEnergyTabs(onShown = null) {
  const container = $('energy-tabs');
  if (!container || !window.bootstrap) return;
  const fromHash = (location.hash || '').replace(/^#/, '');
  const initial = ENERGY_TABS.includes(fromHash) ? fromHash : 'inverter';
  // Scrolls only the tab bar (not the page) so the active tab is never hidden in the swipe row.
  const revealTab = (button) => {
    const offset = button.getBoundingClientRect().left - container.getBoundingClientRect().left + container.scrollLeft;
    const left = offset - (container.clientWidth - button.offsetWidth) / 2;
    container.scrollTo({ left: Math.max(0, left), behavior: 'auto' });
  };
  for (const button of container.querySelectorAll('[data-bs-toggle="tab"]')) {
    button.addEventListener('shown.bs.tab', () => {
      revealTab(button);
      const name = button.dataset.tab;
      // replaceState, not a navigation: changing the fragment must not add history entries or
      // trigger the browser's own hashchange scroll.
      history.replaceState(null, '', `#${name}`);
      onShown?.(name);
    });
  }
  if (initial !== 'inverter') {
    const target = container.querySelector(`[data-tab="${initial}"]`);
    if (target) window.bootstrap.Tab.getOrCreateInstance(target).show();
  } else {
    onShown?.('inverter');
  }
}

const SPOT_MARKET_PROVIDERS = [['energy_charts', 'Fraunhofer Energy-Charts']];
// The fee region concept stays in the backend model, but it is no longer operator-editable in the
// UI. The last loaded config's fee_region is kept here and sent back unchanged on save, so a save
// never wipes a stored value just because the form no longer renders a field for it.
let spotMarketFeeRegion = { code: '', label: '' };
// The last bidding-zone listing fetched for the form, reused when re-rendering after a save so the
// select keeps its options without another metadata fetch.
let spotMarketZoneInfo = null;
// The preview reads the rolling database window on load and after parameter changes. These
// supersede an in-flight request so a newer change always wins: the controller aborts the stale
// request and the generation guard discards a stale response that still arrives.
let spotMarketPreviewController = null;
let spotMarketPreviewGeneration = 0;
let spotMarketPreviewTimer = null;
let spotMarketPreviewRetryTimer = null;
let spotMarketPreviewRetryMs = 5_000;
const SPOT_MARKET_PREVIEW_DEBOUNCE_MS = 350;
const SPOT_MARKET_PREVIEW_MAX_RETRY_MS = 60_000;
let spotMarketPreviewChart = null;
let spotMarketSaving = false;
let spotMarketZoneRenderPending = false;

// The status badge lives in the card's title row, outside the re-rendered preview. It is only
// replaced when its class or text changed, so an unchanged status never rebuilds or shifts it;
// a null class clears it (a state without a status, e.g. unsaved weather edits).
function setPreviewStatus(slotId, badgeClass, text) {
  const slot = $(slotId);
  if (!slot) return;
  if (!badgeClass) {
    slot.replaceChildren();
    return;
  }
  const current = slot.firstChild;
  if (current && current.className === `status-badge ${badgeClass}` && current.textContent === text) return;
  slot.replaceChildren(element('span', `status-badge ${badgeClass}`, text));
}

function spotMarketLoadingState() {
  const loading = element('div', 'spot-market-loading');
  loading.setAttribute('role', 'status');
  const spinner = element('span', 'spinner-border text-secondary');
  spinner.setAttribute('aria-hidden', 'true');
  loading.append(spinner, element('span', 'badge rounded-pill text-bg-secondary', 'Loading day-ahead preview …'));
  return loading;
}

function spotMarketControl(field, value) {
  // Read-only provider metadata is not an editable field: render it as a compact info row with a
  // lock icon, so it never reads as something the operator can change.
  if (field.type === 'static') {
    const row = element('div', 'setting-row spot-market-meta-row');
    const lock = element('span', 'material-icons spot-market-meta-icon', 'lock');
    lock.setAttribute('aria-hidden', 'true');
    const name = element('span', 'spot-market-meta-label', field.label);
    const val = element('span', 'spot-market-meta-value', String(value ?? '—'));
    const help = element('div', 'form-text', field.help);
    const line = element('div', 'spot-market-meta-line');
    line.append(lock, name, val);
    row.append(line, help);
    return row;
  }
  const wrap = element('div', 'setting-row');
  const label = element('label', field.type === 'toggle' ? 'form-check-label' : 'form-label', field.label);
  const id = `spot-market-${field.key}`;
  label.htmlFor = id;
  let control;
  if (field.type === 'select') {
    control = element('select', 'form-select');
    for (const [optionValue, text] of field.options) {
      const item = element('option', '', text);
      item.value = optionValue;
      control.append(item);
    }
    // The saved value may not be in the current option list (a provider dropped a zone, or the
    // metadata is stale); keep it as an explicit option so the operator never silently loses it.
    const stored = value == null ? '' : String(value);
    if (stored && !field.options.some(([v]) => v === stored)) {
      // Without any list (provider down, nothing stored) the saved zone is simply the value.
      const kept = element('option', '', field.options.length ? `${stored} (saved)` : stored);
      kept.value = stored;
      control.append(kept);
    }
    control.value = stored || String(field.options[0]?.[0] ?? '');
  } else {
    control = element('input', field.type === 'toggle' ? 'form-check-input' : 'form-control');
    control.type = field.type === 'toggle' ? 'checkbox' : 'text';
    if (field.type === 'toggle') { control.setAttribute('role', 'switch'); control.checked = Boolean(value); }
    else control.value = String(value ?? '');
  }
  control.id = id;
  control.dataset.field = field.key;
  const help = element('div', 'form-text', field.help);
  help.id = `${id}-help`;
  control.setAttribute('aria-describedby', help.id);
  if (field.type === 'toggle') {
    const check = element('div', 'form-check form-switch d-flex align-items-center gap-2');
    check.append(control, label);
    wrap.append(check, help);
  } else wrap.append(label, control, help);
  return wrap;
}

// The form fields of the Spot Market tab, built with the bidding zones fetched from provider
// metadata. Currency and price unit remain in the preview's read-only context line; the fee region
// is not rendered (kept in the model only).
function spotMarketFields(config, zoneInfo) {
  const zoneOptions = (zoneInfo?.zones ?? []).map((zone) => [zone, zone]);
  const zoneHelp = 'Exchange price zone where the price is formed, e.g. DE-LU. This is not the fee region.';
  return [
    // No 'Enabled' toggle: whether day-ahead prices are fetched follows the Energy Manager mode
    // (manual fetches; off/external do not). Prices are input data only and never command the battery.
    { key: 'provider', label: 'Provider', type: 'select', options: SPOT_MARKET_PROVIDERS, value: config.provider, help: 'The external day-ahead price source. Each provider has its own fixed, trusted endpoint; there is no editable URL.' },
    { key: 'bidding_zone', label: 'Bidding Zone / Market Region', type: 'select', options: zoneOptions, value: config.bidding_zone, help: zoneHelp },
  ];
}

// Build the body a save/preview posts from the current form controls. The fee region is kept out
// of the form but carried through unchanged, so the model concept and any stored value survive.
function spotMarketBody() {
  const container = $('spot-market-fields');
  const read = (key) => container.querySelector(`[data-field="${key}"]`);
  // No 'enabled' field: the server derives whether prices are fetched from the Energy Manager mode.
  return {
    provider: read('provider').value,
    bidding_zone: read('bidding_zone').value.trim(),
    fee_region: { code: spotMarketFeeRegion.code, label: spotMarketFeeRegion.label },
  };
}

function renderSpotMarket(config, zoneInfo) {
  const container = $('spot-market-fields');
  if (!container) return;
  spotMarketFeeRegion = { code: config.fee_region?.code ?? '', label: config.fee_region?.label ?? '' };
  if (zoneInfo !== undefined) spotMarketZoneInfo = zoneInfo;
  container.replaceChildren(
    ...spotMarketFields(config, spotMarketZoneInfo).map((field) => spotMarketControl(field, field.value)),
  );
}

async function initSpotMarket() {
  const form = $('spot-market-form');
  if (!form) return;
  try {
    // Render from the saved config immediately. The zone catalog is independent metadata and may
    // need a provider refresh; load it in the background so it cannot delay the DB-backed graph.
    const config = await api('energy/spot-market/config');
    renderSpotMarket(config);
  } catch (error) {
    $('spot-market-fields').replaceChildren(element('p', 'text-danger', messageFrom(error)));
    return;
  }
  void api('energy/spot-market/bidding-zones').then((zoneInfo) => {
    if (!zoneInfo) return;
    spotMarketZoneInfo = zoneInfo;
    if (spotMarketSaving) {
      spotMarketZoneRenderPending = true;
      return;
    }
    const current = spotMarketBody();
    renderSpotMarket({ ...current, fee_region: spotMarketFeeRegion }, zoneInfo);
  }).catch(() => { });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    saveSpotMarket().catch((error) => toast(messageFrom(error), 'danger'));
  });
  // A changed provider or bidding zone re-queries the day-ahead preview. The listener is delegated
  // on the stable fields container, so it survives the control rebuild in renderSpotMarket without
  // re-binding. Delegation also covers only the operator-editable parameters; the read-only
  // metadata rows carry no form control to emit a change.
  const fields = $('spot-market-fields');
  const previewFields = new Set(['provider', 'bidding_zone']);
  fields.addEventListener('change', (event) => {
    const field = event.target instanceof Element ? event.target.dataset.field : null;
    if (field && previewFields.has(field)) scheduleSpotMarketPreview();
  });
  // One automatic read once the form and bidding-zone options are present; later reads are driven
  // after a parameter change, never by a re-render; the background worker owns provider polling.
  scheduleSpotMarketPreview({ immediate: true });
  window.setInterval(() => { if (!document.hidden) scheduleSpotMarketPreview({ immediate: true, resetRetry: false }); }, 60_000);
}

// Debounce a burst of parameter changes into one preview request, then run it. ``immediate``
// skips the debounce for the single on-load read.
function scheduleSpotMarketPreview({ immediate = false, resetRetry = true } = {}) {
  clearTimeout(spotMarketPreviewTimer);
  clearTimeout(spotMarketPreviewRetryTimer);
  spotMarketPreviewRetryTimer = null;
  if (resetRetry) spotMarketPreviewRetryMs = 5_000;
  const run = () => previewSpotMarket({ background: !resetRetry }).catch((error) => toast(messageFrom(error), 'danger'));
  if (immediate) { run(); return; }
  spotMarketPreviewTimer = setTimeout(run, SPOT_MARKET_PREVIEW_DEBOUNCE_MS);
}

async function saveSpotMarket() {
  const button = $('spot-market-save');
  const state = $('spot-market-save-state');
  button.disabled = true;
  spotMarketSaving = true;
  state.textContent = 'Saving …';
  // Lock the whole form, not just the save button, for the save duration: a slow PUT must not let
  // the operator keep editing and then have the server re-render stomp their newer input. The
  // success path rebuilds fresh, enabled controls via renderSpotMarket, so re-enabling here only
  // matters on the error path.
  const locked = lockFormControls([$('spot-market-fields')]);
  let savedSuccessfully = false;
  try {
    const saved = await api('energy/spot-market/config', { method: 'PUT', body: JSON.stringify(spotMarketBody()) });
    renderSpotMarket(saved);
    savedSuccessfully = true;
    state.textContent = 'Saved';
    toast('Spot-market configuration saved.');
    scheduleSpotMarketPreview({ immediate: true });
  } catch (error) {
    state.textContent = '';
    throw error;
  } finally {
    button.disabled = false;
    unlockFormControls(locked);
    spotMarketSaving = false;
    if (spotMarketZoneRenderPending) {
      spotMarketZoneRenderPending = false;
      if (!savedSuccessfully) {
        const current = spotMarketBody();
        renderSpotMarket({ ...current, fee_region: spotMarketFeeRegion }, spotMarketZoneInfo);
      }
    }
  }
}

// Prices leave the backend in EUR/kWh; the GUI shows the far more legible ct/kWh (×100). The
// internal unit is unchanged — this is a display conversion only.
function centsPerKwh(eurPerKwh) {
  if (eurPerKwh == null) return null;
  return `${(eurPerKwh * 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ct/kWh`;
}

// Date/time use the browser locale (no fixed locale argument), so the GUI follows the user's
// regional format.
function spotMarketShortTime(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

function spotMarketDateTime(iso, timeZone) {
  const date = new Date(iso);
  const zone = timeZone ? { timeZone } : {};
  return `${date.toLocaleDateString(undefined, { ...zone, day: '2-digit', month: 'short' })} · ${date.toLocaleTimeString(undefined, { ...zone, hour: '2-digit', minute: '2-digit' })}`;
}

// One highlight block of the preview: a leading KPI icon (mirroring the dashboard KPI tiles), a
// caption, the ct/kWh figure, and when it occurs. The icon is passed in rather than hard-coded so
// Lowest/Highest can carry visually distinct glyphs; it is decorative (the caption names the tile).
function spotMarketHighlight(caption, priceEurPerKwh, atIso, icon, colorClass, timeZone) {
  const block = element('div', 'spot-market-highlight');
  const glyph = element('span', `material-icons spot-market-highlight-icon ${colorClass}`, icon);
  glyph.setAttribute('aria-hidden', 'true');
  block.append(glyph);
  block.append(element('span', 'spot-market-highlight-caption', caption));
  block.append(element('span', 'spot-market-highlight-value', centsPerKwh(priceEurPerKwh) ?? 'n/a'));
  block.append(element('span', 'spot-market-highlight-when', atIso ? spotMarketDateTime(atIso, timeZone) : 'n/a'));
  return block;
}

// One point per interval in the backend's own order. Identity is ordinal (the interval's position
// in the day), not its local HH:MM: on a DST fall-back day the duplicated wall-clock hour (local
// 02:00 twice) now keeps BOTH intervals instead of the second overwriting the first, and short
// (92) or long (100) days keep their real count. Each point carries its own local HH:MM label,
// derived from the interval's absolute start, for ticks and tooltips. Validation is unchanged and
// stays all-or-nothing (any bad interval returns null).
function spotMarketChartPoints(intervals, timeZone) {
  if (!Array.isArray(intervals) || intervals.length > 100) return null;
  // The formatted time is derived from the interval's absolute start, so a fall-back day's two
  // 02:00 slots read correctly even though they share a wall-clock string. formatToParts with h23
  // plus zero-padding yields 24-hour HH:MM regardless of the browser locale.
  const timeFormatter = new Intl.DateTimeFormat(undefined, {
    timeZone: timeZone || 'UTC', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const hhmm = (instant) => {
    const parts = timeFormatter.formatToParts(instant);
    const field = (type) => (parts.find((part) => part.type === type)?.value || '').padStart(2, '0');
    return `${field('hour')}:${field('minute')}`;
  };
  const points = [];
  for (const interval of intervals) {
    const start = Date.parse(interval?.start);
    const end = Date.parse(interval?.end);
    const price = interval?.price_eur_per_kwh;
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || !Number.isFinite(price)) return null;
    points.push({ y: price * 100, label: hhmm(start) });
  }
  return points;
}

function syncSpotMarketChartTheme() {
  if (!spotMarketPreviewChart) return;
  const styles = getComputedStyle($('spot-market-preview'));
  const labelColor = styles.getPropertyValue('--bs-body-color').trim();
  const gridColor = styles.getPropertyValue('--rct-line').trim();
  spotMarketPreviewChart.options.plugins.legend.labels.color = labelColor;
  for (const axis of Object.values(spotMarketPreviewChart.options.scales)) {
    axis.ticks.color = labelColor;
    axis.grid.color = gridColor;
    if (axis.title) axis.title.color = labelColor;
  }
  spotMarketPreviewChart.update('none');
}

function drawSpotMarketChart(result) {
  spotMarketPreviewChart?.destroy();
  spotMarketPreviewChart = null;
  const canvas = $('spot-market-chart');
  if (!canvas) return;
  if (!window.Chart) {
    canvas.parentElement.replaceChildren(element('p', 'text-danger small', 'Chart library could not be loaded.'));
    return;
  }
  const yesterday = spotMarketChartPoints(result.yesterday_intervals, result.timezone);
  const today = spotMarketChartPoints(result.today_intervals, result.timezone);
  const tomorrow = spotMarketChartPoints(result.next_day_intervals, result.timezone);
  if (!yesterday || !today || !tomorrow ||
    ![yesterday, today, tomorrow].some((day) => day.length > 0)) {
    canvas.parentElement.replaceChildren(element('p', 'text-danger small', 'Price series is invalid or empty.'));
    return;
  }
  // Slots are keyed by local time plus occurrence (see buildDayAxis), so DST days stay aligned.
  const days = [yesterday, today, tomorrow];
  const { labels, slots } = buildDayAxis(days);
  const maxLen = labels.length;
  const align = (day, dayIndex) => {
    const data = Array(maxLen).fill(null);
    day.forEach((point, index) => { data[slots[dayIndex][index]] = point.y; });
    return { data };
  };
  const styles = getComputedStyle($('spot-market-preview'));
  const labelColor = styles.getPropertyValue('--bs-body-color').trim();
  const gridColor = styles.getPropertyValue('--rct-line').trim();
  const series = [
    { label: 'Yesterday', ...align(yesterday, 0), borderColor: styles.getPropertyValue('--spot-market-yesterday-line').trim() },
    { label: 'Today', ...align(today, 1), borderColor: styles.getPropertyValue('--spot-market-today-line').trim(), borderDash: [] },
    { label: 'Tomorrow', ...align(tomorrow, 2), borderColor: styles.getPropertyValue('--spot-market-tomorrow-line').trim(), borderDash: [6, 4] },
  ];
  spotMarketPreviewChart = new window.Chart(canvas, {
    type: 'line',
    data: {
      labels, datasets: series.map(({ label, data, borderColor, borderDash }) => ({
        label, data, borderColor, borderDash, backgroundColor: borderColor,
        stepped: 'after', pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: false,
      }))
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      // Nearest time slot opens the tooltip; the pointer need not hit the line or a hidden point.
      interaction: { mode: 'index', intersect: false, axis: 'x' },
      plugins: {
        legend: { display: true, labels: { color: labelColor, usePointStyle: false } },
        tooltip: {
          // Hide series without a value in this slot (null/NaN) instead of inventing one.
          filter: (item) => Number.isFinite(item.parsed?.y),
          callbacks: {
            title: (items) => labels[items[0].dataIndex],
            label: (item) => `${item.dataset.label}: ${item.parsed.y.toLocaleString(undefined, { maximumFractionDigits: 2 })} ct/kWh`,
          }
        },
      },
      scales: {
        x: {
          type: 'category', grid: { color: gridColor }, ticks: {
            color: labelColor, autoSkip: true, maxTicksLimit: 7, minRotation: 0, maxRotation: 0,
          }
        },
        y: {
          grid: { color: gridColor }, ticks: { color: labelColor },
          title: { display: true, text: 'ct/kWh', color: labelColor }
        },
      },
    },
  });
}

// A compact status line (badge + context) followed by the day-ahead highlights. Replaces the old
// debug description list; provider and zone appear only here, not as equal-weight rows.
// Shown only when the daily retrieval failed and due data is missing (result.stale). The technical
// refresh error stays out of the text; it is available as a tooltip for administrators.
function refreshFailureNotice(message, failedAt) {
  const notice = element('p', 'spot-market-notice-warning small mb-0');
  const icon = element('span', 'material-icons spot-market-notice-icon', 'warning');
  icon.setAttribute('aria-hidden', 'true');
  const when = failedAt ? ` since ${spotMarketShortTime(failedAt)}` : '';
  notice.title = message;
  notice.append(icon, element('span', '', `The data could not be refreshed${when} and may be out of date. The next attempt runs automatically.`));
  return notice;
}

function renderSpotMarketPreview(result) {
  const nodes = [];
  const connected = result.status === 'connected';
  const unavailable = result.status === 'unavailable' || result.status === 'disabled';

  // The backend flags stale only when the daily retrieval failed and due data is still missing;
  // a failed retrieval behind complete data is not worth the user's attention.
  const stale = connected && result.stale === true;
  setPreviewStatus('spot-market-preview-status', stale ? 'status-badge-warning' : connected ? 'status-badge-success' : unavailable ? 'status-badge-neutral' : 'status-badge-danger',
    stale ? 'Stale' : connected ? 'Connected' : result.status === 'disabled' ? 'Disabled' : unavailable ? 'Unavailable' : 'Error');

  const contextRow = element('div', 'spot-market-preview-context');
  contextRow.append(element('span', 'spot-market-preview-context-source',
    `${providerLabel(result.provider)} · ${result.bidding_zone}`));
  nodes.push(contextRow);

  if (!connected) {
    nodes.push(element('p', `${unavailable ? 'text-secondary' : 'text-danger'} small mb-0`, result.message || 'unknown error'));
    return nodes;
  }

  // The Lowest/Highest tiles always render: they reflect whatever price data is available
  // (yesterday/today), independent of whether tomorrow is published. When no figure is available
  // the tile reads "n/a" (see spotMarketHighlight), so the block is never omitted or left blank.
  const grid = element('div', 'spot-market-highlights');
  grid.append(spotMarketHighlight('Lowest', result.min_price_eur_per_kwh, result.min_price_at, 'trending_down', 'text-success', result.timezone));
  grid.append(spotMarketHighlight('Highest', result.max_price_eur_per_kwh, result.max_price_at, 'trending_up', 'text-danger', result.timezone));
  nodes.push(grid);

  if (stale) nodes.push(refreshFailureNotice(result.refresh_error, result.refresh_error_at));

  const meta = element('p', 'spot-market-preview-meta',
    `Currency ${result.currency} · Source unit EUR/MWh · Bidding zone ${result.bidding_zone}`);
  nodes.push(meta);

  if (result.yesterday_message) {
    nodes.push(element('p', 'text-secondary small mb-0', `Yesterday's prices are unavailable: ${result.yesterday_message}`));
  }
  if (result.next_day_message) {
    // A visible warning callout, not plain muted text: a yellow warning triangle (theme-aware
    // Bootstrap warning token, set in CSS) plus the hint. The icon is decorative; the meaning is
    // carried by the text, so screen-reader users do not rely on color.
    const notice = element('p', 'spot-market-notice-warning small mb-0');
    const icon = element('span', 'material-icons spot-market-notice-icon', 'warning');
    icon.setAttribute('aria-hidden', 'true');
    notice.append(icon, element('span', '', `Tomorrow's prices are not available yet${result.next_day_message ? `: ${result.next_day_message}` : ''}`));
    nodes.push(notice);
  }

  const chartBox = element('div', 'spot-market-chart-box');
  const canvas = element('canvas', '');
  canvas.id = 'spot-market-chart';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Day-ahead prices for yesterday in grey, today in green, and tomorrow in blue. Each line can be toggled in the legend.');
  chartBox.append(canvas);
  nodes.push(chartBox);

  return nodes;
}

// Fetch and render the day-ahead preview. Button-independent: the loading, success, and error
// states live only in the preview target. A newer call supersedes an in-flight one — it aborts
// the stale request and a generation guard discards a stale response that still arrives — so the
// rendered preview always reflects the latest parameter values.
async function previewSpotMarket({ background = false } = {}) {
  const target = $('spot-market-preview');
  if (!target) return;
  if (spotMarketPreviewController) {
    spotMarketPreviewController.abort(Object.assign(new Error('Superseded by a newer request.'), { name: 'AbortError' }));
  }
  const controller = new AbortController();
  const generation = ++spotMarketPreviewGeneration;
  spotMarketPreviewController = controller;
  spotMarketPreviewChart?.destroy();
  spotMarketPreviewChart = null;
  target.replaceChildren(spotMarketLoadingState());
  try {
    const result = await api('energy/spot-market/preview', { method: 'POST', body: JSON.stringify(spotMarketBody()), signal: controller.signal, background });
    if (generation !== spotMarketPreviewGeneration) return;
    target.replaceChildren(...renderSpotMarketPreview(result));
    if (result.status === 'connected') drawSpotMarketChart(result);
    if (result.status === 'unavailable' || result.status === 'error') {
      const delay = spotMarketPreviewRetryMs;
      spotMarketPreviewRetryMs = Math.min(delay * 2, SPOT_MARKET_PREVIEW_MAX_RETRY_MS);
      spotMarketPreviewRetryTimer = setTimeout(() => scheduleSpotMarketPreview({ immediate: true, resetRetry: false }), delay);
    } else {
      spotMarketPreviewRetryMs = 5_000;
    }
  } catch (error) {
    // A superseded request's abort is expected; the newer request owns the target, so leave its
    // loading state in place rather than overwriting it with this stale error.
    if (generation !== spotMarketPreviewGeneration || error.name === 'AbortError') return;
    if (error.rateLimited) { // short-lived load shedding: retry after Retry-After instead of showing an error
      spotMarketPreviewRetryTimer = setTimeout(() => scheduleSpotMarketPreview({ immediate: true, resetRetry: false }), pollDelay(spotMarketPreviewRetryMs));
      return;
    }
    setPreviewStatus('spot-market-preview-status', null);
    target.replaceChildren(element('p', 'text-danger small mb-0', messageFrom(error)));
    throw error;
  } finally {
    if (generation === spotMarketPreviewGeneration) spotMarketPreviewController = null;
  }
}

// --- Weather / solar forecast (Open-Meteo) ------------------------------------------------------
