//
// app/admin/static/js/admin-core.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

'use strict';

const page = document.body.dataset.page;
const $ = (id) => document.getElementById(id);
let csrfToken = '';

// Browser locale; the e2e harness pins the context locale, so "Saved/Updated HH:MM" stay assertable.
const hhmm = (date) => date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

// A docker-logs-style full UTC stamp down to seconds (YYYY-MM-DDTHH:MM:SSZ) for the log console.
// Deterministic (always UTC, never locale time) so the tail reads like `docker logs` and the e2e
// assertions stay stable regardless of the browser's timezone.
const fullTimestamp = (date) => `${date.toISOString().slice(0, 19)}Z`;

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = String(value);
  return node;
}

// Disable every form control inside the given containers (plus any standalone control nodes) for
// the duration of a slow save, so an in-flight PUT cannot race the operator's later edits and have
// the server re-render stomp them. Returns the list of controls that were actually disabled by this
// call so unlockFormControls() can re-enable exactly those (never one that was already disabled).
function lockFormControls(targets) {
  const controls = new Set();
  for (const target of targets) {
    if (!target) continue;
    // A bare control node (e.g. the location search input) locks itself; a container locks its
    // form-control descendants.
    if (typeof target.querySelectorAll === 'function') {
      for (const node of target.querySelectorAll('input, select, button, textarea')) controls.add(node);
      for (const node of target.querySelectorAll('[data-field]')) controls.add(node);
    }
    if ('disabled' in target && target.dataset?.field == null && target.tagName !== 'DIV') controls.add(target);
  }
  const locked = [];
  for (const control of controls) {
    if (!control.disabled) { control.disabled = true; locked.push(control); }
  }
  return locked;
}

// Re-enable exactly the controls a matching lockFormControls() call disabled.
function unlockFormControls(locked) {
  for (const control of locked || []) control.disabled = false;
}

// Auto-dismiss delays: a warning or error is something the operator must be able to read and act on.
const TOAST_TIMEOUT_MS = { success: 3500, info: 3500, warning: 12000, danger: 30000 };
const NETWORK_ERROR_TEXT = 'The server did not respond. Check the connection and try again.';

function toast(message, kind = 'success') {
  const region = $('toast-region');
  // An identical message already showing (or still sliding out) is suppressed instead of
  // stacking a visual duplicate, e.g. two call sites reporting the same failed request.
  const duplicate = [...region.children].some((node) => node.dataset.message === message && node.dataset.kind === kind);
  if (duplicate) return;
  const box = element('div', `alert alert-${kind} d-flex align-items-start gap-2`);
  box.dataset.message = message;
  box.dataset.kind = kind;
  const close = element('button', 'btn-close');
  close.type = 'button';
  close.setAttribute('aria-label', 'Dismiss notification');
  box.append(element('span', 'flex-grow-1', message), close);
  region.append(box);
  requestAnimationFrame(() => box.classList.add('is-visible'));
  const remove = () => box.remove();
  let timer = null;
  let started = 0;
  let remaining = TOAST_TIMEOUT_MS[kind] ?? TOAST_TIMEOUT_MS.success;
  const leave = () => {
    clearTimeout(timer);
    if (box.classList.contains('is-leaving')) return;
    box.classList.remove('is-visible');
    box.classList.add('is-leaving');
    box.addEventListener('transitionend', (event) => { if (event.target === box && event.propertyName === 'transform') remove(); });
    setTimeout(remove, 400); // fallback if the transition does not fire (reduced motion, hidden ancestor)
  };
  const arm = () => { started = Date.now(); timer = setTimeout(leave, remaining); };
  // Hover or keyboard focus pauses the countdown so a message can be read without racing it.
  const pause = () => {
    if (timer === null) return;
    clearTimeout(timer);
    timer = null;
    remaining = Math.max(2000, remaining - (Date.now() - started));
  };
  const resume = () => { if (timer === null && !box.matches(':hover, :focus-within')) arm(); };
  box.addEventListener('mouseenter', pause);
  box.addEventListener('focusin', pause);
  box.addEventListener('mouseleave', resume);
  box.addEventListener('focusout', resume);
  close.addEventListener('click', leave);
  arm();
}

