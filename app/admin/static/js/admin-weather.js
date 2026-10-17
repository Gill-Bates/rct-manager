//
// app/admin/static/js/admin-weather.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

// Mirrors the Spot Market tab: a config form plus a live preview that supersede an in-flight
// request. The forecast is input data only and never triggers an inverter action.
const WEATHER_PROVIDERS = [['open_meteo', 'Open-Meteo']];
let weatherProviderMeta = null;
let weatherPreviewController = null;
let weatherPreviewGeneration = 0;
let weatherPreviewTimer = null;
let weatherPreviewRetryTimer = null;
let weatherPreviewRetryMs = 5_000;
const WEATHER_PREVIEW_DEBOUNCE_MS = 350;
const WEATHER_PREVIEW_MAX_RETRY_MS = 60_000;
let weatherPreviewChart = null;
// Signature of the last rendered preview: a poll or retry that returns the same payload must not
// rebuild the card or the chart.
let weatherPreviewRendered = null;
// location_name and location_region are descriptive selection state. Timezone is display-only server state: every
// save/preview derives it from latitude/longitude, never from browser input.
let weatherLocationName = null;
// Region and country as one structured value from the chosen hit (never split out of the name).
let weatherLocationRegion = null;
let weatherTimezone = null;
// Timezone autodetection: the zone is always derived from the coordinates (never typed), through
// the admin timezone endpoint. A debounce coalesces a burst of manual lat/lon edits, and a
// generation counter discards a stale response so only the latest coordinates set the field.
let weatherTimezoneController = null;
let weatherTimezoneGeneration = 0;
let weatherTimezoneTimer = null;
const WEATHER_TIMEZONE_DEBOUNCE_MS = 500;
// The location autocomplete aborts a superseded request and discards a stale response, exactly
// like the preview, so the dropdown always reflects the latest query. The debounce coalesces a
// burst of keystrokes into one geocoding request against the admin locations endpoint.
let weatherLocationController = null;
let weatherLocationGeneration = 0;
let weatherLocationTimer = null;
const WEATHER_LOCATION_DEBOUNCE_MS = 300;
const WEATHER_LOCATION_MIN_QUERY = 2;
// One AbortController owns every listener a render binds (the outside-click close), recreated per
// render so a re-render never stacks a second global handler pinning stale result nodes.
let weatherLocationListeners = null;
// The keyboard-highlighted option index into the current hit list (-1 = none), plus the hits so
// Enter can select without a mouse.
let weatherLocationActive = -1;
let weatherLocationHits = [];

// One weather form control. Numbers carry their unit, min/max and step so the browser validates
// and the operator sees the expected range; the azimuth help spells out the Open-Meteo convention.
function weatherControl(field, value) {
  const wrap = element('div', 'setting-row');
  const id = `weather-${field.key}`;
  const label = element('label', field.type === 'toggle' ? 'form-check-label' : 'form-label', field.label);
  label.htmlFor = id;
  let control;
  if (field.type === 'select') {
    control = element('select', 'form-select');
    for (const [optionValue, text] of field.options) {
      const item = element('option', '', text);
      item.value = optionValue;
      control.append(item);
    }
    control.value = value == null ? String(field.options[0]?.[0] ?? '') : String(value);
  } else if (field.type === 'toggle') {
    control = element('input', 'form-check-input');
    control.type = 'checkbox';
    control.setAttribute('role', 'switch');
    control.checked = Boolean(value);
  } else {
    control = element('input', 'form-control');
    control.type = 'number';
    control.inputMode = 'decimal';
    if (field.min !== undefined) control.min = String(field.min);
    if (field.max !== undefined) control.max = String(field.max);
    if (field.step !== undefined) control.step = String(field.step);
    control.value = value == null ? '' : String(value);
  }
  control.id = id;
  control.dataset.field = field.key;
  if (field.type === 'toggle') {
    const check = element('div', 'form-check form-switch d-flex align-items-center gap-2');
    check.append(control, label);
    wrap.append(check);
  } else wrap.append(label, control);
  return wrap;
}

function weatherFields(config) {
  return [
    { key: 'enabled', label: 'Enable solar forecast', type: 'toggle', value: config.enabled },
    { key: 'provider', label: 'Source', type: 'select', options: WEATHER_PROVIDERS, value: config.provider },
    { key: 'latitude', label: 'Latitude (°)', type: 'number', min: -90, max: 90, step: 'any', value: config.latitude },
    { key: 'longitude', label: 'Longitude (°)', type: 'number', min: -180, max: 180, step: 'any', value: config.longitude },
    // The UI uses kWp (the sharpened spec: `[10.0] kWp`); the domain stays in Wp. The field key
    // is pv_peak_power_kwp so weatherBody() knows to convert *1000 before sending.
    { key: 'pv_peak_power_kwp', label: 'PV peak power (kWp)', type: 'number', min: 0.001, step: 'any', value: config.pv_peak_power_wp == null ? null : config.pv_peak_power_wp / 1000 },
    { key: 'tilt_deg', label: 'Panel tilt (°)', type: 'number', min: 0, max: 90, step: 'any', value: config.tilt_deg },
    { key: 'azimuth_deg', label: 'Panel azimuth (°)', type: 'number', min: -180, max: 180, step: 'any', value: config.azimuth_deg },
    { key: 'performance_ratio', label: 'Performance ratio', type: 'number', min: 0.01, max: 1, step: 'any', value: config.performance_ratio },
  ];
}

// Build the body a save/preview posts. Empty number fields become null so a disabled, incomplete
// configuration can still be saved; the backend enforces completeness only when enabled.
function weatherContainers() {
  return [$('weather-fields'), $('weather-provider-fields'), $('weather-pv-fields')].filter(Boolean);
}

