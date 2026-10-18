//
// app/admin/static/js/admin-settings-view.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

  function buildApplyBar(prefix, { onApply, applyLabel = 'Apply changes' }) {
    const bar = element('div', 'apply-bar');
    const apply = element('button', 'btn btn-primary', applyLabel);
    apply.type = 'button';
    apply.id = `${prefix}-apply`;
    apply.addEventListener('click', onApply);
    bar.append(apply);
    return bar;
  }

  // One builder for both renderSettings() and rerenderGroup(), so a group re-render cannot emit a
  // second <h2>. The Prometheus page does not use this builder; it renders its master toggle and
  // dependents through renderPrometheusSettings().
  function buildGroupBody(group) {
    const body = element('div', 'card-body');
    body.append(element('h2', 'h5 mb-3', group));
    if (group === 'Inverters') renderDevicesSettings(body);
    else {
      for (const field of settingFields.filter((item) => item.group === group && Object.hasOwn(settingsDraft, item.key) && settingVisible(item))) body.append(settingControl(field));
      // The Change-password action lives at the bottom of the Access card, below its toggles.
      if (group === 'Access') renderAccount(body);
    }
    return body;
  }

  function groupSectionId(group) { return `settings-group-${group.toLowerCase().replace(/\s+/g, '-')}`; }

  // Replaces only the affected group, so unrelated sections — and their focus — are never touched.
  function rerenderGroup(group) {
    const section = document.getElementById(groupSectionId(group));
    if (!section) { renderSettings(); return; } // not on this page / never rendered as a <section>
    section.replaceChildren(buildGroupBody(group));
  }

  // The Prometheus page splits the group: the master toggle lives at the top of the status card
  // (#prometheus-toggle), while its dependent fields render straight into #settings-sections. The
  // master's live gating goes through rerenderPrometheusDependents(), not the shared section card.
  function renderPrometheusSettings() {
    const toggleHost = $('prometheus-toggle');
    if (toggleHost) toggleHost.replaceChildren(settingControl(prometheusMasterField()));
    rerenderPrometheusDependents();
  }

  function prometheusMasterField() {
    return settingFields.find((field) => field.key === 'enable_metrics_endpoint');
  }

  function rerenderPrometheusDependents() {
    const host = $('settings-sections');
    if (!host) return;
    const fields = settingFields.filter((item) => item.group === 'Prometheus'
      && item.key !== 'enable_metrics_endpoint' && Object.hasOwn(settingsDraft, item.key) && settingVisible(item));
    // No dependent fields are visible while the endpoint is off, so the card is dropped entirely
    // rather than left as an empty shell.
    if (!fields.length) { host.replaceChildren(); return; }
    const section = element('section', 'card');
    const body = element('div', 'card-body');
    body.append(element('h2', 'h5 mb-3', 'Scrape settings'));
    for (const field of fields) body.append(settingControl(field));
    section.append(body);
    host.replaceChildren(section);
  }

  function renderSettings() {
    if (page === 'prometheus') { renderPrometheusSettings(); return; }
    const host = $('settings-sections');
    host.replaceChildren();
    const groups = {
      settings: ['Access', 'Server', 'Network', 'Session'],
      dashboard: ['Inverters'],
      inverters: ['Write access'],
      tsdb: ['Export'],
    }[page] || [];
    for (const group of groups) {
      if (group === 'Export') { renderExportSettings(host); continue; }
      const section = element('section', 'card');
      section.id = groupSectionId(group);
      section.append(buildGroupBody(group));
      host.append(section);
    }
  }

  function renderAccount(body) {
    // A .setting-row so the Change-password action shares the group card's hairline and row rhythm.
    const row = element('div', 'setting-row');
    const change = element('a', 'btn btn-outline-secondary', 'Change password');
    change.href = '/change-password';
    row.append(change);
    body.append(row);
  }

  async function initSettings() {
    const [result, devices] = await Promise.all([
      api('settings'),
      page === 'tsdb' ? api('devices') : Promise.resolve(null),
    ]);
    settingsCommitted = structuredClone(result.settings || {});
    settingsDraft = structuredClone(settingsCommitted);
    runningInContainer = result.in_container === true;
    if (devices) tsdbRuntimeStatus = devices.tsdb || null;
    // The one place that mints a device identity, besides the blank trailing row.
    if (Object.hasOwn(settingsDraft, 'devices')) adoptDevices(settingsDraft.devices);
    renderSettings();
    renderPrometheusSummary();
  }

  // Dashboard only: defer the sensitive settings read until the operator first opens the inverters
  // modal, so a plain dashboard view never fetches it (SEC-06). Loads once; a failure is reported
  // and leaves the "Loading settings …" placeholder so a later open can retry.
  function initInvertersModalLazy() {
    const modal = $('inverters-modal');
    if (!modal) return;
    let loaded = false;
    modal.addEventListener('show.bs.modal', () => {
      if (loaded) return;
      loaded = true;
      initSettings().catch((error) => { loaded = false; toast(messageFrom(error), 'danger'); });
    });
    // Each open starts without a pending add; the on-close toast fires only for an add made in
    // this interaction. A remove or an open/close with no add leaves the flag false and toasts nothing.
    modal.addEventListener('shown.bs.modal', () => { inverterAddedSinceOpen = false; });
    modal.addEventListener('hidden.bs.modal', () => {
      if (!inverterAddedSinceOpen) return;
      inverterAddedSinceOpen = false;
      toast('Inverter added.', 'success');
    });
  }