function messageFrom(error) {
  // Only api() marks a failed fetch as a network error. A programming TypeError must keep its real
  // message instead of being misreported as "the server did not respond".
  if (error?.network === true) return NETWORK_ERROR_TEXT;
  return error instanceof Error ? error.message : 'An unknown error occurred.';
}

// Themed replacement for window.confirm(): resolves true only through the explicit accept button;
// focus starts on the safe (cancel) button.
let confirmOpen = false;
function confirmAction({ title, message, confirmLabel, danger = false }) {
  if (confirmOpen) return Promise.resolve(false);
  confirmOpen = true;
  const modalElement = $('confirm-modal');
  const modal = window.bootstrap.Modal.getOrCreateInstance(modalElement);
  const accept = $('confirm-accept');
  $('confirm-title').textContent = title;
  $('confirm-message').textContent = message;
  accept.textContent = confirmLabel;
  accept.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  return new Promise((resolve) => {
    let accepted = false;
    const onAccept = () => { accepted = true; modal.hide(); };
    accept.addEventListener('click', onAccept, { once: true });
    modalElement.addEventListener('shown.bs.modal', () => $('confirm-cancel').focus(), { once: true });
    modalElement.addEventListener('hidden.bs.modal', () => {
      accept.removeEventListener('click', onAccept);
      confirmOpen = false;
      // Hiding this modal clears Bootstrap's body lock even when another modal (e.g. the inverter dialog) is still open.
      if (document.querySelector('.modal.show')) document.body.classList.add('modal-open');
      resolve(accepted);
    }, { once: true });
    modal.show();
  });
}

// navigator.clipboard requires a secure context (HTTPS or localhost) and is undefined on a
// plain-HTTP LAN deployment, so fall back to the classic textarea + execCommand('copy') trick.
async function copyToClipboard(text) {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* fall through to the legacy fallback below */ }
  }
  const textarea = element('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '0';
  textarea.style.left = '0';
  textarea.style.opacity = '0';
  // Keep the fallback inside an open Bootstrap modal so its focus trap permits selection.
  (document.querySelector('.modal.show') || document.body).append(textarea);
  textarea.select();
  textarea.setSelectionRange(0, text.length);
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { copied = false; }
  textarea.remove();
  return copied;
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  let response;
  try {
    response = await fetch(`/admin/api/${path}`, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...(method !== 'GET' ? { 'X-CSRF-Token': csrfToken } : {}),
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    // A genuine fetch failure opens the connection-lost modal at once; a caller-initiated abort
    // says nothing about the link. Mark only this path as network-related so unrelated JavaScript
    // TypeErrors are never mislabeled as server outages.
    const callerAbort = options.signal?.aborted || error?.name === 'AbortError' || error?.name === 'TimeoutError';
    if (!callerAbort) {
      window.RCTReconnect?.start();
      const networkError = new Error(NETWORK_ERROR_TEXT);
      networkError.name = 'NetworkError';
      networkError.network = true;
      networkError.cause = error;
      throw networkError;
    }
    throw error;
  }
  if (response.ok && window.RCTReconnect?.isActive()) window.RCTReconnect.stop();
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !['login', 'change-password'].includes(page)) {
      clearDashboardSnapshot();
      location.assign('/login');
    }
    const detail = data.detail;
    // dispatch_capability_conflict (409) carries a structured detail {detail, operation_id, mode}
    // instead of a plain string; naming the blocking operation is what makes the refusal useful.
    // FastAPI's 422 detail is an array of {msg, ...}; only the documented conflict shape is formatted
    // as operation/mode.
    let message = `Request failed (${response.status}).`;
    if (typeof detail === 'string') message = detail;
    else if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') message = detail[0].msg;
    else if (detail && typeof detail === 'object' && 'operation_id' in detail && 'mode' in detail) {
      message = `${detail.detail || 'Conflict.'} (operation ${detail.operation_id}, mode ${detail.mode})`;
    }
    const error = new Error(message);
    error.status = response.status; // callers (e.g. the Energy poll) branch on 503 vs transient
    // Callers that render per-field validation (e.g. the Weather form) read the raw 422 body: an
    // admin HTTPException carries a {loc, msg} list in `detail`, while the problem-document shape
    // carries per-field entries in `errors` ({parameter, detail}). Expose both; keep `message` as is.
    error.detail = detail;
    error.fieldErrors = Array.isArray(data.errors) ? data.errors : null;
    throw error;
  }
  return data;
}