function weatherControlByField(key) {
  for (const container of weatherContainers()) {
    const control = container.querySelector(`[data-field="${key}"]`);
    if (control) return control;
  }
  return null;
}

function weatherBody() {
  const read = (key) => weatherControlByField(key);
  const num = (key) => {
    const raw = (read(key)?.value || '').trim();
    return raw === '' ? null : Number(raw);
  };
  // The UI collects kWp; the domain stores Wp. Timezone is deliberately absent: the server
  // derives it from latitude/longitude for every preview and save.
  const kwp = num('pv_peak_power_kwp');
  return {
    enabled: read('enabled').checked,
    provider: read('provider').value,
    location_name: weatherLocationName || null,
    location_region: weatherLocationRegion || null,
    latitude: num('latitude'),
    longitude: num('longitude'),
    pv_peak_power_wp: kwp == null ? null : kwp * 1000,
    tilt_deg: num('tilt_deg'),
    azimuth_deg: num('azimuth_deg'),
    performance_ratio: num('performance_ratio'),
  };
}

// A required empty ratio or browser-invalid number must not trigger a preview request.
// Native validity checks cover transient edits without duplicating field constraints.
function canPreviewWeather() {
  for (const container of weatherContainers()) {
    for (const control of container.querySelectorAll('[data-field]')) {
      if (control.type !== 'number') continue;
      const raw = (control.value || '').trim();
      // performance_ratio is a non-nullable backend field: an empty value posts as null and 422s
      // before the handler, so an empty required field is not preview-eligible.
      if (raw === '' && control.dataset.field === 'performance_ratio') return false;
      if (raw !== '' && typeof control.checkValidity === 'function' && !control.checkValidity()) return false;
    }
  }
  return true;
}

// A section heading inside the Solar forecast card, matching the uppercase group labels of the
// settings page without introducing a new style.
function weatherSectionHeading(text) {
  return element('h3', 'text-secondary small text-uppercase fw-semibold mt-3 mb-2', text);
}

// The read-only "Selected location" label plus a live region for the autocomplete results. The
// search input sits above it; selecting a hit fills coordinates and timezone and updates the
// label. Nothing here is a persisted control: location_name/timezone are carried as form state.
function weatherLocationSection() {
  const section = document.createDocumentFragment();
  section.append(weatherSectionHeading('Location'));

  const searchRow = element('div', 'setting-row weather-location');
  const searchLabel = element('label', 'form-label', 'Search location');
  const searchId = 'weather-location-search';
  const resultsId = 'weather-location-results';
  searchLabel.htmlFor = searchId;
  const search = element('input', 'form-control');
  search.type = 'search';
  search.id = searchId;
  search.autocomplete = 'off';
  search.setAttribute('placeholder', 'Search city or postal code…');
  // Combobox semantics so the dropdown is operable and announced without a mouse: the input owns
  // the listbox, exposes its expanded state and points at the active option via aria-activedescendant.
  search.setAttribute('role', 'combobox');
  search.setAttribute('aria-autocomplete', 'list');
  search.setAttribute('aria-expanded', 'false');
  search.setAttribute('aria-controls', resultsId);
  const results = element('div', 'list-group weather-location-results');
  results.id = resultsId;
  results.setAttribute('role', 'listbox');
  results.hidden = true;
  // A separate polite live region announces transient search states ("Searching …", "No matching
  // place found", provider errors). The listbox itself then only ever contains real option
  // elements, so assistive tech is not asked to treat a status message as a selectable option.
  const status = element('div', 'form-text weather-location-status');
  status.id = 'weather-location-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.hidden = true;
  searchRow.append(searchLabel, search, results, status);
  section.append(searchRow);

  return { section, search, results };
}

function renderWeather(config) {
  const siteContainer = $('weather-fields');
  const siteSummaryContainer = $('weather-site-summary');
  const providerContainer = $('weather-provider-fields');
  const pvContainer = $('weather-pv-fields');
  if (!siteContainer || !siteSummaryContainer || !providerContainer || !pvContainer) return;
  weatherProviderMeta = config.provider_meta || weatherProviderMeta;
  weatherLocationName = config.location_name ?? null;
  weatherLocationRegion = config.location_region ?? null;
  weatherTimezone = config.timezone ?? null;
  const fields = weatherFields(config);
  const byKey = (key) => fields.find((field) => field.key === key);

  const location = weatherLocationSection();
  const siteSummary = element('div', 'spot-market-highlight');
  const siteGlyph = element('span', 'material-icons spot-market-highlight-icon text-info', 'location_on');
  siteGlyph.setAttribute('aria-hidden', 'true');
  const siteCaption = element('span', 'spot-market-highlight-caption', 'Selected location');
  const siteValue = element('span', 'spot-market-highlight-value', weatherLocationName || '—');
  siteValue.id = 'weather-selected-location';
  const siteRegion = element('span', 'spot-market-highlight-when', weatherLocationRegion || '');
  siteRegion.id = 'weather-selected-region';
  siteRegion.hidden = !weatherLocationRegion;
  siteSummary.append(siteGlyph, siteCaption, siteValue, siteRegion);

  const coordinates = element('div', 'weather-coordinate-grid');
  coordinates.append(
    weatherControl(byKey('latitude'), byKey('latitude').value),
    weatherControl(byKey('longitude'), byKey('longitude').value),
  );

  siteContainer.replaceChildren(
    weatherControl(byKey('enabled'), byKey('enabled').value),
    location.section,
    coordinates,
  );
  // The location tile and the Next 24 h tile share the Spot Market highlight grid.
  siteSummaryContainer.className = 'spot-market-highlights weather-highlights';
  const next24Slot = element('div', 'spot-market-highlight weather-next24');
  next24Slot.id = 'weather-next24';
  siteSummaryContainer.replaceChildren(siteSummary, next24Slot);
  providerContainer.replaceChildren(weatherControl(byKey('provider'), byKey('provider').value));
  pvContainer.replaceChildren(
    ...['pv_peak_power_kwp', 'tilt_deg', 'azimuth_deg', 'performance_ratio']
      .map((key) => weatherControl(byKey(key), byKey(key).value)),
  );
  bindWeatherLocationSearch(location.search, location.results);
}

