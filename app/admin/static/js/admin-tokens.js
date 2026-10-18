//
// app/admin/static/js/admin-tokens.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

function formatDate(value, { time = false, empty = 'No expiry' } = {}) {
  if (!value) return empty;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  // Locale comes from the browser (undefined), never a hardcoded region.
  return new Intl.DateTimeFormat(undefined, time ? { dateStyle: 'medium', timeStyle: 'medium' } : { dateStyle: 'medium' }).format(date);
}

function tokenEmptyRow() {
  const row = element('tr');
  const cell = element('td', 'text-secondary', 'No API tokens created yet.');
  cell.colSpan = 7;
  row.append(cell);
  return row;
}

function tokenRoleCell(role) {
  const write = role !== 'read';
  const cell = element('td');
  cell.dataset.label = 'Permission';
  cell.append(element('span', `status-badge ${write ? 'status-badge-warning' : 'status-badge-success'}`, write ? 'Read and write' : 'Read'));
  return cell;
}

let tokensRequest = 0;

async function loadTokens() {
  const host = $('tokens-list');
  const request = ++tokensRequest;
  const data = await api('tokens');
  if (request !== tokensRequest) return; // a newer load owns the table
  host.replaceChildren();
  if (!data.tokens?.length) { host.append(tokenEmptyRow()); return; }
  for (const token of data.tokens) {
    const row = element('tr');
    // data-label mirrors the column header: the stacked phone layout (.data-table-stack) shows it
    // beside the value, so each value stays associated with its field without duplicate markup.
    const labelled = (cell, label) => { cell.dataset.label = label; return cell; };
    const nowrap = (text, label) => labelled(element('td', 'token-nowrap', text), label);
    const suffixCell = () => {
      const cell = labelled(element('td', 'token-nowrap'), 'Token');
      if (token.suffix) cell.append(element('code', 'token-suffix', `…${token.suffix}`));
      else cell.append('—');
      return cell;
    };
    const actions = element('td', 'text-end token-nowrap data-table-stack-actions');
    const revoke = element('button', 'btn btn-outline-danger btn-sm d-inline-flex align-items-center gap-1');
    const icon = element('span', 'material-icons', 'delete');
    icon.setAttribute('aria-hidden', 'true');
    revoke.append(icon, 'Revoke');
    revoke.type = 'button';
    revoke.setAttribute('aria-label', `Revoke token ${token.name}`);
    revoke.addEventListener('click', async () => {
      if (!await confirmAction({
        title: 'Revoke token?',
        message: `Applications using "${token.name}" lose access immediately. This cannot be undone.`,
        confirmLabel: 'Revoke token',
        danger: true,
      })) return;
      revoke.disabled = true;
      try { await api(`tokens/${encodeURIComponent(token.id)}`, { method: 'DELETE' }); row.remove(); toast('Token revoked.'); if (!host.childElementCount) host.append(tokenEmptyRow()); }
      catch (error) { revoke.disabled = false; toast(messageFrom(error), 'danger'); }
    });
    actions.append(revoke);
    row.append(
      labelled(element('td', '', token.name), 'Name'),
      tokenRoleCell(token.role),
      suffixCell(),
      nowrap(formatDate(token.created_at, { time: true }), 'Created'),
      nowrap(formatDate(token.last_used_at, { time: true, empty: 'Never' }), 'Last used'),
      nowrap(formatDate(token.expires_at, { time: true }), 'Expires'),
      actions,
    );
    host.append(row);
  }
}

// Expiry presets instead of a free date; 'never' sends no expires_at, as an empty date did.
const EXPIRY_DEFAULT = '90d';

function expiryFromPreset(preset) {
  const date = new Date();
  if (preset === '30d') date.setDate(date.getDate() + 30);
  else if (preset === '90d') date.setDate(date.getDate() + 90);
  // Calendar arithmetic, so a leap day shifts to the real anniversary instead of a fixed 365 days.
  else if (preset === '1y') date.setFullYear(date.getFullYear() + 1);
  else return null;
  return date.toISOString();
}

function initTokens() {
  loadTokens().catch((error) => toast(messageFrom(error), 'danger'));
  const modal = $('add-token-modal');
  const form = $('token-form');
  const showResult = (shown) => {
    $('token-form-state').hidden = shown;
    $('new-token-result').hidden = !shown;
    // The dialog is named by whichever title is visible.
    modal.setAttribute('aria-labelledby', shown ? 'token-created-title' : 'add-token-title');
  };
  modal.addEventListener('shown.bs.modal', () => { if (!$('token-form-state').hidden) $('token-name').focus(); });
  // The token is shown once: a stray backdrop click or Escape must not discard it. Only the
  // Done and close buttons (explicit actions) dismiss the result step.
  let explicitClose = false;
  // Done and the close button stay disabled until the copy button was used (even when the clipboard
  // is unavailable and the operator must copy manually), so the token is not dismissed unseen.
  let copied = false;
  const setCopied = (value) => {
    copied = value;
    $('token-done').disabled = !value;
    $('token-close').disabled = !value;
  };
  // These two buttons close through our own handler, so the flag is set before Bootstrap's hide() runs.
  for (const button of modal.querySelectorAll('#new-token-result [data-bs-dismiss="modal"]')) {
    button.removeAttribute('data-bs-dismiss');
    button.addEventListener('click', () => {
      if (!copied) return;
      explicitClose = true;
      window.bootstrap.Modal.getInstance(modal)?.hide();
    });
  }
  modal.addEventListener('hide.bs.modal', (event) => {
    if ($('new-token-result').hidden || explicitClose) return;
    event.preventDefault();
    // The token must be copied before closing: highlight the warning in the modal instead of a toast.
    $('token-copy-warning').hidden = false;
  });
  // Closing by any path wipes the plaintext secret from the DOM and restores the form state.
  modal.addEventListener('hidden.bs.modal', () => {
    explicitClose = false;
    setCopied(false);
    $('token-copy-warning').hidden = true;
    $('new-token-value').textContent = '';
    showResult(false);
    form.reset();
    $('token-expires').value = EXPIRY_DEFAULT;
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    submitState(form, true);
    const expiry = expiryFromPreset($('token-expires').value);
    const body = { name: $('token-name').value.trim(), role: $('token-role').value };
    if (expiry) body.expires_at = expiry;
    try {
      const result = await api('tokens', { method: 'POST', body: JSON.stringify(body) });
      $('new-token-value').textContent = result.token || '';
      setCopied(false);
      showResult(true);
      $('copy-token').focus();
      await loadTokens();
      toast('Token created. Please copy it now.');
    } catch (error) { toast(messageFrom(error), 'danger'); }
    finally { submitState(form, false); }
  });
  $('copy-token').addEventListener('click', async () => {
    if (await copyToClipboard($('new-token-value').textContent)) {
      setCopied(true);
      $('token-copy-warning').hidden = true;
      toast('Token copied.');
    } else {
      // The Clipboard API is unavailable: the token is shown for manual copy, so unlock Done and
      // Close (and the backdrop/Escape path) instead of trapping the user with no way out.
      setCopied(true);
      toast('Copying is not available here. Please select and copy the token.', 'warning');
    }
  });
}