async function session() {
  const data = await api('session');
  csrfToken = data.csrf_token || '';
  authInitReported = false; // a later failure in this page session must announce itself again
  return data;
}

let sessionPromise = null;

function ensureSession() {
  if (!sessionPromise) {
    // Cache the in-flight request only. A rejected promise is truthy, so caching it would make
    // every later submit await the same failure and never issue a second request.
    sessionPromise = session().catch((error) => { sessionPromise = null; throw error; });
  }
  return sessionPromise;
}

// Central SSE dispatcher. Owns the single EventSource to GET /admin/api/events, its reconnect
// (handled natively by EventSource) and the connection lifecycle ONCE, so a feature only registers
// a typed handler (events.on('tsdb.log', fn)) and never touches the connection. The stream opens
// lazily on the first registration and closes when the last handler is removed or the page is left;
// Phase 2 event types register the same way without changing anything here.
const events = (() => {
  const handlers = new Map(); // event type -> Set<handler>
  // Event types with a native EventSource listener already attached. A second on() of the same type
  // must not add a second native listener (each listener fans out over ALL handlers of that type,
  // so a duplicate would double-execute every handler). Cleared on close() so a reconnect re-binds.
  const boundTypes = new Set();
  let source = null;
  const open = () => {
    if (source) return;
    // Same-origin request; the browser attaches the rct_admin_session cookie automatically, which
    // require_admin accepts. EventSource reconnects on its own after a transient drop.
    source = new EventSource('/admin/api/events');
    for (const type of handlers.keys()) bind(type);
  };
  const bind = (type) => {
    if (boundTypes.has(type)) return;
    boundTypes.add(type);
    source.addEventListener(type, (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch { return; } // ignore a malformed frame
      for (const handler of handlers.get(type) || []) handler(payload);
    });
  };
  const close = () => { if (source) { source.close(); source = null; } boundTypes.clear(); };
  return {
    on(type, handler) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type).add(handler);
      if (source) bind(type);
      else open();
      return () => this.off(type, handler);
    },
    off(type, handler) {
      handlers.get(type)?.delete(handler);
      if ([...handlers.values()].every((set) => set.size === 0)) close();
    },
    close,
  };
})();
// Each admin page is a full page load, so leaving the page unloads the connection; closing the
// EventSource on pagehide releases the server-side subscriber at once. pagehide (not the native
// leave-page event, which design rule R4 bars) is also the reliable close hook on mobile Safari.
window.addEventListener('pagehide', () => events.close());

function submitState(form, busy) {
  form.querySelector('button[type="submit"]').disabled = busy;
}

const AUTH_FIELD_IDS = ['username', 'password', 'current-password', 'new-password', 'confirm-password'];

function clearFormError() {
  $('form-error').hidden = true;
  for (const id of AUTH_FIELD_IDS) { $(id)?.removeAttribute('aria-invalid'); $(id)?.removeAttribute('aria-describedby'); }
}

// Focus goes to the offending field (the alert itself is announced by role="alert"); without one
// the alert box takes focus.
function formError(message, fieldId) {
  clearFormError();
  const box = $('form-error');
  box.textContent = message;
  box.hidden = false;
  const field = fieldId ? $(fieldId) : null;
  if (field) {
    field.setAttribute('aria-invalid', 'true');
    field.setAttribute('aria-describedby', 'form-error');
    field.focus();
  } else box.focus();
}

// The status line of both auth pages. Only JS writes into it, and only to report a problem.
const AUTH_INIT_ID = 'auth-init-state';
let authInitReported = false;