// Wire the location autocomplete: a debounced query to the admin locations endpoint (never the
// geocoding provider directly), a dropdown of hits, keyboard navigation, and selection that fills
// coordinates and timezone. All listeners this render adds are owned by one AbortController so a
// re-render tears them down instead of stacking a second global handler on stale nodes.
function bindWeatherLocationSearch(search, results) {
  weatherLocationListeners?.abort();
  weatherLocationListeners = new AbortController();
  const { signal } = weatherLocationListeners;
  // On NEW input: cancel any in-flight request and bump the generation immediately so a late
  // response from the previous query can never reopen the dropdown, then schedule the debounced
  // search (which also closes the dropdown for a too-short query).
  search.addEventListener('input', () => {
    abortWeatherLocationRequest();
    // Clear the now-stale hits at once so the previous list can no longer be clicked while the
    // debounce and request for the new query are still in flight.
    weatherLocationHits = [];
    weatherLocationActive = -1;
    scheduleWeatherLocationSearch(search.value, search, results);
  }, { signal });
  search.addEventListener('keydown', (event) => handleWeatherLocationKeydown(event, search, results), { signal });
  // A click outside the search/results closes the dropdown without selecting anything.
  document.addEventListener('click', (event) => {
    if (!results.contains(event.target) && event.target !== search) hideWeatherLocationResults(search, results);
  }, { signal });
}

// Cancel the in-flight geocoding request and bump the generation so any response still arriving is
// discarded. Called on new input and when the dropdown is closed, so a stale hit never reappears.
function abortWeatherLocationRequest() {
  weatherLocationGeneration += 1;
  if (weatherLocationController) {
    weatherLocationController.abort(Object.assign(new Error('Superseded.'), { name: 'AbortError' }));
    weatherLocationController = null;
  }
}

function scheduleWeatherLocationSearch(query, search, results) {
  clearTimeout(weatherLocationTimer);
  if ((query || '').trim().length < WEATHER_LOCATION_MIN_QUERY) {
    // A too-short or cleared query closes the dropdown and leaves no pending request behind.
    hideWeatherLocationResults(search, results);
    return;
  }
  // Replace any stale hits with a visible "Searching …" state immediately, so there is never a
  // moment where the old list is still shown and clickable while the new query is pending.
  showWeatherLocationSearching(search, results);
  weatherLocationTimer = setTimeout(() => {
    searchWeatherLocations(query, search, results).catch((error) => toast(messageFrom(error), 'danger'));
  }, WEATHER_LOCATION_DEBOUNCE_MS);
}

function showWeatherLocationSearching(search, results) {
  // Clear any stale options so the old list is never shown/clickable while the new query is
  // pending, and announce the pending state through the live status region.
  results.replaceChildren();
  setWeatherLocationStatus(search, results, 'Searching …', true);
  search.removeAttribute('aria-activedescendant');
}

// Show a transient message (searching / no match / error) in the polite live region and open the
// combobox so the status is reachable; the listbox stays option-only.
// The spinner is decorative (aria-hidden) so the live region announces the text only once; every
// non-busy state and clearWeatherLocationStatus() replace the content and so remove it.
function setWeatherLocationStatus(search, results, message, busy = false) {
  const status = $('weather-location-status');
  if (status) {
    status.textContent = message;
    if (busy) {
      const spinner = element('span', 'spinner-border spinner-border-sm me-2 weather-location-spinner');
      spinner.setAttribute('aria-hidden', 'true');
      status.prepend(spinner);
    }
    status.hidden = false;
  }
  results.hidden = false;
  search.setAttribute('aria-expanded', 'true');
}

function clearWeatherLocationStatus() {
  const status = $('weather-location-status');
  if (status) {
    status.textContent = '';
    status.hidden = true;
  }
}

function hideWeatherLocationResults(search, results) {
  // Cancel a debounce timer still pending from scheduleWeatherLocationSearch(): without this a
  // search scheduled just before closing would fire afterwards, adopt the bumped generation and
  // re-open the list.
  clearTimeout(weatherLocationTimer);
  weatherLocationTimer = null;
  abortWeatherLocationRequest();
  weatherLocationHits = [];
  weatherLocationActive = -1;
  results.hidden = true;
  results.replaceChildren();
  clearWeatherLocationStatus();
  search.setAttribute('aria-expanded', 'false');
  search.removeAttribute('aria-activedescendant');
}

async function searchWeatherLocations(query, search, results) {
  const trimmed = (query || '').trim();
  if (trimmed.length < WEATHER_LOCATION_MIN_QUERY) {
    hideWeatherLocationResults(search, results);
    return;
  }
  const controller = new AbortController();
  // The generation was already bumped by abortWeatherLocationRequest() on input; take the current
  // value so a response is accepted only while it is still the latest query.
  const generation = weatherLocationGeneration;
  weatherLocationController = controller;
  try {
    const data = await api(`energy/weather/locations?q=${encodeURIComponent(trimmed)}`, { signal: controller.signal });
    if (generation !== weatherLocationGeneration) return;
    renderWeatherLocationResults(data, search, results, trimmed);
  } catch (error) {
    if (generation !== weatherLocationGeneration || error.name === 'AbortError') return;
    weatherLocationHits = [];
    weatherLocationActive = -1;
    // A thrown API/transport error goes through the same polite status region as the other
    // non-result states, so the listbox keeps holding only real option elements.
    results.replaceChildren();
    setWeatherLocationStatus(search, results, messageFrom(error));
  } finally {
    if (generation === weatherLocationGeneration) weatherLocationController = null;
  }
}

