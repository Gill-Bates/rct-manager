//
// app/admin/static/js/admin-parameters.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  let parameterData = { available: [], exposed_names: [], write_names: [] };
  let parameterDirty = false;
  let parameterSending = false;
  let parameterTimer = null;
  let exposedPage = 0;
  let writablePage = 0;
  const addMetricSelection = new Set();
  const writableSelection = new Set();
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
      if ($('writable-list') && !parameterDirty && !parameterSending) { parameterData = await api('parameters'); renderParameters(); }
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
      toast('Parameters saved.', 'success');
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

  // Shared by the Prometheus and Inverters parameter tables: the row "..." menu, the 15-row pager
  // and the checkbox row of the add modals.
  function parameterActionMenu(name, actions, describedBy) {
    const cell = element('td', 'col-actions text-end');
    const menu = element('div', 'dropdown');
    const toggle = element('button', 'btn btn-outline-secondary btn-sm metric-menu-toggle');
    toggle.type = 'button'; toggle.dataset.bsToggle = 'dropdown'; toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', `Actions for ${name}`);
    if (describedBy) toggle.setAttribute('aria-describedby', describedBy);
    toggle.append(element('span', 'material-icons', 'more_horiz'));
    const options = element('ul', 'dropdown-menu dropdown-menu-end');
    for (const { key, label, run, disabled = false } of actions) {
      const item = element('li');
      const button = element('button', 'dropdown-item', label);
      button.type = 'button'; button.disabled = disabled; button.dataset.action = key;
      button.setAttribute('aria-label', `${label}: ${name}`);
      button.addEventListener('click', run);
      item.append(button); options.append(item);
    }
    menu.append(toggle, options); cell.append(menu);
    return cell;
  }

  // Fills <prefix>-list with one page of `entries`, and drives <prefix>-range/-prev/-next. Returns the page.
  function renderParameterTable(prefix, entries, page, buildRow, emptyText, columns, noun) {
    const body = $(`${prefix}-list`);
    const pages = Math.max(1, Math.ceil(entries.length / EXPOSED_PAGE_SIZE));
    const current = Math.min(page, pages - 1);
    entries.slice(current * EXPOSED_PAGE_SIZE, (current + 1) * EXPOSED_PAGE_SIZE).forEach((entry) => body.append(buildRow(entry)));
    if (!entries.length) {
      const empty = element('tr');
      const cell = element('td', 'text-secondary py-4', emptyText);
      cell.colSpan = columns; empty.append(cell); body.append(empty);
    }
    $(`${prefix}-range`).textContent = entries.length ? `${current * EXPOSED_PAGE_SIZE + 1}–${Math.min((current + 1) * EXPOSED_PAGE_SIZE, entries.length)} of ${entries.length}` : `0 ${noun}`;
    $(`${prefix}-prev`).disabled = current === 0;
    $(`${prefix}-next`).disabled = current >= pages - 1;
    return current;
  }

  // Parameter names have no spaces; <wbr> after each underscore gives them clean break points.
  function appendBreakableName(cell, name) {
    const parts = name.split('_');
    parts.forEach((part, index) => {
      cell.append(document.createTextNode(index < parts.length - 1 ? `${part}_` : part));
      if (index < parts.length - 1) cell.append(document.createElement('wbr'));
    });
  }

  function parameterCheckRow(parameter, selection, ariaPrefix, onChange) {
    const label = element('label', 'parameter-item');
    const box = element('input', 'form-check-input');
    box.type = 'checkbox'; box.value = parameter.name; box.checked = selection.has(parameter.name);
    box.setAttribute('aria-label', `${ariaPrefix} ${parameter.name}`);
    box.addEventListener('change', () => {
      if (box.checked) selection.add(parameter.name);
      else selection.delete(parameter.name);
      onChange();
    });
    const main = element('span', 'parameter-main');
    main.append(element('strong', '', parameter.name));
    if (parameter.description) main.append(element('small', 'text-secondary', parameter.description));
    label.append(box, main);
    return label;
  }

  function exposedMetricRow(parameter, index) {
    const row = element('tr', 'metric-row');
    row.dataset.name = parameter.name;
    const dragCell = element('td', 'col-handle');
    const handle = element('span', 'material-icons metric-drag-handle', 'drag_indicator');
    handle.draggable = true;
    handle.title = `Drag to reorder ${parameter.name}`;
    handle.setAttribute('aria-hidden', 'true');
    handle.addEventListener('dragstart', (event) => { event.dataTransfer.setData('text/plain', parameter.name); event.dataTransfer.effectAllowed = 'move'; });
    dragCell.append(handle);
    const nameCell = element('th', 'col-name metric-name');
    nameCell.scope = 'row';
    nameCell.title = parameter.name;
    appendBreakableName(nameCell, parameter.name);
    // Below 768px the description column is hidden, so the description moves into an expandable
    // native <details> inside the name cell instead of being unavailable there.
    if (parameter.description) {
      const mobile = element('details', 'metric-description-mobile');
      mobile.append(element('summary', '', 'Description'), element('span', '', parameter.description));
      nameCell.append(mobile);
    }
    const descriptionCell = element('td', 'col-description text-secondary');
    descriptionCell.append(element('span', 'metric-description', parameter.description || '—'));
    if (parameter.description) descriptionCell.title = parameter.description;
    const actionCell = parameterActionMenu(parameter.name, [
      { key: 'up', label: 'Move up', disabled: index === 0, run: () => moveParameter(parameter.name, -1) },
      { key: 'down', label: 'Move down', disabled: index === parameterData.exposed_names.length - 1, run: () => moveParameter(parameter.name, 1) },
      {
        key: 'remove', label: 'Remove', run: () => {
          parameterData.exposed_names = parameterData.exposed_names.filter((name) => name !== parameter.name);
          queueParameters(); renderParameters();
          $('exposed-search').focus();
        }
      },
    ]);
    row.append(dragCell, nameCell, descriptionCell, actionCell);
    row.addEventListener('dragover', (event) => { event.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', (event) => { event.preventDefault(); row.classList.remove('drag-over'); reorderParameter(event.dataTransfer.getData('text/plain'), parameter.name); });
    return row;
  }

  function availableMetricRow(parameter) {
    return parameterCheckRow(parameter, addMetricSelection, 'Add', updateAddMetricsCount);
  }

  function writableParameterRow(parameter) {
    const row = element('tr', 'metric-row');
    row.dataset.name = parameter.name;
    const nameCell = element('th', 'col-name metric-name');
    nameCell.scope = 'row';
    nameCell.title = parameter.name;
    appendBreakableName(nameCell, parameter.name);
    const mobileText = [parameter.description, parameter.help_text].filter(Boolean);
    // Below 768px the description column is hidden; same disclosure as the Prometheus table.
    if (mobileText.length) {
      const mobile = element('details', 'metric-description-mobile');
      mobile.append(element('summary', '', 'Description'), element('span', '', mobileText.join(' ')));
      nameCell.append(mobile);
    }
    const descriptionCell = element('td', 'col-description text-secondary');
    descriptionCell.append(element('span', 'metric-description', parameter.description || '—'));
    let helpId = '';
    if (parameter.help_text) {
      // Visible text rather than a tooltip, and tied to the row menu so a screen reader reads it
      // with the control. Parameter names match [a-z][a-z0-9_]{0,63}, so the id stays unique.
      const help = element('small', 'parameter-help d-block mt-1', parameter.help_text);
      help.id = `write-help-${parameter.name}`;
      helpId = help.id;
      descriptionCell.append(help);
    }
    const actionCell = parameterActionMenu(parameter.name, [
      {
        key: 'revoke', label: 'Remove write access', run: () => {
          parameterData.write_names = parameterData.write_names.filter((name) => name !== parameter.name);
          queueParameters(); renderParameters();
          $('writable-search').focus();
        }
      },
    ], helpId);
    row.append(nameCell, descriptionCell, actionCell);
    return row;
  }

  function updateAddWritableCount() {
    if (page !== 'inverters') return;
    const count = writableSelection.size;
    $('add-writable-count').textContent = `${count} selected`;
    $('confirm-add-writable').textContent = count ? `Add ${count} parameters` : 'Add parameters';
    $('confirm-add-writable').disabled = !count;
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
    // Deliberately locale-independent fold: internal search comparison, not user-visible output.
    const normalise = (id) => ($(id)?.value || '').trim().toLowerCase();
    const matches = (item, term) => `${item.name} ${item.description || ''} ${item.help_text || ''}`.toLowerCase().includes(term);
    const exposedSearch = normalise('exposed-search');
    const availableSearch = normalise('parameter-search');
    const writeSearch = normalise('writable-search');
    const writeAvailableSearch = normalise('writable-available-search');
    const available = $('available-list') || element('div');
    const writableAvailable = $('writable-available-list') || element('div');
    $('exposed-list')?.replaceChildren(); $('writable-list')?.replaceChildren();
    available.replaceChildren(); writableAvailable.replaceChildren();
    if ($('exposed-list')) {
      const filtered = parameterData.exposed_names
        .map((name, index) => ({ item: names.get(name), index }))
        .filter(({ item }) => item?.exportable !== false && item && matches(item, exposedSearch));
      exposedPage = renderParameterTable('exposed', filtered, exposedPage, ({ item, index }) => exposedMetricRow(item, index),
        exposedSearch ? 'No matching metrics.' : 'No metrics selected.', 4, 'metrics');
      renderPrometheusSummary();
    }
    if ($('writable-list')) {
      const granted = parameterData.write_names.map((name) => names.get(name)).filter((item) => item?.writable && matches(item, writeSearch));
      writablePage = renderParameterTable('writable', granted, writablePage, writableParameterRow,
        writeSearch ? 'No matching parameters.' : 'No writable parameters selected.', 3, 'parameters');
    }
    let availableMatches = 0;
    let writableMatches = 0;
    for (const item of parameterData.available) {
      if (page === 'prometheus' && item.exportable !== false && !parameterData.exposed_names.includes(item.name) && matches(item, availableSearch)) {
        availableMatches += 1;
        if (availableMatches <= 100) available.append(availableMetricRow(item));
      }
      if (page === 'inverters' && item.writable && !parameterData.write_names.includes(item.name) && matches(item, writeAvailableSearch)) {
        writableMatches += 1;
        if (writableMatches <= 100) writableAvailable.append(parameterCheckRow(item, writableSelection, 'Allow writing', updateAddWritableCount));
      }
    }
    if (availableMatches > 100) available.append(element('p', 'text-secondary small', `${availableMatches - 100} more metrics. Refine the search to select them.`));
    if (writableMatches > 100) writableAvailable.append(element('p', 'text-secondary small', `${writableMatches - 100} more parameters. Refine the search to select them.`));
    if (page === 'prometheus' && !available.childElementCount) available.append(element('p', 'text-secondary small', availableSearch ? 'No matching metrics.' : 'All metrics are exposed.'));
    if (page === 'inverters' && !writableAvailable.childElementCount) writableAvailable.append(element('p', 'text-secondary small', writeAvailableSearch ? 'No matching parameters.' : 'All writable parameters are selected.'));
    updateAddMetricsCount();
    updateAddWritableCount();
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
    $('writable-search')?.addEventListener('input', () => { writablePage = 0; renderParameters(); });
    $('writable-prev')?.addEventListener('click', () => { writablePage -= 1; renderParameters(); });
    $('writable-next')?.addEventListener('click', () => { writablePage += 1; renderParameters(); });
    $('writable-available-search')?.addEventListener('input', renderParameters);
    $('confirm-add-writable')?.addEventListener('click', () => {
      const additions = parameterData.available.filter((item) => writableSelection.has(item.name) && item.writable && !parameterData.write_names.includes(item.name)).map((item) => item.name);
      if (!additions.length) return;
      parameterData.write_names.push(...additions);
      writableSelection.clear();
      queueParameters(); renderParameters();
      window.bootstrap.Modal.getInstance($('add-writable-modal'))?.hide();
    });
    $('add-writable-modal')?.addEventListener('shown.bs.modal', () => $('writable-available-search').focus());
    $('add-writable-modal')?.addEventListener('hidden.bs.modal', () => { writableSelection.clear(); $('writable-available-search').value = ''; renderParameters(); });
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