// The first failure focuses #form-error (a blocking error the operator just triggered); repeat
// attempts only refresh the status line, so focus stays in the password field for another Enter.
function setAuthInitError(message) {
  const line = document.getElementById(AUTH_INIT_ID);
  if (line) line.textContent = message;
  if (!authInitReported) { authInitReported = true; formError(message); }
}

// Restores focus (and the caret) after a section was rebuilt, addressed by the element id.
function preserveFocus(render) {
  const active = document.activeElement;
  const id = active?.id;
  const start = active?.selectionStart;
  const end = active?.selectionEnd;
  render();
  const next = id ? document.getElementById(id) : null;
  if (!next) return;
  next.focus();
  if (start != null && typeof next.setSelectionRange === 'function' && /text|search|url|tel|password/.test(next.type)) {
    next.setSelectionRange(start, end);
  }
}

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-bs-theme', next);
  localStorage.setItem('rct-admin-theme', next);
  syncThemeToggle();
  syncSpotMarketChartTheme();
  syncWeatherChartTheme();
  if (typeof syncConsumptionChartTheme === 'function') syncConsumptionChartTheme();
  if (typeof syncPlanChartTheme === 'function') syncPlanChartTheme();
}

// Reflect the active theme on #theme-toggle: aria-pressed reports the state to assistive tech,
// and the glyph plus the label name the action the button performs next (A11Y-05). Initialised
// from the data-bs-theme attribute theme.js already applied before paint.
function syncThemeToggle() {
  const button = $('theme-toggle');
  if (!button) return;
  const dark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
  button.setAttribute('aria-pressed', String(dark));
  const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
  button.setAttribute('aria-label', label);
  button.title = label;
  const icon = button.querySelector('.material-icons');
  if (icon) icon.textContent = dark ? 'light_mode' : 'dark_mode';
}

function initShell() {
  $('theme-toggle')?.addEventListener('click', toggleTheme);
  syncThemeToggle();
  const toggle = $('nav-toggle');
  const setNav = (open) => {
    const nav = $('mobile-nav');
    nav.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  };
  toggle?.addEventListener('click', () => setNav($('mobile-nav').hidden));
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !toggle || $('mobile-nav').hidden) return;
    setNav(false);
    toggle.focus();
  });
  document.addEventListener('click', (event) => {
    if (!toggle || $('mobile-nav').hidden || event.target.closest('#mobile-nav, #nav-toggle')) return;
    setNav(false);
  });
  $('logout-button')?.addEventListener('click', async () => {
    try {
      await api('logout', { method: 'POST' });
      // Drop user-scoped cached state so the next (possibly different) user does not inherit it.
      clearDashboardSnapshot();
      location.assign('/login');
    } catch (error) { toast(messageFrom(error), 'danger'); }
  });
}

// Registered during page parse, before any await: event.preventDefault() is the only thing that
// suppresses the native submit, and it can only be missing when JS is dead — which is exactly the
// state the form's method="post" exists for, so no password can ever land in a URL.
function initAuthForm(formId, submit) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      if (!csrfToken) await ensureSession(); // lazy recovery, single in-flight request
    } catch {
      setAuthInitError('The login form could not be prepared. Please reload the page.');
      return;
    }
    await submit(form);
  });
}

async function submitLogin(form) {
  clearFormError();
  submitState(form, true);
  try {
    const result = await api('login', { method: 'POST', body: JSON.stringify({ username: $('username').value.trim(), password: $('password').value }) });
    clearDashboardSnapshot();
    location.assign(result.must_change_password ? '/change-password' : '/ui/dashboard');
  } catch (error) { formError(messageFrom(error), 'password'); submitState(form, false); }
}

async function submitPasswordChange(form) {
  clearFormError();
  const next = $('new-password').value;
  if (next !== $('confirm-password').value) { formError('The new passwords do not match.', 'confirm-password'); return; }
  submitState(form, true);
  try {
    await api('change-password', { method: 'POST', body: JSON.stringify({ current_password: $('current-password').value, new_password: next }) });
    clearDashboardSnapshot();
    location.assign('/ui/dashboard');
  } catch (error) {
    const message = messageFrom(error);
    formError(message, /current/i.test(message) ? 'current-password' : 'new-password');
    submitState(form, false);
  }
}