function weatherLocationLabel(result) {
  // "42899 Remscheid (District)": the provider's postcode when it resolved one, the place
  // name, and city/county/district in parentheses so two same-named places that differ only there
  // stay distinguishable. Region and country live in the separate location_region value.
  const extras = [result.city, result.admin2, result.district].filter((part, index, all) => part && part !== result.name && all.indexOf(part) === index);
  const place = `${result.name}${extras.length ? ` (${extras.join(', ')})` : ''}`;
  return result.postal_code ? `${result.postal_code} ${place}` : place;
}

function weatherLocationRegionLabel(result) {
  return [result.admin1, result.country].filter(Boolean).join(', ');
}

function showWeatherLocation() {
  const head = $('weather-selected-location');
  if (head) head.textContent = weatherLocationName || '—';
  const region = $('weather-selected-region');
  if (region) {
    region.textContent = weatherLocationRegion || '';
    region.hidden = !weatherLocationRegion;
  }
}

function renderWeatherLocationResults(data, search, results, query = '') {
  weatherLocationActive = -1;
  search.removeAttribute('aria-activedescendant');
  if (data.status === 'error') {
    weatherLocationHits = [];
    results.replaceChildren();
    setWeatherLocationStatus(search, results, data.message || 'Location search failed.');
    return;
  }
  const hits = Array.isArray(data.results) ? data.results : [];
  weatherLocationHits = hits;
  if (!hits.length) {
    results.replaceChildren();
    setWeatherLocationStatus(search, results, 'No matching place found.');
    return;
  }
  clearWeatherLocationStatus();
  const items = hits.map((hit, index) => {
    const item = element('button', 'list-group-item list-group-item-action');
    item.type = 'button';
    item.id = `weather-location-option-${index}`;
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', 'false');
    item.dataset.index = String(index);
    // Primary line: the place name, prefixed by the postcode the provider says this result is. The
    // server sets ``postal_code`` only for a postcode result or an exact postcode query.
    const postcode = typeof hit.postal_code === 'string' ? hit.postal_code.trim() : '';
    item.append(element('span', 'd-block', postcode ? `${postcode} ${hit.name}` : hit.name));
    // Secondary line: county (admin2), district, region (admin1) and country, whichever are present.
    const sub = [hit.admin2, hit.district, hit.admin1, hit.country].filter(Boolean).join(', ');
    if (sub) item.append(element('span', 'd-block text-secondary small', sub));
    item.addEventListener('click', () => selectWeatherLocation(hit, search, results));
    return item;
  });
  results.replaceChildren(...items);
  results.hidden = false;
  search.setAttribute('aria-expanded', 'true');
}

// Keyboard model for the combobox: ArrowDown/Up move the active option, Enter selects it, Escape
// closes the dropdown. aria-activedescendant tracks the highlighted option for assistive tech.
function handleWeatherLocationKeydown(event, search, results) {
  if (results.hidden || !weatherLocationHits.length) {
    if (event.key === 'Escape') hideWeatherLocationResults(search, results);
    return;
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault();
    weatherLocationActive = (weatherLocationActive + 1) % weatherLocationHits.length;
    highlightWeatherLocationOption(search, results);
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    weatherLocationActive = (weatherLocationActive - 1 + weatherLocationHits.length) % weatherLocationHits.length;
    highlightWeatherLocationOption(search, results);
  } else if (event.key === 'Enter') {
    if (weatherLocationActive >= 0) {
      event.preventDefault();
      selectWeatherLocation(weatherLocationHits[weatherLocationActive], search, results);
    }
  } else if (event.key === 'Escape') {
    event.preventDefault();
    hideWeatherLocationResults(search, results);
  }
}

function highlightWeatherLocationOption(search, results) {
  const options = [...results.querySelectorAll('[role="option"]')];
  options.forEach((option, index) => {
    const active = index === weatherLocationActive;
    option.classList.toggle('active', active);
    option.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const current = options[weatherLocationActive];
  if (current) {
    search.setAttribute('aria-activedescendant', current.id);
    current.scrollIntoView({ block: 'nearest' });
  } else {
    search.removeAttribute('aria-activedescendant');
  }
}

// Apply a chosen place: fill coordinates, derive its timezone server-side, set the descriptive
// label, close the dropdown and refresh the preview. Latitude/longitude stay editable afterwards;
// a manual coordinate edit runs the same server-side derivation.
function selectWeatherLocation(hit, search, results) {
  const set = (key, value) => {
    const control = weatherControlByField(key);
    if (control && value != null) control.value = String(value);
  };
  set('latitude', Number(hit.latitude).toFixed(4));
  set('longitude', Number(hit.longitude).toFixed(4));
  weatherLocationName = weatherLocationLabel(hit);
  weatherLocationRegion = weatherLocationRegionLabel(hit) || null;
  // The geocoder is never the timezone authority. Always derive the IANA zone server-side from
  // the selected coordinates, even if a provider happens to include its own timezone field.
  setWeatherTimezone(null);
  showWeatherLocation();
  if (search) search.value = weatherLocationName || search.value;
  hideWeatherLocationResults(search, results);
  resolveWeatherTimezone(Number(hit.latitude), Number(hit.longitude), { immediate: true });
  scheduleWeatherPreview();
}

// Derive the IANA timezone from the current coordinates through the admin timezone endpoint and
// write it into the read-only field. The operator never types the zone: this is the only writer.
// Three states are kept distinct: no/invalid coordinates clears the zone with no request and no
// error; a successful derivation fills the field; a failed derivation surfaces a toast and leaves
// the field blank rather than claiming a wrong or empty zone as a success.
function resolveWeatherTimezone(latitude, longitude, { immediate = false } = {}) {
  clearTimeout(weatherTimezoneTimer);
  weatherTimezoneTimer = null;
  // A new request supersedes any in-flight one: bump the generation and abort so a late response
  // can never overwrite a newer coordinate's zone.
  weatherTimezoneGeneration += 1;
  if (weatherTimezoneController) {
    weatherTimezoneController.abort(Object.assign(new Error('Superseded.'), { name: 'AbortError' }));
    weatherTimezoneController = null;
  }
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
    || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
    // No usable coordinates: clear the zone, no network call, no error state.
    setWeatherTimezone(null);
    return;
  }
  const run = () => fetchWeatherTimezone(latitude, longitude).catch((error) => toast(messageFrom(error), 'danger'));
  if (immediate) { run(); return; }
  weatherTimezoneTimer = setTimeout(run, WEATHER_TIMEZONE_DEBOUNCE_MS);
}

async function fetchWeatherTimezone(latitude, longitude) {
  const controller = new AbortController();
  const generation = weatherTimezoneGeneration;
  weatherTimezoneController = controller;
  try {
    const data = await api(`energy/weather/timezone?latitude=${encodeURIComponent(latitude)}&longitude=${encodeURIComponent(longitude)}`, { signal: controller.signal });
    if (generation !== weatherTimezoneGeneration) return;
    if (data.status === 'error') {
      // A technical derivation failure is a clean error state, not a silent empty/wrong zone: show
      // the cause and leave the field blank so a stale zone is never passed off as determined.
      setWeatherTimezone(null);
      toast(data.message || 'Could not determine the timezone for this location.', 'danger');
      return;
    }
    setWeatherTimezone(data.timezone ?? null);
    scheduleWeatherPreview();
  } catch (error) {
    if (generation !== weatherTimezoneGeneration || error.name === 'AbortError') return;
    setWeatherTimezone(null);
    throw error;
  } finally {
    if (generation === weatherTimezoneGeneration) weatherTimezoneController = null;
  }
}

// Write the derived zone into the form state and the read-only control; the single place the
// timezone field is ever set.
function setWeatherTimezone(timezone) {
  weatherTimezone = timezone || null;
  const value = $('weather-timezone');
  if (value) value.textContent = weatherTimezone || '—';
}

async function initWeather() {
  const form = $('weather-form');
  if (!form) return;
  try {
    renderWeather(await api('energy/weather/config'));
  } catch (error) {
    $('weather-fields').replaceChildren(element('p', 'text-danger', messageFrom(error)));
    return;
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    saveWeather().catch((error) => toast(messageFrom(error), 'danger'));
  });
  const containers = weatherContainers();
  if (!containers.length) return;
  for (const fields of containers) {
    fields.addEventListener('change', (event) => {
      clearWeatherFieldErrorFor(event.target);
      maybeResolveTimezoneFromEdit(event.target);
      scheduleWeatherPreview();
    });
    fields.addEventListener('input', (event) => {
      clearWeatherFieldErrorFor(event.target);
      if (event.target instanceof HTMLInputElement && event.target.type === 'number') {
        maybeResolveTimezoneFromEdit(event.target);
        scheduleWeatherPreview();
      }
    });
  }
  scheduleWeatherPreview({ immediate: true });
  window.setInterval(() => { if (!document.hidden) scheduleWeatherPreview({ immediate: true, resetRetry: false }); }, 60_000);
}

// On a manual latitude/longitude edit, re-derive the timezone from the new coordinates (debounced).
// The zone is always driven by the geolocation, so a manual coordinate change updates it just like
// picking a location does; an incomplete/invalid pair clears it without a request.
function maybeResolveTimezoneFromEdit(control) {
  const key = control?.dataset?.field;
  if (key !== 'latitude' && key !== 'longitude') return;
  // A manual coordinate edit no longer matches the selected geocoding hit, so the hit's descriptive
  // name/region are now stale: drop them (and refresh the label) before deriving the new zone, so a
  // save never pairs the old location name with new coordinates. selectWeatherLocation sets the
  // inputs via direct value assignment, which fires no input/change event, so the programmatic path
  // never reaches here and keeps the name it just set.
  weatherLocationName = null;
  weatherLocationRegion = null;
  showWeatherLocation();
  const container = $('weather-fields');
  const read = (field) => {
    const raw = (container.querySelector(`[data-field="${field}"]`)?.value || '').trim();
    return raw === '' ? NaN : Number(raw);
  };
  resolveWeatherTimezone(read('latitude'), read('longitude'));
}

function scheduleWeatherPreview({ immediate = false, resetRetry = true } = {}) {
  clearTimeout(weatherPreviewTimer);
  clearTimeout(weatherPreviewRetryTimer);
  weatherPreviewRetryTimer = null;
  if (resetRetry) weatherPreviewRetryMs = 5_000;
  const run = () => previewWeather({ background: !resetRetry }).catch((error) => toast(messageFrom(error), 'danger'));
  if (immediate) { run(); return; }
  weatherPreviewTimer = setTimeout(run, WEATHER_PREVIEW_DEBOUNCE_MS);
}

// The posted body carries domain field names (pv_peak_power_wp); the form control for PV power is
// keyed in kWp. Map a 422 error location back to the control that owns the value so the inline
// message lands on the field the operator actually edits.
const WEATHER_ERROR_FIELD_TO_CONTROL = { pv_peak_power_wp: 'pv_peak_power_kwp' };

// Attach a Bootstrap inline validation message to one form control, matching the is-invalid /
// invalid-feedback convention the other admin forms use. A later edit of the field clears it.
function setWeatherFieldError(key, message) {
  const control = weatherControlByField(key);
  if (!control) return false;
  const container = control.parentElement;
  control.classList.add('is-invalid');
  control.setAttribute('aria-invalid', 'true');
  const errorId = `weather-${key}-error`;
  let node = $('weather-fields')?.querySelector(`#${errorId}`) || $('weather-pv-fields')?.querySelector(`#${errorId}`);
  if (!node) {
    node = element('div', 'invalid-feedback d-block');
    node.id = errorId;
    node.setAttribute('role', 'alert');
    control.parentElement?.append(node);
  }
  node.textContent = message;
  control.setAttribute('aria-describedby', errorId);
  return true;
}

// Drop every inline validation message and invalid flag before a fresh save attempt, so a stale
// error from a previous submit never lingers next to a now-valid field.
function clearWeatherFieldErrors() {
  for (const container of weatherContainers()) {
    for (const control of container.querySelectorAll('[data-field]')) {
      control.classList.remove('is-invalid');
      control.removeAttribute('aria-invalid');
      control.removeAttribute('aria-describedby');
    }
    for (const node of container.querySelectorAll('.invalid-feedback')) node.textContent = '';
  }
}

// Clear the inline validation message on a single control once the operator edits it, so a fixed
// field stops showing a stale error without waiting for the next save.
function clearWeatherFieldErrorFor(control) {
  const key = control?.dataset?.field;
  if (!key || !control.classList?.contains('is-invalid')) return;
  control.classList.remove('is-invalid');
  control.removeAttribute('aria-invalid');
  control.removeAttribute('aria-describedby');
  const node = $('weather-fields')?.querySelector(`#weather-${key}-error`) || $('weather-pv-fields')?.querySelector(`#weather-${key}-error`);
  if (node) node.textContent = '';
}

// Render a structured 422 body ({detail: [{loc, msg}]}) as inline per-field messages and return
// true when at least one field-level message was shown. A field whose control is not on the form
// (or a non-field error) is left to the caller's toast so nothing is silently dropped.
function showWeatherValidationErrors(error) {
  // Normalize both 422 shapes to {field, msg}: the admin HTTPException list ({loc, msg}) and the
  // problem-document per-field list ({parameter, detail}).
  const entries = [];
  if (Array.isArray(error?.detail)) {
    for (const item of error.detail) {
      const msg = typeof item?.msg === 'string' ? item.msg : '';
      const loc = Array.isArray(item?.loc) ? item.loc : [];
      entries.push({ field: loc.length ? String(loc[loc.length - 1]) : '', msg });
    }
  }
  if (Array.isArray(error?.fieldErrors)) {
    for (const item of error.fieldErrors) {
      const msg = typeof item?.detail === 'string' ? item.detail : '';
      entries.push({ field: typeof item?.parameter === 'string' ? item.parameter : '', msg });
    }
  }
  if (!entries.length) return false;
  let shown = false;
  const unplaced = [];
  for (const { field, msg } of entries) {
    if (!msg) continue;
    const key = WEATHER_ERROR_FIELD_TO_CONTROL[field] || field;
    if (key && setWeatherFieldError(key, msg)) shown = true;
    else unplaced.push(msg);
  }
  // A completeness error ("an enabled solar forecast requires latitude, …") names several fields in
  // one model-level message with no single field location: flag each named field so the operator
  // sees exactly what to fill before enabling.
  if (!shown && unplaced.length) {
    for (const message of unplaced) {
      for (const [field, control] of Object.entries({ latitude: 'latitude', longitude: 'longitude', pv_peak_power_wp: 'pv_peak_power_kwp', tilt_deg: 'tilt_deg', azimuth_deg: 'azimuth_deg' })) {
        if (message.includes(field)) shown = setWeatherFieldError(control, 'Required to enable the solar forecast.') || shown;
      }
    }
  }
  return shown;
}

async function saveWeather() {
  const button = $('weather-save');
  const state = $('weather-save-state');
  button.disabled = true;
  state.textContent = 'Saving …';
  clearWeatherFieldErrors();
  // Lock the whole form, not just the save button, for the save duration: a slow PUT must not let
  // the operator keep editing and then have the server re-render stomp their newer input. The
  // success path rebuilds fresh, enabled controls via renderWeather, so re-enabling here only
  // matters on the error/early-return paths.
  const locked = lockFormControls([...weatherContainers(), $('weather-location-search')]);
  try {
    renderWeather(await api('energy/weather/config', { method: 'PUT', body: JSON.stringify(weatherBody()) }));
    state.textContent = 'Saved';
    toast('Solar forecast configuration saved.');
    scheduleWeatherPreview({ immediate: true });
  } catch (error) {
    state.textContent = '';
    // A 422 carries per-field pydantic errors: render them inline on the offending fields and show
    // a short pointer toast instead of surfacing the raw generic message as the only feedback.
    if (error?.status === 422 && showWeatherValidationErrors(error)) {
      toast('Please correct the highlighted fields before saving.', 'warning');
      return;
    }
    throw error;
  } finally {
    button.disabled = false;
    unlockFormControls(locked);
  }
}

function weatherShortTime(iso) {
  if (!iso) return '—';
  // Render in the site's timezone when one is set, falling back to UTC, so the preview reads in the
  // same zone the forecast is anchored to rather than the browser's.
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', timeZone: weatherTimezone || 'UTC' });
}

// One point per interval in the backend's own order (mirrors spotMarketChartPoints). Identity is
// ordinal, not local HH:MM, so a DST fall-back day keeps both duplicated-wall-clock intervals and
// short/long days keep their real count. Each point carries its own HH:MM, derived from the
// interval's absolute start, for ticks and tooltips. Validation stays all-or-nothing.
function weatherChartPoints(intervals, timeZone) {
  if (!Array.isArray(intervals) || intervals.length > 100) return null;
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
    const power = interval?.power_kw;
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || !Number.isFinite(power)) return null;
    points.push({ y: power, label: hhmm(start) });
  }
  return points;
}

function syncWeatherChartTheme() {
  if (!weatherPreviewChart) return;
  const styles = getComputedStyle($('weather-preview'));
  const labelColor = styles.getPropertyValue('--bs-body-color').trim();
  const gridColor = styles.getPropertyValue('--rct-line').trim();
  weatherPreviewChart.options.plugins.legend.labels.color = labelColor;
  for (const axis of Object.values(weatherPreviewChart.options.scales)) {
    axis.ticks.color = labelColor;
    axis.grid.color = gridColor;
    if (axis.title) axis.title.color = labelColor;
  }
  weatherPreviewChart.update('none');
}

function drawWeatherChart(result) {
  weatherPreviewChart?.destroy();
  weatherPreviewChart = null;
  const canvas = $('weather-chart');
  if (!canvas) return;
  if (!window.Chart) {
    canvas.parentElement.replaceChildren(element('p', 'text-danger small', 'Chart library could not be loaded.'));
    return;
  }
  const yesterday = weatherChartPoints(result.yesterday_intervals, result.timezone);
  const today = weatherChartPoints(result.today_intervals, result.timezone);
  const tomorrow = weatherChartPoints(result.next_day_intervals, result.timezone);
  const styles = getComputedStyle($('weather-preview'));
  const labelColor = styles.getPropertyValue('--bs-body-color').trim();
  const gridColor = styles.getPropertyValue('--rct-line').trim();
  // A day-series is usable only when it is a non-null array (weatherChartPoints returns null for a
  // non-array/over-cap/invalid day) that carries at least one real reading. Draw from the usable
  // series only, keeping each day's fixed identity and style; an absent/empty day is simply omitted
  // rather than blanking the whole chart. Show the message only when NO day has usable data.
  const usable = (points) => Array.isArray(points) && points.some((point) => Number.isFinite(point?.y));
  const candidates = [
    { label: 'Yesterday', points: yesterday, borderColor: styles.getPropertyValue('--spot-market-yesterday-line').trim() },
    { label: 'Today', points: today, borderColor: styles.getPropertyValue('--spot-market-today-line').trim(), borderDash: [] },
    { label: 'Tomorrow', points: tomorrow, borderColor: styles.getPropertyValue('--spot-market-tomorrow-line').trim(), borderDash: [6, 4] },
  ].filter((candidate) => usable(candidate.points));
  if (candidates.length === 0) {
    canvas.parentElement.replaceChildren(element('p', 'text-danger small', 'Forecast series is invalid or empty.'));
    return;
  }
  // Ordinal per-day axis (mirrors drawSpotMarketChart): each interval occupies its own slot by its
  // position in the day, not by its local HH:MM. A normal day keeps the familiar 96 quarter-hour
  // labels (00:00..23:45) and an identical look; a DST fall-back day keeps BOTH 02:00 intervals
  // (consecutive slots, no overwrite) and a 100-interval day gets 100 slots. Each dataset keeps its
  // own per-point HH:MM for the tooltip.
  const maxLen = Math.max(96, ...candidates.map((candidate) => candidate.points.length));
  const labels = Array.from({ length: maxLen }, (_, index) => {
    if (index >= 96) return candidates.find((candidate) => candidate.points.length > index)?.points[index]?.label ?? '';
    const hour = String(Math.floor(index / 4)).padStart(2, '0');
    const minute = String((index % 4) * 15).padStart(2, '0');
    return `${hour}:${minute}`;
  });
  const align = (points) => {
    const data = Array(maxLen).fill(null);
    const times = Array(maxLen).fill(null);
    points.forEach((point, index) => { data[index] = point.y; times[index] = point.label; });
    return { data, times };
  };
  const series = candidates.map((candidate) => ({ ...candidate, ...align(candidate.points) }));
  weatherPreviewChart = new window.Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: series.map(({ label, data, times, borderColor, borderDash }) => ({
        label, data, times, borderColor, borderDash, backgroundColor: borderColor,
        stepped: 'after', pointRadius: 0, pointHoverRadius: 4, borderWidth: 2, spanGaps: false,
      })),
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
            title: (items) => items[0].dataset.times?.[items[0].dataIndex] ?? labels[items[0].dataIndex],
            label: (item) => `${item.dataset.label}: ${Number(item.parsed.y).toFixed(2)} kW`,
          },
        },
      },
      scales: {
        x: {
          type: 'category', grid: { color: gridColor }, ticks: {
            color: labelColor, autoSkip: true, maxTicksLimit: 7, minRotation: 0, maxRotation: 0,
          },
        },
        y: {
          grid: { color: gridColor }, ticks: { color: labelColor },
          title: { display: true, text: 'kW', color: labelColor }, beginAtZero: true,
        },
      },
    },
  });
}

function renderWeatherPreview(result) {
  const nodes = [];
  const status = result.status;
  // Unsaved edits have no stored forecast; the preview only describes a saved configuration, so
  // the location tile stays alone until the operator saves.
  if (status === 'unsaved') {
    setPreviewStatus('weather-preview-status', null);
    $('weather-next24')?.replaceChildren();
    return nodes;
  }
  const connected = status === 'connected';
  // The backend flags stale only when the daily retrieval failed and the forecast is still missing.
  const stale = connected && result.stale === true;
  const badgeClass = stale ? 'status-badge-warning' : connected ? 'status-badge-success' : status === 'error' ? 'status-badge-danger' : 'status-badge-neutral';
  const badgeText = stale ? 'Stale' : connected ? 'Connected' : status === 'error' ? 'Error' : status === 'disabled' ? 'Disabled' : 'Unavailable';
  setPreviewStatus('weather-preview-status', badgeClass, badgeText);

  // The Next 24 h tile lives beside the site summary tile, outside this preview container.
  $('weather-next24')?.replaceChildren();

  const contextRow = element('div', 'spot-market-preview-context');
  const sourceLabel = element('span', 'spot-market-preview-context-source', `Source: ${result.source || 'Open-Meteo'}`);
  // The site timezone is derived automatically and read-only; it labels the forecast times.
  if (weatherTimezone) {
    const zone = element('span', 'weather-timezone-value', weatherTimezone);
    zone.id = 'weather-timezone';
    sourceLabel.append(element('span', '', ' · '), zone);
  }
  contextRow.append(sourceLabel);
  nodes.push(contextRow);

  if (status === 'disabled') {
    nodes.push(element('p', 'text-secondary small mb-0', 'Enable the solar forecast and complete the site fields to see a preview.'));
    return nodes;
  }
  if (status === 'unavailable') {
    nodes.push(element('p', 'text-secondary small mb-0', result.message || 'Forecast values are unavailable.'));
    return nodes;
  }
  if (status === 'error') {
    nodes.push(element('p', 'text-danger small mb-0', result.message || 'unknown error'));
    return nodes;
  }

  if (stale) {
    const notice = element('p', 'spot-market-notice-warning small mb-0');
    const icon = element('span', 'material-icons spot-market-notice-icon', 'warning');
    icon.setAttribute('aria-hidden', 'true');
    const when = result.refresh_error_at ? ` since ${weatherShortTime(result.refresh_error_at)}` : '';
    notice.title = result.refresh_error;
    notice.append(icon, element('span', '', `The forecast could not be refreshed${when} and may be out of date. The next attempt runs automatically.`));
    nodes.push(notice);
  }

  if (Number.isFinite(result.energy_next_24h_kwh)) {
    const glyph = element('span', 'material-icons spot-market-highlight-icon text-warning', 'solar_power');
    glyph.setAttribute('aria-hidden', 'true');
    $('weather-next24')?.replaceChildren(
      glyph,
      element('span', 'spot-market-highlight-caption', 'Next 24 h'),
      element('span', 'spot-market-highlight-value', `${result.energy_next_24h_kwh.toFixed(1)} kWh`),
      element('span', 'spot-market-highlight-when', result.covered_until ? `until ${weatherShortTime(result.covered_until)}` : '—'),
    );
  }

  const chartBox = element('div', 'spot-market-chart-box');
  const canvas = element('canvas', '');
  canvas.id = 'weather-chart';
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'PV power for yesterday in grey, today in green, and tomorrow in blue. Each line can be toggled in the legend.');
  chartBox.append(canvas);
  nodes.push(chartBox);
  return nodes;
}

async function previewWeather({ background = false } = {}) {
  const target = $('weather-preview');
  if (!target) return;
  // A transient invalid number (e.g. a value being typed that is out of range) is not preview-fable:
  // skip the request so the backend never sees a 422 from an intermediate state, and leave the
  // current preview in place. A later edit that restores valid (or empty) values previews again.
  if (!canPreviewWeather()) return;
  if (weatherPreviewController) {
    weatherPreviewController.abort(Object.assign(new Error('Superseded by a newer request.'), { name: 'AbortError' }));
  }
  const controller = new AbortController();
  const generation = ++weatherPreviewGeneration;
  weatherPreviewController = controller;
  // The previous preview stays in place while the request runs (the template ships the initial
  // loading state), so a refresh swaps content once instead of flashing a spinner every time.
  try {
    const result = await api('energy/weather/preview', { method: 'POST', body: JSON.stringify(weatherBody()), signal: controller.signal, background });
    if (generation !== weatherPreviewGeneration) return;
    setWeatherTimezone(result.timezone ?? weatherTimezone);
    const signature = JSON.stringify([result, weatherTimezone]);
    if (signature !== weatherPreviewRendered) {
      weatherPreviewRendered = signature;
      weatherPreviewChart?.destroy();
      weatherPreviewChart = null;
      target.replaceChildren(...renderWeatherPreview(result));
      if (result.status === 'connected') drawWeatherChart(result);
    }
    // Only a warming-up database or a provider error can resolve on its own; an unsaved
    // configuration changes only when the operator saves, which schedules a fresh preview.
    if (result.status === 'unavailable' || result.status === 'error') {
      const delay = weatherPreviewRetryMs;
      weatherPreviewRetryMs = Math.min(delay * 2, WEATHER_PREVIEW_MAX_RETRY_MS);
      weatherPreviewRetryTimer = setTimeout(() => scheduleWeatherPreview({ immediate: true, resetRetry: false }), delay);
    } else {
      weatherPreviewRetryMs = 5_000;
    }
  } catch (error) {
    if (generation !== weatherPreviewGeneration || error.name === 'AbortError') return;
    if (error.rateLimited) { // short-lived load shedding: keep the preview and retry after Retry-After
      weatherPreviewRetryTimer = setTimeout(() => scheduleWeatherPreview({ immediate: true, resetRetry: false }), pollDelay(weatherPreviewRetryMs));
      return;
    }
    weatherPreviewRendered = null;
    weatherPreviewChart?.destroy();
    weatherPreviewChart = null;
    $('weather-next24')?.replaceChildren();
    setPreviewStatus('weather-preview-status', null);
    target.replaceChildren(element('p', 'text-danger small mb-0', messageFrom(error)));
    throw error;
  } finally {
    if (generation === weatherPreviewGeneration) weatherPreviewController = null;
  }
}

